import asyncio
import base64
import json
import sqlite3
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agents import Agent
from pydantic import SecretStr

from babata.config import Settings
from babata.main import ChatRequest
from babata.sessions import session_key
from babata.tokyo import (
    TokyoRuntime,
    TokyoTurn,
    foreground_sandbox_policy,
    public_web_ready,
    rokid_memory_policy,
)


def test_command_network_and_workspace_access_belong_only_to_owner_tasks():
    runtime = SimpleNamespace(
        state=SimpleNamespace(settings=SimpleNamespace(codex_user_id="owner")),
        workspace="/var/lib/babata-tokyo/workspace",
    )
    owner = ChatRequest(user_id="owner", session_id="foreground", message="联网")
    policy = foreground_sandbox_policy(runtime, owner)
    assert policy == {
        "type": "workspaceWrite",
        "writableRoots": ["/var/lib/babata-tokyo/workspace"],
        "networkAccess": True,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }
    other = ChatRequest(user_id="other", session_id="foreground", message="联网")
    assert foreground_sandbox_policy(runtime, other) == {
        "type": "readOnly",
        "networkAccess": False,
    }


def test_public_web_waits_for_both_tools_before_first_model_turn():
    async def check():
        statuses = [
            {"name": "babata_public", "runtimeStatus": "starting", "tools": {}},
            {
                "name": "babata_public",
                "runtimeStatus": "connected",
                "tools": {"search_public_web": {}},
            },
            {
                "name": "babata_public",
                "runtimeStatus": "connected",
                "tools": {"search_public_web": {}, "fetch_public_web": {}},
            },
        ]
        calls = []

        async def call(method, params):
            calls.append((method, params))
            return {"data": [statuses.pop(0)]}

        assert await public_web_ready(SimpleNamespace(call=call), "thread")
        assert len(calls) == 3 and not statuses
        assert all(params == {"threadId": "thread"} for _, params in calls)

    asyncio.run(check())


@pytest.mark.parametrize("state", ["failed", "disabled", "timeout", "rpc_error"])
def test_public_web_startup_failure_is_bounded_and_optional(state):
    async def check():
        async def call(method, params):
            if state == "rpc_error":
                raise RuntimeError("Unavailable")
            return {"data": [{"name": "babata_public", "runtimeStatus": state, "tools": {}}]}

        assert not await public_web_ready(SimpleNamespace(call=call), "thread", timeout=0.01)

    asyncio.run(check())


def test_deepseek_runtime_uses_environment_credential_and_native_flash(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-the-child")
    settings = Settings(
        _env_file=None,
        llm_provider="deepseek",
        agent_runtime="codex",
        llm_api_key="synthetic-deepseek-key",
        postgres_password="synthetic-db-key",
        tokyo_state_dir=str(tmp_path),
    )
    runtime = TokyoRuntime(
        SimpleNamespace(settings=settings, shared=SimpleNamespace(memories=None)), None
    )
    assert runtime.rpc.env["BABATA_MODEL_API_KEY"] == "synthetic-deepseek-key"
    assert "OPENAI_API_KEY" not in runtime.rpc.env
    assert "synthetic-deepseek-key" not in " ".join(runtime.rpc.command)
    assert 'memories.extract_model="deepseek-flash"' in runtime.rpc.command
    assert 'memories.consolidation_model="deepseek-flash"' in runtime.rpc.command
    assert "model_context_window=1048576" in runtime.rpc.command
    assert "model_auto_compact_token_limit=900000" in runtime.rpc.command
    assert "features.use_legacy_landlock=false" in runtime.rpc.command
    assert "features.use_legacy_landlock=true" not in runtime.rpc.command
    assert not any("network_access=true" in value for value in runtime.rpc.command)
    assert 'mcp_servers.babata_public.default_tools_approval_mode="auto"' in runtime.rpc.command
    assert 'mcp_servers.babata_public.args=["-m", "babata.public_web_mcp"]' in runtime.rpc.command
    assert "mcp_servers.babata_public.enabled=false" in runtime.rpc.command


@pytest.mark.parametrize(
    ("session_id", "first_remember", "next_remember"),
    [("s", True, True), ("rokid-demo", False, True), ("rokid-demo", True, False)],
)
@pytest.mark.parametrize("with_image", [False, True])
def test_native_steer_keeps_turn_and_archives_all_input(
    session_id, first_remember, next_remember, with_image
):
    async def check():
        saved, calls = [], []
        automatic = []
        lock = asyncio.Lock()

        async def ingest(user, thread):
            saved.extend((user, m.role, m.text) for m in thread.messages)

        async def profile(user):
            return {"content": "", "revision": 0}

        async def remember(*args):
            automatic.append(args)
            return {"memory_status": "disabled"}

        runtime = SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(codex_user_id="u", tokyo_native_memories=True),
                sessions=SimpleNamespace(lock=lambda _: lock),
                shared=SimpleNamespace(ingest=ingest),
                profiles=SimpleNamespace(snapshot=profile),
            ),
            workspace="/test",
            active={},
            by_thread={},
            remember=remember,
        )

        async def thread(*args):
            return "tokyo-thread"

        async def call(method, params, **kwargs):
            calls.append((method, params))
            if method == "turn/start":
                return {"turn": {"id": "native-turn"}}
            return {}

        runtime.thread = thread
        runtime.rpc = SimpleNamespace(call=call)
        image = None
        if with_image:
            from PIL import Image

            buffer = BytesIO()
            Image.new("RGB", (8, 8), "red").save(buffer, "JPEG")
            image = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
        first = ChatRequest(
            user_id="u",
            session_id=session_id,
            message="原任务",
            remember=first_remember,
            image_data_url=image,
        )
        key = session_key("u", session_id)
        task = TokyoTurn(runtime, key, first)
        runtime.active[key] = task
        old = task.attach()
        await task.ready.wait()
        sent_input = next(params for method, params in calls if method == "turn/start")["input"]
        assert sent_input == (
            [{"type": "text", "text": "原任务"}]
            + ([{"type": "image", "url": image}] if with_image else [])
        )
        initial_context = next(params for method, params in calls if method == "turn/start")[
            "additionalContext"
        ]
        turn = next(params for method, params in calls if method == "turn/start")
        assert turn["sandboxPolicy"] == foreground_sandbox_policy(runtime, first)
        assert turn["sandboxPolicy"]["networkAccess"] is True
        assert "命令行联网" in initial_context["babata_execution_permissions"]["value"]
        if session_id.startswith("rokid-"):
            policy = initial_context["rokid_memory_policy"]
            assert policy["kind"] == "application"
            policy = json.loads(policy["value"])
            assert policy["remember"] is first_remember
            assert policy["native_memory_generation"] == "enabled"
        else:
            assert "rokid_memory_policy" not in initial_context
        await task.events.put(
            {
                "method": "item/agentMessage/delta",
                "params": {"turnId": "native-turn", "itemId": "old", "delta": "旧回答开始"},
            }
        )
        while (await old.get())[0] != "delta":
            pass
        latest = await task.steer(
            ChatRequest(
                user_id="u",
                session_id=session_id,
                message="补充要求",
                remember=next_remember,
                image_data_url=image,
            )
        )
        assert task.turn_id == "native-turn" and task.task_id == str(first.request_id)
        assert calls[-1][0] == "turn/steer"
        assert calls[-1][1]["expectedTurnId"] == "native-turn"
        assert task.latest.remember is next_remember
        inputs = calls[-1][1]["input"]
        assert inputs[0] == {"type": "text", "text": "补充要求"}
        if session_id.startswith("rokid-"):
            policy = json.loads(inputs[-1]["text"].split("：", 1)[1])
            assert policy["remember"] is next_remember
            assert policy["native_memory_generation"] == "enabled"
        else:
            assert len(inputs) == (2 if with_image else 1)
        if with_image:
            assert inputs[1] == {"type": "image", "url": image}
        assert not await TokyoRuntime.cancel(runtime, "other", session_id, task.task_id)
        await task.events.put(
            {
                "method": "item/agentMessage/delta",
                "params": {"turnId": "native-turn", "itemId": "old", "delta": "不应继续播报"},
            }
        )
        await task.events.put(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "native-turn",
                    "item": {
                        "id": "old",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "不应混入最终答案",
                    },
                },
            }
        )
        await task.events.put(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "native-turn",
                    "item": {"type": "agentMessage", "phase": "final_answer", "text": "继续完成"},
                },
            }
        )
        await task.events.put(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "native-turn", "status": "completed"}},
            }
        )
        await task.worker
        output = []
        while (value := await latest.get()) is not None:
            output.append(value)
        assert [v["reply"] for e, v in output if e == "done"] == ["继续完成"]
        assert saved == [
            ("u", "user", "原任务"),
            ("u", "user", "补充要求"),
            ("u", "assistant", "继续完成"),
        ]
        assert not lock.locked() and not runtime.active
        assert any(e == "superseded" for e, _ in list(old._queue) if e is not None)
        assert len(automatic) == (0 if session_id.startswith("rokid-") else 2)

    asyncio.run(check())


def test_native_tool_can_forward_original_to_existing_bridge():
    async def check():
        received = []

        async def targets():
            return [{"name": "babata", "label": "Babata 开发", "online": True}]

        async def submit(*args):
            received.append(args)
            return {"id": uuid4(), "target": "babata", "status": "queued"}

        state = SimpleNamespace(
            shared=None,
            agent=Agent(name="test", instructions=""),
            settings=SimpleNamespace(codex_bridge_enabled=True, codex_user_id="user"),
            codex=SimpleNamespace(targets=targets, submit=submit),
        )
        body = ChatRequest(user_id="user", session_id="s", message="发给 Babata 开发：只回复收到")
        task = SimpleNamespace(latest=body, task_id=str(body.request_id))
        result = await TokyoRuntime.invoke(
            SimpleNamespace(state=state), task, "send_to_codex", {"target": "babata"}
        )
        assert "queued" in result and received[0][-1] == body.message

    asyncio.run(check())


def test_peer_notice_uses_agent_draft_without_forwarding_user_commands():
    async def check():
        notices = []

        async def send(user, sender, body):
            notices.append((user, sender, body))
            return {"id": "notice-only"}

        runtime = SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(codex_user_id="user"),
                shared=SimpleNamespace(peer=SimpleNamespace(send=send)),
            )
        )
        task = SimpleNamespace(
            latest=ChatRequest(user_id="user", session_id="s", message="用户没有委托新任务"),
            thread_id="tokyo-thread",
            peer_update_draft="合并模型配置已改为 Terra；验收通过。",
        )
        result = await TokyoRuntime.invoke(
            runtime, task, "send_to_codex", {"target": "peer_updates"}
        )
        assert result["id"] == "notice-only"
        assert notices[0][2].message == "合并模型配置已改为 Terra；验收通过。"
        assert notices[0][2].source_ref == "tokyo:tokyo-thread"
        result = await TokyoRuntime.invoke(
            runtime, task, "send_to_codex", {"target": "peer_updates"}
        )
        assert not result["sent"] and len(notices) == 1
        task.latest.message = "发一条进展通知给桌面同伴，不创建执行任务。"
        result = await TokyoRuntime.invoke(runtime, task, "send_to_codex", {"target": "babata"})
        assert not result["sent"] and len(notices) == 1

    asyncio.run(check())


def test_unexpected_codex_exit_requests_restart_but_shutdown_does_not():
    restarted = []
    task = SimpleNamespace(events=asyncio.Queue())
    runtime = SimpleNamespace(
        active={"one": task}, closing=False, on_fatal=lambda: restarted.append(True)
    )
    TokyoRuntime.on_event(runtime, {"method": "__closed"})
    assert restarted == [True]
    assert task.events.get_nowait()["method"] == "__closed"
    runtime.closing = True
    TokyoRuntime.on_event(runtime, {"method": "__closed"})
    assert restarted == [True]


@pytest.mark.parametrize("thread_state", ["new", "persisted", "loaded"])
@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize(
    ("session_id", "user", "remember", "native_enabled", "expected_generation"),
    [
        ("rokid-demo", "user", False, True, True),
        ("rokid-demo", "user", True, True, True),
        ("rokid-demo", "user", False, False, False),
        ("rokid-demo", "other", False, True, False),
        ("rokid-demo", "other", True, True, False),
        ("s", "user", False, True, False),
        ("s", "user", True, True, True),
        ("s", "other", True, True, False),
    ],
)
def test_rokid_restores_owner_native_memory_without_changing_other_sessions(
    thread_state, provider, session_id, user, remember, native_enabled, expected_generation
):
    async def check():
        calls = []

        async def execute(*args):
            return SimpleNamespace(
                first=lambda: (
                    SimpleNamespace(thread_id="existing") if thread_state != "new" else None
                )
            )

        @asynccontextmanager
        async def begin():
            yield SimpleNamespace(execute=execute)

        async def get_items():
            return []

        async def rpc_call(method, params):
            calls.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "created"}}
            if method == "mcpServerStatus/list":
                return {
                    "data": [
                        {
                            "name": "babata_public",
                            "runtimeStatus": "connected",
                            "tools": {"search_public_web": {}, "fetch_public_web": {}},
                        }
                    ]
                }
            return {}

        runtime = SimpleNamespace(
            state=SimpleNamespace(
                sessions=SimpleNamespace(
                    engine=SimpleNamespace(begin=begin),
                    get=lambda key: SimpleNamespace(get_items=get_items),
                ),
                agent=Agent(name="test", instructions="BASE"),
                settings=SimpleNamespace(
                    codex_user_id="user",
                    llm_model="test-model",
                    llm_provider=provider,
                    tokyo_native_memories=native_enabled,
                ),
            ),
            workspace="/test",
            loaded={"existing"} if thread_state == "loaded" else set(),
            legacy_context={},
            rpc=SimpleNamespace(call=rpc_call),
        )
        body = ChatRequest(user_id=user, session_id=session_id, message="hi", remember=remember)
        await TokyoRuntime.thread(runtime, body, session_key(user, session_id))
        rokid = session_id.startswith("rokid-")
        modes = [p for name, p in calls if name == "thread/memoryMode/set"]
        assert modes == (
            [
                {
                    "threadId": "created" if thread_state == "new" else "existing",
                    "mode": "enabled" if expected_generation else "disabled",
                }
            ]
            if rokid or not expected_generation
            else []
        )
        if thread_state == "loaded":
            assert not any(name in ("thread/start", "thread/resume") for name, _ in calls)
            assert not any(name == "mcpServerStatus/list" for name, _ in calls)
        else:
            assert any(name == "mcpServerStatus/list" for name, _ in calls)
            method = "thread/start" if thread_state == "new" else "thread/resume"
            params = next(params for name, params in calls if name == method)
            assert params["modelProvider"] == provider
            if thread_state == "persisted":
                assert params["excludeTurns"] is True
            assert params["config"]["memories.generate_memories"] is expected_generation
            assert params["config"]["memories.use_memories"] is (user == "user")
            assert params["approvalPolicy"] == "never"
            assert params["sandbox"] == ("workspace-write" if user == "user" else "read-only")
            assert params["config"]["sandbox_workspace_write.network_access"] is (user == "user")
            assert params["config"]["sandbox_workspace_write.writable_roots"] == (
                ["/test"] if user == "user" else []
            )
            assert params["config"]["mcp_servers.babata_public.enabled"] is True
            assert ("本会话来自 Rokid" in params["developerInstructions"]) is rokid
            if rokid and thread_state == "persisted":
                # Restore eligibility before the resumed session can start its next turn.
                assert calls[0][0] == "thread/memoryMode/set"
        if rokid:
            policy = json.loads(rokid_memory_policy(body, runtime.state.settings))
            assert policy["native_memory_generation"] == (
                "enabled" if expected_generation else "disabled"
            )
            assert policy["profile_updates"] == (
                "explicit_user_request_only" if remember else "forbidden"
            )
            assert policy["automatic_profile_extraction"] == "disabled"

    asyncio.run(check())


@pytest.mark.parametrize("native_enabled", [False, True])
@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_start_restores_persisted_rokid_modes_before_any_session_scan(
    tmp_path, native_enabled, provider
):
    async def check():
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        modes = dict(owner_rokid="disabled", owner_phone="disabled", other_rokid="enabled")
        modes["other_phone"] = "enabled"
        database.execute("CREATE TABLE babata_tokyo_sessions (thread_id, user_id, session_id)")
        database.executemany(
            "INSERT INTO babata_tokyo_sessions VALUES (:t,:u,:s)",
            [
                {"t": "owner_rokid", "u": "user", "s": "rokid-demo"},
                {"t": "owner_phone", "u": "user", "s": "phone"},
                {"t": "other_rokid", "u": "other", "s": "rokid-demo"},
                {"t": "other_phone", "u": "other", "s": "phone"},
            ],
        )

        async def execute(statement, params):
            rows = database.execute(str(statement), params)
            return [SimpleNamespace(**dict(row)) for row in rows]

        @asynccontextmanager
        async def connect():
            yield SimpleNamespace(execute=execute)

        calls = []

        async def start():
            calls.append("rpc/start")

        async def rpc_call(method, params):
            calls.append(method)
            if method == "thread/memoryMode/set":
                modes[params["threadId"]] = params["mode"]

        def mirror_start():
            assert modes == {
                "owner_rokid": "enabled" if native_enabled else "disabled",
                "owner_phone": "disabled",
                "other_rokid": "disabled",
                "other_phone": "disabled",
            }
            calls.append("mirror/start")

        async def maintenance_start():
            calls.append("maintenance/start")

        runtime = SimpleNamespace(
            workspace=tmp_path / "workspace",
            directory=tmp_path,
            state=SimpleNamespace(
                settings=SimpleNamespace(
                    codex_user_id="user",
                    tokyo_native_memories=native_enabled,
                    llm_api_key=SecretStr("synthetic-test-key"),
                    llm_provider=provider,
                ),
                sessions=SimpleNamespace(engine=SimpleNamespace(connect=connect)),
            ),
            rpc=SimpleNamespace(start=start, call=rpc_call),
            memory_mirror=SimpleNamespace(start=mirror_start),
            memory_maintenance=SimpleNamespace(start=maintenance_start),
        )
        try:
            # Repeating startup (including a process restart) safely reapplies the policy.
            await TokyoRuntime.start(runtime)
            await TokyoRuntime.start(runtime)
            assert (
                calls
                == (
                    ["rpc/start"]
                    + (["account/login/start"] if provider == "openai" else [])
                    + [
                        "thread/memoryMode/set",
                        "thread/memoryMode/set",
                        "thread/memoryMode/set",
                        "mirror/start",
                        "maintenance/start",
                    ]
                )
                * 2
            )
        finally:
            database.close()

    asyncio.run(check())


def test_rokid_profile_write_checks_latest_intent_and_preserves_old_client_behavior():
    async def check():
        writes = []

        async def put(*args, **kwargs):
            writes.append((args, kwargs))
            return 7

        runtime = SimpleNamespace(
            state=SimpleNamespace(shared=None, profiles=SimpleNamespace(put=put))
        )
        task = SimpleNamespace(
            latest=ChatRequest(
                user_id="user", session_id="rokid-demo", message="hi", remember=False
            ),
            input_lock=asyncio.Lock(),
        )
        args = {"content": "explicit fact", "expected_revision": 6}
        with pytest.raises(PermissionError):
            await TokyoRuntime.invoke(runtime, task, "shared_update_profile", args)
        assert writes == []
        task.latest = task.latest.model_copy(update={"remember": True, "message": "记住这件事"})
        assert await TokyoRuntime.invoke(runtime, task, "shared_update_profile", args) == {
            "revision": 7
        }
        assert len(writes) == 1
        # A queued tool must recheck after a steering update releases the input lock.
        async with task.input_lock:
            pending = asyncio.create_task(
                TokyoRuntime.invoke(runtime, task, "shared_update_profile", args)
            )
            await asyncio.sleep(0)
            assert not pending.done()
            task.latest = task.latest.model_copy(update={"remember": False, "message": "先别保存"})
        with pytest.raises(PermissionError):
            await pending
        assert len(writes) == 1
        task.latest = task.latest.model_copy(update={"session_id": "legacy-phone"})
        assert await TokyoRuntime.invoke(runtime, task, "shared_update_profile", args) == {
            "revision": 7
        }
        assert len(writes) == 2

    asyncio.run(check())


@pytest.mark.parametrize("session_id", ["rokid-demo", "legacy-phone"])
def test_explicit_save_turn_skips_automatic_queue_only_for_rokid(session_id):
    async def check():
        automatic, archived = [], []
        lock = asyncio.Lock()

        async def ingest(user, thread):
            archived.extend(m.role for m in thread.messages)

        async def profile(user):
            return {"content": "", "revision": 0}

        async def remember(*args):
            automatic.append(args)
            return {"memory_status": "pending", "memory_job_id": 3}

        async def thread(*args):
            return "tokyo-thread"

        async def call(method, params, **kwargs):
            if method == "turn/start":
                await task.events.put(
                    {
                        "method": "item/completed",
                        "params": {
                            "turnId": "native-turn",
                            "item": {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "已处理",
                            },
                        },
                    }
                )
                await task.events.put(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "native-turn", "status": "completed"}},
                    }
                )
                return {"turn": {"id": "native-turn"}}
            return {}

        runtime = SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(codex_user_id="u", tokyo_native_memories=True),
                sessions=SimpleNamespace(lock=lambda _: lock),
                shared=SimpleNamespace(ingest=ingest),
                profiles=SimpleNamespace(snapshot=profile),
            ),
            workspace="/test",
            active={},
            by_thread={},
            remember=remember,
            thread=thread,
            rpc=SimpleNamespace(call=call),
        )
        body = ChatRequest(user_id="u", session_id=session_id, message="记住这件事", remember=True)
        key = session_key("u", session_id)
        task = TokyoTurn(runtime, key, body)
        runtime.active[key] = task
        output = task.attach()
        await task.worker
        events = []
        while (event := await output.get()) is not None:
            events.append(event)
        done = next(payload for event, payload in events if event == "done")
        assert len(automatic) == (0 if session_id.startswith("rokid-") else 1)
        assert done["memory_status"] == (
            "disabled" if session_id.startswith("rokid-") else "pending"
        )
        assert archived == ["user", "assistant"]

    asyncio.run(check())
