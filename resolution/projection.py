"""Resolution 결과 → **응답 계약**.

기존 응답을 깨지 않는 것이 이 모듈의 첫 임무다. ``clarification_questions`` 는 지금도 문자열
배열이고 저장된 응답과 프론트가 그 모양을 읽는다. 그래서 타입 있는 질문을 내부 진실로 두고,
문자열 배열은 어댑터가 파생한다(§36) — 방향은 언제나 typed → legacy 다.

새로 실리는 것은 하나의 블록(``resolution``)이다.

    resolution.status        resolved | needs_clarification | unsupported
    resolution.resolution    exact | assumed      ← 정책이 채운 값이 있는가
    resolution.mode          이 배포의 자동 확정 허용선
    resolution.assumptions   사용자 명시가 아닌 의미의 영수증(§34-G)
    resolution.questions     타입 있는 질문(선택지·슬롯·근거 구간 포함)

``assumptions`` 가 비어 있고 ``resolution == "exact"`` 이면 SQL 의 모든 의미가 사용자의 말이다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from resolution.clarification import legacy_questions
from resolution.loop import (
    NeedsClarification,
    ResolutionOutcome,
    ResolvedRequest,
    UnsupportedResolution,
)

RESOLUTION_KEY = "resolution"
RESOLUTION_EXACT = "exact"
RESOLUTION_ASSUMED = "assumed"


def _assumptions(outcome: ResolutionOutcome) -> list[dict[str, Any]]:
    return [assumption.to_dict() for assumption in outcome.assumptions]


def resolution_payload(
    outcome: ResolutionOutcome, *, mode: str, answer_count: int = 0
) -> dict[str, Any]:
    """응답 최상위에 실을 ``resolution`` 블록."""

    payload: dict[str, Any] = {
        "status": outcome.status,
        "mode": mode,
        "assumptions": _assumptions(outcome),
        "questions": [],
        "answer_count": answer_count,
        "rounds": outcome.rounds,
    }
    if isinstance(outcome, ResolvedRequest):
        payload["resolution"] = (
            RESOLUTION_EXACT if outcome.request.is_exact else RESOLUTION_ASSUMED
        )
        return payload
    payload["resolution"] = RESOLUTION_ASSUMED if outcome.assumptions else RESOLUTION_EXACT
    if isinstance(outcome, NeedsClarification):
        payload["questions"] = [item.to_dict() for item in outcome.plan.questions]
        payload["deferred_question_count"] = outcome.plan.deferred_count
        payload["unapplied_answers"] = [
            {"issue_id": issue_id, "reason": reason}
            for issue_id, reason in outcome.unapplied_answers
        ]
        return payload
    if isinstance(outcome, UnsupportedResolution):
        payload["unsupported"] = [issue.to_dict() for issue in outcome.issues]
    return payload


def legacy_clarification_questions(outcome: ResolutionOutcome) -> list[str]:
    """기존 ``clarification_questions[]`` 계약. 되묻기가 아니면 빈 목록이다."""

    if isinstance(outcome, NeedsClarification):
        return legacy_questions(outcome.plan)
    return []


def legacy_questions_from_payload(block: Any) -> list[str]:
    """플랜에 이미 투영된 ``resolution`` 블록에서 legacy 문자열 질문을 만든다.

    응답 조립부는 outcome 객체가 아니라 플랜 dict 만 들고 있다. 같은 어댑터를 두 번 적지 않도록
    dict 입구를 여기 둔다 — 방향은 여전히 typed → legacy 다.
    """

    if not isinstance(block, Mapping):
        return []
    questions = block.get("questions")
    if not isinstance(questions, Sequence) or isinstance(questions, (str, bytes)):
        return []
    rendered: list[str] = []
    for item in questions:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        options = item.get("options")
        labels = [
            str(option.get("label"))
            for option in options
            if isinstance(option, Mapping) and option.get("label")
        ] if isinstance(options, Sequence) and not isinstance(options, (str, bytes)) else []
        rendered.append(f"{text} ({' / '.join(labels)})" if labels else text)
    return rendered


__all__ = [
    "RESOLUTION_ASSUMED",
    "RESOLUTION_EXACT",
    "RESOLUTION_KEY",
    "legacy_clarification_questions",
    "legacy_questions_from_payload",
    "resolution_payload",
]
