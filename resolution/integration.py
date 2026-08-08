"""Resolution 계층과 기존 오디언스 파이프라인의 **접합부**.

접합을 한 곳에 모으는 이유는 결합 방향이다. 이 모듈은 :mod:`query_structurer` 를 모듈 수준에서
import 하지 않는다 — 호출자가 dict 하나(플랜 payload)와 값 몇 개를 넘기면, 이쪽은 canonical
요청을 만들어 확정 계층을 돌리고 결과를 payload 에 **투영만** 한다. 반대 방향 의존이 생기면
확정 계층이 특정 배포의 구조화기 없이는 import 조차 되지 않는다.

투영되는 것은 셋이다.

``plan["resolution"]``      응답 계약 블록(assumption·질문·모드).
``plan["audience_requirement"]["expression"]``  자동 확정/답변이 값을 넣은 canonical 표현.
``plan["audience_requirement"]["issues"]``      확정으로 방면된 신고가 빠진 목록.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from resolution.applier import render_entity_binding_instruction
from resolution.clarification import ClarificationAnswer
from resolution.loop import (
    NeedsClarification,
    ResolutionInvariantViolation,
    ResolutionOutcome,
    UnsupportedResolution,
    resolve_request,
)
from resolution.observability import log_resolution
from resolution.policy import ResolutionPolicy
from resolution.projection import RESOLUTION_KEY, resolution_payload
from resolution.request import CanonicalRequest
from resolution.runtime import current_answers

logger = logging.getLogger("resolution.integration")

#: 엔터티 결속을 응답/후속 단계가 읽는 자리.
ENTITY_BINDINGS_KEY = "entity_bindings"


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    """접합 결과 — 확정 계층이 실제로 돌았는가와 그 산출물."""

    outcome: ResolutionOutcome | None
    expression: Mapping[str, Any] | None
    reported_issues: tuple[Mapping[str, Any], ...]
    payload: dict[str, Any]

    @property
    def ran(self) -> bool:
        return self.outcome is not None

    @property
    def needs_clarification(self) -> bool:
        return isinstance(self.outcome, NeedsClarification) and bool(
            self.outcome.plan.questions
        )

    @property
    def unsupported(self) -> bool:
        return isinstance(self.outcome, UnsupportedResolution)


def run_for_plan(
    plan: dict[str, Any],
    *,
    query: str,
    expression: Mapping[str, Any] | None,
    reported_issues: Sequence[Mapping[str, Any]],
    current_date: str | None = None,
    policy: ResolutionPolicy | None = None,
    answers: Sequence[ClarificationAnswer] | None = None,
) -> IntegrationResult:
    """확정 계층을 한 번 돌리고 결과를 플랜에 투영한다.

    확정 계층이 스스로 모순에 빠지면(:class:`ResolutionInvariantViolation`) **기존 동작을 그대로
    둔다**. 여기서 임의 귀결을 만들면 이 계층이 없던 배포와 다른 답이 나가는데, 그 차이의 원인은
    응답 어디에도 남지 않는다. 대신 시끄럽게 기록한다.
    """

    active_policy = policy if policy is not None else ResolutionPolicy()
    resolved_answers = tuple(answers) if answers is not None else current_answers()
    request = CanonicalRequest(
        query=query,
        expression=dict(expression) if isinstance(expression, Mapping) else None,
        reported_issues=tuple(dict(item) for item in reported_issues),
        current_date=current_date,
    )
    try:
        outcome = resolve_request(
            request, policy=active_policy, answers=resolved_answers
        )
    except ResolutionInvariantViolation as exc:
        logger.error("resolution_invariant_violation error=%s", exc)
        return IntegrationResult(
            outcome=None,
            expression=request.expression,
            reported_issues=request.reported_issues,
            payload={},
        )

    payload = resolution_payload(
        outcome, mode=active_policy.mode.value, answer_count=len(resolved_answers)
    )
    bindings = [binding.to_dict() for binding in outcome.request.entity_bindings]
    if bindings:
        payload[ENTITY_BINDINGS_KEY] = bindings
    plan[RESOLUTION_KEY] = payload
    log_resolution(
        outcome, mode=active_policy.mode.value, answer_count=len(resolved_answers)
    )
    return IntegrationResult(
        outcome=outcome,
        expression=outcome.request.expression,
        reported_issues=outcome.request.reported_issues,
        payload=payload,
    )


def plan_entity_bindings(plan: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """이 플랜이 들고 있는 엔터티 결속(없으면 빈 튜플)."""

    block = plan.get(RESOLUTION_KEY)
    if not isinstance(block, Mapping):
        return ()
    bindings = block.get(ENTITY_BINDINGS_KEY)
    if not isinstance(bindings, Sequence) or isinstance(bindings, (str, bytes)):
        return ()
    return tuple(dict(item) for item in bindings if isinstance(item, Mapping))


def entity_binding_instruction(plan: Mapping[str, Any]) -> str:
    """플랜에 남은 결속을 구조화기 지시문으로. 결속이 없으면 빈 문자열."""

    from resolution.issues import SourceSpan
    from resolution.request import CanonicalRequest as _Request
    from resolution.request import EntityBinding

    bindings = plan_entity_bindings(plan)
    if not bindings:
        return ""
    request = _Request(query="")
    for record in bindings:
        request = request.with_entity_binding(
            EntityBinding(
                issue_id=str(record.get("issue_id") or ""),
                entity_type=str(record.get("entity_type") or ""),
                value=str(record.get("value") or ""),
                evidence=SourceSpan.from_mapping(record.get("evidence")),
            )
        )
    return render_entity_binding_instruction(request)


def realize_entity_bindings(
    plan: dict[str, Any],
    *,
    restructure: Callable[[str], dict[str, Any]],
    write_log: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """되묻기로 받은 엔터티 값을 **같은 구조화기**에 애플리케이션 소유 값으로 알린다.

    답변 문자열을 원문에 이어 붙이지 않는다 — 넘기는 것은 "이 구간의 자리표시자는 이 값"이라는
    타입 있는 사실 하나뿐이다(§15/§16). 재구조화 결과가 그 값을 실제로 반영하지 못하면
    **채택하지 않는다**: 답을 반영하지 못한 플랜을 반영한 척 내보내는 것이 가장 나쁜 결말이다.
    """

    instruction = entity_binding_instruction(plan)
    if not instruction:
        return plan
    candidate = restructure(instruction)
    values = [str(record.get("value") or "") for record in plan_entity_bindings(plan)]
    accepted = isinstance(candidate, dict) and all(
        value and _plan_mentions(candidate, value) for value in values
    )
    if write_log is not None:
        write_log(
            "resolution_entity_binding_realization",
            {"accepted": accepted, "binding_count": len(values)},
        )
    if not accepted:
        return plan
    # 새 플랜의 판정이 맞다(표현이 실제로 섰다). 다만 **사용자가 답한 값의 영수증**은 이번
    # 라운드에서 사라진다 — 모델이 그 값을 원문처럼 들고 오기 때문이다. 영수증이 없으면 응답은
    # 그 값을 사용자가 처음부터 말한 것으로 보고하게 되므로(§34-G) 앞 라운드의 것을 이어 붙인다.
    merged = dict(candidate.get(RESOLUTION_KEY) or {})
    previous = plan.get(RESOLUTION_KEY)
    if isinstance(previous, Mapping):
        carried = [item for item in previous.get("assumptions") or [] if isinstance(item, Mapping)]
        existing = {
            (item.get("code"), item.get("slot"))
            for item in merged.get("assumptions") or []
            if isinstance(item, Mapping)
        }
        merged["assumptions"] = [
            *(merged.get("assumptions") or []),
            *(item for item in carried if (item.get("code"), item.get("slot")) not in existing),
        ]
        if previous.get(ENTITY_BINDINGS_KEY):
            merged[ENTITY_BINDINGS_KEY] = previous[ENTITY_BINDINGS_KEY]
        if merged["assumptions"]:
            merged["resolution"] = "assumed"
    candidate[RESOLUTION_KEY] = merged
    return candidate


def _plan_mentions(plan: Mapping[str, Any], value: str) -> bool:
    """재구조화 결과가 이 값을 실제로 들고 있는가(문자열 존재 검사)."""

    import json

    try:
        rendered = json.dumps(plan, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return False
    return value in rendered


__all__ = [
    "ENTITY_BINDINGS_KEY",
    "IntegrationResult",
    "entity_binding_instruction",
    "plan_entity_bindings",
    "realize_entity_bindings",
    "run_for_plan",
]
