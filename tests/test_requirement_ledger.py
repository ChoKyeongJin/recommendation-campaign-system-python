"""요구사항 원장 계약 — 조건 하나가 끝까지 추적되는가.

강제하는 것:
  - 요구사항마다 9개 정보가 **전부** 보존된다(id·라벨·원문 스팬·정규화 술어·시간 한정어·
    필요 그레인·capability 판정과 사유·후보 슬롯·최종 검증 결과).
  - 실패의 종류가 분리된다: 인자 누락 / 모호 / 연산 미지원 / 그레인 미지원 / 데이터 부재 /
    실행 실패 / 의미검증 실패 / 내부 오류.
  - 내부 오류·실행 실패가 '미지원'으로 접히지 않는다.
  - 조건이 하나라도 귀결되지 않으면 원장은 complete 가 아니다(가짜 성공 차단).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import requirement_ledger as ledger_mod  # noqa: E402
import semantic_pipeline  # noqa: E402
import semantic_plan  # noqa: E402
import temporal_semantics  # noqa: E402
from requirement_ledger import RequirementLedger  # noqa: E402


@pytest.fixture(scope="module")
def context():
    return graph_rag._semantic_compile_context()


def _plan(query: str, nodes: list[dict]):
    return semantic_plan.plan_from_dict({"nodes": nodes}, source_query=query)


def _run(query: str, nodes: list[dict], context) -> semantic_pipeline.PipelineResult:
    return semantic_pipeline.run(
        query, extract=lambda _q: (_plan(query, nodes), []), context=context
    )


CART_QUERY = "장바구니 상품 종류가 2개 이상인 회원"
CART_NODE = {
    "id": "req-1", "type": "aggregate_predicate", "source_span": "상품 종류가 2개 이상",
    "scope": "cart", "metric": "cart_line_count", "operator": "이상", "value": "2개",
}


# ── 레코드가 보존하는 정보 ───────────────────────────────────────────────────────
def test_requirement_preserves_every_declared_field(context) -> None:
    result = _run(CART_QUERY, [CART_NODE], context)
    requirement = result.ledger.by_id("req-1")
    assert requirement is not None

    payload = requirement.to_dict()
    for key in (
        "requirement_id", "label", "source_span", "predicate", "temporal",
        "required_grain", "capability", "candidate_slots", "validation",
    ):
        assert key in payload, f"원장 레코드에 {key} 가 없다"

    assert payload["requirement_id"] == "req-1"
    assert payload["label"], "조건 라벨이 비었다"
    assert payload["source_span"] == "상품 종류가 2개 이상"
    # 정규화된 술어 — 원문 표현이 아니라 확정된 값이 실린다.
    assert payload["predicate"]["operator"] == ">="
    assert payload["predicate"]["value"] == {"value": 2, "unit": "item_quantity"}
    assert payload["candidate_slots"], "후보 출력 슬롯이 비었다"
    assert payload["validation"]["outcome"] == ledger_mod.COMPILED


def test_capability_axes_are_carried_with_a_reason(context) -> None:
    """capability 는 참/거짓 하나가 아니라 축별 판정 + 사유로 실린다."""
    query = "브랜드 판매 순위 상위 5개"
    node = {
        "id": "req-1", "type": "ranked_set", "source_span": query,
        "entity": "brand", "metric": "purchase_amount", "direction": "descending",
        "limit": {"type": "count", "value": 5},
    }
    requirement = _run(query, [node], context).ledger.by_id("req-1")
    assert requirement is not None
    assert requirement.outcome == ledger_mod.UNSUPPORTED
    assert requirement.capability.get("reason"), "미지원 사유가 비었다"
    assert requirement.capability.get("executable_in_request") is False


def test_temporal_qualifier_is_normalized_onto_the_record(context) -> None:
    query = "최신 기준월 구매등급이 VIP인 회원"
    node = {
        "id": "req-1", "type": "relation_predicate", "source_span": query,
        "subject": "member", "attribute": "member_grade", "relation": "as_of", "value": "vip",
    }
    requirement = _run(query, [node], context).ledger.by_id("req-1")
    assert requirement is not None
    assert requirement.temporal is not None
    assert requirement.temporal["operator"] == temporal_semantics.AS_OF


# ── 실패 분류 ────────────────────────────────────────────────────────────────────
def test_missing_argument_is_pending_not_unsupported(context) -> None:
    incomplete = {**CART_NODE}
    incomplete.pop("value")
    requirement = _run(CART_QUERY, [incomplete], context).ledger.by_id("req-1")
    assert requirement is not None
    assert requirement.outcome == ledger_mod.PENDING
    assert requirement.failure_code == semantic_plan.MISSING_ARGUMENT
    assert "value" in (requirement.validation.get("missing_fields") or [])


def test_outcome_mapping_separates_internal_faults_from_unsupported() -> None:
    assert ledger_mod.outcome_for(semantic_plan.UNSUPPORTED_SEMANTICS) == ledger_mod.UNSUPPORTED
    assert ledger_mod.outcome_for(semantic_plan.UNSUPPORTED_DATA_GRAIN) == ledger_mod.UNSUPPORTED
    assert ledger_mod.outcome_for(semantic_plan.DATA_UNAVAILABLE) == ledger_mod.UNSUPPORTED
    # 우리 쪽 사고는 능력의 부재가 아니다.
    assert ledger_mod.outcome_for(semantic_plan.EXECUTION_FAILURE) == ledger_mod.FAILED
    assert ledger_mod.outcome_for(semantic_plan.INTERNAL_FAULT) == ledger_mod.FAILED
    assert ledger_mod.outcome_for(semantic_plan.VALIDATION_MISMATCH) == ledger_mod.FAILED
    # 사용자에게 물어야 하는 것.
    assert ledger_mod.outcome_for(semantic_plan.MISSING_ARGUMENT) == ledger_mod.PENDING
    assert ledger_mod.outcome_for(semantic_plan.AMBIGUOUS_REQUIREMENT) == ledger_mod.PENDING
    assert ledger_mod.outcome_for(None) == ledger_mod.COMPILED


def test_every_failure_code_maps_to_an_outcome() -> None:
    """닫힌 집합의 모든 코드가 귀결을 갖는다 — 새 코드가 조용히 pending 으로 새지 않게."""
    for code in semantic_plan.FAILURE_CODES:
        assert ledger_mod.outcome_for(code) in ledger_mod.OUTCOMES


def test_value_outside_declared_vocabulary_is_a_failed_requirement(context) -> None:
    """생산자 계약 위반은 내부 불량(failed)이지 미지원이 아니다."""
    bad = {**CART_NODE, "scope": "made_up_scope"}
    requirement = _run(CART_QUERY, [bad], context).ledger.by_id("req-1")
    assert requirement is not None
    assert requirement.outcome == ledger_mod.FAILED
    assert requirement.failure_code == semantic_plan.VALIDATION_MISMATCH


def test_uncovered_source_span_becomes_a_requirement(context) -> None:
    """원문에 근거가 있는데 노드가 없으면 조용히 사라지지 않고 원장에 오른다."""
    query = "장바구니 상품 종류가 2개 이상이고 총금액이 10만 원 이상인 회원"
    result = _run(query, [CART_NODE], context)
    uncovered = result.ledger.with_outcome(ledger_mod.UNCOVERED)
    assert uncovered, "미커버 구간이 원장에 오르지 않았다"
    assert any("10만" in item.source_span for item in uncovered)


# ── 가짜 성공 차단 ───────────────────────────────────────────────────────────────
def test_ledger_is_not_complete_when_any_requirement_is_unresolved(context) -> None:
    query = "장바구니 상품 종류가 2개 이상이고 총금액이 10만 원 이상인 회원"
    result = _run(query, [CART_NODE], context)
    assert not result.ledger.is_complete()
    assert result.ledger.unresolved()


def test_empty_ledger_is_not_complete() -> None:
    """조건이 하나도 기록되지 않은 상태를 '완료'로 읽으면 그것이 가짜 성공이다."""
    assert not RequirementLedger().is_complete()


def test_protected_ids_are_exactly_the_compiled_requirements(context) -> None:
    query = "장바구니 상품 종류가 2개 이상이고 총금액이 10만 원 이상인 회원"
    result = _run(query, [CART_NODE], context)
    assert result.ledger.protected_ids() == {"req-1"}


def test_merge_outcomes_detects_regression() -> None:
    def _requirement(requirement_id: str, outcome: str) -> ledger_mod.Requirement:
        return ledger_mod.Requirement(
            requirement_id=requirement_id, label="x", validation={"outcome": outcome}
        )

    before = RequirementLedger([_requirement("a", ledger_mod.COMPILED)])
    after = RequirementLedger([_requirement("a", ledger_mod.PENDING)])
    delta = ledger_mod.merge_outcomes(before, after)
    assert delta["regressed"] == ["a"] and delta["gained"] == []


def test_clarification_targets_quote_the_source_span(context) -> None:
    incomplete = {**CART_NODE}
    incomplete.pop("value")
    result = _run(CART_QUERY, [incomplete], context)
    targets = ledger_mod.clarification_targets(result.ledger)
    assert targets and targets[0]["source_span"] == "상품 종류가 2개 이상"
    assert targets[0]["reason"]
