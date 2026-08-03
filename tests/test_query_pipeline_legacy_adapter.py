"""호환 어댑터 — 저장된 ``event_expression`` 을 한 방향으로만 잇는다."""

from __future__ import annotations

import warnings

import pytest
from query_pipeline_fixtures import clock, id_factory

import graph_rag
import query_pipeline
from query_pipeline.compatibility.legacy_event_expression import (
    LegacyAdapterError,
    LegacyConversionFailed,
    LegacyEventExpressionAdapter,
    LegacyEventExpressionPayload,
    LegacySpecConversion,
    to_legacy_unsupported,
)

WIRE = {
    "type": "exists",
    "relation": {
        "type": "filter",
        "relation": {"type": "source", "name": "purchase"},
        "where": {
            "type": "time_filter",
            "field": {"type": "field", "name": "purchase.occurred_at"},
            "window": {
                "type": "interval",
                "start": "2026-07-01",
                "end_exclusive": "2026-08-01",
            },
        },
    },
    "evidence": {"text": "지난달 구매", "start": 0, "end": 6},
}
PAYLOAD = {"expression": WIRE, "source": "audience_requirement", "receipts": []}


def test_adapter_warns_that_the_slot_is_deprecated() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        LegacyEventExpressionAdapter.to_event_query_spec(
            PAYLOAD, clock=clock(), id_factory=id_factory()
        )
    assert any(item.category is DeprecationWarning for item in caught)


def test_adapter_produces_a_validated_spec() -> None:
    spec = query_pipeline.audience_spec_from_legacy_payload(
        PAYLOAD, clock=clock(), id_factory=id_factory()
    )
    assert spec.source.requirement_id == "legacy-event-expression"
    assert spec.bindings["purchase.occurred_at"].entity == "purchase"
    assert "node.exists" in spec.capabilities.required
    # 어디서 온 표현인지가 영수증에 남는다.
    assert "audience_requirement" in spec.receipts[0].reason


def test_adapter_tolerates_extra_keys_from_migration_tooling() -> None:
    payload = LegacyEventExpressionPayload.model_validate(
        {**PAYLOAD, "binding_fingerprint": "abc", "compiler_version": "1.0.0"}
    )
    assert payload.is_canonical


def test_adapter_reports_structured_failure_instead_of_raising() -> None:
    result = LegacyEventExpressionAdapter.convert({"expression": {"type": "nope"}})
    assert isinstance(result, LegacyConversionFailed)
    assert result.stage == "legacy_event_expression_adapter"

    ok = LegacyEventExpressionAdapter.convert(
        PAYLOAD, clock=clock(), id_factory=id_factory()
    )
    assert isinstance(ok, LegacySpecConversion)


def test_adapter_refuses_a_payload_without_expression() -> None:
    with pytest.raises(LegacyAdapterError, match="표현"):
        LegacyEventExpressionAdapter.to_event_query_spec(
            {"source": "audience_requirement"}, warn=False
        )


def test_legacy_unsupported_shape_is_preserved() -> None:
    payload = to_legacy_unsupported("event_compiler_unsupported", "설명")
    assert set(payload) == {"reason", "message", "clarification"}


def test_graph_rag_builder_routes_through_the_new_layers() -> None:
    """실제 빌더가 새 파이프라인을 통과한다(죽은 코드가 아니라는 근거)."""
    calls: list[str] = []
    original = query_pipeline.compile_audience_predicate

    def spy(payload, **kwargs):
        calls.append("called")
        return original(payload, **kwargs)

    plan = {
        "event_expression": PAYLOAD,
        "audience_authority": "event_ir",
        "campaign_constraints": {},
        "original_query": "지난달 구매한 회원",
        "intent": "find_user_segment",
    }
    monkey = graph_rag.query_pipeline.compile_audience_predicate
    try:
        graph_rag.query_pipeline.compile_audience_predicate = spy  # type: ignore[assignment]
        candidate = graph_rag.build_event_expression_sql_candidate(plan)
    finally:
        graph_rag.query_pipeline.compile_audience_predicate = monkey  # type: ignore[assignment]

    assert calls == ["called"]
    assert candidate is not None
    assert "EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL" in candidate["sql"]


def test_builder_records_the_failing_stage_when_a_layer_fails() -> None:
    """실패에 **단계 이름**이 붙는다 — '어디서 막혔는가'가 사유의 일부가 된다."""

    def explode(payload, **kwargs):
        raise query_pipeline.QueryPipelineError("sql_compilation", "표현할 수 없습니다")

    plan = {
        "event_expression": PAYLOAD,
        "audience_authority": "event_ir",
        "campaign_constraints": {},
        "original_query": "지난달 구매한 회원",
        "intent": "find_user_segment",
    }
    original = graph_rag.query_pipeline.compile_audience_predicate
    try:
        graph_rag.query_pipeline.compile_audience_predicate = explode  # type: ignore[assignment]
        assert graph_rag.build_event_expression_sql_candidate(plan) is None
    finally:
        graph_rag.query_pipeline.compile_audience_predicate = original  # type: ignore[assignment]

    unresolved = plan["unresolved_source_conditions"]
    assert unresolved[0]["stage"] == "sql_compilation"
    assert "표현할 수 없습니다" in unresolved[0]["reason"]


def test_malformed_stored_expression_is_rejected_before_any_builder_runs() -> None:
    """파손된 저장 표현은 플랜 검증에서 걸린다(빌더까지 내려가지 않는다)."""
    plan = {
        "event_expression": {"expression": {"type": "not_a_node"}},
        "audience_authority": "event_ir",
        "campaign_constraints": {},
        "original_query": "x",
        "intent": "find_user_segment",
    }
    assert graph_rag.build_event_expression_sql_candidate(plan) is None
    assert "unresolved_source_conditions" not in plan
