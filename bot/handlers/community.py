"""The community: the chat (settings: chat_id) and the info channel (channel_id).

With join_required = 1 the main menu does not open until the user is in the chat and subscribed to the channel
(whichever of them is set): the bot shows «Последний шаг» with a personal one-time link into the chat, the channel's
link, his team's chat (optional) and «Проверить». Membership is kept in users.in_chat / in_channel: set by «Проверить»
(a live check) and by joins and leaves the bot sees (handlers.admin_chat.on_member). Deal screens and notifications
are never blocked — only the entry points (menu, buying, selling, wallet...). Admins pass.
"""
import logging
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import btn, kb, pe
from bot.models import Event, Setting, User, now
from bot.services import events, settings, teams
from bot.ui import field, ok, quote, show, title, warn

log = logging.getLogger(__name__)
MEMBER = ("member", "administrator", "creator")
LINKS_PER_HOUR = 5
ENTRY = {"menu", "buy:0", "sl", "w", "om", "tm", "api", "op", "deals"}  # the hubs: everything else starts there
ENTRY_COMMANDS = {"/start", "/menu", "/buy", "/sell", "/wallet", "/deals", "/api"}


def chat_id() -> int | None:
    v = settings.get("chat_id")
    return int(v) if v else None


def channel_id() -> int | None:
    v = settings.get("channel_id")
    return int(v) if v else None


def is_member(cm) -> bool:
    return cm.status in MEMBER or (cm.status == "restricted" and bool(getattr(cm, "is_member", False)))


def missing(user: User) -> list[str]:
    """What the user still has to join before the main menu opens ([] = nothing or the gate is off)."""
    if settings.get("join_required") != "1":
        return []
    return [kind for kind, cid, flag in (("chat", chat_id(), user.in_chat), ("channel", channel_id(), user.in_channel))
            if cid and not flag]


class NeedsJoin(Filter):
    """An entry point (menu, buying, selling...) of a user who is not in the community yet."""

    async def __call__(self, event, user: User | None = None, is_admin: bool = False, s=None) -> bool:
        if user is None or is_admin or not missing(user):
            return False
        from bot.services import operators
        if s is not None and await operators.is_operator(s, user.id):
            return False  # operators work orders whether or not they are in the chat
        if isinstance(event, CallbackQuery):
            return event.data in ENTRY
        if isinstance(event, Message) and event.chat.type == "private":
            return ((event.text or "").split() or [""])[0].split("@")[0].lower() in ENTRY_COMMANDS
        return False


async def personal_invite(bot: Bot, s: AsyncSession, user: User) -> tuple[str | None, str]:
    """(link, problem): a one-time link into the community chat named by the user's id, valid 1 h — every join is
    traced to its user. At most LINKS_PER_HOUR an hour."""
    chat = chat_id()
    if chat is None:
        return None, "Чат пока не подключён"
    recent = await s.scalar(select(func.count(Event.id)).where(
        Event.ref == f"user:{user.id}", Event.kind == "chat_link", Event.created_at > now() - timedelta(hours=1)))
    if recent >= LINKS_PER_HOUR:
        return None, "Ссылок за час слишком много — используйте последнюю или попробуйте позже"
    try:
        link = await bot.create_chat_invite_link(chat, name=str(user.id), member_limit=1,
                                                 expire_date=now() + timedelta(hours=1))
    except TelegramAPIError as e:
        log.warning("invite link for %s: %s", user.id, e)
        events.add(s, "app:chat", "link_failed", f"Бот не смог создать ссылку в чат {chat}: {e}"[:300], alert=True)
        return None, "Чат временно недоступен, попробуйте позже"
    events.add(s, f"user:{user.id}", "chat_link", "Получил личную ссылку в чат", user.id, notice=True)
    return link.invite_link, ""


async def channel_link(bot: Bot, s: AsyncSession) -> str | None:
    """The info channel's link: its public address, or an invite link made once and kept."""
    channel = channel_id()
    if channel is None:
        return None
    if link := settings.raw("channel_link"):
        return link
    try:
        info = await bot.get_chat(channel)
        link = f"https://t.me/{info.username}" if info.username else \
            (await bot.create_chat_invite_link(channel, name="bot")).invite_link
    except TelegramAPIError as e:
        log.warning("channel link: %s", e)
        return None
    await settings.put(s, "channel_link", link)
    return link


async def join_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = "", invite: str | None = None):
    need = missing(user)
    team = await teams.of_user(s, user)
    team_chat = team is not None and team.chat_id is not None
    chan = await channel_link(bot, s) if channel_id() and "channel" in need else None
    await show(bot, user, "\n".join(x for x in [
        title(pe("people"), "Последний шаг — сообщество"),
        "",
        quote("В чате — заявки покупателей, курс и общение, в канале — правила и новости. "
              "Вступите, нажмите «Проверить» — и откроется главное меню."),
        "",
        field("Чат Strait Pay", "вы в чате" if user.in_chat else "<b>нужно вступить</b>") if chat_id() else None,
        field("Инфо-канал", "вы подписаны" if user.in_channel else "<b>нужно подписаться</b>") if channel_id() else None,
        field(f"Чат команды «{team.name}»", "по желанию: заявки и общение команды") if team_chat else None,
    ] if x is not None) + note, kb(
        btn("Войти в чат", url=invite, icon="people", style="success") if invite else
        btn("Вступить в чат", "join:chat", "people", style="success") if "chat" in need else None,
        btn("Подписаться на канал", url=chan, icon="bell", style="success") if chan else None,
        btn("Чат команды", "tm:chat", "people") if team_chat else None,
        btn("Проверить", "join:chk", "refresh", style="primary"),
    ), src)


gate = Router()  # placed right after signup.router: entry points of users not in the community yet
gate.message.filter(NeedsJoin())
gate.callback_query.filter(NeedsJoin())
router = Router()  # the gate's own buttons: open to everyone


@gate.message()
async def gate_message(m: Message, bot: Bot, s: AsyncSession, user: User):
    await join_screen(bot, s, user)


@gate.callback_query()
async def gate_callback(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    await join_screen(bot, s, user, c)


@router.callback_query(F.data == "join:chat")
async def cb_join_chat(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    link, problem = await personal_invite(bot, s, user)
    if link is None:
        return await c.answer(problem, show_alert=True)
    await join_screen(bot, s, user, c, ok("Ссылка личная: на один вход, действует 1 час"), invite=link)


async def check(bot: Bot, user: User) -> None:
    """Ask Telegram whether the user is in the chat / the channel now (the bot is an admin there)."""
    for attr, cid in (("in_chat", chat_id()), ("in_channel", channel_id())):
        if cid and not getattr(user, attr):
            try:
                setattr(user, attr, is_member(await bot.get_chat_member(cid, user.id)))
            except TelegramAPIError as e:
                log.warning("membership of %s in %s: %s", user.id, cid, e)


@router.callback_query(F.data == "join:chk")
async def cb_join_check(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, is_admin: bool):
    from bot.handlers.start import main_menu
    await check(bot, user)
    if need := missing(user):
        names = {"chat": "чате", "channel": "канале"}
        return await join_screen(bot, s, user, c, warn("Пока не вижу вас в " + " и ".join(names[n] for n in need)
                                                       + ". Вступите и нажмите «Проверить» ещё раз."))
    events.add(s, f"user:{user.id}", "joined_community", "Вступил в сообщество: чат и канал", user.id, notice=True)
    await main_menu(bot, s, user, is_admin, c, ok("Готово — добро пожаловать в Strait Pay!"))


async def forget_channel_link(s: AsyncSession) -> None:
    """The channel changed: its kept link is no longer valid."""
    if row := await s.get(Setting, "channel_link"):
        await s.delete(row)
    settings._cache.pop("channel_link", None)
