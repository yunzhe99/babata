import os
import time

import pytest

from babata.native_memories import MemorySnapshot
from babata.native_memory_files import allowed_path, snapshot


def test_snapshot_only_generated_markdown_and_never_follows_links(tmp_path):
    root = tmp_path / "memories"
    root.mkdir()
    (root / "MEMORY.md").write_text("known preference")
    (root / "raw_memories.md").write_text("raw input")
    (root / "auth.json").write_text("credential")
    (root / "rollout_summaries").mkdir()
    (root / "rollout_summaries/summary.md").write_text("api_key=sk-" + "x" * 40)
    (root / "rollout_summaries/raw.jsonl").write_text("raw reasoning and tools")
    (root / "rollout_summaries/link.md").symlink_to(root / "auth.json")
    result = snapshot(root, quiet_seconds=0)
    assert {x["path"] for x in result["files"]} == {"MEMORY.md", "rollout_summaries/summary.md"}
    assert "sk-" not in str(result) and "credential removed" in str(result)
    assert snapshot(root) is None  # Wait for in-progress native writes to settle.
    assert snapshot(tmp_path / "not-mounted") is None
    for file in root.rglob("*.md"):
        if not file.is_symlink():
            os.utime(file, (time.time() - 60, time.time() - 60))
    assert snapshot(root) is not None


@pytest.mark.parametrize(
    "path",
    [
        "../MEMORY.md",
        "/MEMORY.md",
        "skills/../auth.md",
        "skills/.git/config.md",
        "raw_memories.md",
        "rollout_summaries/a.jsonl",
    ],
)
def test_upload_rejects_unapproved_paths(path):
    assert not allowed_path(path)
    with pytest.raises(ValueError):
        MemorySnapshot(source="mac", files=[{"path": path, "content": "x", "modified_at": 0}])
