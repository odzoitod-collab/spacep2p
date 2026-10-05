"""Who runs the bot.

Owners: ADMIN_IDS from .env — always admins; only they give and take the admin status. Admins: given by an owner in
the panel (user card → «Сделать админом») or by /addadmin in the admin chat, stored in settings ("admins"), so a
restart keeps them. An admin has the admin panel, the buttons and commands of the admin chat."""
from aiogram.filters import Filter
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import config
from bot.services import settings

KEY = "admins"


def granted() -> list[int]:
    return [int(x) for x in settings.raw(KEY).split(",") if x.strip().lstrip("-").isdigit()]


def ids() -> list[int]:
    return list(dict.fromkeys([*config.admin_ids, *granted()]))


def is_owner(uid: int) -> bool:
    return uid in config.admin_ids


def is_admin(uid: int) -> bool:
    return uid in config.admin_ids or uid in granted()


async def grant(s: AsyncSession, uid: int) -> bool:
    """False if he already is an admin. Does not commit."""
    if is_admin(uid):
        return False
    await settings.put(s, KEY, ",".join(map(str, [*granted(), uid])))
    return True


async def revoke(s: AsyncSession, uid: int) -> bool:
    """False if he is not a granted admin (owners stay owners). Does not commit."""
    if uid not in granted():
        return False
    await settings.put(s, KEY, ",".join(str(x) for x in granted() if x != uid))
    return True


def role(uid: int) -> str:
    return "владелец" if is_owner(uid) else "админ" if is_admin(uid) else ""


class IsAdmin(Filter):
    """Router filter: the sender is an admin now (not just when the bot started)."""

    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return u is not None and is_admin(u.id)
