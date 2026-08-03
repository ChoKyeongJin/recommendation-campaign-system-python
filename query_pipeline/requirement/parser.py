"""LLM 출력 → :class:`AudienceRequirement` 의 **런타임 검증**.

원칙 하나: LLM 출력은 dict 로 쓰지 않는다. 검증 실패도 예외로 흘리지 않고 구조화된
결과(:data:`ParseRequirementResult`)로 바꾼다 — 예외를 그대로 올리면 호출자가 문자열
메시지를 다시 읽어 분기하게 되고, 그것이 이 저장소의 "왜 실패했는지 모르는 500"의 출처다.

로깅 규약: 이 모듈은 원문도 LLM 원문 출력도 스스로 로깅하지 않는다. ``InvalidLlmOutput``
가 ``raw_output`` 을 들고 있는 것은 호출자가 **기존 개인정보 정책에 따라** 다룰 수 있게
하기 위해서이지, 그것을 남기라는 뜻이 아니다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal, Protocol, TypeAlias

from pydantic import Field, JsonValue, ValidationError, model_validator

from query_pipeline.base import Clock, IdFactory, StrictModel, SystemClock, UuidIdFactory
from query_pipeline.event_query.expressions import SourceEvidence
from query_pipeline.requirement.issues import (
    IssueKind,
    IssueSeverity,
    RequirementIssue,
)
from query_pipeline.requirement.models import (
    AudienceRequirement,
    IntentKind,
    ProposedExpression,
    RequirementIntent,
    RequirementSource,
)
from query_pipeline.requirement.validation import issue_from_report

REQUIREMENT_SCHEMA_VERSION = "1"


class ParsedRequirement(StrictModel):
    status: Literal["success"] = "success"
    requirement: AudienceRequirement


class InvalidLlmOutput(StrictModel):
    status: Literal["invalid_output"] = "invalid_output"
    issues: tuple[RequirementIssue, ...] = Field(min_length=1)
    raw_output: JsonValue | None = None


ParseRequirementResult: TypeAlias = Annotated[
    ParsedRequirement | InvalidLlmOutput,
    Field(discriminator="status"),
]


class RequirementParser(Protocol):
    """사용자 문장 → 요구. 구현체는 LLM 이어도, 저장된 payload 어댑터여도 된다."""

    async def parse(self, user_text: str) -> AudienceRequirement: ...


def validate_llm_output(
    raw_output: str | Mapping[str, JsonValue],
) -> ParseRequirementResult:
    """LLM 출력을 요구 모델로 검증한다. 실패는 예외가 아니라 구조화된 결과다."""
    try:
        payload: JsonValue = (
            json.loads(raw_output) if isinstance(raw_output, str) else dict(raw_output)
        )
        requirement = AudienceRequirement.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        issue = RequirementIssue(
            id="invalid-llm-output",
            kind=IssueKind.INVALID,
            severity=IssueSeverity.ERROR,
            path="$",
            message=str(exc),
        )
        return InvalidLlmOutput(
            issues=(issue,),
            raw_output=raw_output if isinstance(raw_output, str) else dict(raw_output),
        )
    return ParsedRequirement(requirement=requirement)


class AudienceRequirementDraft(StrictModel):
    """구조화기가 내는 최소 계약(``{expression, issues}``)의 타입 있는 표기.

    저장소의 기존 LLM 계약(``audience_requirement``)이 정확히 이 모양이다. 어댑터가 이
    모델을 거치는 이유는 dict 접근("payload.get('expression')")이 호출부마다 흩어지는 것을
    막기 위해서다.
    """

    expression: JsonValue | None = None
    issues: tuple[DraftIssue, ...] = ()

    @model_validator(mode="after")
    def _expression_or_issues(self) -> AudienceRequirementDraft:
        if self.expression is not None and self.issues:
            raise ValueError(
                "확정된 표현과 결핍 보고는 공존할 수 없습니다"
                " — 표현을 고치거나 expression=null 로 두십시오"
            )
        return self


class DraftIssue(StrictModel):
    """기존 계약의 issue 표기(code/argument/message/evidence)."""

    code: str = Field(min_length=1)
    argument: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence: DraftEvidence | None = None


class DraftEvidence(StrictModel):
    text: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)


AudienceRequirementDraft.model_rebuild()
DraftIssue.model_rebuild()


def requirement_from_draft(
    draft: AudienceRequirementDraft,
    *,
    user_text: str,
    intent: IntentKind = IntentKind.FIND,
    requirement_id: str,
    created_at: datetime,
    version: str = REQUIREMENT_SCHEMA_VERSION,
) -> AudienceRequirement:
    """기존 ``audience_requirement`` 계약 → 새 요구 모델(무손실).

    '무손실'에는 **근거 구간**이 포함된다. 구간을 떨어뜨리면 결핍의 원인 판정(추출된
    리터럴과의 대조)이 종류만 보고 답하게 되고, 그러면 다른 절의 값 때문에 진짜 결핍이
    재방출로 새거나 이미 아는 값을 사용자에게 되묻는다.
    """
    issues = tuple(
        issue_from_report(
            code=item.code,
            argument=item.argument,
            message=item.message,
            evidence=(
                SourceEvidence(
                    text=item.evidence.text,
                    start=item.evidence.start,
                    end=item.evidence.end,
                )
                if item.evidence is not None
                else None
            ),
        )
        for item in draft.issues
    )
    return AudienceRequirement(
        id=requirement_id,
        version=version,
        intent=RequirementIntent(kind=intent),
        expression=(
            ProposedExpression(payload=draft.expression)
            if draft.expression is not None
            else None
        ),
        issues=issues,
        source=RequirementSource(text=user_text),
        created_at=created_at,
    )


class DraftRequirementParser:
    """구조화기 원문 출력(JSON 문자열/dict) → 요구.

    provider 호출 자체는 주입된 ``complete`` 가 담당한다 — 이 계층은 전송을 모른다.
    """

    def __init__(
        self,
        complete: Completion,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        intent: IntentKind = IntentKind.FIND,
    ) -> None:
        self._complete = complete
        self._clock = clock or SystemClock()
        self._id_factory = id_factory or UuidIdFactory()
        self._intent = intent

    async def parse(self, user_text: str) -> AudienceRequirement:
        requirement_id = self._id_factory.new_id("req")
        created_at = self._clock.now()
        try:
            raw = self._complete(user_text)
            payload = json.loads(raw) if isinstance(raw, str) else raw
            draft = AudienceRequirementDraft.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            return AudienceRequirement(
                id=requirement_id,
                version=REQUIREMENT_SCHEMA_VERSION,
                intent=RequirementIntent(kind=self._intent),
                issues=(
                    RequirementIssue(
                        id="invalid-llm-output",
                        kind=IssueKind.INVALID,
                        severity=IssueSeverity.ERROR,
                        path="$",
                        message=str(exc),
                    ),
                ),
                source=RequirementSource(text=user_text),
                created_at=created_at,
            )
        return requirement_from_draft(
            draft,
            user_text=user_text,
            intent=self._intent,
            requirement_id=requirement_id,
            created_at=created_at,
        )


class Completion(Protocol):
    """사용자 문장 하나를 요구 payload(JSON 문자열 또는 매핑)로 바꾸는 호출."""

    def __call__(self, user_text: str) -> str | Mapping[str, JsonValue]: ...


__all__ = [
    "REQUIREMENT_SCHEMA_VERSION",
    "AudienceRequirementDraft",
    "Completion",
    "DraftEvidence",
    "DraftIssue",
    "DraftRequirementParser",
    "InvalidLlmOutput",
    "ParseRequirementResult",
    "ParsedRequirement",
    "RequirementParser",
    "requirement_from_draft",
    "validate_llm_output",
]
