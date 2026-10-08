"""Small durable peer inbox: contextual updates never become user commands."""

import hashlib
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from babata.codex import bridge_auth
from babata.native_memory_files import redact


class PeerUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    topic: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=4000)
    source_ref: str = Field(min_length=1, max_length=2000)


class PeerUpdates:
    def __init__(self, engine):
        self.engine = engine

    async def initialize(self):
        async with self.engine.begin() as c:
            await c.execute(
                text("""CREATE TABLE IF NOT EXISTS babata_peer_updates (
                id UUID PRIMARY KEY, user_id TEXT NOT NULL, sender TEXT NOT NULL,
                recipient TEXT NOT NULL, topic TEXT NOT NULL, message TEXT NOT NULL,
                source_ref TEXT NOT NULL, digest TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(), delivered_at TIMESTAMPTZ,
                UNIQUE(user_id,sender,digest))""")
            )

    async def send(self, user, sender, body: PeerUpdate):
        if sender not in ("mac", "tokyo"):
            raise ValueError("Unknown sender")
        if not body.message.strip() or not body.topic.strip() or not body.source_ref.strip():
            raise ValueError("Empty update")
        recipient = "tokyo" if sender == "mac" else "mac"
        topic, message, ref = (redact(x) for x in (body.topic, body.message, body.source_ref))
        digest = hashlib.sha256((ref + "\0" + message).encode()).hexdigest()
        async with self.engine.begin() as c:
            row = (
                (
                    await c.execute(
                        text("""INSERT INTO babata_peer_updates
                (id,user_id,sender,recipient,topic,message,source_ref,digest)
                VALUES (:id,:u,:s,:r,:t,:m,:ref,:d)
                ON CONFLICT(user_id,sender,digest) DO UPDATE SET digest=EXCLUDED.digest
                RETURNING id,sender,recipient,created_at,delivered_at"""),
                        {
                            "id": uuid4(),
                            "u": user,
                            "s": sender,
                            "r": recipient,
                            "t": topic,
                            "m": message,
                            "ref": ref,
                            "d": digest,
                        },
                    )
                )
                .mappings()
                .one()
            )
            return {**dict(row), "kind": "peer_update", "reference_only": True}

    async def inbox(self, user, recipient, pending=True, limit=10):
        async with self.engine.connect() as c:
            rows = await c.execute(
                text("""SELECT id,sender,recipient,topic,message,source_ref,
                created_at,delivered_at FROM babata_peer_updates
                WHERE user_id=:u AND recipient=:r AND (NOT :pending OR delivered_at IS NULL)
                ORDER BY created_at ASC LIMIT :n"""),
                {"u": user, "r": recipient, "pending": pending, "n": min(max(limit, 1), 20)},
            )
            return [
                {**dict(r), "kind": "peer_update", "reference_only": True} for r in rows.mappings()
            ]

    async def delivered(self, user, recipient, ids):
        if not ids:
            return 0
        async with self.engine.begin() as c:
            result = await c.execute(
                text("""UPDATE babata_peer_updates
                SET delivered_at=COALESCE(delivered_at,now())
                WHERE user_id=:u AND recipient=:r AND id=ANY(:ids)"""),
                {"u": user, "r": recipient, "ids": [UUID(str(x)) for x in ids]},
            )
            return result.rowcount


router = APIRouter(prefix="/bridge/peer", dependencies=[Depends(bridge_auth)])


@router.post("/notify")
async def notify(body: PeerUpdate, request: Request):
    state = request.app.state
    # This authenticated transport belongs to Mac; Tokyo sends in-process.
    return await state.shared.peer.send(state.settings.codex_user_id, "mac", body)


@router.get("/inbox")
async def inbox(request: Request, pending: bool = True, limit: int = 10):
    state = request.app.state
    return await state.shared.peer.inbox(state.settings.codex_user_id, "mac", pending, limit)


class Delivered(BaseModel):
    ids: list[UUID] = Field(max_length=20)
    status: Literal["delivered"] = "delivered"


@router.post("/delivered")
async def delivered(body: Delivered, request: Request):
    state = request.app.state
    count = await state.shared.peer.delivered(state.settings.codex_user_id, "mac", body.ids)
    return {"delivered": count}
