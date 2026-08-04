"""적재 범위 판정의 SQL 생성/고지 경계를 고정한다.

이 프로젝트의 산출물은 SQL 이다. 스냅샷 적재 여부는 SQL 생성 capability가 아니므로:

  · 요청 기간이 현재 적재 범위 밖 → 요청 기간을 보존한 SQL 을 낸다.
  · 요청 기간이 현재 적재 범위 안 → SQL 을 내며 상시 경고를 붙이지 않는다.
  · 의미가 접히는 컴파일    → 다른 대상을 내는 오답이다. 여전히 막는다(컴파일러의 일).

적재 범위 선언은 운영 데이터에 대한 관찰일 뿐, 미래·다른 환경에서 실행할 SQL의 유효성을
제한하지 않는다. 데이터 가용성 고지는 실행 경로가 제공하는 경우에만 부가 정보로 취급한다.
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


def test_out_of_range_month_still_generates_sql_with_exact_requested_month() -> None:
    result, _plan = _run(QUERY, _as_of_month_node())

    assert result["is_success"] and result["sql"] is not None
    assert "MS.YYYYMM >= '202512'" in result["sql"]
    assert "MS.YYYYMM < '202601'" in result["sql"]
    assert result.get("failure_reason") is None


def test_out_of_range_coverage_does_not_block_sql() -> None:
    result, _plan = _run(QUERY, _as_of_month_node())

    assert result["sql"] is not None
    assert result["is_success"]
    assert result.get("failure_reason") is None


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
