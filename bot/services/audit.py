from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import Audit
from bot.services import events


# actions the admin chat gets a post about (money, access, verdicts, keys); the rest stays in the journal only
IMPORTANT = {"ban", "unban", "balance", "balance_mass", "setting", "resolve", "deal_amount", "wd_confirm", "wd_refund",
             "operator_writeoff", "operator_debt", "admin_grant", "admin_revoke", "merchant_pct", "buyer_terms",
             "bsc_key", "bsc_cancel", "rating", "deposit_unlock", "api_terms", "card_ban", "broadcast"}


def log(s: AsyncSession, actor_id: int, action: str, target: str = "", details: str = "", alert: bool = True) -> None:
    """Audit record; committed together with the change it describes. An IMPORTANT action is also a post of its own
    in the admin chat's «Действия админов» (handlers.logchat); every action is in the journal (/admin → «Журнал»)."""
    s.add(Audit(actor_id=actor_id, action=action, target=target[:64], details=details))
    if alert and action in IMPORTANT:
        events.add(s, f"adm:{actor_id}", f"a:{action}"[:32], "\x1f".join([action, target[:64], details[:1000]]),
                   actor_id, alert=True)
