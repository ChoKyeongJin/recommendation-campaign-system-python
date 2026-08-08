"""Resolution 고정점 — 한 번의 판정으로 끝내지 않는다.

자동 확정은 IR 을 바꾸고, 바뀐 IR 에서는 결핍이 사라지거나 새로 드러난다. 그래서 판정은
**안정 상태까지** 반복한다(§10). 반복이 없으면 '최근 20만원 이상 구매' 같은 문장에서 기간을
채운 뒤에야 보이는 grain 모호성을 놓치거나, 반대로 이미 채운 값을 다시 묻게 된다.

반복에는 두 개의 방벽이 있다.

**확정된 것은 다시 감지되지 않는다.** 사용자가 "합계 기준"이라고 답하면 트리는 그대로여도 그
자리는 확정된 것이다. 확정 목록(``settled``)을 감지 결과에서 빼지 않으면 같은 질문이 영원히
다시 나온다 — 그리고 그것이 §14 가 금지한 "정책으로 이미 확정된 것을 다시 묻기"다.

**진전 없는 라운드는 실패다.** IR 지문·확정 목록·결핍 신원이 앞 라운드와 똑같으면 이 루프는
영원히 돌 것이므로 :class:`ResolutionInvariantViolation` 으로 닫는다. 자동 확정을 약속해 놓고
적용하지 못한 경우도 같은 예외다 — 정책과 적용기가 다른 말을 하고 있다는 뜻이고, 그 상태를
조용한 되묻기로 덮으면 원인이 응답 어디에도 남지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from resolution.applier import apply_auto_resolutions, apply_clarification
from resolution.clarification import (
    ClarificationAnswer,
    ClarificationPlan,
    plan_clarifications,
)
from resolution.config import ResolutionPolicyConfig
from resolution.detection import Detector, detect_semantic_issues
from resolution.issues import SemanticIssue, issue_signature
from resolution.policy import (
    ResolutionAction,
    ResolutionDecisions,
    ResolutionPolicy,
)
from resolution.request import CanonicalRequest, ResolvedAssumption

#: 라운드 상한. 정상 요청은 2~3 라운드에 끝난다 — 이 값은 폭주 방벽이지 성능 손잡이가 아니다.
MAX_ROUNDS = 8


class ResolutionInvariantViolation(RuntimeError):
    """판정과 적용이 서로 다른 말을 하고 있다. 조용히 되묻기로 덮지 않는다."""


@dataclass(frozen=True, slots=True)
class ResolvedRequest:
    """확정 완료. ``assumptions`` 가 비어 있으면 모든 의미가 사용자 명시다."""

    request: CanonicalRequest
    assumptions: tuple[ResolvedAssumption, ...] = ()
    rounds: int = 1
    #: 응답 계약의 판별자. 값은 고정이고 필드로 두는 이유는 투영이 그것을 그대로 싣기 때문이다.
    status: str = "resolved"

    @property
    def resolution(self) -> str:
        return "exact" if self.request.is_exact else "assumed"


@dataclass(frozen=True, slots=True)
class NeedsClarification:
    """되묻기. 이미 자동 확정된 것은 ``assumptions`` 에 있고 질문에는 없다(§14)."""

    request: CanonicalRequest
    issues: tuple[SemanticIssue, ...] = ()
    plan: ClarificationPlan = field(default_factory=ClarificationPlan)
    assumptions: tuple[ResolvedAssumption, ...] = ()
    decisions: ResolutionDecisions = field(default_factory=ResolutionDecisions)
    unapplied_answers: tuple[tuple[str, str], ...] = ()
    rounds: int = 1
    status: str = "needs_clarification"


@dataclass(frozen=True, slots=True)
class UnsupportedResolution:
    """사용자가 답해도 해결되지 않는다. 질문을 만들지 않는다(§25)."""

    request: CanonicalRequest
    issues: tuple[SemanticIssue, ...] = ()
    assumptions: tuple[ResolvedAssumption, ...] = ()
    decisions: ResolutionDecisions = field(default_factory=ResolutionDecisions)
    rounds: int = 1
    status: str = "unsupported"


ResolutionOutcome: TypeAlias = (
    ResolvedRequest | NeedsClarification | UnsupportedResolution
)


def _issues_for(
    issues: Sequence[SemanticIssue], decisions: ResolutionDecisions, action: ResolutionAction
) -> tuple[SemanticIssue, ...]:
    selected = {item.issue_id for item in decisions.by_action(action)}
    return tuple(issue for issue in issues if issue.issue_id in selected)


def resolve_request(
    request: CanonicalRequest,
    *,
    policy: ResolutionPolicy | None = None,
    answers: Sequence[ClarificationAnswer] = (),
    detectors: Sequence[Detector] | None = None,
    max_rounds: int = MAX_ROUNDS,
) -> ResolutionOutcome:
    """결핍이 없어질 때까지 감지 → 판정 → 적용을 반복한다."""

    active_policy = policy if policy is not None else ResolutionPolicy()
    config: ResolutionPolicyConfig = active_policy.config
    current = request
    settled: set[str] = set()
    assumptions: list[ResolvedAssumption] = []
    unapplied_answers: list[tuple[str, str]] = []
    pending_answers = tuple(answers)
    seen: set[tuple[Any, ...]] = set()
    rounds = 0

    while True:
        rounds += 1
        if rounds > max_rounds:
            raise ResolutionInvariantViolation(
                f"resolution 이 {max_rounds} 라운드 안에 안정되지 않았습니다"
            )
        detected = detect_semantic_issues(current, config, detectors=detectors)
        issues = tuple(item for item in detected if item.issue_id not in settled)
        if not issues:
            return ResolvedRequest(
                request=current, assumptions=tuple(assumptions), rounds=rounds
            )

        guard = (current.fingerprint(), tuple(sorted(settled)), issue_signature(issues))
        if guard in seen:
            raise ResolutionInvariantViolation(
                "resolution 이 진전 없이 같은 상태로 되돌아왔습니다"
                f"(결핍 {len(issues)}개, {rounds} 라운드)"
            )
        seen.add(guard)

        decisions = active_policy.resolve(current, issues)

        unsupported = _issues_for(issues, decisions, ResolutionAction.UNSUPPORTED)
        if unsupported:
            # 미지원이 하나라도 있으면 되묻기로 시간을 쓰지 않는다 — 답해도 열리지 않는다.
            return UnsupportedResolution(
                request=current,
                issues=unsupported,
                assumptions=tuple(assumptions),
                decisions=decisions,
                rounds=rounds,
            )

        auto = decisions.by_action(ResolutionAction.AUTO_RESOLVE)
        if auto:
            application = apply_auto_resolutions(current, issues, decisions)
            if not application.changed:
                raise ResolutionInvariantViolation(
                    "정책이 자동 확정을 결정했지만 적용된 값이 없습니다: "
                    f"{application.unapplied}"
                )
            current = application.request
            assumptions.extend(application.applied)
            settled.update(
                item.issue_id for item in application.applied if item.issue_id
            )
            continue

        asked = _issues_for(issues, decisions, ResolutionAction.ASK_USER)
        if not asked:
            raise ResolutionInvariantViolation(
                f"판정되지 않은 결핍이 남았습니다: {issue_signature(issues)}"
            )
        ask_decisions = ResolutionDecisions(
            decisions=decisions.by_action(ResolutionAction.ASK_USER)
        )
        plan = plan_clarifications(
            asked, ask_decisions, max_questions=config.max_questions
        )
        if pending_answers:
            application = apply_clarification(current, plan, pending_answers, asked)
            unapplied_answers.extend(application.unapplied)
            if application.changed:
                current = application.request
                assumptions.extend(application.applied)
                settled.update(
                    item.issue_id for item in application.applied if item.issue_id
                )
                applied_ids = {item.issue_id for item in application.applied}
                pending_answers = tuple(
                    answer
                    for answer in pending_answers
                    if answer.issue_id not in applied_ids
                )
                continue
        return NeedsClarification(
            request=current,
            issues=asked,
            plan=plan,
            assumptions=tuple(assumptions),
            decisions=ask_decisions,
            unapplied_answers=tuple(unapplied_answers),
            rounds=rounds,
        )


__all__ = [
    "MAX_ROUNDS",
    "NeedsClarification",
    "ResolutionInvariantViolation",
    "ResolutionOutcome",
    "ResolvedRequest",
    "UnsupportedResolution",
    "resolve_request",
]
