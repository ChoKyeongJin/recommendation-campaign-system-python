"""Typed, in-memory graph store for offline capability discovery.

``NetworkXGraphStore`` uses a directed multigraph because two capability
entities may have several independently evidenced relations.  The immutable
domain records remain the source of truth inside the store; callers receive a
fresh NetworkX graph when they need graph-native algorithms.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import networkx as nx

from .domain import (
    CapabilityDiscoveryError,
    Evidence,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    RepositoryRevision,
    canonical_json,
    thaw_json,
)


class GraphStoreError(CapabilityDiscoveryError):
    """Base class for graph-store contract violations."""


class FactConflictError(GraphStoreError):
    """Raised when one stable fact id carries incompatible definitions."""


class MissingEndpointError(GraphStoreError):
    """Raised when an edge refers to a node that is not in the store."""


class SourceOwnershipError(GraphStoreError):
    """Raised when replacement facts do not cite their claimed source."""


class UnknownNodeError(GraphStoreError):
    """Raised when a graph traversal starts from an unknown node."""


@dataclass(frozen=True)
class SourceMutationResult:
    """Deterministic summary of a source removal or replacement."""

    source_key: str
    removed_nodes: tuple[str, ...]
    removed_edges: tuple[str, ...]
    retained_nodes: tuple[str, ...]
    retained_edges: tuple[str, ...]


@runtime_checkable
class DiscoveryGraphStore(Protocol):
    """Vendor-neutral interface exposed to discovery services."""

    def upsert_node(self, node: GraphNode) -> GraphNode: ...

    def upsert_edge(self, edge: GraphEdge) -> GraphEdge: ...

    def remove_source(self, source_key: str) -> SourceMutationResult: ...

    def replace_source(
        self,
        source_key: str,
        *,
        nodes: Iterable[GraphNode] = (),
        edges: Iterable[GraphEdge] = (),
    ) -> SourceMutationResult: ...

    def search(
        self,
        query: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 20,
    ) -> tuple[GraphNode, ...]: ...

    def find_paths(
        self,
        source: str,
        target: str,
        *,
        max_hops: int = 4,
        relations: Iterable[str] | None = None,
        limit: int = 20,
    ) -> tuple[tuple[str, ...], ...]: ...

    def snapshot(self, revision: RepositoryRevision) -> GraphSnapshot: ...


def _merge_evidence(
    left: Iterable[Evidence], right: Iterable[Evidence]
) -> tuple[Evidence, ...]:
    merged = {
        canonical_json(item.to_dict()): item for item in (*tuple(left), *tuple(right))
    }
    return tuple(merged[key] for key in sorted(merged))


def _merge_attributes(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    fact_id: str,
) -> dict[str, Any]:
    merged = thaw_json(left)
    incoming = thaw_json(right)
    for key in sorted(incoming):
        if key in merged and canonical_json(merged[key]) != canonical_json(
            incoming[key]
        ):
            raise FactConflictError(
                f"fact {fact_id!r} has conflicting values for attribute {key!r}"
            )
        merged[key] = incoming[key]
    return merged


def _merge_node(current: GraphNode, incoming: GraphNode) -> GraphNode:
    if current.kind != incoming.kind:
        raise FactConflictError(
            f"node {current.id!r} has conflicting kinds "
            f"{current.kind!r} and {incoming.kind!r}"
        )
    return GraphNode(
        id=current.id,
        kind=current.kind,
        attributes=_merge_attributes(
            current.attributes, incoming.attributes, fact_id=current.id
        ),
        evidence=_merge_evidence(current.evidence, incoming.evidence),
    )


def _merge_edge(current: GraphEdge, incoming: GraphEdge) -> GraphEdge:
    old_shape = (current.source, current.target, current.relation)
    new_shape = (incoming.source, incoming.target, incoming.relation)
    if old_shape != new_shape:
        raise FactConflictError(
            f"edge {current.id!r} has conflicting shapes {old_shape!r} and {new_shape!r}"
        )
    return GraphEdge(
        id=current.id,
        source=current.source,
        target=current.target,
        relation=current.relation,
        attributes=_merge_attributes(
            current.attributes, incoming.attributes, fact_id=current.id
        ),
        evidence=_merge_evidence(current.evidence, incoming.evidence),
    )


def _matching_evidence(
    evidence: Iterable[Evidence], source_key: str
) -> tuple[Evidence, ...]:
    return tuple(
        item
        for item in evidence
        if item.source_key == source_key or item.source_path == source_key
    )


class NetworkXGraphStore:
    """A validated typed-fact store backed by ``networkx.MultiDiGraph``.

    This class has no persistence side effects.  Snapshots are returned as
    values; choosing if and where to write one is left to an explicit CLI or
    review workflow.
    """

    def __init__(
        self,
        nodes: Iterable[GraphNode] = (),
        edges: Iterable[GraphEdge] = (),
    ) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}
        self._graph = nx.MultiDiGraph()
        self.replace_all(nodes=nodes, edges=edges)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def nodes(self, *, kind: str | None = None) -> tuple[GraphNode, ...]:
        return tuple(
            node
            for node in sorted(self._nodes.values(), key=lambda item: item.id)
            if kind is None or node.kind == kind
        )

    def edges(self, *, relation: str | None = None) -> tuple[GraphEdge, ...]:
        return tuple(
            edge
            for edge in sorted(self._edges.values(), key=lambda item: item.id)
            if relation is None or edge.relation == relation
        )

    def get_node(self, node_id: str) -> GraphNode:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise UnknownNodeError(f"unknown node: {node_id}") from exc

    def get_edge(self, edge_id: str) -> GraphEdge:
        try:
            return self._edges[edge_id]
        except KeyError as exc:
            raise GraphStoreError(f"unknown edge: {edge_id}") from exc

    def source_keys(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    item.source_key
                    for fact in (*self._nodes.values(), *self._edges.values())
                    for item in fact.evidence
                }
            )
        )

    def replace_all(
        self,
        *,
        nodes: Iterable[GraphNode],
        edges: Iterable[GraphEdge],
    ) -> None:
        """Atomically replace all facts after validating and merging duplicates."""

        new_nodes: dict[str, GraphNode] = {}
        new_edges: dict[str, GraphEdge] = {}
        for node in nodes:
            if not isinstance(node, GraphNode):
                raise GraphStoreError("nodes must contain only GraphNode records")
            current = new_nodes.get(node.id)
            new_nodes[node.id] = node if current is None else _merge_node(current, node)
        for edge in edges:
            if not isinstance(edge, GraphEdge):
                raise GraphStoreError("edges must contain only GraphEdge records")
            current = new_edges.get(edge.id)
            new_edges[edge.id] = edge if current is None else _merge_edge(current, edge)
        self._validate_endpoints(new_nodes, new_edges)
        graph = self._build_graph(new_nodes, new_edges)
        self._nodes = new_nodes
        self._edges = new_edges
        self._graph = graph

    def upsert_node(self, node: GraphNode) -> GraphNode:
        if not isinstance(node, GraphNode):
            raise GraphStoreError("node must be a GraphNode")
        current = self._nodes.get(node.id)
        merged = node if current is None else _merge_node(current, node)
        new_nodes = dict(self._nodes)
        new_nodes[node.id] = merged
        graph = self._build_graph(new_nodes, self._edges)
        self._nodes = new_nodes
        self._graph = graph
        return merged

    def upsert_edge(self, edge: GraphEdge) -> GraphEdge:
        if not isinstance(edge, GraphEdge):
            raise GraphStoreError("edge must be a GraphEdge")
        self._validate_edge_endpoints(edge, self._nodes)
        current = self._edges.get(edge.id)
        merged = edge if current is None else _merge_edge(current, edge)
        new_edges = dict(self._edges)
        new_edges[edge.id] = merged
        graph = self._build_graph(self._nodes, new_edges)
        self._edges = new_edges
        self._graph = graph
        return merged

    def remove_source(self, source_key: str) -> SourceMutationResult:
        """Atomically remove evidence and facts owned only by ``source_key``.

        Edges that would become dangling are removed as well.  The result makes
        that cascading cleanup explicit to callers.
        """

        return self._replace_source(source_key, nodes=(), edges=(), require_owner=False)

    def replace_source(
        self,
        source_key: str,
        *,
        nodes: Iterable[GraphNode] = (),
        edges: Iterable[GraphEdge] = (),
    ) -> SourceMutationResult:
        """Atomically replace every fact observation for one source."""

        return self._replace_source(
            source_key, nodes=nodes, edges=edges, require_owner=True
        )

    def _replace_source(
        self,
        source_key: str,
        *,
        nodes: Iterable[GraphNode],
        edges: Iterable[GraphEdge],
        require_owner: bool,
    ) -> SourceMutationResult:
        if not isinstance(source_key, str) or not source_key.strip():
            raise SourceOwnershipError("source_key must be a non-empty string")
        incoming_nodes = tuple(nodes)
        incoming_edges = tuple(edges)
        for node in incoming_nodes:
            if not isinstance(node, GraphNode):
                raise GraphStoreError(
                    "replacement nodes must contain only GraphNode records"
                )
            if require_owner and not _matching_evidence(node.evidence, source_key):
                raise SourceOwnershipError(
                    f"replacement fact {node.id!r} has no evidence for {source_key!r}"
                )
        for edge in incoming_edges:
            if not isinstance(edge, GraphEdge):
                raise GraphStoreError(
                    "replacement edges must contain only GraphEdge records"
                )
            if require_owner and not _matching_evidence(edge.evidence, source_key):
                raise SourceOwnershipError(
                    f"replacement fact {edge.id!r} has no evidence for {source_key!r}"
                )

        new_nodes, removed_nodes = self._without_source_nodes(source_key)
        new_edges, removed_edges = self._without_source_edges(source_key)

        for node in incoming_nodes:
            current = new_nodes.get(node.id)
            new_nodes[node.id] = node if current is None else _merge_node(current, node)
        for edge in incoming_edges:
            self._validate_edge_endpoints(edge, new_nodes)
            current = new_edges.get(edge.id)
            new_edges[edge.id] = edge if current is None else _merge_edge(current, edge)

        dangling = {
            edge_id
            for edge_id, edge in new_edges.items()
            if edge.source not in new_nodes or edge.target not in new_nodes
        }
        for edge_id in dangling:
            del new_edges[edge_id]
        removed_edges.update(dangling)

        self._validate_endpoints(new_nodes, new_edges)
        graph = self._build_graph(new_nodes, new_edges)
        self._nodes = new_nodes
        self._edges = new_edges
        self._graph = graph
        return SourceMutationResult(
            source_key=source_key,
            removed_nodes=tuple(sorted(removed_nodes - set(new_nodes))),
            removed_edges=tuple(sorted(removed_edges - set(new_edges))),
            retained_nodes=tuple(sorted(new_nodes)),
            retained_edges=tuple(sorted(new_edges)),
        )

    def _without_source_nodes(
        self, source_key: str
    ) -> tuple[dict[str, GraphNode], set[str]]:
        kept: dict[str, GraphNode] = {}
        removed: set[str] = set()
        for node_id, node in self._nodes.items():
            matching = _matching_evidence(node.evidence, source_key)
            if not matching:
                kept[node_id] = node
                continue
            evidence = tuple(item for item in node.evidence if item not in matching)
            if not evidence:
                removed.add(node_id)
                continue
            kept[node_id] = GraphNode(
                id=node.id,
                kind=node.kind,
                attributes=node.attributes,
                evidence=evidence,
            )
        return kept, removed

    def _without_source_edges(
        self, source_key: str
    ) -> tuple[dict[str, GraphEdge], set[str]]:
        kept: dict[str, GraphEdge] = {}
        removed: set[str] = set()
        for edge_id, edge in self._edges.items():
            matching = _matching_evidence(edge.evidence, source_key)
            if not matching:
                kept[edge_id] = edge
                continue
            evidence = tuple(item for item in edge.evidence if item not in matching)
            if not evidence:
                removed.add(edge_id)
                continue
            kept[edge_id] = GraphEdge(
                id=edge.id,
                source=edge.source,
                target=edge.target,
                relation=edge.relation,
                attributes=edge.attributes,
                evidence=evidence,
            )
        return kept, removed

    def to_networkx(self) -> nx.MultiDiGraph:
        """Return a detached directed multigraph for NetworkX algorithms."""

        return self._build_graph(self._nodes, self._edges)

    def search(
        self,
        query: str,
        *,
        kinds: Iterable[str] | None = None,
        limit: int = 20,
    ) -> tuple[GraphNode, ...]:
        """Perform deterministic lexical search over ids, kinds, and attributes."""

        if not isinstance(query, str) or not query.strip():
            raise GraphStoreError("search query must be a non-empty string")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise GraphStoreError("search limit must be a positive integer")
        allowed_kinds = None if kinds is None else frozenset(kinds)
        tokens = tuple(token.casefold() for token in query.split() if token)
        ranked: list[tuple[int, str, GraphNode]] = []
        for node in self._nodes.values():
            if allowed_kinds is not None and node.kind not in allowed_kinds:
                continue
            haystack = " ".join(
                (node.id, node.kind, canonical_json(thaw_json(node.attributes)))
            ).casefold()
            if not all(token in haystack for token in tokens):
                continue
            score = sum(haystack.count(token) for token in tokens)
            ranked.append((-score, node.id, node))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return tuple(item[2] for item in ranked[:limit])

    def find_paths(
        self,
        source: str,
        target: str,
        *,
        max_hops: int = 4,
        relations: Iterable[str] | None = None,
        limit: int = 20,
    ) -> tuple[tuple[str, ...], ...]:
        """Return deterministic directed node paths, optionally relation-filtered."""

        if source not in self._nodes:
            raise UnknownNodeError(f"unknown source node: {source}")
        if target not in self._nodes:
            raise UnknownNodeError(f"unknown target node: {target}")
        if isinstance(max_hops, bool) or not isinstance(max_hops, int) or max_hops < 1:
            raise GraphStoreError("max_hops must be a positive integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise GraphStoreError("path limit must be a positive integer")
        allowed_relations = None if relations is None else frozenset(relations)
        graph = self._graph
        if allowed_relations is not None:
            graph = nx.MultiDiGraph()
            for node_id in sorted(self._nodes):
                graph.add_node(node_id)
            for edge in sorted(self._edges.values(), key=lambda item: item.id):
                if edge.relation in allowed_relations:
                    graph.add_edge(edge.source, edge.target, key=edge.id)
        paths: set[tuple[str, ...]] = set()
        try:
            for path in nx.all_simple_paths(
                graph, source=source, target=target, cutoff=max_hops
            ):
                paths.add(tuple(path))
                if len(paths) >= limit:
                    break
        except nx.NetworkXNoPath:
            return ()
        return tuple(sorted(paths, key=lambda path: (len(path), path)))[:limit]

    def neighbors(
        self,
        node_id: str,
        *,
        direction: str = "out",
        relations: Iterable[str] | None = None,
    ) -> tuple[GraphNode, ...]:
        if node_id not in self._nodes:
            raise UnknownNodeError(f"unknown node: {node_id}")
        if direction not in {"out", "in", "both"}:
            raise GraphStoreError("direction must be 'out', 'in', or 'both'")
        allowed = None if relations is None else frozenset(relations)
        neighbor_ids: set[str] = set()
        for edge in self._edges.values():
            if allowed is not None and edge.relation not in allowed:
                continue
            if direction in {"out", "both"} and edge.source == node_id:
                neighbor_ids.add(edge.target)
            if direction in {"in", "both"} and edge.target == node_id:
                neighbor_ids.add(edge.source)
        return tuple(self._nodes[item] for item in sorted(neighbor_ids))

    def snapshot(self, revision: RepositoryRevision) -> GraphSnapshot:
        return GraphSnapshot(
            revision=revision,
            nodes=self.nodes(),
            edges=self.edges(),
        )

    @classmethod
    def from_snapshot(cls, snapshot: GraphSnapshot) -> NetworkXGraphStore:
        if not isinstance(snapshot, GraphSnapshot):
            raise GraphStoreError("snapshot must be a GraphSnapshot")
        return cls(nodes=snapshot.nodes, edges=snapshot.edges)

    @staticmethod
    def _validate_edge_endpoints(
        edge: GraphEdge, nodes: Mapping[str, GraphNode]
    ) -> None:
        missing = tuple(
            node_id for node_id in (edge.source, edge.target) if node_id not in nodes
        )
        if missing:
            raise MissingEndpointError(
                f"edge {edge.id!r} refers to missing endpoint(s): {missing}"
            )

    @classmethod
    def _validate_endpoints(
        cls,
        nodes: Mapping[str, GraphNode],
        edges: Mapping[str, GraphEdge],
    ) -> None:
        for edge in edges.values():
            cls._validate_edge_endpoints(edge, nodes)

    @staticmethod
    def _build_graph(
        nodes: Mapping[str, GraphNode],
        edges: Mapping[str, GraphEdge],
    ) -> nx.MultiDiGraph:
        graph = nx.MultiDiGraph()
        for node in sorted(nodes.values(), key=lambda item: item.id):
            graph.add_node(
                node.id,
                kind=node.kind,
                attributes=thaw_json(node.attributes),
                evidence=[item.to_dict() for item in node.evidence],
            )
        for edge in sorted(edges.values(), key=lambda item: item.id):
            graph.add_edge(
                edge.source,
                edge.target,
                key=edge.id,
                id=edge.id,
                relation=edge.relation,
                attributes=thaw_json(edge.attributes),
                evidence=[item.to_dict() for item in edge.evidence],
            )
        return graph


__all__ = [
    "DiscoveryGraphStore",
    "FactConflictError",
    "GraphStoreError",
    "MissingEndpointError",
    "NetworkXGraphStore",
    "SourceMutationResult",
    "SourceOwnershipError",
    "UnknownNodeError",
]
