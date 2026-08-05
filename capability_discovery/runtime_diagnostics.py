"""Runtime-safe, non-executable diagnostics over a capability snapshot.

This module is an intentionally narrow bridge from the offline discovery
projection to a runtime response *annotation*.  It does not import the graph
store, NetworkX, the planner, or any SQL compiler.  A caller injects either an
immutable :class:`~capability_discovery.domain.GraphSnapshot` or a service that
can return one.  The snapshot is reduced once to a small immutable alias index.

The adapter has no operation that can rewrite a runtime outcome.  An original
failure code, status, and SQL may be supplied as an immutable context value.
Diagnostics may echo the allowlisted failure code as evidence, but never
return a replacement status or SQL value.
"""

from __future__ import annotations

import math
import threading
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from .domain import GraphEdge, GraphNode, GraphSnapshot

_APPROVED_TRUST = "approved_projection"
_APPROVED_ALIAS_RELATION = "ALIAS_OF"
_DISCOVERY_ALIAS_RELATION = "CANDIDATE_ALIAS_OF"
_ALIAS_RELATIONS = frozenset(
    {_APPROVED_ALIAS_RELATION, _DISCOVERY_ALIAS_RELATION}
)

FAILURE_ANALYSIS_ALLOWLIST = frozenset(
    {
        "catalog_symbol_unresolved",
        "canonical_operator_unresolved",
        "physical_binding_missing",
        "lowering_not_implemented",
        "compiler_not_implemented",
        "semantic_ir_unsupported",
        "semantic_verification_failed",
        "sql_guard_failed",
    }
)
_ALIAS_LOOKUP_FAILURE_CODE = "catalog_symbol_unresolved"


class SnapshotService(Protocol):
    """Structural interface accepted from an already-created discovery service."""

    def snapshot(self) -> GraphSnapshot: ...


class FailureLogProvider(Protocol):
    """Read-only seam for rows already selected from campaign failure logs.

    A database client or ORM is intentionally not part of this interface.  The
    application owns querying ``campaign_query_failure_logs`` and can inject a
    provider with the resulting mappings.
    """

    def load_failure_rows(self) -> Iterable[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class OriginalRuntimeOutcome:
    """Read-only context proving diagnostics cannot rewrite runtime fields.

    The adapter accepts this value only as context.  Runtime diagnostics can
    echo the same allowlisted failure code, but contain no status or SQL field.
    """

    failure_code: str | None = None
    status: str | int | None = None
    sql: str | None = None

    def __post_init__(self) -> None:
        if self.failure_code is not None and not isinstance(self.failure_code, str):
            raise TypeError("failure_code must be a string or None")
        if self.status is not None and (
            isinstance(self.status, bool) or not isinstance(self.status, (str, int))
        ):
            raise TypeError("status must be a string, integer, or None")
        if self.sql is not None and not isinstance(self.sql, str):
            raise TypeError("sql must be a string or None")


@dataclass(frozen=True)
class AliasDiagnostic:
    """One alias-to-concept observation that can never authorize execution."""

    candidate_id: str
    alias: str
    concept_id: str
    canonical_id: str
    concept_kind: str
    relation: str
    trust_state: str
    evidence: tuple[AliasEvidence, ...] = ()
    received_symbol: str | None = None
    diagnostic_only: bool = field(default=True, init=False)
    executable: bool = field(default=False, init=False)

    @property
    def match_type(self) -> str:
        return (
            "approved_alias_candidate"
            if self.trust_state == _APPROVED_TRUST
            else "discovery_alias_candidate"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "alias": self.alias,
            "concept_id": self.concept_id,
            (
                "canonical_id"
                if self.trust_state == _APPROVED_TRUST
                else "canonical_id_candidate"
            ): self.canonical_id,
            "concept_kind": self.concept_kind,
            "relation": self.relation,
            "trust_state": self.trust_state,
            "match_type": self.match_type,
            "score": 1.0,
            "received_symbol": self.received_symbol,
            "evidence": [item.to_dict() for item in self.evidence],
            "diagnostic_only": True,
            "executable": False,
        }


@dataclass(frozen=True)
class AliasEvidence:
    """Public-safe provenance that omits repository paths and SQL content."""

    source_type: str
    trust_state: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_type": self.source_type,
            "trust_state": self.trust_state,
        }


@dataclass(frozen=True)
class FailureLogRecord:
    """Sanitised failure evidence; SQL and other raw payload data are discarded."""

    failure_code: str
    subject: str
    user_impact_weight: float = 1.0
    evidence_completeness: float = 1.0
    legacy_asset_availability: float = 0.5
    latest_at: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> FailureLogRecord | None:
        if not isinstance(row, Mapping):
            return None
        query_plan = row.get("query_plan")
        query_plan = query_plan if isinstance(query_plan, Mapping) else {}
        context = row.get("context_metadata")
        response = dict(context) if isinstance(context, Mapping) else {}
        for key in ("failure_code", "failure_reason", "error_code"):
            if key in row and key not in response:
                response[key] = row.get(key)
        failure_code = _extract_allowlisted_failure_code(response, query_plan)
        decisions = query_plan.get("decisions")
        if _is_sequence(decisions):
            for decision in decisions:
                if not isinstance(decision, Mapping):
                    continue
                decision_code = _first_allowlisted_code(
                    decision.get("failure_code"),
                    decision.get("reason"),
                    decision.get("code"),
                )
                if decision_code is not None:
                    # Offline log aggregation may retain a narrower structured
                    # decision even when the final API category itself is not
                    # allowlisted.  Runtime response annotation keeps the
                    # stricter explicit-outcome boundary below.
                    failure_code = decision_code
                    break
        if failure_code not in FAILURE_ANALYSIS_ALLOWLIST:
            return None
        details = row.get("details")
        details = details if isinstance(details, Mapping) else {}
        subject = _first_text(
            row,
            details,
            keys=(
                "received_symbol",
                "unresolved_symbol",
                "symbol",
                "capability",
                "canonical_id",
                "operation",
            ),
        )
        if not subject:
            response["failure_code"] = failure_code
            signal = extract_failure_signal(query_plan, response)
            subject = signal.received_symbol if signal is not None else ""
        if not subject:
            subject = "(unspecified)"
        default_completeness = 1.0 if subject != "(unspecified)" else 0.5
        return cls(
            failure_code=failure_code,
            subject=subject,
            user_impact_weight=_factor(
                row.get("user_impact_weight"), default=1.0, bounded=False
            ),
            evidence_completeness=_factor(
                row.get("evidence_completeness"),
                default=default_completeness,
                bounded=True,
            ),
            legacy_asset_availability=_availability_factor(row),
            latest_at=_first_text(
                row,
                details,
                keys=("created_at", "occurred_at", "timestamp", "updated_at"),
            )
            or None,
        )


@dataclass(frozen=True)
class RepeatedFailureDiagnostic:
    """Deterministic review priority for two or more equivalent failures."""

    failure_code: str
    subject: str
    failure_frequency: int
    user_impact_weight: float
    evidence_completeness: float
    legacy_asset_availability: float
    priority_score: float
    latest_at: str | None = None
    diagnostic_only: bool = field(default=True, init=False)
    executable: bool = field(default=False, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_code": self.failure_code,
            "subject": self.subject,
            "failure_frequency": self.failure_frequency,
            "user_impact_weight": self.user_impact_weight,
            "evidence_completeness": self.evidence_completeness,
            "legacy_asset_availability": self.legacy_asset_availability,
            "priority_score": self.priority_score,
            "latest_at": self.latest_at,
            "diagnostic_only": True,
            "review_only": True,
            "executable": False,
        }


@dataclass(frozen=True)
class FailureLogSummary:
    """JSON-ready, allowlisted aggregation of failure-log row payloads."""

    available: bool
    total_rows: int = 0
    accepted_rows: int = 0
    ignored_rows: int = 0
    failure_groups: tuple[RepeatedFailureDiagnostic, ...] = ()
    repeated_failures: tuple[RepeatedFailureDiagnostic, ...] = ()
    truncated: bool = False
    reason: str | None = None
    diagnostic_only: bool = field(default=True, init=False)
    executable: bool = field(default=False, init=False)

    @classmethod
    def unavailable(cls, reason: str) -> FailureLogSummary:
        return cls(available=False, reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "allowlisted_failure_codes": sorted(FAILURE_ANALYSIS_ALLOWLIST),
            "total_rows": self.total_rows,
            "accepted_rows": self.accepted_rows,
            "ignored_rows": self.ignored_rows,
            "failure_groups": [
                failure.to_dict() for failure in self.failure_groups
            ],
            "repeated_failures": [
                failure.to_dict() for failure in self.repeated_failures
            ],
            "truncated": self.truncated,
            "reason": self.reason,
            "diagnostic_only": True,
            "review_only": True,
            "executable": False,
        }


class FailureLogIngestor:
    """Pure row-payload ingestor with no database or repository dependency."""

    def ingest(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        limit: int = 1000,
    ) -> FailureLogSummary:
        if isinstance(rows, (str, bytes, Mapping)):
            raise TypeError("failure rows must be an iterable of mappings")
        _validate_limit(limit)
        total = 0
        truncated = False
        accepted: list[FailureLogRecord] = []
        for row in rows:
            if total >= limit:
                truncated = True
                break
            total += 1
            record = FailureLogRecord.from_row(row)
            if record is not None:
                accepted.append(record)

        grouped: dict[tuple[str, str], list[FailureLogRecord]] = {}
        for record in accepted:
            key = (record.failure_code, _normalize(record.subject))
            grouped.setdefault(key, []).append(record)

        groups: list[RepeatedFailureDiagnostic] = []
        for (failure_code, _), records in grouped.items():
            frequency = len(records)
            impact = _average(item.user_impact_weight for item in records)
            completeness = _average(
                item.evidence_completeness for item in records
            )
            legacy = _average(
                item.legacy_asset_availability for item in records
            )
            groups.append(
                RepeatedFailureDiagnostic(
                    failure_code=failure_code,
                    subject=records[0].subject,
                    failure_frequency=frequency,
                    user_impact_weight=impact,
                    evidence_completeness=completeness,
                    legacy_asset_availability=legacy,
                    priority_score=round(
                        frequency * impact * completeness * legacy, 6
                    ),
                    latest_at=max(
                        (
                            item.latest_at
                            for item in records
                            if item.latest_at is not None
                        ),
                        default=None,
                    ),
                )
            )
        groups.sort(
            key=lambda item: (
                -item.priority_score,
                item.failure_code,
                _normalize(item.subject),
            )
        )
        return FailureLogSummary(
            available=True,
            total_rows=total,
            accepted_rows=len(accepted),
            ignored_rows=total - len(accepted),
            failure_groups=tuple(groups),
            repeated_failures=tuple(
                item for item in groups if item.failure_frequency >= 2
            ),
            truncated=truncated,
        )


def summarize_failures(
    rows: Iterable[Mapping[str, Any]], *, limit: int = 1000
) -> FailureLogSummary:
    """Aggregate pre-fetched rows; this function never opens a DB connection."""

    return FailureLogIngestor().ingest(rows, limit=limit)


@dataclass(frozen=True)
class RuntimeAliasIndex:
    """Immutable, NetworkX-free index derived from one content snapshot."""

    content_sha256: str
    revision: str | None = None
    approved_aliases: tuple[AliasDiagnostic, ...] = ()
    discovery_aliases: tuple[AliasDiagnostic, ...] = ()

    @classmethod
    def from_snapshot(cls, snapshot: GraphSnapshot) -> RuntimeAliasIndex:
        if not isinstance(snapshot, GraphSnapshot):
            raise TypeError("snapshot must be a GraphSnapshot")

        nodes = {node.id: node for node in snapshot.nodes}
        approved: list[AliasDiagnostic] = []
        discovery: list[AliasDiagnostic] = []
        seen: set[tuple[str, str, str]] = set()

        for edge in snapshot.edges:
            if edge.relation not in _ALIAS_RELATIONS:
                continue
            alias_node = nodes.get(edge.source)
            concept_node = nodes.get(edge.target)
            if alias_node is None or concept_node is None:
                continue
            alias = _alias_text(alias_node)
            if not alias:
                continue
            key = (alias, concept_node.id, edge.id)
            if key in seen:
                continue
            seen.add(key)

            is_approved = _is_approved_alias(edge, alias_node, concept_node)
            diagnostic = AliasDiagnostic(
                candidate_id=edge.id,
                alias=alias,
                concept_id=concept_node.id,
                canonical_id=str(
                    concept_node.attributes.get("canonical_id")
                    or concept_node.id
                ),
                concept_kind=concept_node.kind,
                relation=edge.relation,
                trust_state=(
                    _APPROVED_TRUST if is_approved else "discovery_only"
                ),
                evidence=tuple(
                    sorted(
                        {
                            AliasEvidence(
                                source_type=evidence.source_type,
                                trust_state=evidence.trust_state,
                            )
                            for evidence in (*alias_node.evidence, *edge.evidence)
                        },
                        key=lambda item: (item.source_type, item.trust_state),
                    )
                ),
            )
            (approved if is_approved else discovery).append(diagnostic)

        order = lambda item: (  # noqa: E731 - local deterministic sort key
            _normalize(item.alias),
            item.concept_id,
            item.candidate_id,
        )
        return cls(
            content_sha256=snapshot.revision.content_sha256,
            revision=(
                snapshot.revision.git_revision
                or snapshot.revision.content_sha256
            ),
            approved_aliases=tuple(sorted(approved, key=order)),
            discovery_aliases=tuple(sorted(discovery, key=order)),
        )

    @classmethod
    def empty(cls) -> RuntimeAliasIndex:
        return cls(content_sha256="")


class ContentHashDiagnosticCache:
    """Process-local immutable-index cache with no TTL or repository scanning.

    Values are immutable :class:`RuntimeAliasIndex` records.  The cache key is
    the repository projection's content SHA-256, which already includes the
    hashes of projection sources and extractor implementation files.
    """

    def __init__(self) -> None:
        self._entries: dict[str, RuntimeAliasIndex] = {}
        self._lock = threading.Lock()

    def get_or_build(self, snapshot: GraphSnapshot) -> RuntimeAliasIndex:
        if not isinstance(snapshot, GraphSnapshot):
            raise TypeError("snapshot must be a GraphSnapshot")
        key = snapshot.revision.content_sha256
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                return cached
            index = RuntimeAliasIndex.from_snapshot(snapshot)
            self._entries[key] = index
            return index

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


@dataclass(frozen=True)
class FailureSignal:
    """An exact structured signal extracted without searching the raw prompt."""

    failure_code: str
    received_symbol: str
    source: str
    source_span: str | None = None
    node_id: str | None = None
    diagnostic_only: bool = field(default=True, init=False)
    executable: bool = field(default=False, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_code": self.failure_code,
            "received_symbol": self.received_symbol,
            "source": self.source,
            "source_span": self.source_span,
            "node_id": self.node_id,
            "diagnostic_only": True,
            "executable": False,
        }


@dataclass(frozen=True)
class RuntimeDiagnosticStatus:
    state: str
    ready: bool
    reason: str | None
    index_revision: str | None
    diagnostic_only: bool = field(default=True, init=False)
    executable: bool = field(default=False, init=False)

    @property
    def available(self) -> bool:
        return self.ready

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ready": self.ready,
            "available": self.available,
            "reason": self.reason,
            "index_revision": self.index_revision,
            "diagnostic_only": True,
            "executable": False,
        }


@dataclass(frozen=True)
class RuntimeDiagnostics:
    """A response annotation only; every candidate is non-executable."""

    available: bool
    failure_code: str | None = None
    received_symbol: str | None = None
    approved_candidates: tuple[AliasDiagnostic, ...] = ()
    discovery_only_candidates: tuple[AliasDiagnostic, ...] = ()
    repeated_failures: tuple[RepeatedFailureDiagnostic, ...] = ()
    index_revision: str | None = None
    snapshot_content_sha256: str | None = None
    reason: str | None = None
    schema_version: str = field(
        default="capability-runtime-diagnostics/v1", init=False
    )
    diagnostic_only: bool = field(default=True, init=False)
    executable: bool = field(default=False, init=False)

    @classmethod
    def empty(
        cls,
        reason: str,
        *,
        failure_code: str | None = None,
        received_symbol: str | None = None,
    ) -> RuntimeDiagnostics:
        return cls(
            available=False,
            failure_code=failure_code,
            received_symbol=received_symbol,
            reason=reason,
        )

    @property
    def approved_alias_candidates(self) -> tuple[AliasDiagnostic, ...]:
        return self.approved_candidates

    def to_dict(self) -> dict[str, Any]:
        """Return a diagnostic annotation; status and SQL are never present."""

        return {
            "schema_version": self.schema_version,
            "diagnostic_only": True,
            "available": self.available,
            "failure_code": self.failure_code,
            "received_symbol": self.received_symbol,
            "approved_candidates": [
                candidate.to_dict() for candidate in self.approved_candidates
            ],
            "discovery_only_candidates": [
                candidate.to_dict()
                for candidate in self.discovery_only_candidates
            ],
            "repeated_failures": [
                failure.to_dict() for failure in self.repeated_failures
            ],
            "index_revision": self.index_revision,
            "snapshot_content_sha256": self.snapshot_content_sha256,
            "reason": self.reason,
            "executable": False,
        }


class RuntimeDiagnosticAdapter:
    """Lazy, one-build diagnostic facade for runtime request annotations.

    Supply exactly one of ``snapshot``, ``service``, or ``snapshot_loader``.
    Supplying none creates a disabled fail-open adapter.  ``initialize`` may be
    called during process startup; otherwise the first ``diagnose`` call starts
    one daemon build.  A timeout or exception returns empty diagnostics and
    never retries the repository build on each request.
    """

    def __init__(
        self,
        *,
        snapshot: GraphSnapshot | None = None,
        service: SnapshotService | None = None,
        snapshot_loader: Callable[[], GraphSnapshot] | None = None,
        cache: ContentHashDiagnosticCache | None = None,
        failure_log_provider: FailureLogProvider | None = None,
        failure_rows: Iterable[Mapping[str, Any]] | None = None,
        build_timeout_seconds: float = 0.05,
    ) -> None:
        sources = sum(
            source is not None for source in (snapshot, service, snapshot_loader)
        )
        if sources > 1:
            raise ValueError(
                "supply only one of snapshot, service, or snapshot_loader"
            )
        if failure_log_provider is not None and failure_rows is not None:
            raise ValueError(
                "supply only one of failure_log_provider or failure_rows"
            )
        if (
            isinstance(build_timeout_seconds, bool)
            or not isinstance(build_timeout_seconds, (int, float))
            or build_timeout_seconds <= 0
        ):
            raise ValueError("build_timeout_seconds must be positive")

        if snapshot is not None:
            loader: Callable[[], GraphSnapshot] | None = lambda: snapshot
        elif service is not None:
            loader = service.snapshot
        else:
            loader = snapshot_loader

        self._loader = loader
        self._failure_log_provider = failure_log_provider
        self._failure_rows = (
            tuple(failure_rows) if failure_rows is not None else None
        )
        self._cache = cache or ContentHashDiagnosticCache()
        self._build_timeout_seconds = float(build_timeout_seconds)
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._state = "new" if loader is not None else "failed"
        self._failure_reason = (
            None if loader is not None else "snapshot_unavailable"
        )
        self._index: RuntimeAliasIndex | None = None
        self._failure_summary = FailureLogSummary.unavailable(
            "failure_logs_not_configured"
        )
        if loader is None:
            self._ready.set()

    def initialize(self, *, timeout_seconds: float | None = None) -> bool:
        """Build once at startup or wait briefly for the first-request build."""

        timeout = (
            self._build_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise ValueError("timeout_seconds must be positive")

        with self._lock:
            if self._state == "new":
                self._state = "building"
                worker = threading.Thread(
                    target=self._build,
                    name="capability-diagnostic-snapshot",
                    daemon=True,
                )
                worker.start()
            if self._state == "ready":
                return True
            if self._state == "failed":
                return False
            ready = self._ready

        if not ready.wait(float(timeout)):
            return False
        with self._lock:
            return self._state == "ready"

    def diagnose(
        self,
        request_text: str,
        *,
        original_outcome: OriginalRuntimeOutcome | None = None,
        limit: int = 8,
    ) -> RuntimeDiagnostics:
        """Compatibility wrapper for an explicitly supplied exact symbol."""

        failure_code = (
            original_outcome.failure_code
            if original_outcome is not None and original_outcome.failure_code
            else _ALIAS_LOOKUP_FAILURE_CODE
        )
        return self.diagnose_failure(
            failure_code,
            request_text,
            original_outcome=original_outcome,
            limit=limit,
        )

    def diagnose_failure(
        self,
        failure_code: str,
        received_symbol: str,
        *,
        original_outcome: OriginalRuntimeOutcome | None = None,
        limit: int = 8,
        failure_rows: Iterable[Mapping[str, Any]] = (),
    ) -> RuntimeDiagnostics:
        """Diagnose one allowlisted exact failure signal without changing it.

        Alias matching is intentionally limited to
        ``catalog_symbol_unresolved``.  Other allowlisted failure codes may
        receive repeated-failure review evidence but can never receive alias
        candidates.  ``failure_rows`` must already have been selected by the
        application and are never fetched from a DB here.
        """

        if not isinstance(failure_code, str):
            raise TypeError("failure_code must be a string")
        if not isinstance(received_symbol, str):
            raise TypeError("received_symbol must be a string")
        if original_outcome is not None and not isinstance(
            original_outcome, OriginalRuntimeOutcome
        ):
            raise TypeError("original_outcome must be OriginalRuntimeOutcome")
        _validate_limit(limit)

        normalized_code = failure_code.strip().casefold()
        symbol = received_symbol.strip()
        if original_outcome is not None and original_outcome.failure_code:
            original_code = original_outcome.failure_code.strip().casefold()
            if original_code != normalized_code:
                return RuntimeDiagnostics.empty(
                    "original_outcome_mismatch",
                    failure_code=original_code,
                    received_symbol=symbol,
                )
            normalized_code = original_code

        if normalized_code not in FAILURE_ANALYSIS_ALLOWLIST:
            return RuntimeDiagnostics(
                available=True,
                failure_code=normalized_code or None,
                received_symbol=symbol or None,
                reason="failure_code_not_allowlisted",
            )
        if not symbol:
            return RuntimeDiagnostics(
                available=True,
                failure_code=normalized_code,
                reason="received_symbol_missing",
            )

        if not self.initialize():
            with self._lock:
                reason = self._failure_reason or "snapshot_build_timeout"
            return RuntimeDiagnostics.empty(
                reason,
                failure_code=normalized_code,
                received_symbol=symbol,
            )

        with self._lock:
            index = self._index
            cached_summary = self._failure_summary
        if index is None:  # Defensive fail-open guard for unexpected state races.
            return RuntimeDiagnostics.empty(
                "snapshot_unavailable",
                failure_code=normalized_code,
                received_symbol=symbol,
            )

        try:
            provided_rows = tuple(failure_rows)
            summary = (
                summarize_failures(provided_rows, limit=1000)
                if provided_rows
                else cached_summary
            )
        except Exception:
            summary = FailureLogSummary.unavailable(
                "failure_log_ingestion_failed"
            )
        normalized_symbol = _normalize(symbol)
        alias_lookup_allowed = normalized_code == _ALIAS_LOOKUP_FAILURE_CODE
        approved = (
            _matching(index.approved_aliases, normalized_symbol, symbol, limit)
            if alias_lookup_allowed
            else ()
        )
        discovery = (
            _matching(index.discovery_aliases, normalized_symbol, symbol, limit)
            if alias_lookup_allowed
            else ()
        )
        repeated = _matching_repeated_failures(
            summary,
            failure_code=normalized_code,
            normalized_subject=normalized_symbol,
            limit=limit,
        )
        return RuntimeDiagnostics(
            available=True,
            failure_code=normalized_code,
            received_symbol=symbol,
            approved_candidates=approved,
            discovery_only_candidates=discovery,
            repeated_failures=repeated,
            index_revision=index.revision,
            snapshot_content_sha256=index.content_sha256,
            reason=(
                "alias_lookup_not_applicable"
                if not alias_lookup_allowed
                else None
            ),
        )

    def diagnose_signal(
        self,
        signal: FailureSignal,
        *,
        original_outcome: OriginalRuntimeOutcome | None = None,
        limit: int = 8,
        failure_rows: Iterable[Mapping[str, Any]] = (),
    ) -> RuntimeDiagnostics:
        if not isinstance(signal, FailureSignal):
            raise TypeError("signal must be a FailureSignal")
        return self.diagnose_failure(
            signal.failure_code,
            signal.received_symbol,
            original_outcome=original_outcome,
            limit=limit,
            failure_rows=failure_rows,
        )

    def status(self) -> RuntimeDiagnosticStatus:
        """Return current adapter state without triggering a repository scan."""

        with self._lock:
            return RuntimeDiagnosticStatus(
                state=self._state,
                ready=self._state == "ready" and self._index is not None,
                reason=self._failure_reason,
                index_revision=(
                    self._index.revision if self._index is not None else None
                ),
            )

    def failure_summary(self) -> FailureLogSummary:
        """Return the one-time, allowlisted operational review aggregation."""

        if not self.initialize():
            with self._lock:
                reason = self._failure_reason or "snapshot_build_timeout"
            return FailureLogSummary.unavailable(reason)
        with self._lock:
            return self._failure_summary

    def summarize_failures(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        limit: int = 1000,
    ) -> FailureLogSummary:
        """Compatibility method for the read-only HTTP aggregation surface."""

        return summarize_failures(rows, limit=limit)

    def _build(self) -> None:
        try:
            loader = self._loader
            if loader is None:
                raise RuntimeError("snapshot loader is unavailable")
            snapshot = loader()
            index = self._cache.get_or_build(snapshot)
        except Exception:
            with self._lock:
                self._state = "failed"
                self._failure_reason = "snapshot_build_failed"
                self._index = None
                self._ready.set()
            return

        try:
            if self._failure_log_provider is not None:
                failure_rows = self._failure_log_provider.load_failure_rows()
                failure_summary = FailureLogIngestor().ingest(failure_rows)
            elif self._failure_rows is not None:
                failure_summary = FailureLogIngestor().ingest(self._failure_rows)
            else:
                failure_summary = FailureLogSummary.unavailable(
                    "failure_logs_not_configured"
                )
        except Exception:
            # Failure-log diagnostics are optional and must not take the alias
            # snapshot (or the existing runtime response) down with them.
            failure_summary = FailureLogSummary.unavailable(
                "failure_log_ingestion_failed"
            )

        with self._lock:
            self._index = index
            self._failure_summary = failure_summary
            self._state = "ready"
            self._failure_reason = None
            self._ready.set()


def extract_failure_signal(
    query_plan: Mapping[str, Any] | None,
    api_response: Mapping[str, Any] | None,
) -> FailureSignal | None:
    """Extract the first exact structured failure signal, or ``None``.

    The raw request/prompt is never accepted.  Signal priority follows the
    runtime's durable structures: audience issues, unsupported semantic
    operations, an explicitly reported symbol, and failed Event IR receipts.
    Missing-field and clarification path names are deliberately ignored.
    """

    plan = query_plan if isinstance(query_plan, Mapping) else {}
    response = api_response if isinstance(api_response, Mapping) else {}
    failure_code = _extract_allowlisted_failure_code(response, plan)
    if failure_code is None:
        return None

    audience = plan.get("audience_requirement")
    if isinstance(audience, Mapping):
        issues = audience.get("issues")
        if _is_sequence(issues):
            for issue in issues:
                if not isinstance(issue, Mapping):
                    continue
                issue_code = str(issue.get("code") or "").strip().casefold()
                argument = _clean_text(issue.get("argument"))
                if issue_code not in {
                    "unsupported_semantics",
                    "validation_mismatch",
                } or not argument:
                    continue
                evidence = issue.get("evidence")
                evidence_text = (
                    _clean_text(evidence.get("text"))
                    if isinstance(evidence, Mapping)
                    else None
                )
                return FailureSignal(
                    failure_code=failure_code,
                    received_symbol=argument,
                    source="audience_requirement.issues",
                    source_span=evidence_text,
                )

    for payload_name, payload in (("query_plan", plan), ("api_response", response)):
        semantic_ir = payload.get("semantic_ir")
        if not isinstance(semantic_ir, Mapping):
            continue
        operations = semantic_ir.get("unsupported_operations")
        if not _is_sequence(operations):
            continue
        for operation in operations:
            if not isinstance(operation, Mapping):
                continue
            operation_code = _first_allowlisted_code(
                operation.get("failure_code"),
                operation.get("kind"),
                operation.get("reason"),
            )
            if operation_code is not None and operation_code != failure_code:
                continue
            symbol = _first_text(
                operation,
                {},
                keys=("received_symbol", "unresolved_symbol", "symbol"),
            )
            if symbol:
                return FailureSignal(
                    failure_code=failure_code,
                    received_symbol=symbol,
                    source=f"{payload_name}.semantic_ir.unsupported_operations",
                    source_span=_span_text(operation.get("source_span")),
                    node_id=_clean_text(operation.get("node_id")),
                )

    # ``semantic_plan.capability_verdicts`` / ``semantic_plan.structurer_issues`` /
    # ``semantic_pipeline.normalization.outcomes`` 를 읽던 세 갈래는 2026-08-05 삭제됐다.
    # SemanticPlanV2 와 그 파이프라인 영수증이 폐기되어 세 구조 모두 플랜에 생길 수 없고,
    # 도달 불가능한 판독을 남겨 두면 진단 우선순위가 실제보다 넓어 보인다.

    explicit_symbol = _first_text(
        response,
        plan,
        keys=("received_symbol", "unresolved_symbol"),
    )
    if explicit_symbol:
        return FailureSignal(
            failure_code=failure_code,
            received_symbol=explicit_symbol,
            source="api_response.received_symbol",
        )

    for payload_name, payload in (("query_plan", plan), ("api_response", response)):
        event_expression = payload.get("event_expression")
        if not isinstance(event_expression, Mapping):
            continue
        receipts = event_expression.get("receipts")
        if not _is_sequence(receipts):
            continue
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            if str(receipt.get("status") or "").strip().casefold() != "failed":
                continue
            symbols = receipt.get("catalog_symbols")
            if not _is_sequence(symbols):
                continue
            for value in symbols:
                symbol = _clean_text(value)
                if symbol:
                    return FailureSignal(
                        failure_code=failure_code,
                        received_symbol=symbol,
                        source=f"{payload_name}.event_expression.receipts",
                    )
    return None


def _alias_text(node: GraphNode) -> str:
    if node.kind not in {"SurfaceTerm", "SymbolAlias", "OperatorAlias", "ValueAlias"}:
        return ""
    for key in ("text", "alias", "value", "name"):
        value = node.attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _approved_node(node: GraphNode) -> bool:
    return node.attributes.get("trust_state") == _APPROVED_TRUST


def _is_approved_alias(
    edge: GraphEdge, alias_node: GraphNode, concept_node: GraphNode
) -> bool:
    return bool(
        edge.relation == _APPROVED_ALIAS_RELATION
        and _approved_node(alias_node)
        and _approved_node(concept_node)
        and edge.evidence
        and all(
            evidence.trust_state == _APPROVED_TRUST
            for evidence in edge.evidence
        )
        and alias_node.evidence
        and all(
            evidence.trust_state == _APPROVED_TRUST
            for evidence in alias_node.evidence
        )
    )


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _matches(alias: str, normalized_symbol: str) -> bool:
    normalized_alias = _normalize(alias)
    if not normalized_alias or not normalized_symbol:
        return False
    return normalized_alias == normalized_symbol


def _matching(
    candidates: tuple[AliasDiagnostic, ...],
    normalized_symbol: str,
    received_symbol: str,
    limit: int,
) -> tuple[AliasDiagnostic, ...]:
    matches = [
        replace(candidate, received_symbol=received_symbol)
        for candidate in candidates
        if _matches(candidate.alias, normalized_symbol)
    ]
    matches.sort(
        key=lambda item: (
            -len(_normalize(item.alias)),
            _normalize(item.alias),
            item.concept_id,
            item.candidate_id,
        )
    )
    return tuple(matches[:limit])


def _matching_repeated_failures(
    summary: FailureLogSummary,
    *,
    failure_code: str,
    normalized_subject: str,
    limit: int,
) -> tuple[RepeatedFailureDiagnostic, ...]:
    return tuple(
        item
        for item in summary.repeated_failures
        if item.failure_code == failure_code
        and _normalize(item.subject) == normalized_subject
    )[:limit]


def _extract_allowlisted_failure_code(
    response: Mapping[str, Any], plan: Mapping[str, Any]
) -> str | None:
    explicit = (
        response.get("failure_code"),
        response.get("failure_reason"),
        response.get("error_code"),
    )
    explicit_present = any(str(value or "").strip() for value in explicit)
    explicit_code = _first_allowlisted_code(*explicit)
    # An explicit non-allowlisted final outcome is a hard boundary.  A stale or
    # incidental nested diagnostic must not opt that response into discovery.
    if explicit_present and explicit_code is None:
        return None

    nested_candidates: list[Any] = []
    for payload in (plan, response):
        for key in ("error", "diagnostics", "capability_diagnostics"):
            nested = payload.get(key)
            if isinstance(nested, Mapping):
                nested_candidates.extend(
                    (
                        nested.get("failure_code"),
                        nested.get("failure_reason"),
                        nested.get("error_code"),
                    )
                )
        decisions = payload.get("decisions")
        if _is_sequence(decisions):
            for decision in decisions:
                if isinstance(decision, Mapping):
                    nested_candidates.extend(
                        (
                            decision.get("failure_code"),
                            decision.get("reason"),
                            decision.get("code"),
                        )
                    )
        semantic_ir = payload.get("semantic_ir")
        if isinstance(semantic_ir, Mapping):
            operations = semantic_ir.get("unsupported_operations")
            if _is_sequence(operations):
                for operation in operations:
                    if isinstance(operation, Mapping):
                        nested_candidates.extend(
                            (
                                operation.get("failure_code"),
                                operation.get("kind"),
                                operation.get("reason"),
                            )
                        )
    nested_code = _first_allowlisted_code(*nested_candidates)
    # The durable top-level category is often semantic_ir_unsupported while a
    # decision/operation records the exact catalog failure needed for alias
    # lookup.  Prefer that narrower allowlisted signal only in this case.
    if explicit_code == "semantic_ir_unsupported" and nested_code is not None:
        return nested_code
    if explicit_code is not None:
        return explicit_code

    plan_direct = _first_allowlisted_code(
        plan.get("failure_code"),
        plan.get("failure_reason"),
        plan.get("error_code"),
    )
    return plan_direct or nested_code


def _first_allowlisted_code(*values: Any) -> str | None:
    for value in values:
        candidate = str(value or "").strip().casefold()
        if candidate in FAILURE_ANALYSIS_ALLOWLIST:
            return candidate
    return None


def _span_text(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, Mapping):
        return _clean_text(value.get("text") or value.get("source_text"))
    return None


def _is_sequence(value: Any) -> bool:
    return isinstance(value, (list, tuple))


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _first_text(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
) -> str:
    for source in (primary, secondary):
        for key in keys:
            text = _clean_text(source.get(key))
            if text:
                return text
    return ""


def _factor(value: Any, *, default: float, bounded: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return default
    if bounded and number > 1:
        return default
    return number


def _availability_factor(row: Mapping[str, Any]) -> float:
    value = row.get("legacy_asset_availability")
    if value is None:
        value = row.get("legacy_asset_available")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return _factor(value, default=0.5, bounded=True)


def _average(values: Iterable[float]) -> float:
    items = tuple(values)
    if not items:
        return 0.0
    return round(sum(items) / len(items), 6)


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")


__all__ = [
    "AliasDiagnostic",
    "AliasEvidence",
    "ContentHashDiagnosticCache",
    "FAILURE_ANALYSIS_ALLOWLIST",
    "FailureLogIngestor",
    "FailureLogProvider",
    "FailureLogRecord",
    "FailureLogSummary",
    "FailureSignal",
    "OriginalRuntimeOutcome",
    "RepeatedFailureDiagnostic",
    "RuntimeAliasIndex",
    "RuntimeDiagnosticAdapter",
    "RuntimeDiagnosticStatus",
    "RuntimeDiagnostics",
    "SnapshotService",
    "extract_failure_signal",
    "summarize_failures",
]
