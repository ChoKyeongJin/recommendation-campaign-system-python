"""SQL 빌더 **순서** 계약(축 E).

`tests/test_capability_contract.py` 가 "누가 어떤 조건을 소유하는가"(축 C)를 본다면, 이 파일은
"누가 먼저 시도되는가"를 본다. dispatch 가 우선순위 튜플을 돌며 **첫 non-None 후보**를 채택하므로
순서는 스타일이 아니라 의미다 — 순서를 바꾸면 같은 프롬프트에서 다른 SQL 이 나간다.

왜 새로 쓰는가(복원이 아니라):
  - 이 축을 지키던 유일한 테스트(`test_builder_order_preserved`)는 ce39f68 일괄 삭제로 사라졌고
    되살리지 않기로 결정돼 있다(plans_ir_decoupling W1-2). W1-4 는 가드를 **신규 작성**하라고 한다.
  - 삭제 이후 순서 지식은 레지스트리 주석 8줄이 전부였는데, 그중 하나('analytical 가장 먼저')는
    이미 사실과 달랐다(실제 3번째). **검증되지 않는 주석은 문서가 아니라 소문이다.**

이 파일이 막는 것:
  1. 선행 제약 위반(빌더를 재배치했는데 신호를 가로채게 됨).
  2. 스테일 제약(빌더는 사라졌는데 제약만 남음).
  3. 종단 폴백이 앞으로 이동(뒤 빌더가 영영 시도되지 않음).
  4. 제약에 사유가 없어짐(다음 사람이 안전하게 못 고침).
  5. import 시점 하드 게이트의 무력화(테스트만 지우면 통과하는 상태로의 회귀).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import capability_validation  # noqa: E402
import graph_rag  # noqa: E402


def _registry():
    return graph_rag._sql_target_builder_registry()


def _names() -> list[str]:
    return [getattr(builder, "__name__", str(builder)) for builder, _kinds in _registry()]


def test_live_registry_satisfies_every_declared_precedence() -> None:
    assert capability_validation.builder_order_issues(_registry()) == []


def test_precedence_declarations_are_not_empty() -> None:
    """제약이 0개면 이 가드는 아무것도 지키지 않는다."""
    assert len(capability_validation.BUILDER_PRECEDENCE) >= 5


def test_every_precedence_states_why() -> None:
    """사유 없는 순서 제약은 다음 사람이 고칠 수 없다 — 삭제도 유지도 근거가 없어진다."""
    for earlier, later, reason in capability_validation.BUILDER_PRECEDENCE:
        assert reason.strip(), f"{earlier} → {later} 제약에 사유가 없다."
        assert len(reason) > 30, (
            f"{earlier} → {later} 사유가 너무 짧다: {reason!r} — "
            "'무엇이 조용히 틀리는가'를 적어야 한다."
        )


def test_precedence_targets_exist_in_the_registry() -> None:
    names = set(_names())
    for earlier, later, _reason in capability_validation.BUILDER_PRECEDENCE:
        assert earlier in names, f"스테일 제약: {earlier} 가 레지스트리에 없다."
        assert later in names, f"스테일 제약: {later} 가 레지스트리에 없다."


def test_terminal_fallback_is_last() -> None:
    names = _names()
    assert names[-1] == capability_validation.TERMINAL_BUILDER_NAME


def test_no_builder_is_registered_twice() -> None:
    names = _names()
    assert len(names) == len(set(names)), "같은 빌더가 두 번 시도된다."


def test_wrapping_preserves_the_names_the_contract_matches_on() -> None:
    """admission guard 가 빌더를 감싼 뒤에도 __name__ 이 유지돼야 계약이 공허해지지 않는다."""
    for name in _names():
        assert name.startswith(("build_", "_build_")), (
            f"빌더 이름이 감싸기 과정에서 뭉개졌다: {name!r} — "
            "이름이 사라지면 순서/소유권 계약이 조용히 통과한다."
        )


# ── 검증기 자체가 실제로 잡는지(가드가 공허하지 않은지) ──────────────────────────────────


def _fake(name: str):
    def builder(query_plan):  # pragma: no cover - 호출되지 않는다
        return None

    builder.__name__ = name
    return builder


def test_violation_is_detected() -> None:
    earlier, later, _reason = capability_validation.BUILDER_PRECEDENCE[0]
    swapped = [(_fake(later), frozenset()), (_fake(earlier), frozenset())]
    issues = capability_validation.builder_order_issues(swapped)
    assert any(issue.startswith("precedence_violated") for issue in issues), issues


def test_missing_builder_is_detected_as_stale_constraint() -> None:
    issues = capability_validation.builder_order_issues([(_fake("build_unrelated"), frozenset())])
    assert any(issue.startswith("precedence_builder_missing") for issue in issues), issues


def test_terminal_builder_out_of_place_is_detected() -> None:
    registry = [
        (_fake(capability_validation.TERMINAL_BUILDER_NAME), frozenset()),
        (_fake("build_something_else"), frozenset()),
    ]
    issues = capability_validation.builder_order_issues(registry)
    assert any(issue.startswith("terminal_builder_not_last") for issue in issues), issues


def test_duplicate_registration_is_detected() -> None:
    registry = [(_fake("build_dup"), frozenset()), (_fake("build_dup"), frozenset())]
    issues = capability_validation.builder_order_issues(registry)
    assert any(issue.startswith("builder_registered_twice") for issue in issues), issues


# ── 삭제 내성: 기동 경로에 하드 게이트가 살아 있는가 ────────────────────────────────────


def test_import_time_gate_exists_and_raises() -> None:
    """계약 테스트는 두 번 일괄 삭제됐다 — 마지막 방어선은 기동 경로 안에 있어야 한다."""
    with pytest.raises(capability_validation.BuilderContractError):
        capability_validation.enforce_builder_contracts(
            [
                (_fake("build_member_targets_sql_candidate"), frozenset()),
                (_fake("build_something_else"), frozenset()),
            ]
        )


def test_install_hook_calls_the_gate() -> None:
    """graph_rag 의 설치 훅이 실제로 enforce 를 호출하는지(배선 삭제 감지)."""
    import inspect

    source = inspect.getsource(graph_rag._install_sql_builder_admission_guards)
    assert "enforce_builder_contracts" in source, (
        "설치 훅에서 계약 강제 호출이 사라졌다 — 테스트를 지우면 통과하는 상태로 되돌아간다."
    )


def test_live_registry_passes_the_gate() -> None:
    capability_validation.enforce_builder_contracts(_registry())


# ── 순서가 선언에서 **파생 가능**한가(제약이 현재 순서를 설명하는가) ────────────────────


def test_declared_precedence_derives_the_live_order() -> None:
    """선언된 제약으로 위상정렬하면 현재 튜플 순서가 그대로 나와야 한다.

    나오지 않으면 둘 중 하나다: 제약이 현재 순서와 **모순**이거나, 현재 순서에 제약으로
    설명되지 않는 의도가 숨어 있다. 어느 쪽이든 다음 사람이 재배치할 때 근거가 없다.
    """
    names = _names()
    assert capability_validation.derive_builder_order(names) == names


def test_precedence_graph_is_acyclic() -> None:
    """순환이면 '먼저'가 성립하지 않는다 — 위상정렬이 예외로 알려준다."""
    capability_validation.derive_builder_order(_names())  # 순환이면 BuilderContractError


def test_derivation_detects_a_contradicting_order() -> None:
    """가드가 공허하지 않은지: 제약을 어긴 순서를 넣으면 파생 결과가 달라진다."""
    names = _names()
    earlier, later, _reason = capability_validation.BUILDER_PRECEDENCE[0]
    swapped = list(names)
    i, j = swapped.index(earlier), swapped.index(later)
    swapped[i], swapped[j] = swapped[j], swapped[i]
    assert capability_validation.derive_builder_order(swapped) != swapped


# ── 배타 라우팅이 선언 소유인가 ──────────────────────────────────────────────────────


def test_exclusive_routes_are_declared_with_reasons() -> None:
    for slot, builder, reason in capability_validation.EXCLUSIVE_ROUTES:
        assert slot and builder
        assert len(reason) > 30, f"{slot} 배타 라우팅에 사유가 없다: {reason!r}"
        assert builder in _names(), f"배타 라우팅이 없는 빌더를 가리킨다: {builder}"


def test_exclusive_route_lookup() -> None:
    slot, builder, _reason = capability_validation.EXCLUSIVE_ROUTES[0]
    assert capability_validation.exclusive_route_for({slot}) == builder
    assert capability_validation.exclusive_route_for({"target_user"}) is None
    assert capability_validation.exclusive_route_for(set()) is None


def test_dispatch_consumes_the_declaration_not_an_inline_branch() -> None:
    """dispatch 가 선언을 읽는지(하드코딩 if 로 되돌아가지 않았는지)."""
    import inspect

    source = inspect.getsource(graph_rag._compile_sql_template_candidate_validated)
    assert "exclusive_route_for" in source, (
        "배타 라우팅이 인라인 분기로 되돌아갔다 — 예외의 사유가 다시 코드에 묻힌다."
    )
