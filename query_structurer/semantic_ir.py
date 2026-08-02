from __future__ import annotations

import copy
import re
from datetime import date, timedelta
from typing import Any

from calendar_window import parse_calendar_window_spans
from semantic_normalizers import AmountNormalizer, Money, NormalizationError


SEMANTIC_IR_STATUSES = frozenset(
    {"resolved", "policy_applied", "needs_clarification", "unsupported"}
)

_COMPARISON_TERMS: tuple[tuple[str, str], ...] = (
    ("이상", ">="),
    ("초과", ">"),
    ("이하", "<="),
    ("미만", "<"),
    (">=", ">="),
    ("<=", "<="),
    (">", ">"),
    ("<", "<"),
)
_COMPARISON_RE = re.compile(
    "|".join(re.escape(surface) for surface, _canonical in _COMPARISON_TERMS)
)
_PERCENT_RE = re.compile(r"(?<![\d.])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>%|퍼센트|프로)")
# 문장 안에서 AmountNormalizer 에 넘길 금액 표면의 경계만 찾는다. 배수 계산과 한글 수사
# 해석은 이 정규식이 아니라 AmountNormalizer 가 소유한다. 통화 표식이 필수이므로 기간이나
# 단순 수량을 금액으로 추측하지 않는다.
_MONEY_MAGNITUDE_GRAMMAR = r"(?:천만|백만|조|억|만|천)"
_MONEY_ARABIC_GRAMMAR = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_MONEY_SINO_GRAMMAR = r"[영공일이삼사오육칠팔구십백천]+"
_MONEY_VALUE_GRAMMAR = (
    rf"(?:{_MONEY_ARABIC_GRAMMAR}(?:\s*{_MONEY_MAGNITUDE_GRAMMAR})?"
    rf"|{_MONEY_SINO_GRAMMAR}\s*{_MONEY_MAGNITUDE_GRAMMAR})"
)
_MONEY_SUFFIX_CURRENCY_GRAMMAR = r"(?:원|won|krw|₩)"
# 한국어 '원'은 접미 통화 단위다. 접두사로도 허용하면 '지원 20만 명'의 끝 글자부터
# '원 20만'을 금액으로 오인한다. 접두 통화 표식은 실제 접두 표기인 기호/영문만 연다.
_MONEY_PREFIX_CURRENCY_GRAMMAR = r"(?:won|krw|₩)"
MONEY_LITERAL_RE = re.compile(
    rf"(?<![\d.,A-Za-z영공일이삼사오육칠팔구십백천])(?:"
    rf"{_MONEY_VALUE_GRAMMAR}\s*{_MONEY_SUFFIX_CURRENCY_GRAMMAR}"
    rf"|{_MONEY_PREFIX_CURRENCY_GRAMMAR}\s*{_MONEY_VALUE_GRAMMAR}"
    rf")(?![\d.,A-Za-z])",
    re.IGNORECASE,
)
COUNTER_LITERAL_RE = re.compile(
    r"(?<![\d.])(?P<value>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>종류|개|회|번|건|종)"
    # Korean counters normally carry a case/topic particle (``10개를``,
    # ``3회는``). Keep the particle outside the literal evidence span.
    r"(?=(?:을|를|이|가|은|는|의|만|중|에서|으로|로)?(?:\s|[,.;!?]|$))"
)
COUNTER_UNIT_SEMANTICS = {
    "개": "item_quantity",
    "회": "order_count",
    "번": "order_count",
    "건": "order_count",
    "종": "distinct_product_count",
    "종류": "distinct_product_count",
}
# 상대 기간 표면('6개월', '30일', '2주'). 이것이 없으면 '최근 6개월'의 '6' 이 **주인 없는 맨 숫자**
# 원자로 남는다 — 그 절의 노드가 period 를 소유해도 커버리지는 그 사실을 모르므로 정상 요청이
# 누락으로 오보고되고 재방출까지 돌게 된다(실측 2026-08-02: '최근 6개월 주문 5건 이상' 0/5 실패).
# 달력 창(2019년 3월)은 위에서 date_window 로 이미 점유되므로 여기 걸리지 않는다.
DURATION_LITERAL_RE = re.compile(
    r"(?<![\d.])(?P<value>\d+)\s*(?P<unit>개월|주일|주|일간|일|달|년간|년)(?![가-힣A-Za-z0-9])"
)
DURATION_UNIT_SEMANTICS = {
    "일": "days", "일간": "days", "주": "weeks", "주일": "weeks",
    "개월": "months", "달": "months", "년": "years", "년간": "years",
}
_NUMBER_RE = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d.])")


SEMANTIC_IR_LLM_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "operations",
        "missing_fields",
        "policy_applications",
        "unsupported_operations",
        "message",
    ],
    "properties": {
        "status": {
            "type": "string",
            "enum": sorted(SEMANTIC_IR_STATUSES),
        },
        # 결핍의 **원인**. status 만으로는 "사용자가 안 알려준 것"과 "우리가 못 만든 것"이
        # 구분되지 않아 후자까지 사용자 확인 요청이 됐다(실측: req-1.member_entity 를 물어봄).
        "missing_field_causes": {
            "type": "array",
            "items": {"type": "object"},
        },
        "failure_kind": {
            "type": ["string", "null"],
            "enum": [
                "user_clarification", "structurer_failure", "system_failure", "unsupported", None
            ],
        },
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "metric_id", "direction", "bindings"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["period_over_period_change"],
                    },
                    "metric_id": {"type": "string", "minLength": 1},
                    "direction": {
                        "type": "string",
                        "enum": ["increase", "decrease"],
                    },
                    "bindings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["role", "literal_id"],
                            "properties": {
                                "role": {
                                    "type": "string",
                                    "enum": [
                                        "baseline",
                                        "current",
                                        "threshold",
                                        "comparison",
                                    ],
                                },
                                "literal_id": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
        },
        "missing_fields": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "policy_applications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["policy_id", "fields"],
                "properties": {
                    "policy_id": {"type": "string", "minLength": 1},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "unsupported_operations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "reason", "evidence"],
                "properties": {
                    "kind": {"type": "string", "minLength": 1},
                    "reason": {"type": "string", "minLength": 1},
                    "evidence": {"type": "string"},
                },
            },
        },
        "message": {"type": ["string", "null"]},
    },
}


def _number(value: str) -> int | float:
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def _overlaps(start: int, end: int, occupied: list[tuple[int, int]]) -> bool:
    return any(start < occupied_end and occupied_start < end for occupied_start, occupied_end in occupied)


def extract_literal_bindings(
    query: str,
    *,
    current_date: str | date | None = None,
) -> list[dict[str, Any]]:
    """Extract value atoms without assigning business meaning between them.

    Dates, money, counter-bearing numbers, percentages, and comparison operators
    are application-owned. Korean counters are semantic literals: ``개`` means
    item quantity, ``회/번/건`` means order count, and ``종/종류`` means distinct
    product count. The LLM may not rewrite one counter into another.
    The LLM may only connect the returned IDs to semantic roles; it cannot submit
    replacement values in the semantic operation payload.
    """

    if not isinstance(query, str) or not query:
        return []
    reference_date: date | None
    if isinstance(current_date, date):
        reference_date = current_date
    elif isinstance(current_date, str):
        try:
            reference_date = date.fromisoformat(current_date)
        except ValueError:
            reference_date = None
    else:
        reference_date = None

    literals: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    counters: dict[str, int] = {}

    def append(kind: str, start: int, end: int, value: Any, normalized: Any) -> None:
        counters[kind] = counters.get(kind, 0) + 1
        literals.append(
            {
                "id": f"{kind}_{counters[kind]}",
                "kind": kind,
                "text": query[start:end],
                "start": start,
                "end": end,
                "value": value,
                "normalized": normalized,
            }
        )
        occupied.append((start, end))

    for window, start, end in parse_calendar_window_spans(query, today=reference_date):
        start_date = date(
            int(window["from"][:4]), int(window["from"][4:6]), int(window["from"][6:8])
        )
        inclusive_end = date(
            int(window["to"][:4]), int(window["to"][4:6]), int(window["to"][6:8])
        )
        append(
            "date_window",
            start,
            end,
            query[start:end],
            {
                "from": window["from"],
                "to": window["to"],
                "label": window.get("label"),
                "event_ir_window": {
                    "type": "interval",
                    "start": start_date.isoformat(),
                    "end_exclusive": (inclusive_end + timedelta(days=1)).isoformat(),
                },
                # 시각 경계는 있을 때만 싣는다 — 날짜만 있는 창의 normalized shape 를 바꾸지 않는다.
                **{key: window[key] for key in ("from_time", "to_time") if window.get(key) is not None},
            },
        )

    for match in MONEY_LITERAL_RE.finditer(query):
        if _overlaps(match.start(), match.end(), occupied):
            continue
        try:
            normalized_money = AmountNormalizer.normalize(match.group(0))
        except NormalizationError:
            continue
        if not isinstance(normalized_money, Money):
            continue
        append(
            "money",
            match.start(),
            match.end(),
            normalized_money.amount,
            normalized_money.to_dict(),
        )

    for match in _PERCENT_RE.finditer(query):
        if not _overlaps(match.start(), match.end(), occupied):
            value = _number(match.group("value"))
            append("percentage", match.start(), match.end(), value, {"value": value, "unit": "percent"})

    for match in COUNTER_LITERAL_RE.finditer(query):
        if not _overlaps(match.start(), match.end(), occupied):
            value = _number(match.group("value").replace(",", ""))
            unit = match.group("unit")
            append(
                "number_with_unit",
                match.start(),
                match.end(),
                value,
                {
                    "value": value,
                    "surface_unit": unit,
                    "semantic_unit": COUNTER_UNIT_SEMANTICS[unit],
                },
            )

    for match in DURATION_LITERAL_RE.finditer(query):
        if not _overlaps(match.start(), match.end(), occupied):
            value = _number(match.group("value"))
            unit = match.group("unit")
            append(
                "duration",
                match.start(),
                match.end(),
                value,
                {"value": value, "surface_unit": unit, "semantic_unit": DURATION_UNIT_SEMANTICS[unit]},
            )

    comparison_map = dict(_COMPARISON_TERMS)
    for match in _COMPARISON_RE.finditer(query):
        append(
            "comparison_operator",
            match.start(),
            match.end(),
            match.group(0),
            comparison_map[match.group(0)],
        )

    for match in _NUMBER_RE.finditer(query):
        if not _overlaps(match.start(), match.end(), occupied):
            value = _number(match.group(0))
            append("number", match.start(), match.end(), value, value)

    return sorted(literals, key=lambda item: (item["start"], item["end"], item["kind"]))


def empty_semantic_ir(
    status: str = "needs_clarification",
    *,
    missing_fields: list[str] | None = None,
    message: str | None = None,
    failure_kind: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "operations": [],
        "missing_fields": list(missing_fields or []),
        "missing_field_causes": [],
        "failure_kind": failure_kind,
        "policy_applications": [],
        "unsupported_operations": [],
        "message": message,
    }


def _has_plan_meaning(payload: dict[str, Any]) -> bool:
    def has_value(value: Any) -> bool:
        if value is None or value is False or value == "" or value == [] or value == {}:
            return False
        if isinstance(value, dict):
            return any(has_value(child) for child in value.values())
        if isinstance(value, list):
            return any(has_value(child) for child in value)
        return True

    # condition_evaluations 는 SemanticPlan 컴파일러가 채우는 plan 의미이고, semantic_plan 은
    # 의미 노드 그 자체다 — 실행 슬롯이 비어도 의미 근거는 있다(슬롯 계층이 SemanticPlan 으로
    # 이관되면서 '근거 있는 resolved'의 판정 기준이 노드로 옮겨졌다).
    return any(
        has_value(payload.get(key))
        for key in (
            "target_user", "exclude", "campaign_constraints", "aggregation_request",
            "set_expressions", "condition_evaluations", "semantic_plan",
            "audience_requirement", "event_expression",
        )
    )


def validate_semantic_ir(
    semantic_ir: Any,
    literal_bindings: Any,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(semantic_ir, dict):
        raise ValueError("semantic_ir must be an object")
    # 필수 키는 반드시 있어야 하고, 선언되지 않은 키는 올 수 없다. 파생 진단 필드
    # (missing_field_causes/failure_kind)는 **선택**이다 — 계산되지 않은 경로(빈 플랜·직접
    # 조립한 payload)도 유효한 semantic_ir 이어야 하기 때문이다.
    required_keys = set(SEMANTIC_IR_LLM_JSON_SCHEMA["required"])
    declared_keys = set(SEMANTIC_IR_LLM_JSON_SCHEMA["properties"])
    if not required_keys <= set(semantic_ir) or not set(semantic_ir) <= declared_keys:
        raise ValueError("semantic_ir fields do not match the closed schema")
    status = semantic_ir.get("status")
    if status not in SEMANTIC_IR_STATUSES:
        raise ValueError("semantic_ir.status is invalid")
    for key in ("operations", "missing_fields", "policy_applications", "unsupported_operations"):
        if not isinstance(semantic_ir.get(key), list):
            raise ValueError(f"semantic_ir.{key} must be an array")
    if semantic_ir.get("message") is not None and not isinstance(semantic_ir.get("message"), str):
        raise ValueError("semantic_ir.message must be a string or null")

    literal_items = literal_bindings if isinstance(literal_bindings, list) else []
    literal_by_id = {
        item.get("id"): item
        for item in literal_items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if len(literal_by_id) != len(literal_items):
        raise ValueError("literal_bindings must contain unique object IDs")

    for index, operation in enumerate(semantic_ir["operations"]):
        if not isinstance(operation, dict):
            raise ValueError(f"semantic_ir.operations[{index}] must be an object")
        if set(operation) != {"kind", "metric_id", "direction", "bindings"}:
            raise ValueError(f"semantic_ir.operations[{index}] fields are invalid")
        if operation.get("kind") != "period_over_period_change":
            raise ValueError(f"semantic_ir.operations[{index}].kind is unsupported")
        if not isinstance(operation.get("metric_id"), str) or not operation["metric_id"].strip():
            raise ValueError(f"semantic_ir.operations[{index}].metric_id is required")
        if operation.get("direction") not in {"increase", "decrease"}:
            raise ValueError(f"semantic_ir.operations[{index}].direction is invalid")
        bindings = operation.get("bindings")
        if not isinstance(bindings, list):
            raise ValueError(f"semantic_ir.operations[{index}].bindings must be an array")
        by_role: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"role", "literal_id"}:
                raise ValueError(f"semantic_ir.operations[{index}] contains an invalid binding")
            role, literal_id = binding.get("role"), binding.get("literal_id")
            if role in by_role:
                raise ValueError(f"semantic_ir.operations[{index}] contains duplicate role {role}")
            literal = literal_by_id.get(literal_id)
            if literal is None:
                raise ValueError(f"semantic_ir.operations[{index}] references unknown literal {literal_id}")
            expected_kind = {
                "baseline": "date_window",
                "current": "date_window",
                "threshold": "percentage",
                "comparison": "comparison_operator",
            }.get(str(role))
            if expected_kind is None or literal.get("kind") != expected_kind:
                raise ValueError(f"semantic_ir.operations[{index}].{role} has the wrong literal kind")
            by_role[str(role)] = literal
        if not {"baseline", "current"} <= set(by_role):
            raise ValueError(f"semantic_ir.operations[{index}] requires baseline and current date literals")
        if ("threshold" in by_role) != ("comparison" in by_role):
            raise ValueError(f"semantic_ir.operations[{index}] requires threshold and comparison together")
        if by_role["baseline"]["id"] == by_role["current"]["id"]:
            raise ValueError(f"semantic_ir.operations[{index}] requires two distinct periods")

    missing_fields = semantic_ir["missing_fields"]
    policy_applications = semantic_ir["policy_applications"]
    unsupported = semantic_ir["unsupported_operations"]
    if not all(isinstance(item, str) and item.strip() for item in missing_fields):
        raise ValueError("semantic_ir.missing_fields must contain non-empty strings")
    if status == "needs_clarification" and not missing_fields:
        raise ValueError("needs_clarification requires missing_fields")
    if status != "needs_clarification" and missing_fields:
        raise ValueError("missing_fields is only valid for needs_clarification")
    if status == "policy_applied" and not policy_applications:
        raise ValueError("policy_applied requires policy_applications")
    for index, application in enumerate(policy_applications):
        if not isinstance(application, dict) or set(application) != {"policy_id", "fields"}:
            raise ValueError(f"semantic_ir.policy_applications[{index}] is invalid")
        if not isinstance(application.get("policy_id"), str) or not application["policy_id"].strip():
            raise ValueError(f"semantic_ir.policy_applications[{index}].policy_id is required")
        fields = application.get("fields")
        if not isinstance(fields, list) or not fields or not all(
            isinstance(field, str) and field.strip() for field in fields
        ):
            raise ValueError(f"semantic_ir.policy_applications[{index}].fields is invalid")
    if status != "policy_applied" and policy_applications:
        raise ValueError("policy_applications is only valid for policy_applied")
    if status == "unsupported" and not unsupported:
        raise ValueError("unsupported requires unsupported_operations")
    for index, item in enumerate(unsupported):
        if not isinstance(item, dict) or set(item) != {"kind", "reason", "evidence"}:
            raise ValueError(f"semantic_ir.unsupported_operations[{index}] is invalid")
        if not all(isinstance(item.get(key), str) for key in ("kind", "reason", "evidence")):
            raise ValueError(f"semantic_ir.unsupported_operations[{index}] must contain strings")
        if not item["kind"].strip() or not item["reason"].strip():
            raise ValueError(f"semantic_ir.unsupported_operations[{index}] requires kind and reason")
    if status != "unsupported" and unsupported:
        raise ValueError("unsupported_operations is only valid for unsupported")
    if status in {"needs_clarification", "unsupported"} and semantic_ir["operations"]:
        raise ValueError(f"{status} cannot contain executable operations")
    if status in {"resolved", "policy_applied"} and not semantic_ir["operations"]:
        if payload is None or not _has_plan_meaning(payload):
            raise ValueError(f"{status} requires an operation or another grounded plan condition")
    return copy.deepcopy(semantic_ir)


# `materialize_semantic_operations` 는 2026-08-02 삭제됐다 — LLM 이 낸 semantic_ir 연산을
# metric_trend 실행 슬롯으로 직접 투영하던 함수다. 그 슬롯의 생산자는 이제
# LegacyQueryPlanCompiler 하나이고, 의미의 소유자는 SemanticPlanV2 MetricComparison 노드다.


# 결핍 사후 삭제(`drop_satisfied_missing_fields`)는 2026-08-02 SemanticPlanV2 이행으로 제거됐다.
# 그 함수는 "LLM 이 만든 결핍 보고 중 이미 채워진 것"을 걷는 sweep 이었고, 존재 이유는 결핍의
# 소유자가 LLM 이었다는 점 하나였다. 이제 missing_fields 는 semantic_plan 노드 스키마에서
# 계산되므로(`semantic_pipeline.project_semantic_ir`) 걷어낼 stale 이 구조적으로 생기지 않는다.
