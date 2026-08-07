"""자연어 → 요구 → 실행 사양 → 논리 계획 → SQL 의 계층 파이프라인.

    사용자 문장
      ↓ RequirementParser        requirement/
    AudienceRequirement          (결핍·모호·추론 허용)
      ↓ RequirementResolver
    EventQuerySpec               event_query/   (미해결 상태 없음)
      ↓ LogicalPlanner
    LogicalPlan                  planning/      (저장소 비의존)
      ↓ SqlCompiler
    CompiledSql                  compiler/      (문장 + 파라미터)

import 방향은 아래에서 위로만 흐른다(requirement → event_query → planning → compiler).
:mod:`tests.test_query_pipeline_layering` 가 이 방향을 전수 강제한다.

이 모듈은 **조립 지점**이다: 상위 계층이 하위 구현을 직접 고르지 않도록, 실CRM 오디언스
경로의 조립을 여기 한 곳에 둔다.
"""

from __future__ import annotations

from query_pipeline.base import (
    Clock,
    FixedClock,
    IdFactory,
    SequentialIdFactory,
    StrictModel,
    SystemClock,
    UuidIdFactory,
)
from query_pipeline.plan_payload import (
    EventExpressionPayloadAdapter,
    PayloadAdapterError,
)
from query_pipeline.compiler import (
    AudiencePredicateCompiler,
    CompileContextFactory,
    CompiledSql,
    MissingBindingError,
    ParameterStyle,
    SqlCompilationContext,
    SqlCompilationError,
    SqlCompiler,
    SqlDialect,
    UnsupportedPlanError,
    schema_bindings_from_compiler,
)
from query_pipeline.event_query import EventQuerySpec, EventQuerySpecError
from query_pipeline.pipeline import (
    QueryExecutionContext,
    QueryPipeline,
    QueryPipelineNeedsResolution,
    QueryPipelineReady,
    QueryPipelineResult,
)
from query_pipeline.planning import AudienceLogicalPlanner, LogicalPlan
from query_pipeline.requirement import (
    AudienceRequirement,
    DefaultRequirementResolver,
    ReadyResolution,
    RequirementResolutionContext,
    UnresolvedResolution,
)


class QueryPipelineError(Exception):
    """파이프라인이 **어느 단계에서** 멈췄는지를 이름으로 들고 있는 실패.

    기존 빌더는 실패를 문자열 사유로 남긴다. 단계를 값으로 들고 있으면 그 사유에
    "어디서"가 자동으로 붙는다 — 이 리팩터링이 노린 관측성 개선의 실질이다.
    """

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def compile_audience_predicate(
    event_expression_payload: object,
    *,
    compile_context_factory: CompileContextFactory,
    dialect: SqlDialect = SqlDialect.TSQL,
    parameter_style: ParameterStyle = ParameterStyle.INLINE_LITERAL,
    clock: Clock | None = None,
    id_factory: IdFactory | None = None,
    subject_entity: str = "subject",
) -> CompiledSql:
    """저장된 canonical 표현 → 회원 술어 SQL, **계층을 전부 통과해서**.

    :func:`graph_rag.build_event_expression_sql_candidate` 의 진입점이다. 예전에는 저장된 dict 를
    그 자리에서 파싱해 컴파일러에 바로 넘겼다 — 그 경로에는 "검증된 사양"이라는 단계가 아예
    없었다. 이제 같은 SQL 이 어댑터 → 사양 → 계획 → 컴파일러를 지나 나온다. 실패는 단계 이름을
    달고 :class:`QueryPipelineError` 로 나간다.
    """
    try:
        spec = EventExpressionPayloadAdapter.to_event_query_spec(
            event_expression_payload,
            clock=clock,
            id_factory=id_factory,
            subject_entity=subject_entity,
        )
    except PayloadAdapterError as exc:
        raise QueryPipelineError(exc.stage, str(exc)) from exc

    plan = AudienceLogicalPlanner().create_plan(spec)
    compiler = AudiencePredicateCompiler(compile_context_factory)
    # 바인딩을 **같은** 컨텍스트에서 파생한다. 손으로 적으면 이 자리가 물리 사실의 두 번째
    # 소유자가 되고, 그 순간 이 항목은 부채 상환이 아니라 부채 신설이 된다.
    compile_context = compile_context_factory()
    context = SqlCompilationContext(
        dialect=dialect,
        parameter_style=parameter_style,
        schema_bindings=schema_bindings_from_compiler(
            events=compile_context.registry,
            fields=compile_context.fields or {},
            subject=compile_context.subject,
        ),
    )
    try:
        return compiler.compile(plan, context)
    except SqlCompilationError as exc:
        raise QueryPipelineError(exc.stage, str(exc)) from exc


def audience_spec_from_plan_payload(
    event_expression_payload: object,
    *,
    clock: Clock | None = None,
    id_factory: IdFactory | None = None,
    subject_entity: str = "subject",
) -> EventQuerySpec:
    """저장된 payload → 검증된 사양(진단용)."""
    return EventExpressionPayloadAdapter.to_event_query_spec(
        event_expression_payload,
        clock=clock,
        id_factory=id_factory,
        subject_entity=subject_entity,
    )


__all__ = [
    "AudienceLogicalPlanner",
    "AudiencePredicateCompiler",
    "AudienceRequirement",
    "Clock",
    "CompileContextFactory",
    "CompiledSql",
    "DefaultRequirementResolver",
    "EventExpressionPayloadAdapter",
    "EventQuerySpec",
    "EventQuerySpecError",
    "FixedClock",
    "IdFactory",
    "LogicalPlan",
    "MissingBindingError",
    "ParameterStyle",
    "PayloadAdapterError",
    "QueryExecutionContext",
    "QueryPipeline",
    "QueryPipelineError",
    "QueryPipelineNeedsResolution",
    "QueryPipelineReady",
    "QueryPipelineResult",
    "ReadyResolution",
    "RequirementResolutionContext",
    "SequentialIdFactory",
    "SqlCompilationContext",
    "SqlCompilationError",
    "SqlCompiler",
    "SqlDialect",
    "StrictModel",
    "SystemClock",
    "UnresolvedResolution",
    "UnsupportedPlanError",
    "UuidIdFactory",
    "audience_spec_from_plan_payload",
    "compile_audience_predicate",
]
