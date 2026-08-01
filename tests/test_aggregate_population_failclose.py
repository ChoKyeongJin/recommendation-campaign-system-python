"""집계 계약의 모집단 주입이 fail-close 를 우회하지 않는다.

집계 질의의 코호트 조건(``여성``/``20대``)은 회원 목록 슬롯이 아니라 계약의 모집단 필터로
컴파일된다. 그때 슬롯을 비우는데, 슬롯이 비었다는 사실만 보는 하류 게이트 두 개가 있다 —
'조건이 사라졌다'(_deterministic_dropped_conditions)와 '요청 안 한 조건이 붙었다'
(validate_unmentioned_sql_conditions). 이 두 게이트의 면제 근거는 **계약 그 자체**여야 한다.

위험은 대칭이다: 면제가 없으면 정상 질의가 SQL 없이 죽고, 면제가 너무 넓으면 SQL 에 실리지도
않은 조건이 '처리됨'으로 통과해 조용한 오답이 나간다. 아래는 그 양쪽을 모두 고정한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402


def _plan(query: str) -> dict:
    return graph_rag.build_query_plan(query, parser="rules")


def _sql(plan: dict) -> str:
    candidate = graph_rag.build_sql_template_candidate(plan)
    return (candidate or {}).get("sql") or ""


@pytest.mark.parametrize(
    "query, predicates",
    [
        ("20대 여성 회원이 가장 많은 시군구 10곳", ("GENDER_CD", "AGE >= 20", "AGE <= 29")),
        ("여성 회원 수가 많은 시군구 상위 5개", ("GENDER_CD",)),
        ("여성 회원의 총 구매금액을 알려줘", ("GENDER_CD",)),
        # 지역 모집단(dimension_filters)은 삭제된 빌더가 컴파일하던 조건이다 — 계약이 이어받는다.
        ("서울에서 회원 수가 많은 시군구 상위 5개", ("B.SIDO IN ('서울')",)),
        ("부산에서 회원 수가 적은 시군구 3곳", ("B.SIDO IN ('부산')",)),
        ("VIP 회원이 가장 많은 시도 3개", ("EMART_GRADE_CD",)),
    ],
)
def test_cohort_conditions_reach_the_sql(query: str, predicates: tuple[str, ...]) -> None:
    """모집단 조건은 슬롯에서 사라지되 **SQL 에는 반드시 남아야** 한다."""
    plan = _plan(query)
    sql = _sql(plan)
    assert sql, f"모집단이 붙은 집계 질의가 SQL 을 못 냈다: {query!r}"
    for predicate in predicates:
        assert predicate in sql, f"{predicate!r} 가 SQL 에서 사라졌다: {query!r}"


def test_owned_slots_are_derived_from_the_contract() -> None:
    """면제 근거는 별도 목록이 아니라 계약에 실린 필터다(목록과 계약이 어긋날 여지를 없앤다)."""
    plan = _plan("20대 여성 회원이 가장 많은 시군구 10곳")
    owned = graph_rag._analytical_owned_audience_slots(plan)
    assert {"gender", "age_min", "age_max"} <= owned
    filters = (plan.get("analytical_intent") or {}).get("filters") or []
    declared = {slot for item in filters for slot in (item.get("ownsSlots") or ())}
    assert declared, "코호트 필터가 회수한 슬롯을 계약에 적어두지 않았다"


def test_uncompilable_condition_still_fails_closed() -> None:
    """계약이 담지 못한 조건은 슬롯에 남아 미지원으로 보고돼야 한다 — 조용히 사라지면 안 된다."""
    plan = _plan("커피를 구매한 회원이 가장 많은 시군구 5곳")
    sql = _sql(plan)
    unsupported = plan.get("unsupported")
    dropped = graph_rag._analytical_dropped_conditions(plan)
    assert unsupported or dropped or "PRODUCT" in sql.upper(), (
        "상품 조건이 SQL 에도 없고 미지원 보고에도 없다 — 조건이 조용히 증발했다"
    )


def test_empty_plan_owns_nothing() -> None:
    """집계 계약이 없으면 어떤 슬롯도 면제되지 않는다(회원 목록 경로 불변)."""
    assert graph_rag._analytical_owned_audience_slots({}) == frozenset()
    assert graph_rag._analytical_owned_audience_slots(_plan("구매금액 10만원 이상인 회원")) == frozenset()
