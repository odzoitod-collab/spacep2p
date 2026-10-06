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
from decimal import ROUND_HALF_UP, Decimal
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
                         expect_credit: Decimal | None = None, client=None, external_id: str | None = None,
                         payer_id: str | None = None) -> Deal:
    """A request for requisites. Terms are quoted now; nothing is frozen until a merchant takes it."""
    if not amount_rub.is_finite() or amount_rub <= 0 or amount_rub.as_tuple().exponent < -2:
        raise DealError("Некорректная сумма", "invalid_amount")
    lo, hi = settings.dec("order_min_rub"), settings.dec("order_max_rub")
    if not lo <= amount_rub <= hi:
        raise DealError(f"Реквизиты под сумму: от {money.fmt(lo)} до {money.fmt(hi)} ₽", "order_range")
    await money.lock(s, buyer.id)
    await deals.check_buyer(s, buyer, amount_rub, client, payer_id)
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
             api_client_id=client.id if client is not None else None, external_id=external_id, payer_id=payer_id)
    s.add(d)
    await s.flush()
    return d


async def open_rub(s: AsyncSession, uid: int) -> Decimal:
    """RUB in the merchant's order deals that are still open."""
    return Decimal(await s.scalar(select(func.coalesce(func.sum(Deal.amount_rub), 0)).where(
        Deal.seller_id == uid, Deal.is_order, Deal.status.in_(deals.FUNDED))))


REP_WINDOW = 30  # the reputation is the average of this many latest scores


async def reputation(s: AsyncSession, uid: int) -> tuple[Decimal | None, int]:
    """(the rating an admin set, else the average of the latest scores or None while there are fewer than
    rep_min_count; how many scores in all)."""
    from bot.models import MerchantRating
    manual = await s.scalar(select(User.rating).where(User.id == uid))
    if manual is not None:
        return Decimal(manual), await s.scalar(select(func.count(MerchantRating.id)).where(
            MerchantRating.merchant_id == uid, MerchantRating.score.is_not(None)))
    return await auto_reputation(s, uid)


async def auto_reputation(s: AsyncSession, uid: int) -> tuple[Decimal | None, int]:
    """The operators' average only, whatever an admin set."""
    from bot.models import MerchantRating
    scores = list((await s.scalars(select(MerchantRating.score).where(
        MerchantRating.merchant_id == uid, MerchantRating.score.is_not(None))
        .order_by(MerchantRating.id.desc()).limit(REP_WINDOW))).all())
    total = await s.scalar(select(func.count(MerchantRating.id)).where(
        MerchantRating.merchant_id == uid, MerchantRating.score.is_not(None)))
    if total < settings.num("rep_min_count"):
        return None, total
    return (Decimal(sum(scores)) / len(scores)).quantize(Decimal("0.1")), total


SCORE_LATE_LINK = 3  # took a Bybit request and sent no link in time: the platform scores it itself


async def auto_score(s: AsyncSession, d: Deal, merchant: int, score: int) -> None:
    """A score from the platform itself (operator_id 0): facts no operator has to report — they count in the
    reputation like an operator's score. Does not commit."""
    from bot.models import MerchantRating
    if not await s.scalar(select(MerchantRating.id).where(MerchantRating.deal_id == d.id,
                                                          MerchantRating.merchant_id == merchant)):
        s.add(MerchantRating(deal_id=d.id, merchant_id=merchant, operator_id=0, gave=False, score=score))


def stars(rep: Decimal) -> str:
    """10 points as 5 stars: 8.5 -> ★★★★☆."""
    n = int((Decimal(rep) / 2).to_integral_value(ROUND_HALF_UP))
    return "★" * n + "☆" * (5 - n)


def rep_line(rep: Decimal | None, total: int) -> str:
    return f"{stars(rep)} <b>{money.fmt(rep, 1)}</b> из 10 · оценок {total}" if rep is not None else \
        f"пока нет ({total} из {settings.get('rep_min_count')} оценок)"


async def rep_text(s: AsyncSession, uid: int) -> list[str]:
    """The reputation for admins: what counts now and where it comes from."""
    manual = await s.scalar(select(User.rating).where(User.id == uid))
    auto, total = await auto_reputation(s, uid)
    by_ops = (f"по оценкам операторов: {money.fmt(auto, 1)} ({total})" if auto is not None else
              f"оценок операторов {total} из {settings.get('rep_min_count')} — авто-рейтинга ещё нет")
    if manual is None:
        return [rep_line(auto, total) if auto is not None else f"пока нет — {by_ops}",
                "считается по оценкам операторов"]
    return [f"{stars(manual)} <b>{money.fmt(Decimal(manual), 1)}</b> из 10 · <b>выставлен вручную</b>", by_ops]


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
    if m.offline:
        return "вы не на линии — включите приём заявок в кабинете"
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
    return [(m, u) for m, u in rows if u.id != d.buyer_id and u.id not in seen and not asleep(m) and not m.offline]


async def merchant_score(s: AsyncSession, uid: int, done: int) -> Decimal:
    """Who gets a new request first: the reputation (operators' scores and the platform's own for late links; 7 until
    there are enough) and a little for experience."""
    rep, _ = await reputation(s, uid)
    return (rep if rep is not None else Decimal(7)) + Decimal(min(done, 100)) / 50


async def first_wave(s: AsyncSession, d: Deal, ids: list[int]) -> set[int] | None:
    """Of these merchants (who have not seen the request yet), the ones who get it during its first
    order_wave_seconds: the order_first_wave best — each re-send in the window reaches the next best, so a request
    the best ones declined is not left unseen. None when the wave is over (or off): everyone and the chats."""
    size, secs = settings.num("order_first_wave"), settings.num("order_wave_seconds")
    started = deals.aware(d.expires_at) - timedelta(minutes=settings.num("order_search_minutes"))
    if not size or now() - started >= timedelta(seconds=secs):
        return None
    done = await deals.completed_count(s, ids)
    scored = sorted([(await merchant_score(s, uid, done[uid]), uid) for uid in ids], reverse=True)
    return {uid for _, uid in scored[:size]}


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
    if bybit and (waiting := await s.scalar(select(Deal.id).where(
            Deal.seller_id == merchant.id, Deal.status == "assigned", Deal.via_bybit).limit(1))):
        raise DealError(f"Сначала пришлите ссылку на ордер по заявке #{waiting} — две заявки без ордера держать "
                        "нельзя", "busy")
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
    if not deals.held(d) and deals.aware(d.expires_at) < now():
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
    held = d.via_bybit and d.operator_id == who.id  # the operator closes it himself: no payment deadline
    return await deals._move(s, d.id, (d.status,), "waiting_payment", card_id=card.id,
                             expires_at=now() + (deals.HOLD if held else timedelta(minutes=minutes)))


async def give_link(s: AsyncSession, deal_id: int, merchant: User, url: str) -> Deal:
    """Bybit mode: the merchant's order link goes to the operators for a check (assigned -> checking)."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "assigned" or d.seller_id != merchant.id or not d.via_bybit:
        raise DealError("Заявка уже не у вас", "not_yours")
    if deals.aware(d.expires_at) < now():
        raise DealError("Время на ссылку вышло — заявка передана другим мерчантам", "late")
    if used := await s.scalar(select(Deal.id).where(Deal.bybit_url == url, Deal.id != d.id).limit(1)):
        raise DealError(f"Эта ссылка уже была в заявке #{used}. Создайте новый ордер под эту сумму", "link_used")
    if d.operator_id:  # the operator asked to recreate the order: the new link is his, still without a deadline
        return await deals._move(s, d.id, ("assigned",), "checking", bybit_url=url, expires_at=now() + deals.HOLD)
    return await deals._move(s, d.id, ("assigned",), "checking", bybit_url=url, operator_id=None,
                             expires_at=now() + timedelta(minutes=settings.num("order_check_minutes")))


async def claim(s: AsyncSession, deal_id: int, operator: User) -> Deal:
    """The first operator who accepts the Bybit order owns it; the others' offers close. Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or d.status != "checking":
        raise DealError("Ордер уже обработан или заявка закрыта", "gone")
    if d.operator_id not in (None, operator.id):
        raise DealError("Ордер уже принял другой оператор", "taken")
    if operator.id in (d.buyer_id, d.seller_id):
        raise DealError("Это ваша собственная заявка — её ордер примет другой оператор", "own")
    if (cap := settings.dec("operator_max_debt")) > 0:
        exposure = await operator_exposure(s, operator.id)
        if exposure + d.seller_debit > cap:
            raise DealError(f"Предел {money.usdt(cap)} USDT: долг и ордера в работе — {money.usdt(exposure)} USDT. "
                            "Погасите долг в «Оператор», и ордера снова можно принимать", "debt_cap")
    d.operator_id = operator.id
    d.expires_at = now() + deals.HOLD  # from now on the operator closes it, not a timer
    return d


async def operator_exposure(s: AsyncSession, uid: int) -> Decimal:
    """What the platform risks with this operator: his debt plus the USDT of the Bybit orders in his hands that will
    become debt once confirmed."""
    from bot.models import Operator
    op = await s.get(Operator, uid)
    open_ = await s.scalar(select(func.coalesce(func.sum(Deal.seller_debit), 0)).where(
        Deal.operator_id == uid, Deal.via_bybit, Deal.bybit_url.is_not(None),
        Deal.status.in_(("assigned", "checking", "waiting_payment", "paid", "dispute"))))
    return (op.debt if op else Decimal(0)) + Decimal(open_)


async def drop_operator(s: AsyncSession, uid: int) -> tuple[list[Deal], list[Deal]]:
    """The operator is gone (removed or banned): his orders not yet with requisites go back to the other operators
    with the usual time; deals where the buyer has requisites but has not paid are closed like an expired payment —
    a buyer who did pay still uploads the receipt and the deal goes to the administration. Returns (returned,
    closed). Does not commit."""
    returned, closed = [], []
    for d in (await s.scalars(select(Deal).where(Deal.operator_id == uid, Deal.via_bybit,
                                                 Deal.status.in_(("checking", "assigned"))))).all():
        d.operator_id = None
        if d.status == "checking":
            d.expires_at = now() + timedelta(minutes=settings.num("order_check_minutes"))
        returned.append(d)
    for did in (await s.scalars(select(Deal.id).where(Deal.operator_id == uid, Deal.via_bybit,
                                                      Deal.status == "waiting_payment"))).all():
        if d := await deals._move(s, did, ("waiting_payment",), "expired", close_reason="operator_gone"):
            closed.append(d)
    return returned, closed


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
    d.expires_at = now() + timedelta(minutes=settings.num("order_check_minutes"))  # the others get the usual time
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
                              expires_at=now() + deals.HOLD)
    return moved, merchant, operator


async def recreate(s: AsyncSession, deal_id: int, operator: User) -> tuple[Deal | None, bool]:
    """«Пересоздать ордер»: the order is wrong or dead (another amount, closed, no requisites yet) — the merchant
    makes a new one and sends its link within order_link_minutes. The operator who asked keeps the deal and gets the
    new link; requisites already given are taken back (the buyer has not paid). (deal or None, requisites revoked).
    Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or not d.via_bybit or not d.bybit_url or not d.seller_id \
            or d.status not in ("checking", "waiting_payment") or d.operator_id not in (None, operator.id):
        return None, False
    revoked = d.status == "waiting_payment"
    moved = await deals._move(s, d.id, (d.status,), "assigned", bybit_url=None, card_id=None, operator_id=operator.id,
                              expires_at=now() + timedelta(minutes=settings.num("order_link_minutes")))
    return moved, revoked


async def close_by_operator(s: AsyncSession, deal_id: int, operator: User) -> Deal | None:
    """The operator closes his deal before the buyer paid (the order is gone, the buyer does not answer…).
    Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if d is None or not d.via_bybit or d.operator_id != operator.id or d.status not in ("checking", "waiting_payment"):
        return None
    if d.status == "waiting_payment":  # the buyer has requisites: if he paid after all, his receipt still comes in
        return await deals._move(s, d.id, ("waiting_payment",), "expired", close_reason="operator_close")
    return await deals.cancel(s, d.id, (d.status,), "cancelled", "operator_close")


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


async def stale(s: AsyncSession) -> tuple[list[int], list[int], list[int]]:
    """(searching, assigned, checking) deal ids past their deadline."""
    t = now()
    out = []
    for st in REQUEST:
        q = select(Deal.id).where(Deal.status == st, Deal.expires_at < t)
        if st == "checking":  # an order an operator accepted has no deadline: he closes it himself
            q = q.where(Deal.operator_id.is_(None))
        out.append(list((await s.scalars(q)).all()))
    return tuple(out)


async def forget_offers(s: AsyncSession, deal_id: int, kinds: tuple[str, ...] = ("merchant", "chat", "operator")
                        ) -> list[tuple[int, int, str]]:
    """(chat id, message id, kind) of offers still showing a button for this request; the caller edits the messages.
    The rows are removed, so if the request returns to searching these merchants and chats get it again."""
    where = (OrderOffer.deal_id == deal_id, ~OrderOffer.declined, OrderOffer.kind.in_(kinds))
    rows = (await s.execute(select(OrderOffer.user_id, OrderOffer.msg_id, OrderOffer.kind).where(*where))).all()
    await s.execute(delete(OrderOffer).where(*where))
    return [(uid, mid, kind) for uid, mid, kind in rows if mid]
