"""Read-only projection of repository capability assets into typed graph facts.

This module is deliberately an *offline* adapter.  It reads the registries and
code contracts that already exist in this repository and emits deterministic
facts with provenance.  It does not import :mod:`graph_rag`, execute SQL, or
write to any approved registry.

The approved half of the projection is a view over the existing canonical
catalogs and executable code contracts.  Legacy registries and the P4
``canonical_coverage_inventory`` result are retained as observed discovery
evidence; they never acquire approved status merely by appearing in the graph.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote

from .domain import Evidence, GraphEdge, GraphNode, RepositoryRevision

APPROVED_TRUST = "approved_projection"
OBSERVED_TRUST = "observed"
EXTRACTION_METHOD = "deterministic"

_AUDIENCE_CATALOG = Path("docs/data/runtime/semantics/audience_catalog.json")
_SEMANTIC_CAPABILITIES = Path("docs/data/runtime/semantics/semantic_capabilities.json")
_MEMBER_TARGET_FILTERS = Path("docs/data/runtime/sql/member_target_filters.json")
_MEMBER_METRICS = Path("docs/data/runtime/sql/member_metrics.json")
_METRIC_DIRECTORY = Path("docs/data/runtime/sql/metrics")
_COVERAGE_INVENTORY = Path("tools/canonical_coverage_inventory.py")
_SEMANTIC_PLAN = Path("semantic_plan.py")
_EVENT_LOWERING = Path("semantic_plan_event_lowering.py")
_CAPABILITY_VALIDATION = Path("capability_validation.py")
_TARGETING_IR = Path("targeting_ir.py")
_DISCOVERY_SCHEMAS = (
    Path("docs/data/schemas/capability_gap_report.schema.json"),
    Path("docs/data/schemas/capability_candidate.schema.json"),
)


def _plain(value: Any) -> Any:
    """Return a deterministic JSON-compatible copy of ``value``."""

    if isinstance(value, Mapping):
        return {
            str(key): _plain(item)
            for key, item in sorted(value.items(), key=lambda x: str(x[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        values = [_plain(item) for item in value]
        return (
            sorted(
                values,
                key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
            )
            if isinstance(value, (set, frozenset))
            else values
        )
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _token(value: Any) -> str:
    """Encode an identifier component without silently normalising its case."""

    text = str(value or "").strip()
    return quote(text, safe="._:-") or "unknown"


def _stable_hash(*parts: Any, length: int = 20) -> str:
    payload = json.dumps(
        [_plain(part) for part in parts], ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bare_column(value: Any) -> str:
    return str(value or "").strip().rpartition(".")[2].upper()


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


@dataclass(frozen=True)
class ProjectionIssue:
    """A source problem that must remain visible to callers.

    Missing discovery-only inputs are warnings; missing approved catalogs and
    malformed deterministic contracts are errors.  The projection remains
    inspectable in either case so CI and review tools can explain the problem.
    """

    code: str
    message: str
    severity: str = "error"
    source_path: str | None = None
    source_pointer: str = ""

    def __post_init__(self) -> None:
        if self.severity not in {"error", "warning"}:
            raise ValueError(f"invalid projection issue severity: {self.severity!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "source_path": self.source_path,
            "source_pointer": self.source_pointer,
        }


@dataclass(frozen=True)
class ProjectionResult:
    """Immutable, non-executable repository projection."""

    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    issues: tuple[ProjectionIssue, ...]
    revision: RepositoryRevision
    metadata: Mapping[str, Any] = field(default_factory=dict)
    cannot_execute_sql: bool = field(default=True, init=False)
    cannot_mutate_approved_registry: bool = field(default=True, init=False)

    @property
    def ok(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "cannot_execute_sql": True,
            "cannot_mutate_approved_registry": True,
            "revision": self.revision.to_dict(),
            "metadata": _plain(self.metadata),
            "issues": [issue.to_dict() for issue in self.issues],
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }


@dataclass(frozen=True)
class _Source:
    root: Path
    path: Path
    relative_path: str
    content_hash: str
    revision: str | None
    trust_state: str
    source_type: str

    def evidence(
        self,
        pointer: str = "",
        *,
        confidence: float = 1.0,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> Evidence:
        location = pointer
        if line_start is not None:
            location = f"{pointer}#L{line_start}"
            if line_end is not None and line_end != line_start:
                location += f"-L{line_end}"
        return Evidence(
            source_type=self.source_type,
            source_path=self.relative_path,
            source_pointer=location,
            extraction_method=EXTRACTION_METHOD,
            confidence=confidence,
            content_hash=self.content_hash,
            revision=self.revision,
            trust_state=self.trust_state,
        )


class _Collector:
    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.issues: list[ProjectionIssue] = []
        self.source_hashes: dict[str, str] = {}

    def issue(
        self,
        code: str,
        message: str,
        *,
        severity: str = "error",
        source_path: str | None = None,
        source_pointer: str = "",
    ) -> None:
        self.issues.append(
            ProjectionIssue(
                code=code,
                message=message,
                severity=severity,
                source_path=source_path,
                source_pointer=source_pointer,
            )
        )

    def source(
        self,
        root: Path,
        relative: Path,
        *,
        revision: str | None,
        trust_state: str,
        source_type: str,
        required: bool,
    ) -> _Source | None:
        path = root / relative
        relative_text = relative.as_posix()
        if not path.is_file():
            self.issue(
                "SOURCE_MISSING",
                f"capability projection input does not exist: {relative_text}",
                severity="error" if required else "warning",
                source_path=relative_text,
            )
            return None
        digest = _content_hash(path)
        self.source_hashes[relative_text] = digest
        return _Source(
            root=root,
            path=path,
            relative_path=relative_text,
            content_hash=digest,
            revision=revision,
            trust_state=trust_state,
            source_type=source_type,
        )

    def json_source(
        self,
        root: Path,
        relative: Path,
        *,
        revision: str | None,
        trust_state: str,
        required: bool,
    ) -> tuple[_Source, Mapping[str, Any]] | None:
        source = self.source(
            root,
            relative,
            revision=revision,
            trust_state=trust_state,
            source_type="config",
            required=required,
        )
        if source is None:
            return None
        try:
            payload = json.loads(source.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.issue(
                "SOURCE_INVALID",
                f"cannot parse JSON capability source: {exc}",
                source_path=source.relative_path,
            )
            return None
        if not isinstance(payload, Mapping):
            self.issue(
                "SOURCE_SHAPE_INVALID",
                "capability JSON source must contain an object at its root",
                source_path=source.relative_path,
            )
            return None
        return source, payload

    def node(
        self,
        node_id: str,
        kind: str,
        attributes: Mapping[str, Any],
        evidence: Evidence,
    ) -> GraphNode:
        current = self.nodes.get(node_id)
        attrs = _plain(attributes)
        if current is None:
            node = GraphNode(
                id=node_id, kind=kind, attributes=attrs, evidence=(evidence,)
            )
            self.nodes[node_id] = node
            return node
        if current.kind != kind:
            self.issue(
                "NODE_KIND_CONFLICT",
                f"node {node_id!r} was observed as both {current.kind!r} and {kind!r}",
                severity="warning",
                source_path=evidence.source_path,
                source_pointer=evidence.source_pointer,
            )
            return current
        merged = dict(current.attributes)
        for key, value in attrs.items():
            if key not in merged or merged[key] in (None, "", [], {}):
                merged[key] = value
            elif merged[key] != value and key != "trust_state":
                conflicts = dict(merged.get("observed_conflicts") or {})
                variants = list(conflicts.get(key) or [merged[key]])
                if value not in variants:
                    variants.append(value)
                conflicts[key] = variants
                merged["observed_conflicts"] = conflicts
        if attrs.get("trust_state") == APPROVED_TRUST:
            merged["trust_state"] = APPROVED_TRUST
        evidences = tuple(
            sorted(
                {*current.evidence, evidence},
                key=lambda item: (
                    item.source_path,
                    item.source_pointer,
                    item.trust_state,
                ),
            )
        )
        node = GraphNode(id=node_id, kind=kind, attributes=merged, evidence=evidences)
        self.nodes[node_id] = node
        return node

    def edge(
        self,
        source: str,
        target: str,
        relation: str,
        evidence: Evidence,
        attributes: Mapping[str, Any] | None = None,
    ) -> GraphEdge:
        attrs = _plain(attributes or {})
        edge_id = f"edge:{relation.lower()}:{_stable_hash(source, target, relation, attrs, evidence.source_path, evidence.source_pointer)}"
        edge = GraphEdge(
            id=edge_id,
            source=source,
            target=target,
            relation=relation,
            attributes=attrs,
            evidence=(evidence,),
        )
        self.edges[edge_id] = edge
        return edge


def _git_state(root: Path) -> tuple[str | None, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        )
        return commit or None, dirty
    except (OSError, subprocess.SubprocessError):
        return None, False


def _record_discovery_implementation(
    collector: _Collector,
    root: Path,
    revision: str | None,
) -> tuple[str, ...]:
    """Hash extractors and contracts so dirty snapshots remain identifiable."""

    package = root / "capability_discovery"
    relatives = [
        path.relative_to(root)
        for path in sorted(package.glob("*.py"))
        if path.is_file()
    ]
    relatives.extend(_DISCOVERY_SCHEMAS)
    recorded: list[str] = []
    for relative in relatives:
        source = collector.source(
            root,
            relative,
            revision=revision,
            trust_state=OBSERVED_TRUST,
            source_type="code" if relative.suffix == ".py" else "config",
            required=True,
        )
        if source is not None:
            recorded.append(source.relative_path)
    return tuple(recorded)


def _table_node(
    collector: _Collector,
    table: str,
    source: _Source,
    pointer: str,
) -> str:
    table_name = str(table or "").strip().upper()
    node_id = f"physical:table:{_token(table_name)}"
    collector.node(
        node_id,
        "PhysicalTable",
        {"name": table_name, "trust_state": source.trust_state},
        source.evidence(pointer),
    )
    return node_id


def _column_name_node(
    collector: _Collector,
    column: str,
    source: _Source,
    pointer: str,
) -> str:
    column_name = _bare_column(column)
    node_id = f"physical:column-name:{_token(column_name)}"
    collector.node(
        node_id,
        "PhysicalColumnName",
        {"name": column_name, "trust_state": source.trust_state},
        source.evidence(pointer),
    )
    return node_id


def _column_node(
    collector: _Collector,
    table: str,
    column: str,
    source: _Source,
    pointer: str,
) -> str:
    table_name = str(table or "unknown").strip().upper()
    column_name = _bare_column(column)
    node_id = f"physical:column:{_token(table_name)}.{_token(column_name)}"
    evidence = source.evidence(pointer)
    collector.node(
        node_id,
        "PhysicalColumn",
        {
            "table": table_name,
            "name": column_name,
            "trust_state": source.trust_state,
        },
        evidence,
    )
    table_id = _table_node(collector, table_name, source, pointer)
    name_id = _column_name_node(collector, column_name, source, pointer)
    collector.edge(table_id, node_id, "HAS_COLUMN", evidence)
    collector.edge(node_id, name_id, "HAS_COLUMN_NAME", evidence)
    return node_id


def _surface_term(
    collector: _Collector,
    owner_id: str,
    term: Any,
    source: _Source,
    pointer: str,
    *,
    approved: bool,
) -> None:
    text = str(term or "").strip()
    if not text:
        return
    prefix = "approved" if approved else "observed"
    node_id = f"{prefix}:surface-term:{_stable_hash(owner_id, text)}"
    evidence = source.evidence(pointer)
    collector.node(
        node_id,
        "SurfaceTerm",
        {"text": text, "trust_state": source.trust_state},
        evidence,
    )
    collector.edge(
        node_id,
        owner_id,
        "ALIAS_OF" if approved else "CANDIDATE_ALIAS_OF",
        evidence,
    )


def _operator(
    collector: _Collector,
    owner_id: str,
    operator: Any,
    source: _Source,
    pointer: str,
    *,
    approved: bool,
) -> None:
    value = str(operator or "").strip()
    if not value:
        return
    prefix = "canonical" if approved else "observed"
    node_id = f"{prefix}:operator:{_token(value)}"
    evidence = source.evidence(pointer)
    collector.node(
        node_id,
        "Operator",
        {"value": value, "trust_state": source.trust_state},
        evidence,
    )
    collector.edge(owner_id, node_id, "USES_OPERATOR", evidence)


def _pointer_part(value: Any) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _extract_audience_catalog(
    collector: _Collector,
    root: Path,
    revision: str | None,
) -> None:
    loaded = collector.json_source(
        root,
        _AUDIENCE_CATALOG,
        revision=revision,
        trust_state=APPROVED_TRUST,
        required=True,
    )
    if loaded is None:
        return
    source, catalog = loaded

    subject = catalog.get("subject")
    if not isinstance(subject, Mapping):
        collector.issue(
            "CATALOG_SECTION_MISSING",
            "audience catalog has no object-valued subject declaration",
            source_path=source.relative_path,
            source_pointer="/subject",
        )
        subject = {}
    subject_name = str(subject.get("name") or "subject")
    subject_id = f"canonical:source:{_token(subject_name)}"
    subject_evidence = source.evidence("/subject")
    collector.node(
        subject_id,
        "EventSource",
        {
            "canonical_id": subject_name,
            "role": "subject",
            "alias": subject.get("alias"),
            "label": subject.get("label"),
            "trust_state": APPROVED_TRUST,
        },
        subject_evidence,
    )
    subject_table = str(subject.get("table") or "").strip()
    if subject_table:
        table_id = _table_node(collector, subject_table, source, "/subject/table")
        collector.edge(
            subject_id, table_id, "BINDS_TO", source.evidence("/subject/table")
        )
        subject_key = str(subject.get("key") or "").strip()
        if subject_key:
            column_id = _column_node(
                collector,
                subject_table,
                subject_key,
                source,
                "/subject/key",
            )
            collector.edge(
                subject_id,
                column_id,
                "USES_COLUMN",
                source.evidence("/subject/key"),
                {"role": "subject_key"},
            )
    else:
        collector.issue(
            "PHYSICAL_BINDING_MISSING",
            "canonical subject has no physical table",
            source_path=source.relative_path,
            source_pointer="/subject/table",
        )

    vocabularies = catalog.get("value_domains")
    if not isinstance(vocabularies, Mapping):
        collector.issue(
            "CATALOG_SECTION_MISSING",
            "audience catalog has no object-valued value_domains section",
            source_path=source.relative_path,
            source_pointer="/value_domains",
        )
        vocabularies = {}
    for vocabulary_id, raw in sorted(
        vocabularies.items(), key=lambda item: str(item[0])
    ):
        if str(vocabulary_id).startswith("_") or not isinstance(raw, Mapping):
            continue
        pointer = f"/value_domains/{_pointer_part(vocabulary_id)}"
        node_id = f"canonical:value-vocabulary:{_token(vocabulary_id)}"
        collector.node(
            node_id,
            "ValueVocabulary",
            {
                "canonical_id": str(vocabulary_id),
                "label": raw.get("label"),
                "source_category": raw.get("source_category"),
                "ordered": bool(raw.get("ordered", False)),
                "trust_state": APPROVED_TRUST,
            },
            source.evidence(pointer),
        )
        values = raw.get("values")
        if not isinstance(values, Mapping):
            continue
        for value_id, value_spec in sorted(
            values.items(), key=lambda item: str(item[0])
        ):
            if not isinstance(value_spec, Mapping):
                continue
            value_pointer = f"{pointer}/values/{_pointer_part(value_id)}"
            canonical_value_id = (
                f"canonical:value:{_token(vocabulary_id)}:{_token(value_id)}"
            )
            collector.node(
                canonical_value_id,
                "CanonicalValue",
                {
                    "canonical_value": str(value_id),
                    "physical_value": value_spec.get("physical"),
                    "trust_state": APPROVED_TRUST,
                },
                source.evidence(value_pointer),
            )
            collector.edge(
                canonical_value_id,
                node_id,
                "DEFINED_IN",
                source.evidence(value_pointer),
            )
            if value_spec.get("physical") is not None:
                mapping_id = (
                    f"canonical:value-mapping:{_token(vocabulary_id)}:"
                    f"{_token(value_id)}"
                )
                collector.node(
                    mapping_id,
                    "ValueMapping",
                    {
                        "canonical": str(value_id),
                        "physical": value_spec.get("physical"),
                        "trust_state": APPROVED_TRUST,
                    },
                    source.evidence(f"{value_pointer}/physical"),
                )
                collector.edge(
                    canonical_value_id,
                    mapping_id,
                    "MAPS_VALUE_TO",
                    source.evidence(f"{value_pointer}/physical"),
                )
            for index, alias in enumerate(value_spec.get("aliases") or []):
                _surface_term(
                    collector,
                    canonical_value_id,
                    alias,
                    source,
                    f"{value_pointer}/aliases/{index}",
                    approved=True,
                )

    sources = catalog.get("sources")
    if not isinstance(sources, Mapping):
        collector.issue(
            "CATALOG_SECTION_MISSING",
            "audience catalog has no object-valued sources section",
            source_path=source.relative_path,
            source_pointer="/sources",
        )
        sources = {}
    source_tables: dict[str, str] = {
        subject_name: subject_table,
        "subject": subject_table,
    }
    for source_id, raw in sorted(sources.items(), key=lambda item: str(item[0])):
        if not isinstance(raw, Mapping):
            continue
        pointer = f"/sources/{_pointer_part(source_id)}"
        node_id = f"canonical:source:{_token(source_id)}"
        table = str(raw.get("table") or "").strip()
        source_tables[str(source_id)] = table
        collector.node(
            node_id,
            "EventSource",
            {
                "canonical_id": str(source_id),
                "alias": raw.get("alias"),
                "binding": raw.get("binding"),
                "grain": raw.get("grain"),
                "time_format": raw.get("time_format"),
                "label": raw.get("label"),
                "trust_state": APPROVED_TRUST,
            },
            source.evidence(pointer),
        )
        if table:
            table_id = _table_node(collector, table, source, f"{pointer}/table")
            collector.edge(
                node_id, table_id, "BINDS_TO", source.evidence(f"{pointer}/table")
            )
        else:
            collector.issue(
                "PHYSICAL_BINDING_MISSING",
                f"canonical source {source_id!r} has no physical table",
                source_path=source.relative_path,
                source_pointer=f"{pointer}/table",
            )
        for role, key in (
            ("subject_key", "subject_key"),
            ("event_subject_key", "event_subject_key"),
            ("time", "time_column"),
        ):
            column = str(raw.get(key) or "").strip()
            if table and column:
                column_id = _column_node(
                    collector,
                    table,
                    column,
                    source,
                    f"{pointer}/{key}",
                )
                collector.edge(
                    node_id,
                    column_id,
                    "USES_COLUMN",
                    source.evidence(f"{pointer}/{key}"),
                    {"role": role},
                )
        for index, alias in enumerate(raw.get("aliases") or []):
            _surface_term(
                collector,
                node_id,
                alias,
                source,
                f"{pointer}/aliases/{index}",
                approved=True,
            )

    fields = catalog.get("fields")
    if not isinstance(fields, Mapping):
        collector.issue(
            "CATALOG_SECTION_MISSING",
            "audience catalog has no object-valued fields section",
            source_path=source.relative_path,
            source_pointer="/fields",
        )
        fields = {}
    for field_id, raw in sorted(fields.items(), key=lambda item: str(item[0])):
        if not isinstance(raw, Mapping):
            continue
        pointer = f"/fields/{_pointer_part(field_id)}"
        node_id = f"canonical:field:{_token(field_id)}"
        source_id = str(raw.get("source") or "").strip()
        collector.node(
            node_id,
            "ProfileAttribute",
            {
                "canonical_id": str(field_id),
                "data_type": raw.get("data_type"),
                "unit": raw.get("unit"),
                "label": raw.get("label"),
                "trust_state": APPROVED_TRUST,
            },
            source.evidence(pointer),
        )
        if source_id:
            canonical_source_id = f"canonical:source:{_token(source_id)}"
            if canonical_source_id not in collector.nodes:
                collector.issue(
                    "CATALOG_REFERENCE_MISSING",
                    f"field {field_id!r} refers to unknown source {source_id!r}",
                    source_path=source.relative_path,
                    source_pointer=f"{pointer}/source",
                )
            else:
                collector.edge(
                    node_id,
                    canonical_source_id,
                    "DEFINED_IN",
                    source.evidence(f"{pointer}/source"),
                )
        column = str(raw.get("column") or "").strip()
        table = source_tables.get(source_id, "")
        if column and table:
            column_id = _column_node(
                collector,
                table,
                column,
                source,
                f"{pointer}/column",
            )
            collector.edge(
                node_id,
                column_id,
                "BINDS_TO",
                source.evidence(f"{pointer}/column"),
            )
        else:
            collector.issue(
                "PHYSICAL_BINDING_MISSING",
                f"canonical field {field_id!r} has no resolvable table/column binding",
                source_path=source.relative_path,
                source_pointer=pointer,
            )
        vocabulary = str(raw.get("value_domain") or "").strip()
        if vocabulary:
            vocabulary_id = f"canonical:value-vocabulary:{_token(vocabulary)}"
            if vocabulary_id not in collector.nodes:
                collector.issue(
                    "CATALOG_REFERENCE_MISSING",
                    f"field {field_id!r} refers to unknown value domain {vocabulary!r}",
                    source_path=source.relative_path,
                    source_pointer=f"{pointer}/value_domain",
                )
            else:
                collector.edge(
                    node_id,
                    vocabulary_id,
                    "USES_VOCABULARY",
                    source.evidence(f"{pointer}/value_domain"),
                )
        for index, alias in enumerate(raw.get("aliases") or []):
            _surface_term(
                collector,
                node_id,
                alias,
                source,
                f"{pointer}/aliases/{index}",
                approved=True,
            )

    metrics = catalog.get("metrics")
    if not isinstance(metrics, Mapping):
        collector.issue(
            "CATALOG_SECTION_MISSING",
            "audience catalog has no object-valued metrics section",
            source_path=source.relative_path,
            source_pointer="/metrics",
        )
        metrics = {}
    for metric_id, raw in sorted(metrics.items(), key=lambda item: str(item[0])):
        if str(metric_id).startswith("_") or not isinstance(raw, Mapping):
            continue
        pointer = f"/metrics/{_pointer_part(metric_id)}"
        node_id = f"canonical:metric:{_token(metric_id)}"
        collector.node(
            node_id,
            "Metric",
            {
                "canonical_id": str(metric_id),
                "kind": raw.get("kind"),
                "data_type": raw.get("data_type"),
                "grain": raw.get("grain"),
                "time": raw.get("time"),
                "coverage": raw.get("coverage"),
                "label": raw.get("label"),
                "trust_state": APPROVED_TRUST,
            },
            source.evidence(pointer),
        )
        source_id = str(raw.get("source") or "").strip()
        if source_id:
            source_node_id = f"canonical:source:{_token(source_id)}"
            if source_node_id in collector.nodes:
                collector.edge(
                    node_id,
                    source_node_id,
                    "READS_FROM",
                    source.evidence(f"{pointer}/source"),
                )
            else:
                collector.issue(
                    "CATALOG_REFERENCE_MISSING",
                    f"metric {metric_id!r} refers to unknown source {source_id!r}",
                    source_path=source.relative_path,
                    source_pointer=f"{pointer}/source",
                )
        for key in ("expression_field", "prev_expression_field"):
            field_ref = str(raw.get(key) or "").strip()
            if not field_ref:
                continue
            field_node_id = f"canonical:field:{_token(field_ref)}"
            if field_node_id in collector.nodes:
                collector.edge(
                    node_id,
                    field_node_id,
                    "READS_FROM",
                    source.evidence(f"{pointer}/{key}"),
                    {"role": key},
                )
            else:
                collector.issue(
                    "CATALOG_REFERENCE_MISSING",
                    f"metric {metric_id!r} refers to unknown field {field_ref!r}",
                    source_path=source.relative_path,
                    source_pointer=f"{pointer}/{key}",
                )
        for index, operator in enumerate(raw.get("allowed_operators") or []):
            _operator(
                collector,
                node_id,
                operator,
                source,
                f"{pointer}/allowed_operators/{index}",
                approved=True,
            )
        for index, alias in enumerate(raw.get("aliases") or []):
            _surface_term(
                collector,
                node_id,
                alias,
                source,
                f"{pointer}/aliases/{index}",
                approved=True,
            )

    coverage = catalog.get("signal_coverage")
    if isinstance(coverage, Mapping):
        for coverage_id, raw in sorted(coverage.items(), key=lambda item: str(item[0])):
            if str(coverage_id).startswith("_") or not isinstance(raw, Mapping):
                continue
            pointer = f"/signal_coverage/{_pointer_part(coverage_id)}"
            node_id = f"canonical:coverage-policy:{_token(coverage_id)}"
            collector.node(
                node_id,
                "CoveragePolicy",
                {
                    "canonical_id": str(coverage_id),
                    "expressible": raw.get("expressible", True),
                    "note": raw.get("note"),
                    "trust_state": APPROVED_TRUST,
                },
                source.evidence(pointer),
            )
            for key, prefix in (
                ("fields", "canonical:field:"),
                ("sources", "canonical:source:"),
                ("negated_sources", "canonical:source:"),
            ):
                for index, ref in enumerate(raw.get(key) or []):
                    target = f"{prefix}{_token(ref)}"
                    if target in collector.nodes:
                        collector.edge(
                            node_id,
                            target,
                            "REQUIRES_DATA",
                            source.evidence(f"{pointer}/{key}/{index}"),
                            {
                                "polarity": "negative"
                                if key == "negated_sources"
                                else "positive"
                            },
                        )


def _extract_semantic_capabilities(
    collector: _Collector,
    root: Path,
    revision: str | None,
) -> None:
    loaded = collector.json_source(
        root,
        _SEMANTIC_CAPABILITIES,
        revision=revision,
        trust_state=APPROVED_TRUST,
        required=True,
    )
    if loaded is None:
        return
    source, catalog = loaded
    policy = str(catalog.get("data_availability_policy") or "").strip()
    policy_id = "canonical:policy:data-availability"
    collector.node(
        policy_id,
        "Policy",
        {
            "canonical_id": "data_availability_policy",
            "mode": policy or None,
            "trust_state": APPROVED_TRUST,
        },
        source.evidence("/data_availability_policy"),
    )
    if policy not in {"advise", "block"}:
        collector.issue(
            "POLICY_VALUE_INVALID",
            f"unknown data_availability_policy: {policy!r}",
            source_path=source.relative_path,
            source_pointer="/data_availability_policy",
        )

    grains = catalog.get("grains")
    if not isinstance(grains, Mapping):
        collector.issue(
            "CATALOG_SECTION_MISSING",
            "semantic capabilities catalog has no object-valued grains section",
            source_path=source.relative_path,
            source_pointer="/grains",
        )
        grains = {}
    for grain_id, raw in sorted(grains.items(), key=lambda item: str(item[0])):
        if not isinstance(raw, Mapping):
            continue
        pointer = f"/grains/{_pointer_part(grain_id)}"
        coverage = (
            raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
        )
        collector.node(
            f"canonical:grain:{_token(grain_id)}",
            "Grain",
            {
                "canonical_id": str(grain_id),
                "label": raw.get("label"),
                "coverage": _plain(coverage),
                "note": raw.get("note"),
                "trust_state": APPROVED_TRUST,
            },
            source.evidence(pointer),
        )

    node_types = catalog.get("node_types")
    if not isinstance(node_types, Mapping):
        collector.issue(
            "CATALOG_SECTION_MISSING",
            "semantic capabilities catalog has no object-valued node_types section",
            source_path=source.relative_path,
            source_pointer="/node_types",
        )
        node_types = {}
    for node_type, raw in sorted(node_types.items(), key=lambda item: str(item[0])):
        if not isinstance(raw, Mapping):
            continue
        pointer = f"/node_types/{_pointer_part(node_type)}"
        node_id = f"canonical:semantic-plan-node-type:{_token(node_type)}"
        collector.node(
            node_id,
            "SemanticPlanNodeType",
            {
                "canonical_id": str(node_type),
                "semantic_supported": raw.get("semantic_supported"),
                "compiler_supported": raw.get("compiler_supported"),
                "executor_supported": raw.get("executor_supported"),
                "unsupported_message": raw.get("unsupported_message"),
                "trust_state": APPROVED_TRUST,
            },
            source.evidence(pointer),
        )
        grain = raw.get("required_grain")
        if grain:
            grain_id = f"canonical:grain:{_token(grain)}"
            if grain_id in collector.nodes:
                collector.edge(
                    node_id,
                    grain_id,
                    "REQUIRES_DATA",
                    source.evidence(f"{pointer}/required_grain"),
                )
            else:
                collector.issue(
                    "CATALOG_REFERENCE_MISSING",
                    f"node type {node_type!r} refers to unknown grain {grain!r}",
                    source_path=source.relative_path,
                    source_pointer=f"{pointer}/required_grain",
                )
        operators = raw.get("operators")
        if isinstance(operators, Mapping):
            for operator in sorted(operators):
                _operator(
                    collector,
                    node_id,
                    operator,
                    source,
                    f"{pointer}/operators/{_pointer_part(operator)}",
                    approved=True,
                )
        collector.edge(
            node_id,
            policy_id,
            "RESTRICTED_BY",
            source.evidence(pointer),
        )

    metrics = catalog.get("metrics")
    if isinstance(metrics, Mapping):
        for metric_id, raw in sorted(metrics.items(), key=lambda item: str(item[0])):
            if str(metric_id).startswith("_") or not isinstance(raw, Mapping):
                continue
            pointer = f"/metrics/{_pointer_part(metric_id)}"
            node_id = f"canonical:metric:{_token(metric_id)}"
            collector.node(
                node_id,
                "Metric",
                {
                    "canonical_id": str(metric_id),
                    "declared_grain": raw.get("grain"),
                    "trust_state": APPROVED_TRUST,
                },
                source.evidence(pointer),
            )
            grain = raw.get("grain")
            grain_id = f"canonical:grain:{_token(grain)}"
            if grain and grain_id in collector.nodes:
                collector.edge(
                    node_id,
                    grain_id,
                    "REQUIRES_DATA",
                    source.evidence(f"{pointer}/grain"),
                )


def _legacy_binding(
    collector: _Collector,
    owner_id: str,
    raw_column: Any,
    table: str,
    source: _Source,
    pointer: str,
) -> None:
    column = _bare_column(raw_column)
    if not column:
        return
    if table:
        column_id = _column_node(collector, table, column, source, pointer)
        collector.edge(owner_id, column_id, "BINDS_TO", source.evidence(pointer))
    else:
        column_id = _column_name_node(collector, column, source, pointer)
        collector.edge(owner_id, column_id, "USES_COLUMN", source.evidence(pointer))


def _extract_member_target_filters(
    collector: _Collector,
    root: Path,
    revision: str | None,
) -> None:
    loaded = collector.json_source(
        root,
        _MEMBER_TARGET_FILTERS,
        revision=revision,
        trust_state=OBSERVED_TRUST,
        required=False,
    )
    if loaded is None:
        return
    source, registry = loaded
    base = registry.get("base_entity")
    base_table = str(base.get("table") or "") if isinstance(base, Mapping) else ""
    alias_tables: dict[str, str] = {}
    if isinstance(base, Mapping) and base_table:
        alias_tables[str(base.get("alias") or "")] = base_table
        base_id = "legacy:asset:member-target-base-entity"
        collector.node(
            base_id,
            "LegacySymbol",
            {
                "legacy_id": "base_entity",
                "asset_kind": "base_entity",
                "trust_state": OBSERVED_TRUST,
            },
            source.evidence("/base_entity"),
        )
        table_id = _table_node(collector, base_table, source, "/base_entity/table")
        collector.edge(
            base_id, table_id, "BINDS_TO", source.evidence("/base_entity/table")
        )

    # These two sections carry explicit symbolic names, value mappings and
    # aliases.  Other, nested target sections are represented without lossy
    # heuristics by the P4 coverage inventory below.
    for section in ("eq_filters", "boolean_filters"):
        entries = registry.get(section)
        if entries is None:
            collector.issue(
                "LEGACY_SECTION_MISSING",
                f"legacy member target registry has no {section!r} section",
                severity="warning",
                source_path=source.relative_path,
                source_pointer=f"/{section}",
            )
            continue
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            collector.issue(
                "LEGACY_SECTION_INVALID",
                f"legacy member target section {section!r} must be an array",
                severity="warning",
                source_path=source.relative_path,
                source_pointer=f"/{section}",
            )
            continue
        for index, raw in enumerate(entries):
            if not isinstance(raw, Mapping):
                continue
            canonical = str(raw.get("canonical") or f"item-{index}")
            pointer = f"/{section}/{index}"
            node_id = f"legacy:symbol:{_token(section)}:{_token(canonical)}"
            collector.node(
                node_id,
                "LegacySymbol",
                {
                    "legacy_id": canonical,
                    "asset_kind": section,
                    "category": raw.get("category"),
                    "rank": raw.get("rank"),
                    "trust_state": OBSERVED_TRUST,
                },
                source.evidence(pointer),
            )
            raw_column = str(raw.get("column") or "")
            alias = raw_column.partition(".")[0] if "." in raw_column else ""
            _legacy_binding(
                collector,
                node_id,
                raw_column,
                alias_tables.get(alias, base_table),
                source,
                f"{pointer}/column",
            )
            for alias_index, alias_term in enumerate(raw.get("synonyms") or []):
                _surface_term(
                    collector,
                    node_id,
                    alias_term,
                    source,
                    f"{pointer}/synonyms/{alias_index}",
                    approved=False,
                )
            mapping_keys = ("value", "true_value", "false_value")
            mapping = {
                key: raw.get(key) for key in mapping_keys if raw.get(key) is not None
            }
            if mapping:
                mapping_id = (
                    f"legacy:value-mapping:{_token(section)}:{_token(canonical)}"
                )
                collector.node(
                    mapping_id,
                    "ValueMapping",
                    {
                        "mapping": mapping,
                        "trust_state": OBSERVED_TRUST,
                    },
                    source.evidence(pointer),
                )
                collector.edge(
                    node_id,
                    mapping_id,
                    "MAPS_VALUE_TO",
                    source.evidence(pointer),
                )

    # Preserve every top-level execution axis as a typed discovery node even
    # when its nested schema is bespoke.  P4 adds its concrete physical-column
    # evidence to the same node IDs.
    for axis, raw in sorted(registry.items(), key=lambda item: str(item[0])):
        if str(axis).startswith("_"):
            continue
        pointer = f"/{_pointer_part(axis)}"
        collector.node(
            f"legacy:coverage-axis:{_token(axis)}",
            "LegacySymbol",
            {
                "legacy_id": str(axis),
                "asset_kind": "coverage_axis",
                "shape": (
                    "object"
                    if isinstance(raw, Mapping)
                    else "array"
                    if isinstance(raw, list)
                    else type(raw).__name__
                ),
                "trust_state": OBSERVED_TRUST,
            },
            source.evidence(pointer),
        )


def _extract_member_metrics(
    collector: _Collector,
    root: Path,
    revision: str | None,
) -> None:
    loaded = collector.json_source(
        root,
        _MEMBER_METRICS,
        revision=revision,
        trust_state=OBSERVED_TRUST,
        required=False,
    )
    if loaded is None:
        return
    source, registry = loaded
    table = str(registry.get("value_table") or "").strip()
    metrics = registry.get("metrics")
    if not isinstance(metrics, Sequence) or isinstance(metrics, (str, bytes)):
        collector.issue(
            "LEGACY_SECTION_INVALID",
            "member_metrics.json must contain an array-valued metrics section",
            severity="warning",
            source_path=source.relative_path,
            source_pointer="/metrics",
        )
        return
    for index, raw in enumerate(metrics):
        if not isinstance(raw, Mapping):
            continue
        metric_id = str(raw.get("metric_id") or f"item-{index}")
        pointer = f"/metrics/{index}"
        node_id = f"legacy:metric:{_token(metric_id)}"
        collector.node(
            node_id,
            "Metric",
            {
                "legacy_id": metric_id,
                "aggregation": raw.get("agg"),
                "label": raw.get("ko_label"),
                "grain_filter": registry.get("grain_filter"),
                "trust_state": OBSERVED_TRUST,
            },
            source.evidence(pointer),
        )
        _legacy_binding(
            collector,
            node_id,
            raw.get("column"),
            table,
            source,
            f"{pointer}/column",
        )
        for alias_index, alias in enumerate(raw.get("synonyms") or []):
            _surface_term(
                collector,
                node_id,
                alias,
                source,
                f"{pointer}/synonyms/{alias_index}",
                approved=False,
            )


def _extract_metric_files(
    collector: _Collector,
    root: Path,
    revision: str | None,
) -> None:
    directory = root / _METRIC_DIRECTORY
    if not directory.is_dir():
        collector.issue(
            "SOURCE_MISSING",
            f"legacy metric directory does not exist: {_METRIC_DIRECTORY.as_posix()}",
            severity="warning",
            source_path=_METRIC_DIRECTORY.as_posix(),
        )
        return
    paths = sorted(directory.glob("*.json"))
    if not paths:
        collector.issue(
            "SOURCE_EMPTY",
            "legacy metric directory contains no JSON registry files",
            severity="warning",
            source_path=_METRIC_DIRECTORY.as_posix(),
        )
        return
    for path in paths:
        relative = path.relative_to(root)
        loaded = collector.json_source(
            root,
            relative,
            revision=revision,
            trust_state=OBSERVED_TRUST,
            required=False,
        )
        if loaded is None:
            continue
        source, raw = loaded
        metric_id = str(raw.get("metric_id") or path.stem)
        node_id = f"legacy:metric:{_token(metric_id)}"
        collector.node(
            node_id,
            "Metric",
            {
                "legacy_id": metric_id,
                "name": raw.get("name"),
                "semantic_type": raw.get("semantic_type"),
                "data_type": raw.get("data_type"),
                "null_semantics": _plain(raw.get("null_semantics") or {}),
                "time_semantics": _plain(raw.get("time_semantics") or {}),
                "targeting": _plain(raw.get("targeting") or {}),
                "trust_state": OBSERVED_TRUST,
            },
            source.evidence("/"),
        )
        source_spec = raw.get("source")
        if isinstance(source_spec, Mapping):
            _legacy_binding(
                collector,
                node_id,
                source_spec.get("column"),
                str(source_spec.get("table") or ""),
                source,
                "/source/column",
            )
        aliases = raw.get("aliases")
        if isinstance(aliases, Mapping):
            for alias_kind in ("nouns", "actions"):
                for index, alias in enumerate(aliases.get(alias_kind) or []):
                    _surface_term(
                        collector,
                        node_id,
                        alias,
                        source,
                        f"/aliases/{alias_kind}/{index}",
                        approved=False,
                    )
        operators = raw.get("operators")
        if isinstance(operators, Mapping):
            for index, operator in enumerate(operators.get("allowed") or []):
                _operator(
                    collector,
                    node_id,
                    operator,
                    source,
                    f"/operators/allowed/{index}",
                    approved=False,
                )


def _load_inventory_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"_capability_coverage_inventory_{_stable_hash(str(path.resolve()))}",
        path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create import specification for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _extract_coverage_inventory(
    collector: _Collector,
    root: Path,
    revision: str | None,
    coverage_scan: Callable[[], Mapping[str, Any]] | None,
) -> Mapping[str, Any]:
    source = collector.source(
        root,
        _COVERAGE_INVENTORY,
        revision=revision,
        trust_state=OBSERVED_TRUST,
        source_type="code",
        required=False,
    )
    if source is None:
        return {}
    try:
        if coverage_scan is None:
            module = _load_inventory_module(source.path)
            scan = getattr(module, "scan", None)
            if not callable(scan):
                raise AttributeError("coverage inventory has no callable scan()")
            result = scan()
        else:
            result = coverage_scan()
    # The injected/local scanner is a plugin boundary; arbitrary scanner
    # failures become provenance-visible issues instead of aborting projection.
    except Exception as exc:  # noqa: BLE001
        collector.issue(
            "COVERAGE_INVENTORY_FAILED",
            f"canonical coverage inventory scan() failed: {exc}",
            severity="warning",
            source_path=source.relative_path,
            source_pointer="scan()",
        )
        return {}
    if not isinstance(result, Mapping):
        collector.issue(
            "COVERAGE_INVENTORY_INVALID",
            "canonical coverage inventory scan() must return an object",
            severity="warning",
            source_path=source.relative_path,
            source_pointer="scan()",
        )
        return {}
    inventory = _plain(result)
    by_axis = inventory.get("by_axis")
    columns = inventory.get("columns")
    if not isinstance(by_axis, Mapping) or not isinstance(columns, Mapping):
        collector.issue(
            "COVERAGE_INVENTORY_INVALID",
            "canonical coverage inventory result requires by_axis and columns objects",
            severity="warning",
            source_path=source.relative_path,
            source_pointer="scan()",
        )
        return inventory

    for axis, raw_columns in sorted(by_axis.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_columns, Sequence) or isinstance(
            raw_columns, (str, bytes)
        ):
            collector.issue(
                "COVERAGE_INVENTORY_INVALID",
                f"coverage axis {axis!r} does not contain a column array",
                severity="warning",
                source_path=source.relative_path,
                source_pointer=f"scan()/by_axis/{axis}",
            )
            continue
        pointer = f"scan()/by_axis/{axis}"
        axis_id = f"legacy:coverage-axis:{_token(axis)}"
        collector.node(
            axis_id,
            "LegacySymbol",
            {
                "legacy_id": str(axis),
                "asset_kind": "coverage_axis",
                "missing_canonical_columns": sorted(str(item) for item in raw_columns),
                "trust_state": OBSERVED_TRUST,
            },
            source.evidence(pointer),
        )
        for index, column in enumerate(raw_columns):
            column_id = _column_name_node(
                collector,
                str(column),
                source,
                f"{pointer}/{index}",
            )
            collector.edge(
                axis_id,
                column_id,
                "USES_COLUMN",
                source.evidence(f"{pointer}/{index}"),
                {"coverage_state": "legacy_only"},
            )
    return inventory


def _python_source(
    collector: _Collector,
    root: Path,
    relative: Path,
    revision: str | None,
    *,
    required: bool = True,
) -> tuple[_Source, ast.Module] | None:
    source = collector.source(
        root,
        relative,
        revision=revision,
        trust_state=APPROVED_TRUST,
        source_type="code",
        required=required,
    )
    if source is None:
        return None
    try:
        tree = ast.parse(
            source.path.read_text(encoding="utf-8"), filename=source.relative_path
        )
    except (OSError, UnicodeError, SyntaxError) as exc:
        collector.issue(
            "SOURCE_INVALID",
            f"cannot parse Python capability source: {exc}",
            source_path=source.relative_path,
        )
        return None
    return source, tree


def _assignment(tree: ast.Module, name: str) -> ast.AST | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return node.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return node.value
    return None


def _literal_assignment(tree: ast.Module, name: str) -> Any:
    value = _assignment(tree, name)
    if value is None:
        raise KeyError(name)
    return ast.literal_eval(value)


def _semantic_plan_types(
    collector: _Collector,
    root: Path,
    revision: str | None,
) -> dict[str, str]:
    parsed = _python_source(collector, root, _SEMANTIC_PLAN, revision)
    if parsed is None:
        return {}
    source, tree = parsed
    class_to_type: dict[str, str] = {}
    for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        node_type: str | None = None
        for child in class_node.body:
            value: ast.AST | None = None
            if (
                isinstance(child, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "TYPE"
                    for target in child.targets
                )
                or (
                    isinstance(child, ast.AnnAssign)
                    and isinstance(child.target, ast.Name)
                    and child.target.id == "TYPE"
                )
            ):
                value = child.value
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value
            ):
                node_type = value.value
                break
        if node_type is None:
            continue
        class_to_type[class_node.name] = node_type
        pointer = f"symbol:{class_node.name}.TYPE"
        evidence = source.evidence(
            pointer,
            line_start=class_node.lineno,
            line_end=getattr(class_node, "end_lineno", class_node.lineno),
        )
        implementation_id = f"code:semantic-plan-class:{_token(class_node.name)}"
        canonical_id = f"canonical:semantic-plan-node-type:{_token(node_type)}"
        collector.node(
            implementation_id,
            "SemanticPlanNodeTypeImplementation",
            {
                "symbol": class_node.name,
                "node_type": node_type,
                "trust_state": APPROVED_TRUST,
            },
            evidence,
        )
        if canonical_id not in collector.nodes:
            collector.issue(
                "CODE_CATALOG_DRIFT",
                f"semantic plan class {class_node.name} declares undeclared node type {node_type!r}",
                source_path=source.relative_path,
                source_pointer=pointer,
            )
            collector.node(
                canonical_id,
                "SemanticPlanNodeType",
                {
                    "canonical_id": node_type,
                    "declared_in_catalog": False,
                    "trust_state": APPROVED_TRUST,
                },
                evidence,
            )
        collector.edge(canonical_id, implementation_id, "IMPLEMENTED_BY", evidence)
    return class_to_type


def _handler_name(if_node: ast.If) -> str | None:
    for child in if_node.body:
        for node in ast.walk(child):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
                return node.func.attr
    return None


def _isinstance_class(if_node: ast.If) -> str | None:
    test = if_node.test
    if not isinstance(test, ast.Call):
        return None
    if not isinstance(test.func, ast.Name) or test.func.id != "isinstance":
        return None
    if len(test.args) != 2 or not isinstance(test.args[1], ast.Attribute):
        return None
    attribute = test.args[1]
    if (
        not isinstance(attribute.value, ast.Name)
        or attribute.value.id != "semantic_plan"
    ):
        return None
    return attribute.attr


def _extract_lowering_contract(
    collector: _Collector,
    root: Path,
    revision: str | None,
    class_to_type: Mapping[str, str],
) -> None:
    parsed = _python_source(collector, root, _EVENT_LOWERING, revision)
    if parsed is None:
        return
    source, tree = parsed
    discovered = 0
    for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for function in (
            node
            for node in class_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            for if_node in (
                node for node in ast.walk(function) if isinstance(node, ast.If)
            ):
                class_name = _isinstance_class(if_node)
                if not class_name:
                    continue
                handler = _handler_name(if_node)
                if not handler:
                    continue
                discovered += 1
                node_type = class_to_type.get(class_name) or _snake_case(class_name)
                pointer = f"symbol:{class_node.name}.{function.name}/{class_name}"
                evidence = source.evidence(
                    pointer,
                    line_start=if_node.lineno,
                    line_end=getattr(if_node, "end_lineno", if_node.lineno),
                )
                lowerer_id = f"code:lowering-function:{_token(class_node.name)}.{_token(handler)}"
                collector.node(
                    lowerer_id,
                    "LoweringFunction",
                    {
                        "symbol": f"{class_node.name}.{handler}",
                        "dispatch_symbol": f"semantic_plan.{class_name}",
                        "evidence_scope": "explicit_isinstance_dispatch",
                        "trust_state": APPROVED_TRUST,
                    },
                    evidence,
                )
                semantic_id = f"canonical:semantic-plan-node-type:{_token(node_type)}"
                if semantic_id not in collector.nodes:
                    collector.node(
                        semantic_id,
                        "SemanticPlanNodeType",
                        {
                            "canonical_id": node_type,
                            "declared_in_catalog": False,
                            "trust_state": APPROVED_TRUST,
                        },
                        evidence,
                    )
                    collector.issue(
                        "LOWERING_CATALOG_DRIFT",
                        f"lowering dispatch refers to undeclared node type {node_type!r}",
                        source_path=source.relative_path,
                        source_pointer=pointer,
                    )
                collector.edge(
                    semantic_id,
                    lowerer_id,
                    "IMPLEMENTED_BY",
                    evidence,
                    {"stage": "event_ir_lowering", "proof": "dispatch"},
                )
    if discovered == 0:
        collector.issue(
            "LOWERING_DISPATCH_NOT_FOUND",
            "no explicit semantic_plan isinstance dispatch was found in event lowering",
            source_path=source.relative_path,
        )


def _compiler_node(
    collector: _Collector,
    builder: str,
    source: _Source,
    pointer: str,
    *,
    line_start: int | None = None,
    terminal: bool = False,
) -> str:
    node_id = f"code:compiler:{_token(builder)}"
    collector.node(
        node_id,
        "Compiler",
        {
            "symbol": builder,
            "terminal_fallback": terminal,
            "evidence_scope": "declared_routing_contract",
            "trust_state": APPROVED_TRUST,
        },
        source.evidence(pointer, line_start=line_start),
    )
    return node_id


def _extract_builder_contracts(
    collector: _Collector,
    root: Path,
    revision: str | None,
) -> None:
    parsed = _python_source(collector, root, _CAPABILITY_VALIDATION, revision)
    if parsed is None:
        return
    source, tree = parsed
    for constant in ("BUILDER_PRECEDENCE", "EXCLUSIVE_ROUTES"):
        try:
            value = _literal_assignment(tree, constant)
        except (KeyError, ValueError, TypeError) as exc:
            collector.issue(
                "CODE_CONTRACT_INVALID",
                f"cannot deterministically extract {constant}: {exc}",
                source_path=source.relative_path,
                source_pointer=f"symbol:{constant}",
            )
            continue
        if not isinstance(value, tuple):
            collector.issue(
                "CODE_CONTRACT_INVALID",
                f"{constant} must be a literal tuple",
                source_path=source.relative_path,
                source_pointer=f"symbol:{constant}",
            )
            continue
        if constant == "BUILDER_PRECEDENCE":
            for index, item in enumerate(value):
                if not isinstance(item, tuple) or len(item) != 3:
                    continue
                earlier, later, reason = (str(part) for part in item)
                pointer = f"symbol:{constant}/{index}"
                earlier_id = _compiler_node(collector, earlier, source, pointer)
                later_id = _compiler_node(collector, later, source, pointer)
                collector.edge(
                    earlier_id,
                    later_id,
                    "PRECEDES",
                    source.evidence(pointer),
                    {"reason": reason},
                )
        else:
            for index, item in enumerate(value):
                if not isinstance(item, tuple) or len(item) != 3:
                    continue
                slot, builder, reason = (str(part) for part in item)
                pointer = f"symbol:{constant}/{index}"
                slot_id = f"canonical:query-plan-slot:{_token(slot)}"
                collector.node(
                    slot_id,
                    "QueryPlanSlot",
                    {
                        "canonical_id": slot,
                        "trust_state": APPROVED_TRUST,
                    },
                    source.evidence(pointer),
                )
                builder_id = _compiler_node(collector, builder, source, pointer)
                collector.edge(
                    slot_id,
                    builder_id,
                    "EXCLUSIVE_ROUTE",
                    source.evidence(pointer),
                    {"reason": reason},
                )
    try:
        terminal = str(_literal_assignment(tree, "TERMINAL_BUILDER_NAME"))
    except (KeyError, ValueError, TypeError):
        collector.issue(
            "CODE_CONTRACT_INVALID",
            "cannot deterministically extract TERMINAL_BUILDER_NAME",
            source_path=source.relative_path,
            source_pointer="symbol:TERMINAL_BUILDER_NAME",
        )
    else:
        _compiler_node(
            collector,
            terminal,
            source,
            "symbol:TERMINAL_BUILDER_NAME",
            terminal=True,
        )


def _string_dict_keys(value: ast.AST | None) -> list[tuple[str, int]]:
    if not isinstance(value, ast.Dict):
        return []
    found: list[tuple[str, int]] = []
    for key in value.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            found.append((key.value, key.lineno))
    return found


def _extract_targeting_contracts(
    collector: _Collector,
    root: Path,
    revision: str | None,
) -> None:
    parsed = _python_source(collector, root, _TARGETING_IR, revision)
    if parsed is None:
        return
    source, tree = parsed
    slots = _string_dict_keys(_assignment(tree, "SLOT_SHAPES"))
    if not slots:
        collector.issue(
            "CODE_CONTRACT_INVALID",
            "cannot deterministically extract SLOT_SHAPES keys",
            source_path=source.relative_path,
            source_pointer="symbol:SLOT_SHAPES",
        )
    for slot, line in slots:
        collector.node(
            f"canonical:query-plan-slot:{_token(slot)}",
            "QueryPlanSlot",
            {
                "canonical_id": slot,
                "trust_state": APPROVED_TRUST,
            },
            source.evidence("symbol:SLOT_SHAPES", line_start=line),
        )

    condition_assignment = _assignment(tree, "CONDITION_SPECS")
    condition_kinds: list[tuple[str, int]] = []
    if isinstance(condition_assignment, (ast.Tuple, ast.List)):
        for item in condition_assignment.elts:
            if not isinstance(item, ast.Call):
                continue
            is_condition_spec = (
                isinstance(item.func, ast.Name) and item.func.id == "ConditionSpec"
            )
            if not is_condition_spec:
                continue
            for keyword in item.keywords:
                if (
                    keyword.arg == "kind"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    condition_kinds.append((keyword.value.value, item.lineno))
                    break
    if not condition_kinds:
        collector.issue(
            "CODE_CONTRACT_INVALID",
            "cannot deterministically extract CONDITION_SPECS kinds",
            source_path=source.relative_path,
            source_pointer="symbol:CONDITION_SPECS",
        )
    slot_names = {slot for slot, _line in slots}
    for kind, line in condition_kinds:
        condition_id = f"canonical:condition-kind:{_token(kind)}"
        evidence = source.evidence("symbol:CONDITION_SPECS", line_start=line)
        collector.node(
            condition_id,
            "ConditionKind",
            {
                "canonical_id": kind,
                "trust_state": APPROVED_TRUST,
            },
            evidence,
        )
        if kind in slot_names:
            collector.edge(
                condition_id,
                f"canonical:query-plan-slot:{_token(kind)}",
                "PROJECTS_TO",
                evidence,
            )


def build_repository_projection(
    repository_root: str | Path,
    *,
    coverage_scan: Callable[[], Mapping[str, Any]] | None = None,
) -> ProjectionResult:
    """Build the offline approved/discovery projection for ``repository_root``.

    ``coverage_scan`` is an injection seam for tests.  Production callers omit
    it and the existing P4 ``tools/canonical_coverage_inventory.py::scan`` is
    loaded from the requested repository.  The returned object is data only;
    this function performs no repository or runtime writes.
    """

    root = Path(repository_root).resolve()
    collector = _Collector()
    if not root.is_dir():
        collector.issue(
            "REPOSITORY_ROOT_MISSING",
            f"repository root does not exist or is not a directory: {root}",
        )
    git_revision, dirty = _git_state(root) if root.is_dir() else (None, False)
    implementation_sources = _record_discovery_implementation(
        collector, root, git_revision
    )

    _extract_audience_catalog(collector, root, git_revision)
    _extract_semantic_capabilities(collector, root, git_revision)
    _extract_member_target_filters(collector, root, git_revision)
    _extract_member_metrics(collector, root, git_revision)
    _extract_metric_files(collector, root, git_revision)
    coverage_inventory = _extract_coverage_inventory(
        collector,
        root,
        git_revision,
        coverage_scan,
    )
    class_to_type = _semantic_plan_types(collector, root, git_revision)
    _extract_lowering_contract(collector, root, git_revision, class_to_type)
    _extract_builder_contracts(collector, root, git_revision)
    _extract_targeting_contracts(collector, root, git_revision)

    source_hashes = dict(sorted(collector.source_hashes.items()))
    content_sha256 = hashlib.sha256(
        json.dumps(source_hashes, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    repository_revision = RepositoryRevision(
        git_revision=git_revision,
        dirty=dirty,
        content_sha256=content_sha256,
        source_hashes=source_hashes,
    )
    nodes = tuple(sorted(collector.nodes.values(), key=lambda node: node.id))
    edges = tuple(sorted(collector.edges.values(), key=lambda edge: edge.id))
    issues = tuple(
        sorted(
            collector.issues,
            key=lambda issue: (
                issue.severity,
                issue.code,
                issue.source_path or "",
                issue.source_pointer,
                issue.message,
            ),
        )
    )
    approved_count = sum(
        1 for node in nodes if node.attributes.get("trust_state") == APPROVED_TRUST
    )
    observed_count = sum(
        1 for node in nodes if node.attributes.get("trust_state") == OBSERVED_TRUST
    )
    result = ProjectionResult(
        nodes=nodes,
        edges=edges,
        issues=issues,
        revision=repository_revision,
        metadata={
            "projection_kind": "offline_capability_discovery_g0",
            "projection_implementation_sources": list(implementation_sources),
            "runtime_connected": False,
            "approved_registry_mutation_enabled": False,
            "approved_authorities": [
                _AUDIENCE_CATALOG.as_posix(),
                _SEMANTIC_CAPABILITIES.as_posix(),
                _SEMANTIC_PLAN.as_posix(),
                _EVENT_LOWERING.as_posix(),
                _CAPABILITY_VALIDATION.as_posix(),
                _TARGETING_IR.as_posix(),
            ],
            "observed_discovery_sources": [
                _MEMBER_TARGET_FILTERS.as_posix(),
                _MEMBER_METRICS.as_posix(),
                _METRIC_DIRECTORY.as_posix(),
                _COVERAGE_INVENTORY.as_posix(),
            ],
            "coverage_inventory": coverage_inventory,
            "node_counts": {
                "approved_projection": approved_count,
                "observed": observed_count,
                "total": len(nodes),
            },
            "edge_count": len(edges),
            "proof_limits": {
                "lowering": (
                    "explicit dispatch proves a routing hook only; it does not by itself "
                    "prove end-to-end binding, compilation, or SQL execution"
                ),
                "compiler": (
                    "capability_validation declarations prove precedence and exclusive "
                    "routing contracts without importing graph_rag"
                ),
            },
        },
    )
    if not result.cannot_execute_sql or not result.cannot_mutate_approved_registry:
        raise RuntimeError("offline projection safety invariants were not preserved")
    return result


def build_approved_projection(
    repository_root: str | Path,
    *,
    coverage_scan: Callable[[], Mapping[str, Any]] | None = None,
) -> ProjectionResult:
    """Compatibility name for the read-only approved-plus-discovery projection.

    The approved entities remain marked ``approved_projection`` while legacy
    and P4 evidence remains ``observed``.  This alias does not filter or promote
    facts and has exactly the same non-executable contract as
    :func:`build_repository_projection`.
    """

    return build_repository_projection(repository_root, coverage_scan=coverage_scan)


__all__ = [
    "APPROVED_TRUST",
    "OBSERVED_TRUST",
    "ProjectionIssue",
    "ProjectionResult",
    "build_approved_projection",
    "build_repository_projection",
]
