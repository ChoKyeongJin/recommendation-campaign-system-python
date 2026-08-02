"""적재 부족은 **차단이 아니라 고지**다 — 그 정책의 배선 계약.

기준은 "행이 나오는가"가 아니라 **"SQL 이 틀리는가"**다:

  · 적재가 얕아 결과가 0건  → SQL 의 의미는 원문 그대로다. 내보내고 이름을 대며 고지한다.
  · 의미가 접히는 컴파일    → 다른 대상을 내는 오답이다. 여전히 막는다(컴파일러의 일).

이 파일이 지키는 것은 그 구분이 **응답까지 살아서 도착하는가**이다. 판정 계층만 고쳐 놓고
배선이 자문을 버리면 사용자에게는 "왜 0건인지 알 수 없는 SQL"이 나가고, 그건 조용한 실패다.
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


def test_out_of_range_month_still_emits_sql_with_a_named_advisory() -> None:
    """적재 밖 기준월도 SQL 을 낸다 — 결과가 0건인 이유를 이름 대어 함께 싣는다."""
    result, _plan = _run(QUERY, _as_of_month_node())

    assert result["sql"], "적재 깊이는 차단 사유가 아니다 — SQL 은 나가야 한다."
    advisories = result[RESPONSE_KEY]
    assert advisories, "0건일 수 있다는 사실이 응답에서 사라지면 조용한 실패가 된다."
    assert any("0건" in str(item.get("message")) for item in advisories)
    assert {item.get("source") for item in advisories} <= {"capability", "attribute_history"}


def test_advisories_never_travel_on_the_blocking_channel() -> None:
    """`dropped_signal_warnings` 는 이름과 달리 **차단 채널**이다 — 고지를 실으면 SQL 이 막힌다."""
    result, _plan = _run(QUERY, _as_of_month_node())

    assert result["sql"]
    blocking = " ".join(str(item) for item in result.get("dropped_signal_warnings") or [])
    for item in result[RESPONSE_KEY]:
        assert str(item.get("message")) not in blocking


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
