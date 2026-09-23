from __future__ import annotations

from sqlalchemy.sql.elements import ColumnElement

from inboxping.models import Analysis


def should_push(analysis: Analysis | None) -> bool:
    return analysis is not None and analysis.should_push is True


def push_decision_expression() -> ColumnElement[bool]:
    return Analysis.should_push.is_(True)
