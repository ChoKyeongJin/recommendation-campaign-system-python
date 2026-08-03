"""PostgreSQL 대상 컴파일러 — 방언 고정 편의 래퍼.

문법 차이는 :data:`query_pipeline.compiler.sql_compiler._SYNTAX` 표가 소유하므로 여기에는
분기가 없다. 방언별 파일을 따로 두는 이유는 3단계(방언별 컴파일러 분리)에서 각 파일이
자기 방언의 예외(타입 캐스팅·시계 함수 등)를 갖게 될 자리를 미리 정해 두기 위해서다.
"""

from __future__ import annotations

from query_pipeline.compiler.models import (
    CompiledSql,
    ParameterStyle,
    SchemaBindings,
    SqlCompilationContext,
    SqlDialect,
)
from query_pipeline.compiler.sql_compiler import LogicalPlanSqlCompiler
from query_pipeline.planning.models import LogicalPlan


class PostgresqlSqlCompiler(LogicalPlanSqlCompiler):
    """계약상 방언이 PostgreSQL 인 컴파일러(다른 방언 컨텍스트는 거부한다)."""

    def compile(
        self, plan: LogicalPlan, context: SqlCompilationContext
    ) -> CompiledSql:
        if context.dialect is not SqlDialect.POSTGRESQL:
            raise ValueError(
                f"PostgreSQL 컴파일러에 다른 방언 컨텍스트가 왔습니다: {context.dialect.value}"
            )
        return super().compile(plan, context)


def postgresql_context(
    schema_bindings: SchemaBindings,
    *,
    parameter_style: ParameterStyle = ParameterStyle.NAMED,
) -> SqlCompilationContext:
    return SqlCompilationContext(
        dialect=SqlDialect.POSTGRESQL,
        schema_bindings=schema_bindings,
        parameter_style=parameter_style,
    )


__all__ = ["PostgresqlSqlCompiler", "postgresql_context"]
