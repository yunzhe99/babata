"""One active SDK task per session; new input steers at the next execution boundary."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime

from agents import FunctionTool, RunConfig, Runner, UserError

from babata.agent import for_request
from babata.codex import with_codex_tools
from babata.rokid_photos import model_user_content
from babata.sessions import session_key

logger = logging.getLogger(__name__)


class SteeringRuns:
    def __init__(self, state, remember):
        self.state = state
        self.remember = remember
        self.active = {}

    async def stream(self, body):
        key = session_key(body.user_id, body.session_id)
        task = self.active.get(key)
        if task is None or task.worker.done():
            task = ConversationTask(self, key, body)
            self.active[key] = task
        queue = task.attach(body)
        try:
            while (event := await queue.get()) is not None:
                yield event
        finally:
            # A new HTTP stream can replace this one without killing the SDK task.
            if task.output is queue:
                task.output = None

    async def cancel(self, user, session, task_id):
        key = session_key(user, session)
        task = self.active.get(key)
        if task is None or task.task_id != str(task_id):
            return False
        task.stop()
        with suppress(asyncio.CancelledError):
            await task.worker
        return True

    async def close(self):
        tasks = list(self.active.values())
        for task in tasks:
            task.stop()
        await asyncio.gather(*(task.worker for task in tasks), return_exceptions=True)


class ConversationTask:
    def __init__(self, owner, key, body):
        self.owner, self.key = owner, key
        self.task_id = str(body.request_id)
        self.pending = []
        self.seen = set()
        self.latest = body
        self.output = None
        self.result = None
        self.stopped = False
        self.tool_tasks = set()
        self.worker = asyncio.create_task(self.run())

    def stop(self):
        self.stopped = True
        self.pending.clear()
        if self.result is not None and not self.result.is_complete:
            self.result.cancel()
        else:
            self.worker.cancel()

    def track_tool(self, invoke):
        async def tracked(context, arguments):
            # Keep cleanup of an already-started tool inside the session lock even if
            # SDK cancellation stops its outer task before the tool's finally finishes.
            task = asyncio.create_task(invoke(context, arguments))
            self.tool_tasks.add(task)
            task.add_done_callback(self.tool_tasks.discard)
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.done() and not task.cancelling():
                    task.cancel()
                raise

        return tracked

    def attach(self, body):
        if self.output is not None:
            self.output.put_nowait(("superseded", {"task_id": self.task_id}))
            self.output.put_nowait(None)
        self.output = asyncio.Queue()
        self.output.put_nowait(("task", {"task_id": self.task_id}))
        if body.request_id not in self.seen:
            self.seen.add(body.request_id)
            self.pending.append((body, datetime.now(UTC)))
            self.latest = body
            if self.result is not None:
                self.result.cancel(mode="after_turn")
                self.output.put_nowait(("steering", {"task_id": self.task_id}))
        return self.output

    def emit(self, body, event, data):
        if self.output is not None and body.request_id == self.latest.request_id:
            self.output.put_nowait((event, {**data, "task_id": self.task_id}))

    async def run(self):
        try:
            async with self.owner.state.sessions.lock(self.key):
                await self.run_locked()
        finally:
            if self.output is not None:
                self.output.put_nowait(None)
            if self.owner.active.get(self.key) is self:
                self.owner.active.pop(self.key, None)

    async def run_locked(self):
        state = self.owner.state
        result = None
        try:
            while self.pending and not self.stopped:
                batch, self.pending = self.pending, []
                body = batch[-1][0]
                agent = await with_codex_tools(
                    for_request(
                        state.agent,
                        await state.profiles.get(body.user_id),
                        body.mode,
                        body.interrupted,
                    ),
                    state,
                    body,
                    should_defer=lambda: bool(self.pending),
                )
                agent.tools = [
                    replace(tool, on_invoke_tool=self.track_tool(tool.on_invoke_tool))
                    if isinstance(tool, FunctionTool)
                    else tool
                    for tool in agent.tools
                ]
                inputs = [
                    {"role": "user", "content": model_user_content(item)} for item, _ in batch
                ]
                run_input = inputs
                if result is not None:
                    # All calls from the old step have finished. Bind future tools to the
                    # latest user message, rather than forwarding the previous instruction.
                    result.last_agent.instructions = agent.instructions
                    result.last_agent.tools = agent.tools
                    checkpoint = result.to_state()
                    try:
                        checkpoint.add_input(inputs)
                        run_input = checkpoint
                    except UserError as error:
                        # A completed answer is terminal. Continue the same task using
                        # the SDK's persisted history, including completed tool results.
                        if str(error) not in (
                            "Cannot add input to a terminal RunState",
                            "Cannot add input to a RunState with no remaining model turns",
                        ):
                            raise
                if self.stopped:
                    return
                async with asyncio.timeout(state.settings.llm_timeout_seconds + 5):
                    result = Runner.run_streamed(
                        agent,
                        run_input,
                        session=state.sessions.get(self.key),
                        run_config=RunConfig(tracing_disabled=True),
                        max_turns=5,
                    )
                    self.result = result
                    if self.pending:
                        result.cancel(mode="after_turn")
                    async for event in result.stream_events():
                        if (
                            event.type == "raw_response_event"
                            and event.data.type == "response.output_text.delta"
                        ):
                            self.emit(body, "delta", {"text": event.data.delta})
                if self.stopped:
                    self.emit(body, "error", {"detail": "Task stopped"})
                    return
                memory = {"memory_status": "disabled", "memory_job_id": None}
                for original, started_at in batch:
                    memory = await self.owner.remember(original, state, self.key, started_at)
                if not self.pending:
                    self.emit(body, "done", {"reply": result.final_output, **memory})
        except asyncio.CancelledError:
            self.emit(self.latest, "error", {"detail": "Task stopped"})
            raise
        except TimeoutError:
            self.emit(self.latest, "error", {"detail": "Model request timed out"})
        except Exception as error:
            logger.warning("Steered task failed: %s", type(error).__name__)
            self.emit(self.latest, "error", {"detail": "Model request failed"})
        finally:
            if result is not None and not result.is_complete:
                result.cancel()
            if result is not None and result.run_loop_task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await result.run_loop_task
            pending_tools = list(self.tool_tasks)
            for task in pending_tools:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*pending_tools, return_exceptions=True)
