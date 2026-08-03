from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import capability_discovery.projection as projection_module
from capability_discovery.projection import (
    APPROVED_TRUST,
    OBSERVED_TRUST,
    build_approved_projection,
    build_repository_projection,
)
from tools import canonical_coverage_inventory

ROOT = Path(__file__).resolve().parents[1]


def _by_id(projection):
    return {node.id: node for node in projection.nodes}


def test_repository_projection_separates_approved_and_observed_assets() -> None:
    projection = build_repository_projection(ROOT)
    nodes = _by_id(projection)

    assert projection.ok, [issue.to_dict() for issue in projection.issues]
    assert projection.cannot_execute_sql is True
    assert projection.cannot_mutate_approved_registry is True
    assert projection.metadata["runtime_connected"] is False
    assert (
        "capability_discovery/projection.py"
        in projection.metadata["projection_implementation_sources"]
    )
    assert (
        projection.revision.source_hashes["capability_discovery/projection.py"]
        == hashlib.sha256(Path(projection_module.__file__).read_bytes()).hexdigest()
    )
    assert (
        nodes["canonical:field:subject.gender"].attributes["trust_state"]
        == APPROVED_TRUST
    )
    assert (
        nodes["legacy:coverage-axis:eq_filters"].attributes["trust_state"]
        == OBSERVED_TRUST
    )
    assert nodes["canonical:policy:data-availability"].attributes["mode"] == "advise"

    approved = {
        node.id
        for node in projection.nodes
        if node.attributes.get("trust_state") == APPROVED_TRUST
    }
    observed = {
        node.id
        for node in projection.nodes
        if node.attributes.get("trust_state") == OBSERVED_TRUST
    }
    assert approved
    assert observed
    assert projection.metadata["node_counts"] == {
        "approved_projection": len(approved),
        "observed": len(observed),
        "total": len(projection.nodes),
    }


def test_projection_reuses_p4_inventory_without_promoting_legacy_facts() -> None:
    projection = build_repository_projection(ROOT)
    expected = canonical_coverage_inventory.scan()

    assert projection.metadata["coverage_inventory"] == expected
    assert expected["missing_columns"] > 0
    for axis, columns in expected["by_axis"].items():
        node = _by_id(projection)[f"legacy:coverage-axis:{axis}"]
        assert list(node.attributes["missing_canonical_columns"]) == columns
        assert node.attributes["trust_state"] == OBSERVED_TRUST


def test_code_contracts_are_extracted_by_ast_with_bounded_proof() -> None:
    projection = build_repository_projection(ROOT)
    nodes = _by_id(projection)
    edges = projection.edges

    expected_handlers = {
        "_logical",
        "_aggregate",
        "_predicate",
        "_relation_predicate",
    }
    lowerers = {
        str(node.attributes["symbol"]).rpartition(".")[2]: node
        for node in projection.nodes
        if node.kind == "LoweringFunction"
    }
    assert expected_handlers <= set(lowerers)
    assert all(
        lowerers[name].attributes["evidence_scope"] == "explicit_isinstance_dispatch"
        for name in expected_handlers
    )
    assert any(edge.relation == "PRECEDES" for edge in edges)
    assert any(edge.relation == "EXCLUSIVE_ROUTE" for edge in edges)
    assert nodes["canonical:query-plan-slot:event_expression"].kind == "QueryPlanSlot"

    syntax = ast.parse(Path(projection_module.__file__).read_text(encoding="utf-8"))
    imported_modules = {
        alias.name
        for node in ast.walk(syntax)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(syntax)
        if isinstance(node, ast.ImportFrom)
    }
    assert "graph_rag" not in imported_modules


def test_every_fact_has_content_addressed_provenance_and_valid_endpoints() -> None:
    projection = build_repository_projection(ROOT)
    node_ids = {node.id for node in projection.nodes}
    source_hashes = projection.revision.source_hashes

    assert len(projection.revision.content_sha256) == 64
    for path, digest in source_hashes.items():
        assert digest == hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
    for fact in (*projection.nodes, *projection.edges):
        assert fact.evidence
        for evidence in fact.evidence:
            assert evidence.source_path
            assert evidence.source_pointer
            assert evidence.content_hash == source_hashes[evidence.source_path]
    assert all(
        edge.source in node_ids and edge.target in node_ids for edge in projection.edges
    )
    json.dumps(projection.to_dict(), ensure_ascii=False, sort_keys=True)


def test_missing_repository_inputs_are_reported_not_hidden(tmp_path: Path) -> None:
    projection = build_repository_projection(tmp_path)

    assert projection.ok is False
    assert projection.nodes == ()
    assert projection.edges == ()
    assert any(
        issue.code == "SOURCE_MISSING"
        and issue.source_path == "docs/data/runtime/semantics/audience_catalog.json"
        and issue.severity == "error"
        for issue in projection.issues
    )
    assert projection.metadata["coverage_inventory"] == {}


def test_approved_projection_alias_preserves_offline_contract() -> None:
    projection = build_approved_projection(ROOT)

    assert projection.cannot_execute_sql is True
    assert projection.cannot_mutate_approved_registry is True
    assert projection.metadata["projection_kind"] == "offline_capability_discovery_g0"
