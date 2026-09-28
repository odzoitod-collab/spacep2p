from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.handlers.deal import deal_screen, on_deal_created
from bot.handlers.seller import parse_rub, seller_menu
from bot.models import Card, User
from bot.services import deals, events, money, settings
from bot.ui import esc, quote, show, title, warn

router = Router()
PAGE = 6
KINDS = {None: "Любой", "card": "Карта", "sbp": "СБП"}


class Buy(StatesGroup):
    filter_amount = State()
    amount = State()


def rate_block() -> str:
    rate, pp = settings.dec("rate"), settings.dec("platform_pct")
    example = money.quote(Decimal(10000), rate, settings.dec("seller_pct"), pp).buyer_credit
    return quote(
        f"{pe('swap')} Курс: <b>1 USDT = {money.fmt(rate)} ₽</b> · комиссия {money.fmt(pp, 3)}%",
        f"{pe('dollar')} Например: 10 000 ₽ → <b>{money.usdt(example)} USDT</b> на баланс",
    )


@router.callback_query(F.data == "mk")
async def cb_market(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await cb_buy_first(c, bot, s, user, state)


async def cb_buy_first(c, bot, s, user, state):
    await state.set_state(None)
    await buy_list(bot, s, user, state, 0, c)


@router.callback_query(F.data == "mk:sell")
async def cb_sell(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    await seller_menu(bot, s, user, c)


def _filters(data: dict) -> tuple[Decimal | None, str | None, str | None]:
    amt = data.get("f_amount")
    return (Decimal(amt) if amt else None), data.get("f_bank"), data.get("f_kind")


async def buy_list(bot: Bot, s: AsyncSession, user: User, state: FSMContext, page: int, src=None, note: str = ""):
    amount, bank, kind = _filters(await state.get_data())
    active = await deals.open_deal_of(s, user.id)
    rows = await deals.market(s, user.id, amount, bank, kind)
    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = min(max(page, 0), pages - 1)
    chunk = rows[page * PAGE:(page + 1) * PAGE]
    stats = await deals.completed_count(s, [seller.id for _, seller, _, _ in chunk])
    filters = " · ".join([f"{money.fmt(amount)} ₽" if amount else "любая сумма",
                          esc(bank) if bank else "любой банк", KINDS[kind].lower() if kind else "карта и СБП"])
    has_filters = bool(amount or bank or kind)
    lines = [title(pe("down"), "RUB ⇄ USDT · покупка USDT"), "", rate_block(), "", f"{pe('filter')} Фильтр: {filters}", ""]
    own = await s.scalar(select(func.count(Card.id)).where(Card.user_id == user.id, Card.is_active, ~Card.is_deleted))
    if own:
        lines.append(f"{pe('info')} Ваши карты ({own}) здесь не показываются — покупатели видят их в своём списке. "
                     "Проверить видимость: «Продать USDT» → карта.")
    if active:
        lines.append(f"{pe('warn')} У вас открыта сделка #{active.id}. Новую можно создать после неё.")
    elif rows:
        lines.append(f"{pe('people')} Продавцов на смене: <b>{len(rows)}</b>. Кнопка: банк · суммы · сделок у продавца.")
    else:
        lines.append(f"{pe('clock')} Сейчас нет продавцов " + ("под этот фильтр. Сбросьте фильтр или обновите позже."
                                                             if has_filters else "на смене. Загляните через несколько минут."))
    nav = []
    if page > 0:
        nav.append(btn(f"{page}/{pages}", f"buy:{page - 1}", "prev"))
    if page < pages - 1:
        nav.append(btn(f"{page + 2}/{pages}", f"buy:{page + 1}", "next"))
    await show(bot, user, "\n".join(lines) + note, kb(
        btn(f"Открыть сделку #{active.id}", f"dl:{active.id}", "fire", style="primary") if active else None,
        *[btn(f"{card.bank} · {money.fmt(lo)}–{money.fmt(hi)} ₽ · {stats[seller.id]} сд.",
              f"bc:{card.id}", "sbp" if card.kind == "sbp" else "card") for card, seller, lo, hi in chunk],
        nav,
        btn(f"Запросить реквизиты на {money.fmt(amount)} ₽" if amount and not rows else "Реквизиты под сумму",
            "orb:new", "search", style="primary" if amount and not rows else None) if not active else None,
        [btn("Фильтр", "flt", "filter"), btn("Обновить", f"buy:{page}", "refresh")],
        btn("Сбросить фильтр", "flt:reset:list", "trash") if has_filters and not rows else None,
        back("menu", "В меню"),
    ), src)


@router.callback_query(F.data.regexp(r"^buy:(\d+)$"))
async def cb_buy(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await buy_list(bot, s, user, state, int(c.data.split(":")[1]), c)


# ---------- filters ----------

async def filter_screen(bot: Bot, s: AsyncSession, user: User, state: FSMContext, src=None, err: str = ""):
    amount, bank, kind = _filters(await state.get_data())
    await show(bot, user, "\n".join([
        title(pe("filter"), "Фильтр продавцов"),
        "",
        "Нажмите на параметр, чтобы изменить. Покажем только подходящих продавцов.",
    ]) + (warn(err) if err else ""), kb(
        btn(f"Сумма: {money.fmt(amount) + ' ₽' if amount else 'любая'}", "flt:amt", "ruble"),
        btn(f"Банк: {bank or 'любой'}", "flt:bank", "bank"),
        btn(f"Тип: {KINDS[kind]}", "flt:kind", "card"),
        [btn("Сбросить", "flt:reset", "trash"), btn("Показать продавцов", "buy:0", "search", style="success")],
    ), src)


@router.callback_query(F.data == "flt")
async def cb_flt(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await filter_screen(bot, s, user, state, c)


@router.callback_query(F.data.in_({"flt:reset", "flt:reset:list"}))
async def cb_flt_reset(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.update_data(f_amount=None, f_bank=None, f_kind=None)
    if c.data.endswith(":list"):
        return await buy_list(bot, s, user, state, 0, c)
    await filter_screen(bot, s, user, state, c)


@router.callback_query(F.data == "flt:kind")
async def cb_flt_kind(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    order = list(KINDS)
    cur = (await state.get_data()).get("f_kind")
    await state.update_data(f_kind=order[(order.index(cur) + 1) % len(order)])
    await filter_screen(bot, s, user, state, c)


@router.callback_query(F.data == "flt:amt")
async def cb_flt_amt(c: CallbackQuery, bot: Bot, user: User, state: FSMContext):
    await state.set_state(Buy.filter_amount)
    await show(bot, user, f"{title(pe('ruble'), 'Сумма для фильтра')}\n\nОтправьте сумму в рублях, которую хотите "
                          "перевести, например <code>5000</code>:",
               kb(btn("Любая сумма", "flt:amt0", "refresh"), back("flt")), c)


@router.callback_query(F.data == "flt:amt0")
async def cb_flt_amt0(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await state.set_state(None)
    await state.update_data(f_amount=None)
    await filter_screen(bot, s, user, state, c)


@router.message(Buy.filter_amount, F.text)
async def msg_flt_amt(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    v = parse_rub(m.text)
    await state.set_state(None)
    if v is None:
        return await filter_screen(bot, s, user, state, err="Сумма должна быть числом, например 5000")
    await state.update_data(f_amount=str(v))
    await buy_list(bot, s, user, state, 0)


@router.callback_query(F.data == "flt:bank")
async def cb_flt_bank(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    banks = sorted((await s.scalars(
        select(distinct(Card.bank)).where(Card.is_active, ~Card.is_banned, ~Card.is_deleted)
    )).all())[:30]
    await state.update_data(f_banks=banks)
    rows = [[btn(b, f"flt:b:{i + j}", "bank") for j, b in enumerate(banks[i:i + 2])] for i in range(0, len(banks), 2)]
    await show(bot, user, f"{title(pe('bank'), 'Банк')}\n\nВыберите банк, на который хотите переводить:",
               kb(btn("Любой банк", "flt:b:-", "refresh"), *rows, back("flt")), c)


@router.callback_query(F.data.startswith("flt:b:"))
async def cb_flt_bank_pick(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    idx = c.data.split(":")[2]
    banks = (await state.get_data()).get("f_banks") or []
    bank = banks[int(idx)] if idx.isdigit() and int(idx) < len(banks) else None
    await state.update_data(f_bank=bank)
    await filter_screen(bot, s, user, state, c)


# ---------- pick seller -> amount -> confirm -> deal ----------

async def _card_for_buyer(s: AsyncSession, user: User, card_id: int):
    for card, seller, lo, hi in await deals.market(s, user.id, None, None, None):
        if card.id == card_id:
            return card, seller, lo, hi
    return None


@router.callback_query(F.data.regexp(r"^bc:(\d+)$"))
async def cb_pick(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    if active := await deals.open_deal_of(s, user.id):
        return await c.answer(f"Сначала завершите сделку #{active.id}", show_alert=True)
    found = await _card_for_buyer(s, user, int(c.data.split(":")[1]))
    if not found:
        return await buy_list(bot, s, user, state, 0, c, warn("Этот продавец уже недоступен — выберите другого."))
    card, seller, lo, hi = found
    amount, *_ = _filters(await state.get_data())
    if amount and lo <= amount <= hi:
        return await confirm_screen(bot, s, user, card, amount, c)
    await state.set_state(Buy.amount)
    await state.update_data(card_id=card.id)
    await show(bot, user, amount_prompt(card, seller, lo, hi), kb(back("buy:0", "К продавцам")), c)


def amount_prompt(card: Card, seller: User, lo: Decimal, hi: Decimal, err: str = "") -> str:
    return "\n".join([
        title(pe("card"), f"{esc(card.bank)} · {'СБП' if card.kind == 'sbp' else 'карта'}"),
        "",
        quote(f"{pe('profile')} Продавец: <b>{esc(seller.name or 'продавец')}</b>",
              f"{pe('ruble')} Сумма сделки: <b>от {money.fmt(lo)} до {money.fmt(hi)} ₽</b>"),
        rate_block(),
        "",
        "Отправьте сумму в рублях, которую переведёте, например <code>" + money.fmt(lo).replace(" ", "") + "</code>:",
    ]) + (warn(err) if err else "")


@router.message(Buy.amount, F.text)
async def msg_amount(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    found = await _card_for_buyer(s, user, (await state.get_data()).get("card_id", 0))
    if not found:
        await state.set_state(None)
        return await buy_list(bot, s, user, state, 0, note=warn("Продавец ушёл — выберите другого."))
    card, seller, lo, hi = found
    v = parse_rub(m.text)
    if v is None or not lo <= v <= hi:
        err = "Нужно число" if v is None else f"Сумма вне лимитов продавца: {money.fmt(lo)} – {money.fmt(hi)} ₽"
        return await show(bot, user, amount_prompt(card, seller, lo, hi, err), kb(back("buy:0", "К продавцам")))
    await state.set_state(None)
    await confirm_screen(bot, s, user, card, v)


async def confirm_screen(bot: Bot, s: AsyncSession, user: User, card: Card, amount: Decimal, src=None, note: str = ""):
    rate, sp, pp = settings.dec("rate"), settings.dec("seller_pct"), settings.dec("platform_pct")
    q = money.quote(amount, rate, sp, pp)
    seller = await s.get(User, card.user_id)
    await show(bot, user, "\n".join([
        title(pe("doc"), "Проверьте условия"),
        "",
        quote(
            f"{pe('bank')} {esc(card.bank)} · {'СБП' if card.kind == 'sbp' else 'карта'} · {esc(seller.name or 'продавец')}",
            f"{pe('ruble')} Вы переводите: <b>{money.fmt(amount)} ₽</b>",
            f"{pe('swap')} По курсу {money.fmt(rate)} ₽: {money.usdt(q.usdt)} USDT",
            f"{pe('percent')} Комиссия {money.fmt(pp, 3)}%: −{money.usdt(q.usdt - q.buyer_credit)} USDT",
            f"{pe('dollar')} Вы получите: <b>{money.usdt(q.buyer_credit)} USDT</b>",
        ),
        "",
        f"{pe('clock')} После создания покажем реквизиты. На перевод и PDF-чек — "
        f"<b>{settings.get('deal_minutes')} мин</b>, потом сделка отменится.",
        "Переводите только после создания сделки и только на показанные реквизиты.",
    ]) + note, kb(
        btn("Создать сделку", f"bgo:{card.id}:{amount}:{q.buyer_credit}", "ok", style="success"),
        back("buy:0", "Отмена"),
    ), src)


@router.callback_query(F.data.regexp(r"^bgo:(\d+):([0-9]+(?:\.[0-9]{1,2})?)(?::([0-9]+(?:\.[0-9]{1,6})?))?$"))
async def cb_go(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    parts = c.data.split(":")
    card_id, amount = int(parts[1]), Decimal(parts[2])
    expect = Decimal(parts[3]) if len(parts) > 3 else None
    try:
        deal = await deals.create(s, user, card_id, amount, expect)
    except deals.DealError as e:
        await s.rollback()
        await s.refresh(user)  # rollback expires every loaded object
        if e.code == "terms" and (card := await s.get(Card, card_id)):
            return await confirm_screen(bot, s, user, card, amount, c, warn(str(e)))
        await c.answer(str(e), show_alert=True)
        return await buy_list(bot, s, user, state, 0, c, warn(str(e)))
    events.add(s, f"deal:{deal.id}", "created", f"Открыта: {money.fmt(deal.amount_rub)} ₽ → {money.usdt(deal.buyer_credit)} "
                                                f"USDT, покупатель {user.id}, продавец {deal.seller_id}, "
                                                f"заморожено {money.usdt(deal.seller_debit)} USDT", notice=True)
    await s.commit()
    await deal_screen(bot, s, user, deal, c)
    await on_deal_created(bot, s, deal)
