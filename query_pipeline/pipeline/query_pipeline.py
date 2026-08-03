"""QueryPipeline — 네 계층을 한 방향으로 잇는 조립 지점.

    사용자 문장 → AudienceRequirement → EventQuerySpec → LogicalPlan → CompiledSql

여기가 유일한 조립 지점인 이유: 각 계층이 서로를 직접 부르면 "요구가 곧 실행"인 지름길이
다시 생긴다. 파이프라인은 그 지름길을 만들지 않고, 미해결이면 **SQL 을 만들지 않은 채**
:class:`QueryPipelineNeedsResolution` 으로 끝난다.

관측: 모든 단계는 같은 ``correlation_id`` 를 달고 구조화 이벤트를 낸다. 원문·파라미터·
개인정보는 기본적으로 싣지 않는다(:attr:`QueryExecutionContext.include_payloads` 를 켠
호출자만 자기 정책 아래에서 그것을 본다).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Literal, TypeAlias

from pydantic import Field, JsonValue

from query_pipeline.base import StrictModel
from query_pipeline.compiler.base import SqlCompilationError, SqlCompiler
from query_pipeline.compiler.models import CompiledSql, SqlCompilationContext
from query_pipeline.event_query.models import EventQuerySpec
from query_pipeline.planning.logical_planner import LogicalPlanner
from query_pipeline.planning.models import LogicalPlan
from query_pipeline.requirement.issues import (
    IssueKind,
    IssueSeverity,
    RequirementIssue,
)
from query_pipeline.requirement.models import AudienceRequirement
from query_pipeline.requirement.parser import RequirementParser
from query_pipeline.requirement.resolver import (
    RequirementResolutionContext,
    RequirementResolver,
)

# ── 관측 ──────────────────────────────────────────────────────────────────────────

EVENT_REQUIREMENT_PARSED = "requirement.parsed"
EVENT_REQUIREMENT_INVALID = "requirement.invalid"
EVENT_REQUIREMENT_UNRESOLVED = "requirement.unresolved"
EVENT_REQUIREMENT_RESOLVED = "requirement.resolved"
EVENT_LOGICAL_PLAN_CREATED = "logical_plan.created"
EVENT_SQL_COMPILED = "sql.compiled"
EVENT_SQL_COMPILATION_FAILED = "sql.compilation_failed"

PIPELINE_EVENTS: tuple[str, ...] = (
    EVENT_REQUIREMENT_PARSED,
    EVENT_REQUIREMENT_INVALID,
    EVENT_REQUIREMENT_UNRESOLVED,
    EVENT_REQUIREMENT_RESOLVED,
    EVENT_LOGICAL_PLAN_CREATED,
    EVENT_SQL_COMPILED,
    EVENT_SQL_COMPILATION_FAILED,
)

EventSink = Callable[[str, Mapping[str, JsonValue]], None]


class QueryExecutionContext(StrictModel):
    """한 실행을 관통하는 상관 식별자."""

    correlation_id: str = Field(min_length=1)
    trace_id: str | None = None
    user_id: str | None = None
    # 원문/파라미터를 이벤트에 실을지. 기본은 싣지 않는다 — 로깅 정책은 호출자 소유다.
    include_payloads: bool = False


# ── 결과 ──────────────────────────────────────────────────────────────────────────


class QueryPipelineReady(StrictModel):
    status: Literal["ready"] = "ready"
    requirement: AudienceRequirement
    event_query_spec: EventQuerySpec
    logical_plan: LogicalPlan
    compiled_sql: CompiledSql


class QueryPipelineNeedsResolution(StrictModel):
    status: Literal["needs_resolution"] = "needs_resolution"
    requirement: AudienceRequirement
    issues: tuple[RequirementIssue, ...] = Field(min_length=1)
    # 어느 단계에서 멈췄는가. 사용자에게 "왜 SQL 이 없는가"를 답하는 좌표다.
    failed_stage: str = "requirement_resolution"


QueryPipelineResult: TypeAlias = Annotated[
    QueryPipelineReady | QueryPipelineNeedsResolution,
    Field(discriminator="status"),
]


class QueryPipeline:
    def __init__(
        self,
        parser: RequirementParser,
        resolver: RequirementResolver,
        logical_planner: LogicalPlanner,
        sql_compiler: SqlCompiler,
        resolution_context: RequirementResolutionContext,
        sql_context: SqlCompilationContext,
        *,
        on_event: EventSink | None = None,
    ) -> None:
        self._parser = parser
        self._resolver = resolver
        self._logical_planner = logical_planner
        self._sql_compiler = sql_compiler
        self._resolution_context = resolution_context
        self._sql_context = sql_context
        self._on_event = on_event

    def _emit(
        self,
        event: str,
        execution: QueryExecutionContext | None,
        payload: Mapping[str, JsonValue] | None = None,
    ) -> None:
        if self._on_event is None:
            return
        record: dict[str, JsonValue] = {"event": event}
        if execution is not None:
            record["correlation_id"] = execution.correlation_id
            if execution.trace_id:
                record["trace_id"] = execution.trace_id
        record.update(payload or {})
        self._on_event(event, record)

    async def execute(
        self,
        user_text: str,
        execution: QueryExecutionContext | None = None,
    ) -> QueryPipelineResult:
        requirement = await self._parser.parse(user_text)
        invalid = any(
            issue.kind is IssueKind.INVALID and issue.severity is IssueSeverity.ERROR
            for issue in requirement.issues
        )
        self._emit(
            EVENT_REQUIREMENT_INVALID if invalid else EVENT_REQUIREMENT_PARSED,
            execution,
            {"requirement_id": requirement.id, "issue_count": len(requirement.issues)},
        )

        resolution = await self._resolver.resolve(
            requirement=requirement, context=self._resolution_context
        )
        if resolution.status == "unresolved":
            issue_kinds: list[JsonValue] = [
                kind for kind in sorted({item.kind.value for item in resolution.issues})
            ]
            self._emit(
                EVENT_REQUIREMENT_UNRESOLVED,
                execution,
                {
                    "requirement_id": resolution.requirement.id,
                    "issue_kinds": issue_kinds,
                },
            )
            return QueryPipelineNeedsResolution(
                requirement=resolution.requirement,
                issues=resolution.issues,
            )

        spec = resolution.spec
        self._emit(
            EVENT_REQUIREMENT_RESOLVED,
            execution,
            {
                "requirement_id": requirement.id,
                "spec_id": spec.id,
                "receipt_count": len(spec.receipts),
                "assumption_count": len(spec.assumptions),
            },
        )

        logical_plan = self._logical_planner.create_plan(spec)
        self._emit(
            EVENT_LOGICAL_PLAN_CREATED,
            execution,
            {"spec_id": spec.id, "plan_kind": logical_plan.kind},
        )

        try:
            compiled_sql = self._sql_compiler.compile(
                plan=logical_plan, context=self._sql_context
            )
        except SqlCompilationError as exc:
            self._emit(
                EVENT_SQL_COMPILATION_FAILED,
                execution,
                {"spec_id": spec.id, "stage": exc.stage, "error": str(exc)},
            )
            return QueryPipelineNeedsResolution(
                requirement=requirement,
                issues=(
                    RequirementIssue(
                        id="sql-compilation-failed",
                        kind=IssueKind.UNSUPPORTED,
                        severity=IssueSeverity.ERROR,
                        path="$.expression",
                        message=str(exc),
                    ),
                ),
                failed_stage=exc.stage,
            )

        payload: dict[str, JsonValue] = {
            "spec_id": spec.id,
            "dialect": compiled_sql.dialect.value,
            "parameter_count": len(compiled_sql.parameters),
        }
        if execution is not None and execution.include_payloads:
            payload["sql"] = compiled_sql.sql
        self._emit(EVENT_SQL_COMPILED, execution, payload)

        return QueryPipelineReady(
            requirement=requirement,
            event_query_spec=spec,
            logical_plan=logical_plan,
            compiled_sql=compiled_sql,
        )


__all__ = [
    "EVENT_LOGICAL_PLAN_CREATED",
    "EVENT_REQUIREMENT_INVALID",
    "EVENT_REQUIREMENT_PARSED",
    "EVENT_REQUIREMENT_RESOLVED",
    "EVENT_REQUIREMENT_UNRESOLVED",
    "EVENT_SQL_COMPILATION_FAILED",
    "EVENT_SQL_COMPILED",
    "PIPELINE_EVENTS",
    "EventSink",
    "QueryExecutionContext",
    "QueryPipeline",
    "QueryPipelineNeedsResolution",
    "QueryPipelineReady",
    "QueryPipelineResult",
]
