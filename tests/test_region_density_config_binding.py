"""밀집 지역 랭킹의 기본값·상한·NULL 정책이 설정에서 온다는 계약.

배선 전에는 상위 N 이 ``int(density.get("top_n", 5))`` 로 코드에 박혀 있었고 설정의
``default_top_n``·``max_top_n``·``exclude_null_or_empty`` 는 아무도 읽지 않았다. 설정만 고친
사람에게는 아무 일도 일어나지 않는 상태였고, 증상이 예외가 아니라 '조금 다른 SQL'이라
드러나지도 않는다. 여기서는 세 값이 실제로 SQL 을 바꾼다는 것을 고정한다.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402


@pytest.fixture
def region_density_config(monkeypatch: pytest.MonkeyPatch):
    """member_target_filters 의 region_density 절만 갈아 끼운다."""

    def _apply(**overrides: Any) -> None:
        filters = copy.deepcopy(graph_rag._MEMBER_TARGET_FILTERS)
        section = dict(filters.get("region_density") or {})
        section.update(overrides)
        filters["region_density"] = section
        monkeypatch.setattr(graph_rag, "_MEMBER_TARGET_FILTERS", filters)

    return _apply


def test_top_n_falls_back_to_configured_default(region_density_config) -> None:
    """문장에 개수가 없으면 설정 기본값이다 — 코드 상수가 아니다."""

    region_density_config(default_top_n=7, max_top_n=30)
    assert graph_rag._region_density_top_n(None) == 7


def test_requested_top_n_wins_within_the_ceiling(region_density_config) -> None:
    region_density_config(default_top_n=5, max_top_n=30)
    assert graph_rag._region_density_top_n(12) == 12


def test_top_n_is_clamped_by_configured_ceiling(region_density_config) -> None:
    """상한이 선언돼 있으면 넘길 수 없다(선언이 강제되지 않으면 상한은 장식이다)."""

    region_density_config(default_top_n=5, max_top_n=30)
    assert graph_rag._region_density_top_n(500) == 30


@pytest.mark.parametrize("requested", [0, -3, "많이", None, True])
def test_invalid_top_n_falls_back_to_default(region_density_config, requested: Any) -> None:
    """0·음수·문자열·bool 은 개수가 아니다 — 조용히 TOP 0 을 만들지 않는다."""

    region_density_config(default_top_n=5, max_top_n=30)
    assert graph_rag._region_density_top_n(requested) == 5


def test_default_ceiling_absent_means_no_clamp(region_density_config) -> None:
    region_density_config(default_top_n=5, max_top_n=None)
    assert graph_rag._region_density_top_n(500) == 500


def test_exclude_null_or_empty_is_declared_not_assumed(region_density_config) -> None:
    region_density_config(exclude_null_or_empty=False)
    assert graph_rag._region_density_excludes_empty() is False

    region_density_config(exclude_null_or_empty=True)
    assert graph_rag._region_density_excludes_empty() is True


def test_missing_declaration_keeps_previous_behaviour(region_density_config) -> None:
    """미선언 설정이 동작을 바꾸면 배선이 아니라 회귀다 — 기존 동작(제외)을 유지한다."""

    region_density_config(exclude_null_or_empty=None)
    assert graph_rag._region_density_excludes_empty() is True


def test_dense_region_sql_uses_configured_values(region_density_config) -> None:
    """단위 함수가 아니라 실제 SQL 에 반영되는지 — 배선의 종착점을 본다."""

    region_density_config(default_top_n=3, max_top_n=30, exclude_null_or_empty=False)
    query_plan: dict[str, Any] = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {},
        "campaign_constraints": {},
        "region_density_target": {"column": "SIGUNGU"},
    }
    compiled = graph_rag.compile_member_target_conditions(query_plan)
    candidate = graph_rag._build_dense_region_targets_candidate(
        query_plan, compiled, query_plan["region_density_target"]
    )
    sql = candidate["sql"]
    assert "SELECT TOP 3 B.SIGUNGU" in sql
    assert "B.SIGUNGU IS NOT NULL" not in sql

    region_density_config(default_top_n=3, max_top_n=30, exclude_null_or_empty=True)
    candidate = graph_rag._build_dense_region_targets_candidate(
        query_plan, compiled, query_plan["region_density_target"]
    )
    assert "B.SIGUNGU IS NOT NULL" in candidate["sql"]
