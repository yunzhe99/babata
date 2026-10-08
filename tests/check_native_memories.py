"""PostgreSQL mirror acceptance: revisions, removal, isolation, and source lookup."""

import asyncio
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from babata.config import Settings
from babata.native_memories import MemorySnapshot
from babata.shared import SharedArchive


async def main():
    engine = create_async_engine(Settings().database_url)
    store = SharedArchive(engine)
    await store.initialize()
    user = "native-test-" + uuid4().hex

    def body(source, content):
        return MemorySnapshot(
            source=source,
            files=[
                {
                    "path": "MEMORY.md",
                    "content": content,
                    "modified_at": 1789780000,
                }
            ],
        )

    try:
        assert (await store.memories.ingest(user, body("mac", "整理标签旧%")))["changed"] == 1
        assert (await store.memories.ingest(user, body("mac", "整理标签旧%")))["changed"] == 0
        await store.memories.ingest(user, body("tokyo", "整理标签东京"))
        assert len(await store.search(user, "整理标签")) == 2
        assert len(await store.search(user, "%")) == 1
        assert not await store.search("different-user", "整理标签")
        await store.memories.ingest(user, body("mac", "整理标签新"))
        assert not await store.search(user, "旧")
        row = await store.read(user, "memory:mac:MEMORY.md")
        assert row["revision"] == 2 and row["messages"][0]["text"] == "整理标签新"
        assert row["reference_only"]
        await store.memories.ingest(user, MemorySnapshot(source="mac", files=[]))
        assert not await store.search(user, "整理标签", "mac")
        assert len(await store.search(user, "整理标签", "tokyo")) == 1
        try:
            await store.read(user, "memory:mac:MEMORY.md")
            raise AssertionError("Deleted mirror still visible")
        except ValueError:
            pass
        async with engine.connect() as c:
            count = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM babata_native_memory_versions "
                        "WHERE user_id=:u AND source='mac'"
                    ),
                    {"u": user},
                )
            ).scalar_one()
            assert count == 3
        print(
            "PASS: native memory mirror idempotency, revisions, deletion, source and user isolation"
        )
    finally:
        async with engine.begin() as c:
            for table in ("babata_native_memory_versions", "babata_native_memories"):
                await c.execute(text("DELETE FROM " + table + " WHERE user_id=:u"), {"u": user})
        await engine.dispose()


asyncio.run(main())
