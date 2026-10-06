"""Guides: one list for the bot («Помощь», /help in chats) and the docs site (the API server serves docs/guides/*.md
at <docs_url>/<slug>; the API reference itself is <docs_url>)."""
from bot.ui import doc, doc_url, esc

# slug ("" = the API reference), title, what it is about
GUIDES: list[tuple[str, str, str]] = [
    ("start", "Как начать", "заявка на вход, роли, главное меню"),
    ("buy", "Как купить USDT", "сумма в рублях → реквизиты → перевод → PDF-чек"),
    ("sell", "Как продать USDT", "своя карта, смена, подтверждение поступлений"),
    ("merchant", "Как стать ордерным мерчантом", "анкета, кабинет, заявки под точную сумму"),
    ("orders", "Заявки: Bybit-ордер или баланс", "как взять заявку и довести её до конца"),
    ("operator", "Операторы", "приём Bybit-ордеров, подтверждение оплаты, долг"),
    ("team", "Кто такой тимлид", "своя команда, реферальная ссылка, чат и 1% со сделок"),
    ("wallet", "Кошелёк", "пополнение и вывод USDT в сети TON, комиссии"),
    ("disputes", "Споры и безопасность", "чеки, сроки, общение только через бота"),
    ("", "API для сервисов", "приём рублей на сайте или в боте с зачислением в USDT"),
]


def lines() -> list[str]:
    """«• <hidden link>Title</a> — what it is about» per guide, for the bot's texts."""
    return [f"• {doc(slug, f'<b>{esc(name)}</b>')} — {esc(about)}" for slug, name, about in GUIDES]


def index_url() -> str | None:
    return doc_url("help")
