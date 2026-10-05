"""Admin chat: forum topics per kind of log, one live card per operation, admin actions, bot errors.

If the admin chat is a forum supergroup, the bot creates its topics itself (and re-creates a topic an admin deleted)
and pins a help message in each one: what comes there, which buttons and commands work. Every operation (deal #15,
withdrawal #7, API application #2 …) has one card in its topic: status, the facts in the card style (a label, the
value on a branch), people and objects as links that open their admin card in the bot, the latest events folded
into a quote. New events edit the card in place (silently); events that need an admin also go to the «Требует
внимания» topic, which rings. Buttons under a card act right there: «Подробнее» turns the card into the full admin
screen in place (nothing is sent to the private chat), decisions (approve / reject) are taken on the card itself.

Separate topics: «Действия админов» — one post per admin action (who, what, on whom); «Ошибки бота» — every error of
the bot with where it happened, repeats folded into one post. Without topics (a plain group or the owners' private
chats) a card is edited for routine steps and re-posted at the bottom when something needs attention.
Needs the bot to be an admin with «Управление темами», «Удаление» and «Закрепление сообщений».
"""
import asyncio
import hashlib
import logging
import traceback
from collections import OrderedDict
from contextlib import suppress
from contextvars import ContextVar
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Chat, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import ui
from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.models import (Adjustment, ApiApplication, ApiClient, Card, Deal, Deposit, Event, LogMessage, Operator,
                        OrderMerchant, Session, Setting, Team, Ticket, User, Withdrawal, now)
from bot.services import events, money, xrocket
from bot.services.admins import IsAdmin
from bot.ui import MSK, alink, at, card, cf, clean, esc, mark, paced, section, stamp, ulink

log = logging.getLogger(__name__)

# key -> (topic title, icon colour; Telegram allows only these six). Keys are stable (stored topic ids depend on
# them), titles may change. Keys are at most 11 characters: Setting keys are "tpin:<chat>:<key>".
TOPICS = {
    "attention": ("⚠️ Требует внимания", 16478047),
    "control": ("🎛 Управление", 7322096),
    "signups": ("📝 Заявки на вход", 16749490),
    "stats": ("📊 Статистика и финансы", 9367192),
    "deals": ("💱 Сделки", 7322096),
    "orders": ("🧾 Ордерные реквизиты", 13338331),
    "bybit": ("🟣 Bybit-ордера", 13338331),
    "operators": ("🧑‍💻 Операторы и долги", 16766590),
    "teams": ("🫂 Команды и тимлиды", 9367192),
    "deposits": ("📥 Пополнения", 9367192),
    "withdrawals": ("📤 Выводы", 16766590),
    "api": ("🔑 API: заявки и клиенты", 13338331),
    "om": ("🤝 Ордерные мерчанты", 9367192),
    "users": ("👤 Пользователи", 16749490),
    "cards": ("💳 Карты", 16766590),
    "adjust": ("✏️ Корректировки", 16749490),
    "tickets": ("💬 Обращения", 7322096),
    "chat": ("👥 Чат и рассылки", 7322096),
    "admins": ("🛡 Действия админов", 16766590),
    "errors": ("🛑 Ошибки бота", 16478047),
    "service": ("⚙️ Сервис", 16478047),
}
KIND_TOPIC = {"wd": "withdrawals", "dep": "deposits", "apa": "api", "apc": "api", "om": "om", "user": "users",
              "card": "cards", "adj": "adjust", "ticket": "tickets", "app": "service", "op": "operators",
              "team": "teams"}
# what each topic is for: (what comes by itself, what the buttons do); the commands are admin_cmds.HELP
ABOUT = {
    "attention": ("Всё, что ждёт решения: споры, отказы xRocket, выводы на проверке, новые заявки и анкеты, "
                  "нехватка средств. Держите эту тему со звуком.",
                  "«Подробнее» — полная карточка прямо здесь; решения — кнопками на карточке."),
    "control": ("Отсюда удобно работать с админ-панелью: /admin открывает её прямо в чате, все переходы — правкой "
                "того же сообщения. В личку ничего не уходит.",
                "Кнопки панели работают на месте. Имена и номера — ссылки: откроют карточку в личке с ботом."),
    "signups": ("Заявки новых пользователей: кто (P2P-продавец или покупатель), оборот в день, скриншот.",
                "«Одобрить» / «Отклонить» — решение прямо на карточке, под ней появится, кто решил."),
    "stats": ("Одно живое сообщение: сколько денег на xRocket, сколько должны пользователям, прибыль и сколько "
              "можно забрать. Обновляется каждые 10 минут.", ""),
    "deals": ("Сделки по статичным картам: одна карточка на сделку, статус и история меняются по ходу.",
              "«Подробнее» — вся сделка и решения (завершить, отменить, изменить сумму) прямо здесь."),
    "orders": ("Ордерные заявки с баланса мерчанта: поиск, выдача реквизитов, оплата, итог.",
               "«Подробнее» — заявка целиком, можно выдать реквизиты самому."),
    "bybit": ("Ордерные заявки через Bybit-ордер: ссылка мерчанта, кто из операторов принял, реквизиты, оплата.",
              "«Подробнее» — заявка целиком."),
    "operators": ("Каждое действие оператора отдельным постом: принял ордер, выдал реквизиты, вернул, «мерчант не "
                  "дал реквизиты», время вышло, подтвердил оплату. И карточки операторов: долг и его погашение.",
                  "«Подробнее» — карточка оператора: снять, вернуть, списать долг."),
    "teams": ("Команды: заявки тимлидов, участники по реферальным ссылкам, подключение чатов, процент тимлида.",
              "Заявку — «Одобрить» / «Отклонить» на карточке; «Подробнее» — участники, ссылка в чат команды."),
    "deposits": ("Пополнения через xRocket: счета и адреса в сетях — сумма, комиссия, зачислено.",
                 "«Подробнее» — проверить счёт в xRocket."),
    "withdrawals": ("Выводы через xRocket: чеки и переводы на кошельки — от запроса до выполнения.",
                    "«Подробнее» — сверка с xRocket и возврат, если чек не создан."),
    "api": ("Заявки на Strait Pay API и API-клиенты: одобрение, токены, приостановка.",
            "Заявку — «Одобрить» / «Отклонить» на карточке."),
    "om": ("Анкеты ордерных мерчантов и их статус.", "Анкету — «Одобрить» / «Отклонить» на карточке."),
    "users": ("Новые пользователи, баны, личные ставки, назначение админов.",
              "«Подробнее» — карточка пользователя: баланс, сделки, бан, права."),
    "cards": ("Карты продавцов: добавление, блокировка.", "«Подробнее» — выключить или заблокировать карту."),
    "adjust": ("Ручные корректировки баланса; крупные ждут второго администратора.",
               "«Подтвердить» / «Отклонить» — для второго администратора."),
    "tickets": ("Обращения пользователей в поддержку.", "«Подробнее» — ответить или закрыть."),
    "chat": ("Чат сообщества: вступления по личным ссылкам, закреп, итоги рассылок.", ""),
    "admins": ("Каждое действие администратора отдельным постом: кто, что сделал и с кем — баны, балансы, "
               "решения споров, настройки, назначение админов.", "Имена — ссылки на карточки в боте."),
    "errors": ("Ошибки бота: что сломалось, где и когда. Одна и та же ошибка в течение 30 минут — один пост со "
               "счётчиком повторов.", ""),
    "service": ("Сервисные сообщения: xRocket и настройки.", ""),
}
IMPORTANT = ("failed", "unknown", "refunded", "dispute", "late_no_funds")
TIMELINE = 6
GOOD, WAIT, BAD, NEUTRAL, ATTENTION = "🟢", "🟡", "🔴", "⚪️", "🟠"

_forum: dict[int, bool] = {}  # chat id -> is a forum (checked once per process)
_topics: dict[tuple[int, str], int | None] = {}  # (chat, key) -> thread id


def important(ev: Event) -> bool:
    return not ev.notice or ev.kind in IMPORTANT


def targets() -> list[int]:
    """Where logs go: the group where an admin sent /setts, else LOG_CHAT_ID, else the owners' private chats."""
    from bot.services import settings
    chosen = settings.raw("log_chat")
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
    """Thread id of the topic, created on first use (with its pinned help). None: post to the general chat."""
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
    # stored at once: losing it would mean a second set of topics on top of the first
    await s.merge(Setting(key=f"topic:{chat}:{key}", value=str(topic.message_thread_id)))
    await s.flush()
    await pin_help(bot, s, chat, key, topic.message_thread_id)
    return topic.message_thread_id


async def forget_topic(s: AsyncSession, chat: int, key: str) -> None:
    _topics.pop((chat, key), None)
    for k in (f"topic:{chat}:{key}", f"tpin:{chat}:{key}"):
        if row := await s.get(Setting, k):
            await s.delete(row)
    await s.flush()  # the next lookup in this session must not find it


def _thread_gone(e: Exception) -> bool:
    text = str(e).lower()
    return isinstance(e, TelegramBadRequest) and ("thread" in text or "topic" in text)


async def post(bot: Bot, s: AsyncSession, chat: int, key: str, text: str, markup, silent: bool,
               media: str | None = None):
    """Send into a topic; if an admin deleted the topic, create it again once. media: a file to send with `text` as
    its caption — "photo:<file_id>" or a document's file_id."""
    for attempt in range(2):
        tid = await thread(bot, s, chat, key)
        try:
            if media:
                send = bot.send_photo if media.startswith("photo:") else bot.send_document
                return await paced(lambda: send(chat, media.removeprefix("photo:"), caption=text, reply_markup=markup,
                                                message_thread_id=tid, disable_notification=silent))
            return await paced(lambda: bot.send_message(chat, text, reply_markup=markup, message_thread_id=tid,
                                                        disable_notification=silent, disable_web_page_preview=True))
        except TelegramBadRequest as e:
            if attempt or tid is None or not _thread_gone(e):
                raise
            await forget_topic(s, chat, key)


def help_text(key: str) -> str:
    """The pinned help of a topic, always in one order; empty blocks are skipped."""
    from bot.handlers.admin_cmds import HELP
    about, buttons = ABOUT[key]
    lines = [f"{mark('📌')} <b>{TOPICS[key][0]}</b>", "", section("bell", "Что приходит само"),
             f"<blockquote>{about}</blockquote>"]
    if buttons:
        lines += [section("list", "Кнопки"), f"<blockquote>{buttons}</blockquote>"]
    if key in ("control", "attention"):
        lines += [section("pencil", "Команды"),
                  *[f"<blockquote><b>{esc(c)}</b>\n{d}</blockquote>" for c, d in HELP]]
    lines.append("<blockquote><i>Кнопки и команды здесь работают только для администраторов.</i></blockquote>")
    return clean("\n".join(lines))


async def pin_help(bot: Bot, s: AsyncSession, chat: int, key: str, tid: int | None) -> str:
    """Post (and pin silently) or edit the topic's help; an unchanged text is not sent again.
    Returns "new" / "updated" / "same"."""
    text = help_text(key)
    digest = hashlib.sha1(text.encode()).hexdigest()[:10]
    row = await s.get(Setting, f"tpin:{chat}:{key}")
    if row:
        mid, _, old = row.value.partition(":")
        if old == digest:
            return "same"
        try:
            await paced(lambda: bot.edit_message_text(text=text, chat_id=chat, message_id=int(mid),
                                                      disable_web_page_preview=True))
            row.value = f"{mid}:{digest}"
            return "updated"
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                row.value = f"{mid}:{digest}"
                return "same"
            # deleted by an admin: a new one
    try:
        m = await paced(lambda: bot.send_message(chat, text, message_thread_id=tid, disable_notification=True,
                                                 disable_web_page_preview=True))
    except TelegramAPIError as e:
        log.warning("help of topic %s not posted in %s: %s", key, chat, e)
        return "same"
    await s.merge(Setting(key=f"tpin:{chat}:{key}", value=f"{m.message_id}:{digest}"))
    with suppress(TelegramAPIError):
        await bot.pin_chat_message(chat, m.message_id, disable_notification=True)
    return "new"


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
    """A person in the log: @username · name linking to his admin card, the id to copy."""
    if uid is None:
        return "—"
    return ulink(await s.get(User, uid), uid)


def _mask(c: Card) -> str:
    r = c.requisites
    return f"{esc(c.bank)} •• {esc(r[-4:])}" + (" · СБП" if c.kind == "sbp" else "")


async def _decided(s: AsyncSession, obj) -> str:
    """Who decided an application and when: the verdict is kept in the data, not only in a message."""
    if not getattr(obj, "admin_id", None) or not getattr(obj, "decided_at", None):
        return ""
    return f"{await who(s, obj.admin_id)} · {at(obj.decided_at, 'dt')}"


async def describe(s: AsyncSession, ref: str) -> tuple[str, str, str, list[str]]:
    """(topic key, status code, status label, card fields) of an operation, read from its current state."""
    from bot.handlers.admin import ADJ_REASONS, ADJ_STATUS, DEP_LABEL, WD_LABEL
    from bot.handlers.admin_api import APP_STATUS
    from bot.handlers.admin_ops import TICKET_STATUS
    from bot.handlers.admin_orders import STATUS as OM_STATUS
    from bot.handlers.deal import REASONS, STATUS as DEAL_STATUS
    from bot.services import admins, teams
    kind, _, oid = ref.partition(":")
    topic = KIND_TOPIC.get(kind, "service")
    if kind == "app":
        return ("chat" if oid in ("chat", "broadcast") else "service"), "attention", "сервис", []
    model = {"deal": Deal, "wd": Withdrawal, "dep": Deposit, "apa": ApiApplication, "apc": ApiClient,
             "om": OrderMerchant, "ticket": Ticket, "adj": Adjustment, "user": User, "card": Card, "op": Operator,
             "team": Team}.get(kind)
    obj = await s.get(model, int(oid)) if model and oid.isdigit() else None
    if obj is None:
        return topic, "unknown", "", []
    if kind == "deal":
        mode = "Bybit-ордер" if obj.via_bybit else "ордерные реквизиты" if obj.is_order else "статичная карта"
        client = await s.get(ApiClient, obj.api_client_id) if obj.api_client_id else None
        c = await s.get(Card, obj.card_id) if obj.card_id else None
        fields = [
            cf("Тип", mode + (f" · API «{esc(client.project)}»" if client else ""), icon="info"),
            cf("Сумма", f"<b>{money.fmt(obj.amount_rub)} ₽</b> → покупателю <b>{money.usdt(obj.buyer_credit)} USDT</b>",
               f"курс {money.fmt(obj.buyer_rate or obj.rate)} ₽ · комиссия {money.fmt(obj.platform_pct, 3)}%",
               f"мерчант отдаёт {money.usdt(obj.seller_debit)} USDT" + (
                   f" по {money.fmt(obj.merchant_rate)} ₽" if obj.merchant_rate else f" · {money.fmt(obj.seller_pct, 3)}%"),
               f"доход площадки {money.usdt(obj.platform_fee)} USDT" + (
                   f" · тимлиду {money.usdt(obj.team_fee)}" if obj.team_fee else ""), icon="ruble"),
            cf("Покупатель", await who(s, obj.buyer_id), icon="profile"),
            cf("Мерчант", await who(s, obj.seller_id) if obj.seller_id else "ещё никто", icon="shop"),
            cf("Оператор", await who(s, obj.operator_id) if obj.operator_id else "не назначен", icon="key")
            if obj.via_bybit else "",
            cf("Реквизиты", _mask(c) if c else "", f"банк покупателя: {esc(obj.sender_bank)}" if obj.sender_bank else "",
               f'<a href="{esc(obj.bybit_url)}">ордер Bybit</a>' if obj.bybit_url else "", icon="card"),
            cf("Спор", REASONS.get(obj.dispute_reason, obj.dispute_reason or "")
               + (f" · пришло {money.fmt(obj.dispute_amount_rub)} ₽" if obj.dispute_amount_rub else ""), icon="flag")
            if obj.dispute_reason else "",
        ]
        topic = "bybit" if obj.via_bybit else "orders" if obj.is_order else "deals"
        return topic, obj.status, DEAL_STATUS[obj.status][1], fields
    if kind == "wd":
        where = (f"{xrocket.net_name(obj.network)} · <code>{esc(obj.address or '—')}</code>" if obj.method == "chain"
                 else "чек xRocket")
        return topic, obj.status, WD_LABEL.get(obj.status, obj.status), [
            cf("Пользователь", await who(s, obj.user_id), icon="profile"),
            cf("Способ", where, icon="wallet"),
            cf("Сумма", f"списано <b>{money.usdt(obj.amount)} USDT</b>",
               f"к получению <b>{money.usdt(obj.amount - obj.fee)} USDT</b> · комиссия {money.usdt(obj.fee)}",
               icon="dollar"),
            cf("Ответ xRocket", f"<code>{esc(obj.error[:200])}</code>", icon="info") if obj.error else ""]
    if kind == "dep" and obj.purpose == "debt":
        return "operators", obj.status, DEP_LABEL.get(obj.status, obj.status), [
            cf("Оператор", await who(s, obj.user_id), icon="profile"),
            cf("Назначение", "погашение долга за Bybit-ордера", icon="info"),
            cf("Счёт", f"<b>{money.usdt(obj.amount)} USDT</b>" + (" · оплачен" if obj.status == "paid" else ""),
               icon="dollar")]
    if kind == "dep":
        how = f"адрес {xrocket.net_name(obj.network)}" if obj.address else "счёт xRocket"
        return topic, obj.status, DEP_LABEL.get(obj.status, obj.status), [
            cf("Пользователь", await who(s, obj.user_id), icon="profile"), cf("Способ", how, icon="wallet"),
            cf("Сумма", f"<b>{money.usdt(obj.amount)} USDT</b>" + (
                f" · зачислено {money.usdt(obj.credit)}" if obj.status == "paid" else ""), icon="dollar")]
    if kind == "apa":
        return topic, obj.status, APP_STATUS[obj.status], [
            cf("Заявитель", await who(s, obj.user_id), icon="profile"),
            cf("Проект", f"<b>{esc(obj.project)}</b> · {esc(obj.url)}", icon="shop"),
            cf("Трафик и оборот", esc(obj.traffic), esc(obj.volume), icon="stats"),
            cf("Решение", await _decided(s, obj), icon="ok")]
    if kind == "apc":
        return topic, obj.status, "активен" if obj.status == "active" else "приостановлен", [
            cf("Владелец", await who(s, obj.user_id), icon="profile"),
            cf("Проект", f"<b>{esc(obj.project)}</b>", icon="shop"),
            cf("Условия", f"курс {money.fmt(obj.rate) + ' ₽' if obj.rate else 'общий'} · "
               f"{money.fmt(obj.pct, 3) + '%' if obj.pct is not None else 'общий %'}", icon="percent")]
    if kind == "om":
        return topic, obj.status, OM_STATUS[obj.status], [
            cf("Мерчант", await who(s, obj.user_id), icon="profile"),
            cf("Анкета", f"источник: {esc(obj.source)}", f"скорость: {esc(obj.speed)}", f"банки: {esc(obj.banks)}",
               icon="list"),
            cf("Решение", await _decided(s, obj), icon="ok")]
    if kind == "ticket":
        return topic, obj.status, TICKET_STATUS[obj.status], [
            cf("Пользователь", await who(s, obj.user_id), icon="profile"),
            cf("Сделка", alink("deal", obj.deal_id, f"#{obj.deal_id}") if obj.deal_id else "", icon="fire"),
            cf("Текст", esc(obj.text[:300]), icon="support")]
    if kind == "adj":
        return topic, obj.status, ADJ_STATUS[obj.status], [
            cf("Пользователь", await who(s, obj.user_id), icon="profile"),
            cf("Изменение", f"<b>{'+' if obj.delta > 0 else ''}{money.usdt(obj.delta)} USDT</b>", icon="dollar"),
            cf("Причина", esc(ADJ_REASONS.get(obj.reason, obj.reason)) + (f" — {esc(obj.comment)}" if obj.comment else ""),
               icon="info"),
            cf("Администратор", await who(s, obj.admin_id)
               + (f" · подтвердил {await who(s, obj.approved_by)}" if obj.approved_by else ""), icon="lock")]
    if kind == "user":
        code = "banned" if obj.is_banned else "active"
        return topic, code, "заблокирован" if obj.is_banned else "активен", [
            cf("Пользователь", await who(s, obj.id), icon="profile"),
            cf("Баланс", f"{money.usdt(obj.balance)} USDT · в сделках {money.usdt(obj.frozen)}", icon="wallet"),
            cf("Права", admins.role(obj.id), icon="lock") if admins.is_admin(obj.id) else ""]
    if kind == "op":
        code = "active" if obj.active else "off"
        return topic, code, "активен" if obj.active else "не оператор", [
            cf("Оператор", await who(s, obj.user_id), icon="profile"),
            cf("Долг", f"<b>{money.usdt(obj.debt)} USDT</b>", icon="dollar")]
    if kind == "team":
        return topic, obj.status, teams.STATUS[obj.status], [
            cf("Команда", f"<b>{esc(obj.name)}</b>", icon="people"),
            cf("Тимлид", await who(s, obj.leader_id), icon="profile"),
            cf("Состав", f"участников {await teams.members(s, obj)} · процент тимлида {money.fmt(teams.pct(obj), 3)}%",
               icon="stats"),
            cf("Чат", f"<code>{obj.chat_id}</code>" if obj.chat_id else "не подключён", icon="support"),
            cf("О себе", esc(obj.about[:300]), icon="info") if obj.status == "pending" and obj.about else "",
            cf("Решение", await _decided(s, obj), icon="ok")]
    if kind == "card":
        code = "deleted" if obj.is_deleted else "banned" if obj.is_banned else "active" if obj.is_active else "off"
        label = {"deleted": "удалена", "banned": "заблокирована", "active": "включена", "off": "выключена"}[code]
        return topic, code, label, [cf("Владелец", await who(s, obj.user_id), icon="profile"),
                                    cf("Карта", _mask(obj), f"{money.fmt(obj.min_rub)}–{money.fmt(obj.max_rub)} ₽",
                                       icon="card")]
    return topic, "", "", []


APP_NAMES = {"chat": ("Чат сообщества", "ach"), "broadcast": ("Рассылка", "ach"), "xrocket": ("xRocket", "al")}
# pending applications are decided right on their card: kind -> (status, approve callback, reject callback)
DECIDE = {"team": ("pending", "atm:ok", "atm:no"), "om": ("pending", "aom:ok", "aom:no"),
          "apa": ("pending", "aap:ok", "aap:no"), "adj": ("pending", "adj:ok", "adj:no")}


def short_time(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.astimezone(MSK):%d.%m %H:%M}"


def _fit(text: str, limit: int = 3900) -> str:
    """A card always fits one message: the oldest history lines go first."""
    while len(text) > limit:
        head, sep, rest = text.partition("<b>История</b>\n")
        body, _, tail = rest.partition("</blockquote>")
        if not sep or "\n" not in body:
            return text[:limit]
        text = head + sep + body.split("\n", 1)[1] + "</blockquote>" + tail
    return text


async def render(s: AsyncSession, ref: str, attention: bool) -> tuple[str, str, object]:
    """(topic key, card text, markup)."""
    from bot.handlers.admin_ops import REF_NAMES
    kind, _, oid = ref.partition(":")
    topic, code, label, fields = await describe(s, ref)
    badge = ATTENTION if attention and code not in ("completed", "done") else _badge(code)
    rows = []
    if kind == "app":
        name, cb = APP_NAMES.get(oid, (oid, "a"))
        head = f"{mark(badge)} <b>{esc(name)}</b>"
        rows.append(btn("Открыть", cb, "search"))
    else:
        name, cb = REF_NAMES.get(kind, ("Событие", ""))
        number = alink(kind, oid, f"#{oid}") if oid.isdigit() else f"#{esc(oid)}"
        head = f"{mark(badge)} <b>{name} {number}</b>" + (f" · {label}" if label else "")
        if kind in DECIDE and code == DECIDE[kind][0]:
            ok_cb, no_cb = DECIDE[kind][1:]
            rows.append([btn("Подтвердить" if kind == "adj" else "Одобрить", f"{ok_cb}:{oid}", "ok", style="success"),
                         btn("Отклонить", f"{no_cb}:{oid}", "cross", style="danger")])
        if cb:
            rows.append(btn("Подробнее", f"{cb}:{oid}", "search"))
    if attention:
        head += " · <b>нужно внимание</b>"
    history = (await s.scalars(select(Event).where(Event.ref == ref).order_by(Event.id.desc()).limit(TIMELINE))).all()
    timeline = [f"{short_time(e.created_at)} · {esc(e.text[:300])}" for e in reversed(history)]
    text = "\n".join([head, "", card(*fields)]
                     + (["", "<blockquote expandable><b>История</b>\n" + "\n".join(timeline) + "</blockquote>"]
                        if timeline else [])
                     + ["", stamp()])
    return topic, clean(_fit(text)), kb(*rows) if rows else None


# ---------- delivery ----------

async def deliver(bot: Bot, s: AsyncSession) -> None:
    """Outbox -> log targets. Several events of one operation become one card update; an admin's action is a post
    of its own in «Действия админов». Commits."""
    if not ui.BOT:
        with suppress(TelegramAPIError):
            ui.BOT = (await bot.me()).username or ""
    pending = await events.outbox(s)
    by_ref: OrderedDict[str, list[Event]] = OrderedDict()
    for ev in pending:
        by_ref.setdefault(ev.ref, []).append(ev)
    for ref, evs in by_ref.items():
        if ref.startswith("adm:"):
            await _admin_actions(bot, s, evs)
            continue
        if ref.startswith("opa:"):
            await _operator_actions(bot, s, evs)
            continue
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
    lm = await s.scalar(select(LogMessage).where(LogMessage.chat_id == chat, LogMessage.ref == ref))
    forum = await is_forum(bot, chat)
    if lm is not None and (forum or not attention):
        try:  # routine step (or any step in a forum): the card changes in place, silently
            await paced(lambda: bot.edit_message_text(text=text, chat_id=chat, message_id=lm.msg_id,
                                                      reply_markup=markup, disable_web_page_preview=True))
            lm.updated_at = now()
        except TelegramBadRequest as e:
            if "not modified" not in str(e):  # deleted by an admin: a new card
                lm = await _new_card(bot, s, chat, ref, topic, text, markup, lm)
    else:
        if lm is not None:  # plain chat and it needs an admin: fresh card at the bottom so it rings
            with suppress(TelegramAPIError):
                await bot.edit_message_text(text=text.split("\n", 1)[0] + "\n↓ обновлено ниже", chat_id=chat,
                                            message_id=lm.msg_id)
        lm = await _new_card(bot, s, chat, ref, topic, text, markup, lm)
    if forum and attention:
        head, _, rest = text.partition("\n\n")
        facts = rest.split("<blockquote expandable>", 1)[0].split("\n<i>", 1)[0].strip().split("\n")[:6]
        what = "\n".join(esc(ev.text[:300]) for ev in evs if important(ev))
        await post(bot, s, chat, "attention", clean("\n".join(
            [head, "", *facts, "", section("bell", "Что случилось"), f"<blockquote>{what}</blockquote>"])),
            markup or kb(back("x", "Скрыть", "cross")), silent=False)


async def _new_card(bot, s, chat, ref, topic, text, markup, lm: LogMessage | None) -> LogMessage:
    m = await post(bot, s, chat, topic, text, markup, silent=False)
    if lm is None:
        lm = LogMessage(chat_id=chat, ref=ref, msg_id=m.message_id)
        s.add(lm)
    lm.msg_id, lm.thread_id, lm.updated_at = m.message_id, m.message_thread_id, now()
    await s.flush()
    return lm


# ---------- admin actions: a post each ----------

ACTIONS = {
    "ban": ("ban", "Заблокировал пользователя"), "unban": ("ok", "Разблокировал пользователя"),
    "balance": ("dollar", "Изменил баланс"), "setting": ("settings", "Изменил настройку"),
    "merchant_pct": ("percent", "Личная ставка мерчанта"), "buyer_terms": ("star", "Условия покупателя"),
    "resolve": ("flag", "Решение по сделке"), "deal_take": ("key", "Взял заявку на себя"),
    "deal_amount": ("pencil", "Изменил сумму сделки"), "deal_extend": ("clock", "Продлил срок сделки"),
    "card_off": ("pause", "Выключил карту"), "card_ban": ("ban", "Заблокировал карту"),
    "card_unban": ("ok", "Разблокировал карту"), "offline": ("pause", "Снял со смены"),
    "wd_check": ("refresh", "Проверил вывод в xRocket"), "wd_refund": ("cross", "Вернул средства по выводу"),
    "report": ("doc", "Выгрузил отчёт CSV"), "broadcast": ("bell", "Запустил рассылку"),
    "operator_add": ("plus", "Назначил оператора"), "operator_status": ("shop", "Изменил статус оператора"),
    "operator_writeoff": ("dollar", "Списал долг оператора"), "team_approve": ("ok", "Одобрил команду"),
    "team_reject": ("cross", "Отклонил команду"), "team_status": ("people", "Изменил статус команды"),
    "team_pct": ("percent", "Изменил процент тимлида"), "team_chat_link": ("support", "Взял ссылку в чат команды"),
    "team_chat_off": ("cross", "Отключил чат команды"), "team_remove": ("cross", "Убрал из команды"),
    "admin_grant": ("lock", "Назначил администратора"), "admin_revoke": ("lock", "Снял администратора"),
    "signup_ok": ("ok", "Одобрил заявку на вход"), "signup_no": ("cross", "Отклонил заявку на вход"),
    "om_approve": ("ok", "Одобрил ордерного мерчанта"), "om_reject": ("cross", "Отклонил ордерного мерчанта"),
    "om_status": ("key", "Изменил статус ордерного мерчанта"), "om_wake": ("ok", "Снял паузу мерчанту"),
    "channel_publish": ("bell", "Оформил инфо-канал"), "xrocket_token": ("key", "Сменил токен xRocket"),
    "payout_queue": ("up", "Отправил очередь выводов"), "deposit_unlock": ("up", "Снял ограничение вывода"), "api_approve": ("ok", "Одобрил заявку на API"),
    "api_reject": ("cross", "Отклонил заявку на API"), "api_client": ("key", "Изменил API-клиента"),
    "api_limit": ("key", "Изменил лимит API-клиента"), "api_terms": ("percent", "Изменил условия API-клиента"),
}
TARGET_KINDS = {"user": "Пользователь", "deal": "Сделка", "team": "Команда", "op": "Оператор", "card": "Карта",
                "wd": "Вывод", "dep": "Пополнение", "om": "Ордерный мерчант", "apa": "Заявка на API",
                "apc": "API-клиент", "adj": "Корректировка", "signup": "Заявка на вход"}


async def target_text(s: AsyncSession, target: str) -> str:
    kind, _, oid = target.partition(":")
    if kind in ("user", "op", "om") and oid.isdigit():
        return await who(s, int(oid))
    if kind in TARGET_KINDS and oid.isdigit():
        return alink(kind, oid, f"{TARGET_KINDS[kind]} #{oid}")
    return f"<code>{esc(target)}</code>" if target else ""


async def _admin_actions(bot: Bot, s: AsyncSession, evs: list[Event]) -> None:
    for ev in evs:
        action, target, details = (ev.text.split("\x1f") + ["", ""])[:3]
        icon, label = ACTIONS.get(action, ("settings", action))
        text = clean("\n".join([
            f"{pe(icon)} <b>{label}</b>",
            "",
            card(cf("Кто", await who(s, ev.user_id), icon="lock"),
                 cf("С кем / чем", await target_text(s, target), icon="search"),
                 cf("Детали", f"<code>{esc(details[:500])}</code>" if details else "", icon="info")),
            "",
            stamp(ev.created_at),
        ]))
        delivered = False
        for chat in targets():
            if chat > 0:  # no admin chat: the owners' private chats get the cards, not every admin action
                delivered = True
                continue
            with suppress(TelegramAPIError):
                await post(bot, s, chat, "admins", text, None, silent=True)
                delivered = True
        if delivered:
            ev.sent_at = now()
        else:
            ev.attempts += 1
        await s.commit()


# ---------- operators' actions on Bybit orders: a post each ----------

OPERATOR_ACTIONS = {
    "accepted": ("ok", "Оператор принял ордер"), "requisites": ("key", "Оператор выдал реквизиты"),
    "returned": ("refresh", "Оператор вернул ордер другим"), "rejected_link": ("pencil", "Оператор отклонил ссылку"),
    "no_requisites": ("cross", "Мерчант не дал реквизиты"), "timeout": ("clock", "Время оператора вышло"),
    "completed": ("dollar", "Оплата подтверждена, долг вырос"),
    "new_merchant": ("search", "Оператор ищет другого мерчанта"), "rated": ("star", "Оператор оценил мерчанта"),
}


async def _operator_actions(bot: Bot, s: AsyncSession, evs: list[Event]) -> None:
    for ev in evs:
        did, _, details = ev.text.partition("\x1f")
        d = await s.get(Deal, int(did)) if did.isdigit() else None
        icon, label = OPERATOR_ACTIONS.get(ev.kind, ("shop", ev.kind))
        text = clean("\n".join([
            f"{pe(icon)} <b>{label}</b>",
            "",
            card(cf("Оператор", await who(s, ev.user_id), icon="shop"),
                 cf("Заявка", f"{alink('deal', did, f'#{did}')} · {money.fmt(d.amount_rub)} ₽ · "
                    f"{money.usdt(d.seller_debit)} USDT", icon="fire") if d else "",
                 cf("Мерчант", await who(s, d.seller_id), icon="profile") if d and d.seller_id else "",
                 cf("Детали", esc(details[:400]), icon="info") if details else ""),
            "",
            stamp(ev.created_at),
        ]))
        delivered = False
        for chat in targets():
            if chat > 0:  # no admin chat: the deal cards carry it
                delivered = True
                continue
            with suppress(TelegramAPIError):
                await post(bot, s, chat, "operators", text, None, silent=True)
                delivered = True
        if delivered:
            ev.sent_at = now()
        else:
            ev.attempts += 1
        await s.commit()


# ---------- bot errors: the «Ошибки бота» topic ----------

ERROR_WINDOW = 1800  # the same error within 30 min: one post with a repeat counter
_sending: ContextVar[bool] = ContextVar("error_sending", default=False)
ERROR_MARKS = (2, 3, 5, 10, 25, 50, 100, 250, 500, 1000)
ERRORS_PER_MIN = 25


class ErrorTopic(logging.Handler):
    """ERROR and CRITICAL records of the bot's own loggers -> the admin chat's «Ошибки бота» topic (only a group:
    tracebacks never go to private chats). Never raises; its own failures are dropped, they would loop back here."""

    def __init__(self, bot: Bot):
        super().__init__(logging.ERROR)
        self.bot = bot
        self.seen: dict[str, list] = {}  # signature -> [message id, chat, count, first seen, text]
        self.minute: list[float] = []
        self.dropped = 0
        self.tasks: set[asyncio.Task] = set()  # running sends: a task nobody holds may be collected mid-way
        self.serial = asyncio.Lock()  # one at a time: the same error twice in a row is one post, not two

    def emit(self, record: logging.LogRecord) -> None:
        if _sending.get() or not record.name.startswith("bot") or record.name == __name__:
            return  # a failure while reporting a failure would only loop back here
        with suppress(RuntimeError):  # no running loop: nowhere to send from
            task = asyncio.get_running_loop().create_task(self.send(record))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    @staticmethod
    def signature(record: logging.LogRecord) -> tuple[str, str, str]:
        """(what, where, the traceback's tail)."""
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            frames = traceback.extract_tb(record.exc_info[2])
            own = [f for f in frames if "/bot/" in f.filename] or frames
            last = own[-1] if own else None
            where = f"{last.filename.split('/bot/')[-1]}:{last.lineno} · {last.name}" if last else record.name
            tail = "".join(traceback.format_exception(*record.exc_info))[-1500:]
            return f"{type(exc).__name__}: {str(exc)[:300]}", where, tail
        return record.getMessage()[:300], f"{record.name}:{record.lineno}", ""

    async def send(self, record: logging.LogRecord) -> None:
        _sending.set(True)  # this task's own context only
        async with self.serial:
            await self._send(record)

    async def _send(self, record: logging.LogRecord) -> None:
        try:
            what, where, tail = self.signature(record)
            key = f"{what}|{where}"
            t = asyncio.get_running_loop().time()
            prev = self.seen.get(key)
            if prev and t - prev[3] < ERROR_WINDOW:
                prev[2] += 1
                if prev[2] in ERROR_MARKS:
                    with suppress(Exception):
                        await self.bot.edit_message_text(
                            text=prev[4] + f"\n<blockquote><b>Повторов:</b> ×{prev[2]} · последний "
                                           f"{datetime.now(MSK):%H:%M:%S} МСК</blockquote>",
                            chat_id=prev[1], message_id=prev[0], disable_web_page_preview=True)
                return
            self.minute = [x for x in self.minute if t - x < 60]
            if len(self.minute) >= ERRORS_PER_MIN:
                self.dropped += 1
                return
            self.minute.append(t)
            head = (f"{mark('🛑')} <b>Критическая ошибка</b>" if record.levelno >= logging.CRITICAL
                    else f"{mark('❌')} <b>Ошибка в боте</b>")
            text = "\n".join(p for p in [
                head, "",
                card(cf("Что", f"<code>{esc(what)}</code>"), cf("Где", f"<code>{esc(where)}</code>"),
                     cf("Сообщение", esc(record.getMessage()[:300])) if record.exc_info else ""),
                f"<i>{datetime.now(MSK):%d.%m %H:%M:%S} МСК</i>"
                + (f" · ещё {self.dropped} пропущено (лимит в минуту)" if self.dropped else ""),
                f"<blockquote expandable>{esc(tail)}</blockquote>" if tail else "",
            ] if p)
            self.dropped = 0
            async with Session() as s:
                for chat in targets():
                    if chat > 0:
                        continue
                    with suppress(Exception):
                        m = await post(self.bot, s, chat, "errors", text, None, silent=False)
                        self.seen[key] = [m.message_id, chat, 1, t, text]
                await s.commit()
        except Exception:  # noqa: BLE001 - never loop back into logging
            pass


# ---------- the topics' layout: renewed on start when it changes ----------

LAYOUT = "3"  # bump when TOPICS change: on the next start the bot makes a fresh set of topics with their pins


async def ensure_topics(bot: Bot) -> None:
    """On start: in a forum admin chat whose topics were made for another LAYOUT, forget the old topics (the admin
    deletes them himself), the cards and the statistics message, then create every topic anew with its pinned help.
    The layout mark is saved before creating, and every topic id right after its creation, so a crash midway never
    makes a second set: the next start only adds what is missing. Every start also refreshes changed pins."""
    from sqlalchemy import delete
    async with Session() as s:
        for chat in [c for c in targets() if c < 0]:
            if not await is_forum(bot, chat):
                continue
            mark = await s.get(Setting, f"layout:{chat}")
            if mark is None or mark.value != LAYOUT:
                for key in list(TOPICS) + ["old"]:
                    _topics.pop((chat, key), None)
                rows = (await s.scalars(select(Setting).where(
                    Setting.key.like(f"topic:{chat}:%") | Setting.key.like(f"tpin:{chat}:%")
                    | (Setting.key == f"stats_msg:{chat}")))).all()
                for row in rows:
                    await s.delete(row)
                await s.execute(delete(LogMessage).where(LogMessage.chat_id == chat))
                await s.merge(Setting(key=f"layout:{chat}", value=LAYOUT))
                await s.commit()
                log.info("admin chat %s: new topics layout %s", chat, LAYOUT)
            for key in TOPICS:
                tid = await thread(bot, s, chat, key)
                if tid is not None:
                    await pin_help(bot, s, chat, key, tid)
                await s.commit()


# ---------- /setts: an admin sets up the admin chat from inside the group ----------

router = Router()


@router.callback_query(F.data.startswith("lg:"), IsAdmin())
async def cb_fold(c: CallbackQuery, s: AsyncSession):
    """«Свернуть в карточку»: a screen opened on a log card turns back into the card."""
    _, text, markup = await render(s, c.data[3:], False)
    with suppress(TelegramAPIError):
        await c.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    await c.answer()


@router.message(Command("setts"), F.chat.type.in_({"group", "supergroup"}), IsAdmin())
async def cmd_setts(m: Message, bot: Bot, s: AsyncSession):
    from bot.services import settings
    chat = m.chat.id
    _forum.pop(chat, None)
    for key in TOPICS:
        _topics.pop((chat, key), None)
    await settings.put(s, "log_chat", str(chat))
    await s.merge(Setting(key=f"layout:{chat}", value=LAYOUT))  # topics made now are of the current layout
    await s.commit()
    reply = lambda text: m.answer(text, disable_web_page_preview=True)  # noqa: E731 - into the thread it was typed in
    if not await is_forum(bot, chat):
        return await reply(clean("\n".join([
            f"{pe('ok')} <b>Админ-чат Strait Pay — здесь.</b>",
            "",
            "<blockquote>Все логи будут приходить в эту группу, кнопки и команды работают прямо тут.</blockquote>",
            section("info", "Чтобы разложить логи по темам"),
            "<blockquote>Настройки группы → <b>Темы</b> → включить; права бота: <b>«Управление темами»</b>, "
            "«Удаление» и «Закрепление сообщений» — и снова /setts.</blockquote>"])))
    made, failed, pins = [], [], {"new": 0, "updated": 0, "same": 0}
    for key, (name, _) in TOPICS.items():
        existed = await s.get(Setting, f"topic:{chat}:{key}") is not None
        tid = await thread(bot, s, chat, key)
        if tid is None:
            failed.append(name)
            continue
        made.append(name if existed else f"{name} · новая")
        pins["new" if not existed else await pin_help(bot, s, chat, key, tid)] += 1  # a new topic got it already
    await s.commit()
    if failed:
        return await reply(clean("\n".join([
            f"{pe('warn')} <b>Не получилось создать темы</b>", "",
            "<blockquote>" + "\n".join(esc(f) for f in failed) + "</blockquote>",
            "<blockquote>Дайте боту права администратора с правом «Управление темами» и снова отправьте "
            "/setts.</blockquote>"])))
    await reply(clean("\n".join([
        f"{pe('ok')} <b>Админ-чат Strait Pay настроен.</b>", "",
        card(cf("Тем", str(len(made)), icon="list"),
             cf("Закрепы-справки", f"новых {pins['new']} · обновлено {pins['updated']} · без изменений {pins['same']}",
                icon="pencil")),
        "",
        "<blockquote>Карточки операций обновляются на месте; всё, что требует решения, дублируется в "
        "«⚠️ Требует внимания». Админ-панель — /admin в «🎛 Управление».</blockquote>"])))
