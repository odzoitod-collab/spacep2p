"""Admin log chat: forum topics per kind of log and one live card per operation.

If the log chat is a forum supergroup, the bot creates its topics itself (and re-creates a topic an admin deleted):
deals, order requisites, deposits, withdrawals, TON, API, order merchants, users, cards, adjustments, tickets,
service, and "needs attention". Every operation (deal #15, withdrawal #7, API application #2 …) has one card in its
topic: status badge, key facts and the latest events. New events edit the card in place (silently); events that
need an admin also go to the "needs attention" topic, which rings. Without topics (a plain group or admins'
private chats) a card is edited for routine steps and re-posted at the bottom when something needs attention.
Needs the bot to be an admin with the "Manage topics" right to create topics; otherwise everything goes to the
general chat.
"""
import logging
from collections import OrderedDict
from contextlib import suppress

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Chat, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.emoji import back, btn, kb
from bot.models import (Adjustment, ApiApplication, ApiClient, Card, Deal, Deposit, Event, LogMessage, OrderMerchant,
                        Setting, Ticket, TonDeposit, TonOp, User, Withdrawal, now)
from bot.services import events, money
from bot.ui import at, clean, esc, paced

log = logging.getLogger(__name__)

# key -> (topic title, icon colour; Telegram allows only these six)
TOPICS = {
    "attention": ("⚠️ Требует внимания", 16478047),
    "stats": ("📊 Статистика и финансы", 9367192),
    "deals": ("💱 Сделки", 7322096),
    "orders": ("🧾 Ордерные реквизиты", 13338331),
    "deposits": ("📥 Пополнения", 9367192),
    "withdrawals": ("📤 Выводы", 16766590),
    "ton": ("💎 TON: автопереводы и газ", 7322096),
    "api": ("🔑 API: заявки и клиенты", 13338331),
    "om": ("🤝 Ордерные мерчанты", 9367192),
    "users": ("👤 Пользователи", 16749490),
    "cards": ("💳 Карты", 16766590),
    "adjust": ("✏️ Корректировки", 16749490),
    "tickets": ("💬 Обращения", 7322096),
    "service": ("⚙️ Сервис", 16478047),
}
KIND_TOPIC = {"wd": "withdrawals", "dep": "deposits", "tdep": "deposits", "tsw": "ton", "apa": "api", "apc": "api",
              "om": "om", "user": "users", "card": "cards", "adj": "adjust", "ticket": "tickets", "app": "service"}
ABOUT = {
    "attention": "Всё, что ждёт решения администратора: споры, отказы xRocket, выводы на проверке, новые анкеты и "
                 "заявки, нехватка газа. Эту ветку стоит держать со звуком.",
    "stats": "Одно живое сообщение: сколько денег на xRocket и на вашем кошельке, сколько должны пользователям, "
             "прибыль и сколько можно забрать. Обновляется каждые 10 минут.",
    "deals": "Сделки по статичным картам: одна карточка на сделку, статус меняется по ходу.",
    "orders": "Сделки по ордерным реквизитам: поиск мерчанта, выдача реквизитов, оплата, итог.",
    "deposits": "Пополнения: счета xRocket и USDT в сети TON (сумма, хеш транзакции, отправитель).",
    "withdrawals": "Выводы: чеки xRocket и переводы USDT в сети TON — от запроса до выполнения.",
    "ton": "Автопереводы USDT с адресов пополнения и газ (TON на комиссии).",
    "api": "Заявки на Strait Pay API и API-клиенты: одобрение, токены, приостановка.",
    "om": "Анкеты ордерных мерчантов и их статус.",
    "users": "Новые пользователи, баны, личные ставки мерчантов.",
    "cards": "Карты продавцов: добавление, блокировка.",
    "adjust": "Ручные корректировки баланса.",
    "tickets": "Обращения пользователей в поддержку.",
    "service": "Сервисные сообщения: xRocket, TON, адрес автоперевода.",
}
IMPORTANT = ("failed", "unknown", "refunded", "dispute", "late_no_funds")
TIMELINE = 6
GOOD, WAIT, BAD, NEUTRAL, ATTENTION = "🟢", "🟡", "🔴", "⚪️", "🟠"

_forum: dict[int, bool] = {}  # chat id -> is a forum (checked once per process)
_topics: dict[tuple[int, str], int | None] = {}  # (chat, key) -> thread id


def important(ev: Event) -> bool:
    kind = ev.ref.partition(":")[0]
    if kind in ("tdep", "tsw"):
        return ev.kind == "failed"  # a deposit or a completed sweep is news, not a problem
    return not ev.notice or ev.kind in IMPORTANT


def targets() -> list[int]:
    """Where logs go: the group where an admin sent /setts, else LOG_CHAT_ID, else the admins' private chats."""
    from bot.services import settings
    chosen = settings._cache.get("log_chat")
    return [int(chosen)] if chosen else config.log_targets


# ---------- topics ----------

async def is_forum(bot: Bot, chat: int) -> bool:
    if chat not in _forum:
        try:
            info = await bot.get_chat(chat)
            _forum[chat] = isinstance(info, Chat) and bool(info.is_forum)
        except TelegramAPIError:
            _forum[chat] = False
    return _forum[chat]


async def thread(bot: Bot, s: AsyncSession, chat: int, key: str) -> int | None:
    """Thread id of the topic, created on first use. None: post to the general chat."""
    if not await is_forum(bot, chat):
        return config.log_thread_id if chat == config.log_chat_id and config.log_thread_id else None
    if (chat, key) in _topics:
        return _topics[(chat, key)]
    row = await s.get(Setting, f"topic:{chat}:{key}")
    if row:
        _topics[(chat, key)] = int(row.value)
        return _topics[(chat, key)]
    name, color = TOPICS[key]
    try:
        topic = await bot.create_forum_topic(chat, name, icon_color=color)
    except TelegramAPIError as e:  # no "Manage topics" right: general chat, try again after a restart
        log.warning("log topic %s not created in %s: %s", key, chat, e)
        _topics[(chat, key)] = None
        return None
    _topics[(chat, key)] = topic.message_thread_id
    await s.merge(Setting(key=f"topic:{chat}:{key}", value=str(topic.message_thread_id)))
    return topic.message_thread_id


async def forget_topic(s: AsyncSession, chat: int, key: str) -> None:
    _topics.pop((chat, key), None)
    if row := await s.get(Setting, f"topic:{chat}:{key}"):
        await s.delete(row)
        await s.flush()  # the next lookup in this session must not find it


def _thread_gone(e: Exception) -> bool:
    text = str(e).lower()
    return isinstance(e, TelegramBadRequest) and ("thread" in text or "topic" in text)


async def post(bot: Bot, s: AsyncSession, chat: int, key: str, text: str, markup, silent: bool):
    """Send into a topic; if an admin deleted the topic, create it again once."""
    for attempt in range(2):
        tid = await thread(bot, s, chat, key)
        try:
            return await paced(lambda: bot.send_message(chat, text, reply_markup=markup, message_thread_id=tid,
                                                        disable_notification=silent, disable_web_page_preview=True))
        except TelegramBadRequest as e:
            if attempt or tid is None or not _thread_gone(e):
                raise
            await forget_topic(s, chat, key)


# ---------- cards ----------

def _badge(status: str) -> str:
    if status in ("completed", "done", "paid", "approved", "active", "answered", "credited"):
        return GOOD
    if status in ("failed", "rejected", "suspended", "banned"):
        return BAD
    if status in ("dispute", "unknown"):
        return ATTENTION
    if status in ("cancelled", "void", "expired", "closed", "deleted"):
        return NEUTRAL
    return WAIT


async def describe(s: AsyncSession, ref: str) -> tuple[str, str, str, list[str]]:
    """(topic key, status code, status label, facts) of an operation, read from its current state."""
    from bot.handlers.admin import ADJ_STATUS, DEP_LABEL, WD_LABEL
    from bot.handlers.admin_api import APP_STATUS
    from bot.handlers.admin_ops import TICKET_STATUS
    from bot.handlers.admin_orders import STATUS as OM_STATUS
    from bot.handlers.admin_ton import OP_STATUS
    from bot.handlers.deal import STATUS as DEAL_STATUS
    kind, _, oid = ref.partition(":")
    topic = KIND_TOPIC.get(kind, "service")
    obj = None
    if kind != "app" and oid.isdigit():
        model = {"deal": Deal, "wd": Withdrawal, "dep": Deposit, "tdep": TonDeposit, "tsw": TonOp, "apa": ApiApplication,
                 "apc": ApiClient, "om": OrderMerchant, "ticket": Ticket, "adj": Adjustment, "user": User,
                 "card": Card}.get(kind)
        obj = await s.get(model, int(oid)) if model else None
    if obj is None:
        return topic, "attention" if kind == "app" else "unknown", "сервис" if kind == "app" else "", []
    if kind == "deal":
        facts = [f"{money.fmt(obj.amount_rub)} ₽ → {money.usdt(obj.buyer_credit)} USDT",
                 f"покупатель <code>{obj.buyer_id}</code>" + (f" · продавец <code>{obj.seller_id}</code>"
                                                               if obj.seller_id else "")
                 + (" · API" if obj.api_client_id else "")]
        return ("orders" if obj.is_order else "deals"), obj.status, DEAL_STATUS[obj.status][1], facts
    if kind == "wd":
        where = f"на {obj.address[:6]}…{obj.address[-4:]}" if obj.method == "ton" else "чеком xRocket"
        return topic, obj.status, WD_LABEL.get(obj.status, obj.status), [
            f"{money.usdt(obj.amount)} USDT {where} · пользователь <code>{obj.user_id}</code>"]
    if kind == "dep":
        return topic, obj.status, DEP_LABEL.get(obj.status, obj.status), [
            f"xRocket · {money.usdt(obj.amount)} USDT · пользователь <code>{obj.user_id}</code>"]
    if kind == "tdep":
        return topic, "credited", "зачислено", [
            f"USDT TON · +{money.usdt(obj.amount)} USDT · пользователь <code>{obj.user_id}</code>"]
    if kind == "tsw":
        return topic, obj.status, OP_STATUS.get(obj.status, obj.status), [
            f"{'автоперевод' if obj.kind == 'sweep' else 'газ'} {money.usdt(obj.amount)} · пользователь "
            f"<code>{obj.user_id}</code>"]
    if kind == "apa":
        return topic, obj.status, APP_STATUS[obj.status], [f"{esc(obj.project)} · {esc(obj.url)} · {esc(obj.volume)}"]
    if kind == "apc":
        return topic, obj.status, "активен" if obj.status == "active" else "приостановлен", [esc(obj.project)]
    if kind == "om":
        return topic, obj.status, OM_STATUS[obj.status], [
            f"{money.fmt(obj.min_rub)}–{money.fmt(obj.max_rub)} ₽ · в работе до {money.fmt(obj.max_open_rub)} ₽"]
    if kind == "ticket":
        return topic, obj.status, TICKET_STATUS[obj.status], [f"пользователь <code>{obj.user_id}</code>"]
    if kind == "adj":
        return topic, obj.status, ADJ_STATUS[obj.status], [
            f"{'+' if obj.delta > 0 else ''}{money.usdt(obj.delta)} USDT · пользователь <code>{obj.user_id}</code>"]
    if kind == "user":
        code = "banned" if obj.is_banned else "active"
        return topic, code, "заблокирован" if obj.is_banned else "активен", [
            f"{esc(obj.name or '—')} @{esc(obj.username or '—')}"]
    if kind == "card":
        code = "deleted" if obj.is_deleted else "banned" if obj.is_banned else "active" if obj.is_active else "off"
        label = {"deleted": "удалена", "banned": "заблокирована", "active": "включена", "off": "выключена"}[code]
        return topic, code, label, [f"{esc(obj.bank)} · владелец <code>{obj.user_id}</code>"]
    return topic, "", "", []


async def render(s: AsyncSession, ref: str, attention: bool) -> tuple[str, str, object]:
    """(topic key, card text, markup)."""
    from bot.handlers.admin_ops import REF_NAMES
    kind, _, oid = ref.partition(":")
    topic, code, label, facts = await describe(s, ref)
    badge = ATTENTION if attention and code not in ("completed", "done") else _badge(code)
    if kind == "app":
        head = f"{badge} <b>Сервис · {esc(oid)}</b>"
        markup = kb(btn("USDT TON", "atn", "wallet") if oid == "ton" else btn("Ввод и вывод", "al", "wallet"))
    else:
        name, cb = REF_NAMES.get(kind, ("Событие", ""))
        head = f"{badge} <b>{name} #{oid}</b>" + (f" · {label}" if label else "")
        markup = kb(btn("Открыть", f"{cb}:{oid}", "search")) if cb else None
    if attention:
        head += " · <b>нужно внимание</b>"
    rows = (await s.scalars(select(Event).where(Event.ref == ref).order_by(Event.id.desc()).limit(TIMELINE))).all()
    timeline = [f"{at(e.created_at)} {esc(e.text[:300])}" for e in reversed(rows)]
    text = "\n".join([head, *facts, "", *timeline])
    return topic, clean(text)[:3900], markup


# ---------- delivery ----------

async def deliver(bot: Bot, s: AsyncSession) -> None:
    """Outbox -> log targets. Several events of one operation become one card update. Commits."""
    pending = await events.outbox(s)
    by_ref: OrderedDict[str, list[Event]] = OrderedDict()
    for ev in pending:
        by_ref.setdefault(ev.ref, []).append(ev)
    for ref, evs in by_ref.items():
        attention = any(important(ev) for ev in evs)
        topic, text, markup = await render(s, ref, attention)
        delivered = False
        for chat in targets():
            with suppress(TelegramAPIError):
                await _to_chat(bot, s, chat, ref, topic, text, markup, attention, evs)
                delivered = True
        for ev in evs:
            if delivered:
                ev.sent_at = now()
            else:
                ev.attempts += 1
        await s.commit()


async def _to_chat(bot: Bot, s: AsyncSession, chat: int, ref: str, topic: str, text: str, markup, attention: bool,
                   evs: list[Event]) -> None:
    card = await s.scalar(select(LogMessage).where(LogMessage.chat_id == chat, LogMessage.ref == ref))
    forum = await is_forum(bot, chat)
    if card is not None and (forum or not attention):
        try:  # routine step (or any step in a forum): the card changes in place, silently
            await paced(lambda: bot.edit_message_text(text=text, chat_id=chat, message_id=card.msg_id,
                                                      reply_markup=markup, disable_web_page_preview=True))
            card.updated_at = now()
        except TelegramBadRequest as e:
            if "not modified" not in str(e):  # deleted by an admin: a new card
                card = await _new_card(bot, s, chat, ref, topic, text, markup, card)
    else:
        if card is not None:  # plain chat and it needs an admin: fresh card at the bottom so it rings
            with suppress(TelegramAPIError):
                await bot.edit_message_text(text=text.split("\n", 1)[0] + "\n↓ обновлено ниже", chat_id=chat,
                                            message_id=card.msg_id)
        card = await _new_card(bot, s, chat, ref, topic, text, markup, card)
    if forum and attention:
        head = text.split("\n", 1)[0]
        what = "\n".join(esc(ev.text[:300]) for ev in evs if important(ev))
        await post(bot, s, chat, "attention", clean(f"{head}\n{what}"), markup or kb(back("x", "Скрыть", "cross")),
                   silent=False)


async def _new_card(bot, s, chat, ref, topic, text, markup, card: LogMessage | None) -> LogMessage:
    m = await post(bot, s, chat, topic, text, markup, silent=False)
    if card is None:
        card = LogMessage(chat_id=chat, ref=ref, msg_id=m.message_id)
        s.add(card)
    card.msg_id, card.thread_id, card.updated_at = m.message_id, m.message_thread_id, now()
    await s.flush()
    return card


# ---------- /setts: an admin sets up the log chat from inside the group ----------

router = Router()


@router.message(Command("setts"), F.chat.type.in_({"group", "supergroup"}), F.from_user.id.in_(config.admin_ids))
async def cmd_setts(m: Message, bot: Bot, s: AsyncSession):
    from bot.services import settings
    chat = m.chat.id
    _forum.pop(chat, None)
    for key in TOPICS:
        _topics.pop((chat, key), None)
    await settings.put(s, "log_chat", str(chat))
    await s.commit()
    reply = lambda text: m.answer(text, disable_web_page_preview=True)  # noqa: E731 - into the thread it was typed in
    if not await is_forum(bot, chat):
        return await reply("✅ <b>Лог-чат Strait Pay — здесь.</b>\nВсе логи будут приходить в эту группу.\n\n"
                           "Чтобы разложить их по веткам: Настройки группы → <b>Темы</b> → включить; дайте боту права "
                           "администратора с правом <b>«Управление темами»</b> и снова отправьте /setts.")
    made, failed = [], []
    for key, (name, _) in TOPICS.items():
        existed = await s.get(Setting, f"topic:{chat}:{key}") is not None
        tid = await thread(bot, s, chat, key)
        if tid is None:
            failed.append(name)
            continue
        made.append(("· " if existed else "＋ ") + name)
        if not existed:
            with suppress(TelegramAPIError):
                await bot.send_message(chat, f"<b>{name}</b>\n{ABOUT[key]}", message_thread_id=tid,
                                       disable_notification=True)
    await s.commit()
    if failed:
        return await reply("⚠️ <b>Не получилось создать ветки</b>: " + ", ".join(failed) + ".\nДайте боту права "
                           "администратора с правом <b>«Управление темами»</b> и снова отправьте /setts.")
    await reply("✅ <b>Лог-чат Strait Pay настроен.</b>\nВетки (＋ новая, · уже была):\n" + "\n".join(made)
                + "\n\nКарточки операций обновляются на месте; всё, что требует решения, дублируется в "
                  "«⚠️ Требует внимания».")
