"""LogicalPlan → 파라미터 바인딩 SQL. 방언 차이는 표 하나로 격리한다.

값은 **절대** 문장에 붙이지 않는다. 리터럴은 전부 이름 있는 파라미터가 되고, 식별자만
문장에 들어간다(그 식별자도 :class:`SchemaBinding` 이 안전한 형태만 통과시킨다).

지원하지 못하는 노드는 조용히 빼지 않는다 — :class:`UnsupportedPlanError` 로 멈춘다.
빼면 문법적으로 멀쩡하지만 **다른 집합을 뽑는** SQL 이 성공으로 나가고, 그것이 이
저장소가 반복해서 다친 형태다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from query_pipeline.compiler.base import (
    MissingBindingError,
    SqlCompilationError,
    UnsupportedPlanError,
)
from query_pipeline.compiler.models import (
    CompiledSql,
    ParameterStyle,
    ParameterValue,
    SchemaBinding,
    SqlCompilationContext,
    SqlDialect,
    SqlParameter,
)
from query_pipeline.event_query.expressions import (
    AbsoluteWindow,
    AggregateFunction,
    AggregateOperand,
    AndExpression,
    ArithmeticOperand,
    ArithmeticOperator,
    AttributeOperand,
    ComparisonExpression,
    ComparisonOperator,
    EntityRelation,
    EventExpression,
    EventOperand,
    EventRelation,
    ExistsExpression,
    FilteredRelation,
    LiteralOperand,
    NotExpression,
    OrExpression,
    RollingWindow,
    SortDirection,
    TimeWindowExpression,
    WindowUnit,
)
from query_pipeline.planning.models import (
    LogicalAggregate,
    LogicalFilter,
    LogicalLimit,
    LogicalPlan,
    LogicalProject,
    LogicalScan,
    LogicalSort,
    plan_stages,
)


@dataclass(frozen=True)
class DialectSyntax:
    """방언이 다른 지점만 모은 표(문법 분기를 코드에 흩지 않는다)."""

    quote_open: str
    quote_close: str
    supports_limit_clause: bool = True
    supports_top_clause: bool = False


_SYNTAX: dict[SqlDialect, DialectSyntax] = {
    SqlDialect.POSTGRESQL: DialectSyntax('"', '"'),
    SqlDialect.MYSQL: DialectSyntax("`", "`"),
    SqlDialect.BIGQUERY: DialectSyntax("`", "`"),
    SqlDialect.CLICKHOUSE: DialectSyntax("`", "`"),
    SqlDialect.TSQL: DialectSyntax(
        "[", "]", supports_limit_clause=False, supports_top_clause=True
    ),
}

_COMPARISON_SQL: dict[ComparisonOperator, str] = {
    ComparisonOperator.EQ: "=",
    ComparisonOperator.NEQ: "<>",
    ComparisonOperator.GT: ">",
    ComparisonOperator.GTE: ">=",
    ComparisonOperator.LT: "<",
    ComparisonOperator.LTE: "<=",
}

_ARITHMETIC_SQL: dict[ArithmeticOperator, str] = {
    ArithmeticOperator.ADD: "+",
    ArithmeticOperator.SUBTRACT: "-",
    ArithmeticOperator.MULTIPLY: "*",
    ArithmeticOperator.DIVIDE: "/",
}

_AGGREGATE_SQL: dict[AggregateFunction, str] = {
    AggregateFunction.COUNT: "COUNT",
    AggregateFunction.SUM: "SUM",
    AggregateFunction.AVG: "AVG",
    AggregateFunction.MIN: "MIN",
    AggregateFunction.MAX: "MAX",
}

# capability 선언이 **렌더 표에서 파생**되도록 공개한다. 두 곳에 손으로 적으면 "지원한다고
# 말했는데 렌더가 없다"(또는 그 반대)가 생기고, 그 어긋남은 실행 시점에야 드러난다.
SCALAR_COMPARISON_OPERATORS: frozenset[ComparisonOperator] = frozenset(_COMPARISON_SQL)
SUPPORTED_AGGREGATE_FUNCTIONS: frozenset[AggregateFunction] = frozenset(_AGGREGATE_SQL)
SET_COMPARISON_OPERATORS: frozenset[ComparisonOperator] = frozenset(
    {
        ComparisonOperator.IN,
        ComparisonOperator.BETWEEN,
        ComparisonOperator.CONTAINS,
    }
)
# :func:`_render_time_window` 가 표현하는 창의 종류. capability 선언이 이것을 읽는다 —
# 렌더가 늘었는데 선언이 그대로면 "표현할 수 있는데 못 한다고 답하는" 상태가 되고,
# 그 어긋남은 사용자에게 **없는 한계**로 나간다.
SUPPORTED_WINDOW_KINDS: frozenset[str] = frozenset({"interval", "rolling"})


@dataclass
class _Scope:
    """컴파일 한 번의 가변 상태 — 별칭 발급과 파라미터 대장."""

    context: SqlCompilationContext
    parameters: list[SqlParameter] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    _alias_counter: int = 0
    _used_names: set[str] = field(default_factory=set)

    @property
    def syntax(self) -> DialectSyntax:
        return _SYNTAX[self.context.dialect]

    def binding(self, entity: str) -> SchemaBinding:
        try:
            return self.context.schema_bindings.require(entity)
        except KeyError as exc:
            raise MissingBindingError(
                f"논리 entity 의 물리 바인딩이 없습니다: {entity}"
            ) from exc

    def next_alias(self, entity: str) -> str:
        self._alias_counter += 1
        binding = self.binding(entity)
        alias = binding.alias or f"t{self._alias_counter}"
        return alias

    def column(self, entity: str, attribute: str, alias: str) -> str:
        binding = self.binding(entity)
        column = binding.column(attribute)
        if column is None:
            raise MissingBindingError(
                f"논리 속성의 물리 컬럼이 없습니다: {entity}.{attribute}"
            )
        return f"{self.quote(alias)}.{self.quote(column)}"

    def quote(self, identifier: str) -> str:
        syntax = self.syntax
        return ".".join(
            f"{syntax.quote_open}{part}{syntax.quote_close}"
            for part in identifier.split(".")
        )

    def bind(self, base: str, value: ParameterValue) -> str:
        """값 하나를 파라미터로 묶고 문장에 넣을 조각을 돌려준다."""
        if self.context.parameter_style is ParameterStyle.INLINE_LITERAL:
            return _inline_literal(value)
        name = _safe_parameter_name(base)
        candidate = name
        suffix = 2
        while candidate in self._used_names:
            candidate = f"{name}_{suffix}"
            suffix += 1
        self._used_names.add(candidate)
        self.parameters.append(SqlParameter(name=candidate, value=value))
        return f":{candidate}"


def _safe_parameter_name(base: str) -> str:
    cleaned = "".join(character if character.isalnum() else "_" for character in base)
    cleaned = cleaned.strip("_") or "p"
    return cleaned if cleaned[0].isalpha() or cleaned[0] == "_" else f"p_{cleaned}"


def _inline_literal(value: ParameterValue) -> str:
    """INLINE_LITERAL 스타일 전용. 인용 구현은 저장소 단일 소유자를 그대로 쓴다."""
    import sql_dialect

    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return str(sql_dialect.quote_literal(value.isoformat(sep=" ")))
    if isinstance(value, date):
        return str(sql_dialect.quote_literal(value.isoformat()))
    return str(sql_dialect.quote_literal(value))


class LogicalPlanSqlCompiler:
    """방언 파라미터화된 기본 컴파일러. :class:`SqlCompiler` 프로토콜 구현."""

    def compile(
        self, plan: LogicalPlan, context: SqlCompilationContext
    ) -> CompiledSql:
        scope = _Scope(context=context)
        stages = plan_stages(plan)
        root = stages[0]
        if not isinstance(root, LogicalScan):
            raise UnsupportedPlanError("계획의 뿌리는 scan 이어야 합니다")

        alias = scope.next_alias(root.entity)
        scope.aliases[root.entity] = alias
        binding = scope.binding(root.entity)

        where: list[str] = []
        group_by: list[str] = []
        having: str | None = None
        select: list[str] = []
        order_by: list[str] = []
        limit: int | None = None
        measure_sql: dict[str, str] = {}

        for stage in stages[1:]:
            if isinstance(stage, LogicalFilter):
                where.append(_render_condition(stage.predicate, scope, alias))
            elif isinstance(stage, LogicalAggregate):
                group_by = [
                    _render_operand(operand, scope, alias) for operand in stage.group_by
                ]
                for metric in stage.metrics:
                    rendered = _render_aggregate(
                        metric.function,
                        metric.target,
                        metric.distinct,
                        scope,
                        alias,
                    )
                    measure_sql[metric.alias] = rendered
                if stage.having is not None:
                    having = _render_condition(stage.having, scope, alias)
            elif isinstance(stage, LogicalSort):
                for key in stage.keys:
                    direction = "DESC" if key.direction is SortDirection.DESC else "ASC"
                    if key.alias is not None:
                        expression = measure_sql.get(key.alias) or scope.quote(key.alias)
                    else:
                        assert key.expression is not None
                        expression = _render_operand(key.expression, scope, alias)
                    order_by.append(f"{expression} {direction}")
            elif isinstance(stage, LogicalLimit):
                limit = stage.count
            elif isinstance(stage, LogicalProject):
                for item in stage.fields:
                    if item.alias is not None and item.expression is None:
                        measure = measure_sql.get(item.alias)
                        if measure is None:
                            raise UnsupportedPlanError(
                                f"프로젝션 별칭을 계산할 지표가 없습니다: {item.alias}"
                            )
                        select.append(f"{measure} AS {scope.quote(item.alias)}")
                        continue
                    assert item.expression is not None
                    rendered = _render_operand(item.expression, scope, alias)
                    select.append(
                        f"{rendered} AS {scope.quote(item.alias)}"
                        if item.alias
                        else rendered
                    )
            else:  # pragma: no cover - discriminated union 은 위가 전부다
                raise UnsupportedPlanError(f"알 수 없는 계획 노드입니다: {stage!r}")

        syntax = scope.syntax
        top = ""
        if limit is not None and syntax.supports_top_clause:
            top = f"TOP ({limit}) "
        lines = [f"SELECT {top}" + (", ".join(select) if select else "*")]
        table = scope.quote(binding.table)
        lines.append(f"FROM {table} AS {scope.quote(alias)}")
        if where:
            lines.append("WHERE " + "\n  AND ".join(where))
        if group_by:
            lines.append("GROUP BY " + ", ".join(group_by))
        if having:
            lines.append("HAVING " + having)
        if order_by:
            lines.append("ORDER BY " + ", ".join(order_by))
        if limit is not None and syntax.supports_limit_clause:
            lines.append(f"LIMIT {limit}")

        return CompiledSql(
            sql="\n".join(lines),
            parameters=tuple(scope.parameters),
            dialect=context.dialect,
        )


# ── 표현 렌더링 ───────────────────────────────────────────────────────────────────


def _render_condition(
    expression: EventExpression, scope: _Scope, alias: str
) -> str:
    if isinstance(expression, AndExpression):
        return "(" + " AND ".join(
            _render_condition(item, scope, alias) for item in expression.expressions
        ) + ")"
    if isinstance(expression, OrExpression):
        return "(" + " OR ".join(
            _render_condition(item, scope, alias) for item in expression.expressions
        ) + ")"
    if isinstance(expression, NotExpression):
        return "NOT (" + _render_condition(expression.expression, scope, alias) + ")"
    if isinstance(expression, ComparisonExpression):
        return _render_comparison(expression, scope, alias)
    if isinstance(expression, TimeWindowExpression):
        return _render_time_window(expression, scope, alias)
    if isinstance(expression, ExistsExpression):
        return _render_exists(expression, scope, alias)
    raise UnsupportedPlanError(
        f"이 컴파일러가 표현하지 못하는 조건입니다: {getattr(expression, 'kind', expression)!r}"
    )


def _render_comparison(
    expression: ComparisonExpression, scope: _Scope, alias: str
) -> str:
    left = _render_operand(expression.left, scope, alias)
    base = _parameter_base(expression.left)
    operator = expression.operator
    if operator in _COMPARISON_SQL:
        right = _render_operand(expression.right, scope, alias, base=base)
        return f"{left} {_COMPARISON_SQL[operator]} {right}"
    if not isinstance(expression.right, LiteralOperand):
        raise UnsupportedPlanError(
            f"{operator.value} 의 오른쪽은 리터럴이어야 합니다"
        )
    value = expression.right.value
    if operator is ComparisonOperator.IN:
        values = value if isinstance(value, tuple) else (value,)
        if not values:
            raise UnsupportedPlanError("IN 목록이 비어 있습니다")
        placeholders = ", ".join(
            scope.bind(f"{base}_{index}", item) for index, item in enumerate(values, 1)
        )
        return f"{left} IN ({placeholders})"
    if operator is ComparisonOperator.BETWEEN:
        if not (isinstance(value, tuple) and len(value) == 2):
            raise UnsupportedPlanError("BETWEEN 값은 경계 2개여야 합니다")
        low = scope.bind(f"{base}_start", value[0])
        high = scope.bind(f"{base}_end", value[1])
        return f"{left} >= {low} AND {left} < {high}"
    if operator is ComparisonOperator.CONTAINS:
        if not isinstance(value, str):
            raise UnsupportedPlanError("CONTAINS 값은 문자열이어야 합니다")
        return f"{left} LIKE {scope.bind(base, f'%{value}%')}"
    raise UnsupportedPlanError(f"지원하지 않는 비교 연산자입니다: {operator.value}")


def _render_time_window(
    expression: TimeWindowExpression, scope: _Scope, alias: str
) -> str:
    window = expression.window
    column = _render_operand(expression.attribute, scope, alias)
    if isinstance(window, AbsoluteWindow):
        base = _parameter_base(expression.attribute)
        start = scope.bind(f"{base}_start", window.start)
        end = scope.bind(f"{base}_end", window.end_exclusive)
        return f"{column} >= {start} AND {column} < {end}"
    if isinstance(window, RollingWindow):
        # 롤링 경계는 **실행 시점 함수**로 렌더한다. 계획 시점 날짜로 접으면 '최근 30일'이
        # 그 날의 30일로 고정돼, 내일 같은 사양을 실행해도 어제의 창을 본다.
        cutoff = _rolling_cutoff(expression.attribute, window, scope)
        return f"{column} >= {cutoff}"
    # 'N단위 전'은 과거의 한 달력 칸이다. 그 확정에는 기준일이 필요하고 기준일의 소유자는
    # Resolver 다 — 여기까지 내려왔다는 것은 확정을 건너뛴 것이므로 근사하지 않고 멈춘다.
    raise UnsupportedPlanError(
        "상대 시간 창('N단위 전')은 사양 단계에서 절대 구간으로 확정되어야 합니다"
    )


# 저장 표기 → 실행 시점 컷오프 문법. 문법 자체는 저장소의 단일 소유자(:mod:`sql_dialect`)가
# 렌더하고 여기는 "어느 표기에 어느 렌더인가"만 고른다. 선언되지 않은 표기(월 스냅샷 컬럼 등)는
# 근사하지 않고 멈춘다 — 월 칸으로 '최근 30일'을 답하면 요청과 다른 구간이 조용히 나온다.
_ROLLING_CUTOFF_BY_DATA_TYPE: dict[str, Callable[[Any, int], str]] = {
    "date_char8": lambda dialect, days: str(dialect.char8_cutoff(days)),
    "date": lambda dialect, days: str(dialect.datetime_cutoff(days)),
}

# 방언 열거 → :mod:`sql_dialect` 어댑터 이름. 미선언 방언에 ANSI 를 폴백으로 주지 않는다 —
# 시계 문법은 엔진마다 다르고, 틀린 문법은 오류가 아니라 다른 구간으로 나온다.
_ROLLING_DIALECT_NAMES: dict[SqlDialect, str] = {
    SqlDialect.POSTGRESQL: "postgres",
    SqlDialect.MYSQL: "mysql",
    SqlDialect.TSQL: "tsql",
}


def _unit_days(unit: WindowUnit) -> int:
    """창 단위 → 일수. 환산표는 저장소 단일 소유자(:data:`event_ir.UNIT_DAYS`)가 갖는다."""
    import event_ir

    return int(event_ir.UNIT_DAYS[unit.value])


def _rolling_cutoff(
    attribute: AttributeOperand, window: RollingWindow, scope: _Scope
) -> str:
    binding = scope.binding(attribute.entity)
    data_type = binding.column_type(attribute.attribute)
    if data_type is None:
        raise MissingBindingError(
            f"논리 속성의 저장 타입 선언이 없습니다: {attribute.logical_name}"
            " — 롤링 창은 저장 표기를 알아야 실행 시점 경계를 렌더할 수 있습니다"
        )
    render = _ROLLING_CUTOFF_BY_DATA_TYPE.get(data_type)
    if render is None:
        raise UnsupportedPlanError(
            f"'{data_type}' 로 적재된 컬럼에는 롤링 창을 걸 수 없습니다:"
            f" {attribute.logical_name}"
        )
    name = _ROLLING_DIALECT_NAMES.get(scope.context.dialect)
    if name is None:
        raise UnsupportedPlanError(
            f"{scope.context.dialect.value} 방언의 실행 시점 컷오프 문법이 선언되지 않았습니다"
        )
    import sql_dialect

    return render(sql_dialect.get_dialect(name), _unit_days(window.unit) * window.value)


def _render_exists(
    expression: ExistsExpression, scope: _Scope, outer_alias: str
) -> str:
    relation = expression.relation
    where: list[str] = []
    root: EventRelation = relation
    if isinstance(relation, FilteredRelation):
        root = relation.relation
    if not isinstance(root, EntityRelation):
        raise UnsupportedPlanError(
            "이 컴파일러는 source/filter 관계의 EXISTS 만 표현합니다"
        )
    inner_alias = scope.next_alias(root.entity)
    binding = scope.binding(root.entity)
    outer_entity = next(
        (entity for entity, alias in scope.aliases.items() if alias == outer_alias),
        None,
    )
    if outer_entity is None:  # pragma: no cover - 스캔 별칭은 항상 등록된다
        raise SqlCompilationError("바깥 스코프 entity 를 찾지 못했습니다")
    correlation_column = binding.correlations.get(outer_entity)
    outer_binding = scope.binding(outer_entity)
    if correlation_column is None or outer_binding.key is None:
        raise MissingBindingError(
            f"{root.entity} 와 {outer_entity} 를 잇는 상관 컬럼 선언이 없습니다"
        )
    where.append(
        f"{scope.quote(inner_alias)}.{scope.quote(correlation_column)}"
        f" = {scope.quote(outer_alias)}.{scope.quote(outer_binding.key)}"
    )
    if isinstance(relation, FilteredRelation):
        previous = scope.aliases.get(root.entity)
        scope.aliases[root.entity] = inner_alias
        try:
            where.append(_render_condition(relation.where, scope, inner_alias))
        finally:
            if previous is None:
                scope.aliases.pop(root.entity, None)
            else:
                scope.aliases[root.entity] = previous
    table = scope.quote(binding.table)
    return (
        f"EXISTS (SELECT 1 FROM {table} AS {scope.quote(inner_alias)}"
        f" WHERE {' AND '.join(where)})"
    )


def _render_operand(
    operand: EventOperand, scope: _Scope, alias: str, *, base: str = "value"
) -> str:
    if isinstance(operand, AttributeOperand):
        entity_alias = scope.aliases.get(operand.entity, alias)
        return scope.column(operand.entity, operand.attribute, entity_alias)
    if isinstance(operand, LiteralOperand):
        value = operand.value
        if isinstance(value, tuple):
            raise UnsupportedPlanError(
                "집합 리터럴은 IN/BETWEEN 연산자에서만 쓸 수 있습니다"
            )
        return scope.bind(base, value)
    if isinstance(operand, ArithmeticOperand):
        left = _render_operand(operand.left, scope, alias, base=base)
        right = _render_operand(operand.right, scope, alias, base=base)
        return f"({left} {_ARITHMETIC_SQL[operand.operator]} {right})"
    if isinstance(operand, AggregateOperand):
        return _render_aggregate(
            operand.function, operand.expression, operand.distinct, scope, alias
        )
    raise UnsupportedPlanError(f"알 수 없는 피연산자입니다: {operand!r}")


def _render_aggregate(
    function: AggregateFunction,
    target: EventOperand | None,
    distinct: bool,
    scope: _Scope,
    alias: str,
) -> str:
    name = _AGGREGATE_SQL[function]
    if target is None:
        if function is not AggregateFunction.COUNT:  # pragma: no cover - 모델이 막는다
            raise UnsupportedPlanError(f"{name} 는 대상 표현이 필요합니다")
        return "COUNT(*)"
    inner = _render_operand(target, scope, alias)
    return f"{name}({'DISTINCT ' if distinct else ''}{inner})"


def _parameter_base(operand: EventOperand) -> str:
    if isinstance(operand, AttributeOperand):
        return operand.attribute
    if isinstance(operand, AggregateOperand):
        return f"{operand.function.value}_value"
    return "value"


__all__ = [
    "SCALAR_COMPARISON_OPERATORS",
    "SET_COMPARISON_OPERATORS",
    "SUPPORTED_AGGREGATE_FUNCTIONS",
    "DialectSyntax",
    "LogicalPlanSqlCompiler",
]
