"""One current background per user, with revision history and pending extraction jobs."""

import json

from sqlalchemy import text


class Profiles:
    def __init__(self, engine):
        self.engine = engine

    async def initialize(self):
        statements = [
            """CREATE TABLE IF NOT EXISTS babata_user_profiles (
                user_id TEXT PRIMARY KEY, content TEXT NOT NULL,
                sources JSONB NOT NULL DEFAULT '[]'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                revision BIGINT NOT NULL DEFAULT 0)""",
            """ALTER TABLE babata_user_profiles
                ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0""",
            """ALTER TABLE babata_user_profiles
                ADD COLUMN IF NOT EXISTS memory_watermark TIMESTAMPTZ""",
            """CREATE TABLE IF NOT EXISTS babata_profile_revisions (
                user_id TEXT NOT NULL, revision BIGINT NOT NULL, content TEXT NOT NULL,
                sources JSONB NOT NULL, changes JSONB NOT NULL DEFAULT '[]'::jsonb,
                kind TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, revision))""",
            """INSERT INTO babata_profile_revisions (user_id, revision, content, sources, kind)
                SELECT user_id, revision, content, sources, 'baseline' FROM babata_user_profiles
                ON CONFLICT DO NOTHING""",
            """CREATE TABLE IF NOT EXISTS babata_memory_jobs (
                id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, session_key TEXT NOT NULL,
                message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                error_type TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMPTZ)""",
            """CREATE INDEX IF NOT EXISTS babata_memory_jobs_pending
                ON babata_memory_jobs (id) WHERE status = 'pending'""",
        ]
        async with self.engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))

    async def snapshot(self, user_id: str) -> dict:
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT content, revision, updated_at FROM babata_user_profiles "
                            "WHERE user_id=:u"
                        ),
                        {"u": user_id},
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else {"content": "", "revision": 0, "updated_at": None}

    async def get(self, user_id: str) -> str:
        return (await self.snapshot(user_id))["content"]

    async def _save(self, connection, user_id, content, sources, changes, kind):
        params = {
            "u": user_id,
            "content": content,
            "sources": json.dumps(sources),
            "changes": json.dumps(changes, ensure_ascii=False),
            "kind": kind,
        }
        revision = (
            await connection.execute(
                text("""
            UPDATE babata_user_profiles SET content=:content, sources=CAST(:sources AS jsonb),
                revision=revision+1, updated_at=CURRENT_TIMESTAMP
            WHERE user_id=:u RETURNING revision
        """),
                params,
            )
        ).scalar_one()
        params["revision"] = revision
        await connection.execute(
            text("""
            INSERT INTO babata_profile_revisions
                (user_id, revision, content, sources, changes, kind)
            VALUES (:u, :revision, :content,
                CAST(:sources AS jsonb), CAST(:changes AS jsonb), :kind)
        """),
            params,
        )
        return revision

    async def _lock_profile(self, connection, user_id):
        await connection.execute(
            text("""
            INSERT INTO babata_user_profiles (user_id, content) VALUES (:u, '')
            ON CONFLICT DO NOTHING
        """),
            {"u": user_id},
        )
        await connection.execute(
            text("""
            INSERT INTO babata_profile_revisions (user_id, revision, content, sources, kind)
            SELECT user_id, revision, content, sources, 'baseline'
            FROM babata_user_profiles WHERE user_id=:u ON CONFLICT DO NOTHING
        """),
            {"u": user_id},
        )
        return (
            (
                await connection.execute(
                    text("""
                    SELECT content, revision, sources, memory_watermark
                    FROM babata_user_profiles WHERE user_id=:u FOR UPDATE
        """),
                    {"u": user_id},
                )
            )
            .mappings()
            .one()
        )

    async def put(
        self, user_id: str, content: str, sources: list, *, kind="manual", expected_revision=None
    ):
        if not 1 <= len(user_id) <= 128 or len(content) > 24000:
            raise ValueError("Invalid user ID or profile size")
        async with self.engine.begin() as connection:
            current = await self._lock_profile(connection, user_id)
            if expected_revision is not None and current["revision"] != expected_revision:
                raise ValueError("Memory revision conflict")
            await connection.execute(
                text("""
                UPDATE babata_user_profiles SET memory_watermark=CURRENT_TIMESTAMP WHERE user_id=:u
            """),
                {"u": user_id},
            )
            # A manual edit/restore wins over older queued or in-flight extractions.
            await connection.execute(
                text("""
                UPDATE babata_memory_jobs SET status='cancelled', finished_at=CURRENT_TIMESTAMP
                WHERE user_id=:u AND status='pending'
            """),
                {"u": user_id},
            )
            return await self._save(connection, user_id, content, sources, [], kind)

    async def delete(self, user_id: str):
        await self.put(user_id, "", [], kind="clear")

    async def history(self, user_id: str, limit=20):
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                text("""
                SELECT revision, kind, changes, created_at FROM babata_profile_revisions
                WHERE user_id=:u ORDER BY revision DESC LIMIT :limit
            """),
                {"u": user_id, "limit": limit},
            )
            return [dict(row) for row in rows.mappings()]

    async def restore(self, user_id: str, revision: int):
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text("""
                SELECT content, sources FROM babata_profile_revisions
                WHERE user_id=:u AND revision=:revision
            """),
                        {"u": user_id, "revision": revision},
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise ValueError("Unknown revision")
        return await self.put(user_id, row["content"], row["sources"], kind=f"restore:{revision}")

    async def enqueue(self, user_id: str, session_key: str, message: str, started_at=None) -> int:
        async with self.engine.begin() as connection:
            return (
                await connection.execute(
                    text("""
                    INSERT INTO babata_memory_jobs (user_id, session_key, message, created_at)
                    VALUES (:u, :session, :message, COALESCE(:started, CURRENT_TIMESTAMP))
                    RETURNING id
            """),
                    {
                        "u": user_id,
                        "session": session_key,
                        "message": message,
                        "started": started_at,
                    },
                )
            ).scalar_one()

    async def next_job(self):
        # The gateway has one process and exactly one memory worker. Pending stays
        # durable during inference; a process restart can retry the same job safely.
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text("""
                SELECT id, user_id, session_key, message, created_at FROM babata_memory_jobs
                    WHERE status='pending' ORDER BY created_at, id LIMIT 1
            """)
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def finish_job(self, job_id: int, status: str, error_type=None):
        async with self.engine.begin() as connection:
            await connection.execute(
                text("""
                UPDATE babata_memory_jobs SET status=:status, error_type=:error,
                    finished_at=CURRENT_TIMESTAMP,
                    message=CASE WHEN :status='failed' THEN message ELSE '' END
                WHERE id=:id AND status='pending'
            """),
                {"id": job_id, "status": status, "error": error_type},
            )

    async def apply_job(self, job: dict, revision: int, content: str, changes: list) -> bool:
        async with self.engine.begin() as connection:
            # Same lock order as manual updates: profile first, then job.
            current = await self._lock_profile(connection, job["user_id"])
            status = (
                await connection.execute(
                    text("SELECT status FROM babata_memory_jobs WHERE id=:id FOR UPDATE"),
                    {"id": job["id"]},
                )
            ).scalar_one()
            if status != "pending":
                return False
            stale = current["memory_watermark"] and job["created_at"] < current["memory_watermark"]
            if current["revision"] != revision or stale:
                status = "superseded"
            else:
                if content != current["content"]:
                    await self._save(
                        connection,
                        job["user_id"],
                        content,
                        current["sources"],
                        changes,
                        f"chat:{job['id']}",
                    )
                status = "updated" if changes else "unchanged"
                await connection.execute(
                    text("""
                    UPDATE babata_user_profiles SET memory_watermark=:created WHERE user_id=:u
                """),
                    {"u": job["user_id"], "created": job["created_at"]},
                )
            await connection.execute(
                text("""
                UPDATE babata_memory_jobs
                SET status=:status, message='', finished_at=CURRENT_TIMESTAMP
                WHERE id=:id
            """),
                {"id": job["id"], "status": status},
            )
            return status == "updated"

    async def job_counts(self, user_id: str):
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                text("""
                SELECT status, count(*) AS count FROM babata_memory_jobs
                WHERE user_id=:u GROUP BY status
            """),
                {"u": user_id},
            )
            return dict(rows.tuples().all())
