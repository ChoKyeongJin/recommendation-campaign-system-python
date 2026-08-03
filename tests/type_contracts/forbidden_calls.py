"""**의도적으로 타입이 틀린 파일.** pytest 가 실행하지 않고 mypy 가 반려해야 한다.

리팩터링의 핵심 주장은 "검증되지 않은 요구를 실행 계층에 넘길 수 없다"이다. 그 주장은
런타임 검사가 아니라 **시그니처**로 지켜지므로, 증거도 타입 검사여야 한다.
:mod:`tests.test_query_pipeline_type_contracts` 가 이 파일에 mypy 를 돌려 아래 세 호출이
각각 반려되는지 확인한다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from query_pipeline.compiler.models import SqlCompilationContext, SqlDialect
from query_pipeline.compiler.sql_compiler import LogicalPlanSqlCompiler
from query_pipeline.planning.logical_planner import AudienceLogicalPlanner
from query_pipeline.requirement.models import (
    AudienceRequirement,
    IntentKind,
    ProposedExpression,
    RequirementIntent,
    RequirementSource,
)

requirement = AudienceRequirement(
    id="req-1",
    version="1",
    intent=RequirementIntent(kind=IntentKind.FIND),
    expression=ProposedExpression(payload={"type": "exists"}),
    source=RequirementSource(text="지난달 결제를 많이 실패한 고객을 보여줘"),
    created_at=datetime(2026, 8, 3, tzinfo=UTC),
)
sql_context = SqlCompilationContext(dialect=SqlDialect.POSTGRESQL)
sql_compiler = LogicalPlanSqlCompiler()
logical_planner = AudienceLogicalPlanner()


def forbidden_requirement_to_compiler() -> None:
    sql_compiler.compile(requirement, sql_context)


def forbidden_proposed_expression_to_compiler() -> None:
    sql_compiler.compile(requirement.expression, sql_context)


def forbidden_requirement_to_planner() -> None:
    logical_planner.create_plan(requirement)
