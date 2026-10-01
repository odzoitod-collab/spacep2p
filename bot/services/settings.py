from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Setting

# key: (default, type, title)
SPEC: dict[str, tuple[str, str, str]] = {
    "rate": ("100", "dec", "Курс сервиса за 1 USDT"),
    "seller_pct": ("5", "pct", "Процент мерчанта со статичной картой"),
    "order_rate": ("104", "dec", "Курс ордерного мерчанта за 1 USDT"),
    "platform_pct": ("6", "pct", "Процент площадки"),
    "deposit_fee": ("1.5", "pct", "Комиссия пополнения"),
    "deposit_min": ("1", "dec", "Минимальное пополнение"),
    "withdraw_min": ("1", "dec", "Минимальный вывод"),
    "withdraw_fee": ("0", "dec", "Комиссия вывода чеком"),
    "deal_minutes": ("30", "int", "Время на оплату сделки"),
    "confirm_minutes": ("30", "int", "Покупатель может открыть спор через"),
    "escalate_minutes": ("1440", "int", "Автоспор, если продавец молчит"),
    "late_hold_minutes": ("30", "int", "Удержание залога после истечения"),
    "late_minutes": ("720", "int", "Приём позднего чека после срока"),
    "buyer_fail_limit": ("3", "int", "Лимит отмен покупателя за 24 ч"),
    "adjust_approval_usdt": ("0", "dec", "Второй админ для корректировок от"),
    "online_minutes": ("60", "int0", "Автоконец смены без действий"),
    "receipt_images": ("0", "int0", "Формат чеков"),
    "log_all": ("1", "int0", "Лог-чат"),
    "support": ("", "text", "Ник поддержки"),
    "manual_url": ("https://telegra.ph/Strait-Pay--P2P-obmen-USDT--RUB-v-Telegram-09-27", "url", "Ссылка на инструкцию"),
    "order_min_rub": ("1000", "dec", "Ордер: минимальная сумма"),
    "order_max_rub": ("1000000", "dec", "Ордер: максимальная сумма"),
    "order_search_minutes": ("15", "int", "Ордер: поиск мерчанта"),
    "order_take_minutes": ("10", "int", "Ордер: мерчанту на выдачу реквизитов"),
    "order_pay_minutes": ("15", "int", "Ордер: минимальное время на оплату"),
    "order_check_minutes": ("15", "int", "Ордер: оператору на Bybit-ордер"),
    "chain_withdraw_min": ("3", "dec", "Минимальный вывод на кошелёк"),
    "chain_withdraw_fee": ("1", "dec", "Комиссия вывода на кошелёк"),
    "chat_id": ("", "chat", "Чат сообщества"),
    "tutorial": (
        "<b>Купить</b>: продавец → сумма → перевод по реквизитам → PDF-чек. USDT придут после подтверждения.\n"
        "<b>Продать</b>: пополните кошелёк, добавьте карту, выйдите на смену, подтверждайте поступления.\n"
        "<b>Кошелёк</b>: пополнение и вывод USDT через xRocket — счёт, адрес в любой сети или чек.",
        "html",
        "Текст «Как это работает»",
    ),
}

# admin panel sections: (title, keys); every SPEC key is in exactly one section
GROUPS: list[tuple[str, list[str]]] = [
    ("Курс и комиссии", ["rate", "order_rate", "seller_pct", "platform_pct", "deposit_fee", "withdraw_fee",
                         "chain_withdraw_fee"]),
    ("Сроки сделок", ["deal_minutes", "confirm_minutes", "escalate_minutes", "late_hold_minutes", "late_minutes",
                      "online_minutes"]),
    ("Кошелёк и лимиты", ["deposit_min", "withdraw_min", "chain_withdraw_min", "buyer_fail_limit",
                          "adjust_approval_usdt"]),
    ("Правила и лог-чат", ["receipt_images", "log_all"]),
    ("Тексты, поддержка, чат", ["support", "tutorial", "manual_url", "chat_id"]),
    ("Ордерные реквизиты", ["order_min_rub", "order_max_rub", "order_search_minutes", "order_take_minutes",
                            "order_pay_minutes", "order_check_minutes"]),
]
HINTS = {
    "dec": "Число, дробная часть через точку или запятую.",
    "pct": "Процент от 0 до 99,999.",
    "int": "Целое число от 1 до 1440 (для сроков — минуты; 1440 = сутки).",
    "int0": "Целое число от 0 до 1440; 0 — выключено.",
    "text": "Ник без @, 5–32 латинских букв, цифр или _. «-» — убрать.",
    "html": "Текст до 3000 символов, можно с форматированием Telegram.",
    "receipt_images": "1 — принимать PDF и фото/скриншоты, 0 — только PDF.",
    "log_all": "1 — в лог-чат идут все шаги сделок, 0 — только проблемы.",
    "order_rate": "Сколько рублей ордерный мерчант получает за 1 USDT: он отдаёт сумму заявки / этот курс USDT "
                  "(через Bybit-ордер или из баланса). Процента у ордерных мерчантов нет. Не выше курса сервиса / "
                  "(1 − процент площадки), иначе площадка доплачивала бы покупателю из своих.",
    "seller_pct": "Процент от суммы сделки по статичной карте, который получает мерчант. Не больше процента площадки.",
    "chat": "ID группы, например <code>-1001234567890</code> (бот — админ с правом приглашать и закреплять). "
            "«-» — отключить чат.",
    "url": "Ссылка https://… (например, на статью в Telegraph). «-» — убрать ссылку из бота.",
    "chain_withdraw_fee": "USDT площадке с каждого вывода на кошелёк. Комиссия сети xRocket добавляется сверху "
                          "и зависит от сети.",
    "deposit_fee": "Процент с каждого пополнения — и счётом, и по адресу. Удерживается из поступившей суммы.",
    "buyer_fail_limit": "Целое число: сколько отмен/просрочек за сутки допускается до блокировки покупок.",
}
FLAGS = ("receipt_images", "log_all")
RATES = ("rate", "order_rate")  # RUB per 1 USDT

_cache: dict[str, str] = {}


async def load(s: AsyncSession) -> None:
    rows = (await s.scalars(select(Setting))).all()
    # No await between clear() and update(): concurrent handlers must never observe
    # defaults instead of configured values (e.g. a default rate while pricing a deal).
    fresh = {k: v[0] for k, v in SPEC.items()} | {r.key: r.value for r in rows}
    _cache.clear()
    _cache.update(fresh)


def merchant_pct(user) -> Decimal:
    """A static-card merchant's percent: personal if an admin set one, the general one otherwise; never above the
    platform's, so a later cut of platform_pct can never make the platform pay out of pocket."""
    return min(user.pct_static if user.pct_static is not None else dec("seller_pct"), dec("platform_pct"))


def client_terms(client) -> tuple[Decimal, Decimal]:
    """(rate, platform percent) an API client is priced by — his own if an admin set them — for every order,
    static card or order requisites alike."""
    return (client.rate if client.rate is not None else dec("rate"),
            client.pct if client.pct is not None else dec("platform_pct"))


def order_rate_cap(rate: Decimal, platform_pct: Decimal) -> Decimal:
    """Highest order_rate at which the platform still covers the buyer: order USDT >= buyer's USDT."""
    return (rate / (1 - platform_pct / 100)).quantize(Decimal("0.01"), "ROUND_DOWN")


def group_of(key: str) -> int:
    return next(i for i, (_, keys) in enumerate(GROUPS) if key in keys)


def human(key: str, value: str | None = None) -> str:
    """Value as an admin reads it: units, yes/no, text length."""
    v = get(key) if value is None else value
    kind = SPEC[key][1]
    if key in FLAGS:
        return {"receipt_images": {"1": "PDF и фото", "0": "только PDF"},
                "log_all": {"1": "все события", "0": "только проблемы"}}[key].get(v, v)
    if key == "online_minutes" and v == "0":
        return "выключено"
    if key == "adjust_approval_usdt" and Decimal(v) == 0:
        return "выключено"
    if kind == "pct":
        return f"{v}%"
    if kind in ("int", "int0"):
        if not key.endswith("minutes"):
            return v
        n = int(v)
        return f"{n // 60} ч" if n >= 60 and n % 60 == 0 else f"{n} мин"
    if kind == "int" and key.startswith("order_"):
        return f"{v} мин"
    if kind == "dec":
        return f"{v} ₽" if key in RATES or key.endswith("_rub") else f"{v} USDT"
    if kind == "html":
        return f"текст, {len(v)} симв."
    if kind == "url":
        return v.split("//", 1)[-1][:24] + "…" if v else "не задана"
    if kind == "chat":
        return v or "не задан"
    return f"@{v}" if v else "не задан"


def get(key: str) -> str:
    return _cache.get(key, SPEC[key][0])


def dec(key: str) -> Decimal:
    return Decimal(get(key))


def num(key: str) -> int:
    return int(get(key))


async def put(s: AsyncSession, key: str, value: str) -> None:
    await s.merge(Setting(key=key, value=value))
    _cache[key] = value


def validate(key: str, raw: str) -> str:
    """Return normalized value or raise ValueError with a human message."""
    kind = SPEC[key][1]
    raw = raw.strip()
    if kind in ("dec", "pct"):
        try:
            v = Decimal(raw.replace(",", "."))
        except Exception:
            raise ValueError("Введите число")
        if not v.is_finite() or v < 0 or (kind == "pct" and v >= 100) or (key in RATES and v == 0):
            raise ValueError("Недопустимое значение")
        places = 3 if kind == "pct" else 2 if key in RATES else 6
        if v.as_tuple().exponent < -places or v >= Decimal("10000000"):
            raise ValueError(f"Слишком большая сумма или более {places} знаков после запятой")
        sp = v if key == "seller_pct" else dec("seller_pct")
        pp = v if key == "platform_pct" else dec("platform_pct")
        rate = v if key == "rate" else dec("rate")
        orate = v if key == "order_rate" else dec("order_rate")
        if key in ("seller_pct", "platform_pct") and pp < sp:
            raise ValueError("Процент площадки должен быть ≥ проценту мерчанта со статичной картой")
        if key in ("rate", "platform_pct", "order_rate") and orate > order_rate_cap(rate, pp):
            cap = order_rate_cap(rate, pp)
            if key == "order_rate":
                raise ValueError(f"Курс ордерного мерчанта не может быть выше {cap} ₽ при курсе {rate} ₽ и комиссии "
                                 f"{pp}%: площадка доплачивала бы покупателю из своих")
            raise ValueError(f"Курс ордерного мерчанта {orate} ₽ тогда не может быть выше {cap} ₽: площадка "
                             f"доплачивала бы покупателю. Сначала снизьте «Курс ордерного мерчанта»")
        return format(v.normalize(), "f")
    if kind in ("int", "int0"):
        low = 0 if kind == "int0" else 1
        if not raw.isdigit() or not low <= int(raw) <= 1440:
            raise ValueError(f"Введите целое число от {low} до 1440")
        if key in ("receipt_images", "log_all") and int(raw) > 1:
            raise ValueError("Введите 1 или 0")
        return str(int(raw))
    if kind == "url":
        if raw == "-":
            return ""
        if not raw.startswith("https://") or len(raw) > 300 or any(c.isspace() for c in raw) or "\"" in raw:
            raise ValueError("Нужна ссылка https://… без пробелов, до 300 символов")
        return raw
    if kind == "chat":
        if raw == "-":
            return ""
        if not raw.lstrip("-").isdigit() or not raw.startswith("-"):
            raise ValueError("Нужен числовой ID группы, начинается с «-», например -1001234567890")
        return raw
    if key == "support":
        raw = raw.lstrip("@")
        if raw == "-":
            return ""
        if not 5 <= len(raw) <= 32 or not all(c.isascii() and (c.isalnum() or c == "_") for c in raw):
            raise ValueError("Некорректный ник")
    if key == "tutorial" and len(raw) > 3000:
        raise ValueError("Туториал не длиннее 3000 символов")
    if not raw:
        raise ValueError("Пустое значение")
    return raw
