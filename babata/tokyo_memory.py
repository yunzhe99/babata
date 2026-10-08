"""Idle triggers for Codex's own memory pipeline; no independent extraction."""

import asyncio
import contextlib
import logging
import time

from sqlalchemy import text

from babata.tokyo_memory_state import snapshot

logger = logging.getLogger(__name__)


class TokyoMemoryMaintenance:
    def __init__(self, runtime):
        self.runtime = runtime
        self.task = None

    async def start(self):
        if not self.runtime.state.settings.tokyo_native_memories:
            return
        async with self.runtime.state.sessions.engine.begin() as c:
            await c.execute(
                text("""CREATE TABLE IF NOT EXISTS babata_tokyo_memory_schedule (
                    user_id TEXT PRIMARY KEY, last_attempt_at BIGINT,
                    maintenance_thread_id TEXT)""")
            )
            await c.execute(
                text("""INSERT INTO babata_tokyo_memory_schedule (user_id)
                    VALUES (:u) ON CONFLICT (user_id) DO NOTHING"""),
                {"u": self.runtime.state.settings.codex_user_id},
            )
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as error:
                # Never log database rows, RPC arguments, or model output.
                logger.warning("Native memory maintenance retry: %s", type(error).__name__)
            await asyncio.sleep(60)

    async def tick(self, now=None):
        runtime = self.runtime
        if runtime.closing or not runtime.state.settings.tokyo_native_memories:
            return
        now = time.time() if now is None else now
        user = runtime.state.settings.codex_user_id
        engine = runtime.state.sessions.engine
        # Do not delay or steer an existing user turn. A later user turn may
        # coexist with the native pipeline once the idle trigger has started.
        async with runtime.registry_lock:
            if runtime.closing or any(not task.worker.done() for task in runtime.active.values()):
                return
        async with engine.connect() as c:
            rows = list(
                await c.execute(
                    text("""SELECT s.thread_id, max(m.sent_at) AS latest_at
                        FROM babata_tokyo_sessions s
                        JOIN babata_shared_threads t
                          ON t.external_id=s.thread_id AND t.source='tokyo' AND t.user_id=:u
                        JOIN babata_shared_messages m ON m.thread_id=t.id
                        WHERE s.user_id=:u AND s.session_id LIKE 'rokid-%'
                        GROUP BY s.thread_id"""),
                    {"u": user},
                )
            )
            schedule = (
                await c.execute(
                    text("""SELECT last_attempt_at,maintenance_thread_id
                        FROM babata_tokyo_memory_schedule WHERE user_id=:u"""),
                    {"u": user},
                )
            ).one()
        if not rows:
            return
        native = await asyncio.to_thread(
            snapshot, runtime.directory / "codex", [r.thread_id for r in rows], now
        )
        # Unsubscribe only removes this client's subscription. Codex defers
        # idle unload while a turn is active; its memory pipeline owns its
        # own context. Unknown metadata raises rather than allowing cleanup.
        if schedule.maintenance_thread_id and not native.running:
            await runtime.rpc.call(
                "thread/unsubscribe", {"threadId": schedule.maintenance_thread_id}, timeout=10
            )
            async with engine.begin() as c:
                await c.execute(
                    text("""UPDATE babata_tokyo_memory_schedule SET maintenance_thread_id=NULL
                        WHERE user_id=:u AND maintenance_thread_id=:t"""),
                    {"u": user, "t": schedule.maintenance_thread_id},
                )
        latest = max(r.latest_at.timestamp() for r in rows)
        if (
            now - latest < 3600
            or native.running
            or not (native.pending_threads or native.consolidation_pending)
        ):
            return
        async with runtime.registry_lock:
            if runtime.closing or any(not task.worker.done() for task in runtime.active.values()):
                return
        # Persist the attempt before RPC, including failed attempts. Native
        # success watermarks, not this timestamp, decide completion. This
        # lets failed jobs and batches beyond the native limit of two retry.
        async with engine.begin() as c:
            claimed = (
                await c.execute(
                    text("""UPDATE babata_tokyo_memory_schedule SET last_attempt_at=:now
                        WHERE user_id=:u AND
                          (last_attempt_at IS NULL OR last_attempt_at<=:cutoff)
                        RETURNING user_id"""),
                    {"u": user, "now": int(now), "cutoff": int(now) - 3600},
                )
            ).first()
        if claimed is None:
            return
        # The native scan is global to this Codex home. Keep all other
        # gateway users excluded, including pre-existing persisted threads.
        async with engine.connect() as c:
            others = list(
                await c.execute(
                    text("SELECT thread_id FROM babata_tokyo_sessions WHERE user_id<>:u"),
                    {"u": user},
                )
            )
        for row in others:
            await runtime.rpc.call(
                "thread/memoryMode/set",
                {"threadId": row.thread_id, "mode": "disabled"},
                timeout=10,
            )
        result = await runtime.rpc.call(
            "thread/start",
            {
                "cwd": str(runtime.workspace),
                "model": runtime.state.settings.tokyo_memory_extract_model,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "dynamicTools": [],
                "config": {
                    "model_reasoning_effort": "low",
                    "web_search": "disabled",
                    "mcp_servers.babata_public.enabled": False,
                    "memories.use_memories": False,
                    "memories.generate_memories": False,
                },
            },
            timeout=10,
        )
        thread_id = result["thread"]["id"]
        async with engine.begin() as c:
            await c.execute(
                text("""UPDATE babata_tokyo_memory_schedule SET maintenance_thread_id=:t
                    WHERE user_id=:u"""),
                {"u": user, "t": thread_id},
            )
        # Codex 0.155.1 starts the memory pipeline only for a nonempty
        # newly started turn. thread/start alone and steering do not do so.
        await runtime.rpc.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": "这是系统后台维护触发轮次。不要调用工具、读取资料、"
                        "执行任务或写文件。仅回复 OK。",
                    }
                ],
                "effort": "low",
            },
            timeout=10,
        )
        logger.info("Native memory maintenance triggered")

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
