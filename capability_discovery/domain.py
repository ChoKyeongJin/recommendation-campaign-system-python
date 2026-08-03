"""Immutable domain records for offline capability discovery.

The runtime campaign planner must not depend on this package.  These records
are deliberately small, JSON-safe values that can be produced from repository
assets, reviewed, and snapshotted without turning discovered facts into an
execution authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias
from urllib.parse import quote

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | Mapping[str, "JsonValue"] | tuple["JsonValue", ...]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NO_WHITESPACE_RE = re.compile(r"^\S+$", re.UNICODE)


class CapabilityDiscoveryError(ValueError):
    """Base class for deterministic discovery contract violations."""


class DomainValidationError(CapabilityDiscoveryError):
    """Raised when a domain record is malformed or not JSON-safe."""


class SnapshotValidationError(CapabilityDiscoveryError):
    """Raised when a graph snapshot is internally inconsistent."""


class _FrozenJsonMapping(Mapping[str, JsonValue]):
    """A tiny hashable mapping used to make frozen records deeply immutable."""

    __slots__ = ("_hash", "_items", "_mapping")

    def __init__(self, items: Mapping[str, JsonValue] | None = None) -> None:
        ordered = tuple(sorted((items or {}).items(), key=lambda item: item[0]))
        self._items = ordered
        self._mapping = dict(ordered)
        self._hash = hash(ordered)

    def __getitem__(self, key: str) -> JsonValue:
        return self._mapping[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)

    def __hash__(self) -> int:
        return self._hash

    def __repr__(self) -> str:
        return repr(self._mapping)


def _freeze_json(value: Any, *, path: str = "$") -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DomainValidationError(f"{path} must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise DomainValidationError(
                    f"{path} contains a non-string mapping key: {raw_key!r}"
                )
            if not raw_key:
                raise DomainValidationError(f"{path} contains an empty mapping key")
            frozen[raw_key] = _freeze_json(raw_value, path=f"{path}.{raw_key}")
        return _FrozenJsonMapping(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise DomainValidationError(
        f"{path} contains a non-JSON value of type {type(value).__name__}"
    )


def thaw_json(value: JsonValue) -> Any:
    """Return a detached, ordinary dict/list representation of a JSON value."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value with a stable byte representation."""

    frozen = _freeze_json(value)
    return json.dumps(
        thaw_json(frozen),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_digest(value: Any) -> str:
    """Return the SHA-256 digest for a canonical JSON value."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(namespace: str, *parts: Any) -> str:
    """Build a readable, percent-encoded identifier from normalized parts.

    The helper preserves semantic text instead of relying on Python's process-
    randomized ``hash``.  Very long identifiers receive a deterministic hash
    suffix while retaining a useful prefix for review output.
    """

    _validate_token(namespace, "namespace")
    if not parts:
        raise DomainValidationError("stable_id requires at least one component")
    encoded: list[str] = []
    for index, part in enumerate(parts):
        if isinstance(part, (Mapping, list, tuple)):
            text = canonical_json(part)
        elif part is None:
            text = "null"
        else:
            text = str(part)
        text = unicodedata.normalize("NFKC", text).strip()
        if not text:
            raise DomainValidationError(
                f"stable_id component {index} must not be empty"
            )
        encoded.append(quote(text, safe="-._~/"))
    identifier = f"{namespace}:{':'.join(encoded)}"
    if len(identifier) <= 240:
        return identifier
    digest = stable_digest([namespace, *parts])[:20]
    return f"{identifier[:219]}~{digest}"


def _validate_text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DomainValidationError(f"{name} must be a string")
    if not allow_empty and not value.strip():
        raise DomainValidationError(f"{name} must not be empty")
    if "\x00" in value:
        raise DomainValidationError(f"{name} must not contain NUL")
    return value


def _validate_token(value: Any, name: str) -> str:
    text = _validate_text(value, name)
    if not _NO_WHITESPACE_RE.fullmatch(text):
        raise DomainValidationError(f"{name} must not contain whitespace: {text!r}")
    return text


def _validate_sha256(value: Any, name: str) -> str:
    text = _validate_text(value, name)
    if not _SHA256_RE.fullmatch(text):
        raise DomainValidationError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _coerce_evidence(values: Any, name: str = "evidence") -> tuple[Evidence, ...]:
    try:
        result = tuple(values)
    except TypeError as exc:
        raise DomainValidationError(f"{name} must be an iterable of Evidence") from exc
    if any(not isinstance(item, Evidence) for item in result):
        raise DomainValidationError(f"{name} must contain only Evidence records")
    unique = {canonical_json(item.to_dict()): item for item in result}
    return tuple(unique[key] for key in sorted(unique))


def _coerce_string_tuple(values: Any, name: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise DomainValidationError(f"{name} must be an iterable, not a string")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise DomainValidationError(f"{name} must be an iterable of strings") from exc
    for item in result:
        _validate_text(item, f"{name} item")
    return tuple(sorted(set(result)))


@dataclass(frozen=True)
class Evidence:
    """Provenance for one observation in a repository source."""

    source_type: str
    source_path: str
    source_pointer: str = ""
    extraction_method: str = "declarative"
    confidence: float = 1.0
    content_hash: str | None = None
    revision: str | None = None
    trust_state: str = "observed"

    def __post_init__(self) -> None:
        _validate_token(self.source_type, "source_type")
        _validate_text(self.source_path, "source_path")
        _validate_text(self.source_pointer, "source_pointer", allow_empty=True)
        _validate_token(self.extraction_method, "extraction_method")
        _validate_token(self.trust_state, "trust_state")
        if isinstance(self.confidence, bool) or not isinstance(
            self.confidence, (int, float)
        ):
            raise DomainValidationError("confidence must be a number between 0 and 1")
        if (
            not math.isfinite(float(self.confidence))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise DomainValidationError("confidence must be between 0 and 1")
        object.__setattr__(self, "confidence", float(self.confidence))
        if self.content_hash is not None:
            _validate_sha256(self.content_hash, "content_hash")
        if self.revision is not None:
            _validate_text(self.revision, "revision")

    @property
    def source_key(self) -> str:
        """Stable ownership key used for source-level graph replacement."""

        return stable_id("source", self.source_type, self.source_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_path": self.source_path,
            "source_pointer": self.source_pointer,
            "extraction_method": self.extraction_method,
            "confidence": self.confidence,
            "content_hash": self.content_hash,
            "revision": self.revision,
            "trust_state": self.trust_state,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Evidence:
        if not isinstance(value, Mapping):
            raise DomainValidationError("evidence payload must be an object")
        return cls(
            source_type=value.get("source_type"),
            source_path=value.get("source_path"),
            source_pointer=value.get("source_pointer", ""),
            extraction_method=value.get("extraction_method", "declarative"),
            confidence=value.get("confidence", 1.0),
            content_hash=value.get("content_hash"),
            revision=value.get("revision"),
            trust_state=value.get("trust_state", "observed"),
        )


@dataclass(frozen=True)
class GraphNode:
    """A typed graph entity with one or more source observations."""

    id: str
    kind: str
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        _validate_token(self.id, "node id")
        _validate_token(self.kind, "node kind")
        frozen = _freeze_json(self.attributes, path="$.attributes")
        if not isinstance(frozen, Mapping):
            raise DomainValidationError("node attributes must be an object")
        object.__setattr__(self, "attributes", frozen)
        object.__setattr__(self, "evidence", _coerce_evidence(self.evidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "attributes": thaw_json(self.attributes),
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GraphNode:
        if not isinstance(value, Mapping):
            raise DomainValidationError("node payload must be an object")
        evidence = value.get("evidence", ())
        if isinstance(evidence, (str, bytes, Mapping)):
            raise DomainValidationError("node evidence must be an array")
        return cls(
            id=value.get("id"),
            kind=value.get("kind"),
            attributes=value.get("attributes", {}),
            evidence=tuple(Evidence.from_dict(item) for item in evidence),
        )


@dataclass(frozen=True)
class GraphEdge:
    """A directed typed relation.  ``id`` is the MultiDiGraph edge key."""

    id: str
    source: str
    target: str
    relation: str
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        _validate_token(self.id, "edge id")
        _validate_token(self.source, "edge source")
        _validate_token(self.target, "edge target")
        _validate_token(self.relation, "edge relation")
        frozen = _freeze_json(self.attributes, path="$.attributes")
        if not isinstance(frozen, Mapping):
            raise DomainValidationError("edge attributes must be an object")
        object.__setattr__(self, "attributes", frozen)
        object.__setattr__(self, "evidence", _coerce_evidence(self.evidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "relation": self.relation,
            "attributes": thaw_json(self.attributes),
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GraphEdge:
        if not isinstance(value, Mapping):
            raise DomainValidationError("edge payload must be an object")
        evidence = value.get("evidence", ())
        if isinstance(evidence, (str, bytes, Mapping)):
            raise DomainValidationError("edge evidence must be an array")
        return cls(
            id=value.get("id"),
            source=value.get("source"),
            target=value.get("target"),
            relation=value.get("relation"),
            attributes=value.get("attributes", {}),
            evidence=tuple(Evidence.from_dict(item) for item in evidence),
        )


@dataclass(frozen=True)
class GapRecord:
    """An offline discrepancy; classifications are not runtime failure codes."""

    gap_id: str
    concept_id: str
    classification: str
    summary: str
    legacy_assets: tuple[str, ...] = ()
    approved_assets: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    details: Mapping[str, JsonValue] = field(default_factory=dict)
    blocking_questions: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_token(self.gap_id, "gap_id")
        _validate_text(self.concept_id, "concept_id")
        _validate_token(self.classification, "classification")
        _validate_text(self.summary, "summary")
        object.__setattr__(
            self,
            "legacy_assets",
            _coerce_string_tuple(self.legacy_assets, "legacy_assets"),
        )
        object.__setattr__(
            self,
            "approved_assets",
            _coerce_string_tuple(self.approved_assets, "approved_assets"),
        )
        object.__setattr__(self, "evidence", _coerce_evidence(self.evidence))
        frozen = _freeze_json(self.details, path="$.details")
        if not isinstance(frozen, Mapping):
            raise DomainValidationError("gap details must be an object")
        object.__setattr__(self, "details", frozen)
        object.__setattr__(
            self,
            "blocking_questions",
            _coerce_string_tuple(self.blocking_questions, "blocking_questions"),
        )
        object.__setattr__(
            self, "conflicts", _coerce_string_tuple(self.conflicts, "conflicts")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "concept_id": self.concept_id,
            "classification": self.classification,
            "summary": self.summary,
            "legacy_assets": list(self.legacy_assets),
            "approved_assets": list(self.approved_assets),
            "evidence": [item.to_dict() for item in self.evidence],
            "details": thaw_json(self.details),
            "blocking_questions": list(self.blocking_questions),
            "conflicts": list(self.conflicts),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GapRecord:
        if not isinstance(value, Mapping):
            raise DomainValidationError("gap payload must be an object")
        raw_evidence = value.get("evidence", ())
        if isinstance(raw_evidence, (str, bytes, Mapping)):
            raise DomainValidationError("gap evidence must be an array")
        return cls(
            gap_id=value.get("gap_id"),
            concept_id=value.get("concept_id"),
            classification=value.get("classification"),
            summary=value.get("summary"),
            legacy_assets=tuple(value.get("legacy_assets", ())),
            approved_assets=tuple(value.get("approved_assets", ())),
            evidence=tuple(Evidence.from_dict(item) for item in raw_evidence),
            details=value.get("details", {}),
            blocking_questions=tuple(value.get("blocking_questions", ())),
            conflicts=tuple(value.get("conflicts", ())),
        )


@dataclass(frozen=True)
class RepositoryRevision:
    """Repository identity plus content hashes used to reproduce a projection."""

    git_revision: str | None
    dirty: bool
    content_sha256: str
    source_hashes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.git_revision is not None:
            _validate_text(self.git_revision, "git_revision")
        if not isinstance(self.dirty, bool):
            raise DomainValidationError("dirty must be a boolean")
        _validate_sha256(self.content_sha256, "content_sha256")
        if not isinstance(self.source_hashes, Mapping):
            raise DomainValidationError("source_hashes must be an object")
        normalized: dict[str, JsonValue] = {}
        for source_path, digest in self.source_hashes.items():
            _validate_text(source_path, "source hash path")
            normalized[source_path] = _validate_sha256(
                digest, f"source hash for {source_path}"
            )
        object.__setattr__(self, "source_hashes", _FrozenJsonMapping(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {
            "git_revision": self.git_revision,
            "dirty": self.dirty,
            "content_sha256": self.content_sha256,
            "source_hashes": dict(self.source_hashes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RepositoryRevision:
        if not isinstance(value, Mapping):
            raise DomainValidationError("revision payload must be an object")
        return cls(
            git_revision=value.get("git_revision"),
            dirty=value.get("dirty"),
            content_sha256=value.get("content_sha256"),
            source_hashes=value.get("source_hashes", {}),
        )


@dataclass(frozen=True)
class GraphSnapshot:
    """A deterministic, self-contained serialization of typed graph facts."""

    revision: RepositoryRevision
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    schema_version: str = "capability-discovery/v1"

    def __post_init__(self) -> None:
        if not isinstance(self.revision, RepositoryRevision):
            raise SnapshotValidationError("revision must be a RepositoryRevision")
        _validate_token(self.schema_version, "schema_version")
        try:
            nodes = tuple(self.nodes)
            edges = tuple(self.edges)
        except TypeError as exc:
            raise SnapshotValidationError("nodes and edges must be iterables") from exc
        if any(not isinstance(node, GraphNode) for node in nodes):
            raise SnapshotValidationError("nodes must contain only GraphNode records")
        if any(not isinstance(edge, GraphEdge) for edge in edges):
            raise SnapshotValidationError("edges must contain only GraphEdge records")
        node_ids = [node.id for node in nodes]
        edge_ids = [edge.id for edge in edges]
        if len(node_ids) != len(set(node_ids)):
            raise SnapshotValidationError("snapshot contains duplicate node ids")
        if len(edge_ids) != len(set(edge_ids)):
            raise SnapshotValidationError("snapshot contains duplicate edge ids")
        known_nodes = set(node_ids)
        dangling = sorted(
            edge.id
            for edge in edges
            if edge.source not in known_nodes or edge.target not in known_nodes
        )
        if dangling:
            raise SnapshotValidationError(
                f"snapshot contains edges with missing endpoints: {dangling}"
            )
        object.__setattr__(
            self, "nodes", tuple(sorted(nodes, key=lambda node: node.id))
        )
        object.__setattr__(
            self, "edges", tuple(sorted(edges, key=lambda edge: edge.id))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision.to_dict(),
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GraphSnapshot:
        if not isinstance(value, Mapping):
            raise SnapshotValidationError("snapshot payload must be an object")
        nodes = value.get("nodes", ())
        edges = value.get("edges", ())
        if isinstance(nodes, (str, bytes, Mapping)):
            raise SnapshotValidationError("snapshot nodes must be an array")
        if isinstance(edges, (str, bytes, Mapping)):
            raise SnapshotValidationError("snapshot edges must be an array")
        return cls(
            revision=RepositoryRevision.from_dict(value.get("revision")),
            nodes=tuple(GraphNode.from_dict(item) for item in nodes),
            edges=tuple(GraphEdge.from_dict(item) for item in edges),
            schema_version=value.get("schema_version", "capability-discovery/v1"),
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            indent=indent,
            sort_keys=True,
            separators=(",", ":") if indent is None else None,
        )

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> GraphSnapshot:
        try:
            decoded = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SnapshotValidationError("snapshot is not valid JSON") from exc
        return cls.from_dict(decoded)


__all__ = [
    "CapabilityDiscoveryError",
    "DomainValidationError",
    "Evidence",
    "GapRecord",
    "GraphEdge",
    "GraphNode",
    "GraphSnapshot",
    "JsonValue",
    "RepositoryRevision",
    "SnapshotValidationError",
    "canonical_json",
    "stable_digest",
    "stable_id",
    "thaw_json",
]
