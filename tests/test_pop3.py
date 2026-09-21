from inboxping.config import Settings
from inboxping.db import Database
from inboxping.mail.receivers import Pop3Receiver
from inboxping.models import AccountState


class FakePop3:
    def __init__(self) -> None:
        self.messages = [
            ("one", b"Subject: one\r\n\r\none"),
            ("two", b"Subject: two\r\n\r\ntwo"),
            ("three", b"Subject: three\r\n\r\nthree"),
        ]
        self.retrieved: list[int] = []
        self.closed = False

    def user(self, _username: str) -> bytes:
        return b"+OK"

    def pass_(self, _password: str) -> bytes:
        return b"+OK"

    def noop(self) -> bytes:
        return b"+OK"

    def uidl(self) -> tuple[bytes, list[bytes], int]:
        lines = [f"{index} {uidl}".encode() for index, (uidl, _) in enumerate(self.messages, 1)]
        return b"+OK", lines, sum(map(len, lines))

    def retr(self, number: int) -> tuple[bytes, list[bytes], int]:
        self.retrieved.append(number)
        body = self.messages[number - 1][1]
        lines = body.split(b"\r\n")
        return b"+OK", lines, len(body)

    def quit(self) -> bytes:
        self.closed = True
        return b"+OK"


def test_pop3_uses_uidl_and_only_fetches_new_mail(tmp_path, monkeypatch) -> None:
    settings = Settings(
        storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"},
        mail={
            "accounts": [
                {
                    "id": "pop",
                    "name": "POP",
                    "username": "user@example.com",
                    "password": "secret",
                    "protocol": "pop3_poll",
                    "pop3": {"host": "pop.example.com"},
                }
            ]
        },
    )
    db = Database(settings)
    db.init()
    with db.session() as session:
        session.add(AccountState(account_id="pop", display_name="POP"))

    fake = FakePop3()
    monkeypatch.setattr("inboxping.mail.receivers.poplib.POP3_SSL", lambda *args, **kwargs: fake)
    receiver = Pop3Receiver(settings.mail.accounts[0], db, settings)

    initial = receiver.receive(limit=2, wait=False)
    assert [message.body for message in initial] == [
        b"Subject: two\r\n\r\ntwo\r\n",
        b"Subject: three\r\n\r\nthree\r\n",
    ]
    receiver.acknowledge()
    assert fake.retrieved == [2, 3]

    restored = receiver.fetch_one(
        uid=initial[0].uid,
        uid_validity=initial[0].uid_validity,
        folder="INBOX",
    )
    assert restored == b"Subject: two\r\n\r\ntwo\r\n"

    fake.messages.append(("four", b"Subject: four\r\n\r\nfour"))
    added = receiver.receive(limit=2, wait=False)
    assert len(added) == 1
    assert added[0].body.startswith(b"Subject: four")
    assert fake.retrieved == [2, 3, 2, 4]
    receiver.acknowledge()
    receiver.close()
    assert fake.closed
