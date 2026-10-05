"""Operators of Bybit-order deals and their debt.

Who is an operator: active rows of `operators` (an admin adds them in the panel) plus OPERATOR_IDS from .env; if there
are none at all, the admins. An operator enters the merchant's Bybit order and confirms the buyer's payment: the
order's USDT arrive on the operator's own Bybit account while the platform credits the buyer. So every confirmed
deal adds its seller_debit to the operator's debt; he repays it with an xRocket invoice (a Deposit with
purpose="debt") or from his balance in the bot. Events of an operator are logged under ref op:<user id>.
"""
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.models import Operator
from bot.services import admins, events, money


async def ids(s: AsyncSession) -> list[int]:
    rows = (await s.scalars(select(Operator.user_id).where(Operator.active).order_by(Operator.created_at))).all()
    out = list(dict.fromkeys([*config.operator_ids, *rows]))
    return out or admins.ids()


async def is_operator(s: AsyncSession, uid: int) -> bool:
    return uid in await ids(s)


async def row(s: AsyncSession, uid: int, lock: bool = False) -> Operator:
    """The operator's row, created on first use (an operator from .env or an admin has none until his first deal)."""
    op = await s.get(Operator, uid, with_for_update=lock, populate_existing=lock)
    if op is None:
        try:  # two transactions may create it at once: the loser just reads the winner's row
            async with s.begin_nested():
                s.add(Operator(user_id=uid, active=uid in config.operator_ids, debt=Decimal(0)))
        except IntegrityError:
            pass
        op = await s.get(Operator, uid, with_for_update=lock, populate_existing=True)
    return op


async def accrue(s: AsyncSession, uid: int, amount: Decimal, ref: str) -> Operator:
    """A Bybit order he confirmed: its USDT came to him, he owes them to the platform. Does not commit."""
    op = await row(s, uid, lock=True)
    op.debt += amount
    events.add(s, f"op:{uid}", "debt", f"Долг +{money.usdt(amount)} USDT ({ref.replace('deal:', 'сделка #')}) · "
                                       f"всего {money.usdt(op.debt)} USDT", uid, notice=True)
    return op


async def repay(s: AsyncSession, uid: int, amount: Decimal, how: str) -> tuple[Decimal, Decimal]:
    """Lower the debt by up to `amount`. Returns (repaid, excess over the debt). Does not commit."""
    op = await row(s, uid, lock=True)
    paid = min(amount, op.debt)
    op.debt -= paid
    events.add(s, f"op:{uid}", "repaid", f"Погашено {money.usdt(paid)} USDT ({how}) · осталось {money.usdt(op.debt)} USDT",
               uid, notice=True)
    return paid, amount - paid


async def total_debt(s: AsyncSession) -> Decimal:
    return Decimal(await s.scalar(select(func.coalesce(func.sum(Operator.debt), 0))))


def log(s: AsyncSession, operator_id: int, deal, action: str, details: str = "") -> None:
    """One operator action on a Bybit order (accepted, gave requisites, returned, no requisites...): a post of its own
    in the admin chat's «Операторы» topic (handlers.logchat), next to the deal's history. Does not commit."""
    events.add(s, f"opa:{operator_id}", action[:32], f"{deal.id}\x1f{details}"[:1000], operator_id, alert=True)
