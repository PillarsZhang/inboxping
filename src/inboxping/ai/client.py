from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator

import httpx
from httpx_sse import aconnect_sse
from loguru import logger
from pydantic import ValidationError

from inboxping.ai.prompts import Prompt, TranslationPrompt
from inboxping.ai.schema import AnalysisResult
from inboxping.config import Settings
from inboxping.models import Message


class AIClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.ai.enabled and self.settings.ai.base_url and self.settings.ai.model
        )

    def trust_context(self, message: Message) -> str:
        try:
            authentication = json.loads(message.authentication_json or "{}")
        except json.JSONDecodeError:
            authentication = {}
        sender = message.sender_address.strip().lower()
        domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
        sender_match = sender in {
            item.strip().lower() for item in self.settings.rules.trusted_senders
        }
        domain_match = any(
            domain == item.strip().lower() or domain.endswith(f".{item.strip().lower()}")
            for item in self.settings.rules.trusted_domains
            if item.strip()
        )
        any_auth_pass = any(authentication.get(item) == "pass" for item in ("spf", "dkim", "dmarc"))
        strong_auth_pass = any(authentication.get(item) == "pass" for item in ("dkim", "dmarc"))
        if sender_match and any_auth_pass:
            trust = "精确发件人匹配且认证通过"
        elif domain_match and strong_auth_pass:
            trust = "可信域名匹配且 DKIM/DMARC 通过"
        elif sender_match or domain_match:
            trust = "命中本地信任配置，但邮件认证未通过或缺失，不应直接信任"
        else:
            trust = "未命中本地信任配置"
        auth_text = ", ".join(
            f"{item.upper()}={authentication.get(item, 'unknown')}"
            for item in ("spf", "dkim", "dmarc")
        )
        return f"{auth_text}；{trust}"

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.settings.ai.connect_timeout_seconds,
            read=self.settings.ai.read_timeout_seconds,
            write=30,
            pool=self.settings.ai.connect_timeout_seconds,
        )

    def _headers(self) -> dict[str, str]:
        api_key = self.settings.ai.api_key.get_secret_value()
        return {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def _count_output(self, current: int, content: str) -> int:
        total = current + len(content)
        limit = self.settings.ai.max_output_chars
        if total > limit:
            raise RuntimeError(f"AI 输出超过配置上限（{limit} 字符）")
        return total

    async def _stream_chat(
        self, payload: dict[str, object], *, message_id: int
    ) -> AsyncIterator[str]:
        url = f"{self.settings.ai.base_url.rstrip('/')}/chat/completions"
        stream_payload = {**payload, "stream": True}
        output_chars = 0
        async with (
            asyncio.timeout(self.settings.ai.max_stream_seconds),
            httpx.AsyncClient(timeout=self._timeout()) as client,
            aconnect_sse(
                client,
                "POST",
                url,
                headers=self._headers(),
                json=stream_payload,
            ) as event_source,
        ):
            event_source.response.raise_for_status()
            async for event in event_source.aiter_sse():
                if event.data == "[DONE]":
                    break
                try:
                    data = json.loads(event.data)
                    content = data["choices"][0]["delta"].get("content")
                except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
                    logger.bind(message_id=message_id).warning(
                        "忽略无法解析的 AI 流事件: {}", type(exc).__name__
                    )
                    continue
                if isinstance(content, str) and content:
                    output_chars = self._count_output(output_chars, content)
                    yield content
                elif isinstance(content, list):
                    text = "".join(
                        item.get("text", "") for item in content if isinstance(item, dict)
                    )
                    if text:
                        output_chars = self._count_output(output_chars, text)
                        yield text

    async def analyze(self, message: Message, prompt: Prompt) -> AnalysisResult:
        if not self.enabled:
            raise RuntimeError("AI 未启用：请检查 config.yaml 中的 ai.enabled")
        schema = AnalysisResult.model_json_schema()
        payload: dict[str, object] = {
            "model": self.settings.ai.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {
                    "role": "user",
                    "content": prompt.render(message, self.trust_context(message)),
                },
            ],
        }
        if self.settings.ai.temperature is not None:
            payload["temperature"] = self.settings.ai.temperature
        if self.settings.ai.response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "email_analysis", "strict": True, "schema": schema},
            }
        elif self.settings.ai.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        logger.bind(message_id=message.id, model=self.settings.ai.model).debug("调用 AI 分析")
        for attempt in range(2):
            chunks = [chunk async for chunk in self._stream_chat(payload, message_id=message.id)]
            content = "".join(chunks)
            try:
                if match := re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL):
                    content = match.group(1)
                return AnalysisResult.model_validate(json.loads(content))
            except (json.JSONDecodeError, ValidationError, KeyError, TypeError) as exc:
                if attempt == 1:
                    raise
                logger.bind(message_id=message.id, model=self.settings.ai.model).warning(
                    "AI 返回的 JSON 无效，将自动重试一次: {}", type(exc).__name__
                )
        raise AssertionError("unreachable")

    async def stream_translation(
        self, message: Message, prompt: TranslationPrompt
    ) -> AsyncIterator[str]:
        if not self.enabled:
            raise RuntimeError("AI 未启用：请检查 config.yaml 中的 ai.enabled")
        payload: dict[str, object] = {
            "model": self.settings.ai.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.render(message)},
            ],
        }
        if self.settings.ai.temperature is not None:
            payload["temperature"] = self.settings.ai.temperature
        logger.bind(message_id=message.id, model=self.settings.ai.model).debug("调用 AI 流式翻译")
        async for chunk in self._stream_chat(payload, message_id=message.id):
            yield chunk
