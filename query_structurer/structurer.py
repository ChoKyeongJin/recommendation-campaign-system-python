from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .prompt import (
    COMPLEX_QUERY_STRUCTURER_SYSTEM_PROMPT,
    build_campaign_query_plan_v4_user_prompt,
    build_retry_prompt,
    build_structuring_user_prompt,
)
from .campaign_plan_v4 import (
    CampaignQueryPlanV4,
    CampaignQueryPlanValidationError,
    attach_campaign_query_plan_v4_identity,
    build_campaign_query_plan_v4_fallback,
    validate_campaign_query_plan_v4,
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


class LLMCampaignQueryPlanV4Structurer:
    """Extract one evidence-bound audience requirement plus campaign metadata.

    The model emits meaning in the canonical Event IR contract. The application
    validates that requirement and owns all downstream execution projections.
    """

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

    def structure(
        self, input: QueryStructuringInput, extra_instruction: str | None = None
    ) -> CampaignQueryPlanV4:
        # A retry hint may request a corrected canonical requirement. The query
        # itself remains unchanged so evidence offsets keep the same coordinate system.
        messages = [
            {
                "role": "system",
                "content": (
                    "You structure campaign requests into one canonical audience contract. Return only the "
                    "four fields accepted by the tool schema: intent, campaign_constraints, result_limit, "
                    "and audience_requirement. audience_requirement.expression is the complete Event IR "
                    "meaning; audience_requirement.issues records missing, ambiguous, unsupported, or invalid "
                    "meaning. Use only the Event IR algebra and semantic-catalog identifiers supplied in the "
                    "user message. Preserve negation, AND/OR grouping, comparison semantics, aggregation grain, "
                    "and temporal scope. Every semantic atom and issue needs an exact evidence substring with "
                    "zero-based [start,end) offsets into the unchanged query. Trust application-owned literal "
                    "bindings and never invent a duration, threshold, date, identifier, or condition. In "
                    "particular, bare '최근' without a duration means expression=null plus a missing_argument "
                    "issue whose argument is 'period'. Keep campaign objective, channel, offer, and sell-object "
                    "as campaign metadata only; never turn an objective into an audience predicate. Do not emit "
                    "target_user, exclude, semantic_plan, semantic_ir, unresolved, event_expression, SQL, or "
                    "physical schema names. If any material audience meaning cannot be represented faithfully, "
                    "set expression to null and report the issue instead of narrowing or guessing."
                ),
            },
            {"role": "user", "content": build_campaign_query_plan_v4_user_prompt(input)},
        ]
        if extra_instruction:
            messages.append({"role": "user", "content": extra_instruction})
        last_error = "unknown"
        for attempt in range(self._max_retries + 1):
            response = ""
            try:
                response = self._complete(messages)
                payload = attach_campaign_query_plan_v4_identity(
                    json.loads(response),
                    input.query,
                    current_date=input.context.current_date,
                )
                result = validate_campaign_query_plan_v4(
                    payload, query=input.query, require_semantic=True
                )
                self._emit(
                    "campaign_query_plan_v4_success",
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
            except Exception as exc:  # noqa: BLE001 - provider failure uses legacy fallback.
                last_error = f"{exc.__class__.__name__}: {exc}"
            self._emit(
                "campaign_query_plan_v4_attempt_failed",
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
            "campaign_query_plan_v4_fallback",
            {"attempts": self._max_retries + 1, "last_error": last_error},
        )
        return build_campaign_query_plan_v4_fallback(
            input.query, current_date=input.context.current_date
        )
