import asyncio
import json
import logging
import os
import signal
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

import anyio
from agents import RunConfig, Runner
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy.exc import SQLAlchemyError

from babata.agent import create_agent, for_request
from babata.codex import CodexInbox, with_codex_tools
from babata.codex import router as codex_router
from babata.config import Settings
from babata.device_actions import (
    ActionGone,
    DeviceActions,
    DeviceCapabilities,
    DeviceContinuation,
    DeviceErrorCode,
    TakePhotoAction,
)
from babata.memory import SECRET, MemoryWriter
from babata.peer_updates import router as peer_router
from babata.profiles import Profiles
from babata.providers import model_provider, model_settings
from babata.rokid_photos import (
    MAX_IMAGE_DATA_URL,
    NORMALIZED_ERROR,
    PhotoValidationError,
    decode_normalized_jpeg,
    model_user_content,
    validate_normalized_jpeg,
)
from babata.sessions import Sessions, session_key
from babata.shared import SharedArchive
from babata.shared import router as shared_router
from babata.steering import SteeringRuns
from babata.tokyo import TokyoRuntime

logger = logging.getLogger(__name__)
Identifier = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
Message = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=32000)
]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    user_id: Identifier
    session_id: Identifier
    message: Message
    mode: Literal["text", "voice"] = "text"
    interrupted: bool = False
    remember: bool = True
    request_id: UUID = Field(default_factory=uuid4)
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)
    device_result: DeviceContinuation | None = None
    image_data_url: (
        Annotated[str, StringConstraints(strict=True, max_length=MAX_IMAGE_DATA_URL)] | None
    ) = Field(default=None, repr=False)

    @field_validator("image_data_url")
    @classmethod
    def validate_image_data_url(cls, value):
        if value is not None:
            decode_normalized_jpeg(value)
        return value


class ChatResponse(BaseModel):
    user_id: str
    session_id: str
    reply: str
    memory_status: str = "disabled"
    memory_job_id: int | None = None
    action: TakePhotoAction | None = None


class DeviceResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    user_id: Identifier
    session_id: Identifier
    request_id: UUID
    action_id: UUID
    status: Literal["ok", "error"]
    error_code: DeviceErrorCode | None = None
    image_data_url: (
        Annotated[str, StringConstraints(strict=True, max_length=MAX_IMAGE_DATA_URL)] | None
    ) = Field(default=None, repr=False)

    @field_validator("image_data_url")
    @classmethod
    def validate_image_data_url(cls, value):
        if value is not None:
            decode_normalized_jpeg(value)
        return value

    @model_validator(mode="after")
    def validate_result(self):
        if self.status == "ok":
            valid = self.image_data_url is not None and self.error_code is None
        else:
            valid = self.image_data_url is None and self.error_code is not None
        if not valid:
            raise ValueError("Invalid device result")
        return self


def device_actions(state):
    if not hasattr(state, "device_actions"):
        state.device_actions = DeviceActions()
    return state.device_actions


def completed_response(body, state, value, generation):
    if body.device_result and not device_actions(state).current(body, generation):
        raise HTTPException(410, "Device action unavailable")
    if value.get("camera_requested"):
        try:
            action = device_actions(state).issue(body, generation)
        except ActionGone:
            raise HTTPException(410, "Device action unavailable") from None
        return ChatResponse(
            user_id=body.user_id, session_id=body.session_id, reply="", action=action
        )
    if not isinstance(value.get("reply"), str) or not value["reply"].strip():
        raise HTTPException(502, "Model request failed")
    return ChatResponse(
        user_id=body.user_id,
        session_id=body.session_id,
        reply=value["reply"],
        memory_status=value.get("memory_status", "disabled"),
        memory_job_id=value.get("memory_job_id"),
    )


def restart_after_codex_exit():
    # A dead child must not leave a permanently unhealthy Gateway running.
    # Uvicorn shuts down gracefully; Compose's restart policy restores it.
    logger.error("Tokyo Codex exited unexpectedly; restarting Gateway")
    os.kill(os.getpid(), signal.SIGTERM)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    sessions = Sessions(settings.database_url)
    try:
        await sessions.initialize()
        profiles = Profiles(sessions.engine)
        await profiles.initialize()
        shared = SharedArchive(sessions.engine)
        await shared.initialize()
        app.state.shared = shared
        codex = CodexInbox(sessions.engine)
        await codex.initialize()
        app.state.codex = codex
        async with model_provider(settings) as model:
            app.state.agent = create_agent(model, model_settings(settings))
            app.state.sessions = sessions
            app.state.settings = settings
            app.state.profiles = profiles
            memory = None
            if settings.auto_memory_enabled:
                memory = MemoryWriter(
                    profiles, model, model_settings(settings), settings.memory_timeout_seconds
                )
                memory.start()
            app.state.memory = memory
            if settings.agent_runtime == "codex":
                app.state.steering = TokyoRuntime(
                    app.state, queue_memory, on_fatal=restart_after_codex_exit
                )
                await app.state.steering.start()
            else:
                app.state.steering = SteeringRuns(app.state, queue_memory)
            try:
                yield
            finally:
                await app.state.steering.close()
                if memory:
                    await memory.close()
    finally:
        await sessions.close()


app = FastAPI(title="Babata Gateway", version="0.1.0", lifespan=lifespan)
app.include_router(codex_router)
app.include_router(shared_router)
app.include_router(peer_router)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    # FastAPI's default handler echoes rejected values, including private images.
    if request.url.path in {"/chat", "/chat/stream", "/chat/device-result"}:
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})
    return await request_validation_exception_handler(request, error)


async def check_photo(body: ChatRequest):
    if body.image_data_url is not None:
        try:
            await anyio.to_thread.run_sync(validate_normalized_jpeg, body.image_data_url)
        except PhotoValidationError:
            raise HTTPException(422, NORMALIZED_ERROR) from None


@app.get("/health")
async def health(request: Request) -> dict[str, str]:
    runtime = request.app.state.steering
    if isinstance(runtime, TokyoRuntime) and runtime.rpc.process.returncode is not None:
        raise HTTPException(status_code=503, detail="Tokyo Codex unavailable")
    try:
        await request.app.state.sessions.ping()
    except (SQLAlchemyError, OSError):
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    return {"status": "ok"}


@app.get("/runtime")
async def runtime_info(request: Request):
    return {
        "engine": request.app.state.settings.agent_runtime,
        "llm_provider": request.app.state.settings.llm_provider,
        "model": request.app.state.settings.llm_model,
        "context_window_tokens": request.app.state.settings.tokyo_model_context_window,
        "auto_compact_token_limit": request.app.state.settings.tokyo_auto_compact_token_limit,
        "shared_memory": "postgresql",
        "native_memory": {
            "enabled": request.app.state.settings.agent_runtime == "codex"
            and request.app.state.settings.tokyo_native_memories,
            "extract_model": request.app.state.settings.tokyo_memory_extract_model,
            "consolidation_model": request.app.state.settings.tokyo_memory_consolidation_model,
        },
    }


@app.get("/memory")
async def read_memory(user_id: Identifier, request: Request):
    return {
        **await request.app.state.profiles.snapshot(user_id),
        "automatic": request.app.state.settings.auto_memory_enabled,
        "jobs": await request.app.state.profiles.job_counts(user_id),
    }


@app.get("/memory/history")
async def memory_history(user_id: Identifier, request: Request):
    return await request.app.state.profiles.history(user_id)


class RestoreMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: Identifier
    revision: int = Field(ge=0)


@app.post("/memory/restore")
async def restore_memory(body: RestoreMemory, request: Request):
    try:
        revision = await request.app.state.profiles.restore(body.user_id, body.revision)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown memory revision") from None
    return {"revision": revision}


async def queue_memory(body, state, key, started_at=None):
    if not body.remember or getattr(state, "memory", None) is None:
        return {"memory_status": "disabled", "memory_job_id": None}
    if SECRET.search(body.message):
        return {"memory_status": "skipped", "memory_job_id": None}
    try:
        async with asyncio.timeout(3):
            job_id = await state.memory.enqueue(body.user_id, key, body.message, started_at)
        return {"memory_status": "pending", "memory_job_id": job_id}
    except Exception as error:
        logger.warning("Memory enqueue failed: %s", type(error).__name__)
        return {"memory_status": "failed", "memory_job_id": None}


@app.post("/chat", response_model=ChatResponse, response_model_exclude_none=True)
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    # Any new user input invalidates an earlier photo request, including old clients.
    generation = device_actions(request.app.state).begin(body)
    await check_photo(body)
    return await run_chat(body, request, generation)


async def run_chat(body: ChatRequest, request: Request, generation) -> ChatResponse:
    if body.device_result and not device_actions(request.app.state).current(body, generation):
        raise HTTPException(410, "Device action unavailable")
    if isinstance(request.app.state.steering, TokyoRuntime):
        async for event, value in request.app.state.steering.stream(body):
            if event == "done":
                return completed_response(body, request.app.state, value, generation)
            if event == "error":
                if body.device_result and not device_actions(request.app.state).current(
                    body, generation
                ):
                    raise HTTPException(410, "Device action unavailable")
                raise HTTPException(502, value["detail"])
        raise HTTPException(409, "Another message continued this task; read its stream")
    started_at = datetime.now(UTC)
    sessions: Sessions = request.app.state.sessions
    key = session_key(body.user_id, body.session_id)
    try:
        async with sessions.lock(key):
            async with asyncio.timeout(request.app.state.settings.llm_timeout_seconds + 5):
                result = await Runner.run(
                    await with_codex_tools(
                        for_request(
                            request.app.state.agent,
                            await request.app.state.profiles.get(body.user_id),
                            body.mode,
                            body.interrupted,
                        ),
                        request.app.state,
                        body,
                    ),
                    body.message
                    if body.image_data_url is None
                    else [{"role": "user", "content": model_user_content(body)}],
                    session=sessions.get(key),
                    run_config=RunConfig(tracing_disabled=True),
                    max_turns=5,
                )
            memory = await queue_memory(body, request.app.state, key, started_at)
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Model request timed out") from None
    except (SQLAlchemyError, OSError):
        raise HTTPException(status_code=503, detail="Database unavailable") from None
    except Exception as error:
        # Provider exception text can contain request content. Log only its type.
        logger.warning("Chat failed: %s", type(error).__name__)
        raise HTTPException(status_code=502, detail="Model request failed") from None

    return ChatResponse(
        user_id=body.user_id, session_id=body.session_id, reply=result.final_output, **memory
    )


@app.post("/chat/device-result", response_model=ChatResponse, response_model_exclude_none=True)
async def receive_device_result(body: DeviceResultRequest, request: Request) -> ChatResponse:
    await check_photo(body)
    actions = device_actions(request.app.state)
    try:
        original, generation = actions.consume(
            body.user_id, body.session_id, body.request_id, body.action_id
        )
    except ActionGone:
        raise HTTPException(410, "Device action unavailable") from None
    continuation = original.model_copy(
        update={
            "request_id": uuid4(),
            "capabilities": DeviceCapabilities(),
            "device_result": DeviceContinuation(
                original_request_id=original.request_id,
                generation=generation,
                status=body.status,
                error_code=body.error_code,
            ),
            "image_data_url": body.image_data_url,
        }
    )
    return await run_chat(continuation, request, generation)


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream_chat(body: ChatRequest, state, generation=None):
    if generation is None:
        generation = device_actions(state).begin(body)
    if not hasattr(state, "steering"):
        state.steering = SteeringRuns(state, queue_memory)
    async for event, payload in state.steering.stream(body):
        if event == "done":
            try:
                response = completed_response(body, state, payload, generation)
            except HTTPException:
                yield sse("error", {"detail": "Device action unavailable"})
                return
            payload = {**payload, "reply": response.reply}
            payload.pop("camera_requested", None)
            if response.action:
                payload["action"] = response.action.model_dump(mode="json")
        yield sse(event, payload)


class CancelChat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: Identifier
    session_id: Identifier
    task_id: UUID


@app.post("/chat/cancel")
async def cancel_chat(body: CancelChat, request: Request):
    stopped = await request.app.state.steering.cancel(body.user_id, body.session_id, body.task_id)
    return {"stopped": stopped}


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest, request: Request):
    generation = device_actions(request.app.state).begin(body)
    await check_photo(body)
    return StreamingResponse(
        stream_chat(body, request.app.state, generation),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
