"""원문 요구 → SemanticPlan 노드 → SQL(또는 정직한 차단) 전 구간 회귀.

앞 계층 테스트들은 각 단계를 따로 고정한다. 여기서는 **배선**을 고정한다 —
단계는 다 맞는데 graph_rag 안에서 순서가 뒤집히거나 게이트 하나가 조용히 먼저
차단하면 사용자에게는 여전히 실패이기 때문이다(2026-08-01 '도달 불가' 사고의 교훈).

각 케이스의 노드는 LLM 이 낼 것으로 기대하는 형태를 손으로 적은 것이다(LLM 호출 없음).
26종 감사 군집 중 컴파일 경로가 있는 조건을 대표한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import networkx as nx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402


def _run(query: str, nodes: list[dict]) -> tuple[dict, dict]:
    plan = graph_rag.build_query_plan(query, parser="rules")
    plan["intent"] = "find_user_segment"
    for node in nodes:
        span = node.get("source_span") or ""
        if span and span in query:
            node["source_start"] = query.index(span)
            node["source_end"] = node["source_start"] + len(span)
    plan["semantic_plan"] = {"nodes": nodes}
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100, original_query=query,
    )
    return result, plan


CASES = [
    pytest.param(
        "장바구니 상품 종류가 2개 이상이고 총금액이 10만 원 이상인 회원",
        [
            {"id": "r1", "type": "aggregate_predicate", "source_span": "상품 종류가 2개 이상",
             "scope": "cart", "metric": "cart_line_count", "operator": "이상", "value": "2개"},
            {"id": "r2", "type": "aggregate_predicate", "source_span": "총금액이 10만 원 이상",
             "scope": "cart", "metric": "cart_amount", "operator": "이상", "value": "10만 원"},
        ],
        ["COUNT(DISTINCT CART_PRODUCT_NO) >= 2", "SUM(TOTAL_SALE_PRICE) >= 100000"],
        id="cart-aggregate",
    ),
    pytest.param(
        "2026년 2월과 3월의 구매금액이 증가한 회원",
        [{"id": "r1", "type": "metric_comparison",
          "source_span": "2026년 2월과 3월의 구매금액이 증가한", "metric": "purchase_amount",
          "baseline": {"type": "calendar_month", "year": 2026, "month": 2},
          "current": {"type": "calendar_month", "year": 2026, "month": 3},
          "relation": "increase"}],
        ["20260201", "20260301"],
        id="metric-trend",
    ),
    pytest.param(
        "누적 구매금액 상위 10% 회원을 추출해줘",
        [{"id": "r1", "type": "ranked_set", "source_span": "누적 구매금액 상위 10%",
          "entity": "member", "metric": "누적 구매금액", "direction": "descending",
          "limit": {"type": "percent", "value": 10}}],
        ["TOP 10 PERCENT", "TOTAL_BUY_AMT"],
        id="member-metric-ranking",
    ),
    pytest.param(
        "최근 캠페인 발송 성공 횟수가 3회 이상인 회원",
        [{"id": "r1", "type": "aggregate_predicate",
          "source_span": "캠페인 발송 성공 횟수가 3회 이상", "scope": "campaign",
          "metric": "발송 성공", "event": "campaign_contact", "operator": "이상", "value": "3회"}],
        ["campaign_contact"],
        id="campaign-frequency",
    ),
    pytest.param(
        "캠페인별 구매반응 금액이 평균 10만 원 이상인 회원",
        [{"id": "r1", "type": "aggregate_predicate",
          "source_span": "구매반응 금액이 평균 10만 원 이상", "scope": "campaign",
          "metric": "campaign_buy_amount", "operator": "이상", "value": "10만 원",
          "aggregation": "avg"}],
        # 캠페인당 평균은 SUM/COUNT(DISTINCT 캠페인 실행) 으로 컴파일된다 — 합계 임계와
        # 바뀌지 않았음을 분모 존재로 고정한다.
        ["SUM(R.BUY_AMT)", "COUNT(DISTINCT CONCAT(R.CAMP_ID"],
        id="campaign-avg-amount",
    ),
    pytest.param(
        "2019년 가장 많이 팔린 상품 10개를 구매한 고객",
        [{"id": "r1", "type": "entity_set_membership",
          "source_span": "가장 많이 팔린 상품 10개를 구매한 고객",
          "member_entity": "member", "relation": "purchase",
          "ranked_set": [{"id": "r1a", "type": "ranked_set",
                          "source_span": "가장 많이 팔린 상품 10개", "entity": "product",
                          "metric": "sales_quantity", "direction": "descending",
                          "limit": {"type": "count", "value": 10},
                          "period": {"type": "absolute", "from": "2019-01-01", "to": "2019-12-31"}}]}],
        ["EXISTS"],
        id="entity-set-membership",
    ),
    pytest.param(
        "지난달 말 기준 VIP였던 회원을 찾아줘",
        [{"id": "r1", "type": "relation_predicate", "source_span": "지난달 말 기준 VIP였던",
          "subject": "member", "attribute": "member_grade", "relation": "as_of", "value": "VIP"}],
        ["CRM_MB_MONTHCRMINFO", "MEM_GRADE_CD.VIP"],
        id="grade-as-of",
    ),
    pytest.param(
        "2019년 3월 같은 상품을 동시 구매한 고객수",
        [{"id": "r1", "type": "relation_predicate",
          "source_span": "같은 상품을 동시 구매한 고객수", "subject": "member",
          "attribute": "product", "relation": "co_purchase",
          "period": {"type": "absolute", "from": "2019-03-01", "to": "2019-03-31"}}],
        ["CONDITION_GROUPS", "20190301"],
        id="co-purchase-count",
    ),
]


@pytest.mark.parametrize("query,nodes,expected_sql_fragments", CASES)
def test_semantic_plan_compiles_all_the_way_to_sql(query, nodes, expected_sql_fragments) -> None:
    result, plan = _run(query, nodes)
    assert result["is_success"] is True, (
        f"{query} -> {result.get('failure_reason')} / "
        f"{(result.get('clarification_questions') or [None])[0]}"
    )
    sql = result["sql"]
    for fragment in expected_sql_fragments:
        assert fragment in sql, f"{query}: SQL 에 '{fragment}' 가 없다\n{sql}"
    assert plan["semantic_ir"]["status"] == "resolved"
    assert plan[graph_rag.semantic_plan_bridge.PIPELINE_KEY]["written_slots"]


def test_held_throughout_emits_sql_with_a_coverage_advisory() -> None:
    """'최근 3개월 내내 VIP 유지' — 값 앵커 구간 판정으로 SQL 이 나가고, 0건 가능성만 고지된다.

    적재 깊이는 차단 사유가 아니다(0건은 정직한 답이다). 그리고 이 문형의 컴파일러는
    2026-08-02 에 신설됐다 — 그 전에는 '미구현'으로 막혔다.
    """
    result, _plan = _run("최근 3개월 내내 VIP 등급을 유지한 회원", [
        {"id": "r1", "type": "relation_predicate", "source_span": "최근 3개월 내내 VIP 등급을 유지한",
         "subject": "member", "attribute": "member_grade", "relation": "held_throughout",
         "value": "VIP", "months": 3},
    ])
    assert result["sql"], "값 앵커 구간 판정은 이제 SQL 을 낸다."
    assert "SUM(CASE WHEN ATTRIBUTE_VALUE IN ('MEM_GRADE_CD.VIP')" in result["sql"]
    advisories = result["data_availability_advisories"]
    assert any(item["code"] == "data_coverage_shallow" for item in advisories)


def test_state_history_without_a_source_still_blocks() -> None:
    """소스가 없으면 낼 SQL 이 없다 — 적재가 얕은 것과 달리 여전히 정직하게 막는다."""
    result, _plan = _run("정상에서 휴면으로 바뀐 회원", [
        {"id": "r1", "type": "relation_predicate", "source_span": "정상에서 휴면으로 바뀐",
         "subject": "member", "attribute": "member_state", "relation": "transition",
         "from_value": "정상", "to_value": "휴면"},
    ])
    assert result["is_success"] is False and result["sql"] is None
    message = " ".join(result.get("clarification_questions") or [])
    assert "적재되어 있지 않습니다" in message


def test_missing_condition_fails_closed_without_sql() -> None:
    """조건 하나를 노드로 못 담으면 SQL 을 내지 않는다(가짜 성공 차단)."""
    result, plan = _run("장바구니 상품 종류가 2개 이상이고 총금액이 10만 원 이상인 회원", [
        {"id": "r1", "type": "aggregate_predicate", "source_span": "상품 종류가 2개 이상",
         "scope": "cart", "metric": "cart_line_count", "operator": "이상", "value": "2개"},
    ])
    assert result["is_success"] is False
    assert result["sql"] is None
    assert plan["semantic_ir"]["status"] == "needs_clarification"
    assert any(field.startswith("uncovered:") for field in plan["semantic_ir"]["missing_fields"])
