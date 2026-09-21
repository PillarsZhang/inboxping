from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement

from inboxping.config import Settings
from inboxping.models import Analysis


def suppression_reason(analysis: Analysis, settings: Settings) -> str | None:
    if analysis.risk_level in settings.analysis.suppress_risk_levels:
        return f"AI 判定风险等级为 {analysis.risk_level}，按配置禁止推送"
    return None


def notification_reasons(analysis: Analysis, settings: Settings) -> list[str]:
    if suppression_reason(analysis, settings):
        return []
    reasons: list[str] = []
    if analysis.push_recommended:
        reasons.append("AI 建议推送")
    if analysis.importance >= settings.analysis.push_importance_threshold:
        reasons.append(f"重要度 {analysis.importance:.2f}")
    return reasons


def should_push(analysis: Analysis | None, settings: Settings) -> bool:
    return analysis is not None and bool(notification_reasons(analysis, settings))


def push_decision_expression(settings: Settings) -> ColumnElement[bool]:
    return and_(
        Analysis.risk_level.not_in(settings.analysis.suppress_risk_levels),
        or_(
            Analysis.push_recommended.is_(True),
            Analysis.importance >= settings.analysis.push_importance_threshold,
        ),
    )
