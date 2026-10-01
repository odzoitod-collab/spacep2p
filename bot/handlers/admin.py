import re
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.handlers.deal import STATUS, dispute_kb, dispute_text, push, send_files, verdict_effects
from bot.handlers.seller import card_icon, card_label
from bot.handlers.wallet import ledger_line, parse_usdt
from bot.models import Adjustment, ApiApplication, Card, OrderMerchant, Deal, Deposit, Event, Ledger, Ticket, User, Withdrawal, now
from bot.services import audit, deals, events, money, orders, settings, xrocket
from bot.ui import at, esc, notify, ok, quote, show, title, warn

router = Router()
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))


class Adm(StatesGroup):
    setting = State()
    search = State()
    balance = State()
    comment = State()
    message = State()
    reply = State()
    verdict = State()
    pct = State()


ADJ_REASONS = {"deposit_fix": "Исправление пополнения", "compensation": "Компенсация", "refund": "Возврат",
               "tech": "Техническая корректировка", "other": "Другое"}
ADJ_STATUS = {"draft": "черновик", "pending": "ждёт второго администратора", "done": "проведена",
              "cancelled": "отменена", "failed": "отклонена: не хватило доступного баланса"}


# ---------- dashboard ----------

@router.callback_query(F.data == "a")
async def cb_admin(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await admin_screen(bot, s, user, c)


async def admin_screen(bot: Bot, s: AsyncSession, user: User, src=None):
    day = now() - timedelta(hours=24)
    count = lambda *where: s.scalar(select(func.count()).where(*where))  # noqa: E731
    users = await s.scalar(select(func.count(User.id)))
    online = await count(User.is_online)
    cards = await count(Card.is_active, ~Card.is_banned, ~Card.is_deleted)
    done24, volume24 = (await s.execute(
        select(func.count(Deal.id), func.coalesce(func.sum(Deal.amount_rub), 0))
        .where(Deal.status == "completed", Deal.closed_at > day))).one()
    opened = await count(Deal.status.in_(deals.OPEN))
    disputes = await count(Deal.status == "dispute")
    slow = await count(Deal.status == "paid",
                       Deal.paid_at < now() - timedelta(minutes=settings.num("confirm_minutes")))
    unknown = await count(Withdrawal.status.in_(("unknown", "pending")))
    tickets = await count(Ticket.status == "open")
    approvals = await count(Adjustment.status == "pending")
    api_apps = await count(ApiApplication.status == "pending")
    om_apps = await count(OrderMerchant.status == "pending")
    searching = await count(Deal.status.in_(orders.REQUEST))
    backlog = await count(Event.alert, Event.sent_at.is_(None))
    income24 = await s.scalar(select(func.coalesce(func.sum(Ledger.delta), 0))
                              .where(Ledger.user_id.is_(None), Ledger.created_at > day))
    income = await s.scalar(select(func.coalesce(func.sum(Ledger.delta), 0)).where(Ledger.user_id.is_(None)))
    held = Decimal(await s.scalar(select(func.coalesce(func.sum(User.balance + User.frozen), 0))))
    solvency = ""
    try:
        available = await xrocket.usdt_available(max_age=60, timeout=3)
        rocket = f"<b>{money.usdt(available)} USDT</b>"
        if available < held:
            solvency = warn(f"В xRocket на {money.usdt(held - available)} USDT меньше, чем на балансах пользователей: "
                            "выводы могут не пройти.")
    except Exception:
        rocket = "недоступно"
    todo = [(disputes, f"{pe('flag')} Споров: <b>{disputes}</b>"),
            (slow, f"{pe('clock')} Продавец молчит дольше {settings.get('confirm_minutes')} мин: <b>{slow}</b>"),
            (unknown, f"{pe('up')} Выводов на проверке: <b>{unknown}</b>"),
            (tickets, f"{pe('support')} Открытых обращений: <b>{tickets}</b>"),
            (approvals, f"{pe('dollar')} Корректировок ждут подтверждения: <b>{approvals}</b>"),
            (api_apps, f"{pe('key')} Заявок на API: <b>{api_apps}</b>"),
            (om_apps, f"{pe('key')} Анкет ордерных мерчантов: <b>{om_apps}</b>"),
            (searching, f"{pe('search')} Ордерных заявок ищут реквизиты: <b>{searching}</b>"),
            (backlog, f"{pe('warn')} Недоставленных уведомлений: <b>{backlog}</b>")]
    active = [line for n, line in todo if n]
    await show(bot, user, "\n".join([
        title(pe("settings"), "Админ-панель"),
        "",
        title(pe("bell"), "Требует внимания"),
        quote(*active) if active else f"{pe('ok')} Очереди пусты",
        title(pe("stats"), "Сводка"),
        quote(
            f"{pe('people')} Пользователей: <b>{users}</b> · на смене: <b>{online}</b> · карт в работе: <b>{cards}</b>",
            f"{pe('ok')} За 24 ч: <b>{done24}</b> сделок на <b>{money.fmt(Decimal(volume24))} ₽</b>, "
            f"доход <b>{money.usdt(Decimal(income24))} USDT</b> · открыто сейчас: {opened}",
            f"{pe('up')} Доход площадки всего: <b>{money.usdt(Decimal(income))} USDT</b>",
            f"{pe('lock')} Балансы пользователей: <b>{money.usdt(held)} USDT</b> · xRocket: {rocket}",
        ),
    ]) + solvency, kb(
        btn(f"Споры ({disputes})", "adl:dispute", "flag", style="danger") if disputes else None,
        btn(f"Продавец молчит ({slow})", "adl:slow", "clock") if slow else None,
        btn(f"Выводы на проверке ({unknown})", "awl:check", "up", style="danger") if unknown else None,
        btn(f"Обращения ({tickets})", "atl", "support", style="primary") if tickets else None,
        btn(f"Корректировки ({approvals})", "aadjl", "dollar", style="primary") if approvals else None,
        btn(f"Заявки на API ({api_apps})", "aapi", "key", style="primary") if api_apps else None,
        btn(f"Анкеты мерчантов ({om_apps})", "aoml", "key", style="primary") if om_apps else None,
        [btn("Найти", "au", "search"), btn("Сделки", "ad", "list")],
        [btn("Ввод и вывод", "al", "wallet"), btn("Карты", "ac:0", "card")],
        [btn("Отчёты CSV", "arp", "doc"), btn("Обращения", "atl", "support")],
        [btn("USDT TON", "atn", "wallet"), btn("API для сервисов", "aapi", "key")],
        btn("Финансы: сколько можно забрать", "afin", style="success"),
        [btn("Комиссии", "acm", "percent"), btn("Ордерные мерчанты", "aoml", "key")],
        [btn("Настройки", "as", "settings"), btn("Журнал", "aa", "list")],
        [btn("Обновить", "a", "refresh"), back("menu", "В меню")],
    ), src)


# ---------- settings ----------

async def settings_screen(bot: Bot, user: User, src=None, note: str = ""):
    rate, sp, pp = settings.dec("rate"), settings.dec("seller_pct"), settings.dec("platform_pct")
    q = money.quote(Decimal(10000), rate, sp, pp)
    qo = money.quote_fixed(Decimal(10000), rate, settings.dec("order_rate"), pp)
    lines = [
        title(pe("settings"), "Настройки"),
        "",
        quote(f"{pe('swap')} Сделка на 10 000 ₽, покупатель получает {money.usdt(q.buyer_credit)} USDT:",
              f"{pe('card')} статичная карта: мерчант отдаёт {money.usdt(q.seller_debit)}, площадке "
              f"{money.usdt(q.platform_fee)} USDT",
              f"{pe('key')} ордерные реквизиты (курс {money.fmt(settings.dec('order_rate'))} ₽): мерчант отдаёт "
              f"{money.usdt(qo.seller_debit)}, площадке {money.usdt(qo.platform_fee)} USDT"),
    ]
    for name, keys in settings.GROUPS:
        lines += [f"<b>{name}</b>", quote(*[f"{settings.SPEC[k][2]}: <b>{esc(settings.human(k))}</b>" for k in keys])]
    lines.append("Открытые сделки сохраняют условия, с которыми были созданы. Выберите раздел:")
    await show(bot, user, "\n".join(lines) + note, kb(
        *[btn(name, f"asg:{i}", "pencil") for i, (name, _) in enumerate(settings.GROUPS)],
        back("a", "Админ-панель"),
    ), src)


async def group_screen(bot: Bot, user: User, idx: int, src=None, note: str = ""):
    name, keys = settings.GROUPS[idx]
    await show(bot, user, f"{title(pe('settings'), name)}\n\nНажмите на параметр, чтобы изменить." + note, kb(
        *[btn(f"{settings.SPEC[k][2]}: {settings.human(k)}", f"as:{k}", "pencil") for k in keys],
        back("as", "Все настройки"),
    ), src)


@router.callback_query(F.data == "as")
async def cb_settings(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(None)
    await settings_screen(bot, user, c)


@router.callback_query(F.data.regexp(r"^asg:(\d+)$"))
async def cb_settings_group(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(None)
    idx = int(c.data.split(":")[1])
    if idx >= len(settings.GROUPS):
        return await c.answer()
    await group_screen(bot, user, idx, c)


def _setting_text(key: str, err: str = "") -> str:
    kind = settings.SPEC[key][1]
    current = settings.get(key) if kind == "html" else esc(settings.get(key))
    return "\n".join([
        title(pe("pencil"), settings.SPEC[key][2]),
        "",
        quote(f"Сейчас: <b>{esc(settings.human(key))}</b>", current if kind == "html" else "",
              f"По умолчанию: {esc(settings.human(key, settings.SPEC[key][0]))}"),
        "",
        f"Отправьте новое значение. {settings.HINTS.get(key) or settings.HINTS.get(kind, '')}",
    ]) + (warn(err) if err else "")


def _ret(data: dict, key: str) -> str:
    return data.get("ret") or f"asg:{settings.group_of(key)}"


RETURN = {"acs": "acm", "asr": "atn:sw"}  # editor opened from «Комиссии и проценты» / TON auto-transfer: back there


@router.callback_query(F.data.startswith("as:") | F.data.startswith("acs:") | F.data.startswith("asr:"))
async def cb_setting(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    """as:<key> from Settings; acs:<key> / asr:<key> return to their screen after saving."""
    prefix, key = c.data.split(":", 1)
    if key not in settings.SPEC:
        return await c.answer()
    await state.set_state(Adm.setting)
    await state.update_data(key=key, ret=RETURN.get(prefix))
    await show(bot, user, _setting_text(key), kb(back(_ret(await state.get_data(), key), "Отмена")), c)


@router.message(Adm.setting)
async def msg_setting(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    key = data["key"]
    raw = m.html_text if settings.SPEC[key][1] == "html" else m.text
    try:
        value = settings.validate(key, raw or "")
    except ValueError as e:
        return await show(bot, user, _setting_text(key, str(e)), kb(back(_ret(data, key), "Отмена")))
    old = settings.get(key)
    await settings.put(s, key, value)
    audit.log(s, user.id, "setting", key, f"{old} → {value}"[:2000])
    if key == "ton_sweep_address":  # where the money goes: every change is visible to all admins
        events.add(s, "app:ton", "target_changed", f"Адрес автоперевода USDT TON изменён администратором "
                   f"{user.name} ({user.id}): {old or '—'} → {value or '—'}", user.id, alert=True)
    await state.set_state(None)
    note = ok(f"Сохранено: {esc(settings.human(key, old))} → {esc(settings.human(key))}")
    if data.get("ret") == "acm":
        return await commissions_screen(bot, s, user, note=note)
    if data.get("ret") == "atn:sw":
        from bot.handlers.admin_ton import ton_sweep_screen
        return await ton_sweep_screen(bot, s, user, note=note)
    await group_screen(bot, user, settings.group_of(key), note=note)


# ---------- commissions and merchant percents ----------

@router.callback_query(F.data == "acm")
async def cb_commissions(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await commissions_screen(bot, s, user, c)


async def commissions_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    rate, pp = settings.dec("rate"), settings.dec("platform_pct")
    sp, orate = settings.dec("seller_pct"), settings.dec("order_rate")
    q, qo = money.quote(Decimal(10000), rate, sp, pp), money.quote_fixed(Decimal(10000), rate, orate, pp)
    personal = (await s.scalars(select(User).where(User.pct_static.is_not(None)).order_by(User.id).limit(20))).all()
    pct = lambda v: f"{money.fmt(v, 3)}%"  # noqa: E731
    await show(bot, admin, "\n".join([
        title(pe("percent"), "Комиссии и проценты"),
        "",
        title(pe("swap"), "Сделки"),
        quote(f"{pe('dollar')} Покупатель платит: <b>{pct(pp)}</b> · курс {money.fmt(rate)} ₽",
              f"{pe('card')} Мерчант, статичная карта: <b>{pct(sp)}</b> → площадке <b>{pct(pp - sp)}</b>",
              f"{pe('key')} Ордерный мерчант: фиксированный курс <b>{money.fmt(orate)} ₽</b> за USDT, без процента "
              f"(не выше {money.fmt(settings.order_rate_cap(rate, pp))} ₽)"),
        f"На 10 000 ₽ покупатель получает {money.usdt(q.buyer_credit)} USDT; площадке {money.usdt(q.platform_fee)} "
        f"(карта) или {money.usdt(qo.platform_fee)} USDT (ордер: мерчант отдаёт {money.usdt(qo.seller_debit)}).",
        title(pe("wallet"), "Кошелёк"),
        quote(f"{pe('down')} Пополнение xRocket: {settings.get('deposit_fee')}% · USDT TON: без комиссии",
              f"{pe('up')} Вывод чеком xRocket: {settings.human('withdraw_fee')} · на TON: "
              f"{settings.human('ton_withdraw_fee')}"),
        title(pe("star"), f"Личные ставки мерчантов ({len(personal)})"),
        quote(*[f"<code>{u.id}</code> {esc((u.name or '—')[:20])}: карта {pct(u.pct_static)}" for u in personal])
        if personal else "Нет — все работают по общим ставкам. Задать: профиль пользователя → «Проценты мерчанта».",
        "Ставка фиксируется в сделке при её создании: изменения не трогают открытые сделки.",
    ]) + note, kb(
        [btn("Покупатель", "acs:platform_pct", "dollar"), btn("Курс", "acs:rate", "swap")],
        [btn("Мерчант: карта", "acs:seller_pct", "card"), btn("Курс ордерного", "acs:order_rate", "key")],
        [btn("Пополнение", "acs:deposit_fee", "down"), btn("Вывод чеком", "acs:withdraw_fee", "up")],
        btn("Вывод на TON", "acs:ton_withdraw_fee", "up"),
        *[btn(f"Личная ставка · {u.id} {(u.name or '')[:16]}", f"aup:{u.id}", "star") for u in personal],
        back("a", "Админ-панель"),
    ), src)


def _pct_text(u: User, err: str = "") -> str:
    pct = lambda v: f"{money.fmt(v, 3)}%"  # noqa: E731
    return "\n".join([
        title(pe("star"), f"Проценты мерчанта · {u.id}"),
        f"{esc(u.name or '—')} @{esc(u.username or '—')}",
        "",
        quote(f"{pe('card')} Статичная карта: <b>{pct(settings.merchant_pct(u))}</b>"
              + (" (личная)" if u.pct_static is not None else " (общая)"),
              f"{pe('key')} Ордерные реквизиты: фиксированный курс {money.fmt(settings.dec('order_rate'))} ₽, "
              "без процента",
              f"{pe('dollar')} Покупатель платит: {pct(settings.dec('platform_pct'))} — личная ставка выше не действует"),
        "Личная ставка — для надёжных мерчантов с большим объёмом. Применяется к новым сделкам.",
    ]) + (warn(err) if err else "")


@router.callback_query(F.data.regexp(r"^aup:(\d+)$"))
async def cb_user_pct(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    u = await s.get(User, int(c.data.split(":")[1]))
    if not u:
        return await c.answer("Пользователь не найден", show_alert=True)
    await show(bot, user, _pct_text(u), _pct_kb(u), c)


def _pct_kb(u: User):
    return kb(btn("Ставка по карте", f"aup:set:{u.id}:s", "card"),
              btn("Сбросить на общую", f"aup:rs:{u.id}", "refresh") if u.pct_static is not None else None,
              btn("Все ставки", "acm", "percent"),
              back(f"auv:{u.id}", "Профиль"))


@router.callback_query(F.data.regexp(r"^aup:set:(\d+):(s)$"))
async def cb_user_pct_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, uid, which = c.data.split(":")
    u = await s.get(User, int(uid))
    await state.set_state(Adm.pct)
    await state.set_data({"uid": u.id, "which": which})
    await show(bot, user, _pct_text(u) + "\n\nОтправьте личный процент по статичной карте"
                                         " (например, <code>5.5</code>) или «-», чтобы вернуть общий.",
               kb(back(f"aup:{u.id}", "Отмена")), c)


@router.message(Adm.pct, F.text)
async def msg_user_pct(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    u = await s.get(User, data["uid"])
    raw = m.text.strip().replace(",", ".")
    value = None
    if raw != "-":
        try:
            value = Decimal(raw)
        except Exception:  # noqa: BLE001
            value = Decimal(-1)
        if not value.is_finite() or not 0 <= value < settings.dec("platform_pct") or value.as_tuple().exponent < -3:
            return await show(bot, user, _pct_text(u, f"Число от 0 до {money.fmt(settings.dec('platform_pct'), 3)} "
                                                      "(меньше процента покупателя), до 3 знаков"),
                              kb(back(f"aup:{u.id}", "Отмена")))
    await state.clear()
    old, u.pct_static = u.pct_static, value
    audit.log(s, user.id, "merchant_pct", f"user:{u.id}", f"pct_static: {old} → {value}")
    events.add(s, f"user:{u.id}", "pct", f"Личная ставка по статичной карте: {old if old is not None else 'общая'} → "
               f"{value if value is not None else 'общая'} ({user.name})", u.id, alert=True)
    await notify(bot, u.id, f"{pe('star')} <b>Ваш процент по статичной карте: {money.fmt(settings.merchant_pct(u), 3)}%</b>"
                            " — действует для новых сделок.")
    await show(bot, user, _pct_text(u) + ok("Сохранено"), _pct_kb(u))


@router.callback_query(F.data.regexp(r"^aup:rs:(\d+)$"))
async def cb_user_pct_reset(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    u = await s.get(User, int(c.data.split(":")[2]))
    u.pct_static = None
    audit.log(s, user.id, "merchant_pct", f"user:{u.id}", "reset")
    events.add(s, f"user:{u.id}", "pct", f"Личная ставка сброшена на общую ({user.name})", u.id, alert=True)
    await notify(bot, u.id, f"{pe('star')} Ваш процент по статичной карте вернулся к общему: "
                            f"{money.fmt(settings.merchant_pct(u), 3)}%.")
    await show(bot, user, _pct_text(u) + ok("Сброшено на общую"), _pct_kb(u), c)


# ---------- search & profile ----------

@router.callback_query(F.data == "au")
async def cb_users(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(Adm.search)
    last = (await s.scalars(select(User).order_by(User.created_at.desc()).limit(6))).all()
    await show(bot, user, "\n".join([
        title(pe("search"), "Поиск"),
        "",
        "Отправьте одно из:",
        quote("<code>123456789</code> — Telegram ID, <code>@username</code>",
              "<code>#15</code> — сделка",
              "<code>в482</code> — вывод, <code>п12</code> — пополнение (открывается профиль владельца)",
              "<code>2200…</code> или <code>+79…</code> — карта/телефон полностью"),
        "Последние регистрации:",
    ]), kb(
        *[btn(f"{u.name[:20] or u.id} · @{u.username or '—'}", f"auv:{u.id}", "ban" if u.is_banned else "profile")
          for u in last],
        back("a", "Админ-панель"),
    ), c)


OP_PREFIX = {"в": "wd", "w": "wd", "п": "dep", "d": "dep"}


@router.message(Adm.search, F.text)
async def msg_search(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    q = m.text.strip()
    low = q.lower()
    if q.startswith("#") and q[1:].isdigit():
        if d := await s.get(Deal, int(q[1:])):
            await state.set_state(None)
            return await deal_view(bot, s, user, d)
    if low[:1] in OP_PREFIX and low[1:].isdigit():
        kind, oid = OP_PREFIX[low[0]], int(low[1:])
        op = await s.get(Withdrawal if kind == "wd" else Deposit, oid)
        if op:
            await state.set_state(None)
            return await user_screen(bot, s, user, await s.get(User, op.user_id), found=(kind, op))
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 11 and not q.startswith("@"):
        req = ("+" + ("7" + digits[1:] if digits[0] == "8" else digits)) if len(digits) == 11 else digits
        card = await s.scalar(select(Card).where(Card.requisites == req).order_by(Card.id.desc()).limit(1))
        if card:
            await state.set_state(None)
            return await admin_card_screen(bot, s, user, card)
    name = q.lstrip("@")
    found = await s.scalar(select(User).where(
        User.id == int(name) if name.isdigit() else func.lower(User.username) == name.lower()))
    if not found:
        return await show(bot, user, title(pe("search"), "Поиск") + "\n\nОтправьте ID, @username, #сделку, в/п+номер "
                          "или реквизиты." + warn(f"Ничего не найдено: {esc(q[:40])}"), kb(back("a", "Админ-панель")))
    await state.set_state(None)
    await user_screen(bot, s, user, found)


def _who(u: User) -> str:
    return f"{esc(u.name or '—')} (<code>{u.id}</code>)"


OUTCOME = {"buyer_cancel": "отменил покупатель", "expired": "истёк срок", "admin_void": "отменил админ",
           "ban_void": "бан участника", "dispute_seller": "спор в пользу продавца",
           "dispute_buyer": "спор в пользу покупателя", "dispute_actual": "спор по факт. сумме"}


async def user_stats(s: AsyncSession, uid: int) -> dict:
    out = {}
    for role, col in (("buy", Deal.buyer_id), ("sell", Deal.seller_id)):
        rows = (await s.execute(select(Deal.status, Deal.close_reason, func.count(Deal.id),
                                       func.coalesce(func.sum(Deal.amount_rub), 0))
                                .where(col == uid).group_by(Deal.status, Deal.close_reason))).all()
        done = [r for r in rows if r[0] == "completed"]
        outcomes = {}
        for status, reason, n, _ in rows:
            if status in ("cancelled", "expired", "void") or reason in ("dispute_buyer", "dispute_actual"):
                key = reason or status
                outcomes[key] = outcomes.get(key, 0) + n
        out[role] = (sum(r[2] for r in done), Decimal(sum(Decimal(r[3]) for r in done)), outcomes,
                     sum(r[2] for r in rows if r[0] in deals.OPEN))
    mine = or_(Deal.buyer_id == uid, Deal.seller_id == uid)
    out["disputes"] = (await s.scalar(select(func.count(Deal.id)).where(mine, Deal.status == "dispute")),
                       await s.scalar(select(func.count(Deal.id)).where(mine, Deal.dispute_reason.is_not(None))))
    out["deposited"] = Decimal(await s.scalar(select(func.coalesce(func.sum(Ledger.delta), 0))
                                              .where(Ledger.user_id == uid, Ledger.kind == "deposit")))
    out["withdrawn"] = Decimal(await s.scalar(select(func.coalesce(func.sum(Withdrawal.amount - Withdrawal.fee), 0))
                                              .where(Withdrawal.user_id == uid, Withdrawal.status == "done")))
    out["checking"] = await s.scalar(select(func.count(Withdrawal.id)).where(
        Withdrawal.user_id == uid, Withdrawal.status.in_(("unknown", "pending"))))
    return out


def _role_line(label: str, data: tuple) -> str:
    n, volume, outcomes, opened = data
    extra = " · ".join(f"{OUTCOME.get(k, k)} {v}" for k, v in outcomes.items())
    return (f"{label}: <b>{n}</b> завершено на <b>{money.fmt(volume)} ₽</b>" + (f" · открыто {opened}" if opened else "")
            + (f"\n   {extra}" if extra else ""))


WD_LABEL = {"queued": "ждёт средств xRocket", "cancelled": "отменён пользователем", "pending": "отправляется",
            "sending": "отправляется", "sent": "в сети", "unknown": "требует проверки",
            "done": "выполнен", "failed": "не выполнен"}
DEP_LABEL = {"new": "создаётся", "active": "ждёт оплаты", "paid": "зачислен", "expired": "истёк", "failed": "не создан"}


async def user_screen(bot: Bot, s: AsyncSession, admin: User, u: User, src=None, note: str = "", found=None):
    st = await user_stats(s, u.id)
    status = ("заблокирован" if u.is_banned else "активен") + (" · на смене" if u.is_online else "")
    head = []
    if found:
        kind, op = found
        head = [f"{pe('search')} Найдено: {'вывод' if kind == 'wd' else 'пополнение'} #{op.id} · "
                f"{(WD_LABEL if kind == 'wd' else DEP_LABEL).get(op.status, op.status)}", ""]
    await show(bot, admin, "\n".join(head + [
        title(pe("profile"), f"Пользователь · {u.id}"),
        f"Имя: {esc(u.name or '—')}",
        f"@{esc(u.username or '—')} · зарегистрирован {at(u.created_at, 'd')}",
        f"Статус: {pe('ban') + ' ' if u.is_banned else ''}<b>{status}</b>",
        f"Последняя активность: {at(u.last_seen, 'dt')}",
        "",
        quote(f"{pe('dollar')} Доступно: <b>{money.usdt(u.balance)} USDT</b>",
              f"{pe('lock')} Заморожено: <b>{money.usdt(u.frozen)} USDT</b>"),
        quote(
            _role_line(f"{pe('down')} Покупки", st["buy"]),
            _role_line(f"{pe('up')} Продажи", st["sell"]),
            f"{pe('flag')} Споры: {st['disputes'][0]} открыто · {st['disputes'][1]} всего",
            f"{pe('wallet')} Пополнено: <b>{money.usdt(st['deposited'])} USDT</b> · "
            f"выведено чеками: <b>{money.usdt(st['withdrawn'])} USDT</b>"
            + (f" · на проверке: {st['checking']}" if st["checking"] else ""),
        ),
    ]) + note, kb(
        btn(f"{'Вывод' if found[0] == 'wd' else 'Пополнение'} #{found[1].id} · "
            f"{(WD_LABEL if found[0] == 'wd' else DEP_LABEL).get(found[1].status, found[1].status)}",
            f"{'awv' if found[0] == 'wd' else 'adp'}:{found[1].id}", "search", style="primary") if found else None,
        [btn("Сделки", f"aud:{u.id}", "fire"), btn("Операции", f"auh:{u.id}", "list")],
        [btn("Карты", f"auc:{u.id}", "card"), btn("Споры", f"aus:{u.id}", "flag")],
        [btn("Ввод и вывод", f"auw:{u.id}", "wallet"), btn("Написать", f"amsg:{u.id}", "support")],
        btn("Изменить баланс", f"aadj:{u.id}", "dollar", style="primary"),
        btn("Процент мерчанта" + (" · личный" if u.pct_static is not None else ""),
            f"aup:{u.id}", "star"),
        [btn("Снять со смены", f"auo:{u.id}", "pause") if u.is_online else None,
         btn("Разблокировать", f"aub:{u.id}:0", "ok", style="success") if u.is_banned
         else btn("Заблокировать", f"aub:{u.id}:1", "ban", style="danger")],
        [btn("Обновить", f"auv:{u.id}", "refresh"), back("au", "Назад")],
    ), src)


@router.callback_query(F.data.regexp(r"^auv:(\d+)$"))
async def cb_user(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    u = await s.get(User, int(c.data.split(":")[1]), populate_existing=True)
    if not u:
        return await c.answer("Пользователь не найден", show_alert=True)
    await user_screen(bot, s, user, u, c)


@router.callback_query(F.data.regexp(r"^aub:(\d+):1$"))
async def cb_ban_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    uid = int(c.data.split(":")[1])
    u = await s.get(User, uid)
    if not u or u.id in config.admin_ids:
        return await c.answer("Нельзя заблокировать администратора", show_alert=True)
    waiting = await s.scalar(select(func.count(Deal.id)).where(
        Deal.status == "waiting_payment", or_(Deal.buyer_id == uid, Deal.seller_id == uid)))
    paid = await s.scalar(select(func.count(Deal.id)).where(Deal.status == "paid", Deal.seller_id == uid))
    await show(bot, user, "\n".join([
        f"{pe('ban')} <b>Заблокировать {_who(u)}?</b>",
        "",
        quote("Пользователь не сможет пользоваться ботом; баланс сохранится.",
              "Все его карты будут выключены, смена завершена.",
              f"Сделок без оплаты будет отменено: <b>{waiting}</b> (участникам придёт уведомление).",
              f"Оплаченных сделок, где он продавец, уйдёт в спор: <b>{paid}</b>."),
    ]), kb([btn("Заблокировать", f"aub2:{uid}", "ban", style="danger"), back(f"auv:{uid}", "Отмена", "back")]), c)


@router.callback_query(F.data.regexp(r"^(aub2:\d+|aub:\d+:0)$"))
async def cb_ban(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    parts = c.data.split(":")
    banning = parts[0] == "aub2"
    uid = int(parts[1])
    u = await money.lock(s, uid) if await s.get(User, uid) else None
    if not u or u.id in config.admin_ids:
        return await c.answer("Нельзя", show_alert=True)
    if u.is_banned == banning:  # explicit target state: repeated or concurrent clicks are no-ops
        return await user_screen(bot, s, user, u, c, ok("Уже " + ("заблокирован" if banning else "разблокирован")))
    u.is_banned = banning
    cancelled, disputed = [], []
    if banning:
        u.is_online = False
        for card in (await s.scalars(select(Card).where(Card.user_id == u.id))).all():
            card.is_active = False
        cancelled, disputed = await deals.on_ban(s, u.id)
        for d in disputed:
            events.add(s, f"deal:{d.id}", "dispute", "Продавец заблокирован: сделка передана в спор", alert=True)
    audit.log(s, user.id, "ban" if banning else "unban", f"user:{uid}",
              f"cancelled={[d.id for d in cancelled]} disputed={[d.id for d in disputed]}")
    events.add(s, f"user:{uid}", "ban" if banning else "unban",
               (f"Заблокирован ({user.name}): отменено сделок {len(cancelled)}, в спор {len(disputed)}"
                if banning else f"Разблокирован ({user.name})"), uid, alert=True)
    await s.commit()
    sup = settings.get("support")
    await notify(bot, u.id, f"{pe('ban')} <b>Ваш аккаунт заблокирован.</b> Баланс сохранён."
                 + (f" Вопросы: @{esc(sup)}" if sup else "") if banning
                 else f"{pe('ok')} <b>Ваш аккаунт разблокирован.</b> Карты выключены — включите нужные в «Продать USDT».")
    for d in cancelled:
        other = d.buyer_id if d.seller_id == u.id else d.seller_id
        await push(bot, s, other, d, f"Сделка #{d.id} отменена: участник заблокирован. Не переводите деньги по ней")
    for d in disputed:
        await push(bot, s, d.buyer_id, d, f"Сделка #{d.id} передана администрации: продавец заблокирован")
    await user_screen(bot, s, user, u, c, ok(
        f"Заблокирован. Отменено сделок: {len(cancelled)}, в спор: {len(disputed)}" if banning else "Разблокирован"))


@router.callback_query(F.data.regexp(r"^auo:(\d+)$"))
async def cb_user_offline(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    u = await s.get(User, int(c.data.split(":")[1]))
    if u:
        u.is_online = False
        audit.log(s, user.id, "offline", f"user:{u.id}")
        await notify(bot, u.id, f"{pe('pause')} <b>Администратор завершил вашу смену.</b> Карты скрыты от покупателей.")
        await user_screen(bot, s, user, u, c, ok("Снят со смены"))


@router.callback_query(F.data.regexp(r"^auh:(\d+)$"))
async def cb_user_ledger(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    uid = int(c.data.split(":")[1])
    rows = (await s.scalars(select(Ledger).where(Ledger.user_id == uid).order_by(Ledger.id.desc()).limit(20))).all()
    total, frozen = (await s.execute(select(func.coalesce(func.sum(Ledger.delta), 0),
                                            func.coalesce(func.sum(Ledger.frozen_delta), 0))
                                     .where(Ledger.user_id == uid))).one()
    u = await s.get(User, uid)
    ok_total, ok_frozen = Decimal(total) == u.balance + u.frozen, Decimal(frozen) == u.frozen
    check = (f"{pe('ok')} Журнал сходится: доступно {money.usdt(u.balance)} + заморожено {money.usdt(u.frozen)} USDT"
             if ok_total and ok_frozen else
             f"{pe('warn')} Расхождение: журнал {money.usdt(Decimal(total))} / {money.usdt(Decimal(frozen))}, "
             f"баланс {money.usdt(u.balance + u.frozen)} / {money.usdt(u.frozen)} USDT")
    await show(bot, user, "\n".join([title(pe("list"), f"Операции · {u.id}"), "Последние 20, новые сверху.", "",
                                     quote(*[ledger_line(r) for r in rows]) if rows else "Операций нет", check]),
               kb(back(f"auv:{uid}", "Профиль")), c)


async def _deal_list(bot, s, user, c, uid: int, heading: str, where):
    rows = (await s.scalars(select(Deal).where(or_(Deal.buyer_id == uid, Deal.seller_id == uid), *where)
                            .order_by(Deal.id.desc()).limit(20))).all()
    await show(bot, user, f"{title(pe('fire'), heading)} · <code>{uid}</code>\n↓ — покупка, ↑ — продажа"
               + ("" if rows else "\n\nПусто"), kb(
        *[btn(f"{'↓' if d.buyer_id == uid else '↑'} #{d.id} · {money.fmt(d.amount_rub)} ₽ · {STATUS[d.status][1]}",
              f"adv:{d.id}", STATUS[d.status][0]) for d in rows],
        back(f"auv:{uid}", "Профиль"),
    ), c)


@router.callback_query(F.data.regexp(r"^aud:(\d+)$"))
async def cb_user_deals(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    await _deal_list(bot, s, user, c, int(c.data.split(":")[1]), "Сделки", ())


@router.callback_query(F.data.regexp(r"^aus:(\d+)$"))
async def cb_user_disputes(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    await _deal_list(bot, s, user, c, int(c.data.split(":")[1]), "Споры", (Deal.dispute_reason.is_not(None),))


@router.callback_query(F.data.regexp(r"^auw:(\d+)$"))
async def cb_user_payments(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    uid = int(c.data.split(":")[1])
    wds = (await s.scalars(select(Withdrawal).where(Withdrawal.user_id == uid).order_by(Withdrawal.id.desc()).limit(8))).all()
    deps = (await s.scalars(select(Deposit).where(Deposit.user_id == uid).order_by(Deposit.id.desc()).limit(8))).all()
    await show(bot, user, f"{title(pe('wallet'), 'Выводы и пополнения')} · <code>{uid}</code>"
               + ("" if wds or deps else "\n\nОпераций нет"), kb(
        *[btn(f"Вывод #{w.id} · {money.usdt(w.amount)} USDT · {WD_LABEL.get(w.status, w.status)}", f"awv:{w.id}", "up")
          for w in wds],
        *[btn(f"Пополнение #{d.id} · {money.usdt(d.amount)} USDT · {DEP_LABEL.get(d.status, d.status)}", f"adp:{d.id}",
              "down") for d in deps],
        back(f"auv:{uid}", "Профиль"),
    ), c)


# ---------- balance adjustment: sign -> amount -> reason -> draft in DB -> confirm (-> second admin) ----------

@router.callback_query(F.data.regexp(r"^aadj:(\d+)$"))
async def cb_adjust_menu(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    u = await s.get(User, int(c.data.split(":")[1]))
    if not u:
        return await c.answer()
    last = (await s.scalars(select(Adjustment).where(Adjustment.user_id == u.id)
                            .order_by(Adjustment.id.desc()).limit(5))).all()
    limit = settings.dec("adjust_approval_usdt")
    await show(bot, user, "\n".join(line for line in [
        title(pe("dollar"), f"Изменить баланс · {u.id}"),
        "",
        quote(f"{pe('dollar')} Доступно: <b>{money.usdt(u.balance)} USDT</b>",
              f"{pe('lock')} Заморожено: {money.usdt(u.frozen)} USDT — меняется только через сделки"),
        "Списать можно только доступный баланс. Каждая корректировка получает номер и попадает в журнал.",
        f"От {money.usdt(limit)} USDT нужно подтверждение второго администратора." if limit > 0 else None,
    ] if line is not None), kb(
        [btn("Начислить", f"aum:{u.id}:+", "plus", style="success"), btn("Списать", f"aum:{u.id}:-", "down", style="danger")],
        *[btn(f"#{a.id} · {'+' if a.delta > 0 else ''}{money.usdt(a.delta)} · {ADJ_STATUS[a.status]}", f"adjv:{a.id}", "doc")
          for a in last],
        back(f"auv:{u.id}", "Профиль"),
    ), c)


@router.callback_query(F.data.regexp(r"^aum:(\d+):([+-])$"))
async def cb_money(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, uid, sign = c.data.split(":")
    u = await s.get(User, int(uid))
    if not u:
        return await c.answer()
    await state.set_state(Adm.balance)
    await state.set_data({"uid": int(uid), "sign": sign})
    await show(bot, user, "\n".join([
        title(pe("dollar"), ("Начисление" if sign == "+" else "Списание") + f" · {u.id}"),
        "",
        f"Доступно сейчас: <b>{money.usdt(u.balance)} USDT</b>.",
        "Шаг 1/3. Отправьте сумму в USDT:",
    ]), kb(back(f"aadj:{uid}", "Отмена")), c)


@router.message(Adm.balance, F.text)
async def msg_money(m: Message, bot: Bot, user: User, state: FSMContext):
    data = await state.get_data()
    v = parse_usdt(m.text)
    if v is None:
        return await show(bot, user, title(pe("dollar"), "Корректировка") + "\n\nШаг 1/3. Отправьте сумму в USDT."
                          + warn("Нужно положительное число"), kb(back(f"aadj:{data['uid']}", "Отмена")))
    await state.set_state(None)
    await state.update_data(amount=str(v))
    await show(bot, user, "\n".join([
        title(pe("dollar"), "Корректировка"),
        "",
        f"Сумма: <b>{data['sign']}{money.usdt(v)} USDT</b>",
        "Шаг 2/3. Выберите причину:",
    ]), kb(*[btn(label, f"amr:{code}", "pencil") for code, label in ADJ_REASONS.items()],
           back(f"aadj:{data['uid']}", "Отмена")))


@router.callback_query(F.data.regexp(r"^amr:(\w+)$"))
async def cb_money_reason(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    code = c.data.split(":")[1]
    data = await state.get_data()
    if code not in ADJ_REASONS or not data.get("amount"):
        return await c.answer("Начните корректировку заново", show_alert=True)
    await state.update_data(reason=code)
    if code == "other":
        await state.set_state(Adm.comment)
        return await show(bot, user, title(pe("dollar"), "Корректировка") + "\n\nШаг 3/3. Опишите причину "
                          "(5–200 символов). Её увидят пользователь и журнал.", kb(back(f"aadj:{data['uid']}", "Отмена")), c)
    await _draft(bot, s, user, state, "", c)


@router.message(Adm.comment, F.text)
async def msg_money_comment(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    comment = " ".join(m.text.split())
    if not 5 <= len(comment) <= 200:
        uid = (await state.get_data())["uid"]
        return await show(bot, user, title(pe("dollar"), "Корректировка") + "\n\nШаг 3/3. Опишите причину."
                          + warn("От 5 до 200 символов"), kb(back(f"aadj:{uid}", "Отмена")))
    await _draft(bot, s, user, state, comment)


async def _draft(bot, s, user, state, comment: str, src=None):
    """The draft lives in the DB: confirming it is idempotent and survives restarts."""
    data = await state.get_data()
    await state.clear()
    v = Decimal(data["amount"])
    u = await s.get(User, data["uid"])
    adj = Adjustment(user_id=u.id, admin_id=user.id, delta=v if data["sign"] == "+" else -v, reason=data["reason"],
                     comment=comment, balance_before=u.balance, balance_after=u.balance + (v if data["sign"] == "+" else -v))
    s.add(adj)
    await s.flush()
    await adjustment_screen(bot, s, user, adj, src)


def _adj_reason(a: Adjustment) -> str:
    return ADJ_REASONS.get(a.reason, a.reason) + (f": {a.comment}" if a.comment else "")


async def adjustment_screen(bot, s, admin: User, a: Adjustment, src=None, note: str = ""):
    u = await s.get(User, a.user_id)
    limit = settings.dec("adjust_approval_usdt")
    needs_second = limit > 0 and abs(a.delta) >= limit
    lines = [
        title(pe("dollar"), f"Корректировка #{a.id} · {ADJ_STATUS[a.status]}"),
        "",
        quote(f"{pe('profile')} Пользователь: {_who(u)}",
              f"{pe('dollar')} {'Начислить' if a.delta > 0 else 'Списать'}: <b>{money.usdt(abs(a.delta))} USDT</b>",
              f"Причина: {esc(_adj_reason(a))}",
              f"Доступно: {money.usdt(a.balance_before)} → <b>{money.usdt(a.balance_after)} USDT</b>"
              + (" (на момент черновика)" if a.status in ("draft", "pending") else ""),
              f"Создал администратор <code>{a.admin_id}</code> · {at(a.created_at, 'dt')}"
              + (f" · подтвердил <code>{a.approved_by}</code>" if a.approved_by else "")
              + (f" · проведена {at(a.done_at, 'dt')}" if a.done_at else "")),
    ]
    if a.status == "draft" and a.balance_after < 0:
        lines.append(warn("Доступного баланса не хватает — списание будет отклонено"))
    if a.status == "draft" and a.user_id == a.admin_id:
        lines.append(f"{pe('lock')} Это ваш собственный баланс: провести сможет только другой администратор.")
    elif a.status == "draft" and needs_second:
        lines.append(f"Сумма от {money.usdt(limit)} USDT: после подтверждения нужен второй администратор.")
    rows = []
    if a.status == "draft" and a.admin_id == admin.id:
        rows.append([btn("Подтвердить", f"adj:ok:{a.id}", "ok", style="success"), btn("Отмена", f"adj:no:{a.id}", "cross")])
    elif a.status == "pending" and a.admin_id != admin.id:
        rows.append([btn("Подтвердить вторым", f"adj:ok:{a.id}", "ok", style="success"),
                     btn("Отклонить", f"adj:no:{a.id}", "cross", style="danger")])
    elif a.status == "pending":
        lines.append("Ждёт подтверждения другого администратора.")
        rows.append(btn("Отозвать", f"adj:no:{a.id}", "cross"))
    await show(bot, admin, "\n".join(lines) + note, kb(
        *rows, [btn("История", f"aev:adj:{a.id}", "list"), btn("Профиль", f"auv:{a.user_id}", "profile")]), src)


@router.callback_query(F.data.regexp(r"^adjv:(\d+)$"))
async def cb_adjustment(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    a = await s.get(Adjustment, int(c.data.split(":")[1]))
    if a:
        await adjustment_screen(bot, s, user, a, c)


async def apply_adjustment(s: AsyncSession, adj_id: int, admin: User) -> tuple[str, Adjustment]:
    """Idempotent: the row lock plus the status check make a repeated click a no-op."""
    a = await s.get(Adjustment, adj_id, with_for_update=True, populate_existing=True)
    if a.status not in ("draft", "pending"):
        return "already", a
    ref = f"adj:{a.id}"
    if a.status == "draft":
        if a.admin_id != admin.id:
            return "not_owner", a
        limit = settings.dec("adjust_approval_usdt")
        # own balance: always a second admin, whatever the amount
        if a.user_id == admin.id or (limit > 0 and abs(a.delta) >= limit):
            a.status = "pending"
            events.add(s, ref, "pending", f"{money.usdt(a.delta)} USDT для {a.user_id} ждёт второго администратора "
                                          f"({_adj_reason(a)})", a.user_id, alert=True)
            return "pending", a
    elif a.admin_id == admin.id:
        return "need_other", a
    else:
        a.approved_by = admin.id
    try:
        u = await money.add(s, a.user_id, a.delta, "admin", ref, note=_adj_reason(a))
    except money.NotEnough:
        a.status, a.done_at = "failed", now()
        events.add(s, ref, "failed", "Отклонена: не хватило доступного баланса", a.user_id)
        return "failed", a
    a.status, a.done_at = "done", now()
    a.balance_after, a.balance_before = u.balance, u.balance - a.delta
    audit.log(s, admin.id, "balance", f"user:{a.user_id}", f"adj #{a.id}: {a.delta} USDT, {_adj_reason(a)}")
    events.add(s, ref, "done", f"{'+' if a.delta > 0 else ''}{money.usdt(a.delta)} USDT пользователю {a.user_id}: "
                               f"{money.usdt(a.balance_before)} → {money.usdt(a.balance_after)} ({_adj_reason(a)})",
               a.user_id, alert=True)
    return "done", a


@router.callback_query(F.data.regexp(r"^adj:(ok|no):(\d+)$"))
async def cb_adjustment_action(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, act, aid = c.data.split(":")
    if act == "no":
        a = await s.get(Adjustment, int(aid), with_for_update=True, populate_existing=True)
        if a.status in ("draft", "pending"):
            a.status = "cancelled"
            events.add(s, f"adj:{a.id}", "cancelled", f"Отменена администратором {user.id}", a.user_id)
        return await adjustment_screen(bot, s, user, a, c)
    result, a = await apply_adjustment(s, int(aid), user)
    await s.commit()
    if result == "done":
        await notify(bot, a.user_id, f"{pe('wallet')} <b>Баланс изменён администрацией: {'+' if a.delta > 0 else ''}"
                                     f"{money.usdt(a.delta)} USDT</b>\nПричина: {esc(_adj_reason(a))}\nОперация #{a.id}")
    note = {"done": ok("Проведена"), "pending": ok("Отправлена на подтверждение второму администратору"),
            "failed": warn("Не хватает доступного баланса — ничего не списано"),
            "already": ok("Уже обработана"), "need_other": warn("Подтвердить должен другой администратор"),
            "not_owner": warn("Черновик создан другим администратором")}[result]
    await adjustment_screen(bot, s, user, a, c, note)


@router.callback_query(F.data == "aadjl")
async def cb_adjust_pending(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    rows = (await s.scalars(select(Adjustment).where(Adjustment.status == "pending").order_by(Adjustment.id))).all()
    await show(bot, user, title(pe("dollar"), "Корректировки на подтверждении") + ("" if rows else "\n\nПусто"), kb(
        *[btn(f"#{a.id} · {a.user_id} · {'+' if a.delta > 0 else ''}{money.usdt(a.delta)} USDT", f"adjv:{a.id}", "doc")
          for a in rows],
        back("a", "Админ-панель"),
    ), c)


# ---------- direct message to a user (support reply) ----------

@router.callback_query(F.data.regexp(r"^amsg:(\d+)$"))
async def cb_message(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    uid = int(c.data.split(":")[1])
    u = await s.get(User, uid)
    if not u:
        return await c.answer()
    await state.set_state(Adm.message)
    await state.update_data(uid=uid)
    await show(bot, user, f"{title(pe('support'), 'Сообщение пользователю')} {_who(u)}\n\n"
                          "Отправьте текст (до 2000 символов). Пользователь сможет ответить через «Помощь».",
               kb(back(f"auv:{uid}", "Отмена")), c)


@router.message(Adm.message, F.text)
async def msg_message(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    uid = (await state.get_data())["uid"]
    await state.set_state(None)
    u = await s.get(User, uid)
    sent = await notify(bot, uid, f"{pe('support')} <b>Сообщение от поддержки</b>\n\n{esc(m.text[:2000])}",
                        kb(btn("Ответить", "sup", "support"), back("x", "Скрыть", "cross")))
    audit.log(s, user.id, "message", f"user:{uid}", m.text[:2000])
    await user_screen(bot, s, user, u, note=ok("Сообщение доставлено") if sent
                      else warn("Не доставлено: пользователь заблокировал бота"))


@router.callback_query(F.data.regexp(r"^auc:(\d+)$"))
async def cb_user_cards(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    uid = int(c.data.split(":")[1])
    cards = (await s.scalars(select(Card).where(Card.user_id == uid, ~Card.is_deleted).order_by(Card.id))).all()
    await show(bot, user, f"{title(pe('card'), 'Карты пользователя')} <code>{uid}</code>"
               + ("" if cards else "\n\nКарт нет"), kb(
        *[btn(card_label(cd), f"acv:{cd.id}", card_icon(cd)) for cd in cards],
        back(f"auv:{uid}", "Профиль"),
    ), c)


# ---------- cards ----------

@router.callback_query(F.data.regexp(r"^ac:(\d+)$"))
async def cb_cards(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    page, per = int(c.data.split(":")[1]), 10
    total = await s.scalar(select(func.count(Card.id)).where(~Card.is_deleted))
    cards = (await s.scalars(
        select(Card).where(~Card.is_deleted).order_by(Card.is_active.desc(), Card.id.desc()).offset(page * per).limit(per)
    )).all()
    nav = []
    if page > 0:
        nav.append(btn("Назад", f"ac:{page - 1}", "prev"))
    if (page + 1) * per < total:
        nav.append(btn("Далее", f"ac:{page + 1}", "next"))
    await show(bot, user, f"{title(pe('card'), 'Все карты')}\n\nВсего: <b>{total}</b>. Найти по номеру — «Найти».", kb(
        *[btn(card_label(cd), f"acv:{cd.id}", card_icon(cd)) for cd in cards],
        nav,
        back("a", "Админ-панель"),
    ), c)


async def admin_card_screen(bot: Bot, s: AsyncSession, admin: User, card: Card, src=None, note: str = ""):
    owner = await s.get(User, card.user_id)
    busy = (await deals.busy_cards(s, owner.id)).get(card.id)
    visible, why = deals.card_visibility(card, owner, busy, (await deals.used_today(s, [card.id]))[card.id])
    state = "Удалена владельцем" if card.is_deleted else "Заблокирована" if card.is_banned \
        else "Включена" if card.is_active else "Выключена"
    await show(bot, admin, "\n".join([
        title(pe("card"), f"Карта #{card.id}"),
        f"{pe(card_icon(card))} {state} · {'видна' if visible else 'не видна'} покупателям: {why}",
        "",
        quote(
            f"{pe('bank')} {esc(card.bank)} · {'СБП' if card.kind == 'sbp' else 'карта'}",
            f"{pe('key')} <code>{esc(card.requisites)}</code>",
            f"{pe('profile')} {esc(card.holder)}",
            f"{pe('ruble')} {money.fmt(card.min_rub)} – {money.fmt(card.max_rub)} ₽",
            f"Владелец: {_who(owner)}",
        ),
        "«Выключить» — владелец может включить снова; «Заблокировать» — только администрация.",
    ]) + note, kb(
        btn("Выключить", f"aco:{card.id}", "pause") if card.is_active else None,
        btn("Разблокировать", f"acb:{card.id}:0", "ok", style="success") if card.is_banned
        else btn("Заблокировать", f"acb:{card.id}:1", "ban", style="danger"),
        btn(f"Сделка #{busy}", f"adv:{busy}", "fire") if busy else None,
        btn("Владелец", f"auv:{owner.id}", "profile"),
        back("ac:0", "Все карты"),
    ), src)


@router.callback_query(F.data.regexp(r"^acv:(\d+)$"))
async def cb_card(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    card = await s.get(Card, int(c.data.split(":")[1]))
    if card:
        await admin_card_screen(bot, s, user, card, c)


@router.callback_query(F.data.regexp(r"^ac(o:\d+|b:\d+:[01])$"))
async def cb_card_action(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    parts = c.data.split(":")
    card = await s.get(Card, int(parts[1]))
    if not card:
        return await c.answer()
    if parts[0] == "aco":
        if not card.is_active:
            return await admin_card_screen(bot, s, user, card, c, ok("Уже выключена"))
        card.is_active = False
        action, msg = "card_off", (f"{pe('pause')} Ваша карта {esc(card_label(card))} выключена администратором "
                                   "и скрыта от покупателей. Включить снова можно в «Продать USDT».")
    else:
        target = parts[2] == "1"
        if card.is_banned == target:  # explicit target: double clicks and two admins are safe
            return await admin_card_screen(bot, s, user, card, c, ok("Уже " + ("заблокирована" if target else "разблокирована")))
        card.is_banned, card.is_active = target, False
        action = "card_ban" if target else "card_unban"
        msg = (f"{pe('ban')} Ваша карта {esc(card_label(card))} заблокирована администрацией. Вопросы — в «Помощь»."
               if target else f"{pe('ok')} Ваша карта {esc(card_label(card))} разблокирована. Включите её, чтобы продавать.")
    audit.log(s, user.id, action, f"card:{card.id}")
    events.add(s, f"card:{card.id}", action, {"card_off": "Выключена", "card_ban": "Заблокирована",
                                              "card_unban": "Разблокирована"}[action] + f" ({user.name})",
               card.user_id, alert=action != "card_off")
    await notify(bot, card.user_id, msg)
    await admin_card_screen(bot, s, user, card, c, ok("Готово. Открытая сделка по карте продолжится."))


# ---------- deals & disputes ----------

@router.callback_query(F.data == "ad")
async def cb_deals(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    await show(bot, user, title(pe("list"), "Сделки") + "\n\nКонкретную сделку найдите через «Найти» → <code>#номер</code>.", kb(
        btn("Споры", "adl:dispute", "flag"),
        btn("Продавец молчит", "adl:slow", "clock"),
        btn("Все открытые", "adl:open", "fire"),
        btn("Последние", "adl:all", "list"),
        back("a", "Админ-панель"),
    ), c)


@router.callback_query(F.data.regexp(r"^adl:(dispute|open|all|slow|search)$"))
async def cb_deal_list(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    kind = c.data.split(":")[1]
    q = select(Deal).order_by(Deal.id.desc()).limit(20)
    names = {"dispute": "Споры", "open": "Открытые сделки", "all": "Последние сделки", "slow": "Продавец молчит",
             "search": "Ордерные заявки в поиске"}
    if kind == "dispute":
        q = select(Deal).where(Deal.status == "dispute").order_by(Deal.paid_at, Deal.id).limit(20)  # oldest first
    elif kind == "open":
        q = q.where(Deal.status.in_(deals.OPEN))
    elif kind == "search":
        q = select(Deal).where(Deal.status.in_(orders.REQUEST)).order_by(Deal.id).limit(20)
    elif kind == "slow":
        q = select(Deal).where(Deal.status == "paid", Deal.paid_at < now() - timedelta(
            minutes=settings.num("confirm_minutes"))).order_by(Deal.paid_at).limit(20)
    rows = (await s.scalars(q)).all()
    hint = {"dispute": "Сначала самые старые. Время — сколько прошло с момента чека.",
            "slow": "Чек загружен, продавец не ответил. Можно написать продавцу из карточки сделки."}.get(kind, "")
    await show(bot, user, title(pe("list"), names[kind]) + (f"\n{hint}" if rows and hint else "")
               + ("" if rows else f"\n\n{pe('ok')} Пусто — всё обработано"), kb(
        *[btn(f"#{d.id} · {money.fmt(d.amount_rub)} ₽ · {STATUS[d.status][1]}"
              + (f" · {ago(d.paid_at)}" if d.paid_at and d.status in ("paid", "dispute") else ""),
              f"adv:{d.id}", STATUS[d.status][0]) for d in rows],
        back("ad", "Сделки"),
    ), c)


def ago(dt) -> str:
    minutes = int((now() - deals.aware(dt)).total_seconds() // 60)
    return f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч" if minutes < 2880 else f"{minutes // 1440} дн"


async def deal_view(bot: Bot, s: AsyncSession, user: User, d: Deal, src=None, note: str = ""):
    markup = dispute_kb(d, btn("История сделки", f"aev:deal:{d.id}", "list"), back("ad", "Сделки"))
    await show(bot, user, await dispute_text(s, d) + note, markup, src)


@router.callback_query(F.data.regexp(r"^adv:(\d+)$"))
async def cb_deal_view(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    d = await s.get(Deal, int(c.data.split(":")[1]))
    if d:
        await deal_view(bot, s, user, d, c)


@router.callback_query(F.data.regexp(r"^af:(\d+)$"))
async def cb_files(c: CallbackQuery, bot: Bot, s: AsyncSession):
    d = await s.get(Deal, int(c.data.split(":")[1]))
    await c.answer()
    if d:
        with suppress(TelegramAPIError):
            await send_files(bot, c.from_user.id, d)


VERDICTS = {"b": "в пользу покупателя", "s": "в пользу продавца (отмена)", "a": "по фактической сумме",
            "c": "отмена неоплаченной сделки"}


@router.callback_query(F.data.regexp(r"^ar:(\d+):([bsac])$"))
async def cb_resolve_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    _, did, verdict = c.data.split(":")
    d = await s.get(Deal, int(did), populate_existing=True)
    allowed = deals.UNPAID if verdict == "c" else ("paid", "dispute")
    if not d or d.status not in allowed or (verdict == "a" and d.dispute_amount_rub is None):
        return await c.answer("Сделка уже закрыта или решение недоступно", show_alert=True)
    await show(bot, user, "\n".join([
        f"{pe('warn')} <b>Решение по сделке #{d.id}: {VERDICTS[verdict]}</b>",
        "",
        quote(*verdict_effects(d, verdict)),
        f"{pe('lock')} <b>Необратимо.</b> Обе стороны получат уведомление с решением.",
        "Комментарий объяснит сторонам, почему принято такое решение (например, «перевод не найден в выписке»).",
    ]), kb(btn("С комментарием", f"ar3:{d.id}:{verdict}", "pencil", style="primary"),
           btn("Без комментария", f"ar2:{d.id}:{verdict}", "ok", style="danger"),
           back(f"adv:{d.id}", "Назад", "back")), c)


@router.callback_query(F.data.regexp(r"^ar3:(\d+):([bsac])$"))
async def cb_resolve_comment(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    _, did, verdict = c.data.split(":")
    await state.set_state(Adm.verdict)
    await state.set_data({"deal": int(did), "verdict": verdict})
    await show(bot, user, f"{title(pe('pencil'), f'Комментарий к решению · сделка #{did}')}\n\n"
                          f"Решение: <b>{VERDICTS[verdict]}</b>.\nОтправьте комментарий (5–500 символов) — "
                          "его увидят покупатель и продавец. Решение будет проведено сразу после отправки.",
               kb(back(f"ar:{did}:{verdict}", "Отмена")), c)


@router.message(Adm.verdict, F.text)
async def msg_resolve_comment(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    comment = " ".join(m.text.split())
    if not 5 <= len(comment) <= 500:
        return await show(bot, user, title(pe("pencil"), "Комментарий к решению") + "\n\nОтправьте комментарий ещё раз."
                          + warn("От 5 до 500 символов"), kb(back(f"ar:{data['deal']}:{data['verdict']}", "Отмена")))
    await state.clear()
    await resolve(bot, s, user, data["deal"], data["verdict"], comment)


@router.callback_query(F.data.regexp(r"^ar2:(\d+):([bsac])$"))
async def cb_resolve(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, did, verdict = c.data.split(":")
    await resolve(bot, s, user, int(did), verdict, "", c)


async def resolve(bot: Bot, s: AsyncSession, user: User, did: int, verdict: str, comment: str, src=None):
    d = await s.get(Deal, did)
    if not d:
        return await admin_screen(bot, s, user, src)
    error = ""
    try:
        if verdict == "c" and d.status in orders.REQUEST:
            res = await orders.cancel(s, d.id, "void", "admin_void")
        elif verdict in ("s", "c"):
            res = (await deals.cancel(s, d.id, ("waiting_payment",), "void", "admin_void") if verdict == "c"
                   else await deals.cancel(s, d.id, ("paid", "dispute"), "cancelled", "dispute_seller"))
        else:
            res = await deals.complete(s, d.id, actual_rub=d.dispute_amount_rub if verdict == "a" else None,
                                       reason="dispute_actual" if verdict == "a" else "dispute_buyer")
    except deals.DealError as e:
        res, error = None, str(e)
    if not res:
        await s.rollback()
        await s.refresh(user)  # rollback expires every loaded object
        d = await s.get(Deal, did, populate_existing=True)
        return await deal_view(bot, s, user, d, src, warn(error or "Сделка уже закрыта другим администратором"))
    res.resolution = comment or None
    audit.log(s, user.id, "resolve", f"deal:{res.id}", VERDICTS[verdict] + (f": {comment}" if comment else ""))
    events.add(s, f"deal:{res.id}", "resolved", f"Решено {VERDICTS[verdict]} ({user.name})"
               + (f": {comment}" if comment else ""), notice=True)
    await s.commit()
    await deal_view(bot, s, user, res, src, ok(f"Решено {VERDICTS[verdict]}. Стороны уведомлены."))
    head = (f"Сделка #{res.id} отменена администрацией. Не переводите по ней деньги" if verdict == "c"
            else f"Спор по сделке #{res.id} решён {VERDICTS[verdict]}")
    await push(bot, s, res.buyer_id, res, head)
    for uid in deals.sellers(res):
        await push(bot, s, uid, res, head)
