"""query_pipeline 테스트가 공유하는 선언 — 논리 도메인 하나와 그 물리 바인딩.

도메인을 테스트 파일마다 다시 적으면 "스키마가 바뀌었는데 한 파일만 고쳤다"가 생긴다.
여기 한 곳에서 선언하고, 각 테스트는 자기 관심사만 조립한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any, TypeVar

from query_pipeline.base import FixedClock, SequentialIdFactory
from query_pipeline.compiler.capability import (
    DeclaredCapabilityProfile,
    generic_sql_capability_profile,
)
from query_pipeline.compiler.models import (
    SchemaBinding,
    SchemaBindings,
    SqlCompilationContext,
    SqlDialect,
)
from query_pipeline.event_query.expressions import AggregateFunction
from query_pipeline.requirement.resolver import (
    FieldResolution,
    MetricResolution,
    RequirementResolutionContext,
    StaticResolutionPolicy,
    StaticSchemaRegistry,
)

T = TypeVar("T")

# 기준 시각 고정 — '지난달'/'이번 달'이 달력 경계에서 흔들리지 않게 한다.
FIXED_NOW = datetime(2026, 8, 3, 4, 30, tzinfo=UTC)  # KST 2026-08-03 13:30
SEOUL = "Asia/Seoul"


def run(coroutine: Coroutine[Any, Any, T]) -> T:
    """pytest-asyncio 를 새 의존성으로 들이지 않기 위한 동기 실행기."""
    return asyncio.run(coroutine)


def schema_registry() -> StaticSchemaRegistry:
    return StaticSchemaRegistry(
        entities={
            "customer": "payment_events",
            "customers": "customers",
            "payment_events": "payment_events",
            "login_events": "login_events",
            "orders": "orders",
            "user": "users",
            "users": "users",
        },
        fields={
            "customer_id": FieldResolution(entity="payment_events", attribute="customer_id"),
            "event_type": FieldResolution(entity="payment_events", attribute="event_type"),
            "occurred_at": FieldResolution(entity="payment_events", attribute="occurred_at"),
            "payment_events.customer_id": FieldResolution(
                entity="payment_events", attribute="customer_id"
            ),
            "payment_events.event_type": FieldResolution(
                entity="payment_events", attribute="event_type"
            ),
            "payment_events.occurred_at": FieldResolution(
                entity="payment_events", attribute="occurred_at"
            ),
            "ordered_at": FieldResolution(entity="orders", attribute="ordered_at"),
            "order_customer_id": FieldResolution(entity="orders", attribute="customer_id"),
            "amount": FieldResolution(entity="orders", attribute="amount"),
            "orders.ordered_at": FieldResolution(entity="orders", attribute="ordered_at"),
            "orders.customer_id": FieldResolution(entity="orders", attribute="customer_id"),
            "orders.amount": FieldResolution(entity="orders", attribute="amount"),
        },
        metrics={
            "failure_count": MetricResolution(
                logical_name="failure_count",
                function=AggregateFunction.COUNT,
                entity="payment_events",
                grain="customer_id",
            ),
        },
    )


def resolution_context(
    *,
    capability_profile: DeclaredCapabilityProfile | None = None,
    defaults: dict[str, Any] | None = None,
) -> RequirementResolutionContext:
    return RequirementResolutionContext(
        timezone=SEOUL,
        locale="ko-KR",
        schema_registry=schema_registry(),
        capability_profile=capability_profile or generic_sql_capability_profile(),
        policy=StaticResolutionPolicy(defaults or {}),
    )


def schema_bindings() -> SchemaBindings:
    return SchemaBindings(
        bindings={
            "payment_events": SchemaBinding(
                entity="payment_events",
                table="payment_events",
                attributes={
                    "customer_id": "customer_id",
                    "event_type": "event_type",
                    "occurred_at": "occurred_at",
                    "amount": "amount",
                },
                data_type={"occurred_at": "date", "amount": "number"},
                key="id",
                correlations={"customers": "customer_id"},
            ),
            "login_events": SchemaBinding(
                entity="login_events",
                table="login_events",
                attributes={"user_id": "user_id", "occurred_at": "occurred_at"},
                data_type={"occurred_at": "date"},
                key="id",
                correlations={"users": "user_id"},
            ),
            "orders": SchemaBinding(
                entity="orders",
                table="orders",
                attributes={
                    "customer_id": "customer_id",
                    "ordered_at": "ordered_at",
                    "amount": "amount",
                },
                # 실CRM 관례(문자열 'YYYYMMDD')를 한 entity 에 심어 둔다 — 롤링 창의 실행
                # 시점 렌더가 저장 표기마다 갈리는 것을 이 도메인 하나로 함께 관측한다.
                data_type={"ordered_at": "date_char8", "amount": "number"},
                key="id",
                correlations={"customers": "customer_id"},
            ),
            "users": SchemaBinding(
                entity="users", table="users", attributes={"id": "id"}, key="id"
            ),
            "customers": SchemaBinding(
                entity="customers", table="customers", attributes={"id": "id"}, key="id"
            ),
        }
    )


def sql_context(
    dialect: SqlDialect = SqlDialect.POSTGRESQL,
) -> SqlCompilationContext:
    return SqlCompilationContext(dialect=dialect, schema_bindings=schema_bindings())


def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


def id_factory() -> SequentialIdFactory:
    return SequentialIdFactory()


__all__ = [
    "FIXED_NOW",
    "SEOUL",
    "clock",
    "id_factory",
    "resolution_context",
    "run",
    "schema_bindings",
    "schema_registry",
    "sql_context",
]
