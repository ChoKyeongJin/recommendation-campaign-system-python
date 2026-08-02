from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .prompt import (
    COMPLEX_QUERY_STRUCTURER_SYSTEM_PROMPT,
    build_campaign_query_plan_v4_retry_prompt,
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


def _is_non_retryable_tool_contract_error(exc: Exception) -> bool:
    """Provider rejects the tool declaration before sampling any model output."""
    message = f"{exc.__class__.__name__}: {exc}".casefold()
    return (
        "invalid schema for function" in message
        or "invalid_function_parameters" in message
        or "invalid tools" in message and "schema" in message
    )


def _audience_repair_error(raw: dict[str, Any], enriched: dict[str, Any]) -> str | None:
    """Report application-derived failures without echoing model-authored issues."""
    raw_requirement = raw.get("audience_requirement")
    if not isinstance(raw_requirement, dict):
        return None
    raw_issues = [
        item for item in (raw_requirement.get("issues") or []) if isinstance(item, dict)
    ]
    if raw_requirement.get("expression") is None:
        if any(item.get("code") == "validation_mismatch" for item in raw_issues):
            return (
                "validation_mismatch is application-owned; do not copy validation errors "
                "into issues, and retry the canonical expression"
            )
        has_period = any(
            item.get("kind") in {"date_window", "duration"}
            for item in (enriched.get("literal_bindings") or [])
            if isinstance(item, dict)
        )
        if has_period and any(
            item.get("code") == "missing_argument" and item.get("argument") == "period"
            for item in raw_issues
        ):
            return "period is present in application-owned literal bindings; retry the expression"
        return None
    if not isinstance(raw_requirement.get("expression"), dict):
        return None
    raw_keys = {
        (item.get("code"), item.get("argument"), item.get("message"))
        for item in raw_issues
    }
    requirement = enriched.get("audience_requirement")
    enriched_issues = (
        requirement.get("issues") if isinstance(requirement, dict) else []
    ) or []
    derived = [
        item for item in enriched_issues
        if isinstance(item, dict)
        and (item.get("code"), item.get("argument"), item.get("message")) not in raw_keys
    ]
    if derived:
        details = "; ".join(
            f"{item.get('code')}[{item.get('argument')}]: {item.get('message')}"
            for item in derived
        )
        return "audience expression failed application validation: " + details
    if raw_issues:
        return (
            "a non-null audience expression cannot coexist with issues; discard stale "
            "issues and return either a corrected expression or expression=null"
        )
    return None


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
                    "five fields accepted by the tool schema: intent, campaign_constraints, result_limit, "
                    "audience_requirement, and semantic_plan. audience_requirement.expression is the complete "
                    "Event IR meaning; audience_requirement.issues records missing, ambiguous, unsupported, or "
                    "invalid meaning. semantic_plan carries only the point-in-time or monthly-snapshot member "
                    "attribute conditions that the Event IR algebra cannot state, and stays {\"nodes\": []} "
                    "otherwise. Each condition belongs to exactly one of the two — never both. "
                    "Use only the Event IR algebra and semantic-catalog identifiers supplied in the "
                    "user message. Preserve negation, AND/OR grouping, comparison semantics, aggregation grain, "
                    "and temporal scope. Every semantic atom and issue needs an exact evidence substring with "
                    "zero-based [start,end) offsets into the unchanged query. Trust application-owned literal "
                    "bindings and never invent a duration, threshold, date, identifier, or condition. In "
                    "particular, bare '최근' without a duration means expression=null plus a missing_argument "
                    "issue whose argument is 'period'. Keep campaign objective, channel, offer, and sell-object "
                    "as campaign metadata only; never turn an objective into an audience predicate. Do not emit "
                    "target_user, exclude, semantic_ir, unresolved, event_expression, SQL, or "
                    "physical schema names. If any material audience meaning cannot be represented faithfully, "
                    "set expression to null and report the issue instead of narrowing or guessing."
                ),
            },
            {"role": "user", "content": build_campaign_query_plan_v4_user_prompt(input)},
        ]
        if extra_instruction:
            messages.append({"role": "user", "content": extra_instruction})
        last_error = "unknown"
        attempts_made = 0
        for attempt in range(self._max_retries + 1):
            attempts_made = attempt + 1
            response = ""
            non_retryable = False
            try:
                response = self._complete(messages)
                raw_payload = json.loads(response)
                payload = attach_campaign_query_plan_v4_identity(
                    raw_payload,
                    input.query,
                    current_date=input.context.current_date,
                )
                repair_error = _audience_repair_error(raw_payload, payload)
                if repair_error:
                    raise CampaignQueryPlanValidationError(repair_error)
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
                non_retryable = _is_non_retryable_tool_contract_error(exc)
            self._emit(
                "campaign_query_plan_v4_attempt_failed",
                {"attempt": attempt + 1, "error": last_error, "response": response},
            )
            if non_retryable:
                break
            if attempt < self._max_retries:
                messages.extend(
                    [
                        {"role": "assistant", "content": response},
                        {
                            "role": "user",
                            "content": build_campaign_query_plan_v4_retry_prompt(
                                response, last_error
                            ),
                        },
                    ]
                )
        self._emit(
            "campaign_query_plan_v4_fallback",
            {"attempts": attempts_made, "last_error": last_error},
        )
        return build_campaign_query_plan_v4_fallback(
            input.query, current_date=input.context.current_date
        )
