"""Read-only scheduling metadata for the pinned Codex 0.155.1 native memory store.

No rollout, message, memory text, or error text is selected. Native job watermarks
remain authoritative; this module neither claims jobs nor changes their retries.
The 10-day age limit is Codex's default; Tokyo overrides its idle limit to one hour.
"""

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

STATE_DATABASE = "state_5.sqlite"
MEMORY_DATABASE = "memories_1.sqlite"
MAX_AGE_SECONDS = 10 * 24 * 3600
MIN_IDLE_SECONDS = 3600
CONSOLIDATION_COOLDOWN_SECONDS = 6 * 3600


@dataclass(frozen=True)
class NativeMemoryState:
    pending_threads: tuple[str, ...]
    running: bool
    consolidation_pending: bool


def _consolidation_pending(row: sqlite3.Row | None, now: float, has_output: bool) -> bool:
    if row is None:
        return has_output
    if row["status"] == "running" and (row["lease_until"] or 0) > now:
        return False
    if row["retry_at"] is not None and row["retry_at"] > now:
        return False
    # Native phase 2 also applies its success cooldown to a subsequently queued job.
    if (
        not row["failed"]
        and row["finished_at"] is not None
        and row["finished_at"] > now - CONSOLIDATION_COOLDOWN_SECONDS
    ):
        return False
    return row["status"] in {"pending", "error", "running"} or (
        (row["input_watermark"] or 0) > (row["last_success_watermark"] or 0)
    )


def snapshot(root: Path, thread_ids: list[str], now: float) -> NativeMemoryState:
    """Return work currently eligible to trigger, plus any live native memory job.

    ``root`` is Tokyo's CODEX_HOME, not its memories directory. The caller must
    supply only owner Rokid IDs from its trusted session mapping. All pending
    thread IDs are intersected with that list. Backoff/cooldown skips are checked
    again on the next call; no completed watermark is inferred or persisted here.

    Missing/unreadable databases or incompatible schemas raise rather than
    reporting an idle/completed store. Read-only URI mode preserves committed WAL
    visibility; immutable mode would incorrectly ignore a running writer's WAL.
    """
    if not math.isfinite(now):
        raise ValueError("Invalid native memory snapshot time")
    now_ms = math.floor(now * 1000)
    root = Path(root).resolve()
    state_uri = (root / STATE_DATABASE).as_uri() + "?mode=ro"
    memory_uri = (root / MEMORY_DATABASE).as_uri() + "?mode=ro"
    pending = []
    has_output = False
    with closing(sqlite3.connect(state_uri, uri=True, timeout=1)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("ATTACH DATABASE ? AS memory", (memory_uri,))
        connection.execute("BEGIN")
        # Deliberately cover all memory jobs: a different owner thread can hold
        # the same global native pipeline resources. No other thread IDs are read.
        running = bool(
            connection.execute(
                """SELECT EXISTS(SELECT 1 FROM memory.jobs
                    WHERE kind IN ('memory_stage1','memory_consolidate_global')
                      AND status='running' AND lease_until>?)""",
                (now,),
            ).fetchone()[0]
        )
        identifiers = sorted(set(thread_ids))
        for start in range(0, len(identifiers), 400):
            batch = identifiers[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"""SELECT t.id,t.updated_at_ms AS updated_ms,t.memory_mode,t.archived,
                    o.source_updated_at,
                    j.input_watermark,j.last_success_watermark,j.retry_at,j.retry_remaining
                    FROM main.threads t
                    LEFT JOIN memory.stage1_outputs o ON o.thread_id=t.id
                    LEFT JOIN memory.jobs j ON j.kind='memory_stage1' AND j.job_key=t.id
                    WHERE t.id IN ({placeholders}) AND t.updated_at_ms IS NOT NULL
                      AND t.preview<>'' AND t.source IN ('cli','vscode','atlas','chatgpt')""",
                batch,
            )
            for row in rows:
                updated_ms = row["updated_ms"]
                has_output = has_output or row["source_updated_at"] is not None
                if (
                    row["memory_mode"] != "enabled"
                    or row["archived"] != 0
                    or not now_ms - MAX_AGE_SECONDS * 1000
                    <= updated_ms
                    <= now_ms - MIN_IDLE_SECONDS * 1000
                ):
                    continue
                source_time = updated_ms // 1000
                if any(
                    watermark is not None and watermark >= source_time
                    for watermark in (row["source_updated_at"], row["last_success_watermark"])
                ):
                    # The job watermark covers successful extraction with no output too.
                    continue
                newer_input = row["input_watermark"] is None or source_time > row["input_watermark"]
                if not newer_input and (
                    (row["retry_remaining"] is not None and row["retry_remaining"] <= 0)
                    or (row["retry_at"] is not None and row["retry_at"] > now)
                ):
                    continue
                pending.append((updated_ms, row["id"]))
        global_job = connection.execute(
            """SELECT status,lease_until,retry_at,finished_at,
                (last_error IS NOT NULL) AS failed,input_watermark,last_success_watermark
                FROM memory.jobs WHERE kind='memory_consolidate_global' AND job_key='global'"""
        ).fetchone()
        consolidation_pending = _consolidation_pending(global_job, now, has_output)
    return NativeMemoryState(
        tuple(tid for _, tid in sorted(pending, key=lambda item: (-item[0], item[1]))),
        running,
        consolidation_pending,
    )
