"""Teams in the bot («Команда» in the menu): what a team is, the application of a future leader, the leader's
cabinet (referral link, chat, members, income), a member's screen with a personal invite link to the team chat,
joining by the leader's link (/start t<id>, see handlers/commands.py) and /team in a group: the leader connects his
chat. Money (1% of the members' deals) is paid in services/deals.complete() via services/teams.py."""
import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.models import Event, Team, User, now
from bot.services import deals, events, money, settings, teams
from bot import guides, ui
from bot.ui import app_btn, at, clean, deep_link, esc, field, ok, quote, section, show, title, warn

log = logging.getLogger(__name__)
router = Router()
REAPPLY_AFTER = timedelta(hours=24)
LINKS_PER_HOUR = 5


class TeamForm(StatesGroup):
    name = State()
    about = State()


def _form(n: int, text: str, err: str = "") -> str:
    return f"{title(pe('people'), 'Заявка на команду')} · шаг {n} из 2\n\n{text}" + (warn(err) if err else "")


@router.callback_query(F.data == "tm")
async def cb_team(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await team_screen(bot, s, user, c)


async def team_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    led = await teams.led_by(s, user.id)
    if led is not None and led.status in ("approved", "suspended"):
        return await leader_screen(bot, s, user, led, src, note)
    member = await teams.of_user(s, user)
    if member is not None:
        leader = await s.get(User, member.leader_id)
        return await show(bot, user, "\n".join([
            title(pe("people"), f"Команда «{esc(member.name)}»"),
            "",
            field("Тимлид", esc(leader.name or "—") + (f" @{esc(leader.username)}" if leader.username else "")),
            field("Участников", str(await teams.members(s, member))),
            field("Чат команды", "подключён" if member.chat_id else "тимлид ещё не подключил"),
            "",
            quote("В чат команды приходят заявки покупателей — берите их кнопкой «Взять заявку в боте»."),
        ]) + note, kb(btn("Вступить в чат команды", "tm:chat", "people", style="success") if member.chat_id else None,
                      btn("Ордерные реквизиты", "om", "key"), back("menu", "В меню")), src)
    lines = [title(pe("people"), "Команды Strait Pay"),
             "Соберите свою команду мерчантов и получайте процент с каждой её сделки.",
             quote(f"• Тимлиду — <b>{money.fmt(settings.dec('team_pct'), 3)}%</b> от суммы каждой сделки участника "
                   "на ваш баланс",
                   "• Своя реферальная ссылка: кто пришёл по ней — в вашей команде",
                   "• Свой чат: бот публикует в нём все заявки покупателей",
                   "• Заявку одобряет администрация")]
    wait = False
    if led is not None and led.status == "pending":
        lines.append(f"{pe('clock')} <b>Заявка «{esc(led.name)}» на рассмотрении</b> с {at(led.created_at, 'dt')}. "
                     "Ответ придёт в этот чат.")
        wait = True
    elif led is not None and led.status == "rejected":
        lines.append("Прошлая заявка отклонена" + (f": <i>{esc(led.reason)}</i>" if led.reason else "") + ".")
        if led.decided_at and now() - deals.aware(led.decided_at) < REAPPLY_AFTER:
            lines.append(f"Подать снова можно после {at(deals.aware(led.decided_at) + REAPPLY_AFTER, 'dt')}.")
            wait = True
    if user.team_id is not None and led is None:
        lines.append(f"{pe('info')} Вы участник команды, которая сейчас не работает.")
    await show(bot, user, "\n".join(lines) + note, kb(
        None if wait else btn("Создать команду", "tm:apply", "plus", style="success"),
        back("menu", "В меню")), src)


async def leader_screen(bot: Bot, s: AsyncSession, user: User, team: Team, src=None, note: str = ""):
    link = await deep_link(bot, f"t{team.id}")
    members = await teams.members(s, team)
    day, week, total = [await teams.stats(s, team, since) for since in
                        (deals.day_start(), now() - timedelta(days=7), None)]
    chat_line = "• Чат: не подключён"
    if team.chat_id:
        try:
            info = await bot.get_chat(team.chat_id)
            chat_line = f"• Чат: <b>{esc(info.title or str(team.chat_id))}</b>"
        except TelegramAPIError:
            chat_line = f"• Чат: <code>{team.chat_id}</code> — бот его не видит, добавьте бота снова"
    active = team.status == "approved"
    await show(bot, user, "\n".join([
        title(pe("people"), f"Команда «{esc(team.name)}» · тимлид"),
        "" if active else f"{pe('pause')} <b>Команда приостановлена администрацией</b>",
        "",
        section("people", "Команда"),
        field("Участников", f"<b>{members}</b>"),
        chat_line.removeprefix("• ").replace("Чат: ", "<b>Чат:</b> ", 1),
        field("Ваш процент", f"<b>{money.fmt(teams.pct(team), 3)}%</b> от каждой сделки участника"),
        "",
        section("key", "Реферальная ссылка"),
        f"<code>{esc(link)}</code>",
        "",
        section("up", "Доход тимлида"),
        field("Командный баланс", f"<b>{money.usdt(user.team_balance)} USDT</b> — переведите на основной и выводите"),
        field("Сегодня", f"<b>+{money.usdt(day[2])} USDT</b> · {day[0]} сделок на {money.fmt(day[1])} ₽"),
        field("7 дней", f"+{money.usdt(week[2])} USDT · {week[0]} на {money.fmt(week[1])} ₽"),
        field("Всего", f"+{money.usdt(total[2])} USDT · {total[0]} на {money.fmt(total[1])} ₽"),
        "",
        quote("Кто запустит бота по ссылке — попадёт в команду и получит приглашение в ваш чат.") if team.chat_id
        else section("support", "Как подключить чат") + "\n" + quote(
            "1. Создайте группу и добавьте в неё бота администратором",
            "2. Права бота: «Приглашать пользователей» и «Закреплять сообщения»",
            "3. Отправьте в группе команду /team — бот привяжет чат к команде"),
    ]).replace("\n\n\n", "\n\n") + note, kb(
        btn(f"На основной баланс · {money.usdt(user.team_balance)} USDT", "tm:out", "wallet", style="success")
        if user.team_balance > 0 else None,
        btn("Скопировать ссылку", icon="key", copy=link, style="primary"),
        btn("Участники", "tm:m", "list"), app_btn("Открыть приложение", "", "live", wide=True),
        back("menu", "В меню")), src)


@router.callback_query(F.data == "tm:out")
async def cb_team_out(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """The leader's team earnings -> his main balance (from there: «Кошелёк» → «Вывести»)."""
    u = await money.lock(s, user.id)
    amount = u.team_balance
    if amount <= 0:
        return await c.answer("Командный баланс пуст", show_alert=True)
    await money.team_to_balance(s, u.id, amount)
    team = await teams.led_by(s, u.id)
    events.add(s, f"team:{team.id}" if team else f"user:{u.id}", "team_out",
               f"Тимлид перевёл {money.usdt(amount)} USDT с командного баланса на основной", u.id, notice=True)
    await s.commit()
    await team_screen(bot, s, u, c, ok(f"{money.usdt(amount)} USDT на основном балансе — вывести можно в «Кошелёк»."))


@router.callback_query(F.data == "tm:m")
async def cb_members(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    team = await teams.led_by(s, user.id)
    if team is None or team.status not in ("approved", "suspended"):
        return await c.answer("Вы не тимлид", show_alert=True)
    rows = (await s.scalars(select(User).where(User.team_id == team.id, User.id != team.leader_id)
                            .order_by(User.created_at.desc()).limit(30))).all()
    done = await deals.completed_count(s, [u.id for u in rows])
    await show(bot, user, "\n".join([
        title(pe("list"), f"Участники «{esc(team.name)}»"),
        "Последние 30, новые сверху.",
        quote(*[f"• {esc(u.name or '—')}" + (f" @{esc(u.username)}" if u.username else "") + f" · сделок {done[u.id]}"
                for u in rows]) if rows else "Пока никого — поделитесь реферальной ссылкой.",
    ]), kb(back("tm", "Команда")), c)


@router.callback_query(F.data == "tm:chat")
async def cb_chat(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """A personal one-time invite link to the team chat (like the community chat)."""
    team = await teams.of_user(s, user)
    if team is None or not team.chat_id:
        return await c.answer("Чат команды пока не подключён", show_alert=True)
    recent = await s.scalar(select(func.count(Event.id)).where(
        Event.ref == f"user:{user.id}", Event.kind == "team_link", Event.created_at > now() - timedelta(hours=1)))
    if recent >= LINKS_PER_HOUR:
        return await c.answer("Ссылок за час слишком много — используйте последнюю или попробуйте позже",
                              show_alert=True)
    try:
        link = await bot.create_chat_invite_link(team.chat_id, name=str(user.id), member_limit=1,
                                                 expire_date=now() + timedelta(hours=1))
    except TelegramAPIError as e:
        log.warning("team %s invite link for %s: %s", team.id, user.id, e)
        events.add(s, f"team:{team.id}", "link_failed", f"Бот не смог создать ссылку в чат команды: {e}"[:300],
                   alert=True)
        return await c.answer("Чат команды временно недоступен — напишите тимлиду", show_alert=True)
    events.add(s, f"user:{user.id}", "team_link", f"Получил ссылку в чат команды «{team.name}»", user.id, notice=True)
    await show(bot, user, "\n".join([
        title(pe("people"), f"Чат команды «{esc(team.name)}»"),
        quote("• Ссылка личная: на один вход, действует 1 час",
              "• В чате — все заявки покупателей и общение команды"),
    ]), kb(btn("Вступить в чат", url=link.invite_link, icon="people", style="success"), back("tm", "Команда")), c)


async def joined_screen(bot: Bot, s: AsyncSession, user: User, team: Team, fresh: bool):
    """After /start t<id>: welcome to the team and an invite to its chat."""
    leader = await s.get(User, team.leader_id)
    note = ok(f"Вы в команде «{esc(team.name)}»") if fresh else ""
    await show(bot, user, "\n".join([
        title(pe("people"), f"Команда «{esc(team.name)}»"),
        f"Тимлид: {esc(leader.name or '—')}" + (f" @{esc(leader.username)}" if leader.username else ""),
        quote("• Вступите в чат команды — туда приходят заявки покупателей",
              "• Брать заявки могут ордерные мерчанты: анкета — в «Ордерные реквизиты»"),
    ]) + note, kb(btn("Вступить в чат команды", "tm:chat", "people", style="success") if team.chat_id else None,
                  btn("Ордерные реквизиты", "om", "key"), back("menu", "В меню")))


# ---------- application ----------

@router.callback_query(F.data == "tm:apply")
async def cb_apply(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    led = await teams.led_by(s, user.id)
    if led is not None and (led.status != "rejected" or (led.decided_at and now() - deals.aware(led.decided_at)
                                                         < REAPPLY_AFTER)):
        return await team_screen(bot, s, user, c)
    await state.set_state(TeamForm.name)
    await show(bot, user, _form(1, "Название команды (3–40 символов) — так её увидят участники:"),
               kb(back("tm", "Отмена")), c)


@router.message(TeamForm.name, F.text)
async def f_name(m: Message, bot: Bot, user: User, state: FSMContext):
    name = " ".join(m.text.split())
    if not 3 <= len(name) <= 40:
        return await show(bot, user, _form(1, "Название команды:", "От 3 до 40 символов"), kb(back("tm", "Отмена")))
    await state.update_data(t_name=name)
    await state.set_state(TeamForm.about)
    await show(bot, user, _form(2, f"Команда: <b>{esc(name)}</b>\n\nРасскажите о себе: опыт в P2P, сколько людей "
                                   "приведёте, откуда трафик (до 1000 символов):"), kb(back("tm", "Отмена")))


@router.message(TeamForm.about, F.text)
async def f_about(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    about = m.text.strip()
    if not 5 <= len(about) <= 1000:
        return await show(bot, user, _form(2, "О себе и команде:", "От 5 до 1000 символов"), kb(back("tm", "Отмена")))
    data = await state.get_data()
    await state.clear()
    if not data.get("t_name"):
        return await team_screen(bot, s, user, note=warn("Заявка устарела — заполните заново."))
    team = await teams.led_by(s, user.id)
    if team is None:
        team = Team(leader_id=user.id, name=data["t_name"])
        s.add(team)
    team.name, team.about, team.status = data["t_name"], about, "pending"
    team.created_at, team.decided_at, team.reason, team.admin_id = now(), None, None, None
    await s.flush()
    events.add(s, f"team:{team.id}", "submitted", f"Заявка на команду «{team.name}»: {about[:200]}", user.id, alert=True)
    await s.commit()
    await team_screen(bot, s, user, note=ok("Заявка отправлена. Обычно рассматриваем в течение суток."))


# ---------- /team in a group: the leader connects his chat ----------

@router.message(Command("team"), F.chat.type.in_({"group", "supergroup"}), lambda _: ui.place.get() is None)
async def cmd_team(m: Message, bot: Bot, s: AsyncSession):
    team = await teams.led_by(s, m.from_user.id)
    if team is None or team.status != "approved":
        return await m.answer("Подключить чат команды может только тимлид с одобренной командой Strait Pay.")
    try:
        me = await bot.get_chat_member(m.chat.id, (await bot.me()).id)
    except TelegramAPIError:
        me = None
    if me is None or me.status not in ("administrator", "creator") or not getattr(me, "can_invite_users", False):
        return await m.answer("Сделайте бота администратором группы с правами «Приглашать пользователей» и "
                              "«Закреплять сообщения» и снова отправьте /team.")
    old, team.chat_id = team.chat_id, m.chat.id
    events.add(s, f"team:{team.id}", "chat", f"Чат команды подключён: {m.chat.title or m.chat.id}"
               + (f" (был {old})" if old and old != m.chat.id else ""), team.leader_id, notice=True)
    await s.commit()
    await m.answer(f"✅ <b>Чат команды «{esc(team.name)}» подключён.</b>\nСюда будут приходить все заявки покупателей "
                   "с кнопкой «Взять заявку в боте». Участники получают личную ссылку в чат в разделе «Команда».")


@router.message(Command("help"), F.chat.type.in_({"group", "supergroup"}), lambda _: ui.place.get() is None)
async def cmd_group_help(m: Message, bot: Bot):
    """/help in the community or a team chat: the guides with links and a way into the bot."""
    await m.answer(clean("\n".join([
        f"{pe('info')} <b>Strait Pay · как работать</b>",
        "Нажмите на раздел — откроется инструкция:",
        "",
        *guides.lines(),
        "",
        "Заявки покупателей публикуются в этом чате — берите их кнопкой «Взять заявку в боте».",
    ])), disable_web_page_preview=True, reply_markup=kb(
        btn("Открыть бота", url=await deep_link(bot, "menu"), icon="shop", style="success"),
        btn("Все инструкции", url=guides.index_url()) if guides.index_url() else None))


@router.message(F.chat.type.in_({"group", "supergroup"}), lambda _: ui.place.get() is None)
async def group_other(m: Message):
    """Any other command that got through to a group (e.g. /team@another_bot): ignored, groups have no screens.
    Not the admin chat: an admin's commands and answers there go on to the admin routers."""
