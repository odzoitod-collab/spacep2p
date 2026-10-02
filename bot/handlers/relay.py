"""Messages through the bot. An admin writes to any user; the people of a deal — buyer, merchant, operator — write to
each other and to the administration. Every message arrives with «Ответить», so a conversation goes on in the bot,
and is written to the history of the deal (the admins see it in the log chat). Links and @usernames are not allowed
— a way to take the deal off the platform: such a message is not sent and the admins are alerted. Admins' messages
are not checked."""
import re
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.models import Deal, Event, User, now
from bot.services import events
from bot.ui import esc, notify, ok, quote, show, title, warn

router = Router()
PER_HOUR = 30
MAX_LEN = 1000
LINK = re.compile(r"(https?://|www\.|t\.me/|telegram\.(me|dog)/|tg://|\b[\w-]+\.(ru|com|net|org|me|io|co|xyz|info|biz|"
                  r"su|online|site|top|pro|cc|link|app|dev|shop|store|club|live|gg|ly|to|in)\b)", re.I)
MENTION = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,}")


class Relay(StatesGroup):
    text = State()


def forbidden(m: Message) -> bool:
    """A link or a @username — by Telegram's own markup or by the text itself."""
    if any(e.type in ("url", "text_link", "mention", "text_mention", "email") for e in (m.entities or [])):
        return True
    return bool(LINK.search(m.text or "") or MENTION.search(m.text or ""))


def people(d: Deal) -> list[int]:
    """Who takes part in a deal and can write and be written to (the buyer of an API order is a service)."""
    return [uid for uid in dict.fromkeys((None if d.api_client_id else d.buyer_id, d.seller_id,
                                          d.operator_id if d.via_bybit else None)) if uid]


def role(d: Deal | None, uid: int) -> str:
    if d is not None and uid in people(d):
        return ("Покупатель" if uid == d.buyer_id else "Оператор" if uid == d.operator_id and d.via_bybit
                else "Мерчант")
    return "Администрация" if uid in config.admin_ids else "Пользователь"


def problem(d: Deal | None, sender: int, to: int) -> str:
    """Why `sender` may not write to `to` ("" = he may)."""
    if sender == to:
        return "Нельзя написать самому себе"
    if sender in config.admin_ids or to in config.admin_ids:  # the administration and answers to it
        return ""
    if d is None:
        return "Писать можно участникам своих сделок"
    if sender not in people(d) or to not in people(d):
        return "Этот человек не участвует в сделке"
    return ""


async def _deal(s: AsyncSession, did: int) -> Deal | None:
    return await s.get(Deal, did, populate_existing=True) if did else None


@router.callback_query(F.data.regexp(r"^dmc:(\d+)$"))
async def cb_choose(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    """«Написать» on a deal: whom — the other people of the deal or the administration."""
    d = await _deal(s, int(c.data.split(":")[1]))
    if d is None or (user.id not in people(d) and user.id not in config.admin_ids):
        return await c.answer("Сделка не найдена", show_alert=True)
    await state.set_state(None)
    others = [uid for uid in people(d) if uid != user.id]
    await show(bot, user, "\n".join([
        title(pe("support"), f"Написать по сделке #{d.id}"),
        "Сообщение придёт через бота с кнопкой «Ответить». Ссылки и @юзернеймы запрещены — такое сообщение "
        "не отправится.",
    ]), kb(*[btn(role(d, uid), f"dm:{d.id}:{uid}", "support") for uid in others],
           btn("Администрации", f"dm:{d.id}:{config.admin_ids[0]}", "support")
           if config.admin_ids and user.id not in config.admin_ids else None,
           back(f"dl:{d.id}", "Назад к сделке")), c)


@router.callback_query(F.data.regexp(r"^(?:dm:(\d+):(\d+)|amsg:(\d+))$"))
async def cb_write(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    parts = c.data.split(":")
    did, to = (0, int(parts[1])) if parts[0] == "amsg" else (int(parts[1]), int(parts[2]))
    d = await _deal(s, did)
    target = await s.get(User, to)
    if target is None or (did and d is None):
        return await c.answer("Получатель не найден", show_alert=True)
    if why := problem(d, user.id, to):
        return await c.answer(why, show_alert=True)
    await state.set_state(Relay.text)
    await state.update_data(dm_to=to, dm_deal=did)
    await show(bot, user, _prompt(d, user.id, to, target), kb(back(_back(d, user.id, to), "Отмена")), c)


def _prompt(d: Deal | None, sender: int, to: int, target: User, err: str = "") -> str:
    whom = role(d, to) if to not in config.admin_ids or sender in config.admin_ids else "Администрация"
    if sender in config.admin_ids and to not in config.admin_ids:
        whom = f"{role(d, to)} · {esc(target.name or '—')} (<code>{to}</code>)"
    return "\n".join([
        title(pe("support"), "Сообщение" + (f" по сделке #{d.id}" if d else "")),
        quote(f"• Кому: <b>{whom}</b>", f"• До {MAX_LEN} символов, одним сообщением",
              "" if sender in config.admin_ids else "• Ссылки и @юзернеймы запрещены — сообщение не отправится"),
        "Напишите текст сообщения:",
    ]) + (warn(err) if err else "")


def _back(d: Deal | None, sender: int, to: int) -> str:
    if sender in config.admin_ids:
        return f"adv:{d.id}" if d else f"auv:{to}"
    return f"dl:{d.id}" if d else "menu"


@router.message(Relay.text)
async def msg_text(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    to, did = data.get("dm_to"), data.get("dm_deal", 0)
    d = await _deal(s, did)
    target = await s.get(User, to) if to else None
    if target is None or problem(d, user.id, to):
        await state.set_state(None)
        return await show(bot, user, warn("Сообщение не отправлено: получатель недоступен"), kb(back("menu", "В меню")))
    admin = user.id in config.admin_ids
    if not m.text or not m.text.strip() or len(m.text) > MAX_LEN:
        return await show(bot, user, _prompt(d, user.id, to, target, f"Нужен текст до {MAX_LEN} символов"),
                          kb(back(_back(d, user.id, to), "Отмена")))
    await state.set_state(None)
    ref = f"deal:{d.id}" if d else f"user:{to}"
    if not admin and forbidden(m):
        events.add(s, ref, "message_blocked", f"{role(d, user.id)} {user.id} пытался отправить ссылку или @юзернейм "
                   f"({role(d, to)}): {m.text[:200]}", user.id, alert=True)
        return await _after(bot, s, user, d, to, warn("Сообщение не отправлено: ссылки и @юзернеймы запрещены. "
                                                      "Общайтесь только через бота."))
    recent = await s.scalar(select(func.count(Event.id)).where(
        Event.user_id == user.id, Event.kind == "message", Event.created_at > now() - timedelta(hours=1)))
    if not admin and recent >= PER_HOUR:
        return await _after(bot, s, user, d, to, warn("Слишком много сообщений за час — попробуйте позже"))
    sender_role = role(d, user.id)
    reply_to = user.id
    sent = await notify(bot, to, "\n".join([
        f"{pe('support')} <b>Сообщение" + (f" · сделка #{d.id}" if d else "") + "</b>",
        f"• От: <b>{sender_role}</b>",
        quote(esc(m.text)),
        "Ответить можно кнопкой ниже — сообщение придёт через бота.",
    ]), kb(btn("Ответить", f"dm:{did}:{reply_to}", "support", style="primary"),
           btn(f"Сделка #{d.id}", f"adv:{d.id}" if to in config.admin_ids and to not in people(d) else f"dl:{d.id}",
               "fire") if d else None,
           back("x", "Скрыть", "cross")))
    events.add(s, ref, "message", f"{sender_role} → {role(d, to)}"
               + ("" if sent else " (не доставлено)") + f": {m.text[:300]}", user.id, notice=True)
    await _after(bot, s, user, d, to, ok("Сообщение доставлено") if sent
                 else warn("Не доставлено: получатель заблокировал бота"))


async def _after(bot: Bot, s: AsyncSession, user: User, d: Deal | None, to: int, note: str):
    """Back where the conversation started: the admin's deal or profile card, or the deal screen."""
    if user.id in config.admin_ids and (d is None or user.id not in people(d)):
        from bot.handlers.admin import deal_view, user_screen
        if d is not None:
            return await deal_view(bot, s, user, d, note=note)
        return await user_screen(bot, s, user, await s.get(User, to), note=note)
    if d is not None:
        from bot.handlers.deal import deal_screen
        return await deal_screen(bot, s, user, d, note=note)
    from bot.handlers.start import main_menu
    await main_menu(bot, s, user, user.id in config.admin_ids, note=note)
