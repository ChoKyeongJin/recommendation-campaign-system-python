"""캠페인 메시지 생성 — 채널 정책 · 변형 생성 · 통신사 규격 검증.

graph_rag.py 에서 분리했다. 분리 전에는 타겟팅 SQL 컴파일러와 한 파일에 있었지만
두 도메인은 공유하는 것이 거의 없다 — 여기서 다루는 것은 "누구를 고를 것인가"가
아니라 "고른 사람에게 무슨 문장을 보낼 것인가"이고, 입력도 SQL 이 아니라 캠페인
컨텍스트다. 실측 결과 이 묶음은 남은 파이프라인과 공유하는 심볼이 0개였다.

담당 범위:
  - 채널 결정: 요청 채널 · 정책 허용 목록 · 질의 신호를 합쳐 실제 발송 채널을 정한다.
  - 변형 생성: benefit/urgency/emotion 3종을 병렬로 만들고, 검증 실패 시 수리 재시도.
  - 규격 검증: LMS/RCS 의 통신사 바이트 한도와 RCS 버튼 규칙을 강제한다.
    바이트 계산이 len() 이 아닌 이유는 통신사 규격이 2바이트 문자를 따로 세기 때문이다.

메시지 생성은 recommend_campaign 인텐트 + 캠페인 컨텍스트가 있어야 정상 동작한다.
그 전제가 깨지면 어색한 폴백 카피가 나가므로 :func:`build_message_context` 가
컨텍스트 부재를 먼저 실패로 만든다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 배관은 rag.llm_io, 기본 경로는
rag.config 에서 가져온다(둘 다 하위 리프).
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from common_utils import elapsed_ms as _elapsed_ms
from rag.config import (
    DEFAULT_MESSAGE_POLICY_PATH,
    DEFAULT_POLICY_PATH,
    DEFAULT_PROMPT_DIR,
    _load_business_policies,
)
from rag.llm_io import (
    _message_summary,
    _openai_chat_create,
    _read_prompt_template,
    _render_prompt_template,
    _write_rag_llm_log,
)


MESSAGE_CHANNEL_TERMS = {"lms", "rcs"}


DEFAULT_MESSAGE_CHANNEL = "lms"


MESSAGE_VARIANTS = ["benefit_emphasis", "urgency_emphasis", "emotion_emphasis"]


MESSAGE_GENERATION_TEMPERATURE = 0.5


MESSAGE_GENERATION_MAX_ATTEMPTS = 3


MESSAGE_GENERATION_MAX_TOKENS = 500


MESSAGE_GENERATION_OPENAI_TIMEOUT_SECONDS = 15.0


DEFAULT_MESSAGE_CHANNEL_LIMITS = {
    "lms": {"max_chars": 1000, "unit": "characters"},
    "rcs": {"max_chars": 1300, "unit": "characters"},
}


MESSAGE_POLICY_CHANNEL_ALIASES = {
    "lms": "lms",
    "rcs": "rcs",
    "rcssms": "rcs",
    "rcs_sms": "rcs",
    "rcs-sms": "rcs",
}


def build_message_context(
    query_plan: dict[str, Any],
    context_nodes: list[dict[str, Any]],
    sql_result: dict[str, Any],
    requested_channel: str = "auto",
    business_policies: Path | None = DEFAULT_POLICY_PATH,
    message_policy: Path | None = DEFAULT_MESSAGE_POLICY_PATH,
    query: str | None = None,
) -> dict[str, Any]:
    channel_policy = _message_channel_policy(business_policies, message_policy)
    channel = _resolve_message_channel(query_plan, requested_channel, channel_policy)
    if channel is None:
        return {
            "is_success": False,
            "requested_channel": requested_channel,
            "channel": None,
            "channel_policy": channel_policy,
            "campaigns": [],
            "message_examples": [],
            "target_context": _message_target_context(query_plan),
            "query": query or str(query_plan.get("original_query") or ""),
            "failure_reason": "unsupported_message_channel",
        }

    if not sql_result.get("is_success"):
        return _message_context_failure(query_plan, requested_channel, channel, channel_policy, "sql_result_failed")
    if query_plan.get("intent") != "recommend_campaign":
        return _message_context_failure(query_plan, requested_channel, channel, channel_policy, "intent_not_recommend_campaign")

    campaigns = _campaign_message_contexts(context_nodes, query_plan, channel)
    if not campaigns:
        return _message_context_failure(query_plan, requested_channel, channel, channel_policy, "campaign_context_missing")

    return {
        "is_success": True,
        "requested_channel": requested_channel,
        "channel": channel,
        "channel_policy": channel_policy,
        "selected_channel_policy": _selected_message_channel_policy(channel_policy, channel),
        "campaigns": campaigns,
        "message_examples": _message_example_contexts(context_nodes, campaigns, channel),
        "target_context": _message_target_context(query_plan),
        "query": query or str(query_plan.get("original_query") or ""),
        "failure_reason": None,
    }


def _message_context_failure(
    query_plan: dict[str, Any],
    requested_channel: str,
    channel: str,
    channel_policy: dict[str, Any],
    failure_reason: str,
) -> dict[str, Any]:
    return {
        "is_success": False,
        "requested_channel": requested_channel,
        "channel": channel,
        "channel_policy": channel_policy,
        "selected_channel_policy": _selected_message_channel_policy(channel_policy, channel),
        "campaigns": [],
        "message_examples": [],
        "target_context": _message_target_context(query_plan),
        "failure_reason": failure_reason,
    }


def _message_channel_policy(business_policies: Path | None, message_policy: Path | None = DEFAULT_MESSAGE_POLICY_PATH) -> dict[str, Any]:
    external_policy = _load_message_policy(message_policy)
    for policy in _load_business_policies(business_policies):
        if policy.get("policy_id") != "channel_message_generation":
            continue
        allowed_channels = [channel for channel in policy.get("allowed_channels", []) if channel in MESSAGE_CHANNEL_TERMS]
        channel_limits = policy.get("channel_limits") if isinstance(policy.get("channel_limits"), dict) else {}
        allowed_channels = _message_policy_allowed_channels(external_policy, allowed_channels or sorted(MESSAGE_CHANNEL_TERMS))
        return {
            "policy_id": policy["policy_id"],
            "default_channel": policy.get("default_channel") if policy.get("default_channel") in MESSAGE_CHANNEL_TERMS else DEFAULT_MESSAGE_CHANNEL,
            "allowed_channels": allowed_channels,
            "channel_limits": {**DEFAULT_MESSAGE_CHANNEL_LIMITS, **channel_limits},
            "message_policy_path": str(message_policy) if message_policy else None,
            "message_policy": external_policy,
            "required_variants": [variant for variant in policy.get("required_variants", []) if variant in MESSAGE_VARIANTS] or MESSAGE_VARIANTS,
            "deny_unverified_benefits": bool(policy.get("anti_hallucination", {}).get("deny_unverified_benefits", True)),
        }
    allowed_channels = _message_policy_allowed_channels(external_policy, sorted(MESSAGE_CHANNEL_TERMS))
    return {
        "policy_id": "default_channel_message_generation",
        "default_channel": DEFAULT_MESSAGE_CHANNEL,
        "allowed_channels": allowed_channels,
        "channel_limits": DEFAULT_MESSAGE_CHANNEL_LIMITS,
        "message_policy_path": str(message_policy) if message_policy else None,
        "message_policy": external_policy,
        "required_variants": MESSAGE_VARIANTS,
        "deny_unverified_benefits": True,
    }


def _load_message_policy(message_policy: Path | None) -> dict[str, Any]:
    if message_policy is None or not message_policy.exists():
        return {}
    payload = json.loads(message_policy.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}

    normalized: dict[str, Any] = {}
    for raw_channel, raw_policy in payload.items():
        channel = _canonical_message_channel(str(raw_channel))
        if channel is None or not isinstance(raw_policy, dict):
            continue
        normalized[channel] = {
            "source_key": raw_channel,
            "name": raw_policy.get("name", raw_channel),
            "message_base_id": raw_policy.get("messageBaseId"),
            "encoding": raw_policy.get("encoding", "UTF-8"),
            "description": raw_policy.get("description"),
            "constraints": raw_policy.get("constraints") if isinstance(raw_policy.get("constraints"), dict) else {},
            "prompt": [line for line in raw_policy.get("prompt", []) if isinstance(line, str)],
            "message_schema": _message_schema_for_channel(channel),
        }
    return normalized


def _message_policy_allowed_channels(message_policy: dict[str, Any], fallback_channels: list[str]) -> list[str]:
    policy_channels = [channel for channel in message_policy if channel in MESSAGE_CHANNEL_TERMS]
    if not policy_channels:
        return fallback_channels
    return [channel for channel in fallback_channels if channel in policy_channels]


def _selected_message_channel_policy(channel_policy: dict[str, Any], channel: str | None) -> dict[str, Any]:
    if channel is None:
        return {}
    message_policy = channel_policy.get("message_policy") if isinstance(channel_policy.get("message_policy"), dict) else {}
    selected = message_policy.get(channel) if isinstance(message_policy.get(channel), dict) else None
    if selected is not None:
        return selected
    return {
        "source_key": channel,
        "name": channel.upper(),
        "encoding": "UTF-8",
        "constraints": channel_policy.get("channel_limits", {}).get(channel, {}),
        "prompt": [],
        "message_schema": _message_schema_for_channel(channel),
    }


def _message_schema_for_channel(channel: str) -> dict[str, Any]:
    if channel == "rcs":
        return {
            "required_fields": ["channel", "variant", "title", "description", "buttons", "source_campaign_id"],
            "optional_fields": ["used_offer"],
            "buttons_item_fields": ["name"],
        }
    return {
        "required_fields": ["channel", "variant", "text", "source_campaign_id"],
        "optional_fields": ["used_offer"],
    }


def _canonical_message_channel(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[\s\-]+", "_", value.strip().casefold())
    return MESSAGE_POLICY_CHANNEL_ALIASES.get(normalized)


def _resolve_message_channel(query_plan: dict[str, Any], requested_channel: str, channel_policy: dict[str, Any]) -> str | None:
    allowed_channels = set(channel_policy.get("allowed_channels", sorted(MESSAGE_CHANNEL_TERMS))) & MESSAGE_CHANNEL_TERMS
    requested = (requested_channel or "auto").strip().casefold()
    if requested != "auto":
        canonical_requested = _canonical_message_channel(requested)
        return canonical_requested if canonical_requested in allowed_channels else None

    campaign_channels = query_plan.get("campaign_constraints", {}).get("channels", [])
    target_channels = query_plan.get("target_user", {}).get("preferred_channels", [])
    for channel in [*campaign_channels, *target_channels]:
        if channel in allowed_channels:
            return channel

    default_channel = channel_policy.get("default_channel", DEFAULT_MESSAGE_CHANNEL)
    return default_channel if default_channel in allowed_channels else DEFAULT_MESSAGE_CHANNEL


def _campaign_message_contexts(context_nodes: list[dict[str, Any]], query_plan: dict[str, Any], channel: str) -> list[dict[str, Any]]:
    campaigns = []
    for node in context_nodes:
        if node.get("type") != "campaign":
            continue
        payload = node.get("payload", {})
        campaign = {
            "campaign_id": payload.get("id") or node.get("id"),
            "name": payload.get("name") or node.get("title"),
            "objective": payload.get("objective"),
            "category": payload.get("category"),
            "channels": payload.get("channel") or payload.get("channels") or [],
            "target_segments": payload.get("target_segments", []),
            "offer": payload.get("offer"),
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "keywords": payload.get("keywords", []),
            "text_for_embedding": payload.get("text_for_embedding"),
            "score": node.get("score"),
        }
        if isinstance(campaign["campaign_id"], str) and campaign["campaign_id"].strip() and _campaign_matches_message_plan(campaign, query_plan, channel):
            campaigns.append(campaign)

    # 실제 캠페인 노드가 하나도 매칭되지 않으면(현재 KB에는 campaign 노드가 없음)
    # 프롬프트에 담긴 판매 목표를 합성 캠페인 컨텍스트로 대체해 채널메시지 소재로 쓴다.
    if not campaigns:
        synthesized = _prompt_goal_campaign_context(query_plan)
        if synthesized is not None and _campaign_matches_message_plan(synthesized, query_plan, channel):
            campaigns.append(synthesized)
    return campaigns


def _prompt_goal_campaign_context(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    constraints = query_plan.get("campaign_constraints", {})
    objective = constraints.get("objective")
    sell_object = constraints.get("sell_object")
    offer_type = constraints.get("offer_type")
    if not objective and not sell_object:
        return None
    name = f"{sell_object} 판매" if sell_object else "프롬프트 목표 캠페인"
    keywords = [keyword for keyword in (sell_object, objective) if keyword]
    return {
        "campaign_id": "prompt_goal",
        "name": name,
        "objective": objective,
        "category": None,
        "channels": [],
        "target_segments": [],
        "offer": offer_type,
        "start_date": None,
        "end_date": None,
        "keywords": keywords,
        "text_for_embedding": f"프롬프트 목표 기반 합성 캠페인. 판매 상품: {sell_object or '미지정'}. 목표: {objective or '미지정'}.",
        "score": None,
        "is_synthesized": True,
    }


def _campaign_matches_message_plan(campaign: dict[str, Any], query_plan: dict[str, Any], channel: str) -> bool:
    channels = campaign.get("channels", [])
    if channels and channel not in channels:
        return False

    campaign_constraints = query_plan.get("campaign_constraints", {})
    categories = campaign_constraints.get("category", [])
    if categories and campaign.get("category") not in categories:
        return False

    offer_type = campaign_constraints.get("offer_type")
    if offer_type and _campaign_offer_text(campaign) and not _campaign_offer_matches(offer_type, campaign):
        return False

    required_segments = _message_required_target_segments(query_plan)
    target_segments = set(campaign.get("target_segments", []))
    if required_segments and target_segments and not (required_segments & target_segments):
        return False

    return True


def _campaign_offer_matches(offer_type: str, campaign: dict[str, Any]) -> bool:
    offer_text = _campaign_offer_text(campaign)
    if offer_type == "coupon":
        return any(term in offer_text for term in ("쿠폰", "할인", "coupon", "discount"))
    if offer_type == "free_shipping":
        return any(term in offer_text for term in ("무료배송", "free shipping"))
    if offer_type == "subscription":
        return any(term in offer_text for term in ("구독", "정기", "subscription"))
    return offer_type in offer_text


def _campaign_offer_text(campaign: dict[str, Any]) -> str:
    return " ".join(str(value) for value in [campaign.get("offer"), *campaign.get("keywords", [])] if value).casefold()


def _message_required_target_segments(query_plan: dict[str, Any]) -> set[str]:
    target_user = query_plan.get("target_user", {})
    segments = set(target_user.get("behaviors", []))
    segments.update(target_user.get("lifecycle", []))
    segments.update(target_user.get("interests", []))
    if target_user.get("price_sensitivity") == "high":
        segments.add("price_sensitive")
    if target_user.get("price_sensitivity") == "low":
        segments.add("premium_buyer")
    return segments


def _message_example_contexts(context_nodes: list[dict[str, Any]], campaigns: list[dict[str, Any]], channel: str) -> list[dict[str, Any]]:
    campaign_ids = {campaign["campaign_id"] for campaign in campaigns}
    examples = []
    for node in context_nodes:
        payload = node.get("payload", {})
        if node.get("type") not in {"campaign_message_example", "message_example"}:
            continue
        campaign_id = payload.get("campaign_id")
        example_channel = payload.get("channel")
        if campaign_id not in campaign_ids or example_channel != channel:
            continue
        examples.append(
            {
                "example_id": payload.get("id") or node.get("id"),
                "campaign_id": campaign_id,
                "channel": example_channel,
                "emphasis_type": payload.get("emphasis_type"),
                "message_text": payload.get("message_text"),
                "brand_tone": payload.get("brand_tone"),
            }
        )
    return examples


def _message_target_context(query_plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_user": query_plan.get("target_user", {}),
        "campaign_constraints": query_plan.get("campaign_constraints", {}),
        "exclude": query_plan.get("exclude", {}),
    }


def _message_generation_tone_manner_rules(prompt_dir: Path | None = DEFAULT_PROMPT_DIR) -> str:
    fallback = "\n".join(
        [
            "Campaign Context와 Existing Message Examples의 brand_tone, message_text를 참고한다.",
            "기존 메시지를 그대로 복사하지 않고 같은 브랜드 말투와 표현 밀도만 유지한다.",
            "benefit_emphasis, urgency_emphasis, emotion_emphasis는 서로 다른 설득 포인트와 문장 구조를 사용한다.",
        ]
    )
    return _read_prompt_template(prompt_dir, "message_generation_tone_manner.txt", fallback)


def build_message_response(
    message_context: dict[str, Any],
    llm_model: str,
    generate_messages: bool,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
    message_generation_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective_options = _message_generation_effective_options(message_generation_options)
    if not message_context.get("is_success"):
        return {
            "is_success": False,
            "mode": "skipped",
            "model": None,
            "options": effective_options,
            "content": None,
            "messages": [],
            "validation": None,
            "context": message_context,
            "failure_reason": message_context.get("failure_reason"),
        }
    if not generate_messages:
        return {
            "is_success": False,
            "mode": "prompt_only",
            "model": None,
            "options": effective_options,
            "content": None,
            "messages": [],
            "validation": None,
            "context": message_context,
            "failure_reason": None,
        }
    if not os.getenv("OPENAI_API_KEY"):
        return {
            "is_success": False,
            "mode": "openai_chat_completion",
            "model": llm_model,
            "options": effective_options,
            "content": None,
            "messages": [],
            "validation": None,
            "context": message_context,
            "failure_reason": "missing_openai_api_key",
        }

    try:
        from openai import OpenAI
    except ImportError as exc:
        return {
            "is_success": False,
            "mode": "openai_chat_completion",
            "model": llm_model,
            "options": effective_options,
            "content": None,
            "messages": [],
            "validation": None,
            "context": message_context,
            "failure_reason": f"openai_import_failed:{exc.__class__.__name__}",
        }

    system_prompt = _read_prompt_template(
        prompt_dir,
        "message_generation_system.txt",
        "\n".join(
            [
                "너는 캠페인 채널 메시지 생성기다.",
                "입력 근거에 없는 혜택, 기간, 사실을 만들지 않는다.",
                "요청된 채널과 variant 하나만 한국어 JSON object로 출력한다.",
                "기존 메시지의 문장을 복사하지 않는다.",
            ]
        ),
    )
    max_attempts = effective_options["max_attempts"]
    attempts: list[dict[str, Any]] = []
    repair_context = "none"
    last_content = None
    last_validation = None
    last_failure_reason = None
    _write_rag_llm_log(
        "llm_message_base_prompt",
        {
            "mode": "openai_chat_completion_parallel_variants",
            "model": llm_model,
            "options": effective_options,
            "message_context": message_context,
            "system_prompt": system_prompt,
            "query": message_context.get("query", ""),
        },
    )

    for attempt_number in range(1, max_attempts + 1):
        attempt_started_at = time.perf_counter()
        parallel_result = _generate_message_variants_parallel(
            repair_context=repair_context,
            message_context=message_context,
            system_prompt=system_prompt,
            llm_model=llm_model,
            prompt_dir=prompt_dir,
            openai_client_factory=OpenAI,
            message_generation_options=effective_options,
        )
        content = parallel_result["content"]
        last_content = content
        validation = validate_message_response(parallel_result["payload"], message_context)
        if parallel_result["issues"]:
            validation = {
                **validation,
                "is_satisfied": False,
                "issues": [*validation.get("issues", []), *parallel_result["issues"]],
            }
        last_validation = validation
        last_failure_reason = None if validation["is_satisfied"] else "message_validation_failed"
        attempts.append(
            _message_generation_attempt(
                attempt_number,
                validation["is_satisfied"],
                last_failure_reason,
                content,
                validation,
                _elapsed_ms(attempt_started_at),
                parallel_result["variant_attempts"],
            )
        )
        if validation["is_satisfied"]:
            return {
                "is_success": True,
                "mode": "openai_chat_completion_parallel_variants",
                "model": llm_model,
                "options": effective_options,
                "content": content,
                "messages": validation["messages"],
                "validation": validation,
                "context": message_context,
                "failure_reason": None,
                "attempt_count": attempt_number,
                "max_attempts": max_attempts,
                "attempts": attempts,
            }

        if attempt_number < max_attempts:
            repair_context = _message_repair_context(
                previous_payload=parallel_result["payload"],
                validation=last_validation,
            )

    return {
        "is_success": False,
        "mode": "openai_chat_completion_parallel_variants",
        "model": llm_model,
        "options": effective_options,
        "content": last_content,
        "messages": [],
        "validation": last_validation,
        "context": message_context,
        "failure_reason": last_failure_reason or "message_generation_failed",
        "attempt_count": len(attempts),
        "max_attempts": max_attempts,
        "attempts": attempts,
    }


def _generate_message_variants_parallel(
    repair_context: str,
    message_context: dict[str, Any],
    system_prompt: str,
    llm_model: str,
    prompt_dir: Path | None,
    openai_client_factory: Any,
    message_generation_options: dict[str, Any],
) -> dict[str, Any]:
    required_variants = message_context.get("channel_policy", {}).get("required_variants", MESSAGE_VARIANTS)
    variant_attempts: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(required_variants) or len(MESSAGE_VARIANTS)) as executor:
        future_to_variant = {
            executor.submit(
                _generate_single_message_variant,
                variant,
                repair_context,
                message_context,
                system_prompt,
                llm_model,
                prompt_dir,
                openai_client_factory,
                message_generation_options,
            ): variant
            for variant in required_variants
        }
        for future in concurrent.futures.as_completed(future_to_variant):
            variant = future_to_variant[future]
            try:
                variant_result = future.result()
            except Exception as exc:
                variant_result = {
                    "variant": variant,
                    "is_success": False,
                    "failure_reason": f"message_generation_failed:{exc.__class__.__name__}",
                    "content": None,
                    "message": None,
                    "duration_ms": 0.0,
                }
            variant_attempts.append(variant_result)
    variant_attempts.sort(key=lambda attempt: required_variants.index(attempt["variant"]) if attempt.get("variant") in required_variants else len(required_variants))
    for variant_attempt in variant_attempts:
        if variant_attempt.get("is_success") and isinstance(variant_attempt.get("message"), dict):
            messages.append(variant_attempt["message"])
        else:
            issues.append(_message_issue(f"messages.{variant_attempt.get('variant', 'unknown')}", variant_attempt.get("failure_reason") or "message variant generation failed."))
    payload = {"messages": messages}
    content = json.dumps(
        {
            "messages": messages,
            "variant_attempts": [
                {
                    "variant": attempt.get("variant"),
                    "is_success": attempt.get("is_success"),
                    "failure_reason": attempt.get("failure_reason"),
                    "duration_ms": attempt.get("duration_ms"),
                    "content": attempt.get("content"),
                }
                for attempt in variant_attempts
            ],
        },
        ensure_ascii=False,
    )
    return {"payload": payload, "content": content, "issues": issues, "variant_attempts": variant_attempts}


def _generate_single_message_variant(
    variant: str,
    repair_context: str,
    message_context: dict[str, Any],
    system_prompt: str,
    llm_model: str,
    prompt_dir: Path | None,
    openai_client_factory: Any,
    message_generation_options: dict[str, Any],
) -> dict[str, Any]:
    started_at = time.perf_counter()
    try:
        client = openai_client_factory()
        completion_options = {
            "temperature": message_generation_options["temperature"],
            "max_tokens": message_generation_options["max_tokens"],
            "timeout": message_generation_options["timeout_seconds"],
        }
        if "top_p" in message_generation_options:
            completion_options["top_p"] = message_generation_options["top_p"]
        user_prompt = render_message_variant_prompt(
            variant=variant,
            message_context=message_context,
            repair_context=repair_context,
            prompt_dir=prompt_dir,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        _write_rag_llm_log(
            "llm_message_variant_request",
            {
                "mode": "openai_chat_completion_parallel_variants",
                "model": llm_model,
                "variant": variant,
                "options": completion_options,
                "messages": messages,
                "message_summary": _message_summary(messages),
            },
        )
        response = _openai_chat_create(client, 
            model=llm_model,
            response_format={"type": "json_object"},
            messages=messages,
            **completion_options,
        )
        content = response.choices[0].message.content or "{}"
        payload = json.loads(content)
        message = _single_variant_message(payload, variant)
        if message is None:
            _write_rag_llm_log(
                "llm_message_variant_response",
                {
                    "mode": "openai_chat_completion_parallel_variants",
                    "model": llm_model,
                    "variant": variant,
                    "is_success": False,
                    "failure_reason": "message_variant_missing",
                    "content": content,
                    "duration_ms": _elapsed_ms(started_at),
                },
            )
            return {
                "variant": variant,
                "is_success": False,
                "failure_reason": "message_variant_missing",
                "content": content,
                "message": None,
                "duration_ms": _elapsed_ms(started_at),
            }
        message["variant"] = variant
        _write_rag_llm_log(
            "llm_message_variant_response",
            {
                "mode": "openai_chat_completion_parallel_variants",
                "model": llm_model,
                "variant": variant,
                "is_success": True,
                "content": content,
                "message": message,
                "duration_ms": _elapsed_ms(started_at),
            },
        )
        return {
            "variant": variant,
            "is_success": True,
            "failure_reason": None,
            "content": content,
            "message": message,
            "duration_ms": _elapsed_ms(started_at),
        }
    except json.JSONDecodeError as exc:
        _write_rag_llm_log(
            "llm_message_variant_failure",
            {
                "mode": "openai_chat_completion_parallel_variants",
                "model": llm_model,
                "variant": variant,
                "failure_reason": f"message_generation_invalid_json:{exc.__class__.__name__}",
                "content": locals().get("content"),
                "duration_ms": _elapsed_ms(started_at),
            },
        )
        return {
            "variant": variant,
            "is_success": False,
            "failure_reason": f"message_generation_invalid_json:{exc.__class__.__name__}",
            "content": locals().get("content"),
            "message": None,
            "duration_ms": _elapsed_ms(started_at),
        }
    except Exception as exc:
        _write_rag_llm_log(
            "llm_message_variant_failure",
            {
                "mode": "openai_chat_completion_parallel_variants",
                "model": llm_model,
                "variant": variant,
                "failure_reason": f"message_generation_failed:{exc.__class__.__name__}",
                "duration_ms": _elapsed_ms(started_at),
            },
        )
        return {
            "variant": variant,
            "is_success": False,
            "failure_reason": f"message_generation_failed:{exc.__class__.__name__}",
            "content": None,
            "message": None,
            "duration_ms": _elapsed_ms(started_at),
        }


def render_message_variant_prompt(
    variant: str,
    message_context: dict[str, Any],
    repair_context: str = "none",
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> str:
    fallback = "\n".join(
        [
            "아래 입력만 사용해 지정된 variant 1개만 생성하라.",
            "반환 JSON은 messages 배열에 정확히 1개 object만 포함해야 한다.",
            "[User Query] ${query}",
            "[Variant] ${variant}",
            "[Requested Channel] ${requested_channel}",
            "[Selected Channel Policy] ${selected_channel_policy}",
            "[Campaign Context] ${campaign_context}",
            "[Target Context] ${target_context}",
            "[Existing Message Examples] ${message_examples}",
            "[Tone And Manner Rules] ${tone_manner_rules}",
            "[Repair Context] ${repair_context}",
        ]
    )
    template = _read_prompt_template(prompt_dir, "message_generation_variant_user.txt", fallback)
    return _render_prompt_template(
        template,
        query=message_context.get("query", ""),
        variant=variant,
        requested_channel=message_context.get("channel", DEFAULT_MESSAGE_CHANNEL),
        selected_channel_policy=json.dumps(message_context.get("selected_channel_policy", {}), ensure_ascii=False, separators=(",", ":")),
        campaign_context=json.dumps(_compact_message_context_items(message_context.get("campaigns", []), 3), ensure_ascii=False, separators=(",", ":")),
        target_context=json.dumps(message_context.get("target_context", {}), ensure_ascii=False, separators=(",", ":")),
        message_examples=json.dumps(_compact_message_context_items(message_context.get("message_examples", []), 6), ensure_ascii=False, separators=(",", ":")),
        tone_manner_rules=_message_generation_tone_manner_rules(prompt_dir),
        repair_context=repair_context,
    )


def _compact_message_context_items(items: Any, limit: int) -> list[Any]:
    if not isinstance(items, list):
        return []
    return items[:limit]


def _message_repair_context(previous_payload: Any, validation: dict[str, Any] | None) -> str:
    previous_messages = (
        previous_payload.get("messages", [])
        if isinstance(previous_payload, dict) and isinstance(previous_payload.get("messages"), list)
        else []
    )
    return json.dumps(
        {
            "validation_issues": (validation or {}).get("issues", []),
            "previous_messages": previous_messages,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _single_variant_message(payload: Any, variant: str) -> dict[str, Any] | None:
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        for message in payload["messages"]:
            if isinstance(message, dict) and message.get("variant") == variant:
                return dict(message)
        for message in payload["messages"]:
            if isinstance(message, dict):
                return dict(message)
    if isinstance(payload, dict) and isinstance(payload.get("message"), dict):
        return dict(payload["message"])
    if isinstance(payload, dict) and any(key in payload for key in ("text", "title", "description")):
        return dict(payload)
    return None


def _message_generation_attempt(
    attempt_number: int,
    is_success: bool,
    failure_reason: str | None,
    content: str | None,
    validation: dict[str, Any] | None,
    duration_ms: float,
    variant_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "attempt": attempt_number,
        "is_success": is_success,
        "failure_reason": failure_reason,
        "duration_ms": duration_ms,
        "variant_attempts": variant_attempts or [],
        "content": content,
        "validation": validation,
    }


def _message_generation_effective_options(options: dict[str, Any] | None = None) -> dict[str, Any]:
    effective_options: dict[str, Any] = {
        "temperature": _message_generation_temperature(options),
        "max_attempts": _message_generation_max_attempts(options),
        "max_tokens": _message_generation_max_tokens(options),
        "timeout_seconds": _message_generation_openai_timeout_seconds(options),
    }
    top_p = _message_generation_top_p(options)
    if top_p is not None:
        effective_options["top_p"] = top_p
    return effective_options


def _generation_option(options: dict[str, Any] | None, key: str) -> Any:
    if not isinstance(options, dict):
        return None
    return options.get(key)


def _message_generation_temperature(options: dict[str, Any] | None = None) -> float:
    configured_temperature = _generation_option(options, "temperature")
    if configured_temperature is None:
        configured_temperature = os.getenv("MESSAGE_GENERATION_TEMPERATURE", MESSAGE_GENERATION_TEMPERATURE)
    try:
        return min(2.0, max(0.0, float(configured_temperature)))
    except (TypeError, ValueError):
        return MESSAGE_GENERATION_TEMPERATURE


def _message_generation_top_p(options: dict[str, Any] | None = None) -> float | None:
    configured_top_p = _generation_option(options, "top_p")
    if configured_top_p is None:
        configured_top_p = os.getenv("MESSAGE_GENERATION_TOP_P")
    if configured_top_p is None:
        return None
    try:
        return min(1.0, max(0.0, float(configured_top_p)))
    except (TypeError, ValueError):
        return None


def _message_generation_max_attempts(options: dict[str, Any] | None = None) -> int:
    try:
        configured_attempts = int(_generation_option(options, "max_attempts") or os.getenv("MESSAGE_GENERATION_MAX_ATTEMPTS", MESSAGE_GENERATION_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        return MESSAGE_GENERATION_MAX_ATTEMPTS
    return max(1, configured_attempts)


def _message_generation_max_tokens(options: dict[str, Any] | None = None) -> int:
    try:
        configured_tokens = int(_generation_option(options, "max_tokens") or os.getenv("MESSAGE_GENERATION_MAX_TOKENS", MESSAGE_GENERATION_MAX_TOKENS))
    except (TypeError, ValueError):
        return MESSAGE_GENERATION_MAX_TOKENS
    return max(100, configured_tokens)


def _message_generation_openai_timeout_seconds(options: dict[str, Any] | None = None) -> float:
    try:
        configured_timeout = float(_generation_option(options, "timeout_seconds") or os.getenv("MESSAGE_GENERATION_OPENAI_TIMEOUT_SECONDS", MESSAGE_GENERATION_OPENAI_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return MESSAGE_GENERATION_OPENAI_TIMEOUT_SECONDS
    return max(1.0, configured_timeout)


def validate_message_response(payload: Any, message_context: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return {"is_satisfied": False, "messages": [], "issues": [_message_issue("messages", "messages must be a list.")]}

    channel = message_context.get("channel")
    selected_policy = message_context.get("selected_channel_policy") if isinstance(message_context.get("selected_channel_policy"), dict) else {}
    channel_constraints = selected_policy.get("constraints") if isinstance(selected_policy.get("constraints"), dict) else {}
    campaigns = message_context.get("campaigns", [])
    campaign_ids = {campaign.get("campaign_id") for campaign in campaigns if campaign.get("campaign_id")}
    offers = {campaign.get("offer") for campaign in campaigns if campaign.get("offer")}
    required_variants = message_context.get("channel_policy", {}).get("required_variants", MESSAGE_VARIANTS)

    issues = []
    normalized_messages = []
    seen_variants = set()
    seen_texts = set()
    for index, message in enumerate(messages):
        path = f"messages[{index}]"
        if not isinstance(message, dict):
            issues.append(_message_issue(path, "message must be an object."))
            continue
        variant = message.get("variant")
        source_campaign_id = message.get("source_campaign_id")
        used_offer = message.get("used_offer")
        message_channel = _canonical_message_channel(message.get("channel")) if isinstance(message.get("channel"), str) else message.get("channel")

        if source_campaign_id in (None, "") and len(campaign_ids) == 1:
            source_campaign_id = next(iter(campaign_ids))

        if variant not in required_variants:
            issues.append(_message_issue(f"{path}.variant", "variant must be one of the required variants."))
        else:
            seen_variants.add(variant)
        if message_channel != channel:
            issues.append(_message_issue(f"{path}.channel", "message channel must match requested channel."))
        if source_campaign_id not in campaign_ids:
            issues.append(_message_issue(f"{path}.source_campaign_id", "source_campaign_id must exist in Campaign Context."))
        if used_offer and used_offer not in offers:
            issues.append(_message_issue(f"{path}.used_offer", "used_offer must match a Campaign Context offer."))
        normalized_message = _normalize_channel_message(message, channel, channel_constraints, path, issues, campaigns, source_campaign_id)
        combined_text = _message_combined_text(normalized_message)

        normalized_text = re.sub(r"\s+", " ", combined_text.casefold())
        if normalized_text in seen_texts:
            issues.append(_message_issue(path, "duplicate message text is not allowed."))
        seen_texts.add(normalized_text)
        normalized_messages.append({**normalized_message, "channel": message_channel, "variant": variant, "source_campaign_id": source_campaign_id, "used_offer": used_offer})

    missing_variants = [variant for variant in required_variants if variant not in seen_variants]
    for variant in missing_variants:
        issues.append(_message_issue("messages", f"missing required variant: {variant}."))

    return {
        "is_satisfied": not issues and len(normalized_messages) == len(required_variants),
        "messages": normalized_messages,
        "issues": issues,
        "policy": selected_policy,
    }


def _normalize_channel_message(
    message: dict[str, Any],
    channel: str,
    constraints: dict[str, Any],
    path: str,
    issues: list[dict[str, str]],
    campaigns: list[dict[str, Any]] | None = None,
    source_campaign_id: Any = None,
) -> dict[str, Any]:
    if channel == "rcs":
        return _normalize_rcs_message(message, constraints, path, issues, campaigns or [], source_campaign_id)
    return _normalize_lms_message(message, constraints, path, issues)


def _normalize_lms_message(message: dict[str, Any], constraints: dict[str, Any], path: str, issues: list[dict[str, str]]) -> dict[str, Any]:
    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        issues.append(_message_issue(f"{path}.text", "text must be a non-empty string."))
        text = ""
    text = text.strip()
    max_bytes = _positive_int(constraints.get("maxBytes"))
    max_korean_chars = _positive_int(constraints.get("maxKoreanChars"))
    max_ascii_chars = _positive_int(constraints.get("maxAsciiChars"))
    byte_count = _carrier_message_byte_count(text)
    if max_bytes and byte_count > max_bytes:
        issues.append(_message_issue(f"{path}.text", f"text exceeds carrier maxBytes={max_bytes}."))
    if max_korean_chars and _is_korean_only_text(text) and len(text) > max_korean_chars:
        issues.append(_message_issue(f"{path}.text", f"Korean text exceeds maxKoreanChars={max_korean_chars}."))
    if max_ascii_chars and text.isascii() and len(text) > max_ascii_chars:
        issues.append(_message_issue(f"{path}.text", f"ASCII text exceeds maxAsciiChars={max_ascii_chars}."))
    return {
        "text": text,
        "char_count": len(text),
        "byte_count": byte_count,
        "byte_count_rule": "carrier: korean/full-width=2, ascii=1",
        "within_limits": not any(issue["path"].startswith(f"{path}.text") for issue in issues),
    }


def _normalize_rcs_message(
    message: dict[str, Any],
    constraints: dict[str, Any],
    path: str,
    issues: list[dict[str, str]],
    campaigns: list[dict[str, Any]],
    source_campaign_id: Any,
) -> dict[str, Any]:
    title = message.get("title")
    description = message.get("description")
    buttons = message.get("buttons")
    if not isinstance(title, str) or not title.strip():
        issues.append(_message_issue(f"{path}.title", "title must be a non-empty string."))
        title = ""
    if not isinstance(description, str) or not description.strip():
        issues.append(_message_issue(f"{path}.description", "description must be a non-empty string."))
        description = ""
    title = title.strip()
    description = description.strip()

    title_constraints = constraints.get("title") if isinstance(constraints.get("title"), dict) else {}
    description_constraints = constraints.get("description") if isinstance(constraints.get("description"), dict) else {}
    button_constraints = constraints.get("buttons") if isinstance(constraints.get("buttons"), dict) else {}
    title_max_chars = _positive_int(title_constraints.get("maxChars"))
    description_max_chars = _positive_int(description_constraints.get("maxChars"))
    max_button_count = _positive_int(button_constraints.get("maxCount"))
    button_name_max_chars = _positive_int(button_constraints.get("buttonNameMaxChars"))

    if title_max_chars and len(title) > title_max_chars:
        issues.append(_message_issue(f"{path}.title", f"title exceeds maxChars={title_max_chars}."))
    if description_max_chars and len(description) > description_max_chars:
        issues.append(_message_issue(f"{path}.description", f"description exceeds maxChars={description_max_chars}."))
    if "(광고)" not in title:
        issues.append(_message_issue(f"{path}.title", "advertising RCS title must include '(광고)'."))
    if "수신거부" not in description:
        issues.append(_message_issue(f"{path}.description", "advertising RCS description must include free opt-out text."))

    normalized_buttons = []
    if buttons is None:
        buttons = []
    if not isinstance(buttons, list):
        issues.append(_message_issue(f"{path}.buttons", "buttons must be a list."))
        buttons = []
    if max_button_count is not None and len(buttons) > max_button_count:
        issues.append(_message_issue(f"{path}.buttons", f"buttons exceeds maxCount={max_button_count}."))
    for button_index, button in enumerate(buttons):
        button_path = f"{path}.buttons[{button_index}]"
        if not isinstance(button, dict):
            issues.append(_message_issue(button_path, "button must be an object."))
            continue
        name = button.get("name") or button.get("button_name") or button.get("buttonName")
        if not isinstance(name, str) or not name.strip():
            issues.append(_message_issue(f"{button_path}.name", "button name must be a non-empty string."))
            name = ""
        name = name.strip()
        if button_name_max_chars and len(name) > button_name_max_chars:
            issues.append(_message_issue(f"{button_path}.name", f"button name exceeds buttonNameMaxChars={button_name_max_chars}."))
        normalized_buttons.append({"name": name})

    if not normalized_buttons and _should_add_rcs_button(title, description, campaigns, source_campaign_id, max_button_count):
        normalized_buttons.append(
            {
                "name": _infer_rcs_button_name(
                    title,
                    description,
                    campaigns,
                    source_campaign_id,
                    button_name_max_chars,
                )
            }
        )

    return {
        "title": title,
        "description": description,
        "buttons": normalized_buttons,
        "title_char_count": len(title),
        "description_char_count": len(description),
        "within_limits": not any(issue["path"].startswith(path) for issue in issues),
    }


def _should_add_rcs_button(
    title: str,
    description: str,
    campaigns: list[dict[str, Any]],
    source_campaign_id: Any,
    max_button_count: int | None,
) -> bool:
    if max_button_count == 0 or not (title or description):
        return False
    campaign = _find_message_campaign(campaigns, source_campaign_id)
    action_context = " ".join(
        str(value)
        for value in [
            title,
            description,
            campaign.get("objective") if campaign else None,
            campaign.get("category") if campaign else None,
            campaign.get("offer") if campaign else None,
        ]
        if value
    )
    action_terms = (
        "구매",
        "할인",
        "쿠폰",
        "혜택",
        "장바구니",
        "신청",
        "예약",
        "구독",
        "리뷰",
        "포인트",
        "무료배송",
        "무료",
        "가이드",
        "타임딜",
        "바우처",
        "purchase",
        "first_purchase",
        "repurchase",
        "reactivation",
        "subscription",
        "lead",
        "consideration",
        "engagement",
        "app_conversion",
    )
    return any(term in action_context for term in action_terms)


def _infer_rcs_button_name(
    title: str,
    description: str,
    campaigns: list[dict[str, Any]],
    source_campaign_id: Any,
    max_chars: int | None,
) -> str:
    campaign = _find_message_campaign(campaigns, source_campaign_id)
    action_context = " ".join(
        str(value)
        for value in [
            title,
            description,
            campaign.get("objective") if campaign else None,
            campaign.get("category") if campaign else None,
            campaign.get("offer") if campaign else None,
        ]
        if value
    )
    candidates = [
        (("쿠폰", "할인", "혜택", "바우처", "타임딜"), "혜택보기"),
        (("장바구니", "cart"), "담으러가기"),
        (("리뷰", "review"), "리뷰쓰기"),
        (("구독", "subscription"), "구독하기"),
        (("신청", "lead"), "신청하기"),
        (("예약", "travel"), "예약하기"),
        (("가이드", "consideration"), "자세히보기"),
        (("앱", "app_conversion"), "앱에서보기"),
    ]
    for terms, button_name in candidates:
        if any(term in action_context for term in terms):
            return _fit_rcs_button_name(button_name, max_chars)
    return _fit_rcs_button_name("자세히보기", max_chars)


def _fit_rcs_button_name(button_name: str, max_chars: int | None) -> str:
    if max_chars is None or len(button_name) <= max_chars:
        return button_name
    fallback_names = ["혜택보기", "보러가기", "자세히"]
    for fallback_name in fallback_names:
        if len(fallback_name) <= max_chars:
            return fallback_name
    return button_name[:max_chars]


def _find_message_campaign(campaigns: list[dict[str, Any]], source_campaign_id: Any) -> dict[str, Any] | None:
    for campaign in campaigns:
        if campaign.get("campaign_id") == source_campaign_id:
            return campaign
    return campaigns[0] if len(campaigns) == 1 else None


def _message_combined_text(message: dict[str, Any]) -> str:
    values = [message.get("text"), message.get("title"), message.get("description")]
    for button in message.get("buttons", []):
        if isinstance(button, dict):
            values.append(button.get("name"))
    return " ".join(str(value) for value in values if value)


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _carrier_message_byte_count(text: str) -> int:
    return sum(2 if _is_carrier_double_byte_char(char) else 1 for char in text)


def _is_carrier_double_byte_char(char: str) -> bool:
    code_point = ord(char)
    return (
        0x1100 <= code_point <= 0x11FF
        or 0x3130 <= code_point <= 0x318F
        or 0xAC00 <= code_point <= 0xD7AF
        or 0x2E80 <= code_point <= 0xA4CF
        or 0xF900 <= code_point <= 0xFAFF
        or 0xFE10 <= code_point <= 0xFE6F
        or 0xFF00 <= code_point <= 0xFFEF
    )


def _is_korean_only_text(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    return bool(letters) and all("가" <= char <= "힣" for char in letters)


def _message_issue(path: str, reason: str) -> dict[str, str]:
    return {"path": path, "reason": reason}
