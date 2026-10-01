"""Order requisites in the bot: the buyer's request, order merchants (application, console, taking a request,
giving requisites or a Bybit order link), operators (checking a Bybit order and giving its requisites) and the
broadcast of requests. Money and state transitions live in services/orders.py."""
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
from bot.handlers.seller import BANKS, check_bank, check_holder, check_requisites, mask, parse_rub
from bot.models import Card, Deal, OrderMerchant, OrderOffer, User, now
from bot.services import deals, events, money, orders, settings
from bot.ui import at, close_kb, esc, manual, notify, ok, quote, show, title, warn

router = Router()
REAPPLY_AFTER = timedelta(hours=24)
SPEED = ["до 5 минут", "5–10 минут", "10–15 минут"]


# ---------- broadcast to merchants ----------

def offer_text(d: Deal, m: OrderMerchant) -> str:
    bybit = m.mode == "bybit"
    return "\n".join([
        f"{pe('bell')} <b>Ордерная заявка #{d.id} · {money.fmt(d.amount_rub)} ₽</b>",
        quote(f"Перевод из: <b>{esc(d.sender_bank or 'банк не указан')}</b>",
              f"Ордер: <b>{money.usdt(d.seller_debit)} USDT</b> по {money.fmt(d.merchant_rate)} ₽",
              "Через Bybit-ордер — баланс не нужен" if bybit else
              f"Заморозится: <b>{money.usdt(d.seller_debit)} USDT</b>",
              f"Взять до {at(d.expires_at)} · на {'ссылку' if bybit else 'реквизиты'} "
              f"{settings.get('order_take_minutes')} мин"),
        f"{pe('info')} Кто первым нажмёт «Взять», тот и работает заявку.",
    ])


async def broadcast(bot: Bot, s: AsyncSession, d: Deal, first: bool = True) -> int:
    """Send the request to every merchant who can take it now and has not seen it. Commits.
    The most reliable merchants (more completed orders) get it first; first=False: a periodic re-send to merchants
    who became available later — quiet if nobody new."""
    merchants = await orders.eligible(s, d)
    done = await deals.completed_count(s, [u.id for _, u in merchants])
    sent = 0
    for om, u in sorted(merchants, key=lambda mu: -done[mu[1].id]):
        m = await notify(bot, u.id, offer_text(d, om), kb(btn("Взять заявку", f"orq:take:{d.id}", "fire", style="success"),
                                                     back("x", "Скрыть", "cross")), silent=u.quiet)
        if m is not None:
            s.add(OrderOffer(deal_id=d.id, user_id=u.id, msg_id=m.message_id))
            sent += 1
    if sent or first:
        deal_log(s, d, "offered", f"Заявка на {money.fmt(d.amount_rub)} ₽ разослана мерчантам: {sent}"
                 + ("" if first else " (досыл)"), notice=True)
    if not sent and first:
        await events.alert_once(s, f"deal:{d.id}", "no_merchants", f"Ордерная заявка на {money.fmt(d.amount_rub)} ₽: "
                                "нет свободных ордерных мерчантов под сумму", d.buyer_id)
    await s.commit()
    return sent


async def close_offers(bot: Bot, s: AsyncSession, d: Deal, text: str, keep: int | None = None) -> None:
    """Remove the "Take" button from every merchant's copy of this request (except `keep`)."""
    for uid, mid in await orders.forget_offers(s, d.id):
        if uid == keep:
            continue
        with suppress(TelegramAPIError):
            await bot.edit_message_text(chat_id=uid, message_id=mid, text=text, reply_markup=close_kb())
    await s.commit()


# ---------- buyer: request requisites for an exact amount ----------

class OrderBuy(StatesGroup):
    amount = State()
    bank = State()


def _buy_step(n: int, text: str, err: str = "") -> str:
    return f"{title(pe('search'), 'Реквизиты под вашу сумму')} · шаг {n} из 3\n\n{text}" + (warn(err) if err else "")


def _range() -> str:
    return f"от {money.fmt(settings.dec('order_min_rub'))} до {money.fmt(settings.dec('order_max_rub'))} ₽"


@router.callback_query(F.data == "orb:new")
async def cb_request(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    if active := await deals.open_deal_of(s, user.id):
        return await c.answer(f"Сначала завершите сделку #{active.id}", show_alert=True)
    prefill = (await state.get_data()).get("f_amount")
    await state.set_state(OrderBuy.amount)
    if prefill and settings.dec("order_min_rub") <= Decimal(prefill) <= settings.dec("order_max_rub"):
        return await _ask_bank(bot, user, state, Decimal(prefill), c)
    await show(bot, user, _buy_step(1, "Нет готовой карты на нужную сумму? Ордерный мерчант выдаст реквизиты "
                                       f"специально под ваш перевод.\n\nОтправьте точную сумму перевода в рублях, {_range()}:"),
               kb(back("buy:0", "Отмена")), c)


@router.message(OrderBuy.amount, F.text)
async def msg_request_amount(m: Message, bot: Bot, user: User, state: FSMContext):
    v = parse_rub(m.text)
    if v is None or not settings.dec("order_min_rub") <= v <= settings.dec("order_max_rub"):
        return await show(bot, user, _buy_step(1, "Отправьте точную сумму перевода в рублях:", f"Нужна сумма {_range()}"),
                          kb(back("buy:0", "Отмена")))
    await _ask_bank(bot, user, state, v)


async def _ask_bank(bot, user, state: FSMContext, amount: Decimal, src=None):
    await state.update_data(o_amount=str(amount))
    await state.set_state(OrderBuy.bank)
    rows = [[btn(b, f"orb:b:{i + j}", "bank") for j, b in enumerate(BANKS[i:i + 2])] for i in range(0, len(BANKS), 2)]
    await show(bot, user, _buy_step(2, f"Сумма: <b>{money.fmt(amount)} ₽</b>\n\nС какого банка будете переводить? "
                                       "Выберите или напишите название — мерчант подберёт реквизиты под него."),
               kb(*rows, back("buy:0", "Отмена")), src)


@router.callback_query(OrderBuy.bank, F.data.regexp(r"^orb:b:(\d+)$"))
async def cb_request_bank(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    i = int(c.data.split(":")[2])
    if i >= len(BANKS):
        return await c.answer()
    await _confirm(bot, user, state, BANKS[i], c)


@router.message(OrderBuy.bank, F.text)
async def msg_request_bank(m: Message, bot: Bot, user: User, state: FSMContext):
    bank = check_bank(m.text)
    if not bank:
        data = await state.get_data()
        return await _ask_bank(bot, user, state, Decimal(data["o_amount"]))
    await _confirm(bot, user, state, bank)


async def _confirm(bot, user, state: FSMContext, bank: str, src=None):
    data = await state.get_data()
    amount = Decimal(data["o_amount"])
    q = money.quote_fixed(amount, settings.dec("rate"), settings.dec("order_rate"), settings.dec("platform_pct"))
    await state.set_state(None)
    await state.update_data(o_bank=bank, o_credit=str(q.buyer_credit))
    await show(bot, user, "\n".join([
        _buy_step(3, "Проверьте заявку:"),
        quote(f"{pe('ruble')} Вы переводите: <b>{money.fmt(amount)} ₽</b> из <b>{esc(bank)}</b>",
              f"{pe('swap')} Курс {money.fmt(settings.dec('rate'))} ₽ · комиссия {money.fmt(settings.dec('platform_pct'), 3)}%",
              f"{pe('dollar')} Вы получите: <b>{money.usdt(q.buyer_credit)} USDT</b>"),
        f"{pe('clock')} Ищем мерчанта до {settings.get('order_search_minutes')} мин. Реквизиты придут уведомлением, "
        f"на оплату — не меньше {settings.get('order_pay_minutes')} мин. До реквизитов ничего не переводите.",
    ]), kb(btn("Создать заявку", "orb:go", "ok", style="success"), back("buy:0", "Отмена")), src)


@router.callback_query(F.data == "orb:go")
async def cb_request_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    data = await state.get_data()
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
    deal_log(s, d, "created", f"Заявка на ордерные реквизиты: {money.fmt(d.amount_rub)} ₽ из {d.sender_bank} → "
                              f"{money.usdt(d.buyer_credit)} USDT, покупатель {user.id}", notice=True)
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
    await close_offers(bot, s, d, f"Заявка #{d.id} отменена покупателем")
    if d.seller_id:
        await notify(bot, d.seller_id, f"{pe('warn')} Покупатель отменил заявку #{d.id}." + (
            " Отмените ордер на Bybit, если уже создали." if d.via_bybit else
            f" Заморозка {money.usdt(d.seller_debit)} USDT снята."))
    if d.via_bybit and d.operator_id:
        await notify(bot, d.operator_id, f"{pe('warn')} Заявка #{d.id} отменена покупателем — не выдавайте "
                                         "реквизиты, отмените ордер на Bybit.")


# ---------- merchant: take a request and give requisites ----------

class OrderGive(StatesGroup):
    bank = State()
    number = State()
    holder = State()
    link = State()


def link_line(d: Deal) -> str:
    return f'<a href="{esc(d.bybit_url)}">{esc(d.bybit_url[:60])}</a>' if d.bybit_url else "—"


def _give_head(d: Deal) -> str:
    bybit = (f"{pe('shop')} Bybit-ордер: {link_line(d)} · зайти на <b>{money.usdt(d.seller_debit)} USDT</b> по "
             f"{money.fmt(d.merchant_rate)} ₽\n") if d.status == "checking" else ""
    return (f"{title(pe('key'), f'Реквизиты для заявки #{d.id}')}\n"
            f"<b>{money.fmt(d.amount_rub)} ₽</b> · перевод из {esc(d.sender_bank or 'банк не указан')} · "
            f"выдать до {at(d.expires_at)}\n{bybit}\n")


async def _assigned(s: AsyncSession, user: User, did: int) -> Deal | None:
    """The request this merchant took and has not answered yet (requisites or a Bybit link)."""
    d = await s.get(Deal, did, populate_existing=True)
    return d if d and d.status == "assigned" and d.seller_id == user.id else None


async def _mine(s: AsyncSession, user: User, did: int) -> Deal | None:
    """The request whose requisites this user gives now: his own (balance mode) or a Bybit order he checks."""
    d = await s.get(Deal, did, populate_existing=True)
    return d if orders.giver(d, user.id) else None


@router.callback_query(F.data.regexp(r"^orq:take:(\d+)$"))
async def cb_take(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    did = int(c.data.split(":")[2])
    try:
        d = await orders.take(s, did, user)
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)
        await c.answer(str(e), show_alert=True)
        if e.code == "taken" and c.message:
            with suppress(TelegramAPIError):
                await c.message.edit_text(f"Заявка #{did} уже взята или закрыта", reply_markup=close_kb())
        return
    deal_log(s, d, "taken", f"Мерчант {user.id} взял заявку, " + (
        "работает через Bybit-ордер" if d.via_bybit else f"заморожено {money.usdt(d.seller_debit)} USDT"), notice=True)
    await s.commit()
    await close_offers(bot, s, d, f"Заявку #{d.id} взял другой мерчант", keep=user.id)
    await push(bot, s, d.buyer_id, d, f"Мерчант взял заявку #{d.id} и готовит реквизиты")
    await (link_screen if d.via_bybit else give_screen)(bot, s, user, d, state, c)


async def give_screen(bot: Bot, s: AsyncSession, user: User, d: Deal, state: FSMContext, src=None, note: str = ""):
    await state.set_state(None)
    await state.update_data(g_deal=d.id)
    templates = await orders.last_requisites(s, user.id)
    operator = d.status == "checking"
    await show(bot, user, _give_head(d) + ("Откройте ордер, зайдите на указанную сумму и выдайте покупателю "
                                           "реквизиты из ордера:" if operator else
                                           "Выдайте реквизиты, на которые покупатель переведёт рубли:") + note, kb(
        *[btn(f"{t.bank} {mask(t)} · {t.holder[:18]}", f"orq:tpl:{d.id}:{t.id}", "refresh", style="primary")
          for t in templates],
        [btn("Новая карта", f"orq:k:{d.id}:card", "card"), btn("Новый СБП", f"orq:k:{d.id}:sbp", "sbp")],
        btn("Отклонить ссылку", f"opq:rj:{d.id}", "cross") if operator else btn("Отказаться", f"orq:drop:{d.id}", "cross"),
    ), src)


async def link_screen(bot: Bot, s: AsyncSession, user: User, d: Deal, state: FSMContext, src=None, note: str = ""):
    """Bybit mode: the merchant sends the link to his Bybit P2P order instead of requisites."""
    await state.set_state(OrderGive.link)
    await state.update_data(g_deal=d.id)
    await show(bot, user, "\n".join([
        title(pe("shop"), f"Bybit-ордер для заявки #{d.id}"),
        f"<b>{money.fmt(d.amount_rub)} ₽</b> · перевод из {esc(d.sender_bank or 'банк не указан')} · "
        f"ссылка до {at(d.expires_at)}",
        "",
        quote(f"{pe('ruble')} Сумма ордера: <b>{money.fmt(d.amount_rub)} ₽</b>",
              f"{pe('swap')} Курс: <b>{money.fmt(d.merchant_rate)} ₽</b> за USDT",
              f"{pe('dollar')} Оператор зайдёт на: <b>{money.usdt(d.seller_debit)} USDT</b>"),
        "Пришлите сюда ссылку на ордер Bybit P2P под эту сумму. Оператор Strait Pay проверит ордер, зайдёт в него "
        "и выдаст покупателю реквизиты. Баланс в боте не нужен — USDT уходят через Bybit.",
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
    deal_log(s, d, "link", f"Мерчант {user.id} прислал Bybit-ордер: {url}", notice=True)
    await s.commit()
    await deal_screen(bot, s, user, d, note=ok("Ссылка у оператора. Он проверит ордер и выдаст покупателю реквизиты."))
    await push(bot, s, d.buyer_id, d, f"Ордер по заявке #{d.id} найден — оператор проверяет его и выдаёт реквизиты")
    await notify_operators(bot, s, d, user)


# ---------- operator: check a Bybit order and give its requisites ----------

def operator_text(d: Deal, merchant: User | None, head: str = "Bybit-ордер на проверку") -> str:
    return "\n".join([
        f"{pe('bell')} <b>{head} · заявка #{d.id}</b>",
        "",
        quote(f"{pe('shop')} Ордер: {link_line(d)}",
              f"{pe('ruble')} Сумма ордера: <b>{money.fmt(d.amount_rub)} ₽</b>",
              f"{pe('swap')} Курс ордерного мерчанта: <b>{money.fmt(d.merchant_rate)} ₽</b> за USDT",
              f"{pe('dollar')} Зайти на: <b>{money.usdt(d.seller_debit)} USDT</b> "
              f"({money.fmt(d.amount_rub)} ₽ / {money.fmt(d.merchant_rate)})",
              f"{pe('bank')} Покупатель переводит из: {esc(d.sender_bank or 'банк не указан')}",
              f"{pe('profile')} Мерчант: {esc(merchant.name or '—') if merchant else '—'} "
              f"(<code>{d.seller_id}</code>)",
              f"{pe('clock')} Выдать реквизиты до {at(d.expires_at)}"),
        "Сверьте сумму и курс в ордере, зайдите на указанную сумму и выдайте покупателю реквизиты из ордера. "
        "Неверный ордер — «Отклонить ссылку», мерчант пришлёт другую.",
    ])


def operator_kb(d: Deal):
    return kb(btn("Выдать реквизиты", f"opq:go:{d.id}", "key", style="success"),
              btn("Отклонить ссылку", f"opq:rj:{d.id}", "cross", style="danger"),
              back("x", "Скрыть", "cross"))


async def notify_operators(bot: Bot, s: AsyncSession, d: Deal, merchant: User | None) -> None:
    """Every operator gets the order; the first one who presses «Выдать реквизиты» owns it. Commits."""
    got = 0
    for oid in config.operators:
        who = await s.get(User, oid)
        if await notify(bot, oid, operator_text(d, merchant), operator_kb(d), silent=bool(who and who.quiet)):
            got += 1
    if not got:
        deal_log(s, d, "notify_failed", f"Ни один оператор не получил Bybit-ордер по заявке #{d.id}", alert=True)
        await s.commit()


def _operator(c: CallbackQuery) -> bool:
    return c.from_user.id in config.operators


@router.callback_query(F.data.regexp(r"^opq:go:(\d+)$"))
async def cb_operator_take(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    if not _operator(c):
        return await c.answer("Только для операторов", show_alert=True)
    try:
        d = await orders.claim(s, int(c.data.split(":")[2]), user)
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)
        return await c.answer(str(e), show_alert=True)
    deal_log(s, d, "operator", f"Оператор {user.id} проверяет Bybit-ордер", notice=True)
    await s.commit()
    await give_screen(bot, s, user, d, state, c)


@router.callback_query(F.data.regexp(r"^opq:rj:(\d+)$"))
async def cb_operator_reject(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    if not _operator(c):
        return await c.answer("Только для операторов", show_alert=True)
    d = await orders.reject_link(s, int(c.data.split(":")[2]), user)
    if d is None:
        return await c.answer("Ордер уже обработан другим оператором или заявка закрыта", show_alert=True)
    await state.clear()
    deal_log(s, d, "link_rejected", f"Оператор {user.id} отклонил Bybit-ордер, мерчант пришлёт другой", notice=True)
    await s.commit()
    await show(bot, user, f"{pe('ok')} Ссылка по заявке #{d.id} отклонена — мерчант пришлёт другую до {at(d.expires_at)}.",
               close_kb(), c)
    await notify(bot, d.seller_id, f"{pe('warn')} <b>Оператор отклонил ссылку на ордер по заявке #{d.id}.</b> "
                                   f"Проверьте сумму ({money.fmt(d.amount_rub)} ₽ = {money.usdt(d.seller_debit)} USDT "
                                   f"по {money.fmt(d.merchant_rate)} ₽) и пришлите другую до {at(d.expires_at)}.",
                 kb(btn("Прислать ссылку", f"orq:give:{d.id}", "shop", style="success"),
                    btn("Отказаться", f"orq:drop:{d.id}", "cross")))


@router.callback_query(F.data.regexp(r"^orq:tpl:(\d+):(\d+)$"))
async def cb_template(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, did, cid = c.data.split(":")
    d = await _mine(s, user, int(did))
    t = await s.get(Card, int(cid))
    if not d or not t or t.user_id != user.id:
        return await c.answer("Заявка уже не у вас", show_alert=True)
    await state.update_data(g_kind=t.kind, g_bank=t.bank, g_number=t.requisites, g_holder=t.holder)
    await _confirm_give(bot, s, user, d, state, await _default_minutes(s, user), c)


@router.callback_query(F.data.regexp(r"^orq:k:(\d+):(card|sbp)$"))
async def cb_kind(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    _, _, did, kind = c.data.split(":")
    d = await _mine(s, user, int(did))
    if not d:
        return await c.answer("Заявка уже не у вас", show_alert=True)
    await state.update_data(g_kind=kind)
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
           btn("Отклонить ссылку", f"opq:rj:{d.id}", "cross") if d.status == "checking"
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
    who = f"Оператор {user.id} выдал реквизиты Bybit-ордера" if d.via_bybit else f"Мерчант {user.id} выдал реквизиты"
    deal_log(s, d, "requisites", f"{who} {data['g_bank']}, оплата {data['g_minutes']} мин", notice=True)
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
    deal_log(s, d, "released", f"Мерчант {user.id} отказался от заявки" + ("" if bybit else ", заморозка снята"),
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
    min = State()
    max = State()
    open = State()
    banks = State()
    about = State()


class OmEdit(StatesGroup):
    value = State()


LIMIT_FIELDS = {"min": ("Минимальная заявка", "min_rub"), "max": ("Максимальная заявка", "max_rub"),
                "open": ("В работе одновременно", "max_open_rub")}


def _form(n: int, text: str, err: str = "") -> str:
    return f"{title(pe('pencil'), 'Анкета ордерного мерчанта')} · шаг {n} из 7\n\n{text}" + (warn(err) if err else "")


@router.callback_query(F.data == "om")
async def cb_merchant(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.clear()
    await merchant_screen(bot, s, user, c)


async def merchant_screen(bot: Bot, s: AsyncSession, user: User, src=None, note: str = ""):
    m = await s.get(OrderMerchant, user.id)
    if m is None or m.status == "rejected":
        wait = m is not None and now() - deals.aware(m.decided_at) < REAPPLY_AFTER
        lines = [title(pe("key"), "Ордерные реквизиты · для мерчантов"),
                 "Выдавайте реквизиты под точную сумму покупателя и зарабатывайте на курсе.",
                 quote(f"Курс: <b>{money.fmt(settings.dec('order_rate'))} ₽</b> за USDT, без процентов",
                       "Bybit-ордер — присылаете ссылку, баланс в боте не нужен",
                       "Баланс — под заявку замораживаются ваши USDT, реквизиты выдаёте сами",
                       f"На ответ по заявке — {settings.get('order_take_minutes')} мин")]
        if m:
            lines.append("Прошлая анкета отклонена" + (f": <i>{esc(m.reason)}</i>" if m.reason else "") + ".")
        lines.append(f"Подать снова можно после {at(deals.aware(m.decided_at) + REAPPLY_AFTER, 'dt')}." if wait
                     else f"{pe('info')} Анкета — 7 коротких шагов, ответ обычно в течение суток. "
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
    bybit = m.mode == "bybit"
    cap = min(m.max_rub, m.max_open_rub - busy)
    if not bybit:
        cap = min(cap, money.max_rub_fixed(user.balance, settings.dec("order_rate")))
    today = await deals.seller_stats(s, user.id, deals.day_start(), order=True)
    week = await deals.seller_stats(s, user.id, now() - timedelta(days=7), order=True)
    total = await deals.seller_stats(s, user.id, None, order=True)
    working = (await s.scalars(select(Deal).where(Deal.seller_id == user.id, Deal.is_order,
                                                  Deal.status.in_(deals.FUNDED)).order_by(Deal.id))).all()
    active = m.status == "approved"
    status = (f"{pe('pause')} <b>Приостановлено администрацией</b>" if not active
              else f"{pe('live')} <b>Принимаю заявки</b>" if m.accepting else f"{pe('pause')} <b>Заявки не принимаю</b>")
    hint = ("Сейчас можете взять заявку до <b>" + money.fmt(max(cap, Decimal(0))) + " ₽</b>" if cap >= m.min_rub
            else f"{pe('warn')} Заявки не придут: " + ("лимит «в работе» занят" if bybit
                                                     else "мало баланса или лимит занят"))
    await show(bot, user, "\n".join([
        title(pe("key"), "Ордерный кабинет"),
        status,
        "<b>Условия</b>",
        quote(f"Режим: <b>{'Bybit-ордер' if bybit else 'баланс'}</b>"
              + (" — ссылка на ордер, баланс не нужен" if bybit else " — замораживаем USDT, реквизиты ваши"),
              f"Курс: <b>{money.fmt(settings.dec('order_rate'))} ₽</b> за USDT",
              f"Заявка: {money.fmt(m.min_rub)}–{money.fmt(m.max_rub)} ₽ · оплата {m.pay_minutes} мин",
              f"В работе: {money.fmt(busy)} из {money.fmt(m.max_open_rub)} ₽"
              + ("" if bybit else f" · свободно {money.usdt(user.balance)} USDT")),
        hint,
        "<b>Результаты</b>",
        quote(f"Сегодня: <b>{today['n']}</b> · {money.fmt(today['rub'])} ₽ · <b>+{money.usdt(today['income'])} USDT</b>",
              f"7 дней: <b>{week['n']}</b> · {money.fmt(week['rub'])} ₽ · <b>+{money.usdt(week['income'])} USDT</b>",
              f"Всего: {total['n']} · {money.fmt(total['rub'])} ₽ · +{money.usdt(total['income'])} USDT"
              + (f" · успешных {total['success']}%" if total["success"] is not None else "")),
        f"{pe('info')} «В работе» — сколько рублей держите одновременно: учтите внешний баланс (биржа).",
    ]) + note, kb(
        *[btn(f"#{d.id} · {money.fmt(d.amount_rub)} ₽ · "
              + ({'assigned': 'прислать ссылку' if d.via_bybit else 'выдать реквизиты', 'checking': 'у оператора'}
                 .get(d.status, 'открыть')),
              f"dl:{d.id}", "fire", style="primary" if d.status == "assigned" else None) for d in working],
        (btn("Заявки: принимаю · выключить", "om:acc:0", "pause", wide=True) if m.accepting
         else btn("Заявки: не принимаю · включить", "om:acc:1", "live", style="success")) if active else None,
        btn(f"Режим: {'Bybit-ордер' if bybit else 'баланс'} · сменить", "om:mode",
            "lock" if bybit else "shop", wide=True) if active else None,
        [btn("Мин. заявка", "om:set:min", "down"), btn("Макс. заявка", "om:set:max", "up")],
        [btn("В работе", "om:set:open", "filter"), btn(f"Оплата {m.pay_minutes} мин", "om:pay", "clock")],
        back("menu", "В меню"),
    ), src)


@router.callback_query(F.data.regexp(r"^om:acc:([01])$"))
async def cb_accepting(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    m = await s.get(OrderMerchant, user.id)
    if not m or m.status != "approved":
        return await c.answer("Кабинет недоступен", show_alert=True)
    m.accepting = c.data.endswith("1")
    await merchant_screen(bot, s, user, c, ok("Заявки будут приходить в этот чат" if m.accepting
                                               else "Новые заявки приходить не будут. Взятые — завершите."))


@router.callback_query(F.data == "om:mode")
async def cb_mode(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    m = await s.get(OrderMerchant, user.id)
    if not m or m.status != "approved":
        return await c.answer("Кабинет недоступен", show_alert=True)
    m.mode = "balance" if m.mode == "bybit" else "bybit"
    events.add(s, f"om:{user.id}", "mode", f"Режим ордерного мерчанта: {orders.MODES[m.mode]}", user.id, notice=True)
    await merchant_screen(bot, s, user, c, ok(
        "Новые заявки — через Bybit-ордер: баланс не нужен." if m.mode == "bybit" else
        "Новые заявки — с баланса: под каждую замораживаются ваши USDT. Взятые заявки доработают как начаты."))


@router.callback_query(F.data == "om:pay")
async def cb_pay_default(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    m = await s.get(OrderMerchant, user.id)
    if not m or m.status != "approved":
        return await c.answer("Кабинет недоступен", show_alert=True)
    choices = orders.pay_choices()
    m.pay_minutes = choices[(choices.index(m.pay_minutes) + 1) % len(choices)] if m.pay_minutes in choices else choices[0]
    await merchant_screen(bot, s, user, c, ok(f"По умолчанию покупателю на оплату: {m.pay_minutes} мин. "
                                              "Меняется нажатием, в заявке можно выбрать другое."))


@router.callback_query(F.data.regexp(r"^om:set:(min|max|open)$"))
async def cb_limit(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    field = c.data.split(":")[2]
    m = await s.get(OrderMerchant, user.id)
    if not m or m.status != "approved":
        return await c.answer("Кабинет недоступен", show_alert=True)
    await state.set_state(OmEdit.value)
    await state.update_data(om_field=field)
    name, attr = LIMIT_FIELDS[field]
    await show(bot, user, f"{title(pe('pencil'), name)}\n\nСейчас: <b>{money.fmt(getattr(m, attr))} ₽</b>\n\n"
                          "Отправьте новое значение в рублях:", kb(back("om", "Отмена")), c)


@router.message(OmEdit.value, F.text)
async def msg_limit(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    field = (await state.get_data()).get("om_field")
    om = await s.get(OrderMerchant, user.id)
    v = parse_rub(m.text)
    name, attr = LIMIT_FIELDS[field]
    lo = v if field == "min" else om.min_rub
    hi = v if field == "max" else om.max_rub
    if v is None or lo > hi or (field == "open" and v < om.min_rub):
        return await show(bot, user, f"{title(pe('pencil'), name)}\n\nОтправьте значение ещё раз."
                          + warn("Число в рублях; минимум не больше максимума; в работе — не меньше минимальной заявки"),
                          kb(back("om", "Отмена")))
    await state.clear()
    setattr(om, attr, v)
    await merchant_screen(bot, s, user, note=ok(f"{name}: {money.fmt(v)} ₽"))


@router.callback_query(F.data == "om:apply")
async def cb_apply(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    m = await s.get(OrderMerchant, user.id)
    if m and (m.status in ("pending", "approved", "suspended")
              or now() - deals.aware(m.decided_at) < REAPPLY_AFTER):
        return await merchant_screen(bot, s, user, c)
    await state.set_state(OmForm.source)
    await state.set_data({})
    await show(bot, user, _form(1, "Откуда берёте реквизиты и заявки? Например: свои карты, команда, партнёры — "
                                   "коротко, как есть."), kb(back("om", "Отмена")), c)


@router.message(OmForm.source, F.text)
async def f_source(m: Message, bot: Bot, user: User, state: FSMContext):
    v = " ".join(m.text.split())
    if not 3 <= len(v) <= 200:
        return await show(bot, user, _form(1, "Откуда берёте реквизиты?", "От 3 до 200 символов"), kb(back("om", "Отмена")))
    await state.update_data(source=v)
    await state.set_state(OmForm.speed)
    await show(bot, user, _form(2, "Как быстро выдаёте реквизиты после заявки?"),
               kb(*[btn(x, f"om:sp:{i}", "clock") for i, x in enumerate(SPEED)], back("om", "Отмена")))


@router.callback_query(OmForm.speed, F.data.regexp(r"^om:sp:(\d)$"))
async def f_speed(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    i = int(c.data.split(":")[2])
    if i >= len(SPEED):
        return await c.answer()
    await state.update_data(speed=SPEED[i])
    await state.set_state(OmForm.min)
    await show(bot, user, _form(3, "Минимальная сумма заявки, ₽ (например, <code>5000</code>):"),
               kb(back("om", "Отмена")), c)


@router.message(OmForm.min, F.text)
async def f_min(m: Message, bot: Bot, user: User, state: FSMContext):
    v = parse_rub(m.text)
    if v is None:
        return await show(bot, user, _form(3, "Минимальная сумма заявки, ₽:", "Нужно число"), kb(back("om", "Отмена")))
    await state.update_data(min=str(v))
    await state.set_state(OmForm.max)
    await show(bot, user, _form(4, f"Минимум: <b>{money.fmt(v)} ₽</b>\n\nМаксимальная сумма одной заявки, ₽:"),
               kb(back("om", "Отмена")))


@router.message(OmForm.max, F.text)
async def f_max(m: Message, bot: Bot, user: User, state: FSMContext):
    lo, v = Decimal((await state.get_data())["min"]), parse_rub(m.text)
    if v is None or v < lo:
        return await show(bot, user, _form(4, "Максимальная сумма заявки, ₽:", f"Не меньше {money.fmt(lo)} ₽"),
                          kb(back("om", "Отмена")))
    await state.update_data(max=str(v))
    await state.set_state(OmForm.open)
    await show(bot, user, _form(5, "Сколько рублей вы готовы держать в работе одновременно? Учтите свой внешний "
                                   "баланс (например, на бирже). Больше этой суммы заявок не придёт."),
               kb(back("om", "Отмена")))


@router.message(OmForm.open, F.text)
async def f_open(m: Message, bot: Bot, user: User, state: FSMContext):
    data = await state.get_data()
    v = parse_rub(m.text)
    if v is None or v < Decimal(data["max"]):
        return await show(bot, user, _form(5, "Сколько держать в работе одновременно, ₽:",
                                           f"Не меньше максимальной заявки — {money.fmt(Decimal(data['max']))} ₽"),
                          kb(back("om", "Отмена")))
    await state.update_data(open=str(v))
    await state.set_state(OmForm.banks)
    await show(bot, user, _form(6, "Реквизиты каких банков можете выдавать? Например: Сбер, Т-Банк, Альфа."),
               kb(back("om", "Отмена")))


@router.message(OmForm.banks, F.text)
async def f_banks(m: Message, bot: Bot, user: User, state: FSMContext):
    v = " ".join(m.text.split())
    if not 2 <= len(v) <= 200:
        return await show(bot, user, _form(6, "Какие банки:", "От 2 до 200 символов"), kb(back("om", "Отмена")))
    await state.update_data(banks=v)
    await state.set_state(OmForm.about)
    await show(bot, user, _form(7, "Опыт в P2P, объёмы, что ещё важно знать. Можно пропустить."),
               kb(btn("Пропустить", "om:skip", "next"), back("om", "Отмена")))


@router.callback_query(OmForm.about, F.data == "om:skip")
async def f_skip(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await _submit(bot, s, user, state, "", c)


@router.message(OmForm.about, F.text)
async def f_about(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    v = m.text.strip()
    if len(v) > 1000:
        return await show(bot, user, _form(7, "О себе:", "До 1000 символов"),
                          kb(btn("Пропустить", "om:skip", "next"), back("om", "Отмена")))
    await _submit(bot, s, user, state, v)


async def _submit(bot, s: AsyncSession, user: User, state: FSMContext, about: str, src=None):
    data = await state.get_data()
    await state.clear()
    m = await s.get(OrderMerchant, user.id)
    if m is None:
        m = OrderMerchant(user_id=user.id)
        s.add(m)
    m.status, m.source, m.speed, m.banks, m.about = "pending", data["source"], data["speed"], data["banks"], about
    m.min_rub, m.max_rub, m.max_open_rub = Decimal(data["min"]), Decimal(data["max"]), Decimal(data["open"])
    m.accepting, m.created_at, m.decided_at, m.reason, m.admin_id = False, now(), None, None, None
    text = (f"Анкета ордерного мерчанта: {m.source} · {m.speed} · {money.fmt(m.min_rub)}–{money.fmt(m.max_rub)} ₽, "
            f"в работе до {money.fmt(m.max_open_rub)} ₽ · банки: {m.banks}")
    events.add(s, f"om:{user.id}", "submitted", text, user.id, alert=True)
    await s.commit()
    for aid in config.admin_ids:  # also straight to the admins' chats with the bot
        await notify(bot, aid, f"{pe('key')} <b>Новая анкета ордерного мерчанта</b>\n{esc(user.name or '—')} "
                               f"(<code>{user.id}</code>)\n{esc(text)}",
                     kb(btn("Открыть анкету", f"aom:{user.id}", "search", style="primary"), back("x", "Скрыть", "cross")))
    await merchant_screen(bot, s, user, src, ok("Анкета отправлена. Обычно рассматриваем в течение суток."))
