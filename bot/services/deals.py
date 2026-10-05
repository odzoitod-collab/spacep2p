from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Card, Deal, User, now
from bot.services import money, settings

OPEN = ("searching", "assigned", "checking", "waiting_payment", "paid", "dispute")
# a merchant works on the deal; his USDT are frozen for it unless it goes through a Bybit order (frozen(d))
FUNDED = ("assigned", "checking", "waiting_payment", "paid", "dispute")
UNPAID = ("searching", "assigned", "checking", "waiting_payment")  # nothing transferred yet
# an operator who accepted a Bybit order owns the deal: no deadline closes it (checking, waiting for the payment),
# he gives the requisites, asks the merchant to recreate the order or closes the deal himself
HOLD = timedelta(days=365)
MAX_EVIDENCE = 15  # per side: one party cannot use up the other's slots


class DealError(Exception):
    def __init__(self, text: str, code: str = ""):
        self.code = code
        super().__init__(text)


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def frozen(d: Deal) -> bool:
    """The seller's USDT are frozen for this deal (not a Bybit order, where the operator receives USDT on Bybit)."""
    return not d.via_bybit


def held(d: Deal) -> bool:
    """The deal is in an operator's hands (a Bybit order he accepted, or an admin's own requisites): no deadline."""
    return bool(d.via_bybit and d.operator_id and d.status in ("checking", "waiting_payment"))


def checker(d: Deal) -> int | None:
    """Who checks the payment and confirms: the operator of a Bybit order, the seller otherwise."""
    return d.operator_id if d.via_bybit else d.seller_id


def sellers(d: Deal) -> list[int]:
    """Who works the deal from the selling side and hears about it: the merchant and the operator of a Bybit order."""
    return [uid for uid in dict.fromkeys((d.seller_id, d.operator_id if d.via_bybit else None)) if uid is not None]


def requote(d: Deal, amount_rub: Decimal) -> money.Quote:
    """The deal's own terms applied to another amount (a dispute settled by the amount actually received)."""
    debit = ((amount_rub / d.merchant_rate).quantize(money.Q, "ROUND_UP") if d.merchant_rate is not None
             else money.seller_debit(amount_rub, d.rate, d.seller_pct))
    return money.split(amount_rub, debit, d.buyer_rate or d.rate, d.platform_pct)


def buyer_quote(qt: money.Quote, amount_rub: Decimal, rate: Decimal, buyer=None, client=None
                ) -> tuple[money.Quote, Decimal | None, Decimal | None]:
    """The merchant side as quoted; the buyer priced by his own terms — an API client's or a user's personal ones.
    Returns (quote, buyer_rate or None if it equals `rate`, platform percent or None = unchanged)."""
    if client is None and not settings.has_terms(buyer):
        return qt, None, None
    brate, pct = settings.buyer_terms(buyer, client)
    try:
        q = money.split(amount_rub, qt.seller_debit, brate, pct)
    except ValueError:
        raise DealError(("Условия API-клиента" if client is not None else "Ваши личные условия")
                        + " выгоднее условий мерчанта — площадка ушла бы в минус. Напишите в поддержку.", "terms_loss")
    return q, (brate if brate != rate else None), pct


def buyer_preview(amount_rub: Decimal, buyer=None) -> money.Quote:
    """What a buyer gets for amount_rub (the buyer side does not depend on the merchant): for screens before a deal."""
    rate, pct = settings.buyer_terms(buyer)
    return money.split(amount_rub, Decimal("Infinity"), rate, pct)


def personal():
    """Deals the user makes in the bot himself, not orders of his API clients."""
    return Deal.api_client_id.is_(None)


async def open_deal_of(s: AsyncSession, uid: int) -> Deal | None:
    """The buyer's latest open deal (he may have several)."""
    return await s.scalar(
        select(Deal).where(Deal.buyer_id == uid, personal(), Deal.status.in_(OPEN)).order_by(Deal.id.desc()).limit(1)
    )


async def open_deals_of(s: AsyncSession, uid: int) -> list[Deal]:
    """Every open purchase of this buyer in the bot, newest first."""
    return list((await s.scalars(select(Deal).where(Deal.buyer_id == uid, personal(), Deal.status.in_(OPEN))
                                 .order_by(Deal.id.desc()))).all())


async def seller_todo(s: AsyncSession, uid: int) -> list[Deal]:
    """Seller's open deals, those needing a decision (paid) first."""
    rows = (await s.scalars(select(Deal).where(Deal.seller_id == uid, Deal.status.in_(OPEN))
                            .order_by(Deal.id))).all()
    return sorted(rows, key=lambda d: d.status != "paid")


MSK = timezone(timedelta(hours=3))


def day_start() -> datetime:
    """Start of the current day in Moscow time: daily card limits reset at 00:00 MSK."""
    return datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def used_today(s: AsyncSession, card_ids: list[int]) -> dict[int, Decimal]:
    """RUB already taken today per card: completed and still open deals count, cancelled do not."""
    if not card_ids:
        return {}
    rows = await s.execute(select(Deal.card_id, func.coalesce(func.sum(Deal.amount_rub), 0)).where(
        Deal.card_id.in_(card_ids), Deal.created_at >= day_start(),
        Deal.status.in_(OPEN + ("completed",))).group_by(Deal.card_id))
    out = dict.fromkeys(card_ids, Decimal(0))
    out.update({cid: Decimal(v) for cid, v in rows.all()})
    return out


def card_range(card: Card, seller: User, used: Decimal = Decimal(0)) -> tuple[Decimal, Decimal]:
    """Effective [min, max] RUB for a card: its limits, the seller's free balance and the daily limit."""
    cap = money.max_rub(seller.balance, settings.dec("rate"), settings.merchant_pct(seller))
    hi = min(card.max_rub, cap)
    if card.daily_limit_rub is not None:
        hi = min(hi, card.daily_limit_rub - used)
    return card.min_rub, hi


def need_usdt(rub: Decimal, seller: User) -> Decimal:
    """Free balance a seller needs to accept a deal of `rub` on a static card."""
    return money.seller_debit(rub, settings.dec("rate"), settings.merchant_pct(seller))


def flow_problem(card: Card, seller: User) -> str:
    """Why this card may not be in the flow ("" — it may): its maximum and the seller's free balance must both cover
    a deal of at least card_min_rub. A card of a seller without balance is never shown to buyers."""
    low = settings.dec("card_min_rub")
    if card.max_rub < low:
        return f"максимум карты меньше {money.fmt(low)} ₽ — поднимите максимум"
    if money.max_rub(seller.balance, settings.dec("rate"), settings.merchant_pct(seller)) < low:
        return (f"пополните баланс: карта в потоке — от {money.fmt(low)} ₽, нужно {money.usdt(need_usdt(low, seller))} "
                f"USDT свободных, у вас {money.usdt(seller.balance)}")
    return ""


def card_visibility(card: Card, seller: User, busy_deal: int | None,
                    used: Decimal = Decimal(0)) -> tuple[bool, str]:
    """Is the card shown to buyers right now, and if not, why (in seller's words)."""
    if card.is_banned:
        return False, "заблокирована администрацией"
    if seller.is_banned:
        return False, "аккаунт заблокирован"
    if problem := flow_problem(card, seller):  # first: what to fix before the card can be switched on at all
        return False, problem
    if not card.is_active:
        return False, "выключена — включите карту"
    if not seller.is_online:
        return False, "вы не на смене"
    if busy_deal:
        return False, f"занята сделкой #{busy_deal}, освободится после неё"
    lo, hi = card_range(card, seller, used)
    if card.daily_limit_rub is not None and card.daily_limit_rub - used < lo:
        return False, (f"дневной лимит исчерпан: принято {money.fmt(used)} из {money.fmt(card.daily_limit_rub)} ₽, "
                       "откроется в 00:00 МСК")
    if hi < lo:
        return False, (f"мало свободного баланса: для минимума {money.fmt(lo)} ₽ нужно "
                       f"{money.usdt(need_usdt(lo, seller))} USDT")
    why = ""
    if hi < card.max_rub:
        daily_left = card.daily_limit_rub - used if card.daily_limit_rub is not None else None
        why = (" — до конца дня по лимиту" if daily_left is not None and hi == daily_left
               else " — больше не позволяет свободный баланс")
    return True, f"сделки от {money.fmt(lo)} до {money.fmt(hi)} ₽{why}"


async def busy_cards(s: AsyncSession, uid: int, full: bool = False) -> dict[int, int]:
    """{card: an open deal on it}. full=True: only the cards that take no more deals now (card_parallel)."""
    rows = (await s.execute(select(Deal.card_id, Deal.id).where(Deal.seller_id == uid, Deal.status.in_(OPEN))
                            .order_by(Deal.id))).all()
    out, count = {}, {}
    for cid, did in rows:
        out.setdefault(cid, did)
        count[cid] = count.get(cid, 0) + 1
    return {cid: did for cid, did in out.items() if not full or count[cid] >= settings.num("card_parallel")}


async def card_load(s: AsyncSession, card_ids: list[int]) -> dict[int, list[Decimal]]:
    """Amounts of the open deals on each card."""
    out = {cid: [] for cid in card_ids}
    if card_ids:
        for cid, amount in (await s.execute(select(Deal.card_id, Deal.amount_rub).where(
                Deal.card_id.in_(card_ids), Deal.status.in_(OPEN)))).all():
            out[cid].append(amount)
    return out


def card_fits(amounts: list[Decimal], amount: Decimal | None) -> str:
    """Why one more deal of `amount` cannot go on a card with these open deals ("" — it can): up to card_parallel at
    once, and the amounts differ by 1 ₽ at least, so the seller tells the transfers apart."""
    if len(amounts) >= settings.num("card_parallel"):
        return "Карта сейчас занята другими сделками"
    if amount is not None and any(abs(a - amount) < 1 for a in amounts):
        return "На эту карту уже идёт перевод такой же суммы — укажите сумму, отличную хотя бы на 1 ₽"
    return ""


async def completed_count(s: AsyncSession, uids: list[int]) -> dict[int, int]:
    """Completed deals per user (as buyer or seller) — shown as a trust signal."""
    if not uids:
        return {}
    out = dict.fromkeys(uids, 0)
    for col in (Deal.seller_id, Deal.buyer_id):
        rows = await s.execute(select(col, func.count(Deal.id)).where(col.in_(uids), Deal.status == "completed")
                               .group_by(col))
        for uid, n in rows.all():
            out[uid] += n
    return out


async def market(s: AsyncSession, me: int, amount: Decimal | None, bank: str | None, kind: str | None):
    q = (
        select(Card, User)
        .join(User, User.id == Card.user_id)
        .where(
            Card.is_active, ~Card.is_banned, ~Card.is_deleted,
            User.is_online, ~User.is_banned, Card.user_id != me,
        )
        .order_by(Card.min_rub)
    )
    if bank:
        q = q.where(Card.bank == bank)
    if kind:
        q = q.where(Card.kind == kind)
    rows = (await s.execute(q)).all()
    used = await used_today(s, [card.id for card, _ in rows])
    load = await card_load(s, [card.id for card, _ in rows])
    out = []
    for card, seller in rows:
        if card_fits(load[card.id], amount):
            continue
        lo, hi = card_range(card, seller, used[card.id])
        if hi < lo or (amount is not None and not lo <= amount <= hi):
            continue
        out.append((card, seller, lo, hi))
    return out


async def seller_stats(s: AsyncSession, uid: int, since: datetime | None = None, order: bool | None = None) -> dict:
    """Merchant view of a period: completed deals (static card / order requisites), RUB received, USDT earned,
    average deal, share of deals that ended well, confirmation speed, disputes. order: None = all deals."""
    where = [Deal.seller_id == uid]
    if order is not None:
        where.append(Deal.is_order == order)
    rows = (await s.execute(select(Deal.amount_rub, Deal.rate, Deal.seller_debit, Deal.is_order, Deal.paid_at,
                                   Deal.closed_at, Deal.close_reason).where(
        *where, Deal.status == "completed", *([Deal.closed_at >= since] if since else [])))).all()
    confirm = [(aware(c) - aware(p)).total_seconds() / 60 for *_, p, c, r in rows if r == "confirmed" and p and c]
    since_created = [Deal.created_at >= since] if since else []
    lost = await s.scalar(select(func.count(Deal.id)).where(
        *where, *since_created, Deal.status.in_(("cancelled", "expired", "void"))))
    problems = await s.scalar(select(func.count(Deal.id)).where(*where, *since_created, Deal.dispute_reason.is_not(None)))
    n = len(rows)
    rub = sum((r[0] for r in rows), Decimal(0))
    return {
        "n": n,
        "n_order": sum(1 for r in rows if r[3]),
        "rub": rub,
        "income": sum((r[0] / r[1] - r[2] for r in rows), Decimal(0)),
        "avg": rub / n if n else Decimal(0),
        "success": round(100 * n / (n + lost)) if n + lost else None,
        "confirm_min": round(sum(confirm) / len(confirm)) if confirm else None,
        "disputes": problems,
    }


async def income_by_day(s: AsyncSession, uid: int, days: int = 7) -> list[tuple[datetime, int, Decimal]]:
    """(day in MSK, completed deals, USDT earned) for the last `days` days, newest first; empty days included."""
    start = day_start() - timedelta(days=days - 1)
    rows = (await s.execute(select(Deal.closed_at, Deal.amount_rub, Deal.rate, Deal.seller_debit).where(
        Deal.seller_id == uid, Deal.status == "completed", Deal.closed_at >= start))).all()
    out = {(start + timedelta(days=i)).astimezone(MSK).date(): [0, Decimal(0)] for i in range(days)}
    for closed, rub, rate, debit in rows:
        day = aware(closed).astimezone(MSK).date()
        if day in out:
            out[day][0] += 1
            out[day][1] += rub / rate - debit
    return [(d, n, inc) for d, (n, inc) in sorted(out.items(), reverse=True)]


async def create(s: AsyncSession, buyer: User, card_id: int, amount_rub: Decimal,
                 expect_credit: Decimal | None = None, client=None, external_id: str | None = None,
                 payer_id: str | None = None) -> Deal:
    """expect_credit: USDT amount the buyer saw on the confirmation screen; terms changed -> error.
    client: ApiClient for an API order — its own limits replace the per-person ones (one open deal, 3 per hour,
    cancellation limit), everything about the card and the seller is checked the same way."""
    if not amount_rub.is_finite() or not 0 < amount_rub < Decimal("100000000") or amount_rub.as_tuple().exponent < -2:
        raise DealError("Некорректная сумма")
    owner = await s.scalar(select(Card.user_id).where(Card.id == card_id))
    if owner is None or owner == buyer.id:
        raise DealError("Карта недоступна")
    for uid in sorted((buyer.id, owner)):
        await money.lock(s, uid)
    await check_buyer(s, buyer, amount_rub, client, payer_id)
    card = await s.get(Card, card_id, with_for_update=True, populate_existing=True)
    if not card or not card.is_active or card.is_banned or card.is_deleted:
        raise DealError("Карта больше недоступна")
    if card.user_id == buyer.id:
        raise DealError("Нельзя купить у себя")
    if problem := card_fits((await card_load(s, [card.id]))[card.id], amount_rub):
        raise DealError(problem, "card_busy")
    seller = await money.lock(s, card.user_id)
    if not seller.is_online or seller.is_banned:
        raise DealError("Продавец ушёл со смены")
    lo, hi = card_range(card, seller, (await used_today(s, [card.id]))[card.id])
    if not lo <= amount_rub <= hi:
        raise DealError(f"Сумма должна быть от {money.fmt(lo)} до {money.fmt(hi)} ₽")
    rate, sp, pp = settings.dec("rate"), settings.merchant_pct(seller), settings.dec("platform_pct")
    try:
        qt = money.quote(amount_rub, rate, sp, pp)
    except ValueError:
        raise DealError("Покупки временно недоступны: некорректные настройки комиссий. Напишите в поддержку.")
    qt, brate, cpct = buyer_quote(qt, amount_rub, rate, buyer, client)
    if expect_credit is not None and qt.buyer_credit != expect_credit:
        raise DealError("Курс или комиссия изменились. Проверьте новую сумму.", "terms")
    if seller.balance < qt.seller_debit:  # seller row is locked, so freeze below cannot fail
        raise DealError("У продавца недостаточно средств")
    deal = Deal(
        buyer_id=buyer.id, seller_id=seller.id, card_id=card.id, amount_rub=amount_rub, buyer_rate=brate,
        rate=rate, seller_pct=sp, platform_pct=cpct if cpct is not None else pp, seller_debit=qt.seller_debit,
        buyer_credit=qt.buyer_credit, platform_fee=qt.platform_fee,
        expires_at=now() + timedelta(minutes=settings.num("deal_minutes")),
        api_client_id=client.id if client is not None else None, external_id=external_id, payer_id=payer_id,
    )
    s.add(deal)
    await s.flush()
    await money.freeze(s, seller.id, qt.seller_debit, f"deal:{deal.id}")
    return deal


async def abandon_pause(s: AsyncSession, *where) -> datetime | None:
    """Until when this buyer (or API payer) may not open deals: abandon_limit deals expired without a receipt within
    24 h — each held a seller's card and USDT for nothing — pause abandon_pause_minutes after the last one."""
    limit = settings.num("abandon_limit")
    if not limit:
        return None
    rows = (await s.scalars(select(Deal.closed_at).where(
        *where, Deal.status == "expired", Deal.close_reason == "expired", Deal.receipt_file_id.is_(None),
        Deal.closed_at > now() - timedelta(hours=24)).order_by(Deal.closed_at.desc()).limit(limit))).all()
    if len(rows) < limit:
        return None
    until = aware(rows[0]) + timedelta(minutes=settings.num("abandon_pause_minutes"))
    return until if until > now() else None


async def check_buyer(s: AsyncSession, buyer: User, amount_rub: Decimal, client=None, payer_id: str | None = None
                      ) -> None:
    """Who may open a deal now: API limits for a client (and its payer); a person — several deals at once, up to
    buyer_max_open unpaid ones (each holds a seller's card and frozen USDT), not on an abandon pause. Buyer row
    locked."""
    if client is not None:
        await _check_client(s, client, amount_rub)
        if payer_id:
            from bot.models import ApiPayerBlock
            if await s.scalar(select(ApiPayerBlock.id).where(ApiPayerBlock.client_id == client.id,
                                                             ApiPayerBlock.payer_id == payer_id)):
                raise DealError("Orders from this payer are not accepted", "payer_blocked")
            if until := await abandon_pause(s, Deal.api_client_id == client.id, Deal.payer_id == payer_id):
                raise DealError(f"This payer abandoned {settings.get('abandon_limit')} orders within 24 h: new orders "
                                f"from him are accepted after {until.isoformat(timespec='minutes')}", "payer_paused")
        return
    if until := await abandon_pause(s, Deal.buyer_id == buyer.id, personal()):
        raise DealError(f"{settings.get('abandon_limit')} сделки за сутки истекли без оплаты — они держали карты "
                        f"продавцов. Новые сделки — с {until.astimezone(MSK):%H:%M} МСК. Создавайте сделку, только "
                        "когда готовы перевести.", "abandon_pause")
    limit = settings.num("buyer_max_open")
    waiting = await s.scalar(select(func.count(Deal.id)).where(
        Deal.buyer_id == buyer.id, personal(), Deal.status.in_(UNPAID)))
    if waiting >= limit:
        raise DealError(f"У вас уже {waiting} неоплаченных сделок — это предел. Оплатите или отмените одну из них.",
                        "open_limit")


async def _check_client(s: AsyncSession, client, amount_rub: Decimal) -> None:
    """API limits. The buyer row is locked by the caller, so parallel requests of one client are serialized."""
    if not client.min_rub <= amount_rub <= client.max_rub:
        raise DealError(f"Сумма заказа по API: от {money.fmt(client.min_rub)} до {money.fmt(client.max_rub)} ₽",
                        "amount_limit")
    waiting = await s.scalar(select(func.count(Deal.id)).where(
        Deal.api_client_id == client.id, Deal.status.in_(UNPAID)))
    if waiting >= client.max_open:
        raise DealError(f"Открыто {waiting} неоплаченных заказов — это лимит. Дождитесь оплаты или отмените лишние.",
                        "open_limit")
    today = await s.scalar(select(func.coalesce(func.sum(Deal.amount_rub), 0)).where(
        Deal.api_client_id == client.id, Deal.created_at >= day_start(),
        Deal.status.in_(OPEN + ("completed",))))
    if Decimal(today) + amount_rub > client.daily_rub:
        raise DealError(f"Дневной лимит API {money.fmt(client.daily_rub)} ₽: сегодня уже {money.fmt(Decimal(today))} ₽",
                        "daily_limit")


def _api_event(s: AsyncSession, d: Deal | None) -> None:
    """An API order changed its status: its webhook is queued in the same transaction, so a client gets every
    status — also the ones that last less than the delivery task's tick (a receipt confirmed at once)."""
    if d is not None and d.api_client_id is not None and d.api_notified != d.status:
        from bot.models import ApiEvent
        s.add(ApiEvent(client_id=d.api_client_id, deal_id=d.id, status=d.status))
        d.api_notified = d.status


async def _move(s: AsyncSession, deal_id: int, frm: tuple[str, ...], to: str, **values) -> Deal | None:
    """Atomic status transition. Returns fresh deal or None if status already changed."""
    values["closed_at"] = None if to in OPEN else now()
    res = await s.execute(
        update(Deal).where(Deal.id == deal_id, Deal.status.in_(frm)).values(status=to, **values)
    )
    if res.rowcount != 1:
        return None
    d = await s.get(Deal, deal_id, populate_existing=True)
    _api_event(s, d)
    return d


async def mark_paid(s: AsyncSession, deal_id: int, buyer_id: int, file_id: str) -> Deal | None:
    d = await s.get(Deal, deal_id)
    if not d or d.buyer_id != buyer_id:
        return None
    res = await s.execute(update(Deal).where(
        Deal.id == deal_id, Deal.buyer_id == buyer_id,
        Deal.status == "waiting_payment", Deal.expires_at > now(),
    ).values(status="paid", receipt_file_id=file_id, paid_at=now(), reminded=False)
        .execution_options(synchronize_session=False))
    if res.rowcount != 1:
        return None
    d = await s.get(Deal, deal_id, populate_existing=True)
    _api_event(s, d)
    return d


async def expire(s: AsyncSession, deal_id: int) -> Deal | None:
    """Payment time is over. The seller's funds stay frozen for late_hold_minutes: a buyer who paid at
    the last minute can still upload the receipt and the seller cannot withdraw that money meanwhile."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    hold = settings.num("late_hold_minutes") if d is not None and frozen(d) else 0
    d = await _move(s, deal_id, ("waiting_payment",), "expired", close_reason="expired", funds_held=hold > 0,
                    hold_until=now() + timedelta(minutes=hold) if hold else None)
    if d and not hold and frozen(d):
        await money.unfreeze(s, d.seller_id, d.seller_debit, f"deal:{d.id}")
    return d


async def release_hold(s: AsyncSession, deal_id: int) -> Deal | None:
    """Hold is over and no receipt came: give the seller the funds back (once)."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if not d or d.status != "expired" or not d.funds_held:
        return None
    d.funds_held = False
    await money.unfreeze(s, d.seller_id, d.seller_debit, f"deal:{d.id}")
    return d


def late_deadline(d: Deal) -> datetime | None:
    """Until when a buyer may still upload a receipt for an expired deal."""
    if d.status != "expired" or d.closed_at is None:
        return None
    return aware(d.closed_at) + timedelta(minutes=settings.num("late_minutes"))


async def reopen_late(s: AsyncSession, deal_id: int, buyer_id: int, file_id: str) -> Deal:
    """Buyer paid but the timer ran out: re-freeze seller funds and return the deal to the seller
    for a normal check (confirm or dispute)."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if not d or d.buyer_id != buyer_id or d.status != "expired":
        raise DealError("Сделка уже изменена")
    deadline = late_deadline(d)
    if deadline is None or now() > deadline:
        raise DealError("Срок загрузки чека истёк. Напишите в поддержку.", "late")
    if not d.funds_held and frozen(d):  # hold already released: freeze again if the seller still has the money
        try:
            await money.freeze(s, d.seller_id, d.seller_debit, f"deal:{d.id}")
        except money.NotEnough:
            raise DealError("У продавца больше нет свободных средств для этой сделки. "
                            "Чек передан в поддержку.", "no_funds")
    return await _move(s, d.id, ("expired",), "paid", receipt_file_id=file_id, paid_at=now(), reminded=False,
                       close_reason=None, funds_held=False, hold_until=None)


async def open_dispute(s: AsyncSession, deal_id: int, seller_id: int, reason: str,
                       files: list, amount_rub: Decimal | None) -> Deal | None:
    d = await s.get(Deal, deal_id)
    if not d or checker(d) != seller_id:
        return None
    return await _move(s, deal_id, ("paid",), "dispute",
                       dispute_reason=reason, dispute_files=files, dispute_amount_rub=amount_rub)


def buyer_dispute_at(d: Deal) -> datetime | None:
    """When the buyer may open a dispute on an unconfirmed deal."""
    if d.status != "paid" or d.paid_at is None:
        return None
    return aware(d.paid_at) + timedelta(minutes=settings.num("confirm_minutes"))


async def buyer_dispute(s: AsyncSession, deal_id: int, buyer_id: int) -> Deal | None:
    d = await s.get(Deal, deal_id, populate_existing=True)
    at = buyer_dispute_at(d) if d and d.buyer_id == buyer_id else None
    if at is None or now() < at:
        return None
    return await _move(s, deal_id, ("paid",), "dispute", dispute_reason="buyer_no_confirm")


async def escalate_unanswered(s: AsyncSession, deal_id: int) -> Deal | None:
    return await _move(s, deal_id, ("paid",), "dispute", dispute_reason="seller_timeout")


async def add_evidence(s: AsyncSession, deal_id: int, uid: int, item: list) -> Deal:
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if not d or uid not in (d.buyer_id, d.seller_id, d.operator_id) or d.status != "dispute":
        raise DealError("Спор уже закрыт")
    role = "buyer" if uid == d.buyer_id else "seller"  # the operator of a Bybit order speaks for the seller side
    if sum(1 for f in d.dispute_files or [] if (f[2] if len(f) > 2 else "seller") == role) >= MAX_EVIDENCE:
        raise DealError(f"Не более {MAX_EVIDENCE} материалов от одной стороны")
    d.dispute_files = list(d.dispute_files or []) + [[*item, role]]
    return d


async def complete(s: AsyncSession, deal_id: int, frm=("paid", "dispute"), actual_rub: Decimal | None = None,
                   reason: str = "confirmed", operator_debt: bool = True) -> Deal | None:
    """Release coins to buyer. actual_rub re-prices the deal by the amount really received. operator_debt=False: a
    Bybit order whose USDT never reached the operator (the merchant did not release them) — the buyer is still
    credited, but the operator owes nothing: the platform carries it and settles with the merchant."""
    from bot.services import events, operators, teams  # they build on this module's callers, not the other way round
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if not d or d.status not in frm:
        return None
    seller = await s.get(User, d.seller_id) if d.seller_id else None
    team = await teams.of_user(s, seller)
    for uid in sorted({d.buyer_id, d.seller_id, *([team.leader_id] if team else [])} - {None}):
        await money.lock(s, uid)
    old_debit = d.seller_debit
    values = {}
    if actual_rub is not None and actual_rub != d.amount_rub:
        if not actual_rub.is_finite() or actual_rub <= 0 or actual_rub.as_tuple().exponent < -2:
            raise DealError("Некорректная фактическая сумма")
        qt = requote(d, actual_rub)
        diff = qt.seller_debit - old_debit if frozen(d) else Decimal(0)
        if diff > 0:
            try:
                await money.freeze(s, d.seller_id, diff, f"deal:{d.id}")
            except money.NotEnough:
                raise DealError("У продавца недостаточно средств для пересчёта")
        elif diff < 0:
            await money.unfreeze(s, d.seller_id, -diff, f"deal:{d.id}")
        values = dict(amount_rub=actual_rub, seller_debit=qt.seller_debit,
                      buyer_credit=qt.buyer_credit, platform_fee=qt.platform_fee)
    d = await _move(s, deal_id, frm, "completed", close_reason=reason, **values)
    if d is None:
        return None
    ref = f"deal:{d.id}"
    if frozen(d):
        await money.spend_frozen(s, d.seller_id, d.seller_debit, ref)
    elif d.operator_id and d.bybit_url and operator_debt:  # a Bybit order: its USDT came to the operator — he owes them
        op = await operators.accrue(s, d.operator_id, d.seller_debit, ref)
        operators.log(s, d.operator_id, d, "completed", f"долг +{money.usdt(d.seller_debit)} USDT, всего "
                                                        f"{money.usdt(op.debt)} USDT")
    await money.add(s, d.buyer_id, d.buyer_credit, "deal_buy", ref)
    money.platform(s, d.platform_fee, "deal_fee", ref)
    if got := await teams.bonus(s, d):  # the merchant's team leader: his percent out of the platform's fee
        team, fee = got
        await money.add_team(s, team.leader_id, fee, ref)  # onto his team balance, he moves it to the main one
        money.platform(s, -fee, "team_fee", ref)
        d.team_id, d.team_fee = team.id, fee
        events.add(s, f"team:{team.id}", "fee", f"Тимлиду +{money.usdt(fee)} USDT · сделка #{d.id} на "
                                                f"{money.fmt(d.amount_rub)} ₽", team.leader_id, notice=True)
    return d


async def change_amount(s: AsyncSession, deal_id: int, amount_rub: Decimal) -> tuple[Deal, Decimal]:
    """An admin changes the amount of an open deal: re-priced by the deal's own terms; the merchant's frozen USDT
    follow (more frozen or released). Returns (deal, old amount). Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if not d or d.status not in OPEN:
        raise DealError("Сделка уже закрыта")
    if not amount_rub.is_finite() or not 0 < amount_rub < Decimal("100000000") or amount_rub.as_tuple().exponent < -2:
        raise DealError("Некорректная сумма")
    old = d.amount_rub
    try:
        q = requote(d, amount_rub)
    except ValueError:
        raise DealError("На этой сумме площадка ушла бы в минус — проверьте условия сделки")
    if frozen(d) and d.seller_id and d.status in FUNDED:  # the merchant's USDT are frozen for this deal
        diff = q.seller_debit - d.seller_debit
        try:
            if diff > 0:
                await money.freeze(s, d.seller_id, diff, f"deal:{d.id}")
            elif diff < 0:
                await money.unfreeze(s, d.seller_id, -diff, f"deal:{d.id}")
        except money.NotEnough:
            raise DealError(f"У мерчанта не хватает свободных USDT: нужно ещё {money.usdt(diff)}")
    d.amount_rub, d.seller_debit, d.buyer_credit, d.platform_fee = amount_rub, q.seller_debit, q.buyer_credit, \
        q.platform_fee
    return d, old


async def extend(s: AsyncSession, deal_id: int, minutes: int) -> Deal:
    """An admin gives more time to the current stage (payment, search, requisites). Does not commit."""
    d = await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    if not d or d.status not in UNPAID:
        raise DealError("Продлить можно только до оплаты")
    d.expires_at = max(aware(d.expires_at), now()) + timedelta(minutes=minutes)
    return d


async def cancel(s: AsyncSession, deal_id: int, frm: tuple[str, ...], to: str = "cancelled",
                 reason: str = "buyer_cancel") -> Deal | None:
    # Match complete(): lock the deal before touching its seller's frozen funds.
    await s.get(Deal, deal_id, with_for_update=True, populate_existing=True)
    d = await _move(s, deal_id, frm, to, close_reason=reason)
    if d and frozen(d):
        await money.unfreeze(s, d.seller_id, d.seller_debit, f"deal:{d.id}")
    return d


async def on_ban(s: AsyncSession, uid: int) -> tuple[list[Deal], list[Deal]]:
    """A banned user can no longer act: cancel unpaid deals (so nobody transfers money to them)
    and hand paid deals where they are the seller to the administration."""
    from bot.services import orders  # orders builds on this module
    cancelled, disputed = [], []
    for did in (await s.scalars(select(Deal.id).where(
            Deal.status.in_(orders.REQUEST), Deal.buyer_id == uid))).all():
        if d := await orders.cancel(s, did, "void", "ban_void"):
            cancelled.append(d)
    for did in (await s.scalars(select(Deal.id).where(Deal.status.in_(("assigned", "checking")),
                                                      Deal.seller_id == uid))).all():
        await orders.release(s, did)  # the request goes back to other merchants
    rows = (await s.scalars(select(Deal.id).where(
        Deal.status == "waiting_payment", (Deal.seller_id == uid) | (Deal.buyer_id == uid)))).all()
    for did in rows:
        if d := await cancel(s, did, ("waiting_payment",), "void", "ban_void"):  # not the buyer's fault
            cancelled.append(d)
    for did in (await s.scalars(select(Deal.id).where(Deal.status == "paid", Deal.seller_id == uid))).all():
        if d := await _move(s, did, ("paid",), "dispute", dispute_reason="seller_banned"):
            disputed.append(d)
    # an operator: an order he accepted goes back to the other operators, a payment he had to check — to the admins
    _, closed = await orders.drop_operator(s, uid)
    cancelled += closed
    for did in (await s.scalars(select(Deal.id).where(Deal.status == "paid", Deal.via_bybit,
                                                      Deal.operator_id == uid))).all():
        if d := await _move(s, did, ("paid",), "dispute", dispute_reason="seller_banned"):
            disputed.append(d)
    return cancelled, disputed
