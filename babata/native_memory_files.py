"""Read completed native memory documents; never alter a Codex memory directory."""

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path, PurePosixPath


def allowed_path(value):
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("..", ".") or x.startswith(".") for x in p.parts):
        return False
    if str(p) != value or len(value) > 1000:
        return False
    return value in ("MEMORY.md", "memory_summary.md") or (
        len(p.parts) >= 2 and p.parts[0] in ("rollout_summaries", "skills") and p.suffix == ".md"
    )


def redact(value):
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[private key removed]",
        value,
        flags=re.S,
    )
    value = re.sub(
        r"\b(?:sk-|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}",
        "[credential removed]",
        value,
    )
    return re.sub(
        r"(?im)((?:api[_ -]?key|password|secret|access_token|密码)\s*[:=：]\s*)[^\s,;]+",
        r"\1[credential removed]",
        value,
    ).replace("\x00", "")


def snapshot(root, quiet_seconds=30):
    """None means unavailable/in-flight; an empty list means an observed empty store.

    Only generated Markdown is shared. Raw rollouts, SQLite, credentials, .git,
    scripts and extension inputs are excluded. Symlinks are never followed.
    """
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        return None
    if (root / ".git").is_dir():
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            timeout=10,
            check=True,
        )
        if status.stdout:
            return None  # Native Phase 2 resets this baseline only after success.
    paths = sorted(p for p in root.rglob("*.md") if allowed_path(p.relative_to(root).as_posix()))
    files, signatures = [], []
    total = 0
    for p in paths:
        relative = p.relative_to(root)
        if any(
            root.joinpath(*relative.parts[:i]).is_symlink()
            for i in range(1, len(relative.parts) + 1)
        ):
            continue
        info = p.stat()
        if time.time() - info.st_mtime < quiet_seconds:
            return None
        if info.st_size > 2_000_000:
            raise ValueError("Memory document exceeds size limit")
        total += info.st_size
        if total > 20_000_000 or len(files) >= 2000:
            raise ValueError("Memory snapshot exceeds size limit")
        files.append(
            {
                "path": p.relative_to(root).as_posix(),
                "content": redact(p.read_text(encoding="utf-8")),
                "modified_at": info.st_mtime,
            }
        )
        signatures.append((p, info.st_size, info.st_mtime_ns))
    for p, size, mtime in signatures:
        info = p.stat()
        if (info.st_size, info.st_mtime_ns) != (size, mtime):
            return None
    if paths != sorted(
        p for p in root.rglob("*.md") if allowed_path(p.relative_to(root).as_posix())
    ):
        return None
    digest = hashlib.sha256(
        json.dumps(files, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return {"files": files, "digest": digest}
