"""파이프라인 계약 — 정규화 / coverage / capability / 실패 분류 / 가짜 성공 차단.

    원문 → SemanticPlan 추출 → 값 정규화 → coverage 검증(+재추출)
    → capability 판정 → 검증 → 결정론 컴파일

여기서 강제하는 것:
  - 값 정규화는 **값만** 본다(문장을 다시 읽지 않는다).
  - 원문에 근거가 있는데 노드가 없으면 uncovered 로 검출되고, 그 구간만 재추출된다.
  - 같은 근거에 다른 의미가 오면 조용히 고르지 않고 conflict(ambiguous)로 남는다.
  - status/missing/unsupported 는 전부 파생값이다.
  - 내부 오류는 '미지원'으로 표시되지 않는다.
  - 원문 requirement 가 빠진 채로 resolved 가 되지 않는다(가짜 성공 차단).
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import semantic_candidates  # noqa: E402
import semantic_capability  # noqa: E402
import semantic_coverage  # noqa: E402
import semantic_normalizers as norm  # noqa: E402
import semantic_pipeline  # noqa: E402
import semantic_plan  # noqa: E402
import semantic_plan_bridge  # noqa: E402
import semantic_reemission  # noqa: E402
from semantic_plan import SemanticPlanV2  # noqa: E402


def _plan(query: str, nodes: list[dict]) -> SemanticPlanV2:
    return semantic_plan.plan_from_dict({"nodes": nodes}, source_query=query)


@pytest.fixture(scope="module")
def context():
    return graph_rag._semantic_compile_context()


# ── 값 정규화 계층 ───────────────────────────────────────────────────────────────
def test_amount_normalizer_reads_values_not_sentences() -> None:
    assert norm.AmountNormalizer.normalize("10만 원") == norm.Money(100000, "KRW")
    assert norm.AmountNormalizer.normalize("이십만원") == norm.Money(200000, "KRW")
    assert norm.AmountNormalizer.normalize({"amount": 100000}) == norm.Money(100000, "KRW")
    assert norm.AmountNormalizer.normalize("2개") == norm.Quantity(2, "item_quantity")
    assert norm.AmountNormalizer.normalize("3종류") == norm.Quantity(3, "distinct_product_count")


def test_operator_and_rank_normalizers() -> None:
    assert norm.OperatorNormalizer.normalize("이상") == ">="
    assert norm.OperatorNormalizer.normalize("gte") == ">="
    assert norm.OperatorNormalizer.normalize("이내") == "<="
    assert norm.RankLimitNormalizer.normalize("상위 10%") == norm.RankLimit("percent", 10)
    assert norm.RankLimitNormalizer.normalize("상위 100명") == norm.RankLimit("count", 100)


def test_period_normalizer_resolves_calendar_and_relative() -> None:
    window = norm.PeriodNormalizer.normalize({"type": "calendar_month", "year": 2026, "month": 2})
    assert (window.start, window.end) == ("20260201", "20260228")
    rolling = norm.PeriodNormalizer.normalize({"type": "relative", "value": 30, "unit": "days"},
                                              today=date(2026, 3, 31))
    assert (rolling.start, rolling.end) == ("20260302", "20260331")


def test_normalizers_do_not_know_destination_slots() -> None:
    """정규화기는 목적지 슬롯 이름을 몰라야 한다 — 알면 값 계층이 슬롯 계층으로 새어든다."""
    import ast
    import inspect

    import legacy_plan_compiler

    tree = ast.parse(inspect.getsource(norm))
    docstrings = {
        ast.get_docstring(node, clean=False) for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
    }
    executable = " ".join(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value not in docstrings
    ) + " " + " ".join(
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    ) + " " + " ".join(
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    )
    for slot in legacy_plan_compiler.COMPILER_OWNED_SLOTS:
        assert slot.rpartition(".")[2] not in executable, f"정규화기가 슬롯을 안다: {slot}"


def test_normalization_failure_is_validation_not_unsupported() -> None:
    plan = _plan("x", [{"id": "req-1", "type": "predicate", "source_span": "x",
                        "subject": "member", "metric": "buy_cycle",
                        "operator": "텔레파시", "value": "30일"}])
    semantic_pipeline.normalize_plan(plan)
    assert plan.validation_errors
    assert plan.validation_errors[0]["failure_code"] == semantic_plan.VALIDATION_MISMATCH
    assert plan.status() == semantic_plan.STATUS_INVALID
    # 내부 불량을 '미지원'으로 표시하지 않는다.
    assert semantic_pipeline.project_semantic_ir(plan)["status"] == "needs_clarification"


# ── coverage 검증 ────────────────────────────────────────────────────────────────
COVERAGE_QUERY = "장바구니 상품 종류가 2개 이상이고 총금액이 10만 원 이상인 회원"


def test_missing_condition_is_detected_as_uncovered_span() -> None:
    """두 조건 중 하나를 LLM 이 누락하면 uncovered span 으로 검출된다."""
    plan = _plan(COVERAGE_QUERY, [{
        "id": "req-1", "type": "aggregate_predicate", "source_span": "상품 종류가 2개 이상",
        "scope": "cart", "metric": "cart_line_count", "operator": ">=", "value": 2,
    }])
    report = semantic_coverage.verify_coverage(COVERAGE_QUERY, plan)
    assert report.uncovered_requirements
    spans = " ".join(item["source_span"] for item in report.uncovered_requirements)
    assert "10만" in spans


def test_full_coverage_reports_nothing() -> None:
    plan = _plan(COVERAGE_QUERY, [
        {"id": "req-1", "type": "aggregate_predicate", "source_span": "상품 종류가 2개 이상",
         "scope": "cart", "metric": "cart_line_count", "operator": ">=", "value": 2},
        {"id": "req-2", "type": "aggregate_predicate", "source_span": "총금액이 10만 원 이상",
         "scope": "cart", "metric": "cart_amount", "operator": ">=", "value": "10만 원"},
    ])
    assert semantic_coverage.verify_coverage(COVERAGE_QUERY, plan).is_complete


def test_external_claims_prevent_false_uncovered_reports() -> None:
    """다른 소유자(V4 슬롯 계층)가 청구한 구간은 누락이 아니다(이행기 장치)."""
    query = "30대 여성 회원"
    plan = _plan(query, [])
    assert semantic_coverage.verify_coverage(query, plan).uncovered_requirements
    claimed = [(0, len(query))]
    assert semantic_coverage.verify_coverage(query, plan, claimed_spans=claimed).is_complete


def test_ungrounded_node_is_a_validation_error() -> None:
    plan = _plan("장바구니 총금액이 10만 원 이상", [{
        "id": "req-1", "type": "aggregate_predicate", "source_span": "원문에 없는 근거",
        "scope": "cart", "metric": "cart_amount", "operator": ">=", "value": "10만 원",
    }])
    report = semantic_coverage.verify_coverage("장바구니 총금액이 10만 원 이상", plan)
    assert report.ungrounded_nodes


def test_uncovered_span_triggers_targeted_patch_only(context) -> None:
    """누락은 슬롯 백필이 아니라 **그 구간만** 패치 재방출로 메운다."""
    asked: list[list[str]] = []

    def reextract(query: str, spans):
        asked.append(list(spans))
        return _plan(query, [{
            "id": "req-2", "type": "aggregate_predicate", "source_span": "총금액이 10만 원 이상",
            "scope": "cart", "metric": "cart_amount", "operator": ">=", "value": "10만 원",
        }]), []

    seed = _plan(COVERAGE_QUERY, [{
        "id": "req-1", "type": "aggregate_predicate", "source_span": "상품 종류가 2개 이상",
        "scope": "cart", "metric": "cart_line_count", "operator": "이상", "value": "2개",
    }])
    result = semantic_pipeline.run(
        COVERAGE_QUERY,
        extract=lambda _query: (seed, []),
        context=context,
        reextract=reextract,
    )
    assert asked, "미해결 요구사항이 있는데 패치 요청이 나가지 않았다"
    assert all("10만" in " ".join(spans) for spans in asked)
    assert len(result.plan.nodes) == 2
    assert result.coverage.is_complete
    assert result.ledger.is_complete()


def test_patch_outside_the_requested_span_is_rejected(context) -> None:
    """패치가 조용한 전체 재해석이 되지 않게 — 요청 구간 밖 노드는 병합하지 않는다."""
    query = "30대 여성이고, 장바구니 총금액이 10만 원 이상인 회원"

    def reextract(_query: str, spans):
        # 요청받은 구간(카트 절)이 아니라 다른 절('30대')의 노드를 낸 경우.
        return _plan(_query, [{
            "id": "req-9", "type": "predicate", "source_span": "30대",
            "subject": "member", "metric": "age", "operator": ">=", "value": 30,
        }]), []

    result = semantic_pipeline.run(
        query,
        extract=lambda _query: (_plan(query, []), []),
        context=context,
        reextract=reextract,
        claimed_spans=[(0, len("30대 여성이고"))],
    )
    requested = [span for record in result.reemission["rounds"] for span in record["requested_spans"]]
    assert requested and all("장바구니" in span for span in requested)
    assert [node.id for node in result.plan.nodes] == []


def test_patch_never_regresses_an_already_compiled_requirement(context) -> None:
    """이미 통과한 요구사항이 재방출로 훼손되면 그 라운드를 통째로 되돌린다."""
    good = {
        "id": "req-1", "type": "aggregate_predicate", "source_span": "상품 종류가 2개 이상",
        "scope": "cart", "metric": "cart_line_count", "operator": "이상", "value": "2개",
    }

    def sabotage(_query: str, _spans):
        # 통과한 조건의 근거 구간을 '값 없는' 노드로 덮어쓰려는 패치.
        return _plan(COVERAGE_QUERY, [
            {**good, "value": None},
            {"id": "req-2", "type": "aggregate_predicate", "source_span": "총금액이 10만 원 이상",
             "scope": "cart", "metric": "cart_amount", "operator": "이상", "value": "10만 원"},
        ]), []

    result = semantic_pipeline.run(
        COVERAGE_QUERY,
        extract=lambda _query: (_plan(COVERAGE_QUERY, [good]), []),
        context=context,
        reextract=sabotage,
    )
    compiled = result.ledger.by_id("req-1")
    assert compiled is not None and compiled.is_resolved, "통과한 요구사항이 패치로 훼손됐다"


def test_reemission_round_count_is_a_policy_value(context) -> None:
    calls: list[int] = []

    def reextract(_query: str, _spans):
        calls.append(1)
        return _plan(COVERAGE_QUERY, []), []

    for rounds in (0, 1):
        calls.clear()
        semantic_pipeline.run(
            COVERAGE_QUERY,
            extract=lambda _query: (_plan(COVERAGE_QUERY, []), []),
            context=context,
            reextract=reextract,
            reemission_policy=semantic_reemission.ReemissionPolicy(max_rounds=rounds),
        )
        assert len(calls) == rounds, f"정책 {rounds} 라운드인데 {len(calls)}회 호출됐다"


# ── 충돌 처리 ────────────────────────────────────────────────────────────────────
def test_same_span_with_different_meaning_becomes_a_conflict() -> None:
    """같은 금액 표현이 cart amount 와 purchase amount 로 모두 해석되면 조용히 고르지 않는다."""
    span = "총금액 10만 원 이상 구매한"
    node_a = semantic_plan.node_from_dict({
        "id": "a", "type": "aggregate_predicate", "source_span": span,
        "source_start": 0, "source_end": len(span),
        "scope": "cart", "metric": "cart_amount", "operator": ">=", "value": "10만 원"})
    node_b = semantic_plan.node_from_dict({
        "id": "b", "type": "aggregate_predicate", "source_span": span,
        "source_start": 0, "source_end": len(span),
        "scope": "order", "metric": "purchase_amount", "operator": ">=", "value": "10만 원"})
    plan = semantic_candidates.resolve_candidates([
        semantic_candidates.RequirementCandidate(span, node_a, "llm", 0.9),
        semantic_candidates.RequirementCandidate(span, node_b, "legacy", 0.8),
    ], source_query=span)
    assert plan.nodes == []
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0]["status"] == semantic_plan.STATUS_AMBIGUOUS
    assert {candidate["metric"] for candidate in plan.conflicts[0]["candidates"]} == {
        "cart_amount", "purchase_amount"
    }
    assert plan.status() == semantic_plan.STATUS_AMBIGUOUS


def test_same_span_same_meaning_merges_without_conflict() -> None:
    span = "총금액 10만 원 이상"
    partial = semantic_plan.node_from_dict({
        "id": "a", "type": "aggregate_predicate", "source_span": span,
        "scope": "cart", "metric": "cart_amount", "operator": ">="})
    complete = semantic_plan.node_from_dict({
        "id": "b", "type": "aggregate_predicate", "source_span": span,
        "scope": "cart", "metric": "cart_amount", "operator": ">=", "value": "10만 원"})
    plan = semantic_candidates.resolve_candidates([
        semantic_candidates.RequirementCandidate(span, partial, "llm", 0.9),
        semantic_candidates.RequirementCandidate(span, complete, "llm", 0.5),
    ], source_query=span)
    assert len(plan.nodes) == 1 and not plan.conflicts
    assert plan.nodes[0].value == "10만 원"


# ── capability 판정 + 실패 분류 ──────────────────────────────────────────────────
def test_capability_verdict_reports_every_axis() -> None:
    registry = semantic_capability.registry()
    node = semantic_plan.node_from_dict({
        "id": "req-1", "type": "metric_comparison", "source_span": "x",
        "metric": "purchase_amount",
        "baseline": {"from": "20260201", "to": "20260228"},
        "current": {"from": "20260301", "to": "20260331"},
        "relation": "increase"})
    verdict = registry.judge(node).to_dict()
    assert verdict["semantic_supported"] is True
    assert verdict["compiler_supported"] is True
    assert verdict["executor_supported"] is True
    assert verdict["required_grain"] == "order_transaction"
    assert verdict["available_grain"] == "order_transaction"
    assert set(verdict["coverage"]) == {"from", "to", "complete"}
    assert verdict["executable"] is True


def test_failure_taxonomy_distinguishes_causes() -> None:
    """필수 값 누락 / 의미 모호 / 연산 미지원 / 데이터 grain 부족 / 데이터 없음 /
    검증 불일치 / 내부 오류가 서로 다른 코드로 나온다."""
    registry = semantic_capability.registry()

    # ① 필수 값 누락 — 노드 스키마에서 계산된다.
    incomplete = _plan("x", [{"id": "req-1", "type": "ranked_set", "source_span": "x",
                              "entity": "member", "metric": "total_buy_amt"}])
    assert incomplete.missing_fields() == ("req-1.direction", "req-1.limit")
    assert incomplete.status() == semantic_plan.STATUS_NEEDS_CLARIFICATION

    # ② 의미 모호
    ambiguous = SemanticPlanV2(conflicts=[{"status": "ambiguous", "source_span": "x"}])
    assert ambiguous.status() == semantic_plan.STATUS_AMBIGUOUS

    # ③ 연산 미지원
    brand_ranking = semantic_plan.node_from_dict({
        "id": "req-1", "type": "ranked_set", "source_span": "x", "entity": "brand",
        "metric": "purchase_amount", "direction": "descending",
        "limit": {"type": "count", "value": 5}})
    assert registry.judge(brand_ranking).failure_code == semantic_plan.UNSUPPORTED_SEMANTICS

    # ④⑤ 적재 부족은 **원인을 그대로 이름 대되 차단하지는 않는다**(data_availability_policy).
    # 적재가 얕으면 같은 SQL 이 0건을 낼 뿐 의미는 그대로다 — 0건은 정직한 답이다.
    multi_month = semantic_plan.node_from_dict({
        "id": "req-1", "type": "relation_predicate", "source_span": "x", "subject": "member",
        "attribute": "member_grade", "relation": "change_count"})
    grain = registry.judge(multi_month, available_months={"monthly_attribute_snapshot": 1})
    assert grain.failure_code is None and grain.executable
    assert [item["code"] for item in grain.advisories] == [semantic_plan.UNSUPPORTED_DATA_GRAIN]

    out_of_range = semantic_plan.node_from_dict({
        "id": "req-1", "type": "relation_predicate", "source_span": "x", "subject": "member",
        "attribute": "member_grade", "relation": "as_of",
        "period": {"from": "20991201", "to": "20991231"}})
    ranged = registry.judge(out_of_range)
    assert ranged.failure_code is None and ranged.executable
    assert [item["code"] for item in ranged.advisories] == [semantic_plan.DATA_UNAVAILABLE]
    # 축은 사실 그대로 보고한다 — 고지로 바뀌어도 '적재 안에 있다'고 말하지 않는다.
    assert ranged.data_covered is False
    assert ranged.axes()["data_coverage"]["covered"] is False

    # ⑥ 검증 불일치(값 정규화 실패)
    bad_value = _plan("x", [{"id": "req-1", "type": "predicate", "source_span": "x",
                             "subject": "member", "metric": "buy_cycle",
                             "operator": ">=", "value": "숫자 아님"}])
    semantic_pipeline.normalize_plan(bad_value)
    assert bad_value.validation_errors[0]["failure_code"] == semantic_plan.VALIDATION_MISMATCH

    # ⑦ 내부 오류
    broken = SemanticPlanV2()
    broken.validation_errors.append({"failure_code": semantic_plan.INTERNAL_FAULT, "reason": "boom"})
    assert broken.internal_failures()
    assert broken.status() == semantic_plan.STATUS_INVALID

    # 실패 코드는 전부 닫힌 집합 안에 있다.
    for code in (semantic_plan.MISSING_ARGUMENT, semantic_plan.AMBIGUOUS_REQUIREMENT,
                 semantic_plan.UNSUPPORTED_SEMANTICS, semantic_plan.UNSUPPORTED_DATA_GRAIN,
                 semantic_plan.DATA_UNAVAILABLE, semantic_plan.VALIDATION_MISMATCH,
                 semantic_plan.EXECUTION_FAILURE, semantic_plan.INTERNAL_FAULT):
        assert code in semantic_plan.FAILURE_CODES


def test_data_availability_policy_can_block_again() -> None:
    """`advise` 는 정책이지 능력 선언이 아니다 — `block` 으로 되돌리면 그대로 차단한다.

    되돌릴 수 없는 완화는 완화가 아니라 삭제다. 이 계약이 있어야 "SQL 은 내보내되 0건임을
    고지한다"가 **선택**으로 남는다.
    """
    payload = json.loads(
        semantic_capability.DEFAULT_CAPABILITY_PATH.read_text(encoding="utf-8")
    )
    node = semantic_plan.node_from_dict({
        "id": "req-1", "type": "relation_predicate", "source_span": "x", "subject": "member",
        "attribute": "member_grade", "relation": "as_of",
        "period": {"from": "20991201", "to": "20991231"}})

    blocking = semantic_capability.CapabilityRegistry({**payload, "data_availability_policy": "block"})
    verdict = blocking.judge(node)
    assert verdict.failure_code == semantic_plan.DATA_UNAVAILABLE
    assert not verdict.executable and not verdict.advisories

    advising = semantic_capability.CapabilityRegistry({**payload, "data_availability_policy": "advise"})
    assert advising.judge(node).executable

    with pytest.raises(semantic_capability.CapabilityRegistryError):
        semantic_capability.CapabilityRegistry({**payload, "data_availability_policy": "maybe"})


def test_internal_failures_are_never_shown_as_unsupported() -> None:
    plan = SemanticPlanV2()
    plan.validation_errors.append({"failure_code": semantic_plan.INTERNAL_FAULT, "reason": "boom"})
    plan.capability_verdicts.append({"failure_code": semantic_plan.EXECUTION_FAILURE, "message": "x"})
    projected = semantic_pipeline.project_semantic_ir(plan)
    assert projected["status"] != "unsupported"
    assert projected["unsupported_operations"] == []
    assert plan.unsupported_operations() == ()


def test_capability_registry_requires_a_declaration_per_node_type() -> None:
    """노드를 추가하면 capability 선언도 함께 — 선언 없는 노드의 조용한 통과를 막는다."""
    payload = {"node_types": {"predicate": {"semantic_supported": True}}}
    with pytest.raises(semantic_capability.CapabilityRegistryError):
        semantic_capability.CapabilityRegistry(payload)


# ── 가짜 성공 차단 ───────────────────────────────────────────────────────────────
def test_uncovered_requirement_never_yields_resolved(context) -> None:
    """원문 requirement 가 AST 에 없으면 최종 status 가 resolved 가 되면 안 된다."""
    plan: dict = {"target_user": {}, "semantic_plan": {"nodes": []}}
    semantic_plan_bridge.apply(plan, COVERAGE_QUERY, context=context)
    assert plan["semantic_ir"]["status"] == "needs_clarification"
    assert plan["semantic_ir"]["missing_fields"]
    assert plan["target_user"] == {}


def test_incomplete_node_never_yields_a_half_filled_slot(context) -> None:
    plan: dict = {
        "target_user": {},
        "semantic_plan": {"nodes": [{
            "id": "req-1", "type": "aggregate_predicate",
            "source_span": "총금액이 10만 원 이상", "scope": "cart", "metric": "cart_amount",
        }]},
    }
    semantic_plan_bridge.apply(plan, COVERAGE_QUERY, context=context)
    assert "cart_aggregate" not in plan["target_user"]
    assert plan["semantic_ir"]["status"] == "needs_clarification"


def test_complete_plan_compiles_and_resolves(context) -> None:
    plan: dict = {
        "target_user": {},
        "semantic_plan": {"nodes": [
            {"id": "req-1", "type": "aggregate_predicate", "source_span": "상품 종류가 2개 이상",
             "scope": "cart", "metric": "cart_line_count", "operator": "이상", "value": "2개"},
            {"id": "req-2", "type": "aggregate_predicate", "source_span": "총금액이 10만 원 이상",
             "scope": "cart", "metric": "cart_amount", "operator": "이상", "value": "10만 원"},
        ]},
    }
    semantic_plan_bridge.apply(plan, COVERAGE_QUERY, context=context)
    assert plan["semantic_ir"]["status"] == "resolved"
    assert len(plan["target_user"]["cart_aggregate"]) == 2
    assert plan[semantic_plan_bridge.PIPELINE_KEY]["written_slots"] == ["target_user.cart_aggregate"]


def test_pipeline_internal_error_is_recorded_not_swallowed() -> None:
    """배선 사고는 조용한 성공도 '미지원'도 아니다 — 내부 오류로 기록된다."""
    def explode(_query: str):
        raise RuntimeError("boom")

    result = semantic_pipeline.run(
        "x", extract=explode, context=semantic_pipeline.CompileContext()
    )
    assert result.status == semantic_plan.STATUS_INVALID
    assert result.plan.validation_errors[0]["failure_code"] == semantic_plan.INTERNAL_FAULT
    assert result.plan.unsupported_operations() == ()
