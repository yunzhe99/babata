"""Stateless HTTP fixture. It can recall a token only if Babata replays history.

This is NOT a GPT substitute and is never included in the production image.
"""

import asyncio
import json
import re
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI()


def answer(items: list[dict]) -> str:
    user_messages = []
    assistant_messages = []
    instructions = []
    for item in items:
        content = item.get("content", "")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content)
        if item.get("role") == "user":
            user_messages.append(content)
        elif item.get("role") == "assistant":
            assistant_messages.append(content)
        elif item.get("role") in ("system", "developer"):
            instructions.append(content)
    if user_messages[-1] == "PROFILE_CHECK":
        matches = re.findall(r"PROFILE_MARKER=([A-Za-z0-9-]+)", " ".join(instructions))
        return matches[-1] if matches else "unknown"
    if "BABATA_MEMORY_EXTRACT_V1" in " ".join(instructions):
        payload = json.loads(user_messages[-1])
        message, profile = payload["user_message"], payload["profile"]
        previous = next((line for line in profile.splitlines() if "饮茶偏好" in line), "")
        edits = []
        if message == "我长期喜欢茉莉花茶，请记住我的饮茶偏好。":
            edits = [
                {
                    "action": "add",
                    "before": "",
                    "after": "- 饮茶偏好：茉莉花茶。",
                    "evidence": message,
                    "reason": "明确长期偏好",
                }
            ]
        elif message == "更正，我长期喜欢乌龙茶，不是茉莉花茶。" and previous:
            edits = [
                {
                    "action": "replace",
                    "before": previous,
                    "after": "- 饮茶偏好：乌龙茶。",
                    "evidence": message,
                    "reason": "明确更正",
                }
            ]
        elif message == "忘掉我的饮茶偏好。" and previous:
            edits = [
                {
                    "action": "forget",
                    "before": previous,
                    "after": "",
                    "evidence": message,
                    "reason": "明确要求忘掉",
                }
            ]
        return json.dumps({"edits": edits}, ensure_ascii=False)
    if user_messages[-1] == "我长期喜欢什么茶？":
        background = " ".join(instructions)
        return (
            "乌龙茶"
            if "饮茶偏好：乌龙茶" in background
            else ("茉莉花茶" if "饮茶偏好：茉莉花茶" in background else "unknown")
        )
    if user_messages[-1] == "TRIGGER_PROVIDER_ERROR":
        raise HTTPException(500, "test provider error")
    tokens = re.findall(r"TOKEN=([A-Za-z0-9-]+)", " ".join(user_messages))
    token = tokens[-1] if tokens else "unknown"
    return f"token={token};users={len(user_messages)};assistants={len(assistant_messages)}"


@app.post("/v1/responses")
async def responses(body: dict):
    inputs = body["input"]
    if isinstance(inputs, str):
        inputs = [{"role": "user", "content": inputs}]
    inputs = [{"role": "system", "content": body.get("instructions", "") or ""}, *inputs]
    return {
        "id": "resp_" + uuid.uuid4().hex,
        "object": "response",
        "created_at": int(time.time()),
        "model": body["model"],
        "status": "completed",
        "output": [
            {
                "id": "msg_" + uuid.uuid4().hex,
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": answer(inputs), "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
    }


@app.post("/v1/chat/completions")
async def completions(body: dict):
    reply = answer(body["messages"])
    if body.get("stream"):

        async def chunks():
            for text in (reply[:10], reply[10:]):
                chunk = {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": body["model"],
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                }
                yield "data: " + json.dumps(chunk) + "\n\n"
                await asyncio.sleep(0.01)
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")
    return {
        "id": "chatcmpl_" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }
