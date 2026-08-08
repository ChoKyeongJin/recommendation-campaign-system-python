"""의미 결핍의 **타입** — 무엇이 비었고, 그것을 채우면 어느 자리가 확정되는가.

이 계층이 없던 동안 결핍은 자유 문장(``message``)과 legacy 보고 코드(``missing_argument``)
두 가지로만 존재했다. 그 두 가지로는 다음 질문에 답할 수 없다.

* 이 결핍은 **사용자가 답하면** 없어지는가, 아니면 우리 쪽 실행 자산의 문제인가.
* 답을 받으면 **어느 자리**를 고쳐야 하는가(원문 전체를 다시 읽지 않으려면 필요하다).
* 후보가 둘 이상인가, 아예 값이 없는가.
* 정책이 대신 채워도 안전한가(=위험도).

그래서 결핍은 여기서 **값**이 된다. ``kind`` 는 닫힌 어휘이고, 각 ``kind`` 는
:data:`ISSUE_KIND_SPECS` 에서 계열(family)·위험도(risk)·의미 슬롯(slot)·질문 모양을 한 번만
선언한다. 판정(:mod:`resolution.policy`)도 질문 생성(:mod:`resolution.clarification`)도 이
선언만 읽는다 — 어느 소비자도 kind 문자열을 다시 분기하지 않는다.

**새 결핍 종류를 여는 방법은 한 가지다**: :data:`ISSUE_KIND_SPECS` 에 항목 하나를 더하고
(:mod:`resolution.detection`) 에 그 종류를 내는 감지기를 등록한다. 생산자가 없는 kind 는
선언하지 않는다 — 죽은 어휘는 "지원한다"는 거짓 신호가 된다.

``issue_id`` 는 **내용 주소**다(순번이 아니다). 같은 원문·같은 자리·같은 종류이면 회차가
달라도 같은 id 가 나온다. 그래야 사용자의 답(:class:`~resolution.clarification.ClarificationAnswer`)이
다음 회차의 같은 결핍에 다시 붙는다 — 순번이면 질문 하나가 늘거나 줄 때 답이 엉뚱한 자리에
적용된다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class IssueFamily(StrEnum):
    """결핍의 계열 — 이 셋의 구분이 곧 "누가 해결할 수 있는가"다."""

    #: 필요한 자리에 값이 없다.
    MISSING = "missing"
    #: 후보가 둘 이상이라 확정되지 않았다.
    AMBIGUOUS = "ambiguous"
    #: 사용자가 답해도 해결되지 않는다(실행 자산·능력의 문제).
    UNSUPPORTED = "unsupported"


class ResolutionRisk(StrEnum):
    """정책이 대신 확정했을 때 **결과 집합이 얼마나 달라질 수 있는가**."""

    #: 선언된 운영 기본값으로 채워도 대상이 크게 흔들리지 않는다.
    LOW = "low"
    #: 단일 후보이지만 사용자가 명시한 값은 아니다.
    MEDIUM = "medium"
    #: 잘못 고르면 대상 집합이 통째로 달라진다(grain·AND/OR·식별자·부재 의미).
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """이 결핍이 원문의 **어디**에서 나왔는가.

    좌표를 들고 있는 이유는 두 가지다 — 되묻기 문장이 그 말을 인용할 수 있고, 같은 표면어가
    여러 번 나오는 문장에서 결핍이 특정된다(:mod:`default_period_policy` 가 실측으로 다친 자리다).
    """

    text: str
    start: int
    end: int

    @classmethod
    def from_mapping(cls, raw: Any) -> SourceSpan | None:
        if not isinstance(raw, Mapping):
            return None
        text, start, end = raw.get("text"), raw.get("start"), raw.get("end")
        if not isinstance(text, str):
            return None
        if not isinstance(start, int) or isinstance(start, bool):
            return None
        if not isinstance(end, int) or isinstance(end, bool):
            return None
        return cls(text=text, start=start, end=end)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start": self.start, "end": self.end}


@dataclass(frozen=True, slots=True)
class IssueCandidate:
    """후보 하나 — 확정될 **값**과 사람이 읽는 라벨.

    질문 문장은 이 후보만 표현한다. 질문 생성기가 여기 없는 선택지를 만들어 내면 사용자가
    고른 값이 어느 슬롯에도 들어가지 못한다(:mod:`resolution.clarification` 의 불변식).
    """

    value: Any
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "label": self.label}


@dataclass(frozen=True, slots=True)
class IssueKindSpec:
    """결핍 종류 하나의 선언. 판정·질문·우선순위가 전부 여기서 나온다."""

    kind: str
    family: IssueFamily
    risk: ResolutionRisk
    #: 이 결핍이 가리키는 **의미 슬롯**. 답이 들어갈 자리이고, applier 가 이 이름으로 분기한다.
    slot: str
    #: 질문 코드(응답 계약의 닫힌 어휘). 문구가 바뀌어도 이 값은 유지된다.
    question_code: str
    #: 질문 문장. ``{evidence}`` 만 치환된다 — 새 의미를 만들지 않는다.
    question_text: str
    #: 후보 목록 없이 자유 입력을 받아야 하는가.
    allow_free_text: bool = False
    #: 질문 순서(작을수록 먼저). 순서만 정하고 의미를 바꾸지 않는다(§20).
    priority: int = 50


# ── 닫힌 어휘 ─────────────────────────────────────────────────────────────────────
# 값은 응답 계약으로 나가고 저장된 응답에서 조회된다. **문자열을 바꾸지 않는다.**

MISSING_RECENT_PERIOD = "missing_recent_period"
MISSING_ENTITY_VALUE = "missing_entity_value"
MISSING_SEMANTIC_VALUE = "missing_semantic_value"
AMBIGUOUS_COMPARISON_GRAIN = "ambiguous_comparison_grain"
AMBIGUOUS_REQUIREMENT = "ambiguous_requirement"
UNSUPPORTED_SEMANTICS = "unsupported_semantics"
UNSUPPORTED_SUBJECT = "unsupported_subject"
UNSUPPORTED_CAPABILITY = "unsupported_capability"

#: 임계값이 걸리는 grain 의 두 후보. 값 어휘의 소유자는 :mod:`grain_claims` 다.
GRAIN_ROW = "row"
GRAIN_SUBJECT = "subject"


ISSUE_KIND_SPECS: dict[str, IssueKindSpec] = {
    MISSING_RECENT_PERIOD: IssueKindSpec(
        kind=MISSING_RECENT_PERIOD,
        family=IssueFamily.MISSING,
        risk=ResolutionRisk.LOW,
        slot="audience.period",
        question_code="MISSING_RECENT_PERIOD",
        question_text="'{evidence}'는 최근 며칠 기준인가요? 기간을 알려 주세요.",
        allow_free_text=True,
        priority=40,
    ),
    MISSING_ENTITY_VALUE: IssueKindSpec(
        kind=MISSING_ENTITY_VALUE,
        family=IssueFamily.MISSING,
        risk=ResolutionRisk.HIGH,
        slot="predicate.entity_value",
        question_code="MISSING_ENTITY_VALUE",
        question_text="'{evidence}'는 어떤 대상을 기준으로 할까요? 이름을 지정해 주세요.",
        allow_free_text=True,
        priority=10,
    ),
    MISSING_SEMANTIC_VALUE: IssueKindSpec(
        kind=MISSING_SEMANTIC_VALUE,
        family=IssueFamily.MISSING,
        risk=ResolutionRisk.MEDIUM,
        slot="audience.requirement",
        question_code="MISSING_SEMANTIC_VALUE",
        question_text="'{evidence}'의 기준값을 확정하지 못했습니다. 값을 알려 주세요.",
        allow_free_text=True,
        priority=30,
    ),
    AMBIGUOUS_COMPARISON_GRAIN: IssueKindSpec(
        kind=AMBIGUOUS_COMPARISON_GRAIN,
        family=IssueFamily.AMBIGUOUS,
        risk=ResolutionRisk.HIGH,
        slot="comparison.grain",
        question_code="AMBIGUOUS_AMOUNT_GRAIN",
        question_text="'{evidence}' 기준은 한 번의 주문인가요, 기간 내 합계인가요?",
        priority=45,
    ),
    AMBIGUOUS_REQUIREMENT: IssueKindSpec(
        kind=AMBIGUOUS_REQUIREMENT,
        family=IssueFamily.AMBIGUOUS,
        risk=ResolutionRisk.HIGH,
        slot="audience.requirement",
        question_code="AMBIGUOUS_REQUIREMENT",
        question_text="'{evidence}'를 어떤 뜻으로 읽을지 확정하지 못했습니다. 조건을 더 구체적으로 알려 주세요.",
        allow_free_text=True,
        priority=20,
    ),
    UNSUPPORTED_SEMANTICS: IssueKindSpec(
        kind=UNSUPPORTED_SEMANTICS,
        family=IssueFamily.UNSUPPORTED,
        risk=ResolutionRisk.HIGH,
        slot="audience.requirement",
        question_code="UNSUPPORTED_SEMANTICS",
        question_text="",
        priority=90,
    ),
    UNSUPPORTED_SUBJECT: IssueKindSpec(
        kind=UNSUPPORTED_SUBJECT,
        family=IssueFamily.UNSUPPORTED,
        risk=ResolutionRisk.HIGH,
        slot="audience.subject",
        question_code="UNSUPPORTED_SUBJECT",
        question_text="",
        priority=90,
    ),
    UNSUPPORTED_CAPABILITY: IssueKindSpec(
        kind=UNSUPPORTED_CAPABILITY,
        family=IssueFamily.UNSUPPORTED,
        risk=ResolutionRisk.HIGH,
        slot="audience.capability",
        question_code="UNSUPPORTED_CAPABILITY",
        question_text="",
        priority=90,
    ),
}

#: 사용자에게 물어도 해결되지 않는 계열. 질문 생성기 입력에서 구조적으로 배제된다(§25).
UNSUPPORTED_KINDS: frozenset[str] = frozenset(
    kind for kind, spec in ISSUE_KIND_SPECS.items() if spec.family is IssueFamily.UNSUPPORTED
)


class UnknownIssueKindError(ValueError):
    """선언되지 않은 결핍 종류. 조용히 기본값으로 흐르지 않는다(§11)."""


def issue_kind_spec(kind: str) -> IssueKindSpec:
    try:
        return ISSUE_KIND_SPECS[kind]
    except KeyError as exc:
        raise UnknownIssueKindError(f"declared issue kind가 아닙니다: {kind!r}") from exc


def issue_identity(
    *,
    kind: str,
    slot: str,
    clause_id: str | None,
    evidence: SourceSpan | None,
) -> str:
    """결핍의 **내용 주소**. 같은 자리·같은 종류이면 회차가 달라도 같은 값이다."""

    payload = json.dumps(
        {
            "kind": kind,
            "slot": slot,
            "clause_id": clause_id,
            "evidence": None if evidence is None else [evidence.start, evidence.end, evidence.text],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class SemanticIssue:
    """확정되지 않은 의미 하나.

    ``slot`` 이 비어 있는 결핍은 만들지 않는다 — 무엇을 채우면 해결되는지 모르는 결핍은
    답을 받아도 적용할 자리가 없다(§3).
    """

    kind: str
    slot: str
    message: str
    clause_id: str | None = None
    candidates: tuple[IssueCandidate, ...] = ()
    entity_type: str | None = None
    evidence: SourceSpan | None = None
    #: 어느 감지기가 냈는가(관측용). 판정 근거로 쓰지 않는다.
    origin: str = "unknown"
    #: legacy 보고로 되돌릴 때 쓰는 원본 좌표(code, argument). 없으면 legacy 표기가 없다.
    legacy_report: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        spec = issue_kind_spec(self.kind)
        if not self.slot.strip():
            raise ValueError(f"{self.kind} issue는 semantic slot을 가리켜야 합니다")
        if not self.message.strip():
            raise ValueError(f"{self.kind} issue는 메시지를 가져야 합니다")
        if (
            spec.family is IssueFamily.AMBIGUOUS
            and not spec.allow_free_text
            and len(self.candidates) < 2
        ):
            raise ValueError(
                f"{self.kind} issue는 후보가 둘 이상이어야 모호성입니다"
                f"(받은 후보 {len(self.candidates)}개)"
            )

    @property
    def spec(self) -> IssueKindSpec:
        return issue_kind_spec(self.kind)

    @property
    def family(self) -> IssueFamily:
        return self.spec.family

    @property
    def risk(self) -> ResolutionRisk:
        return self.spec.risk

    @property
    def issue_id(self) -> str:
        return issue_identity(
            kind=self.kind,
            slot=self.slot,
            clause_id=self.clause_id,
            evidence=self.evidence,
        )

    @property
    def evidence_text(self) -> str:
        return self.evidence.text if self.evidence is not None else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "kind": self.kind,
            "family": self.family.value,
            "risk": self.risk.value,
            "slot": self.slot,
            "message": self.message,
            "clause_id": self.clause_id,
            "entity_type": self.entity_type,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "origin": self.origin,
        }


def issue_signature(issues: tuple[SemanticIssue, ...]) -> tuple[str, ...]:
    """결핍 집합의 신원 — 고정점 판정(:mod:`resolution.loop`)이 쓰는 값."""

    return tuple(sorted(issue.issue_id for issue in issues))


__all__ = [
    "AMBIGUOUS_COMPARISON_GRAIN",
    "AMBIGUOUS_REQUIREMENT",
    "GRAIN_ROW",
    "GRAIN_SUBJECT",
    "ISSUE_KIND_SPECS",
    "MISSING_ENTITY_VALUE",
    "MISSING_RECENT_PERIOD",
    "MISSING_SEMANTIC_VALUE",
    "UNSUPPORTED_CAPABILITY",
    "UNSUPPORTED_KINDS",
    "UNSUPPORTED_SEMANTICS",
    "UNSUPPORTED_SUBJECT",
    "IssueCandidate",
    "IssueFamily",
    "IssueKindSpec",
    "ResolutionRisk",
    "SemanticIssue",
    "SourceSpan",
    "UnknownIssueKindError",
    "issue_identity",
    "issue_kind_spec",
    "issue_signature",
]
