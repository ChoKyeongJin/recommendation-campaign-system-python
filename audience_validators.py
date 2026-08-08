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
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

if TYPE_CHECKING:
    # 런타임 import 는 검증기 안에서 지연시킨다(모듈 결합 회피, 아래 validate 참고).
    # 타입만 여기서 받으므로 `from __future__ import annotations` 와 함께 런타임 비용은 0이다.
    from event_compiler import CompilerCapabilityIssue

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


def _capability_message(issue: CompilerCapabilityIssue) -> str:
    """컴파일 불가 사유를 모델이 읽는 문장까지 옮긴다.

    고정 문장만 돌려주면 '무엇을 고쳐야 하는가'가 사라진다 — 실측(2026-08-07)에서 관계 스코프
    위반이 이 지점에서 지워져, 모델이 같은 식을 한 번 더 낸 뒤 '표현 불가'로 후퇴했다.
    """

    parts = ["Canonical Event IR을 현재 SQL compiler가 표현하지 못합니다."]
    if issue.reason:
        parts.append(f"reason={issue.reason}")
    if issue.detail:
        parts.append(issue.detail)
    return " ".join(parts)


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
                message=_capability_message(issue),
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
        default_period: tuple[int, str] | None = None,
    ) -> None:
        self._as_of = as_of
        # 애플리케이션 소유 계약이 **시간이 아니라 스칼라 임계값임을 증명한** 원문 구간.
        # '구매주기가 30일 이하'의 '30일' 이 그것이다 — 그 구간에 TimeFilter 가 없는 것은
        # 소실이 아니라 정상이다. 비어 있는 것이 기본이므로 기존 판정은 그대로다.
        self._scalar_literal_spans = tuple(scalar_literal_spans)
        # 이 배포가 기간 없는 '최근'에 부여한 기본 기간(값·단위). ``None`` 이면 정책이 꺼져
        # 있다는 뜻이고 판정은 종전과 **완전히 같다**. 값이 있으면 그 창 하나만 결핍을 메울 수
        # 있다 — 모델이 지어낸 다른 창(30일)은 여전히 결핍이다.
        self._default_period = default_period

    def _default_windows(self, condition: Any) -> list[Any]:
        """이 표현이 들고 있는 창 중 **배포 기본 기간과 같은 값**만(정책이 꺼져 있으면 빈 목록)."""
        if self._default_period is None:
            return []
        import event_ir

        value, unit = self._default_period
        return [
            window
            for window in event_ir.time_windows(condition)
            if isinstance(window, event_ir.RollingWindow)
            and window.value == value
            and event_ir.canonical_unit(window.unit) == unit
        ]

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
        # 이 배포의 기본 기간과 **정확히 같은** 창들. 표지 하나가 하나씩만 소비하므로,
        # 창이 조용히 사라진 표현은 여전히 결핍으로 남는다.
        default_windows = self._default_windows(condition)
        import audience_issue_contract  # 지연 import(모듈 결합 회피 — 위 규약)

        for match in _INCOMPLETE_RECENCY_RE.finditer(query):
            if audience_issue_contract.period_span_is_observation_selector(
                query, (match.start(), match.end())
            ):
                # 이 '최근'은 기간이 아니라 **관측 선택자**로 해석됐다('최근 상태' = 최신 관측의
                # 값). 기간을 요구하지 않는 낱말에 결핍을 만들지 않는다 — 만든 뒤 취소하지
                # 않는 것이 계약이다(I4).
                continue
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
                # 이 표지를 덮은 원자가 스스로 창을 들고 있다. 그 창이 기본 기간과 같다면
                # 여기서 이미 쓰인 것이므로 다른 표지가 재사용하지 못하게 뺀다.
                for window in self._default_windows(covered_atom):
                    if window in default_windows:
                        default_windows.remove(window)
                continue
            if default_windows:
                # 근거 구간이 이 표지를 덮지 않아도 표현은 기본 기간을 실제로 들고 있다.
                # 모델의 evidence 배치는 신뢰할 수 없지만 **창의 존재**는 사실이다.
                default_windows.pop()
                continue
            if audience_issue_contract.period_span_owned_by_lowered_clause(
                query, (match.start(), match.end()), today=self._as_of
            ):
                # 이 표지가 붙은 절이 **스스로 창을 확정**한다(월 스냅샷 전이의 관측 칸 같은).
                # 그런 자리의 '최근'은 결핍이 아니라 그 선택의 동어반복이다.
                #
                # 근거를 '표지를 덮는 evidence'로만 보던 동안 이 자리는 통과할 수 없었다 —
                # 전이의 근거는 '승급'이라 문장 앞의 '최근'을 덮을 수 없기 때문이다. 그래서
                # 모델이 옳은 전이 표현을 내도 같은 결핍이 다시 붙어 재시도가 소진됐다
                # (실측 2026-08-08 라이브: 3회 시도 전부 이 자리에서 반려).
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

    def __init__(self, *, as_of: date | None = None) -> None:
        # 시간 의무 재판정이 낮춤과 **같은 기준일**을 써야 한다. 주지 않으면 실행 시점으로
        # 떨어져 상대 시점 표현이 다른 달로 읽히고, 컴파일된 조건이 미컴파일로 보인다.
        self._as_of = as_of

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
            today=self._as_of,
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


def configured_default_period() -> tuple[int, str] | None:
    """이 배포가 기간 없는 '최근'에 부여한 기본 기간. 설정이 없으면 ``None``(정책 꺼짐).

    값을 여기서 만들지 않고 :mod:`default_period_policy` 에 묻는 이유는 **단일 소유**다.
    검증기가 자기 기본값을 들고 있으면 "구조화기가 채운 창"과 "검증기가 인정하는 창"이
    서로 다른 말을 할 수 있고, 그 어긋남은 정확히 옳은 재구조화가 거부되는 모양으로 나타난다.
    """
    import default_period_policy  # 지연 import: 정책 모듈이 query_structurer 를 끌어온다.

    period = default_period_policy.resolve_default_period()
    return None if period is None else (period.value, period.unit)


def audience_validators(
    *,
    as_of: date | None = None,
    scalar_literal_spans: Sequence[tuple[int, int]] = (),
    default_period: tuple[int, str] | None = None,
) -> tuple[ExpressionValidator, ...]:
    """오디언스 경로가 쓰는 검증기 묶음. **순서가 계약이다**(모듈 docstring 참조).

    ``default_period`` 를 주지 않으면 배포 설정을 그대로 따른다 — 설정이 없는 배포에서는
    ``None`` 이므로 판정이 종전과 같다.
    """
    resolved_period = (
        default_period if default_period is not None else configured_default_period()
    )
    return (
        CatalogSymbolValidator(),
        CompilerCapabilityValidator(as_of=as_of),
        TemporalSpanValidator(
            as_of=as_of,
            scalar_literal_spans=scalar_literal_spans,
            default_period=resolved_period,
        ),
        CanonicalClaimValidator(as_of=as_of),
    )


__all__ = [
    "UNSUPPORTED",
    "CanonicalClaimValidator",
    "CatalogSymbolValidator",
    "CompilerCapabilityValidator",
    "TemporalSpanValidator",
    "audience_validators",
    "configured_default_period",
]
