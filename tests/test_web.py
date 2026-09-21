import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from inboxping.config import Settings
from inboxping.mail.source_fetch import FetchedEml
from inboxping.models import Analysis, Message, Notification
from inboxping.web.app import create_app


def test_dashboard_and_health(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        ai={"base_url": "https://api.openai.com/v1?region=test"},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "InboxPing" in dashboard.text
        assert "邮件首页" in dashboard.text
        assert 'href="/static/style.css?v=' in dashboard.text
        assert 'src="/static/dashboard.js?v=' in dashboard.text
        assert 'href="/static/vendor/bootstrap/bootstrap.min.css?v=5.3.8"' in dashboard.text
        assert (
            'href="/static/vendor/bootstrap-icons/bootstrap-icons.min.css?v=1.13.1"'
            in dashboard.text
        )
        assert 'src="/static/vendor/alpine/alpine.min.js?v=3.15.12"' in dashboard.text
        assert "http://testserver/static/" not in dashboard.text
        assert 'x-data="dashboardPage()"' in dashboard.text
        assert 'x-show.important="error"' in dashboard.text
        assert "每页" in dashboard.text
        assert "切换页码" in dashboard.text
        assert "匿名访问" in dashboard.text
        assert ">退出<" not in dashboard.text
        assert client.get("/static/vendor/bootstrap/bootstrap.min.css").status_code == 200
        assert client.get("/static/vendor/alpine/alpine.min.js").status_code == 200
        assert (
            client.get("/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2").status_code
            == 200
        )
        overview = client.get("/api/v1/overview").json()
        assert overview["stats"]["total"] == 0
        assert overview["services"] == {
            "ai_enabled": False,
            "ai_model": "gpt-4.1-mini",
            "ai_base_url": "https://api.openai.com/v1",
            "ai_response_format": "json_schema",
            "notifications_enabled": False,
            "notification_channels": [],
        }
        assert client.get("/api/v1/accounts").json() == []
        events = client.get("/api/v1/events").json()
        assert [event["kind"] for event in events] == ["service_started"]


def test_account_overview_includes_mailbox_address(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        mail={
            "accounts": [
                {
                    "id": "university",
                    "name": "学校邮箱",
                    "username": "student@example.edu.cn",
                    "protocol": "imap_poll",
                    "imap": {"host": "imap.example.edu.cn"},
                    "enabled": False,
                }
            ]
        },
    )
    with TestClient(create_app(settings)) as client:
        account = client.get("/api/v1/accounts").json()[0]
        assert account["username"] == "student@example.edu.cn"
        assert account["status"] == "disabled"


def test_web_shows_push_title_channel_and_delivery_status(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        mail={"accounts": []},
        notifications={
            "default_channels": ["wecom_app"],
            "channels": [
                {
                    "id": "wecom_app",
                    "type": "wecom_app",
                    "corp_id": "corp",
                    "corp_secret": "secret",
                    "agent_id": 1000001,
                    "to_user": ["demo-user"],
                }
            ],
        },
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            message = Message(
                account_id="university",
                uid_validity=1,
                uid=1,
                subject="原始主题",
                sender_address="sender@example.com",
                received_at=datetime.now(UTC),
                status="completed",
            )
            session.add(message)
            session.flush()
            session.add(
                Analysis(
                    message_id=message.id,
                    model="model",
                    prompt_version="test",
                    title_zh="课程安排有更新",
                    summary_zh="摘要",
                    push_recommended=True,
                    raw_json="{}",
                )
            )
            session.add(
                Notification(
                    message_id=message.id,
                    channel="wecom_app",
                    status="sent",
                    attempts=1,
                    sent_at=datetime.now(UTC),
                )
            )
            session.add(
                Message(
                    account_id="university",
                    uid_validity=1,
                    uid=2,
                    subject="未推送邮件",
                    sender_address="other@example.com",
                    received_at=datetime.now(UTC),
                    status="completed",
                )
            )
            message_id = message.id

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "/static/dashboard.js?v=" in dashboard.text

        messages = client.get("/api/v1/messages").json()
        assert messages["total"] == 2
        pushed = next(item for item in messages["items"] if item["id"] == message_id)
        assert pushed["analysis"]["title"] == "课程安排有更新"
        assert pushed["should_push"] is True

        push_only = client.get("/api/v1/messages?push=true")
        assert push_only.status_code == 200
        assert [item["id"] for item in push_only.json()["items"]] == [message_id]

        detail = client.get(f"/messages/{message_id}")
        assert "/static/message.js?v=" in detail.text
        assert f'x-data="messagePage({message_id})"' in detail.text
        assert "返回首页" in detail.text
        assert "下载 EML" in detail.text
        assert '@click="runTranslation()"' in detail.text
        assert ':disabled="translating"' in detail.text
        assert ':disabled="action !== null"' in detail.text
        assert ':disabled="action"' not in detail.text
        assert "←" not in detail.text
        api_detail = client.get(f"/api/v1/messages/{message_id}").json()
        assert api_detail["should_push"] is True
        assert api_detail["notifications"][0] == {
            "channel": "wecom_app",
            "channel_type": "企业微信应用",
            "target": "demo-user",
            "status": "sent",
            "attempts": 1,
            "sent_at": api_detail["notifications"][0]["sent_at"],
            "last_error": None,
        }


def test_message_translation_is_cached_and_can_be_refreshed(tmp_path, monkeypatch) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        ai={"enabled": True, "base_url": "https://api.example.com/v1"},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            message = Message(
                account_id="demo",
                uid_validity=1,
                uid=7,
                subject="Meeting update",
                sender_address="sender@example.com",
                text_body="The meeting starts at 10:00. <script>alert(1)</script>",
                received_at=datetime.now(UTC),
                status="completed",
            )
            session.add(message)
            session.flush()
            message_id = message.id

        translate_calls = 0

        async def stream_translation(message, prompt):
            nonlocal translate_calls
            translate_calls += 1
            yield "会议于"
            yield "上午十点开始。"

        client.app.state.pipeline.ai.stream_translation = stream_translation

        first = client.post(f"/api/v1/messages/{message_id}/translate")
        assert first.status_code == 200
        first_events = [json.loads(line) for line in first.text.splitlines()]
        assert [event["type"] for event in first_events] == [
            "start",
            "delta",
            "delta",
            "complete",
        ]
        assert first_events[-1]["translation"]["text"] == "会议于上午十点开始。"
        assert (
            "".join(event.get("text", "") for event in first_events if event["type"] == "delta")
            == "会议于上午十点开始。"
        )
        assert translate_calls == 1
        detail = client.get(f"/api/v1/messages/{message_id}").json()
        assert detail["translation"]["text"] == "会议于上午十点开始。"
        assert detail["translation"]["target_language"] == "zh-CN"

        original_export = client.get(f"/api/v1/messages/{message_id}/export/original.md")
        assert original_export.status_code == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in original_export.text
        assert "attachment;" in original_export.headers["content-disposition"]
        original_metadata = yaml.safe_load(original_export.text.split("---", 2)[1])
        assert original_metadata["content_type"] == "original"
        assert original_metadata["sender_name"] is None
        assert original_metadata["sender_address"] == "sender@example.com"
        assert original_metadata["received_at"].endswith("+08:00")
        assert all(not isinstance(value, dict) for value in original_metadata.values())
        translation_export = client.get(f"/api/v1/messages/{message_id}/export/translation.md")
        assert translation_export.status_code == 200
        assert "会议于上午十点开始。" in translation_export.text
        translation_metadata = yaml.safe_load(translation_export.text.split("---", 2)[1])
        assert translation_metadata["content_type"] == "translation"
        assert translation_metadata["target_language"] == "zh-CN"
        assert translation_metadata["translation_model"] == settings.ai.model
        assert all(not isinstance(value, dict) for value in translation_metadata.values())
        monkeypatch.setattr(
            "inboxping.web.app.fetch_message_eml",
            lambda settings, db, current_id: FetchedEml(
                content=b"Subject: Test\r\n\r\nBody",
                subject="Meeting update",
            ),
        )
        eml_export = client.get(f"/api/v1/messages/{message_id}/export/eml")
        assert eml_export.content == b"Subject: Test\r\n\r\nBody"

        assert client.post(f"/api/v1/messages/{message_id}/translate").status_code == 200
        assert translate_calls == 1
        assert client.post(f"/api/v1/messages/{message_id}/translate?force=true").status_code == 200
        assert translate_calls == 2

        events = client.get("/api/v1/events").json()
        assert sum(event["kind"] == "translation_completed" for event in events) == 2

        deleted = client.delete(f"/api/v1/messages/{message_id}/translation")
        assert deleted.status_code == 200
        assert client.get(f"/api/v1/messages/{message_id}").json()["translation"] is None
        events = client.get("/api/v1/events").json()
        assert any(event["kind"] == "translation_deleted" for event in events)


def test_streaming_translation_reports_failure_without_caching_partial_text(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        ai={"enabled": True, "base_url": "https://api.example.com/v1"},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            message = Message(
                account_id="demo",
                uid_validity=1,
                uid=8,
                subject="Long message",
                text_body="A long body",
                received_at=datetime.now(UTC),
                status="completed",
            )
            session.add(message)
            session.flush()
            message_id = message.id

        async def failing_stream(message, prompt):
            yield "部分译文"
            raise TimeoutError

        client.app.state.pipeline.ai.stream_translation = failing_stream
        response = client.post(f"/api/v1/messages/{message_id}/translate")
        events = [json.loads(line) for line in response.text.splitlines()]

        assert [event["type"] for event in events] == ["start", "delta", "error"]
        assert "TimeoutError" in events[-1]["message"]
        detail = client.get(f"/api/v1/messages/{message_id}").json()
        assert detail["translation"] is None
        stored_events = client.get("/api/v1/events").json()
        assert any(event["kind"] == "translation_failed" for event in stored_events)


def test_dashboard_orders_by_displayed_mail_time(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            session.add_all(
                [
                    Message(
                        account_id="account",
                        uid_validity=1,
                        uid=1,
                        subject="较新的发件时间",
                        sent_at=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
                        received_at=datetime(2026, 9, 20, 12, 1, tzinfo=UTC),
                        status="completed",
                    ),
                    Message(
                        account_id="account",
                        uid_validity=1,
                        uid=2,
                        subject="较旧的发件时间",
                        sent_at=datetime(2026, 9, 20, 11, 0, tzinfo=UTC),
                        received_at=datetime(2026, 9, 20, 13, 0, tzinfo=UTC),
                        status="completed",
                    ),
                ]
            )

        messages = client.get("/api/v1/messages").json()["items"]

        assert [item["subject"] for item in messages] == [
            "较新的发件时间",
            "较旧的发件时间",
        ]


def test_manual_notification_cannot_bypass_risk_suppression(tmp_path) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={"session_secret": "test-secret", "password_hash": ""},
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        with client.app.state.db.session() as session:
            message = Message(
                account_id="account",
                uid_validity=1,
                uid=1,
                subject="危险邮件",
                received_at=datetime.now(UTC),
                status="analyzed",
            )
            session.add(message)
            session.flush()
            session.add(
                Analysis(
                    message_id=message.id,
                    model="model",
                    prompt_version="test",
                    title_zh="危险邮件",
                    summary_zh="摘要",
                    risk_level="high",
                    push_recommended=True,
                    raw_json="{}",
                )
            )
            message_id = message.id

        response = client.post(f"/api/v1/messages/{message_id}/notify")
        assert response.status_code == 409
        assert "禁止推送" in response.json()["detail"]


def test_authored_web_assets_do_not_use_dash_or_arrow_placeholders() -> None:
    web_dir = Path("src/inboxping/web")
    paths = [*web_dir.glob("templates/*.html"), *web_dir.glob("static/*.*")]
    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert "—" not in content, path
        assert "←" not in content, path

    dashboard_script = (web_dir / "static/dashboard.js").read_text(encoding="utf-8")
    assert "scrollIntoView" not in dashboard_script


def test_login_is_long_lived_and_preserves_username_after_failure(tmp_path) -> None:
    digest = PasswordHash.recommended().hash("correct-password")
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={
            "username": "admin",
            "password_hash": digest,
            "session_secret": "test-secret",
        },
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        failed = client.post("/login", data={"username": "admin", "password": "wrong-password"})
        assert failed.status_code == 401
        assert 'value="admin"' in failed.text

        logged_in = client.post(
            "/login",
            data={"username": "admin", "password": "correct-password"},
            follow_redirects=False,
        )
        assert logged_in.status_code == 303
        assert "Max-Age=34560000" in logged_in.headers["set-cookie"]

        dashboard = client.get("/")
        assert "已登录：admin" in dashboard.text
        assert "匿名访问" not in dashboard.text
        assert "Max-Age=34560000" in dashboard.headers["set-cookie"]


def test_login_lifetime_can_be_configured(tmp_path) -> None:
    digest = PasswordHash.recommended().hash("correct-password")
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        web={
            "username": "admin",
            "password_hash": digest,
            "session_secret": "test-secret",
            "session_max_age_days": 30,
            "session_rolling": False,
        },
        mail={"accounts": []},
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/login",
            data={"username": "admin", "password": "correct-password"},
            follow_redirects=False,
        )
        assert "Max-Age=2592000" in response.headers["set-cookie"]
        assert "set-cookie" not in client.get("/").headers
