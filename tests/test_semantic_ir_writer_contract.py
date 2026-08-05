from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "query_structurer"
MUTATING_METHODS = {"append", "clear", "extend", "insert", "pop", "remove", "setdefault", "update"}
IGNORED_TREE_PARTS = {".git", ".venv", "venv", "tests", "artifacts", "__pycache__"}
EVENT_IR_OR_LEGACY_FILES = {
    "canonical_event_ir_grounding.py",
    "cutover_legacy_audience.py",
    "event_ir.py",
    "legacy_audience_migration.py",
    # `semantic_receipts.py` 는 2026-08-05 삭제됐다(축1 폐기) — 면제 대상 자체가 없다.
    # `legacy_plan_compiler.py` / `semantic_plan_bridge.py` 도 같은 날 삭제됐다
    # (SemanticPlanV2 중간표현 폐기) — 마찬가지로 면제할 파일이 없다.
}
EVENT_IR_FUNCTION_EXEMPTIONS = {
    ("graph_rag.py", "_mark_canonical_event_ir_lowering_failure"),
}


def _semantic_ir_subscript_depth(node: ast.AST) -> int:
    depth = 0
    while isinstance(node, ast.Subscript):
        depth += 1
        if isinstance(node.slice, ast.Constant) and node.slice.value == "semantic_ir":
            return depth
        node = node.value
    return 0


def _semantic_ir_owner_depth(node: ast.AST) -> int:
    depth = _semantic_ir_subscript_depth(node)
    if depth:
        return depth
    if isinstance(node, ast.Name) and node.id == "semantic_ir":
        return 1
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return 0
    if node.func.attr not in {"get", "setdefault"} or not node.args:
        return 0
    key = node.args[0]
    return 1 if isinstance(key, ast.Constant) and key.value == "semantic_ir" else 0


def _dict_contains_semantic_ir(node: ast.AST) -> bool:
    return isinstance(node, ast.Dict) and any(
        isinstance(key, ast.Constant) and key.value == "semantic_ir"
        for key in node.keys
        if key is not None
    )


class _WriterVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.functions: list[str] = []
        self.violations: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _check_target(self, target: ast.AST, line: int) -> None:
        depth = _semantic_ir_subscript_depth(target)
        allowed = (
            self.path.name == "semantic_ir.py"
            and self.functions == ["write_semantic_ir"]
            and depth == 1
        )
        event_ir_exempt = (
            self.path.name,
            self.functions[-1] if self.functions else "",
        ) in EVENT_IR_FUNCTION_EXEMPTIONS
        if depth and not allowed and not event_ir_exempt:
            self.violations.append(f"{self.path.relative_to(REPO_ROOT)}:{line}")

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_target(target, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr in MUTATING_METHODS
            and _semantic_ir_owner_depth(function.value)
        ):
            self.violations.append(f"{self.path.relative_to(REPO_ROOT)}:{node.lineno}")
        elif (
            isinstance(function, ast.Attribute)
            and function.attr == "setdefault"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "semantic_ir"
        ):
            self.violations.append(f"{self.path.relative_to(REPO_ROOT)}:{node.lineno}")
        elif (
            isinstance(function, ast.Attribute)
            and function.attr == "update"
            and node.args
            and _dict_contains_semantic_ir(node.args[0])
        ):
            self.violations.append(f"{self.path.relative_to(REPO_ROOT)}:{node.lineno}")
        self.generic_visit(node)


def test_query_structurer_has_one_semantic_ir_writer_and_no_nested_mutation() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        visitor = _WriterVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        violations.extend(visitor.violations)

    assert not violations, (
        "semantic_ir projection은 write_semantic_ir에서만 기록하고 중첩 변경하지 않는다: "
        + ", ".join(violations)
    )


def test_active_code_has_one_semantic_ir_writer_and_no_nested_mutation() -> None:
    violations: list[str] = []
    for path in REPO_ROOT.rglob("*.py"):
        if set(path.relative_to(REPO_ROOT).parts) & IGNORED_TREE_PARTS:
            continue
        if path.name in EVENT_IR_OR_LEGACY_FILES:
            continue
        visitor = _WriterVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8")))
        violations.extend(visitor.violations)

    assert not violations, (
        "활성 semantic_ir projection은 write_semantic_ir에서만 기록하고 중첩 변경하지 않는다: "
        + ", ".join(violations)
    )


def test_graph_rag_reprojects_application_outcome_through_typed_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import graph_rag
    from query_structurer.semantic_outcome import SemanticOutcome

    unsupported = {
        "kind": "data_coverage_gap",
        "reason": "requested history predates complete coverage",
        "evidence": "지난달 말 기준",
    }
    projection = SemanticOutcome.unsupported(operations=[unsupported]).to_legacy_dict()
    candidate = {"semantic_ir": projection, "literal_bindings": []}

    outcome_calls: list[dict[str, Any]] = []
    writer_calls: list[dict[str, Any]] = []
    real_outcome = graph_rag.SemanticOutcome
    real_writer = graph_rag.write_semantic_ir

    class TrackingOutcome:
        def __new__(cls, **kwargs: Any) -> SemanticOutcome:
            outcome_calls.append(copy.deepcopy(kwargs))
            return real_outcome(**kwargs)

    def tracking_writer(payload: dict[str, Any], value: dict[str, Any]) -> None:
        writer_calls.append(copy.deepcopy(value))
        real_writer(payload, value)

    monkeypatch.setattr(graph_rag, "SemanticOutcome", TrackingOutcome)
    monkeypatch.setattr(graph_rag, "write_semantic_ir", tracking_writer)

    coerced = graph_rag._coerce_llm_query_plan_candidate(candidate, {})

    assert outcome_calls == [
        {
            "status": "unsupported",
            "missing_fields": (),
            "missing_field_causes": (),
            "failure_kind": "unsupported",
            "policy_applications": (),
            "unsupported_operations": (unsupported,),
            "message": None,
        }
    ]
    assert writer_calls == [projection]
    assert coerced["semantic_ir"] == projection
    assert coerced["literal_bindings"] == []


def test_graph_rag_rejects_legacy_semantic_operations_at_typed_boundary() -> None:
    import graph_rag

    literals = [
        {"id": "baseline", "kind": "date_window"},
        {"id": "current", "kind": "date_window"},
    ]
    projection = {
        "status": "resolved",
        "operations": [
            {
                "kind": "period_over_period_change",
                "metric_id": "purchase_amount",
                "direction": "increase",
                "bindings": [
                    {"role": "baseline", "literal_id": "baseline"},
                    {"role": "current", "literal_id": "current"},
                ],
            }
        ],
        "missing_fields": [],
        "missing_field_causes": [],
        "failure_kind": None,
        "policy_applications": [],
        "unsupported_operations": [],
        "message": None,
    }

    with pytest.raises(ValueError, match="cannot contain legacy operations"):
        graph_rag._coerce_llm_query_plan_candidate(
            {"semantic_ir": projection, "literal_bindings": literals},
            {},
        )
