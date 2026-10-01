"""Order requisites: when no static card fits, a buyer (or an API client) requests requisites for an exact amount.

    searching --merchant takes--> assigned --gives requisites (balance mode)--> waiting_payment -> (usual deal:
        |                            |                                         receipt, confirmation, dispute, expiry)
        |                            +--gives a Bybit order link (bybit mode)--> checking --operator gives the
        |                            |          requisites of that order--> waiting_payment
        |                            |          (operator rejects the link -> assigned again)
        |                            +--declines / time is up--> searching (other merchants)
        +--nobody within order_search_minutes / buyer cancels--> cancelled ; checking past its deadline -> cancelled

Order merchants have no percent: they sell at the fixed order_rate, seller_debit = amount_rub / order_rate. The
deal's terms are fixed when the request is created.

Funds. Balance mode: the merchant's seller_debit is frozen when he takes the request (so a taken request is always
covered) and released if he declines or runs out of time. Bybit mode (default): nothing is frozen and no balance is
needed — the merchant gives a link to his Bybit P2P order for seller_debit USDT, an operator enters it, gives its
requisites to the buyer, checks the payment and confirms; the USDT arrive on the operator's Bybit account and the
platform credits the buyer. While searching/assigned/checking, expires_at is the stage deadline.
"""
from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Card, Deal, OrderMerchant, OrderOffer, User, now
from bot.services import deals, money, settings
from bot.services.deals import DealError

PAY_MINUTES = (15, 20, 30, 45, 60)  # payment windows a merchant can give; the setting sets the minimum
REQUEST = ("searching", "assigned", "checking")  # before requisites are given
MODES = {"bybit": "Bybit-ордер", "balance": "баланс"}
BYBIT_HOSTS = ("bybit.com", "bybitglobal.com", "bybit.eu", "bybit.kz", "bybit.nl", "bybit.tr")


def bybit_link(raw: str | None) -> str | None:
    """A Bybit order link as given by the merchant, or None if it is not one."""
    url = (raw or "").strip()
    if not url.startswith("http"):
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or len(url) > 300 or any(c.isspace() or c in "<>\"'" for c in url)
            or not any(host == h or host.endswith("." + h) for h in BYBIT_HOSTS) or len(parts.path) < 2):
        return None
    return url


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
    # order merchants sell at the fixed order_rate; the buyer pays the same platform fee as with a static card
    rate, mr, pp = settings.dec("rate"), settings.dec("order_rate"), settings.dec("platform_pct")
    try:
        qt = money.quote_fixed(amount_rub, rate, mr, pp)
    except ValueError:
        raise DealError("Покупки временно недоступны: некорректные настройки курса. Напишите в поддержку.")
    qt, brate, cpct = deals.client_quote(qt, amount_rub, rate, client)
    if expect_credit is not None and qt.buyer_credit != expect_credit:
        raise DealError("Курс или комиссия изменились. Проверьте новую сумму.", "terms")
    d = Deal(buyer_id=buyer.id, seller_id=None, card_id=None, amount_rub=amount_rub, rate=rate, seller_pct=Decimal(0),
             merchant_rate=mr, platform_pct=cpct if cpct is not None else pp, buyer_rate=brate,
             seller_debit=qt.seller_debit, buyer_credit=qt.buyer_credit,
             platform_fee=qt.platform_fee,
             status="searching", is_order=True, sender_bank=(sender_bank or None) and sender_bank[:40],
             expires_at=now() + timedelta(minutes=settings.num("order_search_minutes")),
             api_client_id=client.id if client is not None else None, external_id=external_id)
    s.add(d)
    await s.flush()
    return d


async def open_rub(s: AsyncSession, uid: int) -> Decimal:
    """RUB in the merchant's order deals that are still open."""
    return Decimal(await s.scalar(select(func.coalesce(func.sum(Deal.amount_rub), 0)).where(
        Deal.seller_id == uid, Deal.is_order, Deal.status.in_(deals.FUNDED))))


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
    if m.mode == "balance" and u.balance < d.seller_debit:  # a Bybit order needs no balance in the bot
        return f"нужно {money.usdt(d.seller_debit)} USDT свободного баланса, у вас {money.usdt(u.balance)}"
    return ""


async def eligible(s: AsyncSession, d: Deal) -> list[tuple[OrderMerchant, User]]:
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
            out.append((m, u))
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
    bybit = m.mode == "bybit"
    if not bybit:
        await money.freeze(s, u.id, d.seller_debit, f"deal:{d.id}")
    moved = await deals._move(s, d.id, ("searching",), "assigned", seller_id=u.id, via_bybit=bybit,
                              expires_at=now() + timedelta(minutes=settings.num("order_take_minutes")))
    if moved is None:  # unreachable with the row lock, kept as a guard
        raise DealError("Заявку уже взял другой мерчант", "taken")
    return moved


def giver(d: Deal | None, uid: int) -> bool:
    """May this user give the requisites of this request now: the merchant himself (balance mode) or the operator
    who took his Bybit order."""
    return d is not None and ((d.status == "assigned" and not d.via_bybit and d.seller_id == uid)
                              or (d.status == "checking" and d.operator_id == uid))


async def give_requisites(s: AsyncSession, deal_id: int, who: User, kind: str, bank: str, number: str,
                          holder: str, minutes: int) -> Deal:
    """Requisites for this deal (the merchant's, or those of the Bybit order given by the operator): an unlisted
    one-off card; the buyer's payment timer starts."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if not giver(d, who.id):
        raise DealError("Заявка уже не у вас", "not_yours")
    if deals.aware(d.expires_at) < now():
        raise DealError("Время на выдачу реквизитов вышло", "late")
    if minutes not in pay_choices():
        raise DealError("Недопустимое время на оплату")
    card = Card(user_id=who.id, kind=kind, bank=bank, requisites=number, holder=holder, min_rub=d.amount_rub,
                max_rub=d.amount_rub, is_active=False, is_deleted=True)  # never listed in the market
    s.add(card)
    await s.flush()
    return await deals._move(s, d.id, (d.status,), "waiting_payment", card_id=card.id,
                             expires_at=now() + timedelta(minutes=minutes))


async def give_link(s: AsyncSession, deal_id: int, merchant: User, url: str) -> Deal:
    """Bybit mode: the merchant's order link goes to the operators for a check (assigned -> checking)."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "assigned" or d.seller_id != merchant.id or not d.via_bybit:
        raise DealError("Заявка уже не у вас", "not_yours")
    if deals.aware(d.expires_at) < now():
        raise DealError("Время на ссылку вышло — заявка передана другим мерчантам", "late")
    if used := await s.scalar(select(Deal.id).where(Deal.bybit_url == url, Deal.id != d.id).limit(1)):
        raise DealError(f"Эта ссылка уже была в заявке #{used}. Создайте новый ордер под эту сумму", "link_used")
    return await deals._move(s, d.id, ("assigned",), "checking", bybit_url=url, operator_id=None,
                             expires_at=now() + timedelta(minutes=settings.num("order_check_minutes")))


async def claim(s: AsyncSession, deal_id: int, operator: User) -> Deal:
    """The first operator who opens the Bybit order owns this check. Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "checking":
        raise DealError("Ордер уже обработан или заявка закрыта", "gone")
    if d.operator_id not in (None, operator.id):
        raise DealError(f"Ордер уже взял другой оператор ({d.operator_id})", "taken")
    d.operator_id = operator.id
    return d


async def reject_link(s: AsyncSession, deal_id: int, operator: User) -> Deal | None:
    """The link is wrong (other amount, closed order...): back to the merchant for another one (checking -> assigned)."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "checking" or d.operator_id not in (None, operator.id):
        return None
    return await deals._move(s, d.id, ("checking",), "assigned", bybit_url=None, operator_id=None,
                             expires_at=now() + timedelta(minutes=settings.num("order_take_minutes")))


async def release(s: AsyncSession, deal_id: int, why: str = "declined") -> Deal | None:
    """Assigned / checking -> searching again: the merchant declined or ran out of time. His funds are unfrozen
    (balance mode) and he is not offered this request again. Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status not in ("assigned", "checking"):
        return None
    merchant = d.seller_id
    if deals.frozen(d):
        await money.unfreeze(s, merchant, d.seller_debit, f"deal:{d.id}")
    s.add(OrderOffer(deal_id=d.id, user_id=merchant, declined=True))
    res = await deals._move(s, d.id, (d.status,), "searching", seller_id=None, via_bybit=False, bybit_url=None,
                            operator_id=None, expires_at=now() + timedelta(minutes=settings.num("order_search_minutes")))
    return res


async def cancel(s: AsyncSession, deal_id: int, to: str = "cancelled", reason: str = "buyer_cancel") -> Deal | None:
    """Close a request before requisites were given. Unfreezes the merchant's funds if he had taken it."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status not in REQUEST:
        return None
    was = d.status
    res = await deals._move(s, d.id, (was,), to, close_reason=reason)
    if res is not None and was != "searching" and deals.frozen(res):
        await money.unfreeze(s, res.seller_id, res.seller_debit, f"deal:{res.id}")
    return res


async def last_requisites(s: AsyncSession, uid: int, limit: int = 3) -> list[Card]:
    """Requisites this user (merchant or operator) gave recently, distinct, newest first: one tap to give them again."""
    rows = (await s.scalars(select(Card).where(Card.user_id == uid, Card.is_deleted, ~Card.is_active,
                                               Card.id.in_(select(Deal.card_id).where(Deal.is_order)))
                            .order_by(Card.id.desc()).limit(30))).all()
    seen, out = set(), []
    for c in rows:
        if (c.kind, c.requisites, c.holder, c.bank) not in seen:
            seen.add((c.kind, c.requisites, c.holder, c.bank))
            out.append(c)
    return out[:limit]


async def stale(s: AsyncSession) -> tuple[list[int], list[int], list[int]]:
    """(searching, assigned, checking) deal ids past their deadline."""
    t = now()
    out = []
    for st in REQUEST:
        out.append(list((await s.scalars(select(Deal.id).where(Deal.status == st, Deal.expires_at < t))).all()))
    return tuple(out)


async def forget_offers(s: AsyncSession, deal_id: int) -> list[tuple[int, int]]:
    """(user id, message id) of offers still showing "Take" for this request; the caller edits the messages.
    The rows are removed, so if the request returns to searching these merchants are offered it again."""
    rows = (await s.execute(select(OrderOffer.user_id, OrderOffer.msg_id).where(
        OrderOffer.deal_id == deal_id, ~OrderOffer.declined))).all()
    await s.execute(delete(OrderOffer).where(OrderOffer.deal_id == deal_id, ~OrderOffer.declined))
    return [(uid, mid) for uid, mid in rows if mid]
