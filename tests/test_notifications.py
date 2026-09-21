import asyncio
import json
from datetime import datetime

import httpx
import pytest
from pydantic import ValidationError

from inboxping.config import Settings, WeComAppChannel
from inboxping.models import Analysis, Message
from inboxping.notify.manager import NotificationManager
from inboxping.notify.wecom import (
    APP_TEXT_MAX_BYTES,
    WeComAppNotifier,
    delivery_parts,
    render_text,
    truncate_utf8,
)


def test_notification_channels_are_discriminated_and_validated() -> None:
    settings = Settings(
        notifications={
            "default_channels": ["app"],
            "channels": [
                {
                    "id": "app",
                    "type": "wecom_app",
                    "corp_id": "ww-test",
                    "corp_secret": "secret",
                    "agent_id": 1000001,
                    "to_user": ["@all"],
                },
                {
                    "id": "group",
                    "type": "wecom_webhook",
                    "enabled": False,
                    "webhook_url": "",
                },
            ],
        }
    )
    manager = NotificationManager(settings)
    assert manager.default_channel_ids == ["app"]
    assert list(manager.channels) == ["app"]

    with pytest.raises(ValidationError, match="不存在的通道"):
        Settings(notifications={"default_channels": ["missing"]})


def test_wecom_app_caches_token_lists_users_and_sends(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200, json={"errcode": 0, "access_token": "token", "expires_in": 7200}
            )
        if request.url.path.endswith("/user/simplelist"):
            return httpx.Response(
                200,
                json={
                    "errcode": 0,
                    "userlist": [{"userid": "zhang", "name": "张", "department": [1]}],
                },
            )
        if request.url.path.endswith("/message/send"):
            body = json.loads(request.content)
            assert body["touser"] == "@all"
            assert body["agentid"] == 1000001
            assert body["msgtype"] == "text"
            assert body["text"]["content"] == "test"
            return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})
        raise AssertionError(request.url)

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        return real_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    monkeypatch.setattr("inboxping.notify.wecom.httpx.AsyncClient", client_factory)
    notifier = WeComAppNotifier(
        WeComAppChannel(
            id="app",
            type="wecom_app",
            corp_id="ww-test",
            corp_secret="secret",
            agent_id=1000001,
            to_user=["@all"],
        )
    )

    async def run() -> None:
        users = await notifier.list_users()
        assert [(user.userid, user.name) for user in users] == [("zhang", "张")]
        await notifier.send_text("test")

    asyncio.run(run())
    assert sum(request.url.path.endswith("/gettoken") for request in requests) == 1


def test_email_notification_is_plain_and_contains_sent_time() -> None:
    message = Message(
        subject="Original subject",
        sender_name="Sender",
        sender_address="sender@example.com",
        sent_at=datetime(2026, 9, 20, 4, 34),
    )
    analysis = Analysis(
        title_zh="论文版权确认已完成",
        summary_zh="IEEE 已确认收到论文版权转让。",
        action_required=True,
        action_text="核对版权信息",
        deadline="2026-09-25",
    )
    content = render_text(message, analysis)
    assert content.splitlines()[0] == "论文版权确认已完成"
    assert "发件时间：2026-09-20 12:34:00 UTC+08:00" in content
    assert "风险" not in content
    assert "📬" not in content
    assert "截止时间：2026-09-25" in content
    assert "操作：" not in content
    assert "需处理：" not in content
    assert len(content.encode("utf-8")) <= APP_TEXT_MAX_BYTES


def test_utf8_limits_are_measured_in_bytes() -> None:
    shortened = truncate_utf8("中" * 1000, 100)
    assert shortened.endswith("…")
    assert len(shortened.encode("utf-8")) <= 100

    parts = delivery_parts("中" * 2000, APP_TEXT_MAX_BYTES)
    assert len(parts) > 1
    assert "".join(part.split("\n", 1)[1] for part in parts) == "中" * 2000
    assert all(len(part.encode("utf-8")) <= APP_TEXT_MAX_BYTES for part in parts)
