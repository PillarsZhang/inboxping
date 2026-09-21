from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from inboxping.config import Settings
from inboxping.db import Database
from inboxping.mail.receivers import create_receiver
from inboxping.models import AccountState, Event, Message


@dataclass(frozen=True)
class FetchedEml:
    content: bytes
    subject: str


def fetch_message_eml(settings: Settings, db: Database, message_id: int) -> FetchedEml:
    """Fetch one message from its source mailbox without persisting the raw EML."""
    with db.session() as session:
        message = session.get(Message, message_id)
        if message is None:
            raise LookupError("邮件不存在")
        account = next(
            (item for item in settings.mail.accounts if item.id == message.account_id),
            None,
        )
        if account is None:
            raise RuntimeError(f"找不到邮件账户配置：{message.account_id}")
        if session.get(AccountState, account.id) is None:
            session.add(
                AccountState(
                    account_id=account.id,
                    display_name=account.name,
                    status="starting",
                )
            )
        account_id = message.account_id
        folder = message.folder
        uid = message.uid
        uid_validity = message.uid_validity
        subject = message.subject

    receiver = create_receiver(account, db, settings)
    try:
        raw_eml = receiver.fetch_one(uid=uid, uid_validity=uid_validity, folder=folder)
    finally:
        receiver.close()

    with db.session() as session:
        session.add(
            Event(
                level="info",
                kind="eml_downloaded",
                account_id=account_id,
                message=f"已从原邮箱读取 EML，本地ID={message_id}，UID={uid}",
            )
        )
    logger.bind(account=account_id, message_id=message_id, uid=uid).info(
        "已从原邮箱读取 EML（大小={} 字节）", len(raw_eml)
    )
    return FetchedEml(content=raw_eml, subject=subject)
