"""SqlCompiler 계약 — 입력은 :class:`LogicalPlan` **하나뿐**이다.

이 시그니처가 이번 리팩터링의 마지막 잠금장치다. 컴파일러는 자연어도,
:class:`~query_pipeline.requirement.issues.RequirementIssue` 도, 결핍도 모른다. 그래서
"컴파일러가 검증과 기본값 처리까지 하던" 구조가 타입 수준에서 되돌아올 수 없다.
"""

from __future__ import annotations

from typing import Protocol

from query_pipeline.compiler.models import CompiledSql, SqlCompilationContext
from query_pipeline.planning.models import LogicalPlan


class SqlCompilationError(Exception):
    """계획은 유효하지만 이 방언/바인딩으로 문장을 만들 수 없다."""

    stage = "sql_compilation"


class UnsupportedPlanError(SqlCompilationError):
    """계획에 이 컴파일러가 표현하지 못하는 노드/연산자가 있다(축소 금지 — 멈춘다)."""


class MissingBindingError(SqlCompilationError):
    """논리 entity/attribute 의 물리 바인딩이 없다."""


class SqlCompiler(Protocol):
    def compile(
        self,
        plan: LogicalPlan,
        context: SqlCompilationContext,
    ) -> CompiledSql: ...


__all__ = [
    "MissingBindingError",
    "SqlCompilationError",
    "SqlCompiler",
    "UnsupportedPlanError",
]
