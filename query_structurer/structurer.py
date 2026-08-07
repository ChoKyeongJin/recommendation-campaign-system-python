from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from jsonschema import Draft202012Validator

import execution_assets

from .campaign_plan_v4 import (
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
    CampaignQueryPlanV4,
    CampaignQueryPlanValidationError,
    attach_campaign_query_plan_v4_identity,
    build_campaign_query_plan_v4_fallback,
    validate_campaign_query_plan_v4,
)
from .prompt import (
    COMPLEX_QUERY_STRUCTURER_SYSTEM_PROMPT,
    build_campaign_query_plan_v4_retry_prompt,
    build_campaign_query_plan_v4_user_prompt,
    build_retry_prompt,
    build_structuring_user_prompt,
)
from .schema import StructuredQueryValidationError, build_fallback, validate_structured_query
from .types import QueryStructurer, QueryStructuringInput, StructuredQuery

Completion = Callable[[list[dict[str, str]]], str]
EventSink = Callable[[str, dict[str, Any]], None]

_CAMPAIGN_TOOL_PAYLOAD_VALIDATOR = Draft202012Validator(
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA
)


def _decode_campaign_query_plan_v4_response(
    response: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Decode one strict tool object, repairing one surplus closing brace.

    A sampled response can close an object one brace too early, which makes the
    document end before the remaining keys and json report ``Extra data``.  The
    observed shapes differ only in *where* the surplus brace sits: before
    ``audience_requirement.issues`` in one sample, and before an ``Exists``
    node's own ``evidence`` in another (which drops a correct expression on the
    floor).  Anchoring the repair on a particular neighbouring key therefore
    fixes one sample and not the defect.

    So the candidate set is every closing brace, and the *acceptance* rule
    carries the safety instead of the search: the repaired document must parse,
    the whole object must validate against the strict provider schema, and the
    repair must be unique.  Any ambiguity or any other malformed shape stays a
    retryable parse failure — a second plausible reading is never guessed at.
    """

    parse_error: json.JSONDecodeError
    try:
        payload = json.loads(response)
        return payload, None
    except json.JSONDecodeError as exc:
        if exc.msg != "Extra data":
            raise
        parse_error = exc

    candidates: dict[str, tuple[dict[str, Any], int]] = {}
    for index, character in enumerate(response):
        if character != "}":
            continue
        repaired = response[:index] + response[index + 1:]
        try:
            candidate = json.loads(repaired)
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict) or not _CAMPAIGN_TOOL_PAYLOAD_VALIDATOR.is_valid(
            candidate
        ):
            continue
        fingerprint = json.dumps(
            candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        candidates[fingerprint] = (candidate, index)

    if len(candidates) != 1:
        raise parse_error
    candidate, index = next(iter(candidates.values()))
    return candidate, {
        "kind": "remove_extra_closing_brace",
        "removed_index": index,
    }


def _is_non_retryable_tool_contract_error(exc: Exception) -> bool:
    """Provider rejects the tool declaration before sampling any model output."""
    message = f"{exc.__class__.__name__}: {exc}".casefold()
    return (
        "invalid schema for function" in message
        or "invalid_function_parameters" in message
        or "invalid tools" in message and "schema" in message
    )


# 모델이 지목한 심볼에서 카탈로그 이름을 떼어낼 때 쓰는 경계. 자기가 만든 접두어를 붙여
# 오기 때문에(`distinct_count_of_cart.product_id`) 정확 일치만 보면 반박을 놓친다.
_SYMBOL_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def _registered_canonical_symbol(symbol: Any) -> str | None:
    """이 이름이 지목하는 canonical 카탈로그 심볼(소스·필드·지표). 없으면 None.

    정확 일치가 먼저다. 실패하면 이름 안의 **점 있는 토큰**만 다시 본다 — 모델은 카탈로그
    심볼에 자기 설명을 접두어로 붙여 오는데(실측 2026-08-06:
    ``distinct_count_of_cart.product_id``, 안쪽 ``cart.product_id`` 는 등록된 필드),
    그때도 주장의 대상은 여전히 그 등록된 심볼이다. 점이 없는 토큰은 보지 않는다 —
    ``count``·``distinct`` 같은 일반어가 우연히 카탈로그 이름과 겹치는 것을 막는다.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    import audience_runtime  # 지연 import — 구조화기는 카탈로그 로딩을 강제하지 않는다

    try:
        catalog = audience_runtime.resolve_audience_catalog()
    except Exception:  # noqa: BLE001 — 카탈로그를 못 읽으면 반박하지 않는다(추측 금지).
        return None

    def registered(name: str) -> bool:
        return name in catalog.sources or name in catalog.fields or name in catalog.metrics

    name = symbol.strip()
    if registered(name):
        return name
    for token in _SYMBOL_TOKEN_RE.findall(name):
        if "." not in token:
            continue
        # 접두어는 왼쪽에서 한 마디씩 벗긴다('distinct_count_of_cart.product_id' → 'cart.product_id').
        parts = token.split(".")
        for start in range(len(parts) - 1):
            candidate = ".".join(parts[start:])
            if registered(candidate):
                return candidate
            head, _, tail = parts[start].rpartition("_")
            if not head or not tail:
                continue
            stripped = ".".join((tail, *parts[start + 1:]))
            if registered(stripped):
                return stripped
    return None


def _application_supported_obligations(query: Any) -> tuple[Any, ...]:
    """원문에서 애플리케이션이 계산한 의무 중 **canonical 컴파일러가 방면할 수 있는** 것들.

    판정과 종류 선언의 소유자는 영수증을 발행하는 쪽(:mod:`canonical_audience_claims`)이다 —
    여기서 그 목록을 다시 적으면 반박과 방면이 서로 다른 답을 쓰게 된다. 이 함수가 하는 일은
    **카탈로그를 못 읽는 상황을 반박하지 않음으로 접는 것**뿐이다.
    """
    import canonical_audience_claims  # 지연 import — 구조화기는 카탈로그 로딩을 강제하지 않는다

    try:
        return canonical_audience_claims.supported_obligations_for_query(query)
    except Exception:  # noqa: BLE001 — 의무를 못 읽으면 반박하지 않는다(추측 금지).
        return ()


def _lowering_plans(query: Any) -> tuple[Any, ...]:
    """원문에서 **실제로 낮출 수 있다고 증명된** 계획들. 증명은 :mod:`lowering_planner` 가 한다.

    여기서 목록을 다시 적지 않는 이유는 :func:`_application_supported_obligations` 와 같다 —
    판정의 소유자는 하나여야 재시도를 거는 조건과 미지원으로 닫는 조건이 갈라지지 않는다.
    """
    import lowering_planner  # 지연 import — 구조화기는 카탈로그 로딩을 강제하지 않는다

    if not isinstance(query, str) or not query.strip():
        return ()
    try:
        return lowering_planner.plans_for_query(query)
    except Exception:  # noqa: BLE001 — 계획을 못 세우면 반박하지 않는다(추측 금지).
        return ()


def _audience_repair_error(raw: dict[str, Any], enriched: dict[str, Any]) -> str | None:
    """Report application-derived failures without echoing model-authored issues."""
    raw_requirement = raw.get("audience_requirement")
    if not isinstance(raw_requirement, dict):
        return None
    raw_issues = [
        item for item in (raw_requirement.get("issues") or []) if isinstance(item, dict)
    ]
    enriched_requirement = enriched.get("audience_requirement")
    if (
        raw_requirement.get("expression") is None
        and isinstance(enriched_requirement, dict)
        and isinstance(enriched_requirement.get("expression"), dict)
        and not enriched_requirement.get("issues")
    ):
        # 애플리케이션이 그 자리를 **이미 낮췄고** 신고도 전부 방면됐다(합성 갈래). 여기서
        # 재방출을 요구하면 검증을 통과한 표현을 버리고 모델에게 같은 것을 다시 그리라고
        # 시키는 것이라, 예산만 태우고 같은 요청이 회차마다 다른 귀결로 끝난다. 반박은
        # 표현이 서지 않았을 때의 수단이지, 이미 선 표현을 되돌리는 수단이 아니다.
        return None
    if raw_requirement.get("expression") is None:
        # **미지원 선언은 가설이지 판정이 아니다.** 애플리케이션이 이미 선언해 둔 것을 못 한다는
        # 주장은 스스로 반박된다. 반박의 축은 둘이고, 각각 다른 실측에서 나왔다.
        #
        #   심볼 축 (2026-08-03) '여성 회원을 찾아줘' → argument='subject.gender'
        #                        "The compiler cannot represent direct profile field comparison…"
        #                        `subject.gender` 는 카탈로그 필드다.
        #   계산 축 (2026-08-06) '장바구니에 서로 다른 상품을 3개 이상 담아둔 회원'
        #                        → argument='distinct_products' / "…count distinct…표현 불가"
        #                        `aggregate.count_distinct` 는 IR 이 선언한 capability 이고,
        #                        컴파일러가 그 자리에서 HAVING COUNT(DISTINCT …) 를 만든다.
        #   의무 축 (2026-08-07) '작년에 가장 많이 팔린 상품 5개 중 2개 이상 구매한 고객'
        #                        → argument='ranked_set_cardinality' / "…지원하지 않습니다"
        #                        그 구간은 애플리케이션이 이미 `ranked_entity_set` 의무로
        #                        계산해 둔 자리이고, canonical 컴파일러가 그것을 방면한다.
        #
        #   lowering 축 (2026-08-07) '2026년 2월과 3월의 구매금액이 증가한 회원'
        #                        → argument 가 회차마다 달랐다(같은 원문 12회에 6가지 이름:
        #                        `metric_transition`·`month_to_month_change`·
        #                        `temporal_comparison_between_monthly_metrics` …).
        #                        그 요구는 `aggregate.scalar` 둘과 비교 하나로 컴파일된다 —
        #                        12회 중 3회는 실제로 정확한 SQL 이 나왔다.
        #
        # 앞의 두 축은 모델이 **이름**을 어떻게 적었는지에 걸린다. 뒤의 둘은 이름을 보지 않고
        # **좌표**를 본다 — 그래서 새 표면어가 생겨도 목록을 늘릴 필요가 없다. 그중 lowering
        # 축은 종류 목록조차 보지 않고 표현을 실제로 만들어 컴파일해 보므로 가장 강하다.
        # 넷 다 종결이 아니라 재시도 사유다.
        import canonical_audience_claims  # 지연 import — 카탈로그 로딩을 강제하지 않는다
        import semantic_requirements

        query = enriched.get("original_query")
        obligations = _application_supported_obligations(query)
        plans = _lowering_plans(query)
        refuted: list[str] = []
        for item in raw_issues:
            if item.get("code") != "unsupported_semantics":
                continue
            # **가장 강한 반박이 먼저다.** 아래 세 축은 모델이 무엇을 적었는지(이름·계산 종류)
            # 또는 의무 종류 allowlist 에 걸리지만, 이 축은 그 자리의 canonical 표현을 실제로
            # 만들어 컴파일해 본 결과다 — 낮출 수 있다면 그 신고는 종류를 따질 것도 없이 틀렸다.
            plan = next(
                (
                    candidate
                    for candidate in plans
                    if semantic_requirements.spans_overlap(
                        item.get("evidence"), candidate.obligation.source_span
                    )
                ),
                None,
            )
            if plan is not None:
                # capabilities 가 비는 형상이 있다(기본 대수만 쓰는 낮춤). 그때 빈 괄호를
                # 붙이면 "선언된 primitive 가 없다"로 읽혀 반박이 스스로를 부정한다.
                named = ", ".join(sorted(plan.capabilities))
                refuted.append(
                    f"'{plan.obligation.source_text}' lowers to declared execution primitives"
                    + (f" ({named})" if named else "")
                )
                continue
            symbol = _registered_canonical_symbol(item.get("argument"))
            if symbol is not None:
                refuted.append(f"{symbol} is registered in the semantic catalog")
                continue
            capability = execution_assets.declared_capability_in_claim(
                item.get("argument"), item.get("message")
            )
            if capability is not None:
                refuted.append(f"{capability} is a declared Event IR capability")
                continue
            obligation = canonical_audience_claims.obligation_conflicting_with_claim(
                item, obligations
            )
            if obligation is not None:
                refuted.append(
                    f"'{obligation.source_text}' is an application-owned "
                    f"{semantic_requirements.obligation_kind(obligation)} obligation that the "
                    "canonical compiler discharges"
                )
        if refuted:
            return (
                f"{'; '.join(sorted(set(refuted)))}; capability is decided by the application, "
                "not by you — emit the canonical expression instead of an unsupported_semantics "
                "issue"
            )
        if any(item.get("code") == "validation_mismatch" for item in raw_issues):
            return (
                "validation_mismatch is application-owned; do not copy validation errors "
                "into issues, and retry the canonical expression"
            )
        # 예전에는 여기서 `argument == "period"` + date_window/duration 만 아는 손코딩 특례가
        # 재방출을 결정했다. 종류 하나만 알아서 percentage(#3)를 놓쳤고, **스팬을 보지 않아**
        # 다른 절의 '3개월' 때문에 진짜 결핍인 맨 '최근'(#2)까지 재방출로 보냈다.
        # 이제 원인은 결정론으로 계산돼 semantic_ir 에 실려 온다 — 여기서는 읽기만 한다.
        causes = (enriched.get("semantic_ir") or {}).get("missing_field_causes") or []
        omitted = [
            record for record in causes
            if isinstance(record, dict) and record.get("cause") == "model_omission"
        ]
        if omitted:
            fields = ", ".join(sorted({str(record.get("field")) for record in omitted}))
            return (
                f"{fields} is present in application-owned literal bindings; "
                "retry the expression"
            )
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
                    "four fields accepted by the tool schema: intent, campaign_constraints, result_limit, "
                    "and audience_requirement. audience_requirement.expression is the complete "
                    "Event IR meaning; audience_requirement.issues records missing, ambiguous, unsupported, or "
                    "invalid meaning. There is no second audience surface: a condition the algebra cannot "
                    "state faithfully becomes an issue, never an approximation. "
                    "Use only the Event IR algebra and semantic-catalog identifiers supplied in the "
                    "user message. Preserve negation, AND/OR grouping, comparison semantics, aggregation grain, "
                    "and temporal scope. Every semantic atom and issue needs an exact evidence substring with "
                    "zero-based [start,end) offsets into the unchanged query. Trust application-owned literal "
                    "bindings and never invent a duration, threshold, date, identifier, or condition. In "
                    "an explicit '<date> 기준 최근 N일' request, emit the literal-owned recent period as a "
                    "RollingWindow; the application alone pins that rolling window to the stated date. Do not "
                    "turn the as-of date into a second independent event interval. In "
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
        # 재시도를 소진했을 때 쓸 **정직한 종결**. 애플리케이션이 "이 의미는 낼 수 있다"고
        # 판정해 둔 경우에만 채워진다 — 그 외의 반박(심볼·capability 축)은 예전처럼 폴백으로
        # 간다. 이 구분이 없으면 모델이 쓴 미지원 판정이 그대로 사용자에게 도달한다.
        emission_failure: dict[str, Any] | None = None
        for attempt in range(self._max_retries + 1):
            attempts_made = attempt + 1
            response = ""
            non_retryable = False
            try:
                response = self._complete(messages)
                raw_payload, syntax_repair = _decode_campaign_query_plan_v4_response(
                    response
                )
                if syntax_repair is not None:
                    self._emit(
                        "campaign_query_plan_v4_syntax_repair",
                        {"attempt": attempt + 1, **syntax_repair},
                    )
                payload = attach_campaign_query_plan_v4_identity(
                    raw_payload,
                    input.query,
                    current_date=input.context.current_date,
                )
                repair_error = _audience_repair_error(raw_payload, payload)
                if repair_error:
                    if payload.get("audience_emission_failures"):
                        emission_failure = payload
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
                                response,
                                last_error,
                                input.query,
                                current_date=input.context.current_date,
                            ),
                        },
                    ]
                )
        # 방출 실패는 'LLM 구조화를 쓸 수 없음' 폴백으로 덮지 않는다 — 그 폴백은 원인을
        # llm_structuring_unavailable 로 바꿔 적어, 고칠 곳이 방출 품질인 실패를 가용성/
        # 레지스트리 문제로 오보고한다. 실패의 이름을 지키는 것이 이 분기의 전부다.
        if emission_failure is not None:
            self._emit(
                "campaign_query_plan_v4_emission_failure",
                {"attempts": attempts_made, "last_error": last_error},
            )
            return validate_campaign_query_plan_v4(
                emission_failure, query=input.query, require_semantic=True
            )
        self._emit(
            "campaign_query_plan_v4_fallback",
            {"attempts": attempts_made, "last_error": last_error},
        )
        return build_campaign_query_plan_v4_fallback(
            input.query, current_date=input.context.current_date
        )
