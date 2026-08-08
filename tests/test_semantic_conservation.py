"""**의미 보존 계약** — 원문에서 잡힌 뜻은 중간에 조용히 증발하지 않는다.

이 파일이 재는 것은 SQL 문자열이 아니다. SQL 을 비교하면 "무엇이 나왔는가"는 알 수 있어도
"무엇이 사라졌는가"는 알 수 없고, 이 저장소가 반복해서 다친 곳은 후자였다(실측 2026-08-08:
``2026년 2월과 3월의 구매금액차이가 10% 이상 증가`` 의 계획 SQL 이 임계 없는
``…증가`` 의 계획 SQL 과 **바이트 동일**했다).

핵심 불변식은 하나다::

    detected source requirements == satisfied + explicitly unsupported

곧 원장에 잡힌 모든 요구는 **영수증으로 방면되거나 미귀결로 남아 출고를 막거나** 둘 중
하나여야 하고, 그 사이의 제3상태('계획이 있으니 지원되는 셈')는 존재하지 않아야 한다.
그 제3상태를 만든 것이 "span 이 겹치면 지원됨"이라는 판정이었다.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

import canonical_audience_claims  # noqa: E402
import event_ir  # noqa: E402
import lowering_planner  # noqa: E402
import semantic_requirements  # noqa: E402

TODAY = date(2026, 8, 8)
CURRENT_DATE = "2026-08-08"

# 대조 코퍼스 — 같은 문장에서 **크기만** 달라진다. 방향만 있는 첫 줄이 기준선이고,
# 나머지 넷은 각각 다른 크기 문법이다.
#
# 지표 표면어로 '매출' 대신 '구매금액'을 쓰는 이유는 카탈로그다 — 이 배포의 오디언스
# 카탈로그에 '매출' 별칭이 없어 지표가 잡히지 않는다(실측). 이 파일이 재는 것은 지표
# 어휘가 아니라 크기의 보존이므로, 어휘가 있는 표면어를 쓴다.
CONTRAST_CORPUS = (
    ("2026년 2월보다 3월 구매금액 증가한 회원", None),
    ("2026년 2월보다 3월 구매금액 10% 이상 증가한 회원", ("ratio", "increase", 11, 10)),
    ("2026년 2월보다 3월 구매금액 5만원 이상 증가한 회원", ("absolute", "increase", None, None)),
    ("2026년 2월보다 3월 구매금액 2배 이상 증가한 회원", ("ratio", "increase", 2, 1)),
    ("2026년 2월보다 3월 구매금액 절반으로 감소한 회원", ("ratio", "decrease", 1, 2)),
)


def _magnitude_requirements(query: str) -> tuple:
    return tuple(
        requirement
        for requirement in semantic_requirements.capture_source_semantic_obligations(query)
        if semantic_requirements.obligation_kind(requirement)
        == semantic_requirements.CHANGE_MAGNITUDE_KIND
    )


def _sole_plan(query: str) -> lowering_planner.LoweringPlan:
    plans = [
        plan
        for plan in lowering_planner.plans_for_query(query, today=TODAY)
        if plan.obligation.kind == lowering_planner.AGGREGATE_COMPARISON
    ]
    assert plans, f"전제: 이 원문은 낮출 수 있다 — {query!r}"
    return plans[0]


# ── 1. 크기는 요구로 잡힌다(숫자형·낱말형 모두) ──────────────────────────────────────


@pytest.mark.parametrize(("query", "expected"), CONTRAST_CORPUS)
def test_change_magnitude_is_captured_as_a_source_requirement(
    query: str, expected: tuple | None
) -> None:
    """크기가 있으면 요구가 있고, 없으면 없다. 낱말형('절반')도 예외가 아니다."""
    requirements = _magnitude_requirements(query)
    if expected is None:
        assert requirements == ()
        return
    assert len(requirements) == 1
    value = dict(requirements[0].value)
    kind, direction, numerator, denominator = expected
    assert value["magnitude_kind"] == kind
    assert value["direction"] == direction
    if numerator is not None:
        assert (value["numerator"], value["denominator"]) == (numerator, denominator)


def test_wordform_magnitude_has_no_literal_binding_to_lean_on() -> None:
    """'절반'은 리터럴 원장에 흔적이 없다 — 그래서 이 축이 없으면 **조용히** 사라진다.

    숫자형은 리터럴 정산이 뒤늦게라도 막지만 낱말형은 막을 것이 없었다. 이 테스트는 그
    비대칭이 실재함을 고정해, 크기 캡처를 리터럴에 의존해 구현하려는 회귀를 막는다.
    """
    from query_structurer.semantic_ir import extract_literal_bindings

    query = "2026년 2월보다 3월 구매금액 절반으로 감소한 회원"
    kinds = {binding["kind"] for binding in extract_literal_bindings(query)}
    assert kinds == {"date_window"}
    assert len(_magnitude_requirements(query)) == 1


# ── 2. 크기는 구조에 실리고 SQL 을 바꾼다 ────────────────────────────────────────────


def test_threshold_reaches_the_obligation_and_changes_the_sql() -> None:
    """임계가 있는 문장과 없는 문장의 SQL 은 **달라야** 한다.

    수정 전에는 바이트 동일이었다. 그때 계획은 '조금이라도 증가한 전원'을 뽑으면서 자기가
    '10% 이상 증가'를 낼 수 있다고 단언했다.
    """
    bare = _sole_plan("2026년 2월보다 3월 구매금액 증가한 회원")
    scaled = _sole_plan("2026년 2월보다 3월 구매금액 10% 이상 증가한 회원")
    assert bare.obligation.threshold is None
    assert scaled.obligation.threshold is not None
    assert bare.sql != scaled.sql


@pytest.mark.parametrize(
    ("query", "expected_fragments"),
    [
        # 비율은 **교차곱**이다 — 나눗셈도 소수점도 SQL 에 없다.
        ("2026년 2월보다 3월 구매금액 10% 이상 증가한 회원", ("* 10) >= (", "* 11)", "> 0")),
        ("2026년 2월보다 3월 구매금액 20% 이상 감소한 회원", ("* 5) <= (", "* 4)", "> 0")),
        ("2026년 2월보다 3월 구매금액 2배 이상 증가한 회원", (">= (", "* 2)", "> 0")),
        ("2026년 2월보다 3월 구매금액 절반으로 감소한 회원", ("* 2) <= (", "> 0")),
        # 절대 증분은 뺄셈 비교이고 기준값 가드가 없다(0 기준에서도 정의된다).
        ("2026년 2월보다 3월 구매금액 5만원 이상 증가한 회원", (") - (", ">= 50000")),
    ],
)
def test_threshold_lowers_without_division_or_float(
    query: str, expected_fragments: tuple[str, ...]
) -> None:
    sql = _sole_plan(query).sql
    for fragment in expected_fragments:
        assert fragment in sql, f"{fragment!r} not in {sql}"
    assert "/" not in sql, f"나눗셈이 들어갔다: {sql}"
    assert "1.1" not in sql and "0.8" not in sql, f"실수 리터럴이 들어갔다: {sql}"


def test_ratio_thresholds_guard_the_zero_baseline() -> None:
    """기준값 0 은 명시적으로 막는다.

    ``ISNULL(SUM,0)`` 때문에 2월 무주문 회원은 ``0 * 10 >= 0 * 11`` 이 참이 되어 **비구매자
    전원**이 뽑힌다. '0 → 100' 을 몇 % 증가로 부를지는 정의되지 않았으므로 포함하지 않는다.
    """
    plan = _sole_plan("2026년 2월보다 3월 구매금액 10% 이상 증가한 회원")
    assert isinstance(plan.expression, event_ir.And)
    guard = plan.expression.operands[0]
    assert isinstance(guard, event_ir.Comparison)
    assert guard.operator == ">"
    assert isinstance(guard.right, event_ir.Literal) and guard.right.value == 0


@pytest.mark.parametrize(
    ("query", "operator"),
    [
        ("2026년 2월보다 3월 구매금액 10% 이상 증가한 회원", ">="),
        ("2026년 2월보다 3월 구매금액 10% 초과 증가한 회원", ">"),
        ("2026년 2월보다 3월 구매금액 20% 이상 감소한 회원", "<="),
        ("2026년 2월보다 3월 구매금액 20% 초과 감소한 회원", "<"),
    ],
)
def test_direction_owns_polarity_and_the_bound_word_only_owns_inclusiveness(
    query: str, operator: str
) -> None:
    """극성 반전 금지 — '20% **이상** 감소'의 '이상'을 ``>=`` 로 읽으면 뜻이 뒤집힌다.

    방향 낱말이 비교의 향을 정하고 경계 낱말은 포함 여부만 정한다. 이 분리가 계약이다.
    """
    plan = _sole_plan(query)
    core = plan.expression.operands[-1] if isinstance(
        plan.expression, event_ir.And
    ) else plan.expression
    assert core.operator == operator


def test_ratio_is_exact_and_never_float() -> None:
    """소수 퍼센트도 유리수로 정확히 담는다(10.5% → 221/200)."""
    magnitudes = semantic_requirements.parse_change_magnitudes(
        "2026년 2월보다 3월 구매금액 10.5% 이상 증가한 회원"
    )
    assert len(magnitudes) == 1
    assert (magnitudes[0].numerator, magnitudes[0].denominator) == (221, 200)
    assert magnitudes[0].amount is None


def test_absolute_amount_is_decimal_not_float() -> None:
    magnitudes = semantic_requirements.parse_change_magnitudes(
        "2026년 2월보다 3월 구매금액 5만원 이상 증가한 회원"
    )
    assert len(magnitudes) == 1
    assert isinstance(magnitudes[0].amount, Decimal)
    assert magnitudes[0].amount == Decimal("50000")


# ── 3. span 겹침은 지원의 근거가 아니다 ──────────────────────────────────────────────


def test_span_overlap_alone_never_asserts_support() -> None:
    """계획 hull 이 크기 표면을 덮어도, 그 크기를 소비하지 않았다면 반박하지 않는다.

    이것이 이 작업의 핵심 불변식이다. 검증 방법은 인위적이지만 의도적이다 — 임계를 지운
    의무로 계획을 세우면 hull 은 '10% 이상'을 그대로 덮는데 표현에는 그 크기가 없다.
    그 계획은 자기 구간의 요구를 소비하지 못했으므로 지원을 단언할 수 없어야 한다.
    """
    query = "2026년 2월보다 3월 구매금액 10% 이상 증가한 회원"
    obligations = lowering_planner.detect_comparison_obligations(query, today=TODAY)
    assert obligations and obligations[0].threshold is not None

    from dataclasses import replace

    stripped = replace(obligations[0], threshold=None)
    plan = lowering_planner.try_plan(stripped, query=query)
    assert plan is not None, "전제: 크기를 뺀 맨 비교는 낮춰진다"

    magnitude_span = obligations[0].threshold.source_span
    hull = plan.obligation.source_span
    assert hull[0] <= magnitude_span[0] and magnitude_span[1] <= hull[1], (
        "전제: 계획 hull 이 크기 표면을 덮는다(예전에 이것만으로 지원을 단언했다)"
    )
    unsettled = lowering_planner.unsettled_requirements(query, plan)
    assert unsettled, "hull 을 덮었다는 이유만으로 요구가 정산되면 안 된다"
    assert all(
        semantic_requirements.obligation_kind(item)
        == semantic_requirements.CHANGE_MAGNITUDE_KIND
        for item in unsettled
    )


def test_a_plan_that_consumed_its_requirements_still_asserts_support() -> None:
    """대조 — 요구를 다 소비한 계획은 그대로 반박한다(과잉 차단 금지)."""
    query = "2026년 2월보다 3월 구매금액 10% 이상 증가한 회원"
    plan = _sole_plan(query)
    assert lowering_planner.unsettled_requirements(query, plan) == ()
    span = {"start": 0, "end": len(query)}
    assert lowering_planner.plan_satisfying_span(query, span, today=TODAY) is not None


def test_bare_comparison_without_any_magnitude_is_unaffected() -> None:
    """회귀 — 크기 요구가 없으면 정산할 것도 없고 기존 동작 그대로다."""
    query = "2026년 2월보다 3월 구매금액 증가한 회원"
    plan = _sole_plan(query)
    assert _magnitude_requirements(query) == ()
    assert lowering_planner.unsettled_requirements(query, plan) == ()


# ── 4. 핵심 불변식: 검출 == 방면 + 명시적 미지원 ─────────────────────────────────────


@pytest.mark.parametrize("query", [item[0] for item in CONTRAST_CORPUS])
def test_every_detected_requirement_is_settled_or_explicitly_unsupported(
    query: str,
) -> None:
    """**의미 보존 계약.** 잡힌 요구는 방면되거나 미지원으로 남는다 — 사라지지 않는다.

    방면(satisfied)의 근거는 영수증이고, 미지원은 ``semantic_obligation_issues`` 가 낸
    신고다. 둘의 합집합이 원장 전체와 같아야 한다. 어느 쪽에도 없는 요구가 생기면
    그것이 곧 조용한 증발이다.
    """
    plans = [
        plan
        for plan in lowering_planner.plans_for_query(query, today=TODAY)
        if plan.obligation.kind == lowering_planner.AGGREGATE_COMPARISON
    ]
    assert plans, f"전제: 이 원문은 낮춰진다 — {query!r}"
    expression = plans[0].expression

    detected = {
        str(requirement.id)
        for requirement in semantic_requirements.capture_source_semantic_obligations(query)
    }
    satisfied = set(
        canonical_audience_claims.satisfied_requirement_ids(query, expression, today=TODAY)
    )
    unsupported_arguments = {
        issue["argument"]
        for issue in canonical_audience_claims.semantic_obligation_issues(
            query, expression, today=TODAY
        )
    }
    unsupported = {
        str(requirement.id)
        for requirement in semantic_requirements.capture_source_semantic_obligations(query)
        if f"source_semantics.{semantic_requirements.obligation_kind(requirement)}"
        in unsupported_arguments
    }

    assert satisfied | unsupported == detected, (
        "조용히 증발한 요구: "
        f"{sorted(detected - (satisfied | unsupported))}"
    )
    assert not (satisfied & unsupported), "한 요구가 방면과 미지원 둘 다일 수 없다"


def test_an_expression_missing_the_magnitude_is_not_settled() -> None:
    """크기가 빠진 표현은 방면되지 않는다 — 영수증은 구조 대조로만 발급된다.

    비대칭 테스트다. 같은 원문·같은 요구에서 임계를 담은 표현은 방면되고, 담지 않은 표현은
    막힌다. 면제가 넓어지지 않았다는 증거가 이 두 줄의 차이다.
    """
    query = "2026년 2월보다 3월 구매금액 10% 이상 증가한 회원"
    faithful = _sole_plan(query).expression

    from dataclasses import replace

    obligations = lowering_planner.detect_comparison_obligations(query, today=TODAY)
    stripped_plan = lowering_planner.try_plan(
        replace(obligations[0], threshold=None), query=query
    )
    assert stripped_plan is not None

    assert canonical_audience_claims.satisfied_requirement_ids(
        query, faithful, today=TODAY
    ), "크기를 담은 표현은 방면돼야 한다"
    assert not canonical_audience_claims.satisfied_requirement_ids(
        query, stripped_plan.expression, today=TODAY
    ), "크기가 빠진 표현이 방면되면 조용한 오답이 출고된다"
    assert canonical_audience_claims.semantic_obligation_issues(
        query, stripped_plan.expression, today=TODAY
    ), "방면되지 않은 요구는 명시적 신고로 남아야 한다"


# ── 5. 소유권 — 같은 리터럴을 두 의미가 청구하지 않는다 ──────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "상위 10% 회원 중 구매금액이 증가한 회원",
        "구매금액 상위 10% 회원 중 방문이 증가한 회원",
    ],
)
def test_ranking_percent_is_not_claimed_as_a_change_magnitude(query: str) -> None:
    """'상위 10%'의 10% 는 랭킹의 것이다 — 방향 낱말이 가까이 있어도 뺏지 않는다."""
    assert _magnitude_requirements(query) == ()


@pytest.mark.parametrize(
    "query",
    [
        "10만원 이상 구매한 회원",  # 방향 낱말이 없다
        "상위 10명 회원",  # 크기가 아니다
        "고객 데이터를 기반으로 분석",  # '기반으로'의 '반으로' 오탐
    ],
)
def test_magnitude_capture_has_no_false_positives(query: str) -> None:
    assert semantic_requirements.parse_change_magnitudes(query) == ()


# ── 6. 정산자는 종류를 모른다(등록되지 않은 축은 fail-close) ─────────────────────────


def test_unregistered_obligation_kinds_are_never_settled() -> None:
    """증명자가 없는 종류는 방면되지 않는다.

    새 의미축이 생겼는데 아무도 증명자를 등록하지 않으면 그 요구는 **막힌다**. 예전 구조
    (종류별 면제 분기)에서는 아무도 분기를 추가하지 않는 것이 기본값이었고, 그것이 곧
    조용한 통과였다. 기본값이 fail-close 라는 사실을 여기서 고정한다.
    """
    provers = canonical_audience_claims._OBLIGATION_SATISFACTION_PROVERS
    assert semantic_requirements.CHANGE_MAGNITUDE_KIND in provers
    assert semantic_requirements.TEMPORAL_QUALIFIER_KIND in provers
    assert provers.get("a_kind_nobody_registered") is None
