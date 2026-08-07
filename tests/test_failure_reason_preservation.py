"""``semantic_ir.failure_reason`` 의 **경계별 보존 계약**.

사유는 판정한 계층만 아는 사실이다. 한 계층이라도 그 필드를 떨어뜨리면 하류의 파생이 대신
답하고, 고칠 곳이 다른 실패가 같은 이름으로 보고된다.

실측(2026-08-07). ``2026년 2월과 3월의 구매금액이 증가한 회원`` 은 낮출 수 있는 요구인데
라이브 12회 중 1회가 ``semantic_registry_gap`` 으로 끝났다. 사용자 문구는 방출 실패로 정확히
나갔는데 사유 코드만 달랐다 — 원인은 :func:`graph_rag.build_query_plan` 이 후보의 semantic_ir
을 ``SemanticOutcome`` 으로 **필드별로 재조립**하면서 ``failure_reason`` 하나만 빠뜨린 것이었다.
그 뒤 :func:`failure_messages.semantic_failure_reason` 이 ``system_failure`` 를 보고 registry
gap 을 지어냈다.

그래서 여기서는 한 경계씩 따로 잰다. 통합 테스트 하나로 묶으면 어느 계층이 떨어뜨렸는지
알 수 없고, 다음에 같은 재조립이 생겨도 같은 방식으로 조용히 샌다::

    semantic IR → projection → validation → plan build → response assembly → failure_messages

그리고 이 파일이 지키는 더 큰 계약 하나: **registry gap 은 추론하지 않는다.**
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import networkx as nx  # noqa: E402
import pytest  # noqa: E402

import failure_messages  # noqa: E402
import graph_rag  # noqa: E402
import semantic_outcome as leaf_outcome  # noqa: E402
from query_structurer import semantic_outcome as wire_outcome  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    validate_campaign_query_plan_v4,
)
from query_structurer.semantic_ir import empty_semantic_ir, validate_semantic_ir  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "tests"))

import test_aggregate_comparison_lowering as comparison  # noqa: E402

DECLARED_REASONS = (
    leaf_outcome.FAILURE_REASON_EMISSION,
    leaf_outcome.FAILURE_REASON_REGISTRY_GAP,
    leaf_outcome.FAILURE_REASON_SYSTEM,
)


# ── 어휘 드리프트 ─────────────────────────────────────────────────────────────────


def test_the_two_declarations_of_the_reason_vocabulary_agree() -> None:
    """리프 진단 모듈과 wire 스키마가 같은 값을 들고 있다(한쪽만 고치면 조용히 어긋난다)."""
    assert wire_outcome.FAILURE_REASONS == leaf_outcome.SYSTEM_FAILURE_REASONS
    assert wire_outcome.FAILURE_REASON_EMISSION == leaf_outcome.FAILURE_REASON_EMISSION
    assert wire_outcome.FAILURE_REASON_REGISTRY_GAP == leaf_outcome.FAILURE_REASON_REGISTRY_GAP
    assert wire_outcome.FAILURE_REASON_SYSTEM == leaf_outcome.FAILURE_REASON_SYSTEM


def test_every_declared_reason_has_a_failure_stage() -> None:
    """새 사유를 추가하고 단계 매핑을 빼면 프론트에서 단계가 사라진다."""
    from rag import failure_stage

    for reason in DECLARED_REASONS:
        assert reason in failure_stage._FAILURE_REASON_TO_STAGE, reason


# ── 경계별 보존 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", DECLARED_REASONS)
def test_boundary_1_semantic_ir_construction(reason: str) -> None:
    ir = empty_semantic_ir(
        status="needs_clarification",
        missing_fields=["audience.requirement"],
        message="m",
        failure_kind="system_failure",
        failure_reason=reason,
    )
    assert ir["failure_reason"] == reason


@pytest.mark.parametrize("reason", DECLARED_REASONS)
def test_boundary_2_outcome_projection(reason: str) -> None:
    outcome = wire_outcome.SemanticOutcome.needs_clarification(
        missing_fields=["audience.requirement"],
        missing_field_causes=[],
        message="m",
        failure_kind="system_failure",
        failure_reason=reason,
    )
    assert outcome.to_legacy_dict()["failure_reason"] == reason


@pytest.mark.parametrize("reason", DECLARED_REASONS)
def test_boundary_3_response_schema_admits_the_reason(reason: str) -> None:
    """wire 스키마가 닫혀 있으므로(additionalProperties=False) 값이 허용값이어야 실린다."""
    from query_structurer.campaign_plan_v4 import SEMANTIC_IR_LLM_JSON_SCHEMA

    schema = SEMANTIC_IR_LLM_JSON_SCHEMA["properties"]["failure_reason"]
    assert reason in schema.get("enum", [reason])


def _emission_plan() -> dict[str, Any]:
    """낮출 수 있는 요구를 모델이 미지원으로 신고한 구조화 payload."""
    return comparison._project("month_to_month_change")


def test_boundary_4_validation_preserves_the_reason() -> None:
    plan = _emission_plan()
    projection = validate_semantic_ir(
        plan["semantic_ir"], plan.get("literal_bindings") or [], payload=plan
    )
    assert projection["failure_reason"] == leaf_outcome.FAILURE_REASON_EMISSION

    validated = validate_campaign_query_plan_v4(
        copy.deepcopy(plan), query=comparison.QUERY, require_semantic=True
    ).to_dict()
    assert validated["semantic_ir"]["failure_reason"] == leaf_outcome.FAILURE_REASON_EMISSION


def test_boundary_5_plan_build_preserves_the_reason() -> None:
    """실측된 유실 지점. 후보 semantic_ir 을 필드별로 재조립하는 곳이라 누락이 조용하다."""
    plan = graph_rag.build_query_plan(
        comparison.QUERY, parser="llm", query_plan_v4=_emission_plan()
    )

    assert (plan.get("semantic_ir") or {}).get("failure_reason") == (
        leaf_outcome.FAILURE_REASON_EMISSION
    )


def test_boundary_6_response_assembly_preserves_the_reason() -> None:
    plan = graph_rag.build_query_plan(
        comparison.QUERY, parser="llm", query_plan_v4=_emission_plan()
    )
    result = graph_rag.build_sql_result(
        nx.Graph(), comparison.QUERY, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=comparison.QUERY,
    )

    assert result["failure_reason"] == leaf_outcome.FAILURE_REASON_EMISSION
    assert result["semantic_ir"]["failure_reason"] == leaf_outcome.FAILURE_REASON_EMISSION


@pytest.mark.parametrize("reason", DECLARED_REASONS)
def test_boundary_7_failure_messages_returns_the_declaration(reason: str) -> None:
    assert failure_messages.semantic_failure_reason(
        "needs_clarification", "system_failure", reason
    ) == reason


# ── registry gap 은 추론하지 않는다 ──────────────────────────────────────────────


def test_a_generic_system_failure_is_never_promoted_to_registry_gap() -> None:
    """사유를 선언하지 않은 system_failure 는 중립으로 남는다 — 설정 결함을 주장하지 않는다."""
    derived = failure_messages.semantic_failure_reason("needs_clarification", "system_failure")

    assert derived == leaf_outcome.FAILURE_REASON_SYSTEM
    assert derived != leaf_outcome.FAILURE_REASON_REGISTRY_GAP


def test_a_real_registry_gap_still_declares_itself() -> None:
    """자산 목록을 대조해 본 경로는 그대로 registry gap 을 선언한다(email_optin 회귀 방지)."""
    query = "이메일 수신에 동의한 회원을 찾아줘"
    plan = graph_rag.build_query_plan(
        query,
        parser="llm",
        query_plan_v4=comparison.attach_campaign_query_plan_v4_identity(
            {
                "intent": "find_user_segment",
                "campaign_constraints": {
                    "objective": None, "offer_type": None, "channels": [], "sell_object": None,
                },
                "result_limit": None,
                "audience_requirement": {
                    "expression": None,
                    "issues": [{
                        "code": "unsupported_semantics",
                        "argument": "email_consent_flag",
                        "message": "이메일 수신 동의 조건을 Canonical Event IR로 표현할 수 없습니다.",
                        "evidence": {"text": "이메일 수신에 동의한", "start": 0, "end": 11},
                    }],
                },
            },
            query,
            current_date=comparison.CURRENT_DATE,
        ),
    )

    assert (plan.get("semantic_ir") or {}).get("failure_reason") == (
        leaf_outcome.FAILURE_REASON_REGISTRY_GAP
    )
    assert "선언된 자산" in (plan.get("semantic_ir") or {}).get("message", "")


# 모델이 근거 구간을 잡는 폭도 회차마다 달랐다(실측: 전체 문장부터 '증가한' 한 낱말까지).
# 이름 × 근거 폭의 **모든 조합**에서 같은 계약이 서야 흔들림이 사라진다.
EVIDENCE_SPANS = (
    "2026년 2월과 3월의 구매금액이 증가한 회원",
    "2026년 2월과 3월의 구매금액이 증가한",
    "구매금액이 증가한",
    "증가한",
    "2026년 2월과 3월",
    "구매금액",
)


def _unsupported_claim_plan(argument: str, span: str) -> dict[str, Any]:
    start = comparison.QUERY.index(span)
    return comparison.attach_campaign_query_plan_v4_identity(
        {
            "intent": "find_user_segment",
            "campaign_constraints": {
                "objective": None, "offer_type": None, "channels": [], "sell_object": None,
            },
            "result_limit": None,
            "audience_requirement": {
                "expression": None,
                "issues": [{
                    "code": "unsupported_semantics",
                    "argument": argument,
                    "message": "표현할 수 없습니다.",
                    "evidence": {"text": span, "start": start, "end": start + len(span)},
                }],
            },
        },
        comparison.QUERY,
        current_date=comparison.CURRENT_DATE,
    )


@pytest.mark.parametrize("argument", comparison.OBSERVED_MODEL_ARGUMENTS)
@pytest.mark.parametrize("span", EVIDENCE_SPANS)
def test_a_lowerable_requirement_never_ends_as_registry_gap(argument: str, span: str) -> None:
    """완료 조건 그 자체 — 낮출 수 있는 요구는 이름·근거 폭과 무관하게 registry gap 이 아니다."""
    import lowering_planner

    assert lowering_planner.plans_for_query(comparison.QUERY), "전제: 이 원문은 낮출 수 있다"

    plan = graph_rag.build_query_plan(
        comparison.QUERY, parser="llm", query_plan_v4=_unsupported_claim_plan(argument, span)
    )
    result = graph_rag.build_sql_result(
        nx.Graph(), comparison.QUERY, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 100,
        original_query=comparison.QUERY,
    )

    assert result["failure_reason"] != leaf_outcome.FAILURE_REASON_REGISTRY_GAP
    assert result["failure_reason"] != "semantic_ir_unsupported"
