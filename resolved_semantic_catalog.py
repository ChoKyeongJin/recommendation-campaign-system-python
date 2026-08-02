"""Resolved catalog for the canonical :mod:`event_ir` execution boundary.

The catalog deliberately contains *bindings*, not language rules.  A semantic
producer emits canonical identifiers; this module resolves those identifiers
to the fixed Event IR algebra and to :mod:`event_compiler`'s physical registry.
Adding a source or metric therefore changes data, not lowering code.

``ResolvedSemanticCatalog.from_compiler`` has two inputs:

* the already established ``event_compiler`` source/field registry; and
* an optional runtime mapping with ``sources``, ``fields``, ``metrics``,
  ``joins``, ``grains``, ``operators``, ``times`` and ``data_coverage``.

The runtime mapping is intentionally generic.  It has no campaign, purchase,
or other domain-specific keys and it never accepts SQL builder names or legacy
slots.  Fixed source predicates remain part of ``event_compiler.EventSpec``;
metric predicates are canonical Event IR supplied through ``where``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import event_compiler
import event_ir
import temporal_semantics


METRIC_KINDS = frozenset({"field", "aggregate", "existence"})
JOIN_CARDINALITIES = frozenset({"one_to_one", "many_to_one", "one_to_many", "many_to_many"})
WINDOW_TYPES = frozenset({"interval", "rolling", "relative"})
UNKNOWN_COVERAGE = "unknown"
SUBJECT_GRAIN = "subject"


class CatalogError(ValueError):
    """A catalog declaration is inconsistent or a canonical symbol is absent."""

    def __init__(self, code: str, message: str, *, symbol: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.symbol = symbol


def _non_empty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError("invalid_catalog_declaration", f"{label} must be a non-empty string")
    return value.strip()


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise CatalogError("invalid_catalog_declaration", f"{label} must be a string list")
    items = tuple(_non_empty(item, label) for item in value)
    if len(items) != len(set(items)):
        raise CatalogError("invalid_catalog_declaration", f"{label} contains duplicates")
    return items


def _date_or_none(value: Any, label: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise CatalogError("invalid_catalog_declaration", f"{label} must be an ISO date") from exc


@dataclass(frozen=True)
class DataCoverageSpec:
    """Known availability bounds for a source or metric.

    Missing bounds mean "not declared", not "unbounded".  The lowerer only
    rejects a window when a declared bound proves it cannot be answered.
    """

    id: str
    available_from: date | None = None
    complete_through: date | None = None
    max_lookback_days: int | None = None
    timezone: str | None = None

    def __post_init__(self) -> None:
        _non_empty(self.id, "data_coverage.id")
        if self.max_lookback_days is not None and (
            isinstance(self.max_lookback_days, bool) or self.max_lookback_days <= 0
        ):
            raise CatalogError(
                "invalid_catalog_declaration",
                f"data coverage {self.id!r} has an invalid max_lookback_days",
                symbol=self.id,
            )
        if (
            self.available_from is not None
            and self.complete_through is not None
            and self.available_from > self.complete_through
        ):
            raise CatalogError(
                "invalid_catalog_declaration",
                f"data coverage {self.id!r} starts after complete_through",
                symbol=self.id,
            )


@dataclass(frozen=True)
class SourceSpec:
    """Logical Event IR source plus its physical ``EventSpec`` binding."""

    id: str
    event: event_compiler.EventSpec
    time_field: str
    coverage: str = UNKNOWN_COVERAGE

    def __post_init__(self) -> None:
        _non_empty(self.id, "source.id")
        _non_empty(self.time_field, f"source {self.id}.time_field")
        if self.event.binding not in {"fact_table", "subject_column"}:
            raise CatalogError(
                "invalid_catalog_declaration",
                f"source {self.id!r} has unknown binding {self.event.binding!r}",
                symbol=self.id,
            )


@dataclass(frozen=True)
class FieldSpec:
    """Logical field and the compiler field it resolves to."""

    id: str
    source: str
    compiler_field: event_compiler.FieldSpec
    nullable: bool = True
    allowed_operators: tuple[str, ...] = ()
    coverage: str = UNKNOWN_COVERAGE

    @property
    def data_type(self) -> str:
        return self.compiler_field.data_type

    def __post_init__(self) -> None:
        _non_empty(self.id, "field.id")
        _non_empty(self.source, f"field {self.id}.source")
        if "." not in self.id:
            raise CatalogError(
                "invalid_catalog_declaration",
                f"field {self.id!r} must use '<source>.<field>' canonical form",
                symbol=self.id,
            )


@dataclass(frozen=True)
class JoinSpec:
    """A validated join edge.  It lowers directly to ``event_ir.Join``."""

    id: str
    left_source: str
    right_source: str
    left_field: str
    right_field: str
    cardinality: str = "many_to_one"
    operator: str = "="

    def __post_init__(self) -> None:
        _non_empty(self.id, "join.id")
        if self.cardinality not in JOIN_CARDINALITIES:
            raise CatalogError(
                "invalid_catalog_declaration",
                f"join {self.id!r} has unknown cardinality {self.cardinality!r}",
                symbol=self.id,
            )


@dataclass(frozen=True)
class GrainSpec:
    """Aggregation grain.  Empty keys mean the correlated audience subject."""

    id: str
    keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.id, "grain.id")


@dataclass(frozen=True)
class OperatorSpec:
    """Canonical comparison operator and the Event IR symbol it emits."""

    id: str
    symbol: str
    value_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.id, "operator.id")
        if self.symbol not in event_ir.COMPARISON_OPERATORS:
            raise CatalogError(
                "invalid_catalog_declaration",
                f"operator {self.id!r} maps to unsupported Event IR symbol {self.symbol!r}",
                symbol=self.id,
            )


@dataclass(frozen=True)
class TimeSpec:
    """How a metric receives a window and which temporal operations it accepts."""

    id: str
    field: str
    window_types: tuple[str, ...] = tuple(sorted(WINDOW_TYPES))
    temporal_operators: tuple[str, ...] = ("WITHIN_INTERVAL", "AT_LEAST_ONCE_IN_INTERVAL")
    required: bool = False
    coverage: str = UNKNOWN_COVERAGE

    def __post_init__(self) -> None:
        _non_empty(self.id, "time.id")
        unknown = set(self.window_types) - WINDOW_TYPES
        if unknown:
            raise CatalogError(
                "invalid_catalog_declaration",
                f"time {self.id!r} has unknown window types {sorted(unknown)}",
                symbol=self.id,
            )


@dataclass(frozen=True)
class MetricSpec:
    """Declarative lowering recipe for a canonical metric.

    ``kind`` selects one of three stable algebraic shapes.  New metric ids do
    not add a lowerer branch:

    * ``field`` -> a field comparison (wrapped in ``Exists`` for fact sources)
    * ``aggregate`` -> ``Comparison(Aggregate(...), Literal(...))``
    * ``existence`` -> ``Exists(relation)`` compared to a boolean requirement
    """

    id: str
    source: str
    kind: str
    aggregate_function: str | None = None
    expression_field: str | None = None
    distinct: bool = False
    allowed_operators: tuple[str, ...] = tuple(sorted(event_ir.COMPARISON_OPERATORS))
    value_type: str = "number"
    grain: str = SUBJECT_GRAIN
    joins: tuple[str, ...] = ()
    time: str | None = None
    coverage: str = UNKNOWN_COVERAGE
    where: event_ir.Condition | None = None

    def __post_init__(self) -> None:
        _non_empty(self.id, "metric.id")
        _non_empty(self.source, f"metric {self.id}.source")
        if self.kind not in METRIC_KINDS:
            raise CatalogError(
                "invalid_catalog_declaration",
                f"metric {self.id!r} has unknown kind {self.kind!r}",
                symbol=self.id,
            )
        if self.kind == "aggregate":
            if self.aggregate_function not in event_ir.AGGREGATE_FUNCTIONS:
                raise CatalogError(
                    "invalid_catalog_declaration",
                    f"metric {self.id!r} needs a supported aggregate_function",
                    symbol=self.id,
                )
            if self.aggregate_function != "count" and not self.expression_field:
                raise CatalogError(
                    "invalid_catalog_declaration",
                    f"metric {self.id!r} needs expression_field for {self.aggregate_function}",
                    symbol=self.id,
                )
        elif self.kind == "field" and not self.expression_field:
            raise CatalogError(
                "invalid_catalog_declaration",
                f"field metric {self.id!r} needs expression_field",
                symbol=self.id,
            )
        elif self.kind == "existence" and self.aggregate_function is not None:
            raise CatalogError(
                "invalid_catalog_declaration",
                f"existence metric {self.id!r} cannot declare aggregate_function",
                symbol=self.id,
            )
        if self.kind == "existence" and self.expression_field is not None:
            raise CatalogError(
                "invalid_catalog_declaration",
                f"existence metric {self.id!r} cannot declare expression_field",
                symbol=self.id,
            )
        if self.distinct and not self.expression_field:
            raise CatalogError(
                "invalid_catalog_declaration",
                f"distinct metric {self.id!r} needs expression_field",
                symbol=self.id,
            )


def _proxy(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(sorted(values.items())))


@dataclass(frozen=True)
class ResolvedSemanticCatalog:
    """Immutable, cross-reference-checked semantic and physical catalog."""

    sources: Mapping[str, SourceSpec]
    fields: Mapping[str, FieldSpec]
    metrics: Mapping[str, MetricSpec]
    joins: Mapping[str, JoinSpec]
    grains: Mapping[str, GrainSpec]
    operators: Mapping[str, OperatorSpec]
    times: Mapping[str, TimeSpec]
    data_coverage: Mapping[str, DataCoverageSpec]
    subject: event_compiler.SubjectSpec = field(default_factory=event_compiler.SubjectSpec)
    compiler_events: Mapping[str, event_compiler.EventSpec] = field(default_factory=dict)
    compiler_fields: Mapping[str, event_compiler.FieldSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "sources", "fields", "metrics", "joins", "grains", "operators", "times",
            "data_coverage", "compiler_events", "compiler_fields",
        ):
            object.__setattr__(self, name, _proxy(getattr(self, name)))
        self._validate_references()

    @classmethod
    def from_compiler(
        cls,
        *,
        registry: Mapping[str, event_compiler.EventSpec] | None = None,
        fields: Mapping[str, event_compiler.FieldSpec] | None = None,
        runtime_config: Mapping[str, Any] | None = None,
        subject: event_compiler.SubjectSpec | None = None,
    ) -> "ResolvedSemanticCatalog":
        """Resolve compiler bindings and generic runtime declarations once."""

        subject_spec = subject or event_compiler.SubjectSpec()
        raw = dict(runtime_config or {})
        nested = raw.get("semantic_catalog") or raw.get("canonical_event_catalog")
        if nested is not None:
            if not isinstance(nested, Mapping):
                raise CatalogError("invalid_catalog_declaration", "semantic_catalog must be an object")
            raw = dict(nested)

        resolved_events = dict(registry or event_compiler.resolve_registry())
        for source_id, declaration in _section(raw, "sources").items():
            resolved_events[source_id] = _event_spec(source_id, declaration, subject_spec)

        runtime_fields = {
            field_id: _compiler_field(field_id, declaration)
            for field_id, declaration in _section(raw, "fields").items()
        }
        if fields is None:
            resolved_fields = event_compiler.resolve_fields(resolved_events, runtime_fields)
        else:
            resolved_fields = {**fields, **runtime_fields}
            # A supplied field registry may predate runtime-added sources.  The
            # compiler's derived occurred_at fields are still part of the contract.
            for source_id, source in resolved_events.items():
                resolved_fields.setdefault(
                    f"{source_id}.{event_ir.TIME_FIELD_SUFFIX}",
                    event_compiler.FieldSpec(
                        source=source_id,
                        column=source.time_column,
                        data_type={"char8": "date_char8", "date": "date"}.get(source.time_format, "date"),
                    ),
                )

        coverage = {UNKNOWN_COVERAGE: DataCoverageSpec(id=UNKNOWN_COVERAGE)}
        coverage.update({
            item_id: _coverage_spec(item_id, declaration)
            for item_id, declaration in _section(raw, "data_coverage").items()
        })

        source_specs: dict[str, SourceSpec] = {}
        runtime_sources = _section(raw, "sources")
        for source_id, compiler_spec in resolved_events.items():
            declaration = runtime_sources.get(source_id, {})
            declaration = declaration if isinstance(declaration, Mapping) else {}
            source_specs[source_id] = SourceSpec(
                id=source_id,
                event=compiler_spec,
                time_field=f"{source_id}.{event_ir.TIME_FIELD_SUFFIX}",
                coverage=str(declaration.get("coverage") or UNKNOWN_COVERAGE),
            )

        # Subject is a relation scope for FieldRef even though it is not an EventSpec.
        subject_event = event_compiler.EventSpec(
            table=subject_spec.table,
            alias=subject_spec.alias,
            subject_key=subject_spec.key,
            event_subject_key=subject_spec.key,
            time_column=subject_spec.key,
            time_format="date",
            binding="subject_column",
            label=subject_spec.name,
        )
        source_specs.setdefault(
            subject_spec.name,
            SourceSpec(
                id=subject_spec.name,
                event=subject_event,
                time_field=f"{subject_spec.name}.{event_ir.TIME_FIELD_SUFFIX}",
            ),
        )

        runtime_field_specs = _section(raw, "fields")
        field_specs: dict[str, FieldSpec] = {}
        for field_id, compiler_field in resolved_fields.items():
            declaration = runtime_field_specs.get(field_id, {})
            declaration = declaration if isinstance(declaration, Mapping) else {}
            field_specs[field_id] = FieldSpec(
                id=field_id,
                source=compiler_field.source,
                compiler_field=compiler_field,
                nullable=bool(declaration.get("nullable", True)),
                allowed_operators=_string_tuple(
                    declaration.get("allowed_operators"), f"field {field_id}.allowed_operators"
                ),
                coverage=str(declaration.get("coverage") or UNKNOWN_COVERAGE),
            )

        operators = {
            symbol: OperatorSpec(id=symbol, symbol=symbol)
            for symbol in sorted(event_ir.COMPARISON_OPERATORS)
        }
        operators.update({
            item_id: _operator_spec(item_id, declaration)
            for item_id, declaration in _section(raw, "operators").items()
        })

        grains = {SUBJECT_GRAIN: GrainSpec(id=SUBJECT_GRAIN)}
        grains.update({
            item_id: _grain_spec(item_id, declaration)
            for item_id, declaration in _section(raw, "grains").items()
        })

        times: dict[str, TimeSpec] = {}
        for source_id, source_spec in source_specs.items():
            if source_id == subject_spec.name or source_spec.time_field not in field_specs:
                continue
            times[_default_time_id(source_id)] = TimeSpec(
                id=_default_time_id(source_id),
                field=source_spec.time_field,
                coverage=source_spec.coverage,
            )
        times.update({
            item_id: _time_spec(item_id, declaration)
            for item_id, declaration in _section(raw, "times").items()
        })

        joins = {
            item_id: _join_spec(item_id, declaration)
            for item_id, declaration in _section(raw, "joins").items()
        }

        # Existing fields and events are immediately useful without duplicating
        # them in runtime config.  Explicit metric declarations override these
        # conservative field/existence projections.
        metrics: dict[str, MetricSpec] = {
            source_id: MetricSpec(
                id=source_id,
                source=source_id,
                kind="existence",
                allowed_operators=("=", "!="),
                value_type="boolean",
                time=(_default_time_id(source_id) if _default_time_id(source_id) in times else None),
                coverage=source.coverage,
            )
            for source_id, source in source_specs.items()
            if source_id != subject_spec.name
        }
        for field_id, field_spec in field_specs.items():
            metrics[field_id] = MetricSpec(
                id=field_id,
                source=field_spec.source,
                kind="field",
                expression_field=field_id,
                allowed_operators=(field_spec.allowed_operators or tuple(sorted(event_ir.COMPARISON_OPERATORS))),
                value_type=field_spec.data_type,
                time=(
                    _default_time_id(field_spec.source)
                    if field_spec.source != subject_spec.name and _default_time_id(field_spec.source) in times
                    else None
                ),
                coverage=field_spec.coverage,
            )
        for item_id, declaration in _section(raw, "metrics").items():
            metric = _metric_spec(item_id, declaration)
            default_time = _default_time_id(metric.source)
            if metric.time is None and default_time in times:
                metric = replace(metric, time=default_time)
            if metric.coverage == UNKNOWN_COVERAGE and metric.source in source_specs:
                metric = replace(metric, coverage=source_specs[metric.source].coverage)
            metrics[item_id] = metric

        return cls(
            sources=source_specs,
            fields=field_specs,
            metrics=metrics,
            joins=joins,
            grains=grains,
            operators=operators,
            times=times,
            data_coverage=coverage,
            subject=subject_spec,
            compiler_events=resolved_events,
            compiler_fields=resolved_fields,
        )

    def _validate_references(self) -> None:
        def require(mapping: Mapping[str, Any], symbol: str, owner: str, kind: str) -> None:
            if symbol not in mapping:
                raise CatalogError(
                    "catalog_reference_unresolved",
                    f"{owner} references unknown {kind} {symbol!r}",
                    symbol=symbol,
                )

        for source in self.sources.values():
            require(self.data_coverage, source.coverage, f"source {source.id!r}", "coverage")
            if source.id != self.subject.name:
                require(self.compiler_events, source.id, f"source {source.id!r}", "compiler event")
        for field_spec in self.fields.values():
            require(self.sources, field_spec.source, f"field {field_spec.id!r}", "source")
            require(self.data_coverage, field_spec.coverage, f"field {field_spec.id!r}", "coverage")
            require(self.compiler_fields, field_spec.id, f"field {field_spec.id!r}", "compiler field")
            for operator in field_spec.allowed_operators:
                self.resolve_operator(operator)
        for join in self.joins.values():
            require(self.sources, join.left_source, f"join {join.id!r}", "left source")
            require(self.sources, join.right_source, f"join {join.id!r}", "right source")
            require(self.fields, join.left_field, f"join {join.id!r}", "left field")
            require(self.fields, join.right_field, f"join {join.id!r}", "right field")
            if self.fields[join.left_field].source != join.left_source:
                raise CatalogError("catalog_reference_mismatch", f"join {join.id!r} left field/source mismatch")
            if self.fields[join.right_field].source != join.right_source:
                raise CatalogError("catalog_reference_mismatch", f"join {join.id!r} right field/source mismatch")
            self.resolve_operator(join.operator)
        for grain in self.grains.values():
            for key in grain.keys:
                require(self.fields, key, f"grain {grain.id!r}", "field")
        for time in self.times.values():
            require(self.fields, time.field, f"time {time.id!r}", "field")
            require(self.data_coverage, time.coverage, f"time {time.id!r}", "coverage")
            unknown_temporal = set(time.temporal_operators) - temporal_semantics.OPERATORS
            if unknown_temporal:
                raise CatalogError(
                    "invalid_catalog_declaration",
                    f"time {time.id!r} has unknown temporal operators {sorted(unknown_temporal)}",
                    symbol=time.id,
                )
            if self.fields[time.field].data_type not in {"date", "date_char8", "date_string"}:
                raise CatalogError(
                    "catalog_reference_mismatch",
                    f"time {time.id!r} field {time.field!r} is not date typed",
                    symbol=time.field,
                )
        for metric in self.metrics.values():
            require(self.sources, metric.source, f"metric {metric.id!r}", "source")
            require(self.grains, metric.grain, f"metric {metric.id!r}", "grain")
            require(self.data_coverage, metric.coverage, f"metric {metric.id!r}", "coverage")
            if (
                metric.kind == "aggregate"
                and self.sources[metric.source].event.binding != "fact_table"
            ):
                raise CatalogError(
                    "catalog_reference_mismatch",
                    f"aggregate metric {metric.id!r} requires a fact_table source",
                    symbol=metric.source,
                )
            if metric.expression_field:
                require(self.fields, metric.expression_field, f"metric {metric.id!r}", "field")
            if metric.time:
                require(self.times, metric.time, f"metric {metric.id!r}", "time")
            for join in metric.joins:
                require(self.joins, join, f"metric {metric.id!r}", "join")
            for operator in metric.allowed_operators:
                self.resolve_operator(operator)
            available_sources = {metric.source, self.subject.name}
            for join_id in metric.joins:
                join = self.joins[join_id]
                if join.left_source not in available_sources or join.right_source in available_sources:
                    raise CatalogError(
                        "catalog_reference_mismatch",
                        f"metric {metric.id!r} has disconnected or cyclic join {join_id!r}",
                        symbol=join_id,
                    )
                available_sources.add(join.right_source)
            if (
                metric.expression_field
                and self.fields[metric.expression_field].source not in available_sources
            ):
                raise CatalogError(
                    "catalog_reference_mismatch",
                    f"metric {metric.id!r} expression field is outside its relation scope",
                    symbol=metric.expression_field,
                )
            for key in self.grains[metric.grain].keys:
                if self.fields[key].source not in available_sources:
                    raise CatalogError(
                        "catalog_reference_mismatch",
                        f"metric {metric.id!r} grain field is outside its relation scope",
                        symbol=key,
                    )
            if metric.time and self.fields[self.times[metric.time].field].source not in available_sources:
                raise CatalogError(
                    "catalog_reference_mismatch",
                    f"metric {metric.id!r} time field is outside its relation scope",
                    symbol=self.times[metric.time].field,
                )
            if metric.where is not None:
                for source in event_ir.sources(metric.where):
                    require(self.sources, source, f"metric {metric.id!r} where", "source")
                    if source not in available_sources:
                        raise CatalogError(
                            "catalog_reference_mismatch",
                            f"metric {metric.id!r} where source is outside its relation scope",
                            symbol=source,
                        )
                for field_id in event_ir.field_names(metric.where):
                    require(self.fields, field_id, f"metric {metric.id!r} where", "field")

    def source(self, symbol: str) -> SourceSpec:
        return self._required(self.sources, symbol, "source")

    def field(self, symbol: str) -> FieldSpec:
        return self._required(self.fields, symbol, "field")

    def metric(self, symbol: str) -> MetricSpec:
        return self._required(self.metrics, symbol, "metric")

    def join(self, symbol: str) -> JoinSpec:
        return self._required(self.joins, symbol, "join")

    def grain(self, symbol: str) -> GrainSpec:
        return self._required(self.grains, symbol, "grain")

    def time(self, symbol: str) -> TimeSpec:
        return self._required(self.times, symbol, "time")

    def coverage(self, symbol: str) -> DataCoverageSpec:
        return self._required(self.data_coverage, symbol, "data coverage")

    @staticmethod
    def _required(mapping: Mapping[str, Any], symbol: str, kind: str) -> Any:
        item = mapping.get(symbol)
        if item is None:
            raise CatalogError(
                f"catalog_{kind.replace(' ', '_')}_unregistered",
                f"canonical {kind} is not registered: {symbol!r}",
                symbol=symbol,
            )
        return item

    def resolve_operator(self, symbol: str) -> OperatorSpec:
        direct = self.operators.get(symbol)
        if direct is not None:
            return direct
        by_symbol = [item for item in self.operators.values() if item.symbol == symbol]
        if len(by_symbol) == 1:
            return by_symbol[0]
        raise CatalogError(
            "catalog_operator_unregistered",
            f"canonical operator is not registered: {symbol!r}",
            symbol=symbol,
        )

    def compile_context(self, **overrides: Any) -> event_compiler.CompileContext:
        """Create the compiler context from the exact same resolved bindings."""

        return event_compiler.CompileContext(
            subject=overrides.pop("subject", self.subject),
            registry=dict(self.compiler_events),
            fields=dict(self.compiler_fields),
            **overrides,
        )


def _section(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not value:
            return {}
        result: dict[str, Any] = {}
        for item in value:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                raise CatalogError(
                    "invalid_catalog_declaration", f"{name} list entries need a string id"
                )
            result[str(item["id"])] = item
        return result
    if not isinstance(value, Mapping):
        raise CatalogError("invalid_catalog_declaration", f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _default_time_id(source_id: str) -> str:
    return f"{source_id}.event_time"


def _event_spec(
    source_id: str, declaration: Any, subject: event_compiler.SubjectSpec
) -> event_compiler.EventSpec:
    if isinstance(declaration, event_compiler.EventSpec):
        return declaration
    if not isinstance(declaration, Mapping):
        raise CatalogError("invalid_catalog_declaration", f"source {source_id!r} must be an object")
    try:
        return event_compiler.EventSpec(
            table=_non_empty(declaration.get("table"), f"source {source_id}.table"),
            alias=_non_empty(declaration.get("alias"), f"source {source_id}.alias"),
            subject_key=str(declaration.get("subject_key") or subject.key),
            event_subject_key=_non_empty(
                declaration.get("event_subject_key"), f"source {source_id}.event_subject_key"
            ),
            time_column=_non_empty(
                declaration.get("time_column"), f"source {source_id}.time_column"
            ),
            time_format=str(declaration.get("time_format") or "char8"),
            binding=str(declaration.get("binding") or "fact_table"),
            extra_predicates=_string_tuple(
                declaration.get("extra_predicates"), f"source {source_id}.extra_predicates"
            ),
            label=str(declaration.get("label") or source_id),
            from_sql=str(declaration.get("from_sql") or ""),
            correlation_sql=str(declaration.get("correlation_sql") or ""),
            time_expression=str(declaration.get("time_expression") or ""),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, CatalogError):
            raise
        raise CatalogError("invalid_catalog_declaration", f"invalid source {source_id!r}: {exc}") from exc


def _compiler_field(field_id: str, declaration: Any) -> event_compiler.FieldSpec:
    if isinstance(declaration, event_compiler.FieldSpec):
        return declaration
    if not isinstance(declaration, Mapping):
        raise CatalogError("invalid_catalog_declaration", f"field {field_id!r} must be an object")
    source = str(declaration.get("source") or field_id.partition(".")[0])
    return event_compiler.FieldSpec(
        source=_non_empty(source, f"field {field_id}.source"),
        column=_non_empty(declaration.get("column"), f"field {field_id}.column"),
        data_type=str(declaration.get("data_type") or "number"),
        expression=str(declaration.get("expression") or ""),
    )


def _coverage_spec(item_id: str, declaration: Any) -> DataCoverageSpec:
    if isinstance(declaration, DataCoverageSpec):
        return declaration
    if not isinstance(declaration, Mapping):
        raise CatalogError("invalid_catalog_declaration", f"coverage {item_id!r} must be an object")
    lookback = declaration.get("max_lookback_days")
    if lookback is not None and (not isinstance(lookback, int) or isinstance(lookback, bool)):
        raise CatalogError("invalid_catalog_declaration", f"coverage {item_id}.max_lookback_days must be int")
    return DataCoverageSpec(
        id=item_id,
        available_from=_date_or_none(declaration.get("available_from"), f"coverage {item_id}.available_from"),
        complete_through=_date_or_none(
            declaration.get("complete_through"), f"coverage {item_id}.complete_through"
        ),
        max_lookback_days=lookback,
        timezone=(str(declaration["timezone"]) if declaration.get("timezone") else None),
    )


def _operator_spec(item_id: str, declaration: Any) -> OperatorSpec:
    if isinstance(declaration, OperatorSpec):
        return declaration
    if not isinstance(declaration, Mapping):
        raise CatalogError("invalid_catalog_declaration", f"operator {item_id!r} must be an object")
    return OperatorSpec(
        id=item_id,
        symbol=str(declaration.get("symbol") or item_id),
        value_types=_string_tuple(declaration.get("value_types"), f"operator {item_id}.value_types"),
    )


def _grain_spec(item_id: str, declaration: Any) -> GrainSpec:
    if isinstance(declaration, GrainSpec):
        return declaration
    if declaration is None:
        declaration = {}
    if not isinstance(declaration, Mapping):
        raise CatalogError("invalid_catalog_declaration", f"grain {item_id!r} must be an object")
    return GrainSpec(id=item_id, keys=_string_tuple(declaration.get("keys"), f"grain {item_id}.keys"))


def _time_spec(item_id: str, declaration: Any) -> TimeSpec:
    if isinstance(declaration, TimeSpec):
        return declaration
    if not isinstance(declaration, Mapping):
        raise CatalogError("invalid_catalog_declaration", f"time {item_id!r} must be an object")
    return TimeSpec(
        id=item_id,
        field=_non_empty(declaration.get("field"), f"time {item_id}.field"),
        window_types=(
            _string_tuple(declaration.get("window_types"), f"time {item_id}.window_types")
            or tuple(sorted(WINDOW_TYPES))
        ),
        temporal_operators=(
            _string_tuple(
                declaration.get("temporal_operators"), f"time {item_id}.temporal_operators"
            )
            or ("WITHIN_INTERVAL", "AT_LEAST_ONCE_IN_INTERVAL")
        ),
        required=bool(declaration.get("required", False)),
        coverage=str(declaration.get("coverage") or UNKNOWN_COVERAGE),
    )


def _join_spec(item_id: str, declaration: Any) -> JoinSpec:
    if isinstance(declaration, JoinSpec):
        return declaration
    if not isinstance(declaration, Mapping):
        raise CatalogError("invalid_catalog_declaration", f"join {item_id!r} must be an object")
    return JoinSpec(
        id=item_id,
        left_source=_non_empty(declaration.get("left_source"), f"join {item_id}.left_source"),
        right_source=_non_empty(declaration.get("right_source"), f"join {item_id}.right_source"),
        left_field=_non_empty(declaration.get("left_field"), f"join {item_id}.left_field"),
        right_field=_non_empty(declaration.get("right_field"), f"join {item_id}.right_field"),
        cardinality=str(declaration.get("cardinality") or "many_to_one"),
        operator=str(declaration.get("operator") or "="),
    )


def _metric_spec(item_id: str, declaration: Any) -> MetricSpec:
    if isinstance(declaration, MetricSpec):
        return declaration
    if not isinstance(declaration, Mapping):
        raise CatalogError("invalid_catalog_declaration", f"metric {item_id!r} must be an object")
    function = declaration.get("aggregate_function", declaration.get("function"))
    kind = str(
        declaration.get("kind")
        or declaration.get("semantic_type")
        or ("aggregate" if function is not None else "")
    )
    field_id = declaration.get(
        "expression_field", declaration.get("field", declaration.get("expression"))
    )
    raw_where = declaration.get("where")
    try:
        where = (
            raw_where
            if isinstance(raw_where, event_ir.ATOM_TYPES + (event_ir.And, event_ir.Or, event_ir.Not, event_ir.TimeFilter))
            else event_ir.condition_from_dict(raw_where) if isinstance(raw_where, Mapping) else None
        )
    except event_ir.IrSchemaError as exc:
        raise CatalogError(
            "invalid_catalog_declaration", f"metric {item_id!r} has invalid Event IR where: {exc}"
        ) from exc
    allowed = _string_tuple(
        declaration.get("allowed_operators", declaration.get("operators")),
        f"metric {item_id}.allowed_operators",
    )
    return MetricSpec(
        id=item_id,
        source=_non_empty(declaration.get("source"), f"metric {item_id}.source"),
        kind=kind,
        aggregate_function=(str(function).casefold() if function is not None else None),
        expression_field=(str(field_id) if field_id else None),
        distinct=bool(declaration.get("distinct", False)),
        allowed_operators=allowed or tuple(sorted(event_ir.COMPARISON_OPERATORS)),
        value_type=str(declaration.get("value_type") or declaration.get("data_type") or "number"),
        grain=str(declaration.get("grain") or SUBJECT_GRAIN),
        joins=_string_tuple(declaration.get("joins"), f"metric {item_id}.joins"),
        time=(str(declaration["time"]) if declaration.get("time") else None),
        coverage=str(declaration.get("coverage") or UNKNOWN_COVERAGE),
        where=where,
    )


def resolve_semantic_catalog(
    *,
    registry: Mapping[str, event_compiler.EventSpec] | None = None,
    fields: Mapping[str, event_compiler.FieldSpec] | None = None,
    runtime_config: Mapping[str, Any] | None = None,
    subject: event_compiler.SubjectSpec | None = None,
) -> ResolvedSemanticCatalog:
    """Convenience facade used by composition roots and tests."""

    return ResolvedSemanticCatalog.from_compiler(
        registry=registry,
        fields=fields,
        runtime_config=runtime_config,
        subject=subject,
    )


__all__ = [
    "CatalogError",
    "DataCoverageSpec",
    "FieldSpec",
    "GrainSpec",
    "JoinSpec",
    "MetricSpec",
    "OperatorSpec",
    "ResolvedSemanticCatalog",
    "SourceSpec",
    "TimeSpec",
    "resolve_semantic_catalog",
]
