from fastapi.testclient import TestClient

from inboxping.demo import build_demo_settings, seed_demo_data
from inboxping.web.app import create_app


def test_demo_is_seeded_and_blocks_external_actions(tmp_path) -> None:
    settings = build_demo_settings(tmp_path / "demo.db")
    seed_demo_data(settings)

    assert settings.web.host == "127.0.0.1"
    assert all(
        account.username.endswith(("example.edu", "example.com"))
        for account in settings.mail.accounts
    )

    with TestClient(create_app(settings, demo_mode=True)) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "演示模式" in dashboard.text

        overview = client.get("/api/v1/overview").json()
        assert overview["stats"]["total"] == 3
        assert overview["services"]["ai_enabled"] is True
        assert overview["services"]["notifications_enabled"] is True
        assert [account["status"] for account in overview["accounts"]] == [
            "connected",
            "connected",
        ]
        assert [event["kind"] for event in overview["events"]] == ["service_started"]

        messages = client.get("/api/v1/messages").json()["items"]
        assert len(messages) == 3
        assert {message["analysis"]["category"] for message in messages} == {
            "academic",
            "malware",
            "advertising",
        }
        decisions = {
            message["analysis"]["category"]: message["should_push"] for message in messages
        }
        assert decisions == {"academic": True, "malware": False, "advertising": False}
        assert all("example" in message["sender_address"] for message in messages)
        assert client.post(f"/api/v1/messages/{messages[0]['id']}/reanalyze").status_code == 403
        assert client.post(f"/api/v1/messages/{messages[0]['id']}/notify").status_code == 403
        assert client.post(f"/api/v1/messages/{messages[0]['id']}/translate").status_code == 403
