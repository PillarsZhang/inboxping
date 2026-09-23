import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import inboxping.ai.client as ai_module
from inboxping.ai.client import AIClient
from inboxping.ai.prompts import Prompt
from inboxping.config import Settings
from inboxping.models import Message


def test_ai_output_limit_is_cumulative() -> None:
    client = AIClient(Settings(ai={"max_output_chars": 1000}))

    total = client._count_output(0, "a" * 600)
    assert total == 600
    with pytest.raises(RuntimeError, match="1000 字符"):
        client._count_output(total, "b" * 401)


def test_stream_usage_is_recorded_even_in_usage_only_chunk(monkeypatch) -> None:
    requests = []

    class FakeSource:
        response = SimpleNamespace(raise_for_status=lambda: None)

        async def aiter_sse(self):
            yield SimpleNamespace(data=json.dumps({"choices": [{"delta": {"content": "结果"}}]}))
            yield SimpleNamespace(
                data=json.dumps(
                    {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 8}}
                )
            )
            yield SimpleNamespace(data="[DONE]")

    @asynccontextmanager
    async def fake_connect(_client, _method, _url, **kwargs):
        requests.append(kwargs["json"])
        yield FakeSource()

    monkeypatch.setattr(ai_module, "aconnect_sse", fake_connect)
    client = AIClient(Settings(ai={"enabled": True}))
    usage = []

    async def consume():
        return [
            chunk async for chunk in client._stream_chat({}, message_id=1, on_usage=usage.append)
        ]

    assert asyncio.run(consume()) == ["结果"]
    assert (usage[0].prompt_tokens, usage[0].completion_tokens) == (120, 8)
    assert requests[0]["stream_options"] == {"include_usage": True}


def test_analysis_usage_includes_invalid_json_retry() -> None:
    client = AIClient(Settings(ai={"enabled": True}))
    attempts = 0

    async def fake_stream(_payload, *, message_id, on_usage):
        nonlocal attempts
        attempts += 1
        on_usage(ai_module.TokenUsage(10, 5))
        if attempts == 1:
            yield "invalid"
        else:
            yield json.dumps(
                {
                    "title_zh": "提醒",
                    "summary_zh": "摘要",
                    "importance_score": 0.5,
                    "risk": {"score": 0.1},
                    "should_push": False,
                    "push_reason": "无需推送",
                }
            )

    client._stream_chat = fake_stream
    message = Message(
        id=1,
        subject="Subject",
        text_body="Body",
        sender_address="person@example.org",
        authentication_json="{}",
    )
    prompt = Prompt(system="System", user_template="{subject}", version="test")
    analyzed = asyncio.run(client.analyze(message, prompt))
    assert attempts == 2
    assert analyzed.usage == ai_module.TokenUsage(20, 10)
