"""확정된 값을 요청에 **넣는** 계층 — 자동 확정과 사용자 답변, 두 입구가 같은 자리로 모인다.

두 입구가 같은 코드를 쓰는 이유는 하나다. 값의 출처가 정책이든 사용자든, 들어가는 자리는 같은
의미 슬롯이고 남는 영수증도 같은 모양이어야 한다. 다른 것은 :class:`ResolutionProvenance`
하나뿐이다 — 그 차이가 응답에서 "배포가 고른 값"과 "사용자가 고른 값"을 가른다(§23).

여기서 하지 않는 것:

* **원문을 다시 읽지 않는다.** 이 모듈은 정규화기·구조화기를 import 하지 않는다(§34-F).
  자유 입력 답변은 슬롯 범위의 좁은 해석기(:data:`ANSWER_RESOLVERS`)만 거친다 — 문장 전체를
  다시 파싱하지 않는다(§16).
* **답이 가리키지 않은 자리를 바꾸지 않는다.** 적용은 :mod:`resolution.slots` 의 국소 교체뿐이고,
  그 밖의 절·값·극성·근거는 그대로 남는다(§34-E).
* **적용하지 못한 것을 성공으로 만들지 않는다.** 못 넣은 값은 ``unapplied`` 로 사유와 함께
  남고, 결핍은 그대로 살아 있다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from resolution import slots
from resolution.clarification import (
    ClarificationAnswer,
    ClarificationPlan,
    answer_value,
)
from resolution.issues import SemanticIssue, SourceSpan
from resolution.policy import ResolutionAction, ResolutionDecisions
from resolution.request import (
    CanonicalRequest,
    ResolutionProvenance,
    ResolvedAssumption,
)

#: 사용자 답변 assumption 의 코드 접두어. 어떤 질문의 답인지 응답에서 바로 읽힌다.
ANSWER_CODE_PREFIX = "USER_ANSWER"


@dataclass(frozen=True, slots=True)
class ApplicationResult:
    """적용 결과 — 무엇이 들어갔고 무엇이 왜 못 들어갔는가."""

    request: CanonicalRequest
    applied: tuple[ResolvedAssumption, ...] = ()
    #: (issue_id, 사유 코드). 조용히 사라지는 값을 만들지 않는다.
    unapplied: tuple[tuple[str, str], ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.applied)


# ── 자유 입력 답변의 슬롯 범위 해석기 ────────────────────────────────────────────


def resolve_period_answer(text: str) -> Any | None:
    """'30일'·'최근 3개월' 같은 답 하나를 롤링 창으로. 못 읽으면 ``None``.

    기간 문법의 소유자는 :mod:`calendar_window` 이고 단위 어휘의 소유자는 :mod:`event_ir` 이다 —
    여기서 두 번째 기간 파서를 만들지 않는다.
    """

    import calendar_window
    import event_ir

    parsed = calendar_window.parse_duration_window(text)
    if not isinstance(parsed, Mapping):
        return None
    unit = event_ir.canonical_unit(parsed.get("unit"))
    value = parsed.get("value")
    if unit is None or unit not in event_ir.WINDOW_UNITS:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return {"type": "rolling", "value": value, "unit": str(unit)}


def resolve_text_answer(text: str) -> Any | None:
    """이름·값을 그대로 쓰는 슬롯의 해석기(엔터티 이름 등)."""

    stripped = text.strip()
    return stripped or None


#: 슬롯별 자유 입력 해석기. 등록되지 않은 슬롯은 문자열을 그대로 쓴다.
ANSWER_RESOLVERS: dict[str, Callable[[str], Any | None]] = {
    "audience.period": resolve_period_answer,
}


def resolve_answer_text(slot: str, text: str) -> Any | None:
    resolver = ANSWER_RESOLVERS.get(slot, resolve_text_answer)
    return resolver(text)


# ── legacy 신고 방면 ─────────────────────────────────────────────────────────────


def _same_evidence(report: Mapping[str, Any], evidence: SourceSpan | None) -> bool:
    reported = SourceSpan.from_mapping(report.get("evidence"))
    if evidence is None or reported is None:
        return evidence is None and reported is None
    return (reported.start, reported.end) == (evidence.start, evidence.end)


def discharge_reported_issue(
    request: CanonicalRequest, issue: SemanticIssue
) -> CanonicalRequest:
    """확정된 결핍의 legacy 신고를 목록에서 뺀다.

    빼지 않으면 값을 채워 놓고도 문장이 결핍으로 닫힌다 — 신고가 하류 진단의 입력이기 때문이다.
    같은 (code, argument) 가 여럿일 수 있으므로 근거 구간까지 맞는 항목 하나만 뺀다.
    """

    if issue.legacy_report is None:
        return request
    code, argument = issue.legacy_report
    remaining: list[Mapping[str, Any]] = []
    removed = False
    for report in request.reported_issues:
        if (
            not removed
            and str(report.get("code") or "") == code
            and str(report.get("argument") or "") == argument
            and _same_evidence(report, issue.evidence)
        ):
            removed = True
            continue
        remaining.append(report)
    return request.with_reported_issues(remaining)


# ── 자동 확정 ────────────────────────────────────────────────────────────────────


def apply_auto_resolutions(
    request: CanonicalRequest,
    issues: Sequence[SemanticIssue],
    decisions: ResolutionDecisions,
) -> ApplicationResult:
    """``AUTO_RESOLVE`` 결정들을 요청에 적용한다."""

    by_id = {issue.issue_id: issue for issue in issues}
    applied: list[ResolvedAssumption] = []
    unapplied: list[tuple[str, str]] = []
    current = request
    for decision in decisions.by_action(ResolutionAction.AUTO_RESOLVE):
        issue = by_id.get(decision.issue_id)
        if issue is None:
            unapplied.append((decision.issue_id, "issue_not_found"))
            continue
        patch = slots.apply_slot(current, issue, decision.value)
        if not patch.applied:
            unapplied.append((decision.issue_id, patch.reason or "slot_patch_failed"))
            continue
        current = discharge_reported_issue(patch.request, issue)
        applied.append(
            ResolvedAssumption(
                code=decision.code or decision.kind.upper(),
                slot=issue.slot,
                value=decision.value,
                provenance=decision.provenance or ResolutionProvenance.POLICY_DEFAULT,
                issue_id=issue.issue_id,
                evidence=issue.evidence,
                reason_code=decision.reason_code,
            )
        )
    for assumption in applied:
        current = current.with_assumption(assumption)
    return ApplicationResult(
        request=current, applied=tuple(applied), unapplied=tuple(unapplied)
    )


# ── 사용자 답변 ──────────────────────────────────────────────────────────────────


def apply_clarification(
    request: CanonicalRequest,
    plan: ClarificationPlan,
    answers: Sequence[ClarificationAnswer],
    issues: Sequence[SemanticIssue],
) -> ApplicationResult:
    """답변들을 **각자의 슬롯에만** 적용한다. 원문 전체를 다시 해석하지 않는다."""

    by_id = {issue.issue_id: issue for issue in issues}
    applied: list[ResolvedAssumption] = []
    unapplied: list[tuple[str, str]] = []
    current = request
    for answer in answers:
        question = plan.question_for(answer.issue_id)
        if question is None:
            # 이번 라운드에 없는 결핍에 대한 답이다(문장이 바뀌었거나 이미 해결됐다).
            unapplied.append((answer.issue_id, "question_not_in_plan"))
            continue
        issue = by_id.get(answer.issue_id)
        if issue is None:
            unapplied.append((answer.issue_id, "issue_not_found"))
            continue
        raw, reason = answer_value(question, answer)
        if reason is not None:
            unapplied.append((answer.issue_id, reason))
            continue
        value = (
            resolve_answer_text(issue.slot, raw)
            if isinstance(raw, str) and answer.option_id is None
            else raw
        )
        if value is None:
            unapplied.append((answer.issue_id, "answer_value_unreadable"))
            continue
        patch = slots.apply_slot(current, issue, value)
        if not patch.applied:
            unapplied.append((answer.issue_id, patch.reason or "slot_patch_failed"))
            continue
        current = discharge_reported_issue(patch.request, issue)
        applied.append(
            ResolvedAssumption(
                code=f"{ANSWER_CODE_PREFIX}:{question.code}",
                slot=issue.slot,
                value=value,
                provenance=ResolutionProvenance.USER_CLARIFICATION,
                issue_id=issue.issue_id,
                evidence=issue.evidence,
                reason_code="user_clarification_applied",
            )
        )
    for assumption in applied:
        current = current.with_assumption(assumption)
    return ApplicationResult(
        request=current, applied=tuple(applied), unapplied=tuple(unapplied)
    )


# ── 엔터티 결속의 전달 ───────────────────────────────────────────────────────────

#: 구조화기에 넘기는 애플리케이션 소유 지시문의 제목. 정책 지시문과 같은 형식을 따른다.
ENTITY_BINDING_HEADING = "[Application-owned Clarification Answers]"


def render_entity_binding_instruction(request: CanonicalRequest) -> str:
    """사용자가 지정한 엔터티 값을 **타입 있는 결속**으로 구조화기에 알린다.

    답변 문자열을 원문에 이어 붙이지 않는다 — 그렇게 하면 원래 확정돼 있던 의미까지 다시
    흔들린다(§15). 여기서 넘기는 것은 "이 구간의 자리표시자는 이 값이다"라는 사실 하나뿐이고,
    다른 값을 추론하지 말라는 제약이 함께 나간다.
    """

    if not request.entity_bindings:
        return ""
    lines = [ENTITY_BINDING_HEADING]
    for binding in request.entity_bindings:
        span = (
            f"'{binding.evidence.text}' [{binding.evidence.start}, {binding.evidence.end})"
            if binding.evidence is not None
            else f"the unspecified {binding.entity_type}"
        )
        lines.append(
            f"- The user answered a clarification question: {span} denotes "
            f"{binding.entity_type} = {binding.value!r}. Use that exact value."
        )
    lines.append(
        "These answers replace ONLY the placeholder they name. Every other condition keeps "
        "the meaning the query already states, and no other missing value may be inferred."
    )
    return "\n".join(lines)


__all__ = [
    "ANSWER_CODE_PREFIX",
    "ANSWER_RESOLVERS",
    "ENTITY_BINDING_HEADING",
    "ApplicationResult",
    "apply_auto_resolutions",
    "apply_clarification",
    "discharge_reported_issue",
    "render_entity_binding_instruction",
    "resolve_answer_text",
    "resolve_period_answer",
    "resolve_text_answer",
]
