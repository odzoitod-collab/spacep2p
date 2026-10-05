"""The platform's money at a glance: what it holds, what it owes users, what is profit and can be taken out.

Assets:       the xRocket app balance: every deposit lands there and every withdrawal is paid from it.
Liabilities:  users' available balances + team leaders' team balances + USDT frozen in deals + withdrawals debited
              but not paid yet.
Operators:    USDT of Bybit-order deals arrive on the operators' Bybit accounts while the buyers are credited in the
              bot: each operator owes them (services/operators.py) and repays to xRocket — a receivable, shown
              separately and not counted in the assets until it is repaid.
Free:         assets − liabilities. This is what the owner can take out without touching users' money; it already
              contains the profit.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Deal, Ledger, User, Withdrawal, now
from bot.services import operators, xrocket

UNPAID = ("queued", "pending", "unknown", "sent")  # withdrawals debited from users, not paid out yet


@dataclass
class Snapshot:
    xrocket: Decimal | None  # None: xRocket did not answer
    users_available: Decimal
    users_frozen: Decimal
    unpaid: Decimal
    unpaid_n: int
    queued: Decimal
    queued_n: int
    profit: dict[str, Decimal]  # 24h / 7d / 30d / all
    volume: dict[str, tuple[int, Decimal]]  # 24h / 7d: completed deals, RUB
    users: int
    online: int
    bybit: dict[str, Decimal]  # 24h / 7d / all: USDT received through Bybit orders of completed deals
    op_debt: Decimal = Decimal(0)  # operators owe for accepted Bybit orders, not repaid yet
    team_paid: Decimal = Decimal(0)  # paid to team leaders, all time
    users_team: Decimal = Decimal(0)  # team leaders' team balances, not moved to the main balance yet

    @property
    def assets(self) -> Decimal:
        return self.xrocket or Decimal(0)

    @property
    def liabilities(self) -> Decimal:
        return self.users_available + self.users_team + self.users_frozen + self.unpaid

    @property
    def free(self) -> Decimal:
        return self.assets - self.liabilities


async def _sum(s: AsyncSession, expr, *where) -> Decimal:
    return Decimal(await s.scalar(select(func.coalesce(func.sum(expr), 0)).where(*where)))


async def snapshot(s: AsyncSession) -> Snapshot:
    try:
        rocket = await xrocket.usdt_available(max_age=60, timeout=5)
    except Exception:  # noqa: BLE001 - shown as "unknown"
        rocket = None
    t = now()
    profit = {}
    for key, since in (("24h", t - timedelta(hours=24)), ("7d", t - timedelta(days=7)),
                       ("30d", t - timedelta(days=30)), ("all", None)):
        profit[key] = await _sum(s, Ledger.delta, Ledger.user_id.is_(None),
                                 *([Ledger.created_at >= since] if since else []))
    volume = {}
    for key, since in (("24h", t - timedelta(hours=24)), ("7d", t - timedelta(days=7))):
        n, rub = (await s.execute(select(func.count(Deal.id), func.coalesce(func.sum(Deal.amount_rub), 0)).where(
            Deal.status == "completed", Deal.closed_at >= since))).one()
        volume[key] = (n, Decimal(rub))
    bybit = {}
    for key, since in (("24h", t - timedelta(hours=24)), ("7d", t - timedelta(days=7)), ("all", None)):
        bybit[key] = await _sum(s, Deal.seller_debit, Deal.via_bybit, Deal.status == "completed",
                                *([Deal.closed_at >= since] if since else []))
    unpaid_n, unpaid = (await s.execute(select(func.count(Withdrawal.id), func.coalesce(
        func.sum(Withdrawal.amount - Withdrawal.fee), 0)).where(Withdrawal.status.in_(UNPAID)))).one()
    queued_n, queued = (await s.execute(select(func.count(Withdrawal.id), func.coalesce(
        func.sum(Withdrawal.amount - Withdrawal.fee), 0)).where(Withdrawal.status == "queued"))).one()
    return Snapshot(
        xrocket=rocket,
        users_available=await _sum(s, User.balance), users_frozen=await _sum(s, User.frozen),
        users_team=await _sum(s, User.team_balance),
        unpaid=Decimal(unpaid), unpaid_n=unpaid_n, queued=Decimal(queued), queued_n=queued_n,
        profit=profit, volume=volume, users=await s.scalar(select(func.count(User.id))),
        online=await s.scalar(select(func.count(User.id)).where(User.is_online)), bybit=bybit,
        op_debt=await operators.total_debt(s), team_paid=await _sum(s, Deal.team_fee, Deal.status == "completed"))
