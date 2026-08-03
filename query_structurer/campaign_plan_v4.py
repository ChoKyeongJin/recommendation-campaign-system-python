from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Any

import audience_runtime
import canonical_audience_claims
import event_compiler
import event_ir
import execution_assets
from aggregation_requirements import aggregation_request_json_schema
from entity_set import derived_set_ast_error
import semantic_plan as semantic_plan_module
import targeting_ir

from .semantic_ir import (
    SEMANTIC_IR_LLM_JSON_SCHEMA,
    empty_semantic_ir,
    extract_literal_bindings,
    validate_semantic_ir,
)


def _semantic_plan_schema() -> dict[str, Any]:
    """SemanticPlanV2 노출면(노드 선언에서 파생 — 여기에 두 번째 권위를 만들지 않는다)."""
    return semantic_plan_module.semantic_plan_json_schema()


CAMPAIGN_QUERY_PLAN_V4_VERSION = "4.0"
QUERY_IDENTITY_DIGEST_KEY = "query_identity_digest"
AUDIENCE_REQUIREMENT_KEY = "audience_requirement"
EVENT_EXPRESSION_KEY = "event_expression"
SEMANTIC_PLAN_KEY = "semantic_plan"

# LLM 노출면에 남기는 SemanticPlan 노드 타입. **Event IR 대수가 표현하지 못하는 축만** 여기 온다.
#
# 2026-08-02 canonical audience 이행에서 semantic_plan 을 LLM 노출면에서 통째로 뺐는데, 그 결과
# 등급/상태 시점·이력 조건이 **생산자를 잃었다** — `compositional_targeting` 의 as_of/transition/
# stable/changed_n_times 컴파일러는 그대로 살아 있는데, 그 입력인 `target_user.relational_operation`
# 을 만드는 유일한 경로가 `legacy_plan_compiler._compile_relation_predicate`(= relation_predicate
# 노드)였기 때문이다. 실측(2026-08-02 라이브): '이번 달 기준 골드 등급 회원'의 query_plan 은
# semantic_plan.nodes=[] + audience_requirement.issues=[unsupported_semantics] 로, 작동하는
# 컴파일러가 한 번도 호출되지 않았다.
#
# 이 목록은 **줄어드는 방향**이 목표다: Event IR 이 월별 스냅샷 축을 흡수하면 여기서 빠진다.
# 새 타입을 늘리려면 "audience_requirement 로 표현할 수 없다"는 근거가 먼저 있어야 한다.
LLM_SEMANTIC_PLAN_NODE_TYPES: tuple[str, ...] = ("relation_predicate",)
AUDIENCE_REQUIREMENT_ISSUE_CODES = frozenset({
    "missing_argument",
    "ambiguous_requirement",
    "unsupported_semantics",
    "validation_mismatch",
})
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
    "member_metric_ranking": (
        "SemanticPlanV2 RankedSet 소유 — LegacyQueryPlanCompiler 만 이 슬롯을 쓴다. "
        "LLM 에 노출하면 같은 의미를 노드와 슬롯 두 곳에서 방출하는 이중 생산자가 된다."
    ),
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


_CLAIM_CONTAINERS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("target_user", _TARGET_USER_SCHEMA),
)


def _claimable_slot_paths() -> list[str]:
    """근거 청구가 가리킬 수 있는 **슬롯 경로**(파생 — 손 목록을 두면 새 슬롯이 조용히 빠진다).

    청구는 "이 원문 구절은 내가 이 슬롯으로 표현했다"는 진술이다. 그래서 경로는 실제 슬롯이어야
    한다. 실측(2026-08-02): 경로 제약이 없어 LLM 이 `path="User Query"` 로 원문 전체를 한 번에
    청구했고, 그 청구가 coverage 검증기의 claimed_spans 가 되어 **모든 누락 검출을 무력화**했다.
    """
    paths: list[str] = []
    for container, schema in _CLAIM_CONTAINERS:
        for name in sorted((schema.get("properties") or {})):
            paths.append(f"{container}.{name}")
    return paths


_EVIDENCE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "text", "start", "end", "confidence"],
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "이 근거 구절을 표현한 **슬롯 경로 하나**. 여러 슬롯을 채웠으면 항목을 여러 개 만든다. "
                "원문 전체를 한 항목으로 청구하지 마라 — 의미 노드로 표현한 조건은 여기 쓰지 않는다"
                "(노드가 자기 source_span 을 이미 가진다)."
            ),
            "enum": _claimable_slot_paths(),
        },
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


_AUDIENCE_ISSUE_EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "start", "end"],
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 0},
    },
}

_AUDIENCE_REQUIREMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "description": (
        "오디언스 의미의 단일 입력 계약. expression은 고정 Event IR 대수이고, "
        "원문 정보가 부족하면 뜻을 축소하지 말고 issues에 기록한다."
    ),
    "required": ["expression", "issues"],
    "properties": {
        "expression": {
            "anyOf": [
                audience_runtime.audience_expression_json_schema(depth=1),
                {"type": "null"},
            ]
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code", "argument", "message", "evidence"],
                "properties": {
                    "code": {
                        "type": "string",
                        "enum": sorted(AUDIENCE_REQUIREMENT_ISSUE_CODES),
                    },
                    "argument": {"type": "string", "minLength": 1},
                    "message": {"type": "string", "minLength": 1},
                    "evidence": _AUDIENCE_ISSUE_EVIDENCE_SCHEMA,
                },
            },
        },
    },
}


CAMPAIGN_QUERY_PLAN_V4_JSON_SCHEMA: dict[str, Any] = {
    "$id": "campaign-query-plan-v4",
    "type": "object",
    "additionalProperties": True,
    "$defs": {
        # SemanticPlanV2 의미 노드(재귀 — logical_expression.children / entity_set.ranked_set).
        "semanticNode": semantic_plan_module.semantic_node_json_schema(
            node_ref="#/$defs/semanticNode"
        ),
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
        AUDIENCE_REQUIREMENT_KEY,
        "semantic_evidence",
        "unresolved",
        "semantic_ir",
        EVENT_EXPRESSION_KEY,
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
        AUDIENCE_REQUIREMENT_KEY: copy.deepcopy(_AUDIENCE_REQUIREMENT_SCHEMA),
        EVENT_EXPRESSION_KEY: {
            "type": "object",
            "description": "애플리케이션이 audience_requirement에서 검증 후 파생한 canonical 실행 IR.",
            "properties": {
                "expression": audience_runtime.audience_expression_json_schema(depth=1),
                "source": {"type": "string"},
                "receipts": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["expression", "source", "receipts"],
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
        # 의미 계층의 단일 소유자. LLM 은 여기에만 의미를 쓴다 — 실행 슬롯도, 결핍 목록도,
        # 최종 status 도 만들지 않는다(semantic_ir 은 이 노드들에서 파생되는 애플리케이션 소유물).
        "semantic_plan": _semantic_plan_schema(),
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


# LLM 이 만들지 않는 plan 필드. semantic_ir 이 여기 있는 것이 이번 이행의 핵심이다 —
# 결핍·미지원·최종 status 의 소유자를 LLM 에서 시스템(semantic_pipeline)으로 옮겼다.
# member_metric_ranking 은 SemanticPlan RankedSet 의 컴파일 산출물이라 역시 LLM 소관이 아니다.
_APPLICATION_OWNED_PLAN_FIELDS = frozenset(
    {
        "semantic_ir",
        "member_metric_ranking",
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


def _campaign_query_plan_v4_llm_schema() -> dict[str, Any]:
    """LLM 입력면: canonical audience requirement + 비오디언스 메타데이터.

    내부 호환 스키마에서 필드를 하나씩 빼는 방식은 새 슬롯이 생길 때마다 LLM 입력면이 다시
    늘어나는 구조였다. 허용 목록으로 작은 envelope를 새로 만들면 슬롯/빌더 수와 무관하게 계약
    모양이 고정된다.
    """
    internal = CAMPAIGN_QUERY_PLAN_V4_JSON_SCHEMA["properties"]
    campaign_metadata = copy.deepcopy(internal["campaign_constraints"])
    # 상품/category 범위는 오디언스 의미다. 캠페인 실행 메타데이터로 우회하지 않는다.
    campaign_metadata.get("properties", {}).pop("category", None)
    campaign_metadata["required"] = [
        key for key in campaign_metadata.get("required", []) if key != "category"
    ]
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "intent", "campaign_constraints", "result_limit", AUDIENCE_REQUIREMENT_KEY,
            SEMANTIC_PLAN_KEY,
        ],
        "properties": {
            "intent": copy.deepcopy(internal["intent"]),
            "campaign_constraints": campaign_metadata,
            "result_limit": copy.deepcopy(internal["result_limit"]),
            AUDIENCE_REQUIREMENT_KEY: copy.deepcopy(_AUDIENCE_REQUIREMENT_SCHEMA),
            SEMANTIC_PLAN_KEY: semantic_plan_module.semantic_plan_json_schema(
                node_types=LLM_SEMANTIC_PLAN_NODE_TYPES
            ),
        },
    }
    # ``#/$defs`` always resolves from the function-parameters document for the
    # provider.  A standalone audience schema may own nested definitions via
    # ``$id``, but embedding that object here leaves the provider looking for a
    # non-existent root component.  Hoist the fixed algebra exactly once.
    expression_branch = schema["properties"][AUDIENCE_REQUIREMENT_KEY]["properties"][
        "expression"
    ]["anyOf"][0]
    schema["$defs"] = expression_branch.pop("$defs")
    expression_branch.pop("$id", None)
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


def _walk_evidence_payloads(value: Any) -> list[dict[str, Any]]:
    """Canonical expression/issue tree에 들어 있는 evidence 객체 전부."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        evidence = value.get("evidence")
        if isinstance(evidence, dict):
            found.append(evidence)
        for key, child in value.items():
            if key != "evidence":
                found.extend(_walk_evidence_payloads(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_evidence_payloads(child))
    return found


def _normalize_audience_evidence(payload: dict[str, Any], query: str) -> None:
    requirement = payload.get(AUDIENCE_REQUIREMENT_KEY)
    if not isinstance(requirement, dict):
        return
    for evidence in _walk_evidence_payloads(requirement):
        text = evidence.get("text")
        if not isinstance(text, str) or not text:
            continue
        start, end = evidence.get("start"), evidence.get("end")
        if (
            isinstance(start, int) and isinstance(end, int)
            and 0 <= start <= end <= len(query) and query[start:end] == text
        ):
            continue
        positions: list[int] = []
        cursor = 0
        while (position := query.find(text, cursor)) >= 0:
            positions.append(position)
            cursor = position + 1
        if len(positions) == 1:
            evidence["start"] = positions[0]
            evidence["end"] = positions[0] + len(text)

    expression = requirement.get("expression")

    def inherit_container_evidence(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key != "evidence":
                    inherit_container_evidence(child)
            if value.get("type") != "exists" or isinstance(value.get("evidence"), dict):
                return
            spans = [
                (item.get("start"), item.get("end"))
                for item in _walk_evidence_payloads(value.get("relation"))
                if isinstance(item.get("start"), int)
                and isinstance(item.get("end"), int)
                and 0 <= item["start"] < item["end"] <= len(query)
                and query[item["start"]:item["end"]] == item.get("text")
            ]
            if spans:
                start = min(item[0] for item in spans)
                end = max(item[1] for item in spans)
                value["evidence"] = {
                    "text": query[start:end], "start": start, "end": end,
                }
        elif isinstance(value, list):
            for child in value:
                inherit_container_evidence(child)

    inherit_container_evidence(expression)


def _validated_audience_issue(item: Any, query: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise CampaignQueryPlanValidationError("audience_requirement.issues items must be objects")
    code = str(item.get("code") or "")
    argument = str(item.get("argument") or "")
    message = str(item.get("message") or "")
    evidence = item.get("evidence")
    if code not in AUDIENCE_REQUIREMENT_ISSUE_CODES:
        raise CampaignQueryPlanValidationError(f"unknown audience issue code: {code!r}")
    if not argument or not message or not isinstance(evidence, dict):
        raise CampaignQueryPlanValidationError("audience issue needs argument/message/evidence")
    text = evidence.get("text")
    start, end = evidence.get("start"), evidence.get("end")
    if not (
        isinstance(text, str) and text
        and isinstance(start, int) and not isinstance(start, bool)
        and isinstance(end, int) and not isinstance(end, bool)
        and 0 <= start < end <= len(query)
        and query[start:end] == text
    ):
        raise CampaignQueryPlanValidationError("audience issue evidence does not match original_query")
    return {
        "code": code,
        "argument": argument,
        "message": message,
        "evidence": {"text": text, "start": start, "end": end},
    }


def _audience_issue_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    """issue 하나의 신원(코드·인자·근거 구간). 생산자를 가르는 데만 쓴다."""
    evidence = item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {}
    return (str(item.get("code")), str(item.get("argument")), str(evidence.get("text")))


_INCOMPLETE_RECENCY_RE = re.compile(r"최근(?!\s*\d)")


def _as_of_date(current_date: str | None) -> date | None:
    """계획 시점 기준일. 파싱 불가면 None(컴파일러가 실행 시점으로 폴백)."""
    try:
        return date.fromisoformat(current_date) if current_date else None
    except ValueError:
        return None


def _temporal_requirement_issues(
    query: str,
    expression: event_ir.Condition,
    *,
    current_date: str | None,
) -> list[dict[str, Any]]:
    """원문 시간 한정이 IR에서 사라지거나 인자가 없으면 전체 이력 폴백을 막는다."""
    issues: list[dict[str, Any]] = []
    try:
        import event_parser  # 순환 없는 language adapter; canonical IR은 이 모듈을 import하지 않는다.

        expected = event_parser.source_time_span_count(query, today=_as_of_date(current_date))
    except (ImportError, ValueError):
        expected = 0
    actual = event_ir.count_time_constraints(expression)
    if expected > actual:
        issues.append({
            "code": "validation_mismatch",
            "argument": "period",
            "message": "원문에 있는 기간 조건이 canonical audience expression에서 누락되었습니다.",
            "evidence": {"text": query, "start": 0, "end": len(query)},
        })

    signed_atoms = list(event_ir.iter_signed_atoms(expression))
    for match in _INCOMPLETE_RECENCY_RE.finditer(query):
        covered_atom = next(
            (
                atom for atom, _negated in signed_atoms
                if atom.evidence is not None
                and atom.evidence.start <= match.start() < atom.evidence.end
            ),
            None,
        )
        if covered_atom is not None and event_ir.time_windows(covered_atom):
            continue
        issues.append({
            "code": "missing_argument",
            "argument": "period",
            "message": "'최근'의 범위를 확정할 기간 값이 필요합니다.",
            "evidence": {
                "text": match.group(0), "start": match.start(), "end": match.end(),
            },
        })
    return issues


def _dedupe_audience_issues(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        key = (
            item.get("code"), item.get("argument"),
            evidence.get("start"), evidence.get("end"),
        )
        unique.setdefault(key, item)
    return list(unique.values())


def _audience_receipts(expression: event_ir.Condition) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for index, (atom, negated) in enumerate(event_ir.iter_signed_atoms(expression)):
        semantic = atom.to_dict()
        fingerprint = hashlib.sha256(
            json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        receipts.append({
            "node_id": f"audience-atom-{index}",
            "fingerprint": fingerprint,
            "status": "compiled",
            "polarity": "negative" if negated else "positive",
            "sources": sorted(event_ir.sources(atom)),
        })
    return receipts


def _derive_audience_execution(
    payload: dict[str, Any], query: str, *, current_date: str | None
) -> bool:
    """Requirement 계약을 검증해 canonical execution IR을 단방향으로 파생한다.

    반환값은 새 계약이 존재했는지다. False면 기존 저장 플랜을 위한 SemanticPlan 호환 경로가
    이어서 처리할 수 있다. canonical→legacy 슬롯 역투영은 하지 않는다.
    """
    requirement = payload.get(AUDIENCE_REQUIREMENT_KEY)
    if not isinstance(requirement, dict):
        return False
    raw_issues = requirement.get("issues")
    if not isinstance(raw_issues, list):
        raise CampaignQueryPlanValidationError("audience_requirement.issues must be an array")
    issues = [_validated_audience_issue(item, query) for item in raw_issues]
    # 모델이 신고한 것과 애플리케이션이 계산한 것을 여기서 갈라 둔다. 아래 강등 판정은
    # **모델 신고에만** 적용된다 — 애플리케이션이 append 하는 issue 의 근거 구간은 원문
    # 전체(:1190, :1204)라, 표면어가 하나만 걸려도 event_compiler 의 권위 있는 판정까지
    # 함께 강등된다. 그러면 '조용한 오답'을 막으려던 장치가 그것을 만드는 장치가 된다.
    model_reported = {_audience_issue_key(item) for item in issues}
    raw_expression = requirement.get("expression")
    expression: event_ir.Condition | None = None
    if isinstance(raw_expression, dict):
        try:
            expression = event_ir.condition_from_dict(raw_expression)
            event_ir.validate_evidence(expression)
        except (event_ir.IrSchemaError, event_ir.SemanticLossError) as exc:
            raise CampaignQueryPlanValidationError(f"invalid audience expression: {exc}") from exc
        for atom, _negated in event_ir.iter_signed_atoms(expression):
            evidence = atom.evidence
            if evidence is None or not (
                0 <= evidence.start < evidence.end <= len(query)
                and query[evidence.start:evidence.end] == evidence.text
            ):
                raise CampaignQueryPlanValidationError(
                    "audience expression evidence does not match original_query"
                )

        catalog = audience_runtime.resolve_audience_catalog()
        unknown_sources = event_compiler.unsupported_events(
            expression, dict(catalog.compiler_events)
        )
        unknown_fields = event_compiler.unsupported_fields(
            expression,
            dict(catalog.compiler_events),
            dict(catalog.compiler_fields),
        )
        for symbol in [*unknown_sources, *unknown_fields]:
            issues.append({
                "code": "unsupported_semantics",
                "argument": symbol,
                "message": f"Semantic Catalog에 등록되지 않은 심볼입니다: {symbol}",
                "evidence": {"text": query, "start": 0, "end": len(query)},
            })
        # 기준일을 넘긴다 — 검증과 SQL 생성이 각자 date.today() 를 부르면 달 경계에서 서로 다른
        # 달을 확정할 수 있고, 월 단위 컬럼에서는 그게 곧 다른 오디언스다.
        capability = event_compiler.validate_compiler_capability(
            expression, context=catalog.compile_context(literals=True, today=_as_of_date(current_date))
        )
        if capability.status != event_compiler.CAPABILITY_SUPPORTED:
            for issue in capability.issues or ():
                issues.append({
                    "code": "unsupported_semantics",
                    "argument": str(issue.symbol or issue.code),
                    "message": "Canonical Event IR을 현재 SQL compiler가 표현하지 못합니다.",
                    "evidence": {"text": query, "start": 0, "end": len(query)},
                })
        issues.extend(
            _temporal_requirement_issues(query, expression, current_date=current_date)
        )
        literal_bindings = payload.get("literal_bindings")
        if not isinstance(literal_bindings, list):
            raise CampaignQueryPlanValidationError(
                "application-owned literal_bindings must be attached before audience validation"
            )
        issues.extend(
            canonical_audience_claims.canonical_claim_issues(
                query,
                expression,
                literal_bindings,
                audience_runtime.load_audience_catalog_config(),
            )
        )
    elif raw_expression is not None:
        raise CampaignQueryPlanValidationError(
            "audience_requirement.expression must be an object or null"
        )

    if expression is None and not issues:
        issues.append({
            "code": "missing_argument",
            "argument": "audience_expression",
            "message": "타겟 오디언스 조건을 canonical expression으로 확정하지 못했습니다.",
            "evidence": {"text": query, "start": 0, "end": len(query)},
        })
    issues = _dedupe_audience_issues(issues)
    requirement["expression"] = expression.to_dict() if expression is not None else None
    requirement["issues"] = issues

    if issues:
        payload.pop(EVENT_EXPRESSION_KEY, None)
        missing = sorted({
            f"audience.{item['argument']}"
            for item in issues if item.get("code") in {"missing_argument", "ambiguous_requirement"}
        })
        unsupported = [item for item in issues if item.get("code") == "unsupported_semantics"]
        if unsupported and not missing:
            # **미지원 선언은 가설이지 판정이 아니다.** 원문 결핍(missing_argument/
            # ambiguous_requirement)은 원문을 읽은 LLM 만 볼 수 있으므로 그대로 종결하지만,
            # "표현할 수 없다"는 실행 자산(컴파일러·카탈로그)을 아는 애플리케이션의 몫이다.
            # 실측(2026-08-02): '이번 달 기준 골드 등급 회원'이 unsupported_semantics 로
            # 종결됐는데, 그 의미를 컴파일하는 as_of 컴파일러는 살아 있었고 호출조차 되지 않았다.
            #
            # 강등의 조건은 "**선언된 실행 자산 중 이 의미를 처리하는 것이 있는가**"다.
            # 예전 조건은 "다른 생산자가 노드를 냈는가"였는데, 모델이 미지원을 선언할 때는
            # 노드를 내지 않으므로 **보호가 필요한 경우에만 정확히 발동하지 않는** 자기무력화
            # 가드였다. canonical 은 이미 자기 차례에 실패했으므로 묻는 것은 그 밖의 계층이다.
            plan_nodes = payload.get(SEMANTIC_PLAN_KEY)
            plan_nodes = plan_nodes.get("nodes") if isinstance(plan_nodes, dict) else None
            contradicted = [
                (item, execution_assets.non_canonical_assets_for_text(item["evidence"]["text"]))
                for item in unsupported
                if _audience_issue_key(item) in model_reported
            ]
            contradicted = [(item, assets) for item, assets in contradicted if assets]
            if contradicted and plan_nodes:
                payload["audience_unsupported_hypotheses"] = [
                    {"kind": item["argument"], "reason": item["message"],
                     "evidence": item["evidence"]["text"]}
                    for item in unsupported
                ]
                return False
            if contradicted:
                # 자산은 선언돼 있는데 그 축을 낼 **생산자가 없다**. 이것은 '표현할 수 없다'가
                # 아니라 레지스트리 구멍이고, 저장소에는 이미 그 이름(semantic_registry_gap)과
                # 사용자 문구가 있다. 미지원으로 부르면 없는 한계를 있다고 말하는 것이 된다.
                named = sorted({asset.symbol for _item, assets in contradicted for asset in assets})
                payload["semantic_ir"] = empty_semantic_ir(
                    status="needs_clarification",
                    missing_fields=["audience.requirement"],
                    message=(
                        "요청한 조건을 처리할 실행 자산은 선언돼 있으나 이 경로로 낼 수 없습니다"
                        f"(선언된 자산: {', '.join(named)})."
                    ),
                    failure_kind="system_failure",
                )
                payload["audience_execution_assets"] = [
                    {"argument": item["argument"], "evidence": item["evidence"]["text"],
                     "assets": [asset.to_dict() for asset in assets]}
                    for item, assets in contradicted
                ]
                return True
            payload["semantic_ir"] = empty_semantic_ir(
                status="unsupported",
                # 사용자에게 나가는 문장은 **모델이 쓴 산문이 아니다**. 실측(2026-08-03) 30/30 이
                # 모델 산문이었고 그 판정은 틀렸다 — 지어낸 kind 만 23종이었다.
                message="요청한 조건을 현재 실행 자산으로 표현할 수 없습니다.",
                failure_kind="unsupported",
            )
            payload["semantic_ir"]["unsupported_operations"] = [
                {
                    # kind 는 닫힌 코드다. 모델의 자유 텍스트(item["argument"])는 근거로 내린다.
                    "kind": "unsupported_semantics",
                    "reason": item["message"],
                    "evidence": item["evidence"]["text"],
                }
                for item in unsupported
            ]
        else:
            # 결핍의 원인을 리터럴 색인과 대조해 계산한다. 이것이 없으면 **시스템이 이미
            # 결정론으로 추출해 정규화까지 마친 값을 사용자에게 되묻는다**(실측 #3: '10%').
            causes = canonical_audience_claims.missing_field_cause_records(
                query, issues, payload.get("literal_bindings") or []
            )
            model_omitted = any(
                record.get("cause") == semantic_plan_module.CAUSE_MODEL_OMISSION
                for record in causes
            )
            payload["semantic_ir"] = empty_semantic_ir(
                status="needs_clarification",
                missing_fields=missing or ["audience.requirement"],
                message=issues[0]["message"],
                # 모델이 놓친 값을 사용자에게 물으면 안 된다 — 그 결핍은 재방출로 고친다.
                failure_kind=(
                    "structurer_failure" if model_omitted
                    else "user_clarification" if missing else "system_failure"
                ),
                missing_field_causes=causes,
            )
        return True

    assert expression is not None
    payload[EVENT_EXPRESSION_KEY] = {
        "expression": expression.to_dict(),
        "source": AUDIENCE_REQUIREMENT_KEY,
        "receipts": _audience_receipts(expression),
    }
    payload["semantic_ir"] = empty_semantic_ir(status="resolved")
    return True


# `_drop_campaign_constraint_requirements` 는 2026-08-02 SemanticPlanV2 이행으로 제거됐다.
# 그 함수는 "LLM 이 낸 결핍 보고 중 캠페인 제약 항목"을 사후 삭제하는 sweep 이었다. 결핍의
# 소유자가 LLM 이었기 때문에 필요했던 보정이고, 이제 결핍은 semantic_plan 노드 스키마에서
# 계산된다 — 캠페인 채널·혜택은 애초에 노드 필드가 아니므로 결핍으로 생기지 않는다.


def _derive_semantic_ir(
    payload: dict[str, Any], query: str, *, current_date: str | None = None
) -> None:
    """semantic_ir 을 단일 audience requirement에서 파생한다.

    audience_requirement가 없는 저장/규칙 플랜만 SemanticPlanV2 호환 경로를 탄다. 새 LLM 계약은
    두 표현을 동시에 만들 수 없고, canonical 경로는 실행 슬롯으로 역투영하지 않는다.
    """
    if _derive_audience_execution(payload, query, current_date=current_date):
        payload.setdefault("semantic_plan", {"nodes": []})
        return

    # Legacy ingress adapter — 신규 LLM schema에서는 노출되지 않는다.
    import semantic_plan as plan_module  # 순환 없음
    import semantic_pipeline  # 순환 없음(파이프라인은 v4 스키마를 모른다)

    raw_plan = payload.get("semantic_plan")
    if not isinstance(raw_plan, dict):
        raw_plan = {"nodes": []}
        payload["semantic_plan"] = raw_plan
    try:
        plan = plan_module.plan_from_dict(raw_plan, source_query=query)
    except plan_module.SemanticPlanError as exc:
        payload["semantic_plan"] = {"nodes": []}
        payload["semantic_ir"] = empty_semantic_ir(
            missing_fields=["semantic_plan"],
            message=f"의미 노드를 해석하지 못했습니다: {exc}",
        )
        return
    payload["semantic_plan"] = plan.to_dict()
    payload["semantic_ir"] = semantic_pipeline.project_semantic_ir(plan)


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
    # LLM 입력면에서 제거한 실행 호환 필드는 애플리케이션이 빈 값으로 소유한다. canonical
    # expression을 이 슬롯들로 다시 투영하지 않는다(SQL 권위가 되살아나는 것을 막는다).
    enriched.setdefault("target_user", {})
    enriched.setdefault("exclude", {"gender": [], "interests": [], "lifecycle": []})
    enriched.setdefault("campaign_constraints", {})
    enriched.setdefault("aggregation_request", None)
    enriched.setdefault("set_expressions", [])
    enriched.setdefault("computed_metrics", [])
    enriched.setdefault("external_conditions", [])
    enriched.setdefault("compound_dimension_filters", [])
    enriched.setdefault("result_limit", None)
    enriched.setdefault("semantic_evidence", [])
    enriched.setdefault("unresolved", [])
    enriched.setdefault("semantic_plan", {"nodes": []})
    # Literal extraction is application-owned input to canonical validation,
    # not a model output and not a post-validation decoration.
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
    _normalize_unique_evidence_spans(enriched, query)
    _normalize_audience_evidence(enriched, query)
    _derive_semantic_ir(enriched, query, current_date=current_date)
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
            AUDIENCE_REQUIREMENT_KEY: {
                "expression": None,
                "issues": [{
                    "code": "validation_mismatch",
                    "argument": "semantic_interpretation",
                    "message": "LLM 의미 구조화를 사용할 수 없습니다.",
                    "evidence": {"text": query, "start": 0, "end": len(query)},
                }],
            },
            "semantic_plan": {"nodes": []},
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
    # 구조화기 자체를 못 쓴 것은 '조건이 없다'가 아니라 내부 사고다 — 파생 semantic_ir(노드 0개
    # → resolved)이 그것을 성공으로 오인하지 않도록 애플리케이션이 직접 선언한다.
    payload["semantic_ir"] = empty_semantic_ir(
        missing_fields=["semantic_interpretation"],
        message="LLM 의미 구조화를 사용할 수 없습니다.",
        failure_kind="system_failure",
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

    audience_requirement = payload.get(AUDIENCE_REQUIREMENT_KEY)
    if audience_requirement is not None:
        if not isinstance(audience_requirement, dict):
            raise CampaignQueryPlanValidationError("audience_requirement must be an object")
        issues = audience_requirement.get("issues")
        if not isinstance(issues, list):
            raise CampaignQueryPlanValidationError("audience_requirement.issues must be an array")
        normalized_issues = [
            _validated_audience_issue(item, payload["original_query"]) for item in issues
        ]
        expression_raw = audience_requirement.get("expression")
        if expression_raw is not None and not isinstance(expression_raw, dict):
            raise CampaignQueryPlanValidationError(
                "audience_requirement.expression must be an object or null"
            )
        if isinstance(expression_raw, dict):
            try:
                expression = event_ir.condition_from_dict(expression_raw)
                event_ir.validate_evidence(expression)
            except (event_ir.IrSchemaError, event_ir.SemanticLossError) as exc:
                raise CampaignQueryPlanValidationError(str(exc)) from exc
            for atom, _negated in event_ir.iter_signed_atoms(expression):
                evidence_item = atom.evidence
                if evidence_item is None or not (
                    0 <= evidence_item.start < evidence_item.end <= len(payload["original_query"])
                    and payload["original_query"][evidence_item.start:evidence_item.end]
                    == evidence_item.text
                ):
                    raise CampaignQueryPlanValidationError(
                        "audience expression evidence does not match original_query"
                    )
        execution = payload.get(EVENT_EXPRESSION_KEY)
        if normalized_issues and isinstance(expression_raw, dict):
            details = "; ".join(
                f"{item['code']}[{item['argument']}]: {item['message']}"
                for item in normalized_issues
            )
            raise CampaignQueryPlanValidationError(
                "audience expression failed semantic validation; return a corrected "
                f"expression or expression=null with issues: {details}"
            )
        if normalized_issues and execution is not None:
            raise CampaignQueryPlanValidationError(
                "audience issues and executable event_expression cannot coexist"
            )
        if not normalized_issues and isinstance(expression_raw, dict):
            if not isinstance(execution, dict) or execution.get("expression") != expression_raw:
                raise CampaignQueryPlanValidationError(
                    "event_expression must be the exact application-owned audience projection"
                )

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
