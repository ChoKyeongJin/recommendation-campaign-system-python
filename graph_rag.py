from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import contextvars
import functools
import json
import math
import os
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from string import Template
from typing import Any

import networkx as nx
from fastembed import TextEmbedding
from qdrant_client import QdrantClient

from formula_engine import DEFAULT_METRIC_LEXICON_PATH, compile_formula_ast, parse_computed_metrics_from_query, validate_formula_ast
from set_expression_engine import parse_set_expressions_from_query
from sql_guard import (
    DEFAULT_LIMIT,
    DEFAULT_SCHEMA_PATH,
    infer_target_connection,
    load_allowed_tables,
    load_table_databases,
    load_table_dialects,
    validate_sql,
)


DEFAULT_DATA_PATH = Path("docs/data/rag_knowledge_base.json")
DEFAULT_NORMALIZATION_PATH = Path("docs/data/normalization_rules.sample.json")
DEFAULT_POLICY_PATH = Path("docs/data/business_policies.sample.json")
DEFAULT_DIMENSION_CATALOG_PATH = Path("docs/data/dimension_catalog.sample.json")
DEFAULT_COLLECTION = "campaign_knowledge_rag"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_LLM_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_PROMPT_DIR = Path(os.getenv("GRAPH_RAG_PROMPT_DIR", "docs/prompts"))
DEFAULT_MESSAGE_POLICY_PATH = Path(os.getenv("GRAPH_RAG_MESSAGE_POLICY", "docs/policies/message-policy.json"))
DEFAULT_RAG_LLM_LOG_DIR = Path(os.getenv("RAG_LLM_LOG_DIR", "logs/rag_llm"))

GENDER_TERMS = {"male", "female"}
LIFECYCLE_TERMS = {"new_user", "inactive_90d", "inactive_180d", "dormant", "vip", "app_user", "welcome_grade", "family_grade", "silver_grade", "gold_grade"}
CAMPAIGN_OBJECTIVES = {"purchase", "repurchase", "retention", "reactivation", "subscription", "awareness"}
# ── 실회원(CRM_MB_BASEINFO) 타겟 속성 레지스트리 ──────────────────────────────
# recommend_campaign 의 타겟을 데모 스키마(users/campaigns) 대신 실회원 테이블로 추출하기 위한
# "조건 -> 실컬럼 술어" 매핑의 단일 출처. 새 속성/값 지원은 코드 분기가 아니라 여기에 항목만 추가하면
# 되고, 조합은 compile_member_target_conditions 가 자동 처리한다(포함/제외/연령 등 임의 조합).
#
# MEMBER_EQ_FILTERS: canonical 값 -> (범주, 실컬럼, 저장값). 포함은 `=`, 제외는 `<>` 로 자동 생성.
#   저장값은 코드도메인 접두어를 포함한다(실DB 조회로 확인: GENDER_CD.FEMALE / MEM_GRADE_CD.VIP /
#   MEMBER_STATE_CD.SLEEP / DEVICE_TYPE_CD.APP). 범주 state 는 회원상태 직접 지정(기본 NORMAL 한정 해제).
# MEMBER_ACTIVITY_FILTERS: canonical -> 미접속 일수. LAST_LOGIN_DATE(YYYYMMDD 문자열) 사전식 비교.
#   범위 조건이라 제외(부정)는 의미가 모호해 미지원(→ fallback).
# new_user 는 REG_TYPE_CD.NEW 가 전체의 96%라 무의미하고 기간 기준이 미정의라 미매핑(→ fallback).
MEMBER_EQ_FILTERS: dict[str, tuple[str, str, str]] = {
    "female": ("gender", "B.GENDER_CD", "GENDER_CD.FEMALE"),
    "male": ("gender", "B.GENDER_CD", "GENDER_CD.MALE"),
    # 회원 등급 EMART_GRADE_CD (실DB 실측 정상회원: SILVER>FAMILY>WELCOME>GOLD>VIP 순 규모).
    # 같은 컬럼(grade)에 값이 여러 개면 compile_member_target_conditions 가 IN 으로 묶는다(OR).
    "welcome_grade": ("grade", "B.EMART_GRADE_CD", "MEM_GRADE_CD.WELCOME"),
    "family_grade": ("grade", "B.EMART_GRADE_CD", "MEM_GRADE_CD.FAMILY"),
    "silver_grade": ("grade", "B.EMART_GRADE_CD", "MEM_GRADE_CD.SILVER"),
    "gold_grade": ("grade", "B.EMART_GRADE_CD", "MEM_GRADE_CD.GOLD"),
    "vip": ("grade", "B.EMART_GRADE_CD", "MEM_GRADE_CD.VIP"),
    "dormant": ("state", "B.MEMBER_STATE_CD", "MEMBER_STATE_CD.SLEEP"),
    "app_user": ("channel", "B.LAST_LOGIN_CHANNEL", "DEVICE_TYPE_CD.APP"),
}
MEMBER_ACTIVITY_FILTERS: dict[str, int] = {"inactive_90d": 90, "inactive_180d": 180}


def _member_eq_predicate(canonical: str, negate: bool = False) -> str | None:
    entry = MEMBER_EQ_FILTERS.get(canonical)
    if entry is None:
        return None
    _, column, value = entry
    return column + (" <> " if negate else " = ") + _sql_quote(value)


def _member_activity_predicate(days: int) -> str:
    return f"(B.LAST_LOGIN_DATE IS NOT NULL AND B.LAST_LOGIN_DATE <= CONVERT(CHAR(8), DATEADD(DAY, -{days}, GETDATE()), 112))"
BEHAVIOR_TERMS = {
    "no_purchase",
    "first_purchase",
    "cart_abandoner",
    "repeat_buyer",
    "review_likely",
    "office_worker",
    "student",
    "gift_buyer",
}
CATEGORY_TERMS = {
    "fashion",
    "beauty",
    "electronics",
    "food",
    "home_living",
    "travel",
    "sports",
    "outdoor",
    "eco",
    "health_food",
    "digital_content",
    "global_shopping",
}
INTEREST_TERMS = CATEGORY_TERMS | {"parent", "pet_owner"}
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
CHANNEL_TERMS = {"app_push", "kakao", "email", "sms", "instagram", *MESSAGE_CHANNEL_TERMS}
OFFER_TERMS = {"coupon", "free_shipping", "subscription"}

NORMALIZATION_COLUMN_HINTS = {
    "gender_male": ["users.gender"],
    "gender_female": ["users.gender"],
    "new_user": ["users.lifecycle", "campaign_target_segments.target_segment"],
    "no_purchase": ["campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "first_purchase": ["campaigns.objective", "campaign_target_segments.target_segment"],
    "cart_abandoner": ["users.lifecycle", "campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "inactive_90d": ["users.lifecycle", "campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "inactive_180d": ["users.lifecycle", "users.last_login_at", "campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "dormant": ["users.lifecycle", "users.last_login_at", "campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "vip": ["users.lifecycle", "users.predicted_ltv_segment", "campaign_target_segments.target_segment"],
    "price_sensitive": ["users.price_sensitivity", "campaign_target_segments.target_segment"],
    "premium_buyer": ["campaign_target_segments.target_segment", "users.predicted_ltv_segment"],
    "repeat_buyer": ["campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "review_likely": ["campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "app_user": ["users.lifecycle", "campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "office_worker": ["campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "student": ["campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "parent": ["campaign_target_segments.target_segment", "user_interests.interest"],
    "pet_owner": ["campaign_target_segments.target_segment", "user_interests.interest"],
    "gift_buyer": ["campaign_target_segments.target_segment", "user_recent_behaviors.behavior"],
    "fashion": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "beauty": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "electronics": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "food": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "home_living": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "travel": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "sports": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "outdoor": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "eco": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "health_food": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "digital_content": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "global_shopping": ["campaigns.category", "user_interests.interest", "campaign_keywords.keyword"],
    "awareness": ["campaigns.objective", "campaign_keywords.keyword"],
    "app_push": ["campaign_channels.channel", "user_preferred_channels.preferred_channel"],
    "kakao": ["campaign_channels.channel", "user_preferred_channels.preferred_channel"],
    "email": ["campaign_channels.channel", "user_preferred_channels.preferred_channel"],
    "sms": ["campaign_channels.channel", "user_preferred_channels.preferred_channel"],
    "instagram": ["campaign_channels.channel", "user_preferred_channels.preferred_channel"],
    "lms": ["campaign_channels.channel", "user_preferred_channels.preferred_channel"],
    "rcs": ["campaign_channels.channel", "user_preferred_channels.preferred_channel"],
    "coupon": ["campaigns.offer", "campaign_keywords.keyword"],
    "free_shipping": ["campaigns.offer", "campaign_keywords.keyword"],
    "subscription": ["campaigns.objective", "campaigns.offer", "campaign_keywords.keyword"],
}


@dataclass(frozen=True)
class SearchHit:
    node_id: str
    score: float
    payload: dict[str, Any]


def _rag_llm_log_enabled() -> bool:
    value = os.getenv("RAG_LLM_LOG_ENABLED", "true").strip().casefold()
    return value not in {"0", "false", "no", "off"}


def _rag_llm_log_dir() -> Path:
    configured_dir = os.getenv("RAG_LLM_LOG_DIR")
    return Path(configured_dir) if configured_dir else DEFAULT_RAG_LLM_LOG_DIR


# 캠페인 생성(프롬프트 1건 = retrieve() 1회) 단위로 로그 파일을 분리하기 위한 실행 스코프.
# 값이 설정돼 있으면 해당 실행의 모든 이벤트가 같은 파일(<날짜>/<시각-해시>.jsonl)에 기록된다.
_rag_llm_run_path: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "_rag_llm_run_path", default=None
)


@contextlib.contextmanager
def rag_llm_run_scope():
    """retrieve() 한 번을 하나의 캠페인 로그 파일로 묶는 컨텍스트."""
    if not _rag_llm_log_enabled():
        yield None
        return
    now = datetime.now().astimezone()
    run_key = f"{now.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"
    log_path = _rag_llm_log_dir() / now.date().isoformat() / f"{run_key}.jsonl"
    token = _rag_llm_run_path.set(log_path)
    try:
        yield log_path
    finally:
        _rag_llm_run_path.reset(token)


def _write_rag_llm_log(event: str, payload: dict[str, Any]) -> None:
    if not _rag_llm_log_enabled():
        return
    try:
        now = datetime.now().astimezone()
        log_path = _rag_llm_run_path.get()
        if log_path is None:
            # 실행 스코프 밖에서 호출된 경우 기존과 동일하게 날짜별 파일로 남긴다.
            log_path = _rag_llm_log_dir() / f"{now.date().isoformat()}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": now.isoformat(timespec="milliseconds"),
            "event": event,
            **payload,
        }
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, default=_json_log_default) + "\n")
    except Exception as exc:
        print(f"rag_llm_log_failed:{exc.__class__.__name__}", flush=True)


def _json_log_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(str(item) for item in value)
    return str(value)


def _message_summary(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "role": message.get("role"),
            "content_length": len(str(message.get("content") or "")),
        }
        for message in messages
    ]


def load_payload(data_path: Path) -> dict[str, Any]:
    return json.loads(data_path.read_text(encoding="utf-8"))


def build_graph(payload: dict[str, Any]) -> nx.Graph:
    graph = nx.Graph()
    nodes = payload.get("nodes", [])
    nodes_by_id = {node["id"]: node for node in nodes}

    for node in nodes:
        graph.add_node(
            node["id"],
            node_type=node["type"],
            title=_node_title(node),
            text=node.get("text_for_embedding", ""),
            payload=node,
        )

    for node in nodes:
        if node["type"] == "schema_table":
            _add_schema_edges(graph, node, nodes_by_id)
        elif node["type"] == "business_term":
            _add_business_term_edges(graph, node)
        elif node["type"] == "business_policy":
            _add_business_policy_edges(graph, node)
        elif node["type"] == "metric_alias":
            _add_metric_alias_edges(graph, node)
        elif node["type"] == "normalization_rule":
            _add_normalization_edges(graph, node)
        elif node["type"] == "dimension":
            _add_dimension_edges(graph, node)
        elif node["type"] == "dimension_value":
            _add_dimension_value_edges(graph, node)
        elif node["type"] == "sql_example":
            _add_sql_example_edges(graph, node)
        elif node["type"] == "campaign":
            _add_campaign_edges(graph, node)
        elif node["type"] == "user":
            _add_user_edges(graph, node)

    _add_recommendation_edges(graph, payload.get("recommendation_edges", []))

    return graph


def _add_recommendation_edges(graph: nx.Graph, recommendation_edges: Any) -> None:
    if not isinstance(recommendation_edges, list):
        return

    for edge in recommendation_edges:
        if not isinstance(edge, dict):
            continue
        user_id = edge.get("user_id")
        campaign_id = edge.get("campaign_id")
        if not (isinstance(user_id, str) and user_id in graph):
            continue
        if not (isinstance(campaign_id, str) and campaign_id in graph):
            continue
        graph.add_edge(
            user_id,
            campaign_id,
            relation="recommended_campaign",
            reason=edge.get("reason"),
            label=edge.get("label"),
        )


def _prompt_normalize_system_prompt(prompt_dir: Path | None = DEFAULT_PROMPT_DIR) -> str:
    fallback = "\n".join(
        [
            "너는 캠페인 타겟팅 프롬프트 전처리기다.",
            "사용자 입력의 오타/띄어쓰기/맞춤법만 보수적으로 교정한다.",
            "의미·의도·타겟 조건을 절대 추가/삭제/변경하지 않는다(없는 조건을 지어내지 말 것).",
            "확실하지 않으면 원문을 그대로 둔다.",
            '다음 JSON object 만 출력한다: {"normalized_prompt": "교정된 문장", "summary": "한 줄 요약", "corrections": ["교정 항목", ...]}.',
        ]
    )
    return _read_prompt_template(prompt_dir, "prompt_normalize_system.txt", fallback)


def normalize_prompt(
    query: str,
    parser: str = "rules",
    llm_model: str = DEFAULT_LLM_MODEL,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> dict[str, Any]:
    """다운스트림 파싱 전에 사용자 프롬프트를 보수적으로 정리한다(오타/띄어쓰기 교정 + 한 줄 요약).

    의도·타겟 조건은 바꾸지 않는다(세그먼트 추가/삭제/재해석 금지). LLM 사용 불가/실패 시 공백만
    정리하는 규칙 fallback 을 쓴다. 원문(original)은 항상 보존해 감사·표시에 사용한다.
    반환: {original, normalized, summary, corrections, mode}.
    """
    original = query if isinstance(query, str) else ""
    rule_cleaned = re.sub(r"\s+", " ", original).strip()
    fallback = {
        "original": original,
        "normalized": rule_cleaned or original,
        "summary": "",
        "corrections": [],
        "mode": "rules",
    }
    # 규칙 전용 모드거나 LLM 사용 불가하면 공백 정리만 한다(원문 의미는 그대로).
    if parser.casefold() == "rules" or not os.getenv("OPENAI_API_KEY") or not rule_cleaned:
        return fallback
    try:
        from openai import OpenAI
    except ImportError:
        return fallback
    try:
        client = OpenAI()
        messages = [
            {"role": "system", "content": _prompt_normalize_system_prompt(prompt_dir)},
            {"role": "user", "content": original},
        ]
        response = client.chat.completions.create(
            model=llm_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=messages,
        )
        data = json.loads(response.choices[0].message.content or "{}")
        normalized = data.get("normalized_prompt")
        if not isinstance(normalized, str) or not normalized.strip():
            return fallback
        corrections = (
            [item for item in data.get("corrections", []) if isinstance(item, str) and item.strip()]
            if isinstance(data.get("corrections"), list)
            else []
        )
        summary = data.get("summary").strip() if isinstance(data.get("summary"), str) else ""
        result = {
            "original": original,
            "normalized": normalized.strip(),
            "summary": summary,
            "corrections": corrections,
            "mode": "llm",
        }
        _write_rag_llm_log("prompt_normalization", result)
        return result
    except Exception as exc:
        # 정리 실패는 치명적이지 않다(원문/규칙 정리본으로 계속 진행).
        return {**fallback, "mode": "rules_fallback", "error": exc.__class__.__name__}


# 대상 지향 표지: 이 뒤부터는 "누구에게 무엇을 한다"의 캠페인/채널·메시지 절로 본다.
_AUDIENCE_DIRECTION_MARKERS = ("에게", "한테", "께", "대상으로", "타겟으로", "타깃으로")
# 채널/메시지 의도 신호. 규칙 분리 실패(표지 없음) 판정과 LLM 폴백 트리거에 쓴다.
_CHANNEL_SIGNAL_WORDS = (
    "홍보", "광고", "알림", "알리", "안내", "소식", "공지", "캠페인",
    "메시지", "발송", "보내", "판매", "팔", "프로모션", "쿠폰", "이벤트",
)


def _prompt_scope_split_system_prompt(prompt_dir: Path | None = DEFAULT_PROMPT_DIR) -> str:
    fallback = "\n".join(
        [
            "너는 캠페인 프롬프트를 '타겟팅(오디언스 조건)'과 '채널(발송·메시지 의도)'로 분리하는 분류기다.",
            "타겟팅: 누구를 뽑을지(속성/구매이력/세그먼트 등 오디언스 정의)만.",
            "채널: 그들에게 무엇을 어떻게 알릴지(홍보/판매/알림/채널/메시지/혜택).",
            "원문 표현을 그대로 나눠 담고 의미를 새로 지어내지 않는다. 한쪽이 없으면 빈 문자열로 둔다.",
            '다음 JSON object 만 출력한다: {"targeting": "…", "channel": "…"}.',
        ]
    )
    return _read_prompt_template(prompt_dir, "prompt_scope_split_system.txt", fallback)


def _rule_split_prompt_scopes(text: str) -> tuple[str, str] | None:
    """대상 지향 표지(에게/한테/…) 첫 등장 지점 기준으로 앞=타겟팅, 뒤=채널 로 나눈다.

    "[오디언스]에게 [채널/메시지 액션]" 구조를 이용한다. 표지가 없거나 타겟팅 절이 비면 None(규칙 실패).
    """
    pattern = r"(?P<targeting>.*?(?:%s))\s*(?P<channel>.*)$" % "|".join(_AUDIENCE_DIRECTION_MARKERS)
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    targeting = match.group("targeting").strip()
    channel = match.group("channel").strip()
    if len(targeting) < 2:
        return None
    return targeting, channel


def _has_channel_signal(text: str) -> bool:
    compact = text.replace(" ", "").casefold()
    return any(word in compact for word in _CHANNEL_SIGNAL_WORDS)


def _llm_split_prompt_scopes(
    text: str, parser: str, llm_model: str, prompt_dir: Path | None
) -> dict[str, str] | None:
    """LLM 으로 프롬프트를 타겟팅/채널 두 절로 의미 분리한다. 사용 불가/실패 시 None."""
    if parser.casefold() == "rules" or not os.getenv("OPENAI_API_KEY") or not text.strip():
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    try:
        client = OpenAI()
        response = client.chat.completions.create(
            model=llm_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _prompt_scope_split_system_prompt(prompt_dir)},
                {"role": "user", "content": text},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        targeting = data.get("targeting")
        channel = data.get("channel")
        if not isinstance(targeting, str) or not targeting.strip():
            return None
        result = {"targeting": targeting.strip(), "channel": channel.strip() if isinstance(channel, str) else ""}
        _write_rag_llm_log("prompt_scope_split", {"text": text, **result})
        return result
    except Exception:
        return None


def split_prompt_scopes(
    text: str,
    parser: str = "rules",
    llm_model: str = DEFAULT_LLM_MODEL,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> dict[str, Any]:
    """프롬프트를 타겟팅(오디언스) 절과 채널(발송·메시지) 절로 분리한다.

    규칙 분리(대상 지향 표지)를 먼저 쓰고, 표지가 없어 못 나눴는데 채널 신호가 있으면 LLM 의미 분리로
    보완한다. 검색·그래프 컨텍스트를 스코프별로 좁히는 용도이며 SQL/Query Plan 에는 영향을 주지 않는다.
    반환: {targeting, channel, mode}.
    """
    original = text if isinstance(text, str) else ""
    rule = _rule_split_prompt_scopes(original)
    # 규칙으로 채널 절을 얻었거나, 애초에 채널 신호가 없어 전부 타겟팅이면 규칙 결과를 그대로 쓴다.
    if rule is not None and (rule[1] or not _has_channel_signal(original)):
        return {"targeting": rule[0], "channel": rule[1], "mode": "rules"}
    # 규칙이 제대로 못 나눴고(표지 없음/채널 절 공백) 채널 신호가 있으면 LLM 의미 분리 시도.
    llm = _llm_split_prompt_scopes(original, parser, llm_model, prompt_dir)
    if llm is not None:
        return {**llm, "mode": "llm"}
    if rule is not None:
        return {"targeting": rule[0], "channel": rule[1], "mode": "rules"}
    # 최종 폴백: 전부 타겟팅(채널 비움). 검색이 좁아질 순 있어도 채널 노드 오염은 생기지 않는다.
    return {"targeting": original, "channel": "", "mode": "rules_fallback"}


def _attach_retrieval_scopes(plan: dict[str, Any], scopes: dict[str, str]) -> None:
    """분리된 타겟팅/채널 절을 기준으로 retrieval 을 스코프별(query·terms)로 분해해 plan 에 부착한다.

    canonical 값(female/vip/purchase 등)은 한글 원문에 안 나타나므로 범주로 분류하고(채널=채널/혜택/목적),
    그 외 원문 토큰은 어느 절에 등장하는지로 나눈다. build_query_plan(전체 문장) 결과는 그대로 두고
    검색 단계에서만 골라 쓴다.
    """
    targeting_text = scopes.get("targeting") or ""
    channel_text = scopes.get("channel") or ""
    retrieval = plan.setdefault("retrieval", {})
    retrieval.setdefault("query", targeting_text)
    retrieval.setdefault("terms", [])
    channel_canonicals = CHANNEL_TERMS | OFFER_TERMS | CAMPAIGN_OBJECTIVES
    # 한글 토큰은 조사가 붙어 정확일치가 안 되므로, 공백 제거한 각 절 텍스트에 대한 '부분문자열' 포함으로 판정한다.
    targeting_compact = targeting_text.replace(" ", "").casefold()
    channel_compact = channel_text.replace(" ", "").casefold()

    targeting_terms: list[str] = []
    channel_terms: list[str] = []
    for term in retrieval["terms"]:
        lowered = term.casefold()
        if term in channel_canonicals:
            channel_terms.append(term)
        elif lowered in channel_compact and lowered not in targeting_compact:
            # 채널 절에만 등장하는 표현(홍보/캠페인/신상 등)은 채널로. 양쪽에 있으면 타겟팅 우선.
            channel_terms.append(term)
        else:
            # 타겟팅 절에 있거나(기저귀/구매 등), 원문에 안 드러나는 타겟 canonical(female/vip 등)은 타겟팅.
            targeting_terms.append(term)

    plan["retrieval"]["scope_mode"] = scopes.get("mode", "rules")
    plan["retrieval"]["targeting_query"] = targeting_text or plan["retrieval"]["query"]
    plan["retrieval"]["channel_query"] = channel_text
    plan["retrieval"]["targeting_terms"] = _unique_strings(targeting_terms)
    plan["retrieval"]["channel_terms"] = _unique_strings(channel_terms)


def build_query_plan(
    query: str,
    normalization_rules: Path | None = DEFAULT_NORMALIZATION_PATH,
    business_policies: Path | None = DEFAULT_POLICY_PATH,
    metric_lexicon: Path = DEFAULT_METRIC_LEXICON_PATH,
    sql_schema: Path = DEFAULT_SCHEMA_PATH,
    parser: str = "rules",
    llm_model: str = DEFAULT_LLM_MODEL,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> dict[str, Any]:
    parser = parser.casefold()
    if parser not in {"rules", "auto", "llm"}:
        raise ValueError("query parser must be one of: rules, auto, llm.")

    # 검색·그래프 컨텍스트 스코핑용 타겟팅/채널 절 분리(전체 문장 파싱·SQL 에는 영향 없음).
    scopes = split_prompt_scopes(query, parser=parser, llm_model=llm_model, prompt_dir=prompt_dir)

    rules_plan = _build_rule_query_plan(
        query,
        normalization_rules=normalization_rules,
        business_policies=business_policies,
        metric_lexicon=metric_lexicon,
        sql_schema=sql_schema,
    )
    if parser == "rules":
        rules_plan["parser"] = {"type": "rules", "fallback_used": False}
        _attach_retrieval_scopes(rules_plan, scopes)
        return rules_plan

    llm_plan, failure_reason = _try_llm_query_plan(query, rules_plan, llm_model, prompt_dir, sql_schema)
    if llm_plan is None:
        rules_plan["parser"] = {
            "type": "rules",
            "requested": parser,
            "fallback_used": True,
            "fallback_reason": failure_reason or "llm_query_parser_unavailable",
        }
        _attach_retrieval_scopes(rules_plan, scopes)
        return rules_plan

    llm_plan["parser"] = {
        "type": "llm",
        "requested": parser,
        "fallback_used": False,
        "model": llm_model,
    }
    # 디멘션 값(브랜드명)→코드 해석과 판매 상품 추출은 프롬프트 텍스트에서 결정론적으로 뽑으므로,
    # LLM 플랜에도 동일하게 적용해 rules/llm 어느 경로든 동일한 타겟팅/메시지 컨텍스트를 보장한다.
    llm_plan.setdefault("campaign_constraints", {}).setdefault("sell_object", None)
    _apply_sell_object(query, llm_plan)
    _apply_dimension_filters(query, llm_plan)
    # 구매 상품(purchase_object)도 프롬프트 텍스트에서 결정론적으로 뽑아, rules/llm 어느 경로든 동일하게
    # 상품 구매 이력 타겟팅(build_purchase_history_targets_sql_candidate)으로 이어지게 한다.
    _apply_purchase_object_filter(query, llm_plan.setdefault("target_user", {}))
    _attach_retrieval_scopes(llm_plan, scopes)
    return llm_plan


def _build_rule_query_plan(
    query: str,
    normalization_rules: Path | None = DEFAULT_NORMALIZATION_PATH,
    business_policies: Path | None = DEFAULT_POLICY_PATH,
    metric_lexicon: Path = DEFAULT_METRIC_LEXICON_PATH,
    sql_schema: Path = DEFAULT_SCHEMA_PATH,
) -> dict[str, Any]:
    normalized_query = query
    matches: list[dict[str, str]] = []

    if normalization_rules and normalization_rules.exists():
        from ingest import NormalizationIngester

        normalized = NormalizationIngester.from_file(normalization_rules).normalize_text(query)
        normalized_query = normalized["text"]
        matches = normalized["matches"]

    plan: dict[str, Any] = {
        "intent": _infer_intent(query),
        "target_user": {
            "gender": None,
            "age_min": None,
            "age_max": None,
            "lifecycle": [],
            "interests": [],
            "preferred_channels": [],
            "behaviors": [],
            "purchase_object": None,
            "price_sensitivity": None,
            "inactivity_period": None,
        },
        "exclude": {"gender": [], "interests": [], "lifecycle": []},
        "campaign_constraints": {
            "category": [],
            "objective": _infer_objective(query),
            "offer_type": None,
            "channels": [],
            "sell_object": None,
        },
        "retrieval": {
            "query": normalized_query,
            "terms": [],
        },
        "matched_terms": [],
        "policy_constraints": [],
        "semantic_resolutions": [],
        "computed_metrics": [],
        "dimension_filters": [],
        "cart_context": False,
        "set_expressions": parse_set_expressions_from_query(query, normalization_path=normalization_rules) if normalization_rules else [],
    }
    _apply_age_filters(query, plan["target_user"])
    _apply_purchase_object_filter(query, plan["target_user"])
    _apply_sell_object(query, plan)
    _apply_dimension_filters(query, plan)
    set_expression_terms = _set_expression_canonical_values(plan["set_expressions"])
    if any(term.startswith("age_") for term in set_expression_terms):
        plan["target_user"]["age_min"] = None
        plan["target_user"]["age_max"] = None

    for match in matches:
        canonical = match["normalized"]
        plan["matched_terms"].append(
            {
                "matched_text": match["matched_text"],
                "source_term": match["source_term"],
                "canonical": canonical,
                "rule_id": match["rule_id"],
                "match_type": match["match_type"],
            }
        )
        if canonical in set_expression_terms:
            continue
        inverse_canonical = _inverse_negative_synonym(canonical, match["match_type"])
        if inverse_canonical is not None:
            _apply_exclusion(plan, inverse_canonical)
        elif _is_exclusion_context(query, match["matched_text"], match["match_type"]):
            _apply_exclusion(plan, canonical)
        elif canonical in CHANNEL_TERMS and _is_delivery_channel_context(query, match["matched_text"]):
            # 발송 채널 표기("발송 채널: RCS")는 SQL에 전혀 반영하지 않는다. 오디언스 필터도,
            # 캠페인 채널 필터도 만들지 않고 그냥 버린다 — SQL은 캠페인 목표(objective)만 신경 쓴다.
            continue
        else:
            _apply_query_term(plan, canonical)

    _apply_cart_repurchase_context(query, plan)
    _apply_inactivity_period_filter(query, plan)
    _apply_policy_constraints(query, plan, business_policies)
    plan["computed_metrics"] = parse_computed_metrics_from_query(query, schema_path=sql_schema, metric_lexicon_path=metric_lexicon)
    policy_terms = [
        term
        for policy in plan["policy_constraints"]
        for term in (policy.get("canonical"), policy.get("metric"))
        if isinstance(term, str) and term
    ]
    semantic_terms = [
        term
        for resolution in plan["semantic_resolutions"]
        for term in (resolution.get("canonical"), resolution.get("ambiguous_term"), resolution.get("default_resolution"))
        if isinstance(term, str) and term
    ]
    computed_metric_terms = [
        term
        for metric in plan["computed_metrics"]
        for term in (metric.get("metric_id"), metric.get("ko_label"), metric.get("formula_text"))
        if isinstance(term, str) and term
    ]
    set_expression_terms = [
        term
        for expression in plan["set_expressions"]
        for term in _set_expression_retrieval_terms(expression)
    ]
    plan["retrieval"]["terms"] = _unique_strings(
        [match["canonical"] for match in plan["matched_terms"]]
        + policy_terms
        + semantic_terms
        + computed_metric_terms
        + set_expression_terms
        + _inactivity_retrieval_terms(plan["target_user"].get("inactivity_period"))
        + _query_tokens(normalized_query)
    )
    return plan


def _try_llm_query_plan(
    query: str,
    fallback_plan: dict[str, Any],
    llm_model: str,
    prompt_dir: Path | None,
    sql_schema: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    if not os.getenv("OPENAI_API_KEY"):
        return None, "missing_openai_api_key"

    try:
        from openai import OpenAI
    except ImportError as exc:
        return None, f"openai_import_failed:{exc.__class__.__name__}"

    try:
        client = OpenAI()
        messages = [
            {
                "role": "system",
                "content": _query_plan_system_prompt(prompt_dir),
            },
            {
                "role": "user",
                "content": _query_plan_user_prompt(query, fallback_plan, prompt_dir),
            },
        ]
        _write_rag_llm_log(
            "llm_query_plan_request",
            {
                "mode": "openai_chat_completion",
                "model": llm_model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "query": query,
                "fallback_plan": fallback_plan,
                "messages": messages,
                "message_summary": _message_summary(messages),
            },
        )
        response = client.chat.completions.create(
            model=llm_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=messages,
        )
        content = response.choices[0].message.content or "{}"
        query_plan = _coerce_llm_query_plan(json.loads(content), fallback_plan, sql_schema)
        _write_rag_llm_log(
            "llm_query_plan_response",
            {
                "mode": "openai_chat_completion",
                "model": llm_model,
                "query": query,
                "content": content,
                "query_plan": query_plan,
            },
        )
        return query_plan, None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        _write_rag_llm_log(
            "llm_query_plan_failure",
            {
                "mode": "openai_chat_completion",
                "model": llm_model,
                "query": query,
                "failure_reason": f"llm_query_parser_invalid_response:{exc.__class__.__name__}",
                "content": locals().get("content"),
            },
        )
        return None, f"llm_query_parser_invalid_response:{exc.__class__.__name__}"
    except Exception as exc:
        _write_rag_llm_log(
            "llm_query_plan_failure",
            {
                "mode": "openai_chat_completion",
                "model": llm_model,
                "query": query,
                "failure_reason": f"llm_query_parser_failed:{exc.__class__.__name__}",
            },
        )
        return None, f"llm_query_parser_failed:{exc.__class__.__name__}"


def _read_prompt_template(prompt_dir: Path | None, filename: str, fallback: str) -> str:
    # 1) DB(prompt_store 캐시) 우선
    db_template = _read_prompt_from_db(filename)
    if db_template:
        return db_template
    # 2) 파일(prompt_dir)
    if prompt_dir is not None:
        try:
            template = (prompt_dir / filename).read_text(encoding="utf-8").strip()
        except OSError:
            template = ""
        if template:
            return template
    # 3) 코드 내 하드코딩 fallback
    return fallback


def _read_prompt_from_db(filename: str) -> str | None:
    try:
        import prompt_store

        return prompt_store.get_template(filename)
    except Exception:  # noqa: BLE001 - DB 미가용 시 파일/하드코딩 fallback으로 진행
        return None


def _render_prompt_template(template: str, **values: str) -> str:
    return Template(template).safe_substitute(values)


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 2)


def _query_plan_system_prompt(prompt_dir: Path | None = DEFAULT_PROMPT_DIR) -> str:
    fallback = "\n".join(
        [
            "너는 캠페인 추천/NL2SQL Query Planner다.",
            "사용자 질문을 지정된 JSON 구조로만 반환한다.",
            "성별, 행동, 관심사, 채널, 혜택은 canonical 값만 사용한다.",
            "부정 조건은 target_user에 긍정 조건으로 바꾸지 말고 exclude에 넣는다.",
            "반드시 JSON object만 출력한다.",
        ]
    )
    return _read_prompt_template(prompt_dir, "query_plan_system.txt", fallback)


def _query_plan_user_prompt(
    query: str,
    fallback_plan: dict[str, Any],
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> str:
    allowed_values = {
        "gender": sorted(GENDER_TERMS),
        "lifecycle": sorted(LIFECYCLE_TERMS),
        "behaviors": sorted(BEHAVIOR_TERMS),
        "interests": sorted(INTEREST_TERMS),
        "channels": sorted(CHANNEL_TERMS),
        "offer_type": sorted(OFFER_TERMS),
        "objective": sorted(CAMPAIGN_OBJECTIVES),
    }
    fallback = "\n".join(
        [
            "[User Query]\n${query}",
            "",
            "[Allowed Canonical Values]",
            "${allowed_values}",
            "",
            "[Fallback Rules Plan]",
            "${fallback_plan}",
            "",
            "Fallback Rules Plan과 같은 JSON 구조로 보완된 Query Plan을 반환하라.",
        ]
    )
    template = _read_prompt_template(prompt_dir, "query_plan_user.txt", fallback)
    return _render_prompt_template(
        template,
        query=query,
        allowed_values=json.dumps(allowed_values, ensure_ascii=False, indent=2),
        fallback_plan=json.dumps(fallback_plan, ensure_ascii=False, indent=2),
    )


def _coerce_llm_query_plan(candidate: Any, fallback_plan: dict[str, Any], sql_schema: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    plan = json.loads(json.dumps(fallback_plan, ensure_ascii=False))
    if not isinstance(candidate, dict):
        return plan

    intent = candidate.get("intent")
    if intent in {"recommend_campaign", "find_user_segment", "unknown"}:
        plan["intent"] = intent

    target_user = candidate.get("target_user") if isinstance(candidate.get("target_user"), dict) else {}
    _merge_scalar(plan["target_user"], target_user, "gender", GENDER_TERMS)
    _merge_int(plan["target_user"], target_user, "age_min")
    _merge_int(plan["target_user"], target_user, "age_max")
    _merge_list(plan["target_user"], target_user, "lifecycle", LIFECYCLE_TERMS)
    _merge_list(plan["target_user"], target_user, "interests", INTEREST_TERMS)
    _merge_list(plan["target_user"], target_user, "preferred_channels", CHANNEL_TERMS)
    _merge_list(plan["target_user"], target_user, "behaviors", BEHAVIOR_TERMS)
    _merge_scalar(plan["target_user"], target_user, "price_sensitivity", {"high", "low"})

    exclude = candidate.get("exclude") if isinstance(candidate.get("exclude"), dict) else {}
    _merge_list(plan["exclude"], exclude, "gender", GENDER_TERMS)
    _merge_list(plan["exclude"], exclude, "interests", INTEREST_TERMS)
    _merge_list(plan["exclude"], exclude, "lifecycle", LIFECYCLE_TERMS)

    campaign_constraints = candidate.get("campaign_constraints") if isinstance(candidate.get("campaign_constraints"), dict) else {}
    _merge_list(plan["campaign_constraints"], campaign_constraints, "category", CATEGORY_TERMS)
    _merge_scalar(plan["campaign_constraints"], campaign_constraints, "objective", CAMPAIGN_OBJECTIVES)
    _merge_scalar(plan["campaign_constraints"], campaign_constraints, "offer_type", OFFER_TERMS)
    _merge_list(plan["campaign_constraints"], campaign_constraints, "channels", CHANNEL_TERMS)

    retrieval = candidate.get("retrieval") if isinstance(candidate.get("retrieval"), dict) else {}
    if isinstance(retrieval.get("query"), str) and retrieval["query"].strip():
        plan["retrieval"]["query"] = retrieval["query"].strip()
    if isinstance(retrieval.get("terms"), list):
        plan["retrieval"]["terms"] = _unique_strings(
            [*plan["retrieval"]["terms"], *[str(term).strip() for term in retrieval["terms"] if str(term).strip()]]
        )
    computed_metrics = candidate.get("computed_metrics")
    if isinstance(computed_metrics, list):
        coerced_metrics = [_coerce_llm_computed_metric(metric, sql_schema) for metric in computed_metrics]
        coerced_metrics = [metric for metric in coerced_metrics if metric is not None]
        if coerced_metrics:
            plan["computed_metrics"] = coerced_metrics
    set_expressions = candidate.get("set_expressions")
    if isinstance(set_expressions, list):
        coerced_set_expressions = [_coerce_llm_set_expression(expression) for expression in set_expressions]
        coerced_set_expressions = [expression for expression in coerced_set_expressions if expression is not None]
        if coerced_set_expressions:
            plan["set_expressions"] = coerced_set_expressions
    return plan


def _coerce_llm_set_expression(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, dict) or not isinstance(candidate.get("set_ast"), dict):
        return None
    return {
        "expression_id": candidate.get("expression_id") if isinstance(candidate.get("expression_id"), str) else "segment_set_expression",
        "ko_label": candidate.get("ko_label") if isinstance(candidate.get("ko_label"), str) else "세그먼트 집합식",
        "expression_text": candidate.get("expression_text") if isinstance(candidate.get("expression_text"), str) else "",
        "set_ast": candidate["set_ast"],
        "requires_clarification": bool(candidate.get("requires_clarification")),
        "clarification_question": candidate.get("clarification_question") if isinstance(candidate.get("clarification_question"), str) else None,
        "source": "llm_set_expression_ast",
    }


def _coerce_llm_computed_metric(candidate: Any, sql_schema: Path) -> dict[str, Any] | None:
    if not isinstance(candidate, dict) or not isinstance(candidate.get("formula_ast"), dict):
        return None
    metric_id = candidate.get("metric_id") if isinstance(candidate.get("metric_id"), str) else "computed_formula_score"
    metric_id = _safe_metric_alias(metric_id) or "computed_formula_score"
    behavior = candidate.get("sql_behavior") if candidate.get("sql_behavior") in {"select", "rank", "filter"} else "select"
    order_by = candidate.get("order_by") if candidate.get("order_by") in {"asc", "desc"} else None
    validation = validate_formula_ast(candidate["formula_ast"], schema_path=sql_schema)
    requires_clarification = bool(candidate.get("requires_clarification")) or not validation["is_valid"]
    clarification_question = candidate.get("clarification_question") if isinstance(candidate.get("clarification_question"), str) else None
    if requires_clarification and clarification_question is None:
        clarification_question = "계산식에 사용할 수 없는 컬럼이나 연산자가 포함되어 있습니다: " + "; ".join(validation["issues"])
    return {
        "metric_id": metric_id,
        "ko_label": candidate.get("ko_label") if isinstance(candidate.get("ko_label"), str) else "계산 점수",
        "formula_text": candidate.get("formula_text") if isinstance(candidate.get("formula_text"), str) else "",
        "formula_ast": candidate["formula_ast"],
        "sql_behavior": behavior,
        "operator": candidate.get("operator") if candidate.get("operator") in {"=", ">", ">=", "<", "<="} else None,
        "threshold": candidate.get("threshold") if isinstance(candidate.get("threshold"), int | float) else None,
        "order_by": order_by,
        "unit": candidate.get("unit") if isinstance(candidate.get("unit"), str) else None,
        "confidence": candidate.get("confidence") if isinstance(candidate.get("confidence"), int | float) else None,
        "requires_clarification": requires_clarification,
        "clarification_question": clarification_question,
        "source": "llm_formula_ast",
    }


def _merge_scalar(target: dict[str, Any], source: dict[str, Any], key: str, allowed_values: set[str]) -> None:
    value = source.get(key)
    if isinstance(value, str) and value in allowed_values:
        target[key] = value


def _merge_int(target: dict[str, Any], source: dict[str, Any], key: str) -> None:
    value = source.get(key)
    if isinstance(value, int) and 0 <= value <= 120:
        target[key] = value


def _merge_list(target: dict[str, Any], source: dict[str, Any], key: str, allowed_values: set[str]) -> None:
    values = source.get(key)
    if not isinstance(values, list):
        return
    canonical_values = [value for value in values if isinstance(value, str) and value in allowed_values]
    if canonical_values:
        target[key] = _unique_strings([*target.get(key, []), *canonical_values])


def _infer_intent(query: str) -> str:
    compact_query = query.replace(" ", "").casefold()
    if _is_reactivation_goal_context(query):
        return "recommend_campaign"
    if _is_cart_abandonment_query(query) and _is_repurchase_goal_context(query):
        return "recommend_campaign"
    # "…에게 신제품 출시 소식을 알리고 싶어요" 같은 (신제품)알림/홍보 아웃리치는 캠페인 목적이다.
    # "고객"이 있어도 단순 세그먼트 조회가 아니라 캠페인 발송이 목적이므로 아래 find_user_segment
    # 분기보다 먼저 recommend_campaign 으로 잡아야 메시지 생성(build_message_context)까지 이어진다.
    if _is_awareness_announcement_context(query):
        return "recommend_campaign"
    # "…고객에게 …을 팔고 싶어요 / 판매하고 싶어요" 같은 판매 아웃리치는 캠페인(발송) 목적이다.
    # "고객"이 있어도 단순 세그먼트 조회가 아니라 특정 상품을 파는 캠페인이므로 아래 find_user_segment
    # 분기보다 먼저 recommend_campaign 으로 잡아 메시지 생성(build_message_context)까지 이어지게 한다.
    if _is_sales_outreach_context(query):
        return "recommend_campaign"
    if any(keyword in compact_query for keyword in ("캠페인", "추천", "recommend", "campaign")):
        return "recommend_campaign"
    if any(keyword in compact_query for keyword in ("사용자", "고객", "사람", "지역", "세그먼트", "user", "segment", "region")):
        return "find_user_segment"
    return "unknown"


def _infer_objective(query: str) -> str | None:
    compact_query = query.replace(" ", "").casefold()
    if _is_repurchase_goal_context(query):
        return "repurchase"
    if _is_reactivation_goal_context(query):
        return "reactivation"
    if any(keyword in compact_query for keyword in ("구매", "구입", "전환", "매출", "purchase", "conversion", "판매", "팔고", "팔려", "sell")):
        return "purchase"
    if any(keyword in compact_query for keyword in ("구독", "subscription")):
        return "subscription"
    if any(keyword in compact_query for keyword in ("휴면", "복귀", "재방문", "reactivation")):
        return "reactivation"
    if "retention" in compact_query:
        return "retention"
    if any(keyword in compact_query for keyword in ("신제품", "신상품", "출시", "런칭", "awareness", "launch")):
        return "awareness"
    return None


def _is_awareness_announcement_context(query: str) -> bool:
    # 신제품/출시/런칭 등 인지(awareness) 키워드 + 알림/홍보 아웃리치 동사가 함께 있으면 캠페인 발송 의도.
    # "신제품 관심 고객 찾아줘"(조회)처럼 아웃리치 동사가 없으면 걸리지 않도록 둘 다 요구한다.
    compact_query = query.replace(" ", "").casefold()
    has_launch = any(keyword in compact_query for keyword in ("신제품", "신상품", "출시", "런칭", "launch", "awareness"))
    has_announce = any(keyword in compact_query for keyword in ("알리", "알림", "소식", "안내", "홍보"))
    return has_launch and has_announce


def _is_sales_outreach_context(query: str) -> bool:
    # 판매 동사(팔다/판매/sell) + 대상 지향(에게/한테/고객/대상/타겟)이 함께 있으면 특정 상품을
    # 파는 캠페인 발송 의도. "고객 찾아줘"(조회)처럼 판매 동사가 없으면 걸리지 않도록 둘 다 요구한다.
    # "팔레트/팔로우" 등 오탐을 피하려고 "팔" 단독이 아닌 "팔고/팔려/판매"만 판매 동사로 본다.
    compact_query = query.replace(" ", "").casefold()
    has_sell = any(keyword in compact_query for keyword in ("팔고", "팔려", "팔것", "판매", "sell"))
    has_audience = any(keyword in compact_query for keyword in ("에게", "한테", "고객", "대상", "타겟", "타깃"))
    return has_sell and has_audience


def _is_reactivation_goal_context(query: str) -> bool:
    compact_query = query.replace(" ", "").casefold()
    return any(
        keyword in compact_query
        for keyword in (
            "재활성",
            "다시활성",
            "활성화",
            "휴면복귀",
            "복귀캠페인",
            "reactivation",
            "reactivate",
        )
    )


def _apply_age_filters(query: str, target_user: dict[str, Any]) -> None:
    decade_range_match = re.search(r"(?P<min>[1-9]\d)\s*(?:~|-|부터)\s*(?P<max>[1-9]\d)\s*대", query)
    if decade_range_match:
        target_user["age_min"] = _valid_age(decade_range_match.group("min"))
        max_decade = _valid_age(decade_range_match.group("max"))
        target_user["age_max"] = max_decade + 9 if max_decade is not None else None
        return

    decade_matches = [int(match.group("decade")) for match in re.finditer(r"(?P<decade>[1-9]\d)\s*대", query)]
    if decade_matches:
        target_user["age_min"] = min(decade_matches)
        target_user["age_max"] = max(decade_matches) + 9

    range_match = re.search(r"(?P<min>\d{1,3})\s*(?:세)?\s*(?:~|-|부터)\s*(?P<max>\d{1,3})\s*세?", query)
    if range_match:
        target_user["age_min"] = _valid_age(range_match.group("min"))
        target_user["age_max"] = _valid_age(range_match.group("max"))

    min_match = re.search(r"(?P<age>\d{1,3})\s*세?\s*(?:이상|부터)", query)
    if min_match:
        target_user["age_min"] = _valid_age(min_match.group("age"))

    max_match = re.search(r"(?P<age>\d{1,3})\s*세?\s*(?:이하|까지)", query)
    if max_match:
        target_user["age_max"] = _valid_age(max_match.group("age"))


def _valid_age(value: str) -> int | None:
    age = int(value)
    return age if 0 <= age <= 120 else None


def _apply_purchase_object_filter(query: str, target_user: dict[str, Any]) -> None:
    # "…을/를 구매한/구입한/구매했던/구입하신 …" — 구매·구입은 동의어이므로 둘 다 상품 구매 이력으로 본다.
    # object 클래스에 공백을 넣지 않아 "를/을" 직전 상품 명사만 잡는다. (공백 허용 시 "40대 여성 중
    # 기저귀를 구매한" 처럼 앞 절 조건까지 삼켜 LIKE 가 무의미해지므로) 상품 카테고리 단어면 재현율에 충분하다.
    match = re.search(r"(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})\s*(?:을|를)?\s*(?:구매|구입)(?:한|했던|하신)", query, re.IGNORECASE)
    if not match:
        return
    purchase_object = _sanitize_purchase_object(match.group("object"))
    if purchase_object:
        target_user["purchase_object"] = purchase_object


def _apply_sell_object(query: str, plan: dict[str, Any]) -> None:
    # "…(신상 컴퓨터)를 팔고 싶어요 / 판매하고 싶어요" 에서 파는 상품을 뽑아 캠페인 목표로 쓴다.
    # 타겟 필터가 아니라 채널메시지 카피의 소재(캠페인 컨텍스트)로만 사용한다.
    match = re.search(r"(?P<object>.+?)\s*(?:을|를)\s*(?:팔|판매)", query)
    if not match:
        return
    fragment = match.group("object")
    # "…에게/…한테/…께/…대상으로" 같은 대상 지향 표현 뒤의 상품만 취해 대상 문구를 삼키지 않는다.
    fragment = re.split(r".*(?:에게|한테|께|대상으로)\s*", fragment)[-1]
    sell_object = _sanitize_purchase_object(fragment)
    if sell_object:
        plan["campaign_constraints"]["sell_object"] = sell_object


@functools.lru_cache(maxsize=8)
def _load_dimension_catalog(path: Path) -> tuple[dict[str, Any], ...]:
    if not path or not path.exists():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    dimensions = payload.get("dimensions", [])
    return tuple(dimension for dimension in dimensions if isinstance(dimension, dict) and dimension.get("dimension_id"))


@functools.lru_cache(maxsize=256)
def _resolve_dimension_values_cached(connection: str, ds_sql: str) -> tuple[tuple[str, str], ...]:
    # DS_SQL 을 실제 DB에 실행해 (코드, 이름) 쌍을 얻는다. 규약: 결과 첫 컬럼=코드, 둘째 컬럼=이름.
    # 값은 매우 많을 수 있어 정적 저장하지 않고 런타임에 조회한다(디멘션당 lru 캐시).
    from db_connections import run_read_query
    from sql_guard import validate_sql

    # SELECT 전용만 실행(직접 검증). enforce_select=False 로 원본을 그대로 실행해 dialect 별
    # 자동 LIMIT/TOP 부착(예: MSSQL 에서 'LIMIT' 구문 오류)과 값 목록 truncation 을 피한다.
    guard = validate_sql(ds_sql, allowed_tables=None)
    if any(issue["severity"] == "error" for issue in guard["issues"]):
        return ()
    rows = run_read_query(connection, ds_sql, enforce_select=False)
    pairs: list[tuple[str, str]] = []
    for row in rows:
        values = list(row.values()) if isinstance(row, dict) else list(row)
        if not values or values[0] is None:
            continue
        code = str(values[0]).strip()
        name = str(values[1]).strip() if len(values) > 1 and values[1] is not None else code
        if code:
            pairs.append((code, name))
    return tuple(pairs)


def _resolve_dimension_values(dimension: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    connection = dimension.get("connection")
    ds_sql = dimension.get("ds_sql")
    if not connection or not isinstance(ds_sql, str) or not ds_sql.strip():
        return ()
    try:
        return _resolve_dimension_values_cached(connection, ds_sql.strip())
    except Exception:
        # DB 드라이버 미설치/연결 실패/DS_SQL 오류 등은 조용히 건너뛴다(타겟팅만 비고 나머지는 정상).
        return ()


def _apply_dimension_filters(query: str, plan: dict[str, Any], dimension_catalog: Path | None = DEFAULT_DIMENSION_CATALOG_PATH) -> None:
    # 프롬프트에 디멘션 라벨(예: "상품브랜드")이 언급되면 그 디멘션의 DS_SQL 을 런타임에 실행해
    # 값 이름(예: "포멜카멜리")을 코드(예: 'A')로 동적 해석하고, 큐레이션된 타겟 컬럼이 있으면
    # 타겟팅 조건으로 넘긴다. 타겟 필터이지 캠페인 목표가 아니다.
    if dimension_catalog is None:
        return
    dimensions = _load_dimension_catalog(dimension_catalog)
    if not dimensions:
        return
    compact_query = query.replace(" ", "").casefold()
    filters = []
    for dimension in dimensions:
        synonyms = [synonym for synonym in dimension.get("synonyms", []) if isinstance(synonym, str) and synonym]
        # 프롬프트에 디멘션 라벨/동의어가 언급된 경우에만 값 해석(불필요한 DS_SQL 실행 방지).
        if not any(synonym.replace(" ", "").casefold() in compact_query for synonym in synonyms):
            continue
        codes: list[str] = []
        names: list[str] = []
        for code, name in _resolve_dimension_values(dimension):
            if name and name.replace(" ", "").casefold() in compact_query and code not in codes:
                codes.append(code)
                names.append(name)
        if codes:
            filters.append(
                {
                    "dimension_id": dimension.get("dimension_id"),
                    "prompt_label": dimension.get("prompt_label"),
                    "column": dimension.get("target_column"),
                    "table": dimension.get("target_table"),
                    "operator": dimension.get("operator", "IN"),
                    "codes": codes,
                    "names": names,
                }
            )
    if filters:
        plan["dimension_filters"] = filters
        plan["cart_context"] = "장바구니" in compact_query


def _cart_dimension_brand_filter(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    # 큐레이션된 타겟 매핑(상품브랜드 -> CRM_CM_PRODUCT.BRAND_ID)이 잡히고 장바구니 맥락일 때만
    # 실제 테이블 cart 타겟팅 템플릿으로 라우팅한다.
    if not query_plan.get("cart_context"):
        return None
    for dimension_filter in query_plan.get("dimension_filters", []):
        column = dimension_filter.get("column") or ""
        if dimension_filter.get("codes") and dimension_filter.get("table") == "CRM_CM_PRODUCT" and column.endswith("BRAND_ID"):
            return dimension_filter
    return None


def _is_cart_dimension_targeting(query_plan: dict[str, Any]) -> bool:
    return _cart_dimension_brand_filter(query_plan) is not None


def _apply_cart_repurchase_context(query: str, plan: dict[str, Any]) -> None:
    if _is_cart_abandonment_query(query):
        _append_unique(plan["target_user"]["behaviors"], "cart_abandoner")
    if _is_repurchase_goal_context(query):
        plan["campaign_constraints"]["objective"] = "repurchase"
        plan["target_user"]["behaviors"] = [
            behavior for behavior in plan["target_user"].get("behaviors", []) if behavior != "repeat_buyer"
        ]


def _apply_inactivity_period_filter(query: str, plan: dict[str, Any]) -> None:
    period = _parse_inactivity_period(query)
    if period is None:
        return
    plan["target_user"]["inactivity_period"] = period
    if period["min_days"] >= 180:
        plan["target_user"]["lifecycle"] = [
            lifecycle
            for lifecycle in plan["target_user"].get("lifecycle", [])
            if lifecycle not in {"inactive_90d", "inactive_180d", "dormant"}
        ]


def _parse_inactivity_period(query: str) -> dict[str, Any] | None:
    compact_query = query.replace(" ", "").casefold()
    if not any(
        keyword in compact_query
        for keyword in (
            "미접속",
            "접속하지않",
            "접속안",
            "로그인하지않",
            "로그인안",
            "휴면",
            "비활성",
            "inactive",
            "dormant",
        )
    ):
        return None

    month_match = re.search(r"(?P<value>\d{1,2})\s*(?:개월|달)\s*(?:이상|넘|초과|째|간)?", query)
    if month_match:
        months = int(month_match.group("value"))
        if months > 0:
            return {
                "value": months,
                "unit": "months",
                "min_days": months * 30,
                "sql_interval": f"{months} months",
            }

    day_match = re.search(r"(?P<value>\d{1,4})\s*일\s*(?:이상|넘|초과|째|간)?", query)
    if day_match:
        days = int(day_match.group("value"))
        if days > 0:
            return {
                "value": days,
                "unit": "days",
                "min_days": days,
                "sql_interval": f"{days} days",
            }

    return None


def _inactivity_retrieval_terms(period: Any) -> list[str]:
    if not isinstance(period, dict):
        return []
    terms = ["last_login_at", "last_active_days", "inactive", "dormant"]
    if period.get("min_days", 0) >= 180:
        terms.extend(["inactive_180d", "reactivation", "6개월", "미접속", "휴면"])
    return terms


def _is_cart_abandonment_query(query: str) -> bool:
    compact_query = query.replace(" ", "").casefold()
    return "장바구니" in compact_query and any(
        keyword in compact_query
        for keyword in (
            "결제하지않",
            "결제안",
            "미결제",
            "구매하지않",
            "구매안",
            "안산",
            "방치",
            "이탈",
            "cartabandon",
        )
    )


def _is_repurchase_goal_context(query: str) -> bool:
    compact_query = query.replace(" ", "").casefold()
    if not any(keyword in compact_query for keyword in ("재구매", "repurchase")):
        return False
    return any(keyword in compact_query for keyword in ("유도", "촉진", "리마인드", "캠페인", "메시지", "발송", "추천"))


def _sanitize_purchase_object(value: str) -> str | None:
    tokens = []
    for token in re.findall(r"[0-9A-Za-z가-힣_+\-]+", value.casefold()):
        stripped_token = re.sub(r"(?:을|를)$", "", token)
        if stripped_token and stripped_token not in {"사람", "고객", "사용자"}:
            tokens.append(stripped_token)
    if not tokens:
        return None
    return " ".join(tokens[-3:])[:40]


def _is_exclusion_context(query: str, matched_text: str, match_type: str) -> bool:
    lowered_query = query.casefold()
    match_index = lowered_query.find(matched_text.casefold())
    if match_index < 0:
        return False

    match_end = match_index + len(matched_text)
    before_window = lowered_query[max(0, match_index - 8) : match_index]
    after_window = lowered_query[match_end : match_end + 12]
    return any(marker in after_window for marker in ("제외", "빼고", "말고", "아닌", "아니고")) or any(
        marker in before_window for marker in ("not ", "except ", "exclude ")
    )


def _is_delivery_channel_context(query: str, matched_text: str) -> bool:
    # "발송 채널: RCS (리치 메시지 ...)" 처럼 발송/전송 채널을 표기한 문맥이면 True.
    # 이 경우 채널은 타겟팅 조건이 아니라 발송 채널일 뿐이므로 SQL 생성에서 제외한다.
    lowered_query = query.casefold()
    match_index = lowered_query.find(matched_text.casefold())
    if match_index < 0:
        return False
    line_start = lowered_query.rfind("\n", 0, match_index) + 1
    line_end = lowered_query.find("\n", match_index)
    line = lowered_query[line_start : line_end if line_end != -1 else len(lowered_query)]
    return any(
        marker in line
        for marker in ("발송 채널", "발송채널", "전송 채널", "전송채널", "발신 채널", "발신채널", "보낼 채널")
    )


def _inverse_negative_synonym(canonical: str, match_type: str) -> str | None:
    if match_type != "negative_synonym":
        return None
    if canonical == "female":
        return "male"
    if canonical == "male":
        return "female"
    return canonical


def _apply_query_term(plan: dict[str, Any], canonical: str) -> None:
    target_user = plan["target_user"]
    campaign_constraints = plan["campaign_constraints"]

    if canonical in GENDER_TERMS:
        target_user["gender"] = canonical
    elif canonical in LIFECYCLE_TERMS:
        _append_unique(target_user["lifecycle"], canonical)
    elif canonical in BEHAVIOR_TERMS:
        _append_unique(target_user["behaviors"], canonical)
        if canonical == "first_purchase" and campaign_constraints["objective"] is None:
            campaign_constraints["objective"] = "purchase"
    elif canonical in INTEREST_TERMS:
        _append_unique(target_user["interests"], canonical)
        if canonical in CATEGORY_TERMS:
            _append_unique(campaign_constraints["category"], canonical)
    elif canonical in CHANNEL_TERMS:
        _append_unique(target_user["preferred_channels"], canonical)
        _append_unique(campaign_constraints["channels"], canonical)
    elif canonical in OFFER_TERMS:
        campaign_constraints["offer_type"] = canonical
    elif canonical == "price_sensitive":
        target_user["price_sensitivity"] = "high"
    elif canonical == "premium_buyer":
        target_user["price_sensitivity"] = "low"


def _apply_policy_constraints(query: str, plan: dict[str, Any], business_policies: Path | None) -> None:
    for policy in _load_business_policies(business_policies):
        if not _policy_matches_query(query, policy):
            continue
        if policy.get("sql_behavior") == "disambiguation":
            plan["semantic_resolutions"].append(_semantic_resolution(query, policy))
            continue
        plan["policy_constraints"].append(
            {
                "policy_id": policy["policy_id"],
                "canonical": policy["canonical"],
                "ko_label": policy.get("ko_label", policy["canonical"]),
                "scope": policy.get("scope"),
                "metric": policy.get("metric"),
                "table": policy.get("table"),
                "column": policy.get("column"),
                "expression": policy.get("expression"),
                "operator": policy.get("operator"),
                "threshold_krw": policy.get("threshold_krw"),
                "requires_threshold": bool(policy.get("requires_threshold")),
                "sql_behavior": policy.get("sql_behavior", "context"),
                "order_by": policy.get("order_by"),
                "related_columns": policy.get("related_columns", []),
                "source": "business_policies",
            }
        )


def _semantic_resolution(query: str, policy: dict[str, Any]) -> dict[str, Any]:
    requires_clarification = _semantic_resolution_requires_clarification(query, policy)
    return {
        "policy_id": policy["policy_id"],
        "canonical": policy["canonical"],
        "ko_label": policy.get("ko_label", policy["canonical"]),
        "ambiguous_term": policy.get("ambiguous_term"),
        "default_resolution": policy.get("default_resolution"),
        "default_column": policy.get("default_column"),
        "default_select": policy.get("default_select"),
        "requires_clarification": requires_clarification,
        "clarification_question": policy.get("clarification_question"),
        "alternatives": policy.get("alternatives", []),
        "source": "business_policies",
    }


def _semantic_resolution_requires_clarification(query: str, policy: dict[str, Any]) -> bool:
    normalized_query = query.casefold()
    compact_query = re.sub(r"\s+", "", normalized_query)
    for term in policy.get("clarification_terms", []):
        if not isinstance(term, str):
            continue
        normalized_term = term.casefold()
        compact_term = re.sub(r"\s+", "", normalized_term)
        if normalized_term in normalized_query or compact_term in compact_query:
            return True
    return False


def _load_business_policies(business_policies: Path | None) -> list[dict[str, Any]]:
    if business_policies is None or not business_policies.exists():
        return []
    payload = json.loads(business_policies.read_text(encoding="utf-8"))
    policies = payload.get("policies", [])
    return [policy for policy in policies if isinstance(policy, dict) and policy.get("policy_id") and policy.get("canonical")]


def _policy_matches_query(query: str, policy: dict[str, Any]) -> bool:
    normalized_query = query.casefold()
    compact_query = re.sub(r"\s+", "", normalized_query)
    terms = [policy.get("canonical", ""), policy.get("ko_label", ""), *policy.get("synonyms", [])]
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            continue
        normalized_term = term.casefold()
        compact_term = re.sub(r"\s+", "", normalized_term)
        if normalized_term in normalized_query or compact_term in compact_query:
            return True
    return False


def _apply_exclusion(plan: dict[str, Any], canonical: str) -> None:
    if canonical in GENDER_TERMS:
        _append_unique(plan["exclude"]["gender"], canonical)
    elif canonical in INTEREST_TERMS:
        _append_unique(plan["exclude"]["interests"], canonical)
    elif canonical in LIFECYCLE_TERMS:
        _append_unique(plan["exclude"]["lifecycle"], canonical)


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _unique_strings(values: list[str]) -> list[str]:
    unique_values = []
    for value in values:
        if value and value not in unique_values:
            unique_values.append(value)
    return unique_values


def retrieve(
    query: str,
    graph: nx.Graph,
    collection: str,
    url: str,
    api_key: str | None,
    embedding_model_name: str,
    vector_top_k: int,
    keyword_top_k: int,
    graph_top_k: int,
    hops: int,
    normalization_rules: Path | None = DEFAULT_NORMALIZATION_PATH,
    business_policies: Path | None = DEFAULT_POLICY_PATH,
    metric_lexicon: Path = DEFAULT_METRIC_LEXICON_PATH,
    sql_schema: Path = DEFAULT_SCHEMA_PATH,
    sql_limit: int = DEFAULT_LIMIT,
    query_parser: str = "rules",
    llm_model: str = DEFAULT_LLM_MODEL,
    generate_answer: bool = False,
    generate_messages: bool = False,
    message_channel: str = "auto",
    message_policy: Path | None = DEFAULT_MESSAGE_POLICY_PATH,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
    message_generation_options: dict[str, Any] | None = None,
    retrieval_scope: str = "all",
) -> dict[str, Any]:
    timings_ms: dict[str, float] = {}
    retrieve_started_at = time.perf_counter()
    _write_rag_llm_log(
        "rag_retrieve_request",
        {
            "query": query,
            "collection": collection,
            "url": url,
            "embedding_model": embedding_model_name,
            "vector_top_k": vector_top_k,
            "keyword_top_k": keyword_top_k,
            "graph_top_k": graph_top_k,
            "hops": hops,
            "sql_limit": sql_limit,
            "query_parser": query_parser,
            "llm_model": llm_model,
            "generate_answer": generate_answer,
            "generate_messages": generate_messages,
            "message_channel": message_channel,
        },
    )

    # 파싱 전에 사용자 프롬프트를 보수적으로 정리(오타/띄어쓰기)한다. 정리본으로 파싱하되 원문은 보존한다.
    stage_started_at = time.perf_counter()
    prompt_normalization = normalize_prompt(query, parser=query_parser, llm_model=llm_model, prompt_dir=prompt_dir)
    effective_query = prompt_normalization["normalized"]
    timings_ms["prompt_normalization"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    query_plan = build_query_plan(
        effective_query,
        normalization_rules=normalization_rules,
        business_policies=business_policies,
        metric_lexicon=metric_lexicon,
        sql_schema=sql_schema,
        parser=query_parser,
        llm_model=llm_model,
        prompt_dir=prompt_dir,
    )
    timings_ms["query_plan"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    retrieval = query_plan["retrieval"]
    # SQL 은 항상 전체 문장 기준(스코프 무관). 검색·그래프 컨텍스트만 스코프별 절/용어로 좁힌다.
    full_retrieval_query = retrieval["query"]
    keyword_query = " ".join(_unique_strings([full_retrieval_query, *retrieval["terms"]]))
    scope = (retrieval_scope or "all").casefold()
    if scope == "targeting":
        scoped_query = retrieval.get("targeting_query") or full_retrieval_query
        scoped_terms = retrieval.get("targeting_terms", retrieval["terms"])
    elif scope == "channel":
        scoped_query = retrieval.get("channel_query") or full_retrieval_query
        scoped_terms = retrieval.get("channel_terms", retrieval["terms"])
    else:
        scoped_query = full_retrieval_query
        scoped_terms = retrieval["terms"]
    scoped_keyword_query = " ".join(_unique_strings([scoped_query, *scoped_terms]))
    timings_ms["retrieval_query"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    vector_hits = vector_search(
        query=scoped_query,
        collection=collection,
        url=url,
        api_key=api_key,
        embedding_model_name=embedding_model_name,
        limit=vector_top_k,
    )
    timings_ms["vector_search"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    keyword_hits = keyword_search(graph=graph, query=scoped_keyword_query, limit=keyword_top_k)
    timings_ms["keyword_search"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    hits = merge_hits([*vector_hits, *keyword_hits])
    context_nodes = expand_context(graph=graph, hits=hits, hops=hops, limit=graph_top_k)
    context_assembly = assemble_context(context_nodes)
    _write_rag_llm_log(
        "rag_context_assembly",
        {
            "query": query,
            "retrieval_scope": scope,
            "retrieval_query": scoped_query,
            "keyword_query": scoped_keyword_query,
            "full_keyword_query": keyword_query,
            "query_plan": query_plan,
            "vector_hits": [_hit_result(hit) for hit in vector_hits],
            "keyword_hits": [_hit_result(hit) for hit in keyword_hits],
            "merged_hits": [_hit_result(hit) for hit in hits],
            "context_nodes": context_nodes,
            "prompt_context": context_assembly.get("prompt"),
        },
    )
    timings_ms["context_assembly"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    sql_result = build_sql_result(
        graph=graph,
        query=keyword_query,
        query_plan=query_plan,
        context_nodes=context_nodes,
        schema_path=sql_schema,
        default_limit=sql_limit,
    )
    timings_ms["sql_generation"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    stage_log = build_stage_log(
        query_plan=query_plan,
        vector_hits=vector_hits,
        keyword_hits=keyword_hits,
        merged_hits=hits,
        context_nodes=context_nodes,
        context_assembly=context_assembly,
        sql_result=sql_result,
    )
    timings_ms["stage_log"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    answer_prompt = render_answer_prompt(query, query_plan, context_assembly, sql_result, prompt_dir)
    answer_response = build_answer_response(answer_prompt, sql_result, llm_model, generate_answer, prompt_dir)
    timings_ms["answer_generation"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    message_context = build_message_context(
        query_plan=query_plan,
        context_nodes=context_nodes,
        sql_result=sql_result,
        requested_channel=message_channel,
        business_policies=business_policies,
        message_policy=message_policy,
    )
    timings_ms["message_context"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    message_generation_prompt = render_message_prompt(query, query_plan, sql_result, message_context, prompt_dir) if message_context.get("is_success") else None
    timings_ms["message_prompt"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    message_generation = build_message_response(
        message_prompt=message_generation_prompt,
        message_context=message_context,
        llm_model=llm_model,
        generate_messages=generate_messages,
        prompt_dir=prompt_dir,
        message_generation_options=message_generation_options,
    )
    timings_ms["message_generation"] = _elapsed_ms(stage_started_at)
    timings_ms["total_retrieve"] = _elapsed_ms(retrieve_started_at)

    api_response = build_recommendation_api_response(query, query_plan, sql_result, answer_response, message_generation, prompt_normalization)
    return {
        "query": query,
        "prompt_normalization": prompt_normalization,
        "retrieval_scope": scope,
        "prompt_scopes": {
            "mode": query_plan["retrieval"].get("scope_mode"),
            "targeting": query_plan["retrieval"].get("targeting_query"),
            "channel": query_plan["retrieval"].get("channel_query"),
        },
        "query_plan": query_plan,
        "collection": collection,
        "stage_log": stage_log,
        "vector_matches": [_hit_result(hit) for hit in vector_hits],
        "keyword_matches": [_hit_result(hit) for hit in keyword_hits],
        "seed_matches": [_hit_result(hit) for hit in hits],
        "graph_context": context_nodes,
        "context_assembly": context_assembly,
        "sql_result": sql_result,
        "prompt_context": context_assembly["prompt"],
        "answer_prompt": answer_prompt,
        "answer": answer_response,
        "message_generation_prompt": message_generation_prompt,
        "message_generation": message_generation,
        "timings_ms": timings_ms,
        "api_response": api_response,
    }


def vector_search(
    query: str,
    collection: str,
    url: str,
    api_key: str | None,
    embedding_model_name: str,
    limit: int,
) -> list[SearchHit]:
    if limit < 1:
        return []

    embedding_model = TextEmbedding(model_name=embedding_model_name)
    query_vector = list(next(embedding_model.embed([query])))
    client = QdrantClient(url=url, api_key=api_key)

    if hasattr(client, "query_points"):
        response = client.query_points(
            collection_name=collection,
            query=query_vector,
            limit=limit,
            with_payload=True,
        )
        points = getattr(response, "points", response)
    else:
        points = client.search(
            collection_name=collection,
            query_vector=query_vector,
            limit=limit,
            with_payload=True,
        )

    hits = []
    for point in points:
        payload = point.payload or {}
        node_id = payload.get("node_id") or payload.get("source", {}).get("id")
        if not node_id:
            continue
        hits.append(SearchHit(node_id=node_id, score=float(point.score), payload=payload))
    return hits


def keyword_search(graph: nx.Graph, query: str, limit: int) -> list[SearchHit]:
    query_terms = _unique_strings([*_keyword_tokens(query), *_query_tokens(query)])
    if not query_terms or limit < 1:
        return []

    documents: list[tuple[str, dict[str, Any], list[str], str]] = []
    document_frequency: Counter[str] = Counter()
    for node_id, node_data in graph.nodes(data=True):
        haystack = _node_haystack(node_id, node_data)
        document_tokens = _keyword_tokens(haystack)
        if not document_tokens:
            continue
        documents.append((node_id, node_data, document_tokens, haystack))
        document_frequency.update(set(document_tokens))

    if not documents:
        return []

    average_doc_length = sum(len(document_tokens) for _, _, document_tokens, _ in documents) / len(documents)
    hits = []
    for node_id, node_data, document_tokens, haystack in documents:
        token_counts = Counter(document_tokens)
        matched_terms = [term for term in query_terms if token_counts.get(term, 0) > 0]
        if not matched_terms:
            continue
        score = _bm25_score(
            query_terms=matched_terms,
            token_counts=token_counts,
            doc_length=len(document_tokens),
            average_doc_length=average_doc_length,
            document_count=len(documents),
            document_frequency=document_frequency,
        )
        hits.append(
            SearchHit(
                node_id=node_id,
                score=score,
                payload={
                    "node_id": node_id,
                    "node_type": node_data.get("node_type"),
                    "text": node_data.get("text", ""),
                    "matched_terms": matched_terms,
                },
            )
        )

    return sorted(hits, key=lambda hit: hit.score, reverse=True)[:limit]


def _node_haystack(node_id: str, node_data: dict[str, Any]) -> str:
    payload = node_data.get("payload", {})
    return " ".join(
        [
            node_id,
            node_data.get("title", ""),
            node_data.get("text", ""),
            json.dumps(payload, ensure_ascii=False),
        ]
    ).casefold()


def _keyword_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[0-9A-Za-z가-힣_]+", text.casefold()):
        if len(token) < 2:
            continue
        tokens.append(token)
        if "_" in token:
            tokens.extend(part for part in token.split("_") if len(part) >= 2)
    return tokens


def _bm25_score(
    query_terms: list[str],
    token_counts: Counter[str],
    doc_length: int,
    average_doc_length: float,
    document_count: int,
    document_frequency: Counter[str],
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    score = 0.0
    for term in query_terms:
        term_frequency = token_counts.get(term, 0)
        if term_frequency == 0:
            continue
        idf = math.log(1 + (document_count - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
        denominator = term_frequency + k1 * (1 - b + b * doc_length / average_doc_length)
        score += idf * (term_frequency * (k1 + 1)) / denominator
    return score


def merge_hits(hits: list[SearchHit]) -> list[SearchHit]:
    merged: dict[str, SearchHit] = {}
    for hit in hits:
        existing = merged.get(hit.node_id)
        if existing is None or hit.score > existing.score:
            merged[hit.node_id] = hit
    return sorted(merged.values(), key=lambda hit: hit.score, reverse=True)


def expand_context(graph: nx.Graph, hits: list[SearchHit], hops: int, limit: int) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    seed_scores = {hit.node_id: hit.score for hit in hits}

    for hit in hits:
        if hit.node_id not in graph:
            continue
        lengths = nx.single_source_shortest_path_length(graph, hit.node_id, cutoff=hops)
        for node_id, distance in lengths.items():
            graph_score = hit.score / (1 + distance * 0.35)
            if graph_score > scores.get(node_id, 0.0):
                scores[node_id] = graph_score
            reasons.setdefault(node_id, []).append(f"seed={hit.node_id}, distance={distance}")

    ordered_node_ids = sorted(scores, key=lambda node_id: scores[node_id], reverse=True)[:limit]
    context = []
    for node_id in ordered_node_ids:
        node_data = graph.nodes[node_id]
        context.append(
            {
                "id": node_id,
                "type": node_data["node_type"],
                "title": node_data["title"],
                "score": round(scores[node_id], 6),
                "seed_score": round(seed_scores.get(node_id, 0.0), 6) if node_id in seed_scores else None,
                "reasons": reasons[node_id][:3],
                "neighbors": _neighbor_summary(graph, node_id),
                "payload": _compact_payload(node_data["payload"]),
            }
        )
    return context


def render_prompt_context(context_nodes: list[dict[str, Any]]) -> str:
    sections = []
    for index, node in enumerate(context_nodes, start=1):
        payload = node["payload"]
        text = payload.get("text_for_embedding") or payload.get("description") or payload.get("sql") or ""
        sections.append(f"[{index}] {node['type']} {node['title']}\n{text}")
    return "\n\n".join(sections)


def assemble_context(context_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    top_k_chunks = []
    graph_context = []
    node_type_counts: Counter[str] = Counter()

    for index, node in enumerate(context_nodes, start=1):
        payload = node["payload"]
        text = payload.get("text_for_embedding") or payload.get("description") or payload.get("sql") or ""
        node_type_counts[node["type"]] += 1
        top_k_chunks.append(
            {
                "rank": index,
                "id": node["id"],
                "type": node["type"],
                "title": node["title"],
                "score": node["score"],
                "text": text,
            }
        )
        graph_context.append(
            {
                "id": node["id"],
                "type": node["type"],
                "score": node["score"],
                "neighbors": node["neighbors"],
                "reasons": node["reasons"],
            }
        )

    return {
        "top_k_chunks": top_k_chunks,
        "graph_context": graph_context,
        "metadata": {
            "node_count": len(context_nodes),
            "node_types": dict(sorted(node_type_counts.items())),
        },
        "prompt": render_prompt_context(context_nodes),
    }


def render_answer_prompt(
    query: str,
    query_plan: dict[str, Any],
    context_assembly: dict[str, Any],
    sql_result: dict[str, Any],
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> str:
    sql_policy = [
        "SQL은 SQL Result의 검증된 safe_sql 또는 masked_sql만 사용하라.",
    ]
    if not sql_result.get("is_success"):
        sql_policy.extend(
            [
                "SQL Result가 실패 상태이므로 새 SQL을 생성하거나 기존 SQL을 수정하지 마라.",
            ]
        )
        if sql_result.get("failure_reason") == "query_plan_required_conditions_missing":
            sql_policy.append("사용자 입력에 필요한 조건이 부족하므로 SQL 대신 clarification_questions를 질문하라.")
        else:
            sql_policy.append("사용자에게 현재 Query Plan 조건을 완전히 만족하는 검증된 SQL이 없다고 답변하라.")

    fallback = "\n".join(
        [
            "너는 캠페인 추천/NL2SQL 보조 답변 생성기다.",
            "아래 Query Plan과 검색 Context만 근거로 답변하라.",
            "${sql_policy}",
            "근거가 부족하면 부족하다고 말하고 임의로 SQL이나 사실을 만들지 마라.",
            "",
            "[User Query]\n${query}",
            "",
            "[Query Plan]\n${query_plan}",
            "",
            "[Context]\n${context}",
            "",
            "[SQL Result]",
            "${sql_result}",
        ]
    )
    template = _read_prompt_template(prompt_dir, "answer_user.txt", fallback)
    return _render_prompt_template(
        template,
        query=query,
        query_plan=json.dumps(query_plan, ensure_ascii=False, indent=2),
        context=context_assembly["prompt"],
        sql_result=json.dumps(sql_result, ensure_ascii=False, indent=2),
        sql_policy="\n".join(sql_policy),
    )


def build_answer_response(
    answer_prompt: str,
    sql_result: dict[str, Any],
    llm_model: str,
    generate_answer: bool,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> dict[str, Any]:
    if not generate_answer:
        return {
            "is_success": False,
            "mode": "prompt_only",
            "model": None,
            "content": None,
            "failure_reason": None,
        }
    if not os.getenv("OPENAI_API_KEY"):
        return {
            "is_success": False,
            "mode": "openai_chat_completion",
            "model": llm_model,
            "content": None,
            "failure_reason": "missing_openai_api_key",
        }

    try:
        from openai import OpenAI
    except ImportError as exc:
        return {
            "is_success": False,
            "mode": "openai_chat_completion",
            "model": llm_model,
            "content": None,
            "failure_reason": f"openai_import_failed:{exc.__class__.__name__}",
        }

    system_prompt = _read_prompt_template(
        prompt_dir,
        "answer_system.txt",
        "\n".join(
            [
                "너는 캠페인 추천/NL2SQL 최종 답변 생성기다.",
                "SQL은 SQL Result의 sql 값이 있을 때만 사용자에게 제시한다.",
                "SQL Result가 실패 상태이면 새 SQL을 만들거나 후보 SQL을 수정하지 않는다.",
            ]
        ),
    )
    try:
        client = OpenAI()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": answer_prompt},
        ]
        _write_rag_llm_log(
            "llm_answer_request",
            {
                "mode": "openai_chat_completion",
                "model": llm_model,
                "temperature": 0,
                "sql_result": sql_result,
                "messages": messages,
                "message_summary": _message_summary(messages),
            },
        )
        response = client.chat.completions.create(
            model=llm_model,
            temperature=0,
            messages=messages,
        )
        content = response.choices[0].message.content
        _write_rag_llm_log(
            "llm_answer_response",
            {
                "mode": "openai_chat_completion",
                "model": llm_model,
                "content": content,
            },
        )
        return {
            "is_success": True,
            "mode": "openai_chat_completion",
            "model": llm_model,
            "content": content,
            "failure_reason": None,
        }
    except Exception as exc:
        _write_rag_llm_log(
            "llm_answer_failure",
            {
                "mode": "openai_chat_completion",
                "model": llm_model,
                "failure_reason": f"answer_generation_failed:{exc.__class__.__name__}",
            },
        )
        return {
            "is_success": False,
            "mode": "openai_chat_completion",
            "model": llm_model,
            "content": None,
            "failure_reason": f"answer_generation_failed:{exc.__class__.__name__}",
        }


def build_message_context(
    query_plan: dict[str, Any],
    context_nodes: list[dict[str, Any]],
    sql_result: dict[str, Any],
    requested_channel: str = "auto",
    business_policies: Path | None = DEFAULT_POLICY_PATH,
    message_policy: Path | None = DEFAULT_MESSAGE_POLICY_PATH,
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


def render_message_prompt(
    query: str,
    query_plan: dict[str, Any],
    sql_result: dict[str, Any],
    message_context: dict[str, Any],
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> str:
    fallback = "\n".join(
        [
            "[User Query]\n${query}",
            "",
            "[Requested Channel]\n${requested_channel}",
            "",
            "[Channel Policy]\n${channel_policy}",
            "",
            "[Query Plan]\n${query_plan}",
            "",
            "[Campaign Context]\n${campaign_context}",
            "",
            "[Target Context]\n${target_context}",
            "",
            "[Existing Message Examples]\n${message_examples}",
            "",
            "[Tone And Manner Rules]\n${tone_manner_rules}",
            "",
            "[SQL Result]\n${sql_result}",
            "",
            "messages 배열에 benefit_emphasis, urgency_emphasis, emotion_emphasis 3개 JSON object만 반환하라.",
        ]
    )
    template = _read_prompt_template(prompt_dir, "message_generation_user.txt", fallback)
    return _render_prompt_template(
        template,
        query=query,
        requested_channel=message_context.get("channel", DEFAULT_MESSAGE_CHANNEL),
        channel_policy=json.dumps(message_context.get("channel_policy", {}), ensure_ascii=False, indent=2),
        selected_channel_policy=json.dumps(message_context.get("selected_channel_policy", {}), ensure_ascii=False, indent=2),
        query_plan=json.dumps(query_plan, ensure_ascii=False, indent=2),
        campaign_context=json.dumps(message_context.get("campaigns", []), ensure_ascii=False, indent=2),
        target_context=json.dumps(message_context.get("target_context", {}), ensure_ascii=False, indent=2),
        message_examples=json.dumps(message_context.get("message_examples", []), ensure_ascii=False, indent=2),
        tone_manner_rules=_message_generation_tone_manner_rules(prompt_dir),
        sql_result=json.dumps(sql_result, ensure_ascii=False, indent=2),
    )


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
    message_prompt: str | None,
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
    if message_prompt is None:
        return {
            "is_success": False,
            "mode": "openai_chat_completion",
            "model": llm_model,
            "options": effective_options,
            "content": None,
            "messages": [],
            "validation": None,
            "context": message_context,
            "failure_reason": "message_prompt_missing",
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
                "반드시 한국어 JSON object만 출력한다.",
                "없는 혜택이나 근거 없는 사실을 만들지 않는다.",
            ]
        ),
    )
    max_attempts = effective_options["max_attempts"]
    attempts: list[dict[str, Any]] = []
    current_prompt = message_prompt
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
            "base_prompt": message_prompt,
        },
    )

    for attempt_number in range(1, max_attempts + 1):
        attempt_started_at = time.perf_counter()
        parallel_result = _generate_message_variants_parallel(
            base_prompt=current_prompt,
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
            current_prompt = render_message_retry_prompt(
                original_prompt=message_prompt,
                previous_content=last_content,
                failure_reason=last_failure_reason,
                validation=last_validation,
                attempt_number=attempt_number + 1,
                max_attempts=max_attempts,
                prompt_dir=prompt_dir,
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
    base_prompt: str,
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
                base_prompt,
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
    base_prompt: str,
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
        user_prompt = render_message_variant_prompt(base_prompt, variant, message_context, prompt_dir)
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
        response = client.chat.completions.create(
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
    base_prompt: str,
    variant: str,
    message_context: dict[str, Any],
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> str:
    fallback = "\n".join(
        [
            "아래 입력만 사용해 지정된 variant 1개만 생성하라.",
            "반환 JSON은 messages 배열에 정확히 1개 object만 포함해야 한다.",
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
        variant=variant,
        requested_channel=message_context.get("channel", DEFAULT_MESSAGE_CHANNEL),
        selected_channel_policy=json.dumps(message_context.get("selected_channel_policy", {}), ensure_ascii=False, separators=(",", ":")),
        campaign_context=json.dumps(_compact_message_context_items(message_context.get("campaigns", []), 3), ensure_ascii=False, separators=(",", ":")),
        target_context=json.dumps(message_context.get("target_context", {}), ensure_ascii=False, separators=(",", ":")),
        message_examples=json.dumps(_compact_message_context_items(message_context.get("message_examples", []), 6), ensure_ascii=False, separators=(",", ":")),
        tone_manner_rules=_message_generation_tone_manner_rules(prompt_dir),
        repair_context=_message_variant_repair_context(base_prompt),
    )


def _compact_message_context_items(items: Any, limit: int) -> list[Any]:
    if not isinstance(items, list):
        return []
    return items[:limit]


def _message_variant_repair_context(prompt: str) -> str:
    failure_match = re.search(r"\[Failure Reason\]\s*(.*?)(?:\n\[|$)", prompt, re.DOTALL)
    issues_match = re.search(r"\[Validation Issues\]\s*(.*?)(?:\n\[|$)", prompt, re.DOTALL)
    parts = []
    if failure_match:
        parts.append("Failure Reason: " + failure_match.group(1).strip())
    if issues_match:
        parts.append("Validation Issues: " + issues_match.group(1).strip())
    return "\n".join(parts) or "none"


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


def render_message_retry_prompt(
    original_prompt: str,
    previous_content: str | None,
    failure_reason: str | None,
    validation: dict[str, Any] | None,
    attempt_number: int,
    max_attempts: int,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> str:
    fallback = "\n".join(
        [
            "이전 채널 메시지 생성 결과가 검증에 실패했다.",
            "이번 응답은 아래 실패 사유를 모두 수정해서 JSON object만 반환하라.",
            "",
            "[Attempt] ${attempt_number}/${max_attempts}",
            "[Failure Reason] ${failure_reason}",
            "[Validation Issues] ${validation_issues}",
            "[Previous Content] ${previous_content}",
            "",
            "[Original Prompt]",
            "${original_prompt}",
        ]
    )
    template = _read_prompt_template(prompt_dir, "message_generation_retry_user.txt", fallback)
    return _render_prompt_template(
        template,
        original_prompt=original_prompt,
        previous_content=previous_content or "",
        failure_reason=failure_reason or "message_validation_failed",
        validation_issues=json.dumps((validation or {}).get("issues", []), ensure_ascii=False, indent=2),
        attempt_number=str(attempt_number),
        max_attempts=str(max_attempts),
    )


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


def build_recommendation_api_response(
    query: str,
    query_plan: dict[str, Any],
    sql_result: dict[str, Any],
    answer_response: dict[str, Any],
    message_generation: dict[str, Any] | None = None,
    prompt_normalization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unsupported_labels = sql_result.get("unsupported_condition_labels", [])
    if answer_response.get("content"):
        message = answer_response["content"]
    elif sql_result.get("is_success"):
        message = "Query Plan 조건을 만족하는 검증 SQL이 준비되었습니다."
    elif sql_result.get("failure_reason") == "query_plan_required_conditions_missing":
        message = "SQL 생성을 위해 필요한 조건이 부족합니다. 추가 조건을 확인해 주세요."
    elif sql_result.get("failure_reason") == "real_db_unsupported_conditions" and unsupported_labels:
        message = "다음 조건은 아직 실DB 타겟 추출로 지원되지 않습니다: " + ", ".join(unsupported_labels) + "."
    else:
        message = "현재 Query Plan 조건을 완전히 만족하는 검증된 SQL이 없습니다."

    normalization = prompt_normalization or {"original": query, "normalized": query, "summary": "", "corrections": [], "mode": "noop"}
    response = {
        "status": _api_status(sql_result),
        "query": query,
        "normalized_query": normalization.get("normalized", query),
        "prompt_summary": normalization.get("summary", ""),
        "prompt_corrections": normalization.get("corrections", []),
        "prompt_normalization_mode": normalization.get("mode"),
        "intent": query_plan.get("intent"),
        "sql": sql_result.get("sql"),
        "target_connection": sql_result.get("target_connection"),
        "target_dialect": sql_result.get("target_dialect"),
        "message": message,
        "missing_input_conditions": sql_result.get("missing_input_conditions", []),
        "clarification_questions": sql_result.get("clarification_questions", []),
        "unsupported_conditions": sql_result.get("unsupported_conditions", []),
        "unsupported_condition_labels": unsupported_labels,
        "answer_mode": answer_response.get("mode"),
        "answer_failure_reason": answer_response.get("failure_reason"),
        "failure_reason": sql_result.get("failure_reason"),
    }
    if message_generation is not None:
        response.update(
            {
                "message_variants": message_generation.get("messages", []),
                "message_generation_mode": message_generation.get("mode"),
                "message_generation_failure_reason": message_generation.get("failure_reason"),
                "message_generation_validation": message_generation.get("validation"),
            }
        )
    return response


def _api_status(sql_result: dict[str, Any]) -> str:
    if sql_result.get("is_success"):
        return "success"
    if sql_result.get("failure_reason") == "query_plan_required_conditions_missing":
        return "needs_clarification"
    return "no_verified_sql"


def build_sql_result(
    graph: nx.Graph,
    query: str,
    query_plan: dict[str, Any],
    context_nodes: list[dict[str, Any]],
    schema_path: Path,
    default_limit: int,
    candidate_limit: int = 20,
) -> dict[str, Any]:
    condition_tokens = build_verified_condition_tokens(query_plan)
    input_validation = validate_required_input_conditions(query_plan, condition_tokens)
    required_conditions = required_sql_conditions(query_plan)
    if not input_validation["is_satisfied"]:
        return {
            "sql": None,
            "selected": None,
            "candidates": [],
            "candidate_count": 0,
            "condition_tokens": condition_tokens,
            "required_conditions": required_conditions,
            "input_validation": input_validation,
            "missing_input_conditions": input_validation["missing_conditions"],
            "clarification_questions": input_validation["clarification_questions"],
            "is_success": False,
            "failure_reason": "query_plan_required_conditions_missing",
        }

    allowed_tables = load_allowed_tables(schema_path)
    table_dialects = load_table_dialects(schema_path)
    table_databases = load_table_databases(schema_path)
    template_candidate = build_sql_template_candidate(query_plan, condition_tokens)
    candidates = [template_candidate] if template_candidate is not None else []

    validated_candidates = []
    for candidate in candidates:
        # 타겟 오디언스는 전체가 나와야 하므로 행수 제한(LIMIT/TOP)을 붙이지 않는다.
        validation = validate_sql(
            candidate["sql"],
            allowed_tables=allowed_tables,
            default_limit=None,
            table_dialects=table_dialects,
        )
        coverage = validate_sql_condition_coverage(candidate["sql"], required_conditions)
        intent_scope = validate_sql_intent_scope(candidate, query_plan)
        unmentioned_conditions = validate_unmentioned_sql_conditions(candidate["sql"], query_plan)
        validated_candidates.append(
            {
                **candidate,
                "validation": validation,
                "coverage": coverage,
                "intent_scope": intent_scope,
                "unmentioned_conditions": unmentioned_conditions,
                "is_eligible": validation["is_valid"] and coverage["is_satisfied"] and intent_scope["is_satisfied"],
            }
        )
        validated_candidates[-1]["is_eligible"] = validated_candidates[-1]["is_eligible"] and unmentioned_conditions["is_satisfied"]

    selected = next((candidate for candidate in validated_candidates if candidate["is_eligible"]), None)
    if selected is None and validated_candidates:
        selected = validated_candidates[0]

    selected_sql = None
    target_connection = None
    target_dialect = None
    if selected is not None and selected["is_eligible"]:
        validation = selected["validation"]
        selected_sql = validation["masked_sql"] if validation["sensitive_columns"] else validation["safe_sql"]
        # 이 SQL 을 실제 어느 DB에서 실행해야 하는지(외부 실DB면 커넥션명, 로컬이면 None) 판별.
        target_connection = infer_target_connection(selected.get("tables", []), table_databases)
        target_dialect = validation.get("dialect")

    failure_reason = None
    if selected is None:
        failure_reason = "no_sql_candidates"
    elif not selected["is_eligible"]:
        if not selected["validation"]["is_valid"]:
            failure_reason = "sql_guard_failed"
        elif not selected["coverage"]["is_satisfied"]:
            failure_reason = "query_plan_conditions_missing"
        elif not selected["intent_scope"]["is_satisfied"]:
            failure_reason = "intent_scope_mismatch"
        elif not selected["unmentioned_conditions"]["is_satisfied"]:
            failure_reason = "query_plan_unmentioned_conditions_added"

    # 실회원(CRM_MB_BASEINFO) 경로가 미지원 조건 때문에 데모 스키마로 fallback→guard 탈락한 경우,
    # 제네릭 sql_guard_failed 대신 "어떤 조건이 실DB 추출 미지원인지"를 구체적으로 알린다.
    unsupported_conditions: list[str] = []
    unsupported_condition_labels: list[str] = []
    if not (selected_sql is not None) and query_plan.get("intent") == "recommend_campaign":
        unsupported_conditions = compile_member_target_conditions(query_plan)["unsupported"]
        unsupported_condition_labels = [_unsupported_condition_label(path) for path in unsupported_conditions]
        if failure_reason == "sql_guard_failed" and unsupported_conditions:
            failure_reason = "real_db_unsupported_conditions"

    return {
        "sql": selected_sql,
        "target_connection": target_connection,
        "target_dialect": target_dialect,
        "selected": selected,
        "candidates": validated_candidates,
        "candidate_count": len(validated_candidates),
        "condition_tokens": condition_tokens,
        "required_conditions": required_conditions,
        "input_validation": input_validation,
        "missing_input_conditions": [],
        "clarification_questions": [],
        "unsupported_conditions": unsupported_conditions,
        "unsupported_condition_labels": unsupported_condition_labels,
        "is_success": selected_sql is not None,
        "failure_reason": failure_reason,
    }


def build_verified_condition_tokens(query_plan: dict[str, Any]) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    target_user = query_plan.get("target_user", {})
    campaign_constraints = query_plan.get("campaign_constraints", {})
    exclude = query_plan.get("exclude", {})
    intent = query_plan.get("intent")

    gender = target_user.get("gender")
    if gender in GENDER_TERMS:
        _add_token(tokens, "target_user.gender", "gender", "=", gender, ["u.gender = " + _sql_quote(gender)], [])

    for gender_value in exclude.get("gender", []):
        if gender_value in GENDER_TERMS:
            _add_token(tokens, "exclude.gender", "gender", "!=", gender_value, ["u.gender <> " + _sql_quote(gender_value)], [])

    age_min = target_user.get("age_min")
    if isinstance(age_min, int):
        _add_token(tokens, "target_user.age_min", "age", ">=", age_min, [f"u.age >= {age_min}"], [])

    age_max = target_user.get("age_max")
    if isinstance(age_max, int):
        _add_token(tokens, "target_user.age_max", "age", "<=", age_max, [f"u.age <= {age_max}"], [])

    inactivity_period = target_user.get("inactivity_period")
    if isinstance(inactivity_period, dict) and isinstance(inactivity_period.get("sql_interval"), str):
        _add_inactivity_period_token(tokens, inactivity_period)

    for lifecycle in target_user.get("lifecycle", []):
        if lifecycle in LIFECYCLE_TERMS and not _has_explicit_long_inactivity_period(inactivity_period):
            _add_token(tokens, "target_user.lifecycle", "lifecycle", "=", lifecycle, ["u.lifecycle = " + _sql_quote(lifecycle)], [])
            if intent == "recommend_campaign":
                _add_token(tokens, "campaign_constraints.target_segment", "target_segment", "=", lifecycle, ["ts.target_segment = " + _sql_quote(lifecycle)], ["target_segments"])

    for lifecycle in exclude.get("lifecycle", []):
        if lifecycle in LIFECYCLE_TERMS:
            _add_token(tokens, "exclude.lifecycle", "lifecycle", "!=", lifecycle, ["u.lifecycle <> " + _sql_quote(lifecycle)], [])

    for interest in target_user.get("interests", []):
        if interest in INTEREST_TERMS:
            _add_token(tokens, "target_user.interests", "interest", "=", interest, ["ui.interest = " + _sql_quote(interest)], ["user_interests"])

    for interest in exclude.get("interests", []):
        if interest in INTEREST_TERMS:
            clause = (
                "NOT EXISTS (SELECT 1 FROM user_interests ui_ex "
                "WHERE ui_ex.user_id = u.user_id AND ui_ex.interest = " + _sql_quote(interest) + ")"
            )
            _add_token(tokens, "exclude.interests", "interest", "not_exists", interest, [clause], [])

    for channel in target_user.get("preferred_channels", []):
        if channel in CHANNEL_TERMS:
            _add_token(tokens, "target_user.preferred_channels", "preferred_channel", "=", channel, ["upc.preferred_channel = " + _sql_quote(channel)], ["user_preferred_channels"])

    for behavior in target_user.get("behaviors", []):
        if behavior in BEHAVIOR_TERMS:
            behavior_clause = "urb.behavior LIKE 'cart_abandoned:%'" if behavior == "cart_abandoner" else "urb.behavior = " + _sql_quote(behavior)
            _add_token(tokens, "target_user.behaviors", "behavior", "=", behavior, [behavior_clause], ["user_recent_behaviors"])
            if intent == "recommend_campaign":
                _add_token(tokens, "campaign_constraints.target_segment", "target_segment", "=", behavior, ["ts.target_segment = " + _sql_quote(behavior)], ["target_segments"])

    purchase_object = target_user.get("purchase_object")
    if isinstance(purchase_object, str) and purchase_object:
        clauses = ["urb.behavior LIKE 'purchased:%'", "LOWER(urb.behavior) LIKE " + _sql_quote("%" + purchase_object.casefold() + "%")]
        _add_token(tokens, "target_user.purchase_object", "purchase_object", "like", purchase_object, clauses, ["user_recent_behaviors"])

    price_sensitivity = target_user.get("price_sensitivity")
    if price_sensitivity in {"high", "low"}:
        _add_token(tokens, "target_user.price_sensitivity", "price_sensitivity", "=", price_sensitivity, ["u.price_sensitivity = " + _sql_quote(price_sensitivity)], [])
        if intent == "recommend_campaign":
            segment = "price_sensitive" if price_sensitivity == "high" else "premium_buyer"
            _add_token(tokens, "campaign_constraints.target_segment", "target_segment", "=", segment, ["ts.target_segment = " + _sql_quote(segment)], ["target_segments"])

    for category in campaign_constraints.get("category", []):
        if category in CATEGORY_TERMS:
            _add_token(tokens, "campaign_constraints.category", "campaign_category", "=", category, ["c.category = " + _sql_quote(category)], [])

    objective = campaign_constraints.get("objective")
    if intent == "recommend_campaign" and objective in CAMPAIGN_OBJECTIVES:
        _add_token(tokens, "campaign_constraints.objective", "campaign_objective", "=", objective, ["c.objective = " + _sql_quote(objective)], [])

    offer_type = campaign_constraints.get("offer_type")
    if offer_type in OFFER_TERMS:
        if offer_type == "coupon":
            clauses = ["ck.keyword = '쿠폰'"]
        elif offer_type == "free_shipping":
            clauses = ["(ck.keyword = '무료배송' OR c.offer LIKE '%무료배송%')"]
        else:
            clauses = ["(ck.keyword = " + _sql_quote(offer_type) + " OR c.offer LIKE " + _sql_quote("%" + offer_type + "%") + ")"]
        _add_token(tokens, "campaign_constraints.offer_type", "offer_type", "=", offer_type, clauses, ["campaign_keywords"])

    # objective/target_segment 와 동일하게 recommend_campaign 에서만 캠페인 채널 절을 낸다.
    # find_user_segment 프롬프트에 "발송 채널: RCS" 같은 표기가 섞여 들어오면 campaign_channels
    # JOIN 이 생겨 intent_scope 검증에 걸리고 sql=None("검증 SQL 없음")으로 빠지기 때문이다.
    if intent == "recommend_campaign":
        for channel in campaign_constraints.get("channels", []):
            if channel in CHANNEL_TERMS:
                _add_token(tokens, "campaign_constraints.channels", "campaign_channel", "=", channel, ["cc.channel = " + _sql_quote(channel)], ["campaign_channels"])

    # 디멘션 값 필터(예: 상품브랜드 포멜카멜리 -> C.BRAND_ID IN ('A')). 실제 CRMDW 테이블 대상
    # 전용 cart 템플릿(build_sql_template_candidate)이 이 절을 그대로 생성하므로 별칭 C 로 맞춘다.
    # cart 디멘션 타겟팅 모드에서만 토큰을 낸다(다른 템플릿에 잘못 섞이지 않도록).
    brand_filter = _cart_dimension_brand_filter(query_plan)
    if brand_filter is not None:
        column_short = brand_filter.get("column", "").split(".")[-1]
        codes = [code for code in brand_filter.get("codes", []) if isinstance(code, str) and code]
        if column_short and codes:
            in_list = ", ".join(_sql_quote(code) for code in codes)
            clause = f"C.{column_short} {brand_filter.get('operator', 'IN')} ({in_list})"
            _add_token(
                tokens,
                "dimension_filters." + str(brand_filter.get("dimension_id", "dimension")),
                "dimension_filter",
                "in",
                ",".join(codes),
                [clause],
                [],
            )

    for policy in query_plan.get("policy_constraints", []):
        _add_policy_token(tokens, policy)

    for metric in query_plan.get("computed_metrics", []):
        _add_computed_metric_token(tokens, metric, intent)

    for expression in query_plan.get("set_expressions", []):
        _add_set_expression_token(tokens, expression)

    for resolution in query_plan.get("semantic_resolutions", []):
        _add_semantic_resolution_token(tokens, resolution)

    return tokens


def _add_set_expression_token(tokens: list[dict[str, Any]], expression: dict[str, Any]) -> None:
    issue = _set_expression_issue(expression)
    if issue:
        return
    compiled = _compile_set_expression_ast(expression["set_ast"])
    _add_token(
        tokens,
        "set_expressions",
        "set_expression_segment",
        "segment_predicate",
        expression.get("expression_id", "segment_set_expression"),
        [compiled["expression_sql"]],
        [],
    )


def _add_inactivity_period_token(tokens: list[dict[str, Any]], period: dict[str, Any]) -> None:
    interval = period["sql_interval"]
    clauses = [
        "u.last_login_at <= CURRENT_TIMESTAMP - INTERVAL " + _sql_quote(interval),
    ]
    if period.get("min_days", 0) >= 180:
        clauses.extend(
            [
                "u.purchase_count_90d = 0",
                "u.lifecycle IN ('inactive_90d', 'inactive_180d', 'dormant')",
            ]
        )
    _add_token(
        tokens,
        "target_user.inactivity_period",
        "inactivity_period",
        ">=",
        interval,
        clauses,
        [],
        order_by=["inactive_days DESC", "u.user_id ASC"],
        select_columns=["u.last_login_at", "CURRENT_DATE - u.last_login_at::date AS inactive_days", "u.lifecycle"],
    )


def _has_explicit_long_inactivity_period(period: Any) -> bool:
    return isinstance(period, dict) and isinstance(period.get("min_days"), int) and period["min_days"] >= 180


def _set_expression_issue(expression: dict[str, Any]) -> str | None:
    if expression.get("requires_clarification"):
        return expression.get("clarification_question") or "집합식의 의미를 명확히 지정해 주세요."
    if not isinstance(expression.get("set_ast"), dict):
        return "집합식 AST가 없습니다."
    compiled = _compile_set_expression_ast(expression["set_ast"])
    if not compiled["is_valid"]:
        return "; ".join(compiled["issues"])
    return None


def _compile_set_expression_ast(ast: dict[str, Any]) -> dict[str, Any]:
    if ast.get("type") == "set_op":
        left = _compile_set_expression_ast(ast.get("left", {}))
        right = _compile_set_expression_ast(ast.get("right", {}))
        issues = [*left["issues"], *right["issues"]]
        if not left["is_valid"] or not right["is_valid"]:
            return {"is_valid": False, "expression_sql": "", "issues": issues}
        op = ast.get("op")
        if op == "+":
            return {"is_valid": True, "expression_sql": f"({left['expression_sql']} OR {right['expression_sql']})", "issues": []}
        if op == "*":
            return {"is_valid": True, "expression_sql": f"({left['expression_sql']} AND {right['expression_sql']})", "issues": []}
        if op == "-":
            return {"is_valid": True, "expression_sql": f"({left['expression_sql']} AND NOT ({right['expression_sql']}))", "issues": []}
        return {"is_valid": False, "expression_sql": "", "issues": [f"지원하지 않는 집합 연산자입니다: {op}"]}
    if ast.get("type") == "age_range":
        age_min = ast.get("age_min")
        age_max = ast.get("age_max")
        if isinstance(age_min, int) and isinstance(age_max, int):
            return {"is_valid": True, "expression_sql": f"(u.age >= {age_min} AND u.age <= {age_max})", "issues": []}
        return {"is_valid": False, "expression_sql": "", "issues": ["연령대 피연산자의 범위가 올바르지 않습니다."]}
    if ast.get("type") == "operand":
        return _compile_set_operand(ast)
    if ast.get("type") == "unknown_operand":
        return {"is_valid": False, "expression_sql": "", "issues": ["정규화되지 않은 집합 피연산자입니다: " + str(ast.get("text", ""))]}
    return {"is_valid": False, "expression_sql": "", "issues": ["지원하지 않는 집합식 AST 노드입니다."]}


def _compile_set_operand(operand: dict[str, Any]) -> dict[str, Any]:
    canonical = operand.get("canonical")
    if canonical in GENDER_TERMS:
        return {"is_valid": True, "expression_sql": "u.gender = " + _sql_quote(canonical), "issues": []}
    if canonical in LIFECYCLE_TERMS:
        return {"is_valid": True, "expression_sql": "u.lifecycle = " + _sql_quote(canonical), "issues": []}
    if canonical == "price_sensitive":
        return {"is_valid": True, "expression_sql": "u.price_sensitivity = 'high'", "issues": []}
    if canonical == "premium_buyer":
        return {"is_valid": True, "expression_sql": "u.predicted_ltv_segment = 'high'", "issues": []}
    if canonical in INTEREST_TERMS:
        clause = (
            "EXISTS (SELECT 1 FROM user_interests ui_set "
            "WHERE ui_set.user_id = u.user_id AND ui_set.interest = " + _sql_quote(canonical) + ")"
        )
        return {"is_valid": True, "expression_sql": clause, "issues": []}
    if canonical in BEHAVIOR_TERMS:
        behavior_clause = "urb_set.behavior LIKE 'cart_abandoned:%'" if canonical == "cart_abandoner" else "urb_set.behavior = " + _sql_quote(canonical)
        clause = (
            "EXISTS (SELECT 1 FROM user_recent_behaviors urb_set "
            "WHERE urb_set.user_id = u.user_id AND " + behavior_clause + ")"
        )
        return {"is_valid": True, "expression_sql": clause, "issues": []}
    if canonical in CHANNEL_TERMS:
        clause = (
            "EXISTS (SELECT 1 FROM user_preferred_channels upc_set "
            "WHERE upc_set.user_id = u.user_id AND upc_set.preferred_channel = " + _sql_quote(canonical) + ")"
        )
        return {"is_valid": True, "expression_sql": clause, "issues": []}
    if canonical == "coupon":
        return {"is_valid": True, "expression_sql": "u.price_sensitivity = 'high'", "issues": []}
    return {"is_valid": False, "expression_sql": "", "issues": ["사용자 집합 조건으로 컴파일할 수 없는 피연산자입니다: " + str(canonical)]}


def _set_expression_canonical_values(expressions: list[dict[str, Any]]) -> set[str]:
    values: set[str] = set()
    for expression in expressions:
        values.update(_set_ast_canonical_values(expression.get("set_ast")))
    return values


def _set_ast_canonical_values(ast: Any) -> set[str]:
    if not isinstance(ast, dict):
        return set()
    values: set[str] = set()
    canonical = ast.get("canonical")
    if isinstance(canonical, str):
        values.add(canonical)
    values.update(_set_ast_canonical_values(ast.get("left")))
    values.update(_set_ast_canonical_values(ast.get("right")))
    return values


def _set_expression_retrieval_terms(expression: dict[str, Any]) -> list[str]:
    terms = [expression.get("expression_id"), expression.get("ko_label"), expression.get("expression_text")]
    terms.extend(sorted(_set_ast_canonical_values(expression.get("set_ast"))))
    return [term for term in terms if isinstance(term, str) and term]


def _add_semantic_resolution_token(tokens: list[dict[str, Any]], resolution: dict[str, Any]) -> None:
    if resolution.get("requires_clarification"):
        return
    select_column = resolution.get("default_select")
    if isinstance(select_column, str) and _is_safe_select_expression(select_column):
        _add_token(
            tokens,
            "semantic_resolutions",
            "semantic_resolution_select",
            "select",
            resolution.get("canonical", "semantic_resolution"),
            [],
            [],
            select_columns=[select_column],
        )


def _is_safe_select_expression(expression: str) -> bool:
    return bool(re.fullmatch(r"[uc]\.[a-z_][a-z0-9_]*", expression.strip()))


def _add_computed_metric_token(tokens: list[dict[str, Any]], metric: dict[str, Any], intent: str | None) -> None:
    if metric.get("requires_clarification") or not isinstance(metric.get("formula_ast"), dict):
        return
    compiled = compile_formula_ast(metric["formula_ast"], schema_path=DEFAULT_SCHEMA_PATH)
    if not compiled["is_valid"] or _computed_metric_intent_issue(metric, intent):
        return

    expression = compiled["expression_sql"]
    alias = _safe_metric_alias(metric.get("metric_id")) or "computed_formula_score"
    select_columns = [f"({expression}) AS {alias}"]
    behavior = metric.get("sql_behavior") or "select"
    if behavior == "rank":
        direction = "ASC" if str(metric.get("order_by", "desc")).casefold() == "asc" else "DESC"
        _add_token(
            tokens,
            "computed_metrics",
            "computed_metric_rank",
            "order_by",
            alias,
            [],
            [],
            order_by=[f"{expression} {direction}"],
            select_columns=select_columns,
        )
        return
    if behavior == "filter" and isinstance(metric.get("threshold"), int | float):
        operator = metric.get("operator") if metric.get("operator") in {"=", ">", ">=", "<", "<="} else ">="
        _add_token(
            tokens,
            "computed_metrics",
            "computed_metric_filter",
            operator,
            alias,
            [f"{expression} {operator} {metric['threshold']}"],
            [],
            select_columns=select_columns,
        )
        return
    _add_token(
        tokens,
        "computed_metrics",
        "computed_metric_select",
        "select",
        alias,
        [],
        [],
        select_columns=select_columns,
    )


def _safe_metric_alias(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    alias = re.sub(r"[^a-zA-Z0-9_]", "_", value.strip()).lower()
    if not re.match(r"^[a-z_][a-z0-9_]*$", alias):
        return None
    return alias[:48]


def _computed_metric_intent_issue(metric: dict[str, Any], intent: str | None) -> str | None:
    if not isinstance(metric.get("formula_ast"), dict):
        return "계산식 AST가 없습니다."
    compiled = compile_formula_ast(metric["formula_ast"], schema_path=DEFAULT_SCHEMA_PATH)
    if not compiled["is_valid"]:
        return "계산식에 사용할 수 없는 컬럼이나 연산자가 포함되어 있습니다: " + "; ".join(compiled["issues"])
    referenced_tables = set(compiled["referenced_tables"])
    if intent == "find_user_segment" and referenced_tables - {"users"}:
        return "사용자 세그먼트 조회 계산식에는 users 테이블의 숫자형 컬럼만 사용할 수 있습니다."
    return None


def _add_policy_token(tokens: list[dict[str, Any]], policy: dict[str, Any]) -> None:
    behavior = policy.get("sql_behavior")
    expression = _policy_sql_expression(policy)
    if not expression:
        return

    metric = policy.get("metric") or policy.get("canonical")
    select_columns = [f"({expression}) AS {metric}"] if isinstance(metric, str) and re.match(r"^[a-z_][a-z0-9_]*$", metric) else []
    if behavior == "rank":
        direction = "DESC" if str(policy.get("order_by", "desc")).casefold() != "asc" else "ASC"
        _add_token(
            tokens,
            "policy_constraints",
            "business_policy_rank",
            "order_by",
            policy.get("canonical", "business_policy"),
            [],
            [],
            order_by=[f"{expression} {direction}"],
            select_columns=select_columns,
        )
        return

    if behavior != "filter" or policy.get("threshold_krw") is None:
        return

    operator = policy.get("operator") or ">="
    if operator not in {"=", ">", ">=", "<", "<="}:
        return
    threshold = int(policy["threshold_krw"])
    _add_token(
        tokens,
        "policy_constraints",
        "business_policy_filter",
        operator,
        policy.get("canonical", "business_policy"),
        [f"{expression} {operator} {threshold}"],
        [],
        select_columns=select_columns,
    )


def _policy_sql_expression(policy: dict[str, Any]) -> str | None:
    expression = policy.get("expression")
    if isinstance(expression, str) and _is_safe_policy_expression(expression):
        return expression
    table_alias = "u" if policy.get("table") == "users" else "c" if policy.get("table") == "campaigns" else None
    column = policy.get("column")
    if table_alias and isinstance(column, str) and re.match(r"^[a-z_][a-z0-9_]*$", column):
        return f"{table_alias}.{column}"
    return None


def _is_safe_policy_expression(expression: str) -> bool:
    return bool(re.fullmatch(r"[uc]\.[a-z_][a-z0-9_]*(?:\s*[*+\-/]\s*[uc]\.[a-z_][a-z0-9_]*)*", expression.strip()))


def _add_token(
    tokens: list[dict[str, Any]],
    path: str,
    token_type: str,
    operator: str,
    value: str | int,
    clauses: list[str],
    joins: list[str],
    order_by: list[str] | None = None,
    select_columns: list[str] | None = None,
    ctes: list[str] | None = None,
    base_joins: list[str] | None = None,
) -> None:
    token = {
        "path": path,
        "type": token_type,
        "operator": operator,
        "value": value,
        "sql_clauses": clauses,
        "joins": joins,
        "order_by": order_by or [],
        "select_columns": select_columns or [],
        "ctes": ctes or [],
        "base_joins": base_joins or [],
    }
    if token not in tokens:
        tokens.append(token)


def build_sql_template_candidate(query_plan: dict[str, Any], condition_tokens: list[dict[str, Any]]) -> dict[str, Any] | None:
    intent = query_plan.get("intent")
    if intent == "recommend_campaign":
        brand_filter = _cart_dimension_brand_filter(query_plan)
        if brand_filter is not None:
            # 장바구니에 특정 상품브랜드(BRAND_ID) 상품을 담은 회원 추출(실제 CRMDW 테이블).
            # 브랜드명은 dimension_catalog 스냅샷으로 이미 코드(예: 'A')로 해석돼 넘어온다.
            column_short = brand_filter.get("column", "CRM_CM_PRODUCT.BRAND_ID").split(".")[-1]
            operator = brand_filter.get("operator", "IN")
            in_list = ", ".join(_sql_quote(code) for code in brand_filter["codes"])
            sql = "\n".join(
                [
                    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID",
                    "FROM ODS_MALL_OMS_CART A",
                    "     INNER JOIN CRM_MB_BASEINFO B ON A.CART_ID = B.MEMBER_ID",
                    "     INNER JOIN CRM_CM_PRODUCT C ON A.PRODUCT_ID = C.PRODUCT_ID",
                    "WHERE A.KEEP_YN = 'Y'",
                    f"  AND C.{column_short} {operator} ({in_list})",
                ]
            )
            return _sql_candidate("sql_template:cart_dimension_targets", "장바구니 상품브랜드 타겟팅 SQL 템플릿", 1.0, sql, _template_tables(sql), "sql_template")
        if _should_use_cart_repurchase_template(query_plan):
            # 타겟은 "장바구니에 담고 아직 결제 안 함"(카트 이탈)뿐 — KEEP_YN='Y'가 미결제 보관 상태를 표현한다.
            # 재구매(objective)는 메시지 목적 라벨일 뿐 타겟 필터가 아니므로, 회원 단위 주문 anti-join은 걸지 않는다.
            #   (NOT EXISTS(모든 주문)은 "평생 무주문 회원"을 뜻해 재구매 대상과 자기모순이라 제거했다.)
            # 라벨 컬럼(target_segment/objective)은 세그먼트·목적 태그이자 조건 커버리지 충족용(값은 query_plan 기준).
            objective = query_plan.get("campaign_constraints", {}).get("objective")
            select_columns = ["B.MEMBER_NO AS CUST_ID", "'cart_abandoner' AS target_segment"]
            if objective:
                select_columns.append(_sql_quote(objective) + " AS objective")
            sql = "\n".join(
                [
                    "SELECT DISTINCT " + ", ".join(select_columns),
                    "FROM ODS_MALL_OMS_CART A",
                    "     INNER JOIN CRM_MB_BASEINFO B ON A.CART_ID = B.MEMBER_ID",
                    "WHERE A.KEEP_YN = 'Y'",
                ]
            )
            return _sql_candidate("sql_template:cart_repurchase_targets", "장바구니 미결제 재구매 유도 SQL 템플릿(CRMDW)", 1.0, sql, _template_tables(sql), "sql_template")
        member_candidate = build_member_targets_sql_candidate(query_plan)
        if member_candidate is not None:
            # 성별/연령/등급/휴면/장기미접속/앱 등 회원 속성 타겟은 실회원 테이블(CRMDW CRM_MB_BASEINFO)로 뽑는다.
            # 데모 스키마(users/recommendation_edges/campaigns) fallback 은 실DB 미이관 테이블이라
            # sql_guard 의 table_not_allowed 로 폐기되므로, 매핑 가능한 조건만이면 이 경로를 쓴다.
            return member_candidate
        sql = assemble_sql_from_template(
            select_columns=[
                "u.user_id",
                "u.age",
                "u.gender",
                "u.price_sensitivity",
                "c.campaign_id",
                "c.name",
                "c.objective",
                "c.category",
                "c.offer",
                "c.start_date",
                "c.end_date",
            ],
            base_from="FROM users u\nJOIN recommendation_edges re ON re.user_id = u.user_id\nJOIN campaigns c ON c.campaign_id = re.campaign_id",
            condition_tokens=condition_tokens,
        )
        return _sql_candidate("sql_template:recommend_campaign", "의도별 캠페인 추천 SQL 템플릿", 1.0, sql, _template_tables(sql), "sql_template")
    if intent == "find_user_segment":
        sql = assemble_sql_from_template(
            select_columns=["u.user_id", "u.age", "u.gender", "u.region", "u.lifecycle", "u.price_sensitivity"],
            base_from="FROM users u",
            condition_tokens=condition_tokens,
        )
        return _sql_candidate("sql_template:find_user_segment", "의도별 사용자 세그먼트 SQL 템플릿", 1.0, sql, _template_tables(sql), "sql_template")
    return None


_UNSUPPORTED_CONDITION_LABELS = {
    "target_user.gender": "성별 조건",
    "target_user.interests": "관심사 조건",
    "target_user.preferred_channels": "선호 채널 조건",
    "target_user.behaviors": "행동 조건",
    "target_user.purchase_object": "구매 상품 조건",
    "target_user.price_sensitivity": "가격 민감도 조건",
    "target_user.inactivity_period": "미접속 기간 조건",
    "target_user.lifecycle": "생애주기 조건",
    "exclude.gender": "성별 제외 조건",
    "exclude.interests": "관심사 제외 조건",
    "exclude.lifecycle": "생애주기 제외 조건",
    "set_expressions": "세그먼트 집합식",
    "computed_metrics": "계산 지표 조건",
    "policy_constraints": "업무 정책 조건",
    "semantic_resolutions": "의미 해석 조건",
    "campaign_constraints.category": "캠페인 카테고리 조건",
    "campaign_constraints.offer_type": "혜택 유형 조건",
    "campaign_constraints.channels": "발송 채널 조건",
}


def _unsupported_condition_label(path: str) -> str:
    """미지원 조건 path 를 사람이 읽을 라벨로 바꾼다(예: 'exclude.lifecycle:new_user' -> '생애주기 제외 조건: new_user')."""
    base, _, value = path.partition(":")
    label = _UNSUPPORTED_CONDITION_LABELS.get(base, base)
    return f"{label}: {value}" if value else label


def compile_member_target_conditions(query_plan: dict[str, Any]) -> dict[str, Any]:
    """query_plan 의 타겟 조건을 실회원 테이블(CRM_MB_BASEINFO) 술어로 컴파일한다.

    조건 -> 실컬럼 매핑을 한곳(MEMBER_EQ_FILTERS/MEMBER_ACTIVITY_FILTERS)에서 조회하므로 지원 속성의
    어떤 조합(포함/제외/연령 …)도 자동으로 술어 목록이 된다. CRM_MB_BASEINFO 단독으로 표현할 수 없는
    조건은 그 경로(path)를 unsupported 에 모은다. 호출부는 unsupported 가 비어있을 때만 실DB SQL 을 쓴다.

    반환 dict: predicates(WHERE 술어), labels(세그먼트 라벨 canonical 값), forces_state(회원상태 직접
    지정 여부), has_signal(회원 대상 신호 존재), unsupported(미지원 조건 path 목록).
    """
    target_user = query_plan.get("target_user", {})
    exclude = query_plan.get("exclude", {})
    campaign = query_plan.get("campaign_constraints", {})
    eq_includes: dict[str, list[str]] = {}  # 실컬럼 -> 포함 저장값들(같은 컬럼은 IN 으로 OR)
    include_categories: set[str] = set()
    other_predicates: list[str] = []  # 제외(<>)/연령/활동 등은 그대로 AND
    labels: list[str] = []
    unsupported: list[str] = []
    has_signal = False

    def _add_include(canonical: str) -> None:
        category, column, value = MEMBER_EQ_FILTERS[canonical]
        eq_includes.setdefault(column, [])
        if value not in eq_includes[column]:
            eq_includes[column].append(value)
        include_categories.add(category)

    # 성별(포함/제외)
    gender = target_user.get("gender")
    if gender in GENDER_TERMS:
        _add_include(gender); labels.append(gender); has_signal = True
    elif gender:
        unsupported.append("target_user.gender")
    for value in exclude.get("gender", []):
        if value in GENDER_TERMS:
            other_predicates.append(_member_eq_predicate(value, negate=True)); labels.append("non_" + value); has_signal = True
        else:
            unsupported.append("exclude.gender")

    # 연령
    age_min = target_user.get("age_min")
    if isinstance(age_min, int):
        other_predicates.append(f"B.AGE >= {age_min}"); has_signal = True
    age_max = target_user.get("age_max")
    if isinstance(age_max, int):
        other_predicates.append(f"B.AGE <= {age_max}"); has_signal = True

    # lifecycle 포함(등가/활동)
    for lifecycle in target_user.get("lifecycle", []):
        if lifecycle in MEMBER_EQ_FILTERS:
            _add_include(lifecycle); labels.append(lifecycle); has_signal = True
        elif lifecycle in MEMBER_ACTIVITY_FILTERS:
            other_predicates.append(_member_activity_predicate(MEMBER_ACTIVITY_FILTERS[lifecycle])); labels.append(lifecycle); has_signal = True
        else:
            unsupported.append("target_user.lifecycle:" + lifecycle)

    # lifecycle 제외(등가만 부정 가능; 활동 범위 부정은 모호해 미지원)
    for lifecycle in exclude.get("lifecycle", []):
        if lifecycle in MEMBER_EQ_FILTERS:
            other_predicates.append(_member_eq_predicate(lifecycle, negate=True)); labels.append("non_" + lifecycle); has_signal = True
        else:
            unsupported.append("exclude.lifecycle:" + lifecycle)

    # CRM_MB_BASEINFO 단독으로 표현할 수 없는 조건(→ unsupported 로 모아 fallback 유도)
    for field in ("interests", "preferred_channels", "behaviors", "purchase_object", "price_sensitivity", "inactivity_period"):
        if target_user.get(field):
            unsupported.append("target_user." + field)
    if exclude.get("interests"):
        unsupported.append("exclude.interests")
    for field in ("set_expressions", "computed_metrics", "policy_constraints", "semantic_resolutions"):
        if query_plan.get(field):
            unsupported.append(field)
    for field in ("category", "offer_type", "channels"):
        if campaign.get(field):
            unsupported.append("campaign_constraints." + field)

    # 같은 컬럼 포함값은 1개면 `=`, 2개 이상이면 `IN (...)` 으로 묶는다(예: 실버 OR 골드 등급).
    include_predicates: list[str] = []
    for column, values in eq_includes.items():
        if len(values) == 1:
            include_predicates.append(column + " = " + _sql_quote(values[0]))
        else:
            include_predicates.append(column + " IN (" + ", ".join(_sql_quote(value) for value in values) + ")")

    return {
        "predicates": _unique_strings([*include_predicates, *other_predicates]),
        "labels": _unique_strings(labels),
        "forces_state": "state" in include_categories,
        "has_signal": has_signal,
        "unsupported": unsupported,
    }


def build_member_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """실회원 테이블 CRM_MB_BASEINFO 로 타겟 대상 추출 SQL 을 생성한다(compile_member_target_conditions 기반).

    지원 속성(성별·연령·등급/생애주기 포함·제외)만으로 대상이 정해지면 실DB SQL 을 낸다. 미지원 조건이
    하나라도 섞이거나 회원 대상 신호가 전혀 없으면(objective 만) None 을 돌려 기존 템플릿 경로로 넘긴다
    (조건을 조용히 누락하지 않기 위함).
    """
    compiled = compile_member_target_conditions(query_plan)
    if compiled["unsupported"] or not compiled["has_signal"]:
        return None

    where_clauses = list(compiled["predicates"])
    # 회원상태(dormant 등)를 직접 지정한 타겟이 아니면 정상 회원으로 한정한다(탈퇴/휴면 제외).
    if not compiled["forces_state"]:
        where_clauses.append("B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'")
    where_clauses = _unique_strings(where_clauses)

    select_columns = ["DISTINCT B.MEMBER_NO AS CUST_ID", "B.EMART_GRADE_CD AS member_grade"]
    # 세그먼트 라벨 — 다운스트림 태그이자 조건 커버리지(값 문자열 매칭) 충족용. 제외는 'non_<canonical>'.
    if compiled["labels"]:
        select_columns.append(_sql_quote(",".join(compiled["labels"])) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        # 캠페인 목적은 타겟 필터가 아니라 메시지 목적 라벨(조건 커버리지 충족 겸용).
        select_columns.append(_sql_quote(objective) + " AS objective")

    sql = "\n".join(
        [
            "SELECT " + ", ".join(select_columns),
            "FROM CRM_MB_BASEINFO B",
            "WHERE " + "\n  AND ".join(where_clauses),
        ]
    )
    return _sql_candidate("sql_template:member_targets", "회원 속성 타겟 추출 SQL 템플릿(CRMDW)", 1.0, sql, _template_tables(sql), "sql_template")


def _should_use_cart_repurchase_template(query_plan: dict[str, Any]) -> bool:
    target_user = query_plan.get("target_user", {})
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    return "cart_abandoner" in target_user.get("behaviors", []) and objective in {None, "purchase", "repurchase", "retention"}


def assemble_sql_from_template(
    select_columns: list[str],
    base_from: str,
    condition_tokens: list[dict[str, Any]],
) -> str:
    select_columns = _unique_strings([*select_columns, *[column for token in condition_tokens for column in token.get("select_columns", [])]])
    ctes = _unique_strings([cte for token in condition_tokens for cte in token.get("ctes", [])])
    base_joins = _unique_strings([join for token in condition_tokens for join in token.get("base_joins", [])])
    joins = _template_join_sql(_unique_strings([join for token in condition_tokens for join in token["joins"]]))
    where_clauses = _unique_strings([clause for token in condition_tokens for clause in token["sql_clauses"]])
    order_by = _unique_strings([clause for token in condition_tokens for clause in token.get("order_by", [])])
    sql_parts = []
    if ctes:
        sql_parts.append("WITH " + ",\n".join(ctes))
    sql_parts.extend(["SELECT DISTINCT " + ", ".join(select_columns), base_from])
    sql_parts.extend(base_joins)
    sql_parts.extend(joins)
    if where_clauses:
        sql_parts.append("WHERE " + "\n  AND ".join(where_clauses))
    if order_by:
        sql_parts.append("ORDER BY " + ", ".join(order_by))
    return "\n".join(sql_parts)


def _template_join_sql(join_keys: list[str]) -> list[str]:
    join_sql = {
        "user_interests": "JOIN user_interests ui ON ui.user_id = u.user_id",
        "user_preferred_channels": "JOIN user_preferred_channels upc ON upc.user_id = u.user_id",
        "user_recent_behaviors": "JOIN user_recent_behaviors urb ON urb.user_id = u.user_id",
        "campaign_keywords": "JOIN campaign_keywords ck ON ck.campaign_id = c.campaign_id",
        "target_segments": "JOIN campaign_target_segments ts ON ts.campaign_id = c.campaign_id",
        "campaign_channels": "JOIN campaign_channels cc ON cc.campaign_id = c.campaign_id",
    }
    return [join_sql[key] for key in join_keys if key in join_sql]


def _template_tables(sql: str) -> list[str]:
    return _unique_strings(
        [match.group(1) for match in re.finditer(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", sql, re.IGNORECASE)]
    )


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def validate_required_input_conditions(query_plan: dict[str, Any], condition_tokens: list[dict[str, Any]]) -> dict[str, Any]:
    set_expression_missing_conditions = [
        _missing_input_condition(
            f"set_expressions.{expression.get('expression_id', 'segment_set_expression')}",
            expression.get("ko_label", expression.get("expression_id", "집합식")),
            _set_expression_issue(expression) or "집합식의 의미를 명확히 지정해 주세요.",
        )
        for expression in query_plan.get("set_expressions", [])
        if _set_expression_issue(expression)
    ]
    if set_expression_missing_conditions:
        return {
            "is_satisfied": False,
            "missing_conditions": set_expression_missing_conditions,
            "clarification_questions": [condition["question"] for condition in set_expression_missing_conditions],
        }

    computed_metric_missing_conditions = [
        _missing_input_condition(
            f"computed_metrics.{metric.get('metric_id', 'computed_formula_score')}",
            metric.get("ko_label", metric.get("metric_id", "계산식")),
            metric.get("clarification_question") or _computed_metric_intent_issue(metric, query_plan.get("intent")) or "계산식의 의미를 명확히 지정해 주세요.",
        )
        for metric in query_plan.get("computed_metrics", [])
        if metric.get("requires_clarification") or _computed_metric_intent_issue(metric, query_plan.get("intent"))
    ]
    if computed_metric_missing_conditions:
        return {
            "is_satisfied": False,
            "missing_conditions": computed_metric_missing_conditions,
            "clarification_questions": [condition["question"] for condition in computed_metric_missing_conditions],
        }

    semantic_missing_conditions = [
        _missing_input_condition(
            f"semantic_resolutions.{resolution.get('policy_id', resolution.get('canonical', 'unknown'))}",
            resolution.get("ko_label", resolution.get("canonical", "의미 해석")),
            resolution.get("clarification_question") or "모호한 표현의 의미를 명확히 지정해 주세요.",
        )
        for resolution in query_plan.get("semantic_resolutions", [])
        if resolution.get("requires_clarification")
    ]
    if semantic_missing_conditions:
        return {
            "is_satisfied": False,
            "missing_conditions": semantic_missing_conditions,
            "clarification_questions": [condition["question"] for condition in semantic_missing_conditions],
        }

    policy_missing_conditions = [
        _missing_input_condition(
            f"policy_constraints.{policy.get('policy_id', policy.get('canonical', 'unknown'))}.threshold_krw",
            policy.get("ko_label", policy.get("canonical", "업무 정책")),
            f"'{policy.get('ko_label', policy.get('canonical', '업무 정책'))}' 정책의 기준 금액을 business_policies 파일의 threshold_krw에 정의해 주세요.",
        )
        for policy in query_plan.get("policy_constraints", [])
        if policy.get("sql_behavior") == "filter" and policy.get("requires_threshold") and policy.get("threshold_krw") is None
    ]
    if policy_missing_conditions:
        return {
            "is_satisfied": False,
            "missing_conditions": policy_missing_conditions,
            "clarification_questions": [condition["question"] for condition in policy_missing_conditions],
        }

    if query_plan.get("intent") != "recommend_campaign":
        return {"is_satisfied": True, "missing_conditions": [], "clarification_questions": []}

    if condition_tokens:
        return {"is_satisfied": True, "missing_conditions": [], "clarification_questions": []}

    missing_conditions = []
    missing_conditions.append(
        _missing_input_condition(
            "query_plan.conditions",
            "추천 조건",
            "추천 기준이 되는 고객 조건이나 캠페인 조건을 지정해 주세요. 예: 쿠폰 관심 고객, 20대 여성, 장바구니 이탈 고객",
        )
    )

    return {
        "is_satisfied": not missing_conditions,
        "missing_conditions": missing_conditions,
        "clarification_questions": [condition["question"] for condition in missing_conditions],
    }


def _missing_input_condition(path: str, label: str, question: str) -> dict[str, str]:
    return {"path": path, "label": label, "question": question}


def _has_target_segment_input(query_plan: dict[str, Any]) -> bool:
    target_user = query_plan.get("target_user", {})
    return bool(
        target_user.get("behaviors")
        or target_user.get("lifecycle")
        or target_user.get("interests")
        or target_user.get("price_sensitivity")
    )


def validate_unmentioned_sql_conditions(sql: str, query_plan: dict[str, Any]) -> dict[str, Any]:
    normalized_sql = sql.casefold()
    target_user = query_plan.get("target_user", {})
    exclude = query_plan.get("exclude", {})
    set_expression_terms = _set_expression_canonical_values(query_plan.get("set_expressions", []))
    unexpected_conditions = []

    if not target_user.get("gender") and not exclude.get("gender") and not (set_expression_terms & GENDER_TERMS) and _has_gender_filter(normalized_sql):
        unexpected_conditions.append(_unexpected_sql_condition("target_user.gender", "성별 조건"))

    if target_user.get("age_min") is None and target_user.get("age_max") is None and not any(term.startswith("age_") for term in set_expression_terms) and _has_age_filter(normalized_sql):
        unexpected_conditions.append(_unexpected_sql_condition("target_user.age_range", "연령대 조건"))

    if not target_user.get("behaviors") and not target_user.get("purchase_object") and not (set_expression_terms & BEHAVIOR_TERMS) and _has_behavior_filter(normalized_sql):
        unexpected_conditions.append(_unexpected_sql_condition("target_user.behaviors", "행동 조건"))

    unexpected_segments = _unexpected_target_segments(normalized_sql, query_plan)
    for segment in unexpected_segments:
        unexpected_conditions.append(
            _unexpected_sql_condition("campaign_constraints.target_segment", f"타겟 세그먼트 조건: {segment}")
        )

    return {
        "is_satisfied": not unexpected_conditions,
        "unexpected_conditions": unexpected_conditions,
    }


def _unexpected_sql_condition(path: str, label: str) -> dict[str, str]:
    return {
        "path": path,
        "label": label,
        "reason": "SQL candidate contains a condition that was not explicit in the user query.",
    }


def _has_gender_filter(normalized_sql: str) -> bool:
    return bool(re.search(r"\bgender\b\s*(?:=|<>|!=|in\b|not\b)", normalized_sql))


def _has_age_filter(normalized_sql: str) -> bool:
    return bool(re.search(r"\bage\b\s*(?:=|<>|!=|>|<|between\b|in\b)", normalized_sql))


def _has_behavior_filter(normalized_sql: str) -> bool:
    return bool(re.search(r"\bbehavior\b\s*(?:=|like\b|in\b)", normalized_sql))


def _unexpected_target_segments(normalized_sql: str, query_plan: dict[str, Any]) -> list[str]:
    segment_values = re.findall(r"\btarget_segment\b\s*=\s*'([^']+)'", normalized_sql)
    if not segment_values:
        return []

    allowed_segments = _allowed_target_segments(query_plan)
    return [segment for segment in segment_values if segment not in allowed_segments]


def _allowed_target_segments(query_plan: dict[str, Any]) -> set[str]:
    target_user = query_plan.get("target_user", {})
    allowed_segments = set(target_user.get("behaviors", []))
    allowed_segments.update(target_user.get("lifecycle", []))
    allowed_segments.update(target_user.get("interests", []))
    if target_user.get("price_sensitivity") == "high":
        allowed_segments.add("price_sensitive")
    if target_user.get("price_sensitivity") == "low":
        allowed_segments.add("premium_buyer")
    return allowed_segments


def required_sql_conditions(query_plan: dict[str, Any]) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    target_user = query_plan.get("target_user", {})
    campaign_constraints = query_plan.get("campaign_constraints", {})
    exclude = query_plan.get("exclude", {})

    gender = target_user.get("gender")
    if gender:
        conditions.append(
            _condition(
                "target_user.gender",
                gender,
                any_terms=[],
                all_terms=["gender"],
                any_term_groups=[_condition_terms(gender, "gender")],
            )
        )

    age_min = target_user.get("age_min")
    if age_min is not None:
        conditions.append(_condition("target_user.age_min", str(age_min), [str(age_min)], all_terms=["age"]))

    age_max = target_user.get("age_max")
    if age_max is not None:
        conditions.append(_condition("target_user.age_max", str(age_max), [str(age_max)], all_terms=["age"]))

    for field_name in ("lifecycle", "interests", "preferred_channels", "behaviors"):
        for value in target_user.get(field_name, []):
            if field_name == "lifecycle" and _has_explicit_long_inactivity_period(target_user.get("inactivity_period")):
                continue
            conditions.append(_condition(f"target_user.{field_name}", value, _condition_terms(value, field_name)))
            if field_name == "behaviors":
                conditions.append(
                    _condition(
                        "campaign_constraints.target_segment",
                        value,
                        _condition_terms(value, field_name),
                        all_terms=["target_segment"],
                    )
                )

    purchase_object = target_user.get("purchase_object")
    if purchase_object:
        conditions.append(
            _condition(
                "target_user.purchase_object",
                purchase_object,
                [purchase_object, "purchased:%"],
                all_terms=["behavior"],
            )
        )

    inactivity_period = target_user.get("inactivity_period")
    if isinstance(inactivity_period, dict) and isinstance(inactivity_period.get("sql_interval"), str):
        conditions.append(
            _condition(
                "target_user.inactivity_period",
                inactivity_period["sql_interval"],
                [inactivity_period["sql_interval"], str(inactivity_period.get("min_days", ""))],
                all_terms=["last_login_at"],
            )
        )

    price_sensitivity = target_user.get("price_sensitivity")
    if price_sensitivity:
        conditions.append(_condition("target_user.price_sensitivity", price_sensitivity, ["price_sensitive", "price_sensitivity", price_sensitivity]))

    for value in campaign_constraints.get("category", []):
        conditions.append(_condition("campaign_constraints.category", value, _condition_terms(value, "category")))

    # 채널도 생성부(build_verified_condition_tokens)와 동일하게 recommend_campaign 에서만 요구한다.
    # "발송 채널: RCS" 표기로 채널이 잡혀도 find_user_segment 에선 캠페인 채널 절을 만들지 않으므로,
    # 검증부가 이를 요구하면 커버리지가 깨져 sql=None("검증 SQL 없음")이 된다.
    if query_plan.get("intent") == "recommend_campaign" and not _is_cart_dimension_targeting(query_plan):
        for value in campaign_constraints.get("channels", []):
            conditions.append(_condition("campaign_constraints.channels", value, _condition_terms(value, "channels")))

    objective = campaign_constraints.get("objective")
    # 생성부(build_verified_condition_tokens)와 동일하게 CAMPAIGN_OBJECTIVES로 게이트한다.
    # 생성부는 지원 objective만 SQL 절로 내보내는데 검증부가 임의 objective를 요구하면
    # 커버리지 검증이 실패해 sql=None이 되고 "검증된 SQL 없음"으로 빠진다.
    # 장바구니 디멘션(브랜드) 타겟팅은 순수 오디언스 추출 SQL이라 캠페인 objective/채널 컬럼이 없다.
    # 이 모드에선 objective/채널 커버리지를 요구하지 않고 브랜드 코드 조건만 요구한다.
    if query_plan.get("intent") == "recommend_campaign" and objective in CAMPAIGN_OBJECTIVES and not _is_cart_dimension_targeting(query_plan):
        conditions.append(_condition("campaign_constraints.objective", objective, [objective], all_terms=["objective"]))

    brand_filter = _cart_dimension_brand_filter(query_plan)
    if brand_filter is not None:
        column_short = brand_filter.get("column", "").split(".")[-1]
        for code in brand_filter.get("codes", []):
            if column_short and code:
                conditions.append(
                    _condition(
                        "dimension_filters." + str(brand_filter.get("dimension_id", "dimension")),
                        code,
                        [_sql_quote(code)],
                        all_terms=[column_short],
                    )
                )

    offer_type = campaign_constraints.get("offer_type")
    if offer_type:
        conditions.append(_condition("campaign_constraints.offer_type", offer_type, _condition_terms(offer_type, "offer_type")))

    for expression in query_plan.get("set_expressions", []):
        if _set_expression_issue(expression):
            continue
        compiled = _compile_set_expression_ast(expression["set_ast"])
        conditions.append(
            _condition(
                "set_expressions",
                expression.get("expression_id", "segment_set_expression"),
                [compiled["expression_sql"]],
            )
        )

    for policy in query_plan.get("policy_constraints", []):
        expression = _policy_sql_expression(policy)
        if not expression:
            continue
        if policy.get("sql_behavior") == "rank":
            conditions.append(
                _condition(
                    "policy_constraints",
                    policy.get("canonical", "business_policy"),
                    [policy.get("metric", ""), expression],
                    all_terms=["order by"],
                )
            )
        elif policy.get("sql_behavior") == "filter" and policy.get("threshold_krw") is not None:
            conditions.append(
                _condition(
                    "policy_constraints",
                    policy.get("canonical", "business_policy"),
                    [policy.get("metric", ""), expression, str(policy.get("threshold_krw"))],
                )
            )

    for metric in query_plan.get("computed_metrics", []):
        if metric.get("requires_clarification") or _computed_metric_intent_issue(metric, query_plan.get("intent")):
            continue
        compiled = compile_formula_ast(metric["formula_ast"], schema_path=DEFAULT_SCHEMA_PATH)
        if not compiled["is_valid"]:
            continue
        expression = compiled["expression_sql"]
        alias = _safe_metric_alias(metric.get("metric_id")) or "computed_formula_score"
        behavior = metric.get("sql_behavior") or "select"
        if behavior == "rank":
            conditions.append(_condition("computed_metrics", alias, [alias, expression], all_terms=["order by"]))
        elif behavior == "filter" and metric.get("threshold") is not None:
            conditions.append(_condition("computed_metrics", alias, [alias, expression, str(metric.get("threshold"))]))
        else:
            conditions.append(_condition("computed_metrics", alias, [alias, expression]))

    for resolution in query_plan.get("semantic_resolutions", []):
        if resolution.get("requires_clarification"):
            continue
        default_select = resolution.get("default_select")
        if isinstance(default_select, str):
            conditions.append(
                _condition(
                    "semantic_resolutions",
                    resolution.get("canonical", "semantic_resolution"),
                    [default_select, resolution.get("default_column", ""), resolution.get("ambiguous_term", "")],
                )
            )

    for field_name, values in exclude.items():
        for value in values:
            if field_name == "gender":
                conditions.append(
                    _condition(
                        f"exclude.{field_name}",
                        value,
                        any_terms=[],
                        all_terms=["gender"],
                        any_term_groups=[_condition_terms(value, field_name), ["<>", "!=", " not ", "not("]],
                    )
                )
            else:
                conditions.append(
                    _condition(
                        f"exclude.{field_name}",
                        value,
                        any_terms=[],
                        any_term_groups=[_condition_terms(value, field_name), ["<>", "!=", " not ", "not("]],
                    )
                )

    return conditions


def validate_sql_condition_coverage(sql: str, required_conditions: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_sql = sql.casefold()
    missing_conditions = []
    matched_conditions = []

    for condition in required_conditions:
        all_terms_matched = all(term.casefold() in normalized_sql for term in condition["all_terms"])
        any_terms_matched = not condition["any_terms"] or any(
            term.casefold() in normalized_sql for term in condition["any_terms"]
        )
        any_term_groups_matched = all(
            any(term.casefold() in normalized_sql for term in term_group)
            for term_group in condition.get("any_term_groups", [])
        )
        if all_terms_matched and any_terms_matched and any_term_groups_matched:
            matched_conditions.append(condition)
        else:
            missing_conditions.append(condition)

    return {
        "is_satisfied": not missing_conditions,
        "required_count": len(required_conditions),
        "matched_count": len(matched_conditions),
        "missing_conditions": missing_conditions,
    }


def validate_sql_intent_scope(candidate: dict[str, Any], query_plan: dict[str, Any]) -> dict[str, Any]:
    intent = query_plan.get("intent")
    campaign_tables = {"campaigns", "campaign_channels", "campaign_target_segments", "campaign_keywords"}
    tables = set(candidate.get("tables", []))
    if intent == "find_user_segment" and tables & campaign_tables:
        return {
            "is_satisfied": False,
            "reason": "find_user_segment must not select campaign recommendation SQL.",
            "blocked_tables": sorted(tables & campaign_tables),
        }
    return {"is_satisfied": True, "reason": None, "blocked_tables": []}


def _condition(
    path: str,
    value: str,
    any_terms: list[str],
    all_terms: list[str] | None = None,
    any_term_groups: list[list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "path": path,
        "value": value,
        "any_terms": _unique_strings([term for term in any_terms if term]),
        "all_terms": _unique_strings([term for term in (all_terms or []) if term]),
        "any_term_groups": [
            _unique_strings([term for term in term_group if term])
            for term_group in (any_term_groups or [])
        ],
    }


def _condition_terms(value: str, field_name: str) -> list[str]:
    aliases = {
        "female": ["female", "여성", "여자"],
        "male": ["male", "남성", "남자"],
        "cart_abandoner": ["cart_abandoned", "cart_abandoner", "장바구니"],
        "coupon": ["coupon", "쿠폰", "할인"],
        "app_push": ["app_push", "앱푸시"],
        "kakao": ["kakao", "카카오", "카톡"],
        "price_sensitive": ["price_sensitive", "가격", "쿠폰", "할인"],
    }
    if value in aliases:
        return aliases[value]
    if field_name in {"interests", "category"}:
        return [value]
    if field_name in {"preferred_channels", "channels"}:
        return [value]
    if field_name == "behaviors":
        return [value]
    return [value]


def _sql_candidate(node_id: str, title: str, score: float, sql: str, tables: list[str], source: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "title": title,
        "score": round(score, 6),
        "source": source,
        "tables": tables,
        "sql": sql,
    }


def build_stage_log(
    query_plan: dict[str, Any],
    vector_hits: list[SearchHit],
    keyword_hits: list[SearchHit],
    merged_hits: list[SearchHit],
    context_nodes: list[dict[str, Any]],
    context_assembly: dict[str, Any],
    sql_result: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "stage": "1. Query Planning",
            "summary": f"intent={query_plan['intent']}, normalized_query={query_plan['retrieval']['query']}",
            "metrics": {
                "matched_terms": len(query_plan["matched_terms"]),
                "retrieval_terms": len(query_plan["retrieval"]["terms"]),
            },
        },
        {
            "stage": "2. Hybrid Retrieval",
            "summary": "Dense vector hits and BM25 keyword hits were collected as seed candidates.",
            "metrics": {"vector_hits": len(vector_hits), "keyword_hits": len(keyword_hits)},
        },
        {
            "stage": "3. Merge / Score Sort",
            "summary": "Duplicate node ids were merged by highest score and sorted by relevance.",
            "metrics": {"merged_hits": len(merged_hits)},
        },
        {
            "stage": "4. Graph Expansion",
            "summary": "Seed nodes were expanded through graph relationships to build retrieval context.",
            "metrics": {"context_nodes": len(context_nodes)},
        },
        {
            "stage": "5. Context Assembly",
            "summary": "Top-K chunks, graph context, metadata, and prompt context were assembled.",
            "metrics": context_assembly["metadata"],
        },
        {
            "stage": "6. SQL Template / Guard",
            "summary": "Verified condition tokens were assembled into an intent SQL template and validated by sql_guard.",
            "metrics": {
                "candidate_count": sql_result["candidate_count"],
                "condition_tokens": len(sql_result.get("condition_tokens", [])),
                "selected_sql": bool(sql_result["sql"]),
                "selected_valid": bool(sql_result["selected"] and sql_result["selected"].get("is_eligible")),
                "required_conditions": len(sql_result["required_conditions"]),
                "failure_reason": sql_result["failure_reason"] or "none",
            },
        },
    ]


def render_stage_log(stage_log: list[dict[str, Any]]) -> str:
    lines = []
    for entry in stage_log:
        metrics = ", ".join(f"{key}={value}" for key, value in entry["metrics"].items())
        lines.append(f"- {entry['stage']}: {entry['summary']} ({metrics})")
    return "\n".join(lines)


def build_retrieve_trace(result: dict[str, Any]) -> dict[str, Any]:
    """retrieve() 결과를 '의미추론 → 벡터검색 → 키워드검색 → Graph확장 → SQL생성/검증'
    단계별 트레이스로 재구성한다(시연/디버깅용). LLM 호출 없이 결정론적으로 동작."""
    query_plan = result.get("query_plan", {})
    sql_result = result.get("sql_result", {})
    api_response = result.get("api_response", {})
    target_user = query_plan.get("target_user", {})
    retrieval = query_plan.get("retrieval", {})

    def _hit_rows(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "rank": index + 1,
                "id": hit.get("id"),
                "type": hit.get("type"),
                "score": hit.get("score"),
                "snippet": (hit.get("text") or "")[:160],
            }
            for index, hit in enumerate(hits)
        ]

    graph_rows = [
        {
            "rank": index + 1,
            "id": node.get("id"),
            "type": node.get("type"),
            "title": node.get("title"),
            "score": node.get("score"),
            "seed_score": node.get("seed_score"),
            "is_seed": node.get("seed_score") is not None,
            "reached_via": node.get("reasons", []),
        }
        for index, node in enumerate(result.get("graph_context", []))
    ]

    candidate_rows = [
        {
            "id": candidate.get("id"),
            "source": candidate.get("source"),
            "tables": candidate.get("tables", []),
            "is_eligible": candidate.get("is_eligible"),
            "guard_valid": candidate.get("validation", {}).get("is_valid"),
            "coverage_ok": candidate.get("coverage", {}).get("is_satisfied"),
            "coverage_missing": [
                condition.get("path")
                for condition in candidate.get("coverage", {}).get("missing_conditions", [])
            ],
            "intent_scope_ok": candidate.get("intent_scope", {}).get("is_satisfied"),
            "unmentioned_ok": candidate.get("unmentioned_conditions", {}).get("is_satisfied"),
            "sql": candidate.get("sql"),
        }
        for candidate in sql_result.get("candidates", [])
    ]

    return {
        "query": result.get("query"),
        "collection": result.get("collection"),
        "stages": [
            {
                "step": 1,
                "name": "요청 이해 — 고객 문장을 조건으로 해석",
                "description": "고객이 입력한 문장을 시스템이 쓰는 표준 용어·검색어·타겟 조건으로 바꾸는 단계입니다.",
                "tech_name": "의미 추론 (Query Planning / Normalization)",
                "intent": query_plan.get("intent"),
                "matched_terms": query_plan.get("matched_terms", []),
                "semantic_resolutions": query_plan.get("semantic_resolutions", []),
                "target_user": {key: value for key, value in target_user.items() if value not in (None, [], {})},
                "dimension_filters": query_plan.get("dimension_filters", []),
                "cart_context": query_plan.get("cart_context"),
                "retrieval_query": retrieval.get("query"),
                "retrieval_terms": retrieval.get("terms", []),
            },
            {
                "step": 2,
                "name": "비슷한 의미의 지식 찾기 — AI 유사도 검색",
                "description": "뜻이 가까운 용어·규칙·예시를 AI가 의미 기반으로 찾아옵니다. 단어가 달라도 뜻이 비슷하면 잡힙니다.",
                "tech_name": "벡터 검색 (Dense / Qdrant)",
                "count": len(result.get("vector_matches", [])),
                "hits": _hit_rows(result.get("vector_matches", [])),
            },
            {
                "step": 3,
                "name": "같은 단어의 지식 찾기 — 키워드 검색",
                "description": "입력한 단어와 글자가 실제로 일치하는 용어·예시를 찾습니다. 2단계(의미)와 서로 보완합니다.",
                "tech_name": "키워드 검색 (Lexical over graph)",
                "count": len(result.get("keyword_matches", [])),
                "hits": _hit_rows(result.get("keyword_matches", [])),
            },
            {
                "step": 4,
                "name": "찾은 지식 연결·확장 — 관계 그래프",
                "description": "2·3단계에서 찾은 항목을 출발점으로, 연결된 테이블·컬럼까지 관계를 타고 넓혀 필요한 재료를 모읍니다.",
                "tech_name": "병합 + Graph 확장 (GraphRAG)",
                "seed_count": len(result.get("seed_matches", [])),
                "context_count": len(graph_rows),
                "context_nodes": graph_rows,
            },
            {
                "step": 5,
                "name": "대상 추출 쿼리 만들기·검증 — 최종 SQL",
                "description": "모은 조건으로 고객을 뽑아내는 조회문(SQL)을 만들고, 요청과 어긋나지 않는지 자동 점검한 뒤 확정합니다.",
                "tech_name": "SQL 생성 / 검증 (Template + sql_guard)",
                "condition_tokens": [token.get("path") for token in sql_result.get("condition_tokens", [])],
                "required_conditions": [condition.get("path") for condition in sql_result.get("required_conditions", [])],
                "candidates": candidate_rows,
                "selected_sql": sql_result.get("sql"),
                "target_connection": sql_result.get("target_connection"),
                "target_dialect": sql_result.get("target_dialect"),
                "is_success": sql_result.get("is_success"),
                "failure_reason": sql_result.get("failure_reason"),
            },
        ],
        "stage_log": result.get("stage_log", []),
        "result": {
            "status": api_response.get("status"),
            "sql": api_response.get("sql"),
            "target_connection": api_response.get("target_connection"),
            "message": api_response.get("message"),
        },
        "timings_ms": result.get("timings_ms", {}),
    }


def graph_stats(graph: nx.Graph) -> dict[str, Any]:
    node_types = Counter(nx.get_node_attributes(graph, "node_type").values())
    edge_types = Counter(edge_data.get("relation", "related") for _, _, edge_data in graph.edges(data=True))
    return {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "node_types": dict(sorted(node_types.items())),
        "edge_types": dict(sorted(edge_types.items())),
    }


def _query_tokens(query: str) -> list[str]:
    raw_tokens = [token.strip().lower() for token in query.replace("_", " ").split()]
    compact_query = query.replace(" ", "").lower()
    tokens = {token for token in raw_tokens if len(token) >= 2}
    for raw_token in raw_tokens:
        if raw_token:
            tokens.add(raw_token.replace(" ", ""))
    if "앱푸시" in compact_query:
        tokens.update({"앱푸시", "앱 푸시", "app_push"})
    if "카카오" in query or "카톡" in query:
        tokens.update({"kakao", "카카오", "카톡"})
    if "coupon" in query.lower() or "쿠폰" in query or "할인" in query:
        tokens.update({"coupon", "쿠폰", "할인"})
    if "sql" in query.lower() or "쿼리" in query:
        tokens.update({"sql", "select", "쿼리"})
    return sorted(tokens, key=len, reverse=True)


def _add_schema_edges(graph: nx.Graph, node: dict[str, Any], nodes_by_id: dict[str, dict[str, Any]]) -> None:
    table_node_id = node["id"]
    table_name = node["table_name"]
    for column in node.get("columns", []):
        column_node_id = _column_node_id(table_name, column["name"])
        graph.add_node(
            column_node_id,
            node_type="schema_column",
            title=f"{table_name}.{column['name']}",
            text=f"컬럼 {table_name}.{column['name']} {column['type']}",
            payload={"id": column_node_id, "type": "schema_column", "table_name": table_name, **column},
        )
        graph.add_edge(table_node_id, column_node_id, relation="has_column")

        reference = column.get("references")
        if reference:
            target_table_node_id = f"schema_table:{reference['table']}"
            target_column_node_id = _column_node_id(reference["table"], reference["column"])
            if target_table_node_id in nodes_by_id:
                graph.add_edge(table_node_id, target_table_node_id, relation="foreign_key_to")
                graph.add_edge(column_node_id, target_table_node_id, relation="references_table")
            if target_column_node_id in graph:
                graph.add_edge(column_node_id, target_column_node_id, relation="references_column")

    for foreign_key in node.get("foreign_keys", []):
        reference = foreign_key.get("references", {})
        target_table = reference.get("table")
        if not target_table:
            continue
        target_table_node_id = f"schema_table:{target_table}"
        if target_table_node_id in nodes_by_id:
            graph.add_edge(table_node_id, target_table_node_id, relation="foreign_key_to")
        for column_name, target_column_name in zip(foreign_key.get("columns", []), reference.get("columns", [])):
            column_node_id = _column_node_id(table_name, column_name)
            target_column_node_id = _column_node_id(target_table, target_column_name)
            if column_node_id in graph and target_table_node_id in nodes_by_id:
                graph.add_edge(column_node_id, target_table_node_id, relation="references_table")
            if column_node_id in graph and target_column_node_id in graph:
                graph.add_edge(column_node_id, target_column_node_id, relation="references_column")


def _add_business_term_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    for table_name in node.get("related_tables", []):
        table_node_id = f"schema_table:{table_name}"
        if table_node_id in graph:
            graph.add_edge(node["id"], table_node_id, relation="business_term_table")

    for column_name in node.get("related_columns", []):
        column_node_id = f"schema_column:{column_name}"
        if column_node_id in graph:
            graph.add_edge(node["id"], column_node_id, relation="business_term_column")


def _add_business_policy_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    for table_name in node.get("related_tables", []):
        table_node_id = f"schema_table:{table_name}"
        if table_node_id in graph:
            graph.add_edge(node["id"], table_node_id, relation="business_policy_table")

    for column_name in node.get("related_columns", []):
        column_node_id = f"schema_column:{column_name}"
        if column_node_id in graph:
            graph.add_edge(node["id"], column_node_id, relation="business_policy_column")


def _add_metric_alias_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    for table_name in node.get("related_tables", []):
        table_node_id = f"schema_table:{table_name}"
        if table_node_id in graph:
            graph.add_edge(node["id"], table_node_id, relation="metric_alias_table")

    for column_name in node.get("related_columns", []):
        column_node_id = f"schema_column:{column_name}"
        if column_node_id in graph:
            graph.add_edge(node["id"], column_node_id, relation="metric_alias_column")


def _add_normalization_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    for column_name in NORMALIZATION_COLUMN_HINTS.get(node.get("canonical"), []):
        column_node_id = f"schema_column:{column_name}"
        if column_node_id in graph:
            graph.add_edge(node["id"], column_node_id, relation="normalizes_column_value")

    business_term_node_id = f"business_term:{node.get('canonical')}"
    if business_term_node_id in graph:
        graph.add_edge(node["id"], business_term_node_id, relation="normalizes_business_term")


def _add_dimension_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    # 디멘션(예: 상품브랜드)을 실제 필터 대상 스키마 테이블/컬럼에 연결해
    # 브랜드명 -> 코드 -> BRAND_ID IN (...) 경로가 스키마 허브로 이어지게 한다.
    table_name = node.get("target_table")
    if table_name:
        table_node_id = f"schema_table:{table_name}"
        if table_node_id in graph:
            graph.add_edge(node["id"], table_node_id, relation="dimension_filters_table")

    column_name = node.get("target_column")
    if column_name:
        column_node_id = f"schema_column:{column_name}"
        if column_node_id in graph:
            graph.add_edge(node["id"], column_node_id, relation="dimension_filters_column")


def _add_dimension_value_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    dimension_node_id = f"dimension:{node.get('dimension_id')}"
    if dimension_node_id in graph:
        graph.add_edge(node["id"], dimension_node_id, relation="value_of_dimension")


def _add_sql_example_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    for table_name in node.get("tables", []):
        table_node_id = f"schema_table:{table_name}"
        if table_node_id in graph:
            graph.add_edge(node["id"], table_node_id, relation="sql_uses_table")


# 캠페인/사용자 속성값을 대응하는 스키마 컬럼(및 테이블)에 연결해 그래프 확장이
# 정규화 사전/업무 용어 경로와 동일한 스키마 허브로 이어지도록 한다.
CAMPAIGN_VALUE_COLUMNS = {
    "objective": ("campaigns.objective", "campaign_objective"),
    "category": ("campaigns.category", "campaign_category"),
    "channel": ("campaign_channels.channel", "campaign_channel"),
    "target_segments": ("campaign_target_segments.target_segment", "campaign_target_segment"),
    "keywords": ("campaign_keywords.keyword", "campaign_keyword"),
}
USER_VALUE_COLUMNS = {
    "gender": ("users.gender", "user_gender"),
    "region": ("users.region", "user_region"),
    "lifecycle": ("users.lifecycle", "user_lifecycle"),
    "price_sensitivity": ("users.price_sensitivity", "user_price_sensitivity"),
    "predicted_ltv_segment": ("users.predicted_ltv_segment", "user_ltv_segment"),
    "interests": ("user_interests.interest", "user_interest"),
    "preferred_channels": ("user_preferred_channels.preferred_channel", "user_preferred_channel"),
    "recent_behaviors": ("user_recent_behaviors.behavior", "user_recent_behavior"),
}


def _add_campaign_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    _add_attribute_column_edges(graph, node, "schema_table:campaigns", "campaign_of_table", CAMPAIGN_VALUE_COLUMNS)


def _add_user_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    _add_attribute_column_edges(graph, node, "schema_table:users", "user_of_table", USER_VALUE_COLUMNS)


def _add_attribute_column_edges(
    graph: nx.Graph,
    node: dict[str, Any],
    table_node_id: str,
    table_relation: str,
    value_columns: dict[str, tuple[str, str]],
) -> None:
    source_node_id = node["id"]
    if table_node_id in graph:
        graph.add_edge(source_node_id, table_node_id, relation=table_relation)

    for field_name, (table_column, relation) in value_columns.items():
        values = [value for value in _as_value_list(node.get(field_name)) if _is_present_value(value)]
        if not values:
            continue

        # 스키마 허브 연결: 속성 -> 컬럼 (모든 인스턴스가 공유하는 넓은 연결)
        column_node_id = f"schema_column:{table_column}"
        if column_node_id in graph:
            graph.add_edge(source_node_id, column_node_id, relation=relation)

        # 선택적 의미 연결: 값이 정규화 canonical과 일치하면 해당 규칙 노드로 직접 연결한다.
        # 컬럼 허브와 달리 이 엣지는 그 값을 가진 인스턴스에만 붙어 검색 선택성을 높인다.
        for value in values:
            if not isinstance(value, str):
                continue
            rule_node_id = f"normalization_rule:{value}"
            if rule_node_id in graph:
                graph.add_edge(source_node_id, rule_node_id, relation=f"{relation}_term")


def _as_value_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _is_present_value(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _node_title(node: dict[str, Any]) -> str:
    if node["type"] == "schema_table":
        return node.get("table_name", node["id"])
    if node["type"] == "normalization_rule":
        return node.get("canonical", node["id"])
    if node["type"] == "business_term":
        return node.get("term", node["id"])
    if node["type"] == "business_policy":
        return node.get("ko_label", node.get("canonical", node["id"]))
    if node["type"] == "metric_alias":
        return node.get("ko_label", node.get("canonical", node["id"]))
    if node["type"] == "sql_example":
        return node.get("title", node["id"])
    if node["type"] == "dimension":
        return node.get("prompt_label", node["id"])
    if node["type"] == "dimension_value":
        return node.get("name", node["id"])
    if node["type"] == "campaign":
        return node.get("name", node["id"])
    if node["type"] == "user":
        return node.get("id", node["id"])
    return node["id"]


def _column_node_id(table_name: str, column_name: str) -> str:
    return f"schema_column:{table_name}.{column_name}"


def _neighbor_summary(graph: nx.Graph, node_id: str) -> list[dict[str, str]]:
    neighbors = []
    for neighbor_id in list(graph.neighbors(node_id))[:12]:
        edge_data = graph.get_edge_data(node_id, neighbor_id) or {}
        neighbors.append(
            {
                "id": neighbor_id,
                "type": graph.nodes[neighbor_id].get("node_type", "unknown"),
                "relation": edge_data.get("relation", "related"),
            }
        )
    return neighbors


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keep_keys = [
        "id",
        "type",
        "table_name",
        "description",
        "columns",
        "canonical",
        "ko_label",
        "synonyms",
        "negative_synonyms",
        "term",
        "policy_id",
        "metric",
        "scope",
        "expression",
        "operator",
        "threshold_krw",
        "requires_threshold",
        "sql_behavior",
        "order_by",
        "related_tables",
        "related_columns",
        "title",
        "name",
        "objective",
        "category",
        "channel",
        "channels",
        "target_segments",
        "offer",
        "start_date",
        "end_date",
        "keywords",
        "expected_ctr",
        "expected_cvr",
        "campaign_id",
        "emphasis_type",
        "message_text",
        "brand_tone",
        "sql",
        "tables",
        "text_for_embedding",
    ]
    return {key: payload[key] for key in keep_keys if key in payload}


def _hit_result(hit: SearchHit) -> dict[str, Any]:
    return {
        "id": hit.node_id,
        "score": round(hit.score, 6),
        "type": hit.payload.get("node_type"),
        "text": hit.payload.get("text", "")[:500],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run graph-expanded retrieval over the campaign knowledge RAG collection.")
    parser.add_argument("query", nargs="?", help="Natural language query to retrieve context for.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_PATH, help="RAG knowledge JSON path.")
    parser.add_argument("--normalization-rules", type=Path, default=DEFAULT_NORMALIZATION_PATH, help="Normalization dictionary JSON path for query planning.")
    parser.add_argument("--business-policies", type=Path, default=DEFAULT_POLICY_PATH, help="Business policy JSON path for query planning.")
    parser.add_argument("--metric-lexicon", type=Path, default=DEFAULT_METRIC_LEXICON_PATH, help="Metric alias JSON path for computed formula query planning.")
    parser.add_argument("--url", default=os.getenv("QDRANT_URL", "http://localhost:6333"), help="Qdrant URL.")
    parser.add_argument("--api-key", default=os.getenv("QDRANT_API_KEY"), help="Qdrant API key.")
    parser.add_argument("--collection", default=os.getenv("QDRANT_GRAPH_COLLECTION", DEFAULT_COLLECTION), help="Qdrant collection name.")
    parser.add_argument("--embedding-model", default=os.getenv("QDRANT_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL), help="FastEmbed model name.")
    parser.add_argument("--query-parser", choices=["rules", "auto", "llm"], default=os.getenv("QUERY_PARSER", "rules"), help="Query planning parser. auto/llm uses OpenAI when OPENAI_API_KEY is available and falls back to rules.")
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL, help="OpenAI model for optional query parsing and answer generation.")
    parser.add_argument("--generate-answer", action="store_true", help="Call OpenAI to generate the final answer from answer_prompt.")
    parser.add_argument("--generate-messages", action="store_true", help="Call OpenAI to generate LMS/RCS message variants after SQL generation succeeds.")
    parser.add_argument("--message-channel", choices=["auto", "lms", "rcs", "rcsSms"], default="auto", help="Message channel to generate. auto uses Query Plan LMS/RCS channel or defaults to LMS.")
    parser.add_argument("--message-policy", type=Path, default=DEFAULT_MESSAGE_POLICY_PATH, help="Channel message policy JSON path for prompt constraints and validation.")
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR, help="Directory containing prompt templates used by LLM query planning and answer generation.")
    parser.add_argument("--sql-schema", type=Path, default=DEFAULT_SCHEMA_PATH, help="Schema catalog JSON path for SQL guard validation.")
    parser.add_argument("--sql-limit", type=int, default=DEFAULT_LIMIT, help="Default LIMIT to apply to template-generated SQL.")
    parser.add_argument("--vector-top-k", type=int, default=5, help="Number of vector seed nodes.")
    parser.add_argument("--keyword-top-k", type=int, default=5, help="Number of local keyword seed nodes to blend with vector seeds.")
    parser.add_argument("--graph-top-k", type=int, default=15, help="Number of graph-expanded context nodes.")
    parser.add_argument("--hops", type=int, default=2, help="Graph expansion hops from vector seed nodes.")
    parser.add_argument("--stats", action="store_true", help="Print graph statistics and exit.")
    parser.add_argument("--format", choices=["json", "text"], default="text", help="Output format.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = load_payload(args.data)
    graph = build_graph(payload)

    if args.stats:
        print(json.dumps(graph_stats(graph), ensure_ascii=False, indent=2))
        return

    if not args.query:
        raise SystemExit("query is required unless --stats is used.")

    with rag_llm_run_scope():
        result = retrieve(
            query=args.query,
            graph=graph,
            collection=args.collection,
            url=args.url,
            api_key=args.api_key,
            embedding_model_name=args.embedding_model,
            vector_top_k=args.vector_top_k,
            keyword_top_k=args.keyword_top_k,
            graph_top_k=args.graph_top_k,
            hops=args.hops,
            normalization_rules=args.normalization_rules,
            business_policies=args.business_policies,
            metric_lexicon=args.metric_lexicon,
            sql_schema=args.sql_schema,
            sql_limit=args.sql_limit,
            query_parser=args.query_parser,
            llm_model=args.llm_model,
            generate_answer=args.generate_answer,
            generate_messages=args.generate_messages,
            message_channel=args.message_channel,
            message_policy=args.message_policy,
            prompt_dir=args.prompt_dir,
        )
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"QUERY: {result['query']}")

    print("\nSTAGE LOG")
    print(render_stage_log(result["stage_log"]))

    print("\nQUERY PLAN")
    print(json.dumps(result["query_plan"], ensure_ascii=False, indent=2))

    print("\nSQL RESULT")
    if result["sql_result"]["sql"]:
        print(result["sql_result"]["sql"])
        selected = result["sql_result"]["selected"]
        print(f"source={selected['id']} ({selected['title']}), valid={selected['validation']['is_valid']}")
    else:
        print("No SQL template satisfied SQL guard and Query Plan condition coverage.")
        if result["sql_result"]["failure_reason"]:
            print(f"failure_reason={result['sql_result']['failure_reason']}")
        if result["sql_result"].get("clarification_questions"):
            print("clarification_questions=")
            for question in result["sql_result"]["clarification_questions"]:
                print(f"- {question}")
        selected = result["sql_result"]["selected"]
        if selected and selected.get("coverage"):
            missing = selected["coverage"]["missing_conditions"]
            print("missing_conditions=" + json.dumps(missing, ensure_ascii=False))

    print("\nAPI RESPONSE")
    print(json.dumps(result["api_response"], ensure_ascii=False, indent=2))

    if result["answer"]["content"]:
        print("\nANSWER")
        print(result["answer"]["content"])

    print("\nMESSAGE GENERATION")
    print(json.dumps(result["message_generation"], ensure_ascii=False, indent=2))

    print("\nVECTOR MATCHES")
    for match in result["vector_matches"]:
        print(f"- {match['id']} ({match['type']}, score={match['score']})")

    print("\nKEYWORD MATCHES")
    for match in result["keyword_matches"]:
        print(f"- {match['id']} ({match['type']}, score={match['score']})")

    print("\nGRAPH CONTEXT")
    for node in result["graph_context"]:
        print(f"- {node['id']} [{node['type']}] score={node['score']}")
        for neighbor in node["neighbors"][:4]:
            print(f"  -> {neighbor['relation']}: {neighbor['id']}")

    print("\nPROMPT CONTEXT")
    print(result["prompt_context"])


if __name__ == "__main__":
    main()