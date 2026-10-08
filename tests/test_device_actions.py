import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from PIL import Image
from pydantic import ValidationError

from babata.device_actions import ActionGone, CameraDecision, DeviceActions, TakePhotoAction
from babata.main import (
    ChatRequest,
    ChatResponse,
    DeviceResultRequest,
    chat,
    chat_stream,
    receive_device_result,
    validation_error,
)
from babata.rokid_gateway import GatewaySettings, create_app
from babata.rokid_photos import validate_normalized_jpeg
from babata.tokyo import TokyoRuntime

TOKEN = "synthetic-device-token-" + "x" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}


def image():
    output = BytesIO()
    with Image.new("RGB", (16, 12), "blue") as source:
        source.save(output, format="JPEG")
    return {"mime_type": "image/jpeg", "data_base64": base64.b64encode(output.getvalue()).decode()}


def user_body(**extra):
    return {
        "message": "看看这个是什么",
        "session_id": "voice-memory",
        "request_id": str(uuid4()),
        **extra,
    }


def callback(original, action, **extra):
    return {
        "session_id": original["session_id"],
        "request_id": original["request_id"],
        "action_id": action["id"],
        "status": "error",
        "error_code": "permission",
        **extra,
    }


class NativeStub(TokyoRuntime):
    def __init__(self, override=None):
        self.seen = []
        self.override = override

    async def stream(self, body):
        self.seen.append(body)
        if self.override:
            value = await self.override(body)
        elif body.capabilities.camera:
            value = {"reply": "", "camera_requested": True}
        else:
            value = {"reply": "已收到照片" if body.image_data_url else "这次没有取得照片"}
        yield "done", value


@asynccontextmanager
async def clients(native=None, actions=None):
    native = native or NativeStub()
    private = FastAPI()
    private.state.steering = native
    private.state.device_actions = actions or DeviceActions()
    private.add_exception_handler(RequestValidationError, validation_error)
    private.add_api_route(
        "/chat",
        chat,
        methods=["POST"],
        response_model=ChatResponse,
        response_model_exclude_none=True,
    )
    private.add_api_route(
        "/chat/device-result",
        receive_device_result,
        methods=["POST"],
        response_model=ChatResponse,
        response_model_exclude_none=True,
    )
    private.add_api_route("/chat/stream", chat_stream, methods=["POST"])
    gateway = create_app(
        GatewaySettings(_env_file=None, enabled=True, token=TOKEN),
        transport=httpx.ASGITransport(app=private),
    )
    application = gateway.app.app
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="https://gateway.test"
        ) as public:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=private), base_url="http://private.test"
            ) as inner:
                yield public, inner, native, private.state.device_actions


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "reply", "reply": ""},
        {"kind": "take_photo", "reply": "already taken"},
        {"kind": "take_photo", "reply": "", "id": str(uuid4())},
        {"kind": "other", "reply": "hi"},
        {"kind": "reply", "reply": 42},
    ],
)
def test_model_decision_is_strict_and_cannot_create_action_ids(value):
    with pytest.raises(ValidationError):
        CameraDecision.model_validate(value)


def test_action_wire_requires_all_server_generated_fields():
    for missing in ("type", "id", "expires_in_ms"):
        value = {"type": "take_photo", "id": str(uuid4()), "expires_in_ms": 60000}
        value.pop(missing)
        with pytest.raises(ValidationError):
            TakePhotoAction.model_validate(value)


def test_broker_binds_nonce_to_owner_session_request_expiry_and_one_use():
    now = [100.0]
    actions = DeviceActions(clock=lambda: now[0])
    body = ChatRequest(user_id="u", session_id="s", message="look", capabilities={"camera": True})
    generation = actions.begin(body)
    action = actions.issue(body, generation)
    for args in (
        ("other", "s", body.request_id, action.id),
        ("u", "other", body.request_id, action.id),
        ("u", "s", uuid4(), action.id),
        ("u", "s", body.request_id, uuid4()),
    ):
        with pytest.raises(ActionGone):
            actions.consume(*args)
    stored, original_generation = actions.consume("u", "s", body.request_id, action.id)
    assert stored == body and original_generation == generation
    with pytest.raises(ActionGone):
        actions.consume("u", "s", body.request_id, action.id)
    with pytest.raises(ActionGone):
        actions.issue(body, generation)
    newer = body.model_copy(update={"request_id": uuid4()})
    action = actions.issue(newer, actions.begin(newer))
    now[0] += 60
    with pytest.raises(ActionGone):
        actions.consume("u", "s", newer.request_id, action.id)


def test_broker_has_bounded_session_and_issued_record_counts():
    actions = DeviceActions(max_sessions=2, max_issued=1)
    body = ChatRequest(user_id="u", session_id="s", message="look", capabilities={"camera": True})
    actions.issue(body, actions.begin(body))
    newer = body.model_copy(update={"request_id": uuid4()})
    with pytest.raises(ActionGone):
        actions.issue(newer, actions.begin(newer))
    for session in ("s2", "s3"):
        actions.begin(body.model_copy(update={"session_id": session}))
    assert len(actions.sessions) == 2 and ("u", "s") not in actions.sessions


def test_request_tombstones_expire_and_capacity_recovers_without_reissuing_current_request():
    now = [100.0]
    actions = DeviceActions(clock=lambda: now[0], max_issued=1)
    first = ChatRequest(user_id="u", session_id="s", message="look", capabilities={"camera": True})
    actions.issue(first, actions.begin(first))
    second = first.model_copy(update={"request_id": uuid4()})
    generation = actions.begin(second)
    with pytest.raises(ActionGone):
        actions.issue(second, generation)
    now[0] += 601
    generation = actions.begin(second)
    action = actions.issue(second, generation)
    assert action.type == "take_photo" and len(actions.sessions[("u", "s")].issued) == 1
    now[0] += 601
    generation = actions.begin(second)
    assert not actions.sessions[("u", "s")].issued
    with pytest.raises(ActionGone):
        actions.issue(second, generation)


def test_legacy_text_and_photo_still_return_only_normal_reply():
    async def check():
        async with clients() as (public, _, native, _):
            text = user_body()
            response = await public.post("/v1/chat", headers=AUTH, json=text)
            assert response.status_code == 200 and set(response.json()) == {
                "session_id",
                "request_id",
                "reply",
            }
            assert native.seen[-1].capabilities.camera is False
            response = await public.post("/v1/photo", headers=AUTH, json=user_body(image=image()))
            assert response.status_code == 200 and "action" not in response.json()
            validate_normalized_jpeg(native.seen[-1].image_data_url)

    asyncio.run(check())


@pytest.mark.parametrize("status", ["ok", "error", "decode_failure"])
def test_complete_action_handshake_continues_same_session_with_no_second_camera(status, caplog):
    async def check():
        async with clients() as (public, _, native, _):
            original = user_body(capabilities={"camera": True}, remember=True)
            response = await public.post("/v1/chat", headers=AUTH, json=original)
            assert response.status_code == 200
            action = response.json()["action"]
            assert action["type"] == "take_photo" and action["expires_in_ms"] == 60000
            assert response.json()["reply"] == ""
            payload = callback(original, action)
            if status != "error":
                payload.pop("error_code")
                payload.update(status="ok", image=image())
                if status == "decode_failure":
                    payload["image"]["data_base64"] = "cHJpdmF0ZS1ub3QtYW4taW1hZ2U="
            result = await public.post("/v1/device-result", headers=AUTH, json=payload)
            assert result.status_code == 200 and "action" not in result.json()
            assert result.json()["request_id"] == original["request_id"]
            assert len(native.seen) == 2
            before, after = native.seen
            assert after.user_id == before.user_id and after.session_id == before.session_id
            assert after.request_id != before.request_id
            assert after.device_result.original_request_id == before.request_id
            assert after.capabilities.camera is False and after.remember is True
            assert after.message == before.message
            if status == "ok":
                assert after.device_result.status == "ok"
                validate_normalized_jpeg(after.image_data_url)
            else:
                assert after.device_result.status == "error" and after.image_data_url is None
            repeat = await public.post("/v1/device-result", headers=AUTH, json=payload)
            assert repeat.status_code == 410 and len(native.seen) == 2
            for secret in (original["message"], action["id"], TOKEN, image()["data_base64"]):
                assert secret not in caplog.text
            assert any(
                r.args[:3] == ("/v1/device-result", 200, "POST")
                for r in caplog.records
                if r.name == "uvicorn.error.rokid"
            )

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())


@pytest.mark.parametrize(
    "invalidation", ["expired", "new_input", "wrong_session", "wrong_request", "unknown"]
)
def test_invalid_device_results_return_410_without_model_continuation(invalidation):
    async def check():
        now = [100.0]
        async with clients(actions=DeviceActions(clock=lambda: now[0])) as (public, _, native, _):
            original = user_body(capabilities={"camera": True})
            action = (await public.post("/v1/chat", headers=AUTH, json=original)).json()["action"]
            payload = callback(original, action)
            if invalidation == "expired":
                now[0] += 60
            elif invalidation == "new_input":
                assert (
                    await public.post("/v1/chat", headers=AUTH, json=user_body(message="不用看了"))
                ).status_code == 200
            elif invalidation == "wrong_session":
                payload["session_id"] = "elsewhere"
            elif invalidation == "wrong_request":
                payload["request_id"] = str(uuid4())
            else:
                payload["action_id"] = str(uuid4())
            before = len(native.seen)
            response = await public.post("/v1/device-result", headers=AUTH, json=payload)
            assert response.status_code == 410 and len(native.seen) == before

    asyncio.run(check())


def test_no_capability_cannot_receive_upstream_action_and_results_cannot_request_second_photo():
    async def check():
        async def always_camera(body):
            return {"reply": "", "camera_requested": True}

        async with clients(native=NativeStub(always_camera)) as (public, _, _, _):
            assert (
                await public.post("/v1/chat", headers=AUTH, json=user_body())
            ).status_code == 502
            original = user_body(capabilities={"camera": True})
            action = (await public.post("/v1/chat", headers=AUTH, json=original)).json()["action"]
            result = await public.post(
                "/v1/device-result", headers=AUTH, json=callback(original, action)
            )
            assert result.status_code == 410 and "action" not in result.json()

    asyncio.run(check())


def test_new_user_input_during_result_validation_invalidates_it_before_model(monkeypatch):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_check(body):
            if isinstance(body, DeviceResultRequest):
                entered.set()
                await release.wait()

        monkeypatch.setattr("babata.main.check_photo", delayed_check)
        async with clients() as (public, inner, native, _):
            original = user_body(capabilities={"camera": True})
            action = (await public.post("/v1/chat", headers=AUTH, json=original)).json()["action"]
            mapped = native.seen[0]
            waiting = asyncio.create_task(
                inner.post(
                    "/chat/device-result",
                    json={
                        **callback(original, action),
                        "user_id": mapped.user_id,
                        "session_id": mapped.session_id,
                    },
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            assert (
                await public.post("/v1/chat", headers=AUTH, json=user_body(message="不用拍了"))
            ).status_code == 200
            release.set()
            assert (await waiting).status_code == 410
            assert len(native.seen) == 2 and all(body.device_result is None for body in native.seen)

    asyncio.run(check())


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok"},
        {"status": "error"},
        {"status": "ok", "image": image(), "error_code": "capture"},
        {"status": "error", "image": image(), "error_code": "capture"},
        {"status": "error", "error_code": "private-raw-error-marker"},
    ],
)
def test_flat_result_validation_never_echoes_private_fields(payload):
    async def check():
        async with clients() as (public, inner, native, _):
            body = {
                "session_id": "s",
                "request_id": str(uuid4()),
                "action_id": str(uuid4()),
                **payload,
            }
            response = await public.post("/v1/device-result", headers=AUTH, json=body)
            assert response.status_code == 422 and "private-raw" not in response.text
            assert image()["data_base64"] not in response.text
            body["user_id"] = "u"
            body["image_data_url"] = "private-image-marker"
            response = await inner.post("/chat/device-result", json=body)
            assert response.status_code == 422 and "private-image-marker" not in response.text
            assert not native.seen

    asyncio.run(check())


def test_device_result_auth_cors_and_camera_capability_types():
    async def check():
        async with clients() as (public, _, native, _):
            assert (await public.post("/v1/device-result", json={})).status_code == 401
            response = await public.options(
                "/v1/device-result",
                headers={
                    "Origin": "https://aiui.rokid.com",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            assert response.status_code == 200
            for caps in ({"camera": "true"}, {"camera": 1}, {"camera": True, "shell": True}):
                assert (
                    await public.post("/v1/chat", headers=AUTH, json=user_body(capabilities=caps))
                ).status_code == 422
            assert not native.seen

    asyncio.run(check())
