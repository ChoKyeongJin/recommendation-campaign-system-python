"""랭킹 파생집합 + 교집합 개수 임계('상위 N개 중 M개 이상')의 canonical 실행 계약.

이 파일이 고정하는 사실은 하나다 — **이 의미는 새 IR·새 operator·새 실행 경로 없이 이미
canonical Event IR 로 표현되고 SQL 로 컴파일된다.** 그래서 이 조합이 사용자에게 '표현할 수
없습니다'로 나갔다면 결함은 compiler/lowering/execution 이 아니라 **방출(emission)과 검증**에
있다는 것이 이 테스트의 진술이다.

의미:

    작년 판매량 상위 5개 상품 중 회원이 구매한 서로 다른 상품이 2개 이상

형상(손으로 적은 canonical IR — 이 파일의 주 계약):

    Comparison(
        Aggregate(count, distinct=true, expression=<entity field>,
                  relation=Join(semi, 회원 상관 Source, <랭킹 집합>)),
        ">=", 2)

    랭킹 집합 = Limit(Order(Summarize(Filter(전역 Source, 작년), by=상품, sum(수량))), 5)

SQL 문자열 전체는 고정하지 않는다 — 별칭은 컴파일러가 정하는 구현 세부라 계약이 아니다.
대신 의미가 살아 있는지를 구조(capability·영수증)와 별칭 비의존 SQL 조각으로 잰다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import canonical_audience_claims  # noqa: E402
import event_ir  # noqa: E402
import graph_rag  # noqa: E402
import semantic_requirements  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    attach_campaign_query_plan_v4_identity,
)

QUERY = "작년에 가장 많이 팔린 상품 5개 중 2개 이상 구매한 고객"
CURRENT_DATE = "2026-08-07"

# 카탈로그가 선언한 심볼. 이 테스트는 물리 테이블/컬럼을 알지 못한다.
RANK_SOURCE = "purchase_line"
ENTITY_FIELD = "purchase_line.product_id"
MEASURE_FIELD = "purchase_line.quantity"
TIME_FIELD = f"{RANK_SOURCE}.{event_ir.TIME_FIELD_SUFFIX}"


def _evidence(text: str) -> dict:
    start = QUERY.index(text)
    return {"text": text, "start": start, "end": start + len(text)}


def _ranked_relation() -> dict:
    """작년 판매수량 상위 5개 상품 — 회원별이 아닌 **전역** 랭킹(correlation='none')."""
    return {
        "type": "limit",
        "count": 5,
        "relation": {
            "type": "order",
            "keys": [
                {"name": "sold_quantity", "direction": "desc"},
                # 동점 시 결과가 흔들리지 않도록 엔터티 키로 결정론 정렬(exact_count 계약).
                {"name": "ranked_product_id", "direction": "asc"},
            ],
            "relation": {
                "type": "summarize",
                "relation": {
                    "type": "filter",
                    "relation": {
                        "type": "source",
                        "name": RANK_SOURCE,
                        "correlation": "none",
                    },
                    "where": {
                        "type": "time_filter",
                        "field": {"type": "field", "name": TIME_FIELD},
                        "window": {
                            "type": "interval",
                            "start": "2025-01-01",
                            "end_exclusive": "2026-01-01",
                        },
                    },
                },
                "keys": [
                    {
                        "name": "ranked_product_id",
                        "expression": {"type": "field", "name": ENTITY_FIELD},
                    }
                ],
                "measures": [
                    {
                        "name": "sold_quantity",
                        "function": "sum",
                        "expression": {"type": "field", "name": MEASURE_FIELD},
                        "distinct": False,
                    }
                ],
            },
        },
    }


def _membership_join() -> dict:
    """회원 구매 상품 ∩ 랭킹 집합. Join.on 양쪽 모두 entity field 다(회원 키가 아니다)."""
    return {
        "type": "join",
        "kind": "semi",
        "left": {"type": "source", "name": RANK_SOURCE},
        "right": _ranked_relation(),
        "on": {
            "type": "comparison",
            "operator": "=",
            "left": {"type": "field", "name": ENTITY_FIELD},
            "right": {"type": "field", "name": ENTITY_FIELD},
            "evidence": _evidence("가장 많이 팔린 상품 5개"),
        },
    }


def ranked_set_cardinality_expression() -> dict:
    """'상위 5개 중 2개 이상' = 교집합의 distinct 엔터티 수 >= 2."""
    return {
        "type": "comparison",
        "operator": ">=",
        "left": {
            "type": "aggregate",
            "function": "count",
            "distinct": True,
            "expression": {"type": "field", "name": ENTITY_FIELD},
            "relation": _membership_join(),
        },
        "right": {"type": "literal", "value": 2},
        "evidence": _evidence("작년에 가장 많이 팔린 상품 5개 중 2개 이상 구매"),
    }


def build_plan(expression: dict) -> dict:
    payload = {
        "intent": "find_user_segment",
        "campaign_constraints": {
            "objective": None,
            "offer_type": None,
            "channels": None,
            "sell_object": None,
        },
        "result_limit": None,
        "audience_requirement": {"expression": expression, "issues": []},
    }
    return attach_campaign_query_plan_v4_identity(
        payload, QUERY, current_date=CURRENT_DATE
    )


def _ranked_obligation() -> semantic_requirements.SourceRequirement:
    obligations = [
        requirement
        for requirement in semantic_requirements.capture_source_semantic_obligations(QUERY)
        if semantic_requirements.obligation_kind(requirement) == "ranked_entity_set"
    ]
    assert len(obligations) == 1, (
        f"랭킹 의무가 정확히 하나여야 한다: {[o.to_dict() for o in obligations]}"
    )
    return obligations[0]


def test_ranked_set_cardinality_is_expressible_in_canonical_event_ir() -> None:
    """구조 계약: 이 의미가 요구하는 계산은 전부 **이미 선언된** capability 다."""
    expression = event_ir.condition_from_dict(ranked_set_cardinality_expression())
    capabilities = event_ir.expression_capabilities(expression)

    assert {
        "aggregate.count_distinct",
        "relation.membership_join",
        "relation.ranked_limit",
    } <= capabilities, sorted(capabilities)
    # 새 capability 를 요구하지 않는다는 것이 이 조합의 핵심이다.
    assert capabilities <= event_ir.CAPABILITIES


def test_ranked_set_cardinality_resolves_and_compiles_to_sql() -> None:
    plan = build_plan(ranked_set_cardinality_expression())

    assert plan["semantic_ir"]["status"] == "resolved", plan["semantic_ir"]
    assert plan.get("event_expression") is not None
    assert not plan.get("unresolved_source_conditions")

    candidate = graph_rag.compile_executable_plan(plan)
    assert candidate is not None, "canonical 경로가 SQL 후보를 내지 못했다"
    sql = candidate["sql"]

    # 별칭에 의존하지 않는 의미 조각만 본다.
    assert re.search(r"COUNT\(\s*DISTINCT\s+\w+\.\w+\s*\)", sql), sql
    assert re.search(r"\bTOP\s+5\b", sql), sql
    assert re.search(r"ORDER BY\s+SUM\(\s*\w+\.\w+\s*\)\s+DESC", sql), sql
    # 동점 타이브레이크(엔터티 키 오름차순)가 정렬 뒤에 붙는다.
    assert re.search(r"DESC\s*,\s*\w+\.\w+\s+ASC", sql), sql
    # 교집합 크기 임계.
    assert re.search(r">=\s*2\b", sql), sql
    # 회원 구매 행과 랭킹 집합을 잇는 멤버십(=교집합)이 실제로 있다.
    assert "EXISTS" in sql and "GROUP BY" in sql, sql


def test_ranked_obligation_is_discharged_by_the_cardinality_expression() -> None:
    """영수증 계약: 애플리케이션이 계산한 의무를 이 표현이 실제로 방면한다."""
    expression = event_ir.condition_from_dict(ranked_set_cardinality_expression())
    obligation = _ranked_obligation()

    assert canonical_audience_claims.ranked_obligation_is_compiled(
        expression, obligation.value
    ), obligation.to_dict()


def test_ranked_obligation_records_the_ranking_contract() -> None:
    """의무는 랭킹 계약(엔터티·측정·방향·개수)을 값으로 들고 있다 — 프롬프트/검증의 단일 진실원천."""
    value = _ranked_obligation().value

    assert value["source"] == RANK_SOURCE
    assert value["entity_field"] == ENTITY_FIELD
    assert value["measure_function"] == "sum"
    assert value["measure_field"] == MEASURE_FIELD
    assert value["direction"] == "top"
    assert value["limit"] == 5


def test_ranked_obligation_records_the_intersection_cardinality() -> None:
    """'중 2개 이상'은 랭킹 집합 크기(5)도 구매 횟수도 아니다 — 교집합의 distinct 엔터티 수다."""
    cardinality = _ranked_obligation().value["cardinality"]

    assert dict(cardinality) == {"operator": ">=", "value": 2, "distinct": True}


# ── 영수증(discharge) 계약 ────────────────────────────────────────────────────────
#
# 아래는 전부 "이 표현이 그 의무를 방면하는가"의 경계값이다. 한 칸이 느슨해지면 요청보다 넓은
# 오디언스가 조용히 SQL 로 나간다.


def _obligation_value() -> dict:
    return dict(_ranked_obligation().value)


def _discharges(expression: dict, value: dict | None = None) -> bool:
    return canonical_audience_claims.ranked_obligation_is_compiled(
        event_ir.condition_from_dict(expression), value or _obligation_value()
    )


def test_exact_count_distinct_shape_discharges_the_cardinality_obligation() -> None:
    assert _discharges(ranked_set_cardinality_expression())


def test_exists_cannot_discharge_a_cardinality_obligation() -> None:
    """Exists 는 '1개 이상'이라 '상위 5개 중 2개 이상'보다 넓다."""
    membership_only = {
        "type": "exists",
        "relation": _membership_join(),
        "evidence": _evidence("작년에 가장 많이 팔린 상품 5개 중 2개 이상 구매"),
    }

    assert not _discharges(membership_only)


def test_non_distinct_count_is_rejected() -> None:
    """같은 상품을 두 번 산 것은 2개가 아니다."""
    expression = ranked_set_cardinality_expression()
    expression["left"]["distinct"] = False

    assert not _discharges(expression)


def test_counting_a_different_entity_is_rejected() -> None:
    expression = ranked_set_cardinality_expression()
    expression["left"]["expression"]["name"] = "purchase_line.order_id"

    assert not _discharges(expression)


def test_a_different_comparison_operator_is_rejected() -> None:
    expression = ranked_set_cardinality_expression()
    expression["operator"] = ">"

    assert not _discharges(expression)


def test_a_different_threshold_is_rejected() -> None:
    expression = ranked_set_cardinality_expression()
    expression["right"]["value"] = 3

    assert not _discharges(expression)


def test_counting_outside_the_ranked_membership_is_rejected() -> None:
    """같은 모양의 집계라도 랭킹 집합과의 교집합이 아니면 다른 수를 센다."""
    expression = ranked_set_cardinality_expression()
    expression["left"]["relation"] = {"type": "source", "name": RANK_SOURCE}

    assert not _discharges(expression)


def test_a_broken_ranked_relation_is_rejected() -> None:
    expression = ranked_set_cardinality_expression()
    # 랭킹 크기가 계약과 다르면 그 집합은 요청된 집합이 아니다.
    expression["left"]["relation"]["right"]["count"] = 7

    assert not _discharges(expression)


def test_a_plain_ranked_membership_still_discharges_without_cardinality() -> None:
    """개수 임계가 없는 기존 요청은 예전 형상 그대로 통과한다(회귀 방지)."""
    plain_query = "가장 많이 팔린 상품 5개를 구매한 회원"
    obligations = [
        requirement
        for requirement in semantic_requirements.capture_source_semantic_obligations(plain_query)
        if semantic_requirements.obligation_kind(requirement) == "ranked_entity_set"
    ]
    assert len(obligations) == 1
    value = dict(obligations[0].value)
    assert "cardinality" not in value

    membership_only = {
        "type": "exists",
        "relation": _membership_join(),
        "evidence": _evidence("가장 많이 팔린 상품 5개"),
    }
    assert _discharges(membership_only, value)
