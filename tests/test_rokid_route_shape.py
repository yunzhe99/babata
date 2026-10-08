import asyncio
import logging

import pytest

from babata.rokid_gateway import UnmatchedGetShapeLog, create_app


@pytest.mark.parametrize(
    "target, expected",
    [
        ("https://192.0.2.10/v1/chat", (True, True, True, True, True)),
        ("https://192.0.2.10:443/v1/chat", (True, True, True, True, True)),
        ("https://private-host-marker/v1/chat", (True, True, True, True, True)),
        ("/private-path-marker", (False, False, False, False, True)),
        ("https://[private-invalid-marker/v1/chat", (True, False, False, True, True)),
    ],
)
def test_unmatched_get_logs_only_shape_and_preserves_scope(target, expected, caplog):
    async def check():
        scope = {
            "type": "http",
            "method": "GET",
            "path": target,
            "raw_path": target.encode(),
            "query_string": b"secret=private-query-marker",
            "headers": [(b"authorization", b"Bearer private-token-marker")],
            "client": ("192.0.2.177", 123),
        }
        original = dict(scope)
        sent = []

        async def inner(received, receive, send):
            assert received is scope and received == original
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"private-body-marker"})

        async def receive():
            raise AssertionError("Diagnostics must not read request bodies")

        async def send(message):
            sent.append(message)

        await UnmatchedGetShapeLog(inner)(scope, receive, send)
        assert sent[-1]["body"] == b"private-body-marker"
        records = [r for r in caplog.records if r.name == "uvicorn.error.rokid"]
        assert len(records) == 1 and records[0].args == (404, *expected)
        assert records[0].exc_info is None
        for private in (target, "private-", "192.0.2.177"):
            assert private not in caplog.text

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())


@pytest.mark.parametrize(
    "method, path",
    [("GET", "/v1/chat"), ("GET", "/v1/chat/stream"), ("POST", "/private-path")],
)
def test_shape_diagnostic_leaves_other_cases_unlogged(method, path, caplog):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 405, "headers": []})

    async def receive():
        raise AssertionError("Must not read")

    async def send(message):
        pass

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(
        UnmatchedGetShapeLog(inner)({"type": "http", "method": method, "path": path}, receive, send)
    )
    assert not any(r.name == "uvicorn.error.rokid" for r in caplog.records)


@pytest.mark.parametrize("target, status", [("/v1/chat", 405), ("https://192.0.2.10/v1/chat", 404)])
def test_shape_diagnostic_does_not_rewrite_application_routing(target, status, caplog):
    async def check():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await create_app()(
            {
                "type": "http",
                "http_version": "1.1",
                "scheme": "https",
                "method": "GET",
                "path": target,
                "raw_path": target.encode(),
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "server": ("192.0.2.10", 443),
                "client": ("192.0.2.177", 123),
            },
            receive,
            send,
        )
        assert messages[0]["status"] == status
        records = [r for r in caplog.records if r.name == "uvicorn.error.rokid"]
        assert len(records) == 1
        prefix = "rokid_get_shape" if status == 404 else "rokid_http"
        assert records[0].getMessage().startswith(prefix)

    caplog.set_level(logging.INFO, logger="uvicorn.error.rokid")
    asyncio.run(check())
