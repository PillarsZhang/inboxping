from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from inboxping.config import Settings
from inboxping.db import Database
from inboxping.mail.source_fetch import fetch_message_eml, refresh_message_body
from inboxping.models import AccountState, Analysis, Event, Message, Notification, Translation


class FakeReceiver:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[tuple[int, int, str]] = []
        self.closed = False

    def fetch_one(self, *, uid: int, uid_validity: int, folder: str) -> bytes:
        self.calls.append((uid, uid_validity, folder))
        return self.content

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def stored_message(tmp_path):
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        mail={
            "accounts": [
                {
                    "id": "mail",
                    "name": "Mailbox",
                    "username": "user@example.com",
                    "password": "secret",
                    "protocol": "imap_poll",
                    "imap": {"host": "imap.example.com"},
                }
            ]
        },
    )
    db = Database(settings)
    db.init()
    with db.session() as session:
        message = Message(
            account_id="mail",
            folder="INBOX",
            uid_validity=42,
            uid=7,
            message_id="<archived@example.com>",
            subject="Archived message",
            text_body="Cached body",
            status="analyzed",
            received_at=datetime.now(UTC),
        )
        session.add(message)
        session.flush()
        message_id = message.id
        session.add_all(
            [
                AccountState(account_id="mail", display_name="Mailbox", last_uid=99),
                Analysis(
                    message_id=message_id,
                    model="old-model",
                    prompt_version="old",
                    summary_zh="保存的分析",
                    should_push=True,
                    raw_json="{}",
                ),
                Translation(
                    message_id=message_id,
                    model="old-model",
                    prompt_version="old",
                    translated_text="保存的译文",
                ),
                Notification(message_id=message_id, channel="demo", status="sent", attempts=1),
            ]
        )
    return settings, db, message_id


def test_fetch_message_eml_does_not_persist_content(stored_message, monkeypatch) -> None:
    settings, db, message_id = stored_message

    receiver = FakeReceiver(b"Subject: Archived\r\n\r\nBody")
    monkeypatch.setattr(
        "inboxping.mail.source_fetch.create_receiver",
        lambda account, database, current_settings: receiver,
    )

    result = fetch_message_eml(settings, db, message_id)
    assert result.content == b"Subject: Archived\r\n\r\nBody"
    assert result.subject == "Archived message"
    assert receiver.calls == [(7, 42, "INBOX")]
    assert receiver.closed is True
    with db.session() as session:
        assert session.query(Event).filter_by(kind="eml_downloaded").count() == 1


def test_refresh_body_preserves_metadata_analysis_translation_and_notifications(
    stored_message, monkeypatch
) -> None:
    settings, db, message_id = stored_message
    mail = EmailMessage()
    mail["Message-ID"] = "<archived@example.com>"
    mail.set_content(
        "<p>Hello <b>world</b>! <a href='https://example.org'>Details</a></p>", subtype="html"
    )
    receiver = FakeReceiver(mail.as_bytes())
    monkeypatch.setattr("inboxping.mail.source_fetch.create_receiver", lambda *args: receiver)

    refresh_message_body(settings, db, message_id)

    assert receiver.calls == [(7, 42, "INBOX")]
    assert receiver.closed
    with db.session() as session:
        message = session.get(Message, message_id)
        assert message.text_body == "Hello **world**! [Details](https://example.org)"
        assert message.subject == "Archived message"
        assert message.status == "analyzed"
        assert message.analysis.summary_zh == "保存的分析"
        assert message.translation.translated_text == "保存的译文"
        assert len(message.notifications) == 1
        assert message.notifications[0].status == "sent"
        assert session.get(AccountState, "mail").last_uid == 99
        assert session.query(Event).filter_by(kind="body_refreshed").count() == 1


@pytest.mark.parametrize("failure", ["fetch", "parse", "empty", "identity"])
def test_refresh_body_failure_preserves_cached_content(
    stored_message, monkeypatch, failure
) -> None:
    settings, db, message_id = stored_message
    mail = EmailMessage()
    mail["Message-ID"] = (
        "<other@example.com>" if failure == "identity" else "<archived@example.com>"
    )
    mail.set_content(" " if failure == "empty" else "Updated body")
    receiver = FakeReceiver(mail.as_bytes())
    monkeypatch.setattr("inboxping.mail.source_fetch.create_receiver", lambda *args: receiver)
    if failure == "fetch":

        def missing_message(**kwargs):
            raise RuntimeError("原邮件已被删除")

        monkeypatch.setattr(receiver, "fetch_one", missing_message)
    elif failure == "parse":

        def failed_conversion(*args):
            raise ValueError("conversion failed")

        monkeypatch.setattr("inboxping.mail.source_fetch.parse_message", failed_conversion)

    with pytest.raises(ValueError if failure == "parse" else RuntimeError):
        refresh_message_body(settings, db, message_id)

    assert receiver.closed
    with db.session() as session:
        message = session.get(Message, message_id)
        assert message.text_body == "Cached body"
        assert message.translation.translated_text == "保存的译文"
        assert message.analysis.summary_zh == "保存的分析"
        assert session.query(Event).filter_by(kind="body_refreshed").count() == 0
