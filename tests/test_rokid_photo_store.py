import asyncio
import base64
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from babata.rokid_gateway import GatewaySettings, create_app, session_key
from babata.rokid_photo_store import (
    ARCHIVE_VERSION,
    MANIFEST_NAME,
    STORE_DIR_MODE,
    PhotoArchive,
    decode_data_url,
)
from babata.rokid_photos import normalize_photo

TOKEN = "synthetic-archive-token-" + "x" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}


def image_bytes(size=(16, 12)):
    output = BytesIO()
    with Image.new("RGB", size, "red") as image:
        image.save(output, format="JPEG")
    return output.getvalue()


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


def normalized_jpeg():
    return decode_data_url(normalize_photo(encoded(image_bytes()), "image/jpeg"))


def photo_payload(**overrides):
    return {
        "message": "这张照片是什么？",
        "session_id": "voice-memory",
        "remember": True,
        "image": {"data_base64": encoded(image_bytes()), "mime_type": "image/jpeg"},
        **overrides,
    }


def device_result_payload(**overrides):
    return {
        "session_id": "voice-memory",
        "request_id": str(uuid4()),
        "action_id": str(uuid4()),
        "status": "ok",
        "image": {"data_base64": encoded(image_bytes()), "mime_type": "image/jpeg"},
        **overrides,
    }


def entries(root):
    path = root / MANIFEST_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@asynccontextmanager
async def client_for(handler, **overrides):
    settings = GatewaySettings(_env_file=None, enabled=True, token=TOKEN, **overrides)
    app = create_app(settings, transport=httpx.MockTransport(handler))
    application = app.app.app
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as client:
            yield client


def test_save_keeps_the_bounded_jpeg_sidecar_and_one_manifest_line(tmp_path):
    root = tmp_path / "photos"
    archive = PhotoArchive(root)
    jpeg = normalized_jpeg()
    record = archive.save(
        jpeg, session="rokid-abc", user_id="user", kind="photo", source_mime="image/png"
    )
    assert archive.enabled and record is not None
    assert record["id"].startswith("photo-")
    assert len(record["id"]) == len("photo-20261002-163000-abcdef")
    assert record["sha256"] == hashlib.sha256(jpeg).hexdigest()
    assert record["bytes"] == len(jpeg) and (record["width"], record["height"]) == (16, 12)
    assert record["kind"] == "photo" and record["source_mime"] == "image/png"
    assert record["description"] is None
    assert record["file"] == f"{record['date']}/{record['id']}.jpg"
    stored = root / record["file"]
    assert stored.read_bytes() == jpeg
    assert json.loads((root / record["date"] / f"{record['id']}.json").read_text("utf-8")) == record
    written = entries(root)
    assert len(written) == 1 and written[0]["event"] == "stored"
    assert written[0]["id"] == record["id"] and "description" not in written[0]
    assert stored.stat().st_mode & 0o777 == 0o640
    # Directory modes are only applied at creation; the shared archive group is
    # provisioned on the server and covered by the deployment probe there.
    assert stored.parent.stat().st_mode & 0o777 == 0o750
    assert STORE_DIR_MODE == 0o750
    assert "data:image" not in json.dumps(written)


def test_describe_attaches_the_spoken_answer_to_the_same_photo(tmp_path):
    root = tmp_path / "photos"
    archive = PhotoArchive(root)
    record = archive.save(normalized_jpeg(), session="rokid-abc", user_id="user", kind="photo")
    archive.describe(record, "  一张红色图片  ")
    written = entries(root)
    assert [entry["event"] for entry in written] == ["stored", "described"]
    assert written[-1] == {
        "version": ARCHIVE_VERSION,
        "event": "described",
        "id": record["id"],
        "date": record["date"],
        "file": record["file"],
        "description": "一张红色图片",
    }
    sidecar = root / record["date"] / f"{record['id']}.json"
    assert json.loads(sidecar.read_text("utf-8"))["description"] == "一张红色图片"
    archive.describe(record, "   ")
    archive.describe(None, "ignored")
    assert len(entries(root)) == 2


def test_archive_is_disabled_without_a_configured_directory():
    archive = PhotoArchive(None)
    assert not archive.enabled
    assert archive.save(normalized_jpeg(), session="s", user_id="u", kind="photo") is None
    assert archive.describe({"id": "x"}, "ignored") is None


def test_storage_failure_returns_none_and_logs_no_image_data(tmp_path, caplog):
    root = tmp_path / "photos"
    root.write_text("a file blocks the directory")
    archive = PhotoArchive(root)
    caplog.set_level(logging.WARNING, logger="babata.rokid_photo_store")
    assert archive.save(normalized_jpeg(), session="s", user_id="u", kind="photo") is None
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1 and messages[0].startswith("Rokid photo archive write failed: ")
    assert "data:image" not in caplog.text and encoded(image_bytes()) not in caplog.text
    assert not (root / "manifest.jsonl").exists()


def test_normalized_data_url_is_required():
    for value in ("", "not-a-data-url", "data:image/png;base64,AAAA", "data:image/jpeg;base64,!!!"):
        with pytest.raises(ValueError):
            decode_data_url(value)


def test_relative_store_directory_is_rejected():
    with pytest.raises(ValidationError):
        GatewaySettings(_env_file=None, enabled=True, token=TOKEN, photo_store_dir="photos")


def test_photo_request_archives_the_photo_and_its_answer(tmp_path):
    async def check():
        def handler(request):
            body = json.loads(request.content)
            assert request.url.path == "/chat"
            assert body["session_id"] == session_key("voice-memory")
            assert body["image_data_url"].startswith("data:image/jpeg;base64,")
            return httpx.Response(200, json={"reply": "一张红色图片"})

        async with client_for(handler, photo_store_dir=str(tmp_path / "photos")) as client:
            response = await client.post("/v1/photo", headers=AUTH, json=photo_payload())
        assert response.status_code == 200
        assert response.json()["reply"] == "一张红色图片"

    asyncio.run(check())
    root = tmp_path / "photos"
    stored, described = entries(root)
    assert stored["event"] == "stored" and described["event"] == "described"
    assert stored["session"] == session_key("voice-memory") and stored["kind"] == "photo"
    assert described["id"] == stored["id"] and described["description"] == "一张红色图片"
    assert hashlib.sha256((root / stored["file"]).read_bytes()).hexdigest() == stored["sha256"]


def test_device_result_photo_is_archived_with_its_own_kind(tmp_path):
    async def check():
        def handler(request):
            assert request.url.path == "/chat/device-result"
            return httpx.Response(200, json={"reply": "照片里是一个红点"})

        async with client_for(handler, photo_store_dir=str(tmp_path / "photos")) as client:
            response = await client.post(
                "/v1/device-result", headers=AUTH, json=device_result_payload()
            )
        assert response.status_code == 200

    asyncio.run(check())
    stored, described = entries(tmp_path / "photos")
    assert stored["kind"] == "device-result"
    assert described["description"] == "照片里是一个红点"


def test_photo_is_kept_when_the_model_call_fails(tmp_path):
    async def check():
        def handler(request):
            return httpx.Response(502, json={"detail": "upstream"})

        async with client_for(handler, photo_store_dir=str(tmp_path / "photos")) as client:
            response = await client.post("/v1/photo", headers=AUTH, json=photo_payload())
        assert response.status_code == 502

    asyncio.run(check())
    written = entries(tmp_path / "photos")
    assert [entry["event"] for entry in written] == ["stored"]


def test_unusable_archive_directory_never_changes_the_answer(tmp_path, caplog):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")

    async def check():
        def handler(request):
            return httpx.Response(200, json={"reply": "仍然回答"})

        async with client_for(handler, photo_store_dir=str(blocked)) as client:
            response = await client.post("/v1/photo", headers=AUTH, json=photo_payload())
        assert response.status_code == 200 and response.json()["reply"] == "仍然回答"

    caplog.set_level(logging.WARNING, logger="babata.rokid_photo_store")
    asyncio.run(check())
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1 and messages[0].startswith("Rokid photo archive write failed: ")


def test_gateway_without_a_store_directory_writes_nothing(tmp_path):
    async def check():
        def handler(request):
            return httpx.Response(200, json={"reply": "普通回答"})

        async with client_for(handler) as client:
            response = await client.post("/v1/photo", headers=AUTH, json=photo_payload())
        assert response.status_code == 200 and response.json()["reply"] == "普通回答"

    asyncio.run(check())
    assert list(tmp_path.iterdir()) == []
