"""실패한 응답은 반드시 **단계**를 갖는다.

``rag/failure_stage._FAILURE_REASON_TO_STAGE`` 에 없는 ``failure_reason`` 은 조용히 ``None`` 이 되고
(rag/failure_stage.py 의 조기 반환), 그러면 프론트는 실패 단계 배지·스텝퍼를 **통째로** 그리지
않는다. 즉 사유는 있는데 "어디서 막혔는지"가 사라진다.

이 파일은 그 표를 두 방향으로 잰다.

- **생산 ⊆ 표**: 생산 가능한 사유가 전부 매핑돼 있는가. 사유는 두 갈래로 생산된다 —
  ``graph_rag.py`` 안의 리터럴(``"failure_reason": "..."``)과, 접두어+상태로 **조립**되는 것들
  (``"plan_validation_" + status``, ``f"semantic_ir_{status}"``, ``"event_compiler_" + status``).
  조립형은 상태 집합을 소유한 모듈에서 곱집합으로 계산한다 — 손으로 적으면 상태가 하나 늘 때
  이 테스트만 옛 목록으로 잰다.
- **표 ⊆ 생산**: 아무도 내지 않는 사유가 표에 남아 있지 않은가(스테일 항목).

런타임 모듈(rag/failure_stage.py)은 이 곱집합을 **직접 계산하지 않는다**. 그 모듈은 순수 모듈
불변식(plain dict 입력만 받는다)을 스스로 선언하고 있어서, plan_validation/semantic_plan/
event_compiler 를 끌어오면 렌더링 계층이 코어 스키마 로딩 실패에 묶인다 — 같은 계층의
``failure_messages`` 가 정확히 그 이유로 ``semantic_plan`` 을 함수 안에서 지연 import 한다.
총체성은 테스트가 강제하고 런타임은 리터럴 표를 유지한다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import event_compiler
import plan_validation
from rag import failure_stage

REPO_ROOT = Path(__file__).resolve().parents[1]


def _declared() -> set[str]:
    return set(failure_stage._FAILURE_REASON_TO_STAGE)


def _literal_reasons(path: Path) -> set[str]:
    """sql_result 모양의 dict 가 리터럴로 내는 ``failure_reason``.

    ``"sql"`` 키를 함께 요구하는 이유: ``failure_reason`` 은 sql_result 만의 어휘가 아니다.
    LLM 호출 결과 dict 도 같은 키를 쓰는데(예: ``missing_openai_api_key``) 그것은 파이프라인
    단계가 아니라 호출 실패다. 단계 표에 넣으면 표가 다른 축을 섞어 담게 된다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        # ① dict 안에 직접 쓴 리터럴.
        if isinstance(node, ast.Dict):
            keys = {key.value for key in node.keys if isinstance(key, ast.Constant)}
            if "sql" not in keys:
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "failure_reason"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value
                ):
                    found.add(value.value)
        # ② 지역 변수에 먼저 담은 뒤 싣는 형태(`failure_reason = "..."`). 이 형태를 세지 않으면
        #    가드가 **부류로** 새어 나간다 — 실측(구현 중): 변수로 담은 사유 하나가 매핑에서
        #    빠져도 이 파일 전체가 green 이었다.
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if not isinstance(node.value.value, str) or not node.value.value:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "failure_reason":
                    found.add(node.value.value)
    return found


def _composed_reasons() -> set[str]:
    """접두어+상태로 조립되는 사유. 상태 집합은 그것을 소유한 모듈에서 읽는다."""
    plan_statuses = {
        status
        for status in plan_validation.PlanValidationStatus.__args__  # type: ignore[attr-defined]
        if status != plan_validation.EXECUTABLE
    }
    capability_statuses = {
        event_compiler.CAPABILITY_UNSUPPORTED,
        event_compiler.CAPABILITY_PARTIALLY_SUPPORTED,
    }
    # failure_messages.semantic_failure_reason 이 f"semantic_ir_{status}" 로 조립하는 상태.
    # 그 함수는 semantic_ir.status 를 그대로 싣고, 게이트는 아래 둘에서만 막는다.
    semantic_statuses = {"needs_clarification", "unsupported"}
    return (
        {f"plan_validation_{status}" for status in plan_statuses}
        | {f"event_compiler_{status}" for status in capability_statuses}
        | {f"semantic_ir_{status}" for status in semantic_statuses}
    )


def test_every_composed_failure_reason_has_a_stage() -> None:
    """조립형 사유는 상태가 하나 늘 때마다 새로 생긴다 — 곱집합 전체가 매핑돼야 한다."""
    missing = sorted(_composed_reasons() - _declared())
    assert not missing, (
        "단계가 없는 조립형 failure_reason:\n  " + "\n  ".join(missing)
        + "\n\n매핑이 없으면 failure_stage=None 이 되어 프론트가 실패 단계를 그리지 않는다."
    )


def test_every_literal_failure_reason_has_a_stage() -> None:
    """graph_rag.py 가 리터럴로 내는 사유도 전부 매핑돼야 한다."""
    missing = sorted(_literal_reasons(REPO_ROOT / "graph_rag.py") - _declared())
    assert not missing, (
        "단계가 없는 리터럴 failure_reason:\n  " + "\n  ".join(missing)
    )


def test_no_stale_stage_mapping() -> None:
    """아무도 내지 않는 사유가 표에 남아 있으면, 그 표는 더 이상 실제 동작을 설명하지 않는다."""
    produced = _composed_reasons()
    for path in REPO_ROOT.glob("*.py"):
        produced |= _literal_reasons(path)
    # 문자열이 다른 형태(변수·상수 참조·집합 원소)로 실리는 경우를 위해 소스 전수 검색으로 한 번 더
    # 본다. 패키지 하위(rag/·query_structurer/ 등)까지 봐야 한다 — 루트만 훑으면 그쪽에서 선언된
    # 사유가 전부 '스테일'로 오보고된다.
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in REPO_ROOT.rglob("*.py")
        if "tests" not in path.parts and ".venv" not in path.parts
    )
    stale = sorted(
        reason
        for reason in _declared() - produced
        if f'"{reason}"' not in sources and f"'{reason}'" not in sources
    )
    assert not stale, "생산자가 없는 스테일 매핑:\n  " + "\n  ".join(stale)


def test_every_mapped_stage_is_a_declared_pipeline_stage() -> None:
    """매핑이 가리키는 단계는 실제 스텝퍼 단계여야 한다(오타 방지)."""
    stages = {stage["code"] for stage in failure_stage._FAILURE_STAGE_SEQUENCE}
    unknown = sorted(set(failure_stage._FAILURE_REASON_TO_STAGE.values()) - stages)
    assert not unknown, "스텝퍼에 없는 단계를 가리키는 매핑:\n  " + "\n  ".join(unknown)


def test_unmapped_reason_still_returns_none_by_contract() -> None:
    """미매핑 시 None 은 **계약**이다 — 예외로 바꾸면 실패 응답이 500 이 된다."""
    assert failure_stage._FAILURE_REASON_TO_STAGE.get("definitely_not_a_reason") is None
