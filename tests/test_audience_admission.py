"""`audience_admission` 계약 — "실행 언어가 둘인가"의 판정만 소유한다.

이 파일이 지키는 것은 넷이다.

  ① 경로 표기와 순서가 결정적이다(같은 플랜 → 같은 튜플).
  ② 빈 값 정책이 `plan_validation` 의 지역 판정과 같다 — 특히 `0`/`False` 는 조건이지 빈 값이 아니다.
  ③ 게이트 표면이 **두 컨테이너로 얼려져 있다**(§6-1 미결정 유지). 넓히는 편집이 곧 결정의 기록이다.
  ④ `canonical_event_ir_grounding` 의 동명 술어와 **다른 판정**이며 서로 위임하지 않는다.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_admission  # noqa: E402
import audience_authority  # noqa: E402
import canonical_event_ir_grounding  # noqa: E402
import plan_schema  # noqa: E402
import plan_validation  # noqa: E402

CANONICAL_EXPRESSION = {"type": "exists", "relation": {"type": "source", "name": "purchase"}}


def _imported_repo_modules(relative_path: str) -> set[str]:
    """이 파일이 import 하는 **저장소 최상위 모듈** 이름들(표준 라이브러리 제외)."""
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])
    return imported & {path.stem for path in REPO_ROOT.glob("*.py")}


def _canonical(**extra: object) -> dict[str, object]:
    """권위가 명시 스탬프로 event_ir 인 플랜."""
    plan: dict[str, object] = {audience_authority.PLAN_AUTHORITY_KEY: "event_ir"}
    plan.update(extra)
    return plan


def test_paths_use_the_container_slot_notation_and_are_deterministic() -> None:
    plan = _canonical(
        target_user={"grades": ["VIP"], "gender": "F"},
        exclude={"interests": ["golf"]},
    )
    assert audience_admission.legacy_audience_paths(plan) == (
        "target_user.gender",
        "target_user.grades",
        "exclude.interests",
    )

    # 같은 내용, 다른 삽입 순서 → 같은 결과여야 한다.
    reordered = _canonical(
        target_user={"gender": "F", "grades": ["VIP"]},
        exclude={"interests": ["golf"]},
    )
    assert audience_admission.legacy_audience_paths(reordered) == (
        audience_admission.legacy_audience_paths(plan)
    )


def test_skeleton_empty_slots_are_not_a_surface() -> None:
    plan = _canonical(
        target_user={"grades": [], "gender": None, "nested": {"a": {}, "b": []}},
        exclude={},
    )
    assert audience_admission.legacy_audience_paths(plan) == ()
    assert audience_admission.execution_conflicts(plan) == ()


def test_zero_and_false_are_conditions_not_emptiness() -> None:
    """`0`/`False` 를 빈 값으로 세면 그 조건이 판정에서 조용히 사라진다."""

    assert audience_admission.legacy_audience_paths(
        _canonical(target_user={"min_order_count": 0})
    ) == ("target_user.min_order_count",)
    assert audience_admission.legacy_audience_paths(
        _canonical(target_user={"opted_in": False})
    ) == ("target_user.opted_in",)


def test_non_mapping_container_is_reported_as_the_container_path() -> None:
    assert audience_admission.legacy_audience_paths(
        _canonical(target_user=["grades"])
    ) == ("target_user",)


def test_unstamped_plans_now_conflict_too() -> None:
    """폐쇄 전 이 플랜은 legacy 레인이라 충돌이 아니었다 — 그 면제가 사라졌다.

    권위 미선언(rules 레인·표식 없는 저장 페이로드)이 legacy 로 읽히던 것이 회원 슬롯이
    실행에 닿는 마지막 통로였다. 지금은 같은 플랜이 채워진 표면 수만큼 충돌을 낸다.
    """

    unstamped = {"target_user": {"grades": ["VIP"]}}
    assert [c.path for c in audience_admission.execution_conflicts(unstamped)] == [
        "target_user.grades"
    ]


def test_a_stored_legacy_stamp_is_no_longer_an_escape_hatch() -> None:
    """rollback 탈출구는 폐쇄됐다 — 조용히 통과시키지 않고 어휘 오류로 드러낸다."""

    rollback = {
        audience_authority.PLAN_AUTHORITY_KEY: "legacy",
        audience_authority.EVENT_EXPRESSION_KEY: {
            "expression": CANONICAL_EXPRESSION,
            "source": "audience_requirement",
        },
        "target_user": {"grades": ["VIP"]},
    }
    with pytest.raises(audience_authority.AudienceAuthorityError, match="legacy"):
        audience_admission.execution_conflicts(rollback)


def test_explicit_event_ir_authority_without_any_source_marker_conflicts() -> None:
    """표식이 아니라 **권위**로 판정한다 — 오늘의 리터럴 가드가 놓치는 갈래다."""

    conflicts = audience_admission.execution_conflicts(
        _canonical(target_user={"grades": ["VIP"]})
    )
    assert [(c.code, c.path) for c in conflicts] == [
        (audience_admission.LEGACY_AUDIENCE_CONFLICT_CODE, "target_user.grades")
    ]


def test_declared_canonical_ingress_without_a_stamp_also_conflicts() -> None:
    """스탬프 이전의 canonical 계약도 같은 레인이다(폐쇄 전부터의 계약)."""

    plan = {
        "audience_requirement": {"expression": {"kind": "x"}, "issues": []},
        "target_user": {"gender": "F"},
    }
    assert audience_authority.requires_event_ir(plan) is True
    assert audience_admission.declares_audience(plan) is True
    assert [c.path for c in audience_admission.execution_conflicts(plan)] == ["target_user.gender"]


def test_declares_audience_scopes_the_closure_to_requests_that_have_an_audience() -> None:
    """회원 조건이 0개인 요청(집계·분석)은 폐쇄 범위 밖이다.

    범위를 권위로 잡으면 legacy 레인과 아무 상관 없는 집계 질의까지 "Event IR 표현이 없다"로
    죽는다. 그 경계가 여기서 얼어 있다.
    """

    assert audience_admission.declares_audience({}) is False
    assert audience_admission.declares_audience({"aggregation_request": {"x": 1}}) is False
    assert audience_admission.declares_audience({"target_user": {}}) is False
    assert audience_admission.declares_audience({"target_user": {"gender": "F"}}) is True
    assert audience_admission.declares_audience({"exclude": {"interests": ["golf"]}}) is True
    assert audience_admission.declares_audience(
        {"audience_requirement": {"expression": None, "issues": []}}
    ) is True


def test_both_containers_yield_one_conflict_per_slot() -> None:
    conflicts = audience_admission.execution_conflicts(
        _canonical(target_user={"grades": ["VIP"]}, exclude={"interests": ["golf"]})
    )
    assert [c.path for c in conflicts] == ["target_user.grades", "exclude.interests"]


def test_admission_surface_is_the_two_containers_and_widening_is_a_decision() -> None:
    """표면 확대는 결정이다(docs/plans_event_ir_only.md §6-1). 이 테스트가 지금 범위를 얼린다."""

    for key in plan_schema.audience_keys():
        plan = _canonical(**{key: [{"x": 1}]})
        assert audience_admission.execution_conflicts(plan) == (), (
            f"{key} 가 입장 판정 표면에 들어왔다 — §6-1 결정 없이 넓히면 그 키를 가진 "
            "canonical 요청이 fail-close 된다."
        )

    # ``semantic_plan`` 노드가 입장 판정 표면 밖이라는 단언은 2026-08-05 삭제됐다 — 그 키
    # 자체가 plan 계약에서 폐기되어 지킬 계약이 없다(tests/test_plan_schema_registry.py 가
    # "되살아나지 않았는가"를 대신 지킨다).

    for container in plan_schema.AUDIENCE_CONTAINERS:
        assert audience_admission.execution_conflicts(_canonical(**{container: {"slot": ["v"]}}))


def test_admission_and_grounding_are_different_predicates_and_neither_delegates() -> None:
    """같은 이름의 다른 술어를 위임으로 묶으면 둘 중 하나가 조용히 바뀐다."""

    only_top_level_key = _canonical(set_expressions=[{"x": 1}])
    assert canonical_event_ir_grounding.has_empty_legacy_audience_surface(only_top_level_key) is False
    assert audience_admission.legacy_audience_paths(only_top_level_key) == ()

    # 공유 표면(컨테이너)에서는 두 술어가 일치한다.
    shared = _canonical(target_user={"grades": ["VIP"]})
    assert canonical_event_ir_grounding.has_empty_legacy_audience_surface(shared) is False
    assert audience_admission.legacy_audience_paths(shared) != ()

    # 산문에서 서로를 **설명**하는 것은 필요하다(두 술어의 차이가 이 파일들의 요점이다).
    # 금지되는 것은 **import**, 즉 한쪽이 다른 쪽의 판정을 실제로 끌어다 쓰는 것이다.
    assert "canonical_event_ir_grounding" not in _imported_repo_modules("audience_admission.py")
    assert "audience_admission" not in _imported_repo_modules("canonical_event_ir_grounding.py")


def test_module_imports_only_plan_schema_authority_and_stdlib() -> None:
    """순수 모듈 규약 — 실행 지식을 끌어오면 plan_validation 방향의 import 가 막힌다."""

    imported = _imported_repo_modules("audience_admission.py")
    assert imported == {"plan_schema", "audience_authority"}, (
        f"허용되지 않은 저장소 모듈 import: {sorted(imported - {'plan_schema', 'audience_authority'})}"
    )


def test_surface_names_are_derived_not_literal() -> None:
    """컨테이너 이름을 리터럴로 적으면 소유자가 다시 둘이 된다."""

    source = (REPO_ROOT / "audience_admission.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # 모듈 docstring 의 설명문은 제외
    for literal in ('"target_user"', "'target_user'", '"exclude"', "'exclude'"):
        assert literal not in body, f"컨테이너 이름 리터럴이 코드에 있다: {literal}"


def test_conflict_maps_onto_a_plan_validation_issue_without_renaming_anything() -> None:
    """3-4 가 이 conflict 를 issue 로 옮길 때 코드·경로가 보존되는지 **실제 값으로** 확인한다."""

    plan = _canonical(target_user={"gender": "F"})
    conflicts = audience_admission.execution_conflicts(plan)
    assert conflicts, "픽스처가 충돌을 만들지 못하면 아래 매핑은 아무것도 재지 않는다."

    conflict = conflicts[0]
    issue = plan_validation._issue(plan_validation.INTERNAL_INVALID, conflict.code, conflict.path)
    assert issue.code == audience_admission.LEGACY_AUDIENCE_CONFLICT_CODE
    assert issue.path == conflict.path
    assert issue.path in audience_admission.legacy_audience_paths(plan)
    assert issue.status == plan_validation.INTERNAL_INVALID

    # status 를 코드에서 파생시키면 안 된다는 사실의 반증기: 파생하면 semantic_conflict 로 뒤집힌다.
    assert plan_validation._status_for_validation_code(conflict.code) != plan_validation.INTERNAL_INVALID
