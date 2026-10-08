#!/usr/bin/env python3
"""Create fresh private instance settings without contacting a model or server."""

import argparse
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def https_origin(value: str) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError("Public URL must be a valid HTTPS origin") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or not value.isascii()
        or any(char.isspace() for char in value)
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError("Public URL must be an HTTPS origin without credentials or a path")
    return value.rstrip("/")


def dotenv_value(value: str) -> str:
    # Compose does not interpolate single-quoted .env values. Its parser keeps
    # backslashes literal; escape quotes only, or a key would change on loading.
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("Environment values must be single-line text")
    return "'" + value.replace("'", "\\'") + "'"


def exclusive_write(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)


def create_instance(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.project):
        raise ValueError("Project must be 1-63 lowercase letters, digits or hyphens")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.owner):
        raise ValueError("Owner must be 1-64 letters, digits, underscores or hyphens")
    if not 1024 <= args.gateway_port <= 65535:
        raise ValueError("Gateway port must be between 1024 and 65535")
    public_url = https_origin(args.public_url)
    try:
        api_key = (
            args.api_key_file.read_text(encoding="utf-8").strip()
            if args.api_key_file
            else os.environ.get("LLM_API_KEY", "")
        )
    except (OSError, UnicodeError):
        raise ValueError("Cannot read the supplied private API-key file") from None
    if not api_key or not api_key.isascii() or any(char.isspace() for char in api_key):
        raise ValueError("Supply your own API key through --api-key-file or LLM_API_KEY")
    env_path = ROOT / ".env"
    client_path = args.client_config.expanduser().absolute()
    resolved_client_path = client_path.resolve()
    if resolved_client_path == ROOT or ROOT in resolved_client_path.parents:
        raise ValueError("Client configuration must be outside the repository")
    if env_path.exists() or env_path.is_symlink():
        raise ValueError(".env already exists; refusing to overwrite or rotate credentials")
    if client_path.exists() or client_path.is_symlink():
        raise ValueError("Client configuration already exists; choose a fresh private path")
    # Use the code's API-key authentication. Do not copy an existing Codex home,
    # account/login, skills, conversation history or native memory directory.
    is_deepseek = args.provider == "deepseek"
    token = secrets.token_urlsafe(48)
    values = {
        "COMPOSE_PROJECT_NAME": args.project,
        "CODEX_USER_ID": args.owner,
        "TOKYO_CODEX_VERSION": "0.160.0",
        "BABATA_SECCOMP_PROFILE": str(ROOT / "deploy/security/seccomp-native.json"),
        "LLM_PROVIDER": args.provider,
        "LLM_API_STYLE": "responses",
        "LLM_MODEL": "deepseek-flash" if is_deepseek else "gpt-5.6-luna",
        "LLM_API_KEY": api_key,
        "LLM_BASE_URL": "https://api.deepseek.com" if is_deepseek else "https://api.openai.com/v1",
        "LLM_TIMEOUT_SECONDS": "90",
        "LLM_MAX_TOKENS": "4096",
        "TOKYO_NATIVE_MEMORIES": "true",
        "TOKYO_MEMORY_EXTRACT_MODEL": "deepseek-flash" if is_deepseek else "gpt-5.6-luna",
        "TOKYO_MEMORY_CONSOLIDATION_MODEL": "deepseek-flash" if is_deepseek else "gpt-5.6-terra",
        "TOKYO_MODEL_CONTEXT_WINDOW": "1048576" if is_deepseek else "0",
        "TOKYO_AUTO_COMPACT_TOKEN_LIMIT": "900000" if is_deepseek else "0",
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "ROKID_GATEWAY_TOKEN": token,
        "ROKID_GATEWAY_PORT": str(args.gateway_port),
        "ROKID_GATEWAY_TIMEOUT_SECONDS": "90",
    }
    env_text = "# Private instance configuration. Never upload or print this file.\n"
    env_text += "".join(f"{key}={dotenv_value(value)}\n" for key, value in values.items())
    client_text = (
        json.dumps(
            {
                "endpoint": public_url + "/v1/chat",
                "token": token,
                "sessionId": "voice-memory",
                "timeoutMs": 120000,
            },
            indent=2,
        )
        + "\n"
    )
    client_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        exclusive_write(env_path, env_text)
        written.append(env_path)
        exclusive_write(client_path, client_text)
        written.append(client_path)
    except Exception:
        for path in reversed(written):
            path.unlink()
        raise
    print("Created private .env and external Rokid configuration; no credentials printed.")
    print("No containers started. Verify Linux sandbox policies and configure HTTPS before use.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Unique Compose project for this person")
    parser.add_argument("--owner", default="user", help="Owner ID bound by the gateway")
    parser.add_argument(
        "--public-url", required=True, help="HTTPS origin, e.g. https://glasses.example.com"
    )
    parser.add_argument(
        "--client-config", required=True, type=Path, help="New private file outside this repository"
    )
    parser.add_argument("--provider", choices=["openai", "deepseek"], default="openai")
    parser.add_argument(
        "--api-key-file", type=Path, help="Private file containing only your provider API key"
    )
    parser.add_argument(
        "--gateway-port", type=int, default=8081, help="Unused loopback port for this instance"
    )
    args = parser.parse_args()
    try:
        create_instance(args)
    except ValueError as error:
        # Every validation message above is fixed text, never input contents.
        parser.exit(1, str(error) + "; no credentials printed.\n")
    except (OSError, UnicodeError):
        parser.exit(
            1,
            "Setup failed: outputs could not be created at the private paths; "
            "no credentials printed.\n",
        )


if __name__ == "__main__":
    main()
