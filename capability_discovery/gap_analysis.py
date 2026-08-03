"""Deterministic legacy-to-canonical gap analysis for offline discovery.

The analyzer intentionally consumes the existing P4 coverage inventory.  It
does not invent support from graph similarity and it never emits runtime
failure codes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import Evidence, GapRecord, GraphNode, RepositoryRevision, stable_id
from .policy import DEFAULT_DISCOVERY_POLICY

GAP_CLASSIFICATIONS = frozenset(
    {
        "LEGACY_ONLY",
        "CANONICAL_ONLY",
        "ALIAS_MISSING",
        "PHYSICAL_BINDING_MISSING",
        "OPERATOR_MISMATCH",
        "VALUE_MAPPING_MISSING",
        "LOWERING_MISSING",
        "COMPILER_MISSING",
        "TEST_COVERAGE_MISSING",
        "EXPRESSIBILITY_UNDECLARED",
        "CONFLICTING_DEFINITION",
        "STALE_APPROVED_ASSET",
    }
)


class GapAnalysisError(ValueError):
    """Raised when deterministic inventory input is malformed."""


def _plain(value: Any) -> Any:
    """Return a detached JSON value without relying on mutable graph objects."""

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise GapAnalysisError(
        f"gap inventory contains a non-JSON value: {type(value).__name__}"
    )


@dataclass(frozen=True)
class CapabilityGapReport:
    report_id: str
    revision: RepositoryRevision
    inventory: Mapping[str, Any]
    gaps: tuple[GapRecord, ...]
    schema_version: str = "capability-gap-report/v1"

    def __post_init__(self) -> None:
        if not self.report_id.strip():
            raise GapAnalysisError("report_id is required")
        unknown = sorted(
            {gap.classification for gap in self.gaps} - GAP_CLASSIFICATIONS
        )
        if unknown:
            raise GapAnalysisError(f"unknown gap classifications: {unknown}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "snapshot": self.revision.to_dict(),
            "policy": DEFAULT_DISCOVERY_POLICY.to_dict(),
            "inventory": _plain(self.inventory),
            "gaps": [gap.to_dict() for gap in self.gaps],
        }

    def find(self, gap_id: str) -> GapRecord:
        for gap in self.gaps:
            if gap.gap_id == gap_id:
                return gap
        raise KeyError(f"unknown capability gap: {gap_id}")


class CapabilityGapAnalyzer:
    """Build reliable G0 gaps from the P4 physical-column difference."""

    def analyze(self, projection: Any) -> CapabilityGapReport:
        metadata = getattr(projection, "metadata", None)
        revision = getattr(projection, "revision", None)
        nodes = getattr(projection, "nodes", ())
        if not isinstance(metadata, Mapping):
            raise GapAnalysisError("projection.metadata is required")
        if not isinstance(revision, RepositoryRevision):
            raise GapAnalysisError("projection.revision must be RepositoryRevision")
        inventory = metadata.get("coverage_inventory")
        return self.analyze_inventory(inventory, revision=revision, nodes=nodes)

    def analyze_inventory(
        self,
        inventory: Any,
        *,
        revision: RepositoryRevision,
        nodes: Iterable[GraphNode] = (),
    ) -> CapabilityGapReport:
        normalized = self._validate_inventory(inventory)
        graph_nodes = tuple(nodes)
        gaps = tuple(
            self._legacy_axis_gap(axis, columns, revision, graph_nodes)
            for axis, columns in sorted(normalized["by_axis"].items())
        )
        report_id = stable_id(
            "gap-report",
            revision.content_sha256,
            json.dumps(
                normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
        return CapabilityGapReport(
            report_id=report_id,
            revision=revision,
            inventory=normalized,
            gaps=gaps,
        )

    @staticmethod
    def _validate_inventory(inventory: Any) -> dict[str, Any]:
        if not isinstance(inventory, Mapping):
            raise GapAnalysisError("metadata.coverage_inventory must be an object")
        by_axis = inventory.get("by_axis")
        if not isinstance(by_axis, Mapping):
            raise GapAnalysisError("coverage_inventory.by_axis must be an object")
        normalized_axes: dict[str, list[str]] = {}
        for raw_axis, raw_columns in by_axis.items():
            axis = str(raw_axis).strip()
            if not axis:
                raise GapAnalysisError("coverage axis cannot be empty")
            if not isinstance(raw_columns, (list, tuple)):
                raise GapAnalysisError(
                    f"coverage axis {axis!r} must contain a column list"
                )
            columns = sorted(
                {
                    str(column).strip().upper()
                    for column in raw_columns
                    if str(column).strip()
                }
            )
            if not columns:
                raise GapAnalysisError(f"coverage axis {axis!r} cannot be empty")
            normalized_axes[axis] = columns

        normalized = _plain(inventory)
        normalized["by_axis"] = dict(sorted(normalized_axes.items()))
        normalized["missing_axes"] = len(normalized_axes)
        normalized["missing_columns"] = len(
            {column for columns in normalized_axes.values() for column in columns}
        )
        return normalized

    def _legacy_axis_gap(
        self,
        axis: str,
        columns: list[str],
        revision: RepositoryRevision,
        nodes: tuple[GraphNode, ...],
    ) -> GapRecord:
        assets = tuple(node for node in nodes if self._belongs_to_axis(node, axis))
        evidence = self._deduplicate_evidence(
            item for node in assets for item in node.evidence
        )
        if not evidence:
            inventory_path = "tools/canonical_coverage_inventory.py"
            evidence = (
                Evidence(
                    source_type="code",
                    source_path=inventory_path,
                    source_pointer=f"scan()/by_axis/{axis}",
                    extraction_method="deterministic",
                    confidence=1.0,
                    content_hash=revision.source_hashes.get(inventory_path),
                    revision=revision.git_revision,
                    trust_state="observed",
                ),
            )

        gap_id = stable_id("gap", "LEGACY_ONLY", axis, *columns)
        return GapRecord(
            gap_id=gap_id,
            concept_id=axis,
            classification="LEGACY_ONLY",
            summary=(
                f"legacy execution axis {axis!r} references {len(columns)} physical "
                "column(s) absent from the canonical audience projection"
            ),
            legacy_assets=tuple(sorted(node.id for node in assets)),
            approved_assets=(),
            evidence=evidence,
            details={"axis": axis, "missing_columns": columns},
            blocking_questions=(
                "canonical semantic identifier and owning registry entry are not declared",
                "table, grain, NULL/time semantics, and policy constraints require review",
                "lowering, compiler, and independent test evidence are required before promotion",
            ),
            conflicts=(),
        )

    @staticmethod
    def _belongs_to_axis(node: GraphNode, axis: str) -> bool:
        attrs = node.attributes
        declared = {
            str(attrs.get(key) or "")
            for key in ("axis", "legacy_axis", "coverage_axis", "name")
        }
        if axis in declared:
            return True
        return node.id.endswith(f":{axis}") and node.kind.lower().startswith("legacy")

    @staticmethod
    def _deduplicate_evidence(items: Iterable[Evidence]) -> tuple[Evidence, ...]:
        unique: dict[str, Evidence] = {}
        for item in items:
            key = json.dumps(
                item.to_dict(), ensure_ascii=False, sort_keys=True, default=str
            )
            unique[key] = item
        return tuple(unique[key] for key in sorted(unique))


def validate_gap_report_payload(
    payload: Mapping[str, Any],
    *,
    schema_path: str | Path | None = None,
) -> tuple[str, ...]:
    """Validate the report against its checked-in Draft 2020-12 contract."""

    path = (
        Path(schema_path).resolve()
        if schema_path is not None
        else Path(__file__).resolve().parents[1]
        / "docs"
        / "data"
        / "schemas"
        / "capability_gap_report.schema.json"
    )
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:
        return ("jsonschema dependency is required for gap report validation",)
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as exc:
        return (f"gap report schema is unavailable or invalid: {exc}",)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    return tuple(
        "schema/"
        + "/".join(str(part) for part in error.absolute_path)
        + f": {error.message}"
        for error in errors
    )
