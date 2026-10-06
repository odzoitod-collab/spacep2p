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
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.admins import IsAdmin
from bot.emoji import back, btn, kb, pe
from bot.handlers.admin_deals import deal_view
from bot.handlers.deal import STATUS, push, send_files, verdict_effects
from bot.handlers.seller import card_icon, card_label
from bot.handlers.wallet import ledger_line, parse_usdt, withdraw_terms
from bot.models import (Adjustment, ApiApplication, Card, Deal, Deposit, Event, Ledger, Operator, OrderMerchant, Signup,
                        Team, Ticket, TonAddress, User, Withdrawal, now)
from bot.services import admins, audit, deals, events, money, operators, orders, settings, teams, ton
from bot.ui import alink, at, cf, esc, field, files_to, mention, notify, ok, quote, section, show, title, ulink, warn
from bot.ui import card as fields

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class Adm(StatesGroup):
    setting = State()
    search = State()
    balance = State()
    comment = State()
    reply = State()
    verdict = State()
    pct = State()


ADJ_REASONS = {"deposit_fix": "Исправление пополнения", "compensation": "Компенсация", "refund": "Возврат",
               "tech": "Техническая корректировка", "other": "Другое",
               "manual": "Ручная правка"}  # «manual»: only /balance (handlers.admin_balance), not in the picker
ADJ_STATUS = {"draft": "черновик", "pending": "ждёт второго администратора", "done": "проведена",
              "cancelled": "отменена", "failed": "отклонена: не хватило доступного баланса"}


# ---------- dashboard ----------

@router.callback_query(F.data == "a")
async def cb_admin(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await admin_screen(bot, s, user, c)


async def admin_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    day = now() - timedelta(hours=24)
    count = lambda *where: s.scalar(select(func.count()).where(*where))  # noqa: E731
    users = await s.scalar(select(func.count(User.id)))
    new_users = await count(User.created_at > day)
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
    queued_wd = await count(Withdrawal.status == "queued", Withdrawal.method == "ton")
    tickets = await count(Ticket.status == "open")
    approvals = await count(Adjustment.status == "pending")
    api_apps = await count(ApiApplication.status == "pending")
    om_apps = await count(OrderMerchant.status == "pending")
    om_working = await count(OrderMerchant.status == "approved")
    searching = await count(Deal.status.in_(orders.REQUEST))
    free_orders = await count(Deal.status == "checking", Deal.operator_id.is_(None))
    team_apps = await count(Team.status == "pending")
    teams_working = await count(Team.status == "approved")
    signups = await count(Signup.status == "pending")
    op_debt = await operators.total_debt(s)
    backlog = await count(Event.alert, Event.sent_at.is_(None), Event.attempts < events.MAX_ATTEMPTS)
    income24 = await s.scalar(select(func.coalesce(func.sum(Ledger.delta), 0))
                              .where(Ledger.user_id.is_(None), Ledger.created_at > day))
    income = await s.scalar(select(func.coalesce(func.sum(Ledger.delta), 0)).where(Ledger.user_id.is_(None)))
    held = Decimal(await s.scalar(select(func.coalesce(func.sum(User.balance + User.frozen + User.team_balance), 0))))
    solvency = ""
    unswept = Decimal(await s.scalar(select(func.coalesce(func.sum(TonAddress.unswept), 0))))
    try:
        available, gas = await ton.hot_balances(max_age=60, timeout=3)
        wallet = f"<b>{money.usdt(available)} USDT</b> · газ {money.fmt(gas, 4)} TON" + (
            f" · не собрано {money.usdt(unswept)} USDT" if unswept else "")
        if available + unswept < held:
            solvency = warn(f"На кошельках TON на {money.usdt(held - available - unswept)} USDT меньше, чем на балансах "
                            "пользователей: выводы могут ждать в очереди.")
        if gas < ton.HOT_LOW:
            solvency += warn(f"Газа на горячем кошельке {money.fmt(gas, 4)} TON — пополните TON.")
    except Exception:
        wallet = "нет ответа" if ton.enabled() else "выключен (TON_SEED не задан)"
    todo = [(signups, f"<b>Заявок на вход:</b> {signups}"),
            (disputes, f"<b>Споров:</b> {disputes}"),
            (slow, f"<b>Продавец молчит дольше {settings.get('confirm_minutes')} мин:</b> {slow}"),
            (unknown, f"<b>Выводов на проверке:</b> {unknown}"),
            (free_orders, f"<b>Bybit-ордеров ждут оператора:</b> {free_orders}"),
            (tickets, f"<b>Открытых обращений:</b> {tickets}"),
            (approvals, f"<b>Корректировок ждут второго админа:</b> {approvals}"),
            (api_apps, f"<b>Заявок на API:</b> {api_apps}"),
            (om_apps, f"<b>Анкет ордерных мерчантов:</b> {om_apps}"),
            (team_apps, f"<b>Заявок на команды:</b> {team_apps}"),
            (searching, f"<b>Ордерных заявок ищут реквизиты:</b> {searching}"),
            (backlog, f"<b>Недоставленных уведомлений:</b> {backlog}")]
    active = [line for n, line in todo if n]
    await show(bot, user, "\n".join([
        title(pe("settings"), "Админ-панель") + f" · {admins.role(user.id)}",
        "",
        section("bell", "Требует внимания"),
        "\n".join(active) if active else "<i>Очереди пусты — всё обработано.</i>",
        "",
        section("stats", "За 24 часа"),
        f"<b>Сделок:</b> {done24} на {money.fmt(Decimal(volume24))} ₽ · открыто сейчас {opened}",
        f"<b>Доход площадки:</b> +{money.usdt(Decimal(income24))} USDT · всего {money.usdt(Decimal(income))} USDT",
        f"<b>Новых пользователей:</b> {new_users} · всего {users}",
        "",
        section("people", "Люди"),
        f"<b>На смене:</b> {online} · карт в работе {cards}",
        f"<b>Ордерных мерчантов:</b> {om_working} · операторов {len(await operators.ids(s))} · команд {teams_working}",
        f"<b>Администраторов:</b> {len(admins.ids())}",
        "",
        section("wallet", "Деньги"),
        f"<b>Балансы пользователей:</b> {money.usdt(held)} USDT",
        f"<b>Горячий кошелёк TON:</b> {wallet}",
        f"<b>Долг операторов (Bybit):</b> {money.usdt(op_debt)} USDT" if op_debt else "",
        "",
        section("support", "Админ-чат"),
        await _admin_chat_line(bot),
    ]).replace("\n\n\n", "\n\n") + solvency + note, kb(
        # what is urgent stands out on top; everything else is one button per section, its queue in the label
        btn(f"Споры · {disputes}", "adl:dispute", "flag", style="danger") if disputes else None,
        btn(f"Выводы на проверке · {unknown}", "awl:check", "up", style="danger") if unknown else None,
        [btn(_n("Заявки на вход", signups), "asu", "pencil"), btn(_n("Сделки", slow + searching), "ad", "list")],
        [btn("Найти", "au", "search"), btn(_n("Обращения", tickets), "atl", "support")],
        [btn(_n("Мерчанты", om_apps), "aoml", "key"), btn(_n("Операторы", free_orders), "aopl", "shop")],
        [btn(_n("Команды", team_apps), "atml", "people"), btn(_n("API", api_apps), "aapi", "key")],
        [btn("Финансы", "afin", "stats"), btn(_n("TON-кошелёк", queued_wd), "aton", "wallet")],
        [btn(_n("Корректировки", approvals), "aadjl", "dollar"), btn("Балансы", "bal:p:0", "wallet")],
        btn("Карты", "ac:0", "card"),
        [btn("Комиссии", "acm", "percent"), btn("Настройки", "as", "settings")],
        [btn("Чат и канал", "ach", "people"), btn("Администраторы", "aadm", "lock")],
        [btn("Журнал", "aa", "list"), btn("Отчёты CSV", "arp", "doc")],
        back("menu", "В меню"),
    ), src)


def _n(label: str, n: int) -> str:
    return f"{label} · {n}" if n else label


async def _admin_chat_line(bot: Bot) -> str:
    from bot.handlers.logchat import is_forum, targets
    groups = [c for c in targets() if c < 0]
    if not groups:
        return "<b>Не настроен:</b> добавьте бота админом в группу и отправьте там /setts — логи, решения и " \
               "панель будут прямо в группе."
    try:
        info = await bot.get_chat(groups[0])
        name = f"«{esc(info.title or str(groups[0]))}»"
    except TelegramAPIError:
        return f"<b>Чат:</b> <code>{groups[0]}</code> — бот его не видит"
    topics = "темы включены" if await is_forum(bot, groups[0]) else "без тем — включите темы и отправьте /setts"
    return f"<b>Чат:</b> {name} · {topics}"


# ---------- settings ----------

SETTING_ICONS = ["percent", "clock", "wallet", "lock", "info", "key"]  # one per settings.GROUPS section
# short names for the overview: every setting in one line of its section
SHORT = {"rate": "курс", "order_rate": "ордер", "seller_pct": "карта", "platform_pct": "площадка",
         "deposit_fee": "пополнение", "withdraw_pct": "вывод", "chain_withdraw_fee": "вывод +",
         "team_pct": "тимлиду", "deal_minutes": "оплата", "buyer_max_open": "сделок сразу", "confirm_minutes": "спор через",
         "escalate_minutes": "автоспор", "late_hold_minutes": "залог", "late_minutes": "поздний чек",
         "online_minutes": "смена", "deposit_min": "пополнение от",
         "chain_withdraw_min": "вывод от", "ton_sweep_min": "сбор от", "adjust_approval_usdt": "второй админ от", "withdraw_turnover": "прокрутка", "receipt_images": "чеки",
         "log_all": "лог", "signup_review": "вход", "join_required": "чат и канал", "support": "поддержка", "manager": "менеджер",
         "tutorial": "о сервисе", "manual_url": "памятка", "docs_url": "инструкции", "chat_id": "чат",
         "channel_id": "канал", "channel_autopost_hours": "автопост", "order_min_rub": "от", "order_max_rub": "до", "order_search_minutes": "поиск",
         "order_take_minutes": "реквизиты", "order_link_minutes": "ссылка", "order_pay_minutes": "оплата от",
         "order_check_minutes": "оператору", "strike_limit": "пропусков", "strike_sleep_hours": "пауза",
         "rep_min_count": "оценок", "rep_low": "без Bybit <", "rep_mid": "лимит <", "rep_mid_max_rub": "до",
         "late_hold_minutes": "залог", "escalate_minutes": "автоспор", "webapp_url": "приложение",
         "webapp_link": "app-ссылка", "card_min_rub": "карта от",
         "operator_max_debt": "долг до", "abandon_limit": "брошенных", "abandon_pause_minutes": "пауза",
         "card_parallel": "на карту", "order_first_wave": "лучшим", "order_wave_seconds": "через"}


OVERVIEW_PER_GROUP = 7


def _brief(key: str) -> str:
    """A setting's value in the overview: texts and links only say whether they are set."""
    if settings.SPEC[key][1] in ("html", "url"):
        return "есть" if settings.get(key) else "нет"
    return settings.human(key)


async def settings_screen(bot: Bot, user: User, src=None, note: str = ""):
    rate, sp, pp = settings.dec("rate"), settings.dec("seller_pct"), settings.dec("platform_pct")
    q = money.quote(Decimal(10000), rate, sp, pp)
    qo = money.quote_fixed(Decimal(10000), rate, settings.dec("order_rate"), pp)
    lines = [
        title(pe("settings"), "Настройки"),
        "",
        f"10 000 ₽ → {money.usdt(q.buyer_credit)} USDT · площадке {money.usdt(q.platform_fee)} / "
        f"{money.usdt(qo.platform_fee)}",
    ]
    for (name, keys), icon in zip(settings.GROUPS, SETTING_ICONS):
        shown = keys[:OVERVIEW_PER_GROUP]  # the screen keeps its banner: the rest is one tap away
        lines += ["", section(icon, name),
                  " · ".join(f"{esc(SHORT.get(k, k))} <b>{esc(_brief(k))}</b>" for k in shown)
                  + (f" · <i>ещё {len(keys) - len(shown)}</i>" if len(keys) > len(shown) else "")]
    await show(bot, user, "\n".join(lines) + note, kb(
        *[btn(name, f"asg:{i}", "pencil") for i, (name, _) in enumerate(settings.GROUPS)],
        back("a", "Админ-панель"),
    ), src)


async def group_screen(bot: Bot, user: User, idx: int, src=None, note: str = ""):
    name, keys = settings.GROUPS[idx]
    await show(bot, user, "\n".join([
        title(pe("settings"), name),
        "",
        *[field(settings.SPEC[k][2], f"<code>{esc(settings.human(k))}</code>") for k in keys],
        "",
        quote("Нажмите на параметр, чтобы изменить."),
    ]) + note, kb(
        *[btn(f"{settings.SPEC[k][2]} · {settings.human(k)}", f"as:{k}", "pencil") for k in keys],
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
    current = settings.get(key) if kind == "html" else ""
    return "\n".join([
        f"{pe('pencil')} <b>{settings.SPEC[key][2]}</b>",
        "",
        cf("Сейчас", f"<b>{esc(settings.human(key))}</b>", icon="info"),
        *([f"<blockquote expandable>{current}</blockquote>"] if current else []),
        cf("По умолчанию", esc(settings.human(key, settings.SPEC[key][0])), icon="refresh"),
        "",
        quote(f"Пришлите новое значение следующим сообщением. {settings.HINTS.get(key) or settings.HINTS.get(kind, '')}"),
    ]) + (warn(err) if err else "")


def _ret(data: dict, key: str) -> str:
    return data.get("ret") or f"asg:{settings.group_of(key)}"


RETURN = {"acs": "acm", "acx": "ach", "acc": "achn"}  # opened from «Комиссии» / the chat / the channel: back there


@router.callback_query(F.data.startswith("as:") | F.data.startswith("acs:") | F.data.startswith("acx:")
                       | F.data.startswith("acc:"))
async def cb_setting(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    """as:<key> from Settings; acs:<key> / acx:<key> return to their screen after saving."""
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
    if key == "chat_id":
        events.add(s, "app:chat", "chat_changed", f"Чат сообщества: {old or '—'} → {value or 'отключён'} "
                   f"({user.name}, {user.id})", user.id, alert=True)
    if key in ("chat_id", "channel_id") and old != value:  # another chat: who is in it is checked again
        from bot.handlers.community import forget_channel_link
        await s.execute(sql_update(User).values(**{"in_chat" if key == "chat_id" else "in_channel": False}))
        if key == "channel_id":
            await forget_channel_link(s)
    if key in ("webapp_url", "docs_url") and old != value:  # the app's address follows: the menu button too
        from bot.handlers.commands import app_menu
        await app_menu(bot)
    await state.set_state(None)
    note = ok(f"Сохранено: {esc(settings.human(key, old))} → {esc(settings.human(key))}")
    if data.get("ret") == "acm":
        return await commissions_screen(bot, s, user, note=note)
    if data.get("ret") == "ach":
        from bot.handlers.admin_chat import chat_screen
        return await chat_screen(bot, s, user, note=note)
    if data.get("ret") == "achn":
        from bot.handlers.channel import channel_screen
        return await channel_screen(bot, s, user, note=note)
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
    buyers = (await s.scalars(select(User).where((User.buy_rate.is_not(None)) | (User.buy_pct.is_not(None)))
                              .order_by(User.id).limit(20))).all()
    pct = lambda v: f"{money.fmt(v, 3)}%"  # noqa: E731
    await show(bot, admin, "\n".join([
        title(pe("percent"), "Комиссии и проценты"),
        "",
        section("swap", "Сделки"),
        field("Покупатель платит", f"<b>{pct(pp)}</b> · курс {money.fmt(rate)} ₽"),
        field("Мерчант, статичная карта", f"<b>{pct(sp)}</b> → площадке <b>{pct(pp - sp)}</b>"),
        field("Ордерный мерчант", f"курс <b>{money.fmt(orate)} ₽</b> за USDT, без процента (не выше "
              f"{money.fmt(settings.order_rate_cap(rate, pp))} ₽)"),
        field("На 10 000 ₽", f"покупателю {money.usdt(q.buyer_credit)} USDT; площадке {money.usdt(q.platform_fee)} "
              f"(карта) или {money.usdt(qo.platform_fee)} USDT (ордер: мерчант отдаёт {money.usdt(qo.seller_debit)})"),
        "",
        section("wallet", "Кошелёк"),
        field("Пополнение", f"{settings.get('deposit_fee')}% (погашение долга оператора — без комиссии)"),
        field("Вывод", f"{withdraw_terms()} — фикс. часть покрывает газ сети TON (его платит горячий кошелёк)"),
        "",
        section("people", "Команды"),
        field("Тимлиду", f"<b>{settings.get('team_pct')}%</b> от сделок участников (из дохода площадки, не больше него)"),
        "",
        section("star", f"Личные ставки мерчантов · {len(personal)}"),
        "\n".join(f"{ulink(u)}: карта {pct(u.pct_static)}" for u in personal)
        if personal else "<i>Нет — все по общим ставкам. Задать: профиль пользователя → «Процент мерчанта».</i>",
        "",
        section("star", f"Личные условия покупателей · {len(buyers)}"),
        "\n".join(f"{ulink(u)}: курс {money.fmt(settings.buyer_terms(u)[0])} ₽ · {pct(settings.buyer_terms(u)[1])}"
                  for u in buyers)
        if buyers else "<i>Нет — задать: профиль пользователя → «Условия покупателя».</i>",
        "",
        quote("Ставка фиксируется в сделке при её создании: изменения не трогают открытые сделки. Личные условия "
              "API-клиентов — «API-клиенты» → клиент."),
    ]) + note, kb(
        [btn("Покупатель", "acs:platform_pct", "dollar"), btn("Курс", "acs:rate", "swap")],
        [btn("Мерчант: карта", "acs:seller_pct", "card"), btn("Курс ордерного", "acs:order_rate", "key")],
        [btn("Пополнение", "acs:deposit_fee", "down"), btn("Вывод, %", "acs:withdraw_pct", "up")],
        btn("Вывод, фикс", "acs:chain_withdraw_fee", "up"),
        btn("Процент тимлида", "acs:team_pct", "people"),
        *[btn(f"Личная ставка · {u.id} {(u.name or '')[:16]}", f"aup:{u.id}", "star") for u in personal],
        *[btn(f"Покупатель · {u.id} {(u.name or '')[:16]}", f"aut:{u.id}", "star") for u in buyers],
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
        section("pencil", "Пришлите следующим сообщением"),
        field("Пользователь", "<code>123456789</code> или <code>@username</code>"),
        field("Сделка", "<code>#15</code>"),
        field("Вывод / пополнение", "<code>в482</code> / <code>п12</code> — откроется владелец"),
        field("Карта или телефон", "<code>2200…</code> / <code>+79…</code> полностью"),
        "",
        section("people", "Последние регистрации"),
        *[f"{ulink(u)} · {at(u.created_at, 'dt')}" + (" · заблокирован" if u.is_banned else "") for u in last],
        "",
        quote("В админ-чате то же самое — командой <code>/find запрос</code>."),
    ]), kb(
        *[btn(f"{u.name[:20] or u.id} · @{u.username or '—'}", f"auv:{u.id}", "ban" if u.is_banned else "profile")
          for u in last],
        back("a", "Админ-панель"),
    ), c)


OP_PREFIX = {"в": "wd", "w": "wd", "п": "dep", "d": "dep"}


@router.message(Adm.search, F.text)
async def msg_search(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    q = m.text.strip()
    if await find(bot, s, user, q):
        return await state.set_state(None)
    await show(bot, user, title(pe("search"), "Поиск") + "\n\n" + quote(
        "Пришлите ID, @username, #сделку, в/п + номер или реквизиты.") + warn(f"Ничего не найдено: {esc(q[:40])}"),
        kb(back("a", "Админ-панель")))


async def find(bot: Bot, s: AsyncSession, admin: User, q: str) -> bool:
    """Open what `q` points to: #15 — a deal, в12 / п12 — the owner of a withdrawal / deposit, a full card number or
    phone — the card, an ID or @username — the user. False if nothing matched."""
    low = q.lower()
    if q.startswith("#") and q[1:].isdigit():
        if d := await s.get(Deal, int(q[1:])):
            await deal_view(bot, s, admin, d)
            return True
    if low[:1] in OP_PREFIX and low[1:].isdigit():
        kind, oid = OP_PREFIX[low[0]], int(low[1:])
        op = await s.get(Withdrawal if kind == "wd" else Deposit, oid)
        if op:
            await user_screen(bot, s, admin, await s.get(User, op.user_id), found=(kind, op))
            return True
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 11 and not q.startswith("@"):
        req = ("+" + ("7" + digits[1:] if digits[0] == "8" else digits)) if len(digits) == 11 else digits
        c = await s.scalar(select(Card).where(Card.requisites == req).order_by(Card.id.desc()).limit(1))
        if c:
            await admin_card_screen(bot, s, admin, c)
            return True
    name = q.lstrip("@")
    found = await s.scalar(select(User).where(
        User.id == int(name) if name.isdigit() else func.lower(User.username) == name.lower()))
    if found:
        await user_screen(bot, s, admin, found)
    return found is not None


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
            + (f" · {extra}" if extra else ""))


WD_LABEL = {"queued": "в очереди", "cancelled": "отменён пользователем", "pending": "отправляется",
            "sending": "отправляется", "sent": "в сети", "unknown": "требует проверки",
            "done": "выполнен", "failed": "не выполнен, возвращён"}
DEP_LABEL = {"new": "создаётся", "active": "ждёт оплаты", "paid": "зачислен", "expired": "истёк", "failed": "не создан",
             "cancelled": "отменён пользователем", "small": "меньше минимума"}


async def user_screen(bot: Bot, s: AsyncSession, admin: User, u: User, src=None, note: str = "", found=None):
    st = await user_stats(s, u.id)
    oper = await s.get(Operator, u.id)
    team = await s.get(Team, u.team_id) if u.team_id else None
    led = await teams.led_by(s, u.id)
    om = await s.get(OrderMerchant, u.id)
    is_op = await operators.is_operator(s, u.id)
    roles = [r for r in (
        admins.role(u.id),
        "оператор" if is_op else "",
        "ордерный мерчант" if om and om.status == "approved" else "",
        f"тимлид {alink('team', led.id, f'«{esc(led.name)}»')}" if led and led.status in ("approved", "suspended") else "",
        f"в команде {alink('team', team.id, f'«{esc(team.name)}»')}" if team and (not led or led.id != team.id) else "")
        if r]
    rate, pct_ = settings.buyer_terms(u)
    status = ("заблокирован" if u.is_banned else "активен") + (" · на смене" if u.is_online else "")
    head = []
    if found:
        kind, op = found
        head = [f"{pe('search')} Найдено: {'вывод' if kind == 'wd' else 'пополнение'} #{op.id} · "
                f"{(WD_LABEL if kind == 'wd' else DEP_LABEL).get(op.status, op.status)}", ""]
    in_team = team is not None and team.leader_id != u.id
    owner = admins.is_owner(admin.id) and not admins.is_owner(u.id) and u.id != admin.id
    await show(bot, admin, "\n".join(head + [
        title(pe("profile"), f"Пользователь · {u.id}") + f" · {status}",
        "",
        fields(
            cf("Кто", f"{mention(u)}" + (f" · @{esc(u.username)}" if u.username else "") + f" · <code>{u.id}</code>",
               icon="profile"),
            cf("Роли", *roles, icon="lock"),
            cf("Баланс", f"доступно <b>{money.usdt(u.balance)} USDT</b> · заморожено {money.usdt(u.frozen)}",
               f"командный {money.usdt(u.team_balance)} USDT" if u.team_balance else "",
               f"не прокручено пополнений {money.usdt(u.deposit_lock)} USDT · вывести можно "
               f"{money.usdt(money.withdrawable(u))}" if u.deposit_lock else "",
               f"долг оператора <b>{money.usdt(oper.debt)} USDT</b>" if oper and oper.debt else "",
               f"личные условия покупки: курс {money.fmt(rate)} ₽ · {money.fmt(pct_, 3)}%" if settings.has_terms(u)
               else "",
               f"личная ставка по карте: {money.fmt(u.pct_static, 3)}%" if u.pct_static is not None else "",
               icon="wallet"),
            cf("Сделки", _role_line("Покупки", st["buy"]), _role_line("Продажи", st["sell"]),
               f"Споры: {st['disputes'][0]} открыто · {st['disputes'][1]} всего", icon="fire"),
            cf("Кошелёк", f"пополнено <b>{money.usdt(st['deposited'])} USDT</b> · выведено "
               f"<b>{money.usdt(st['withdrawn'])} USDT</b>" + (f" · на проверке {st['checking']}" if st["checking"] else ""),
               icon="dollar"),
            cf("Ордерный мерчант", f"пропуски реквизитов: {om.strikes} из {settings.get('strike_limit')} подряд"
               if om.strikes else "", f"<b>пауза до {at(om.sleep_until, 'dt')}</b>" if orders.asleep(om) else "",
               icon="key") if om and om.status in ("approved", "suspended") else "",
            cf("Сообщество", ("в чате" if u.in_chat else "не в чате") + " · "
               + ("подписан на канал" if u.in_channel else "не подписан на канал"), icon="people")
            if settings.get("chat_id") or settings.get("channel_id") else "",
            cf("Активность", f"зарегистрирован {at(u.created_at, 'd')}", f"последний раз {at(u.last_seen, 'dt')}",
               f"вход: {ACCESS.get(u.access, u.access)}" if u.access != "approved" else "", icon="clock"),
        ),
    ]) + note, kb(
        btn(f"{'Вывод' if found[0] == 'wd' else 'Пополнение'} #{found[1].id} · "
            f"{(WD_LABEL if found[0] == 'wd' else DEP_LABEL).get(found[1].status, found[1].status)}",
            f"{'awv' if found[0] == 'wd' else 'adp'}:{found[1].id}", "search", style="primary") if found else None,
        [btn("Сделки", f"aud:{u.id}", "fire"), btn("Операции", f"auh:{u.id}", "list")],
        [btn("Карты", f"auc:{u.id}", "card"), btn("Ввод и вывод", f"auw:{u.id}", "wallet")],
        [btn("История", f"aev:user:{u.id}", "list"),
         btn("Мерчант", f"aom:{u.id}", "key") if om and om.status != "rejected" else None],
        [btn("Написать", f"dm:0:{u.id}", "support"), btn("Баланс", f"bal:u:{u.id}", "dollar")],
        [btn("Ставка мерчанта" + (" · своя" if u.pct_static is not None else ""), f"aup:{u.id}", "star"),
         btn("Условия покупки" + (" · свои" if settings.has_terms(u) else ""), f"aut:{u.id}", "star")],
        btn("Оператор: долг и ордера", f"aop:{u.id}", "shop") if is_op or (oper and oper.debt) else None,
        btn("Убрать из команды", f"aux:{u.id}", "cross") if in_team else None,
        btn("Снять ограничение вывода", f"aul:{u.id}", "up") if u.deposit_lock else None,
        (btn("Снять права админа", f"aga:{u.id}:0", "lock", style="danger") if admins.is_admin(u.id)
         else btn("Сделать админом", f"aga:{u.id}", "lock")) if owner else None,
        [btn("Снять со смены", f"auo:{u.id}", "pause") if u.is_online else None,
         btn("Разблокировать", f"aub:{u.id}:0", "ok", style="success") if u.is_banned
         else btn("Заблокировать", f"aub:{u.id}:1", "ban", style="danger")],
        back("au", "Назад"),
    ), src)


ACCESS = {"new": "ещё не подал заявку", "pending": "заявка на рассмотрении", "rejected": "заявка отклонена"}


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
    if not u or admins.is_admin(u.id):
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
    if not u or admins.is_admin(u.id):
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


@router.callback_query(F.data.regexp(r"^aul:(\d+)$"))
async def cb_unlock(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """The admin lets a user withdraw a deposit he did not turn over (a refund, a mistake)."""
    u = await money.lock(s, int(c.data.split(":")[1]))
    was, u.deposit_lock = u.deposit_lock, Decimal(0)
    audit.log(s, user.id, "deposit_unlock", f"user:{u.id}", f"{was} USDT")
    events.add(s, f"user:{u.id}", "unlock", f"Снято ограничение вывода на {money.usdt(was)} USDT ({user.name})",
               u.id, alert=True)
    await s.commit()
    await user_screen(bot, s, user, u, c, ok(f"Ограничение снято: вывести можно {money.usdt(u.balance)} USDT"))


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
    main = Ledger.kind.not_in(money.TEAM)
    total, frozen = (await s.execute(select(func.coalesce(func.sum(Ledger.delta), 0),
                                            func.coalesce(func.sum(Ledger.frozen_delta), 0))
                                     .where(Ledger.user_id == uid, main))).one()
    team = Decimal(await s.scalar(select(func.coalesce(func.sum(Ledger.delta), 0))
                                  .where(Ledger.user_id == uid, Ledger.kind.in_(money.TEAM))))
    u = await s.get(User, uid)
    ok_total, ok_frozen = Decimal(total) == u.balance + u.frozen, Decimal(frozen) == u.frozen
    check = (f"{pe('ok')} Журнал сходится: доступно {money.usdt(u.balance)} + заморожено {money.usdt(u.frozen)} USDT"
             + (f" · командный {money.usdt(u.team_balance)}" if u.team_balance or team else "")
             if ok_total and ok_frozen and team == u.team_balance else
             f"{pe('warn')} Расхождение: журнал {money.usdt(Decimal(total))} / {money.usdt(Decimal(frozen))} / "
             f"командный {money.usdt(team)}, баланс {money.usdt(u.balance + u.frozen)} / {money.usdt(u.frozen)} / "
             f"{money.usdt(u.team_balance)} USDT")
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
    ]), kb(*[btn(label, f"amr:{code}", "pencil") for code, label in ADJ_REASONS.items() if code != "manual"],
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
    people = {x.id: x for x in (await s.scalars(select(User).where(User.id.in_(
        [i for i in (a.admin_id, a.approved_by) if i])))).all()}
    lines = [
        title(pe("dollar"), f"Корректировка {alink('adj', a.id, f'#{a.id}')}") + f" · {ADJ_STATUS[a.status]}",
        "",
        fields(
            cf("Пользователь", ulink(u), icon="profile"),
            cf("Начислить" if a.delta > 0 else "Списать", f"<b>{money.usdt(abs(a.delta))} USDT</b>", icon="dollar"),
            cf("Причина", esc(_adj_reason(a)), icon="info"),
            cf("Доступно", f"{money.usdt(a.balance_before)} → <b>{money.usdt(a.balance_after)} USDT</b>"
               + (" (на момент черновика)" if a.status in ("draft", "pending") else ""), icon="wallet"),
            cf("Кто", f"создал {ulink(people.get(a.admin_id), a.admin_id)} · {at(a.created_at, 'dt')}",
               f"подтвердил {ulink(people.get(a.approved_by), a.approved_by)}" if a.approved_by else "",
               f"проведена {at(a.done_at, 'dt')}" if a.done_at else "", icon="lock"),
        ),
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
        *rows, btn("История", f"aev:adj:{a.id}", "list")), src)


@router.callback_query(F.data.regexp(r"^adjv:(\d+)$"))
async def cb_adjustment(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    a = await s.get(Adjustment, int(c.data.split(":")[1]))
    if a:
        await adjustment_screen(bot, s, user, a, c)


async def apply_adjustment(s: AsyncSession, adj_id: int, admin: User, quiet: bool = False) -> tuple[str, Adjustment]:
    """Idempotent: the row lock plus the status check make a repeated click a no-op. quiet: a mass action
    (/balance) — the done step stays in the history but posts nothing; the action posts one summary itself."""
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
    audit.log(s, admin.id, "balance", f"user:{a.user_id}", f"adj #{a.id}: {a.delta} USDT, {_adj_reason(a)}",
              alert=not quiet)
    events.add(s, ref, "done", f"{'+' if a.delta > 0 else ''}{money.usdt(a.delta)} USDT пользователю {a.user_id}: "
                               f"{money.usdt(a.balance_before)} → {money.usdt(a.balance_after)} ({_adj_reason(a)})",
               a.user_id, alert=not quiet)
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
    busy = (await deals.busy_cards(s, owner.id, full=True)).get(card.id)
    visible, why = deals.card_visibility(card, owner, busy, (await deals.used_today(s, [card.id]))[card.id])
    state = "Удалена владельцем" if card.is_deleted else "Заблокирована" if card.is_banned \
        else "Включена" if card.is_active else "Выключена"
    await show(bot, admin, "\n".join([
        title(pe("card"), f"Карта {alink('card', card.id, f'#{card.id}')}") + f" · {state}",
        "",
        fields(
            cf("Покупателям", f"{'видна' if visible else 'не видна'}: {why}", icon=card_icon(card)),
            cf("Реквизиты", f"{esc(card.bank)} · {'СБП' if card.kind == 'sbp' else 'карта'}",
               f"<code>{esc(card.requisites)}</code>", esc(card.holder), icon="bank"),
            cf("Суммы", f"{money.fmt(card.min_rub)} – {money.fmt(card.max_rub)} ₽"
               + (f" · лимит в день {money.fmt(card.daily_limit_rub)} ₽" if card.daily_limit_rub else ""), icon="ruble"),
            cf("Владелец", ulink(owner), icon="profile"),
            cf("Сейчас в сделке", alink("deal", busy, f"#{busy}"), icon="fire") if busy else "",
        ),
        "",
        quote("«Выключить» — владелец может включить снова; «Заблокировать» — только администрация."),
    ]) + note, kb(
        btn("Выключить", f"aco:{card.id}", "pause") if card.is_active else
        btn("Поставить в поток", f"amc:on:{card.id}", "ok", style="success") if not card.is_banned and not card.is_deleted
        else None,
        [btn("Минимум", f"amc:min:{card.id}", "down"), btn("Максимум", f"amc:max:{card.id}", "up")]
        if not card.is_deleted else None,
        btn("Лимит в день", f"amc:daily:{card.id}", "clock") if not card.is_deleted else None,
        btn("Разблокировать", f"acb:{card.id}:0", "ok", style="success") if card.is_banned
        else btn("Заблокировать", f"acb:{card.id}:1", "ban", style="danger"),
        btn(f"Сделка #{busy}", f"adv:{busy}", "fire") if busy else None,
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
    await show(bot, user, title(pe("list"), "Сделки") + "\n\nКонкретная сделка по номеру: команда <code>/deal 15</code>, "
               "«Найти» → <code>#15</code> или «Открыть» на её карточке в лог-чате.", kb(
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


@router.callback_query(F.data.regexp(r"^af:(\d+)$"))
async def cb_files(c: CallbackQuery, bot: Bot, s: AsyncSession):
    d = await s.get(Deal, int(c.data.split(":")[1]))
    await c.answer()
    if d:
        with suppress(TelegramAPIError):
            chat, topic = files_to(c)
            await send_files(bot, chat, d, topic)


VERDICTS = {"b": "в пользу покупателя", "s": "в пользу продавца (отмена)", "a": "по фактической сумме",
            "c": "отмена неоплаченной сделки", "n": "в пользу покупателя, USDT оператору не пришли"}


@router.callback_query(F.data.regexp(r"^ar:(\d+):([bsacn])$"))
async def cb_resolve_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    _, did, verdict = c.data.split(":")
    d = await s.get(Deal, int(did), populate_existing=True)
    if not verdict_allowed(d, verdict):
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


@router.callback_query(F.data.regexp(r"^ar3:(\d+):([bsacn])$"))
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


@router.callback_query(F.data.regexp(r"^ar2:(\d+):([bsacn])$"))
async def cb_resolve(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, did, verdict = c.data.split(":")
    await resolve(bot, s, user, int(did), verdict, "", c)


def verdict_allowed(d: Deal | None, verdict: str) -> bool:
    """The same rule for the bot and the mini app: which verdicts a deal in its current state can take."""
    if d is None or verdict not in VERDICTS:
        return False
    allowed = deals.UNPAID if verdict == "c" else ("paid", "dispute")
    return (d.status in allowed and not (verdict == "a" and d.dispute_amount_rub is None)
            and not (verdict == "n" and not (d.via_bybit and d.bybit_url)))


async def apply_verdict(bot: Bot, s: AsyncSession, user: User, did: int, verdict: str,
                        comment: str) -> tuple[Deal | None, str]:
    """Carry out an admin's verdict, commit, tell both sides. (deal, "") or (None, why). The bot and the app alike."""
    d = await s.get(Deal, did, populate_existing=True)
    if not verdict_allowed(d, verdict):
        return None, "Сделка уже закрыта или решение недоступно"
    error = ""
    try:
        if verdict == "c" and d.status in orders.REQUEST:
            res = await orders.cancel(s, d.id, "void", "admin_void")
        elif verdict in ("s", "c"):
            res = (await deals.cancel(s, d.id, ("waiting_payment",), "void", "admin_void") if verdict == "c"
                   else await deals.cancel(s, d.id, ("paid", "dispute"), "cancelled", "dispute_seller"))
        else:
            res = await deals.complete(s, d.id, actual_rub=d.dispute_amount_rub if verdict == "a" else None,
                                       reason="dispute_actual" if verdict == "a" else "dispute_buyer",
                                       operator_debt=verdict != "n")
    except deals.DealError as e:
        res, error = None, str(e)
    if not res:
        await s.rollback()
        await s.refresh(user)  # rollback expires every loaded object
        return None, error or "Сделка уже закрыта другим администратором"
    res.resolution = comment or None
    if verdict == "c" and res.is_order and res.card_id is None:  # a request: its offers in chats and bots close
        from bot.handlers.orders import CLOSED, close_offers
        await close_offers(bot, s, res, f"Заявка #{res.id} {CLOSED['void']}")
    audit.log(s, user.id, "resolve", f"deal:{res.id}", VERDICTS[verdict] + (f": {comment}" if comment else ""))
    events.add(s, f"deal:{res.id}", "resolved", f"Решено {VERDICTS[verdict]} ({user.name})"
               + (f": {comment}" if comment else ""), notice=True)
    await s.commit()
    head = (f"Сделка #{res.id} отменена администрацией. Не переводите по ней деньги" if verdict == "c"
            else f"Спор по сделке #{res.id} решён {VERDICTS[verdict]}")
    await push(bot, s, res.buyer_id, res, head)
    for uid in deals.sellers(res):
        await push(bot, s, uid, res, head)
    return res, ""


async def resolve(bot: Bot, s: AsyncSession, user: User, did: int, verdict: str, comment: str, src=None):
    if not await s.get(Deal, did):
        return await admin_screen(bot, s, user, src)
    res, error = await apply_verdict(bot, s, user, did, verdict, comment)
    if not res:
        d = await s.get(Deal, did, populate_existing=True)
        return await deal_view(bot, s, user, d, src, warn(error))
    await deal_view(bot, s, user, res, src, ok(f"Решено {VERDICTS[verdict]}. Стороны уведомлены."))
