"""Admin log chat: forum topics per kind of log and one live card per operation.

If the log chat is a forum supergroup, the bot creates its topics itself (and re-creates a topic an admin deleted):
deals, order requisites, deposits, withdrawals, API, order merchants, users, cards, adjustments, tickets, chat,
service, and "needs attention". Every operation (deal #15, withdrawal #7, API application #2 …) has one card in its
topic: status badge, one fact per line (people as @username · name · id) and the latest events. New events edit the card in place (silently); events that
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
                        Setting, Ticket, User, Withdrawal, now)
from bot.services import events, money, xrocket
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
    "api": ("🔑 API: заявки и клиенты", 13338331),
    "om": ("🤝 Ордерные мерчанты", 9367192),
    "users": ("👤 Пользователи", 16749490),
    "cards": ("💳 Карты", 16766590),
    "adjust": ("✏️ Корректировки", 16749490),
    "tickets": ("💬 Обращения", 7322096),
    "chat": ("👥 Чат и рассылки", 7322096),
    "service": ("⚙️ Сервис", 16478047),
}
KIND_TOPIC = {"wd": "withdrawals", "dep": "deposits", "apa": "api", "apc": "api", "om": "om", "user": "users",
              "card": "cards", "adj": "adjust", "ticket": "tickets", "app": "service"}
ABOUT = {
    "attention": "Всё, что ждёт решения администратора: споры, отказы xRocket, выводы на проверке, новые анкеты и "
                 "заявки, нехватка газа. Эту ветку стоит держать со звуком.",
    "stats": "Одно живое сообщение: сколько денег на xRocket, сколько должны пользователям, прибыль и сколько "
             "можно забрать. Обновляется каждые 10 минут.",
    "deals": "Сделки по статичным картам: одна карточка на сделку, статус меняется по ходу.",
    "orders": "Сделки по ордерным реквизитам: поиск мерчанта, выдача реквизитов, оплата, итог.",
    "deposits": "Пополнения через xRocket: счета и адреса в сетях (сумма, комиссия, зачислено).",
    "withdrawals": "Выводы через xRocket: чеки и переводы на кошельки в сетях — от запроса до выполнения.",
    "api": "Заявки на Strait Pay API и API-клиенты: одобрение, токены, приостановка.",
    "om": "Анкеты ордерных мерчантов и их статус.",
    "users": "Новые пользователи, баны, личные ставки мерчантов.",
    "cards": "Карты продавцов: добавление, блокировка.",
    "adjust": "Ручные корректировки баланса.",
    "tickets": "Обращения пользователей в поддержку.",
    "chat": "Чат сообщества: вступления по личным ссылкам, закреп, итоги рассылок.",
    "service": "Сервисные сообщения: xRocket и настройки.",
}
IMPORTANT = ("failed", "unknown", "refunded", "dispute", "late_no_funds")
TIMELINE = 6
GOOD, WAIT, BAD, NEUTRAL, ATTENTION = "🟢", "🟡", "🔴", "⚪️", "🟠"

_forum: dict[int, bool] = {}  # chat id -> is a forum (checked once per process)
_topics: dict[tuple[int, str], int | None] = {}  # (chat, key) -> thread id


def important(ev: Event) -> bool:
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


async def who(s: AsyncSession, uid: int | None) -> str:
    """A person in the log: @username · name · id — readable and searchable at once."""
    if uid is None:
        return "—"
    u = await s.get(User, uid)
    parts = [f"@{esc(u.username)}" if u and u.username else "", esc(u.name) if u and u.name else "", f"<code>{uid}</code>"]
    return " · ".join(p for p in parts if p)


def _mask(card: Card) -> str:
    r = card.requisites
    return f"{esc(card.bank)} •• {esc(r[-4:])}" + (" · СБП" if card.kind == "sbp" else "")


async def describe(s: AsyncSession, ref: str) -> tuple[str, str, str, list[str]]:
    """(topic key, status code, status label, facts — one per line) of an operation, read from its current state."""
    from bot.handlers.admin import ADJ_REASONS, ADJ_STATUS, DEP_LABEL, WD_LABEL
    from bot.handlers.admin_api import APP_STATUS
    from bot.handlers.admin_ops import TICKET_STATUS
    from bot.handlers.admin_orders import STATUS as OM_STATUS
    from bot.handlers.deal import STATUS as DEAL_STATUS
    kind, _, oid = ref.partition(":")
    topic = KIND_TOPIC.get(kind, "service")
    if kind == "app":
        return ("chat" if oid in ("chat", "broadcast") else "service"), "attention", "сервис", []
    model = {"deal": Deal, "wd": Withdrawal, "dep": Deposit, "apa": ApiApplication, "apc": ApiClient,
             "om": OrderMerchant, "ticket": Ticket, "adj": Adjustment, "user": User, "card": Card}.get(kind)
    obj = await s.get(model, int(oid)) if model and oid.isdigit() else None
    if obj is None:
        return topic, "unknown", "", []
    uids = {getattr(obj, a, None) for a in ("buyer_id", "seller_id", "operator_id", "user_id", "admin_id")}
    names = {uid: await who(s, uid) for uid in uids if uid}
    u = lambda label, uid: f"{label}: {names.get(uid, '—')}"  # noqa: E731
    if kind == "deal":
        mode = "Bybit-ордер" if obj.via_bybit else "ордерные реквизиты" if obj.is_order else "статичная карта"
        client = await s.get(ApiClient, obj.api_client_id) if obj.api_client_id else None
        card = await s.get(Card, obj.card_id) if obj.card_id else None
        facts = [f"Тип: {mode}" + (f" · API «{esc(client.project)}»" if client else ""),
                 f"Сумма: <b>{money.fmt(obj.amount_rub)} ₽</b>",
                 f"Покупатель получит: <b>{money.usdt(obj.buyer_credit)} USDT</b> · курс "
                 f"{money.fmt(obj.buyer_rate or obj.rate)} ₽ · {money.fmt(obj.platform_pct, 3)}%",
                 f"Мерчант отдаёт: {money.usdt(obj.seller_debit)} USDT" + (
                     f" по {money.fmt(obj.merchant_rate)} ₽" if obj.merchant_rate else f" · {money.fmt(obj.seller_pct, 3)}%"),
                 f"Доход площадки: {money.usdt(obj.platform_fee)} USDT",
                 u("Покупатель", obj.buyer_id)]
        if obj.seller_id:
            facts.append(u("Мерчант", obj.seller_id))
        if obj.via_bybit and obj.operator_id:
            facts.append(u("Оператор", obj.operator_id))
        if card:
            facts.append(f"Реквизиты: {_mask(card)}")
        if obj.sender_bank:
            facts.append(f"Банк покупателя: {esc(obj.sender_bank)}")
        return ("orders" if obj.is_order else "deals"), obj.status, DEAL_STATUS[obj.status][1], facts
    if kind == "wd":
        where = (f"{xrocket.net_name(obj.network)} · <code>{esc(obj.address or '—')}</code>" if obj.method == "chain"
                 else "чек xRocket")
        return topic, obj.status, WD_LABEL.get(obj.status, obj.status), [
            u("Пользователь", obj.user_id), f"Способ: {where}",
            f"Списано: <b>{money.usdt(obj.amount)} USDT</b> · комиссия {money.usdt(obj.fee)}",
            f"К получению: <b>{money.usdt(obj.amount - obj.fee)} USDT</b>"]
    if kind == "dep":
        how = f"адрес {xrocket.net_name(obj.network)}" if obj.address else "счёт xRocket"
        return topic, obj.status, DEP_LABEL.get(obj.status, obj.status), [
            u("Пользователь", obj.user_id), f"Способ: {how}",
            f"Сумма: <b>{money.usdt(obj.amount)} USDT</b>" + (
                f" · зачислено {money.usdt(obj.credit)}" if obj.status == "paid" else "")]
    if kind == "apa":
        return topic, obj.status, APP_STATUS[obj.status], [
            u("Заявитель", obj.user_id), f"Проект: <b>{esc(obj.project)}</b>", f"Ссылка: {esc(obj.url)}",
            f"Трафик: {esc(obj.traffic)}", f"Оборот: {esc(obj.volume)}"]
    if kind == "apc":
        return topic, obj.status, "активен" if obj.status == "active" else "приостановлен", [
            u("Владелец", obj.user_id), f"Проект: <b>{esc(obj.project)}</b>",
            f"Условия: курс {money.fmt(obj.rate) + ' ₽' if obj.rate else 'общий'} · "
            f"{money.fmt(obj.pct, 3) + '%' if obj.pct is not None else 'общий %'}"]
    if kind == "om":
        return topic, obj.status, OM_STATUS[obj.status], [
            u("Мерчант", obj.user_id), f"Режим: {'Bybit-ордер' if obj.mode == 'bybit' else 'баланс'}",
            f"Заявки: {money.fmt(obj.min_rub)}–{money.fmt(obj.max_rub)} ₽ · в работе до {money.fmt(obj.max_open_rub)} ₽",
            f"Банки: {esc(obj.banks)}"]
    if kind == "ticket":
        return topic, obj.status, TICKET_STATUS[obj.status], [
            u("Пользователь", obj.user_id), f"Текст: {esc(obj.text[:300])}"]
    if kind == "adj":
        return topic, obj.status, ADJ_STATUS[obj.status], [
            u("Пользователь", obj.user_id), f"Изменение: <b>{'+' if obj.delta > 0 else ''}{money.usdt(obj.delta)} USDT</b>",
            f"Причина: {esc(ADJ_REASONS.get(obj.reason, obj.reason))}" + (f" — {esc(obj.comment)}" if obj.comment else ""),
            u("Администратор", obj.admin_id)]
    if kind == "user":
        code = "banned" if obj.is_banned else "active"
        return topic, code, "заблокирован" if obj.is_banned else "активен", [
            f"Пользователь: {await who(s, obj.id)}",
            f"Баланс: {money.usdt(obj.balance)} USDT · в сделках {money.usdt(obj.frozen)}"]
    if kind == "card":
        code = "deleted" if obj.is_deleted else "banned" if obj.is_banned else "active" if obj.is_active else "off"
        label = {"deleted": "удалена", "banned": "заблокирована", "active": "включена", "off": "выключена"}[code]
        return topic, code, label, [u("Владелец", obj.user_id), f"Карта: {_mask(obj)}",
                                    f"Суммы: {money.fmt(obj.min_rub)}–{money.fmt(obj.max_rub)} ₽"]
    return topic, "", "", []


APP_NAMES = {"chat": ("Чат сообщества", "ach"), "broadcast": ("Рассылка", "ach"), "xrocket": ("xRocket", "al")}


async def render(s: AsyncSession, ref: str, attention: bool) -> tuple[str, str, object]:
    """(topic key, card text, markup)."""
    from bot.handlers.admin_ops import REF_NAMES
    kind, _, oid = ref.partition(":")
    topic, code, label, facts = await describe(s, ref)
    badge = ATTENTION if attention and code not in ("completed", "done") else _badge(code)
    if kind == "app":
        name, cb = APP_NAMES.get(oid, (oid, "a"))
        head = f"{badge} <b>{esc(name)}</b>"
        markup = kb(btn("Открыть", cb, "search"))
    else:
        name, cb = REF_NAMES.get(kind, ("Событие", ""))
        head = f"{badge} <b>{name} #{oid}</b>" + (f" · {label}" if label else "")
        markup = kb(btn("Открыть", f"{cb}:{oid}", "search")) if cb else None
    if attention:
        head += " · <b>нужно внимание</b>"
    rows = (await s.scalars(select(Event).where(Event.ref == ref).order_by(Event.id.desc()).limit(TIMELINE))).all()
    timeline = [f"{at(e.created_at)} · {esc(e.text[:300])}" for e in reversed(rows)]
    text = "\n".join([head, "", *facts, *(["", "<b>История</b>", *timeline] if timeline else [])])
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
        head, _, rest = text.partition("\n\n")
        facts = rest.split("\n\n", 1)[0].split("\n")[:4]  # who and how much: enough to decide whether to open it
        what = "\n".join(f"• {esc(ev.text[:300])}" for ev in evs if important(ev))
        await post(bot, s, chat, "attention", clean("\n".join([head, *facts, "", what])),
                   markup or kb(back("x", "Скрыть", "cross")), silent=False)


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
