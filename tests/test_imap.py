import pytest

from inboxping.config import Settings
from inboxping.db import Database
from inboxping.mail.monitor import MailMonitor
from inboxping.mail.receivers import ImapReceiver
from inboxping.models import AccountState, Event, Message


class FakeImap:
    def __init__(self) -> None:
        self.fetches: list[tuple[tuple[int, ...], tuple[bytes, ...]]] = []
        self.bodies = {1: b"small", 2: b"x" * 2048}

    def noop(self) -> None:
        pass

    def search(self, _criteria) -> list[int]:
        return [1, 2]

    def fetch(self, uids, fields) -> dict[int, dict[bytes, object]]:
        uid_values = tuple(int(uid) for uid in uids)
        field_values = tuple(fields)
        self.fetches.append((uid_values, field_values))
        if fields == [b"RFC822.SIZE"]:
            return {uid: {b"RFC822.SIZE": len(self.bodies[uid])} for uid in uid_values}
        return {uid: {b"BODY[]": self.bodies[uid]} for uid in uid_values}


def build_receiver(tmp_path) -> tuple[Settings, Database, ImapReceiver, FakeImap]:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        monitor={"max_message_bytes": 1024},
        mail={
            "accounts": [
                {
                    "id": "imap",
                    "name": "IMAP",
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
        session.add(AccountState(account_id="imap", display_name="IMAP"))
    receiver = ImapReceiver(settings.mail.accounts[0], db, settings)
    fake = FakeImap()
    receiver.client = fake  # type: ignore[assignment]
    receiver.uid_validity = 42
    return settings, db, receiver, fake


def test_imap_checks_size_before_fetching_body(tmp_path) -> None:
    _, _, receiver, fake = build_receiver(tmp_path)

    messages = receiver.receive(limit=10, wait=False)

    assert messages[0].body == b"small"
    assert messages[1].body is None
    assert messages[1].size == 2048
    body_fetches = [uids for uids, fields in fake.fetches if fields == (b"BODY.PEEK[]",)]
    assert body_fetches == [(1,)]


def test_imap_eml_download_rejects_oversized_message_before_body_fetch(tmp_path) -> None:
    _, _, receiver, fake = build_receiver(tmp_path)

    with pytest.raises(RuntimeError, match="超过配置上限"):
        receiver.fetch_one(uid=2, uid_validity=42, folder="INBOX")

    assert not any(fields == (b"BODY.PEEK[]",) for _, fields in fake.fetches)


def test_oversized_message_is_skipped_and_cursor_advances(tmp_path) -> None:
    settings, db, receiver, _ = build_receiver(tmp_path)
    monitor = MailMonitor(settings, db, object())  # type: ignore[arg-type]
    messages = receiver.receive(limit=10, wait=False)

    assert monitor._save_message(settings.mail.accounts[0], messages[0]) is not None
    assert monitor._save_message(settings.mail.accounts[0], messages[1]) is None

    with db.session() as session:
        assert session.get(AccountState, "imap").last_uid == 2
        assert session.query(Message).count() == 1
        assert session.query(Event).filter_by(kind="mail_skipped").count() == 1
