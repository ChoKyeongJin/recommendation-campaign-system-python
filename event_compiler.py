"""조건 IR → SQL. 자연어를 **모르는** 컴파일러 + 업무 심볼 레지스트리.

이 모듈은 :mod:`event_ir` 의 범용 노드만 읽는다. 한국어 표면어도, 구매 전용 분기도, 슬롯 이름도 여기
없다 — 있으면 업무 개념이 하나 늘 때마다 컴파일러가 같이 자란다. 업무 개념과 물리 스키마의 대응은
**레지스트리 둘**이 소유하고, 그 확장이 새 조건을 여는 유일한 방법이다:

    EVENT_REGISTRY  — 사건 심볼('purchase') → 테이블/조인키/시각컬럼
    FIELD_REGISTRY  — 필드 심볼('purchase.amount') → 컬럼/타입

새 업무 이벤트/속성이 등장하면 IR 타입을 추가하지 않고 이 표에 한 줄을 넣는다. 사건의 발생 시각
필드(``<event>.occurred_at``)는 EventSpec 에서 **자동 파생**되므로 따로 적지 않는다.

바인딩 종류가 둘인 이유는 실제 스키마가 그렇기 때문이다. 주문은 별도 팩트 테이블(EXISTS/NOT EXISTS)
이지만, 로그인·가입은 실CRM 에서 회원 테이블의 컬럼 하나다(``LAST_LOGIN_DATE``/``REG_DT``). 둘을 한
컴파일러가 다루지 못하면 '구매는 없고 로그인은 있는' 같은 한 문장이 다시 두 트랙으로 쪼개진다.

경계 규칙: 절대 구간은 **반개구간**(``>= start AND < end_exclusive``)이다. ``BETWEEN`` 은 날짜 컬럼이
timestamp 일 때 마지막 날을 잘라먹는다 — IR 이 정한 경계를 SQL 이 다시 흔들지 않는다.

파라미터: 기본은 이름 있는 바인드 파라미터(``:event_0_start``)다. 실행 파이프라인이 SQL **문자열**을
검증·가드하는 이 저장소의 기존 계약을 위해 리터럴 렌더도 같은 코드 경로로 지원한다
(:class:`CompileContext` 의 ``literals=True``) — 두 렌더러를 따로 두면 반드시 갈라진다.
"""

from __future__ import annotations

import sql_dialect

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import event_ir
import semantic_fields
from event_ir import (
    AbsoluteInterval,
    Aggregate,
    And,
    Arithmetic,
    Comparison,
    EventReference,
    Exists,
    FieldRef,
    Filter,
    Group,
    Join,
    Limit,
    Literal,
    Not,
    Order,
    Or,
    Project,
    RelativeWindow,
    RollingWindow,
    Source,
    Summarize,
    TemporalRelation,
    TimeFilter,
)
from sql_dialect import SqlDialect, get_dialect


class SqlCompileError(Exception):
    """IR 은 유효하지만 이 스키마/방언으로는 표현할 수 없다. 의미를 줄이지 말고 여기서 멈춘다."""


CAPABILITY_SUPPORTED = "supported"
CAPABILITY_UNSUPPORTED = "unsupported"
CAPABILITY_PARTIALLY_SUPPORTED = "partially_supported"


@dataclass(frozen=True)
class CompilerCapabilityIssue:
    code: str
    node_id: str
    symbol: str | None = None


@dataclass(frozen=True)
class CompilerCapabilityResult:
    status: str
    issues: tuple[CompilerCapabilityIssue, ...]
    supported_node_ids: tuple[str, ...]
    unsupported_node_ids: tuple[str, ...]


@dataclass
class CompiledCondition:
    sql: str
    params: dict[str, Any] = field(default_factory=dict)


# ── 레지스트리 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubjectSpec:
    """조건이 걸리는 주체(오디언스). 실CRM 은 회원 기준 테이블이다."""

    table: str = "CRM_MB_BASEINFO"
    alias: str = "B"
    key: str = "MEMBER_NO"
    name: str = "subject"


@dataclass(frozen=True)
class EventSpec:
    """사건 심볼 하나의 물리 바인딩.

    ``binding="fact_table"``    — 사건이 별도 테이블의 행이다(EXISTS/NOT EXISTS 상관 서브쿼리).
    ``binding="subject_column"`` — 사건의 발생 시각이 주체 테이블의 컬럼 하나다(직접 비교).

    ``time_format`` 은 시각 컬럼의 저장 관례다. 실CRM 은 ``nvarchar(8) 'YYYYMMDD'``(``char8``)이고,
    일반 날짜/시각 컬럼은 ``date``. 경계 렌더가 여기서 갈린다.
    """

    table: str
    alias: str
    subject_key: str
    event_subject_key: str
    time_column: str
    time_format: str = "char8"
    binding: str = "fact_table"
    # 사건 정의에 항상 붙는 추가 술어(예: 보관 중인 카트만). 별칭은 ``{alias}`` 로 적는다.
    extra_predicates: tuple[str, ...] = ()
    label: str = ""
    # 단순 ``table alias`` 로 끝나지 않는 물리 소스(검증된 조인을 포함한 relation)도
    # 같은 Source 노드로 컴파일한다. ``{alias}`` 는 이 사건 인스턴스의 별칭이다.
    # 비어 있으면 기존 ``table alias`` 바인딩을 그대로 사용한다.
    from_sql: str = ""
    # 회원키 타입 변환처럼 ``alias.column = subject.column`` 으로 표현할 수 없는 상관식.
    # {alias}, {subject_alias}, {subject_key}, {event_subject_key} 를 사용할 수 있다.
    correlation_sql: str = ""
    # 발생 시각이 검증된 조인 대상에 있거나 계산식인 경우의 논리 시각 필드 바인딩.
    time_expression: str = ""


@dataclass(frozen=True)
class FieldSpec:
    """필드 심볼 하나의 물리 바인딩. 새 업무 속성은 이 표에 한 줄이면 쓸 수 있다."""

    source: str
    column: str
    data_type: str = "number"  # number | string | date | date_char8
    # 계산 필드/조인 필드. ``{alias}`` 는 필드가 속한 Source 의 현재 별칭이다.
    # 비어 있으면 기존 ``alias.column`` 바인딩을 사용한다.
    expression: str = ""
    # 논리값(canonical) → 물리 저장값. 값 도메인이 선언된 필드는 알 수 없는
    # 문자열을 그대로 SQL에 흘리지 않고 fail-close 한다.
    value_map: tuple[tuple[str, Any], ...] = ()

    def physical_value(self, value: Any) -> Any:
        if not self.value_map:
            return value
        if not isinstance(value, str):
            raise SqlCompileError(
                f"값 도메인이 있는 필드 '{self.source}.{self.column}'에는 canonical 문자열이 필요합니다"
            )
        values = dict(self.value_map)
        if value not in values:
            raise SqlCompileError(
                f"필드 '{self.source}.{self.column}'에 등록되지 않은 canonical 값입니다: {value}"
            )
        return values[value]


# 기본 사건 레지스트리 — 실CRM(CRMDW) 바인딩. graph_rag 가 member_target_filters.json 의 값으로
# 오버라이드를 주입한다(:func:`resolve_registry`). 코드 상수는 설정 부재 시 폴백이다.
EVENT_REGISTRY: dict[str, EventSpec] = {
    "purchase": EventSpec(
        table="CRM_SL_ORDERHEADERMALL", alias="EO",
        subject_key="MEMBER_NO", event_subject_key="MEMBER_NO",
        time_column="ORDER_DATE", time_format="char8", binding="fact_table", label="구매",
    ),
    "login": EventSpec(
        # 실CRM 에는 로그인 이력 테이블이 없고 회원 기준 테이블의 마지막 접속일만 있다.
        table="CRM_MB_BASEINFO", alias="B",
        subject_key="MEMBER_NO", event_subject_key="MEMBER_NO",
        time_column="LAST_LOGIN_DATE", time_format="char8", binding="subject_column", label="로그인",
    ),
    "signup": EventSpec(
        table="CRM_MB_BASEINFO", alias="B",
        subject_key="MEMBER_NO", event_subject_key="MEMBER_NO",
        time_column="REG_DT", time_format="char8", binding="subject_column", label="가입",
    ),
    "cart": EventSpec(
        table="ODS_MALL_OMS_CART", alias="EC",
        subject_key="MEMBER_NO", event_subject_key="MEMBER_ID",
        time_column="UPD_DT", time_format="char8", binding="fact_table", label="장바구니 담기",
    ),
}

# 기본 필드 레지스트리. 발생 시각(``<event>.occurred_at``)은 EventSpec 에서 파생하므로 여기 없다.
FIELD_REGISTRY: dict[str, FieldSpec] = {
    "purchase.amount": FieldSpec(source="purchase", column="PAYMENT_AMT", data_type="number"),
    "purchase.order_id": FieldSpec(source="purchase", column="ORDER_ID", data_type="string"),
    "cart.quantity": FieldSpec(source="cart", column="QTY", data_type="number"),
    "cart.product_id": FieldSpec(source="cart", column="PRODUCT_ID", data_type="string"),
    "subject.grade": FieldSpec(source="subject", column="EMART_GRADE_CD", data_type="string"),
    "subject.age": FieldSpec(source="subject", column="AGE", data_type="number"),
}

_TIME_FORMAT_DATA_TYPE = {"char8": "date_char8", "date": "date"}


def resolve_registry(overrides: dict[str, EventSpec] | None = None) -> dict[str, EventSpec]:
    """기본 사건 레지스트리에 배포별 오버라이드를 얹는다(설정이 스키마의 단일 소스이도록)."""
    resolved = dict(EVENT_REGISTRY)
    if overrides:
        resolved.update(overrides)
    return resolved


def resolve_fields(
    registry: dict[str, EventSpec], overrides: dict[str, FieldSpec] | None = None
) -> dict[str, FieldSpec]:
    """필드 레지스트리 + **사건에서 파생된 발생 시각 필드**.

    ``<event>.occurred_at`` 을 손으로 적지 않는 이유: 이벤트를 등록해 놓고 시각 필드를 빠뜨리면
    그 사건에는 기간 조건을 걸 수 없는데, 그 사실이 조용히 드러나지 않는다."""
    fields = dict(FIELD_REGISTRY)
    for name, spec in registry.items():
        fields[f"{name}.{event_ir.TIME_FIELD_SUFFIX}"] = FieldSpec(
            source=name, column=spec.time_column,
            data_type=_TIME_FORMAT_DATA_TYPE.get(spec.time_format, "date"),
            expression=spec.time_expression,
        )
    if overrides:
        fields.update(overrides)
    return fields


@dataclass
class CompileContext:
    """컴파일 한 번의 환경. 주체·레지스트리·방언·파라미터 스타일·기준일을 한 곳에 모은다."""

    subject: SubjectSpec = field(default_factory=SubjectSpec)
    registry: dict[str, EventSpec] = field(default_factory=lambda: dict(EVENT_REGISTRY))
    fields: dict[str, FieldSpec] | None = None
    dialect: SqlDialect = field(default_factory=lambda: get_dialect("tsql"))
    # True 면 값을 SQL 리터럴로 인라인한다(문자열 SQL 을 검증하는 기존 파이프라인용).
    literals: bool = False
    # 상대 창('3개월 전')을 절대 구간으로 확정하는 기준일. 계획 시점에 날짜가 드러나야 감사 가능하다.
    today: date | None = None
    _counter: list[int] = field(default_factory=lambda: [0])
    # 컴파일 중인 관계 스코프: 소스 심볼 → 별칭. FieldRef 해석이 이걸 본다.
    _scope: dict[str, str] = field(default_factory=dict)
    # 파생 관계가 materialize 된 뒤 canonical field가 가리키는 SQL 출력.
    _field_bindings: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.fields is None:
            self.fields = resolve_fields(self.registry)

    def next_index(self) -> int:
        index = self._counter[0]
        self._counter[0] += 1
        return index

    def event_spec(self, name: str) -> EventSpec:
        spec = self.registry.get(name)
        if spec is None:
            raise SqlCompileError(f"등록되지 않은 사건입니다: {name}")
        return spec

    def field_spec(self, name: str) -> FieldSpec:
        spec = (self.fields or {}).get(name)
        if spec is None:
            raise SqlCompileError(f"등록되지 않은 필드입니다: {name}")
        return spec

    def with_scope(self, scope: dict[str, str]) -> "CompileContext":
        return CompileContext(
            subject=self.subject, registry=self.registry, fields=self.fields, dialect=self.dialect,
            literals=self.literals, today=self.today, _counter=self._counter,
            _scope={**self._scope, **scope},
            _field_bindings=self._field_bindings,
        )

    def with_field_bindings(self, bindings: dict[str, str]) -> "CompileContext":
        return CompileContext(
            subject=self.subject, registry=self.registry, fields=self.fields, dialect=self.dialect,
            literals=self.literals, today=self.today, _counter=self._counter,
            _scope=self._scope,
            _field_bindings={**self._field_bindings, **bindings},
        )


def _sql_quote(value: Any) -> str:
    """SQL 문자열 리터럴. 구현은 sql_dialect 가 단일 소유한다(미러 복제 금지)."""
    return sql_dialect.quote_literal(value)


# ── 시간 ──────────────────────────────────────────────────────────────────────────


def _render_date(value: date, data_type: str) -> str:
    return _sql_quote(value.strftime("%Y%m%d") if data_type == "date_char8" else value.isoformat())


def _param_value(value: date, data_type: str) -> Any:
    return value.strftime("%Y%m%d") if data_type == "date_char8" else value


def compile_time_window(
    column: str, window: event_ir.TimeWindow, param_prefix: str, *,
    data_type: str, context: CompileContext,
) -> CompiledCondition:
    """시간 창 하나 → 컬럼 비교 술어. 절대 구간은 반개구간, 롤링은 실행 시점 컷오프."""
    if isinstance(window, RelativeWindow):
        window = event_ir.resolve_relative_window(window, context.today)

    if isinstance(window, AbsoluteInterval):
        start_key, end_key = f"{param_prefix}_start", f"{param_prefix}_end"
        if context.literals:
            start_sql = _render_date(window.start, data_type)
            end_sql = _render_date(window.end_exclusive, data_type)
            params: dict[str, Any] = {}
        else:
            start_sql, end_sql = f":{start_key}", f":{end_key}"
            params = {
                start_key: _param_value(window.start, data_type),
                end_key: _param_value(window.end_exclusive, data_type),
            }
        return CompiledCondition(sql=f"{column} >= {start_sql} AND {column} < {end_sql}", params=params)

    if isinstance(window, RollingWindow):
        # 롤링 경계는 실행 시점 함수로 렌더한다 — 계획 시점 날짜로 굳히면 '최근 30일'이 고정된다.
        cutoff = (
            context.dialect.char8_cutoff(window.days)
            if data_type == "date_char8"
            else context.dialect.datetime_cutoff(window.days)
        )
        return CompiledCondition(sql=f"{column} >= {cutoff}")

    raise SqlCompileError(f"지원하지 않는 시간 창입니다: {window!r}")


# ── 관계 ──────────────────────────────────────────────────────────────────────────


@dataclass
class RelationPlan:
    """관계 하나를 서브쿼리 조각으로 편 것(FROM/WHERE/GROUP BY)."""

    from_sql: str
    where: list[str]
    group_by: list[str]
    scope: dict[str, str]  # 소스 심볼 → 별칭
    root_source: str
    binding: str
    params: dict[str, Any]
    projection: list[str] = field(default_factory=list)
    # Both an output's short name and its canonical input FieldRef can resolve
    # to the materialized column.  Measures only have the short name.
    output_aliases: dict[str, str] = field(default_factory=dict)
    output_expressions: dict[str, str] = field(default_factory=dict)
    order_by: list[str] = field(default_factory=list)
    limit: int | None = None
    field_bindings: dict[str, str] = field(default_factory=dict)


def _binding_tokens(
    spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> dict[str, str]:
    """Catalog SQL 조각에 허용하는 닫힌 치환 변수.

    업무 사건별 Python 분기를 만들지 않고 검증된 물리 선언을 재사용하기 위한 경계다.
    임의 query 값은 들어오지 않으며, 리터럴은 catalog 적재 시점에 확정된 문자열만 사용한다.
    """
    return {
        "alias": alias or spec.alias,
        "subject_alias": context.subject.alias,
        "subject_key": spec.subject_key,
        "event_subject_key": spec.event_subject_key,
    }


def _render_binding(
    template: str, spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> str:
    try:
        return template.format(**_binding_tokens(spec, context, alias=alias))
    except (KeyError, ValueError) as exc:
        raise SqlCompileError(f"사건 '{spec.label or spec.table}' 물리 바인딩 형식이 잘못되었습니다") from exc


def _source_sql(spec: EventSpec, context: CompileContext, *, alias: str | None = None) -> str:
    active_alias = alias or spec.alias
    if spec.from_sql:
        return _render_binding(spec.from_sql, spec, context, alias=active_alias)
    return f"{spec.table} {active_alias}"


def _correlation(
    spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> str:
    active_alias = alias or spec.alias
    if spec.correlation_sql:
        return _render_binding(spec.correlation_sql, spec, context, alias=active_alias)
    return f"{active_alias}.{spec.event_subject_key} = {context.subject.alias}.{spec.subject_key}"


def _extra_predicates(
    spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> list[str]:
    return [
        _render_binding(item, spec, context, alias=alias or spec.alias)
        for item in spec.extra_predicates
    ]


def _event_time_sql(
    spec: EventSpec, context: CompileContext, *, alias: str | None = None
) -> str:
    active_alias = alias or spec.alias
    if spec.time_expression:
        return _render_binding(spec.time_expression, spec, context, alias=active_alias)
    return f"{active_alias}.{spec.time_column}"


def compile_relation(relation: event_ir.Relation, context: CompileContext) -> RelationPlan:
    if isinstance(relation, Source):
        spec = context.event_spec(relation.name)
        if spec.binding == "subject_column":
            if relation.correlation == "none":
                raise SqlCompileError("주체 컬럼 사건은 전역 비상관 관계로 사용할 수 없습니다")
            # 주체 테이블 컬럼으로 표현되는 사건은 독립 관계가 아니다 — 별도 서브쿼리를 만들지 않는다.
            return RelationPlan(
                from_sql="", where=[], group_by=[], scope={relation.name: context.subject.alias},
                root_source=relation.name, binding="subject_column", params={},
            )
        return RelationPlan(
            from_sql=_source_sql(spec, context),
            where=[
                *([_correlation(spec, context)] if relation.correlation == "subject" else []),
                *_extra_predicates(spec, context),
            ],
            group_by=[], scope={relation.name: spec.alias},
            root_source=relation.name, binding="fact_table", params={},
        )

    if isinstance(relation, Filter):
        plan = compile_relation(relation.relation, context)
        compiled = compile_condition(relation.where, _relation_context(plan, context))
        plan.where.append(compiled.sql)
        plan.params.update(compiled.params)
        return plan

    if isinstance(relation, Join):
        left = compile_relation(relation.left, context)
        if left.binding != "fact_table":
            raise SqlCompileError("주체 컬럼 사건은 조인할 수 없습니다")
        if relation.kind == "inner" and isinstance(relation.right, Source):
            # Exact legacy shape: the joined source is governed by ON, not by
            # an additional subject correlation predicate.
            right_spec = context.event_spec(relation.right.name)
            if right_spec.from_sql:
                raise SqlCompileError("복합 물리 소스는 Join 오른쪽이 아니라 독립 Source 로 사용해야 합니다")
            left.scope[relation.right.name] = right_spec.alias
            on_sql = compile_condition(relation.on, _relation_context(left, context))
            left.from_sql += f" INNER JOIN {_source_sql(right_spec, context)} ON {on_sql.sql}"
            left.where.extend(_extra_predicates(right_spec, context))
            left.params.update(on_sql.params)
            return left

        right = compile_relation(relation.right, context)
        if right.binding != "fact_table":
            raise SqlCompileError("주체 컬럼 사건은 파생 관계 조인 오른쪽에 사용할 수 없습니다")
        if relation.kind in {"semi", "anti"}:
            return _compile_membership_join(relation, left, right, context)
        return _compile_derived_inner_join(relation, left, right, context)

    if isinstance(relation, Group):
        plan = compile_relation(relation.relation, context)
        inner = _relation_context(plan, context)
        plan.group_by.extend(compile_scalar(key, inner) for key in relation.keys)
        return plan

    if isinstance(relation, Project):
        plan = compile_relation(relation.relation, context)
        inner = _relation_context(plan, context)
        projection: list[str] = []
        aliases: dict[str, str] = {}
        expressions: dict[str, str] = {}
        for item in relation.items:
            expression = compile_scalar(item.expression, inner)
            projection.append(f"{expression} AS {item.name}")
            aliases[item.name] = item.name
            expressions[item.name] = expression
            if isinstance(item.expression, FieldRef):
                aliases[item.expression.name] = item.name
                expressions[item.expression.name] = expression
        plan.projection = projection
        plan.output_aliases = aliases
        plan.output_expressions = expressions
        return plan

    if isinstance(relation, Summarize):
        plan = compile_relation(relation.relation, context)
        if plan.group_by or plan.projection or plan.order_by or plan.limit is not None:
            raise SqlCompileError("summarize 입력은 아직 materialize 된 관계를 지원하지 않습니다")
        inner = _relation_context(plan, context)
        projection: list[str] = []
        aliases: dict[str, str] = {}
        expressions: dict[str, str] = {}
        group_by: list[str] = []
        for key in relation.keys:
            expression = compile_scalar(key.expression, inner)
            projection.append(f"{expression} AS {key.name}")
            group_by.append(expression)
            aliases[key.name] = key.name
            expressions[key.name] = expression
            if isinstance(key.expression, FieldRef):
                aliases[key.expression.name] = key.name
                expressions[key.expression.name] = expression
        for measure in relation.measures:
            expression = _named_measure_expression(measure, inner)
            projection.append(f"{expression} AS {measure.name}")
            aliases[measure.name] = measure.name
            expressions[measure.name] = expression
        plan.projection = projection
        plan.output_aliases = aliases
        plan.output_expressions = expressions
        plan.group_by = group_by
        return plan

    if isinstance(relation, Order):
        plan = compile_relation(relation.relation, context)
        inner = _relation_context(plan, context)
        order_by: list[str] = []
        for key in relation.keys:
            expression = plan.output_expressions.get(key.name)
            if expression is None and "." in key.name:
                expression = compile_scalar(FieldRef(key.name), inner)
            if expression is None:
                raise SqlCompileError(f"정렬 출력이 관계에 없습니다: {key.name}")
            order_by.append(f"{expression} {key.direction.upper()}")
        plan.order_by = order_by
        return plan

    if isinstance(relation, Limit):
        plan = compile_relation(relation.relation, context)
        plan.limit = min(plan.limit, relation.count) if plan.limit is not None else relation.count
        return plan

    raise SqlCompileError(f"지원하지 않는 관계입니다: {relation!r}")


def _relation_context(plan: RelationPlan, context: CompileContext) -> CompileContext:
    return context.with_scope(plan.scope).with_field_bindings(plan.field_bindings)


def _named_measure_expression(
    measure: event_ir.NamedMeasure, context: CompileContext
) -> str:
    argument = "*" if measure.expression is None else compile_scalar(measure.expression, context)
    distinct = "DISTINCT " if measure.distinct and argument != "*" else ""
    return f"{measure.function.upper()}({distinct}{argument})"


def _merge_params(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    duplicated = target.keys() & incoming.keys()
    if duplicated:
        raise SqlCompileError(f"SQL 파라미터 이름이 중복되었습니다: {sorted(duplicated)}")
    target.update(incoming)


def _derived_field_bindings(plan: RelationPlan, alias: str) -> dict[str, str]:
    return {
        symbol: f"{alias}.{column}"
        for symbol, column in plan.output_aliases.items()
        if "." in symbol
    }


def _compile_membership_join(
    relation: Join,
    left: RelationPlan,
    right: RelationPlan,
    context: CompileContext,
) -> RelationPlan:
    materialized = bool(
        right.projection or right.group_by or right.order_by or right.limit is not None
    )
    if materialized:
        if not right.projection:
            raise SqlCompileError("파생 조인 관계에는 명시적 출력이 필요합니다")
        alias = f"ER{context.next_index()}"
        bindings = _derived_field_bindings(right, alias)
        # Join.on has a directional contract: its left scalar is evaluated in
        # the left relation and its right scalar in the right relation.  A
        # self-semi-join legitimately uses the same canonical field on both
        # sides; merging bindings first would turn ``left.id = right.id`` into
        # the tautology ``right.id = right.id``.
        left_context = _relation_context(left, context)
        right_context = context.with_field_bindings(bindings)
        on_sql = CompiledCondition(
            sql=(
                f"{compile_scalar(relation.on.left, left_context)} "
                f"{relation.on.operator} "
                f"{compile_scalar(relation.on.right, right_context)}"
            )
        )
        body = _subquery(right, None, context)
        predicate = f"EXISTS (SELECT 1 FROM ({body}) AS {alias} WHERE {on_sql.sql})"
    else:
        scope = {**left.scope, **right.scope}
        bindings = {**left.field_bindings, **right.field_bindings}
        on_context = context.with_scope(scope).with_field_bindings(bindings)
        on_sql = compile_condition(relation.on, on_context)
        right.where.append(on_sql.sql)
        predicate = f"EXISTS ({_subquery(right, '1', context)})"
    if relation.kind == "anti":
        predicate = "NOT " + predicate
    left.where.append(predicate)
    _merge_params(left.params, right.params)
    _merge_params(left.params, on_sql.params)
    return left


def _compile_derived_inner_join(
    relation: Join,
    left: RelationPlan,
    right: RelationPlan,
    context: CompileContext,
) -> RelationPlan:
    if not right.projection:
        raise SqlCompileError("파생 inner join 오른쪽에는 명시적 출력이 필요합니다")
    alias = f"ER{context.next_index()}"
    bindings = _derived_field_bindings(right, alias)
    on_context = _relation_context(left, context).with_field_bindings(bindings)
    on_sql = compile_condition(relation.on, on_context)
    left.from_sql += f" INNER JOIN ({_subquery(right, None, context)}) AS {alias} ON {on_sql.sql}"
    left.field_bindings.update(bindings)
    _merge_params(left.params, right.params)
    _merge_params(left.params, on_sql.params)
    return left


def _subquery(
    plan: RelationPlan, projection: str | None, context: CompileContext
) -> str:
    selected = projection if projection is not None else ", ".join(plan.projection or ["*"])
    prefix = context.dialect.row_limit_prefix(plan.limit) if plan.limit is not None else ""
    parts = [f"SELECT {prefix}{selected} FROM {plan.from_sql}"]
    if plan.where:
        parts.append("WHERE " + " AND ".join(plan.where))
    if plan.group_by:
        parts.append("GROUP BY " + ", ".join(plan.group_by))
    if plan.order_by:
        parts.append("ORDER BY " + ", ".join(plan.order_by))
    if plan.limit is not None:
        suffix = context.dialect.row_limit_suffix(plan.limit)
        if suffix:
            parts.append(suffix)
    return " ".join(parts)


# ── 스칼라 ────────────────────────────────────────────────────────────────────────


def compile_scalar(scalar: event_ir.Scalar, context: CompileContext) -> str:
    if isinstance(scalar, Literal):
        return _sql_quote(scalar.value) if isinstance(scalar.value, str) else str(scalar.value)

    if isinstance(scalar, FieldRef):
        bound = context._field_bindings.get(scalar.name)
        if bound is not None:
            return bound
        spec = context.field_spec(scalar.name)
        alias = context._scope.get(spec.source) or (
            context.subject.alias if spec.source == context.subject.name else None
        )
        if alias is None:
            raise SqlCompileError(f"'{scalar.name}' 을 참조할 관계가 현재 스코프에 없습니다")
        if spec.expression:
            try:
                return spec.expression.format(
                    alias=alias,
                    subject_alias=context.subject.alias,
                    subject_key=context.subject.key,
                )
            except (KeyError, ValueError) as exc:
                raise SqlCompileError(f"필드 '{scalar.name}' 물리 바인딩 형식이 잘못되었습니다") from exc
        return f"{alias}.{spec.column}"

    if isinstance(scalar, Arithmetic):
        return f"({compile_scalar(scalar.left, context)} {scalar.operator} {compile_scalar(scalar.right, context)})"

    if isinstance(scalar, Aggregate):
        plan = compile_relation(scalar.relation, context)
        if plan.binding != "fact_table":
            raise SqlCompileError("주체 컬럼 사건은 집계할 수 없습니다")
        if plan.group_by:
            raise SqlCompileError("그룹 집계는 비교 조건에서만 컴파일됩니다(EXISTS + HAVING)")
        return _aggregate_subquery(scalar, plan, context)

    raise SqlCompileError(f"지원하지 않는 스칼라입니다: {scalar!r}")


def _aggregate_expression(aggregate: Aggregate, plan: RelationPlan, context: CompileContext) -> str:
    inner = context.with_scope(plan.scope)
    argument = "*" if aggregate.expression is None else compile_scalar(aggregate.expression, inner)
    distinct = "DISTINCT " if aggregate.distinct and argument != "*" else ""
    return f"{aggregate.function.upper()}({distinct}{argument})"


def _aggregate_subquery(aggregate: Aggregate, plan: RelationPlan, context: CompileContext) -> str:
    expression = _aggregate_expression(aggregate, plan, context)
    # 행이 없을 때 SUM 은 NULL 을 돌려줘 '0원 이상' 같은 조건이 조용히 거짓이 된다 — 0 으로 접는다.
    # COUNT 는 0 을 돌려주므로 감싸지 않는다(불필요한 함수 중첩은 인덱스 판단만 흐린다).
    projection = context.dialect.coalesce(expression, "0") if aggregate.function == "sum" else expression
    return f"({_subquery(plan, projection, context)})"


# ── 조건 ──────────────────────────────────────────────────────────────────────────


def compile_condition(condition: event_ir.Condition, context: CompileContext) -> CompiledCondition:
    if isinstance(condition, And):
        return _combine(condition.operands, " AND ", context)
    if isinstance(condition, Or):
        return _combine(condition.operands, " OR ", context)
    if isinstance(condition, Not):
        inner = compile_condition(condition.operand, context)
        # NOT EXISTS 는 SQL 이 직접 지원하는 형태라 NOT (EXISTS ...) 로 감싸지 않는다(플랜 동일, 가독성 우위).
        if inner.sql.startswith("EXISTS ("):
            return CompiledCondition(sql="NOT " + inner.sql, params=inner.params)
        # SQL's NOT UNKNOWN is still UNKNOWN, while an audience complement is
        # two-valued: members for whom the predicate is not true (including
        # NULL/unknown) must remain.  Fold the predicate to 1/0 before NOT.
        return CompiledCondition(
            sql=f"NOT (CASE WHEN ({inner.sql}) THEN 1 ELSE 0 END = 1)",
            params=inner.params,
        )
    if isinstance(condition, TimeFilter):
        spec = context.field_spec(condition.field.name)
        return compile_time_window(
            compile_scalar(condition.field, context), condition.window,
            f"event_{context.next_index()}", data_type=spec.data_type, context=context,
        )
    if isinstance(condition, Exists):
        return _compile_exists(condition, context)
    if isinstance(condition, Comparison):
        return _compile_comparison(condition, context)
    if isinstance(condition, TemporalRelation):
        return _compile_temporal_relation(condition, context)
    raise SqlCompileError(f"지원하지 않는 조건입니다: {condition!r}")


def _combine(
    operands: tuple[event_ir.Condition, ...], joiner: str, context: CompileContext
) -> CompiledCondition:
    compiled = [compile_condition(operand, context) for operand in operands]
    if len(compiled) == 1:
        return compiled[0]
    params: dict[str, Any] = {}
    for item in compiled:
        duplicated = params.keys() & item.params.keys()
        if duplicated:
            raise SqlCompileError(f"SQL 파라미터 이름이 중복되었습니다: {sorted(duplicated)}")
        params.update(item.params)
    return CompiledCondition(sql=joiner.join(f"({item.sql})" for item in compiled), params=params)


def _compile_exists(condition: Exists, context: CompileContext) -> CompiledCondition:
    plan = compile_relation(condition.relation, context)
    if plan.binding == "subject_column":
        # 주체 컬럼 사건의 '존재'는 값이 있느냐다. 창이 붙었으면 Filter 가 이미 술어를 만들어 뒀다.
        spec = context.event_spec(plan.root_source)
        predicates = [
            f"{_event_time_sql(spec, context, alias=context.subject.alias)} IS NOT NULL",
            *plan.where,
        ]
        return CompiledCondition(sql="(" + " AND ".join(predicates) + ")", params=plan.params)
    return CompiledCondition(sql=f"EXISTS ({_subquery(plan, '1', context)})", params=plan.params)


def _compile_comparison(condition: Comparison, context: CompileContext) -> CompiledCondition:
    # 그룹 집계 비교('한 주문에 3개 이상')는 스칼라 서브쿼리로 표현할 수 없다 — grain 이 회원이 아니라
    # 그룹이므로 EXISTS + HAVING 이 정확한 번역이다.
    if isinstance(condition.left, Aggregate):
        plan = compile_relation(condition.left.relation, context)
        if plan.group_by:
            having = (
                f"{_aggregate_expression(condition.left, plan, context)} "
                f"{condition.operator} {compile_scalar(condition.right, context)}"
            )
            return CompiledCondition(
                sql=f"EXISTS ({_subquery(plan, '1', context)} HAVING {having})",
                params=plan.params,
            )
    left_scalar, right_scalar = condition.left, condition.right
    if isinstance(left_scalar, FieldRef) and isinstance(right_scalar, Literal):
        right_scalar = Literal(context.field_spec(left_scalar.name).physical_value(right_scalar.value))
    elif isinstance(right_scalar, FieldRef) and isinstance(left_scalar, Literal):
        left_scalar = Literal(context.field_spec(right_scalar.name).physical_value(left_scalar.value))
    left = compile_scalar(left_scalar, context)
    right = compile_scalar(right_scalar, context)
    return CompiledCondition(sql=f"{left} {condition.operator} {right}")


def _event_time_anchor(reference: EventReference, context: CompileContext) -> str:
    """시간 관계의 기준 시점 — 팩트 테이블이면 MIN/MAX 상관 서브쿼리, 주체 컬럼이면 그 컬럼."""
    spec = context.event_spec(reference.source)
    if spec.binding == "subject_column":
        return _event_time_sql(spec, context, alias=context.subject.alias)
    aggregate = {"first": "MIN", "last": "MAX"}.get(reference.selector)
    if aggregate is None:
        raise SqlCompileError(f"시간 관계의 기준 시점은 first/last 여야 합니다(받은 값: {reference.selector})")
    alias = spec.alias + "1"
    predicates = [
        _correlation(spec, context, alias=alias),
        *_extra_predicates(spec, context, alias=alias),
    ]
    return (
        f"(SELECT {aggregate}({_event_time_sql(spec, context, alias=alias)}) "
        f"FROM {_source_sql(spec, context, alias=alias)} "
        f"WHERE {' AND '.join(predicates)})"
    )


def _compile_temporal_relation(condition: TemporalRelation, context: CompileContext) -> CompiledCondition:
    """'A 후 D 이내에 B' — 기준 시점을 스칼라로 잡고 대상 사건을 그 창 안에서 찾는다.

    소스 이름만 바꾸면 '가입 후 7일 이내 구매'·'배송 후 30일 이내 반품'에 그대로 쓰인다."""
    if condition.operator != "within_after":
        raise SqlCompileError(f"아직 지원하지 않는 시간 관계입니다: {condition.operator}")
    target = context.event_spec(condition.right.source)
    if target.binding != "fact_table":
        raise SqlCompileError("시간 관계의 대상 사건은 팩트 테이블이어야 합니다")
    days = condition.duration.days
    if days is None:
        raise SqlCompileError(
            f"'{target.time_column}' 은 날짜 단위 컬럼이라 {condition.duration.unit} 단위 관계를 표현할 수 없습니다"
        )

    anchor = _event_time_anchor(condition.left, context)
    alias = target.alias + "2"
    upper = (
        context.dialect.char8_shift(anchor, days)
        if target.time_format == "char8"
        else context.dialect.date_add_days(anchor, days)
    )
    predicates = [
        _correlation(target, context, alias=alias),
        *_extra_predicates(target, context, alias=alias),
        # 기준 시점 **이후**부터(같은 사건이면 기준이 된 발생 자체를 다시 세지 않도록 초과 비교) 창 끝까지.
        f"{_event_time_sql(target, context, alias=alias)} > {anchor}",
        f"{_event_time_sql(target, context, alias=alias)} <= {upper}",
    ]
    return CompiledCondition(
        sql=f"EXISTS (SELECT 1 FROM {_source_sql(target, context, alias=alias)} "
            f"WHERE {' AND '.join(predicates)})"
    )


# ── 진입점 ────────────────────────────────────────────────────────────────────────


def compile_expression(
    expression: event_ir.Condition, *, context: CompileContext | None = None
) -> CompiledCondition:
    """조건 IR → SQL 조건. AND/OR 우선순위는 트리 모양 그대로 괄호로 보존한다."""
    return compile_condition(expression, context or CompileContext())


def compile_expression_sql(
    expression: event_ir.Condition,
    *,
    subject: SubjectSpec | None = None,
    registry: dict[str, EventSpec] | None = None,
    fields: dict[str, FieldSpec] | None = None,
    dialect: SqlDialect | None = None,
    today: date | None = None,
) -> str:
    """리터럴 인라인 SQL 조건 문자열(문자열 SQL 을 검증·가드하는 기존 빌더용 진입점)."""
    resolved = registry or dict(EVENT_REGISTRY)
    context = CompileContext(
        subject=subject or SubjectSpec(), registry=resolved,
        fields=fields or resolve_fields(resolved),
        dialect=dialect or get_dialect("tsql"), literals=True, today=today,
    )
    return compile_expression(expression, context=context).sql


def supported_events(registry: dict[str, EventSpec] | None = None) -> frozenset[str]:
    return frozenset(registry or EVENT_REGISTRY)


def unsupported_events(
    expression: event_ir.Condition, registry: dict[str, EventSpec] | None = None
) -> list[str]:
    """레지스트리에 없는 사건 심볼 목록(컴파일 전 fail-close 판정용)."""
    known = supported_events(registry)
    subject_name = SubjectSpec().name
    return sorted(name for name in event_ir.sources(expression) if name not in known and name != subject_name)


def unsupported_fields(
    expression: event_ir.Condition,
    registry: dict[str, EventSpec] | None = None,
    fields: dict[str, FieldSpec] | None = None,
) -> list[str]:
    """레지스트리에 없는 필드 심볼 목록."""
    resolved = fields or resolve_fields(registry or EVENT_REGISTRY)
    return sorted(name for name in event_ir.field_names(expression) if name not in resolved)


def _semantic_payload(value: Any) -> Any:
    """노드 지문용 의미 정규형. 무엇이 출처인지는 :mod:`semantic_fields` 가 단일 소스로 소유한다."""
    return semantic_fields.strip_provenance(value)


def _capability_node_id(expression: event_ir.Condition) -> str:
    payload = json.dumps(
        _semantic_payload(expression.to_dict()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "event_node_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _capability_leaves(expression: event_ir.Condition) -> list[event_ir.Condition]:
    if isinstance(expression, (And, Or)):
        return [leaf for child in expression.operands for leaf in _capability_leaves(child)]
    return [expression]


def _fresh_context(context: CompileContext) -> CompileContext:
    return CompileContext(
        subject=context.subject,
        registry=context.registry,
        fields=context.fields,
        dialect=context.dialect,
        literals=context.literals,
        today=context.today,
        _field_bindings=context._field_bindings,
    )


def validate_compiler_capability(
    expression: event_ir.Condition,
    *,
    context: CompileContext | None = None,
) -> CompilerCapabilityResult:
    """Report physical compiler capability without changing semantic status.

    Capability is evaluated per Boolean leaf.  A mixture of supported and
    unsupported leaves is ``partially_supported`` and must never be projected
    to SQL by dropping the unsupported leaves.
    """

    active = context or CompileContext()
    issues: list[CompilerCapabilityIssue] = []
    supported: list[str] = []
    unsupported: list[str] = []
    for leaf in _capability_leaves(expression):
        node_id = _capability_node_id(leaf)
        leaf_issues: list[CompilerCapabilityIssue] = []
        for symbol in unsupported_events(leaf, active.registry):
            leaf_issues.append(CompilerCapabilityIssue("compiler_event_unregistered", node_id, symbol))
        for symbol in unsupported_fields(leaf, active.registry, active.fields):
            leaf_issues.append(CompilerCapabilityIssue("compiler_field_unregistered", node_id, symbol))
        if not leaf_issues:
            try:
                compile_expression(leaf, context=_fresh_context(active))
            except (SqlCompileError, event_ir.IrSchemaError, ValueError):
                leaf_issues.append(CompilerCapabilityIssue("compiler_operation_unsupported", node_id))
        if leaf_issues:
            unsupported.append(node_id)
            issues.extend(leaf_issues)
        else:
            supported.append(node_id)

    if unsupported and supported:
        status = CAPABILITY_PARTIALLY_SUPPORTED
    elif unsupported or not supported:
        status = CAPABILITY_UNSUPPORTED
    else:
        status = CAPABILITY_SUPPORTED
    return CompilerCapabilityResult(
        status=status,
        issues=tuple(issues),
        supported_node_ids=tuple(supported),
        unsupported_node_ids=tuple(unsupported),
    )
