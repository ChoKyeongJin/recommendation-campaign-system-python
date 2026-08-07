"""권위가 Event IR 이면 legacy SQL 빌더는 **한 줄도** 실행되지 않는다(Phase 3-5 회귀).

이 계약은 이미 구현돼 있다 — `graph_rag._admitted_sql_builder` 가 등록된 하위 빌더를 import
시점에 감싸고, `audience_authority.requires_event_ir(plan)` 이면 배타 라우팅으로 선언된 빌더
하나를 제외한 전부를 `None` 으로 만든다. 계획 문서(docs/plans_event_ir_only.md Phase 3-5)는 이걸
"앞으로 할 일"로 적고 있었는데 실측해 보니 이미 참이었다. **테스트가 없는 참은 다음 정리
커밋에서 사라진다** — 이 저장소는 계약 테스트가 일괄 정리로 두 번 삭제된 전력이 있다
(capability_validation.py 의 주석 참조). 그래서 사후에 계약을 고정한다.

픽스처 규약(어기면 이 파일은 다른 게이트를 재게 된다):

  ① `event_expression.source` 는 :data:`audience_authority.CANONICAL_EVENT_EXPRESSION_SOURCES`
     **밖**이어야 한다. 안쪽 값이면 `plan_validation` 의 hybrid 가드가 먼저 걸려 플랜이
     EXECUTABLE 이 아니게 되고, 그러면 빌더가 `None` 을 돌려준 이유가 권위 게이트가 아니라
     검증 게이트가 된다.
  ② 그래서 canonical 팔의 권위는 **명시 스탬프 한 줄에만** 매달려 있다(source 가 canonical 밖이라
     호환 판독으로는 LEGACY 다). 픽스처가 조용히 legacy 로 미끄러지면 이 파일 전체가 "아무것도
     안 재면서 초록"이 되므로 모든 테스트에 `requires_event_ir` 위생 단언을 넣는다.
  ③ 면제 빌더 이름을 리터럴로 적지 않는다. 저장소에 이미 그 이름의 사본이 셋이다(레지스트리 /
     admission 가드 / 배타 라우팅 선언). 여기서는 선언(capability_validation)에서 파생한다.

**Phase 3-4 이후의 수명**: 3-4 가 랜딩하면 "권위 event_ir + legacy 표면"이 검증 단계에서 먼저
막히므로, 아래 ②③번 테스트의 픽스처는 EXECUTABLE 이 아니게 되고 두 테스트는 *다른 이유로도*
통과하게 된다(= 계약이 약해진다). 그때에도 계약을 온전히 소유하는 것은 ①번
`test_authority_gate_short_circuits_before_plan_validation` 이다 — 그 테스트는 plan_validation 을
sentinel 로 갈아끼워 검증 게이트를 아예 배제하기 때문이다. 3-4 커밋에서 ②③을 손볼 때 이 문단을
근거로 삼되, ①은 건드리지 마라.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_authority  # noqa: E402
import capability_validation  # noqa: E402
import event_ir  # noqa: E402
import graph_rag  # noqa: E402
import plan_validation  # noqa: E402

# 배타 라우팅 선언에서 파생한다(capability_validation.EXCLUSIVE_ROUTES). 리터럴로 적으면
# 그 이름의 네 번째 사본이 되고, 라우팅이 바뀌는 날 이 파일만 옛 이름을 붙들게 된다.
EXEMPT_BUILDER = capability_validation.exclusive_route_for(
    {audience_authority.EVENT_EXPRESSION_KEY}
)

# canonical 집합 밖의 표식(픽스처 규약 ①). 이행 어댑터가 쓰는 실제 값이다.
NON_CANONICAL_SOURCE = "legacy_migration"


def _expression() -> dict[str, Any]:
    return event_ir.Not(operand=event_ir.Exists(relation=event_ir.Source(name="purchase"))).to_dict()


def _plan(authority: str) -> dict[str, Any]:
    """권위만 다른 **같은** 플랜. 두 팔의 차이가 권위 하나임을 픽스처로 보인다.

    target_user.gender 를 쓰는 이유: legacy 팔에서 `build_member_targets_sql_candidate` 가 실제로
    SQL 을 내는 슬롯이어야 대조군이 성립한다(behaviors/grades 만으로는 legacy 팔도 None 이라
    "권위 때문에 None" 인지 "원래 None" 인지 갈리지 않는다 — 실측으로 확인했다).
    """
    return {
        "intent": "find_user_segment",
        audience_authority.PLAN_AUTHORITY_KEY: authority,
        audience_authority.EVENT_EXPRESSION_KEY: {
            "expression": _expression(),
            "source": NON_CANONICAL_SOURCE,
            "receipts": [],
        },
        "target_user": {"gender": "female"},
    }


def _registered_builders() -> list[Any]:
    return [builder for builder, _kinds in graph_rag._sql_target_builder_registry()]


def _non_exempt_builders() -> list[Any]:
    return [
        builder
        for builder in _registered_builders()
        if getattr(builder, "__name__", "") != EXEMPT_BUILDER
    ]


def test_the_exempt_builder_is_declared_not_guessed() -> None:
    """공허한 통과 방지 — 파생이 실패해 None 이면 아래 필터가 전부를 '비면제'로 세거나 그 반대다."""

    assert EXEMPT_BUILDER == "build_event_expression_sql_candidate"
    assert _registered_builders(), "빌더 레지스트리가 비었다 — 아래 계약이 아무것도 재지 않는다."
    assert _non_exempt_builders(), "비면제 빌더가 0개다 — 차단 계약이 공허해진다."


def test_authority_gate_short_circuits_before_plan_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """권위 차단은 **플랜 검증보다 앞**이다 — 이 파일에서 계약을 영구히 소유하는 테스트.

    plan_validation 을 폭발하는 sentinel 로 갈아끼우면, 검증까지 내려간 빌더만 예외를 일으킨다.
    권위 게이트가 사라지면 비면제 빌더 전부가 sentinel 에 닿는다. 이 테스트는 플랜이 EXECUTABLE
    인지와 **무관**하므로 Phase 3-4 가 랜딩해도 의미가 그대로 남는다.
    """

    plan = _plan("event_ir")
    assert audience_authority.requires_event_ir(plan) is True

    reached: list[str] = []

    def sentinel(query_plan: dict[str, Any]) -> Any:
        reached.append("validate")
        raise AssertionError("권위 게이트가 검증 단계까지 통과시켰다")

    monkeypatch.setattr(plan_validation, "validate_executable_plan", sentinel)
    monkeypatch.setattr(graph_rag.plan_validation, "validate_executable_plan", sentinel)

    for builder in _non_exempt_builders():
        assert builder(copy.deepcopy(plan)) is None, (
            f"{builder.__name__} 이 canonical 권위에서 후보를 냈다."
        )
    assert reached == [], "비면제 빌더가 플랜 검증까지 내려갔다."

    exempt = next(
        builder
        for builder in _registered_builders()
        if getattr(builder, "__name__", "") == EXEMPT_BUILDER
    )
    with pytest.raises(AssertionError):
        exempt(copy.deepcopy(plan))
    assert reached == ["validate"], "면제 빌더는 검증 게이트를 거쳐야 한다."


def test_canonical_authority_blocks_every_non_exempt_builder_and_leaves_the_plan_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """검증이 **통과한다고 쳐도** 비면제 빌더는 전부 None 이고, 플랜은 바이트 동일하다.

    초판은 픽스처가 실제로 EXECUTABLE 인지를 위생 단언으로 확인했는데, Phase 3-4 가 랜딩하면서
    같은 플랜이 `canonical_legacy_audience_conflict` 로 막히게 됐다(그 단언이 정확히 그것을
    잡았다). 검증 결과를 EXECUTABLE 로 고정해 **검증 게이트를 변수에서 제거**한다 — 그러면 남는
    None 의 원인은 권위 게이트 하나뿐이고, 이 테스트는 3-4 이후에도 같은 것을 잰다.
    """

    plan = _plan("event_ir")
    assert audience_authority.requires_event_ir(plan) is True

    executable = plan_validation.PlanValidationResult(
        status=plan_validation.EXECUTABLE,
        issues=(),
        blocking_claim_ids=(),
        unresolved_span_ids=(),
    )
    monkeypatch.setattr(
        graph_rag.plan_validation, "validate_executable_plan", lambda _plan: executable
    )

    before = copy.deepcopy(plan)
    for builder in _non_exempt_builders():
        assert builder(plan) is None, f"{builder.__name__} 이 canonical 권위에서 후보를 냈다."
    assert plan == before, "차단된 빌더가 플랜을 변형했다(부작용 없는 차단이어야 한다)."


def test_the_same_plan_builds_member_sql_once_the_gate_is_lifted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대조군 — 위의 None 이 픽스처 무능이 아니라 **게이트**에서 온다는 증거.

    폐쇄 전에는 이 대조군을 ``audience_authority: "legacy"`` 로 만들었다. 그 권위 값이
    어휘에서 사라졌으므로(2026-08-07), 같은 것을 재려면 게이트 술어 자체를 끄는 수밖에 없다.
    검증 결과도 EXECUTABLE 로 고정해 남는 변수를 게이트 하나로 줄인다 — 그러지 않으면
    채워진 ``target_user`` 가 `canonical_legacy_audience_conflict` 로 먼저 막혀서, 이 테스트가
    게이트가 아니라 검증을 재게 된다.
    """

    plan = _plan("event_ir")
    executable = plan_validation.PlanValidationResult(
        status=plan_validation.EXECUTABLE,
        issues=(),
        blocking_claim_ids=(),
        unresolved_span_ids=(),
    )
    monkeypatch.setattr(
        graph_rag.plan_validation, "validate_executable_plan", lambda _plan: executable
    )
    monkeypatch.setattr(
        graph_rag.audience_admission, "declares_audience", lambda _plan: False
    )

    candidate = graph_rag.build_member_targets_sql_candidate(copy.deepcopy(plan))
    assert candidate is not None, "게이트를 끄면 회원 빌더는 여전히 SQL 을 낸다(코드는 살아 있다)."
    assert candidate["id"] == "sql_template:member_targets"
    assert "GENDER_CD" in candidate["sql"]


def test_every_registered_builder_actually_carries_the_admission_wrapper() -> None:
    """감싸지지 않은 빌더가 하나라도 있으면 그 빌더는 권위를 무시한다."""

    def probe(query_plan: dict[str, Any]) -> None:
        return None

    wrapper_code = graph_rag._admitted_sql_builder(probe).__code__

    unwrapped = [
        getattr(builder, "__name__", repr(builder))
        for builder in _registered_builders()
        if builder.__code__ is not wrapper_code
    ]
    assert not unwrapped, (
        f"admission 래퍼가 없는 빌더: {unwrapped}. "
        "_install_sql_builder_admission_guards 의 globals() 동일성 조건에서 빠졌을 수 있다."
    )

    # 모듈 전역에 감싸지지 않은 원본이 별칭으로 남아 있으면 그쪽으로 우회할 수 있다.
    registered = {getattr(builder, "__name__", "") for builder in _registered_builders()}
    aliases = [
        name
        for name, value in vars(graph_rag).items()
        if callable(value)
        and getattr(value, "__name__", "") in registered
        and getattr(value, "__code__", None) is not wrapper_code
    ]
    assert not aliases, f"감싸지지 않은 원본 별칭이 전역에 있다: {aliases}"


def test_wrapping_is_idempotent() -> None:
    """이미 감싼 빌더를 다시 감싸면 같은 객체다 — 이중 래핑이면 검증이 두 번 돈다."""

    builder = _registered_builders()[0]
    assert graph_rag._admitted_sql_builder(builder) is builder
    assert getattr(builder, "_requires_plan_validation", False) is True
