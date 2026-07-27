from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .prompt import COMPLEX_QUERY_STRUCTURER_SYSTEM_PROMPT, build_retry_prompt, build_structuring_user_prompt
from .schema import StructuredQueryValidationError, build_fallback, validate_structured_query
from .types import QueryStructurer, QueryStructuringInput, StructuredQuery


Completion = Callable[[list[dict[str, str]]], str]


class LLMQueryStructurer(QueryStructurer):
    def __init__(self, complete: Completion, max_retries: int = 2) -> None:
        self._complete = complete
        self._max_retries = max_retries

    def structure(self, input: QueryStructuringInput) -> StructuredQuery:
        messages = [
            {"role": "system", "content": COMPLEX_QUERY_STRUCTURER_SYSTEM_PROMPT},
            {"role": "user", "content": build_structuring_user_prompt(input)},
        ]
        previous_response = ""

        for attempt in range(self._max_retries + 1):
            try:
                previous_response = self._complete(messages)
                return validate_structured_query(json.loads(previous_response), query=input.query)
            except (json.JSONDecodeError, StructuredQueryValidationError, TypeError, ValueError) as exc:
                error = f"{exc.__class__.__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 - a failed provider call must preserve the original query.
                error = f"{exc.__class__.__name__}: {exc}"

            if attempt < self._max_retries:
                messages.extend(
                    [
                        {"role": "assistant", "content": previous_response},
                        {"role": "user", "content": build_retry_prompt(previous_response, error)},
                    ]
                )

        return build_fallback(input.query)