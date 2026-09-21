from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from inboxping.config import WeComAppChannel, WeComWebhookChannel
from inboxping.models import Analysis, Message

WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"
TOKEN_ERROR_CODES = {40001, 40014, 42001}
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
APP_TEXT_MAX_BYTES = 2048
WEBHOOK_MARKDOWN_MAX_BYTES = 4096
MULTIPART_HEADER_RESERVE = 32


class WeComAPIError(RuntimeError):
    def __init__(self, operation: str, data: dict[str, object]):
        self.errcode = int(data.get("errcode", -1))
        detail = data.get("errmsg", data)
        super().__init__(f"企业微信{operation}失败 ({self.errcode}): {detail}")


def check_http_response(response: httpx.Response, operation: str) -> None:
    """Raise without including token-bearing request URLs in the exception."""
    if response.is_error:
        raise RuntimeError(f"企业微信{operation} HTTP 错误: {response.status_code}")


def truncate_utf8(text: str, max_bytes: int, *, suffix: str = "…") -> str:
    if max_bytes < 0:
        raise ValueError("max_bytes 不能为负数")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    suffix_bytes = suffix.encode("utf-8")
    if len(suffix_bytes) > max_bytes:
        suffix = ""
        suffix_bytes = b""
    budget = max_bytes - len(suffix_bytes)
    result: list[str] = []
    used = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if used + size > budget:
            break
        result.append(character)
        used += size
    return "".join(result).rstrip() + suffix


def split_utf8(text: str, max_bytes: int) -> list[str]:
    """Split text without breaking a Unicode character or exceeding max_bytes."""
    if max_bytes <= 0:
        raise ValueError("max_bytes 必须大于 0")
    if not text:
        return [""]
    parts: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if size > max_bytes:
            raise ValueError("单个字符超过分段字节限制")
        if current and current_bytes + size > max_bytes:
            parts.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += size
    if current:
        parts.append("".join(current))
    return parts


def delivery_parts(text: str, max_bytes: int) -> list[str]:
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    chunks = split_utf8(text, max_bytes - MULTIPART_HEADER_RESERVE)
    total = len(chunks)
    return [f"[{index}/{total}]\n{chunk}" for index, chunk in enumerate(chunks, 1)]


def render_text(message: Message, analysis: Analysis, max_bytes: int = APP_TEXT_MAX_BYTES) -> str:
    sender = message.sender_name or message.sender_address
    sent_at = message.sent_at or message.received_at
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    sent_at = sent_at.astimezone(BEIJING_TZ)
    title = analysis.title_zh.strip() or analysis.summary_zh.split("。", 1)[0]
    lines = [
        truncate_utf8(title, 240),
        f"原主题：{truncate_utf8(message.subject, 400)}",
        "来自：" + truncate_utf8(f"{sender} <{message.sender_address}>", 300),
        f"发件时间：{sent_at:%Y-%m-%d %H:%M:%S} UTC+08:00",
    ]
    if analysis.deadline:
        lines.append(f"截止时间：{truncate_utf8(analysis.deadline, 100)}")
    prefix = "\n".join(lines) + "\n摘要："
    summary_budget = max(max_bytes - len(prefix.encode("utf-8")), 0)
    return prefix + truncate_utf8(analysis.summary_zh, summary_budget)


class WeComWebhookNotifier:
    def __init__(self, config: WeComWebhookChannel):
        self.id = config.id
        self.type = config.type
        self.webhook_url = config.webhook_url.get_secret_value()

    async def send(self, message: Message, analysis: Analysis) -> None:
        content = render_text(message, analysis, WEBHOOK_MARKDOWN_MAX_BYTES)
        logger.bind(message_id=message.id, channel=self.id).info("推送内容:\n{}", content)
        await self._post(content)

    async def send_text(self, content: str) -> None:
        logger.bind(channel=self.id).info("推送内容:\n{}", content)
        parts = delivery_parts(content, WEBHOOK_MARKDOWN_MAX_BYTES)
        if len(parts) > 1:
            logger.bind(channel=self.id).info("长文本分段发送，共 {} 条", len(parts))
        for index, part in enumerate(parts):
            await self._post(part)
            if index + 1 < len(parts):
                await asyncio.sleep(0.2)

    async def _post(self, content: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                self.webhook_url,
                json={"msgtype": "markdown", "markdown": {"content": content}},
            )
            check_http_response(response, "机器人消息发送")
            data = response.json()
        if data.get("errcode") != 0:
            raise WeComAPIError("机器人消息发送", data)


@dataclass(frozen=True)
class WeComUser:
    userid: str
    name: str
    department: list[int]


class WeComAppNotifier:
    def __init__(self, config: WeComAppChannel):
        self.id = config.id
        self.type = config.type
        self.corp_id = config.corp_id
        self.corp_secret = config.corp_secret.get_secret_value()
        self.agent_id = config.agent_id
        self.to_user = config.to_user
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def send(self, message: Message, analysis: Analysis) -> None:
        content = render_text(message, analysis, APP_TEXT_MAX_BYTES)
        logger.bind(message_id=message.id, channel=self.id).info("推送内容:\n{}", content)
        await self._send_text(content)

    async def send_text(self, content: str) -> None:
        logger.bind(channel=self.id).info("推送内容:\n{}", content)
        parts = delivery_parts(content, APP_TEXT_MAX_BYTES)
        if len(parts) > 1:
            logger.bind(channel=self.id).info("长文本分段发送，共 {} 条", len(parts))
        for index, part in enumerate(parts):
            await self._send_text(part)
            if index + 1 < len(parts):
                await asyncio.sleep(0.2)

    async def list_users(self) -> list[WeComUser]:
        data = await self._api_request(
            "GET",
            "/user/simplelist",
            operation="读取应用可见成员",
            params={"department_id": 1, "fetch_child": 1},
        )
        return [
            WeComUser(
                userid=str(item["userid"]),
                name=str(item.get("name", "")),
                department=[int(value) for value in item.get("department", [])],
            )
            for item in data.get("userlist", [])
        ]

    async def _send_text(self, content: str) -> None:
        await self._api_request(
            "POST",
            "/message/send",
            operation="应用消息发送",
            json={
                "touser": "|".join(self.to_user),
                "msgtype": "text",
                "agentid": self.agent_id,
                "text": {"content": content},
                "safe": 0,
                "enable_duplicate_check": 1,
                "duplicate_check_interval": 1800,
            },
        )

    async def _access_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expires_at:
            return self._token
        async with self._token_lock:
            if not force and self._token and time.monotonic() < self._token_expires_at:
                return self._token
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{WECOM_API}/gettoken",
                    params={"corpid": self.corp_id, "corpsecret": self.corp_secret},
                )
                check_http_response(response, "Access Token 获取")
                data = response.json()
            if data.get("errcode") != 0:
                raise WeComAPIError("Access Token 获取", data)
            self._token = str(data["access_token"])
            expires_in = max(int(data.get("expires_in", 7200)) - 60, 1)
            self._token_expires_at = time.monotonic() + expires_in
            return self._token

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, object] | None = None,
        json: dict[str, object] | None = None,
    ) -> dict[str, object]:
        for token_attempt in range(2):
            token = await self._access_token(force=token_attempt > 0)
            request_params = {**(params or {}), "access_token": token}
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.request(
                    method, f"{WECOM_API}{path}", params=request_params, json=json
                )
                check_http_response(response, operation)
                data = response.json()
            if data.get("errcode") == 0:
                return data
            error = WeComAPIError(operation, data)
            if error.errcode not in TOKEN_ERROR_CODES or token_attempt == 1:
                raise error
        raise AssertionError("unreachable")
