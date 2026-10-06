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
    "withdraw_pct": ("1.5", "pct", "Комиссия вывода, %"),
    "deal_minutes": ("30", "int", "Время на оплату сделки"),
    "buyer_max_open": ("5", "int", "Неоплаченных сделок у покупателя"),
    "confirm_minutes": ("30", "int", "Покупатель может открыть спор через"),
    "escalate_minutes": ("1440", "int", "Автоспор, если продавец молчит"),
    "late_hold_minutes": ("30", "int", "Удержание залога после истечения"),
    "late_minutes": ("720", "int", "Приём позднего чека после срока"),
    "adjust_approval_usdt": ("0", "dec", "Второй админ для корректировок от"),
    "withdraw_turnover": ("1", "int0", "Вывод только прокрученного"),
    "online_minutes": ("60", "int0", "Автоконец смены без действий"),
    "receipt_images": ("0", "int0", "Формат чеков"),
    "log_all": ("1", "int0", "Лог-чат"),
    "support": ("", "text", "Ник поддержки"),
    "manager": ("", "text", "Ник менеджера для вопросов"),
    "manual_url": ("https://telegra.ph/Strait-Pay--P2P-obmen-USDT--RUB-v-Telegram-09-27", "url", "Ссылка на инструкцию"),
    "order_min_rub": ("1000", "dec", "Ордер: минимальная сумма"),
    "order_max_rub": ("1000000", "dec", "Ордер: максимальная сумма"),
    "order_search_minutes": ("15", "int", "Ордер: поиск мерчанта"),
    "order_take_minutes": ("10", "int", "Ордер: мерчанту на выдачу реквизитов"),
    "order_pay_minutes": ("15", "int", "Ордер: минимальное время на оплату"),
    "order_check_minutes": ("15", "int", "Ордер: оператору на Bybit-ордер"),
    "order_link_minutes": ("5", "int", "Ордер: мерчанту на ссылку Bybit"),
    "card_min_rub": ("2000", "dec", "Мин. карты в потоке"),
    "operator_max_debt": ("1000", "dec", "Оператор: предел долга"),
    "abandon_limit": ("3", "int0", "Брошенных сделок за сутки до паузы"),
    "abandon_pause_minutes": ("120", "int", "Пауза за брошенные сделки, мин"),
    "card_parallel": ("3", "int", "Сделок на одну карту сразу"),
    "order_first_wave": ("5", "int0", "Заявка сначала лучшим: мерчантов"),
    "order_wave_seconds": ("45", "int", "Заявка лучшим: секунд до всех"),
    "strike_limit": ("3", "int", "Ордер: пропусков реквизитов до паузы"),
    "strike_sleep_hours": ("72", "int", "Ордер: пауза мерчанта, часов"),
    "rep_min_count": ("10", "int", "Репутация: оценок до расчёта"),
    "rep_low": ("5", "dec", "Репутация: ниже — без Bybit-ордеров"),
    "rep_mid": ("7", "dec", "Репутация: ниже — Bybit с лимитом"),
    "rep_mid_max_rub": ("30000", "dec", "Репутация: лимит Bybit-заявки"),
    "chain_withdraw_min": ("3", "dec", "Минимальный вывод"),
    "chain_withdraw_fee": ("1", "dec", "Фикс. комиссия вывода"),
    "ton_sweep_min": ("5", "dec", "Сбор с адресов пополнения от"),
    "team_pct": ("1", "pct", "Тимлиду от сделок команды"),
    "chat_id": ("", "chat", "Чат сообщества"),
    "channel_id": ("", "chat", "Инфо-канал"),
    "join_required": ("1", "int0", "Вступление в чат и канал"),
    "channel_autopost_hours": ("24", "int0", "Автопост в канал, часов"),
    "signup_review": ("1", "int0", "Вход новых пользователей"),
    "docs_url": ("https://straitpay.best/docs", "url", "Сайт с инструкциями"),
    "webapp_url": ("", "url", "Мини-приложение"),
    "webapp_link": ("", "url", "Приложение: t.me-ссылка"),
    "tutorial": (
        "<b>Купить</b>: «RUB ⇄ USDT» → сумма в рублях → перевод по реквизитам → PDF-чек. USDT придут после "
        "подтверждения.\n"
        "<b>Продать</b>: пополните кошелёк, добавьте карту, выйдите на смену, подтверждайте поступления — или берите "
        "заявки под сумму как ордерный мерчант.\n"
        "<b>Кошелёк</b>: пополнение на личный адрес USDT в сети TON, вывод на любой TON-кошелёк или биржу.",
        "html",
        "Текст «Как это работает»",
    ),
}

# admin panel sections: (title, keys); every SPEC key is in exactly one section
GROUPS: list[tuple[str, list[str]]] = [
    ("Курс и комиссии", ["rate", "order_rate", "seller_pct", "platform_pct", "deposit_fee", "withdraw_pct",
                         "chain_withdraw_fee", "team_pct"]),
    ("Сроки сделок", ["deal_minutes", "buyer_max_open", "abandon_limit", "abandon_pause_minutes", "card_parallel", "confirm_minutes", "escalate_minutes", "late_hold_minutes", "late_minutes",
                      "online_minutes"]),
    ("Кошелёк и лимиты", ["deposit_min", "chain_withdraw_min", "ton_sweep_min", "card_min_rub",
                          "adjust_approval_usdt", "withdraw_turnover"]),
    ("Правила и лог-чат", ["receipt_images", "log_all", "signup_review", "join_required"]),
    ("Тексты, поддержка, чат", ["support", "manager", "tutorial", "manual_url", "docs_url", "webapp_url", "webapp_link",
                                "chat_id", "channel_id",
                                "channel_autopost_hours"]),
    ("Ордерные реквизиты", ["order_min_rub", "order_max_rub", "order_search_minutes", "order_take_minutes",
                            "order_link_minutes", "order_pay_minutes", "order_check_minutes", "strike_limit",
                            "strike_sleep_hours", "rep_min_count", "rep_low", "rep_mid", "rep_mid_max_rub",
                            "operator_max_debt", "order_first_wave", "order_wave_seconds"]),
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
    "signup_review": "1 — новый пользователь заполняет заявку (роль, оборот, скриншот) и ждёт одобрения в лог-чате, "
                     "0 — бот открыт всем сразу.",
    "docs_url": "Адрес страниц с инструкциями (их отдаёт API-сервер бота: /docs/buy, /docs/sell …). «-» — без ссылок.",
    "webapp_url": "Адрес мини-приложения (https). Пусто — домен сайта с инструкциями + /app: приложение отдаёт тот же "
                  "сервер бота. Кнопки «Приложение» и меню у поля ввода открывают его.",
    "webapp_link": "t.me-ссылка на приложение для лог-чата и групп (там нельзя кнопку web_app), например "
                   "https://t.me/straitpay_bot/app. Пусто — Main Mini App бота, если он включён в BotFather.",
    "card_min_rub": "Карту можно поставить в поток, только если её максимум и свободный баланс мерчанта покрывают "
                    "сделку не меньше этой суммы. Остальные карты бот снимает с потока сам.",
    "operator_max_debt": "Долг оператора плюс USDT ордеров у него в работе не больше этой суммы — иначе новые ордера "
                         "он не принимает, пока не погасит. 0 — без предела (площадка рискует этой суммой).",
    "abandon_limit": "Сделка истекла без чека — это брошенная сделка: она держала карту продавца и его USDT. Столько "
                     "брошенных за сутки — и покупатель (или плательщик API-клиента) на паузе. 0 — без ограничения.",
    "card_parallel": "Сколько сделок одновременно может идти по одной карте. Суммы сделок на карте различаются хотя бы на "
                     "1 ₽ — продавец отличает переводы по сумме.",
    "order_first_wave": "Сколько лучших мерчантов (репутация и надёжность) получают новую заявку первыми. 0 — всем сразу.",
    "order_wave_seconds": "Через сколько секунд заявка уходит всем остальным мерчантам и в чаты.",
    "order_rate": "Сколько рублей ордерный мерчант получает за 1 USDT: он отдаёт сумму заявки / этот курс USDT "
                  "(через Bybit-ордер или из баланса). Процента у ордерных мерчантов нет. Не выше курса сервиса / "
                  "(1 − процент площадки), иначе площадка доплачивала бы покупателю из своих.",
    "seller_pct": "Процент от суммы сделки по статичной карте, который получает мерчант. Не больше процента площадки.",
    "chat": "ID группы или канала, например <code>-1001234567890</code> (бот — админ с правом приглашать, "
            "закреплять и публиковать). «-» — отключить.",
    "join_required": "1 — без вступления в чат сообщества и подписки на инфо-канал (если они заданы) главное меню "
                     "не откроется; 0 — бот только предлагает вступить.",
    "order_link_minutes": "Сколько минут у мерчанта на ссылку Bybit-ордера после «Взять». Не успел — заявка не "
                          "считается взятой и уходит другим.",
    "strike_limit": "Сколько раз подряд мерчант может не дать реквизиты по своему ордеру (по ответу оператора), "
                    "прежде чем уйдёт на паузу.",
    "strike_sleep_hours": "На сколько часов мерчант уходит на паузу (не получает и не берёт заявки).",
    "buyer_max_open": "Сколько неоплаченных сделок покупатель может держать одновременно (каждая занимает карту "
                      "продавца). Оплаченные (с чеком) не считаются.",
    "withdraw_turnover": "1 — пополнение нельзя сразу вывести: выводится только баланс сверх непрокрученных "
                         "пополнений (пополнение уменьшается на USDT, ушедшие покупателям в завершённых сделках). "
                         "Купленное, доход тимлида и начисления админа выводятся сразу. 0 — выключено.",
    "channel_autopost_hours": "Раз во сколько часов бот публикует в инфо-канал промо-пост (по кругу: курсы, "
                              "безопасность, заработок на карте, Bybit-заявки, команды, покупка, вывод). 0 — выключено.",
    "rep_min_count": "После скольких оценок операторов (1–10) у мерчанта считается репутация и действуют ограничения.",
    "rep_low": "Средняя оценка ниже этой — мерчант берёт заявки только с баланса, без Bybit-ордеров.",
    "rep_mid": "Средняя оценка ниже этой (но не ниже нижней) — Bybit-заявки только до лимита суммы.",
    "rep_mid_max_rub": "Максимальная сумма Bybit-заявки для мерчанта со средней репутацией, ₽.",
    "url": "Ссылка https://… (например, на статью в Telegraph). «-» — убрать ссылку из бота.",
    "chain_withdraw_fee": "USDT сверх процента с каждого вывода. Покрывает газ: перевод USDT в сети TON стоит "
                          "около 0,05 TON, его платит горячий кошелёк.",
    "withdraw_pct": "Процент от суммы вывода. Удерживается из списываемой суммы.",
    "deposit_min": "Переводы меньше этой суммы не зачисляются на баланс (защита от пыли) — пользователь видит это "
                   "на экране пополнения.",
    "ton_sweep_min": "USDT копятся на личных адресах пополнения и собираются на горячий кошелёк, когда на адресе "
                     "набирается эта сумма: каждый сбор стоит ~0,1 TON газа. Зачисление на баланс — сразу, "
                     "независимо от сбора.",
    "team_pct": "Процент от суммы сделки (в USDT по курсу сделки), который тимлид получает с каждой завершённой "
                "сделки участника команды. Платит площадка из своего дохода по сделке, не больше него.",
    "deposit_fee": "Процент с каждого пополнения. Удерживается из поступившей суммы; погашение долга оператора — "
                   "без комиссии.",
}
FLAGS = ("receipt_images", "log_all", "signup_review", "join_required", "withdraw_turnover")
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


def buyer_terms(user=None, client=None) -> tuple[Decimal, Decimal]:
    """(rate, platform percent) a buyer is priced by: an API client's own terms, else the user's personal ones (an
    admin sets them in the profile), else the general rate / platform_pct."""
    if client is not None:
        return client_terms(client)
    rate = getattr(user, "buy_rate", None)
    pct = getattr(user, "buy_pct", None)
    return (rate if rate is not None else dec("rate"), pct if pct is not None else dec("platform_pct"))


def has_terms(user) -> bool:
    return user is not None and (user.buy_rate is not None or user.buy_pct is not None)


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
                "log_all": {"1": "все события", "0": "только проблемы"},
                "signup_review": {"1": "по заявке", "0": "открыт всем"},
                "join_required": {"1": "обязательно", "0": "по желанию"},
                "withdraw_turnover": {"1": "включено", "0": "выключено"}}[key].get(v, v)
    if key == "online_minutes" and v == "0":
        return "выключено"
    if key == "adjust_approval_usdt" and Decimal(v) == 0:
        return "выключено"
    if kind == "pct":
        return f"{v}%"
    if kind in ("int", "int0"):
        if key.endswith("_hours"):
            return f"{v} ч"
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


def raw(key: str) -> str:
    """A stored value that is not an admin setting (the log chat, granted admins...): "" if not set."""
    return _cache.get(key, "")


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
        if key in FLAGS and int(raw) > 1:
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
    if key in ("support", "manager"):
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
