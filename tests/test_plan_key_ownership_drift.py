"""plan 최상위 키의 분류가 여러 목록에 흩어진 채 갈라지지 않는지 지킨다.

plan dict 는 이 시스템의 사실상 IR 인데, "어떤 키가 조건이고 어떤 키가 파생인가"를 최소 다섯 곳이
각자의 목록으로 들고 있다(ir_snapshot 의 DERIVED/KNOWN_CONDITION, plan_decisions 의 NON_CONDITION,
semantic_requirements 의 요구 슬롯 allow-list, graph_rag 의 사건 IR 차단 키). 한 곳만 고치면
같은 키가 곳에 따라 조건이었다가 파생이 되고, 그 결과 조건이 스냅샷·감사·요구 원장에서 조용히 빠진다.

기존 계약 테스트(test_ir_schema_contract)는 **코퍼스가 실제로 만든 키**만 본다. 코퍼스가 안 만드는
키는 분류가 없어도 통과한다. 여기서는 소스를 AST 로 훑어 '대입되는 모든 plan 키'를 인벤토리로
만들어 그 사각지대를 없앤다.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import ir_snapshot  # noqa: E402
import plan_decisions  # noqa: E402
import semantic_requirements  # noqa: E402

# plan 을 담는 변수 이름들(대입 대상 판정용).
PLAN_VARIABLES = {"plan", "query_plan", "llm_plan", "staged_plan", "rules_plan", "payload_plan"}

# 분류 대상이 아닌 키(제어·트레이스 상태). '_' 로 시작하는 것은 관례적으로 내부 상태다.
def _is_internal(key: str) -> bool:
    return key.startswith("_")


def _assigned_plan_keys() -> set[str]:
    """graph_rag 소스에서 plan[...] = ... / plan.setdefault("k", ...) 로 쓰이는 최상위 키."""

    tree = ast.parse((REPO_ROOT / "graph_rag.py").read_text(encoding="utf-8"))
    keys: set[str] = set()

    def record(node: ast.AST) -> None:
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in PLAN_VARIABLES
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                record(target)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            record(node.target)
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"setdefault", "pop"}
                and isinstance(func.value, ast.Name)
                and func.value.id in PLAN_VARIABLES
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)

    return {key for key in keys if not _is_internal(key)}


def _classified_keys() -> set[str]:
    return (
        set(ir_snapshot.DERIVED_PLAN_KEYS)
        | set(ir_snapshot.KNOWN_CONDITION_PLAN_KEYS)
        | set(plan_decisions.NON_CONDITION_PLAN_KEYS)
    )


# 아직 어느 목록에도 없는 키. 하향 전용 래칫 — 늘리려면 사유가 필요하다.
# 2026-08-01 에 13건을 전부 분류해 0 이 됐다. 다시 올리려면 사유가 필요하다.
KNOWN_UNCLASSIFIED_CEILING = 0


def test_inventory_is_not_empty() -> None:
    """AST 스캔이 아무것도 못 찾으면 이 계약 전체가 공허해진다."""

    assert len(_assigned_plan_keys()) > 30, f"인벤토리가 너무 작다: {sorted(_assigned_plan_keys())}"


def test_unclassified_plan_keys_do_not_grow() -> None:
    """분류 없는 키가 늘면 그만큼 스냅샷·감사·요구 원장의 사각지대가 커진다."""

    unclassified = sorted(_assigned_plan_keys() - _classified_keys())
    assert len(unclassified) <= KNOWN_UNCLASSIFIED_CEILING, (
        f"미분류 plan 키가 {len(unclassified)}개로 상한 {KNOWN_UNCLASSIFIED_CEILING} 을 넘었다:\n  "
        + "\n  ".join(unclassified)
        + "\n\n조건/파생/비조건 중 하나로 선언하라."
    )


def test_no_key_is_both_derived_and_non_condition() -> None:
    """같은 분류 사실의 소유자가 둘로 갈리면 한쪽만 고치는 드리프트가 생긴다.

    현재 8건이 겹쳐 있다. 기능상 무해하지만(둘 중 하나만 있어도 판정이 같다) 소유가 갈라져 있다는
    사실 자체가 드리프트 여지다. 플랜 W4-3 에서 단일 소스로 합친다 — 그때까지 개수를 고정한다.
    """

    overlap = sorted(set(ir_snapshot.DERIVED_PLAN_KEYS) & set(plan_decisions.NON_CONDITION_PLAN_KEYS))
    assert len(overlap) <= 8, f"이중소유 키가 늘었다({len(overlap)}건): {overlap}"


def test_requirement_slots_do_not_contradict_the_snapshot_classification() -> None:
    """같은 키를 한쪽은 '사용자 의미 슬롯', 다른 쪽은 '파생'으로 부르면 둘 중 하나는 틀렸다.

    2026-08-01 에 policy_constraints 를 조건으로 통일해 모순을 0 으로 만들었다(업무 정책 카탈로그로
    실체화되지만 촉발은 사용자 어구라, 브랜드명→코드인 dimension_filters 와 같은 성격이다).
    """

    contradictions = sorted(
        set(semantic_requirements._PLAN_REQUIREMENT_SLOTS) & set(ir_snapshot.DERIVED_PLAN_KEYS)
    )
    assert not contradictions, (
        f"의미 슬롯이면서 동시에 파생으로 분류된 키: {contradictions}. "
        "한쪽 분류가 틀렸다 — 어느 쪽인지 정하고 단일 소스로 합쳐라."
    )


def test_every_list_is_non_empty() -> None:
    """목록을 비우면 모든 대조가 통과해버린다 — 퇴화 방지."""

    for name, values in (
        ("ir_snapshot.DERIVED_PLAN_KEYS", ir_snapshot.DERIVED_PLAN_KEYS),
        ("ir_snapshot.KNOWN_CONDITION_PLAN_KEYS", ir_snapshot.KNOWN_CONDITION_PLAN_KEYS),
        ("plan_decisions.NON_CONDITION_PLAN_KEYS", plan_decisions.NON_CONDITION_PLAN_KEYS),
        ("semantic_requirements._PLAN_REQUIREMENT_SLOTS", semantic_requirements._PLAN_REQUIREMENT_SLOTS),
    ):
        assert values, f"{name} 가 비었다."
