"""Optional Rokid text/photo facade; run separately from the private Babata app.

The process is disabled unless ROKID_GATEWAY_ENABLED=true. Its independent
ROKID_GATEWAY_TOKEN authenticates callers; the private upstream never receives
that token. Deployment controls the listening address and TLS termination.
"""

import hashlib
import json
import logging
import secrets
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.datastructures import Headers

from babata.device_actions import DeviceCapabilities, DeviceErrorCode, TakePhotoAction
from babata.rokid_photo_store import PhotoArchive, decode_data_url
from babata.rokid_photos import (
    MAX_PHOTO_BODY_BYTES,
    MAX_SOURCE_BASE64,
    PHOTO_ERROR,
    PhotoValidationError,
    normalize_photo,
)

logger = logging.getLogger(__name__)
request_logger = logging.getLogger("uvicorn.error.rokid")
MAX_BODY_BYTES = 128 * 1024
MAX_EVENT_BYTES = 512 * 1024
UPSTREAM_ERROR = "Babata is temporarily unavailable."
NATIVE_EVENTS = {"task", "steering", "superseded", "delta", "done", "error"}
PREVIEW_ORIGINS = ("https://aiui.rokid.com", "https://aiui-global.rokid.com")


class UnmatchedGetShapeLog:
    """Observe GET routing shape without retaining targets, headers or bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope.get("method") != "GET"
            or path in {"/v1/chat", "/v1/chat/stream"}
        ):
            return await self.app(scope, receive, send)
        absolute = path.startswith(("http://", "https://"))
        raw_path = scope.get("raw_path", b"")
        raw_absolute = isinstance(raw_path, bytes) and raw_path.startswith(
            (b"http://", b"https://")
        )
        chat_path = authority_present = False
        query_present = bool(scope.get("query_string"))
        try:
            parts = urlsplit(path)
            chat_path = parts.path == "/v1/chat"
            authority_present = bool(parts.netloc)
            query_present = query_present or bool(parts.query)
        except ValueError:
            pass
        status = "not_started"

        async def send_status(message):
            nonlocal status
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]

        try:
            return await self.app(scope, receive, send_status)
        finally:
            request_logger.info(
                "rokid_get_shape status=%s absolute=%d chat_path=%d "
                "authority_present=%d raw_absolute=%d query_present=%d",
                status,
                absolute,
                chat_path,
                authority_present,
                raw_absolute,
                query_present,
            )


class RouteStatusLog:
    """Log fixed chat routes, methods, status and an origin bucket only."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        route = {
            path: path for path in ("/v1/chat", "/v1/chat/stream", "/v1/photo", "/v1/device-result")
        }.get(scope.get("path"))
        method = {"POST": "POST", "OPTIONS": "OPTIONS", "GET": "GET"}.get(scope.get("method"))
        if scope["type"] == "http" and method == "GET" and route is None:
            return await UnmatchedGetShapeLog(self.app)(scope, receive, send)
        if scope["type"] != "http" or method is None or route is None:
            return await self.app(scope, receive, send)
        # Match CORS's effective header value without retaining it in the log.
        origin = Headers(raw=scope.get("headers", [])).get("origin")
        origin_bucket = (
            "missing"
            if origin is None
            else "expected"
            if origin in PREVIEW_ORIGINS
            else "other"
        )
        status = "not_started"

        async def send_status(message):
            nonlocal status
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]

        try:
            await self.app(scope, receive, send_status)
        finally:
            request_logger.info(
                "rokid_http route=%s status=%s method=%s origin_bucket=%s",
                route,
                status,
                method,
                origin_bucket,
            )


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ROKID_GATEWAY_", extra="ignore", hide_input_in_errors=True
    )

    enabled: bool = False
    token: SecretStr = SecretStr("")
    user_id: str = Field(default="user", min_length=1, max_length=128)
    upstream_url: str = "http://127.0.0.1:8000"
    timeout_seconds: float = Field(default=90, gt=0, le=300)
    photo_store_dir: str = ""

    @model_validator(mode="after")
    def validate_gateway(self):
        token = self.token.get_secret_value()
        if self.enabled and (
            not 32 <= len(token) <= 512 or not token.isascii() or any(c.isspace() for c in token)
        ):
            raise ValueError(
                "ROKID_GATEWAY_TOKEN must contain 32-512 non-whitespace ASCII characters"
            )
        try:
            url = httpx.URL(self.upstream_url)
        except httpx.InvalidURL:
            raise ValueError("ROKID_GATEWAY_UPSTREAM_URL must be an HTTP origin") from None
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("ROKID_GATEWAY_UPSTREAM_URL must be an HTTP origin")
        self.upstream_url = str(url).rstrip("/")
        if self.photo_store_dir and not Path(self.photo_store_dir).is_absolute():
            raise ValueError("ROKID_GATEWAY_PHOTO_STORE_DIR must be an absolute path")
        return self


class TextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    message: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=32000)
    ]
    session_id: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=128)
    ]
    request_id: UUID = Field(default_factory=uuid4)
    remember: StrictBool = False
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)


class PhotoInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    data_base64: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=MAX_SOURCE_BASE64)
    ] = Field(repr=False)
    mime_type: Literal["image/jpeg", "image/png"]


class PhotoRequest(TextRequest):
    image: PhotoInput


class DeviceResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    session_id: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=128)
    ]
    request_id: UUID
    action_id: UUID
    status: Literal["ok", "error"]
    image: PhotoInput | None = Field(default=None, repr=False)
    error_code: DeviceErrorCode | None = None

    @model_validator(mode="after")
    def validate_result(self):
        if self.status == "ok":
            valid = self.image is not None and self.error_code is None
        else:
            valid = self.image is None and self.error_code is not None
        if not valid:
            raise ValueError("Invalid device result")
        return self


def session_key(session_id: str) -> str:
    """Fixed upstream session name; the raw device value never leaves here."""
    return "rokid-" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def upstream_body(body: TextRequest, settings: GatewaySettings) -> dict:
    result = {
        "user_id": settings.user_id,
        "session_id": session_key(body.session_id),
        "request_id": str(body.request_id),
        "message": body.message,
        "mode": "voice",
        "remember": body.remember,
    }
    if body.capabilities.camera and not isinstance(body, PhotoRequest):
        result["capabilities"] = {"camera": True}
    return result


async def read_input(request: Request, *, photo: bool = False, result: bool = False):
    settings = request.app.state.settings
    if not settings.enabled:
        raise HTTPException(404, "Not found")
    authorization = request.headers.get("Authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not secrets.compare_digest(
            supplied.encode("utf-8"), settings.token.get_secret_value().encode("utf-8")
        )
    ):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
    if (
        request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415, "A JSON text request is required")
    limit = MAX_PHOTO_BODY_BYTES if photo or result else MAX_BODY_BYTES
    length = request.headers.get("Content-Length")
    if length is not None:
        try:
            size = int(length)
        except ValueError:
            raise HTTPException(400, "Invalid request length") from None
        if size < 0:
            raise HTTPException(400, "Invalid request length")
        if size > limit:
            raise HTTPException(413, "Request is too large")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > limit:
            raise HTTPException(413, "Request is too large")
        content.extend(chunk)
    try:
        schema = DeviceResultInput if result else PhotoRequest if photo else TextRequest
        return schema.model_validate_json(content)
    except ValidationError:
        if result:
            raise HTTPException(422, "Invalid device result") from None
        if photo:
            raise HTTPException(422, PHOTO_ERROR) from None
        raise HTTPException(
            422,
            "Only a text message, session_id, request_id, remember and capabilities are supported; "
            "images, audio and other fields are not supported.",
        ) from None


def encode_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def bounded_lines(response: httpx.Response):
    # Read network chunks immediately; a fixed chunk_size would delay short speech deltas.
    pending = bytearray()
    async for chunk in response.aiter_bytes():
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            if end < 0:
                end = len(chunk)
            if len(pending) + end - start > MAX_EVENT_BYTES:
                raise ValueError("Oversized upstream line")
            pending.extend(chunk[start:end])
            if end == len(chunk):
                break
            yield bytes(pending).removesuffix(b"\r").decode("utf-8")
            pending.clear()
            start = end + 1
    if pending:
        yield bytes(pending).removesuffix(b"\r").decode("utf-8")


async def relay_events(response: httpx.Response):
    """Preserve native events, replacing errors without cancelling the Tokyo turn."""
    event, data, size = "", [], 0
    try:
        async for line in bounded_lines(response):
            size += len(line.encode("utf-8")) + 1
            if size > MAX_EVENT_BYTES:
                raise ValueError("Oversized upstream event")
            if not line:
                if data:
                    if event not in NATIVE_EVENTS:
                        raise ValueError("Unexpected upstream event")
                    if event == "error":
                        yield encode_event("error", {"detail": UPSTREAM_ERROR})
                        return
                    payload = json.loads("\n".join(data))
                    if not isinstance(payload, dict):
                        raise ValueError("Invalid upstream event")
                    yield encode_event(event, payload)
                    if event in {"done", "superseded"}:
                        return
                event, data, size = "", [], 0
            elif line.startswith(":"):
                # Comments may contain upstream diagnostics; relay only a fixed heartbeat.
                yield ": keep-alive\n\n"
            else:
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    event = value
                elif field == "data":
                    data.append(value)
        # EOF without a complete terminal event is not a successful answer.
        yield encode_event("error", {"detail": UPSTREAM_ERROR})
    except (httpx.HTTPError, ValueError, UnicodeError) as error:
        logger.warning("Rokid upstream stream failed: %s", type(error).__name__)
        yield encode_event("error", {"detail": UPSTREAM_ERROR})
    finally:
        with anyio.CancelScope(shield=True):
            await response.aclose()


class UpstreamStreamingResponse(StreamingResponse):
    """Close the subscription even if the downstream fails before iteration starts."""

    def __init__(self, upstream: httpx.Response):
        self.upstream = upstream
        super().__init__(
            relay_events(upstream),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


def archive_of(request: Request) -> PhotoArchive | None:
    return getattr(request.app.state, "photo_archive", None)


async def store_photo(
    request: Request, image_data_url: str, *, session: str, kind: str, source_mime: str | None
) -> dict | None:
    """Archive one accepted photo; a storage problem never fails the request."""
    archive = archive_of(request)
    if archive is None:
        return None
    try:
        jpeg = decode_data_url(image_data_url)
    except ValueError:
        return None
    settings = request.app.state.settings
    return await anyio.to_thread.run_sync(
        partial(
            archive.save,
            jpeg,
            session=session,
            user_id=settings.user_id,
            kind=kind,
            source_mime=source_mime,
        )
    )


async def describe_photo(request: Request, record: dict | None, output: dict) -> None:
    """Attach the spoken answer to its archived photo, without changing the reply."""
    archive = archive_of(request)
    if archive is None or record is None:
        return
    await anyio.to_thread.run_sync(archive.describe, record, output.get("reply"))


def create_app(
    settings: GatewaySettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RouteStatusLog:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings if settings is not None else GatewaySettings()
        # Reserve before reading the large body. Never queue unbounded decoded photos.
        app.state.photo_slot = anyio.Semaphore(1)
        config = app.state.settings
        app.state.photo_archive = PhotoArchive(config.photo_store_dir)
        async with httpx.AsyncClient(
            base_url=config.upstream_url,
            timeout=httpx.Timeout(config.timeout_seconds, connect=5, write=10, pool=5),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            app.state.upstream = client
            yield

    application = FastAPI(
        title="Rokid gateway",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )

    async def send_chat(
        body, request: Request, image_data_url: str | None = None, *, device_payload=None
    ):
        payload = (
            device_payload
            if device_payload is not None
            else upstream_body(body, request.app.state.settings)
        )
        path = "/chat/device-result" if device_payload is not None else "/chat"
        if image_data_url is not None:
            payload["image_data_url"] = image_data_url
        allow_action = (
            isinstance(body, TextRequest)
            and not isinstance(body, PhotoRequest)
            and body.capabilities.camera
            and image_data_url is None
        )
        try:
            response = await request.app.state.upstream.post(path, json=payload)
            if device_payload is not None and response.status_code == 410:
                raise HTTPException(410, "Device action unavailable")
            if not 200 <= response.status_code < 300:
                raise HTTPException(502, UPSTREAM_ERROR)
            result = response.json()
            if not isinstance(result, dict) or not isinstance(result.get("reply"), str):
                raise ValueError("Invalid upstream response")
            output = {
                "session_id": body.session_id,
                "request_id": str(body.request_id),
                "reply": result["reply"],
            }
            if result.get("action") is not None:
                if not allow_action or result["reply"] != "":
                    raise ValueError("Unexpected device action")
                output["action"] = TakePhotoAction.model_validate(result["action"]).model_dump(
                    mode="json"
                )
            elif not result["reply"].strip():
                raise ValueError("Empty upstream response")
            return output
        except httpx.TimeoutException:
            raise HTTPException(504, UPSTREAM_ERROR) from None
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, UPSTREAM_ERROR) from None

    @application.post("/v1/chat")
    async def chat(request: Request):
        body = await read_input(request)
        return await send_chat(body, request)

    @application.post("/v1/photo")
    async def photo(request: Request):
        slot = request.app.state.photo_slot
        try:
            slot.acquire_nowait()
        except anyio.WouldBlock:
            raise HTTPException(
                429, "A photo is already being processed", headers={"Retry-After": "5"}
            ) from None
        try:
            body = await read_input(request, photo=True)
            try:
                image_data_url = await anyio.to_thread.run_sync(
                    normalize_photo, body.image.data_base64, body.image.mime_type
                )
            except PhotoValidationError:
                raise HTTPException(422, PHOTO_ERROR) from None
            record = await store_photo(
                request,
                image_data_url,
                session=session_key(body.session_id),
                kind="photo",
                source_mime=body.image.mime_type,
            )
            output = await send_chat(body, request, image_data_url)
            await describe_photo(request, record, output)
            return output
        finally:
            slot.release()

    @application.post("/v1/device-result")
    async def device_result(request: Request):
        slot = request.app.state.photo_slot
        try:
            slot.acquire_nowait()
        except anyio.WouldBlock:
            raise HTTPException(
                429, "A photo is already being processed", headers={"Retry-After": "5"}
            ) from None
        try:
            body = await read_input(request, result=True)
            payload = {
                "user_id": request.app.state.settings.user_id,
                "session_id": session_key(body.session_id),
                "request_id": str(body.request_id),
                "action_id": str(body.action_id),
                "status": body.status,
            }
            record = None
            if body.status == "ok":
                try:
                    payload["image_data_url"] = await anyio.to_thread.run_sync(
                        normalize_photo, body.image.data_base64, body.image.mime_type
                    )
                except PhotoValidationError:
                    # A decoding failure is a device failure, never a successful photo.
                    payload.update(status="error", error_code="invalid")
                else:
                    record = await store_photo(
                        request,
                        payload["image_data_url"],
                        session=payload["session_id"],
                        kind="device-result",
                        source_mime=body.image.mime_type,
                    )
                body.image = None  # Do not retain the large original while the model replies.
            else:
                payload["error_code"] = body.error_code
            output = await send_chat(body, request, device_payload=payload)
            await describe_photo(request, record, output)
            return output
        finally:
            slot.release()

    @application.post("/v1/chat/stream")
    async def chat_stream(request: Request):
        body = await read_input(request)
        client = request.app.state.upstream
        try:
            response = await client.send(
                client.build_request(
                    "POST",
                    "/chat/stream",
                    json=upstream_body(body, request.app.state.settings),
                    headers={"Accept": "text/event-stream"},
                ),
                stream=True,
            )
        except httpx.TimeoutException:
            raise HTTPException(504, UPSTREAM_ERROR) from None
        except httpx.HTTPError:
            raise HTTPException(502, UPSTREAM_ERROR) from None
        if (
            not 200 <= response.status_code < 300
            or response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            != "text/event-stream"
        ):
            await response.aclose()
            raise HTTPException(502, UPSTREAM_ERROR)
        return UpstreamStreamingResponse(response)

    # Wrap the error handler too, so the preview can read even an unexpected 500.
    cors = CORSMiddleware(
        application,
        allow_origins=list(PREVIEW_ORIGINS),
        allow_methods=["POST"],
        allow_headers=["Content-Type", "Authorization"],
        allow_credentials=False,
    )
    return RouteStatusLog(cors)


app = create_app()
