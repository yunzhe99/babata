"""Isolated PostgreSQL acceptance for contextual peer updates, never executor jobs."""

import asyncio
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from babata.config import Settings
from babata.peer_updates import PeerUpdate, PeerUpdates


async def main():
    engine = create_async_engine(Settings().database_url)
    store = PeerUpdates(engine)
    await store.initialize()
    user = "peer-test-" + uuid4().hex
    try:
        async with engine.connect() as c:
            jobs_before = (
                await c.execute(
                    text("SELECT count(*) FROM babata_codex_jobs WHERE user_id=:u"), {"u": user}
                )
            ).scalar()
        update = PeerUpdate(topic="change", message="api_key=sk-" + "x" * 30, source_ref="mac:test")
        sent = await store.send(user, "mac", update)
        assert (await store.send(user, "mac", update))["id"] == sent["id"]
        # A fresh store instance reads the persistent inbox.
        inbox = await PeerUpdates(engine).inbox(user, "tokyo")
        assert len(inbox) == 1 and "sk-" not in inbox[0]["message"]
        assert inbox[0]["reference_only"] and inbox[0]["source_ref"] == "mac:test"
        assert not await store.inbox("another-user", "tokyo")
        assert not await store.inbox(user, "mac")
        assert await store.delivered("another-user", "tokyo", [sent["id"]]) == 0
        assert await store.delivered(user, "mac", [sent["id"]]) == 0
        assert await store.delivered(user, "tokyo", [sent["id"]]) == 1
        await store.send(user, "mac", update)
        assert not await store.inbox(user, "tokyo")  # Retries cannot re-open a delivered update.
        assert len(await store.inbox(user, "tokyo", pending=False)) == 1
        await store.send(
            user, "tokyo", PeerUpdate(topic="reply", message="new fact", source_ref="tokyo:test")
        )
        assert len(await store.inbox(user, "mac")) == 1
        async with engine.connect() as c:
            assert (
                await c.execute(
                    text("SELECT count(*) FROM babata_codex_jobs WHERE user_id=:u"), {"u": user}
                )
            ).scalar() == jobs_before
        print("PASS: peer inbox persistence, retries, isolation, redaction; no task execution")
    finally:
        async with engine.begin() as c:
            await c.execute(text("DELETE FROM babata_peer_updates WHERE user_id=:u"), {"u": user})
        await engine.dispose()


asyncio.run(main())
