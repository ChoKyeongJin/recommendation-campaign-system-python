"""오디언스 실행 권위의 계약 — "실행 언어는 하나다".

2026-08-07 legacy 실행 레인이 폐쇄되면서 이 파일이 지키는 것이 바뀌었다. 이행기의 계약은
"존재가 아니라 선언이 실행 경로를 정한다"였고(경로가 둘이었으므로), 그 시절의 상태 기계
계약 넷(cut-over 선행 상태·rollback 목적지·STALE 재변환·RETIRED 종착)은 이행 도구와 함께
**삭제**됐다. 지금 지키는 것은 셋이다.

    ① 어떤 플랜도 두 번째 오디언스 언어로 새지 않는다 — 표식이 없어도, 계약이 없어도.
    ② 저장된 ``audience_authority: "legacy"`` 는 조용히 삼켜지지 않고 **명명된 실패**가 된다.
    ③ 실행기는 권위 술어만 읽고 페이로드 모양을 다시 해석하지 않는다.

②가 이 폐쇄에서 가장 비싼 자리다. 폐쇄된 값을 "이제 event_ir 과 같은 뜻"으로 접어 읽으면,
legacy 실행을 기대하고 저장된 플랜이 다른 오디언스를 조용히 추출한다.
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
from audience_authority import AudienceAuthority  # noqa: E402


def test_the_vocabulary_has_exactly_one_authority() -> None:
    """값이 둘이 되는 순간 "무엇이 실행됐는가"의 답도 둘이 된다."""

    assert list(AudienceAuthority) == [AudienceAuthority.EVENT_IR]
    assert not hasattr(AudienceAuthority, "LEGACY")


def test_a_bare_plan_requires_event_ir_instead_of_defaulting_to_legacy() -> None:
    """폐쇄 전 이 자리의 기본값이 legacy 였다 — rules 레인이 새어 나가던 마지막 통로."""

    assert audience_authority.resolve_authority({}) is AudienceAuthority.EVENT_IR
    assert audience_authority.resolve_authority(None) is AudienceAuthority.EVENT_IR
    assert audience_authority.requires_event_ir({}) is True
    assert audience_authority.requires_event_ir({"target_user": {"gender": "F"}}) is True


def test_plans_without_a_canonical_contract_are_no_longer_a_separate_lane() -> None:
    """``audience_requirement`` 계약의 유무가 더 이상 경로를 가르지 않는다.

    폐쇄 전에는 계약이 없는 플랜(rules 레인·저장 페이로드)이 legacy 로 남았고, 그 갈래를
    `declares_canonical_audience` 가 소유했다. 그 술어는 갈래와 함께 삭제됐다.
    """

    assert not hasattr(audience_authority, "declares_canonical_audience")
    for plan in ({}, {"audience_requirement": {"issues": []}}, {"target_user": {"gender": "F"}}):
        assert audience_authority.requires_event_ir(plan) is True


def test_a_stored_legacy_stamp_fails_loudly_instead_of_being_folded() -> None:
    """폐쇄된 값은 강등도 승격도 하지 않는다 — 예외이고, 응답 경로가 명명된 실패로 잡는다."""

    plan = {
        "audience_authority": "legacy",
        "event_expression": {
            "expression": {"type": "exists", "relation": {"type": "source", "name": "purchase"}},
            "source": "audience_requirement",
        },
    }

    with pytest.raises(audience_authority.AudienceAuthorityError, match="legacy"):
        audience_authority.resolve_authority(plan)


def test_the_named_failure_gate_catches_the_stored_legacy_stamp() -> None:
    """②의 배선 — 예외가 500 이 되지 않고 ``audience_authority_invalid`` 로 종결한다."""

    blocked = graph_rag._audience_authority_blocking_sql_result(
        {"audience_authority": "legacy"}
    )

    assert blocked is not None
    assert blocked["failure_reason"] == "audience_authority_invalid"
    assert blocked["sql"] is None


def test_unknown_authority_value_fails_closed_instead_of_downgrading() -> None:
    with pytest.raises(audience_authority.AudienceAuthorityError):
        audience_authority.resolve_authority({"audience_authority": "event-ir"})


def test_stamp_authority_round_trips_through_serialization() -> None:
    plan: dict[str, object] = {}
    audience_authority.stamp_authority(plan, AudienceAuthority.EVENT_IR)

    assert plan["audience_authority"] == "event_ir"  # 문자열이어야 JSON 경계를 넘는다
    assert audience_authority.resolve_authority(plan) is AudienceAuthority.EVENT_IR


def test_the_migration_state_machine_is_gone() -> None:
    """상태 기계는 cut-over/shadow/rollback 도구의 것이었고, 그 도구들과 함께 삭제됐다.

    되살리려면 그것이 전이시킬 두 번째 권위가 함께 있어야 한다 — 이 단언이 그 결합을 못박는다.
    """

    for removed in (
        "MigrationStatus",
        "ALLOWED_TRANSITIONS",
        "CUTOVER_PREDECESSOR",
        "ROLLBACK_TARGET",
        "authority_for_status",
        "can_transition",
        "coerce_status",
        "is_blocked",
        "transition",
    ):
        assert not hasattr(audience_authority, removed), (
            f"{removed} 가 되살아났다 — 이행 상태 기계는 legacy 권위 없이는 뜻이 없다."
        )


def test_executor_reads_authority_and_not_expression_presence() -> None:
    """`_has_canonical_audience_authority` 가 다시 페이로드 모양을 읽기 시작하면 red.

    함수 본문이 ``event_expression`` 을 직접 들여다보는 순간 판정자가 둘이 되고, 한쪽만 고치는
    드리프트가 시작된다.
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
