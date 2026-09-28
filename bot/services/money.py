"""Deal math and balance operations.

Ledger row: delta = change of total holdings (balance + frozen), frozen_delta = change of frozen.
So per user: sum(delta) == balance + frozen, sum(frozen_delta) == frozen, and the available
balance moved by (delta - frozen_delta). freeze/unfreeze are journaled with delta 0.

Callers own the transaction; all functions lock the user row (SELECT ... FOR UPDATE).
"""
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Ledger, User

Q = Decimal("0.000001")
KOP = Decimal("0.01")
HUNDRED = Decimal(100)


class NotEnough(Exception):
    pass


@dataclass(frozen=True)
class Quote:
    usdt: Decimal
    seller_debit: Decimal
    buyer_credit: Decimal
    platform_fee: Decimal


def seller_debit(amount_rub: Decimal, rate: Decimal, seller_pct: Decimal) -> Decimal:
    return (amount_rub / rate * (1 - seller_pct / HUNDRED)).quantize(Q, ROUND_UP)


def quote(amount_rub: Decimal, rate: Decimal, seller_pct: Decimal, platform_pct: Decimal) -> Quote:
    """10000 RUB, rate 100, 5% / 6% -> usdt 100, seller pays 95, buyer gets 94, platform keeps 1."""
    if platform_pct < seller_pct or rate <= 0:
        # the platform would pay the difference out of its own pocket on every deal
        raise ValueError("platform_pct must be >= seller_pct and rate > 0")
    usdt = amount_rub / rate
    debit = seller_debit(amount_rub, rate, seller_pct)
    credit = (usdt * (1 - platform_pct / HUNDRED)).quantize(Q, ROUND_DOWN)
    return Quote(usdt.quantize(Q, ROUND_DOWN), debit, credit, debit - credit)


def max_rub(balance: Decimal, rate: Decimal, seller_pct: Decimal) -> Decimal:
    """Largest RUB amount whose seller debit fits in balance."""
    k = 1 - seller_pct / HUNDRED
    if balance <= 0 or k <= 0:
        return Decimal(0)
    rub = (balance / k * rate).quantize(KOP, ROUND_DOWN)
    while rub > 0 and seller_debit(rub, rate, seller_pct) > balance:
        rub -= KOP
    return rub


def fmt(v: Decimal, places: int = 2) -> str:
    q = Decimal(1).scaleb(-places)
    s = f"{v.quantize(q, ROUND_DOWN):,.{places}f}".replace(",", " ")
    return s.rstrip("0").rstrip(".") if "." in s else s


def usdt(v: Decimal) -> str:
    return fmt(v, 6 if v and abs(v) < Decimal("0.01") else 2)


async def lock(s: AsyncSession, uid: int) -> User:
    user = await s.get(User, uid, with_for_update=True, populate_existing=True)
    if user is None:
        raise LookupError(uid)
    return user


def _log(s: AsyncSession, uid: int | None, delta: Decimal, kind: str, ref: str, note: str | None = None,
         frozen_delta: Decimal = Decimal(0)) -> None:
    s.add(Ledger(user_id=uid, delta=delta, frozen_delta=frozen_delta, kind=kind, ref=ref, note=note))


async def add(s: AsyncSession, uid: int, delta: Decimal, kind: str, ref: str = "", note: str | None = None) -> User:
    if not delta.is_finite():
        raise ValueError("Invalid amount")
    u = await lock(s, uid)
    if u.balance + delta < 0:
        raise NotEnough
    u.balance += delta
    _log(s, uid, delta, kind, ref, note)
    return u


async def freeze(s: AsyncSession, uid: int, amount: Decimal, ref: str) -> None:
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Invalid freeze amount")
    u = await lock(s, uid)
    if u.balance < amount:
        raise NotEnough
    u.balance -= amount
    u.frozen += amount
    _log(s, uid, Decimal(0), "freeze", ref, frozen_delta=amount)


async def unfreeze(s: AsyncSession, uid: int, amount: Decimal, ref: str) -> None:
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Invalid unfreeze amount")
    u = await lock(s, uid)
    if u.frozen < amount:
        raise NotEnough
    u.frozen -= amount
    u.balance += amount
    _log(s, uid, Decimal(0), "unfreeze", ref, frozen_delta=-amount)


async def spend_frozen(s: AsyncSession, uid: int, amount: Decimal, ref: str) -> None:
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Invalid spend amount")
    u = await lock(s, uid)
    if u.frozen < amount:
        raise NotEnough
    u.frozen -= amount
    _log(s, uid, -amount, "deal_sell", ref, frozen_delta=-amount)


def platform(s: AsyncSession, amount: Decimal, kind: str, ref: str) -> None:
    _log(s, None, amount, kind, ref)
