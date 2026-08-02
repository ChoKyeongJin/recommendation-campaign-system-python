"""Deterministic ``SemanticPlanV2`` -> canonical :mod:`event_ir` lowering.

This is the execution boundary between requirement semantics and physical SQL.
It does not inspect the original sentence, resolve aliases, choose legacy
slots, or contain a branch for any business event.  Every leaf is lowered from
its canonical metric declaration in :mod:`resolved_semantic_catalog`.

Lowering is fail-closed.  If one requirement cannot be represented, the public
``LoweringResult.expression`` is ``None``; successfully lowered siblings remain
visible only through receipts and can never be executed as a narrowed subset.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

import event_ir
import semantic_fields
import semantic_plan
import temporal_semantics
from resolved_semantic_catalog import (
    CatalogError,
    DataCoverageSpec,
    MetricSpec,
    ResolvedSemanticCatalog,
)


LOWERED = "lowered"
FAILED = "failed"
SUPPORTED = "supported"
PARTIALLY_SUPPORTED = "partially_supported"
UNSUPPORTED = "unsupported"

MISSING_ARGUMENT = "missing_argument"
CATALOG_SYMBOL_UNRESOLVED = "catalog_symbol_unresolved"
CATALOG_CONTRACT_MISMATCH = "catalog_contract_mismatch"
VALUE_NOT_NORMALIZED = "value_not_normalized"
OPERATION_UNSUPPORTED = "operation_unsupported"
DATA_COVERAGE_GAP = "data_coverage_gap"
CHILD_LOWERING_FAILED = "child_lowering_failed"


class LoweringError(ValueError):
    def __init__(
        self,
        failure_code: str,
        message: str,
        *,
        symbol: str | None = None,
        catalog_symbols: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.symbol = symbol
        self.catalog_symbols = catalog_symbols


@dataclass(frozen=True)
class LoweringReceipt:
    """One semantic node's auditable lowering outcome."""

    node_id: str
    status: str
    failure_code: str | None = None
    expression_fingerprint: str | None = None
    catalog_symbols: tuple[str, ...] = ()
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "node_id": self.node_id,
            "status": self.status,
            "failure_code": self.failure_code,
            "expression_fingerprint": self.expression_fingerprint,
            "catalog_symbols": list(self.catalog_symbols),
        }
        if self.message:
            payload["message"] = self.message
        return payload


@dataclass(frozen=True)
class LoweringResult:
    """Whole-plan result.  ``expression`` exists only for an all-or-nothing success."""

    status: str
    expression: event_ir.Condition | None
    receipts: tuple[LoweringReceipt, ...]
    expression_fingerprint: str | None = None

    @property
    def executable(self) -> bool:
        return self.status == SUPPORTED and self.expression is not None

    @property
    def failures(self) -> tuple[LoweringReceipt, ...]:
        return tuple(receipt for receipt in self.receipts if receipt.status == FAILED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "executable": self.executable,
            "expression": self.expression.to_dict() if self.expression is not None else None,
            "expression_fingerprint": self.expression_fingerprint,
            "receipts": [receipt.to_dict() for receipt in self.receipts],
        }


@dataclass(frozen=True)
class _TemporalBinding:
    window: event_ir.TimeWindow | None = None
    negate: bool = False


def expression_fingerprint(expression: event_ir.Condition) -> str:
    """Stable semantic hash.  Evidence/provenance never changes the digest."""

    payload = semantic_fields.strip_provenance(expression.to_dict())
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SemanticPlanEventLowerer:
    """Lower normalized semantic nodes using only a resolved catalog."""

    def __init__(self, catalog: ResolvedSemanticCatalog) -> None:
        self.catalog = catalog
        self._receipts: list[LoweringReceipt] = []
        self._invalid_nodes: dict[str, str] = {}

    def lower(self, plan: semantic_plan.SemanticPlanV2) -> LoweringResult:
        self._receipts = []
        self._invalid_nodes = {
            str(item.get("node_id")): str(item.get("reason") or "semantic validation failed")
            for item in plan.validation_errors
            if item.get("node_id")
        }
        if not plan.nodes:
            receipt = LoweringReceipt(
                node_id="plan",
                status=FAILED,
                failure_code=MISSING_ARGUMENT,
                message="semantic plan has no requirement nodes",
            )
            return LoweringResult(status=UNSUPPORTED, expression=None, receipts=(receipt,))

        conditions: list[event_ir.Condition] = []
        for node in plan.nodes:
            condition = self._lower_node(node)
            if condition is not None:
                conditions.append(condition)

        failures = [receipt for receipt in self._receipts if receipt.status == FAILED]
        successes = [receipt for receipt in self._receipts if receipt.status == LOWERED]
        if failures:
            status = PARTIALLY_SUPPORTED if successes else UNSUPPORTED
            # Never expose a partial expression as executable output.
            return LoweringResult(status=status, expression=None, receipts=tuple(self._receipts))

        try:
            expression = event_ir.conjunction(conditions)
        except event_ir.IrSchemaError as exc:
            receipt = LoweringReceipt(
                node_id="plan",
                status=FAILED,
                failure_code=MISSING_ARGUMENT,
                message=str(exc),
            )
            return LoweringResult(
                status=UNSUPPORTED,
                expression=None,
                receipts=(*self._receipts, receipt),
            )
        fingerprint = expression_fingerprint(expression)
        return LoweringResult(
            status=SUPPORTED,
            expression=expression,
            receipts=tuple(self._receipts),
            expression_fingerprint=fingerprint,
        )

    def _lower_node(self, node: semantic_plan.SemanticNode) -> event_ir.Condition | None:
        if node.id in self._invalid_nodes:
            self._failure(node, VALUE_NOT_NORMALIZED, self._invalid_nodes[node.id])
            return None
        missing = node.missing_fields()
        if missing:
            self._failure(
                node,
                MISSING_ARGUMENT,
                "required semantic arguments are missing: " + ", ".join(missing),
            )
            return None
        try:
            if isinstance(node, semantic_plan.LogicalExpression):
                expression, symbols = self._logical(node)
            elif isinstance(node, semantic_plan.AggregatePredicate):
                expression, symbols = self._aggregate(node)
            elif isinstance(node, semantic_plan.Predicate):
                expression, symbols = self._predicate(node)
            else:
                raise LoweringError(
                    OPERATION_UNSUPPORTED,
                    f"semantic node type {node.type!r} has no Event IR algebra mapping",
                    symbol=node.type,
                )
        except CatalogError as exc:
            self._failure(
                node,
                CATALOG_SYMBOL_UNRESOLVED,
                str(exc),
                catalog_symbols=((exc.symbol,) if exc.symbol else ()),
            )
            return None
        except (event_ir.IrSchemaError, temporal_semantics.TemporalSemanticsError) as exc:
            self._failure(node, VALUE_NOT_NORMALIZED, str(exc))
            return None
        except LoweringError as exc:
            self._failure(
                node,
                exc.failure_code,
                str(exc),
                catalog_symbols=exc.catalog_symbols or ((exc.symbol,) if exc.symbol else ()),
            )
            return None

        self._receipts.append(
            LoweringReceipt(
                node_id=node.id,
                status=LOWERED,
                expression_fingerprint=expression_fingerprint(expression),
                catalog_symbols=tuple(dict.fromkeys(symbols)),
            )
        )
        return expression

    def _failure(
        self,
        node: semantic_plan.SemanticNode,
        code: str,
        message: str,
        *,
        catalog_symbols: tuple[str, ...] = (),
    ) -> None:
        self._receipts.append(
            LoweringReceipt(
                node_id=node.id,
                status=FAILED,
                failure_code=code,
                catalog_symbols=catalog_symbols,
                message=message,
            )
        )

    def _logical(
        self, node: semantic_plan.LogicalExpression
    ) -> tuple[event_ir.Condition, tuple[str, ...]]:
        operator = str(node.values.get("operator") or "")
        children = tuple(node.children())
        if operator == "not" and len(children) != 1:
            raise LoweringError(MISSING_ARGUMENT, "logical not requires exactly one child")
        if operator in {"and", "or"} and not children:
            raise LoweringError(MISSING_ARGUMENT, f"logical {operator} requires children")

        before = len(self._receipts)
        lowered = [self._lower_node(child) for child in children]
        if any(item is None for item in lowered):
            failed_children = [
                receipt.node_id
                for receipt in self._receipts[before:]
                if receipt.status == FAILED
            ]
            raise LoweringError(
                CHILD_LOWERING_FAILED,
                "logical child lowering failed: " + ", ".join(failed_children),
            )
        operands = [item for item in lowered if item is not None]
        if operator == "and":
            return event_ir.conjunction(operands), ()
        if operator == "or":
            return event_ir.disjunction(operands), ()
        if operator == "not":
            return event_ir.Not(operand=operands[0]), ()
        raise LoweringError(VALUE_NOT_NORMALIZED, f"unknown logical operator: {operator!r}")

    def _aggregate(
        self, node: semantic_plan.AggregatePredicate
    ) -> tuple[event_ir.Condition, tuple[str, ...]]:
        metric = self.catalog.metric(str(node.values.get("metric") or ""))
        if metric.kind != "aggregate":
            raise LoweringError(
                CATALOG_CONTRACT_MISMATCH,
                f"metric {metric.id!r} is {metric.kind}, not aggregate",
                symbol=metric.id,
            )
        declared_function = node.values.get("aggregation")
        if declared_function not in (None, "") and str(declared_function).casefold() != metric.aggregate_function:
            raise LoweringError(
                CATALOG_CONTRACT_MISMATCH,
                f"node aggregation {declared_function!r} conflicts with metric {metric.aggregate_function!r}",
                catalog_symbols=(metric.id,),
            )
        declared_grain = node.values.get("grain")
        if declared_grain not in (None, "") and str(declared_grain) != metric.grain:
            raise LoweringError(
                CATALOG_CONTRACT_MISMATCH,
                f"node grain {declared_grain!r} conflicts with metric grain {metric.grain!r}",
                catalog_symbols=(metric.id, metric.grain),
            )

        operator = self._operator(metric, node.values.get("operator"))
        value = self._literal(node.values.get("value"), metric.value_type)
        temporal = self._temporal(node, metric)
        relation, symbols = self._relation(metric, temporal.window)
        aggregate = event_ir.Aggregate(
            function=str(metric.aggregate_function),
            relation=relation,
            expression=(event_ir.FieldRef(metric.expression_field) if metric.expression_field else None),
            distinct=metric.distinct,
        )
        expression: event_ir.Condition = event_ir.Comparison(
            operator=operator,
            left=aggregate,
            right=event_ir.Literal(value),
            evidence=self._evidence(node),
        )
        if temporal.negate:
            expression = event_ir.Not(expression)
        return expression, (metric.id, metric.source, *symbols, operator)

    def _predicate(
        self, node: semantic_plan.Predicate
    ) -> tuple[event_ir.Condition, tuple[str, ...]]:
        metric = self.catalog.metric(str(node.values.get("metric") or ""))
        operator = self._operator(metric, node.values.get("operator"))
        temporal = self._temporal(node, metric)
        negated = self._bool_flag(node.values.get("negated"), "negated") ^ temporal.negate

        if metric.kind == "existence":
            wanted = self._existence_value(node.values.get("value"), operator)
            relation, symbols = self._relation(metric, temporal.window)
            expression: event_ir.Condition = event_ir.Exists(
                relation=relation,
                evidence=self._evidence(node),
            )
            if not wanted:
                expression = event_ir.Not(expression)
            if negated:
                expression = event_ir.Not(expression)
            return expression, (metric.id, metric.source, *symbols, operator)

        if metric.kind != "field":
            raise LoweringError(
                CATALOG_CONTRACT_MISMATCH,
                f"Predicate requires a field or existence metric, got {metric.kind!r}",
                symbol=metric.id,
            )
        value = self._literal(node.values.get("value"), metric.value_type)
        comparison = event_ir.Comparison(
            operator=operator,
            left=event_ir.FieldRef(str(metric.expression_field)),
            right=event_ir.Literal(value),
            evidence=self._evidence(node),
        )

        if metric.source == self.catalog.subject.name:
            operands: list[event_ir.Condition] = [comparison]
            symbols: tuple[str, ...] = (str(metric.expression_field),)
            if temporal.window is not None:
                if not metric.time:
                    raise LoweringError(
                        OPERATION_UNSUPPORTED,
                        f"metric {metric.id!r} has no time binding",
                        catalog_symbols=(metric.id,),
                    )
                time = self.catalog.time(metric.time)
                operands.insert(
                    0,
                    event_ir.TimeFilter(field=event_ir.FieldRef(time.field), window=temporal.window),
                )
                symbols = (str(metric.expression_field), time.id, time.field)
            expression = event_ir.conjunction(operands)
        else:
            relation, relation_symbols = self._relation(metric, temporal.window, predicate=comparison)
            expression = event_ir.Exists(relation=relation, evidence=self._evidence(node))
            symbols = (str(metric.expression_field), *relation_symbols)
        if negated:
            expression = event_ir.Not(expression)
        return expression, (metric.id, metric.source, *symbols, operator)

    def _operator(self, metric: MetricSpec, raw: Any) -> str:
        if not isinstance(raw, str) or not raw:
            raise LoweringError(MISSING_ARGUMENT, "comparison operator is missing")
        resolved = self.catalog.resolve_operator(raw)
        allowed = {self.catalog.resolve_operator(item).symbol for item in metric.allowed_operators}
        if resolved.symbol not in allowed:
            raise LoweringError(
                OPERATION_UNSUPPORTED,
                f"operator {resolved.symbol!r} is not allowed for metric {metric.id!r}",
                catalog_symbols=(metric.id, resolved.id),
            )
        if resolved.value_types and metric.value_type not in resolved.value_types:
            raise LoweringError(
                OPERATION_UNSUPPORTED,
                f"operator {resolved.id!r} does not accept value type {metric.value_type!r}",
                catalog_symbols=(metric.id, resolved.id),
            )
        return resolved.symbol

    def _relation(
        self,
        metric: MetricSpec,
        window: event_ir.TimeWindow | None,
        *,
        predicate: event_ir.Condition | None = None,
    ) -> tuple[event_ir.Relation, tuple[str, ...]]:
        if metric.source == self.catalog.subject.name:
            raise LoweringError(
                OPERATION_UNSUPPORTED,
                "subject-column metrics do not form an event relation",
                catalog_symbols=(metric.id, metric.source),
            )
        relation: event_ir.Relation = event_ir.Source(metric.source)
        available_sources = {metric.source}
        symbols: list[str] = [metric.source]
        for join_id in metric.joins:
            join = self.catalog.join(join_id)
            if join.left_source not in available_sources or join.right_source in available_sources:
                raise LoweringError(
                    CATALOG_CONTRACT_MISMATCH,
                    f"join order for {join.id!r} is disconnected or cyclic",
                    catalog_symbols=(metric.id, join.id),
                )
            relation = event_ir.Join(
                left=relation,
                right=event_ir.Source(join.right_source),
                on=event_ir.Comparison(
                    operator=self.catalog.resolve_operator(join.operator).symbol,
                    left=event_ir.FieldRef(join.left_field),
                    right=event_ir.FieldRef(join.right_field),
                ),
            )
            available_sources.add(join.right_source)
            symbols.extend((join.id, join.left_field, join.right_field))

        filters: list[event_ir.Condition] = []
        if metric.where is not None:
            filters.append(metric.where)
        if window is not None:
            if not metric.time:
                raise LoweringError(
                    OPERATION_UNSUPPORTED,
                    f"metric {metric.id!r} has no time binding",
                    catalog_symbols=(metric.id,),
                )
            time = self.catalog.time(metric.time)
            if getattr(window, "type", "") not in time.window_types:
                raise LoweringError(
                    OPERATION_UNSUPPORTED,
                    f"time binding {time.id!r} does not allow {getattr(window, 'type', '')!r}",
                    catalog_symbols=(metric.id, time.id),
                )
            self._check_coverage(metric, time.coverage, window)
            filters.append(event_ir.TimeFilter(field=event_ir.FieldRef(time.field), window=window))
            symbols.extend((time.id, time.field))
        if predicate is not None:
            filters.append(predicate)
        if filters:
            relation = event_ir.Filter(relation=relation, where=event_ir.conjunction(filters))

        grain = self.catalog.grain(metric.grain)
        if grain.keys:
            relation = event_ir.Group(
                relation=relation,
                keys=tuple(event_ir.FieldRef(key) for key in grain.keys),
            )
            symbols.extend((grain.id, *grain.keys))
        else:
            symbols.append(grain.id)
        return relation, tuple(symbols)

    def _temporal(
        self, node: semantic_plan.SemanticNode, metric: MetricSpec
    ) -> _TemporalBinding:
        raw_temporal = node.values.get("temporal")
        period = node.values.get("period")
        qualifier: temporal_semantics.TemporalQualifier | None = None
        if raw_temporal not in (None, "", {}, []):
            clean = raw_temporal
            declared_missing: tuple[str, ...] = ()
            if isinstance(raw_temporal, Mapping):
                declared_missing = tuple(str(item) for item in raw_temporal.get("missing_arguments", ()))
                clean = {
                    key: value
                    for key, value in raw_temporal.items()
                    if key != "missing_arguments"
                }
            qualifier = temporal_semantics.normalize(clean)
            if qualifier is None:
                raise LoweringError(VALUE_NOT_NORMALIZED, f"unknown temporal qualifier: {raw_temporal!r}")
            missing = tuple(sorted(set(declared_missing) | set(qualifier.missing_arguments())))
            # A node-level period supplies the interval argument without copying
            # it into the temporal object.
            if period not in (None, "", {}, []) and temporal_semantics.ARG_INTERVAL in missing:
                missing = tuple(item for item in missing if item != temporal_semantics.ARG_INTERVAL)
            if missing:
                raise LoweringError(
                    MISSING_ARGUMENT,
                    f"temporal operator {qualifier.operator} is missing: {', '.join(missing)}",
                    catalog_symbols=((metric.time,) if metric.time else (metric.id,)),
                )
            if qualifier.surplus_arguments():
                raise LoweringError(
                    VALUE_NOT_NORMALIZED,
                    f"temporal operator {qualifier.operator} has surplus arguments: "
                    + ", ".join(qualifier.surplus_arguments()),
                )
            if (
                period not in (None, "", {}, [])
                and qualifier.interval not in (None, "", {}, [])
                and self._window(period).to_dict() != self._window(qualifier.interval).to_dict()
            ):
                raise LoweringError(
                    CATALOG_CONTRACT_MISMATCH,
                    "node period conflicts with temporal interval",
                    catalog_symbols=((metric.time,) if metric.time else (metric.id,)),
                )
            if period in (None, "", {}, []) and qualifier.interval not in (None, "", {}, []):
                period = qualifier.interval

        if metric.time:
            time = self.catalog.time(metric.time)
            if qualifier is not None and qualifier.operator not in time.temporal_operators:
                raise LoweringError(
                    OPERATION_UNSUPPORTED,
                    f"time binding {time.id!r} does not support {qualifier.operator!r}",
                    catalog_symbols=(metric.id, time.id),
                )
            if time.required and period in (None, "", {}, []):
                raise LoweringError(
                    MISSING_ARGUMENT,
                    f"metric {metric.id!r} requires a time window",
                    catalog_symbols=(metric.id, time.id),
                )
        elif period not in (None, "", {}, []) or qualifier is not None:
            raise LoweringError(
                OPERATION_UNSUPPORTED,
                f"metric {metric.id!r} has no time binding",
                catalog_symbols=(metric.id,),
            )

        window = self._window(period) if period not in (None, "", {}, []) else None
        negate = bool(qualifier and qualifier.operator == temporal_semantics.NEVER_IN_INTERVAL)
        return _TemporalBinding(window=window, negate=negate)

    @staticmethod
    def _window(raw: Any) -> event_ir.TimeWindow:
        if isinstance(raw, (event_ir.AbsoluteInterval, event_ir.RollingWindow, event_ir.RelativeWindow)):
            return raw
        if not isinstance(raw, Mapping):
            raise LoweringError(VALUE_NOT_NORMALIZED, f"time window is not normalized: {raw!r}")
        kind = str(raw.get("type") or "")
        if kind in {"interval", "rolling", "relative"}:
            return event_ir.window_from_dict(raw)
        calendar = event_ir.AbsoluteInterval.from_calendar_window(dict(raw))
        if calendar is not None:
            return calendar
        # SemanticPlan normalization keeps a recent window as {value, unit}
        # (without a type); at this boundary it has one unambiguous IR shape.
        if raw.get("value") is not None and raw.get("unit") is not None:
            unit = event_ir.canonical_unit(raw.get("unit"))
            if unit not in event_ir.WINDOW_UNITS:
                raise LoweringError(VALUE_NOT_NORMALIZED, f"unsupported rolling unit: {raw.get('unit')!r}")
            try:
                return event_ir.RollingWindow(value=int(raw["value"]), unit=str(unit))
            except (TypeError, ValueError) as exc:
                raise LoweringError(VALUE_NOT_NORMALIZED, f"invalid rolling window: {raw!r}") from exc
        raise LoweringError(VALUE_NOT_NORMALIZED, f"unknown normalized time window: {raw!r}")

    def _check_coverage(
        self, metric: MetricSpec, time_coverage: str, window: event_ir.TimeWindow
    ) -> None:
        coverage_id = metric.coverage
        if coverage_id == "unknown":
            coverage_id = time_coverage
        if coverage_id == "unknown":
            coverage_id = self.catalog.source(metric.source).coverage
        coverage = self.catalog.coverage(coverage_id)
        issue = self._coverage_issue(coverage, window)
        if issue:
            raise LoweringError(
                DATA_COVERAGE_GAP,
                issue,
                catalog_symbols=(metric.id, coverage.id),
            )

    @staticmethod
    def _coverage_issue(coverage: DataCoverageSpec, window: event_ir.TimeWindow) -> str:
        if isinstance(window, event_ir.AbsoluteInterval):
            if coverage.available_from and window.start < coverage.available_from:
                return (
                    f"window starts at {window.start.isoformat()}, before coverage "
                    f"{coverage.available_from.isoformat()}"
                )
            if coverage.complete_through and window.inclusive_end > coverage.complete_through:
                return (
                    f"window ends at {window.inclusive_end.isoformat()}, after complete coverage "
                    f"{coverage.complete_through.isoformat()}"
                )
        elif coverage.max_lookback_days is not None:
            days = window.days if isinstance(window, event_ir.RollingWindow) else (
                window.value * event_ir.UNIT_DAYS[window.unit]
            )
            if days > coverage.max_lookback_days:
                return f"window needs {days} days, coverage allows {coverage.max_lookback_days}"
        return ""

    @staticmethod
    def _literal(raw: Any, value_type: str) -> int | float | str | bool:
        if isinstance(raw, Mapping):
            populated = {key: value for key, value in raw.items() if value is not None}
            if "amount" in populated:
                raw = populated["amount"]
            elif "value" in populated:
                raw = populated["value"]
            else:
                raise LoweringError(VALUE_NOT_NORMALIZED, f"literal wrapper has no amount/value: {raw!r}")
        if not isinstance(raw, (int, float, str, bool)):
            raise LoweringError(VALUE_NOT_NORMALIZED, f"unsupported literal value: {raw!r}")
        numeric_types = {"number", "integer", "decimal", "money"}
        string_types = {"string", "date", "date_char8", "date_string"}
        if value_type in numeric_types and (isinstance(raw, bool) or not isinstance(raw, (int, float))):
            raise LoweringError(VALUE_NOT_NORMALIZED, f"metric expects {value_type}, got {raw!r}")
        if value_type in string_types and not isinstance(raw, str):
            raise LoweringError(VALUE_NOT_NORMALIZED, f"metric expects {value_type}, got {raw!r}")
        if value_type in {"boolean", "bool"} and not isinstance(raw, bool):
            raise LoweringError(VALUE_NOT_NORMALIZED, f"metric expects boolean, got {raw!r}")
        return raw

    @staticmethod
    def _existence_value(raw: Any, operator: str) -> bool:
        if not isinstance(raw, bool):
            raise LoweringError(VALUE_NOT_NORMALIZED, f"existence predicate needs boolean value, got {raw!r}")
        if operator == "=":
            return raw
        if operator == "!=":
            return not raw
        raise LoweringError(OPERATION_UNSUPPORTED, f"existence predicate does not support {operator!r}")

    @staticmethod
    def _bool_flag(raw: Any, label: str) -> bool:
        if raw in (None, ""):
            return False
        if not isinstance(raw, bool):
            raise LoweringError(VALUE_NOT_NORMALIZED, f"{label} must be boolean")
        return raw

    @staticmethod
    def _evidence(node: semantic_plan.SemanticNode) -> event_ir.Evidence | None:
        text = str(node.source_span or "")
        if not text:
            return None
        start = node.source_start if isinstance(node.source_start, int) else 0
        end = node.source_end if isinstance(node.source_end, int) else start + len(text)
        if end <= start:
            end = start + len(text)
        return event_ir.Evidence(text=text, start=start, end=end)


def lower_semantic_plan(
    plan: semantic_plan.SemanticPlanV2,
    catalog: ResolvedSemanticCatalog,
) -> LoweringResult:
    """Functional entry point for composition roots."""

    return SemanticPlanEventLowerer(catalog).lower(plan)


__all__ = [
    "CATALOG_CONTRACT_MISMATCH",
    "CATALOG_SYMBOL_UNRESOLVED",
    "CHILD_LOWERING_FAILED",
    "DATA_COVERAGE_GAP",
    "FAILED",
    "LOWERED",
    "LoweringReceipt",
    "LoweringResult",
    "MISSING_ARGUMENT",
    "OPERATION_UNSUPPORTED",
    "PARTIALLY_SUPPORTED",
    "SUPPORTED",
    "SemanticPlanEventLowerer",
    "UNSUPPORTED",
    "VALUE_NOT_NORMALIZED",
    "expression_fingerprint",
    "lower_semantic_plan",
]
