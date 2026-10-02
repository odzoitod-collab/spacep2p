"""Teams: a leader applies, an admin approves; users who start the bot by the leader's link (?start=t<id>) join the
team, the team's chat gets every order request, and the leader earns pct (team_pct by default, 1%) of each completed
deal where a member is the merchant — paid by the platform out of its fee for that deal, never more than that fee.
Events of a team are logged under ref team:<id>."""
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Deal, Team, User
from bot.services import events, money, settings

STATUS = {"pending": "на рассмотрении", "approved": "работает", "rejected": "отклонена", "suspended": "приостановлена"}


def pct(team: Team) -> Decimal:
    return team.pct if team.pct is not None else settings.dec("team_pct")


async def of_user(s: AsyncSession, user: User | None) -> Team | None:
    """The working team this user belongs to (as a member or its leader)."""
    if user is None or user.team_id is None:
        return None
    team = await s.get(Team, user.team_id)
    return team if team is not None and team.status == "approved" else None


async def led_by(s: AsyncSession, uid: int) -> Team | None:
    return await s.scalar(select(Team).where(Team.leader_id == uid))


async def join(s: AsyncSession, user: User, team: Team) -> bool:
    """By the leader's link: only a user without a team joins, and only a working team. Does not commit."""
    if team.status != "approved" or user.team_id is not None or user.id == team.leader_id:
        return False
    user.team_id = team.id
    events.add(s, f"team:{team.id}", "joined", f"Новый участник: {user.name or '—'} (@{user.username or '—'}, "
                                               f"{user.id})", user.id, notice=True)
    return True


async def bonus(s: AsyncSession, d: Deal) -> tuple[Team, Decimal] | None:
    """(team, USDT to its leader) for this deal at completion, or None. The merchant is a member (not the leader);
    pct of the deal's USDT at its rate, capped by the platform's fee so the platform never pays out of pocket."""
    seller = await s.get(User, d.seller_id) if d.seller_id else None
    team = await of_user(s, seller)
    if team is None or team.leader_id == d.seller_id:
        return None
    amount = min((d.amount_rub / d.rate * pct(team) / 100).quantize(money.Q, ROUND_DOWN), d.platform_fee)
    return (team, amount) if amount > 0 else None


async def members(s: AsyncSession, team: Team) -> int:
    return await s.scalar(select(func.count(User.id)).where(User.team_id == team.id, User.id != team.leader_id))


async def stats(s: AsyncSession, team: Team, since=None) -> tuple[int, Decimal, Decimal]:
    """(completed deals of members, RUB, USDT earned by the leader) since a moment (None = all time)."""
    n, rub, fee = (await s.execute(select(func.count(Deal.id), func.coalesce(func.sum(Deal.amount_rub), 0),
                                          func.coalesce(func.sum(Deal.team_fee), 0)).where(
        Deal.team_id == team.id, Deal.status == "completed", *([Deal.closed_at >= since] if since else [])))).one()
    return n, Decimal(rub), Decimal(fee)
