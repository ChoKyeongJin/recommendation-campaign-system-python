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

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import event_ir
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
    Literal,
    Not,
    Or,
    RelativeWindow,
    RollingWindow,
    Source,
    TemporalRelation,
    TimeFilter,
)
from sql_dialect import SqlDialect, get_dialect


class SqlCompileError(Exception):
    """IR 은 유효하지만 이 스키마/방언으로는 표현할 수 없다. 의미를 줄이지 말고 여기서 멈춘다."""


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


@dataclass(frozen=True)
class FieldSpec:
    """필드 심볼 하나의 물리 바인딩. 새 업무 속성은 이 표에 한 줄이면 쓸 수 있다."""

    source: str
    column: str
    data_type: str = "number"  # number | string | date | date_char8


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
        )


def _sql_quote(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


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


def _correlation(spec: EventSpec, context: CompileContext) -> str:
    return f"{spec.alias}.{spec.event_subject_key} = {context.subject.alias}.{spec.subject_key}"


def compile_relation(relation: event_ir.Relation, context: CompileContext) -> RelationPlan:
    if isinstance(relation, Source):
        spec = context.event_spec(relation.name)
        if spec.binding == "subject_column":
            # 주체 테이블 컬럼으로 표현되는 사건은 독립 관계가 아니다 — 별도 서브쿼리를 만들지 않는다.
            return RelationPlan(
                from_sql="", where=[], group_by=[], scope={relation.name: context.subject.alias},
                root_source=relation.name, binding="subject_column", params={},
            )
        return RelationPlan(
            from_sql=f"{spec.table} {spec.alias}",
            where=[_correlation(spec, context), *(item.format(alias=spec.alias) for item in spec.extra_predicates)],
            group_by=[], scope={relation.name: spec.alias},
            root_source=relation.name, binding="fact_table", params={},
        )

    if isinstance(relation, Filter):
        plan = compile_relation(relation.relation, context)
        compiled = compile_condition(relation.where, context.with_scope(plan.scope))
        plan.where.append(compiled.sql)
        plan.params.update(compiled.params)
        return plan

    if isinstance(relation, Join):
        plan = compile_relation(relation.left, context)
        if plan.binding != "fact_table":
            raise SqlCompileError("주체 컬럼 사건은 조인할 수 없습니다")
        right_spec = context.event_spec(relation.right.name)
        plan.scope[relation.right.name] = right_spec.alias
        on_sql = compile_condition(relation.on, context.with_scope(plan.scope))
        plan.from_sql += f" INNER JOIN {right_spec.table} {right_spec.alias} ON {on_sql.sql}"
        plan.where.extend(item.format(alias=right_spec.alias) for item in right_spec.extra_predicates)
        plan.params.update(on_sql.params)
        return plan

    if isinstance(relation, Group):
        plan = compile_relation(relation.relation, context)
        inner = context.with_scope(plan.scope)
        plan.group_by.extend(compile_scalar(key, inner) for key in relation.keys)
        return plan

    raise SqlCompileError(f"지원하지 않는 관계입니다: {relation!r}")


def _subquery(plan: RelationPlan, projection: str) -> str:
    parts = [f"SELECT {projection} FROM {plan.from_sql}"]
    if plan.where:
        parts.append("WHERE " + " AND ".join(plan.where))
    if plan.group_by:
        parts.append("GROUP BY " + ", ".join(plan.group_by))
    return " ".join(parts)


# ── 스칼라 ────────────────────────────────────────────────────────────────────────


def compile_scalar(scalar: event_ir.Scalar, context: CompileContext) -> str:
    if isinstance(scalar, Literal):
        return _sql_quote(scalar.value) if isinstance(scalar.value, str) else str(scalar.value)

    if isinstance(scalar, FieldRef):
        spec = context.field_spec(scalar.name)
        alias = context._scope.get(spec.source) or (
            context.subject.alias if spec.source == context.subject.name else None
        )
        if alias is None:
            raise SqlCompileError(f"'{scalar.name}' 을 참조할 관계가 현재 스코프에 없습니다")
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
    return f"({_subquery(plan, projection)})"


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
        return CompiledCondition(sql=f"NOT ({inner.sql})", params=inner.params)
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
        predicates = [f"{context.subject.alias}.{spec.time_column} IS NOT NULL", *plan.where]
        return CompiledCondition(sql="(" + " AND ".join(predicates) + ")", params=plan.params)
    return CompiledCondition(sql=f"EXISTS ({_subquery(plan, '1')})", params=plan.params)


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
            return CompiledCondition(sql=f"EXISTS ({_subquery(plan, '1')} HAVING {having})", params=plan.params)
    left = compile_scalar(condition.left, context)
    right = compile_scalar(condition.right, context)
    return CompiledCondition(sql=f"{left} {condition.operator} {right}")


def _event_time_anchor(reference: EventReference, context: CompileContext) -> str:
    """시간 관계의 기준 시점 — 팩트 테이블이면 MIN/MAX 상관 서브쿼리, 주체 컬럼이면 그 컬럼."""
    spec = context.event_spec(reference.source)
    if spec.binding == "subject_column":
        return f"{context.subject.alias}.{spec.time_column}"
    aggregate = {"first": "MIN", "last": "MAX"}.get(reference.selector)
    if aggregate is None:
        raise SqlCompileError(f"시간 관계의 기준 시점은 first/last 여야 합니다(받은 값: {reference.selector})")
    alias = spec.alias + "1"
    predicates = [
        f"{alias}.{spec.event_subject_key} = {context.subject.alias}.{spec.subject_key}",
        *(item.format(alias=alias) for item in spec.extra_predicates),
    ]
    return (
        f"(SELECT {aggregate}({alias}.{spec.time_column}) FROM {spec.table} {alias} "
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
        f"{alias}.{target.event_subject_key} = {context.subject.alias}.{target.subject_key}",
        *(item.format(alias=alias) for item in target.extra_predicates),
        # 기준 시점 **이후**부터(같은 사건이면 기준이 된 발생 자체를 다시 세지 않도록 초과 비교) 창 끝까지.
        f"{alias}.{target.time_column} > {anchor}",
        f"{alias}.{target.time_column} <= {upper}",
    ]
    return CompiledCondition(sql=f"EXISTS (SELECT 1 FROM {target.table} {alias} WHERE {' AND '.join(predicates)})")


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
