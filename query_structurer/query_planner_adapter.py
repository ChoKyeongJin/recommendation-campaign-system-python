from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .campaign_plan_v2 import CampaignQueryPlanV2
from .types import StructuredQuery


@dataclass(frozen=True)
class QueryPlannerInput:
    query: str
    query_plan: CampaignQueryPlanV2 | None = None
    # API에서 받은 문자열 전체. ``query``는 정규화/스코프 분리된 planning input일 수 있다.
    raw_query: str | None = None
    # Deprecated compatibility input. New campaign flows pass ``query_plan``.
    structured_query: StructuredQuery | None = None


def call_query_planner(
    create_plan: Callable[..., dict[str, Any]],
    input: QueryPlannerInput,
    **options: Any,
) -> dict[str, Any]:
    if input.raw_query is not None:
        options["raw_query"] = input.raw_query
    if input.query_plan is not None:
        return create_plan(input.query, query_plan_v2=input.query_plan, **options)
    return create_plan(input.query, structured_query=input.structured_query, **options)
