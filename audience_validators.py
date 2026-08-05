"""오디언스 표현 검증기 — 요구 계층에 **주입되는** 도메인 구현 4종.

이 넷은 원래 ``campaign_plan_v4._derive_audience_execution`` 안에 인라인으로 있었다.
그 함수가 217줄이었던 이유의 절반이 여기 있고, 나머지 절반은 legacy plan 키로의 투영이다.
둘을 나누면 각각이 무엇을 하는지 이름으로 드러난다.

검증기를 요구 계층(:mod:`query_pipeline.requirement`)에 두지 않는 이유는 결합이다 —
넷 전부가 이 배포의 실행 자산(카탈로그·컴파일러·언어 어댑터·리터럴 색인)을 안다. 그것을
범용 계층에 두면 그 계층은 특정 배포 없이는 import 조차 되지 않는다. 계층 가드는 패키지
내부 방향만 보므로 이 경계를 지키는 것은 가드가 아니라 **시그니처**다
(:class:`query_pipeline.requirement.validation.ExpressionValidator`).

넷의 **순서**는 계약이다. ``semantic_ir.message`` 가 첫 issue 의 문장이므로, 순서가 바뀌면
사용자가 보는 첫 문장이 바뀐다:

    1. CatalogSymbolValidator     심볼이 카탈로그에 있는가
    2. CompilerCapabilityValidator 컴파일러가 그 의미를 낼 수 있는가
    3. TemporalSpanValidator       원문의 시간 한정이 표현에 남아 있는가
    4. CanonicalClaimValidator     표현이 주장하는 값이 원문 리터럴과 맞는가
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date
from typing import Any

from pydantic import JsonValue

from query_pipeline.event_query.event_ir_bridge import to_event_ir
from query_pipeline.event_query.expressions import EventExpression, SourceEvidence
from query_pipeline.requirement.issues import RequirementIssue
from query_pipeline.requirement.validation import (
    ExpressionValidator,
    issue_from_report,
)

UNSUPPORTED = "unsupported_semantics"

# 값 없는 '최근'. 정규식으로 남아 있는 이유는 이것이 **부재**의 검출이기 때문이다 —
# calendar_window 는 있는 표현을 읽지, 없는 값을 신고하지 않는다.
_INCOMPLETE_RECENCY_RE = re.compile(r"최근(?!\s*\d)")


def _whole_query(query: str) -> SourceEvidence | None:
    """원문 전체를 근거로 삼는다 — 애플리케이션이 계산한 판정에는 좁은 구간이 없다."""
    return SourceEvidence(text=query, start=0, end=len(query)) if query else None


class CatalogSymbolValidator:
    """표현이 부르는 사건/필드 심볼이 Semantic Catalog 에 등록돼 있는가."""

    def validate(
        self,
        expression: EventExpression,
        *,
        query: str,
        literals: Sequence[JsonValue],
    ) -> tuple[RequirementIssue, ...]:
        import audience_runtime
        import event_compiler

        condition = to_event_ir(expression)
        catalog = audience_runtime.resolve_audience_catalog()
        unknown_sources = event_compiler.unsupported_events(
            condition, dict(catalog.compiler_events)
        )
        unknown_fields = event_compiler.unsupported_fields(
            condition,
            dict(catalog.compiler_events),
            dict(catalog.compiler_fields),
        )
        return tuple(
            issue_from_report(
                code=UNSUPPORTED,
                argument=symbol,
                message=f"Semantic Catalog에 등록되지 않은 심볼입니다: {symbol}",
                evidence=_whole_query(query),
            )
            for symbol in [*unknown_sources, *unknown_fields]
        )


class CompilerCapabilityValidator:
    """SQL 컴파일러가 이 표현을 실제로 낼 수 있는가.

    기준일을 생성자로 받는다. 검증과 SQL 생성이 각자 ``date.today()`` 를 부르면 달 경계에서
    서로 다른 달을 확정할 수 있고, 월 단위 컬럼에서는 그것이 곧 다른 오디언스다.
    """

    def __init__(self, *, as_of: date | None = None) -> None:
        self._as_of = as_of

    def validate(
        self,
        expression: EventExpression,
        *,
        query: str,
        literals: Sequence[JsonValue],
    ) -> tuple[RequirementIssue, ...]:
        import audience_runtime
        import event_compiler

        catalog = audience_runtime.resolve_audience_catalog()
        capability = event_compiler.validate_compiler_capability(
            to_event_ir(expression),
            context=catalog.compile_context(literals=True, today=self._as_of),
        )
        if capability.status == event_compiler.CAPABILITY_SUPPORTED:
            return ()
        return tuple(
            issue_from_report(
                code=UNSUPPORTED,
                argument=str(issue.symbol or issue.code),
                message="Canonical Event IR을 현재 SQL compiler가 표현하지 못합니다.",
                evidence=_whole_query(query),
            )
            for issue in capability.issues or ()
        )


class TemporalSpanValidator:
    """원문에 있던 시간 한정이 표현에서 사라지지 않았는가 + 값 없는 '최근'은 결핍인가.

    두 판정이 한 검증기에 있는 이유는 둘 다 같은 실패를 막기 때문이다 — 기간 조건이
    사라진 채 성공하면 '최근 30일 구매'가 **전체 이력 구매**가 된다.
    """

    def __init__(
        self,
        *,
        as_of: date | None = None,
        scalar_literal_spans: Sequence[tuple[int, int]] = (),
    ) -> None:
        self._as_of = as_of
        # 애플리케이션 소유 계약이 **시간이 아니라 스칼라 임계값임을 증명한** 원문 구간.
        # '구매주기가 30일 이하'의 '30일' 이 그것이다 — 그 구간에 TimeFilter 가 없는 것은
        # 소실이 아니라 정상이다. 비어 있는 것이 기본이므로 기존 판정은 그대로다.
        self._scalar_literal_spans = tuple(scalar_literal_spans)

    def validate(
        self,
        expression: EventExpression,
        *,
        query: str,
        literals: Sequence[JsonValue],
    ) -> tuple[RequirementIssue, ...]:
        import event_ir

        condition = to_event_ir(expression)
        issues: list[RequirementIssue] = []
        try:
            import event_parser  # 순환 없는 language adapter

            expected = event_parser.source_time_span_count(
                query, today=self._as_of, masked_spans=self._scalar_literal_spans
            )
        except (ImportError, ValueError):
            expected = 0
        if expected > event_ir.count_time_constraints(condition):
            issues.append(
                issue_from_report(
                    code="validation_mismatch",
                    argument="period",
                    message=(
                        "원문에 있는 기간 조건이 canonical audience expression에서"
                        " 누락되었습니다."
                    ),
                    evidence=_whole_query(query),
                )
            )

        signed_atoms = list(event_ir.iter_signed_atoms(condition))
        for match in _INCOMPLETE_RECENCY_RE.finditer(query):
            covered_atom = next(
                (
                    atom
                    for atom, _negated in signed_atoms
                    if atom.evidence is not None
                    and atom.evidence.start <= match.start() < atom.evidence.end
                ),
                None,
            )
            if covered_atom is not None and event_ir.time_windows(covered_atom):
                continue
            issues.append(
                issue_from_report(
                    code="missing_argument",
                    argument="period",
                    message="'최근'의 범위를 확정할 기간 값이 필요합니다.",
                    evidence=SourceEvidence(
                        text=match.group(0), start=match.start(), end=match.end()
                    ),
                )
            )
        return tuple(issues)


class CanonicalClaimValidator:
    """표현이 주장하는 값이 원문에서 뽑은 리터럴 색인과 맞는가."""

    def validate(
        self,
        expression: EventExpression,
        *,
        query: str,
        literals: Sequence[JsonValue],
    ) -> tuple[RequirementIssue, ...]:
        import audience_runtime
        import canonical_audience_claims

        reports = canonical_audience_claims.canonical_claim_issues(
            query,
            to_event_ir(expression),
            list(literals),
            audience_runtime.catalog_snapshot(),
        )
        return tuple(_issue_from_legacy_report(report) for report in reports)


def _issue_from_legacy_report(report: dict[str, Any]) -> RequirementIssue:
    """``canonical_audience_claims`` 가 내는 기존 표기 dict → 구조화된 결핍."""
    evidence = report.get("evidence")
    return issue_from_report(
        code=str(report.get("code") or "validation_mismatch"),
        argument=str(report.get("argument") or "audience_expression"),
        message=str(report.get("message") or ""),
        evidence=(
            SourceEvidence(
                text=str(evidence.get("text")),
                start=int(evidence.get("start", 0)),
                end=int(evidence.get("end", 0)),
            )
            if isinstance(evidence, dict) and evidence.get("text")
            else None
        ),
    )


def audience_validators(
    *,
    as_of: date | None = None,
    scalar_literal_spans: Sequence[tuple[int, int]] = (),
) -> tuple[ExpressionValidator, ...]:
    """오디언스 경로가 쓰는 검증기 묶음. **순서가 계약이다**(모듈 docstring 참조)."""
    return (
        CatalogSymbolValidator(),
        CompilerCapabilityValidator(as_of=as_of),
        TemporalSpanValidator(as_of=as_of, scalar_literal_spans=scalar_literal_spans),
        CanonicalClaimValidator(),
    )


__all__ = [
    "UNSUPPORTED",
    "CanonicalClaimValidator",
    "CatalogSymbolValidator",
    "CompilerCapabilityValidator",
    "TemporalSpanValidator",
    "audience_validators",
]
