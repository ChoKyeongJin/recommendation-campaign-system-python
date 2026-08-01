"""semantic_ir 게이트의 결정론 소유권 sweep — capability 소유 missing_fields 가 게이트를 막지 않음을 고정.

배경(2026-08-01 '같은 상품 동시 구매 고객수' 실사고): 전용 동시구매 capability(condition_evaluations
백필)가 존재하고 감지 정규식도 맞았는데, LLM 이 '같은 상품'을 미확정 purchase_object 결핍으로
보고하자 semantic_ir 게이트가 백필보다 먼저 실행돼 영원히 도달 불가였다.

여기서 강제하는 계약:
  1. 검증 통과한 condition_evaluations 가 있으면 상품/기간 계열 missing_fields 는 걷힌다
     (상품 축은 관계 조건이라 상품명 불요, 기간 축은 선택 입력 — 부재=제한 없음).
  2. 전부 걷히고 unsupported_operations 도 없으면 status 는 resolved 로 되돌아간다 —
     needs_clarification + 빈 missing_fields 는 닫힌 스키마 위반(internal_invalid)이 된다.
  3. 소유 밖 필드는 절대 걷지 않는다(fail-close) — 진짜 결핍은 계속 게이트가 막는다.
  4. unsupported 상태는 sweep 대상이 아니다.
  5. resolved + operations 없음 + condition_evaluations 만 있는 플랜은 유효하다
     (_has_plan_meaning 이 condition_evaluations 를 인정).
  6. graph_rag 게이트는 sweep 뒤에 통과한다(백필→sweep→게이트 순서의 배선 가드).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import condition_evaluation_ir  # noqa: E402
from query_structurer.semantic_ir import validate_semantic_ir  # noqa: E402

_QUERY = "2026년 3월 같은 상품을 동시 구매한 고객수"


def _needs_clarification(missing: list[str]) -> dict:
    return {
        "status": "needs_clarification",
        "operations": [],
        "missing_fields": list(missing),
        "policy_applications": [],
        "unsupported_operations": [],
        "message": None,
    }


def _plan_with_backfill(missing: list[str]) -> dict:
    plan = {
        "target_user": {"purchase_date": {"from": "20260301", "to": "20260331"}},
        "semantic_ir": _needs_clarification(missing),
    }
    condition_evaluation_ir.apply_same_product_co_purchase_backfill(plan, _QUERY)
    assert plan.get(condition_evaluation_ir.PLAN_KEY), "백필 전제가 깨졌다 — 감지/스팬 규칙을 확인하라."
    return plan


def test_owned_fields_are_swept_and_status_resolves() -> None:
    plan = _plan_with_backfill(["purchase_object"])
    dropped = condition_evaluation_ir.drop_capability_owned_missing_fields(plan, _QUERY)
    assert dropped == ["purchase_object"]
    assert plan["semantic_ir"]["missing_fields"] == []
    assert plan["semantic_ir"]["status"] == "resolved"


def test_purchase_period_family_is_owned() -> None:
    """판정 창은 선택 입력이다 — 창 부재를 사용자 결핍으로 되묻지 않는다."""
    plan = _plan_with_backfill(["purchase_date", "target_user.purchase_period"])
    dropped = condition_evaluation_ir.drop_capability_owned_missing_fields(plan, _QUERY)
    assert set(dropped) == {"purchase_date", "target_user.purchase_period"}
    assert plan["semantic_ir"]["status"] == "resolved"


def test_unowned_fields_survive_and_keep_clarification() -> None:
    """브랜드 등 소유 밖 결핍은 계속 게이트가 막아야 한다(fail-close)."""
    plan = _plan_with_backfill(["purchase_object", "brand"])
    dropped = condition_evaluation_ir.drop_capability_owned_missing_fields(plan, _QUERY)
    assert dropped == ["purchase_object"]
    assert plan["semantic_ir"]["missing_fields"] == ["brand"]
    assert plan["semantic_ir"]["status"] == "needs_clarification"


def test_sweep_requires_valid_condition_evaluations() -> None:
    """IR 이 없거나 검증 실패면 소유 주장 금지 — 불완전 IR 이 결핍을 삼키면 안 된다."""
    no_ir = {"semantic_ir": _needs_clarification(["purchase_object"])}
    assert condition_evaluation_ir.drop_capability_owned_missing_fields(no_ir, _QUERY) == []
    broken = {
        "semantic_ir": _needs_clarification(["purchase_object"]),
        condition_evaluation_ir.PLAN_KEY: [{"id": "broken"}],
    }
    assert condition_evaluation_ir.drop_capability_owned_missing_fields(broken, _QUERY) == []
    assert broken["semantic_ir"]["status"] == "needs_clarification"


def test_unsupported_status_is_untouched() -> None:
    plan = _plan_with_backfill([])
    plan["semantic_ir"] = {
        "status": "unsupported",
        "operations": [],
        "missing_fields": [],
        "policy_applications": [],
        "unsupported_operations": [{"kind": "x", "reason": "y", "evidence": "z"}],
        "message": None,
    }
    assert condition_evaluation_ir.drop_capability_owned_missing_fields(plan, _QUERY) == []
    assert plan["semantic_ir"]["status"] == "unsupported"


def test_resolved_with_only_condition_evaluations_is_valid_semantic_ir() -> None:
    """sweep 이 status 를 resolved 로 되돌린 플랜이 닫힌 스키마 검증을 통과해야 한다."""
    plan = _plan_with_backfill(["purchase_object"])
    plan["target_user"] = {}
    condition_evaluation_ir.drop_capability_owned_missing_fields(plan, _QUERY)
    validated = validate_semantic_ir(plan["semantic_ir"], [], payload=plan)
    assert validated["status"] == "resolved"


def test_resolved_without_any_plan_meaning_still_fails() -> None:
    """condition_evaluations 인정이 '빈 플랜 resolved'까지 허용하면 안 된다."""
    ir = _needs_clarification([])
    ir["status"] = "resolved"
    with pytest.raises(ValueError):
        validate_semantic_ir(ir, [], payload={"target_user": {}})


def test_scalar_count_ir_declares_analytical_output_contract() -> None:
    """'고객수' IR 의 출력 계약은 capability 가 소유한다 — 기본 expected_grain='member' 가
    정당한 COUNT 결과를 grain 불일치로 차단하지 않게."""
    plan = _plan_with_backfill([])
    contract = condition_evaluation_ir.scalar_count_output_contract(plan)
    assert contract == {
        "expected_grain": "analytical",
        "requires_member_id": False,
        "source": "condition_evaluations",
    }
    assert condition_evaluation_ir.scalar_count_output_contract({"target_user": {}}) is None
    broken = {condition_evaluation_ir.PLAN_KEY: [{"id": "broken"}]}
    assert condition_evaluation_ir.scalar_count_output_contract(broken) is None


def test_gate_passes_after_backfill_and_sweep() -> None:
    """배선 가드: 백필→sweep 뒤 게이트는 차단하지 않는다(순서가 뒤집히면 도달 불가 재발)."""
    import graph_rag

    plan = _plan_with_backfill(["purchase_object"])
    condition_evaluation_ir.drop_capability_owned_missing_fields(plan, _QUERY)
    assert graph_rag._semantic_ir_blocking_sql_result(plan) is None

    blocked = {"semantic_ir": _needs_clarification(["brand"])}
    result = graph_rag._semantic_ir_blocking_sql_result(blocked)
    assert result is not None and result["failure_reason"] == "semantic_ir_needs_clarification"


# ── 귀속 가드(적대 리뷰에서 확정된 오걷어냄 시나리오의 회귀 고정) ─────────────────────────


def test_other_clause_purchase_verb_blocks_sweep_entirely() -> None:
    """동시구매 어구 밖에 다른 구매 절('지난 시즌에 구매한')이 있으면 그 절의 결핍을 삼키면 안 된다 —
    걷지 않고 게이트가 확인 질문으로 막는 것이 정답이다(조용한 lifetime 오답 방지)."""
    query = "같은 상품을 동시 구매했고 지난 시즌에 구매한 고객수"
    plan = {"semantic_ir": _needs_clarification(["purchase_period"])}
    condition_evaluation_ir.apply_same_product_co_purchase_backfill(plan, query)
    assert plan.get(condition_evaluation_ir.PLAN_KEY), "백필 전제가 깨졌다."
    assert condition_evaluation_ir.drop_capability_owned_missing_fields(plan, query) == []
    assert plan["semantic_ir"]["status"] == "needs_clarification"
    assert plan["semantic_ir"]["missing_fields"] == ["purchase_period"]


def test_entity_marker_outside_blocks_sweep_entirely() -> None:
    """어구 밖 엔터티 표지('브랜드' 등)도 다른 절 소유 신호다 — sweep 전체를 포기한다."""
    query = "브랜드 A 와 같은 상품을 동시 구매한 고객수"
    plan = {"semantic_ir": _needs_clarification(["purchase_object"])}
    condition_evaluation_ir.apply_same_product_co_purchase_backfill(plan, query)
    if not plan.get(condition_evaluation_ir.PLAN_KEY):
        plan[condition_evaluation_ir.PLAN_KEY] = [
            condition_evaluation_ir.build_same_product_co_purchase_evaluation(query, None)
        ]
    assert condition_evaluation_ir.drop_capability_owned_missing_fields(plan, query) == []
    assert plan["semantic_ir"]["status"] == "needs_clarification"


def test_unparsed_period_is_not_owned_without_time_range() -> None:
    """IR 이 판정창을 물려받지 못했으면(time_range=None) 기간 결핍은 진짜 결핍일 수 있다 —
    상품 축만 걷고 기간 축은 남겨 게이트가 묻게 한다('기간 언급됐으나 미파싱' 케이스)."""
    plan = {"semantic_ir": _needs_clarification(["purchase_object", "purchase_period"])}
    condition_evaluation_ir.apply_same_product_co_purchase_backfill(plan, _QUERY)
    assert plan.get(condition_evaluation_ir.PLAN_KEY)
    dropped = condition_evaluation_ir.drop_capability_owned_missing_fields(plan, _QUERY)
    assert dropped == ["purchase_object"]
    assert plan["semantic_ir"]["missing_fields"] == ["purchase_period"]
    assert plan["semantic_ir"]["status"] == "needs_clarification"


def test_llm_semantic_ir_unresolved_channel_is_swept_with_same_attribution() -> None:
    """같은 결핍 신호의 두 번째 채널(unresolved_source_conditions, source=llm_semantic_ir)도
    같은 귀속 규칙으로 걷는다 — 안 걷으면 게이트 하나를 열어도 다음 채널이 도달을 막는다."""
    plan = _plan_with_backfill(["purchase_object"])
    plan["unresolved_source_conditions"] = [
        {"path": "semantic_ir.purchase_object", "condition": "같은 상품을 동시 구매한 고객수",
         "reason": "확정 실패", "source": "llm_semantic_ir"},
        {"path": "source_coverage.unresolved[0]", "condition": "브랜드 조건",
         "reason": "확정 실패", "source": "source_semantic_contract"},
    ]
    condition_evaluation_ir.drop_capability_owned_missing_fields(plan, _QUERY)
    remaining = plan["unresolved_source_conditions"]
    assert len(remaining) == 1 and remaining[0]["source"] == "source_semantic_contract"


def test_sweep_records_claim_decisions() -> None:
    """걷어낸 결핍은 CLAIM 으로 감사 기록된다 — 사라진 확인 질문을 사후 추적할 수 있어야 한다."""
    plan = _plan_with_backfill(["purchase_object"])
    condition_evaluation_ir.drop_capability_owned_missing_fields(plan, _QUERY)
    decisions = plan.get("decisions") or []
    assert any(
        entry.get("action") == "claim"
        and str(entry.get("filter", "")).startswith("condition_evaluations:")
        and entry.get("value") == "purchase_object"
        for entry in decisions
    ), f"CLAIM 감사 기록이 없다: {decisions}"


def test_build_sql_result_source_wires_backfill_and_sweep_before_gate() -> None:
    """실배선 가드(소스 순서): build_sql_result 안에서 백필→sweep 호출이 semantic_ir 게이트 호출보다
    앞에 있어야 한다 — 이 순서가 뒤집힌 것이 2026-08-01 도달 불가 사고의 원인이다."""
    import graph_rag

    source = Path(graph_rag.__file__).read_text(encoding="utf-8")
    backfill = source.index("apply_same_product_co_purchase_backfill(query_plan, original_query or query)")
    sweep = source.index("drop_capability_owned_missing_fields(query_plan, original_query or query)")
    gate = source.index("_semantic_ir_blocking_sql_result(query_plan)")
    assert backfill < gate and sweep < gate, (
        "백필/sweep 호출이 semantic_ir 게이트 호출보다 뒤에 있다 — 도달 불가 사고 재발."
    )
