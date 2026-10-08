import hashlib
import sqlite3

import pytest

from babata import tokyo_memory_state as native

NOW = 1_900_000_000


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "native state with spaces"
    root.mkdir()
    with sqlite3.connect(root / native.STATE_DATABASE) as db:
        db.executescript("""
            CREATE TABLE threads (
                id TEXT PRIMARY KEY, updated_at INTEGER NOT NULL, updated_at_ms INTEGER,
                memory_mode TEXT NOT NULL DEFAULT 'enabled', archived INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'cli', title TEXT DEFAULT 'private title',
                preview TEXT DEFAULT 'private preview');
        """)
    with sqlite3.connect(root / native.MEMORY_DATABASE) as db:
        db.executescript("""
            CREATE TABLE stage1_outputs (
                thread_id TEXT PRIMARY KEY, source_updated_at INTEGER NOT NULL,
                raw_memory TEXT NOT NULL DEFAULT 'private memory',
                rollout_summary TEXT NOT NULL DEFAULT 'private summary');
            CREATE TABLE jobs (
                kind TEXT NOT NULL, job_key TEXT NOT NULL, status TEXT NOT NULL,
                lease_until INTEGER, retry_at INTEGER, retry_remaining INTEGER NOT NULL DEFAULT 3,
                finished_at INTEGER, last_error TEXT, input_watermark INTEGER,
                last_success_watermark INTEGER, PRIMARY KEY(kind,job_key));
        """)
    return root


def thread(
    root,
    tid,
    *,
    ago=7200,
    milliseconds=...,
    mode="enabled",
    archived=0,
    source="cli",
    preview="private preview",
):
    at = NOW - ago
    with sqlite3.connect(root / native.STATE_DATABASE) as db:
        db.execute(
            "INSERT INTO threads (id,updated_at,updated_at_ms,memory_mode,archived,source,preview) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                tid,
                at,
                at * 1000 if milliseconds is ... else milliseconds,
                mode,
                archived,
                source,
                preview,
            ),
        )
    return at


def job(root, tid="global", *, kind="memory_consolidate_global", status="pending", **fields):
    values = {"kind": kind, "job_key": tid, "status": status, **fields}
    with sqlite3.connect(root / native.MEMORY_DATABASE) as db:
        db.execute(
            f"INSERT INTO jobs ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            list(values.values()),
        )


def output(root, tid, at):
    with sqlite3.connect(root / native.MEMORY_DATABASE) as db:
        db.execute(
            "INSERT INTO stage1_outputs (thread_id,source_updated_at) VALUES (?,?)", (tid, at)
        )


def test_only_supplied_eligible_threads_are_pending(store):
    thread(store, "eligible", ago=4000)
    thread(store, "idle-boundary", ago=3600)
    thread(store, "age-boundary", ago=10 * 86400)
    thread(store, "fresh", ago=3599)
    thread(store, "old", ago=10 * 86400 + 1)
    thread(store, "disabled", mode="disabled")
    thread(store, "polluted", mode="polluted")
    thread(store, "archived", archived=1)
    thread(store, "unrelated-owner", ago=4000)
    thread(store, "not-interactive", source='{"subagent":"chatgpt"}')
    thread(store, "empty-preview", preview="")
    thread(store, "null-preview", preview=None)
    thread(store, "null-milliseconds", milliseconds=None)
    ids = [
        "eligible",
        "idle-boundary",
        "age-boundary",
        "fresh",
        "old",
        "disabled",
        "polluted",
        "archived",
        "not-interactive",
        "empty-preview",
        "null-preview",
        "null-milliseconds",
        "missing",
        "eligible",
        "' OR 1=1 --",
    ]
    result = native.snapshot(store, ids, NOW)
    assert result == native.NativeMemoryState(
        ("idle-boundary", "eligible", "age-boundary"), False, False
    )


@pytest.mark.parametrize(
    "source,expected",
    [
        ("cli", True),
        ("vscode", True),
        ("atlas", True),
        ("chatgpt", True),
        ('"cli"', False),
        ('{"custom":"atlas"}', False),
        ('{"custom":"chatgpt"}', False),
    ],
)
def test_interactive_sources_match_native_exact_sql_values(store, source, expected):
    thread(store, "t", source=source)
    assert native.snapshot(store, ["t"], NOW).pending_threads == (("t",) if expected else ())


def test_milliseconds_override_legacy_timestamp_and_watermarks_use_seconds(store):
    source_ms = (NOW - 3601) * 1000 + 999
    thread(store, "up-to-date", ago=100, milliseconds=source_ms)
    thread(store, "stale", ago=100, milliseconds=source_ms)
    thread(store, "fresh", ago=8000, milliseconds=(NOW - 3600) * 1000 + 1)
    output(store, "up-to-date", source_ms // 1000)
    output(store, "stale", source_ms // 1000 - 1)
    assert native.snapshot(store, ["up-to-date", "stale", "fresh"], NOW).pending_threads == (
        "stale",
    )


def test_age_boundary_uses_native_integer_millisecond_clock(store):
    thread(store, "age-boundary", ago=10 * 86400)
    assert native.snapshot(store, ["age-boundary"], NOW + 0.0001).pending_threads == (
        "age-boundary",
    )


def test_success_without_output_uses_job_watermark(store):
    at = thread(store, "no-output")
    thread(store, "stale-success")
    job(store, "no-output", kind="memory_stage1", status="done", last_success_watermark=at)
    job(store, "stale-success", kind="memory_stage1", status="done", last_success_watermark=at - 1)
    result = native.snapshot(store, ["no-output", "stale-success"], NOW)
    assert result.pending_threads == ("stale-success",)
    assert not result.consolidation_pending


@pytest.mark.parametrize("newer", [False, True])
@pytest.mark.parametrize("blocked", ["exhausted", "backoff"])
def test_stage1_retries_require_budget_and_backoff_unless_source_advanced(store, newer, blocked):
    at = thread(store, "t")
    job(
        store,
        "t",
        kind="memory_stage1",
        status="error",
        input_watermark=at - int(newer),
        retry_remaining=0 if blocked == "exhausted" else 3,
        retry_at=NOW + 100 if blocked == "backoff" else None,
    )
    assert native.snapshot(store, ["t"], NOW).pending_threads == (("t",) if newer else ())


@pytest.mark.parametrize("kind", ["memory_stage1", "memory_consolidate_global"])
@pytest.mark.parametrize(
    "lease,expected", [(NOW + 1, True), (NOW, False), (NOW - 1, False), (None, False)]
)
def test_any_memory_job_with_a_live_lease_blocks_even_if_thread_not_supplied(
    store, kind, lease, expected
):
    job(
        store,
        "global" if kind.endswith("global") else "another-thread",
        kind=kind,
        status="running",
        lease_until=lease,
    )
    assert native.snapshot(store, [], NOW).running is expected


def test_nonmemory_jobs_do_not_block(store):
    job(store, "other", kind="unrelated", status="running", lease_until=NOW + 100)
    assert not native.snapshot(store, [], NOW).running


@pytest.mark.parametrize("status", ["pending", "error", "running"])
def test_phase2_unfinished_jobs_retry_without_new_stage1_input(store, status):
    job(
        store,
        status=status,
        lease_until=NOW - 1,
        retry_remaining=0,
        last_error="private diagnostic",
    )
    assert native.snapshot(store, [], NOW).consolidation_pending


@pytest.mark.parametrize("done,expected", [(9, True), (10, False), (11, False)])
def test_phase2_watermarks_survive_done_status(store, done, expected):
    job(store, status="done", input_watermark=10, last_success_watermark=done)
    assert native.snapshot(store, [], NOW).consolidation_pending is expected


def test_phase2_backoff_and_success_cooldown_expire_without_losing_pending_state(store):
    job(
        store,
        status="pending",
        input_watermark=20,
        last_success_watermark=10,
        finished_at=NOW,
        retry_at=NOW + 50,
    )
    assert not native.snapshot(store, [], NOW + 49).consolidation_pending
    assert not native.snapshot(store, [], NOW + 6 * 3600 - 1).consolidation_pending
    assert native.snapshot(store, [], NOW + 6 * 3600).consolidation_pending


def test_phase2_failure_bypasses_success_cooldown_but_honors_backoff(store):
    job(store, status="error", finished_at=NOW, retry_at=NOW + 50, last_error="private diagnostic")
    assert not native.snapshot(store, [], NOW + 49).consolidation_pending
    assert native.snapshot(store, [], NOW + 50).consolidation_pending


def test_missing_phase2_job_with_an_owner_output_still_needs_consolidation(store):
    at = thread(store, "t")
    output(store, "t", at)
    result = native.snapshot(store, ["t"], NOW)
    assert result.pending_threads == () and result.consolidation_pending


def test_missing_or_incompatible_databases_raise_without_creating_files(tmp_path, store):
    before = sorted(tmp_path.iterdir())
    with pytest.raises(sqlite3.Error):
        native.snapshot(tmp_path, [], NOW)
    assert sorted(tmp_path.iterdir()) == before
    with sqlite3.connect(store / native.MEMORY_DATABASE) as db:
        db.execute("DROP TABLE jobs")
    with pytest.raises(sqlite3.Error):
        native.snapshot(store, [], NOW)


def test_metadata_reads_never_select_text_or_change_database_bytes(store, monkeypatch):
    at = thread(store, "t")
    output(store, "t", at - 1)
    job(store, last_error="private error must never be returned")
    files = sorted(store.iterdir())
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    connect = sqlite3.connect
    selected = set()

    def checked_connect(*args, **kwargs):
        assert kwargs["uri"] is True and "mode=ro" in args[0]
        connection = connect(*args, **kwargs)

        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_READ:
                assert column not in {"title", "raw_memory", "rollout_summary", "body"}
                selected.add((table, column))
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(native.sqlite3, "connect", checked_connect)
    result = native.snapshot(store, ["t"], NOW)
    assert result.pending_threads == ("t",)
    assert "private" not in repr(result)
    assert ("jobs", "last_success_watermark") in selected
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in store.iterdir()} == before


def test_committed_wal_metadata_is_visible_and_reader_does_not_modify_db_or_wal(store):
    with sqlite3.connect(store / native.MEMORY_DATABASE) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute(
            "INSERT INTO jobs (kind,job_key,status,lease_until) VALUES (?,?,?,?)",
            ("memory_stage1", "other", "running", NOW + 50),
        )
        writer.commit()
        watched = [store / native.MEMORY_DATABASE, store / (native.MEMORY_DATABASE + "-wal")]
        before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in watched]
        assert native.snapshot(store, [], NOW).running
        assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in watched] == before
