"""오디언스 실행 권위의 계약 — "존재가 아니라 선언이 실행 경로를 정한다".

이 파일이 지키는 것은 이행기의 가장 비싼 사고 넷이다:

    ① 저장돼 있다는 이유만으로 Event IR 이 실행된다
    ② Event IR 컴파일이 실패하면 legacy 로 조용히 폴백한다
    ③ 검증(shadow) 단계를 건너뛴 cut-over
    ④ rollback 이 IR 을 슬롯으로 역변환한다
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_authority  # noqa: E402
import graph_rag  # noqa: E402
from audience_authority import AudienceAuthority, MigrationStatus  # noqa: E402


def test_authority_defaults_to_legacy_for_a_bare_plan() -> None:
    assert audience_authority.resolve_authority({}) is AudienceAuthority.LEGACY
    assert audience_authority.resolve_authority(None) is AudienceAuthority.LEGACY


def test_stored_expression_alone_never_grants_event_ir_authority() -> None:
    """이행 어댑터가 세운 표현(source=legacy_migration)은 저장돼 있어도 실행되지 않는다."""

    plan = {
        "event_expression": {
            "expression": {"type": "exists", "relation": {"type": "source", "name": "purchase"}},
            "source": "legacy_migration",
        }
    }

    assert audience_authority.resolve_authority(plan) is AudienceAuthority.LEGACY
    assert graph_rag._has_canonical_audience_authority(plan) is False


def test_ingress_marker_keeps_working_for_stored_canonical_payloads() -> None:
    """명시 필드가 없는 **기존** 페이로드는 ingress 표식으로 읽는다(역직렬화 호환)."""

    for marker in sorted(audience_authority.CANONICAL_EVENT_EXPRESSION_SOURCES):
        plan = {
            "event_expression": {
                "expression": {"type": "exists", "relation": {"type": "source", "name": "purchase"}},
                "source": marker,
            }
        }
        assert audience_authority.resolve_authority(plan) is AudienceAuthority.EVENT_IR
        assert graph_rag._has_canonical_audience_authority(plan) is True


def test_explicit_declaration_wins_over_the_compatibility_marker() -> None:
    plan = {
        "audience_authority": "legacy",
        "event_expression": {
            "expression": {"type": "exists", "relation": {"type": "source", "name": "purchase"}},
            "source": "audience_requirement",
        },
    }

    assert audience_authority.resolve_authority(plan) is AudienceAuthority.LEGACY
    assert graph_rag._has_canonical_audience_authority(plan) is False


def test_unknown_authority_value_fails_closed_instead_of_downgrading() -> None:
    """오타 하나로 Event IR 자산이 조용히 legacy 로 실행되면 안 된다."""

    with pytest.raises(audience_authority.AudienceAuthorityError):
        audience_authority.resolve_authority({"audience_authority": "event-ir"})


def test_stamp_authority_round_trips_through_serialization() -> None:
    plan: dict[str, object] = {}
    audience_authority.stamp_authority(plan, AudienceAuthority.EVENT_IR)

    assert plan["audience_authority"] == "event_ir"  # 문자열이어야 JSON 경계를 넘는다
    assert audience_authority.resolve_authority(plan) is AudienceAuthority.EVENT_IR


def test_authority_is_derived_from_status_and_never_stored_twice() -> None:
    event_ir_states = {
        status for status in MigrationStatus
        if audience_authority.authority_for_status(status) is AudienceAuthority.EVENT_IR
    }

    assert event_ir_states == {
        MigrationStatus.EVENT_IR_PRIMARY,
        MigrationStatus.ROLLBACK_ELIGIBLE,
        MigrationStatus.RETIRED,
    }


def test_cutover_is_only_reachable_from_shadow_verification() -> None:
    """검증을 건너뛴 cut-over 를 상태 기계가 막는다."""

    predecessors = {
        status for status, targets in audience_authority.ALLOWED_TRANSITIONS.items()
        if MigrationStatus.EVENT_IR_PRIMARY in targets
    }

    assert predecessors == {audience_authority.CUTOVER_PREDECESSOR}
    assert audience_authority.can_transition(
        MigrationStatus.CONVERTED, MigrationStatus.EVENT_IR_PRIMARY
    ) is False
    with pytest.raises(audience_authority.AudienceAuthorityError):
        audience_authority.transition(MigrationStatus.LEGACY_ONLY, MigrationStatus.EVENT_IR_PRIMARY)


def test_stale_assets_must_be_reconverted_before_verification() -> None:
    assert audience_authority.can_transition(MigrationStatus.STALE, MigrationStatus.SHADOW_VERIFIED) is False
    assert audience_authority.can_transition(MigrationStatus.STALE, MigrationStatus.CONVERTED) is True


def test_rollback_target_is_the_preserved_legacy_state() -> None:
    """rollback 은 역변환이 아니라 권위 되돌리기다 — 목적지는 legacy payload 그대로인 상태다."""

    assert audience_authority.ROLLBACK_TARGET is MigrationStatus.LEGACY_ONLY
    assert audience_authority.authority_for_status(
        audience_authority.ROLLBACK_TARGET
    ) is AudienceAuthority.LEGACY
    for status in (MigrationStatus.EVENT_IR_PRIMARY, MigrationStatus.ROLLBACK_ELIGIBLE):
        assert audience_authority.can_transition(status, MigrationStatus.LEGACY_ONLY)


def test_retired_is_terminal_because_the_legacy_payload_is_gone() -> None:
    assert audience_authority.ALLOWED_TRANSITIONS[MigrationStatus.RETIRED] == frozenset()


def test_blocked_statuses_never_carry_event_ir_authority() -> None:
    for status in MigrationStatus:
        if audience_authority.is_blocked(status):
            assert audience_authority.authority_for_status(status) is AudienceAuthority.LEGACY


def test_executor_reads_authority_and_not_expression_presence() -> None:
    """`_has_canonical_audience_authority` 가 다시 페이로드 모양을 읽기 시작하면 red.

    함수 본문이 ``event_expression`` 을 직접 들여다보는 순간 판정자가 둘이 되고, 한쪽만 고치는
    드리프트가 시작된다 — 이 이행에서 그 드리프트의 값은 '검증 전 IR 이 실행된다'이다.
    """

    source = (REPO_ROOT / "graph_rag.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_has_canonical_audience_authority"
    )
    body_strings = {
        node.value for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } - {ast.get_docstring(function, clean=False)}
    calls = {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }

    assert calls == {"audience_authority.executes_event_ir"}
    assert not body_strings, f"판정 함수가 다시 자체 어휘를 들고 있다: {sorted(body_strings)}"
