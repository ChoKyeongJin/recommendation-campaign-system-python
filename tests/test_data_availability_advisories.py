"""적재 범위 판정의 차단/고지 경계를 고정한다.

기준은 "행이 나오는가"가 아니라 **"SQL 이 틀리는가"**다:

  · 요청 기간이 완전 적재 범위 밖 → 관측했다는 전제가 거짓이므로 SQL 을 막는다.
  · 요청 기간이 완전 적재 범위 안 → SQL 을 내며 상시 경고를 붙이지 않는다.
  · 의미가 접히는 컴파일    → 다른 대상을 내는 오답이다. 여전히 막는다(컴파일러의 일).

완전 적재가 선언되지 않은 월에 0건이 나온 것은 "조건을 만족한 회원이 없음"과 구분할 수 없다.
그래서 그 경우는 advisory 로 SQL 옆에 싣지 않고 ``data_coverage_gap`` 으로 정직하게 닫는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import networkx as nx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402

RESPONSE_KEY = "data_availability_advisories"
QUERY = "2025년 12월 기준 골드 등급 회원"


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


def _as_of_month_node() -> list[dict]:
    return [{
        "id": "r1", "type": "relation_predicate", "source_span": "2025년 12월 기준 골드 등급",
        "subject": "member", "attribute": "member_grade", "relation": "as_of",
        "value": "골드", "value_comparison": "eq",
        "period": {"from": "20251201", "to": "20251231"},
    }]


def test_out_of_range_month_is_blocked_with_a_named_coverage_gap() -> None:
    result, plan = _run(QUERY, _as_of_month_node())

    assert not result["is_success"] and result["sql"] is None
    assert result["failure_reason"] == "semantic_ir_unsupported"
    assert result["interpretation_status"] == "unsupported"
    unsupported = plan["semantic_ir"]["unsupported_operations"]
    assert unsupported[0]["kind"] == "data_coverage_gap"
    assert "2025-12-01..2025-12-31" in unsupported[0]["reason"]


def test_out_of_range_coverage_never_downgrades_to_an_advisory() -> None:
    result, _plan = _run(QUERY, _as_of_month_node())

    assert result["sql"] is None
    assert result["failure_reason"] == "semantic_ir_unsupported"
    assert not result.get(RESPONSE_KEY)


def test_advisory_is_absent_when_the_window_is_inside_coverage() -> None:
    """고지는 사실일 때만 붙는다 — 상시 경고는 곧 무시되는 경고다."""
    query = "최신 기준월 골드 등급 회원"
    result, _plan = _run(query, [{
        "id": "r1", "type": "relation_predicate", "source_span": "최신 기준월 골드 등급",
        "subject": "member", "attribute": "member_grade", "relation": "as_of",
        "value": "골드", "value_comparison": "eq",
    }])

    assert result["sql"]
    assert result[RESPONSE_KEY] == []
