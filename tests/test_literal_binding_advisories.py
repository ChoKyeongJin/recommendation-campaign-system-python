"""미소비 리터럴 감사(비차단) — 바인딩만 되고 어떤 실행 조건에도 소비되지 않은 리터럴의 진단 계약.

배경(2026-08-01 실사고): '최근 3개월 주문은 있었지만'의 '3', '상위 10%'의 '10%'가 literal_bindings
에 바인딩만 된 채 어느 슬롯도 참조하지 않았고, 결정론 드롭 감지기는 숫자/기간 family 를 오탐
우려로 명시 제외해 아무도 알리지 않았다. 이 감사는 그 사각을 **비차단 자문**으로 채운다 —
항목이 있어도 SQL 은 나간다(차단 승격은 graph_rag 의 invariants 인자 한 곳).

여기서 강제하는 계약:
  1. 어떤 소비 축에도 걸리지 않은 숫자/기간 리터럴은 advisory 로 승격된다.
  2. 소비 축 3종이 각각 단독으로 소비를 성립시킨다 — semantic_ir literal_id 참조 /
     슬롯 값 구조 동치(날짜창 from-to, 숫자 정규화값) / 정밀 스팬 requirement 의 포함.
  3. 전문장 폴백 스팬(span_precision="whole_query")은 소비 근거가 아니다 — 모든 리터럴을
     덮으므로 인정하면 감사 전체가 공허해진다.
  4. comparison_operator 리터럴은 항상 제외한다(값 리터럴의 부속 — 단독 소비 개념이 없다).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import semantic_requirements  # noqa: E402


def _number(literal_id: str, text: str, value: float, start: int = 0) -> dict:
    return {
        "id": literal_id, "kind": "number", "text": text,
        "start": start, "end": start + len(text), "value": value, "normalized": value,
    }


def test_unconsumed_number_is_reported() -> None:
    plan = {"literal_bindings": [_number("number_1", "3", 3)], "target_user": {}}
    advisories = semantic_requirements.unconsumed_literal_advisories(plan)
    assert [entry["literal_id"] for entry in advisories] == ["number_1"]
    entry = advisories[0]
    assert entry["status"] == "advisory"
    assert entry["source"] == "literal_binding_audit"
    assert entry["source_span"] == {"start": 0, "end": 1}


def test_value_equivalence_consumes() -> None:
    plan = {
        "literal_bindings": [_number("number_1", "5", 5)],
        "target_user": {"aggregate_conditions": [{"metric_id": "order_count", "operator": ">=", "threshold": 5}]},
    }
    assert semantic_requirements.unconsumed_literal_advisories(plan) == []


def test_semantic_ir_binding_reference_consumes() -> None:
    plan = {
        "literal_bindings": [{
            "id": "percentage_1", "kind": "percentage", "text": "10%",
            "start": 0, "end": 3, "value": 10, "normalized": {"value": 10, "unit": "percent"},
        }],
        "semantic_ir": {"status": "resolved", "operations": [
            {"kind": "period_over_period_change", "metric_id": "purchase_amount", "direction": "increase",
             "bindings": [{"role": "threshold", "literal_id": "percentage_1"}]}
        ], "missing_fields": [], "policy_applications": [], "unsupported_operations": [], "message": None},
    }
    assert semantic_requirements.unconsumed_literal_advisories(plan) == []


def test_date_window_consumed_by_matching_slot() -> None:
    window = {"id": "date_window_1", "kind": "date_window", "text": "2026년 3월",
              "start": 0, "end": 8, "value": None,
              "normalized": {"from": "20260301", "to": "20260331", "label": "2026년 3월"}}
    consumed = {
        "literal_bindings": [dict(window)],
        "target_user": {"purchase_date": {"from": "20260301", "to": "20260331"}},
    }
    assert semantic_requirements.unconsumed_literal_advisories(consumed) == []
    dangling = {"literal_bindings": [dict(window)], "target_user": {}}
    advisories = semantic_requirements.unconsumed_literal_advisories(dangling)
    assert [entry["literal_id"] for entry in advisories] == ["date_window_1"]


def test_precise_requirement_span_consumes_but_whole_query_does_not() -> None:
    literal = _number("number_1", "3", 3, start=3)
    covering = {"source_span": {"start": 0, "end": 10}, "span_precision": "evidence"}
    fallback = {"source_span": {"start": 0, "end": 10}, "span_precision": "whole_query"}
    plan_precise = {"literal_bindings": [dict(literal)], "source_requirements": [covering]}
    plan_fallback = {"literal_bindings": [dict(literal)], "source_requirements": [fallback]}
    assert semantic_requirements.unconsumed_literal_advisories(plan_precise) == []
    assert len(semantic_requirements.unconsumed_literal_advisories(plan_fallback)) == 1


def test_unit_converted_number_is_consumed() -> None:
    """'최근 6개월'의 '6'은 window_days=180 으로 소비된 것이다 — 환산을 모르면 정상 프롬프트마다
    오탐 자문이 붙는다(비차단이지만 잡음이 감사의 신뢰를 갉는다)."""
    query = "최근 6개월 주문 건수가 5건 이상인 회원"
    start = query.index("6")
    plan = {
        "literal_bindings": [_number("number_1", "6", 6, start=start)],
        "target_user": {"aggregate_conditions": [
            {"metric_id": "order_count", "operator": ">=", "threshold": 5, "window_days": 180}
        ]},
    }
    assert semantic_requirements.unconsumed_literal_advisories(plan, query) == []
    # 환산 결과가 플랜에 없으면 여전히 보고된다(억제 과잉 방지).
    plan_no_window = {
        "literal_bindings": [_number("number_1", "6", 6, start=start)],
        "target_user": {"aggregate_conditions": [
            {"metric_id": "order_count", "operator": ">=", "threshold": 5}
        ]},
    }
    advisories = semantic_requirements.unconsumed_literal_advisories(plan_no_window, query)
    assert [entry["literal_id"] for entry in advisories] == ["number_1"]


def test_comparison_operator_is_never_reported() -> None:
    plan = {"literal_bindings": [{
        "id": "comparison_operator_1", "kind": "comparison_operator", "text": "이상",
        "start": 0, "end": 2, "value": ">=", "normalized": ">=",
    }]}
    assert semantic_requirements.unconsumed_literal_advisories(plan) == []
