"""SDK-managed PostgreSQL history; no separate memory layer."""

import asyncio
import hashlib
import json
from weakref import WeakValueDictionary

from agents.extensions.memory import SQLAlchemySession
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine


def session_key(user_id: str, session_id: str) -> str:
    identity = json.dumps([user_id, session_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode()).hexdigest()


class Sessions:
    def __init__(self, database_url: URL):
        self.engine = create_async_engine(database_url, pool_pre_ping=True)
        # v0.1 runs one process. Serialize complete turns, not just SQL writes.
        # Weak references avoid retaining a lock for every historical session.
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def initialize(self) -> None:
        # Trigger SDK schema creation once at startup. Reading creates no message.
        bootstrap = SQLAlchemySession("__schema_init__", engine=self.engine, create_tables=True)
        await bootstrap.get_items(limit=1)

    def get(self, key: str) -> SQLAlchemySession:
        return SQLAlchemySession(key, engine=self.engine, ensure_ascii=False)

    def lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()
