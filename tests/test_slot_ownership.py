"""출처 구간(span) 기반 소유권 회수.

조건 소유권 조정이 ``plan.pop(...)`` 이던 동안, 삭제 근거는 '조건의 종류'뿐이었다. 그래서 순위 절이
자기 기간을 소유한다는 이유로 ``purchase_date`` 를 지우면 실제로는 **뒤쪽 절의 구매 시점**이 지워졌다.
여기서 강제하는 계약:

  1. 슬롯에는 출처 구간이 기록된다(어느 필터가 원문 어디를 읽었는지).
  2. 회수는 소유 구간과 출처 구간이 겹칠 때만 일어난다 — 겹치지 않으면 종류가 같아도 보존한다.
  3. 회수된 값은 사라지지 않고 ``superseded_conditions`` 에 사유와 함께 남는다.
  4. 구간을 모르면 기존 동작(종류 기준 회수)이다 — 점진 도입이 조건을 새로 흘리지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import slot_ownership
from calendar_window import parse_calendar_window_group_span, parse_calendar_window_span
from entity_set import parse_entity_set_condition
from graph_rag import _entity_set_config, build_query_plan, build_sql_template_candidate


QUERY_SAME_CLAUSE = "2019년 가장 많이 팔린 상품 10개를 구매한 고객"
QUERY_TWO_CLAUSES = "2019년 가장 많이 팔린 상품 10개를 2020년 3월에 구매한 고객"


def _plan_with_span(slot: str, span: tuple[int, int], source: str, value: object = "값") -> dict:
    plan: dict = {"target_user": {slot: value}}
    slot_ownership.record_slot_span(plan, slot, span, source_text=source, container="target_user")
    return plan


def test_disjoint_span_keeps_the_other_clause_condition() -> None:
    """소유 구간 밖에서 온 조건은 종류가 같아도 회수하지 않는다."""
    source = "0123456789"
    plan = _plan_with_span("purchase_date", (6, 10), source)

    outcome = slot_ownership.claim_slot(
        plan, "target_user", "purchase_date",
        owner="entity_set_condition", reason="순위 절 기간", source_text=source, owner_span=(0, 4),
    )

    assert outcome == "kept"
    assert plan["target_user"]["purchase_date"] == "값"
    record = slot_ownership.superseded_conditions(plan)[0]
    assert record["outcome"] == "kept"
    assert record["owner"] == "entity_set_condition"


def test_overlapping_span_is_reclaimed_and_recorded() -> None:
    """소유 구간과 겹치는 조건은 회수하되 기록을 남긴다(조용한 삭제 금지)."""
    source = "0123456789"
    plan = _plan_with_span("purchase_date", (0, 4), source)

    outcome = slot_ownership.claim_slot(
        plan, "target_user", "purchase_date",
        owner="entity_set_condition", reason="순위 절 기간", source_text=source, owner_span=(0, 5),
    )

    assert outcome == "removed"
    assert "purchase_date" not in plan["target_user"]
    record = slot_ownership.superseded_conditions(plan)[0]
    assert record["outcome"] == "removed"
    assert record["value"] == "값"
    assert record["reason"] == "순위 절 기간"


def test_unknown_span_falls_back_to_kind_based_reclaim() -> None:
    """구간을 모르는 슬롯은 기존 동작대로 회수한다 — 점진 도입이 조건을 흘리지 않는다."""
    plan: dict = {"target_user": {"purchase_membership": True}}

    outcome = slot_ownership.claim_slot(
        plan, "target_user", "purchase_membership",
        owner="entity_set_condition", reason="관계 중복", source_text="아무 문장", owner_span=(0, 5),
    )

    assert outcome == "removed"
    assert slot_ownership.superseded_conditions(plan)[0]["detail"] == slot_ownership.UNKNOWN_SPAN_REASON


def test_span_from_a_different_sentence_is_not_trusted() -> None:
    """다른 문장에서 만든 구간은 좌표계가 다르다 — 믿지 않고 기존 동작으로 돌아간다."""
    plan = _plan_with_span("purchase_date", (6, 10), "전혀 다른 문장입니다 여기가 구간")

    outcome = slot_ownership.claim_slot(
        plan, "target_user", "purchase_date",
        owner="entity_set_condition", reason="순위 절 기간", source_text="0123456789", owner_span=(0, 4),
    )

    assert outcome == "removed"


def test_list_slot_reclaims_only_the_owned_value() -> None:
    """리스트 슬롯은 소유한 값만 빼고 나머지 행동 조건은 남긴다."""
    plan: dict = {"target_user": {"behaviors": ["cart_abandoner", "repeat_buyer"]}}

    slot_ownership.claim_slot(
        plan, "target_user", "behaviors", value="cart_abandoner",
        owner="entity_set_condition", reason="순위 절 관계", source_text="문장",
    )

    assert plan["target_user"]["behaviors"] == ["repeat_buyer"]


def test_calendar_window_span_locates_the_group_it_selected() -> None:
    """창 문법 소유자가 위치도 함께 준다(구간 계산이 문법에 딸린 정보)."""
    text = "2018년 및 2019년에 구매한 회원"
    start, end = parse_calendar_window_group_span(text)
    assert text[start:end] == "2018년 및 2019년"

    narrow_start, narrow_end = parse_calendar_window_span("2019년 3월과 2018년")
    assert "2019년 3월"[: narrow_end - narrow_start] == "2019년 3월"[: narrow_end - narrow_start]
    assert "2019년 3월과 2018년"[narrow_start:narrow_end] == "2019년 3월"


def test_entity_set_node_reports_raw_query_spans() -> None:
    """순위 절이 원문의 어느 구간을 읽었는지 요소별로 돌려준다(원문 좌표)."""
    node = parse_entity_set_condition(QUERY_TWO_CLAUSES, _entity_set_config())
    assert node is not None
    spans = node["spans"]

    def text(key: str) -> str:
        start, end = spans[key]
        return QUERY_TWO_CLAUSES[start:end]

    assert text("window") == "2019년"
    assert text("entity") == "상품"
    assert text("count").replace(" ", "") == "10개"
    assert "구매" in text("relation")


def test_ranking_clause_does_not_steal_another_clause_purchase_date() -> None:
    """순위 절의 기간('2019년')이 뒤쪽 절의 구매 시점('2020년 3월')을 지우지 않는다."""
    plan = build_query_plan(QUERY_TWO_CLAUSES, parser="rules")

    assert plan["target_user"].get("entity_set_condition") is not None
    purchase_date = plan["target_user"].get("purchase_date")
    assert purchase_date is not None, "다른 절이 만든 구매 시점 조건이 조용히 사라졌다"
    assert purchase_date["from"] == "20200301"
    assert purchase_date["to"] == "20200331"

    kept = [
        record for record in slot_ownership.superseded_conditions(plan)
        if record["slot"] == "target_user.purchase_date"
    ]
    assert kept and kept[0]["outcome"] == "kept"


def test_ranking_clause_still_reclaims_its_own_window() -> None:
    """같은 절이 만든 기간은 종전대로 회수한다 — 순위 창이 구매 시점으로 이중 해석되면 안 된다."""
    plan = build_query_plan(QUERY_SAME_CLAUSE, parser="rules")

    assert plan["target_user"].get("entity_set_condition") is not None
    assert not plan["target_user"].get("purchase_date")
    removed = [
        record for record in slot_ownership.superseded_conditions(plan)
        if record["slot"] == "target_user.purchase_date"
    ]
    assert removed and removed[0]["outcome"] == "removed"


def test_preserved_condition_is_announced_when_the_builder_cannot_compile_it() -> None:
    """보존된 조건이 SQL 에 없으면 부분추출로 고지한다 — 살려두고 조용히 무시하면 같은 사고다."""
    plan = build_query_plan(QUERY_TWO_CLAUSES, parser="rules")
    candidate = build_sql_template_candidate(dict(plan))

    assert candidate is not None
    assert "target_user.purchase_date" in candidate["dropped_conditions"]
    assert "구매일 조건" in candidate["dropped_condition_labels"]

    # 같은 절이 소유한 기간은 회수됐으므로 고지 대상이 아니다(거짓 경고 금지).
    same_clause = build_sql_template_candidate(dict(build_query_plan(QUERY_SAME_CLAUSE, parser="rules")))
    assert same_clause is not None
    assert "target_user.purchase_date" not in same_clause["dropped_conditions"]


def test_ranking_clause_reclaims_the_entity_it_owns() -> None:
    """순위 절의 엔터티(상품)는 리터럴 상품 조건으로 남지 않는다(기존 계약 유지)."""
    plan = build_query_plan(QUERY_SAME_CLAUSE, parser="rules")

    assert not plan["target_user"].get("purchase_object")
    assert not plan["target_user"].get("purchase_objects")
