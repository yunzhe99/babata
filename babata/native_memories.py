"""Source-separated mirrors of native Codex memories, shared through PostgreSQL."""

import asyncio
import contextlib
import hashlib
import logging
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from babata.native_memory_files import allowed_path, redact, snapshot

logger = logging.getLogger(__name__)


class MemoryFile(BaseModel):
    path: str
    content: str = Field(max_length=2_000_000)
    modified_at: float = Field(ge=0)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value):
        if not allowed_path(value):
            raise ValueError("Only native memory Markdown paths are accepted")
        return value


class MemorySnapshot(BaseModel):
    source: Literal["mac", "tokyo"]
    files: list[MemoryFile] = Field(max_length=2000)


class NativeMemories:
    def __init__(self, engine):
        self.engine = engine

    async def initialize(self):
        async with self.engine.begin() as c:
            await c.execute(
                text("""CREATE TABLE IF NOT EXISTS babata_native_memories (
                user_id TEXT NOT NULL, source TEXT NOT NULL, path TEXT NOT NULL,
                content TEXT NOT NULL, digest TEXT NOT NULL, modified_at TIMESTAMPTZ NOT NULL,
                revision BIGINT NOT NULL DEFAULT 1, active BOOLEAN NOT NULL DEFAULT true,
                synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(user_id,source,path))""")
            )
            await c.execute(
                text("""CREATE TABLE IF NOT EXISTS babata_native_memory_versions (
                user_id TEXT NOT NULL, source TEXT NOT NULL, path TEXT NOT NULL,
                revision BIGINT NOT NULL, content TEXT NOT NULL, active BOOLEAN NOT NULL,
                saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY(user_id,source,path,revision))""")
            )

    async def ingest(self, user, body):
        paths = [f.path for f in body.files]
        if len(set(paths)) != len(paths) or sum(len(f.content) for f in body.files) > 20_000_000:
            raise ValueError("Invalid memory snapshot")
        changed = 0
        async with self.engine.begin() as c:
            await c.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k,0))"),
                {"k": "native-memory:" + user + ":" + body.source},
            )
            old = {
                r.path: dict(r._mapping)
                for r in await c.execute(
                    text("SELECT * FROM babata_native_memories WHERE user_id=:u AND source=:s"),
                    {"u": user, "s": body.source},
                )
            }
            entries = [
                (f.path, redact(f.content), datetime.fromtimestamp(f.modified_at, UTC), True)
                for f in body.files
            ]
            entries.extend(
                (p, r["content"], r["modified_at"], False)
                for p, r in old.items()
                if p not in paths and r["active"]
            )
            for path, content, modified, active in entries:
                digest = hashlib.sha256(content.encode()).hexdigest()
                previous = old.get(path)
                if previous and previous["digest"] == digest and previous["active"] == active:
                    continue
                revision = previous["revision"] + 1 if previous else 1
                params = dict(
                    u=user,
                    s=body.source,
                    p=path,
                    b=content,
                    d=digest,
                    m=modified,
                    a=active,
                    r=revision,
                )
                await c.execute(
                    text("""INSERT INTO babata_native_memories
                    (user_id,source,path,content,digest,modified_at,active,revision)
                    VALUES (:u,:s,:p,:b,:d,:m,:a,:r)
                    ON CONFLICT (user_id,source,path) DO UPDATE SET
                    content=:b,digest=:d,modified_at=:m,active=:a,revision=:r,synced_at=now()"""),
                    params,
                )
                await c.execute(
                    text("""INSERT INTO babata_native_memory_versions
                    (user_id,source,path,revision,content,active) VALUES (:u,:s,:p,:r,:b,:a)"""),
                    params,
                )
                changed += 1
        return {"source": body.source, "documents": len(paths), "changed": changed}

    async def search(self, user, query, source=None, limit=5):
        query = query.strip()[:200]
        if not query:
            return []
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self.engine.connect() as c:
            rows = await c.execute(
                text("""SELECT * FROM babata_native_memories
                WHERE user_id=:u AND active AND (CAST(:s AS TEXT) IS NULL OR source=:s)
                AND (content ILIKE :q OR path ILIKE :q)
                ORDER BY modified_at DESC,path LIMIT :n"""),
                {"u": user, "s": source, "q": pattern, "n": min(max(limit, 1), 20)},
            )
            items = []
            for row in rows.mappings():
                start = max(0, row["content"].lower().find(query.lower()) - 100)
                items.append(
                    {
                        "thread_id": "memory:" + row["source"] + ":" + row["path"],
                        "message_id": "revision:" + str(row["revision"]),
                        "kind": "native_memory",
                        "source": row["source"],
                        "path": row["path"],
                        "title": "Codex 整理记忆 · " + row["path"],
                        "sent_at": row["modified_at"],
                        "revision": row["revision"],
                        "excerpt": row["content"][start : start + 700],
                    }
                )
            return items

    async def read(self, user, identity, offset=0, limit=20, message_id=None):
        _, source, path = identity.split(":", 2)
        async with self.engine.connect() as c:
            row = (
                (
                    await c.execute(
                        text("""SELECT * FROM babata_native_memories
                WHERE user_id=:u AND source=:s AND path=:p AND active"""),
                        {"u": user, "s": source, "p": path},
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise ValueError("Memory not found")
            chunks = [row["content"][i : i + 4000] for i in range(0, len(row["content"]), 4000)]
            offset = max(0, offset)
            selected = chunks[offset : offset + min(max(limit, 1), 5)]
            return {
                "thread_id": identity,
                "kind": "native_memory",
                "source": source,
                "path": path,
                "revision": row["revision"],
                "modified_at": row["modified_at"],
                "reference_only": True,
                "next_offset": offset + len(selected),
                "messages": [
                    {"id": str(offset + i), "role": "assistant", "text": s}
                    for i, s in enumerate(selected)
                ],
                "note": "模型整理的参考资料；有冲突时查原对话。文内来源路径属于原主机。",
            }

    async def stats(self, user):
        async with self.engine.connect() as c:
            rows = await c.execute(
                text("""SELECT source,count(*) AS documents,
                max(synced_at) AS last_synced_at FROM babata_native_memories
                WHERE user_id=:u AND active GROUP BY source"""),
                {"u": user},
            )
            return [dict(r._mapping) for r in rows]


class NativeMemoryMirror:
    def __init__(self, store, user, root):
        self.store, self.user, self.root = store, user, root
        self.digest = None
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            try:
                value = await asyncio.to_thread(snapshot, self.root)
                if value is not None and value["digest"] != self.digest:
                    await self.store.ingest(
                        self.user, MemorySnapshot(source="tokyo", files=value["files"])
                    )
                    self.digest = value["digest"]
            except Exception as e:
                logger.warning("Native memory mirror retry: %s", type(e).__name__)
            await asyncio.sleep(30)

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
