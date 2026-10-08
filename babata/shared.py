"""Shared, source-addressable user conversations; no model-generated summaries."""

import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from babata.codex import bridge_auth
from babata.native_memories import MemorySnapshot, NativeMemories
from babata.peer_updates import PeerUpdates


def redact(value: str) -> str:
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[private key removed]",
        value,
        flags=re.S,
    )
    value = re.sub(
        r"\b(?:sk-|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}", "[credential removed]", value
    )
    value = re.sub(
        r"(?im)((?:api[_ -]?key|password|secret|access_token|密码)\s*[:=：]\s*)[^\s,;]+",
        r"\1[credential removed]",
        value,
    )
    return value.replace("\x00", "")


def archive_id(source: str, external_id: str) -> str:
    return source + ":" + external_id


class SharedMessage(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    role: Literal["user", "assistant"]
    text: str = Field(max_length=200000)
    position: int = Field(ge=0)
    timestamp: datetime | None = None


class SharedThread(BaseModel):
    source: Literal["mac", "tokyo", "babata"]
    external_id: str = Field(min_length=1, max_length=160)
    # Older Codex tasks sometimes retain their full initial prompt as the title.
    title: str = Field(max_length=200000)
    project: str = Field(default="", max_length=2000)
    uri: str = Field(default="", max_length=2000)
    messages: list[SharedMessage] = Field(default_factory=list, max_length=100)


class SharedArchive:
    def __init__(self, engine):
        self.engine = engine
        self.memories = NativeMemories(engine)
        self.peer = PeerUpdates(engine)

    async def initialize(self):
        await self.memories.initialize()
        await self.peer.initialize()
        async with self.engine.begin() as c:
            for statement in [
                """CREATE TABLE IF NOT EXISTS babata_shared_threads (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source TEXT NOT NULL,
                    external_id TEXT NOT NULL, title TEXT NOT NULL, project TEXT NOT NULL,
                    uri TEXT NOT NULL, synced_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
                """CREATE TABLE IF NOT EXISTS babata_shared_messages (
                    thread_id TEXT NOT NULL REFERENCES babata_shared_threads(id),
                    id TEXT NOT NULL, role TEXT NOT NULL, body TEXT NOT NULL,
                    position BIGINT NOT NULL, sent_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY(thread_id,id))""",
                """CREATE INDEX IF NOT EXISTS shared_messages_order
                    ON babata_shared_messages(thread_id,position,id)""",
                """CREATE TABLE IF NOT EXISTS babata_tokyo_sessions (
                    session_key TEXT PRIMARY KEY, thread_id TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL, session_id TEXT NOT NULL)""",
            ]:
                await c.execute(text(statement))

    async def ingest(self, user: str, thread: SharedThread):
        tid = archive_id(thread.source, thread.external_id)
        async with self.engine.begin() as c:
            row = await c.execute(
                text("""INSERT INTO babata_shared_threads
                (id,user_id,source,external_id,title,project,uri)
                VALUES (:id,:u,:s,:e,:t,:p,:uri)
                ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title,
                    project=EXCLUDED.project,uri=EXCLUDED.uri,synced_at=now()
                WHERE babata_shared_threads.user_id=EXCLUDED.user_id RETURNING id"""),
                {
                    "id": tid,
                    "u": user,
                    "s": thread.source,
                    "e": thread.external_id,
                    "t": redact(thread.title),
                    "p": thread.project,
                    "uri": thread.uri,
                },
            )
            if row.first() is None:
                raise ValueError("Conversation belongs to another user")
            if thread.messages:
                await c.execute(
                    text("""INSERT INTO babata_shared_messages
                    (thread_id,id,role,body,position,sent_at) VALUES (:tid,:id,:r,:b,:p,:ts)
                    ON CONFLICT (thread_id,id) DO UPDATE SET body=EXCLUDED.body,
                    role=EXCLUDED.role,position=EXCLUDED.position,sent_at=EXCLUDED.sent_at"""),
                    [
                        {
                            "tid": tid,
                            "id": m.id,
                            "r": m.role,
                            "b": redact(m.text),
                            "p": m.position,
                            "ts": m.timestamp or datetime.now(UTC),
                        }
                        for m in thread.messages
                    ],
                )
        return {"thread_id": tid, "accepted": len(thread.messages)}

    async def search(self, user, query, source=None, limit=10):
        query = query.strip()[:200]
        limit = min(max(int(limit), 1), 20)
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        async with self.engine.connect() as c:
            result = await c.execute(
                text("""SELECT t.id AS thread_id,t.title,t.source,
                t.project,t.uri,m.id AS message_id,m.role,m.body,m.sent_at,m.position
                FROM babata_shared_threads t JOIN babata_shared_messages m ON m.thread_id=t.id
                WHERE t.user_id=:u AND (CAST(:source AS TEXT) IS NULL OR t.source=:source)
                AND (m.body ILIKE :q OR t.title ILIKE :q)
                ORDER BY m.sent_at DESC,m.position DESC LIMIT :n"""),
                {"u": user, "source": source, "q": pattern, "n": limit},
            )
            items = []
            for row in result.mappings():
                item = dict(row)
                body = item.pop("body")
                index = body.lower().find(query.lower())
                start = max(index - 100, 0)
                item["excerpt"] = body[start : start + 700]
                items.append(item)
            # Reserve an archive hit when there is room for both categories.
            memory_limit = min(5, limit - 1 if items and limit > 1 else limit)
            memories = await self.memories.search(user, query, source, memory_limit)
            return memories + items[: max(0, limit - len(memories))]

    async def read(self, user, tid, offset=0, limit=20, message_id=None):
        if tid.startswith("memory:"):
            return await self.memories.read(user, tid, offset, limit, message_id)
        async with self.engine.connect() as c:
            row = (
                (
                    await c.execute(
                        text("SELECT * FROM babata_shared_threads WHERE id=:t AND user_id=:u"),
                        {"t": tid, "u": user},
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise ValueError("Conversation not found")
            if message_id:
                position = (
                    await c.execute(
                        text(
                            "SELECT position FROM babata_shared_messages "
                            "WHERE thread_id=:t AND id=:m"
                        ),
                        {"t": tid, "m": message_id},
                    )
                ).scalar_one_or_none()
                if position is None:
                    raise ValueError("Message not found")
                offset = max(
                    0,
                    (
                        await c.execute(
                            text(
                                "SELECT count(*) FROM babata_shared_messages "
                                "WHERE thread_id=:t AND position<:p"
                            ),
                            {"t": tid, "p": position},
                        )
                    ).scalar_one()
                    - 2,
                )
            rows = await c.execute(
                text("""SELECT id,role,body AS text,sent_at,position
                FROM babata_shared_messages WHERE thread_id=:t ORDER BY position,id
                OFFSET :o LIMIT :n"""),
                {"t": tid, "o": max(0, offset), "n": min(max(limit, 1), 50)},
            )
            messages = [dict(x) for x in rows.mappings()]
            return {
                **dict(row),
                "thread_id": tid,
                "messages": messages,
                "next_offset": offset + len(messages),
            }

    async def stats(self, user):
        async with self.engine.connect() as c:
            rows = await c.execute(
                text("""SELECT t.source,count(DISTINCT t.id) AS conversations,
                count(m.id) AS messages,max(t.synced_at) AS last_synced_at
                FROM babata_shared_threads t LEFT JOIN babata_shared_messages m ON m.thread_id=t.id
                WHERE t.user_id=:u GROUP BY t.source"""),
                {"u": user},
            )
            return [dict(r) for r in rows.mappings()]


router = APIRouter(prefix="/bridge/shared", dependencies=[Depends(bridge_auth)])


@router.post("/memories")
async def mirror_memories(body: MemorySnapshot, request: Request):
    # This transport is the Mac uploader. Tokyo writes its own namespace locally.
    if body.source != "mac":
        raise HTTPException(403, "Only the Mac namespace can be uploaded here")
    try:
        return await request.app.state.shared.memories.ingest(
            request.app.state.settings.codex_user_id, body
        )
    except ValueError:
        raise HTTPException(422, "Invalid memory snapshot") from None


@router.get("/memories/stats")
async def memory_stats(request: Request):
    return await request.app.state.shared.memories.stats(request.app.state.settings.codex_user_id)


@router.post("/ingest")
async def ingest(body: SharedThread, request: Request):
    return await request.app.state.shared.ingest(request.app.state.settings.codex_user_id, body)


@router.get("/search")
async def search(request: Request, q: str = "", source: str | None = None, limit: int = 10):
    return await request.app.state.shared.search(
        request.app.state.settings.codex_user_id, q, source, limit
    )


@router.get("/read")
async def read(
    request: Request,
    thread_id: str,
    offset: int = 0,
    limit: int = 20,
    message_id: str | None = None,
):
    try:
        return await request.app.state.shared.read(
            request.app.state.settings.codex_user_id, thread_id, offset, limit, message_id
        )
    except ValueError:
        raise HTTPException(404, "Conversation not found") from None


@router.get("/stats")
async def stats(request: Request):
    return await request.app.state.shared.stats(request.app.state.settings.codex_user_id)


@router.get("/profile")
async def profile(request: Request):
    return await request.app.state.profiles.snapshot(request.app.state.settings.codex_user_id)


class ProfileUpdate(BaseModel):
    content: str = Field(max_length=24000)
    expected_revision: int = Field(ge=0)


@router.post("/profile")
async def update_profile(body: ProfileUpdate, request: Request):
    try:
        rev = await request.app.state.profiles.put(
            request.app.state.settings.codex_user_id,
            redact(body.content),
            [],
            kind="shared-edit",
            expected_revision=body.expected_revision,
        )
        return {"revision": rev}
    except ValueError:
        raise HTTPException(409, "Memory changed; read current revision before editing") from None
