"""Publication deployment checks use fresh temporary secrets, never a real server."""

import argparse
import importlib.util
import json
import re
import stat
from pathlib import Path

import pytest
from dotenv import dotenv_values

PUBLIC_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "setup_instance", PUBLIC_ROOT / "scripts/setup_instance.py"
)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def arguments(root, owner="user", provider="openai"):
    return argparse.Namespace(
        project=f"babata-{owner}",
        owner=owner,
        public_url=f"https://{owner}.example.com",
        client_config=root.parent / f"{owner}.json",
        provider=provider,
        api_key_file=None,
        gateway_port=8081,
    )


def instance(tmp_path, monkeypatch, name, provider="openai"):
    root = tmp_path / name
    root.mkdir()
    monkeypatch.setattr(setup, "ROOT", root)
    monkeypatch.setenv("LLM_API_KEY", "sk-offline-test-$-quote'\\-key")
    setup.create_instance(arguments(root, name, provider))
    return root, dotenv_values(root / ".env"), json.loads((tmp_path / f"{name}.json").read_text())


def test_fresh_instances_have_distinct_credentials_and_owner_bindings(
    tmp_path, monkeypatch, capsys
):
    first_root, first, first_client = instance(tmp_path, monkeypatch, "alice")
    second_root, second, second_client = instance(tmp_path, monkeypatch, "bob", "deepseek")
    assert first["CODEX_USER_ID"] == "alice"
    assert second["CODEX_USER_ID"] == "bob"
    assert first["COMPOSE_PROJECT_NAME"] != second["COMPOSE_PROJECT_NAME"]
    assert first["POSTGRES_PASSWORD"] != second["POSTGRES_PASSWORD"]
    assert first["ROKID_GATEWAY_TOKEN"] != second["ROKID_GATEWAY_TOKEN"]
    assert first_client["token"] == first["ROKID_GATEWAY_TOKEN"]
    assert second_client["token"] == second["ROKID_GATEWAY_TOKEN"]
    assert (
        second["LLM_MODEL"]
        == second["TOKYO_MEMORY_EXTRACT_MODEL"]
        == second["TOKYO_MEMORY_CONSOLIDATION_MODEL"]
        == "deepseek-flash"
    )
    assert second["TOKYO_MODEL_CONTEXT_WINDOW"] == "1048576"
    assert second["TOKYO_AUTO_COMPACT_TOKEN_LIMIT"] == "900000"
    for root, client in (
        (first_root, tmp_path / "alice.json"),
        (second_root, tmp_path / "bob.json"),
    ):
        assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
        assert stat.S_IMODE(client.stat().st_mode) == 0o600
    output = capsys.readouterr().out
    assert first["LLM_API_KEY"] not in output
    assert first["ROKID_GATEWAY_TOKEN"] not in output
    assert second["ROKID_GATEWAY_TOKEN"] not in output


def test_dotenv_secret_round_trip_does_not_expand_dollars(tmp_path, monkeypatch):
    _, values, _ = instance(tmp_path, monkeypatch, "user")
    assert values["LLM_API_KEY"] == "sk-offline-test-$-quote'\\-key"


def test_repeat_setup_preserves_existing_credentials(tmp_path, monkeypatch):
    root, _, _ = instance(tmp_path, monkeypatch, "user")
    original = (root / ".env").read_bytes()
    with pytest.raises(ValueError, match="refusing to overwrite"):
        setup.create_instance(arguments(root))
    assert (root / ".env").read_bytes() == original


def test_client_configuration_cannot_be_generated_inside_repository(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(setup, "ROOT", root)
    monkeypatch.setenv("LLM_API_KEY", "sk-offline-example-key")
    args = arguments(root)
    args.client_config = root / "config.json"
    with pytest.raises(ValueError, match="outside the repository"):
        setup.create_instance(args)
    assert not (root / ".env").exists()


@pytest.mark.parametrize("project", ["alice_", "alice___bob"])
def test_project_names_cannot_create_invalid_docker_image_names(tmp_path, monkeypatch, project):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(setup, "ROOT", root)
    args = arguments(root)
    args.project = project
    with pytest.raises(ValueError, match="letters, digits or hyphens"):
        setup.create_instance(args)
    assert not (root / ".env").exists()


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "https://u:p@example.com",
        "https://example.com/path",
        "https://example.com?token=test",
        "https://example.com#fragment",
    ],
)
def test_public_endpoint_rejects_insecure_or_ambiguous_url(value):
    with pytest.raises(ValueError):
        setup.https_origin(value)


def test_compose_is_private_and_keeps_project_scoped_storage():
    compose = (PUBLIC_ROOT / "compose.yaml").read_text()
    assert "127.0.0.1:${ROKID_GATEWAY_PORT:-8081}:8001" in compose
    assert "ROKID_GATEWAY_PHOTO_STORE_DIR: /photos" in compose
    assert "apparmor:babata-native" in compose
    assert "seccomp:${BABATA_SECCOMP_PROFILE" in compose
    assert "internal: true" in compose
    assert not re.search(r"(?:privileged|network_mode|container_name):", compose)
    # Explicit volume names would share state across Compose projects.
    volumes = compose.split("\nvolumes:\n", 1)[1]
    assert "name:" not in volumes and "external:" not in volumes
