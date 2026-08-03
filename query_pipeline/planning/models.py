"""LogicalPlan — 조회·필터·집계·정렬의 **순서**를 저장소와 무관하게 표현한다.

여기 없는 것들이 이 계층의 정의다: 테이블 이름, 컬럼 이름, SQL 문법, 방언, 파라미터
스타일. 그래서 같은 LogicalPlan 이 서로 다른 컴파일러(방언별)로 내려갈 수 있고, 반대로
"어느 단계에서 의미가 바뀌었는가"를 SQL 을 읽지 않고 판정할 수 있다.

노드는 입력을 하나씩 감싸는 단항 파이프라인이다(Scan → Filter → Aggregate → Sort →
Limit → Project). 집계 술어(HAVING)와 행 술어(WHERE)의 구분은 SQL 문법이 아니라 **표현이
집계를 포함하는가**로 결정되고, 그 판정은 :mod:`query_pipeline.planning.logical_planner`
한 곳에만 있다.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from query_pipeline.base import StrictModel
from query_pipeline.event_query.expressions import (
    AggregateFunction,
    EventExpression,
    EventOperand,
    SortDirection,
)


class LogicalScan(StrictModel):
    """논리 entity 하나에서 행을 읽는다."""

    kind: Literal["scan"] = "scan"
    entity: str = Field(min_length=1)


class LogicalFilter(StrictModel):
    kind: Literal["filter"] = "filter"
    input: LogicalPlan
    predicate: EventExpression


class LogicalMetric(StrictModel):
    function: AggregateFunction
    target: EventOperand | None = None
    alias: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    distinct: bool = False

    @model_validator(mode="after")
    def _target_required(self) -> LogicalMetric:
        if self.function is not AggregateFunction.COUNT and self.target is None:
            raise ValueError(f"metric '{self.alias}' needs a target expression")
        return self


class LogicalAggregate(StrictModel):
    kind: Literal["aggregate"] = "aggregate"
    input: LogicalPlan
    group_by: tuple[EventOperand, ...] = ()
    metrics: tuple[LogicalMetric, ...] = Field(min_length=1)
    having: EventExpression | None = None


class LogicalSortKey(StrictModel):
    """정렬 키. 집계 뒤에는 출력 별칭으로, 그 전에는 피연산자로 정렬한다."""

    expression: EventOperand | None = None
    alias: str | None = None
    direction: SortDirection = SortDirection.ASC

    @model_validator(mode="after")
    def _one_target(self) -> LogicalSortKey:
        if (self.expression is None) == (self.alias is None):
            raise ValueError("sort key needs exactly one of expression/alias")
        return self


class LogicalSort(StrictModel):
    kind: Literal["sort"] = "sort"
    input: LogicalPlan
    keys: tuple[LogicalSortKey, ...] = Field(min_length=1)


class LogicalLimit(StrictModel):
    kind: Literal["limit"] = "limit"
    input: LogicalPlan
    count: int = Field(ge=1)


class LogicalProjectionField(StrictModel):
    expression: EventOperand | None = None
    alias: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> LogicalProjectionField:
        if self.expression is None and self.alias is None:
            raise ValueError("projection field needs an expression or an alias")
        return self


class LogicalProject(StrictModel):
    kind: Literal["project"] = "project"
    input: LogicalPlan
    fields: tuple[LogicalProjectionField, ...] = Field(min_length=1)


LogicalPlan: TypeAlias = Annotated[
    LogicalScan | LogicalFilter | LogicalAggregate | LogicalSort | LogicalLimit | LogicalProject,
    Field(discriminator="kind"),
]


for _model in (
    LogicalFilter,
    LogicalAggregate,
    LogicalSort,
    LogicalLimit,
    LogicalProject,
):
    _model.model_rebuild()


def plan_stages(plan: LogicalPlan) -> tuple[LogicalPlan, ...]:
    """뿌리(scan)부터 바깥까지의 단계 목록. 순서 검증과 디버깅의 단일 구현."""
    stages: list[LogicalPlan] = []
    current: LogicalPlan = plan
    while True:
        stages.append(current)
        nested = getattr(current, "input", None)
        if nested is None:
            break
        current = nested
    return tuple(reversed(stages))


def scan_entity(plan: LogicalPlan) -> str:
    """이 계획이 읽는 entity(단항 파이프라인이므로 뿌리는 언제나 하나다)."""
    root = plan_stages(plan)[0]
    if not isinstance(root, LogicalScan):
        raise ValueError("logical plan must start from a scan")
    return root.entity


__all__ = [
    "LogicalAggregate",
    "LogicalFilter",
    "LogicalLimit",
    "LogicalMetric",
    "LogicalPlan",
    "LogicalProject",
    "LogicalProjectionField",
    "LogicalScan",
    "LogicalSort",
    "LogicalSortKey",
    "plan_stages",
    "scan_entity",
]
