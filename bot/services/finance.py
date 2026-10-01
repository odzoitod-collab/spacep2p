"""The platform's money at a glance: what it holds, what it owes users, what is profit and can be taken out.

Assets:       xRocket app balance (pays every withdrawal) + USDT on the auto-transfer wallet (read on chain) +
              TON deposits credited but not transferred yet.
Liabilities:  users' available balances + USDT frozen in deals + withdrawals debited but not paid yet.
Bybit:        USDT of Bybit-order deals arrive on the operators' Bybit accounts, outside the assets above, while the
              buyers are credited in the bot: move them to xRocket (shown separately, not counted).
Free:         assets − liabilities. This is what the owner can take out without touching users' money; it already
              contains the profit. If the auto-transfer wallet also holds personal money, it is counted too.
"""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Deal, Ledger, TonDeposit, TonOp, User, Withdrawal, now
from bot.services import settings, ton, xrocket

UNPAID = ("queued", "pending", "unknown", "sent")  # withdrawals debited from users, not paid out yet


@dataclass
class Snapshot:
    xrocket: Decimal | None  # None: xRocket did not answer
    wallet: Decimal | None  # USDT on the auto-transfer address; None: not an address / not readable
    wallet_kind: str  # "address" | "xrocket" | "none"
    unswept: Decimal
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

    @property
    def assets(self) -> Decimal:
        return (self.xrocket or 0) + (self.wallet or 0) + self.unswept

    @property
    def liabilities(self) -> Decimal:
        return self.users_available + self.users_frozen + self.unpaid

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
    target = settings.get("ton_sweep_address")
    wallet, kind = None, "none"
    if target == ton.XROCKET:
        kind = "xrocket"
    elif target and ton.enabled() and ton.chain is not None:
        kind = "address"
        try:
            wallet = await ton.chain.usdt_balance(ton.raw(target))
        except Exception:  # noqa: BLE001
            wallet = None
    deposited = await _sum(s, TonDeposit.amount)
    swept = await _sum(s, TonOp.amount, TonOp.kind == "sweep", TonOp.status.in_(("sent", "done")))
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
        xrocket=rocket, wallet=wallet, wallet_kind=kind, unswept=max(deposited - swept, Decimal(0)),
        users_available=await _sum(s, User.balance), users_frozen=await _sum(s, User.frozen),
        unpaid=Decimal(unpaid), unpaid_n=unpaid_n, queued=Decimal(queued), queued_n=queued_n,
        profit=profit, volume=volume, users=await s.scalar(select(func.count(User.id))),
        online=await s.scalar(select(func.count(User.id)).where(User.is_online)), bybit=bybit)
