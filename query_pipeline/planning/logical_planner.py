"""LogicalPlanner — :class:`EventQuerySpec` 하나를 실행 순서로 편다.

시그니처가 계약이다: ``create_plan(spec: EventQuerySpec) -> LogicalPlan``. 요구
(:class:`~query_pipeline.requirement.models.AudienceRequirement`)를 여기 넘기는 호출은
정적 타입 검사에서 실패한다 — 그것이 이 계층을 따로 만든 이유다.

WHERE/HAVING 분리 규칙은 여기 한 곳에만 있다: **표현이 집계를 포함하면 HAVING, 아니면
WHERE**. SQL 문법이 아니라 표현의 구조로 정하므로 방언이 늘어도 이 규칙은 그대로다.
"""

from __future__ import annotations

from typing import Protocol

from query_pipeline.event_query.expressions import (
    AggregateOperand,
    AndExpression,
    AttributeOperand,
    EntityRelation,
    EventExpression,
    EventRelation,
    SortDirection,
    walk,
)
from query_pipeline.event_query.models import EventQuerySpec, QueryOutput
from query_pipeline.planning.models import (
    LogicalAggregate,
    LogicalFilter,
    LogicalLimit,
    LogicalMetric,
    LogicalPlan,
    LogicalProject,
    LogicalProjectionField,
    LogicalScan,
    LogicalSort,
    LogicalSortKey,
)


class LogicalPlanner(Protocol):
    def create_plan(self, spec: EventQuerySpec) -> LogicalPlan: ...


def contains_aggregate(expression: EventExpression) -> bool:
    return any(isinstance(node, AggregateOperand) for node in walk(expression))


def relation_root_entity(relation: EventRelation) -> str | None:
    """관계 트리의 뿌리 entity(파생 관계를 벗겨 낸 것)."""
    current: object = relation
    while True:
        if isinstance(current, EntityRelation):
            return current.entity
        nested = getattr(current, "relation", None) or getattr(current, "left", None)
        if nested is None:
            return None
        current = nested


def aggregates_scanned_relation(
    expression: EventExpression, scan_entity: str
) -> bool:
    """이 절의 집계가 **스캔 대상 관계 자체**를 접는가.

    이 구분이 WHERE/HAVING 을 가른다. 오디언스 조건에서 흔한 ``COUNT(캠페인 발송) >= 3``
    은 회원 행을 접는 집계가 아니라 **회원마다 도는 상관 스칼라 서브쿼리**다 — 그것을
    HAVING 으로 보내면 회원 테이블을 GROUP BY 하는, 뜻이 전혀 다른 SQL 이 된다
    (2026-08-03 실측: 이 규칙 없이는 캠페인 반응 횟수 조건이 계획 단계에서 깨졌다).
    """
    for node in walk(expression):
        if isinstance(node, AggregateOperand) and (
            relation_root_entity(node.relation) == scan_entity
        ):
            return True
    return False


def split_predicates(
    expression: EventExpression, scan_entity: str
) -> tuple[tuple[EventExpression, ...], tuple[EventExpression, ...]]:
    """(행 술어, 집계 술어). AND 는 평평하게 펴서 절 단위로 가른다.

    평평하게 펴는 것이 중요하다 — 접힌 AND 하나에 행 술어와 집계 술어가 섞여 있으면 그
    절 전체가 HAVING 으로 가고, 행 필터가 집계 **후** 로 밀린다(다른 결과가 나온다).
    """
    row: list[EventExpression] = []
    aggregate: list[EventExpression] = []
    for clause in _flatten_and(expression):
        target = (
            aggregate if aggregates_scanned_relation(clause, scan_entity) else row
        )
        target.append(clause)
    return tuple(row), tuple(aggregate)


def _flatten_and(expression: EventExpression) -> tuple[EventExpression, ...]:
    if isinstance(expression, AndExpression):
        return tuple(
            clause
            for operand in expression.expressions
            for clause in _flatten_and(operand)
        )
    return (expression,)


def _conjunction(clauses: tuple[EventExpression, ...]) -> EventExpression | None:
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return AndExpression(expressions=clauses)


class AudienceLogicalPlanner:
    """사양 → 계획. 단계 순서는 Scan → Filter → Aggregate → Sort → Limit → Project 다."""

    def create_plan(self, spec: EventQuerySpec) -> LogicalPlan:
        output: QueryOutput = spec.output
        row_predicates, aggregate_predicates = split_predicates(
            spec.expression, output.entity
        )

        plan: LogicalPlan = LogicalScan(entity=output.entity)

        row_predicate = _conjunction(row_predicates)
        if row_predicate is not None:
            plan = LogicalFilter(input=plan, predicate=row_predicate)

        if output.is_aggregated or aggregate_predicates:
            metrics = tuple(
                LogicalMetric(
                    function=measure.function,
                    target=measure.target,
                    alias=measure.alias,
                    distinct=measure.distinct,
                )
                for measure in output.measures
            )
            if not metrics:
                raise ValueError(
                    "집계 술어가 있는데 출력 지표가 없습니다 — 무엇을 세는지가 사양에 없습니다"
                )
            plan = LogicalAggregate(
                input=plan,
                group_by=tuple(output.dimensions),
                metrics=metrics,
                having=_conjunction(aggregate_predicates),
            )

        if output.order_by:
            aliases = {measure.alias for measure in output.measures}
            keys = tuple(
                LogicalSortKey(
                    alias=sort.name if sort.name in aliases else None,
                    expression=(
                        None
                        if sort.name in aliases
                        else _dimension_by_name(output, sort.name)
                    ),
                    direction=SortDirection.DESC if sort.descending else SortDirection.ASC,
                )
                for sort in output.order_by
            )
            plan = LogicalSort(input=plan, keys=keys)

        if output.limit is not None:
            plan = LogicalLimit(input=plan, count=output.limit)

        fields = tuple(
            LogicalProjectionField(expression=dimension)
            for dimension in output.dimensions
        ) + tuple(
            LogicalProjectionField(alias=measure.alias) for measure in output.measures
        )
        if fields:
            plan = LogicalProject(input=plan, fields=fields)
        return plan


def _dimension_by_name(output: QueryOutput, name: str) -> AttributeOperand:
    for dimension in output.dimensions:
        if name in {dimension.attribute, dimension.logical_name}:
            return dimension
    raise ValueError(f"정렬 키를 출력에서 찾지 못했습니다: {name!r}")


__all__ = [
    "AudienceLogicalPlanner",
    "LogicalPlanner",
    "aggregates_scanned_relation",
    "contains_aggregate",
    "relation_root_entity",
    "split_predicates",
]
