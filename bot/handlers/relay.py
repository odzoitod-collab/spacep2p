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

from bot.services import admins
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
    chat = State()


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
    return "Администрация" if admins.is_admin(uid) else "Пользователь"


def problem(d: Deal | None, sender: int, to: int) -> str:
    """Why `sender` may not write to `to` ("" = he may)."""
    if sender == to:
        return "Нельзя написать самому себе"
    if admins.is_admin(sender) or admins.is_admin(to):  # the administration and answers to it
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
    if d is None or (user.id not in people(d) and not admins.is_admin(user.id)):
        return await c.answer("Сделка не найдена", show_alert=True)
    await state.set_state(None)
    others = [uid for uid in people(d) if uid != user.id]
    await show(bot, user, "\n".join([
        title(pe("support"), f"Написать по сделке #{d.id}"),
        "Сообщение придёт через бота с кнопкой «Ответить». Ссылки и @юзернеймы запрещены — такое сообщение "
        "не отправится.",
    ]), kb(*[btn(role(d, uid), f"dm:{d.id}:{uid}", "support") for uid in others],
           btn("Администрации", f"dm:{d.id}:{admins.ids()[0]}", "support")
           if admins.ids() and not admins.is_admin(user.id) else None,
           back(f"adv:{d.id}" if admins.is_admin(user.id) and user.id not in people(d) else f"dl:{d.id}",
                "Назад к сделке")), c)


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
    whom = role(d, to) if not admins.is_admin(to) or admins.is_admin(sender) else "Администрация"
    if admins.is_admin(sender) and not admins.is_admin(to):
        whom = f"{role(d, to)} · {esc(target.name or '—')} (<code>{to}</code>)"
    return "\n".join([
        title(pe("support"), "Сообщение" + (f" по сделке #{d.id}" if d else "")),
        quote(f"• Кому: <b>{whom}</b>", f"• До {MAX_LEN} символов, одним сообщением",
              "" if admins.is_admin(sender) else "• Ссылки и @юзернеймы запрещены — сообщение не отправится"),
        "Напишите текст сообщения:",
    ]) + (warn(err) if err else "")


def _back(d: Deal | None, sender: int, to: int) -> str:
    if admins.is_admin(sender):
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
    admin = admins.is_admin(user.id)
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
           btn(f"Сделка #{d.id}", f"adv:{d.id}" if admins.is_admin(to) and to not in people(d) else f"dl:{d.id}",
               "fire") if d else None,
           back("x", "Скрыть", "cross")))
    events.add(s, ref, "message", f"{sender_role} → {role(d, to)}"
               + ("" if sent else " (не доставлено)") + f": {m.text[:300]}", user.id, notice=True)
    await _after(bot, s, user, d, to, ok("Сообщение доставлено") if sent
                 else warn("Не доставлено: получатель заблокировал бота"))


async def _after(bot: Bot, s: AsyncSession, user: User, d: Deal | None, to: int, note: str):
    """Back where the conversation started: the admin's deal or profile card, or the deal screen."""
    if admins.is_admin(user.id) and (d is None or user.id not in people(d)):
        from bot.handlers.admin import deal_view, user_screen
        if d is not None:
            return await deal_view(bot, s, user, d, note=note)
        return await user_screen(bot, s, user, await s.get(User, to), note=note)
    if d is not None:
        from bot.handlers.deal import deal_screen
        return await deal_screen(bot, s, user, d, note=note)
    from bot.handlers.start import main_menu
    await main_menu(bot, s, user, admins.is_admin(user.id), note=note)


# ---------- the deal's chat: buyer, merchant, operator, administration ----------

CHAT_SHOWN = 12  # latest messages on the chat screen


def chat_people(d: Deal) -> list[int]:
    """Who is in the chat and gets every message (the administration joins when it writes)."""
    return people(d)


def chat_role(d: Deal, uid: int) -> str:
    return role(d, uid)


async def chat_screen(bot: Bot, s: AsyncSession, user: User, d: Deal, state: FSMContext, src=None, note: str = ""):
    """The chat of a deal: the latest messages and «write here» — every message the user sends now goes to the chat
    until he leaves it with a button."""
    from bot.models import DealMessage
    from bot.ui import at, field, section, visible_len
    await state.set_state(Relay.chat)
    await state.update_data(chat_deal=d.id)
    rows = list(reversed((await s.scalars(select(DealMessage).where(DealMessage.deal_id == d.id)
                                          .order_by(DealMessage.id.desc()).limit(CHAT_SHOWN))).all()))
    members = " · ".join(dict.fromkeys(chat_role(d, uid) for uid in chat_people(d)))
    lines = [f"<b>{esc(r.role)}</b>{' (вы)' if r.sender_id == user.id else ''} · {at(r.created_at)}\n"
             f"{esc(r.text)}" for r in rows]
    head = [title(pe("support"), f"Чат сделки #{d.id}"), "", field("В чате", members + " · администрация"), ""]
    tail = ["", quote("Пишите сюда сообщением — его получат все участники сделки. Ссылки и @юзернеймы запрещены: "
                      "такое сообщение не уйдёт. Общайтесь только здесь.")]
    while lines and visible_len("\n".join(head + ["\n\n".join(lines)] + tail) + note) > 1000:
        lines.pop(0)  # the screen keeps its banner: the oldest messages go first
    body = "<blockquote expandable>" + "\n\n".join(lines) + "</blockquote>" if lines else "<i>Пока сообщений нет.</i>"
    admin_view = admins.is_admin(user.id) and user.id not in people(d)
    await show(bot, user, "\n".join(head + [section("support", "Сообщения"), body] + tail) + note, kb(
        [btn("Обновить", f"dch:{d.id}", "refresh"),
         btn("Администрации", f"dm:{d.id}:{admins.ids()[0]}", "support") if not admin_view and admins.ids() else None],
        back(f"adv:{d.id}" if admin_view else f"dl:{d.id}", "К сделке")), src)


async def _chat_deal(s: AsyncSession, user: User, did: int) -> Deal | None:
    d = await _deal(s, did)
    if d is None or (user.id not in chat_people(d) and not admins.is_admin(user.id)):
        return None
    return d


@router.callback_query(F.data.regexp(r"^dch:(\d+)$"))
async def cb_chat(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await _chat_deal(s, user, int(c.data.split(":")[1]))
    if d is None:
        return await c.answer("Чат недоступен", show_alert=True)
    await chat_screen(bot, s, user, d, state, c)


@router.message(Relay.chat)
async def msg_chat(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await _chat_deal(s, user, (await state.get_data()).get("chat_deal", 0))
    if d is None:
        await state.set_state(None)
        return await show(bot, user, warn("Чат недоступен"), kb(back("menu", "В меню")))
    err = await post(bot, s, user, d, (m.text or "").strip(), forbidden(m))
    await chat_screen(bot, s, user, d, state, note=warn(err) if err else "")


def text_forbidden(text: str) -> bool:
    """A link or a @username in plain text (the mini app sends no Telegram markup)."""
    return bool(LINK.search(text) or MENTION.search(text))


async def post(bot: Bot, s: AsyncSession, user: User, d: Deal, text: str, bad: bool = False) -> str:
    """A message into the deal chat — from the bot or the mini app: the same checks, the history of the deal and a
    notification to every other member. "" or why it was not sent. Commits."""
    from bot.models import DealMessage
    from bot.ui import app_btn
    admin = admins.is_admin(user.id)
    if not text or len(text) > MAX_LEN:
        return f"Только текст, до {MAX_LEN} символов"
    if not admin and (bad or text_forbidden(text)):
        events.add(s, f"deal:{d.id}", "message_blocked", f"{chat_role(d, user.id)} {user.id} пытался отправить в чат "
                   f"ссылку или @юзернейм: {text[:200]}", user.id, alert=True)
        await s.commit()
        return "Не отправлено: ссылки и @юзернеймы запрещены. Общайтесь только здесь."
    recent = await s.scalar(select(func.count(DealMessage.id)).where(
        DealMessage.sender_id == user.id, DealMessage.created_at > now() - timedelta(hours=1)))
    if not admin and recent >= PER_HOUR:
        return "Слишком много сообщений за час — попробуйте позже"
    sender_role = chat_role(d, user.id)
    s.add(DealMessage(deal_id=d.id, sender_id=user.id, role=sender_role[:40], text=text))
    events.add(s, f"deal:{d.id}", "message", f"Чат · {sender_role}: {text[:300]}", user.id, notice=True)
    await s.commit()
    members = " · ".join(dict.fromkeys(chat_role(d, x) for x in chat_people(d)))
    for uid in [x for x in chat_people(d) if x != user.id]:
        await notify(bot, uid, "\n".join([
            f"{pe('support')} <b>Чат сделки #{d.id}</b>",
            f"Пишет: <b>{esc(sender_role)}</b> · вы здесь — {esc(chat_role(d, uid))}",
            quote(esc(text)),
            f"<i>В чате: {esc(members)}{' · администрация' if not admins.is_admin(user.id) else ''}</i>",
        ]), kb(btn("Ответить в чат", f"dch:{d.id}", "support", style="primary"),
               app_btn("Чат в приложении", f"deal/{d.id}/chat"), back("x", "Скрыть", "cross")))
    return ""
