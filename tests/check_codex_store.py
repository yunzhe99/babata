"""Integration against PostgreSQL; deletes only this run's synthetic identities."""

import asyncio
from uuid import uuid4

from sqlalchemy import text

from babata.codex import CodexInbox
from babata.config import Settings
from babata.sessions import Sessions


async def main():
    sessions = Sessions(Settings().database_url)
    inbox = CodexInbox(sessions.engine)
    await inbox.initialize()
    name = "test-" + uuid4().hex
    user = name
    try:
        await inbox.register([{"name": name, "label": "合成测试"}])
        key = uuid4()
        jobs = await asyncio.gather(
            *[inbox.submit(user, "test", key, name, "synthetic inbox test") for _ in range(8)]
        )
        assert len({j["id"] for j in jobs}) == 1
        job_id = jobs[0]["id"]
        claimed = await asyncio.gather(inbox.claim([name]), inbox.claim([name]))
        assert sum(j is not None for j in claimed) == 1
        assert await inbox.claim(["unrelated"]) is None
        await inbox.update(job_id, "needs_input", question={"id": "q1", "text": "Test?"})
        assert not await inbox.answer("other", job_id, "q1", "yes")
        assert not await inbox.answer(user, job_id, "stale", "yes")
        assert await inbox.answer(user, job_id, "q1", "允许这次操作")
        assert not await inbox.answer(user, job_id, "q1", "拒绝这次操作")
        await inbox.update(job_id, "completed", result="synthetic result")
        assert len(await inbox.notifications(user, "test")) == 1
        await inbox.acknowledge(user, job_id, (await inbox.get(job_id))["updated_at"])
        await inbox.update(job_id, "completed", result="synthetic result")
        assert await inbox.notifications(user, "test") == []
        await inbox.submit(user, "test", uuid4(), name, "synthetic recovery")
        await inbox.claim([name])
        assert await inbox.recover([name]) == 1
        assert await inbox.claim([name]) is None
        print(
            "PostgreSQL inbox: deduplication, claim, isolation, "
            "question identity, ACK, recovery passed"
        )
    finally:
        async with sessions.engine.begin() as c:
            await c.execute(text("DELETE FROM babata_codex_jobs WHERE user_id=:u"), {"u": user})
            await c.execute(text("DELETE FROM babata_codex_targets WHERE name=:n"), {"n": name})
        await sessions.close()


if __name__ == "__main__":
    asyncio.run(main())
