from pathlib import Path

import pytest
from pydantic import ValidationError

from inboxping.config import LOG_LEVEL_ENV, Settings, load_settings
from inboxping.db import Database
from inboxping.models import AccountState, Event


def test_example_config_is_valid_and_generic() -> None:
    settings = load_settings(Path("config.example.yaml"))
    assert settings.ai.model == "deepseek-flash"
    assert settings.ai.response_format == "json_object"
    assert settings.ai.temperature is None
    assert [account.id for account in settings.mail.accounts] == ["university", "legacy_pop3"]
    assert settings.mail.accounts[0].imap.host == "imap.example.edu"
    assert settings.mail.accounts[0].protocol == "imap_poll"
    assert settings.mail.accounts[0].pop3 is None
    assert settings.mail.accounts[1].protocol == "pop3_poll"
    assert [account.username for account in settings.mail.accounts] == [
        "student@example.edu",
        "user@example.com",
    ]
    assert settings.notifications.default_channels == []
    assert settings.notifications.channels[0].type == "wecom_app"
    assert settings.web.session_max_age_days == 400
    assert settings.web.session_rolling is True
    assert settings.ai.connect_timeout_seconds == 10
    assert settings.ai.read_timeout_seconds == 90
    assert settings.ai.max_stream_seconds == 600
    assert settings.ai.max_output_chars == 100_000
    assert settings.monitor.poll_interval_seconds == 60
    assert settings.monitor.max_message_bytes == 25 * 1024 * 1024
    assert settings.trust.sender_addresses[0].note == "曾通过独立渠道确认的通知地址"
    assert settings.trust.link_domains[0].note is None


@pytest.mark.parametrize("days", [0, 401])
def test_session_lifetime_must_fit_browser_cookie_limit(days: int) -> None:
    with pytest.raises(ValidationError, match="session_max_age_days"):
        Settings(web={"session_max_age_days": days})


def test_log_level_environment_override(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("logging:\n  level: INFO\n", encoding="utf-8")
    monkeypatch.setenv(LOG_LEVEL_ENV, "trace")

    assert load_settings(path).logging.level == "TRACE"


def test_unknown_configuration_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("ai:\n  enabled: true\n  typo: value\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="typo"):
        load_settings(path)


def test_protocol_configuration_is_mutually_exclusive() -> None:
    common = {
        "id": "test",
        "name": "Test",
        "username": "user@example.com",
        "password": "secret",
    }
    with pytest.raises(ValidationError, match="必须且只能配置 pop3"):
        Settings(
            mail={
                "accounts": [
                    {
                        **common,
                        "protocol": "pop3_poll",
                        "pop3": {"host": "pop.example.com"},
                        "imap": {"host": "imap.example.com"},
                    }
                ]
            }
        )
    with pytest.raises(ValidationError, match="必须且只能配置 imap"):
        Settings(mail={"accounts": [{**common, "protocol": "imap_idle"}]})


def test_account_ids_must_be_unique() -> None:
    account = {
        "id": "same",
        "name": "Test",
        "username": "user@example.com",
        "password": "secret",
        "protocol": "imap_poll",
        "imap": {"host": "imap.example.com"},
    }
    with pytest.raises(ValidationError, match="id 不能重复"):
        Settings(mail={"accounts": [account, account]})


def test_clear_all_data_preserves_schema(tmp_path: Path) -> None:
    settings = Settings(storage={"database_url": f"sqlite:///{tmp_path / 'clear.db'}"})
    db = Database(settings)
    db.init()
    with db.session() as session:
        session.add(AccountState(account_id="test", display_name="Test"))
        session.add(Event(level="info", kind="test", message="event"))

    counts = db.clear_all_data()
    assert counts["account_states"] == 1
    assert counts["events"] == 1
    with db.session() as session:
        assert session.get(AccountState, "test") is None
