import asyncio
import json
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from babata.rokid_gateway import (
    MAX_BODY_BYTES,
    MAX_EVENT_BYTES,
    UPSTREAM_ERROR,
    GatewaySettings,
    RouteStatusLog,
    TextRequest,
    UpstreamStreamingResponse,
    create_app,
    relay_events,
    upstream_body,
)

TOKEN = "synthetic-rokid-token-" + "x" * 32
AUTH = {"Authorization": "Bearer " + TOKEN}
PREVIEW_ORIGIN = "https://aiui.rokid.com"
GLOBAL_PREVIEW_ORIGIN = "https://aiui-global.rokid.com"


def settings(**overrides):
    return GatewaySettings(_env_file=None, enabled=True, token=TOKEN, **overrides)


@asynccontextmanager
async def client_for(handler, config=None, *, raise_app_exceptions=True):
    app = create_app(config or settings(), transport=httpx.MockTransport(handler))
    application = app.app.app
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
            base_url="http://gateway.test",
        ) as client:
            yield client


@pytest.mark.parametrize("path", ["/v1/chat", "/v1/chat/stream"])
@pytest.mark.parametrize("origin", [PREVIEW_ORIGIN, GLOBAL_PREVIEW_ORIGIN])
def test_preview_preflight_needs_no_token_and_never_calls_upstream(path, origin, caplog):
    async def check():
        def handler(request):
            raise AssertionError("Preflight and unauthenticated requests must not reach upstream")

        async with client_for(handler) as client:
            response = await client.options(
                path,
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type, authorization",
                },
            )
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == origin
            assert response.headers["access-control-allow-methods"] == "POST"
            allowed_headers = {
                h.strip().lower()
                for h in response.headers["access-control-allow-headers"].split(",")
            }
            assert {"content-type", "authorization"} <= allowed_headers
            assert "*" not in allowed_headers
            assert "access-control-allow-credentials" not in response.headers
            vary = {value.strip().lower() for value in response.headers["vary"].split(",")}
            assert "origin" in vary
            assert "*" not in vary
            records = [r for r in caplog.records if r.name == "uvicorn.error.rokid"]
            assert [r.args for r in records] == [(path, 200, "OPTIONS", "expected")]
            response = await client.post(path, headers={"Origin": origin}, json={})
            assert response.status_code == 401
            assert response.headers["access-control-allow-origin"] == origin
            assert response.headers["www-authenticate"] == "Bearer"
            assert "access-control-allow-credentials" not in response.headers
        records = [r for r in caplog.records if r.name == "uvicorn.error.rokid"]
        assert [r.args for r in records] == [
            (path, 200, "OPTIONS", "expected"),
            (path, 401, "POST", "expected"),
        ]

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())


@pytest.mark.parametrize(
    "origin",
    [
        "https://other.example",
        "null",
        "http://aiui.rokid.com",
        "https://aiui.rokid.com.evil.test",
        "http://aiui-global.rokid.com",
        "https://aiui-global.rokid.com.evil.test",
    ],
)
def test_other_origins_receive_no_cors_authorization(origin):
    async def check():
        def handler(request):
            raise AssertionError("Rejected requests must not reach upstream")

        async with client_for(handler) as client:
            response = await client.options(
                "/v1/chat",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            assert response.status_code == 400
            assert "access-control-allow-origin" not in response.headers
            assert "access-control-allow-credentials" not in response.headers
            response = await client.post("/v1/chat", headers={"Origin": origin}, json={})
            assert response.status_code == 401
            assert "access-control-allow-origin" not in response.headers

    asyncio.run(check())


@pytest.mark.parametrize(
    ("method", "headers"), [("GET", "authorization"), ("POST", "authorization,x-private-header")]
)
def test_preview_preflight_rejects_other_methods_and_headers(method, headers):
    async def check():
        def handler(request):
            raise AssertionError("Rejected preflights must not reach upstream")

        async with client_for(handler) as client:
            response = await client.options(
                "/v1/chat",
                headers={
                    "Origin": PREVIEW_ORIGIN,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": headers,
                },
            )
            assert response.status_code == 400
            assert response.headers["access-control-allow-methods"] == "POST"
            assert "x-private-header" not in response.headers["access-control-allow-headers"]
            assert "access-control-allow-credentials" not in response.headers

    asyncio.run(check())


@pytest.mark.parametrize("path", ["/v1/chat", "/v1/chat/stream"])
@pytest.mark.parametrize("outcome", ["success", "invalid", "upstream_error", "unexpected_error"])
@pytest.mark.parametrize("origin", [PREVIEW_ORIGIN, GLOBAL_PREVIEW_ORIGIN])
def test_preview_can_read_normal_sse_and_error_responses(path, outcome, origin):
    async def check():
        calls = []

        def handler(request):
            calls.append(request)
            if outcome == "upstream_error":
                return httpx.Response(500, text="private upstream detail")
            if outcome == "unexpected_error":
                raise RuntimeError("private unexpected detail")
            if request.url.path.endswith("/stream"):
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content='event: done\ndata: {"reply":"你好"}\n\n',
                )
            return httpx.Response(200, json={"reply": "你好"})

        async with client_for(handler, raise_app_exceptions=False) as client:
            payload = {} if outcome == "invalid" else {"message": "hi", "session_id": "s"}
            response = await client.post(
                path, headers={**AUTH, "Origin": origin}, json=payload
            )
            expected = {
                "success": 200,
                "invalid": 422,
                "upstream_error": 502,
                "unexpected_error": 500,
            }
            assert response.status_code == expected[outcome]
            assert response.headers["access-control-allow-origin"] == origin
            assert "access-control-allow-credentials" not in response.headers
            assert "Origin" in response.headers["vary"]
            assert "private" not in response.text
            assert len(calls) == (0 if outcome == "invalid" else 1)
            if outcome == "success":
                assert "你好" in response.text

    asyncio.run(check())


@pytest.mark.parametrize("path", ["/v1/chat", "/v1/chat/stream"])
@pytest.mark.parametrize("bucket", ["expected", "other", "missing"])
@pytest.mark.parametrize("method", ["OPTIONS", "GET", "POST"])
def test_fixed_diagnostics_observe_cors_and_routing_without_upstream(path, bucket, method, caplog):
    async def check():
        def handler(request):
            raise AssertionError("Diagnostic requests must not reach upstream")

        headers = {
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        }
        if bucket != "missing":
            headers["Origin"] = (
                PREVIEW_ORIGIN if bucket == "expected" else "https://private-origin-marker.example"
            )
        async with client_for(handler) as client:
            response = await client.request(method, path, headers=headers)
        if method == "OPTIONS":
            status = {"expected": 200, "other": 400, "missing": 405}[bucket]
        else:
            status = 405 if method == "GET" else 401
        assert response.status_code == status
        records = [r for r in caplog.records if r.name == "uvicorn.error.rokid"]
        assert [r.args for r in records] == [(path, status, method, bucket)]
        assert all(r.exc_info is None for r in records)
        assert "private-origin-marker" not in caplog.text
        assert PREVIEW_ORIGIN not in caplog.text

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())


@pytest.mark.parametrize(
    ("origins", "bucket", "status"),
    [
        ([""], "other", 400),
        (["null"], "other", 400),
        ([PREVIEW_ORIGIN, "private-origin-marker"], "expected", 200),
        (["private-origin-marker", PREVIEW_ORIGIN], "other", 400),
    ],
)
def test_origin_bucket_matches_cors_effective_value(origins, bucket, status, caplog):
    async def check():
        def handler(request):
            raise AssertionError("Preflight must not reach upstream")

        headers = [
            ("Access-Control-Request-Method", "POST"),
            ("Access-Control-Request-Headers", "authorization,content-type"),
        ] + [("Origin", origin) for origin in origins]
        async with client_for(handler) as client:
            response = await client.options("/v1/chat", headers=headers)
        assert response.status_code == status
        records = [r for r in caplog.records if r.name == "uvicorn.error.rokid"]
        assert [r.args for r in records] == [("/v1/chat", status, "OPTIONS", bucket)]
        assert "private-origin-marker" not in caplog.text and PREVIEW_ORIGIN not in caplog.text

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())


def test_diagnostics_ignore_other_routes_and_methods(caplog):
    async def check():
        def handler(request):
            raise AssertionError("Unmatched requests must not reach upstream")

        headers = {
            "Origin": "private-origin-marker",
            "Authorization": "Bearer private-token-marker",
            "Cookie": "private-cookie-marker",
            "X-Forwarded-For": "192.0.2.177",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        }
        async with client_for(handler) as client:
            for path in (
                "/private-path-marker",
                "/v1/chat/",
                "/v1/chat/private-path-marker",
                "/v1/chat/stream/",
                "/V1/chat",
                "/v1/private-path-marker/../chat",
            ):
                # Preserve the raw path in the ASGI scope for the traversal-like case.
                forwarded = []

                async def inner(scope, receive, send):
                    forwarded.append(scope)
                    await send({"type": "http.response.start", "status": 404, "headers": []})
                    await send({"type": "http.response.body", "body": b""})

                async def receive():
                    return {"type": "http.disconnect"}

                async def send(message):
                    pass

                scope = {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "headers": [(b"origin", b"private-origin-marker")],
                    "query_string": b"secret=private-query-marker",
                    "client": ("192.0.2.177", 3),
                }
                await RouteStatusLog(inner)(scope, receive, send)
                assert forwarded == [scope]
            for method in ("PUT", "PATCH", "DELETE", "HEAD"):
                await client.request(
                    method, "/v1/chat?secret=private-query-marker", headers=headers
                )
        assert not any(r.name == "uvicorn.error.rokid" for r in caplog.records)
        assert "private-" not in caplog.text and "192.0.2.177" not in caplog.text

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())


def test_outer_diagnostic_reports_unexpected_500_without_exception_details(caplog):
    async def check():
        def handler(request):
            raise RuntimeError("private-exception-marker")

        async with client_for(handler, raise_app_exceptions=False) as client:
            response = await client.post(
                "/v1/chat?private-query-marker",
                headers={**AUTH, "Origin": PREVIEW_ORIGIN},
                json={"message": "private-message-marker", "session_id": "private-session-marker"},
            )
        assert response.status_code == 500
        assert response.headers["access-control-allow-origin"] == PREVIEW_ORIGIN
        records = [r for r in caplog.records if r.name == "uvicorn.error.rokid"]
        assert [r.args for r in records] == [("/v1/chat", 500, "POST", "expected")]
        assert records[0].exc_info is None
        assert "private-" not in caplog.text and TOKEN not in caplog.text

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())


def test_disabled_default_and_secret_validation(monkeypatch):
    monkeypatch.delenv("ROKID_GATEWAY_ENABLED", raising=False)
    monkeypatch.delenv("ROKID_GATEWAY_TOKEN", raising=False)
    assert not GatewaySettings(_env_file=None).enabled
    assert TOKEN not in repr(settings())
    for token in ("", "short", "x" * 31, "x" * 31 + " ", "中" * 32):
        with pytest.raises(ValidationError):
            GatewaySettings(_env_file=None, enabled=True, token=token)
    for origin in (
        "file:///tmp/socket",
        "http://a:password@localhost",
        "http://host/chat",
        "http://h?q=x",
    ):
        with pytest.raises(ValidationError):
            settings(upstream_url=origin)


def test_rejected_requests_never_reach_upstream():
    async def check():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"reply": "unexpected"})

        async with client_for(handler) as client:
            for headers in (
                {},
                {"Authorization": "Bearer wrong"},
                {"Authorization": "Basic " + TOKEN},
            ):
                response = await client.post("/v1/chat", headers=headers, json={})
                assert response.status_code == 401
            for payload in (
                {},
                {"message": " ", "session_id": "s"},
                {"message": "x" * 32001, "session_id": "s"},
                {"message": 12, "session_id": "s"},
                {"message": "hi", "session_id": ""},
                {"message": "hi", "session_id": "s", "remember": "false"},
                {"message": "hi", "session_id": "s", "request_id": "not-a-uuid"},
                {"message": "hi", "session_id": "s", "user_id": "someone-else"},
                {"message": "hi", "session_id": "s", "upstream_url": "http://elsewhere"},
                {"message": "hi", "session_id": "s", "image": "data:image/png;base64,test"},
                {"message": [{"type": "image"}], "session_id": "s"},
            ):
                response = await client.post("/v1/chat/stream", headers=AUTH, json=payload)
                assert response.status_code == 422
                assert "not supported" in response.json()["detail"]
            response = await client.post("/v1/chat", headers=AUTH, content=b"plain text")
            assert response.status_code == 415
            response = await client.post(
                "/v1/chat", headers={**AUTH, "Content-Type": "application/json"}, content=b"{"
            )
            assert response.status_code == 422
        async with client_for(handler, GatewaySettings(_env_file=None, enabled=False)) as client:
            response = await client.post("/v1/chat", headers=AUTH, json={})
            assert response.status_code == 404
        assert calls == []

    asyncio.run(check())


def test_body_limit_including_chunked_and_false_content_length():
    async def check():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"reply": "unexpected"})

        async def chunks():
            yield b" " * (MAX_BODY_BYTES // 2)
            yield b" " * (MAX_BODY_BYTES // 2 + 1)

        async with client_for(handler) as client:
            for length in (None, "1", str(MAX_BODY_BYTES + 1)):
                headers = {**AUTH, "Content-Type": "application/json"}
                if length is not None:
                    headers["Content-Length"] = length
                response = await client.post("/v1/chat", headers=headers, content=chunks())
                assert response.status_code == 413
            for length in ("-1", "invalid"):
                response = await client.post(
                    "/v1/chat",
                    headers={**AUTH, "Content-Type": "application/json", "Content-Length": length},
                    content=b"{}",
                )
                assert response.status_code == 400
        assert calls == []

    asyncio.run(check())


def test_chat_uses_fixed_identity_and_private_session_namespace():
    async def check():
        calls = []
        request_id = str(uuid4())

        def handler(request):
            calls.append(request)
            return httpx.Response(
                200, json={"reply": "你好，世界", "internal_secret": "not returned"}
            )

        async with client_for(handler, settings(user_id="fixed-owner")) as client:
            response = await client.post(
                "/v1/chat",
                headers=AUTH,
                json={
                    "message": "你好",
                    "session_id": "日常",
                    "request_id": request_id,
                    "remember": False,
                },
            )
            assert response.status_code == 200
            assert response.json() == {
                "reply": "你好，世界",
                "session_id": "日常",
                "request_id": request_id,
            }
        request = calls[0]
        body = json.loads(request.content)
        assert request.url == "http://127.0.0.1:8000/chat"
        assert "authorization" not in request.headers
        assert body["user_id"] == "fixed-owner"
        assert body["session_id"].startswith("rokid-") and len(body["session_id"]) == 70
        assert body["request_id"] == request_id and body["remember"] is False
        assert body["mode"] == "voice" and body["message"] == "你好"
        same = upstream_body(TextRequest(message="next", session_id="日常"), settings())
        other = upstream_body(TextRequest(message="next", session_id="other"), settings())
        assert same["session_id"] == body["session_id"] != other["session_id"]
        assert same["remember"] is False
        explicit = upstream_body(
            TextRequest(message="save", session_id="s", remember=True), settings()
        )
        assert explicit["remember"] is True

    asyncio.run(check())


def test_only_exact_post_routes_exist():
    async def check():
        def handler(request):
            raise AssertionError("No upstream request should occur")

        async with client_for(handler) as client:
            for path in (
                "/docs",
                "/redoc",
                "/openapi.json",
                "/chat",
                "/memory",
                "/bridge/shared/search",
                "/v1/chat/",
                "/v1/chat/stream/",
                "/v1/chat/cancel",
            ):
                response = await client.post(path, headers=AUTH, json={})
                assert response.status_code == 404
            assert (await client.get("/v1/chat", headers=AUTH)).status_code == 405
        app = create_app(settings())
        assert {route.path for route in app.app.app.routes} == {
            "/v1/chat",
            "/v1/chat/stream",
            "/v1/photo",
            "/v1/device-result",
        }

    asyncio.run(check())


@pytest.mark.parametrize("path", ["/v1/chat", "/v1/chat/stream"])
@pytest.mark.parametrize("failure", ["redirect", "status", "connection", "timeout", "invalid"])
def test_upstream_failures_are_safe_and_do_not_follow_redirects(path, failure):
    async def check():
        calls = []

        def handler(request):
            calls.append(request)
            if failure == "connection":
                raise httpx.ConnectError("secret internal host", request=request)
            if failure == "timeout":
                raise httpx.ReadTimeout("secret internal host", request=request)
            if failure == "redirect":
                return httpx.Response(302, headers={"Location": "https://external.example/secret"})
            if failure == "status":
                return httpx.Response(500, json={"detail": "secret internal error"})
            return httpx.Response(200, text="secret HTML", headers={"Content-Type": "text/html"})

        async with client_for(handler) as client:
            response = await client.post(
                path, headers=AUTH, json={"message": "hi", "session_id": "s"}
            )
            assert response.status_code == (504 if failure == "timeout" else 502)
            assert response.json() == {"detail": UPSTREAM_ERROR}
            assert len(calls) == 1

    asyncio.run(check())


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, failure=False):
        self.chunks = chunks
        self.failure = failure
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.failure:
            raise httpx.ReadError("private stream error")

    async def aclose(self):
        self.closed = True


def test_native_sse_unicode_boundaries_heartbeat_and_control_events():
    async def check():
        raw = (
            ": private diagnostic\r\n\r\n"
            'event: task\r\ndata: {"task_id":"t"}\r\n\r\n'
            'event: steering\ndata: {"task_id":"t"}\n\n'
            'event: delta\ndata: {"text":"你好🌏"}\n\n'
            'event: done\ndata: {"reply":"你好🌏"}\n\n'
        ).encode()
        stream = Chunks([bytes([byte]) for byte in raw])
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

        async with client_for(handler) as client:
            response = await client.post(
                "/v1/chat/stream", headers=AUTH, json={"message": "hi", "session_id": "s"}
            )
            assert response.status_code == 200
            assert "private diagnostic" not in response.text
            assert ": keep-alive\n\n" in response.text
            assert "event: task" in response.text and "event: steering" in response.text
            assert '"text": "你好🌏"' in response.text
            assert response.text.count("event: done") == 1
            assert "event: error" not in response.text
            assert response.headers["x-accel-buffering"] == "no"
        assert stream.closed and len(calls) == 1
        assert calls[0].url.path == "/chat/stream"
        assert "authorization" not in calls[0].headers

    asyncio.run(check())


@pytest.mark.parametrize(
    ("raw", "failure", "expected"),
    [
        ('event: done\ndata: {"reply":"only final"}\n\n', False, "done"),
        ('event: superseded\ndata: {"task_id":"t"}\n\n', False, "superseded"),
        ('event: error\ndata: {"detail":"private secret"}\n\n', False, "error"),
        ('event: delta\ndata: {"text":"partial"}\n\n', True, "error"),
        ('event: delta\ndata: {"text":"partial"}\n\n', False, "error"),
        ("event: done\ndata: invalid secret\n\n", False, "error"),
    ],
)
def test_stream_terminal_events_and_safe_errors(raw, failure, expected):
    async def check():
        stream = Chunks([raw.encode()], failure=failure)
        response = httpx.Response(200, stream=stream)
        output = "".join([item async for item in relay_events(response)])
        assert f"event: {expected}\n" in output
        assert "private secret" not in output and "invalid secret" not in output
        if expected == "error":
            assert UPSTREAM_ERROR in output
            assert output.count("event: error") == 1
        assert stream.closed

    asyncio.run(check())


def test_detaching_stream_closes_subscription_without_cancel_request():
    async def check():
        stream = Chunks(
            [
                b'event: task\ndata: {"task_id":"t"}\n\n',
                b'event: delta\ndata: {"text":"still working"}\n\n',
            ]
        )
        response = httpx.Response(200, stream=stream)
        relay = relay_events(response)
        assert "event: task" in await anext(relay)
        await relay.aclose()
        assert stream.closed

    asyncio.run(check())


@pytest.mark.parametrize("failed_message", ["http.response.start", "http.response.body"])
def test_downstream_failure_closes_upstream_even_before_first_event(failed_message):
    async def check():
        stream = Chunks([b'event: task\ndata: {"task_id":"t"}\n\n'])
        upstream = httpx.Response(200, stream=stream)
        response = UpstreamStreamingResponse(upstream)

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == failed_message:
                raise OSError("downstream is gone")

        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert stream.closed

    asyncio.run(check())


def test_oversized_unterminated_sse_line_stops_reading_at_limit():
    async def check():
        class LongLine(Chunks):
            consumed = 0

            async def __aiter__(self):
                for chunk in self.chunks:
                    self.consumed += len(chunk)
                    yield chunk

        stream = LongLine([b"x" * 8192 for _ in range(256)])
        response = httpx.Response(200, stream=stream)
        output = "".join([item async for item in relay_events(response)])
        assert stream.consumed <= MAX_EVENT_BYTES + 8192
        assert output.count("event: error") == 1 and UPSTREAM_ERROR in output
        assert stream.closed

    asyncio.run(check())


@pytest.mark.parametrize("path", ["/v1/chat", "/v1/chat/stream"])
def test_route_status_diagnostics_exclude_sensitive_request_data(path, caplog):
    async def check():
        def handler(request):
            if request.url.path.endswith("/stream"):
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=b'event: done\ndata: {"reply":"ok"}\n\n',
                )
            return httpx.Response(200, json={"reply": "ok"})

        headers = {
            **AUTH,
            "Origin": "https://private-origin-marker.example",
            "Cookie": "private-cookie-marker",
            "X-Forwarded-For": "192.0.2.177",
        }
        payload = {"message": "private-message-marker", "session_id": "private-session-marker"}
        target = path + "?secret=private-query-marker"
        async with client_for(handler, settings(user_id="private-owner-marker")) as client:
            response = await client.post(
                target, headers={**headers, "Authorization": "Bearer wrong"}, json=payload
            )
            assert response.status_code == 401
            response = await client.post(
                target, headers=headers, json={**payload, "image": "private-image-marker"}
            )
            assert response.status_code == 422
            response = await client.post(
                target,
                headers={**headers, "Content-Type": "application/json"},
                content=b"x" * (MAX_BODY_BYTES + 1),
            )
            assert response.status_code == 413
            response = await client.post(target, headers=headers, json=payload)
            assert response.status_code == 200
            await client.get(target, headers=headers)
            await client.post(
                "/private-path-marker?secret=private-query-marker", headers=headers, json=payload
            )
        records = [record for record in caplog.records if record.name == "uvicorn.error.rokid"]
        assert [record.args for record in records] == [
            (path, status, "POST", "other") for status in (401, 422, 413, 200)
        ] + [(path, 405, "GET", "other")]
        assert all(
            record.getMessage().startswith(f"rokid_http route={path} status={record.args[1]} ")
            for record in records
        )
        assert all(record.exc_info is None for record in records)
        assert all(record.created > 0 for record in records)
        for secret in (TOKEN, "private-", "192.0.2.177", "Bearer wrong"):
            assert secret not in caplog.text

    # Only this fixed diagnostic logger is enabled; ordinary HTTP access logs stay disabled.
    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())


@pytest.mark.parametrize("started", [False, True])
def test_route_status_diagnostic_is_once_even_when_stream_disconnects(started, caplog):
    async def check():
        async def application(scope, receive, send):
            if started:
                await send({"type": "http.response.start", "status": 200, "headers": []})
            raise ClientDisconnect()

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        middleware = RouteStatusLog(application)
        with pytest.raises(ClientDisconnect):
            await middleware(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/chat/stream",
                    "query_string": b"token=private-query-marker",
                    "client": ("192.0.2.177", 1234),
                },
                receive,
                send,
            )
        records = [record for record in caplog.records if record.name == "uvicorn.error.rokid"]
        assert len(records) == 1
        assert records[0].args == (
            "/v1/chat/stream",
            200 if started else "not_started",
            "POST",
            "missing",
        )
        assert records[0].exc_info is None
        assert "private-" not in caplog.text and "192.0.2.177" not in caplog.text

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())
