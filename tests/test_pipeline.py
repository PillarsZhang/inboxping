import asyncio

import pytest
from sqlalchemy import select

from inboxping.config import Settings
from inboxping.db import Database
from inboxping.models import Analysis, Message
from inboxping.services.pipeline import NotificationSuppressedError, Pipeline
from inboxping.services.push_policy import push_decision_expression, should_push


def test_ai_decision_is_the_only_push_condition(tmp_path) -> None:
    settings = Settings(storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"})
    db = Database(settings)
    db.init()
    pipeline = Pipeline(settings, db)
    with db.session() as session:
        message = Message(account_id="mail", uid_validity=1, uid=1, subject="Subject")
        session.add(message)
        session.flush()
        analysis = Analysis(
            message_id=message.id,
            model="model",
            prompt_version="test",
            title_zh="测试",
            summary_zh="摘要",
            importance_score=0.99,
            risk_score=0.01,
            should_push=False,
            push_reason="无需即时打扰",
            raw_json="{}",
        )
        session.add(analysis)
        message_id = message.id

    with db.session() as session:
        analysis = session.scalar(select(Analysis).where(Analysis.message_id == message_id))
        assert not should_push(analysis)
        assert session.scalar(select(Analysis).where(push_decision_expression())) is None

    with pytest.raises(NotificationSuppressedError, match="无需即时打扰"):
        asyncio.run(pipeline.send_notification(message_id, force=True))

    with db.session() as session:
        analysis = session.scalar(select(Analysis).where(Analysis.message_id == message_id))
        analysis.importance_score = 0.05
        analysis.risk_score = 0.95
        analysis.should_push = True
        analysis.push_reason = "AI 仍决定推送"

    with db.session() as session:
        analysis = session.scalar(select(Analysis).where(Analysis.message_id == message_id))
        assert should_push(analysis)
        assert session.scalar(select(Analysis).where(push_decision_expression())) is not None
