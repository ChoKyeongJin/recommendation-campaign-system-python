"""회원 상관 스칼라 집계 → 집합형 semi-join 물리 lowering 계약.

고정하는 것은 SQL 문자열이 아니라 **셋**이다.

  1. 의미 동치 — 낮춘 SQL 과 낮추지 않은 SQL 이 같은 회원 집합을 뜻한다. 두 모양이 갈리는
     유일한 지점은 "이벤트가 하나도 없는 회원"이므로, 임계값 0 에서 비교가 거짓일 때만 낮춘다.
  2. Event IR 불변 — 표현·직렬화·지문·노드 타입이 최적화 전후로 동일하다. 이건 IR 변경이
     아니라 물리 lowering 이다.
  3. 저비용 fast-path — 집계가 없는 조건은 판정 비용도, receipt 도, SQL 변화도 없다.

DB 결과집합 대조(original EXCEPT optimized / optimized EXCEPT original)는 실DB 연결이
필요해 여기서 실행하지 않는다. 여기서 고정하는 것은 그 대조가 성립하기 위한 구조 조건이다.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import sql_guard  # noqa: E402


CONTACT = "campaign_contact_success"
EXECUTION_ID = f"{CONTACT}.execution_id"
OCCURRED_AT = f"{CONTACT}.occurred_at"


def _context(**overrides):
    return audience_runtime.resolve_audience_catalog().compile_context(
        literals=True, **overrides
    )


def _contact_relation(days: int | None = 5) -> event_ir.Relation:
    relation: event_ir.Relation = event_ir.Source(name=CONTACT)
    if days is None:
        return relation
    return event_ir.Filter(
        relation=relation,
        where=event_ir.TimeFilter(
            field=event_ir.FieldRef(name=OCCURRED_AT),
            window=event_ir.RollingWindow(value=days, unit="day"),
        ),
    )


def _contact_count(
    operator: str = ">=", threshold: object = 3, *, days: int | None = 5
) -> event_ir.Comparison:
    """'최근 N일 캠페인 발송 성공 횟수가 T회 <operator>' 의 canonical 표현."""
    return event_ir.Comparison(
        operator=operator,
        left=event_ir.Aggregate(
            function="count",
            relation=_contact_relation(days),
            expression=event_ir.FieldRef(name=EXECUTION_ID),
            distinct=True,
        ),
        right=event_ir.Literal(value=threshold),
    )


def _compile(expression: event_ir.Condition, **overrides) -> str:
    return event_compiler.compile_expression(
        expression, context=_context(**overrides)
    ).sql


def _correlated(expression: event_ir.Condition) -> str:
    return _compile(expression, optimize_aggregate_membership=False)


# ── 1. 대상 사례: 최근 5일 캠페인 발송 성공 3회 이상 ──────────────────────────────


def test_target_case_becomes_one_grouped_scan_semi_join() -> None:
    sql = _compile(_contact_count())

    # 회원 semi-join — 바깥 회원 행마다 반복되는 스칼라 집계가 아니다.
    assert sql.startswith("B.MEMBER_NO IN (SELECT TRY_CAST(M.MBR_NO AS BIGINT) FROM Z_CAMP_MBR M")
    assert "GROUP BY TRY_CAST(M.MBR_NO AS BIGINT)" in sql
    assert "HAVING COUNT(DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO)) >= 3" in sql
    # 상관 술어가 남으면 최적화가 아니라 같은 계획의 다른 표기다.
    assert "= B.MEMBER_NO" not in sql
    assert sql.count("Z_CAMP_MBR") == 1


def test_every_defining_predicate_is_pushed_below_the_aggregate() -> None:
    """의미 요소가 하나라도 집계 위로 새면 다른 모집단이 된다."""
    sql = _compile(_contact_count())

    assert "M.CELL_TYPE_CD = 'T'" in sql  # 대상군
    assert "M.CONTAC_SUCC_YN = 'Y'" in sql  # 발송 성공
    assert "ISNULL(ZC.CANCEL_YN, 'N') = 'N'" in sql  # 취소 제외
    assert "ZC.CAMP_SDATE >= CONVERT(CHAR(8), DATEADD(DAY, -5, GETDATE()), 112)" in sql
    # 변환 불가 회원키는 NULL 그룹으로 모이므로 명시적으로 제외한다(IN 술어를 2값으로 유지).
    assert "TRY_CAST(M.MBR_NO AS BIGINT) IS NOT NULL" in sql


def test_distinct_execution_key_is_preserved_not_simplified() -> None:
    """복합 실행키를 COUNT(*)/COUNT(DISTINCT CAMP_ID) 로 임의 단순화하지 않는다."""
    sql = _compile(_contact_count())

    assert "COUNT(DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO))" in sql
    assert "COUNT(*)" not in sql
    assert "COUNT(DISTINCT M.CAMP_ID)" not in sql


def test_no_window_still_lowers_with_the_same_population() -> None:
    """기간이 없는 '캠페인 발송 성공 3회 이상'도 같은 구조다(창만 빠진다)."""
    sql = _compile(_contact_count(days=None))

    assert "GROUP BY TRY_CAST(M.MBR_NO AS BIGINT)" in sql
    assert "CAMP_SDATE >=" not in sql


# ── 2. 의미 동치의 경계: 0 에서 거짓인 비교만 낮춘다 ──────────────────────────────


@pytest.mark.parametrize(
    ("operator", "threshold"),
    [
        (">=", 1),
        (">=", 3),
        (">=", 4),
        (">", 0),
        (">", 2),
        ("=", 1),
        ("=", 3),
        ("!=", 0),
    ],
)
def test_thresholds_false_at_zero_are_lowered(operator: str, threshold: int) -> None:
    """이벤트가 없는 회원이 거짓이면 그룹 부재와 COUNT=0 이 같은 답을 준다."""
    sql = _compile(_contact_count(operator, threshold))

    assert sql.startswith("B.MEMBER_NO IN (")
    assert f"HAVING COUNT(DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO)) {operator} {threshold}" in sql


@pytest.mark.parametrize(
    ("operator", "threshold"),
    [
        ("=", 0),      # 0건 — 이벤트가 없는 회원이 참이다
        ("<=", 0),
        ("<=", 2),
        ("<", 5),
        (">=", 0),     # 모든 회원이 참 — semi-join 은 그 집합을 표현하지 못한다
        ("!=", 3),     # 0 != 3 은 참
    ],
)
def test_thresholds_true_at_zero_keep_the_correlated_shape(
    operator: str, threshold: int
) -> None:
    """이벤트가 없는 회원도 참인 조건은 낮추지 않는다 — semi-join 은 그 회원을 못 담는다."""
    receipts: list[dict] = []
    sql = _compile(_contact_count(operator, threshold), optimization_receipts=receipts)

    assert sql.startswith("(SELECT COUNT(")
    assert "= B.MEMBER_NO" in sql
    assert [item["reason"] for item in receipts] == [
        event_compiler.SKIP_ZERO_SENSITIVE_COMPARISON
    ]


def test_boundary_between_lowered_and_correlated_is_exactly_zero() -> None:
    """경계 한 칸: '> 0' 은 낮추고 '>= 0' 은 낮추지 않는다."""
    assert _compile(_contact_count(">", 0)).startswith("B.MEMBER_NO IN (")
    assert _compile(_contact_count(">=", 0)).startswith("(SELECT COUNT(")


def test_mirrored_comparison_is_normalized_to_the_aggregate_side() -> None:
    """'3 <= 발송 성공 횟수' 도 같은 조건이다 — 리터럴이 왼쪽이라고 최적화를 놓치지 않는다."""
    mirrored = event_ir.Comparison(
        operator="<=",
        left=event_ir.Literal(value=3),
        right=_contact_count().left,
    )
    sql = _compile(mirrored)

    assert sql.startswith("B.MEMBER_NO IN (")
    assert "HAVING COUNT(DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO)) >= 3" in sql


def test_fractional_threshold_is_not_lowered() -> None:
    """'2.5회 이상' 은 횟수 임계값이 아니다 — float 로 0 판정을 하지 않는다."""
    receipts: list[dict] = []
    _compile(_contact_count(">=", 2.5), optimization_receipts=receipts)

    assert [item["reason"] for item in receipts] == [event_compiler.SKIP_UNSUPPORTED_SCOPE]


def test_boolean_threshold_is_not_treated_as_a_number() -> None:
    """True 는 파이썬에서 1 이지만 횟수 임계값이 아니다 — 조용히 '1회 이상'으로 읽지 않는다."""
    receipts: list[dict] = []
    _compile(_contact_count(">=", True), optimization_receipts=receipts)

    assert [item["reason"] for item in receipts] == [event_compiler.SKIP_UNSUPPORTED_SCOPE]


def test_sum_aggregate_is_not_lowered_without_its_own_equivalence_proof() -> None:
    """빈 집합에서 SUM 은 NULL(→COALESCE 0)이고 음수 합도 가능하다 — 별도 증명 전까지 제외.

    아래 8절의 비율형 lowering 은 그 증명을 0 분모 NULL 가드가 있는 **비율식**에 한해 갖는다.
    임계값과 직접 비교되는 맨 SUM 은 여전히 이 경로가 소유하고, 여전히 낮추지 않는다.
    """
    expression = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="sum",
            relation=event_ir.Source(name="purchase"),
            expression=event_ir.FieldRef(name="purchase.amount"),
        ),
        right=event_ir.Literal(value=100000),
    )
    receipts: list[dict] = []
    sql = _compile(expression, optimization_receipts=receipts)

    assert "= B.MEMBER_NO" in sql
    assert [item["reason"] for item in receipts] == [event_compiler.SKIP_UNSUPPORTED_SCOPE]


# ── 3. 불리언 문맥: 극성과 결합자 ────────────────────────────────────────────────


def test_and_with_another_member_condition_keeps_both_predicates() -> None:
    expression = event_ir.And(
        operands=(
            _contact_count(),
            event_ir.Comparison(
                operator=">=",
                left=event_ir.FieldRef(name="subject.age"),
                right=event_ir.Literal(value=30),
            ),
        )
    )
    sql = _compile(expression)

    assert "B.MEMBER_NO IN (" in sql
    assert "B.AGE >= 30" in sql


def test_or_branch_is_lowered_because_the_predicate_is_two_valued() -> None:
    """OR 아래에서도 회원별 진리값이 같다 — NULL 그룹을 제외해 IN 이 2값이기 때문이다."""
    expression = event_ir.Or(
        operands=(
            _contact_count(),
            event_ir.Comparison(
                operator=">=",
                left=event_ir.FieldRef(name="subject.age"),
                right=event_ir.Literal(value=30),
            ),
        )
    )
    sql = _compile(expression)

    assert "B.MEMBER_NO IN (" in sql
    assert "= B.MEMBER_NO" not in sql


def test_negation_keeps_the_audience_complement_semantics() -> None:
    """부정은 NOT IN 의 NULL 함정을 피해 기존 2값화 보수를 그대로 쓴다."""
    sql = _compile(event_ir.Not(operand=_contact_count()))

    assert sql.startswith("NOT (CASE WHEN (B.MEMBER_NO IN (")
    assert "THEN 1 ELSE 0 END = 1)" in sql


def test_group_grain_comparison_keeps_the_exists_having_shape() -> None:
    """grain 이 회원이 아닌 집계(주문 단위)는 이미 집합형이라 다시 낮추지 않는다."""
    expression = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="count",
            relation=event_ir.Group(
                relation=event_ir.Source(name="purchase"),
                keys=(event_ir.FieldRef(name="purchase.order_id"),),
            ),
            expression=event_ir.FieldRef(name="purchase.order_id"),
            distinct=True,
        ),
        right=event_ir.Literal(value=2),
    )
    receipts: list[dict] = []
    sql = _compile(expression, optimization_receipts=receipts)

    assert sql.startswith("EXISTS (") and "HAVING" in sql
    assert [item["reason"] for item in receipts] == [event_compiler.SKIP_ALREADY_SET_BASED]


# ── 4. fast-path: 집계가 없으면 비용도 receipt 도 없다 ───────────────────────────


def test_member_attribute_comparison_never_reaches_the_optimizer() -> None:
    receipts: list[dict] = []
    sql = _compile(
        event_ir.Comparison(
            operator=">=",
            left=event_ir.FieldRef(name="subject.age"),
            right=event_ir.Literal(value=30),
        ),
        optimization_receipts=receipts,
    )

    assert sql == "B.AGE >= 30"
    assert receipts == []


def test_subject_column_event_window_never_reaches_the_optimizer() -> None:
    """'최근 로그인일' 같은 주체 컬럼 사건은 팩트 집계가 아니다."""
    receipts: list[dict] = []
    _compile(
        event_ir.Exists(
            relation=event_ir.Filter(
                relation=event_ir.Source(name="login"),
                where=event_ir.TimeFilter(
                    field=event_ir.FieldRef(name="login.occurred_at"),
                    window=event_ir.RollingWindow(value=30, unit="day"),
                ),
            )
        ),
        optimization_receipts=receipts,
    )

    assert receipts == []


def test_plain_existence_is_untouched() -> None:
    receipts: list[dict] = []
    sql = _compile(
        event_ir.Exists(relation=event_ir.Source(name="purchase")),
        optimization_receipts=receipts,
    )

    assert sql.startswith("EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL EO")
    assert receipts == []


def test_disabled_flag_restores_the_previous_sql_byte_for_byte() -> None:
    """킬 스위치는 우회책이 아니라 롤백 경로다 — 껐을 때 이전 SQL 이 그대로 나와야 한다."""
    expression = _contact_count()
    receipts: list[dict] = []
    sql = _compile(expression, optimize_aggregate_membership=False, optimization_receipts=receipts)

    assert sql == (
        "(SELECT COUNT(DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO)) "
        "FROM Z_CAMP_MBR M INNER JOIN Z_CAMPAIGN ZC ON ZC.CAMP_ID = M.CAMP_ID "
        "AND ZC.CAMP_EXEC_NO = M.CAMP_EXEC_NO "
        "WHERE TRY_CAST(M.MBR_NO AS BIGINT) = B.MEMBER_NO "
        "AND M.CELL_TYPE_CD = 'T' AND M.CONTAC_SUCC_YN = 'Y' "
        "AND ISNULL(ZC.CANCEL_YN, 'N') = 'N' "
        "AND ZC.CAMP_SDATE >= CONVERT(CHAR(8), DATEADD(DAY, -5, GETDATE()), 112)) >= 3"
    )
    assert [item["reason"] for item in receipts] == [
        event_compiler.SKIP_OPTIMIZATION_DISABLED
    ]


# ── 5. Event IR 비침범 ───────────────────────────────────────────────────────────


def test_optimization_does_not_touch_the_expression_or_its_serialization() -> None:
    expression = _contact_count()
    before = copy.deepcopy(expression.to_dict())

    lowered = _compile(expression)
    correlated = _correlated(expression)

    assert expression.to_dict() == before
    assert event_ir.condition_from_dict(before).to_dict() == before
    assert event_ir.node_type_names(expression) == {
        "comparison", "aggregate", "filter", "source", "time_filter", "field", "literal",
        "rolling",
    }
    assert lowered != correlated  # 물리 모양만 다르다


def test_capability_and_semantic_surface_are_identical_both_ways() -> None:
    expression = _contact_count()

    lowered = event_compiler.validate_compiler_capability(
        expression, context=_context()
    )
    correlated = event_compiler.validate_compiler_capability(
        expression, context=_context(optimize_aggregate_membership=False)
    )

    assert lowered.status == correlated.status == event_compiler.CAPABILITY_SUPPORTED
    # 노드 지문은 IR 에서만 파생한다 — 물리 최적화가 query identity 를 흔들면 안 된다.
    assert lowered.supported_node_ids == correlated.supported_node_ids


def test_receipt_carries_the_preserved_expression_fingerprint() -> None:
    expression = _contact_count()
    receipts: list[dict] = []
    _compile(expression, optimization_receipts=receipts)

    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["optimization"] == event_compiler.AGGREGATE_MEMBERSHIP_OPTIMIZATION
    assert receipt["status"] == "applied"
    assert receipt["source"] == CONTACT
    assert receipt["preserved_expression_fingerprint"].startswith("event_node_")
    # receipt 는 진단이다 — JSON 으로 남길 수 있어야 하고 IR 을 담고 있으면 안 된다.
    assert json.loads(json.dumps(receipt)) == receipt


# ── 6. 물리 바인딩: group key 는 선언에서만 온다 ─────────────────────────────────


def test_group_key_is_declared_not_reverse_parsed_from_the_correlation() -> None:
    """상관식이 있는데 group key 선언이 없으면 추측하지 않고 기존 경로를 쓴다."""
    catalog = audience_runtime.resolve_audience_catalog()
    registry = dict(catalog.compiler_events)
    spec = registry[CONTACT]
    assert spec.group_subject_expression == "TRY_CAST({alias}.MBR_NO AS BIGINT)"

    registry[CONTACT] = event_compiler.EventSpec(
        **{
            **{f.name: getattr(spec, f.name) for f in spec.__dataclass_fields__.values()},
            "group_subject_expression": "",
        }
    )
    context = catalog.compile_context(literals=True)
    context.registry = registry
    receipts: list[dict] = []
    context.optimization_receipts = receipts

    sql = event_compiler.compile_expression(_contact_count(), context=context).sql

    assert "= B.MEMBER_NO" in sql
    assert [item["reason"] for item in receipts] == [
        event_compiler.SKIP_NO_GROUP_SUBJECT_BINDING
    ]


def test_default_correlation_derives_its_group_key_without_a_declaration() -> None:
    """기본 상관식('alias.key = subject.key')의 group key 는 파생이 유일해서 안전하다."""
    expression = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="count",
            relation=event_ir.Source(name="purchase"),
            expression=event_ir.FieldRef(name="purchase.order_id"),
            distinct=True,
        ),
        right=event_ir.Literal(value=3),
    )
    sql = _compile(expression)

    assert sql.startswith("B.MEMBER_NO IN (SELECT EO.MEMBER_NO FROM CRM_SL_ORDERHEADERMALL EO")
    assert "GROUP BY EO.MEMBER_NO" in sql
    assert "HAVING COUNT(DISTINCT EO.ORDER_ID) >= 3" in sql


# ── 7. 성능 구조 가드 ────────────────────────────────────────────────────────────


def test_performance_guard_finds_the_correlated_scalar_aggregate() -> None:
    correlated = (
        "SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE ("
        "SELECT COUNT(DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO)) FROM Z_CAMP_MBR M "
        "WHERE TRY_CAST(M.MBR_NO AS BIGINT) = B.MEMBER_NO) >= 3"
    )

    assert sql_guard.correlated_scalar_aggregates(correlated, "B") == [
        "COUNT(DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO))"
    ]


def test_performance_guard_passes_the_lowered_shape() -> None:
    lowered = (
        "SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE "
        + _compile(_contact_count())
    )

    assert sql_guard.correlated_scalar_aggregates(lowered, "B") == []


def test_performance_guard_reports_unreadable_instead_of_clean() -> None:
    """읽지 못한 것을 '깨끗하다'로 돌려주면 가드가 통과 도장이 된다."""
    assert sql_guard.correlated_scalar_aggregates("SELECT FROM WHERE ((", "B") is None


def test_performance_guard_ignores_semi_join_subqueries() -> None:
    """EXISTS 세미조인은 스칼라 위치가 아니다 — 행마다 다시 계산되는 구조가 아니다."""
    semi = (
        "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE EXISTS ("
        "SELECT 1 FROM CRM_SL_ORDERHEADERMALL EO WHERE EO.MEMBER_NO = B.MEMBER_NO)"
    )

    assert sql_guard.correlated_scalar_aggregates(semi, "B") == []


# ── 8. 비율형: 같은 관계를 공유하는 집계 여럿을 2단 집계로 낮춘다 ────────────────
#
# 위 경로는 '집계 하나 vs 리터럴' 만 본다. 캠페인 분모 평균은 비교 좌변이 Arithmetic 이라 그
# fast-path 를 그대로 지나가고, 회원 1행마다 상관 스칼라 서브쿼리가 집계 개수만큼 열린다.
# 여기서 고정하는 것은 그 모양을 낮출 수 있는 **경계**다.

RESPONSE = "campaign_purchase_response"


def _campaign_average(
    operator: str = ">=",
    threshold: object = 100000,
    *,
    relation: event_ir.Relation | None = None,
    numerator: event_ir.Aggregate | None = None,
    denominator: event_ir.Scalar | None = None,
) -> event_ir.Comparison:
    """``SUM(금액) * 1.0 / NULLIF(COUNT(DISTINCT (캠페인, 실행회차)), 0) <operator> 임계``."""
    shared: event_ir.Relation = relation or event_ir.Source(name=RESPONSE)
    return event_ir.Comparison(
        operator=operator,
        left=event_ir.Arithmetic(
            operator="/",
            left=event_ir.Arithmetic(
                operator="*",
                left=numerator
                or event_ir.Aggregate(
                    function="sum",
                    relation=shared,
                    expression=event_ir.FieldRef(name=f"{RESPONSE}.amount"),
                ),
                right=event_ir.Literal(value=1.0),
            ),
            right=denominator
            if denominator is not None
            else event_ir.NullIf(
                expression=event_ir.Aggregate(
                    function="count",
                    relation=shared,
                    expression=event_ir.Tuple(
                        items=(
                            event_ir.FieldRef(name=f"{RESPONSE}.campaign_id"),
                            event_ir.FieldRef(name=f"{RESPONSE}.execution_no"),
                        )
                    ),
                    distinct=True,
                ),
                value=event_ir.Literal(value=0),
            ),
        ),
        right=event_ir.Literal(value=threshold),
    )


def _skip_reasons(expression: event_ir.Condition) -> list[str]:
    receipts: list[dict] = []
    _compile(expression, optimization_receipts=receipts)
    return [item["reason"] for item in receipts if "reason" in item]


def test_ratio_comparison_becomes_a_two_level_grouped_semi_join() -> None:
    """분모가 다중 컬럼 distinct 여도 낮춘다 — 안쪽 분할의 개수가 곧 그 distinct 수다."""
    sql = _compile(_campaign_average())

    assert sql.startswith("B.MEMBER_NO IN (SELECT EM0 FROM (")
    # 안쪽: (회원키, 캠페인 실행키) 분할의 부분합
    assert "GROUP BY TRY_CAST(R.MBR_NO AS BIGINT), R.CAMP_ID, R.CAMP_EXEC_NO" in sql
    assert "SUM(R.BUY_AMT) AS EP1" in sql
    # 바깥: 회원키로 다시 접는다. 분모는 안쪽 그룹 수다.
    assert "GROUP BY EM0 HAVING ((ISNULL(SUM(EP1), 0) * 1.0) / NULLIF(COUNT(*), 0)) >= 100000" in sql
    # 팩트를 한 번만 읽고, 상관 술어는 남지 않는다.
    assert sql.count("MCS_CAMP_MBR_RSPN_FT") == 1
    assert "= B.MEMBER_NO" not in sql
    # 구분자 결합 키로 바꾸지 않는다(서로 다른 키가 충돌한다).
    assert "CONCAT(" not in sql


def test_ratio_lowering_leaves_a_receipt_naming_its_own_optimization() -> None:
    """두 lowering 이 같은 이름으로 섞이지 않는다 — 어느 쪽이 걸렸는지 영수증이 말한다."""
    receipts: list[dict] = []
    _compile(_campaign_average(), optimization_receipts=receipts)

    assert [item["optimization"] for item in receipts] == [
        event_compiler.AGGREGATE_RATIO_MEMBERSHIP_OPTIMIZATION
    ]
    assert [item["status"] for item in receipts] == ["applied"]


def test_single_aggregate_comparison_still_belongs_to_the_original_lowering() -> None:
    """'집계 하나 vs 리터럴' 의 소유자는 그대로다 — 비율 경로가 가로채지 않는다."""
    receipts: list[dict] = []
    _compile(_contact_count(), optimization_receipts=receipts)

    assert [item["optimization"] for item in receipts] == [
        event_compiler.AGGREGATE_MEMBERSHIP_OPTIMIZATION
    ]


def test_ratio_without_a_zero_denominator_guard_is_lowered_only_when_false_at_empty() -> None:
    """0 분모 NULL 가드가 없으면 빈 집합 값이 0 이다 — 그때는 0 에서 거짓인 비교만 낮춘다."""
    plain = event_ir.Literal(value=2)

    assert _compile(_campaign_average(">", 0, denominator=plain)).startswith("B.MEMBER_NO IN (")
    assert _skip_reasons(_campaign_average(">=", 0, denominator=plain)) == [
        event_compiler.SKIP_ZERO_SENSITIVE_COMPARISON
    ]


def test_zero_denominator_guard_makes_every_operator_false_at_empty() -> None:
    """``NULLIF(0, 0)`` 은 NULL 이고 NULL 비교는 UNKNOWN 이다 — 반응 없는 회원은 늘 거짓이다."""
    for operator in (">=", ">", "<=", "<", "=", "!="):
        assert _compile(_campaign_average(operator, 100000)).startswith("B.MEMBER_NO IN ("), operator


def test_mixed_relations_are_not_lowered() -> None:
    """분자에만 기간이 걸리면 한 번의 GROUP BY 로 셀 수 없다 — 조용히 합치지 않는다."""
    windowed = event_ir.Filter(
        relation=event_ir.Source(name=RESPONSE),
        where=event_ir.TimeFilter(
            field=event_ir.FieldRef(name=f"{RESPONSE}.occurred_at"),
            window=event_ir.RollingWindow(value=30, unit="day"),
        ),
    )
    numerator = event_ir.Aggregate(
        function="sum",
        relation=windowed,
        expression=event_ir.FieldRef(name=f"{RESPONSE}.amount"),
    )

    assert _skip_reasons(_campaign_average(numerator=numerator)) == [
        event_compiler.SKIP_MIXED_AGGREGATE_RELATIONS
    ]


def test_non_decomposable_partial_aggregate_is_not_lowered() -> None:
    """2단 집계에서는 바깥이 안쪽 부분집계를 다시 접을 수 있어야 한다.

    ``AVG`` 는 부분평균의 평균이 전체 평균이 아니고, ``DISTINCT`` 는 분할 경계를 넘어 중복을
    제거해야 한다. 둘 다 분해되지 않으므로 상관 모양을 유지한다.
    """
    average = event_ir.Aggregate(
        function="avg",
        relation=event_ir.Source(name=RESPONSE),
        expression=event_ir.FieldRef(name=f"{RESPONSE}.amount"),
    )
    distinct_count = event_ir.Aggregate(
        function="count",
        relation=event_ir.Source(name=RESPONSE),
        expression=event_ir.FieldRef(name=f"{RESPONSE}.amount"),
        distinct=True,
    )

    assert _skip_reasons(_campaign_average(numerator=average)) == [
        event_compiler.SKIP_NON_DECOMPOSABLE_AGGREGATE
    ]
    assert _skip_reasons(_campaign_average(numerator=distinct_count)) == [
        event_compiler.SKIP_NON_DECOMPOSABLE_AGGREGATE
    ]


def test_undecidable_empty_set_value_is_not_lowered() -> None:
    """리터럴 0 으로 나누는 식은 빈 집합에서 값이 아니라 런타임 오류다 — 증명할 수 없다."""
    assert _skip_reasons(_campaign_average(denominator=event_ir.Literal(value=0))) == [
        event_compiler.SKIP_EMPTY_SET_UNDECIDABLE
    ]


def test_ratio_lowering_is_a_switch_not_a_rewrite() -> None:
    """끄면 이전 상관 스칼라 SQL 이 그대로 나오고, IR 은 어느 쪽에서도 변하지 않는다."""
    expression = _campaign_average()
    before = json.dumps(expression.to_dict(), sort_keys=True, ensure_ascii=False)
    snapshot = copy.deepcopy(expression.to_dict())

    correlated = _correlated(expression)
    lowered = _compile(expression)

    assert correlated.startswith("(((SELECT ISNULL(SUM(R.BUY_AMT), 0)")
    assert "TRY_CAST(R.MBR_NO AS BIGINT) = B.MEMBER_NO" in correlated
    assert correlated.count("MCS_CAMP_MBR_RSPN_FT") == 2
    assert lowered != correlated
    assert json.dumps(expression.to_dict(), sort_keys=True, ensure_ascii=False) == before
    assert expression.to_dict() == snapshot
    assert event_ir.expression_capabilities(expression) == event_ir.expression_capabilities(
        _campaign_average()
    )


def test_ratio_lowering_passes_the_performance_structure_guard() -> None:
    """낮춘 결과에 상관 스칼라 집계가 남아 있지 않다 — 가드가 그것을 읽어 확인한다."""
    lowered = (
        "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE " + _compile(_campaign_average())
    )
    correlated = (
        "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B WHERE "
        + _correlated(_campaign_average())
    )

    assert sql_guard.correlated_scalar_aggregates(correlated, "B") == [
        "COUNT(*)",
        "SUM(R.BUY_AMT)",
    ]
    assert sql_guard.correlated_scalar_aggregates(lowered, "B") == []
