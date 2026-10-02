"""Admin panel: operators (add / remove, debt, manual write-off), teams (applications, suspend, leader's percent) and
a user's personal buyer terms (rate and percent, like an API client's own terms)."""
from decimal import Decimal
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.handlers.admin_api import terms_lines
from bot.handlers.wallet import parse_usdt
from bot.models import Deal, Operator, Team, User, now
from bot.services import audit, events, money, operators, settings, teams
from bot.ui import at, deep_link, esc, notify, ok, quote, show, title, warn

router = Router()
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))


class AdmPeople(StatesGroup):
    operator = State()
    writeoff = State()
    team_reason = State()
    team_pct = State()
    terms = State()


def who(u: User | None, uid: int) -> str:
    if u is None:
        return f"<code>{uid}</code>"
    return " · ".join(p for p in (f"@{esc(u.username)}" if u.username else "", esc(u.name or ""),
                                  f"<code>{uid}</code>") if p)


# ---------- operators ----------

@router.callback_query(F.data == "aopl")
async def cb_operators(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await operators_screen(bot, s, user, c)


async def operators_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    active = await operators.ids(s)
    rows = {op.user_id: op for op in (await s.scalars(select(Operator))).all()}
    shown = list(dict.fromkeys([*active, *[uid for uid, op in rows.items() if op.debt > 0]]))
    users = {u.id: u for u in (await s.scalars(select(User).where(User.id.in_(shown)))).all()} if shown else {}
    free = await s.scalar(select(func.count(Deal.id)).where(Deal.status == "checking", Deal.operator_id.is_(None)))
    total = await operators.total_debt(s)
    fallback = not config.operator_ids and not any(op.active for op in rows.values())
    await show(bot, admin, "\n".join([
        title(pe("shop"), "Операторы Bybit-ордеров"),
        quote(f"• Операторов: <b>{len(active)}</b>" + (" — это админы: своих операторов ещё нет" if fallback else ""),
              f"• Долг операторов всего: <b>{money.usdt(total)} USDT</b>",
              f"• Ордеров ждут оператора: <b>{free}</b>"),
        "Оператор получает ссылку на Bybit-ордер, выдаёт его реквизиты и подтверждает оплату. USDT ордера приходят "
        "ему на Bybit — это его долг, он гасит его счётом xRocket или с баланса.",
        f"Из .env (OPERATOR_IDS): {', '.join(map(str, config.operator_ids))}" if config.operator_ids else "",
    ]) + note, kb(
        btn("Добавить оператора", "aop:add", "plus", style="success"),
        *[btn(f"{((users[uid].name if uid in users else '') or str(uid))[:22]} · долг "
              f"{money.usdt(rows[uid].debt if uid in rows else Decimal(0))}"
              + ("" if uid in active else " · убран"), f"aop:{uid}", "profile") for uid in shown],
        back("a", "Админ-панель")), src)


@router.callback_query(F.data == "aop:add")
async def cb_operator_add(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(AdmPeople.operator)
    await show(bot, user, "\n".join([
        title(pe("plus"), "Новый оператор"),
        "Отправьте Telegram ID или @username. Человек должен хотя бы раз запустить бота (/start)."]),
        kb(back("aopl", "Отмена")), c)


@router.message(AdmPeople.operator, F.text)
async def msg_operator_add(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    raw = m.text.strip().lstrip("@")
    u = await s.scalar(select(User).where(User.id == int(raw) if raw.isdigit() else
                                          func.lower(User.username) == raw.lower()))
    if u is None:
        return await show(bot, user, title(pe("plus"), "Новый оператор") + "\n\nОтправьте ID или @username."
                          + warn("Не найден: пусть человек запустит бота и попробуйте снова"), kb(back("aopl", "Отмена")))
    await state.clear()
    op = await operators.row(s, u.id, lock=True)
    op.active, op.added_by = True, user.id
    audit.log(s, user.id, "operator_add", f"op:{u.id}")
    events.add(s, f"op:{u.id}", "added", f"Назначен оператором ({user.name})", u.id, alert=True)
    await s.commit()
    await notify(bot, u.id, "\n".join([
        f"{pe('shop')} <b>Вы — оператор Strait Pay</b>",
        "• Когда мерчант пришлёт Bybit-ордер, вам придёт сообщение с кнопкой «Принять ордер»",
        "• Кто первым принял — получает ссылку, у остальных ордер пропадает",
        "• Зайдите в ордер, выдайте покупателю реквизиты, проверьте оплату и подтвердите",
        "• USDT ордера приходят вам на Bybit — это ваш долг, гасите его в «Оператор» счётом xRocket"]),
        kb(btn("Кабинет оператора", "op", "shop", style="success"), back("x", "Скрыть", "cross")))
    await operator_card(bot, s, user, u.id, note=ok("Оператор добавлен и уведомлён"))


async def operator_card(bot: Bot, s: AsyncSession, admin: User, uid: int, src=None, note: str = ""):
    u = await s.get(User, uid)
    op = await s.get(Operator, uid, populate_existing=True)
    active = uid in await operators.ids(s)
    n, usdt = (await s.execute(select(func.count(Deal.id), func.coalesce(func.sum(Deal.seller_debit), 0)).where(
        Deal.operator_id == uid, Deal.via_bybit, Deal.status == "completed"))).one()
    working = await s.scalar(select(func.count(Deal.id)).where(
        Deal.operator_id == uid, Deal.status.in_(("checking", "waiting_payment", "paid", "dispute"))))
    env = uid in config.operator_ids
    await show(bot, admin, "\n".join([
        title(pe("shop"), "Оператор"),
        quote(f"• Кто: {who(u, uid)}",
              "• Статус: " + ("<b>активен</b>" + (" (из .env)" if env else "") if active else "убран"),
              f"• Долг: <b>{money.usdt(op.debt if op else Decimal(0))} USDT</b>",
              f"• Подтверждено ордеров: {n} на {money.usdt(Decimal(usdt))} USDT · в работе сейчас {working}",
              f"• Последняя активность: {at(u.last_seen, 'dt')}" if u else ""),
    ]) + note, kb(
        (btn("Убрать из операторов", f"aop:st:{uid}:0", "pause", style="danger") if active and not env else
         btn("Вернуть в операторы", f"aop:st:{uid}:1", "ok", style="success") if not active else None),
        btn("Списать долг вручную", f"aop:wo:{uid}", "dollar") if op and op.debt > 0 else None,
        [btn("Профиль", f"auv:{uid}", "profile"), btn("История", f"aev:op:{uid}", "list")],
        back("aopl", "Операторы")), src)


@router.callback_query(F.data.regexp(r"^aop:(\d+)$"))
async def cb_operator(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await operator_card(bot, s, user, int(c.data.split(":")[1]), c)


@router.callback_query(F.data.regexp(r"^aop:st:(\d+):([01])$"))
async def cb_operator_status(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, _, uid, on = c.data.split(":")
    uid = int(uid)
    if not await s.get(User, uid):
        return await c.answer("Пользователь не найден", show_alert=True)
    op = await operators.row(s, uid, lock=True)
    op.active = on == "1"
    what = "вернули в операторы" if op.active else "убрали из операторов"
    audit.log(s, user.id, "operator_status", f"op:{uid}", what)
    events.add(s, f"op:{uid}", "status", f"Оператора {what} ({user.name})", uid, alert=True)
    await s.commit()
    await notify(bot, uid, f"{pe('shop')} Администрация {what.replace('вернули', 'вернула').replace('убрали', 'убрала')} "
                           "вас." + (f" Долг {money.usdt(op.debt)} USDT остаётся — погасите его в «Оператор»."
                                     if not op.active and op.debt > 0 else ""))
    await operator_card(bot, s, user, uid, c, ok(what.capitalize()))


@router.callback_query(F.data.regexp(r"^aop:wo:(\d+)$"))
async def cb_writeoff(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    uid = int(c.data.split(":")[2])
    op = await s.get(Operator, uid)
    await state.set_state(AdmPeople.writeoff)
    await state.set_data({"uid": uid})
    await show(bot, user, "\n".join([
        title(pe("dollar"), "Списать долг вручную"),
        quote(f"• Долг сейчас: <b>{money.usdt(op.debt if op else Decimal(0))} USDT</b>"),
        "Используйте, если оператор вернул USDT в обход бота (например, переводом на xRocket площадки). "
        "Отправьте сумму в USDT:"]), kb(back(f"aop:{uid}", "Отмена")), c)


@router.message(AdmPeople.writeoff, F.text)
async def msg_writeoff(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    uid = (await state.get_data())["uid"]
    v = parse_usdt(m.text)
    if v is None:
        return await show(bot, user, title(pe("dollar"), "Списать долг вручную") + "\n\nОтправьте сумму в USDT."
                          + warn("Нужно положительное число"), kb(back(f"aop:{uid}", "Отмена")))
    await state.clear()
    paid, extra = await operators.repay(s, uid, v, f"вручную, администратор {user.id}")
    audit.log(s, user.id, "operator_writeoff", f"op:{uid}", f"{paid} USDT")
    await s.commit()
    await notify(bot, uid, f"{pe('ok')} Администрация зачла погашение долга: {money.usdt(paid)} USDT.")
    await operator_card(bot, s, user, uid, note=ok(f"Списано {money.usdt(paid)} USDT"
                                                   + (f" (больше долга на {money.usdt(extra)} — не учтено)" if extra else "")))


# ---------- teams ----------

@router.callback_query(F.data == "atml")
async def cb_teams(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    rows = (await s.scalars(select(Team).order_by((Team.status == "pending").desc(), Team.id.desc()).limit(30))).all()
    paid = Decimal(await s.scalar(select(func.coalesce(func.sum(Deal.team_fee), 0))))
    await show(bot, user, "\n".join([
        title(pe("people"), "Команды и тимлиды"),
        quote(f"• Работают: <b>{sum(1 for t in rows if t.status == 'approved')}</b> · заявок: "
              f"<b>{sum(1 for t in rows if t.status == 'pending')}</b>",
              f"• Тимлидам по умолчанию: <b>{money.fmt(settings.dec('team_pct'), 3)}%</b> от сделок участников",
              f"• Выплачено тимлидам всего: {money.usdt(paid)} USDT"),
        "Заявки на рассмотрении — сверху.",
    ]), kb(*[btn(f"{teams.STATUS[t.status]} · {t.name[:24]}", f"atm:{t.id}", "people",
                 style="primary" if t.status == "pending" else None) for t in rows],
           btn("Процент тимлидов", "acs:team_pct", "percent"),
           back("a", "Админ-панель")), c)


async def team_card(bot: Bot, s: AsyncSession, admin: User, t: Team, src=None, note: str = ""):
    leader = await s.get(User, t.leader_id)
    n, rub, fee = await teams.stats(s, t)
    await show(bot, admin, "\n".join([
        title(pe("people"), f"Команда «{esc(t.name)}» · {teams.STATUS[t.status]}"),
        quote(f"• Тимлид: {who(leader, t.leader_id)}",
              f"• Участников: {await teams.members(s, t)}",
              f"• Чат: <code>{t.chat_id}</code>" if t.chat_id else "• Чат: не подключён",
              f"• Процент тимлида: <b>{money.fmt(teams.pct(t), 3)}%</b>" + (" (личный)" if t.pct is not None
                                                                             else " (общий)"),
              f"• Сделок участников: {n} на {money.fmt(rub)} ₽ · тимлиду выплачено {money.usdt(fee)} USDT",
              f"• Заявка {at(t.created_at, 'dt')}" + (f" · решение {at(t.decided_at, 'dt')}" if t.decided_at else "")),
        f"<b>О себе</b>\n{esc(t.about)}" if t.about else "",
        f"Причина отказа: <i>{esc(t.reason)}</i>" if t.reason else "",
    ]) + note, kb(
        [btn("Одобрить", f"atm:ok:{t.id}", "ok", style="success"),
         btn("Отклонить", f"atm:no:{t.id}", "cross", style="danger")] if t.status == "pending" else None,
        btn("Приостановить", f"atm:st:{t.id}:0", "pause", style="danger") if t.status == "approved" else None,
        btn("Возобновить", f"atm:st:{t.id}:1", "ok", style="success") if t.status == "suspended" else None,
        btn("Процент тимлида", f"atm:pct:{t.id}", "percent") if t.status in ("approved", "suspended") else None,
        [btn("Тимлид", f"auv:{t.leader_id}", "profile"), btn("История", f"aev:team:{t.id}", "list")],
        back("atml", "Команды")), src)


@router.callback_query(F.data.regexp(r"^atm:(\d+)$"))
async def cb_team(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    t = await s.get(Team, int(c.data.split(":")[1]))
    if not t:
        return await c.answer("Команда не найдена", show_alert=True)
    await team_card(bot, s, user, t, c)


@router.callback_query(F.data.regexp(r"^atm:ok:(\d+)$"))
async def cb_team_approve(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    t = await s.get(Team, int(c.data.split(":")[2]), with_for_update=True, populate_existing=True)
    if not t or t.status != "pending":
        return await c.answer("Заявка уже рассмотрена", show_alert=True)
    t.status, t.admin_id, t.decided_at = "approved", user.id, now()
    (await s.get(User, t.leader_id)).team_id = t.id
    audit.log(s, user.id, "team_approve", f"team:{t.id}")
    events.add(s, f"team:{t.id}", "approved", f"Команда одобрена ({user.name})", t.leader_id, alert=True)
    await s.commit()
    await notify(bot, t.leader_id, "\n".join([
        f"{pe('ok')} <b>Команда «{esc(t.name)}» одобрена — вы тимлид</b>",
        f"• Ваша реферальная ссылка: <code>{esc(await deep_link(bot, f't{t.id}'))}</code>",
        f"• Вы получаете {money.fmt(teams.pct(t), 3)}% от каждой сделки участника на баланс",
        "• Подключите чат: добавьте бота админом группы и отправьте в ней /team"]),
        kb(btn("Кабинет тимлида", "tm", "people", style="success"), back("x", "Скрыть", "cross")))
    await team_card(bot, s, user, t, c, ok("Одобрена, тимлид уведомлён"))


@router.callback_query(F.data.regexp(r"^atm:no:(\d+)$"))
async def cb_team_reject_ask(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    tid = int(c.data.split(":")[2])
    await state.set_state(AdmPeople.team_reason)
    await state.set_data({"team": tid})
    await show(bot, user, f"{title(pe('cross'), 'Отказ по команде')}\n\nНапишите причину (5–500 символов) — "
                          "её увидит заявитель.", kb(back(f"atm:{tid}", "Отмена")), c)


@router.message(AdmPeople.team_reason, F.text)
async def msg_team_reject(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    tid = (await state.get_data())["team"]
    reason = " ".join(m.text.split())
    if not 5 <= len(reason) <= 500:
        return await show(bot, user, title(pe("cross"), "Причина отказа") + "\n\nНапишите причину ещё раз."
                          + warn("От 5 до 500 символов"), kb(back(f"atm:{tid}", "Отмена")))
    await state.clear()
    t = await s.get(Team, tid, with_for_update=True, populate_existing=True)
    if not t or t.status != "pending":
        return await show(bot, user, warn("Заявка уже рассмотрена"), kb(back("atml", "Команды")))
    t.status, t.admin_id, t.decided_at, t.reason = "rejected", user.id, now(), reason
    audit.log(s, user.id, "team_reject", f"team:{tid}", reason)
    events.add(s, f"team:{tid}", "rejected", f"Заявка отклонена ({user.name}): {reason}", t.leader_id, alert=True)
    await s.commit()
    await notify(bot, t.leader_id, f"{pe('cross')} <b>Заявка на команду «{esc(t.name)}» отклонена</b>\n"
                                   f"• Причина: {esc(reason)}")
    await team_card(bot, s, user, t, note=ok("Отклонена, заявитель уведомлён"))


@router.callback_query(F.data.regexp(r"^atm:st:(\d+):([01])$"))
async def cb_team_status(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, _, tid, on = c.data.split(":")
    t = await s.get(Team, int(tid), with_for_update=True, populate_existing=True)
    if not t or t.status not in ("approved", "suspended"):
        return await c.answer()
    t.status = "approved" if on == "1" else "suspended"
    what = "возобновлена" if on == "1" else "приостановлена"
    audit.log(s, user.id, "team_status", f"team:{tid}", what)
    events.add(s, f"team:{tid}", "status", f"Команда {what} ({user.name})", t.leader_id, alert=True)
    await s.commit()
    await notify(bot, t.leader_id, f"{pe('people')} Команда «{esc(t.name)}» {what} администрацией."
                 + (" Пока она приостановлена, процент с её сделок не начисляется и заявки в чат не приходят."
                    if on == "0" else ""))
    await team_card(bot, s, user, t, c, ok(what.capitalize()))


@router.callback_query(F.data.regexp(r"^atm:pct:(\d+)$"))
async def cb_team_pct(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    t = await s.get(Team, int(c.data.split(":")[2]))
    if not t:
        return await c.answer()
    await state.set_state(AdmPeople.team_pct)
    await state.set_data({"team": t.id})
    await show(bot, user, "\n".join([
        title(pe("percent"), f"Процент тимлида · «{esc(t.name)}»"),
        quote(f"• Сейчас: <b>{money.fmt(teams.pct(t), 3)}%</b>",
              f"• Общий: {money.fmt(settings.dec('team_pct'), 3)}%"),
        "Отправьте процент (например, <code>1.5</code>) или «-» — общий. Платит площадка из своего дохода по сделке, "
        "не больше него."]), kb(back(f"atm:{t.id}", "Отмена")), c)


@router.message(AdmPeople.team_pct, F.text)
async def msg_team_pct(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    tid = (await state.get_data())["team"]
    value = _parse(m.text, "pct")
    if value is False:
        return await show(bot, user, title(pe("percent"), "Процент тимлида") + "\n\nОтправьте процент или «-»."
                          + warn("Процент от 0 до 99,999, до 3 знаков"), kb(back(f"atm:{tid}", "Отмена")))
    await state.clear()
    t = await s.get(Team, tid)
    old, t.pct = t.pct, value
    audit.log(s, user.id, "team_pct", f"team:{tid}", f"{old} → {value}")
    events.add(s, f"team:{tid}", "pct", f"Процент тимлида: {old if old is not None else 'общий'} → "
               f"{value if value is not None else 'общий'} ({user.name})", t.leader_id, alert=True)
    await notify(bot, t.leader_id, f"{pe('percent')} <b>Ваш процент тимлида: {money.fmt(teams.pct(t), 3)}%</b> — "
                                   "для новых сделок команды.")
    await team_card(bot, s, user, t, note=ok("Сохранено"))


def _parse(raw: str, field: str):
    """Decimal, None for «-» (back to the general value) or False if invalid."""
    raw = raw.strip().replace(",", ".").replace(" ", "")
    if raw == "-":
        return None
    try:
        v = Decimal(raw)
    except Exception:  # noqa: BLE001
        return False
    ok_ = (v.is_finite() and 0 < v < 10_000_000 and v.as_tuple().exponent >= -2 if field == "rate"
           else v.is_finite() and 0 <= v < 100 and v.as_tuple().exponent >= -3)
    return v if ok_ else False


# ---------- personal buyer terms ----------

TERMS = {"rate": "Личный курс покупателя, ₽ за 1 USDT", "pct": "Личный процент с покупателя"}


def _terms_view(u: User) -> list[str]:
    return terms_lines(SimpleNamespace(rate=u.buy_rate, pct=u.buy_pct))


async def terms_screen(bot: Bot, admin: User, u: User, src=None, note: str = ""):
    await show(bot, admin, "\n".join([
        title(pe("star"), "Условия покупателя"),
        f"Пользователь: {who(u, u.id)}",
        quote(*[f"• {line}" for line in _terms_view(u)]),
        "Личные курс и процент действуют на все его покупки — по картам и ордерным реквизитам, как у API-клиента. "
        "Сторона мерчанта не меняется, разница — площадке; сделка, где площадка ушла бы в минус, не создаётся.",
    ]) + note, kb([btn("Курс", f"aut:set:{u.id}:rate", "swap"), btn("Процент", f"aut:set:{u.id}:pct", "percent")],
                  btn("Сбросить на общие", f"aut:rs:{u.id}", "refresh") if settings.has_terms(u) else None,
                  back(f"auv:{u.id}", "Профиль")), src)


@router.callback_query(F.data.regexp(r"^aut:(\d+)$"))
async def cb_terms(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    u = await s.get(User, int(c.data.split(":")[1]))
    if not u:
        return await c.answer("Пользователь не найден", show_alert=True)
    await terms_screen(bot, user, u, c)


@router.callback_query(F.data.regexp(r"^aut:set:(\d+):(rate|pct)$"))
async def cb_terms_set(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, uid, field = c.data.split(":")
    u = await s.get(User, int(uid))
    if not u:
        return await c.answer()
    await state.set_state(AdmPeople.terms)
    await state.set_data({"uid": u.id, "field": field})
    await show(bot, user, _terms_ask(u, field), kb(back(f"aut:{u.id}", "Отмена")), c)


def _terms_ask(u: User, field: str, err: str = "") -> str:
    general = (f"{money.fmt(settings.dec('rate'))} ₽" if field == "rate"
               else f"{money.fmt(settings.dec('platform_pct'), 3)}%")
    return "\n".join([
        title(pe("pencil"), TERMS[field]),
        quote(*[f"• {line}" for line in _terms_view(u)]),
        f"Отправьте значение (например, <code>{'98' if field == 'rate' else '4'}</code>) или «-» — общий ({general}). "
        "Действует на новые сделки.",
    ]) + (warn(err) if err else "")


@router.message(AdmPeople.terms, F.text)
async def msg_terms(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    u = await s.get(User, data["uid"])
    field = data["field"]
    value = _parse(m.text, field)
    if value is False:
        return await show(bot, user, _terms_ask(u, field, "Курс — число больше 0, до 2 знаков" if field == "rate"
                                                else "Процент от 0 до 99,999, до 3 знаков"),
                          kb(back(f"aut:{u.id}", "Отмена")))
    await state.clear()
    attr = "buy_rate" if field == "rate" else "buy_pct"
    old = getattr(u, attr)
    setattr(u, attr, value)
    await _terms_changed(bot, s, user, u, f"{TERMS[field]}: {old if old is not None else 'общий'} → "
                                          f"{value if value is not None else 'общий'}")
    await terms_screen(bot, user, u, note=ok("Сохранено"))


@router.callback_query(F.data.regexp(r"^aut:rs:(\d+)$"))
async def cb_terms_reset(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    u = await s.get(User, int(c.data.split(":")[2]))
    if not u:
        return await c.answer()
    u.buy_rate = u.buy_pct = None
    await _terms_changed(bot, s, user, u, "Личные условия покупателя сброшены на общие")
    await terms_screen(bot, user, u, c, ok("Сброшено на общие"))


async def _terms_changed(bot: Bot, s: AsyncSession, admin: User, u: User, what: str) -> None:
    audit.log(s, admin.id, "buyer_terms", f"user:{u.id}", what)
    events.add(s, f"user:{u.id}", "terms", f"{what} ({admin.name})", u.id, alert=True)
    rate, pct = settings.buyer_terms(u)
    await notify(bot, u.id, "\n".join([f"{pe('star')} <b>Ваши условия покупки изменены</b>",
                                       f"• Курс: <b>{money.fmt(rate)} ₽</b> за 1 USDT",
                                       f"• Комиссия: <b>{money.fmt(pct, 3)}%</b>",
                                       "Действуют для новых сделок."]))
