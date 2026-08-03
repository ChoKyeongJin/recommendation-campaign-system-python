"""해결 영수증과 가정 — "왜 이 사양이 이렇게 됐는가"의 감사 기록.

요구(Requirement)에서 실행 사양(EventQuerySpec)으로 넘어오는 동안 애플리케이션은 값을
정규화하고, 기본값을 적용하고, 후보를 고르고, capability 를 확인한다. 그 판단이 기록되지
않으면 나중에 남는 것은 SQL 하나뿐이고, "왜 임계값이 5 인가"의 답이 사라진다.

영수증은 **표현을 바꾸지 않는다**. 사양은 이미 확정된 값만 담고, 영수증은 그 확정에 이르는
경로를 담는다 — 둘이 섞이면 실행 IR 안에 '미확정'이 다시 들어온다.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from query_pipeline.base import StrictModel
from query_pipeline.event_query.expressions import LiteralValue


class ReceiptAction(str, Enum):
    RESOLVED_REFERENCE = "resolved_reference"
    APPLIED_DEFAULT = "applied_default"
    SELECTED_CANDIDATE = "selected_candidate"
    NORMALIZED_VALUE = "normalized_value"
    POLICY_APPLIED = "policy_applied"
    CAPABILITY_CHECKED = "capability_checked"
    REWROTE_EXPRESSION = "rewrote_expression"


class ReceiptActor(str, Enum):
    USER = "user"
    LLM = "llm"
    APPLICATION = "application"
    POLICY = "policy"


class ResolutionReceipt(StrictModel):
    """해결 단계 하나의 기록. ``before``/``after`` 는 사람이 읽을 수 있는 스칼라 표기다."""

    id: str = Field(min_length=1)
    action: ReceiptAction

    input_path: str = Field(min_length=1)
    output_path: str | None = None

    before: LiteralValue | None = None
    after: LiteralValue | None = None

    reason: str = Field(min_length=1)
    actor: ReceiptActor

    confidence: float | None = Field(default=None, ge=0, le=1)
    policy_id: str | None = None
    schema_version: str | None = None
    timestamp: datetime


class Assumption(StrictModel):
    """사용자가 말하지 않았지만 실행에 필요해 애플리케이션이 채운 값."""

    id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    description: str = Field(min_length=1)
    value: LiteralValue
    confidence: float | None = Field(default=None, ge=0, le=1)


__all__ = [
    "Assumption",
    "ReceiptAction",
    "ReceiptActor",
    "ResolutionReceipt",
]
