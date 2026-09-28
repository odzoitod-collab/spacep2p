"""Order requisites: when no static card fits, a buyer (or an API client) requests requisites for an exact amount.

    searching --merchant takes--> assigned --gives requisites--> waiting_payment -> (usual deal: receipt,
        |                            |                             confirmation, dispute, expiry)
        |                            +--declines / time is up--> searching (other merchants)
        +--nobody within order_search_minutes / buyer cancels--> cancelled

The deal's terms (rate, fees, USDT amounts) are fixed when the request is created. Funds: the merchant's
seller_debit is frozen at the moment he takes the request (so a taken request is always covered) and released if
he declines or runs out of time. While searching/assigned, expires_at is the search / requisites deadline.
"""
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Card, Deal, OrderMerchant, OrderOffer, User, now
from bot.services import deals, money, settings
from bot.services.deals import DealError

PAY_MINUTES = (15, 20, 30, 45, 60)  # payment windows a merchant can give; the setting sets the minimum


def pay_choices() -> list[int]:
    low = settings.num("order_pay_minutes")
    return [m for m in PAY_MINUTES if m >= low] or [low]


async def create_request(s: AsyncSession, buyer: User, amount_rub: Decimal, sender_bank: str | None,
                         expect_credit: Decimal | None = None, client=None, external_id: str | None = None) -> Deal:
    """A request for requisites. Terms are quoted now; nothing is frozen until a merchant takes it."""
    if not amount_rub.is_finite() or amount_rub <= 0 or amount_rub.as_tuple().exponent < -2:
        raise DealError("Некорректная сумма", "invalid_amount")
    lo, hi = settings.dec("order_min_rub"), settings.dec("order_max_rub")
    if not lo <= amount_rub <= hi:
        raise DealError(f"Реквизиты под сумму: от {money.fmt(lo)} до {money.fmt(hi)} ₽", "order_range")
    await money.lock(s, buyer.id)
    await deals.check_buyer(s, buyer, amount_rub, client)
    # order merchants earn their own (lower) percent; the buyer pays the same platform fee as with a static card
    rate, sp, pp = settings.dec("rate"), settings.dec("order_seller_pct"), settings.dec("platform_pct")
    try:
        qt = money.quote(amount_rub, rate, sp, pp)
    except ValueError:
        raise DealError("Покупки временно недоступны: некорректные настройки комиссий. Напишите в поддержку.")
    if expect_credit is not None and qt.buyer_credit != expect_credit:
        raise DealError("Курс или комиссия изменились. Проверьте новую сумму.", "terms")
    d = Deal(buyer_id=buyer.id, seller_id=None, card_id=None, amount_rub=amount_rub, rate=rate, seller_pct=sp,
             platform_pct=pp, seller_debit=qt.seller_debit, buyer_credit=qt.buyer_credit, platform_fee=qt.platform_fee,
             status="searching", is_order=True, sender_bank=(sender_bank or None) and sender_bank[:40],
             expires_at=now() + timedelta(minutes=settings.num("order_search_minutes")),
             api_client_id=client.id if client is not None else None, external_id=external_id)
    s.add(d)
    await s.flush()
    return d


def income(d: Deal) -> Decimal:
    """What the merchant earns on this deal, USDT."""
    return (d.amount_rub / d.rate - d.seller_debit).quantize(money.Q)


async def open_rub(s: AsyncSession, uid: int) -> Decimal:
    """RUB in the merchant's order deals that are still open."""
    return Decimal(await s.scalar(select(func.coalesce(func.sum(Deal.amount_rub), 0)).where(
        Deal.seller_id == uid, Deal.is_order, Deal.status.in_(deals.FUNDED))))


def debit_for(d: Deal, u: User) -> tuple[Decimal, Decimal]:
    """(percent, USDT to freeze) of this request for this merchant: his personal or the general order percent."""
    pct = settings.merchant_pct(u, True)
    return pct, money.seller_debit(d.amount_rub, d.rate, pct)


def fit_problem(m: OrderMerchant, u: User, d: Deal, busy_rub: Decimal) -> str:
    """Why this merchant cannot take this request now ("" = he can)."""
    if m.status != "approved":
        return "доступ ордерного мерчанта не активен"
    if u.is_banned:
        return "аккаунт заблокирован"
    if not m.min_rub <= d.amount_rub <= m.max_rub:
        return f"сумма вне ваших настроек {money.fmt(m.min_rub)}–{money.fmt(m.max_rub)} ₽"
    if busy_rub + d.amount_rub > m.max_open_rub:
        return (f"лимит одновременной работы {money.fmt(m.max_open_rub)} ₽: уже в работе "
                f"{money.fmt(busy_rub)} ₽")
    need = debit_for(d, u)[1]
    if u.balance < need:
        return f"нужно {money.usdt(need)} USDT свободного баланса, у вас {money.usdt(u.balance)}"
    return ""


async def eligible(s: AsyncSession, d: Deal) -> list[User]:
    """Merchants who accept requests now and can cover this one and have not been offered it yet (a live offer or
    a decline both count). Not the buyer."""
    declined = set((await s.scalars(select(OrderOffer.user_id).where(OrderOffer.deal_id == d.id))).all())
    rows = (await s.execute(select(OrderMerchant, User).join(User, User.id == OrderMerchant.user_id).where(
        OrderMerchant.status == "approved", OrderMerchant.accepting))).all()
    out = []
    for m, u in rows:
        if u.id == d.buyer_id or u.id in declined:
            continue
        if not fit_problem(m, u, d, await open_rub(s, u.id)):
            out.append(u)
    return out


async def take(s: AsyncSession, deal_id: int, merchant: User) -> Deal:
    """First merchant wins: the deal row is locked, the status checked, his funds frozen. Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "searching" or deals.aware(d.expires_at) < now():
        raise DealError("Заявку уже взял другой мерчант или она закрыта", "taken")
    if d.buyer_id == merchant.id:
        raise DealError("Это ваша собственная заявка")
    m = await s.get(OrderMerchant, merchant.id)
    u = await money.lock(s, merchant.id)
    if m is None or (problem := fit_problem(m, u, d, await open_rub(s, u.id))):
        raise DealError(f"Не можете взять заявку: {problem or 'вы не ордерный мерчант'}", "cannot")
    pct, _ = debit_for(d, u)  # the taker's own percent; the buyer's side (credit, platform_pct) does not change
    qt = money.quote(d.amount_rub, d.rate, pct, d.platform_pct)
    d.seller_pct, d.seller_debit, d.platform_fee = pct, qt.seller_debit, qt.seller_debit - d.buyer_credit
    await money.freeze(s, u.id, d.seller_debit, f"deal:{d.id}")
    moved = await deals._move(s, d.id, ("searching",), "assigned", seller_id=u.id,
                              expires_at=now() + timedelta(minutes=settings.num("order_take_minutes")))
    if moved is None:  # unreachable with the row lock, kept as a guard
        raise DealError("Заявку уже взял другой мерчант", "taken")
    return moved


async def give_requisites(s: AsyncSession, deal_id: int, merchant: User, kind: str, bank: str, number: str,
                          holder: str, minutes: int) -> Deal:
    """The merchant's requisites for this deal: an unlisted one-off card; the buyer's payment timer starts."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "assigned" or d.seller_id != merchant.id:
        raise DealError("Заявка уже не у вас", "not_yours")
    if deals.aware(d.expires_at) < now():
        raise DealError("Время на выдачу реквизитов вышло — заявка передана другим мерчантам", "late")
    if minutes not in pay_choices():
        raise DealError("Недопустимое время на оплату")
    card = Card(user_id=merchant.id, kind=kind, bank=bank, requisites=number, holder=holder, min_rub=d.amount_rub,
                max_rub=d.amount_rub, is_active=False, is_deleted=True)  # never listed in the market
    s.add(card)
    await s.flush()
    return await deals._move(s, d.id, ("assigned",), "waiting_payment", card_id=card.id,
                             expires_at=now() + timedelta(minutes=minutes))


async def release(s: AsyncSession, deal_id: int, why: str = "declined") -> Deal | None:
    """Assigned -> searching again: the merchant declined or ran out of time. His funds are unfrozen and he is not
    offered this request again. Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "assigned":
        return None
    merchant = d.seller_id
    await money.unfreeze(s, merchant, d.seller_debit, f"deal:{d.id}")
    s.add(OrderOffer(deal_id=d.id, user_id=merchant, declined=True))
    res = await deals._move(s, d.id, ("assigned",), "searching", seller_id=None,
                            expires_at=now() + timedelta(minutes=settings.num("order_search_minutes")))
    return res


async def cancel(s: AsyncSession, deal_id: int, to: str = "cancelled", reason: str = "buyer_cancel") -> Deal | None:
    """Close a request before requisites were given. Unfreezes the merchant's funds if he had taken it."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status not in ("searching", "assigned"):
        return None
    was = d.status
    res = await deals._move(s, d.id, (was,), to, close_reason=reason)
    if res is not None and was == "assigned":
        await money.unfreeze(s, res.seller_id, res.seller_debit, f"deal:{res.id}")
    return res


async def last_requisites(s: AsyncSession, uid: int, limit: int = 3) -> list[Card]:
    """The merchant's recently given requisites, distinct, newest first: one tap to give them again."""
    rows = (await s.scalars(select(Card).where(Card.user_id == uid, Card.is_deleted, ~Card.is_active,
                                               Card.id.in_(select(Deal.card_id).where(Deal.is_order,
                                                                                      Deal.seller_id == uid)))
                            .order_by(Card.id.desc()).limit(30))).all()
    seen, out = set(), []
    for c in rows:
        if (c.kind, c.requisites, c.holder, c.bank) not in seen:
            seen.add((c.kind, c.requisites, c.holder, c.bank))
            out.append(c)
    return out[:limit]


async def stale(s: AsyncSession) -> tuple[list[int], list[int]]:
    """(searching past deadline, assigned past deadline) deal ids."""
    t = now()
    searching = (await s.scalars(select(Deal.id).where(Deal.status == "searching", Deal.expires_at < t))).all()
    assigned = (await s.scalars(select(Deal.id).where(Deal.status == "assigned", Deal.expires_at < t))).all()
    return list(searching), list(assigned)


async def forget_offers(s: AsyncSession, deal_id: int) -> list[tuple[int, int]]:
    """(user id, message id) of offers still showing "Take" for this request; the caller edits the messages.
    The rows are removed, so if the request returns to searching these merchants are offered it again."""
    rows = (await s.execute(select(OrderOffer.user_id, OrderOffer.msg_id).where(
        OrderOffer.deal_id == deal_id, ~OrderOffer.declined))).all()
    await s.execute(delete(OrderOffer).where(OrderOffer.deal_id == deal_id, ~OrderOffer.declined))
    return [(uid, mid) for uid, mid in rows if mid]
