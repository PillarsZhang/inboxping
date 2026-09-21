from __future__ import annotations

import hashlib
import json
import poplib
import ssl
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from imapclient import IMAPClient
from loguru import logger

from inboxping.config import MailAccount, Settings
from inboxping.db import Database
from inboxping.models import AccountState, Event


@dataclass
class RawMessage:
    uid: int
    uid_validity: int
    body: bytes | None
    folder: str = "INBOX"
    size: int = 0


class Receiver(Protocol):
    def receive(self, *, limit: int, wait: bool) -> list[RawMessage]: ...

    def fetch_one(self, *, uid: int, uid_validity: int, folder: str) -> bytes: ...

    def acknowledge(self) -> None: ...

    def close(self) -> None: ...


class ImapReceiver:
    def __init__(self, account: MailAccount, db: Database, settings: Settings):
        assert account.imap is not None
        self.account = account
        self.config = account.imap
        self.db = db
        self.settings = settings
        self.client: IMAPClient | None = None
        self.uid_validity = 0

    def close(self) -> None:
        if self.client is not None:
            with suppress(Exception):
                self.client.logout()
            self.client = None

    def acknowledge(self) -> None:
        pass

    def _connect(self) -> IMAPClient:
        if self.client is not None:
            try:
                self.client.noop()
                return self.client
            except Exception:
                self.close()
        password = self.account.password.get_secret_value()
        if not password:
            raise RuntimeError(f"邮箱 {self.account.id} 未配置密码")
        ssl_context = ssl.create_default_context() if self.config.ssl else None
        client = IMAPClient(
            self.config.host,
            port=self.config.port,
            ssl=self.config.ssl,
            ssl_context=ssl_context,
            timeout=30,
        )
        try:
            client.login(self.account.username, password)
            info = client.select_folder(self.config.folder, readonly=True)
        except Exception:
            with suppress(Exception):
                client.logout()
            raise
        self.client = client
        self.uid_validity = int(info.get(b"UIDVALIDITY", info.get("UIDVALIDITY", 0)))
        with self.db.session() as session:
            state = session.get(AccountState, self.account.id)
            assert state is not None
            if state.uid_validity is not None and state.uid_validity != self.uid_validity:
                logger.bind(account=self.account.id).warning("UIDVALIDITY 已变化，将重新建立游标")
                state.last_uid = 0
            state.uid_validity = self.uid_validity
            state.status = "connected"
            state.last_connected_at = datetime.now(UTC)
            state.last_error = None
            session.add(
                Event(
                    level="info",
                    kind="mail_connected",
                    account_id=self.account.id,
                    message=(
                        f"邮箱连接成功，协议={self.account.protocol}，"
                        f"UIDVALIDITY={self.uid_validity}"
                    ),
                )
            )
        logger.bind(
            account=self.account.id,
            protocol=self.account.protocol,
            uid_validity=self.uid_validity,
        ).info("邮箱连接成功")
        return client

    def receive(self, *, limit: int, wait: bool) -> list[RawMessage]:
        client = self._connect()
        with self.db.session() as session:
            state = session.get(AccountState, self.account.id)
            assert state is not None
            last_uid = state.last_uid
        uids = [
            int(uid) for uid in client.search(["UID", f"{last_uid + 1}:*"]) if int(uid) > last_uid
        ]
        if len(uids) > limit:
            uids = uids[-limit:] if last_uid == 0 else uids[:limit]
        if uids:
            logger.bind(account=self.account.id, protocol=self.account.protocol).info(
                "发现 {} 封待拉取邮件，UID {}..{}", len(uids), min(uids), max(uids)
            )
        messages: list[RawMessage] = []
        if uids:
            size_data = client.fetch(uids, [b"RFC822.SIZE"])
            for uid in sorted(uids):
                metadata = size_data.get(uid, {})
                size = int(metadata.get(b"RFC822.SIZE", metadata.get("RFC822.SIZE", 0)))
                if size > self.settings.monitor.max_message_bytes:
                    messages.append(
                        RawMessage(
                            uid=uid,
                            uid_validity=self.uid_validity,
                            body=None,
                            folder=self.config.folder,
                            size=size,
                        )
                    )
                    continue
                data = client.fetch([uid], [b"BODY.PEEK[]"]).get(uid, {})
                body = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
                if isinstance(body, bytes):
                    actual_size = len(body)
                    if actual_size > self.settings.monitor.max_message_bytes:
                        body = None
                    messages.append(
                        RawMessage(
                            uid=uid,
                            uid_validity=self.uid_validity,
                            body=body,
                            folder=self.config.folder,
                            size=max(size, actual_size),
                        )
                    )
        if self.account.protocol == "imap_idle" and wait and not messages:
            client.idle()
            try:
                client.idle_check(timeout=self.settings.monitor.idle_timeout_seconds)
            finally:
                client.idle_done()
        return messages

    def fetch_one(self, *, uid: int, uid_validity: int, folder: str) -> bytes:
        if folder != self.config.folder:
            raise RuntimeError(f"邮件原文件夹 {folder} 与当前配置 {self.config.folder} 不一致")
        client = self._connect()
        if self.uid_validity != uid_validity:
            raise RuntimeError(
                f"UIDVALIDITY 已变化（原 {uid_validity}，当前 {self.uid_validity}），无法可靠补拉"
            )
        metadata = client.fetch([uid], [b"RFC822.SIZE"]).get(uid, {})
        size = int(metadata.get(b"RFC822.SIZE", metadata.get("RFC822.SIZE", 0)))
        if size > self.settings.monitor.max_message_bytes:
            raise RuntimeError(
                f"邮件大小 {size} 字节超过配置上限 "
                f"{self.settings.monitor.max_message_bytes} 字节"
            )
        fetched = client.fetch([uid], [b"BODY.PEEK[]"])
        data = fetched.get(uid, {})
        body = data.get(b"BODY[]") or data.get(b"BODY.PEEK[]")
        if not isinstance(body, bytes):
            raise RuntimeError(f"原邮箱中找不到 UID {uid}，邮件可能已移动或删除")
        if len(body) > self.settings.monitor.max_message_bytes:
            raise RuntimeError("邮件实际大小超过配置上限")
        return body


class Pop3Receiver:
    """Read-only POP3 receiver using UIDL as its durable message identity."""

    def __init__(self, account: MailAccount, db: Database, settings: Settings):
        assert account.pop3 is not None
        self.account = account
        self.config = account.pop3
        self.db = db
        self.settings = settings
        self.client: poplib.POP3 | poplib.POP3_SSL | None = None
        self.seen: set[str] = set()
        self._pending_seen: set[str] | None = None
        self._initialized = False
        with self.db.session() as session:
            state = session.get(AccountState, account.id)
            assert state is not None
            try:
                saved = json.loads(state.protocol_state_json)
            except (TypeError, ValueError):
                saved = {}
        if isinstance(saved, dict) and saved.get("kind") == "pop3":
            values = saved.get("seen_uidls", [])
            if isinstance(values, list):
                self.seen = {str(value) for value in values}
                self._initialized = True

    def close(self) -> None:
        if self.client is not None:
            with suppress(Exception):
                self.client.quit()
            self.client = None

    def acknowledge(self) -> None:
        if self._pending_seen is None:
            return
        self.seen = self._pending_seen
        with self.db.session() as session:
            state = session.get(AccountState, self.account.id)
            assert state is not None
            state.protocol_state_json = json.dumps(
                {"kind": "pop3", "seen_uidls": sorted(self.seen)}, ensure_ascii=False
            )
        self._pending_seen = None
        self._initialized = True

    def _connect(self) -> poplib.POP3 | poplib.POP3_SSL:
        if self.client is not None:
            try:
                self.client.noop()
                return self.client
            except Exception:
                self.close()
        password = self.account.password.get_secret_value()
        if not password:
            raise RuntimeError(f"邮箱 {self.account.id} 未配置密码")
        if self.config.ssl:
            client: poplib.POP3 | poplib.POP3_SSL = poplib.POP3_SSL(
                self.config.host,
                self.config.port,
                timeout=30,
                context=ssl.create_default_context(),
            )
        else:
            client = poplib.POP3(self.config.host, self.config.port, timeout=30)
        try:
            client.user(self.account.username)
            client.pass_(password)
        except Exception:
            with suppress(Exception):
                client.quit()
            raise
        self.client = client
        with self.db.session() as session:
            state = session.get(AccountState, self.account.id)
            assert state is not None
            state.status = "connected"
            state.last_connected_at = datetime.now(UTC)
            state.last_error = None
            session.add(
                Event(
                    level="info",
                    kind="mail_connected",
                    account_id=self.account.id,
                    message=f"邮箱连接成功，协议={self.account.protocol}",
                )
            )
        logger.bind(account=self.account.id, protocol=self.account.protocol).info("邮箱连接成功")
        return client

    @staticmethod
    def _uidl_entries(client: poplib.POP3 | poplib.POP3_SSL) -> list[tuple[int, str]]:
        try:
            _, lines, _ = client.uidl()
        except poplib.error_proto as exc:
            raise RuntimeError("POP3 服务器不支持 UIDL，无法可靠去重") from exc
        entries: list[tuple[int, str]] = []
        for line in lines:
            number, separator, uidl = line.partition(b" ")
            if separator and number.isdigit() and uidl:
                try:
                    decoded_uidl = uidl.decode("ascii")
                except UnicodeDecodeError as exc:
                    raise RuntimeError("POP3 服务器返回了非 ASCII UIDL") from exc
                entries.append((int(number), decoded_uidl))
        return entries

    def receive(self, *, limit: int, wait: bool) -> list[RawMessage]:
        del wait  # POP3 has no push command; MailMonitor controls the polling interval.
        client = self._connect()
        entries = self._uidl_entries(client)
        current_uidls = {uidl for _, uidl in entries}
        unseen = [(number, uidl) for number, uidl in entries if uidl not in self.seen]
        selected = unseen[-limit:] if not self._initialized else unseen[:limit]
        _, size_lines, _ = client.list()
        sizes = {}
        for line in size_lines:
            number, separator, size = line.partition(b" ")
            if separator and number.isdigit() and size.isdigit():
                sizes[int(number)] = int(size)
        if selected:
            logger.bind(account=self.account.id, protocol=self.account.protocol).info(
                "发现 {} 封待拉取邮件", len(selected)
            )
        messages: list[RawMessage] = []
        for number, uidl in selected:
            size = sizes.get(number, 0)
            uid = int.from_bytes(hashlib.sha256(uidl.encode()).digest()[:8], "big")
            uid &= 0x7FFF_FFFF_FFFF_FFFF
            if size > self.settings.monitor.max_message_bytes:
                messages.append(RawMessage(uid=uid, uid_validity=0, body=None, size=size))
                continue
            _, lines, _ = client.retr(number)
            body = b"\r\n".join(lines) + b"\r\n"
            actual_size = len(body)
            if actual_size > self.settings.monitor.max_message_bytes:
                body = None
            messages.append(
                RawMessage(uid=uid, uid_validity=0, body=body, size=max(size, actual_size))
            )
        if self._initialized:
            self._pending_seen = (self.seen & current_uidls) | {uidl for _, uidl in selected}
        else:
            # Match IMAP's initial behavior: import only the newest limit and skip older mail.
            self._pending_seen = current_uidls
        return messages

    def fetch_one(self, *, uid: int, uid_validity: int, folder: str) -> bytes:
        del folder
        if uid_validity != 0:
            raise RuntimeError("该邮件的 POP3 标识无效，无法可靠补拉")
        client = self._connect()
        for number, uidl in self._uidl_entries(client):
            candidate = int.from_bytes(hashlib.sha256(uidl.encode()).digest()[:8], "big")
            candidate &= 0x7FFF_FFFF_FFFF_FFFF
            if candidate == uid:
                size_response = client.list(number)
                size_parts = size_response.split()
                if (
                    len(size_parts) >= 3
                    and size_parts[-1].isdigit()
                    and int(size_parts[-1]) > self.settings.monitor.max_message_bytes
                ):
                    raise RuntimeError("邮件大小超过配置上限")
                _, lines, _ = client.retr(number)
                body = b"\r\n".join(lines) + b"\r\n"
                if len(body) > self.settings.monitor.max_message_bytes:
                    raise RuntimeError("邮件实际大小超过配置上限")
                return body
        raise RuntimeError("原邮箱中找不到该邮件，邮件可能已被删除")


def create_receiver(account: MailAccount, db: Database, settings: Settings) -> Receiver:
    if account.protocol == "pop3_poll":
        return Pop3Receiver(account, db, settings)
    return ImapReceiver(account, db, settings)
