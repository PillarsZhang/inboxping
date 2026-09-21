from datetime import UTC, datetime

from inboxping.config import Settings
from inboxping.db import Database
from inboxping.mail.source_fetch import fetch_message_eml
from inboxping.models import Event, Message


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


def test_fetch_message_eml_does_not_persist_content(tmp_path, monkeypatch) -> None:
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
            subject="Archived message",
            received_at=datetime.now(UTC),
        )
        session.add(message)
        session.flush()
        message_id = message.id

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
