from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Risk(BaseModel):
    score: float = Field(ge=0, le=1)
    types: list[str] = Field(default_factory=list)
    reason: str = ""


class AnalysisResult(BaseModel):
    title_zh: str = Field(min_length=1, max_length=80)
    summary_zh: str
    category: Literal[
        "academic", "administrative", "security", "personal", "newsletter", "advertising", "other"
    ] = "other"
    importance_score: float = Field(ge=0, le=1)
    risk: Risk
    action_required: bool = False
    action_text: str = ""
    deadline: str | None = None
    should_push: bool
    push_reason: str = Field(min_length=1)
