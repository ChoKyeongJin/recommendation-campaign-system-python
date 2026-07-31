from __future__ import annotations

import networkx as nx
import pytest

import compositional_targeting
import graph_rag


def _empty_plan() -> dict:
    return {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "set_expressions": [],
    }


def test_generic_grade_history_phrase_returns_physical_binding_clarification() -> None:
    query = "최근 12개월 동안 등급이 한 번도 바뀌지 않은 회원을 추출해줘."
    plan = _empty_plan()

    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )

    assert result["is_success"] is False
    assert result["failure_reason"] == "relational_ir_needs_clarification"
    assert "월별 변경 이력이 없습니다" in result["clarification_questions"][0]
    assert "가치 등급" in result["clarification_questions"][0]
    assert plan["condition_claims"][0]["owner"] == "relational_ir"


@pytest.mark.parametrize(
    ("attribute_phrase", "value_column"),
    [
        ("가치 등급", "WORTH_GRADE"),
        ("상태 등급", "STATE_GRADE"),
    ],
)
def test_catalog_bound_attribute_uses_shared_temporal_compiler(
    attribute_phrase: str, value_column: str,
) -> None:
    query = f"최근 12개월 동안 {attribute_phrase}이 한 번도 바뀌지 않은 회원을 추출해줘."
    plan = _empty_plan()

    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=query,
    )

    assert result["is_success"] is True
    assert result["selected"]["id"] == graph_rag.RELATIONAL_TARGETING_CANDIDATE_ID
    assert f"S.{value_column}" in result["sql"]
    assert "COUNT(DISTINCT SNAPSHOT_TIME) = 12" in result["sql"]
    assert "COUNT(DISTINCT ATTRIBUTE_VALUE) = 1" in result["sql"]
    assert "HAVING COUNT(DISTINCT S." + value_column + ") = 1" in result["sql"]


def test_change_count_is_an_operator_not_an_attribute_specific_capability() -> None:
    query = "최근 6개월 동안 상태 등급이 2회 이상 변경된 회원 찾아줘"

    ir = compositional_targeting.interpret(query)

    assert ir is not None
    assert ir["status"] == "resolved"
    assert ir["attribute"]["id"] == "member.grade.state"
    assert ir["aggregate"] == "change_count"
    assert ir["comparison"] == {"operator": "gte", "value": 2}
