"""실CRM 오디언스 컴파일러 — LogicalPlan 을 받아 **기존 사건 IR 엔진**에 위임한다.

새 계층을 세우면서 :mod:`event_compiler` 를 다시 쓰지 않는 이유는 그것이 리팩터링이
아니라 재작성이기 때문이다. 그 모듈에는 실CRM 물리 사실(반개구간 경계·char8 날짜 컬럼·
subject_column 바인딩·semi/anti 조인 전개·롤링 창의 실행시점 렌더)이 축적돼 있고, 그것을
새로 쓰는 순간 '같은 문장이 다른 오디언스를 뽑는' 위험이 생긴다.

그래서 경계만 옮긴다:

    입력   LogicalPlan            (SqlCompiler 프로토콜 — 요구도 IR dict 도 받지 않는다)
    위임   event_compiler          (물리 렌더링의 단일 소유자)
    출력   CompiledSql             (문장 + 파라미터 + 방언)

이 컴파일러의 산출물은 **회원 술어 한 조각**이다. 기존 빌더가 SELECT/FROM 을 만들고 이
술어를 AND 로 붙이는 계약을 유지하기 위해서다(이행 중 SQL 바이트 동일성 유지).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import event_compiler
import event_ir
from query_pipeline.compiler.base import (
    MissingBindingError,
    SqlCompilationError,
    UnsupportedPlanError,
)
from query_pipeline.compiler.models import (
    CompiledSql,
    ParameterStyle,
    SqlCompilationContext,
    SqlParameter,
)
from query_pipeline.event_query.event_ir_bridge import (
    ExpressionBridgeError,
    to_event_ir,
)
from query_pipeline.event_query.expressions import EventExpression, entities
from query_pipeline.planning.models import (
    LogicalFilter,
    LogicalPlan,
    LogicalScan,
    plan_stages,
)

CompileContextFactory = Callable[[], event_compiler.CompileContext]


class AudiencePredicateCompiler:
    """``Scan(subject) → Filter(predicate)`` 계획을 회원 술어 SQL 로 만든다."""

    def __init__(self, compile_context_factory: CompileContextFactory) -> None:
        self._compile_context_factory = compile_context_factory

    def compile(
        self, plan: LogicalPlan, context: SqlCompilationContext
    ) -> CompiledSql:
        predicate = self._predicate(plan)
        self._verify_bindings(predicate, context)
        try:
            condition = to_event_ir(predicate)
        except ExpressionBridgeError as exc:
            raise UnsupportedPlanError(str(exc)) from exc

        compile_context = dataclasses.replace(
            self._compile_context_factory(),
            literals=context.parameter_style is ParameterStyle.INLINE_LITERAL,
        )
        try:
            compiled = event_compiler.compile_expression(
                condition, context=compile_context
            )
        except (event_compiler.SqlCompileError, event_ir.IrSchemaError) as exc:
            raise SqlCompilationError(str(exc)) from exc
        return CompiledSql(
            sql=compiled.sql,
            parameters=tuple(
                SqlParameter(name=name, value=value)
                for name, value in sorted(compiled.params.items())
            ),
            dialect=context.dialect,
        )

    def _predicate(self, plan: LogicalPlan) -> EventExpression:
        stages = plan_stages(plan)
        if len(stages) != 2 or not isinstance(stages[0], LogicalScan):
            raise UnsupportedPlanError(
                "오디언스 술어 컴파일러는 Scan → Filter 계획만 표현합니다"
            )
        filter_stage = stages[1]
        if not isinstance(filter_stage, LogicalFilter):
            raise UnsupportedPlanError(
                "오디언스 술어 컴파일러는 Scan → Filter 계획만 표현합니다"
            )
        return filter_stage.predicate

    def _verify_bindings(
        self, predicate: EventExpression, context: SqlCompilationContext
    ) -> None:
        """계획이 참조하는 논리 entity 가 전부 선언돼 있는지 먼저 확인한다.

        실제 렌더는 event_compiler 가 하지만, 바인딩 누락을 그쪽 예외 메시지로만 알면
        '어느 계층이 몰랐는가'가 사라진다 — 실패 단계를 여기서 명시한다.
        """
        declared = set(context.schema_bindings.bindings)
        if not declared:
            return
        missing = [entity for entity in entities(predicate) if entity not in declared]
        if missing:
            raise MissingBindingError(
                "선언되지 않은 논리 entity 입니다: " + ", ".join(sorted(missing))
            )


__all__ = ["AudiencePredicateCompiler", "CompileContextFactory"]
