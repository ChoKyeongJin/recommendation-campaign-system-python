"""실행 게이트 보안·안전 — 검증되지 않은 값으로 SQL 을 만들 수 없다(요청 17·21번).

게이트가 막아야 하는 것: 비정규화 상태 5종, 카탈로그 미접지, 컴파일 미지원, 임의 컴파일러
이름, span 없는 조건, 슬롯 충돌. 통과한 것의 성질: plan 조각이 SLOT_SHAPES 검증 값만 담는다."""

from __future__ import annotations

import pytest

import member_filters_config
from compiler_registry import CompileContext
from execution_gate import (
    ExecutableCondition,
    ExecutionGateError,
    build_validated_plan_fragment,
    ensure_executable,
)
from normalization_orchestrator import RawInterpretation, SourceRef, resolve_condition
from targeting_ir import SLOT_SHAPES


def _ref(text: str, matched: str) -> SourceRef:
    start = text.index(matched)
    return SourceRef(text=text, matched_text=matched, start=start, end=start + len(matched))


def _resolved_purchase_inactivity() -> dict:
    return resolve_condition(RawInterpretation(
        kind="special_slot", concept_query="미구매",
        candidate={"value": 3, "unit": "weeks"},
        source=_ref("3주 동안 구매 안 한 고객", "3주"), confidence=0.96))


def _resolved_purchase_amount() -> dict:
    return resolve_condition(RawInterpretation(
        kind="generic_condition", concept_query="결제한 돈",
        candidate={"operator": "이상", "value": 100000, "window": {"value": 30, "unit": "days"}},
        source=_ref("최근 30일 동안 결제한 돈이 10만원 이상인 고객", "결제한 돈이 10만원 이상"),
        confidence=0.95))


def _executable_payload(**overrides) -> dict:
    base = _resolved_purchase_inactivity()
    base.update(overrides)
    return base


# ── 상태별 차단 ───────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["candidate", "needs_review", "unresolved", "invalid", "unsupported"])
def test_every_non_normalized_status_is_blocked(status: str) -> None:
    with pytest.raises(ExecutionGateError):
        ensure_executable(_executable_payload(status=status))


def test_unresolved_concept_from_orchestrator_is_blocked() -> None:
    resolved = resolve_condition(RawInterpretation(
        kind="generic_condition", concept_query="고객 생애가치 상승 가능성",
        candidate={"operator": ">", "value": 0.7},
        source=_ref("고객 생애가치가 앞으로 상승할 가능성이 높은 고객", "생애가치"),
        confidence=0.92))
    assert resolved["status"] == "unresolved"
    with pytest.raises(ExecutionGateError):
        ensure_executable(resolved)


def test_catalog_and_execution_axes_are_enforced_separately() -> None:
    with pytest.raises(ExecutionGateError):
        ensure_executable(_executable_payload(catalog_match=False))
    with pytest.raises(ExecutionGateError):
        ensure_executable(_executable_payload(execution_support=False))
    with pytest.raises(ExecutionGateError):
        ensure_executable(_executable_payload(sql_generation_allowed=False))


def test_condition_without_compiler_or_span_is_blocked() -> None:
    with pytest.raises(ExecutionGateError):
        ensure_executable(_executable_payload(compiler=None))
    with pytest.raises(ExecutionGateError):
        ensure_executable(_executable_payload(source={}))


def test_direct_construction_cannot_bypass_the_gate() -> None:
    """ExecutableCondition 을 손으로 만들어도 __post_init__ 이 같은 검증을 돌린다."""
    with pytest.raises(ExecutionGateError):
        ExecutableCondition(
            concept="purchase_inactivity", kind="special_slot", compiler="purchase_inactivity",
            normalized={"value": 3, "unit": "months", "min_days": 90}, status="unresolved",
            catalog_match=True, execution_support=True,
            source={"span": {"start": 0, "end": 3}})


# ── 임의 컴파일러/개념 차단 ────────────────────────────────────────────────────────────────
def test_fabricated_compiler_name_cannot_reach_plan() -> None:
    payload = _executable_payload(compiler="llm_generated_compiler")
    with pytest.raises(ExecutionGateError):
        build_validated_plan_fragment([payload])


def test_sql_injection_in_numeric_field_never_reaches_the_gate() -> None:
    resolved = resolve_condition(RawInterpretation(
        kind="generic_condition", concept_query="구매 금액",
        candidate={"operator": ">=", "value": "100000; DROP TABLE users"},
        source=_ref("구매 금액 조건", "구매 금액"), confidence=0.95))
    assert resolved["status"] == "invalid"
    with pytest.raises(ExecutionGateError):
        ensure_executable(resolved)


# ── 통과 경로 성질 ────────────────────────────────────────────────────────────────────────
def _context() -> CompileContext:
    return CompileContext(allowed={"aggregate_metrics": set(member_filters_config.aggregate_metrics())})


def test_validated_fragment_contains_only_slot_shape_validated_values() -> None:
    model = build_validated_plan_fragment(
        [_resolved_purchase_inactivity(), _resolved_purchase_amount()], context=_context())
    target_user = model.plan_fragment["target_user"]
    assert set(target_user) <= set(SLOT_SHAPES), "SLOT_SHAPES 에 없는 슬롯이 plan 조각에 들어갔다"
    assert target_user["purchase_inactivity"] == SLOT_SHAPES["purchase_inactivity"].coerce(
        {"value": 3, "unit": "weeks"})
    assert target_user["aggregate_conditions"] == [
        {"metric_id": "purchase_amount", "operator": ">=", "threshold": 100000, "window_days": 30}]


def test_calendar_duration_without_an_execution_window_is_blocked() -> None:
    unresolved_calendar = resolve_condition(RawInterpretation(
        kind="special_slot",
        concept_query="미구매",
        candidate={"value": 3, "unit": "months"},
        source=_ref("석 달 동안 구매 안 한 고객", "석 달"),
        confidence=0.96,
    ))

    with pytest.raises(ExecutionGateError):
        build_validated_plan_fragment([unresolved_calendar], context=_context())


def test_one_bad_condition_fails_the_whole_fragment() -> None:
    """일부만 조용히 빠진 plan 은 '조건이 사라진 SQL' — 전체 실패가 맞다(fail-close)."""
    bad = _executable_payload(status="unresolved")
    with pytest.raises(ExecutionGateError):
        build_validated_plan_fragment([_resolved_purchase_amount(), bad], context=_context())


def test_two_conditions_setting_the_same_slot_conflict() -> None:
    with pytest.raises(ExecutionGateError):
        build_validated_plan_fragment(
            [_resolved_purchase_inactivity(), _resolved_purchase_inactivity()], context=_context())


def test_extend_slots_accumulate_instead_of_conflicting() -> None:
    first = _resolved_purchase_amount()
    second = resolve_condition(RawInterpretation(
        kind="generic_condition", concept_query="구매 횟수",
        candidate={"operator": "이상", "value": 3},
        source=_ref("구매 횟수 3회 이상", "구매 횟수 3회 이상"), confidence=0.95))
    assert second["status"] == "normalized", second["errors"]
    model = build_validated_plan_fragment([first, second], context=_context())
    assert len(model.plan_fragment["target_user"]["aggregate_conditions"]) == 2
