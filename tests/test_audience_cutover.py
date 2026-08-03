"""cut-over / rollback 판정의 계약 — **권위를 옮길 때 무엇이 반드시 참이어야 하는가.**

이 파일이 지키는 사고 다섯:

    ① 검증을 건너뛰거나, 다른 것을 검증한 보고서로 cut-over 한다
    ② 원본·바인딩이 움직인 뒤에도 예전 검증 근거로 cut-over 한다
    ③ 되돌릴 재료(보존 payload) 없이 권위를 옮긴다
    ④ rollback 이 IR 을 슬롯으로 역변환한다
    ⑤ legacy 가 더 이상 실행하지 못하는 슬롯을 가진 자산을 legacy 로 되돌린다
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_cutover as cutover  # noqa: E402
import migration_fingerprint  # noqa: E402
from audience_authority import AudienceAuthority, MigrationStatus  # noqa: E402

EXPRESSION: dict[str, Any] = {
    "type": "not",
    "operand": {"type": "exists", "relation": {"type": "source", "name": "purchase"}},
}
PAYLOAD: dict[str, Any] = {"target_user": {"behaviors": ["no_purchase"]}}
ENVELOPE: dict[str, Any] = {"expression": EXPRESSION, "source": "legacy_migration", "receipts": []}

SEMANTIC = migration_fingerprint.compute_semantic_fingerprint(EXPRESSION)


def _facts(**overrides: Any) -> cutover.ConversionFacts:
    values: dict[str, Any] = {
        "source_fingerprint": "src-1",
        "source_schema_checksum": "shape-1",
        "semantic_fingerprint": SEMANTIC,
        "binding_fingerprint": "bind-1",
        "is_executable": True,
        "status": MigrationStatus.CONVERTED,
        "expression": EXPRESSION,
    }
    values.update(overrides)
    return cutover.ConversionFacts(**values)


def _state(**overrides: Any) -> cutover.StoredState:
    values: dict[str, Any] = {
        "asset_id": "aud-x",
        "revision": 1,
        "status": MigrationStatus.SHADOW_VERIFIED,
        "source_fingerprint": "src-1",
        "source_schema_checksum": "shape-1",
        "semantic_fingerprint": SEMANTIC,
        "binding_fingerprint": "bind-1",
        "legacy_payload": PAYLOAD,
        "legacy_payload_checksum": migration_fingerprint.digest(PAYLOAD),
        "event_expression": ENVELOPE,
        "verification_digest": "evidence-1",
        "verified_at": "2026-08-03T00:00:00+00:00",
        "row_version": 3,
    }
    values.update(overrides)
    return cutover.StoredState(**values)


def _report(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "asset_id": "aud-x",
        "asset_revision": 1,
        "cutover_allowed": True,
        "blocking_reasons": [],
        "source_fingerprint": "src-1",
        "semantic_fingerprint": SEMANTIC,
        "stages": [{"stage": "path_accounting", "status": "pass", "divergences": []}],
    }
    entry.update(overrides)
    return {"generated_at": "2026-08-03T00:00:00+00:00", "assets": [entry]}


def _codes(decision: cutover.Decision) -> set[str]:
    return {item.code for item in decision.blockers}


def _cutover(state: cutover.StoredState | None, facts: Any = None, **kwargs: Any) -> cutover.Decision:
    """기본은 '저장 플랜이 판정 대상과 같은 자산'이다 — 그 대조 자체를 보는 테스트만 덮어쓴다."""
    resolved = facts or _facts()
    kwargs.setdefault("plan_source_fingerprint", resolved.source_fingerprint)
    return cutover.evaluate_cutover(
        state, resolved, asset_id="aud-x", revision=1,
        plan_present=kwargs.pop("plan_present", True), **kwargs,
    )


# ── ① 검증 근거 ───────────────────────────────────────────────────────────────────


def test_cutover_is_allowed_only_when_every_material_lines_up() -> None:
    """공허한 통과 방지: 아래 차단 테스트들이 '원래 항상 막힌다'가 아님을 먼저 고정한다."""
    decision = _cutover(_state())

    assert decision.allowed, decision.to_dict()["blockers"]
    assert decision.to_status is MigrationStatus.EVENT_IR_PRIMARY
    assert decision.authority is AudienceAuthority.EVENT_IR
    assert decision.evidence_digest == "evidence-1"


def test_cutover_from_converted_is_refused_by_the_state_machine() -> None:
    """검증을 건너뛴 cut-over 는 판정이 아니라 상태 기계가 막는다."""
    decision = _cutover(_state(status=MigrationStatus.CONVERTED))

    assert not decision.allowed
    assert cutover.ILLEGAL_TRANSITION in _codes(decision)
    assert decision.to_status is None


def test_cutover_requires_stored_verification_evidence() -> None:
    """상태만 SHADOW_VERIFIED 로 만들어 두고 넘어가는 길을 막는다."""
    decision = _cutover(_state(verification_digest="", verified_at=""))

    assert cutover.SHADOW_VERIFICATION_MISSING in _codes(decision)


def test_cutover_refuses_a_report_that_is_not_the_one_used_for_promotion() -> None:
    verdict = cutover.shadow_verdict_from_report(_report(), asset_id="aud-x", revision=1)
    decision = _cutover(_state(), verdict=verdict)

    assert cutover.SHADOW_EVIDENCE_CHANGED in _codes(decision)


def test_missing_state_row_blocks_instead_of_creating_one() -> None:
    decision = _cutover(None)

    assert _codes(decision) == {cutover.STATE_ROW_MISSING}


# ── ② 지문 세 축 ──────────────────────────────────────────────────────────────────


def test_moved_source_blocks_cutover_because_the_verification_was_about_the_old_source() -> None:
    decision = _cutover(_state(), _facts(source_fingerprint="src-2"))

    assert cutover.SOURCE_FINGERPRINT_MOVED in _codes(decision)


def test_moved_schema_shape_blocks_cutover() -> None:
    """값이 같아도 모양이 바뀌면 경로 회계가 더 이상 완전하지 않다."""
    decision = _cutover(_state(), _facts(source_schema_checksum="shape-2"))

    assert cutover.SOURCE_SCHEMA_CHECKSUM_MOVED in _codes(decision)


def test_binding_change_blocks_cutover_even_when_the_meaning_is_identical() -> None:
    """지문을 세 축으로 나눈 값이 여기서 나온다 — 뜻은 같은데 SQL 이 달라진 경우다."""
    decision = _cutover(_state(), _facts(binding_fingerprint="bind-2"))

    assert _codes(decision) == {cutover.BINDING_FINGERPRINT_MOVED}
    assert "다시 검증하면" in next(
        item.message for item in decision.blockers if item.code == cutover.BINDING_FINGERPRINT_MOVED
    )


def test_stored_expression_must_be_the_verified_one() -> None:
    """플랜에 얹는 것은 **검증된 산출물**이다 — 저장본이 다른 뜻이면 그것은 아니다."""
    other = {"type": "exists", "relation": {"type": "source", "name": "purchase"}}
    decision = _cutover(_state(event_expression={"expression": other}))

    assert cutover.SEMANTIC_FINGERPRINT_MOVED in _codes(decision)


def test_missing_stored_expression_blocks_cutover() -> None:
    decision = _cutover(_state(event_expression=None))

    assert cutover.EXPRESSION_MISSING in _codes(decision)


def test_non_executable_conversion_never_takes_authority() -> None:
    decision = _cutover(_state(), _facts(is_executable=False))

    assert cutover.CONVERSION_NOT_EXECUTABLE in _codes(decision)


# ── ③ 되돌릴 재료 ─────────────────────────────────────────────────────────────────


def test_cutover_requires_the_preserved_payload_that_rollback_will_need() -> None:
    decision = _cutover(_state(legacy_payload={}))

    assert cutover.PRESERVED_PAYLOAD_MISSING in _codes(decision)


def test_file_corpus_is_not_an_execution_target() -> None:
    """저장된 플랜이 없으면 옮길 권위 자체가 없다(파일 코퍼스는 실행 경로가 아니다)."""
    decision = _cutover(_state(), plan_present=False)

    assert cutover.PLAN_ROW_MISSING in _codes(decision)


# ── ⑥ 판정한 자산 = 권위를 얹을 자산 ──────────────────────────────────────────────


def test_cutover_refuses_when_the_stored_plan_holds_a_different_audience() -> None:
    """판정은 읽어 온 payload 로 하고 스탬프는 저장 행에 한다 — 둘이 다르면 **다른 자산**에 얹는다.

    이 대조가 없던 동안 파일 코퍼스에서 검증한 IR 이 저장 플랜의 실행 권위가 될 수 있었다.
    """
    decision = _cutover(_state(), plan_source_fingerprint="src-in-db")

    assert cutover.PLAN_PAYLOAD_MISMATCH in _codes(decision)
    assert decision.to_status is None


def test_omitting_the_plan_comparison_blocks_instead_of_assuming_they_match() -> None:
    """대조 재료를 넘기지 않은 호출은 통과가 아니다 — 빼먹은 대조가 조용히 통과하면 규칙이 없는 것이다."""
    decision = cutover.evaluate_cutover(
        _state(), _facts(), asset_id="aud-x", revision=1, plan_present=True
    )

    assert cutover.PLAN_PAYLOAD_UNVERIFIED in _codes(decision)


def test_an_unreadable_plan_row_is_not_read_as_matching() -> None:
    """저장 플랜을 변환하지 못했으면 '같다'가 아니라 '대조하지 않았다'이고, 그것은 차단이다."""
    decision = _cutover(_state(), plan_source_fingerprint="")

    assert cutover.PLAN_PAYLOAD_UNVERIFIED in _codes(decision)


def test_rollback_keeps_the_same_comparison_as_a_warning_not_a_blocker() -> None:
    """같은 대조가 방향에 따라 다르게 끝난다 — 옮기는 길은 막고, 되돌아오는 길은 열어 둔다."""
    moving = _cutover(_state(), plan_source_fingerprint="src-in-db")
    returning = cutover.evaluate_rollback(
        _state(status=MigrationStatus.EVENT_IR_PRIMARY), asset_id="aud-x", revision=1,
        plan_present=True, live_source_fingerprint="src-in-db",
    )

    assert not moving.allowed and cutover.PLAN_PAYLOAD_MISMATCH in _codes(moving)
    assert returning.allowed
    assert cutover.LEGACY_PAYLOAD_DRIFTED in {item.code for item in returning.warnings}


# ── 승격(promote) ─────────────────────────────────────────────────────────────────


def test_promotion_needs_a_report_that_allows_cutover() -> None:
    report = _report(cutover_allowed=False, blocking_reasons=["필수 검증 단계 미실행: snapshot_members"])
    verdict = cutover.shadow_verdict_from_report(report, asset_id="aud-x", revision=1)
    decision = cutover.evaluate_promotion(_state(status=MigrationStatus.CONVERTED), verdict, _facts())

    assert cutover.SHADOW_REPORT_BLOCKED in _codes(decision)


def test_a_not_run_stage_blocks_promotion_even_if_the_report_says_allowed() -> None:
    """미실행은 통과가 아니다 — 보고서가 뭐라고 하든 여기서 한 번 더 센다."""
    report = _report(stages=[
        {"stage": "path_accounting", "status": "pass"},
        {"stage": "snapshot_members", "status": "not_run", "detail": "요청되지 않았다"},
    ])
    verdict = cutover.shadow_verdict_from_report(report, asset_id="aud-x", revision=1)
    decision = cutover.evaluate_promotion(_state(status=MigrationStatus.CONVERTED), verdict, _facts())

    assert cutover.SHADOW_REPORT_STAGE_NOT_RUN in _codes(decision)
    assert verdict.not_run_stages == ("snapshot_members",)


def test_promotion_rejects_a_report_about_a_different_source() -> None:
    report = _report(source_fingerprint="src-old")
    verdict = cutover.shadow_verdict_from_report(report, asset_id="aud-x", revision=1)
    decision = cutover.evaluate_promotion(_state(status=MigrationStatus.CONVERTED), verdict, _facts())

    assert cutover.SHADOW_REPORT_FINGERPRINT_MISMATCH in _codes(decision)


def test_promotion_carries_the_evidence_pointer_forward() -> None:
    verdict = cutover.shadow_verdict_from_report(_report(), asset_id="aud-x", revision=1)
    decision = cutover.evaluate_promotion(_state(status=MigrationStatus.CONVERTED), verdict, _facts())

    assert decision.allowed
    assert decision.evidence_digest == verdict.digest
    assert decision.verified_at == "2026-08-03T00:00:00+00:00"


def test_report_for_another_asset_is_unreadable_rather_than_blocking() -> None:
    """보고서를 잘못 준 것과 검증이 막힌 것은 다른 사건이다."""
    with pytest.raises(cutover.CutoverError):
        cutover.shadow_verdict_from_report(_report(), asset_id="aud-other", revision=1)
    with pytest.raises(cutover.CutoverError):
        cutover.shadow_verdict_from_report(_report(), asset_id="aud-x", revision=2)


def test_report_without_a_generation_time_is_refused() -> None:
    report = _report()
    report.pop("generated_at")

    with pytest.raises(cutover.CutoverError):
        cutover.shadow_verdict_from_report(report, asset_id="aud-x", revision=1)


# ── ④⑤ rollback ──────────────────────────────────────────────────────────────────


def _rollback(state: cutover.StoredState | None, **kwargs: Any) -> cutover.Decision:
    kwargs.setdefault("plan_present", True)
    return cutover.evaluate_rollback(state, asset_id="aud-x", revision=1, **kwargs)


def test_rollback_returns_authority_to_legacy_from_event_ir_states() -> None:
    for status in (MigrationStatus.EVENT_IR_PRIMARY, MigrationStatus.ROLLBACK_ELIGIBLE):
        decision = _rollback(_state(status=status))

        assert decision.allowed, decision.to_dict()["blockers"]
        assert decision.to_status is MigrationStatus.LEGACY_ONLY
        assert decision.authority is AudienceAuthority.LEGACY


def test_rollback_refuses_a_corrupt_preserved_payload() -> None:
    decision = _rollback(_state(status=MigrationStatus.EVENT_IR_PRIMARY, legacy_payload_checksum="x"))

    assert cutover.PRESERVED_PAYLOAD_CORRUPT in _codes(decision)


def test_rollback_refuses_when_the_legacy_compiler_lost_a_slot() -> None:
    """되돌린 결과가 '조건 하나가 조용히 사라진 오디언스'면 실패보다 나쁘다."""
    decision = _rollback(
        _state(status=MigrationStatus.EVENT_IR_PRIMARY),
        unsupported_paths=("target_user.behaviors:no_purchase",),
    )

    assert cutover.LEGACY_SLOT_UNSUPPORTED in _codes(decision)


def test_source_drift_is_recorded_but_never_locks_the_exit() -> None:
    """잘못된 cut-over 에서 빠져나오는 유일한 길을 무관한 편집 하나가 막으면 안 된다."""
    decision = _rollback(
        _state(status=MigrationStatus.EVENT_IR_PRIMARY), live_source_fingerprint="src-edited"
    )

    assert decision.allowed
    assert {item.code for item in decision.warnings} == {cutover.LEGACY_PAYLOAD_DRIFTED}


def test_rollback_of_a_legacy_asset_is_an_illegal_transition() -> None:
    decision = _rollback(_state(status=MigrationStatus.CONVERTED))

    assert cutover.ILLEGAL_TRANSITION in _codes(decision)


def test_rollback_requires_the_revision_it_preserved() -> None:
    decision = cutover.evaluate_rollback(
        _state(status=MigrationStatus.EVENT_IR_PRIMARY), asset_id="aud-x", revision=7,
        plan_present=True,
    )

    assert cutover.REVISION_MISMATCH in _codes(decision)


# ── 플랜 페이로드 ─────────────────────────────────────────────────────────────────


def test_cutover_keeps_the_legacy_slots_and_moves_only_the_authority() -> None:
    plan = {"target_user": {"behaviors": ["no_purchase"]}, "intent": "find_user_segment"}

    updated = cutover.plan_after_cutover(plan, ENVELOPE)

    assert updated["target_user"] == plan["target_user"], "슬롯을 지우면 rollback 이 역변환이 된다"
    assert updated["event_expression"]["expression"] == EXPRESSION
    assert cutover.executed_authority(updated) is AudienceAuthority.EVENT_IR
    assert "audience_authority" not in plan, "입력 플랜을 변형하지 않는다"


def test_rollback_moves_authority_without_deleting_the_expression() -> None:
    """rollback 은 표현을 지우는 일이 아니다 — 저장은 남고 실행만 legacy 로 돌아간다."""
    plan = cutover.plan_after_cutover({"target_user": {"behaviors": ["no_purchase"]}}, ENVELOPE)

    reverted = cutover.plan_after_rollback(plan)

    assert reverted["event_expression"] == plan["event_expression"]
    assert cutover.executed_authority(reverted) is AudienceAuthority.LEGACY


def test_cutover_envelope_without_an_expression_is_refused() -> None:
    with pytest.raises(cutover.CutoverError):
        cutover.plan_after_cutover({}, {"source": "legacy_migration"})


# ── 저장(record) ──────────────────────────────────────────────────────────────────


def test_record_never_overwrites_an_asset_that_is_already_executing_event_ir() -> None:
    decision = cutover.evaluate_record(
        _state(status=MigrationStatus.EVENT_IR_PRIMARY), _facts(), asset_id="aud-x", revision=1
    )

    assert cutover.AUTHORITY_ALREADY_EVENT_IR in _codes(decision)


def test_record_of_a_moved_source_is_allowed_but_says_so() -> None:
    """원본이 바뀐 자산을 다시 변환하는 것이 record 의 일이다(그리고 검증 근거는 폐기된다)."""
    decision = cutover.evaluate_record(
        _state(status=MigrationStatus.CONVERTED, source_fingerprint="src-old"),
        _facts(), asset_id="aud-x", revision=1,
    )

    assert decision.allowed
    assert cutover.SOURCE_FINGERPRINT_MOVED in {item.code for item in decision.warnings}


def test_re_recording_says_out_loud_that_the_verification_is_discarded() -> None:
    """조용히 지우면 운영자는 승격이 왜 다시 필요해졌는지 모른 채 promote 를 다시 돌린다."""
    decision = cutover.evaluate_record(
        _state(status=MigrationStatus.SHADOW_VERIFIED), _facts(), asset_id="aud-x", revision=1
    )

    assert decision.allowed
    assert decision.to_status is MigrationStatus.CONVERTED
    assert cutover.VERIFICATION_EVIDENCE_DISCARDED in {item.code for item in decision.warnings}


def test_record_without_a_state_row_starts_from_legacy_only() -> None:
    decision = cutover.evaluate_record(None, _facts(), asset_id="aud-x", revision=1)

    assert decision.allowed
    assert decision.from_status is MigrationStatus.LEGACY_ONLY
    assert decision.authority is AudienceAuthority.LEGACY, "저장은 권위를 옮기지 않는다"


# ── legacy 슬롯 지원 판정 ─────────────────────────────────────────────────────────


def test_unsupported_paths_read_the_injected_vocabulary_not_a_local_list() -> None:
    payload: Mapping[str, Any] = {
        "target_user": {
            "behaviors": ["no_purchase", "office_worker"],
            "purchase_membership": {"operator": "exists"},
            "slot_that_vanished": {"value": 1},
            "empty_slot": [],
        },
        "exclude": {"gender": ["male"]},
    }

    missing = cutover.unsupported_legacy_paths(
        payload,
        containers=("target_user", "exclude"),
        supported_slots={"behaviors", "purchase_membership", "gender"},
        supported_behaviors={"no_purchase"},
    )

    assert missing == (
        "target_user.behaviors:office_worker",
        "target_user.slot_that_vanished",
    )
