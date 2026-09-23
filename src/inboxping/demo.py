from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from inboxping.config import Settings
from inboxping.db import Database
from inboxping.models import AccountState, Analysis, Message, Notification


@dataclass(frozen=True)
class DemoMail:
    account: str
    sender_name: str
    sender_address: str
    subject: str
    title: str
    summary: str
    category: str
    importance_score: float
    risk_score: float = 0.05
    action: str = ""
    deadline: str | None = None
    push: bool = False


DEMO_MAILS = (
    DemoMail(
        "campus_mail",
        "教务办公室",
        "registrar@example.edu",
        "下学期课程选课时间安排",
        "下学期选课将于周三开放",
        "选课系统将在本周三上午开放，请在规定时间内完成课程确认，并提前检查培养方案中的必修课程。",
        "academic",
        0.92,
        action="登录教务系统确认选课计划",
        deadline="本周五 18:00",
        push=True,
    ),
    DemoMail(
        "personal_mail",
        "未知文件投递",
        "delivery@untrusted.example",
        "重要文件查收通知",
        "检测到携带恶意附件的病毒邮件",
        "邮件伪装成文件投递通知，附件类型和发件来源异常，具有恶意程序传播特征，已禁止推送。",
        "malware",
        0.98,
        risk_score=0.98,
        action="不要打开附件，删除邮件并通过安全渠道报告",
        push=False,
    ),
    DemoMail(
        "campus_mail",
        "课程推广中心",
        "offers@example.net",
        "限时课程优惠活动",
        "在线课程促销广告",
        "这是一封批量发送的课程促销邮件，主要内容为折扣和限时优惠，无需处理。",
        "advertising",
        0.16,
    ),
)


def build_demo_settings(database_path: Path) -> Settings:
    """Build an isolated configuration that is never loaded from config.yaml."""
    return Settings(
        web={"host": "127.0.0.1", "port": 8001, "session_secret": "demo-only"},
        ai={
            "enabled": True,
            "base_url": "https://api.example.com/v1",
            "model": "demo-language-model",
            "response_format": "json_object",
        },
        notifications={
            "default_channels": ["demo_app"],
            "channels": [
                {
                    "id": "demo_app",
                    "type": "wecom_app",
                    "corp_id": "demo-corp",
                    "corp_secret": "demo-secret",
                    "agent_id": 1000001,
                    "to_user": ["demo-user"],
                }
            ],
        },
        mail={
            "accounts": [
                {
                    "id": "campus_mail",
                    "name": "校园邮箱",
                    "username": "student@example.edu",
                    "password": "demo-only",
                    "protocol": "imap_poll",
                    "imap": {"host": "imap.example.edu"},
                },
                {
                    "id": "personal_mail",
                    "name": "个人邮箱",
                    "username": "hello@example.com",
                    "password": "demo-only",
                    "protocol": "imap_idle",
                    "imap": {"host": "imap.example.com"},
                },
            ]
        },
        storage={"database_url": f"sqlite:///{database_path}"},
        logging={"level": "INFO"},
    )


def seed_demo_data(settings: Settings) -> None:
    db = Database(settings)
    db.init()
    now = datetime.now(UTC).replace(microsecond=0)
    with db.session() as session:
        for account_id, name, last_uid, minutes_ago in (
            ("campus_mail", "校园邮箱", 2841, 2),
            ("personal_mail", "个人邮箱", 917, 4),
        ):
            connected_at = now - timedelta(minutes=minutes_ago)
            session.add(
                AccountState(
                    account_id=account_id,
                    display_name=name,
                    status="connected",
                    uid_validity=1,
                    last_uid=last_uid,
                    last_connected_at=connected_at,
                    last_received_at=connected_at - timedelta(minutes=3),
                    protocol_state_json="{}",
                )
            )

        for index, sample in enumerate(DEMO_MAILS, start=1):
            sent_at = now - timedelta(minutes=index * 37)
            recipient = (
                "student@example.edu" if sample.account == "campus_mail" else "hello@example.com"
            )
            message = Message(
                account_id=sample.account,
                uid_validity=1,
                uid=3000 - index,
                message_id=f"<demo-{index}@example.org>",
                subject=sample.subject,
                sender_name=sample.sender_name,
                sender_address=sample.sender_address,
                recipients=recipient,
                sent_at=sent_at,
                received_at=sent_at + timedelta(minutes=1),
                text_body=sample.summary,
                authentication_json=json.dumps(
                    {"spf": "pass", "dkim": "pass", "dmarc": "pass"}
                    if sample.risk_score < 0.5
                    else {"spf": "fail", "dkim": "unknown", "dmarc": "fail"}
                ),
                status="completed",
            )
            session.add(message)
            session.flush()
            session.add(
                Analysis(
                    message_id=message.id,
                    model="demo-language-model",
                    prompt_version="demo",
                    title_zh=sample.title,
                    summary_zh=sample.summary,
                    category=sample.category,
                    importance_score=sample.importance_score,
                    risk_score=sample.risk_score,
                    risk_types_json='["malware"]' if sample.risk_score > 0.9 else "[]",
                    risk_reason=(
                        "发件来源与附件类型异常，疑似传播恶意程序。"
                        if sample.risk_score > 0.9
                        else "发件认证通过，内容与发件来源一致。"
                    ),
                    action_required=bool(sample.action),
                    action_text=sample.action,
                    deadline=sample.deadline,
                    should_push=sample.push,
                    push_reason=("需要及时确认选课安排" if sample.push else "无需即时通知"),
                    raw_json="{}",
                )
            )
            if sample.push:
                session.add(
                    Notification(
                        message_id=message.id,
                        channel="demo_app",
                        status="sent",
                        attempts=1,
                        sent_at=sent_at + timedelta(minutes=2),
                    )
                )
