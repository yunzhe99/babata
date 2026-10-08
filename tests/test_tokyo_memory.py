import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from babata import tokyo_memory
from babata.tokyo_memory import TokyoMemoryMaintenance


class Database:
    """Execute the scheduler's SQL against real, synthetic SQLite tables."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE babata_tokyo_sessions (thread_id, user_id, session_id);
            CREATE TABLE babata_shared_threads (id, external_id, source, user_id);
            CREATE TABLE babata_shared_messages (thread_id, sent_at);
        """)

    def conversation(self, tid, user, session, at, archive_user=None):
        self.db.execute("INSERT INTO babata_tokyo_sessions VALUES (?,?,?)", (tid, user, session))
        self.db.execute(
            "INSERT INTO babata_shared_threads VALUES (?,?,'tokyo',?)",
            ("archive-" + tid, tid, user if archive_user is None else archive_user),
        )
        self.db.execute("INSERT INTO babata_shared_messages VALUES (?,?)", ("archive-" + tid, at))
        self.db.commit()

    async def execute(self, statement, params=None):
        cursor = self.db.execute(str(statement), params or {})
        values = []
        for row in cursor.fetchall():
            value = dict(row)
            if value.get("latest_at") is not None:
                value["latest_at"] = datetime.fromtimestamp(value["latest_at"], UTC)
            values.append(SimpleNamespace(**value))

        class Result(list):
            def first(self):
                return self[0] if self else None

            def one(self):
                assert len(self) == 1
                return self[0]

        return Result(values)

    @asynccontextmanager
    async def connect(self):
        yield self

    @asynccontextmanager
    async def begin(self):
        try:
            yield self
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def schedule(self):
        return dict(self.db.execute("SELECT * FROM babata_tokyo_memory_schedule").fetchone())


async def build(tmp_path, monkeypatch, database=None):
    database = database or Database()
    calls, snapshots = [], []
    native = SimpleNamespace(
        pending_threads=("voice",), running=False, consolidation_pending=False, error=None
    )

    def snapshot(root, tids, now):
        snapshots.append((root, tids, now))
        if native.error:
            raise native.error
        return native

    monkeypatch.setattr(tokyo_memory, "snapshot", snapshot)

    async def rpc_call(method, params, **kwargs):
        calls.append((method, params, kwargs))
        if method == "thread/start":
            return {"thread": {"id": "maintenance-" + str(len(calls))}}
        return {}

    runtime = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(
                codex_user_id="owner",
                tokyo_native_memories=True,
                tokyo_memory_extract_model="test-luna",
            ),
            sessions=SimpleNamespace(engine=database),
        ),
        active={},
        closing=False,
        registry_lock=asyncio.Lock(),
        directory=tmp_path,
        workspace=tmp_path / "workspace",
        rpc=SimpleNamespace(call=rpc_call),
    )
    maintenance = TokyoMemoryMaintenance(runtime)
    await maintenance.start()
    await maintenance.close()
    return maintenance, runtime, database, calls, native, snapshots


def test_idle_trigger_uses_native_pipeline_and_excludes_other_users(tmp_path, monkeypatch):
    async def check():
        maintenance, _, db, calls, _, snapshots = await build(tmp_path, monkeypatch)
        now = 100_000
        db.conversation("voice", "owner", "rokid-voice", now - 3601)
        db.conversation("phone", "owner", "phone", now - 1)
        db.conversation("other", "other", "rokid-voice", now - 1)
        db.conversation("mismatch", "owner", "rokid-voice", now - 1, archive_user="other")
        await maintenance.tick(now)
        assert snapshots[0][1] == ["voice"]
        assert [name for name, _, _ in calls] == [
            "thread/memoryMode/set",
            "thread/start",
            "turn/start",
        ]
        assert calls[0][1] == {"threadId": "other", "mode": "disabled"}
        params = calls[1][1]
        assert params["model"] == "test-luna"
        assert params["sandbox"] == "read-only" and params["approvalPolicy"] == "never"
        assert params["dynamicTools"] == []
        assert params["config"]["memories.generate_memories"] is False
        assert params["config"]["memories.use_memories"] is False
        assert params["config"]["mcp_servers.babata_public.enabled"] is False
        assert not any(key.startswith("features.") for key in params["config"])
        assert "developerInstructions" not in params and "baseInstructions" not in params
        assert calls[2][1]["input"][0]["text"].endswith("仅回复 OK。")
        assert "additionalContext" not in calls[2][1]
        assert db.schedule()["last_attempt_at"] == now
        assert db.db.execute("SELECT count(*) FROM babata_tokyo_sessions").fetchone()[0] == 4
        assert db.db.execute("SELECT count(*) FROM babata_shared_messages").fetchone()[0] == 4
        db.db.close()

    asyncio.run(check())


@pytest.mark.parametrize(
    "reason", ["fresh", "active", "native_running", "done", "no_owner", "closing", "disabled"]
)
def test_no_idle_trigger_when_work_is_ineligible(tmp_path, monkeypatch, reason):
    async def check():
        maintenance, runtime, db, calls, native, _ = await build(tmp_path, monkeypatch)
        now = 100_000
        db.conversation(
            "voice",
            "other" if reason == "no_owner" else "owner",
            "rokid-voice",
            now - (3599 if reason == "fresh" else 3601),
        )
        if reason == "active":
            runtime.active["voice"] = SimpleNamespace(worker=SimpleNamespace(done=lambda: False))
        if reason == "native_running":
            native.running = True
        if reason == "done":
            native.pending_threads = ()
        if reason == "closing":
            runtime.closing = True
        if reason == "disabled":
            runtime.state.settings.tokyo_native_memories = False
        await maintenance.tick(now)
        assert calls == []
        assert db.schedule()["last_attempt_at"] is None
        db.db.close()

    asyncio.run(check())


def test_restart_throttles_failed_attempt_and_retries_without_new_chat(tmp_path, monkeypatch):
    async def check():
        maintenance, runtime, db, calls, _, _ = await build(tmp_path, monkeypatch)
        now = 100_000
        db.conversation("voice", "owner", "rokid-voice", now - 3601)

        async def failed_rpc(method, params, **kwargs):
            calls.append((method, params, kwargs))
            raise RuntimeError("synthetic failure")

        runtime.rpc.call = failed_rpc
        with pytest.raises(RuntimeError):
            await maintenance.tick(now)
        assert db.schedule()["last_attempt_at"] == now
        restarted, _, _, restarted_calls, native, _ = await build(tmp_path, monkeypatch, db)
        await restarted.tick(now + 3599)
        assert restarted_calls == []
        await restarted.tick(now + 3600)
        assert [method for method, _, _ in restarted_calls] == ["thread/start", "turn/start"]
        native.pending_threads = ()
        native.consolidation_pending = True
        await restarted.tick(now + 7200)
        assert [method for method, _, _ in restarted_calls].count("turn/start") == 2
        db.db.close()

    asyncio.run(check())


def test_native_completion_deduplicates_and_new_input_becomes_eligible_after_idle(
    tmp_path, monkeypatch
):
    async def check():
        maintenance, _, db, calls, native, _ = await build(tmp_path, monkeypatch)
        now = 100_000
        db.conversation("voice", "owner", "rokid-voice", now - 3601)
        await maintenance.tick(now)
        tid = db.schedule()["maintenance_thread_id"]
        native.running = True
        await maintenance.tick(now + 60)
        assert not any(method == "thread/unsubscribe" for method, _, _ in calls)
        native.running = False
        native.pending_threads = ()
        await maintenance.tick(now + 3600)
        assert calls[-1][0:2] == ("thread/unsubscribe", {"threadId": tid})
        assert db.schedule()["maintenance_thread_id"] is None
        assert db.schedule()["last_attempt_at"] == now
        native.pending_threads = ("voice",)
        db.db.execute("UPDATE babata_shared_messages SET sent_at=?", (now + 3600,))
        db.db.commit()
        await maintenance.tick(now + 3601)
        assert [method for method, _, _ in calls].count("turn/start") == 1
        await maintenance.tick(now + 7200)
        assert [method for method, _, _ in calls].count("turn/start") == 2
        db.db.close()

    asyncio.run(check())


def test_unknown_native_state_cannot_trigger_or_cleanup(tmp_path, monkeypatch):
    async def check():
        maintenance, _, db, calls, native, _ = await build(tmp_path, monkeypatch)
        db.conversation("voice", "owner", "rokid-voice", 90_000)
        db.db.execute("UPDATE babata_tokyo_memory_schedule SET maintenance_thread_id='previous'")
        db.db.commit()
        native.error = sqlite3.OperationalError("synthetic schema mismatch")
        with pytest.raises(sqlite3.OperationalError):
            await maintenance.tick(100_000)
        assert calls == []
        assert db.schedule()["maintenance_thread_id"] == "previous"
        assert db.schedule()["last_attempt_at"] is None
        db.db.close()

    asyncio.run(check())


def test_slow_maintenance_rpc_does_not_hold_user_registry_lock(tmp_path, monkeypatch):
    async def check():
        maintenance, runtime, db, _, _, _ = await build(tmp_path, monkeypatch)
        db.conversation("voice", "owner", "rokid-voice", 90_000)
        entered, release = asyncio.Event(), asyncio.Event()
        original = runtime.rpc.call

        async def slow_rpc(method, params, **kwargs):
            if method == "thread/start":
                entered.set()
                await release.wait()
            return await original(method, params, **kwargs)

        runtime.rpc.call = slow_rpc
        worker = asyncio.create_task(maintenance.tick(100_000))
        await asyncio.wait_for(entered.wait(), 1)
        async with asyncio.timeout(1):
            async with runtime.registry_lock:
                assert not worker.done()
        release.set()
        await worker
        db.db.close()

    asyncio.run(check())


def test_native_two_candidate_batch_does_not_discard_remaining_backlog(tmp_path, monkeypatch):
    async def check():
        maintenance, _, db, calls, native, _ = await build(tmp_path, monkeypatch)
        now = 100_000
        for tid in ("one", "two", "three"):
            db.conversation(tid, "owner", "rokid-" + tid, now - 3601)
        native.pending_threads = ("one", "two", "three")
        await maintenance.tick(now)
        # Native max_rollouts_per_startup=2 leaves one unfinished source.
        native.pending_threads = ("three",)
        await maintenance.tick(now + 3600)
        native.pending_threads = ()
        await maintenance.tick(now + 7200)
        assert [method for method, _, _ in calls].count("turn/start") == 2
        assert db.schedule()["last_attempt_at"] == now + 3600
        db.db.close()

    asyncio.run(check())
