"""Install one pinned official native Codex package, verifying npm's SHA-512 digest."""

import base64
import hashlib
import io
import json
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

version = sys.argv[1]
arch = {"x86_64": "x64", "aarch64": "arm64"}[platform.machine()]
metadata = json.load(
    urllib.request.urlopen(
        f"https://registry.npmjs.org/@openai/codex/{version}-linux-{arch}", timeout=60
    )
)
payload = urllib.request.urlopen(metadata["dist"]["tarball"], timeout=180).read()
expected = metadata["dist"]["integrity"]
assert expected == "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode()
with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
    archive.extractall("/opt/codex", filter="data")
    member = next(m for m in archive.getmembers() if m.name.endswith("/bin/codex"))
    Path("/usr/local/bin/codex").symlink_to(Path("/opt/codex") / member.name)
print("Installed Codex", version, "sha512 verified")
