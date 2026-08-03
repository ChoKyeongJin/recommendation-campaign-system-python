from __future__ import annotations

import ast
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from capability_discovery.domain import (
    Evidence,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    RepositoryRevision,
)
from capability_discovery.runtime_diagnostics import (
    ContentHashDiagnosticCache,
    OriginalRuntimeOutcome,
    RuntimeAliasIndex,
    RuntimeDiagnosticAdapter,
    extract_failure_signal,
    summarize_failures,
)

SHA = "a" * 64


def _evidence(trust: str, path: str) -> Evidence:
    return Evidence(
        source_type="config",
        source_path=path,
        source_pointer="/aliases/0",
        extraction_method="deterministic",
        content_hash=SHA,
        trust_state=trust,
    )


def _snapshot() -> GraphSnapshot:
    approved = _evidence("approved_projection", "approved.json")
    observed = _evidence("observed", "legacy.json")
    nodes = (
        GraphNode(
            id="canonical:field:member_grade",
            kind="CanonicalField",
            attributes={
                "canonical_id": "member_grade",
                "trust_state": "approved_projection",
            },
            evidence=(approved,),
        ),
        GraphNode(
            id="approved:surface-term:grade",
            kind="SurfaceTerm",
            attributes={"text": "grade", "trust_state": "approved_projection"},
            evidence=(approved,),
        ),
        GraphNode(
            id="legacy:metric:dormant",
            kind="LegacyMetricAsset",
            attributes={"trust_state": "observed"},
            evidence=(observed,),
        ),
        GraphNode(
            id="observed:surface-term:dormant",
            kind="SurfaceTerm",
            attributes={"text": "dormant", "trust_state": "observed"},
            evidence=(observed,),
        ),
        GraphNode(
            id="observed:surface-term:unapproved",
            kind="SurfaceTerm",
            attributes={"text": "unapproved", "trust_state": "observed"},
            evidence=(observed,),
        ),
    )
    edges = (
        GraphEdge(
            id="edge:approved",
            source="approved:surface-term:grade",
            target="canonical:field:member_grade",
            relation="ALIAS_OF",
            evidence=(approved,),
        ),
        GraphEdge(
            id="edge:observed",
            source="observed:surface-term:dormant",
            target="legacy:metric:dormant",
            relation="CANDIDATE_ALIAS_OF",
            evidence=(observed,),
        ),
        # Relation spelling alone cannot promote observed evidence.
        GraphEdge(
            id="edge:suspicious",
            source="observed:surface-term:unapproved",
            target="canonical:field:member_grade",
            relation="ALIAS_OF",
            evidence=(observed,),
        ),
    )
    return GraphSnapshot(
        revision=RepositoryRevision(
            git_revision="deadbeef",
            dirty=False,
            content_sha256=SHA,
            source_hashes={"approved.json": SHA},
        ),
        nodes=nodes,
        edges=edges,
    )


def test_only_fully_approved_alias_edges_reach_approved_candidates() -> None:
    adapter = RuntimeDiagnosticAdapter(snapshot=_snapshot())

    approved = adapter.diagnose_failure("catalog_symbol_unresolved", "grade")
    observed = adapter.diagnose_failure("catalog_symbol_unresolved", "dormant")
    suspicious = adapter.diagnose_failure(
        "catalog_symbol_unresolved", "unapproved"
    )

    assert [item.canonical_id for item in approved.approved_candidates] == [
        "member_grade"
    ]
    assert approved.discovery_only_candidates == ()
    assert observed.approved_candidates == ()
    assert [item.alias for item in observed.discovery_only_candidates] == [
        "dormant"
    ]
    assert suspicious.approved_candidates == ()
    assert [item.alias for item in suspicious.discovery_only_candidates] == [
        "unapproved"
    ]
    assert approved.to_dict()["approved_candidates"][0]["score"] == 1.0
    assert approved.to_dict()["approved_candidates"][0]["executable"] is False
    assert "source_paths" not in approved.to_dict()["approved_candidates"][0]


def test_matching_uses_only_the_exact_received_symbol() -> None:
    adapter = RuntimeDiagnosticAdapter(snapshot=_snapshot())

    diagnostics = adapter.diagnose_failure(
        "catalog_symbol_unresolved", "find users with grade"
    )

    assert diagnostics.approved_candidates == ()
    assert diagnostics.discovery_only_candidates == ()


def test_original_outcome_is_immutable_and_never_rewrites_status_or_sql() -> None:
    original = OriginalRuntimeOutcome(
        failure_code="catalog_symbol_unresolved",
        status=422,
        sql="SELECT existing_sql",
    )
    adapter = RuntimeDiagnosticAdapter(snapshot=_snapshot())

    diagnostics = adapter.diagnose_failure(
        "catalog_symbol_unresolved", "grade", original_outcome=original
    )
    payload = diagnostics.to_dict()

    assert original.status == 422
    assert original.sql == "SELECT existing_sql"
    with pytest.raises(FrozenInstanceError):
        original.status = 200  # type: ignore[misc]
    assert payload["failure_code"] == original.failure_code
    assert "status" not in payload
    assert "sql" not in payload
    assert payload["diagnostic_only"] is True
    assert payload["executable"] is False


def test_outcome_mismatch_and_non_allowlisted_codes_are_empty() -> None:
    adapter = RuntimeDiagnosticAdapter(snapshot=_snapshot())
    original = OriginalRuntimeOutcome(failure_code="sql_guard_failed")

    mismatch = adapter.diagnose_failure(
        "catalog_symbol_unresolved", "grade", original_outcome=original
    )
    ignored = adapter.diagnose_failure("made_up_failure", "grade")

    assert mismatch.available is False
    assert mismatch.reason == "original_outcome_mismatch"
    assert mismatch.approved_candidates == ()
    assert ignored.reason == "failure_code_not_allowlisted"
    assert ignored.approved_candidates == ()
    assert ignored.executable is False


def test_failure_rows_are_allowlisted_aggregated_and_never_retain_sql() -> None:
    rows = [
        {
            "failure_code": "catalog_symbol_unresolved",
            "received_symbol": "grade",
            "created_at": "2026-08-01T00:00:00Z",
            "user_impact_weight": 2,
            "evidence_completeness": 0.5,
            "legacy_asset_availability": 1,
            "sql": "SELECT secret_one",
        },
        {
            "failure_code": "catalog_symbol_unresolved",
            "received_symbol": "grade",
            "created_at": "2026-08-02T00:00:00Z",
            "user_impact_weight": 2,
            "evidence_completeness": 0.5,
            "legacy_asset_availability": 1,
            "sql": "SELECT secret_two",
        },
        {"failure_code": "unknown_code", "received_symbol": "grade"},
    ]

    summary = summarize_failures(rows)
    payload = summary.to_dict()

    assert summary.total_rows == 3
    assert summary.accepted_rows == 2
    assert summary.ignored_rows == 1
    assert len(summary.failure_groups) == 1
    repeated = summary.repeated_failures[0]
    assert repeated.failure_frequency == 2
    assert repeated.latest_at == "2026-08-02T00:00:00Z"
    assert repeated.priority_score == 2.0
    assert repeated.executable is False
    assert "secret" not in str(payload)


def test_persisted_row_uses_decision_code_and_exact_structured_symbol() -> None:
    rows = [
        {
            "query_plan": {
                "decisions": [
                    {"reason": "catalog_symbol_unresolved"}
                ],
                "audience_requirement": {
                    "issues": [
                        {
                            "code": "unsupported_semantics",
                            "argument": "member_grade",
                        }
                    ]
                },
            }
        },
        {
            "query_plan": {
                "decisions": [
                    {"reason": "catalog_symbol_unresolved"}
                ],
                "semantic_ir": {
                    "unsupported_operations": [
                        {
                            "kind": "catalog_symbol_unresolved",
                            "received_symbol": "member_grade",
                        }
                    ]
                },
            }
        },
    ]

    summary = summarize_failures(rows)

    assert summary.accepted_rows == 2
    assert len(summary.repeated_failures) == 1
    assert summary.repeated_failures[0].subject == "member_grade"
    assert summary.repeated_failures[0].failure_frequency == 2


def test_failure_provider_and_snapshot_are_loaded_once() -> None:
    class Service:
        calls = 0

        def snapshot(self) -> GraphSnapshot:
            self.calls += 1
            return _snapshot()

    class Provider:
        calls = 0

        def load_failure_rows(self) -> list[dict[str, str]]:
            self.calls += 1
            return [
                {
                    "failure_code": "catalog_symbol_unresolved",
                    "received_symbol": "grade",
                },
                {
                    "failure_code": "catalog_symbol_unresolved",
                    "received_symbol": "grade",
                },
            ]

    service = Service()
    provider = Provider()
    adapter = RuntimeDiagnosticAdapter(
        service=service,
        failure_log_provider=provider,
        build_timeout_seconds=1,
    )

    assert adapter.initialize(timeout_seconds=1) is True
    adapter.diagnose_failure("catalog_symbol_unresolved", "grade")
    adapter.failure_summary()

    assert service.calls == 1
    assert provider.calls == 1
    assert adapter.status().ready is True
    assert adapter.status().index_revision == "deadbeef"


def test_signal_extractor_uses_structured_priority_and_ignores_missing_paths() -> None:
    plan = {
        "audience_requirement": {
            "issues": [
                {
                    "code": "unsupported_semantics",
                    "argument": "grade",
                    "evidence": {"text": "gold grade users"},
                }
            ]
        },
        "missing_fields": ["audience_requirement.member_entity"],
        "semantic_plan": {
            "capability_verdicts": [{"metric": "later", "node_id": "n1"}],
            "nodes": [{"id": "n1", "source_span": "later span"}],
        },
    }
    response = {"failure_code": "catalog_symbol_unresolved"}

    signal = extract_failure_signal(plan, response)

    assert signal is not None
    assert signal.received_symbol == "grade"
    assert signal.source == "audience_requirement.issues"
    assert signal.diagnostic_only is True
    assert signal.executable is False


def test_signal_extractor_joins_capability_verdict_to_recursive_node() -> None:
    plan = {
        "semantic_plan": {
            "capability_verdicts": [{"metric": "purchase_cycle", "node_id": "n2"}],
            "nodes": [
                {
                    "id": "root",
                    "children": [
                        {"id": "n2", "source_span": "purchase cycle"}
                    ],
                }
            ],
        }
    }

    signal = extract_failure_signal(
        plan, {"failure_code": "catalog_symbol_unresolved"}
    )

    assert signal is not None
    assert signal.received_symbol == "purchase_cycle"
    assert signal.node_id == "n2"
    assert signal.source_span == "purchase cycle"


def test_content_hash_cache_reuses_immutable_index() -> None:
    cache = ContentHashDiagnosticCache()
    snapshot = _snapshot()

    first = cache.get_or_build(snapshot)
    second = cache.get_or_build(snapshot)

    assert first is second
    assert isinstance(first, RuntimeAliasIndex)
    assert len(cache) == 1
    with pytest.raises(FrozenInstanceError):
        first.content_sha256 = "b" * 64  # type: ignore[misc]


def test_snapshot_build_failure_fails_open_and_is_not_retried() -> None:
    calls = 0

    def broken_loader() -> GraphSnapshot:
        nonlocal calls
        calls += 1
        raise OSError("repository unavailable")

    adapter = RuntimeDiagnosticAdapter(
        snapshot_loader=broken_loader, build_timeout_seconds=1
    )

    first = adapter.diagnose_failure("catalog_symbol_unresolved", "grade")
    second = adapter.diagnose_failure("catalog_symbol_unresolved", "grade")

    assert first.to_dict() == second.to_dict()
    assert first.available is False
    assert first.reason == "snapshot_build_failed"
    assert first.approved_candidates == ()
    assert first.executable is False
    assert calls == 1


def test_snapshot_timeout_returns_empty_then_uses_same_completed_build() -> None:
    release = threading.Event()
    loader_finished = threading.Event()
    calls = 0

    def slow_loader() -> GraphSnapshot:
        nonlocal calls
        calls += 1
        release.wait(1)
        loader_finished.set()
        return _snapshot()

    adapter = RuntimeDiagnosticAdapter(
        snapshot_loader=slow_loader, build_timeout_seconds=0.001
    )

    timed_out = adapter.diagnose_failure("catalog_symbol_unresolved", "grade")
    assert timed_out.available is False
    assert timed_out.reason == "snapshot_build_timeout"

    release.set()
    assert loader_finished.wait(1)
    assert adapter.initialize(timeout_seconds=1) is True
    ready = adapter.diagnose_failure("catalog_symbol_unresolved", "grade")
    assert [item.alias for item in ready.approved_candidates] == ["grade"]
    assert calls == 1


def test_disabled_adapter_is_an_explicit_empty_diagnostic_source() -> None:
    diagnostics = RuntimeDiagnosticAdapter().diagnose_failure(
        "catalog_symbol_unresolved", "grade"
    )

    assert diagnostics.available is False
    assert diagnostics.reason == "snapshot_unavailable"
    assert diagnostics.executable is False


def test_runtime_adapter_has_no_networkx_or_graph_store_dependency() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "capability_discovery"
        / "runtime_diagnostics.py"
    )
    syntax = ast.parse(module_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(syntax)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(syntax)
        if isinstance(node, ast.ImportFrom)
    }

    assert "networkx" not in imports
    assert "capability_discovery.graph_store" not in imports
    assert ".graph_store" not in imports
