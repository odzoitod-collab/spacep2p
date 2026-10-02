"""Operation history and admin alert outbox.

An event is added to the same transaction as the change it describes, so it is never lost:
if Telegram is down, tasks.deliver_alerts() retries unsent alerts later.
"""
import asyncio
from datetime import timedelta

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Event, now
from bot.services import settings

MAX_ATTEMPTS = 360  # ~1 hour of retries every 10 s; undelivered ones stay visible on the dashboard


def add(s: AsyncSession, ref: str, kind: str, text: str, user_id: int | None = None, alert: bool = False,
        notice: bool = False) -> Event:
    """alert: a problem an admin must see. notice: routine step (deal opened, paid, completed...) that goes to
    the log chat only while the "log_all" setting is on."""
    if notice and not alert:
        alert = settings.get("log_all") == "1"
    ev = Event(ref=ref, kind=kind, text=text, user_id=user_id, alert=alert, notice=notice)
    s.add(ev)
    return ev


async def alert_once(s: AsyncSession, ref: str, kind: str, text: str, user_id: int | None = None,
                     minutes: int = 60) -> Event | None:
    """Alert unless the same ref/kind was already alerted recently (repeating background checks)."""
    seen = await s.scalar(select(exists().where(
        Event.ref == ref, Event.kind == kind, Event.created_at > now() - timedelta(minutes=minutes))))
    return None if seen else add(s, ref, kind, text, user_id, alert=True)


async def history(s: AsyncSession, ref: str, limit: int = 30) -> list[Event]:
    """The latest `limit` events of an operation, oldest first."""
    rows = (await s.scalars(select(Event).where(Event.ref == ref).order_by(Event.id.desc()).limit(limit))).all()
    return list(reversed(rows))


async def outbox(s: AsyncSession, limit: int = 30) -> list[Event]:
    return list((await s.scalars(select(Event).where(
        Event.alert, Event.sent_at.is_(None), Event.attempts < MAX_ATTEMPTS).order_by(Event.id).limit(limit))).all())


_wake: asyncio.Event | None = None


def kick() -> None:
    """New alerts were committed: let the delivery task run now instead of on its next tick."""
    if _wake is not None:
        _wake.set()


async def wait(timeout: float) -> None:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    try:
        await asyncio.wait_for(_wake.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    _wake.clear()
