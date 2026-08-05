"""복합 집계 lowering 의 고정 계약 — IR 모양과 방언별 SQL.

두 축을 함께 잰다.

* 회원별 스칼라 지표 9종 — ``Exists(Filter(Source, Comparison(FieldRef, Literal)))``
* 캠페인당 평균 구매금액 — ``SUM * 1.0 / NULLIF(COUNT(DISTINCT (CAMP_ID, CAMP_EXEC_NO)), 0)``

여기서 고정하는 것은 문자열이 아니라 **뜻**이다. 특히 캠페인 평균은 ``AVG(BUY_AMT)`` 로
바뀌면 값이 다르므로(반응 행당 평균), 분자·분모·0 분모 처리·정수 나눗셈 방지를 각각 잰다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import campaign_metric_claims  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import member_scalar_metrics  # noqa: E402
import sql_dialect  # noqa: E402

DIALECTS = ("tsql", "mysql", "postgres", "ansi")

CAMPAIGN_AVERAGE_QUERY = "캠페인별 구매반응 금액이 평균 10만 원 이상인 회원"

# (지표 id, 연산자, 임계값, 물리 컬럼)
PROFILE_SCALAR_CASES: tuple[tuple[str, str, int, str], ...] = (
    ("member_scalar_activity_month_cnt", ">=", 6, "ACTIVITY_MONTH_CNT"),
    ("member_scalar_buy_cycle", "<=", 30, "BUY_CYCLE"),
    ("member_scalar_buy_product_cnt", ">=", 3, "BUY_PRODUCT_CNT"),
    ("member_scalar_max_buy_amt", ">=", 300000, "MAX_BUY_AMT"),
    ("member_scalar_mean_buy_amt", ">=", 50000, "MEAN_BUY_AMT"),
    ("member_scalar_min_buy_amt", ">=", 1000, "MIN_BUY_AMT"),
    ("member_scalar_total_buy_amt", ">=", 100000, "TOTAL_BUY_AMT"),
    ("member_scalar_total_buy_cnt", ">=", 5, "TOTAL_BUY_CNT"),
    ("member_scalar_total_buy_qty", ">=", 10, "TOTAL_BUY_QTY"),
)


@pytest.fixture(scope="module")
def catalog():
    return audience_runtime.resolve_audience_catalog()


def _sql(expression: event_ir.Condition, catalog, dialect: str) -> str:
    context = catalog.compile_context(
        literals=True, dialect=sql_dialect.get_dialect(dialect)
    )
    return event_compiler.compile_expression(expression, context=context).sql


def _campaign_average_expression(catalog) -> event_ir.Condition:
    """모델이 내는 행당 평균 표현 → 카탈로그 선언이 재작성한 복합 집계식."""

    span = "10만 원 이상"
    start = CAMPAIGN_AVERAGE_QUERY.index(span)
    model_expression = {
        "type": "comparison",
        "operator": ">=",
        "left": {
            "type": "aggregate",
            "function": "avg",
            "relation": {"type": "source", "name": "campaign_purchase_response"},
            "expression": {"type": "field", "name": "campaign_purchase_response.amount"},
            "distinct": False,
        },
        "right": {"type": "literal", "value": 100000},
        "evidence": {"text": span, "start": start, "end": start + len(span)},
    }
    claim = campaign_metric_claims.detect_campaign_average_claim(
        CAMPAIGN_AVERAGE_QUERY,
        model_expression,
        [],
        audience_runtime.catalog_snapshot(),
    )
    assert claim is not None and claim["expression"] is not None
    return event_ir.condition_from_dict(claim["expression"])


# ── 회원별 스칼라 지표 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("metric_id", "operator", "threshold", "column"),
    PROFILE_SCALAR_CASES,
    ids=[case[0] for case in PROFILE_SCALAR_CASES],
)
def test_profile_scalar_metric_ir_shape(
    metric_id: str, operator: str, threshold: int, column: str, catalog
) -> None:
    """IR 스냅샷 — 새 노드 없이 기존 조합 하나로 표현된다."""

    predicate = member_scalar_metrics.lower_member_scalar_metric(
        catalog, metric_id, operator=operator, value=threshold
    )
    assert predicate.expression.to_dict() == {
        "type": "exists",
        "relation": {
            "type": "filter",
            "relation": {"type": "source", "name": metric_id},
            "where": {
                "type": "comparison",
                "operator": operator,
                "left": {"type": "field", "name": f"{metric_id}.value"},
                "right": {"type": "literal", "value": threshold},
                "evidence": None,
            },
        },
        "evidence": None,
    }
    # 회원별 스칼라는 집계도 순위도 아니다 — capability 파생이 그것을 말한다.
    assert event_ir.expression_capabilities(predicate.expression) == frozenset()


@pytest.mark.parametrize(
    ("metric_id", "operator", "threshold", "column"),
    PROFILE_SCALAR_CASES,
    ids=[case[0] for case in PROFILE_SCALAR_CASES],
)
@pytest.mark.parametrize("dialect", DIALECTS)
def test_profile_scalar_metric_sql(
    metric_id: str, operator: str, threshold: int, column: str, dialect: str, catalog
) -> None:
    """방언별 golden SQL — 회원 상관 + 최신 월 고정 + 임계 비교."""

    predicate = member_scalar_metrics.lower_member_scalar_metric(
        catalog, metric_id, operator=operator, value=threshold
    )
    assert _sql(predicate.expression, catalog, dialect) == (
        "EXISTS (SELECT 1 FROM CRM_MB_MONTHCRMINFO MS "
        "WHERE MS.MEMBER_NO = B.MEMBER_NO "
        "AND MS.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO) "
        f"AND MS.{column} {operator} {threshold})"
    )


def test_profile_scalar_receipt_states_the_policies(catalog) -> None:
    """정책은 코드에 숨지 않고 영수증에 이름으로 남는다."""

    predicate = member_scalar_metrics.lower_member_scalar_metric(
        catalog, "member_scalar_buy_cycle", operator="<=", value=30
    )
    assert predicate.receipt["kind"] == "member_scalar"
    assert predicate.receipt["cardinality"] == "scalar"
    assert predicate.receipt["null_behavior"] == "exclude"
    assert predicate.receipt["zero_row_behavior"] == "exclude"
    assert predicate.receipt["value_field"] == "member_scalar_buy_cycle.value"


# ── 캠페인당 평균 구매금액 ─────────────────────────────────────────────────────────


def test_campaign_average_is_a_ratio_not_an_avg_aggregate(catalog) -> None:
    """IR 스냅샷 — 분자 SUM, 분모 다중 컬럼 COUNT DISTINCT, 0 분모 가드."""

    expression = _campaign_average_expression(catalog)
    assert expression.to_dict() == {
        "type": "comparison",
        "operator": ">=",
        "left": {
            "type": "arithmetic",
            "operator": "/",
            "left": {
                "type": "arithmetic",
                "operator": "*",
                "left": {
                    "type": "aggregate",
                    "function": "sum",
                    "distinct": False,
                    "expression": {
                        "type": "field",
                        "name": "campaign_purchase_response.amount",
                    },
                    "relation": {
                        "type": "source",
                        "name": "campaign_purchase_response",
                    },
                },
                "right": {"type": "literal", "value": 1.0},
            },
            "right": {
                "type": "null_if",
                "expression": {
                    "type": "aggregate",
                    "function": "count",
                    "distinct": True,
                    "expression": {
                        "type": "tuple",
                        "items": [
                            {
                                "type": "field",
                                "name": "campaign_purchase_response.campaign_id",
                            },
                            {
                                "type": "field",
                                "name": "campaign_purchase_response.execution_no",
                            },
                        ],
                    },
                    "relation": {
                        "type": "source",
                        "name": "campaign_purchase_response",
                    },
                },
                "value": {"type": "literal", "value": 0},
            },
        },
        "right": {"type": "literal", "value": 100000},
        "evidence": {
            "text": "10만 원 이상",
            "start": CAMPAIGN_AVERAGE_QUERY.index("10만 원 이상"),
            "end": CAMPAIGN_AVERAGE_QUERY.index("10만 원 이상") + len("10만 원 이상"),
        },
    }
    # 행당 평균 집계는 표현 어디에도 없다.
    assert not any(
        isinstance(node, event_ir.Aggregate) and node.function == "avg"
        for node in event_ir.walk(expression)
    )


def test_campaign_average_declares_the_capabilities_it_uses(catalog) -> None:
    """선언한 capability 와 표현이 실제로 쓰는 capability 가 어긋나지 않는다."""

    expression = _campaign_average_expression(catalog)
    used = event_ir.expression_capabilities(expression)
    assert used == frozenset({
        "scalar.arithmetic",
        "scalar.null_if",
        "scalar.safe_divide",
        "scalar.tuple",
        "aggregate.scalar",
        "aggregate.count_distinct",
        "aggregate.multi_column_count_distinct",
        "aggregate.derived_expression",
    })
    declared = (
        audience_runtime.catalog_snapshot()["metrics"]["campaign_purchase_amount"]
        ["claim_synthesis"]["average_per_campaign"]["required_capabilities"]
    )
    assert set(declared) == used
    assert event_compiler.unsupported_capabilities(expression) == ()


@pytest.mark.parametrize("dialect", DIALECTS)
def test_campaign_average_sql_per_dialect(dialect: str, catalog) -> None:
    """방언별 golden SQL. 다중 컬럼 distinct 는 **네 방언 모두** DISTINCT 서브쿼리로 낮춘다.

    네이티브 문법을 쓰지 않는 것이 결정이다 — MySQL 의 ``COUNT(DISTINCT a, b)`` 는 인자 중
    하나라도 NULL 인 행을 세지 않고 PostgreSQL 의 행 값은 센다. 같은 IR 이 방언에 따라 다른
    수를 세면 그것은 최적화가 아니라 조용한 오답이다.
    """

    sql = _sql(_campaign_average_expression(catalog), catalog, dialect)
    coalesce = "ISNULL" if dialect == "tsql" else "COALESCE"
    fact = (
        "MCS_CAMP_MBR_RSPN_FT R "
        "INNER JOIN Z_CAMPAIGN ZC ON ZC.CAMP_ID = R.CAMP_ID "
        "AND ZC.CAMP_EXEC_NO = R.CAMP_EXEC_NO"
    )
    predicates = (
        "TRY_CAST(R.MBR_NO AS BIGINT) = B.MEMBER_NO "
        "AND R.CGRP_TYPE_CD = 'T' AND R.BUY_RSPN_YN = 'Y' "
        "AND ISNULL(ZC.CANCEL_YN, 'N') = 'N'"
    )
    assert sql == (
        f"(((SELECT {coalesce}(SUM(R.BUY_AMT), 0) FROM {fact} WHERE {predicates}) * 1.0) "
        f"/ NULLIF((SELECT COUNT(*) FROM (SELECT DISTINCT R.CAMP_ID, R.CAMP_EXEC_NO "
        f"FROM {fact} WHERE {predicates}) AS ED0), 0)) >= 100000"
    )


@pytest.mark.parametrize("dialect", DIALECTS)
def test_campaign_average_sql_meaning_guards(dialect: str, catalog) -> None:
    """뜻 단위 가드 — 분자·분모·0 분모·정수 나눗셈·구분자 결합 금지."""

    sql = _sql(_campaign_average_expression(catalog), catalog, dialect)
    assert "AVG(R.BUY_AMT)" not in sql
    assert "SUM(R.BUY_AMT)" in sql
    assert "SELECT DISTINCT R.CAMP_ID, R.CAMP_EXEC_NO" in sql
    assert "NULLIF(" in sql and ", 0)" in sql
    assert "* 1.0" in sql
    # 구분자 결합 키는 값 안의 구분자·NULL 로 서로 다른 키가 충돌한다.
    assert "CONCAT(R.CAMP_ID" not in sql
    # 네이티브 다중 컬럼 문법은 NULL 규칙이 방언마다 달라 쓰지 않는다.
    assert "COUNT(DISTINCT R.CAMP_ID, R.CAMP_EXEC_NO)" not in sql


def test_campaign_average_numerator_and_denominator_share_one_relation(catalog) -> None:
    """기간 필터가 한쪽에만 걸리지 않도록 분자·분모가 같은 관계를 물려받는다."""

    comparison = _campaign_average_expression(catalog)
    assert isinstance(comparison, event_ir.Comparison)
    ratio = comparison.left
    assert isinstance(ratio, event_ir.Arithmetic) and ratio.operator == "/"
    numerator = ratio.left.left  # type: ignore[union-attr]
    denominator = ratio.right.expression  # type: ignore[union-attr]
    assert isinstance(numerator, event_ir.Aggregate)
    assert isinstance(denominator, event_ir.Aggregate)
    assert numerator.relation == denominator.relation


def test_row_average_request_without_a_grain_term_is_left_alone() -> None:
    """grain 표면어가 없으면 행당 평균 요청이므로 재작성하지 않는다(전면 차단이 아니다)."""

    query = "구매반응 금액이 평균 10만 원 이상인 회원"
    model_expression = {
        "type": "comparison",
        "operator": ">=",
        "left": {
            "type": "aggregate",
            "function": "avg",
            "relation": {"type": "source", "name": "campaign_purchase_response"},
            "expression": {"type": "field", "name": "campaign_purchase_response.amount"},
            "distinct": False,
        },
        "right": {"type": "literal", "value": 100000},
        "evidence": {"text": "평균", "start": 10, "end": 12},
    }
    assert (
        campaign_metric_claims.detect_campaign_average_claim(
            query, model_expression, [], audience_runtime.catalog_snapshot()
        )
        is None
    )


# ── IR 대수 자체의 계약 ────────────────────────────────────────────────────────────


def test_tuple_is_only_valid_for_count_distinct() -> None:
    """행 값은 '몇 개의 서로 다른 키인가'를 세는 자리에서만 뜻이 있다."""

    items = (event_ir.FieldRef("purchase.order_id"), event_ir.FieldRef("purchase.amount"))
    with pytest.raises(event_ir.IrSchemaError):
        event_ir.Aggregate(
            function="sum",
            relation=event_ir.Source(name="purchase"),
            expression=event_ir.Tuple(items=items),
        )
    with pytest.raises(event_ir.IrSchemaError):
        event_ir.Aggregate(
            function="count",
            relation=event_ir.Source(name="purchase"),
            expression=event_ir.Tuple(items=items),
            distinct=False,
        )
    # count(distinct tuple) 만 유효하다.
    event_ir.Aggregate(
        function="count",
        relation=event_ir.Source(name="purchase"),
        expression=event_ir.Tuple(items=items),
        distinct=True,
    )


def test_tuple_needs_at_least_two_items() -> None:
    with pytest.raises(event_ir.IrSchemaError):
        event_ir.Tuple(items=(event_ir.FieldRef("purchase.order_id"),))
    with pytest.raises(event_ir.IrSchemaError):
        event_ir.Tuple(
            items=(
                event_ir.FieldRef("purchase.order_id"),
                event_ir.Tuple(
                    items=(
                        event_ir.FieldRef("purchase.amount"),
                        event_ir.FieldRef("purchase.order_id"),
                    )
                ),
            )
        )


def test_bare_tuple_outside_a_distinct_count_fails_closed(catalog) -> None:
    """행 값이 엉뚱한 자리에 있으면 조용히 이어 붙이지 않고 컴파일이 멈춘다."""

    condition = event_ir.Comparison(
        operator="=",
        left=event_ir.Tuple(
            items=(
                event_ir.FieldRef("campaign_purchase_response.campaign_id"),
                event_ir.FieldRef("campaign_purchase_response.execution_no"),
            )
        ),
        right=event_ir.Literal(value=1),
    )
    with pytest.raises(event_compiler.SqlCompileError):
        _sql(condition, catalog, "tsql")
