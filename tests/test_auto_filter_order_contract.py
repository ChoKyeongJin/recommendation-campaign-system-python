"""auto 경로 결정론 필터의 실행 순서 계약을 지킨다.

auto 경로는 필터 목록을 두 구간으로 나눠 돌고, 그 사이에 집합식 operand 값 복원을 끼운다.
값 인덱스(member_value/dimension) 계열이 끝난 **뒤**, 랭킹 감지가 시작되기 **전**이어야 한다.

예전에는 이 요구가 `_AUTO_FILTERS[:5]` / `[5:]` 라는 **숫자**로만 표현돼 있었다. 튜플 앞쪽에
필터를 하나 추가하면 경계가 조용히 밀려 값 복원이 엉뚱한 시점에 끼어드는데, 컴파일도 테스트도
그걸 못 잡는다 — 증상은 "규칙 경로는 되는데 auto 만 실패"로 나타난다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402

# 값 인덱스 계열 — 경계 앞에 있어야 한다(집합식 operand 가 복원할 값을 이들이 만든다).
VALUE_INDEX_FILTERS = ("dimension", "member_value", "macro_region")
# 랭킹 감지 계열 — 경계 뒤에 있어야 한다(복원된 값 위에서 판정해야 한다).
RANKING_FILTERS = ("group_ranking", "region_member_count", "region_density")


def test_boundary_filter_exists_in_the_registry() -> None:
    """경계 이름이 오타거나 필터가 사라지면 분할 자체가 터진다(조용한 오작동보다 낫다)."""

    assert graph_rag._AUTO_FILTER_ENRICHMENT_BOUNDARY in graph_rag._AUTO_FILTERS


def test_value_index_filters_run_before_enrichment() -> None:
    before, _ = graph_rag._split_auto_filters_at_enrichment()
    missing = [name for name in VALUE_INDEX_FILTERS if name not in before]
    assert not missing, (
        f"값 인덱스 필터가 값 복원 뒤로 밀렸다: {missing}. "
        "집합식 operand 가 복원할 값이 아직 없는 상태에서 복원이 돈다."
    )


def test_ranking_filters_run_after_enrichment() -> None:
    _, after = graph_rag._split_auto_filters_at_enrichment()
    missing = [name for name in RANKING_FILTERS if name not in after]
    assert not missing, (
        f"랭킹 감지 필터가 값 복원 앞으로 당겨졌다: {missing}. "
        "복원되지 않은 operand 위에서 랭킹을 판정하게 된다."
    )


def test_split_covers_every_filter_exactly_once() -> None:
    before, after = graph_rag._split_auto_filters_at_enrichment()
    assert before + after == graph_rag._AUTO_FILTERS
    assert not (set(before) & set(after)), "두 구간에 같은 필터가 들어 있다."


def test_every_listed_filter_is_registered() -> None:
    """이름 목록과 레지스트리가 갈라지면 등록 누락이 조용한 무동작이 된다."""

    registry = set(graph_rag._deterministic_filter_registry())
    unknown = sorted(set(graph_rag._AUTO_FILTERS) - registry)
    assert not unknown, f"레지스트리에 없는 필터 이름: {unknown}"


def test_no_orphan_filter_spec() -> None:
    """어느 경로에도 안 들어간 spec 은 영영 실행되지 않는다."""

    paths = set(graph_rag._AUTO_FILTERS) | set(graph_rag._RULES_PRE_FILTERS) | set(graph_rag._RULES_POST_FILTERS)
    orphans = sorted(set(graph_rag._deterministic_filter_registry()) - paths)
    assert not orphans, f"어느 실행 경로에도 없는 필터: {orphans}"
