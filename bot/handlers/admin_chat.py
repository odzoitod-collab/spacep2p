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
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.models import Card, Deal, Event, OrderMerchant, Session, Setting, User, now
from bot.services import audit, deals, events, money, orders, settings
from bot.ui import MSK, clean, esc, notify, ok, paced, quote, show, title

log = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id.in_(config.admin_ids))
router.callback_query.filter(F.from_user.id.in_(config.admin_ids))
events_router = Router()  # open to everyone: «Чат» for users, joins in the community chat
LINKS_PER_HOUR = 5


def chat_id() -> int | None:
    v = settings.get("chat_id")
    return int(v) if v else None


# ---------- user: a personal invite link ----------

@events_router.callback_query(F.data == "chat")
async def cb_chat(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    chat = chat_id()
    if chat is None:
        return await c.answer("Чат пока не подключён", show_alert=True)
    recent = await s.scalar(select(func.count(Event.id)).where(
        Event.ref == f"user:{user.id}", Event.kind == "chat_link", Event.created_at > now() - timedelta(hours=1)))
    if recent >= LINKS_PER_HOUR:
        return await c.answer("Ссылок за час слишком много — используйте последнюю или попробуйте позже",
                              show_alert=True)
    try:
        link = await bot.create_chat_invite_link(chat, name=str(user.id), member_limit=1,
                                                 expire_date=now() + timedelta(hours=1))
    except TelegramAPIError as e:
        log.warning("invite link for %s: %s", user.id, e)
        events.add(s, "app:chat", "link_failed", f"Бот не смог создать ссылку в чат {chat}: {e}"[:300], alert=True)
        return await c.answer("Чат временно недоступен, попробуйте позже", show_alert=True)
    events.add(s, f"user:{user.id}", "chat_link", "Получил личную ссылку в чат", user.id, notice=True)
    await show(bot, user, "\n".join([
        title(pe("people"), "Чат Strait Pay"),
        "Курс, новости и сводка — в закрепе чата.",
        quote("Ссылка личная: на один вход, действует 1 час"),
    ]), kb(btn("Вступить в чат", url=link.invite_link, icon="people", style="success"), back("menu", "В меню")), c)


@events_router.chat_member()
async def on_member(e: ChatMemberUpdated, s: AsyncSession):
    """A user joined the community chat by his personal link: one line in his log card."""
    if e.chat.id != chat_id() or e.new_chat_member.status != "member" or e.old_chat_member.status == "member":
        return
    name = (e.invite_link.name or "") if e.invite_link else ""
    joined = e.new_chat_member.user
    owner = int(name) if name.isdigit() else None
    who = f"@{joined.username}" if joined.username else joined.full_name
    events.add(s, f"user:{owner or joined.id}", "chat_join",
               f"Вступил в чат: {who}" + ("" if owner in (None, joined.id) else f" — по ссылке пользователя {owner}"),
               owner or joined.id, alert=owner not in (None, joined.id), notice=True)


# ---------- the pinned summary ----------

async def summary(s: AsyncSession) -> dict:
    t = now()
    count = lambda *where: s.scalar(select(func.count()).where(*where))  # noqa: E731
    done = lambda *where: s.execute(select(func.count(Deal.id), func.coalesce(func.sum(Deal.amount_rub), 0))  # noqa: E731
                                    .where(Deal.status == "completed", *where))
    n24, rub24 = (await done(Deal.closed_at >= t - timedelta(hours=24))).one()
    n_all, rub_all = (await done()).one()
    return {
        "online": await count(User.is_online, ~User.is_banned),
        "cards": len(await deals.market(s, 0, None, None, None)),
        "om": await count(OrderMerchant.status == "approved", OrderMerchant.accepting),
        "open": await count(Deal.status.in_(("waiting_payment", "paid"))),
        "requests": await count(Deal.status.in_(orders.REQUEST)),
        "day": (n24, Decimal(rub24)), "all": (n_all, Decimal(rub_all)),
        "users": await s.scalar(select(func.count(User.id))),
    }


def pin_text(st: dict, bot_name: str) -> str:
    rate, pp = settings.dec("rate"), settings.dec("platform_pct")
    example = money.quote(Decimal(10000), rate, settings.dec("seller_pct"), pp).buyer_credit
    return clean("\n".join([
        f"📌 <b>Strait Pay · сводка</b> · {now().astimezone(MSK):%d.%m %H:%M} МСК",
        "",
        "<b>Курс</b>",
        f"1 USDT = <b>{money.fmt(rate)} ₽</b> · комиссия {money.fmt(pp, 3)}%",
        f"10 000 ₽ → <b>{money.usdt(example)} USDT</b>",
        "",
        "<b>Сейчас</b>",
        f"Продавцов на смене: <b>{st['online']}</b> · карт доступно: <b>{st['cards']}</b>",
        f"Ордерных мерчантов на приёме: <b>{st['om']}</b>",
        f"Активных сделок: <b>{st['open']}</b> · заявок на реквизиты: <b>{st['requests']}</b>",
        "",
        "<b>Оборот</b>",
        f"За 24 ч: <b>{st['day'][0]}</b> сделок · <b>{money.fmt(st['day'][1])} ₽</b>",
        f"За всё время: <b>{st['all'][0]}</b> сделок · <b>{money.fmt(st['all'][1])} ₽</b>",
        f"Пользователей: <b>{st['users']}</b>",
        "",
        f"Купить или продать USDT — @{bot_name}" if bot_name else "",
    ]))


async def publish_pin(bot: Bot, s: AsyncSession) -> str:
    """Edit the pinned summary or post and pin a new one. Returns what happened (for the admin screen)."""
    chat = chat_id()
    if chat is None:
        return "чат не задан"
    text = pin_text(await summary(s), (await bot.me()).username or "")
    key = f"chat_pin:{chat}"
    row = await s.get(Setting, key)
    if row:
        try:
            await paced(lambda: bot.edit_message_text(text=text, chat_id=chat, message_id=int(row.value),
                                                      disable_web_page_preview=True))
            return "обновлён"
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return "актуален"
            # deleted or too old: post it again
    try:
        m = await paced(lambda: bot.send_message(chat, text, disable_notification=True, disable_web_page_preview=True))
        await s.merge(Setting(key=key, value=str(m.message_id)))
        await s.commit()
        await bot.pin_chat_message(chat, m.message_id, disable_notification=True)
    except TelegramAPIError as e:
        await events.alert_once(s, "app:chat", "pin_failed", f"Закреп в чате {chat} не обновлён: {e}"[:300])
        await s.commit()
        return f"ошибка: {esc(str(e)[:120])}"
    return "опубликован и закреплён"


# ---------- admin: chat screen ----------

@router.callback_query(F.data.in_({"ach", "ach:pin"}))
async def cb_chat_admin(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    note = ok(f"Закреп: {await publish_pin(bot, s)}") if c.data == "ach:pin" else ""
    await chat_screen(bot, s, user, c, note)


async def chat_screen(bot: Bot, s: AsyncSession, admin: User, src=None, note: str = ""):
    chat = chat_id()
    lines = [title(pe("people"), "Чат сообщества")]
    if chat is None:
        lines += ["Чат не подключён.",
                  quote("1. Создайте группу и добавьте бота администратором",
                        "2. Права: «Приглашать пользователей» и «Закреплять сообщения»",
                        "3. Узнайте ID группы (например, через @getidsbot) и задайте его здесь")]
    else:
        try:
            info = await bot.get_chat(chat)
            me = await bot.get_chat_member(chat, (await bot.me()).id)
            rights = [("приглашать", getattr(me, "can_invite_users", False)),
                      ("закреплять", getattr(me, "can_pin_messages", False))]
            state = quote(f"Чат: <b>{esc(info.title or str(chat))}</b> · <code>{chat}</code>",
                          "Права бота: " + ", ".join(f"{'✅' if v else '❌'} {k}" for k, v in rights))
        except TelegramAPIError as e:
            state = quote(f"ID: <code>{chat}</code>", f"{pe('warn')} Бот не видит чат: {esc(str(e)[:120])}")
        joins = await s.scalar(select(func.count(Event.id)).where(Event.kind == "chat_join"))
        lines += [state, f"Вступили по личным ссылкам: <b>{joins}</b>",
                  "Кнопка «Чат» в меню выдаёт каждому личную ссылку на один вход. Закреп обновляется каждые 5 мин."]
    await show(bot, admin, "\n".join(lines) + note, kb(
        btn("Обновить закреп", "ach:pin", "refresh", style="primary") if chat else None,
        btn("Изменить ID чата" if chat else "Задать ID чата", "acx:chat_id", "pencil"),
        btn("Рассылка", "abc", "bell"),
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
    await state.update_data(bc_msg=m.message_id)
    n = len(await recipients(s, who))
    await bot.copy_message(user.id, user.id, m.message_id)  # the preview, exactly as recipients will see it
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
