from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .types import StructuredQuery


@dataclass(frozen=True)
class QueryPlannerInput:
    query: str
    structured_query: StructuredQuery | None = None


def call_query_planner(
    create_plan: Callable[..., dict[str, Any]],
    input: QueryPlannerInput,
    **options: Any,
) -> dict[str, Any]:
    return create_plan(input.query, structured_query=input.structured_query, **options)