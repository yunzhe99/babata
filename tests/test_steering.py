import asyncio
import json
from types import SimpleNamespace

from agents import Agent, Model, SQLiteSession, function_tool
from openai.types.responses import Response, ResponseCompletedEvent

from babata.main import ChatRequest
from babata.steering import SteeringRuns


class ScriptedModel(Model):
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.inputs = []

    async def get_response(self, *args, **kwargs):
        raise AssertionError("Expected streaming")

    async def stream_response(self, *args, **kwargs):
        inputs = kwargs.get("input", args[1] if len(args) > 1 else None)
        self.inputs.append(inputs)
        response = Response(
            id=f"response_{len(self.inputs)}",
            created_at=0,
            model="scripted",
            object="response",
            output=next(self.outputs),
            parallel_tool_calls=False,
            tool_choice="auto",
            tools=[],
        )
        yield ResponseCompletedEvent(
            type="response.completed", response=response, sequence_number=1
        )


def call(name, identity):
    return {"type": "function_call", "call_id": identity, "name": name, "arguments": "{}"}


def answer(text):
    return {
        "id": "answer",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


async def empty_profile(_):
    return ""


async def no_memory(*_):
    return {"memory_status": "disabled", "memory_job_id": None}


def setup(agent):
    session = SQLiteSession("steering")
    lock = asyncio.Lock()
    state = SimpleNamespace(
        agent=agent,
        profiles=SimpleNamespace(get=empty_profile),
        settings=SimpleNamespace(llm_timeout_seconds=5, codex_bridge_enabled=False),
        sessions=SimpleNamespace(lock=lambda _: lock, get=lambda _: session),
    )
    return SteeringRuns(state, no_memory), session, lock


def test_sdk_steer_preserves_tool_result_without_reexecuting():
    async def check():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        @function_tool
        async def work() -> str:
            calls.append("work")
            started.set()
            await release.wait()
            return "STEP_ALREADY_DONE"

        model = ScriptedModel([[call("work", "work_1")], [answer("按新要求继续")]])
        runs, session, lock = setup(
            Agent(name="test", instructions="BASE", model=model, tools=[work])
        )
        first = ChatRequest(user_id="u", session_id="s", message="先处理原任务")
        old = runs.stream(first)
        task_id = (await anext(old))[1]["task_id"]
        await asyncio.wait_for(started.wait(), 2)
        newer = runs.stream(
            ChatRequest(user_id="u", session_id="s", message="改成新要求", interrupted=True)
        )
        assert (await anext(newer))[1]["task_id"] == task_id
        assert (await anext(old))[0] == "superseded"
        await old.aclose()  # Replacing the HTTP client must not cancel the ongoing tool.
        assert calls == ["work"] and lock.locked()
        release.set()
        events = [event async for event in newer]
        assert [data["reply"] for event, data in events if event == "done"] == ["按新要求继续"]
        assert calls == ["work"]
        assert "STEP_ALREADY_DONE" in json.dumps(model.inputs[-1])
        items = await session.get_items()
        assert [i["content"] for i in items if i.get("role") == "user"] == [
            "先处理原任务",
            "改成新要求",
        ]
        assert not lock.locked()
        await runs.close()

    asyncio.run(check())


def test_explicit_stop_waits_for_tool_cleanup_before_unlock():
    async def check():
        started = asyncio.Event()
        cleaned = []
        lock = None

        @function_tool
        async def work() -> str:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.append(lock.locked())

        model = ScriptedModel([[call("work", "work_1")]])
        runs, _, lock = setup(Agent(name="test", instructions="BASE", model=model, tools=[work]))
        body = ChatRequest(user_id="u", session_id="s", message="开始")
        stream = runs.stream(body)
        task_id = (await anext(stream))[1]["task_id"]
        await asyncio.wait_for(started.wait(), 2)
        assert not await runs.cancel("other", "s", task_id)
        assert await runs.cancel("u", "s", task_id)
        assert cleaned == [True] and not lock.locked()
        await stream.aclose()

    asyncio.run(check())


def test_two_supplements_are_kept_in_order_when_old_answer_is_terminal():
    async def check():
        started, release = asyncio.Event(), asyncio.Event()

        class SlowAnswer(ScriptedModel):
            async def stream_response(self, *args, **kwargs):
                if not self.inputs:
                    started.set()
                    await release.wait()
                async for event in super().stream_response(*args, **kwargs):
                    yield event

        model = SlowAnswer([[answer("旧回答")], [answer("修改后的回答")]])
        runs, session, _ = setup(Agent(name="test", instructions="BASE", model=model))
        original = runs.stream(ChatRequest(user_id="u", session_id="s", message="安排东京行程"))
        task_id = (await anext(original))[1]["task_id"]
        await asyncio.wait_for(started.wait(), 2)
        second = runs.stream(ChatRequest(user_id="u", session_id="s", message="改成大阪"))
        assert (await anext(second))[1]["task_id"] == task_id
        third = runs.stream(ChatRequest(user_id="u", session_id="s", message="预算不变"))
        assert (await anext(third))[1]["task_id"] == task_id
        await original.aclose()
        await second.aclose()
        release.set()
        events = [event async for event in third]
        assert [data["reply"] for event, data in events if event == "done"] == ["修改后的回答"]
        items = await session.get_items()
        assert [i["content"] for i in items if i.get("role") == "user"] == [
            "安排东京行程",
            "改成大阪",
            "预算不变",
        ]
        assert len(model.inputs) == 2
        await runs.close()

    asyncio.run(check())
