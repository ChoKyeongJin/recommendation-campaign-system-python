from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from aggregation_requirements import aggregation_request_json_schema
from entity_set import derived_set_ast_error
import targeting_ir

from .semantic_ir import (
    SEMANTIC_IR_LLM_JSON_SCHEMA,
    empty_semantic_ir,
    extract_literal_bindings,
    validate_semantic_ir,
)


CAMPAIGN_QUERY_PLAN_V4_VERSION = "4.0"
QUERY_IDENTITY_DIGEST_KEY = "query_identity_digest"
CAMPAIGN_INTENTS = {
    "recommend_campaign",
    "find_user_segment",
    "analyze_aggregation",
    "unknown",
}


class CampaignQueryPlanValidationError(ValueError):
    """Raised when the shared campaign planning/execution IR is malformed."""


class CampaignQueryPlanV4(dict[str, Any]):
    """The single mutable IR shared by LLM structuring and SQL execution.

    It deliberately remains a ``dict`` subtype because the existing compiler
    enriches plans in place.  The type gives that payload a versioned contract
    without introducing a second DTO and a lossy front-to-back conversion.
    On top of the execution contract, every accepted value carries source
    evidence and anything the model cannot express is returned as an
    unresolved item instead of being guessed.
    """

    @property
    def schema_version(self) -> str:
        return str(self["schema_version"])

    @property
    def original_query(self) -> str:
        return str(self["original_query"])

    @property
    def raw_query(self) -> str:
        return str(self["raw_query"])

    @property
    def planning_query(self) -> str:
        return str(self["planning_query"])

    @property
    def normalized_query(self) -> str:
        return str(self["normalized_query"])

    @property
    def intent(self) -> str:
        return str(self["intent"])

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self))


def campaign_query_identity_digest(payload: dict[str, Any]) -> str:
    """원문/타겟 원문/실제 파싱문의 결합 무결성 해시."""
    identity = {
        "raw_query": payload.get("raw_query"),
        "original_query": payload.get("original_query"),
        "planning_query": payload.get("planning_query"),
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def verify_campaign_query_identity(payload: dict[str, Any]) -> bool:
    expected = payload.get(QUERY_IDENTITY_DIGEST_KEY)
    actual = campaign_query_identity_digest(payload)
    if not isinstance(expected, str) or expected != actual:
        raise CampaignQueryPlanValidationError("query identity changed after initial capture")
    return True


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "null":
        return schema
    if isinstance(schema.get("type"), list) and "null" in schema["type"]:
        return schema
    if "anyOf" in schema and any(
        isinstance(item, dict) and item.get("type") == "null"
        for item in schema["anyOf"]
    ):
        return schema
    return {"anyOf": [schema, {"type": "null"}]}


def _slot_schema(name: str) -> dict[str, Any]:
    """targeting_ir.SLOT_SHAPES 가 소유한 슬롯 스키마 조각(설명+properties)을 그대로 노출한다.

    V4 병합 때 이 배선이 빠져 슬롯이 설명 없는 불투명 object 로 노출됐고, strict 변환이
    properties 없는 object 를 빈 닫힌 객체로 만들어 LLM 이 카트/창 슬롯을 아예 표현할 수
    없었다(장바구니 이탈 질의가 entity_set_condition 으로 오모델링되던 원인)."""
    return copy.deepcopy(targeting_ir.SLOT_SHAPES[name].schema)


def _aggregation_request_llm_schema() -> dict[str, Any]:
    """aggregation_requirements 소유 집계 계약을 strict tool 호환형으로 변환해 노출한다.

    변환 규칙(계약 자체는 aggregation_request_json_schema 가 소유하고, 여기서는 strict
    함수 호출이 못 삼키는 형태만 고친다):
      - 무타입 자유값 스키마({"description": ...})는 스칼라/문자배열 union 으로 명시한다.
      - businessRules/comparison 자유형 dict 는 노출하지 않는다(파서 기본값이 소유).
      - aggregations[].condition 의 '필터와 같은 구조' 서술형 object 는 실제 필터 스키마로 치환한다.
      - table/column 필수는 해제한다 — V4 계약상 모델은 물리 스키마를 생성하지 않으며,
        파서(_field_ref)도 논리 entity/field 만으로 항목을 보존한다.
    """
    schema = copy.deepcopy(aggregation_request_json_schema())
    for key in ("businessRules", "comparison"):
        schema["properties"].pop(key, None)
    schema["required"] = [key for key in schema.get("required", []) if key in schema["properties"]]
    filter_item = copy.deepcopy(schema["properties"]["filters"]["items"])

    value_union: dict[str, Any] = {
        "anyOf": [
            {"type": "string"},
            {"type": "number"},
            {"type": "boolean"},
            {"type": "array", "items": {"type": "string"}},
            {"type": "null"},
        ]
    }

    def scrub(node: Any) -> None:
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            for key, sub in list(properties.items()):
                if not isinstance(sub, dict):
                    continue
                if key == "condition" and sub.get("type") == "object" and not sub.get("properties"):
                    replacement = copy.deepcopy(filter_item)
                    if sub.get("description"):
                        replacement["description"] = sub["description"]
                    properties[key] = replacement
                    scrub(replacement)
                    continue
                if not (sub.keys() & {"type", "anyOf", "enum", "$ref"}):
                    properties[key] = {**value_union, "description": sub.get("description", "비교값")}
                    continue
                scrub(sub)
        if isinstance(node.get("items"), dict):
            scrub(node["items"])
        required = node.get("required")
        if isinstance(required, list):
            node["required"] = [key for key in required if key not in {"table", "column"}]

    scrub(schema)
    return schema


# ── target_user 노출면: SLOT_SHAPES 파생 ────────────────────────────────────────
# 앱 소유 속성(SLOT_SHAPES 밖). coarse 축(성별/연령/행동 등)과 파생 집합 조건은 구조화기 계약이
# 직접 소유한다 — 아래 순서 튜플에서 여기 없는 이름은 전부 targeting_ir.SLOT_SHAPES 파생이다.
_APP_OWNED_TARGET_USER_PROPERTIES: dict[str, Any] = {
    "gender": _nullable({"type": "string"}),
    "age_min": _nullable({"type": "integer"}),
    "age_max": _nullable({"type": "integer"}),
    "age_exclude_ranges": {
        "type": "array",
        "description": (
            "제외 연령 구간 목록. 각 항목은 [최소나이, 최대나이] 정수 2개 배열(경계 포함). "
            "예: '30대 제외' → [[30, 39]], '25~35세 제외' → [[25, 35]]."
        ),
        "items": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 120}},
    },
    "lifecycle": {"type": "array", "items": {"type": "string"}},
    "interests": {"type": "array", "items": {"type": "string"}},
    "preferred_channels": {"type": "array", "items": {"type": "string"}},
    "behaviors": {
        "type": "array",
        "description": (
            "행동 canonical 목록([Allowed Canonical Values].behaviors 값만 사용). "
            "장바구니에 담았지만 구매/결제하지 않은 이탈 고객은 'cart_abandoner' 하나로 표현한다."
        ),
        "items": {"type": "string"},
    },
    "price_sensitivity": _nullable({"type": "string"}),
    "entity_set_condition": _nullable({
        "type": "object",
        "description": (
            "집계 → 랭킹 → 회원 집합으로 구성된 파생 집합 조건. "
            "'상위/가장 많이 팔린 N개 중 M개 구매'처럼 랭킹 집합이 명시된 요청 전용이다 — "
            "장바구니 이탈·미구매 같은 단순 행동 조건을 이 슬롯으로 우회 표현하지 않는다."
        ),
        "properties": {
            "derived_set_ast": {"$ref": "#/$defs/derivedSetMemberNode"},
        },
    }),
}

# 노출 순서 동결 — 기존 노출분의 프롬프트 바이트 보존 장치이지 두 번째 권위가 아니다.
# 새 target_user 슬롯은 이 튜플에 추가하지 않는다: SLOT_SHAPES 등록만으로 뒤에 자동 편입된다.
# 스테일 이름(SLOT_SHAPES 에도 앱 소유에도 없음)은 모듈 임포트 시점 KeyError 로 즉시 드러난다.
_TARGET_USER_EXPOSURE_ORDER: tuple[str, ...] = (
    "gender", "age_min", "age_max", "age_exclude_ranges", "lifecycle", "interests",
    "preferred_channels", "behaviors", "purchase_object", "purchase_date", "price_sensitivity",
    "inactivity_period", "recent_login", "purchase_inactivity", "birthday_target", "signup_target",
    "aggregate_conditions", "balance_conditions", "profile_date_conditions", "campaign_responses",
    "campaign_response_frequency", "campaign_buy_amount", "campaign_buy_count", "cart_retention",
    "cart_type", "cart_aggregate", "cart_absence", "cell_rate_target", "metric_trend",
    "entity_set_condition",
)

# 역사적으로 nullable 래핑 없이 노출된 배열 슬롯(빈 배열이 '표현 안 함'을 대신한다).
# 신규 슬롯은 타입과 무관하게 _nullable 이 기본이다 — strict 모드에서 무표현을 null 로 명시한다.
_BARE_ARRAY_SLOTS: frozenset[str] = frozenset({
    "aggregate_conditions", "balance_conditions", "profile_date_conditions", "campaign_responses",
})


# plan 컨테이너 슬롯의 LLM 노출 제외 + 사유. 새 plan 슬롯은 노출하거나 여기 사유와 함께
# 등재해야 한다 — 계약 테스트가 '노출 ∨ 선언된 제외' 전수를 강제한다(조용한 미노출 금지).
_PLAN_SLOT_EXPOSURE_EXCLUSIONS: dict[str, str] = {
    "region_density_target": (
        "properties 없는 조각이라 strict 에서 표현 불가 — 노출하려면 targeting_ir.SLOT_SHAPES "
        "조각에 properties 를 먼저 선언해야 한다."
    ),
    "purchase_count_ranking": (
        "properties 없는 조각이라 strict 에서 표현 불가 — 노출하려면 targeting_ir.SLOT_SHAPES "
        "조각에 properties 를 먼저 선언해야 한다."
    ),
}


def _target_user_slot_names() -> tuple[str, ...]:
    return tuple(
        name for name, shape in targeting_ir.SLOT_SHAPES.items()
        if shape.container == "target_user"
    )


def _target_user_properties() -> dict[str, Any]:
    """target_user 노출면을 SLOT_SHAPES 에서 파생한다(손 나열 금지).

    새 슬롯 추가 = SLOT_SHAPES 한 항목 — 이 모듈은 편집하지 않는다. 과거에는 슬롯 목록이 여기
    리터럴로 중복돼, 컴파일러가 있어도 스키마에 빠진 슬롯은 LLM 이 표현할 수 없었다(감사에서
    확인된 '지원되는데 미방출' 계열의 구조적 원인 하나)."""
    shaped = set(_target_user_slot_names())
    properties: dict[str, Any] = {}
    for name in _TARGET_USER_EXPOSURE_ORDER:
        if name in shaped:
            schema = _slot_schema(name)
            properties[name] = schema if name in _BARE_ARRAY_SLOTS else _nullable(schema)
        else:
            properties[name] = copy.deepcopy(_APP_OWNED_TARGET_USER_PROPERTIES[name])
    for name in _target_user_slot_names():
        if name not in properties:
            properties[name] = _nullable(_slot_schema(name))
    return properties


_TARGET_USER_SCHEMA: dict[str, Any] = {
    "type": "object",
    # Targeting slots are registry-driven and grow independently.  Known core
    # properties document the stable contract while extensions remain valid.
    "additionalProperties": True,
    "properties": _target_user_properties(),
}


_SOURCE_REQUIREMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "플래너가 최초 파싱 직후 봉인하는 원문 요구 스냅샷. LLM은 생성하지 않는다.",
    "required": ["id", "path", "polarity", "source", "source_text", "source_span", "value"],
    "properties": {
        "id": {"type": "string"},
        "path": _nullable({"type": "string"}),
        "polarity": {"type": "string", "enum": ["positive", "negative"]},
        "source": {"type": "string"},
        "source_text": {"type": "string"},
        "source_span": {
            "type": "object",
            "required": ["start", "end"],
            "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}},
        },
        "value": {},
    },
}


_EXTERNAL_CONDITION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id", "domain", "condition_type", "condition_code", "state",
        "target_basis", "resolution_status",
    ],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "domain": {"type": "string", "minLength": 1},
        "condition_type": {"type": "string", "minLength": 1},
        "condition_code": {"type": "string", "minLength": 1},
        "state": {"type": "string", "minLength": 1},
        "target_basis": {
            "type": "object",
            "additionalProperties": False,
            "required": ["entity", "attribute"],
            "properties": {
                "entity": {"type": "string", "minLength": 1},
                "attribute": {"type": "string", "minLength": 1},
            },
        },
        "resolution_status": {
            "type": "string",
            "enum": ["pending", "resolved", "empty", "failed", "unsupported"],
        },
        "freshness_requirement": {
            "type": "string",
            "enum": [
                "unspecified",
                "live",
                "general_knowledge_non_realtime",
            ],
        },
        "source_text": {"type": "string"},
        "source_span": {
            "type": "object",
            "additionalProperties": False,
            "required": ["start", "end"],
            "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}},
        },
    },
}


_EVIDENCE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "text", "start", "end", "confidence"],
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "text": {"type": "string", "minLength": 1},
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 0},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

_UNRESOLVED_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "reason", "evidence"],
    "properties": {
        "path": {"type": ["string", "null"]},
        "reason": {"type": "string", "minLength": 1},
        "evidence": {"type": "string"},
    },
}


CAMPAIGN_QUERY_PLAN_V4_JSON_SCHEMA: dict[str, Any] = {
    "$id": "campaign-query-plan-v4",
    "type": "object",
    "additionalProperties": True,
    "$defs": {
        "derivedSetDimensionFilter": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "dimension", "operator", "value"],
            "properties": {
                "type": {"const": "dimension_filter"},
                "dimension": {"type": "string", "minLength": 1},
                "operator": {"enum": ["equals", "contains"]},
                "value": {"type": "string", "minLength": 1},
            },
        },
        "derivedSetAggregationNode": {
            "type": "object",
            "required": ["type", "relation", "group_by", "measure"],
            "properties": {
                "type": {"const": "aggregation"},
                "relation": {"type": "string", "minLength": 1},
                "group_by": {"type": "string", "minLength": 1},
                "measure": {"type": "string", "minLength": 1},
                "window": {
                    "type": "object",
                    "description": (
                        "집계 창. 절대 구간은 {from,to}(둘 다 문자열), 상대 창은 {days}(양의 정수)만 "
                        "유효하다. 창이 명시되지 않았으면 window 자체를 null 로 둔다 — 빈 객체는 거부된다."
                    ),
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "days": {"type": "integer", "minimum": 1},
                        "label": {"type": "string"},
                    },
                },
                "filters": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/derivedSetDimensionFilter"},
                },
            },
        },
        # 세그먼트 집합식 AST(+ 합집합, * 교집합, - 차집합). 피연산자 canonical 은 집합식 카탈로그가
        # 소유하므로, 확실하지 않은 피연산자는 unknown_operand 로 원문을 보존한다(임의 canonical 금지).
        "setAstNode": {
            "anyOf": [
                {
                    "type": "object",
                    "description": "집합 연산 노드. op: + 합집합, * 교집합, - 차집합.",
                    "required": ["type", "op", "left", "right"],
                    "properties": {
                        "type": {"const": "set_op"},
                        "op": {"enum": ["+", "*", "-"]},
                        "left": {"$ref": "#/$defs/setAstNode"},
                        "right": {"$ref": "#/$defs/setAstNode"},
                    },
                },
                {
                    "type": "object",
                    "description": "카탈로그 canonical 피연산자(등급/지역 등).",
                    "required": ["type", "canonical"],
                    "properties": {
                        "type": {"const": "operand"},
                        "canonical": {"type": "string", "minLength": 1},
                        "label": {"type": "string"},
                        "matched_text": {"type": "string"},
                    },
                },
                {
                    "type": "object",
                    "description": "연령대 피연산자('30대' → age_min 30, age_max 39).",
                    "required": ["type", "age_min", "age_max"],
                    "properties": {
                        "type": {"const": "age_range"},
                        "canonical": {"type": "string"},
                        "label": {"type": "string"},
                        "age_min": {"type": "integer", "minimum": 0, "maximum": 120},
                        "age_max": {"type": "integer", "minimum": 0, "maximum": 120},
                        "matched_text": {"type": "string"},
                    },
                },
                {
                    "type": "object",
                    "description": "정규화하지 못한 피연산자 — 원문 표현을 그대로 보존한다.",
                    "required": ["type", "text"],
                    "properties": {
                        "type": {"const": "unknown_operand"},
                        "text": {"type": "string", "minLength": 1},
                    },
                },
            ],
        },
        # 숫자 계산식 AST. column 은 스키마에 실재하는 수치 컬럼만 허용되며 검증기가 카탈로그로 판정한다.
        "formulaAstNode": {
            "anyOf": [
                {
                    "type": "object",
                    "description": "숫자 리터럴.",
                    "required": ["type", "value"],
                    "properties": {"type": {"const": "number"}, "value": {"type": "number"}},
                },
                {
                    "type": "object",
                    "description": "수치 컬럼 참조(실제 테이블/컬럼명).",
                    "required": ["type", "table", "column"],
                    "properties": {
                        "type": {"const": "column"},
                        "table": {"type": "string", "minLength": 1},
                        "column": {"type": "string", "minLength": 1},
                    },
                },
                {
                    "type": "object",
                    "description": "이항 연산(+, -, *, / 만).",
                    "required": ["type", "op", "left", "right"],
                    "properties": {
                        "type": {"const": "binary_op"},
                        "op": {"enum": ["+", "-", "*", "/"]},
                        "left": {"$ref": "#/$defs/formulaAstNode"},
                        "right": {"$ref": "#/$defs/formulaAstNode"},
                    },
                },
            ],
        },
        "derivedSetRankingNode": {
            "type": "object",
            "required": ["type", "direction", "limit", "source"],
            "properties": {
                "type": {"const": "ranking"},
                "direction": {"enum": ["top", "bottom"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "source": {"$ref": "#/$defs/derivedSetAggregationNode"},
            },
        },
        "derivedSetMemberNode": {
            "type": "object",
            "required": ["type", "relation", "exists", "source"],
            "properties": {
                "type": {"const": "member_set"},
                "relation": {"type": "string", "minLength": 1},
                "exists": {"type": "boolean"},
                "cardinality": {
                    "type": "object",
                    "description": (
                        "랭킹 집합과 회원 행동 집합의 서로 다른 엔터티 교집합 개수. "
                        "예: 상위 3개 상품 중 정확히 2개 구매."
                    ),
                    "required": ["operator", "value"],
                    "properties": {
                        "operator": {"enum": ["=", ">", ">=", "<", "<="]},
                        "value": {"type": "integer", "minimum": 0, "maximum": 1000},
                    },
                },
                "source": {"$ref": "#/$defs/derivedSetRankingNode"},
            },
        },
    },
    "required": [
        "schema_version",
        "raw_query",
        "original_query",
        "planning_query",
        "normalized_query",
        "intent",
        "target_user",
        "exclude",
        "campaign_constraints",
        "semantic_evidence",
        "unresolved",
        "semantic_ir",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": CAMPAIGN_QUERY_PLAN_V4_VERSION},
        "raw_query": {
            "type": "string",
            "minLength": 1,
            "description": "API에 입력된 사용자 문자열 전체. 정규화하거나 절을 잘라내지 않는다.",
        },
        "original_query": {"type": "string", "minLength": 1},
        "planning_query": {
            "type": "string",
            "minLength": 1,
            "description": "정규화와 스코프 분리 후 실제 파서가 해석한 문자열.",
        },
        "normalized_query": {"type": "string", "minLength": 1},
        "intent": {"type": "string", "enum": sorted(CAMPAIGN_INTENTS)},
        "target_user": _TARGET_USER_SCHEMA,
        "exclude": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "gender": {"type": "array", "items": {"type": "string"}},
                "interests": {"type": "array", "items": {"type": "string"}},
                "lifecycle": {"type": "array", "items": {"type": "string"}},
            },
        },
        "campaign_constraints": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "category": {"type": "array", "items": {"type": "string"}},
                "objective": _nullable({"type": "string"}),
                "offer_type": _nullable({"type": "string"}),
                "channels": {"type": "array", "items": {"type": "string"}},
                "sell_object": _nullable({"type": "string"}),
            },
        },
        "aggregation_request": _nullable(_aggregation_request_llm_schema()),
        # plan 컨테이너 구조화 슬롯. 노출/제외는 _PLAN_SLOT_EXPOSURE_EXCLUSIONS 가 사유와 함께
        # 선언한다(계약 테스트가 '노출 ∨ 선언된 제외' 를 전수 강제).
        "member_metric_ranking": _nullable(_slot_schema("member_metric_ranking")),
        "condition_evaluations": {"type": "array", "items": {"type": "object"}},
        "external_conditions": {"type": "array", "items": _EXTERNAL_CONDITION_SCHEMA},
        "compound_dimension_filters": {"type": "array", "items": {"type": "object"}},
        "external_condition_results": {"type": "array", "items": {"type": "object"}},
        "external_condition_resolution": {"type": "object"},
        "set_expressions": {
            "type": "array",
            "description": (
                "세그먼트 합집합/교집합/차집합 요청. SQL 문자열이 아니라 set_ast 로만 표현하고, "
                "집합 연산이 없으면 빈 배열로 둔다."
            ),
            "items": {
                "type": "object",
                "required": ["set_ast"],
                "properties": {
                    "expression_id": {"type": "string"},
                    "ko_label": {"type": "string"},
                    "expression_text": {"type": "string"},
                    "set_ast": {"$ref": "#/$defs/setAstNode"},
                    "requires_clarification": {"type": "boolean"},
                    "clarification_question": {"type": "string"},
                },
            },
        },
        "computed_metrics": {
            "type": "array",
            "description": "숫자 지표 계산식(column/number/binary_op AST만, SQL 문자열 금지).",
            "items": {
                "type": "object",
                "required": ["formula_ast"],
                "properties": {
                    "metric_id": {"type": "string"},
                    "ko_label": {"type": "string"},
                    "formula_text": {"type": "string"},
                    "formula_ast": {"$ref": "#/$defs/formulaAstNode"},
                    "sql_behavior": {"enum": ["select", "rank", "filter"]},
                    "operator": {"enum": ["=", ">", ">=", "<", "<="]},
                    "threshold": {"type": "number"},
                    "order_by": {"enum": ["asc", "desc"]},
                    "unit": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "requires_clarification": {"type": "boolean"},
                    "clarification_question": {"type": "string"},
                },
            },
        },
        "result_limit": _nullable({"type": "integer", "minimum": 1}),
        "semantic_evidence": {"type": "array", "items": _EVIDENCE_ITEM_SCHEMA},
        "unresolved": {"type": "array", "items": _UNRESOLVED_ITEM_SCHEMA},
        "semantic_ir": copy.deepcopy(SEMANTIC_IR_LLM_JSON_SCHEMA),
        "literal_bindings": {
            "type": "array",
            "description": "원문 날짜·숫자·퍼센트 리터럴의 애플리케이션 소유 스냅샷. LLM은 생성하지 않는다.",
            "items": {"type": "object"},
        },
        "source_requirements": {"type": "array", "items": _SOURCE_REQUIREMENT_SCHEMA},
        "source_requirements_digest": {"type": "string"},
        QUERY_IDENTITY_DIGEST_KEY: {"type": "string"},
        "strict_source_coverage": {"type": "boolean"},
        "unresolved_source_conditions": {"type": "array", "items": {"type": "object"}},
    },
}


_APPLICATION_OWNED_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "raw_query",
        "original_query",
        "planning_query",
        "normalized_query",
        "literal_bindings",
        "source_requirements",
        "source_requirements_digest",
        QUERY_IDENTITY_DIGEST_KEY,
        "strict_source_coverage",
        "unresolved_source_conditions",
        "condition_evaluations",
        "compound_dimension_filters",
        "external_condition_results",
        "external_condition_resolution",
    }
)


def _strictify(schema: Any, *, required_here: bool = True) -> Any:
    """Return the fixed strict-function-calling form of a JSON schema.

    Chat Completions strict tools require every object property to be required
    and every object to reject additional properties.  Fields that used to be
    optional become required-but-nullable, keeping the semantic distinction
    without permitting an open-ended payload.
    """

    if isinstance(schema, list):
        return [_strictify(item) for item in schema]
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)

    out = {key: copy.deepcopy(value) for key, value in schema.items()}
    # OpenAI strict tools reject const/enum-only property schemas even though
    # they are valid JSON Schema.  Infer the primitive type so every property
    # has an explicit provider-compatible shape.
    if "type" not in out and "const" in out:
        const = out["const"]
        out["type"] = (
            "boolean" if isinstance(const, bool)
            else "integer" if isinstance(const, int)
            else "number" if isinstance(const, float)
            else "string" if isinstance(const, str)
            else "null" if const is None
            else "object"
        )
    if "type" not in out and isinstance(out.get("enum"), list) and out["enum"]:
        values = out["enum"]
        if all(isinstance(value, str) for value in values):
            out["type"] = "string"
        elif all(isinstance(value, bool) for value in values):
            out["type"] = "boolean"
        elif all(isinstance(value, int) and not isinstance(value, bool) for value in values):
            out["type"] = "integer"
    for keyword in ("anyOf", "oneOf", "allOf"):
        if isinstance(out.get(keyword), list):
            out[keyword] = [_strictify(item) for item in out[keyword]]
    if isinstance(out.get("items"), dict):
        out["items"] = _strictify(out["items"])
    if isinstance(out.get("$defs"), dict):
        out["$defs"] = {
            name: _strictify(value) for name, value in out["$defs"].items()
        }

    properties = out.get("properties")
    if isinstance(properties, dict):
        originally_required = set(out.get("required") or [])
        strict_properties: dict[str, Any] = {}
        for name, value in properties.items():
            strict_value = _strictify(value)
            if name not in originally_required:
                strict_value = _nullable(strict_value)
            strict_properties[name] = strict_value
        out["properties"] = strict_properties
        out["required"] = list(strict_properties)
        out["additionalProperties"] = False
    elif out.get("type") == "object":
        out["properties"] = {}
        out["required"] = []
        out["additionalProperties"] = False

    return out


# LLM 에 노출하지 않는 target_user 하위 필드. profile_date_conditions 는 논리 슬롯
# ({metric_id, state} canonical)으로 재정의되어 노출로 복귀했다 — 물리 alias/column/table 은 여전히
# LLM 이 만들지 않고 coerce 가 지표 스펙 레지스트리 매핑에서 채운다(V4 계약 유지).
_APPLICATION_OWNED_TARGET_USER_FIELDS = frozenset()


def _campaign_query_plan_v4_llm_schema() -> dict[str, Any]:
    """Tool 호출에서 모델이 결정할 필드만 strict 형태로 노출한다."""
    schema = copy.deepcopy(CAMPAIGN_QUERY_PLAN_V4_JSON_SCHEMA)
    schema.pop("$id", None)
    schema["required"] = [
        key for key in schema.get("required", []) if key not in _APPLICATION_OWNED_PLAN_FIELDS
    ]
    properties = schema.get("properties", {})
    for key in _APPLICATION_OWNED_PLAN_FIELDS:
        properties.pop(key, None)
    target_user_properties = (properties.get("target_user") or {}).get("properties", {})
    for key in _APPLICATION_OWNED_TARGET_USER_FIELDS:
        target_user_properties.pop(key, None)
    return _strictify(schema)


CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA = _campaign_query_plan_v4_llm_schema()


CAMPAIGN_QUERY_PLAN_V4_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_campaign_query_plan_v4",
        "description": (
            "사용자 원문의 캠페인/타겟팅 의미를 실행기와 공유하는 QueryPlan v4로 제출한다. "
            "SQL이나 물리 컬럼을 만들지 않고 원문 근거와 미해결 의미를 함께 반환한다."
        ),
        "strict": True,
        "parameters": CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
    },
}


def _normalize_unique_evidence_spans(payload: dict[str, Any], query: str) -> None:
    """Repair only unambiguous model offsets; never invent evidence text."""

    evidence = payload.get("semantic_evidence")
    if not isinstance(evidence, list):
        return
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        text = item["text"]
        start, end = item.get("start"), item.get("end")
        if (
            isinstance(start, int)
            and isinstance(end, int)
            and 0 <= start <= end <= len(query)
            and query[start:end] == text
        ):
            continue
        occurrences: list[int] = []
        cursor = 0
        while text and (found := query.find(text, cursor)) >= 0:
            occurrences.append(found)
            cursor = found + 1
        if len(occurrences) == 1:
            item["start"] = occurrences[0]
            item["end"] = occurrences[0] + len(text)


# 고객 목록/집계 분석 요청에서 모델이 누락으로 요구하면 안 되는 캠페인 제약 필드. 프롬프트 계약
# ("고객 리스트 요청에 사용자가 요구하지 않은 캠페인 채널·혜택·상품·목표를 추가로 요구하지 않는다")
# 의 결정론 집행이다 — 선언만으로는 위반이 반복돼 needs_clarification 이 타겟 추출을 막는다.
# 이 필드들은 타겟 계산에 쓰이지 않으므로 제거해도 조건 손실이 없다. category 는 상품 조건과
# 중의적이라 제외한다(계약 문구의 채널·혜택·판매상품·목표만).
_CAMPAIGN_CONSTRAINT_FIELD_TOKENS = frozenset({
    "objective", "offer_type", "offer", "channels", "channel", "sell_object",
    "campaign_constraints",
})


def _is_campaign_constraint_field(name: Any) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    compact = re.sub(r"[^0-9a-z]+", "_", name.strip().casefold()).strip("_")
    candidates = {compact}
    for prefix in ("campaign_constraints_", "constraints_", "campaign_"):
        if compact.startswith(prefix):
            candidates.add(compact[len(prefix):])
    return bool(candidates & _CAMPAIGN_CONSTRAINT_FIELD_TOKENS)


def _campaign_constraint_only_unresolved(item: Any) -> bool:
    """unresolved 항목이 캠페인 제약 필드만을 근거로 삼는지(그때만 계약 위반으로 제거)."""
    if not isinstance(item, dict):
        return False
    if _is_campaign_constraint_field(item.get("path")):
        return True
    evidence = item.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        tokens = [token for token in re.split(r"[,/;\s]+", evidence.strip()) if token]
        return bool(tokens) and all(_is_campaign_constraint_field(token) for token in tokens)
    return False


def _drop_campaign_constraint_requirements(payload: dict[str, Any]) -> None:
    """고객 목록/집계 요청의 semantic_ir·unresolved 에서 캠페인 제약 누락 요구를 걷어낸다.

    프롬프트가 금지한 요구인데도 모델이 반복 위반하므로 결정론으로 집행한다. 캠페인 필드 외의
    누락(예: 기간 확정)은 그대로 남고, 전부 캠페인 필드였을 때만 needs_clarification 을
    resolved 로 되돌린다 — 실제 미해결 의미를 조용히 지우는 일은 구조적으로 불가능하다."""
    if payload.get("intent") not in {"find_user_segment", "analyze_aggregation"}:
        return
    semantic_ir = payload.get("semantic_ir")
    if isinstance(semantic_ir, dict):
        missing = semantic_ir.get("missing_fields")
        if isinstance(missing, list):
            kept = [field for field in missing if not _is_campaign_constraint_field(field)]
            if len(kept) != len(missing):
                semantic_ir["missing_fields"] = kept
                if not kept and semantic_ir.get("status") == "needs_clarification":
                    semantic_ir["status"] = "resolved"
    unresolved = payload.get("unresolved")
    if isinstance(unresolved, list):
        kept_items = [item for item in unresolved if not _campaign_constraint_only_unresolved(item)]
        if len(kept_items) != len(unresolved):
            payload["unresolved"] = kept_items


def attach_campaign_query_plan_v4_identity(
    payload: Any,
    query: str,
    *,
    current_date: str | None = None,
) -> Any:
    """모델 출력에 애플리케이션이 소유하는 버전·질의 identity·리터럴을 결정론적으로 붙인다."""
    if not isinstance(payload, dict):
        return payload
    enriched = copy.deepcopy(payload)
    _normalize_unique_evidence_spans(enriched, query)
    _drop_campaign_constraint_requirements(enriched)
    enriched.update(
        {
            "schema_version": CAMPAIGN_QUERY_PLAN_V4_VERSION,
            "raw_query": query,
            "original_query": query,
            "planning_query": query,
            "normalized_query": query,
            "literal_bindings": extract_literal_bindings(
                query, current_date=current_date
            ),
        }
    )
    enriched[QUERY_IDENTITY_DIGEST_KEY] = campaign_query_identity_digest(enriched)
    return enriched


def build_campaign_query_plan_v4_fallback(
    query: str,
    *,
    current_date: str | None = None,
) -> CampaignQueryPlanV4:
    payload = attach_campaign_query_plan_v4_identity(
        {
            "intent": "unknown",
            "target_user": {},
            "exclude": {"gender": [], "interests": [], "lifecycle": []},
            "campaign_constraints": {
                "category": [],
                "objective": None,
                "offer_type": None,
                "channels": [],
                "sell_object": None,
            },
            "aggregation_request": None,
            "set_expressions": [],
            "computed_metrics": [],
            "external_conditions": [],
            "compound_dimension_filters": [],
            "result_limit": None,
            "semantic_evidence": [],
            "semantic_ir": empty_semantic_ir(
                missing_fields=["semantic_interpretation"],
                message="LLM 의미 구조화를 사용할 수 없습니다.",
            ),
            "unresolved": [
                {
                    "path": None,
                    "reason": "llm_structuring_unavailable",
                    "evidence": query,
                }
            ],
        },
        query,
        current_date=current_date,
    )
    return CampaignQueryPlanV4(payload)


def validate_campaign_query_plan_v4(
    payload: Any,
    *,
    query: str | None = None,
    raw_query: str | None = None,
    require_semantic: bool = False,
) -> CampaignQueryPlanV4:
    """실행 계약과 의미 계약을 하나의 검증으로 판정한다.

    ``require_semantic=True``는 LLM 구조화기 경로다: 의미 계층 필드
    (semantic_evidence/unresolved/semantic_ir/literal_bindings)의 존재와 원문
    일치를 강제한다. ``False``는 실행기 보강 경로다: 실행 슬롯 계약만 검증하고
    의미 필드는 보강 단계가 소유권을 이미 회수했으므로 재검증하지 않는다.
    """
    if not isinstance(payload, dict):
        raise CampaignQueryPlanValidationError("campaign QueryPlan v4 must be an object")
    version = payload.get("schema_version")
    if version != CAMPAIGN_QUERY_PLAN_V4_VERSION:
        raise CampaignQueryPlanValidationError(
            f"schema_version must be {CAMPAIGN_QUERY_PLAN_V4_VERSION}"
        )
    preserved_raw_query = _non_empty_string(payload.get("raw_query"), "raw_query")
    if raw_query is not None and preserved_raw_query != raw_query:
        raise CampaignQueryPlanValidationError("raw_query must exactly match the API input query")
    original_query = _non_empty_string(payload.get("original_query"), "original_query")
    if query is not None and original_query != query:
        raise CampaignQueryPlanValidationError("original_query must exactly match the input query")
    if original_query not in preserved_raw_query:
        raise CampaignQueryPlanValidationError("original_query must be preserved within raw_query")
    _non_empty_string(payload.get("planning_query"), "planning_query")
    _non_empty_string(payload.get("normalized_query"), "normalized_query")
    if payload.get("intent") not in CAMPAIGN_INTENTS:
        raise CampaignQueryPlanValidationError("intent is not supported by campaign QueryPlan v4")
    for key in ("target_user", "exclude", "campaign_constraints"):
        if not isinstance(payload.get(key), dict):
            raise CampaignQueryPlanValidationError(f"{key} must be an object")
    entity_set = payload["target_user"].get("entity_set_condition")
    if isinstance(entity_set, dict) and "derived_set_ast" in entity_set:
        ast_error = derived_set_ast_error(entity_set.get("derived_set_ast"))
        if ast_error:
            raise CampaignQueryPlanValidationError(
                f"target_user.entity_set_condition.derived_set_ast is invalid: {ast_error}"
            )
    for key in ("set_expressions", "computed_metrics", "external_conditions", "compound_dimension_filters"):
        value = payload.get(key, [])
        if value is None:
            # strict tool 은 이 키들을 required-but-nullable 로 노출하고, 프롬프트도 '없으면 null'을
            # 허용한다. 명시적 null 은 빈 배열과 같은 뜻이므로 정규화한다 — null 하나마다 재시도
            # 1회를 태우면 슬롯을 고칠 재시도 한도가 형식 문제로 소진된다(실측: 3회 중 2회).
            payload[key] = value = []
        if not isinstance(value, list):
            raise CampaignQueryPlanValidationError(f"{key} must be an array")
    for index, condition in enumerate(payload.get("external_conditions", [])):
        _validate_external_condition(condition, index)
    result_limit = payload.get("result_limit")
    if result_limit is not None and (
        not isinstance(result_limit, int) or isinstance(result_limit, bool) or result_limit < 1
    ):
        raise CampaignQueryPlanValidationError("result_limit must be a positive integer or null")
    source_requirements = payload.get("source_requirements")
    if source_requirements is not None:
        if not isinstance(source_requirements, list):
            raise CampaignQueryPlanValidationError("source_requirements must be an array")
        for index, requirement in enumerate(source_requirements):
            _validate_source_requirement(requirement, index)
        digest = payload.get("source_requirements_digest")
        if digest is not None:
            actual = hashlib.sha256(
                json.dumps(
                    source_requirements,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if not isinstance(digest, str) or digest != actual:
                raise CampaignQueryPlanValidationError("source_requirements digest mismatch")
    unresolved_source_conditions = payload.get("unresolved_source_conditions", [])
    if not isinstance(unresolved_source_conditions, list) or not all(
        isinstance(item, dict) for item in unresolved_source_conditions
    ):
        raise CampaignQueryPlanValidationError("unresolved_source_conditions must be an array of objects")
    strict_source_coverage = payload.get("strict_source_coverage")
    if strict_source_coverage is not None and not isinstance(strict_source_coverage, bool):
        raise CampaignQueryPlanValidationError("strict_source_coverage must be a boolean")

    if require_semantic:
        _validate_semantic_layer(payload)

    actual_identity_digest = campaign_query_identity_digest(payload)
    supplied_identity_digest = payload.get(QUERY_IDENTITY_DIGEST_KEY)
    if supplied_identity_digest is not None and supplied_identity_digest != actual_identity_digest:
        raise CampaignQueryPlanValidationError("query identity digest mismatch")
    validated = CampaignQueryPlanV4(copy.deepcopy(payload))
    validated[QUERY_IDENTITY_DIGEST_KEY] = actual_identity_digest
    return validated


def _validate_semantic_layer(payload: dict[str, Any]) -> None:
    """의미 계층 계약: 모든 채택 값에 원문 근거, 값 리터럴은 애플리케이션 소유."""

    evidence = payload.get("semantic_evidence")
    if not isinstance(evidence, list):
        raise CampaignQueryPlanValidationError("semantic_evidence must be an array")
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise CampaignQueryPlanValidationError(
                f"semantic_evidence[{index}] must be an object"
            )
        text = item.get("text")
        start, end = item.get("start"), item.get("end")
        confidence = item.get("confidence")
        if not isinstance(text, str) or text not in payload["original_query"]:
            raise CampaignQueryPlanValidationError(
                f"semantic_evidence[{index}].text must occur in original_query"
            )
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (start, end)):
            raise CampaignQueryPlanValidationError(
                f"semantic_evidence[{index}] must contain integer start/end"
            )
        if start < 0 or end < start or payload["original_query"][start:end] != text:
            raise CampaignQueryPlanValidationError(
                f"semantic_evidence[{index}] span does not match original_query"
            )
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
        ):
            raise CampaignQueryPlanValidationError(
                f"semantic_evidence[{index}].confidence must be between 0 and 1"
            )

    unresolved = payload.get("unresolved")
    if not isinstance(unresolved, list) or not all(
        isinstance(item, dict) and isinstance(item.get("reason"), str)
        for item in unresolved
    ):
        raise CampaignQueryPlanValidationError("unresolved must be an array of objects")

    literals = payload.get("literal_bindings")
    if not isinstance(literals, list):
        raise CampaignQueryPlanValidationError("literal_bindings must be an array")
    seen_literal_ids: set[str] = set()
    for index, item in enumerate(literals):
        if not isinstance(item, dict):
            raise CampaignQueryPlanValidationError(
                f"literal_bindings[{index}] must be an object"
            )
        literal_id = item.get("id")
        start, end = item.get("start"), item.get("end")
        text = item.get("text")
        if not isinstance(literal_id, str) or not literal_id or literal_id in seen_literal_ids:
            raise CampaignQueryPlanValidationError(
                f"literal_bindings[{index}].id must be unique"
            )
        seen_literal_ids.add(literal_id)
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (start, end)):
            raise CampaignQueryPlanValidationError(
                f"literal_bindings[{index}] must contain integer start/end"
            )
        if not isinstance(text, str) or start < 0 or end < start or payload["original_query"][start:end] != text:
            raise CampaignQueryPlanValidationError(
                f"literal_bindings[{index}] span does not match original_query"
            )
    try:
        validate_semantic_ir(
            payload.get("semantic_ir"), literals, payload=payload
        )
    except ValueError as exc:
        raise CampaignQueryPlanValidationError(str(exc)) from exc


def as_campaign_query_plan_v4(
    plan: dict[str, Any],
    *,
    original_query: str,
    raw_query: str | None = None,
    planning_query: str | None = None,
    normalized_query: str | None = None,
) -> CampaignQueryPlanV4:
    """Attach the v4 identity to an existing execution plan without remapping it."""

    payload = copy.deepcopy(dict(plan))
    payload["schema_version"] = CAMPAIGN_QUERY_PLAN_V4_VERSION
    payload["raw_query"] = raw_query or payload.get("raw_query") or original_query
    payload["original_query"] = original_query
    payload["planning_query"] = planning_query or payload.get("planning_query") or original_query
    payload["normalized_query"] = (
        normalized_query
        or payload.get("normalized_query")
        or (payload.get("retrieval") or {}).get("query")
        or original_query
    )
    payload.setdefault("intent", "unknown")
    payload.setdefault("target_user", {})
    payload.setdefault("exclude", {"gender": [], "interests": [], "lifecycle": []})
    payload.setdefault("campaign_constraints", {})
    payload.setdefault("set_expressions", [])
    payload.setdefault("computed_metrics", [])
    payload.setdefault("external_conditions", [])
    payload.setdefault("compound_dimension_filters", [])
    payload.setdefault("result_limit", None)
    payload.setdefault("unresolved_source_conditions", [])
    payload[QUERY_IDENTITY_DIGEST_KEY] = campaign_query_identity_digest(payload)
    return validate_campaign_query_plan_v4(
        payload,
        query=original_query,
        raw_query=payload["raw_query"],
    )


def _non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignQueryPlanValidationError(f"{path} must be a non-empty string")
    return value


def _validate_source_requirement(value: Any, index: int) -> None:
    path = f"source_requirements[{index}]"
    if not isinstance(value, dict):
        raise CampaignQueryPlanValidationError(f"{path} must be an object")
    for key in ("id", "source", "source_text"):
        _non_empty_string(value.get(key), f"{path}.{key}")
    if value.get("polarity") not in {"positive", "negative"}:
        raise CampaignQueryPlanValidationError(f"{path}.polarity is invalid")
    span = value.get("source_span")
    if not isinstance(span, dict):
        raise CampaignQueryPlanValidationError(f"{path}.source_span must be an object")
    start, end = span.get("start"), span.get("end")
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in (start, end)):
        raise CampaignQueryPlanValidationError(f"{path}.source_span must contain integer start/end")
    if start < 0 or end < start:
        raise CampaignQueryPlanValidationError(f"{path}.source_span is out of range")


def _validate_external_condition(value: Any, index: int) -> None:
    path = f"external_conditions[{index}]"
    if not isinstance(value, dict):
        raise CampaignQueryPlanValidationError(f"{path} must be an object")
    for key in ("id", "domain", "condition_type", "condition_code", "state"):
        _non_empty_string(value.get(key), f"{path}.{key}")
    target_basis = value.get("target_basis")
    if not isinstance(target_basis, dict):
        raise CampaignQueryPlanValidationError(f"{path}.target_basis must be an object")
    for key in ("entity", "attribute"):
        _non_empty_string(target_basis.get(key), f"{path}.target_basis.{key}")
    if value.get("resolution_status") not in {
        "pending", "resolved", "empty", "failed", "unsupported",
    }:
        raise CampaignQueryPlanValidationError(f"{path}.resolution_status is invalid")
    freshness = value.get("freshness_requirement")
    if freshness is not None and freshness not in {
        "unspecified", "live", "general_knowledge_non_realtime",
    }:
        raise CampaignQueryPlanValidationError(
            f"{path}.freshness_requirement is invalid"
        )
