"""CompilerRegistry 계약 — register/get/has, 슬롯 방출이 SLOT_SHAPES coerce 와 동일 검증을 통과.

핵심 불변식: 이 레지스트리는 SQL 문자열을 만들지 않는다. 방출(SlotEmission) 값은 전부
targeting_ir.SLOT_SHAPES coerce(LLM 경로와 같은 닫힌 어휘 검증)를 통과한 것이어야 한다."""

from __future__ import annotations

import pytest

import member_filters_config
import metric_registry
from compiler_registry import (
    CompileContext,
    CompilerError,
    CompilerRegistry,
    ConditionCompiler,
    SlotEmission,
    default_registry,
)
from targeting_ir import SLOT_SHAPES


def _normalized(concept: str, normalized: dict) -> dict:
    return {"concept": concept, "slot": concept, "kind": "special_slot",
            "normalized": normalized, "status": "normalized"}


def test_default_registry_is_not_empty_and_covers_expected_names() -> None:
    registry = default_registry()
    assert registry.names(), "기본 레지스트리가 비었다 — 검사가 공허하다."
    expected = {
        "purchase_inactivity", "inactivity_period", "recent_login", "signup_window",
        "cart_retention", "purchase_aggregate", "campaign_purchase_aggregate", "member_profile_metric",
    }
    assert expected <= registry.names()


def test_register_get_has_contract() -> None:
    registry = CompilerRegistry()
    assert not registry.has("anything")
    with pytest.raises(CompilerError):
        registry.get("anything")
    compiler = ConditionCompiler(
        name="demo", supports_ids=frozenset({"demo"}),
        emit=lambda condition, context: SlotEmission("target_user", "purchase_inactivity",
                                                     {"value": 1, "unit": "days", "min_days": 1}))
    registry.register("demo", compiler)
    assert registry.has("demo") and registry.get("demo") is compiler
    with pytest.raises(CompilerError):
        registry.register("demo", compiler)  # 조용한 덮어쓰기 금지


def test_llm_invented_compiler_name_is_not_registered() -> None:
    assert not default_registry().has("llm_generated_compiler")


def test_duration_emission_equals_slot_shape_coerce_output() -> None:
    """컴파일러 방출 값 == SLOT_SHAPES coerce 출력 — 새 계층이 별도 검증 표면을 만들지 않는다."""
    registry = default_registry()
    normalized = {"value": 3, "unit": "months", "min_days": 90}
    emission = registry.get("purchase_inactivity").compile(
        _normalized("purchase_inactivity", normalized), CompileContext())
    assert emission.container == "target_user" and emission.slot == "purchase_inactivity"
    assert emission.value == SLOT_SHAPES["purchase_inactivity"].coerce(dict(normalized))

    login = registry.get("recent_login").compile(
        {**_normalized("recent_login", normalized)}, CompileContext())
    assert login.value == SLOT_SHAPES["recent_login"].coerce(dict(normalized))
    assert login.value["sql_interval"] == "3 months"


def test_compile_refuses_non_normalized_status() -> None:
    registry = default_registry()
    for status in ("candidate", "needs_review", "unresolved", "invalid", "unsupported"):
        with pytest.raises(CompilerError):
            registry.get("purchase_inactivity").compile(
                {"concept": "purchase_inactivity", "normalized": {"value": 1, "unit": "days"},
                 "status": status}, CompileContext())


def test_aggregate_emission_uses_config_vocabulary() -> None:
    context = CompileContext(allowed={"aggregate_metrics": set(member_filters_config.aggregate_metrics())})
    condition = {"concept": "purchase_amount", "kind": "generic_condition", "status": "normalized",
                 "normalized": {"concept": "purchase_amount", "operator": ">=", "value": 100000,
                                "window_days": 30}}
    emission = default_registry().get("purchase_aggregate").compile(condition, context)
    assert emission.mode == "extend" and emission.slot == "aggregate_conditions"
    assert emission.value == [{"metric_id": "purchase_amount", "operator": ">=",
                               "threshold": 100000, "window_days": 30}]


def test_aggregate_emission_rejects_unregistered_metric_when_vocab_present() -> None:
    context = CompileContext(allowed={"aggregate_metrics": set(member_filters_config.aggregate_metrics())})
    condition = {"concept": "fabricated_metric", "status": "normalized",
                 "normalized": {"concept": "fabricated_metric", "operator": ">=", "value": 1}}
    with pytest.raises(CompilerError):
        default_registry().get("purchase_aggregate").compile(condition, context)


def test_profile_metric_emission_binds_physical_column_from_registry() -> None:
    """물리 컬럼은 metric_registry 매핑이 채운다 — LLM/조건이 컬럼명을 지정할 자리가 없다."""
    vocab, _ = metric_registry.profile_slot_vocab()
    if "buy_cycle" not in vocab:
        pytest.skip("buy_cycle 지표 스펙이 없다")
    context = CompileContext(allowed={"profile_metrics": vocab})
    condition = {"concept": "buy_cycle", "status": "normalized",
                 "normalized": {"concept": "buy_cycle", "operator": "<=", "value": 30}}
    emission = default_registry().get("member_profile_metric").compile(condition, context)
    (entry,) = emission.value
    assert entry["metric_id"] == "buy_cycle"
    assert entry["column"] == vocab["buy_cycle"]["column"]  # 설정 소유 물리 바인딩


def test_emission_mode_vocabulary_is_closed() -> None:
    with pytest.raises(CompilerError):
        SlotEmission("target_user", "purchase_inactivity", {}, mode="merge")
