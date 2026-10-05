"""Buying USDT: «RUB ⇄ USDT» → the amount in rubles → the bot finds the requisites itself: the most reliable static
card that takes this amount, or, if there is none, a request for requisites under the exact amount that goes to the
order merchants (handlers/orders.py). No lists, no filters: one amount, one confirmation."""
from decimal import Decimal

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.emoji import back, btn, kb, pe
from bot.handlers.deal import deal_screen, on_deal_created
from bot.handlers.seller import parse_rub, seller_menu
from bot.models import Card, User
from bot.services import deals, events, money, settings
from bot.ui import doc, esc, person, quote, show, title, warn

router = Router()


class Buy(StatesGroup):
    amount = State()


def rate_block(user: User | None = None) -> str:
    """The buyer's terms: his personal ones if an admin set them, the general ones otherwise."""
    rate, pp = settings.buyer_terms(user)
    example = deals.buyer_preview(Decimal(10000), user).buyer_credit
    return quote(
        f"{pe('swap')} Курс: <b>1 USDT = {money.fmt(rate)} ₽</b> · комиссия {money.fmt(pp, 3)}%"
        + (" · ваши условия" if settings.has_terms(user) else ""),
        f"{pe('dollar')} Например: 10 000 ₽ → <b>{money.usdt(example)} USDT</b> на баланс",
    )


def order_range() -> tuple[Decimal, Decimal]:
    return settings.dec("order_min_rub"), settings.dec("order_max_rub")


@router.callback_query(F.data.in_({"mk", "orb:new"}) | F.data.regexp(r"^buy:\d+$"))
async def cb_buy(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    await buy_screen(bot, s, user, state, c)


@router.callback_query(F.data == "mk:sell")
async def cb_sell(c: CallbackQuery, bot: Bot, s: AsyncSession, user: User):
    await seller_menu(bot, s, user, c)


async def buy_screen(bot: Bot, s: AsyncSession, user: User, state: FSMContext, src=None, note: str = ""):
    active = await deals.open_deals_of(s, user.id)
    await state.set_state(Buy.amount)
    lo, hi = order_range()
    await show(bot, user, "\n".join([
        title(pe("down"), "RUB ⇄ USDT · покупка USDT"),
        rate_block(user),
        "<b>Отправьте сумму в рублях</b>, которую переведёте, например <code>10000</code>.",
        f"Бот сам подберёт реквизиты: готовую карту продавца или реквизиты под вашу сумму от ордерного мерчанта "
        f"(от {money.fmt(lo)} до {money.fmt(hi)} ₽). Подробнее — {doc('buy', 'как купить USDT')}.",
        f"{pe('info')} Открытых сделок: <b>{len(active)}</b> — можно вести несколько сразу." if active else "",
    ]) + note, kb(*[btn(f"Сделка #{d.id} · {money.fmt(d.amount_rub)} ₽", f"dl:{d.id}", "fire") for d in active[:3]],
                  back("menu", "В меню")), src)


@router.message(Buy.amount, F.text)
async def msg_amount(m: Message, bot: Bot, s: AsyncSession, user: User, state: FSMContext):
    v = parse_rub(m.text)
    if v is None:
        return await buy_screen(bot, s, user, state, note=warn("Нужна сумма числом, например 10000"))
    await offer_screen(bot, s, user, state, v)


async def pick_card(s: AsyncSession, user: User, amount: Decimal) -> Card | None:
    """The static card for this amount: the seller with more completed deals first."""
    offers = await deals.market(s, user.id, amount, None, None)
    if not offers:
        return None
    done = await deals.completed_count(s, list({seller.id for _, seller, _, _ in offers}))
    return min(offers, key=lambda o: (-done[o[1].id], o[0].id))[0]


async def offer_screen(bot: Bot, s: AsyncSession, user: User, state: FSMContext, amount: Decimal, src=None,
                       note: str = ""):
    """What the buyer gets and how — a card or a request — with one button to start."""
    card = await pick_card(s, user, amount)
    lo, hi = order_range()
    if card is None and not lo <= amount <= hi:
        return await buy_screen(bot, s, user, state, src, warn(
            f"Сейчас нет реквизитов на {money.fmt(amount)} ₽. Под точную сумму — от {money.fmt(lo)} до "
            f"{money.fmt(hi)} ₽. Отправьте другую сумму."))
    rate, pp = settings.buyer_terms(user)
    q = deals.buyer_preview(amount, user)
    await state.set_state(Buy.amount)  # typing another amount re-quotes right away
    await state.update_data(o_amount=str(amount), o_bank=None, o_credit=str(q.buyer_credit))
    if card is not None:
        how = [f"{pe('card')} <b>Готовая карта продавца</b> · {esc(card.bank)} · "
               f"{'СБП' if card.kind == 'sbp' else 'карта'}",
               f"Реквизиты появятся после создания сделки. На перевод и PDF-чек — <b>{settings.get('deal_minutes')} мин</b>."]
        go = btn("Создать сделку", f"bgo:{card.id}:{amount}:{q.buyer_credit}", "ok", style="success")
    else:
        how = [f"{pe('search')} <b>Реквизиты под вашу сумму</b> — выдаст ордерный мерчант",
               f"Ищем до {settings.get('order_search_minutes')} мин, реквизиты придут уведомлением. На оплату — не "
               f"меньше {settings.get('order_pay_minutes')} мин."]
        go = btn("Создать заявку", "orb:go", "ok", style="success")
    await show(bot, user, "\n".join([
        title(pe("doc"), "Проверьте покупку"),
        quote(f"• Вы переводите: <b>{money.fmt(amount)} ₽</b>",
              f"• По курсу {money.fmt(rate)} ₽: {money.usdt(q.usdt)} USDT",
              f"• Комиссия {money.fmt(pp, 3)}%: −{money.usdt(q.usdt - q.buyer_credit)} USDT"
              + (" · ваши условия" if settings.has_terms(user) else ""),
              f"• Вы получите: <b>{money.usdt(q.buyer_credit)} USDT</b>"),
        *how,
        "Переводите только после создания и только на показанные реквизиты. Другая сумма — просто отправьте её.",
    ]) + note, kb(go, back("menu", "Отмена")), src)


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
        await c.answer(str(e), show_alert=True)
        # the card was taken meanwhile or the terms changed: quote again (another card or a request)
        return await offer_screen(bot, s, user, state, amount, c, warn(str(e)))
    await state.set_state(None)
    seller = await s.get(User, deal.seller_id)
    events.add(s, f"deal:{deal.id}", "created", f"Открыта: {money.fmt(deal.amount_rub)} ₽ → {money.usdt(deal.buyer_credit)} "
                                                f"USDT, создал покупатель {person(user)}, карта продавца "
                                                f"{person(seller)}, заморожено {money.usdt(deal.seller_debit)} USDT",
               notice=True)
    await s.commit()
    await deal_screen(bot, s, user, deal, c)
    await on_deal_created(bot, s, deal)

