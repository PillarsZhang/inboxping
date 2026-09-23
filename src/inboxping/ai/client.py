from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from httpx_sse import aconnect_sse
from loguru import logger
from pydantic import ValidationError

from inboxping.ai.prompts import Prompt, TranslationPrompt
from inboxping.ai.schema import AnalysisResult
from inboxping.config import Settings
from inboxping.models import Message


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class AnalyzedMessage:
    result: AnalysisResult
    usage: TokenUsage | None


class AIClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.ai.enabled and self.settings.ai.base_url and self.settings.ai.model
        )

    def authentication_context(self, message: Message) -> str:
        try:
            authentication = json.loads(message.authentication_json or "{}")
        except json.JSONDecodeError:
            authentication = {}
        auth_text = ", ".join(
            f"{item.upper()}={str(authentication.get(item, 'unknown'))[:20]}"
            for item in ("spf", "dkim", "dmarc")
        )
        return auth_text

    def trust_context(self, message: Message) -> str:
        sender = message.sender_address.strip().lower()
        sender_domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
        links = re.findall(r"https?://[^\s<>\"']+", message.text_body, re.IGNORECASE)
        link_hosts: set[str] = set()
        for link in links:
            try:
                host = urlsplit(link.rstrip(".,;:!?)")).hostname
            except ValueError:
                continue
            if host:
                link_hosts.add(host.lower())
        matches: list[str] = []
        for entry in self.settings.trust.sender_addresses:
            if sender == entry.value:
                matches.append(self._trust_match("发件地址", entry.value, entry.note))
        for entry in self.settings.trust.sender_domains:
            if sender_domain == entry.value or sender_domain.endswith(f".{entry.value}"):
                matches.append(self._trust_match("发件域名", entry.value, entry.note))
        for entry in self.settings.trust.link_domains:
            if any(host == entry.value or host.endswith(f".{entry.value}") for host in link_hosts):
                matches.append(self._trust_match("链接域名", entry.value, entry.note))
        return "；".join(matches) if matches else "未命中本地信任参考名单"

    @staticmethod
    def _trust_match(kind: str, value: str, note: str | None) -> str:
        return f"{kind}={value}" + (f"（备注：{note}）" if note else "")

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
        self,
        payload: dict[str, object],
        *,
        message_id: int,
        on_usage: Callable[[TokenUsage], None] | None = None,
    ) -> AsyncIterator[str]:
        url = f"{self.settings.ai.base_url.rstrip('/')}/chat/completions"
        stream_payload = {**payload, "stream": True}
        if self.settings.ai.stream_usage:
            stream_payload["stream_options"] = {"include_usage": True}
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
                    usage = data.get("usage")
                    if on_usage and isinstance(usage, dict):
                        prompt_tokens = usage.get("prompt_tokens")
                        completion_tokens = usage.get("completion_tokens")
                        if (
                            type(prompt_tokens) is int
                            and prompt_tokens >= 0
                            and type(completion_tokens) is int
                            and completion_tokens >= 0
                        ):
                            on_usage(TokenUsage(prompt_tokens, completion_tokens))
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    content = choices[0]["delta"].get("content")
                except (
                    json.JSONDecodeError,
                    AttributeError,
                    KeyError,
                    IndexError,
                    TypeError,
                ) as exc:
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

    async def analyze(self, message: Message, prompt: Prompt) -> AnalyzedMessage:
        if not self.enabled:
            raise RuntimeError("AI 未启用：请检查 config.yaml 中的 ai.enabled")
        schema = AnalysisResult.model_json_schema()
        payload: dict[str, object] = {
            "model": self.settings.ai.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {
                    "role": "user",
                    "content": prompt.render(
                        message,
                        self.authentication_context(message),
                        self.trust_context(message),
                    ),
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
        request_usages: list[TokenUsage | None] = []
        for attempt in range(2):
            usage: TokenUsage | None = None

            def record_usage(value: TokenUsage) -> None:
                nonlocal usage
                usage = value

            chunks = [
                chunk
                async for chunk in self._stream_chat(
                    payload, message_id=message.id, on_usage=record_usage
                )
            ]
            request_usages.append(usage)
            content = "".join(chunks)
            try:
                if match := re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL):
                    content = match.group(1)
                result = AnalysisResult.model_validate(json.loads(content))
                total_usage = (
                    TokenUsage(
                        sum(item.prompt_tokens for item in request_usages if item),
                        sum(item.completion_tokens for item in request_usages if item),
                    )
                    if all(request_usages)
                    else None
                )
                return AnalyzedMessage(result, total_usage)
            except (json.JSONDecodeError, ValidationError, KeyError, TypeError) as exc:
                if attempt == 1:
                    raise
                logger.bind(message_id=message.id, model=self.settings.ai.model).warning(
                    "AI 返回的 JSON 无效，将自动重试一次: {}", type(exc).__name__
                )
        raise AssertionError("unreachable")

    async def stream_translation(
        self,
        message: Message,
        prompt: TranslationPrompt,
        *,
        on_usage: Callable[[TokenUsage], None] | None = None,
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
        async for chunk in self._stream_chat(payload, message_id=message.id, on_usage=on_usage):
            yield chunk
