import asyncio
import json
import urllib.error
from types import SimpleNamespace
from uuid import uuid4

from agents import Agent
from fastapi.testclient import TestClient
from pydantic import SecretStr

from babata.codex import with_codex_tools
from babata.main import ChatRequest, app
from scripts.codex_worker import Worker, approval_decision


def test_approval_requires_users_exact_explicit_answer():
    assert approval_decision("允许这次操作。") == "accept"
    assert approval_decision("拒绝这次操作") == "decline"
    for answer in ["好", "不允许这次操作", "他说允许这次操作", "是否允许这次操作", "永远允许"]:
        assert approval_decision(answer) is None


def test_bridge_requires_its_own_token():
    app.state.settings = SimpleNamespace(codex_bridge_token=SecretStr("A" * 40))
    client = TestClient(app)
    for headers in [{}, {"Authorization": "Bearer wrong"}]:
        assert client.post("/bridge/claim", json=["babata"], headers=headers).status_code == 401


def test_forward_original_only_named_destination_and_stable_request_id():
    async def check():
        submitted = []

        async def targets():
            return [{"name": "babata", "label": "Babata 开发", "online": True}]

        async def submit(*args):
            submitted.append(args)
            return {"id": uuid4(), "target": args[3], "status": "queued"}

        state = SimpleNamespace(
            settings=SimpleNamespace(codex_bridge_enabled=True, codex_user_id="user"),
            codex=SimpleNamespace(targets=targets, submit=submit),
        )
        body = ChatRequest(user_id="user", session_id="voice", message="发给 Babata 开发：检查测试")
        agent = await with_codex_tools(Agent(name="test", instructions=""), state, body)
        tool = agent.tools[0]
        context = SimpleNamespace(
            tool_name=tool.name, _function_tool_arguments=None, run_config=None
        )
        output = await tool.on_invoke_tool(context, json.dumps({"target": "不存在的目标"}))
        assert "目标不存在" in output and not submitted
        await tool.on_invoke_tool(context, json.dumps({"target": "Babata 开发"}))
        assert submitted[0][2:] == (body.request_id, "babata", body.message)
        deferred = await with_codex_tools(
            Agent(name="test", instructions=""), state, body, should_defer=lambda: True
        )
        result = await deferred.tools[0].on_invoke_tool(context, '{"target":"babata"}')
        assert "未执行" in result and len(submitted) == 1
        body.message = "今天天气不错"
        await tool.on_invoke_tool(context, json.dumps({"target": "babata"}))
        assert len(submitted) == 1
        body.user_id = "other"
        assert (
            await with_codex_tools(Agent(name="test", instructions=""), state, body)
        ).tools == []

    asyncio.run(check())


def test_waiting_approval_survives_transient_gateway_disconnect(monkeypatch):
    async def check():
        calls, reads = [], 0

        async def api(path, body=None):
            nonlocal reads
            calls.append((path, body))
            if body is None:
                reads += 1
                if reads == 1:
                    raise urllib.error.URLError("temporary tunnel outage")
                return {"answer": "允许这次操作"}
            return {"ok": True}

        async def immediate(_):
            pass

        monkeypatch.setattr("scripts.codex_worker.asyncio.sleep", immediate)
        worker = Worker.__new__(Worker)
        worker.api = api
        answer = await worker.ask({"id": "synthetic"}, "read-only lookup", "approval")
        assert answer == "允许这次操作" and reads == 2
        assert [body["status"] for _, body in calls if body] == ["needs_input", "running"]

    asyncio.run(check())
