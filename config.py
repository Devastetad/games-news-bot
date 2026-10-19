# Настройки бота. Меняйте под себя — бот подхватит при перезапуске.

# Источники новостей: название -> RSS-лента.
# Лишнее можно закомментировать, своё — добавить по тому же образцу.
FEEDS = {
    # Русскоязычные
    "StopGame": "https://rss.stopgame.ru/rss_news.xml",
    "Игромания": "https://www.igromania.ru/rss/news.rss",
    "Канобу": "https://kanobu.ru/rss/news.xml",
    "3DNews": "https://3dnews.ru/games/rss/",
    "PlayGround": "https://www.playground.ru/rss/news.xml",
    # Англоязычные
    "IGN": "https://feeds.feedburner.com/ign/news",
    "PC Gamer": "https://www.pcgamer.com/rss/",
    "Eurogamer": "https://www.eurogamer.net/feed/news",
    "GameSpot": "https://www.gamespot.com/feeds/game-news",
    "Rock Paper Shotgun": "https://www.rockpapershotgun.com/feed/news",
}

# Как часто проверять ленты, в минутах
CHECK_INTERVAL_MIN = 5

# Фильтр по словам в заголовке/описании (без учёта регистра).
# Пустой список — присылать всё. Пример: ["GTA", "Elden Ring", "Nintendo"]
KEYWORDS = []

# Слова-исключения: новости с ними не присылаются. Пример: ["скидк", "deal"]
STOP_WORDS = []

# Длина краткого описания под заголовком (0 — без описания)
SUMMARY_LEN = 250

# Сколько новостей показывает команда /latest
LATEST_COUNT = 10

# Пауза между отправками, секунд (Telegram ограничивает частоту сообщений)
SEND_DELAY_SEC = 3

WELCOME_TEXT = (
    "🎮 Привет! Я собираю игровые новости круглосуточно и присылаю их сюда.\n\n"
    "Вы подписаны. Команды:\n"
    "/latest — последние новости\n"
    "/sources — откуда беру новости\n"
    "/stop — отписаться"
)
