"""Clarification Planner — 이미 ``ASK_USER`` 로 결정된 결핍을 **문장으로 표현만** 한다.

이 계층은 판단하지 않는다. 지원 여부도, 자동으로 채울지도 여기서 정하지 않는다 —
:mod:`resolution.policy` 가 이미 정했고, 여기 들어오는 것은 그 판정의 결과물뿐이다. 그 사실을
말이 아니라 **구조**로 강제한다: 입력에 ``ASK_USER`` 가 아닌 결정이 하나라도 섞이면
:class:`ClarificationInvariantViolation` 으로 닫는다(§34-B/C/D).

두 번째 계약은 **후보를 발명하지 않는 것**이다(§12). 질문의 선택지는 결핍이 들고 있는
:class:`~resolution.issues.IssueCandidate` 를 그대로 표현한 것이고, 그 밖의 값은 생기지 않는다.
질문 텍스트에서 새 선택지를 만들면 사용자가 고른 값이 어느 슬롯에도 들어가지 못한다.

``question_id`` 는 결핍의 내용 주소에서 파생된다. 회차가 달라도 같은 결핍이면 같은 질문 id 가
나오므로, 사용자의 답이 다음 회차의 같은 자리에 다시 붙는다.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from resolution.issues import SemanticIssue, SourceSpan, issue_kind_spec
from resolution.policy import ResolutionAction, ResolutionDecisions

logger = logging.getLogger("resolution.clarification")


class ClarificationInvariantViolation(RuntimeError):
    """질문으로 만들면 안 되는 결핍이 planner 입력에 들어왔다."""


@dataclass(frozen=True, slots=True)
class ClarificationOption:
    option_id: str
    label: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.option_id, "label": self.label, "value": self.value}


@dataclass(frozen=True, slots=True)
class ClarificationQuestion:
    question_id: str
    issue_id: str
    #: 닫힌 질문 코드. 문구가 바뀌어도 이 값으로 응답을 조회한다.
    code: str
    text: str
    slot: str
    options: tuple[ClarificationOption, ...] = ()
    allow_free_text: bool = False
    entity_type: str | None = None
    evidence: SourceSpan | None = None
    priority: int = 50

    def option(self, option_id: str) -> ClarificationOption | None:
        for item in self.options:
            if item.option_id == option_id:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "issue_id": self.issue_id,
            "code": self.code,
            "text": self.text,
            "slot": self.slot,
            "options": [option.to_dict() for option in self.options],
            "allow_free_text": self.allow_free_text,
            "entity_type": self.entity_type,
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ClarificationPlan:
    questions: tuple[ClarificationQuestion, ...] = ()
    #: 상한 때문에 이번 응답에 싣지 않은 질문 수. 조용한 절단을 만들지 않는다.
    deferred_count: int = 0

    def __bool__(self) -> bool:
        return bool(self.questions)

    def question_for(self, issue_id: str) -> ClarificationQuestion | None:
        for question in self.questions:
            if question.issue_id == issue_id:
                return question
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "questions": [question.to_dict() for question in self.questions],
            "deferred_count": self.deferred_count,
        }


@dataclass(frozen=True, slots=True)
class ClarificationAnswer:
    """사용자의 답 하나. **어느 결핍에 대한 답인지**를 반드시 들고 있다.

    원문을 다시 보내지 않는다 — 답은 슬롯 하나를 확정하는 값이지 새 문장이 아니다(§15).
    """

    issue_id: str
    option_id: str | None = None
    text: str | None = None

    @classmethod
    def from_mapping(cls, raw: Any) -> ClarificationAnswer | None:
        if not isinstance(raw, Mapping):
            return None
        issue_id = raw.get("issue_id") or raw.get("issueId")
        if not isinstance(issue_id, str) or not issue_id.strip():
            return None
        option_id = raw.get("option_id") or raw.get("optionId")
        text = raw.get("text") or raw.get("value")
        return cls(
            issue_id=issue_id.strip(),
            option_id=option_id.strip() if isinstance(option_id, str) and option_id.strip() else None,
            text=text.strip() if isinstance(text, str) and text.strip() else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"issue_id": self.issue_id, "option_id": self.option_id, "text": self.text}


def parse_answers(raw: Any) -> tuple[ClarificationAnswer, ...]:
    """API 경계에서 들어온 답 목록을 타입으로. 읽히지 않는 항목은 조용히 버리지 않고 제외한다."""

    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    parsed = [ClarificationAnswer.from_mapping(item) for item in raw]
    return tuple(item for item in parsed if item is not None)


def question_from_issue(issue: SemanticIssue) -> ClarificationQuestion:
    """결핍 하나 → 질문 하나. 후보는 결핍이 들고 있는 것뿐이다."""

    spec = issue_kind_spec(issue.kind)
    evidence_text = issue.evidence_text or "요청하신 조건"
    return ClarificationQuestion(
        question_id=f"q-{issue.issue_id}",
        issue_id=issue.issue_id,
        code=spec.question_code,
        text=spec.question_text.format(evidence=evidence_text),
        slot=issue.slot,
        options=tuple(
            ClarificationOption(
                option_id=str(candidate.value),
                label=candidate.label,
                value=candidate.value,
            )
            for candidate in issue.candidates
        ),
        allow_free_text=spec.allow_free_text,
        entity_type=issue.entity_type,
        evidence=issue.evidence,
        priority=spec.priority,
    )


def plan_clarifications(
    issues: Sequence[SemanticIssue],
    decisions: ResolutionDecisions,
    *,
    max_questions: int = 0,
) -> ClarificationPlan:
    """``ASK_USER`` 결핍만 질문으로. 다른 결정이 섞이면 invariant 위반이다."""

    asked_ids = {
        decision.issue_id
        for decision in decisions.by_action(ResolutionAction.ASK_USER)
    }
    selected: list[SemanticIssue] = []
    for issue in issues:
        decision = decisions.get(issue.issue_id)
        if decision is None:
            raise ClarificationInvariantViolation(
                f"판정되지 않은 결핍이 질문 생성기에 들어왔습니다: {issue.kind}/{issue.issue_id}"
            )
        if decision.action is not ResolutionAction.ASK_USER:
            raise ClarificationInvariantViolation(
                f"{decision.action.value} 로 결정된 결핍은 질문이 될 수 없습니다: "
                f"{issue.kind}/{issue.issue_id}"
            )
        selected.append(issue)
    missing = asked_ids - {issue.issue_id for issue in selected}
    if missing:
        raise ClarificationInvariantViolation(
            f"ASK_USER 로 결정됐는데 질문 입력에 없는 결핍이 있습니다: {sorted(missing)}"
        )

    ordered = sorted(
        selected, key=lambda item: (issue_kind_spec(item.kind).priority, item.issue_id)
    )
    if max_questions > 0 and len(ordered) > max_questions:
        deferred = len(ordered) - max_questions
        # 조용한 절단을 만들지 않는다 — 남긴 수를 응답과 로그에 함께 싣는다.
        logger.info(
            "clarification_questions_deferred shown=%s deferred=%s", max_questions, deferred
        )
        ordered = ordered[:max_questions]
    else:
        deferred = 0
    return ClarificationPlan(
        questions=tuple(question_from_issue(issue) for issue in ordered),
        deferred_count=deferred,
    )


def answer_value(
    question: ClarificationQuestion, answer: ClarificationAnswer
) -> tuple[Any, str | None]:
    """답 하나 → 슬롯에 넣을 값. 읽을 수 없으면 ``(None, 사유)``.

    선택지가 있는 질문에 자유 입력이 오면 값을 만들어 내지 않는다 — 그것이 곧 후보 발명이다.
    """

    if answer.option_id is not None:
        option = question.option(answer.option_id)
        if option is None:
            return None, f"unknown_option:{answer.option_id}"
        return option.value, None
    if answer.text is not None:
        if not question.allow_free_text:
            return None, "free_text_not_allowed"
        return answer.text, None
    return None, "empty_answer"


def legacy_questions(plan: ClarificationPlan) -> list[str]:
    """타입 있는 질문 → 기존 ``clarification_questions[]`` 문자열 목록.

    새 스키마로 완전히 넘어가기 전까지 기존 소비자를 깨지 않기 위한 어댑터다(§36). 내부 구조를
    이 문자열 모양에 맞추지 않는다 — 방향은 언제나 typed → legacy 다.
    """

    rendered: list[str] = []
    for question in plan.questions:
        if question.options:
            choices = " / ".join(option.label for option in question.options)
            rendered.append(f"{question.text} ({choices})")
        else:
            rendered.append(question.text)
    return rendered


__all__ = [
    "ClarificationAnswer",
    "ClarificationInvariantViolation",
    "ClarificationOption",
    "ClarificationPlan",
    "ClarificationQuestion",
    "answer_value",
    "legacy_questions",
    "parse_answers",
    "plan_clarifications",
    "question_from_issue",
]
