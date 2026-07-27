"""집계·분석 조건의 DB 독립 AST, 검증기와 SQL 식 컴파일러.

이 모듈은 자연어 파서와 물리 테이블 매핑 사이의 계약이다.  문자열 SQL을
AST로 가장하지 않고 집계 대상/범위/필터/비교를 각각 보존하며, 지원 여부가
확인되지 않은 함수나 파생 지표는 SQL로 추측하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from sql_dialect import SqlDialect, get_dialect


COMPARISON_SQL = {"EQ": "=", "NE": "<>", "GT": ">", "GTE": ">=", "LT": "<", "LTE": "<="}
NUMERIC_TYPES = frozenset({"integer", "int", "bigint", "decimal", "numeric", "float", "double", "number"})


@dataclass(frozen=True)
class AggregationDefinition:
    name: str
    accepts: frozenset[str] = frozenset({"any"})
    field_required: bool = True
    allows_distinct: bool = False
    dialects: frozenset[str] | None = None


class AggregationRegistry:
    """화이트리스트 기반 함수 레지스트리. 배포 코드에서 정의를 추가할 수 있다."""

    def __init__(self, definitions: Iterable[AggregationDefinition] = ()) -> None:
        self._items: dict[str, AggregationDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: AggregationDefinition) -> None:
        name = definition.name.upper()
        if name in self._items:
            raise ValueError(f"duplicate aggregation: {name}")
        self._items[name] = AggregationDefinition(
            name, definition.accepts, definition.field_required,
            definition.allows_distinct, definition.dialects,
        )

    def get(self, name: str) -> AggregationDefinition | None:
        return self._items.get(name.upper())

    def supports(self, name: str, dialect: str) -> bool:
        definition = self.get(name)
        return bool(definition and (definition.dialects is None or dialect in definition.dialects))


DEFAULT_AGGREGATIONS = AggregationRegistry([
    AggregationDefinition("SUM", NUMERIC_TYPES),
    AggregationDefinition("COUNT", frozenset({"any"}), False, True),
    AggregationDefinition("COUNT_DISTINCT", frozenset({"any"}), True, True),
    AggregationDefinition("AVG", NUMERIC_TYPES),
    AggregationDefinition("MAX", frozenset({"any"})),
    AggregationDefinition("MIN", frozenset({"any"})),
    AggregationDefinition("LATEST", frozenset({"any"})),
    AggregationDefinition("EARLIEST", frozenset({"any"})),
    AggregationDefinition("MEDIAN", NUMERIC_TYPES, dialects=frozenset({"postgres", "mysql"})),
    AggregationDefinition("PERCENTILE_CONT", NUMERIC_TYPES, dialects=frozenset({"postgres", "tsql"})),
    AggregationDefinition("PERCENTILE_DISC", frozenset({"any"}), dialects=frozenset({"postgres", "tsql"})),
    AggregationDefinition("STDDEV", NUMERIC_TYPES, dialects=frozenset({"postgres", "mysql"})),
    AggregationDefinition("VARIANCE", NUMERIC_TYPES, dialects=frozenset({"postgres", "mysql"})),
    AggregationDefinition("MODE", frozenset({"any"}), dialects=frozenset({"postgres"})),
    AggregationDefinition("FIRST_VALUE", frozenset({"any"})),
    AggregationDefinition("LAST_VALUE", frozenset({"any"})),
])


@dataclass(frozen=True)
class SourceSpan:
    text: str
    start: int = 0
    end: int = 0


@dataclass(frozen=True)
class Period:
    from_value: Any | None = None
    to_exclusive: Any | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class Scope:
    entity: str
    event: str
    group_by: tuple[str, ...]


@dataclass(frozen=True)
class AttributeCondition:
    field: str
    operator: str
    value: Any


@dataclass(frozen=True)
class Aggregate:
    function: str
    event: str
    field: str | None = None
    distinct: bool = False
    field_type: str = "any"
    event_filter: AttributeCondition | None = None
    period: Period | None = None
    order_by: str | None = None
    percentile: float | None = None


@dataclass(frozen=True)
class AggregateCondition:
    scope: Scope
    aggregation: Aggregate
    operator: str
    value: Any
    source: SourceSpan | None = None
    type: str = field(default="AGGREGATE_CONDITION", init=False)


@dataclass(frozen=True)
class AggregateExpression:
    type: str
    left: "AggregateExpression | Aggregate | None" = None
    right: "AggregateExpression | Aggregate | None" = None
    value: Any = None
    zero_division: str | None = None


@dataclass(frozen=True)
class ComputedAggregateCondition:
    expression: AggregateExpression
    operator: str
    value: Any
    type: str = field(default="COMPUTED_AGGREGATE_CONDITION", init=False)


@dataclass(frozen=True)
class OrderedValueCondition:
    event: str
    value_field: str
    order_field: str
    direction: str
    position: str
    operator: str
    value: Any
    type: str = field(default="ORDERED_VALUE_CONDITION", init=False)


@dataclass(frozen=True)
class RelativeMetricCondition:
    metric: Aggregate
    direction: str
    value: float
    tie_policy: str = "INCLUDE"
    type: str = "PERCENTILE_CONDITION"  # PERCENTILE_CONDITION | TOP_N_CONDITION | RANK_CONDITION


@dataclass(frozen=True)
class DerivedMetricDefinition:
    name: str
    expression: AggregateExpression


@dataclass(frozen=True)
class DerivedMetricCondition:
    metric: str
    operator: str
    value: Any
    type: str = field(default="DERIVED_METRIC_CONDITION", init=False)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    path: str = ""


@dataclass(frozen=True)
class CompilationResult:
    status: str
    sql: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    errors: tuple[ValidationIssue, ...] = ()


def _aggregates(node: AggregateExpression | Aggregate | None) -> Iterable[Aggregate]:
    if isinstance(node, Aggregate):
        yield node
    elif isinstance(node, AggregateExpression):
        yield from _aggregates(node.left)
        yield from _aggregates(node.right)


def validate_aggregate(aggregate: Aggregate, dialect: str, registry: AggregationRegistry = DEFAULT_AGGREGATIONS) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    definition = registry.get(aggregate.function)
    if definition is None or not registry.supports(aggregate.function, dialect):
        return [ValidationIssue("unsupported_aggregation", f"{aggregate.function} is not supported for {dialect}", "aggregation.function")]
    if definition.field_required and not aggregate.field:
        issues.append(ValidationIssue("field_required", f"{definition.name} requires a field", "aggregation.field"))
    if aggregate.distinct and not definition.allows_distinct:
        issues.append(ValidationIssue("distinct_not_allowed", f"DISTINCT is not allowed for {definition.name}", "aggregation.distinct"))
    if "any" not in definition.accepts and aggregate.field_type.casefold() not in definition.accepts:
        issues.append(ValidationIssue("incompatible_field_type", f"{definition.name} cannot aggregate {aggregate.field_type}", "aggregation.field"))
    if definition.name in {"FIRST_VALUE", "LAST_VALUE"} and not aggregate.order_by:
        issues.append(ValidationIssue("order_by_required", f"{definition.name} requires an ordering field", "aggregation.order_by"))
    if definition.name.startswith("PERCENTILE_") and not (aggregate.percentile is not None and 0 <= aggregate.percentile <= 1):
        issues.append(ValidationIssue("invalid_percentile", "percentile must be between 0 and 1", "aggregation.percentile"))
    return issues


def validate_condition(condition: Any, dialect: str = "ansi", registry: AggregationRegistry = DEFAULT_AGGREGATIONS, derived_metrics: Mapping[str, DerivedMetricDefinition] | None = None) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if getattr(condition, "operator", None) not in COMPARISON_SQL:
        issues.append(ValidationIssue("invalid_operator", f"unknown comparison operator: {getattr(condition, 'operator', None)}", "operator"))
    if isinstance(condition, AggregateCondition):
        issues.extend(validate_aggregate(condition.aggregation, dialect, registry))
        if not condition.scope.group_by:
            issues.append(ValidationIssue("group_by_required", "aggregate scope requires group_by", "scope.group_by"))
        if condition.scope.event != condition.aggregation.event:
            issues.append(ValidationIssue("period_scope_mismatch", "scope event and aggregate event differ", "scope.event"))
    elif isinstance(condition, ComputedAggregateCondition):
        issues.extend(issue for agg in _aggregates(condition.expression) for issue in validate_aggregate(agg, dialect, registry))
        if _contains_divide_without_policy(condition.expression):
            issues.append(ValidationIssue("zero_division_policy_required", "DIVIDE requires zero_division policy", "expression"))
    elif isinstance(condition, DerivedMetricCondition) and condition.metric not in (derived_metrics or {}):
        issues.append(ValidationIssue("unsupported_metric", f"derived metric is not registered: {condition.metric}", "metric"))
    elif isinstance(condition, RelativeMetricCondition):
        issues.extend(validate_aggregate(condition.metric, dialect, registry))
        if condition.direction not in {"TOP", "BOTTOM"}:
            issues.append(ValidationIssue("invalid_direction", "direction must be TOP or BOTTOM", "direction"))
    return issues


def _contains_divide_without_policy(expr: AggregateExpression | Aggregate | None) -> bool:
    return isinstance(expr, AggregateExpression) and (
        (expr.type == "DIVIDE" and expr.zero_division not in {"NULL", "ZERO"})
        or _contains_divide_without_policy(expr.left)
        or _contains_divide_without_policy(expr.right)
    )


class _Binder:
    def __init__(self) -> None:
        self.params: dict[str, Any] = {}

    def bind(self, value: Any, hint: str = "p") -> str:
        key = f"{hint}_{len(self.params)}"
        self.params[key] = value
        return f":{key}"


def _filter_sql(condition: AttributeCondition, binder: _Binder) -> str:
    return f"{condition.field} {COMPARISON_SQL[condition.operator]} {binder.bind(condition.value, 'filter')}"


def compile_aggregate(aggregate: Aggregate, dialect: SqlDialect, binder: _Binder) -> str:
    function = aggregate.function.upper()
    field = aggregate.field or "*"
    if function == "COUNT_DISTINCT":
        sql = f"COUNT(DISTINCT {field})"
    elif function in {"LATEST", "EARLIEST"}:
        # 날짜/시간 필드 자체의 최근·최초 시각. 다른 열의 동시점 값은 OrderedValueCondition을 쓴다.
        sql = f"{'MAX' if function == 'LATEST' else 'MIN'}({field})"
    elif function in {"FIRST_VALUE", "LAST_VALUE"}:
        sql = f"{function}({field}) OVER (ORDER BY {aggregate.order_by})"
    elif function in {"PERCENTILE_CONT", "PERCENTILE_DISC"}:
        sql = f"{function}({binder.bind(aggregate.percentile, 'percentile')}) WITHIN GROUP (ORDER BY {field})"
    elif function == "MEDIAN" and dialect.name == "postgres":
        sql = f"PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {field})"
    else:
        distinct = "DISTINCT " if aggregate.distinct else ""
        sql = f"{function}({distinct}{field})"
    if aggregate.event_filter:
        predicate = _filter_sql(aggregate.event_filter, binder)
        # T-SQL/MySQL에도 이식 가능한 CASE 형태. 행 사전필터가 가능한 빌더는 WHERE로 승격할 수 있다.
        target = "1" if field == "*" else field
        if function == "COUNT":
            sql = f"COUNT(CASE WHEN {predicate} THEN {target} END)"
        else:
            sql = f"{function}(CASE WHEN {predicate} THEN {field} END)"
    return sql


def compile_expression(expr: AggregateExpression | Aggregate, dialect: SqlDialect, binder: _Binder) -> str:
    if isinstance(expr, Aggregate):
        return compile_aggregate(expr, dialect, binder)
    kind = expr.type.upper()
    if kind == "AGGREGATE" and isinstance(expr.left, Aggregate):
        return compile_aggregate(expr.left, dialect, binder)
    if kind == "CONSTANT":
        return binder.bind(expr.value, "constant")
    if kind in {"ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"}:
        left = compile_expression(expr.left, dialect, binder)  # type: ignore[arg-type]
        right = compile_expression(expr.right, dialect, binder)  # type: ignore[arg-type]
        if kind == "DIVIDE":
            if expr.zero_division == "NULL":
                right = f"NULLIF({right}, 0)"
            elif expr.zero_division == "ZERO":
                return f"COALESCE(({left} / NULLIF({right}, 0)), 0)"
        symbol = {"ADD": "+", "SUBTRACT": "-", "MULTIPLY": "*", "DIVIDE": "/"}[kind]
        return f"({left} {symbol} {right})"
    if kind in {"COALESCE", "NULLIF"}:
        left = compile_expression(expr.left, dialect, binder)  # type: ignore[arg-type]
        right = compile_expression(expr.right, dialect, binder)  # type: ignore[arg-type]
        return f"{kind}({left}, {right})"
    raise ValueError(f"unsupported expression node: {kind}")


def compile_condition(condition: Any, dialect: str = "ansi", registry: AggregationRegistry = DEFAULT_AGGREGATIONS, derived_metrics: Mapping[str, DerivedMetricDefinition] | None = None) -> CompilationResult:
    errors = validate_condition(condition, dialect, registry, derived_metrics)
    if errors:
        status = "unsupported_aggregation" if any(e.code == "unsupported_aggregation" for e in errors) else "unsupported_metric" if any(e.code == "unsupported_metric" for e in errors) else "validation_error"
        return CompilationResult(status=status, errors=tuple(errors))
    binder, adapter = _Binder(), get_dialect(dialect)
    if isinstance(condition, AggregateCondition):
        lhs = compile_aggregate(condition.aggregation, adapter, binder)
    elif isinstance(condition, ComputedAggregateCondition):
        lhs = compile_expression(condition.expression, adapter, binder)
    elif isinstance(condition, DerivedMetricCondition):
        lhs = compile_expression((derived_metrics or {})[condition.metric].expression, adapter, binder)
    else:
        return CompilationResult("validation_error", errors=(ValidationIssue("requires_query_compiler", f"{condition.type} requires an analytic query compiler"),))
    rhs = binder.bind(condition.value, "threshold")
    return CompilationResult("ok", f"{lhs} {COMPARISON_SQL[condition.operator]} {rhs}", binder.params)
