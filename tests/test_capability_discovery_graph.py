"""Contracts for the offline typed capability graph."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from capability_discovery.domain import (
    DomainValidationError,
    Evidence,
    GapRecord,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    RepositoryRevision,
    SnapshotValidationError,
    stable_digest,
    stable_id,
)
from capability_discovery.graph_store import (
    DiscoveryGraphStore,
    FactConflictError,
    MissingEndpointError,
    NetworkXGraphStore,
    SourceOwnershipError,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def test_networkx_store_implements_vendor_neutral_protocol() -> None:
    assert isinstance(NetworkXGraphStore(), DiscoveryGraphStore)


def _evidence(path: str, pointer: str = "") -> Evidence:
    return Evidence(
        source_type="json",
        source_path=path,
        source_pointer=pointer,
        extraction_method="deterministic",
        confidence=1,
        content_hash=SHA_A if path.endswith("a.json") else SHA_B,
        revision="0123456789abcdef",
        trust_state="approved_projection",
    )


def _revision() -> RepositoryRevision:
    return RepositoryRevision(
        git_revision="0123456789abcdef",
        dirty=True,
        content_sha256=stable_digest({"a": SHA_A, "b": SHA_B}),
        source_hashes={"catalog/a.json": SHA_A, "catalog/b.json": SHA_B},
    )


def _facts() -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...]]:
    source = GraphNode(
        id="canonical:source:orders",
        kind="Source",
        attributes={"name": "orders", "grains": ["member", "event"]},
        evidence=(_evidence("catalog/a.json", "/sources/orders"),),
    )
    column = GraphNode(
        id="physical:column:orders.member_id",
        kind="PhysicalColumn",
        attributes={"table": "orders", "column": "member_id"},
        evidence=(_evidence("catalog/a.json", "/sources/orders/key"),),
    )
    binding = GraphEdge(
        id="edge:orders-member-id:binding",
        source=source.id,
        target=column.id,
        relation="BINDS_TO",
        attributes={"role": "subject_key"},
        evidence=(_evidence("catalog/a.json", "/sources/orders/key"),),
    )
    read = GraphEdge(
        id="edge:orders-member-id:read",
        source=source.id,
        target=column.id,
        relation="READS_FROM",
        attributes={"required": True},
        evidence=(_evidence("catalog/b.json", "/reads/0"),),
    )
    return (source, column), (binding, read)


def test_domain_records_are_deeply_immutable_and_round_trip() -> None:
    mutable = {"nested": {"values": [1, 2]}}
    node = GraphNode(
        id="canonical:field:tier",
        kind="Field",
        attributes=mutable,
        evidence=(_evidence("catalog/a.json", "/fields/tier"),),
    )
    mutable["nested"]["values"].append(3)

    assert node.to_dict()["attributes"] == {"nested": {"values": [1, 2]}}
    assert GraphNode.from_dict(node.to_dict()) == node
    with pytest.raises(FrozenInstanceError):
        node.kind = "Other"  # type: ignore[misc]

    gap = GapRecord(
        gap_id=stable_id("gap", "LEGACY_ONLY", "consent"),
        concept_id="consent",
        classification="LEGACY_ONLY",
        summary="legacy consent binding has no canonical declaration",
        legacy_assets=("legacy:axis:consent",),
        evidence=node.evidence,
        details={"columns": ["EMAIL_OPTIN"]},
        blocking_questions=("Which registry owns this concept?",),
    )
    assert GapRecord.from_dict(gap.to_dict()) == gap


def test_domain_validation_rejects_ambiguous_or_non_json_values() -> None:
    with pytest.raises(DomainValidationError, match="whitespace"):
        GraphNode(id="canonical field", kind="Field")
    with pytest.raises(DomainValidationError, match="NaN"):
        GraphNode(id="canonical:field:x", kind="Field", attributes={"x": float("nan")})
    with pytest.raises(DomainValidationError, match="SHA-256"):
        RepositoryRevision(git_revision=None, dirty=False, content_sha256="short")
    with pytest.raises(DomainValidationError, match="non-JSON"):
        GraphNode(id="canonical:field:x", kind="Field", attributes={"bad": {1, 2}})


def test_multidigraph_preserves_direction_and_parallel_typed_edges() -> None:
    nodes, edges = _facts()
    store = NetworkXGraphStore(nodes=nodes, edges=edges)
    graph = store.to_networkx()

    assert graph.is_directed()
    assert graph.is_multigraph()
    assert graph.number_of_edges(nodes[0].id, nodes[1].id) == 2
    assert graph.number_of_edges(nodes[1].id, nodes[0].id) == 0
    assert set(graph[nodes[0].id][nodes[1].id]) == {edge.id for edge in edges}
    assert store.find_paths(nodes[0].id, nodes[1].id) == ((nodes[0].id, nodes[1].id),)
    assert (
        store.find_paths(
            nodes[0].id,
            nodes[1].id,
            relations={"NOT_A_RELATION"},
        )
        == ()
    )


def test_store_merges_duplicate_evidence_but_rejects_conflicts() -> None:
    original = GraphNode(
        id="canonical:metric:frequency",
        kind="Metric",
        attributes={"operator": "gte"},
        evidence=(_evidence("catalog/a.json"),),
    )
    duplicate = GraphNode(
        id=original.id,
        kind=original.kind,
        attributes={"operator": "gte", "grain": "member"},
        evidence=(_evidence("catalog/b.json"),),
    )
    store = NetworkXGraphStore(nodes=(original, duplicate))

    merged = store.get_node(original.id)
    assert len(merged.evidence) == 2
    assert merged.attributes["grain"] == "member"

    with pytest.raises(FactConflictError, match="conflicting values"):
        store.upsert_node(
            GraphNode(
                id=original.id,
                kind=original.kind,
                attributes={"operator": "lte"},
            )
        )
    assert store.get_node(original.id) == merged


def test_store_rejects_missing_edge_endpoints_without_partial_mutation() -> None:
    nodes, _ = _facts()
    store = NetworkXGraphStore(nodes=nodes)
    missing = GraphEdge(
        id="edge:missing",
        source=nodes[0].id,
        target="physical:column:missing",
        relation="BINDS_TO",
    )

    with pytest.raises(MissingEndpointError, match="missing endpoint"):
        store.upsert_edge(missing)
    assert store.edge_count == 0


def test_source_replacement_is_atomic_and_removes_deleted_facts() -> None:
    source_a = _evidence("catalog/a.json")
    source_b = _evidence("catalog/b.json")
    old_owner = GraphNode(
        id="canonical:field:consent",
        kind="Field",
        attributes={"version": 1},
        evidence=(source_a,),
    )
    old_column = GraphNode(
        id="physical:column:member.email_optin",
        kind="PhysicalColumn",
        evidence=(source_a,),
    )
    retained = GraphNode(
        id="canonical:field:tier",
        kind="Field",
        evidence=(source_b,),
    )
    old_edge = GraphEdge(
        id="edge:old-consent-binding",
        source=old_owner.id,
        target=old_column.id,
        relation="BINDS_TO",
        evidence=(source_a,),
    )
    store = NetworkXGraphStore(
        nodes=(old_owner, old_column, retained), edges=(old_edge,)
    )

    new_owner = GraphNode(
        id=old_owner.id,
        kind=old_owner.kind,
        attributes={"version": 2},
        evidence=(source_a,),
    )
    new_column = GraphNode(
        id="physical:column:member.email_consent_flag",
        kind="PhysicalColumn",
        evidence=(source_a,),
    )
    new_edge = GraphEdge(
        id="edge:new-consent-binding",
        source=new_owner.id,
        target=new_column.id,
        relation="BINDS_TO",
        evidence=(source_a,),
    )
    result = store.replace_source(
        source_a.source_key,
        nodes=(new_owner, new_column),
        edges=(new_edge,),
    )

    assert old_column.id in result.removed_nodes
    assert old_edge.id in result.removed_edges
    assert store.get_node(old_owner.id).attributes["version"] == 2
    assert store.get_node(retained.id) == retained
    assert {edge.id for edge in store.edges()} == {new_edge.id}


def test_failed_source_replacement_leaves_the_store_unchanged() -> None:
    nodes, edges = _facts()
    store = NetworkXGraphStore(nodes=nodes, edges=edges)
    before = store.snapshot(_revision()).to_json()
    wrong_owner = GraphNode(
        id="canonical:field:new",
        kind="Field",
        evidence=(_evidence("catalog/b.json"),),
    )

    with pytest.raises(SourceOwnershipError, match="no evidence"):
        store.replace_source(
            _evidence("catalog/a.json").source_key,
            nodes=(wrong_owner,),
        )
    assert store.snapshot(_revision()).to_json() == before


def test_snapshot_json_is_sorted_deterministic_and_round_trips() -> None:
    nodes, edges = _facts()
    store = NetworkXGraphStore(nodes=reversed(nodes), edges=reversed(edges))

    snapshot = store.snapshot(_revision())
    payload = snapshot.to_json()
    restored = GraphSnapshot.from_json(payload)

    assert payload == snapshot.to_json()
    assert restored == snapshot
    assert [item["id"] for item in snapshot.to_dict()["nodes"]] == sorted(
        node.id for node in nodes
    )
    restored_store = NetworkXGraphStore.from_snapshot(restored)
    assert restored_store.edges() == store.edges()


def test_snapshot_rejects_dangling_edges_explicitly() -> None:
    dangling = GraphEdge(
        id="edge:dangling",
        source="canonical:field:a",
        target="canonical:field:b",
        relation="DEPENDS_ON",
    )
    with pytest.raises(SnapshotValidationError, match="missing endpoints"):
        GraphSnapshot(revision=_revision(), nodes=(), edges=(dangling,))


def test_search_is_typed_deterministic_and_returns_detached_domain_records() -> None:
    nodes, edges = _facts()
    store = NetworkXGraphStore(nodes=nodes, edges=edges)

    assert store.search("orders member_id")[0].id == nodes[1].id
    assert store.search("orders", kinds={"PhysicalColumn"}) == (nodes[1],)
    assert store.neighbors(nodes[0].id, relations={"BINDS_TO"}) == (nodes[1],)
