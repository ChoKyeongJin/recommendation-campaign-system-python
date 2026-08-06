"""컴파일 실패는 **이름**을 갖는다 — ``no_sql_candidates`` 로 끝나지 않는다.

``None`` 반환 하나에 "미지원" · "컴파일 실패" · "해당 없음" 세 뜻이 뭉쳐 있었고, 그래서
``구매주기가 30일 이하인 회원`` 의 실패 응답에는 단계도 코드도 없었다(2026-08-06 실측 #7).
"""

from __future__ import annotations

import pytest

import capability_audit
import compile_outcome
import semantic_fingerprint


def test_unknown_stage_is_rejected() -> None:
    with pytest.raises(compile_outcome.CompileOutcomeError):
        compile_outcome.CompileFailure(stage="somewhere", code="x")


def test_explicit_unsupported_wins_over_compile_failure() -> None:
    """둘 다 있으면 더 정확한 쪽이 사유다 — 미지원은 판정이고 실패는 결함이다."""

    plan: dict[str, object] = {}
    compile_outcome.record(
        plan,
        compile_outcome.CompileFailure(
            stage=compile_outcome.STAGE_AUDIENCE_LOWERING, code="compile_broke"
        ),
    )
    compile_outcome.record(
        plan,
        compile_outcome.ExplicitUnsupported(
            stage=compile_outcome.STAGE_CAPABILITY, code="event_compiler_unsupported"
        ),
    )
    assert compile_outcome.blocking_outcome(plan)["code"] == "event_compiler_unsupported"


def test_failure_reason_names_the_stage_and_code() -> None:
    plan: dict[str, object] = {}
    compile_outcome.record(
        plan,
        compile_outcome.CompileFailure(
            stage=compile_outcome.STAGE_RULES_FALLBACK,
            code="required_candidate_not_constructed",
            requirement_ids=("req_buy_cycle",),
        ),
    )
    assert compile_outcome.failure_reason(plan, "no_sql_candidates") == (
        "rules_fallback:required_candidate_not_constructed"
    )
    assert compile_outcome.requirement_ids(plan) == ["req_buy_cycle"]


def test_not_applicable_is_not_a_reason() -> None:
    """'이 빌더의 일이 아니다'는 실패가 아니다 — 사유로 승격하면 원인이 흐려진다."""

    plan: dict[str, object] = {}
    compile_outcome.record(
        plan, compile_outcome.NotApplicable(stage=compile_outcome.STAGE_AUDIENCE_LOWERING)
    )
    assert compile_outcome.blocking_outcome(plan) is None
    assert compile_outcome.failure_reason(plan, "no_sql_candidates") == "no_sql_candidates"


def test_every_compile_stage_maps_to_a_pipeline_stage() -> None:
    """조립형 사유가 단계 표에서 빠지면 프론트는 실패 배지를 통째로 그리지 않는다."""

    from rag import failure_stage

    assert set(failure_stage._COMPILE_STAGE_TO_STAGE) == set(compile_outcome.STAGES)
    for stage in compile_outcome.STAGES:
        classified = failure_stage._classify_failure_stage(f"{stage}:some_code")
        assert classified is not None, stage
        assert classified["reason"] == f"{stage}:some_code"


def test_capability_audit_names_the_broken_link() -> None:
    check = capability_audit.CapabilityCheck(
        symbol="orders",
        kind="event_source",
        field_mapping=True,
        operator_mapping=False,
        validator=True,
        lowering=True,
        compiler=True,
        physical_binding=False,
    )
    assert not check.is_complete
    assert check.missing_links == ("operator_mapping", "physical_binding")
    report = capability_audit.AuditReport((check,))
    assert capability_audit.REGISTRY_GAP_CODE in report.summary()
    assert capability_audit.unwired_symbols(report) == ("orders",)


def test_strict_mode_fails_startup_on_a_gap() -> None:
    report = capability_audit.AuditReport(
        (capability_audit.CapabilityCheck(symbol="orders", kind="event_source"),)
    )
    with pytest.raises(capability_audit.CapabilityAuditError):
        capability_audit.enforce(report, strict=True)
    # strict 가 아니면 기동은 계속되지만 gap 사실은 남는다(조용히 지나가지 않는다).
    assert capability_audit.enforce(report, strict=False) is report


def test_declared_event_sources_are_fully_wired() -> None:
    """선언과 구현이 어긋난 심볼이 있으면 프롬프트 실행 **전에** 걸린다."""

    report = capability_audit.audit_event_sources()
    assert report.catalog_error is None, report.catalog_error
    assert report.checks, "심볼이 하나도 없으면 이 검사는 공허하다"
    assert report.is_clean, report.summary()


def test_semantic_fingerprint_ignores_volatile_identity() -> None:
    """생성 순서 id·타임스탬프가 달라도 뜻이 같으면 같은 지문이다."""

    first = semantic_fingerprint.semantic_fingerprint(
        canonical_ir={"type": "and", "operands": [{"a": 1, "id": "n1"}, {"b": 2, "id": "n2"}]},
        result_shape={"kind": "entity_list"},
    )
    second = semantic_fingerprint.semantic_fingerprint(
        canonical_ir={"operands": [{"b": 2, "id": "z9"}, {"id": "z8", "a": 1}], "type": "and"},
        result_shape={"kind": "entity_list"},
    )
    assert first == second


def test_semantic_fingerprint_ignores_evidence_spans() -> None:
    """근거 구간은 '어디서 왔나'이지 '무엇을 뜻하나'가 아니다.

    실측(2026-08-06): 같은 프롬프트 두 실행이 **바이트 동일한 SQL** 을 냈는데 지문이 갈렸다.
    차이는 IR 원자에 붙은 근거 구절과 좌표뿐이었다 — 그것이 지문에 남으면 이 지표는 의미
    편차를 재지 못한다.
    """

    left = {
        "type": "exists",
        "relation": {"type": "source", "name": "purchase"},
        "evidence": {"text": "최근 30일 구매한 회원 수", "start": 0, "end": 12},
    }
    right = {
        "type": "exists",
        "relation": {"type": "source", "name": "purchase"},
        "evidence": {"text": "구매한 회원", "start": 7, "end": 12},
    }
    assert semantic_fingerprint.semantic_fingerprint(
        canonical_ir=left
    ) == semantic_fingerprint.semantic_fingerprint(canonical_ir=right)


def test_semantic_fingerprint_still_sees_a_different_source() -> None:
    """근거를 지우더라도 **뜻이 다르면** 지문은 달라야 한다(퇴화 방지)."""

    purchase = {"type": "exists", "relation": {"type": "source", "name": "purchase"}}
    cart = {"type": "exists", "relation": {"type": "source", "name": "cart"}}
    assert semantic_fingerprint.semantic_fingerprint(
        canonical_ir=purchase
    ) != semantic_fingerprint.semantic_fingerprint(canonical_ir=cart)


def test_semantic_fingerprint_changes_with_the_result_shape() -> None:
    ir = {"type": "exists", "relation": {"name": "purchase"}}
    listed = semantic_fingerprint.semantic_fingerprint(
        canonical_ir=ir, result_shape={"kind": "entity_list"}
    )
    counted = semantic_fingerprint.semantic_fingerprint(
        canonical_ir=ir, result_shape={"kind": "scalar", "metric": "count"}
    )
    assert listed != counted


def test_execution_fingerprint_separates_the_environment() -> None:
    """같은 뜻이라도 모델이 바뀌면 실행 지문은 달라야 한다(의미 지문은 그대로일 수 있다)."""

    common = dict(
        prompt="최근 30일 구매한 회원",
        model_provider="openai",
        schema_digest="s1",
        catalog_digest="c1",
        policy_digest="p1",
        compiler_version="1",
        reference_time="2026-08-06",
        timezone="Asia/Seoul",
    )
    assert semantic_fingerprint.execution_fingerprint(
        model_name="model-a", **common
    ) != semantic_fingerprint.execution_fingerprint(model_name="model-b", **common)


def test_advisory_policy_decisions_do_not_move_the_semantic_fingerprint() -> None:
    """적재 범위 '고지'는 안내지 의미 변화가 아니다 — 문구 하나가 지문을 흔들면 안 된다."""

    ir = {"type": "exists", "relation": {"name": "purchase"}}
    plain = semantic_fingerprint.semantic_fingerprint(canonical_ir=ir)
    advised = semantic_fingerprint.semantic_fingerprint(
        canonical_ir=ir,
        policy_decisions=[
            {
                "policy_id": "data_availability",
                "decision": "allow",
                "reason_code": "data_coverage_gap_advised",
            }
        ],
    )
    # 원문이 말한 기간을 되찾은 교정도 의미 변화가 아니다 — 되찾은 값은 이미 IR 안에 있다.
    repaired = semantic_fingerprint.semantic_fingerprint(
        canonical_ir=ir,
        policy_decisions=[
            {
                "policy_id": "period_binding",
                "decision": "allow",
                "reason_code": "stated_period_contradicts_missing_argument",
            }
        ],
    )
    # 반면 배포 설정이 **원문에 없던** 기간을 채운 것은 뜻을 바꾼다.
    invented = semantic_fingerprint.semantic_fingerprint(
        canonical_ir=ir,
        policy_decisions=[
            {
                "policy_id": "period_binding",
                "decision": "allow",
                "reason_code": "default_period_applied",
            }
        ],
    )
    assert plain == advised == repaired
    assert plain != invented
