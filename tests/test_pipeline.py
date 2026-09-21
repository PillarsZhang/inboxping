from inboxping.config import Settings
from inboxping.models import Analysis
from inboxping.services.pipeline import Pipeline


def test_push_policy() -> None:
    pipeline = object.__new__(Pipeline)
    pipeline.settings = Settings(
        analysis={
            "push_importance_threshold": 0.7,
            "suppress_risk_levels": ["medium", "high"],
        }
    )
    analysis = Analysis(
        model="x",
        prompt_version="x",
        title_zh="摘要标题",
        summary_zh="x",
        category="other",
        importance=0.8,
        risk_level="low",
        risk_types_json="[]",
        risk_reason="",
        action_required=False,
        action_text="",
        push_recommended=False,
        raw_json="{}",
    )
    assert pipeline._should_notify(analysis)
    analysis.importance = 0.1
    analysis.risk_level = "high"
    analysis.push_recommended = True
    assert not pipeline._should_notify(analysis)
    assert "禁止推送" in pipeline._notification_suppression_reason(analysis)
    analysis.push_recommended = False
    analysis.risk_level = "low"
    assert not pipeline._should_notify(analysis)
    analysis.risk_level = "medium"
    analysis.category = "advertising"
    assert not pipeline._should_notify(analysis)
    analysis.category = "security"
    assert not pipeline._should_notify(analysis)
