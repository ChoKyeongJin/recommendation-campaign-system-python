from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import contextvars
import functools
import hashlib
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
from typing import Any, Callable

import networkx as nx
from fastembed import TextEmbedding
from qdrant_client import QdrantClient

from common_utils import elapsed_ms as _elapsed_ms
from aggregation_requirements import (
    SchemaMetadata,
    aggregation_request_json_schema,
    aggregation_retry_count,
    parse_aggregation_request,
    validate_aggregation_sql,
)
from analytical_intent import (
    SUPPORTED_QUERY_TYPES,
    SUPPORTED_RESULT_SHAPES,
    UNSUPPORTED_CLARIFICATIONS as ANALYTICAL_UNSUPPORTED_CLARIFICATIONS,
    analyze_analytical_intent,
    build_aggregation_request as build_deterministic_aggregation_request,
    compile_aggregation_ast,
    member_condition_filter,
    validate_intent_sql_contract,
)
from entity_set import (
    compile_entity_set_predicate,
    entity_set_label,
    parse_entity_set_condition,
)
from formula_engine import DEFAULT_METRIC_LEXICON_PATH, compile_formula_ast, parse_computed_metrics_from_query, validate_formula_ast
from targeting_expression import (
    TargetingExpressionError,
    compile_targeting_expression,
    describe_targeting_expression,
    targeting_expression_json_schema,
    validate_targeting_expression,
)
import logical_expression as _logic
from set_expression_engine import parse_set_expressions_from_query
from sql_ast import SelectAst, render_select_ast, validate_select_ast
from sql_guard import (
    DEFAULT_LIMIT,
    DEFAULT_SCHEMA_PATH,
    infer_target_connection,
    load_allowed_tables,
    load_column_types,
    load_schema_columns,
    load_join_key_registry,
    load_table_databases,
    load_table_dialects,
    analyze_query_performance,
    validate_analytics_shape,
    validate_join_keys,
    validate_sql,
)
from confidence import render_confidence_markdown, render_confidence_report, score_targeting_confidence
from sql_dialect import SqlDialect, get_dialect
import targeting_ir
from targeting_ir import extract_target_conditions, fact_join_kinds
import metric_registry
import segment_semantics
import semantic_requirements
import compiler_strategies
from query_structurer import (
    LLMQueryStructurer,
    QueryPlannerInput,
    QueryStructurer,
    QueryStructuringInput,
    StructuredQuery,
    StructuringContext,
    build_fallback,
    call_query_planner,
)
from query_structurer.prompt import PLANNER_STRUCTURED_QUERY_RULES
from query_semantics import classify_query_tokens, extract_extreme_semantics, is_non_entity_candidate
from data_quality import validate_metric_profile
from member_policy import (
    active_member_filter,
    active_member_predicate,
    member_condition_canonicals,
    resolve_member_scope,
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


def _model_restricts_sampling(model: str | None) -> bool:
    """gpt-5·o-series 추론 모델은 Chat Completions 에서 temperature 기본값(1)만 허용한다.
    이런 모델에 temperature=0 등을 보내면 400(invalid_request)로 실패한다."""
    lowered = (model or "").lower()
    return lowered.startswith(("gpt-5", "o1", "o3", "o4"))


def _openai_chat_create(client: Any, *, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
    """모델 호환 OpenAI Chat 호출 래퍼. 모든 chat.completions 호출은 이걸 거친다.

    - gpt-5/o-series 는 temperature!=1 미지원 → 제약 모델이면 temperature 를 떼고 기본값(1)을 쓴다.
    - 신모델은 max_tokens 대신 max_completion_tokens 를 요구 → 있으면 이관(구모델도 허용).
    이렇게 안 하면 이런 모델에서 전 LLM 단계가 400 으로 실패하고 규칙으로 조용히 폴백한다.
    """
    params = dict(kwargs)
    if "max_tokens" in params:
        params.setdefault("max_completion_tokens", params.pop("max_tokens"))
    if _model_restricts_sampling(model):
        params.pop("temperature", None)
        # 추론 모델은 기본 추론 깊이가 커서 느리다(재작성 1회 ~18s > 12s 타임아웃 → 규칙 폴백).
        # 이 파이프라인은 구조화 추출이 대부분이라 최소 추론으로 충분하고 빠르다(~4s). env 로 조절 가능.
        params.setdefault("reasoning_effort", os.getenv("OPENAI_REASONING_EFFORT", "minimal"))
    return client.chat.completions.create(model=model, messages=messages, **params)


def _structure_query(
    query: str,
    context: StructuringContext,
    llm_model: str,
    query_structurer: QueryStructurer | None = None,
) -> StructuredQuery:
    input = QueryStructuringInput(query=query, context=context)
    if query_structurer is not None:
        try:
            return query_structurer.structure(input)
        except Exception:  # noqa: BLE001 - structuring must never block the existing planner.
            return build_fallback(query)

    if not os.getenv("OPENAI_API_KEY"):
        return build_fallback(query)
    try:
        from openai import OpenAI

        client = OpenAI()

        def complete(messages: list[dict[str, str]]) -> str:
            response = _openai_chat_create(
                client,
                model=_fast_llm_model(llm_model) or llm_model,
                temperature=0,
                messages=messages,
            )
            return response.choices[0].message.content or ""

        return LLMQueryStructurer(complete).structure(input)
    except Exception:  # noqa: BLE001 - unavailable LLM uses the same safe fallback as invalid output.
        return build_fallback(query)


def _fast_llm_model(current: str | None) -> str | None:
    """지연에 민감하거나 정확도가 중요한 경량 단계(재작성·타겟/채널 분리·상품추출·의미검증)용 모델.

    메인 OPENAI_MODEL 이 느린 추론모델(gpt-5 등)이면 이 단계들은 12s 타임아웃에 걸리거나(폴백) 최소
    추론에서 조건을 드롭해 가드에 반려된다. 그래서 이들만 빠르고 정확한 모델(기본 gpt-4o-mini)로 고정한다.
    current=None(규칙 모드)이면 그대로 None 을 돌려줘 LLM 을 건너뛴다. OPENAI_FAST_MODEL 로 조절 가능.
    """
    if current is None:
        return None
    return os.getenv("OPENAI_FAST_MODEL") or "gpt-4o-mini"


def _semantic_verify_model(current: str | None) -> str | None:
    """의미검증(최종 SQL↔원문 직접 대조) 전용 모델. 재작성·타겟분리·상품추출과 분리해 따로 지정할 수 있게
    한다(OPENAI_SEMANTIC_VERIFY_MODEL). 미지정이면 fast 모델을 그대로 쓴다. 규칙 모드(current=None)면 None."""
    if current is None:
        return None
    return os.getenv("OPENAI_SEMANTIC_VERIFY_MODEL") or _fast_llm_model(current)


# 위 경량 단계에 해당하는 트레이스 step 번호(배지 모델명을 메인이 아니라 fast 모델로 표기).
_TRACE_FAST_MODEL_STEPS = {1, 2, 4, 9}
# 9단계(SQL 안전 검증 → 의미검증)는 전용 모델(OPENAI_SEMANTIC_VERIFY_MODEL) override 대상.
_TRACE_SEMANTIC_VERIFY_STEP = 9

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
#   범주 consent 는 수신동의 Y/N 컬럼 — 이들 값은 코드도메인 접두어 없이 순수 'Y'/'N' 이다(CRMDW 실값
#   GROUP BY 로 확인). '<채널> 수신 동의' 문맥 승격은 _apply_channel_consent_filter 가 담당한다.
# activity_filters: canonical -> 미접속 일수. LAST_LOGIN_DATE(YYYYMMDD 문자열) 사전식 비교.
#   범위 조건이라 제외(부정)는 의미가 모호해 미지원(→ fallback).
# lifecycle_extra_terms: 어휘로는 존재하나 등가/활동 필터로는 표현 못 하는 lifecycle canonical.
#   new_user 는 여기 남겨 LLM 파서 어휘(LIFECYCLE_TERMS)로 인식시키되, 실컬럼 매핑은 signup_target
#   (REG_DT 최근 N일 창)이 담당해 compile_member_target_conditions 가 술어로 만든다(→ fallback 아님).
# signup_target: 신규 가입 타겟(REG_DT, YYYYMMDD). REG_TYPE_CD.NEW 는 96%라 무의미해 가입일 창으로 정의한다.
#   anchor="data_max" 는 실적재 데이터 최신일(MAX(REG_DT)) 기준 최근 default_days 일 — 데모 데이터가
#   과거(2022~2023)라 GETDATE 기준이면 0명이 되는 문제를 피한다. 운영 전환 시 anchor="getdate" 로 바꾼다.
# recent_login_target: 최근 로그인(긍정형 접속) 창. '최근 N일/개월 로그인·접속한'을 LAST_LOGIN_DATE >=
#   (기준일-N일) 술어로 컴파일한다(미접속 activity_filters 의 대칭, 창은 프롬프트 명시 필수). anchor 는
#   기본 'getdate' — 적재 데이터가 과거라 0명이 나올 수 있어도, 조건 표현이 가능하면 요청 기간을
#   왜곡하지 않고 무조건 그대로 건다는 방침이다('data_max' 는 데모 시연용 옵션).
DEFAULT_MEMBER_TARGET_FILTERS_PATH = Path(
    os.getenv("GRAPH_RAG_MEMBER_TARGET_FILTERS", "docs/data/member_target_filters.json")
)
# 속성 토큰 그룹 선언(회원속성 표면어→lifecycle/exclude 승격 문법)의 단일 소스. 파일 부재/파손 시
# _default_attribute_token_groups_raw() 코드 폴백을 쓴다(동작 불변).
DEFAULT_ATTRIBUTE_TOKEN_GROUPS_PATH = Path(
    os.getenv("GRAPH_RAG_ATTRIBUTE_TOKEN_GROUPS", "docs/data/attribute_token_groups.json")
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
        {"canonical": "app_push_optin", "category": "consent", "column": "B.APP_PUSH_YN", "value": "Y"},
        {"canonical": "sms_optin", "category": "consent", "column": "B.SMS_YN", "value": "Y"},
        {"canonical": "email_optin", "category": "consent", "column": "B.EMAIL_YN", "value": "Y"},
        {"canonical": "marketing_optin", "category": "consent", "column": "B.AGREE_YN", "value": "Y"},
    ],
    "activity_filters": [
        {"canonical": "inactive_90d", "days": 90},
        {"canonical": "inactive_180d", "days": 180},
    ],
    "lifecycle_extra_terms": ["new_user"],
    "active_state": {"column": "MEMBER_STATE_CD", "value": "MEMBER_STATE_CD.NORMAL"},
    "birthday_target": {"column": "BIRTHDAY"},
    "signup_target": {"column": "REG_DT", "table": "CRM_MB_BASEINFO", "default_days": 90, "anchor": "data_max"},
    "recent_login_target": {"column": "LAST_LOGIN_DATE", "table": "CRM_MB_BASEINFO", "anchor": "getdate"},
    "order_count_targets": {
        "table": "CRM_SL_ORDERHEADERMALL",
        "evidence_tables": [
            "CRM_SL_ORDERHEADERMALL", "CRM_SL_ORDERHEADERALL",
            "CRM_SL_ORDERDETAILMALL", "CRM_SL_ORDERDETAILALL",
        ],
        "join_column": "MEMBER_NO",
        "order_id_column": "ORDER_ID",
        "order_date_column": "ORDER_DATE",
        "behaviors": {
            "first_purchase": {"operator": "=", "count": 1},
            "repeat_buyer": {"operator": ">=", "count": 2},
            "no_purchase": {"anti_join": True},
        },
    },
    # 장바구니 타겟. registered_date_column 은 '담아둔 지 N일' 비교 기준 시점이다 — INS_DT 는 ETL
    # 적재 시각이라 전 행이 같은 값이고(필터가 무력화된다) 행마다 실제로 다른 시점은 UPD_DT 뿐이다.
    # (나머지 조인/코드 값은 아직 빌더가 직접 들고 있어 여기 기본값은 기준 시점만 갖는다.)
    "cart_targets": {"table": "ODS_MALL_OMS_CART", "registered_date_column": "C.UPD_DT", "recent_default_days": 30},
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
    # 캠페인 반응 팩트(MCS_CAMP_MBR_RSPN_FT) 타겟. 이 최상위 키가 기본값에 있어야 파일의 rich config
    # (campaign_join/aggregate_metrics 등)가 로더에서 머지된다([[member-target-filters-loader-key-whitelist]]).
    # 그전엔 키가 없어 JSON 의 campaign_join·유효캠페인 조건이 조용히 무시되고 빌더 인라인 기본값만 살았다.
    # campaign_date_column 은 '최근 N개월 캠페인' 창 기준(반응 팩트엔 범용 반응일자 컬럼이 없어 캠페인
    # 마스터 시작일 CAMP_SDATE 가 유일한 날짜 기준), response_predicate 는 일반형 '반응'의 정의
    # (발송성공=도달은 반응이 아니라 제외; 오퍼/구매 반응만), campaign_key_expression 은 반응한 서로 다른
    # 캠페인 수를 세는 DISTINCT 식이다.
    "campaign_response_targets": {
        "table": "MCS_CAMP_MBR_RSPN_FT",
        "alias": "R",
        "member_column": "MBR_NO",
        "member_join": {"left": "TRY_CAST(R.MBR_NO AS BIGINT)", "right": "B.MEMBER_NO"},
        "campaign_join": {
            "table": "Z_CAMPAIGN",
            "alias": "ZC",
            "conditions": ["ZC.CAMP_ID = R.CAMP_ID", "ZC.CAMP_EXEC_NO = R.CAMP_EXEC_NO"],
        },
        "target_group_condition": {"column": "R.CGRP_TYPE_CD", "value": "T"},
        "valid_campaign_condition": {"expression": "ISNULL(ZC.CANCEL_YN, 'N') = 'N'"},
        "campaign_date_column": "CAMP_SDATE",
        "campaign_key_expression": "CONCAT(R.CAMP_ID, ':', R.CAMP_EXEC_NO)",
        "response_predicate": "(R.OFFR_RSPN_YN = 'Y' OR R.BUY_RSPN_YN = 'Y')",
        # 접촉(발송) 성공의 소스는 반응 팩트가 아니라 셀 발송 대상 명단이다 — 반응 팩트는 반응자
        # 중심 적재라(데모는 구매반응자뿐) '발송 성공 & 구매반응 없음'이 구조적으로 공집합이 된다.
        "contact_member_list": {
            "table": "Z_CAMP_MBR",
            "alias": "M",
            "member_join": {"left": "TRY_CAST(M.MBR_NO AS BIGINT)", "right": "B.MEMBER_NO"},
        },
    },
    # 셀 단위 비율 타겟: "발송 성공률 높고 구매율 낮은 셀의 회원". 분모는 셀 발송 대상 명단
    # Z_CAMP_MBR(회원별 접촉성공 CONTAC_SUCC_YN 포함)이고, 구매율 분자는 반응 팩트의 구매반응 행이다 —
    # 반응 팩트 단독으로는 불가(전 행이 구매반응자라 분모가 없어 구매율이 항상 100%). '높은/낮은' 같은
    # 막연한 표현은 여기 기본 임계로 컴파일한다(명시 % 는 그대로).
    "cell_rate_targets": {
        "member_table": "Z_CAMP_MBR",
        "alias": "M",
        "member_column": "MBR_NO",
        "member_join": {"left": "TRY_CAST(M.MBR_NO AS BIGINT)", "right": "B.MEMBER_NO"},
        "cell_alias": "M2",
        "cell_subquery_alias": "CELL",
        "cell_keys": ["CAMP_ID", "CAMP_EXEC_NO", "CELL_NODE_ID"],
        "contact_success_column": "CONTAC_SUCC_YN",
        "response_join": {"table": "MCS_CAMP_MBR_RSPN_FT", "alias": "R", "buy_predicate": "R.BUY_RSPN_YN = 'Y'"},
        "vague_high_default": 80,
        "vague_low_default": 10,
    },
    # AST 검증(validate_select_ast) 설정. 기본값에 키가 있어야 파일의 validation 섹션이 머지된다.
    # 별칭 허용 목록은 실제 빌더가 쓰는 별칭 전체(B 회원, A 카트, C/CP 상품, R 캠페인반응, O/OH/OD 주문,
    # M 지표/캠페인멤버, M2/CELL 셀 집계, S 보조속성, ZC 등)를 담는다 — 목록에 없는 별칭이 SQL 에
    # 나타나면 후보를 거부한다.
    "validation": {
        "allowed_table_aliases": ["A", "B", "C", "CELL", "CP", "D", "M", "M2", "O", "OD", "OH", "P", "R", "S", "ZC"],
        "max_conditions": 30,
        "max_or_branches": 10,
        "allow_raw_sql": False,
    },
    # 회원 잔액 임계값(적립금/예치금)용 numeric_filters. balance 카테고리만 _apply_balance_condition_filter 가
    # 소비한다(회원 테이블 컬럼 직접 비교). 기본값에 키가 있어야 파일의 numeric_filters 가 머지된다.
    "numeric_filters": [
        {"canonical": "deposit_balance", "category": "balance", "column": "B.DEPOSIT_BALANCE_AMT", "synonyms": ["예치금", "예치금 잔액"]},
        {"canonical": "carrot_balance", "category": "balance", "column": "B.CARROT_BALANCE_AMT", "synonyms": ["적립금", "당근 잔액", "포인트 잔액"]},
    ],
    "purchase_product_match_columns": [
        "CATEGORY", "CATEGORYL_NAME", "CATEGORYM_NAME", "CATEGORYS_NAME", "BRAND_NAME", "PRODUCT_NAME",
    ],
    "supported_condition_hint": "성별·연령·회원등급·휴면/미접속·최근 로그인 기간·수신동의(앱푸시/SMS/이메일)·상품 구매 이력",
    "region_density": {
        "granularity_tokens": ["동네", "지역", "시군구", "시도", "구"],
        "granularity_columns": {"시도": "SIDO"},
        "default_column": "SIGUNGU",
        "default_top_n": 5,
        "max_top_n": 30,
    },
    # 광역 권역어(수도권 등)를 구성 시도(SIDO 저장값)로 푼다. '수도권'은 단일 저장값이 아니라
    # 서울/경기/인천을 묶은 관용어라 값 인덱스에 없어 조용히 탈락한다 → 여기서 시도 IN 조건으로 확장한다.
    "macro_regions": {
        "column": "SIDO",
        "groups": {
            "수도권": ["서울", "경기", "인천"],
            "충청권": ["대전", "세종", "충북", "충남"],
            "호남권": ["광주", "전북", "전남"],
            "영남권": ["부산", "대구", "울산", "경북", "경남"],
            "강원권": ["강원"],
        },
    },
    "member_metric_ranking": {
        "granularity_tokens": ["고객님", "구매자", "사용자", "고객", "회원", "유저", "손님"],
        "default_top_n": 100,
        "max_top_n": 10000,
    },
    # '구매 금액 0원'의 의미 정책. 0원을 무구매(no_purchase, 주문 anti-join)와 동일하게 볼지 여부는
    # 도메인 결정 사항이라 코드가 단정하지 않고 이 플래그로 관리한다. 기본값은 '동일시하지 않음' —
    # 0원 결제(주문은 있으나 금액 0)와 평생 무주문은 다를 수 있으므로 기본은 clarification 으로 되묻는다.
    "zero_amount_semantics": {
        "maps_to_no_purchase": False,
    },
}


def _load_member_target_filters(path: Path = DEFAULT_MEMBER_TARGET_FILTERS_PATH) -> dict[str, Any]:
    """레지스트리 JSON 을 읽어 코드 기본값 위에 키 단위로 덮는다. 파일 부재/파손 시 기본값 그대로.

    JSON 의 **모든** 최상위 키를 머지한다 — 예전엔 코드 기본값에 선언된 키만 골라 담아서,
    base_entity/region_target/purchase_product_target 처럼 기본값 dict 에 없는 섹션이 파일에
    있어도 조용히 버려지는 '죽은 설정' 함정이 있었다(boolean_filters 사례와 동일 원인).
    단일 진실 소스는 JSON 이므로 전부 싣고, 소비 여부는 각 독자가 결정한다."""
    merged = dict(_DEFAULT_MEMBER_TARGET_FILTERS)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return merged
    if isinstance(payload, dict):
        merged.update(payload)
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


def _load_metric_registry() -> "metric_registry.MetricRegistry":
    """통합 지표 스펙 레지스트리(docs/data/metrics/*.json)를 읽는다. 스펙 파손/부재로 import 가
    통째로 죽지 않게 실패 시 빈 레지스트리로 강등한다 — 그러면 각 지표는 semantic_type/type 기반
    기본 단위로 폴백하므로(회귀 테스트가 '일' 단위 소실을 즉시 잡음) 조용한 크래시 대신 가시적 실패가 된다.

    신규 지표 추가 구조 개선안 P1(단위): _metric_window_grammar 가 numeric_filters 의 unit 대신 이
    레지스트리의 units 를 우선 읽는다. 이후 단계(zero/최근성/비율)에서 소비 범위를 넓힌다."""
    try:
        return metric_registry.MetricRegistry.load()
    except metric_registry.MetricSpecError:
        return metric_registry.MetricRegistry(specs=())


_METRIC_REGISTRY = _load_metric_registry()


def _load_segment_semantics() -> "segment_semantics.SegmentSemanticsRegistry | None":
    """쿠폰 도메인 의미 스펙(docs/data/segment_metrics.json + segment_operators.json)을 읽는다.

    스펙 파손/부재로 import 가 죽지 않게 실패 시 None 으로 강등한다 — 그러면 _apply_coupon_semantics 가
    무동작(no-op)이 되어 기존 경로가 유지된다(가시적 실패는 tests/test_coupon_semantics.py 가 잡는다)."""
    try:
        return segment_semantics.SegmentSemanticsRegistry.load()
    except segment_semantics.SegmentSemanticsError:
        return None


_SEGMENT_SEMANTICS = _load_segment_semantics()


def _load_requirement_registry() -> "semantic_requirements.RequirementRegistry | None":
    """공통 semantic requirement capability 레지스트리(docs/data/requirement_capabilities.json)를 읽는다.
    파손/부재 시 None 으로 강등(회계 계층이 무동작 → 기존 동작 유지). 가시적 실패는 테스트가 잡는다."""
    try:
        return semantic_requirements.RequirementRegistry.load()
    except semantic_requirements.RequirementCapabilityError:
        return None


_REQUIREMENT_REGISTRY = _load_requirement_registry()
# 쿠폰 미지원 판정이 더 구체적으로 대체할 수 있는 '일반 폴백' 미지원 사유(조용한/무관한 안내 교정).
_COUPON_OVERRIDABLE_REASONS = frozenset({"metric_not_resolved", "ranking_metric_unspecified"})
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


_MEMBER_DIALECT: SqlDialect | None = None


def _member_dialect() -> SqlDialect:
    """회원 타겟 결정론 빌더가 쓸 SQL 방언 어댑터(sql_dialect.py). 소스에 엔진 문법을 박지 않기
    위한 이식성 계층(docs/operations/db_portability_audit.md §4-A). 판별 우선순위:
    1) member_target_filters.json base_entity.dialect (설정이 자기 엔진을 명시)
    2) schema_catalog 의 회원 기준 테이블 방언(load_table_dialects — 카탈로그 도출)
    3) 'tsql' (현행 기본 — 실CRM 이 MSSQL이라 기존 출력과 문자열 동일 보장)
    """
    global _MEMBER_DIALECT
    if _MEMBER_DIALECT is None:
        base = _MEMBER_TARGET_FILTERS.get("base_entity")
        base = base if isinstance(base, dict) else {}
        name = base.get("dialect") if isinstance(base.get("dialect"), str) else None
        if not name:
            table = base.get("table")
            if isinstance(table, str) and table:
                try:
                    name = load_table_dialects().get(table)
                except Exception:
                    name = None
        _MEMBER_DIALECT = get_dialect(name or "tsql")
    return _MEMBER_DIALECT


# ── 회원 기준 테이블(base_entity) 접근자 ─────────────────────────────────────
# 회원 테이블명/별칭/회원키는 member_target_filters.json base_entity 가 소유한다(스키마 사실).
# 빌더는 반드시 이 헬퍼로 읽는다 — 소스에 'CRM_MB_BASEINFO'/'B.MEMBER_NO' 를 직접 박으면
# DB 스왑 시 소스를 고쳐야 한다(docs/operations/db_portability_audit.md §4-B).


def _member_base_entity() -> dict[str, Any]:
    base = _MEMBER_TARGET_FILTERS.get("base_entity")
    if isinstance(base, dict):
        return base
    default = _DEFAULT_MEMBER_TARGET_FILTERS.get("base_entity")
    return default if isinstance(default, dict) else {}


def _member_table() -> str:
    return str(_member_base_entity().get("table") or "CRM_MB_BASEINFO")


def _member_alias() -> str:
    return str(_member_base_entity().get("alias") or "B")


def _member_key_column() -> str:
    return str(_member_base_entity().get("member_key") or "MEMBER_NO")


def _member_login_id_column() -> str:
    return str(_member_base_entity().get("login_id_key") or "MEMBER_ID")


def _member_from_clause(alias: str | None = None) -> str:
    """'FROM CRM_MB_BASEINFO B' 형태의 회원 기준 FROM 절(별칭 오버라이드 가능)."""
    return f"FROM {_member_table()} {alias or _member_alias()}"


def _member_key_select(alias: str | None = None) -> str:
    """'B.MEMBER_NO AS CUST_ID' — 회원키 SELECT 관례. CUST_ID 는 앱 결과 계약(스키마 무관)이라 고정."""
    return f"{alias or _member_alias()}.{_member_key_column()} AS CUST_ID"


def _member_grade_column() -> str:
    """'B.EMART_GRADE_CD' — 등급 컬럼은 eq_filters(grade 범주)가 소유(별칭 접두 포함)."""
    for _canonical, (category, column, _value) in MEMBER_EQ_FILTERS.items():
        if category == "grade":
            return column
    return f"{_member_alias()}.EMART_GRADE_CD"


def _member_grade_select() -> str:
    """'B.EMART_GRADE_CD AS member_grade' — 등급 SELECT 관례(member_grade 는 앱 결과 계약)."""
    return f"{_member_grade_column()} AS member_grade"


def _member_region_short_columns() -> tuple[str, str]:
    """(시도, 시군구) 짧은 컬럼명 — region_target.columns 레지스트리 소유."""
    config = _MEMBER_TARGET_FILTERS.get("region_target")
    columns = (config or {}).get("columns") if isinstance(config, dict) else None
    columns = columns if isinstance(columns, dict) else {}
    sido = str(columns.get("sido") or "B.SIDO").split(".")[-1]
    sigungu = str(columns.get("sigungu") or "B.SIGUNGU").split(".")[-1]
    return sido, sigungu


def _member_activity_predicate(days: int) -> str:
    d = _member_dialect()
    return f"(B.LAST_LOGIN_DATE IS NOT NULL AND B.LAST_LOGIN_DATE <= {d.char8_cutoff(days)})"


def _member_active_state_predicate(alias: str = "B") -> str:
    """정상 회원 한정(탈퇴/휴면 제외) 술어. 기준 컬럼/값은 member_target_filters.json 의 active_state."""
    return active_member_predicate(alias, DEFAULT_MEMBER_TARGET_FILTERS_PATH)


def _member_policy_predicates(query_plan: dict[str, Any], alias: str = "B") -> list[str]:
    """Query Plan에 명시된 회원 정책 필터를 SQL 술어로 렌더한다.

    오래된 수동 plan처럼 계약이 없는 호출자는 기존 정상회원 기본값을 유지한다. 계약이 있으면 빌더가
    자체 판단으로 조건을 더하지 않고 ``appliedPolicyFilters``만 따른다.
    """
    contract = query_plan.get("member_policy")
    if not isinstance(contract, dict):
        return [_member_active_state_predicate(alias)]
    filters = contract.get("appliedPolicyFilters")
    if not isinstance(filters, list):
        return []
    predicates: list[str] = []
    for item in filters:
        if not isinstance(item, dict) or item.get("id") != "policy_active_member":
            continue
        column = str(item.get("column") or "").split(".")[-1]
        operator = item.get("operator")
        value = item.get("value")
        if not column:
            continue
        if operator == "eq" and isinstance(value, str):
            predicates.append(f"{alias}.{column} = '{value.replace(chr(39), chr(39) * 2)}'")
        elif operator == "in" and isinstance(value, list) and value:
            quoted = ", ".join("'" + str(v).replace("'", "''") + "'" for v in value)
            predicates.append(f"{alias}.{column} IN ({quoted})")
    return predicates


def _member_birthday_predicate(granularity: str = "day", alias: str = "B") -> str:
    """생일 타겟 술어. BIRTHDAY 는 nvarchar(8) 'YYYYMMDD' 문자열이라 년도까지 비교하면 안 되고,
    월일(MMDD)만 오늘과 비교한다(day). '이달 생일'은 월(MM)만 비교(month). 컬럼은 birthday_target 설정."""
    config = _MEMBER_TARGET_FILTERS.get("birthday_target")
    if not isinstance(config, dict):
        config = _DEFAULT_MEMBER_TARGET_FILTERS["birthday_target"]
    column = config.get("column") or "BIRTHDAY"
    length = 2 if granularity == "month" else 4  # month: MM(2자리), day: MMDD(4자리)
    col = f"{alias}.{column}"
    d = _member_dialect()
    today = d.char8_today()  # 'YYYYMMDD'
    # char8 가드로 8자리 정상값만 비교(널/이상치 제외).
    return (
        f"({d.char8_valid(col)} "
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
    d = _member_dialect()
    if config.get("anchor") == "getdate":
        anchor = d.now()
    else:
        # 적재 데이터 최신 가입일 기준(서브쿼리). MAX 는 널/공백을 무시하고, 포맷 고정이라 문자열 MAX = 최신일.
        anchor = d.parse_char8(f"(SELECT MAX({column}) FROM {table} WHERE {d.str_len(column)} = 8)")
    boundary = d.char8_cutoff(days, anchor)
    return f"({d.char8_valid(col)} AND {col} >= {boundary})"


def _member_recent_login_predicate(days: int, alias: str = "B") -> str:
    """최근 로그인 타겟 술어(긍정형). LAST_LOGIN_DATE(nvarchar(8) 'YYYYMMDD') 가 기준일로부터
    최근 N일 이내인 회원 — 미접속(_member_activity_predicate, `<=`)의 대칭(`>=`)이다.

    기준일(anchor)은 recent_login_target.anchor 설정: 기본 'getdate'(실제 오늘) — 적재 데이터가
    과거라 0명이 나올 수 있어도 조건 표현이 가능하면 요청 기간을 왜곡하지 않고 그대로 건다.
    'data_max' 는 적재 데이터 최신 접속일(MAX(LAST_LOGIN_DATE)) 기준(데모 시연용 옵션).
    포맷 고정 8자리라 문자열 대소 = 날짜 대소이고, LEN 가드로 이상치를 제외한다."""
    config = _MEMBER_TARGET_FILTERS.get("recent_login_target")
    if not isinstance(config, dict):
        config = _DEFAULT_MEMBER_TARGET_FILTERS["recent_login_target"]
    column = config.get("column") or "LAST_LOGIN_DATE"
    table = config.get("table") or "CRM_MB_BASEINFO"
    col = f"{alias}.{column}"
    d = _member_dialect()
    if config.get("anchor") == "data_max":
        anchor = d.parse_char8(f"(SELECT MAX({column}) FROM {table} WHERE {d.str_len(column)} = 8)")
    else:
        anchor = d.now()
    boundary = d.char8_cutoff(days, anchor)
    return f"({d.char8_valid(col)} AND {col} >= {boundary})"


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
    # '곳에': "브랜드가 X인 곳에 쿠폰을 …" 같은 장소형 오디언스 표현('에게'가 아니라 '에'만 붙음).
    "audience_direction_markers": ["에게", "한테", "께", "대상으로", "타겟으로", "타깃으로", "곳에"],
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
            "브랜드 언급은 브랜드 조건 그대로 유지한다('브랜드가 X인 곳'의 X는 브랜드명 — 지역/거주 조건으로 바꾸지 않는다).",
            "targeting_label 에서는 이 캠페인이 보내거나 파는 상품·혜택(쿠폰/할인 등), 행동 표현(보내다/뿌리다/판매/만들다), 단어 '캠페인', 지금 이 캠페인이 쓸 발송 채널을 뺀다.",
            "단, 상품 '구매/구입 이력'은 오디언스 조건이므로 targeting_label 에 유지한다(예: '기저귀 구매 고객').",
            "과거 캠페인 반응(발송/접촉 성공, 오퍼·구매 반응, 쿠폰 사용)은 오디언스 조건이므로 targeting_label 에 유지한다 — '캠페인'·'발송' 단어가 들어 있어도 뺀다 아니다(예: '발송은 성공했지만 구매하지 않은 회원' -> '발송 성공 후 미구매 회원').",
            "기간 조건('장바구니에 일주일 이상 담아둔', '30일 이상 미접속')도 오디언스 조건이므로 기간과 방향어를 그대로 유지한다.",
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


def _targeting_prompt(text: str) -> str:
    """BFF 발송 채널 접미어를 제외한 타겟팅 파이프라인 입력을 반환한다."""
    targeting, _channel_suffix = _split_channel_suffix(text if isinstance(text, str) else "")
    return targeting.strip()


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
# Product followed by an explicit quantity/frequency before the purchase verb:
# ``기저귀를 2개 이상 구매``.  The ordinary immediate-object pattern would
# otherwise capture ``이상`` as the product and silently lose the real scope.
_PURCHASE_OBJECT_QUANTIFIED_PATTERN = re.compile(
    r"(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})\s*(?:을|를)\s*"
    r"\d+(?:\.\d+)?\s*(?:개|회|번|건|장|종|가지|종류|품목|매|권)?\s*"
    r"(?:이상|이하|미만|초과|정확히)?\s*(?:구매|구입|주문)",
    re.IGNORECASE,
)

# 상품이 아닌 일반 명사(예: "알로루 브랜드 '상품' 구매한")가 구매 동사 바로 앞에 오면, 위 단일 토큰
# 캡처가 그 일반명사를 상품명으로 오인해 LIKE '%상품%' 같은 무의미하게 넓은 매칭을 만든다.
_GENERIC_PRODUCT_NOUNS = {"상품", "제품", "물건", "품목", "굿즈", "아이템", "브랜드"}
# 일반명사 앞의 실제 브랜드/상품명 재시도 캡처("알로루 브랜드 상품 구매한" → '알로루').
_PURCHASE_OBJECT_BRAND_PATTERN = re.compile(
    r"(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})\s+"
    r"(?:브랜드|상품|제품|물건|품목|굿즈|아이템)(?:\s*(?:브랜드|상품|제품|물건|품목|굿즈|아이템))*\s*(?:을|를)?\s*(?:구매|구입)",
    re.IGNORECASE,
)

# "브랜드가 알로루인 곳/고객" 같은 계사(copula) 형 브랜드 언급. 구매 동사가 없어도 '그 브랜드(상품)
# 구매 고객' 타겟으로 본다 — 브랜드에 쿠폰을 뿌린다 = 그 브랜드 구매 이력 고객에게 뿌린다.
# 연결형("브랜드가 알로루면서/이면서/이고 …")도 같은 계사 표현이다. object 는 lazy 로 최소 매칭해
# 어미 알로루+'이면서'를 '알로루이'+'면서'로 쪼개 잡지 않게 한다(어미 대안은 긴 것 우선).
_BRAND_COPULA_PATTERN = re.compile(
    r"브랜드(?:가|는|명이|명은)\s*(?P<object>[0-9A-Za-z가-힣_+\-]{1,40}?)"
    r"(?:이면서|이거나|인데|이고|이며|면서|인)(?![0-9A-Za-z가-힣])",
    re.IGNORECASE,
)

# "상품명이/제품명이 X인" 같은 계사형 상품명 언급. 브랜드 계사와 대칭이며 반드시 '명'(name)을 요구해
# "상품이 좋은" 처럼 상품명이 아닌 표현을 배제한다. 매칭되면 PRODUCT_NAME 컬럼만 좁혀 매칭한다.
_PRODUCT_NAME_COPULA_PATTERN = re.compile(
    r"(?:상품|제품)명(?:이|은)\s*(?P<object>[0-9A-Za-z가-힣_+\-]{1,40}?)"
    r"(?:이면서|이거나|인데|이고|이며|면서|인)(?![0-9A-Za-z가-힣])",
    re.IGNORECASE,
)


# 구매 '횟수/조건' 수식어는 상품명이 아니다(예: '2회 이상 구매' 의 '이상'). 게이트가 상품 조건으로 오인해
# 재작성을 헛되이 폐기하지 않도록 제외한다.
_PURCHASE_SIGNAL_STOPWORDS = {"이상", "이하", "미만", "초과", "회", "번", "건", "원", "개", "명", "이력", "내역", "경험", "동안", "번째"}
# 구매 동사 앞에 붙는 금액/가격 지표·수식어는 상품명이 아니다. 이 어휘는 aggregate/member metric
# 트랙이 소유하며, 상품 LIKE 로 흘리면 "고액 구매 고객"이 "고액"이라는 상품을 산 고객으로 뒤집힌다.
# 특정 프롬프트 예외가 아니라 금액 수준/지표 표면형 전체를 같은 비상품 범주로 다룬다.
_PURCHASE_VALUE_QUALIFIERS = {
    "고액", "소액", "고가", "저가", "고금액", "저금액",
    "금액", "구매금액", "주문금액", "결제금액", "평균구매금액", "평균주문금액", "평균결제금액", "객단가",
}
# 수량/횟수 토큰('2개', '3회', '5건', 맨숫자 '2')도 상품명이 아니라 개수 조건이다. 상품명 추출
# (_sanitize_purchase_object)에서 걸러 '2개 이상 상품 구입' 의 '2개'/'이상' 이 LIKE 로 새지 않게 한다.
_QUANTITY_COUNT_TOKEN = re.compile(r"^\d+(?:개|회|번|건|원|명|장|종|가지|종류|품목|매|권)?$")

# 상품 나열 연결어("기저귀와 건강식품", "우유, 빵과 계란"). 와/과/랑/이랑 은 앞 명사에 붙는 접속조사라
# 명사 내부의 과/와(과자·사과·와플)와 구분하려 '앞이 한글 + 뒤가 공백'일 때만 연결어로 본다(lookbehind +
# 뒤 \s+). 및/그리고/쉼표는 독립형이라 앞뒤 공백만 본다. 이 규칙이 있어야 단일 상품('아기 기저귀')은 공백이
# 있어도 쪼개지지 않고, 진짜 나열('기저귀와 건강식품')만 상품별로 분리된다.
_PRODUCT_CONJUNCTION_RE = re.compile(
    r"(?:(?<=[가-힣])(?:와|과|랑|이랑)\s+|\s*(?:및|그리고)\s+|\s*[,、]\s*)"
)
# '상품 나열' 사슬(연결어로 이어진 2~5개 상품)이 목적격 조사(을/를)로 끝나는 형태를 통째로 잡는다. 연결어를
# 최소 1개 요구해 단일 상품은 여기 안 걸리고(기존 _PURCHASE_OBJECT_PATTERN 담당), 목적격 조사로 끝나야
# 나열 대상이 '구매 목적어'임을 보장한다. 구매 동사가 사슬 바로 뒤가 아니라 개수어를 사이에 두고 떨어져
# 있어도('기저귀와 건강식품을 2회 이상 구매') 목적격 조사 앵커로 잡히므로, 호출부는 구매 신호가 있을 때만
# 이 패턴을 쓴다(비구매 나열 '서울과 부산을 대상으로'의 오검출을 구매 신호 게이트로 차단).
_PURCHASE_OBJECT_CHAIN_PATTERN = re.compile(
    r"(?P<chain>[0-9A-Za-z가-힣_+\-]{1,40}"
    r"(?:(?:(?<=[가-힣])(?:와|과|랑|이랑)\s+|\s*(?:및|그리고)\s+|\s*[,、]\s*)"
    r"[0-9A-Za-z가-힣_+\-]{1,40}){1,4})"
    r"\s*(?:을|를)(?![0-9A-Za-z가-힣])"
    # 이 목적격 조사가 '구매'의 목적어임을 보장 — 뒤로 짧은 필러(개수·비교어·부사, 다른 목적격 조사 을/를은
    # 불허)를 지나 구매 동사가 나와야 한다. 이 앵커가 없으면 '남성과 여성을 대상으로 기저귀를 구매'에서
    # 데모그래픽 나열('남성과 여성을')을 상품으로 오검출한다(그 경우 뒤의 '기저귀를'의 를이 필러를 막아 거부).
    r"(?=[^을를]{0,15}?(?:구매|구입|주문|샀|산(?=\s)))",
    re.IGNORECASE,
)


def _split_product_terms(value: str) -> list[str]:
    """상품 나열 문자열을 개별 상품 표면어 리스트로 나눈다('기저귀와 건강식품'→['기저귀','건강식품']).

    연결어(_PRODUCT_CONJUNCTION_RE)가 없으면 정제값 1개만 반환한다 — 공백 포함 단일 상품명('아기 기저귀')을
    억지로 쪼개지 않기 위함이다. 각 조각은 _sanitize_purchase_object 로 조사/수량어를 털고, 일반명사(상품/제품)와
    중복은 제외한다."""
    if not isinstance(value, str) or not value.strip():
        return []
    terms: list[str] = []
    for part in _PRODUCT_CONJUNCTION_RE.split(value):
        cleaned = _sanitize_purchase_object(part)
        if cleaned and cleaned not in _GENERIC_PRODUCT_NOUNS and cleaned not in terms:
            terms.append(cleaned)
    return terms


def _extract_purchase_object_list(query: str) -> list[dict[str, Any]]:
    """'A와 B를 (… 구매)' 나열형에서 상품별 {value, kind} 리스트를 뽑는다(연결어 사슬이 없으면 빈 리스트).

    값은 _canonicalize_product_term 으로 DB 표기 보정하고, 실DB 브랜드명과 일치하면 kind='brand' 로 표시해
    빌더가 컬럼을 좁힐 수 있게 한다. 단일 상품은 여기서 잡지 않는다(_apply_purchase_object_filter 단일 경로 소유).
    사슬 패턴이 목적격 조사만 앵커로 쓰므로, 비구매 나열('서울과 부산을')을 배제하려 구매 신호가 있을 때만 쓴다."""
    if not _has_purchase_history_signal(query):
        return []
    match = _PURCHASE_OBJECT_CHAIN_PATTERN.search(query)
    if not match:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for term in _split_product_terms(match.group("chain")):
        canonical = _canonicalize_product_term(term)
        if not canonical or canonical in _GENERIC_PRODUCT_NOUNS or canonical in seen:
            continue
        seen.add(canonical)
        result.append({"value": canonical, "kind": "brand" if _is_known_brand_term(canonical) else None})
    return result


def _purchase_object_signals(text: str) -> set[str]:
    """텍스트에서 상품 구매 이력 조건의 상품명(canonical 소문자) 집합을 뽑는다(게이트 비교용)."""
    objects: set[str] = set()
    for match in _PURCHASE_OBJECT_PATTERN.finditer(text or ""):
        purchase_object = _sanitize_purchase_object(match.group("object"))
        if purchase_object and purchase_object not in _PURCHASE_SIGNAL_STOPWORDS and not purchase_object.isdigit():
            objects.add(purchase_object.casefold())
    # 나열형('A와 B를 구매한')은 단일 패턴이 마지막 상품만 잡으므로 사슬 상품도 함께 넣는다(게이트 누락 방지).
    # 목적격 조사만 앵커라 비구매 나열 오검출을 막으려 구매 신호가 있을 때만 본다(_extract_purchase_object_list 와 동일).
    if _has_purchase_history_signal(text or ""):
        chain = _PURCHASE_OBJECT_CHAIN_PATTERN.search(text or "")
        if chain:
            for term in _split_product_terms(chain.group("chain")):
                objects.add(term.casefold())
    return objects


# 브랜드 언급 신호(게이트 비교용) 추출 패턴. 계사형("브랜드가 X인")에 더해 "X 브랜드"/"브랜드 X"
# 인접형까지 본다 — 재작성이 표현형을 바꿔도(예: "알로루 브랜드 구매 고객") 브랜드 언급이면 보존으로 인정.
_BRAND_ADJACENT_BEFORE = re.compile(r"(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})\s*브랜드", re.IGNORECASE)
_BRAND_ADJACENT_AFTER = re.compile(r"브랜드\s+(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})", re.IGNORECASE)


def _brand_object_signals(text: str) -> set[str]:
    """텍스트에서 '브랜드 조건'으로 파싱되는 브랜드명 집합을 뽑는다(재작성 게이트 비교용).

    구매 상품(_purchase_object_signals)의 '문자열 존재' 검사와 달리 브랜드는 의미 검사가 필요하다:
    재작성이 "브랜드가 알로루인 곳" → "알로루에 거주하는 고객"으로 바꾸면 '알로루' 문자열은 남지만
    브랜드 조건은 거주지 조건으로 변질된다. 브랜드명이 다시 브랜드 언급으로 파싱될 때만 보존으로 본다."""
    objects: set[str] = set()
    for pattern in (_BRAND_COPULA_PATTERN, _BRAND_ADJACENT_BEFORE, _BRAND_ADJACENT_AFTER):
        for match in pattern.finditer(text or ""):
            brand = _sanitize_purchase_object(match.group("object"))
            if brand and brand not in _GENERIC_PRODUCT_NOUNS and brand not in _PURCHASE_SIGNAL_STOPWORDS and not brand.isdigit():
                objects.add(brand.casefold())
    return objects


def _rewrite_guard_enabled() -> bool:
    """재작성 검증 게이트 on/off(환경변수 PROMPT_REWRITE_GUARD, 기본 on)."""
    return os.getenv("PROMPT_REWRITE_GUARD", "true").casefold() not in {"0", "false", "off", "no"}


def _sql_semantic_verify_enabled() -> bool:
    """최종 SQL↔원문 의미 검증 게이트 on/off(환경변수 SQL_SEMANTIC_VERIFY, 기본 on).

    정규식 파서의 조용한 드롭·의미 반전(예: '구매 이력이 없는'을 EXISTS 구매로 뒤집음)은 SQL 을 plan 과
    대조하는 결정론 검증(coverage/intent_scope)으로는 못 잡는다 — plan 자체가 틀렸기 때문. 이 게이트만
    유일하게 **원문 NL 과 최종 SQL 을 직접 대조**해(어느 계층이 원인이든) 틀린 SQL 의 조용한 출고를 막는다."""
    return os.getenv("SQL_SEMANTIC_VERIFY", "true").casefold() not in {"0", "false", "off", "no"}


def _semantic_validation_v2_mode() -> str:
    """의미 검증 v2(AST 기반) 동작 모드(환경변수 SEMANTIC_VALIDATION_V2).

    off(기본): 실행 안 함. shadow: 신규 검증을 병행 실행해 결과/차이를 sql_result 에 싣되(트레이스·로깅)
    사용자 응답 판정은 기존 게이트를 그대로 쓴다. enforce: 신규 검증이 status='fail' 이면(구체 근거 있는
    경우에만) 기존 게이트가 통과시킨 SQL 도 clarification 으로 전환한다. review 는 어느 모드에서도 차단하지
    않는다(비차단 자문). 신규 파이프라인은 정상 SQL 오탐(false-fail) 억제가 목표라 기본은 안전한 shadow 이하다."""
    mode = os.getenv("SEMANTIC_VALIDATION_V2", "off").strip().casefold()
    return mode if mode in {"off", "shadow", "enforce"} else "off"


def _run_semantic_validation_v2(
    original_query: str,
    sql: str,
    query_plan: dict[str, Any],
    dialect: str | None,
    context_nodes: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """의미 검증 v2 를 shadow/enforce 모드로 실행한다(예외는 삼키지 않되 흐름은 깨지 않게 내부 폴백).

    LLM 근거 탐색은 붙이지 않는다(규칙 우선, 결정론) — 신규 게이트의 가치는 규칙 기반 동치 판정이다."""
    try:
        import semantic_validation
    except Exception as exc:  # noqa: BLE001 - 모듈/의존성(sqlglot) 부재 시 조용히 비활성
        return {"ran": False, "error": f"import_failed:{type(exc).__name__}"}
    evidence = _retrieved_evidence_fingerprints(context_nodes)
    return semantic_validation.run_shadow_validation(
        original_query, sql, query_plan, dialect=dialect, retrieved_evidence=evidence,
        field_columns=_v2_field_columns(),
        membership_tables=_v2_membership_tables(),
        code_filter_values=frozenset(MEMBER_EQ_FILTERS.keys()),
        lifecycle_columns=_v2_lifecycle_columns())


def _v2_lifecycle_columns() -> dict[str, str]:
    """lifecycle canonical 값 → 실제 코드 컬럼(값마다 다름: 등급/회원상태/수신동의 등). MEMBER_EQ_FILTERS 소유."""
    out: dict[str, str] = {}
    for canonical, (_category, column, _value) in MEMBER_EQ_FILTERS.items():
        if isinstance(canonical, str) and isinstance(column, str):
            out[canonical] = column
    return out


def _v2_field_columns() -> dict[str, str]:
    """의미 검증 v2 스펙 빌더에 넘길 논리필드→실제 SQL 컬럼 매핑(범주형 값 확장 정렬용).

    SQL 생성기와 같은 레지스트리(MEMBER_EQ_FILTERS·등급/로그인 컬럼)에서 뽑아 검증기가 생성기와 동일한
    컬럼을 바라보게 한다(§7 근거 정합의 컬럼판). 'B.' 접두어는 매핑기가 short-field 로 비교하므로 그대로 둔다."""
    columns: dict[str, str] = {}
    for _canonical, (category, column, _value) in MEMBER_EQ_FILTERS.items():
        if category == "gender" and "gender" not in columns:
            columns["gender"] = column
    try:
        columns["lifecycle"] = _member_grade_column()
    except Exception:  # noqa: BLE001 - 등급 컬럼 조회 실패는 치명적이지 않다(폴백은 값 접두어)
        pass
    login_cfg = _MEMBER_TARGET_FILTERS.get("recent_login_target")
    login_col = (login_cfg or {}).get("column") if isinstance(login_cfg, dict) else None
    columns["last_login"] = str(login_col or "LAST_LOGIN_DATE")
    return columns


def _v2_membership_tables() -> dict[str, str]:
    """논리 팩트(orders/cart/campaign)→실제 테이블명. 생성기와 같은 레지스트리에서 뽑아 EXISTS/JOIN
    매칭을 실제 테이블로 좁힌다(카트 테이블·주문 테이블·캠페인 반응 팩트)."""
    tables: dict[str, str] = {}
    try:
        cart_cfg = _cart_targets_registry()
        if isinstance(cart_cfg, dict) and cart_cfg.get("table"):
            tables["cart"] = str(cart_cfg["table"])
    except Exception:  # noqa: BLE001
        pass
    campaign_cfg = _MEMBER_TARGET_FILTERS.get("campaign_response_targets")
    if isinstance(campaign_cfg, dict) and campaign_cfg.get("table"):
        tables["campaign"] = str(campaign_cfg["table"])
    # 주문(구매) 팩트는 몰 구매기준 헤더 테이블을 관례로 쓴다([[crmdw-order-tables]]).
    tables.setdefault("orders", "CRM_SL_ORDERHEADERMALL")
    return tables


def _retrieved_evidence_fingerprints(context_nodes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """§7 RAG 근거 고정: 생성에 사용한 context_nodes 를 {id, version, content_hash} 지문으로 남긴다.

    검증기가 동일 근거를 참조할 수 있게 하는 최소 고정. version 이 없으면 '0', content_hash 는 요약 텍스트의
    안정 해시(hashlib)로 채운다(민감정보는 넣지 않는다 — id/타입/버전/해시만)."""
    out: list[dict[str, Any]] = []
    for node in (context_nodes or [])[:50]:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id") or node.get("node_id") or node.get("term_id")
        if not node_id:
            continue
        version = str(node.get("version") or node.get("schema_version") or "0")
        basis = str(node.get("content") or node.get("text") or node.get("description") or "")
        content_hash = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12] if basis else ""
        out.append({"id": str(node_id), "version": version, "content_hash": content_hash})
    return out


# 수신동의 신호(게이트 비교용) 사람이 읽는 라벨. canonical:극성(+동의/-거부) → 라벨.
_CONSENT_SIGNAL_LABELS = {
    "app_push_optin:+": "앱푸시 수신 동의", "app_push_optin:-": "앱푸시 수신 거부",
    "sms_optin:+": "SMS 수신 동의", "sms_optin:-": "SMS 수신 거부",
    "email_optin:+": "이메일 수신 동의", "email_optin:-": "이메일 수신 거부",
    "marketing_optin:+": "마케팅 수신 동의", "marketing_optin:-": "마케팅 수신 거부",
}


def _consent_signals(text: str) -> set[str]:
    """텍스트에서 '<채널> 수신 동의/거부' 신호를 'canonical:극성'(+동의/-거부) 문자열로 뽑는다.

    _apply_channel_consent_filter 와 같은 문맥 판정(_consent_context_signals — 인접형·나열 공유형)을 써서,
    재작성이 수신동의 조건을 조용히 지우거나 극성을 뒤집으면(동의→미동의) 게이트가 소실로 잡게 한다."""
    return {f"{canonical}:{polarity}" for canonical, polarity in _consent_context_signals(text).items()}


# 회원 Y/N 플래그(활동회원·블랙리스트) 신호 라벨. canonical:극성(+포함/-제외) → 사람이 읽는 라벨.
_MEMBER_FLAG_SIGNAL_LABELS = {
    "active_member:+": "활동 회원", "active_member:-": "비활동 회원",
    "blacklisted:+": "블랙리스트", "blacklisted:-": "블랙리스트 제외",
}


def _member_flag_signals(text: str) -> set[str]:
    """텍스트에서 '활동회원'·'블랙리스트' 신호를 'canonical:극성'(+포함/-제외) 문자열로 뽑는다.

    attribute_token 그룹 "member_flag"의 선언형 스펙(표면어·부정 문법)을 그대로 재사용한다 — 그룹 선언을
    JSON(attribute_token_groups.json)에서 고치면 이 신호 감지도 함께 따라가 단일 소스가 유지된다. 재작성이
    '블랙리스트가 아니면서' 같은 조건을 조용히 지우거나 극성을 뒤집으면 게이트가 소실로 잡게 한다."""
    group = _attribute_token_groups().get("member_flag")
    if group is None:
        return set()
    compact = (text or "").replace(" ", "").casefold()
    signals: set[str] = set()
    for canonical, terms in group.canonicals:
        term_alt = "(?:" + "|".join(re.escape(term) for term in _attribute_terms(canonical, terms)) + ")"
        if group.neg and re.search(term_alt + group.neg, compact):
            signals.add(f"{canonical}:-")
        elif re.search(term_alt + group.pos, compact):
            signals.add(f"{canonical}:+")
    return signals


# 캠페인 반응(발송/접촉 성공·오퍼·구매 반응·쿠폰) 신호 canonical → 사람이 읽는 라벨.
_CAMPAIGN_RESPONSE_SIGNAL_LABELS = {
    "campaign_contact": "발송/접촉 성공",
    "offer_response": "오퍼 반응",
    "buy_response": "캠페인 반응 구매",
    "coupon_used": "쿠폰 사용",
}


def _campaign_response_signals(text: str) -> set[str]:
    """텍스트에서 캠페인 반응(발송/접촉 성공·오퍼·구매 반응·쿠폰) 신호를 canonical 집합으로 뽑는다.

    _apply_campaign_response_filter 와 같은 표면어 매칭(_CAMPAIGN_RESPONSE_PATTERNS)을 써서, 재작성이
    '캠페인에서 발송은 성공했지만' 같은 과거 반응 조건을 조용히 지우면 게이트가 소실로 잡게 한다.
    (재작성기가 '캠페인'·'발송' 단어를 발송 채널로 오해해 통째로 삭제하던 사고 방지.)"""
    compact = (text or "").replace(" ", "").casefold()
    return {canonical for canonical, _predicate, pattern in _CAMPAIGN_RESPONSE_PATTERNS if pattern.search(compact)}


def _prompt_signal_signature(text: str) -> dict[str, set[str]]:
    """재작성 전후 비교용 '핵심 신호' 서명.

    재작성은 표현(구어체·오타)만 다듬어야 하므로 아래 리터럴 신호는 반드시 보존돼야 한다:
      - numbers: 연령·일수·횟수·금액 등 숫자(천단위 콤마는 제거 후 추출)
      - genders: 성별 표면형에서 해석한 canonical(female/male)
      - purchases: 상품 구매 이력 조건의 상품명(예: '화장품 구매' → 화장품)
      - durations: 기간 표현을 일수로 정규화한 값(숫자 없는 '일주일'도 7 로 잡힌다)
    """
    compact = text or ""
    # "30,000" 같은 천단위 콤마는 하나의 숫자로 보도록 자릿수 사이 콤마만 제거한다.
    digits_only = re.sub(r"(?<=\d),(?=\d)", "", compact)
    numbers = set(re.findall(r"\d+", digits_only))
    genders = {canonical for surface, canonical in _GENDER_SURFACE_TO_CANONICAL.items() if surface in compact}
    return {
        "numbers": numbers,
        "genders": genders,
        "purchases": _purchase_object_signals(compact),
        "brands": _brand_object_signals(compact),
        "consents": _consent_signals(compact),
        "member_flags": _member_flag_signals(compact),
        "campaign_responses": _campaign_response_signals(compact),
        "durations": _duration_days_signals(compact),
    }


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
    # 브랜드 조건은 문자열 존재가 아니라 '의미'로 판정한다: 재작성본에서 그 이름이 다시 브랜드 언급
    # (또는 구매 상품 조건)으로 파싱돼야 보존이다. "브랜드가 알로루인 곳"→"알로루에 거주하는 고객"처럼
    # 이름은 남지만 브랜드→거주지로 변질되는 환각을 잡는다.
    preserved_after = after["brands"] | after["purchases"]
    for brand in sorted(before["brands"]):
        if brand not in preserved_after:
            dropped.append(f"브랜드 조건 '{brand}'")
    # 수신동의(앱푸시/SMS/이메일/마케팅) 조건은 재작성이 자주 흘리는 신호다 — 극성까지 서명해
    # 조건 삭제뿐 아니라 동의→미동의 뒤집힘도 소실로 잡는다(원문 기준 폴백 → 결정론 필터가 복원).
    for consent in sorted(before["consents"] - after["consents"]):
        dropped.append(f"수신동의 조건 '{_CONSENT_SIGNAL_LABELS.get(consent, consent)}'")
    # 활동회원·블랙리스트 Y/N 플래그도 재작성이 자주 흘리는 신호다(예: '블랙리스트가 아니면서' 누락) —
    # 극성까지 서명해 조건 삭제뿐 아니라 포함↔제외 뒤집힘도 소실로 잡는다(원문 폴백 → 결정론 필터가 복원).
    for flag in sorted(before["member_flags"] - after["member_flags"]):
        dropped.append(f"회원 조건 '{_MEMBER_FLAG_SIGNAL_LABELS.get(flag, flag)}'")
    # 캠페인 반응(발송/접촉 성공·오퍼·구매 반응·쿠폰)도 재작성이 '캠페인'·'발송' 단어를 발송 채널로
    # 오해해 통째로 지우는 신호다 — 사라지면 소실로 잡아 원문 기준 폴백(→ 결정론 파서가 복원)한다.
    for response in sorted(before["campaign_responses"] - after["campaign_responses"]):
        dropped.append(f"캠페인 반응 조건 '{_CAMPAIGN_RESPONSE_SIGNAL_LABELS.get(response, response)}'")
    # 기간 조건은 숫자 서명으로 못 잡는 단어형('일주일 이상 유지')이 있어 일수로 정규화해 따로 본다
    # (표기 변환 '일주일'→'7일'은 같은 7 이라 보존으로 통과, 조건 자체가 사라진 경우만 소실로 잡는다).
    # 같은 소실을 이미 숫자로 고지했으면('90일' → 숫자 '90') 중복 항목은 만들지 않는다.
    dropped_numbers = before["numbers"] - after["numbers"]
    for days in sorted(before["durations"] - after["durations"]):
        if str(days) not in dropped_numbers:
            dropped.append(f"기간 조건 '{days}일'")
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
    llm_model = _fast_llm_model(llm_model)  # 재작성은 빠르고 정확한 모델 고정(느린 추론모델 분리)
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
        response = _openai_chat_create(client, 
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
    # (?!서): '곳에서/에게서/께서'처럼 '서'가 이어지면 대상 지향("~에게")이 아니라 장소·출처·존칭 주격
    # 표현이므로 표지로 보지 않는다(예: "브랜드가 X인 곳에서 구매한 고객"은 통째로 타겟팅 절).
    pattern = r"(?P<targeting>.*?(?:%s))(?!서)\s*(?P<channel>.*)$" % "|".join(
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
        response = _openai_chat_create(client, 
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
    llm_model = _fast_llm_model(llm_model)  # 분리도 빠르고 정확한 모델 고정
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
        response = _openai_chat_create(client, 
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
    if not base_tu.get("recent_login") and other_tu.get("recent_login"):
        base_tu["recent_login"] = other_tu["recent_login"]
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
    structured_query: StructuredQuery | None = None,
) -> dict[str, Any]:
    """단일 파싱으로 query_plan 을 만든다. multi_query_variants>0 이고 LLM 사용 가능하면 프롬프트를
    의미보존 재구성한 변이들도 파싱해 '성공적으로 잡힌 타겟 조건'을 base 에 합집합으로 병합한다.

    한 표현형이 조건을 놓쳐(파서 미스) 후보가 아예 안 생기던 케이스를, 다른 표현형의 파싱으로 살린다.
    변이는 값이 아니라 표현만 바꾸므로(결정론 파서가 실제 조건 추출) 없는 조건을 지어내지 않는다.
    변이 파싱은 rules(결정론)로 하여 비용을 낮춘다 — 다양한 표현형이 서로 다른 규칙 패턴에 걸리는 것이 핵심.
    """
    base = _build_single_query_plan(
        query,
        normalization_rules,
        business_policies,
        metric_lexicon,
        sql_schema,
        parser,
        llm_model,
        prompt_dir,
        structured_query,
    )
    if multi_query_variants and multi_query_variants > 0 and parser.casefold() != "rules":
        variant_intents: list[str] = []
        for variant in _generate_prompt_reformulations(query, multi_query_variants, parser, llm_model, prompt_dir):
            variant_plan = _build_single_query_plan(
                variant,
                normalization_rules,
                business_policies,
                metric_lexicon,
                sql_schema,
                "rules",
                llm_model,
                prompt_dir,
                structured_query,
            )
            _merge_targeting_conditions(base, variant_plan)
            variant_intents.append(variant_plan.get("intent"))
        _upgrade_intent_from_variants(base, variant_intents)
        base.setdefault("parser", {})["multi_query_variants"] = multi_query_variants
    if structured_query is not None:
        base["structured_query"] = structured_query.to_dict()
    # Entity extraction and analytical routing consume the same deterministic
    # token-role view.  Exposing it on the plan also makes silent role drift
    # observable in traces and tests.
    base["query_semantics"] = {
        "tokens": classify_query_tokens(query),
        "extreme": extract_extreme_semantics(query),
    }
    # 집계 출력은 회원 목록보다 우선한다. 규칙/LLM 파서가 VIP·여성·캠페인·쿠폰 같은 수식어를
    # 오디언스 조건으로 먼저 잡았더라도, 등록된 수치 지표와 집계 함수/그룹 축이 확인되면 별도의
    # 분석 계약으로 승격한다. 지표·차원·필터 물리 매핑은 analytics_registry.json이 단일 소스다.
    # 행동 의미(구매/장바구니/캠페인 반응의 존재·부재)를 먼저 구조화한다. 분석 계약은 이 결과를
    # 모집단 스코프로 넘겨받아야 "구매한 회원 수"를 전체 회원 수로 계산하는 조용한 오답을 막는다.
    # 파서가 단순 완료형 행동("구매한 회원")을 놓쳐도 여기서 복원된다.
    _apply_core_membership_semantics(query, base)
    # 파생 엔터티 집합(순위 서브쿼리를 피연산자로 갖는 조건)은 같은 문장의 상품/기간 표현을 소유한다 —
    # 분석 계약보다 먼저 확정해야 '상품 10개'가 리터럴 상품 조건으로 새지 않는다.
    _apply_entity_set_condition(query, base)
    _apply_analytical_intent(query, base, sql_schema)
    _attach_query_output_contract(query, base)
    base["complexity"] = classify_query_complexity(base)
    return base


def _apply_entity_set_condition(query: str, plan: dict[str, Any]) -> None:
    """Attach the derived entity-set condition and consume the slots it owns.

    ``2019년 가장 많이 팔린 상품 10개를 구매한 고객``의 ``상품``·``2019년``은 리터럴 상품 조건도,
    구매 시점 조건도 아니다 — 순위 집합 정의의 일부다. 슬롯 파서가 이미 만들어 둔 해석을 여기서
    회수하지 않으면 같은 어구가 두 번 컴파일돼 서로 모순되는 SQL 이 된다.
    """
    node = parse_entity_set_condition(query, _entity_set_config())
    if not isinstance(node, dict):
        return
    node["ko_label"] = entity_set_label(node, _entity_set_config())
    reason = node.get("unsupported_reason")
    if reason:
        # 표현은 인식했지만 물리 매핑이 없다 — 조건을 무시한 SQL 대신 어느 요소가 문제인지 알린다.
        plan["unsupported"] = {
            "reason": reason,
            "message": f"'{node['ko_label']}' 조건을 현재 스키마로 추출할 수 없습니다.",
            "clarification": "순위 기준(판매수량/매출)이나 대상(상품/브랜드/카테고리)을 바꿔서 다시 요청해 주시겠어요?",
        }
        return
    target_user = plan.setdefault("target_user", {})
    target_user["entity_set_condition"] = node
    # 순위 절이 소유한 어구는 오디언스 슬롯에서 회수한다.
    target_user.pop("purchase_object", None)
    target_user.pop("purchase_object_kind", None)
    target_user.pop("purchase_objects", None)
    if node.get("window"):
        target_user.pop("purchase_date", None)
    # 관계 자체(구매/장바구니의 존재·부재)는 이 조건의 EXISTS/NOT EXISTS 가 이미 표현한다.
    # 부정도 마찬가지다 — '상위 상품을 구매하지 않은'은 전체 무주문(no_purchase)이 아니라
    # 이 집합에 한정된 부재이므로, 일반 무주문 조건으로 남겨두면 서로 다른 모집단을 요구하게 된다.
    target_user.pop("purchase_membership", None)
    # 순위 절의 관계 표현('가장 많이 *장바구니에 담은* 상품')도 이 조건이 소유한다 — 오디언스
    # 행동 조건으로 남으면 순위 절의 어구가 회원 조건으로 두 번 해석된다.
    relations = {str(node.get("relation")), str(node.get("rankRelation") or node.get("relation"))}
    owned_behaviors = {"cart_abandoner"} if "cart" in relations else set()
    if node.get("negated"):
        owned_behaviors.add("no_purchase" if node.get("relation") == "purchase" else "no_cart")
        target_user.pop("purchase_inactivity", None)
        target_user.pop("cart_absence", None)
    if owned_behaviors:
        target_user["behaviors"] = [
            value for value in target_user.get("behaviors", []) or [] if value not in owned_behaviors
        ]
    # 극값 표현('가장 많이')은 순위 집합의 것이지 회원 지표 랭킹이 아니다.
    for key in ("member_metric_selection", "member_metric_ranking", "purchase_count_ranking", "group_ranking_target"):
        plan.pop(key, None)
    # '매출 상위 5개'의 개수는 엔터티 개수지 결과 회원 수 제한이 아니다. 같은 수가 행수 제한으로도
    # 잡혔을 때만 회수한다("… 구매한 회원 100명"처럼 별도로 지정한 제한은 보존).
    if plan.get("result_limit") == node.get("limit"):
        plan["result_limit"] = None
    # 같은 어구('매출이 높은')를 회원 단위 랭킹 정책으로도 읽은 결과는 이 조건과 이중 해석이다.
    # 임계값 정책은 진짜 추가 조건이므로 남긴다 — 순위(rank) 정책만 회수한다.
    policies = plan.get("policy_constraints")
    if isinstance(policies, list):
        plan["policy_constraints"] = [
            policy for policy in policies
            if not (isinstance(policy, dict) and policy.get("sql_behavior") == "rank")
        ]


def _apply_analytical_intent(query: str, plan: dict[str, Any], schema_path: Path) -> None:
    """Attach the deterministic aggregate contract and consume audience-only misclassification."""
    intent = analyze_analytical_intent(query)
    if not isinstance(intent, dict):
        return
    public_keys = (
        "query_type", "aggregate_function", "metric", "dimensions", "filters",
        "comparison", "result_shape", "target_entity",
    )
    plan["detected_intent"] = {key: intent.get(key) for key in public_keys}
    plan["analytical_intent"] = intent
    plan["intent"] = "analyze_aggregation"
    plan["selected_route"] = (
        "analytical_ranking_sql" if intent.get("query_type") == "ranking" else "analytical_aggregate_sql"
    )
    capability_passed = (
        intent.get("query_type") in SUPPORTED_QUERY_TYPES
        and intent.get("result_shape") in SUPPORTED_RESULT_SHAPES
        and not bool(intent.get("unsupported_reason"))
    )
    plan["capability_check"] = {
        "endpoint": "/target-sql",
        "query_type": intent.get("query_type"),
        "result_shape": intent.get("result_shape"),
        "passed": capability_passed,
        "reason": intent.get("unsupported_reason") or (None if capability_passed else "unsupported_result_shape"),
    }

    if intent.get("unsupported_reason"):
        plan["unsupported"] = {
            "reason": intent["unsupported_reason"],
            "message": intent.get("unsupported_message") or "요청한 집계를 안전하게 생성할 수 없습니다.",
            "clarification": "계산할 지표와 집계 함수, 그룹 기준을 스키마에 있는 항목으로 지정해 주세요.",
        }
        return
    try:
        plan["aggregation_request"] = build_deterministic_aggregation_request(intent)
    except (KeyError, TypeError, ValueError) as exc:
        plan["unsupported"] = {
            "reason": "unsupported_aggregate_contract",
            "message": f"집계 의도를 물리 스키마 계약으로 확정하지 못했습니다: {exc}",
            "clarification": "계산할 지표와 그룹 기준을 더 명확히 지정해 주세요.",
        }
        return

    # 분석 IR이 소유한 필터를 기존 회원목록 슬롯에서 제거한다. 그렇지 않으면 required_sql_conditions가
    # SELECT DISTINCT 회원번호/EXISTS 형태를 추가로 요구해 올바른 집계 SQL을 목록 SQL로 되돌린다.
    target_user = plan.get("target_user") if isinstance(plan.get("target_user"), dict) else {}
    filter_ids = {item.get("id") for item in intent.get("filters", []) if isinstance(item, dict)}
    if "female" in filter_ids:
        target_user["gender"] = None
    if "vip" in filter_ids:
        target_user["lifecycle"] = [value for value in target_user.get("lifecycle", []) if value != "vip"]
    if intent.get("metric") in {"campaign_purchase_amount", "coupon_purchase_amount"}:
        target_user["campaign_responses"] = []
    # Targeting-only group/ranking slots must never impose audience predicates
    # on a general aggregation.  The aggregation_request owns dimensions,
    # measures, user filters, and policy filters as separate concepts.
    for key in ("region_density_target", "region_member_count_target", "group_ranking_target"):
        plan.pop(key, None)
    member_policy = intent.get("member_policy") if isinstance(intent.get("member_policy"), dict) else {}
    if member_policy.get("mode") in {"all", "expanded"}:
        state_terms = {"dormant", "inactive_90d", "inactive_180d", "withdrawn", "withdrawn_user"}
        target_user["lifecycle"] = [
            value for value in target_user.get("lifecycle", []) if value not in state_terms
        ]
    if intent.get("query_type") == "ranking":
        # The analytical ranking contract owns the member metric and TOP 1 semantics.  Remove
        # legacy audience-ranking slots (whose default is TOP 100) so they cannot compete with it.
        for key in (
            "member_metric_selection", "member_column_selection_filter", "cart_aggregate",
            "group_ranking_target", "region_member_count_target", "region_density_target",
            "purchase_count_ranking",
        ):
            plan.pop(key, None)
        # 극값 계약은 지표 소스 위에서 정의된다 — "가장 많이 구매한 회원"의 구매 존재 조건은
        # 주문 테이블 집계 자체가 보장하므로 별도 오디언스 조건으로 남기지 않는다.
        for key in ("behaviors", "purchase_object", "interests", "category", "purchase_membership"):
            if key in target_user:
                target_user[key] = [] if isinstance(target_user.get(key), list) else None
    _consume_analytical_scope_conditions(plan, intent)
    _bind_member_condition_filters(plan, intent)
    plan["semantic_conditions"] = []
    campaign = plan.get("campaign_constraints")
    if isinstance(campaign, dict):
        campaign["objective"] = None
        campaign["offer_type"] = None
        campaign["channels"] = []
        campaign["sell_object"] = None
    # 오디언스 파서가 같은 문구에 붙인 미지원 판정은 분석 계약이 완전히 대체한다.
    plan.pop("unsupported", None)
    # 그룹 축("브랜드별")을 상품명으로 이중 해석한 결과는 조건이 아니다 — 남은 조건을 세기 전에 정리한다.
    _normalize_aggregation_axis_filters(plan)
    dropped = _analytical_dropped_conditions(plan)
    if dropped:
        # 집계 계약이 담지 못한 조건이 남았다 = 그 조건을 무시한 '그럴듯한 숫자'가 나갈 상태다.
        # 조용한 오답 대신 무엇이 빠졌는지 명시하고 확인을 요청한다(fail-close).
        plan["unsupported"] = {
            "reason": "analytical_signal_dropped",
            "message": "질문의 다음 조건을 집계 SQL에 반영하지 못했습니다: " + ", ".join(dropped),
            "clarification": ANALYTICAL_UNSUPPORTED_CLARIFICATIONS["analytical_signal_dropped"],
            "dropped_conditions": dropped,
        }
        plan["capability_check"] = {
            **(plan.get("capability_check") or {}),
            "passed": False,
            "reason": "analytical_signal_dropped",
        }
        return
    _refresh_aggregation_request_validation(plan, schema_path)


# 분석 계약이 소유하는 오디언스 슬롯. 스코프/필터로 컴파일된 조건은 회원 목록 요구사항에서 지워야
# required_sql_conditions 가 올바른 집계 SQL 을 "조건 누락"으로 되돌리지 않는다.
_ANALYTICAL_SCOPE_OWNERSHIP: dict[str, tuple[tuple[str, str | None], ...]] = {
    "purchase": (("purchase_membership", None), ("behaviors", "no_purchase"), ("purchase_inactivity", None)),
    "cart": (("behaviors", "cart_abandoner"), ("cart_absence", None), ("cart_retention", None)),
    "campaign_response": (("campaign_responses", None),),
    "login": (("recent_login", None),),
}
_ANALYTICAL_FILTER_OWNERSHIP: dict[str, tuple[tuple[str, str | None], ...]] = {
    "female": (("gender", None),),
    "vip": (("lifecycle", "vip"),),
    "app_login_channel": (("lifecycle", "app_user"), ("preferred_channels", "app")),
}
# 오디언스 조건이 아닌 부가 정보 슬롯. 이 목록만 예외이고, 나머지 target_user 값은 전부 검사 대상이다
# — 새 조건 슬롯이 생겨도 목록에 추가하는 것을 잊어서 조용히 무시되는 일이 없어야 한다.
_ANALYTICAL_IGNORED_SLOTS = frozenset({"purchase_object_kind", "sell_object"})
# 회원 상태 정책(member_policy)과 분석 계약이 이미 소유하는 lifecycle canonical.
_ANALYTICAL_OWNED_LIFECYCLE = frozenset({"normal_member"})


def _remove_slot_value(target_user: dict[str, Any], slot: str, value: str | None) -> None:
    if value is None:
        target_user[slot] = [] if isinstance(target_user.get(slot), list) else None
        return
    current = target_user.get(slot)
    if isinstance(current, list):
        target_user[slot] = [item for item in current if item != value]


def _consume_analytical_scope_conditions(plan: dict[str, Any], intent: dict[str, Any]) -> None:
    """Remove the audience slots the analytical contract compiled itself."""
    target_user = plan.get("target_user") if isinstance(plan.get("target_user"), dict) else {}
    for scope in intent.get("scopes", []) or []:
        if not isinstance(scope, dict):
            continue
        for slot, value in _ANALYTICAL_SCOPE_OWNERSHIP.get(str(scope.get("id")), ()):
            _remove_slot_value(target_user, slot, value)
    for item in intent.get("filters", []) or []:
        if not isinstance(item, dict):
            continue
        for slot, value in _ANALYTICAL_FILTER_OWNERSHIP.get(str(item.get("id")), ()):
            _remove_slot_value(target_user, slot, value)


def _bind_member_condition_filters(plan: dict[str, Any], intent: dict[str, Any]) -> None:
    """Compile audience canonicals the parser found into the analytical contract.

    ``VIP``/``임직원``/``휴면``/``앱 사용자`` already have one physical definition in
    ``member_target_filters.json``.  Binding them here means an aggregate over that
    population reuses the audience definition instead of failing closed — and the
    count always describes the same rows the member list would return.
    """
    target_user = plan.get("target_user") if isinstance(plan.get("target_user"), dict) else {}
    remaining: list[str] = []
    for canonical in list(target_user.get("lifecycle", []) or []):
        spec = member_condition_filter(str(canonical))
        if spec is None:
            remaining.append(str(canonical))
            continue
        candidate = {
            **intent,
            "filters": [*intent.get("filters", []), {
                "id": f"member_{canonical}", "label": spec.get("label", canonical), "spec": spec,
            }],
        }
        try:
            request = build_deterministic_aggregation_request(candidate)
        except (KeyError, TypeError, ValueError):
            # 이 지표 소스가 해당 회원 조건에 닿지 못한다 — 조용히 무시하지 않고 미반영으로 남긴다.
            remaining.append(str(canonical))
            continue
        intent["filters"] = candidate["filters"]
        plan["analytical_intent"] = intent
        plan["aggregation_request"] = request
        plan["detected_intent"] = {**(plan.get("detected_intent") or {}), "filters": intent["filters"]}
    target_user["lifecycle"] = remaining


def _analytical_dropped_conditions(plan: dict[str, Any]) -> list[str]:
    """Audience conditions the analytical contract neither compiled nor consumed.

    Enumerating what is *left* rather than what is forbidden keeps the guard honest
    as the parser grows: a newly extracted condition that no aggregate can express
    blocks the answer instead of quietly disappearing from the number.
    """
    target_user = plan.get("target_user") if isinstance(plan.get("target_user"), dict) else {}
    dropped: list[str] = []
    for slot, value in target_user.items():
        if slot in _ANALYTICAL_IGNORED_SLOTS or value in (None, [], {}, "", False):
            continue
        if slot in {"lifecycle", "behaviors"}:
            dropped.extend(
                _unsupported_condition_label(f"target_user.{slot}:{item}")
                for item in value
                if slot != "lifecycle" or str(item) not in _ANALYTICAL_OWNED_LIFECYCLE
            )
            continue
        dropped.append(_unsupported_condition_label(f"target_user.{slot}"))
    return dropped


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


def classify_query_complexity(query_plan: dict[str, Any]) -> str:
    """의도·복잡도 판별 단계: 질의를 'simple'/'complex' 로 분류한다.

    simple: 회원 테이블(CRM_MB_BASEINFO) 단독 속성 조건만(성별/연령/등급/Y-N 플래그/지역/잔액/생일/
      가입/로그인) — 결정론 rules 파서가 전부 뽑으므로 LLM Query Plan 보강 없이 바로 AST→SQL 로 간다.
    complex: 조인/집계/합집합이 필요한 구조(구매이력·횟수·금액, 장바구니, 캠페인 반응, 랭킹, 집합식,
      OR 합집합 등) — Query Plan 경로(파서 auto/llm 이면 LLM 보강 포함)를 거친다.
    """
    target_user = query_plan.get("target_user", {})
    complex_signals = (
        target_user.get("behaviors"),                 # 첫구매/재구매/무구매/장바구니 이탈(주문·카트 조인)
        target_user.get("purchase_object"),           # 상품 구매 이력(주문 상세 조인)
        target_user.get("purchase_date"),             # 구매 날짜 창(주문 조인)
        target_user.get("purchase_inactivity"),       # 구매 미발생 기간(주문 집계)
        target_user.get("purchase_membership"),       # 구매 존재(선택적 최근 창, 주문 EXISTS)
        target_user.get("aggregate_conditions"),      # 누적 금액/횟수 임계값(주문 집계)
        target_user.get("cart_aggregate"),            # 장바구니 개수/수량 임계값(카트 집계)
        target_user.get("cart_retention"),            # 장바구니 보관 기간(카트 담은 시점 비교)
        target_user.get("cart_type"),                 # 장바구니 유형(정기배송/픽업 등 CART_TYPE_CD)
        target_user.get("campaign_responses"),        # 캠페인 반응(팩트 EXISTS)
        target_user.get("campaign_response_frequency"),  # 캠페인 반응 횟수(팩트 집계)
        target_user.get("campaign_buy_amount"),       # 캠페인 귀속 구매금액(팩트 BUY_AMT 집계)
        target_user.get("campaign_buy_count"),        # 캠페인 귀속 구매건수(팩트 구매반응 캠페인 수)
        target_user.get("cell_rate_target"),          # 셀 단위 성공률/구매율 비율(셀 집계)
        query_plan.get("union_condition"),            # 합집합(OR) 컴파일
        query_plan.get("logical_expression"),         # 논리식(OR-of-conjunctions) 컴파일
        query_plan.get("set_expressions"),            # 집합식
        query_plan.get("region_density_target"),      # 밀집 지역 랭킹(집계)
        query_plan.get("group_ranking_target"),       # 그룹별 회원 Top-N(PARTITION BY 윈도)
        query_plan.get("region_member_count_target"), # 지역 단위 회원 수 집계 랭킹
        query_plan.get("member_metric_ranking"),      # 지표 상위 N 랭킹(지표 조인)
        query_plan.get("member_metric_selection"),    # 잔액 등 회원 컬럼 선택(상위 N/N%/평균 대비)
        query_plan.get("purchase_count_ranking"),     # 기간 내 구매 랭킹(주문 집계)
        query_plan.get("computed_metrics"),           # 계산 지표
        query_plan.get("aggregation_request"),        # 일반 집계 요구사항 IR
    )
    return "complex" if any(complex_signals) else "simple"


# ── 결정론 필터 파이프라인 레지스트리(단일 소스) ─────────────────────────────────────
# 배경: 새 타겟 조건을 하나 넣을 때마다 정규식 파서 _apply_*_filter 를 규칙 경로(_build_rule_query_plan)와
# LLM/auto 경로(_build_single_query_plan) 두 곳(+union 재감지 패스)에 손으로 나열해야 했다. 한 곳을 빠뜨리면
# "rules 는 되는데 auto 만 실패"가 나온다(반복 사고). 이 레지스트리는 '필터를 어떻게 호출하나'(컨테이너/슬롯
# 초기화/추가 인자/참여 경로)를 필터당 한 엔트리로 선언하고, 두 경로는 이름 순서 리스트만 넘겨 _run_filters 로
# 순회한다. 순서는 문서화된 의존성(주석 참조)이 경로마다 달라 경로별 리스트가 소유하고, 참여 경로 집합은 spec
# 이 소유한다 — test_deterministic_filter_registry 가 '리스트 == spec.paths'와 '고아 없음'을 강제해 등록 누락을
# 컴파일타임 아닌 테스트타임에 잡는다(_sql_target_builder_registry 의 소유권 불변식과 같은 방식).
@dataclass(frozen=True)
class _FilterSpec:
    """결정론 필터 하나의 호출 방식 선언. 새 필터는 여기 한 엔트리 + 경로 리스트에 이름 추가만 하면 된다."""

    # apply 는 첫 필드여야 한다 — 커스텀 엔트리가 _FilterSpec(_apply_x, ...) 처럼 함수를 위치인자로 넘긴다.
    apply: Callable[..., None] | None = None  # impl="custom": apply(query, plan|target_user[, business_policies])
    # impl: 이 필터가 어떻게 실행되나. "custom"=전용 _apply_* 함수(도메인 고유 로직), "slot_setter"=범용
    # 감지→슬롯 세팅(detect/slot 필드로 완전 선언), "attribute_token"=범용 회원속성 토큰 승격(group 필드).
    # 선언형 impl 은 전용 함수 없이 이 레지스트리 한 줄로 새 필터를 연다 — 신규 필터마다 _apply_* 가 느는 문제 해소.
    impl: str = "custom"
    # apply 2번째 인자: "plan" → apply(query, plan) / "target_user" → apply(query, plan["target_user"]).
    arg: str = "plan"
    # impl="slot_setter": detect(query)->값|None; 값이 있으면 컨테이너[slot] 에 세팅(mode="append"면 리스트에 유일 추가).
    detect: Callable[[str], Any] | None = None
    slot: str | None = None
    slot_on: str = "target_user"  # "target_user" | "plan"
    mode: str = "set"  # "set" | "append"
    # impl="attribute_token": _attribute_token_groups() 의 그룹 이름(표면어→lifecycle/exclude 승격 문법).
    group: str | None = None
    # impl="threshold": 타입 스펙(_ThresholdSpec)으로 생성한 matcher. '<숫자><단위><연산자>'를 slot 에
    # {operator, threshold} dict 로 세팅한다 — 전용 파서 함수 없이 스펙 등록만으로 임계 필터가 열린다.
    # gate: 이 문자열이 있어야 발동(오탐 방지, None=무조건). 창/라벨/교차정리 등 오케스트레이션이 필요하면
    # 그건 스펙으로 표현 못 하므로 custom 필터로 남긴다.
    threshold: "_ThresholdMatcher | None" = None
    gate: str | None = None
    # auto 경로 슬롯 선초기화(LLM 플랜은 희소해 키가 없을 수 있음; 규칙 경로는 플랜을 리터럴로 선초기화하므로
    # init=False 로 건너뛴다 — 현행 동작 그대로다). init_on: "target_user"|"plan", init_list: 기본값 [] 여부.
    init_key: str | None = None
    init_on: str = "target_user"
    init_list: bool = False
    needs_policies: bool = False  # apply(query, plan, business_policies)
    # family: 이 필터가 속한 선언형 클러스터(예: "attribute_token"). impl 이 선언형이면 impl 이 곧 family 지만,
    # 공통화 불가 예외를 커스텀(impl="custom")으로 남길 때 family 로 '원래 이 클러스터 소속'임을 표시하고
    # exception_reason 에 사유를 적는다 — 테스트가 '클러스터 소속은 선언형이거나 사유 있는 예외' 불변식을 강제한다.
    family: str | None = None
    exception_reason: str | None = None
    paths: frozenset[str] = frozenset({"rules", "auto"})


def _deterministic_filter_registry() -> dict[str, _FilterSpec]:
    """결정론 필터 레지스트리(이름 → 호출 방식). 런타임 호출이라 아래 _apply_* 가 나중에 정의돼도 된다.

    참여 경로: 대부분 {rules, auto}. rules 전용은 age(정규식 연령; LLM 은 슬롯으로 직접 산출), cart_repurchase/
    inactivity_period(정규화 매칭 뒤 문맥 보정), policy(업무 정책; auto 는 별도 처리). recognized_domains 는
    파이프라인 위치가 두 경로에서 달라(진단 전용) 레지스트리 밖에서 명시 호출한다."""
    return {
        # 회원 속성/값/지역 + 랭킹(정렬·TOP·서브쿼리류).
        "sell_object": _FilterSpec(_apply_sell_object),
        "dimension": _FilterSpec(_apply_dimension_filters),
        "member_value": _FilterSpec(_apply_member_value_filters),
        # 광역 권역어(수도권 등)를 구성 시도(SIDO IN)로 확장 — 값 인덱스 뒤에 실행해 명시 시도와 병합.
        "macro_region": _FilterSpec(_apply_macro_region_filter),
        # 그룹별 회원 Top-N(지역별 … N명씩)·지역 회원수 랭킹은 전역 회원/지역밀집 랭킹보다 먼저 실행해
        # 그룹/지역-단위 의도를 먼저 확정한다(전역 랭킹이 가로채지 못하게 라우팅 우선순위 소유).
        "group_ranking": _FilterSpec(_apply_group_ranking_target),
        "region_member_count": _FilterSpec(_apply_region_member_count_target),
        "region_density": _FilterSpec(_apply_region_density_target),
        "member_metric_ranking": _FilterSpec(_apply_member_metric_ranking_target),
        "purchase_count_ranking": _FilterSpec(_apply_purchase_count_ranking_target),
        # 연령(정규식) — rules 전용. target_user 를 직접 받는다.
        "age": _FilterSpec(_apply_age_filters, arg="target_user", paths=frozenset({"rules"})),
        "purchase_object": _FilterSpec(_apply_purchase_object_filter, arg="target_user"),
        # 선언형(slot_setter): 감지 파서 → 슬롯. 전용 _apply_* 함수 없이 레지스트리 한 줄.
        "purchase_date": _FilterSpec(impl="slot_setter", detect=_parse_purchase_date_period, slot="purchase_date", init_key="purchase_date"),
        "result_limit": _FilterSpec(impl="slot_setter", detect=_parse_result_limit, slot="result_limit", slot_on="plan", init_key="result_limit", init_on="plan"),
        "purchase_inactivity": _FilterSpec(_apply_purchase_inactivity_filter, init_key="purchase_inactivity"),
        "recent_login": _FilterSpec(impl="slot_setter", detect=_parse_recent_login_period, slot="recent_login", init_key="recent_login"),
        # 예외(attribute_token 클러스터지만 커스텀): 온/오프라인 가입은 online=NOT offline 상호정의 + 이중부정
        # 진리표라 단순 neg/pos 문법으로 표현 불가 → 커스텀 유지.
        "signup_channel": _FilterSpec(_apply_signup_channel_filter, family="attribute_token",
                                      exception_reason="online/offline 상호정의 + 이중부정 진리표(단순 문법 불가)"),
        # 선언형(attribute_token): 표면어→회원속성 canonical 을 lifecycle/exclude 로 승격(문법은 그룹이 소유).
        "signup_device": _FilterSpec(impl="attribute_token", group="signup_device"),
        # 파생 비율('하루 평균 로그인 횟수')은 원 임계(balance_condition) 앞에 실행해 CNT/DAYS 비로 먼저
        # 확정한다 — 뒤 balance_condition 이 '로그인 횟수'를 원 횟수 임계로 오탐하는 걸 접두어 게이트로 막는다.
        "ratio_metric": _FilterSpec(_apply_ratio_metric_filter),
        "balance_condition": _FilterSpec(_apply_balance_condition_filter),
        "balance_selection": _FilterSpec(_apply_balance_selection_filter),
        # 행위 동사형 지표('한 번도 로그인하지 않은/정확히 20번 로그인한/평균보다 많이 로그인') → 명사형(balance_*)
        # 뒤에 실행해 이미 잡힌 슬롯은 덮지 않는다. action_aliases 를 선언한 numeric_filters 항목만 대상.
        "action_metric": _FilterSpec(_apply_action_metric_filter),
        "campaign_response": _FilterSpec(_apply_campaign_response_filter),
        # '추가 구매 없는'(무구매 anti-join)은 캠페인 반응·미구매창 파싱 뒤에 실행(리스트 순서가 보장).
        "no_additional_purchase": _FilterSpec(_apply_no_additional_purchase_filter),
        # '구매 이력은 있지만 결제금액 합계 0원'(주문 있고 SUM=0)은 무주문이 아니라 결제금액 집계 =0 으로
        # 컴파일한다 — 0원 게이트보다 먼저 aggregate_conditions 를 채워 모호 미지원 처리를 피한다.
        "zero_amount_purchase": _FilterSpec(_apply_zero_amount_with_purchase_filter),
        # '구매 횟수가 0회/없는'(공집합 COUNT=0)도 no_purchase 로 승격 — 집계(order_count '='0) 파싱 뒤에
        # 실행해 그 공집합 조건을 걷어내고 anti-join 으로 대체한다. 캠페인/기간창 문맥은 각 트랙에 양보.
        "zero_purchase_count": _FilterSpec(_apply_zero_purchase_count_filter),
        # 선언형(slot_setter, append): 카트 '존재' 감지 → behaviors 에 cart_abandoner 유일 추가.
        "cart_presence": _FilterSpec(impl="slot_setter", detect=_detect_cart_presence, slot="behaviors", mode="append"),
        # 카트 '부재'는 존재/이탈 승격 뒤에 실행해 오파싱된 cart_abandoner 를 걷어낸다.
        "cart_absence": _FilterSpec(_apply_cart_absence_filter),
        "campaign_response_frequency": _FilterSpec(_apply_campaign_response_frequency_filter, init_key="campaign_response_frequency"),
        "children_registered": _FilterSpec(impl="attribute_token", group="children"),
        # 예외(attribute_token 클러스터지만 커스텀): '<등급> 이상/이하'는 서열 랭크 집합 확장 + 기존 등급
        # lifecycle 소유권 override 라 단순 표면어 매칭이 아님 → 커스텀 유지.
        "grade_threshold": _FilterSpec(_apply_grade_threshold_filter, family="attribute_token",
                                       exception_reason="서열 랭크 집합 확장 + 등급 lifecycle 소유권 override"),
        # 예외(attribute_token 클러스터지만 커스텀): 채널 수신동의는 group-gap 나열 매칭 + 매칭 채널어를
        # preferred_channels/캠페인 채널에서 강등하는 부수효과가 있어 단순 문법으로 표현 불가 → 커스텀 유지.
        "channel_consent": _FilterSpec(_apply_channel_consent_filter, family="attribute_token",
                                       exception_reason="group-gap 나열 매칭 + 채널어 강등 부수효과"),
        "member_flag": _FilterSpec(impl="attribute_token", group="member_flag"),
        "aggregate": _FilterSpec(_apply_aggregate_condition_filter, init_key="aggregate_conditions", init_list=True),
        # 지표명 없는 개수 임계('2개 이상 구입')는 지표명 명시형 파싱 뒤에 실행(order_count 중복 추가 방지).
        "purchase_count_threshold": _FilterSpec(_apply_purchase_count_threshold_filter),
        # '캠페인 구매금액'(귀속 금액)은 누적 금액·반응 파싱 뒤에 실행해 이중 파싱을 걷어낸다.
        "campaign_buy_amount": _FilterSpec(_apply_campaign_buy_amount_filter, init_key="campaign_buy_amount"),
        # '캠페인 구매건수'(귀속 건수)도 집계(order_count) 파싱 뒤에 실행해 이중 파싱을 걷어낸다.
        "campaign_buy_count": _FilterSpec(_apply_campaign_buy_count_filter, init_key="campaign_buy_count"),
        # '성공률/구매율'(셀 비율)도 캠페인 반응 파싱 뒤에 실행해 오배정 접촉성공 EXISTS 를 걷어낸다.
        "cell_rate": _FilterSpec(_apply_cell_rate_target_filter, init_key="cell_rate_target"),
        "cart_aggregate": _FilterSpec(_apply_cart_aggregate_condition_filter),
        "cart_retention": _FilterSpec(_apply_cart_retention_filter, init_key="cart_retention"),
        "cart_type": _FilterSpec(_apply_cart_type_filter, init_key="cart_type"),
        "birthday": _FilterSpec(impl="slot_setter", detect=_detect_birthday_target, slot="birthday_target", init_key="birthday_target"),
        "signup_target": _FilterSpec(_apply_signup_target_filter, init_key="signup_target"),
        # rules 전용(정규화 매칭 뒤 문맥 보정 / 업무 정책).
        "cart_repurchase": _FilterSpec(_apply_cart_repurchase_context, paths=frozenset({"rules"})),
        "inactivity_period": _FilterSpec(_apply_inactivity_period_filter, paths=frozenset({"rules"})),
        "policy": _FilterSpec(_apply_policy_constraints, needs_policies=True, paths=frozenset({"rules"})),
    }


def _detect_birthday_target(query: str) -> dict[str, Any] | None:
    """'오늘/이달 생일' → {granularity}. 생년월일(원본 DOB) 언급은 생일 이벤트 타겟이 아니라 잡지 않는다.

    생일은 BIRTHDAY(YYYYMMDD)의 월일(MMDD)만 오늘과 비교해야 한다(년도까지 비교하면 아무도 안 걸림).
    '이달/이번 달 생일'은 월(MM)만 비교한다(granularity='month')."""
    compact = query.replace(" ", "").casefold()
    if "생일" not in compact and "생신" not in compact and "birthday" not in compact:
        return None
    granularity = "month" if any(sig in compact for sig in ("이달", "이번달", "당월", "금월")) else "day"
    return {"granularity": granularity}


def _detect_cart_presence(query: str) -> str | None:
    """'장바구니에 (상품이) 있는/담아둔' 존재 표현 → cart_abandoner(보관 상태 KEEP_YN='Y') 토큰."""
    return "cart_abandoner" if _CART_PRESENCE_PATTERN.search(query.replace(" ", "").casefold()) else None


@dataclass(frozen=True)
class _AttributeTokenGroup:
    """회원속성 토큰 승격 문법(선언형 스펙). 표면어(canonical)를 lifecycle(포함)/exclude.lifecycle(제외)로
    올리는 '단순 속성형' 필터의 전체 동작을 데이터로 선언한다 — 새 필터는 이 스펙 한 줄 + 레지스트리
    attribute_token 엔트리 등록만으로 열린다(전용 _apply_* 함수 불필요). 복합 파싱(서열 랭크 확장·이중부정·
    채널어 강등 등)은 이 문법으로 표현 못 하므로 커스텀 필터(impl="custom", family="attribute_token",
    exception_reason=...)로 남긴다 — test_deterministic_filter_registry 가 그 분류를 강제한다."""

    canonicals: tuple[tuple[str, tuple[str, ...]], ...]  # (canonical, 기본 표면어); surface_terms JSON 있으면 덮음
    neg: str | None = None   # 부정 접미어 정규식 → exclude.lifecycle (None=부정 없음)
    pos: str = ""            # 긍정 접미어 정규식 ('' = 표면어 단독)
    gate: str | None = None  # 이 문자열이 있어야 발동 (None = 무조건)
    first_only: bool = False  # 첫 매치 하나만(구체성 우선)


def _default_attribute_token_groups_raw() -> dict[str, dict[str, Any]]:
    """속성 토큰 그룹의 코드 폴백(JSON 파일 부재/파손 시). JSON 스키마와 동일 형태로 반환한다.
    런타임 호출이라 아래 상수(_MEMBER_FLAG_TARGETS 등)가 나중에 정의돼도 된다."""
    return {
        # 활동회원/블랙리스트 등 Y/N 플래그: 부정→제외, 표면어 단독→포함. 여러 플래그 동시 매치 가능.
        "member_flag": {"neg": _MEMBER_FLAG_NEG, "pos": "", "gate": None, "first_only": False,
                        "canonicals": [[c, list(t)] for c, t in _MEMBER_FLAG_TARGETS]},
        # 자녀정보 등록: '등록/보유/있음' 문맥이 붙어야 긍정, '없/미등록' 부정→제외('자녀 선물' 등 비속성 분리).
        "children": {"neg": _CHILDREN_NEG, "pos": _CHILDREN_POS, "gate": None, "first_only": False,
                     "canonicals": [["children_registered", list(_CHILDREN_TERMS)]]},
        # 가입 디바이스(앱/PC/모바일웹): '가입' 문맥 필수, 구체성 순서로 첫 매치 하나만, 부정 없음.
        "signup_device": {"neg": None, "pos": _SIGNUP_DEVICE_SUFFIX, "gate": "가입", "first_only": True,
                          "canonicals": [[c, list(t)] for c, t in _SIGNUP_DEVICE_TARGETS]},
    }


def _load_attribute_token_groups_raw(path: Path = DEFAULT_ATTRIBUTE_TOKEN_GROUPS_PATH) -> dict[str, dict[str, Any]]:
    """attribute_token_groups.json 의 "groups" 를 읽는다. 파일 부재/파손/빈 groups 면 코드 폴백."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_attribute_token_groups_raw()
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if isinstance(groups, dict) and groups:
        return groups
    return _default_attribute_token_groups_raw()


def _coerce_attribute_token_group(raw: Any) -> _AttributeTokenGroup | None:
    """JSON 그룹 dict → _AttributeTokenGroup 스펙. canonicals 는 [[canonical, [표면어]]] 형태여야 한다."""
    if not isinstance(raw, dict):
        return None
    canonicals: list[tuple[str, tuple[str, ...]]] = []
    for item in raw.get("canonicals", []):
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str) and isinstance(item[1], (list, tuple)):
            terms = tuple(t for t in item[1] if isinstance(t, str) and t)
            if item[0] and terms:
                canonicals.append((item[0], terms))
    if not canonicals:
        return None
    neg = raw.get("neg")
    gate = raw.get("gate")
    return _AttributeTokenGroup(
        canonicals=tuple(canonicals),
        neg=neg if isinstance(neg, str) and neg else None,
        pos=raw.get("pos") if isinstance(raw.get("pos"), str) else "",
        gate=gate if isinstance(gate, str) and gate else None,
        first_only=bool(raw.get("first_only", False)),
    )


def _attribute_token_groups() -> dict[str, _AttributeTokenGroup]:
    """회원속성 토큰 승격 그룹의 선언형 문법 스펙(단일 소스 = attribute_token_groups.json, 코드 폴백).

    새 단순 속성형 필터는 이 JSON 에 그룹/카노니컬 한 줄 추가 + graph_rag 레지스트리에 attribute_token
    엔트리 등록만으로 열린다(전용 _apply_* 함수 불필요)."""
    out: dict[str, _AttributeTokenGroup] = {}
    for name, raw in _load_attribute_token_groups_raw().items():
        group = _coerce_attribute_token_group(raw)
        if group is not None:
            out[name] = group
    return out or {
        name: _coerce_attribute_token_group(raw)
        for name, raw in _default_attribute_token_groups_raw().items()
    }


def _member_surface_terms() -> dict[str, list[str]]:
    """eq_filters JSON 엔트리의 surface_terms(문맥 승격용 표면어)를 {canonical: [표면어]} 로 읽는다.

    하드코딩 표면어 맵의 외부화 지점 — 있으면 코드 기본값을 덮는다(없으면 코드 기본값 사용, 동작 불변).
    normalization 의 synonyms 와 별개다: synonyms 는 정규화 매칭용, surface_terms 는 결정론 문맥 승격용."""
    out: dict[str, list[str]] = {}
    entries = _MEMBER_TARGET_FILTERS.get("eq_filters")
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        canonical = entry.get("canonical")
        terms = entry.get("surface_terms")
        if isinstance(canonical, str) and canonical and isinstance(terms, list):
            clean = [t.replace(" ", "").casefold() for t in terms if isinstance(t, str) and t]
            if clean:
                out[canonical] = clean
    return out


_MEMBER_SURFACE_TERMS = _member_surface_terms()


def _attribute_terms(canonical: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """canonical 의 표면어 — 그룹 선언(attribute_token_groups.json)의 표면어와 eq_filters surface_terms 의
    합집합. 어느 파일에 추가해도 매칭되게 한다(한쪽이 다른 쪽을 가리지 않음). 둘 다 같은 값이면 합집합=그대로."""
    override = _MEMBER_SURFACE_TERMS.get(canonical)
    if not override:
        return tuple(default)
    merged = list(default)
    for term in override:
        if term not in merged:
            merged.append(term)
    return tuple(merged)


def _run_attribute_token(group: _AttributeTokenGroup, query: str, plan: dict[str, Any]) -> None:
    """선언형 문법 스펙(_AttributeTokenGroup) 하나로 회원속성 표면어를 lifecycle(포함)/exclude.lifecycle
    (제외) canonical 로 승격하는 범용 실행기. member_flag/children/signup_device 가 이 하나를 공유한다 —
    새 단순 속성형 필터는 스펙만 추가하면 전용 함수 없이 동작한다. canonical 이 MEMBER_EQ_FILTERS 에 없으면
    (컴파일 불가) 승격도 하지 않는다. 스펙 객체를 받으므로 테스트가 합성 스펙으로 직접 구동해 데이터-구동을
    검증할 수 있다(이름 조회는 _dispatch_filter 가 한다)."""
    compact = query.replace(" ", "").casefold()
    if group.gate and group.gate not in compact:
        return
    for canonical, default_terms in group.canonicals:
        if canonical not in MEMBER_EQ_FILTERS:
            continue  # 레지스트리에서 빠졌다면 문맥 승격도 하지 않는다(컴파일 불가 방지)
        term_alt = "(?:" + "|".join(re.escape(term) for term in _attribute_terms(canonical, default_terms)) + ")"
        if group.neg and re.search(term_alt + group.neg, compact):
            _append_unique(plan.setdefault("exclude", {}).setdefault("lifecycle", []), canonical)
        elif re.search(term_alt + group.pos, compact):
            _append_unique(plan.setdefault("target_user", {}).setdefault("lifecycle", []), canonical)
            if group.first_only:
                return  # 가장 구체적인 매치 하나만(예: 모바일웹 > 웹/PC)


def _run_threshold_filter(spec: "_FilterSpec", query: str, plan: dict[str, Any]) -> None:
    """타입 스펙(_ThresholdMatcher)만으로 '<숫자><단위><연산자>'를 slot 에 {operator, threshold} 로 세팅한다.
    전용 파서 함수 없이 스펙 등록만으로 동작하는 임계 필터 실행기 — 창/라벨/교차정리가 필요한 도메인은 custom."""
    if spec.gate and spec.gate not in query.replace(" ", "").casefold():
        return
    match = spec.threshold.pattern.search(query)
    if match is None:
        return
    parsed = spec.threshold.parse(match)
    if parsed is None:
        return
    operator, value = parsed
    container = plan if spec.slot_on == "plan" else plan.setdefault("target_user", {})
    container[spec.slot] = {"operator": operator, "threshold": value}


def _dispatch_filter(spec: "_FilterSpec", query: str, plan: dict[str, Any], business_policies: Path | None) -> None:
    """단일 필터를 impl 에 따라 실행한다(custom=전용 함수 / slot_setter / attribute_token / threshold)."""
    if spec.impl == "slot_setter":
        value = spec.detect(query)
        if value is None:
            return
        container = plan if spec.slot_on == "plan" else plan.setdefault("target_user", {})
        if spec.mode == "append":
            _append_unique(container.setdefault(spec.slot, []), value)
        else:
            container[spec.slot] = value
    elif spec.impl == "attribute_token":
        _run_attribute_token(_attribute_token_groups()[spec.group], query, plan)
    elif spec.impl == "threshold":
        _run_threshold_filter(spec, query, plan)
    elif spec.arg == "target_user":
        spec.apply(query, plan.setdefault("target_user", {}))
    elif spec.needs_policies:
        spec.apply(query, plan, business_policies)
    else:
        spec.apply(query, plan)


def _apply_named_filter(
    name: str,
    query: str,
    plan: dict[str, Any],
    *,
    business_policies: Path | None = None,
    init: bool = False,
) -> None:
    """레지스트리의 필터 하나를 이름으로 실행한다(union 재감지·테스트가 개별 필터를 호출하는 단일 진입점)."""
    spec = _deterministic_filter_registry()[name]
    if init and spec.init_key is not None:
        container = plan if spec.init_on == "plan" else plan.setdefault("target_user", {})
        container.setdefault(spec.init_key, [] if spec.init_list else None)
    _dispatch_filter(spec, query, plan, business_policies)


def _run_filters(
    names: tuple[str, ...],
    query: str,
    plan: dict[str, Any],
    *,
    business_policies: Path | None = None,
    init: bool = False,
) -> None:
    """이름 순서대로 결정론 필터를 실행한다(호출 방식은 레지스트리가 소유). init=True(auto 경로)면 희소한
    LLM 플랜에 슬롯을 선초기화한다 — 규칙 경로는 플랜을 리터럴로 선초기화하므로 init=False 로 건너뛴다."""
    for name in names:
        _apply_named_filter(name, query, plan, business_policies=business_policies, init=init)


# 결정론 필터 실행 순서(경로별). 순서는 문서화된 파싱 의존성을 보존한다(레지스트리 엔트리 주석 참조).
# rules 경로는 정규화 matched_terms 루프를 사이에 끼우므로 PRE/POST 두 단계로 나뉜다.
_RULES_PRE_FILTERS: tuple[str, ...] = (
    "age", "purchase_object", "purchase_date", "result_limit", "purchase_inactivity",
    "birthday", "signup_target", "sell_object", "dimension", "member_value", "macro_region",
    "aggregate", "purchase_count_threshold", "cart_aggregate", "cart_retention", "cart_type",
)
_RULES_POST_FILTERS: tuple[str, ...] = (
    "cart_repurchase", "cart_presence", "cart_absence", "inactivity_period", "recent_login",
    "signup_channel", "signup_device", "ratio_metric", "balance_condition", "balance_selection", "action_metric",
    "campaign_response", "no_additional_purchase", "campaign_response_frequency", "campaign_buy_amount",
    "campaign_buy_count", "cell_rate", "children_registered", "grade_threshold", "channel_consent", "member_flag", "policy",
    "group_ranking", "region_member_count", "region_density", "member_metric_ranking", "purchase_count_ranking",
    "zero_amount_purchase", "zero_purchase_count",
)
_AUTO_FILTERS: tuple[str, ...] = (
    "sell_object", "dimension", "member_value", "macro_region",
    "group_ranking", "region_member_count", "region_density",
    "member_metric_ranking", "purchase_count_ranking", "purchase_object", "purchase_date",
    "result_limit", "purchase_inactivity", "recent_login", "signup_channel", "signup_device",
    "ratio_metric", "balance_condition", "balance_selection", "action_metric", "campaign_response", "no_additional_purchase",
    "cart_presence", "cart_absence", "campaign_response_frequency", "children_registered",
    "grade_threshold", "channel_consent", "member_flag", "aggregate", "purchase_count_threshold",
    "campaign_buy_amount", "campaign_buy_count", "cell_rate", "cart_aggregate", "cart_retention", "cart_type",
    "birthday", "signup_target", "zero_amount_purchase", "zero_purchase_count",
)


def _build_single_query_plan(
    query: str,
    normalization_rules: Path | None = DEFAULT_NORMALIZATION_PATH,
    business_policies: Path | None = DEFAULT_POLICY_PATH,
    metric_lexicon: Path = DEFAULT_METRIC_LEXICON_PATH,
    sql_schema: Path = DEFAULT_SCHEMA_PATH,
    parser: str = "rules",
    llm_model: str = DEFAULT_LLM_MODEL,
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
    structured_query: StructuredQuery | None = None,
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
    # 의도·복잡도 판별(파이프라인 2단계): 이후 라우팅·관측용으로 plan 에 기록한다.
    rules_plan["complexity"] = classify_query_complexity(rules_plan)
    if parser == "rules":
        rules_plan["parser"] = {"type": "rules", "fallback_used": False}
        _attach_retrieval_scopes(rules_plan, scopes)
        return rules_plan

    # 단순 질의 직행(파이프라인: 단순 질의 → AST): 회원 속성만으로 완결되고 실추출 신호가 확인되면
    # LLM Query Plan 보강을 건너뛴다 — 결정론 rules 플랜이 조건을 전부 뽑았으므로 LLM 은 비용/지연만
    # 늘리고 조건을 흘릴 위험(재작성 소실류)만 있다. 복잡 질의만 Query Plan(LLM 보강) 경로를 탄다.
    # parser="llm"(명시 강제)은 존중하고 "auto" 에만 적용한다.
    if parser == "auto" and rules_plan["complexity"] == "simple" and _has_member_target_signal(rules_plan):
        rules_plan["parser"] = {
            "type": "rules",
            "requested": parser,
            "fallback_used": False,
            "skip_reason": "simple_query_direct",
        }
        _attach_retrieval_scopes(rules_plan, scopes)
        return rules_plan

    llm_plan, failure_reason = _try_llm_query_plan(
        parse_query,
        rules_plan,
        llm_model,
        prompt_dir,
        sql_schema,
        structured_query,
    )
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
    # 결정론 필터를 레지스트리 순서(_AUTO_FILTERS)로 실행한다 — 호출 방식·슬롯 선초기화(init=True: 희소한
    # LLM 플랜)는 _deterministic_filter_registry 가 소유한다. 집합식 operand 값 복원(_enrich)은 값 인덱스
    # (member_value/dimension) 뒤·랭킹 감지 전에 끼워야 하므로 macro_region 까지(앞 4개)와 그 뒤로 나눠 돈다.
    _run_filters(_AUTO_FILTERS[:4], parse_query, llm_plan, init=True)
    # LLM 이 만든 집합식 operand(지역/등급 디멘션)에도 프롬프트에서 복원한 값을 실어 컴파일되게 한다.
    _enrich_set_expression_operand_values(llm_plan, parse_query)
    _run_filters(_AUTO_FILTERS[4:], parse_query, llm_plan, init=True)
    # 결정론 정규식이 다 돈 뒤, LLM 이 채운 구조화 슬롯을 fill-if-empty 로 병합한다(덧셈형 — 정규식이
    # 발동한 슬롯은 불가침, LLM 은 정규식이 못 잡은 표현 변형만 메운다). 재실행 정규식이 LLM 값을 덮는
    # 순서 문제를 원천 차단하려고 여기(모든 _apply_* 이후)서 적용한다.
    _apply_llm_structured_slots(llm_plan)
    # behaviors 가 이미 소유한 canonical(예: cart_abandoner)이 lifecycle 에도 중복 분류되면 lifecycle 쪽을 뺀다.
    # lifecycle_extra_terms 에 behavior 겸용 어휘가 있어 LLM 이 같은 값을 lifecycle 로도 넣으면, compile 이 그
    # lifecycle 항목을 '미지원 제외 조건'으로 처리해 신뢰도 저점수 카드('생애주기: cart_abandoner')·경고가 뜬다
    # (behaviors 는 전용 빌더가 처리하므로 lifecycle 중복은 순전히 잡음). _apply_* 로 behaviors 가 다 채워진 뒤 실행.
    _dedupe_lifecycle_against_behaviors(llm_plan)
    # 값 보강까지 끝난 뒤, 컴파일되지 않는 리던던트 집합식(잘못 감싼 AND 나열, 지표/디멘션 canonical 오매칭
    # 등)은 버린다 — 결정론 필터가 조건을 커버하므로 SQL 을 막지 않는다(미정규화 값 clarification 은 유지).
    _drop_uncompilable_set_expressions(llm_plan)
    # dimension/속성 필터가 이미 소유한 operator-scan 집합식(평범한 지역 OR)을 버린다(중복 clarification 방지).
    _drop_dimension_consumed_set_expressions(llm_plan)
    # 어휘로 인식된 도메인을 기록한다(조건 생성 X) — SQL 이 안 나왔을 때 "조건을 못 찾음"과
    # "조건은 인식했지만 그 형태는 미지원"을 구별해 안내하기 위한 진단 정보다.
    _apply_recognized_domains(parse_query, llm_plan)
    # 의도·복잡도 판별: 결정론 필터가 전부 반영된 최종 플랜 기준으로 기록한다(관측/라우팅용).
    llm_plan["complexity"] = classify_query_complexity(llm_plan)
    _attach_retrieval_scopes(llm_plan, scopes)
    return llm_plan


def _dedupe_lifecycle_against_behaviors(plan: dict[str, Any]) -> None:
    """lifecycle 슬롯에 새어 들어온 behavior 용어(BEHAVIOR_TERMS)를 제거한다.

    cart_abandoner/repeat_buyer 처럼 BEHAVIOR_TERMS 이자 lifecycle_extra_terms 인 겸용 어휘는 LLM 이 같은
    값을 behaviors·lifecycle 양쪽에(또는 lifecycle 에만) 넣을 수 있다. behaviors 는 전용 빌더(카트 등)나 목적
    (objective) 문맥이 소유하지만, 같은 값이 lifecycle 에 남으면 compile_member_target_conditions 가 이를
    등가/활동 필터로 매핑하지 못해 '미지원 제외 조건'으로 처리하고, 신뢰도 리포트가 '생애주기: <값>'
    (unknown, 저점수) 카드와 '레지스트리/스키마 미확인' 경고를 낸다. behavior 용어는 lifecycle 에서 어떤
    필터 predicate 도 만들지 못하므로(신호는 behaviors/objective 가 소유) lifecycle 쪽만 걷어낸다."""
    target_user = plan.get("target_user")
    if not isinstance(target_user, dict):
        return
    lifecycle = target_user.get("lifecycle")
    if not isinstance(lifecycle, list) or not lifecycle:
        return
    target_user["lifecycle"] = [value for value in lifecycle if value not in BEHAVIOR_TERMS]


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
            # 닫힌 연령 구간의 '아닌/제외'(여집합이 분리 2구간이라 min/max 로 표현 불가) → NOT BETWEEN 목록.
            "age_exclude_ranges": [],
            "lifecycle": [],
            "interests": [],
            "preferred_channels": [],
            "behaviors": [],
            "purchase_object": None,
            "purchase_date": None,
            "price_sensitivity": None,
            "inactivity_period": None,
            "recent_login": None,
            "purchase_inactivity": None,
            "birthday_target": None,
            "signup_target": None,
            "aggregate_conditions": [],
            "cart_retention": None,
            "cart_type": None,
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
        "result_limit": None,
        "member_metric_selection": None,
        "set_expressions": parse_set_expressions_from_query(query, normalization_path=normalization_rules) if normalization_rules else [],
    }
    # 정규화 matched_terms 루프 앞의 결정론 필터를 레지스트리 순서(_RULES_PRE_FILTERS)로 실행한다.
    # 규칙 경로는 플랜을 위 리터럴로 선초기화하므로 슬롯 재초기화(init)는 건너뛴다.
    _run_filters(_RULES_PRE_FILTERS, query, plan)
    _apply_recognized_domains(query, plan)
    _enrich_set_expression_operand_values(plan, query)
    # 재작성문이 지표/디멘션 canonical(구매금액 등)을 집합식 operand 로 매칭해 컴파일 불가가 되면 SQL 이
    # 막힌다. 결정론 필터가 커버하는 리던던트 집합식은 버린다(age 소유 판정 전에 실행해 age-clear 오작동 방지).
    _drop_uncompilable_set_expressions(plan)
    # 집합식 term 소유권은 '컴파일 가능한' 집합식에만 준다 — clarification 으로만 남는 집합식이
    # 회원속성 term(성별 등)의 일반 적용까지 막아 조건이 증발하는 것 방지.
    set_expression_terms = _compilable_set_expression_canonical_values(plan["set_expressions"])
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

    # matched_terms 루프 뒤의 결정론 필터를 레지스트리 순서(_RULES_POST_FILTERS)로 실행한다. 순서 의존성은
    # 레지스트리 엔트리 주석이 문서화한다(예: no_additional_purchase 는 campaign_response 뒤, channel_consent 는
    # preferred_channels 가 matched_terms 루프에서 채워진 뒤, region_density 는 policy 의 semantic_resolutions 뒤).
    _run_filters(_RULES_POST_FILTERS, query, plan, business_policies=business_policies)
    # 모든 결정론 필터가 끝나(dimension_filters·gender·lifecycle 확정) 소유권이 확정된 뒤, dimension/속성
    # 필터가 이미 소유한 operator-scan 집합식(평범한 '서울 또는 경기' 지역 OR)을 버려 중복 clarification 을 막는다.
    _drop_dimension_consumed_set_expressions(plan)
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
        + _recent_login_retrieval_terms(plan["target_user"].get("recent_login"))
        + _query_tokens(normalized_query)
    )
    # 쿠폰 도메인 의미(사용 여부/건수 임계/순위/지표 비교/파생)를 JSON 스펙 기반으로 확정한다. 미지원
    # 판정을 게이트보다 먼저 남겨(게이트는 unsupported 가 있으면 양보) 어순 무관하게 일관되게 처리하고,
    # 논리식 리프(_compile_logical_leaf → _build_rule_query_plan)에서도 동일하게 동작하게 한다.
    _apply_coupon_semantics(query, plan)
    # 모든 결정론 필터가 끝난 뒤 미지원 표현을 명시 표시한다(조용한 오답/빈결과 방지). member_metric_selection
    # 등 필터 결과를 봐야 하므로 반드시 필터 실행 후에 둔다.
    _apply_unsupported_intent_gate(query, plan)
    return plan


def _build_target_user_tool_schema() -> dict[str, Any]:
    """LLM tool 의 target_user properties 를 IR 레지스트리 슬롯 + coarse 어휘에서 생성한다.

    예전엔 target_user 가 불투명 {"type":"object"} 라 LLM 이 어떤 구조화 슬롯(가입창/로그인창/카트/캠페인
    등)이 있는지 몰랐고, 그래서 그 조건들을 채우지 못했다. targeting_ir.structured_slot_shapes() 의 각
    SlotShape.schema 조각과 coarse enum(gender/lifecycle/behaviors/…)을 합쳐 슬롯을 명시적으로 노출한다.
    상세 검증·정규화는 _coerce_llm_structured_conditions 가 닫힌 어휘로 수행하므로 여기 스키마는 느슨해도
    안전하다."""
    properties: dict[str, Any] = {
        "gender": {"type": "string", "enum": sorted(GENDER_TERMS)},
        "age_min": {"type": "integer"},
        "age_max": {"type": "integer"},
        "lifecycle": {"type": "array", "items": {"type": "string", "enum": sorted(LIFECYCLE_TERMS)}},
        "interests": {"type": "array", "items": {"type": "string", "enum": sorted(INTEREST_TERMS)}},
        "preferred_channels": {"type": "array", "items": {"type": "string", "enum": sorted(CHANNEL_TERMS)}},
        "behaviors": {"type": "array", "items": {"type": "string", "enum": sorted(BEHAVIOR_TERMS)}},
        "price_sensitivity": {"type": "string", "enum": ["high", "low"]},
    }
    for shape in targeting_ir.structured_slot_shapes():
        if shape.container == "target_user":
            properties[shape.name] = shape.schema
    return {"type": "object", "description": "오디언스 조건", "properties": properties}


# Tool Calling(구조화 출력) 스키마: LLM 파서가 자유 텍스트 대신 이 함수 인자(JSON)로만 응답하게 강제한다.
# target_user 는 IR 레지스트리에서 파생한 슬롯 스키마를 노출하고(_build_target_user_tool_schema), plan-level
# 랭킹 슬롯도 함께 광고한다. 필드 상세 검증/정규화는 coerce 계층이 닫힌 어휘로 담당한다.
_QUERY_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_query_plan",
        "description": "사용자 캠페인/타겟팅 질문을 구조화된 Query Plan 으로 제출한다.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "recommend_campaign(발송/캠페인) | find_user_segment(고객 목록) | analyze_aggregation(집계 결과) | unknown",
                },
                "target_user": _build_target_user_tool_schema(),
                "exclude": {"type": "object", "description": "제외 조건(gender/interests/lifecycle)"},
                "campaign_constraints": {"type": "object", "description": "캠페인 목적/채널/혜택"},
                "aggregation_request": aggregation_request_json_schema(),
                **{
                    shape.name: shape.schema
                    for shape in targeting_ir.structured_slot_shapes()
                    if shape.container == "plan"
                },
            },
            "required": ["intent", "target_user"],
        },
    },
}


def _try_llm_query_plan(
    query: str,
    fallback_plan: dict[str, Any],
    llm_model: str,
    prompt_dir: Path | None,
    sql_schema: Path,
    structured_query: StructuredQuery | None = None,
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
                "content": _query_plan_user_prompt(
                    query, fallback_plan, prompt_dir, structured_query, sql_schema=sql_schema
                ),
            },
        ]
        _write_rag_llm_log(
            "llm_query_plan_request",
            {
                "mode": "openai_tool_calling",
                "model": llm_model,
                "temperature": 0,
                "tool": _QUERY_PLAN_TOOL["function"]["name"],
                "query": query,
                "fallback_plan": fallback_plan,
                "messages": messages,
                "message_summary": _message_summary(messages),
            },
        )
        # Tool Calling 구조화 출력: tool_choice 로 함수 호출을 강제해 JSON 스키마 안의 인자만 받는다.
        # (기존 json_object 방식보다 최상위 구조 이탈·자유 텍스트 혼입이 원천 차단된다.)
        response = _openai_chat_create(client, 
            model=llm_model,
            temperature=0,
            tools=[_QUERY_PLAN_TOOL],
            tool_choice={"type": "function", "function": {"name": _QUERY_PLAN_TOOL["function"]["name"]}},
            messages=messages,
        )
        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            content = tool_calls[0].function.arguments or "{}"
        else:
            # 일부 모델/프록시가 tool_choice 를 무시할 수 있어 content JSON 폴백을 허용한다.
            content = message.content or "{}"
        query_plan = _coerce_llm_query_plan(json.loads(content), fallback_plan, sql_schema)
        # 실제 전송된 프롬프트/응답을 트레이스 표시용으로 담아 둔다(retrieve 가 result 로 옮기고 plan 에선 제거).
        query_plan["_llm_trace"] = {
            "system": messages[0]["content"],
            "user": messages[1]["content"],
            "response": content,
        }
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


def _query_plan_system_prompt(prompt_dir: Path | None = DEFAULT_PROMPT_DIR) -> str:
    fallback = "\n".join(
        [
            "너는 캠페인 추천/NL2SQL Query Planner다.",
            "사용자 질문을 지정된 JSON 구조로만 반환한다.",
            "성별, 행동, 관심사, 채널, 혜택은 canonical 값만 사용한다.",
            "집계 요청은 SQL보다 먼저 aggregation_request 구조로 만들고 스키마에 연결되지 않는 항목은 unresolvedFields에 기록한다.",
            "필드 참조는 entity/field와 함께 실제 물리 table/column을 반드시 채우고, 정렬·순위·HAVING은 집계 지표 id를 metricId로 참조한다.",
            "부정 조건은 target_user에 긍정 조건으로 바꾸지 말고 exclude에 넣는다.",
            "반드시 JSON object만 출력한다.",
        ]
    )
    base = _read_prompt_template(prompt_dir, "query_plan_system.txt", fallback)
    examples = _query_plan_fewshot_examples(prompt_dir)
    if examples:
        base = f"{base}\n\n{examples}"
    return base


def _query_plan_fewshot_examples(prompt_dir: Path | None = DEFAULT_PROMPT_DIR) -> str:
    """입력 패턴별 few-shot 가이드(query_plan_examples.txt)를 읽어 시스템 프롬프트에 덧붙일 본문을 만든다.

    "이런 구조의 입력이면 query_plan 을 이렇게 채워라" 예시를 운영자가 파일/DB 로 관리하는 지점이다.
    '#' 로 시작하는 줄은 편집자용 주석이라 LLM 에 보내지 않고, 실제 예시 줄이 하나도 없으면
    빈 문자열을 반환해 시스템 프롬프트에 아무 것도 덧붙이지 않는다(기본 무동작 → 예시 채우면 활성).
    """
    raw = _read_prompt_template(prompt_dir, "query_plan_examples.txt", "")
    body = "\n".join(line for line in raw.splitlines() if not line.lstrip().startswith("#"))
    return body.strip()


def _query_plan_user_prompt(
    query: str,
    fallback_plan: dict[str, Any],
    prompt_dir: Path | None = DEFAULT_PROMPT_DIR,
    structured_query: StructuredQuery | None = None,
    sql_schema: Path = DEFAULT_SCHEMA_PATH,
) -> str:
    allowed = _llm_slot_allowed()
    allowed_values = {
        "gender": sorted(GENDER_TERMS),
        "lifecycle": sorted(LIFECYCLE_TERMS),
        "behaviors": sorted(BEHAVIOR_TERMS),
        "interests": sorted(INTEREST_TERMS),
        "channels": sorted(CHANNEL_TERMS),
        "offer_type": sorted(OFFER_TERMS),
        "objective": sorted(CAMPAIGN_OBJECTIVES),
        # 구조화 슬롯 닫힌 어휘(LLM 이 canonical 만 고르게) — 슬롯 스키마와 이중으로 명시한다.
        "duration_unit": sorted(targeting_ir.UNIT_DAYS),
        "operator": sorted(targeting_ir.OPERATORS),
        "campaign_response_canonical": sorted(allowed["campaign_responses"]),
        "cart_type_canonical": sorted({s["canonical"] for s in allowed["cart_types"].values()}),
        "aggregate_metric_id": sorted(allowed["aggregate_metrics"]),
        "cart_aggregate_metric": sorted(allowed["cart_aggregate_metrics"]),
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
            "[Structured Query]",
            "${structured_query}",
            "",
            "Fallback Rules Plan과 같은 JSON 구조로 보완된 Query Plan을 반환하라.",
            PLANNER_STRUCTURED_QUERY_RULES,
        ]
    )
    template = _read_prompt_template(prompt_dir, "query_plan_user.txt", fallback)
    rendered = _render_prompt_template(
        template,
        query=query,
        allowed_values=json.dumps(allowed_values, ensure_ascii=False, indent=2),
        fallback_plan=json.dumps(fallback_plan, ensure_ascii=False, indent=2),
        structured_query=json.dumps(
            structured_query.to_dict() if structured_query is not None else None,
            ensure_ascii=False,
            indent=2,
        ),
    )
    if "[Structured Query]" not in rendered:
        rendered += "\n\n[Structured Query]\n" + json.dumps(
            structured_query.to_dict() if structured_query is not None else None,
            ensure_ascii=False,
            indent=2,
        )
    if PLANNER_STRUCTURED_QUERY_RULES not in rendered:
        rendered += "\n\n" + PLANNER_STRUCTURED_QUERY_RULES
    aggregation_schema = _aggregation_schema_prompt_context(query, sql_schema)
    rendered += "\n\n[Aggregation Schema Metadata]\n" + json.dumps(
        aggregation_schema, ensure_ascii=False, indent=2
    )
    return rendered


def _aggregation_schema_prompt_context(query: str, schema_path: Path, table_limit: int = 12) -> list[dict[str, Any]]:
    """집계 플래너에 전달할 관련 테이블/컬럼 메타데이터를 토큰 점수로 제한한다."""
    context = SchemaMetadata.load(schema_path).prompt_context()
    terms = [token.casefold() for token in _query_tokens(query) if len(token.strip()) >= 2]

    def score(table: dict[str, Any]) -> tuple[int, int]:
        searchable = " ".join(
            str(value or "")
            for value in [table.get("table"), table.get("logicalName"), table.get("description")]
        ).casefold()
        searchable += " " + " ".join(
            str(value or "")
            for column in table.get("columns", [])
            for value in [column.get("column"), column.get("logicalName"), column.get("meaning")]
        ).casefold()
        hits = sum(1 for term in terms if term in searchable)
        important = sum(1 for column in table.get("columns", []) if column.get("important"))
        return hits, important

    selected = sorted(context, key=score, reverse=True)[:table_limit]
    for table in selected:
        columns = table.get("columns", [])
        preferred = [
            column for column in columns
            if column.get("important") or column.get("aggregatable") or column.get("semanticRoles")
        ]
        table["columns"] = (preferred or columns)[:40]
    return selected


def _llm_slot_allowed() -> dict[str, Any]:
    """SlotShape.allowed_key → 런타임 렉시콘 어휘 맵. LLM 은 canonical 만 고르고 SQL 술어/전체 shape 는
    여기서 채워 임의 SQL 주입·비존재 값을 막는다. (참조 테이블은 이 함수 호출 시점엔 모두 정의돼 있다.)"""
    campaign_map: dict[str, dict[str, Any]] = {}
    for canonical, predicate, _terms in _CAMPAIGN_RESPONSE_TARGETS:
        entry: dict[str, Any] = {"predicate": predicate}
        if canonical == "campaign_contact":
            entry["source"] = "camp_member_list"
        campaign_map[canonical] = entry
    # 구매반응 부정(NOT EXISTS)은 negated=True 로 요청. 술어는 긍정형과 같고 부정은 컴파일러가 감싼다.
    campaign_map.setdefault("no_buy_response", {"predicate": "R.BUY_RSPN_YN = 'Y'"})
    cart_type_map: dict[str, dict[str, Any]] = {}
    for entry in _cart_type_entries():
        shape = {
            "canonical": entry["canonical"],
            "value": entry["value"],
            "label": entry.get("ko_label") or entry["canonical"],
            "unpaid_only": False,  # LLM 은 미결제 문맥을 신뢰성 있게 못 정하므로 안전 기본(비한정).
        }
        cart_type_map[entry["canonical"]] = shape
        cart_type_map[entry["value"]] = shape
    return {
        "campaign_responses": campaign_map,
        "cart_types": cart_type_map,
        "cart_aggregate_metrics": set(_CART_AGGREGATE_METRIC_EXPRESSIONS),
        "aggregate_metrics": set(_aggregate_targets_config().get("metrics", {}) or {}),
    }


def _coerce_llm_structured_conditions(candidate: Any) -> dict[str, Any]:
    """LLM 후보의 구조화 슬롯을 IR SlotShape 의 닫힌 어휘로 검증·정규화해 {slot: value} 로 돌려준다.

    유효한 슬롯만 담고 어휘 이탈/형식 오류는 drop(환각 차단). 실제 플랜 병합은 _apply_llm_structured_slots
    가 재실행 _apply_* 이후 fill-if-empty(덧셈형)로 수행한다."""
    if not isinstance(candidate, dict):
        return {}
    allowed = _llm_slot_allowed()
    target_user = candidate.get("target_user") if isinstance(candidate.get("target_user"), dict) else {}
    out: dict[str, Any] = {}
    for shape in targeting_ir.slot_coercers():
        container = target_user if shape.container == "target_user" else candidate
        if shape.name not in container:
            continue
        allowed_vocab = allowed.get(shape.allowed_key) if shape.allowed_key else None
        coerced = shape.coerce(container[shape.name], allowed=allowed_vocab)
        if coerced is not None:
            out[shape.name] = coerced
    return out


def _is_empty_slot(current: Any) -> bool:
    return current is None or current == [] or current == {}


def _apply_llm_structured_slots(plan: dict[str, Any]) -> None:
    """coerce 완료된 LLM 구조화 슬롯을 fill-if-empty(덧셈형)로 병합하고 stash 를 제거한다.

    정규식이 이미 값을 채운 슬롯은 건드리지 않는다 — LLM 은 정규식이 비운(표현 변형으로 못 잡은) 슬롯만
    메운다. 신뢰 모델: 정규식 우선."""
    slots = plan.pop("_llm_structured_slots", None)
    if not isinstance(slots, dict):
        return
    target_user = plan.setdefault("target_user", {})
    resolved_unsupported_reasons: set[str] = set()
    for name, value in slots.items():
        shape = targeting_ir.SLOT_SHAPES.get(name)
        container = target_user if (shape is None or shape.container == "target_user") else plan
        if _is_empty_slot(container.get(name)):
            container[name] = value
            if shape is not None:
                resolved_unsupported_reasons.update(shape.resolves_unsupported)

    # A deterministic parser can leave a clarification before the LLM fills the
    # corresponding empty slot.  Keeping that stale state would make the SQL
    # builder fail closed despite a valid condition.  Clear only reasons that the
    # applied slot explicitly declares it resolves; unrelated gates stay intact.
    unsupported = plan.get("unsupported")
    if (
        isinstance(unsupported, dict)
        and unsupported.get("reason") in resolved_unsupported_reasons
    ):
        plan.pop("unsupported", None)


def _coerce_llm_query_plan(candidate: Any, fallback_plan: dict[str, Any], sql_schema: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    plan = json.loads(json.dumps(fallback_plan, ensure_ascii=False))
    if not isinstance(candidate, dict):
        return plan

    intent = candidate.get("intent")
    if intent in {"recommend_campaign", "find_user_segment", "analyze_aggregation", "unknown"}:
        plan["intent"] = intent

    target_user = candidate.get("target_user") if isinstance(candidate.get("target_user"), dict) else {}
    _merge_scalar(plan["target_user"], target_user, "gender", GENDER_TERMS)
    _merge_int(plan["target_user"], target_user, "age_min")
    _merge_int(plan["target_user"], target_user, "age_max")
    _merge_age_exclude_ranges(plan["target_user"], target_user)
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
    aggregation_candidate = candidate.get("aggregation_request")
    if isinstance(aggregation_candidate, dict):
        aggregation_request, aggregation_errors = parse_aggregation_request(aggregation_candidate, sql_schema)
        if aggregation_request is not None:
            aggregation_payload = aggregation_request.to_dict()
            # LLM이 회원 목록 필터를 aggregation_request(outputColumns/filters만 존재)로도 포장한다.
            # 실제 집계·그룹·순위 연산이 없고 intent도 목록/캠페인이면 이를 분석 계약으로 채택하지 않는다.
            # 빈 집계 객체의 존재만으로 _attach_query_output_contract가 expected_grain=analytical로
            # 승격해 정상 회원 SQL을 query_result_grain_mismatch로 차단하는 것을 막는다.
            deterministic_analytical = (
                fallback_plan.get("intent") == "analyze_aggregation"
                or isinstance(fallback_plan.get("aggregation_request"), dict)
            )
            if _is_substantive_aggregation_request(aggregation_payload) and deterministic_analytical:
                plan["aggregation_request"] = aggregation_payload
                plan["aggregation_request_validation"] = {
                    "valid": not aggregation_errors,
                    "errors": [error.to_dict() for error in aggregation_errors],
                }
            elif intent == "analyze_aggregation" and not deterministic_analytical:
                # 목록 질의를 LLM이 임의 집계로 바꿔도 출력 grain은 결정론 판정 결과를 따른다.
                plan["intent"] = fallback_plan.get("intent", "unknown")
    set_expressions = candidate.get("set_expressions")
    if isinstance(set_expressions, list):
        coerced_set_expressions = [_coerce_llm_set_expression(expression) for expression in set_expressions]
        coerced_set_expressions = [expression for expression in coerced_set_expressions if expression is not None]
        if coerced_set_expressions:
            plan["set_expressions"] = coerced_set_expressions
    # 구조화 슬롯(가입창/로그인창/카트/캠페인/집계 등)을 IR 레지스트리 닫힌 어휘로 검증해 stash 에 담는다.
    # 실제 병합은 _build_llm_query_plan 이 재실행 _apply_* 이후 fill-if-empty 로 수행(덧셈형).
    plan["_llm_structured_slots"] = _coerce_llm_structured_conditions(candidate)
    return plan


def _is_substantive_aggregation_request(request: dict[str, Any]) -> bool:
    """회원 행 선택이 아니라 실제 분석 결과 단위를 요구하는 집계 IR인지 판정한다."""
    ranking = request.get("ranking") if isinstance(request.get("ranking"), dict) else {}
    return any(
        (
            request.get("aggregations"),
            request.get("derivedMetrics"),
            request.get("groupings"),
            request.get("postAggregationFilters"),
            request.get("comparison"),
            request.get("dateGrain"),
            ranking.get("enabled"),
        )
    )


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


def _merge_age_exclude_ranges(target: dict[str, Any], source: dict[str, Any]) -> None:
    """제외 연령 구간 목록을 병합한다(LLM 이 지어낸 항목은 정수쌍·유효범위만 통과)."""
    merged = list(target.get("age_exclude_ranges", []))
    for candidate in source.get("age_exclude_ranges", []) or []:
        if (
            isinstance(candidate, (list, tuple))
            and len(candidate) == 2
            and all(isinstance(v, int) and 0 <= v <= 120 for v in candidate)
            and candidate[0] <= candidate[1]
        ):
            pair = [int(candidate[0]), int(candidate[1])]
            if pair not in merged:
                merged.append(pair)
    if merged:
        target["age_exclude_ranges"] = merged


def _merge_list(target: dict[str, Any], source: dict[str, Any], key: str, allowed_values: set[str]) -> None:
    values = source.get(key)
    if not isinstance(values, list):
        return
    canonical_values = [value for value in values if isinstance(value, str) and value in allowed_values]
    if canonical_values:
        target[key] = _unique_strings([*target.get(key, []), *canonical_values])


def _infer_intent(query: str) -> str:
    compact_query = query.replace(" ", "").casefold()
    # 과거 행동을 완료형으로 조회하면서 회원 집합/인원수를 요구하면 캠페인 추천이 아니라 세그먼트
    # 조회다. 특히 "캠페인에 반응한 회원은 몇 명"을 '캠페인' 한 단어 때문에 추천 intent로 보내면
    # 반응 팩트 조회가 사라지므로, 발송/생성 목적보다 먼저 출력 형태와 완료 행동을 함께 본다.
    if _is_completed_behavior_segment_lookup(query):
        return "find_user_segment"
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


_MEMBER_OUTPUT_RE = re.compile(r"회원|고객|사용자|가입자|대상|몇\s*명|인원\s*수|회원\s*수|고객\s*수")
_COUNT_OUTPUT_SIGNAL_RE = re.compile(
    r"(?:몇\s*(?:명|건|개|곳)|(?:회원|고객|사용자|가입자|구매자|상품|제품|주문|구매|반응)\s*(?:수|인원|개수|건수))"
)
_COMPLETED_BEHAVIOR_RE = re.compile(
    r"(?:구매|구입|주문)(?:했|한|했던)|장바구니.{0,10}(?:담|보관|있)|"
    r"캠페인.{0,12}(?:반응|응답)(?:했|한|없는|않)|(?:로그인|방문)(?:했|한|하지않|없는)"
)
_OUTREACH_ACTION_RE = re.compile(
    r"추천(?:해|하|안)|캠페인(?:을)?\s*(?:생성|만들|기획)|발송(?:해|하|할)|"
    r"보내(?:줘|고|기)|알리(?:고|기)|홍보|유도|판매하고\s*싶"
)


def _is_completed_behavior_segment_lookup(query: str) -> bool:
    """완료된 과거 행동 + 회원/인원 출력 요청을 추천 intent보다 우선하는 세그먼트 조회로 판정."""
    return bool(
        _MEMBER_OUTPUT_RE.search(query)
        and _COMPLETED_BEHAVIOR_RE.search(query)
        and not _OUTREACH_ACTION_RE.search(query)
    )


_PURCHASE_POSITIVE_MEMBERSHIP_RE = re.compile(
    r"(?:구매|구입|주문)(?:(?:이력|내역)?(?:을|를|은|는|이|가|도)?(?:했|한|했던|있는)|(?=(?:고객|회원|사용자|유저)))"
)
_CAMPAIGN_GENERIC_RESPONSE_RE = re.compile(
    r"캠페인(?:에|에서|을|를|의)?(?:는|은|도)?(?:반응|응답)"
    r"(?:을|를|이|가|은|는|도)?(?P<negative>하지않|안한|안했|않은|없)?(?:했|한|자|회원|고객)?"
)
_WHOLE_MEMBER_RE = re.compile(r"(?:전체|모든|전부|모두의?)\s*(?:회원|고객|사용자|가입자)")
_ACTIVE_MEMBER_RE = re.compile(r"정상\s*(?:회원|고객|사용자)|활성\s*상태\s*(?:회원|고객|사용자)")
_CONDITION_LANGUAGE_RE = re.compile(
    r"구매|구입|주문|재구매|장바구니|카트|캠페인|반응|로그인|접속|방문|쿠폰|찜|"
    r"거주|지역|등급|성별|남성|여성|나이|연령|휴면|탈퇴|정상|활동|가입|수신|블랙리스트"
)


def _aggregate_conditions_imply_purchase_membership(
    target_user: dict[str, Any], membership: dict[str, Any]
) -> bool:
    """주문 집계가 같은 범위의 구매 존재를 이미 보장하는지 판정한다.

    회원별 주문 집계 서브쿼리에 행이 생기려면 주문이 적어도 하나 있어야 하므로, 기간 없는 구매 존재는
    어떤 유효한 주문 집계로도 충족된다. 기간이 있는 구매 존재는 같은 rolling window를 가진 집계만
    충족한다. 예를 들어 ``누적 20만원 이상이고 최근 30일 구매``의 최근 구매 조건은 평생 집계로
    대체할 수 없으므로 별도 EXISTS로 남긴다.
    """
    conditions = [
        condition
        for condition in target_user.get("aggregate_conditions") or []
        if isinstance(condition, dict)
        and condition.get("metric_id")
        and condition.get("operator") in {"=", ">", ">=", "<", "<="}
        and isinstance(condition.get("threshold"), (int, float))
    ]
    if not conditions:
        return False
    membership_window = membership.get("window_days")
    if not isinstance(membership_window, int):
        return True
    # 절대 구매기간(purchase_date)은 실행시점 기준 rolling window와 같은 범위라고 볼 수 없다.
    if isinstance(target_user.get("purchase_date"), dict):
        return False
    return any(condition.get("window_days") == membership_window for condition in conditions)


def _mark_purchase_membership_ownership(target_user: dict[str, Any]) -> None:
    """중복 구매 존재 조건의 SQL 소유자를 기록한다(의미 조건 자체는 보존)."""
    membership = target_user.get("purchase_membership")
    if not isinstance(membership, dict) or membership.get("operator") != "exists":
        return
    if _aggregate_conditions_imply_purchase_membership(target_user, membership):
        membership["satisfied_by"] = "aggregate_conditions"
    else:
        membership.pop("satisfied_by", None)


def _apply_core_membership_semantics(query: str, plan: dict[str, Any]) -> None:
    """핵심 행동의 존재/부재 방향을 구조화한다.

    특정 문장 전체를 하드코딩하지 않고 행동 도메인과 operator를 분리한다. 기존 상세 파서가 이미
    더 구체적인 조건(상품, 절대기간, 캠페인 구매반응)을 만든 경우에는 그 소유권을 보존한다.
    """
    target_user = plan.setdefault("target_user", {})
    compact = query.replace(" ", "").casefold()

    # 캠페인 구매반응은 campaign_responses 전용 의미이므로 일반 주문 존재 조건으로 중복 승격하지 않는다.
    campaign_scoped_purchase = "캠페인" in compact and bool(target_user.get("campaign_responses"))
    purchase_negative = bool(_PURCHASE_NEG_RE.search(compact))
    cart_checkout_context = "cart_abandoner" in (target_user.get("behaviors") or []) and any(
        marker in compact for marker in ("담고구매", "담았지만구매", "장바구니에담고", "장바구니담고")
    )
    repurchase_negative = "재구매" in compact and purchase_negative
    if purchase_negative and not campaign_scoped_purchase and not cart_checkout_context and not repurchase_negative:
        # 기간이 있으면 기존 purchase_inactivity가 기간 anti-join을 소유한다. 기간이 없으면 평생 무주문.
        if not isinstance(target_user.get("purchase_inactivity"), dict):
            _append_unique(target_user.setdefault("behaviors", []), "no_purchase")
        target_user.pop("purchase_membership", None)
    elif _PURCHASE_POSITIVE_MEMBERSHIP_RE.search(compact) and not campaign_scoped_purchase:
        window = _parse_duration_window(query, anchor_terms=("구매", "구입", "주문"))
        condition: dict[str, Any] = {"domain": "purchase", "operator": "exists"}
        if isinstance(window, dict) and isinstance(window.get("min_days"), int):
            condition["window_days"] = window["min_days"]
        target_user["purchase_membership"] = condition
        # 기간 수식어가 상품명으로 오인된 경우를 제거한다("최근 30일 이내 구매한 회원" → 상품 '이내').
        if target_user.get("purchase_object") in {"이내", "동안", "최근", "기간", "내"}:
            target_user["purchase_object"] = None
            target_user.pop("purchase_object_kind", None)

    # 구매금액/횟수 집계가 같은 범위의 구매 존재를 이미 증명하면 의미 조건은 남기되 SQL 소유권을 집계로
    # 넘긴다. 컴파일러·커버리지 계층은 이 표식을 보고 중복 EXISTS를 요구하거나 방출하지 않는다.
    _mark_purchase_membership_ownership(target_user)

    # "캠페인에 반응한 회원"의 일반 반응은 오퍼 또는 구매 반응 중 하나가 있는 회원으로 정의한다.
    # 구체 반응(오퍼/구매/쿠폰/접촉)이 이미 추출됐으면 그 정의를 우선한다.
    generic_response = _CAMPAIGN_GENERIC_RESPONSE_RE.search(compact)
    if generic_response and not target_user.get("campaign_responses"):
        negative = bool(generic_response.group("negative"))
        target_user["campaign_responses"] = [{
            "canonical": "no_campaign_response" if negative else "campaign_response",
            "predicate": "(R.OFFR_RSPN_YN = 'Y' OR R.BUY_RSPN_YN = 'Y')",
            "negated": negative,
        }]

    # 정상 회원은 상태코드 정책을 명시적으로 사용한다. 휴면도 기존 lifecycle 매핑이 있으면 정의 출처를
    # 아래 semantic_conditions에 기록해 상태기반/행동기반이 묵시적으로 섞이지 않게 한다.
    if _ACTIVE_MEMBER_RE.search(query) and "normal_member" not in target_user.setdefault("lifecycle", []):
        target_user["lifecycle"].append("normal_member")

    semantic_conditions: list[dict[str, Any]] = []
    membership = target_user.get("purchase_membership")
    if isinstance(membership, dict):
        semantic_conditions.append({**membership, "is_primary_condition": True})
    if "no_purchase" in (target_user.get("behaviors") or []):
        semantic_conditions.append({"domain": "purchase", "operator": "not_exists", "is_primary_condition": True})
    inactivity = target_user.get("purchase_inactivity")
    if isinstance(inactivity, dict):
        semantic_conditions.append({
            "domain": "purchase", "operator": "not_exists", "window_days": inactivity.get("min_days"),
            "is_primary_condition": True,
        })
    if "cart_abandoner" in (target_user.get("behaviors") or []):
        semantic_conditions.append({"domain": "cart", "operator": "exists", "is_primary_condition": True})
    if target_user.get("cart_absence"):
        semantic_conditions.append({"domain": "cart", "operator": "not_exists", "is_primary_condition": True})
    for response in target_user.get("campaign_responses") or []:
        if isinstance(response, dict):
            semantic_conditions.append({
                "domain": "campaign_response",
                "operator": "not_exists" if response.get("negated") else "exists",
                "canonical": response.get("canonical"), "is_primary_condition": True,
            })
    if "dormant" in (target_user.get("lifecycle") or []):
        semantic_conditions.append({"domain": "dormancy", "definition_type": "status_code", "is_primary_condition": True})
    inactivity_period = target_user.get("inactivity_period")
    if isinstance(inactivity_period, dict):
        semantic_conditions.append({
            "domain": "dormancy", "definition_type": "inactivity_period",
            "days": inactivity_period.get("min_days"), "is_primary_condition": True,
        })
    plan["semantic_conditions"] = semantic_conditions


def _attach_member_policy_contract(query: str, plan: dict[str, Any]) -> None:
    """최종 SQL에 적용할 회원상태 정책과 출처를 Query Plan에 명시한다.

    예전 타겟팅 경로는 SQL 빌더가 NORMAL 술어를 암묵적으로 붙여 의미 검증기가 서비스 정책인지 알 수
    없었다. 분석 계약과 같은 ``appliedPolicyFilters`` 형태로 기록해 생성·검증이 하나의 유효 조건 계약을
    공유하게 한다. 사용자가 상태를 직접 지정했거나 전체 회원을 요청한 경우에는 정책 필터를 비운다.
    """
    aggregation = plan.get("aggregation_request")
    if isinstance(aggregation, dict):
        rules = aggregation.get("businessRules") if isinstance(aggregation.get("businessRules"), dict) else {}
        applied = rules.get("appliedPolicyFilters") if isinstance(rules.get("appliedPolicyFilters"), list) else []
        plan["member_policy"] = {
            "policy_id": "active_member",
            "mode": rules.get("memberScope") or "default",
            "source": "service_policy",
            "appliedPolicyFilters": applied,
        }
        return

    scope = resolve_member_scope(query, DEFAULT_MEMBER_TARGET_FILTERS_PATH)
    if plan.get("member_scope") == "all":
        scope = {**scope, "mode": "all", "states": []}

    compiled = compile_member_target_conditions(plan)
    applied_policy_filters: list[dict[str, Any]] = []
    source = "service_policy"
    mode = str(scope.get("mode") or "default")
    if compiled["forces_state"]:
        # 상태/미접속 조건은 사용자 조건 컴파일러가 소유한다. 기본 NORMAL 정책은 적용하지 않는다.
        source = "user"
        mode = "explicit"
    elif mode != "all":
        policy = active_member_filter(
            query,
            table=_member_table(),
            alias=_member_alias(),
            path=DEFAULT_MEMBER_TARGET_FILTERS_PATH,
        )
        if isinstance(policy, dict):
            applied_policy_filters.append({
                "id": policy.get("id") or "policy_active_member",
                "column": policy.get("column"),
                "operator": policy.get("operator"),
                "value": policy.get("value"),
                "mode": policy.get("policyMode") or mode,
            })

    plan["member_policy"] = {
        "policy_id": str(scope.get("policy_id") or "active_member"),
        "mode": mode,
        "states": list(scope.get("states") or []),
        "source": source,
        "appliedPolicyFilters": applied_policy_filters,
    }


def _attach_query_output_contract(query: str, plan: dict[str, Any]) -> None:
    """질문의 기대 결과 단위와 API 결과 계약을 SQL 생성 전에 확정한다."""
    _normalize_aggregation_axis_filters(plan)
    if isinstance(plan.get("aggregation_request"), dict) or plan.get("intent") == "analyze_aggregation":
        if isinstance(plan.get("aggregation_request"), dict):
            plan["intent"] = "analyze_aggregation"
        analytical = plan.get("analytical_intent") if isinstance(plan.get("analytical_intent"), dict) else {}
        if analytical.get("result_shape") == "single_member":
            expected_grain = "member"
            requires_member_id = True
        else:
            expected_grain = "analytical"
            requires_member_id = False
    elif isinstance(plan.get("region_member_count_target"), dict):
        expected_grain = "region"
        requires_member_id = False
    else:
        # 타겟 API의 기본 계약은 회원 ID 집합이며 인원수는 실행부가 별도로 계산한다.
        expected_grain = "member"
        requires_member_id = True
    whole_target = bool(_WHOLE_MEMBER_RE.search(query))
    if (
        expected_grain == "member"
        and not _has_member_target_signal(plan)
        and _MEMBER_OUTPUT_RE.search(query)
        and not _CONDITION_LANGUAGE_RE.search(query)
    ):
        # "회원은 몇 명인가"도 조건 없는 전체 회원 조회로 명시한다. 정상 회원 표현은 위에서 상태 조건으로
        # 구조화됐으므로 여기에 오지 않는다.
        whole_target = True
    plan["output_contract"] = {
        "target_entity": (plan.get("analytical_intent") or {}).get("target_entity") or "member",
        "expected_grain": expected_grain,
        "requires_member_id": requires_member_id,
        "requires_member_no_as_cust_id": (
            expected_grain == "member"
            and plan.get("intent") in {"recommend_campaign", "find_user_segment"}
        ),
        "whole_target": whole_target,
    }
    if not isinstance(plan.get("capability_check"), dict):
        plan["capability_check"] = {
            "endpoint": "/target-sql",
            "query_type": "member_selection",
            "result_shape": "member_rows",
            "passed": True,
            "reason": None,
        }
        plan.setdefault("selected_route", "member_target_sql")
    if whole_target:
        plan["member_scope"] = "all"
    _attach_member_policy_contract(query, plan)
    # 명시적인 지역 그룹/랭킹 슬롯이 결과 축을 소유하면 일반 "지역=시도" 의미정책은 더 이상 필터
    # requirement가 아니다. 남겨두면 시군구 PARTITION SQL에 B.SIDO를 추가 요구해 정상 SQL을 차단한다.
    if any(isinstance(plan.get(key), dict) for key in (
        "group_ranking_target", "region_member_count_target", "region_density_target",
    )):
        plan["semantic_resolutions"] = [
            item for item in plan.get("semantic_resolutions") or []
            if not (isinstance(item, dict) and item.get("policy_id") == "region_context_default")
        ]


def _preserve_count_output_query(effective_query: str, targeting_query: str) -> str:
    """스코프 분리가 잘라낸 숫자 집계 출력 지시를 Query Planner 입력에 보존한다."""
    if _COUNT_OUTPUT_SIGNAL_RE.search(effective_query) and not _COUNT_OUTPUT_SIGNAL_RE.search(targeting_query):
        return effective_query
    return targeting_query


_GROUP_AXIS_OBJECT_RE = re.compile(r"^\s*[0-9A-Za-z가-힣_+\-]+\s*별\s*$", re.IGNORECASE)
_DATE_WINDOW_UNRESOLVED_RE = re.compile(
    r"(?:\b(?:date|from|to|today|window|yyyy(?:mmdd)?)\b|order_date|purchase_date|날짜|기간|현재일|실행\s*시점)",
    re.IGNORECASE,
)


def _normalize_aggregation_axis_filters(plan: dict[str, Any]) -> None:
    """그룹 축 표현을 구매 상품 필터로 중복 해석한 결과를 제거한다.

    ``<차원>별 구매 고객 수``에서 ``<차원>별``은 GROUP BY 축이지 상품명이 아니다. 특정 차원명에
    의존하지 않고, 구조화된 그룹 집계가 존재하면서 구매 상품 후보 전체가 ``별`` 축 형태일 때만
    정리한다. 실제 상품 조건이 함께 있으면 그 조건은 그대로 보존한다.
    """
    aggregation = plan.get("aggregation_request")
    if not isinstance(aggregation, dict) or not aggregation.get("groupings"):
        return
    target_user = plan.get("target_user")
    if not isinstance(target_user, dict):
        return

    raw_objects = target_user.get("purchase_objects")
    if isinstance(raw_objects, list):
        remaining = []
        for item in raw_objects:
            value = item.get("value") if isinstance(item, dict) else item
            if isinstance(value, str) and _GROUP_AXIS_OBJECT_RE.fullmatch(value):
                continue
            remaining.append(item)
        if remaining:
            target_user["purchase_objects"] = remaining
        else:
            target_user.pop("purchase_objects", None)

    purchase_object = target_user.get("purchase_object")
    if isinstance(purchase_object, str) and _GROUP_AXIS_OBJECT_RE.fullmatch(purchase_object):
        target_user.pop("purchase_object", None)
        target_user.pop("purchase_object_kind", None)


def _normalize_purchase_aggregation_request(plan: dict[str, Any]) -> None:
    """결정론적으로 확인된 구매 기간·대상 grain을 집계 IR에 반영한다.

    LLM이 상대 기간을 실행 시점과 무관한 고정 날짜로 바꾸거나, 고객 수를 주문 행 ``COUNT(*)``로
    표현해도 구매 존재 조건의 ``window_days``와 설정 기반 회원키를 단일 진실 소스로 사용한다.
    기간이나 고객 집계가 아닌 요청은 건드리지 않는다.
    """
    aggregation = plan.get("aggregation_request")
    target_user = plan.get("target_user")
    if not isinstance(aggregation, dict) or not isinstance(target_user, dict):
        return
    # 레지스트리 계약은 지표·모집단 스코프·기간을 이미 물리 매핑으로 확정한 상태다. LLM 플래너 출력을
    # 교정하려는 이 후처리가 그 위에 다시 회원키/테이블을 덮어쓰면, 요구사항만 바뀌고 SQL 은 그대로라
    # 정상 SQL 이 "요청된 집계가 없다"로 탈락한다(검증 대상이 검증 기준을 바꾸는 순환).
    if (aggregation.get("businessRules") or {}).get("contractSource") == "analytics_registry":
        return
    membership = target_user.get("purchase_membership")
    if not isinstance(membership, dict) or membership.get("operator") != "exists":
        return

    registry = _order_count_targets_config()
    member_column = str(registry.get("join_column") or _member_key_column())
    date_column = str(registry.get("order_date_column") or "ORDER_DATE")
    evidence_tables = {
        str(table).casefold(): str(table)
        for table in registry.get("evidence_tables", [])
        if isinstance(table, str) and table
    }
    configured_table = str(registry.get("table") or "")
    if configured_table:
        evidence_tables.setdefault(configured_table.casefold(), configured_table)

    filters = aggregation.get("filters")
    filters = filters if isinstance(filters, list) else []
    purchase_table = None
    window_days = membership.get("window_days")
    normalized_filters = []
    relative_filter_resolved = False
    for item in filters:
        if not isinstance(item, dict):
            normalized_filters.append(item)
            continue
        table = item.get("table")
        if isinstance(table, str) and table.casefold() in evidence_tables:
            purchase_table = table
        column = str(item.get("column") or item.get("field") or "")
        is_order_date = column.casefold() == date_column.casefold()
        if (
            isinstance(window_days, int)
            and window_days > 0
            and is_order_date
            and item.get("operator") in {"gte", ">="}
        ):
            item["value"] = f"P{window_days}D"
            relative_filter_resolved = True
        # '최근 N일'의 상한은 현재 시점으로 암묵적이다. 모델이 값 없는 별도 종료 필터를 만든 경우에는
        # 실행 불가능한 필터를 남기지 않는다. 명시 값이 있는 상한은 보존한다.
        if (
            isinstance(window_days, int)
            and window_days > 0
            and is_order_date
            and item.get("operator") in {"lte", "<="}
            and (item.get("value") is None or item.get("value") == "")
        ):
            continue
        normalized_filters.append(item)
    aggregation["filters"] = normalized_filters
    if relative_filter_resolved:
        unresolved = aggregation.get("unresolvedFields")
        if isinstance(unresolved, list):
            aggregation["unresolvedFields"] = [
                note for note in unresolved
                if not (isinstance(note, str) and _DATE_WINDOW_UNRESOLVED_RE.search(note))
            ]
    if isinstance(window_days, int) and window_days > 0:
        assumptions = aggregation.get("assumptions")
        assumptions = assumptions if isinstance(assumptions, list) else []
        anchor_markers = ("current date", "today", "anchor date", "현재 날짜", "현재일", "오늘", "기준일")
        aggregation["assumptions"] = [
            assumption for assumption in assumptions
            if not (
                isinstance(assumption, str)
                and any(marker in assumption.casefold() for marker in anchor_markers)
            )
        ]
        aggregation["assumptions"].append(
            f"Relative period P{window_days}D is evaluated using the database current date at execution time."
        )

    target_entity = str(aggregation.get("targetEntity") or "").casefold()
    member_target = bool(
        target_entity in {"member", "customer", "user", "회원", "고객"}
        or re.search(r"(?:^|_)(?:member|customer|user)(?:$|_)", target_entity)
        or any(term in target_entity for term in ("회원", "고객"))
    )
    if not member_target:
        return
    metrics = aggregation.get("aggregations")
    if not isinstance(metrics, list):
        return
    for metric in metrics:
        if not isinstance(metric, dict) or metric.get("function") not in {"count", "count_distinct"}:
            continue
        metric_table = metric.get("table")
        if isinstance(metric_table, str) and metric_table.casefold() in evidence_tables:
            purchase_table = metric_table
        table = purchase_table or configured_table
        if not table:
            continue
        metric.update({
            "function": "count_distinct",
            "entity": target_entity or "member",
            "field": member_column,
            "table": table,
            "column": member_column,
            "distinct": True,
        })


def _refresh_aggregation_request_validation(plan: dict[str, Any], schema_path: Path) -> None:
    """후처리로 바뀐 집계 IR을 입력 게이트 전에 다시 스키마 검증한다."""
    payload = plan.get("aggregation_request")
    if not isinstance(payload, dict):
        return
    request, errors = parse_aggregation_request(payload, schema_path, dialect=_member_dialect().name)
    if request is None:
        return
    plan["intent"] = "analyze_aggregation"
    plan["aggregation_request"] = request.to_dict()
    plan["aggregation_request_validation"] = {
        "valid": not errors,
        "errors": [error.to_dict() for error in errors],
    }


def _extract_conditions_ir(query_plan: dict[str, Any]):
    """query_plan → 타겟 조건 IR(targeting_ir.extract_target_conditions)의 graph_rag 진입점.

    설정 소유 값(order_count_targets.behaviors 키 집합)을 주입한다 — IR 모듈은 설정/graph_rag 를
    import 하지 않는 순수 도메인 계층이라 컨텍스트를 호출자가 준다."""
    return extract_target_conditions(
        query_plan, order_count_behaviors=frozenset(_order_count_targets_config()["behaviors"])
    )


def _has_member_target_signal(query_plan: dict[str, Any]) -> bool:
    """실DB 로 실제 추출 SQL 을 만드는 회원/주문 타겟 신호가 하나라도 있는지 판정한다.

    회원 속성 신호는 compile_member_target_conditions, 그 외 결정론 빌더 신호(생일/밀집지역/지표랭킹/
    주문횟수/미구매창/집계조건/구매이력/장바구니/캠페인반응 등)는 조건 IR 레지스트리
    (targeting_ir.CONDITION_SPECS 의 signals_target)에서 파생한다 — 새 조건 유형은 레지스트리에
    spec 하나 추가로 여기·intent 승격(_promote_unknown_intent_for_target_signal)·recommend_campaign
    필수조건 검증(validate_required_input_conditions)에 동시에 반영된다(수작업 OR 목록 제거)."""
    if compile_member_target_conditions(query_plan)["has_signal"]:
        return True
    return any(condition.spec.signals_target for condition in _extract_conditions_ir(query_plan))


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
    # 호출자(plan 초기화 여부)와 무관하게 동작하도록 연령 슬롯을 보장한다.
    target_user.setdefault("age_min", None)
    target_user.setdefault("age_max", None)
    target_user.setdefault("age_exclude_ranges", [])
    # 연대 범위("20~30대"). '아닌/제외' 문맥이면 닫힌 구간의 여집합이라 min/max 로 못 담아 NOT BETWEEN 으로 뺀다.
    decade_range_match = re.search(r"(?P<min>[1-9]\d)\s*(?:~|-|부터)\s*(?P<max>[1-9]\d)\s*대", query)
    if decade_range_match:
        lo = _valid_age(decade_range_match.group("min"))
        max_decade = _valid_age(decade_range_match.group("max"))
        hi = max_decade + 9 if max_decade is not None else None
        if lo is not None and hi is not None and _age_range_excluded(query, decade_range_match):
            target_user["age_exclude_ranges"].append([lo, hi])
        else:
            target_user["age_min"] = lo
            target_user["age_max"] = hi
        return

    # 단일/복수 연대("20대", "20·40대"). 뒤에 경계어가 붙으면("40대 이상") 연대를 열린 경계로 열어주고,
    # 제외/아닌이면 NOT BETWEEN, 경계어·제외 없는 순수 연대만 기존처럼 닫힌 포함 범위로 병합한다.
    included_decades: list[int] = []
    for match in re.finditer(r"(?P<decade>[1-9]\d)\s*대\s*(?P<op>이상|이하|초과|미만)?", query):
        decade = int(match.group("decade"))
        op = match.group("op")
        excluded = _age_range_excluded(query, match)
        if op:
            _apply_decade_operator(target_user, decade, op, excluded)
        elif excluded:
            target_user["age_exclude_ranges"].append([decade, decade + 9])
        else:
            included_decades.append(decade)
    if included_decades:
        target_user["age_min"] = min(included_decades)
        target_user["age_max"] = max(included_decades) + 9

    range_match = re.search(r"(?P<min>\d{1,3})\s*(?:세)?\s*(?:~|-|부터)\s*(?P<max>\d{1,3})\s*세?", query)
    if range_match:
        lo = _valid_age(range_match.group("min"))
        hi = _valid_age(range_match.group("max"))
        if lo is not None and hi is not None and _age_range_excluded(query, range_match):
            target_user["age_exclude_ranges"].append([lo, hi])
        else:
            target_user["age_min"] = lo
            target_user["age_max"] = hi

    # 단일 '세' 경계. 연산자 어휘는 공용 비교 문법과 공유하므로(부사형·동사형·'보다 많은/적은') age 도
    # "40세보다 많은"·"40세를 넘는"을 그대로 잡는다. 하한/상한을 각각 찾는 건 한 문장에 둘 다("30세 이상
    # 50세 미만") 올 수 있어서다. 방향(>=,>,<=,<) 판정은 _comparison_operator 로 단일화한다.
    min_match = re.search(rf"(?P<age>\d{{1,3}})\s*세?\s*(?:을|를|이|가)?\s*(?P<op>이상|부터|초과|넘|보다\s*(?:많|큰|높))", query)
    if min_match:
        _assign_age_bound(target_user, query, min_match, side="min")

    max_match = re.search(rf"(?P<age>\d{{1,3}})\s*세?\s*(?:을|를|이|가)?\s*(?P<op>이하|까지|미만|미달|보다\s*(?:적|작|낮))", query)
    if max_match:
        _assign_age_bound(target_user, query, max_match, side="max")

    # 정확 연령("나이가 30세인 회원"). 경계어(이상/이하/미만/초과/부터/까지)나 범위(~,-)가 없는 딱 'N세'만
    # AGE = N 으로 잡는다 — 위 경계·범위 패스가 이미 뭔가 잡았으면 건너뛴다(그 경우 정확 연령이 아니다).
    if target_user["age_min"] is None and target_user["age_max"] is None and not target_user["age_exclude_ranges"]:
        exact_match = re.search(r"(?P<age>\d{1,3})\s*세(?!\s*(?:이상|이하|미만|초과|부터|까지))(?![~\-\d])", query)
        if exact_match:
            age = _valid_age(exact_match.group("age"))
            if age is not None:
                target_user["age_min"] = age
                target_user["age_max"] = age


# 연령 절 바로 뒤에 붙는 제외/부정 표지만 인식한다(회원/고객 등 목적어 + 조사 + 제외/빼/아닌). '이고/이며'
# 같은 연결어미로 이어진 다른 절의 제외("18세 이상이고 블랙리스트는 제외")까지 삼키지 않도록 앵커(^)를 쓴다.
# '아닌/아니'까지 봐서 "20대가 아닌"·"18세 미만이 아닌" 같은 부정형도 제외로 잡는다.
_AGE_EXCLUSION_TAIL = re.compile(
    r"^(?:인|한|된)?\s*(?:회원|고객|사용자|유저|이용자|분|명)?\s*(?:은|는|을|를|이|가)?\s*(?:모두|전부|다)?\s*(?:제외|제거|빼|제하|아닌|아니)"
)


def _age_range_excluded(query: str, match: re.Match) -> bool:
    """연령 구간 표현(연대/명시 범위) 바로 뒤에 제외·부정 표지가 붙었는지."""
    return bool(_AGE_EXCLUSION_TAIL.match(query[match.end():]))


def _set_age_bound(target_user: dict[str, Any], side: str, bound: int, excluded: bool) -> None:
    """포함 경계(side=min → AGE>=bound, side=max → AGE<=bound)를 넣는다. '제외' 문맥이면 여집합이라
    반대편 열린 경계로 뒤집는다(하한 제외 → 그 미만만, 상한 제외 → 그 초과만). 세·연대 경로 공용."""
    if side == "min":
        if excluded:
            target_user["age_max"] = bound - 1  # 하한 절 제외 → AGE < bound
        else:
            target_user["age_min"] = bound
    else:
        if excluded:
            target_user["age_min"] = bound + 1  # 상한 절 제외 → AGE > bound
        else:
            target_user["age_max"] = bound


def _assign_age_bound(target_user: dict[str, Any], query: str, match: re.Match, side: str) -> None:
    """단일 '세' 경계를 넣는다(예: '18세 미만 제외' = AGE >= 18). 연산자→부등호는 공용 _comparison_operator
    로 단일화하고(부터=>=, 까지=<= 만 age 관례로 보완), 정수 도메인이라 배타(>,<)는 인접 정수로 환산한다."""
    age = _valid_age(match.group("age"))
    if age is None:
        return
    op_text = match.group("op")
    operator = _comparison_operator(op_text) or (">=" if "부터" in op_text else "<=" if "까지" in op_text else None)
    if operator is None:
        return
    excluded = bool(_AGE_EXCLUSION_TAIL.match(query[match.end():]))
    if side == "min":
        bound = age + 1 if operator == ">" else age  # 배타(초과/넘/보다많)만 +1
    else:
        bound = age - 1 if operator == "<" else age  # 배타(미만/보다적)만 -1
    _set_age_bound(target_user, side, bound, excluded)


def _apply_decade_operator(target_user: dict[str, Any], decade: int, op: str, excluded: bool) -> None:
    """'N대 <경계어>'를 열린 경계로 연다. 이상/미만은 연대 시작(N), 이하/초과는 연대 끝(N+9) 기준이다.
    예: '40대 이상'=AGE>=40, '40대 이하'=AGE<=49, '40대 미만'=AGE<=39, '40대 초과'=AGE>=50."""
    start, end = decade, decade + 9
    if op == "이상":      # >= 연대 시작
        side, bound = "min", start
    elif op == "초과":    # > 연대 끝  → >= 끝+1
        side, bound = "min", end + 1
    elif op == "이하":    # <= 연대 끝
        side, bound = "max", end
    else:                 # 미만: < 연대 시작 → <= 시작-1
        side, bound = "max", start - 1
    _set_age_bound(target_user, side, bound, excluded)


def _valid_age(value: str) -> int | None:
    age = int(value)
    return age if 0 <= age <= 120 else None


def _apply_purchase_object_filter(query: str, target_user: dict[str, Any]) -> None:
    # "…을/를 구매한/구입한/구매했던/구입하신 …" 같은 동사형뿐 아니라, "기저귀 구매 고객" 같은 명사형
    # (구매/구입 + 고객/회원/이력 등)도 상품 구매 이력 타겟으로 본다. 타겟팅 프롬프트 재작성(normalize_prompt)
    # 이 "…를 산 고객"을 "… 구매 고객" 명사형으로 정규화하므로, 명사형을 놓치면 조건이 통째로 사라진다.
    # object 클래스에 공백을 넣지 않아 "를/을" 또는 구매/구입 직전 상품 명사만 잡는다. (공백 허용 시 "40대
    # 여성 중 기저귀를 구매한" 처럼 앞 절 조건까지 삼켜 LIKE 가 무의미해지므로) 상품 카테고리 단어면 재현율에 충분하다.
    # 나열형 다중 상품('기저귀와 건강식품을 … 구매')은 개수어가 상품과 구매 동사 사이에 끼어도 목적격 조사
    # 앵커로 먼저 잡는다 — 단일 정규식이 마지막 상품만/개수어를 잡거나 LLM 이 상품을 뭉치는 소실을 막는다.
    multi_objects = _extract_purchase_object_list(query)
    if len(multi_objects) > 1:
        target_user["purchase_objects"] = multi_objects
        target_user["purchase_object"] = multi_objects[0]["value"]
        target_user["purchase_object_kind"] = multi_objects[0]["kind"]
        return
    match = _PURCHASE_OBJECT_QUANTIFIED_PATTERN.search(query) or _PURCHASE_OBJECT_PATTERN.search(query)
    purchase_object = _sanitize_purchase_object(match.group("object")) if match else None
    # 사용자가 '브랜드'/'상품(제품)명'을 명시했으면 매칭 컬럼을 BRAND_NAME/PRODUCT_NAME 으로 좁힐
    # 근거가 된다(아래 kind 마킹). 애매하게 상품어만 말하면 kind 없이 광역 6컬럼 LIKE 를 유지한다.
    is_brand_mention = False
    is_product_mention = False
    if purchase_object in _GENERIC_PRODUCT_NOUNS:
        # 일반명사("상품/브랜드")가 잡혔으면 그 앞의 실제 브랜드/상품명으로 재시도한다
        # ("알로루 브랜드 상품 구매한" → '상품'이 아니라 '알로루'). 재시도가 실패하면 기존 동작 유지.
        retry = _PURCHASE_OBJECT_BRAND_PATTERN.search(query)
        retried = _sanitize_purchase_object(retry.group("object")) if retry else None
        if retried and retried not in _GENERIC_PRODUCT_NOUNS:
            purchase_object = retried
            qualifier = retry.group(0)
            # 인접 수식어가 '브랜드'면 브랜드, '상품/제품'이면 상품명으로 좁힌다.
            # (물건/품목/굿즈/아이템 등 그 외 일반명사는 상품명 신호로 보지 않아 광역 매칭 유지.)
            is_brand_mention = "브랜드" in qualifier
            is_product_mention = not is_brand_mention and ("상품" in qualifier or "제품" in qualifier)
    if not purchase_object:
        # 구매 동사 없이 브랜드만 언급한 계사형("브랜드가 알로루인 곳")도 구매 이력 타겟으로 승격한다.
        brand_match = _BRAND_COPULA_PATTERN.search(query)
        candidate = _sanitize_purchase_object(brand_match.group("object")) if brand_match else None
        if candidate and candidate not in _GENERIC_PRODUCT_NOUNS:
            purchase_object = candidate
            is_brand_mention = True
    if not purchase_object:
        # "상품명이/제품명이 X인" 계사형 상품명 언급도 구매 이력 타겟으로 승격한다.
        product_match = _PRODUCT_NAME_COPULA_PATTERN.search(query)
        candidate = _sanitize_purchase_object(product_match.group("object")) if product_match else None
        if candidate and candidate not in _GENERIC_PRODUCT_NOUNS:
            purchase_object = candidate
            is_product_mention = True
    # 재시도까지 실패해 일반명사('상품/제품')만 남으면 상품 필터로 쓰지 않는다 — LIKE '%상품%' 는
    # 사실상 모든 상품을 뜻해 무의미하고, '2개 이상 상품 구입' 처럼 실제 상품명이 없는 개수 조건을
    # 억지 LIKE 로 만들기 때문이다(개수 조건은 별도 트랙이 담당).
    if purchase_object in _GENERIC_PRODUCT_NOUNS:
        purchase_object = None
    if purchase_object:
        canonical = _canonicalize_product_term(purchase_object)
        target_user["purchase_object"] = canonical
        # '브랜드' 명시 또는 값 자체가 실DB 브랜드명과 일치하면 브랜드로 확정한다.
        # → purchase_history 템플릿이 6컬럼 광역 LIKE 대신 BRAND_NAME 만 매칭(정밀도↑).
        if is_brand_mention or _is_known_brand_term(canonical):
            target_user["purchase_object_kind"] = "brand"
        # '상품명/제품명' 명시면 PRODUCT_NAME 만 매칭한다(브랜드 확정이 우선).
        elif is_product_mention:
            target_user["purchase_object_kind"] = "product"


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
            "purchase_objects: 타겟 오디언스가 '구매/구입한' 상품(구매 이력 조건)의 상품명 배열. 여러 상품이",
            "  나열되면('기저귀와 건강식품') 각 상품을 배열 원소로 분리한다. 하나면 원소 1개, 없으면 빈 배열 [].",
            "  하나의 상품명을 여러 문자열로 쪼개지 말고, 여러 상품을 한 문자열로 합치지도 마라.",
            "sell_object: 이 캠페인이 '팔려는/판매하려는' 상품명(하나, 없으면 null).",
            "반드시 입력 문장에 그대로 등장하는 명사만 사용한다(번역·유추·추가 금지).",
            "조사·수식어(첫/재/최근 등)와 수량어(2개/3번 등)는 빼고 핵심 상품 명사만 남긴다.",
            '다음 JSON object 만 출력한다: {"purchase_objects": ["상품명", ...], "sell_object": "상품명 또는 null"}.',
        ]
    )
    return _read_prompt_template(prompt_dir, "target_object_extract_system.txt", fallback)


def _llm_extract_target_objects(
    query: str, llm_model: str, prompt_dir: Path | None
) -> dict[str, Any] | None:
    """LLM 으로 문장에서 purchase_object/sell_object 후보를 추출한다. 사용 불가/실패 시 None."""
    llm_model = _fast_llm_model(llm_model)  # 상품추출도 빠르고 정확한 모델 고정(12s 타임아웃 방지)
    if not os.getenv("OPENAI_API_KEY") or not query.strip():
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    try:
        client = OpenAI()
        response = _openai_chat_create(client, 
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
        # 신 계약(purchase_objects 배열) 우선, 구 계약(purchase_object 문자열)도 호환한다.
        raw_objects = data.get("purchase_objects")
        if isinstance(raw_objects, list):
            purchase_objects = [o for o in raw_objects if isinstance(o, str) and o.strip()]
        elif isinstance(data.get("purchase_object"), str) and data["purchase_object"].strip():
            purchase_objects = [data["purchase_object"]]
        else:
            purchase_objects = []
        result = {
            "purchase_objects": purchase_objects,
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
    plan["_trace_target_object_llm_used"] = True
    extracted = _llm_extract_target_objects(query, llm_model, prompt_dir)
    if not extracted:
        return
    if need_purchase:
        # LLM 이 준 상품 배열의 각 원소를 원문 존재 검증 → 나열형 분리 → DB 표기 보정한다. 나열형 다중 상품이면
        # purchase_objects 리스트로, 하나면 단일 필드만 채운다(빌더가 상품별로 각각 결합).
        objects: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in extracted.get("purchase_objects") or []:
            validated = _validated_object(raw, query)
            if not validated:
                continue
            for term in (_split_product_terms(validated) or [validated]):
                canonical = _canonicalize_product_term(term)
                if not canonical or canonical in _GENERIC_PRODUCT_NOUNS or canonical in seen:
                    continue
                seen.add(canonical)
                objects.append({"value": canonical, "kind": "brand" if _is_known_brand_term(canonical) else None})
        if objects:
            target_user["purchase_object"] = objects[0]["value"]
            if objects[0]["kind"]:
                target_user["purchase_object_kind"] = objects[0]["kind"]
            if len(objects) > 1:
                target_user["purchase_objects"] = objects
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
    table = index.get("table", _member_table())
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


def _apply_macro_region_filter(query: str, plan: dict[str, Any]) -> None:
    """광역 권역어(수도권 등)를 구성 시도(SIDO IN) 조건으로 확장한다.

    '수도권'은 단일 저장값이 아니라 서울/경기/인천을 묶은 관용어라 member_value_index 에 없어 지역
    조건이 통째로 빠진다. macro_regions 매핑으로 구성 시도명을 SIDO dimension_filter(값 인덱스와 같은
    형태)로 만들어 기존 컴파일러(_member_region_predicates)가 그대로 SIDO IN 으로 컴파일한다.
    _apply_member_value_filters 뒤에 실행해, 사용자가 구체 시도('부산')도 같이 말했으면 기존 SIDO
    필터에 병합(합집합)한다 — 매크로가 명시 시도를 덮어쓰지 않게."""
    config = _MEMBER_TARGET_FILTERS.get("macro_regions")
    if not isinstance(config, dict):
        return
    groups = config.get("groups")
    if not isinstance(groups, dict):
        return
    column = (config.get("column") or "SIDO").upper()
    names: list[str] = []
    for macro, members in groups.items():
        if not isinstance(members, list) or not _value_token_mentioned(macro, query):
            continue
        for name in members:
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    if not names:
        return
    # 이미 같은 시도 컬럼 조건이 있으면(구체 시도 지정) 합집합으로 병합한다.
    for dimension_filter in plan.get("dimension_filters", []):
        if (dimension_filter.get("column") or "").split(".")[-1].upper() == column:
            for key in ("codes", "names"):
                existing = dimension_filter.setdefault(key, [])
                for name in names:
                    if name not in existing:
                        existing.append(name)
            return
    table = _member_table()
    plan.setdefault("dimension_filters", []).append({
        "dimension_id": "macro_region:" + column,
        "prompt_label": column,
        "column": table + "." + column,
        "table": table,
        "operator": "IN",
        "codes": list(names),
        "names": list(names),
        "source": "macro_region",
    })


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


def _region_granularity_match(query: str) -> re.Match[str] | None:
    """Return an explicit region-granularity token, excluding substrings such as ``구매``.

    Most configured tokens are self-describing words (시도, 시군구, 지역).  Single-syllable
    tokens such as ``구`` and ``동`` need a following unit boundary; otherwise ``구매`` or ``동안``
    is misrouted to the regional member-count builder.
    """
    for match in re.finditer(rf"({_region_granularity_alternation()})", query):
        token = match.group(1)
        if len(token) > 1:
            return match
        suffix = query[match.end():]
        if not suffix or re.match(r"(?:\s|별|단위|마다|지역)", suffix):
            return match
    return None


def _region_column_bare(granularity: str) -> str:
    """지역 단위어(지역/시군구/시도/동…)를 실컬럼명(SIGUNGU/SIDO/DONG)으로. 매핑에 없으면 기본 컬럼.

    config 의 granularity_columns 값은 'B.SIGUNGU'처럼 별칭 접두어를 달고 있어, 빌더가 자기 별칭을
    다시 붙일 수 있게 여기서 접두어를 떼어 맨 컬럼명만 돌려준다(그룹/밀집 지역 빌더 공용)."""
    config = _region_density_config()
    cols = config.get("granularity_columns")
    cols = cols if isinstance(cols, dict) else {}
    raw = cols.get(granularity) or config.get("default_column") or "SIGUNGU"
    return str(raw).split(".")[-1]


_REGION_DENSITY_PATTERN = re.compile(
    rf"(?:가장\s*|제일\s*)?많이\s*(?:거주하|사|살고\s*있)는\s*({_region_granularity_alternation()})"
)
_REGION_DENSITY_ALT_PATTERN = re.compile(rf"밀집\s*({_region_granularity_alternation()})")
_REGION_DENSITY_TOP_N_PATTERN = re.compile(r"상위\s*([\d,]+)|(?:top|톱)\s*([\d,]+)", re.IGNORECASE)

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


# 공용 랭킹 지시 문법: '<지표>가 높은 고객'(관용 어순)뿐 아니라 지표어와 떨어진 '기준 상위 N명 / 상위
# N명 / 높은 순 N명 / 낮은 순 N명 / 하위 N명 / TOP N' 도 랭킹으로 인식한다. 방향(고/저)과 개수(N)만
# 뽑고, 지표 결합은 호출부가 한다. '높은 순/낮은 순'은 순위 방향 표현이라 관용 어순 패턴이 못 잡는다.
# 개수는 [\d,]+ 로 받아 천 단위 콤마('1,000명')를 허용한다 — 뒤에서 콤마를 떼고 정수화한다(_parse_count).
_RANKING_HIGH_DIRECTIVE = re.compile(r"상위\s*[\d,]*\s*명?|높은\s*순|top\s*[\d,]+", re.IGNORECASE)
_RANKING_LOW_DIRECTIVE = re.compile(r"하위\s*[\d,]*\s*명?|낮은\s*순")
_RANKING_DIRECTIVE_TOP_N = re.compile(r"(?:상위|하위|top)\s*([\d,]+)", re.IGNORECASE)
# 상위/하위 N% 퍼센트 지시(정수·소수). 방향(상위=high/하위=low)은 접두어로, 없으면 호출부가 지표 어순으로 판단.
_RANKING_PERCENT_PATTERN = re.compile(r"(?P<dir>상위|하위)?\s*(?P<pct>\d+(?:\.\d+)?)\s*(?:%|퍼센트|프로)")


def _parse_count(text: str | None) -> int | None:
    """'1,000' 같은 천 단위 콤마 포함 숫자열을 정수로. 콤마를 떼고 파싱하며, 실패 시 None."""
    if not text:
        return None
    try:
        return int(str(text).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _detect_ranking_directive(query: str) -> dict[str, Any] | None:
    """'상위/하위 N명·N%·높은/낮은 순·TOP N' 순위 지시를 {direction, limit_type, top_n, percent} 로.

    direction: high(상위/높은 순/top) | low(하위/낮은 순). limit_type: 'percent'(N%면) | 'count'.
    percent 우선(‘상위 5%’의 5 를 top_n=5 로 오독하지 않게). top_n: 방향어 뒤 숫자 우선, 없으면 'N명', 둘 다
    없으면 None(호출부가 기본값). 콤마 숫자('1,000명')는 _parse_count 로 정수화. 표지 없으면 None."""
    high = _RANKING_HIGH_DIRECTIVE.search(query)
    low = _RANKING_LOW_DIRECTIVE.search(query)
    if not (high or low):
        return None
    direction = "low" if (low and not high) else "high"
    # 퍼센트 지시가 있으면 개수보다 우선한다('상위 5%'는 상위 5명이 아니다).
    percent_match = _RANKING_PERCENT_PATTERN.search(query)
    if percent_match:
        pct = float(percent_match.group("pct"))
        pct_dir = percent_match.group("dir")
        if pct_dir == "하위":
            direction = "low"
        elif pct_dir == "상위":
            direction = "high"
        return {"direction": direction, "limit_type": "percent", "percent": pct, "top_n": None}
    top_n = None
    directive_n = _RANKING_DIRECTIVE_TOP_N.search(query)
    if directive_n:
        top_n = _parse_count(directive_n.group(1))
    else:
        count = re.search(r"([\d,]+)\s*명", query)
        if count:
            top_n = _parse_count(count.group(1))
    return {"direction": direction, "limit_type": "count", "top_n": top_n, "percent": None}


def _resolve_member_metric_in_query(query: str) -> dict[str, Any] | None:
    """질의 어디에든 나타난 회원 지표(member_metrics) 동의어를 찾아 지표 정의를 돌려준다(긴 동의어 우선).

    '누적 구매 금액 기준 상위 100명'처럼 지표어와 순위 지시가 떨어져 있어도 결합하기 위한 것이다."""
    registry = _load_member_metrics(str(DEFAULT_MEMBER_METRICS_PATH))
    if not registry:
        return None
    pairs: list[tuple[str, dict[str, Any]]] = []
    for metric in registry.get("metrics", []):
        for synonym in metric.get("synonyms", []):
            if isinstance(synonym, str) and synonym:
                pairs.append((synonym, metric))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    for synonym, metric in pairs:
        if synonym in query:
            return metric
    return None


def _apply_member_metric_ranking_target(query: str, plan: dict[str, Any]) -> None:
    """'<지표>가 높은 고객' 및 '<지표> 기준 상위/하위 N명'을 회원 단위 지표 랭킹(member_metric_ranking)으로 해석한다.

    지역 랭킹(_apply_region_density_target)의 회원 단위 짝이다. build_member_metric_ranking_sql_candidate
    가 이 플래그를 보고 지표 테이블(CRM_MB_MONTHCRMINFO)을 회원키로 조인해 지표값 순(방향별 DESC/ASC)
    상위 N 명을 뽑는 SQL 을 생성한다(월 스냅샷 중복은 레지스트리 grain_filter 로 방지). 데모 스키마(users
    테이블) 참조라 실DB 에 못 쓰는 매출 순위/고매출 정책이 같은 어구에 얻어걸려 남으면 파이프라인이
    막히므로, 지표어가 라벨/동의어에 포함된 target_user 정책을 소비한다.

    지표 없는 순수 순위 지시('상위 100명')는 여기서 확정하지 않는다 — 부사형 구매 랭킹(purchase_count_ranking)
    등 다른 트랙에 먼저 양보하고, 끝까지 미해석이면 후단 게이트(_apply_unsupported_intent_gate)가
    '무엇 기준인지' clarification 으로 돌려준다."""
    if isinstance(plan.get("group_ranking_target"), dict):
        # 그룹별 랭킹('지역별로 … 10명씩')으로 이미 해석됐으면 전역 회원 랭킹으로 가로채지 않는다.
        return
    if isinstance(plan.get("region_density_target"), dict):
        # 지역 랭킹으로 이미 해석됐으면(예: '매출 높은 지역') 회원 랭킹으로 중복 해석하지 않는다.
        return
    if _has_metric_scoping_period(query, plan):
        # 기간 스코프('최근 3개월/2025년/지난달 …')는 최신 월 스냅샷 랭킹으로 표현 불가 — 스냅샷 랭킹으로
        # 조용히 보내지 않고(오답 방지) 후단 게이트가 명시 처리한다(기간/스냅샷 라우팅 경계).
        return
    config = _member_metric_ranking_config()
    max_top_n = int(config.get("max_top_n") or 10000)
    default_top_n = int(config.get("default_top_n") or 100)

    # ① 관용 어순('구매금액이 높은 고객 100명') — 방향은 항상 high(내림차순).
    pattern = _member_metric_customer_pattern(str(DEFAULT_MEMBER_METRICS_PATH))
    match = pattern.search(query) if pattern else None
    matched_metric_text: str | None = None
    metric_info: dict[str, Any] | None = None
    direction = "high"
    limit_type = "count"
    top_n = default_top_n
    percent: float | None = None
    if match:
        matched_metric_text = match.group(1)
        metric_info = _member_metric_by_synonym(str(DEFAULT_MEMBER_METRICS_PATH), matched_metric_text)
        # 관용 어순이라도 '상위 N% 회원'처럼 퍼센트가 붙으면 퍼센트 랭킹으로 잡는다(공용 % 문법 재사용).
        percent_match = _RANKING_PERCENT_PATTERN.search(query)
        if percent_match:
            limit_type = "percent"
            percent = float(percent_match.group("pct"))
            if percent_match.group("dir") == "하위":
                direction = "low"
        else:
            top_match = _REGION_DENSITY_TOP_N_PATTERN.search(query) or re.search(r"([\d,]+)\s*명", query)
            if top_match:
                top_n = max(1, min(_parse_count(next(group for group in top_match.groups() if group)) or default_top_n, max_top_n))
    else:
        # ② 공용 순위 지시('<지표> 기준 상위/하위 N명', '높은/낮은 순 N명', 'TOP N').
        directive = _detect_ranking_directive(query)
        if directive is None:
            return
        # 정렬키는 '랭킹 어구에 결합된 지표'만 인정한다 — 질의 아무 데나 있는 지표(_resolve_member_metric_in_query)를
        # 상위 N 에 묶으면 '구매 횟수 10회 이상 … 상위 100명'의 임계 지표를 정렬키로 오결합해(가짜 랭킹) 임계
        # HAVING 이 소실됐다. 결합된 정렬키가 없으면(순수 '상위 N') 확정하지 않고 result_limit/게이트에 양보한다.
        metric_info = _resolve_ranking_sort_metric_info(query)
        if metric_info is None:
            return  # 정렬키 미결합 — 후단 게이트/‌result_limit 이 처리(부사형 구매 랭킹 등에 먼저 양보).
        matched_metric_text = metric_info.get("ko_label")
        direction = directive["direction"]
        if directive.get("limit_type") == "percent":
            limit_type = "percent"
            percent = directive.get("percent")
        else:
            top_n = max(1, min(directive["top_n"] or default_top_n, max_top_n))

    if metric_info is None:
        return
    # 퍼센트 경계: (0, 100) 밖이면 랭킹으로 확정하지 않는다(0%·음수·100% 초과는 무의미).
    if limit_type == "percent" and not (isinstance(percent, (int, float)) and 0 < percent < 100):
        return
    ranking = {
        "metric_id": metric_info["metric_id"],
        "metric_label": metric_info.get("ko_label", metric_info["metric_id"]),
        "top_n": top_n,
        "direction": direction,
        "limit_type": limit_type,
    }
    if limit_type == "percent":
        ranking["percent"] = percent
    plan["member_metric_ranking"] = ranking
    # 같은 지표어에 얻어걸린 데모 스키마(users) 회원 정책을 소비한다(실DB 미지원 → clarification 차단).
    _consume_metric_labeled_target_policies(plan, matched_metric_text)


def _consume_metric_labeled_target_policies(plan: dict[str, Any], matched_metric_text: str | None) -> None:
    """지표어(예: '매출')가 라벨/카노니컬에 포함된 target_user 스코프 정책을 제거한다.

    '<지표> 랭킹'으로 이미 구조화한 어구에 데모 스키마(users) 고매출 정책 등이 얻어걸려 남으면 실DB
    미지원 조건으로 파이프라인이 막히므로(clarification), 랭킹 계열 파서(전역/그룹)가 공용으로 소비한다."""
    if not matched_metric_text:
        return
    plan["policy_constraints"] = [
        policy
        for policy in plan.get("policy_constraints", [])
        if not (
            policy.get("scope") == "target_user"
            and matched_metric_text in str(policy.get("ko_label", "")) + str(policy.get("canonical", ""))
        )
    ]


# ── 그룹별 회원 Top-N('지역별로/성별로/연령대별 <지표> 높은 회원 N명씩') ────────────────────
# 전역 회원 랭킹(member_metric_ranking)이 '전체에서 상위 N 명'이라면, 그룹별 랭킹은 '그룹(지역/성별/
# 연령대)마다 상위 N 명씩'이다. ROW_NUMBER() OVER(PARTITION BY 그룹식 ORDER BY 지표)로 그룹 내 순위를
# 매겨 N 이하만 남긴다. 전역 랭킹이 '매출 높은 회원'을 가로채 그룹을 버리던 문제를, 이 파서를 전역보다
# 먼저 실행하고 전역/지역밀집 파서에 group_ranking_target 가드를 달아 별도 실행 경로로 분리해 해결한다.
#
# 그룹 축은 단일 소스(_group_axis_registry)로 관리한다 — 지역/성별/연령대마다 SQL 그룹식·표시 컬럼·NULL
# 정책·표지어를 데이터로 선언하고, 공통 윈도 빌더(build_group_ranking_sql_candidate)가 그룹식만 주입받는다
# (축별 SQL 생성기 복제 없음). 새 축은 config(group_ranking_axes) 또는 지역(region_density) 한 곳만 고치면 열린다.


@dataclass(frozen=True)
class _GroupAxisSpec:
    """그룹별 랭킹의 그룹 축 하나(단일 소스). 공통 윈도 빌더가 이 스펙의 group_expr/select_alias/null 만 쓴다."""

    axis: str                       # canonical 축 이름: region | gender | age_group
    group_expr: str                 # PARTITION BY / GROUP 표현식(별칭 B), 예: 'B.SIGUNGU', 'B.GENDER_CD', CASE 식
    select_alias: str               # 결과 그룹 표시 컬럼 별칭: target_region | gender | age_group
    coverage_token: str             # 커버리지 검증에 반드시 SQL 에 있어야 하는 토큰(SIGUNGU/GENDER_CD/AGE)
    null_predicates: tuple[str, ...] # NULL/미분류 회원 제외 술어(정책)
    label: str                      # 사람이 읽는 축 이름(성별/연령대/시군구 …)
    granularity: str | None = None  # region 축의 세부 단위(지역/시군구/시도 …)


def _group_ranking_axes_config() -> dict[str, Any]:
    config = _MEMBER_TARGET_FILTERS.get("group_ranking_axes")
    return config if isinstance(config, dict) else {}


def _age_band_case_expr(band_config: dict[str, Any] | None) -> str:
    """연령대 CASE 식을 config(bands)에서 중앙 생성한다 — PARTITION BY 와 SELECT 가 동일 식을 쓴다.

    bands = [[상한(미만), 라벨], …] + else_label. 예: AGE<20→'10대 이하', <30→'20대' … ELSE '60대 이상'.
    구간·명칭은 코드 하드코딩이 아니라 member_target_filters.json(group_ranking_axes.age_group.age_band)이 소유한다."""
    band_config = band_config if isinstance(band_config, dict) else {}
    column = band_config.get("column") or "B.AGE"
    bands = band_config.get("bands") if isinstance(band_config.get("bands"), list) else []
    else_label = band_config.get("else_label") or "기타"
    whens = []
    for entry in bands:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            upper, label = entry
            whens.append(f"WHEN {column} < {int(upper)} THEN {_sql_quote(str(label))}")
    if not whens:  # 코드 폴백(config 파손 시): 표준 10년 밴드.
        whens = [
            f"WHEN {column} < 20 THEN {_sql_quote('10대 이하')}",
            f"WHEN {column} < 30 THEN {_sql_quote('20대')}",
            f"WHEN {column} < 40 THEN {_sql_quote('30대')}",
            f"WHEN {column} < 50 THEN {_sql_quote('40대')}",
            f"WHEN {column} < 60 THEN {_sql_quote('50대')}",
        ]
        else_label = "60대 이상"
    return "CASE " + " ".join(whens) + f" ELSE {_sql_quote(str(else_label))} END"


def _resolve_group_axis(axis: str, granularity: str | None = None) -> _GroupAxisSpec | None:
    """축 이름(+region 세부 단위)을 그룹 SQL 식·표시 컬럼·NULL 정책이 담긴 스펙으로 해석한다(중앙 resolver)."""
    if axis == "region":
        column = _region_column_bare(granularity or "지역")
        return _GroupAxisSpec(
            axis="region", group_expr=f"B.{column}", select_alias="target_region",
            coverage_token=column, null_predicates=(f"B.{column} IS NOT NULL", f"B.{column} <> ''"),
            label=granularity or "지역", granularity=granularity,
        )
    axes = _group_ranking_axes_config()
    spec = axes.get(axis) if isinstance(axes.get(axis), dict) else None
    if axis == "gender":
        column = (spec or {}).get("group_expr") or "B.GENDER_CD"
        include_null = bool((spec or {}).get("include_null", False))
        return _GroupAxisSpec(
            axis="gender", group_expr=column, select_alias=(spec or {}).get("select_alias") or "gender",
            coverage_token=(spec or {}).get("coverage_token") or "GENDER_CD",
            null_predicates=() if include_null else (f"{column} IS NOT NULL",),
            label=(spec or {}).get("label") or "성별",
        )
    if axis == "age_group":
        band_expr = _age_band_case_expr((spec or {}).get("age_band"))
        age_column = ((spec or {}).get("age_band") or {}).get("column") or "B.AGE"
        include_null = bool((spec or {}).get("include_null", False))
        return _GroupAxisSpec(
            axis="age_group", group_expr=band_expr, select_alias=(spec or {}).get("select_alias") or "age_group",
            coverage_token=(spec or {}).get("coverage_token") or "AGE",
            null_predicates=() if include_null else (f"{age_column} IS NOT NULL",),
            label=(spec or {}).get("label") or "연령대",
        )
    return None


def _group_axis_markers() -> list[tuple[str, str, str | None]]:
    """그룹 축 표지 사전(단일 소스): (표지어, 축, region이면 granularity). 긴 표지 우선(오탐 방지).

    지역 표지는 region_density granularity_tokens + 그룹 조사(별/별로/마다), 성별/연령대는 config 표지어.
    '독립된 그룹 축 표현'만 사전에 등록해 '행동별로'·'특별로'·'개별로'·'상품별로' 같은 일반 단어의 부분
    문자열 오탐을 원천 차단한다(부분 문자열이 아니라 사전 등록 표지 + 경계 판정)."""
    markers: list[tuple[str, str, str | None]] = []
    # 지역 축: 각 지역 단위어 + 그룹 조사.
    region_tokens = [t for t in _region_density_config().get("granularity_tokens", []) if isinstance(t, str) and t]
    for token in region_tokens:
        for particle in ("별로", "별", "마다"):
            markers.append((f"{token}{particle}", "region", token))
        markers.append((f"각 {token}마다", "region", token))
        markers.append((f"{token} 마다", "region", token))
    # 성별/연령대 축: config 표지어.
    for axis, spec in _group_ranking_axes_config().items():
        if not isinstance(spec, dict):
            continue
        for marker in spec.get("markers", []):
            if isinstance(marker, str) and marker:
                markers.append((marker, axis, None))
    # 긴 표지부터(‘연령대별’이 ‘연령별’보다, ‘시군구별’이 ‘구별’보다 먼저).
    markers.sort(key=lambda m: len(m[0]), reverse=True)
    return markers


def _detect_group_axis(query: str) -> tuple[str, str | None] | None:
    """질의에서 '독립된 그룹 축 표지'를 찾아 (axis, granularity) 로. 없으면 None.

    사전 등록 표지의 부분 문자열 오탐을 막기 위해 표지 앞에 한글/영숫자가 붙어 다른 단어를 이루면
    (예: '행동별로'의 '동별로', '특별로'의 '별로', '상품별로'의 '품별로') 그룹 표지로 인정하지 않는다."""
    for marker, axis, granularity in _group_axis_markers():
        start = 0
        while True:
            idx = query.find(marker, start)
            if idx < 0:
                break
            prev = query[idx - 1] if idx > 0 else ""
            # 표지 바로 앞이 한글/영숫자면 더 긴 단어의 일부 → 독립 표지가 아님(경계 판정).
            if prev and (prev.isalnum() or "가" <= prev <= "힣"):
                start = idx + 1
                continue
            return axis, granularity
    return None


_PER_GROUP_COUNT_RE = re.compile(r"(?:상위\s*)?([\d,]+)\s*(?:명|개|곳)?\s*씩")
# 그룹당 회원 수: 'N명씩'(가장 명시) | '상위/하위 N명' | 'N명'. 회원 단위(명)만 — 개/곳(지역 단위)은 제외.
_GROUP_PER_COUNT_RE = re.compile(r"([\d,]+)\s*명\s*씩|(?:상위|하위)\s*([\d,]+)\s*명|([\d,]+)\s*명")
_GROUP_HIGH_DIR_RE = re.compile(r"높은|많은|큰|상위")
_GROUP_LOW_DIR_RE = re.compile(r"낮은|적은|작은|하위")
_PER_GROUP_SUFFIX_RE = re.compile(r"([\d,]+)\s*(?:명|개|곳)?\s*씩|명씩")
# 미지원 그룹 축(지역/성별/연령대 외): 등급/채널/브랜드/카테고리별. 지원 축(지역/성별/연령대)은
# 실제 그룹 SQL 로 컴파일되므로 여기서 제외한다 — 미구현 축만 조용한 전역 붕괴 대신 명시 미지원으로 돌린다.
_UNSUPPORTED_GROUP_AXIS_RE = re.compile(r"등급\s*별|회원등급\s*별|채널\s*별|브랜드\s*별|카테고리\s*별")

# 지표를 특정 기간으로 스코프하는 표현(최근 N일/개월/년, 지난달/이번달, 2025년, 지난주 등). 회원 지표
# 랭킹은 CRM_MB_MONTHCRMINFO 최신 월 스냅샷(전 기간 누적) 기준이라 임의 기간을 표현하지 못한다 —
# 기간 스코프가 붙으면 스냅샷 랭킹으로 조용히 보내지 않고(오답 방지) 게이트가 명시 처리한다.
_METRIC_SCOPING_PERIOD_RE = re.compile(
    r"최근\s*\d+\s*(?:일|주|주간|개월|달|년|년간|개월간|분기)"
    r"|지난\s*(?:달|주|해|분기|주간)|지난달|저번\s*달|저번달|전월|당월|이번\s*달|이번달"
    r"|올해|금년|작년|지난해|재작년"
    r"|\d{4}\s*년(?!령)"
)


def _has_metric_scoping_period(query: str, plan: dict[str, Any]) -> bool:
    """지표를 특정 기간으로 스코프하는 표현이 있는지. 정규식 표지 또는 이미 파싱된 구매 날짜창(purchase_date).

    최신 월 스냅샷 랭킹(member_metric_ranking/그룹 랭킹)이 기간 스코프 질의를 조용히 삼키지 못하게 하는
    라우팅 경계다. 기간 없는 '구매 횟수 많은 회원 100명'은 여기 걸리지 않아 스냅샷 랭킹 정책을 유지한다."""
    if _METRIC_SCOPING_PERIOD_RE.search(query):
        return True
    return isinstance(plan.get("target_user", {}).get("purchase_date"), dict)


def _apply_group_ranking_target(query: str, plan: dict[str, Any]) -> None:
    """'<축>별(로) <지표> 높은 회원 N명씩'을 그룹별 회원 Top-N 타겟(group_ranking_target)으로 해석한다.

    축(지역/성별/연령대)은 _detect_group_axis(사전 기반)로 판정하고 _resolve_group_axis 로 그룹 SQL 식을
    얻는다. build_group_ranking_sql_candidate 가 PARTITION BY(그룹식) ORDER BY(지표) 윈도로 그룹 내 순위를
    매겨 상위 N 명씩 뽑는다. 그룹 표지 + 'N명씩'(그룹당 개수) + 회원 지표가 모두 있어야 확정한다 —
    하나라도 없으면 기존 전역 랭킹/집계 경로에 양보한다(오탐 방지). 기간 표현이 있으면(최신 월 스냅샷으로
    표현 불가) 확정하지 않고 후단 게이트에 넘긴다(전역 랭킹과 동일한 기간/스냅샷 경계)."""
    if isinstance(plan.get("group_ranking_target"), dict):
        return
    axis_match = _detect_group_axis(query)
    # 그룹당 인원(회원 단위): 'N명씩'(가장 명시) | '상위/하위 N명' | 'N명'. 축 표지가 이미 그룹 의도를
    # 확정하므로 '씩'이 없어도('연령대별 … 상위 5명') 그룹당 N 으로 본다. 개/곳(지역 단위)은 제외.
    count_match = _GROUP_PER_COUNT_RE.search(query)
    if axis_match is None or count_match is None:
        return
    if _has_metric_scoping_period(query, plan):
        return  # 기간 스코프 랭킹은 최신 월 스냅샷 윈도로 표현 불가 — 후단 게이트가 명시 처리.
    metric_info = _resolve_member_metric_in_query(query)
    if metric_info is None:
        return  # 기준 지표 미해석 — 그룹 랭킹으로 확정하지 않는다.
    axis, granularity = axis_match
    axis_spec = _resolve_group_axis(axis, granularity)
    if axis_spec is None:
        return
    config = _member_metric_ranking_config()
    max_top_n = int(config.get("max_top_n") or 10000)
    top_n = max(1, min(_parse_count(next((g for g in count_match.groups() if g), None)) or 10, max_top_n))
    direction = "low" if (_GROUP_LOW_DIR_RE.search(query) and not _GROUP_HIGH_DIR_RE.search(query)) else "high"
    plan["group_ranking_target"] = {
        "target_entity": "member",
        "group_axis": axis,
        "granularity": granularity,
        "group_column": axis_spec.coverage_token,  # 커버리지/표시용 축 식별 토큰
        "metric_id": metric_info["metric_id"],
        "metric_label": metric_info.get("ko_label", metric_info["metric_id"]),
        "limit_type": "count",
        "top_n": top_n,
        "per_group": True,
        "direction": direction,
    }
    # 'N명씩'은 그룹당 개수(윈도)라 전역 행수 제한이 아니다 — result_limit 로 잡혔으면 제거(전역 TOP 오적용 방지).
    plan.pop("result_limit", None)
    # 교정 가드(주로 auto/LLM 경로): LLM 이 같은 질의를 전역 랭킹/지역밀집으로 잘못 채웠으면 그룹 랭킹으로
    # 확정하며 그 슬롯을 제거한다 — 두 슬롯이 공존하면 커버리지가 서로를 요구해 후보가 조용히 탈락한다.
    plan.pop("member_metric_ranking", None)
    plan.pop("region_density_target", None)
    _consume_metric_labeled_target_policies(plan, metric_info.get("ko_label"))


# ── 지역 단위 회원 수 랭킹('회원 수가 많은 시군구 상위 N개') ──────────────────────────────
# 밀집 지역(region_density: 코호트가 많이 '거주'하는 지역의 회원을 추출)과 달리, 이건 지역 자체의 회원
# 수를 집계해 '지역명 + 회원수'를 반환하는 지역-단위 랭킹이다(출력 행 = 지역, not 회원). 회원 수는
# COUNT(DISTINCT 회원키)로 안전 집계한다.
_MEMBER_COUNT_SIGNAL_RE = re.compile(
    r"(?:회원|고객|가입자)\s*수|(?:회원|고객|가입자)\s*(?:이|가|은|는)\s*(?:가장\s*|제일\s*)?(?:많|적)"
)
_MEMBER_COUNT_HIGH_RE = re.compile(r"많은|높은|상위|가장\s*많|제일\s*많|많은\s*순")
_MEMBER_COUNT_LOW_RE = re.compile(r"적은|낮은|하위|가장\s*적|제일\s*적|적은\s*순")
_REGION_COUNT_TOP_N_RE = re.compile(r"([\d,]+)\s*(?:개|곳|군데)")


def _apply_region_member_count_target(query: str, plan: dict[str, Any]) -> None:
    """'회원 수가 많은 시군구 상위 N개' 등을 지역 단위 회원 수 랭킹(region_member_count_target)으로 해석한다.

    build_region_member_count_sql_candidate 가 지역 컬럼으로 GROUP BY 해 COUNT(DISTINCT 회원키)로 회원
    수를 집계하고, 순위 요청이 있으면 정렬·상위 N 을 적용한다(지역명+회원수 반환). 밀집 지역(거주 회원
    추출)과는 다른 출력 형태라 별도 슬롯/빌더로 소유한다 — '많이 거주하는' 밀집 표현은 양보한다."""
    if isinstance(plan.get("group_ranking_target"), dict) or isinstance(plan.get("region_member_count_target"), dict):
        return
    # '많이 거주하는 동네/밀집 지역'은 밀집 지역(거주 회원 추출) 트랙 소유 — 여기서 가로채지 않는다.
    if _REGION_DENSITY_PATTERN.search(query) or _REGION_DENSITY_ALT_PATTERN.search(query):
        return
    if not _MEMBER_COUNT_SIGNAL_RE.search(query):
        return
    granularity_match = _region_granularity_match(query)
    if not granularity_match:
        return
    granularity = granularity_match.group(1)
    column = _region_column_bare(granularity)
    low = bool(_MEMBER_COUNT_LOW_RE.search(query))
    high = bool(_MEMBER_COUNT_HIGH_RE.search(query))
    direction = "low" if (low and not high) else "high"
    config = _region_density_config()
    max_top_n = int(config.get("max_top_n") or 30)
    top_n: int | None = None
    top_match = _REGION_DENSITY_TOP_N_PATTERN.search(query) or _REGION_COUNT_TOP_N_RE.search(query)
    if top_match:
        top_n = max(1, min(_parse_count(next((g for g in top_match.groups() if g), None)) or 1, max_top_n))
    plan["region_member_count_target"] = {
        "column": column,
        "granularity": granularity,
        "direction": direction,
        "top_n": top_n,
    }
    # '지역/동네' 언급으로 잡힌 지역 모호성 정책(region_context_default)은 여기서 구체 해석됐으므로 소비.
    plan["semantic_resolutions"] = [
        resolution
        for resolution in plan.get("semantic_resolutions", [])
        if resolution.get("policy_id") != "region_context_default"
    ]


# "많이/자주 구입한 사람" 처럼 수량·빈도 부사가 구매 동사 앞에 오는 '구매 많은 순 상위 N' 랭킹 신호.
# member_metric_ranking('구매횟수가 많은 고객' — 지표 명사 랭킹)의 부사형 짝이다. 지표 명사 랭킹은 월
# 스냅샷(CRM_MB_MONTHCRMINFO) 전 기간 누적 기준이라 '2019년 2월에 많이 산 사람' 같은 절대 기간 랭킹을
# 표현 못 하므로, 이쪽은 실주문 집계를 기간 창으로 정렬해 상위 N 명을 뽑는다. '산(?!책)' 으로 산책 등
# 오탐을 막고, 사용/사은 등과 겹치는 맨 '사'는 제외해 구매 의미만 잡는다.
_PURCHASE_QUANTITY_RANK_PATTERN = re.compile(
    r"(?P<sup>가장\s*|제일\s*)?(?:많이|자주|최다)\s*(?:구매|구입|주문|샀|산(?!책))"
)
# 랭킹 대상이 '사람/회원'임을 확인한다(밀집 '지역' 랭킹과 구분 — 지역이면 region_density 가 이미 소비).
_PURCHASE_RANK_TARGET_PATTERN = re.compile(r"고객님|고객|회원|유저|사람|구매자|소비자")


def _apply_purchase_count_ranking_target(query: str, plan: dict[str, Any]) -> None:
    """'(기간 내) 많이/자주 구입한 사람 상위 N 명'을 구매 건수 랭킹 타겟(purchase_count_ranking)으로 해석한다.

    build_purchase_count_ranking_sql_candidate 가 주문 상세(CRM_SL_ORDERDETAILMALL)를 회원별로 집계해
    구매 건수 내림차순 상위 N 명을 뽑는다. 구매 날짜 창(purchase_date)이 함께 잡혀 있으면 그 기간 주문만
    센다. 정밀도 가드: 명시적 개수(N명/상위 N)나 최상급(가장/제일 많이)이 있을 때만 랭킹으로 확정하고,
    그 외 모호한 '많이 구매한 고객'은 일반 세그먼트 경로에 맡긴다."""
    # 지역 랭킹('많이 거주하는 동네')·지표 명사 랭킹('구매횟수가 많은 고객')·그룹별 랭킹으로 이미 해석됐으면 중복 해석 안 함.
    if (
        isinstance(plan.get("region_density_target"), dict)
        or isinstance(plan.get("member_metric_ranking"), dict)
        or isinstance(plan.get("group_ranking_target"), dict)
    ):
        return
    rank_match = _PURCHASE_QUANTITY_RANK_PATTERN.search(query)
    if not rank_match:
        return
    # 부정 맥락('많이 구매하지 않은/못 한')은 랭킹이 아니다.
    tail = query[rank_match.end(): rank_match.end() + 8]
    if any(neg in tail for neg in ("안", "않", "못")):
        return
    config = _member_metric_ranking_config()
    top_match = _REGION_DENSITY_TOP_N_PATTERN.search(query) or re.search(r"(\d+)\s*명", query)
    top_n: int | None = None
    if top_match:
        max_top_n = int(config.get("max_top_n") or 10000)
        top_n = max(1, min(int(next(group for group in top_match.groups() if group)), max_top_n))
    superlative = bool(rank_match.group("sup"))
    # 개수도 최상급도 없으면(모호) 랭킹 확정 안 함.
    if top_n is None and not superlative:
        return
    # 대상이 '사람'임이 드러나야 한다 — 명시적 개수('N명', 곧 N 명의 사람) 또는 사람 토큰(고객/회원/사람 등).
    # (밀집 '지역' 랭킹은 위 region_density 가드가 이미 걸러 여기 오지 않는다.)
    if not (top_match or _PURCHASE_RANK_TARGET_PATTERN.search(query)):
        return
    if top_n is None:
        top_n = int(config.get("default_top_n") or 100)
    plan["purchase_count_ranking"] = {"top_n": top_n}


# "최근 N일/개월 동안 구매하지 않은" 같은 구매 미발생 기간(구매 리센시) 신호. 구매 부정어 + 시간 창이
# 함께 있을 때만 잡는다. 시간 창이 없으면(예: '미구매 고객') '전혀 구매 안 함(no_purchase)'과 구분이
# 없으므로 여기서 잡지 않고 기존 no_purchase 경로로 둔다.
# 구매/구입/주문 + (이력/내역)? + (조사)* + 부정어. 조사('구매가 없는'의 '가')·명사('구매 이력이 없는')가
# 사이에 껴도 잡는다 — '구매없'만 리터럴로 보면 '구매가없'을 놓쳐 '최근 90일 구매가 없는'이 통째로 샜다.
# 부정어는 '안내'(안 아님) 오탐을 피해 없/않/하지않/안함·안한·안했·안하 로 한정. 접두형 '미구매'도 본다.
_PURCHASE_NEG_RE = re.compile(
    r"(?:구매|구입|주문)(?:이력|내역)?(?:을|를|은|는|이|가|도)*(?:없|않|하지않|안함|안한|안했|안하)"
    r"|미(?:구매|구입|주문)"
)


def _parse_purchase_inactivity_period(query: str) -> dict[str, Any] | None:
    compact_query = query.replace(" ", "").casefold()
    if not _PURCHASE_NEG_RE.search(compact_query):
        return None
    # 통합 창 파서 — 년/주/단어형(반년 등)까지 커버. sql_interval 은 이 슬롯 계약에 없다. 구매 키워드
    # 근처의 창만 본다(다른 조건의 창을 훔쳐가지 않게).
    return _parse_duration_window(query, anchor_terms=("구매", "구입", "주문"))


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
# ── 비교 연산자 어휘의 단일 소스 ────────────────────────────────────────────────────
# 이상/초과/이하/미만 → 부등호. 정규식 열거(_OP_ALT_BASIC)·매핑(_AGG_OPERATOR_WORDS)·rich 문법
# (_COMPARISON_OP_ALT)이 전부 여기서 파생한다 — 새 비교어는 여기 한 곳에만 추가하면 모든 도메인이 얻는다.
# (도메인 정규식들이 예전엔 '이상|초과|이하|미만'을 각자 인라인으로 재하드코딩했다.)
# 비교어→부등호 매핑은 targeting_ir(순수 모듈)이 단일 소스로 소유한다 — IR 정규화와 표면 파싱이 같은 표를
# 공유해, 새 비교어 추가 시 두 곳을 따로 고치지 않는다. 순서(이상/초과/이하/미만)는 아래 _OP_ALT_BASIC 열거가
# 의존하므로 targeting_ir 쪽 리터럴 순서로 보존된다.
_COMPARISON_OPERATORS = targeting_ir.COMPARISON_WORD_OPERATORS
_OP_ALT_BASIC = "|".join(_COMPARISON_OPERATORS)  # "이상|초과|이하|미만"
_AGG_OPERATOR_WORDS = _COMPARISON_OPERATORS  # 별칭(op→부등호 매핑; 기존 참조 다수가 이 이름을 쓴다)
# 집계 지표 임계값의 측정 단위 — 공용 비교 문법(_parse_amount_comparison)에 넘긴다. 상품 수량/종류
# 단위(개·수량·점·종·종류·가지·품목)도 포함해 '10종 이상'·'상품 5개'가 임계값으로 파싱되게 한다.
_AGG_UNIT = r"원|건수|회수|종류|종수|품목|가지|건|회|명|개|장|번|종|점|수량"
# ── 공용 비교 문법(도메인 공통) ─────────────────────────────────────────────────────
# age/balance/aggregate/count 마다 재구현하던 '이상/이하/초과/미만/넘는/보다 많은/정확히/범위'를 단위(unit)만
# 바꿔 한 곳에서 파싱한다. 새 표현형은 여기 한 번만 추가하면 모든 도메인이 함께 얻는다(도메인별 함수 추가 불필요).
# rich 형(부사·'보다 많은/적은')도 기본 4어(_OP_ALT_BASIC)를 단일 소스에서 포함한다.
_COMPARISON_OP_ALT = rf"{_OP_ALT_BASIC}|넘|미달|보다\s*(?:많|큰|높|적|작|낮|{_OP_ALT_BASIC})"


def _threshold_measure(num: str, unit: str, *, mag: bool = False, sep: str = r"\s*", unit_optional: bool = False) -> str:
    """<숫자>[<배수어>]<단위> 정규식 조각(연산자 앞까지). num 은 (?P<num>) 로, 배수어는 (?P<mag>) 로 캡처.
    배수어가 단위와 융합되는 특수형(캠페인 구매금액 '10만'·'10만원'·'10원')은 number 타입이 measure 를 직접 소유한다."""
    mag_part = rf"{sep}(?P<mag>억|천만|백만|만|천)?" if mag else ""
    u = rf"(?:{unit})?" if unit_optional else rf"(?:{unit})"
    return rf"(?P<num>{num}){mag_part}{sep}{u}"


def _threshold_regex(num: str, unit: str, *, mag: bool = False, sep: str = r"\s*", prefix: str = "", unit_optional: bool = False) -> str:
    """<숫자>[<배수어>]<단위><연산자> 정규식 '문자열'. 연산자 열거는 단일 소스(_OP_ALT_BASIC)에서 온다.
    문자열이라 다른 정규식에 임베드할 수 있다(예: 셀 비율 = 지표어 + 이 조각)."""
    measure = _threshold_measure(num, unit, mag=mag, sep=sep, unit_optional=unit_optional)
    return rf"{prefix}{measure}{sep}(?P<op>{_OP_ALT_BASIC})"


def _threshold_pattern(num: str, unit: str, *, mag: bool = False, sep: str = r"\s*", prefix: str = "", unit_optional: bool = False) -> "re.Pattern[str]":
    """_threshold_regex 컴파일본(단독 search 용). 스펙 기반 도메인 생성은 _compile_threshold 를 쓴다."""
    return re.compile(_threshold_regex(num, unit, mag=mag, sep=sep, prefix=prefix, unit_optional=unit_optional))


# 숫자 고유어 수사(한~열) → 값. 순수 카운트('세 번 이상')용 — 금액/배수어와 구분한다.
_NATIVE_COUNT_WORDS = {
    "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5,
    "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}


def _percent_value(match: "re.Match[str]") -> float | None:
    value = float(match.group("num"))
    return value if 0 < value <= 100 else None


def _native_count_value(match: "re.Match[str]") -> int | None:
    text = match.group("num")
    count = _NATIVE_COUNT_WORDS.get(text)
    if count is None:
        try:
            count = int(text)
        except ValueError:
            return None
    return count if count > 0 else None


def _korean_amount_value(match: "re.Match[str]") -> float | None:
    return _parse_korean_amount(match.group("num"), match.group("mag") or "")


# 숫자 해석 '타입' — 정규식 조각(또는 measure 통짜)·배수어 여부·기본 값 추출기를 함께 선언한다(표면 파싱 +
# 값 해석 결합). 새 타입은 여기 한 줄. 값 검증 실패(범위 밖 %·0 이하 등)면 None 을 돌려 도메인이 폴백하게 한다.
# 대부분 pattern(숫자 조각) + 도메인 unit 으로 measure 를 조립하지만, 배수어가 단위와 융합되는 특수형은
# measure(num+mag+unit 통짜)를 타입이 직접 소유한다(unit/mag/sep 조립 규칙 밖).
_THRESHOLD_NUMBER_KINDS: dict[str, dict[str, Any]] = {
    "integer": {"pattern": r"\d+", "mag": False, "value": lambda m: int(m.group("num"))},
    "korean_amount": {"pattern": r"[\d,]+(?:\.\d+)?", "mag": True, "value": _korean_amount_value},
    "percent": {"pattern": r"\d+(?:\.\d+)?", "mag": False, "value": _percent_value},
    "native_count": {"pattern": r"\d+|" + "|".join(_NATIVE_COUNT_WORDS), "mag": False, "value": _native_count_value},
    # 캠페인 귀속 구매금액: '10만'(원 없이)·'10만원'·'10원' — 배수어가 단위(원) 안에 융합되고 원이 optional.
    "korean_amount_bare": {"measure": r"(?P<num>[\d,]+(?:\.\d+)?)(?:(?P<mag>억|천만|백만|만|천)원?|원)", "value": _korean_amount_value},
}


@dataclass(frozen=True)
class _ThresholdSpec:
    """타입 있는 임계 조건 선언 — 도메인별 regex + 값 파서를 함께 생성하는 입력. number 로 숫자 해석
    타입(정규식 조각 + 기본 파서)을 고르고, unit/prefix/sep/unit_optional 로 표면형을 맞춘다. 값 해석이
    특수한 경우에만 parse 커스텀 훅(match -> (operator, value)|None)을 준다(없으면 number 기본 파서)."""

    number: str  # _THRESHOLD_NUMBER_KINDS 키
    unit: str
    sep: str = r"\s*"
    prefix: str = ""
    unit_optional: bool = False
    parse: "Callable[[re.Match[str]], tuple[str, float] | None] | None" = None


@dataclass(frozen=True)
class _ThresholdMatcher:
    """_ThresholdSpec 로 생성된 도메인 전용 정규식 + 값 파서. regex(임베드용 문자열)·pattern(단독 search)·
    parse(match -> (operator, value)|None; op 부등호 정규화 + 타입별 값 추출·검증)."""

    regex: str
    pattern: "re.Pattern[str]"
    parse: "Callable[[re.Match[str]], tuple[str, float] | None]"


def _compile_threshold(spec: _ThresholdSpec) -> _ThresholdMatcher:
    """타입 스펙 → 도메인 전용 regex + 파서(거대 범용 정규식 하나가 아니라 스펙별 전용본)."""
    kind = _THRESHOLD_NUMBER_KINDS[spec.number]
    # 대부분 pattern+unit 으로 measure 를 조립하지만, 타입이 measure 를 직접 소유하면(융합 단위) 그걸 쓴다.
    measure = kind.get("measure") or _threshold_measure(
        kind["pattern"], spec.unit, mag=kind.get("mag", False), sep=spec.sep, unit_optional=spec.unit_optional
    )
    regex = rf"{spec.prefix}{measure}{spec.sep}(?P<op>{_OP_ALT_BASIC})"

    def default_parse(match: "re.Match[str]") -> "tuple[str, float] | None":
        value = kind["value"](match)
        if value is None:
            return None
        operator = _comparison_operator(match.group("op"))
        return (operator, value) if operator else None

    return _ThresholdMatcher(regex=regex, pattern=re.compile(regex), parse=spec.parse or default_parse)


def _comparison_operator(op_text: str) -> str | None:
    """비교 어구(부사형·동사형·'보다 X')를 부등호로 정규화한다."""
    t = op_text.replace(" ", "")
    if t.startswith("이상") or t == "보다이상":
        return ">="
    if t.startswith("이하") or t == "보다이하":
        return "<="
    if t.startswith("초과") or t.startswith("넘") or t.startswith("보다많") or t.startswith("보다큰") or t.startswith("보다높") or t.startswith("보다초과"):
        return ">"
    if t.startswith("미만") or t.startswith("미달") or t.startswith("보다적") or t.startswith("보다작") or t.startswith("보다낮") or t.startswith("보다미만"):
        return "<"
    return None


@functools.lru_cache(maxsize=32)
def _comparison_patterns(unit: str, unit_required: bool = False) -> tuple["re.Pattern[str]", "re.Pattern[str]", "re.Pattern[str]"]:
    # unit_required=True 면 단위를 필수로 요구한다 — 지표 명사(잔액 등)가 숫자 앞에 오는 도메인은 단위가
    # 선택이라 '30에서 49'(단위 없는 나이 범위)까지 잡지만, 장바구니 개수처럼 단위(개/종…)가 신호 그 자체인
    # 도메인은 단위 없는 숫자·범위를 흡수하면 안 된다(카트 질의에 섞인 '30~49세'·'6개월'을 배제).
    num, mag = r"[\d,]+(?:\.\d+)?", r"억|천만|백만|만|천"
    u = rf"(?:{unit})" if unit_required else rf"(?:{unit})?"
    range_p = re.compile(rf"(?P<lo>{num})\s*(?P<lomag>{mag})?\s*{u}\s*(?:에서|부터|~|-)\s*(?P<hi>{num})\s*(?P<himag>{mag})?\s*{u}\s*(?:사이|까지)?")
    op_p = re.compile(rf"(?P<num>{num})\s*(?P<mag>{mag})?\s*{u}\s*(?:을|를|이|가)?\s*(?P<op>{_COMPARISON_OP_ALT})")
    eq_p = re.compile(rf"(?P<num>{num})\s*(?P<mag>{mag})?\s*(?:{unit})")
    return range_p, op_p, eq_p


def _parse_amount_comparison(window: str, unit: str, *, bare_equals: bool = False, unit_required: bool = False) -> list[tuple[str, float]] | None:
    """단위(unit) 뒤 비교 어구를 [(operator, value), ...] 로 정규화한다(범위=두 술어 >=lo,<=hi). 부등호
    (부사형·동사형·'보다 많은/적은')·정확값('정확히 N')·범위를 공통 처리한다. bare_equals=True 면 연산자 없는
    맨 'N<unit>'을 등호로 본다(잔액처럼 맥락상 정확값이 자연스러운 도메인용; 횟수처럼 모호하면 False).
    unit_required=True 면 단위를 필수로 요구해 단위 없는 숫자·범위를 흡수하지 않는다(장바구니 개수 등)."""
    range_p, op_p, eq_p = _comparison_patterns(unit, unit_required)
    rng = range_p.search(window)
    if rng is not None:
        lo = _parse_korean_amount(rng.group("lo"), rng.group("lomag") or "")
        hi = _parse_korean_amount(rng.group("hi"), rng.group("himag") or "")
        return [(">=", lo), ("<=", hi)] if lo is not None and hi is not None and lo <= hi else None
    parsed_ops: list[tuple[str, float]] = []
    for op in op_p.finditer(window):
        operator = _comparison_operator(op.group("op"))
        value = _parse_korean_amount(op.group("num"), op.group("mag") or "")
        if operator and value is not None:
            parsed_ops.append((operator, value))
    if parsed_ops:
        # 이중 경계('30 이상이지만 100 미만'처럼 하한+상한이 한 window 에 함께)면 둘 다 반환(BETWEEN 유사).
        # 하나뿐이면 그대로. '사이/에서~까지' 범위형은 위 range_p 가 이미 처리한다.
        lower = next((p for p in parsed_ops if p[0] in (">=", ">")), None)
        upper = next((p for p in parsed_ops if p[0] in ("<=", "<")), None)
        if lower is not None and upper is not None:
            return [lower, upper]
        return [parsed_ops[0]]
    marker = _EXACT_EQUALS_MARKER.search(window)
    if marker is not None:
        # 단위 필수 도메인은 정확값도 단위 있는 숫자만 본다(eq_p 는 단위 필수) — '정확히 30'(나이) 오탐 방지.
        exact_p = eq_p if unit_required else _EXACT_AMOUNT_PATTERN
        amt = exact_p.search(window, marker.end())
        if amt is not None and amt.group("num"):
            value = _parse_korean_amount(amt.group("num"), amt.group("mag") or "")
            if value is not None:
                return [("=", value)]
    if bare_equals:
        eq = eq_p.search(window)
        if eq is not None:
            value = _parse_korean_amount(eq.group("num"), eq.group("mag") or "")
            if value is not None:
                return [("=", value)]
    return None


# 잔액 지표어 뒤 window 분류: 숫자 비교는 위 공용 문법에 위임하고, 랭킹/%/평균(선택 전략)·존재/부재(잔액
# 전용 어휘)만 여기서 갈라낸다.
_BALANCE_DEFER_PATTERN = re.compile(r"가장|제일|상위|하위|최상위|랭킹|순위|top|톱|퍼센트|프로|%|평균")
# 존재/부재: '보유/있는' → > 0, '없는/미보유/보유하지 않은' → = 0. 부재를 먼저 본다(부정형 '보유하지 않'이
# '보유' 부분문자열로 존재에 오탐되지 않게). '보유액/보유금액'은 지표 명사라 존재로 보지 않는다.
_BALANCE_ABSENCE_PATTERN = re.compile(r"없|미보유|보유하지\s*않|보유\s*안|보유하지\s*못")
_BALANCE_PRESENCE_PATTERN = re.compile(r"보유|가지고|가진|있는|있으신")
_BALANCE_METRIC_NOUN_PATTERN = re.compile(r"보유액|보유금액|보유량")

# '값 자체가 없음'(데이터 미기입, NULL)을 '0(원/회)'과 구분하는 표지. "정보(가) 없는 / 값이 없는 /
# 입력되지 않은 / 미입력 / 기재되지 않은 / 미기재 / 누락". 카트 수량 미입력(QTY IS NULL)도 이 표지를
# 공유한다. NULL 은 '0원/0회'(값이 0)와도, '한 번도'(COALESCE=0, NULL 포함)와도 다른 세 번째 의미다.
_DATA_MISSING_PATTERN = re.compile(
    r"정보\S*\s*없|값\S*\s*없|입력\s*(?:되지|하지)?\s*(?:않|안|못)|미입력|기재\S*\s*않|미기재|누락"
)
# 명시적 0 값(0원/0회/0건/0개/0번). 앞뒤에 숫자·소수점이 없어야 '100원'·'0.5'의 부분문자열에 오탐하지
# 않는다(단위는 선택 — 잔액 뒤 조사 다음의 맨 '0'도 잡는다).
_BALANCE_ZERO_MARKER = re.compile(r"(?<![\d,.])0\s*(?:원|회|건|개|번|명)?(?![\d,.])")


def _balance_null_zero_mode(window: str) -> str | None:
    """수치 지표 뒤 window 에서 NULL/0 의미를 분류한다. 반환:
      'null_or_zero' — 부재어('없')와 명시 0 이 함께('없거나 0원') → (col IS NULL OR col = 0)
      'is_null'      — 값 미기입('정보가 없는 / 입력되지 않은') → col IS NULL (0 과 구분)
      'zero_exact'   — 명시적 0('0원/0회')만 → col = 0 (NULL 제외, COALESCE 아님)
      None           — 위 신호 없음(호출자가 기존 비교/존재/부재 분류로 처리)
    '없'만 있고 '정보/0' 신호가 없으면 None 을 돌려 기존 부재(=0/COALESCE) 폴백을 유지한다."""
    has_zero = _BALANCE_ZERO_MARKER.search(window) is not None
    if _BALANCE_ABSENCE_PATTERN.search(window) and has_zero:
        return "null_or_zero"
    if _DATA_MISSING_PATTERN.search(window):
        return "is_null"
    if has_zero:
        return "zero_exact"
    return None


# 잔액 '선택 전략'(랭킹/퍼센타일/평균) 감지 — WHERE 임계가 아니라 정렬·TOP·서브쿼리로 뽑는다.
_BALANCE_HIGH_TERMS = re.compile(r"가장\s*많|제일\s*많|가장\s*높|제일\s*높|많은|높은|큰|상위|최상위")
_BALANCE_LOW_TERMS = re.compile(r"가장\s*적|제일\s*적|가장\s*낮|제일\s*낮|적은|낮은|작은|하위")
_BALANCE_PERCENT_PATTERN = re.compile(r"(?P<dir>상위|하위)?\s*(?P<pct>\d+(?:\.\d+)?)\s*(?:%|퍼센트|프로)")
_BALANCE_TOPN_PATTERN = re.compile(r"상위\s*(?P<a>[\d,]+)|(?P<b>[\d,]+)\s*명")


def _classify_balance_selection(window: str, column: str, label: str) -> dict[str, Any] | None:
    """잔액 선택 전략을 분류한다 → {mode, column, label, ...}. 랭킹/%/평균 마커가 없으면 None.
    평균 대비('평균보다 높은') → vs_average, 퍼센타일('상위 5%') → top_percent, 상위 N 명 → top_n.

    비교어(높은/낮은/많은/적은/큰/작은)만 있고 최상급/상위 표지가 없어도, 명시적 개수(N명)나 퍼센트가
    함께 있으면 랭킹으로 확정한다('적립금이 높은 회원 100명'). 반대로 개수·퍼센트 없는 비교어 단독
    ('적립금이 높은 회원')은 임의 개수를 붙이지 않고 None 을 돌려 기존 정책 경로에 맡긴다."""
    low = bool(_BALANCE_LOW_TERMS.search(window))
    high = bool(_BALANCE_HIGH_TERMS.search(window))
    has_range = bool(_BALANCE_TOPN_PATTERN.search(window) or _BALANCE_PERCENT_PATTERN.search(window))
    # 최상급/상위·하위·%·평균(_BALANCE_DEFER)이 없더라도, 비교어+명시 범위(N명/N%)면 랭킹으로 본다.
    if not _BALANCE_DEFER_PATTERN.search(window) and not ((low or high) and has_range):
        return None
    if "평균" in window:
        # 경계 포함/배타를 구분: '평균 이상'→>=, '평균 이하'→<=, '평균보다 높은/많은'→>, '평균보다 낮은/적은'→<.
        if re.search(r"평균\s*이상|평균\s*보다\s*(?:크|많|높)거나\s*같", window):
            average_op = ">="
        elif re.search(r"평균\s*이하|평균\s*보다\s*(?:작|적|낮)거나\s*같", window):
            average_op = "<="
        else:
            average_op = "<" if low and not high else ">"
        return {"mode": "vs_average", "column": column, "label": label, "average_op": average_op}
    percent = _BALANCE_PERCENT_PATTERN.search(window)
    if percent is not None:
        pct = float(percent.group("pct"))
        if 0 < pct < 100:
            direction = "low" if (percent.group("dir") == "하위" or (low and not high)) else "high"
            return {"mode": "top_percent", "column": column, "label": label, "percent": pct, "direction": direction}
    if high or low:
        top = _BALANCE_TOPN_PATTERN.search(window)
        n = _parse_count(top.group("a") or top.group("b")) if top else None
        if n is None and re.search(r"가장|제일|상위|하위|최상위", window):
            n = int(_member_metric_ranking_config().get("default_top_n") or 100)
        if n:
            max_top_n = int(_member_metric_ranking_config().get("max_top_n") or 10000)
            direction = "low" if (low and not high) else "high"
            return {"mode": "top_n", "column": column, "label": label, "n": max(1, min(n, max_top_n)), "direction": direction}
    return None


def _apply_balance_selection_filter(query: str, plan: dict[str, Any]) -> None:
    """'예치금이 가장 많은 100명/상위 5%/평균보다 높은'을 잔액 선택 전략(member_metric_selection)으로 해석한다.

    임계값 조건(balance_conditions)이 이미 잡혔으면 선택 전략이 아니다(부등호/범위/등호 우선). 지표 동의어
    주변 window 를 _classify_balance_selection 으로 분류해 build_member_column_selection_sql_candidate 가
    정렬·TOP/PERCENT·평균 서브쿼리로 컴파일하게 한다."""
    if plan.get("member_metric_selection") is not None:
        return
    if isinstance(plan.get("target_user", {}).get("balance_conditions"), list):
        return  # 이미 WHERE 임계로 잡힘
    for entry in _numeric_metric_filters():
        column = entry["column"].split(".")[-1]
        label = entry.get("canonical", column)
        synonyms = sorted([s for s in entry.get("synonyms", []) if isinstance(s, str) and s], key=len, reverse=True)
        for synonym in synonyms:
            index = query.find(synonym)
            if index < 0:
                continue
            selection = _classify_balance_selection(query[index: index + 60], column, label)
            if selection is not None:
                plan["member_metric_selection"] = selection
                return


# '평균 대비' 비교 표지: '평균보다/평균 대비/평균 이상/이하/초과/미만'. 지표 명사('평균 주문 금액')나
# 파생 비율('하루 평균 …')과 달리, 평균 직후에 비교어가 오는 형태만 잡는다('평균 구매' 는 매칭 안 됨).
_AVERAGE_COMPARISON_MARKER = re.compile(r"평균\s*(?:보다|대비|이상|이하|초과|미만)")

# 구매 금액 0원 표지: 정확히 0원(10원/100원의 끝 0 은 제외) + 구매/결제 문맥. '0원 결제/구매 금액 0원' 등.
_ZERO_AMOUNT_MARKER = re.compile(r"(?<!\d)0\s*원")
_ZERO_AMOUNT_CONTEXT = ("구매", "결제", "구매액", "구매금액", "구매 금액", "주문 금액", "주문금액")


def _zero_amount_semantics() -> dict[str, Any]:
    config = _MEMBER_TARGET_FILTERS.get("zero_amount_semantics")
    if not isinstance(config, dict):
        config = _DEFAULT_MEMBER_TARGET_FILTERS["zero_amount_semantics"]
    return config


def _has_zero_amount_purchase_condition(query: str) -> bool:
    """'구매 금액이 0원인' 처럼 정확히 0원인 구매/결제 금액 조건인지. 10원/100원 등은 제외한다."""
    return bool(_ZERO_AMOUNT_MARKER.search(query)) and any(sign in query for sign in _ZERO_AMOUNT_CONTEXT)


# 기간 대 기간 비교 감지용 달력 구간 토큰(전주=지역명 등 오탐 소지 있는 표현은 제외). 두 개 이상의 서로
# 다른 구간이 '보다/대비' 비교와 함께 오면 기간 대 기간 비교로 본다('지난달 결제 금액이 이번 달보다 많은').
_PERIOD_TOKENS = ("지난달", "저번달", "전월", "이번달", "금월", "당월", "지난주", "이번주", "올해", "금년", "작년", "지난해")
# 롤링 기간 대 기간: '최근 N일 vs 이전/직전 N일'. 달력어가 아니라 상대 창 두 개를 비교한다.
_ROLLING_PRIOR_PERIOD_RE = re.compile(r"이전|직전")
_PERIOD_COMPARE_MARKER_RE = re.compile(r"보다|대비|증가|감소|늘|줄")


def _has_period_over_period_comparison(query: str) -> bool:
    """두 기간의 집계를 비교하는지. ①달력 구간 2개+'보다/대비'('지난달 결제액이 이번 달보다 많은'), 또는
    ②롤링 창 2개('최근 90일 객단가가 이전 90일보다 증가')를 잡는다. 단일 구간 임계('지난달 … 10만원보다',
    '최근 90일 … 20만원 이상')는 구간이 하나뿐이라 제외된다."""
    compact = (query or "").replace(" ", "")
    calendar_found = {token for token in _PERIOD_TOKENS if token in compact}
    if len(calendar_found) >= 2 and _PERIOD_COMPARE_MARKER_RE.search(compact):
        return True
    # 롤링: '최근'(현재 창)과 '이전/직전'(직전 창)이 함께, 비교 표지와 같이 오면 기간 대 기간.
    if "최근" in compact and _ROLLING_PRIOR_PERIOD_RE.search(compact) and _PERIOD_COMPARE_MARKER_RE.search(compact):
        return True
    return False


# 회원 내 시점 비교(E-2): 회원별 '첫 구매'값과 '최근 구매'값을 비교. '첫 구매'=order_count=1 로,
# '구매 금액 큰'=랭킹으로 분해하면 안 되는(시점 기준 두 값 비교) 표현이다.
_FIRST_PURCHASE_REF_RE = re.compile(r"첫\s*구매|첫구매|최초\s*구매|첫\s*주문|최초\s*주문|첫\s*결제")
_LATEST_PURCHASE_REF_RE = re.compile(r"최근\s*구매|마지막\s*구매|최종\s*구매|최근\s*주문|마지막\s*주문|최종\s*주문|최근\s*결제")
_INTRA_TEMPORAL_COMPARE_RE = re.compile(r"보다|대비|큰|작은|많은|적은|높은|낮은|증가|감소|커진|늘|줄")


def _has_intra_member_temporal_comparison(query: str) -> bool:
    """'첫 구매 금액보다 최근 구매 금액이 큰' 처럼 회원별 시점(첫/최근) 값 비교인지. 첫·최근 구매 지시가
    모두 있고 비교 표지가 함께 와야 한다(단독 '첫 구매 고객'·'최근 구매 고객'은 아님)."""
    return bool(
        _FIRST_PURCHASE_REF_RE.search(query)
        and _LATEST_PURCHASE_REF_RE.search(query)
        and _INTRA_TEMPORAL_COMPARE_RE.search(query)
    )


def _find_aggregate_metric_id_in(text: str) -> str | None:
    """텍스트 조각에 나타난 집계 지표(aggregate_targets) 동의어를 찾아 metric_id 반환(긴 동의어 우선)."""
    metrics = _aggregate_targets_config().get("metrics", {})
    pairs: list[tuple[str, str]] = []
    for metric_id, metric in metrics.items():
        for synonym in metric.get("synonyms", []):
            if isinstance(synonym, str) and synonym:
                pairs.append((synonym, metric_id))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    for synonym, metric_id in pairs:
        if synonym in text:
            return metric_id
    return None


def _detect_ratio_comparison(query: str) -> dict[str, str] | None:
    """'A 대비 B'(비율) 표현에서 양쪽 지표를 뽑는다 → {numerator: B, denominator: A}. '평균 대비'(단일
    지표 평균 비교)나 기간 '대비'(달력/롤링)는 양쪽이 지표가 아니라 여기서 잡히지 않는다."""
    index = query.find("대비")
    if index < 0:
        return None
    denominator = _find_aggregate_metric_id_in(query[max(0, index - 25):index])
    numerator = _find_aggregate_metric_id_in(query[index + 2: index + 27])
    if denominator and numerator and denominator != numerator:
        return {"numerator": numerator, "denominator": denominator}
    return None


# 쿠폰 '사용 건수' 임계('쿠폰 3개 이상 사용')·순위·지표 비교·파생(쿠폰당 구매금액)의 미지원 판정은 이제
# 문장별 정규식이 아니라 JSON 스펙(segment_metrics.json) + segment_semantics 의미 노드 + capability 게이트로
# 처리한다(_apply_coupon_semantics). 어순에 따라 임계값이 조용히 USE_CPN_CNT>0 으로 축소되던 결함을 없앤다.
# 캠페인 메시지 '받은/수신 횟수' 임계('메시지 3회 이상 받은'): 접촉(EXISTS)·반응 횟수(campaign_response_frequency)
# 는 있으나 '발송/수신 건수' 임계는 모델링되지 않았다(반응 팩트는 반응자 중심 적재라 수신 횟수 분모가 없다).
_MESSAGE_RECEIVED_COUNT_RE = re.compile(
    r"(?:메시지|문자|알림|톡|dm)(?:를|을|이|은|는)?\s*\d+\s*(?:회|번|건)\s*(?:이상|이하|초과|미만)?\s*(?:받|수신)"
)
# AND·OR 우선순위: OR(또는/이거나/거나)이 '임계 조건'(구매 금액/횟수·잔액·카트 등)을 피연산자로 물면 현재
# 컴파일러가 OR 를 표현하지 못한다 — union_condition 은 회원 속성 집합식(연령/성별/등급/지역 canonical)만
# 컴파일하므로, 임계가 낀 OR 은 조용히 AND 로 뭉개지거나(분기 소실) 같은 방향 임계가 첫 값으로 붕괴한다.
# 지역 OR(→SIDO IN)·연령 OR(→구간)처럼 IN/구간으로 접히는 동종 속성 OR 은 정상이라 게이트하지 않는다.
_OR_CONNECTIVE_RE = re.compile(r"또는|이거나|거나")
# OR 피연산자 경계: AND 접속어·다른 OR·'중'(회원 중)·쉼표. 이 경계 안에 수치 임계가 있으면 그 OR 분기가
# 임계 조건이라는 뜻(AND 로 뒤에 붙은 임계는 경계 밖이라 제외 — '20대 또는 30대이면서 5회'의 5회 등).
_OR_OPERAND_BOUNDARY_RE = re.compile(r"이면서|면서|이고|이며|그리고|동시에|반면|지만|중|또는|이거나|거나|,")
_OR_OPERAND_THRESHOLD_RE = re.compile(r"\d[\d,]*\s*(?:회|원|개|건|명|번|종|일|장|점|%)\s*(?:이상|이하|초과|미만)")


def _has_metric_or_branch(compact: str) -> bool:
    """OR 연결어의 좌/우 피연산자(다음 경계까지)에 수치 임계 비교가 있으면 True — '임계를 OR 로 묶음'."""
    for m in _OR_CONNECTIVE_RE.finditer(compact):
        left_start = 0
        for b in _OR_OPERAND_BOUNDARY_RE.finditer(compact, 0, m.start()):
            left_start = b.end()
        right_bound = _OR_OPERAND_BOUNDARY_RE.search(compact, m.end())
        left = compact[left_start:m.start()]
        right = compact[m.end(): right_bound.start() if right_bound else len(compact)]
        if _OR_OPERAND_THRESHOLD_RE.search(left) or _OR_OPERAND_THRESHOLD_RE.search(right):
            return True
    return False


def _remove_coupon_campaign_responses(target_user: dict[str, Any]) -> None:
    """campaign_responses 에서 쿠폰 항목(coupon_used/no_coupon_used)만 제거한다(offer/buy/contact 은 보존).

    _apply_campaign_response_filter 의 어순 취약한 리터럴 매칭이 남긴 쿠폰 항목(예: '쿠폰 사용 횟수가 5회
    초과'의 부분문자열 '쿠폰사용' 오탐으로 붙은 coupon_used)을 걷어내고, segment_semantics 의 JSON 기반
    판정으로 다시 세운다(멱등)."""
    responses = target_user.get("campaign_responses")
    if not responses:
        return
    kept = [r for r in responses if r.get("canonical") not in ("coupon_used", "no_coupon_used")]
    if kept:
        target_user["campaign_responses"] = kept
    else:
        target_user.pop("campaign_responses", None)


def _apply_coupon_semantics(query: str, plan: dict[str, Any]) -> None:
    """쿠폰 도메인 조건을 JSON 스펙(segment_metrics/operators) 기반 의미 노드 + capability 게이트로 해석한다.

    문장별 정규식·어순 의존 대신 segment_semantics.interpret() 가 지표/연산자/값/범위/부정/비교대상/파생식을
    '완성'한 뒤 capability 로 지원/미지원을 판정한다. 이 어댑터는 그 결과를 plan 에 반영한다:
      * 지원(사용 여부 존재/부재) → campaign_responses 에 coupon_used/no_coupon_used(멱등 재구성).
      * 미지원(사용 건수 임계/순위/지표 비교/파생 비율) → plan['unsupported'] 로 명시(임계값 조용한 축소 금지).

    미지원 사유는 무관한 일반 폴백(metric_not_resolved 등)만 대체하고, 더 구체적인 상위 게이트(논리식/기간
    비교 등)는 건드리지 않는다. 모든 결정 근거(의미 노드)는 plan['_coupon_ir'] 에 남겨 SQL 생성 전 의미
    보존 검증(_guard_coupon_semantic_preservation)이 조용한 의미 소실을 fail-close 로 막게 한다."""
    if _SEGMENT_SEMANTICS is None or "쿠폰" not in query:
        return
    interp = segment_semantics.interpret(query, _SEGMENT_SEMANTICS)
    if interp is None:
        return
    target_user = plan.setdefault("target_user", {})
    _remove_coupon_campaign_responses(target_user)
    target_user.pop("coupon_usage_thresholds", None)  # 멱등 재구성(재감지 대비)
    cond = interp.condition
    plan.setdefault("_coupon_ir", []).append({**cond.to_dict(), "_gated": not interp.capability.supported})

    if not interp.capability.supported:
        cap = interp.capability
        existing = plan.get("unsupported")
        if not existing or existing.get("reason") in _COUPON_OVERRIDABLE_REASONS:
            plan["unsupported"] = {
                "reason": cap.code,
                "message": cap.message,
                "clarification": cap.clarification or cap.message,
            }
        # 지표 비교/순위가 순위 트랙으로 오배정돼 있으면(예: '쿠폰 수보다 구매건수가 많은' → member_metric_ranking)
        # 걷어낸다 — 미지원이 build 단계에서 이기지만, 조용한 오배정 흔적을 남기지 않는다.
        if cond.type in ("metric_comparison", "ranking"):
            plan.pop("member_metric_ranking", None)
            plan.pop("purchase_count_ranking", None)
            plan.pop("group_ranking_target", None)
        return

    # 지원되는 사용 여부(존재/부재) → campaign_responses 로 컴파일(기존 EXISTS/NOT EXISTS 빌더가 소비).
    if cond.type == "existence_filter" and interp.existence_predicate:
        entry: dict[str, Any] = {
            "canonical": "coupon_used" if cond.exists else "no_coupon_used",
            "predicate": interp.existence_predicate,
        }
        if not cond.exists:
            entry["negated"] = True
        target_user.setdefault("campaign_responses", []).append(entry)
        return

    # 지원되는 사용 '건수' 임계(≥2·>5·범위 등) → 회원별 SUM(USE_CPN_CNT) HAVING 집계 조건으로 컴파일한다
    # (compile_member_target_conditions 가 회원키 IN 서브쿼리 술어로 만들어 다른 조건과 AND 결합).
    if cond.type == "metric_filter":
        threshold: dict[str, Any] = {"operator": cond.operator}
        if cond.operator == "between":
            threshold["min_value"] = cond.min_value
            threshold["max_value"] = cond.max_value
        else:
            threshold["value"] = cond.value
        target_user.setdefault("coupon_usage_thresholds", []).append(threshold)


# ── 랭킹 정렬키 지표(ORDER BY 대상)의 구조적 판정 ─────────────────────────────────────
# 게이트/랭킹 라우팅의 핵심 질문은 "'상위 N'이 지표 정렬 랭킹인가, 아니면 임계로 정의된 오디언스의 단순
# result_limit 캡인가"이다. 예전엔 원문 키워드 공존(기간어+지표어+상위N)만으로 랭킹이라 단정해 오탐했다.
# 대신 '지표가 실제로 랭킹 어구에 결합됐는가'를 구조적으로 판정한다 — 임계값('N 이상')에 결합된 지표는
# 정렬키가 아니고(HAVING 필터), '기준/순/많은/높은/큰/적은/낮은/상위/하위'에 결합된 지표만 정렬키다.
@functools.lru_cache(maxsize=4)
def _ranking_sort_binding_pattern(path_text: str) -> "re.Pattern[str] | None":
    """member_metrics 동의어가 '랭킹 어구'(기준/순/최상급/많은·높은·큰·적은·낮은/상위·하위·top)에 결합된
    형태만 잡는 패턴. 숫자+이상/이하(임계값 결합)엔 매칭되지 않아, 정렬키로 쓰인 지표만 식별한다. 지표어와
    떨어진 '<지표> 기준 상위 N'(관용 어순 아님)도 포함한다. 긴 동의어 우선(짧은 동의어가 앞을 삼키지 않게)."""
    registry = _load_member_metrics(path_text)
    if not registry:
        return None
    synonyms = [
        syn
        for metric in registry.get("metrics", [])
        for syn in metric.get("synonyms", [])
        if isinstance(syn, str) and syn
    ]
    if not synonyms:
        return None
    synonyms.sort(key=len, reverse=True)
    alternation = "|".join(re.escape(syn) for syn in synonyms)
    return re.compile(
        rf"(?P<metric>{alternation})\s*(?:이|가|은|는|을|를|의)?\s*"
        rf"(?:기준(?:으로)?"          # '<지표> 기준(으로) [상위…]'
        rf"|순(?:으로|서)?(?=\s|$|[0-9])"   # '<지표> 순으로/순서/순 N'
        rf"|(?:가장|제일)\s*(?:많|높|큰|적|낮)"  # 최상급('가장 많은')
        rf"|많은|높은|큰|적은|낮은"    # 비교 상위/하위형
        rf"|상위|하위|top)",            # 지표어 바로 뒤 '상위/하위/top'
        re.IGNORECASE,
    )


@functools.lru_cache(maxsize=4)
def _member_metric_synonym_pattern(path_text: str) -> "re.Pattern[str] | None":
    """member_metrics 동의어를 잡는 단순 패턴(랭킹 어구 결합 없이 지표어 위치만). 긴 동의어 우선."""
    registry = _load_member_metrics(path_text)
    if not registry:
        return None
    synonyms = sorted(
        {syn for metric in registry.get("metrics", []) for syn in metric.get("synonyms", []) if isinstance(syn, str) and syn},
        key=len, reverse=True,
    )
    if not synonyms:
        return None
    return re.compile(rf"(?P<metric>{'|'.join(re.escape(syn) for syn in synonyms)})")


# 지표어 바로 뒤가 '숫자[배수]단위 비교연산자'(예: '10회 이상', '5만원 이상')면 그 지표는 임계값에 결합된
# HAVING 필터이지 정렬키가 아니다. _AGG_UNIT/_OP_ALT_BASIC 는 집계 임계 파서와 동일 어휘를 재사용한다.
_METRIC_THRESHOLD_TAIL_RE = re.compile(
    rf"^\s*(?:이|가|은|는|을|를|의)?\s*[\d,]+\s*(?:억|천만|백만|만|천)?\s*(?:{_AGG_UNIT})?\s*(?:{_OP_ALT_BASIC})"
)


def _resolve_free_ranking_metric_info(query: str) -> dict[str, Any] | None:
    """순위 지시가 지표 앞에 오는 형태('TOP 20 매출 고객')를 위해, 임계값에 결합되지 않은(자유) 회원 지표를
    정렬키 후보로 찾는다. 지표어 바로 뒤가 임계값('N 이상')이면 정렬키가 아니므로 건너뛴다. 순위 지시가
    있을 때만 호출한다(단독 지표 언급을 랭킹으로 오인하지 않게)."""
    path = str(DEFAULT_MEMBER_METRICS_PATH)
    pattern = _member_metric_synonym_pattern(path)
    if pattern is None:
        return None
    for match in pattern.finditer(query):
        if _METRIC_THRESHOLD_TAIL_RE.match(query[match.end():]):
            continue  # 임계 결합 지표 → 정렬키 아님
        info = _member_metric_by_synonym(path, match.group("metric"))
        if info:
            return info
    return None


def _resolve_ranking_sort_metric_info(query: str) -> dict[str, Any] | None:
    """'상위 N'이 정렬하는 회원 지표(ORDER BY 대상)를 구조적으로 판정한다. 관용 어순('<지표> 높은 고객'),
    랭킹 어구 결합('<지표> 기준/순/많은/상위'), 또는 순위 지시 존재 시 임계값에 결합되지 않은 자유 지표
    ('TOP 20 매출')만 정렬키로 인정하고, 임계값('N 이상')에만 결합된 지표는 정렬키가 아니므로 반환하지
    않는다. 정렬키가 없으면 None → '상위 N'은 단순 result_limit 캡이다."""
    path = str(DEFAULT_MEMBER_METRICS_PATH)
    customer = _member_metric_customer_pattern(path)  # ① 관용 어순: '<지표> 높은/많은/큰/상위 고객/회원'
    match = customer.search(query) if customer else None
    if match:
        info = _member_metric_by_synonym(path, match.group(1))
        if info:
            return info
    binding = _ranking_sort_binding_pattern(path)  # ② 랭킹 어구 결합: '<지표> 기준/순/많은/상위 …'
    bmatch = binding.search(query) if binding else None
    if bmatch:
        info = _member_metric_by_synonym(path, bmatch.group("metric"))
        if info:
            return info
    if _detect_ranking_directive(query) is not None:  # ③ 순위 지시 + 자유 지표('TOP 20 매출 고객')
        return _resolve_free_ranking_metric_info(query)
    return None


def _resolve_ranking_sort_metric_id(query: str) -> str | None:
    info = _resolve_ranking_sort_metric_info(query)
    return info.get("metric_id") if info else None


def _metric_scoping_period_targets_metric(query: str, plan: dict[str, Any], sort_metric_id: str | None) -> bool:
    """기간 스코프가 '랭킹 정렬 지표'에 적용되는지(구조 기반). 정렬 지표가 자체 기간 창(aggregate_condition
    .window_days)을 가지면 명백히 기간 스코프 대상(True). 반대로 기간이 캠페인 반응 조건에만 귀속되고
    (campaign_response_frequency 등 창 보유) 정렬 지표엔 창이 없으면, 기간은 정렬 지표를 스코프하지 않으므로
    False(전 기간 스냅샷 랭킹으로 표현 가능 → 기간 스코프 랭킹 아님). 구조적으로 분리를 확신 못 하면 보수적
    으로 True(기존 차단 유지)."""
    conditions = plan.get("target_user", {}).get("aggregate_conditions") or []
    sort_metric_has_window = any(
        cond.get("metric_id") == sort_metric_id and cond.get("window_days") for cond in conditions
    )
    if sort_metric_has_window:
        return True
    if _period_is_campaign_scoped(plan):
        return False
    return True


def _period_is_campaign_scoped(plan: dict[str, Any]) -> bool:
    """기간 창이 캠페인 반응 조건에 귀속됐는지 — campaign_response_frequency 나 창을 가진 campaign_responses.
    이 경우 '최근 N개월'은 캠페인 반응 여부를 스코프하는 것이지 회원 지표 집계를 스코프하지 않는다."""
    target_user = plan.get("target_user", {})
    frequency = target_user.get("campaign_response_frequency")
    if isinstance(frequency, dict) and frequency.get("window_days"):
        return True
    responses = target_user.get("campaign_responses")
    if isinstance(responses, list):
        return any(isinstance(r, dict) and r.get("window_days") for r in responses)
    return False


def _is_threshold_limited_audience(query: str, plan: dict[str, Any]) -> bool:
    """지표가 임계(aggregate_conditions/HAVING)로 정의됐고 '상위 N'이 정렬키 없는 단순 행수 캡(result_limit)인
    구조인지. 판정은 원문 키워드가 아니라 파싱된 aggregate_conditions·result_limit 과 구조적 정렬키
    (_resolve_ranking_sort_metric_id)로만 한다. 이때 기간 스코프 랭킹/랭킹 기준 미지정 게이트를 발동하지
    않고 result_limit 로 통과시킨다(HAVING 절대 임계 + LIMIT)."""
    if not (plan.get("target_user", {}).get("aggregate_conditions") or []):
        return False
    if plan.get("result_limit") is None:
        return False
    return _resolve_ranking_sort_metric_id(query) is None


def _apply_unsupported_intent_gate(query: str, plan: dict[str, Any]) -> None:
    """해석은 되지만 실DB SQL 로 컴파일할 수 없는 표현을 조용한 오답/빈결과 대신 '명시적 미지원'으로 표시한다.

    이런 표현은 지금까지 엉뚱한 트랙(상품 텍스트 검색 등)으로 폴백해 그럴듯하지만 틀린 SQL 을 냈다.
    plan['unsupported']={reason,message,clarification} 를 남기면 build_sql_template_candidate 가 후보
    생성을 중단하고 build_sql_result 가 unsupported_reason/clarification 으로 명시 응답한다."""
    if plan.get("unsupported"):
        return
    target_user = plan.get("target_user", {})
    compact = query.replace(" ", "")

    # 캠페인 메시지 '받은/수신 횟수' 임계('메시지 3회 이상 받은')는 아직 미모델 — 반응 횟수로 오매핑하지 않게 명시 미지원.
    if _MESSAGE_RECEIVED_COUNT_RE.search(compact):
        plan["unsupported"] = {
            "reason": "message_received_count_unsupported",
            "message": "'메시지를 N회 이상 받은'처럼 캠페인 발송/수신 '횟수' 임계 조건은 아직 지원되지 않습니다(접촉 여부·반응 횟수만 지원).",
            "clarification": "'캠페인을 받은 회원'(접촉 여부) 또는 '캠페인에 N회 이상 반응한'(반응 횟수)으로 지정하시겠어요?",
        }
        return

    # AND·OR 우선순위: OR 가 임계 조건(구매 금액/횟수·잔액·카트)을 물었는데 union_condition 으로 컴파일되지
    # 못했으면, 조용히 AND 로 뭉개(분기 소실)거나 같은 방향 임계가 붕괴한다 — 명시 미지원으로 중단.
    # 지역/연령 OR(→SIDO IN·구간)은 정상 컴파일이라 _has_metric_or_branch 가 임계 경계로 제외한다.
    # 논리식 컴파일러(feature flag)가 켜져 있으면 이 게이트는 양보한다 — _apply_logical_expression 이
    # 컴파일 성공 시 SQL 을, 실패 시 자체 fail-close 미지원을 남긴다(여전히 AND-only 폴백 금지).
    if not plan.get("union_condition") and not _logical_or_compiler_enabled() and _has_metric_or_branch(compact):
        plan["unsupported"] = {
            "reason": "mixed_and_or_precedence_unsupported",
            "message": "구매 금액/횟수·잔액·장바구니 같은 '임계 조건'을 OR(또는/이거나)로 묶는 조건은 아직 지원되지 않습니다 — 현재 OR 은 연령·성별·등급·지역 같은 회원 속성에만 지원됩니다.",
            "clarification": "OR 분기를 각각 별도 조건으로 나눠 주시겠어요? 예: '로그인 100회 이상' 세그먼트와 '구매 10회 이상이면서 마케팅 동의' 세그먼트를 따로 추출.",
        }
        return

    # 기간 대 기간 비교(달력 '지난달 대비 이번 달' / 롤링 '최근 90일 vs 이전 90일')는 아직 미지원. 두 기간
    # 집계를 비교하는 구조라 단일 서브쿼리로 표현 불가 — 조용한 None/전체기간 폴백 대신 명시 미지원으로 중단.
    if _has_period_over_period_comparison(query):
        plan["unsupported"] = {
            "reason": "period_over_period_comparison_not_supported",
            "message": "'지난달 대비 이번 달'·'최근 90일 대비 이전 90일'처럼 두 기간의 집계를 비교하는 조건은 아직 지원되지 않습니다.",
            "clarification": "기간 비교 대신 단일 기간 조건(예: '지난달 결제 금액 10만원 이상', '최근 90일 객단가 20만원 이상')으로 지정해 주시겠어요?",
        }
        return

    # 회원 내 시점 비교(E-2): '첫 구매 금액보다 최근 구매 금액이 큰'. '첫 구매'=order_count=1, '금액 큰'=랭킹으로
    # 분해하면 안 되는 시점 기준 값 비교라 아직 미지원. 조용히 엉뚱한 트랙(무구매/랭킹)으로 분해하지 않는다.
    if _has_intra_member_temporal_comparison(query):
        plan["unsupported"] = {
            "reason": "intra_member_temporal_metric_comparison_not_supported",
            "message": "'첫 구매 금액보다 최근 구매 금액이 큰'처럼 회원별 시점(첫/최근) 값을 비교하는 조건은 아직 지원되지 않습니다.",
            "clarification": "시점 비교 대신 단일 시점 조건(예: '최근 구매 금액 10만원 이상')으로 지정해 주시겠어요?",
        }
        return

    # 비율 표현(D): 'A 대비 B'(구매 횟수 대비 구매 금액). 등록된 파생 비율 지표가 있으면 그걸 쓰고, 없으면
    # 한쪽 지표('구매 금액 높은')만 남겨 매출 랭킹으로 폴백하지 말고 미지원으로 명시한다.
    ratio = _detect_ratio_comparison(query)
    if ratio is not None:
        plan["unsupported"] = {
            "reason": "unregistered_ratio_metric",
            "message": f"'{ratio['denominator']} 대비 {ratio['numerator']}' 같은 비율 지표는 등록돼 있지 않아 아직 지원되지 않습니다.",
            "clarification": "비율(예: 객단가=구매 금액/구매 횟수)이 필요하면 등록된 지표로 바꾸거나, 단일 지표 임계값으로 지정해 주시겠어요?",
            "numerator": ratio["numerator"],
            "denominator": ratio["denominator"],
        }
        return

    # '구매 금액 0원'은 도메인 정책 사안이라 코드가 무구매로 단정하지 않는다(zero_amount_semantics 플래그).
    # maps_to_no_purchase=true 면 무구매(no_purchase, 주문 anti-join)로 컴파일하고, 기본(false)이면 0원 결제와
    # 평생 무주문이 다를 수 있으므로 조용히 넘기지 않고 clarification 으로 되묻는다.
    # 캠페인 문맥('캠페인 구매금액 0원')은 캠페인 반응 트랙(no_buy_response, NOT EXISTS)이 이미 소유하므로 양보한다.
    if (
        _has_zero_amount_purchase_condition(query)
        and not target_user.get("aggregate_conditions")
        and not target_user.get("campaign_responses")
        and "캠페인" not in query
    ):
        if _zero_amount_semantics().get("maps_to_no_purchase"):
            _append_unique(target_user.setdefault("behaviors", []), "no_purchase")
        else:
            plan["unsupported"] = {
                "reason": "zero_amount_semantics_requires_policy",
                "message": (
                    "'구매 금액 0원'을 무구매(주문 없음)와 같은 의미로 볼지 정책이 정해지지 않았습니다"
                    "(zero_amount_semantics.maps_to_no_purchase=false)."
                ),
                "clarification": (
                    "'구매 금액 0원'이 '한 번도 구매하지 않은 회원'(무구매)을 뜻하나요? "
                    "그렇다면 '구매 이력이 없는 회원'으로 지정하거나 정책에서 무구매 동일시를 켜 주세요."
                ),
            }
        return

    # '평균 대비' 비교('구매 금액이 평균보다 높은')인데 지원 지표(회원 컬럼)로 해석되지 않은 경우 → 미지원.
    # 예치금/적립금 등 회원 컬럼은 member_metric_selection(vs_average)으로 이미 해석되므로 여기 안 걸린다.
    # 주문 집계 지표(구매 금액/횟수)의 평균 대비는 회원 단일 테이블 서브쿼리로 표현 불가라 미지원으로 명시한다.
    if (
        _AVERAGE_COMPARISON_MARKER.search(query)
        and plan.get("member_metric_selection") is None
        and not target_user.get("balance_conditions")
    ):
        plan["unsupported"] = {
            "reason": "average_comparison_metric_unsupported",
            "message": (
                "'평균 대비' 비교는 예치금·적립금 등 회원 지표에서만 지원됩니다. "
                "구매 금액 등 주문 집계 지표의 평균 대비 추출은 아직 지원되지 않습니다."
            ),
            "clarification": (
                "'평균보다 높은' 비교를 예치금/적립금 같은 회원 지표로 바꾸거나, "
                "구체적 금액 임계값(예: 구매 금액 10만원 이상)으로 지정해 주시겠어요?"
            ),
        }
        return

    # 기간 스코프 랭킹('최근 3개월/2025년/지난달 <지표> 높은 회원 N명') — 회원 지표 랭킹은 최신 월 스냅샷
    # (전 기간 누적) 기준이라 임의 기간을 표현하지 못한다. 스냅샷 랭킹으로 조용히 보내지 않고(오답 방지)
    # 명시 미지원으로 돌린다.
    #
    # 판정은 원문 키워드 공존(기간어+지표어+상위N)이 아니라 파싱 구조로 한다. 진짜 기간 스코프 랭킹은
    #   ① 기간 스코프가 존재하고(_has_metric_scoping_period)
    #   ② 실제 지표 정렬키(ORDER BY 대상)가 결합돼 있고(_resolve_ranking_sort_metric_id — 임계값 결합 지표는
    #      정렬키가 아니므로 제외; '순수 상위 N'도 정렬키 None)
    #   ③ 그 기간이 정렬 지표에 적용되며(_metric_scoping_period_targets_metric — 캠페인 반응 등 다른 조건에만
    #      귀속된 창이면 정렬 지표를 스코프하지 않는다)
    #   ④ 어떤 랭킹 트랙도 이를 실제로 컴파일하지 못한 경우
    # 일 때만 성립한다. '…구매 횟수 10회 이상, 평균 주문 금액 5만원 이상인 상위 100명'은 정렬키가 없어(② 탈락)
    # 단순 result_limit 캡으로 통과하고, '최근 3개월 구매 횟수가 가장 많은 상위 100명'은 정렬키(구매 횟수)가
    # 기간에 결합돼 계속 차단된다.
    ranking_sort_metric = _resolve_ranking_sort_metric_id(query)
    if (
        _has_metric_scoping_period(query, plan)
        and ranking_sort_metric is not None
        and _metric_scoping_period_targets_metric(query, plan, ranking_sort_metric)
        and plan.get("member_metric_ranking") is None
        and plan.get("group_ranking_target") is None
        and plan.get("purchase_count_ranking") is None
        # 엔터티 순위('최근 90일 매출 상위 5개 카테고리')는 회원 지표 스냅샷 랭킹이 아니라 팩트 집계라
        # 임의 기간을 그대로 표현한다 — 이 트랙이 컴파일하면 미지원이 아니다.
        and not _entity_set_condition_supported(query)
    ):
        plan["unsupported"] = {
            "reason": "period_scoped_ranking_unsupported",
            "message": (
                "기간을 지정한 지표 랭킹(예: '최근 3개월/2025년/지난달 구매 횟수 상위 N명')은 아직 지원되지 않습니다 "
                "— 회원 지표 랭킹은 최신 월 스냅샷(전 기간 누적) 기준이라 임의 기간을 표현할 수 없습니다."
            ),
            "clarification": (
                "기간 없이 누적 기준 상위 N 으로 추출하거나, 기간을 임계 조건(예: '최근 3개월 구매 3회 이상')으로 지정해 주시겠어요?"
            ),
        }
        return

    # 지원 외 그룹 축(등급/채널/브랜드/카테고리별) 상위 N — 지역/성별/연령대 축은 실제 그룹 SQL 로
    # 컴파일되므로(group_ranking_target 세팅) 여기 안 걸린다. 미구현 축만 조용한 전역 붕괴 대신 명시 미지원.
    if (
        _UNSUPPORTED_GROUP_AXIS_RE.search(query)
        and (_PER_GROUP_SUFFIX_RE.search(query) or _detect_ranking_directive(query) is not None)
        and not isinstance(plan.get("group_ranking_target"), dict)
        and not isinstance(plan.get("region_member_count_target"), dict)
        and not _entity_set_condition_supported(query)
    ):
        plan["unsupported"] = {
            "reason": "group_ranking_axis_unsupported",
            "message": "등급·채널 등 그룹 축별 상위 N 추출은 아직 지원되지 않습니다(현재 지역/성별/연령대 그룹만 지원).",
            "clarification": "지역/성별/연령대별 상위 N 으로 바꾸거나, 그룹 없이 전체 상위 N 으로 추출할까요?",
        }
        return

    # 순위 지시('상위/하위 N명·높은/낮은 순·TOP N')는 있는데 기준 지표가 지정되지 않았고, 어떤 랭킹 트랙도
    # 이를 해석하지 못한 경우 → result_limit 만 조용히 적용(그럴듯한 임의 N명)하지 말고 '무엇 기준인지' 되묻는다.
    # 단, 지표가 전부 임계(HAVING)로 정의된 오디언스의 '상위 N'은 정렬키 없는 단순 행수 캡이 사용자 의도이므로
    # (_is_threshold_limited_audience: aggregate_conditions + result_limit + 정렬키 None) 되묻지 않고
    # result_limit 로 통과시킨다.
    if (
        _detect_ranking_directive(query) is not None
        and plan.get("member_metric_ranking") is None
        and plan.get("purchase_count_ranking") is None
        and plan.get("member_metric_selection") is None
        and not isinstance(plan.get("region_density_target"), dict)
        and not isinstance(plan.get("group_ranking_target"), dict)
        and not isinstance(plan.get("region_member_count_target"), dict)
        and not _is_threshold_limited_audience(query, plan)
        # 엔터티 순위('상위 5개 카테고리')는 회원 순위가 아니다 — 기준 지표가 그 절 안에 있고
        # 엔터티 집합 트랙이 컴파일하므로 되묻지 않는다.
        and not _entity_set_condition_supported(query)
    ):
        plan["unsupported"] = {
            "reason": "ranking_metric_unspecified",
            "message": "'상위/하위 N명' 순위의 기준 지표가 지정되지 않았습니다.",
            "clarification": "어떤 지표 기준으로 순위를 매길까요? (예: 누적 구매 금액, 구매 횟수, 예치금 등)",
        }


def _classify_balance_window(window: str, unit: str = "원", bare_equals: bool = True) -> list[tuple[str, float]] | None:
    """수치 지표어 뒤 window 를 [(operator, threshold), ...] 로 분류. 숫자 비교(부등호/범위/등호/'보다 많은')는
    공용 문법(_parse_amount_comparison)에 단위(unit)만 바꿔 위임하고, 존재/부재만 여기서 본다. 랭킹/%/평균이면
    None(선택 전략 파서가 소유). money=원·bare_equals=True(잔액), integer=횟수·bare_equals=False(호출자가 지정)."""
    if _BALANCE_DEFER_PATTERN.search(window):
        return None  # 랭킹/%/평균 → 집계·윈도우 필요, WHERE 임계 아님
    # NULL/0 구분은 숫자 비교보다 먼저 본다 — '없거나 0원'의 '0원'을 _parse_amount_comparison 이 먼저
    # 평범한 =0 으로 채가지 못하게, '정보가 없는'의 '없'이 부재(=0)로 붕괴하지 못하게.
    null_zero = _balance_null_zero_mode(window)
    if null_zero == "null_or_zero":
        return [("NULL_OR_ZERO", 0.0)]  # (col IS NULL OR col = 0)
    if null_zero == "is_null":
        return [("IS NULL", 0.0)]  # 값 미기입(NULL) — 0 과 구분
    comparison = _parse_amount_comparison(window, unit, bare_equals=bare_equals)
    if comparison is not None:
        return comparison
    if null_zero == "zero_exact":
        return [("ZERO_EXACT", 0.0)]  # 명시적 0(0회 등 bare_equals=False 지표 포함) — NULL 제외
    if _BALANCE_ABSENCE_PATTERN.search(window):
        return [("=", 0.0)]  # 없는/미보유/보유하지 않은
    if _BALANCE_PRESENCE_PATTERN.search(window) and not _BALANCE_METRIC_NOUN_PATTERN.search(window):
        return [(">", 0.0)]  # 보유/있는
    return None


def _classify_balance_column_comparison(window: str, self_column: str, entries: list[dict[str, Any]]) -> tuple[str, str] | None:
    """'<다른 잔액지표>보다 많은/적은'(컬럼 대 컬럼 비교) → (operator, 'B.<컬럼>'). 숫자 임계가 아니라
    두 잔액 컬럼을 직접 비교한다(예: '적립금이 예치금보다 많은' → CARROT > DEPOSIT)."""
    for entry in entries:
        other_column = entry["column"].split(".")[-1]
        if other_column == self_column:
            continue
        for synonym in entry.get("synonyms", []):
            if not isinstance(synonym, str) or not synonym:
                continue
            match = re.search(re.escape(synonym) + r"\s*보다\s*(?P<cmp>많|큰|높|적|작|낮)", window)
            if match is not None:
                return (">" if match.group("cmp") in ("많", "큰", "높") else "<"), f"B.{other_column}"
    return None
_RECENT_WINDOW_PATTERN = re.compile(r"최근\s*(\d+)\s*(일|주|개월|달|년)")
_WINDOW_UNIT_DAYS = {"일": 1, "주": 7, "개월": 30, "달": 30, "년": 365}
# 명시적 등호 마커. 연산자어(이상/이하) 없는 임계값은 보통 모호("3회 구매"=정확히? 최소?)하지만,
# '정확히/딱 N'은 등호 의도가 분명하므로 이때만 '='로 확정한다(무턱대고 등호 폴백하지 않는다).
_EXACT_EQUALS_MARKER = re.compile(r"정확히|정확하게|딱")
_EXACT_AMOUNT_PATTERN = re.compile(r"(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<mag>억|천만|백만|만|천)?\s*(?:원|건|회|명|개|장|번|건수|회수)?")
_EXACT_COUNT_PATTERN = re.compile(r"(?P<num>\d+)\s*(?:개|번|회|건)")


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
    """'최근 90일' -> 90, '최근 3개월' -> 90, '최근 2주' -> 14 (없으면 None = 롤링 윈도우 아님).

    이건 '지금으로부터 N일 전까지'의 롤링 윈도우다. '올해'·'지난달' 같은 고정 달력 구간은 성격이
    다르므로(_parse_calendar_period 소유) 여기서 잡지 않는다."""
    match = _RECENT_WINDOW_PATTERN.search(query)
    if not match:
        return None
    count = int(match.group(1))
    if count <= 0:
        return None
    return count * _WINDOW_UNIT_DAYS[match.group(2)]


# 달력 기간(올해/지난달 등): '지금으로부터 N일'의 롤링 윈도우(_parse_recent_window_days)와 구분되는
# 별개 타입이다 — 경계가 달력(연/월/주)에 고정된다. 아직 집계 SQL 에는 반영하지 않는다(별도 작업);
# 여기서는 조건에 표식만 남겨, 기간을 조용히 무시(전체 기간 폴백)하는 대신 명시 경고로 돌려주기 위한
# 감지만 한다. 지역명(전주 등)·연동어(전년대비)와의 오탐을 피하려 경계 명확한 표현만 본다.
_CALENDAR_PERIOD_SIGNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("current_year", ("올해", "금년")),
    ("last_year", ("작년", "지난해")),
    ("previous_month", ("지난달", "저번달")),
    ("current_month", ("이번달",)),
    ("previous_week", ("지난주",)),
    ("current_week", ("이번주",)),
)
_CALENDAR_PERIOD_LABELS = {
    "current_year": "올해",
    "last_year": "작년",
    "previous_month": "지난달",
    "current_month": "이번 달",
    "previous_week": "지난주",
    "current_week": "이번 주",
}


def _parse_calendar_period(query: str) -> str | None:
    """'올해' -> current_year, '지난달' -> previous_month 등 고정 달력 구간을 표준 토큰으로.

    롤링 윈도우(최근 N일)와 다른 별도 타입. 현재는 집계 SQL 에 미반영이라, 감지되면 조건에 표식만
    남겨 명시 경고(_deterministic_dropped_conditions)로 돌려준다 — 전체 기간으로 조용히 폴백하지 않는다."""
    compact = (query or "").replace(" ", "")
    for token, signs in _CALENDAR_PERIOD_SIGNS:
        if any(sign in compact for sign in signs):
            return token
    return None


# 한글 수사(한/두/세…) → 숫자: '두 번 이상'·'정확히 두 번' 같은 표현이 개수 임계값(숫자형) 파서에
# 걸리도록 표면 정규화한다. 개수 단위(번/회/건/개) 바로 앞의 수사만 치환해 금액·연령 등과 갈린다.
# 앞 음절이 한글이면(가세/치열 등 단어 일부) 치환하지 않는다(오탐 방지). 전역이 아니라 개수 임계값
# 추출 경로에서만 로컬 적용한다 — '한 번도 주문하지 않은'(no_purchase 동의어)이 '1번도…'로 바뀌어
# 미구매 매칭이 깨지는 것을 피하기 위해서다.
_KOREAN_COUNT_NUMERALS = {
    "하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "넷": 4, "네": 4,
    "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}
_KOREAN_COUNT_NUMERAL_RE = re.compile(
    r"(?<![가-힣])(" + "|".join(sorted(_KOREAN_COUNT_NUMERALS, key=len, reverse=True)) + r")\s*(번|회|건|개)"
)


def _normalize_korean_count_numerals(text: str) -> str:
    """'두 번' -> '2번', '세 개' -> '3개' 등 한글 수사+개수 단위를 숫자로 치환한다(개수 임계값 파서 공용).

    개수 임계값 추출 경로에서만 로컬로 쓴다(전역 아님). 앞 음절이 한글인 경우(예: '가세 번')는 단어
    일부일 수 있어 치환하지 않는다."""
    if not text:
        return text
    return _KOREAN_COUNT_NUMERAL_RE.sub(
        lambda match: f"{_KOREAN_COUNT_NUMERALS[match.group(1)]}{match.group(2)}", text
    )


_SINO_KOREAN_DIGITS = {"영": 0, "공": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5,
                       "육": 6, "칠": 7, "팔": 8, "구": 9}
_SINO_KOREAN_SMALL_UNITS = {"십": 10, "백": 100, "천": 1000}
_SINO_KOREAN_AMOUNT_RE = re.compile(
    r"(?P<num>[영공일이삼사오육칠팔구십백천]+)(?P<mag>억|천만|백만|만)?원"
)


def _parse_sino_korean_integer(text: str) -> int | None:
    """일~구/십/백/천 조합을 양의 정수로 바꾼다('이십'→20, '삼백오'→305)."""
    if not text:
        return None
    total, pending = 0, 0
    for char in text:
        if char in _SINO_KOREAN_DIGITS:
            pending = _SINO_KOREAN_DIGITS[char]
        elif char in _SINO_KOREAN_SMALL_UNITS:
            total += (pending or 1) * _SINO_KOREAN_SMALL_UNITS[char]
            pending = 0
        else:
            return None
    value = total + pending
    return value if value > 0 else None


def _normalize_sino_korean_amounts(text: str) -> str:
    """금액 위치의 한자어 수사를 기존 숫자 금액 문법으로 낮춘다('이십만원'→'20만원').

    반드시 `원`으로 끝나는 금액만 변환하므로 이십대/삼십일 같은 연령·날짜 표현에는 관여하지 않는다.
    """
    def replace(match: "re.Match[str]") -> str:
        value = _parse_sino_korean_integer(match.group("num"))
        return match.group(0) if value is None else f"{value}{match.group('mag') or ''}원"

    return _SINO_KOREAN_AMOUNT_RE.sub(replace, text)


# ── 상품/주문 집계 조건 리졸버(스펙 기반·점수화) ─────────────────────────────────
# 지표는 aggregate_targets.metrics 스펙(semantic_type/agg/column/distinct/table/units/hint_terms/
# anti_hint_terms/synonyms)으로만 등록한다 — 문장별 파이썬 분기 없이 스펙만으로 신규 지표를 추가한다.
# 절 경계 접속어: 서로 다른 조건을 가르는 접속어. 단일 지표 범위('10만 이상이지만 50만 미만')는 뒷 절에
# 지표가 없어 '고아 bound'로 앞 지표에 병합되므로, 접속어로 끊어도 범위가 안 깨진다.
# 쉼표는 숫자 천단위 구분(100,000) 안에서는 절 경계로 쓰지 않는다(숫자 사이가 아닌 쉼표만 분리).
# 가법 접속어(이고/이며/그리고)도 절을 가른다 — 단일 지표 범위('10만 이상이고 50만 이하')는 뒷 절에 지표가
# 없어 고아 bound 로 앞 지표에 병합되므로 범위가 안 깨진다.
# 동사 연결어미(구매'했고'/'하고', 받'았고'/넘'었고')도 절 경계다 — 이게 없으면 '5회 이상 구매했고 구매금액이
# 500,000원 이상'이 한 절로 뭉쳐 같은 방향 임계 둘이 첫 값(5) 하나로 붕괴하고 500,000·주문수가 소실된다.
_AGG_CLAUSE_SPLIT_RE = re.compile(r"이지만|하지만|지만|반면에|반면|그리고|이면서|면서|동시에|이고|이며|했고|았고|었고|하고|또는|(?<!\d),|,(?!\d)")


def _clause_scoped_window(query: str, start: int, length: int = 50) -> str:
    """지표 동의어 뒤 비교어를 읽을 window 를 다음 절 경계(_AGG_CLAUSE_SPLIT_RE)에서 끊는다. 고정 길이
    window 는 '로그인 횟수가 100회 이상이지만 구매 횟수는 1회 이하'에서 옆 절의 '1회 이하'까지 삼켜
    한 지표에 모순 임계(>=100 AND <=1)를 붙이거나, '평균' 같은 표지가 끼어들어 조건이 통째로 드롭됐다.

    단, 경계 뒤가 곧바로 숫자면(‘30회 이상이지만 100회 미만’) 같은 지표의 이중경계 연속이므로 끊지 않는다 —
    지표 명사로 시작하는 진짜 다음 조건(‘구매 횟수는 1회 이하’)에서만 끊는다."""
    window = query[start:start + length]
    for boundary in _AGG_CLAUSE_SPLIT_RE.finditer(window):
        if window[boundary.end():].lstrip()[:1].isdigit():
            continue  # 숫자로 시작 = 같은 지표의 상·하한 연속(이중경계) → 유지
        return window[:boundary.start()]
    return window


# 도메인 문맥: 구매/상품/결제/할인 등이 있어야 집계 지표 후보로 본다('2회 방문'·'자녀 2명'은 제외).
_AGG_DOMAIN_CONTEXT_RE = re.compile(r"구매|구입|주문|샀|상품|제품|품목|결제|할인|수량|종류|객단가|매출|구매액|금액|건수|종수")
# 누적/평생 표지: 이 절의 집계는 전 생애(창 없음)로 본다 — 옆 절의 최근성 창('최근 180일 무주문')이 '누적
# 구매액'에 새어 들어와 '최근 180일 구매 100만↑ AND 최근 180일 무주문'(공집합)이 되는 걸 막는다.
_CUMULATIVE_WINDOW_MARKER_RE = re.compile(r"누적|누계|평생|통산|역대|전체\s*기간")
# 임계값 단위 추출(숫자 뒤 단위, 긴 단위 우선). 상품 수량/종류 단위 포함.
_AGG_UNIT_TOKEN_RE = re.compile(r"\d[\d,]*\s*(?:억|천만|백만|만|천)?\s*(종류|종수|품목|가지|건수|회수|종|개|건|회|번|원|점|장)")
# 집계 범위(grain): 한 주문 내 / 동일 상품별 / 회원 누적.
_AGG_SCOPE_PER_ORDER_RE = re.compile(r"한\s*주문|한\s*번에|한번에|주문당|주문\s*당|주문별|주문\s*별|1회\s*주문")
_AGG_SCOPE_PER_PRODUCT_RE = re.compile(r"동일\s*상품|같은\s*상품|동일한\s*상품|상품별|상품\s*별|동일\s*제품|같은\s*제품")
# 범위(scope) 필터: 브랜드/카테고리. '특정/어떤/모든' 등은 값 미지정 자리표시자다.
_BRAND_SCOPE_RE = re.compile(r"(?P<val>[가-힣A-Za-z0-9]+)\s*브랜드")
_CATEGORY_SCOPE_RE = re.compile(r"(?P<val>[가-힣A-Za-z0-9]+)\s*카테고리")
_SCOPE_PLACEHOLDER_VALUES = {"특정", "어떤", "모든", "해당", "일부", "각", "그", "이", "저", "무슨", "어느", "임의"}


def _clause_primary_unit(clause: str) -> str | None:
    match = _AGG_UNIT_TOKEN_RE.search(clause)
    return match.group(1) if match else None


def _extract_aggregation_scope(clause: str) -> tuple[str, str]:
    """절의 집계 grain(per_member/per_order/per_product)을 판정하고, grain 표지 어구를 절에서 제거한 사본을
    함께 돌려준다. 표지 제거는 '한 번에'가 한글 수사 정규화로 '1번'이 돼 개수 단위로 오인되는 것을 막는다.
    반드시 한글 수사 정규화 전(원문)에 호출한다('한 번에'/'한 주문'을 그대로 봐야 하므로)."""
    for scope_name, pattern in (("per_product", _AGG_SCOPE_PER_PRODUCT_RE), ("per_order", _AGG_SCOPE_PER_ORDER_RE)):
        match = pattern.search(clause)
        if match is not None:
            cleaned = clause[: match.start()] + " " + clause[match.end():]
            return scope_name, cleaned
    return "per_member", clause


def _clause_scope(clause: str) -> dict[str, str]:
    """브랜드/카테고리 범위 필터를 뽑는다(값이 자리표시자 '특정'이면 값 미지정으로 표시)."""
    scope: dict[str, str] = {}
    brand = _BRAND_SCOPE_RE.search(clause)
    if brand is not None:
        scope["brand"] = brand.group("val")
    category = _CATEGORY_SCOPE_RE.search(clause)
    if category is not None:
        scope["category"] = category.group("val")
    return scope


def _metric_match_text(value: str) -> str:
    """Return a stable comparison form for registry-backed metric vocabulary.

    Korean compound nouns commonly vary only by internal spacing (for example,
    ``평균 주문 금액`` / ``평균 주문금액`` / ``평균주문금액``).  Normalizing only
    the comparison text avoids duplicating every spacing variant in the registry
    without changing the original query or any SQL value.  Case-folding also makes
    Latin aliases such as AOV consistent.
    """
    return "".join(value.split()).casefold()


def _score_metric_for_clause(clause: str, metric: dict[str, Any], unit: str | None) -> int:
    """절에 대한 지표 적합도 점수. 정확·긴 별칭(+), 의미 힌트(+), 단위 일치(+)/불일치(-), 의미 충돌(-).
    문장별 하드코딩 대신 스펙(units/hint_terms/anti_hint_terms)만으로 후보를 가른다."""
    score = 0
    compact_clause = _metric_match_text(clause)
    alias = None
    for synonym in metric.get("synonyms", []):
        if not isinstance(synonym, str) or not synonym:
            continue
        compact_synonym = _metric_match_text(synonym)
        if compact_synonym in compact_clause and (alias is None or len(compact_synonym) > len(alias)):
            alias = compact_synonym
    if alias is not None:
        # A full compound metric name must outrank a contained shorter metric
        # even when both share generic hints such as "결제" or "금액".
        score += 80 + len(alias) * 30  # 긴 별칭일수록 우세(포함 관계 최장 일치 구현)
    units = [u for u in metric.get("units", []) if isinstance(u, str)]
    if unit and units:
        score += 40 if unit in units else -80
    # 서비스 기본 해석 정책: 명시 지표어 없이 단위만 있는 표현('20만원 이상 구매')은 메타데이터에서
    # 그 단위의 기본 지표로 선언된 항목을 우선한다. 코드가 SUM/AVG 등을 임의 선택하지 않으며 배포별로
    # default_for_units 만 바꿔 정책을 교체할 수 있다.
    default_units = [u for u in metric.get("default_for_units", []) if isinstance(u, str)]
    if unit and unit in default_units:
        score += 20
    if any(
        isinstance(h, str) and h and _metric_match_text(h) in compact_clause
        for h in metric.get("hint_terms", [])
    ):
        score += 55
    if any(
        isinstance(a, str) and a and _metric_match_text(a) in compact_clause
        for a in metric.get("anti_hint_terms", [])
    ):
        score -= 100
    return score


def _resolve_clause_metric(clause: str, metrics: dict[str, Any], unit: str | None) -> tuple[str | None, str]:
    """절의 지표를 점수로 확정한다. 반환: (metric_id, status) — status ∈ ok/ambiguous/unresolved/none.
    도메인 문맥이 없으면 none(우리 집계 대상 아님), 문맥은 있으나 후보 없음/동점이면 unresolved/ambiguous
    (조용한 폴백 금지 — 호출부가 clarification)."""
    if not _AGG_DOMAIN_CONTEXT_RE.search(clause):
        return None, "none"
    scored = [(mid, _score_metric_for_clause(clause, metric, unit)) for mid, metric in metrics.items()]
    positive = sorted([(mid, s) for mid, s in scored if s > 0], key=lambda x: x[1], reverse=True)
    if not positive:
        return None, "unresolved"
    if len(positive) > 1 and positive[1][1] >= positive[0][1] - 15:
        return None, "ambiguous"
    return positive[0][0], "ok"


# 집계 창(최근성) 앵커: 이 도메인어 근처(_DURATION_ANCHOR_GAP)의 기간만 그 절의 집계 창으로 귀속한다 —
# 옆 조건(로그인/미접속 등)의 창이 구매/주문 집계로 새는 것을 막는다(전역 first-match 대신 앵커 게이트).
_AGG_WINDOW_ANCHOR_TERMS = (
    "구매", "구입", "주문", "결제", "구매액", "매출", "객단가", "금액", "건수",
    "상품", "제품", "품목", "수량", "종류", "할인", "샀",
)


def _aggregate_clause_time_scope(
    raw_clause: str, inactivity_days_set: frozenset[int],
) -> tuple[int | None, str | None, bool]:
    """한 절의 집계 시간 스코프를 그 절 텍스트에서만 귀속한다 — (window_days|None, calendar_period|None, is_lifetime).

    우선순위:
      (1) 절 안의 명시 창(집계 도메인 앵커 근처의 '최근 N일/개월') → 롤링 창. 단 그 절에 '누적' 표지가 있고
          추출 창이 미구매/미접속 기간(inactivity)과 같으면, 그 창은 옆 무주문 조건 것이지 이 누적 지표의
          창이 아니다 → lifetime('최근 90일 누적' 처럼 지표에 직접 붙은 창은 inactivity 와 무관해 롤링 유지).
      (2) '누적/평생/통산/과거 누적' 표지(명시 창 없음) → lifetime(None).
      (3) 달력 구간(올해/지난달) → calendar_period.
    전역 first-match 를 쓰지 않아 옆 절(로그인/미접속)의 창이 이 절로 새지 않는다.
    is_lifetime=True 는 '전 기간(창 없음)'을 확정한 것이라 상위 루프가 앞 절 공유 창을 상속하지 않게 한다."""
    cumulative = _CUMULATIVE_WINDOW_MARKER_RE.search(raw_clause) is not None
    window = _parse_duration_window(raw_clause, anchor_terms=_AGG_WINDOW_ANCHOR_TERMS)
    if window is not None:
        if cumulative and window["min_days"] in inactivity_days_set:
            return None, None, True  # 창이 옆 무주문 조건 것 → 누적 지표는 lifetime
        return window["min_days"], None, False
    if cumulative:
        return None, None, True
    return None, _parse_calendar_period(raw_clause), False


def _make_aggregate_condition(
    context: dict[str, Any], operator: str, threshold: float, window_days: Any,
    calendar_period: str | None, metrics: dict[str, Any],
) -> dict[str, Any]:
    metric = metrics.get(context["metric_id"], {})
    condition: dict[str, Any] = {
        "metric_id": context["metric_id"],
        "operator": operator,
        "threshold": threshold,
        "window_days": window_days,
        "label": metric.get("ko_label", context["metric_id"]),
    }
    # per_member 가 아니면(주문별/상품별)만 표식으로 붙인다(기본값이면 조건 dict 형태 불변 — 회귀 안전).
    scope_grain = context.get("aggregation_scope", "per_member")
    if scope_grain != "per_member":
        condition["aggregation_scope"] = scope_grain
    if context.get("scope"):
        condition["scope"] = dict(context["scope"])
    if calendar_period:
        condition["calendar_period"] = calendar_period
    return condition


def _aggregate_condition_conflict(conditions: list[dict[str, Any]]) -> str | None:
    """같은 지표(+grain)에 불가능한 범위(하한>상한)가 생성됐으면 그 라벨 반환(절 과포획 의심). 정상 범위
    (>=lo, <=hi, lo<=hi)는 상충이 아니다. 없으면 None."""
    by_key: dict[tuple, list[dict[str, Any]]] = {}
    for condition in conditions:
        key = (condition["metric_id"], condition.get("aggregation_scope", "per_member"))
        by_key.setdefault(key, []).append(condition)
    for group in by_key.values():
        lowers = [c["threshold"] for c in group if c["operator"] in (">", ">=")]
        uppers = [c["threshold"] for c in group if c["operator"] in ("<", "<=")]
        if lowers and uppers and max(lowers) > min(uppers):
            return group[0].get("label", group[0]["metric_id"])
    return None


def _apply_aggregate_condition_filter(query: str, plan: dict[str, Any]) -> None:
    """상품/주문 집계 조건을 스펙 기반 점수화로 해석한다(주문 건수·상품 수량·서로 다른 상품 수·구매/할인
    금액·평균 주문 금액을 지표 스펙과 단위/의미 힌트로 구분).

    절 단위로 나눠(고아 bound 는 앞 지표 범위로 병합) 각 절에서 {metric, operator, value, aggregation_scope,
    scope} 를 뽑는다. '개'는 상품 수량, '종/종류'는 서로 다른 상품 수로 라우팅되며 order_count 로 조용히
    폴백하지 않는다. 도메인 문맥이 있으나 지표를 확정 못 하면 clarification(metric_not_resolved) 을 반환한다.
    브랜드/카테고리는 metric 이 아니라 scope 로 분리하며, 값이 '특정'(자리표시자)이면 clarification 한다."""
    config = _aggregate_targets_config()
    metrics = config.get("metrics", {})
    if not isinstance(metrics, dict) or not metrics:
        return
    # 옆 무주문/미접속 조건의 창(min_days) — '누적' 절이 그 창을 지표 창으로 오상속하는 걸 막는 판정에 쓴다.
    inactivity_days_set = frozenset(
        d for d in (
            (plan.get("target_user", {}).get("purchase_inactivity") or {}).get("min_days"),
            (plan.get("target_user", {}).get("inactivity_period") or {}).get("min_days"),
        ) if isinstance(d, int)
    )

    conditions: list[dict[str, Any]] = []
    last_context: dict[str, Any] | None = None
    last_window: int | None = None       # 앞 aggregate 지표(절)의 유효 창 — 공유 창/고아 bound 상속용
    last_calendar: str | None = None
    scope_clarify_key: str | None = None
    # 원문(정규화 전)으로 절 분리·grain/scope 판정 — '한 번에'가 '1번'으로 바뀌기 전에 봐야 한다.
    for raw_clause in _AGG_CLAUSE_SPLIT_RE.split(query):
        aggregation_scope, scoped_clause = _extract_aggregation_scope(raw_clause)
        scope = _clause_scope(raw_clause)
        # grain 표지를 뗀 뒤 한글 수사 정규화('두 번'→'2번') → 임계값/단위/지표 해석.
        clause = _normalize_sino_korean_amounts(_normalize_korean_count_numerals(scoped_clause))
        # 시간 창은 이 절 텍스트에서만 귀속한다(전역 first-match 금지) — 옆 절(로그인/미접속)의 창 누수·
        # 누적 절의 롤링 창 오상속을 원천 차단한다([[numeric-metric-unit-and-ratio]] 창 게이트와 동일 원칙).
        clause_window, clause_calendar, clause_lifetime = _aggregate_clause_time_scope(raw_clause, inactivity_days_set)
        comparisons = _parse_amount_comparison(clause, _AGG_UNIT, bare_equals=False)
        if not comparisons:
            continue
        unit = _clause_primary_unit(clause)
        metric_id, status = _resolve_clause_metric(clause, metrics, unit)
        # 유효 창: 절 자체 창 > (lifetime 확정이면 창 없음) > 앞 aggregate 지표의 공유 창 상속.
        # 공유 창('최근 90일 동안 A이고 B이며 C')은 같은 문장의 연속 집계 지표에만 흐르고, lifetime(누적)
        # 절이나 로그인 등 비집계 절(last_window 를 세팅하지 않음)에서는 상속되지 않는다(도메인 간 누수 차단).
        if clause_window is not None:
            effective_window, effective_calendar = clause_window, clause_calendar
        elif clause_lifetime:
            effective_window, effective_calendar = None, None
        else:
            effective_window = last_window
            effective_calendar = clause_calendar if clause_calendar is not None else last_calendar
        if metric_id is None:
            # 지표어 없는 뒷 절(범위 연속) → 앞 지표에 병합(고아 bound). 앞 지표가 없으면:
            #  - none(도메인 아님): 무시. - unresolved/ambiguous(도메인 문맥 있음): clarification.
            if last_context is not None:
                for operator, threshold in comparisons:
                    conditions.append(_make_aggregate_condition(last_context, operator, threshold, effective_window, effective_calendar, metrics))
                continue
            if status in ("unresolved", "ambiguous"):
                plan["unsupported"] = {
                    "reason": "metric_not_resolved",
                    "message": "상품/주문 조건의 지표를 확정할 수 없습니다(수량/종류/횟수/금액 등).",
                    "clarification": "상품 개수는 총수량을 의미하나요, 서로 다른 상품 종류 수를 의미하나요? 또는 주문 건수/금액 중 무엇인가요?",
                }
                return
            continue
        for key, value in list(scope.items()):
            if value in _SCOPE_PLACEHOLDER_VALUES:
                scope_clarify_key = key  # 값 미지정('특정 브랜드')이라 필터로 못 씀 → 뒤에서 clarification
                scope.pop(key)
        context = {"metric_id": metric_id, "aggregation_scope": aggregation_scope, "scope": scope}
        last_context = context
        last_window = effective_window
        last_calendar = effective_calendar
        for operator, threshold in comparisons:
            conditions.append(_make_aggregate_condition(context, operator, threshold, effective_window, effective_calendar, metrics))

    if not conditions:
        return
    # 범위 값 미지정('특정 브랜드/카테고리') → 조용히 전체 집계로 폴백하지 않고 명시 clarification.
    if scope_clarify_key is not None:
        label = {"brand": "브랜드", "category": "카테고리"}.get(scope_clarify_key, scope_clarify_key)
        plan["unsupported"] = {
            "reason": "scope_value_unspecified",
            "message": f"'특정 {label}'의 구체적인 {label} 값이 지정되지 않았습니다.",
            "clarification": f"어느 {label}를 기준으로 할까요? (예: 특정 브랜드명/카테고리명 지정)",
            "scope": scope_clarify_key,
        }
        return
    conflict_label = _aggregate_condition_conflict(conditions)
    if conflict_label is not None:
        plan["unsupported"] = {
            "reason": "conflicting_aggregate_conditions",
            "message": f"'{conflict_label}' 지표에 서로 모순되는 임계값(하한>상한)이 생성됐습니다 — 절 경계 과포획 의심.",
            "clarification": "한 지표에 상충하는 임계값이 감지됐습니다. 조건을 절별로 명확히 나눠 다시 입력해 주시겠어요?",
        }
        return

    plan.setdefault("target_user", {})["aggregate_conditions"] = conditions


# 지표 명사('구매 횟수') 없이 구매 동사에 바로 붙는 개수 임계값("2개/3번/2회/2건 이상 구매/구입").
# 지표 동의어가 없어 _apply_aggregate_condition_filter(지표명이 있어야 발동)가 못 잡는 간극을 메운다 —
# 주문 건수(order_count) 지표로 컴파일해 회원별 COUNT(DISTINCT ORDER_ID) 임계값이 된다. 개수 단위
# (개/번/회/건)만 봐서 금액(원)·연령(세)·기간(개월)과 갈린다('3개월'은 '개' 뒤가 '월'이라 매칭 안 됨).
# 개수 단위(개/번/회/건)를 필수로 요구해 금액(원)·연령(세)·기간(개월)과 갈린다. 연산자는 공용 어휘를 써서
# 부사형·동사형·'보다 많은'을 함께 잡는다('3회보다 많이 구매' 등). 방향 판정은 _comparison_operator 로 단일화.
_PURCHASE_COUNT_THRESHOLD_PATTERN = re.compile(rf"(?P<num>\d+)\s*(?:개|번|회|건)\s*(?:을|를|이|가)?\s*(?P<op>{_COMPARISON_OP_ALT})")
# 개수 임계값을 구매 조건으로 확정할 구매 동사 표지. 장바구니/반응 문맥은 각 전용 트랙에 양보한다.
_PURCHASE_COUNT_VERB_SIGNS = ("구매", "구입", "주문", "샀")
_PURCHASE_COUNT_CONTEXT_YIELDS = ("장바구니", "카트", "반응")


def _apply_purchase_count_threshold_filter(query: str, plan: dict[str, Any]) -> None:
    """(비활성) 지표명 없는 개수 임계값('N개/번/회/건 이상 구매')은 이제 통합 리졸버
    _apply_aggregate_condition_filter 가 단위/의미 힌트 점수로 처리한다 — '개'는 상품 수량,
    '회/번/건'은 주문 건수로 갈린다. 예전엔 여기서 전부 order_count 로 뭉쳐 '상품 5개'가 주문 5건으로
    오해석됐다. 레지스트리 배선 호환을 위해 함수는 남기되 no-op 로 둔다(이중 추가 방지)."""
    return


# 장바구니 개수/수량 임계값 단위: "N개 이상", "종류 3종 이상", "정확히 3개", "2개에서 5개 사이". 비교 자체
# (이상/초과/미만/정확값/범위)는 공용 _parse_amount_comparison 에 위임한다([[shared-comparison-grammar]]) —
# 개수 단위만 넘겨 돈(원)·연령(세)·기간(개월)과 갈린다. '건'은 주문 건수와 겹쳐 빼고, '종/종수'를 넣어
# '3종 이상'(상품 종류 수)을 카트 종류 수로 잡는다.
_CART_COUNT_UNIT = r"개|종류|종수|종|가지|품목|점"
# 장바구니 금액 임계값: "장바구니에 10만원 이상". 단위(원)로 개수 패턴과 갈리고, 배수어(만/천만)는
# 누적 구매 금액과 같은 파서(_parse_korean_amount)를 쓴다.
_CART_AMOUNT = _compile_threshold(_ThresholdSpec("korean_amount", r"원"))
_CART_AMOUNT_PATTERN = _CART_AMOUNT.pattern
# 금액 표현 앞쪽에서 장바구니 어휘를 찾는 창(공백 제거 기준). 창을 두는 이유는 "장바구니에 담은 고객 중
# 구매 금액 10만원 이상"처럼 한 문장에 장바구니와 '누적 구매 금액'이 같이 오는 경우 때문이다 — 금액이
# 장바구니에 붙어 있을 때만 카트 금액으로 본다.
_CART_AMOUNT_WINDOW = 24
# 금액 바로 앞이 구매/결제/주문이면 카트 금액이 아니라 누적 구매 금액이다(_apply_aggregate_condition_filter 담당).
_CART_AMOUNT_PURCHASE_WORDS = ("구매", "결제", "주문", "누적")
# 동일 상품 복수 담기: "장바구니에 동일 상품을 여러 개 담은". 한 라인의 담은 수량(QTY)이 임계값 이상인
# 장바구니를 뜻하므로 MAX(QTY) 로 판정한다 — SUM(QTY)은 서로 다른 상품을 하나씩 담아도 커져서 '동일 상품'이
# 아니고, COUNT(라인)는 상품 종류 수라 역시 다르다.
_CART_SAME_PRODUCT_PATTERN = re.compile(r"(동일|같은|똑같은)(상품|제품|품목|것)")
# 수량이 숫자로 안 나오는 표현('여러 개', '복수'). 이때 '여럿'의 하한은 2다.
_CART_MULTIPLE_WORDS = ("여러", "복수", "중복", "2개이상", "두개이상")
_CART_MULTIPLE_DEFAULT_THRESHOLD = 2


def _cart_comparison_condition(metric: str, query: str, unit: str = _CART_COUNT_UNIT) -> dict[str, Any] | None:
    """개수 단위 뒤 수치 비교(이상/초과/미만/정확값/범위)를 공용 문법으로 파싱해 cart_aggregate 조건으로
    만든다(없으면 None). 단일 비교는 기존 형태 {metric, operator, threshold} 그대로 두고, 범위/이중경계
    ('2개에서 5개 사이')일 때만 comparisons=[[op,val],...] 를 추가로 실어 빌더가 HAVING 을 AND 로 잇게 한다.
    개수라 값은 정수로 정규화한다(3.0→3). 단위(개/종…)는 필수다 — 카트 질의에 섞인 '30~49세'·'6개월'
    같은 단위 없는 숫자·범위를 흡수하지 않게 한다. unit 을 좁히면 특정 지표('종'→종류 수, '개'→총 수량)만 딴다."""
    comparisons = _parse_amount_comparison(query, unit, bare_equals=False, unit_required=True)
    if not comparisons:
        return None
    normalized = [(op, int(val) if float(val).is_integer() else val) for op, val in comparisons]
    op0, th0 = normalized[0]
    condition: dict[str, Any] = {"metric": metric, "operator": op0, "threshold": th0}
    if len(normalized) > 1:
        condition["comparisons"] = [[op, val] for op, val in normalized]
    return condition


def _cart_same_product_condition(query: str, compact: str) -> dict[str, Any] | None:
    """'장바구니에 동일 상품을 여러 개 담은'을 라인 수량 임계값으로 해석한다(없으면 None).

    '여러 개'처럼 숫자가 없으면 하한 2로 본다. '같은 상품 3개 이상'처럼 숫자가 붙으면 그 값을 쓴다.
    개수 패턴을 그대로 쓰면 '3개 이상 담은'(상품 종류 수)과 구별이 안 되므로, 동일상품 표현이 있을
    때만 라인 수량(MAX QTY) 지표로 돌린다."""
    if _CART_SAME_PRODUCT_PATTERN.search(compact) is None:
        return None
    condition = _cart_comparison_condition("cart_same_product_quantity", query)
    if condition is not None:
        return condition
    if any(word in compact for word in _CART_MULTIPLE_WORDS):
        return {
            "metric": "cart_same_product_quantity",
            "operator": ">=",
            "threshold": _CART_MULTIPLE_DEFAULT_THRESHOLD,
        }
    return None  # '동일 상품'만 있고 수량 표현이 없으면 임계값을 지어내지 않는다.


def _cart_amount_condition(compact: str) -> dict[str, Any] | None:
    """공백 제거 텍스트에서 장바구니 금액 임계값('장바구니에 10만원 이상')을 찾는다(없으면 None).

    금액이 장바구니 어휘 근처에 있을 때만 인정한다 — 창 없이 잡으면 "장바구니에 담은 고객 중 구매 금액
    10만원 이상"의 누적 구매 금액까지 카트 금액으로 오인한다. 같은 이유로 금액 바로 앞이 구매/결제면
    넘긴다(그건 aggregate_conditions 담당이고, 여기서 채가면 지표가 조용히 바뀐다)."""
    cart_positions = [
        match.start()
        for term in _lexicon_terms("cart_terms")
        for match in re.finditer(re.escape(term), compact)
    ]
    if not cart_positions:
        return None
    for match in _CART_AMOUNT_PATTERN.finditer(compact):
        start = match.start()
        if not any(0 <= start - position <= _CART_AMOUNT_WINDOW for position in cart_positions):
            continue
        preceding = compact[max(0, start - 6): start]
        if any(word in preceding for word in _CART_AMOUNT_PURCHASE_WORDS):
            continue
        parsed = _CART_AMOUNT.parse(match)
        if parsed is None:
            continue
        operator, threshold = parsed
        return {"metric": "cart_amount", "operator": operator, "threshold": threshold}
    return None


# 종류 수(distinct) 단위와 총 수량 단위 구분: '종/종류/종수/가지/품목'=상품 종류 수(COUNT DISTINCT),
# '개/점'=낱개. '총 N개'·'수량' 신호가 붙은 낱개만 총 수량(SUM QTY)으로 본다(맨 '개'는 종류 수 기본 유지).
_CART_KIND_UNIT = r"종류|종수|종|가지|품목"
_CART_QTY_UNIT = r"개|점"
_CART_TOTAL_QTY_SIGNAL = re.compile(r"수량|총\s*개수|총\s*\d+\s*개|총\s*\d")


def _cart_count_quantity_conditions(clause: str) -> list[dict[str, Any]]:
    """한 절에서 상품 '종류 수'(cart_line_count)와 '총 수량'(cart_quantity) 조건을 각각 뽑는다.

    - '종/종류/가지/품목' 단위 → cart_line_count(COUNT DISTINCT CART_PRODUCT_NO).
    - '총 N개'·'수량' 신호가 있는 '개/점' → cart_quantity(SUM QTY).
    - 그 외 맨 '개/점'(총/수량 신호 없음) → cart_line_count(기존 기본: '3개 이상 담은'=담은 상품 종류 수).
    둘 다 있으면('3종 이상 총 5개 이상') 두 조건을 함께 돌려 빌더가 HAVING 을 AND 로 합성한다."""
    conditions: list[dict[str, Any]] = []
    kind = _cart_comparison_condition("cart_line_count", clause, unit=_CART_KIND_UNIT)
    if kind is not None:
        conditions.append(kind)
    if _CART_TOTAL_QTY_SIGNAL.search(clause):
        quantity = _cart_comparison_condition("cart_quantity", clause, unit=_CART_QTY_UNIT)
        if quantity is not None:
            conditions.append(quantity)
    if not conditions:
        # 총/수량/종류 신호 없는 맨 개수('3개 이상 담은') → 담은 상품 종류 수(기존 기본 동작 유지).
        plain = _cart_comparison_condition("cart_line_count", clause)
        if plain is not None:
            conditions.append(plain)
    return conditions


# 카트 집계 지표 ↔ 같은 뜻의 일반 주문/상품 집계 지표(쌍둥이). 카트 문맥의 임계값은 두 파서가 각각
# 청구한다 — '장바구니 총금액 5만원 이상'을 카트는 cart_amount 로, 일반 집계는 purchase_amount 로 잡는다.
# 카트 파서는 금액 앞이 구매/결제면 양보하지만(_cart_amount_condition) 반대 방향 가드가 없어, 카트가
# 소유한 조건의 사본이 aggregate_conditions 에 남는다. 그 사본은 카트 빌더가 컴파일할 수 없어
# dropped_conditions 로 남고, 커버리지 게이트가 정상 SQL 을 통째로 버린다(query_plan_conditions_missing).
# 지표 대응표를 선언해 '카트가 소유자'임을 한 곳에서 못 박는다(금액/수량/종류 전 지표 공통).
_CART_AGGREGATE_TWIN_METRICS: dict[str, frozenset[str]] = {
    "cart_amount": frozenset({"purchase_amount"}),
    "cart_quantity": frozenset({"total_item_quantity"}),
    "cart_line_count": frozenset({"distinct_product_count"}),
    "cart_same_product_quantity": frozenset({"total_item_quantity"}),
}


def _cart_condition_comparisons(condition: dict[str, Any]) -> list[tuple[str, float]]:
    """카트 집계 조건의 비교쌍 목록. 범위형은 comparisons, 단일형은 (operator, threshold)."""
    raw = condition.get("comparisons") or [[condition.get("operator"), condition.get("threshold")]]
    return [
        (operator, float(threshold))
        for operator, threshold in raw
        if isinstance(operator, str) and isinstance(threshold, (int, float))
    ]


def _release_cart_twin_aggregates(plan: dict[str, Any], conditions: list[dict[str, Any]]) -> None:
    """카트가 소유한 임계값을 일반 집계가 이중 청구한 사본을 aggregate_conditions 에서 걷어낸다.

    캠페인 귀속 금액/건수 파서(_apply_campaign_buy_amount_filter)가 쓰는 '이중 파싱 정리'와 같은 규칙이고,
    카트 파서가 일반 집계 파서보다 뒤에 돌기 때문에 여기서 정리할 수 있다.

    지우는 기준은 '같은 뜻의 지표(쌍둥이) + 같은 연산자 + 같은 임계값' 셋을 모두 만족할 때뿐이다 —
    숫자만 보고 지우면 우연히 같은 숫자를 쓴 다른 조건('3종 담고 3회 구매')까지 조용히 사라진다.
    쌍둥이가 아니면 그대로 남겨 기존처럼 미반영 조건으로 고지되게 둔다(조용한 드롭보다 실패가 낫다)."""
    aggregates = plan.get("target_user", {}).get("aggregate_conditions")
    if not isinstance(aggregates, list) or not aggregates:
        return
    claimed = {
        (twin, operator, threshold)
        for condition in conditions
        for twin in _CART_AGGREGATE_TWIN_METRICS.get(condition.get("metric"), frozenset())
        for operator, threshold in _cart_condition_comparisons(condition)
    }
    if not claimed:
        return
    plan["target_user"]["aggregate_conditions"] = [
        condition
        for condition in aggregates
        if not (
            isinstance(condition, dict)
            and isinstance(condition.get("threshold"), (int, float))
            and (condition.get("metric_id"), condition.get("operator"), float(condition["threshold"])) in claimed
        )
    ]


def _set_cart_aggregate(plan: dict[str, Any], conditions: list[dict[str, Any]]) -> None:
    """카트 집계 조건을 세운다(단일은 dict, 여럿이면 list — 기존 형태 유지).

    세우는 즉시 일반 집계 쪽 사본을 걷어내 임계값 소유권을 카트로 단일화한다."""
    plan.setdefault("target_user", {})["cart_aggregate"] = conditions[0] if len(conditions) == 1 else conditions
    _release_cart_twin_aggregates(plan, conditions)


def _apply_cart_aggregate_condition_filter(query: str, plan: dict[str, Any]) -> None:
    """'장바구니에 N개 이상 담은'을 장바구니 집계 조건(cart_aggregate)으로 해석한다.

    build_cart_aggregate_targets_sql_candidate 가 ODS_MALL_OMS_CART 를 회원별로 집계한 서브쿼리
    (GROUP BY CART_ID HAVING COUNT(DISTINCT CART_PRODUCT_NO) op N)로 컴파일한다. '수량/총 개수' 문맥이면
    담은 총 수량(SUM QTY — QTY 가 '담은 수량', SET_QTY 는 '세트 수량'이라 다르다), 금액(원)이면 담은 금액 합
    (SUM TOTAL_SALE_PRICE), 아니면 담은 상품 종류 수(COUNT DISTINCT 라인)로 본다. 비교는 공용 문법에 위임해
    이상/초과/미만/정확값/범위를 모두 처리한다. 장바구니 어휘가 있을 때만 발동해 일반 개수 표현('3개 이상
    구매' 등)과 섞이지 않게 한다 — 파싱에 실패해도 여기서 멈춰, 카트 질의가 조용히 주문 집계로 새지 않게 한다."""
    compact = query.replace(" ", "")
    if not any(term in compact for term in _lexicon_terms("cart_terms")):
        return
    # '장바구니 수량이 입력되지 않은/미입력'(QTY IS NULL) — 값 자체가 미기입. '수량 0개'(=0, HAVING)와 달리
    # 집계 임계로 표현할 수 없어(EXISTS QTY IS NULL) 전용 플래그로 승격한다. 수량 문맥일 때만 발동한다.
    if _DATA_MISSING_PATTERN.search(compact) and re.search(r"수량|개수", compact):
        plan.setdefault("target_user", {})["cart_quantity_missing"] = True
        return
    same_product = _cart_same_product_condition(query, compact)
    if same_product is not None:
        _set_cart_aggregate(plan, [same_product])
        return
    amount = _cart_amount_condition(compact)
    if amount is not None:
        _set_cart_aggregate(plan, [amount])
        return
    # 개수/수량 임계 — 절별로 나눠 여러 카트 조건('총수량 10개 이상이고 종류 3종 이상')을 함께 잡는다.
    # 카트 어휘가 있는 절만 본다(첫 절 이후 일반 개수 표현이 카트로 새지 않게).
    cart_terms = _lexicon_terms("cart_terms")
    conditions: list[dict[str, Any]] = []
    for clause in _AGG_CLAUSE_SPLIT_RE.split(query):
        if not any(term in clause.replace(" ", "") for term in cart_terms):
            continue
        conditions.extend(_cart_count_quantity_conditions(clause))
    if not conditions:
        return
    _set_cart_aggregate(plan, conditions)


# 한글 기간 단위 → 캐노니컬 영문 단위(슬롯 정규화용). 일수 환산은 targeting_ir.UNIT_DAYS 가 소유한다.
_KO_UNIT_TO_CANON = {"일": "days", "주": "weeks", "주일": "weeks", "개월": "months", "달": "months", "년": "years"}
# 기간 표현 → 일수. 숫자형('7일', '2주')과 숫자 없는 한글 단어형('일주일', '보름', '한 달')을 모두 본다.
# 단어형은 숫자가 없어서 재작성 가드의 숫자 서명에도, 기존 '최근 N일' 파서에도 안 잡혔다.
# 한글토큰→일수는 토큰→canonical(_KO_UNIT_TO_CANON)과 canonical→일수(targeting_ir.UNIT_DAYS)의 합성으로
# 파생한다 — 별도 한글 일수표를 두지 않아, 새 단위는 _KO_UNIT_TO_CANON(+targeting_ir.UNIT_DAYS)만 고치면 된다.
_DURATION_UNIT_DAYS = {ko: targeting_ir.UNIT_DAYS[canon] for ko, canon in _KO_UNIT_TO_CANON.items()}
_NUMERIC_DURATION_PATTERN = re.compile(r"(?P<num>\d+)\s*(?P<unit>주일|개월|일|주|달|년)")
_WORD_DURATION_DAYS = {
    "일주일": 7, "한주일": 7, "한주": 7, "일주": 7,
    "이주일": 14, "두주일": 14, "두주": 14,
    "삼주일": 21, "세주일": 21, "세주": 21,
    "보름": 15,
    "한달": 30, "한개월": 30,
    "두달": 60, "두개월": 60,
    "석달": 90, "세달": 90, "세개월": 90,
    "반년": 180, "일년": 365, "한해": 365, "한햇": 365,
}


def _duration_days_signals(text: str) -> set[int]:
    """텍스트에 나온 모든 기간 표현을 일수 집합으로 돌려준다('일주일'과 '7일'은 같은 7로 정규화).

    재작성 가드가 '일주일 이상 유지' 같은 기간 조건 소실을 잡을 때 쓴다. 숫자 서명만으로는
    숫자 없는 단어형('일주일')이 사라져도 알 수 없었다."""
    return {days for _, _, days in _duration_matches((text or "").replace(" ", ""))}


_WORD_DURATION_PATTERN = re.compile("|".join(sorted(map(re.escape, _WORD_DURATION_DAYS), key=len, reverse=True)))


def _duration_matches(compact: str) -> list[tuple[int, int, int]]:
    """공백 제거 텍스트에서 (시작, 끝, 일수) 목록을 위치와 함께 돌려준다(등장 순)."""
    found = [
        (match.start(), match.end(), int(match.group("num")) * _DURATION_UNIT_DAYS[match.group("unit")])
        for match in _NUMERIC_DURATION_PATTERN.finditer(compact)
        if int(match.group("num")) > 0
        and not (match.group("unit") == "년" and 1900 <= int(match.group("num")) <= 2199)
    ]
    found += [
        (match.start(), match.end(), _WORD_DURATION_DAYS[match.group(0)])
        for match in _WORD_DURATION_PATTERN.finditer(compact)
    ]
    return sorted(found)


# 최근성 표지(기간 숫자 없는 '최근 로그인'류에 기본 창을 줄지 판정). 레지스트리 recently.synonyms 미러.
_RECENCY_MARKERS = ("최근", "요즘", "근래", "최근에")


def _recently_default_days() -> int:
    """숫자 없는 '최근' 창의 기본 일수(date_expression_registry.recently.default_days, 기본 30)."""
    registry = _MEMBER_TARGET_FILTERS.get("date_expression_registry", {})
    recently = registry.get("relative_terms", {}).get("recently", {}) if isinstance(registry, dict) else {}
    days = recently.get("default_days") if isinstance(recently, dict) else None
    return days if isinstance(days, int) and days > 0 else 30


def _duration_window_candidates(compact: str) -> list[tuple[int, int, int, str]]:
    """공백 제거 텍스트의 기간 표현을 (시작, 끝, value, canonical_unit) 목록으로(등장 순). 단어형은 unit=days."""
    out: list[tuple[int, int, int, str]] = []
    for match in _NUMERIC_DURATION_PATTERN.finditer(compact):
        value = int(match.group("num"))
        # 2019년/2026년은 달력 연도이지 2019년 길이의 롤링 창이 아니다. 이를 기간으로 잡으면
        # DATEADD(DAY, -736935, ...) 같은 비정상 조건이 절대 날짜 범위와 함께 생성된다.
        if value > 0 and not (match.group("unit") == "년" and 1900 <= value <= 2199):
            out.append((match.start(), match.end(), value, _KO_UNIT_TO_CANON.get(match.group("unit"), "days")))
    for match in _WORD_DURATION_PATTERN.finditer(compact):
        out.append((match.start(), match.end(), _WORD_DURATION_DAYS[match.group(0)], "days"))
    return sorted(out)


# 앵커어와 기간 표현 사이 허용 간격(공백 제거 기준). '6개월동안로그인'(동안=2), '1년이내가입'(이내=2)은
# 붙은 것으로 보고, 프롬프트 반대편의 다른 조건 창은 배제한다.
_DURATION_ANCHOR_GAP = 8


def _parse_duration_window(
    query: str,
    *,
    require_number: bool = True,
    default_days: int | None = None,
    exclude_past: bool = False,
    anchor_terms: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    """통합 기간 창 파서 — 숫자형(3개월/2주/1년)·단어형(일주일/반년/한달)을 모두 잡아 정규 shape로 돌려준다.

    반환 {value, unit(∈days/weeks/months/years), min_days}. 파편화된 슬롯별 창 파서(가입/로그인/미구매/
    미접속)가 각자 다른 단위 부분집합만 지원해 '1년 이내 가입'·'반년 미구매' 같은 표현을 놓치던 것을
    한 곳으로 모은다. 문맥 게이트(가입 신호/로그인 신호/부정어)는 호출자가 유지한다.

    anchor_terms 를 주면 그 앵커어 근처(±_DURATION_ANCHOR_GAP)의 기간만 본다 — 여러 조건이 각자 창을
    가진 프롬프트('최근 1년 이내 가입 … 최근 로그인')에서 로그인 창이 가입의 '1년'을 훔쳐가는 조건 간
    창 충돌을 막는다(앵커가 하나도 없으면 전체에서 첫 창으로 폴백). exclude_past=True 면 'N개월 전'을 건너뛴다."""
    compact = query.replace(" ", "").casefold()
    candidates = _duration_window_candidates(compact)
    if exclude_past:
        candidates = [c for c in candidates if compact[c[1]:c[1] + 1] != "전"]
    if anchor_terms:
        anchor_spans = [
            (match.start(), match.end())
            for term in anchor_terms
            for match in re.finditer(re.escape(term), compact)
        ]
        if anchor_spans:
            def _near(cand: tuple[int, int, int, str]) -> bool:
                start, end = cand[0], cand[1]
                return any(
                    max(start, a_start) - min(end, a_end) <= _DURATION_ANCHOR_GAP
                    for a_start, a_end in anchor_spans
                )
            candidates = [c for c in candidates if _near(c)]
    if candidates:
        _s, _e, value, unit = candidates[0]
        return {"value": value, "unit": unit, "min_days": value * targeting_ir.UNIT_DAYS[unit]}
    if not require_number and default_days:
        return {"value": default_days, "unit": "days", "min_days": default_days}
    return None


# 장바구니 보관 기간: "장바구니에 담아둔 지 일주일 이상", "일주일 이상 유지/담고 있는". 담은 시점
# 에서 N일이 지나도록 KEEP_YN='Y' 인 회원 = 오래 방치된 장바구니.
# 보관 표현은 어간으로 본다 — 재작성이 표현형을 자주 바꾼다('유지하고'→'담고 있는').
_CART_RETENTION_MARKERS = ("담", "유지", "방치", "넣어", "보관", "남아있", "남겨", "그대로", "묵혀", "미결제")
# 기간이 오디언스 조건이 아니라 혜택/행사 기간을 뜻하는 문맥. 같은 창에 있으면 보관 기간으로 보지 않는다
# (예: '장바구니에 담은 고객에게 7일 이상 유효한 쿠폰').
_CART_RETENTION_BENEFIT_WORDS = ("쿠폰", "할인", "유효", "기한", "배송", "증정", "적립", "이벤트", "행사", "발송")
# 기간의 방향어. '이상/넘게/지난'은 최소 보관 기간, '이내/이하/미만'은 최대 보관 기간.
_CART_RETENTION_MIN_WORDS = ("이상", "넘게", "넘은", "지난", "지났", "째", "동안", "이후")
_CART_RETENTION_MAX_WORDS = ("이내", "이하", "미만", "안에")
# '이상/넘게/지난'은 최신성 표현('최근')과 같이 나와도 하한이 확실하다("최근 3개월 이상 방치된").
# 그 외에는 '최근'이 붙으면 상한으로 본다("최근 7일 동안 담은" = 담은 지 7일 이내).
_CART_RETENTION_STRONG_MIN_WORDS = ("이상", "넘게", "넘은", "지난", "지났")
# 숫자 없는 최신성 표현. "최근 생성된 장바구니가 있는"처럼 기간이 안 붙어도 방향(최근)은 분명하다.
_CART_RECENT_WORDS = ("최근", "새로", "방금", "갓")
# 최신성 표현이 가리키는 '담긴 사건'. 이게 있어야 장바구니가 최근 생긴 것으로 본다.
_CART_RECENT_EVENT_MARKERS = ("생성", "담", "등록", "추가", "만들")
# '최근'과 담김 표현이 이만큼 떨어져 있으면 같은 조건으로 보지 않는다(공백 제거 기준).
_CART_RECENT_WINDOW = 20
# 기간 표현 주변에서 보관 표현·방향어를 찾는 창(공백 제거 기준 글자 수).
_CART_RETENTION_WINDOW = 16
# 기간이 장바구니 어휘에 '붙어 있는' 것으로 볼 거리. '최근 30일 장바구니 총금액'처럼 담기 동사 없이
# 기간이 장바구니를 직접 수식하는 표현은 보관 표지(담/유지/방치)가 하나도 없어 창이 통째로 사라졌다.
# 붙어 있을 때만 인정해, 기간이 옆 조건 것인 경우('최근 30일 구매한 회원 중 장바구니가 있는')는 제외한다.
_CART_DURATION_ADJACENCY = 6
# 방향어를 그 기간에 '붙은' 것만 읽을 거리. 창 전체에서 찾으면 다른 숫자의 비교어를 방향어로 오독한다
# ('최근 30일 장바구니 총금액이 5만원 이상'의 '이상'은 기간이 아니라 금액 것이라 보관 하한이 아니다).
_CART_RETENTION_DIRECTION_GAP = 4
# 구매 미발생 표지: '최근 N일' 뒤에 이게 오면 보관 기간이 아니라 구매 미발생 기간(purchase_inactivity)이다.
_CART_PURCHASE_ABSENCE_RE = re.compile(r"구매하지|구입하지|주문하지|주문이?없|사지않|안\s*샀|미구매")


def _apply_cart_retention_filter(query: str, plan: dict[str, Any]) -> None:
    """'장바구니에 일주일 이상 담아둔/유지 중인 회원'을 장바구니 보관 기간(cart_retention)으로 해석한다.

    KEEP_YN='Y'(보관중)만으로는 "언제 담았든 아직 안 산 회원"이라 기간 조건이 통째로 사라진다.
    담은 시점 컬럼(cart_targets.registered_date_column)과 기준일 차이를 비교해야 '일주일 이상 유지'가
    실제 필터가 된다. 판정은 기간 표현에 붙은 어구를 먼저 보고(방향어·장바구니 어휘의 인접), 없으면 주변
    창으로 넓힌다 — 보관 표현과 방향어가 그 기간에 실제로 붙어 있어야 잡아, '장바구니에 3개 이상 담은'
    (개수 임계값)이나 '담은 고객에게 7일 유효한 쿠폰'(혜택 기간)과 섞이지 않는다. 보관 자체가 미결제 상태이므로 cart_abandoner 행동도 함께 세워 카트 템플릿
    (_build_cart_targets_candidate)이 선택되게 한다."""
    compact = query.replace(" ", "")
    if not any(term in compact for term in _lexicon_terms("cart_terms")):
        return
    for start, end, days in _duration_matches(compact):
        window = compact[max(0, start - _CART_RETENTION_WINDOW): end + _CART_RETENTION_WINDOW]
        # '최근 N일 (동안) 구매하지 않은'은 보관 기간이 아니라 구매 미발생 기간(purchase_inactivity)이다 —
        # 문장에 '담다'(개수 '담았지만')가 있어 창에 보관 표지가 섞여도, 이 N일까지 보관 창으로 채가지 않는다.
        if "최근" in compact[max(0, start - 4): start] and _CART_PURCHASE_ABSENCE_RE.search(compact[end: end + _CART_RETENTION_WINDOW]):
            continue
        if any(word in window for word in _CART_RETENTION_BENEFIT_WORDS):
            continue
        # 기간이 장바구니 어휘에 바로 붙어 있으면 그 자체가 보관 표지다 — 담기 동사('담/유지')를 요구하면
        # '최근 30일 장바구니 총금액'처럼 명사로만 수식하는 표현에서 기간이 조용히 사라진다.
        adjacent = compact[max(0, start - _CART_DURATION_ADJACENCY): start] + compact[end: end + _CART_DURATION_ADJACENCY]
        if not any(term in adjacent for term in _lexicon_terms("cart_terms")) and not any(
            marker in window for marker in _CART_RETENTION_MARKERS
        ):
            continue
        # 방향은 기간에 붙은 어구로 먼저 판정한다(창 전체 판정은 뒤 폴백) — '일주일 이상'은 하한,
        # '최근 30일'은 상한이고, 창 어딘가의 '이상'이 옆 금액 비교어일 때 하한으로 뒤집히는 걸 막는다.
        following = compact[end: end + _CART_RETENTION_DIRECTION_GAP]
        preceding = compact[max(0, start - _CART_RETENTION_DIRECTION_GAP): start]
        if any(word in following for word in _CART_RETENTION_STRONG_MIN_WORDS):
            retention: dict[str, Any] = {"min_days": days, "label": f"장바구니 보관 {days}일 이상"}
        elif any(word in preceding for word in _CART_RECENT_WORDS):
            retention = {"max_days": days, "label": f"장바구니 보관 {days}일 이내"}
        elif any(word in window for word in _CART_RETENTION_STRONG_MIN_WORDS):
            retention = {"min_days": days, "label": f"장바구니 보관 {days}일 이상"}
        elif any(word in window for word in _CART_RECENT_WORDS) or any(
            word in window for word in _CART_RETENTION_MAX_WORDS
        ):
            retention = {"max_days": days, "label": f"장바구니 보관 {days}일 이내"}
        elif any(word in window for word in _CART_RETENTION_MIN_WORDS):
            retention = {"min_days": days, "label": f"장바구니 보관 {days}일 이상"}
        else:
            continue  # 방향어가 없으면 기간의 의미가 모호하다(예: '장바구니 7일 이벤트') → 잡지 않는다.
        _set_cart_retention(plan, retention)
        return
    # 숫자가 없는 '최근 생성된 장바구니'도 방향(최신)은 분명하다. 기간 기본값은 레지스트리
    # (cart_targets.recent_default_days)가 소유한다 — 코드가 숫자를 지어내지 않게 하고, 어떤 창이
    # 적용됐는지 라벨로 드러낸다.
    if _has_recent_cart_event(compact):
        days = _cart_recent_default_days()
        _set_cart_retention(plan, {"max_days": days, "label": f"장바구니 담긴 지 {days}일 이내(최근)"})


def _has_recent_cart_event(compact: str) -> bool:
    """'최근 생성된/담긴 장바구니'처럼 숫자 없는 최신성 표현인지 본다(둘이 가까이 있어야 인정)."""
    for word in _CART_RECENT_WORDS:
        for match in re.finditer(re.escape(word), compact):
            window = compact[match.start(): match.end() + _CART_RECENT_WINDOW]
            if any(marker in window for marker in _CART_RECENT_EVENT_MARKERS):
                return True
    return False


def _cart_recent_default_days() -> int:
    """숫자 없는 '최근'에 적용할 기본 창(일). 레지스트리 소유, 없으면 30일."""
    configured = _MEMBER_TARGET_FILTERS.get("cart_targets", {}).get("recent_default_days")
    return configured if isinstance(configured, int) and configured > 0 else 30


def _set_cart_retention(plan: dict[str, Any], retention: dict[str, Any]) -> None:
    """보관 기간 조건을 세우고, 보관=미결제이므로 카트 행동도 함께 세운다(카트 템플릿 선택용)."""
    plan.setdefault("target_user", {})["cart_retention"] = retention
    _append_unique(plan["target_user"].setdefault("behaviors", []), "cart_abandoner")


def _cart_type_entries() -> tuple[dict[str, Any], ...]:
    """장바구니 유형(CART_TYPE_CD) 값 목록. 레지스트리(cart_targets.cart_types)가 소유한다."""
    entries = _MEMBER_TARGET_FILTERS.get("cart_targets", {}).get("cart_types")
    if not isinstance(entries, list):
        return ()
    return tuple(
        entry
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("canonical"), str)
        and entry["canonical"]
        and isinstance(entry.get("value"), str)
        and entry["value"]
    )


def _apply_cart_type_filter(query: str, plan: dict[str, Any]) -> None:
    """'장바구니에 정기배송 상품을 담은 회원'을 장바구니 유형(cart_type) 조건으로 해석한다.

    '정기배송 상품'은 상품 마스터의 속성이 아니다 — CRM_CM_PRODUCT 에 유형 구분 컬럼이 없고
    (PRODUCT_TYPE_CD 는 GENERAL 단일값) 실DB에서 정기배송을 구분하는 컬럼은 카트 라인의
    ODS_MALL_OMS_CART.CART_TYPE_CD 뿐이다. 이 파서가 없으면 조건을 표현할 술어가 없어
    LLM 자유생성으로 떨어지고, 실제로 `CRM_CM_PRODUCT.PRODUCT_TYPE = 'subscription'`(없는 컬럼·
    없는 값) 서브쿼리가 나왔다.

    값/동의어는 레지스트리가 소유하고, 매칭은 장바구니 어휘(cart_terms)가 같은 문장에 있을 때만 한다 —
    '정기배송 신청한 회원'(주문 유형)까지 카트로 채가지 않게 하는 게이트다."""
    compact = query.replace(" ", "")
    if not any(term in compact for term in _lexicon_terms("cart_terms")):
        return
    candidates = [
        (entry, synonym.replace(" ", ""))
        for entry in _cart_type_entries()
        for synonym in entry.get("synonyms", [])
        if isinstance(synonym, str) and synonym.strip()
    ]
    # 긴 표현 우선 — '정기배송 상품'이 '정기배송'보다 먼저 걸리게(같은 값이라도 매칭을 결정론으로).
    for entry, synonym in sorted(candidates, key=lambda pair: len(pair[1]), reverse=True):
        if synonym not in compact:
            continue
        plan.setdefault("target_user", {})["cart_type"] = {
            "canonical": entry["canonical"],
            "value": entry["value"],
            "label": entry.get("ko_label") or entry["canonical"],
            # '담은'만 물었으면 보관 상태(KEEP_YN='Y')로 좁히지 않는다 — 미결제/이탈은 별도 표현이고,
            # 실데이터에서 정기배송 라인은 전건 KEEP_YN='N' 이라 그냥 걸면 조건이 전멸한다.
            "unpaid_only": _is_cart_abandonment_query(query),
        }
        return


# 생일 타겟: BIRTHDAY(YYYYMMDD)의 월일만 오늘과 비교한다(년도 무시). '이달/이번 달'이면 월만 비교.
# 생일 타겟 감지는 slot_setter(_detect_birthday_target)가 담당한다(레지스트리 "birthday").


# 구매 날짜 타겟: '2024년 3월에 구매한 고객'처럼 구매가 일어난 절대 날짜/기간을 ORDER_DATE 창으로
# 해석한다. 상대 창('최근 N일 미구매')은 purchase_inactivity 가 담당하므로 여기선 연도가 명시된
# 절대 날짜만 잡는다(연도 없는 'M월'은 어느 해인지 모호해 잡지 않는다 → 오탐 방지).
_PURCHASE_DATE_SIGNALS = ("구매", "구입", "주문")


def _month_last_day(year: int, month: int) -> int:
    if month == 2:
        leap = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def _ymd(year: int, month: int, day: int) -> str:
    return f"{year:04d}{month:02d}{day:02d}"


def _parse_purchase_date_period(query: str) -> dict[str, Any] | None:
    """구매가 일어난 절대 날짜/기간을 ORDER_DATE(YYYYMMDD CHAR8) 창 {from,to}로 파싱한다.

    지원: 'YYYY년 M월 D일'(하루), 'YYYY년 M월'(그 달 전체), 'YYYY년'(그 해 전체),
          'YYYY-MM-DD'/'YYYY.MM.DD'/'YYYY/MM/DD'(하루), 'YYYY-MM'(그 달 전체),
          'YYYY년 상반기/하반기'(6개월), 'YYYY년 N분기(=N사분기)'(3개월).
    구매/구입/주문 신호가 있어야 발동한다(생일·캠페인 기간 등 무관한 날짜를 잡지 않기 위함)."""
    if not any(signal in query for signal in _PURCHASE_DATE_SIGNALS):
        return None

    # YYYY년 M월 D일 (하루)
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", query)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= _month_last_day(y, mo):
            return {"from": _ymd(y, mo, d), "to": _ymd(y, mo, d), "label": f"{y}년 {mo}월 {d}일 구매"}
    # YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD (하루)
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", query)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= _month_last_day(y, mo):
            return {"from": _ymd(y, mo, d), "to": _ymd(y, mo, d), "label": f"{y}-{mo:02d}-{d:02d} 구매"}
    # YYYY년 M월 (그 달 전체)
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월", query)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return {"from": _ymd(y, mo, 1), "to": _ymd(y, mo, _month_last_day(y, mo)), "label": f"{y}년 {mo}월 구매"}
    # YYYY-MM (그 달 전체; 뒤에 일자 구분자가 없을 때만)
    m = re.search(r"(\d{4})[-./](\d{1,2})(?![-./]?\d)", query)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return {"from": _ymd(y, mo, 1), "to": _ymd(y, mo, _month_last_day(y, mo)), "label": f"{y}-{mo:02d} 구매"}
    # YYYY년 상반기/하반기·N분기(반기/분기 기간). '그 해 전체' 폴백보다 먼저 봐야 한 해로 뭉개지지 않는다.
    half_quarter = _parse_half_or_quarter_period(query)
    if half_quarter is not None:
        return half_quarter
    # YYYY년 (그 해 전체; 뒤에 '월'이 없을 때)
    m = re.search(r"(\d{4})\s*년(?!\s*\d{1,2}\s*월)", query)
    if m:
        y = int(m.group(1))
        return {"from": _ymd(y, 1, 1), "to": _ymd(y, 12, 31), "label": f"{y}년 구매"}
    return None


# 반기/분기 → 월 범위. 상반기=1~6월, 하반기=7~12월, N분기=(N-1)*3+1 부터 3개월.
_QUARTER_MONTH_RANGES = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}


def _parse_half_or_quarter_period(query: str) -> dict[str, Any] | None:
    """'YYYY년 상반기/하반기', 'YYYY년 N분기(=N사분기)'를 ORDER_DATE 창 {from,to}로 파싱한다.

    연도가 명시돼야 발동한다(연도 없는 '상반기'는 어느 해인지 모호 → 미해석, 오탐 방지). 그냥 '반기'
    (상/하 없이)나 숫자 없는 '분기'도 어느 반/분기인지 모호하므로 잡지 않는다."""
    year_match = re.search(r"(\d{4})\s*년", query)
    if year_match is None:
        return None
    y = int(year_match.group(1))
    if "상반기" in query:
        return {"from": _ymd(y, 1, 1), "to": _ymd(y, 6, 30), "label": f"{y}년 상반기 구매"}
    if "하반기" in query:
        return {"from": _ymd(y, 7, 1), "to": _ymd(y, 12, 31), "label": f"{y}년 하반기 구매"}
    quarter_match = re.search(r"([1-4])\s*(?:사)?분기", query)
    if quarter_match is not None:
        q = int(quarter_match.group(1))
        start_month, end_month = _QUARTER_MONTH_RANGES[q]
        return {
            "from": _ymd(y, start_month, 1),
            "to": _ymd(y, end_month, _month_last_day(y, end_month)),
            "label": f"{y}년 {q}분기 구매",
        }
    return None


# 구매 날짜 타겟 감지는 slot_setter(_parse_purchase_date_period)가 담당한다(레지스트리 "purchase_date").


# 신규 가입 타겟: '신규 가입/신규 회원/새 가입자/new user' 등 가입 신호로 잡는다. 기본 창은
# signup_target.default_days 이고, '최근 N일/N개월 (이내) 가입' 이 있으면 그 창으로 덮는다.
_SIGNUP_SIGNALS = ("신규가입", "신규회원", "신규유저", "신규고객", "새가입", "새로가입", "새가입자", "가입한지",
                   "newuser", "newmember", "newlyregistered", "newsignup", "signedup")
# 가입 창은 통합 파서 _parse_duration_window 가 담당한다(예전 _SIGNUP_PERIOD_PATTERN 제거 — 일/개월/달만
# 알아 '1년 이내 가입'을 놓쳤다).


def _apply_signup_target_filter(query: str, plan: dict[str, Any]) -> None:
    """'신규 가입 고객'을 신규 가입 타겟(signup_target, REG_DT 최근 N일 창)으로 해석한다.

    REG_TYPE_CD.NEW 는 전체의 96%라 무의미하므로 '신규'는 가입일(REG_DT) 기준 최근 N일로 정의한다.
    compile_member_target_conditions 가 signup_target 또는 lifecycle 'new_user' 를 실컬럼 술어로 만들어
    성별/연령 등과 자동 결합한다(LLM 파서가 이미 new_user 를 내보내는 경로와 이중화 — 창 파싱은 이쪽 담당).
    """
    compact = query.replace(" ", "").casefold()
    # 통합 창 파서 — 일/주/개월/년·단어형(한달 등)까지 커버(예전 _SIGNUP_PERIOD_PATTERN 은 일/개월/달만
    # 알아 '1년 이내 가입'을 놓쳤다). 가입 키워드 근처의 창만 본다(다른 조건 창 훔치기 방지).
    window = _parse_duration_window(query, anchor_terms=("가입", "등록"))
    has_signup_signal = any(signal in compact for signal in _SIGNUP_SIGNALS)
    # '가입/등록' + 기간 창(예: '30일 이내 가입한 고객')도 신규 가입 타겟으로 본다. 단 '미가입/재가입/
    # 탈퇴' 등 반대·무관 맥락은 제외한다('가입' 부분문자열 오탐 방지).
    join_window = (
        window is not None
        and ("가입" in compact or "등록" in compact)
        and not any(neg in compact for neg in ("미가입", "재가입", "비가입", "가입안", "가입하지", "탈퇴"))
    )
    if not has_signup_signal and not join_window:
        return
    # days 가 None 이면 compile 이 signup_target.default_days 를 쓴다.
    days = window["min_days"] if window else None
    plan.setdefault("target_user", {})["signup_target"] = {"days": days}


# 결과 행수 제한: "N명만 / N건만 / 상위 N명 / 최대 N명 / N명으로 제한" 처럼 명시적으로 개수를 못박은
# 경우에만 결과 행수를 제한한다(그 외 타겟 오디언스는 '전체가 나와야 한다'는 방침대로 무제한 유지).
# 실제 TOP(MSSQL: CRMDW/CRMAN)/LIMIT(MariaDB: quadmax_sdz) 부착은 sql_guard 가 대상 DBMS 방언에 맞춰
# 처리하고, 이미 TOP 인 지표 랭킹(member_metric_ranking)은 sql_guard 가 중복 없이 보존한다.
_RESULT_LIMIT_MAX_ROWS = 1_000_000
_RESULT_LIMIT_UNITS = r"(?:명|건|개|곳|개사|행|건수|개수|row|rows)"
_RESULT_LIMIT_PATTERNS = (
    re.compile(rf"(\d[\d,]*)\s*{_RESULT_LIMIT_UNITS}\s*(?:만|까지만)(?!\s*원)"),  # 'N명만'
    re.compile(rf"상위\s*(\d[\d,]*)\s*{_RESULT_LIMIT_UNITS}"),                     # '상위 N명'
    re.compile(rf"(?:최대|최고|상한)\s*(\d[\d,]*)\s*{_RESULT_LIMIT_UNITS}"),        # '최대 N명'
    re.compile(rf"(\d[\d,]*)\s*{_RESULT_LIMIT_UNITS}\s*(?:으로|로)?\s*(?:제한|이내로)"),  # 'N명으로 제한'
)


def _parse_result_limit(query: str) -> int | None:
    for pattern in _RESULT_LIMIT_PATTERNS:
        match = pattern.search(query)
        if not match:
            continue
        try:
            value = int(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if value > 0:
            return min(value, _RESULT_LIMIT_MAX_ROWS)
    return None


# 결과 개수 제한('N명만')은 slot_setter(_parse_result_limit → plan.result_limit)가 담당한다(레지스트리 "result_limit").


def _apply_region_density_target(query: str, plan: dict[str, Any]) -> None:
    """'X가 많이 거주하는 동네' 표현을 밀집 지역 랭킹 타겟(region_density_target)으로 해석한다.

    X(코호트 조건: 성별/연령/등급/값인덱스 …)는 별도 필드로 이미 파싱돼 있으므로 여기서는 집계
    구조만 표시한다. build_member_targets_sql_candidate 가 이 플래그를 보고 2단계 SQL(지역별 X 수
    집계 상위 N → 그 지역 정상 회원 추출)을 생성한다. '지역/동네' 언급으로 잡힌 지역 모호성 정책
    (region_context_default)은 여기서 '거주 밀집 지역'으로 구체 해석됐으므로 소비한다(미소비 시
    semantic_resolutions 가 실DB 미지원 조건으로 남아 SQL 생성이 막힌다).
    """
    if isinstance(plan.get("group_ranking_target"), dict):
        # 그룹별 회원 랭킹('지역별로 … N명씩')은 지역 자체 랭킹이 아니라 회원 추출이다 — 가로채지 않는다.
        return
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
    # 맨 컬럼명으로 저장한다(빌더가 자기 별칭을 붙임). config 값의 'B.' 접두어를 그대로 두면
    # 빌더에서 'B.B.SIGUNGU'/'M.B.SIGUNGU' 처럼 이중 접두어가 되어 무효 SQL 이 됐다(구조적 수정).
    column = _region_column_bare(granularity)
    top_n = int(density_config.get("default_top_n") or 5)
    top_match = _REGION_DENSITY_TOP_N_PATTERN.search(query)
    if top_match:
        max_top_n = int(density_config.get("max_top_n") or 30)
        top_n = max(1, min(_parse_count(next(group for group in top_match.groups() if group)) or top_n, max_top_n))
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


def _cart_brand_name_qualifier(query_plan: dict[str, Any]) -> list[str] | None:
    """장바구니 오디언스에서 dimension 코드로 해석되지 '못한' 브랜드명 qualifier 를 반환한다(없으면 None).

    하이브리드(사용자 결정) 폴백: curated dimension_catalog 에 브랜드가 없어 BRAND_ID 코드가 안 잡히면,
    구매 경로와 동일하게 CRM_CM_PRODUCT.BRAND_NAME LIKE 로 거른다. 코드 경로(_cart_dimension_brand_filter)
    가 이미 잡혔으면 그쪽이 우선이므로 여기선 None. 값은 브랜드 계사/명시로 확정된 purchase_object(canonical).

    호출부(_build_cart_targets_candidate 의 repurchase 분기)가 이미 장바구니 오디언스(이탈/보관/유형)를
    보장하므로 여기선 cart_context 를 따로 요구하지 않는다(cart_context 는 dimension 해석이 돌아야 켜져
    rules/무DB 경로에선 꺼져 있음 — 그걸 게이트로 쓰면 브랜드명 폴백이 조용히 사라진다)."""
    if _cart_dimension_brand_filter(query_plan) is not None:
        return None  # 코드 경로 우선
    target_user = query_plan.get("target_user", {}) if isinstance(query_plan.get("target_user"), dict) else {}
    if target_user.get("purchase_object_kind") != "brand":
        return None
    brand = target_user.get("purchase_object")
    return [brand] if isinstance(brand, str) and brand else None


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


# 장바구니 '존재' 표현: "장바구니에 상품이 있는/담아둔/담은". 렉시콘 경로(_is_cart_abandonment_query)는
# 이탈어(미결제/방치 등)가 필수라 존재 표현만으로는 카트 조건이 통째로 소실됐다. 담긴 상태는 그 자체가
# KEEP_YN='Y' 보관 오디언스다. 부정형('담지 않은'/'있지 않은')은 lookahead 로 배제한다.
_CART_PRESENCE_PATTERN = re.compile(
    # '장바구니에 담긴' 뿐 아니라 '장바구니를 보유하고/가지고 있는' 같은 소유 표현도 카트 존재로 본다.
    # 조사(에/를/을/가/이)는 자유롭게 받고, 부정형('보유하지 않은', '있지 않은')은 뒤 lookahead 로 배제한다.
    r"장바구니(?:에|에는|를|을|가|이)?(?:상품|물건|제품|아이템)?(?:이|가|을|를)?(?:들어)?"
    r"(?:있(?!지)|담(?!지)|보유(?!하지)|보관(?!하지)|가지(?!지))"
)

# 장바구니 '부재' 표현: "장바구니(생성)가 없는", "장바구니 생성이나 구매 이력 없는"(분배 부정). '장바구니'
# 뒤에 (생성/담긴 등 명사)·(이나/또는 나열)·(구매이력 등 다른 부재 명사)?가 오고 부정어(없/않)로 끝나는
# 좁은 패턴만 잡는다 — '장바구니에 담은 고객은 쿠폰이 없는'(카트 존재+다른 부재)에 오탐하지 않게 명사군을
# 열거로 제한한다(임의 텍스트 사이끼움 금지).
_CART_ABSENCE_PATTERN = re.compile(
    r"장바구니(?:생성|생성한|담긴|담은|상품|물건|제품|아이템|이력)?"
    r"(?:이나|나|또는|랑|이랑)?(?:구매이력|주문이력|구매내역|구매|주문|상품)?"
    r"(?:이|가|을|를|은|는|도)?(?:없|않)"
)


def _cart_targets_registry() -> dict[str, Any]:
    config = _MEMBER_TARGET_FILTERS.get("cart_targets")
    return config if isinstance(config, dict) else {}


def _cart_member_join_on(alias: str = "A") -> str:
    """카트→회원 조인식('A.CART_ID = B.MEMBER_ID'). 조인키는 cart_targets.join 레지스트리 소유."""
    join = _cart_targets_registry().get("join")
    join = join if isinstance(join, dict) else {}
    left_column = str(join.get("left") or "C.CART_ID").split(".")[-1]
    right = str(join.get("right") or "B.MEMBER_ID")
    return f"{alias}.{left_column} = {right}"


def _cart_from_join_lines(alias: str = "A", product_alias: str | None = None) -> list[str]:
    """카트(→회원[→상품]) FROM/JOIN 절 — 테이블명·조인키는 레지스트리 소유, 별칭만 호출자 관례."""
    config = _cart_targets_registry()
    table = config.get("table", "ODS_MALL_OMS_CART")
    lines = [
        f"FROM {table} {alias}",
        f"     INNER JOIN {_member_table()} {_member_alias()} ON {_cart_member_join_on(alias)}",
    ]
    if product_alias:
        product_join = config.get("product_join")
        product_join = product_join if isinstance(product_join, dict) else {}
        product_table = product_join.get("table", "CRM_CM_PRODUCT")
        left_column = str(product_join.get("left") or "C.PRODUCT_ID").split(".")[-1]
        right_column = str(product_join.get("right") or "CP.PRODUCT_ID").split(".")[-1]
        lines.append(f"     INNER JOIN {product_table} {product_alias} ON {alias}.{left_column} = {product_alias}.{right_column}")
    return lines


def _cart_absence_predicate() -> str:
    """보관(KEEP_YN='Y') 카트 라인이 없는 회원의 NOT EXISTS 술어. cart_targets 레지스트리 소유값 사용."""
    config = _cart_targets_registry()
    table = config.get("table", "ODS_MALL_OMS_CART")
    active = config.get("active_condition", {}) if isinstance(config.get("active_condition"), dict) else {}
    keep_column = (active.get("column") or "A.KEEP_YN").split(".")[-1]
    keep_value = active.get("value", "Y")
    return (
        f"NOT EXISTS (SELECT 1 FROM {table} A "
        f"WHERE {_cart_member_join_on('A')} AND A.{keep_column} = {_sql_quote(str(keep_value))})"
    )


def _cart_quantity_missing_predicate() -> str:
    """담은 수량(QTY)이 입력되지 않은(NULL) 카트 라인이 있는 회원의 EXISTS 술어. '수량이 0'(=0)이 아니라
    '값 자체가 미기입(NULL)'을 뜻한다 — cart_absence 처럼 회원키 상관 서브쿼리라 어느 빌더에나 AND 결합된다."""
    config = _cart_targets_registry()
    table = config.get("table", "ODS_MALL_OMS_CART")
    return (
        f"EXISTS (SELECT 1 FROM {table} A "
        f"WHERE {_cart_member_join_on('A')} AND A.QTY IS NULL)"
    )


def _purchase_inactivity_predicate(min_days: int) -> str:
    """'최근 N일 내 주문 없음'(구매 미발생 기간) 회원키 anti-join 술어.

    cart_absence/campaign_responses 처럼 회원키 상관 NOT EXISTS 라 어느 빌더에나 AND 결합된다 —
    compile_member_target_conditions 와 order_count 빌더가 이 헬퍼를 공유해 동일 문자열을 내므로,
    두 곳이 함께 방출해도 _unique_strings 로 중복 없이 합쳐진다('장바구니 보유 + 최근 90일 미구매'처럼
    카트 빌더가 이겨도 미구매 조건이 조용히 누락되지 않는다)."""
    config = _order_count_targets_config()
    table = config.get("table", "CRM_SL_ORDERHEADERMALL")
    join_column = config.get("join_column", "MEMBER_NO")
    order_date_column = config.get("order_date_column", "ORDER_DATE")
    cutoff = _member_dialect().char8_cutoff(min_days)
    return (
        f"NOT EXISTS (SELECT 1 FROM {table} O WHERE O.{join_column} = B.{join_column} "
        f"AND O.{order_date_column} >= {cutoff})"
    )


def _purchase_membership_predicate(window_days: int | None = None) -> str:
    """구매 이력 존재를 주문 헤더 EXISTS로 증명한다. 기간이 있으면 그 창 안의 주문으로 한정."""
    config = _order_count_targets_config()
    table = config.get("table", "CRM_SL_ORDERHEADERMALL")
    join_column = config.get("join_column", "MEMBER_NO")
    order_date_column = config.get("order_date_column", "ORDER_DATE")
    date_clause = ""
    if isinstance(window_days, int) and window_days > 0:
        date_clause = f" AND O.{order_date_column} >= {_member_dialect().char8_cutoff(window_days)}"
    return f"EXISTS (SELECT 1 FROM {table} O WHERE O.{join_column} = B.{join_column}{date_clause})"


def _apply_cart_absence_filter(query: str, plan: dict[str, Any]) -> None:
    """'장바구니(생성)가 없는'을 장바구니 부재(cart_absence) 조건으로 승격한다.

    '장바구니 없는'을 cart_abandoner(장바구니에 상품이 '있는')로 뒤집던 오파싱을 막고, 회원키 NOT EXISTS
    로 컴파일한다. '장바구니 없음'은 '카트 있음' 조건들(cart_abandoner/cart_retention/cart_type)과 모순이라
    같은 절에서 오파싱된 그것들을 걷어낸다 — 안 그러면 카트 보관(KEEP_YN='Y') 빌더가 이겨 NOT EXISTS 와
    자기모순 SQL 이 나온다. 절 안에서 상품으로 오추출된 purchase_object 조각('생성이나' 등)도 제거한다.
    '장바구니 생성이나 구매 이력 없는'처럼 구매 부재가 함께 오면 그건 기존 no_purchase 트랙이 잡는다."""
    compact = query.replace(" ", "").casefold()
    match = _CART_ABSENCE_PATTERN.search(compact)
    if match is None:
        return
    target_user = plan.setdefault("target_user", {})
    target_user["cart_absence"] = True
    # '카트 있음' 조건들은 부재와 모순 → 걷어낸다.
    target_user["behaviors"] = [b for b in target_user.get("behaviors", []) if b != "cart_abandoner"]
    target_user["cart_retention"] = None
    target_user["cart_type"] = None
    # 부재 절('장바구니 생성이나 …') 안에서 상품으로 오추출된 조각 제거(purchase_history 빌더 오발동 방지).
    obj = target_user.get("purchase_object")
    if isinstance(obj, str) and obj and obj.replace(" ", "").casefold() in compact[match.start():match.end()]:
        target_user["purchase_object"] = None
        target_user["purchase_object_kind"] = None


# 카트 '존재' 승격은 slot_setter(_detect_cart_presence → behaviors append)가 담당한다(레지스트리 "cart_presence").


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

    # 통합 창 파서 — 년/주/단어형(반년 등)까지 커버. inactivity 는 sql_interval 을 포함한다(빌더 계약).
    # 접속/휴면 키워드 근처의 창만 본다(다른 조건 창 훔치기 방지).
    window = _parse_duration_window(query, anchor_terms=("접속", "로그인", "휴면", "비활성"))
    if window is None:
        return None
    return {**window, "sql_interval": f"{window['value']} {window['unit']}"}


def _inactivity_retrieval_terms(period: Any) -> list[str]:
    if not isinstance(period, dict):
        return []
    terms = ["last_login_at", "last_active_days", "inactive", "dormant"]
    if period.get("min_days", 0) >= 180:
        terms.extend(["inactive_180d", "reactivation", "6개월", "미접속", "휴면"])
    return terms


# 최근 로그인(긍정형 접속) 타겟: '최근 N개월/N일 (이내·동안) 로그인·접속한'. 부정형(미접속/로그인하지
# 않은/휴면)은 _parse_inactivity_period 소관이라 여기서는 배제한다. 기간 창이 명시된 경우에만 잡는다 —
# '앱으로 로그인한 사용자'처럼 창 없는 로그인 언급은 최근성 조건이 아니다(app_user 등 다른 트랙 소관).
# 로그인/접속 + (선택 조사) + 활동형(한/했/하신/함) 또는 이력/기록. '로그인은 했지만'처럼 조사(은/는/을/…)가
# 끼는 표현까지 잡는다 — 리터럴 '로그인했'만 나열하면 조사가 낀 '로그인은했'을 놓쳐 최근 로그인 조건이
# 통째로 사라졌다(공백 지운 프롬프트에 맞춘다).
_RECENT_LOGIN_SIGNAL_RE = re.compile(
    r"(?:로그인|접속)(?:은|는|을|를|이|도)?(?:한|했|하신|하였|함|이력|기록)|loggedin"
)
_RECENT_LOGIN_NEG_SIGNALS = (
    "미접속", "미로그인", "접속하지", "접속안", "로그인하지", "로그인안", "휴면", "비활성", "inactive", "dormant",
)
# 'N일 (조사) 비교연산자' = 누적 일수 임계(total_login_days) 신호. 최근성 창(이내/최근/이후)과 배타적이라
# 이게 보이면 최근 로그인 감지를 양보한다(공백 지운 compact 프롬프트에 맞춘다).
_CUMULATIVE_DAYS_THRESHOLD_RE = re.compile(r"\d+일(?:을|를|이|가)?(?:이상|이하|초과|미만|미달)")


def _parse_recent_login_period(query: str) -> dict[str, Any] | None:
    compact_query = query.replace(" ", "").casefold()
    if not _RECENT_LOGIN_SIGNAL_RE.search(compact_query):
        return None
    if any(signal in compact_query for signal in _RECENT_LOGIN_NEG_SIGNALS):
        return None
    # 누적 로그인 '일수' 임계('접속한 날이 10일 미만')는 최근성 창이 아니라 total_login_days 지표다 —
    # 최근성은 이내/이후/최근 등 창 표지를 쓰지, 이상/이하/초과/미만 비교를 쓰지 않는다. 'N일+비교연산자'가
    # 보이면 최근 로그인으로 잡지 않는다(안 그러면 '10일 미만'이 '최근 10일 이내 로그인'으로 뒤집힌다).
    if _CUMULATIVE_DAYS_THRESHOLD_RE.search(compact_query):
        return None
    # 통합 창 파서 — 년/주/단어형까지 커버. 'N개월 전'(과거 시점)은 exclude_past 로 건너뛴다. 로그인/접속
    # 키워드 근처의 창만 본다 — '최근 1년 이내 가입 … 최근 로그인'에서 가입의 '1년'을 훔쳐가지 않게.
    window = _parse_duration_window(query, exclude_past=True, anchor_terms=("로그인", "접속"))
    if window is None:
        # 숫자/기간 없는 '최근 로그인' — 최근성 표지가 있으면 기본 창(recently.default_days)을 준다.
        # '앱으로 로그인한' 처럼 최근성 표지 없는 로그인 언급은 최근성 조건이 아니므로 잡지 않는다.
        if not any(marker in compact_query for marker in _RECENCY_MARKERS):
            return None
        default_days = _recently_default_days()
        window = {"value": default_days, "unit": "days", "min_days": default_days}
    return {**window, "sql_interval": f"{window['value']} {window['unit']}"}
    return None


# 최근 로그인 타겟 감지는 slot_setter(_parse_recent_login_period → recent_login)가 담당한다(레지스트리 "recent_login").


def _recent_login_retrieval_terms(period: Any) -> list[str]:
    if not isinstance(period, dict):
        return []
    return ["last_login_at", "recent_login", "로그인", "접속"]


# 채널 수신동의 타겟: '<채널> 수신(에) 동의한' 은 발송 채널이 아니라 회원 속성(수신동의 Y/N 컬럼)
# 조건이다. 실컬럼 매핑은 member_target_filters.json eq_filters 의 consent 카테고리가 소유하고
# (CRMDW 실값 확인: APP_PUSH_YN/SMS_YN/EMAIL_YN/AGREE_YN 모두 순수 'Y'/'N'), 여기서는 문맥 판정만
# 담당한다. 동의 문맥이면 채널 어휘 매칭(preferred_channels/campaign channels)에서 해당 채널을 빼서
# '선호 채널 미지원' dropped 경고로 조건이 새는 것을 막는다. 거부/미동의는 제외 조건(<> 'Y')이 된다.
# 등급 임계(서열 비교) 연산어 → (rank 비교 연산자). '골드 이상'처럼 등급명 뒤(또는 '등급' 뒤)에
# 붙는 표지. 이상/이하는 경계 포함(>=,<=), 초과/미만은 경계 제외(>,<).
_GRADE_THRESHOLD_OPERATORS: tuple[tuple[str, str], ...] = (
    ("이상", ">="), ("이상의", ">="), ("이상인", ">="),
    ("초과", ">"),
    ("이하", "<="), ("이하의", "<="), ("이하인", "<="),
    ("미만", "<"),
)


def _grade_threshold_registry() -> list[dict[str, Any]]:
    """등급 eq_filters 를 서열(rank) 오름차순으로 반환한다(등급 임계 확장용).

    각 항목: canonical/value/rank/tokens(매칭 표면형 집합). rank 가 없으면 파일 등장 순서(낮음→높음)를
    서열로 쓴다. tokens 는 synonyms + 코드값 영문 토큰(MEM_GRADE_CD.GOLD→gold) + canonical 접미어 제거형
    (gold_grade→gold)까지 모아, '골드'·'GOLD'·'gold' 어느 표기로 와도 잡히게 한다(공백 제거·casefold)."""
    raw = _MEMBER_TARGET_FILTERS.get("eq_filters")
    if not isinstance(raw, list):
        raw = _DEFAULT_MEMBER_TARGET_FILTERS["eq_filters"]
    grades: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict) or entry.get("category") != "grade":
            continue
        canonical, value = entry.get("canonical"), entry.get("value")
        if not (isinstance(canonical, str) and canonical and isinstance(value, str) and value):
            continue
        rank = entry.get("rank")
        if not isinstance(rank, int):
            rank = idx  # 파일 등장 순서 폴백(리스트가 이미 낮음→높음)
        tokens = {syn.replace(" ", "").casefold() for syn in entry.get("synonyms", []) if isinstance(syn, str) and syn}
        tokens.add(value.split(".")[-1].casefold())  # 코드값 영문 토큰(GOLD/VIP/SILVER…)
        tokens.add(re.sub(r"_grade$", "", canonical).casefold())  # canonical 접미어 제거형(gold 등)
        tokens = {token for token in tokens if token}
        grades.append({"canonical": canonical, "value": value, "rank": rank, "tokens": tokens})
    grades.sort(key=lambda grade: grade["rank"])
    return grades


_GRADE_OR_CONNECTIVE = r"(?:또는|이거나|거나|이나)"


def _grade_or_canonicals(compact: str, registry: list[dict[str, Any]]) -> list[str] | None:
    """'골드 또는 VIP'처럼 등급을 OR 로 나열한 표현에서 참여 등급 canonical 목록을 뽑는다(없으면 None).

    지역 OR(→SIDO IN)처럼 등급 OR 도 EMART_GRADE_CD IN(...) 으로 접혀야 하는데, 임계('골드 이상')만 확장하고
    직접 나열은 개별 토큰으로 파싱돼 한 등급만 남던(골드 드롭) 걸 고친다. 임계어(이상/이하)가 아닌 순수 OR 나열만 본다."""
    token_to_canonical = {token: grade["canonical"] for grade in registry for token in grade["tokens"]}
    if not token_to_canonical:
        return None
    grade_alt = "(?:" + "|".join(re.escape(t) for t in sorted(token_to_canonical, key=len, reverse=True)) + ")"
    # 등급 + (등급/회원)? + OR + (등급/회원)? + 등급 이 하나 이상 이어지는 최대 체인.
    chain = re.compile(grade_alt + r"(?:등급|회원)*(?:" + _GRADE_OR_CONNECTIVE + r"(?:등급|회원)*" + grade_alt + r")+")
    match = chain.search(compact)
    if match is None:
        return None
    span = match.group(0)
    # 체인 안에 실제로 등장한 등급 canonical 을 서열 순서대로(registry 순) 모은다.
    selected: list[str] = []
    for grade in registry:
        if any(token in span for token in grade["tokens"]):
            _append_unique(selected, grade["canonical"])
    return selected if len(selected) >= 2 else None


def _apply_grade_threshold_filter(query: str, plan: dict[str, Any]) -> None:
    """'<등급> 이상/이하/초과/미만'을 서열(rank)로 확장한 등급 집합 조건으로 컴파일한다.

    예: '골드 등급 이상' → rank>=골드 = {gold_grade, vip} → lifecycle 에 두 canonical 을 실어
    compile_member_target_conditions 가 같은 컬럼(EMART_GRADE_CD) IN (...) 으로 묶게 한다.
    임계가 감지되면 그 등급 집합이 조건을 소유한다 — 정규화가 경계 등급을 등가로 넣었어도(초과/미만이면
    경계 등급 제외) 기존 grade lifecycle 을 전부 걷어내고 계산된 집합으로 교체해 오차를 막는다."""
    compact = query.replace(" ", "").casefold()
    registry = _grade_threshold_registry()
    if not registry:
        return
    # 임계 표지를 가진 등급 하나를 찾는다(가장 긴 연산어 우선 — '이상의'가 '이상'보다 먼저).
    operators = sorted(_GRADE_THRESHOLD_OPERATORS, key=lambda pair: len(pair[0]), reverse=True)
    matched: tuple[dict[str, Any], str] | None = None
    for grade in registry:
        token_alt = "(?:" + "|".join(re.escape(token) for token in sorted(grade["tokens"], key=len, reverse=True)) + ")"
        op_alt = "(?:" + "|".join(re.escape(word) for word, _ in operators) + ")"
        if not re.search(token_alt + r"(?:등급)?" + op_alt, compact):
            continue
        # 실제 붙은 연산어를 확정한다(가장 긴 것 우선).
        for word, comparator in operators:
            if re.search(token_alt + r"(?:등급)?" + re.escape(word), compact):
                matched = (grade, comparator)
                break
        if matched:
            break
    if not matched:
        # 임계('골드 이상')는 없지만 '골드 또는 VIP'처럼 등급을 OR 로 나열했으면 그 등급 집합으로 컴파일한다.
        or_grades = _grade_or_canonicals(compact, registry)
        if or_grades:
            target_user = plan.setdefault("target_user", {})
            lifecycle = target_user.setdefault("lifecycle", [])
            grade_canonicals = {grade["canonical"] for grade in registry}
            target_user["lifecycle"] = [item for item in lifecycle if item not in grade_canonicals]
            for canonical in or_grades:
                _append_unique(target_user["lifecycle"], canonical)
        return
    pivot, comparator = matched
    compare = {
        ">=": lambda rank: rank >= pivot["rank"],
        ">": lambda rank: rank > pivot["rank"],
        "<=": lambda rank: rank <= pivot["rank"],
        "<": lambda rank: rank < pivot["rank"],
    }[comparator]
    selected = [grade["canonical"] for grade in registry if compare(grade["rank"])]
    if not selected:
        return
    target_user = plan.setdefault("target_user", {})
    lifecycle = target_user.setdefault("lifecycle", [])
    grade_canonicals = {grade["canonical"] for grade in registry}
    # 임계가 등급 조건을 소유한다: 기존 등급 등가(정규화가 넣은 경계 등급 등)를 전부 제거 후 계산 집합으로 교체.
    target_user["lifecycle"] = [item for item in lifecycle if item not in grade_canonicals]
    for canonical in selected:
        _append_unique(target_user["lifecycle"], canonical)


# 가입 채널(온라인/오프라인 매장) 타겟: '온라인 가입'·'오프라인 매장 가입'은 구매 채널(online_buyer/
# offline_buyer)이 아니라 가입 경로 회원 속성이다. 실컬럼은 online_signup eq_filter(REG_OFFSHOP_ID='O',
# 'O'=온라인/몰 가입, 그 외 값=오프라인 매장 가입)가 소유한다. 정규화 사전이 '온라인'/'오프라인' 단독
# 토큰을 buyer 로 먼저 삼켜 가입 문맥을 놓치므로, '가입' 문맥이 붙은 경우만 결정론으로 승격한다.
# 가입 뒤 부정(안 함) 표지. '오프라인 매장 가입 안 한' 같은 이중부정을 잡는다.
_SIGNUP_NEG = r"(?:하지\s*않|안\s*[했한하]|지\s*않|미가입)"
# 채널어(+선택 매장 명사)+조사?+(회원)?+가입+부정? — 공백 제거 compact 기준.
_SIGNUP_ONLINE_RE = re.compile(r"(?:온라인|온라인몰|쇼핑몰)(?:매장|점포|지점|몰)?(?:에서|에|으로|로)?(?:회원)?가입(" + _SIGNUP_NEG + r")?")
_SIGNUP_OFFLINE_RE = re.compile(r"(?:오프라인|오프샵|매장|점포|지점)(?:매장|점포|지점)?(?:에서|에|으로|로)?(?:회원)?가입(" + _SIGNUP_NEG + r")?")


def _apply_signup_channel_filter(query: str, plan: dict[str, Any]) -> None:
    """'온라인/오프라인(매장) 가입(하지 않은)'을 online_signup(REG_OFFSHOP_ID='O') 포함/제외로 승격한다.

    'O'=온라인 가입, 그 외=오프라인 매장 가입이므로 오프라인은 online_signup 의 부정으로 매핑한다:
      온라인 가입           → include online_signup
      온라인 가입 안 함     → exclude online_signup
      오프라인 매장 가입      → exclude online_signup (오프라인 = 온라인 아님)
      오프라인 매장 가입 안 함 → include online_signup (이중부정)
    '가입' 문맥이 채널어에 붙은 경우만 발동해 순수 구매 채널('온라인 구매')과 분리한다. 정규화가
    '온라인'→online_buyer 로 잘못 삼켜도(회원 컬럼 미표현이라 조용히 탈락) 이 필터가 실컬럼 조건을 만든다."""
    if "online_signup" not in MEMBER_EQ_FILTERS:
        return
    compact = query.replace(" ", "").casefold()
    includes = excludes = False
    online = _SIGNUP_ONLINE_RE.search(compact)
    if online:  # 온라인: 긍정→include, 부정→exclude
        if online.group(1):
            excludes = True
        else:
            includes = True
    offline = _SIGNUP_OFFLINE_RE.search(compact)
    if offline:  # 오프라인: 긍정→exclude, 부정→include(이중부정)
        if offline.group(1):
            includes = True
        else:
            excludes = True
    if includes == excludes:
        # 둘 다 없음(발동 안 함) 또는 모순(포함=제외 동시 요청, 공집합)이면 조건을 걸지 않는다.
        return
    if includes:
        _append_unique(plan.setdefault("target_user", {}).setdefault("lifecycle", []), "online_signup")
    else:
        _append_unique(plan.setdefault("exclude", {}).setdefault("lifecycle", []), "online_signup")


# 가입 디바이스 채널(REG_CHANNEL_CD): '앱/PC/모바일웹 (으)로 가입한'. 모바일웹을 PC('웹')보다 먼저
# 판정해 '모바일웹'이 '웹'으로 새지 않게 한다. '가입' 문맥이 있을 때만 발동해 로그인 채널(app_user)·
# 앱푸시 동의 등 다른 '앱' 언급과 분리한다. 실컬럼은 eq_filters signup_channel 카테고리가 소유한다.
_SIGNUP_DEVICE_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("app_signup", ("앱", "어플", "app", "어플리케이션")),
    ("mobile_web_signup", ("모바일웹", "엠웹", "모바일브라우저")),
    ("pc_signup", ("pc", "컴퓨터", "웹", "데스크탑", "데스크톱")),
)
_SIGNUP_DEVICE_SUFFIX = r"(?:으로|로|에서|을통해|를통해|앱)?가입"


# 가입 디바이스(앱/PC/모바일웹) 승격은 attribute_token 실행기(그룹 "signup_device")가 담당한다.
# 문법·표면어는 _attribute_token_groups()["signup_device"] + eq_filters surface_terms 가 소유한다.


def _balance_numeric_filters() -> list[dict[str, Any]]:
    """numeric_filters 중 잔액(balance) 카테고리 항목(적립금/예치금)을 반환한다(컬럼 대 컬럼 비교 전용).

    member_target_filters.json numeric_filters 의 category=="balance" 만 골라, '적립금이 예치금보다 많은'
    같은 동종(금액) 컬럼 비교에 쓴다. 일반 비교/선택은 _numeric_metric_filters(type 구동)가 담당한다."""
    raw = _MEMBER_TARGET_FILTERS.get("numeric_filters")
    if not isinstance(raw, list):
        raw = _DEFAULT_MEMBER_TARGET_FILTERS.get("numeric_filters", [])
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict) and entry.get("category") == "balance" and isinstance(entry.get("column"), str):
            out.append(entry)
    return out


# 회원 수치 지표를 balance 한정이 아니라 numeric_filters 의 type 구동으로 일반화한다 — 새 수치 컬럼(로그인 횟수·
# 로그인 일수 등)은 JSON numeric_filters 에 {canonical, category, column, type, synonyms} 한 줄만 추가하면 비교
# (이상/이하/초과/미만/범위/정확값)와 선택(랭킹/상위%/평균대비)이 전부 열린다 — 전용 파서/코드 추가 불필요.
# age 는 전용 파서(_apply_age_filters, 연대·배타경계 등 값 의미론 고유)가 담당하므로 제외한다.
_COUNT_METRIC_UNIT = "회|번|차례|건|회수"  # integer 지표 임계값 측정 단위(횟수/건수). money 지표는 '원'.


def _numeric_metric_filters() -> list[dict[str, Any]]:
    """일반 비교/선택 머신러리가 다루는 회원 수치 지표(numeric_filters, type∈{money,integer}, age 제외)."""
    raw = _MEMBER_TARGET_FILTERS.get("numeric_filters")
    if not isinstance(raw, list):
        raw = _DEFAULT_MEMBER_TARGET_FILTERS.get("numeric_filters", [])
    out: list[dict[str, Any]] = []
    for entry in raw:
        if (isinstance(entry, dict) and isinstance(entry.get("column"), str)
                and entry.get("type") in {"money", "integer"} and entry.get("canonical") != "age"):
            out.append(entry)
    return out


def _default_metric_grammar(data_type: str | None) -> tuple[str, bool]:
    """레지스트리 units 가 없을 때의 semantic_type/data_type 기반 기본 단위. money=원·맨숫자는 정확값
    (잔액 맥락), 그 외 정수/횟수=회 계열·맨숫자는 모호(연산자 필요)."""
    if data_type == "money":
        return "원", True
    return _COUNT_METRIC_UNIT, False


def _metric_window_grammar(entry: dict[str, Any]) -> tuple[str, bool]:
    """지표 → (_parse_amount_comparison 단위, bare_equals).

    P1(단위): 측정 단위는 코드가 아니라 **통합 지표 레지스트리(docs/data/metrics/*.json)의 units** 가
    소유한다 — 단위가 숫자와 연산자 사이에 끼는 '30일 이상'에서 '일'을 못 흡수하면 숫자와 '이상'을 잇지
    못해 조건이 통째로 누락되고 옆 절의 '100회'를 훔쳐와 오염된다. 레지스트리 스펙(canonical/컬럼으로 매칭)의
    units.expressions 를 alternation 으로 우선 쓰고, **units 가 없을 때만** semantic_type/type 기반 기본 단위로
    폴백한다(레지스트리에 없는 잔액 지표 등은 기존 동작 유지). money 는 맨숫자를 정확값으로 본다(bare_equals)."""
    spec = _registry_spec_for_numeric_entry(entry)
    if spec is not None:
        if spec.units is not None and spec.units.expressions:
            return "|".join(spec.units.expressions), (spec.data_type == "money")
        return _default_metric_grammar(spec.data_type)  # 스펙은 있으나 units 미선언 → semantic 기본
    return _default_metric_grammar(entry.get("type"))  # 레지스트리 미등록 지표 → 기존 type 기본


def _registry_spec_for_numeric_entry(entry: dict[str, Any]) -> "metric_registry.MetricSpec | None":
    """numeric_filters 항목(canonical/column)을 통합 레지스트리의 MetricSpec 에 매칭한다. canonical==metric_id
    우선, 없으면 컬럼('B.TOTAL_LOGIN_CNT')이 spec.source 와 일치하는지로 본다. 미등록이면 None."""
    canonical = entry.get("canonical")
    column = entry.get("column")
    for spec in _METRIC_REGISTRY.all():
        if canonical and spec.metric_id == canonical:
            return spec
        if isinstance(column, str) and spec.source is not None and spec.source.qualified == column:
            return spec
    return None


# 동사형 지표 표현('로그인하지 않은 / 정확히 20번 로그인한 / 평균보다 많이 로그인')을 지표에 연결한다. 명사
# 동의어(로그인 횟수)로는 안 잡히는 행위 표현을, numeric_filters 의 action_aliases 로 잡되 '로그인한 지 30일'
# 같은 날짜/최근성 조건과의 충돌은 게이트로 막는다 — action 어 주변에 '숫자+기간단위'(날짜 조건 신호)가 있으면
# 지표가 아니라 날짜 조건으로 보고 건너뛴다. 부재(=0)·비교·선택은 기존 분류기를 그대로 재사용한다.
_ACTION_METRIC_DATE_GATE = re.compile(r"\d+\s*(?:일|주|주일|개월|달|년|시간|분)")
# 부재(=0) 표지: '한 번도/전혀 … (안)한', '기록/이력/한 적이 없는'. zero_semantics 로 NULL 을 0 으로 본다.
_ACTION_ZERO_PATTERN = re.compile(r"한\s*번도|전혀|이력이?\s*없|기록이?\s*없|한\s*적이?\s*없|없")


def _balance_condition_from_pair(
    column: str, label: str, operator: str, threshold: float, coalesce_zero: bool
) -> dict[str, Any]:
    """(operator, threshold) 한 쌍을 balance_condition dict 로 변환한다. NULL/0 구분 센티넬
    (IS NULL / NULL_OR_ZERO / ZERO_EXACT)을 null_mode·명시 =0 으로 풀어, 잔액 필터와 행위형 필터가
    같은 방식으로 '값 없음'과 '0'을 구분하게 한다."""
    if operator == "IS NULL":
        return {"column": column, "null_mode": "is_null", "label": label}
    if operator == "NULL_OR_ZERO":
        return {"column": column, "null_mode": "null_or_zero", "label": label}
    if operator == "ZERO_EXACT":
        # 명시적 0(0회/0원)은 NULL 을 포함하지 않는다 — '한 번도'(COALESCE=0)와 구분한다.
        return {"column": column, "operator": "=", "threshold": 0.0, "label": label}
    cond = {"column": column, "operator": operator, "threshold": threshold, "label": label}
    if coalesce_zero and operator == "=" and threshold == 0:
        cond["coalesce_zero"] = True
    return cond


def _numeric_metric_action_entries() -> list[dict[str, Any]]:
    """action_aliases(동사형 표면어)를 선언한 수치 지표. 없으면 빈 리스트(행위→지표 연결을 안 씀)."""
    return [
        e for e in _numeric_metric_filters()
        if isinstance(e.get("action_aliases"), list) and any(isinstance(a, str) and a for a in e["action_aliases"])
    ]


def _apply_action_metric_filter(query: str, plan: dict[str, Any]) -> None:
    """행위 동사형 지표 표현을 조건으로 연결한다(명사 동의어 필터 뒤 실행, 이미 잡힌 슬롯은 덮지 않음).

    '한 번도 로그인하지 않은'→=0(COALESCE), '정확히 20번 로그인한'→=20, '평균보다 많이 로그인한'→평균대비.
    action 어 주변에 날짜 신호(숫자+기간단위)가 있으면 날짜/최근성 조건이므로 건너뛴다(오탐 게이트)."""
    tu = plan.setdefault("target_user", {})
    for entry in _numeric_metric_action_entries():
        if isinstance(tu.get("balance_conditions"), list) or plan.get("member_metric_selection") is not None:
            return  # 이미 (명사형 등으로) 수치 조건이 잡혔으면 행위형은 관여하지 않는다
        column = entry["column"].split(".")[-1]
        label = entry.get("canonical", column)
        unit, bare_equals = _metric_window_grammar(entry)
        zero = entry.get("zero_semantics")
        coalesce_zero = bool(isinstance(zero, dict) and zero.get("missing_as_zero"))
        actions = sorted(
            [a for a in entry["action_aliases"] if isinstance(a, str) and a], key=len, reverse=True
        )
        for action in actions:
            index = query.find(action)
            if index < 0:
                continue
            after = query[index + len(action): index + len(action) + 50]
            around = query[max(0, index - 30): index + len(action) + 50]
            if _ACTION_METRIC_DATE_GATE.search(around):
                continue  # 날짜/최근성 조건(예: '로그인한 지 30일') → 지표 아님
            # 부재(=0): NULL 회원 포함(COALESCE). 선택/비교보다 먼저 본다('한 번도 … 않은'은 임계가 아님).
            if _ACTION_ZERO_PATTERN.search(around):
                cond = {"column": column, "operator": "=", "threshold": 0, "label": label}
                if coalesce_zero:
                    cond["coalesce_zero"] = True
                tu["balance_conditions"] = [cond]
                return
            # 선택(랭킹/상위%/평균대비): 수식어가 지표어 앞에 올 수 있어 앞뒤(around)를 본다.
            selection = _classify_balance_selection(around, column, label)
            if selection is not None:
                plan["member_metric_selection"] = selection
                return
            # 비교/정확값: 동사 뒤(after)를 count 단위로 분류('정확히 20번'→=20).
            classified = _classify_balance_window(after, unit, bare_equals)
            if classified:
                tu["balance_conditions"] = [
                    _balance_condition_from_pair(column, label, op, th, coalesce_zero)
                    for op, th in classified
                ]
                return


# 두 잔액 컬럼의 합계('예치금과 적립금의 합', '예치금+적립금'). '종합/결합/조합' 등 무관어에 오탐하지 않게
# 합 어근을 조사/경계와 함께 제한한다.
_BALANCE_SUM_RE = re.compile(r"합계|합산|합쳐|합친|합한|더한|더하면|더해|의\s*합\b|합이\b|합은\b|합으로")


def _balance_sum_condition(query: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """서로 다른 잔액(money) 지표 둘 이상이 '합' 문맥에 함께 오면 **두 컬럼의 실제 합** 임계 조건을 만든다.

    '적립금과 예치금의 합이 50,000원 이상' → (COALESCE(B.CARROT,0) + COALESCE(B.DEPOSIT,0)) >= 50000.
    예전엔 미지원(column_sum_unsupported)으로 막았지만, 컴파일러가 column_expr(좌변 임의 식)을 지원하므로
    조용한 개별-임계 분해(둘 다 >= N, 훨씬 좁은 오답) 없이 정확한 합으로 컴파일한다([[always-apply-expressible-conditions]]).
    NULL 잔액은 0 으로 본다(한쪽만 값이 있어도 합에 반영). 임계를 못 읽으면 None(합 미구성, 일반 흐름에 양보)."""
    sum_match = _BALANCE_SUM_RE.search(query)
    if sum_match is None:
        return None
    money_columns: list[str] = []  # entries 순서 보존(결정론)
    for e in entries:
        if e.get("type") != "money":
            continue
        if any(isinstance(s, str) and s in query for s in e.get("synonyms", [])):
            col = e["column"] if e["column"].startswith("B.") else f"B.{e['column'].split('.')[-1]}"
            if col not in money_columns:
                money_columns.append(col)
    if len(money_columns) < 2:
        return None
    # 임계값은 '합' 표지 뒤에서 읽는다('…의 합이 50,000원 이상' → 50000 >=). money 라 원 단위·맨숫자=정확값.
    window = query[sum_match.end(): sum_match.end() + 50]
    classified = _classify_balance_window(window, "원", bare_equals=True)
    if not classified:
        return None
    column_expr = "(" + " + ".join(f"COALESCE({col}, 0)" for col in money_columns) + ")"
    label = "balance_sum(" + "+".join(col.split(".")[-1] for col in money_columns) + ")"
    conditions: list[dict[str, Any]] = []
    for operator, threshold in classified:
        if operator not in ("=", ">", ">=", "<", "<="):
            continue  # 존재/부재·NULL 센티넬은 '합' 임계에 의미 없음 → 건너뜀
        conditions.append({
            "column": money_columns[0].split(".")[-1],  # 대표 컬럼(빌더 유효성 검사용)
            "column_expr": column_expr,
            "operator": operator,
            "threshold": threshold,
            "label": label,
        })
    return conditions or None


def _apply_balance_condition_filter(query: str, plan: dict[str, Any]) -> None:
    """'적립금/예치금 N원 이상/이하/초과/범위/정확값/보유·미보유'를 회원 잔액 컬럼 조건(balance_conditions)으로
    해석한다. 지표 동의어 뒤 어구를 _classify_balance_window 로 **우선순위 분류**(랭킹/%/평균은 소유 포기 →
    오답 대신 미지원, 범위는 BETWEEN, 부등호는 부사형+동사형, 그다음 등호, 마지막 존재/부재)한다.
    compile_member_target_conditions 가 B.<컬럼> <op> <임계값> 술어로 컴파일해 다른 조건과 AND 결합한다.
    잔액(money)뿐 아니라 로그인 횟수 같은 integer 지표도 numeric_filters 등록만으로 동일 문법을 공유한다."""
    entries = _numeric_metric_filters()
    if not entries:
        return
    # 두 잔액의 '합'(예치금+적립금 >= N)은 두 컬럼의 실제 합 식으로 컴파일한다. 개별 임계로 조용히 분해하면
    # (둘 다 >= N) 훨씬 좁은 오답이 되므로, 합 문맥이 잡히면 여기서 합 조건만 세우고 개별 파싱은 건너뛴다.
    sum_conditions = _balance_sum_condition(query, entries)
    if sum_conditions:
        tu = plan.setdefault("target_user", {})
        existing = tu.get("balance_conditions")
        if isinstance(existing, list):
            existing.extend(sum_conditions)
        else:
            tu["balance_conditions"] = sum_conditions
        return
    money_entries = [e for e in entries if e.get("type") == "money"]  # 컬럼 대 컬럼 비교는 동종(금액)끼리만
    conditions: list[dict[str, Any]] = []
    for entry in entries:
        column = entry["column"].split(".")[-1]  # 'B.' 접두어 제거(빌더가 alias 부착)
        unit, bare_equals = _metric_window_grammar(entry)
        zero = entry.get("zero_semantics")
        coalesce_zero = bool(isinstance(zero, dict) and zero.get("missing_as_zero"))
        synonyms = sorted(
            [s for s in entry.get("synonyms", []) if isinstance(s, str) and s], key=len, reverse=True
        )
        for synonym in synonyms:
            index = query.find(synonym)
            if index < 0:
                continue
            # 하루 평균 <지표>(비율)는 이 지표의 원 임계가 아니라 파생 비율이다(_apply_ratio_metric_filter 소유).
            # '하루 평균 로그인 횟수 3회 이상'에서 '로그인 횟수'를 원 횟수 임계(=3)로 오탐하지 않게 앞을 본다.
            if _RATIO_METRIC_PREFIX_RE.search(query[max(0, index - 10): index]):
                continue
            window = _clause_scoped_window(query, index + len(synonym))
            classified = _classify_balance_window(window, unit, bare_equals)
            if classified:
                label = entry.get("canonical", column)
                for operator, threshold in classified:
                    # NULL/0 구분 센티넬(IS NULL / NULL_OR_ZERO / ZERO_EXACT)과 nullable 부재(COALESCE)를
                    # 공용 변환기로 처리한다 — '값 없음'과 '0'을 구분한다.
                    conditions.append(
                        _balance_condition_from_pair(column, label, operator, threshold, coalesce_zero)
                    )
                break  # 한 지표당 하나(범위는 위에서 두 술어로 확장)
            # 컬럼 대 컬럼 비교('적립금이 예치금보다 많은') — 금액 지표끼리, 숫자 임계가 없을 때만.
            if entry.get("type") != "money":
                continue
            column_cmp = _classify_balance_column_comparison(window, column, money_entries)
            if column_cmp is not None:
                operator, threshold_expr = column_cmp
                conditions.append(
                    {"column": column, "operator": operator, "threshold_expr": threshold_expr, "label": entry.get("canonical", column)}
                )
                break
    if conditions:
        # 파생 비율 필터(_apply_ratio_metric_filter)가 먼저 '하루 평균' 조건을 심어뒀을 수 있으므로
        # 덮어쓰지 않고 이어붙인다 — '로그인 일수 30일 이상 + 하루 평균 로그인 3회 이상'처럼 원 지표와
        # 파생 비율이 한 문장에 오면 둘 다 살아남아야 한다.
        tu = plan.setdefault("target_user", {})
        existing = tu.get("balance_conditions")
        if isinstance(existing, list):
            existing.extend(conditions)
        else:
            tu["balance_conditions"] = conditions


# 파생(비율) 지표: '하루 평균 로그인 횟수'처럼 두 수치 컬럼의 비(numerator/denominator)를 임계와 비교한다.
# 원 컬럼 임계('로그인 횟수 3회 이상' → CNT>=3)와 의미가 달라(하루 평균은 CNT/DAYS>=3) 별도 파생으로 다룬다.
# '하루/일/매일 + 평균' 접두어가 붙은 지표어만 비율로 보고, 그 접두어를 balance_condition 이 원 임계로 오탐하지
# 않도록 억제한다(_apply_balance_condition_filter 에서 이 접두어가 앞에 오면 해당 동의어를 건너뛴다).
_RATIO_METRIC_PREFIX_RE = re.compile(r"(?:하루|1일|매일|일)\s*평균\s*$")


def _ratio_metric_filters() -> list[dict[str, Any]]:
    """파생 비율 지표(ratio_filters, {canonical, numerator_column, denominator_column, unit, synonyms})."""
    raw = _MEMBER_TARGET_FILTERS.get("ratio_filters")
    if not isinstance(raw, list):
        raw = _DEFAULT_MEMBER_TARGET_FILTERS.get("ratio_filters", [])
    out: list[dict[str, Any]] = []
    for entry in raw:
        if (isinstance(entry, dict) and isinstance(entry.get("numerator_column"), str)
                and isinstance(entry.get("denominator_column"), str) and entry.get("synonyms")):
            out.append(entry)
    return out


def _apply_ratio_metric_filter(query: str, plan: dict[str, Any]) -> None:
    """'하루 평균 로그인 횟수 N회 이상' → CAST(numerator AS FLOAT)/NULLIF(denominator,0) <op> N.

    balance_condition 앞에 실행해 파생 비율을 먼저 확정한다 — 분모 0(로그인 일수 0)은 NULLIF 로 NULL 화해
    나눗셈 예외를 막고, 그 회원은 조건에서 자연 제외된다. 이미 balance_conditions 가 있으면 추가로 AND 결합."""
    for entry in _ratio_metric_filters():
        num_col = entry["numerator_column"].split(".")[-1]
        den_col = entry["denominator_column"].split(".")[-1]
        unit = entry.get("unit")
        unit = unit.strip() if isinstance(unit, str) and unit.strip() else _COUNT_METRIC_UNIT
        synonyms = sorted(
            [s for s in entry.get("synonyms", []) if isinstance(s, str) and s], key=len, reverse=True
        )
        for synonym in synonyms:
            index = query.find(synonym)
            if index < 0:
                continue
            window = _clause_scoped_window(query, index + len(synonym))
            classified = _classify_balance_window(window, unit, bare_equals=False)
            if not classified:
                continue
            expr = f"CAST(B.{num_col} AS FLOAT) / NULLIF(B.{den_col}, 0)"
            conds = [
                {"column": num_col, "column_expr": expr, "operator": op, "threshold": th,
                 "label": entry.get("canonical", num_col)}
                for op, th in classified
            ]
            tu = plan.setdefault("target_user", {})
            existing = tu.get("balance_conditions")
            if isinstance(existing, list):
                existing.extend(conds)
            else:
                tu["balance_conditions"] = conds
            return


# 캠페인 접촉/오퍼·구매 반응/쿠폰 사용: 캠페인 회원 반응 팩트(MCS_CAMP_MBR_RSPN_FT, 회원키 MBR_NO)로
# 컴파일한다. 각 항목: (canonical, R 별칭 술어, 표면어 정규식). 여러 개가 잡히면 각각 EXISTS 로 AND 결합한다.
# 표면어는 공백을 지운 프롬프트에 re.search 로 맞춘다 — '발송은 성공했지만'/'발송에 성공한'처럼 조사가
# 끼는 표현까지 리터럴로 나열하면 조합이 폭발하고, 실제로 '캠페인에서 발송은 성공했지만'이 어느 항목에도
# 걸리지 않아 접촉 성공(CNCT_SCS_YN='Y') 조건이 통째로 누락됐다.
_CAMPAIGN_RESPONSE_TARGETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # 접촉(발송) 성공은 반응 팩트가 아니라 셀 발송 대상 명단(Z_CAMP_MBR, source=camp_member_list)이
    # 소스다 — 반응 팩트는 반응자 중심 적재라(데모는 구매반응자뿐) 접촉성공 EXISTS 를 팩트에 걸면
    # '발송 성공했지만 구매반응 없음' 조합이 구조적으로 공집합이 된다.
    ("campaign_contact", "M.CONTAC_SUCC_YN = 'Y'",
     ("캠페인접촉", "캠페인을받은", "캠페인메시지를받은", "캠페인문자를받은", "캠페인을수신", "캠페인발송받은", "캠페인대상", "캠페인수신",
      # 발송/전송/접촉 + (조사) + 성공 = 접촉 성공. '발송 실패'는 걸리지 않는다.
      r"(?:발송|전송|접촉|도달)(?:은|는|이|가|에|에는|도|을|를)?성공")),
    ("offer_response", "R.OFFR_RSPN_YN = 'Y'",
     ("오퍼반응", "오퍼에반응", "혜택반응", "혜택에반응", "제안반응")),
    ("buy_response", "R.BUY_RSPN_YN = 'Y'",
     ("캠페인보고구매", "캠페인후구매", "캠페인반응구매", "캠페인을보고구매", "구매반응", "캠페인구매")),
    ("coupon_used", "R.USE_CPN_CNT > 0",
     ("쿠폰사용", "쿠폰을사용", "쿠폰쓴", "쿠폰을쓴", "쿠폰이용", "쿠폰을이용")),
)
_CAMPAIGN_RESPONSE_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (canonical, predicate, re.compile("|".join(terms)))
    for canonical, predicate, terms in _CAMPAIGN_RESPONSE_TARGETS
)

# 캠페인 구매반응 '부정': "캠페인에서(는) 구매하지 않은/구매 안 한/구매 반응이 없는". 반응 팩트에
# 구매반응 행이 없음(NOT EXISTS)으로 컴파일한다 — 전체 주문 기준 미구매(purchase_inactivity/no_purchase)와
# 의미가 다르다(캠페인 밖 구매는 허용). 긍정 리터럴('캠페인구매')이 부정문의 부분문자열에 오탐하므로
# 부정을 먼저 판정하고 긍정 buy_response 를 눌러야 한다.
_CAMPAIGN_BUY_NEG_PATTERN = re.compile(
    r"캠페인(?:에서|에|을|를)?(?:는|은|도)?(?:보고|통해|후)?"
    # 구매와 부정어 사이에 '이력/내역'(+조사)이 끼는 '캠페인 구매 이력이 없는'도 잡는다 — 안 그러면
    # 부정이 안 잡히고(buy_negated=False) 긍정 리터럴 '캠페인구매'가 매칭돼 정반대(EXISTS 구매)로 뒤집혔다.
    r"(?:구매(?:이력|내역)?(?:를|은|는|도|이|가)*(?:반응)?(?:이|가|은|는)?(?:하지않|안하|안한|없)|미구매)"
)
# "캠페인 구매금액이 0원(인)/없는" = 캠페인 귀속 구매금액이 0 = 캠페인 구매 안 함 → 구매반응 부정(NOT EXISTS).
# SUM(BUY_AMT)=0 은 반응 팩트에 행이 없다는 뜻이라 HAVING 임계로 못 세고 no_buy_response 로 다뤄야 의미가 맞다.
# 0 은 (?<!\d)0원 으로 정확히 잡아 '100원'의 부분문자열 '0원' 오탐을 막는다.
_CAMPAIGN_BUY_ZERO_AMOUNT_PATTERN = re.compile(
    r"캠페인(?:을|를|에서|으로|에|의)?(?:통해|통한|보고|반응|후)?(?:한)?(?:구매|결제)한?금액(?:이|은|는|가)?"
    r"(?:(?<!\d)0원|없)"
)
# "캠페인 구매건수(가) 없거나/0건(인)" = 캠페인 귀속 구매 건수 0 = 캠페인 구매 안 함 → 구매반응 부정(NOT
# EXISTS). 건수 임계는 HAVING COUNT op K(K>0)만 표현할 수 있고 0/없음은 반응 팩트에 행이 없다는 뜻이라
# no_buy_response 로 다뤄야 옳다. 안 그러면 긍정 리터럴('캠페인구매')이 매칭돼 정반대(EXISTS 구매)로 뒤집힌다.
_CAMPAIGN_BUY_ZERO_COUNT_PATTERN = re.compile(
    r"캠페인(?:을|를|에서|으로|에|의)?(?:통해|통한|보고|반응|후)?(?:한)?(?:구매|결제)(?:건수|횟수)(?:가|이|은|는)?"
    r"(?:없|(?<![\d,.])0\s*건)"
)
# 캠페인 어순 무관 '구매반응' 부정: "구매 반응이 없는". '구매반응'은 반응 팩트(BUY_RSPN_YN) 어휘라
# 캠페인 단어와 인접하지 않아도 캠페인 구매반응 부정으로 확정한다 — "캠페인 발송에 성공했지만 구매
# 반응이 없는"처럼 발송 절이 캠페인과 구매 사이에 끼는 어순에서, 긍정 리터럴('구매반응')이 부정문의
# 부분문자열에 매칭돼 정반대(EXISTS 구매반응 있음)로 컴파일되던 사고 방지.
_BUY_RSPN_NEG_PATTERN = re.compile(r"구매반응(?:이|가|은|는|도)?(?:없|하지않|안하|안한)")
# 문장 전체의 일반형 구매 부정(캠페인 문맥 여부 무관) — 캠페인 부정 매치 스팬과 겹침을 비교해, 모든
# 구매 부정이 캠페인 문맥이면 오배정된 전체 주문 미구매(purchase_inactivity/no_purchase)를 걷어낸다.
_GENERIC_BUY_NEG_PATTERN = re.compile(
    r"구매(?:이력|내역)?(?:를|은|는|도|이|가)*(?:반응)?(?:이|가|은|는)?(?:하지않|안하|안한|없)|미구매"
)

# ── 부정 직교 패스 ──────────────────────────────────────────────────────────────────────
# 긍정 리터럴('쿠폰을사용')이 부정문('쿠폰을 사용하지 않은')의 부분문자열에 매칭돼 정반대(EXISTS)로
# 뒤집히던 반전 사고를, 개념별 부정을 '독립 감지'해 그 개념의 긍정을 '구조적으로 억제'하는 방식으로 막는다.
# buy 는 어순 무관·'이력' 삽입까지 커버하는 전용 패턴(_CAMPAIGN_BUY_NEG_PATTERN)이 담당하고, 나머지
# 개념(offer/coupon/contact)은 '개념어 바로 뒤 tail'에서 부정 표지를 본다 — 단, 다음 개념어/절 경계
# 전까지만 봐서('쿠폰 사용하고 구매하지 않은'처럼) 옆 개념의 부정을 훔쳐오지 않는다.
_CAMPAIGN_TAIL_NEG_RE = re.compile(r"없|않|못[한했하받]|안[한함했하]")
# 다음 '개념' 시작(부정 탐색을 여기서 멈춤 — 옆 개념 부정 오귀속 방지).
_CAMPAIGN_CONCEPT_ANCHOR_RE = re.compile(r"구매|구입|쿠폰|오퍼|혜택|제안|발송|전송|접촉|도달")
# 절 경계(부정 탐색 상한). 조사/어미 하나로 절이 갈리는 지점만(공백 제거 텍스트라 '고객'의 '고' 같은
# 단음절 오탐을 피해 2음절 이상 연결어미만 나열).
_CAMPAIGN_CLAUSE_BOUNDARY_RE = re.compile(r"지만|면서|이며|이고|이거나|거나|또는|그리고|반면|다만|,")
_CAMPAIGN_TAIL_NEG_WINDOW = 10
# 개념어(부정 탐색의 기준점) + 그 개념의 canonical. buy 는 전용 패턴이 담당하므로 제외.
_CAMPAIGN_CONCEPT_NEG_SPECS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("offer_response", re.compile(r"(?:오퍼|혜택|제안)(?:에|에는|을|를|이|가|은|는|도)?(?:반응|응답)")),
    ("coupon_used", re.compile(r"쿠폰(?:을|를|이|은|는|도)?(?:사용|이용|쓰|쓴)")),
    ("campaign_contact", re.compile(r"(?:발송|전송|접촉|도달)(?:은|는|이|가|에|에는|도|을|를)?성공")),
)


def _campaign_concept_tail_negated(compact: str, start: int, anchors: list[int], boundaries: list[int]) -> bool:
    """개념어가 끝난 위치(start) 뒤 tail 에 부정 표지가 있으면 True. 탐색 상한은 '다음 개념어 시작·절
    경계·윈도우' 중 가장 가까운 곳 — 옆 개념('구매하지 않은')의 부정을 이 개념 것으로 훔쳐오지 않는다."""
    limit = start + _CAMPAIGN_TAIL_NEG_WINDOW
    for pos in boundaries:
        if pos >= start:
            limit = min(limit, pos)
            break
    for pos in anchors:
        if pos > start:  # 다음 개념 시작 전까지만
            limit = min(limit, pos)
            break
    return _CAMPAIGN_TAIL_NEG_RE.search(compact, start, limit) is not None


def _apply_campaign_response_filter(query: str, plan: dict[str, Any]) -> None:
    """'캠페인 접촉/오퍼·구매 반응/쿠폰 사용'을 캠페인 반응 조건(campaign_responses)으로 해석한다.

    compile_member_target_conditions 가 MCS_CAMP_MBR_RSPN_FT(회원키 MBR_NO) EXISTS 서브쿼리로 컴파일하므로
    어느 빌더를 타든 다른 조건과 AND 결합된다. 여러 반응이 잡히면 각각 EXISTS 로 AND 결합한다(서로 다른
    캠페인이어도 됨). 부정형 '캠페인에서 구매하지 않은'은 negated 플래그로 NOT EXISTS 컴파일되고,
    그 부정이 문장의 유일한 구매 부정이면 오배정된 전체 주문 미구매(purchase_inactivity/no_purchase —
    '최근 N개월' 창을 로그인 절 등에서 훔쳐온다)를 걷어낸다."""
    compact = query.replace(" ", "").casefold()
    anchors = [match.start() for match in _CAMPAIGN_CONCEPT_ANCHOR_RE.finditer(compact)]
    boundaries = [match.start() for match in _CAMPAIGN_CLAUSE_BOUNDARY_RE.finditer(compact)]
    # 부정 직교 패스: 개념별로 '부정됨'을 독립 감지한다. buy 는 전용 패턴(어순 무관·'이력' 삽입 커버),
    # offer/coupon/contact 는 개념어 뒤 tail 부정으로. negated_for 에 든 개념은 아래 루프에서 긍정을
    # 절대 내지 않고 부정 트랙(NOT EXISTS)만 낸다 — 긍정↔부정이 상호배타라 반전이 구조적으로 불가능하다.
    buy_negation_spans = [
        match.span()
        for pattern in (
            _CAMPAIGN_BUY_NEG_PATTERN, _BUY_RSPN_NEG_PATTERN,
            _CAMPAIGN_BUY_ZERO_AMOUNT_PATTERN, _CAMPAIGN_BUY_ZERO_COUNT_PATTERN,
        )
        for match in pattern.finditer(compact)
    ]
    negated_for: set[str] = set()
    if buy_negation_spans:
        negated_for.add("buy_response")
    for canonical, concept_pattern in _CAMPAIGN_CONCEPT_NEG_SPECS:
        for match in concept_pattern.finditer(compact):
            if _campaign_concept_tail_negated(compact, match.end(), anchors, boundaries):
                negated_for.add(canonical)
                break
    responses: list[dict[str, Any]] = []
    for canonical, predicate, pattern in _CAMPAIGN_RESPONSE_PATTERNS:
        if canonical in negated_for:
            # 부정 확정 개념: 긍정 리터럴 매칭 여부와 무관하게 부정 트랙만 낸다(긍정 억제 = 반전 차단).
            neg_canonical = "no_buy_response" if canonical == "buy_response" else "no_" + canonical
            response: dict[str, Any] = {"canonical": neg_canonical, "predicate": predicate, "negated": True}
            if canonical == "campaign_contact":
                response["source"] = "camp_member_list"
            responses.append(response)
        elif pattern.search(compact):
            response = {"canonical": canonical, "predicate": predicate}
            if canonical == "campaign_contact":
                response["source"] = "camp_member_list"
            responses.append(response)
    if buy_negation_spans:
        # 문장의 모든 일반형 구매 부정이 캠페인 부정 매치와 겹치면(=캠페인 문맥뿐이면) 오배정된 전체
        # 주문 미구매를 걷어낸다. 별개의 전체 미구매("최근 90일 구매 안 했고 캠페인 반응도 없는")가
        # 있으면 스팬이 안 겹쳐 주문 트랙이 유지된다.
        generic_matches = list(_GENERIC_BUY_NEG_PATTERN.finditer(compact))
        all_campaign_scoped = generic_matches and all(
            any(match.start() < end and start < match.end() for start, end in buy_negation_spans)
            for match in generic_matches
        )
        if all_campaign_scoped:
            target_user = plan.setdefault("target_user", {})
            target_user["purchase_inactivity"] = None
            target_user["behaviors"] = [b for b in target_user.get("behaviors", []) if b != "no_purchase"]
    if responses:
        plan.setdefault("target_user", {})["campaign_responses"] = responses


# "쿠폰 사용 후 추가(로) 구매(구입/주문) 없는/하지 않은" 처럼 '추가 구매가 일어나지 않음'을 뜻하는 표현.
# 이를 '실주문 자체가 전혀 없음'(no_purchase, CRM_SL_ORDERHEADERMALL anti-join)으로 확정한다 — 캠페인
# 반응(쿠폰 사용 등)과 함께 오면 campaign_response 빌더가 fact_join(order_count_behavior)에 양보하고,
# order_count 빌더가 쿠폰 EXISTS + 주문 NOT EXISTS 를 하나의 SQL 로 AND 결합한다. 공백을 지운 프롬프트에
# 맞춘다. '재구매/다시 구매하지 않은'(과거 구매는 있고 재구매만 없음)과는 어의가 달라 포함하지 않는다.
_ADDITIONAL_PURCHASE_ABSENCE_PATTERN = re.compile(
    r"(?:추가로|추가|더이상|더)(?:의)?(?:구매|구입|주문)"
    r"(?:를|은|는|가|도|한)?(?:없|안했|안한|않았|않은|않는|하지않|안함|못했|못한)"
)


def _apply_no_additional_purchase_filter(query: str, plan: dict[str, Any]) -> None:
    """'추가 구매 없는'을 '실주문 자체가 전혀 없음'(no_purchase, 주문 anti-join) 행동으로 승격한다.

    시간 창이 붙은 미구매(purchase_inactivity, '최근 N일 구매 안 함')나 '캠페인 구매반응 없음'(no_buy_response,
    캠페인 밖 구매는 허용)은 각기 다른 트랙이 소유하므로, 그 둘이 이미 잡혔으면 여기서 승격하지 않는다
    (이중 조건 방지). no_purchase 는 order_count 빌더(anti-join)가 소유하고, 그 빌더가 캠페인 반응 EXISTS 를
    compile_member_target_conditions 로 함께 결합하므로 '쿠폰 사용 후 추가 구매 없는'이 한 SQL 로 남는다."""
    target_user = plan.setdefault("target_user", {})
    if isinstance(target_user.get("purchase_inactivity"), dict):
        return
    if any(
        isinstance(response, dict) and response.get("canonical") == "no_buy_response"
        for response in target_user.get("campaign_responses") or []
    ):
        return
    compact = query.replace(" ", "").casefold()
    if _ADDITIONAL_PURCHASE_ABSENCE_PATTERN.search(compact):
        _append_unique(target_user.setdefault("behaviors", []), "no_purchase")


# '구매/주문 횟수(건수)가 0회/0건 / 없는' = 주문 건수 0 = 평생 무주문. 집계 HAVING COUNT(...)=0 은 그룹에
# 아예 안 나타나 항상 공집합이므로, '한 번도 구매하지 않은'과 같은 no_purchase(주문 anti-join, NOT EXISTS)로
# 컴파일해야 옳다(사용자 결정 2026-07-26). 0 뒤에 비교어(이상/이하/초과/미만/보다)가 오면 임계 조건이지
# 무주문이 아니므로 제외한다. 캠페인 문맥('캠페인 구매건수 0건')은 no_buy_response 트랙 소유라 양보한다.
# 지표 명사(횟수/건수)는 선택적 — '주문이 0건'(명사=주문, 0건의 '건'은 0의 계수 단위)처럼 지표어 없이
# 조사만 끼는 표현도 잡는다. 0 값 branch 가 경계(?<![\d,.])로 보호돼 '구매액 0원' 같은 금액 0 오탐은 없다.
_ZERO_PURCHASE_COUNT_PATTERN = re.compile(
    r"(?:구매|구입|주문)\s*(?:횟수|건수|건|회|번)?(?:가|이|은|는|도)?\s*"
    r"(?:"
    r"(?<![\d,.])0\s*(?:회|건|번)(?!\s*(?:이상|이하|초과|미만|넘|보다))"  # '0회'(비교어가 뒤따르면 임계 조건이라 제외)
    r"|(?<![\d,.])0(?![\d,.회건번])"  # 단위 없는 맨 '0'('0인') — 숫자/단위 미부착
    r"|없"
    r")"
)


def _strip_zero_order_count_aggregates(aggregates: list[Any]) -> list[Any]:
    """order_count '='0(공집합 HAVING COUNT=0)을 걷어낸다 — 창 유무 무관. 이 조건은 GROUP BY 서브쿼리에서
    항상 빈 결과라 표현 불가이므로 anti-join(no_purchase/purchase_inactivity)이 대체한다."""
    return [
        c for c in aggregates
        if not (
            isinstance(c, dict) and c.get("metric_id") == "order_count"
            and c.get("operator") == "=" and c.get("threshold") == 0
        )
    ]


def _apply_zero_purchase_count_filter(query: str, plan: dict[str, Any]) -> None:
    """'구매 횟수가 0회 / 주문 건수가 0건 / 구매 건수가 없는' → no_purchase(주문 anti-join)로 승격한다.

    집계 COUNT=0 은 그룹 밖이라 표현 불가라 anti-join 이 유일한 정답이다([[additional-purchase-absence-no-purchase]]).
    기간 창('최근 90일 구매 0회')은 그 기간 무주문(purchase_inactivity)이라 평생 무주문과 다르므로 제외하고,
    캠페인 문맥은 캠페인 구매반응 부정(no_buy_response)이 소유하므로 양보한다. 집계 파서가 '정확히 0회'를
    order_count '='0 으로 오컴파일해 뒀으면(공집합 HAVING) 그 조건을 걷어내고 no_purchase 로 대체한다."""
    if "캠페인" in query:
        return  # 캠페인 귀속 건수 0 → no_buy_response 트랙(NOT EXISTS 구매반응)
    target_user = plan.setdefault("target_user", {})
    # 집계 파서가 '정확히 0회 구매한'을 order_count '='0(기간창 없음)으로 뽑아뒀으면, 어순과 무관하게
    # 그 자체가 무주문이다(공집합 HAVING). 기간창/달력기간이 붙은 0 은 '그 기간 무주문'이라 제외한다.
    aggregates = target_user.get("aggregate_conditions")
    zero_count_aggregate = isinstance(aggregates, list) and any(
        isinstance(c, dict) and c.get("metric_id") == "order_count"
        and c.get("operator") == "=" and c.get("threshold") == 0
        and not c.get("window_days") and not c.get("calendar_period")
        for c in aggregates
    )
    compact = query.replace(" ", "").casefold()
    if not zero_count_aggregate and _ZERO_PURCHASE_COUNT_PATTERN.search(compact) is None:
        return
    if isinstance(target_user.get("purchase_inactivity"), dict):
        # 이미 창 기반 구매 미발생 기간이 잡힘 — 정규화가 뒤늦게 추가한 평생 no_purchase 를 걷어낸다
        # (윈도우 anti-join 이 그 기간의 무주문을 이미 표현하므로 평생 anti-join 은 의미를 좁히는 잡음).
        target_user["behaviors"] = [b for b in target_user.get("behaviors", []) if b != "no_purchase"]
        return
    # 창이 붙은 무주문('최근 180일 구매건수 0건')은 '그 기간 무주문'이므로 평생 no_purchase 가 아니라
    # purchase_inactivity(윈도우 anti-join, NOT EXISTS + ORDER_DATE 컷오프)로 컴파일한다. 지표어 없는
    # 0건 표현이라 창은 구매 도메인 앵커 근처에서만 본다(옆 조건 창 도용 방지). — silent drop 방지 핵심.
    windowed = _parse_duration_window(query, anchor_terms=("구매", "구입", "주문"))
    if windowed is not None:
        target_user["purchase_inactivity"] = windowed
        if isinstance(aggregates, list):
            target_user["aggregate_conditions"] = _strip_zero_order_count_aggregates(aggregates)
        target_user["behaviors"] = [b for b in target_user.get("behaviors", []) if b != "no_purchase"]
        return
    if _parse_calendar_period(query) is not None:
        return  # 달력 구간('올해 0건')은 롤링 창 anti-join 으로 컴파일 불가 — 양보(드롭 경고가 고지)
    if isinstance(aggregates, list):
        target_user["aggregate_conditions"] = _strip_zero_order_count_aggregates(aggregates)
    _append_unique(target_user.setdefault("behaviors", []), "no_purchase")


# "구매(주문) 이력은 있지만 (결제/구매) 금액 합계가 0원" — 주문은 존재하되 결제 합계가 0. 무주문(no_purchase)이
# 아니라 '주문 있고 SUM=0'이므로 결제금액 집계(purchase_amount = 0 → HAVING SUM(PAYMENT_AMT)=0)로 컴파일한다.
# GROUP BY 서브쿼리는 주문행 있는 회원만 포함하므로 '구매 이력 있음'이 자동 보장된다(COUNT=0 공집합과 달리
# SUM=0 은 표현 가능). '구매했지만/구매는 있으나/주문 이력은 있는데' 등 구매 존재 단언이 있을 때만 발동해
# 모호한 '구매 금액 0원'(무주문 동일시 정책 필요)과 구분한다([[unsupported-intent-gate]]).
_PURCHASE_EXISTS_ASSERT_RE = re.compile(
    r"(?:구매|구입|주문)(?:이력|내역)?(?:은|는|이|가|를|도)?(?:있|했지만|했으나|했는데|하였)"
)


def _apply_zero_amount_with_purchase_filter(query: str, plan: dict[str, Any]) -> None:
    """'구매 이력은 있지만 결제금액 합계가 0원' → 결제금액 집계 =0(HAVING SUM(PAYMENT_AMT)=0) 조건 주입.

    0원 게이트(_apply_unsupported_intent_gate)보다 먼저 실행돼 aggregate_conditions 를 채워두면, 그 게이트가
    '구매 금액 0원'을 모호(무주문 정책 필요)로 막지 않는다 — 여기서 '구매 존재'가 명시됐기 때문이다."""
    if not _has_zero_amount_purchase_condition(query):
        return
    if "캠페인" in query:
        return  # 캠페인 귀속 금액 0 은 no_buy_response 트랙 소유
    compact = query.replace(" ", "").casefold()
    if _PURCHASE_EXISTS_ASSERT_RE.search(compact) is None:
        return
    target_user = plan.setdefault("target_user", {})
    aggregates = target_user.setdefault("aggregate_conditions", [])
    if not isinstance(aggregates, list):
        aggregates = target_user["aggregate_conditions"] = []
    if any(isinstance(c, dict) and c.get("metric_id") == "purchase_amount" for c in aggregates):
        return  # 이미 결제금액 임계가 있으면 중복 주입 금지
    aggregates.append({
        "metric_id": "purchase_amount",
        "operator": "=",
        "threshold": 0,
        "window_days": _parse_recent_window_days(query),
        "label": "결제금액 합계 0원",
    })


# 캠페인 반응 '횟수' 임계값: "(최근 N개월 캠페인 중) 두 번/2회 이상 반응한". 캠페인 반응 EXISTS(≥1회)와
# 달리 반응한 서로 다른 캠페인 수를 세어(HAVING COUNT(DISTINCT 캠페인)) 임계값과 비교한다. '최근 N개월'
# 창은 반응 팩트에 범용 반응일자 컬럼이 없어 캠페인 마스터(Z_CAMPAIGN) 시작일로 건다. '캠페인'+'반응'이 함께
# 있고 횟수 임계어가 있을 때만 발동해 '구매 2회 이상'(주문 집계 order_count) 과 갈린다.
# 숫자 또는 고유어 수사(한~열) + 횟수 단위(번/회/차례/건) + 비교어. 배수어/금액과 달리 순수 횟수만 본다.
# 고유어 수사→값·정규식은 native_count 타입(_THRESHOLD_NUMBER_KINDS)이 소유한다.
_CAMPAIGN_FREQ = _compile_threshold(_ThresholdSpec("native_count", r"번|회|차례|건"))
_CAMPAIGN_FREQ_COUNT_PATTERN = _CAMPAIGN_FREQ.pattern


def _apply_campaign_response_frequency_filter(query: str, plan: dict[str, Any]) -> None:
    """'최근 N개월 캠페인 중 K번 이상 반응한'을 캠페인 반응 횟수 조건(campaign_response_frequency)으로 해석한다.

    build_campaign_response_frequency_targets_sql_candidate 가 반응 팩트를 회원별로 집계
    (GROUP BY MBR_NO HAVING COUNT(DISTINCT 캠페인) op K)해 실추출하고, Z_CAMPAIGN 시작일로 '최근 N개월
    캠페인' 창을 건다. 성별/연령/등급 등 회원 속성은 compile_member_target_conditions 로 같은 SQL 에 AND 결합."""
    compact = query.replace(" ", "").casefold()
    if "캠페인" not in compact or "반응" not in compact:
        return
    match = _CAMPAIGN_FREQ_COUNT_PATTERN.search(query) or _CAMPAIGN_FREQ_COUNT_PATTERN.search(compact)
    if match is None:
        return
    parsed = _CAMPAIGN_FREQ.parse(match)
    if parsed is None:
        return
    operator, count = parsed
    operator_word = match.group("op")  # 라벨은 한글 어구('이상')를 그대로 쓴다
    plan.setdefault("target_user", {})["campaign_response_frequency"] = {
        "operator": operator,
        "count": int(count),
        "window_days": _parse_recent_window_days(query),
        "label": f"캠페인 {int(count)}회 {operator_word} 반응",
    }


# 캠페인 '귀속 구매금액' 임계값: "캠페인 구매금액 20만원 이상"/"캠페인을 통해 20만원 이상 구매한".
# 반응 팩트(MCS_CAMP_MBR_RSPN_FT)에는 반응 Y/N 외에 캠페인 귀속 집계 측정값(BUY_AMT)이 있어, 캠페인
# 문맥이 붙은 구매금액은 전 생애 주문 합(aggregate_conditions purchase_amount, ORDERHEADERMALL)이 아니라
# 이 컬럼의 회원별 합계(HAVING SUM(BUY_AMT))로 걸어야 의미가 맞다. 금액 단위(원/배수어)만 보고 횟수
# 단위(번/회/건)는 보지 않아 반응 '횟수'(campaign_response_frequency)와 갈린다. '누적 구매금액'처럼
# 캠페인과 지표 사이에 다른 수식어가 끼면 캠페인 문맥으로 보지 않는다(연결 조사/통해/보고/반응만 허용).
_CAMPAIGN_BUY_AMOUNT_METRIC_PATTERN = re.compile(
    r"캠페인(?:을|를|에서|으로|에|의)?(?:통해|통한|보고|반응|후)?(?:한)?(?:구매|결제)한?금액"
)
# 캠페인 귀속 구매금액 임계: korean_amount_bare 타입('10만'·'10만원'·'10원')으로 regex 조각 + 값 파서를
# 함께 생성한다. 단독 임계(_CAMPAIGN_AMOUNT_THRESHOLD_PATTERN)는 metric 뒤 창에서 search, 동사형 패턴은
# 문맥(통해/보고 … 구매) 사이에 같은 조각(_CAMPAIGN_BUY_AMOUNT.regex)을 임베드한다.
_CAMPAIGN_BUY_AMOUNT = _compile_threshold(_ThresholdSpec("korean_amount_bare", r"원", sep=""))
_CAMPAIGN_AMOUNT_THRESHOLD_PATTERN = _CAMPAIGN_BUY_AMOUNT.pattern
_CAMPAIGN_BUY_VERB_PATTERN = re.compile(
    r"캠페인(?:을|를|에서|으로|에)?(?:통해|통한|보고|후)"
    + _CAMPAIGN_BUY_AMOUNT.regex
    + r"(?:어치)?(?:을|를)?(?:구매|구입|결제|산|샀)"
)


def _apply_campaign_buy_amount_filter(query: str, plan: dict[str, Any]) -> None:
    """'캠페인 (귀속) 구매금액 N원 이상/이하'를 campaign_buy_amount 조건으로 해석한다.

    build_campaign_response_frequency_targets_sql_candidate(캠페인 팩트 집계 빌더)가 반응 팩트를
    회원별로 집계(GROUP BY MBR_NO HAVING SUM(BUY_AMT) op N)해 실추출한다. 같은 어구를 이중 파싱한
    누적 구매 금액 조건(임계값·연산자 동일)은 걷어낸다 — 전 생애 주문 합으로 걸면 캠페인과 금액이
    연결되지 않아 의미가 달라진다. 구매반응 EXISTS(buy_response)도 리던던트라 걷어낸다(집계 그룹
    존재 자체가 구매반응 1회 이상을 함의한다)."""
    compact = query.replace(" ", "").casefold()
    metric = _CAMPAIGN_BUY_AMOUNT_METRIC_PATTERN.search(compact)
    if metric is not None:
        threshold_match = _CAMPAIGN_AMOUNT_THRESHOLD_PATTERN.search(compact, metric.end(), metric.end() + 24)
    else:
        threshold_match = _CAMPAIGN_BUY_VERB_PATTERN.search(compact)
    if threshold_match is None:
        return
    parsed = _CAMPAIGN_BUY_AMOUNT.parse(threshold_match)  # korean_amount_bare 타입: (op, 금액)
    if parsed is None:
        return
    operator, amount = parsed
    if amount <= 0:
        return
    if float(amount).is_integer():  # 캠페인 금액은 정수로 저장(도메인 관례)
        amount = int(amount)
    operator_word = threshold_match.group("op")  # 라벨은 한글 어구('이상')를 그대로 쓴다
    target_user = plan.setdefault("target_user", {})
    target_user["campaign_buy_amount"] = {
        "operator": operator,
        "amount": amount,
        "window_days": _parse_recent_window_days(query),
        "label": f"캠페인 구매금액 {_format_threshold(amount)}원 {operator_word}",
    }
    aggregates = target_user.get("aggregate_conditions")
    if isinstance(aggregates, list):
        target_user["aggregate_conditions"] = [
            condition for condition in aggregates
            if not (
                isinstance(condition, dict)
                and condition.get("metric_id") == "purchase_amount"
                and condition.get("operator") == operator
                and isinstance(condition.get("threshold"), (int, float))
                and float(condition["threshold"]) == float(amount)
            )
        ]
    responses = target_user.get("campaign_responses")
    if isinstance(responses, list):
        target_user["campaign_responses"] = [
            response for response in responses
            if not (isinstance(response, dict) and response.get("canonical") == "buy_response")
        ]


# 캠페인 '구매 건수/횟수'(귀속 구매 건수) 임계값: "캠페인 구매건수 2건 이상". 반응 팩트에서 구매반응(BUY)
# 캠페인 수(COUNT DISTINCT 캠페인)로, 전 생애 주문 건수(order_count, ORDERHEADERMALL)와 다르다 — 캠페인
# 문맥이 붙은 '구매 건수/횟수'는 캠페인 팩트 집계로 걸어야 의미가 맞다. 단위(건/회/번)만 보고 금액과 갈린다.
_CAMPAIGN_BUY_COUNT_METRIC_PATTERN = re.compile(
    r"캠페인(?:을|를|에서|으로|에|의)?(?:통해|통한|보고|반응|후)?(?:한)?(?:구매|결제)(?:건수|횟수)"
)


def _apply_campaign_buy_count_filter(query: str, plan: dict[str, Any]) -> None:
    """'캠페인 구매건수 K건 이상'을 campaign_buy_count 조건으로 해석한다(캠페인 팩트 집계 빌더가
    HAVING COUNT(DISTINCT 캠페인) op K 로 컴파일). 같은 어구를 전 생애 주문 건수(order_count)로 이중
    파싱한 aggregate_conditions 는 걷어낸다 — 캠페인과 건수가 연결되지 않아 의미가 달라진다."""
    compact = query.replace(" ", "").casefold()
    metric = _CAMPAIGN_BUY_COUNT_METRIC_PATTERN.search(compact)
    if metric is None:
        return
    comparisons = _parse_amount_comparison(compact[metric.end(): metric.end() + 16], r"건|회|번", unit_required=True)
    if not comparisons:
        return
    operator, count = comparisons[0]
    if count <= 0:
        return
    count = int(count)
    target_user = plan.setdefault("target_user", {})
    target_user["campaign_buy_count"] = {
        "operator": operator,
        "count": count,
        "window_days": _parse_recent_window_days(query),
        "label": f"캠페인 구매건수 {operator} {count}건",
    }
    aggregates = target_user.get("aggregate_conditions")
    if isinstance(aggregates, list):
        target_user["aggregate_conditions"] = [
            condition for condition in aggregates
            if not (
                isinstance(condition, dict)
                and condition.get("metric_id") == "order_count"
                and condition.get("operator") == operator
                and isinstance(condition.get("threshold"), (int, float))
                and float(condition["threshold"]) == float(count)
            )
        ]


# 셀 단위 비율 타겟: "발송 성공률은 높지만 구매율이 낮은 셀의 회원". '성공률/구매율'은 회원 플래그가
# 아니라 캠페인 셀 단위 비율 지표다 — 접촉성공 정규식('발송성공')이 '발송 성공률'의 부분문자열에 걸려
# 회원 EXISTS 로 강등되고, LLM 재작성은 '구매율 낮음'을 '미구매'(평생 무주문)로 극단화하는 오배정을
# 여기서 바로잡는다. 명시 %("성공률 90% 이상")는 그대로, '높은/낮은' 막연어는 설정 기본 임계
# (vague_high_default/vague_low_default)로 컴파일한다.
# 명시 % 접미(지표어 뒤에 임베드): percent 타입 스펙으로 regex 조각 + 값 파서(0<v<=100 검증)를 함께 생성한다.
# 단위(%)는 optional, 앞에 조사(이/가/은/는/도)가 올 수 있고 공백 없이 붙는다(sep="").
_CELL_RATE = _compile_threshold(_ThresholdSpec("percent", r"%|퍼센트|프로", prefix=r"(?:이|가|은|는|도)?", sep="", unit_optional=True))
_CELL_RATE_EXPLICIT_SUFFIX = _CELL_RATE.regex
_CELL_SUCCESS_RATE_TERM = r"(?:발송|전송|접촉|도달)?성공률"
_CELL_BUY_RATE_TERM = r"구매(?:전환)?율"
_CELL_RATE_PATTERNS: dict[str, tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]] = {
    "success_rate": (
        re.compile(_CELL_SUCCESS_RATE_TERM + _CELL_RATE_EXPLICIT_SUFFIX),
        re.compile(_CELL_SUCCESS_RATE_TERM + r"(?:이|가|은|는|도)?높"),
        re.compile(_CELL_SUCCESS_RATE_TERM + r"(?:이|가|은|는|도)?낮"),
    ),
    "buy_rate": (
        re.compile(_CELL_BUY_RATE_TERM + _CELL_RATE_EXPLICIT_SUFFIX),
        re.compile(_CELL_BUY_RATE_TERM + r"(?:이|가|은|는|도)?높"),
        re.compile(_CELL_BUY_RATE_TERM + r"(?:이|가|은|는|도)?낮"),
    ),
}
_CELL_RATE_KO = {"success_rate": "발송 성공률", "buy_rate": "구매율"}


def _parse_cell_rate(compact: str, metric: str, high_default: float, low_default: float) -> dict[str, Any] | None:
    """공백 제거 텍스트에서 셀 비율 조건 하나를 찾는다(명시 % 우선, 없으면 높/낮 막연어 → 기본 임계)."""
    explicit_pattern, high_pattern, low_pattern = _CELL_RATE_PATTERNS[metric]
    match = explicit_pattern.search(compact)
    if match is not None:
        parsed = _CELL_RATE.parse(match)  # percent 타입: 0<v<=100 검증 포함, 벗어나면 None → 막연어로 폴백
        if parsed is not None:
            operator, value = parsed
            return {"operator": operator, "value": value, "inferred": False}
    if high_pattern.search(compact):
        return {"operator": ">=", "value": float(high_default), "inferred": True}
    if low_pattern.search(compact):
        return {"operator": "<=", "value": float(low_default), "inferred": True}
    return None


def _apply_cell_rate_target_filter(query: str, plan: dict[str, Any]) -> None:
    """'발송 성공률/구매율 임계(높은·낮은 포함)'를 셀 단위 비율 타겟(cell_rate_target)으로 해석한다.

    build_cell_rate_targets_sql_candidate 가 Z_CAMP_MBR 를 셀(CAMP_ID, CAMP_EXEC_NO, CELL_NODE_ID)로
    집계해 성공률(CONTAC_SUCC_YN 비중)·구매율(구매반응 회원 비중) HAVING 으로 셀을 고르고, 그 셀의
    발송 대상 회원을 타겟한다. 발동 시 오배정 산물을 걷어낸다 — '발송 성공률'의 부분문자열로 잡힌
    접촉성공 EXISTS(campaign_contact), '구매율 낮음'이 극단화된 no_purchase(평생 무주문)."""
    compact = query.replace(" ", "").casefold()
    config = _MEMBER_TARGET_FILTERS.get("cell_rate_targets", {})
    high_default = config.get("vague_high_default", 80)
    low_default = config.get("vague_low_default", 10)
    success = _parse_cell_rate(compact, "success_rate", high_default, low_default)
    buy = _parse_cell_rate(compact, "buy_rate", high_default, low_default)
    if success is None and buy is None:
        return
    label_parts = []
    for metric, condition in (("success_rate", success), ("buy_rate", buy)):
        if condition is None:
            continue
        operator_ko = {">=": "이상", ">": "초과", "<=": "이하", "<": "미만"}[condition["operator"]]
        suffix = "(기본 임계)" if condition["inferred"] else ""
        label_parts.append(f"{_CELL_RATE_KO[metric]} {_format_threshold(condition['value'])}% {operator_ko}{suffix}")
    target_user = plan.setdefault("target_user", {})
    target_user["cell_rate_target"] = {
        "success_rate": success,
        "buy_rate": buy,
        "label": "셀 " + " · ".join(label_parts),
    }
    if success is not None:
        responses = target_user.get("campaign_responses")
        if isinstance(responses, list):
            target_user["campaign_responses"] = [
                response for response in responses
                if not (isinstance(response, dict) and response.get("canonical") == "campaign_contact")
            ]
    if buy is not None:
        behaviors = target_user.get("behaviors")
        if isinstance(behaviors, list):
            target_user["behaviors"] = [behavior for behavior in behaviors if behavior != "no_purchase"]


# 자녀정보 등록 여부(CHILDREN_YN) 타겟: '자녀(정보) 등록/보유/있음'은 육아 관심(parent 페르소나)이
# 아니라 회원 속성(CHILDREN_YN Y/N)이다. 실컬럼은 children_registered eq_filter 가 소유한다. 정규화가
# '자녀'→parent(관심/페르소나, 회원 컬럼 미표현)로 삼켜 조건이 새므로, '등록/보유/있음' 문맥이 붙은
# 경우만 결정론으로 승격한다. 부정('없/미등록')은 exclude 로 `<> 'Y'`.
_CHILDREN_TERMS = ("자녀", "아이", "키즈")
# 부정을 먼저 판정한다 — '등록 안'/'없'.
_CHILDREN_NEG = r"(?:정보)?(?:가|이|를|을|은|는)?(?:없|미등록|미보유|등록안|등록하지\s*않|보유하지\s*않)"
_CHILDREN_POS = r"(?:정보)?(?:가|이|를|을|은|는)?(?:등록|보유|있|존재)"


# 자녀정보 등록 승격은 attribute_token 실행기(그룹 "children")가 담당한다.
# 문법·표면어는 _attribute_token_groups()["children"] + eq_filters surface_terms 가 소유한다.


_CHANNEL_CONSENT_TARGETS: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("app_push_optin", "app_push", ("앱푸시", "푸시", "apppush")),
    ("sms_optin", "sms", ("sms", "문자")),
    ("email_optin", "email", ("이메일", "email", "메일")),
    ("marketing_optin", None, ("마케팅", "정보활용")),
)
# 부정(거부)을 먼저 판정한다 — '동의하지 않'/'동의 안' 은 긍정 패턴('동의')의 부분문자열을 포함한다.
# 조사 뒤 수량 부사('수신에 모두 동의')를 허용한다 — 이전엔 '모두'가 조사와 '동의' 사이에 끼면
# 접미어가 깨져 동의 승격이 통째로 실패했고, 채널어가 선호/발송 채널로 남아 SQL 생성이 거부됐다.
_CONSENT_ADVERB = r"(?:는|도)?(?:모두|둘다|전부|다|각각)?"
_CONSENT_NEG_SUFFIX = r"(?:수신)?(?:에|을|를)?" + _CONSENT_ADVERB + r"(?:거부|미동의|동의안|동의하지|비동의)"
_CONSENT_POS_SUFFIX = r"(?:수신)?(?:에|을|를)?" + _CONSENT_ADVERB + r"동의"
# 여러 채널이 하나의 '수신 동의/거부'를 공유하는 나열형("SMS와 앱푸시 모두 수신동의")도 잡기 위한 간격.
# 채널어와 동의/거부 접미어 사이에 나열 연결어·수량 부사(모두/둘다)·다른 채널어만 있으면 허용한다.
# '로/으로 보내'(발송 채널) 같은 다른 문맥이 끼면 이 간격이 끊겨 매칭되지 않아 오탐을 막는다.
_CONSENT_CHANNEL_TERMS_ALL = tuple(term for _c, _ch, terms in _CHANNEL_CONSENT_TARGETS for term in terms)
_CONSENT_GROUP_GAP = (
    r"(?:와|과|,|、|·|ㆍ|‧|・|및|랑|이랑|그리고|또는|이나|나|/|모두|둘다|둘|전부|각각|다같이|"
    + "|".join(re.escape(t) for t in _CONSENT_CHANNEL_TERMS_ALL)
    + r")*"
)


def _consent_context_signals(text: str) -> dict[str, str]:
    """텍스트에서 '<채널> 수신 동의/거부' 신호를 {canonical: 극성('+'동의/'-'거부)} 으로 뽑는다.

    인접형("SMS 수신 동의")과 나열 공유형("SMS와 앱푸시 모두 수신 동의") 둘 다 지원한다(_CONSENT_GROUP_GAP).
    부정(거부)을 먼저 판정한다 — 거부 접미어가 긍정('동의')의 상위 문자열을 포함하기 때문."""
    compact = (text or "").replace(" ", "").casefold()
    signals: dict[str, str] = {}
    for canonical, _channel, terms in _CHANNEL_CONSENT_TARGETS:
        term_alt = "(?:" + "|".join(re.escape(term) for term in terms) + ")"
        if re.search(term_alt + _CONSENT_GROUP_GAP + _CONSENT_NEG_SUFFIX, compact):
            signals[canonical] = "-"
        elif re.search(term_alt + _CONSENT_GROUP_GAP + _CONSENT_POS_SUFFIX, compact):
            signals[canonical] = "+"
    return signals


def _apply_channel_consent_filter(query: str, plan: dict[str, Any]) -> None:
    """'앱푸시/SMS/이메일 수신 동의·거부' 문맥을 수신동의 회원 속성 조건으로 승격한다.

    동의는 target_user.lifecycle 에 consent canonical 을 넣어 eq_filters 규칙 엔진이 `= 'Y'` 로,
    거부/미동의는 exclude.lifecycle 로 `<> 'Y'` 로 컴파일한다. 채널어가 동의 문맥으로 쓰였으면
    선호 채널(preferred_channels)·캠페인 채널에서 제거해 미지원 조건 탈락(dropped)을 방지한다.
    "SMS와 앱푸시 모두 수신동의"처럼 채널들이 하나의 동의를 공유하는 나열형도 잡는다(_consent_context_signals)."""
    target_user = plan.setdefault("target_user", {})
    exclude = plan.setdefault("exclude", {})
    channel_by_canonical = {canonical: channel for canonical, channel, _ in _CHANNEL_CONSENT_TARGETS}
    removed_channels: set[str] = set()
    for canonical, polarity in _consent_context_signals(query).items():
        if canonical not in MEMBER_EQ_FILTERS:
            continue  # 레지스트리 파일 커스텀으로 빠졌다면 문맥 승격도 하지 않는다(컴파일 불가 방지)
        if polarity == "-":
            _append_unique(exclude.setdefault("lifecycle", []), canonical)
        else:
            _append_unique(target_user.setdefault("lifecycle", []), canonical)
        channel = channel_by_canonical.get(canonical)
        if channel:
            removed_channels.add(channel)
    if not removed_channels:
        return
    target_user["preferred_channels"] = [
        value for value in target_user.get("preferred_channels", []) if value not in removed_channels
    ]
    campaign = plan.setdefault("campaign_constraints", {})
    campaign["channels"] = [value for value in campaign.get("channels", []) if value not in removed_channels]


# 회원 Y/N 플래그(활동회원·블랙리스트) 문맥 판정. 표면어와 canonical 매핑을 결정론 필터와
# 재작성 게이트(_member_flag_signals)가 함께 쓴다. 이 canonical 들은 eq_filters 에 있어야
# compile_member_target_conditions 가 `= 'Y'`(포함)/`<> 'Y'`(exclude.lifecycle) 로 만든다
# ([[boolean-filters-dead-registry]] 참고).
_MEMBER_FLAG_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("active_member", ("활동회원", "활성회원", "액티브회원", "활동중인회원")),
    ("blacklisted", ("블랙리스트", "발송제외고객", "차단회원")),
    ("employee", ("임직원", "직원회원")),
    ("premium_member", ("프리미엄회원", "프리미엄고객", "프리미엄")),
    ("membership_member", ("멤버십회원", "멤버십가입고객", "멤버십가입")),
    ("sns_registered", ("sns가입", "소셜가입", "간편가입", "sns회원")),
)
# 부정(제외) 접미어 — '블랙리스트가 아니', '블랙리스트 제외', '활동회원이 아닌' 등. 없으면 포함(긍정)으로 본다.
# 소비어(회원/고객)와 조사를 건너뛰고 부정 동사에 닿는다.
_MEMBER_FLAG_NEG = r"(?:인|한|중인|상태)?(?:회원|고객|사람)?(?:가|이|은|는|를|을|도|이면)?(?:아니|아닌|제외|빼|말고|배제)"


# 회원 Y/N 플래그(활동회원·블랙리스트 등) 승격은 attribute_token 실행기(그룹 "member_flag")가 담당한다.
# 문법·표면어는 _attribute_token_groups()["member_flag"] + eq_filters surface_terms 가 소유한다(신호 감지는
# _member_flag_signals 가 같은 표면어를 재사용). 긍정→target_user.lifecycle(='Y'), 부정→exclude.lifecycle(<>'Y').


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


def _is_date_like_token(token: str) -> bool:
    """'2024년'·'3월'·'15일'·'2024-03'·'20240301' 처럼 날짜/기간을 뜻하는 토큰이면 True.

    구매 이력 상품명 추출('… 구매한 고객')에서 '구매' 앞이 날짜뿐이면(예: '3월에 구매한 고객')
    날짜가 상품명(purchase_object)으로 새어 상품 LIKE 를 무의미하게 만든다. 날짜는 구매 상품이 아니라
    구매 날짜 조건(purchase_date)이므로 상품 후보에서 제외한다."""
    # 숫자+년/월/일/분기/주 (뒤에 '에/의/부터/까지' 같은 조사가 붙어도 접두가 날짜면 날짜로 본다).
    if re.match(r"^\d{1,4}(?:년|년도|월|일|분기|주|주차)", token):
        return True
    # 순수 숫자/구분자(YYYYMMDD, 2024-03, 2024.03.01, 2024/03).
    if re.match(r"^\d[\d\-/.]*$", token):
        return True
    return False


# 스키마 검색어에서 뺄 '값' 토큰의 수량/기간/금액 단위. 날짜(_is_date_like_token)에 더해 숫자+단위
# (10명·100만원·3개월·5건 등)를 값으로 본다. 이들은 이미 결정론 추출기가 구조화 필터(purchase_date/
# result_limit/aggregate_conditions 등)로 뽑아 SQL 조건이 되므로, RAG 스키마(테이블·컬럼 의미) 검색어에는
# 노이즈일 뿐이다(제안 #2 — 검색어는 스키마 의미만).
_SCHEMA_QUERY_VALUE_UNIT = (
    "개월", "주차", "주간", "년도", "분기", "시간", "달", "주", "년", "월", "일",
    "조", "억", "만원", "천원", "만", "천", "원", "명", "건", "개", "회", "번", "차례", "퍼센트", "포인트", "점", "%",
)
# 숫자 + 단위 0개 이상(+조사/연산어). 단위를 반복 허용해 복합 금액('3천만원'=천·만·원)도 값으로 본다.
# 조사(에/의/부터/까지/동안/이내/간/째)나 임계 연산어(이상/이하/미만/초과)가 붙어도 접두가 숫자면 값으로
# 본다(예: '90일간', '100만원이상'). 단위는 반드시 접두 숫자 뒤에서만 매칭돼 일반 단어('일요일')엔 영향 없다.
_SCHEMA_QUERY_VALUE_RE = re.compile(
    r"^\d[\d,\.]*\s*(?:" + "|".join(re.escape(unit) for unit in _SCHEMA_QUERY_VALUE_UNIT) + r")*"
    r"(?:이상|이하|미만|초과|부터|까지|동안|이내|간|째|에|의|로|으로)?$"
)


def _is_schema_query_value_token(token: str) -> bool:
    """RAG 스키마 검색에서 제외할 '값'(날짜/숫자/수량/기간/금액) 토큰이면 True."""
    stripped = token.strip().strip(",.")
    if not stripped:
        return False
    return _is_date_like_token(stripped) or bool(_SCHEMA_QUERY_VALUE_RE.match(stripped))


def _schema_retrieval_query(text: str) -> str:
    """자연어 검색 문장에서 값 토큰(날짜/숫자/수량)을 제거해 '스키마 의미'만 남긴 검색어를 만든다.

    벡터/키워드 검색이 스키마(테이블·컬럼 의미)를 찾는 데 집중하도록, 이미 구조화 필터로 추출된
    날짜/숫자/기간/개수 리터럴을 검색어에서 뺀다(예: '2019년 2월 구매한 고객 조회' → '구매한 고객 조회').
    모든 토큰이 값이면(값만 있는 질의) 원문을 그대로 둔다(빈 검색어 방지)."""
    if not isinstance(text, str) or not text.strip():
        return text
    kept = [token for token in text.split() if not _is_schema_query_value_token(token)]
    return " ".join(kept) if kept else text


def _sanitize_purchase_object(value: str) -> str | None:
    if is_non_entity_candidate(value):
        return None
    tokens = []
    for token in re.findall(r"[0-9A-Za-z가-힣_+\-]+", value.casefold()):
        stripped_token = re.sub(r"(?:을|를)$", "", token)
        # 상품이 아닌 구매행동 수식어(첫/재/최근 구매, 많이/자주 등 수량·빈도 부사)는 명사형 매칭에서
        # 엉뚱한 LIKE(예: '많이 구입한' → PRODUCT_NAME LIKE N'%많이%')를 만들 수 있어 제외한다.
        # 장소·대상 지시어("이곳에서 구매한" — 앞 절의 브랜드/장소를 가리키는 조응 표현)도 상품명이 아니다
        # — 지시어를 걸러야 브랜드 계사절("브랜드가 X면서 … 이곳에서 구매한")이 브랜드 추출로 이어진다.
        if not stripped_token or stripped_token in _PURCHASE_VALUE_QUALIFIERS or stripped_token in {
            "사람", "고객", "사용자", "첫", "재", "최근", "최초", "최초로", "반복", "자주", "많이", "많은", "다수", "대량", "처음", "처음으로", "미",
            "이곳", "이곳에서", "그곳", "그곳에서", "저곳", "여기", "여기서", "여기에서", "거기", "거기서", "거기에서", "저기", "저기서", "해당", "동일", "같은",
            # '캠페인 구매 이력'의 '캠페인'은 상품명이 아니라 캠페인 반응(구매 반응) 문맥어다. 상품 LIKE
            # 로 새면 PRODUCT_NAME LIKE N'%캠페인%' 같은 무의미 매칭이 되므로 상품 후보에서 제외한다.
            "캠페인",
            # 전체/전부/모든/모두/평균 같은 전칭·집계 수식어는 상품명이 아니다. '전체 구매 회원 평균보다 높은'
            # 의 '전체'가 PRODUCT_NAME LIKE N'%전체%' 로 새어 그럴듯한 오답 SQL 이 나오던 것을 막는다.
            "전체", "전부", "모든", "모두", "평균", "평균값",
            # 비교·최상급·정렬 표현은 상품/브랜드/카테고리명이 아니라 query semantics가 소유한다.
            "가장", "제일", "적게", "적은", "높게", "낮게", "높은", "낮은", "큰", "작은",
            "최대", "최소", "최고", "최저", "상위", "하위", "마지막",
            # 중복 제거 지시어는 집계 방식(DISTINCT)이지 상품명이 아니다. '중복 없이 구매 회원 수'의
            # '없이'가 상품 LIKE 로 새면 정상 집계가 '상품 조건 미충족'으로 탈락한다.
            "중복", "없이", "고유", "유니크",
        }:
            continue
        # 날짜/기간 토큰은 상품이 아니라 구매 날짜 조건이므로 상품명 후보에서 뺀다(→ purchase_date 가 담당).
        if _is_date_like_token(stripped_token):
            continue
        # 수량/횟수·비교 수식어('2개 이상', '3회', '5건 이상')는 상품이 아니라 개수 조건이므로 뺀다
        # (→ '2개 이상 상품 구입' 의 '이상'/'2개' 가 PRODUCT_NAME LIKE 로 새는 것을 막는다).
        if stripped_token in _PURCHASE_SIGNAL_STOPWORDS or _QUANTITY_COUNT_TOKEN.match(stripped_token):
            continue
        tokens.append(stripped_token)
    if not tokens:
        return None
    return " ".join(tokens[-3:])[:40]


@functools.lru_cache(maxsize=1)
def _purchase_brand_names() -> tuple[str, ...]:
    """CRM_CM_PRODUCT 의 실제 브랜드명 스냅샷(프로세스당 1회 조회). DB 미연결/실패 시 빈 튜플.

    사용자가 특수문자를 생략한 브랜드 표기('알로루')를 DB 표기('알로&루')로 보정하는 데 쓴다."""
    try:
        from db_connections import run_read_query

        rows = run_read_query(
            "CRMDW",
            "SELECT DISTINCT BRAND_NAME FROM CRM_CM_PRODUCT WHERE BRAND_NAME IS NOT NULL AND BRAND_NAME <> ''",
        )
    except Exception:
        return ()
    names = []
    for row in rows:
        value = next(iter(row.values()), None) if isinstance(row, dict) else (row[0] if row else None)
        if isinstance(value, str) and value.strip():
            names.append(value.strip())
    return tuple(names)


def _normalize_product_term(value: str) -> str:
    """브랜드/상품명 비교용 정규화: 영숫자·한글만 남긴다('알로&루'→'알로루', 'A-BC '→'abc')."""
    return re.sub(r"[^0-9a-z가-힣]", "", value.casefold())


def _canonicalize_product_term(term: str) -> str:
    """구매 상품어 토큰을 DB 브랜드 표기로 보정한다(정규화 완전 일치만 — 부분일치 오탐 방지).

    '알로루 티셔츠' 같은 다중 토큰은 토큰별로 보정한다. 브랜드 스냅샷을 못 얻으면 원문 그대로."""
    names = _purchase_brand_names()
    if not names or not isinstance(term, str) or not term:
        return term
    by_normalized = {_normalize_product_term(name): name for name in names if _normalize_product_term(name)}
    tokens = [by_normalized.get(_normalize_product_term(token), token) for token in term.split()]
    return " ".join(tokens)[:40]


def _is_known_brand_term(term: str) -> bool:
    """상품어 '전체'가 실DB 브랜드명과 정규화 일치하면 True(브랜드 확정 → BRAND_NAME 단독 매칭 근거).

    '알로루 티셔츠' 같은 혼합 표현은 브랜드 단독이 아니므로 False(광역 컬럼 매칭 유지)."""
    if not isinstance(term, str) or not term:
        return False
    normalized = _normalize_product_term(term)
    return bool(normalized) and any(_normalize_product_term(name) == normalized for name in _purchase_brand_names())


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
    timings_ms: dict[str, float] | None = None,
    structuring_context: StructuringContext | None = None,
    query_structurer: QueryStructurer | None = None,
) -> dict[str, Any]:
    # 계측 dict 을 호출자가 넘길 수 있게 한다. 트레이스 엔드포인트는 이 dict 을 소유해, retrieve() 가
    # 중간 단계에서 예외로 죽어도 그때까지 채워진 단계별 시간을 읽어 "오류 전까지" 부분 트레이스를 만든다.
    timings_ms = timings_ms if timings_ms is not None else {}
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

    # BFF가 화면에서 선택한 채널을 원문 끝에 붙이더라도, 구조화·재작성·Query Plan·SQL 검증은
    # 사용자가 입력한 타겟팅 프롬프트만 사용한다. 원문 query는 감사 로그와 화면 표시용으로 보존한다.
    targeting_prompt = _targeting_prompt(query)

    stage_started_at = time.perf_counter()
    context = structuring_context or StructuringContext(
        current_date=date.today().isoformat(),
        timezone=os.getenv("GRAPH_RAG_TIMEZONE"),
    )
    structured_query = _structure_query(targeting_prompt, context, llm_model, query_structurer)
    timings_ms["query_structuring"] = _elapsed_ms(stage_started_at)

    # 파싱 전에 사용자 프롬프트를 타겟 조건 중심으로 재작성(룰/LLM)한다. 재작성본으로 파싱하되 원문은 보존한다.
    stage_started_at = time.perf_counter()
    prompt_normalization = normalize_prompt(
        targeting_prompt,
        parser=query_parser,
        llm_model=llm_model,
        prompt_dir=prompt_dir,
    )
    effective_query = prompt_normalization["normalized"]
    timings_ms["prompt_normalization"] = _elapsed_ms(stage_started_at)

    # 타겟팅 스코프면 SQL·추론(Query Plan)을 오디언스(타겟팅) 절로만 수행한다. 채널·발송·혜택 문구는
    # 파싱에서 제외해 타겟 조건만 SQL/트레이스에 반영한다(검색 스코프 원칙을 파싱까지 확장). 채널 절은
    # 검색 스코프·메시지 생성에서만 쓰인다. 타겟팅 절이 비면 전체 재작성본으로 폴백한다.
    scope = (retrieval_scope or "all").casefold()
    stage_started_at = time.perf_counter()
    if scope == "targeting":
        plan_scopes = split_prompt_scopes(effective_query, parser=query_parser, llm_model=llm_model, prompt_dir=prompt_dir)
        plan_query = (plan_scopes.get("targeting") or "").strip() or effective_query
        plan_query = _preserve_count_output_query(effective_query, plan_query)
    else:
        plan_query = effective_query
    # 타겟/채널 분리(2단계) 계측을 Query Plan 과 분리해 둔다 — 부분 트레이스에서 분리 단계와 계획 단계의
    # 실패를 구분해 귀속하기 위함(이 키가 있으면 2단계 완료, query_plan 키가 있으면 3~6단계 완료).
    timings_ms["prompt_scopes"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    planner_input = QueryPlannerInput(query=plan_query, structured_query=structured_query)
    query_plan = call_query_planner(
        build_query_plan,
        planner_input,
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
    _apply_union_condition(targeting_prompt, query_plan, normalization_rules)
    # 임계값·서로 다른 지표가 섞인 OR-of-conjunctions 는 논리식 컴파일러(feature flag)가 괄호·우선순위를
    # 보존해 하나의 SQL 로 만든다(성공 시 logical_expression 슬롯, 실패 시 fail-close 미지원).
    _apply_logical_expression(targeting_prompt, query_plan, normalization_rules)
    # 파싱에 실제 사용한 문장(타겟팅 절 또는 전체 재작성본)을 트레이스/응답에 노출한다.
    query_plan["planning_query"] = plan_query
    # 프롬프트 재작성기가 '많이 거주하는' 같은 집계 표현을 지울 수 있으므로(비결정적 LLM 재작성),
    # 파싱 문장 기준으로도 밀집 지역 타겟을 감지한다(이미 감지됐으면 동일 값으로 덮어써 무해).
    _apply_region_density_target(plan_query, query_plan)
    _apply_member_metric_ranking_target(plan_query, query_plan)
    # 부사형 구매 랭킹('많이 산 사람 상위 N명')도 재작성이 '많이'를 지울 수 있어 원문 기준으로 재감지한다
    # (이미 잡혔으면 동일 값으로 덮어써 무해).
    _apply_purchase_count_ranking_target(plan_query, query_plan)
    # 개수 지시('N명만')는 재작성기가 조사 '만'을 떼어 'N명'으로 만들면 파서가 못 잡아 개수 제한이 소실된다.
    # (재작성은 비결정적 LLM 이라 표현이 흔들림) 원문 프롬프트에서 다시 감지해 결과 행수 제한을 확정한다
    # (이미 잡혔으면 동일 값으로 덮어써 무해). union/밀집지역을 원문에서 재감지하는 것과 같은 이유.
    _apply_named_filter("result_limit", targeting_prompt, query_plan)
    # 캠페인 반응(발송/접촉 성공·오퍼·구매반응·쿠폰)은 오디언스 조건인데, 재작성·스코프 분리(LLM)가
    # '발송' 단어를 발송 채널로 오해해 타겟팅 절에서 떨어뜨릴 수 있다(예: '발송은 성공했지만' 소실).
    # 원문(발송 채널 접미어 제외) 기준으로 재감지해 복원한다 — union/result_limit 재감지와 같은 이유.
    _apply_campaign_response_filter(targeting_prompt, query_plan)
    # 쿠폰 의미(사용 여부/건수 임계/순위/비교/파생)도 원문 기준으로 재확정한다 — 위 재감지가 다시 붙인
    # coupon_used 를, JSON 기반 판정으로 재조정(임계/순위/비교/파생이면 미지원으로 교체)한다(멱등).
    _apply_coupon_semantics(targeting_prompt, query_plan)
    # 상품/브랜드 구매 이력(purchase_object)도 원문 기준으로 재감지한다 — 비결정적 LLM 질의계획이 브랜드
    # 값을 손상('알로루'→'알로&루')시키거나 통째로 드롭하는 사례가 잦아, 결정론 추출로 덮어써 복원한다
    # (campaign_response 재감지와 같은 이유). _apply_purchase_object_filter 는 매칭이 있을 때만 값을 쓰므로
    # (없으면 무동작) 유효한 값을 지우지 않고, 구매 동사 없는 장바구니 문맥('담은')엔 걸리지 않아 안전하다.
    _apply_purchase_object_filter(targeting_prompt, query_plan.setdefault("target_user", {}))
    # LLM 병합이 구매 존재/기간 슬롯을 누락해도 실제 파싱 문장으로 실행 직전에 재확정한다. 이 슬롯이
    # 상대기간 집계 정규화(P{N}D)의 결정론적 근거이므로 집계 후처리보다 먼저 실행한다.
    _apply_core_membership_semantics(plan_query, query_plan)
    # 위 재확정(구매 존재/상품 슬롯 복원)은 순위 절의 어구까지 오디언스 조건으로 되살린다. 파생 엔터티
    # 집합 조건이 소유한 슬롯을 여기서 다시 회수하지 않으면, 같은 어구가 두 번 컴파일돼 rules 는 되고
    # auto(UI 경로)만 실패한다. 재파싱은 결정론이라 멱등하다.
    _apply_entity_set_condition(plan_query, query_plan)
    _normalize_aggregation_axis_filters(query_plan)
    _normalize_purchase_aggregation_request(query_plan)
    # '최근 N개월 캠페인 K번 이상 반응'(반응 횟수)도 원문 기준으로 재감지한다 — 재작성/스코프 분리가 횟수·기간
    # 어구를 흔들 수 있어 결정론 조건을 복원한다(campaign_responses 재감지와 같은 이유).
    _apply_campaign_response_frequency_filter(targeting_prompt, query_plan)
    # '캠페인 구매금액 N원'(귀속 금액)도 원문 기준으로 재감지한다 — 재작성이 캠페인 문맥을 지우면
    # 전 생애 누적 금액으로 격하되므로 결정론 조건을 복원한다(횟수 재감지와 같은 이유).
    _apply_campaign_buy_amount_filter(targeting_prompt, query_plan)
    # '성공률/구매율'(셀 비율)도 원문 기준으로 재감지한다 — 재작성이 '성공률 높음→발송 성공',
    # '구매율 낮음→미구매'로 극단화한 오배정을 걷어내고 셀 비율 조건을 복원한다. 위의
    # campaign_responses 재감지가 원문 '발송성공률'에서 접촉성공을 다시 세우므로 반드시 그 뒤에 실행.
    _apply_cell_rate_target_filter(targeting_prompt, query_plan)
    # 장바구니 '존재' 표현도 원문 기준으로 재감지한다(재작성/스코프 분리가 카트 절을 지우는 것 방지, 멱등).
    _apply_named_filter("cart_presence", targeting_prompt, query_plan)
    # 장바구니 '부재'도 원문 기준으로 재감지한다(멱등; 오파싱된 cart_abandoner 걷어내기).
    _apply_cart_absence_filter(targeting_prompt, query_plan)
    # 위 원문 복원 단계가 캠페인/쿠폰 표현을 다시 회원 오디언스 슬롯에 넣을 수 있다. 최종 출력이
    # 등록형 집계라면 분석 계약을 다시 적용해 그 슬롯을 소비하고 목록 SQL로의 회귀를 막는다.
    _apply_analytical_intent(targeting_prompt, query_plan, sql_schema)
    # 타겟팅 스코프면 plan_query 가 오디언스 절뿐이라 '재구매를 유도' 같은 캠페인 목적 절이 잘려
    # intent 가 recommend_campaign→find_user_segment 로 약화된다(장바구니 이탈 재구매 유도 등).
    # 목적 절이 살아있는 전체 재작성본으로 intent 를 재추론해 더 강한 캠페인 의도로만 승격한다.
    if scope == "targeting":
        _upgrade_intent_from_effective_query(query_plan, effective_query)
        # Query Plan 은 타겟팅 절만으로 빌드돼 내부 재분리에선 채널 절이 빈 문자열이 된다
        # (이미 분리된 텍스트를 다시 분리하므로). 응답 prompt_scopes 가 '실제 분리 결과'(타겟팅+채널)를
        # 보여주도록 파이프라인 레벨 분리(plan_scopes)로 스코프 필드를 되살린다 — BFF 는 채널 절이
        # 있어야 분리 성공으로 보고 타겟팅 절을 "타겟팅 프롬프트"로 표시한다.
        _attach_retrieval_scopes(query_plan, plan_scopes)
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
    # 스키마 검색 입력에서만 값 토큰(날짜/숫자/기간/개수)을 제거한다(제안 #2). SQL 생성용 keyword_query
    # (아래 build_sql_result 입력)에는 손대지 않는다 — 날짜 토큰이 커버리지 검증/LLM 폴백 컨텍스트에 필요하다.
    schema_query = _schema_retrieval_query(scoped_query)
    schema_terms = [term for term in scoped_terms if not _is_schema_query_value_token(term)] or scoped_terms
    scoped_keyword_query = " ".join(_unique_strings([schema_query, *schema_terms]))
    timings_ms["retrieval_query"] = _elapsed_ms(stage_started_at)

    stage_started_at = time.perf_counter()
    vector_hits = vector_search(
        query=schema_query,
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
            "retrieval_query": schema_query,
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
        # 의미 검증 게이트는 가공된 keyword_query 가 아니라 사용자 원문과 SQL 을 직접 대조해야 한다.
        original_query=targeting_prompt,
        prompt_dir=prompt_dir,
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

    # 실제 LLM 프롬프트 캡처를 노출 구조(query_plan/sql_result)에서 빼 result 상단으로 옮긴다 —
    # 메인 API 응답 비대화·프롬프트 유출을 막고, 트레이스 엔드포인트만 result 에서 읽어 표시한다.
    llm_query_plan_prompt = query_plan.pop("_llm_trace", None)
    _selected_candidate = sql_result.get("selected")
    llm_sql_prompt = _selected_candidate.pop("_llm_prompt", None) if isinstance(_selected_candidate, dict) else None

    api_response = build_recommendation_api_response(query, query_plan, sql_result, answer_response, message_generation, prompt_normalization)
    return {
        "query": query,
        "structured_query": structured_query.to_dict(),
        "llm_query_plan_prompt": llm_query_plan_prompt,
        "llm_sql_prompt": llm_sql_prompt,
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
        response = _openai_chat_create(client, 
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


# 프롬프트에서 어휘로 인식되는 타겟 도메인. SQL 후보가 하나도 안 나왔을 때(no_sql_candidates) 실패를
# 두 가지로 가르는 데 쓴다:
#   (a) 신호 자체가 없음        → "타겟 조건을 넣어 주세요"(기존 안내가 맞다)
#   (b) 도메인은 인식했는데 그 형태를 컴파일할 수단이 없음 → 지원 형태를 구체적으로 안내해야 한다
# (b)를 (a)로 안내하면 사용자는 이미 쓴 조건을 다시 쓰게 된다("장바구니에 10만원" → "장바구니 조건을
# 넣어 주세요"). 파서가 슬롯을 못 채우면 흔적이 안 남아 하위에서 둘을 구별할 수 없었기에, 어휘 인식
# 사실만 플랜에 남긴다(조건을 지어내지 않으므로 SQL 에는 영향이 없다).
_RECOGNIZED_DOMAINS: tuple[dict[str, str], ...] = (
    {
        "id": "cart",
        "lexicon": "cart_terms",
        "label": "장바구니",
        "supported": "담은 상품 개수·수량·금액 임계값, 보관 기간, 브랜드, 미결제 이탈",
    },
)


def _apply_recognized_domains(query: str, plan: dict[str, Any]) -> None:
    """프롬프트에 어휘가 등장한 타겟 도메인 id 목록을 플랜에 기록한다(조건은 만들지 않는다)."""
    compact = (query or "").replace(" ", "")
    plan["recognized_domains"] = [
        domain["id"]
        for domain in _RECOGNIZED_DOMAINS
        if any(term in compact for term in _lexicon_terms(domain["lexicon"]))
    ]


def _recognized_domains(query_plan: dict[str, Any]) -> list[dict[str, str]]:
    recognized = query_plan.get("recognized_domains")
    if not isinstance(recognized, list):
        return []
    return [domain for domain in _RECOGNIZED_DOMAINS if domain["id"] in recognized]


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


# 명시적 미지원(unsupported-intent) 사유 — 조건은 인식했으나 실DB SQL 로 컴파일할 수 없는 경우. 이들은
# SQL 안전 검증/의미 검증이 아니라 '실DB 조건 매핑' 단계에서 막힌 것이고, plan['unsupported'] 에 구체적
# 안내(message/clarification)를 이미 담고 있다. 사용자가 재입력(예: 쿠폰 '사용 여부')으로 풀 수 있어
# needs_clarification 으로 다루고, 무관한 일반 조건 라벨('혜택 유형 조건' 등)이 아니라 그 안내를 노출한다.
_UNSUPPORTED_INTENT_REASONS = frozenset({
    "coupon_usage_count_filter_unsupported",
    "coupon_usage_count_ranking_unsupported",
    "coupon_usage_count_metric_comparison_unsupported",
    "derived_metric_filter_unsupported",
    "coupon_semantic_preservation_failed",
})


def _describe_sql_failure(query_plan: dict[str, Any], sql_result: dict[str, Any]) -> str:
    """검증 SQL 실패를 실패 유형별로 구체적으로 설명한다(어디서 왜 막혔는지 사용자가 알 수 있게)."""
    reason = sql_result.get("failure_reason")
    selected = sql_result.get("selected") or {}
    unsupported_labels = sql_result.get("unsupported_condition_labels", [])

    # 명시적 미지원(쿠폰 건수/순위/비교/파생 등): 게이트가 만든 구체적 안내를 그대로 노출한다 — 무관한
    # 일반 조건 라벨('혜택 유형 조건')로 덮어쓰지 않는다(_UNSUPPORTED_INTENT_REASONS).
    if reason in _UNSUPPORTED_INTENT_REASONS:
        unsupported = query_plan.get("unsupported") if isinstance(query_plan.get("unsupported"), dict) else {}
        message = (unsupported.get("message") or "").strip()
        clarification = (unsupported.get("clarification") or "").strip()
        if message and clarification and clarification != message:
            return f"{message} {clarification}"
        return message or clarification or "요청하신 조건은 아직 실DB 타겟 추출로 지원되지 않습니다."

    if reason == "query_plan_required_conditions_missing":
        # 집합식/계산식/의미해석 중 무엇이 확정 안 됐는지 짚어 "어디서" 막혔는지 문구에도 반영한다.
        kind = _missing_condition_kind(sql_result)
        questions = sql_result.get("clarification_questions") or []
        if questions:
            lead = f"{kind[1]} 해석을 위해 " if kind else "SQL 생성을 위해 "
            return lead + "조건 확인이 필요합니다: " + " / ".join(str(q) for q in questions)
        return f"SQL 생성을 위해 필요한 조건이 부족합니다. {_SUPPORTED_CONDITION_HINT} 같은 타겟 조건을 추가해 주세요."

    if reason == "semantic_verification_failed":
        # 의미 검증 게이트가 원문↔SQL 불일치(드롭/반전 등)를 확신 → 틀린 SQL 출고 대신 확인 요청.
        questions = sql_result.get("clarification_questions") or _semantic_verification_clarifications(
            (sql_result.get("semantic_verification") or {}).get("issues", [])
        )
        if questions:
            return "생성된 SQL이 원문 의도와 다르게 반영된 부분이 있어 확인이 필요합니다: " + " / ".join(str(q) for q in questions)
        return "생성된 SQL이 원문 의도를 충실히 반영하지 못한 것으로 판단돼 확인이 필요합니다. 조건을 더 명확히 입력해 주세요."

    if unsupported_labels:
        # 요청 조건 중 실DB 타겟 추출로 아직 매핑되지 않은 것(관심사·행동·가격민감도 등)이 원인.
        return ("요청하신 조건 중 다음은 아직 실DB 타겟 추출로 지원되지 않아 검증 SQL을 만들지 못했습니다: "
                + ", ".join(unsupported_labels) + f". 지원되는 조건({_SUPPORTED_CONDITION_HINT})으로 바꾸거나 조합해 주세요.")

    if reason in ("no_sql_candidates", "recognized_domain_unsupported"):
        recognized = _recognized_domains(query_plan)
        if recognized:
            # 도메인은 인식했으니 "조건을 못 찾았다"고 하면 안 된다 — 어떤 형태가 되는지를 알려준다.
            return ("입력에서 " + "·".join(domain["label"] for domain in recognized)
                    + " 조건은 인식했지만, 요청하신 형태는 아직 실DB 타겟 추출로 지원되지 않습니다. 현재 지원되는 형태: "
                    + " / ".join(f"{domain['label']} — {domain['supported']}" for domain in recognized) + ".")
        return f"입력에서 타겟 조건을 찾지 못해 SQL을 만들지 못했습니다. {_SUPPORTED_CONDITION_HINT} 같은 타겟 조건을 넣어 주세요."

    if reason == "aggregation_validation_failed":
        errors = selected.get("aggregation_validation", {}).get("errors", [])
        detail = "; ".join(
            str(error.get("message")) for error in errors
            if isinstance(error, dict) and error.get("message")
        )
        return "생성된 SQL이 구조화된 집계 요구사항을 충족하지 않아 실행을 차단했습니다" + (f": {detail}" if detail else "") + "."

    if reason == "intent_sql_contract_failed":
        issues = selected.get("intent_sql_contract", {}).get("issues", [])
        detail = "; ".join(
            str(issue.get("message")) for issue in issues
            if isinstance(issue, dict) and issue.get("message")
        )
        return "생성된 SQL의 결과 shape나 집계·랭킹 의미가 QueryIntent와 달라 실행을 차단했습니다" + (f": {detail}" if detail else "") + "."

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

    if reason == "semantic_conditions_not_extracted":
        return "요청에 필수 조건이 있지만 Query Plan이 SQL 검증 조건으로 추출하지 못해 출고를 차단했습니다. 조건의 대상과 포함·제외 방향을 확인해 주세요."

    if reason in {"semantic_conditions_not_covered", "semantic_condition_polarity_mismatch"}:
        delivery = sql_result.get("delivery_validation") or selected.get("delivery_validation") or {}
        missing = delivery.get("missing_conditions") or delivery.get("polarity_mismatches") or []
        labels = [str(item.get("domain") or item) for item in missing]
        return "생성된 SQL이 핵심 행동 조건의 근거 또는 포함·제외 방향을 증명하지 못해 출고를 차단했습니다" + (
            ": " + ", ".join(_unique_strings(labels)) if labels else ""
        ) + "."

    if reason == "query_result_grain_mismatch":
        delivery = sql_result.get("delivery_validation") or selected.get("delivery_validation") or {}
        return ("질문의 기대 결과 단위와 SQL 결과 단위가 일치하지 않아 출고를 차단했습니다: "
                f"expected={delivery.get('expected_grain')}, actual={delivery.get('actual_grain')}.")

    if reason == "targeting_result_member_id_missing":
        return "타겟팅 SQL이 회원 ID 집합을 반환하지 않아 대상 인원수와 회원 목록을 안전하게 구성할 수 없습니다."

    if reason == "targeting_result_member_projection_missing":
        return "타겟팅 SQL의 결과 컬럼에 MEMBER_NO AS CUST_ID가 없어 출고를 차단했습니다."

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


# 타겟 SQL 실패가 발생할 수 있는 파이프라인 단계(실행 순서대로). failure_reason 은 "왜"(=message)를
# 설명하지만, 사용자는 "어디서" 막혔는지도 한 눈에 알아야 한다(집합식/조건 인식에서 막혔나, SQL 안전
# 검증에서 막혔나). failure_reason 을 아래 단계로 승격해 프론트가 단계 배지·스텝퍼로 그대로 노출한다.
_FAILURE_STAGE_SEQUENCE: tuple[dict[str, str], ...] = (
    {"code": "condition_recognition", "label": "타겟 조건 인식"},
    {"code": "real_db_mapping", "label": "실DB 조건 매핑"},
    {"code": "sql_safety_validation", "label": "SQL 안전 검증"},
    {"code": "aggregation_validation", "label": "집계 요구 검증"},
    {"code": "condition_coverage", "label": "조건 반영 검증"},
    {"code": "intent_scope", "label": "요청 의도 검증"},
    {"code": "semantic_verification", "label": "의미 검증"},
)

# failure_reason → 단계 code. 새 failure_reason 을 추가하면 여기에도 매핑을 넣어야 프론트에 단계가 뜬다.
_FAILURE_REASON_TO_STAGE: dict[str, str] = {
    # 집합식/계산식/의미해석이 확정 안 되면 required_conditions_missing 으로, 그 외 조건 미인식은
    # no_sql_candidates 로 떨어진다 — 둘 다 '조건 인식' 단계다(세부 라벨은 _refine_stage_label 로 가른다).
    "no_sql_candidates": "condition_recognition",
    "recognized_domain_unsupported": "condition_recognition",
    "query_plan_required_conditions_missing": "condition_recognition",
    "semantic_conditions_not_extracted": "condition_recognition",
    "real_db_unsupported_conditions": "real_db_mapping",
    # 명시적 미지원(쿠폰 건수/순위/비교/파생·의미보존 실패): 조건은 인식했으나 실DB 로 매핑 불가 —
    # SQL 안전 검증/의미 검증이 아니라 '실DB 조건 매핑' 단계에서 막힌 것으로 스텝퍼에 정직하게 표시한다.
    "coupon_usage_count_filter_unsupported": "real_db_mapping",
    "coupon_usage_count_ranking_unsupported": "real_db_mapping",
    "coupon_usage_count_metric_comparison_unsupported": "real_db_mapping",
    "derived_metric_filter_unsupported": "real_db_mapping",
    "coupon_semantic_preservation_failed": "real_db_mapping",
    "sql_guard_failed": "sql_safety_validation",
    "aggregation_validation_failed": "aggregation_validation",
    "intent_sql_contract_failed": "intent_scope",
    "query_plan_conditions_missing": "condition_coverage",
    "semantic_conditions_not_covered": "condition_coverage",
    "semantic_condition_polarity_mismatch": "semantic_verification",
    "critical_conditions_dropped": "semantic_verification",
    "critical_semantic_issue": "semantic_verification",
    "query_result_grain_mismatch": "intent_scope",
    "targeting_result_member_id_missing": "intent_scope",
    "targeting_result_member_projection_missing": "intent_scope",
    "query_plan_unmentioned_conditions_added": "condition_coverage",
    "intent_scope_mismatch": "intent_scope",
    "semantic_verification_failed": "semantic_verification",
}

# 미확정 required 조건(query_plan_required_conditions_missing)의 종류별 세부 라벨.
# (missing_input_conditions[].path prefix, 단계 세부 라벨, 안내문에 쓸 종류 라벨).
# 같은 '조건 인식' 단계라도 집합식 파싱에서 막혔는지, 계산식/의미 해석에서 막혔는지 구분해 보여준다.
_MISSING_CONDITION_KINDS: tuple[tuple[str, str, str], ...] = (
    ("aggregation_request.", "집계 요구사항 확정", "집계 요구사항"),
    ("set_expressions.", "집합식 파싱", "집합식"),
    ("computed_metrics.", "계산식 해석", "계산식"),
    ("semantic_resolutions.", "의미 해석 확정", "의미 해석"),
)


def _missing_condition_kind(sql_result: dict[str, Any]) -> tuple[str, str] | None:
    """미확정 required 조건의 종류를 (단계 세부 라벨, 안내 종류 라벨)로 돌려준다(해당 없으면 None).

    집합식/계산식/의미해석이 확정되지 못해 SQL 이 막힌 경우, 어느 것이 원인인지 path prefix 로 판별한다.
    """
    paths = [str(condition.get("path", "")) for condition in sql_result.get("missing_input_conditions", [])]
    for prefix, stage_label, kind_label in _MISSING_CONDITION_KINDS:
        if any(path.startswith(prefix) for path in paths):
            return stage_label, kind_label
    return None


def _refine_stage_label(failure_reason: str, sql_result: dict[str, Any]) -> str | None:
    """같은 단계 안에서 실패 원인을 더 구체적으로 짚는 세부 라벨(없으면 None=기본 라벨).

    현재는 조건 확정 실패(required_conditions_missing)를 집합식/계산식/의미해석으로 세분한다.
    """
    if failure_reason != "query_plan_required_conditions_missing":
        return None
    kind = _missing_condition_kind(sql_result)
    return kind[0] if kind else None


def _classify_failure_stage(
    failure_reason: str | None,
    sql_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """실패 사유(failure_reason)를 파이프라인 단계로 승격한다(어디서 막혔는지 UI 노출용).

    반환: {"code", "label", "order"(1-base), "total", "reason"(원 코드),
           "pipeline"[전체 단계 목록]} — 프론트가 단계 배지·스텝퍼를 데이터 기반으로 그린다.
    sql_result 로 같은 단계 안의 세부 원인(집합식 파싱 등)을 라벨에 반영한다. 매핑이 없으면 None.
    """
    if not failure_reason:
        return None
    stage_code = _FAILURE_REASON_TO_STAGE.get(failure_reason)
    if stage_code is None:
        return None
    # 세부 라벨: 같은 순번(단계) 안에서 무엇이 막혔는지 더 구체적으로 짚어준다(예: '타겟 조건 인식' → '집합식 파싱').
    label_override = _refine_stage_label(failure_reason, sql_result or {})
    pipeline: list[dict[str, Any]] = []
    matched: dict[str, Any] | None = None
    for index, stage in enumerate(_FAILURE_STAGE_SEQUENCE):
        label = label_override if (stage["code"] == stage_code and label_override) else stage["label"]
        entry = {"order": index + 1, "code": stage["code"], "label": label}
        pipeline.append(entry)
        if stage["code"] == stage_code:
            matched = entry
    assert matched is not None  # stage_code 는 항상 시퀀스에 존재
    return {
        "code": matched["code"],
        "label": matched["label"],
        "order": matched["order"],
        "total": len(pipeline),
        "reason": failure_reason,
        "pipeline": pipeline,
    }


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
        "detected_intent": query_plan.get("detected_intent"),
        "capability_check": query_plan.get("capability_check"),
        "selected_route": query_plan.get("selected_route"),
        "sql": sql_result.get("sql"),
        # 의미 검증 등으로 출고가 막혔지만 생성은 된 SQL(표시 전용, 실행 안 함). 정상 출고 시엔 None.
        # 프론트는 sql 이 없고 blocked_sql 이 있으면 "생성된 SQL(검증 실패)"로 노출한다.
        "blocked_sql": sql_result.get("blocked_sql"),
        # 프롬프트가 명시한 결과 행수 제한(없으면 None = 전체). SQL 에는 방언별 TOP/LIMIT 로 이미 반영됨.
        "result_limit": sql_result.get("result_limit"),
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
        # 실패가 발생한 파이프라인 단계(어디서 막혔는지). {code,label,order,total,reason,pipeline}.
        # 성공이면 None — 프론트는 이 값이 있을 때만 "실패 단계" 배지·스텝퍼를 노출한다.
        "failure_stage": _classify_failure_stage(sql_result.get("failure_reason"), sql_result),
        # 의미 검증 게이트 판정(원문↔최종 SQL 직접 대조). {ran, faithful, issues} — 오탐 튜닝·디버깅용.
        "semantic_verification": sql_result.get("semantic_verification", {"ran": False}),
        "delivery_validation": sql_result.get("delivery_validation", {"is_satisfied": False}),
        "aggregation_request": sql_result.get("aggregation_request"),
        "aggregation_validation": sql_result.get("aggregation_validation", {"ran": False}),
        "intent_sql_contract": sql_result.get("intent_sql_contract", {"ran": False}),
        # 쿼리 성능 튜닝 자문: 실행 함정 findings + 권장 인덱스(비차단, SQL 은 그대로).
        "query_tuning": sql_result.get("query_tuning", {"findings": [], "recommended_indexes": []}),
        # ③ 결정론 드롭 경고: 원문 신호가 plan 에 안 잡힌 조건(비차단 자문 — 조용한 드롭을 시끄럽게).
        "dropped_signal_warnings": sql_result.get("dropped_signal_warnings", []),
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
    # 의미 검증 게이트 차단·명시적 미지원(쿠폰 건수/순위/비교/파생 등)은 '틀린 SQL' 이 아니라 '확인 필요' 다
    # — 재작성/입력 보완(예: 쿠폰 '사용 여부')으로 풀 수 있어 needs_clarification 으로 안내한다.
    if sql_result.get("failure_reason") in (
        "query_plan_required_conditions_missing", "semantic_conditions_not_extracted", "semantic_verification_failed",
    ) or sql_result.get("failure_reason") in _UNSUPPORTED_INTENT_REASONS:
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


def _walk_dict_values(value: Any, key: str) -> list[Any]:
    """중첩 JSON에서 같은 이름의 필드값을 수집한다(프롬프트용 스키마 범위 축소)."""
    found: list[Any] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key == key:
                found.append(child)
            found.extend(_walk_dict_values(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_dict_values(child, key))
    return found


def _llm_aggregation_response_errors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """LLM의 자기진술을 신뢰하지 않되 구조화 응답 계약 자체는 엄격히 검사한다."""
    errors: list[dict[str, Any]] = []
    required_lists = ("requirementMappings", "usedTables", "usedColumns", "assumptions", "unresolvedFields", "warnings")
    for field_name in required_lists:
        if not isinstance(payload.get(field_name), list):
            errors.append({
                "code": "INVALID_SQL_RESPONSE_SCHEMA",
                "message": f"SQL 생성 응답의 {field_name} 필드는 배열이어야 합니다.",
            })
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        errors.append({"code": "INVALID_SQL_RESPONSE_SCHEMA", "message": "confidence는 0~1 숫자여야 합니다."})
    unresolved = payload.get("unresolvedFields")
    if isinstance(unresolved, list) and unresolved:
        errors.append({
            "code": "UNRESOLVED_BUSINESS_RULE",
            "message": "SQL 생성 응답에 미해결 필드가 있습니다: " + ", ".join(str(value) for value in unresolved),
        })
    try:
        minimum_confidence = float(os.getenv("AGGREGATION_SQL_MIN_CONFIDENCE", "0.8"))
    except ValueError:
        minimum_confidence = 0.8
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and float(confidence) < minimum_confidence:
        errors.append({
            "code": "LOW_CONFIDENCE",
            "message": f"집계 SQL 신뢰도 {float(confidence):.2f}가 실행 기준 {minimum_confidence:.2f}보다 낮습니다.",
        })
    return errors


def _member_condition_predicate(canonical: str) -> str | None:
    """회원 조건 canonical → 실컬럼 술어. 슬롯 경로와 같은 렌더러를 써서 두 경로가 같은 SQL 을 낸다."""
    if canonical in MEMBER_EQ_FILTERS:
        return _member_eq_predicate(canonical)
    days = MEMBER_ACTIVITY_FILTERS.get(canonical)
    return _member_activity_predicate(days) if isinstance(days, int) else None


def _targeting_expression_tool_schema() -> dict[str, Any]:
    return targeting_expression_json_schema(_entity_set_config(), member_condition_canonicals())


def _build_llm_targeting_ir_candidate(
    query: str,
    query_plan: dict[str, Any],
    llm_model: str,
) -> dict[str, Any] | None:
    """LLM 에게 SQL 이 아니라 타겟팅 IR 을 받아 결정론 컴파일한다(1.5티어 폴백).

    자유 SQL 폴백보다 먼저 시도한다. 출력 공간이 닫힌 문법이라 (i) 회원 투영 누락, (ii) 없는 컬럼·값
    생성, (iii) 1:N 조인으로 인한 행 증폭이 표현 자체로 불가능하다 — 사후 의미검증에 기대지 않고
    생성 단계에서 형태를 보장한다. 검증에 실패하면 조용히 고치지 않고 후보를 포기한다(fail-close).
    """
    if not os.getenv("OPENAI_API_KEY"):
        return None
    config = _entity_set_config()
    canonicals = member_condition_canonicals()
    if not config or not canonicals:
        return None
    schema = _targeting_expression_tool_schema()
    vocabulary = {
        "member_filter": {name: meta.get("terms", [])[:4] for name, meta in canonicals.items()},
        "relations": sorted(str(name) for name in (config.get("relations") or {})),
        "entities": sorted(str(name) for name in (config.get("entities") or {})),
        "measures": sorted(str(name) for name in (config.get("measures") or {})),
    }
    system_prompt = "\n".join([
        "너는 자연어 타겟팅 요청을 아래 JSON 스키마의 '회원 집합 표현식'으로 변환한다. SQL 은 쓰지 않는다.",
        "규칙:",
        "- 스키마에 열거된 어휘(member_filter/relations/entities/measures)만 사용한다. 없는 값은 만들지 않는다.",
        "- 원문에 있는 조건만 넣는다. 성별·연령·지역 등을 임의로 추가하지 않는다.",
        "- '가장 많이 팔린 상품 N개' 같은 순위 집합은 relation.entitySet 으로 표현한다.",
        "- 회원 상태(정상/휴면) 기본 정책과 결과 컬럼은 시스템이 붙이므로 표현식에 넣지 않는다.",
        "- 이 문법으로 표현할 수 없으면 expression 없이 unsupported 에 사유만 적는다(억지로 근사하지 않는다).",
        "JSON 스키마:",
        json.dumps(schema, ensure_ascii=False),
        "사용 가능한 어휘:",
        json.dumps(vocabulary, ensure_ascii=False),
    ])
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps({"user_query": query}, ensure_ascii=False)},
    ]
    # 문법 위반은 결정론으로 판정되므로 그 사유를 되돌려 한 번만 교정 기회를 준다 — 모델 출력 흔들림이
    # 곧바로 '조건 미반영' 실패로 굳는 것을 막는다. 그래도 실패하면 조용히 고치지 않고 포기한다.
    for attempt in range(2):
        try:
            from openai import OpenAI

            _write_rag_llm_log("llm_targeting_ir_request", {"model": llm_model, "query": query, "attempt": attempt})
            response = OpenAI().chat.completions.create(
                model=llm_model, messages=messages, response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            payload = json.loads(content)
        except Exception as exc:  # noqa: BLE001 - 폴백 경로는 실패 시 다음 티어로 넘어간다
            _write_rag_llm_log("llm_targeting_ir_error", {"model": llm_model, "error": f"{type(exc).__name__}: {exc}"})
            return None

        expression = payload.get("expression")
        if isinstance(expression, dict):
            try:
                validate_targeting_expression(expression, config, canonicals)
            except TargetingExpressionError as exc:
                _write_rag_llm_log("llm_targeting_ir_invalid", {"query": query, "error": str(exc), "attempt": attempt})
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"표현식이 규칙을 위반했습니다: {exc}. 스키마와 어휘를 지켜 다시 작성하세요."},
                ]
                continue
            return _compile_targeting_ir_candidate(expression)
        _write_rag_llm_log("llm_targeting_ir_unsupported", {"query": query, "reason": payload.get("unsupported")})
        return None
    return None


def _compile_targeting_ir_candidate(expression: dict[str, Any]) -> dict[str, Any] | None:
    """검증된 타겟팅 IR → SQL 후보. 회원 투영·상태 정책은 여기(컴파일러)가 소유한다.

    생성 주체(LLM/규칙/테스트)와 무관하게 같은 계약을 강제하려고 분리했다 — 표현식이 무엇이든
    결과는 회원 집합이다.
    """
    config = _entity_set_config()
    canonicals = member_condition_canonicals()
    try:
        validate_targeting_expression(expression, config, canonicals)
        predicate = compile_targeting_expression(
            expression, config,
            member_predicate=_member_condition_predicate,
            member_alias=_member_alias(),
            member_key=_member_key_column(),
            relative_date=_member_dialect().char8_cutoff,
        )
    except TargetingExpressionError:
        return None

    labels = describe_targeting_expression(expression)
    select_columns = ["DISTINCT " + _member_key_select(), _member_grade_select()]
    if labels:
        select_columns.append(_sql_quote(",".join(_unique_strings(labels))) + " AS segment_label")
    where = [predicate]
    # 회원 상태 기본 정책은 표현식이 상태를 직접 지정하지 않은 경우에만 붙인다(슬롯 경로와 동일 규칙).
    state_canonicals = {name for name, meta in canonicals.items() if meta.get("category") in {"state", "activity"}}
    if not any(label in state_canonicals for label in labels):
        where.append(_member_active_state_predicate())
    ast = SelectAst(columns=select_columns, from_lines=[_member_from_clause()], where=_unique_strings(where))
    candidate = _select_ast_candidate(
        "sql_template:llm_targeting_ir",
        "LLM 타겟팅 IR 컴파일 SQL(결정론 컴파일러)",
        0.9,
        ast,
        "llm_targeting_ir",
    )
    candidate["targeting_expression"] = expression
    return candidate


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
            for key in (
                "intent", "target_user", "exclude", "campaign_constraints", "dimension_filters",
                "region_density_target", "aggregation_request",
            )
            if query_plan.get(key)
        }
        aggregation_request_payload = query_plan.get("aggregation_request")
        aggregation_request = None
        aggregation_request_errors = []
        aggregation_schema_context: list[dict[str, Any]] = []
        if isinstance(aggregation_request_payload, dict) and schema_path is not None:
            aggregation_request, aggregation_request_errors = parse_aggregation_request(
                aggregation_request_payload, schema_path, dialect=_member_dialect().name
            )
            referenced_tables = {
                str(value)
                for value in _walk_dict_values(aggregation_request_payload, "table")
                if isinstance(value, str) and value
            }
            aggregation_schema_context = SchemaMetadata.load(schema_path).prompt_context(referenced_tables)
        # 스키마 사실(회원 테이블/키/상태 술어/코드값 예시)과 방언은 레지스트리·어댑터에서 렌더한다 —
        # 프롬프트에 직접 박으면 DB 스왑 시 프롬프트도 고쳐야 한다(docs/operations/db_portability_audit.md §4-C).
        dialect = _member_dialect()
        dialect_title = {"tsql": "MSSQL(T-SQL)", "mysql": "MySQL/MariaDB", "postgres": "PostgreSQL"}.get(dialect.name, "ANSI SQL")
        row_limit_rule = "LIMIT 대신 TOP 사용" if dialect.name == "tsql" else "TOP 대신 LIMIT 사용"
        member_alias = _member_alias()
        target_entity = (
            aggregation_request.target_entity.casefold()
            if aggregation_request is not None and aggregation_request.target_entity
            else "customer"
        )
        code_examples = [
            str(value)
            for category in ("gender", "grade", "state")
            for value in [
                next(
                    (value for _c, (cat, _col, value) in MEMBER_EQ_FILTERS.items() if cat == category and "." in str(value)),
                    None,
                )
            ]
            if value
        ]
        system_prompt = "\n".join(
            [
                "너는 자연어 집계 요구사항을 SQL로 변환하는 전문 SQL 엔지니어다. 반드시 JSON "
                "{\"extracted_conditions\": {...}, \"sql\": \"...\", \"queryType\": \"aggregation|targeting\", "
                "\"requirementMappings\": [...], \"condition_verification\": [...], \"usedTables\": [...], "
                "\"usedColumns\": [...], \"assumptions\": [...], \"unresolvedFields\": [...], "
                "\"warnings\": [...], \"confidence\": 0.0, \"explanation\": \"...\"} 형식으로만 답한다.",
                "규칙:",
                f"- {dialect_title} SELECT 단일문만 생성한다. DML/DDL/임시테이블 금지, {row_limit_rule}.",
                f"- 허용 테이블만 사용한다: {allowed_list}",
                (
                    f"- 최종 대상이 customer이면 첫 컬럼은 회원키다: SELECT DISTINCT {member_alias}.{_member_key_column()} AS CUST_ID "
                    f"({_member_table()} 별칭 {member_alias})."
                    if target_entity == "customer"
                    else f"- 최종 대상은 {target_entity}다. aggregation_request.outputColumns와 groupings에 확정된 컬럼만 SELECT한다."
                ),
                (
                    f"- 발송 대상 customer이면 기본으로 {_member_active_state_predicate()} 조건을 넣는다(사용자가 휴면/탈퇴를 명시하면 예외)."
                    if target_entity == "customer"
                    else "- 분석 결과 요청에는 고객 발송 상태 조건을 임의로 추가하지 않는다."
                ),
                "- 코드 컬럼 저장값은 도메인 접두어를 포함한다(예: " + ", ".join(code_examples) + ")."
                if code_examples
                else "- 코드 컬럼 저장값은 카탈로그(값 인덱스)의 실값 표기를 그대로 따른다.",
                "- 사용자가 명시한 조건은 모두 WHERE 에 반영하고, 명시하지 않은 조건(성별/연령/지역 등)은 절대 추가하지 않는다.",
                (
                    "- SELECT 에 반영한 조건의 canonical 요약 라벨을 포함한다(조건 커버리지 검증용): 예) 'no_purchase' AS segment_label."
                    if target_entity == "customer"
                    else "- 일반 집계 결과에는 요청하지 않은 segment_label이나 회원 컬럼을 추가하지 않는다."
                ),
                "- 컨텍스트에 없는 테이블/컬럼을 지어내지 않는다. 확실한 SQL 을 만들 수 없으면 {\"sql\": null, \"explanation\": \"이유\"} 를 반환한다.",
                "- 테이블 요약의 ⚠️(0행/미적재) 경고가 있는 테이블은 조건 판정 기준으로 쓰지 않는다(빈 테이블 anti-join 은 전원 매칭 오류).",
                "자연어 조건 추출(SQL 변환 전에 반드시 수행):",
                "- extracted_conditions 에 다음 8개 항목을 명시적으로 추출한다: "
                "target_entities(조회 대상 엔티티), period_conditions(기간 조건), "
                "aggregation(집계 대상과 집계 기준), order_by(정렬 기준), top_n(상위 N 조건), "
                "relationship_conditions(대상 간 관계 조건), deduplication_basis(중복 제거 기준), "
                "exclusion_conditions(취소·반품·탈퇴 등 제외 조건).",
                "- 원문에 없는 조건은 추론해 채우지 말고 해당 항목을 null 또는 빈 배열로 표시한다.",
                "- aggregation_request가 제공되면 이를 SQL 생성의 단일 진실 소스로 사용한다. unresolvedFields가 하나라도 있으면 sql=null을 반환한다.",
                "- filters.value의 ISO-8601 상대기간(P30D, P1M 등)은 미해결 값이 아니다. 대상 컬럼 타입과 현재 DB 방언에 맞춰 DB 현재 시점 기준 범위식으로 렌더링하고 임의의 기준일이나 날짜 리터럴로 고정하지 않는다.",
                "- '수/개수/건수/고객 수/상품 수'는 COUNT 대상과 DISTINCT 여부를 구분하고, '~별'은 GROUP BY로 구현한다.",
                "- 비율·점유율·전환율·증감률은 분자/분모 또는 비교 기간을 같은 grain으로 집계하고 NULLIF로 0 나눗셈을 처리한다.",
                "- '가장 많이', '상위 N', '베스트 N' 표현이 있으면 aggregation, order_by, top_n 을 모두 추출하고, "
                "SQL 에 집계, 내림차순 정렬, 순위 제한(TOP/LIMIT 또는 동등한 순위 조건)을 반드시 함께 포함한다.",
                "SQL 정합성 자가검증(생성 전 반드시 점검):",
                (
                    "- 결과 grain 은 회원 1행이다. 1:N 조인(주문/장바구니/캠페인반응 등)은 결과 행을 부풀리므로, 그런 조건은 JOIN 대신 EXISTS/IN 서브쿼리로 표현한다(DISTINCT 로 덮지 말 것 — DISTINCT 는 잘못된 조인을 가린다)."
                    if target_entity == "customer"
                    else "- 결과 grain은 aggregation_request의 targetEntity와 groupings다. 집계 전에 1:N/N:M 조인으로 원본 행이 증폭되지 않게 사전 집계 또는 검증된 유일키 조인을 사용한다."
                ),
                "- 집계(COUNT/SUM/AVG/MIN/MAX)와 비집계 컬럼을 함께 SELECT 하면 비집계 컬럼을 모두 GROUP BY 에 넣는다. 집계 후 조건은 HAVING, 집계 전 조건은 WHERE 로 분리한다(집계 함수는 WHERE 에 쓰지 않는다).",
                (
                    "- 회원별 지표 임계값(누적 구매금액/횟수 등)은 회원키로 GROUP BY 한 서브쿼리에서 HAVING 으로 거른 뒤 회원키로 조인/IN 한다 — 바깥에서 재집계하지 않는다(이중 집계 방지)."
                    if target_entity == "customer"
                    else "- 집계 결과 임계값은 HAVING에 두고, 최종 대상과 집계 grain이 다르면 집계 CTE를 만든 뒤 관계키로 다시 연결한다."
                ),
                "- 최근 N건/순위/누적값은 회원 단위로 접어야 하면 GROUP BY, 회원 내 순번이 필요하면 윈도 함수(ROW_NUMBER/RANK/SUM() OVER)를 쓴다 — 단순 TOP+ORDER BY 는 회원별 최근 N건을 주지 못한다.",
                "- 기간 A/B 를 비교할 때 두 기간의 기준 집합(모수)이 동일해야 한다 — 한 기간에만 존재하는 회원이 빠지지 않도록 회원 집합을 먼저 고정하고 각 기간 지표를 LEFT JOIN 한다.",
                f"- 날짜는 경계를 반열림 구간([시작, 끝))으로 잡고(BETWEEN 의 종료일 포함 주의), aggregation_schema_metadata의 실제 컬럼 타입/저장 형식에 맞춰 비교한다. 상대 기간 기준시각은 {dialect.now()} 앵커를 쓴다.",
                f"- LEFT JOIN 후 미매칭(NULL)을 의도대로 처리했는지 본다 — anti-join 은 NOT EXISTS/IS NULL, 합산은 {dialect.coalesce('x', '0')}. WHERE 에서 우변 테이블 컬럼을 조건에 쓰면 LEFT JOIN 이 INNER 로 바뀐다.",
                "조건 구현 검증(SQL 작성 후 반드시 수행):",
                "- condition_verification 에 extracted_conditions 의 각 명시 조건, 이를 구현한 SQL 절(SELECT/FROM/JOIN/WHERE/GROUP BY/HAVING/ORDER BY/TOP·LIMIT 등), 통과 여부를 기록한다.",
                "- requirementMappings에는 aggregation_request의 각 요구사항 id, 실제 SQL 절과 SQL 조각을 기록한다. 이 매핑은 애플리케이션 AST 검증기가 다시 확인한다.",
                "- 추출한 조건이 SQL 에서 하나라도 구현되지 않았거나 '가장 많이'·'상위 N'·'베스트 N'의 집계/정렬/순위 제한 중 하나라도 빠졌으면 그 SQL 을 출력하지 말고 다시 생성·검증한다.",
                "- 모든 명시 조건의 검증이 통과한 경우에만 sql 값을 출력한다. 컨텍스트나 스키마 한계로 구현할 수 없으면 sql=null 로 반환하고 explanation 에 미구현 조건과 이유를 적는다.",
            ]
        )
        user_prompt = json.dumps(
            {
                "user_query": query,
                "query_plan": plan_slim,
                "table_catalog": table_summaries,
                "aggregation_schema_metadata": aggregation_schema_context,
                "retrieval_context": context_lines,
            },
            ensure_ascii=False,
        )
        _write_rag_llm_log(
            "llm_sql_fallback_request",
            {"model": llm_model, "query": query, "query_plan": plan_slim, "context_line_count": len(context_lines)},
        )
        client = OpenAI()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        max_attempts = 1 + (aggregation_retry_count() if aggregation_request is not None else 0)
        last_content = "{}"
        last_payload: dict[str, Any] = {}
        last_validation: dict[str, Any] = {
            "valid": not aggregation_request_errors,
            "errors": [error.to_dict() for error in aggregation_request_errors],
        }
        for attempt in range(1, max_attempts + 1):
            response = _openai_chat_create(
                client,
                model=llm_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=messages,
            )
            last_content = response.choices[0].message.content or "{}"
            try:
                parsed_payload = json.loads(last_content)
                last_payload = parsed_payload if isinstance(parsed_payload, dict) else {}
                parse_errors: list[dict[str, Any]] = []
            except json.JSONDecodeError as exc:
                last_payload = {}
                parse_errors = [{
                    "code": "INVALID_SQL_RESPONSE_JSON",
                    "message": f"SQL 생성 응답 JSON 파싱 실패: {exc.msg}",
                }]
            sql_value = last_payload.get("sql")
            response_errors = [
                *parse_errors,
                *(_llm_aggregation_response_errors(last_payload) if aggregation_request is not None else []),
            ]
            if isinstance(sql_value, str) and sql_value.strip() and aggregation_request is not None and schema_path is not None:
                last_validation = validate_aggregation_sql(
                    aggregation_request, sql_value.strip(), schema_path, dialect=dialect.name
                )
                if response_errors:
                    last_validation = {
                        **last_validation,
                        "valid": False,
                        "errors": [*last_validation.get("errors", []), *response_errors],
                    }
            elif aggregation_request is not None:
                last_validation = {
                    "valid": False,
                    "errors": [*response_errors, {"code": "SQL_MISSING", "message": "생성 응답에 SQL이 없습니다."}],
                }
            _write_rag_llm_log(
                "llm_sql_fallback_response",
                {"model": llm_model, "query": query, "attempt": attempt, "content": last_content,
                 "aggregation_validation": last_validation},
            )
            if aggregation_request is None or last_validation.get("valid"):
                break
            if attempt < max_attempts:
                repair_prompt = json.dumps(
                    {
                        "task": "검증 오류를 모두 수정해 같은 JSON 응답 구조로 SQL을 다시 생성하라.",
                        "original_user_query": query,
                        "structured_aggregation_request": aggregation_request.to_dict(),
                        "previous_sql": sql_value,
                        "validation_errors": last_validation.get("errors", []),
                        "missing_requirements": [
                            mapping for mapping in last_validation.get("requirementMappings", [])
                            if not mapping.get("implemented")
                        ],
                        "available_schema": aggregation_schema_context,
                        "join_relationships": [item.get("foreignKeys", []) for item in aggregation_schema_context],
                        "business_rules": aggregation_request.business_rules,
                        "dbms": dialect_title,
                        "constraints": [
                            "단일 read-only SELECT/WITH SELECT만 사용", "스키마에 없는 테이블·컬럼 금지",
                            "WHERE/HAVING 분리", "집계 전 N:M 조인 금지", "미해결 항목이 있으면 sql=null",
                        ],
                    },
                    ensure_ascii=False,
                )
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": last_content},
                    {"role": "user", "content": repair_prompt},
                ]

        sql = last_payload.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            return None
        # 조건 커버리지 검증용 라벨을 결정론적으로 주입(LLM 지시 순응에 기대지 않는다).
        sql = _inject_segment_label(sql.strip(), query_plan) if target_entity == "customer" else sql.strip()
        candidate = _sql_candidate(
            "llm_sql:fallback",
            "LLM 생성 SQL(템플릿 미지원 형태 — 가드 검증 통과 시에만 채택)",
            0.5,
            sql,
            _template_tables(sql),
            "llm_generated",
        )
        candidate["explanation"] = last_payload.get("explanation")
        candidate["llm_response"] = last_payload
        candidate["aggregation_validation"] = last_validation if aggregation_request is not None else {"ran": False}
        candidate["generation_attempt_count"] = attempt
        candidate["generation_max_attempts"] = max_attempts
        candidate["dropped_conditions"] = []
        candidate["dropped_condition_labels"] = []
        # 실제 전송된 SQL 폴백 프롬프트/응답(트레이스 표시용; retrieve 가 result 로 옮기고 후보에선 제거).
        candidate["_llm_prompt"] = {"system": system_prompt, "user": user_prompt, "response": last_content}
        return candidate
    except Exception as exc:  # LLM 폴백 실패는 기존 실패 흐름(정직한 거절)으로 되돌아간다.
        _write_rag_llm_log("llm_sql_fallback_error", {"model": llm_model, "query": query, "error": str(exc)})
        return None


# 의미 검증 게이트가 분류하는 불일치 유형 → 사람이 읽는 라벨.
_SEMANTIC_ISSUE_LABELS = {
    "dropped": "누락(원문 조건이 SQL에 없음)",
    "inverted": "의미 반전(긍정↔부정/이상↔이하 등이 뒤집힘)",
    "wrong_value": "값 불일치(연령·지역·등급 등 값이 다름)",
    "spurious": "미요청 추가(원문에 없는 조건이 SQL에 있음)",
}


def _sql_semantic_verify_system_prompt() -> str:
    """의미 검증 게이트 시스템 프롬프트. 오탐(정상 SQL 차단)을 줄이려 '확신할 때만 불일치' 원칙을 강조한다.

    스키마 사실(성별 코드값·등급/지역/로그인/가입/생일 컬럼·카트/캠페인 팩트 테이블·날짜 포맷)은
    member_target_filters.json 레지스트리에서 렌더한다 — 프롬프트에 직접 박으면 DB 스왑 시
    이 함수도 고쳐야 한다(docs/operations/db_portability_audit.md §4-C). 검증 원칙 문구는 스키마
    무관이라 리터럴로 둔다."""

    def _short_column(config_key: str, fallback: str) -> str:
        config = _MEMBER_TARGET_FILTERS.get(config_key)
        column = (config or {}).get("column") if isinstance(config, dict) else None
        return str(column or fallback).split(".")[-1]

    gender_example = next(
        (str(value) for _c, (cat, _col, value) in MEMBER_EQ_FILTERS.items() if cat == "gender"), "GENDER_CD.FEMALE"
    )
    grade_column = _member_grade_column().split(".")[-1]
    sido_column, _sigungu = _member_region_short_columns()
    login_column = _short_column("recent_login_target", "LAST_LOGIN_DATE")
    signup_column = _short_column("signup_target", "REG_DT")
    birthday_column = _short_column("birthday_target", "BIRTHDAY")
    cart_config = _cart_targets_registry()
    cart_table = cart_config.get("table", "ODS_MALL_OMS_CART")
    cart_active = cart_config.get("active_condition") if isinstance(cart_config.get("active_condition"), dict) else {}
    keep_column = str((cart_active or {}).get("column") or "C.KEEP_YN").split(".")[-1]
    keep_value = str((cart_active or {}).get("value") or "Y")
    keep_predicate = f"{keep_column} = '{keep_value}'"
    campaign_config = _MEMBER_TARGET_FILTERS.get("campaign_response_targets")
    campaign_table = (campaign_config or {}).get("table") if isinstance(campaign_config, dict) else None
    campaign_table = campaign_table or "MCS_CAMP_MBR_RSPN_FT"
    date_format_label = str(_member_base_entity().get("date_format") or "yyyyMMdd").upper()
    return (
        "당신은 타겟팅 SQL 검증기다. 사용자 원문과 그 원문으로 생성된 SQL 을 받는다. "
        "SQL 이 원문의 **오디언스(타겟 회원) 조건**을 빠짐없이·왜곡 없이 반영했는지만 판정하라.\n"
        "다음은 무시한다(불일치로 보지 말 것): 발송 채널(문자/앱푸시/RCS 등)·메시지 카피·캠페인 목적/목표"
        "(objective, 예: 재구매 유도)·결과 개수 제한. SQL 은 오디언스 필터만 담고 이들은 담지 않는 게 정상이다.\n"
        "**값 변환·확장의 '완전성'은 절대 판정하지 말라**: 자연어 값은 시스템이 코드/등급 체계/권역 매핑으로 "
        f"변환·확장해 SQL 에 넣는다(여성→{gender_example}, 30대→AGE 30~39, 'GOLD 이상'→등급 IN 목록, 수도권→{sido_column} IN 목록). "
        "너는 등급 서열·권역 구성을 알지 못하므로, 어떤 값이 IN 목록/범위에 들어갔는지의 정확성·완전성을 "
        "**추측해서 판정하면 안 된다**. 원문의 각 조건이 SQL 에 **대응하는 컬럼 필터로 존재하기만 하면** 반영된 "
        f"것으로 보라(예: 'GOLD 이상' → {grade_column} IN(...) 이 있으면 OK, 목록에 무엇이 들었든 faithful). "
        "IN 목록·코드값·범위 확장을 dropped 나 wrong_value 로 보지 말라.\n"
        f"**날짜 창(window) 비교의 방향을 정확히 읽어라**: 날짜는 {date_format_label} 문자열이라 사전식 비교로 기간을 표현한다. "
        f"`{login_column} <= (기준일 - N일)` 은 마지막 접속이 N일보다 **이전** = '**N일 이상 미접속/장기 미접속/휴면**'(부정형 접속)이고, "
        f"`{login_column} >= (기준일 - N일)` 은 '**최근 N일 내 접속**'(긍정형)이다. `{signup_column} >= (기준일 - N일)` 은 '최근 N일 내 가입(신규)'이다. "
        "여기서 `IS NOT NULL` 은 널·이상치를 거르는 **가드일 뿐 '접속함(긍정)'을 뜻하지 않는다** — 미접속(휴면) 조건에 이 가드가 붙어 있어도 "
        "정상이며 inverted 로 보지 말라(원문 '접속하지 않은/휴면'과 `<= 과거기준일`은 방향이 일치한다). 방향(부등호)과 원문 극성만 맞으면 faithful 이다.\n"
        "**연령 경계의 '제외(여집합)'를 정확히 읽어라**: '~을 제외'는 그 조건의 여집합이라 SQL 부등호가 원문 단어와 반대로 보이는 게 정상이다. "
        "'N세 미만(<N) 회원 제외' = 여집합 `AGE >= N`(경계 N 포함, 예: '18세 미만 제외' → `AGE >= 18` 이 정답이고 18세는 남는 게 맞다), "
        "'N세 이상(>=N) 제외' = `AGE <= N-1`, 'N세 이하(<=N) 제외' = `AGE >= N+1`, 'N세 초과(>N) 제외' = `AGE <= N`. "
        "즉 '미만/이상' 같은 방향어 + '제외'의 **이중부정**이라 SQL 부호가 뒤집혀 보여도 여집합의 정상 변환이므로 inverted 로 보지 말라. "
        "닫힌 구간(연대/범위)의 제외 '**N대가 아닌/제외**'(예: '20대가 아닌')은 여집합이 분리 2구간이라 `NOT (AGE BETWEEN N AND N+9)` "
        "(또는 `AGE < N OR AGE > N+9`)로 나오는 게 정답이다 — 이걸 '20대만 뽑음'의 반대라 해서 inverted 로 보지 말라(오히려 `AGE BETWEEN 20 AND 29` 만 있으면 그게 반전이다). "
        "제외가 없는 순수 '~이상/이하/N대'는 그대로 방향/구간을 비교한다. "
        "**정확 연령**: '나이가 N세인'은 `AGE = N` 이고 `AGE >= N AND AGE <= N` 은 이와 **완전히 동일**하다(하·상한이 같은 점 범위) — 둘을 다르다고 inverted 로 보지 말라.\n"
        "**연대 OR(합집합)**: '20대 또는 30대'처럼 여러 연대를 '또는/이거나'로 묶으면 인접 구간이 이어져 하나의 "
        "범위가 된다 — '20대 또는 30대' → `AGE >= 20 AND AGE <= 39`(= BETWEEN 20 AND 39)가 **정답**이다. 이를 두고 "
        "'20대·30대를 포함하지 않고 20~39로 잘못 반영됐다'거나 범위가 틀렸다며 inverted/wrong_value 로 보지 말라(동일 의미다).\n"
        "**도메인 인코딩 사전(원문 개념 → SQL 표현)**: 원문의 개념은 컬럼/테이블 이름이 원문 단어와 다르게 인코딩된다. "
        "아래 대응이 SQL 에 있으면 원문 조건이 **반영된 것(faithful)**으로 보고 dropped 로 판정하지 말라.\n"
        f"  · '장바구니에 담고 결제/구매 안 함(장바구니 이탈/방치)' → 카트 테이블({cart_table} 등) 조인 + 보관중 카트 `{keep_predicate}` "
        f"(보관 중인 카트 라인 = 담아두고 미결제). 별도의 '결제 안 함' 부정 술어가 없어도 {keep_predicate} 자체가 '담고 미결제'를 뜻하므로 정상이다.\n"
        "  · '구매/주문 이력 없는·미구매·재구매 안 한' → 주문 팩트 테이블에 `NOT EXISTS`(anti-join). '구매한/재구매' → `EXISTS`.\n"
        "  · '최근 N일 미구매' → 주문 테이블 `NOT EXISTS`(최근 N일 창).  · '장바구니 없는' → 카트 `NOT EXISTS`.\n"
        f"  · '캠페인 발송/접촉·오퍼 반응·쿠폰 사용' → 캠페인 반응 팩트({campaign_table} 등) `EXISTS`, 부정형은 `NOT EXISTS`.\n"
        f"  · '신규 가입/가입한 지 N일' → `{signup_column}` 최근 창.  · '생일' → `{birthday_column}` 월일 비교.  · 등급/성별/지역 → 코드 컬럼 = / IN.\n"
        "  · SELECT 절의 라벨 컬럼(예: `'cart_abandoner' AS target_segment`, `'repurchase' AS objective`)은 **필터가 아니라 세그먼트 표식**이니 판정 대상이 아니다.\n"
        "핵심: 원문 개념이 위처럼 **대응 테이블/컬럼 필터로 존재하기만 하면** 반영된 것이다. 리터럴 단어 일치를 요구하지 말라.\n"
        "불일치 유형: dropped(원문 조건이 SQL 에 없음), inverted(긍정↔부정 또는 이상↔이하 등 의미가 반대로 "
        "반영됨. 예: '구매 이력이 없는'인데 SQL 은 구매함(EXISTS)으로 반영), wrong_value(연령대·지역·등급 등 값이 "
        "다름), spurious(원문에 없는 조건이 SQL 에 있음. 예: 엉뚱한 상품 LIKE).\n"
        "집계 질의에서는 원문 또는 함께 제공된 구조화 집계 계약에 있는 filters만 필수로 검사하라. "
        "원문과 계약 모두에 없는 기간·주문상태 조건이 SQL에도 없는 것은 dropped가 아니라 정상이다. "
        "구조화 집계 계약의 businessRules.appliedPolicyFilters 또는 별도로 제공된 [적용된 서비스 정책]의 "
        "appliedPolicyFilters에 기록된 조건은 서비스 정책이므로 원문에 없어도 spurious가 아니다. "
        "반대로 dimensions의 컬럼은 SELECT와 GROUP BY에 모두 있어야 하며, 다른 의미의 컬럼으로 바꾸면 dropped로 판정하라.\n"
        "중요: **확실한 의미 불일치만** 보고하라. 표현만 다르고 의미가 같으면 faithful=true. 판단이 애매하면 "
        "faithful=true 로 둔다(정상 SQL 을 막는 오탐이 놓치는 것보다 나쁘다). NOT EXISTS=조건 없음/부정, "
        "EXISTS=조건 있음/긍정임에 유의하라.\n"
        'JSON 으로만 답하라: {"faithful": true|false, "issues": [{"type": "dropped|inverted|wrong_value|spurious", '
        '"condition": "원문의 해당 표현", "detail": "무엇이 어떻게 틀렸는지 한 문장"}]}. faithful=true 면 issues 는 빈 배열.'
    )


def _verify_sql_semantics(
    original_query: str,
    sql: str,
    llm_model: str | None,
    prompt_dir: Path | None,
    query_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """최종 SQL 이 원문 의도를 충실히 반영했는지 LLM 으로 검증한다(원문↔SQL 직접 대조).

    반환 {ran, faithful, issues}. 게이트 비활성/LLM 불가/호출 실패면 ran=False 로 **통과(fail-open)** —
    검증기 자체 문제로 정상 SQL 을 막지 않는다. ran=True 이고 faithful=False 일 때만 호출자가 출고를 막는다.
    비결정적 LLM 이라 temperature=0 + '확신할 때만 불일치' 프롬프트로 오탐을 억제한다."""
    llm_model = _semantic_verify_model(llm_model)  # 의미검증 전용 모델(OPENAI_SEMANTIC_VERIFY_MODEL, 미지정 시 fast)
    if not _sql_semantic_verify_enabled() or not llm_model or not os.getenv("OPENAI_API_KEY"):
        return {"ran": False}
    if not (isinstance(original_query, str) and original_query.strip() and isinstance(sql, str) and sql.strip()):
        return {"ran": False}
    try:
        from openai import OpenAI
    except ImportError:
        return {"ran": False}
    try:
        client = OpenAI()
        aggregation_context = None
        if isinstance(query_plan, dict) and isinstance(query_plan.get("aggregation_request"), dict):
            aggregation_context = query_plan["aggregation_request"]
        member_policy_context = None
        if isinstance(query_plan, dict) and isinstance(query_plan.get("member_policy"), dict):
            policy = query_plan["member_policy"]
            if isinstance(policy.get("appliedPolicyFilters"), list) and policy["appliedPolicyFilters"]:
                member_policy_context = policy
        user_content = f"[원문]\n{original_query.strip()}"
        if aggregation_context is not None:
            user_content += "\n\n[구조화 집계 계약]\n" + json.dumps(aggregation_context, ensure_ascii=False, indent=2)
        if member_policy_context is not None:
            user_content += "\n\n[적용된 서비스 정책]\n" + json.dumps(
                member_policy_context, ensure_ascii=False, indent=2
            )
        user_content += f"\n\n[생성된 SQL]\n{sql.strip()}"
        response = _openai_chat_create(client,
            model=llm_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _sql_semantic_verify_system_prompt()},
                {"role": "user", "content": user_content},
            ],
            timeout=_prompt_rewrite_timeout_seconds(),
        )
        data = json.loads(response.choices[0].message.content or "{}")
        if not isinstance(data, dict) or not isinstance(data.get("faithful"), bool):
            return {"ran": False}  # 형식 불명 → 통과(fail-open)
        raw_issues = data.get("issues") if isinstance(data.get("issues"), list) else []
        issues = [
            {
                "type": issue.get("type") if issue.get("type") in _SEMANTIC_ISSUE_LABELS else "dropped",
                "condition": str(issue.get("condition") or "").strip(),
                "detail": str(issue.get("detail") or "").strip(),
            }
            for issue in raw_issues
            if isinstance(issue, dict)
        ]
        # 모델이 faithful=false를 명시했다면 issue 배열이 비었어도 출고 게이트가 반드시 막는다.
        # 근거가 비어 있는 경우에는 구조화된 일반 issue를 보강해 사용자에게 확인 이유를 남긴다.
        faithful = bool(data.get("faithful"))
        if not faithful and not issues:
            issues = [{
                "type": "dropped",
                "condition": "요청한 핵심 의도",
                "detail": "의미 검증기가 SQL과 원문의 불일치를 감지했지만 세부 항목을 반환하지 않았습니다.",
            }]
        verdict = {"ran": True, "faithful": faithful, "issues": [] if faithful else issues}
        _write_rag_llm_log("sql_semantic_verify", {"query": original_query, "sql": sql, **verdict})
        return verdict
    except Exception as exc:  # noqa: BLE001 - 게이트 실패는 치명적이지 않다(정상 SQL 통과 유지).
        _write_rag_llm_log("sql_semantic_verify_error", {"query": original_query, "error": str(exc)})
        return {"ran": False}


# 극성(긍정↔부정) 반전을 뜻하는 부정/제외 표지. inverted 판정이 '진짜 극성 반전'인지, 아니면 정상적인
# 결정론 변환(연령대→범위·수치 임계·값 확장)에 대한 오판인지 가르는 신호다. 부정 표지가 전혀 없는
# '양의 조건'(예: '20대 또는 30대', '구매 횟수 5회 이상')은 뒤집을 극성 자체가 없어 inverted 가 성립하지 않는다.
_NEGATION_CUE_RE = re.compile(
    r"없|않|못[한했하받]|아닌|아니|제외|미사용|미구매|미접속|미반응|미결제|미가입|미방문|비동의|취소|해지|중단|"
    r"안\s*[한함했하샀]|\bNOT\b",
    re.IGNORECASE,
)


def _is_noncredible_inverted_verdict(issue: dict[str, Any]) -> bool:
    """LLM 의미검증의 inverted 판정이 '극성이 없는 양의 조건'을 가리키면 True(→ 차단 면제, 자문만).

    inverted(의미 반전)는 긍정↔부정 극성이 뒤집힌 경우('구매 이력이 없는'인데 EXISTS 로 반영)에만 성립한다.
    그런데 판정 모델(경량 LLM)이 값 산술·구조를 자주 틀려, 결정론적으로 '옳게' 컴파일된 양의 조건까지
    inverted 로 오판한다: 연령대→AGE 범위('20대 또는 30대'→20~39), 수치 임계('5회 이상'→HAVING >=5),
    등급/권역 값 확장 등. 이들은 뒤집을 부정 극성 자체가 없으므로 inverted 가 논리적으로 성립하지 않는다.
    그래서 원문 표현에 부정/제외 표지(_NEGATION_CUE_RE)가 있을 때만 inverted 를 차단 사유로 인정하고,
    없으면 비차단 자문으로 강등한다(값/구조 정확성은 결정론 컴파일러·커버리지 검증이 소유 — dropped/
    wrong_value/spurious 를 비차단으로 두는 원칙의 연장). 진짜 반전('없는'→EXISTS)은 표지가 있어 계속 차단된다."""
    condition = str(issue.get("condition") or "")
    detail = str(issue.get("detail") or "")
    # 원문 조건 표현에 부정/제외 표지가 있으면 진짜 극성 반전일 수 있어 차단 유지(신뢰). detail 은 판정
    # 모델이 쓴 설명이라 '부정형으로 해석' 같은 표현이 섞여 오탐하므로, 원문 표현(condition)만 신뢰한다.
    if _NEGATION_CUE_RE.search(condition):
        return False
    # condition 이 비었으면 detail 로라도 극성 단서를 본다(단, 여기 걸리면 보수적으로 차단 유지).
    if not condition.strip() and _NEGATION_CUE_RE.search(detail):
        return False
    return True


def _infer_requirement_base(query_plan: dict[str, Any], sql: str | None) -> tuple[str, str]:
    """qualifier(브랜드/상품/카테고리 등)가 붙는 '주 조건(base)'을 plan/SQL 에서 추론한다 → (base_name, base_type).

    공통 requirement 회계에서 base×qualifier capability 를 조회할 키다. 장바구니는 브랜드/상품 qualifier 를
    지원 못 하고(unsupported) 구매는 지원(join_product_brand)하는 식으로 도메인 차이가 갈린다. SQL 이 카트
    테이블(KEEP_YN)을 쓰면 카트 문맥으로 확정(LLM plan 이 슬롯을 안 채워도 안전)하고, 아니면 plan 슬롯으로 본다."""
    cart_config = _cart_targets_registry()
    cart_table = str(cart_config.get("table") or "ODS_MALL_OMS_CART")
    if sql and cart_table in sql:
        return ("cart_retention", "behavior")
    target_user = query_plan.get("target_user", {}) if isinstance(query_plan.get("target_user"), dict) else {}
    domains = query_plan.get("recognized_domains") or []
    dom_names = {(d.get("name") if isinstance(d, dict) else d) for d in domains}
    behaviors = set(target_user.get("behaviors") or [])
    if ("cart" in dom_names or target_user.get("cart_retention") or target_user.get("cart_aggregate")
            or target_user.get("cart_type") or target_user.get("cart_absence") or "cart_abandoner" in behaviors):
        return ("cart_retention", "behavior")
    if target_user.get("coupon_usage_thresholds") or any(
        isinstance(r, dict) and "coupon" in str(r.get("canonical", ""))
        for r in (target_user.get("campaign_responses") or [])
    ):
        return ("coupon", "behavior")
    if isinstance(target_user.get("recent_login"), dict) or isinstance(target_user.get("inactivity_period"), dict):
        return ("login", "behavior")
    # 그 외(구매/집계/속성)는 구매 문맥이 브랜드/상품 qualifier 의 자연스러운 소유처다.
    return ("purchase", "behavior")


def _account_source_requirements(
    original_query: str, query_plan: dict[str, Any], sql: str | None,
    selected: dict[str, Any] | None = None,
) -> "semantic_requirements.RequirementAccounting | None":
    """원문의 조건을 source requirement 로 기록하고 base×qualifier capability + 반영 evidence 로 귀결시킨다.

    브랜드 전용 감지기 대신 공통 계층: 모든 qualifier requirement 가 compiled/unsupported/clarification 로
    귀결됐는지 회계한다. 미지원 조합(장바구니+상품 등)은 unsupported(+안내), 지원인데 반영 근거 없으면
    clarification(사일런트 드롭). 반영 확인은 선택 후보의 구조화 evidence(applied_requirements) 우선, SQL
    문자열 폴백 — 코드 치환·canonical 보정에도 정상 컴파일을 누락으로 오탐하지 않는다. 레지스트리 부재 시 None."""
    if _REQUIREMENT_REGISTRY is None or not sql:
        return None
    base_name, base_type = _infer_requirement_base(query_plan, sql)
    applied = selected.get("applied_requirements") if isinstance(selected, dict) else None
    return semantic_requirements.account_requirements(
        original_query, base_name, base_type, sql, _REQUIREMENT_REGISTRY, applied_requirements=applied
    )


def _semantic_verification_clarifications(issues: list[dict[str, Any]]) -> list[str]:
    """의미 불일치 issue 목록 → 사용자 확인용 clarification 문구."""
    questions: list[str] = []
    for issue in issues:
        label = _SEMANTIC_ISSUE_LABELS.get(issue.get("type"), "불일치")
        condition = issue.get("condition") or "일부 조건"
        detail = issue.get("detail") or ""
        questions.append(
            f"'{condition}' 조건이 생성 SQL에서 {label} 문제로 원문과 다르게 반영된 것으로 보입니다"
            + (f": {detail}" if detail else "")
            + ". 의도가 맞는지 확인해 주세요."
        )
    return questions


def _deterministic_dropped_conditions(original_query: str, query_plan: dict[str, Any]) -> list[str]:
    """③ 놓침을 시끄럽게: 원문에 정밀 추출된 신호가 최종 plan 슬롯에 하나도 안 잡혔으면(조용한 드롭)
    사람이 읽는 경고로 돌려준다. 결정론이라 rules/auto 양쪽에서 항상 돈다(LLM 의미검증 게이트의 보완재).

    오탐을 낮추려 '재작성 가드가 이미 신뢰하는 정밀 추출기(_prompt_signal_signature)'를 재사용하고, plan
    매핑이 명확한 family(성별·수신동의·캠페인 반응·최근 로그인)만 본다. 숫자/기간/상품처럼 여러 슬롯에
    흩어지거나 애매한 family 는 오탐이 커서 제외한다. 비차단 자문 — SQL 출고를 막지 않는다."""
    warnings: list[str] = []
    text = original_query or ""
    if not text.strip():
        return warnings
    signature = _prompt_signal_signature(text)
    target_user = query_plan.get("target_user", {})
    exclude = query_plan.get("exclude", {})

    if signature["genders"] and not (target_user.get("gender") or exclude.get("gender")):
        for gender in sorted(signature["genders"]):
            warnings.append(f"성별 '{_GENDER_CANONICAL_KO.get(gender, gender)}'")

    optin_slots = set(target_user.get("lifecycle") or []) | set(exclude.get("lifecycle") or [])
    for consent in sorted(signature["consents"]):
        if consent.split(":")[0] not in optin_slots:
            warnings.append(f"수신동의 조건 '{_CONSENT_SIGNAL_LABELS.get(consent, consent)}'")

    # 캠페인 반응: plan 이 긍정/부정 어느 트랙이든 잡았으면 보존(canonical 의 no_ 접두어 제거 후 비교).
    plan_responses = {
        str(response.get("canonical", "")).replace("no_", "", 1)
        for response in target_user.get("campaign_responses") or []
        if isinstance(response, dict)
    }
    for response in sorted(signature["campaign_responses"]):
        if response not in plan_responses:
            warnings.append(f"캠페인 반응 조건 '{_CAMPAIGN_RESPONSE_SIGNAL_LABELS.get(response, response)}'")

    # 최근 로그인/접속(긍정): 부정형(미접속/휴면)이 아닌데 recent_login·미접속 슬롯 둘 다 비었으면 드롭.
    compact = text.replace(" ", "").casefold()
    if (
        _RECENT_LOGIN_SIGNAL_RE.search(compact)
        and not any(neg in compact for neg in _RECENT_LOGIN_NEG_SIGNALS)
        and any(marker in compact for marker in _RECENCY_MARKERS)
        and not isinstance(target_user.get("recent_login"), dict)
        and not isinstance(target_user.get("inactivity_period"), dict)
    ):
        warnings.append("최근 로그인/접속 조건")

    # 구매 미발생(미구매/최근 N일 미구매): 원문에 구매 부정이 있는데 어떤 미구매 슬롯도 안 잡혔으면 드롭.
    behaviors = set(target_user.get("behaviors") or [])
    campaign_canonicals = {
        str(response.get("canonical", "")) for response in target_user.get("campaign_responses") or [] if isinstance(response, dict)
    }
    # 구매 미발생 표현: 부정어형('미구매/구매하지 않은')과 0-건형('구매건수 0건/주문 0건') 모두 본다 —
    # 후자는 창이 있으면 purchase_inactivity 로 컴파일되지만, 달력구간('올해 0건') 등 컴파일 불가 형태는
    # 어디에도 안 잡혀 조용히 사라진다. 어느 슬롯에도 반영 안 됐으면 시끄럽게 경고한다(silent drop 금지).
    purchase_absence_mentioned = bool(
        _PURCHASE_NEG_RE.search(compact) or _ZERO_PURCHASE_COUNT_PATTERN.search(compact)
    )
    if purchase_absence_mentioned and not (
        isinstance(target_user.get("purchase_inactivity"), dict)
        or isinstance(target_user.get("inactivity_period"), dict)
        or "no_purchase" in behaviors
        or "no_buy_response" in campaign_canonicals
        or target_user.get("cart_absence")
    ):
        warnings.append("구매 미발생(미구매/최근 N일 미구매/구매건수 0건) 조건")

    # 달력 기간(올해/지난달 등)은 집계 조건에 표식으로 보존되지만 아직 집계 SQL 에 반영되지 않는다(별도 작업).
    # 전체 기간으로 조용히 계산되면 '올해 10회'가 '평생 10회'로 왜곡되므로, 명시 경고로 사용자에게 알린다.
    calendar_periods = {
        str(condition.get("calendar_period"))
        for condition in target_user.get("aggregate_conditions") or []
        if isinstance(condition, dict) and condition.get("calendar_period")
    }
    for period in sorted(calendar_periods):
        label = _CALENDAR_PERIOD_LABELS.get(period, period)
        warnings.append(f"기간 '{label}' 조건(달력 기간은 집계에 아직 미반영 — 전체 기간으로 계산됨)")

    # 장바구니: 원문에 '장바구니'가 있는데 어떤 카트 슬롯도 안 잡혔으면 드롭(존재/부재/보관/유형/개수 전부).
    if "장바구니" in compact and not (
        "cart_abandoner" in behaviors
        or isinstance(target_user.get("cart_retention"), dict)
        or target_user.get("cart_type")
        or target_user.get("cart_absence")
        or target_user.get("cart_aggregate")
        or target_user.get("cart_quantity_missing")
    ):
        warnings.append("장바구니 조건")
    return warnings


def _verify_sql_semantic_invariants(
    query: str, plan: dict[str, Any], sql: str, dropped_signal_warnings: list[str],
) -> dict[str, Any]:
    """SQL 생성 시 항상 실행되는 결정론 의미 보존 불변식 점검(LLM 불필요, ran=True 보장).

    LLM 게이트(_verify_sql_semantics)는 OPENAI 없으면 ran=False 로 통과(fail-open)하지만, 이 게이트는
    파서 전파 결함(창 도메인 누수·누적↔롤링 혼입·구매 미발생 silent drop)을 원문↔plan 대조로 결정론
    점검한다. 위반은 issues 로 남기는 비차단 자문이다 — 진짜 drop 은 dropped_signal_warnings 가 시끄럽게
    고지하고, 값/컴파일 정확성은 결정론 컴파일러·커버리지 검증이 소유한다."""
    issues: list[dict[str, Any]] = []
    target_user = plan.get("target_user", {}) if isinstance(plan.get("target_user"), dict) else {}
    compact = query.replace(" ", "").casefold()
    aggregates = [c for c in (target_user.get("aggregate_conditions") or []) if isinstance(c, dict)]

    # (1) lifetime↔rolling 혼입 금지: 원문에 '누적/평생' 표지가 있는데 명시 롤링 창('최근 N일')은 전혀 없고,
    #     그런데도 집계 조건에 window_days 가 붙어 있으면 옆 도메인 조건(로그인 등)에서 창이 흘러든 것이다.
    if _CUMULATIVE_WINDOW_MARKER_RE.search(compact) and _parse_recent_window_days(query) is None:
        for condition in aggregates:
            if condition.get("window_days"):
                issues.append({
                    "type": "lifetime_rolling_window",
                    "detail": f"누적 지표 '{condition.get('label', condition.get('metric_id'))}'에 "
                              f"롤링 창({condition.get('window_days')}일)이 주입됨(옆 조건 창 누수 의심)",
                })

    # (2) 구매 미발생 silent drop 금지: 표현은 있는데 어느 슬롯에도 없고 경고도 없으면 조용한 드롭이다.
    purchase_absence_mentioned = bool(
        _PURCHASE_NEG_RE.search(compact) or _ZERO_PURCHASE_COUNT_PATTERN.search(compact)
    )
    represented = (
        isinstance(target_user.get("purchase_inactivity"), dict)
        or isinstance(target_user.get("inactivity_period"), dict)
        or "no_purchase" in (target_user.get("behaviors") or [])
        or any(isinstance(r, dict) and r.get("canonical") == "no_buy_response"
               for r in (target_user.get("campaign_responses") or []))
        or target_user.get("cart_absence")
    )
    warned = any(("구매" in w or "주문" in w) for w in (dropped_signal_warnings or []))
    if purchase_absence_mentioned and not represented and not warned:
        issues.append({"type": "purchase_absence_dropped",
                       "detail": "구매 미발생 조건이 plan/SQL/경고 어디에도 반영되지 않음"})

    return {"ran": True, "ok": not issues, "issues": issues}


def _semantic_evidence_sources() -> dict[str, tuple[str, ...]]:
    """행동 도메인별 허용 SQL 근거 소스. 설정을 우선해 DB 스왑 시 검증도 함께 이동한다."""
    order_cfg = _order_count_targets_config()
    cart_cfg = _cart_targets_registry()
    campaign_cfg = _MEMBER_TARGET_FILTERS.get("campaign_response_targets", {})
    contact_cfg = campaign_cfg.get("contact_member_list", {}) if isinstance(campaign_cfg, dict) else {}
    configured_purchase_tables = order_cfg.get("evidence_tables")
    purchase_tables = (
        [str(value) for value in configured_purchase_tables if isinstance(value, str) and value]
        if isinstance(configured_purchase_tables, list)
        else [str(order_cfg.get("table") or "CRM_SL_ORDERHEADERMALL"), "CRM_SL_ORDERDETAILMALL"]
    )
    return {
        "purchase": tuple(_unique_strings(purchase_tables)),
        "cart": (str(cart_cfg.get("table") or "ODS_MALL_OMS_CART"),),
        "campaign_response": tuple(_unique_strings([
            str(campaign_cfg.get("table") or "MCS_CAMP_MBR_RSPN_FT") if isinstance(campaign_cfg, dict) else "MCS_CAMP_MBR_RSPN_FT",
            str(contact_cfg.get("table") or "Z_CAMP_MBR") if isinstance(contact_cfg, dict) else "Z_CAMP_MBR",
        ])),
        "coupon": tuple(_unique_strings([
            str(campaign_cfg.get("table") or "MCS_CAMP_MBR_RSPN_FT") if isinstance(campaign_cfg, dict) else "MCS_CAMP_MBR_RSPN_FT",
        ])),
        "login": (_member_table(),),
        "dormancy": (_member_table(),),
        # 아래 도메인은 현재 플랜 슬롯이 열리기 전에도 공통 검증기를 확장 가능한 형태로 테스트할 수 있게
        # 명시한다. 실제 배포 매핑이 생기면 설정 기반 소스로 교체하면 된다.
        "visit": ("VISIT", "LOG"),
        "wishlist": ("WISHLIST",),
    }


def _condition_evidence(condition: dict[str, Any], sql: str) -> dict[str, Any]:
    """semantic condition 하나의 SQL 소스와 포함/제외 극성을 검증한다."""
    domain = str(condition.get("domain") or "")
    operator = str(condition.get("operator") or "exists")
    normalized = re.sub(r"\s+", " ", sql).casefold()
    sources = _semantic_evidence_sources().get(domain, ())
    source_hits = [source for source in sources if source and source.casefold() in normalized]
    required = [f"{domain}_source_reference"]
    actual: list[str] = [f"source:{source}" for source in source_hits]

    if domain == "dormancy":
        definition = condition.get("definition_type")
        if definition == "status_code":
            satisfied = bool(source_hits and "member_state_cd" in normalized and "sleep" in normalized)
            required.append("dormancy_status_filter")
            if "member_state_cd" in normalized:
                actual.append("status_code_filter")
        else:
            satisfied = bool(source_hits and "last_login" in normalized and any(op in normalized for op in (" <= ", " < ")))
            required.append("inactivity_date_filter")
            if "last_login" in normalized:
                actual.append("last_login_filter")
        return {"condition": condition, "required_evidence": required, "actual_evidence": actual,
                "satisfied": satisfied, "polarity_match": satisfied}

    if domain == "login":
        has_login = "last_login" in normalized
        if operator == "not_exists":
            polarity_match = bool(re.search(r"last_login[^;]*(?:is\s+null|<=|<)", normalized))
            direction = "login_absence_filter"
        else:
            polarity_match = bool(re.search(r"last_login[^;]*(?:is\s+not\s+null|>=|>)", normalized))
            direction = "login_presence_filter"
        satisfied = bool(source_hits and has_login and polarity_match)
        return {"condition": condition, "required_evidence": [*required, direction],
                "actual_evidence": actual + ([direction] if polarity_match else []),
                "satisfied": satisfied, "polarity_match": polarity_match}

    negative_hits: list[str] = []
    positive_hits: list[str] = []
    for source in source_hits:
        escaped = re.escape(source.casefold())
        if re.search(rf"not\s+exists\s*\([^;]*?\b{escaped}\b", normalized, re.DOTALL):
            negative_hits.append(source)
        if (
            re.search(rf"(?<!not\s)exists\s*\([^;]*?\b{escaped}\b", normalized, re.DOTALL)
            or re.search(rf"\b(?:inner\s+|left\s+|right\s+)?join\s+{escaped}\b", normalized)
            or re.search(rf"\bin\s*\(\s*select\b[^;]*?\bfrom\s+{escaped}\b", normalized, re.DOTALL)
            or re.search(rf"\bfrom\s+{escaped}\b", normalized)
        ):
            positive_hits.append(source)

    if operator == "not_exists":
        required.append("anti_join_or_not_exists")
        polarity_match = bool(negative_hits)
        if negative_hits:
            actual.append("not_exists")
    else:
        required.append("positive_membership")
        # 동일 소스가 오직 NOT EXISTS 안에만 있으면 긍정 근거로 인정하지 않는다.
        polarity_match = bool(positive_hits and any(source not in negative_hits for source in positive_hits))
        if polarity_match:
            actual.append("exists_or_join")
    satisfied = bool(source_hits) and polarity_match
    return {
        "condition": condition,
        "required_evidence": required,
        "actual_evidence": actual,
        "satisfied": satisfied,
        "polarity_match": polarity_match,
    }


_TARGET_MEMBER_PROJECTION_RE = re.compile(
    r"(?i)(?:\b[A-Za-z_][\w$]*\s*\.\s*)?"
    r"(?:\[\s*MEMBER_NO\s*\]|`MEMBER_NO`|\"MEMBER_NO\"|MEMBER_NO)\s+AS\s+"
    r"(?:\[\s*CUST_ID\s*\]|`CUST_ID`|\"CUST_ID\"|CUST_ID)(?![\w$])"
)


def _has_target_member_projection(sql: str) -> bool:
    """Return whether target SQL exposes MEMBER_NO under the required CUST_ID alias."""
    return bool(_TARGET_MEMBER_PROJECTION_RE.search(sql or ""))


def _actual_sql_grain(sql: str, dialect: str | None = None) -> dict[str, Any]:
    """최상위 SELECT AST에서 결과 grain과 회원 ID 계약을 판정한다."""
    try:
        from sql_semantics import extract_sql_semantics

        semantics = extract_sql_semantics(sql, dialect=dialect)
    except Exception as exc:  # SQL 가드는 별도로 돌지만 의미 파서 실패는 출고 계약을 증명하지 못한 것.
        return {
            "actual_grain": "unknown",
            "has_member_id": False,
            "has_member_no_as_cust_id": _has_target_member_projection(sql),
            "parser_error": str(exc),
        }

    selected = [value.casefold() for value in semantics.selected_columns]
    grouped = [value.casefold() for value in semantics.group_by]
    # ODS_MALL_OMS_CART.CART_ID is the mall login member identifier and is catalog-mapped to
    # CRM_MB_BASEINFO.MEMBER_ID.  A cart aggregation can therefore return it without a potentially
    # duplicating join to the member table.
    member_columns = {"cust_id", "user_id", "customer_id", "member_no", "member_id", "cart_id"}
    has_member_id = any(any(column == item.rsplit(".", 1)[-1] for column in member_columns) for item in selected)
    group_text = " ".join(grouped)
    if grouped and any(token in group_text for token in ("sigungu", "sido", "region", "city", "district")):
        grain = "region"
    elif grouped and any(token in group_text for token in ("product", "item", "brand", "sku")):
        grain = "product"
    elif grouped and any(token in group_text for token in ("campaign", "camp_id", "camp_id")):
        grain = "campaign"
    elif has_member_id:
        grain = "member"
    elif semantics.aggregates and not grouped:
        grain = "member_count"
    elif grouped:
        grain = "grouped"
    else:
        grain = "unknown"
    return {
        "actual_grain": grain,
        "has_member_id": has_member_id,
        "has_member_no_as_cust_id": _has_target_member_projection(sql),
        "selected_columns": semantics.selected_columns,
        "group_by": semantics.group_by,
    }


def _semantic_issue_is_critical(issue: dict[str, Any], query: str, sql: str) -> bool:
    """Classify result-shaping dropped/spurious issues as fail-closed."""
    if issue.get("severity") == "critical" or issue.get("affects_result_set") is True or issue.get("is_primary_condition") is True:
        return True
    if issue.get("type") == "inverted" and not _is_noncredible_inverted_verdict(issue):
        return True
    issue_type = str(issue.get("type") or "").casefold()
    structural_text = " ".join(
        str(issue.get(key) or "") for key in ("condition", "detail", "expected", "actual")
    ).casefold()
    structural_terms = (
        "sum", "count", "avg", "max", "min", "aggregate", "metric", "measure", "group by", "join",
        "time range", "filter", "grain", "column", "table", "회원번호", "회원 번호", "주문번호", "거래번호",
        "집계", "합계", "총액", "지표", "그룹", "차원", "조인", "기간", "필터", "컬럼", "테이블",
    )
    if issue_type in {"dropped", "spurious"} and any(term in structural_text for term in structural_terms):
        return True
    if issue_type == "spurious":
        # 요청하지 않은 GROUP BY/식별자/필터는 결과 grain이나 행 수를 바꾸므로 항상 차단한다.
        return True
    if issue_type != "dropped":
        return False
    condition_text = str(issue.get("condition") or "").replace(" ", "").casefold()
    compact_query = query.replace(" ", "").casefold()
    # 판정기가 원문에 없는 조건을 지어낸 dropped는 차단하지 않는다. 원문 조건 자체이거나 원문과 같은
    # 행동 도메인을 가리킬 때만 primary 후보가 된다.
    domain_terms = {
        "purchase": ("구매", "구입", "주문"), "cart": ("장바구니", "카트"),
        "campaign": ("캠페인", "반응", "오퍼"), "login": ("로그인", "접속"),
    }
    for domain, terms in domain_terms.items():
        if (
            (condition_text and condition_text in compact_query)
            or (any(term in condition_text for term in terms) and any(term in compact_query for term in terms))
        ) and any(term in condition_text for term in terms):
            # SQL에 그 도메인 근거가 아예 없을 때만 critical. 동등 인코딩을 LLM이 못 읽은 오탐은 경고로 둔다.
            evidence_domain = "campaign_response" if domain == "campaign" else domain
            if not any(source.casefold() in sql.casefold() for source in _semantic_evidence_sources().get(evidence_domain, ())):
                return True
    return False


def _validate_sql_delivery_contract(
    query: str,
    query_plan: dict[str, Any],
    sql: str,
    *,
    dialect: str | None = None,
    semantic_verification: dict[str, Any] | None = None,
    dropped_conditions: list[str] | None = None,
) -> dict[str, Any]:
    """최종 출고의 단일 fail-closed 불변식: evidence, polarity, grain, 결과 컬럼, critical drop."""
    output = query_plan.get("output_contract") if isinstance(query_plan.get("output_contract"), dict) else {}
    expected_grain = str(output.get("expected_grain") or "member")
    actual = _actual_sql_grain(sql, dialect)
    actual_grain = actual["actual_grain"]
    grain_match = (
        actual_grain == expected_grain
        or (expected_grain == "analytical" and actual_grain in {"region", "product", "campaign", "grouped", "member_count"})
    )
    # 회원 수 단일 집계도 회원 질문의 허용 결과지만, 타겟 API 회원집합 계약(requires_member_id=true)이면
    # 회원 ID를 반드시 반환해야 별도 target_customer_count를 안전하게 계산할 수 있다.
    requires_member_id = bool(output.get("requires_member_id", expected_grain == "member"))
    requires_member_projection = bool(output.get("requires_member_no_as_cust_id")) or (
        expected_grain == "member"
        and query_plan.get("intent") in {"recommend_campaign", "find_user_segment"}
    )
    member_id_match = (not requires_member_id) or bool(actual.get("has_member_id"))
    member_projection_match = (
        (not requires_member_projection) or bool(actual.get("has_member_no_as_cust_id"))
    )
    api_contract_match = member_id_match and member_projection_match

    extracted_conditions = [c for c in query_plan.get("semantic_conditions") or [] if isinstance(c, dict)]
    evidence = [_condition_evidence(condition, sql) for condition in extracted_conditions]
    missing = [item["condition"] for item in evidence if not item["satisfied"]]
    polarity_mismatches = [item["condition"] for item in evidence if item["actual_evidence"] and not item["polarity_match"]]

    enriched_issues: list[dict[str, Any]] = []
    critical_issues: list[dict[str, Any]] = []
    verification = semantic_verification or {"ran": False}
    for raw_issue in verification.get("issues") or []:
        if not isinstance(raw_issue, dict):
            continue
        critical = _semantic_issue_is_critical(raw_issue, query, sql)
        issue_type = str(raw_issue.get("type") or "dropped").casefold()
        reason_code = {
            "dropped": "DROPPED_SEMANTIC_REQUIREMENT",
            "spurious": "SPURIOUS_RESULT_SHAPING_CLAUSE",
            "inverted": "INVERTED_SEMANTIC_REQUIREMENT",
            "wrong_value": "WRONG_FILTER_VALUE",
        }.get(issue_type, "SEMANTIC_MISMATCH")
        issue = {
            **raw_issue,
            "reason_code": raw_issue.get("reason_code") or reason_code,
            "severity": "critical" if critical else raw_issue.get("severity", "warning"),
            "affects_result_set": critical,
            "is_primary_condition": critical,
        }
        enriched_issues.append(issue)
        if critical:
            critical_issues.append(issue)

    reasons: list[str] = []
    if polarity_mismatches:
        reasons.append("semantic_condition_polarity_mismatch")
    if missing:
        reasons.append("semantic_conditions_not_covered")
    if not grain_match:
        reasons.append("query_result_grain_mismatch")
    if not member_id_match:
        reasons.append("targeting_result_member_id_missing")
    if not member_projection_match:
        reasons.append("targeting_result_member_projection_missing")
    if dropped_conditions:
        reasons.append("critical_conditions_dropped")
    # faithful=false 자체가 최종 출고 불가 조건이다. issue 분류는 reason code와 안내 품질을 위한
    # 부가 정보이며, 빈/오분류 issue 때문에 불일치 SQL이 success로 빠져나가면 안 된다.
    if verification.get("ran") and verification.get("faithful") is False:
        reasons.append("critical_semantic_issue")
    return {
        "is_satisfied": not reasons,
        "expected_grain": expected_grain,
        "actual_grain": actual_grain,
        "grain_match": grain_match,
        "api_contract_match": api_contract_match,
        "member_projection_match": member_projection_match,
        "required_conditions": len(extracted_conditions),
        "condition_tokens": None,
        "extracted_conditions": extracted_conditions,
        "missing_conditions": missing,
        "polarity_mismatches": polarity_mismatches,
        "semantic_issues": enriched_issues,
        "sql_evidence": {str(index): item for index, item in enumerate(evidence, start=1)},
        "failure_reasons": _unique_strings(reasons),
        "failure_reason": reasons[0] if reasons else None,
        "sql_contract": actual,
    }


def _failed_sql_confidence(reason: str | None, *, execution_error: bool = False) -> dict[str, Any]:
    """Return a renderer-compatible low confidence payload after final validation."""
    normalized = str(reason or "").upper()
    zero_score_reasons = {
        "UNREGISTERED_RELATIONSHIP", "JOIN_KEY_MISMATCH", "JOIN_TYPE_MISMATCH",
        "UNVERIFIED_JOIN_CAST", "ZERO_JOIN_MATCH_RATE", "UNUSABLE_METRIC_COLUMN",
        "METRIC_ALL_NULL", "METRIC_UNAVAILABLE_FOR_POPULATION", "METRIC_MISMATCH",
        "METRIC_UNIT_MISMATCH", "AGGREGATION_MISMATCH", "TOP_N_MISMATCH",
    }
    if execution_error or normalized in zero_score_reasons:
        score = 0
    elif normalized in {"SEMANTIC_VERIFICATION_FAILED", "SEMANTIC_MISMATCH"}:
        score = 10
    else:
        score = 20
    return {
        "overall_score": score,
        "level": "낮음",
        "dimensions": {
            "request_sql_match": 0,
            "schema_match": 0 if score == 0 else 20,
            "policy_similarity": 0,
            "clarity": 20,
            "static_validation": 0,
        },
        "dimension_weights": {},
        "conditions": [],
        "warnings": [f"최종 SQL 검증에 실패했습니다: {reason or 'unknown_validation_failure'}"],
    }


def build_sql_result(
    graph: nx.Graph,
    query: str,
    query_plan: dict[str, Any],
    context_nodes: list[dict[str, Any]],
    schema_path: Path,
    default_limit: int,
    candidate_limit: int = 20,
    llm_model: str | None = None,
    original_query: str | None = None,
    prompt_dir: Path | None = None,
) -> dict[str, Any]:
    # 호출자가 수동 plan을 넘기는 단위/통합 경로도 동일한 의미 추출·출력 계약을 거친다.
    # 파생 엔터티 집합이 소유한 슬롯은 여기서 마지막으로 회수한다 — 계획 이후 단계(변이 병합·조건
    # 재확정)가 순위 절의 어구를 오디언스 조건으로 되살리면 같은 어구가 두 번 컴파일된다.
    _apply_entity_set_condition(original_query or query, query_plan)
    _normalize_aggregation_axis_filters(query_plan)
    _normalize_purchase_aggregation_request(query_plan)
    _refresh_aggregation_request_validation(query_plan, schema_path)
    if not isinstance(query_plan.get("semantic_conditions"), list):
        _apply_core_membership_semantics(original_query or query, query_plan)
    if not isinstance(query_plan.get("output_contract"), dict):
        _attach_query_output_contract(original_query or query, query_plan)
    condition_tokens = build_verified_condition_tokens(query_plan)
    input_validation = validate_required_input_conditions(query_plan, condition_tokens)
    required_conditions = required_sql_conditions(query_plan)
    # 슬롯 파서가 조건을 구조화하지 못한 것과 '표현할 수 없는 요청'은 다르다. 닫힌 IR 로 요청 전체를
    # 표현할 수 있으면 그것이 더 정확한 근거이므로 확인 요청 대신 그 후보로 진행한다. IR 은 어휘가
    # 레지스트리로 검증되고 회원 투영이 컴파일러 소유라, 슬롯 없이도 임의 SQL 이 나올 수 없다.
    # 빈 표현식(전체 회원)은 조건 소실과 구분되지 않으므로 채택하지 않는다.
    structured_ir_candidate = None
    if (
        not input_validation["is_satisfied"]
        and llm_model
        and not query_plan.get("unsupported")
        and query_plan.get("intent") in ("recommend_campaign", "find_user_segment")
    ):
        candidate = _build_llm_targeting_ir_candidate(original_query or query, query_plan, llm_model)
        if candidate is not None and describe_targeting_expression(candidate["targeting_expression"]):
            structured_ir_candidate = candidate

    if not input_validation["is_satisfied"] and structured_ir_candidate is None:
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
            "confidence": _failed_sql_confidence("query_plan_required_conditions_missing"),
            "is_success": False,
            "failure_reason": "query_plan_required_conditions_missing",
        }

    # 필수조건이 있는데 검증 토큰이 하나도 없으면 비교 대상이 없어 coverage=0/0으로 통과하던 구멍을
    # 후보 생성 전에 닫는다. 명시적 전체 대상은 required_conditions 자체가 0이므로 정상 통과한다.
    if required_conditions and not condition_tokens and not query_plan["output_contract"].get("whole_target"):
        missing = [
            _missing_input_condition(
                str(condition.get("path") or "query_plan.conditions"),
                str(condition.get("value") or condition.get("path") or "필수 조건"),
                "요청한 필수 조건을 SQL 조건으로 추출하지 못했습니다. 조건의 대상과 포함/제외 방향을 확인해 주세요.",
            )
            for condition in required_conditions
        ]
        return {
            "sql": None, "blocked_sql": None, "target_connection": None, "target_dialect": None,
            "selected": None, "candidates": [], "candidate_count": 0,
            "condition_tokens": [], "required_conditions": required_conditions,
            "input_validation": {"is_satisfied": False, "missing_conditions": missing,
                                 "clarification_questions": [item["question"] for item in missing]},
            "missing_input_conditions": missing,
            "clarification_questions": [item["question"] for item in missing],
            "semantic_verification": {"ran": False},
            "delivery_validation": {
                "is_satisfied": False,
                "expected_grain": query_plan["output_contract"].get("expected_grain"),
                "actual_grain": "unknown", "required_conditions": len(required_conditions),
                "condition_tokens": 0, "extracted_conditions": query_plan.get("semantic_conditions", []),
                "missing_conditions": required_conditions, "semantic_issues": [], "sql_evidence": {},
                "failure_reason": "semantic_conditions_not_extracted",
            },
            "llm_fallback_used": False, "generation_source": None,
            "confidence": _failed_sql_confidence("semantic_conditions_not_extracted"),
            "is_success": False, "failure_reason": "semantic_conditions_not_extracted",
        }

    allowed_tables = load_allowed_tables(schema_path)
    table_dialects = load_table_dialects(schema_path)
    table_databases = load_table_databases(schema_path)
    column_types = load_column_types(schema_path)
    schema_columns = load_schema_columns(schema_path)
    join_key_registry = load_join_key_registry(schema_path)
    template_candidate = build_sql_template_candidate(query_plan)
    candidates = [template_candidate] if template_candidate is not None else []
    if structured_ir_candidate is not None:
        candidates.append(structured_ir_candidate)

    # 2티어 폴백: 결정론 템플릿/조합 빌더가 후보를 못 만든 타겟팅 질의만 LLM 생성으로 시도한다.
    # 생성 SQL 도 아래 루프에서 템플릿과 동일한 가드 스택으로 검증되며, 실패하면 기존 거절 흐름 유지.
    # 단, 명시적 미지원(plan['unsupported'])으로 후보가 없어진 경우엔 LLM 폴백을 시도하지 않는다 — 그러면
    # LLM 이 '그럴듯하지만 틀린' SQL(예: 쿠폰 건수 임계를 USE_CPN_CNT>0 존재로 축소)을 지어내 의미 검증에서
    # inverted/불일치로 떨어지는 '혼합/실패' 잡음이 생긴다. 미지원은 깔끔한 unsupported 응답으로 끝낸다.
    llm_fallback_used = structured_ir_candidate is not None
    targeting_intent = query_plan.get("intent") in ("recommend_campaign", "find_user_segment")
    # 결정론 경로가 후보를 냈더라도 실DB 로 매핑하지 못한 조건이 남아 있으면 그 후보는 검증에서
    # 탈락한다. 그 경우에도 IR 후보를 함께 세워 경쟁시킨다 — 적격 후보가 있으면 선택 로직이
    # 결정론 후보를 먼저 고르므로 기존 동작은 바뀌지 않고, 탈락할 때만 IR 이 대안이 된다.
    member_unsupported = bool(
        targeting_intent
        and candidates
        and not query_plan.get("unsupported")
        and compile_member_target_conditions(query_plan)["unsupported"]
    )
    if (
        (not candidates or member_unsupported or isinstance(query_plan.get("aggregation_request"), dict))
        and not query_plan.get("unsupported")
        and llm_model
        and query_plan.get("intent") in ("recommend_campaign", "find_user_segment", "analyze_aggregation")
    ):
        # 1.5티어: 회원 집합 요청은 자유 SQL 대신 닫힌 IR 로 먼저 시도한다. 컴파일러가 회원 투영과
        # 물리 매핑을 보장하므로, 성공하면 '그럴듯하게 틀린 SQL' 자체가 생성되지 않는다.
        llm_candidate = None
        if targeting_intent and structured_ir_candidate is None:
            # 원문 문장을 넘긴다 — 이 시점의 query 는 검색용으로 canonical 토큰이 덧붙은 확장 질의라
            # 그대로 주면 모델이 사람 문장이 아닌 토큰 나열을 해석하게 된다(간헐 실패의 원인).
            llm_candidate = _build_llm_targeting_ir_candidate(original_query or query, query_plan, llm_model)
        if llm_candidate is None and not member_unsupported:
            # 자유 SQL 폴백은 종전대로 '후보 없음/집계' 경로에서만 쓴다.
            llm_candidate = _build_llm_sql_fallback_candidate(
                query, query_plan, context_nodes, allowed_tables, llm_model, schema_path=schema_path
            )
        if llm_candidate is not None:
            candidates.append(llm_candidate)
            llm_fallback_used = True

    # 타겟 오디언스는 기본적으로 전체가 나와야 하므로 행수 제한(TOP/LIMIT)을 붙이지 않는다(default_limit=None).
    # 단 프롬프트가 'N명만' 등으로 개수를 명시했을 때만 그 값을 sql_guard 에 넘겨 대상 DBMS 방언에 맞는
    # TOP(MSSQL)/LIMIT(MariaDB)을 부착한다(지표 랭킹의 기존 TOP 은 sql_guard 가 중복 없이 보존).
    result_limit = query_plan.get("result_limit")
    result_limit = result_limit if isinstance(result_limit, int) and result_limit > 0 else None
    structured_aggregation = None
    structured_aggregation_errors = []
    if isinstance(query_plan.get("aggregation_request"), dict):
        structured_aggregation, structured_aggregation_errors = parse_aggregation_request(
            query_plan["aggregation_request"], schema_path, dialect=_member_dialect().name
        )
    validated_candidates = []
    for candidate in candidates:
        validation = validate_sql(
            candidate["sql"],
            allowed_tables=allowed_tables,
            default_limit=result_limit,
            table_dialects=table_dialects,
            column_types=column_types,
            schema_columns=schema_columns,
        )
        # 필수조건을 부분 추출 대상으로 빼고 성공시키지 않는다. 지원하지 못한 조건은 최종적으로 SQL 없이
        # 실패/확인요청으로 귀결돼야 하며, "되는 조건만"의 SQL은 출고하지 않는다.
        coverage = validate_sql_condition_coverage(candidate["sql"], required_conditions)
        intent_scope = validate_sql_intent_scope(candidate, query_plan)
        unmentioned_conditions = validate_unmentioned_sql_conditions(candidate["sql"], query_plan)
        delivery_validation = _validate_sql_delivery_contract(
            original_query or query,
            query_plan,
            candidate["sql"],
            dialect=validation.get("dialect"),
            dropped_conditions=candidate.get("dropped_conditions") or [],
        )
        # 조인키 검증: 타입군이 다른 등호 조인(nvarchar↔bigint)은 실행 자체가 실패하고, 타입이 같아도
        # 검증된 관계(schema_catalog.foreign_keys, confidence=verified)와 다른 컬럼에 붙인 조인은 조용히
        # 0건이 된다. LLM 폴백이 CART_ID=MEMBER_NO 를 지어내도 기존 가드는 통과시켰다(올바른 짝은 MEMBER_ID).
        join_keys = validate_join_keys(
            candidate["sql"], column_types, join_key_registry,
            # Deterministic builders render joins in application code. LLM candidates are not
            # allowed to invent a relationship merely because both columns happen to share a type.
            strict_relationships=candidate.get("source") == "llm_generated",
        )
        if join_keys["issues"]:
            validation = {**validation, "issues": [*validation["issues"], *join_keys["issues"]], "is_valid": False}
        # 집계·grain 정합성 검사: 집계 함수를 WHERE 에 쓰거나(집계 전/후 필터 미분리), 집계·비집계
        # 컬럼을 GROUP BY 없이 혼용하는 등 '문법은 그럴듯하나 grain 이 틀린' SQL 을 잡는다(주로 LLM 폴백).
        # error 는 후보를 탈락시키고, warning(DISTINCT 은폐·1:N 조인 의심)은 고지만 남긴다.
        analytics_shape = validate_analytics_shape(candidate["sql"])
        analytics_errors = [issue for issue in analytics_shape["issues"] if issue["severity"] == "error"]
        analytics_warnings = [issue for issue in analytics_shape["issues"] if issue["severity"] == "warning"]
        if analytics_errors:
            validation = {**validation, "issues": [*validation["issues"], *analytics_errors], "is_valid": False}
        aggregation_validation: dict[str, Any] = {"ran": False}
        if structured_aggregation is not None:
            aggregation_validation = validate_aggregation_sql(
                structured_aggregation,
                candidate["sql"],
                schema_path,
                dialect=validation.get("dialect") or _member_dialect().name,
            )
            generation_validation = candidate.get("aggregation_validation")
            if isinstance(generation_validation, dict) and generation_validation.get("ran") is not False:
                aggregation_validation["generationConfidence"] = (candidate.get("llm_response") or {}).get("confidence")
                if not generation_validation.get("valid", True):
                    aggregation_validation = {
                        **aggregation_validation,
                        "valid": False,
                        "errors": [
                            *aggregation_validation.get("errors", []),
                            *generation_validation.get("errors", []),
                        ],
                    }
            if structured_aggregation_errors:
                aggregation_validation = {
                    **aggregation_validation,
                    "valid": False,
                    "errors": [
                        *aggregation_validation.get("errors", []),
                        *[error.to_dict() for error in structured_aggregation_errors],
                    ],
                }
            if not aggregation_validation.get("valid"):
                aggregation_issues = [
                    {
                        "code": error.get("code", "aggregation_requirement_failed").casefold(),
                        "severity": "error",
                        "message": error.get("message", "집계 요구사항 검증에 실패했습니다."),
                    }
                    for error in aggregation_validation.get("errors", [])
                ]
                validation = {**validation, "issues": [*validation["issues"], *aggregation_issues], "is_valid": False}
        intent_sql_contract: dict[str, Any] = {"ran": False}
        analytical_intent = query_plan.get("analytical_intent")
        if isinstance(analytical_intent, dict):
            intent_sql_contract = validate_intent_sql_contract(
                analytical_intent,
                candidate["sql"],
                dialect=validation.get("dialect") or _member_dialect().name,
            )
            if not intent_sql_contract.get("valid"):
                contract_issues = [
                    {
                        "code": issue.get("code", "intent_sql_contract_failed"),
                        "severity": "error",
                        "message": issue.get("message", "SQL does not satisfy the detected intent contract."),
                    }
                    for issue in intent_sql_contract.get("issues", [])
                ]
                validation = {**validation, "issues": [*validation["issues"], *contract_issues], "is_valid": False}
        metric_profile_validation = validate_metric_profile(analytical_intent, schema_path)
        if not metric_profile_validation.get("valid", True):
            validation = {
                **validation,
                "issues": [*validation["issues"], *metric_profile_validation.get("issues", [])],
                "is_valid": False,
            }
        validated_candidates.append(
            {
                **candidate,
                "validation": validation,
                "coverage": coverage,
                "intent_scope": intent_scope,
                "unmentioned_conditions": unmentioned_conditions,
                "join_keys": join_keys,
                "aggregation_validation": aggregation_validation,
                "intent_sql_contract": intent_sql_contract,
                "metric_profile_validation": metric_profile_validation,
                "analytics_warnings": analytics_warnings,
                "delivery_validation": delivery_validation,
                "is_eligible": (
                    validation["is_valid"] and coverage["is_satisfied"] and intent_scope["is_satisfied"]
                    and delivery_validation["is_satisfied"]
                ),
            }
        )
        validated_candidates[-1]["is_eligible"] = validated_candidates[-1]["is_eligible"] and unmentioned_conditions["is_satisfied"]

    selected = next((candidate for candidate in validated_candidates if candidate["is_eligible"]), None)
    if selected is None and validated_candidates:
        selected = validated_candidates[0]
    llm_fallback_used = bool(selected and selected.get("source") == "llm_generated" and selected.get("is_eligible"))

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
        if selected.get("intent_sql_contract", {}).get("ran") and not selected.get("intent_sql_contract", {}).get("valid", True):
            failure_reason = "intent_sql_contract_failed"
        elif selected.get("aggregation_validation", {}).get("ran") is not False and not selected.get("aggregation_validation", {}).get("valid", True):
            failure_reason = "aggregation_validation_failed"
        elif not selected.get("metric_profile_validation", {}).get("valid", True):
            failure_reason = selected["metric_profile_validation"].get("reason_code") or "UNUSABLE_METRIC_COLUMN"
        elif not selected.get("join_keys", {}).get("is_valid", True):
            first_join_issue = next(iter(selected["join_keys"].get("issues") or []), {})
            failure_reason = first_join_issue.get("reason_code") or "join_validation_failed"
        elif not selected["validation"]["is_valid"]:
            failure_reason = "sql_guard_failed"
        elif not selected["coverage"]["is_satisfied"]:
            failure_reason = "query_plan_conditions_missing"
        elif not selected["intent_scope"]["is_satisfied"]:
            failure_reason = "intent_scope_mismatch"
        elif not selected["unmentioned_conditions"]["is_satisfied"]:
            failure_reason = "query_plan_unmentioned_conditions_added"
        elif not selected.get("delivery_validation", {}).get("is_satisfied", True):
            failure_reason = selected["delivery_validation"].get("failure_reason") or "semantic_conditions_not_covered"

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
        elif failure_reason == "no_sql_candidates" and _recognized_domains(query_plan):
            # 파서가 슬롯을 못 채웠지만 도메인 어휘는 있었다 = "조건을 못 찾음"이 아니라 "그 형태 미지원".
            # 원인을 구분해 둬야 안내 문구도, 다음에 무엇을 구현해야 하는지도 정확해진다.
            failure_reason = "recognized_domain_unsupported"

    # 최종 SQL↔원문 의미 검증 게이트: plan 을 신뢰하는 결정론 검증(coverage/intent_scope)과 달리, 원문 NL 과
    # SQL 을 직접 대조해 정규식 파서의 조용한 드롭·의미 반전(예: '구매 이력이 없는'을 EXISTS 구매로 뒤집음)을
    # 잡는다. 불일치를 확신하면(ran & not faithful) 틀린 SQL 을 조용히 출고하는 대신 clarification 으로 전환한다.
    # LLM 불가/게이트 비활성이면 ran=False 라 통과(fail-open) — rules 모드는 llm_model=None 이라 자연히 skip.
    semantic_verification: dict[str, Any] = {"ran": False}
    clarification_questions: list[str] = []
    # 의미 검증에서 차단돼 출고(sql=None)되지 않더라도, "무엇이 생성됐는지" 확인용으로 원본 SQL 을 보존한다.
    # 실행은 sql(=None)로만 하므로 blocked_sql 은 화면 표시 전용 — 차단된 SQL 이 자동 실행되는 일은 없다.
    blocked_sql: str | None = None
    sql_result_requirements: list[dict[str, Any]] = []  # 공통 requirement 회계 결과(트레이스·디버깅 노출)
    delivery_validation: dict[str, Any] = (
        dict(selected.get("delivery_validation") or {}) if selected else {"is_satisfied": False}
    )
    if selected_sql is not None:
        semantic_verification = _verify_sql_semantics(
            original_query or query, selected_sql, llm_model, prompt_dir, query_plan
        )
        delivery_validation = _validate_sql_delivery_contract(
            original_query or query,
            query_plan,
            selected_sql,
            dialect=target_dialect,
            semantic_verification=semantic_verification,
            dropped_conditions=selected.get("dropped_conditions") or [],
        )
        delivery_validation["required_conditions"] = len(required_conditions)
        delivery_validation["condition_tokens"] = len(condition_tokens)
        if semantic_verification.get("ran"):
            semantic_verification = {
                **semantic_verification,
                "issues": delivery_validation.get("semantic_issues", []),
            }
        # faithful=false는 issue 세부 분류와 무관하게 차단한다. 결정론 AST/스키마 계약이 정상이어도
        # 원문↔SQL 직접 검증에서 불일치가 확인된 SQL을 success로 출고하지 않는다.
        blocking_issues = []
        if semantic_verification.get("ran") and not semantic_verification.get("faithful"):
            blocking_issues = [
                issue for issue in semantic_verification.get("issues", [])
                if issue.get("severity") == "critical"
            ]
        # 공통 semantic requirement 회계(브랜드 전용 감지기 대체): 원문 조건을 source requirement 로 기록하고
        # base×qualifier capability + SQL 반영 여부로 귀결한다. 미지원 조합(장바구니+브랜드 등)은 unsupported,
        # 지원인데 SQL 에 없으면 clarification(사일런트 드롭). 하나라도 terminal 로 귀결 못 하거나 차단 상태면
        # needs_clarification 로 승격 — 명시 조건이 조용히 빠진 채 '성공'으로 나가는 부분추출을 막는다. 결정론
        # 이라 LLM 판정에 의존하지 않는다.
        requirement_accounting = _account_source_requirements(original_query or query, query_plan, selected_sql, selected)
        requirement_blocking = requirement_accounting.blocking() if requirement_accounting else []
        if requirement_accounting is not None:
            sql_result_requirements = requirement_accounting.to_list()
        if blocking_issues or requirement_blocking or not delivery_validation.get("is_satisfied", True):
            failure_reason = "semantic_verification_failed"
            clarification_questions = _semantic_verification_clarifications(blocking_issues) + _unique_strings(
                [req.message for req in requirement_blocking if req.message]
            )
            if not clarification_questions and semantic_verification.get("faithful") is False:
                clarification_questions = [
                    "생성 SQL이 원문의 핵심 집계·필터·그룹 의도와 일치하지 않습니다. 요청 의도를 확인해 주세요."
                ]
            blocked_sql = selected_sql  # 표시용 보존(출고는 막되 무엇이 생성됐는지 노출)
            selected_sql = None
            target_connection = None
            target_dialect = None

    # 명시적 미지원 표현(예: 주문 집계 지표의 '평균 대비' 비교): 조용한 빈결과/오답 대신 unsupported_reason 과
    # clarification 을 명시 응답한다. 결정론 게이트(_apply_unsupported_intent_gate)가 plan 에 표시해 둔다.
    unsupported_intent = query_plan.get("unsupported") if isinstance(query_plan.get("unsupported"), dict) else None
    unsupported_reason = None
    if selected_sql is None and unsupported_intent:
        unsupported_reason = unsupported_intent.get("reason")
        if unsupported_reason:
            failure_reason = unsupported_reason
        clarification = unsupported_intent.get("clarification")
        if clarification and not clarification_questions:
            clarification_questions = [clarification]

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

    # 쿼리 성능 튜닝(정적 자문): 출고되는 SQL 의 실행 함정(선행 와일드카드/캐스트 조인/안티조인 등)을 진단하고
    # 권장 인덱스를 제안한다. SQL 을 바꾸지 않는 비차단 자문이라 실패해도 SQL 출고엔 영향 없음.
    query_tuning = {"findings": [], "recommended_indexes": []}
    if selected_sql is not None:
        try:
            query_tuning = analyze_query_performance(selected_sql)
        except Exception:
            query_tuning = {"findings": [], "recommended_indexes": []}

    # ③ 놓침을 시끄럽게(결정론): 원문 신호가 plan 에 조용히 드롭됐으면 경고(rules/auto 항상, 비차단).
    try:
        dropped_signal_warnings = _deterministic_dropped_conditions(original_query or query, query_plan)
    except Exception:
        dropped_signal_warnings = []

    # 결정론 의미 보존 불변식 게이트: LLM 게이트(_verify_sql_semantics)와 달리 SQL 이 생성되면 LLM 유무와
    # 무관하게 항상 실행된다(ran=True). 창 도메인 누수·누적↔롤링 혼입·구매 미발생 silent drop 을 결정론으로
    # 점검해 '조용한 오답 출고'를 막는다(비차단 자문 — 감지되면 issues 에 남고 dropped 는 경고로도 고지된다).
    if selected_sql is not None:
        semantic_invariants = _verify_sql_semantic_invariants(
            original_query or query, query_plan, selected_sql, dropped_signal_warnings
        )
        if not semantic_invariants.get("ok", True):
            failure_reason = "semantic_verification_failed"
            clarification_questions = _unique_strings([
                *clarification_questions,
                *[
                    str(issue.get("detail") or "핵심 조건의 SQL 반영을 확인해 주세요.")
                    for issue in semantic_invariants.get("issues", [])
                    if isinstance(issue, dict)
                ],
            ])
            blocked_sql = selected_sql
            selected_sql = None
            target_connection = None
            target_dialect = None
    else:
        semantic_invariants = {"ran": False, "ok": True, "issues": []}

    # 의미 검증 v2(AST 기반, shadow/enforce): 규칙 기반 결과 집합 의미 판정. 기본 off. shadow 는 결과만
    # 싣고(트레이스/로깅) 사용자 판정은 기존 게이트 유지. enforce 는 v2 가 구체 근거로 fail 일 때만 차단.
    semantic_validation_v2: dict[str, Any] = {"ran": False}
    v2_mode = _semantic_validation_v2_mode()
    if v2_mode != "off" and selected_sql is not None:
        semantic_validation_v2 = _run_semantic_validation_v2(
            original_query or query, selected_sql, query_plan, target_dialect, context_nodes)
        _write_rag_llm_log("semantic_validation_v2", {
            "query": original_query or query, "mode": v2_mode,
            "legacy_faithful": semantic_verification.get("faithful"),
            "v2": semantic_validation_v2.get("result", {}).get("status") if semantic_validation_v2.get("ran") else None,
            "v2_reason_codes": semantic_validation_v2.get("result", {}).get("reason_codes"),
        })
        if v2_mode == "enforce" and semantic_validation_v2.get("ran"):
            v2_result = semantic_validation_v2.get("result", {})
            if v2_result.get("status") == "fail":
                failure_reason = "semantic_verification_failed"
                v2_questions = [
                    f"[의미검증] 요구 '{cid}' 이 SQL 에 반영되지 않았거나 반대로 반영된 것으로 보입니다. 확인해 주세요."
                    for cid in (v2_result.get("missing_requirements") or [])
                ] or ["[의미검증] 생성 SQL 이 원문 요구를 충족하지 못한 것으로 보입니다. 확인해 주세요."]
                clarification_questions = _unique_strings([*clarification_questions, *v2_questions])
                if selected_sql is not None:
                    blocked_sql = selected_sql
                selected_sql = None
                target_connection = None
                target_dialect = None

    # 신뢰도는 모든 의미/스키마/집계 검증이 끝난 뒤 최종 상태로 보정한다. 중간 후보 점수가 높았어도
    # SQL이 차단됐으면 높음으로 노출하지 않는다.
    if selected_sql is None:
        confidence = _failed_sql_confidence(failure_reason)

    return {
        "sql": selected_sql,
        # 의미 검증 등으로 출고가 막혔지만 생성은 된 SQL(표시 전용, 실행 안 함). 정상 출고 시엔 None.
        "blocked_sql": blocked_sql,
        "target_connection": target_connection,
        "target_dialect": target_dialect,
        "selected": selected,
        "candidates": validated_candidates,
        "candidate_count": len(validated_candidates),
        "condition_tokens": condition_tokens,
        "required_conditions": required_conditions,
        "input_validation": input_validation,
        "missing_input_conditions": [],
        "clarification_questions": clarification_questions,
        # 의미 검증 게이트 판정(트레이스/디버깅용): {ran, faithful, issues}. ran=False 면 게이트 미실행.
        "semantic_verification": semantic_verification,
        # 공통 semantic requirement 회계(트레이스/디버깅용): 원문 조건별 귀결(compiled/unsupported/clarification).
        "source_requirements": sql_result_requirements,
        # 결정론 의미 보존 불변식(SQL 생성 시 항상 실행): {ran, ok, issues}. LLM 게이트와 독립.
        "semantic_invariants": semantic_invariants,
        # 최종 fail-closed 출고 계약: expected/actual grain, 조건별 SQL evidence, polarity, API 컬럼.
        "delivery_validation": delivery_validation,
        # 의미 검증 v2(AST 기반, shadow/enforce): {ran, spec, result, legacy}. off 면 ran=False.
        "semantic_validation_v2": semantic_validation_v2,
        # 일반 집계 요구사항 IR과 SQL AST의 강제 검증 결과. valid=false면 SQL은 출고·실행되지 않는다.
        "aggregation_request": query_plan.get("aggregation_request"),
        "aggregation_validation": (selected or {}).get("aggregation_validation", {"ran": False}),
        # QueryIntent의 기대 결과 shape·집계 함수·지표 컬럼·랭킹 방향/TOP 1 계약 검증.
        "intent_sql_contract": (selected or {}).get("intent_sql_contract", {"ran": False}),
        "metric_profile_validation": (selected or {}).get("metric_profile_validation", {"ran": False, "valid": True}),
        # 쿼리 성능 튜닝 자문(비차단): {findings, recommended_indexes}. 출고 SQL 이 없으면 빈 결과.
        "query_tuning": query_tuning,
        # ③ 결정론 드롭 경고: 원문 신호가 plan 에 안 잡힌 조건 목록(비차단 자문).
        "dropped_signal_warnings": dropped_signal_warnings,
        "unsupported_conditions": unsupported_conditions,
        "unsupported_condition_labels": unsupported_condition_labels,
        # 명시적 미지원 표현의 사유 코드(예: average_comparison_metric_unsupported). 지원 표현이면 None.
        "unsupported_reason": unsupported_reason,
        "dropped_conditions": dropped_conditions,
        "dropped_condition_labels": dropped_condition_labels,
        # 프롬프트가 명시한 결과 행수 제한(없으면 None = 전체). sql_guard 가 방언별 TOP/LIMIT 로 반영한다.
        "result_limit": result_limit,
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

    for index, age_range in enumerate(target_user.get("age_exclude_ranges", [])):
        if isinstance(age_range, (list, tuple)) and len(age_range) == 2 and all(isinstance(v, int) for v in age_range):
            lo, hi = age_range
            _add_token(
                tokens, f"target_user.age_exclude_ranges[{index}]", "age", "not_between",
                f"{lo}-{hi}", [f"NOT (u.age BETWEEN {lo} AND {hi})"], [],
            )

    inactivity_period = target_user.get("inactivity_period")
    if isinstance(inactivity_period, dict) and isinstance(inactivity_period.get("sql_interval"), str):
        _add_inactivity_period_token(tokens, inactivity_period)

    recent_login = target_user.get("recent_login")
    if isinstance(recent_login, dict) and isinstance(recent_login.get("sql_interval"), str):
        _add_token(
            tokens,
            "target_user.recent_login",
            "recent_login",
            ">=",
            recent_login["sql_interval"],
            ["u.last_login_at >= CURRENT_TIMESTAMP - INTERVAL " + _sql_quote(recent_login["sql_interval"])],
            [],
            select_columns=["u.last_login_at"],
        )

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

    purchase_membership = target_user.get("purchase_membership")
    # Analytical aggregation SQL expresses positive membership by reading the fact table directly
    # (for example COUNT(DISTINCT member_key) FROM orders).  Requiring a targeting-only EXISTS
    # shape here rejects valid aggregate SQL even though aggregation AST and delivery evidence
    # already prove the same condition.
    if (
        not isinstance(query_plan.get("aggregation_request"), dict)
        and isinstance(purchase_membership, dict)
        and purchase_membership.get("operator") == "exists"
        and purchase_membership.get("satisfied_by") != "aggregate_conditions"
    ):
        _add_token(
            tokens, "target_user.purchase_membership", "purchase", "exists",
            purchase_membership.get("window_days") or "any_time",
            [_purchase_membership_predicate(purchase_membership.get("window_days"))],
            [_order_count_targets_config().get("table", "CRM_SL_ORDERHEADERMALL")],
        )

    purchase_inactivity = target_user.get("purchase_inactivity")
    if isinstance(purchase_inactivity, dict) and isinstance(purchase_inactivity.get("min_days"), int):
        _add_token(
            tokens, "target_user.purchase_inactivity", "purchase", "not_exists",
            purchase_inactivity["min_days"], [_purchase_inactivity_predicate(purchase_inactivity["min_days"])],
            [_order_count_targets_config().get("table", "CRM_SL_ORDERHEADERMALL")],
        )

    if target_user.get("cart_absence"):
        _add_token(tokens, "target_user.cart_absence", "cart", "not_exists", True,
                   [_cart_absence_predicate()], [_cart_targets_registry().get("table", "ODS_MALL_OMS_CART")])

    for index, response in enumerate(target_user.get("campaign_responses") or []):
        if not isinstance(response, dict) or not response.get("predicate"):
            continue
        operator = "not_exists" if response.get("negated") else "exists"
        _add_token(
            tokens, f"target_user.campaign_responses[{index}]", "campaign_response", operator,
            response.get("canonical") or "campaign_response",
            [_campaign_response_exists_predicate(
                str(response["predicate"]), negated=bool(response.get("negated")), source=response.get("source")
            )],
            [],
        )

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

    # 회원 기준 디멘션(지역/직업 등)도 검증 토큰으로 승격한다. 예전에는 cart 브랜드만 토큰화해
    # "서울 회원"이 required_conditions=1, condition_tokens=0인 모순 상태였다.
    if brand_filter is None:
        for index, dimension_filter in enumerate(query_plan.get("dimension_filters") or []):
            if not isinstance(dimension_filter, dict):
                continue
            column = str(dimension_filter.get("column") or "").split(".")[-1]
            codes = [str(code) for code in dimension_filter.get("codes") or [] if str(code)]
            if not column or not codes:
                continue
            alias = "B" if dimension_filter.get("table") == _member_table() else "S"
            clause = f"{alias}.{column} IN ({', '.join(_sql_quote(code) for code in codes)})"
            _add_token(
                tokens, "dimension_filters." + str(dimension_filter.get("dimension_id") or index),
                "dimension_filter", "in", ",".join(codes), [clause], [str(dimension_filter.get("table") or "")],
            )

    region_count = query_plan.get("region_member_count_target")
    if isinstance(region_count, dict):
        column = str(region_count.get("column") or "SIGUNGU")
        _add_token(tokens, "region_member_count_target", "aggregation", "group_by", column,
                   [f"GROUP BY B.{column}", "COUNT(DISTINCT"], [_member_table()])

    # 정렬/랭킹 전용 빌더 조건도 검증 토큰으로 명시한다. 이들은 WHERE 술어가 아니라 TOP/ORDER BY/
    # PARTITION BY 구조라 기존 토큰 생성기가 0개를 반환했고, fail-closed 0-token 게이트에 정상 SQL까지 막혔다.
    for path, value, clauses in (
        (
            "member_metric_ranking",
            (query_plan.get("member_metric_ranking") or {}).get("metric_id"),
            ["ORDER BY", "TOP"],
        ),
        (
            "group_ranking_target",
            (query_plan.get("group_ranking_target") or {}).get("metric_id"),
            ["ROW_NUMBER", "PARTITION BY"],
        ),
        (
            "region_density_target",
            (query_plan.get("region_density_target") or {}).get("column"),
            ["GROUP BY", "TOP"],
        ),
        (
            "purchase_count_ranking",
            "purchase_count" if isinstance(query_plan.get("purchase_count_ranking"), dict) else None,
            ["ORDER BY", "TOP"],
        ),
        (
            "member_metric_selection",
            (query_plan.get("member_metric_selection") or {}).get("metric_id"),
            ["ORDER BY"],
        ),
    ):
        if value is not None:
            _add_token(tokens, path, "ranking", "rank", value, clauses, [])

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


def _is_owned_aggregate_base_exclusion(ast: Any, plan: dict[str, Any]) -> bool:
    """`집계 대상 고객 중 X 제외`에서 집계 base를 set unknown으로 중복 보유한 AST인지 판정한다.

    구매금액/횟수 같은 base는 set operand가 아니라 aggregate_conditions가 이미 정확히 소유한다. 우변 제외
    대상은 뒤의 정규화 matched-term 루프가 exclude 슬롯에 넣으므로, 이 중복 set AST를 유지하면 unknown
    clarification만 발생하고 정상 집계 SQL이 차단된다. 진짜 미정 집합식은 aggregate 조건이 없으므로 보존한다.
    """
    if not isinstance(ast, dict) or ast.get("type") != "set_op" or ast.get("op") != "-":
        return False
    left, right = ast.get("left"), ast.get("right")
    aggregates = plan.get("target_user", {}).get("aggregate_conditions") or []
    return bool(
        aggregates
        and isinstance(left, dict)
        and left.get("type") == "unknown_operand"
        and isinstance(right, dict)
        and _compile_set_expression_ast(right)["is_valid"]
    )


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
        if _is_owned_aggregate_base_exclusion(ast, plan):
            continue
        if not isinstance(ast, dict) or _set_ast_has_unknown_operand(ast) or _compile_set_expression_ast(ast)["is_valid"]:
            kept.append(expression)  # 파서 clarification / 미정규화 값 / 컴파일 가능 → 유지
    plan["set_expressions"] = kept


# 집합 연산이 실제 의미를 갖는 '세그먼트류' 피연산자 canonical(행동/관심/채널/성향). 이 중 하나라도 있으면
# operator-scan 집합식이라도 진짜 집합연산으로 보고 유지한다. 나머지(성별/연령/등급/지역/지표)는 결정론
# dimension/속성/집계 필터가 소유하므로 operator-scan 집합식은 리던던트다.
def _set_level_segment_canonicals() -> set[str]:
    return set(BEHAVIOR_TERMS) | set(INTEREST_TERMS) | set(CHANNEL_TERMS) | {"price_sensitive", "premium_buyer", "coupon"}


def _iter_all_set_operands(ast: Any):
    """set_ast 의 모든 리프 피연산자 노드(operand/unknown_operand/age_range)를 재귀로 내준다."""
    if not isinstance(ast, dict):
        return
    if ast.get("type") in ("operand", "unknown_operand", "age_range"):
        yield ast
        return
    yield from _iter_all_set_operands(ast.get("left"))
    yield from _iter_all_set_operands(ast.get("right"))


def _plan_dimension_filter_has_value(plan: dict[str, Any], value: str) -> bool:
    """dimension_filters(지역 등)가 이 값을 이미 소비했는지(names/codes 경계 일치)."""
    fold = str(value).casefold()
    for f in plan.get("dimension_filters", []):
        if not isinstance(f, dict):
            continue
        for v in (f.get("names") or []) + (f.get("codes") or []):
            if isinstance(v, str) and v.casefold() == fold:
                return True
    return False


def _set_operand_text_dimension_consumed(text: str, plan: dict[str, Any]) -> bool:
    """미해결 집합 operand 표면어가 이미 결정론 dimension 필터(지역 등)로 소비된 값인지 판정한다.

    '서울 또는 경기'처럼 지역 값이 dimension_filters(SIDO IN)로 이미 처리됐는데 operator-scan 폴백이 같은
    지역을 unknown_operand 로 다시 물어 SQL 을 막던 중복을 걸러낸다(source span 이 없으므로 값 동등성으로 판정)."""
    stripped = text.strip() if isinstance(text, str) else ""
    if not stripped:
        return False
    if _plan_dimension_filter_has_value(plan, stripped):
        return True
    region = _region_value_from_query(stripped)
    return bool(region and _plan_dimension_filter_has_value(plan, region))


def _set_operand_text_attribute_consumed(text: str, plan: dict[str, Any]) -> bool:
    """미해결 집합 operand 표면어가 이미 결정론 등급(lifecycle) 필터로 소비된 값인지 판정한다.

    '골드 또는 VIP'처럼 등급 OR 이 lifecycle(→EMART_GRADE_CD IN)로 처리됐는데, auto 재작성이 만든 콤마
    나열형("골드 또는 VIP 회원, 로그인 200회 이상, …")에서 operator-scan 이 같은 등급 '골드'를
    unknown_operand 로 다시 물어 SQL 을 막던 중복을 걸러낸다(지역 소비 판정 _set_operand_text_dimension_
    consumed 의 등급 판). 반드시 lifecycle 이 채워진 뒤 호출돼야 소비로 인정된다(미포착이면 clarification 유지)."""
    if not isinstance(text, str) or not text.strip():
        return False
    folded = text.strip().casefold()
    lifecycle = plan.get("target_user", {}).get("lifecycle") or []
    return any(surface in folded and value in lifecycle for surface, value in _GRADE_SURFACE_TO_VALUE)


def _operator_scan_expression_fully_owned(expression: dict[str, Any], plan: dict[str, Any]) -> bool:
    """operator-scan 집합식이 결정론 dimension/속성/집계 필터로 '완전히 소유'된 리던던시인지 판정한다.

    True 조건: (1) 진짜 세그먼트류(_set_level_segment_canonicals) 피연산자가 하나도 없고, (2) 미해결
    operand 는 전부 dimension(지역) 또는 lifecycle(등급) 필터가 이미 소비한 값이다. 이때 집합식은 결정론
    필터가 커버하는 평범한 dimension/등급 OR/AND 나열이므로 버려도 조건이 사라지지 않는다(오히려 데모
    스키마 오컴파일·중복 clarification 방지). 지표(로그인횟수/구매금액 등) operand 는 세그먼트류가 아니라
    balance/aggregate 필터 소유이므로 drop 을 막지 않는다."""
    ast = expression.get("set_ast")
    operands = list(_iter_all_set_operands(ast))
    if not operands:
        return False
    segment_canonicals = _set_level_segment_canonicals()
    for operand in operands:
        if operand.get("type") == "unknown_operand":
            text = operand.get("text", "")
            if not (_set_operand_text_dimension_consumed(text, plan) or _set_operand_text_attribute_consumed(text, plan)):
                return False  # 미소비 unknown = 진짜 세그먼트/clarification 대상 → 유지
        elif operand.get("canonical") in segment_canonicals:
            return False  # 진짜 세그먼트 피연산자 → 집합식 유지
    return True


def _drop_dimension_consumed_set_expressions(plan: dict[str, Any]) -> None:
    """dimension/속성 필터가 이미 소유한 operator-scan 집합식(평범한 '서울 또는 경기' 지역 OR 등)을 버린다.

    반드시 dimension_filters·gender·lifecycle 등 결정론 조건이 모두 채워진 뒤(POST 필터 후) 호출한다 —
    소유권 판정이 채워진 슬롯을 봐야 하기 때문. natural/postfix(진짜 집합-구조) 집합식은 대상이 아니다."""
    expressions = plan.get("set_expressions")
    if not isinstance(expressions, list) or not expressions:
        return
    kept = [
        expression for expression in expressions
        if not (
            isinstance(expression, dict)
            and expression.get("detection") == "operator_scan"
            and _operator_scan_expression_fully_owned(expression, plan)
        )
    ]
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


def _compilable_set_expression_canonical_values(expressions: list[dict[str, Any]]) -> set[str]:
    """컴파일 가능한 집합식의 canonical 만 — matched-term 스킵(집합식 소유권) 판단용.

    unknown operand 가 남았거나 컴파일 불가한 집합식은 SQL 이 되지 못하므로 term 소유권을 주지 않는다.
    이전엔 '서울 또는 경기' 지역 나열이 문장 전체를 집합식으로 감싸며 female 같은 회원속성을 operand 로
    삼켰고, 집합식 자체는 clarification 으로만 남는데 term 의 일반 적용(gender 등)도 스킵돼 조건이
    통째로 증발했다. (validate_unmentioned_sql_conditions 의 관대한 검사는 기존 전체 수집을 유지한다.)"""
    values: set[str] = set()
    for expression in expressions:
        ast = expression.get("set_ast")
        if not isinstance(ast, dict) or _set_ast_has_unknown_operand(ast):
            continue
        if not _compile_set_expression_ast(ast)["is_valid"]:
            continue
        values.update(_set_ast_canonical_values(ast))
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
    # 단일 식별자 별칭. 기존 데모(u/c)뿐 아니라 실DB 회원 별칭(B)도 허용하되 함수/연산/주석은 금지한다.
    return bool(re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z_][a-z0-9_]*", expression.strip(), re.IGNORECASE))


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
    candidate: dict[str, Any], query_plan: dict[str, Any], compiled: dict[str, Any],
    covered_behaviors: frozenset[str] = frozenset({"cart_abandoner"}),
) -> None:
    """cart 템플릿용 부분 추출 고지(형제 빌더와 동일 규칙). 장바구니 행동(cart_abandoner)은 템플릿
    자체가 커버하므로 behaviors 가 그것뿐이면 dropped 에서 제외한다(purchase_object 처리와 같은 방식).
    빌더가 추가로 컴파일한 행동(예: 카트 집계 빌더의 no_purchase anti-join)은 covered_behaviors 로 넘겨
    dropped 에서 함께 뺀다."""
    behaviors = set(query_plan.get("target_user", {}).get("behaviors", []))
    dropped = [
        path
        for path in compiled["unsupported"]
        if not (path == "target_user.behaviors" and behaviors <= covered_behaviors)
    ]
    candidate["dropped_conditions"] = dropped
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in dropped]


def _cart_retention_column() -> str:
    """장바구니 보관 기간 비교에 쓸 시점 컬럼명(테이블 접두어 없는 짧은 이름)을 준다.

    ODS_MALL_OMS_CART.INS_DT 는 '담은 시점'이 아니라 ETL 적재 시각이다 — 전 행이 단일 값
    (2020-02-03 14:23:14.850, 38,133행 중 distinct 1개)이라 어떤 임계값을 걸어도 전건 통과 아니면
    전건 탈락인 계단 함수가 된다(= 기간 조건이 조용히 사라짐). 행마다 실제로 다른 시점을 갖는 컬럼은
    UPD_DT(distinct 33,446, 2016-12~2017-01)뿐이고, KEEP_YN='Y' 인 미결제 라인에서는 마지막으로
    그 라인을 건드린 시각 = 방치 시작점이므로 '담아둔 지 N일'의 근사로 맞다."""
    configured = _MEMBER_TARGET_FILTERS.get("cart_targets", {}).get("registered_date_column")
    if not isinstance(configured, str) or not configured.strip():
        configured = "C.UPD_DT"
    return configured.split(".")[-1]


def _cart_retention_predicates(query_plan: dict[str, Any], alias: str = "A") -> list[str]:
    """장바구니 보관 기간(cart_retention)을 담은 시점 술어로 만든다(없으면 빈 목록).

    '일주일 이상 유지' = 담은 지 7일이 지나도록 아직 KEEP_YN='Y' 이므로 시점 <= 기준일-7일이다.
    KEEP_YN='Y' 만 걸면 담은 시점과 무관한 '아직 안 산 회원' 전체가 되어 기간 조건이 조용히 사라진다.
    컬럼은 레지스트리(cart_targets.registered_date_column) 소유 — 어느 컬럼이 실제 담은 시점인지는
    데이터 사실이라 코드에 박지 않는다(_cart_retention_column 참고). datetime2 라 형제 빌더의
    YYYYMMDD 문자열 컬럼과 달리 CONVERT 없이 그대로 비교한다."""
    retention = query_plan.get("target_user", {}).get("cart_retention")
    if not isinstance(retention, dict):
        return []
    column = (alias + "." if alias else "") + _cart_retention_column()
    min_days = retention.get("min_days")
    if isinstance(min_days, int) and min_days > 0:
        return [f"{column} <= {_member_dialect().datetime_cutoff(min_days)}"]
    max_days = retention.get("max_days")
    if isinstance(max_days, int) and max_days > 0:
        return [f"{column} >= {_member_dialect().datetime_cutoff(max_days)}"]
    return []


def _cart_type_column() -> str:
    """장바구니 유형 컬럼(레지스트리 cart_targets.cart_type_column 소유, 없으면 CART_TYPE_CD)."""
    configured = _MEMBER_TARGET_FILTERS.get("cart_targets", {}).get("cart_type_column")
    if not isinstance(configured, str) or not configured.strip():
        configured = "C.CART_TYPE_CD"
    return configured.split(".")[-1]


def _cart_type_predicates(query_plan: dict[str, Any], alias: str = "A") -> list[str]:
    """장바구니 유형(cart_type)을 CART_TYPE_CD 등가 술어로 만든다(없으면 빈 목록).

    저장값은 도메인 접두어를 포함한다('CART_TYPE_CD.REGULARDELIVERY') — 값은 레지스트리가 들고 있고
    코드가 접두어를 조립하지 않는다."""
    cart_type = query_plan.get("target_user", {}).get("cart_type")
    if not isinstance(cart_type, dict) or not isinstance(cart_type.get("value"), str) or not cart_type["value"]:
        return []
    column = (alias + "." if alias else "") + _cart_type_column()
    return [f"{column} = {_sql_quote(cart_type['value'])}"]


def _cart_is_unpaid_only(query_plan: dict[str, Any]) -> bool:
    """카트 오디언스를 미결제 보관(KEEP_YN='Y') 라인으로 좁혀야 하는지.

    기본은 좁히는 쪽이다(카트 템플릿의 원래 대상 = 담고 결제 안 한 회원). 다만 '정기배송 상품을 담은
    회원'처럼 유형만 물은 질의는 보관 여부를 묻지 않았는데도 KEEP_YN='Y' 를 붙이면 사용자가 걸지 않은
    조건으로 결과가 전멸한다(실측: 정기배송 카트 라인 3,155건이 전부 KEEP_YN='N'). 미결제/이탈 표현이
    실제로 있었을 때만(_is_cart_abandonment_query 가 파서에서 판정) 다시 좁힌다."""
    target_user = query_plan.get("target_user", {})
    cart_type = target_user.get("cart_type")
    if not isinstance(cart_type, dict):
        return True
    return bool(cart_type.get("unpaid_only")) or isinstance(target_user.get("cart_retention"), dict)


def _cart_keep_predicates(query_plan: dict[str, Any], alias: str = "A") -> list[str]:
    column = (alias + "." if alias else "") + "KEEP_YN"
    return [f"{column} = 'Y'"] if _cart_is_unpaid_only(query_plan) else []


def _cart_segment_label(query_plan: dict[str, Any]) -> str:
    """카트 템플릿의 target_segment 라벨. 유형만 물은 오디언스는 '이탈'이 아니므로 유형 canonical 로 쓴다."""
    cart_type = query_plan.get("target_user", {}).get("cart_type")
    if isinstance(cart_type, dict) and not _cart_is_unpaid_only(query_plan):
        return str(cart_type.get("canonical") or "cart_holder")
    return "cart_abandoner"


def _entity_set_config() -> dict[str, Any]:
    """파생 엔터티 집합 조건의 물리 매핑(member_target_filters.json 소유)."""
    config = _MEMBER_TARGET_FILTERS.get("entity_set_targets")
    return config if isinstance(config, dict) else {}


def _entity_set_condition_supported(query: str) -> bool:
    """이 문장을 파생 엔터티 집합 조건으로 컴파일할 수 있는지(미지원 게이트 양보 판정용)."""
    node = parse_entity_set_condition(query, _entity_set_config())
    return isinstance(node, dict) and not node.get("unsupported_reason")


def build_entity_set_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """파생 엔터티 집합 조건('가장 많이 팔린 상품 N개를 구매한 회원')을 실추출 SQL 로 컴파일한다.

    순위 집합은 서브쿼리가 만들고 회원 투영은 이 빌더가 소유한다 — 조건 절만 계산하고 대상을
    잊는 형태가 구조적으로 나올 수 없다. 성별/등급 등 회원 속성은 다른 빌더와 같은 방식으로
    AND 결합하므로 'VIP 중 작년 인기상품 구매자' 같은 조합도 하나의 SQL 이 된다.
    """
    node = (query_plan.get("target_user") or {}).get("entity_set_condition")
    if not isinstance(node, dict):
        return None
    predicate = compile_entity_set_predicate(
        node, _entity_set_config(), member_alias=_member_alias(), member_key=_member_key_column()
    )
    if predicate is None:
        return None

    compiled = compile_member_target_conditions(query_plan)
    select_columns = ["DISTINCT " + _member_key_select(), _member_grade_select()]
    if compiled["labels"]:
        select_columns.append(_sql_quote(",".join(compiled["labels"])) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    where_clauses = [predicate, *compiled["predicates"]]
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())
    ast = SelectAst(
        columns=select_columns,
        from_lines=[_member_from_clause()],
        where=_unique_strings(where_clauses),
    )
    candidate = _select_ast_candidate(
        "sql_template:entity_set_targets",
        "파생 엔터티 집합 타겟 추출 SQL 템플릿(CRMDW)",
        1.0,
        ast,
        "sql_template",
    )
    candidate["dropped_conditions"] = compiled["unsupported"]
    return candidate


def _build_cart_targets_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """장바구니(ODS_MALL_OMS_CART) 기반 실CRM 타겟 SQL 후보를 만든다(비해당이면 None).

    recommend_campaign(캠페인 발송)과 find_user_segment(세그먼트 조회) 양쪽에서 같은 템플릿을 쓴다 —
    '장바구니 이탈 고객'은 발송 여부와 무관한 오디언스라 두 의도 모두 실추출돼야 한다. 두 경로:
      (1) cart_dimension_targets: 장바구니에 특정 상품브랜드(BRAND_ID)를 담은 회원.
      (2) cart_repurchase_targets: 장바구니에 담고 아직 결제 안 함(KEEP_YN='Y') = 카트 이탈."""
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
        where_clauses = [
            *_cart_keep_predicates(query_plan),
            *_cart_retention_predicates(query_plan),
            *_cart_type_predicates(query_plan),
            f"C.{column_short} {operator} ({in_list})",
            *compiled["predicates"],
        ]
        # 회원상태 직접 지정(dormant 등)이 아니면 발송 대상 기본 정책대로 정상 회원으로 한정한다.
        if not compiled["forces_state"]:
            where_clauses.append(_member_active_state_predicate())
        select_columns = [_member_key_select()]
        if "cart_abandoner" in query_plan.get("target_user", {}).get("behaviors", []):
            select_columns.append("'cart_abandoner' AS target_segment")
        objective = query_plan.get("campaign_constraints", {}).get("objective")
        if objective:
            select_columns.append(_sql_quote(objective) + " AS objective")
        ast = SelectAst(
            distinct=True,
            columns=select_columns,
            from_lines=_cart_from_join_lines("A", product_alias="C"),
            where=_unique_strings(where_clauses),
        )
        candidate = _select_ast_candidate("sql_template:cart_dimension_targets", "장바구니 상품브랜드 타겟팅 SQL 템플릿", 1.0, ast, "sql_template")
        _attach_cart_dropped_conditions(candidate, query_plan, compiled)
        # 구조화 evidence: 검증기가 SQL 문자열(코드 'A')이 아니라 이 evidence 로 브랜드 반영을 확인한다
        # (dimension 코드 치환으로 원문 값 'CJ제일제당'은 SQL 에 없으므로 문자열 검사로는 오탐).
        candidate["applied_requirements"] = [{
            "base": "cart_retention", "qualifier": "brand", "values": list(brand_filter["codes"]),
            "strategy": "join_product_dimension", "join_path": "cart_to_product",
            "filter_field": column_short, "filter_expression": f"C.{column_short} {operator} ({in_list})",
            "resolved_via": "code",
            "source_values": [str(brand_filter.get("name") or n) for n in brand_filter.get("names", [])] or None,
        }]
        return candidate
    if _should_use_cart_repurchase_template(query_plan):
        # 타겟은 "장바구니에 담고 아직 결제 안 함"(카트 이탈)뿐 — KEEP_YN='Y'가 미결제 보관 상태를 표현한다.
        # 재구매(objective)는 메시지 목적 라벨일 뿐 타겟 필터가 아니므로, 회원 단위 주문 anti-join은 걸지 않는다.
        #   (NOT EXISTS(모든 주문)은 "평생 무주문 회원"을 뜻해 재구매 대상과 자기모순이라 제거했다.)
        # 라벨 컬럼(target_segment/objective)은 세그먼트·목적 태그이자 조건 커버리지 충족용(값은 query_plan 기준).
        # 회원 속성이 함께 오면 형제 빌더와 동일하게 B 술어로 AND 결합한다(조용한 누락 방지).
        compiled = compile_member_target_conditions(query_plan)
        objective = query_plan.get("campaign_constraints", {}).get("objective")
        # 하이브리드 폴백: dimension 코드로 못 잡힌 브랜드명은 CART→CRM_CM_PRODUCT 조인 + BRAND_NAME LIKE
        # (구매 경로와 동일 표현)로 거른다. 없으면 상품 조인을 붙이지 않아 기존 출력과 바이트 동일.
        brand_names = _cart_brand_name_qualifier(query_plan)
        brand_cf = compiler_strategies.compile_product_dimension_filter(
            base="cart_retention", qualifier="brand", product_alias="C",
            name_field="BRAND_NAME", name_values=brand_names, join_path="cart_to_product",
        ) if brand_names else None
        select_columns = [_member_key_select(), _sql_quote(_cart_segment_label(query_plan)) + " AS target_segment"]
        if objective:
            select_columns.append(_sql_quote(objective) + " AS objective")
        where_clauses = [
            *_cart_keep_predicates(query_plan),
            *_cart_retention_predicates(query_plan),
            *_cart_type_predicates(query_plan),
            *([brand_cf.filter_expression] if brand_cf else []),
            *compiled["predicates"],
        ]
        if not compiled["forces_state"]:
            where_clauses.append(_member_active_state_predicate())
        ast = SelectAst(
            distinct=True,
            columns=select_columns,
            from_lines=_cart_from_join_lines("A", product_alias="C" if brand_cf else None),
            where=_unique_strings(where_clauses),
        )
        # 제목은 실제로 건 조건을 따른다 — 유형만 물은 오디언스에 '미결제'라고 적으면 걸지도 않은
        # KEEP_YN 조건을 건 것처럼 읽힌다.
        title = (
            "장바구니 상품브랜드 타겟팅 SQL 템플릿"
            if brand_cf
            else "장바구니 미결제 재구매 유도 SQL 템플릿(CRMDW)"
            if _cart_is_unpaid_only(query_plan)
            else "장바구니 유형(정기배송 등) 타겟 SQL 템플릿(CRMDW)"
        )
        candidate = _select_ast_candidate("sql_template:cart_repurchase_targets", title, 1.0, ast, "sql_template")
        _attach_cart_dropped_conditions(candidate, query_plan, compiled)
        if brand_cf:
            candidate["applied_requirements"] = [brand_cf.to_evidence()]
        return candidate
    return None


# 실CRM 타겟 및 분석 SQL을 만드는 의도. 분석은 회원 ID 목록 계약과 분리된 집계 빌더만 사용한다.
_SQL_TARGET_INTENTS = frozenset({"recommend_campaign", "find_user_segment", "analyze_aggregation"})


def _sql_target_builder_registry() -> tuple[tuple[Any, frozenset[str]], ...]:
    """실CRM 타겟 SQL 빌더 레지스트리(우선순위 순): (빌더, 소유하는 조건 IR kind 집합).

    라우팅·소유권의 단일 소스. 새 조건 유형은 targeting_ir.CONDITION_SPECS 에 spec 을 선언하고 여기서
    소유 빌더에 kind 를 달면 발송(recommend_campaign)·조회(find_user_segment) 두 의도, 신호 감지,
    EXISTS-류 빌더 defer 까지 자동 반영된다. '모든 fact_join kind 는 정확히 하나의 빌더가 소유한다'는
    불변식을 테스트가 강제한다(소유자 없는 조건이 조용히 다른 빌더로 새는 사고 방지 — 이번
    campaign_response_frequency 이전의 '캠페인 반응 횟수→주문 집계 오배정'이 정확히 그 사고였다).
    순서 주의: purchase_count_ranking(기간 내 상위 N)은 상품 구매 이력(purchase_history)보다 먼저 — 날짜만
    있는 랭킹이 구매 이력 쪽으로 새지 않게. 반응 '횟수'(HAVING COUNT) 빌더는 EXISTS-only 캠페인 빌더보다
    먼저(EXISTS 빌더는 fact_join 신호에 양보). 빌더는 비해당이면 None 을 반환하고 다음으로 넘어간다.
    (런타임 호출이라 아래 빌더가 이 함수 정의보다 파일에서 나중에 나와도 된다.)"""
    return (
        # 등록형 일반 집계: metric/dimension/filter IR을 결정론 SelectAst로 컴파일한다. 분석 의도에서
        # 회원 목록 빌더로 폴백하면 안 되므로 가장 먼저 두고, 비분석 플랜에서는 즉시 None을 반환한다.
        (build_analytical_aggregation_sql_candidate, frozenset()),
        # 논리식(OR-of-conjunctions) 컴파일러 — AND/OR/괄호를 보존한 복합 빌더. logical_expression 슬롯이
        # 있을 때만(검증 통과) 발동하고 단일 조건 kind 를 소유하지 않는다. union 보다 우선(더 일반적).
        (build_logical_expression_sql_candidate, frozenset()),
        # 합집합(OR) 컴파일러 — 피연산자를 재귀 컴파일하는 복합 빌더라 단일 조건 kind 를 소유하지 않는다.
        (build_union_targets_sql_candidate, frozenset()),
        # 파생 엔터티 집합('가장 많이 팔린 상품 N개를 구매한 회원') — 피연산자가 순위 서브쿼리다.
        # 상품/날짜 슬롯 빌더보다 먼저: 같은 문장의 '상품'·기간 표현이 리터럴 상품 조건으로 새면
        # 순위 집합이 통째로 사라진 채 그럴듯한 SQL 이 나간다.
        (build_entity_set_targets_sql_candidate, frozenset({"entity_set_condition"})),
        # 장바구니 담김/이탈 + 보관 기간 + 유형(CART_TYPE_CD).
        (_build_cart_targets_candidate, frozenset({"cart_abandoner", "cart_retention", "cart_type"})),
        (build_cart_aggregate_targets_sql_candidate, frozenset({"cart_aggregate"})),
        # 셀 단위 비율 타겟('성공률 높고 구매율 낮은 셀') — Z_CAMP_MBR 셀 집계 HAVING → 셀 회원 조인.
        (build_cell_rate_targets_sql_candidate, frozenset({"cell_rate_target"})),
        # 캠페인 팩트 회원별 집계 — 반응 '횟수'(HAVING COUNT DISTINCT)와 '귀속 구매금액'(HAVING SUM(BUY_AMT)).
        (build_campaign_response_frequency_targets_sql_candidate, frozenset({"campaign_response_frequency", "campaign_buy_amount", "campaign_buy_count"})),
        # 캠페인 반응 EXISTS(≥1회) — 회원키 EXISTS 술어라 어느 빌더에나 compile_member_target_conditions 로
        # AND 결합된다(fact_join 아님). 이 빌더는 반응이 '주 신호'일 때만 잡고 fact_join 조건에는 양보한다.
        (build_campaign_response_targets_sql_candidate, frozenset({"campaign_responses"})),
        (build_purchase_count_ranking_sql_candidate, frozenset({"purchase_count_ranking"})),
        # "○○ 구매/구입한 고객"(상품 LIKE) + 절대 날짜 구매창(ORDER_DATE BETWEEN).
        (build_purchase_history_targets_sql_candidate, frozenset({"purchase_object", "purchase_date"})),
        # 첫 구매/재구매/무구매(주문수 집계) + 구매 미발생 기간(anti-join). 지원 집합 밖 행동
        # (unclassified_behavior)도 여기 소유로 명시 — 새 행동이 등장하면 이 빌더가 지원 여부를 판정하고
        # 미지원이면 부분추출 고지로 남긴다(조용한 누락 방지).
        (build_order_count_targets_sql_candidate, frozenset({"order_count_behavior", "purchase_inactivity", "unclassified_behavior"})),
        (build_aggregate_targets_sql_candidate, frozenset({"aggregate_conditions"})),
        # 그룹별 회원 Top-N(지역별 … N명씩): PARTITION BY 윈도. 전역 회원 랭킹보다 먼저 — 그룹 정보를
        # 보존한 별도 실행 경로(전역 랭킹이 '매출 높은 회원'을 가로채 그룹을 버리던 문제 방지).
        (build_group_ranking_sql_candidate, frozenset({"group_ranking_target"})),
        (build_member_metric_ranking_sql_candidate, frozenset({"member_metric_ranking"})),
        # 회원 컬럼(잔액) 선택 전략: 상위 N/N%/평균 대비(정렬·TOP/PERCENT·서브쿼리, 단일 테이블).
        (build_member_column_selection_sql_candidate, frozenset({"member_metric_selection"})),
        # 지역 단위 회원 수 집계 랭킹(회원 수 많은 지역 상위 N): 지역+회원수 반환. 밀집지역(거주 회원 추출)과
        # 출력 형태가 달라(지역 행 vs 회원 행) 별도 빌더로 소유한다.
        (build_region_member_count_sql_candidate, frozenset({"region_member_count_target"})),
        # 회원 속성 폴백 + 밀집 지역 랭킹(코호트 조건으로 지역 랭킹 후 거주 회원 타겟).
        (build_member_targets_sql_candidate, frozenset({"region_density_target"})),
    )


def _sql_target_builders() -> tuple[Any, ...]:
    """실CRM 타겟 SQL 빌더 목록(우선순위 순) — _sql_target_builder_registry 에서 파생."""
    return tuple(builder for builder, _owned in _sql_target_builder_registry())


def _guard_coupon_semantic_preservation(query_plan: dict[str, Any]) -> None:
    """추출된 쿠폰 의미 노드가 SQL 생성 전까지 보존됐는지 검증한다(silent semantic degradation 방지).

    미지원(_gated)으로 판정된 노드가 있는데 plan 이 미지원으로 표시돼 있지 않으면, 임계값/분모/비교대상 등
    필수 의미가 조용히 사라진 상태다 — fail-close 로 plan 을 미지원 처리해 그럴듯한 오답 SQL 출고를 막는다.
    지원되는 노드(사용 여부·컴파일 가능한 건수 임계)는 정상 컴파일되므로 검증 대상이 아니다."""
    if query_plan.get("unsupported"):
        return
    for node in query_plan.get("_coupon_ir", []):
        if node.get("_gated"):
            query_plan["unsupported"] = {
                "reason": "coupon_semantic_preservation_failed",
                "message": "쿠폰 조건의 필수 의미(임계값/범위/비교대상/파생식)가 SQL 생성 전에 소실됐습니다.",
                "clarification": "쿠폰 사용 '여부'(사용/미사용)로 지정하시거나 조건을 나눠 다시 입력해 주시겠어요?",
            }
            return


def build_sql_template_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    if query_plan.get("intent") not in _SQL_TARGET_INTENTS:
        return None
    # 쿠폰 의미 보존 검증: 미지원 쿠폰 의미가 조용히 SQL 로 축소되지 않게 마지막 방어선(fail-close).
    _guard_coupon_semantic_preservation(query_plan)
    # 미지원으로 명시된 질의는 어떤 빌더로도 폴백하지 않는다 — 그럴듯한 오답/빈결과 대신 명시 미지원 응답.
    if isinstance(query_plan.get("unsupported"), dict):
        return None
    for builder in _sql_target_builders():
        candidate = builder(query_plan)
        # 빌더가 무효 지표 등으로 plan 을 미지원 표시했으면 즉시 중단한다 — 다른 트랙으로 조용히 폴백 금지.
        if isinstance(query_plan.get("unsupported"), dict):
            return None
        if candidate is None:
            continue
        # Validation 게이트(파이프라인: 빌더 → AST → Validation → SQL): 별칭 허용 목록·raw SQL 토큰·
        # OR 분기 수 위반 후보는 채택하지 않는다(_sql_candidate 가 검증을 수행하고 여기서 거부).
        if candidate.get("validation", {}).get("issues"):
            continue
        return candidate
    # 실DB(union/cart/purchase/order/aggregate/metric/member)로 매핑 가능한 조건이 없으면 후보 없음(→ 미지원 안내).
    return None


def build_analytical_aggregation_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """Compile a registry-backed analytical intent without an LLM fallback."""
    if query_plan.get("intent") != "analyze_aggregation":
        return None
    # 미지원(지표 미해석·조건 미반영)으로 판정된 질의는 어떤 SQL 도 내지 않는다 — 호출 순서에
    # 기대지 않고 여기서도 닫는다(그럴듯한 오답 대신 명시 미지원).
    if isinstance(query_plan.get("unsupported"), dict):
        return None
    intent = query_plan.get("analytical_intent")
    request = query_plan.get("aggregation_request")
    if not isinstance(intent, dict) or not isinstance(request, dict):
        return None
    try:
        ast = compile_aggregation_ast(intent, request)
    except (KeyError, TypeError, ValueError) as exc:
        query_plan["unsupported"] = {
            "reason": "analytical_sql_compilation_failed",
            "message": f"확정된 집계 계약을 SQL AST로 컴파일하지 못했습니다: {exc}",
            "clarification": "지표·그룹 기준·필터 조합을 확인해 주세요.",
        }
        return None
    candidate = _select_ast_candidate(
        "sql_template:analytical_aggregation",
        "등록형 분석 집계 SQL 템플릿",
        1.0,
        ast,
        "sql_template",
    )
    candidate["structured_aggregation_request"] = request
    candidate["detected_intent"] = query_plan.get("detected_intent")
    return candidate


_UNSUPPORTED_CONDITION_LABELS = {
    "target_user.gender": "성별 조건",
    "target_user.interests": "관심사 조건",
    "target_user.preferred_channels": "선호 채널 조건",
    "target_user.behaviors": "행동 조건",
    "target_user.purchase_object": "구매 상품 조건",
    "target_user.aggregate_conditions": "집계 조건(구매 금액/횟수 임계값)",
    "target_user.birthday_target": "생일 조건",
    "target_user.signup_target": "가입일 조건",
    "target_user.purchase_date": "구매일 조건",
    "target_user.cart_type": "장바구니 유형 조건",
    "target_user.balance_conditions": "잔액 조건",
    "target_user.campaign_responses": "캠페인 반응 조건",
    "target_user.purchase_membership": "구매 이력 조건",
    "target_user.purchase_inactivity": "미구매 기간 조건",
    "target_user.cart_absence": "장바구니 미보유 조건",
    "target_user.age_exclude_ranges": "연령 제외 조건",
    "target_user.price_sensitivity": "가격 민감도 조건",
    "target_user.inactivity_period": "미접속 기간 조건",
    "target_user.recent_login": "최근 로그인 기간 조건",
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

    반환 dict: predicates(WHERE 술어), labels(세그먼트 라벨 canonical 값), forces_state(기본 상태필터
    (NORMAL 한정) 해제 여부 — 회원상태 직접 지정 또는 미접속 재활성화 신호), has_signal(회원 대상 신호
    존재), unsupported(미지원 조건 path 목록).
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
    # 장기 미접속(휴면 재활성화) 신호가 있으면 기본 상태필터(NORMAL 한정)를 해제한다 — "6개월 이상
    # 접속하지 않은 휴면 고객"처럼 미접속=휴면으로 읽는 요청에서 NORMAL 이 붙으면 SLEEP/WITHDRAW 를
    # 배제해 원문("휴면 고객")과 모순되고, 의미검증기가 이를 반전으로 오탐한다. forces_state 로 흡수한다.
    suppresses_default_state = False

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

    # 연령. 하·상한이 같으면(정확 연령 "30세인") >=N AND <=N 대신 = N 으로 방출한다 — 깔끔하고,
    # 의미검증 게이트가 '>=N AND <=N'을 '=N'과 다르다고 오탐하는 것도 원천 차단한다.
    age_min = target_user.get("age_min")
    age_max = target_user.get("age_max")
    if isinstance(age_min, int) and isinstance(age_max, int) and age_min == age_max:
        other_predicates.append(f"B.AGE = {age_min}"); has_signal = True
    else:
        if isinstance(age_min, int):
            other_predicates.append(f"B.AGE >= {age_min}"); has_signal = True
        if isinstance(age_max, int):
            other_predicates.append(f"B.AGE <= {age_max}"); has_signal = True
    # 닫힌 연령 구간 제외("20대가 아닌"). 여집합이 분리 2구간이라 NOT BETWEEN 으로 뺀다(널은 BETWEEN 이 이미 거름).
    for age_range in target_user.get("age_exclude_ranges", []):
        if isinstance(age_range, (list, tuple)) and len(age_range) == 2 and all(isinstance(v, int) for v in age_range):
            lo, hi = age_range
            other_predicates.append(f"NOT (B.AGE BETWEEN {lo} AND {hi})"); has_signal = True

    # lifecycle 포함(등가/활동)
    for lifecycle in target_user.get("lifecycle", []):
        if lifecycle == "new_user":
            continue  # 신규 가입은 아래 signup_target 분기가 REG_DT 창 술어로 처리(미지원 아님)
        if lifecycle in MEMBER_EQ_FILTERS:
            _add_include(lifecycle); labels.append(lifecycle); has_signal = True
        elif lifecycle in MEMBER_ACTIVITY_FILTERS:
            other_predicates.append(_member_activity_predicate(MEMBER_ACTIVITY_FILTERS[lifecycle])); labels.append(lifecycle); has_signal = True; suppresses_default_state = True
        else:
            unsupported.append("target_user.lifecycle:" + lifecycle)

    # lifecycle 제외(등가만 부정 가능; 활동 범위 부정은 모호해 미지원)
    for lifecycle in exclude.get("lifecycle", []):
        if lifecycle in MEMBER_EQ_FILTERS:
            other_predicates.append(_member_eq_predicate(lifecycle, negate=True)); labels.append("non_" + lifecycle); has_signal = True
        else:
            unsupported.append("exclude.lifecycle:" + lifecycle)

    # 미접속 기간(휴면/장기 미접속): LAST_LOGIN_DATE(YYYYMMDD 문자열) 사전식 비교 술어로 컴파일한다.
    # 미접속=휴면 재활성화 신호이므로 기본 상태필터(NORMAL 한정)를 해제한다(suppresses_default_state) —
    # "6개월 이상 접속하지 않은 휴면 고객"에 NORMAL 을 붙이면 SLEEP/WITHDRAW 를 배제해 원문과 모순되고
    # 의미검증기가 반전으로 오탐하기 때문. 오디언스는 LAST_LOGIN_DATE 창만으로 정의한다.
    inactivity_period = target_user.get("inactivity_period")
    if isinstance(inactivity_period, dict) and isinstance(inactivity_period.get("min_days"), int):
        other_predicates.append(_member_activity_predicate(inactivity_period["min_days"])); has_signal = True; suppresses_default_state = True

    # 최근 로그인 창(긍정형 접속): LAST_LOGIN_DATE >= (기준일-N일) 술어. 적재 데이터가 과거라 0명이
    # 나올 수 있어도, 조건 표현이 가능하면 요청 기간을 왜곡하지 않고 무조건 그대로 건다.
    recent_login = target_user.get("recent_login")
    if isinstance(recent_login, dict) and isinstance(recent_login.get("min_days"), int):
        other_predicates.append(_member_recent_login_predicate(recent_login["min_days"])); labels.append("recent_login"); has_signal = True

    # 생일 타겟(BIRTHDAY 월일 비교; '이달 생일'은 월 비교). 년도는 비교하지 않는다.
    birthday_target = target_user.get("birthday_target")
    if isinstance(birthday_target, dict):
        granularity = "month" if birthday_target.get("granularity") == "month" else "day"
        other_predicates.append(_member_birthday_predicate(granularity)); labels.append("birthday_" + granularity); has_signal = True

    # 잔액 임계값(적립금/예치금 N원 이상): 회원 테이블 잔액 컬럼 직접 비교. balance_conditions 는
    # _apply_balance_condition_filter 가 numeric_filters(balance) 설정 기준으로 뽑는다.
    for condition in target_user.get("balance_conditions", []):
        if not isinstance(condition, dict):
            continue
        column = condition.get("column")
        # NULL/0 구분 술어: '정보가 없는'(IS NULL, 0 과 구분)·'없거나 0원'(IS NULL OR = 0). 값이 0 인
        # 회원과 값 자체가 없는(미기입) 회원을 다른 대상으로 취급한다([[deterministic-filter-registry]]).
        null_mode = condition.get("null_mode")
        if isinstance(column, str) and column and null_mode in {"is_null", "null_or_zero"}:
            if null_mode == "is_null":
                other_predicates.append(f"B.{column} IS NULL")
            else:
                other_predicates.append(f"(B.{column} IS NULL OR B.{column} = 0)")
            labels.append(str(condition.get("label") or column)); has_signal = True
            continue
        operator = condition.get("operator")
        if not (isinstance(column, str) and column and operator in {"=", ">", ">=", "<", "<="}):
            continue
        threshold_expr = condition.get("threshold_expr")
        threshold = condition.get("threshold")
        if isinstance(threshold_expr, str) and threshold_expr:
            right = threshold_expr  # 컬럼 대 컬럼 비교('적립금 > 예치금')
        elif isinstance(threshold, (int, float)):
            right = _format_threshold(threshold)
        else:
            continue
        # 파생 비율 지표(하루 평균 = CNT/DAYS)는 좌변을 이미 조립된 식(column_expr)으로 쓴다.
        column_expr = condition.get("column_expr")
        if isinstance(column_expr, str) and column_expr:
            left = column_expr
        else:
            # zero_semantics(missing_as_zero): NULL 을 0 으로 봐야 '한 번도 …' 조건이 NULL 회원까지 포함한다.
            left = f"COALESCE(B.{column}, 0)" if condition.get("coalesce_zero") else f"B.{column}"
        other_predicates.append(f"{left} {operator} {right}")
        labels.append(str(condition.get("label") or column)); has_signal = True

    # 캠페인 반응(접촉 성공/오퍼·구매 반응/쿠폰 사용): 회원키 EXISTS 서브쿼리라 회원 컬럼 술어와 똑같이
    # AND 결합된다. 여기서 컴파일해야 어느 빌더를 타든 조건이 남는다 — 예전엔 전용 빌더만 이 조건을
    # 알아서, '발송 성공했지만 구매하지 않은'처럼 다른 트랙(무구매 anti-join)이 이기는 프롬프트에서
    # 캠페인 조건이 조용히 사라졌다.
    for response in target_user.get("campaign_responses", []):
        predicate = response.get("predicate") if isinstance(response, dict) else None
        if not predicate:
            continue
        other_predicates.append(
            _campaign_response_exists_predicate(
                str(predicate),
                negated=bool(response.get("negated")),
                source=response.get("source"),
            )
        )
        labels.append(str(response.get("canonical") or "campaign_response")); has_signal = True

    # 쿠폰 사용 '건수' 임계(≥2·>5·범위 등): 회원별 SUM(USE_CPN_CNT) HAVING 집계를 회원키 IN 서브쿼리로
    # 컴파일한다(사용 '여부'는 위 campaign_responses EXISTS 가 담당). 다른 회원 조건과 AND 결합된다.
    for threshold in target_user.get("coupon_usage_thresholds", []) or []:
        predicate = _coupon_usage_threshold_predicate(threshold) if isinstance(threshold, dict) else None
        if predicate:
            other_predicates.append(predicate)
            labels.append("coupon_usage_count"); has_signal = True

    # 장바구니 부재('장바구니 없는/생성 안 한'): 보관(KEEP_YN='Y') 카트 라인이 없는 회원. 회원키
    # NOT EXISTS 라 캠페인 반응과 같이 어느 빌더에나 AND 결합된다. 구매 부재(no_purchase)와 함께 오면
    # ("장바구니나 구매 이력 없는") 각각 NOT EXISTS/anti-join 으로 둘 다 남는다.
    if target_user.get("cart_absence"):
        other_predicates.append(_cart_absence_predicate())
        labels.append("cart_absence"); has_signal = True

    # 장바구니 수량 미입력('수량이 입력되지 않은'): 담은 수량(QTY)이 NULL 인 카트 라인이 있는 회원.
    # '수량 0'(=0)이 아니라 값 자체가 미기입(NULL) — 회원키 EXISTS 라 어느 빌더에나 AND 결합된다.
    if target_user.get("cart_quantity_missing"):
        other_predicates.append(_cart_quantity_missing_predicate())
        labels.append("cart_quantity_missing"); has_signal = True

    # 구매 이력 존재(선택적으로 최근 N일 창). 단순 "구매한 회원"도 주문 근거 없이 회원 테이블 전체로
    # 축약되지 않도록 반드시 주문 헤더 EXISTS로 컴파일한다.
    purchase_membership = target_user.get("purchase_membership")
    if (
        isinstance(purchase_membership, dict)
        and purchase_membership.get("operator") == "exists"
        and purchase_membership.get("satisfied_by") != "aggregate_conditions"
    ):
        other_predicates.append(_purchase_membership_predicate(purchase_membership.get("window_days")))
        labels.append("purchase_exists"); has_signal = True

    # 구매 미발생 기간('최근 N일 미구매'): 회원키 NOT EXISTS anti-join 이라 cart_absence/캠페인 반응처럼
    # 어느 빌더에나 AND 결합된다. 여기서 방출해야 '장바구니 보유 + 최근 90일 미구매'처럼 다른 팩트 빌더
    # (카트)가 이기는 조합에서도 미구매 조건이 살아남는다. order_count 빌더와 동일 문자열이라 dedup 됨.
    purchase_inactivity = target_user.get("purchase_inactivity")
    if isinstance(purchase_inactivity, dict) and isinstance(purchase_inactivity.get("min_days"), int):
        other_predicates.append(_purchase_inactivity_predicate(purchase_inactivity["min_days"]))
        labels.append(f"purchase_inactive_{purchase_inactivity['min_days']}d"); has_signal = True

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
        if table_name != _member_table() and not join_column:
            continue
        column_short = (dimension_filter.get("column") or "").split(".")[-1]
        codes = [code for code in dimension_filter.get("codes", []) if isinstance(code, str) and code]
        if not column_short or not codes:
            continue
        if table_name == _member_table() and column_short in _member_region_short_columns():
            member_region_codes.setdefault(column_short, [])
            member_region_codes[column_short].extend(code for code in codes if code not in member_region_codes[column_short])
        else:
            in_list = ", ".join(_sql_quote(code) for code in codes)
            if table_name == _member_table():
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
        "forces_state": ("state" in include_categories) or suppresses_default_state,
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
    if not compiled["has_signal"] and not isinstance(density, dict) and query_plan.get("member_scope") != "all":
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
    state_predicate = (
        None
        if compiled["forces_state"] or query_plan.get("member_scope") == "all"
        else _member_active_state_predicate()
    )
    where_clauses = list(compiled["predicates"])
    if state_predicate is not None:
        where_clauses.append(state_predicate)
    where_clauses = _unique_strings(where_clauses)

    select_columns = [_member_key_select(), _member_grade_select()]
    # 세그먼트 라벨 — 다운스트림 태그이자 조건 커버리지(값 문자열 매칭) 충족용. 제외는 'non_<canonical>'.
    if compiled["labels"]:
        select_columns.append(_sql_quote(",".join(compiled["labels"])) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        # 캠페인 목적은 타겟 필터가 아니라 메시지 목적 라벨(조건 커버리지 충족 겸용).
        select_columns.append(_sql_quote(objective) + " AS objective")

    # 파이프라인: 조건 → SelectAst(AST) → Validation → SQL 렌더. 단순 질의의 직행 경로.
    ast = SelectAst(distinct=True, columns=select_columns, from_lines=[_member_from_clause()], where=list(where_clauses))
    candidate = _select_ast_candidate("sql_template:member_targets", "회원 속성 타겟 추출 SQL 템플릿(CRMDW)", 1.0, ast, "sql_template")
    # 실DB 미지원이라 SQL 에서 뺀 조건(부분 추출). 커버리지 검증에서 제외하고 응답에 고지한다.
    candidate["dropped_conditions"] = compiled["unsupported"]
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in compiled["unsupported"]]
    # 술어별 카디널리티 진단용 메타. 실행 결과가 0명일 때, from_clause + 각 술어를 독립 COUNT 로
    # 돌려 어느 AND 술어가 오디언스를 죽였는지 귀속한다(과잉 조건 탐지). injected_default 는
    # 사용자가 명시하지 않았지만 주입된 기본 게이트(정상회원 한정)를 가리킨다.
    candidate["cardinality_probe"] = {
        "from_clause": f"{_member_table()} {_member_alias()}",
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
    inner_from = ["    " + _member_from_clause()]
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
        "DISTINCT " + _member_key_select("M"),
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
            _member_from_clause("M"),
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

    # 개수(TOP N) vs 퍼센트(TOP N PERCENT) 랭킹. 퍼센트는 (0,100) 밖이면 후보 없음(파서 게이트가 이미
    # 걸러내지만 방어적으로 재확인). T-SQL 의 TOP N PERCENT 는 결과 행수를 올림(ceil)해 1 명 이상을 보장한다.
    if ranking.get("limit_type") == "percent":
        pct = ranking.get("percent")
        if not isinstance(pct, (int, float)) or not 0 < pct < 100:
            return None
        top_clause = f"TOP {pct:g} PERCENT "
    else:
        top_n = int(ranking.get("top_n", 100))
        top_clause = f"TOP {top_n} "

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
        f"DISTINCT {top_clause}" + _member_key_select(),
        _member_grade_select(),
        f"{metric_expr} AS {metric['metric_id']}",
    ]
    segment_parts = [ranking["metric_id"], *compiled["labels"]]
    select_columns.append(_sql_quote("metric_rank:" + ",".join(segment_parts)) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    # 방향: 상위/높은 순 → DESC, 하위/낮은 순 → ASC. 표식 없으면 기존 동작(내림차순) 유지.
    order_direction = "ASC" if ranking.get("direction") == "low" else "DESC"
    sql = "\n".join(
        [
            "SELECT " + ", ".join(select_columns),
            _member_from_clause(),
            f"     INNER JOIN {value_table} C ON B.{join_column} = C.{join_column}",
            "WHERE " + "\n  AND ".join(where_clauses),
            f"ORDER BY {metric_expr} {order_direction}",
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


def build_group_ranking_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """'<축>별(로) <지표> 높은 회원 N명씩'을 그룹별 회원 Top-N SQL 로 생성한다(그룹마다 상위 N 명씩).

    전역 회원 랭킹(build_member_metric_ranking_sql_candidate)의 그룹 버전이다. 축(지역/성별/연령대)은
    _resolve_group_axis 로 SQL 그룹식(B.SIGUNGU / B.GENDER_CD / 연령대 CASE)만 주입받는 **공통 윈도
    빌더**다 — 축별 SQL 생성기를 복제하지 않는다. base CTE 가 group_key(그룹식)와 지표를 계산하고,
    ranked CTE 가 ROW_NUMBER() OVER(PARTITION BY group_key ORDER BY 지표)로 그룹 내 순위를 매기면,
    바깥에서 row_num <= N 으로 그룹당 N 명씩만 남긴다(전역 TOP N 이 아니라 그룹별 TOP N). 그룹 표시
    컬럼과 그룹 내 순위(row_num)를 결과에 유지하고, 성별/연령/등급 등 다른 회원 속성 조건은
    compile_member_target_conditions 로 base CTE WHERE 에 AND 결합한다. NULL/미분류 그룹은 축 정책
    (null_predicates)으로 제외한다."""
    group = query_plan.get("group_ranking_target")
    if not isinstance(group, dict):
        return None
    registry = _load_member_metrics(str(DEFAULT_MEMBER_METRICS_PATH)) or {}
    metric = next((m for m in registry.get("metrics", []) if m.get("metric_id") == group.get("metric_id")), None)
    if not metric:
        return None
    axis_spec = _resolve_group_axis(group.get("group_axis", "region"), group.get("granularity"))
    if axis_spec is None:
        return None
    metric_id = metric["metric_id"]
    metric_expr = f"C.{metric['column']}"
    value_table = registry.get("value_table", "CRM_MB_MONTHCRMINFO")
    join_column = registry.get("join_column", "MEMBER_NO")
    top_n = int(group.get("top_n", 10))
    order_direction = "ASC" if group.get("direction") == "low" else "DESC"

    compiled = compile_member_target_conditions(query_plan)
    base_where = list(compiled["predicates"])
    if not compiled["forces_state"]:
        base_where.append(_member_active_state_predicate())
    grain_filter = registry.get("grain_filter")
    if grain_filter:
        base_where.append(grain_filter)
    base_where.append(f"{metric_expr} IS NOT NULL")
    base_where.extend(axis_spec.null_predicates)  # 축 NULL 정책(예: B.GENDER_CD IS NOT NULL, B.AGE IS NOT NULL)
    base_where = _unique_strings(base_where)

    # base CTE: group_key(그룹식)와 지표를 계산한다 — 그룹식(연령대 CASE 등)을 한 번만 쓰고 이후 alias 로 참조.
    base_select = [
        _member_key_select(),
        _member_grade_select(),
        f"{axis_spec.group_expr} AS group_key",
        f"{metric_expr} AS {metric_id}",
    ]
    base_cte = "\n".join(
        [
            "    SELECT " + ", ".join(base_select),
            "    " + _member_from_clause(),
            f"         INNER JOIN {value_table} C ON B.{join_column} = C.{join_column}",
            "    WHERE " + "\n      AND ".join(base_where),
        ]
    )
    # ranked CTE: 그룹(group_key) 내 지표 순위. 방향 상위=DESC / 하위=ASC.
    ranked_cte = "\n".join(
        [
            f"    SELECT base.CUST_ID, base.member_grade, base.group_key, base.{metric_id},",
            f"           ROW_NUMBER() OVER (PARTITION BY base.group_key ORDER BY base.{metric_id} {order_direction}) AS row_num",
            "    FROM base",
        ]
    )

    segment_parts = [metric_id, group.get("group_axis", "region"), *compiled["labels"]]
    outer_select = [
        "ranked.CUST_ID",
        "ranked.member_grade",
        f"ranked.group_key AS {axis_spec.select_alias}",
        f"ranked.{metric_id}",
        "ranked.row_num",
        _sql_quote("group_metric_rank:" + ",".join(segment_parts)) + " AS segment_label",
    ]
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        outer_select.append(_sql_quote(objective) + " AS objective")

    sql = "\n".join(
        [
            "WITH base AS (",
            base_cte,
            "),",
            "ranked AS (",
            ranked_cte,
            ")",
            "SELECT " + ", ".join(outer_select),
            "FROM ranked",
            f"WHERE ranked.row_num <= {top_n}",
            "ORDER BY ranked.group_key, ranked.row_num",
        ]
    )
    candidate = _sql_candidate(
        "sql_template:group_ranking",
        f"그룹별 회원 Top-N({axis_spec.label}별 {group.get('metric_label', metric_id)}) 타겟 추출 SQL 템플릿(CRMDW)",
        1.0,
        sql,
        _template_tables(sql),
        "sql_template",
    )
    candidate["dropped_conditions"] = compiled["unsupported"]
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in compiled["unsupported"]]
    return candidate


def build_region_member_count_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """'회원 수가 많은 시군구 상위 N개'를 지역 단위 회원 수 랭킹 SQL 로 생성한다(지역명 + 회원수 반환).

    지역 컬럼으로 GROUP BY 해 COUNT(DISTINCT 회원키)로 회원 수를 집계하고, 순위 요청이 있으면 정렬
    (많은=DESC/적은=ASC)·상위 N(TOP)을 적용한다. 밀집 지역 빌더(거주 회원 추출)와 달리 출력 행이
    지역이라 회원 추출 SELECT 가 아니다. 코호트(성별/연령 등) 조건이 있으면 집계 모집단을 그만큼 좁힌다."""
    target = query_plan.get("region_member_count_target")
    if not isinstance(target, dict):
        return None
    column = target.get("column") or "SIGUNGU"
    group_expr = f"B.{column}"
    key_column = _member_key_column()
    count_expr = f"COUNT(DISTINCT B.{key_column})"
    top_n = target.get("top_n")
    order_direction = "ASC" if target.get("direction") == "low" else "DESC"

    compiled = compile_member_target_conditions(query_plan)
    where_clauses = list(compiled["predicates"])
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())
    where_clauses.append(f"{group_expr} IS NOT NULL")
    where_clauses.append(f"{group_expr} <> ''")
    where_clauses = _unique_strings(where_clauses)

    top_clause = f"TOP {int(top_n)} " if isinstance(top_n, int) and top_n > 0 else ""
    select_columns = [
        f"{top_clause}{group_expr} AS target_region",
        f"{count_expr} AS member_count",
    ]
    sql = "\n".join(
        [
            "SELECT " + ", ".join(select_columns),
            _member_from_clause(),
            "WHERE " + "\n  AND ".join(where_clauses),
            f"GROUP BY {group_expr}",
            f"ORDER BY {count_expr} {order_direction}",
        ]
    )
    candidate = _sql_candidate(
        "sql_template:region_member_count",
        f"지역 단위 회원 수 랭킹({target.get('granularity', '지역')}별 회원 수) 추출 SQL 템플릿(CRMDW)",
        1.0,
        sql,
        _template_tables(sql),
        "sql_template",
    )
    candidate["dropped_conditions"] = compiled["unsupported"]
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in compiled["unsupported"]]
    return candidate


def build_member_column_selection_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """회원 기준 테이블 컬럼(잔액 등)의 선택 전략을 SQL 로 생성한다 — 상위 N 명/상위 N%/평균 대비.

    임계값(WHERE)으로 표현 못 하는 랭킹·퍼센타일·평균 비교를 정렬(TOP/PERCENT)·서브쿼리로 뽑는다.
    잔액은 회원 기준 테이블(CRM_MB_BASEINFO) 컬럼이라 조인이 없다(지표 랭킹의 단일 테이블 짝).
    성별/연령/등급 등 회원 속성은 compile_member_target_conditions 로 같은 SQL 에 AND 결합한다."""
    selection = query_plan.get("member_metric_selection")
    if not isinstance(selection, dict):
        return None
    column = selection.get("column")
    mode = selection.get("mode")
    if not isinstance(column, str) or not column or mode not in {"top_n", "top_percent", "vs_average"}:
        return None
    alias = _member_alias()
    expr = f"{alias}.{column}"
    order_dir = "ASC" if selection.get("direction") == "low" else "DESC"

    compiled = compile_member_target_conditions(query_plan)
    where_clauses = list(compiled["predicates"])
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())
    where_clauses.append(f"{expr} IS NOT NULL")

    top_clause = ""
    order_by: str | None = None
    if mode == "vs_average":
        op = selection.get("average_op") if selection.get("average_op") in {">", "<", ">=", "<="} else ">"
        # 비상관 서브쿼리라 별칭 없이 맨 컬럼명으로 쓴다(별칭 허용목록 결합 회피). 평균 모집단은
        # 정상 회원 전체 — 성별/연령 등 코호트 조건은 바깥 WHERE 에만 걸어 '전체 평균 대비'로 읽는다.
        state = _MEMBER_TARGET_FILTERS.get("active_state")
        if not isinstance(state, dict):
            state = _DEFAULT_MEMBER_TARGET_FILTERS["active_state"]
        state_col = state.get("column") or "MEMBER_STATE_CD"
        state_val = state.get("value") or "MEMBER_STATE_CD.NORMAL"
        avg_sub = (
            f"(SELECT AVG({column}) FROM {_member_table()} "
            f"WHERE {state_col} = {_sql_quote(state_val)} AND {column} IS NOT NULL)"
        )
        where_clauses.append(f"{expr} {op} {avg_sub}")
    elif mode == "top_percent":
        pct = selection.get("percent")
        if not isinstance(pct, (int, float)) or not 0 < pct < 100:
            return None
        top_clause = f"TOP {pct:g} PERCENT "
        order_by = f"ORDER BY {expr} {order_dir}"
    else:  # top_n
        n = selection.get("n")
        if not isinstance(n, int) or n <= 0:
            return None
        top_clause = f"TOP {n} "
        order_by = f"ORDER BY {expr} {order_dir}"

    where_clauses = _unique_strings(where_clauses)
    select_columns = [
        f"DISTINCT {top_clause}" + _member_key_select(),
        _member_grade_select(),
        f"{expr} AS {column}",
    ]
    segment_parts = [f"balance_{mode}", *compiled["labels"]]
    select_columns.append(_sql_quote("member_selection:" + ",".join(segment_parts)) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    lines = ["SELECT " + ", ".join(select_columns), _member_from_clause(), "WHERE " + "\n  AND ".join(where_clauses)]
    if order_by:
        lines.append(order_by)
    sql = "\n".join(lines)
    candidate = _sql_candidate(
        "sql_template:member_metric_selection",
        f"회원 컬럼 선택({mode}, {selection.get('label', column)}) 타겟 추출 SQL 템플릿(CRMDW)",
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


def _purchase_product_registry() -> dict[str, Any]:
    config = _MEMBER_TARGET_FILTERS.get("purchase_product_target")
    return config if isinstance(config, dict) else {}


def _order_detail_member_join_lines(alias: str = "D", product_alias: str | None = None) -> list[str]:
    """주문상세(→상품)→회원 FROM/JOIN 절 — 테이블·조인키는 purchase_product_target 레지스트리 소유,
    별칭만 호출자 관례. 회원 조인은 주문상세의 회원키 = 회원 기준 테이블 회원키(base_entity)."""
    config = _purchase_product_registry()
    detail = config.get("order_detail") if isinstance(config.get("order_detail"), dict) else {}
    product = config.get("product") if isinstance(config.get("product"), dict) else {}
    detail_table = detail.get("table", "CRM_SL_ORDERDETAILMALL")
    product_table = product.get("table", "CRM_CM_PRODUCT")
    # 상품 조인키: 'P.PRODUCT_ID = OD.PRODUCT_ID' 선언에서 짧은 컬럼명만 취한다(별칭은 호출자 소유).
    product_join = str(product.get("join") or "P.PRODUCT_ID = OD.PRODUCT_ID")
    product_key = product_join.split("=")[0].strip().split(".")[-1] or "PRODUCT_ID"
    lines = [f"FROM {detail_table} {alias}"]
    if product_alias:
        lines.append(f"     INNER JOIN {product_table} {product_alias} ON {alias}.{product_key} = {product_alias}.{product_key}")
    member_key = _member_key_column()
    lines.append(f"     INNER JOIN {_member_table()} {_member_alias()} ON {alias}.{member_key} = {_member_alias()}.{member_key}")
    return lines


def _sql_nlike_contains(column: str, term: str) -> str:
    """유니코드 부분일치 LIKE 술어(N'%term%'). term 은 _sanitize_purchase_object 로 정제돼 홑따옴표가 없으나
    방어적으로 이스케이프한다. N 접두어는 tsql/mysql 모두 유효해 한글 리터럴을 안전하게 비교한다."""
    return f"{column} LIKE N'%{term.replace(chr(39), chr(39) * 2)}%'"


def _purchase_date_predicate(purchase_date: Any, alias: str | None = "D", column: str = "ORDER_DATE") -> str | None:
    """구매 날짜 창 {from,to}(YYYYMMDD CHAR8)를 ORDER_DATE BETWEEN 술어로 만든다.

    ORDER_DATE 는 CHAR(8) 'YYYYMMDD' 로 저장되므로 문자열 BETWEEN 이 곧 날짜 범위다(집계 빌더의
    CONVERT(CHAR(8), …, 112) 비교와 같은 표현계). alias=None 이면 컬럼을 별칭 없이 쓴다(집계 서브쿼리처럼
    단일 테이블 스캔이라 별칭이 없는 문맥용). 값이 없거나 형식이 어긋나면 None."""
    if not isinstance(purchase_date, dict):
        return None
    start, end = purchase_date.get("from"), purchase_date.get("to")
    if not (isinstance(start, str) and isinstance(end, str) and re.fullmatch(r"\d{8}", start) and re.fullmatch(r"\d{8}", end)):
        return None
    if start > end:
        start, end = end, start
    prefix = f"{alias}." if alias else ""
    return f"{prefix}{column} BETWEEN {_sql_quote(start)} AND {_sql_quote(end)}"


# 상품 스코프로 쓰기엔 너무 일반적인 상품 지시어(구체적 상품/브랜드가 아님). 이런 값이 상품 스코프
# LIKE 로 새면 '%상품%' 처럼 상품명에 그 글자가 든 상품만 걸려 집계가 왜곡된다 — 집계 상품 스코프에서 뺀다.
# ('동일 상품' grain 이 이 단어를 purchase_object 로 흘리는 비결정 추출과 무관하게 방어.)
_GENERIC_PRODUCT_OBJECT_WORDS = frozenset({"상품", "제품", "물건", "물품", "품목", "것", "상품들", "제품들"})


def _target_purchase_objects(target_user: dict[str, Any]) -> list[dict[str, Any]]:
    """구매 상품 조건을 상품별 [{value, kind}] 리스트로 정규화한다(빌더 공용 단일 소스).

    나열형 다중 상품은 target_user['purchase_objects'] 에, 단일 상품은 target_user['purchase_object'] 에 담기므로
    둘 중 있는 쪽을 리스트로 통일한다. 일반 지시어('상품/제품')는 스코프로 쓸 수 없어 제외한다."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    objects = target_user.get("purchase_objects")
    if isinstance(objects, list) and objects:
        source = [
            {"value": o["value"], "kind": o.get("kind")}
            for o in objects
            if isinstance(o, dict) and isinstance(o.get("value"), str) and o["value"].strip()
        ]
    else:
        value = target_user.get("purchase_object")
        source = (
            [{"value": value, "kind": target_user.get("purchase_object_kind")}]
            if isinstance(value, str) and value.strip()
            else []
        )
    for item in source:
        value = item["value"].strip()
        if value in _GENERIC_PRODUCT_OBJECT_WORDS or value in seen:
            continue
        seen.add(value)
        result.append({"value": value, "kind": item.get("kind")})
    return result


def _purchase_object_match_predicate(purchase_object: str, object_kind: Any = None, alias: str = "P") -> str:
    """상품 자유텍스트를 상품 마스터(<alias>.*) 부분일치(OR)로 컴파일한다. object_kind 가 brand/product 면
    해당 컬럼(BRAND_NAME/PRODUCT_NAME)만 좁혀 매칭해 카테고리 등 다른 컬럼의 우연 일치를 막고, 애매하면
    광역 6컬럼 LIKE 를 유지한다. purchase_history 빌더와 집계 빌더의 상품 스코프가 같은 술어를 쓰게 한다."""
    kind_column = {"brand": "BRAND_NAME", "product": "PRODUCT_NAME"}.get(object_kind)
    if kind_column:
        columns = tuple(
            column for column in _PURCHASE_PRODUCT_MATCH_COLUMNS if column.rsplit(".", 1)[-1] == kind_column
        ) or _PURCHASE_PRODUCT_MATCH_COLUMNS
    else:
        columns = _PURCHASE_PRODUCT_MATCH_COLUMNS
    return "(" + " OR ".join(_sql_nlike_contains(f"{alias}.{column}", purchase_object) for column in columns) + ")"


def _valid_aggregate_conditions(target_user: dict[str, Any]) -> list[dict[str, Any]]:
    """aggregate_targets 레지스트리가 실제로 컴파일할 수 있는(지표 등록 + 연산자 + 임계값) 집계 조건만
    추린다. purchase_history 빌더의 '집계 빌더에 양보' 판정과 집계 빌더의 valid 판정이 같은 규칙을 쓰게
    단일화한다 — 그래야 양보한 조건을 집계 빌더가 반드시 소유한다(누구도 안 잡는 간극 방지)."""
    metrics = _aggregate_targets_config().get("metrics", {})
    return [
        condition
        for condition in (target_user.get("aggregate_conditions") or [])
        if isinstance(condition, dict)
        and isinstance(metrics.get(condition.get("metric_id")), dict)
        and condition.get("operator") in {"=", ">", ">=", "<", "<="}
        and isinstance(condition.get("threshold"), (int, float))
    ]


def _product_presence_member_subquery(product: dict[str, Any], purchase_date: Any, alias: str) -> str:
    """특정 상품을 (기간 내) 구매한 회원 id 집합 서브쿼리. 나열형 다중 상품을 상품별로 나눠 INNER JOIN(AND)
    하려는 용도 — 한 주문상세행은 상품 하나라 두 상품 LIKE 를 같은 행에 AND 하면 공집합이 되기 때문이다."""
    where = ["D.MEMBER_NO IS NOT NULL", _purchase_object_match_predicate(product["value"], product.get("kind"), "P")]
    date_between = _purchase_date_predicate(purchase_date, alias="D")
    if date_between is not None:
        where.append(date_between)
    return "\n".join(
        [
            "(",
            "    SELECT D.MEMBER_NO",
            f"    FROM {_PRODUCT_SCOPE_TABLE} D",
            "         INNER JOIN CRM_CM_PRODUCT P ON D.PRODUCT_ID = P.PRODUCT_ID",
            f"    WHERE {' AND '.join(where)}",
            "    GROUP BY D.MEMBER_NO",
            f") {alias}",
        ]
    )


def build_purchase_history_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """실주문 상세(CRM_SL_ORDERDETAILMALL) → 상품(CRM_CM_PRODUCT) → 회원(CRM_MB_BASEINFO) 조인으로
    특정 상품/카테고리를 구매한 회원을 추출한다.

    CRM_MB_BASEINFO 단독으로는 표현 못 하는 "상품 구매 이력"을 실주문 테이블 조인으로 해결한다.
    성별/연령/등급/휴면 등 회원 속성은 compile_member_target_conditions 로 그대로 재사용해 같은 SQL 에
    AND 결합하므로, "40대 여성 중 기저귀 구매자" 같은 조합도 하나의 추출 SQL 이 된다.
    """
    target_user = query_plan.get("target_user", {})
    product_objects = _target_purchase_objects(target_user)
    has_object = bool(product_objects)
    purchase_date = target_user.get("purchase_date")
    date_predicate = _purchase_date_predicate(purchase_date)
    # 상품 구매 이력 조건(상품 LIKE)도, 구매 날짜 창 조건(ORDER_DATE BETWEEN)도 없으면 이 빌더 대상이 아니다.
    if not has_object and date_predicate is None:
        return None
    # 개수/금액 임계값(aggregate_conditions)이 함께 잡혔으면 집계 빌더에 양보한다 — 이 빌더는 상품·날짜창만
    # 걸 수 있어 '기저귀를 2개 이상 구매'의 개수 임계값(HAVING)이 조용히 새기 때문이다. 집계 빌더는 상품
    # (purchase_object)을 상품 스코프 LIKE 로, 절대 구매창을 서브쿼리 안에서 함께 걸어 개수 임계값까지 상품
    # 범위로 컴파일한다(build_aggregate_targets_sql_candidate). 지표/연산자/임계값이 유효한 조건이 있을
    # 때만 양보한다 — 집계 빌더가 반드시 소유하도록(양보 후 누구도 안 잡는 간극 방지).
    if _valid_aggregate_conditions(target_user):
        return None

    compiled = compile_member_target_conditions(query_plan)

    select_columns = ["DISTINCT " + _member_key_select(), _member_grade_select()]
    if compiled["labels"]:
        select_columns.append(_sql_quote(",".join(compiled["labels"])) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    member_where = list(compiled["predicates"])
    # 회원상태를 직접 지정한 타겟(휴면 등)이 아니면 정상 회원으로 한정한다(탈퇴/휴면 제외).
    if not compiled["forces_state"]:
        member_where.append(_member_active_state_predicate())

    if len(product_objects) >= 2:
        # 나열형 다중 상품('기저귀와 건강식품 구매') → 상품별 회원 집합 서브쿼리를 회원 기준(B)에 INNER JOIN(AND).
        # 한 상세행은 상품 하나라 두 상품 LIKE 를 같은 행에 AND 하면 공집합이 되므로 회원 단위로 나눠 결합한다.
        from_lines = [_member_from_clause()]
        member_key = _member_key_column()
        for idx, product in enumerate(product_objects):
            alias = f"OBJ{idx}"
            subquery = _product_presence_member_subquery(product, purchase_date, alias)
            from_lines.append(f"     INNER JOIN {subquery} ON B.{member_key} = {alias}.{member_key}")
        where_clauses = _unique_strings(member_where)
        sql_lines = ["SELECT " + ", ".join(select_columns), *from_lines]
        if where_clauses:
            sql_lines.append("WHERE " + "\n  AND ".join(where_clauses))
        sql = "\n".join(sql_lines)
    else:
        where_clauses: list[str] = []
        if has_object:
            # 사용자가 '브랜드'/'상품명'을 명시했거나 값이 실DB 브랜드명으로 확정된 경우, 광역 6컬럼 LIKE 대신
            # 해당 컬럼(BRAND_NAME/PRODUCT_NAME)만 매칭한다(다른 컬럼 우연 일치 방지). 애매하면 광역 6컬럼 LIKE.
            where_clauses.append(
                _purchase_object_match_predicate(product_objects[0]["value"], product_objects[0].get("kind"), "P")
            )
        if date_predicate is not None:
            where_clauses.append(date_predicate)
        where_clauses.extend(member_where)
        where_clauses = _unique_strings(where_clauses)
        sql = "\n".join(
            [
                "SELECT " + ", ".join(select_columns),
                *_order_detail_member_join_lines("D", product_alias="P"),
                "WHERE " + "\n  AND ".join(where_clauses),
            ]
        )
    candidate = _sql_candidate(
        "sql_template:purchase_history_targets", "상품 구매 이력 타겟 추출 SQL 템플릿(CRMDW)", 1.0, sql, _template_tables(sql), "sql_template"
    )
    # purchase_object(상품 LIKE)·purchase_date(ORDER_DATE 창)는 이 템플릿이 실제로 커버하므로 dropped(미고지)에서
    # 제외한다. 회원 속성 외 다른 미지원 조건(관심사 등)이 있으면 그것만 부분추출 고지 대상으로 남긴다.
    _covered = {"target_user.purchase_object", "target_user.purchase_objects", "target_user.purchase_date"}
    dropped = [path for path in compiled["unsupported"] if path not in _covered]
    candidate["dropped_conditions"] = dropped
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in dropped]
    return candidate


def build_purchase_count_ranking_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """'(기간 내) 많이/자주 구입한 사람 상위 N 명'을 실주문 집계 랭킹 SQL 로 생성한다(구매 건수 내림차순 상위 N).

    member_metric_ranking(월 스냅샷 CRM_MB_MONTHCRMINFO 지표 랭킹)은 '전 기간 누적' 기준이라 '2019년
    2월에 많이 산 사람' 같은 절대 기간 랭킹을 표현 못 하는 간극을 메운다. 상품 구매 이력 빌더
    (build_purchase_history_targets_sql_candidate)와 같은 주문 상세(CRM_SL_ORDERDETAILMALL)→회원
    (CRM_MB_BASEINFO) 조인을 쓰되, 회원별로 GROUP BY 해 COUNT(*) 내림차순 상위 N 명을 뽑는다. 구매 날짜
    창(purchase_date)이 있으면 그 기간 주문만, 특정 상품(purchase_object)이 있으면 그 상품 주문만 센다.
    성별/연령/등급/지역 등 회원 속성은 compile_member_target_conditions 로 같은 SQL 에 AND 결합한다."""
    ranking = query_plan.get("purchase_count_ranking")
    if not isinstance(ranking, dict):
        return None
    top_n = int(ranking.get("top_n") or 100)
    target_user = query_plan.get("target_user", {})
    purchase_object = target_user.get("purchase_object")
    has_object = isinstance(purchase_object, str) and bool(purchase_object)
    date_predicate = _purchase_date_predicate(target_user.get("purchase_date"))

    compiled = compile_member_target_conditions(query_plan)
    where_clauses: list[str] = []
    if has_object:
        where_clauses.append(
            "(" + " OR ".join(
                _sql_nlike_contains("P." + column, purchase_object) for column in _PURCHASE_PRODUCT_MATCH_COLUMNS
            ) + ")"
        )
    if date_predicate is not None:
        where_clauses.append(date_predicate)
    where_clauses.extend(compiled["predicates"])
    # 회원상태를 직접 지정한 타겟(휴면 등)이 아니면 정상 회원으로 한정한다(탈퇴/휴면 제외).
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())
    where_clauses = _unique_strings(where_clauses)

    select_columns = [
        f"TOP {top_n} " + _member_key_select(),
        _member_grade_select(),
        "COUNT(*) AS purchase_count",
    ]
    segment_label = "purchase_count_rank"
    if compiled["labels"]:
        segment_label += ":" + ",".join(compiled["labels"])
    select_columns.append(_sql_quote(segment_label) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    from_lines = _order_detail_member_join_lines("D", product_alias="P" if has_object else None)

    sql_lines = ["SELECT " + ", ".join(select_columns), *from_lines]
    if where_clauses:
        sql_lines.append("WHERE " + "\n  AND ".join(where_clauses))
    sql_lines.append(f"GROUP BY {_member_alias()}.{_member_key_column()}, {_member_grade_column()}")
    sql_lines.append("ORDER BY COUNT(*) DESC")
    sql = "\n".join(sql_lines)

    candidate = _sql_candidate(
        "sql_template:purchase_count_ranking",
        f"구매 건수 상위 N({top_n}) 회원 랭킹 타겟 추출 SQL 템플릿(CRMDW)",
        1.0,
        sql,
        _template_tables(sql),
        "sql_template",
    )
    # 구매 건수 랭킹(랭킹 신호)·구매 날짜 창·상품 조건은 이 템플릿이 커버하므로 dropped 에서 뺀다.
    _covered = {"target_user.purchase_object", "target_user.purchase_objects", "target_user.purchase_date"}
    dropped = [path for path in compiled["unsupported"] if path not in _covered]
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


def _render_aggregate_expression(expression: str, alias_prefix: str) -> str | None:
    """집계식 템플릿의 alias 자리표시자 `{t}` 를 서브쿼리 실제 접두어(예: '' 또는 'OH.')로 치환한다.

    임의 문자열 치환이 아니라 정의된 자리표시자만 채우는 '허용 템플릿' 방식이다. 치환 후 집계식이 안전한
    토큰(대문자 식별자·숫자·산술·괄호·허용 집계함수)만 담는지 검증하고, 아니면 None(무효)을 돌려 SUM(None)
    류가 빌드로 새는 것을 원천 차단한다."""
    if not isinstance(expression, str) or not expression.strip():
        return None
    rendered = expression.replace("{t}", alias_prefix)
    # 허용 토큰만: 식별자(대소문자/숫자/_)·점(별칭)·숫자·산술/비교·콤마·괄호·공백. 리터럴 None/미치환 자리표시자 배제.
    if "{" in rendered or "}" in rendered or re.search(r"\bNone\b", rendered):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.,()\s*/+\-]+", rendered):
        return None
    # alias-less 서브쿼리(alias_prefix='')인데 렌더 결과에 'alias.column' 한정자가 남아 있으면, {t} 로 접두어를
    # 맞추지 않고 리터럴 별칭을 박은 설정 오류다 — 서브쿼리에 없는 별칭이라 실행 시 실패한다(존재하지 않는 별칭 차단).
    if alias_prefix == "" and re.search(r"[A-Za-z_]\w*\.[A-Za-z_]\w*", rendered):
        return None
    return rendered


def _member_summary_threshold_subquery(
    summary: dict[str, Any], operator: str, threshold: int | float, alias: str,
) -> str | None:
    """회원 요약(사전 계산) 컬럼 임계 서브쿼리 — 예: CRM_MB_MONTHCRMINFO.MEAN_BUY_AMT >= N.

    스냅샷 컬럼이라 회원당 1행(grain_filter 로 최신 월 한정)이며 GROUP BY/HAVING 없이 WHERE 임계로 뽑는다.
    기간창을 반영할 수 없으므로 호출부가 window/purchase_date 가 없을 때만 이 경로를 쓴다."""
    table = summary.get("table")
    column = summary.get("column")
    join_column = summary.get("join_column", "MEMBER_NO")
    if not (isinstance(table, str) and table and isinstance(column, str) and column):
        return None
    where = [f"{join_column} IS NOT NULL"]
    grain_filter = summary.get("grain_filter")
    if isinstance(grain_filter, str) and grain_filter.strip():
        where.append(grain_filter)
    where.append(f"{column} IS NOT NULL")
    where.append(f"{column} {operator} {_format_threshold(threshold)}")
    return "\n".join(
        [
            "(",
            f"    SELECT {join_column}",
            f"    FROM {table}",
            f"    WHERE {' AND '.join(where)}",
            f") {alias}",
        ]
    )


_PRODUCT_SCOPE_TABLE = "CRM_SL_ORDERDETAILMALL"  # 상품 단위 컬럼(PRODUCT_ID/ORDER_QTY/PAYMENT_AMT/DC_AMT) 보유
_AGG_GRAIN_COLUMN = {"per_order": "ORDER_ID", "per_product": "PRODUCT_ID"}


def _scope_predicates(scope: dict[str, Any], alias: str) -> list[str] | None:
    """브랜드/카테고리 scope 를 상품 마스터(P.*) 술어로. 지원 안 하는 scope 키면 None(범위 결합 불가)."""
    predicates: list[str] = []
    for key, value in scope.items():
        if not (isinstance(value, str) and value):
            return None
        if key == "brand":
            predicates.append(f"{alias}.BRAND_NAME = {_sql_quote(value)}")
        elif key == "category":
            predicates.append(
                "("
                + " OR ".join(
                    f"{alias}.{column} = {_sql_quote(value)}"
                    for column in ("CATEGORYL_NAME", "CATEGORYM_NAME", "CATEGORYS_NAME", "CATEGORY")
                )
                + ")"
            )
        else:
            return None  # 미지원 scope 키
    return predicates


def _aggregate_member_subquery(
    config: dict[str, Any], metric: dict[str, Any], operator: str, threshold: int | float,
    window_days: Any, alias: str, purchase_date: Any = None,
    aggregation_scope: str = "per_member", scope: dict[str, Any] | None = None,
    product_scope: dict[str, Any] | None = None,
) -> str | None:
    """회원별 집계 조건 서브쿼리(GROUP BY <회원키>[, grain] HAVING <집계식> <연산자> <임계값>)를 만든다.

    지표 소스: ①회원 요약 컬럼(스냅샷, 기간창·grain·scope 없을 때만), ②집계식(expression 템플릿),
    ③agg+column. **aggregation_scope**: per_member(회원 누적)·per_order(주문별)·per_product(상품별) — grain
    컬럼을 GROUP BY 에 추가한다. **scope**: 브랜드/카테고리면 상품 마스터를 조인(CRM_SL_ORDERDETAILMALL D
    JOIN CRM_CM_PRODUCT P)해 그 범위 안에서만 집계한다. **product_scope**({value,kind}): 상품 자유텍스트
    ('기저귀')를 6컬럼 LIKE 로 같은 상품 마스터 조인 위에 얹어 그 상품 범위 안에서만 집계한다('기저귀를
    2개 이상'). 셋 다 해석 불가/미지원이면 None(무효 SQL 방지)."""
    scope = scope or {}
    join_column = config.get("join_column", "MEMBER_NO")
    date_column = config.get("date_column", "ORDER_DATE")
    has_window = (isinstance(window_days, int) and window_days > 0) or purchase_date is not None
    needs_grain = aggregation_scope in _AGG_GRAIN_COLUMN
    needs_scope = bool(scope) or bool(product_scope)

    # ① 회원 요약 컬럼: 기간창·grain·scope 가 없을 때만(스냅샷은 그 어느 것도 반영 불가).
    source = metric.get("source") if isinstance(metric.get("source"), dict) else {}
    summary = metric.get("summary") if isinstance(metric.get("summary"), dict) else None
    if summary and source.get("preferred") == "member_summary_column" and not (has_window or needs_grain or needs_scope):
        summary_sql = _member_summary_threshold_subquery(summary, operator, threshold, alias)
        if summary_sql is not None:
            return summary_sql

    # scope/grain 이 있으면 상품 단위 테이블(D)로 계산한다(PRODUCT_ID/ORDER_QTY 등 보유). 별칭 접두어 결정.
    table = _PRODUCT_SCOPE_TABLE if (needs_scope or aggregation_scope == "per_product") else (metric.get("table") or config.get("table", "CRM_SL_ORDERHEADERMALL"))
    use_alias = needs_scope
    tp = "D." if use_alias else ""

    # ②/③ 집계식/agg+column.
    expression = metric.get("expression")
    if isinstance(expression, str) and expression.strip():
        agg_expr = _render_aggregate_expression(expression, alias_prefix=tp)
        if agg_expr is None:
            return None
    else:
        column = metric.get("column")
        if not (isinstance(column, str) and column):
            return None
        agg = str(metric.get("agg", "SUM")).upper()
        agg_expr = f"COUNT(DISTINCT {tp}{column})" if metric.get("distinct") else f"{agg}({tp}{column})"

    from_lines = [f"    FROM {table}" + (" D" if use_alias else "")]
    if needs_scope:
        scope_predicates = _scope_predicates(scope, "P")
        if scope_predicates is None:
            return None  # 미지원 scope → 무효(호출부가 처리)
        if product_scope and isinstance(product_scope.get("value"), str) and product_scope["value"]:
            scope_predicates.append(
                _purchase_object_match_predicate(product_scope["value"], product_scope.get("kind"), "P")
            )
        from_lines.append(f"         INNER JOIN CRM_CM_PRODUCT P ON D.PRODUCT_ID = P.PRODUCT_ID")
    else:
        scope_predicates = []

    where = [f"{tp}{join_column} IS NOT NULL"]
    if isinstance(window_days, int) and window_days > 0 and date_column:
        where.append(f"{tp}{date_column} >= {_member_dialect().char8_cutoff(window_days)}")
    date_between = _purchase_date_predicate(purchase_date, alias=("D" if use_alias else None), column=date_column) if date_column else None
    if date_between is not None:
        where.append(date_between)
    where.extend(scope_predicates)

    group_columns = [f"{tp}{join_column}"]
    grain_column = _AGG_GRAIN_COLUMN.get(aggregation_scope)
    if grain_column:
        group_columns.append(f"{tp}{grain_column}")

    return "\n".join(
        [
            "(",
            f"    SELECT {tp}{join_column}",
            *from_lines,
            f"    WHERE {' AND '.join(where)}",
            f"    GROUP BY {', '.join(group_columns)}",
            f"    HAVING {agg_expr} {operator} {_format_threshold(threshold)}",
            f") {alias}",
        ]
    )


def _aggregate_subquery_matches_metric(metric: dict[str, Any], subquery: str) -> bool:
    """생성된 집계 서브쿼리가 지표 스펙과 일치하는지(집계 컬럼 존재) 검증한다. 식(expression)·요약(summary)
    소스는 렌더/소스 선택에서 이미 검증되므로 통과시키고, agg+column 지표만 컬럼 존재를 확인한다."""
    if metric.get("expression") or metric.get("summary"):
        return True
    column = metric.get("column")
    if not (isinstance(column, str) and column):
        return True
    return column in subquery


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
    # 절대 구매창('2019년 1월')이 함께 잡혔으면 집계를 그 기간 주문으로 한정한다(그래야 '2019년 1월에
    # 2개 이상 구매'의 개수 임계값이 기간 안에서 세어진다). 상대창(최근 N일)은 조건별 window_days 소유.
    purchase_date = target_user.get("purchase_date")
    # 상품 자유텍스트('기저귀')가 함께 잡혔으면 집계를 그 상품 범위로 한정한다 — '기저귀를 2개 이상 구매'의
    # 개수 임계값이 상품 스코프 안에서 세어진다(purchase_history 빌더가 여기로 양보). 브랜드/카테고리 scope
    # 는 조건별 scope 가 소유하므로 그와 별개로 얹는다(둘 다 있으면 AND 결합). 단 (1) '상품/제품' 같은 일반
    # 지시어와 (2) per_product/per_order grain(‘동일 상품’·‘한 주문에’) 조건은 상품 스코프로 쓰지 않는다 —
    # 전자는 '%상품%' 오필터, 후자는 grain 이 이미 상품 범위를 표현하므로 LIKE 가 그 의미와 충돌한다.
    # 나열형 다중 상품('기저귀와 건강식품을 2번 이상')은 상품별 스코프 리스트로 편다 — 각 상품마다 별도
    # 집계 서브쿼리(HAVING)를 만들어 INNER JOIN(AND)하면 '각 상품 각각 N번 이상' 의미가 된다. 한 서브쿼리에
    # 두 상품을 OR 로 얹으면 '합쳐서 N번'이 되어 의미가 달라지므로 상품별로 나눈다. 단일 상품이면 리스트 길이 1.
    product_scopes = _target_purchase_objects(target_user)
    product_scope_applied = False
    from_clause = [_member_from_clause()]
    labels = list(compiled["labels"])
    alias_index = 0
    for condition in valid:
        metric = metrics[condition["metric_id"]]
        condition_scope = condition.get("aggregation_scope", "per_member")
        # per_product/per_order grain('동일 상품'·'한 주문에')은 grain 이 이미 상품 범위를 표현하므로 상품
        # 스코프 LIKE 를 얹지 않는다(충돌). per_member 조건만 상품별로 편다; 상품이 없으면 스코프 1개(None).
        cond_scopes = product_scopes if (condition_scope == "per_member" and product_scopes) else [None]
        for cond_product_scope in cond_scopes:
            alias = f"AGG{alias_index}"
            alias_index += 1
            if cond_product_scope:
                product_scope_applied = True
            subquery = _aggregate_member_subquery(
                config, metric, condition["operator"], condition["threshold"], condition.get("window_days"), alias,
                purchase_date=purchase_date,
                aggregation_scope=condition_scope,
                scope=condition.get("scope"),
                product_scope=cond_product_scope,
            )
            if subquery is None:
                # 지표가 컬럼/식/요약 어느 소스로도 해석되지 않음 → 무효 SQL(SUM(None) 등)을 만들지 않는다.
                # plan 을 미지원으로 표시하고 None 반환 — 디스패처가 다른 트랙으로 조용히 폴백하지 않는다(원인 명시).
                query_plan["unsupported"] = {
                    "reason": "unresolved_aggregate_column",
                    "message": f"집계 지표 '{condition['metric_id']}' 를 유효한 컬럼/식/요약 컬럼으로 해석할 수 없습니다.",
                    "clarification": "해당 지표의 집계 정의(컬럼/식/요약 컬럼)가 없어 SQL 을 만들 수 없습니다. 지표 설정을 확인해 주세요.",
                    "metric_id": condition["metric_id"],
                }
                return None
            # metric_id ↔ SQL 집계식 일치 검증(설정/빌더 드리프트 방지): 예) distinct_product_count 가 PRODUCT_ID
            # 아닌 ORDER_ID 로 컴파일되면 실패 처리(그럴듯한 오답 SQL 출고 금지).
            if not _aggregate_subquery_matches_metric(metric, subquery):
                query_plan["unsupported"] = {
                    "reason": "metric_aggregation_mismatch",
                    "message": f"집계 지표 '{condition['metric_id']}' 가 기대한 집계 컬럼으로 컴파일되지 않았습니다.",
                    "clarification": "지표 정의와 생성된 집계식이 일치하지 않습니다. 지표 설정을 확인해 주세요.",
                    "metric_id": condition["metric_id"],
                }
                return None
            from_clause.append(f"     INNER JOIN {subquery} ON B.{join_column} = {alias}.{join_column}")
            labels.append(condition.get("label") or condition["metric_id"])

    where_clauses = list(compiled["predicates"])
    if not compiled["forces_state"]:
        where_clauses.extend(_member_policy_predicates(query_plan))
    where_clauses = _unique_strings(where_clauses)

    select_columns = ["DISTINCT " + _member_key_select(), _member_grade_select()]
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
    # 집계 조건은 이 템플릿이 커버하므로 dropped 에서 뺀다. 상품 스코프를 얹었으면 purchase_object 도 커버된
    # 것이므로 함께 뺀다(purchase_history 가 여기로 양보한 케이스). 그 외 미지원 회원 조건만 부분추출로 고지.
    covered = {"target_user.aggregate_conditions"}
    if product_scope_applied:
        covered.add("target_user.purchase_object")
        covered.add("target_user.purchase_objects")
    dropped = [path for path in compiled["unsupported"] if path not in covered]
    candidate["dropped_conditions"] = dropped
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in dropped]
    return candidate


# 장바구니 집계 지표 → HAVING 식. 새 지표는 여기 한 줄 + 파서 인식만 추가하면 된다.
# 수량은 QTY('담은 수량', schema_catalog important=true)다 — SET_QTY 는 '세트 수량'(세트 상품 구성 수)이라
# 담은 개수와 무관하고, 실데이터에서도 두 컬럼 값이 갈린다(SET_QTY=1·QTY=2 등).
# TOTAL_SALE_PRICE 는 라인별 합계 금액(수량 반영)이라 장바구니 총액은 그 SUM 이다.
# MAX(QTY) 는 '한 상품을 몇 개까지 담았나' = 동일 상품 복수 담기 판정용(SUM 은 서로 다른 상품 합이라 안 된다).
_CART_AGGREGATE_METRIC_EXPRESSIONS = {
    "cart_line_count": "COUNT(DISTINCT CART_PRODUCT_NO)",
    "cart_quantity": "SUM(QTY)",
    "cart_amount": "SUM(TOTAL_SALE_PRICE)",
    "cart_same_product_quantity": "MAX(QTY)",
}


def _no_purchase_anti_join_predicate() -> str:
    """'평생 무주문'(no_purchase) anti-join 술어 — 주문 헤더에 회원 주문이 하나도 없는 회원. 테이블/조인키는
    order_count_targets 레지스트리 소유(형제 무구매 빌더와 동일)."""
    config = _order_count_targets_config()
    table = config.get("table", "CRM_SL_ORDERHEADERMALL")
    join_column = config.get("join_column", "MEMBER_NO")
    return f"NOT EXISTS (SELECT 1 FROM {table} O WHERE O.{join_column} = B.{join_column})"


def build_cart_aggregate_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """'장바구니에 N개 이상 담은' 회원을 ODS_MALL_OMS_CART 회원별 집계 서브쿼리로 추출한다.

    집계는 회원키(CART_ID = B.MEMBER_ID, 형제 cart 빌더와 같은 링크) IN 서브쿼리로 컴파일해, 성별/연령/
    등급 등 회원 속성(compile_member_target_conditions)과 같은 SQL 에 AND 결합한다. KEEP_YN='Y'는 현재
    장바구니에 담긴(미결제) 상태를 뜻한다. 테이블/컬럼은 형제 cart 빌더 관례대로 하드코딩한다."""
    # cart_aggregate 는 단일 조건(dict) 또는 여러 카트 조건(list, '총수량 10개 이상이고 종류 3종 이상').
    raw = query_plan.get("target_user", {}).get("cart_aggregate")
    conditions = [raw] if isinstance(raw, dict) else [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else None
    if not conditions:
        return None
    # 각 조건: 단일 비교는 (operator, threshold), 범위/이중경계는 comparisons=[[op,val],...]. 여러 조건·여러
    # 비교를 모두 하나의 HAVING AND 로 잇는다(같은 GROUP BY CART_ID 서브쿼리에서 SUM(QTY)/COUNT(DISTINCT ...) 병렬).
    having_parts: list[str] = []
    label_parts: list[str] = []
    for condition in conditions:
        raw_comparisons = condition.get("comparisons") or [[condition.get("operator"), condition.get("threshold")]]
        comparisons = [
            (op, th) for op, th in raw_comparisons
            if op in {"=", ">", ">=", "<", "<="} and isinstance(th, (int, float))
        ]
        if not comparisons:
            continue
        metric = condition.get("metric") if condition.get("metric") in _CART_AGGREGATE_METRIC_EXPRESSIONS else "cart_line_count"
        agg_expr = _CART_AGGREGATE_METRIC_EXPRESSIONS[metric]
        having_parts.extend(f"{agg_expr} {op} {_format_threshold(th)}" for op, th in comparisons)
        label_parts.append(metric + "".join(op + _format_threshold(th) for op, th in comparisons))
    if not having_parts:
        return None
    having_expr = " AND ".join(having_parts)
    label = ",".join(label_parts)
    # 보관 기간('일주일 이상 담아둔')이 함께 오면 집계 대상 라인도 담은 시점으로 좁힌다.
    retention_filter = "".join(" AND " + predicate for predicate in _cart_retention_predicates(query_plan, alias=""))
    # 유형('정기배송 상품 3개 이상 담은')이 함께 오면 집계 대상 라인도 그 유형으로 좁힌다. 보관 상태
    # (KEEP_YN='Y') 한정 여부는 형제 cart 빌더와 같은 규칙을 따른다(_cart_is_unpaid_only).
    line_filters = "".join(
        " AND " + predicate
        for predicate in (*_cart_keep_predicates(query_plan, alias=""), *_cart_type_predicates(query_plan, alias=""))
    )
    cart_config = _cart_targets_registry()
    cart_table = cart_config.get("table", "ODS_MALL_OMS_CART")
    cart_join = cart_config.get("join") if isinstance(cart_config.get("join"), dict) else {}
    cart_key = str((cart_join or {}).get("left") or "C.CART_ID").split(".")[-1]
    member_side = str((cart_join or {}).get("right") or "B.MEMBER_ID")
    inner = (
        f"SELECT {cart_key} FROM {cart_table} "
        f"WHERE {cart_key} IS NOT NULL{line_filters}{retention_filter} "
        f"GROUP BY {cart_key} HAVING {having_expr}"
    )
    compiled = compile_member_target_conditions(query_plan)
    where_clauses = [f"{member_side} IN ({inner})", *compiled["predicates"]]
    # '담았지만 구매 이력이 없는'(no_purchase)이 함께 오면 평생 무주문 anti-join 을 AND 결합한다 — 카트에
    # 담긴(KEEP_YN='Y') 미결제 상태에 더해 '한 번도 산 적 없음'까지 명시 요구한 것이므로 조용히 드롭하지 않는다.
    behaviors = set(query_plan.get("target_user", {}).get("behaviors", []))
    if "no_purchase" in behaviors:
        where_clauses.append(_no_purchase_anti_join_predicate())
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())
    # 라벨 컬럼은 세그먼트 태그이자 조건 커버리지(문자열 매칭) 충족용 — 형제 cart 빌더와 동일하게
    # target_segment 를 싣고, segment_label 에는 회원 조건 canonical 라벨(compiled.labels)을 함께 담아
    # 수신동의·캠페인 반응 같은 결합 조건이 커버리지에서 미반영으로 오판되지 않게 한다.
    select_columns = [
        _member_key_select(),
        _sql_quote(_cart_segment_label(query_plan)) + " AS target_segment",
        _sql_quote(",".join(_unique_strings([label, *compiled["labels"]]))) + " AS segment_label",
    ]
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")
    ast = SelectAst(distinct=True, columns=select_columns, from_lines=[_member_from_clause()], where=_unique_strings(where_clauses))
    candidate = _select_ast_candidate(
        "sql_template:cart_aggregate_targets", "장바구니 상품 개수/수량 임계값 타겟 SQL 템플릿(CRMDW)", 1.0, ast, "sql_template"
    )
    # 장바구니 행동(cart_abandoner)은 서브쿼리의 KEEP_YN='Y'가, no_purchase 는 위 anti-join 이 커버하므로
    # dropped 에서 뺀다(형제 cart 빌더와 동일 규칙 + 이 빌더가 추가 컴파일한 no_purchase).
    _attach_cart_dropped_conditions(
        candidate, query_plan, compiled, covered_behaviors=frozenset({"cart_abandoner", "no_purchase"})
    )
    return candidate


def _campaign_response_exists_predicate(predicate: str, negated: bool = False, source: str | None = None) -> str:
    """캠페인 반응 술어를 회원키 EXISTS(부정이면 NOT EXISTS) 서브쿼리로 감싼다.

    조인키는 MBR_NO(문자열)↔MEMBER_NO(숫자)로 타입군이 달라 raw 등호(R.MBR_NO = B.MEMBER_NO)는
    validate_join_keys 의 타입 불일치 가드에 걸려 후보가 통째로 탈락한다(실행 시에도 실패). 그래서
    member_target_filters.json 의 campaign_response_targets.member_join 이 지정한 캐스트 조인
    (TRY_CAST(R.MBR_NO AS BIGINT) = B.MEMBER_NO)을 사용한다.

    source='camp_member_list' 면 반응 팩트 대신 셀 발송 대상 명단(contact_member_list, Z_CAMP_MBR)에
    건다 — 접촉(발송) 성공은 반응자 중심 팩트가 아니라 발송 명단이 분모/소스다. source 없는 예전
    predicate(플랜에 저장된 R.* 형태)는 기존 팩트 EXISTS 그대로 동작한다."""
    config = _MEMBER_TARGET_FILTERS.get("campaign_response_targets", {})
    if source == "camp_member_list":
        member_list = config.get("contact_member_list", {}) if isinstance(config.get("contact_member_list"), dict) else {}
        table = member_list.get("table", "Z_CAMP_MBR")
        alias = member_list.get("alias", "M")
        join = member_list.get("member_join", {}) if isinstance(member_list.get("member_join"), dict) else {}
        left = join.get("left", _member_dialect().cast_bigint(f"{alias}.MBR_NO"))
    else:
        table = config.get("table", "MCS_CAMP_MBR_RSPN_FT")
        alias = config.get("alias", "R")
        join = config.get("member_join", {}) if isinstance(config.get("member_join"), dict) else {}
        left = join.get("left", _member_dialect().cast_bigint("R.MBR_NO"))
    right = join.get("right", "B.MEMBER_NO")
    prefix = "NOT " if negated else ""
    return f"{prefix}EXISTS (SELECT 1 FROM {table} {alias} WHERE {left} = {right} AND {predicate})"


_COUPON_THRESHOLD_OP_SQL = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _fmt_threshold_number(value: Any) -> str:
    """임계값을 SQL 리터럴로. 정수면 소수점 없이(3.0→3), 아니면 그대로."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _coupon_usage_threshold_predicate(threshold: dict[str, Any]) -> str | None:
    """쿠폰 사용 '건수' 임계를 회원별 SUM(USE_CPN_CNT) HAVING 집계의 회원키 IN 서브쿼리로 컴파일한다.

    USE_CPN_CNT 는 반응 팩트(MCS_CAMP_MBR_RSPN_FT)에 회원×캠페인 행 단위로 있어, 회원별 '총 사용 건수'는
    MBR_NO 로 묶어 SUM 해야 한다. 조인키 MBR_NO(varchar)↔MEMBER_NO(bigint)는 캐스트(TRY_CAST)로 타입
    불일치 가드를 통과한다. IN 서브쿼리라 다른 회원 조건과 그대로 AND 결합된다."""
    config = _MEMBER_TARGET_FILTERS.get("campaign_response_targets", {})
    table = config.get("table", "MCS_CAMP_MBR_RSPN_FT")
    alias = config.get("alias", "R")
    member_col = config.get("member_column", "MBR_NO")
    join = config.get("member_join", {}) if isinstance(config.get("member_join"), dict) else {}
    left = join.get("left", _member_dialect().cast_bigint(f"{alias}.{member_col}"))
    right = join.get("right", "B.MEMBER_NO")
    agg = f"SUM(COALESCE({alias}.USE_CPN_CNT, 0))"
    operator = threshold.get("operator")
    if operator == "between":
        lo, hi = threshold.get("min_value"), threshold.get("max_value")
        if lo is None or hi is None:
            return None
        having = f"{agg} >= {_fmt_threshold_number(lo)} AND {agg} <= {_fmt_threshold_number(hi)}"
    else:
        sql_op = _COUPON_THRESHOLD_OP_SQL.get(str(operator))
        value = threshold.get("value")
        if sql_op is None or value is None:
            return None
        having = f"{agg} {sql_op} {_fmt_threshold_number(value)}"
    return (f"{right} IN (SELECT {left} FROM {table} {alias} "
            f"WHERE {alias}.{member_col} IS NOT NULL GROUP BY {alias}.{member_col} HAVING {having})")


def build_campaign_response_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """'캠페인 접촉/오퍼·구매 반응/쿠폰 사용' 회원을 캠페인 반응 팩트(MCS_CAMP_MBR_RSPN_FT)로 추출한다.

    반응 조건 자체는 compile_member_target_conditions 가 회원키 EXISTS 술어로 컴파일한다(여러 개면 각각
    EXISTS 로 AND 결합, 서로 다른 캠페인이어도 됨). 캠페인 반응 외에 주문/집계/랭킹/장바구니 팩트 조인이
    필요한 조건(무구매·재구매·구매 미발생 기간·누적 금액/횟수·상위 N 랭킹 등)이 함께 오면, 그 조건을
    소유한 전용 빌더에 양보(defer)한다 — 그 빌더도 compile_member_target_conditions 로 캠페인 EXISTS 를
    포함하므로 두 조건이 AND 로 함께 남는다('발송 성공했지만 구매하지 않은' 같은 조합이 조용히 한쪽만
    남던 버그 방지). 그런 팩트 신호가 없을 때만 여기서 잡아 세그먼트 라벨을 캠페인 반응 기준으로 붙인다."""
    target_user = query_plan.get("target_user", {})
    responses = target_user.get("campaign_responses")
    thresholds = target_user.get("coupon_usage_thresholds")
    has_responses = isinstance(responses, list) and bool(responses)
    has_thresholds = isinstance(thresholds, list) and bool(thresholds)
    if not has_responses and not has_thresholds:
        return None
    # 전용 팩트조인 빌더 소유 조건(spec.fact_join — 주문횟수/집계/랭킹/카트/반응횟수 등)이 있으면
    # 그 빌더에 양보한다(defer 목록을 조건 IR 레지스트리에서 파생 — 수작업 나열 제거).
    # purchase_object/purchase_date 는 예외 — 캠페인 반응 관용구('캠페인 보고 구매'의 '보고')가 상품으로
    # 오추출돼 구매이력으로 새는 것을 막기 위해 여기서 계속 처리한다(기존 동작 보존).
    keeps = {"purchase_object", "purchase_date"}
    defers_to_fact_builder = any(
        condition.spec.fact_join and condition.kind not in keeps
        for condition in _extract_conditions_ir(query_plan)
    )
    if defers_to_fact_builder:
        return None
    labels = [
        str(response.get("canonical") or "campaign_response")
        for response in (responses or [])
        if isinstance(response, dict) and response.get("predicate")
    ]
    if has_thresholds:
        labels.append("coupon_usage_count")  # 쿠폰 사용 건수 임계(회원키 IN 서브쿼리로 compiled 에 포함)
    if not labels:
        return None
    compiled = compile_member_target_conditions(query_plan)
    where_clauses = list(compiled["predicates"])  # 캠페인 반응 EXISTS 포함
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())
    select_columns = [_member_key_select(), _sql_quote(",".join(_unique_strings(labels))) + " AS segment_label"]
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")
    ast = SelectAst(distinct=True, columns=select_columns, from_lines=[_member_from_clause()], where=_unique_strings(where_clauses))
    candidate = _select_ast_candidate(
        "sql_template:campaign_response_targets", "캠페인 접촉/오퍼·구매 반응/쿠폰 사용 타겟 SQL 템플릿(CRMDW)", 1.0, ast, "sql_template"
    )
    candidate["dropped_conditions"] = compiled["unsupported"]
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in compiled["unsupported"]]
    return candidate


def build_cell_rate_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """'발송 성공률 높고 구매율 낮은 셀'의 발송 대상 회원을 셀 단위 비율 집계로 추출한다.

    회원 조건이 아니라 셀 선별이다: Z_CAMP_MBR(셀별 발송 대상 명단 = 분모, 회원별 접촉성공
    CONTAC_SUCC_YN)를 셀(CAMP_ID, CAMP_EXEC_NO, CELL_NODE_ID)로 집계해 성공률(접촉성공 비중)과
    구매율(구매반응 회원 비중, 분자는 반응 팩트 LEFT JOIN) HAVING 으로 셀을 고른 뒤, 그 셀의 발송
    대상 회원을 회원 테이블에 조인한다. 반응 팩트 단독으로는 불가 — 전 행이 구매반응자라 분모가 없어
    구매율이 항상 100%다. 성별/등급 등 회원 속성은 compile_member_target_conditions 로 AND 결합.
    조인키 MBR_NO(varchar)↔MEMBER_NO(bigint) 는 캐스트 조인으로 타입 불일치 가드를 통과한다."""
    cell_rate = query_plan.get("target_user", {}).get("cell_rate_target")
    if not isinstance(cell_rate, dict):
        return None

    def _valid_rate(condition: Any) -> dict[str, Any] | None:
        if not isinstance(condition, dict):
            return None
        value = condition.get("value")
        operator = condition.get("operator")
        if not isinstance(value, (int, float)) or not 0 < value <= 100:
            return None
        if operator not in _AGG_OPERATOR_WORDS.values():
            return None
        return condition

    success = _valid_rate(cell_rate.get("success_rate"))
    buy = _valid_rate(cell_rate.get("buy_rate"))
    if success is None and buy is None:
        return None

    config = _MEMBER_TARGET_FILTERS.get("cell_rate_targets", {})
    member_table = config.get("member_table", "Z_CAMP_MBR")
    alias = config.get("alias", "M")
    member_col = config.get("member_column", "MBR_NO")
    join = config.get("member_join", {}) if isinstance(config.get("member_join"), dict) else {}
    join_left = str(join.get("left", _member_dialect().cast_bigint(f"{alias}.{member_col}")))
    join_right = str(join.get("right", "B.MEMBER_NO"))
    cell_alias = config.get("cell_alias", "M2")
    cell_subquery_alias = config.get("cell_subquery_alias", "CELL")
    cell_keys = [key for key in config.get("cell_keys", []) if isinstance(key, str)] or [
        "CAMP_ID", "CAMP_EXEC_NO", "CELL_NODE_ID",
    ]
    success_col = config.get("contact_success_column", "CONTAC_SUCC_YN")
    response_join = config.get("response_join", {}) if isinstance(config.get("response_join"), dict) else {}
    response_table = response_join.get("table", "MCS_CAMP_MBR_RSPN_FT")
    response_alias = response_join.get("alias", "R")
    buy_predicate = response_join.get("buy_predicate", f"{response_alias}.BUY_RSPN_YN = 'Y'")

    having_clauses: list[str] = []
    if success is not None:
        success_expr = (
            f"SUM(CASE WHEN {cell_alias}.{success_col} = 'Y' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)"
        )
        having_clauses.append(f"{success_expr} {success['operator']} {_format_threshold(success['value'])}")
    if buy is not None:
        buy_expr = (
            f"COUNT(DISTINCT {response_alias}.{member_col}) * 100.0 / COUNT(DISTINCT {cell_alias}.{member_col})"
        )
        having_clauses.append(f"{buy_expr} {buy['operator']} {_format_threshold(buy['value'])}")

    response_join_conditions = " AND ".join(
        [f"{response_alias}.{key} = {cell_alias}.{key}" for key in cell_keys]
        + [f"{response_alias}.{member_col} = {cell_alias}.{member_col}", buy_predicate]
    )
    cell_key_list = ", ".join(f"{cell_alias}.{key}" for key in cell_keys)
    cell_subquery = "\n".join(
        [
            "(",
            f"    SELECT {cell_key_list}",
            f"    FROM {member_table} {cell_alias}",
            f"    LEFT JOIN {response_table} {response_alias} ON {response_join_conditions}",
            f"    GROUP BY {cell_key_list}",
            "    HAVING " + "\n       AND ".join(having_clauses),
            f") {cell_subquery_alias}",
        ]
    )
    cell_member_join = " AND ".join(
        f"{cell_subquery_alias}.{key} = {alias}.{key}" for key in cell_keys
    )

    compiled = compile_member_target_conditions(query_plan)
    where_clauses = list(compiled["predicates"])
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())

    segment = "low_conversion_cell" if buy is not None else "high_success_cell"
    select_columns = [
        "DISTINCT " + _member_key_select(),
        _member_grade_select(),
        _sql_quote(segment) + " AS target_segment",
    ]
    label = cell_rate.get("label")
    segment_labels = [str(label)] if label else []
    segment_labels.extend(compiled["labels"])
    if segment_labels:
        select_columns.append(_sql_quote(",".join(segment_labels)) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")

    sql = "\n".join(
        [
            "SELECT " + ", ".join(select_columns),
            _member_from_clause(),
            f"     INNER JOIN {member_table} {alias} ON {join_left} = {join_right}",
            f"     INNER JOIN {cell_subquery} ON {cell_member_join}",
            "WHERE " + "\n  AND ".join(_unique_strings(where_clauses)),
        ]
    )
    candidate = _sql_candidate(
        "sql_template:cell_rate_targets",
        "캠페인 셀 성공률/구매율 비율 타겟 추출 SQL 템플릿(CRMDW)",
        1.0,
        sql,
        _template_tables(sql),
        "sql_template",
    )
    candidate["dropped_conditions"] = compiled["unsupported"]
    candidate["dropped_condition_labels"] = [_unsupported_condition_label(path) for path in compiled["unsupported"]]
    return candidate


def build_campaign_response_frequency_targets_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """캠페인 반응 팩트 회원별 집계 타겟: 반응 '횟수'와 캠페인 '귀속 구매금액' 임계값을 추출한다.

    캠페인 반응 EXISTS(≥1회, build_campaign_response_targets_sql_candidate)와 달리 회원별 집계로 거른다:
    MCS_CAMP_MBR_RSPN_FT 를 Z_CAMPAIGN 과 조인해 대상군(CGRP_TYPE_CD='T')·유효 캠페인(취소 제외)·
    최근 N개월 캠페인(Z_CAMPAIGN.CAMP_SDATE 창)으로 좁힌 뒤 GROUP BY 회원으로 HAVING 을 건다 —
    반응 횟수(campaign_response_frequency)는 COUNT(DISTINCT 캠페인) op K, 캠페인 귀속 구매금액
    (campaign_buy_amount)은 SUM(BUY_AMT) op N. 두 조건이 함께 오면 하나의 서브쿼리에서 HAVING AND 로
    결합한다(귀속 금액은 전 생애 주문 합과 다른 지표 — 팩트의 BUY_AMT 가 캠페인 단위 귀속 금액이다).
    반응 팩트엔 범용 반응일자 컬럼이 없어 '최근 N개월'은 캠페인 마스터 시작일로만 걸 수 있다.
    성별/연령/등급/지역 등 회원 속성은 compile_member_target_conditions 로 같은 SQL 에 AND 결합한다.
    조인키 MBR_NO(nvarchar)↔MEMBER_NO(bigint) 는 캐스트 조인(TRY_CAST)으로 타입 불일치 가드를 통과한다."""
    target_user = query_plan.get("target_user", {})
    freq = target_user.get("campaign_response_frequency")
    if isinstance(freq, dict):
        count = freq.get("count")
        operator = freq.get("operator")
        if not isinstance(count, int) or count <= 0 or operator not in _AGG_OPERATOR_WORDS.values():
            freq = None
    else:
        freq = None
    buy = target_user.get("campaign_buy_amount")
    if isinstance(buy, dict):
        amount = buy.get("amount")
        buy_operator = buy.get("operator")
        if not isinstance(amount, (int, float)) or amount <= 0 or buy_operator not in _AGG_OPERATOR_WORDS.values():
            buy = None
    else:
        buy = None
    buy_count = target_user.get("campaign_buy_count")
    if isinstance(buy_count, dict):
        bc_count = buy_count.get("count")
        bc_operator = buy_count.get("operator")
        if not isinstance(bc_count, int) or bc_count <= 0 or bc_operator not in _AGG_OPERATOR_WORDS.values():
            buy_count = None
    else:
        buy_count = None
    if freq is None and buy is None and buy_count is None:
        return None

    config = _MEMBER_TARGET_FILTERS.get("campaign_response_targets", {})
    table = config.get("table", "MCS_CAMP_MBR_RSPN_FT")
    alias = config.get("alias", "R")
    member_col = config.get("member_column", "MBR_NO")
    join = config.get("member_join", {}) if isinstance(config.get("member_join"), dict) else {}
    campaign_join = config.get("campaign_join", {}) if isinstance(config.get("campaign_join"), dict) else {}
    camp_table = campaign_join.get("table", "Z_CAMPAIGN")
    camp_alias = campaign_join.get("alias", "ZC")
    camp_conditions = [c for c in campaign_join.get("conditions", []) if isinstance(c, str)] or [
        f"{camp_alias}.CAMP_ID = {alias}.CAMP_ID",
        f"{camp_alias}.CAMP_EXEC_NO = {alias}.CAMP_EXEC_NO",
    ]
    key_expr = config.get("campaign_key_expression", _member_dialect().concat(f"{alias}.CAMP_ID", "':'", f"{alias}.CAMP_EXEC_NO"))
    response_predicate = config.get("response_predicate", f"({alias}.OFFR_RSPN_YN = 'Y' OR {alias}.BUY_RSPN_YN = 'Y')")
    # 귀속 구매금액 지표(BUY_AMT 합계)와 구매반응 플래그는 설정(aggregate_metrics/boolean_metrics)이 소유.
    aggregate_metrics = config.get("aggregate_metrics", {}) if isinstance(config.get("aggregate_metrics"), dict) else {}
    buy_metric = aggregate_metrics.get("campaign_purchase_amount", {}) if isinstance(aggregate_metrics.get("campaign_purchase_amount"), dict) else {}
    buy_amount_column = buy_metric.get("column", f"{alias}.BUY_AMT")
    buy_amount_agg = buy_metric.get("agg", "SUM")
    boolean_metrics = config.get("boolean_metrics", {}) if isinstance(config.get("boolean_metrics"), dict) else {}
    buy_flag = boolean_metrics.get("purchase_response", {}) if isinstance(boolean_metrics.get("purchase_response"), dict) else {}
    buy_response_predicate = f"{buy_flag.get('column', alias + '.BUY_RSPN_YN')} = {_sql_quote(str(buy_flag.get('value', 'Y')))}"
    target_group = config.get("target_group_condition", {}) if isinstance(config.get("target_group_condition"), dict) else {}
    valid_campaign = config.get("valid_campaign_condition", {}) if isinstance(config.get("valid_campaign_condition"), dict) else {}
    date_column = config.get("campaign_date_column", "CAMP_SDATE")

    inner_where: list[str] = []
    if target_group.get("column") and target_group.get("value"):
        inner_where.append(f"{target_group['column']} = {_sql_quote(str(target_group['value']))}")
    if valid_campaign.get("expression"):
        inner_where.append(str(valid_campaign["expression"]))
    window_days = None
    for condition in (freq, buy, buy_count):
        days = condition.get("window_days") if condition else None
        if isinstance(days, int) and days > 0:
            window_days = days if window_days is None else min(window_days, days)
    if window_days is not None:
        cutoff = _member_dialect().char8_cutoff(window_days)
        inner_where.append(f"{camp_alias}.{date_column} >= {cutoff}")
    # 행 스코프: 반응 '횟수' 조건이 있으면 일반형 '반응'(오퍼/구매) 정의를 쓰고, 귀속 금액/건수만 있으면
    # 구매반응 행으로 좁힌다(BUY_AMT 는 구매반응 행에만 실리고, '구매 건수'도 구매반응 캠페인 수다).
    inner_where.append(response_predicate if freq is not None else buy_response_predicate)

    having_clauses: list[str] = []
    if freq is not None:
        having_clauses.append(f"COUNT(DISTINCT {key_expr}) {freq['operator']} {freq['count']}")
    if buy_count is not None:
        # 구매반응 캠페인 수(구매 건수). 행 스코프가 구매반응 행이면 이 COUNT 는 구매한 캠페인 수가 된다.
        having_clauses.append(f"COUNT(DISTINCT {key_expr}) {buy_count['operator']} {buy_count['count']}")
    if buy is not None:
        having_clauses.append(f"{buy_amount_agg}({buy_amount_column}) {buy['operator']} {_format_threshold(buy['amount'])}")

    subquery = "\n".join(
        [
            "(",
            f"    SELECT {alias}.{member_col}",
            f"    FROM {table} {alias}",
            f"    INNER JOIN {camp_table} {camp_alias} ON " + " AND ".join(camp_conditions),
            "    WHERE " + "\n      AND ".join(inner_where),
            f"    GROUP BY {alias}.{member_col}",
            "    HAVING " + "\n       AND ".join(having_clauses),
            ") O",
        ]
    )
    # 서브쿼리 밖에선 반응 팩트 별칭(R)이 O 로 바뀌므로 조인식의 alias 접두어를 O 로 치환한다.
    left = str(join.get("left", _member_dialect().cast_bigint(f"{alias}.{member_col}"))).replace(f"{alias}.", "O.")
    right = str(join.get("right", "B.MEMBER_NO"))

    # A campaign-frequency predicate and ordinary order aggregates may coexist
    # (for example campaign response >= K AND purchase count >= N).  They have
    # different fact grains, so compile each order metric as its own member-key
    # subquery and join the already-aggregated member sets.  Dropping either
    # condition would turn an AND request into a broader audience.
    order_aggregate_joins: list[str] = []
    aggregate_config = _aggregate_targets_config()
    aggregate_metrics = aggregate_config.get("metrics", {})
    aggregate_conditions = [
        condition for condition in target_user.get("aggregate_conditions") or []
        if isinstance(condition, dict)
        and isinstance(aggregate_metrics.get(condition.get("metric_id")), dict)
        and condition.get("operator") in {"=", ">", ">=", "<", "<="}
        and isinstance(condition.get("threshold"), (int, float))
    ]
    for index, condition in enumerate(aggregate_conditions):
        aggregate_alias = f"CAGG{index}"
        aggregate_subquery = _aggregate_member_subquery(
            aggregate_config,
            aggregate_metrics[condition["metric_id"]],
            condition["operator"],
            condition["threshold"],
            condition.get("window_days"),
            aggregate_alias,
            purchase_date=target_user.get("purchase_date"),
            aggregation_scope=condition.get("aggregation_scope", "per_member"),
            scope=condition.get("scope"),
        )
        if aggregate_subquery is None:
            return None
        aggregate_join_column = aggregate_config.get("join_column", "MEMBER_NO")
        order_aggregate_joins.append(
            f"     INNER JOIN {aggregate_subquery} ON B.{aggregate_join_column} = "
            f"{aggregate_alias}.{aggregate_join_column}"
        )

    compiled = compile_member_target_conditions(query_plan)
    where_clauses = list(compiled["predicates"])
    if not compiled["forces_state"]:
        where_clauses.append(_member_active_state_predicate())

    segment_parts: list[str] = []
    if freq is not None:
        segment_parts.append(f"campaign_responder_{freq['count']}x")
    if buy_count is not None:
        segment_parts.append(f"campaign_buyer_{buy_count['count']}cnt")
    if buy is not None:
        segment_parts.append(f"campaign_buyer_{_format_threshold(buy['amount'])}")
    segment = "_".join(segment_parts)
    select_columns = [
        "DISTINCT " + _member_key_select(),
        _member_grade_select(),
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
            _member_from_clause(),
            f"     INNER JOIN {subquery} ON {left} = {right}",
            *order_aggregate_joins,
            "WHERE " + "\n  AND ".join(_unique_strings(where_clauses)),
        ]
    )
    if freq is not None:
        template_id = "sql_template:campaign_response_frequency_targets"
        title = "최근 N개월 캠페인 K회 이상 반응 타겟 추출 SQL 템플릿(CRMDW)"
        if buy is not None:
            title = "캠페인 반응 횟수 + 귀속 구매금액 타겟 추출 SQL 템플릿(CRMDW)"
    else:
        template_id = "sql_template:campaign_buy_amount_targets"
        title = "캠페인 귀속 구매금액 임계 타겟 추출 SQL 템플릿(CRMDW)"
    candidate = _sql_candidate(
        template_id,
        title,
        1.0,
        sql,
        _template_tables(sql),
        "sql_template",
    )
    covered_paths = {"target_user.aggregate_conditions"} if aggregate_conditions else set()
    candidate["dropped_conditions"] = [path for path in compiled["unsupported"] if path not in covered_paths]
    candidate["dropped_condition_labels"] = [
        _unsupported_condition_label(path) for path in candidate["dropped_conditions"]
    ]
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
        if dimension_filter.get("table") != _member_table():
            continue
        column = (dimension_filter.get("column") or "").split(".")[-1].upper()
        if column not in _member_region_short_columns():
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
        where.append(f"{date_column} >= {_member_dialect().char8_cutoff(window_days)}")
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
    select_columns = ["DISTINCT " + _member_key_select(), _member_grade_select()]
    labels = _union_condition_labels(query_plan)
    if labels:
        select_columns.append(_sql_quote(",".join(labels)) + " AS segment_label")
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")
    sql = "\n".join(
        [
            "SELECT " + ", ".join(select_columns),
            _member_from_clause(),
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


# ══════════════════════════════════════════════════════════════════════════════
# 논리식(OR-of-conjunctions) 컴파일 계층 — 임계값과 서로 다른 지표가 섞인 AND/OR 를 괄호·우선순위를 보존한
# 하나의 SQL 로 컴파일한다. 불리언 구조는 logical_expression 모듈(_logic)이 소유하고, 여기서는 각 Leaf(원자
# 조건)를 기존 도메인 파서(_build_rule_query_plan)로 슬롯화한 뒤 회원(B) 상관 불리언 fragment 로 컴파일한다.
# feature flag(LOGICAL_OR_COMPILER) 뒤에 두고, 실패/검증불일치는 fail-close(미지원) — AND-only 폴백 금지.
# ══════════════════════════════════════════════════════════════════════════════
_LOGIC_OR_RE = re.compile(r"또는|혹은|이거나|거나")
_LOGIC_AND_RE = re.compile(r"그리고|이면서|동시에|이며|이고|면서")
# 오디언스 꼬리말('… 회원을 찾아줘 / 고객을 보여줘 / 회원')을 떼어, 괄호 뒤에 붙은 명사가 논리식 파서의
# 최상위 여분 토큰이 되지 않게 한다('(A) 또는 (B) 회원'). 조건은 이 명사 앞에서 끝나므로 떼도 안전하다.
_LOGIC_TAIL_RE = re.compile(
    r"\s*(?:인|한|하는|이신|된)?\s*(?:회원|고객|사람|유저|이용자|분|대상|명단)"
    r"(?:\s*(?:을|를|들)?\s*(?:찾아|보여|추출|조회|알려|뽑아|골라|선정|선별|검색|리스트업?)\S*)?\s*$"
)
# Leaf 로 컴파일할 때 지원하지 않는 조건 슬롯이 남아 있으면 fail-close(조용한 조건 소실 방지).
_LOGIC_HANDLED_SLOTS = frozenset({
    "gender", "age_min", "age_max", "lifecycle", "aggregate_conditions", "balance_conditions", "cart_aggregate",
})
# target_user 에서 '조건'을 담는 슬롯(비면 무시). 이 중 _LOGIC_HANDLED_SLOTS 밖이 채워져 있으면 미지원 Leaf.
_LOGIC_CONDITION_SLOTS = frozenset({
    "gender", "age_min", "age_max", "age_exclude_ranges", "lifecycle", "interests", "preferred_channels",
    "behaviors", "purchase_object", "purchase_date", "price_sensitivity", "inactivity_period", "recent_login",
    "purchase_inactivity", "birthday_target", "signup_target", "aggregate_conditions", "cart_retention",
    "cart_type", "cart_aggregate", "balance_conditions", "campaign_responses", "campaign_response_frequency",
    "campaign_buy_amount", "campaign_buy_count", "cell_rate_target",
})


def _logical_or_compiler_enabled() -> bool:
    """새 논리식 컴파일러 활성 여부(기본 off = 기존 fail-close 게이트). env LOGICAL_OR_COMPILER∈{1,true,on}."""
    return os.environ.get("LOGICAL_OR_COMPILER", "").strip().casefold() in {"1", "true", "on", "yes"}


def _logical_aggregate_fragment(condition: dict[str, Any], namer: "Callable[[Any], str]") -> tuple[str, dict[str, Any]] | None:
    """집계 조건 → 'B.MEMBER_NO IN (SELECT … HAVING <집계> <op> @param)'. per_order/per_product·scope·식
    기반 지표는 이 계층에서 미지원(None → fail-close)."""
    config = _aggregate_targets_config()
    metric = config.get("metrics", {}).get(condition.get("metric_id"))
    if not isinstance(metric, dict) or not isinstance(metric.get("column"), str):
        return None
    if condition.get("aggregation_scope", "per_member") != "per_member" or condition.get("scope"):
        return None
    operator, threshold = condition.get("operator"), condition.get("threshold")
    if operator not in {"=", ">", ">=", "<", "<="} or not isinstance(threshold, (int, float)):
        return None
    table = metric.get("table") or config.get("table", "CRM_SL_ORDERHEADERMALL")
    join_column = config.get("join_column", "MEMBER_NO")
    date_column = config.get("date_column", "ORDER_DATE")
    column = metric["column"]
    agg = str(metric.get("agg", "SUM")).upper()
    agg_expr = f"COUNT(DISTINCT {column})" if metric.get("distinct") else f"{agg}({column})"
    where = [f"{join_column} IS NOT NULL"]
    window_days = condition.get("window_days")
    if isinstance(window_days, int) and window_days > 0 and date_column:
        where.append(f"{date_column} >= {_member_dialect().char8_cutoff(window_days)}")
    param = namer(threshold)
    inner = (f"SELECT {join_column} FROM {table} WHERE {' AND '.join(where)} "
             f"GROUP BY {join_column} HAVING {agg_expr} {operator} @{param}")
    return f"B.{join_column} IN ({inner})", {"metric": condition.get("metric_id"), "operator": operator, "value": threshold, "domain": "aggregate"}


def _logical_cart_fragment(cart: Any, namer: "Callable[[Any], str]") -> tuple[str, list[dict[str, Any]]] | None:
    """장바구니 집계 조건(dict 또는 list) → 'B.MEMBER_ID IN (SELECT CART_ID … HAVING <agg> <op> @param [AND …])'."""
    conditions = [cart] if isinstance(cart, dict) else [c for c in cart if isinstance(c, dict)] if isinstance(cart, list) else None
    if not conditions:
        return None
    having_parts: list[str] = []
    metas: list[dict[str, Any]] = []
    for condition in conditions:
        raw = condition.get("comparisons") or [[condition.get("operator"), condition.get("threshold")]]
        comparisons = [(op, th) for op, th in raw if op in {"=", ">", ">=", "<", "<="} and isinstance(th, (int, float))]
        if not comparisons:
            return None
        metric = condition.get("metric") if condition.get("metric") in _CART_AGGREGATE_METRIC_EXPRESSIONS else "cart_line_count"
        agg_expr = _CART_AGGREGATE_METRIC_EXPRESSIONS[metric]
        for op, th in comparisons:
            having_parts.append(f"{agg_expr} {op} @{namer(th)}")
            metas.append({"metric": metric, "operator": op, "value": th, "domain": "cart"})
    cart_config = _cart_targets_registry()
    cart_table = cart_config.get("table", "ODS_MALL_OMS_CART")
    cart_join = cart_config.get("join") if isinstance(cart_config.get("join"), dict) else {}
    cart_key = str((cart_join or {}).get("left") or "C.CART_ID").split(".")[-1]
    member_side = str((cart_join or {}).get("right") or "B.MEMBER_ID")
    inner = (f"SELECT {cart_key} FROM {cart_table} WHERE {cart_key} IS NOT NULL AND KEEP_YN = 'Y' "
             f"GROUP BY {cart_key} HAVING {' AND '.join(having_parts)}")
    return f"{member_side} IN ({inner})", metas


def _logical_lifecycle_fragments(lifecycle: list[str], namer: "Callable[[Any], str]") -> tuple[list[str], list[dict[str, Any]]] | None:
    """lifecycle canonical 목록 → 등급은 EMART_GRADE_CD IN(...), 나머지(마케팅/앱푸시 등)는 각 등가 술어."""
    registry = {grade["canonical"]: grade["value"] for grade in _grade_threshold_registry()}
    grade_values = [registry[c] for c in lifecycle if c in registry]
    fragments: list[str] = []
    metas: list[dict[str, Any]] = []
    if grade_values:
        if len(grade_values) == 1:
            fragments.append(f"(B.EMART_GRADE_CD = {_sql_quote(grade_values[0])})")
        else:
            fragments.append("(B.EMART_GRADE_CD IN (" + ", ".join(_sql_quote(v) for v in grade_values) + "))")
        metas.append({"metric": "grade", "operator": "IN", "value": None, "domain": "member_attr"})
    for canonical in lifecycle:
        if canonical in registry:
            continue
        predicate = _member_eq_predicate(canonical)
        if predicate is None:
            return None  # 미지원 lifecycle canonical → fail-close
        fragments.append(f"({predicate})")
        metas.append({"metric": canonical, "operator": "=", "value": None, "domain": "member_attr"})
    return fragments, metas


def _compile_logical_leaf(text: str, prefix: str) -> "_logic.LeafCompile":
    """Leaf(원자 조건 원문)를 기존 도메인 파서로 슬롯화한 뒤 회원(B) 상관 불리언 fragment 로 컴파일한다.
    지원 못 하는 조건이 하나라도 남으면 LeafUnsupported(fail-close). 임계값은 바인드 자리표시자(@prefixN)로."""
    leaf_plan = _build_rule_query_plan(text)
    if isinstance(leaf_plan.get("unsupported"), dict):
        raise _logic.LeafUnsupported(text, str(leaf_plan["unsupported"].get("reason", "unsupported")))
    tu = leaf_plan.get("target_user", {})

    params: dict[str, Any] = {}
    counter = [0]

    def namer(value: Any) -> str:
        name = f"{prefix}{counter[0]}"
        counter[0] += 1
        params[name] = value
        return name

    fragments: list[str] = []
    predicates: list[dict[str, Any]] = []
    covered: set[str] = set()

    if tu.get("gender"):
        predicate = _member_eq_predicate(tu["gender"])
        if predicate is None:
            raise _logic.LeafUnsupported(text, "gender")
        fragments.append(f"({predicate})")
        predicates.append({"metric": "gender", "operator": "=", "value": None, "domain": "member_attr"})
        covered.add("gender")

    age_min, age_max = tu.get("age_min"), tu.get("age_max")
    if age_min is not None or age_max is not None:
        parts: list[str] = []
        if age_min is not None:
            parts.append(f"B.AGE >= @{namer(age_min)}")
        if age_max is not None:
            parts.append(f"B.AGE <= @{namer(age_max)}")
        fragments.append("(" + " AND ".join(parts) + ")")
        predicates.append({"metric": "age", "operator": ">=" if age_min is not None else "<=",
                           "value": age_min if age_min is not None else age_max, "domain": "age"})
        covered.update({"age_min", "age_max"})

    if tu.get("lifecycle"):
        result = _logical_lifecycle_fragments(tu["lifecycle"], namer)
        if result is None:
            raise _logic.LeafUnsupported(text, "lifecycle")
        life_frags, life_metas = result
        fragments.extend(life_frags)
        predicates.extend(life_metas)
        covered.add("lifecycle")

    # 장바구니 조건은 카트 IN 으로 컴파일하고, 카트 어휘가 상품 집계로 샌 phantom aggregate_conditions 는 덮는다.
    if tu.get("cart_aggregate"):
        cart_result = _logical_cart_fragment(tu["cart_aggregate"], namer)
        if cart_result is None:
            raise _logic.LeafUnsupported(text, "cart")
        cart_frag, cart_metas = cart_result
        fragments.append(f"({cart_frag})")
        predicates.extend(cart_metas)
        covered.update({"cart_aggregate", "aggregate_conditions"})
    else:
        for condition in tu.get("aggregate_conditions") or []:
            agg_result = _logical_aggregate_fragment(condition, namer)
            if agg_result is None:
                raise _logic.LeafUnsupported(text, f"aggregate:{condition.get('metric_id')}")
            agg_frag, agg_meta = agg_result
            fragments.append(f"({agg_frag})")
            predicates.append(agg_meta)
        if tu.get("aggregate_conditions"):
            covered.add("aggregate_conditions")

    for condition in tu.get("balance_conditions") or []:
        column = condition.get("column_expr") or f"B.{condition['column']}"
        operator, threshold = condition.get("operator"), condition.get("threshold")
        if operator not in {"=", ">", ">=", "<", "<="} or not isinstance(threshold, (int, float)):
            raise _logic.LeafUnsupported(text, "balance")
        fragments.append(f"({column} {operator} @{namer(threshold)})")
        predicates.append({"metric": condition.get("label") or condition.get("column"),
                           "operator": operator, "value": threshold, "domain": "member_column"})
    if tu.get("balance_conditions"):
        covered.add("balance_conditions")

    # fail-close: 지원하지 않은 조건 슬롯이 남아 있으면(예: 캠페인 반응·무구매·구매 상품) 전체 논리식 실패.
    leftover = [
        slot for slot in _LOGIC_CONDITION_SLOTS
        if slot not in covered and slot not in _LOGIC_HANDLED_SLOTS and tu.get(slot) not in (None, [], {})
    ]
    if leftover:
        raise _logic.LeafUnsupported(text, f"unsupported_condition:{sorted(leftover)}")
    if not fragments:
        raise _logic.LeafUnsupported(text, "no_predicate")

    fragment = fragments[0] if len(fragments) == 1 else "(" + " AND ".join(fragments) + ")"
    return _logic.LeafCompile(fragment=fragment, params=params, predicates=predicates)


def _apply_logical_expression(original_query: str, query_plan: dict[str, Any], normalization_rules: Path | None) -> None:
    """임계값과 서로 다른 지표가 섞인 OR-of-conjunctions 를 논리식 AST 로 파싱·컴파일·검증해 슬롯에 붙인다.

    feature flag off 면 미적용(기존 mixed_and_or 게이트가 fail-close). OR 가 없으면(순수 AND·단일 조건)
    기존 경로에 맡긴다. 파싱/컴파일/검증 실패는 plan['unsupported'] 로 **fail-close** — AND-only 폴백 금지."""
    if not _logical_or_compiler_enabled() or query_plan.get("union_condition"):
        return
    # BFF 가 붙인 "발송 채널: RCS (리치 메시지, …)" 접미어를 먼저 뗀다 — 그 괄호가 논리식 파서에 들어가면
    # 괄호 불균형으로 logical_expression_parse_failed 가 난다(채널 절은 오디언스 조건이 아니다).
    audience_text = _split_channel_suffix(original_query)[0] or original_query
    audience_text = re.split(r"(?:을|를)?\s*대상으로", audience_text, maxsplit=1)[0].strip() or audience_text
    audience_text = _LOGIC_TAIL_RE.sub("", audience_text).strip() or audience_text
    if _LOGIC_OR_RE.search(audience_text) is None:
        return  # OR 없음 → 논리식 컴파일 대상 아님(순수 AND 은 기존 경로)
    # 임계가 낀 OR(기존 mixed_and_or 게이트가 막던 것)만 이 컴파일러가 맡는다 — 등급/지역/연령 같은 동종
    # 회원 속성 OR(→GRADE IN/SIDO IN/구간)은 기존 경로가 정확히 처리하므로 가로채지 않는다(하위 호환).
    if not _has_metric_or_branch(audience_text.replace(" ", "")):
        return
    try:
        ast = _logic.parse(audience_text, _LOGIC_OR_RE, _LOGIC_AND_RE)
    except _logic.ParseError as exc:
        query_plan["unsupported"] = {
            "reason": "logical_expression_parse_failed",
            "message": f"AND/OR 논리식을 파싱하지 못했습니다({exc}).",
            "clarification": "괄호가 맞는지, 각 분기가 완전한 조건인지 확인해 다시 입력해 주시겠어요?",
        }
        return
    if not _logic.has_or(ast):
        return  # top-level OR 아님(순수 AND) → 기존 경로
    try:
        assembled = _logic.assemble(ast, _compile_logical_leaf)
    except _logic.LeafUnsupported as exc:
        query_plan["unsupported"] = {
            "reason": "logical_expression_unsupported_predicate",
            "message": f"논리식의 한 분기를 실DB 조건으로 컴파일할 수 없습니다({exc.reason}).",
            "clarification": "지원되지 않는 지표가 분기에 포함돼 있습니다. 각 분기를 지원되는 조건으로 바꾸거나 별도로 추출해 주시겠어요?",
        }
        return
    except _logic.ParseError as exc:
        query_plan["unsupported"] = {"reason": "logical_expression_parse_failed", "message": str(exc), "clarification": "조건을 다시 확인해 주세요."}
        return
    issues = _logic.verify(assembled, _format_threshold)
    if issues:
        query_plan["unsupported"] = {
            "reason": "logical_expression_verification_failed",
            "message": "생성된 SQL 이 입력 논리식과 의미가 일치하지 않습니다: " + "; ".join(issues),
            "clarification": "조건 구조가 복잡합니다. 분기를 나눠 다시 입력해 주시겠어요?",
        }
        return
    query_plan["logical_expression"] = {
        "fragment": assembled.fragment,
        "params": assembled.params,
        "predicates": assembled.predicates,
        "inline_where": _logic.render_inline(assembled.fragment, assembled.params, _format_threshold),
    }
    query_plan["combine_mode"] = "logical"
    query_plan["set_expressions"] = []


def build_logical_expression_sql_candidate(query_plan: dict[str, Any]) -> dict[str, Any] | None:
    """logical_expression 슬롯(검증 통과한 OR-of-conjunctions)을 CRM_MB_BASEINFO 단일 쿼리로 컴파일한다.
    회원 속성/집계 IN/카트 IN 술어의 괄호·AND/OR 구조를 그대로 WHERE 로 싣고 정상회원 상태를 AND 결합한다."""
    logical = query_plan.get("logical_expression")
    if not isinstance(logical, dict) or not logical.get("inline_where"):
        return None
    where_clauses = [logical["inline_where"], _member_active_state_predicate()]
    labels = _unique_strings([
        str(p.get("metric")) for p in logical.get("predicates", []) if p.get("metric")
    ])
    select_columns = [
        "DISTINCT " + _member_key_select(),
        _member_grade_select(),
        _sql_quote(",".join(labels)) + " AS segment_label" if labels else _sql_quote("logical_or") + " AS segment_label",
    ]
    objective = query_plan.get("campaign_constraints", {}).get("objective")
    if objective:
        select_columns.append(_sql_quote(objective) + " AS objective")
    sql = "\n".join([
        "SELECT " + ", ".join(select_columns),
        _member_from_clause(),
        "WHERE " + "\n  AND ".join(where_clauses),
    ])
    candidate = _sql_candidate(
        "sql_template:logical_expression", "AND/OR 논리식(OR-of-conjunctions) 타겟 추출 SQL 템플릿(CRMDW)", 1.0,
        sql, _template_tables(sql), "sql_template",
    )
    candidate["logical_params"] = logical.get("params")
    candidate["logical_predicates"] = logical.get("predicates")
    return candidate


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
    # 단 집계 조건('누적 구매액 100만 이상')이 함께 오면 집계 빌더에 양보한다 — 그 빌더가 집계 INNER JOIN
    # 과 미구매 anti-join(compile_member_target_conditions 가 방출)을 한 SQL 에 합성한다. 여기서 잡으면
    # 집계 조건이 통째로 드롭된다('고액 구매했지만 최근 무주문' 휴면 고가치 세그먼트가 무주문만 남음).
    if (isinstance(purchase_inactivity, dict) and isinstance(purchase_inactivity.get("min_days"), int)
            and not (isinstance(target_user.get("aggregate_conditions"), list) and target_user["aggregate_conditions"])):
        min_days = purchase_inactivity["min_days"]
        compiled = compile_member_target_conditions(query_plan)
        where_clauses = list(compiled["predicates"])
        if not compiled["forces_state"]:
            where_clauses.append(_member_active_state_predicate())
        where_clauses.append(_purchase_inactivity_predicate(min_days))
        segment = f"purchase_inactive_{min_days}d"
        select_columns = [
            "DISTINCT " + _member_key_select(),
            _member_grade_select(),
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
                _member_from_clause(),
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
        from_clause = [_member_from_clause()]
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
        from_clause = [_member_from_clause(), f"     INNER JOIN {order_subquery} ON B.{join_column} = O.{join_column}"]

    where_clauses = _unique_strings(where_clauses)
    select_columns = [
        "DISTINCT " + _member_key_select(),
        _member_grade_select(),
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
    # 개수/수량 임계값이 함께 오면('3개 이상 담고 일주일 넘게 유지') 집계 빌더가 개수·기간을 모두
    # 컴파일하므로 이 템플릿은 비켜준다 — 여기서 잡으면 개수 조건이 조용히 사라진다.
    if target_user.get("cart_aggregate"):  # dict(단일) 또는 list(복수 카트 조건) 모두 집계 빌더가 소유
        return False
    # 유형('정기배송 상품을 담은')은 그 자체가 카트 라인 술어(CART_TYPE_CD)라 캠페인 목적과 무관하게
    # 카트 오디언스로 본다 — '정기배송'은 objective=subscription 으로도 잡혀서, 목적 게이트를 그대로
    # 적용하면 조건을 정확히 파싱하고도 템플릿이 비켜서 LLM 자유생성(없는 컬럼)으로 떨어졌다.
    if isinstance(target_user.get("cart_type"), dict):
        return True
    # 보관 기간('일주일 이상 담아둔')만 잡혀도 카트 보관 오디언스이므로 같은 템플릿을 쓴다.
    is_cart_audience = "cart_abandoner" in target_user.get("behaviors", []) or isinstance(target_user.get("cart_retention"), dict)
    return is_cart_audience and objective in {None, "purchase", "repurchase", "retention"}


def _template_tables(sql: str) -> list[str]:
    return _unique_strings(
        [match.group(1) for match in re.finditer(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", sql, re.IGNORECASE)]
    )


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def validate_required_input_conditions(query_plan: dict[str, Any], condition_tokens: list[dict[str, Any]]) -> dict[str, Any]:
    aggregation_request = query_plan.get("aggregation_request")
    aggregation_validation = query_plan.get("aggregation_request_validation") or {}
    if isinstance(aggregation_request, dict):
        aggregation_errors = aggregation_validation.get("errors", []) if isinstance(aggregation_validation, dict) else []
        unresolved = aggregation_request.get("unresolvedFields", [])
        if aggregation_errors or unresolved:
            details = [
                str(error.get("message")) for error in aggregation_errors
                if isinstance(error, dict) and error.get("message")
            ] + [str(value) for value in unresolved if str(value).strip()]
            missing = [
                _missing_input_condition(
                    "aggregation_request.unresolvedFields",
                    "집계 요구사항",
                    "집계 SQL 생성 전에 다음 항목의 스키마 또는 업무 정의를 확인해 주세요: " + "; ".join(_unique_strings(details)),
                )
            ]
            return {
                "is_satisfied": False,
                "missing_conditions": missing,
                "clarification_questions": [condition["question"] for condition in missing],
            }

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

    # 그룹별 랭킹의 그룹 축(성별/연령대)은 '필터'가 아니라 '그룹 기준'이다 — 성별 PARTITION·연령대 CASE 가
    # SQL 에 있는 건 사용자가 명시한 그룹 축이므로 '추가된 미명시 조건'으로 오탐하면 안 된다(축별 면제).
    group_axis = _group_ranking_axis(query_plan)
    if not target_user.get("gender") and not exclude.get("gender") and not (set_expression_terms & GENDER_TERMS) and group_axis != "gender" and _has_gender_filter(normalized_sql):
        unexpected_conditions.append(_unexpected_sql_condition("target_user.gender", "성별 조건"))

    if target_user.get("age_min") is None and target_user.get("age_max") is None and not target_user.get("age_exclude_ranges") and not any(term.startswith("age_") for term in set_expression_terms) and group_axis != "age_group" and _has_age_filter(normalized_sql):
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


def _group_ranking_axis(query_plan: dict[str, Any]) -> str | None:
    """그룹별 랭킹 타겟의 그룹 축(region/gender/age_group)을 돌려준다(없으면 None)."""
    group = query_plan.get("group_ranking_target")
    return group.get("group_axis") if isinstance(group, dict) else None


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

    purchase_membership = target_user.get("purchase_membership")
    if (
        not isinstance(query_plan.get("aggregation_request"), dict)
        and isinstance(purchase_membership, dict)
        and purchase_membership.get("operator") == "exists"
        and purchase_membership.get("satisfied_by") != "aggregate_conditions"
    ):
        order_table = _order_count_targets_config().get("table", "CRM_SL_ORDERHEADERMALL")
        terms = [str(order_table), "exists"]
        if isinstance(purchase_membership.get("window_days"), int):
            terms.append(_order_count_targets_config().get("order_date_column", "ORDER_DATE"))
        conditions.append(_condition(
            "target_user.purchase_membership", "purchase_exists", [], all_terms=terms,
        ))

    purchase_inactivity = target_user.get("purchase_inactivity")
    if isinstance(purchase_inactivity, dict) and isinstance(purchase_inactivity.get("min_days"), int):
        conditions.append(_condition(
            "target_user.purchase_inactivity", str(purchase_inactivity["min_days"]), [],
            all_terms=["not exists", str(_order_count_targets_config().get("table", "CRM_SL_ORDERHEADERMALL")),
                       str(_order_count_targets_config().get("order_date_column", "ORDER_DATE"))],
        ))

    if target_user.get("cart_absence"):
        conditions.append(_condition(
            "target_user.cart_absence", "cart_absence", [],
            all_terms=["not exists", str(_cart_targets_registry().get("table", "ODS_MALL_OMS_CART"))],
        ))

    for index, response in enumerate(target_user.get("campaign_responses") or []):
        if not isinstance(response, dict) or not response.get("predicate"):
            continue
        config = _MEMBER_TARGET_FILTERS.get("campaign_response_targets", {})
        source = response.get("source")
        if source == "camp_member_list":
            table = (config.get("contact_member_list") or {}).get("table", "Z_CAMP_MBR")
        else:
            table = config.get("table", "MCS_CAMP_MBR_RSPN_FT")
        conditions.append(_condition(
            f"target_user.campaign_responses[{index}]", str(response.get("canonical") or "campaign_response"), [],
            all_terms=["not exists" if response.get("negated") else "exists", str(table)],
        ))

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

    recent_login = target_user.get("recent_login")
    if isinstance(recent_login, dict) and isinstance(recent_login.get("sql_interval"), str):
        conditions.append(
            _condition(
                "target_user.recent_login",
                recent_login["sql_interval"],
                [recent_login["sql_interval"], str(recent_login.get("min_days", ""))],
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
            # 퍼센트 랭킹은 'TOP N PERCENT', 개수 랭킹은 'TOP N' 이 SQL 에 실제로 있어야 커버로 본다.
            if ranking.get("limit_type") == "percent" and isinstance(ranking.get("percent"), (int, float)):
                limit_terms = [f"top {ranking['percent']:g} percent"]
            else:
                limit_terms = [f"top {ranking.get('top_n', 100)}"]
            conditions.append(
                _condition(
                    "member_metric_ranking",
                    metric["column"],
                    limit_terms,
                    all_terms=[metric["column"], "order by"],
                )
            )

    # 그룹별 회원 Top-N(group_ranking_target)은 PARTITION BY(그룹 컬럼) 윈도와 지표 컬럼, row_num 제한이
    # SQL 에 실제로 있어야 커버로 본다 — 생성부(build_group_ranking_sql_candidate)-검증부 일치.
    group_rank = query_plan.get("group_ranking_target")
    if isinstance(group_rank, dict):
        registry = _load_member_metrics(str(DEFAULT_MEMBER_METRICS_PATH)) or {}
        metric = next((m for m in registry.get("metrics", []) if m.get("metric_id") == group_rank.get("metric_id")), None)
        if metric:
            group_column = group_rank.get("group_column", "SIGUNGU")
            conditions.append(
                _condition(
                    "group_ranking_target",
                    metric["column"],
                    [f"row_num <= {group_rank.get('top_n', 10)}"],
                    all_terms=[metric["column"], "partition by", group_column, "row_number"],
                )
            )

    # 지역 회원 수 랭킹(region_member_count_target)은 지역 GROUP BY 와 회원 수 집계가 SQL 에 있어야 커버로 본다.
    member_count = query_plan.get("region_member_count_target")
    if isinstance(member_count, dict):
        mc_column = member_count.get("column", "SIGUNGU")
        conditions.append(
            _condition(
                "region_member_count_target",
                mc_column,
                [mc_column],
                all_terms=["group by", "count("],
            )
        )

    # 구매 건수 랭킹(purchase_count_ranking)은 상위 N(TOP)과 정렬(ORDER BY)이 SQL 에 실제로 있어야 커버된
    # 것으로 본다 — 생성부(build_purchase_count_ranking_sql_candidate)-검증부 일치.
    count_ranking = query_plan.get("purchase_count_ranking")
    if isinstance(count_ranking, dict):
        conditions.append(
            _condition(
                "purchase_count_ranking",
                "purchase_count",
                [f"top {count_ranking.get('top_n', 100)}"],
                all_terms=["order by"],
            )
        )

    # 회원 테이블 디멘션 필터(예: 시도/SIDO)는 회원/구매 타겟 SQL 에 그대로 컴파일되므로
    # (compile_member_target_conditions) 커버리지도 요구한다 — 생성부-검증부 일치.
    # 보조 속성 테이블 필터(join_column, 예: JOB_CD)도 동일하게 서브쿼리로 컴파일되므로 요구한다.
    # cart 디멘션 타겟팅 모드는 별도 cart SQL 이라 회원 컬럼 조건을 만들지 않으므로 요구하지 않는다.
    if not _is_cart_dimension_targeting(query_plan):
        for dimension_filter in query_plan.get("dimension_filters", []):
            if dimension_filter.get("table") != _member_table() and not dimension_filter.get("join_column"):
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
        # 'oms_cart': 장바구니 테이블(ODS_MALL_OMS_CART)을 조회하는 SQL 은 라벨 문자열 없이도
        # 장바구니 행동을 실제로 커버한다(카트 빌더는 canonical 라벨을 싣지 않는 경우가 있다).
        "cart_abandoner": ["cart_abandoned", "cart_abandoner", "장바구니", "oms_cart"],
        "coupon": ["coupon", "쿠폰", "할인"],
        "app_push": ["app_push", "앱푸시"],
        "kakao": ["kakao", "카카오", "카톡"],
        "price_sensitive": ["price_sensitive", "가격", "쿠폰", "할인"],
    }
    if value in aliases:
        return aliases[value]
    if field_name == "lifecycle" and value in MEMBER_EQ_FILTERS:
        # eq_filters 소유 lifecycle(수신동의/플래그 등)은 canonical 문자열 외에 실컬럼명(B.APP_PUSH_YN 의
        # APP_PUSH_YN)으로도 커버를 인정한다 — 라벨 컬럼을 싣지 않는 빌더(카트류)의 SQL 이 실제로는
        # 조건을 컴파일했는데 문자열 매칭 커버리지에서 탈락하던 문제 방지.
        column = MEMBER_EQ_FILTERS[value][1].split(".")[-1]
        return [value, column]
    if field_name in {"interests", "category"}:
        return [value]
    if field_name in {"preferred_channels", "channels"}:
        return [value]
    if field_name == "behaviors":
        return [value]
    return [value]


def _sql_validation_config() -> dict[str, Any] | None:
    """AST/SQL 검증 설정(member_target_filters.json "validation"). 없으면 코드 기본값."""
    config = _MEMBER_TARGET_FILTERS.get("validation")
    if isinstance(config, dict):
        return config
    fallback = _DEFAULT_MEMBER_TARGET_FILTERS.get("validation")
    return fallback if isinstance(fallback, dict) else None


def _sql_candidate(
    node_id: str, title: str, score: float, sql: str, tables: list[str], source: str,
    ast: "SelectAst | None" = None,
) -> dict[str, Any]:
    """SQL 후보 dict 를 만든다. sql_template(실CRM 타겟 추출) 후보는 Validation 단계를 여기서 거친다.

    파이프라인: 빌더 → (SelectAst) → validate_select_ast → SQL. ast 가 없으면(레거시 문자열 빌더)
    전체 SQL 을 단일 노드로 감싸 같은 검증기(별칭 허용 목록·raw SQL 토큰·OR 분기 수)를 통과시킨다.
    위반(issues)이 있으면 build_sql_template_candidate 오케스트레이터가 이 후보를 거부한다.
    """
    candidate = {
        "id": node_id,
        "title": title,
        "score": round(score, 6),
        "source": source,
        "tables": tables,
        "sql": sql,
    }
    if source == "sql_template":
        config = _sql_validation_config()
        check_ast = ast if ast is not None else SelectAst(columns=[], from_lines=[sql])
        issues = validate_select_ast(check_ast, config)
        candidate["validation"] = {"issues": issues, "ast_used": ast is not None}
        if issues:
            _write_rag_llm_log("sql_ast_validation_rejected", {"id": node_id, "issues": issues, "sql": sql})
    return candidate


def _select_ast_candidate(node_id: str, title: str, score: float, ast: "SelectAst", source: str) -> dict[str, Any]:
    """SelectAst 를 렌더(SQL 생성)하고 검증 메타를 실은 후보를 만든다 — 빌더의 AST 경로 공통 꼬리."""
    sql = render_select_ast(ast)
    return _sql_candidate(node_id, title, score, sql, _template_tables(sql), source, ast=ast)


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
                "expected_grain": (sql_result.get("delivery_validation") or {}).get("expected_grain"),
                "actual_grain": (sql_result.get("delivery_validation") or {}).get("actual_grain"),
                "extracted_conditions": (sql_result.get("delivery_validation") or {}).get("extracted_conditions", []),
                "missing_conditions": (sql_result.get("delivery_validation") or {}).get("missing_conditions", []),
                "semantic_issues": (sql_result.get("delivery_validation") or {}).get("semantic_issues", []),
                "sql_evidence": (sql_result.get("delivery_validation") or {}).get("sql_evidence", {}),
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


# 트레이스 화면 10단계 메타. (step, 한글 단계명, 기술명, method['혼합'=LLM 사용 / '규칙'=결정론]).
# retrieve() 내부에서 실제로 따로 실행·계측되는 단계들을 사용자용 10단계로 노출한다.
_TRACE_STAGES_META: tuple[tuple[int, str, str, str], ...] = (
    (1, "프롬프트 재작성", "정규화·재작성 (normalize_prompt)", "혼합"),
    (2, "타겟/채널 분리", "스코프 분리 (split_prompt_scopes)", "혼합"),
    (3, "질의 계획 수립", "Query Plan (build_query_plan)", "혼합"),
    (4, "상품·구매이력 추출", "타겟 오브젝트 추출 (정규식+LLM)", "혼합"),
    (5, "값 해석(브랜드→코드)", "디멘션 값 해석 (DS_SQL)", "규칙"),
    (6, "집합식 파싱", "집합식 (parse_set_expressions)", "규칙"),
    (7, "지식그래프 검색", "벡터+키워드+Graph 확장 (GraphRAG)", "혼합"),
    (8, "타겟팅 SQL 생성", "SelectAst 조립·렌더 (sql_ast.py) + 조건빌더/LLM", "혼합"),
    (9, "SQL 안전 검증", "sql_guard + 의미 검증", "혼합"),
    (10, "실행·결과", "DB 실행", "규칙"),
)

# timings_ms 키 → 그 키가 있으면 '완료된' 단계 번호들. 부분 트레이스에서 어디까지 갔는지 판정한다.
# (2·4·5·6 은 build_query_plan 안에서 함께 수행되므로 query_plan 계측 하나로 묶어 완료 판정한다.)
_TRACE_TIMING_TO_STEPS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("prompt_normalization", (1,)),
    ("prompt_scopes", (2,)),
    # 2·4·5·6 중 3~6 은 build_query_plan 안에서 함께 수행되므로 query_plan 계측 하나로 완료 판정한다.
    ("query_plan", (3, 4, 5, 6)),
    ("vector_search", (7,)),
    ("keyword_search", (7,)),
    ("context_assembly", (7,)),
    ("sql_generation", (8, 9)),
)


# target_user 중 '상품·구매이력' 단계(4)로 보낼 키. 나머지는 '질의 계획'(3)의 프로필 조건으로 본다.
_PURCHASE_PLAN_KEYS = {
    "aggregate_conditions", "cart_aggregate", "purchase_objects", "sales_objects",
    "target_objects", "purchase_history", "purchase_object", "sales_object",
}


def _trace_line(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _format_captured_prompt(captured: Any, cap: int = 2500) -> list[str]:
    """캡처한 실제 LLM 프롬프트(system/user/response)를 트레이스 details 줄로 만든다.
    system 은 정적·장문이라 길면 자르고(전체는 logs/rag_llm), 값이 치환된 user·응답이 핵심이다."""
    if not isinstance(captured, dict):
        return []

    def _cap(text: str, limit: int) -> str:
        text = text or ""
        return text if len(text) <= limit else text[:limit] + f"\n…(생략 {len(text) - limit}자, 전체는 logs/rag_llm 로그)"

    lines: list[str] = []
    if captured.get("system"):
        lines.append("[system 프롬프트]\n" + _cap(captured["system"], 1500))
    if captured.get("user"):
        lines.append("[user 프롬프트 — 값 치환 완료]\n" + _cap(captured["user"], cap))
    if captured.get("response"):
        lines.append("[LLM 응답]\n" + _cap(captured["response"], 1500))
    return lines


# 단계별 참조 자산(프롬프트/데이터/모델). 화면 "참조" 배지로 노출한다 — 어느 프롬프트·docs/data 를
# 근거로 그 단계가 동작하는지 사용자가 바로 알 수 있게. (정적 매핑 — 실제 로딩 경로는 코드 주석 참고.)
_TRACE_STAGE_REFS: dict[int, tuple[dict[str, str], ...]] = {
    1: (
        # 기본 style="targeting" 은 재작성 프롬프트를, conservative 모드만 정규화 프롬프트를 쓴다(normalize_prompt).
        {"kind": "프롬프트", "name": "prompt_rewrite_system.txt"},
        {"kind": "프롬프트", "name": "prompt_normalize_system.txt (보수 모드)"},
        {"kind": "모델", "name": "{model}"},
    ),
    2: (
        {"kind": "프롬프트", "name": "prompt_scope_split_system.txt"},
        # 대상 방향 표지·채널 신호어를 어휘 사전에서 읽어 절을 나눈다(split_prompt_scopes).
        {"kind": "데이터", "name": "targeting_lexicon.json"},
        {"kind": "모델", "name": "{model}"},
    ),
    3: (
        {"kind": "프롬프트", "name": "query_plan_system.txt"},
        {"kind": "프롬프트", "name": "query_plan_examples.txt"},
        {"kind": "프롬프트", "name": "query_plan_user.txt"},
        # 규칙 계획(_build_rule_query_plan)이 항상 로딩: 정규화 사전·업무정책·어휘/속성 사전·지표 사전·스키마.
        {"kind": "데이터", "name": "normalization_rules.sample.json"},
        {"kind": "데이터", "name": "business_policies.sample.json"},
        {"kind": "데이터", "name": "targeting_lexicon.json"},
        {"kind": "데이터", "name": "attribute_token_groups.json"},
        {"kind": "데이터", "name": "member_metrics.json"},
        {"kind": "데이터", "name": "metric_lexicon.sample.json"},
        {"kind": "데이터", "name": "schema_catalog.json"},
        {"kind": "모델", "name": "{model} (tool calling)"},
    ),
    4: (
        {"kind": "프롬프트", "name": "target_object_extract_system.txt"},
        # 구매이력·판매 동사 신호로 LLM 추출을 게이팅한다(정규식이 놓친 경우만 폴백).
        {"kind": "데이터", "name": "targeting_lexicon.json"},
        {"kind": "모델", "name": "{model} (폴백)"},
    ),
    5: (
        {"kind": "데이터", "name": "dimension_catalog.sample.json"},
        {"kind": "데이터", "name": "member_value_index.json"},
        {"kind": "데이터", "name": "member_target_filters.json"},
        # 디멘션 DS_SQL 을 실DB 에서 실행해 이름→코드로 값을 해석한다(런타임 조회).
        {"kind": "인프라", "name": "실DB (DS_SQL 값 조회)"},
    ),
    6: (
        {"kind": "데이터", "name": "normalization_rules.sample.json"},
        {"kind": "코드", "name": "set_expression_engine.py"},
        {"kind": "코드", "name": "logical_expression.py"},
    ),
    7: (
        # rag_knowledge_base·sql_examples 는 적재(빌드) 시 Qdrant/그래프로 들어간다 — 검색 단계는 그 인덱스를 조회.
        {"kind": "데이터", "name": "rag_knowledge_base.json (적재시)"},
        {"kind": "데이터", "name": "sql_examples.sample.sql (적재시)"},
        {"kind": "모델", "name": "{embed_model}"},
        {"kind": "인프라", "name": "Qdrant + 인메모리 그래프"},
    ),
    8: (
        {"kind": "코드", "name": "sql_ast.py (SelectAst)"},
        {"kind": "코드", "name": "조건빌더 build_*_sql_candidate"},
        {"kind": "데이터", "name": "schema_catalog.json"},
        {"kind": "데이터", "name": "member_target_filters.json"},
        {"kind": "데이터", "name": "member_metrics.json"},
        # 수치 지표 측정단위는 지표 레지스트리(docs/data/metrics/*.json)에서 읽는다(metric_registry).
        {"kind": "데이터", "name": "docs/data/metrics/*.json"},
        {"kind": "프롬프트", "name": "(LLM 폴백 인라인)"},
        {"kind": "모델", "name": "{model} (폴백)"},
    ),
    9: (
        {"kind": "데이터", "name": "schema_catalog.json"},
        # 의미검증 시스템 프롬프트는 member_target_filters.json(컬럼·코드·팩트테이블)에서 렌더된다.
        {"kind": "데이터", "name": "member_target_filters.json"},
        {"kind": "프롬프트", "name": "(의미검증 인라인)"},
        {"kind": "모델", "name": "{model} (의미검증)"},
    ),
    10: (
        # 커넥션은 8단계(infer_target_connection)에서 이미 확정 — 실행은 커넥션 어댑터로 실DB 를 읽는다.
        {"kind": "코드", "name": "db_connections.py (run_read_query / psycopg)"},
    ),
}


def _trace_stage_shell(step: int, name: str, tech_name: str, method: str, status: str) -> dict[str, Any]:
    shell: dict[str, Any] = {"step": step, "name": name, "tech_name": tech_name, "method": method, "status": status}
    refs = _TRACE_STAGE_REFS.get(step)
    if refs:
        # 모델명은 하드코딩하지 않고 실제 설정을 읽는다(OPENAI_MODEL / QDRANT_EMBEDDING_MODEL).
        # 경량 단계(1·2·4·9)는 fast 모델(gpt-4o-mini)로, 나머지 LLM 단계는 메인 모델로 표기 —
        # 실제 호출 라우팅(_fast_llm_model)과 배지를 일치시킨다.
        main_model = os.getenv("OPENAI_MODEL") or DEFAULT_LLM_MODEL
        if step == _TRACE_SEMANTIC_VERIFY_STEP:
            # 9단계 의미검증은 전용 override(OPENAI_SEMANTIC_VERIFY_MODEL) 가 있으면 그걸, 없으면 fast 모델.
            model = os.getenv("OPENAI_SEMANTIC_VERIFY_MODEL") or os.getenv("OPENAI_FAST_MODEL") or "gpt-4o-mini"
        elif step in _TRACE_FAST_MODEL_STEPS:
            model = os.getenv("OPENAI_FAST_MODEL") or "gpt-4o-mini"
        else:
            model = main_model
        embed_model = os.getenv("QDRANT_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
        shell["refs"] = [
            {
                **ref,
                "name": ref["name"].replace("{model}", model).replace("{embed_model}", embed_model),
                "used": False,
            }
            for ref in refs
        ]
    return shell


def _mark_trace_refs_used(stages: list[dict[str, Any]], result: dict[str, Any]) -> None:
    """이번 요청의 Query Plan·검색·SQL 결과에 실제 기여한 참조만 ``used``로 표시한다.

    ``refs`` 전체 목록은 처리 단계에서 사용할 수 있는 자산을 보여주기 위해 유지한다. 이 함수는
    입력이 특정 분기·조건·검색 결과를 만들었을 때만 해당 자산을 강조하도록 응답 메타데이터를 보강한다.
    """
    stages_by_step = {stage.get("step"): stage for stage in stages}

    def mark(step: int, *names: str, kind: str | None = None) -> None:
        wanted_names = set(names)
        for ref in stages_by_step.get(step, {}).get("refs", []):
            if ref.get("name") in wanted_names or (kind is not None and ref.get("kind") == kind):
                ref["used"] = True

    query_plan = result.get("query_plan") or {}
    sql_result = result.get("sql_result") or {}
    prompt_normalization = result.get("prompt_normalization") or {}
    target_user = query_plan.get("target_user") or {}
    exclude = query_plan.get("exclude") or {}
    retrieval = query_plan.get("retrieval") or {}
    parser = query_plan.get("parser") or {}
    matched_terms = query_plan.get("matched_terms") or []
    dimension_filters = query_plan.get("dimension_filters") or []
    semantic_resolutions = query_plan.get("semantic_resolutions") or []
    set_expressions = query_plan.get("set_expressions") or []
    computed_metrics = query_plan.get("computed_metrics") or []
    policy_constraints = query_plan.get("policy_constraints") or []
    condition_tokens = sql_result.get("condition_tokens") or []
    generation_source = str(sql_result.get("generation_source") or "")
    llm_query_plan_prompt = result.get("llm_query_plan_prompt")
    llm_sql_prompt = result.get("llm_sql_prompt")
    semantic_verification = sql_result.get("semantic_verification") or {}
    timings_ms = result.get("timings_ms") or {}

    normalization_mode = str(prompt_normalization.get("mode") or "")
    if normalization_mode == "llm_rewrite":
        mark(1, "prompt_rewrite_system.txt", kind="모델")
    elif normalization_mode == "llm":
        mark(1, "prompt_normalize_system.txt (보수 모드)", kind="모델")

    # 스코프 분리는 규칙 우선이며, LLM 분리가 실제로 선택됐을 때만 프롬프트/모델을 강조한다.
    mark(2, "targeting_lexicon.json")
    if retrieval.get("scope_mode") == "llm":
        mark(2, "prompt_scope_split_system.txt", kind="모델")

    parser_used_llm = parser.get("type") == "llm" and isinstance(llm_query_plan_prompt, dict)
    if parser_used_llm:
        mark(3, "query_plan_system.txt", "query_plan_user.txt", kind="모델")
        examples = _query_plan_fewshot_examples()
        if examples and examples in str(llm_query_plan_prompt.get("system") or ""):
            mark(3, "query_plan_examples.txt")
    if matched_terms:
        mark(3, "normalization_rules.sample.json")
    if policy_constraints or any(
        item.get("source") == "business_policies"
        for item in semantic_resolutions
        if isinstance(item, dict)
    ):
        mark(3, "business_policies.sample.json")
    mark(3, "targeting_lexicon.json")

    attribute_canonicals = {
        canonical
        for group in _attribute_token_groups().values()
        for canonical, _terms in group.canonicals
    }
    lifecycle_values = set(target_user.get("lifecycle") or []) | set(exclude.get("lifecycle") or [])
    if attribute_canonicals & lifecycle_values:
        mark(3, "attribute_token_groups.json")
    if query_plan.get("member_metric_selection") is not None or target_user.get("balance_conditions"):
        mark(3, "member_metrics.json")
    if computed_metrics:
        mark(3, "metric_lexicon.sample.json", "schema_catalog.json")

    if query_plan.get("_trace_target_object_llm_used"):
        mark(4, "target_object_extract_system.txt", kind="모델")
    if target_user.get("purchase_object") or (query_plan.get("campaign_constraints") or {}).get("sell_object"):
        mark(4, "targeting_lexicon.json")

    uses_member_value_index = any(
        item.get("source") == "member_value_index"
        for item in dimension_filters
        if isinstance(item, dict)
    )
    uses_dimension_catalog = any(
        item.get("codes") and item.get("source") not in {"member_value_index", "macro_region"}
        for item in dimension_filters
        if isinstance(item, dict)
    )
    has_member_condition = any(value not in (None, [], {}) for value in target_user.values()) or bool(exclude) or bool(dimension_filters)
    if uses_dimension_catalog:
        mark(5, "dimension_catalog.sample.json", "실DB (DS_SQL 값 조회)")
    if uses_member_value_index:
        mark(5, "member_value_index.json")
    if has_member_condition:
        mark(5, "member_target_filters.json")

    if set_expressions:
        mark(6, "normalization_rules.sample.json", "set_expression_engine.py", "logical_expression.py")

    search_executed = "vector_search" in timings_ms or "keyword_search" in timings_ms
    search_hits = [
        *result.get("vector_matches", []),
        *result.get("keyword_matches", []),
        *result.get("graph_context", []),
    ]
    if search_executed:
        mark(7, "rag_knowledge_base.json (적재시)", "Qdrant + 인메모리 그래프")
    if "vector_search" in timings_ms:
        mark(7, kind="모델")
    if any(item.get("type") == "sql_example" for item in search_hits if isinstance(item, dict)):
        mark(7, "sql_examples.sample.sql (적재시)")

    has_sql_candidate = bool(sql_result.get("candidates"))
    if has_sql_candidate:
        mark(8, "sql_ast.py (SelectAst)", "schema_catalog.json")
    if has_sql_candidate and not generation_source.startswith("llm"):
        mark(8, "조건빌더 build_*_sql_candidate")
    if any(
        str(token.get("path") or "").startswith(("target_user.", "exclude.", "dimension_filters."))
        for token in condition_tokens
        if isinstance(token, dict)
    ):
        mark(8, "member_target_filters.json")
    if any(
        str(token.get("path") or "").startswith(("computed_metrics.", "member_metric_selection", "target_user.aggregate_conditions"))
        for token in condition_tokens
        if isinstance(token, dict)
    ):
        mark(8, "member_metrics.json", "docs/data/metrics/*.json")
    if isinstance(llm_sql_prompt, dict):
        mark(8, "(LLM 폴백 인라인)", kind="모델")

    if has_sql_candidate:
        mark(9, "schema_catalog.json")
    if semantic_verification.get("ran"):
        mark(9, "member_target_filters.json", "(의미검증 인라인)", kind="모델")


def _trace_failure_diagnosis(
    category: str,
    label: str,
    confidence: str,
    summary: str,
    evidence: list[str],
    next_action: str,
) -> dict[str, Any]:
    return {
        "category": category,
        "label": label,
        "confidence": confidence,
        "summary": summary,
        "evidence": evidence,
        "next_action": next_action,
    }


def _trace_sql_failure_diagnosis(
    query_plan: dict[str, Any],
    sql_result: dict[str, Any],
) -> dict[str, Any] | None:
    """검증 SQL 미생성 사유를 입력·참조 데이터·개발/정책 점검으로 분류한다.

    참조 JSON이 로드돼도 사용자의 조건을 실DB 컬럼으로 매핑하지 못할 수 있으므로, 근거가 명확한
    ``unsupported_conditions`` 계열만 데이터/매핑 부족으로 단정한다. 나머지는 입력 부족 또는
    개발/정책 설정 점검으로 분리하고, 근거가 약하면 낮은 신뢰도로 반환한다.
    """
    reason = str(sql_result.get("failure_reason") or "")
    if not reason:
        return None

    unsupported_labels = [
        str(label)
        for label in sql_result.get("unsupported_condition_labels", [])
        if str(label)
    ]
    missing_paths = [
        str(condition.get("path"))
        for condition in sql_result.get("missing_input_conditions", [])
        if isinstance(condition, dict) and condition.get("path")
    ]
    clarification_questions = [
        str(question)
        for question in sql_result.get("clarification_questions", [])
        if str(question)
    ]
    recognized_domains = _recognized_domains(query_plan)

    if reason == "query_plan_required_conditions_missing":
        evidence = [f"미확정 조건: {path}" for path in missing_paths]
        evidence.extend(f"확인 질문: {question}" for question in clarification_questions)
        return _trace_failure_diagnosis(
            "input_incomplete",
            "입력 조건 추가 필요",
            "high",
            "필수 조건이 확정되지 않아 SQL을 만들지 못했습니다. 참조 JSON 부족으로 단정하지 않습니다.",
            evidence or ["Query Plan 필수 조건이 비어 있습니다."],
            "실패 단계에 표시된 조건을 더 구체적으로 입력한 뒤 다시 실행하세요.",
        )

    data_gap_reasons = {
        "real_db_unsupported_conditions",
        "recognized_domain_unsupported",
        *_UNSUPPORTED_INTENT_REASONS,
    }
    if reason in data_gap_reasons or (reason == "no_sql_candidates" and recognized_domains):
        evidence = [f"실DB 미매핑 조건: {label}" for label in unsupported_labels]
        evidence.extend(
            f"인식된 도메인: {domain.get('label', domain.get('code', 'unknown'))}"
            for domain in recognized_domains
            if isinstance(domain, dict)
        )
        return _trace_failure_diagnosis(
            "reference_data_gap",
            "참조 데이터/매핑 보강 필요",
            "high" if unsupported_labels else "medium",
            "입력 조건은 인식됐지만 현재 참조 JSON 또는 실DB 매핑으로 SQL 조건을 만들 수 없습니다.",
            evidence or [f"실패 사유: {reason}"],
            "강조된 member_target_filters.json, dimension_catalog.sample.json, schema_catalog.json의 조건·컬럼·코드 매핑을 확인하세요.",
        )

    if reason == "no_sql_candidates":
        return _trace_failure_diagnosis(
            "input_unrecognized",
            "타겟 조건 인식 부족",
            "medium",
            "명시적인 타겟 조건을 찾지 못해 SQL 후보를 만들지 못했습니다.",
            ["인식된 실DB 타겟 도메인이 없습니다."],
            "성별, 연령, 등급, 지역, 구매 이력처럼 지원되는 타겟 조건을 추가해 다시 실행하세요.",
        )

    implementation_reasons = {
        "sql_guard_failed",
        "query_plan_conditions_missing",
        "query_plan_unmentioned_conditions_added",
        "intent_scope_mismatch",
        "semantic_verification_failed",
    }
    if reason in implementation_reasons:
        failure_stage = _classify_failure_stage(reason, sql_result)
        return _trace_failure_diagnosis(
            "implementation_or_policy_review",
            "개발/정책 설정 점검 필요",
            "high",
            "입력 데이터 부족으로 판정할 근거보다 생성·검증 규칙에서 막힌 근거가 더 강합니다.",
            [
                f"실패 사유: {reason}",
                f"실패 단계: {failure_stage['label']}" if failure_stage else "실패 단계 미분류",
            ],
            "해당 단계의 기술 정보와 SQL 가드·조건 커버리지·의미 검증 결과를 개발자가 확인하세요.",
        )

    return _trace_failure_diagnosis(
        "unknown",
        "원인 추가 확인 필요",
        "low",
        "현재 실패 사유만으로는 참조 데이터 부족과 개발 오류를 신뢰성 있게 구분할 수 없습니다.",
        [f"실패 사유: {reason}"],
        "실패 단계의 상세 로그와 참조 파일을 함께 확인하세요.",
    )


def _trace_exception_diagnosis(error_message: str) -> dict[str, Any]:
    """중간 예외를 데이터 파일·인프라·개발 오류로 보수적으로 분류한다."""
    normalized = error_message.casefold()
    if "filenotfounderror" in normalized or "no such file" in normalized:
        return _trace_failure_diagnosis(
            "reference_data_error",
            "참조 파일 누락",
            "high",
            "처리에 필요한 JSON 또는 프롬프트 파일을 찾지 못했습니다.",
            [error_message],
            "docs/data 또는 docs/prompts 경로와 배포된 파일 목록을 확인하세요.",
        )
    if "jsondecodeerror" in normalized or "json decode" in normalized:
        return _trace_failure_diagnosis(
            "reference_data_error",
            "참조 JSON 형식 오류",
            "high",
            "참조 JSON을 읽는 중 형식 오류가 발생했습니다.",
            [error_message],
            "최근 수정한 docs/data JSON의 문법과 필수 필드를 확인하세요.",
        )
    if any(token in normalized for token in ("connectionerror", "timeouterror", "operationalerror", "importerror", "modulenotfounderror", "qdrant", "openai", "missing_openai_api_key")):
        return _trace_failure_diagnosis(
            "infrastructure_or_configuration",
            "실행 환경/연결 설정 점검 필요",
            "high",
            "참조 데이터나 애플리케이션 코드보다 외부 서비스 또는 환경 설정 문제 가능성이 높습니다.",
            [error_message],
            "Qdrant·DB·OpenAI 연결과 관련 환경 변수를 확인하세요.",
        )
    if any(token in normalized for token in ("keyerror", "attributeerror", "assertionerror", "nameerror", "typeerror", "unboundlocalerror", "programmingerror", "syntaxerror", "undefinedtable", "undefinedcolumn")):
        return _trace_failure_diagnosis(
            "implementation_error",
            "개발 오류 가능성",
            "high",
            "처리 코드의 예외 유형이 감지됐습니다. 참조 JSON 부족으로 보이지 않습니다.",
            [error_message],
            "실패 단계의 서버 로그와 스택 트레이스를 개발자가 확인하세요.",
        )
    if "valueerror" in normalized:
        return _trace_failure_diagnosis(
            "input_or_configuration_error",
            "입력값 또는 설정값 확인 필요",
            "medium",
            "허용되지 않은 입력값 또는 설정값이 처리 단계에서 감지됐습니다.",
            [error_message],
            "요청 파라미터와 관련 환경 설정의 허용값을 확인하세요.",
        )
    return _trace_failure_diagnosis(
        "unknown",
        "원인 추가 확인 필요",
        "low",
        "예외 유형만으로 참조 데이터 부족과 개발 오류를 신뢰성 있게 구분할 수 없습니다.",
        [error_message],
        "서버 로그와 실패 단계의 기술 정보를 확인하세요.",
    )


def _trace_execution_failure_diagnosis(execution: dict[str, Any]) -> dict[str, Any]:
    error = str(execution.get("error") or execution.get("failure_reason") or "database_execution_failed")
    diagnosis = _trace_exception_diagnosis(error)
    if diagnosis["category"] != "unknown":
        return diagnosis
    return _trace_failure_diagnosis(
        "infrastructure_or_configuration",
        "DB 실행 설정 점검 필요",
        "medium",
        "검증 SQL은 생성됐지만 DB 실행 단계에서 실패했습니다.",
        [error],
        "대상 DB 연결, 권한, 방언 설정과 실행된 SQL을 확인하세요.",
    )


def build_partial_retrieve_trace(query: str, timings_ms: dict[str, Any], error_message: str) -> dict[str, Any]:
    """retrieve() 가 중간에 예외로 죽었을 때, 그때까지 채워진 timings_ms 로 '오류 전까지'의 부분 트레이스를 만든다.

    완료된 단계는 status=ok, 처음으로 도달하지 못한 단계는 status=fail(여기서 막힘), 그 이후는 skipped.
    실제 예외 지점을 timings_ms 계측 단위(정규화/Query Plan/검색/SQL 생성)로만 좁힐 수 있어, 그 묶음의 첫
    단계에 오류를 귀속한다(예: Query Plan LLM 파싱 실패 → '질의 계획 수립'에 표시)."""
    completed: set[int] = set()
    for key, steps in _TRACE_TIMING_TO_STEPS:
        if timings_ms.get(key) is not None:
            completed.update(steps)

    stages: list[dict[str, Any]] = []
    error_marked = False
    for step, name, tech_name, method in _TRACE_STAGES_META:
        if step in completed:
            status = "ok"
        elif not error_marked and step != 10:
            status, error_marked = "fail", True
        else:
            status = "skipped"  # 오류 이후 단계이거나(도달 못함), 실행 전 중단된 10단계
        shell = _trace_stage_shell(step, name, tech_name, method, status)
        if status == "fail":
            shell["summary"] = "이 단계에서 처리가 중단되었습니다"
            shell["details"] = [f"오류: {error_message}"]
        elif status == "skipped" and step != 10:
            shell["summary"] = "앞 단계 오류로 실행되지 않음"
        stages.append(shell)

    return {
        "query": query,
        "partial": True,
        "stages": stages,
        "timings_ms": timings_ms,
        "failure_diagnosis": _trace_exception_diagnosis(error_message),
        "result": {"status": "error", "message": f"처리 중 오류가 발생했습니다: {error_message}"},
    }


def build_retrieve_trace(result: dict[str, Any]) -> dict[str, Any]:
    """retrieve() 결과를 사용자용 10단계 트레이스(프롬프트 재작성 → … → 실행·결과)로 재구성한다.
    각 단계에 method(혼합/규칙)·status(ok/info/fail/skipped)를 붙인다. LLM 호출 없이 결정론적으로 동작."""
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
            # 집계·grain 정합성 경고(DISTINCT 은폐·1:N 조인 의심 등) — 후보를 탈락시키진 않지만 검토용으로 노출.
            "analytics_warnings": [issue.get("message") for issue in candidate.get("analytics_warnings", [])],
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

    # ── 단계별 페이로드 준비 ─────────────────────────────────────────────────────
    tu = {key: value for key, value in target_user.items() if value not in (None, [], {})}
    tu_purchase = {k: v for k, v in tu.items() if k in _PURCHASE_PLAN_KEYS}
    tu_profile = {k: v for k, v in tu.items() if k not in _PURCHASE_PLAN_KEYS}
    set_expressions = query_plan.get("set_expressions", []) or []
    dimension_filters = query_plan.get("dimension_filters", []) or []
    semantic_resolutions = query_plan.get("semantic_resolutions", []) or []
    semantic_verification = sql_result.get("semantic_verification", {"ran": False}) or {"ran": False}
    failure_stage = _classify_failure_stage(sql_result.get("failure_reason"), sql_result)
    cart_context = query_plan.get("cart_context")
    corrections = prompt_normalization.get("corrections", []) or []
    vcount = len(result.get("vector_matches", []))
    kcount = len(result.get("keyword_matches", []))
    gcount = len(graph_rows)
    selected_sql = sql_result.get("sql")
    is_success = sql_result.get("is_success")

    # ── 3단계용: 원문 → 계획 문장 → Query Plan JSON (실제 예문이 잘려 JSON 이 되는 모습) ──
    planning_query = query_plan.get("planning_query") or prompt_normalization.get("normalized")
    campaign_constraints = {k: v for k, v in query_plan.get("campaign_constraints", {}).items() if v not in (None, [], {})}
    plan_json_slots = {k: v for k, v in {
        "intent": query_plan.get("intent"),
        "target_user": tu,
        "dimension_filters": dimension_filters,
        "set_expressions": [e.get("ko_label") or e.get("expression_id") for e in set_expressions],
        "campaign_constraints": campaign_constraints,
    }.items() if v not in (None, [], {}, "")}
    plan_json = json.dumps(plan_json_slots, ensure_ascii=False, indent=2)

    # 실제 전송된 LLM 프롬프트(질의 계획 tool calling / SQL 폴백). retrieve 가 result 상단에 담아 준다.
    llm_query_plan_prompt = result.get("llm_query_plan_prompt")
    llm_sql_prompt = result.get("llm_sql_prompt")

    # ── 8단계용: 선택된 SQL 을 어떤 방식으로 만들었나(결정론 조건빌더 vs LLM 폴백; 둘 다 SelectAst 로 렌더) ──
    generation_source = sql_result.get("generation_source")
    if not generation_source:
        generation_label = None
    elif str(generation_source).startswith("llm"):
        generation_label = f"LLM 폴백 생성 ({generation_source})"
    else:
        generation_label = f"결정론 조건빌더 ({generation_source})"

    def _stage(step: int, status: str, **payload: Any) -> dict[str, Any]:
        _, name, tech_name, method = _TRACE_STAGES_META[step - 1]
        stage = _trace_stage_shell(step, name, tech_name, method, status)
        # 빈 값(None/[]/{}/"")은 화면에 노이즈라 떨어뜨린다. 0/False 는 의미가 있어 남긴다.
        stage.update({k: v for k, v in payload.items() if v not in (None, [], {}, "")})
        return stage

    # 후보별 검증 플래그(9단계) 한 줄 요약.
    candidate_flag_lines = []
    for candidate in candidate_rows:
        parts = []
        for label, key in (("guard", "guard_valid"), ("coverage", "coverage_ok"), ("scope", "intent_scope_ok"), ("unmentioned", "unmentioned_ok"), ("eligible", "is_eligible")):
            val = candidate.get(key)
            if val is not None:
                parts.append(f"{label}={'✓' if val else '✗'}")
        candidate_flag_lines.append(f"candidate {candidate.get('id')}: {' '.join(parts)}".rstrip())

    stages = [
        _stage(
            1, "info",
            description="고객 문장의 오타·표현을 시스템이 이해할 표준 문장으로 다시 씁니다(LLM 재작성).",
            summary=prompt_normalization.get("summary") or None,
            plain=[line for line in [
                f"입력: {prompt_normalization.get('original', result.get('query'))}",
                f"재작성: {prompt_normalization.get('normalized', result.get('query'))}",
            ] if line],
            details=[f"교정: {_trace_line(c)}" for c in corrections],
        ),
        _stage(
            2, "info",
            description="재작성 문장을 '누구를(타겟)' 절과 '무엇을 보낼지(채널·혜택)' 절로 나눕니다.",
            plain=[line for line in [
                f"타겟 절: {prompt_scopes.get('targeting')}" if prompt_scopes.get("targeting") else None,
                f"채널 절: {prompt_scopes.get('channel')}" if prompt_scopes.get("channel") else None,
                None if (prompt_scopes.get("targeting") or prompt_scopes.get("channel")) else "분리 없음 — 전체 문장을 타겟 절로 사용",
            ] if line],
            details=[f"scope_mode={prompt_scopes.get('mode')}"] if prompt_scopes.get("mode") else [],
        ),
        _stage(
            3, "info",
            summary=f"intent={query_plan.get('intent')}" if query_plan.get("intent") else None,
            description="타겟 절을 규칙 파싱 + LLM tool calling 으로 구조화된 Query Plan JSON(슬롯)으로 바꿉니다. 이 JSON 이 다음 SQL 조립의 입력입니다.",
            plain=[line for line in (
                [f"계획 문장(파싱 대상): {planning_query}" if planning_query else None]
                + [f"‘{m.get('matched_text')}’ → {m.get('canonical')}" for m in targeting_matched_terms if m.get("matched_text") and m.get("canonical")]
            ) if line],
            # 원문이 어떻게 잘려 어떤 JSON 값이 되는지 그대로 노출한다.
            details=[
                f"① 입력 원문: {result.get('query')}",
                f"② 계획 문장(타겟 절만): {planning_query}",
                "③ Query Plan JSON:\n" + plan_json,
            ] + ([f"④ 이 JSON 으로 만들 SQL 생성 방식: {generation_label}"] if generation_label else [])
            + (["── 실제 LLM 프롬프트(질의 계획, tool calling) ──"] + _format_captured_prompt(llm_query_plan_prompt)
               if llm_query_plan_prompt else ["LLM 미사용 — 규칙 파싱으로 계획 수립(parser=" + str((query_plan.get("parser") or {}).get("type") or "rules") + ")"]),
        ),
        _stage(
            4, "info" if (tu_purchase or cart_context) else "skipped",
            description="상품 구매·판매 이력, 장바구니 같은 행동 조건을 추출합니다(정규식 우선, LLM 폴백).",
            plain=None if (tu_purchase or cart_context) else ["구매·상품 관련 조건 없음"],
            details=[f"{k}: {_trace_line(v)}" for k, v in tu_purchase.items()]
                    + ([f"cart_context: {_trace_line(cart_context)}"] if cart_context else []),
        ),
        _stage(
            5, "info" if (dimension_filters or semantic_resolutions) else "skipped",
            description="브랜드·지역 같은 표기를 실DB 코드(DS_SQL)로 변환합니다(결정론).",
            plain=[
                f"{d.get('prompt_label') or d.get('dimension_id')} → {', '.join(map(str, d.get('codes', []))) or _trace_line(d.get('names', []))}"
                for d in dimension_filters
            ] or (None if not semantic_resolutions else ["의미 해석 규칙 적용"]),
            details=[f"dimension: {_trace_line(d)}" for d in dimension_filters]
                    + [f"semantic: {_trace_line(s)}" for s in semantic_resolutions],
        ),
        _stage(
            6, "info" if set_expressions else "skipped",
            description="‘A 또는 B’·‘A이면서 B’ 같은 집합 연산을 파싱해 SQL 술어로 만듭니다(결정론).",
            plain=None if set_expressions else ["집합식 없음"],
            details=[f"{e.get('ko_label') or e.get('expression_id')}: {_trace_line(e.get('set_ast'))}" for e in set_expressions],
        ),
        _stage(
            7, "info",
            summary=f"벡터 {vcount} · 키워드 {kcount} → 확장 {gcount}",
            description="벡터(의미)·키워드(글자) 검색으로 지식을 찾고 관계 그래프로 확장합니다.",
            plain=[f"AI 유사도 검색 {vcount}건, 키워드 검색 {kcount}건에서 출발해 관계 그래프로 {gcount}건까지 넓혔습니다."],
            hits=graph_rows,
            count=gcount,
            seed_count=len(result.get("seed_matches", [])),
            details=[f"vector: {_trace_line(h.get('id'))} ({h.get('score')})" for h in result.get("vector_matches", [])[:5]]
                    + [f"keyword: {_trace_line(h.get('id'))} ({h.get('score')})" for h in result.get("keyword_matches", [])[:5]],
        ),
        _stage(
            8, "ok" if selected_sql else "info",
            summary=(f"{generation_label} · " if generation_label else "") + f"후보 {len(candidate_rows)}개" + (" · 선택됨" if selected_sql else " · 미선택"),
            description="Query Plan JSON 을 조건빌더가 SelectAst(sql_ast.py)로 조립해 SQL 로 렌더합니다. 결정론 빌더가 표현 못 하면 LLM 폴백으로 초안을 만들고 같은 가드를 태웁니다.",
            plain=[line for line in [
                f"생성 방식: {generation_label}" if generation_label else None,
                f"조건 토큰 {len(sql_result.get('condition_tokens', []))}개로 SQL 후보 {len(candidate_rows)}개를 SelectAst 로 조립했습니다.",
                "검증 통과 SQL이 선택되었습니다." if selected_sql else "선택된 SQL이 아직 없습니다(다음 단계에서 사유 표시).",
            ] if line],
            details=[line for line in [
                "condition_tokens: " + ", ".join(t.get("path") for t in sql_result.get("condition_tokens", []) if t.get("path")),
                "required_conditions: " + ", ".join(c.get("path") for c in sql_result.get("required_conditions", []) if c.get("path")),
            ] if line.split(": ", 1)[-1]]
                    + [f"candidate {c.get('id')}: tables={','.join(c.get('tables', []))}" for c in candidate_rows]
                    + (["── 실제 LLM 프롬프트(SQL 폴백) ──"] + _format_captured_prompt(llm_sql_prompt)
                       if llm_sql_prompt else ["결정론 조건빌더가 SelectAst 로 직접 조립 — LLM 프롬프트 없음(생성 SQL 은 아래 '실행된 SQL' 참조)"]),
        ),
        _stage(
            9, "ok" if is_success else "fail",
            summary="검증 통과" if is_success else ("검증 실패 · " + (failure_stage.get("label") if failure_stage else str(sql_result.get("failure_reason")))),
            description="생성된 SQL이 허용 테이블·컬럼만 쓰는지, 요청 의도와 어긋나지 않는지 자동 점검합니다.",
            plain=[line for line in [
                "안전 검증을 통과했습니다." if is_success else (f"‘{failure_stage.get('label')}’ 단계에서 막혔습니다." if failure_stage else "검증에서 막혔습니다."),
                (f"의미 검증: {'원문과 일치' if semantic_verification.get('faithful') else '불일치(확인 필요)'}") if semantic_verification.get("ran") else None,
            ] if line],
            details=candidate_flag_lines
                    + ([f"semantic_verification: ran={semantic_verification.get('ran')} faithful={semantic_verification.get('faithful')} issues={_trace_line(semantic_verification.get('issues', []))}"] if semantic_verification.get("ran") else [])
                    + ([f"failure_reason: {sql_result.get('failure_reason')}"] if sql_result.get("failure_reason") else []),
            failure_stage=failure_stage,
        ),
        # 10단계(실행·결과)는 엔드포인트가 execute 후 채운다. 여기선 대기 상태로 둔다.
        _stage(
            10, "skipped",
            summary="실행 대기",
            description="확정된 SQL을 실DB에서 실행해 대상 고객 수를 집계합니다.",
        ),
    ]

    # 3단계 배지를 실제 parser 에 맞춰 정직하게: 규칙 파싱이면 혼합/모델/프롬프트 배지를 규칙으로 정정한다
    # (query_parser=rules 기본이면 LLM 질의계획이 호출되지 않아 모델·프롬프트가 실제로 안 쓰인다).
    if (query_plan.get("parser") or {}).get("type") != "llm":
        stage3 = stages[2]
        stage3["method"] = "규칙"
        stage3["refs"] = [ref for ref in stage3.get("refs", []) if ref["kind"] not in ("모델", "프롬프트")]

    _mark_trace_refs_used(stages, result)

    failure_diagnosis = _trace_sql_failure_diagnosis(query_plan, sql_result)
    return {
        "query": result.get("query"),
        "collection": result.get("collection"),
        "retrieval_scope": result.get("retrieval_scope"),
        "prompt_scopes": prompt_scopes,
        "stages": stages,
        "stage_log": result.get("stage_log", []),
        "failure_diagnosis": failure_diagnosis,
        "result": {
            "status": api_response.get("status"),
            "sql": api_response.get("sql"),
            "blocked_sql": api_response.get("blocked_sql"),
            "target_connection": api_response.get("target_connection"),
            "message": api_response.get("message"),
        },
        "timings_ms": result.get("timings_ms", {}),
    }


def apply_execution_to_trace(trace: dict[str, Any], execution: dict[str, Any]) -> None:
    """execute_target_sql 결과를 트레이스 10단계(실행·결과)에 반영한다(엔드포인트에서 호출)."""
    targeting_result = execution.get("targeting_result", {}) or {}
    count = targeting_result.get("target_customer_count")
    ok = execution.get("is_success")
    # 실행할 SQL 이 없어(미지원/needs_clarification 로 sql=None) 또는 요청이 실행을 끈 경우는 '실패' 가
    # 아니라 '생략(skipped)' 이다 — 앞 단계에서 이미 막혀 실행에 도달하지 않은 것을 빨간 '실행 실패' 로
    # 표시하면 오해를 준다(mode='skipped').
    skipped = execution.get("mode") == "skipped"
    for stage in trace.get("stages", []):
        if stage.get("step") != 10:
            continue
        stage["status"] = "ok" if ok else ("skipped" if skipped else "fail")
        stage["summary"] = (
            f"대상 {count:,}명" if isinstance(count, int)
            else ("실행 성공" if ok else ("실행 생략(생성된 SQL 없음)" if skipped else "실행 실패"))
        )
        if not skipped:
            for ref in stage.get("refs", []):
                ref["used"] = True
        if not ok and not skipped:
            trace["failure_diagnosis"] = _trace_execution_failure_diagnosis(execution)
        details = []
        if targeting_result.get("result_row_count") is not None:
            details.append(f"result_row_count={targeting_result.get('result_row_count')}")
        if targeting_result.get("target_campaign_count") is not None:
            details.append(f"target_campaign_count={targeting_result.get('target_campaign_count')}")
        if execution.get("cardinality_diagnostic"):
            details.append(f"cardinality={_trace_line(execution.get('cardinality_diagnostic'))}")
        if execution.get("error"):
            details.append(f"error={_trace_line(execution.get('error'))}")
        if details:
            stage["details"] = details
        break


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
