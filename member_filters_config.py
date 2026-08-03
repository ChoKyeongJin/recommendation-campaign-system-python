"""회원 타겟 레지스트리에서 **여러 모듈이 공유해야 하는 값**만 꺼내는 순수 접근자.

배경: ``order_count_targets.behaviors`` 의 키 집합(주문 횟수 행동)을 세 소비자가 각자 다른 방법으로
얻고 있었다 — graph_rag 는 설정 JSON 을 주입하고, confidence 와 canonical_targeting 은
targeting_ir 의 코드 기본값 폴백을 썼다. 그래서 설정에 행동을 하나 추가하면 같은 조건이
graph_rag 에서는 ``order_count_behavior`` 로, 나머지 둘에서는 ``unclassified_behavior`` 로 분류돼
신뢰도 리포트와 레거시 조건 트리에서 조용히 빠졌다. 값이 세 곳에 이중 소유돼 있던 것이다.

이 모듈은 그 값을 한 곳에서 읽는다. graph_rag 를 import 하지 않으므로(순수 모듈) confidence
같은 하위 계층도 순환 없이 쓸 수 있고, targeting_ir 은 여전히 설정을 모른다
(호출자가 주입하는 규약 유지). (당시 세 번째 소비자였던 canonical_targeting 모듈은 이후 삭제됐다.)

경로 규약은 graph_rag 와 동일하다: 환경변수 GRAPH_RAG_MEMBER_TARGET_FILTERS 로 재지정 가능,
기본값은 저장소의 docs/data/runtime/sql/member_target_filters.json. 기본 경로는 **모듈 기준 절대경로**다 —
상대경로면 cwd 가 바뀌는 순간 조용히 빈 설정으로 강등되기 때문이다.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = Path(
    os.getenv("GRAPH_RAG_MEMBER_TARGET_FILTERS")
    or (_REPO_ROOT / "docs" / "data" / "runtime" / "sql" / "member_target_filters.json")
)


def _load(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@lru_cache(maxsize=4)
def _behaviors_cached(path_text: str) -> frozenset[str]:
    section = _load(Path(path_text)).get("order_count_targets") or {}
    behaviors = section.get("behaviors") if isinstance(section, dict) else None
    return frozenset(behaviors) if isinstance(behaviors, dict) else frozenset()


def order_count_behaviors(path: Path | None = None) -> frozenset[str]:
    """주문 횟수 행동의 캐노니컬 키 집합(설정이 단일 소스).

    설정을 못 읽으면 **빈 집합**을 돌려준다 — 코드 기본값으로 조용히 대체하지 않는다. 폴백이
    있으면 설정 파손이 '조금 다른 분류'로 나타나 눈에 띄지 않기 때문이다. 호출자는 빈 집합을
    보고 자기 정책(폴백/차단)을 정한다.
    """
    return _behaviors_cached(str(Path(path) if path is not None else DEFAULT_PATH))


def behavior_spec(behavior: str, path: Path | None = None) -> dict[str, Any] | None:
    """행동 하나의 설정 선언(없으면 None)."""
    section = _load(path).get("order_count_targets") or {}
    behaviors = section.get("behaviors") if isinstance(section, dict) else None
    if not isinstance(behaviors, dict):
        return None
    spec = behaviors.get(behavior)
    return spec if isinstance(spec, dict) else None


@lru_cache(maxsize=4)
def _aggregate_metrics_cached(path_text: str) -> tuple[tuple[str, str], ...]:
    """(metric_id, spec_json) 튜플 — lru_cache 는 해시 가능 값만 담으므로 직렬화해 캐시한다."""
    section = _load(Path(path_text)).get("aggregate_targets") or {}
    metrics = section.get("metrics") if isinstance(section, dict) else None
    if not isinstance(metrics, dict):
        return ()
    return tuple(
        (metric_id, json.dumps(spec, ensure_ascii=False, sort_keys=True))
        for metric_id, spec in metrics.items()
        if isinstance(metric_id, str) and isinstance(spec, dict)
    )


def aggregate_metrics(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """집계 지표 스펙(aggregate_targets.metrics)의 metric_id → 선언 사본.

    concept_catalog 가 공통 조건 개념을 파생하는 소스다 — 지표·동의어를 카탈로그에 다시
    나열하지 않는다(이중 소유 금지). 설정을 못 읽으면 빈 dict(폴백 없음, order_count_behaviors
    와 같은 정책)."""
    target = str(Path(path) if path is not None else DEFAULT_PATH)
    return {metric_id: json.loads(spec) for metric_id, spec in _aggregate_metrics_cached(target)}


def campaign_response_targets(path: Path | None = None) -> dict[str, Any]:
    """캠페인 반응 집계 SQL 자산 선언의 방어적 사본.

    SemanticPlan 생산자는 이 접근자로 물리 집계·대상군·구매반응 조건이 모두
    존재하는지 확인한다. 설정을 읽지 못하거나 섹션이 불완전하면 빈 dict를 돌려
    주고, 호출자는 합성을 중단한다. 코드 기본값으로 조용히 메우지 않는다.
    """

    section = _load(path).get("campaign_response_targets")
    return section if isinstance(section, dict) else {}


@lru_cache(maxsize=4)
def _eq_filters_cached(path_text: str) -> str:
    entries = _load(Path(path_text)).get("eq_filters")
    return json.dumps(entries if isinstance(entries, list) else [], ensure_ascii=False)


def eq_filters(path: Path | None = None) -> list[dict[str, Any]]:
    """회원 속성 값 사전(eq_filters)의 사본.

    등급 서열·상태 값 어휘의 **단일 소유자**다. 같은 낱말(VIP/골드/휴면…)이 여러 모듈의
    정규식에 재등장하던 이중 소유를 여기서 파생으로 대체한다 — 값이 늘면 설정 한 줄로
    모든 소비자가 함께 열린다. 설정을 못 읽으면 빈 목록(폴백 없음).
    """
    target = str(Path(path) if path is not None else DEFAULT_PATH)
    return [entry for entry in json.loads(_eq_filters_cached(target)) if isinstance(entry, dict)]


def eq_filter_values(category: str, path: Path | None = None) -> dict[str, dict[str, Any]]:
    """한 범주(grade/state/gender…)의 canonical → {value, rank, synonyms}."""
    values: dict[str, dict[str, Any]] = {}
    for entry in eq_filters(path):
        if str(entry.get("category") or "") != category:
            continue
        canonical = str(entry.get("canonical") or "")
        if not canonical:
            continue
        values[canonical] = {
            "value": entry.get("value"),
            "rank": entry.get("rank"),
            "synonyms": [str(term) for term in entry.get("synonyms") or [] if str(term).strip()],
        }
    return values


def order_count_rule_supported(rule: Any) -> bool:
    """주문 횟수 행동 규칙이 컴파일 가능한 완전한 선언인가.

    ``_supported: false`` 선언(lapsed_buyer)이나 operator/count 도 anti_join 도 없는 불완전 규칙을
    ``.get(..., "=")/.get(..., 1)`` 폴백으로 조용히 '첫 구매'로 컴파일하던 잠복 결함의 차단 술어다.
    빌더는 이 판정이 거짓인 행동을 선택하지 않는다(fail-close — 미지원 행동은 부분추출 고지로 남는다).
    """
    if not isinstance(rule, dict) or rule.get("_supported") is False:
        return False
    if rule.get("anti_join") is True:
        return True
    return isinstance(rule.get("operator"), str) and isinstance(rule.get("count"), int)


def behavior_aggregate_equivalents(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """집계 지표와 등가인 행동 선언(behavior → {metric_id, operator, count}).

    설정의 ``behaviors.*.metric_id`` 가 단일 소스다(코드에 behavior→metric 매핑을 박으면 이중 소유
    재발). anti_join(부재 조건)·미지원(_supported:false)·불완전 규칙은 등가가 아니다 — 부재 조건을
    집계 임계값으로 오인해 강등하면 극성이 반전되므로 여기서부터 제외한다.
    """
    section = _load(path).get("order_count_targets") or {}
    behaviors = section.get("behaviors") if isinstance(section, dict) else None
    if not isinstance(behaviors, dict):
        return {}
    equivalents: dict[str, dict[str, Any]] = {}
    for behavior, rule in behaviors.items():
        if not order_count_rule_supported(rule) or rule.get("anti_join") is True:
            continue
        metric_id = rule.get("metric_id")
        if isinstance(metric_id, str) and metric_id:
            equivalents[behavior] = {
                "metric_id": metric_id,
                "operator": rule["operator"],
                "count": rule["count"],
            }
    return equivalents


def clear_cache() -> None:
    """설정 파일을 바꿔 끼우는 테스트용."""
    _behaviors_cached.cache_clear()
    _aggregate_metrics_cached.cache_clear()
    _eq_filters_cached.cache_clear()


__all__ = [
    "DEFAULT_PATH",
    "aggregate_metrics",
    "behavior_aggregate_equivalents",
    "behavior_spec",
    "campaign_response_targets",
    "clear_cache",
    "eq_filter_values",
    "eq_filters",
    "order_count_behaviors",
    "order_count_rule_supported",
]


# ── 실회원 타겟 속성 레지스트리의 **코드 기본값 미러** ───────────────────────────────
#
# 단일 출처는 docs/data/runtime/sql/member_target_filters.json 이다. 아래 CODE_DEFAULTS 는
# 파일 부재/파손 시 폴백이자 스키마 예시이며, 로더가 JSON 의 전 최상위 키를 이 위에 덮는다.
#
# 왜 graph_rag 가 아니라 여기 있는가(2026-08-02 이관): 예전에는 이 미러가 graph_rag 안에 있었고,
# 그 결과 물리 이름이 **세 곳**에 살았다 — JSON, 이 미러, 그리고 접근자 안의 인라인
# `config.get(key, "LITERAL")` 기본값. 셋째 층은 미러가 키를 선언하는 한 죽은 코드지만,
# 미러가 키를 빠뜨리는 순간 조용히 되살아나 **구DB 이름으로 컴파일**된다("SQL 은 성공하는데 0명").
# 미러를 순수 설정 모듈로 옮기고 인라인 기본값을 걷어내면 남는 층은 둘(JSON + 미러)이고,
# 미러의 키 누락은 tests/test_member_filters_defaults_drift.py 가 잡는다.
# ── 실회원(CRM_MB_BASEINFO) 타겟 속성 레지스트리 ──────────────────────────────
# recommend_campaign 의 타겟을 데모 스키마(users/campaigns) 대신 실회원 테이블로 추출하기 위한
# "조건 -> 실컬럼 술어" 매핑의 단일 출처는 docs/data/runtime/sql/member_target_filters.json 이다. 새 속성/값
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
CODE_DEFAULTS: dict[str, Any] = {
    # 회원 기준 엔터티. JSON 에는 있었으나 미러에 없어서, graph_rag 접근자의 인라인 기본값
    # (`or "CRM_MB_BASEINFO"` 등)이 실제로 살아 있던 키다 — 선언해야 그 층이 죽는다.
    "base_entity": {
        "table": "CRM_MB_BASEINFO",
        "alias": "B",
        "member_key": "MEMBER_NO",
        "login_id_key": "MEMBER_ID",
        "age_column": "AGE",
        "date_format": "yyyyMMdd",
    },
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
    # LLM 어휘 별칭 → 컴파일 가능한 canonical. 플래너 허용 어휘가 컴파일러 매핑보다 넓어 생기는
    # '미지원 조건' 차단을 흡수한다(새 별칭은 여기 한 줄 추가로 열린다).
    "lifecycle_aliases": {
        "withdrawn_user": "withdrawn",
        "dormant_user": "dormant",
        "active_user": "active_member",
        "inactive_user": "inactive_90d",
    },
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
    # 장바구니 기간은 의미별로 분리한다. 최근 담기/생성 창은 created_date_column(INS_DT),
    # N일 이상 보관·방치 창은 registered_date_column(UPD_DT)을 쓴다. 모든 카트 빌더가 이 설정을 공유한다.
    "cart_targets": {
        "table": "ODS_MALL_OMS_CART",
        "created_date_column": "C.INS_DT",
        "registered_date_column": "C.UPD_DT",
        "recent_default_days": 30,
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
        # 지표 보정(adjustment): '반품금액 차감/환불 제외'처럼 **집계식의 구성 컬럼을 치환**하는 선언형 수정자.
        # 트리거 정규식이 프롬프트에 걸리면 그 컬럼을 쓰는 모든 지표(누적 금액·평균 주문 금액·할인 금액 …)에
        # 일괄 적용된다 — 지표마다 '반품 차감 버전'을 복제하지 않는다. 새 보정은 여기 항목 하나 추가로 열린다.
        "adjustments": {
            "net_of_returns": {
                "ko_label": "반품 차감",
                # 공백 제거 텍스트 기준. '반품금액이 있는 경우 총결제금액에서 차감'처럼 어구가 떨어져 있어도
                # 같은 절(마침표/쉼표 이내) 안의 근접 표현이면 잡는다.
                "trigger_patterns": [
                    r"(?:반품|환불|취소)[^.。!?\n,]{0,40}?(?:차감|제외|뺀|빼고|제하)",
                    r"(?:순|실)(?:결제|구매|매출)\s*금액",
                ],
                "applies_to_semantic_types": ["sum", "ratio"],
                "column_expressions": {
                    "PAYMENT_AMT": "(COALESCE({t}PAYMENT_AMT, 0) - COALESCE({t}RETURN_AMT, 0))",
                },
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
            "member_column": "MBR_NO",
            "member_join": {"left": "TRY_CAST(M.MBR_NO AS BIGINT)", "right": "B.MEMBER_NO"},
            "campaign_join": {
                "table": "Z_CAMPAIGN",
                "alias": "ZC",
                "conditions": ["ZC.CAMP_ID = M.CAMP_ID", "ZC.CAMP_EXEC_NO = M.CAMP_EXEC_NO"],
            },
            "target_group_condition": {"column": "M.CELL_TYPE_CD", "value": "T"},
            "valid_campaign_condition": {"expression": "ISNULL(ZC.CANCEL_YN, 'N') = 'N'"},
            "campaign_date_column": "CAMP_SDATE",
            "campaign_key_expression": "CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO)",
        },
        # 횟수 지표는 이벤트와 물리 소스를 분리한다. 새 캠페인 이벤트는 이 레지스트리에 소스·술어·
        # 동의어를 추가하면 동일한 회원별 COUNT(DISTINCT 캠페인 실행회차) 빌더를 재사용한다.
        "frequency_events": {
            "campaign_response": {
                "source": "response_fact",
                "predicate": "(R.OFFR_RSPN_YN = 'Y' OR R.BUY_RSPN_YN = 'Y')",
                "synonyms": ["캠페인 반응", "반응"],
            },
            "campaign_contact": {
                "source": "contact_member_list",
                "predicate": "M.CONTAC_SUCC_YN = 'Y'",
                "synonyms": ["발송 성공", "전송 성공", "접촉 성공", "도달 성공", "메시지", "문자", "알림", "수신", "받"],
            },
            "offer_response": {
                "source": "response_fact",
                "predicate": "R.OFFR_RSPN_YN = 'Y'",
                "synonyms": ["오퍼 반응", "혜택 반응", "쿠폰 반응"],
            },
            "buy_response": {
                "source": "response_fact",
                "predicate": "R.BUY_RSPN_YN = 'Y'",
                "synonyms": ["구매 반응", "구매반응", "구매 전환"],
            },
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
    # M 지표/캠페인멤버, M2/CELL 셀 집계, S 보조속성, ZC, EO/EC 조건 IR 서브쿼리)를 담는다. 숫자 접미
    # (EO1/EO2)는 시간 관계처럼 같은 테이블을 중첩 참조하는 조건이 쓰는 기준/대상 별칭이다 —
    # 목록에 없는 별칭이 SQL 에 나타나면 후보를 거부한다.
    "validation": {
        "allowed_table_aliases": [
            "A", "B", "C", "CELL", "CP", "D", "EC", "EC1", "EC2", "EO", "EO1", "EO2",
            "M", "M2", "O", "OD", "OH", "P", "R", "S", "ZC",
        ],
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
    # supported_condition_hint 는 여기 기본값을 두지 않는다 — JSON 큐레이션 문구가 1차 소스이고,
    # 키가 없으면 라벨 레지스트리 파생 폴백(_derived_supported_condition_hint)을 쓴다(스테일 손 목록 금지).
    "region_density": {
        "granularity_tokens": ["동네", "지역", "시군구", "시도", "구"],
        "granularity_columns": {"시도": "SIDO"},
        "default_column": "SIGUNGU",
        "default_top_n": 5,
        "max_top_n": 30,
    },
    "member_metric_ranking": {
        "granularity_tokens": ["고객님", "구매자", "사용자", "고객", "회원", "유저", "손님", "사람"],
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
