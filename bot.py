"""Telegram-бот: круглосуточно собирает игровые новости из RSS-лент.

Два режима:
  python bot.py --once  — один проход: ответить на накопившиеся команды,
                          собрать и разослать новые новости, выйти.
                          Так бот запускается в GitHub Actions по расписанию.
  python bot.py         — работать без остановки на своём компьютере.

Новые новости уходят подписчикам (всем, кто нажал /start) и, если задан
CHANNEL_ID, в канал. Всё состояние (что уже отправлено, подписчики) —
в state.json, поэтому повторов не бывает.
"""

import html
import json
import logging
import os
import re
import sys
import threading
import time
from calendar import timegm
from datetime import datetime
from pathlib import Path

import feedparser
import requests

import config

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%d.%m %H:%M:%S",
)
log = logging.getLogger("games-news")


def load_env(path):
    """Читает KEY=VALUE из .env в os.environ (без сторонних библиотек)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(BASE_DIR / ".env")

# Если Python не доверяет сертификатам (антивирус, корпоративная сеть) —
# truststore берёт их из хранилища Windows.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass


# ---------------------------------------------------------------- API Telegram

class TelegramApi:
    def __init__(self, token, proxy=None):
        self.url = f"https://api.telegram.org/bot{token}/"
        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def call(self, method, timeout=40, **params):
        resp = self.session.post(self.url + method, json=params, timeout=timeout)
        data = resp.json()
        if not data.get("ok"):
            retry = data.get("parameters", {}).get("retry_after")
            if retry:  # превысили лимит Telegram — ждём и повторяем
                time.sleep(retry + 1)
                return self.call(method, timeout, **params)
            raise RuntimeError(f"{method}: {data.get('description', resp.text[:300])}")
        return data["result"]

    def get_updates(self, offset, wait=30):
        # wait — long polling на стороне Telegram, поэтому HTTP-таймаут больше
        params = {"offset": offset, "timeout": wait, "allowed_updates": ["message"]}
        resp = self.session.post(self.url + "getUpdates", json=params, timeout=wait + 15)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"getUpdates: {data.get('description')}")
        return data["result"]

    def send(self, chat_id, text, preview_url=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if preview_url:
            params["link_preview_options"] = {"url": preview_url, "prefer_large_media": True}
        else:
            params["link_preview_options"] = {"is_disabled": True}
        return self.call("sendMessage", **params)


# ---------------------------------------------------------------- состояние

class Storage:
    """state.json: отправленные новости, подписчики, offset Telegram.

    Новости пишутся по одной на строку — так изменения в git-истории
    остаются маленькими.
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.offset = data.get("offset")
        self.subs = set(data.get("subscribers", []))
        self.news = data.get("news", {})  # link -> [title, source, published, added]

    def save(self):
        with self.lock:
            lines = [f"  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False)}"
                     for k, v in sorted(self.news.items())]
            text = (
                "{\n"
                f'"offset": {json.dumps(self.offset)},\n'
                f'"subscribers": {json.dumps(sorted(self.subs))},\n'
                '"news": {\n' + ",\n".join(lines) + "\n}\n}\n"
            )
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.path)

    def is_empty(self):
        return not self.news

    def add_news(self, item):
        """True, если новость новая (и она записана)."""
        with self.lock:
            if item["link"] in self.news:
                return False
            self.news[item["link"]] = [item["title"], item["source"],
                                       item["published"], int(time.time())]
            return True

    def latest(self, n):
        with self.lock:
            rows = sorted(self.news.items(), key=lambda kv: kv[1][2], reverse=True)[:n]
            return [(v[0], link, v[1]) for link, v in rows]

    def cleanup(self, days=30):
        border = int(time.time()) - days * 86400
        with self.lock:
            self.news = {k: v for k, v in self.news.items() if v[3] >= border}

    def subscribe(self, chat_id):
        with self.lock:
            self.subs.add(chat_id)

    def unsubscribe(self, chat_id):
        with self.lock:
            self.subs.discard(chat_id)

    def subscribers(self):
        with self.lock:
            return list(self.subs)


# ---------------------------------------------------------------- сбор новостей

def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean(text):
    """HTML из RSS -> обычный текст в одну строку."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return re.sub(r"\s*\[(…|\.\.\.)\]$", "", text)  # хвост «[…]» из WordPress-лент


def passes_filter(item):
    hay = (item["title"] + " " + item["summary"]).lower()
    if config.STOP_WORDS and any(w.lower() in hay for w in config.STOP_WORDS):
        return False
    if config.KEYWORDS:
        return any(w.lower() in hay for w in config.KEYWORDS)
    return True


def fetch_feed(source, url):
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 games-news-bot"})
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for e in feed.entries:
        link = e.get("link")
        title = clean(e.get("title"))
        if not link or not title:
            continue
        parsed = e.get("published_parsed") or e.get("updated_parsed")
        items.append({
            "source": source,
            "title": title,
            "link": link,
            "summary": clean(e.get("summary")),
            "published": timegm(parsed) if parsed else int(time.time()),
        })
    return items


def format_news(item):
    text = f"🎮 <b>{esc(item['title'])}</b>\n"
    summary = item["summary"]
    if config.SUMMARY_LEN and summary and summary != item["title"]:
        if len(summary) > config.SUMMARY_LEN:
            summary = summary[:config.SUMMARY_LEN].rsplit(" ", 1)[0] + "…"
        text += f"\n{esc(summary)}\n"
    text += f'\n📰 {esc(item["source"])} · <a href="{esc(item["link"])}">читать</a>'
    return text


class Collector:
    """Обходит ленты и рассылает новое."""

    def __init__(self, api, storage, channel_id):
        self.api = api
        self.storage = storage
        self.channel_id = channel_id

    def step(self):
        # Первый запуск: просто запоминаем текущие новости, чтобы не завалить
        # чат сотней старых сообщений. Дальше — только свежие.
        if self.storage.is_empty():
            n = len(self.collect())
            log.info("Первый запуск: запомнил %d текущих новостей, слежу за новыми", n)
        else:
            fresh = self.collect()
            log.info("Новых новостей: %d", len(fresh))
            self.broadcast(fresh)
        self.storage.cleanup()
        self.storage.save()

    def loop(self):
        while True:
            try:
                self.step()
            except Exception:
                log.exception("Ошибка в цикле сбора")
            time.sleep(config.CHECK_INTERVAL_MIN * 60)

    def collect(self):
        """Возвращает новые (ещё не виденные) новости, от старых к новым."""
        fresh = []
        for source, url in config.FEEDS.items():
            try:
                items = fetch_feed(source, url)
            except Exception as e:
                log.warning("%s: не удалось загрузить ленту (%s)", source, e)
                continue
            for item in items:
                if self.storage.add_news(item) and passes_filter(item):
                    fresh.append(item)
        fresh.sort(key=lambda i: i["published"])
        return fresh

    def broadcast(self, items):
        targets = self.storage.subscribers()
        if self.channel_id:
            targets.append(self.channel_id)
        for item in items:
            text = format_news(item)
            for chat_id in targets:
                try:
                    self.api.send(chat_id, text, preview_url=item["link"])
                except Exception as e:
                    log.warning("Не отправилось в %s: %s", chat_id, e)
                    if "blocked" in str(e) or "chat not found" in str(e):
                        self.storage.unsubscribe(chat_id)
            time.sleep(config.SEND_DELAY_SEC)


# ---------------------------------------------------------------- команды

def handle(api, storage, msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip().split("@")[0]

    if text == "/start":
        storage.subscribe(chat_id)
        api.send(chat_id, config.WELCOME_TEXT)
    elif text == "/stop":
        storage.unsubscribe(chat_id)
        api.send(chat_id, "Вы отписались. Вернуться — /start")
    elif text == "/latest":
        rows = storage.latest(config.LATEST_COUNT)
        if not rows:
            api.send(chat_id, "Пока пусто — новости ещё собираются.")
            return
        lines = [f'• <a href="{esc(link)}">{esc(title)}</a> <i>({esc(src)})</i>'
                 for title, link, src in rows]
        api.send(chat_id, "🕹 <b>Последние новости</b>\n\n" + "\n\n".join(lines))
    elif text == "/sources":
        api.send(chat_id, "📚 <b>Источники</b>\n\n" + "\n".join(f"• {esc(s)}" for s in config.FEEDS))
    elif text == "/id":
        api.send(chat_id, f"Chat ID: <code>{chat_id}</code>")
    else:
        api.send(chat_id, "Команды: /latest, /sources, /stop")


def process_updates(api, storage, wait):
    for update in api.get_updates(storage.offset, wait):
        storage.offset = update["update_id"] + 1
        if "message" in update:
            try:
                handle(api, storage, update["message"])
            except Exception:
                log.exception("Ошибка при обработке сообщения")


# ---------------------------------------------------------------- запуск

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен.")

    proxy = os.environ.get("TG_PROXY")
    channel_id = os.environ.get("CHANNEL_ID")
    storage = Storage(STATE_FILE)

    if "--once" in sys.argv:
        api = TelegramApi(token, proxy)
        process_updates(api, storage, wait=0)  # команды, пришедшие с прошлого запуска
        storage.save()
        Collector(api, storage, channel_id).step()
        return

    api = TelegramApi(token, proxy)
    while True:
        try:
            me = api.call("getMe")
            break
        except requests.RequestException as e:
            log.warning("Нет связи с Telegram (%s) — повтор через 10 с", e)
            time.sleep(10)
    log.info("Бот @%s запущен (%s). Остановить — Ctrl+C",
             me["username"], datetime.now().strftime("%d.%m.%Y %H:%M"))

    # У сборщика своя сессия requests — их нельзя делить между потоками
    collector = Collector(TelegramApi(token, proxy), storage, channel_id)
    threading.Thread(target=collector.loop, daemon=True).start()

    while True:
        try:
            process_updates(api, storage, wait=30)
            storage.save()
        except KeyboardInterrupt:
            log.info("Остановлен.")
            break
        except Exception as e:
            log.warning("Сеть: %s — повтор через 5 с", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
