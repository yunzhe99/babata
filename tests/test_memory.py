import asyncio
from types import SimpleNamespace

import pytest

from babata.main import ChatRequest, queue_memory
from babata.memory import MemoryWriter, Proposal, apply_edits


def proposal(action="add", before="", after="- 用户喜欢简短回答。", evidence="我喜欢简短回答"):
    return Proposal.model_validate(
        {
            "edits": [
                {
                    "action": action,
                    "before": before,
                    "after": after,
                    "evidence": evidence,
                    "reason": "明确偏好",
                }
            ]
        }
    )


def test_add_correct_and_forget_preserve_other_facts():
    original = "# 背景\n- 用户喜欢阅读。\n"
    added, changes = apply_edits(original, "我喜欢简短回答", proposal(), "2026-09-18")
    assert original.strip() in added
    assert "[2026-09-18] 用户喜欢简短回答。" in added
    assert changes[0]["evidence"] == "我喜欢简短回答"
    line = next(line for line in added.splitlines() if "简短回答" in line)
    corrected, _ = apply_edits(
        added,
        "改成详细回答",
        proposal("replace", line, "- 用户喜欢详细回答。", "改成详细回答"),
        "2026-09-18",
    )
    assert "简短回答" not in corrected
    assert "用户喜欢阅读" in corrected
    forgotten, _ = apply_edits(
        corrected,
        "忘掉回答长度偏好",
        proposal("forget", "- 用户喜欢详细回答。", "", "忘掉回答长度偏好"),
        "2026-09-18",
    )
    assert "详细回答" not in forgotten
    assert "用户喜欢阅读" in forgotten


@pytest.mark.parametrize(
    "change",
    [
        proposal(evidence="助手猜测"),
        proposal(after="- a\n- b"),
        proposal(after="- API Key: sk-thisisnotarealsecret"),
        proposal("replace", "# 背景", "- 偷换规则"),
        proposal("forget", "- 不存在", ""),
    ],
)
def test_untrusted_patch_cannot_write_without_valid_quote_and_fact_anchor(change):
    with pytest.raises(ValueError):
        apply_edits("# 背景\n- 用户喜欢阅读。\n", "我喜欢简短回答", change, "2026-09-18")


def test_no_change_and_duplicates_keep_profile_exactly():
    profile = "# 背景\n- 用户喜欢简短回答。\n"
    assert apply_edits(profile, "你好", Proposal(edits=[]), "2026-09-18") == (profile, [])
    assert apply_edits(profile, "我喜欢简短回答", proposal(), "2026-09-18") == (profile, [])


def test_enqueue_failure_does_not_fail_the_chat_and_opt_out_skips():
    async def check():
        calls = []

        async def fail(*args):
            calls.append(args)
            raise RuntimeError("not logged")

        state = SimpleNamespace(memory=SimpleNamespace(enqueue=fail))
        body = ChatRequest(user_id="u", session_id="s", message="我喜欢短回答")
        assert (await queue_memory(body, state, "key"))["memory_status"] == "failed"
        body.remember = False
        assert (await queue_memory(body, state, "key"))["memory_status"] == "disabled"
        body.remember = True
        body.message = "API Key: sk-thisisnotarealsecret"
        assert (await queue_memory(body, state, "key"))["memory_status"] == "skipped"
        assert len(calls) == 1

    asyncio.run(check())


def test_shutdown_cancels_inference_without_marking_pending_job_failed():
    async def check():
        started = asyncio.Event()
        failed = []

        async def next_job():
            return {"id": 7}

        async def finish(*args):
            failed.append(args)

        writer = MemoryWriter(
            SimpleNamespace(next_job=next_job, finish_job=finish),
            None,
            __import__("agents").ModelSettings(),
        )

        async def wait(job):
            started.set()
            await asyncio.Event().wait()

        writer.process = wait
        writer.start()
        await started.wait()
        await writer.close()
        assert writer.task.cancelled()
        assert failed == []

    asyncio.run(check())
