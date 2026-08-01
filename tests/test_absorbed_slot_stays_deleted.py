"""등록형 집계 IR 로 흡수된 슬롯(region_member_count_target)이 되살아나지 않는다.

이 프로젝트의 반복 실패 모드는 '죽은 설정'이다 — 코드는 지웠는데 슬롯 이름이 분류 레지스트리·
요구 원장·의미 해소 목록에 남아, 아무도 만들지 않는 조건을 영원히 조회한다. 어떤 기존 테스트도
'없어야 할 이름'을 검사하지 않으므로(있는 것의 정합만 본다) 이 가드가 유일한 방어선이다.

되살리려면 이 파일을 지우는 것이 아니라, 왜 전용 슬롯이 다시 필요한지를 먼저 적어야 한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import plan_schema  # noqa: E402
import semantic_requirements  # noqa: E402
import targeting_ir  # noqa: E402

ABSORBED_SLOT = "region_member_count_target"
ABSORBED_FILTER = "region_member_count"

# 흔적을 남길 수 있는 소스·설정. 문서(docs/*.md)는 역사 서술이라 제외한다.
SEARCHED_FILES = (
    "graph_rag.py",
    "targeting_ir.py",
    "plan_schema.py",
    "semantic_requirements.py",
    "docs/data/semantic_resolution_registry.json",
    "docs/data/slot_policy.json",
    "docs/data/member_target_filters.json",
    "docs/data/analytics_registry.json",
)


def test_slot_is_not_declared_anywhere() -> None:
    assert plan_schema.kind_of(ABSORBED_SLOT) is None
    assert ABSORBED_SLOT not in {key.name for key in plan_schema.ALL}
    assert ABSORBED_SLOT not in semantic_requirements._PLAN_REQUIREMENT_SLOTS
    assert ABSORBED_SLOT not in {spec.kind for spec in targeting_ir.CONDITION_SPECS}


def test_no_builder_or_filter_owns_the_slot() -> None:
    assert ABSORBED_FILTER not in graph_rag._deterministic_filter_registry()
    assert ABSORBED_FILTER not in graph_rag._AUTO_FILTERS
    assert ABSORBED_FILTER not in graph_rag._RULES_POST_FILTERS
    owned = {kind for _builder, kinds in graph_rag._sql_target_builder_registry() for kind in kinds}
    assert ABSORBED_SLOT not in owned


@pytest.mark.parametrize("relative", SEARCHED_FILES)
def test_no_source_or_config_mentions_the_slot(relative: str) -> None:
    path = REPO_ROOT / relative
    if not path.exists():
        pytest.skip(f"{relative} 없음")
    assert ABSORBED_SLOT not in path.read_text(encoding="utf-8"), (
        f"{relative} 에 흡수된 슬롯 이름이 남아 있다 — 아무 코드도 만들지 않는 죽은 선언이다"
    )


# (질의, 그룹 컬럼, 정렬 방향, 상위 N) — 렌더된 SQL 이 실제로 이 의미를 담아야 한다.
ABSORBED = [
    ("회원 수가 많은 시군구 상위 5개", "B.SIGUNGU", "DESC", 5),
    ("회원 수가 적은 시도 3곳", "B.SIDO", "ASC", 3),
    ("시도별 회원 수 상위 3개", "B.SIDO", "DESC", 3),
    ("회원 수가 많은 동네 5곳", "B.DONG", "DESC", 5),
    ("회원 수 적은 시도 하위 3개", "B.SIDO", "ASC", 3),
    ("회원 수가 많은 시군구", "B.SIGUNGU", "DESC", None),
]


@pytest.mark.parametrize("query, column, direction, limit", ABSORBED)
def test_absorbed_queries_compile_with_the_requested_ranking(
    query: str, column: str, direction: str, limit: int | None
) -> None:
    """흡수의 존재 이유. 'SQL 이 나온다'가 아니라 **요청한 순위 의미가 SQL 에 있다**를 단언한다.

    정렬 방향이 뒤집히거나 상위 N 이 빠져도 문자열 두어 개만 보는 단언은 초록으로 남는다 —
    실제로 그 변이들이 전체 스위트를 통과했다.
    """
    plan = graph_rag.build_query_plan(query, parser="rules")
    candidate = graph_rag.build_sql_template_candidate(plan)
    assert candidate, f"흡수된 질의가 SQL 을 못 냈다: {query!r}"
    sql = candidate["sql"]
    assert plan.get("intent") == "analyze_aggregation", plan.get("intent")
    assert f"GROUP BY {column}" in sql, sql
    assert "COUNT(DISTINCT" in sql, sql
    assert f"ORDER BY COUNT(DISTINCT B.MEMBER_NO) {direction}" in sql, (
        f"정렬 방향이 요청과 다르다(조용한 의미 반전): {query!r}\n{sql}"
    )
    if limit is None:
        assert " TOP " not in sql, f"요청하지 않은 행 수 제한이 붙었다: {query!r}\n{sql}"
    else:
        assert f"SELECT TOP {limit} " in sql, f"요청한 상위 {limit} 이 SQL 에서 사라졌다: {query!r}\n{sql}"


def test_absorbed_ranking_survives_delivery() -> None:
    """후보가 아니라 **출고된 SQL** 기준으로도 순위가 남아야 한다(하류 검증 게이트 포함)."""
    import networkx as nx

    query = "서울에서 회원 수가 많은 시군구 상위 5개"
    plan = graph_rag.build_query_plan(query, parser="rules")
    result = graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 1000
    )
    assert result.get("is_success"), result.get("failure_reason")
    sql = result.get("sql") or ""
    assert "SELECT TOP 5 " in sql and "GROUP BY B.SIGUNGU" in sql, sql
    # 지역 모집단('서울')은 삭제된 빌더가 컴파일하던 조건이다 — 계약이 이어받았는지 확인한다.
    assert "B.SIDO IN ('서울')" in sql, f"지역 모집단 조건이 SQL 에서 사라졌다\n{sql}"
