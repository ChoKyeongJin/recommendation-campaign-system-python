"""출처 구간(span) 기반 소유권 회수 회귀 테스트.

배경: ``slot_ownership`` 은 처음부터 span 기반으로 설계됐지만, 구간을 선언한 결정론 필터가 33개 중
3개뿐이라 나머지는 ``claim_slot`` 이 '구간 미상 → 종류 기준 회수'(옛 ``plan.pop`` 동작)로 퇴화했다.
그 상태에서는 순위 절이 소유권을 주장할 때 **문장의 다른 절이 만든 동종 조건까지** 함께 지워진다.

이 파일은 두 가지를 강제한다.

  1. 소유권 회수 대상 슬롯을 채우는 필터는 반드시 출처 구간을 선언한다(레지스트리 불변식).
     — 새 필터가 구간 없이 들어와 조용히 옛 동작으로 퇴화하는 것을 막는다.
  2. 순위 절 밖에서 만들어진 조건은 종류가 같아도 보존된다(동작 회귀).
"""

from __future__ import annotations

import graph_rag
import slot_ownership


# 순위 절(``_apply_entity_set_condition``)이 소유권을 주장하는 슬롯 ↔ 그 슬롯을 채우는 결정론 필터.
# 이 목록의 필터가 구간을 선언하지 않으면 회수가 '종류 기준'으로 퇴화한다.
_CLAIMED_SLOT_OWNERS: tuple[tuple[str, str], ...] = (
    ("purchase_object", "target_user.purchase_object"),
    ("purchase_date", "target_user.purchase_date"),
    ("purchase_inactivity", "target_user.purchase_inactivity"),
    ("cart_presence", "target_user.behaviors:cart_abandoner"),
    ("cart_absence", "target_user.cart_absence"),
    ("result_limit", "plan.result_limit"),
    ("member_metric_ranking", "plan.member_metric_ranking"),
    ("purchase_count_ranking", "plan.purchase_count_ranking"),
    ("group_ranking", "plan.group_ranking_target"),
    ("balance_selection", "plan.member_metric_selection"),
)


def test_claimed_slot_filters_declare_source_span() -> None:
    """회수 대상 슬롯을 채우는 필터는 전부 출처 구간을 선언한다."""
    registry = graph_rag._deterministic_filter_registry()
    missing = [
        f"{name} ({slot})"
        for name, slot in _CLAIMED_SLOT_OWNERS
        if registry[name].span is None
    ]
    assert not missing, (
        "소유권 회수 대상 슬롯인데 출처 구간 미선언 — 회수가 '종류 기준'으로 퇴화한다: "
        + ", ".join(missing)
    )


def test_declared_span_slots_are_covered_by_a_locator() -> None:
    """span_slots 를 선언한 필터는 위치추적기도 함께 선언한다(선언만 하고 못 찾는 상태 방지)."""
    registry = graph_rag._deterministic_filter_registry()
    orphaned = [name for name, spec in registry.items() if spec.span_slots and spec.span is None]
    assert not orphaned, f"span_slots 만 있고 위치추적기가 없는 필터: {orphaned}"


def test_claimed_slot_filters_are_reconfirmed_against_the_source_prompt() -> None:
    """회수 대상 슬롯을 채우는 필터는 전부 '원문 권위 재확정' 단계에 등록돼 있다.

    출처 구간은 그것을 만든 텍스트의 오프셋이다(``slot_ownership._source_compatible``). 계획은
    LLM 재작성본(plan_query)으로 만들고 소유권 회수는 원문(targeting_prompt)으로 하므로, 재확정
    단계에 빠진 필터의 구간은 좌표계가 어긋나 신뢰를 잃고 판정이 옛 '종류 기준 회수'로 되돌아간다.
    """
    stages = {
        name
        for name, _run, _reason in graph_rag._source_authoritative_stages(
            sql_schema=graph_rag.DEFAULT_SCHEMA_PATH,
            normalization_rules=graph_rag.DEFAULT_NORMALIZATION_PATH,
        )
    }
    missing = [name for name, _slot in _CLAIMED_SLOT_OWNERS if name not in stages]
    assert not missing, (
        "회수 대상 슬롯 필터가 원문 재확정 단계에 없다 — 출처 구간 좌표계가 어긋난다: "
        + ", ".join(missing)
    )


def test_source_authoritative_stage_names_are_unique() -> None:
    """단계 이름은 감사 로그의 키라 중복되면 어느 단계가 바꿨는지 되짚을 수 없다."""
    names = [
        name
        for name, _run, _reason in graph_rag._source_authoritative_stages(
            sql_schema=graph_rag.DEFAULT_SCHEMA_PATH,
            normalization_rules=graph_rag.DEFAULT_NORMALIZATION_PATH,
        )
    ]
    duplicated = sorted({name for name in names if names.count(name) > 1})
    assert not duplicated, f"중복된 재확정 단계 이름: {duplicated}"


def test_compact_source_span_maps_back_to_original_coordinates() -> None:
    """공백 제거 좌표계의 구간이 원문 좌표로 정확히 되돌아온다."""
    query = "최근 30일 동안 구매하지 않은 회원"
    compact = query.replace(" ", "")
    start = compact.index("구매하지")
    span = graph_rag._compact_source_span(query, (start, start + len("구매하지")))
    assert span is not None
    assert query[span[0]: span[1]].replace(" ", "") == "구매하지"


def test_compact_source_span_rejects_out_of_range() -> None:
    """좌표가 범위를 벗어나면 '모름'을 돌려준다(잘못된 구간보다 안전)."""
    assert graph_rag._compact_source_span("짧은문장", (0, 999)) is None
    assert graph_rag._compact_source_span("짧은문장", (3, 3)) is None


def test_ranking_clause_keeps_purchase_inactivity_from_a_later_clause() -> None:
    """순위 절의 '구매하지 않은'이 뒤 절의 구매 미발생 조건을 삼키지 않는다.

    두 절 모두 구매 부정 표현이라 종류 기준 회수에서는 뒤 절 조건이 통째로 사라졌다.
    """
    query = "2019년 가장 많이 팔린 상품 10개를 구매하지 않은 고객 중 최근 30일 동안 구매하지 않은 회원"
    plan = graph_rag.build_query_plan(query, parser="rules")

    assert (plan.get("target_user") or {}).get("entity_set_condition"), "순위 절이 구조화되지 않았다"
    inactivity = (plan.get("target_user") or {}).get("purchase_inactivity")
    assert inactivity == {"value": 30, "unit": "days", "min_days": 30}, (
        f"뒤 절의 구매 미발생 조건이 순위 절에 회수됐다: {inactivity!r}"
    )

    kept = [
        record for record in slot_ownership.superseded_conditions(plan)
        if record.get("slot") == "target_user.purchase_inactivity"
    ]
    assert kept and kept[0]["outcome"] == "kept", "보존 판정이 감사 기록에 남지 않았다"


def test_ranking_clause_keeps_member_metric_selection_from_a_later_clause() -> None:
    """순위 절의 극값 표현이 뒤 절의 회원 지표 선택 조건을 삼키지 않는다."""
    query = "2019년 가장 많이 팔린 상품 10개를 구매한 고객 중 예치금이 가장 많은 50명"
    plan = graph_rag.build_query_plan(query, parser="rules")

    assert (plan.get("target_user") or {}).get("entity_set_condition"), "순위 절이 구조화되지 않았다"
    selection = plan.get("member_metric_selection")
    assert isinstance(selection, dict), "뒤 절의 잔액 선택 조건이 순위 절에 회수됐다"
    assert selection.get("column") == "DEPOSIT_BALANCE_AMT"
    assert selection.get("n") == 50


def test_slot_span_records_the_surface_text_that_produced_the_value() -> None:
    """기록된 구간의 텍스트가 실제로 그 조건을 만든 표면어다(구간이 밀리지 않았다)."""
    query = "2019년 가장 많이 팔린 상품 10개를 구매한 고객 중 예치금이 가장 많은 50명"
    plan = graph_rag.build_query_plan(query, parser="rules")

    recorded = slot_ownership.slot_span(plan, "member_metric_selection", container="plan")
    assert recorded is not None, "잔액 선택 슬롯의 출처 구간이 기록되지 않았다"
    assert recorded["text"] == "예치금"


def test_ranking_clause_still_claims_its_own_clause_conditions() -> None:
    """겹치는 구간은 여전히 회수한다 — span 도입이 회수 자체를 무력화하지 않는다."""
    query = "2019년 가장 많이 팔린 상품 10개를 구매한 고객"
    plan = graph_rag.build_query_plan(query, parser="rules")

    target_user = plan.get("target_user") or {}
    assert target_user.get("entity_set_condition"), "순위 절이 구조화되지 않았다"
    # 순위 절의 '상품'·'2019년'은 리터럴 상품/구매시점 조건이 아니라 순위 집합 정의의 일부다.
    assert not target_user.get("purchase_object"), "순위 절의 엔터티가 리터럴 상품 조건으로 남았다"
    assert not target_user.get("purchase_date"), "순위 절의 기간이 구매 시점 조건으로 남았다"

    removed = {
        record["slot"] for record in slot_ownership.superseded_conditions(plan)
        if record.get("outcome") == "removed"
    }
    assert "target_user.purchase_date" in removed, "회수 판정이 감사 기록에 남지 않았다"
