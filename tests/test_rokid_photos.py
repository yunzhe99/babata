import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from pydantic import ValidationError

from babata.main import ChatRequest
from babata.main import app as main_app
from babata.rokid_gateway import GatewaySettings, create_app
from babata.rokid_photos import (
    JPEG_DATA_PREFIX,
    MAX_PHOTO_BODY_BYTES,
    MAX_SOURCE_BASE64,
    NORMALIZED_ERROR,
    PHOTO_ERROR,
    PhotoValidationError,
    decode_normalized_jpeg,
    model_user_content,
    normalize_photo,
    validate_normalized_jpeg,
)

TOKEN = "synthetic-photo-token-" + "x" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}


def image_bytes(size=(16, 12), *, fmt="JPEG", mode="RGB", color="red", **kwargs):
    output = BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(output, format=fmt, **kwargs)
    return output.getvalue()


def encoded(raw):
    return base64.b64encode(raw).decode("ascii")


def normalized():
    return normalize_photo(encoded(image_bytes()), "image/jpeg")


def payload(**overrides):
    return {
        "message": "这张照片是什么？",
        "session_id": "voice-memory",
        "remember": True,
        "image": {"data_base64": encoded(image_bytes()), "mime_type": "image/jpeg"},
        **overrides,
    }


@asynccontextmanager
async def client_for(handler):
    settings = GatewaySettings(_env_file=None, enabled=True, token=TOKEN)
    app = create_app(settings, transport=httpx.MockTransport(handler))
    application = app.app.app
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
        ) as client:
            yield client


def test_photo_is_oriented_resized_and_stripped_of_private_metadata():
    exif = Image.Exif()
    exif[274] = 6  # Rotate 90 degrees clockwise.
    exif[270] = "private-location-marker"
    original = image_bytes((3000, 1500), exif=exif, comment=b"private-comment-marker")
    url = normalize_photo(encoded(original), "image/jpeg")
    output = decode_normalized_jpeg(url)
    validate_normalized_jpeg(url)
    assert b"private-" not in output
    with Image.open(BytesIO(output)) as result:
        assert result.size == (1024, 2048)
        assert result.mode == "RGB" and result.format == "JPEG"
        assert not result.getexif()
        assert "comment" not in result.info


def test_png_alpha_is_composited_on_white():
    original = image_bytes(fmt="PNG", mode="RGBA", color=(0, 0, 0, 0))
    output = decode_normalized_jpeg(normalize_photo(encoded(original), "image/png"))
    with Image.open(BytesIO(output)) as result:
        assert result.getpixel((5, 5)) == (255, 255, 255)


@pytest.mark.parametrize(
    "fmt, mode, size, color, save",
    [
        ("JPEG", "RGB", (6000, 4000), "red", {}),
        ("PNG", "RGB", (4000, 3000), "red", {}),
        ("PNG", "RGBA", (3000, 2000), (0, 0, 0, 0), {}),
        ("PNG", "LA", (3000, 2000), (0, 0), {}),
        ("PNG", "P", (3000, 2000), 0, {"transparency": 0}),
    ],
)
def test_format_specific_pixel_limit_accepts_boundary_and_rejects_next_row(
    fmt, mode, size, color, save
):
    mime = "image/jpeg" if fmt == "JPEG" else "image/png"
    for extra_row in (0, 1):
        original = image_bytes(
            (size[0], size[1] + extra_row), fmt=fmt, mode=mode, color=color, **save
        )
        if extra_row:
            with pytest.raises(PhotoValidationError, match=PHOTO_ERROR):
                normalize_photo(encoded(original), mime)
        else:
            validate_normalized_jpeg(normalize_photo(encoded(original), mime))


@pytest.mark.parametrize("many_chunks", [False, True])
def test_compressed_png_text_cannot_exhaust_memory(many_chunks):
    info = PngInfo()
    if many_chunks:
        for index in range(9):
            info.add_text(str(index), "x" * (128 * 1024), zip=True)
    else:
        info.add_text("description", "x" * (256 * 1024 + 1), zip=True)
    raw = image_bytes(fmt="PNG", pnginfo=info)
    assert len(raw) < 4096
    with pytest.raises(PhotoValidationError, match=PHOTO_ERROR):
        normalize_photo(encoded(raw), "image/png")


@pytest.mark.parametrize(
    "kind", ["invalid", "wrong_mime", "truncated", "gif", "animated", "pixels", "edge"]
)
def test_real_decoder_rejects_malformed_wrong_format_animated_and_oversized_images(kind):
    mime = "image/jpeg"
    if kind == "invalid":
        raw = b"private-secret-marker-is-not-an-image"
    elif kind == "wrong_mime":
        raw = image_bytes(fmt="PNG")
    elif kind == "truncated":
        raw = image_bytes()[:-10]
    elif kind == "gif":
        raw = image_bytes(fmt="GIF")
    elif kind == "pixels":
        raw = image_bytes((5000, 5000))
    elif kind == "edge":
        raw = image_bytes((12001, 1))
    else:
        with Image.new("RGB", (4, 4), "blue") as second:
            raw = image_bytes(fmt="PNG", save_all=True, append_images=[second])
        mime = "image/png"
    with pytest.raises(PhotoValidationError, match=PHOTO_ERROR):
        normalize_photo(encoded(raw), mime)


@pytest.mark.parametrize("value", ["%%%", "a\na=", "Zh==", "A" * (MAX_SOURCE_BASE64 + 4)])
def test_base64_is_canonical_and_bounded(value):
    with pytest.raises(PhotoValidationError):
        normalize_photo(value, "image/jpeg")


@pytest.mark.parametrize(
    "value",
    [
        "https://private.example/photo.jpg",
        "file:///private/image.jpg",
        "/tmp/photo.jpg",
        "data:image/png;base64,aGVsbG8=",
        "data:image/jpeg;base64,%%%",
        "data:image/jpeg;base64,aGVsbG8=",
    ],
)
def test_main_schema_rejects_url_path_and_invalid_jpeg_without_echoing_it(value):
    with pytest.raises(ValidationError) as error:
        ChatRequest(user_id="u", session_id="s", message="look", image_data_url=value)
    assert value not in str(error.value)


def test_main_accepts_only_small_decodable_rgb_jpeg_without_metadata():
    exif = Image.Exif()
    exif[270] = "private-location-marker"
    for raw in (
        image_bytes((2049, 1)),
        image_bytes(exif=exif),
        image_bytes(comment=b"private-comment-marker"),
        image_bytes(mode="L", color=0),
        b"\xff\xd8\xffmalformed\xff\xd9",
    ):
        with pytest.raises(PhotoValidationError, match=NORMALIZED_ERROR):
            validate_normalized_jpeg(JPEG_DATA_PREFIX + encoded(raw))
    validate_normalized_jpeg(normalized())


def test_photo_forwarding_preserves_fixed_identity_session_memory_and_no_raw_image(caplog):
    async def check():
        seen = []
        request_id = str(uuid4())
        original = payload(request_id=request_id)

        def handler(request):
            seen.append(json.loads(request.content))
            assert request.url == "http://127.0.0.1:8000/chat"
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"reply": "看到了红色图片。"})

        async with client_for(handler) as client:
            response = await client.post("/v1/photo", headers=AUTH, json=original)
        assert response.status_code == 200
        assert response.json() == {
            "session_id": "voice-memory",
            "request_id": request_id,
            "reply": "看到了红色图片。",
        }
        body = seen[0]
        assert body["user_id"] == "user" and body["session_id"].startswith("rokid-")
        assert body["request_id"] == request_id and body["remember"] is True
        assert body["mode"] == "voice" and body["message"] == original["message"]
        assert "image" not in body
        validate_normalized_jpeg(body["image_data_url"])
        assert original["image"]["data_base64"] not in caplog.text
        assert TOKEN not in caplog.text and original["message"] not in caplog.text
        records = [r.args for r in caplog.records if r.name == "uvicorn.error.rokid"]
        assert records == [("/v1/photo", 200, "POST", "missing")]

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())


def test_photo_auth_cors_and_strict_text_route_are_unchanged():
    async def check():
        def handler(request):
            raise AssertionError("Rejected requests cannot reach the model")

        async with client_for(handler) as client:
            assert (await client.post("/v1/photo", json=payload())).status_code == 401
            assert (await client.post("/v1/chat", headers=AUTH, json=payload())).status_code == 422
            response = await client.options(
                "/v1/photo",
                headers={
                    "Origin": "https://aiui.rokid.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == "https://aiui.rokid.com"
            response = await client.post(
                "/v1/photo",
                headers=AUTH,
                json=payload(
                    image={"data_base64": "private-secret-marker", "mime_type": "image/jpeg"}
                ),
            )
            assert response.status_code == 422 and response.json()["detail"] == PHOTO_ERROR
            assert "private-secret" not in response.text
            response = await client.post(
                "/v1/photo", headers=AUTH, json=payload(upstream_url="private")
            )
            assert response.status_code == 422 and "private" not in response.text

    asyncio.run(check())


@pytest.mark.parametrize("length", [None, "1", str(MAX_PHOTO_BODY_BYTES + 1)])
def test_photo_body_limit_applies_to_chunked_and_dishonest_length(length):
    async def check():
        def handler(request):
            raise AssertionError("Oversized input cannot reach upstream")

        async def chunks():
            yield b" " * (MAX_PHOTO_BODY_BYTES // 2)
            yield b" " * (MAX_PHOTO_BODY_BYTES // 2 + 1)

        headers = {**AUTH, "Content-Type": "application/json"}
        if length is not None:
            headers["Content-Length"] = length
        async with client_for(handler) as client:
            response = await client.post("/v1/photo", headers=headers, content=chunks())
        assert response.status_code == 413

    asyncio.run(check())


def test_photo_concurrency_has_no_large_request_queue_and_recovers():
    async def check():
        entered, finish = asyncio.Event(), asyncio.Event()

        async def handler(request):
            entered.set()
            await finish.wait()
            return httpx.Response(200, json={"reply": "done"})

        async with client_for(handler) as client:
            first = asyncio.create_task(client.post("/v1/photo", headers=AUTH, json=payload()))
            await asyncio.wait_for(entered.wait(), 3)
            second = await client.post("/v1/photo", headers=AUTH, json=payload())
            assert second.status_code == 429 and second.headers["retry-after"] == "5"
            finish.set()
            assert (await first).status_code == 200
            assert (await client.post("/v1/photo", headers=AUTH, json=payload())).status_code == 200

    asyncio.run(check())


@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
def test_main_http_validation_never_echoes_private_image_or_reaches_model(path):
    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main_app), base_url="http://private.test"
        ) as client:
            for value in (
                "private-image-marker",
                JPEG_DATA_PREFIX + encoded(b"\xff\xd8\xffprivate\xff\xd9"),
            ):
                response = await client.post(
                    path,
                    json={
                        "user_id": "u",
                        "session_id": "s",
                        "message": "look",
                        "image_data_url": value,
                    },
                )
                assert response.status_code == 422
                assert "private" not in response.text and value not in response.text

    asyncio.run(check())


def test_model_content_preserves_text_shape_and_adds_one_visual_input():
    body = SimpleNamespace(message="look", image_data_url=None)
    assert model_user_content(body) == "look"
    body.image_data_url = normalized()
    assert model_user_content(body) == [
        {"type": "input_text", "text": "look"},
        {"type": "input_image", "image_url": body.image_data_url, "detail": "auto"},
    ]


@pytest.mark.parametrize("with_photo", [False, True])
def test_main_sdk_receives_validated_image_and_text_without_changing_plain_input(
    monkeypatch, with_photo
):
    from babata.main import chat

    async def check():
        seen = []
        lock = asyncio.Lock()

        async def run(agent, user_input, **kwargs):
            seen.append(user_input)
            return SimpleNamespace(final_output="完成")

        async def get_profile(user):
            return ""

        async def with_tools(agent, *args):
            return agent

        monkeypatch.setattr("babata.main.Runner.run", run)
        monkeypatch.setattr("babata.main.for_request", lambda agent, *args: agent)
        monkeypatch.setattr("babata.main.with_codex_tools", with_tools)
        body = ChatRequest(
            user_id="u",
            session_id="s",
            message="look",
            remember=False,
            image_data_url=normalized() if with_photo else None,
        )
        state = SimpleNamespace(
            steering=object(),
            agent=object(),
            memory=None,
            sessions=SimpleNamespace(lock=lambda key: lock, get=lambda key: None),
            profiles=SimpleNamespace(get=get_profile),
            settings=SimpleNamespace(llm_timeout_seconds=5),
        )
        response = await chat(body, SimpleNamespace(app=SimpleNamespace(state=state)))
        assert response.reply == "完成"
        expected = [{"role": "user", "content": model_user_content(body)}] if with_photo else "look"
        assert seen == [expected]

    asyncio.run(check())
