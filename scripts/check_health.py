#!/usr/bin/env python3
"""Check a running Compose instance and rejected unauthenticated traffic, with no model call."""

import argparse
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url",
        default="http://127.0.0.1:8081",
        help="HTTP loopback or public HTTPS origin",
    )
    parser.add_argument(
        "--tls", action="store_true", help="Use the HTTPS Compose overlay for core inspection"
    )
    args = parser.parse_args()
    command = ["docker", "compose", "-f", str(ROOT / "compose.yaml")]
    if args.tls:
        command.extend(["-f", str(ROOT / "deploy/https/compose.yaml")])
    command.extend(
        [
            "exec",
            "-T",
            "babata",
            "python",
            "-c",
            "import json, urllib.request; "
            "print(json.load(urllib.request.urlopen("
            "'http://127.0.0.1:8000/health', timeout=5))['status'])",
        ]
    )
    try:
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, check=False, timeout=30
        )
        if result.returncode or result.stdout.strip() != "ok":
            parser.exit(1, "Private backend health check failed; inspect local logs privately.\n")
        request = urllib.request.Request(
            args.gateway_url.rstrip("/") + "/v1/chat", data=b"{}", method="POST"
        )
        try:
            urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                parser.exit(1, "Gateway did not return the expected 401 rejection.\n")
        else:
            parser.exit(1, "Gateway accepted unauthenticated traffic; stop before using it.\n")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        parser.exit(1, "Health check could not complete; inspect local deployment privately.\n")
    print(json.dumps({"backend_health": "ok", "unauthenticated_gateway": 401, "model_calls": 0}))
    print(
        "Chat, recall after restart, photo persistence, native sandbox and "
        "separate-user acceptance remain to be tested."
    )


if __name__ == "__main__":
    main()
