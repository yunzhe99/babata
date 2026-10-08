"""Native camera decisions stay in one conversation without leaking protocol JSON."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from babata.device_actions import DeviceCapabilities, DeviceContinuation
from babata.main import ChatRequest
from babata.sessions import session_key
from babata.tokyo import (
    CAMERA_DECISION_SCHEMA,
    CAMERA_REPLY_SCHEMA,
    TokyoRuntime,
    TokyoTurn,
)


def request(*, camera=False, result=None, image=None, message="这是什么？"):
    return ChatRequest(user_id="u", session_id="rokid-synthetic", message=message).model_copy(
        update={
            "capabilities": DeviceCapabilities(camera=camera),
            "device_result": result,
            "image_data_url": image,
        }
    )


def continuation(status="ok"):
    return DeviceContinuation(
        original_request_id=uuid4(),
        generation=uuid4(),
        status=status,
        error_code="permission" if status == "error" else None,
    )


def native_fixture(answers, *, finish=True):
    saved, calls = [], []
    lock = asyncio.Lock()
    answers = iter(answers)
    current = SimpleNamespace(value=True)

    async def ingest(user, thread):
        saved.extend((m.role, m.text) for m in thread.messages)

    async def profile(user):
        return {"content": "", "revision": 0}

    async def remember(*args):
        return {"memory_status": "disabled"}

    async def thread(*args):
        return "synthetic-native-thread"

    runtime = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(codex_user_id="u", tokyo_native_memories=True),
            sessions=SimpleNamespace(lock=lambda _: lock),
            shared=SimpleNamespace(ingest=ingest),
            profiles=SimpleNamespace(snapshot=profile),
            device_actions=SimpleNamespace(current=lambda *_: current.value),
        ),
        workspace="/synthetic",
        active={},
        by_thread={},
        registry_lock=asyncio.Lock(),
        remember=remember,
        thread=thread,
    )

    async def call(method, params, **kwargs):
        calls.append((method, params))
        if method != "turn/start":
            return {}
        # Ordinary requests, camera decisions and picture callbacks all keep
        # the same owner workspace/network policy, including a warm thread.
        assert params["sandboxPolicy"]["type"] == "workspaceWrite"
        assert params["sandboxPolicy"]["networkAccess"] is True
        assert params["sandboxPolicy"]["writableRoots"] == ["/synthetic"]
        turn_id = "turn-" + str(len(calls))
        task = runtime.by_thread["synthetic-native-thread"]
        answer = next(answers)
        await task.events.put(
            {
                "method": "item/agentMessage/delta",
                "params": {"turnId": turn_id, "itemId": "answer", "delta": answer},
            }
        )
        if finish:
            await task.events.put(
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": turn_id,
                        "item": {"type": "agentMessage", "phase": "final_answer", "text": answer},
                    },
                }
            )
            await task.events.put(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": turn_id, "status": "completed"}},
                }
            )
        return {"turn": {"id": turn_id}}

    runtime.rpc = SimpleNamespace(call=call)
    return runtime, saved, calls, current


async def events(runtime, body):
    return [event async for event in TokyoRuntime.stream(runtime, body)]


@pytest.mark.parametrize("kind,answer", [("reply", "这是一个测试。"), ("take_photo", "")])
def test_camera_decision_parses_only_final_json_without_delta_or_archive_leak(kind, answer):
    async def check():
        raw = json.dumps({"kind": kind, "reply": answer})
        runtime, saved, calls, _ = native_fixture([raw])
        output = await events(runtime, request(camera=True))
        sent = calls[0][1]
        assert sent["outputSchema"] == CAMERA_DECISION_SCHEMA
        assert sent["additionalContext"]["rokid_camera_decision"]["kind"] == "application"
        assert not [payload for event, payload in output if event == "delta"]
        done = next(payload for event, payload in output if event == "done")
        assert done["reply"] == answer
        assert done.get("camera_requested", False) is (kind == "take_photo")
        assert saved == [("user", "这是什么？")] + (
            [("assistant", answer)] if kind == "reply" else []
        )
        assert all(raw != content for _, content in saved)

    asyncio.run(check())


@pytest.mark.parametrize(
    "raw",
    [
        "{not valid JSON}",
        '{"kind":"take_photo","reply":"已经看到了"}',
        '{"kind":"reply","reply":""}',
        '{"kind":"take_photo","reply":"","url":"http://example.invalid"}',
        '{"kind":"other","reply":"hi"}',
    ],
)
def test_malformed_model_decision_never_creates_action_or_leaks_json(raw):
    async def check():
        runtime, saved, _, _ = native_fixture([raw])
        output = await events(runtime, request(camera=True))
        assert any(event == "error" for event, _ in output)
        assert not any(event in {"delta", "done"} for event, _ in output)
        assert saved == [("user", "这是什么？")]

    asyncio.run(check())


@pytest.mark.parametrize(
    "answer",
    [
        "这是合成的普通中文回答。",
        "take a photo",
        "这里的 take_photo 只是能力名称，不表示需要执行。",
    ],
)
@pytest.mark.parametrize("camera", [True, False])
def test_plain_answer_is_a_reply_and_never_a_device_action(answer, camera, caplog):
    caplog.set_level("INFO", logger="babata.tokyo")

    async def check():
        runtime, saved, calls, _ = native_fixture([answer])
        body = request(camera=True) if camera else request(result=continuation("error"))
        output = await events(runtime, body)
        assert calls[0][1]["outputSchema"] == (
            CAMERA_DECISION_SCHEMA if camera else CAMERA_REPLY_SCHEMA
        )
        assert not any(event in {"delta", "error"} for event, _ in output)
        done = next(payload for event, payload in output if event == "done")
        assert done["reply"] == answer and "camera_requested" not in done
        assert saved == ([("user", "这是什么？")] if camera else []) + [("assistant", answer)]
        assert any(record.getMessage() == "Tokyo reply format=plain" for record in caplog.records)

    asyncio.run(check())


@pytest.mark.parametrize("status", ["ok", "error"])
@pytest.mark.parametrize("wire_format", ["json", "plain"])
def test_device_result_is_reply_only_application_turn_without_repeating_user(
    status, wire_format, caplog
):
    caplog.set_level("INFO", logger="babata.tokyo")

    async def check():
        answer = (
            ("照片中的物品是蓝色的合成测试方块，背景是一张白色测试卡片。" * 5)[:126]
            if wire_format == "plain"
            else "照片中的物品是红色。"
        )
        raw = answer if wire_format == "plain" else json.dumps({"kind": "reply", "reply": answer})
        if wire_format == "plain":
            assert len(raw) == 126
        runtime, saved, calls, _ = native_fixture([raw])
        image = "data:image/jpeg;base64,synthetic" if status == "ok" else None
        body = request(result=continuation(status), image=image)
        output = await events(runtime, body)
        sent = calls[0][1]
        assert sent["outputSchema"] == CAMERA_REPLY_SCHEMA
        assert sent["outputSchema"]["properties"]["kind"]["enum"] == ["reply"]
        assert sent["input"][0]["text"].startswith("应用回传：")
        assert sent["input"][0]["text"] != body.message
        assert sent["input"][1:] == ([{"type": "image", "url": image}] if image else [])
        context = sent["additionalContext"]["rokid_device_result"]
        assert context["kind"] == "application" and "不是用户新发言" in context["value"]
        assert saved == [("assistant", answer)]
        assert not any(event == "delta" for event, _ in output)
        done = next(payload for event, payload in output if event == "done")
        assert done["reply"] == answer and "camera_requested" not in done
        assert any(
            record.getMessage() == "Tokyo reply format=plain" for record in caplog.records
        ) is (wire_format == "plain")

    asyncio.run(check())


@pytest.mark.parametrize(
    "raw",
    [
        " ",
        "{not valid JSON}",
        "[not valid JSON]",
        "[]",
        '{"reply":"missing kind"}',
        '{"kind":"reply","reply":""}',
        '{"kind":"reply","reply":"hi","action":"take_photo"}',
        '```json\n{"kind":"reply","reply":"hi"}\n```',
        '~~~json\n{"kind":"reply","reply":"hi"}\n~~~',
        '好的，结果如下：\n```json\n{"kind":"take_photo","reply":""}\n```',
        '好的，结果如下：\n~~~json\n{"kind":"reply","reply":"hi"}\n~~~',
        "说明：\n```text\nsynthetic code\n```",
        '结果：{"kind":"reply","reply":"hi","extra":"invalid"}',
        '结果：{"reply":"hi","kind":"reply","extra":"invalid"}',
        '结果：{"reply":"missing kind"}',
        '结果：{"kind":"take_photo","reply":""}',
        "synthetic " * 4000,
    ],
)
@pytest.mark.parametrize("camera", [True, False])
def test_invalid_structured_camera_output_is_not_relaxed_into_plain_text(raw, camera):
    async def check():
        runtime, saved, _, _ = native_fixture([raw])
        body = request(camera=True) if camera else request(result=continuation("error"))
        output = await events(runtime, body)
        assert any(event == "error" for event, _ in output)
        assert not any(event in {"done", "delta"} for event, _ in output)
        assert saved == ([("user", "这是什么？")] if camera else [])

    asyncio.run(check())


@pytest.mark.parametrize("camera", [True, False])
def test_valid_reply_json_can_contain_code_and_control_words_without_an_action(camera):
    async def check():
        answer = '说明中的示例代码：\n```json\n{"kind":"take_photo","reply":""}\n```'
        raw = json.dumps({"kind": "reply", "reply": answer})
        runtime, saved, _, _ = native_fixture([raw])
        body = request(camera=True) if camera else request(result=continuation("error"))
        output = await events(runtime, body)
        assert not any(event in {"delta", "error"} for event, _ in output)
        done = next(payload for event, payload in output if event == "done")
        assert done["reply"] == answer and "camera_requested" not in done
        assert saved == ([("user", "这是什么？")] if camera else []) + [("assistant", answer)]

    asyncio.run(check())


def test_device_result_cannot_request_another_photo_even_if_model_breaks_schema():
    async def check():
        runtime, saved, _, _ = native_fixture(['{"kind":"take_photo","reply":""}'])
        output = await events(runtime, request(result=continuation("error")))
        assert any(event == "error" for event, _ in output)
        assert not any(event in {"done", "delta"} for event, _ in output)
        assert saved == []

    asyncio.run(check())


def test_plain_device_reply_and_followup_keep_the_original_native_thread():
    async def check():
        runtime, saved, calls, _ = native_fixture(
            [
                '{"kind":"take_photo","reply":""}',
                "照片中是一个合成测试方块。",
                "刚才看到的是合成测试方块。",
            ]
        )
        await events(runtime, request(camera=True))
        continued = await events(runtime, request(result=continuation("ok")))
        followup = await events(runtime, request(message="刚才看到了什么？"))
        assert {params["threadId"] for _, params in calls} == {"synthetic-native-thread"}
        assert calls[1][1]["outputSchema"] == CAMERA_REPLY_SCHEMA
        assert "outputSchema" not in calls[2][1]
        assert not any(event == "delta" for event, _ in continued)
        assert next(p for e, p in continued if e == "done")["reply"] == "照片中是一个合成测试方块。"
        assert next(p for e, p in followup if e == "done")["reply"] == "刚才看到的是合成测试方块。"
        assert saved == [
            ("user", "这是什么？"),
            ("assistant", "照片中是一个合成测试方块。"),
            ("user", "刚才看到了什么？"),
            ("assistant", "刚才看到的是合成测试方块。"),
        ]

    asyncio.run(check())


def test_photo_then_camera_enabled_followups_allow_mixed_json_and_plain_in_same_thread():
    async def check():
        photo_answer = "照片中是一个蓝色的合成测试方块。"
        plain_followup = ("刚才照片中的合成方块是蓝色。" * 6)[:72]
        assert len(plain_followup) == 72
        json_followup = "背景是一张白色的合成测试卡片。"
        runtime, saved, calls, _ = native_fixture(
            [
                '{"kind":"take_photo","reply":""}',
                photo_answer,
                plain_followup,
                json.dumps({"kind": "reply", "reply": json_followup}),
                '{"kind":"take_photo","reply":""}',
            ]
        )
        first = await events(runtime, request(camera=True))
        continued = await events(runtime, request(result=continuation("ok")))
        followup = await events(runtime, request(camera=True, message="刚才方块是什么颜色？"))
        next_followup = await events(runtime, request(camera=True, message="它的背景是什么？"))
        another_photo = await events(runtime, request(camera=True, message="再看一下眼前。"))
        assert {params["threadId"] for _, params in calls} == {"synthetic-native-thread"}
        assert [params["outputSchema"] for _, params in calls] == [
            CAMERA_DECISION_SCHEMA,
            CAMERA_REPLY_SCHEMA,
            CAMERA_DECISION_SCHEMA,
            CAMERA_DECISION_SCHEMA,
            CAMERA_DECISION_SCHEMA,
        ]
        for output, answer in (
            (continued, photo_answer),
            (followup, plain_followup),
            (next_followup, json_followup),
        ):
            assert not any(event in {"delta", "error"} for event, _ in output)
            done = next(payload for event, payload in output if event == "done")
            assert done["reply"] == answer and "camera_requested" not in done
        for output in (first, another_photo):
            done = next(payload for event, payload in output if event == "done")
            assert done["camera_requested"] is True and done["reply"] == ""
        assert saved == [
            ("user", "这是什么？"),
            ("assistant", photo_answer),
            ("user", "刚才方块是什么颜色？"),
            ("assistant", plain_followup),
            ("user", "它的背景是什么？"),
            ("assistant", json_followup),
            ("user", "再看一下眼前。"),
        ]

    asyncio.run(check())


def test_plain_legacy_reply_after_camera_decision_keeps_same_thread_and_original_wire_shape():
    async def check():
        runtime, saved, calls, _ = native_fixture(
            ['{"kind":"take_photo","reply":""}', "一加一等于二。"]
        )
        await events(runtime, request(camera=True))
        output = await events(runtime, request(message="一加一等于几？"))
        assert calls[0][1]["threadId"] == calls[1][1]["threadId"]
        plain = calls[1][1]
        assert "outputSchema" not in plain
        assert "不要沿用或输出" in plain["additionalContext"]["rokid_plain_reply"]["value"]
        assert any(e == "delta" and p["text"] == "一加一等于二。" for e, p in output)
        done = next(p for e, p in output if e == "done")
        assert done["reply"] == "一加一等于二。" and "camera_requested" not in done
        assert saved == [
            ("user", "这是什么？"),
            ("user", "一加一等于几？"),
            ("assistant", "一加一等于二。"),
        ]

    asyncio.run(check())


@pytest.mark.parametrize("first_camera,next_camera", [(True, True), (True, False), (False, True)])
def test_overlapping_camera_turn_does_not_steer_or_detach_existing_request(
    first_camera, next_camera
):
    async def check():
        runtime, _, calls, _ = native_fixture(["pending"], finish=False)
        first = request(camera=first_camera)
        key = session_key(first.user_id, first.session_id)
        task = TokyoTurn(runtime, key, first)
        runtime.active[key] = task
        initial = task.attach()
        await task.ready.wait()
        rejected = await task.steer(request(camera=next_camera))
        assert task.output is initial
        assert task.latest is first
        assert (await rejected.get())[0] == "error"
        assert await rejected.get() is None
        assert all(method != "turn/steer" for method, _ in calls)
        await task.stop()

    asyncio.run(check())


def test_stale_camera_continuation_rechecked_after_profile_await_before_model_call():
    async def check():
        runtime, saved, calls, current = native_fixture([])

        async def superseding_profile(user):
            current.value = False
            return {"content": "", "revision": 0}

        runtime.state.profiles.snapshot = superseding_profile
        output = await events(runtime, request(result=continuation("error")))
        assert calls == [] and saved == []
        assert any(event == "error" for event, _ in output)

    asyncio.run(check())


@pytest.mark.parametrize("active_result", [False, True])
def test_application_continuation_never_steers_into_an_existing_turn(active_result):
    async def check():
        runtime, saved, calls, _ = native_fixture(["pending"], finish=False)
        first = request(result=continuation("error") if active_result else None)
        incoming = request(result=None if active_result else continuation("error"))
        key = session_key(first.user_id, first.session_id)
        task = TokyoTurn(runtime, key, first)
        runtime.active[key] = task
        original_output = task.attach()
        await task.ready.wait()
        rejected = await task.steer(incoming)
        assert task.output is original_output and task.latest is first
        assert (await rejected.get())[0] == "error"
        assert await rejected.get() is None
        assert all(method != "turn/steer" for method, _ in calls)
        assert saved == ([] if active_result else [("user", first.message)])
        await task.stop()

    asyncio.run(check())


def test_already_stale_device_result_does_not_load_thread_or_profile():
    async def check():
        runtime, saved, calls, current = native_fixture([])
        current.value = False

        async def must_not_load(*args):
            pytest.fail("Stale device result loaded context")

        runtime.thread = must_not_load
        runtime.state.profiles.snapshot = must_not_load
        output = await events(runtime, request(result=continuation("error")))
        assert saved == [] and calls == []
        assert any(event == "error" for event, _ in output)

    asyncio.run(check())
