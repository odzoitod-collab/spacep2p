"""Community chat and broadcasts.

Chat: an admin sets the group's ID (settings: chat_id); the bot must be an admin there with the rights to invite and
to pin. Every press of «Чат» gives the user a personal one-time invite link (member_limit=1, valid 1 h, named by the
user's id), so every join is traced to its user in the log chat. The bot keeps one pinned summary in the chat:
rate, what is open right now and the turnover, edited in place every few minutes.

Broadcast: an admin sends any message (text, photo, video, file), sees the preview and the audience size and
confirms; the bot copies it to every recipient in the background with Telegram's pacing and reports the result.
"""
import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.admins import IsAdmin
from bot.emoji import back, btn, kb, pe
from bot.models import Card, Event, OrderMerchant, Session, Setting, Team, User, now
from bot.services import audit, events, money, settings
from bot import ui
from bot.ui import card, cf, clean, deep_link, esc, field, notify, ok, paced, quote, section, show, title, warn

log = logging.getLogger(__name__)
router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())
events_router = Router()  # open to everyone: «Чат» for users, joins in the community chat
LINKS_PER_HOUR = 5


def chat_id() -> int | None:
    v = settings.get("chat_id")
    return int(v) if v else None


# ---------- user: a personal invite link ----------

@events_router.callback_query(F.data == "chat")
async def cb_chat(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    from bot.handlers.community import channel_link, personal_invite
    link, problem = await personal_invite(bot, s, user)
    if link is None:
        return await c.answer(problem, show_alert=True)
    chan = await channel_link(bot, s)
    await show(bot, user, "\n".join([
        title(pe("people"), "Чат Strait Pay"),
        "",
        quote("Курс, новости и заявки покупателей — в чате. Ссылка личная: на один вход, действует 1 час."),
    ]), kb(btn("Вступить в чат", url=link, icon="people", style="success"),
           btn("Инфо-канал", url=chan, icon="bell") if chan else None, back("menu", "В меню")), c)


_welcomes: dict[int, int] = {}  # chat -> its last welcome: one at a time, the chat is not flooded with greetings


async def welcome(bot: Bot, s: AsyncSession, chat: int, joined, team: Team | None) -> None:
    """A greeting in the chat for whoever joined it: who we are, the rules, a way into the bot and the channel."""
    from bot.handlers.community import channel_link
    name = f'<a href="tg://user?id={joined.id}">{esc(joined.full_name or "участник")}</a>'
    chan = await channel_link(bot, s)
    open_bot = await deep_link(bot, "menu")
    text = clean("\n".join([
        f"{pe('people')} <b>Добро пожаловать, {name}!</b>",
        "",
        quote(f"Это чат команды <b>«{esc(team.name)}»</b>: заявки покупателей приходят сюда — берите их кнопкой "
              "«Взять заявку в боте»." if team else
              "Это чат Strait Pay: заявки покупателей, курс и новости. Заявки берите кнопкой «Взять заявку в боте»."),
        f"{pe('lock')} <b>Правила:</b> сделки и общение по ним — только в боте; ссылки, реклама и обмен в обход "
        "бота — бан.",
        f"{pe('info')} <b>С чего начать:</b> откройте бота → «Помощь» — там условия и инструкции.",
    ]))
    try:
        m = await paced(lambda: bot.send_message(chat, text, disable_web_page_preview=True, disable_notification=True,
                                                 reply_markup=kb(btn("Открыть бота", url=open_bot, icon="shop",
                                                                     style="success"),
                                                                 btn("Инфо-канал", url=chan, icon="bell") if chan
                                                                 else None)))
    except TelegramAPIError as e:
        log.warning("welcome in %s: %s", chat, e)
        return
    old, _welcomes[chat] = _welcomes.get(chat), m.message_id
    if old:
        with suppress(TelegramAPIError):
            await bot.delete_message(chat, old)


@events_router.chat_member()
async def on_member(e: ChatMemberUpdated, bot: Bot, s: AsyncSession):
    """Joins and leaves the bot sees. The community chat and the info channel keep users.in_chat / in_channel (the
    entry gate, handlers.community); a join into the community chat or a team chat gets a greeting there and a line in
    the user's log card (and the team's)."""
    from bot.handlers.community import channel_id, is_member
    joined = e.new_chat_member.user
    now_in, was_in = is_member(e.new_chat_member), is_member(e.old_chat_member)
    if now_in == was_in or joined.is_bot:
        return
    if e.chat.id == channel_id():
        if u := await s.get(User, joined.id):
            u.in_channel = now_in
        if now_in:
            events.add(s, f"user:{joined.id}", "channel_join", "Подписался на инфо-канал", joined.id, notice=True)
        return
    community = e.chat.id == chat_id()
    team = None if community else await s.scalar(select(Team).where(Team.chat_id == e.chat.id))
    if not community and team is None:
        return
    if community and (u := await s.get(User, joined.id)):
        u.in_chat = now_in
    if not now_in:
        return
    name = (e.invite_link.name or "") if e.invite_link else ""
    owner = int(name) if name.isdigit() else None
    who = f"@{joined.username}" if joined.username else joined.full_name
    where = f"чат команды «{team.name}»" if team else "чат"
    events.add(s, f"user:{owner or joined.id}", "chat_join",
               f"Вступил в {where}: {who}" + ("" if owner in (None, joined.id) else f" — по ссылке пользователя {owner}"),
               owner or joined.id, alert=owner not in (None, joined.id), notice=True)
    if team:
        events.add(s, f"team:{team.id}", "chat_join", f"В чат команды вступил {who}", joined.id, notice=True)
    await welcome(bot, s, e.chat.id, joined, team)


# ---------- the pinned summary ----------

def pin_text() -> str:
    """The pinned message of the community chat: the current terms only — the rate and the merchants' terms."""
    rate, pp, sp = settings.dec("rate"), settings.dec("platform_pct"), settings.dec("seller_pct")
    orate = settings.dec("order_rate")
    return clean("\n".join([
        "📌 <b>Strait Pay · актуальные курсы</b>",
        "",
        section("swap", "Покупка USDT"),
        field("Курс", f"<b>1 USDT = {money.fmt(rate)} ₽</b> · комиссия {money.fmt(pp, 3)}%"),
        "",
        section("card", "Мерчант со статичной картой"),
        field("Курс", f"<b>{money.fmt(rate)} ₽</b> за USDT"),
        field("Доход", f"<b>{money.fmt(sp, 3)}%</b> с каждой сделки"),
        "",
        section("key", "Ордерный мерчант"),
        field("Курс", f"<b>{money.fmt(orate)} ₽</b> за USDT, без процента"),
        "",
        "<blockquote>Курсы обновляются здесь сами, как только меняются. Работать — в боте, кнопкой ниже.</blockquote>",
    ]))


async def pin_markup(bot: Bot):
    return kb(btn("Открыть бота", url=await deep_link(bot, "menu"), icon="shop", style="success"),
              btn("Стать ордерным мерчантом", url=await deep_link(bot, "om"), icon="key"))


async def publish_pin(bot: Bot, s: AsyncSession) -> str:
    """Edit the pinned summary (the banner with the summary as its caption) or post and pin a new one. Returns what
    happened (for the admin screen)."""
    chat = chat_id()
    if chat is None:
        return "чат не задан"
    text, markup = pin_text(), await pin_markup(bot)
    banner = ui.banner_path() is not None
    key = f"chat_pin:{chat}"
    row = await s.get(Setting, key)
    if row:
        mid, _, kind = row.value.partition(":")  # "<message id>:c" — the banner's caption; old rows are text
        if (kind == "c") == banner:
            try:
                if banner:
                    await paced(lambda: bot.edit_message_caption(chat_id=chat, message_id=int(mid), caption=text,
                                                                 reply_markup=markup))
                else:
                    await paced(lambda: bot.edit_message_text(text=text, chat_id=chat, message_id=int(mid),
                                                              reply_markup=markup, disable_web_page_preview=True))
                return "обновлён"
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return "актуален"
                # deleted or too old: post it again
        else:  # switched between text and banner: the old pin goes away
            with suppress(TelegramAPIError):
                await bot.unpin_chat_message(chat, message_id=int(mid))
    try:
        if banner:
            async with ui._upload:
                m = await ui.send_banner(bot, chat, text, markup)
        else:
            m = await paced(lambda: bot.send_message(chat, text, reply_markup=markup, disable_notification=True,
                                                     disable_web_page_preview=True))
        await s.merge(Setting(key=key, value=f"{m.message_id}:c" if banner else str(m.message_id)))
        await s.commit()
        await bot.pin_chat_message(chat, m.message_id, disable_notification=True)
    except TelegramAPIError as e:
        await events.alert_once(s, "app:chat", "pin_failed", f"Закреп в чате {chat} не обновлён: {e}"[:300])
        await s.commit()
        return f"ошибка: {esc(str(e)[:120])}"
    return "опубликован и закреплён"


# ---------- admin: chat screen ----------

@router.callback_query(F.data.in_({"ach", "ach:pin", "ach:inv"}))
async def cb_chat_admin(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    note, invite = "", ""
    if c.data == "ach:pin":
        note = ok(f"Закреп: {await publish_pin(bot, s)}")
    elif c.data == "ach:inv" and chat_id():
        try:
            invite = (await bot.create_chat_invite_link(chat_id(), name=f"admin {user.id}"[:32], member_limit=1,
                                                        expire_date=now() + timedelta(hours=1))).invite_link
            note = ok("Ссылка готова: на один вход, действует 1 час")
        except TelegramAPIError as e:
            note = warn(f"Бот не смог создать ссылку: {esc(str(e)[:120])}")
    await chat_screen(bot, s, user, c, note, invite)


async def chat_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = "", invite: str = ""):
    chat = chat_id()
    lines = [title(pe("people"), "Чат сообщества"), ""]
    if chat is None:
        lines += [cf("Статус", "не подключён", icon="info"), "",
                  quote("1. Создайте группу и добавьте бота администратором",
                        "2. Права: «Приглашать пользователей» и «Закреплять сообщения»",
                        "3. Узнайте ID группы (например, через @getidsbot) и задайте его здесь")]
    else:
        try:
            info = await bot.get_chat(chat)
            me = await bot.get_chat_member(chat, (await bot.me()).id)
            members = ""
            with suppress(TelegramAPIError):
                members = f" · {await bot.get_chat_member_count(chat)} участн."
            rights = [("приглашать", getattr(me, "can_invite_users", False)),
                      ("закреплять", getattr(me, "can_pin_messages", False))]
            state = card(cf("Чат", f"<b>{esc(info.title or str(chat))}</b>{members} · <code>{chat}</code>", icon="people"),
                         cf("Права бота", ", ".join(f"{k} — {'да' if v else 'НЕТ'}" for k, v in rights), icon="lock"))
        except TelegramAPIError as e:
            state = card(cf("Чат", f"<code>{chat}</code>", icon="people"),
                         cf("Проблема", f"бот не видит чат: {esc(str(e)[:120])}", icon="warn"))
        joins = await s.scalar(select(func.count(Event.id)).where(Event.kind == "chat_join",
                                                                  Event.ref.like("user:%")))
        team_chats = await s.scalar(select(func.count(Team.id)).where(Team.status == "approved",
                                                                      Team.chat_id.is_not(None)))
        lines += [state, cf("Статистика", f"вступили по личным ссылкам: <b>{joins}</b>",
                            f"чатов команд (туда тоже идут заявки): <b>{team_chats}</b>", icon="stats"), "",
                  quote("Кнопка «Чат» в меню выдаёт каждому личную ссылку на один вход. Закреп — баннер со сводкой, "
                        "обновляется каждые 5 мин. Все ордерные заявки публикуются в чат с кнопкой «Взять заявку "
                        "в боте».")]
    await show(bot, admin, "\n".join(lines) + note, kb(
        btn("Войти в чат", url=invite, icon="people", style="success") if invite else None,
        btn("Ссылка в чат для меня", "ach:inv", "people") if chat and not invite else None,
        btn("Обновить закреп", "ach:pin", "refresh", style="primary") if chat else None,
        btn("Изменить ID чата" if chat else "Задать ID чата", "acx:chat_id", "pencil"),
        [btn("Инфо-канал", "achn", "bell"), btn("Рассылка", "abc", "bell")],
        back("a", "Админ-панель")), src)


# ---------- admin: broadcast ----------

class Bc(StatesGroup):
    message = State()


AUDIENCE = {"all": "Все пользователи", "sellers": "Продавцы с картами", "online": "На смене",
            "om": "Ордерные мерчанты", "chat": "Чат сообщества"}
_task: asyncio.Task | None = None


async def recipients(s: AsyncSession, who: str) -> list[int]:
    if who == "chat":
        return [chat_id()] if chat_id() else []
    q = select(User.id).where(~User.is_banned)
    if who == "sellers":
        q = q.where(User.id.in_(select(Card.user_id).where(~Card.is_deleted)))
    elif who == "online":
        q = q.where(User.is_online)
    elif who == "om":
        q = q.where(User.id.in_(select(OrderMerchant.user_id).where(OrderMerchant.status == "approved")))
    return list((await s.scalars(q.order_by(User.id))).all())


@router.callback_query(F.data == "abc")
async def cb_broadcast(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    if ui.pressed_in_group(c):  # the message to send is composed and previewed in the private chat with the bot
        return await show(bot, user, "\n".join([
            title(pe("bell"), "Рассылка"), "",
            quote("Рассылка готовится в личном чате с ботом: там вы пришлёте сообщение, увидите предпросмотр и "
                  "подтвердите отправку. Откройте бота → /admin → «Чат и рассылка» → «Рассылка».")]),
            kb(btn("Открыть бота", url=f"https://t.me/{ui.BOT}", icon="bell", style="primary") if ui.BOT else None,
               back("ach", "Назад")), c)
    sizes = {k: len(await recipients(s, k)) for k in AUDIENCE}
    busy = _task is not None and not _task.done()
    await show(bot, user, "\n".join([
        title(pe("bell"), "Рассылка"),
        f"{pe('clock')} Идёт рассылка — дождитесь отчёта." if busy else "Кому отправить?",
        quote("Подойдёт любое сообщение: текст, фото, видео, файл — с форматированием",
              "Перед отправкой покажем предпросмотр"),
    ]), kb(*[btn(f"{name} · {sizes[k]}", f"abc:{k}", "people") for k, name in AUDIENCE.items()
             if sizes[k] and not busy], back("ach", "Назад")), c)


@router.callback_query(F.data.regexp(r"^abc:(all|sellers|online|om|chat)$"))
async def cb_broadcast_who(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    who = c.data.split(":")[1]
    await state.set_state(Bc.message)
    await state.update_data(bc_who=who, bc_msg=None)
    await show(bot, user, f"{title(pe('bell'), 'Рассылка · ' + AUDIENCE[who])}\nОтправьте сообщение для рассылки.",
               kb(back("abc", "Отмена")), c)


@router.message(Bc.message)
async def msg_broadcast(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    who = (await state.get_data())["bc_who"]
    await state.set_state(None)
    n = len(await recipients(s, who))
    # the preview, exactly as recipients will see it; it is also the source of the broadcast: the admin's own
    # message is removed as an answer to the question (middlewares.Context)
    preview = await bot.copy_message(user.id, user.id, m.message_id)
    await state.update_data(bc_msg=preview.message_id)
    await show(bot, user, "\n".join([
        title(pe("bell"), "Проверьте рассылку"),
        quote(f"Кому: <b>{AUDIENCE[who]}</b> · {n}", "Сообщение — выше, ровно так его увидят"),
    ]), kb(btn(f"Отправить · {n}", "abc:go", "ok", style="success"), back("abc", "Отмена")))


@router.callback_query(F.data == "abc:go")
async def cb_broadcast_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    global _task
    data = await state.get_data()
    await state.update_data(bc_msg=None)
    if not data.get("bc_msg"):
        return await c.answer("Рассылка уже отправлена или устарела", show_alert=True)
    if _task is not None and not _task.done():
        return await c.answer("Уже идёт рассылка — дождитесь отчёта", show_alert=True)
    ids = await recipients(s, data["bc_who"])
    audit.log(s, user.id, "broadcast", data["bc_who"], f"message {data['bc_msg']} → {len(ids)}")
    _task = asyncio.create_task(run_broadcast(bot, user.id, data["bc_msg"], data["bc_who"], ids))
    await show(bot, user, title(pe("bell"), "Рассылка запущена") + f"\nПолучателей: {len(ids)}. Отчёт придёт сюда.",
               kb(back("a", "Админ-панель")), c)


async def run_broadcast(bot: Bot, admin_id: int, msg_id: int, who: str, ids: list[int]) -> tuple[int, int]:
    sent = blocked = failed = 0
    for uid in ids:
        try:
            await paced(lambda: bot.copy_message(uid, admin_id, msg_id))
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
        except TelegramAPIError:
            failed += 1
    report = (f"Рассылка «{AUDIENCE[who]}»: доставлено {sent} из {len(ids)}"
              + (f", заблокировали бота {blocked}" if blocked else "") + (f", ошибок {failed}" if failed else ""))
    async with Session() as s:
        events.add(s, "app:broadcast", "done", report, admin_id, notice=True)
        await s.commit()
    events.kick()
    with suppress(TelegramAPIError):
        await notify(bot, admin_id, f"{pe('ok')} <b>{esc(report)}</b>")
    return sent, blocked + failed
