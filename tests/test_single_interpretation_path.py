"""단일 해석 경로 드리프트 가드 — 삭제된 이중 해석 구조가 이름만 바꿔 돌아오지 못하게.

2026-08-02 이행의 계약을 코드베이스 전수 검사로 고정한다:

  1. 원문을 정규식으로 해석해 query_plan 슬롯을 채우는 코드: 0건
  2. fill-if-empty 백필 호출: 0건
  3. missing_fields 사후 삭제 코드: 0건
  4. LLM 과 결정론 파서의 이중 최종 해석 경로: 0건

'이름만 바꾼 부활'을 막는 것이 요점이다 — 함수명이 아니라 **구조**를 본다:
컴파일러 소유 슬롯에 대입하는 모듈이 legacy_plan_compiler / semantic_pipeline 뿐인가,
semantic_ir 의 missing_fields/status 를 쓰는 곳이 파생 함수 하나뿐인가.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import legacy_plan_compiler  # noqa: E402
import semantic_plan  # noqa: E402

# 검사 대상: 저장소 루트 + query_structurer 패키지의 프로덕션 소스.
SOURCES = sorted(
    [path for path in REPO_ROOT.glob("*.py")]
    + [path for path in (REPO_ROOT / "query_structurer").glob("*.py")]
    + [path for path in (REPO_ROOT / "rag").glob("*.py")]
)

# 슬롯을 쓸 자격이 있는 모듈(컴파일러와 그 배선). 그 외 모듈이 쓰면 이중 생산자다.
SLOT_WRITERS = {"legacy_plan_compiler.py", "semantic_pipeline.py"}

# 파생 semantic_ir 을 만드는 유일한 지점.
SEMANTIC_IR_PRODUCERS = {"semantic_pipeline.py", "campaign_plan_v4.py", "semantic_ir.py"}


def _module_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _executable_strings(tree: ast.AST) -> set[str]:
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }


# ── ① 삭제된 모듈·함수가 되살아나지 않았다 ──────────────────────────────────────
DELETED_MODULES = ("numeric_condition_backfill", "campaign_condition_backfill", "slot_reemission")
DELETED_FUNCTIONS = (
    "_drop_trend_owned_missing_fields",
    "_drop_history_owned_missing_fields",
    "_drop_fabricated_purchase_period_fields",
    "_drop_campaign_constraint_requirements",
    "drop_satisfied_missing_fields",
    "drop_capability_owned_missing_fields",
    "drop_entity_set_owned_missing_fields",
    "apply_profile_condition_backfill",
    "apply_same_product_co_purchase_backfill",
    "apply_member_attribute_history_backfill",
    "detect_member_attribute_history",
    "detect_profile_conditions",
    "parse_entity_set_condition",
    "_apply_entity_set_backfill",
)


def test_deleted_modules_have_no_importers() -> None:
    for path in SOURCES:
        tree = ast.parse(_module_text(path))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        stray = imported & set(DELETED_MODULES)
        assert not stray, f"{path.name} 이 삭제된 백필 모듈을 import 한다: {sorted(stray)}"


def test_deleted_functions_are_not_defined_anywhere() -> None:
    revived: list[str] = []
    for path in SOURCES:
        tree = ast.parse(_module_text(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in DELETED_FUNCTIONS:
                revived.append(f"{path.name}:{node.name}")
    assert not revived, f"삭제된 결정론 파서/sweep 이 되살아났다: {revived}"


# ── ② 컴파일러 소유 슬롯의 단일 생산자 ──────────────────────────────────────────
def test_only_the_compiler_writes_compiler_owned_slots() -> None:
    """`x["cart_aggregate"] = ...` 형태의 대입이 컴파일러/배선 밖에 있으면 이중 생산자다."""
    slot_names = {slot.rpartition(".")[2] for slot in legacy_plan_compiler.COMPILER_OWNED_SLOTS}
    offenders: list[str] = []
    for path in SOURCES:
        if path.name in SLOT_WRITERS:
            continue
        text = _module_text(path)
        for name in slot_names:
            pattern = re.compile(r"\[\s*(?P<q>[\"'])" + re.escape(name) + r"(?P=q)\s*\]\s*=[^=]")
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.name}:{line} -> {name}")
    assert not offenders, (
        "컴파일러 소유 슬롯을 다른 모듈이 직접 쓴다(이중 생산자):\n" + "\n".join(offenders)
    )


def test_slot_writing_modules_are_a_closed_set() -> None:
    """슬롯을 쓸 자격이 있는 모듈 목록은 늘어나면 안 된다 — 늘리려면 여기 사유와 함께."""
    assert SLOT_WRITERS == {"legacy_plan_compiler.py", "semantic_pipeline.py"}


# ── ③ missing_fields 사후 삭제 0건 ─────────────────────────────────────────────
def test_no_module_mutates_semantic_ir_missing_fields() -> None:
    """`semantic_ir["missing_fields"] = ...` / `["status"] = ...` 대입은 파생 생산자만 한다."""
    offenders: list[str] = []
    for path in SOURCES:
        if path.name in SEMANTIC_IR_PRODUCERS:
            continue
        text = _module_text(path)
        for field in ("missing_fields", "unsupported_operations"):
            pattern = re.compile(
                r"semantic_ir\s*\[\s*(?P<q>[\"'])" + field + r"(?P=q)\s*\]\s*=[^=]"
            )
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.name}:{line} -> {field}")
    assert not offenders, (
        "결핍/미지원 목록을 사후에 고치는 코드가 있다(파생값이므로 고칠 것이 없어야 한다):\n"
        + "\n".join(offenders)
    )


def test_semantic_ir_is_derived_from_the_semantic_plan() -> None:
    """semantic_ir 을 만드는 함수는 하나이고, 그 입력은 SemanticPlanV2 다."""
    import inspect

    import semantic_pipeline

    signature = inspect.signature(semantic_pipeline.project_semantic_ir)
    assert list(signature.parameters) == ["plan"]
    plan = semantic_plan.SemanticPlanV2()
    projected = semantic_pipeline.project_semantic_ir(plan)
    # 결핍에는 **원인**이 붙는다 — 원인 없는 결핍은 전부 사용자 확인 요청이 되고, 사용자는
    # 답할 수 없는 내부 필드를 요구받는다(2026-08-02 실측).
    assert set(projected) == {
        "status", "operations", "missing_fields", "missing_field_causes", "failure_kind",
        "policy_applications", "unsupported_operations", "message",
    }


# ── ④ LLM 이 최종 플랜/판정을 만들지 않는다 ─────────────────────────────────────
def test_llm_schema_exposes_no_execution_slot_or_verdict() -> None:
    import json

    from query_structurer import campaign_plan_v4
    from query_structurer.campaign_plan_v4 import CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA

    properties = CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"]
    assert set(properties) == {
        "intent", "campaign_constraints", "result_limit", "audience_requirement",
        "semantic_plan",
    }
    assert "semantic_ir" not in properties, "LLM 이 다시 결핍/상태의 소유자가 됐다."
    # semantic_plan 은 **좁은 노출면**으로만 허용된다. 여기서 이중 해석이 되살아나는 경로는
    # "타입이 늘어나는 것"이므로, 금지 대상은 키의 존재가 아니라 **폭**이다.
    exposed_node_types = {
        branch["properties"]["type"]["enum"][0]
        for branch in properties["semantic_plan"]["properties"]["nodes"]["items"]["anyOf"]
    }
    assert exposed_node_types == set(campaign_plan_v4.LLM_SEMANTIC_PLAN_NODE_TYPES), (
        "이행기 의미 계약이 좁은 노출면을 넘어 다시 LLM 에 열렸다."
    )
    assert "target_user" not in properties and "exclude" not in properties

    # **구조만** 훑는다(필드명·enum). description 은 모델에게 주는 지시문이라 금지어가
    # 정상적으로 등장한다 — "실행 슬롯·SQL 은 만들지 않는다" 가 'sql 노출'로 잡히면
    # 가드가 지시문 자체를 금지하게 되고, 그건 이 테스트가 지키려는 것의 반대다.
    def _structural(node: object) -> object:
        if isinstance(node, dict):
            return {
                key: _structural(value)
                for key, value in node.items()
                if key not in {"description", "title"}
            }
        if isinstance(node, list):
            return [_structural(item) for item in node]
        return node

    rendered = json.dumps(_structural(properties), ensure_ascii=False)
    for slot in legacy_plan_compiler.COMPILER_OWNED_SLOTS:
        name = slot.rpartition(".")[2]
        assert f'"{name}"' not in rendered, (
            f"컴파일러 소유 슬롯 {slot} 이 LLM 에 다시 노출됐다."
        )
    assert "sql" not in rendered.casefold()


# ── ⑤ 이행기 shadow mode 잔재가 없다 ────────────────────────────────────────────
def test_no_permanent_shadow_mode_or_feature_flag_fallback() -> None:
    """비교용 shadow mode 와 결정론 폴백 플래그는 영구 잔류가 금지다."""
    banned = ("legacy_backfill", "compare_semantics", "SEMANTIC_PLAN_SHADOW", "LEGACY_PARSER_FALLBACK")
    offenders: list[str] = []
    for path in SOURCES:
        tree = ast.parse(_module_text(path))
        strings = _executable_strings(tree)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        for token in banned:
            if token in names or any(token in text for text in strings):
                offenders.append(f"{path.name} -> {token}")
    assert not offenders, f"shadow mode/폴백 잔재: {offenders}"


# ── ⑥ 확장 계약: 새 조건 = 노드 + 매핑, 백필 함수 아님 ──────────────────────────
def test_adding_a_condition_needs_no_backfill_or_sweep_function() -> None:
    """확장 지점이 선언 3곳(노드 클래스 / capability JSON / 컴파일러 매핑)뿐임을 고정한다."""
    import semantic_capability

    registry_types = set(semantic_capability.registry()._node_types)
    assert set(semantic_plan.NODE_CLASS_BY_TYPE) == registry_types
    assert set(legacy_plan_compiler.NODE_SLOT_MAP) == set(semantic_plan.NODE_CLASS_BY_TYPE)
    # 노드 타입별 필수 필드는 선언에서 파생된다(별도 목록 없음).
    for node_type, required in semantic_plan.NODE_REQUIREMENTS.items():
        declared = semantic_plan.NODE_CLASS_BY_TYPE[node_type].required_field_names()
        assert required == declared
