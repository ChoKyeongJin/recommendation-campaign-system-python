"""조건 IR 계약 — 자연어 의미가 IR 을 거쳐 SQL 까지 **줄지 않고** 가는지 고정한다.

이 파일이 지키는 것은 특정 문장의 결과가 아니라 구조적 성질 다섯이다:

  1. **문장 유형이 늘어도 IR 타입은 늘지 않는다** — 노드 타입 집합이 닫혀 있고, 새 사건·새 필드는
     레지스트리 확장만으로 동작한다(설계 원칙: event_ir 모듈 주석).
  2. 표현형이 달라도 같은 IR 이 나온다(구매 기록/이력/내역/한 적).
  3. 기간과 극성은 **자기 절에** 귀속된다 — 문장에 조건이 둘이면 창도 둘이다.
  4. 표현할 수 없는 의미는 다른 슬롯으로 **바뀌지 않는다**(특정 기간 부재 ≠ 평생 무구매).
  5. 원문의 부정·기간이 IR 에서 사라지면 SQL 을 만들지 않는다(fail-close).
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("SURFACE_LEXICON_LLM", "off")

import event_compiler
import event_ir
import event_parser

# 기준일 고정 — '올해/하반기'는 달력에 의존하므로 주입한다(calendar_window 와 같은 관례).
TODAY = date(2026, 7, 30)

H1 = event_ir.AbsoluteInterval(start=date(2026, 1, 1), end_exclusive=date(2026, 7, 1))
H2 = event_ir.AbsoluteInterval(start=date(2026, 7, 1), end_exclusive=date(2027, 1, 1))

REFERENCE = "올해 상반기 구매 기록이 있는 고객 중 하반기 구매 기록이 없는 고객"


def parse(text: str) -> event_ir.Condition:
    expression = event_parser.parse_expression(text, today=TODAY)
    assert expression is not None, f"조건을 하나도 읽지 못했다: {text}"
    return expression


def existence_shape(expression: event_ir.Condition) -> list[tuple[str, bool, Any]]:
    """근거 텍스트를 뺀 존재 조건의 의미 축만(소스, 극성, 창) — 표현형 비교용 정규형."""
    return [(view.source, view.negated, view.window) for view in event_ir.existence_views(expression)]


# ── 1. 설계 원칙: 문장이 늘어도 타입은 늘지 않는다 ─────────────────────────────────

# 이 목록이 곧 "허용된 범용 연산자"다. 문장 유형을 위해 여기 항목을 늘리는 변경은 설계 거부다.
EXPECTED_NODE_TYPES = {
    "and", "or", "not",
    "literal", "field", "arithmetic", "aggregate",
    "source", "filter", "join", "group", "project", "summarize", "order", "limit",
    "comparison", "exists", "time_filter", "temporal_relation", "event_reference",
    "interval", "rolling", "relative", "duration",
}

# 서로 다른 업무 요구 — 전부 위 연산자 조합만으로 표현돼야 한다.
SENTENCES = [
    REFERENCE,
    "올해 상반기에 구매한 적이 있고 하반기에는 구매하지 않은 고객",
    "최근 30일 구매하지 않은 고객",
    "2026년 7월부터 12월까지 구매가 없는 고객",
    "하반기 구매 기록이 없는 고객",
    "2026년 상반기에 구매한 고객",
    "구매 이력이 없는 고객",
    "최근 180일 미구매 고객",
    "최근 30일 동안 3회 이상 구매한 고객",
    "올해 상반기 구매 금액 합계가 100000원 이상인 고객",
    "첫 구매 후 30일 이내 재구매한 고객",
    "가입 후 7일 이내 구매한 고객",
    "3년 전에 구매한 고객",
    "상반기에 구매했고 하반기에는 구매하지 않았거나, 최근 30일 로그인하지 않은 고객",
]


def test_ir_node_type_set_is_closed() -> None:
    """IR 이 선언한 노드 타입 목록 자체가 닫혀 있다(문장별 타입이 섞여 들어오면 실패)."""
    assert event_ir.NODE_TYPES == EXPECTED_NODE_TYPES


@pytest.mark.parametrize("text", SENTENCES, ids=range(len(SENTENCES)))
def test_every_sentence_uses_only_generic_operators(text: str) -> None:
    """서로 다른 업무 요구가 전부 같은 연산자 집합의 **조합**으로 표현된다."""
    assert event_ir.node_type_names(parse(text)) <= EXPECTED_NODE_TYPES


def test_business_events_are_registry_symbols_not_types() -> None:
    """구매/로그인/가입은 IR 타입이 아니라 ``Source`` 의 이름이다."""
    for text, expected in (
        ("구매 이력이 없는 고객", "purchase"),
        ("최근 30일 로그인하지 않은 고객", "login"),
    ):
        expression = parse(text)
        assert event_ir.sources(expression) >= {expected}
        assert all(name.islower() for name in event_ir.node_type_names(expression))


def test_new_event_needs_only_a_registry_entry() -> None:
    """레지스트리에 항목을 더하는 것만으로 다른 스키마의 사건이 컴파일된다(IR 타입 추가 없음)."""
    registry = event_compiler.resolve_registry({
        "refund": event_compiler.EventSpec(
            table="refunds", alias="EO", subject_key="MEMBER_NO",
            event_subject_key="customer_id", time_column="refunded_at", time_format="date",
        )
    })
    condition = event_ir.existence(
        "refund", negated=True, window=H2,
        evidence=event_ir.Evidence(text="환불 없음", start=0, end=5),
    )
    sql = event_compiler.compile_expression_sql(condition, registry=registry)
    assert sql == (
        "NOT EXISTS (SELECT 1 FROM refunds EO WHERE EO.customer_id = B.MEMBER_NO "
        "AND EO.refunded_at >= '2026-07-01' AND EO.refunded_at < '2027-01-01')"
    )
    assert event_ir.node_type_names(condition) <= EXPECTED_NODE_TYPES


def test_new_field_needs_only_a_registry_entry() -> None:
    """새 업무 속성은 FIELD_REGISTRY 한 줄로 쓸 수 있다."""
    registry = event_compiler.resolve_registry()
    fields = event_compiler.resolve_fields(registry, {
        "purchase.discount": event_compiler.FieldSpec(
            source="purchase", column="DISCOUNT_AMT", data_type="number"
        )
    })
    condition = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="sum", relation=event_ir.Source(name="purchase"),
            expression=event_ir.FieldRef(name="purchase.discount"),
        ),
        right=event_ir.Literal(value=5000),
        evidence=event_ir.Evidence(text="할인 합계 5000원 이상", start=0, end=13),
    )
    sql = event_compiler.compile_expression_sql(condition, registry=registry, fields=fields)
    assert "SUM(EO.DISCOUNT_AMT)" in sql and ">= 5000" in sql


def test_occurred_at_field_is_derived_from_the_event_registry() -> None:
    """이벤트를 등록하면 발생 시각 필드는 자동으로 생긴다(빠뜨릴 수 없게)."""
    registry = event_compiler.resolve_registry({
        "refund": event_compiler.EventSpec(
            table="refunds", alias="EO", subject_key="MEMBER_NO",
            event_subject_key="MEMBER_NO", time_column="refunded_at", time_format="date",
        )
    })
    fields = event_compiler.resolve_fields(registry)
    assert fields["refund.occurred_at"].column == "refunded_at"
    assert fields["refund.occurred_at"].data_type == "date"


def test_unregistered_symbols_fail_closed() -> None:
    condition = event_ir.existence(
        "unknown_thing", evidence=event_ir.Evidence(text="무언가", start=0, end=3)
    )
    assert event_compiler.unsupported_events(condition) == ["unknown_thing"]
    with pytest.raises(event_compiler.SqlCompileError, match="등록되지 않은 사건"):
        event_compiler.compile_expression(condition)


# ── 2. 부재는 전용 타입이 아니라 Not + Exists ─────────────────────────────────────

def test_absence_is_not_plus_exists() -> None:
    expression = parse("구매 이력이 없는 고객")
    assert isinstance(expression, event_ir.Not)
    assert isinstance(expression.operand, event_ir.Exists)
    assert expression.operand.relation == event_ir.Source(name="purchase")


def test_existence_helper_is_sugar_over_the_same_composition() -> None:
    """``existence(negated=True)`` 는 편의 문법일 뿐 ``Not(Exists(...))`` 와 같은 트리다."""
    evidence = event_ir.Evidence(text="구매 없음", start=0, end=5)
    sugar = event_ir.existence("purchase", negated=True, window=H2, evidence=evidence)
    manual = event_ir.Not(operand=event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name="purchase"),
            where=event_ir.TimeFilter(field=event_ir.FieldRef(name="purchase.occurred_at"), window=H2),
        ),
        evidence=evidence,
    ))
    assert sugar == manual


# ── 3. 정상 케이스: 기간별 존재/부재가 각각 살아남는다 ────────────────────────────

def test_half_year_exists_and_absence_are_two_independent_conditions() -> None:
    expression = parse(REFERENCE)
    assert isinstance(expression, event_ir.And)
    assert existence_shape(expression) == [("purchase", False, H1), ("purchase", True, H2)]


def test_compiled_sql_keeps_both_windows_and_polarities() -> None:
    sql = event_compiler.compile_expression_sql(parse(REFERENCE))
    assert "EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL" in sql
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL" in sql
    # 반개구간 — timestamp 컬럼에서 마지막 날이 잘리지 않게 BETWEEN 을 쓰지 않는다.
    assert ">= '20260101'" in sql and "< '20260701'" in sql
    assert ">= '20260701'" in sql and "< '20270101'" in sql
    assert "BETWEEN" not in sql


def test_bound_parameters_are_unique_per_condition() -> None:
    compiled = event_compiler.compile_expression(parse(REFERENCE))
    assert compiled.params == {
        "event_0_start": "20260101", "event_0_end": "20260701",
        "event_1_start": "20260701", "event_1_end": "20270101",
    }


# ── 4. 동의어: '기록'이 SQL 예외 처리 없이 해석된다 ───────────────────────────────

@pytest.mark.parametrize(
    "text",
    [
        REFERENCE,
        "올해 상반기 구매 이력이 있는 고객 중 하반기 구매 이력이 없는 고객",
        "올해 상반기 구매 내역이 있는 고객 중 하반기 구매 내역이 없는 고객",
        "올해 상반기에 구매한 적이 있고 하반기에는 구매하지 않은 고객",
    ],
)
def test_synonyms_produce_the_same_ir(text: str) -> None:
    assert existence_shape(parse(text)) == [("purchase", False, H1), ("purchase", True, H2)]


# ── 5. 시간 표현 ──────────────────────────────────────────────────────────────────

def test_rolling_window_absence() -> None:
    assert existence_shape(parse("최근 30일 구매하지 않은 고객")) == [
        ("purchase", True, event_ir.RollingWindow(value=30, unit="day"))
    ]


def test_rolling_window_compiles_to_a_runtime_cutoff_not_a_frozen_date() -> None:
    sql = event_compiler.compile_expression_sql(parse("최근 30일 구매하지 않은 고객"))
    assert "DATEADD(DAY, -30, GETDATE())" in sql


def test_absolute_interval_absence() -> None:
    assert existence_shape(parse("2026년 7월부터 12월까지 구매가 없는 고객")) == [("purchase", True, H2)]


def test_relative_window_is_kept_relative_in_the_ir() -> None:
    """'3년 전'은 원문 그대로 상대 창으로 남고, 확정은 컴파일러가 기준일로 한다."""
    expression = parse("3년 전에 구매한 고객")
    assert existence_shape(expression) == [
        ("purchase", False, event_ir.RelativeWindow(value=3, unit="year"))
    ]
    sql = event_compiler.compile_expression_sql(expression, today=TODAY)
    assert ">= '20230101'" in sql and "< '20240101'" in sql


# ── 6. 집계: 횟수·합계가 같은 노드다 ──────────────────────────────────────────────

def test_count_threshold_uses_aggregate_and_comparison() -> None:
    expression = parse("최근 30일 동안 3회 이상 구매한 고객")
    assert isinstance(expression, event_ir.Comparison)
    assert expression.operator == ">="
    assert isinstance(expression.left, event_ir.Aggregate)
    assert expression.left.function == "count"
    assert expression.right == event_ir.Literal(value=3)
    sql = event_compiler.compile_expression_sql(expression)
    assert "SELECT COUNT(DISTINCT EO.ORDER_ID)" in sql and ">= 3" in sql


def test_sum_threshold_uses_the_same_nodes_with_a_different_function() -> None:
    expression = parse("올해 상반기 구매 금액 합계가 100000원 이상인 고객")
    assert isinstance(expression, event_ir.Comparison)
    assert isinstance(expression.left, event_ir.Aggregate)
    assert expression.left.function == "sum"
    assert expression.left.expression == event_ir.FieldRef(name="purchase.amount")
    sql = event_compiler.compile_expression_sql(expression)
    assert "SUM(EO.PAYMENT_AMT)" in sql and ">= 100000" in sql
    assert ">= '20260101'" in sql and "< '20260701'" in sql


@pytest.mark.parametrize("function", ["avg", "min", "max"])
def test_other_aggregate_functions_need_no_new_type(function: str) -> None:
    condition = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function=function, relation=event_ir.Source(name="purchase"),
            expression=event_ir.FieldRef(name="purchase.amount"),
        ),
        right=event_ir.Literal(value=1000),
        evidence=event_ir.Evidence(text="객단가 1000원 이상", start=0, end=12),
    )
    assert function.upper() + "(EO.PAYMENT_AMT)" in event_compiler.compile_expression_sql(condition)


def test_group_makes_the_aggregate_grain_explicit() -> None:
    """'한 주문에 3개 이상'은 회원 단위가 아니라 주문 단위 집계다 — grain 을 노드로 드러낸다."""
    condition = event_ir.Comparison(
        operator=">=",
        left=event_ir.Aggregate(
            function="count",
            relation=event_ir.Group(
                relation=event_ir.Source(name="purchase"),
                keys=(event_ir.FieldRef(name="purchase.order_id"),),
            ),
        ),
        right=event_ir.Literal(value=3),
        evidence=event_ir.Evidence(text="한 주문에 3건 이상", start=0, end=11),
    )
    sql = event_compiler.compile_expression_sql(condition)
    assert sql.startswith("EXISTS (") and "GROUP BY EO.ORDER_ID" in sql and "HAVING COUNT(*) >= 3" in sql


def test_join_composes_two_sources() -> None:
    """관계 조인도 범용 노드다(상품 조인 등) — 소스 심볼만 바뀌면 다른 조합에 그대로 쓰인다."""
    registry = event_compiler.resolve_registry({
        "order_line": event_compiler.EventSpec(
            table="CRM_SL_ORDERDETAILMALL", alias="EO", subject_key="MEMBER_NO",
            event_subject_key="MEMBER_NO", time_column="ORDER_DATE",
        ),
        "product": event_compiler.EventSpec(
            table="CRM_CM_PRODUCT", alias="EC", subject_key="MEMBER_NO",
            event_subject_key="PRODUCT_ID", time_column="REG_DT",
        ),
    })
    fields = event_compiler.resolve_fields(registry, {
        "order_line.product_id": event_compiler.FieldSpec("order_line", "PRODUCT_ID", "string"),
        "product.product_id": event_compiler.FieldSpec("product", "PRODUCT_ID", "string"),
        "product.brand": event_compiler.FieldSpec("product", "BRAND_NM", "string"),
    })
    condition = event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Join(
                left=event_ir.Source(name="order_line"),
                right=event_ir.Source(name="product"),
                on=event_ir.Comparison(
                    operator="=",
                    left=event_ir.FieldRef(name="order_line.product_id"),
                    right=event_ir.FieldRef(name="product.product_id"),
                ),
            ),
            where=event_ir.Comparison(
                operator="=",
                left=event_ir.FieldRef(name="product.brand"),
                right=event_ir.Literal(value="알로&루"),
            ),
        ),
        evidence=event_ir.Evidence(text="알로루 브랜드 구매", start=0, end=10),
    )
    sql = event_compiler.compile_expression_sql(condition, registry=registry, fields=fields)
    assert "INNER JOIN CRM_CM_PRODUCT EC ON EO.PRODUCT_ID = EC.PRODUCT_ID" in sql
    assert "EC.BRAND_NM = '알로&루'" in sql


# ── 7. 시간 관계: 새 타입은 재사용 가능할 때만 ────────────────────────────────────

def test_temporal_relation_for_first_purchase_then_repurchase() -> None:
    expression = parse("첫 구매 후 30일 이내 재구매한 고객")
    assert isinstance(expression, event_ir.TemporalRelation)
    assert expression.operator == "within_after"
    assert expression.left == event_ir.EventReference(source="purchase", selector="first")
    assert expression.duration == event_ir.Duration(value=30, unit="day")
    sql = event_compiler.compile_expression_sql(expression)
    assert "MIN(EO1.ORDER_DATE)" in sql and "DATEADD(DAY, 30," in sql


def test_the_same_temporal_node_serves_a_different_domain_pair() -> None:
    """'가입 후 7일 이내 구매'가 같은 노드로 표현된다 — 소스 심볼만 다르다."""
    expression = parse("가입 후 7일 이내 구매한 고객")
    assert isinstance(expression, event_ir.TemporalRelation)
    assert (expression.left.source, expression.right.source) == ("signup", "purchase")
    sql = event_compiler.compile_expression_sql(expression)
    assert "B.REG_DT" in sql and "DATEADD(DAY, 7," in sql


def test_temporal_relation_fails_closed_on_an_unrepresentable_unit() -> None:
    """'24시간 이내'는 날짜 단위 컬럼으로 표현할 수 없다 — 일 단위로 반올림하지 않는다."""
    condition = event_ir.TemporalRelation(
        operator="within_after",
        left=event_ir.EventReference(source="signup", selector="first"),
        right=event_ir.EventReference(source="purchase", selector="subsequent"),
        duration=event_ir.Duration(value=24, unit="hour"),
        evidence=event_ir.Evidence(text="가입 후 24시간 이내 구매", start=0, end=14),
    )
    with pytest.raises(event_compiler.SqlCompileError, match="hour"):
        event_compiler.compile_expression(condition)


# ── 8. 의미 왜곡 방지: 특정 기간 부재를 평생 부재로 바꾸지 않는다 ──────────────────

def test_windowed_absence_never_becomes_lifetime_absence() -> None:
    expression = parse("하반기 구매 기록이 없는 고객")
    assert existence_shape(expression) == [("purchase", True, H2)]
    assert event_ir.try_convert_to_legacy_slots(expression) is None
    assert event_ir.requires_general_ir(expression) is True


def test_lifetime_absence_still_maps_to_the_legacy_slot() -> None:
    assert event_ir.try_convert_to_legacy_slots(parse("구매 이력이 없는 고객")) == {
        "target_user": {"behaviors": ["no_purchase"]}
    }


def test_rolling_absence_maps_to_the_legacy_inactivity_slot() -> None:
    assert event_ir.try_convert_to_legacy_slots(parse("최근 180일 미구매 고객")) == {
        "target_user": {"purchase_inactivity": {"value": 180, "unit": "days", "min_days": 180}}
    }


def test_rolling_aggregate_maps_to_the_legacy_aggregate_slot() -> None:
    assert event_ir.try_convert_to_legacy_slots(parse("최근 30일 동안 3회 이상 구매한 고객")) == {
        "target_user": {"aggregate_conditions": [
            {"metric_id": "order_count", "operator": ">=", "threshold": 3, "window_days": 30}
        ]}
    }


@pytest.mark.parametrize(
    "text, expected",
    [
        ("최근 180일 미구매 고객", False),
        ("2026년 상반기에 구매한 고객", False),
        ("최근 30일 로그인한 고객", False),
        ("최근 30일 동안 3회 이상 구매한 고객", False),
        ("3년 전에 구매한 고객", False),
        ("하반기 구매 기록이 없는 고객", True),
        ("올해 상반기 구매 금액 합계가 100000원 이상인 고객", True),
        ("첫 구매 후 30일 이내 재구매한 고객", True),
        (REFERENCE, True),
    ],
)
def test_routing_authority_matches_slot_expressibility(text: str, expected: bool) -> None:
    """호환 계층의 방향: 슬롯이 담을 수 있으면 IR 은 실행 모델이 되지 않는다."""
    expression = parse(text)
    assert event_ir.requires_general_ir(expression) is expected
    # 두 함수의 답이 갈라지면 라우팅과 감사 로그가 서로 다른 이야기를 하게 된다.
    assert (event_ir.try_convert_to_legacy_slots(expression, today=TODAY) is None) is expected


# ── 9. 근거(evidence) ─────────────────────────────────────────────────────────────

def test_every_atom_carries_a_source_span() -> None:
    expression = parse(REFERENCE)
    event_ir.validate_evidence(expression)  # 예외 없음
    for atom in event_ir.iter_atoms(expression):
        assert atom.evidence is not None
        assert atom.evidence.start < atom.evidence.end
        assert REFERENCE[atom.evidence.start:atom.evidence.end] == atom.evidence.text


def test_evidence_must_be_present() -> None:
    with pytest.raises(event_ir.SemanticLossError):
        event_ir.validate_evidence(
            event_ir.Exists(
                relation=event_ir.Source(name="purchase"),
                evidence=event_ir.Evidence(text="   ", start=0, end=3),
            )
        )


# ── 10. 의미 손실 감지 ────────────────────────────────────────────────────────────

def test_negation_loss_is_detected() -> None:
    """부정 탐지는 구매 전용 정규식이 아니라 도메인 무관 어휘를 본다."""
    positive_only = event_ir.Exists(
        relation=event_ir.Source(name="purchase"),
        evidence=event_ir.Evidence(text="구매 기록이 없는", start=0, end=9),
    )
    with pytest.raises(event_ir.SemanticLossError, match="부정"):
        event_ir.validate_negation_preserved("하반기 구매 기록이 없는 고객", positive_only)


def test_negative_comparison_counts_as_negation() -> None:
    """'구매 0건'은 Not 노드가 아니라 ``= 0`` 비교로도 적을 수 있다 — 검증이 잡음이 되면 안 된다."""
    condition = event_ir.Comparison(
        operator="=",
        left=event_ir.Aggregate(function="count", relation=event_ir.Source(name="purchase")),
        right=event_ir.Literal(value=0),
        evidence=event_ir.Evidence(text="구매 0건", start=0, end=6),
    )
    event_ir.validate_negation_preserved("구매 0건인 고객", condition)


@pytest.mark.parametrize("text", ["구매가 없는", "구매하지 않은", "구매 0건", "한번도 구매하지 않은"])
def test_generic_negation_vocabulary_covers_common_forms(text: str) -> None:
    assert event_ir.source_contains_negation(text) is True


def test_time_loss_is_detected() -> None:
    expression = parse("하반기 구매 기록이 없는 고객")
    event_ir.validate_time_preserved(1, expression)  # 일치 — 예외 없음
    with pytest.raises(event_ir.SemanticLossError, match="시간 조건 수"):
        event_ir.validate_time_preserved(2, expression)


def test_source_time_span_count_is_measured_by_the_calendar_parser() -> None:
    """시간 소실 탐지의 기준값은 IR 이 아니라 달력 파서가 센다(같은 코드로 세면 함께 놓친다)."""
    assert event_parser.source_time_span_count(REFERENCE, today=TODAY) == 2
    assert event_ir.count_time_constraints(parse(REFERENCE)) == 2


@pytest.mark.parametrize("text", SENTENCES, ids=range(len(SENTENCES)))
def test_validation_passes_for_every_supported_sentence(text: str) -> None:
    event_ir.validate_expression(
        event_parser.event_clause_text(text),
        parse(text),
        parsed_time_span_count=event_parser.source_time_span_count(text, today=TODAY),
    )


# ── 11. 복합 논리(AND/OR 우선순위) ────────────────────────────────────────────────

def test_and_binds_tighter_than_or() -> None:
    expression = parse("상반기에 구매했고 하반기에는 구매하지 않았거나, 최근 30일 로그인하지 않은 고객")
    assert isinstance(expression, event_ir.Or)
    left, right = expression.operands
    assert isinstance(left, event_ir.And)
    assert existence_shape(left) == [("purchase", False, H1), ("purchase", True, H2)]
    assert existence_shape(right) == [("login", True, event_ir.RollingWindow(value=30, unit="day"))]


def test_or_precedence_is_preserved_in_sql() -> None:
    sql = event_compiler.compile_expression_sql(
        parse("상반기에 구매했고 하반기에는 구매하지 않았거나, 최근 30일 로그인하지 않은 고객")
    )
    assert ") OR (" in sql and ") AND (" in sql
    assert sql.index(") AND (") < sql.index(") OR ("), "AND 가 OR 안쪽에 있어야 한다"


def test_signed_atoms_carry_the_effective_polarity() -> None:
    """원자만 보고 판정하면 ``Not`` 이 안 보인다 — 극성은 누적해서 읽어야 한다."""
    signs = [negated for _atom, negated in event_ir.iter_signed_atoms(parse(REFERENCE))]
    assert signs == [False, True]


def test_subject_column_absence_includes_members_who_never_did_it() -> None:
    """마지막 접속일 컬럼으로 표현되는 사건의 부재는 NULL(한 번도 없음)을 포함해야 한다."""
    sql = event_compiler.compile_expression_sql(parse("최근 30일 로그인하지 않은 고객"))
    # NOT (col IS NOT NULL AND col >= cutoff) — NULL 회원은 술어가 FALSE 라 부정에서 살아남는다.
    assert sql.startswith("NOT (") and "IS NOT NULL" in sql


# ── 12. 스키마 강제(자유 형식 JSON 금지) ──────────────────────────────────────────

@pytest.mark.parametrize("text", SENTENCES, ids=range(len(SENTENCES)))
def test_expression_round_trips_through_serialization(text: str) -> None:
    expression = parse(text)
    assert event_ir.condition_from_dict(expression.to_dict()).to_dict() == expression.to_dict()


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "exists", "relation": {"type": "source", "name": ""},
         "evidence": {"text": "x", "start": 0, "end": 1}},
        {"type": "comparison", "operator": "~=", "left": {"type": "literal", "value": 1},
         "right": {"type": "literal", "value": 2}, "evidence": {"text": "x", "start": 0, "end": 1}},
        {"type": "and", "operands": []},
        {"type": "nonsense"},
        {"type": "exists", "relation": {"type": "filter", "relation": {"type": "source", "name": "purchase"},
                                        "where": {"type": "time_filter",
                                                  "field": {"type": "field", "name": "purchase.occurred_at"},
                                                  "window": {"type": "fortnight", "value": 2}}},
         "evidence": {"text": "x", "start": 0, "end": 1}},
        {"type": "aggregate", "function": "median", "relation": {"type": "source", "name": "purchase"}},
    ],
)
def test_schema_violations_are_rejected(payload: dict) -> None:
    with pytest.raises(event_ir.IrSchemaError):
        event_parser.parse_llm_expression(payload)


def test_empty_interval_is_rejected() -> None:
    with pytest.raises(event_ir.IrSchemaError, match="empty interval"):
        event_ir.AbsoluteInterval(start=date(2026, 7, 1), end_exclusive=date(2026, 7, 1))


def test_field_reference_requires_a_source_qualifier() -> None:
    with pytest.raises(event_ir.IrSchemaError):
        event_ir.FieldRef(name="amount")


def test_llm_tool_schema_is_closed() -> None:
    schema = event_parser.llm_tool_schema()["properties"]["expression"]
    kinds = {
        variant["properties"]["type"]["enum"][0]
        for variant in schema["anyOf"]
        if "enum" in variant["properties"]["type"]
    }
    assert kinds <= EXPECTED_NODE_TYPES


# ── 13. 미지원 고지는 의미를 보존한다 ─────────────────────────────────────────────

def test_unsupported_payload_preserves_the_expression() -> None:
    payload = event_ir.unsupported_payload(
        "absolute_not_exists_not_supported", parse("하반기 구매 기록이 없는 고객")
    )
    assert payload["status"] == "unsupported"
    preserved = payload["preserved_expression"]
    assert preserved["type"] == "not"
    window = preserved["operand"]["relation"]["where"]["window"]
    assert (window["start"], window["end_exclusive"]) == ("2026-07-01", "2027-01-01")
