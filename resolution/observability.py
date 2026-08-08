"""Resolution 의 **관측** — 어떤 기본값이 얼마나 자주 쓰이는지 나중에 물을 수 있게.

운영에서 답해야 할 질문은 이것이다: *우리가 대신 고른 값이 실제 사용자 의도와 맞는가.* 그
질문은 나중에 생기고, 그때 데이터가 없으면 답할 수 없다. 그래서 귀결(exact / auto_resolved /
clarification / unsupported)과 **코드별 분포**를 구조화 로그로 남긴다.

원문·개인정보·SQL 은 싣지 않는다(§48). 여기 나가는 것은 코드·개수·모드뿐이다 — 근거 구간의
텍스트조차 넣지 않는다.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from resolution.loop import (
    NeedsClarification,
    ResolutionOutcome,
    ResolvedRequest,
    UnsupportedResolution,
)
from resolution.request import ResolutionProvenance

logger = logging.getLogger("resolution")

EVENT_RESOLUTION_SETTLED = "resolution.settled"

OUTCOME_EXACT = "exact"
OUTCOME_AUTO_RESOLVED = "auto_resolved"
OUTCOME_CLARIFICATION = "clarification"
OUTCOME_UNSUPPORTED = "unsupported"


def outcome_label(outcome: ResolutionOutcome) -> str:
    if isinstance(outcome, UnsupportedResolution):
        return OUTCOME_UNSUPPORTED
    if isinstance(outcome, NeedsClarification):
        return OUTCOME_CLARIFICATION
    if isinstance(outcome, ResolvedRequest):
        return OUTCOME_AUTO_RESOLVED if outcome.assumptions else OUTCOME_EXACT
    return "unknown"


def resolution_metrics(
    outcome: ResolutionOutcome, *, mode: str, answer_count: int = 0
) -> dict[str, Any]:
    """구조화 로그/메트릭 한 벌. 값은 전부 코드와 개수다."""

    by_provenance: Counter[str] = Counter()
    by_code: Counter[str] = Counter()
    for assumption in outcome.assumptions:
        by_provenance[assumption.provenance.value] += 1
        by_code[assumption.code] += 1

    metrics: dict[str, Any] = {
        "outcome": outcome_label(outcome),
        "mode": mode,
        "rounds": outcome.rounds,
        "assumption_count": len(outcome.assumptions),
        "answer_count": answer_count,
        "auto_resolution_by_code": dict(by_code),
        "provenance_counts": dict(by_provenance),
        "policy_default_count": by_provenance.get(
            ResolutionProvenance.POLICY_DEFAULT.value, 0
        ),
        "semantic_inference_count": by_provenance.get(
            ResolutionProvenance.SEMANTIC_INFERENCE.value, 0
        ),
        "user_clarification_count": by_provenance.get(
            ResolutionProvenance.USER_CLARIFICATION.value, 0
        ),
        "clarification_by_code": {},
        "unsupported_by_kind": {},
    }
    if isinstance(outcome, NeedsClarification):
        metrics["clarification_by_code"] = dict(
            Counter(question.code for question in outcome.plan.questions)
        )
        metrics["question_count"] = len(outcome.plan.questions)
        metrics["deferred_question_count"] = outcome.plan.deferred_count
        metrics["unapplied_answer_count"] = len(outcome.unapplied_answers)
    if isinstance(outcome, UnsupportedResolution):
        metrics["unsupported_by_kind"] = dict(
            Counter(issue.kind for issue in outcome.issues)
        )
    return metrics


def log_resolution(
    outcome: ResolutionOutcome, *, mode: str, answer_count: int = 0
) -> dict[str, Any]:
    """메트릭을 만들고 구조화 로그로 남긴다(만든 값을 그대로 돌려준다)."""

    metrics = resolution_metrics(outcome, mode=mode, answer_count=answer_count)
    logger.info(EVENT_RESOLUTION_SETTLED, extra={"resolution": metrics})
    return metrics


__all__ = [
    "EVENT_RESOLUTION_SETTLED",
    "OUTCOME_AUTO_RESOLVED",
    "OUTCOME_CLARIFICATION",
    "OUTCOME_EXACT",
    "OUTCOME_UNSUPPORTED",
    "log_resolution",
    "outcome_label",
    "resolution_metrics",
]
