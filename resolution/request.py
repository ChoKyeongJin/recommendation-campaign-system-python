"""Resolution 계층이 다루는 **요청 상태** — canonical IR + 아직 확정되지 않은 것들.

이 계층은 원문을 다시 읽지 않는다. 읽는 것은 이미 확정된 canonical 표현(event IR wire dict)과
그 표현을 만든 계층이 남긴 결핍 보고뿐이다. 그래서 상태는 작고 **불변**이다 — 정책이 값을
채우면 새 객체가 나오고, 무엇이 바뀌었는지는 :class:`ResolvedAssumption` 목록이 남긴다.

``assumptions`` 가 이 모듈의 핵심이다. 사용자가 말하지 않은 의미가 SQL 에 들어갔다면 그
사실은 반드시 어딘가에 남아야 한다(§34-G). 남지 않으면 응답을 읽는 쪽은 30일이 사용자의
말인지 배포 설정인지 구별할 수 없고, 의미 검증기는 자기 SQL 을 '원문에 없는 조건'으로 되잡는다.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from resolution.issues import SourceSpan


class ResolutionProvenance(StrEnum):
    """이 값이 **어디에서** 왔는가. 넷을 하나로 접으면 §23 이 금지한 혼동이 생긴다."""

    #: 사용자가 원문에서 직접 말했다.
    USER_EXPLICIT = "user_explicit"
    #: 운영 정책이 선언한 기본값으로 채웠다.
    POLICY_DEFAULT = "policy_default"
    #: 문장 구조가 강하게 유도하지만 직접 명시되지는 않았다.
    SEMANTIC_INFERENCE = "semantic_inference"
    #: 되묻기에 사용자가 답한 값이다.
    USER_CLARIFICATION = "user_clarification"


#: 사용자 명시가 아닌 출처. SQL 에 이 출처의 의미가 들어가면 영수증이 반드시 있어야 한다.
NON_EXPLICIT_PROVENANCES: frozenset[ResolutionProvenance] = frozenset(
    {
        ResolutionProvenance.POLICY_DEFAULT,
        ResolutionProvenance.SEMANTIC_INFERENCE,
        ResolutionProvenance.USER_CLARIFICATION,
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedAssumption:
    """정책 또는 사용자 답변이 확정한 의미 하나의 영수증."""

    #: 응답 계약의 닫힌 코드(질문 코드와 같은 어휘를 쓴다).
    code: str
    #: 어느 의미 슬롯을 채웠는가.
    slot: str
    #: 채운 값(JSON 직렬화 가능한 형태여야 한다).
    value: Any
    provenance: ResolutionProvenance
    issue_id: str | None = None
    evidence: SourceSpan | None = None
    #: 왜 이렇게 확정했는가(정책 규칙 이름 등). 사람이 읽는 문장이 아니라 코드다.
    reason_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "slot": self.slot,
            "value": copy.deepcopy(self.value),
            "provenance": self.provenance.value,
            "issue_id": self.issue_id,
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class EntityBinding:
    """되묻기로 받은 **엔터티 값** 하나 — canonical 슬롯이 아직 없을 때의 자리.

    자유 입력 답변('로얄캐닌 사료')은 표현이 서 있을 때만 슬롯에 바로 들어간다. 표현 자체가
    서지 못해 되묻는 경우(가장 흔하다)에는 채울 자리가 없으므로, 답을 **타입 있는 결속**으로
    들고 있다가 표현을 만드는 계층에 애플리케이션 소유 값으로 전달한다. 답변 문자열을 원문에
    이어 붙여 다시 해석하지 않는다(§15) — 결속은 구간과 엔터티 종류를 함께 들고 있다.
    """

    issue_id: str
    entity_type: str
    value: str
    evidence: SourceSpan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "entity_type": self.entity_type,
            "value": self.value,
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    """확정된 의미 + 아직 확정되지 않은 것들. 이 계층이 다루는 유일한 상태다."""

    query: str
    #: canonical event IR 의 wire dict. 표현이 서지 못했으면 ``None``.
    expression: Mapping[str, Any] | None = None
    #: 표현을 만든 계층이 남긴 legacy 결핍 보고(code/argument/message/evidence).
    #: 이 계층은 이것을 **읽기만** 하고, 감지기가 타입 있는 결핍으로 옮긴다.
    reported_issues: tuple[Mapping[str, Any], ...] = ()
    assumptions: tuple[ResolvedAssumption, ...] = ()
    entity_bindings: tuple[EntityBinding, ...] = ()
    current_date: str | None = None
    literal_bindings: tuple[Mapping[str, Any], ...] = field(default=())

    @property
    def has_expression(self) -> bool:
        return isinstance(self.expression, Mapping)

    @property
    def is_exact(self) -> bool:
        """모든 의미가 사용자 명시인가. 하나라도 정책·추론·답변이면 ``assumed`` 다."""

        return not any(
            assumption.provenance in NON_EXPLICIT_PROVENANCES
            for assumption in self.assumptions
        )

    def with_expression(self, expression: Mapping[str, Any] | None) -> CanonicalRequest:
        return replace(self, expression=None if expression is None else dict(expression))

    def with_reported_issues(
        self, issues: Sequence[Mapping[str, Any]]
    ) -> CanonicalRequest:
        return replace(self, reported_issues=tuple(dict(item) for item in issues))

    def with_assumption(self, assumption: ResolvedAssumption) -> CanonicalRequest:
        return replace(self, assumptions=(*self.assumptions, assumption))

    def with_entity_binding(self, binding: EntityBinding) -> CanonicalRequest:
        return replace(self, entity_bindings=(*self.entity_bindings, binding))

    def fingerprint(self) -> str:
        """이 요청의 **의미 지문**. 고정점 판정이 "IR 이 변했는가"를 이 값으로 답한다."""

        import hashlib
        import json

        payload = json.dumps(
            {
                "expression": self.expression,
                "reported": [dict(item) for item in self.reported_issues],
                "bindings": [binding.to_dict() for binding in self.entity_bindings],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "NON_EXPLICIT_PROVENANCES",
    "CanonicalRequest",
    "EntityBinding",
    "ResolutionProvenance",
    "ResolvedAssumption",
]
