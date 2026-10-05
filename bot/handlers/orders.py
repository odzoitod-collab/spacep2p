"""Order requisites in the bot: the buyer's request, order merchants (application, console, taking a request by a
Bybit order or from the balance, giving requisites or the order link), operators (accepting a Bybit order, giving its
requisites) and the broadcast of requests — to every merchant and into the community and team chats.
Money and state transitions live in services/orders.py."""
import re
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.handlers.deal import deal_screen, log as deal_log, push
from bot.handlers.seller import BANKS, check_bank, check_holder, check_requisites
from bot.models import Deal, Event, OrderMerchant, OrderOffer, Team, User, now
from bot.services import deals, events, money, operators, orders, settings
from bot.ui import (at, clean, close_kb, deep_link, esc, field, manual, mark, notify, ok, person, quote, safe_text,
                    section, show, title,
                    warn)

router = Router()
REAPPLY_AFTER = timedelta(hours=24)
SPEED = ["до 5 минут", "5–10 минут", "10–15 минут"]
CLOSED = {"taken": "уже взята", "cancelled": "отменена покупателем", "expired": "закрыта: время поиска вышло",
          "void": "закрыта администрацией"}


# ---------- the request as merchants and chats see it ----------

def request_facts(d: Deal) -> list[str]:
    return [f"• Сумма перевода: <b>{money.fmt(d.amount_rub)} ₽</b>",
            f"• Банк покупателя: <b>{esc(d.sender_bank)}</b>" if d.sender_bank else "",
            f"• Курс площадки для ордера: <b>{money.fmt(d.merchant_rate)} ₽</b> за USDT",
            f"• Зайти в ордер на: <b>{money.usdt(d.seller_debit)} USDT</b>",
            f"• Взять до {at(d.expires_at)} · ссылка на ордер — за {settings.get('order_link_minutes')} мин"]


def offer_text(d: Deal, u: User, rep_problem: str = "") -> str:
    covered = u.balance >= d.seller_debit
    return "\n".join([
        f"{pe('bell')} <b>Новая заявка #{d.id} · {money.fmt(d.amount_rub)} ₽</b>",
        quote(*request_facts(d)),
        quote(f"• Bybit-ордер недоступен: {rep_problem}" if rep_problem else
              "• Bybit-ордер: баланс не нужен — пришлёте ссылку, оператор выдаст реквизиты. Нет ссылки за "
              f"{settings.get('order_link_minutes')} мин — заявка уйдёт другим",
              f"• С баланса: заморозим {money.usdt(d.seller_debit)} USDT, реквизиты выдаёте сами"
              + ("" if covered else f" — свободно только {money.usdt(u.balance)}")),
        "Кто первым нажмёт «Взять», тот и работает заявку.",
    ])


def offer_kb(d: Deal, u: User, rep_problem: str = ""):
    return kb(None if rep_problem else btn("Взять · Bybit-ордер", f"orq:take:{d.id}:b", "fire", style="success"),
              btn("Взять · с баланса", f"orq:take:{d.id}:w", "wallet", style="primary")
              if u.balance >= d.seller_debit else None,
              back("x", "Скрыть", "cross"))


CLOSED_TEXT = {"completed": "Выполнена — покупатель получил USDT", "cancelled": "Закрыта", "void": "Закрыта администрацией",
               "expired": "Закрыта: покупатель не оплатил вовремя"}


def _steps(d: Deal) -> list[tuple[str, str]]:
    """The way of this request, step by step: (step, what it is waiting for when it is the current one)."""
    take = ("Мерчант взял заявку", "ждём мерчанта")
    if d.via_bybit and d.status != "searching":
        mid = [("Ссылка на Bybit-ордер получена", f"ждём ссылку до {d.expires_at.astimezone(deals.MSK):%H:%M} МСК"
                if d.status == "assigned" else ""),
               ("Оператор выдал реквизиты", "оператор проверяет ордер" if d.operator_id else "ждём оператора")]
    else:
        mid = [("Реквизиты выданы", "мерчант готовит реквизиты")]
    return [take, *mid, ("Покупатель оплатил", "ждём перевод и чек"), ("Оплата подтверждена", "чек на проверке")]


def _done(d: Deal) -> int:
    """How many steps of _steps(d) are behind."""
    order = {"searching": 0, "assigned": 1, "waiting_payment": None, "paid": None, "dispute": None, "completed": None}
    n = len(_steps(d))
    if d.status == "checking":
        return 2
    if d.status == "waiting_payment":
        return n - 2
    if d.status in ("paid", "dispute"):
        return n - 1
    if d.status == "completed":
        return n
    return order.get(d.status) or 0


def chat_text(d: Deal) -> str:
    """The post of a request in the community and team chats: the request, then its way — what is done, what it is
    waiting for now. The bot edits it as the request moves on."""
    closed = d.status in CLOSED_TEXT or d.status == "void"
    head = (f"{pe('ok') if d.status == 'completed' else pe('cross')} <b>Заявка #{d.id} · {money.fmt(d.amount_rub)} ₽"
            f"</b> · {CLOSED_TEXT.get(d.status, 'закрыта')}" if closed else
            f"{pe('bell')} <b>{'Новая заявка' if d.status == 'searching' else 'Заявка'} #{d.id} · "
            f"{money.fmt(d.amount_rub)} ₽</b>")
    lines = [head, quote(*request_facts(d)[:-1]) if not closed else ""]
    if d.status == "searching":
        lines += [f"{pe('clock')} <b>Ищем мерчанта</b> до {at(d.expires_at)} · ссылка на ордер — за "
                  f"{settings.get('order_link_minutes')} мин",
                  "Взять — кнопкой ниже: в боте выберете Bybit-ордер (без баланса) или работу с баланса."]
    elif not closed or d.status == "completed":
        steps, done = _steps(d), _done(d)
        lines.append(section("list", "Ход заявки"))
        for i, (step, wait) in enumerate(steps):
            mark_ = "✅" if i < done else "⏳" if i == done else "▫️"
            lines.append(f"{mark(mark_)} {step}" + (f" — <i>{wait}</i>" if i == done and wait else ""))
        if d.status == "dispute":
            lines.append(f"{pe('flag')} <b>Спор</b> — решает администрация")
    return "\n".join(x for x in lines if x)


def chat_markup(bot_link: str, d: Deal):
    return kb(btn("Взять заявку в боте", url=bot_link, icon="fire", style="success")) if d.status == "searching" \
        else None


_posted: dict[tuple[int, int], str] = {}  # (chat, message) -> the text last put there: unchanged posts are not edited


async def sync_chat_posts(bot: Bot, s: AsyncSession, d: Deal) -> None:
    """Every chat post of this request shows its current state (the take button only while it is searching)."""
    rows = (await s.execute(select(OrderOffer.user_id, OrderOffer.msg_id).where(
        OrderOffer.deal_id == d.id, OrderOffer.kind == "chat", OrderOffer.msg_id.is_not(None)))).all()
    for chat, mid in rows:
        team = await s.scalar(select(Team.id).where(Team.chat_id == chat))
        text = clean(chat_text(d))
        if _posted.get((chat, mid)) == text:
            continue
        markup = chat_markup(await deep_link(bot, f"o{d.id}" + (f"_t{team}" if team else "")), d)
        try:
            await safe_text(lambda t: bot.edit_message_text(chat_id=chat, message_id=mid, text=t, reply_markup=markup,
                                                            disable_web_page_preview=True), text)
        except TelegramAPIError as e:
            if "not modified" not in str(e):
                continue
        _posted[(chat, mid)] = text


async def chats(s: AsyncSession) -> list[tuple[int, int | None]]:
    """(chat id, team id) where requests are posted: the community chat and every working team's chat."""
    out = [(int(settings.get("chat_id")), None)] if settings.get("chat_id") else []
    rows = (await s.execute(select(Team.chat_id, Team.id).where(Team.status == "approved",
                                                                Team.chat_id.is_not(None)))).all()
    return out + [(cid, tid) for cid, tid in rows if cid not in {c for c, _ in out}]


async def broadcast(bot: Bot, s: AsyncSession, d: Deal, first: bool = True) -> int:
    """Send the request to every merchant and chat that has not seen it. Commits.
    Merchants with more completed deals first; first=False: a periodic re-send to merchants approved later and chats
    added later — quiet if nobody new. A failed delivery is remembered too, so a blocked bot is not retried."""
    merchants = await orders.eligible(s, d)
    done = await deals.completed_count(s, [u.id for _, u in merchants])
    sent = 0
    for _, u in sorted(merchants, key=lambda mu: -done[mu[1].id]):
        problem = orders.bybit_problem((await orders.reputation(s, u.id))[0], d)
        if problem and u.balance < d.seller_debit:
            continue  # neither way is open to him for this request
        m = await notify(bot, u.id, offer_text(d, u, problem), offer_kb(d, u, problem), silent=u.quiet)
        s.add(OrderOffer(deal_id=d.id, user_id=u.id, msg_id=m.message_id if m else None))
        sent += m is not None
    posted = set((await s.scalars(select(OrderOffer.user_id).where(OrderOffer.deal_id == d.id,
                                                                   OrderOffer.kind == "chat"))).all())
    in_chats = 0
    for chat, team in await chats(s):
        if chat in posted:
            continue
        payload = f"o{d.id}" + (f"_t{team}" if team else "")
        try:
            markup = chat_markup(await deep_link(bot, payload), d)
            m = await safe_text(lambda t: bot.send_message(chat, t, reply_markup=markup, disable_web_page_preview=True),
                                clean(chat_text(d)))
            _posted[(chat, m.message_id)] = clean(chat_text(d))
            in_chats += 1
        except TelegramAPIError as e:
            m = None
            await events.alert_once(s, "app:chat", "post_failed", f"Заявка не опубликована в чате {chat}: {e}"[:300])
        s.add(OrderOffer(deal_id=d.id, user_id=chat, msg_id=m.message_id if m else None, kind="chat"))
    if sent or in_chats or first:
        deal_log(s, d, "offered", f"Заявка на {money.fmt(d.amount_rub)} ₽ разослана: мерчантам {sent}, в чаты "
                                  f"{in_chats}" + ("" if first else " (досыл)"), notice=True)
    if not sent and not in_chats and first:
        await events.alert_once(s, f"deal:{d.id}", "no_merchants", f"Ордерная заявка на {money.fmt(d.amount_rub)} ₽: "
                                "некому отправить — нет ордерных мерчантов и чатов", d.buyer_id)
    await s.commit()
    return sent + in_chats


async def close_offers(bot: Bot, s: AsyncSession, d: Deal, text: str, keep: int | None = None,
                       kinds: tuple[str, ...] = ("merchant", "operator")) -> None:
    """Remove the buttons from every copy of this request sent to merchants and operators, except `keep`'s. The chat
    posts are not closed: they show the request's way (sync_chat_posts)."""
    kinds = tuple(k for k in kinds if k != "chat")
    for uid, mid, kind in await orders.forget_offers(s, d.id, kinds):
        if uid == keep:
            continue
        body = (f"<b>Заявка #{d.id} · {money.fmt(d.amount_rub)} ₽</b> — {text}" if kind == "chat"
                else f"{pe('info')} {text}")
        with suppress(TelegramAPIError):
            await safe_text(lambda t: bot.edit_message_text(chat_id=uid, message_id=mid, text=t,
                                                            reply_markup=None if kind == "chat" else close_kb()),
                            clean(body))
    await s.commit()
    await sync_chat_posts(bot, s, d)


# ---------- buyer: a request for requisites under the exact amount (the amount comes from handlers/market) ----------

@router.callback_query(F.data == "orb:go")
async def cb_request_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
    await state.set_state(None)
    await state.update_data(o_amount=None, o_bank=None, o_credit=None)
    if not data.get("o_amount"):
        return await c.answer("Заявка уже создана или устарела", show_alert=True)
    try:
        d = await orders.create_request(s, user, Decimal(data["o_amount"]), data.get("o_bank"),
                                        Decimal(data["o_credit"]) if data.get("o_credit") else None)
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)
        return await c.answer(str(e), show_alert=True)
    deal_log(s, d, "created", f"Заявка на реквизиты под сумму: {money.fmt(d.amount_rub)} ₽ → "
                              f"{money.usdt(d.buyer_credit)} USDT, создал покупатель {person(user)}", notice=True)
    await s.commit()
    await deal_screen(bot, s, user, d, c)
    await broadcast(bot, s, d)


@router.callback_query(F.data.regexp(r"^orb:cn:(\d+)$"))
async def cb_request_cancel(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    did = int(c.data.split(":")[2])
    d = await s.get(Deal, did)
    if not d or d.buyer_id != user.id:
        return await c.answer("Заявка не найдена", show_alert=True)
    res = await orders.cancel(s, did)
    if res is None:
        return await c.answer("Реквизиты уже выданы — отмена доступна на экране сделки", show_alert=True)
    deal_log(s, res, "cancelled", "Покупатель отменил заявку на реквизиты", notice=True)
    await s.commit()
    await deal_screen(bot, s, user, res, c, ok("Заявка отменена"))
    await request_cancelled(bot, s, res)


async def request_cancelled(bot: Bot, s: AsyncSession, d: Deal) -> None:
    """The buyer cancelled a request before requisites: offers are closed, whoever worked on it is told."""
    await close_offers(bot, s, d, f"Заявка #{d.id} {CLOSED['cancelled']}")
    if d.seller_id:
        await notify(bot, d.seller_id, f"{pe('warn')} Покупатель отменил заявку #{d.id}." + (
            " Отмените ордер на Bybit, если уже создали." if d.via_bybit else
            f" Заморозка {money.usdt(d.seller_debit)} USDT снята."))
    if d.via_bybit and d.operator_id:
        await notify(bot, d.operator_id, f"{pe('warn')} Заявка #{d.id} отменена покупателем — не выдавайте "
                                         "реквизиты, отмените ордер на Bybit.")


# ---------- merchant: take a request (from an offer, the cabinet or a chat link) ----------

class OrderGive(StatesGroup):
    number = State()
    link = State()


BANK_ALIASES = {"сбер": "Сбербанк", "тинькофф": "Т-Банк", "тинек": "Т-Банк", "тиньк": "Т-Банк", "т-банк": "Т-Банк",
                "т банк": "Т-Банк", "тбанк": "Т-Банк", "альфа": "Альфа-Банк", "втб": "ВТБ", "райф": "Райффайзен",
                "озон": "Озон Банк", "газпром": "Газпромбанк", "совком": "Совкомбанк"}
NUMBER = re.compile(r"\+?\d[\d\s()\-]{8,}\d")


def parse_requisites(raw: str) -> tuple[tuple[str, str, str, str] | None, str]:
    """One message «card number or SBP phone + bank» (the recipient's name optional, on its own line) ->
    ((kind, number, bank, holder), "") or (None, what is wrong)."""
    found = NUMBER.search(raw)
    if not found:
        return None, "Не вижу номера карты или телефона"
    digits = re.sub(r"\D", "", found.group())
    kind = "card" if len(digits) >= 16 else "sbp"
    number, err = check_requisites(kind, digits)
    if not number:
        return None, err
    lines = [" ".join(x.split()) for x in (raw[:found.start()] + "\n" + raw[found.end():]).splitlines()]
    lines = [x for x in (y.strip(" ,;:—-") for y in lines) if x.strip(".")]
    if not lines:
        return None, "Добавьте банк получателя"
    lines[0] = lines[0].strip(".")
    low = lines[0].lower()
    bank = next((v for k, v in BANK_ALIASES.items() if low.startswith(k)), None) or \
        next((b for b in BANKS if low == b.lower()), None) or check_bank(lines[0])
    if not bank:
        return None, "Название банка — от 2 до 40 символов"
    holder = check_holder(" ".join(lines[1:])) or "" if len(lines) > 1 else ""
    return (kind, number, bank, holder), ""


def link_line(d: Deal) -> str:
    return f'<a href="{esc(d.bybit_url)}">{esc(d.bybit_url[:60])}</a>' if d.bybit_url else "—"


async def take_screen(bot: Bot, s: AsyncSession, user: User, deal_id: int, src=None, note: str = ""):
    """A request opened from a chat post: details and the two ways to take it (or why this user cannot)."""
    d = await s.get(Deal, deal_id, populate_existing=True)
    if d is None or not d.is_order:
        return await show(bot, user, warn("Заявка не найдена"), kb(back("menu", "В меню")), src)
    if d.buyer_id == user.id and d.api_client_id is None:  # his own request, posted in the chat
        return await deal_screen(bot, s, user, d, src, note)
    if d.status != "searching" or deals.aware(d.expires_at) < now():
        mine = d.seller_id == user.id or d.buyer_id == user.id
        return await show(bot, user, f"{title(pe('bell'), f'Заявка #{d.id}')}\n\n"
                                     f"{pe('info')} Заявку уже взяли или она закрыта." + note,
                          kb(btn("Открыть сделку", f"dl:{d.id}", "fire", style="primary") if mine else None,
                             btn("Ордерный кабинет", "om", "key"), back("menu", "В меню")), src)
    m = await s.get(OrderMerchant, user.id)
    lines = [f"{title(pe('bell'), f'Заявка #{d.id} · {money.fmt(d.amount_rub)} ₽')}", *filter(None, request_facts(d)), ""]
    if m is None or m.status != "approved":
        lines += [f"{pe('lock')} Брать заявки могут ордерные мерчанты Strait Pay.",
                  "Заполните короткую анкету — после одобрения заявки будут приходить вам в бот и в чат."
                  if m is None or m.status == "rejected" else "Ваша анкета на рассмотрении — ответ придёт в этот чат."
                  if m.status == "pending" else "Ваш доступ ордерного мерчанта приостановлен."]
        return await show(bot, user, "\n".join(lines) + note, kb(btn("Ордерные реквизиты", "om", "key", style="primary"),
                                                                 back("menu", "В меню")), src)
    lines.append("<b>Как работать</b>")
    lines.append("• Bybit-ордер: баланс не нужен — пришлёте ссылку на ордер, оператор выдаст реквизиты")
    lines.append(f"• С баланса: заморозим {money.usdt(d.seller_debit)} USDT (свободно {money.usdt(user.balance)}), "
                 "реквизиты выдаёте сами")
    problem = orders.bybit_problem((await orders.reputation(s, user.id))[0], d)
    await show(bot, user, "\n".join(lines) + note, offer_kb(d, user, problem), src)


@router.callback_query(F.data.regexp(r"^orq:take:(\d+)(?::([bw]))?$"))
async def cb_take(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, did, *how = c.data.split(":")
    did, bybit = int(did), (how or ["b"])[0] == "b"
    try:
        d = await orders.take(s, did, user, bybit)
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)
        await c.answer(str(e), show_alert=True)
        if e.code == "taken" and c.message:
            with suppress(TelegramAPIError):
                await c.message.edit_text(f"{pe('info')} Заявка #{did} {CLOSED['taken']}", reply_markup=close_kb())
        return
    deal_log(s, d, "taken", f"Принял мерчант {person(user)}: " + (
        "Bybit-ордер" if d.via_bybit else f"с баланса, заморожено {money.usdt(d.seller_debit)} USDT"), notice=True)
    await s.commit()
    await close_offers(bot, s, d, f"Заявка #{d.id} {CLOSED['taken']}", keep=user.id)
    if not d.via_bybit:  # a Bybit-order take counts only with the link: the buyer hears about it then
        await push(bot, s, d.buyer_id, d, f"Мерчант взял заявку #{d.id} и готовит реквизиты")
    await (link_screen if d.via_bybit else give_screen)(bot, s, user, d, state, c)


def _give_head(d: Deal) -> str:
    bybit = (f"• Ордер Bybit: {link_line(d)}\n• Зайти на <b>{money.usdt(d.seller_debit)} USDT</b> по "
             f"{money.fmt(d.merchant_rate)} ₽\n") if d.status == "checking" else ""
    return (f"{title(pe('key'), f'Реквизиты для заявки #{d.id}')}\n"
            f"• <b>{money.fmt(d.amount_rub)} ₽</b>" + (f" · перевод из {esc(d.sender_bank)}" if d.sender_bank else "") + "\n"
            f"• Выдать до {at(d.expires_at)}\n{bybit}\n")


async def _assigned(s: AsyncSession, user: User, did: int) -> Deal | None:
    """The request this merchant took and has not answered yet (requisites or a Bybit link)."""
    d = await s.get(Deal, did, populate_existing=True)
    return d if d and d.status == "assigned" and d.seller_id == user.id else None


async def _mine(s: AsyncSession, user: User, did: int) -> Deal | None:
    """The request whose requisites this user gives now: his own (balance) or a Bybit order he accepted."""
    d = await s.get(Deal, did, populate_existing=True)
    return d if orders.giver(d, user.id) else None


async def give_screen(bot: Bot, s: AsyncSession, user: User, d: Deal, state: FSMContext, src=None, note: str = ""):
    """Requisites in one message: a card number or an SBP phone and the bank — nothing to pick, no old cards."""
    await state.set_state(OrderGive.number)
    await state.update_data(g_deal=d.id)
    operator = d.status == "checking"
    await show(bot, user, _give_head(d) + "\n".join([
        "Откройте ордер, сверьте сумму и курс, зайдите в него и пришлите реквизиты из ордера. Реквизитов нет или "
        "ссылка не та — «Проблема с ордером»." if operator and d.bybit_url else
        "Пришлите свои реквизиты — рубли придут на них, USDT покупателю зачислит площадка." if operator else
        "Пришлите реквизиты, на которые покупатель переведёт рубли.",
        "",
        quote("<b>Одним сообщением: номер карты или телефон СБП и банк</b>",
              "• <code>2200 7001 2345 6781 Сбербанк</code>",
              "• <code>+7 900 123-45-67 Т-Банк</code>",
              "ФИО получателя — по желанию, второй строкой."),
    ]) + note, kb(
        btn("Открыть ордер Bybit", url=d.bybit_url, icon="shop", style="success") if operator and d.bybit_url else None,
        btn("Вернуть в поиск", f"opq:back:{d.id}", "refresh") if operator and not d.bybit_url else
        btn("Проблема с ордером", f"opq:pr:{d.id}", "warn", style="danger") if operator
        else btn("Отказаться", f"orq:drop:{d.id}", "cross"),
    ), src)


async def link_screen(bot: Bot, s: AsyncSession, user: User, d: Deal, state: FSMContext, src=None, note: str = ""):
    """Bybit order: the merchant sends the link to his Bybit P2P order instead of requisites."""
    await state.set_state(OrderGive.link)
    await state.update_data(g_deal=d.id)
    await show(bot, user, "\n".join([
        title(pe("shop"), f"Bybit-ордер · заявка #{d.id}"),
        f"{pe('clock')} <b>Ссылка — до {at(d.expires_at)}</b>, иначе заявка уйдёт другим мерчантам.",
        "",
        quote(f"• Сумма ордера: <b>{money.fmt(d.amount_rub)} ₽</b>",
              f"• Курс: <b>{money.fmt(d.merchant_rate)} ₽</b> за USDT · на <b>{money.usdt(d.seller_debit)} USDT</b>",
              f"• Банк покупателя: {esc(d.sender_bank)}" if d.sender_bank else ""),
        quote("1. Создайте на Bybit P2P ордер на продажу на эту сумму и курс — с вашими реквизитами.",
              "2. Пришлите сюда ссылку на ордер.",
              "3. Оператор зайдёт в ордер и выдаст покупателю реквизиты. Если реквизитов в ордере не окажется — "
              f"это пропуск; {settings.get('strike_limit')} пропуска подряд — пауза "
              f"{settings.human('strike_sleep_hours')}."),
    ]) + note, kb(btn("Отказаться", f"orq:drop:{d.id}", "cross")), src)


@router.callback_query(F.data.regexp(r"^orq:give:(\d+)$"))
async def cb_give(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    did = int(c.data.split(":")[2])
    if (d := await _assigned(s, user, did)) and d.via_bybit:
        return await link_screen(bot, s, user, d, state, c)
    d = await _mine(s, user, did)
    if not d:
        return await c.answer("Заявка уже не у вас", show_alert=True)
    await give_screen(bot, s, user, d, state, c)


@router.message(OrderGive.link, F.text)
async def msg_link(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await _assigned(s, user, (await state.get_data()).get("g_deal", 0))
    if not d or not d.via_bybit:
        await state.clear()
        return await show(bot, user, warn("Заявка уже не у вас"), close_kb())
    url = orders.bybit_link(m.text)
    if not url:
        return await link_screen(bot, s, user, d, state, note=warn(
            "Нужна ссылка на ордер Bybit: https://www.bybit.com/… (скопируйте из приложения Bybit)"))
    did = d.id
    try:
        d = await orders.give_link(s, did, user, url)
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)
        d = await _assigned(s, user, did)
        if e.code == "link_used" and d:
            return await link_screen(bot, s, user, d, state, note=warn(str(e)))
        await state.clear()
        return await show(bot, user, warn(str(e)), close_kb())
    await state.clear()
    deal_log(s, d, "link", f"Мерчант {person(user)} прислал Bybit-ордер: {url}", notice=True)
    await s.commit()
    await deal_screen(bot, s, user, d, note=ok("Ссылка ушла операторам. Первый, кто примет ордер, выдаст "
                                               "покупателю реквизиты."))
    await push(bot, s, d.buyer_id, d, f"Ордер по заявке #{d.id} найден — оператор проверяет его и выдаёт реквизиты")
    await notify_operators(bot, s, d, user)


# ---------- operators: accept a Bybit order, give its requisites ----------

def operator_offer_text(d: Deal, merchant: User | None, rep: str = "") -> str:
    return "\n".join(line for line in [
        f"{pe('bell')} <b>Bybit-ордер · заявка #{d.id} · {money.usdt(d.seller_debit)} USDT</b>",
        f"• Сумма ордера: <b>{money.fmt(d.amount_rub)} ₽</b>",
        f"• Курс мерчанта: <b>{money.fmt(d.merchant_rate)} ₽</b> за 1 USDT",
        f"• Зайти на: <b>{money.usdt(d.seller_debit)} USDT</b>",
        f"• Покупатель переводит из: {esc(d.sender_bank)}" if d.sender_bank else "",
        f"• Мерчант: {esc(merchant.name or '—') if merchant else '—'} (<code>{d.seller_id}</code>)",
        f"• Репутация мерчанта: {rep}" if rep else None,
        f"• Принять до {at(d.expires_at)}",
        "",
        "Нажмите «Принять ордер» — ссылка на ордер придёт вам, у остальных операторов заявка пропадёт. "
        "Принятый ордер становится вашим долгом после подтверждения оплаты.",
    ] if line is not None)


async def notify_operators(bot: Bot, s: AsyncSession, d: Deal, merchant: User | None) -> None:
    """Every operator gets «Принять ордер»; the first one who accepts gets the link, the others' messages close.
    Commits."""
    got = 0
    rep = orders.rep_line(*await orders.reputation(s, d.seller_id)) if d.seller_id else ""
    for oid in await operators.ids(s):
        if oid == d.seller_id:
            continue  # an operator never checks his own order
        who = await s.get(User, oid)
        m = await notify(bot, oid, operator_offer_text(d, merchant, rep),
                         kb(btn("Принять ордер", f"opq:go:{d.id}", "ok", style="success"), back("x", "Скрыть", "cross")),
                         silent=bool(who and who.quiet))
        s.add(OrderOffer(deal_id=d.id, user_id=oid, msg_id=m.message_id if m else None, kind="operator"))
        got += m is not None
    if not got:
        deal_log(s, d, "notify_failed", f"Ни один оператор не получил Bybit-ордер по заявке #{d.id}", alert=True)
    await s.commit()


async def _operator(c: CallbackQuery, s: AsyncSession) -> bool:
    if await operators.is_operator(s, c.from_user.id):
        return True
    await c.answer("Только для операторов", show_alert=True)
    return False


@router.callback_query(F.data.regexp(r"^opq:go:(\d+)$"))
async def cb_operator_take(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    if not await _operator(c, s):
        return
    did = int(c.data.split(":")[2])
    if (d := await s.get(Deal, did)) and d.seller_id == user.id:
        return await c.answer("Это ваш собственный ордер — его примет другой оператор", show_alert=True)
    try:
        d = await orders.claim(s, did, user)
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)
        await c.answer(str(e), show_alert=True)
        if c.message:
            with suppress(TelegramAPIError):
                await c.message.edit_text(f"{pe('info')} {esc(str(e))}", reply_markup=close_kb())
        return
    deal_log(s, d, "operator", f"Ордер принял оператор {person(user)}", notice=True)
    operators.log(s, user.id, d, "accepted", f"заявка на {money.fmt(d.amount_rub)} ₽, ордер {d.bybit_url or '—'}")
    await s.commit()
    await close_offers(bot, s, d, f"Ордер по заявке #{d.id} принял другой оператор", keep=user.id,
                       kinds=("operator",))
    await give_screen(bot, s, user, d, state, c)


@router.callback_query(F.data.regexp(r"^opq:back:(\d+)$"))
async def cb_operator_back(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await orders.unclaim(s, int(c.data.split(":")[2]), user)
    if d is None:
        return await c.answer("Ордер уже не у вас", show_alert=True)
    await state.clear()
    if d.status == "searching":  # an admin gave a request without an order back: the search goes on
        deal_log(s, d, "admin_back", f"{person(user)} вернул заявку в поиск", notice=True)
        await s.commit()
        await show(bot, user, f"{pe('ok')} Заявка #{d.id} снова в поиске мерчанта.", close_kb(), c)
        return await broadcast(bot, s, d)
    deal_log(s, d, "operator_back", f"Оператор {person(user)} вернул Bybit-ордер другим операторам", notice=True)
    operators.log(s, user.id, d, "returned", "вернул ордер другим операторам")
    await s.commit()
    await show(bot, user, f"{pe('ok')} Ордер по заявке #{d.id} возвращён — его примет другой оператор.", close_kb(), c)
    await notify_operators(bot, s, d, await s.get(User, d.seller_id))


@router.callback_query(F.data.regexp(r"^opq:rj:(\d+)$"))
async def cb_operator_reject(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    if not await _operator(c, s):
        return
    d = await orders.reject_link(s, int(c.data.split(":")[2]), user)
    if d is None:
        return await c.answer("Ордер уже обработан другим оператором или заявка закрыта", show_alert=True)
    await state.clear()
    deal_log(s, d, "link_rejected", f"Оператор {person(user)} отклонил Bybit-ордер, мерчант пришлёт другой",
             notice=True)
    operators.log(s, user.id, d, "rejected_link", "ссылка не та — мерчант пришлёт другую")
    await s.commit()
    await close_offers(bot, s, d, f"Ссылка по заявке #{d.id} отклонена", kinds=("operator",))
    await show(bot, user, f"{pe('ok')} Ссылка по заявке #{d.id} отклонена — мерчант пришлёт другую до {at(d.expires_at)}.",
               close_kb(), c)
    await notify(bot, d.seller_id, "\n".join([
        f"{pe('warn')} <b>Оператор отклонил ссылку на ордер по заявке #{d.id}</b>",
        f"• Нужно: {money.fmt(d.amount_rub)} ₽ = {money.usdt(d.seller_debit)} USDT по {money.fmt(d.merchant_rate)} ₽",
        f"• Пришлите другую ссылку до {at(d.expires_at)}"]),
        kb(btn("Прислать ссылку", f"orq:give:{d.id}", "shop", style="success"),
           btn("Отказаться", f"orq:drop:{d.id}", "cross")))


@router.callback_query(F.data.regexp(r"^opq:pr:(\d+)$"))
async def cb_operator_problem(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """What is wrong with the merchant's order: no requisites in it (a miss for him), a wrong link, or not for me."""
    d = await _mine(s, user, int(c.data.split(":")[2]))
    if not d or not d.bybit_url:
        return await c.answer("Ордер уже не у вас", show_alert=True)
    m = await s.get(OrderMerchant, d.seller_id)
    await show(bot, user, "\n".join([
        title(pe("warn"), f"Проблема с ордером · заявка #{d.id}"),
        "",
        quote(f"• Мерчант дал реквизиты в ордере? Если нет — это его пропуск "
              f"({(m.strikes if m else 0) + 1} из {settings.get('strike_limit')} подряд → пауза "
              f"{settings.human('strike_sleep_hours')}), заявка уйдёт другим мерчантам.",
              "• Другая причина (мерчант просит отменить, не отвечает) — «Искать другого мерчанта» без пропуска.",
              "• Ссылка не та (другая сумма, закрытый ордер) — мерчант пришлёт другую.",
              "• Не можете взять сами — ордер вернётся другим операторам.",
              "Этот мерчант больше не сможет взять эту заявку."),
    ]), kb(btn("Мерчант не дал реквизиты · пропуск", f"opq:nr:{d.id}", "cross", style="danger"),
           btn("Искать другого мерчанта · без пропуска", f"opq:nm:{d.id}", "search"),
           [btn("Ссылка не та", f"opq:rj:{d.id}", "pencil"), btn("Вернуть операторам", f"opq:back:{d.id}", "refresh")],
           back(f"orq:give:{d.id}", "Назад")), c)


@router.callback_query(F.data.regexp(r"^opq:(nr|nm):(\d+)$"))
async def cb_no_requisites(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    """The operator puts the request back to the search for another merchant: «nr» — the merchant's order had no
    requisites (a miss for him), «nm» — another reason, no miss. Either way this merchant cannot take this request
    again, and the operator scores him."""
    _, act, did = c.data.split(":")
    d = await _mine(s, user, int(did))
    if not d or not d.bybit_url or not d.seller_id:
        return await c.answer("Ордер уже не у вас", show_alert=True)
    merchant, miss = d.seller_id, act == "nr"
    d = await orders.release(s, d.id)
    count, sleep = await orders.strike(s, merchant) if miss else (0, None)
    await state.clear()
    deal_log(s, d, "no_requisites" if miss else "new_merchant",
             f"Оператор {person(user)}: " + (f"в ордере мерчанта {merchant} нет реквизитов" if miss else
                                             f"ищем другого мерчанта вместо {merchant}") + " — заявка снова в поиске",
             notice=True)
    operators.log(s, user.id, d, "no_requisites" if miss else "new_merchant",
                  f"мерчант {merchant}" + (f": пропуск {count} из {settings.get('strike_limit')}"
                                           + (" → пауза" if sleep else "") if miss else ", без пропуска"))
    if miss:
        _strike_event(s, merchant, d, count, sleep)
    rating = await _ask_rating(s, d, merchant, user, gave=False)
    await s.commit()
    await close_offers(bot, s, d, f"Ордер по заявке #{d.id} закрыт", kinds=("operator",))
    await show(bot, user, f"{pe('ok')} Заявка #{d.id} снова ищет мерчанта"
               + (", мерчанту засчитан пропуск." if miss else ", без пропуска мерчанту."), close_kb(), c)
    await _send_rating(bot, user, rating, merchant, d)
    if miss:
        await _tell_striked(bot, merchant, d, count, sleep)
    else:
        await notify(bot, merchant, f"{pe('info')} Оператор передал заявку #{d.id} другому мерчанту. Ордер на Bybit "
                                    "по ней отмените.")
    await push(bot, s, d.buyer_id, d, f"Ищем другого мерчанта для заявки #{d.id}")
    await broadcast(bot, s, d)


async def _ask_rating(s: AsyncSession, d: Deal, merchant: int, operator: User, gave: bool):
    """A rating slot for this operator and merchant on this request (the operator may fill it once)."""
    from bot.models import MerchantRating
    row = await s.scalar(select(MerchantRating).where(MerchantRating.deal_id == d.id,
                                                      MerchantRating.merchant_id == merchant))
    if row is None:
        row = MerchantRating(deal_id=d.id, merchant_id=merchant, operator_id=operator.id, gave=gave)
        s.add(row)
        await s.flush()
    return row


async def _send_rating(bot: Bot, operator: User, rating, merchant: int, d: Deal) -> None:
    if rating.score is not None or rating.operator_id != operator.id:
        return
    await notify(bot, operator.id, "\n".join([
        f"{pe('star')} <b>Оцените мерчанта · заявка #{d.id}</b>",
        "",
        quote("Насколько надёжно он сработал: ордер, реквизиты, скорость. 1 — плохо, 10 — отлично. После "
              f"{settings.get('rep_min_count')} оценок из них складывается его репутация и лимиты."),
    ]), kb(*[btn(str(n), f"opr:{rating.id}:{n}") for n in range(1, 11)], back("x", "Пропустить", "cross")))


@router.callback_query(F.data.regexp(r"^opr:(\d+):(\d{1,2})$"))
async def cb_rate(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    from bot.models import MerchantRating
    _, rid, score = c.data.split(":")
    row = await s.get(MerchantRating, int(rid), with_for_update=True, populate_existing=True)
    if row is None or row.operator_id != user.id or not 1 <= int(score) <= 10:
        return await c.answer("Эта оценка не ваша", show_alert=True)
    if row.score is not None:
        return await c.answer(f"Оценка уже стоит: {row.score} из 10", show_alert=True)
    row.score = int(score)
    rep, total = await orders.reputation(s, row.merchant_id)
    events.add(s, f"om:{row.merchant_id}", "rated", f"Оценка оператора по заявке #{row.deal_id}: {score}/10 · "
               f"репутация: {orders.rep_line(rep, total)}", row.merchant_id, notice=True)
    d = await s.get(Deal, row.deal_id)
    operators.log(s, user.id, d, "rated", f"мерчант {row.merchant_id}: {score}/10")
    await s.commit()
    await show(bot, user, f"{pe('ok')} Оценка {score}/10 сохранена. Репутация мерчанта: {orders.rep_line(rep, total)}.",
               close_kb(), c)


def _strike_event(s: AsyncSession, merchant: int, d: Deal, count: int, sleep) -> None:
    events.add(s, f"om:{merchant}", "strike", f"Пропуск реквизитов по заявке #{d.id}: {count} из "
               f"{settings.get('strike_limit')} подряд" + (f" — пауза до {sleep.astimezone(deals.MSK):%d.%m %H:%M} МСК"
                                                           if sleep else ""), merchant, alert=bool(sleep), notice=not sleep)


async def _tell_striked(bot: Bot, merchant: int, d: Deal, count: int, sleep) -> None:
    limit = settings.get("strike_limit")
    await notify(bot, merchant, "\n".join([
        f"{pe('warn')} <b>Заявка #{d.id}: в вашем ордере не оказалось реквизитов</b>",
        "",
        quote(f"• Пропуск {count} из {limit} подряд" if not sleep else
              f"• {limit} пропуска подряд — <b>пауза до {at(sleep, 'dt')}</b>: заявки не приходят и не берутся",
              "• Заявка ушла другим мерчантам",
              "• Берите только те заявки, под которые точно есть реквизиты. Выданные реквизиты обнуляют счётчик."),
    ]), kb(btn("Ордерный кабинет", "om", "key"), back("x", "Скрыть", "cross")))


@router.callback_query(F.data.regexp(r"^opq:ans:(\d+):([01])$"))
async def cb_after_timeout(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    """After a Bybit order ran out of time with this operator: did the merchant give requisites?"""
    _, _, did, gave = c.data.split(":")
    d = await s.get(Deal, int(did))
    if d is None or d.operator_id != user.id or not d.seller_id:
        return await c.answer("Вопрос уже неактуален", show_alert=True)
    if await s.scalar(select(Event.id).where(Event.ref == f"deal:{d.id}", Event.kind == "timeout_answer").limit(1)):
        return await c.answer("Ответ уже записан", show_alert=True)
    deal_log(s, d, "timeout_answer", f"Оператор {person(user)}: мерчант " + ("дал" if gave == "1" else "НЕ дал")
             + " реквизиты", notice=True)
    count, sleep = (0, None) if gave == "1" else await orders.strike(s, d.seller_id)
    operators.log(s, user.id, d, "timeout", "мерчант дал реквизиты, не успел оператор" if gave == "1"
                  else f"мерчант не дал реквизиты: пропуск {count} из {settings.get('strike_limit')}")
    if gave != "1":
        _strike_event(s, d.seller_id, d, count, sleep)
    await s.commit()
    await show(bot, user, f"{pe('ok')} Записано: " + ("мерчант дал реквизиты." if gave == "1" else
                                                     "мерчанту засчитан пропуск."), close_kb(), c)
    if gave != "1":
        await _tell_striked(bot, d.seller_id, d, count, sleep)


async def _deal_from_state(s, user, state) -> Deal | None:
    return await _mine(s, user, (await state.get_data()).get("g_deal", 0))


@router.message(OrderGive.number, F.text)
async def msg_number(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await _deal_from_state(s, user, state)
    if not d:
        await state.clear()
        return await show(bot, user, warn("Заявка уже не у вас"), close_kb())
    got, err = parse_requisites(m.text)
    if not got:
        return await give_screen(bot, s, user, d, state, note=warn(err))
    kind, number, bank, holder = got
    await state.update_data(g_kind=kind, g_bank=bank, g_number=number, g_holder=holder)
    await state.set_state(None)
    await _confirm_give(bot, s, user, d, state, await _default_minutes(s, user))


async def _default_minutes(s: AsyncSession, user: User) -> int:
    m = await s.get(OrderMerchant, user.id)  # an operator has none: the shortest allowed window
    choices = orders.pay_choices()
    return m.pay_minutes if m and m.pay_minutes in choices else choices[0]


async def _ask_minutes(bot, user, d: Deal, src=None):
    await show(bot, user, _give_head(d) + "Сколько времени дать покупателю на оплату?", kb(
        [btn(f"{mnt} мин", f"orq:t:{d.id}:{mnt}", "clock") for mnt in orders.pay_choices()[:3]],
        [btn(f"{mnt} мин", f"orq:t:{d.id}:{mnt}", "clock") for mnt in orders.pay_choices()[3:]] or None,
        back(f"orq:give:{d.id}", "Назад")), src)


@router.callback_query(F.data.regexp(r"^orq:tm:(\d+)$"))
async def cb_other_time(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    d = await _mine(s, user, int(c.data.split(":")[2]))
    if not d:
        return await c.answer("Заявка уже не у вас", show_alert=True)
    await _ask_minutes(bot, user, d, c)


@router.callback_query(F.data.regexp(r"^orq:t:(\d+):(\d+)$"))
async def cb_minutes(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, did, mnt = c.data.split(":")
    d = await _mine(s, user, int(did))
    if not d or not (await state.get_data()).get("g_number"):
        return await c.answer("Заявка уже не у вас", show_alert=True)
    await _confirm_give(bot, s, user, d, state, int(mnt), c)


async def _confirm_give(bot, s: AsyncSession, user: User, d: Deal, state: FSMContext, minutes: int, src=None):
    await state.update_data(g_minutes=minutes)
    data = await state.get_data()
    await show(bot, user, _give_head(d) + "\n".join([
        "Проверьте — покупатель переведёт именно сюда:",
        quote(f"{pe('bank')} {esc(data['g_bank'])} · {'СБП' if data['g_kind'] == 'sbp' else 'карта'}",
              f"{pe('key')} <code>{esc(data['g_number'])}</code>",
              f"{pe('profile')} {esc(data['g_holder'])}" if data["g_holder"] else "",
              f"{pe('clock')} На оплату: <b>{minutes} мин</b>"),
    ]), kb(btn("Выдать реквизиты", f"orq:ok:{d.id}", "ok", style="success"),
           [btn("Другое время", f"orq:tm:{d.id}", "clock"), btn("Другие реквизиты", f"orq:give:{d.id}", "pencil")],
           btn("Проблема с ордером", f"opq:pr:{d.id}", "warn") if d.status == "checking" and d.bybit_url
           else btn("Вернуть в поиск", f"opq:back:{d.id}", "refresh") if d.status == "checking"
           else btn("Отказаться", f"orq:drop:{d.id}", "cross")), src)


@router.callback_query(F.data.regexp(r"^orq:ok:(\d+)$"))
async def cb_give_ok(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    did = int(c.data.split(":")[2])
    data = await state.get_data()
    if data.get("g_deal") != did or not data.get("g_minutes"):
        return await c.answer("Заполните реквизиты заново", show_alert=True)
    try:
        d = await orders.give_requisites(s, did, user, data["g_kind"], data["g_bank"], data["g_number"],
                                         data["g_holder"], data["g_minutes"])
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)
        return await c.answer(str(e), show_alert=True)
    await state.clear()
    who = (f"Оператор {person(user)} выдал реквизиты" if d.via_bybit else f"Мерчант {person(user)} выдал реквизиты")
    deal_log(s, d, "requisites", f"{who}: {data['g_bank']} •• {data['g_number'][-4:]}, {data['g_holder'] or 'без ФИО'}, "
                                 f"оплата {data['g_minutes']} мин", notice=True)
    rating = None
    if d.via_bybit and d.operator_id == user.id:
        operators.log(s, user.id, d, "requisites", f"{data['g_bank']} •• {data['g_number'][-4:]}, "
                                                   f"оплата {data['g_minutes']} мин")
        if d.bybit_url and d.seller_id:
            rating = await _ask_rating(s, d, d.seller_id, user, gave=True)
    await s.commit()
    await deal_screen(bot, s, user, d, c, ok("Реквизиты выданы покупателю. Ждите чек — придёт уведомлением."))
    await push(bot, s, d.buyer_id, d, f"Реквизиты по заявке #{d.id} готовы — переведите {money.fmt(d.amount_rub)} ₽")
    if d.via_bybit:
        await push(bot, s, d.seller_id, d, f"Оператор выдал покупателю реквизиты вашего ордера по заявке #{d.id}")
    if rating is not None:  # last: the score is the operator's next step
        await _send_rating(bot, user, rating, d.seller_id, d)


@router.callback_query(F.data.regexp(r"^orq:drop:(\d+)$"))
async def cb_drop(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await _assigned(s, user, int(c.data.split(":")[2]))
    if not d:
        return await c.answer("Заявка уже не у вас", show_alert=True)
    await state.clear()
    bybit = d.via_bybit
    d = await orders.release(s, d.id)
    deal_log(s, d, "released", f"Мерчант {person(user)} отказался от заявки" + ("" if bybit else ", заморозка снята"),
             notice=True)
    await s.commit()
    await merchant_screen(bot, s, user, c, ok(f"Вы отказались от заявки #{d.id}" + ("." if bybit else
                                                                                  ", заморозка снята.")))
    await push(bot, s, d.buyer_id, d, f"Ищем другого мерчанта для заявки #{d.id}")
    await broadcast(bot, s, d)


# ---------- order merchant: application and console ----------

class OmForm(StatesGroup):
    source = State()
    speed = State()
    banks = State()
    about = State()


def _form(n: int, text: str, err: str = "") -> str:
    return f"{title(pe('pencil'), 'Анкета ордерного мерчанта')} · шаг {n} из 4\n\n{text}" + (warn(err) if err else "")


@router.callback_query(F.data == "om")
async def cb_merchant(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await merchant_screen(bot, s, user, c)


async def merchant_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    m = await s.get(OrderMerchant, user.id)
    if m is None or m.status == "rejected":
        wait = m is not None and now() - deals.aware(m.decided_at) < REAPPLY_AFTER
        lines = [title(pe("key"), "Ордерные реквизиты · для мерчантов"),
                 "Берите заявки покупателей под точную сумму и зарабатывайте на курсе.",
                 quote(f"Курс: <b>{money.fmt(settings.dec('order_rate'))} ₽</b> за USDT, без процентов",
                       "Заявки приходят все — берёте те, что подходят",
                       "Bybit-ордер — присылаете ссылку, баланс в боте не нужен",
                       "С баланса — замораживаем ваши USDT, реквизиты выдаёте сами",
                       f"На ответ по заявке — {settings.get('order_take_minutes')} мин")]
        if m:
            lines.append("Прошлая анкета отклонена" + (f": <i>{esc(m.reason)}</i>" if m.reason else "") + ".")
        lines.append(f"Подать снова можно после {at(deals.aware(m.decided_at) + REAPPLY_AFTER, 'dt')}." if wait
                     else f"{pe('info')} Анкета — 4 коротких шага, ответ обычно в течение суток. "
                          f"Подробно — в {manual('инструкции')}.")
        lines.append("Покупаете? Реквизиты под вашу сумму — в «RUB ⇄ USDT» → «Реквизиты под сумму».")
        return await show(bot, user, "\n".join(lines) + note, kb(
            None if wait else btn("Заполнить анкету", "om:apply", "pencil", style="success"),
            back("menu", "В меню")), src)
    if m.status == "pending":
        return await show(bot, user, "\n".join([
            title(pe("key"), "Ордерные реквизиты · анкета"),
            f"{pe('clock')} <b>На рассмотрении</b> с {at(m.created_at, 'dt')}. Ответ придёт в этот чат."]) + note,
            kb(back("menu", "В меню")), src)
    busy = await orders.open_rub(s, user.id)
    today = await deals.seller_stats(s, user.id, deals.day_start(), order=True)
    week = await deals.seller_stats(s, user.id, now() - timedelta(days=7), order=True)
    total = await deals.seller_stats(s, user.id, None, order=True)
    working = (await s.scalars(select(Deal).where(Deal.seller_id == user.id, Deal.is_order,
                                                  Deal.status.in_(deals.FUNDED)).order_by(Deal.id))).all()
    searching = (await s.scalars(select(Deal).where(Deal.status == "searching", Deal.buyer_id != user.id,
                                                    Deal.expires_at >= now()).order_by(Deal.id).limit(5))).all()
    sleeping = orders.asleep(m)
    active = m.status == "approved" and not sleeping
    cover = money.max_rub_fixed(user.balance, settings.dec("order_rate"))
    limit = settings.get("strike_limit")
    rep, rated = await orders.reputation(s, user.id)
    rep_limit = orders.bybit_problem(rep, Deal(amount_rub=settings.dec("order_max_rub")))
    await show(bot, user, "\n".join(x for x in [
        title(pe("key"), "Ордерный кабинет"),
        f"{pe('pause')} <b>Пауза до {at(m.sleep_until, 'dt')}</b>: {limit} раза подряд в ордере не было реквизитов"
        if sleeping else f"{pe('live')} <b>На линии: заявки приходят все</b>" if active
        else f"{pe('pause')} <b>Приостановлено администрацией</b>",
        "",
        section("percent", "Условия"),
        field("Курс", f"<b>{money.fmt(settings.dec('order_rate'))} ₽</b> за USDT, без процента"),
        field("Bybit-ордер", f"без баланса · ссылка за {settings.get('order_link_minutes')} мин после «Взять»"),
        field("С баланса", f"свободно <b>{money.usdt(user.balance)} USDT</b> — заявка до {money.fmt(cover)} ₽"),
        field("Пропуски реквизитов", f"<b>{m.strikes} из {limit}</b> подряд — потом пауза "
              f"{settings.human('strike_sleep_hours')}") if m.strikes and not sleeping else None,
        field("Репутация", orders.rep_line(rep, rated) + (f" — {rep_limit}" if rep_limit else "")),
        field("В работе", f"{money.fmt(busy)} ₽ · на оплату по умолчанию {m.pay_minutes} мин"),
        "",
        section("up", "Результаты"),
        field("Сегодня", f"{today['n']} · {money.fmt(today['rub'])} ₽ · <b>+{money.usdt(today['income'])} USDT</b>"),
        field("7 дней", f"{week['n']} · {money.fmt(week['rub'])} ₽ · +{money.usdt(week['income'])} USDT"),
        field("Всего", f"{total['n']} · {money.fmt(total['rub'])} ₽ · +{money.usdt(total['income'])} USDT"
              + (f" · успешных {total['success']}%" if total["success"] is not None else "")),
    ] if x is not None) + note, kb(
        *[btn(f"#{d.id} · {money.fmt(d.amount_rub)} ₽ · "
              + ({'assigned': 'прислать ссылку' if d.via_bybit else 'выдать реквизиты', 'checking': 'у оператора'}
                 .get(d.status, 'открыть')),
              f"dl:{d.id}", "fire", style="primary" if d.status == "assigned" else None) for d in working],
        *[btn(f"Свободна #{d.id} · {money.fmt(d.amount_rub)} ₽ · {money.usdt(d.seller_debit)} USDT", f"orq:see:{d.id}",
              "bell") for d in searching] if active else [],
        btn(f"Время на оплату · {m.pay_minutes} мин", "om:pay", "clock", wide=True),
        back("menu", "В меню"),
    ), src)


@router.callback_query(F.data.regexp(r"^orq:see:(\d+)$"))
async def cb_see(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    await take_screen(bot, s, user, int(c.data.split(":")[2]), c)


@router.callback_query(F.data == "om:pay")
async def cb_pay_default(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    m = await s.get(OrderMerchant, user.id)
    if not m or m.status != "approved":
        return await c.answer("Кабинет недоступен", show_alert=True)
    choices = orders.pay_choices()
    m.pay_minutes = choices[(choices.index(m.pay_minutes) + 1) % len(choices)] if m.pay_minutes in choices else choices[0]
    await merchant_screen(bot, s, user, c, ok(f"По умолчанию покупателю на оплату: {m.pay_minutes} мин. "
                                              "Меняется нажатием, в заявке можно выбрать другое."))


@router.callback_query(F.data == "om:apply")
async def cb_apply(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    m = await s.get(OrderMerchant, user.id)
    if m and (m.status in ("pending", "approved", "suspended")
              or now() - deals.aware(m.decided_at) < REAPPLY_AFTER):
        return await merchant_screen(bot, s, user, c)
    await state.set_state(OmForm.source)
    await state.set_data({})
    await show(bot, user, _form(1, "Откуда берёте реквизиты и ордера? Например: свои карты, команда, Bybit P2P — "
                                   "коротко, как есть."), kb(back("om", "Отмена")), c)


@router.message(OmForm.source, F.text)
async def f_source(m: Message, bot: Bot, user: User, state: FSMContext):
    v = " ".join(m.text.split())
    if not 3 <= len(v) <= 200:
        return await show(bot, user, _form(1, "Откуда берёте реквизиты?", "От 3 до 200 символов"), kb(back("om", "Отмена")))
    await state.update_data(source=v)
    await state.set_state(OmForm.speed)
    await show(bot, user, _form(2, "Как быстро выдаёте реквизиты или ссылку на ордер после заявки?"),
               kb(*[btn(x, f"om:sp:{i}", "clock") for i, x in enumerate(SPEED)], back("om", "Отмена")))


@router.callback_query(OmForm.speed, F.data.regexp(r"^om:sp:(\d)$"))
async def f_speed(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    i = int(c.data.split(":")[2])
    if i >= len(SPEED):
        return await c.answer()
    await state.update_data(speed=SPEED[i])
    await state.set_state(OmForm.banks)
    await show(bot, user, _form(3, "Реквизиты каких банков можете выдавать? Например: Сбер, Т-Банк, Альфа."),
               kb(back("om", "Отмена")), c)


@router.message(OmForm.banks, F.text)
async def f_banks(m: Message, bot: Bot, user: User, state: FSMContext):
    v = " ".join(m.text.split())
    if not 2 <= len(v) <= 200:
        return await show(bot, user, _form(3, "Какие банки:", "От 2 до 200 символов"), kb(back("om", "Отмена")))
    await state.update_data(banks=v)
    await state.set_state(OmForm.about)
    await show(bot, user, _form(4, "Опыт в P2P, объёмы, что ещё важно знать. Можно пропустить."),
               kb(btn("Пропустить", "om:skip", "next"), back("om", "Отмена")))


@router.callback_query(OmForm.about, F.data == "om:skip")
async def f_skip(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await _submit(bot, s, user, state, "", c)


@router.message(OmForm.about, F.text)
async def f_about(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    v = m.text.strip()
    if len(v) > 1000:
        return await show(bot, user, _form(4, "О себе:", "До 1000 символов"),
                          kb(btn("Пропустить", "om:skip", "next"), back("om", "Отмена")))
    await _submit(bot, s, user, state, v)


async def _submit(bot, s: AsyncSession, user: User, state: FSMContext, about: str, src=None):
    data = await state.get_data()
    await state.clear()
    if not data.get("banks"):
        return await merchant_screen(bot, s, user, src, warn("Анкета устарела — заполните заново."))
    m = await s.get(OrderMerchant, user.id)
    if m is None:
        m = OrderMerchant(user_id=user.id)
        s.add(m)
    m.status, m.source, m.speed, m.banks, m.about = "pending", data["source"], data["speed"], data["banks"], about
    m.created_at, m.decided_at, m.reason, m.admin_id = now(), None, None, None
    text = f"Анкета ордерного мерчанта: {m.source} · {m.speed} · банки: {m.banks}"
    events.add(s, f"om:{user.id}", "submitted", text, user.id, alert=True)
    await s.commit()
    await merchant_screen(bot, s, user, src, ok("Анкета отправлена. Обычно рассматриваем в течение суток."))
