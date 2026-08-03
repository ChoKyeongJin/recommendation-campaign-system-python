"""Public offline API for repository capability discovery.

No runtime planner, compiler, SQL builder, vector store, or GraphRAG service is
imported here.  Approved facts are a read-only projection of existing sources.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .candidates import CapabilityCandidate, CapabilityCandidateGenerator
from .domain import GraphEdge, GraphNode, GraphSnapshot
from .gap_analysis import (
    CapabilityGapAnalyzer,
    CapabilityGapReport,
    validate_gap_report_payload,
)
from .graph_store import NetworkXGraphStore
from .projection import ProjectionIssue, ProjectionResult, build_repository_projection


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _trust_state(node: GraphNode) -> str:
    declared = str(node.attributes.get("trust_state") or "")
    if declared:
        return declared
    states = {item.trust_state for item in node.evidence}
    return next(iter(states)) if len(states) == 1 else "mixed"


@dataclass(frozen=True)
class ConceptSearchResult:
    node_id: str
    kind: str
    trust_state: str
    attributes: Mapping[str, Any]
    evidence_count: int

    @property
    def approved(self) -> bool:
        return self.trust_state == "approved_projection"

    @property
    def executable(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "trust_state": self.trust_state,
            "approved": self.approved,
            "executable": False,
            "attributes": _plain(self.attributes),
            "evidence_count": self.evidence_count,
        }


@dataclass(frozen=True)
class CapabilityEvidenceBundle:
    concept_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]

    @property
    def executable(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "executable": False,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }


@dataclass(frozen=True)
class VerificationResult:
    revision: Mapping[str, Any]
    errors: tuple[Mapping[str, Any], ...]
    warnings: tuple[Mapping[str, Any], ...]
    statistics: Mapping[str, int]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "revision": _plain(self.revision),
            "errors": [_plain(item) for item in self.errors],
            "warnings": [_plain(item) for item in self.warnings],
            "statistics": _plain(self.statistics),
            "runtime_changed": False,
        }


class CapabilityDiscoveryService:
    """Read-only facade over deterministic repository projection and gaps."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        projection: ProjectionResult | None = None,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.projection = projection or build_repository_projection(
            self.repository_root
        )
        self.graph_store = NetworkXGraphStore(
            self.projection.nodes, self.projection.edges
        )
        self._gap_report: CapabilityGapReport | None = None

    @property
    def cannot_execute_sql(self) -> bool:
        return True

    @property
    def cannot_mutate_approved_registry(self) -> bool:
        return True

    def snapshot(self) -> GraphSnapshot:
        return self.graph_store.snapshot(self.projection.revision)

    def search_concepts(
        self,
        query: str,
        *,
        approved_only: bool = False,
        limit: int = 10,
    ) -> list[ConceptSearchResult]:
        if not query.strip():
            return []
        requested = max(limit, 1)
        nodes = self.graph_store.search(query, limit=max(requested * 4, requested))
        results: list[ConceptSearchResult] = []
        for node in nodes:
            trust_state = _trust_state(node)
            if approved_only and trust_state != "approved_projection":
                continue
            results.append(
                ConceptSearchResult(
                    node_id=node.id,
                    kind=node.kind,
                    trust_state=trust_state,
                    attributes=node.attributes,
                    evidence_count=len(node.evidence),
                )
            )
            if len(results) >= requested:
                break
        return results

    def find_capability_evidence(self, concept_id: str) -> CapabilityEvidenceBundle:
        snapshot = self.snapshot()
        node_ids = {node.id for node in snapshot.nodes}
        selected_id = concept_id if concept_id in node_ids else None
        if selected_id is None:
            matches = self.graph_store.search(concept_id, limit=1)
            selected_id = matches[0].id if matches else concept_id
        edge_set = tuple(
            edge
            for edge in snapshot.edges
            if edge.source == selected_id or edge.target == selected_id
        )
        related_ids = {selected_id}
        for edge in edge_set:
            related_ids.update((edge.source, edge.target))
        nodes = tuple(node for node in snapshot.nodes if node.id in related_ids)
        return CapabilityEvidenceBundle(
            concept_id=selected_id, nodes=nodes, edges=edge_set
        )

    def find_gaps(self) -> CapabilityGapReport:
        if self._gap_report is None:
            self._gap_report = CapabilityGapAnalyzer().analyze(self.projection)
        return self._gap_report

    def generate_candidate(self, gap_id: str) -> CapabilityCandidate:
        gap = self.find_gaps().find(gap_id)
        return CapabilityCandidateGenerator().generate(
            gap, revision=self.projection.revision
        )

    def verify(self, *, fail_on_conflict: bool = False) -> VerificationResult:
        errors: list[Mapping[str, Any]] = []
        warnings: list[Mapping[str, Any]] = []
        for issue in self.projection.issues:
            target = errors if issue.severity == "error" else warnings
            target.append(issue.to_dict())

        approved_missing_provenance = 0
        observed_missing_provenance = 0
        for fact in (*self.projection.nodes, *self.projection.edges):
            trust = str(fact.attributes.get("trust_state") or "")
            for evidence in fact.evidence:
                missing = not (
                    evidence.source_path
                    and evidence.source_pointer
                    and evidence.content_hash
                )
                if not missing:
                    continue
                if (
                    trust == "approved_projection"
                    or evidence.trust_state == "approved_projection"
                ):
                    approved_missing_provenance += 1
                else:
                    observed_missing_provenance += 1
        if approved_missing_provenance:
            errors.append(
                {
                    "code": "APPROVED_PROVENANCE_INCOMPLETE",
                    "message": f"{approved_missing_provenance} approved fact evidence record(s) are incomplete",
                }
            )
        if observed_missing_provenance:
            warnings.append(
                {
                    "code": "DISCOVERY_PROVENANCE_INCOMPLETE",
                    "message": f"{observed_missing_provenance} observed fact evidence record(s) are incomplete",
                }
            )

        report = self.find_gaps()
        report_schema_issues = validate_gap_report_payload(report.to_dict())
        if report_schema_issues:
            errors.append(
                {
                    "code": "GAP_REPORT_SCHEMA_INVALID",
                    "message": "; ".join(report_schema_issues),
                }
            )
        conflicts = tuple(
            gap for gap in report.gaps if gap.classification == "CONFLICTING_DEFINITION"
        )
        if conflicts:
            item = {
                "code": "DETERMINISTIC_CONFLICT",
                "message": f"{len(conflicts)} conflicting approved definition(s) require review",
            }
            (errors if fail_on_conflict else warnings).append(item)
        if report.gaps:
            warnings.append(
                {
                    "code": "LEGACY_CANONICAL_GAPS",
                    "message": (
                        f"{len(report.gaps)} legacy coverage axis gap(s) remain; "
                        "discovery warnings do not change runtime support"
                    ),
                }
            )

        return VerificationResult(
            revision=self.projection.revision.to_dict(),
            errors=tuple(errors),
            warnings=tuple(warnings),
            statistics={
                "nodes": len(self.projection.nodes),
                "edges": len(self.projection.edges),
                "projection_issues": len(self.projection.issues),
                "gaps": len(report.gaps),
            },
        )

    def full_rebuild_is_deterministic(self) -> bool:
        """Compare two isolated full projections without persisting either one."""

        rebuilt = build_repository_projection(self.repository_root)
        first = self.snapshot().to_dict()
        second = (
            NetworkXGraphStore(rebuilt.nodes, rebuilt.edges)
            .snapshot(rebuilt.revision)
            .to_dict()
        )
        return first == second


def projection_issues_by_severity(
    issues: Iterable[ProjectionIssue],
) -> dict[str, list[dict[str, Any]]]:
    grouped = {"error": [], "warning": []}
    for issue in issues:
        grouped[issue.severity].append(issue.to_dict())
    return grouped
