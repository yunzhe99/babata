"""Run only inside the disposable integration stack, while its gateway is stopped."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from babata.config import Settings
from babata.profiles import Profiles


async def main():
    engine = create_async_engine(Settings().database_url)
    profiles = Profiles(engine)
    await profiles.initialize()
    try:
        user = "memory-store-race"
        await profiles.put(user, "- Original", [])
        base = await profiles.snapshot(user)
        await profiles.enqueue(user, "s", "old pending fact")
        job = await profiles.next_job()
        assert job["user_id"] == user
        await profiles.put(user, "- Manual correction", [])
        assert not await profiles.apply_job(job, base["revision"], "- Stale overwrite", [{}])
        assert await profiles.get(user) == "- Manual correction"

        # An older HTTP request can finish after a newer request already updated memory.
        base = await profiles.snapshot(user)
        await profiles.enqueue(user, "new", "new fact")
        newer = await profiles.next_job()
        assert await profiles.apply_job(newer, base["revision"], "- New fact", [{}])
        await profiles.enqueue(user, "late", "old fact", datetime.now(UTC) - timedelta(days=1))
        older = await profiles.next_job()
        current = await profiles.snapshot(user)
        assert not await profiles.apply_job(older, current["revision"], "- Old fact", [{}])
        assert await profiles.get(user) == "- New fact"

        # Inference racing with clear cannot recreate the cleared background.
        await profiles.enqueue(user, "clear-race", "pending")
        pending = await profiles.next_job()
        revision = (await profiles.snapshot(user))["revision"]
        await profiles.delete(user)
        assert not await profiles.apply_job(pending, revision, "- Resurrected", [{}])
        assert await profiles.get(user) == ""

        # Leave one durable job for the next gateway startup to process.
        await profiles.enqueue(
            "memory-restart-pending", "s", "我长期喜欢茉莉花茶，请记住我的饮茶偏好。"
        )
        async with engine.connect() as connection:
            statuses = (
                (
                    await connection.execute(
                        text("SELECT status FROM babata_memory_jobs WHERE user_id=:u ORDER BY id"),
                        {"u": user},
                    )
                )
                .scalars()
                .all()
            )
        assert statuses == ["cancelled", "updated", "superseded", "cancelled"], statuses
        print("PASS: manual changes win, late results cannot overwrite, pending job is durable")
    finally:
        await engine.dispose()


asyncio.run(main())
