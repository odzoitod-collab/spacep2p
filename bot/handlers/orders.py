"""Order requisites in the bot: the buyer's request, order merchants (application, console, taking a request by a
Bybit order or from the balance, giving requisites or the order link), operators (accepting a Bybit order, giving its
requisites) and the broadcast of requests — to every merchant and into the community and team chats.
Money and state transitions live in services/orders.py."""
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

from bot.config import config
from bot.emoji import back, btn, kb, pe
from bot.handlers.deal import deal_screen, log as deal_log, push
from bot.handlers.seller import BANKS, check_bank, check_holder, check_requisites, mask
from bot.models import Card, Deal, OrderMerchant, OrderOffer, Team, User, now
from bot.services import deals, events, money, operators, orders, settings
from bot.ui import (at, clean, close_kb, deep_link, esc, manual, notify, ok, person, quote, safe_text, show, title,
                    warn)

router = Router()
REAPPLY_AFTER = timedelta(hours=24)
SPEED = ["до 5 минут", "5–10 минут", "10–15 минут"]
CLOSED = {"taken": "уже взята", "cancelled": "отменена покупателем", "expired": "закрыта: время поиска вышло",
          "void": "закрыта администрацией"}


# ---------- the request as merchants and chats see it ----------

def income(d: Deal) -> Decimal:
    """What the merchant earns on a request: the RUB at the service rate minus the USDT he gives."""
    return d.amount_rub / d.rate - d.seller_debit


def request_facts(d: Deal) -> list[str]:
    return [f"• Сумма перевода: <b>{money.fmt(d.amount_rub)} ₽</b>",
            f"• Банк покупателя: <b>{esc(d.sender_bank)}</b>" if d.sender_bank else "",
            f"• Курс ордера: <b>{money.fmt(d.merchant_rate)} ₽</b> за 1 USDT",
            f"• Ордер: <b>{money.usdt(d.seller_debit)} USDT</b> = {money.fmt(d.amount_rub)} ₽ / "
            f"{money.fmt(d.merchant_rate)}",
            f"• По курсу сервиса {money.fmt(d.rate)} ₽: {money.usdt(d.amount_rub / d.rate)} USDT → доход "
            f"мерчанта ≈ <b>+{money.usdt(income(d))} USDT</b>",
            f"• Взять до {at(d.expires_at)} · на ответ {settings.get('order_take_minutes')} мин"]


def offer_text(d: Deal, u: User) -> str:
    covered = u.balance >= d.seller_debit
    return "\n".join([
        f"{pe('bell')} <b>Заявка #{d.id} · {money.fmt(d.amount_rub)} ₽ · {money.usdt(d.seller_debit)} USDT</b>",
        *filter(None, request_facts(d)),
        "",
        "<b>Как работать — выберите кнопкой</b>",
        "• Bybit-ордер: баланс не нужен — пришлёте ссылку на ордер, оператор выдаст реквизиты",
        f"• С баланса: заморозим {money.usdt(d.seller_debit)} USDT, реквизиты выдаёте сами "
        f"(свободно {money.usdt(u.balance)} USDT" + ("" if covered else " — не хватает") + ")",
        "Кто первым нажмёт «Взять», тот и работает заявку.",
    ])


def offer_kb(d: Deal, u: User):
    return kb(btn("Взять · Bybit-ордер", f"orq:take:{d.id}:b", "fire", style="success"),
              btn("Взять · с баланса", f"orq:take:{d.id}:w", "wallet", style="primary")
              if u.balance >= d.seller_debit else None,
              back("x", "Скрыть", "cross"))


def chat_text(d: Deal) -> str:
    return "\n".join([
        f"{pe('bell')} <b>Новая заявка #{d.id} · {money.fmt(d.amount_rub)} ₽</b>",
        *filter(None, request_facts(d)),
        "",
        "Взять — кнопкой ниже: в боте выберете Bybit-ордер (без баланса) или работу с баланса бота.",
    ])


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
        m = await notify(bot, u.id, offer_text(d, u), offer_kb(d, u), silent=u.quiet)
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
            markup = kb(btn("Взять заявку в боте", url=await deep_link(bot, payload), icon="fire", style="success"))
            m = await safe_text(lambda t: bot.send_message(chat, t, reply_markup=markup, disable_web_page_preview=True),
                                clean(chat_text(d)))
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
                       kinds: tuple[str, ...] = ("merchant", "chat", "operator")) -> None:
    """Remove the buttons from every copy of this request (merchants, chats, operators), except `keep`'s."""
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
    bank = State()
    number = State()
    holder = State()
    link = State()


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
    await show(bot, user, "\n".join(lines) + note, offer_kb(d, user), src)


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
    await state.set_state(None)
    await state.update_data(g_deal=d.id)
    templates = await orders.last_requisites(s, user.id)
    operator = d.status == "checking"
    await show(bot, user, _give_head(d) + ("Выдайте покупателю свои реквизиты — рубли придут на них, USDT покупателю "
                                           "зачислит площадка:" if operator and not d.bybit_url else
                                           "Откройте ордер по ссылке, сверьте сумму и курс, зайдите в него и выдайте "
                                           "покупателю реквизиты из ордера:" if operator else
                                           "Выдайте реквизиты, на которые покупатель переведёт рубли:") + note, kb(
        *[btn(f"{t.bank} {mask(t)} · {t.holder[:18]}", f"orq:tpl:{d.id}:{t.id}", "refresh", style="primary")
          for t in templates],
        [btn("Новая карта", f"orq:k:{d.id}:card", "card"), btn("Новый СБП", f"orq:k:{d.id}:sbp", "sbp")],
        btn("Открыть ордер Bybit", url=d.bybit_url, icon="shop") if operator and d.bybit_url else None,
        btn("Вернуть в поиск", f"opq:back:{d.id}", "refresh") if operator and not d.bybit_url else
        [btn("Отклонить ссылку", f"opq:rj:{d.id}", "cross"), btn("Вернуть другим", f"opq:back:{d.id}", "refresh")]
        if operator else btn("Отказаться", f"orq:drop:{d.id}", "cross"),
    ), src)


async def link_screen(bot: Bot, s: AsyncSession, user: User, d: Deal, state: FSMContext, src=None, note: str = ""):
    """Bybit order: the merchant sends the link to his Bybit P2P order instead of requisites."""
    await state.set_state(OrderGive.link)
    await state.update_data(g_deal=d.id)
    await show(bot, user, "\n".join([
        title(pe("shop"), f"Bybit-ордер для заявки #{d.id}"),
        f"• Ссылка до {at(d.expires_at)}" + (f" · перевод из {esc(d.sender_bank)}" if d.sender_bank else ""),
        "",
        quote(f"{pe('ruble')} Сумма ордера: <b>{money.fmt(d.amount_rub)} ₽</b>",
              f"{pe('swap')} Курс: <b>{money.fmt(d.merchant_rate)} ₽</b> за USDT",
              f"{pe('dollar')} Оператор зайдёт на: <b>{money.usdt(d.seller_debit)} USDT</b>",
              f"{pe('up')} Ваш доход: ≈ +{money.usdt(income(d))} USDT"),
        "1. Создайте на Bybit P2P ордер на продажу ровно на эту сумму и курс.",
        "2. Пришлите сюда ссылку на ордер.",
        "3. Оператор Strait Pay примет ордер, зайдёт в него и выдаст покупателю реквизиты — баланс в боте не нужен.",
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

def operator_offer_text(d: Deal, merchant: User | None) -> str:
    return "\n".join(line for line in [
        f"{pe('bell')} <b>Bybit-ордер · заявка #{d.id} · {money.usdt(d.seller_debit)} USDT</b>",
        f"• Сумма ордера: <b>{money.fmt(d.amount_rub)} ₽</b>",
        f"• Курс мерчанта: <b>{money.fmt(d.merchant_rate)} ₽</b> за 1 USDT",
        f"• Зайти на: <b>{money.usdt(d.seller_debit)} USDT</b>",
        f"• Покупатель переводит из: {esc(d.sender_bank)}" if d.sender_bank else "",
        f"• Мерчант: {esc(merchant.name or '—') if merchant else '—'} (<code>{d.seller_id}</code>)",
        f"• Принять до {at(d.expires_at)}",
        "",
        "Нажмите «Принять ордер» — ссылка на ордер придёт вам, у остальных операторов заявка пропадёт. "
        "Принятый ордер становится вашим долгом после подтверждения оплаты.",
    ] if line is not None)


async def notify_operators(bot: Bot, s: AsyncSession, d: Deal, merchant: User | None) -> None:
    """Every operator gets «Принять ордер»; the first one who accepts gets the link, the others' messages close.
    Commits."""
    got = 0
    for oid in await operators.ids(s):
        if oid == d.seller_id:
            continue  # an operator never checks his own order
        who = await s.get(User, oid)
        m = await notify(bot, oid, operator_offer_text(d, merchant),
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


@router.callback_query(F.data.regexp(r"^orq:tpl:(\d+):(\d+)$"))
async def cb_template(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, did, cid = c.data.split(":")
    d = await _mine(s, user, int(did))
    t = await s.get(Card, int(cid))
    if not d or not t or t.user_id != user.id:
        return await c.answer("Заявка уже не у вас", show_alert=True)
    await state.update_data(g_deal=d.id, g_kind=t.kind, g_bank=t.bank, g_number=t.requisites, g_holder=t.holder)
    await _confirm_give(bot, s, user, d, state, await _default_minutes(s, user), c)


@router.callback_query(F.data.regexp(r"^orq:k:(\d+):(card|sbp)$"))
async def cb_kind(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, did, kind = c.data.split(":")
    d = await _mine(s, user, int(did))
    if not d:
        return await c.answer("Заявка уже не у вас", show_alert=True)
    await state.update_data(g_deal=d.id, g_kind=kind)
    await state.set_state(OrderGive.bank)
    rows = [[btn(b, f"orq:b:{d.id}:{i + j}", "bank") for j, b in enumerate(BANKS[i:i + 2])]
            for i in range(0, len(BANKS), 2)]
    await show(bot, user, _give_head(d) + "Банк получателя — выберите или напишите:",
               kb(*rows, back(f"orq:give:{d.id}", "Назад")), c)


async def _ask_number(bot, user, state: FSMContext, d: Deal, bank: str, src=None, err: str = ""):
    await state.update_data(g_bank=bank)
    await state.set_state(OrderGive.number)
    kind = (await state.get_data())["g_kind"]
    await show(bot, user, _give_head(d) + f"Банк: <b>{esc(bank)}</b>\n\n" + (
        "Номер карты (16–19 цифр):" if kind == "card" else "Телефон для СБП (+7…):") + (warn(err) if err else ""),
        kb(back(f"orq:give:{d.id}", "Назад")), src)


@router.callback_query(OrderGive.bank, F.data.regexp(r"^orq:b:(\d+):(\d+)$"))
async def cb_bank(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, did, i = c.data.split(":")
    d = await _mine(s, user, int(did))
    if not d or int(i) >= len(BANKS):
        return await c.answer("Заявка уже не у вас", show_alert=True)
    await _ask_number(bot, user, state, d, BANKS[int(i)], c)


async def _deal_from_state(s, user, state) -> Deal | None:
    return await _mine(s, user, (await state.get_data()).get("g_deal", 0))


@router.message(OrderGive.bank, F.text)
async def msg_bank(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await _deal_from_state(s, user, state)
    if not d:
        await state.clear()
        return await show(bot, user, warn("Заявка уже не у вас"), close_kb())
    bank = check_bank(m.text)
    if not bank:
        return await show(bot, user, _give_head(d) + "Банк получателя:" + warn("Название от 2 до 40 символов"),
                          kb(back(f"orq:give:{d.id}", "Назад")))
    await _ask_number(bot, user, state, d, bank)


@router.message(OrderGive.number, F.text)
async def msg_number(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await _deal_from_state(s, user, state)
    if not d:
        await state.clear()
        return await show(bot, user, warn("Заявка уже не у вас"), close_kb())
    data = await state.get_data()
    value, err = check_requisites(data["g_kind"], m.text)
    if not value:
        return await _ask_number(bot, user, state, d, data["g_bank"], err=err)
    await state.update_data(g_number=value)
    await state.set_state(OrderGive.holder)
    await show(bot, user, _give_head(d) + "ФИО получателя так, как его увидит покупатель при переводе "
                                          "(например, <code>Иван Иванович И.</code>):",
               kb(back(f"orq:give:{d.id}", "Назад")))


@router.message(OrderGive.holder, F.text)
async def msg_holder(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    d = await _deal_from_state(s, user, state)
    if not d:
        await state.clear()
        return await show(bot, user, warn("Заявка уже не у вас"), close_kb())
    fio = check_holder(m.text)
    if not fio:
        return await show(bot, user, _give_head(d) + "ФИО получателя:" + warn("Буквами, минимум 2 слова"),
                          kb(back(f"orq:give:{d.id}", "Назад")))
    await state.update_data(g_holder=fio)
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
              f"{pe('profile')} {esc(data['g_holder'])}",
              f"{pe('clock')} На оплату: <b>{minutes} мин</b>"),
    ]), kb(btn("Выдать реквизиты", f"orq:ok:{d.id}", "ok", style="success"),
           [btn("Другое время", f"orq:tm:{d.id}", "clock"), btn("Другие реквизиты", f"orq:give:{d.id}", "pencil")],
           btn("Отклонить ссылку", f"opq:rj:{d.id}", "cross") if d.status == "checking" and d.bybit_url
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
    deal_log(s, d, "requisites", f"{who}: {data['g_bank']} •• {data['g_number'][-4:]}, {data['g_holder']}, "
                                 f"оплата {data['g_minutes']} мин", notice=True)
    await s.commit()
    await deal_screen(bot, s, user, d, c, ok("Реквизиты выданы покупателю. Ждите чек — придёт уведомлением."))
    await push(bot, s, d.buyer_id, d, f"Реквизиты по заявке #{d.id} готовы — переведите {money.fmt(d.amount_rub)} ₽")
    if d.via_bybit:
        await push(bot, s, d.seller_id, d, f"Оператор выдал покупателю реквизиты вашего ордера по заявке #{d.id}")


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
    active = m.status == "approved"
    cover = money.max_rub_fixed(user.balance, settings.dec("order_rate"))
    await show(bot, user, "\n".join([
        title(pe("key"), "Ордерный кабинет"),
        f"{pe('live')} <b>На линии: заявки приходят все</b>" if active
        else f"{pe('pause')} <b>Приостановлено администрацией</b>",
        "<b>Условия</b>",
        quote(f"• Курс: <b>{money.fmt(settings.dec('order_rate'))} ₽</b> за USDT, без процента",
              "• Bybit-ордер: без баланса, на любую сумму",
              f"• С баланса: свободно <b>{money.usdt(user.balance)} USDT</b> — хватит на заявку до "
              f"{money.fmt(cover)} ₽",
              f"• В работе сейчас: {money.fmt(busy)} ₽ · по умолчанию на оплату {m.pay_minutes} мин"),
        "<b>Результаты</b>",
        quote(f"• Сегодня: <b>{today['n']}</b> · {money.fmt(today['rub'])} ₽ · <b>+{money.usdt(today['income'])} USDT</b>",
              f"• 7 дней: <b>{week['n']}</b> · {money.fmt(week['rub'])} ₽ · <b>+{money.usdt(week['income'])} USDT</b>",
              f"• Всего: {total['n']} · {money.fmt(total['rub'])} ₽ · +{money.usdt(total['income'])} USDT"
              + (f" · успешных {total['success']}%" if total["success"] is not None else "")),
        f"{pe('info')} Свободные заявки — кнопками ниже; новые приходят сюда сообщением с кнопкой «Взять»."
        if active and searching else "",
    ]) + note, kb(
        *[btn(f"#{d.id} · {money.fmt(d.amount_rub)} ₽ · "
              + ({'assigned': 'прислать ссылку' if d.via_bybit else 'выдать реквизиты', 'checking': 'у оператора'}
                 .get(d.status, 'открыть')),
              f"dl:{d.id}", "fire", style="primary" if d.status == "assigned" else None) for d in working],
        *[btn(f"Свободна #{d.id} · {money.fmt(d.amount_rub)} ₽ · {money.usdt(d.seller_debit)} USDT", f"orq:see:{d.id}",
              "bell") for d in searching] if active else [],
        btn(f"Оплата по умолчанию: {m.pay_minutes} мин", "om:pay", "clock", wide=True),
        btn("Обновить", "om", "refresh"),
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
    for aid in config.admin_ids:  # also straight to the admins' chats with the bot
        await notify(bot, aid, "\n".join([f"{pe('key')} <b>Новая анкета ордерного мерчанта</b>",
                                          f"• Кто: {esc(user.name or '—')} @{esc(user.username or '—')} "
                                          f"(<code>{user.id}</code>)",
                                          f"• Откуда реквизиты: {esc(m.source)}", f"• Скорость: {esc(m.speed)}",
                                          f"• Банки: {esc(m.banks)}"]),
                     kb(btn("Открыть анкету", f"aom:{user.id}", "search", style="primary"), back("x", "Скрыть", "cross")))
    await merchant_screen(bot, s, user, src, ok("Анкета отправлена. Обычно рассматриваем в течение суток."))
