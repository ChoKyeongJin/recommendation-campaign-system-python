"""통합 — 사용자 문장 → 요구 → 사양 → 계획 → SQL 을 한 번에 통과시킨다.

네 시나리오는 서로 다른 결말을 검증한다:

    ① 결제 실패 고객   기본값 적용 + 집계 조건(HAVING)
    ② 미로그인 사용자  부정 존재(NOT EXISTS) + 전체 집계
    ③ 상위 10명       정렬·상한
    ④ 중앙값          **SQL 을 만들지 않고** unsupported 로 끝난다
"""

from __future__ import annotations

from datetime import UTC, datetime

from query_pipeline_fixtures import clock, id_factory, resolution_context, run, sql_context

from query_pipeline.compiler.sql_compiler import LogicalPlanSqlCompiler
from query_pipeline.event_query.receipts import ReceiptAction
from query_pipeline.pipeline.query_pipeline import (
    EVENT_REQUIREMENT_RESOLVED,
    EVENT_SQL_COMPILED,
    QueryExecutionContext,
    QueryPipeline,
)
from query_pipeline.planning.logical_planner import AudienceLogicalPlanner
from query_pipeline.requirement.issues import IssueKind
from query_pipeline.requirement.models import (
    AudienceRequirement,
    IntentKind,
    MetricKind,
    ProposedExpression,
    RequirementConstraint,
    RequirementContext,
    RequirementIntent,
    RequirementMetric,
    RequirementOperator,
    RequirementOrder,
    RequirementOutput,
    RequirementReference,
    RequirementSource,
    SortDirection,
)
from query_pipeline.requirement.parser import RequirementParser
from query_pipeline.requirement.resolver import DefaultRequirementResolver

NOW = datetime(2026, 8, 3, tzinfo=UTC)


class _StaticParser(RequirementParser):
    """LLM 자리를 고정 요구로 대체한다(모델 출력에 테스트를 매달지 않는다)."""

    def __init__(self, requirement: AudienceRequirement) -> None:
        self._requirement = requirement

    async def parse(self, user_text: str) -> AudienceRequirement:
        assert user_text == self._requirement.source.text
        return self._requirement


def _pipeline(requirement: AudienceRequirement, events: list[tuple[str, dict]] | None = None):
    return QueryPipeline(
        parser=_StaticParser(requirement),
        resolver=DefaultRequirementResolver(clock=clock(), id_factory=id_factory()),
        logical_planner=AudienceLogicalPlanner(),
        sql_compiler=LogicalPlanSqlCompiler(),
        resolution_context=resolution_context(),
        sql_context=sql_context(),
        on_event=(lambda name, payload: events.append((name, dict(payload))))
        if events is not None
        else None,
    )


def _requirement(
    text: str,
    *,
    intent: IntentKind = IntentKind.FIND,
    target: str | None = None,
    constraints: tuple[RequirementConstraint, ...] = (),
    expression: ProposedExpression | None = None,
    output: RequirementOutput | None = None,
    issues: tuple = (),
) -> AudienceRequirement:
    return AudienceRequirement(
        id="req-1",
        version="1",
        intent=RequirementIntent(
            kind=intent,
            target=RequirementReference(name=target) if target else None,
        ),
        constraints=constraints,
        expression=expression,
        output=output,
        issues=issues,
        context=RequirementContext(timezone="Asia/Seoul", locale="ko-KR"),
        source=RequirementSource(text=text),
        created_at=NOW,
    )


# ── ① 지난달 결제를 많이 실패한 고객 ────────────────────────────────────────────


def test_failed_payments_last_month_becomes_parameterized_sql() -> None:
    from query_pipeline.requirement.issues import (
        IssueResolution,
        IssueSeverity,
        RequirementIssue,
        ResolutionKind,
    )
    from query_pipeline.requirement.models import (
        AmbiguousRequirementValue,
        InferredRequirementValue,
        ResolvedRequirementValue,
    )

    text = "지난달 결제를 많이 실패한 고객을 보여줘"
    requirement = _requirement(
        text,
        target="customer",
        constraints=(
            RequirementConstraint(
                id="constraint-event",
                field=RequirementReference(name="event_type"),
                operator=RequirementOperator.EQ,
                value=ResolvedRequirementValue(value="payment.failed"),
            ),
            RequirementConstraint(
                id="constraint-time",
                field=RequirementReference(name="occurred_at"),
                operator=RequirementOperator.BETWEEN,
                value=InferredRequirementValue(value="지난달", confidence=0.95),
            ),
            RequirementConstraint(
                id="constraint-count",
                field=RequirementReference(name="failure_count"),
                operator=RequirementOperator.GTE,
                value=AmbiguousRequirementValue(candidates=(3, 5, 10)),
            ),
        ),
        issues=(
            RequirementIssue(
                id="issue-count",
                kind=IssueKind.AMBIGUOUS,
                severity=IssueSeverity.ERROR,
                path="$.constraints[2].value",
                message="'많이'의 기준이 명확하지 않습니다.",
                candidates=(3, 5, 10),
                resolution=IssueResolution(
                    kind=ResolutionKind.USE_DEFAULT, default_value=5
                ),
            ),
        ),
    )
    events: list[tuple[str, dict]] = []
    result = run(
        _pipeline(requirement, events).execute(
            text, QueryExecutionContext(correlation_id="corr-1")
        )
    )
    assert result.status == "ready"

    sql = result.compiled_sql.sql
    assert 'FROM "payment_events" AS "t1"' in sql
    assert 'COUNT(*) AS "failure_count"' in sql
    assert 'GROUP BY "t1"."customer_id"' in sql
    assert "HAVING COUNT(*) >= :count_value" in sql
    parameters = result.compiled_sql.parameter_map
    assert parameters["event_type"] == "payment.failed"
    assert str(parameters["occurred_at_start"]) == "2026-07-01"
    assert str(parameters["occurred_at_end"]) == "2026-08-01"
    assert parameters["count_value"] == 5
    # 값은 하나도 문장에 인라인되지 않았다.
    assert "payment.failed" not in sql and "2026-07-01" not in sql

    spec = result.event_query_spec
    assert any(
        receipt.action is ReceiptAction.APPLIED_DEFAULT for receipt in spec.receipts
    )
    assert any(
        receipt.action is ReceiptAction.NORMALIZED_VALUE for receipt in spec.receipts
    )
    names = [name for name, _ in events]
    assert EVENT_REQUIREMENT_RESOLVED in names and EVENT_SQL_COMPILED in names
    # 기본값은 원문을 로그에 싣지 않는다.
    assert all("sql" not in payload for _name, payload in events)


# ── ② 최근 7일 동안 로그인하지 않은 사용자 수 ───────────────────────────────────


def test_users_without_recent_login_counts_with_not_exists() -> None:
    text = "최근 7일 동안 로그인하지 않은 사용자 수를 알려줘"
    proposed = ProposedExpression(
        payload={
            "type": "not",
            "operand": {
                "type": "exists",
                "relation": {
                    "type": "filter",
                    "relation": {"type": "source", "name": "login_events"},
                    "where": {
                        "type": "time_filter",
                        "field": {"type": "field", "name": "login_events.occurred_at"},
                        "window": {
                            "type": "interval",
                            "start": "2026-07-27",
                            "end_exclusive": "2026-08-03",
                        },
                    },
                },
                "evidence": {"text": "최근 7일 동안 로그인", "start": 0, "end": 12},
            },
        }
    )
    requirement = _requirement(
        text,
        intent=IntentKind.COUNT,
        target="users",
        expression=proposed,
        output=RequirementOutput(
            metrics=(RequirementMetric(kind=MetricKind.COUNT, alias="user_count"),)
        ),
    )
    result = run(_pipeline(requirement).execute(text))
    assert result.status == "ready"
    sql = result.compiled_sql.sql
    assert 'COUNT(*) AS "user_count"' in sql
    assert 'FROM "users" AS "t1"' in sql
    assert "NOT (EXISTS (SELECT 1 FROM \"login_events\"" in sql
    assert '"t2"."user_id" = "t1"."id"' in sql


# ── ③ 이번 달 주문 금액 상위 10명 ───────────────────────────────────────────────


def test_top_ten_customers_by_order_amount_this_month() -> None:
    from query_pipeline.requirement.models import InferredRequirementValue

    text = "이번 달 주문 금액 상위 10명의 고객을 보여줘"
    requirement = _requirement(
        text,
        target="customers",
        constraints=(
            RequirementConstraint(
                id="constraint-time",
                field=RequirementReference(name="ordered_at"),
                operator=RequirementOperator.BETWEEN,
                value=InferredRequirementValue(value="이번 달", confidence=0.9),
            ),
        ),
        output=RequirementOutput(
            dimensions=(RequirementReference(name="orders.customer_id"),),
            metrics=(
                RequirementMetric(
                    kind=MetricKind.SUM,
                    target=RequirementReference(name="orders.amount"),
                    alias="order_amount",
                ),
            ),
            order_by=(
                RequirementOrder(
                    target=RequirementReference(name="order_amount"),
                    direction=SortDirection.DESC,
                ),
            ),
            limit=10,
        ),
    )
    result = run(_pipeline(requirement).execute(text))
    assert result.status == "ready"
    sql = result.compiled_sql.sql
    assert 'SUM("t1"."amount") AS "order_amount"' in sql
    assert 'GROUP BY "t1"."customer_id"' in sql
    assert 'ORDER BY SUM("t1"."amount") DESC' in sql
    assert sql.rstrip().endswith("LIMIT 10")
    parameters = result.compiled_sql.parameter_map
    assert str(parameters["ordered_at_start"]) == "2026-08-01"
    assert str(parameters["ordered_at_end"]) == "2026-09-01"


# ── ④ 지원하지 않는 중앙값 집계 ─────────────────────────────────────────────────


def test_unsupported_median_aggregation_returns_issue_without_sql() -> None:
    from query_pipeline.requirement.models import ResolvedRequirementValue

    text = "지원하지 않는 중앙값 집계를 실행해줘"
    requirement = _requirement(
        text,
        target="customers",
        constraints=(
            RequirementConstraint(
                id="constraint-event",
                field=RequirementReference(name="event_type"),
                operator=RequirementOperator.EQ,
                value=ResolvedRequirementValue(value="payment.failed"),
            ),
        ),
        output=RequirementOutput(
            metrics=(
                RequirementMetric(
                    kind=MetricKind.MEDIAN,
                    target=RequirementReference(name="orders.amount"),
                    alias="median_amount",
                ),
            )
        ),
    )
    result = run(_pipeline(requirement).execute(text))
    assert result.status == "needs_resolution"
    assert not hasattr(result, "compiled_sql")
    assert [issue.kind for issue in result.issues] == [IssueKind.UNSUPPORTED]
    assert "median" in result.issues[0].message
    assert result.failed_stage == "requirement_resolution"


def test_invalid_llm_output_stops_before_sql() -> None:
    from query_pipeline.requirement.parser import DraftRequirementParser

    text = "여성 회원을 찾아줘"
    parser = DraftRequirementParser(
        lambda _text: "{not json", clock=clock(), id_factory=id_factory()
    )
    pipeline = QueryPipeline(
        parser=parser,
        resolver=DefaultRequirementResolver(clock=clock(), id_factory=id_factory()),
        logical_planner=AudienceLogicalPlanner(),
        sql_compiler=LogicalPlanSqlCompiler(),
        resolution_context=resolution_context(),
        sql_context=sql_context(),
    )
    result = run(pipeline.execute(text))
    assert result.status == "needs_resolution"
    assert result.issues[0].kind is IssueKind.INVALID
