import asyncio
import json
from types import SimpleNamespace

from babata.main import ChatRequest, stream_chat


def test_explicit_stop_finishes_cancellation_before_unlock(monkeypatch):
    async def check():
        lock = asyncio.Lock()
        cleanup_with_lock = []

        async def work():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleanup_with_lock.append(lock.locked())

        class Result:
            is_complete = False
            run_loop_task = asyncio.create_task(work())

            async def stream_events(self):
                try:
                    await asyncio.sleep(0)
                    yield SimpleNamespace(
                        type="raw_response_event",
                        data=SimpleNamespace(type="response.output_text.delta", delta="你好"),
                    )
                    await asyncio.Event().wait()
                finally:
                    # Like the SDK, closing the event iterator waits on the model task.
                    await self.run_loop_task

            def cancel(self, mode="immediate"):
                self.is_complete = True
                self.run_loop_task.cancel()

        result = Result()

        async def get_profile(user_id):
            return ""

        monkeypatch.setattr("babata.steering.for_request", lambda agent, *args: agent)
        monkeypatch.setattr("babata.steering.Runner.run_streamed", lambda *a, **kw: result)
        state = SimpleNamespace(
            agent=SimpleNamespace(tools=[]),
            profiles=SimpleNamespace(get=get_profile),
            settings=SimpleNamespace(llm_timeout_seconds=1),
            sessions=SimpleNamespace(lock=lambda key: lock, get=lambda key: None),
        )
        stream = stream_chat(ChatRequest(user_id="u", session_id="s", message="hi"), state)
        task = await anext(stream)
        assert "你好" in await anext(stream)
        assert lock.locked()
        task_id = json.loads(task.split("data: ")[1])["task_id"]
        await asyncio.wait_for(state.steering.cancel("u", "s", task_id), timeout=0.5)
        await stream.aclose()
        assert result.run_loop_task.cancelled()
        assert cleanup_with_lock == [True]
        assert not lock.locked()

    asyncio.run(check())


def test_stream_queues_memory_only_after_successful_completion(monkeypatch):
    async def check():
        queued = []
        finish = asyncio.Event()
        lock = asyncio.Lock()

        class Result:
            is_complete = False
            final_output = "收到"
            run_loop_task = None

            async def stream_events(self):
                yield SimpleNamespace(
                    type="raw_response_event",
                    data=SimpleNamespace(type="response.output_text.delta", delta="收到"),
                )
                await finish.wait()
                self.is_complete = True

            def cancel(self, mode="immediate"):
                self.is_complete = True

        async def get_profile(user_id):
            return ""

        async def enqueue(*args):
            queued.append(args)
            return 42

        monkeypatch.setattr("babata.steering.for_request", lambda agent, *args: agent)
        monkeypatch.setattr("babata.steering.Runner.run_streamed", lambda *a, **kw: Result())
        state = SimpleNamespace(
            agent=SimpleNamespace(tools=[]),
            profiles=SimpleNamespace(get=get_profile),
            memory=SimpleNamespace(enqueue=enqueue),
            settings=SimpleNamespace(llm_timeout_seconds=1),
            sessions=SimpleNamespace(lock=lambda key: lock, get=lambda key: None),
        )
        stream = stream_chat(
            ChatRequest(user_id="u", session_id="s", message="我喜欢短回答"), state
        )
        assert "task" in await anext(stream)
        assert "delta" in await anext(stream)
        assert queued == []
        finish.set()
        done = await anext(stream)
        assert '"memory_job_id": 42' in done
        assert len(queued) == 1 and queued[0][2] == "我喜欢短回答"
        await stream.aclose()
        assert not lock.locked()

    asyncio.run(check())
