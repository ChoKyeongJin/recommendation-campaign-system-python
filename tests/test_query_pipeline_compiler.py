"""SqlCompiler 계약 — 값은 파라미터, 식별자는 바인딩, 미지원은 명시적 실패."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from query_pipeline_fixtures import schema_bindings, sql_context

import event_compiler
import event_ir
import graph_rag
import query_pipeline
from query_pipeline.compiler.base import (
    MissingBindingError,
    UnsupportedPlanError,
)
from query_pipeline.compiler.bindings import schema_bindings_from_compiler
from query_pipeline.compiler.models import (
    ParameterStyle,
    SchemaBindings,
    SqlCompilationContext,
    SqlDialect,
)
from query_pipeline.compiler.postgresql import PostgresqlSqlCompiler
from query_pipeline.compiler.sql_compiler import LogicalPlanSqlCompiler
from query_pipeline.event_query.expressions import (
    AbsoluteWindow,
    AggregateFunction,
    AndExpression,
    AttributeOperand,
    ComparisonExpression,
    ComparisonOperator,
    EntityRelation,
    ExistsExpression,
    FilteredRelation,
    LiteralOperand,
    NotExpression,
    RelativeWindow,
    RollingWindow,
    TimeWindowExpression,
    WindowUnit,
)
from query_pipeline.event_query.models import (
    CapabilityRequirements,
    EventQuerySpec,
    QueryOutput,
    QuerySource,
    ResolvedBinding,
    ResolvedMeasure,
)
from query_pipeline.planning.logical_planner import AudienceLogicalPlanner

NOW = datetime(2026, 8, 3, tzinfo=UTC)
EVENT_TYPE = AttributeOperand(entity="payment_events", attribute="event_type")
OCCURRED_AT = AttributeOperand(entity="payment_events", attribute="occurred_at")
CUSTOMER = AttributeOperand(entity="payment_events", attribute="customer_id")

INJECTION = "payment.failed'; DROP TABLE payment_events; --"


def _spec(expression: object, output: QueryOutput, *operands: AttributeOperand) -> EventQuerySpec:
    return EventQuerySpec(
        id="spec-1",
        version="1",
        expression=expression,  # type: ignore[arg-type]
        output=output,
        bindings={
            operand.logical_name: ResolvedBinding(
                logical_name=operand.logical_name,
                entity=operand.entity,
                attribute=operand.attribute,
            )
            for operand in operands
        },
        source=QuerySource(requirement_id="req-1", requirement_version="1"),
        capabilities=CapabilityRequirements(),
        created_at=NOW,
    )


def _compile(expression: object, output: QueryOutput, *operands: AttributeOperand):
    plan = AudienceLogicalPlanner().create_plan(_spec(expression, output, *operands))
    return LogicalPlanSqlCompiler().compile(plan, sql_context())


def test_sql_compiler_uses_parameters() -> None:
    expression = AndExpression(
        expressions=(
            ComparisonExpression(
                operator=ComparisonOperator.EQ,
                left=EVENT_TYPE,
                right=LiteralOperand(value="payment.failed"),
            ),
            TimeWindowExpression(
                attribute=OCCURRED_AT,
                window=AbsoluteWindow(
                    start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)
                ),
            ),
        )
    )
    compiled = _compile(
        expression, QueryOutput(entity="payment_events"), EVENT_TYPE, OCCURRED_AT
    )
    assert compiled.dialect is SqlDialect.POSTGRESQL
    assert compiled.parameter_map == {
        "event_type": "payment.failed",
        "occurred_at_start": date(2026, 7, 1),
        "occurred_at_end": date(2026, 8, 1),
    }
    assert ":event_type" in compiled.sql
    assert "payment.failed" not in compiled.sql


def test_sql_does_not_inline_user_input() -> None:
    expression = ComparisonExpression(
        operator=ComparisonOperator.EQ,
        left=EVENT_TYPE,
        right=LiteralOperand(value=INJECTION),
    )
    compiled = _compile(expression, QueryOutput(entity="payment_events"), EVENT_TYPE)
    assert "DROP TABLE" not in compiled.sql
    assert compiled.parameter_map["event_type"] == INJECTION


def test_postgresql_compiler_uses_schema_bindings() -> None:
    expression = ComparisonExpression(
        operator=ComparisonOperator.EQ,
        left=EVENT_TYPE,
        right=LiteralOperand(value="payment.failed"),
    )
    output = QueryOutput(
        entity="payment_events",
        dimensions=(CUSTOMER,),
        measures=(
            ResolvedMeasure(alias="failure_count", function=AggregateFunction.COUNT),
        ),
    )
    plan = AudienceLogicalPlanner().create_plan(
        _spec(expression, output, EVENT_TYPE, CUSTOMER)
    )
    compiled = PostgresqlSqlCompiler().compile(plan, sql_context())
    assert 'FROM "payment_events" AS "t1"' in compiled.sql
    assert '"t1"."customer_id"' in compiled.sql
    assert 'COUNT(*) AS "failure_count"' in compiled.sql
    assert 'GROUP BY "t1"."customer_id"' in compiled.sql


def test_postgresql_compiler_rejects_other_dialects() -> None:
    plan = AudienceLogicalPlanner().create_plan(
        _spec(
            ComparisonExpression(
                operator=ComparisonOperator.EQ,
                left=EVENT_TYPE,
                right=LiteralOperand(value="x"),
            ),
            QueryOutput(entity="payment_events"),
            EVENT_TYPE,
        )
    )
    with pytest.raises(ValueError, match="다른 방언"):
        PostgresqlSqlCompiler().compile(plan, sql_context(SqlDialect.MYSQL))


def test_relative_window_must_be_resolved_before_compilation() -> None:
    """'N단위 전'은 기준일이 있어야 확정된다 — 기준일의 소유자는 Resolver 다."""
    relative = TimeWindowExpression(
        attribute=OCCURRED_AT,
        window=RelativeWindow(value=3, unit=WindowUnit.MONTH),
    )
    with pytest.raises(UnsupportedPlanError, match="절대 구간"):
        _compile(relative, QueryOutput(entity="payment_events"), OCCURRED_AT)


def test_rolling_window_renders_an_execution_time_cutoff() -> None:
    """롤링 경계는 계획 시점 날짜로 접지 않는다 — 접으면 '최근 7일'이 그 날로 고정된다."""
    rolling = TimeWindowExpression(
        attribute=OCCURRED_AT,
        window=RollingWindow(value=7, unit=WindowUnit.DAY),
    )
    compiled = _compile(rolling, QueryOutput(entity="payment_events"), OCCURRED_AT)
    assert "NOW()" in compiled.sql
    assert "INTERVAL '7 day'" in compiled.sql
    # 날짜가 문장에 굳지 않았다는 것이 이 테스트의 전부다.
    assert not compiled.parameters


def test_rolling_window_follows_the_declared_storage_format() -> None:
    """char8('YYYYMMDD') 컬럼은 날짜가 아니라 **문자열**과 비교해야 한다.

    표기를 무시하고 날짜 경계를 렌더하면 오류가 아니라 사전식 비교로 **항상 0건**이 나온다.
    """
    ordered_at = AttributeOperand(entity="orders", attribute="ordered_at")
    rolling = TimeWindowExpression(
        attribute=ordered_at, window=RollingWindow(value=30, unit=WindowUnit.DAY)
    )
    compiled = _compile(rolling, QueryOutput(entity="orders"), ordered_at)
    assert "TO_CHAR(" in compiled.sql and "YYYYMMDD" in compiled.sql


def test_rolling_window_without_a_declared_storage_format_stops() -> None:
    """표기 선언이 없으면 추측하지 않는다."""
    undeclared = AttributeOperand(entity="payment_events", attribute="event_type")
    rolling = TimeWindowExpression(
        attribute=undeclared, window=RollingWindow(value=7, unit=WindowUnit.DAY)
    )
    with pytest.raises(MissingBindingError, match="저장 타입"):
        _compile(rolling, QueryOutput(entity="payment_events"), undeclared)


def test_rolling_window_on_a_month_grain_column_is_refused() -> None:
    """월 스냅샷 컬럼에 '최근 N일'은 답할 수 없다 — 칸으로 근사하면 다른 구간이 된다."""
    month_column = AttributeOperand(entity="payment_events", attribute="occurred_at")
    bindings = SchemaBindings(
        bindings={
            entity: item.model_copy(
                update={"data_type": {**item.data_type, "occurred_at": "date_char6"}}
            )
            for entity, item in schema_bindings().bindings.items()
        }
    )
    plan = AudienceLogicalPlanner().create_plan(
        _spec(
            TimeWindowExpression(
                attribute=month_column, window=RollingWindow(value=7, unit=WindowUnit.DAY)
            ),
            QueryOutput(entity="payment_events"),
            month_column,
        )
    )
    with pytest.raises(UnsupportedPlanError, match="롤링 창"):
        LogicalPlanSqlCompiler().compile(
            plan,
            SqlCompilationContext(
                dialect=SqlDialect.POSTGRESQL, schema_bindings=bindings
            ),
        )


def test_same_rolling_spec_compiles_identically_on_different_days() -> None:
    """같은 사양을 서로 다른 기준일에 컴파일해도 SQL 이 같아야 한다(계획 시점 고정 금지)."""
    from query_pipeline.requirement.resolver import resolve_period_phrase

    windows = {
        resolve_period_phrase(
            "최근 30일", now=datetime(year, 8, 3, tzinfo=UTC), timezone="Asia/Seoul"
        ).window  # type: ignore[union-attr]
        for year in (2025, 2026)
    }
    assert len(windows) == 1, "롤링 창이 기준일에 따라 달라졌다 — 계획 시점으로 접힌 것이다."
    ordered_at = AttributeOperand(entity="orders", attribute="ordered_at")
    statements = {
        _compile(
            TimeWindowExpression(attribute=ordered_at, window=window),
            QueryOutput(entity="orders"),
            ordered_at,
        ).sql
        for window in windows
    }
    assert len(statements) == 1


def test_capability_declaration_matches_what_the_renderer_actually_does() -> None:
    """"표현할 수 있다"는 선언과 렌더 분기가 갈리면, 그 어긋남은 실행 시점에야 드러난다.

    두 방향 모두 해롭다 — 지원한다고 해 놓고 컴파일에서 실패하면 사유 없는 실패가 되고,
    지원하지 않는다고 해 놓고 렌더가 있으면 **없는 한계**를 사용자에게 말한다.
    """
    from query_pipeline.compiler.capability import GENERIC_SQL_CAPABILITIES
    from query_pipeline.compiler.sql_compiler import SUPPORTED_WINDOW_KINDS

    declared = {
        name.split(".", 1)[1]
        for name in GENERIC_SQL_CAPABILITIES
        if name.startswith("window.")
    }
    assert declared == SUPPORTED_WINDOW_KINDS

    for kind in SUPPORTED_WINDOW_KINDS:
        window = (
            AbsoluteWindow(start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1))
            if kind == "interval"
            else RollingWindow(value=7, unit=WindowUnit.DAY)
        )
        compiled = _compile(
            TimeWindowExpression(attribute=OCCURRED_AT, window=window),
            QueryOutput(entity="payment_events"),
            OCCURRED_AT,
        )
        assert compiled.sql, kind


def test_missing_binding_fails_explicitly() -> None:
    unknown = AttributeOperand(entity="payment_events", attribute="nope")
    expression = ComparisonExpression(
        operator=ComparisonOperator.EQ,
        left=unknown,
        right=LiteralOperand(value=1),
    )
    with pytest.raises(MissingBindingError):
        _compile(expression, QueryOutput(entity="payment_events"), unknown)


def test_exists_uses_declared_correlation() -> None:
    login_time = AttributeOperand(entity="login_events", attribute="occurred_at")
    expression = NotExpression(
        expression=ExistsExpression(
            relation=FilteredRelation(
                relation=EntityRelation(entity="login_events"),
                where=TimeWindowExpression(
                    attribute=login_time,
                    window=AbsoluteWindow(
                        start=date(2026, 7, 27), end_exclusive=date(2026, 8, 3)
                    ),
                ),
            )
        )
    )
    compiled = _compile(expression, QueryOutput(entity="users"), login_time)
    assert 'NOT (EXISTS (SELECT 1 FROM "login_events"' in compiled.sql
    assert '"t2"."user_id" = "t1"."id"' in compiled.sql


def test_exists_without_correlation_declaration_fails() -> None:
    bindings = SchemaBindings(
        bindings={
            item.entity: item.model_copy(update={"correlations": {}})
            for item in schema_bindings().bindings.values()
        }
    )
    expression = ExistsExpression(relation=EntityRelation(entity="login_events"))
    plan = AudienceLogicalPlanner().create_plan(
        _spec(expression, QueryOutput(entity="users"))
    )
    with pytest.raises(MissingBindingError, match="상관 컬럼"):
        LogicalPlanSqlCompiler().compile(
            plan,
            SqlCompilationContext(
                dialect=SqlDialect.POSTGRESQL, schema_bindings=bindings
            ),
        )


def test_in_and_between_use_one_parameter_per_value() -> None:
    expression = ComparisonExpression(
        operator=ComparisonOperator.IN,
        left=EVENT_TYPE,
        right=LiteralOperand(value=("payment.failed", "payment.declined")),
    )
    compiled = _compile(expression, QueryOutput(entity="payment_events"), EVENT_TYPE)
    assert ":event_type_1" in compiled.sql and ":event_type_2" in compiled.sql
    assert len(compiled.parameters) == 2


def test_tsql_uses_top_instead_of_limit() -> None:
    expression = ComparisonExpression(
        operator=ComparisonOperator.EQ,
        left=EVENT_TYPE,
        right=LiteralOperand(value="payment.failed"),
    )
    output = QueryOutput(
        entity="payment_events",
        dimensions=(CUSTOMER,),
        limit=10,
    )
    plan = AudienceLogicalPlanner().create_plan(
        _spec(expression, output, EVENT_TYPE, CUSTOMER)
    )
    compiled = LogicalPlanSqlCompiler().compile(plan, sql_context(SqlDialect.TSQL))
    assert compiled.sql.startswith("SELECT TOP (10) ")
    assert "LIMIT" not in compiled.sql


def test_unsafe_identifier_in_binding_is_rejected() -> None:
    from pydantic import ValidationError

    from query_pipeline.compiler.models import SchemaBinding

    with pytest.raises(ValidationError):
        SchemaBinding(entity="e", table="users; DROP TABLE users")


# ── 실CRM 오디언스 경로(기존 엔진 위임) ───────────────────────────────────────────


PURCHASE_WIRE = {
    "type": "exists",
    "relation": {
        "type": "filter",
        "relation": {"type": "source", "name": "purchase"},
        "where": {
            "type": "time_filter",
            "field": {"type": "field", "name": "purchase.occurred_at"},
            "window": {
                "type": "interval",
                "start": "2026-07-01",
                "end_exclusive": "2026-08-01",
            },
        },
    },
    "evidence": {"text": "지난달 구매", "start": 0, "end": 6},
}


def test_audience_predicate_matches_legacy_engine_byte_for_byte() -> None:
    """새 계층을 통과해도 실CRM SQL 은 한 글자도 달라지지 않는다."""
    legacy = event_compiler.compile_expression(
        event_ir.condition_from_dict(PURCHASE_WIRE),
        context=graph_rag._event_compile_context(),
    ).sql
    compiled = query_pipeline.compile_audience_predicate(
        {"expression": PURCHASE_WIRE, "source": "audience_requirement", "receipts": []},
        compile_context_factory=graph_rag._event_compile_context,
    )
    assert compiled.sql == legacy
    assert compiled.dialect is SqlDialect.TSQL


def test_audience_predicate_can_emit_bound_parameters() -> None:
    compiled = query_pipeline.compile_audience_predicate(
        {"expression": PURCHASE_WIRE, "source": "audience_requirement", "receipts": []},
        compile_context_factory=graph_rag._event_compile_context,
        parameter_style=ParameterStyle.NAMED,
    )
    assert "'20260701'" not in compiled.sql
    assert compiled.parameters
    assert all(":" + parameter.name in compiled.sql for parameter in compiled.parameters)


def test_audience_predicate_reports_the_failing_stage() -> None:
    with pytest.raises(query_pipeline.QueryPipelineError) as excinfo:
        query_pipeline.compile_audience_predicate(
            {"expression": {"type": "unknown_node"}},
            compile_context_factory=graph_rag._event_compile_context,
        )
    assert excinfo.value.stage == "event_expression_payload_adapter"


def test_schema_bindings_are_derived_from_the_resolved_catalog() -> None:
    import audience_runtime

    catalog = audience_runtime.resolve_audience_catalog()
    bindings = schema_bindings_from_compiler(
        events=catalog.compiler_events,
        fields=catalog.compiler_fields,
        subject=catalog.subject,
    )
    purchase = bindings.require("purchase")
    assert purchase.table == catalog.compiler_events["purchase"].table
    assert purchase.correlations[catalog.subject.name] == (
        catalog.compiler_events["purchase"].event_subject_key
    )
    assert bindings.require(catalog.subject.name).table == catalog.subject.table


def test_storage_formats_are_derived_not_restated() -> None:
    """``data_type`` 도 카탈로그에서 **파생**된다 — 손으로 적으면 두 번째 소유자가 된다."""
    import audience_runtime

    catalog = audience_runtime.resolve_audience_catalog()
    bindings = schema_bindings_from_compiler(
        events=catalog.compiler_events,
        fields=catalog.compiler_fields,
        subject=catalog.subject,
    )
    purchase = bindings.require("purchase")
    assert purchase.column_type("amount") == (
        catalog.compiler_fields["purchase.amount"].data_type
    )
    # 시간 컬럼의 표기는 필드가 아니라 **사건 선언**(time_format)이 소유한다.
    assert purchase.column_type(event_ir.TIME_FIELD_SUFFIX) == (
        event_compiler.time_format_data_type(
            catalog.compiler_events["purchase"].time_format
        )
    )


def test_audience_path_declares_its_bindings() -> None:
    """오디언스 경로의 컴파일 컨텍스트가 더 이상 비어 있지 않다(부채 ③).

    비어 있으면 :meth:`AudiencePredicateCompiler._verify_bindings` 가 검사할 것이 없어
    항상 통과한다 — 있는 줄 알았던 안전망이 없는 상태다.
    """
    import audience_runtime

    catalog = audience_runtime.resolve_audience_catalog()
    context = SqlCompilationContext(
        dialect=SqlDialect.TSQL,
        schema_bindings=schema_bindings_from_compiler(
            events=catalog.compiler_events,
            fields=catalog.compiler_fields,
            subject=catalog.subject,
        ),
    )
    assert "purchase" in context.schema_bindings.bindings

    unknown = {
        "type": "exists",
        "relation": {"type": "source", "name": "not_a_declared_event"},
    }
    plan = AudienceLogicalPlanner().create_plan(
        query_pipeline.audience_spec_from_plan_payload(
            {"expression": unknown, "source": "audience_requirement", "receipts": []}
        )
    )
    with pytest.raises(MissingBindingError, match="선언되지 않은 논리 entity"):
        query_pipeline.AudiencePredicateCompiler(
            graph_rag._event_compile_context
        ).compile(plan, context)
