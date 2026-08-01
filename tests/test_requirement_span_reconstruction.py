"""source requirement 스팬의 결정론 재구성 — LLM 슬롯이 전문장 폴백으로 떨어지지 않음을 고정.

배경(2026-08-01 실사고): LLM 방출 슬롯에는 정밀 스팬 기록이 없어 모든 requirement 가 전체문장
스팬(0, len(query))을 받았다. 그 결과 스팬 기반 중복 판별·소유권 회수·리터럴 소비 감사가 전부
무력화됐다(같은 문장에서 aggregate 와 behaviors 가 이중 방출돼도 같은 스팬이라 구별 불가).

여기서 강제하는 계약:
  1. semantic_evidence(경로 일치, V4 가 본문 일치를 보장) → 정밀 스팬 + span_precision="evidence".
  2. literal_bindings(정규화값 구조 동치) → "literal". 본문 불일치(다른 좌표계)는 채택하지 않는다.
  3. 레지스트리 동의어(behaviors) 원문 유일 출현 → "registry".
  4. 후보가 유일하지 않으면(같은 숫자 2회 등) 전문장 폴백 + "whole_query" — 임의 선택 금지.
  5. span_precision 은 to_dict 에 실려 소비자(리터럴 감사 등)가 폴백을 구별할 수 있다.
  6. 재구성 스팬이 붙어도 digest 봉인·검증 왕복은 그대로 성립한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import semantic_requirements  # noqa: E402

_QUERY = "최근 6개월 주문 건수가 5건 이상인 회원"


def _capture_one(query: str, plan: dict):
    requirements = semantic_requirements.capture_plan_source_requirements(query, plan)
    assert requirements, "캡처 대상 슬롯이 없다 — fixture 를 확인하라."
    return requirements[0]


def test_evidence_span_wins_with_path_match() -> None:
    plan = {
        "target_user": {"aggregate_conditions": [
            {"metric_id": "order_count", "operator": ">=", "threshold": 5, "window_days": 180}
        ]},
        "semantic_evidence": [
            {"path": "target_user.aggregate_conditions[0]", "text": "주문 건수가 5건 이상", "start": 7, "end": 19}
        ],
    }
    requirement = _capture_one(_QUERY, plan)
    assert dict(requirement.source_span) == {"start": 7, "end": 19}
    assert requirement.span_precision == "evidence"
    assert requirement.source_text == "주문 건수가 5건 이상"


def test_evidence_with_mismatched_text_is_rejected() -> None:
    """본문 불일치는 다른 좌표계(재작성 프롬프트 등) 신호다 — 채택하면 스팬이 오염된다."""
    plan = {
        "target_user": {"aggregate_conditions": [
            {"metric_id": "order_count", "operator": ">=", "threshold": 5}
        ]},
        "semantic_evidence": [
            {"path": "target_user.aggregate_conditions[0]", "text": "완전히 다른 문구", "start": 7, "end": 19}
        ],
    }
    requirement = _capture_one(_QUERY, plan)
    assert requirement.span_precision == "whole_query"


def test_literal_binding_span_by_normalized_value() -> None:
    start = _QUERY.index("5건")
    plan = {
        "target_user": {"aggregate_conditions": [
            {"metric_id": "order_count", "operator": ">=", "threshold": 5, "window_days": 180}
        ]},
        "literal_bindings": [{
            "id": "number_with_unit_1", "kind": "number_with_unit", "text": "5건",
            "start": start, "end": start + 2, "value": 5,
            "normalized": {"value": 5, "surface_unit": "건", "semantic_unit": "order_count"},
        }],
    }
    requirement = _capture_one(_QUERY, plan)
    assert dict(requirement.source_span) == {"start": start, "end": start + 2}
    assert requirement.span_precision == "literal"


def test_ambiguous_literal_candidates_fall_back() -> None:
    """같은 정규화값의 리터럴이 둘이면 임의 선택하지 않는다(소유권 좌표 오염 방지)."""
    query = "5건 이상 구매했고 5회 방문한 회원"
    first = query.index("5건")
    second = query.index("5회")
    plan = {
        "target_user": {"aggregate_conditions": [
            {"metric_id": "order_count", "operator": ">=", "threshold": 5}
        ]},
        "literal_bindings": [
            {"id": "a", "kind": "number_with_unit", "text": "5건", "start": first, "end": first + 2,
             "value": 5, "normalized": {"value": 5}},
            {"id": "b", "kind": "number_with_unit", "text": "5회", "start": second, "end": second + 2,
             "value": 5, "normalized": {"value": 5}},
        ],
    }
    requirement = _capture_one(query, plan)
    assert requirement.span_precision == "whole_query"
    assert dict(requirement.source_span) == {"start": 0, "end": len(query)}


def test_sibling_element_does_not_inherit_indexed_evidence() -> None:
    """[1]+ 원소의 requirement 가 형제([0]/무인덱스) 증거 스팬을 상속하면 다른 조건의 좌표가
    정밀 스팬으로 봉인된다 — 인덱스 제거 폴백은 [0]/무인덱스 경로에만 허용된다."""
    query = "재구매 고객 중 첫 구매 고객"
    plan = {
        "target_user": {"behaviors": ["repeat_buyer", "first_purchase"]},
        "semantic_evidence": [
            {"path": "target_user.behaviors[0]", "text": "재구매", "start": 0, "end": 3}
        ],
    }
    requirements = semantic_requirements.capture_plan_source_requirements(query, plan)
    by_path = {requirement.path: requirement for requirement in requirements}
    second = by_path["target_user.behaviors[1]"]
    assert second.span_precision != "evidence", (
        f"behaviors[1] 이 behaviors[0] 의 증거 스팬을 상속했다: {dict(second.source_span)}"
    )


def test_registry_synonym_span_for_behaviors() -> None:
    query = "재구매 고객을 대상으로 캠페인"
    plan = {"target_user": {"behaviors": ["repeat_buyer"]}}
    requirement = _capture_one(query, plan)
    assert requirement.span_precision == "registry"
    assert requirement.source_text == "재구매"


def test_span_precision_is_serialized_and_digest_roundtrips() -> None:
    plan = {"target_user": {"behaviors": ["repeat_buyer"]}}
    requirements = semantic_requirements.capture_plan_source_requirements("재구매 고객", plan)
    payload = requirements[0].to_dict()
    assert payload["span_precision"] == "registry"
    holder: dict = {}
    semantic_requirements.attach_source_requirements(holder, requirements)
    assert semantic_requirements.verify_source_requirements(holder) is True
