"""Admin panel: operators (add / remove, debt, manual write-off), teams (applications, suspend, leader's percent) and
a user's personal buyer terms (rate and percent, like an API client's own terms)."""
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.services.admins import IsAdmin
from bot.emoji import back, btn, kb, pe
from bot.handlers.admin_api import terms_lines
from bot.handlers.wallet import parse_usdt
from bot.models import Deal, Operator, Team, User, now
from bot.services import admins, audit, deals, events, money, operators, orders, settings, teams
from bot.ui import (BRAND, alink, at, card, cf, deep_link, esc, mark, notify, ok, quote, show, title, ulink,
                    verdict, warn)

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdmPeople(StatesGroup):
    admin = State()
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
        "",
        card(cf("Сейчас", f"операторов: <b>{len(active)}</b>" + (" — это админы: своих ещё нет" if fallback else ""),
                f"долг всего: <b>{money.usdt(total)} USDT</b>", f"ордеров ждут оператора: <b>{free}</b>", icon="stats")),
        "",
        "\n".join(f"{ulink(users.get(uid), uid)} · долг {money.usdt(rows[uid].debt if uid in rows else Decimal(0))} USDT"
                  + ("" if uid in active else " · убран") for uid in shown) if shown else "",
        "",
        quote("Оператор получает ссылку на Bybit-ордер, выдаёт его реквизиты и подтверждает оплату. USDT ордера "
              "приходят ему на Bybit — это его долг, он гасит его переводом USDT (TON) на свой адрес погашения или с "
              "баланса."
              + (f" Из .env (OPERATOR_IDS): {', '.join(map(str, config.operator_ids))}." if config.operator_ids else "")),
    ]).replace("\n\n\n", "\n\n") + note, kb(
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
    u.access = "approved"  # an operator is let in: no entry application stands between him and the orders
    audit.log(s, user.id, "operator_add", f"op:{u.id}")
    events.add(s, f"op:{u.id}", "added", f"Назначен оператором ({user.name})", u.id, alert=True)
    await s.commit()
    await notify(bot, u.id, "\n".join([
        f"{pe('shop')} <b>Вы — оператор Strait Pay</b>",
        "• Когда мерчант пришлёт Bybit-ордер, вам придёт сообщение с кнопкой «Принять ордер»",
        "• Кто первым принял — получает ссылку, у остальных ордер пропадает",
        "• Зайдите в ордер, выдайте покупателю реквизиты, проверьте оплату и подтвердите",
        "• USDT ордера приходят вам на Bybit — это ваш долг, гасите его в «Оператор» переводом USDT (TON)"]),
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
        title(pe("shop"), f"Оператор {alink('op', uid, f'#{uid}')}") + " · " + (
            "активен" + (" (из .env)" if env else "") if active else "убран"),
        "",
        card(cf("Кто", ulink(u, uid), icon="profile"),
             cf("Долг", f"<b>{money.usdt(op.debt if op else Decimal(0))} USDT</b>", icon="dollar"),
             cf("Ордера", f"подтверждено {n} на {money.usdt(Decimal(usdt))} USDT", f"в работе сейчас {working}",
                icon="list"),
             cf("Последняя активность", at(u.last_seen, "dt"), icon="clock") if u else ""),
    ]) + note, kb(
        (btn("Убрать из операторов", f"aop:st:{uid}:0", "pause", style="danger") if active and not env else
         btn("Вернуть в операторы", f"aop:st:{uid}:1", "ok", style="success") if not active else None),
        btn("Списать долг вручную", f"aop:wo:{uid}", "dollar") if op and op.debt > 0 else None,
        [btn("Долг: +", f"amd:{uid}:+", "plus"), btn("Долг: −", f"amd:{uid}:-", "down")],
        btn("История", f"aev:op:{uid}", "list"),
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
    returned, closed = await orders.drop_operator(s, uid) if not op.active else ([], [])
    await s.commit()
    if returned or closed:  # his deals do not hang: orders go to the other operators, unpaid deals close
        from bot.handlers.deal import push
        from bot.handlers.orders import notify_operators
        for d in returned:
            if d.status == "checking":
                await notify_operators(bot, s, d, await s.get(User, d.seller_id) if d.seller_id else None)
        for d in closed:
            await push(bot, s, d.buyer_id, d, f"Сделка #{d.id} закрыта: оператор больше не работает. Не переводите "
                                              "по ней — уже перевели, загрузите чек")
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
        "Используйте, если оператор вернул USDT в обход бота (например, переводом прямо на горячий кошелёк). "
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
    await teams_screen(bot, s, user, c)


async def teams_screen(bot: Bot, s: AsyncSession, admin: User, src=None):
    rows = (await s.scalars(select(Team).order_by((Team.status == "pending").desc(), Team.id.desc()).limit(40))).all()
    paid = Decimal(await s.scalar(select(func.coalesce(func.sum(Deal.team_fee), 0))))
    sizes = dict((await s.execute(select(User.team_id, func.count(User.id)).where(User.team_id.is_not(None))
                                  .group_by(User.team_id))).all())
    leaders = {u.id: u for u in (await s.scalars(select(User).where(User.id.in_([t.leader_id for t in rows]))))}
    working = sum(1 for t in rows if t.status == "approved")
    lines = [f"{mark(TEAM_ICON[t.status])} {alink('team', t.id, f'«{esc(t.name)}»')} · {teams.STATUS[t.status]} · "
             f"{max(sizes.get(t.id, 0) - 1, 0)} чел. · тимлид {ulink(leaders.get(t.leader_id), t.leader_id)}"
             + (" · есть чат" if t.chat_id else "") for t in rows]
    await show(bot, admin, "\n".join([
        title(pe("people"), "Команды и тимлиды"),
        card(cf("Сейчас", f"работают: <b>{working}</b> · ждут решения: <b>{sum(t.status == 'pending' for t in rows)}</b>",
                f"тимлидам по умолчанию: <b>{money.fmt(settings.dec('team_pct'), 3)}%</b> от сделок участников",
                f"выплачено тимлидам всего: <b>{money.usdt(paid)} USDT</b>")),
        "",
        "\n".join(lines) if lines else "<i>Пока нет ни одной команды.</i>",
        "",
        quote("Название команды и имя тимлида — ссылки: откроют карточку в боте. Заявки — кнопками ниже."),
    ]), kb(*[btn(f"Заявка · {t.name[:28]}", f"atm:{t.id}", "people", style="primary")
             for t in rows if t.status == "pending"],
           *[btn(f"{t.name[:30]}", f"atm:{t.id}", "people") for t in rows if t.status != "pending"][:12],
           btn("Процент тимлидов", "acs:team_pct", "percent"),
           back("a", "Админ-панель")), src)


TEAM_ICON = {"pending": "🟡", "approved": "🟢", "suspended": "⏸", "rejected": "🔴"}


async def _chat_line(bot: Bot, t: Team) -> str:
    if not t.chat_id:
        return "не подключён — тимлид отправляет /team в своей группе"
    try:
        info = await bot.get_chat(t.chat_id)
        name = f"«{esc(info.title or str(t.chat_id))}»"
        with suppress(TelegramAPIError):
            name += f" · {await bot.get_chat_member_count(t.chat_id)} участн."
        return f"{name} · <code>{t.chat_id}</code>"
    except TelegramAPIError:
        return f"<code>{t.chat_id}</code> — бот не видит чат (удалён из группы?)"


async def team_card(bot: Bot, s: AsyncSession, admin: User, t: Team, src=None, note: str = "", invite: str = ""):
    leader = await s.get(User, t.leader_id)
    n, rub, fee = await teams.stats(s, t)
    day = await teams.stats(s, t, now() - timedelta(hours=24))
    link = await deep_link(bot, f"t{t.id}")
    await show(bot, admin, "\n".join([
        title(pe("people"), f"Команда «{esc(t.name)}»") + f" · {mark(TEAM_ICON[t.status])} {teams.STATUS[t.status]}",
        "",
        card(
            cf("Тимлид", ulink(leader, t.leader_id), icon="profile"),
            cf("Участники", f"<b>{await teams.members(s, t)}</b> · {alink('teamm', t.id, 'список')}", icon="people"),
            cf("Чат команды", await _chat_line(bot, t), icon="support"),
            cf("Реферальная ссылка", f"<code>{esc(link)}</code>", icon="key"),
            cf("Процент тимлида", f"<b>{money.fmt(teams.pct(t), 3)}%</b>"
               + (" (личный)" if t.pct is not None else " (общий)"), icon="percent"),
            cf("Оборот участников", f"24 ч: {day[0]} сделок · {money.fmt(day[1])} ₽ · тимлиду +{money.usdt(day[2])} USDT",
               f"всего: {n} сделок · {money.fmt(rub)} ₽ · тимлиду +{money.usdt(fee)} USDT", icon="stats"),
            cf("Заявка", f"подана {at(t.created_at, 'dt')}"
               + (f" · решение {at(t.decided_at, 'dt')}" if t.decided_at else "")
               + (f" · {ulink(await s.get(User, t.admin_id), t.admin_id)}" if t.admin_id else ""), icon="clock"),
        ),
        quote(f"<b>О себе:</b> {esc(t.about[:600])}") if t.about else "",
        quote(f"<b>Причина отказа:</b> {esc(t.reason)}") if t.reason else "",
    ]) + note, kb(
        [btn("Одобрить", f"atm:ok:{t.id}", "ok", style="success"),
         btn("Отклонить", f"atm:no:{t.id}", "cross", style="danger")] if t.status == "pending" else None,
        btn("Войти в чат команды", url=invite, icon="support", style="success") if invite else None,
        btn("Ссылка в чат команды", f"atm:inv:{t.id}", "support", style="primary") if t.chat_id and not invite else None,
        [btn("Участники", f"atm:m:{t.id}", "list"), btn("Процент тимлида", f"atm:pct:{t.id}", "percent")]
        if t.status in ("approved", "suspended") else None,
        btn("Приостановить", f"atm:st:{t.id}:0", "pause", style="danger") if t.status == "approved" else None,
        btn("Возобновить", f"atm:st:{t.id}:1", "ok", style="success") if t.status == "suspended" else None,
        btn("Отключить чат", f"atm:uc:{t.id}", "cross") if t.chat_id else None,
        btn("История", f"aev:team:{t.id}", "list"),
        back("atml", "Команды")), src)


async def open_team(bot: Bot, s: AsyncSession, admin: User, tid: int, src=None) -> bool:
    t = await s.get(Team, tid, populate_existing=True)
    if t is None:
        return False
    await team_card(bot, s, admin, t, src)
    return True


@router.callback_query(F.data.regexp(r"^atm:(\d+)$"))
async def cb_team(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    if not await open_team(bot, s, user, int(c.data.split(":")[1]), c):
        await c.answer("Команда не найдена", show_alert=True)


@router.callback_query(F.data.regexp(r"^atm:inv:(\d+)$"))
async def cb_team_invite(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """A personal one-time link into the team's chat for the admin (the bot is an admin there)."""
    t = await s.get(Team, int(c.data.split(":")[2]))
    if not t or not t.chat_id:
        return await c.answer("У команды нет чата", show_alert=True)
    try:
        link = await bot.create_chat_invite_link(t.chat_id, name=f"admin {user.id}"[:32], member_limit=1,
                                                 expire_date=now() + timedelta(hours=1))
    except TelegramAPIError as e:
        return await team_card(bot, s, user, t, c, warn(f"Бот не смог создать ссылку: {esc(str(e)[:150])}. "
                                                        "Нужны права администратора «Приглашать пользователей»."))
    audit.log(s, user.id, "team_chat_link", f"team:{t.id}")
    await team_card(bot, s, user, t, c, ok("Ссылка готова: на один вход, действует 1 час"), invite=link.invite_link)


@router.callback_query(F.data.regexp(r"^atm:m:(\d+)$"))
async def cb_team_members(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    t = await s.get(Team, int(c.data.split(":")[2]))
    if not t:
        return await c.answer("Команда не найдена", show_alert=True)
    await members_screen(bot, s, user, t, c)


async def members_screen(bot: Bot, s: AsyncSession, admin: User, t: Team, src=None):
    rows = (await s.scalars(select(User).where(User.team_id == t.id, User.id != t.leader_id)
                            .order_by(User.created_at.desc()).limit(50))).all()
    done = await deals.completed_count(s, [u.id for u in rows])
    total = await teams.members(s, t)
    await show(bot, admin, "\n".join([
        title(pe("list"), f"Участники «{esc(t.name)}»") + f" · {total}",
        "",
        "\n".join(f"{i}. {ulink(u)} · сделок {done[u.id]}" + (" · заблокирован" if u.is_banned else "")
                  for i, u in enumerate(rows, 1)) if rows else "<i>Пока никого — тимлид раздаёт реферальную ссылку.</i>",
        "",
        quote("Имя — ссылка на карточку в боте; там же «Убрать из команды»."
              + (f" Показаны последние 50 из {total}." if total > 50 else "")),
    ]), kb(back(f"atm:{t.id}", "Команда")), src)


@router.callback_query(F.data.regexp(r"^atm:uc:(\d+)$"))
async def cb_team_unlink_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    t = await s.get(Team, int(c.data.split(":")[2]))
    if not t or not t.chat_id:
        return await c.answer("Чат уже отключён", show_alert=True)
    await show(bot, user, "\n".join([
        f"{pe('warn')} <b>Отключить чат команды «{esc(t.name)}»?</b>",
        "",
        quote("Заявки покупателей перестанут публиковаться в этом чате, участники не получат в него ссылки. "
              "Сама группа и её участники останутся. Тимлид сможет подключить чат снова командой /team."),
    ]), kb([btn("Отключить", f"atm:uc2:{t.id}", "cross", style="danger"), back(f"atm:{t.id}", "Отмена")]), c)


@router.callback_query(F.data.regexp(r"^atm:uc2:(\d+)$"))
async def cb_team_unlink(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    t = await s.get(Team, int(c.data.split(":")[2]), with_for_update=True, populate_existing=True)
    if not t or not t.chat_id:
        return await c.answer("Чат уже отключён", show_alert=True)
    old, t.chat_id = t.chat_id, None
    audit.log(s, user.id, "team_chat_off", f"team:{t.id}", str(old))
    events.add(s, f"team:{t.id}", "chat_off", f"Чат команды {old} отключён ({user.name})", t.leader_id, alert=True)
    await s.commit()
    await notify(bot, t.leader_id, f"{pe('info')} Администрация отключила чат команды «{esc(t.name)}». "
                                   "Подключить другой — отправьте /team в нужной группе.")
    await team_card(bot, s, user, t, c, ok("Чат отключён, тимлид уведомлён"))


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
        quote(f"• Реферальная ссылка: <code>{esc(await deep_link(bot, f't{t.id}'))}</code>",
              f"• Ваш процент: {money.fmt(teams.pct(t), 3)}% от каждой сделки участника — на баланс",
              "• Чат: добавьте бота админом группы и отправьте в ней /team")]),
        kb(btn("Кабинет тимлида", "tm", "people", style="success"), back("x", "Скрыть", "cross")))
    await team_card(bot, s, user, t, c, "\n\n" + verdict("ok", "Одобрено", user))


@router.callback_query(F.data.regexp(r"^atm:no:(\d+)$"))
async def cb_team_reject_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    tid = int(c.data.split(":")[2])
    t = await s.get(Team, tid)
    if not t or t.status != "pending":
        return await c.answer("Заявка уже рассмотрена", show_alert=True)
    await state.set_state(AdmPeople.team_reason)
    await state.set_data({"team": tid})
    await show(bot, user, "\n".join([
        f"{pe('cross')} <b>Отказ: команда «{esc(t.name)}»</b>",
        "",
        quote("Напишите причину следующим сообщением (5–500 символов) — её увидит заявитель."),
    ]), kb(back(f"atm:{tid}", "Отмена")), c)


@router.message(AdmPeople.team_reason, F.text)
async def msg_team_reject(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    tid = (await state.get_data())["team"]
    reason = " ".join(m.text.split())
    if not 5 <= len(reason) <= 500:
        return await show(bot, user, f"{pe('cross')} <b>Причина отказа</b>\n\n"
                          + quote("Напишите причину ещё раз.") + warn("От 5 до 500 символов"),
                          kb(back(f"atm:{tid}", "Отмена")))
    await state.clear()
    t = await s.get(Team, tid, with_for_update=True, populate_existing=True)
    if not t or t.status != "pending":
        return await show(bot, user, warn("Заявка уже рассмотрена"), kb(back("atml", "Команды")))
    t.status, t.admin_id, t.decided_at, t.reason = "rejected", user.id, now(), reason
    audit.log(s, user.id, "team_reject", f"team:{tid}", reason)
    events.add(s, f"team:{tid}", "rejected", f"Заявка отклонена ({user.name}): {reason}", t.leader_id, alert=True)
    await s.commit()
    await notify(bot, t.leader_id, f"{pe('cross')} <b>Заявка на команду «{esc(t.name)}» отклонена</b>\n"
                                   + quote(f"• Причина: {esc(reason)}"))
    await team_card(bot, s, user, t, note="\n\n" + verdict("cross", "Отклонено", user))


@router.callback_query(F.data.regexp(r"^atm:st:(\d+):([01])$"))
async def cb_team_status(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, _, tid, on = c.data.split(":")
    t = await s.get(Team, int(tid), with_for_update=True, populate_existing=True)
    if not t or t.status not in ("approved", "suspended"):
        return await c.answer()
    if t.status == ("approved" if on == "1" else "suspended"):
        return await c.answer("Уже сделано", show_alert=True)
    t.status = "approved" if on == "1" else "suspended"
    what = "возобновлена" if on == "1" else "приостановлена"
    audit.log(s, user.id, "team_status", f"team:{tid}", what)
    events.add(s, f"team:{tid}", "status", f"Команда {what} ({user.name})", t.leader_id, alert=True)
    await s.commit()
    await notify(bot, t.leader_id, f"{pe('people')} Команда «{esc(t.name)}» {what} администрацией."
                 + (" Пока она приостановлена, процент с её сделок не начисляется и заявки в чат не приходят."
                    if on == "0" else ""))
    await team_card(bot, s, user, t, c, "\n\n" + verdict("ok" if on == "1" else "pause", what.capitalize(), user))


@router.callback_query(F.data.regexp(r"^atm:pct:(\d+)$"))
async def cb_team_pct(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    t = await s.get(Team, int(c.data.split(":")[2]))
    if not t:
        return await c.answer()
    await state.set_state(AdmPeople.team_pct)
    await state.set_data({"team": t.id})
    await show(bot, user, "\n".join([
        f"{pe('pencil')} <b>Процент тимлида · «{esc(t.name)}»</b>",
        "",
        card(cf("Сейчас", f"<b>{money.fmt(teams.pct(t), 3)}%</b>"), cf("Общий", f"{money.fmt(settings.dec('team_pct'), 3)}%")),
        quote("Пришлите процент следующим сообщением (например, <code>1.5</code>) или «-» — общий. Платит площадка "
              "из своего дохода по сделке, не больше него.")]), kb(back(f"atm:{t.id}", "Отмена")), c)


@router.message(AdmPeople.team_pct, F.text)
async def msg_team_pct(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    tid = (await state.get_data())["team"]
    value = _parse(m.text, "pct")
    if value is False:
        return await show(bot, user, f"{pe('pencil')} <b>Процент тимлида</b>\n\n" + quote("Пришлите процент или «-».")
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


# ---------- a member: out of his team (and its chat) ----------

@router.callback_query(F.data.regexp(r"^aux:(\d+)$"))
async def cb_leave_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    u = await s.get(User, int(c.data.split(":")[1]))
    t = await s.get(Team, u.team_id) if u and u.team_id else None
    if t is None:
        return await c.answer("Пользователь не в команде", show_alert=True)
    if t.leader_id == u.id:
        return await c.answer("Это тимлид: приостановите команду в её карточке", show_alert=True)
    await show(bot, user, "\n".join([
        f"{pe('warn')} <b>Убрать {ulink(u)} из команды «{esc(t.name)}»?</b>",
        "",
        quote("Его новые сделки больше не принесут процент тимлиду. "
              + ("Бот также удалит его из чата команды." if t.chat_id else "")),
    ]), kb([btn("Убрать", f"aux2:{u.id}", "cross", style="danger"), back(f"auv:{u.id}", "Отмена")]), c)


@router.callback_query(F.data.regexp(r"^aux2:(\d+)$"))
async def cb_leave(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    from bot.handlers.admin import user_screen
    u = await s.get(User, int(c.data.split(":")[1]), with_for_update=True, populate_existing=True)
    t = await s.get(Team, u.team_id) if u and u.team_id else None
    if t is None or t.leader_id == u.id:
        return await c.answer("Пользователь не в команде", show_alert=True)
    u.team_id = None
    kicked = False
    if t.chat_id:
        with suppress(TelegramAPIError):
            await bot.ban_chat_member(t.chat_id, u.id)
            await bot.unban_chat_member(t.chat_id, u.id, only_if_banned=True)  # removed, may join again by a link
            kicked = True
    audit.log(s, user.id, "team_remove", f"user:{u.id}", f"team {t.id}")
    events.add(s, f"team:{t.id}", "removed", f"Участник {u.name or '—'} ({u.id}) убран администрацией ({user.name})"
               + (", удалён из чата" if kicked else ""), u.id, alert=True)
    await s.commit()
    await notify(bot, u.id, f"{pe('info')} Администрация убрала вас из команды «{esc(t.name)}».")
    await user_screen(bot, s, user, u, c, ok(f"Убран из команды «{esc(t.name)}»" + (" и из её чата" if kicked else "")))


# ---------- admins ----------

@router.callback_query(F.data == "aadm")
async def cb_admins(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await admins_screen(bot, s, user, c)


async def admins_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    ids = admins.ids()
    people = {u.id: u for u in (await s.scalars(select(User).where(User.id.in_(ids)))).all()}
    owner = admins.is_owner(admin.id)
    lines = [f"{mark('👑' if admins.is_owner(uid) else '🛡')} {ulink(people.get(uid), uid)} · {admins.role(uid)}"
             + (f" · был {at(people[uid].last_seen, 'dt')}" if uid in people else " · ещё не запускал бота")
             for uid in ids]
    await show(bot, admin, "\n".join([
        title(pe("lock"), "Администраторы") + f" · {len(ids)}",
        "",
        "\n".join(lines),
        "",
        quote("Владельцы — из ADMIN_IDS в .env: их права не снимаются. Админов назначают и снимают владельцы: "
              "кнопкой ниже, в карточке пользователя или командой <code>/addadmin ID</code> в админ-чате.",
              "Админ получает админ-панель, кнопки и команды админ-чата и все решения: споры, балансы, заявки."),
    ]) + note, kb(btn("Назначить админа", "aadm:add", "plus", style="success") if owner else None,
                  *[btn(f"Снять · {(people[uid].name if uid in people else str(uid))[:28]}", f"aga:{uid}:0", "cross")
                    for uid in admins.granted()] if owner else [],
                  back("a", "Админ-панель")), src)


@router.callback_query(F.data == "aadm:add")
async def cb_admin_add(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    if not admins.is_owner(user.id):
        return await c.answer("Назначать админов могут только владельцы", show_alert=True)
    await state.set_state(AdmPeople.admin)
    await show(bot, user, "\n".join([
        f"{pe('plus')} <b>Новый администратор</b>",
        "",
        quote("Пришлите Telegram ID или @username следующим сообщением. Человек должен хотя бы раз запустить бота."),
    ]), kb(back("aadm", "Отмена")), c)


@router.message(AdmPeople.admin, F.text)
async def msg_admin_add(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    u = await find_user(s, m.text)
    if u is None:
        return await show(bot, user, f"{pe('plus')} <b>Новый администратор</b>\n\n"
                          + quote("Пришлите Telegram ID или @username.")
                          + warn("Не найден: пусть человек запустит бота, и пришлите снова"), kb(back("aadm", "Отмена")))
    await state.clear()
    await grant_screen(bot, s, user, u)


async def find_user(s: AsyncSession, raw: str) -> User | None:
    raw = (raw or "").strip().lstrip("@")
    if not raw:
        return None
    return await s.scalar(select(User).where(User.id == int(raw) if raw.isdigit() else
                                             func.lower(User.username) == raw.lower()))


async def grant_screen(bot: Bot, s: AsyncSession, admin: User, u: User, src=None):
    if admins.is_admin(u.id):
        return await admins_screen(bot, s, admin, src, ok(f"{esc(u.name or str(u.id))} уже {admins.role(u.id)}"))
    await show(bot, admin, "\n".join([
        f"{pe('warn')} <b>Выдать права администратора?</b>",
        "",
        card(cf("Кто", ulink(u), icon="profile"),
             cf("Получит", "админ-панель в боте и команды /admin, /deal, /user",
                "кнопки и команды админ-чата", "решения: споры, балансы, заявки, баны")),
        "",
        quote("Снять права можно в любой момент: «Администраторы» или карточка пользователя."),
    ]), kb([btn("Выдать", f"aga:{u.id}:1", "ok", style="success"), back("aadm", "Отмена")]), src)


@router.callback_query(F.data.regexp(r"^aga:(\d+)$"))
async def cb_grant_ask(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    if not admins.is_owner(user.id):
        return await c.answer("Назначать админов могут только владельцы", show_alert=True)
    u = await s.get(User, int(c.data.split(":")[1]))
    if not u:
        return await c.answer("Пользователь не найден", show_alert=True)
    await grant_screen(bot, s, user, u, c)


@router.callback_query(F.data.regexp(r"^aga:(\d+):([01])$"))
async def cb_grant(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    _, uid, on = c.data.split(":")
    if not admins.is_owner(user.id):
        return await c.answer("Назначать админов могут только владельцы", show_alert=True)
    u = await s.get(User, int(uid))
    if not u:
        return await c.answer("Пользователь не найден", show_alert=True)
    done = await set_admin(bot, s, user, u, on == "1")
    if not done:
        return await c.answer("Уже сделано" if on == "1" else "Владельца снять нельзя", show_alert=True)
    await admins_screen(bot, s, user, c, "\n\n" + verdict("ok" if on == "1" else "cross",
                                                          ("Назначен" if on == "1" else "Снят") + f": {esc(u.name or uid)}",
                                                          user))


async def set_admin(bot: Bot, s: AsyncSession, owner: User, u: User, on: bool) -> bool:
    """Give or take the admin status: stored, logged, his command menu changed, he is told (with a link into the
    admin chat when he gets it, removed from it when he loses it). Commits. False if nothing changed."""
    from bot.handlers.commands import admin_menu
    from bot.handlers.logchat import targets
    if not (await admins.grant(s, u.id) if on else await admins.revoke(s, u.id)):
        return False
    audit.log(s, owner.id, "admin_grant" if on else "admin_revoke", f"user:{u.id}")
    events.add(s, f"user:{u.id}", "admin", ("Назначен администратором" if on else "Снят с администраторов")
               + f" ({owner.name})", u.id, alert=True)
    await s.commit()
    await admin_menu(bot, u.id, on)
    chats = [c for c in targets() if c < 0]
    if on:
        invite = None
        for chat in chats:
            with suppress(TelegramAPIError):
                invite = (await bot.create_chat_invite_link(chat, name=f"admin {u.id}"[:32], member_limit=1,
                                                            expire_date=now() + timedelta(days=1))).invite_link
        await notify(bot, u.id, "\n".join([
            f"{pe('lock')} <b>Вам выданы права администратора {BRAND}</b>",
            "",
            quote("• Админ-панель: кнопка ниже или /admin",
                  "• Админ-чат: логи по темам, решения кнопками прямо там" + (" — ссылка ниже, на один вход"
                                                                              if invite else "")),
        ]), kb(btn("Админ-панель", "a", "settings", style="success"),
               btn("Войти в админ-чат", url=invite) if invite else None, back("x", "Скрыть", "cross")))
    else:
        for chat in chats:
            with suppress(TelegramAPIError):
                await bot.ban_chat_member(chat, u.id)
                await bot.unban_chat_member(chat, u.id, only_if_banned=True)
        await notify(bot, u.id, f"{pe('info')} Права администратора {BRAND} сняты.")
    return True


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
