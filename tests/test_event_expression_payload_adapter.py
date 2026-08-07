"""페이로드 어댑터 — 저장된 ``event_expression`` 을 한 방향으로만 잇는다.

전신은 ``tests/test_query_pipeline_legacy_adapter.py`` 다(같은 계약, 이름만 바뀌었다).
어댑터가 deprecated 라고 경고하던 계약은 **삭제**됐다 — legacy 오디언스 레인이 닫히면서
``plan["event_expression"]`` 이 폐기 대상 슬롯이 아니라 오디언스 의미의 유일한 직렬화 표기가
됐기 때문이다. 없어질 것이라고 광고하던 경고를 지우는 것이 그 사실의 기록이다.
"""

from __future__ import annotations

import pytest
from query_pipeline_fixtures import clock, id_factory

import graph_rag
import query_pipeline
from query_pipeline.plan_payload.event_expression_payload import (
    EventExpressionPayload,
    EventExpressionPayloadAdapter,
    PayloadAdapterError,
    PayloadConversionFailed,
    PayloadSpecConversion,
    to_unsupported_payload,
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


def test_adapter_produces_a_validated_spec() -> None:
    spec = query_pipeline.audience_spec_from_plan_payload(
        PAYLOAD, clock=clock(), id_factory=id_factory()
    )
    assert spec.source.requirement_id == "plan-event-expression"
    assert spec.bindings["purchase.occurred_at"].entity == "purchase"
    assert "node.exists" in spec.capabilities.required
    # 어디서 온 표현인지가 영수증에 남는다.
    assert "audience_requirement" in spec.receipts[0].reason


def test_adapter_tolerates_extra_keys_from_producers() -> None:
    payload = EventExpressionPayload.model_validate(
        {**PAYLOAD, "binding_fingerprint": "abc", "compiler_version": "1.0.0"}
    )
    assert payload.is_canonical


def test_adapter_reports_structured_failure_instead_of_raising() -> None:
    result = EventExpressionPayloadAdapter.convert({"expression": {"type": "nope"}})
    assert isinstance(result, PayloadConversionFailed)
    assert result.stage == "event_expression_payload_adapter"

    ok = EventExpressionPayloadAdapter.convert(
        PAYLOAD, clock=clock(), id_factory=id_factory()
    )
    assert isinstance(ok, PayloadSpecConversion)


def test_adapter_refuses_a_payload_without_expression() -> None:
    with pytest.raises(PayloadAdapterError, match="표현"):
        EventExpressionPayloadAdapter.to_event_query_spec(
            {"source": "audience_requirement"}
        )


def test_unsupported_shape_is_preserved() -> None:
    payload = to_unsupported_payload("event_compiler_unsupported", "설명")
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
        "campaign_constraints": {},
        "original_query": "x",
        "intent": "find_user_segment",
    }
    assert graph_rag.build_event_expression_sql_candidate(plan) is None
    assert "unresolved_source_conditions" not in plan
