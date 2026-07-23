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
from confidence import render_confidence_markdown, render_confidence_report, score_targeting_confidence


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

CAMPAIGN_OBJECTIVES = {"purchase", "repurchase", "retention", "reactivation", "subscription", "awareness"}
# ── 실회원(CRM_MB_BASEINFO) 타겟 속성 레지스트리 ──────────────────────────────
# recommend_campaign 의 타겟을 데모 스키마(users/campaigns) 대신 실회원 테이블로 추출하기 위한
# "조건 -> 실컬럼 술어" 매핑의 단일 출처는 docs/data/member_target_filters.json 이다. 새 속성/값
# 지원(등급 추가, 상태 추가 등)은 코드 수정이 아니라 그 파일에 항목만 추가하면 되고, 조합은
# compile_member_target_conditions 가 자동 처리한다(포함/제외/연령 등 임의 조합). 아래
# _DEFAULT_MEMBER_TARGET_FILTERS 는 파일 부재/파손 시 폴백이자 스키마 예시다.
#
# eq_filters: canonical 값 -> (범주, 실컬럼, 저장값). 포함은 `=`, 제외는 `<>` 로 자동 생성.
#   저장값은 코드도메인 접두어를 포함한다(실DB 조회로 확인: GENDER_CD.FEMALE / MEM_GRADE_CD.VIP /
#   MEMBER_STATE_CD.SLEEP / DEVICE_TYPE_CD.APP). 범주 state 는 회원상태 직접 지정(기본 NORMAL 한정 해제).
#   같은 컬럼(grade)에 값이 여러 개면 compile_member_target_conditions 가 IN 으로 묶는다(OR).
# activity_filters: canonical -> 미접속 일수. LAST_LOGIN_DATE(YYYYMMDD 문자열) 사전식 비교.
#   범위 조건이라 제외(부정)는 의미가 모호해 미지원(→ fallback).
# lifecycle_extra_terms: 어휘로는 존재하나 등가/활동 필터로는 표현 못 하는 lifecycle canonical.
#   new_user 는 여기 남겨 LLM 파서 어휘(LIFECYCLE_TERMS)로 인식시키되, 실컬럼 매핑은 signup_target
#   (REG_DT 최근 N일 창)이 담당해 compile_member_target_conditions 가 술어로 만든다(→ fallback 아님).
# signup_target: 신규 가입 타겟(REG_DT, YYYYMMDD). REG_TYPE_CD.NEW 는 96%라 무의미해 가입일 창으로 정의한다.
#   anchor="data_max" 는 실적재 데이터 최신일(MAX(REG_DT)) 기준 최근 default_days 일 — 데모 데이터가
#   과거(2022~2023)라 GETDATE 기준이면 0명이 되는 문제를 피한다. 운영 전환 시 anchor="getdate" 로 바꾼다.
DEFAULT_MEMBER_TARGET_FILTERS_PATH = Path(
    os.getenv("GRAPH_RAG_MEMBER_TARGET_FILTERS", "docs/data/member_target_filters.json")
)

_DEFAULT_MEMBER_TARGET_FILTERS: dict[str, Any] = {
    "eq_filters": [
        {"canonical": "female", "category": "gender", "column": "B.GENDER_CD", "value": "GENDER_CD.FEMALE"},
        {"canonical": "male", "category": "gender", "column": "B.GENDER_CD", "value": "GENDER_CD.MALE"},
        {"canonical": "welcome_grade", "category": "grade", "column": "B.EMART_GRADE_CD", "value": "MEM_GRADE_CD.WELCOME"},
        {"canonical": "family_grade", "category": "grade", "column": "B.EMART_GRADE_CD", "value": "MEM_GRADE_CD.FAMILY"},
        {"canonical": "silver_grade", "category": "grade", "column": "B.EMART_GRADE_CD", "value": "MEM_GRADE_CD.SILVER"},
        {"canonical": "gold_grade", "category": "grade", "column": "B.EMART_GRADE_CD", "value": "MEM_GRADE_CD.GOLD"},
        {"canonical": "vip", "category": "grade", "column": "B.EMART_GRADE_CD", "value": "MEM_GRADE_CD.VIP"},
        {"canonical": "dormant", "category": "state", "column": "B.MEMBER_STATE_CD", "value": "MEMBER_STATE_CD.SLEEP"},
        {"canonical": "app_user", "category": "channel", "column": "B.LAST_LOGIN_CHANNEL", "value": "DEVICE_TYPE_CD.APP"},
    ],
    "activity_filters": [
        {"canonical": "inactive_90d", "days": 90},
        {"canonical": "inactive_180d", "days": 180},
    ],
    "lifecycle_extra_terms": ["new_user"],
    "active_state": {"column": "MEMBER_STATE_CD", "value": "MEMBER_STATE_CD.NORMAL"},
    "birthday_target": {"column": "BIRTHDAY"},
    "signup_target": {"column": "REG_DT", "table": "CRM_MB_BASEINFO", "default_days": 90, "anchor": "data_max"},
    "order_count_targets": {
        "table": "CRM_SL_ORDERHEADERMALL",
        "join_column": "MEMBER_NO",
        "order_id_column": "ORDER_ID",
        "order_date_column": "ORDER_DATE",
        "behaviors": {
            "first_purchase": {"operator": "=", "count": 1},
            "repeat_buyer": {"operator": ">=", "count": 2},
            "no_purchase": {"anti_join": True},
        },
    },
    # 범용 집계 조건 타겟: 주문 테이블을 회원별로 집계해 '<지표> <임계값> 이상/이하' 세그먼트를 뽑는다.
    # 새 지표는 metrics 에 항목 하나 추가로 끝난다(agg/column/동의어만 지정 — 빌더/파서 코드 수정 없음).
    "aggregate_targets": {
        "table": "CRM_SL_ORDERHEADERMALL",
        "join_column": "MEMBER_NO",
        "date_column": "ORDER_DATE",
        "metrics": {
            "purchase_amount": {
                "agg": "SUM",
                "column": "PAYMENT_AMT",
                "ko_label": "누적 구매 금액",
                "synonyms": ["누적 구매 금액", "누적구매금액", "구매 금액", "구매금액", "결제 금액", "결제금액", "구매액", "구매 총액", "구매총액", "구매 총금액"],
            },
            "order_count": {
                "agg": "COUNT",
                "column": "ORDER_ID",
                "distinct": True,
                "ko_label": "구매 횟수",
                "synonyms": ["구매 횟수", "구매횟수", "주문 횟수", "주문횟수", "구매 건수", "구매건수", "주문 건수", "주문건수"],
            },
        },
    },
    "purchase_product_match_columns": [
        "CATEGORY", "CATEGORYL_NAME", "CATEGORYM_NAME", "CATEGORYS_NAME", "BRAND_NAME", "PRODUCT_NAME",
    ],
    "supported_condition_hint": "성별·연령·회원등급·휴면/미접속 기간·상품 구매 이력",
    "region_density": {
        "granularity_tokens": ["동네", "지역", "시군구", "시도", "구"],
        "granularity_columns": {"시도": "SIDO"},
        "default_column": "SIGUNGU",
        "default_top_n": 5,
        "max_top_n": 30,
    },
    "member_metric_ranking": {
        "granularity_tokens": ["고객님", "구매자", "사용자", "고객", "회원", "유저", "손님"],
        "default_top_n": 100,
        "max_top_n": 10000,
    },
}


def _load_member_target_filters(path: Path = DEFAULT_MEMBER_TARGET_FILTERS_PATH) -> dict[str, Any]:
    """레지스트리 JSON 을 읽어 코드 기본값 위에 키 단위로 덮는다. 파일 부재/파손 시 기본값 그대로."""
    merged = dict(_DEFAULT_MEMBER_TARGET_FILTERS)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return merged
    if isinstance(payload, dict):
        for key in _DEFAULT_MEMBER_TARGET_FILTERS:
            if key in payload:
                merged[key] = payload[key]
    return merged


def _parse_eq_filters(entries: Any) -> dict[str, tuple[str, str, str]]:
    if not isinstance(entries, list):
        return {}
    return {
        entry["canonical"]: (entry["category"], entry["column"], entry["value"])
        for entry in entries
        if isinstance(entry, dict)
        and all(isinstance(entry.get(key), str) and entry.get(key) for key in ("canonical", "category", "column", "value"))
    }


def _parse_activity_filters(entries: Any) -> dict[str, int]:
    if not isinstance(entries, list):
        return {}
    return {
        entry["canonical"]: entry["days"]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("canonical"), str)
        and entry.get("canonical")
        and isinstance(entry.get("days"), int)
        and entry["days"] > 0
    }


_MEMBER_TARGET_FILTERS = _load_member_target_filters()
# 파일 항목이 전부 비정형이어도 규칙 엔진이 죽지 않게 빈 결과는 코드 기본값으로 복원한다.
MEMBER_EQ_FILTERS: dict[str, tuple[str, str, str]] = (
    _parse_eq_filters(_MEMBER_TARGET_FILTERS.get("eq_filters"))
    or _parse_eq_filters(_DEFAULT_MEMBER_TARGET_FILTERS["eq_filters"])
)
MEMBER_ACTIVITY_FILTERS: dict[str, int] = (
    _parse_activity_filters(_MEMBER_TARGET_FILTERS.get("activity_filters"))
    or _parse_activity_filters(_DEFAULT_MEMBER_TARGET_FILTERS["activity_filters"])
)
# 파서 어휘(성별/생애주기)는 레지스트리에서 파생한다 — 레지스트리에 항목을 추가하면 별도의
# 어휘 셋 수정 없이 plan 병합(_merge_scalar/_merge_list)과 술어 컴파일이 함께 열린다.
GENDER_TERMS = {canonical for canonical, (category, _, _) in MEMBER_EQ_FILTERS.items() if category == "gender"}
LIFECYCLE_TERMS = (
    {canonical for canonical, (category, _, _) in MEMBER_EQ_FILTERS.items() if category != "gender"}
    | set(MEMBER_ACTIVITY_FILTERS)
    | {term for term in _MEMBER_TARGET_FILTERS.get("lifecycle_extra_terms", []) if isinstance(term, str) and term}
)


def _member_eq_predicate(canonical: str, negate: bool = False) -> str | None:
    entry = MEMBER_EQ_FILTERS.get(canonical)
    if entry is None:
        return None
    _, column, value = entry
    return column + (" <> " if negate else " = ") + _sql_quote(value)


def _member_activity_predicate(days: int) -> str:
    return f"(B.LAST_LOGIN_DATE IS NOT NULL AND B.LAST_LOGIN_DATE <= CONVERT(CHAR(8), DATEADD(DAY, -{days}, GETDATE()), 112))"


def _member_active_state_predicate(alias: str = "B") -> str:
    """정상 회원 한정(탈퇴/휴면 제외) 술어. 기준 컬럼/값은 member_target_filters.json 의 active_state."""
    state = _MEMBER_TARGET_FILTERS.get("active_state")
    if not isinstance(state, dict):
        state = _DEFAULT_MEMBER_TARGET_FILTERS["active_state"]
    column = state.get("column") or "MEMBER_STATE_CD"
    value = state.get("value") or "MEMBER_STATE_CD.NORMAL"
    return f"{alias}.{column} = " + _sql_quote(value)


def _member_birthday_predicate(granularity: str = "day", alias: str = "B") -> str:
    """생일 타겟 술어. BIRTHDAY 는 nvarchar(8) 'YYYYMMDD' 문자열이라 년도까지 비교하면 안 되고,
    월일(MMDD)만 오늘과 비교한다(day). '이달 생일'은 월(MM)만 비교(month). 컬럼은 birthday_target 설정."""
    config = _MEMBER_TARGET_FILTERS.get("birthday_target")
    if not isinstance(config, dict):
        config = _DEFAULT_MEMBER_TARGET_FILTERS["birthday_target"]
    column = config.get("column") or "BIRTHDAY"
    length = 2 if granularity == "month" else 4  # month: MM(2자리), day: MMDD(4자리)
    col = f"{alias}.{column}"
    today = "CONVERT(CHAR(8), GETDATE(), 112)"  # 'YYYYMMDD'
    # LEN 가드로 8자리 정상값만 비교(널/이상치 제외).
    return (
        f"({col} IS NOT NULL AND LEN({col}) = 8 "
        f"AND SUBSTRING({col}, 5, {length}) = SUBSTRING({today}, 5, {length}))"
    )


def _member_signup_predicate(days: int | None = None, alias: str = "B") -> str:
    """신규 가입 타겟 술어. REG_DT(nvarchar(8) 'YYYYMMDD') 가 기준일로부터 최근 N일 이내인 회원.

    기준일(anchor)은 signup_target.anchor 설정: 'getdate' 는 실제 오늘(운영 정합), 'data_max' 는
    적재 데이터 최신일 MAX(REG_DT)(데모 데이터가 과거라 GETDATE 기준이면 0명이 되는 문제 회피).
    REG_DT 는 문자열이라 날짜연산 전 CONVERT(DATE, ., 112)로 파싱하고, 경계값은 다시 CHAR(8) 로 바꿔
    사전식(문자열) 비교한다(포맷이 고정 8자리라 문자열 대소 = 날짜 대소). LEN 가드로 이상치를 제외한다."""
    config = _MEMBER_TARGET_FILTERS.get("signup_target")
    if not isinstance(config, dict):
        config = _DEFAULT_MEMBER_TARGET_FILTERS["signup_target"]
    column = config.get("column") or "REG_DT"
    table = config.get("table") or "CRM_MB_BASEINFO"
    if not isinstance(days, int) or days <= 0:
        default_days = config.get("default_days")
        days = default_days if isinstance(default_days, int) and default_days > 0 else 90
    col = f"{alias}.{column}"
    if config.get("anchor") == "getdate":
        anchor = "GETDATE()"
    else:
        # 적재 데이터 최신 가입일 기준(서브쿼리). MAX 는 널/공백을 무시하고, 포맷 고정이라 문자열 MAX = 최신일.
        anchor = f"CONVERT(DATE, (SELECT MAX({column}) FROM {table} WHERE LEN({column}) = 8), 112)"
    boundary = f"CONVERT(CHAR(8), DATEADD(DAY, -{days}, {anchor}), 112)"
    return f"({col} IS NOT NULL AND LEN({col}) = 8 AND {col} >= {boundary})"


# ── 타겟팅 신호어 사전(intent/objective/문맥) ─────────────────────────────────
# 의도·목적 분류와 문맥 판정(판매 아웃리치/신제품 알림/재활성/장바구니 이탈 등)에 쓰는 표현형의
# 단일 출처는 docs/data/targeting_lexicon.json 이다. 새 표현("리텐션 캠페인", 새 판매 동사 등)은
# 코드 수정 없이 그 파일에 추가한다. 아래 기본값은 파일 부재/파손 시 폴백이자 스키마 예시다.
# objective_rules 는 순서가 의미(먼저 걸린 목적 승리)라 리스트로 유지한다.
DEFAULT_TARGETING_LEXICON_PATH = Path(
    os.getenv("GRAPH_RAG_TARGETING_LEXICON", "docs/data/targeting_lexicon.json")
)

_DEFAULT_TARGETING_LEXICON: dict[str, Any] = {
    # 대상 지향 표지: 이 뒤부터는 "누구에게 무엇을 한다"의 캠페인/채널·메시지 절로 본다.
    "audience_direction_markers": ["에게", "한테", "께", "대상으로", "타겟으로", "타깃으로"],
    # 채널/메시지 의도 신호. 규칙 분리 실패(표지 없음) 판정과 LLM 폴백 트리거에 쓴다.
    "channel_signal_words": [
        "홍보", "광고", "알림", "알리", "안내", "소식", "공지", "캠페인",
        "메시지", "발송", "보내", "판매", "팔", "프로모션", "쿠폰", "이벤트",
    ],
    "intent_recommend_campaign": ["캠페인", "추천", "recommend", "campaign"],
    "intent_find_user_segment": ["사용자", "고객", "사람", "지역", "세그먼트", "user", "segment", "region"],
    "objective_rules": [
        {"objective": "purchase", "keywords": ["구매", "구입", "전환", "매출", "purchase", "conversion", "판매", "팔고", "팔려", "sell"]},
        {"objective": "subscription", "keywords": ["구독", "subscription"]},
        {"objective": "reactivation", "keywords": ["휴면", "복귀", "재방문", "reactivation"]},
        {"objective": "retention", "keywords": ["retention"]},
        {"objective": "awareness", "keywords": ["신제품", "신상품", "출시", "런칭", "awareness", "launch"]},
    ],
    "awareness_launch_terms": ["신제품", "신상품", "출시", "런칭", "launch", "awareness"],
    "awareness_announce_terms": ["알리", "알림", "소식", "안내", "홍보"],
    # "팔레트/팔로우" 등 오탐을 피하려고 "팔" 단독이 아닌 "팔고/팔려/판매"만 판매 동사로 본다.
    "sell_outreach_verbs": ["팔고", "팔려", "팔것", "판매", "sell"],
    "sell_outreach_audience": ["에게", "한테", "고객", "대상", "타겟", "타깃"],
    "reactivation_goal_terms": ["재활성", "다시활성", "활성화", "휴면복귀", "복귀캠페인", "reactivation", "reactivate"],
    "cart_terms": ["장바구니"],
    "cart_abandonment_terms": ["결제하지않", "결제안", "미결제", "구매하지않", "구매안", "안산", "방치", "이탈", "cartabandon"],
    "repurchase_terms": ["재구매", "repurchase"],
    "repurchase_outreach_terms": ["유도", "촉진", "리마인드", "캠페인", "메시지", "발송", "추천"],
    "purchase_history_signals": ["구매", "구입", "샀", "purchased", "bought"],
}


@functools.lru_cache(maxsize=4)
def _load_targeting_lexicon(path_text: str) -> dict[str, Any] | None:
    path = Path(path_text)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _lexicon_terms(group: str) -> tuple[str, ...]:
    """사전 파일의 그룹 표현형 목록. 그룹이 없거나 비정형이면 코드 기본값으로 폴백한다."""
    lexicon = _load_targeting_lexicon(str(DEFAULT_TARGETING_LEXICON_PATH)) or {}
    values = lexicon.get(group)
    if isinstance(values, list):
        terms = tuple(value for value in values if isinstance(value, str) and value)
        if terms:
            return terms
    return tuple(_DEFAULT_TARGETING_LEXICON[group])


def _lexicon_objective_rules() -> list[tuple[str, tuple[str, ...]]]:
    """(objective, keywords) 목록을 파일 순서대로 반환한다. objective 는 허용 목록만 통과시킨다."""
    lexicon = _load_targeting_lexicon(str(DEFAULT_TARGETING_LEXICON_PATH)) or {}
    raw_rules = lexicon.get("objective_rules")
    if not isinstance(raw_rules, list):
        raw_rules = _DEFAULT_TARGETING_LEXICON["objective_rules"]
    rules: list[tuple[str, tuple[str, ...]]] = []
    for rule in raw_rules:
        if not isinstance(rule, dict):
            continue
        objective = rule.get("objective")
        keywords = tuple(k for k in rule.get("keywords", []) if isinstance(k, str) and k)
        if objective in CAMPAIGN_OBJECTIVES and keywords:
            rules.append((objective, keywords))
    if not rules:
        rules = [
            (rule["objective"], tuple(rule["keywords"]))
            for rule in _DEFAULT_TARGETING_LEXICON["objective_rules"]
        ]
    return rules


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

    return graph


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


def _prompt_rewrite_system_prompt(prompt_dir: Path | None = DEFAULT_PROMPT_DIR) -> str:
    fallback = "\n".join(
        [
            "너는 캠페인 타겟팅 프롬프트 재작성기다.",
            "rewritten_prompt: 파싱·SQL 생성용 전체 재작성(타겟 조건 + 캠페인 목적/혜택을 표준 용어로 정리).",
            "targeting_label: 화면 표시용으로, 오디언스(누구를 타겟하는가)만 담은 아주 간결한 라벨.",
            "원문에 있는 조건만 사용한다(없는 조건·수치·세그먼트·혜택을 추가/삭제/재해석하지 말 것).",
            "구어체·오타·모호한 표현만 표준 타겟 용어로 정리한다(예: 2030 -> 20~30대).",
            "targeting_label 에서는 이 캠페인이 보내거나 파는 상품·혜택(쿠폰/할인 등), 행동 표현(보내다/뿌리다/판매/만들다), 단어 '캠페인', 발송 채널을 뺀다.",
            "단, 상품 '구매/구입 이력'은 오디언스 조건이므로 targeting_label 에 유지한다(예: '기저귀 구매 고객').",
            "오디언스 조건이 없으면 targeting_label 은 빈 문자열로 둔다.",
            '다음 JSON object 만 출력한다: {"rewritten_prompt": "재작성된 타겟팅 프롬프트", "targeting_label": "오디언스만 담은 라벨 또는 빈 문자열", "summary": "한 줄 요약", "changes": ["원문표현 -> 재작성표현", ...]}.',
        ]
    )
    return _read_prompt_template(prompt_dir, "prompt_rewrite_system.txt", fallback)


# '발송 채널: ...' 지시를 타겟팅 본문과 분리한다. BFF 가 프롬프트 끝에 붙이는 채널 절은
# 재작성 대상에서 제외하고 원문 그대로 보존해야 effective_query 의 발송 채널 스코프가 유지된다.
_CHANNEL_SUFFIX_PATTERN = re.compile(r"\n?\s*발송\s*채널\s*:.*$", flags=re.DOTALL)


def _split_channel_suffix(text: str) -> tuple[str, str]:
    """(타겟팅 본문, 채널 접미어)로 분리한다. 접미어가 없으면 두 번째 값은 빈 문자열."""
    match = _CHANNEL_SUFFIX_PATTERN.search(text)
    if not match:
        return text, ""
    return text[: match.start()].rstrip(), text[match.start() :]


def _prompt_rewrite_timeout_seconds(default: float = 12.0) -> float:
    raw = os.getenv("PROMPT_REWRITE_TIMEOUT_SECONDS")
    if not raw:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# 재작성 검증 게이트에서 지키는 성별 표면형(→ canonical). 이 표현이 원문에 있었는데 재작성본에서
# 사라지면 조건이 소실된 것으로 본다. 부정문("~가 아닌")까지 정밀 구분하진 않는다 — 게이트가
# 오탐해도 결과는 '재작성 미적용(원문 사용)'뿐이라 안전하기 때문이다.
_GENDER_SURFACE_TO_CANONICAL = {"여성": "female", "여자": "female", "남성": "male", "남자": "male"}
_GENDER_CANONICAL_KO = {"female": "여성", "male": "남성"}
# 상품 구매 이력 조건("… 구매/구입한 …")의 상품명 추출 패턴. _apply_purchase_object_filter 와 공유해
# 재작성 검증 게이트에서도 같은 기준으로 '구매 상품 조건'이 지워졌는지 본다.
_PURCHASE_OBJECT_PATTERN = re.compile(
    r"(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})\s*(?:을|를)?\s*"
    r"(?:구매|구입)\s*(?:한|했|했던|하신|하였|이력|내역|경험|고객|회원|유저|구매자)",
    re.IGNORECASE,
)


# 구매 '횟수/조건' 수식어는 상품명이 아니다(예: '2회 이상 구매' 의 '이상'). 게이트가 상품 조건으로 오인해
# 재작성을 헛되이 폐기하지 않도록 제외한다.
_PURCHASE_SIGNAL_STOPWORDS = {"이상", "이하", "미만", "초과", "회", "번", "건", "원", "개", "명", "이력", "내역", "경험", "동안", "번째"}


def _purchase_object_signals(text: str) -> set[str]:
    """텍스트에서 상품 구매 이력 조건의 상품명(canonical 소문자) 집합을 뽑는다(게이트 비교용)."""
    objects: set[str] = set()
    for match in _PURCHASE_OBJECT_PATTERN.finditer(text or ""):
        purchase_object = _sanitize_purchase_object(match.group("object"))
        if purchase_object and purchase_object not in _PURCHASE_SIGNAL_STOPWORDS and not purchase_object.isdigit():
            objects.add(purchase_object.casefold())
    return objects


def _rewrite_guard_enabled() -> bool:
    """재작성 검증 게이트 on/off(환경변수 PROMPT_REWRITE_GUARD, 기본 on)."""
    return os.getenv("PROMPT_REWRITE_GUARD", "true").casefold() not in {"0", "false", "off", "no"}


def _prompt_signal_signature(text: str) -> dict[str, set[str]]:
    """재작성 전후 비교용 '핵심 신호' 서명.

    재작성은 표현(구어체·오타)만 다듬어야 하므로 아래 리터럴 신호는 반드시 보존돼야 한다:
      - numbers: 연령·일수·횟수·금액 등 숫자(천단위 콤마는 제거 후 추출)
      - genders: 성별 표면형에서 해석한 canonical(female/male)
      - purchases: 상품 구매 이력 조건의 상품명(예: '화장품 구매' → 화장품)
    """
    compact = text or ""
    # "30,000" 같은 천단위 콤마는 하나의 숫자로 보도록 자릿수 사이 콤마만 제거한다.
    digits_only = re.sub(r"(?<=\d),(?=\d)", "", compact)
    numbers = set(re.findall(r"\d+", digits_only))
    genders = {canonical for surface, canonical in _GENDER_SURFACE_TO_CANONICAL.items() if surface in compact}
    return {"numbers": numbers, "genders": genders, "purchases": _purchase_object_signals(compact)}


def _rewrite_dropped_signals(original: str, rewritten: str) -> list[str]:
    """원문 대비 재작성본에서 사라진 핵심 신호를 사람이 읽는 목록으로 돌려준다.

    빈 목록이면 소실 없음(재작성 채택 가능). 하나라도 있으면 재작성이 조건을 지운 것이므로
    호출부는 재작성을 폐기하고 원문 기준으로 되돌린다.
    """
    before = _prompt_signal_signature(original)
    after = _prompt_signal_signature(rewritten)
    dropped: list[str] = []
    for number in sorted(before["numbers"] - after["numbers"]):
        dropped.append(f"숫자 '{number}'")
    for gender in sorted(before["genders"] - after["genders"]):
        dropped.append(f"성별 '{_GENDER_CANONICAL_KO.get(gender, gender)}'")
    # 구매 상품은 재작성이 구매 표현형을 바꿔도(예: '구매한'→'구매') 상품명 자체가 남아있으면 보존으로 본다.
    # 그래서 엄격 패턴 재추출이 아니라 상품명이 재작성본 어디에도 없을 때만 소실로 판정한다(오탐 방지).
    after_compact = (rewritten or "").casefold()
    for purchase in sorted(before["purchases"]):
        if purchase not in after_compact:
            dropped.append(f"구매 상품 '{purchase}'")
    return dropped


def normalize_prompt(
    query: str,
    parser: str = "rules",
    llm_model: str = DEFAULT_LLM_MODEL,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
    style: str | None = None,
) -> dict[str, Any]:
    """다운스트림 파싱 전에 사용자 프롬프트를 타겟 조건 중심으로 정리/재작성한다.

    style="targeting"(기본): LLM 이 구어체·오타·모호한 표현을 표준 타겟 용어로 재작성한다. 원문의
      타겟 조건은 추가·삭제 없이 보존하고, BFF 가 붙인 "발송 채널: ..." 지시는 원문 그대로 유지한다.
      재작성 결과(effective_query)가 실제 타겟 SQL·세그먼트 생성의 기준이 된다.
    style="conservative": 오타/띄어쓰기만 보수적으로 교정한다(기존 동작).
    style="off"/"none"/"rules" 또는 OPENAI_API_KEY 미설정/호출 실패 시 공백만 정리하는 규칙
      fallback 을 쓴다. 원문(original)은 항상 보존해 감사·표시에 사용한다.
    재작성은 query_parser 와 무관하게 OPENAI_API_KEY 유무로 동작한다(전처리 단계이므로 분리).
    반환: {original, normalized, summary, corrections, mode}.
    """
    original = query if isinstance(query, str) else ""
    rule_cleaned = re.sub(r"\s+", " ", original).strip()
    fallback = {
        "original": original,
        "normalized": rule_cleaned or original,
        "summary": "",
        "corrections": [],
        "targeting_label": "",
        "mode": "rules",
    }
    resolved_style = (style or os.getenv("PROMPT_REWRITE_STYLE", "targeting")).casefold()
    # 재작성 비활성(off/none/rules)이거나 LLM 사용 불가하면 공백 정리만 한다(원문 의미는 그대로).
    if resolved_style in {"off", "none", "rules"} or not os.getenv("OPENAI_API_KEY") or not rule_cleaned:
        return fallback
    try:
        from openai import OpenAI
    except ImportError:
        return fallback

    conservative = resolved_style == "conservative"
    # 재작성은 타겟팅 본문에만 적용하고, "발송 채널: ..." 지시는 분리해 원문 그대로 다시 붙인다.
    targeting_part, channel_suffix = _split_channel_suffix(original)
    llm_input = original if conservative else targeting_part
    if not llm_input.strip():
        return fallback
    try:
        client = OpenAI()
        system_prompt = (
            _prompt_normalize_system_prompt(prompt_dir)
            if conservative
            else _prompt_rewrite_system_prompt(prompt_dir)
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": llm_input},
        ]
        response = client.chat.completions.create(
            model=llm_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=messages,
            timeout=_prompt_rewrite_timeout_seconds(),
        )
        data = json.loads(response.choices[0].message.content or "{}")
        rewritten = data.get("normalized_prompt") if conservative else data.get("rewritten_prompt")
        if not isinstance(rewritten, str) or not rewritten.strip():
            return fallback
        rewritten = rewritten.strip()
        # 검증 게이트: 재작성이 원문의 핵심 타겟 신호(숫자·성별)를 조용히 지웠는지 확인한다.
        # 하나라도 사라졌으면 재작성을 폐기하고 규칙 정리본(fallback)으로 되돌린다. 폴백은 항상
        # 원문 의미를 보존하므로 오탐이 있어도 손해는 '재작성 미적용'뿐이다. llm_input(채널 접미어
        # 제외한 재작성 대상)과 rewritten(접미어 재부착 전)을 같은 기준으로 비교한다.
        if _rewrite_guard_enabled():
            dropped = _rewrite_dropped_signals(llm_input, rewritten)
            if dropped:
                guarded = {**fallback, "mode": "rules_guarded", "guard_dropped": dropped}
                _write_rag_llm_log("prompt_normalization", guarded)
                return guarded
        changes_key = "corrections" if conservative else "changes"
        corrections = (
            [item for item in data.get(changes_key, []) if isinstance(item, str) and item.strip()]
            if isinstance(data.get(changes_key), list)
            else []
        )
        summary = data.get("summary").strip() if isinstance(data.get("summary"), str) else ""
        # targeting_label: 화면 표시용 오디언스-only 라벨(재작성 모드에서만). effective_query(전체 재작성)와
        # 분리된 필드라 SQL/intent 파싱에는 영향을 주지 않는다. 값이 비면 BFF 가 normalized 로 폴백한다.
        targeting_label = data.get("targeting_label") if not conservative else None
        targeting_label = targeting_label.strip() if isinstance(targeting_label, str) else ""
        # targeting_label 도 오디언스 조건을 조용히 지울 수 있다(예: '최근 화장품을 구매한' 누락). 원문 대비
        # 핵심 신호(숫자·성별·구매 상품)가 사라졌으면 라벨을 비워 BFF 가 normalized(검증된 전체 재작성)로
        # 폴백하게 한다 — 틀린 라벨보다 조건이 다 보이는 라벨이 낫다.
        if targeting_label and _rewrite_guard_enabled() and _rewrite_dropped_signals(llm_input, targeting_label):
            targeting_label = ""
        # 채널 지시를 다시 붙여 effective_query 가 발송 채널 스코프 분리를 유지하게 한다.
        normalized_full = rewritten if conservative else (rewritten + channel_suffix)
        result = {
            "original": original,
            "normalized": normalized_full,
            "summary": summary,
            "corrections": corrections,
            "targeting_label": targeting_label,
            "mode": "llm" if conservative else "llm_rewrite",
        }
        _write_rag_llm_log("prompt_normalization", result)
        return result
    except Exception as exc:
        # 재작성 실패는 치명적이지 않다(원문/규칙 정리본으로 계속 진행).
        return {**fallback, "mode": "rules_fallback", "error": exc.__class__.__name__}


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
    pattern = r"(?P<targeting>.*?(?:%s))\s*(?P<channel>.*)$" % "|".join(
        re.escape(marker) for marker in _lexicon_terms("audience_direction_markers")
    )
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
    return any(word in compact for word in _lexicon_terms("channel_signal_words"))


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
    # BFF 가 붙이는 구조적 "발송 채널: <채널> (설명)" 절은 오디언스 표지·파서와 무관하게 항상 채널
    # 스코프로 떼어낸다. 이 절은 발송 채널일 뿐 타겟 조건이 아니므로 타겟팅 RAG 검색에서 제외해야 한다.
    base, channel_suffix = _split_channel_suffix(original)
    channel_suffix = channel_suffix.strip()
    base = base if channel_suffix else original

    def _with_channel_suffix(channel: str) -> str:
        parts = [part for part in (channel.strip(), channel_suffix) if part]
        return " ".join(parts).strip()

    rule = _rule_split_prompt_scopes(base)
    # 규칙으로 채널 절을 얻었거나, 애초에 채널 신호가 없어 전부 타겟팅이면 규칙 결과를 그대로 쓴다.
    if rule is not None and (rule[1] or not _has_channel_signal(base)):
        return {"targeting": rule[0], "channel": _with_channel_suffix(rule[1]), "mode": "rules"}
    # 규칙이 제대로 못 나눴고(표지 없음/채널 절 공백) 채널 신호가 있으면 LLM 의미 분리 시도.
    llm = _llm_split_prompt_scopes(base, parser, llm_model, prompt_dir)
    if llm is not None:
        return {"targeting": llm["targeting"], "channel": _with_channel_suffix(llm.get("channel", "")), "mode": "llm"}
    if rule is not None:
        return {"targeting": rule[0], "channel": _with_channel_suffix(rule[1]), "mode": "rules"}
    # 최종 폴백: 나머지는 전부 타겟팅. 채널 접미어를 뗐다면 그 절만 채널로 남아 오염이 사라진다.
    return {"targeting": base, "channel": channel_suffix, "mode": "rules" if channel_suffix else "rules_fallback"}


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

    # canonical(female/repeat_buyer 등)은 영문이라 한글 절에 안 나타나 스코프를 직접 못 가린다.
    # 대신 그 canonical 을 만든 원문 표현(matched_text)이 어느 절에 있는지로 판정한다. 예) "재구매를"이
    # 캠페인 목표(채널) 절에 있으면 repeat_buyer 는 타겟팅이 아니라 채널로 간다 → 타겟팅 검색 오염 방지.
    canonical_source: dict[str, str] = {}
    for match in plan.get("matched_terms", []):
        canonical = match.get("canonical")
        matched_text = match.get("matched_text")
        if isinstance(canonical, str) and isinstance(matched_text, str):
            canonical_source.setdefault(canonical, matched_text)

    def _scope_of(term: str) -> str:
        if term in channel_canonicals:
            return "channel"
        source = canonical_source.get(term)
        if source is not None:
            src = source.replace(" ", "").casefold()
            if src and src in targeting_compact:
                return "targeting"
            if src and src in channel_compact:
                return "channel"
        lowered = term.casefold()
        if lowered in channel_compact and lowered not in targeting_compact:
            return "channel"
        return "targeting"

    targeting_terms: list[str] = []
    channel_terms: list[str] = []
    for term in retrieval["terms"]:
        (channel_terms if _scope_of(term) == "channel" else targeting_terms).append(term)

    # 채널로 간 canonical 의 파편 토큰(예: repeat_buyer -> "repeat","buyer를")이 전체 정규화문 토큰화에서
    # 타겟팅으로 새는 걸 막는다. 채널 canonical 을 "_"로 쪼갠 조각으로 시작하는 타겟팅 토큰은 버린다.
    channel_fragments = {
        piece for term in channel_terms if "_" in term for piece in term.casefold().split("_") if piece
    }
    if channel_fragments:
        targeting_terms = [
            term
            for term in targeting_terms
            if not any(term.casefold().startswith(fragment) for fragment in channel_fragments)
        ]

    plan["retrieval"]["scope_mode"] = scopes.get("mode", "rules")
    plan["retrieval"]["targeting_query"] = targeting_text or plan["retrieval"]["query"]
    plan["retrieval"]["channel_query"] = channel_text
    plan["retrieval"]["targeting_terms"] = _unique_strings(targeting_terms)
    plan["retrieval"]["channel_terms"] = _unique_strings(channel_terms)


def _prompt_reformulation_system_prompt(prompt_dir: Path | None = DEFAULT_PROMPT_DIR) -> str:
    fallback = "\n".join(
        [
            "너는 캠페인 타겟팅 프롬프트를 의미를 100% 보존한 채 표현만 바꾼 재구성 문장들을 만드는 도구다.",
            "규칙: 대상 조건(성별/연령/지역/회원등급/구매이력/행동/제외/캠페인 목적/혜택/채널)을 절대",
            "추가·삭제·변경하지 않는다. 같은 뜻을 다른 어순·어휘·조사로 바꾼 한국어 문장만 만든다",
            "(동의어, 명사형↔동사형 전환, 띄어쓰기 변형 허용). 새로운 대상이나 조건을 지어내지 마라.",
            '다음 JSON object 만 출력한다: {"variants": ["재구성1", "재구성2", ...]}.',
        ]
    )
    return _read_prompt_template(prompt_dir, "prompt_reformulation_system.txt", fallback)


def _generate_prompt_reformulations(
    query: str, count: int, parser: str, llm_model: str, prompt_dir: Path | None
) -> list[str]:
    """의미를 보존한 프롬프트 재구성 문장 목록을 LLM 으로 생성한다. 사용 불가/실패 시 빈 목록.

    표현만 바꾼 문장들이라 결정론 파서가 각기 다른 규칙 패턴에 걸려 조건 재현율을 높인다. 원문과
    같거나 중복인 재구성은 제거한다."""
    if count <= 0 or parser.casefold() == "rules" or not os.getenv("OPENAI_API_KEY") or not query.strip():
        return []
    try:
        from openai import OpenAI
    except ImportError:
        return []
    try:
        client = OpenAI()
        response = client.chat.completions.create(
            model=llm_model,
            temperature=0.4,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _prompt_reformulation_system_prompt(prompt_dir)},
                {"role": "user", "content": f"원문: {query}\n서로 다른 표현의 재구성 {count}개를 만들어라."},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        variants = data.get("variants")
        if not isinstance(variants, list):
            return []
        seen = {query.replace(" ", "").casefold()}
        result: list[str] = []
        for variant in variants:
            if not isinstance(variant, str) or not variant.strip():
                continue
            key = variant.replace(" ", "").casefold()
            if key and key not in seen:
                seen.add(key)
                result.append(variant.strip())
        _write_rag_llm_log("prompt_reformulation", {"query": query, "variants": result})
        return result[:count]
    except Exception:
        return []


def _merge_targeting_conditions(base: dict[str, Any], other: dict[str, Any]) -> None:
    """변이 파싱 결과(other)의 타겟 조건을 base 에 합집합으로 병합한다.

    스칼라(성별/연령/구매상품 등)는 base 가 비어 있을 때만 채워 모순을 막고(원문 우선), 리스트
    (생애주기/관심사/행동/제외/디멘션 필터)는 합집합한다. 집합식·계산지표·정책·의미해석은 병합하지
    않는다(고급 조건이라 표현 변이 병합 시 모순 위험). 즉 병합은 조건을 '늘리기만' 한다."""
    base_tu = base.setdefault("target_user", {})
    other_tu = other.get("target_user", {})
    # 성별/가격민감도는 닫힌 값집합이라 병합이 안전하다. 구매상품(purchase_object)/판매상품(sell_object)은
    # 자유 텍스트라 변이 오인식(예: '최초로'를 상품으로 추출)이 섞이면 엉뚱한 상품 LIKE 로 라우팅되므로
    # 병합하지 않는다(원문 base 기준만 사용, 상품 표현형은 이미 _apply_llm_object_fallback 이 보완).
    for field in ("gender", "price_sensitivity"):
        if not base_tu.get(field) and other_tu.get(field):
            base_tu[field] = other_tu[field]
    for field in ("age_min", "age_max"):
        if base_tu.get(field) is None and other_tu.get(field) is not None:
            base_tu[field] = other_tu[field]
    if not base_tu.get("inactivity_period") and other_tu.get("inactivity_period"):
        base_tu["inactivity_period"] = other_tu["inactivity_period"]
    for field in ("lifecycle", "interests", "preferred_channels", "behaviors"):
        merged = _unique_strings([*base_tu.get(field, []), *other_tu.get(field, [])])
        if merged:
            base_tu[field] = merged

    base_exclude = base.setdefault("exclude", {})
    other_exclude = other.get("exclude", {})
    for field in ("gender", "interests", "lifecycle"):
        merged = _unique_strings([*base_exclude.get(field, []), *other_exclude.get(field, [])])
        if merged:
            base_exclude[field] = merged

    base_campaign = base.setdefault("campaign_constraints", {})
    other_campaign = other.get("campaign_constraints", {})
    for field in ("objective", "offer_type"):  # sell_object 는 자유 텍스트라 병합 제외(위 purchase_object 와 동일 이유)
        if not base_campaign.get(field) and other_campaign.get(field):
            base_campaign[field] = other_campaign[field]
    for field in ("category", "channels"):
        merged = _unique_strings([*base_campaign.get(field, []), *other_campaign.get(field, [])])
        if merged:
            base_campaign[field] = merged

    # 디멘션 필터(지역/브랜드 등): (컬럼, 코드집합) 기준 중복 제거 합집합.
    existing = {(f.get("column"), tuple(f.get("codes", []))) for f in base.get("dimension_filters", [])}
    for dimension_filter in other.get("dimension_filters", []):
        key = (dimension_filter.get("column"), tuple(dimension_filter.get("codes", [])))
        if key not in existing:
            existing.add(key)
            base.setdefault("dimension_filters", []).append(dimension_filter)

    if not isinstance(base.get("region_density_target"), dict) and isinstance(other.get("region_density_target"), dict):
        base["region_density_target"] = other["region_density_target"]
    if not isinstance(base.get("member_metric_ranking"), dict) and isinstance(other.get("member_metric_ranking"), dict):
        base["member_metric_ranking"] = other["member_metric_ranking"]
    if not base.get("cart_context") and other.get("cart_context"):
        base["cart_context"] = True


def build_query_plan(
    query: str,
    normalization_rules: Path | None = DEFAULT_NORMALIZATION_PATH,
    business_policies: Path | None = DEFAULT_POLICY_PATH,
    metric_lexicon: Path = DEFAULT_METRIC_LEXICON_PATH,
    sql_schema: Path = DEFAULT_SCHEMA_PATH,
    parser: str = "rules",
    llm_model: str = DEFAULT_LLM_MODEL,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
    multi_query_variants: int = 0,
) -> dict[str, Any]:
    """단일 파싱으로 query_plan 을 만든다. multi_query_variants>0 이고 LLM 사용 가능하면 프롬프트를
    의미보존 재구성한 변이들도 파싱해 '성공적으로 잡힌 타겟 조건'을 base 에 합집합으로 병합한다.

    한 표현형이 조건을 놓쳐(파서 미스) 후보가 아예 안 생기던 케이스를, 다른 표현형의 파싱으로 살린다.
    변이는 값이 아니라 표현만 바꾸므로(결정론 파서가 실제 조건 추출) 없는 조건을 지어내지 않는다.
    변이 파싱은 rules(결정론)로 하여 비용을 낮춘다 — 다양한 표현형이 서로 다른 규칙 패턴에 걸리는 것이 핵심.
    """
    base = _build_single_query_plan(
        query, normalization_rules, business_policies, metric_lexicon, sql_schema, parser, llm_model, prompt_dir
    )
    if multi_query_variants and multi_query_variants > 0 and parser.casefold() != "rules":
        variant_intents: list[str] = []
        for variant in _generate_prompt_reformulations(query, multi_query_variants, parser, llm_model, prompt_dir):
            variant_plan = _build_single_query_plan(
                variant, normalization_rules, business_policies, metric_lexicon, sql_schema, "rules", llm_model, prompt_dir
            )
            _merge_targeting_conditions(base, variant_plan)
            variant_intents.append(variant_plan.get("intent"))
        _upgrade_intent_from_variants(base, variant_intents)
        base.setdefault("parser", {})["multi_query_variants"] = multi_query_variants
    return base


def _upgrade_intent_from_variants(base: dict[str, Any], variant_intents: list[str]) -> None:
    """base intent 가 unknown 일 때만, 변이가 잡은 더 강한 intent 로 승격한다(안나옴 방지).

    recommend_campaign(발송/메시지 목적) > find_user_segment(조회) 순. 원래 조회/캠페인으로 잡힌
    intent 는 변이 표현으로 뒤집지 않는다(원문 의도 우선)."""
    if base.get("intent") != "unknown":
        return
    rank = {"recommend_campaign": 2, "find_user_segment": 1}
    best_intent, best_rank = None, 0
    for intent in variant_intents:
        if rank.get(intent, 0) > best_rank:
            best_intent, best_rank = intent, rank[intent]
    if best_intent:
        base["intent"] = best_intent


def _upgrade_intent_from_effective_query(query_plan: dict[str, Any], effective_query: str) -> None:
    """타겟팅 스코프 파싱은 오디언스(타겟팅) 절만 보므로 '재구매를 유도' 같은 캠페인 목적 절이
    plan_query 에서 잘려 intent 가 recommend_campaign→find_user_segment 로 약화될 수 있다
    (예: '장바구니 이탈 고객에게 재구매를 유도' → plan_query='장바구니 이탈 고객에게' → 목적 소실).
    목적 절이 살아있는 전체 재작성본(effective_query)으로 intent 를 재추론해 더 강한 캠페인 의도로만
    승격한다(하향 없음). 승격 순서는 recommend_campaign > find_user_segment > unknown."""
    rank = {"recommend_campaign": 2, "find_user_segment": 1}
    intent_query = _split_channel_suffix(effective_query)[0] or effective_query
    full_intent = _infer_intent(intent_query)
    if rank.get(full_intent, 0) > rank.get(query_plan.get("intent"), 0):
        query_plan["intent"] = full_intent


def _build_single_query_plan(
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

    # "발송 채널: <채널>" 지시는 타겟 조건이 아니라 발송 채널일 뿐이므로, 정규화·검색어 추출 전에 떼어낸다.
    # 남기면 채널 설명("장문 문자" 등)이 정규화 매칭(→lms)과 retrieval terms 로 새어, 타겟팅 키워드 검색이
    # channel_lms 를 끌어온다("(lms," 같은 토큰이 스코프 분류를 우회). 발송 채널은 message_channel 요청
    # 파라미터로 별도 처리되고, 접미어의 채널은 이미 SQL 필터에서도 제외되므로(_is_delivery_channel_context)
    # 파싱에서 빼도 발송 채널 선택에 영향이 없다.
    parse_query = _split_channel_suffix(query)[0] or query

    rules_plan = _build_rule_query_plan(
        parse_query,
        normalization_rules=normalization_rules,
        business_policies=business_policies,
        metric_lexicon=metric_lexicon,
        sql_schema=sql_schema,
    )
    # 정규식이 못 뽑은 상품 구매이력/판매 상품을 검증된 LLM 추출로 보완한다(표현형 변화 흡수).
    # rules_plan 에 반영하면 llm 경로도 _coerce_llm_query_plan 의 깊은 복사로 값을 물려받는다.
    _apply_llm_object_fallback(parse_query, rules_plan, llm_model=llm_model, prompt_dir=prompt_dir)
    if parser == "rules":
        rules_plan["parser"] = {"type": "rules", "fallback_used": False}
        _attach_retrieval_scopes(rules_plan, scopes)
        return rules_plan

    llm_plan, failure_reason = _try_llm_query_plan(parse_query, rules_plan, llm_model, prompt_dir, sql_schema)
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
    _apply_sell_object(parse_query, llm_plan)
    _apply_dimension_filters(parse_query, llm_plan)
    _apply_member_value_filters(parse_query, llm_plan)
    # LLM 이 만든 집합식 operand(지역/등급 디멘션)에도 프롬프트에서 복원한 값을 실어 컴파일되게 한다.
    _enrich_set_expression_operand_values(llm_plan, parse_query)
    # LLM 이 semantic_resolutions 를 자체 추가했을 수 있으므로 밀집 지역 소비를 여기서도 보장한다.
    _apply_region_density_target(parse_query, llm_plan)
    _apply_member_metric_ranking_target(parse_query, llm_plan)
    # 구매 상품(purchase_object)도 프롬프트 텍스트에서 결정론적으로 뽑아, rules/llm 어느 경로든 동일하게
    # 상품 구매 이력 타겟팅(build_purchase_history_targets_sql_candidate)으로 이어지게 한다.
    _apply_purchase_object_filter(parse_query, llm_plan.setdefault("target_user", {}))
    # '최근 N일 구매 안 함'은 결정론 파싱으로 확정한다(LLM 이 no_purchase 로 오분류하는 것 방지).
    llm_plan["target_user"].setdefault("purchase_inactivity", None)
    _apply_purchase_inactivity_filter(parse_query, llm_plan)
    # 범용 집계 조건(누적 구매 금액/횟수 임계값)도 결정론 파싱으로 확정한다(rules/llm 동일 컨텍스트).
    llm_plan["target_user"].setdefault("aggregate_conditions", [])
    _apply_aggregate_condition_filter(parse_query, llm_plan)
    # 생일 타겟도 결정론 파싱으로 확정한다(LLM 이 BIRTHDAY 를 날짜로 캐스팅해 년도까지 비교하는 오류 방지).
    llm_plan["target_user"].setdefault("birthday_target", None)
    _apply_birthday_target_filter(parse_query, llm_plan)
    # 신규 가입 타겟도 결정론 파싱으로 확정한다(창 길이 파싱 담당; LLM 의 new_user 라벨과 이중화).
    llm_plan["target_user"].setdefault("signup_target", None)
    _apply_signup_target_filter(parse_query, llm_plan)
    # 값 보강까지 끝난 뒤, 컴파일되지 않는 리던던트 집합식(잘못 감싼 AND 나열, 지표/디멘션 canonical 오매칭
    # 등)은 버린다 — 결정론 필터가 조건을 커버하므로 SQL 을 막지 않는다(미정규화 값 clarification 은 유지).
    _drop_uncompilable_set_expressions(llm_plan)
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
            "purchase_inactivity": None,
            "birthday_target": None,
            "signup_target": None,
            "aggregate_conditions": [],
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
    _apply_purchase_inactivity_filter(query, plan)
    _apply_birthday_target_filter(query, plan)
    _apply_signup_target_filter(query, plan)
    _apply_sell_object(query, plan)
    _apply_dimension_filters(query, plan)
    _apply_member_value_filters(query, plan)
    _apply_aggregate_condition_filter(query, plan)
    _enrich_set_expression_operand_values(plan, query)
    # 재작성문이 지표/디멘션 canonical(구매금액 등)을 집합식 operand 로 매칭해 컴파일 불가가 되면 SQL 이
    # 막힌다. 결정론 필터가 커버하는 리던던트 집합식은 버린다(age 소유 판정 전에 실행해 age-clear 오작동 방지).
    _drop_uncompilable_set_expressions(plan)
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
    # 지역 모호성 정책(semantic_resolutions)이 채워진 뒤에 실행해야 밀집 지역 해석이 이를 소비한다.
    _apply_region_density_target(query, plan)
    _apply_member_metric_ranking_target(query, plan)
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


# 집합식 AST 노드의 알려진 타입. LLM 이 평범한 AND 조건 나열을 집합식으로 잘못 감싸면서 이 밖의
# 노드 타입(예: 임계값/지표 노드)을 지어내면 컴파일 단계에서 "지원하지 않는 집합식 AST 노드"로 SQL 이
# 통째로 막힌다. 결정론 필터(집계/디멘션/회원)가 이미 조건을 커버하므로, 이런 malformed 집합식은 버린다.
_KNOWN_SET_AST_NODE_TYPES = {"set_op", "age_range", "operand", "unknown_operand"}


def _set_ast_is_structurally_valid(ast: Any) -> bool:
    if not isinstance(ast, dict) or ast.get("type") not in _KNOWN_SET_AST_NODE_TYPES:
        return False
    if ast.get("type") == "set_op":
        if ast.get("op") not in {"+", "*", "-"}:
            return False
        return _set_ast_is_structurally_valid(ast.get("left")) and _set_ast_is_structurally_valid(ast.get("right"))
    return True


def _coerce_llm_set_expression(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, dict) or not isinstance(candidate.get("set_ast"), dict):
        return None
    # 알 수 없는 노드 타입이 섞인 LLM 집합식은 버린다(결정론 필터가 조건 커버; 진짜 집합연산은 rules 파서가
    # 결정론적으로 잡아 fallback 으로 보존됨). unknown_operand(정규화 못한 값)는 정상 clarification 이라 유지.
    if not _set_ast_is_structurally_valid(candidate["set_ast"]):
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
    if any(keyword in compact_query for keyword in _lexicon_terms("intent_recommend_campaign")):
        return "recommend_campaign"
    if any(keyword in compact_query for keyword in _lexicon_terms("intent_find_user_segment")):
        return "find_user_segment"
    return "unknown"


def _has_member_target_signal(query_plan: dict[str, Any]) -> bool:
    """실DB 로 실제 추출 SQL 을 만드는 회원/주문 타겟 신호가 하나라도 있는지 판정한다.

    build_verified_condition_tokens 가 토큰을 만들지 않는 결정론 빌더 신호(생일/신규가입/밀집지역/
    지표랭킹/주문횟수/미구매창/집계조건/구매이력)도 '타겟 조건 있음'으로 인정하기 위한 공통 판정.
    intent 승격(_promote_unknown_intent_for_target_signal)과 recommend_campaign 필수조건 검증
    (validate_required_input_conditions)이 같은 신호 집합을 공유하게 한다."""
    has_member_signal = compile_member_target_conditions(query_plan)["has_signal"]
    target_user = query_plan.get("target_user", {})
    purchase_object = target_user.get("purchase_object")
    has_density_target = isinstance(query_plan.get("region_density_target"), dict)
    has_metric_ranking = isinstance(query_plan.get("member_metric_ranking"), dict)
    # 주문 횟수 행동(첫 구매/재구매/무구매)·구매 미발생 기간(최근 N일 미구매)도 주문 집계로 실추출 가능한 신호다.
    behaviors = target_user.get("behaviors", [])
    has_order_count_signal = any(behavior in _order_count_targets_config()["behaviors"] for behavior in behaviors)
    has_purchase_inactivity = isinstance(target_user.get("purchase_inactivity"), dict)
    has_birthday_target = isinstance(target_user.get("birthday_target"), dict)
    # 집계 조건(누적 구매 금액/횟수 임계값)도 주문 집계로 실추출 가능한 세그먼트 신호다.
    has_aggregate_condition = bool(target_user.get("aggregate_conditions"))
    return bool(
        has_member_signal
        or has_density_target
        or has_metric_ranking
        or has_order_count_signal
        or has_purchase_inactivity
        or has_birthday_target
        or has_aggregate_condition
        or (isinstance(purchase_object, str) and bool(purchase_object))
    )


def _promote_unknown_intent_for_target_signal(query_plan: dict[str, Any]) -> None:
    """intent=unknown 이라도 실DB로 추출 가능한 타겟 신호가 있으면 find_user_segment 로 승격한다.

    '서울 거주 20대 여성'처럼 캠페인/조회 동사 없이 회원 속성만 나열한 프롬프트는 파서(룰/LLM)가
    intent=unknown 을 주는데, 그러면 build_sql_template_candidate 가 회원 타겟 빌더를 아예 호출하지
    않아(no_sql_candidates) 성별·연령을 정상 파싱하고도 SQL 을 못 만든다. 실DB 매핑 가능한 회원
    신호(성별/연령/등급/휴면 등)나 상품 구매 이력이 있으면 세그먼트 조회로 보고 승격한다(발송/메시지
    목적은 없으므로 recommend_campaign 이 아니라 find_user_segment)."""
    if query_plan.get("intent") != "unknown":
        return
    if _has_member_target_signal(query_plan):
        query_plan["intent"] = "find_user_segment"


def _infer_objective(query: str) -> str | None:
    compact_query = query.replace(" ", "").casefold()
    if _is_repurchase_goal_context(query):
        return "repurchase"
    if _is_reactivation_goal_context(query):
        return "reactivation"
    for objective, keywords in _lexicon_objective_rules():
        if any(keyword in compact_query for keyword in keywords):
            return objective
    return None


def _is_awareness_announcement_context(query: str) -> bool:
    # 신제품/출시/런칭 등 인지(awareness) 키워드 + 알림/홍보 아웃리치 동사가 함께 있으면 캠페인 발송 의도.
    # "신제품 관심 고객 찾아줘"(조회)처럼 아웃리치 동사가 없으면 걸리지 않도록 둘 다 요구한다.
    compact_query = query.replace(" ", "").casefold()
    has_launch = any(keyword in compact_query for keyword in _lexicon_terms("awareness_launch_terms"))
    has_announce = any(keyword in compact_query for keyword in _lexicon_terms("awareness_announce_terms"))
    return has_launch and has_announce


def _is_sales_outreach_context(query: str) -> bool:
    # 판매 동사(팔다/판매/sell) + 대상 지향(에게/한테/고객/대상/타겟)이 함께 있으면 특정 상품을
    # 파는 캠페인 발송 의도. "고객 찾아줘"(조회)처럼 판매 동사가 없으면 걸리지 않도록 둘 다 요구한다.
    # "팔레트/팔로우" 등 오탐을 피하려고 "팔" 단독이 아닌 "팔고/팔려/판매"만 판매 동사로 본다.
    compact_query = query.replace(" ", "").casefold()
    has_sell = any(keyword in compact_query for keyword in _lexicon_terms("sell_outreach_verbs"))
    has_audience = any(keyword in compact_query for keyword in _lexicon_terms("sell_outreach_audience"))
    return has_sell and has_audience


def _is_reactivation_goal_context(query: str) -> bool:
    compact_query = query.replace(" ", "").casefold()
    return any(keyword in compact_query for keyword in _lexicon_terms("reactivation_goal_terms"))


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
    # "…을/를 구매한/구입한/구매했던/구입하신 …" 같은 동사형뿐 아니라, "기저귀 구매 고객" 같은 명사형
    # (구매/구입 + 고객/회원/이력 등)도 상품 구매 이력 타겟으로 본다. 타겟팅 프롬프트 재작성(normalize_prompt)
    # 이 "…를 산 고객"을 "… 구매 고객" 명사형으로 정규화하므로, 명사형을 놓치면 조건이 통째로 사라진다.
    # object 클래스에 공백을 넣지 않아 "를/을" 또는 구매/구입 직전 상품 명사만 잡는다. (공백 허용 시 "40대
    # 여성 중 기저귀를 구매한" 처럼 앞 절 조건까지 삼켜 LIKE 가 무의미해지므로) 상품 카테고리 단어면 재현율에 충분하다.
    match = _PURCHASE_OBJECT_PATTERN.search(query)
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


# ── 상품(구매이력/판매) 추출: 정규식 우선 → 검증된 LLM 폴백 ────────────────────────
# 재작성기(normalize_prompt)는 자유 입력을 다양한 표현형("… 구매 고객 / 구입 이력 / 샀던 …")으로
# 정규화하지만 정규식 추출기는 고정 패턴만 안다. 이 간극 때문에 표현형이 바뀔 때마다 조건이 조용히
# 사라져 규칙(정규식)에 패턴을 계속 덧붙여야 했다. 폴백은 그 두더지잡기를 끊는다:
#   재현율(표현형 유연성)은 LLM 이, 정밀도(없는 상품을 지어내지 않음)는 원문 존재 검증이 담당한다.
# 정규식이 이미 뽑았거나 구매/판매 신호 자체가 없으면 LLM 을 호출하지 않아 비용/지연을 최소화한다.
def _has_purchase_history_signal(query: str) -> bool:
    compact = query.replace(" ", "").casefold()
    return any(signal in compact for signal in _lexicon_terms("purchase_history_signals"))


def _has_sell_signal(query: str) -> bool:
    compact = query.replace(" ", "").casefold()
    return any(signal in compact for signal in _lexicon_terms("sell_outreach_verbs"))


def _object_present_in_text(obj: str, text: str) -> bool:
    """정제된 상품어(obj)의 모든 토큰이 원문(text)에 그대로 등장하면 True(환각 방지 검증).

    LLM 이 원문에 없는 상품을 지어내면(예: '기저귀 구매 고객' -> '냉장고') 여기서 걸러진다.
    """
    compact = re.sub(r"\s+", "", text).casefold()
    tokens = re.findall(r"[0-9A-Za-z가-힣_+\-]+", obj.casefold())
    return bool(tokens) and all(token in compact for token in tokens)


def _validated_object(value: Any, text: str) -> str | None:
    """LLM 이 뽑은 상품어를 정제 후 원문 존재 검증까지 통과한 값만 반환한다(아니면 None)."""
    if not isinstance(value, str) or not value.strip():
        return None
    sanitized = _sanitize_purchase_object(value)
    if sanitized and _object_present_in_text(sanitized, text):
        return sanitized
    return None


def _target_object_extract_system_prompt(prompt_dir: Path | None = DEFAULT_PROMPT_DIR) -> str:
    fallback = "\n".join(
        [
            "너는 캠페인 타겟팅 문장에서 '상품명'만 뽑아내는 추출기다.",
            "purchase_object: 타겟 오디언스가 '구매/구입한' 상품(구매 이력 조건)의 상품명.",
            "sell_object: 이 캠페인이 '팔려는/판매하려는' 상품명.",
            "반드시 입력 문장에 그대로 등장하는 명사만 사용한다(번역·유추·추가 금지).",
            "해당 조건이 없으면 null 로 둔다. 조사·수식어(첫/재/최근 등)는 빼고 핵심 상품 명사만 남긴다.",
            '다음 JSON object 만 출력한다: {"purchase_object": "상품명 또는 null", "sell_object": "상품명 또는 null"}.',
        ]
    )
    return _read_prompt_template(prompt_dir, "target_object_extract_system.txt", fallback)


def _llm_extract_target_objects(
    query: str, llm_model: str, prompt_dir: Path | None
) -> dict[str, Any] | None:
    """LLM 으로 문장에서 purchase_object/sell_object 후보를 추출한다. 사용 불가/실패 시 None."""
    if not os.getenv("OPENAI_API_KEY") or not query.strip():
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
                {"role": "system", "content": _target_object_extract_system_prompt(prompt_dir)},
                {"role": "user", "content": query},
            ],
            timeout=_prompt_rewrite_timeout_seconds(),
        )
        data = json.loads(response.choices[0].message.content or "{}")
        if not isinstance(data, dict):
            return None
        result = {
            "purchase_object": data.get("purchase_object") if isinstance(data.get("purchase_object"), str) else None,
            "sell_object": data.get("sell_object") if isinstance(data.get("sell_object"), str) else None,
        }
        _write_rag_llm_log("target_object_extraction", {"query": query, **result})
        return result
    except Exception:
        # 폴백은 치명적이지 않다(정규식 결과 그대로 진행).
        return None


def _target_object_llm_fallback_enabled() -> bool:
    value = os.getenv("TARGET_OBJECT_LLM_FALLBACK", "true").strip().casefold()
    return value not in {"0", "false", "no", "off"}


def _apply_llm_object_fallback(
    query: str,
    plan: dict[str, Any],
    llm_model: str = DEFAULT_LLM_MODEL,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
) -> None:
    """정규식이 못 뽑은 상품 구매이력/판매 상품을 검증된 LLM 추출로 보완한다.

    parser 모드와 무관하게 OPENAI_API_KEY 유무로 동작한다(재작성기와 동일한 전제). 프로덕션이
    QUERY_PARSER=rules 여도 재작성이 LLM 으로 도는 환경이라, 이 폴백도 rules 경로에서 함께 동작해야
    표현형 변화로 사라진 타겟 조건을 복구한다. LLM 값은 반드시 원문 존재 검증을 통과해야 채택된다.
    """
    if not _target_object_llm_fallback_enabled() or not os.getenv("OPENAI_API_KEY"):
        return
    target_user = plan.setdefault("target_user", {})
    constraints = plan.setdefault("campaign_constraints", {})
    need_purchase = not target_user.get("purchase_object") and _has_purchase_history_signal(query)
    need_sell = not constraints.get("sell_object") and _has_sell_signal(query)
    if not (need_purchase or need_sell):
        return
    extracted = _llm_extract_target_objects(query, llm_model, prompt_dir)
    if not extracted:
        return
    if need_purchase:
        purchase_object = _validated_object(extracted.get("purchase_object"), query)
        if purchase_object:
            target_user["purchase_object"] = purchase_object
    if need_sell:
        sell_object = _validated_object(extracted.get("sell_object"), query)
        if sell_object:
            constraints["sell_object"] = sell_object


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


_HANGUL_SYLLABLE = re.compile(r"[가-힣]")
_ASCII_ALNUM = re.compile(r"[0-9A-Za-z]")
# 값(예: 지역명) 뒤에 한글이 바로 이어져도 값 언급으로 인정할 조사/행정접미(예: '서울에', '경기도').
_VALUE_TAIL_TOKENS = (
    "특별자치시", "특별자치도", "특별시", "광역시", "도", "시", "권", "지역", "지방", "쪽",
    "거주", "사는", "살", "에서", "에게", "에", "은", "는", "이", "가", "을", "를", "의",
    "만", "과", "와", "랑", "보다", "까지", "부터",
)


def _value_token_mentioned(value: str, query: str) -> bool:
    """값(예: '서울', 'VIP')이 프롬프트에 '토큰 경계'로 나타나는지 검사한다(ASCII 는 대소문자 무시).

    값만으로 조건을 활성화하는 경로(회원 값 인덱스)는 순수 부분문자열 매칭이면 짧은 값이 무관한
    단어에 얻어걸린다(예: '경기'가 '경기침체'에, 'APP'이 'HAPPY'에). 앞경계: 한글 금지, ASCII 값이면
    영숫자도 금지. 뒤경계: 끝/비한글·비영숫자면 통과, 한글 값+한글 연속은 조사·행정접미만 허용,
    ASCII 값 뒤 영숫자는 거절(단어 내부), ASCII 값 뒤 한글은 자연 경계('VIP고객')로 허용.
    """
    if not value:
        return False
    haystack = query.casefold()
    needle = value.casefold()
    first_ascii = bool(_ASCII_ALNUM.match(needle[0]))
    last_ascii = bool(_ASCII_ALNUM.match(needle[-1]))
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return False
        start = idx + 1
        before = haystack[idx - 1] if idx > 0 else ""
        after = haystack[idx + len(needle):]
        if before and (_HANGUL_SYLLABLE.match(before) or (first_ascii and _ASCII_ALNUM.match(before))):
            continue  # 앞이 같은 종류 문자면 다른 단어의 일부
        if not after:
            return True
        next_char = after[0]
        if _HANGUL_SYLLABLE.match(next_char):
            if not _HANGUL_SYLLABLE.match(needle[-1]) or any(after.startswith(token) for token in _VALUE_TAIL_TOKENS):
                return True
            continue
        if last_ascii and _ASCII_ALNUM.match(next_char):
            continue  # ASCII 단어 내부(예: 'APP'이 'APPLE'에)
        return True


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
        # 라벨 없이 값만 언급되는 회원 속성은 member_value_index(_apply_member_value_filters)가 담당한다.
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
        plan["cart_context"] = any(term in compact_query for term in _lexicon_terms("cart_terms"))


DEFAULT_MEMBER_VALUE_INDEX_PATH = Path("docs/data/member_value_index.json")
_PLAIN_NUMERIC_VALUE = re.compile(r"^[\d.\-/:%\s]+$")


@functools.lru_cache(maxsize=4)
def _load_member_value_index(path_text: str) -> dict[str, Any] | None:
    path = Path(path_text)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _matchable_value_name(name: str) -> bool:
    """프롬프트 토큰 매칭에 쓸 수 있는 값 이름인지(짧거나 숫자뿐인 값은 오탐 위험이라 제외)."""
    if not name or _PLAIN_NUMERIC_VALUE.match(name):
        return False
    if _HANGUL_SYLLABLE.search(name):
        return len(name) >= 2
    return len(name) >= 3


def _region_columns() -> set[str]:
    """지역(행정구역) 컬럼명 집합. member_target_filters.json 의 region 설정에서 파생한다."""
    config = _region_density_config()
    columns: set[str] = set()
    default_column = config.get("default_column")
    if isinstance(default_column, str) and default_column:
        columns.add(default_column.upper())
    granularity_columns = config.get("granularity_columns")
    if isinstance(granularity_columns, dict):
        columns.update(str(value).upper() for value in granularity_columns.values() if value)
    return columns or {"SIGUNGU", "SIDO"}


_REGION_CITY_SUFFIX = re.compile(r"(?:특별자치시|특별자치도|특별시|광역시|시|군)$")


def _region_city_alias_map(values: list[dict[str, Any]]) -> dict[str, list[str]]:
    """시군구 값 목록에서 '시 단위 별칭 -> 그 시에 속한 (구 단위) 저장값들' 매핑을 만든다.

    실DB SIGUNGU 는 구를 둔 시를 '안양시 동안구'처럼 구 단위로 저장한다. 사용자가 '안양'(시 단위)만
    입력하면 저장값 전체 매칭('안양시 동안구' ∈ 프롬프트?)이 실패해 지역 조건이 조용히 사라진다.
    그래서 시 성분('안양시'→'안양')을 별칭으로 뽑아 같은 시의 구 단위 값 전체를 IN 으로 확장한다.
    별칭이 저장값과 같으면(광역시 자치구 '남구' 등) 기존 정확 매칭이 이미 처리하므로 제외한다.
    인덱스 값에서 파생하므로 새 도시는 인덱스 재생성만으로 자동 반영된다(코드 수정 없음)."""
    alias_map: dict[str, list[str]] = {}
    for entry in values:
        name = entry.get("name") or ""
        if not name:
            continue
        city_token = name.split(" ", 1)[0]
        bare = _REGION_CITY_SUFFIX.sub("", city_token)
        # 별칭이 저장값 자체와 같으면 확장 대상이 아니다(정확 매칭이 담당). 너무 짧으면 오탐 위험.
        if len(bare) < 2 or bare == name:
            continue
        alias_map.setdefault(bare, [])
        if name not in alias_map[bare]:
            alias_map[bare].append(name)
    return alias_map


def _apply_member_value_filters(
    query: str, plan: dict[str, Any], index_path: Path | None = DEFAULT_MEMBER_VALUE_INDEX_PATH
) -> None:
    """회원 값 인덱스(member_value_index.json)로 프롬프트의 값 토큰을 실컬럼 조건으로 해석한다.

    build_member_value_index.py 가 실DB에서 자동 생성한 인덱스가 소스이므로 컬럼별 수동 큐레이션이
    필요 없다 — 새 컬럼/값은 인덱스 재생성만으로 타겟팅에 반영된다. 값 이름은 _value_token_mentioned
    경계 검사로 매칭해 부분문자열 오탐('경기'≠'경기침체')을 막고, 결과는 dimension_filters 와 같은
    형태로 추가돼 기존 컴파일러(compile_member_target_conditions)·커버리지 검증이 그대로 소비한다.
    """
    index = _load_member_value_index(str(index_path)) if index_path else None
    if not index:
        return
    table = index.get("table", "CRM_MB_BASEINFO")
    # 이미 다른 경로(디멘션 카탈로그 등)가 조건을 만든 컬럼은 건너뛴다(이중 술어 방지).
    existing_columns = {
        (dimension_filter.get("column") or "").split(".")[-1].upper()
        for dimension_filter in plan.get("dimension_filters", [])
    }
    matches_by_column: dict[str, list[tuple[str, str]]] = {}
    columns_by_name: dict[str, set[str]] = {}
    column_sources: dict[str, dict[str, Any]] = {}
    region_columns = _region_columns()

    def _record_match(column: str, code: str, name: str) -> None:
        matches_by_column.setdefault(column, [])
        if code not in [existing_code for existing_code, _ in matches_by_column[column]]:
            matches_by_column[column].append((code, name))
        columns_by_name.setdefault(name.casefold(), set()).add(column)

    for column_entry in index.get("columns", []):
        column = column_entry.get("column")
        if not column or column.upper() in existing_columns:
            continue
        column_sources[column] = column_entry
        values = column_entry.get("values", [])
        for entry in values:
            code = entry.get("value") or ""
            name = entry.get("name") or ""
            if not code or not _matchable_value_name(name):
                continue
            if _value_token_mentioned(name, query):
                _record_match(column, code, name)
        # 지역 컬럼은 시 단위 입력('안양')을 같은 시의 구 단위 저장값('안양시 동안구/만안구')으로 확장한다.
        if column.upper() in region_columns:
            name_to_code = {(entry.get("name") or ""): (entry.get("value") or "") for entry in values}
            exact_names = {name for _, name in matches_by_column.get(column, [])}
            for city_alias, member_names in _region_city_alias_map(values).items():
                # 사용자가 특정 구('안양시 동안구')를 명시했으면 그 시를 전체로 넓히지 않는다(정확도 우선).
                if any(name in exact_names for name in member_names):
                    continue
                if not _value_token_mentioned(city_alias, query):
                    continue
                for name in member_names:
                    code = name_to_code.get(name)
                    if code:
                        _record_match(column, code, name)

    # 같은 이름이 여러 컬럼에 존재하면(예: 'App' 이 가입채널·로그인채널 양쪽) 어느 컬럼 조건인지
    # 추측할 수 없으므로 그 이름은 매칭에서 제외한다(조용한 오필터 방지).
    ambiguous_names = {name for name, columns in columns_by_name.items() if len(columns) > 1}

    filters = []
    for column, matched in matches_by_column.items():
        matched = [(code, name) for code, name in matched if name.casefold() not in ambiguous_names]
        if not matched:
            continue
        # 보조 속성 테이블 컬럼(예: JOB_CD)은 저장 테이블/조인키를 실어 회원키 서브쿼리로 컴파일되게 한다.
        source_table = column_sources[column].get("source_table") or table
        filter_entry = {
            "dimension_id": "member_value:" + column,
            "prompt_label": column,
            "column": source_table + "." + column,
            "table": source_table,
            "operator": "IN",
            "codes": [code for code, _ in matched],
            "names": [name for _, name in matched],
            "source": "member_value_index",
        }
        if column_sources[column].get("join_column"):
            filter_entry["join_column"] = column_sources[column]["join_column"]
        filters.append(filter_entry)
    if filters:
        plan.setdefault("dimension_filters", [])
        plan["dimension_filters"].extend(filters)


# "X가 많이 거주하는 동네/지역" 같은 밀집 지역(집계 랭킹) 표현 감지. 지역 단위 어휘와 단위→컬럼
# 매핑(예: 시도 → SIDO, 그 외 → SIGUNGU)은 member_target_filters.json 의 region_density 가 소유한다.
def _region_density_config() -> dict[str, Any]:
    config = _MEMBER_TARGET_FILTERS.get("region_density")
    return config if isinstance(config, dict) else _DEFAULT_MEMBER_TARGET_FILTERS["region_density"]


def _region_granularity_alternation() -> str:
    tokens = [t for t in _region_density_config().get("granularity_tokens", []) if isinstance(t, str) and t]
    if not tokens:
        tokens = list(_DEFAULT_MEMBER_TARGET_FILTERS["region_density"]["granularity_tokens"])
    tokens.sort(key=len, reverse=True)  # '시군구'가 '구'보다 먼저 매칭되게 긴 토큰 우선
    return "|".join(re.escape(token) for token in tokens)


_REGION_DENSITY_PATTERN = re.compile(
    rf"(?:가장\s*|제일\s*)?많이\s*(?:거주하|사|살고\s*있)는\s*({_region_granularity_alternation()})"
)
_REGION_DENSITY_ALT_PATTERN = re.compile(rf"밀집\s*({_region_granularity_alternation()})")
_REGION_DENSITY_TOP_N_PATTERN = re.compile(r"상위\s*(\d+)|(?:top|톱)\s*(\d+)", re.IGNORECASE)

DEFAULT_MEMBER_METRICS_PATH = Path("docs/data/member_metrics.json")


@functools.lru_cache(maxsize=4)
def _load_member_metrics(path_text: str) -> dict[str, Any] | None:
    path = Path(path_text)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


@functools.lru_cache(maxsize=4)
def _member_metric_region_pattern(path_text: str) -> "re.Pattern[str] | None":
    """지표 레지스트리(member_metrics.json)의 동의어로 '<지표>가 높은 지역' 패턴을 동적 생성한다.

    새 지표는 레지스트리에 항목 추가만으로 패턴에 반영된다(코드 수정 없음). 동의어는 긴 것부터
    매칭해 '평균 구매금액'이 '구매금액'보다 먼저 잡히게 한다.
    """
    registry = _load_member_metrics(path_text)
    if not registry:
        return None
    synonyms: list[tuple[str, str]] = []  # (synonym, metric_id)
    for metric in registry.get("metrics", []):
        for synonym in metric.get("synonyms", []):
            if isinstance(synonym, str) and synonym:
                synonyms.append((synonym, metric["metric_id"]))
    if not synonyms:
        return None
    synonyms.sort(key=lambda pair: len(pair[0]), reverse=True)
    alternation = "|".join(re.escape(synonym) for synonym, _ in synonyms)
    return re.compile(
        rf"({alternation})(?:이|가|을|를)?\s*(?:가장\s*|제일\s*)?(?:높은|많은|큰|상위)\s*({_region_granularity_alternation()})"
    )


def _member_metric_by_synonym(path_text: str, matched_synonym: str) -> dict[str, Any] | None:
    registry = _load_member_metrics(path_text)
    if not registry:
        return None
    for metric in registry.get("metrics", []):
        if matched_synonym in metric.get("synonyms", []):
            return metric
    return None


def _member_metric_ranking_config() -> dict[str, Any]:
    config = _MEMBER_TARGET_FILTERS.get("member_metric_ranking")
    return config if isinstance(config, dict) else _DEFAULT_MEMBER_TARGET_FILTERS["member_metric_ranking"]


def _member_ranking_granularity_alternation() -> str:
    tokens = [t for t in _member_metric_ranking_config().get("granularity_tokens", []) if isinstance(t, str) and t]
    if not tokens:
        tokens = list(_DEFAULT_MEMBER_TARGET_FILTERS["member_metric_ranking"]["granularity_tokens"])
    tokens.sort(key=len, reverse=True)  # '구매자'가 '자'보다, '고객님'이 '고객'보다 먼저 매칭되게 긴 토큰 우선
    return "|".join(re.escape(token) for token in tokens)


@functools.lru_cache(maxsize=4)
def _member_metric_customer_pattern(path_text: str) -> "re.Pattern[str] | None":
    """지표 레지스트리(member_metrics.json)의 동의어로 '<지표>가 높은 고객' 패턴을 동적 생성한다.

    지역 랭킹(_member_metric_region_pattern)의 회원 단위 짝이다 — granularity 만 지역 토큰 대신
    고객/회원 토큰이다. '누적 구매금액이 높은 고객'처럼 회원 단위로 지표를 정렬해 상위 N 명을 뽑는
    표현을 결정론 빌더(build_member_metric_ranking_sql_candidate)로 라우팅해, LLM 폴백이 없는 컬럼을
    지어내는 것을 막는다. 동의어는 긴 것부터 매칭한다('평균 구매금액'이 '구매금액'보다 먼저)."""
    registry = _load_member_metrics(path_text)
    if not registry:
        return None
    synonyms: list[tuple[str, str]] = []
    for metric in registry.get("metrics", []):
        for synonym in metric.get("synonyms", []):
            if isinstance(synonym, str) and synonym:
                synonyms.append((synonym, metric["metric_id"]))
    if not synonyms:
        return None
    synonyms.sort(key=lambda pair: len(pair[0]), reverse=True)
    alternation = "|".join(re.escape(synonym) for synonym, _ in synonyms)
    return re.compile(
        rf"({alternation})(?:이|가|을|를)?\s*(?:가장\s*|제일\s*)?(?:높은|많은|큰|상위)\s*"
        rf"(?:\d+\s*명?\s*)?({_member_ranking_granularity_alternation()})"
    )


def _apply_member_metric_ranking_target(query: str, plan: dict[str, Any]) -> None:
    """'<지표>가 높은 고객'을 회원 단위 지표 랭킹 타겟(member_metric_ranking)으로 해석한다.

    지역 랭킹(_apply_region_density_target)의 회원 단위 짝이다. build_member_metric_ranking_sql_candidate
    가 이 플래그를 보고 지표 테이블(CRM_MB_MONTHCRMINFO)을 회원키로 조인해 지표값 내림차순 상위 N
    명을 뽑는 SQL 을 생성한다(월 스냅샷 중복은 레지스트리 grain_filter 로 방지). 데모 스키마(users
    테이블) 참조라 실DB 에 못 쓰는 매출 순위/고매출 정책(top_revenue_user/high_revenue_user)이 같은
    어구에 얻어걸려 남으면 threshold clarification 등으로 파이프라인이 막히므로, 지표어가 라벨/동의어에
    포함된 target_user 정책을 소비한다."""
    if isinstance(plan.get("region_density_target"), dict):
        # 지역 랭킹으로 이미 해석됐으면(예: '매출 높은 지역') 회원 랭킹으로 중복 해석하지 않는다.
        return
    pattern = _member_metric_customer_pattern(str(DEFAULT_MEMBER_METRICS_PATH))
    match = pattern.search(query) if pattern else None
    if not match:
        return
    matched_metric_text = match.group(1)
    metric_info = _member_metric_by_synonym(str(DEFAULT_MEMBER_METRICS_PATH), matched_metric_text)
    if metric_info is None:
        return
    config = _member_metric_ranking_config()
    top_n = int(config.get("default_top_n") or 100)
    top_match = _REGION_DENSITY_TOP_N_PATTERN.search(query) or re.search(r"(\d+)\s*명", query)
    if top_match:
        max_top_n = int(config.get("max_top_n") or 10000)
        top_n = max(1, min(int(next(group for group in top_match.groups() if group)), max_top_n))
    plan["member_metric_ranking"] = {
        "metric_id": metric_info["metric_id"],
        "metric_label": metric_info.get("ko_label", metric_info["metric_id"]),
        "top_n": top_n,
    }
    # 같은 지표어에 얻어걸린 데모 스키마(users) 회원 정책을 소비한다(실DB 미지원 → clarification 차단).
    plan["policy_constraints"] = [
        policy
        for policy in plan.get("policy_constraints", [])
        if not (
            policy.get("scope") == "target_user"
            and matched_metric_text in str(policy.get("ko_label", "")) + str(policy.get("canonical", ""))
        )
    ]


# "최근 N일/개월 동안 구매하지 않은" 같은 구매 미발생 기간(구매 리센시) 신호. 구매 부정어 + 시간 창이
# 함께 있을 때만 잡는다. 시간 창이 없으면(예: '미구매 고객') '전혀 구매 안 함(no_purchase)'과 구분이
# 없으므로 여기서 잡지 않고 기존 no_purchase 경로로 둔다.
_PURCHASE_NEG_SIGNALS = (
    "구매안", "구매하지않", "구매않", "구매없", "구입안", "구입하지않", "구입없", "주문안", "주문하지않", "미구매",
)


def _parse_purchase_inactivity_period(query: str) -> dict[str, Any] | None:
    compact_query = query.replace(" ", "").casefold()
    if not any(signal in compact_query for signal in _PURCHASE_NEG_SIGNALS):
        return None
    month_match = re.search(r"(?P<value>\d{1,2})\s*(?:개월|달)", query)
    if month_match:
        months = int(month_match.group("value"))
        if months > 0:
            return {"value": months, "unit": "months", "min_days": months * 30}
    day_match = re.search(r"(?P<value>\d{1,4})\s*일", query)
    if day_match:
        days = int(day_match.group("value"))
        if days > 0:
            return {"value": days, "unit": "days", "min_days": days}
    return None


def _apply_purchase_inactivity_filter(query: str, plan: dict[str, Any]) -> None:
    """'최근 N일 동안 구매하지 않은 고객'을 구매 미발생 기간 타겟(purchase_inactivity)으로 해석한다.

    '전혀 구매 안 함(no_purchase, 평생 무주문)'과 다르다 — 과거엔 샀어도 최근 N일 내 주문이 없으면
    대상이다(이탈/재참여 세그먼트). LLM 파서가 '구매하지 않은'을 no_purchase 로 오분류하는 경우가
    있어, 기간 창이 잡히면 no_purchase 를 제거해 오분류를 바로잡는다(윈도우 anti-join 빌더가 처리)."""
    period = _parse_purchase_inactivity_period(query)
    if period is None:
        return
    plan.setdefault("target_user", {})["purchase_inactivity"] = period
    plan["target_user"]["behaviors"] = [
        behavior for behavior in plan["target_user"].get("behaviors", []) if behavior != "no_purchase"
    ]


# 범용 집계 조건('<지표> <임계값> 이상/이하')의 값·기간·연산자 파서. 지표/컬럼 정의는 member_target_filters.json
# 의 aggregate_targets 가 소유하고(코드-프리 레지스트리), 여기서는 프롬프트 텍스트에서 조건만 뽑는다.
# 배수 단위는 긴 것부터(천만/백만이 만/천보다 먼저) 매칭한다.
_AMOUNT_MAGNITUDES = (("억", 100_000_000), ("천만", 10_000_000), ("백만", 1_000_000), ("만", 10_000), ("천", 1_000))
_AGG_OPERATOR_WORDS = {"이상": ">=", "초과": ">", "이하": "<=", "미만": "<"}
# 지표 뒤에 오는 "<수><배수?> <측정단위?> <비교어>" (예: '100만 원 이상', '5건 이상', '50만원 초과').
_AGG_THRESHOLD_PATTERN = re.compile(
    r"(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<mag>억|천만|백만|만|천)?\s*(?:원|건|회|명|개|장|번|건수|회수)?\s*(?P<op>이상|초과|이하|미만)"
)
_RECENT_WINDOW_PATTERN = re.compile(r"최근\s*(\d+)\s*(일|주|개월|달|년)")
_WINDOW_UNIT_DAYS = {"일": 1, "주": 7, "개월": 30, "달": 30, "년": 365}


def _parse_korean_amount(number_text: str, magnitude_text: str) -> float | None:
    """'100'+'만' -> 1000000. 배수어 없으면 숫자 그대로. 콤마 제거."""
    try:
        value = float(number_text.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None
    for unit, multiplier in _AMOUNT_MAGNITUDES:
        if magnitude_text and magnitude_text.startswith(unit):
            return value * multiplier
    return value


def _parse_recent_window_days(query: str) -> int | None:
    """'최근 90일' -> 90, '최근 3개월' -> 90, '최근 2주' -> 14 (없으면 None = 전체 기간)."""
    match = _RECENT_WINDOW_PATTERN.search(query)
    if not match:
        return None
    count = int(match.group(1))
    if count <= 0:
        return None
    return count * _WINDOW_UNIT_DAYS[match.group(2)]


def _apply_aggregate_condition_filter(query: str, plan: dict[str, Any]) -> None:
    """'[최근 N일] <지표> <임계값> 이상/이하'를 범용 집계 조건(aggregate_conditions)으로 해석한다.

    지표 동의어(구매 금액/구매 횟수 등) 바로 뒤의 임계값 어구를 잡아 {metric_id, operator, threshold,
    window_days} 로 만든다. build_aggregate_targets_sql_candidate 가 주문 테이블 회원별 집계 서브쿼리
    (GROUP BY MEMBER_NO HAVING agg(col) op threshold)로 컴파일하고, 성별/연령/등급/지역 등 회원 속성은
    compile_member_target_conditions 로 같은 SQL 에 AND 결합한다."""
    config = _aggregate_targets_config()
    metrics = config.get("metrics", {})
    if not isinstance(metrics, dict) or not metrics:
        return
    window_days = _parse_recent_window_days(query)
    conditions: list[dict[str, Any]] = []
    # 긴 동의어를 가진 지표부터 본다('구매 금액'이 '구매 횟수'와 겹치지 않도록 지표 단위로 독립 매칭).
    for metric_id, metric in metrics.items():
        synonyms = sorted(
            [synonym for synonym in metric.get("synonyms", []) if isinstance(synonym, str) and synonym],
            key=len,
            reverse=True,
        )
        for synonym in synonyms:
            index = query.find(synonym)
            if index < 0:
                continue
            tail = query[index + len(synonym): index + len(synonym) + 40]
            match = _AGG_THRESHOLD_PATTERN.search(tail)
            if match is None:
                continue
            threshold = _parse_korean_amount(match.group("num"), match.group("mag") or "")
            if threshold is None:
                continue
            conditions.append(
                {
                    "metric_id": metric_id,
                    "operator": _AGG_OPERATOR_WORDS[match.group("op")],
                    "threshold": threshold,
                    "window_days": window_days,
                    "label": metric.get("ko_label", metric_id),
                }
            )
            break  # 한 지표당 하나의 조건만
    if conditions:
        plan.setdefault("target_user", {})["aggregate_conditions"] = conditions


# 생일 타겟: BIRTHDAY(YYYYMMDD)의 월일만 오늘과 비교한다(년도 무시). '이달/이번 달'이면 월만 비교.
_BIRTHDAY_SIGNALS = ("생일", "생신", "birthday")
_BIRTHDAY_MONTH_SIGNALS = ("이달", "이번달", "이번 달", "당월", "금월", "이달의")


def _apply_birthday_target_filter(query: str, plan: dict[str, Any]) -> None:
    """'오늘 생일인 고객' / '이달 생일 고객'을 생일 타겟(birthday_target)으로 해석한다.

    생일은 BIRTHDAY(YYYYMMDD)의 월일(MMDD)만 오늘과 비교해야 한다(년도까지 비교하면 아무도 안 걸림).
    '이달/이번 달 생일'은 월(MM)만 비교한다. compile_member_target_conditions 가 실컬럼 술어로 만들어
    성별/연령 등과 자동 결합한다. '생년월일'(원본 DOB 컬럼 언급)은 생일 타겟이 아니므로 잡지 않는다."""
    compact = query.replace(" ", "").casefold()
    # '생년월일/출생' 등 원본 DOB 필드 언급은 생일 이벤트 타겟이 아니다.
    if "생일" not in compact and "생신" not in compact and "birthday" not in compact:
        return
    granularity = "month" if any(sig in compact for sig in ("이달", "이번달", "당월", "금월")) else "day"
    plan.setdefault("target_user", {})["birthday_target"] = {"granularity": granularity}


# 신규 가입 타겟: '신규 가입/신규 회원/새 가입자/new user' 등 가입 신호로 잡는다. 기본 창은
# signup_target.default_days 이고, '최근 N일/N개월 (이내) 가입' 이 있으면 그 창으로 덮는다.
_SIGNUP_SIGNALS = ("신규가입", "신규회원", "신규유저", "신규고객", "새가입", "새로가입", "새가입자", "가입한지",
                   "newuser", "newmember", "newlyregistered", "newsignup", "signedup")
# '가입/등록/신규' 뒤나 앞에 붙는 기간 창(예: '최근 30일 이내 가입', '가입 30일차').
_SIGNUP_PERIOD_PATTERN = re.compile(r"(\d{1,4})\s*(일|개월|달)")


def _apply_signup_target_filter(query: str, plan: dict[str, Any]) -> None:
    """'신규 가입 고객'을 신규 가입 타겟(signup_target, REG_DT 최근 N일 창)으로 해석한다.

    REG_TYPE_CD.NEW 는 전체의 96%라 무의미하므로 '신규'는 가입일(REG_DT) 기준 최근 N일로 정의한다.
    compile_member_target_conditions 가 signup_target 또는 lifecycle 'new_user' 를 실컬럼 술어로 만들어
    성별/연령 등과 자동 결합한다(LLM 파서가 이미 new_user 를 내보내는 경로와 이중화 — 창 파싱은 이쪽 담당).
    """
    compact = query.replace(" ", "").casefold()
    match = _SIGNUP_PERIOD_PATTERN.search(query)
    has_signup_signal = any(signal in compact for signal in _SIGNUP_SIGNALS)
    # '가입/등록' + 기간 창(예: '30일 이내 가입한 고객')도 신규 가입 타겟으로 본다. 단 '미가입/재가입/
    # 탈퇴' 등 반대·무관 맥락은 제외한다('가입' 부분문자열 오탐 방지).
    join_window = (
        match is not None
        and ("가입" in compact or "등록" in compact)
        and not any(neg in compact for neg in ("미가입", "재가입", "비가입", "가입안", "가입하지", "탈퇴"))
    )
    if not has_signup_signal and not join_window:
        return
    days: int | None = None
    if match:
        value = int(match.group(1))
        if value > 0:
            days = value * 30 if match.group(2) in ("개월", "달") else value
    # days 가 None 이면 compile 이 signup_target.default_days 를 쓴다.
    plan.setdefault("target_user", {})["signup_target"] = {"days": days}


def _apply_region_density_target(query: str, plan: dict[str, Any]) -> None:
    """'X가 많이 거주하는 동네' 표현을 밀집 지역 랭킹 타겟(region_density_target)으로 해석한다.

    X(코호트 조건: 성별/연령/등급/값인덱스 …)는 별도 필드로 이미 파싱돼 있으므로 여기서는 집계
    구조만 표시한다. build_member_targets_sql_candidate 가 이 플래그를 보고 2단계 SQL(지역별 X 수
    집계 상위 N → 그 지역 정상 회원 추출)을 생성한다. '지역/동네' 언급으로 잡힌 지역 모호성 정책
    (region_context_default)은 여기서 '거주 밀집 지역'으로 구체 해석됐으므로 소비한다(미소비 시
    semantic_resolutions 가 실DB 미지원 조건으로 남아 SQL 생성이 막힌다).
    """
    metric_info: dict[str, Any] | None = None
    matched_metric_text: str | None = None
    match = _REGION_DENSITY_PATTERN.search(query) or _REGION_DENSITY_ALT_PATTERN.search(query)
    if not match:
        # "<지표>가 높은/많은 지역" — 지표 레지스트리 기반 그룹 랭킹(예: 매출이 높은 지역).
        metric_pattern = _member_metric_region_pattern(str(DEFAULT_MEMBER_METRICS_PATH))
        metric_match = metric_pattern.search(query) if metric_pattern else None
        if not metric_match:
            return
        matched_metric_text = metric_match.group(1)
        metric_info = _member_metric_by_synonym(str(DEFAULT_MEMBER_METRICS_PATH), matched_metric_text)
        if metric_info is None:
            return
        match = metric_match
        granularity = metric_match.group(2)
    else:
        granularity = match.group(1)
    density_config = _region_density_config()
    granularity_columns = density_config.get("granularity_columns")
    granularity_columns = granularity_columns if isinstance(granularity_columns, dict) else {}
    column = granularity_columns.get(granularity) or density_config.get("default_column") or "SIGUNGU"
    top_n = int(density_config.get("default_top_n") or 5)
    top_match = _REGION_DENSITY_TOP_N_PATTERN.search(query)
    if top_match:
        max_top_n = int(density_config.get("max_top_n") or 30)
        top_n = max(1, min(int(next(group for group in top_match.groups() if group)), max_top_n))
    target = {"column": column, "granularity": granularity, "top_n": top_n}
    if metric_info is not None:
        target["metric_id"] = metric_info["metric_id"]
        target["metric_label"] = metric_info.get("ko_label", metric_info["metric_id"])
    plan["region_density_target"] = target
    plan["semantic_resolutions"] = [
        resolution
        for resolution in plan.get("semantic_resolutions", [])
        if resolution.get("policy_id") != "region_context_default"
    ]
    # '<지표> 높은 지역' 은 지역 랭킹이지 고객 단위 조건이 아니다. 같은 어구('매출이 높은')에
    # 얻어걸린 고객 단위 매출 정책(고매출 고객 threshold, 매출 상위 rank)이 남으면 threshold
    # clarification 으로 파이프라인이 막히므로, 지표어가 라벨에 포함된 target_user 정책을 소비한다.
    if matched_metric_text:
        plan["policy_constraints"] = [
            policy
            for policy in plan.get("policy_constraints", [])
            if not (
                policy.get("scope") == "target_user"
                and matched_metric_text in str(policy.get("ko_label", "")) + str(policy.get("canonical", ""))
            )
        ]


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
    is_cart = _is_cart_abandonment_query(query)
    if is_cart:
        _append_unique(plan["target_user"]["behaviors"], "cart_abandoner")
    if _is_repurchase_goal_context(query):
        plan["campaign_constraints"]["objective"] = "repurchase"
        # 장바구니 이탈 재구매 유도 흐름에서는 실제 타겟이 cart_abandoner 이고 repeat_buyer 는 목적
        # 라벨과 중복/모순이라 제거한다. 장바구니 맥락이 아니면 '재구매 고객'을 오디언스로 보고 주문
        # 집계 빌더(build_order_count_targets_sql_candidate: 주문 2건 이상)가 실추출하도록 남긴다.
        if is_cart:
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
    return any(keyword in compact_query for keyword in _lexicon_terms("cart_terms")) and any(
        keyword in compact_query for keyword in _lexicon_terms("cart_abandonment_terms")
    )


def _is_repurchase_goal_context(query: str) -> bool:
    compact_query = query.replace(" ", "").casefold()
    if not any(keyword in compact_query for keyword in _lexicon_terms("repurchase_terms")):
        return False
    return any(keyword in compact_query for keyword in _lexicon_terms("repurchase_outreach_terms"))


def _sanitize_purchase_object(value: str) -> str | None:
    tokens = []
    for token in re.findall(r"[0-9A-Za-z가-힣_+\-]+", value.casefold()):
        stripped_token = re.sub(r"(?:을|를)$", "", token)
        # 상품이 아닌 구매행동 수식어(첫/재/최근 구매 등)는 명사형 매칭에서 엉뚱한 LIKE 를 만들 수 있어 제외한다.
        if stripped_token and stripped_token not in {"사람", "고객", "사용자", "첫", "재", "최근", "최초", "최초로", "반복", "자주", "처음", "처음으로", "미"}:
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
    multi_query_variants: int | None = None,
) -> dict[str, Any]:
    timings_ms: dict[str, float] = {}
    retrieve_started_at = time.perf_counter()
    # 다중 재구성 파싱 변이 수. 명시값이 없으면 환경변수로 전역 설정(기본 0=끔). LLM(파서 auto/llm) 필요.
    if multi_query_variants is None:
        try:
            multi_query_variants = int(os.getenv("GRAPH_RAG_MULTI_QUERY_VARIANTS", "0"))
        except ValueError:
            multi_query_variants = 0
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

    # 파싱 전에 사용자 프롬프트를 타겟 조건 중심으로 재작성(룰/LLM)한다. 재작성본으로 파싱하되 원문은 보존한다.
    stage_started_at = time.perf_counter()
    prompt_normalization = normalize_prompt(query, parser=query_parser, llm_model=llm_model, prompt_dir=prompt_dir)
    effective_query = prompt_normalization["normalized"]
    timings_ms["prompt_normalization"] = _elapsed_ms(stage_started_at)

    # 타겟팅 스코프면 SQL·추론(Query Plan)을 오디언스(타겟팅) 절로만 수행한다. 채널·발송·혜택 문구는
    # 파싱에서 제외해 타겟 조건만 SQL/트레이스에 반영한다(검색 스코프 원칙을 파싱까지 확장). 채널 절은
    # 검색 스코프·메시지 생성에서만 쓰인다. 타겟팅 절이 비면 전체 재작성본으로 폴백한다.
    scope = (retrieval_scope or "all").casefold()
    if scope == "targeting":
        plan_scopes = split_prompt_scopes(effective_query, parser=query_parser, llm_model=llm_model, prompt_dir=prompt_dir)
        plan_query = (plan_scopes.get("targeting") or "").strip() or effective_query
    else:
        plan_query = effective_query

    stage_started_at = time.perf_counter()
    query_plan = build_query_plan(
        plan_query,
        normalization_rules=normalization_rules,
        business_policies=business_policies,
        metric_lexicon=metric_lexicon,
        sql_schema=sql_schema,
        parser=query_parser,
        llm_model=llm_model,
        prompt_dir=prompt_dir,
        multi_query_variants=multi_query_variants,
    )
    # OR(합집합) 은 재작성이 콤마로 뭉개므로 원본 프롬프트에서 top-level 합집합을 감지해 붙인다.
    # (값·임계값은 재작성본 기준으로 뽑힌 dimension_filters/aggregate_conditions 를 재사용한다.)
    _apply_union_condition(query, query_plan, normalization_rules)
    # 파싱에 실제 사용한 문장(타겟팅 절 또는 전체 재작성본)을 트레이스/응답에 노출한다.
    query_plan["planning_query"] = plan_query
    # 프롬프트 재작성기가 '많이 거주하는' 같은 집계 표현을 지울 수 있으므로(비결정적 LLM 재작성),
    # 파싱 문장 기준으로도 밀집 지역 타겟을 감지한다(이미 감지됐으면 동일 값으로 덮어써 무해).
    _apply_region_density_target(plan_query, query_plan)
    _apply_member_metric_ranking_target(plan_query, query_plan)
    # 타겟팅 스코프면 plan_query 가 오디언스 절뿐이라 '재구매를 유도' 같은 캠페인 목적 절이 잘려
    # intent 가 recommend_campaign→find_user_segment 로 약화된다(장바구니 이탈 재구매 유도 등).
    # 목적 절이 살아있는 전체 재작성본으로 intent 를 재추론해 더 강한 캠페인 의도로만 승격한다.
    if scope == "targeting":
        _upgrade_intent_from_effective_query(query_plan, effective_query)
    # 캠페인/조회 동사 없이 회원 속성만 나열한 프롬프트는 파서가 intent=unknown 을 주는데, 그러면
    # 회원 타겟 SQL 빌더가 호출되지 않는다. 실DB 매핑 가능한 타겟 신호가 있으면 세그먼트 조회로 승격.
    _promote_unknown_intent_for_target_signal(query_plan)
    timings_ms["query_plan"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    retrieval = query_plan["retrieval"]
    # 타겟팅 스코프면 Query Plan 자체가 타겟팅 절 기준이라 아래 검색어도 자연히 타겟팅 절이 된다.
    # (all/channel 스코프는 전체 문장 기준 Query Plan + 스코프별 검색어 분리 — 기존 동작 유지.)
    full_retrieval_query = retrieval["query"]
    keyword_query = " ".join(_unique_strings([full_retrieval_query, *retrieval["terms"]]))
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
        # 템플릿/조합 빌더가 못 만드는 형태는 LLM 폴백이 GraphRAG 컨텍스트를 근거로 SQL 초안을
        # 만들고 동일 가드 스택(guard/coverage/미언급)으로 검증한다. rules 파서 모드면 비활성.
        llm_model=llm_model if query_parser in ("auto", "llm") else None,
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
    """BM25 색인/질의용 토큰. 단어 토큰(정확 일치)에 더해, 한글은 교착어라 조사·어미가 붙어 정확
    토큰 일치가 깨지므로('결제수단으로'≠'결제수단') 인접 한글 문자 bigram 을 함께 색인해 변형을
    흡수한다. 질의·문서를 같은 방식으로 토큰화하므로, 정확 단어는 단어+bigram 양쪽으로 걸려 최상위
    점수를 유지하고, 조사/활용 변형은 공유 bigram 으로 부분 점수를 받는다(재현율↑, 정밀도는 idf 로 보정)."""
    tokens: list[str] = []
    for raw_token in re.findall(r"[0-9A-Za-z가-힣_]+", text.casefold()):
        parts = [raw_token, *raw_token.split("_")] if "_" in raw_token else [raw_token]
        for part in parts:
            if len(part) < 2:
                continue
            tokens.append(part)
            # 한글 인접쌍 bigram(3자 이상; 2자 토큰은 그 자체가 bigram 이라 중복 색인하지 않는다).
            # 혼합 토큰('sms수신동의여부')도 한글 구간만 bigram 처리한다.
            if len(part) >= 3:
                tokens.extend(
                    part[i:i + 2]
                    for i in range(len(part) - 1)
                    if _HANGUL_SYLLABLE.match(part[i]) and _HANGUL_SYLLABLE.match(part[i + 1])
                )
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
    # 각 노드까지의 '대표 경로'(점수 최고 seed에서 최단 경로)를 함께 보관해,
    # UI가 어떤 출발점에서 어떤 관계를 타고 확장됐는지 그대로 보여줄 수 있게 한다.
    best_paths: dict[str, list[str]] = {}
    seed_scores = {hit.node_id: hit.score for hit in hits}

    for hit in hits:
        if hit.node_id not in graph:
            continue
        # _length 대신 실제 경로를 받아, distance(=len(path)-1)와 확장 경로를 동시에 얻는다.
        paths = nx.single_source_shortest_path(graph, hit.node_id, cutoff=hops)
        for node_id, path in paths.items():
            distance = len(path) - 1
            graph_score = hit.score / (1 + distance * 0.35)
            if graph_score > scores.get(node_id, 0.0):
                scores[node_id] = graph_score
                best_paths[node_id] = path
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
                "path": _describe_path(graph, best_paths.get(node_id, [node_id])),
                "neighbors": _neighbor_summary(graph, node_id),
                "payload": _compact_payload(node_data["payload"]),
            }
        )
    return context


def _describe_path(graph: nx.Graph, path_ids: list[str]) -> list[dict[str, Any]]:
    """출발점(seed)→목표 노드까지의 경로를 관계명과 함께 사람이 읽을 수 있는 형태로 만든다.

    각 원소는 {id, title, type, relation}이며 relation 은 '직전 노드에서 이 노드로 온 엣지'의
    관계명(첫 노드=seed 는 None)이다. UI 브레드크럼(A ─relation→ B ─relation→ C)에 그대로 쓴다.
    """
    described: list[dict[str, Any]] = []
    previous_id: str | None = None
    for node_id in path_ids:
        node_data = graph.nodes[node_id]
        relation = None
        if previous_id is not None:
            edge_data = graph.get_edge_data(previous_id, node_id) or {}
            relation = edge_data.get("relation", "related")
        described.append(
            {
                "id": node_id,
                "title": node_data.get("title", node_id),
                "type": node_data.get("node_type", "unknown"),
                "relation": relation,
            }
        )
        previous_id = node_id
    return described


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

    # 신뢰도 리포트(전체/조건별 점수·근거·경고). 결과 화면에 그대로 노출할 수 있게 사람이 읽는
    # 텍스트로 미리 렌더해 프롬프트에 주입한다(LLM 이 점수를 임의로 만들지 않도록 값은 여기서 확정).
    confidence = sql_result.get("confidence")
    confidence_block = (
        render_confidence_report(confidence)
        if confidence
        else "신뢰도 정보 없음(검증된 SQL 이 없어 신뢰도를 산정하지 않았습니다)."
    )

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
            "",
            "[신뢰도]",
            "${confidence}",
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
        confidence=confidence_block,
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


def _condition_labels(conditions: list[dict[str, Any]]) -> list[str]:
    """조건 dict 목록에서 사람이 읽을 라벨 목록을 만든다(라벨 없으면 path 기반)."""
    labels = [condition.get("label") or _unsupported_condition_label(condition.get("path", "")) for condition in conditions]
    return _unique_strings([label for label in labels if label])


# 사용자 안내용 "지원 조건" 힌트. 지원 속성이 늘면 member_target_filters.json 에서 함께 갱신한다.
_SUPPORTED_CONDITION_HINT = str(
    _MEMBER_TARGET_FILTERS.get("supported_condition_hint")
    or _DEFAULT_MEMBER_TARGET_FILTERS["supported_condition_hint"]
)
# 실DB 미이관 데모 스키마 테이블. 이 테이블 참조로 가드 탈락하면 "조건이 실DB로 매핑 안 됨"을 뜻한다.
_DEMO_SCHEMA_TABLES = {
    "users", "recommendation_edges", "campaigns", "campaign_target_segments",
    "user_recent_behaviors", "user_interests", "campaign_keywords", "campaign_channels",
}


def _describe_sql_failure(query_plan: dict[str, Any], sql_result: dict[str, Any]) -> str:
    """검증 SQL 실패를 실패 유형별로 구체적으로 설명한다(어디서 왜 막혔는지 사용자가 알 수 있게)."""
    reason = sql_result.get("failure_reason")
    selected = sql_result.get("selected") or {}
    unsupported_labels = sql_result.get("unsupported_condition_labels", [])

    if reason == "query_plan_required_conditions_missing":
        questions = sql_result.get("clarification_questions") or []
        if questions:
            return "SQL 생성을 위해 조건 확인이 필요합니다: " + " / ".join(str(q) for q in questions)
        return f"SQL 생성을 위해 필요한 조건이 부족합니다. {_SUPPORTED_CONDITION_HINT} 같은 타겟 조건을 추가해 주세요."

    if unsupported_labels:
        # 요청 조건 중 실DB 타겟 추출로 아직 매핑되지 않은 것(관심사·행동·가격민감도 등)이 원인.
        return ("요청하신 조건 중 다음은 아직 실DB 타겟 추출로 지원되지 않아 검증 SQL을 만들지 못했습니다: "
                + ", ".join(unsupported_labels) + f". 지원되는 조건({_SUPPORTED_CONDITION_HINT})으로 바꾸거나 조합해 주세요.")

    if reason == "no_sql_candidates":
        return f"입력에서 타겟 조건을 찾지 못해 SQL을 만들지 못했습니다. {_SUPPORTED_CONDITION_HINT} 같은 타겟 조건을 넣어 주세요."

    if reason == "sql_guard_failed":
        issues = [issue for issue in selected.get("validation", {}).get("issues", []) if issue.get("severity") == "error"]
        disallowed = [issue.get("message", "").split(":")[-1].strip() for issue in issues if issue.get("code") == "table_not_allowed"]
        if disallowed and {table.casefold() for table in disallowed} & _DEMO_SCHEMA_TABLES:
            # 데모 스키마로만 생성됐다 = 요청 조건이 인식되지 않았거나 아직 실DB 회원 속성으로 매핑 안 됨.
            return (f"입력에서 실DB로 타겟을 추출할 수 있는 조건을 찾지 못했습니다. {_SUPPORTED_CONDITION_HINT} 같은 조건으로 다시 입력해 주세요. "
                    "(요청한 조건이 인식되지 않았거나, 아직 실DB 회원 속성으로 매핑되지 않는 조건입니다.)")
        if disallowed:
            return "생성된 SQL이 실DB에 없는 테이블(" + ", ".join(_unique_strings(disallowed)) + ")을 참조해 안전 검증에서 제외됐습니다."
        detail = "; ".join(issue.get("message", "") for issue in issues if issue.get("message"))
        return "생성된 SQL이 안전성 검증(SQL 가드)에서 막혔습니다" + (f": {detail}" if detail else "") + "."

    if reason == "query_plan_conditions_missing":
        missing = _condition_labels(selected.get("coverage", {}).get("missing_conditions", []))
        if missing:
            return "생성된 SQL이 요청 조건 중 다음을 SQL에 반영하지 못했습니다: " + ", ".join(missing) + "."
        return "생성된 SQL이 요청한 조건을 일부 반영하지 못했습니다."

    if reason == "intent_scope_mismatch":
        blocked = selected.get("intent_scope", {}).get("blocked_tables", [])
        suffix = f" (캠페인 추천 전용 테이블 사용: {', '.join(blocked)})" if blocked else ""
        return "생성된 SQL이 요청 의도(세그먼트 조회)와 맞지 않아 제외됐습니다" + suffix + "."

    if reason == "query_plan_unmentioned_conditions_added":
        added = _condition_labels(selected.get("unmentioned_conditions", {}).get("unexpected_conditions", []))
        if added:
            return "생성된 SQL에 요청하지 않은 조건이 포함돼 제외했습니다: " + ", ".join(added) + "."
        return "생성된 SQL에 요청하지 않은 조건이 포함돼 제외했습니다."

    return "현재 Query Plan 조건을 완전히 만족하는 검증된 SQL이 없습니다."


def build_recommendation_api_response(
    query: str,
    query_plan: dict[str, Any],
    sql_result: dict[str, Any],
    answer_response: dict[str, Any],
    message_generation: dict[str, Any] | None = None,
    prompt_normalization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unsupported_labels = sql_result.get("unsupported_condition_labels", [])
    dropped_labels = sql_result.get("dropped_condition_labels", [])
    if answer_response.get("content"):
        message = answer_response["content"]
    elif sql_result.get("is_success") and dropped_labels:
        # 부분 추출: 되는 조건으로 뽑되 실DB 미지원이라 뺀 조건을 함께 고지한다.
        message = "검증 SQL이 준비되었습니다. 단, 다음 조건은 실DB 타겟 추출로 지원되지 않아 제외했습니다: " + ", ".join(dropped_labels) + "."
    elif sql_result.get("is_success"):
        message = "Query Plan 조건을 만족하는 검증 SQL이 준비되었습니다."
    else:
        message = _describe_sql_failure(query_plan, sql_result)

    normalization = prompt_normalization or {"original": query, "normalized": query, "summary": "", "corrections": [], "mode": "noop"}
    response = {
        "status": _api_status(sql_result),
        "query": query,
        "normalized_query": normalization.get("normalized", query),
        # 화면 "타겟팅 프롬프트"용 오디언스-only 라벨. 비어 있으면 BFF 는 normalized_query 로 폴백한다.
        "targeting_label": normalization.get("targeting_label", ""),
        "prompt_summary": normalization.get("summary", ""),
        "prompt_corrections": normalization.get("corrections", []),
        "prompt_normalization_mode": normalization.get("mode"),
        "prompt_scopes": {
            "mode": query_plan.get("retrieval", {}).get("scope_mode"),
            "targeting": query_plan.get("retrieval", {}).get("targeting_query"),
            "channel": query_plan.get("retrieval", {}).get("channel_query"),
        },
        "intent": query_plan.get("intent"),
        "sql": sql_result.get("sql"),
        "target_connection": sql_result.get("target_connection"),
        "target_dialect": sql_result.get("target_dialect"),
        # 0명 결과일 때 실행부에서 어느 술어가 오디언스를 죽였는지 귀속하기 위한 술어별 probe.
        "cardinality_probe": sql_result.get("cardinality_probe"),
        # 생성 SQL 신뢰도(전체/조건별 점수·근거·경고) + 사람이 읽는 리포트 텍스트 + 프론트 노출용 마크다운.
        "confidence": sql_result.get("confidence"),
        "confidence_report": render_confidence_report(sql_result["confidence"]) if sql_result.get("confidence") else None,
        "confidence_markdown": render_confidence_markdown(sql_result["confidence"]) if sql_result.get("confidence") else None,
        "message": message,
        "missing_input_conditions": sql_result.get("missing_input_conditions", []),
        "clarification_questions": sql_result.get("clarification_questions", []),
        "unsupported_conditions": sql_result.get("unsupported_conditions", []),
        "unsupported_condition_labels": unsupported_labels,
        "dropped_conditions": sql_result.get("dropped_conditions", []),
        "dropped_condition_labels": dropped_labels,
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


@functools.lru_cache(maxsize=4)
def _schema_table_summaries(schema_path_text: str) -> tuple[str, ...]:
    """허용 테이블 전체의 한 줄 요약(빈 테이블 ⚠️ 경고 포함). LLM 폴백의 테이블 선택 근거.

    검색 히트만 주면 LLM 이 문서화된 함정(예: ODS_MALL_OMS_ORDER 0행 — anti-join 시 전원 매칭)을
    모른 채 그럴듯한 테이블을 고르므로, 카탈로그의 description_llm 을 전부 제공한다.
    """
    try:
        catalog = json.loads(Path(schema_path_text).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ()
    tables = catalog.get("tables", catalog) if isinstance(catalog, dict) else {}
    summaries = []
    for table_name, table_info in tables.items():
        if not isinstance(table_info, dict):
            continue
        description = str(table_info.get("description_llm") or "")[:220]
        join_hints = "; ".join(table_info.get("join_hints", [])[:2])
        line = f"[{table_name}] {description}"
        if join_hints:
            line += f" (조인: {join_hints})"
        summaries.append(line)
    return tuple(summaries)


def _inject_segment_label(sql: str, query_plan: dict[str, Any]) -> str:
    """LLM 생성 SQL 의 최상위 SELECT 에 조건 canonical 라벨 컬럼을 결정론적으로 주입한다.

    커버리지 검증은 조건 값 문자열이 SQL 에 존재하는지로 판정하므로(템플릿은 segment_label 로 충족),
    LLM 의 지시 순응에 기대지 않고 코드가 직접 주입한다. 이미 segment_label 이 있으면 건드리지 않는다.
    """
    target_user = query_plan.get("target_user", {})
    behaviors = _unique_strings([b for b in (target_user.get("behaviors") or []) if isinstance(b, str) and b])
    others = _unique_strings(
        [
            label
            for label in [*(target_user.get("lifecycle") or []), *(target_user.get("interests") or [])]
            if isinstance(label, str) and label
        ]
    )
    columns = []
    # behaviors 는 검증부가 target_segment 토큰까지 요구한다(cart 템플릿과 동일 규약의 별칭 사용).
    if behaviors and "target_segment" not in sql.casefold():
        columns.append(_sql_quote(",".join(behaviors)) + " AS target_segment")
    if others and "segment_label" not in sql.casefold():
        columns.append(_sql_quote(",".join(others)) + " AS segment_label")
    if not columns:
        return sql
    match = re.search(r"\bFROM\b", sql, re.IGNORECASE)
    if not match:
        return sql
    return sql[: match.start()].rstrip() + ", " + ", ".join(columns) + " " + sql[match.start():]


def _build_llm_sql_fallback_candidate(
    query: str,
    query_plan: dict[str, Any],
    context_nodes: list[dict[str, Any]],
    allowed_tables: Any,
    llm_model: str,
    schema_path: Path | None = None,
) -> dict[str, Any] | None:
    """템플릿/조합 빌더가 표현 못 하는 질의 형태의 SQL 초안을 LLM 으로 생성한다(2티어 폴백).

    근거는 GraphRAG 검색 컨텍스트(실스키마/조인힌트/값 노드/SQL 예시)로 한정하고, 결과는 호출부에서
    템플릿과 동일한 가드 스택(sql_guard 테이블 허용목록·SELECT 전용, 조건 커버리지, 미언급 조건
    차단)을 전부 통과해야만 채택된다 — 가드는 허용 위반은 잡지만 '그럴듯하게 틀린 로직'(조인 중복
    집계 등)은 못 잡으므로, 생성 SQL 은 source=llm_generated 로 명시 라벨링해 응답에 노출하고
    로그를 남겨 반복 성공 형태의 템플릿 승격 근거로 쓴다.
    """
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI

        context_lines = []
        for node in context_nodes[:12]:
            text = node.get("text") or node.get("text_for_embedding") or ""
            if text:
                context_lines.append(f"[{node.get('type', 'node')}] {text[:600]}")
        table_summaries = list(_schema_table_summaries(str(schema_path))) if schema_path else []
        allowed_list = ", ".join(sorted(str(table) for table in allowed_tables))
        plan_slim = {
            key: query_plan.get(key)
            for key in ("intent", "target_user", "exclude", "campaign_constraints", "dimension_filters", "region_density_target")
            if query_plan.get(key)
        }
        system_prompt = "\n".join(
            [
                "너는 CRM 타겟팅 SQL 생성기다. 반드시 JSON {\"sql\": \"...\", \"explanation\": \"...\"} 형식으로만 답한다.",
                "규칙:",
                f"- MSSQL(T-SQL) SELECT 단일문만 생성한다. DML/DDL/임시테이블 금지, LIMIT 대신 TOP 사용.",
                f"- 허용 테이블만 사용한다: {allowed_list}",
                "- 첫 컬럼은 반드시 회원키다: SELECT DISTINCT B.MEMBER_NO AS CUST_ID (CRM_MB_BASEINFO 별칭 B).",
                "- 발송 대상이므로 기본으로 B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL' 조건을 넣는다(사용자가 휴면/탈퇴를 명시하면 예외).",
                "- CRMDW 코드 컬럼 저장값은 도메인 접두어를 포함한다(예: GENDER_CD.FEMALE, MEM_GRADE_CD.VIP, MEMBER_STATE_CD.NORMAL).",
                "- 사용자가 명시한 조건은 모두 WHERE 에 반영하고, 명시하지 않은 조건(성별/연령/지역 등)은 절대 추가하지 않는다.",
                "- SELECT 에 반영한 조건의 canonical 요약 라벨을 포함한다(조건 커버리지 검증용): 예) 'no_purchase' AS segment_label.",
                "- 컨텍스트에 없는 테이블/컬럼을 지어내지 않는다. 확실한 SQL 을 만들 수 없으면 {\"sql\": null, \"explanation\": \"이유\"} 를 반환한다.",
                "- 테이블 요약의 ⚠️(0행/미적재) 경고가 있는 테이블은 조건 판정 기준으로 쓰지 않는다(빈 테이블 anti-join 은 전원 매칭 오류).",
            ]
        )
        user_prompt = json.dumps(
            {
                "user_query": query,
                "query_plan": plan_slim,
                "table_catalog": table_summaries,
                "retrieval_context": context_lines,
            },
            ensure_ascii=False,
        )
        _write_rag_llm_log(
            "llm_sql_fallback_request",
            {"model": llm_model, "query": query, "query_plan": plan_slim, "context_line_count": len(context_lines)},
        )
        client = OpenAI()
        response = client.chat.completions.create(
            model=llm_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        content = response.choices[0].message.content or "{}"
        payload = json.loads(content)
        _write_rag_llm_log("llm_sql_fallback_response", {"model": llm_model, "query": query, "content": content})
        sql = payload.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return None
        # 조건 커버리지 검증용 라벨을 결정론적으로 주입(LLM 지시 순응에 기대지 않는다).
        sql = _inject_segment_label(sql.strip(), query_plan)
        candidate = _sql_candidate(
            "llm_sql:fallback",
            "LLM 생성 SQL(템플릿 미지원 형태 — 가드 검증 통과 시에만 채택)",
            0.5,
            sql,
            _template_tables(sql),
            "llm_generated",
        )
        candidate["explanation"] = payload.get("explanation")
        candidate["dropped_conditions"] = []
        candidate["dropped_condition_labels"] = []
        return candidate
    except Exception as exc:  # LLM 폴백 실패는 기존 실패 흐름(정직한 거절)으로 되돌아간다.
        _write_rag_llm_log("llm_sql_fallback_error", {"model": llm_model, "query": query, "error": str(exc)})
        return None


def build_sql_result(
    graph: nx.Graph,
    query: str,
    query_plan: dict[str, Any],
    context_nodes: list[dict[str, Any]],
    schema_path: Path,
    default_limit: int,
    candidate_limit: int = 20,
    llm_model: str | None = None,
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
            "llm_fallback_used": False,
            "generation_source": None,
            "is_success": False,
            "failure_reason": "query_plan_required_conditions_missing",
        }

    allowed_tables = load_allowed_tables(schema_path)
    table_dialects = load_table_dialects(schema_path)
    table_databases = load_table_databases(schema_path)
    template_candidate = build_sql_template_candidate(query_plan)
    candidates = [template_candidate] if template_candidate is not None else []

    # 2티어 폴백: 결정론 템플릿/조합 빌더가 후보를 못 만든 타겟팅 질의만 LLM 생성으로 시도한다.
    # 생성 SQL 도 아래 루프에서 템플릿과 동일한 가드 스택으로 검증되며, 실패하면 기존 거절 흐름 유지.
    llm_fallback_used = False
    if not candidates and llm_model and query_plan.get("intent") in ("recommend_campaign", "find_user_segment"):
        llm_candidate = _build_llm_sql_fallback_candidate(
            query, query_plan, context_nodes, allowed_tables, llm_model, schema_path=schema_path
        )
        if llm_candidate is not None:
            candidates = [llm_candidate]
            llm_fallback_used = True

    validated_candidates = []
    for candidate in candidates:
        # 타겟 오디언스는 전체가 나와야 하므로 행수 제한(LIMIT/TOP)을 붙이지 않는다.
        validation = validate_sql(
            candidate["sql"],
            allowed_tables=allowed_tables,
            default_limit=None,
            table_dialects=table_dialects,
        )
        # 부분 추출 candidate 가 실DB 미지원이라 뺀 조건은 커버리지 요구에서 제외한다(대신 응답에 고지).
        dropped_paths = {path.split(":")[0] for path in candidate.get("dropped_conditions", [])}
        effective_required = [condition for condition in required_conditions if condition["path"] not in dropped_paths]
        coverage = validate_sql_condition_coverage(candidate["sql"], effective_required)
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
    if selected_sql is None and query_plan.get("intent") in ("recommend_campaign", "find_user_segment"):
        unsupported_conditions = compile_member_target_conditions(query_plan)["unsupported"]
        unsupported_condition_labels = [_unsupported_condition_label(path) for path in unsupported_conditions]
        # 데모 폴백 제거 후, 매핑 불가 조건은 후보 자체가 없어(no_sql_candidates) 되기도 한다. 둘 다 승격.
        # LLM 폴백 후보가 검증(커버리지 등)에서 탈락한 경우도 미지원 조건이 원인이면 같은 안내로 승격.
        promotable_reasons = ("sql_guard_failed", "no_sql_candidates")
        if llm_fallback_used:
            promotable_reasons += ("query_plan_conditions_missing", "query_plan_unmentioned_conditions_added")
        if failure_reason in promotable_reasons and unsupported_conditions:
            failure_reason = "real_db_unsupported_conditions"

    # 부분 추출로 SQL 이 나온 경우, 실DB 미지원이라 뺀 조건을 고지한다(성공이지만 일부 조건 제외).
    dropped_conditions = selected.get("dropped_conditions", []) if selected else []
    dropped_condition_labels = selected.get("dropped_condition_labels", []) if selected else []

    # 생성된 SQL 에 대한 결정론 신뢰도 산정(0~100, 조건별 근거 포함). 실패해도 SQL 생성엔 영향 없음.
    confidence = None
    if selected_sql is not None and selected is not None:
        try:
            confidence = score_targeting_confidence(query_plan, selected, context_nodes, schema_path=schema_path)
        except Exception:
            confidence = None

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
        "dropped_conditions": dropped_conditions,
        "dropped_condition_labels": dropped_condition_labels,
        # LLM 폴백으로 생성·검증된 SQL 인지 명시 라벨(응답/UI 에서 결정론 템플릿과 구분).
        "llm_fallback_used": llm_fallback_used,
        "generation_source": (selected or {}).get("source"),
        # 실행부(0명 결과)에서 술어별 카디널리티 진단을 돌릴 수 있게 선택된 후보의 probe 를 노출.
        "cardinality_probe": (selected or {}).get("cardinality_probe") if selected_sql is not None else None,
        # 생성 SQL 의 결정론 신뢰도(전체/조건별 점수·근거·경고).
        "confidence": confidence,
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


# 집합식 피연산자로 온 "디멘션 레벨" canonical(회원등급/지역 등)을 데모 users 스키마 조건으로 해석한다.
# 정규화 사전은 값(vip/서울)이 아니라 디멘션(member_grade/지역)을 canonical 로 내주기도 하는데, 이때
# 구체 값은 canonical 이름이나 operand 의 표면형 필드(value/text/matched_text/label)에 실려 온다.
# 값을 복원하지 못하면 하드 실패("컴파일 불가") 대신 "무슨 값인지" 되묻는 clarification 이슈로 돌려준다.
_GRADE_DIMENSION_CANONICALS = {"member_grade", "vip등급", "grade", "tier", "등급", "회원등급", "membership grade"}
_REGION_DIMENSION_CANONICALS = {"지역", "region", "area", "시도", "시군구", "sido", "sigungu"}
# 등급 표면형 -> u.lifecycle 저장값(존재는 LIFECYCLE_TERMS 로 재검증). 긴 표기를 먼저 본다.
_GRADE_SURFACE_TO_VALUE = (
    ("vvip", "vip"), ("vip", "vip"), ("브이아이피", "vip"),
    ("gold", "gold_grade"), ("골드", "gold_grade"),
    ("silver", "silver_grade"), ("실버", "silver_grade"),
    ("family", "family_grade"), ("패밀리", "family_grade"),
    ("welcome", "welcome_grade"), ("웰컴", "welcome_grade"),
)


def _set_operand_surface_terms(operand: dict[str, Any]) -> list[str]:
    """operand 에서 값 복원에 쓸 표면형 문자열을 우선순위대로 모은다(값 필드 우선, canonical 최후)."""
    terms: list[str] = []
    for key in ("value", "text", "matched_text", "label", "canonical"):
        value = operand.get(key)
        if isinstance(value, str) and value.strip():
            terms.append(value.strip())
    return terms


def _compile_grade_dimension_operand(operand: dict[str, Any], canonical: Any) -> dict[str, Any] | None:
    """회원등급 디멘션 operand를 u.lifecycle 등가 조건으로 컴파일한다(비해당이면 None)."""
    if str(canonical).casefold() not in _GRADE_DIMENSION_CANONICALS:
        return None
    joined = " ".join(_set_operand_surface_terms(operand)).casefold()
    for surface, value in _GRADE_SURFACE_TO_VALUE:
        if surface in joined and value in LIFECYCLE_TERMS:
            return {"is_valid": True, "expression_sql": "u.lifecycle = " + _sql_quote(value), "issues": []}
    return {"is_valid": False, "expression_sql": "", "issues": ["어떤 회원 등급인지 지정해 주세요(예: VIP·골드·실버): " + str(canonical)]}


def _compile_region_dimension_operand(operand: dict[str, Any], canonical: Any) -> dict[str, Any] | None:
    """지역 디멘션 operand를 u.region 등가 조건으로 컴파일한다(비해당이면 None)."""
    if str(canonical).casefold() not in _REGION_DIMENSION_CANONICALS:
        return None
    region = _region_value_from_surface(operand, canonical)
    if region is None:
        return {"is_valid": False, "expression_sql": "", "issues": ["어느 지역인지 지정해 주세요(예: 서울): " + str(canonical)]}
    return {"is_valid": True, "expression_sql": "u.region = " + _sql_quote(region), "issues": []}


def _region_value_from_surface(operand: dict[str, Any], canonical: Any) -> str | None:
    """operand 표면형에서 구체 지역명을 복원한다(거주/행정단위 접미어 제거, 디멘션 단어 자체는 제외)."""
    canonical_fold = str(canonical).casefold()
    for term in _set_operand_surface_terms(operand):
        cleaned = re.sub(r"\s*(?:에\s*)?(?:거주(?:하는)?|사는|살고\s*있는)\s*", "", term).strip()
        cleaned = re.sub(r"(?:특별자치시|특별자치도|특별시|광역시|자치도|시|도|지역)\s*$", "", cleaned).strip()
        if not cleaned or cleaned.casefold() in _REGION_DIMENSION_CANONICALS or cleaned.casefold() == canonical_fold:
            continue
        return cleaned
    return None


# 시도(광역) 값 표면형 -> 데모 users.region 저장값(짧은 시도명). 17개 시도. 경계검사(_value_token_mentioned)로
# 부분문자열 오탐('경기'≠'경기침체')을 막는다. 실DB SIDO 타겟팅은 member_value_index 가 담당하고(별개 스키마),
# 여기 리스트는 집합식(데모 users.region) 경로 전용이다.
_REGION_VALUE_SURFACES = (
    ("서울특별시", "서울"), ("서울", "서울"), ("부산", "부산"), ("대구", "대구"), ("인천", "인천"),
    ("광주", "광주"), ("대전", "대전"), ("울산", "울산"), ("세종", "세종"), ("경기", "경기"),
    ("강원", "강원"), ("충북", "충북"), ("충남", "충남"), ("전북", "전북"), ("전남", "전남"),
    ("경북", "경북"), ("경남", "경남"), ("제주", "제주"),
)


def _region_value_from_query(query: str) -> str | None:
    for surface, value in _REGION_VALUE_SURFACES:
        if _value_token_mentioned(surface, query):
            return value
    return None


def _grade_value_from_query(query: str) -> str | None:
    for surface, value in _GRADE_SURFACE_TO_VALUE:
        if value in LIFECYCLE_TERMS and _value_token_mentioned(surface, query):
            return value
    return None


def _iter_set_ast_operands(ast: Any):
    """set_ast 를 재귀 순회하며 operand 노드(dict)를 그대로 내준다(호출부가 in-place 로 값 보강)."""
    if not isinstance(ast, dict):
        return
    if ast.get("type") == "operand":
        yield ast
    yield from _iter_set_ast_operands(ast.get("left"))
    yield from _iter_set_ast_operands(ast.get("right"))


def _enrich_set_expression_operand_values(plan: dict[str, Any], query: str) -> None:
    """집합식 operand 가 디멘션(지역/등급)만 있고 값이 없으면 프롬프트에서 값을 복원해 실어준다.

    rules/LLM 어느 경로가 만든 set_ast 든 동일하게 적용된다. 재작성·정규화가 "서울 거주"를 값 없는 `지역`
    operand 로 뭉개 컴파일러가 "어느 지역인지 지정" 만 되묻던 문제를, 프롬프트 원문에서 시도값을 경계검사로
    복원해 operand.value 로 채워 u.region 조건까지 이어지게 한다(멱등 — 이미 값이 있으면 건드리지 않는다).
    """
    region_value: str | None = None
    grade_value: str | None = None
    for expression in plan.get("set_expressions", []):
        for operand in _iter_set_ast_operands(expression.get("set_ast") if isinstance(expression, dict) else None):
            canonical = operand.get("canonical")
            canonical_fold = str(canonical).casefold()
            if canonical_fold in _REGION_DIMENSION_CANONICALS:
                if _region_value_from_surface(operand, canonical) is not None:
                    continue  # 이미 값이 있음
                if region_value is None:
                    region_value = _region_value_from_query(query)
                if region_value is not None:
                    operand["value"] = region_value
            elif canonical_fold in _GRADE_DIMENSION_CANONICALS:
                joined = " ".join(_set_operand_surface_terms(operand)).casefold()
                if any(surface in joined for surface, _ in _GRADE_SURFACE_TO_VALUE):
                    continue  # 이미 등급값이 표면형에 있음(예: VIP등급)
                if grade_value is None:
                    grade_value = _grade_value_from_query(query)
                if grade_value is not None:
                    operand["value"] = grade_value


def _set_ast_has_unknown_operand(ast: Any) -> bool:
    if not isinstance(ast, dict):
        return False
    if ast.get("type") == "unknown_operand":
        return True
    return _set_ast_has_unknown_operand(ast.get("left")) or _set_ast_has_unknown_operand(ast.get("right"))


def _drop_uncompilable_set_expressions(plan: dict[str, Any]) -> None:
    """(값 보강 후에도) 컴파일되지 않는 '리던던트' 집합식을 버린다 — source 무관.

    LLM 이든(잘못 감싼 AND 나열) rules 파서든(재작성문이 '구매금액' 같은 지표/디멘션 canonical 을 집합식
    operand 로 매칭) 인식된-canonical 이지만 집합식 컴파일러가 지원하지 않는 operand(구매금액/지역/등급 등)를
    넣으면 SQL 이 통째로 막힌다. 이런 조건은 결정론 필터(집계/디멘션/회원)가 이미 커버하므로 집합식을 버려
    막지 않게 한다. 단, 정규화 못한 값(unknown_operand)이나 set_ast 자체가 없는 경우는 진짜 clarification
    이므로 유지한다. 반드시 값 보강(_enrich_set_expression_operand_values) 이후에 호출해 지역/등급 operand 를
    성급히 버리지 않는다(값이 채워지면 컴파일되어 유지됨)."""
    expressions = plan.get("set_expressions")
    if not isinstance(expressions, list) or not expressions:
        return
    kept: list[dict[str, Any]] = []
    for expression in expressions:
        ast = expression.get("set_ast")
        if not isinstance(ast, dict) or _set_ast_has_unknown_operand(ast) or _compile_set_expression_ast(ast)["is_valid"]:
            kept.append(expression)  # 파서 clarification / 미정규화 값 / 컴파일 가능 → 유지
    plan["set_expressions"] = kept


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
    grade_predicate = _compile_grade_dimension_operand(operand, canonical)
    if grade_predicate is not None:
        return grade_predicate
    region_predicate = _compile_region_dimension_operand(operand, canonical)
    if region_predicate is not None:
        return region_predicate
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


def _attach_cart_dropped_conditions(
    candidate: dict[str, Any], query_plan: dict[str, Any], compiled: dict[str, Any]
) -> None:
    """cart 템플릿용 부분 추출 고지(형제 빌더와 동일 규칙). 장바구니 행동(cart_abandoner)은 템플릿
    자체가 커버하므로 behaviors 가 그것뿐이면 dropped 에서 제외한다(purchase_object 처리와 같은 방식)."""
    behaviors = set(query_plan.get("target_user", {}).get("behaviors", []))
    dropped = [
        path
        for path in compiled["unsupported"]
        if not (path == "target_user.behaviors" and behaviors <= {"cart_abandoner"})
    ]
    candidate["dropped_conditions"] = dropped
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in dropped]


def build_sql_template_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    intent = query_plan.get("intent")
    if intent == "recommend_campaign":
        union_candidate = build_union_targets_sql_candidate(query_plan)
        if union_candidate is not None:
            # "A 이거나 B 또는 C" 합집합(OR) 조건은 실CRM 한 쿼리에서 OR 로 묶어 추출한다(AND 폴백 방지).
            return union_candidate
        brand_filter = _cart_dimension_brand_filter(query_plan)
        if brand_filter is not None:
            # 장바구니에 특정 상품브랜드(BRAND_ID) 상품을 담은 회원 추출(실제 CRMDW 테이블).
            # 브랜드명은 dimension_catalog 스냅샷으로 이미 코드(예: 'A')로 해석돼 넘어온다.
            # 회원 속성(성별/연령/등급 등)이 함께 오면 형제 빌더와 동일하게 B 술어로 AND 결합하고,
            # 실DB 미지원 조건은 dropped 로 고지한다 — 장바구니 경로만 조건을 조용히 버리지 않게.
            compiled = compile_member_target_conditions(query_plan)
            column_short = brand_filter.get("column", "CRM_CM_PRODUCT.BRAND_ID").split(".")[-1]
            operator = brand_filter.get("operator", "IN")
            in_list = ", ".join(_sql_quote(code) for code in brand_filter["codes"])
            where_clauses = ["A.KEEP_YN = 'Y'", f"C.{column_short} {operator} ({in_list})", *compiled["predicates"]]
            # 회원상태 직접 지정(dormant 등)이 아니면 발송 대상 기본 정책대로 정상 회원으로 한정한다.
            if not compiled["forces_state"]:
                where_clauses.append(_member_active_state_predicate())
            select_columns = ["DISTINCT B.MEMBER_NO AS CUST_ID"]
            if "cart_abandoner" in query_plan.get("target_user", {}).get("behaviors", []):
                select_columns.append("'cart_abandoner' AS target_segment")
            objective = query_plan.get("campaign_constraints", {}).get("objective")
            if objective:
                select_columns.append(_sql_quote(objective) + " AS objective")
            sql = "\n".join(
                [
                    "SELECT " + ", ".join(select_columns),
                    "FROM ODS_MALL_OMS_CART A",
                    "     INNER JOIN CRM_MB_BASEINFO B ON A.CART_ID = B.MEMBER_ID",
                    "     INNER JOIN CRM_CM_PRODUCT C ON A.PRODUCT_ID = C.PRODUCT_ID",
                    "WHERE " + "\n  AND ".join(_unique_strings(where_clauses)),
                ]
            )
            candidate = _sql_candidate("sql_template:cart_dimension_targets", "장바구니 상품브랜드 타겟팅 SQL 템플릿", 1.0, sql, _template_tables(sql), "sql_template")
            _attach_cart_dropped_conditions(candidate, query_plan, compiled)
            return candidate
        if _should_use_cart_repurchase_template(query_plan):
            # 타겟은 "장바구니에 담고 아직 결제 안 함"(카트 이탈)뿐 — KEEP_YN='Y'가 미결제 보관 상태를 표현한다.
            # 재구매(objective)는 메시지 목적 라벨일 뿐 타겟 필터가 아니므로, 회원 단위 주문 anti-join은 걸지 않는다.
            #   (NOT EXISTS(모든 주문)은 "평생 무주문 회원"을 뜻해 재구매 대상과 자기모순이라 제거했다.)
            # 라벨 컬럼(target_segment/objective)은 세그먼트·목적 태그이자 조건 커버리지 충족용(값은 query_plan 기준).
            # 회원 속성이 함께 오면 형제 빌더와 동일하게 B 술어로 AND 결합한다(조용한 누락 방지).
            compiled = compile_member_target_conditions(query_plan)
            objective = query_plan.get("campaign_constraints", {}).get("objective")
            select_columns = ["B.MEMBER_NO AS CUST_ID", "'cart_abandoner' AS target_segment"]
            if objective:
                select_columns.append(_sql_quote(objective) + " AS objective")
            where_clauses = ["A.KEEP_YN = 'Y'", *compiled["predicates"]]
            if not compiled["forces_state"]:
                where_clauses.append(_member_active_state_predicate())
            sql = "\n".join(
                [
                    "SELECT DISTINCT " + ", ".join(select_columns),
                    "FROM ODS_MALL_OMS_CART A",
                    "     INNER JOIN CRM_MB_BASEINFO B ON A.CART_ID = B.MEMBER_ID",
                    "WHERE " + "\n  AND ".join(_unique_strings(where_clauses)),
                ]
            )
            candidate = _sql_candidate("sql_template:cart_repurchase_targets", "장바구니 미결제 재구매 유도 SQL 템플릿(CRMDW)", 1.0, sql, _template_tables(sql), "sql_template")
            _attach_cart_dropped_conditions(candidate, query_plan, compiled)
            return candidate
        purchase_candidate = build_purchase_history_targets_sql_candidate(query_plan)
        if purchase_candidate is not None:
            # "…를 구매/구입한 고객" 은 실주문(CRM_SL_ORDERDETAILMALL) 조인으로 상품 구매 이력 회원을 뽑는다.
            # 회원 속성(성별/연령/등급 등)이 함께 있으면 CRM_MB_BASEINFO 술어로 같은 SQL 에 결합한다.
            return purchase_candidate
        order_count_candidate = build_order_count_targets_sql_candidate(query_plan)
        if order_count_candidate is not None:
            # "첫 구매/재구매/무구매 고객" 은 주문 헤더(CRM_SL_ORDERHEADERMALL)를 회원별로 집계해 뽑는다.
            return order_count_candidate
        aggregate_candidate = build_aggregate_targets_sql_candidate(query_plan)
        if aggregate_candidate is not None:
            # "최근 N일 누적 구매 금액 X원 이상" 등 집계 임계값 조건은 주문 테이블 회원별 집계로 뽑는다.
            return aggregate_candidate
        metric_ranking_candidate = build_member_metric_ranking_sql_candidate(query_plan)
        if metric_ranking_candidate is not None:
            # "매출/누적 구매금액이 높은 고객 상위 N명" 은 지표 테이블(CRM_MB_MONTHCRMINFO)을 회원키로
            # 조인해 지표값 내림차순 상위 N 명을 뽑는다(회원 속성이 있으면 같은 SQL 에 AND 결합).
            return metric_ranking_candidate
        member_candidate = build_member_targets_sql_candidate(query_plan)
        if member_candidate is not None:
            # 성별/연령/등급/휴면/장기미접속/앱 등 회원 속성 타겟은 실회원 테이블(CRMDW CRM_MB_BASEINFO)로 뽑는다.
            return member_candidate
        # 실DB(cart/purchase/order/member) 로 매핑 가능한 조건이 없으면 후보를 만들지 않는다(None → 미지원 안내).
        return None
    if intent == "find_user_segment":
        union_candidate = build_union_targets_sql_candidate(query_plan)
        if union_candidate is not None:
            # "A 이거나 B 또는 C" 합집합(OR) 조건 세그먼트 조회도 실CRM 한 쿼리에서 OR 로 묶어 추출한다.
            return union_candidate
        # "…를 구매/구입한 고객 찾아줘" 처럼 캠페인 발송이 아니라 세그먼트 조회여도 상품 구매 이력은
        # 동일한 실주문 조인으로 실회원 오디언스를 추출한다(recommend_campaign 과 같은 템플릿).
        purchase_candidate = build_purchase_history_targets_sql_candidate(query_plan)
        if purchase_candidate is not None:
            return purchase_candidate
        order_count_candidate = build_order_count_targets_sql_candidate(query_plan)
        if order_count_candidate is not None:
            # "첫 구매/재구매/무구매 고객 찾아줘" 세그먼트 조회도 주문 집계로 실회원을 추출한다.
            return order_count_candidate
        aggregate_candidate = build_aggregate_targets_sql_candidate(query_plan)
        if aggregate_candidate is not None:
            # "최근 N일 누적 구매 금액 X원 이상 고객 찾아줘" 세그먼트 조회도 주문 집계로 실회원을 추출한다.
            return aggregate_candidate
        metric_ranking_candidate = build_member_metric_ranking_sql_candidate(query_plan)
        if metric_ranking_candidate is not None:
            # "매출/누적 구매금액이 높은 고객" 세그먼트 조회도 지표 테이블 조인 상위 N 랭킹으로 추출한다.
            return metric_ranking_candidate
        member_candidate = build_member_targets_sql_candidate(query_plan)
        if member_candidate is not None:
            # 성별/연령/등급/휴면·장기 미접속 등 회원 속성 세그먼트 조회도 실회원 테이블(CRMDW
            # CRM_MB_BASEINFO)로 뽑는다.
            return member_candidate
        # 실컬럼으로 매핑 가능한 조건이 없으면 후보를 만들지 않는다(None → 미지원 안내).
        return None
    return None


_UNSUPPORTED_CONDITION_LABELS = {
    "target_user.gender": "성별 조건",
    "target_user.interests": "관심사 조건",
    "target_user.preferred_channels": "선호 채널 조건",
    "target_user.behaviors": "행동 조건",
    "target_user.purchase_object": "구매 상품 조건",
    "target_user.aggregate_conditions": "집계 조건(구매 금액/횟수 임계값)",
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
    "campaign_constraints.target_segment": "타겟 세그먼트 조건",
    "target_user.age_min": "최소 연령 조건",
    "target_user.age_max": "최대 연령 조건",
    "target_user.age_range": "연령대 조건",
}


def _unsupported_condition_label(path: str) -> str:
    """미지원 조건 path 를 사람이 읽을 라벨로 바꾼다(예: 'exclude.lifecycle:new_user' -> '생애주기 제외 조건: new_user')."""
    base, _, value = path.partition(":")
    label = _UNSUPPORTED_CONDITION_LABELS.get(base, base)
    return f"{label}: {value}" if value else label


def _member_region_predicates(region_codes: dict[str, list[str]]) -> list[str]:
    """지역 컬럼(SIDO/SIGUNGU) 조건의 결합 방식을 행정 계층 데이터로 판별해 술어 목록을 만든다.

    두 컬럼이 함께 잡혔을 때: 언급된 모든 시군구가 언급된 시도 소속이면 '인천 서구' 같은 수식
    관계로 보고 AND(각각 별도 술어), 하나라도 소속이 아니면 '금천구랑 인천' 같은 지역 나열로 보고
    OR(단일 괄호 술어)로 묶는다 — 나열을 AND 로 붙이면 존재하지 않는 조합(인천의 금천구)이 되어
    조용히 0명이 추출되는 오류를 막는다. 소속 판별 근거는 member_value_index 의 region_hierarchy
    (주소 마스터 스냅샷)이며, 계층 정보가 없으면 기존 동작(AND)을 유지한다.
    """
    if not region_codes:
        return []

    def _in_predicate(column: str) -> str:
        return "B." + column + " IN (" + ", ".join(_sql_quote(code) for code in region_codes[column]) + ")"

    sido_codes = region_codes.get("SIDO")
    sigungu_codes = region_codes.get("SIGUNGU")
    if not sido_codes or not sigungu_codes:
        return [_in_predicate(column) for column in region_codes]

    index = _load_member_value_index(str(DEFAULT_MEMBER_VALUE_INDEX_PATH)) or {}
    sigungu_to_sido = index.get("region_hierarchy", {}).get("sigungu_to_sido", {})
    hierarchical = bool(sigungu_to_sido) and all(
        any(sido in sido_codes for sido in sigungu_to_sido.get(sigungu, []))
        for sigungu in sigungu_codes
    )
    if hierarchical or not sigungu_to_sido:
        return [_in_predicate("SIDO"), _in_predicate("SIGUNGU")]
    return ["(" + _in_predicate("SIDO") + " OR " + _in_predicate("SIGUNGU") + ")"]


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
        if lifecycle == "new_user":
            continue  # 신규 가입은 아래 signup_target 분기가 REG_DT 창 술어로 처리(미지원 아님)
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

    # 미접속 기간(휴면/장기 미접속): LAST_LOGIN_DATE(YYYYMMDD 문자열) 사전식 비교 술어로 컴파일한다.
    # 회원상태는 기본값(정상 회원 한정)을 유지한다 — 법적 휴면(SLEEP)·탈퇴(WITHDRAW) 계정은 발송 대상에서
    # 빼고, "장기 미접속 정상 회원"을 재활성화 오디언스로 본다.
    inactivity_period = target_user.get("inactivity_period")
    if isinstance(inactivity_period, dict) and isinstance(inactivity_period.get("min_days"), int):
        other_predicates.append(_member_activity_predicate(inactivity_period["min_days"])); has_signal = True

    # 생일 타겟(BIRTHDAY 월일 비교; '이달 생일'은 월 비교). 년도는 비교하지 않는다.
    birthday_target = target_user.get("birthday_target")
    if isinstance(birthday_target, dict):
        granularity = "month" if birthday_target.get("granularity") == "month" else "day"
        other_predicates.append(_member_birthday_predicate(granularity)); labels.append("birthday_" + granularity); has_signal = True

    # 신규 가입 타겟(REG_DT 최근 N일 창). signup_target(창 파싱) 또는 lifecycle 'new_user'(LLM 라벨)
    # 어느 쪽이든 트리거하고 하나의 술어로 합친다. 창은 signup_target.days > default_days 순으로 결정.
    signup_target = target_user.get("signup_target")
    if isinstance(signup_target, dict) or "new_user" in (target_user.get("lifecycle") or []):
        days = signup_target.get("days") if isinstance(signup_target, dict) else None
        other_predicates.append(_member_signup_predicate(days if isinstance(days, int) else None))
        labels.append("new_user"); has_signal = True

    # 회원 테이블 디멘션 필터(예: 시도 → CRM_MB_BASEINFO.SIDO IN ('서울')). dimension_catalog 로 값이
    # 이미 코드로 해석돼 넘어오고, 회원 기본정보 단독 컬럼이라 조인 없이 술어로 AND 결합한다.
    # 지역 컬럼(SIDO/SIGUNGU)은 같은 '거주 지역' 도메인이라 별도 수집 후 나열(OR)/수식(AND)을 판별한다.
    # 보조 속성 테이블 필터(join_column 지정, 예: ODS_MALL_MMS_MEMBER_ZTS.JOB_CD)는 회원키 서브쿼리
    # (B.<join> IN (SELECT <join> FROM <표> WHERE <컬럼> IN ...))로 결합한다 — 값 인덱스가 채워지면
    # 코드 수정 없이 자동으로 이 경로를 탄다. (dimension_id 별 필터는 각각 술어가 되어 자동 조합.)
    member_region_codes: dict[str, list[str]] = {}
    for dimension_filter in query_plan.get("dimension_filters", []):
        table_name = dimension_filter.get("table")
        join_column = dimension_filter.get("join_column")
        if table_name != "CRM_MB_BASEINFO" and not join_column:
            continue
        column_short = (dimension_filter.get("column") or "").split(".")[-1]
        codes = [code for code in dimension_filter.get("codes", []) if isinstance(code, str) and code]
        if not column_short or not codes:
            continue
        if table_name == "CRM_MB_BASEINFO" and column_short in ("SIDO", "SIGUNGU"):
            member_region_codes.setdefault(column_short, [])
            member_region_codes[column_short].extend(code for code in codes if code not in member_region_codes[column_short])
        else:
            in_list = ", ".join(_sql_quote(code) for code in codes)
            if table_name == "CRM_MB_BASEINFO":
                if len(codes) == 1 and (dimension_filter.get("operator") or "IN").upper() == "=":
                    other_predicates.append("B." + column_short + " = " + _sql_quote(codes[0]))
                else:
                    other_predicates.append("B." + column_short + " IN (" + in_list + ")")
            else:
                other_predicates.append(
                    f"B.{join_column} IN (SELECT S.{join_column} FROM {table_name} S WHERE S.{column_short} IN ({in_list}))"
                )
        labels.extend(dimension_filter.get("names") or codes)
        has_signal = True
    other_predicates.extend(_member_region_predicates(member_region_codes))

    # CRM_MB_BASEINFO 단독으로 표현할 수 없는 조건(→ unsupported 로 모아 fallback 유도)
    for field in ("interests", "preferred_channels", "behaviors", "purchase_object", "price_sensitivity"):
        if target_user.get(field):
            unsupported.append("target_user." + field)
    if exclude.get("interests"):
        unsupported.append("exclude.interests")
    # 집계 조건은 build_aggregate_targets_sql_candidate 가 커버한다. 그 빌더가 dropped 에서 빼주므로,
    # 여기선 일단 unsupported 로 표시해 (집계 빌더에 닿지 못하고) 회원 빌더로 빠질 때 조용한 누락을 막는다.
    if target_user.get("aggregate_conditions"):
        unsupported.append("target_user.aggregate_conditions")
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

    부분 추출 + 고지 정책: 실DB로 해석 가능한 회원 신호(성별·연령·등급/생애주기)가 하나라도 있으면
    그 조건들로 SQL 을 만들고, 실컬럼이 없어 뺀 조건(예: 관심사)은 candidate 의 dropped_conditions 에
    담아 함께 고지한다(조용한 누락 방지). 회원 신호가 전혀 없으면(objective/관심사만) None 을 돌려
    기존 템플릿 경로로 넘긴다.
    """
    compiled = compile_member_target_conditions(query_plan)
    density = query_plan.get("region_density_target")
    # 밀집/지표 지역 랭킹은 코호트 조건이 없어도 성립한다("매출이 높은 지역" = 전체 회원 기준 랭킹).
    if not compiled["has_signal"] and not isinstance(density, dict):
        return None

    # "X가 많이 거주하는 동네" — 코호트(X) 조건으로 지역을 랭킹하고 그 지역 거주 회원을 타겟한다.
    if isinstance(density, dict):
        # 코호트 조건이 있었는데 전부 미지원이면(예: 직장인) 전체 인구 랭킹으로 조용히 대체하지
        # 않는다 — 의미가 달라지므로 기존 미지원 안내 흐름으로 거절한다.
        if not compiled["has_signal"] and compiled["unsupported"] and not density.get("metric_id"):
            return None
        return _build_dense_region_targets_candidate(query_plan, compiled, density)

    # 회원상태(dormant 등)를 직접 지정한 타겟이 아니면 정상 회원으로 한정한다(탈퇴/휴면 제외).
    # 이 술어는 사용자가 말하지 않았는데 주입되는 기본 게이트라, 카디널리티 진단에서 과잉 조건
    # 후보로 따로 표시하기 위해 참조를 보관한다.
    state_predicate = None if compiled["forces_state"] else _member_active_state_predicate()
    where_clauses = list(compiled["predicates"])
    if state_predicate is not None:
        where_clauses.append(state_predicate)
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
    candidate = _sql_candidate("sql_template:member_targets", "회원 속성 타겟 추출 SQL 템플릿(CRMDW)", 1.0, sql, _template_tables(sql), "sql_template")
    # 실DB 미지원이라 SQL 에서 뺀 조건(부분 추출). 커버리지 검증에서 제외하고 응답에 고지한다.
    candidate["dropped_conditions"] = compiled["unsupported"]
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in compiled["unsupported"]]
    # 술어별 카디널리티 진단용 메타. 실행 결과가 0명일 때, from_clause + 각 술어를 독립 COUNT 로
    # 돌려 어느 AND 술어가 오디언스를 죽였는지 귀속한다(과잉 조건 탐지). injected_default 는
    # 사용자가 명시하지 않았지만 주입된 기본 게이트(정상회원 한정)를 가리킨다.
    candidate["cardinality_probe"] = {
        "from_clause": "CRM_MB_BASEINFO B",
        "predicates": [
            {"sql": clause, "injected_default": clause == state_predicate}
            for clause in where_clauses
        ],
    }
    return candidate


def _build_dense_region_targets_candidate(
    query_plan: dict[str, Any], compiled: dict[str, Any], density: dict[str, Any]
) -> dict[str, Any]:
    """밀집 지역 타겟 SQL: 코호트(X) 조건으로 지역별 회원 수를 집계해 상위 N개 지역을 뽑고(내부),
    그 지역에 거주하는 정상 회원 전체를 타겟한다(외부). "X가 많이 거주하는 동네에 판촉" 은 지역
    단위 캠페인이므로 외부는 코호트로 다시 좁히지 않는다(지역 선정 기준 ≠ 발송 대상 조건)."""
    column = density.get("column", "SIGUNGU")
    top_n = int(density.get("top_n", 5))

    # 랭킹 기준: 기본은 거주 회원 수(COUNT). metric_id 가 있으면 지표 레지스트리(member_metrics.json)
    # 의 집계식(예: SUM(TOTAL_BUY_AMT))으로 랭킹한다 — 지표 테이블은 회원키로 조인, 월 스냅샷
    # 테이블의 중복 집계는 레지스트리의 grain_filter(최신 월 한정)로 막는다.
    inner_from = ["    FROM CRM_MB_BASEINFO B"]
    order_by = "COUNT(*)"
    metric_where: list[str] = []
    metric_id = density.get("metric_id")
    if metric_id:
        registry = _load_member_metrics(str(DEFAULT_MEMBER_METRICS_PATH)) or {}
        metric = next((m for m in registry.get("metrics", []) if m.get("metric_id") == metric_id), None)
        if metric:
            value_table = registry.get("value_table", "CRM_MB_MONTHCRMINFO")
            join_column = registry.get("join_column", "MEMBER_NO")
            inner_from.append(f"         INNER JOIN {value_table} C ON B.{join_column} = C.{join_column}")
            order_by = f"{metric.get('agg', 'SUM')}(C.{metric['column']})"
            grain_filter = registry.get("grain_filter")
            if grain_filter:
                metric_where.append(grain_filter)

    inner_where = list(compiled["predicates"])
    if not compiled["forces_state"]:
        inner_where.append(_member_active_state_predicate())
    inner_where.extend(metric_where)
    inner_where.extend([f"B.{column} IS NOT NULL", f"B.{column} <> ''"])
    inner_where = _unique_strings(inner_where)
    inner_sql = "\n".join(
        [
            f"    SELECT TOP {top_n} B.{column}",
            *inner_from,
            "    WHERE " + "\n      AND ".join(inner_where),
            f"    GROUP BY B.{column}",
            f"    ORDER BY {order_by} DESC",
        ]
    )

    select_columns = [
        "DISTINCT M.MEMBER_NO AS CUST_ID",
        "M.EMART_GRADE_CD AS member_grade",
        f"M.{column} AS target_region",
    ]
    segment_parts = [metric_id] if metric_id else []
    segment_parts.extend(compiled["labels"])
    segment = "dense_region" + (":" + ",".join(segment_parts) if segment_parts else "")
    select_columns.append(_sql_quote(segment) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    sql = "\n".join(
        [
            "SELECT " + ", ".join(select_columns),
            "FROM CRM_MB_BASEINFO M",
            f"WHERE M.{column} IN (",
            inner_sql,
            ")",
            "  AND " + _member_active_state_predicate("M"),
        ]
    )
    candidate = _sql_candidate(
        "sql_template:dense_region_targets",
        "거주 밀집 지역(상위 N) 타겟 추출 SQL 템플릿(CRMDW)",
        1.0,
        sql,
        _template_tables(sql),
        "sql_template",
    )
    candidate["dropped_conditions"] = compiled["unsupported"]
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in compiled["unsupported"]]
    return candidate


def build_member_metric_ranking_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """'<지표>가 높은 고객'을 회원 단위 지표 랭킹 SQL 로 생성한다(지표값 내림차순 상위 N 명).

    지역 랭킹(_build_dense_region_targets_candidate)의 회원 단위 짝이다. 지표 테이블
    (CRM_MB_MONTHCRMINFO)을 회원키로 조인해 지표값(예: TOTAL_BUY_AMT)으로 정렬한다 — 월 스냅샷
    테이블의 회원당 중복 행은 레지스트리 grain_filter(최신 월 한정)로 막아 회원당 1 행을 보장한다.
    성별/연령/등급/휴면 등 회원 속성이 함께 있으면 compile_member_target_conditions 술어로 같은 SQL 에
    AND 결합한다("30대 여성 중 매출 높은 고객 상위 100명"). LLM 이 없는 컬럼(CUMULATIVE_PURCHASE_AMOUNT
    등)을 지어내던 폴백을 대체한다."""
    ranking = query_plan.get("member_metric_ranking")
    if not isinstance(ranking, dict):
        return None
    registry = _load_member_metrics(str(DEFAULT_MEMBER_METRICS_PATH)) or {}
    metric = next((m for m in registry.get("metrics", []) if m.get("metric_id") == ranking.get("metric_id")), None)
    if not metric:
        return None
    value_table = registry.get("value_table", "CRM_MB_MONTHCRMINFO")
    join_column = registry.get("join_column", "MEMBER_NO")
    metric_expr = f"C.{metric['column']}"
    top_n = int(ranking.get("top_n", 100))

    compiled = compile_member_target_conditions(query_plan)
    where_clauses = list(compiled["predicates"])
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())
    grain_filter = registry.get("grain_filter")
    if grain_filter:
        where_clauses.append(grain_filter)
    where_clauses.append(f"{metric_expr} IS NOT NULL")
    where_clauses = _unique_strings(where_clauses)

    select_columns = [
        f"DISTINCT TOP {top_n} B.MEMBER_NO AS CUST_ID",
        "B.EMART_GRADE_CD AS member_grade",
        f"{metric_expr} AS {metric['metric_id']}",
    ]
    segment_parts = [ranking["metric_id"], *compiled["labels"]]
    select_columns.append(_sql_quote("metric_rank:" + ",".join(segment_parts)) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    sql = "\n".join(
        [
            "SELECT " + ", ".join(select_columns),
            "FROM CRM_MB_BASEINFO B",
            f"     INNER JOIN {value_table} C ON B.{join_column} = C.{join_column}",
            "WHERE " + "\n  AND ".join(where_clauses),
            f"ORDER BY {metric_expr} DESC",
        ]
    )
    candidate = _sql_candidate(
        "sql_template:member_metric_ranking",
        f"회원 단위 지표 랭킹(상위 N, {ranking.get('metric_label', ranking['metric_id'])}) 타겟 추출 SQL 템플릿(CRMDW)",
        1.0,
        sql,
        _template_tables(sql),
        "sql_template",
    )
    candidate["dropped_conditions"] = compiled["unsupported"]
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in compiled["unsupported"]]
    return candidate


# 상품 구매 이력 매칭 대상 컬럼(CRM_CM_PRODUCT). 카테고리 계층~상품명~브랜드명까지 넓게 LIKE 매칭해
# "기저귀"(카테고리), "하기스"(브랜드), 특정 상품명 등 어떤 표현으로 말해도 재현율을 확보한다.
# 컬럼 목록은 member_target_filters.json 의 purchase_product_match_columns 가 소유한다.
_PURCHASE_PRODUCT_MATCH_COLUMNS = tuple(
    column
    for column in _MEMBER_TARGET_FILTERS.get("purchase_product_match_columns", [])
    if isinstance(column, str) and column
) or tuple(_DEFAULT_MEMBER_TARGET_FILTERS["purchase_product_match_columns"])


def _sql_nlike_contains(column: str, term: str) -> str:
    """유니코드 부분일치 LIKE 술어(N'%term%'). term 은 _sanitize_purchase_object 로 정제돼 홑따옴표가 없으나
    방어적으로 이스케이프한다. N 접두어는 tsql/mysql 모두 유효해 한글 리터럴을 안전하게 비교한다."""
    return f"{column} LIKE N'%{term.replace(chr(39), chr(39) * 2)}%'"


def build_purchase_history_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """실주문 상세(CRM_SL_ORDERDETAILMALL) → 상품(CRM_CM_PRODUCT) → 회원(CRM_MB_BASEINFO) 조인으로
    특정 상품/카테고리를 구매한 회원을 추출한다.

    CRM_MB_BASEINFO 단독으로는 표현 못 하는 "상품 구매 이력"을 실주문 테이블 조인으로 해결한다.
    성별/연령/등급/휴면 등 회원 속성은 compile_member_target_conditions 로 그대로 재사용해 같은 SQL 에
    AND 결합하므로, "40대 여성 중 기저귀 구매자" 같은 조합도 하나의 추출 SQL 이 된다.
    """
    purchase_object = query_plan.get("target_user", {}).get("purchase_object")
    if not isinstance(purchase_object, str) or not purchase_object:
        return None

    compiled = compile_member_target_conditions(query_plan)
    product_match = "(" + " OR ".join(
        _sql_nlike_contains("P." + column, purchase_object) for column in _PURCHASE_PRODUCT_MATCH_COLUMNS
    ) + ")"
    where_clauses = [product_match, *compiled["predicates"]]
    # 회원상태를 직접 지정한 타겟(휴면 등)이 아니면 정상 회원으로 한정한다(탈퇴/휴면 제외).
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())
    where_clauses = _unique_strings(where_clauses)

    select_columns = ["DISTINCT B.MEMBER_NO AS CUST_ID", "B.EMART_GRADE_CD AS member_grade"]
    if compiled["labels"]:
        select_columns.append(_sql_quote(",".join(compiled["labels"])) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    sql = "\n".join(
        [
            "SELECT " + ", ".join(select_columns),
            "FROM CRM_SL_ORDERDETAILMALL D",
            "     INNER JOIN CRM_CM_PRODUCT P ON D.PRODUCT_ID = P.PRODUCT_ID",
            "     INNER JOIN CRM_MB_BASEINFO B ON D.MEMBER_NO = B.MEMBER_NO",
            "WHERE " + "\n  AND ".join(where_clauses),
        ]
    )
    candidate = _sql_candidate(
        "sql_template:purchase_history_targets", "상품 구매 이력 타겟 추출 SQL 템플릿(CRMDW)", 1.0, sql, _template_tables(sql), "sql_template"
    )
    # purchase_object 는 이 템플릿(상품 LIKE)이 실제로 커버하므로 dropped(미고지)에서 제외한다.
    # 회원 속성 외 다른 미지원 조건(관심사 등)이 있으면 그것만 부분추출 고지 대상으로 남긴다.
    dropped = [path for path in compiled["unsupported"] if path != "target_user.purchase_object"]
    candidate["dropped_conditions"] = dropped
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in dropped]
    return candidate


def _order_count_targets_config() -> dict[str, Any]:
    config = _MEMBER_TARGET_FILTERS.get("order_count_targets")
    if not isinstance(config, dict):
        config = _DEFAULT_MEMBER_TARGET_FILTERS["order_count_targets"]
    behaviors = config.get("behaviors")
    return config if isinstance(behaviors, dict) else _DEFAULT_MEMBER_TARGET_FILTERS["order_count_targets"]


def _aggregate_targets_config() -> dict[str, Any]:
    config = _MEMBER_TARGET_FILTERS.get("aggregate_targets")
    if not isinstance(config, dict) or not isinstance(config.get("metrics"), dict):
        return _DEFAULT_MEMBER_TARGET_FILTERS["aggregate_targets"]
    return config


def _format_threshold(threshold: int | float) -> str:
    return str(int(threshold)) if float(threshold).is_integer() else repr(float(threshold))


def _aggregate_member_subquery(
    config: dict[str, Any], metric: dict[str, Any], operator: str, threshold: int | float,
    window_days: Any, alias: str,
) -> str:
    """회원별 집계 조건 서브쿼리(GROUP BY <회원키> HAVING <집계식> <연산자> <임계값>)를 만든다.

    이것이 '범용 집계 조건 빌더'의 핵심이다 — agg/column/distinct/기간창은 전부 인자·config 로 주어지고,
    주문 횟수든 누적 금액이든 같은 서브쿼리 골격을 쓴다. 기간창이 있으면 주문일자(YYYYMMDD 문자열)를
    사전식 비교로 최근 N일로 한정한다."""
    table = config.get("table", "CRM_SL_ORDERHEADERMALL")
    join_column = config.get("join_column", "MEMBER_NO")
    date_column = config.get("date_column", "ORDER_DATE")
    column = metric.get("column")
    agg = str(metric.get("agg", "SUM")).upper()
    agg_expr = f"COUNT(DISTINCT {column})" if metric.get("distinct") else f"{agg}({column})"
    where = [f"{join_column} IS NOT NULL"]
    if isinstance(window_days, int) and window_days > 0 and date_column:
        cutoff = f"CONVERT(CHAR(8), DATEADD(DAY, -{window_days}, GETDATE()), 112)"
        where.append(f"{date_column} >= {cutoff}")
    return "\n".join(
        [
            "(",
            f"    SELECT {join_column}",
            f"    FROM {table}",
            f"    WHERE {' AND '.join(where)}",
            f"    GROUP BY {join_column}",
            f"    HAVING {agg_expr} {operator} {_format_threshold(threshold)}",
            f") {alias}",
        ]
    )


def build_aggregate_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """범용 집계 조건('최근 N일 누적 구매 금액 100만원 이상' 등)을 실주문 집계로 타겟 추출한다.

    CRM_MB_BASEINFO 단독으론 표현 못 하는 '기간 내 집계 임계값' 조건을 주문 테이블 회원별 집계 서브쿼리
    INNER JOIN 으로 해결한다. 성별/연령/등급/지역 등 회원 속성은 compile_member_target_conditions 로 같은
    SQL 에 AND 결합한다("서울 VIP 중 최근 90일 100만원 이상 구매자"처럼 하나의 추출 SQL). 지원 지표/컬럼은
    member_target_filters.json 의 aggregate_targets 가 소유한다."""
    target_user = query_plan.get("target_user", {})
    conditions = target_user.get("aggregate_conditions")
    if not isinstance(conditions, list) or not conditions:
        return None
    config = _aggregate_targets_config()
    metrics = config.get("metrics", {})
    join_column = config.get("join_column", "MEMBER_NO")
    valid = [
        condition
        for condition in conditions
        if isinstance(condition, dict)
        and isinstance(metrics.get(condition.get("metric_id")), dict)
        and condition.get("operator") in {"=", ">", ">=", "<", "<="}
        and isinstance(condition.get("threshold"), (int, float))
    ]
    if not valid:
        return None

    compiled = compile_member_target_conditions(query_plan)
    from_clause = ["FROM CRM_MB_BASEINFO B"]
    labels = list(compiled["labels"])
    for position, condition in enumerate(valid):
        metric = metrics[condition["metric_id"]]
        alias = f"AGG{position}"
        subquery = _aggregate_member_subquery(
            config, metric, condition["operator"], condition["threshold"], condition.get("window_days"), alias
        )
        from_clause.append(f"     INNER JOIN {subquery} ON B.{join_column} = {alias}.{join_column}")
        labels.append(condition.get("label") or condition["metric_id"])

    where_clauses = list(compiled["predicates"])
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())
    where_clauses = _unique_strings(where_clauses)

    select_columns = ["DISTINCT B.MEMBER_NO AS CUST_ID", "B.EMART_GRADE_CD AS member_grade"]
    if labels:
        select_columns.append(_sql_quote(",".join(_unique_strings(labels))) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    sql_lines = ["SELECT " + ", ".join(select_columns), *from_clause]
    if where_clauses:
        sql_lines.append("WHERE " + "\n  AND ".join(where_clauses))
    sql = "\n".join(sql_lines)
    candidate = _sql_candidate(
        "sql_template:aggregate_targets", "집계 조건(구매 금액/횟수 임계값) 타겟 추출 SQL 템플릿(CRMDW)", 1.0, sql, _template_tables(sql), "sql_template"
    )
    # 집계 조건은 이 템플릿이 커버하므로 dropped 에서 뺀다. 그 외 미지원 회원 조건만 부분추출로 고지한다.
    dropped = [path for path in compiled["unsupported"] if path != "target_user.aggregate_conditions"]
    candidate["dropped_conditions"] = dropped
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in dropped]
    return candidate


# ── 합집합(OR) 타겟 컴파일 ─────────────────────────────────────────────────────────────
# 재작성이 "A 이거나 B 또는 C" 의 OR 을 콤마로 뭉개고, 회원속성·집계 조건이 서로 다른 메커니즘이라
# 기본 빌더는 전부 AND 로만 결합한다. 여기서는 원본에서 감지한 top-level 합집합(union_condition, set_ast)을
# 실CRM 술어로 재귀 컴파일해 하나의 CRM_MB_BASEINFO 쿼리에서 OR/AND/AND NOT 로 묶는다. 각 피연산자는
# 회원속성이면 컬럼 술어, 집계 지표(구매금액 등)면 회원키 IN 서브쿼리로 컴파일된다. 값·임계값은 결정론
# 필터가 재작성본에서 뽑아둔 dimension_filters/aggregate_conditions 를 재사용한다.
def _region_predicate_from_plan(query_plan: dict[str, Any]) -> str | None:
    codes_by_column: dict[str, list[str]] = {}
    for dimension_filter in query_plan.get("dimension_filters", []):
        if dimension_filter.get("table") != "CRM_MB_BASEINFO":
            continue
        column = (dimension_filter.get("column") or "").split(".")[-1].upper()
        if column not in ("SIDO", "SIGUNGU"):
            continue
        codes = [code for code in dimension_filter.get("codes", []) if isinstance(code, str) and code]
        if codes:
            codes_by_column.setdefault(column, [])
            codes_by_column[column].extend(code for code in codes if code not in codes_by_column[column])
    predicates = [
        "B." + column + " IN (" + ", ".join(_sql_quote(code) for code in codes) + ")"
        for column, codes in codes_by_column.items()
    ]
    if not predicates:
        return None
    return predicates[0] if len(predicates) == 1 else "(" + " OR ".join(predicates) + ")"


def _aggregate_metric_id_for_canonical(canonical: str) -> str | None:
    target = re.sub(r"\s+", "", canonical).casefold()
    for metric_id, metric in _aggregate_targets_config().get("metrics", {}).items():
        for synonym in metric.get("synonyms", []):
            if isinstance(synonym, str) and re.sub(r"\s+", "", synonym).casefold() == target:
                return metric_id
    return None


def _aggregate_in_predicate_from_plan(metric_id: str, query_plan: dict[str, Any]) -> str | None:
    config = _aggregate_targets_config()
    metric = config.get("metrics", {}).get(metric_id)
    if not isinstance(metric, dict):
        return None
    condition = next(
        (
            c for c in query_plan.get("target_user", {}).get("aggregate_conditions", [])
            if isinstance(c, dict) and c.get("metric_id") == metric_id
            and c.get("operator") in {"=", ">", ">=", "<", "<="} and isinstance(c.get("threshold"), (int, float))
        ),
        None,
    )
    if condition is None:
        return None
    table = config.get("table", "CRM_SL_ORDERHEADERMALL")
    join_column = config.get("join_column", "MEMBER_NO")
    date_column = config.get("date_column", "ORDER_DATE")
    column = metric.get("column")
    agg = str(metric.get("agg", "SUM")).upper()
    agg_expr = f"COUNT(DISTINCT {column})" if metric.get("distinct") else f"{agg}({column})"
    where = [f"{join_column} IS NOT NULL"]
    window_days = condition.get("window_days")
    if isinstance(window_days, int) and window_days > 0 and date_column:
        where.append(f"{date_column} >= CONVERT(CHAR(8), DATEADD(DAY, -{window_days}, GETDATE()), 112)")
    inner = (
        f"SELECT {join_column} FROM {table} WHERE {' AND '.join(where)} "
        f"GROUP BY {join_column} HAVING {agg_expr} {condition['operator']} {_format_threshold(condition['threshold'])}"
    )
    return f"B.{join_column} IN ({inner})"


def _resolve_union_operand_predicate(operand: dict[str, Any], query_plan: dict[str, Any]) -> str | None:
    canonical = operand.get("canonical")
    if not isinstance(canonical, str) or not canonical:
        return None
    eq_predicate = _member_eq_predicate(canonical)  # 성별/등급/상태/채널 등가 필터(canonical 직접)
    if eq_predicate:
        return eq_predicate
    canonical_fold = canonical.casefold()
    if canonical_fold in _REGION_DIMENSION_CANONICALS:  # 지역 → dimension_filters SIDO/SIGUNGU
        return _region_predicate_from_plan(query_plan)
    if canonical_fold in _GRADE_DIMENSION_CANONICALS:  # 등급 디멘션 → 표면형에서 등급값 복원
        joined = " ".join(_set_operand_surface_terms(operand)).casefold()
        for surface, value in _GRADE_SURFACE_TO_VALUE:
            if surface in joined and value in MEMBER_EQ_FILTERS:
                return _member_eq_predicate(value)
        return None
    metric_id = _aggregate_metric_id_for_canonical(canonical)  # 집계 지표 → 회원키 IN 서브쿼리
    if metric_id:
        return _aggregate_in_predicate_from_plan(metric_id, query_plan)
    return None


def _compile_crm_set_ast(ast: Any, query_plan: dict[str, Any]) -> str | None:
    """set_ast 를 실CRM(CRM_MB_BASEINFO) 불리언 술어로 재귀 컴파일한다(하나라도 불가면 None → 폴백)."""
    if not isinstance(ast, dict):
        return None
    node_type = ast.get("type")
    if node_type == "set_op":
        left = _compile_crm_set_ast(ast.get("left"), query_plan)
        right = _compile_crm_set_ast(ast.get("right"), query_plan)
        if left is None or right is None:
            return None
        op = ast.get("op")
        if op == "+":
            return f"({left} OR {right})"
        if op == "*":
            return f"({left} AND {right})"
        if op == "-":
            return f"({left} AND NOT {right})"
        return None
    if node_type == "age_range":
        age_min, age_max = ast.get("age_min"), ast.get("age_max")
        if isinstance(age_min, int) and isinstance(age_max, int):
            return f"(B.AGE >= {age_min} AND B.AGE <= {age_max})"
        return None
    if node_type == "operand":
        return _resolve_union_operand_predicate(ast, query_plan)
    return None  # unknown_operand 등 → 컴파일 불가


def _union_condition_labels(query_plan: dict[str, Any]) -> list[str]:
    """union 조건이 아우르는 세그먼트 라벨(등급/지역/집계) — 조건 커버리지 충족용."""
    labels: list[str] = list(query_plan.get("target_user", {}).get("lifecycle", []))
    for dimension_filter in query_plan.get("dimension_filters", []):
        labels.extend(dimension_filter.get("names") or [])
    for condition in query_plan.get("target_user", {}).get("aggregate_conditions", []):
        if isinstance(condition, dict) and condition.get("label"):
            labels.append(condition["label"])
    return _unique_strings([label for label in labels if isinstance(label, str) and label])


def build_union_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """top-level 합집합(OR) 조건을 실CRM 한 쿼리로 추출한다(union_condition 이 있고 전부 컴파일될 때만)."""
    ast = query_plan.get("union_condition")
    if not isinstance(ast, dict):
        return None
    predicate = _compile_crm_set_ast(ast, query_plan)
    if not predicate:
        return None
    where_clauses = _unique_strings([predicate, _member_active_state_predicate()])
    select_columns = ["DISTINCT B.MEMBER_NO AS CUST_ID", "B.EMART_GRADE_CD AS member_grade"]
    labels = _union_condition_labels(query_plan)
    if labels:
        select_columns.append(_sql_quote(",".join(labels)) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")
    sql = "\n".join(
        [
            "SELECT " + ", ".join(select_columns),
            "FROM CRM_MB_BASEINFO B",
            "WHERE " + "\n  AND ".join(where_clauses),
        ]
    )
    candidate = _sql_candidate(
        "sql_template:union_targets", "합집합(OR) 조건 타겟 추출 SQL 템플릿(CRMDW)", 1.0, sql, _template_tables(sql), "sql_template"
    )
    candidate["dropped_conditions"] = []
    candidate["dropped_condition_labels"] = []
    return candidate


def _apply_union_condition(original_query: str, query_plan: dict[str, Any], normalization_rules: Path | None) -> None:
    """원본 프롬프트에서 top-level 합집합(OR)을 감지해 union_condition(set_ast)으로 붙인다.

    OR(또는/이거나 등)은 재작성에서 콤마로 사라지므로 원본에서 감지한다. 캠페인 로직 절('…대상으로 …')은
    떼고 오디언스 절만 파싱한다. top-level 이 합집합(+)이고 모든 피연산자가 실CRM 술어로 컴파일될 때만
    붙인다(하나라도 불가하거나 AND-only 면 붙이지 않아 기존 AND 경로로 안전하게 폴백)."""
    if not normalization_rules:
        return
    audience_text = re.split(r"(?:을|를)?\s*대상으로", original_query, maxsplit=1)[0].strip() or original_query
    try:
        expressions = parse_set_expressions_from_query(audience_text, normalization_path=normalization_rules)
    except Exception:  # noqa: BLE001 - 파싱 실패 시 union 미적용(기존 AND 경로 유지)
        return
    if not expressions:
        return
    ast = expressions[0].get("set_ast")
    if not isinstance(ast, dict) or ast.get("op") != "+":
        return  # top-level 합집합일 때만 OR 타겟으로 본다
    if _compile_crm_set_ast(ast, query_plan) is None:
        return  # 피연산자 중 CRM 술어로 컴파일 못하는 게 있으면 폴백
    query_plan["union_condition"] = ast
    query_plan["combine_mode"] = "or"
    # 재작성본에서 뽑힌 plan 의 set_expressions 는 이 union_condition 이 대표하는 조건과 중복이고,
    # 재작성이 OR·값을 뭉개 종종 미정규화(unknown_operand) clarification 으로 SQL 을 막는다. union_condition
    # 이 권위 있는 표현이므로 그 redundant 집합식은 비운다(막힘 방지).
    query_plan["set_expressions"] = []


def build_order_count_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """실주문 헤더(CRM_SL_ORDERHEADERMALL)를 회원별로 집계해 '주문 횟수' 행동 세그먼트를 추출한다.

    첫 구매(주문 1건)/재구매(2건 이상)는 회원별 주문 수 서브쿼리(INNER JOIN)로, 무구매는 주문이
    없는 정상 회원(NOT EXISTS anti-join)으로 뽑는다. 이 세 세그먼트는 CRM_MB_BASEINFO 단독 컬럼으로는
    표현할 수 없어(주문 이력 집계 필요) 기존 회원 빌더가 처리하지 못하던 조건이다. 성별/연령/등급/지역
    등 회원 속성은 compile_member_target_conditions 로 그대로 AND 결합한다("첫 구매 30대 여성" 등 조합).
    지원 행동/집계 기준은 member_target_filters.json 의 order_count_targets 가 소유한다.
    """
    target_user = query_plan.get("target_user", {})
    behaviors = target_user.get("behaviors", [])
    purchase_inactivity = target_user.get("purchase_inactivity")
    config = _order_count_targets_config()
    behavior_rules = config["behaviors"]
    table = config.get("table", "CRM_SL_ORDERHEADERMALL")
    join_column = config.get("join_column", "MEMBER_NO")
    order_id_column = config.get("order_id_column", "ORDER_ID")
    order_date_column = config.get("order_date_column", "ORDER_DATE")

    # 구매 미발생 기간('최근 N일 구매 안 함')이 우선한다 — no_purchase(평생 무주문)와 달리 기간 창
    # anti-join 으로 뽑는다(과거 구매 여부 무관, 최근 N일 내 주문 없음).
    if isinstance(purchase_inactivity, dict) and isinstance(purchase_inactivity.get("min_days"), int):
        min_days = purchase_inactivity["min_days"]
        compiled = compile_member_target_conditions(query_plan)
        where_clauses = list(compiled["predicates"])
        if not compiled["forces_state"]:
            where_clauses.append(_member_active_state_predicate())
        cutoff = f"CONVERT(CHAR(8), DATEADD(DAY, -{min_days}, GETDATE()), 112)"
        where_clauses.append(
            f"NOT EXISTS (SELECT 1 FROM {table} O WHERE O.{join_column} = B.{join_column} "
            f"AND O.{order_date_column} >= {cutoff})"
        )
        segment = f"purchase_inactive_{min_days}d"
        select_columns = [
            "DISTINCT B.MEMBER_NO AS CUST_ID",
            "B.EMART_GRADE_CD AS member_grade",
            _sql_quote(segment) + " AS target_segment",
        ]
        if compiled["labels"]:
            select_columns.append(_sql_quote(",".join(compiled["labels"])) + " AS segment_label")
        objective = query_plan.get("campaign_constraints", {}).get("objective")
        if objective:
            select_columns.append(_sql_quote(objective) + " AS objective")
        sql = "\n".join(
            [
                "SELECT " + ", ".join(select_columns),
                "FROM CRM_MB_BASEINFO B",
                "WHERE " + "\n  AND ".join(_unique_strings(where_clauses)),
            ]
        )
        candidate = _sql_candidate(
            "sql_template:order_count_targets", "구매 미발생 기간(최근 N일 미구매) 타겟 추출 SQL 템플릿(CRMDW)", 1.0, sql, _template_tables(sql), "sql_template"
        )
        candidate["dropped_conditions"] = compiled["unsupported"]
        candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in compiled["unsupported"]]
        return candidate

    # 프롬프트에 잡힌 행동 중 지원되는 주문 집계 행동을 고른다(정의 순서 우선; 보통 1개).
    selected = next((behavior for behavior in behavior_rules if behavior in behaviors), None)
    if selected is None:
        return None

    rule = behavior_rules[selected]
    compiled = compile_member_target_conditions(query_plan)
    where_clauses = list(compiled["predicates"])
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())

    if rule.get("anti_join"):
        # 무구매: 주문 이력이 전혀 없는 회원(anti-join).
        where_clauses.append(
            f"NOT EXISTS (SELECT 1 FROM {table} O WHERE O.{join_column} = B.{join_column})"
        )
        from_clause = ["FROM CRM_MB_BASEINFO B"]
    else:
        # 첫 구매/재구매: 회원별 주문 수를 집계한 서브쿼리와 조인(중복 주문행 방지 위해 DISTINCT ORDER_ID).
        operator = rule.get("operator", "=")
        count = int(rule.get("count", 1))
        order_subquery = "\n".join(
            [
                "(",
                f"    SELECT {join_column}",
                f"    FROM {table}",
                f"    WHERE {join_column} IS NOT NULL",
                f"    GROUP BY {join_column}",
                f"    HAVING COUNT(DISTINCT {order_id_column}) {operator} {count}",
                ") O",
            ]
        )
        from_clause = ["FROM CRM_MB_BASEINFO B", f"     INNER JOIN {order_subquery} ON B.{join_column} = O.{join_column}"]

    where_clauses = _unique_strings(where_clauses)
    select_columns = [
        "DISTINCT B.MEMBER_NO AS CUST_ID",
        "B.EMART_GRADE_CD AS member_grade",
        # 행동 세그먼트 라벨(조건 커버리지: behaviors/target_segment 충족 겸용).
        _sql_quote(selected) + " AS target_segment",
    ]
    if compiled["labels"]:
        select_columns.append(_sql_quote(",".join(compiled["labels"])) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    sql = "\n".join(
        [
            "SELECT " + ", ".join(select_columns),
            *from_clause,
            "WHERE " + "\n  AND ".join(where_clauses),
        ]
    )
    candidate = _sql_candidate(
        "sql_template:order_count_targets", "주문 횟수 행동(첫 구매/재구매/무구매) 타겟 추출 SQL 템플릿(CRMDW)", 1.0, sql, _template_tables(sql), "sql_template"
    )
    # 선택된 행동은 이 템플릿이 커버하므로 dropped 에서 뺀다. 단 지원 목록 밖의 다른 behavior 가 섞여
    # 있으면(예: office_worker) target_user.behaviors 드롭을 남겨 부분추출로 고지한다(조용한 누락 방지).
    remaining_behaviors = [behavior for behavior in behaviors if behavior not in behavior_rules]
    dropped = [
        path
        for path in compiled["unsupported"]
        if not (path == "target_user.behaviors" and not remaining_behaviors)
    ]
    candidate["dropped_conditions"] = dropped
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in dropped]
    return candidate


def _should_use_cart_repurchase_template(query_plan: dict[str, Any]) -> bool:
    target_user = query_plan.get("target_user", {})
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    return "cart_abandoner" in target_user.get("behaviors", []) and objective in {None, "purchase", "repurchase", "retention"}


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

    # 결정론 회원/주문 타겟 신호(생일·신규가입·밀집지역·지표랭킹·주문횟수·미구매창·집계·구매이력)는
    # build_verified_condition_tokens 가 토큰을 만들지 않지만 전용 빌더가 실제 추출 SQL 을 만든다.
    # recommend_campaign 이어도 이런 신호가 있으면 '추천 조건 있음'으로 인정한다 — 타겟팅 스코프 분리로
    # 캠페인 절('쿠폰 발송 캠페인')이 잘려도 오디언스 절('생일 고객')만으로 타겟팅되는 경우를 통과시킨다.
    if _has_member_target_signal(query_plan):
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
        # 상품 구매 이력 타겟(purchase_history_targets)은 상품값을 SQL 리터럴(LIKE N'%값%')로 직접 담으므로
        # 값 문자열이 SQL 에 존재하면 커버된 것으로 본다(데모 fallback 의 behavior LIKE '%값%' 도 동일 충족).
        conditions.append(_condition("target_user.purchase_object", purchase_object, [purchase_object]))

    inactivity_period = target_user.get("inactivity_period")
    if isinstance(inactivity_period, dict) and isinstance(inactivity_period.get("sql_interval"), str):
        conditions.append(
            _condition(
                "target_user.inactivity_period",
                inactivity_period["sql_interval"],
                [inactivity_period["sql_interval"], str(inactivity_period.get("min_days", ""))],
                # 데모(users.last_login_at)·실DB(CRM_MB_BASEINFO.LAST_LOGIN_DATE) 양쪽 공통 부분문자열.
                all_terms=["last_login"],
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

    # 밀집 지역 타겟(region_density_target)은 상위 N 집계 구조(TOP n / GROUP BY 지역컬럼)가 SQL 에
    # 실제로 있어야 커버된 것으로 본다 — 생성부(_build_dense_region_targets_candidate)-검증부 일치.
    density = query_plan.get("region_density_target")
    if isinstance(density, dict) and not _is_cart_dimension_targeting(query_plan):
        density_column = density.get("column", "SIGUNGU")
        density_terms = [density_column, "group by"]
        # 지표 랭킹(예: 매출)이면 지표 컬럼(TOTAL_BUY_AMT)이 SQL 에 실제로 있어야 커버된 것으로 본다.
        density_metric_id = density.get("metric_id")
        if density_metric_id:
            registry = _load_member_metrics(str(DEFAULT_MEMBER_METRICS_PATH)) or {}
            metric = next((m for m in registry.get("metrics", []) if m.get("metric_id") == density_metric_id), None)
            if metric:
                density_terms.append(metric["column"])
        conditions.append(
            _condition(
                "region_density_target",
                density_column,
                [f"top {density.get('top_n', 5)}"],
                all_terms=density_terms,
            )
        )

    # 회원 단위 지표 랭킹(member_metric_ranking)은 지표 컬럼(TOTAL_BUY_AMT)과 정렬(ORDER BY)이 SQL 에
    # 실제로 있어야 커버된 것으로 본다 — 생성부(build_member_metric_ranking_sql_candidate)-검증부 일치.
    ranking = query_plan.get("member_metric_ranking")
    if isinstance(ranking, dict):
        registry = _load_member_metrics(str(DEFAULT_MEMBER_METRICS_PATH)) or {}
        metric = next((m for m in registry.get("metrics", []) if m.get("metric_id") == ranking.get("metric_id")), None)
        if metric:
            conditions.append(
                _condition(
                    "member_metric_ranking",
                    metric["column"],
                    [f"top {ranking.get('top_n', 100)}"],
                    all_terms=[metric["column"], "order by"],
                )
            )

    # 회원 테이블 디멘션 필터(예: 시도/SIDO)는 회원/구매 타겟 SQL 에 그대로 컴파일되므로
    # (compile_member_target_conditions) 커버리지도 요구한다 — 생성부-검증부 일치.
    # 보조 속성 테이블 필터(join_column, 예: JOB_CD)도 동일하게 서브쿼리로 컴파일되므로 요구한다.
    # cart 디멘션 타겟팅 모드는 별도 cart SQL 이라 회원 컬럼 조건을 만들지 않으므로 요구하지 않는다.
    if not _is_cart_dimension_targeting(query_plan):
        for dimension_filter in query_plan.get("dimension_filters", []):
            if dimension_filter.get("table") != "CRM_MB_BASEINFO" and not dimension_filter.get("join_column"):
                continue
            column_short = (dimension_filter.get("column") or "").split(".")[-1]
            for code in dimension_filter.get("codes", []):
                if column_short and isinstance(code, str) and code:
                    conditions.append(
                        _condition(
                            "dimension_filters." + str(dimension_filter.get("dimension_id", "dimension")),
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
    prompt_normalization = result.get("prompt_normalization", {})
    # 정규화 프롬프트를 타겟팅/채널 절로 나눈 결과(검색·그래프 스코프의 근거).
    prompt_scopes = {
        "mode": retrieval.get("scope_mode"),
        "targeting": retrieval.get("targeting_query"),
        "channel": retrieval.get("channel_query"),
    }

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
            "path": node.get("path", []),
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

    # 트레이스 1단계(요청 이해)는 '타겟팅 프롬프트' 기준으로 보여준다. 캠페인 목표·발송 채널 절에서만
    # 나온 정규화 매칭(예: "재구매를"->repeat_buyer)은 오디언스 조건이 아니므로 추론 표시에서 제외한다.
    _targeting_compact = (prompt_scopes.get("targeting") or "").replace(" ", "").casefold()
    _channel_compact = (prompt_scopes.get("channel") or "").replace(" ", "").casefold()

    def _is_targeting_match(match: dict[str, Any]) -> bool:
        matched = (match.get("matched_text") or "").replace(" ", "").casefold()
        # 채널/목표 절에만 등장하는 표현이면 타겟팅 추론에서 뺀다(양쪽에 있거나 어디에도 없으면 유지).
        return not (matched and matched in _channel_compact and matched not in _targeting_compact)

    targeting_matched_terms = [
        match for match in query_plan.get("matched_terms", []) if _is_targeting_match(match)
    ]

    return {
        "query": result.get("query"),
        "collection": result.get("collection"),
        "retrieval_scope": result.get("retrieval_scope"),
        "prompt_scopes": prompt_scopes,
        "stages": [
            {
                "step": 1,
                "name": "요청 이해 — 고객 문장을 조건으로 해석",
                "description": "고객이 입력한 문장을 시스템이 쓰는 표준 용어·검색어·타겟 조건으로 바꾸는 단계입니다.",
                "tech_name": "의미 추론 (Query Planning / Normalization)",
                "intent": query_plan.get("intent"),
                "original_prompt": prompt_normalization.get("original", result.get("query")),
                "normalized_prompt": prompt_normalization.get("normalized", result.get("query")),
                # 실제 파싱(Query Plan/SQL)에 사용한 문장. 타겟팅 스코프면 오디언스(타겟팅) 절만 쓴다.
                "planning_prompt": query_plan.get("planning_query", prompt_normalization.get("normalized", result.get("query"))),
                # 정규화 프롬프트를 타겟팅(오디언스)/채널(발송·메시지) 절로 분리한 결과.
                "prompt_scopes": prompt_scopes,
                "applied_scope": result.get("retrieval_scope"),
                "targeting_terms": retrieval.get("targeting_terms", []),
                "channel_terms": retrieval.get("channel_terms", []),
                # 타겟팅 절에서 나온 매칭만 표시(캠페인 목표·채널 절 매칭은 제외).
                "matched_terms": targeting_matched_terms,
                "semantic_resolutions": query_plan.get("semantic_resolutions", []),
                "target_user": {key: value for key, value in target_user.items() if value not in (None, [], {})},
                "dimension_filters": query_plan.get("dimension_filters", []),
                "cart_context": query_plan.get("cart_context"),
                "retrieval_query": retrieval.get("query"),
                # 검색어도 타겟팅 스코프 기준으로 표시(전체 문장 토큰이 아니라 오디언스 검색어).
                "retrieval_terms": retrieval.get("targeting_terms", retrieval.get("terms", [])),
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
    # 회원 값 인덱스 노드는 저장 컬럼/테이블로 연결해 값→컬럼→테이블 그래프 확장이 이어지게 한다.
    column_name = node.get("target_column")
    if column_name and f"schema_column:{column_name}" in graph:
        graph.add_edge(node["id"], f"schema_column:{column_name}", relation="value_of_column")
    table_name = node.get("target_table")
    if table_name and f"schema_table:{table_name}" in graph:
        graph.add_edge(node["id"], f"schema_table:{table_name}", relation="value_in_table")


def _add_sql_example_edges(graph: nx.Graph, node: dict[str, Any]) -> None:
    for table_name in node.get("tables", []):
        table_node_id = f"schema_table:{table_name}"
        if table_node_id in graph:
            graph.add_edge(node["id"], table_node_id, relation="sql_uses_table")


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