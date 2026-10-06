from contextlib import suppress
from datetime import timedelta

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import ui
from bot.models import LogMessage, Session, User, now
from bot.services import admins, deals, events, money, settings
from bot.emoji import pe
from bot.ui import esc, show

SEEN_EVERY = timedelta(seconds=60)  # last_seen resolution: enough for auto-offline, saves a write per click


def banned_text(user: User) -> str:
    sup = settings.get("support")
    return "\n".join([
        f"{pe('ban')} <b>Аккаунт заблокирован</b>",
        "",
        f"Баланс сохранён: {money.usdt(user.balance)} USDT доступно, {money.usdt(user.frozen)} USDT заморожено.",
        f"Для разблокировки или вывода средств напишите в поддержку: @{esc(sup)}" if sup
        else "Для разблокировки или вывода средств обратитесь к администратору.",
        f"Ваш ID: <code>{user.id}</code>",
    ])


def let_in(user: User, uid: int) -> bool:
    """May use the bot: entry is open, he was approved, or he is an admin (handlers.signup)."""
    return admins.is_admin(uid) or user.access == "approved" or settings.get("signup_review") != "1"


def admin_chat(chat_id: int) -> bool:
    """The admin chat: the group the logs go to (handlers.logchat)."""
    from bot.handlers.logchat import targets
    return chat_id < 0 and chat_id in targets()


_prompt_in: dict[int, int] = {}  # admin id -> the group where the question he is answering was asked


def _touch(user: User, tg) -> None:
    user.username = tg.username
    user.name = (tg.full_name or "")[:128]
    if user.last_seen is None or now() - deals.aware(user.last_seen) >= SEEN_EVERY:
        user.last_seen = now()


async def _commit(s: AsyncSession, user: User, tg, old_screen: int | None) -> None:
    if user.ui_msg_id is None:
        user.ui_msg_id = old_screen  # the handler drew nothing: the old screen stays current
    _touch(user, tg)
    await s.commit()
    events.kick()


class Context(BaseMiddleware):
    """DB session per update, user upsert, ban gate, screen placement and deletion of answered prompts.
    Runs inside UserIsolation: updates of one user never overlap.

    Groups: screens and dialogs live in the private chat, except the admin chat — there an admin's commands and his
    answers to the bot's questions are handled like in the private chat, and every screen for him stays in that
    chat (ui.place). Anyone else's buttons there get «Только для администраторов» and nothing more."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        tg = data.get("event_from_user")
        if tg is None or not isinstance(event, Update):
            return await handler(event, data)
        if not ui.BOT:
            with suppress(TelegramAPIError):
                ui.BOT = (await data["bot"].me()).username or ""
        msg = event.message
        is_admin = admins.is_admin(tg.id)
        if event.chat_member is not None:  # joins in the community chat (handlers.admin_chat)
            async with Session() as s:
                data.update(s=s)
                result = await handler(event, data)
                await s.commit()
            events.kick()
            return result
        if event.inline_query is not None:  # inline search (deals, operations): no screen, just results
            async with Session() as s:
                user = await s.get(User, tg.id)
                if user is None or (user.is_banned and not is_admin) or not let_in(user, tg.id):
                    with suppress(TelegramAPIError):
                        await event.inline_query.answer([], cache_time=0, is_personal=True)
                    return None
                data.update(s=s, user=user, is_admin=is_admin)
                result = await handler(event, data)
                await s.commit()
                return result
        group = None  # (chat, topic) of an admin's update in the admin chat
        cq = event.callback_query
        if cq is not None and cq.message is not None and cq.message.chat.type != "private" \
                and admin_chat(cq.message.chat.id):
            if not is_admin:
                with suppress(TelegramAPIError):
                    await cq.answer("Только для администраторов.", show_alert=True)
                return None
            group = (cq.message.chat.id, getattr(cq.message, "message_thread_id", None))
        if msg is not None and msg.chat.type != "private":
            cmd = ((msg.text or "").split() or [""])[0].lower()
            if admin_chat(msg.chat.id):
                if not is_admin:
                    return None  # silence: commands of the admin chat are not to be found by trying
                answer = (not cmd.startswith("/") and bool(msg.text) and data.get("raw_state") is not None
                          and _prompt_in.get(tg.id) == msg.chat.id)
                if not cmd.startswith("/") and not answer:
                    return None  # admins talking in the chat
                group = (msg.chat.id, msg.message_thread_id)
                if not answer:
                    ui.forget_group_screen(tg.id, msg.chat.id)  # a command: a new message, the old one stays
            else:
                # community and team chats: an admin's /setts (sets up the admin chat), a team leader's /team
                # (connects his chat) and /help (the guides) for anyone
                if not ((cmd == "/setts" or cmd.startswith("/setts@")) and is_admin) \
                        and cmd.split("@")[0] not in ("/team", "/help"):
                    return None
                async with Session() as s:
                    data.update(s=s, is_admin=is_admin)
                    result = await handler(event, data)
                    await s.commit()
                    return result
        if msg is None and cq is None:
            return None  # channel posts, edits etc.
        bot: Bot = data["bot"]
        # A typed answer to the bot's own question (amount, card number, name) is removed once processed and the
        # question screen is edited in place. Everything else — commands, free text, files (receipts, evidence) —
        # stays in the chat, and the reply comes as a new screen below it.
        # A card picked in the inline search arrives as "/deal 15" sent via the bot: treated the same way — the
        # message goes, the current screen turns into the details.
        if cq is not None and data.get("raw_state") == "Relay:chat" and not (cq.data or "").startswith("dch:"):
            await data["state"].set_state(None)  # left the deal's chat by a button: typed text is no chat message
            data["raw_state"] = None
        picked = bool(msg and msg.via_bot and msg.via_bot.id == data["bot"].id)
        answered_prompt = picked or bool(msg and msg.text and not msg.text.startswith("/") and data.get("raw_state"))
        token, ref_token = ui.place.set((tg.id, *group) if group else None), ui.card_ref.set(None)
        try:
            async with Session() as s:
                user = await s.get(User, tg.id)
                if user is None:
                    user = User(id=tg.id)
                    s.add(user)  # nothing to the admin chat: an application to enter (signup) is what it gets
                if user.is_banned and not is_admin:
                    if cq:
                        with suppress(TelegramAPIError):
                            await cq.answer("Ваш аккаунт заблокирован", show_alert=True)
                    with suppress(TelegramAPIError):
                        await show(bot, user, banned_text(user))
                    _touch(user, tg)
                    await s.commit()
                    return None
                if user.access in (None, "new") and settings.get("signup_review") != "1":  # None: not flushed yet
                    user.access = "approved"  # entry is open: nobody waits (and nobody is locked out if it closes)
                data.update(s=s, user=user, is_admin=is_admin)
                if group and cq is not None:  # pressed under a log card: its screens can fold back into it
                    ui.card_ref.set(await s.scalar(select(LogMessage.ref).where(
                        LogMessage.chat_id == group[0], LogMessage.msg_id == cq.message.message_id)))
                old_screen = user.ui_msg_id
                if msg and not answered_prompt and not group:
                    user.ui_msg_id = None  # the answer appears below the user's message, not in a screen further up
                try:
                    result = await handler(event, data)
                except TelegramAPIError:
                    # Telegram failed to show the result (network, bad markup...). Handlers change data
                    # before rendering, so keep what was done: a card added or enabled must not vanish
                    # because its screen could not be drawn.
                    await _commit(s, user, tg, old_screen)
                    raise
                await _commit(s, user, tg, old_screen)
        finally:
            ui.place.reset(token)
            ui.card_ref.reset(ref_token)
        if is_admin and "state" in data:  # where an open question of an admin was asked: there it is answered
            if group and await data["state"].get_state() is not None:
                _prompt_in[tg.id] = group[0]
            else:
                _prompt_in.pop(tg.id, None)
        if answered_prompt:
            with suppress(TelegramAPIError):
                await msg.delete()
        return result
