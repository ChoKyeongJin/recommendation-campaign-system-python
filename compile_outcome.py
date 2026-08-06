"""컴파일 반환 타입의 **총체화** — 빌더가 ``None`` 을 돌려주지 않게 한다.

배경(실측 2026-08-06)
---------------------
``구매주기가 30일 이하인 회원`` 이 ``no_sql_candidates`` 로 끝났다. 그 사유가 말해 주는 것은
"후보가 0개다" 뿐이다 — **어느 단계**에서 **어떤 의미 요구**를 처리하지 못했는지는 아무 데도
없다. 원인은 반환 타입이다. 빌더가 ``dict | None`` 을 돌려주면 ``None`` 하나에
"지원하지 않는다" · "컴파일에 실패했다" · "해당 없음" 세 가지가 뭉친다. 호출자는 그 셋을
구분할 방법이 없으므로 가장 거친 사유로 뭉갠다.

여기서는 셋을 타입으로 가른다::

    CompileOutcome = Candidate | ExplicitUnsupported | CompileFailure | NotApplicable

``NotApplicable`` 은 "이 빌더의 일이 아니다"이고, 나머지 셋은 전부 **이름을 가진 귀결**이다.
실패 코드만 보고도 단계·코드·요구사항 id 를 알 수 있어야 한다는 것이 이 모듈의 계약이다.

실행: python -m pytest tests/test_compile_outcome_totality.py -q
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeAlias

COMPILE_OUTCOMES_KEY = "compile_outcomes"

# 단계 이름(닫힌 집합). 실패가 어디서 났는지 이 어휘로만 말한다.
STAGE_SEMANTIC_LOWERING = "semantic_lowering"
STAGE_AUDIENCE_LOWERING = "audience_lowering"
STAGE_PROJECTION_LOWERING = "projection_lowering"
STAGE_RULES_FALLBACK = "rules_fallback"
STAGE_IR_SCHEMA = "ir_schema"
STAGE_CAPABILITY = "capability"

STAGES: frozenset[str] = frozenset({
    STAGE_SEMANTIC_LOWERING,
    STAGE_AUDIENCE_LOWERING,
    STAGE_PROJECTION_LOWERING,
    STAGE_RULES_FALLBACK,
    STAGE_IR_SCHEMA,
    STAGE_CAPABILITY,
})


class CompileOutcomeError(ValueError):
    """귀결 선언이 계약을 어겼다(알 수 없는 단계 등)."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """컴파일이 성공했다. ``payload`` 는 기존 후보 dict(호환 유지)."""

    payload: Mapping[str, Any]
    receipts: tuple[Mapping[str, Any], ...] = ()
    requirement_ids: tuple[str, ...] = ()

    status = "candidate"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "candidate_id": self.payload.get("id"),
            "receipts": [dict(item) for item in self.receipts],
            "requirement_ids": list(self.requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class ExplicitUnsupported:
    """의미는 읽혔지만 그 뜻을 실행할 자산이 없다. **정직한 미지원**이다."""

    stage: str
    code: str
    requirement_ids: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    status = "explicit_unsupported"

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise CompileOutcomeError(f"unknown compile stage: {self.stage!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "code": self.code,
            "requirement_ids": list(self.requirement_ids),
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class CompileFailure:
    """처리할 수 있어야 하는데 못 했다. 미지원과 다르다 — 이쪽은 **우리 쪽 결함**이다."""

    stage: str
    code: str
    requirement_ids: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    status = "compile_failure"

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise CompileOutcomeError(f"unknown compile stage: {self.stage!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "code": self.code,
            "requirement_ids": list(self.requirement_ids),
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class NotApplicable:
    """이 빌더가 다룰 입력이 아니다(실패가 아니다).

    ``None`` 이 뭉뚱그리던 세 뜻 중 이것만이 '아무 일도 없었음'이다. 나머지 둘과 구분되므로
    호출자는 "빌더가 전부 해당 없음"과 "전부 실패"를 다르게 말할 수 있다.
    """

    stage: str
    reason: str = "input_not_owned_by_this_builder"

    status = "not_applicable"

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise CompileOutcomeError(f"unknown compile stage: {self.stage!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "stage": self.stage, "reason": self.reason}


CompileOutcome: TypeAlias = Candidate | ExplicitUnsupported | CompileFailure | NotApplicable


def record(plan: dict[str, Any] | None, outcome: CompileOutcome) -> CompileOutcome:
    """귀결을 플랜에 남긴다. 후보를 못 낸 이유가 **응답까지** 살아 있게 하는 유일한 경로다."""
    if isinstance(plan, dict):
        ledger = plan.setdefault(COMPILE_OUTCOMES_KEY, [])
        if isinstance(ledger, list):
            ledger.append(outcome.to_dict())
    return outcome


def outcomes(plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(plan, Mapping):
        return []
    raw = plan.get(COMPILE_OUTCOMES_KEY)
    return [dict(item) for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def blocking_outcome(plan: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """후보가 없을 때 그 이유를 말하는 **가장 구체적인** 귀결.

    우선순위: 명시적 미지원 → 컴파일 실패 → 없음. ``not_applicable`` 은 이유가 아니다.
    """
    recorded = outcomes(plan)
    for wanted in (ExplicitUnsupported.status, CompileFailure.status):
        for item in recorded:
            if item.get("status") == wanted:
                return item
    return None


def failure_reason(plan: Mapping[str, Any] | None, fallback: str) -> str:
    """``no_sql_candidates`` 대신 쓸 구체 사유. 없으면 fallback 그대로.

    형식은 ``<stage>:<code>`` 다 — 운영 로그에서 단계와 코드를 한 번에 읽는다.
    """
    item = blocking_outcome(plan)
    if item is None:
        return fallback
    stage, code = item.get("stage"), item.get("code")
    return f"{stage}:{code}" if stage and code else fallback


def record_silent_absence(
    plan: dict[str, Any] | None,
    *,
    recognized_symbols: Sequence[str] = (),
    audience_expression_present: bool = False,
) -> CompileOutcome | None:
    """어떤 빌더도 귀결을 남기지 않은 상태 자체에 이름을 붙인다.

    구조화가 표현을 못 만들어 빌더가 **아예 돌지 않은** 경우가 여기다(실측 #7: LLM 구조화
    실패 후 rules 폴백이 후보를 세우지 못했고, 응답에는 ``no_sql_candidates`` 만 남았다).
    이미 다른 귀결이 있으면 아무것도 하지 않는다 — 더 구체적인 사실이 이긴다.
    """
    if blocking_outcome(plan) is not None:
        return None
    return record(
        plan,
        CompileFailure(
            stage=STAGE_RULES_FALLBACK,
            code="required_candidate_not_constructed",
            details={
                "recognized_symbols": list(recognized_symbols)[:8],
                "audience_expression_present": audience_expression_present,
            },
        ),
    )


def requirement_ids(plan: Mapping[str, Any] | None) -> list[str]:
    """차단 귀결이 지목한 요구사항 id 들(진단 응답용)."""
    item = blocking_outcome(plan)
    if item is None:
        return []
    ids = item.get("requirement_ids")
    return [str(value) for value in ids] if isinstance(ids, Sequence) and not isinstance(ids, str) else []


__all__ = [
    "COMPILE_OUTCOMES_KEY",
    "STAGES",
    "STAGE_AUDIENCE_LOWERING",
    "STAGE_CAPABILITY",
    "STAGE_IR_SCHEMA",
    "STAGE_PROJECTION_LOWERING",
    "STAGE_RULES_FALLBACK",
    "STAGE_SEMANTIC_LOWERING",
    "Candidate",
    "CompileFailure",
    "CompileOutcome",
    "CompileOutcomeError",
    "ExplicitUnsupported",
    "NotApplicable",
    "blocking_outcome",
    "failure_reason",
    "outcomes",
    "record",
    "record_silent_absence",
    "requirement_ids",
]
