from __future__ import annotations

import ast
from pathlib import Path

import pytest

from capability_discovery.policy import (
    DEFAULT_DISCOVERY_POLICY,
    DiscoveryBoundaryError,
    DiscoveryPolicy,
    validate_offline_payload,
)


def test_policy_cannot_be_weakened_for_execution_or_mutation() -> None:
    with pytest.raises(DiscoveryBoundaryError):
        DiscoveryPolicy(executable=True)
    with pytest.raises(DiscoveryBoundaryError):
        DiscoveryPolicy(mutation_allowed=True)
    with pytest.raises(DiscoveryBoundaryError):
        DiscoveryPolicy(runtime_failure_codes_reused=True)


def test_serialized_payload_must_keep_offline_boundary() -> None:
    payload = {
        "policy": DEFAULT_DISCOVERY_POLICY.to_dict(),
        "executable": False,
        "mutation_allowed": False,
    }
    validate_offline_payload(payload)

    payload["policy"] = {**DEFAULT_DISCOVERY_POLICY.to_dict(), "discovery_only": False}
    with pytest.raises(DiscoveryBoundaryError):
        validate_offline_payload(payload)


ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    return imported


def test_runtime_decision_pipeline_does_not_depend_on_discovery() -> None:
    runtime_files = list(ROOT.glob("*.py"))
    for directory in ("query_structurer", "graph", "rag"):
        runtime_files.extend((ROOT / directory).rglob("*.py"))

    # ``api.py`` is the single composition/presentation boundary allowed to
    # append non-executable diagnostics after the runtime outcome is final.
    # Planning, routing, lowering, compilation, and SQL modules remain barred
    # from importing discovery.
    presentation_boundary = ROOT / "api.py"
    offenders = [
        str(path.relative_to(ROOT))
        for path in runtime_files
        if path != presentation_boundary
        and "capability_discovery" in _imports(path)
    ]
    assert offenders == []


def test_api_is_the_only_runtime_file_with_a_discovery_dependency() -> None:
    runtime_files = list(ROOT.glob("*.py"))
    for directory in ("query_structurer", "graph", "rag"):
        runtime_files.extend((ROOT / directory).rglob("*.py"))

    users = sorted(
        str(path.relative_to(ROOT))
        for path in runtime_files
        if "capability_discovery" in _imports(path)
    )
    assert users == ["api.py"]


def test_discovery_does_not_import_runtime_planner_or_sql_modules() -> None:
    forbidden = {
        "event_compiler",
        "graph_rag",
        "query_structurer",
        "resolved_semantic_catalog",
        "semantic_capability",
        "semantic_plan_event_lowering",
        "targeting_ir",
    }
    offenders = {
        str(path.relative_to(ROOT)): sorted(_imports(path) & forbidden)
        for path in (ROOT / "capability_discovery").glob("*.py")
        if _imports(path) & forbidden
    }
    assert offenders == {}
