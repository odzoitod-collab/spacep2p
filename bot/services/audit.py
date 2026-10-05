from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Audit
from bot.services import events


def log(s: AsyncSession, actor_id: int, action: str, target: str = "", details: str = "") -> None:
    """Audit record; committed together with the change it describes. The admin chat gets it as a post of its own
    in «Действия админов» (handlers.logchat)."""
    s.add(Audit(actor_id=actor_id, action=action, target=target[:64], details=details))
    events.add(s, f"adm:{actor_id}", f"a:{action}"[:32], "\x1f".join([action, target[:64], details[:1000]]),
               actor_id, alert=True)
