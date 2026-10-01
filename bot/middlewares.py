from contextlib import suppress
from datetime import timedelta

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import TelegramObject, Update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.models import Session, User, now
from bot.services import deals, events, money, settings
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
    Runs inside UserIsolation: updates of one user never overlap."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        tg = data.get("event_from_user")
        if tg is None or not isinstance(event, Update):
            return await handler(event, data)
        msg = event.message
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
                if user is None or (user.is_banned and tg.id not in config.admin_ids):
                    with suppress(TelegramAPIError):
                        await event.inline_query.answer([], cache_time=0, is_personal=True)
                    return None
                data.update(s=s, user=user, is_admin=tg.id in config.admin_ids)
                result = await handler(event, data)
                await s.commit()
                return result
        if msg is not None and msg.chat.type != "private":
            # groups (the log chat): screens and dialogs live only in the private chat; the one exception is an
            # admin's /setts that sets up the log chat right from the group
            if tg.id not in config.admin_ids or not (msg.text or "").startswith("/setts"):
                return None
            async with Session() as s:
                data.update(s=s, is_admin=True)
                result = await handler(event, data)
                await s.commit()
                return result
        if msg is None and event.callback_query is None:
            return None  # channel posts, edits etc.
        bot: Bot = data["bot"]
        is_admin = tg.id in config.admin_ids
        # A typed answer to the bot's own question (amount, card number, name) is removed once processed and the
        # question screen is edited in place. Everything else — commands, free text, files (receipts, evidence) —
        # stays in the chat, and the reply comes as a new screen below it.
        # A card picked in the inline search arrives as "/deal 15" sent via the bot: treated the same way — the
        # message goes, the current screen turns into the details.
        picked = bool(msg and msg.via_bot and msg.via_bot.id == data["bot"].id)
        answered_prompt = picked or bool(msg and msg.text and not msg.text.startswith("/") and data.get("raw_state"))
        async with Session() as s:
            user = await s.get(User, tg.id)
            if user is None:
                user = User(id=tg.id)
                s.add(user)
                events.add(s, f"user:{tg.id}", "registered",
                           f"Новый пользователь {tg.full_name or ''} @{tg.username or '—'}"[:200], tg.id, notice=True)
            if user.is_banned and not is_admin:
                if event.callback_query:
                    with suppress(TelegramAPIError):
                        await event.callback_query.answer("Ваш аккаунт заблокирован", show_alert=True)
                with suppress(TelegramAPIError):
                    await show(bot, user, banned_text(user))
                _touch(user, tg)
                await s.commit()
                return None
            data.update(s=s, user=user, is_admin=is_admin)
            old_screen = user.ui_msg_id
            if msg and not answered_prompt:
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
        if answered_prompt:
            with suppress(TelegramAPIError):
                await msg.delete()
        return result
