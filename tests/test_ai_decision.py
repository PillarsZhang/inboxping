import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from inboxping.ai.client import AnalyzedMessage, TokenUsage
from inboxping.ai.schema import AnalysisResult
from inboxping.config import Settings
from inboxping.db import Database
from inboxping.models import Analysis, Event, Message, Notification
from inboxping.services.maintenance import MaintenanceWorker
from inboxping.services.pipeline import Pipeline


def test_ai_decision_and_scores_are_required() -> None:
    payload = {
        "title_zh": "测试邮件",
        "summary_zh": "内容摘要",
        "importance_score": 0.9,
        "risk": {"score": 0.1, "reason": "未发现异常"},
        "should_push": False,
        "push_reason": "无需处理",
    }
    assert AnalysisResult.model_validate(payload).should_push is False
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate({**payload, "risk": {"score": 1.1}})
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(
            {key: value for key, value in payload.items() if key != "should_push"}
        )
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate({**payload, "push_reason": ""})


def test_failed_ai_analysis_never_creates_push_decision(tmp_path) -> None:
    settings = Settings(storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"})
    db = Database(settings)
    db.init()
    pipeline = Pipeline(settings, db)
    with db.session() as session:
        message = Message(account_id="mail", uid_validity=1, uid=1, subject="Important")
        session.add(message)
        session.flush()
        message_id = message.id

    async def failing_analyze(*_args):
        raise RuntimeError("AI unavailable")

    pipeline.ai.analyze = failing_analyze  # type: ignore[method-assign]
    asyncio.run(pipeline.process(message_id))

    with db.session() as session:
        message = session.get(Message, message_id)
        assert message.status == "analysis_failed"
        assert message.analysis.should_push is None
        assert message.analysis.importance_score is None
        assert message.analysis.risk_score is None
        notification = session.scalar(
            select(Notification).where(Notification.message_id == message_id)
        )
        assert notification is None
        assert session.scalar(select(Event).where(Event.kind == "analysis_failed")) is not None


def test_successful_ai_analysis_persists_scores_and_decision(tmp_path) -> None:
    settings = Settings(storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"})
    db = Database(settings)
    db.init()
    pipeline = Pipeline(settings, db)
    with db.session() as session:
        message = Message(account_id="mail", uid_validity=1, uid=1, subject="Subject")
        session.add(message)
        session.flush()
        message_id = message.id

    async def analyze(*_args):
        return AnalyzedMessage(
            AnalysisResult(
                title_zh="事项提醒",
                summary_zh="请尽快处理",
                category="administrative",
                importance_score=0.8,
                risk={"score": 0.1, "reason": "未发现异常"},
                should_push=True,
                push_reason="有明确期限",
            ),
            TokenUsage(prompt_tokens=123, completion_tokens=45),
        )

    pipeline.ai.analyze = analyze  # type: ignore[method-assign]
    asyncio.run(pipeline.process(message_id, notify=False))

    with db.session() as session:
        message = session.get(Message, message_id)
        assert message.status == "analyzed"
        assert message.analysis.importance_score == 0.8
        assert message.analysis.risk_score == 0.1
        assert message.analysis.should_push is True
        assert message.analysis.push_reason == "有明确期限"
        assert message.analysis.prompt_tokens == 123
        assert message.analysis.completion_tokens == 45


def test_maintenance_does_not_retry_after_ai_changes_to_no_push(tmp_path) -> None:
    settings = Settings(storage={"database_url": f"sqlite:///{tmp_path / 'test.db'}"})
    db = Database(settings)
    db.init()
    pipeline = Pipeline(settings, db)
    with db.session() as session:
        message = Message(account_id="mail", uid_validity=1, uid=1, subject="Subject")
        session.add(message)
        session.flush()
        session.add(
            Analysis(
                message_id=message.id,
                model="model",
                prompt_version="test",
                title_zh="标题",
                summary_zh="摘要",
                should_push=False,
                push_reason="不需要推送",
                raw_json="{}",
            )
        )
        session.add(
            Notification(message_id=message.id, channel="test", status="failed", attempts=1)
        )
        message_id = message.id

    async def unexpected_send(*_args, **_kwargs):
        raise AssertionError("should not retry")

    pipeline.send_notification = unexpected_send  # type: ignore[method-assign]
    asyncio.run(MaintenanceWorker(settings, db, pipeline).run_once())
    with db.session() as session:
        notification = session.scalar(
            select(Notification).where(Notification.message_id == message_id)
        )
        assert notification.attempts == 1
