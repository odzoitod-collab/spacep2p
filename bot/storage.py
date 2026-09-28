"""FSM storage in the main database: a restart does not lose what the user was typing
(amount, receipt upload, card details).

Only one bot process runs per database (pg advisory lock in __main__), so an in-memory copy of each key
is authoritative: reads are served from memory, writes go to the database first and then to the copy."""
import asyncio
import copy
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage, StateType, StorageKey
from sqlalchemy.exc import IntegrityError

from bot.models import FsmState, Session


def _key(k: StorageKey) -> str:
    return f"{k.bot_id}:{k.chat_id}:{k.user_id}:{k.thread_id or ''}:{k.business_connection_id or ''}:{k.destiny}"


class DbStorage(BaseStorage):
    def __init__(self) -> None:
        self._cache: dict[str, tuple[str | None, dict[str, Any]]] = {}

    async def _load(self, key: StorageKey) -> tuple[str | None, dict[str, Any]]:
        k = _key(key)
        if k not in self._cache:
            async with Session() as s:
                row = await s.get(FsmState, k)
                self._cache[k] = (row.state, dict(row.data or {})) if row else (None, {})
        return self._cache[k]

    async def _write(self, key: StorageKey, **fields) -> None:
        k = _key(key)
        for attempt in range(2):  # two parallel first writes for one key: second one retries as update
            async with Session() as s:
                row = await s.get(FsmState, k)
                if row is None:
                    row = FsmState(key=k, state=None, data={})
                    s.add(row)
                for name, v in fields.items():
                    setattr(row, name, v)
                try:
                    await s.commit()
                except IntegrityError:
                    if attempt:
                        raise
                    continue
                self._cache[k] = (row.state, dict(row.data or {}))
                return

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        await self._write(key, state=state.state if isinstance(state, State) else state)

    async def get_state(self, key: StorageKey) -> str | None:
        return (await self._load(key))[0]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        await self._write(key, data=copy.deepcopy(dict(data)))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return copy.deepcopy((await self._load(key))[1])

    async def close(self) -> None:
        self._cache.clear()


class UserIsolation(BaseEventIsolation):
    """Updates of one user are handled one at a time, in arrival order. Without it a command and a message
    sent together race: both read the same screen id and dialog state, and the last commit wins.
    Locks exist only while a user has updates in flight."""

    def __init__(self) -> None:
        self._locks: dict[int, list] = {}  # user_id -> [lock, holders + waiters]

    @asynccontextmanager
    async def lock(self, key: StorageKey) -> AsyncGenerator[None, None]:
        entry = self._locks.setdefault(key.user_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self._locks[key.user_id]

    async def close(self) -> None:
        self._locks.clear()
