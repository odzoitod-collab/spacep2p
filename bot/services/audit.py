from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Audit


def log(s: AsyncSession, actor_id: int, action: str, target: str = "", details: str = "") -> None:
    """Audit record; committed together with the change it describes."""
    s.add(Audit(actor_id=actor_id, action=action, target=target[:64], details=details))
