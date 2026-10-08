"""PG acceptance uses unique synthetic records, then removes only those records."""

import asyncio
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from babata.config import Settings
from babata.profiles import Profiles
from babata.shared import SharedArchive, SharedMessage, SharedThread


async def main():
    engine = create_async_engine(Settings().database_url)
    archive, profiles = SharedArchive(engine), Profiles(engine)
    user, external = "shared-test-" + uuid4().hex, uuid4().hex
    tid = "mac:" + external
    try:
        data = SharedThread(
            source="mac",
            external_id=external,
            title="Test",
            messages=[
                SharedMessage(
                    id=str(i), role="user", text="独特检索词" if i == 3 else "context", position=i
                )
                for i in range(6)
            ],
        )
        await archive.ingest(user, data)
        await archive.ingest(user, data)
        assert (await archive.stats(user))[0]["messages"] == 6
        found = await archive.search(user, "独特检索词")
        assert len(found) == 1 and found[0]["message_id"] == "3"
        assert not await archive.search("other", "独特检索词")
        assert not await archive.search(user, "%")  # literal search, not a wildcard
        page = await archive.read(user, tid, message_id="3", limit=3)
        assert [m["id"] for m in page["messages"]] == ["1", "2", "3"]
        try:
            await archive.ingest("other", data)
            raise AssertionError("Cross-user overwrite allowed")
        except ValueError:
            pass
        rev = await profiles.put(user, "first", [], expected_revision=0)
        rev2 = await profiles.put(user, "second", [], expected_revision=rev)
        try:
            await profiles.put(user, "stale", [], expected_revision=rev)
            raise AssertionError("Stale overwrite allowed")
        except ValueError:
            pass
        assert (await profiles.snapshot(user))["revision"] == rev2
        assert await profiles.get(user) == "second"
        print(
            "PASS: idempotent mirror, ownership, literal search, source position, revision conflict"
        )
    finally:
        async with engine.begin() as c:
            for statement in [
                "DELETE FROM babata_shared_messages WHERE thread_id=:t",
                "DELETE FROM babata_shared_threads WHERE id=:t",
                "DELETE FROM babata_profile_revisions WHERE user_id=:u",
                "DELETE FROM babata_user_profiles WHERE user_id=:u",
            ]:
                await c.execute(text(statement), {"u": user, "t": tid})
        await engine.dispose()


asyncio.run(main())
