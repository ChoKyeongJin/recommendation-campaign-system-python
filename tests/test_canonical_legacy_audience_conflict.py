"""`canonical_legacy_audience_conflict` 계약 — 권위가 Event IR 이면 legacy 표면은 실행 불가다.

이 사유코드는 오늘까지 저장소 전체에서 **정의 1줄, 테스트 0건**이었다. 그래서 그 판정이
무엇을 잡고 무엇을 놓치는지 아무도 재지 않았고, 실제로 `event_expression.source` 표식이 없는
페이로드는 권위가 Event IR 이어도 통과했다(Phase 3-4 착수 전 실측).

이 파일이 고정하는 것 넷:

  ① 표식(`event_expression.source`) 유무와 **무관하게** 차단된다 — 판정 기준은 권위다.
  ② canonical 계약으로 들어왔지만 아직 스탬프가 없는 플랜도 같은 레인이다
     (`requires_event_ir` 선택의 기록. `executes_event_ir` 였다면 이 갈래가 샌다).
  ③ 명시 `legacy` 스탬프는 **의도한 탈출구**다(rollback). 이것이 "의도한 완화"와 "실수로 뚫린
     구멍"을 가른다.
  ④ 사용자 문구에 내부 슬롯명이 실리지 않는다(§6-6 계약). 좌표는 운영자 채널에만 남는다.

**대가**: cut-over 산출 플랜(표현 + 보존된 legacy 슬롯 + Event IR 권위)은 이제 실행 불가다.
그 모양을 실행 가능으로 읽던 계약이 `tests/test_cutover_legacy_audience_cli.py` 에 있었고 같은
커밋에서 반전했다. 근거는 §6-3 결정(저장 자산 0행, 소유자가 cut-over 를 쓰지 않기로 함)이며,
결정이 뒤집히면 그 파일과 `tests/test_cutover_execution_isolation.py` 가 먼저 red 가 된다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_admission  # noqa: E402
import audience_authority  # noqa: E402
import event_ir  # noqa: E402
import failure_messages  # noqa: E402
import graph_rag  # noqa: E402
import plan_validation  # noqa: E402

CODE = audience_admission.LEGACY_AUDIENCE_CONFLICT_CODE


def _expression() -> dict[str, Any]:
    return event_ir.Not(operand=event_ir.Exists(relation=event_ir.Source(name="purchase"))).to_dict()


def _plan(*, source: str | None, authority: str | None = "event_ir", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"expression": _expression(), "receipts": []}
    if source is not None:
        payload["source"] = source
    plan: dict[str, Any] = {
        "intent": "find_user_segment",
        audience_authority.EVENT_EXPRESSION_KEY: payload,
        "target_user": {"gender": "female"},
    }
    if authority is not None:
        plan[audience_authority.PLAN_AUTHORITY_KEY] = authority
    plan.update(extra)
    return plan


def _codes(plan: dict[str, Any]) -> list[str]:
    return [issue.code for issue in plan_validation.validate_executable_plan(dict(plan)).issues]


def test_conflict_is_named_without_any_source_marker() -> None:
    """표식 밖(그리고 표식 없음)에서도 잡힌다 — 교체 전에는 둘 다 executable 이었다."""

    for source in ("legacy_migration", None):
        plan = _plan(source=source)
        result = plan_validation.validate_executable_plan(dict(plan))
        assert result.status == plan_validation.INTERNAL_INVALID, source
        assert CODE in [issue.code for issue in result.issues], source


def test_conflict_is_still_named_with_the_canonical_marker() -> None:
    """옛 리터럴 가드가 잡던 모양은 계속 잡힌다(판정 축소가 아니다)."""

    for source in audience_authority.CANONICAL_EVENT_EXPRESSION_SOURCES:
        assert CODE in _codes(_plan(source=source, authority=None)), source


def test_declared_canonical_ingress_without_a_stamp_also_conflicts() -> None:
    """스탬프 이전 canonical 계약도 같은 레인 — `requires_event_ir` 을 고른 이유의 기록."""

    plan = {
        "intent": "find_user_segment",
        "audience_requirement": {"expression": {"kind": "exists"}, "issues": []},
        "target_user": {"gender": "female"},
    }
    assert audience_authority.requires_event_ir(plan) is True
    assert audience_authority.executes_event_ir(plan) is False
    assert CODE in _codes(plan)


def test_explicit_legacy_authority_is_the_rollback_escape_hatch() -> None:
    """명시 legacy 는 통과한다. 이 테스트가 '의도한 완화'와 '뚫린 구멍'을 가른다."""

    plan = _plan(source="audience_requirement", authority="legacy")
    result = plan_validation.validate_executable_plan(dict(plan))
    assert CODE not in [issue.code for issue in result.issues]
    assert result.status == plan_validation.EXECUTABLE


def test_gate_surface_is_exactly_the_declared_containers() -> None:
    """표면 확대는 §6-1 결정이다 — 최상위 조건 키는 이 코드를 내지 않는다."""

    import plan_schema

    for container in plan_schema.AUDIENCE_CONTAINERS:
        plan = _plan(source="legacy_migration")
        plan.pop("target_user")
        plan[container] = {"grades": ["VIP"]}
        assert CODE in _codes(plan), container

    for key in plan_schema.audience_keys():
        plan = _plan(source="legacy_migration")
        plan.pop("target_user")
        plan[key] = [{"x": 1}]
        assert CODE not in _codes(plan), key


def test_both_containers_yield_one_issue_per_slot_with_slot_paths() -> None:
    plan = _plan(source="legacy_migration", exclude={"interests": ["golf"]})
    result = plan_validation.validate_executable_plan(dict(plan))
    conflicts = [issue for issue in result.issues if issue.code == CODE]
    assert [issue.path for issue in conflicts] == ["target_user.gender", "exclude.interests"]


def test_an_already_blocked_plan_gains_an_issue_but_keeps_its_status() -> None:
    """표현이 없어 이미 막힌 플랜에 코드가 하나 더 붙되 status 는 그대로다."""

    plan = {
        "intent": "find_user_segment",
        audience_authority.PLAN_AUTHORITY_KEY: "event_ir",
        "target_user": {"gender": "female"},
    }
    result = plan_validation.validate_executable_plan(dict(plan))
    assert result.status == plan_validation.INTERNAL_INVALID
    assert set(result.issues and [issue.code for issue in result.issues]) == {
        CODE,
        "canonical_event_expression_missing",
    }


def test_conflict_blocks_on_the_response_path_and_on_a_direct_builder_call() -> None:
    plan = _plan(source="legacy_migration")
    assert graph_rag.build_event_expression_sql_candidate(dict(plan)) is None
    assert graph_rag.build_member_targets_sql_candidate(dict(plan)) is None


def test_user_facing_message_does_not_leak_internal_slot_names() -> None:
    """§6-6 계약 — 좌표는 운영자 채널에만 남는다."""

    result = plan_validation.validate_executable_plan(dict(_plan(source="legacy_migration")))
    issue = next(item for item in result.issues if item.code == CODE)
    assert issue.path == "target_user.gender", "운영자 좌표는 슬롯 단위로 남아야 한다."

    message = failure_messages.plan_validation_issue_ko(issue)
    assert "target_user" not in message
    assert CODE in message


def test_plan_validation_holds_no_canonical_source_literal() -> None:
    """리터럴 사본이 되살아나면 red."""

    source = (REPO_ROOT / "plan_validation.py").read_text(encoding="utf-8")
    assert '"audience_requirement"' not in source or '"semantic_plan"' not in source, (
        "plan_validation 이 canonical source 리터럴 집합을 다시 들고 있다."
    )
    assert "audience_admission" in source
