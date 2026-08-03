"""컴파일 계층의 값 타입 — 방언·물리 바인딩·컴파일 결과.

물리 스키마(테이블/컬럼)가 처음으로 등장하는 계층이다. 그 위 세 계층은 논리
entity/attribute 만 말한다 — 그래서 DB 가 바뀌어도 바뀌는 것은 여기 바인딩뿐이라는 것이
이 분리의 실질적 이득이다(이 저장소의 이식성 목표와 같은 방향).

값은 SQL 문자열에 넣지 않는다. :class:`CompiledSql` 은 언제나 (문장, 파라미터) 쌍이고,
문자열 인라인은 **기존 문자열 SQL 검증 파이프라인과의 호환**을 위해서만
:attr:`ParameterStyle.INLINE_LITERAL` 로 명시적으로 선택된다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import TypeAlias

from pydantic import Field, model_validator

from query_pipeline.base import StrictModel


class SqlDialect(str, Enum):
    """대상 방언. ``TSQL`` 은 이 저장소의 실CRM(MSSQL) 때문에 추가된 항목이다."""

    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    BIGQUERY = "bigquery"
    CLICKHOUSE = "clickhouse"
    TSQL = "tsql"


class ParameterStyle(str, Enum):
    """값을 문장에 어떻게 실을 것인가."""

    NAMED = "named"
    # 기존 파이프라인(sql_guard·커버리지 검증)은 완성된 SQL **문자열**을 검사한다.
    # 그 경로를 위해서만 리터럴 인라인을 허용하고, 기본값은 언제나 NAMED 다.
    INLINE_LITERAL = "inline_literal"


ParameterValue: TypeAlias = str | int | float | bool | date | datetime


class SqlParameter(StrictModel):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value: ParameterValue


class CompiledSql(StrictModel):
    """특정 방언에서 실행할 문장과 파라미터."""

    sql: str = Field(min_length=1)
    parameters: tuple[SqlParameter, ...] = ()
    dialect: SqlDialect

    @property
    def parameter_map(self) -> dict[str, ParameterValue]:
        return {parameter.name: parameter.value for parameter in self.parameters}


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


class SchemaBinding(StrictModel):
    """논리 entity 하나의 물리 바인딩.

    ``key``/``correlations`` 는 EXISTS 를 쓰는 조건에만 필요하다. 선언이 없으면 컴파일러는
    상관 서브쿼리를 **추측하지 않고** :class:`MissingBindingError` 로 멈춘다 — 조인 키를
    추측하면 조용히 다른 집합이 나온다.
    """

    entity: str = Field(min_length=1)
    table: str = Field(min_length=1)
    attributes: Mapping[str, str] = Field(default_factory=dict)
    # 속성 → 저장 타입(number/string/date/date_char8/date_char6). 롤링 창('최근 30일')은
    # 실행 시점 컷오프로 렌더되는데, 그 문법이 컬럼의 **저장 표기**마다 다르다(문자열
    # 'YYYYMMDD' 인가 진짜 날짜인가). 선언이 없으면 컴파일러는 추측하지 않고 멈춘다 —
    # 틀린 표기로 비교하면 오류가 아니라 **항상 0건**이 조용히 나온다.
    data_type: Mapping[str, str] = Field(default_factory=dict)
    alias: str | None = None
    key: str | None = None
    correlations: Mapping[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _safe_identifiers(self) -> SchemaBinding:
        # 바인딩은 카탈로그(설정)에서 오고 설정은 배포마다 다르다. 여기서 걸러 두지 않으면
        # 식별자가 SQL 문법으로 새는 경로가 열린다 — 값이 아니라 식별자라 파라미터로 묶을
        # 수 없으므로, 안전한 형태만 허용하는 것이 유일한 방어다.
        for label, value in (
            ("table", self.table),
            ("alias", self.alias),
            ("key", self.key),
        ):
            if value is not None and not _is_safe_identifier(value):
                raise ValueError(f"unsafe {label} identifier: {value!r}")
        for mapping in (self.attributes, self.correlations):
            for name, column in mapping.items():
                if not _is_safe_identifier(column):
                    raise ValueError(f"unsafe column identifier for {name!r}: {column!r}")
        return self

    def column(self, attribute: str) -> str | None:
        return self.attributes.get(attribute)

    def column_type(self, attribute: str) -> str | None:
        return self.data_type.get(attribute)


def _is_safe_identifier(value: str) -> bool:
    """``schema.table`` 처럼 점으로 이어진 식별자까지 허용한다."""
    return bool(value) and all(_IDENTIFIER_RE.match(part) for part in value.split("."))


class SchemaBindings(StrictModel):
    bindings: Mapping[str, SchemaBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keys_match(self) -> SchemaBindings:
        for entity, binding in self.bindings.items():
            if binding.entity != entity:
                raise ValueError(
                    f"binding key does not match entity: {entity!r} != {binding.entity!r}"
                )
        return self

    def require(self, entity: str) -> SchemaBinding:
        binding = self.bindings.get(entity)
        if binding is None:
            raise KeyError(entity)
        return binding


class SqlCompilationContext(StrictModel):
    """컴파일 한 번의 대상 환경.

    ``schema_bindings`` 는 손으로 적는 것이 아니라 **해석된 카탈로그에서 파생**한다
    (:func:`~query_pipeline.compiler.bindings.schema_bindings_from_compiler`). 두 벌을 적으면
    같은 사실(어느 논리 소스가 어느 테이블인가)의 소유자가 둘이 되고, 갈라진 순간
    "무엇이 실행됐는가"의 답도 둘이 된다.

    기본값이 비어 있는 이유는 다르다: 바인딩을 **아직 선언하지 않은** 호출자를 막지 않기
    위해서다. 비어 있으면 entity 선언 검증(:meth:`AudiencePredicateCompiler._verify_bindings`)이
    검사할 것이 없어 통과하고, 물리 렌더는 위임받은 엔진이 자기 카탈로그로 한다.
    """

    dialect: SqlDialect
    schema_bindings: SchemaBindings = SchemaBindings()
    parameter_style: ParameterStyle = ParameterStyle.NAMED


__all__ = [
    "CompiledSql",
    "ParameterStyle",
    "ParameterValue",
    "SchemaBinding",
    "SchemaBindings",
    "SqlCompilationContext",
    "SqlDialect",
    "SqlParameter",
]
