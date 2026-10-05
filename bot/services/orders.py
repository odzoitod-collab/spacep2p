"""Order requisites: when no static card fits, a buyer (or an API client) requests requisites for an exact amount.

    searching --merchant takes--> assigned --gives requisites (balance)--> waiting_payment -> (usual deal:
        |                            |                                    receipt, confirmation, dispute, expiry)
        |                            +--gives a Bybit order link--> checking --an operator accepts the order and
        |                            |          gives its requisites--> waiting_payment
        |                            |          (operator rejects the link -> assigned again)
        |                            +--declines / time is up--> searching (other merchants)
        +--nobody within order_search_minutes / buyer cancels--> cancelled ; checking past its deadline -> cancelled

Every approved order merchant gets every request — no amount limits, no on/off switch — in the bot, and the request is
also posted in the community chat and the team chats with a link into the bot. The merchant decides per request how
to work it when he takes it:
  * Bybit order (via_bybit): no balance needed, nothing frozen — he sends a link to his Bybit P2P order for
    seller_debit USDT, every operator gets «Принять ордер», the first one gets the link, enters the order, gives its
    requisites to the buyer, checks the payment and confirms; the USDT arrive on the operator's Bybit account (his
    debt to the platform, services/operators.py) and the platform credits the buyer.
  * Balance: his seller_debit is frozen when he takes the request (so a taken request is always covered) and
    released if he declines or runs out of time; he gives the requisites and confirms the payment himself.

Order merchants have no percent: they sell at the fixed order_rate, seller_debit = amount_rub / order_rate. The deal's
terms are fixed when the request is created. While searching/assigned/checking, expires_at is the stage deadline.
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
    qt, brate, cpct = deals.buyer_quote(qt, amount_rub, rate, buyer, client)
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


REP_WINDOW = 30  # the reputation is the average of this many latest scores


async def reputation(s: AsyncSession, uid: int) -> tuple[Decimal | None, int]:
    """(average of the latest scores or None while there are fewer than rep_min_count, how many scores in all)."""
    from bot.models import MerchantRating
    scores = list((await s.scalars(select(MerchantRating.score).where(
        MerchantRating.merchant_id == uid, MerchantRating.score.is_not(None))
        .order_by(MerchantRating.id.desc()).limit(REP_WINDOW))).all())
    total = await s.scalar(select(func.count(MerchantRating.id)).where(
        MerchantRating.merchant_id == uid, MerchantRating.score.is_not(None)))
    if total < settings.num("rep_min_count"):
        return None, total
    return (Decimal(sum(scores)) / len(scores)).quantize(Decimal("0.1")), total


def rep_line(rep: Decimal | None, total: int) -> str:
    return f"★ {money.fmt(rep, 1)} из 10 · оценок {total}" if rep is not None else \
        f"пока нет ({total} из {settings.get('rep_min_count')} оценок)"


def bybit_problem(rep: Decimal | None, d: Deal) -> str:
    """Why a merchant with this reputation may not take this request by a Bybit order ("" = he may)."""
    if rep is None:
        return ""
    if rep < settings.dec("rep_low"):
        return f"репутация {money.fmt(rep, 1)} ниже {settings.get('rep_low')} — только с баланса"
    if rep < settings.dec("rep_mid") and d.amount_rub > settings.dec("rep_mid_max_rub"):
        return (f"репутация {money.fmt(rep, 1)}: Bybit-заявки до {money.fmt(settings.dec('rep_mid_max_rub'))} ₽ "
                "— эту возьмите с баланса")
    return ""


def asleep(m: OrderMerchant | None) -> bool:
    """On a pause after strike_limit requests in a row without requisites: takes and gets no requests."""
    return m is not None and m.sleep_until is not None and deals.aware(m.sleep_until) > now()


def fit_problem(m: OrderMerchant | None, u: User, d: Deal, bybit: bool) -> str:
    """Why this merchant cannot take this request this way now ("" = he can)."""
    if m is None or m.status != "approved":
        return "вы не ордерный мерчант" if m is None or m.status in ("pending", "rejected") else \
            "доступ ордерного мерчанта приостановлен"
    if u.is_banned:
        return "аккаунт заблокирован"
    if asleep(m):
        return (f"пауза до {deals.aware(m.sleep_until).astimezone(deals.MSK):%d.%m %H:%M} МСК — "
                f"{settings.num('strike_limit')} раза подряд не дали реквизиты по своему ордеру")
    if not bybit and u.balance < d.seller_debit:  # a Bybit order needs no balance in the bot
        return (f"для работы с баланса нужно {money.usdt(d.seller_debit)} USDT свободных, у вас "
                f"{money.usdt(u.balance)} — возьмите через Bybit-ордер")
    return ""


async def eligible(s: AsyncSession, d: Deal) -> list[tuple[OrderMerchant, User]]:
    """Approved merchants who have not been offered this request yet (a live offer or a decline both count). Not the
    buyer, not banned. No amount limits: every merchant sees every request and decides himself."""
    seen = set((await s.scalars(select(OrderOffer.user_id).where(
        OrderOffer.deal_id == d.id, OrderOffer.kind == "merchant"))).all())
    rows = (await s.execute(select(OrderMerchant, User).join(User, User.id == OrderMerchant.user_id).where(
        OrderMerchant.status == "approved", ~User.is_banned))).all()
    return [(m, u) for m, u in rows if u.id != d.buyer_id and u.id not in seen and not asleep(m)]


async def take(s: AsyncSession, deal_id: int, merchant: User, bybit: bool = True) -> Deal:
    """First merchant wins: the deal row is locked, the status checked; with the balance his funds are frozen.
    Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "searching" or deals.aware(d.expires_at) < now():
        raise DealError("Заявку уже взял другой мерчант или она закрыта", "taken")
    if d.buyer_id == merchant.id:
        raise DealError("Это ваша собственная заявка")
    m = await s.get(OrderMerchant, merchant.id)
    u = await money.lock(s, merchant.id)
    if problem := fit_problem(m, u, d, bybit):
        raise DealError(f"Не можете взять заявку: {problem}", "cannot")
    if await s.scalar(select(OrderOffer.id).where(OrderOffer.deal_id == d.id, OrderOffer.user_id == merchant.id,
                                                  OrderOffer.declined).limit(1)):
        raise DealError("Вы уже работали с этой заявкой — её выполнит другой мерчант", "cannot")
    if bybit and (problem := bybit_problem((await reputation(s, merchant.id))[0], d)):
        raise DealError(f"Не можете взять через Bybit-ордер: {problem}", "cannot")
    if not bybit:
        await money.freeze(s, u.id, d.seller_debit, f"deal:{d.id}")
    # a Bybit order: order_link_minutes for the link, or the request is not his (it goes to the others);
    # with the balance: order_take_minutes to fill in the requisites
    minutes = settings.num("order_link_minutes" if bybit else "order_take_minutes")
    moved = await deals._move(s, d.id, ("searching",), "assigned", seller_id=u.id, via_bybit=bybit,
                              expires_at=now() + timedelta(minutes=minutes))
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
    if d.via_bybit and d.bybit_url and d.seller_id and (m := await s.get(
            OrderMerchant, d.seller_id, with_for_update=True, populate_existing=True)):
        m.strikes = 0  # his order had requisites: the misses in a row start again (row locked, like strike())
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
    """The first operator who accepts the Bybit order owns it; the others' offers close. Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "checking":
        raise DealError("Ордер уже обработан или заявка закрыта", "gone")
    if d.operator_id not in (None, operator.id):
        raise DealError("Ордер уже принял другой оператор", "taken")
    d.operator_id = operator.id
    return d


async def unclaim(s: AsyncSession, deal_id: int, operator: User) -> Deal | None:
    """The operator gives the order back: it is offered to all operators again; a request an admin took without a
    Bybit order goes back to the search. Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "checking" or d.operator_id != operator.id:
        return None
    if d.bybit_url is None:
        return await deals._move(s, d.id, ("checking",), "searching", seller_id=None, via_bybit=False,
                                 operator_id=None,
                                 expires_at=now() + timedelta(minutes=settings.num("order_search_minutes")))
    d.operator_id = None
    return d


async def admin_take(s: AsyncSession, deal_id: int, admin: User) -> tuple[Deal, int | None, int | None]:
    """An admin gives the requisites of a request himself: he becomes its operator (nothing frozen, the platform
    credits the buyer, the rubles come to the requisites he gives). A merchant who had taken it without a Bybit order
    is released (his freeze too); with a Bybit order the merchant stays and the admin enters his order.
    Returns (deal, released merchant, replaced operator). Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status not in REQUEST:
        raise DealError("Реквизиты уже выданы или заявка закрыта", "gone")
    merchant = d.seller_id if d.status == "assigned" or (d.status == "checking" and not d.bybit_url) else None
    operator = d.operator_id if d.operator_id != admin.id else None
    if d.status == "assigned" and deals.frozen(d) and d.seller_id:
        await money.unfreeze(s, d.seller_id, d.seller_debit, f"deal:{d.id}")
    keep = d.status == "checking" and d.bybit_url
    moved = await deals._move(s, d.id, (d.status,), "checking", via_bybit=True, operator_id=admin.id,
                              seller_id=d.seller_id if keep else None,
                              expires_at=now() + timedelta(minutes=settings.num("order_take_minutes")))
    return moved, merchant, operator


async def reject_link(s: AsyncSession, deal_id: int, operator: User) -> Deal | None:
    """The link is wrong (other amount, closed order...): back to the merchant for another one (checking -> assigned)."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "checking" or d.operator_id not in (None, operator.id):
        return None
    return await deals._move(s, d.id, ("checking",), "assigned", bybit_url=None, operator_id=None,
                             expires_at=now() + timedelta(minutes=settings.num("order_link_minutes")))


async def strike(s: AsyncSession, merchant_id: int) -> tuple[int, object]:
    """An operator says the merchant's order had no requisites: one more miss in a row; at strike_limit the merchant
    sleeps strike_sleep_hours (no requests) and the count starts again. Returns (misses in a row, sleeps until or
    None). Does not commit."""
    m = await s.get(OrderMerchant, merchant_id, with_for_update=True, populate_existing=True)
    if m is None:
        return 0, None
    m.strikes += 1
    if m.strikes < settings.num("strike_limit"):
        return m.strikes, None
    m.strikes, m.sleep_until = 0, now() + timedelta(hours=settings.num("strike_sleep_hours"))
    return settings.num("strike_limit"), m.sleep_until


async def release(s: AsyncSession, deal_id: int, why: str = "declined") -> Deal | None:
    """Assigned / checking -> searching again: the merchant declined or ran out of time. His funds are unfrozen
    (balance mode) and he is not offered this request again. Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status not in ("assigned", "checking"):
        return None
    merchant = d.seller_id
    if merchant and deals.frozen(d):
        await money.unfreeze(s, merchant, d.seller_debit, f"deal:{d.id}")
    if merchant:  # he does not get this request again
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


async def forget_offers(s: AsyncSession, deal_id: int, kinds: tuple[str, ...] = ("merchant", "chat", "operator")
                        ) -> list[tuple[int, int, str]]:
    """(chat id, message id, kind) of offers still showing a button for this request; the caller edits the messages.
    The rows are removed, so if the request returns to searching these merchants and chats get it again."""
    where = (OrderOffer.deal_id == deal_id, ~OrderOffer.declined, OrderOffer.kind.in_(kinds))
    rows = (await s.execute(select(OrderOffer.user_id, OrderOffer.msg_id, OrderOffer.kind).where(*where))).all()
    await s.execute(delete(OrderOffer).where(*where))
    return [(uid, mid, kind) for uid, mid, kind in rows if mid]
