from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .prompt import (
    COMPLEX_QUERY_STRUCTURER_SYSTEM_PROMPT,
    build_campaign_query_plan_v2_user_prompt,
    build_retry_prompt,
    build_structuring_user_prompt,
)
from .campaign_plan_v2 import (
    CampaignQueryPlanV2,
    CampaignQueryPlanValidationError,
    attach_campaign_query_plan_v2_identity,
    build_campaign_query_plan_v2_fallback,
    validate_campaign_query_plan_v2,
)
from .schema import StructuredQueryValidationError, build_fallback, validate_structured_query
from .types import QueryStructurer, QueryStructuringInput, StructuredQuery


Completion = Callable[[list[dict[str, str]]], str]
EventSink = Callable[[str, dict[str, Any]], None]


class LLMQueryStructurer(QueryStructurer):
    def __init__(
        self,
        complete: Completion,
        max_retries: int = 2,
        on_event: EventSink | None = None,
    ) -> None:
        self._complete = complete
        self._max_retries = max_retries
        self._on_event = on_event

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, payload)

    def structure(self, input: QueryStructuringInput) -> StructuredQuery:
        messages = [
            {"role": "system", "content": COMPLEX_QUERY_STRUCTURER_SYSTEM_PROMPT},
            {"role": "user", "content": build_structuring_user_prompt(input)},
        ]
        previous_response = ""

        for attempt in range(self._max_retries + 1):
            previous_response = ""
            try:
                previous_response = self._complete(messages)
                result = validate_structured_query(json.loads(previous_response), query=input.query)
                self._emit(
                    "query_structuring_success",
                    {"attempt": attempt + 1, "structured_query": result.to_dict()},
                )
                return result
            except (json.JSONDecodeError, StructuredQueryValidationError, TypeError, ValueError) as exc:
                error = f"{exc.__class__.__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - a failed provider call must preserve the original query.
                error = f"{exc.__class__.__name__}: {exc}"

            self._emit(
                "query_structuring_attempt_failed",
                {
                    "attempt": attempt + 1,
                    "error": error,
                    "response": previous_response,
                },
            )

            if attempt < self._max_retries:
                messages.extend(
                    [
                        {"role": "assistant", "content": previous_response},
                        {"role": "user", "content": build_retry_prompt(previous_response, error)},
                    ]
                )

        self._emit(
            "query_structuring_fallback",
            {"attempts": self._max_retries + 1, "last_error": error},
        )
        return build_fallback(input.query)


class LLMCampaignQueryPlanStructurer:
    """Build the same CampaignQueryPlanV2 object consumed by the executor."""

    def __init__(
        self,
        complete: Completion,
        max_retries: int = 2,
        on_event: EventSink | None = None,
    ) -> None:
        self._complete = complete
        self._max_retries = max_retries
        self._on_event = on_event

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, payload)

    def structure(self, input: QueryStructuringInput) -> CampaignQueryPlanV2:
        messages = [
            {
                "role": "system",
                "content": (
                    "너는 캠페인 타겟팅 QueryPlan v2 구조화기다. "
                    "실행기가 그대로 소비할 snake_case 필드를 사용하고 JSON 외에는 출력하지 않는다. "
                    "명시되지 않은 타겟 조건이나 혜택은 만들지 않는다."
                ),
            },
            {"role": "user", "content": build_campaign_query_plan_v2_user_prompt(input)},
        ]
        last_error = "unknown"
        for attempt in range(self._max_retries + 1):
            response = ""
            try:
                response = self._complete(messages)
                payload = attach_campaign_query_plan_v2_identity(json.loads(response), input.query)
                result = validate_campaign_query_plan_v2(payload, query=input.query)
                self._emit(
                    "campaign_query_plan_v2_success",
                    {"attempt": attempt + 1, "query_plan": result.to_dict()},
                )
                return result
            except (
                json.JSONDecodeError,
                CampaignQueryPlanValidationError,
                TypeError,
                ValueError,
            ) as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - provider failure is a safe fallback.
                last_error = f"{exc.__class__.__name__}: {exc}"
            self._emit(
                "campaign_query_plan_v2_attempt_failed",
                {"attempt": attempt + 1, "error": last_error, "response": response},
            )
            if attempt < self._max_retries:
                messages.extend(
                    [
                        {"role": "assistant", "content": response},
                        {"role": "user", "content": build_retry_prompt(response, last_error)},
                    ]
                )
        self._emit(
            "campaign_query_plan_v2_fallback",
            {"attempts": self._max_retries + 1, "last_error": last_error},
        )
        return build_campaign_query_plan_v2_fallback(input.query)
