"""타겟팅 IR — LLM 이 SQL 대신 내놓는 닫힌 표현식.

배경: 템플릿 빌더가 못 만드는 요청은 LLM 이 SQL 을 직접 써 왔다. 출력 공간이 자유 SQL 이면
'2019년 top10 상품'만 계산하고 정작 그 상품을 산 **고객**을 빼먹는 식의 오류가 계속 나오고,
사후 의미검증이 그걸 잡아내면 사용자는 결과를 못 받는다. 규칙을 프롬프트에 더 적는 방식으로는
표현 형태가 늘어날수록 같은 사고가 반복된다.

이 모듈은 LLM 의 출력 공간을 다음 문법으로 닫는다:

    member_set := all | member_filter(canonical) | age(min,max)
                | relation(name, exists, windowDays?, entitySet?)
                | and[...] | or[...] | not(...)

효과는 두 가지다.
  1) 회원 투영(SELECT DISTINCT 회원키)은 컴파일러가 항상 붙인다 — '대상을 잊은 SQL' 이 문법적으로
     표현 불가능해진다.
  2) 모든 리프가 레지스트리(member_target_filters.json)에 대해 검증된다 — 없는 컬럼·값·테이블을
     지어낼 수 없다. 검증은 LLM 판단이 아니라 사전 대조다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 물리 매핑과 방언 렌더러는 호출자가 주입한다.
"""

from __future__ import annotations

from typing import Any, Callable

from calendar_window import calendar_window_from_parts, parse_calendar_window
from entity_set import build_derived_set_ast, compile_entity_set_predicate, entity_set_capability


MAX_DEPTH = 6
MAX_NODES = 40
_COMBINATORS = ("and", "or", "not")
_LEAVES = ("all", "member_filter", "age", "relation")


class TargetingExpressionError(ValueError):
    """IR 이 문법·어휘·물리 매핑 중 하나를 위반했다(어느 것인지 메시지에 남긴다)."""


def _relative_date(days: int) -> str:
    return f"CONVERT(CHAR(8), DATEADD(DAY, -{int(days)}, GETDATE()), 112)"


# 절대 기간의 자유 표현 슬롯. 연/월 이외의 달력 표현(분기·반기·특정일)까지 한 필드로 받고 해석은
# calendar_window 가 한다 — 표현형이 늘어도 스키마를 늘리지 않는다(LLM 이 문장의 기간을 그대로 옮긴다).
_PERIOD_FIELD = {
    "type": ["string", "null"],
    "description": (
        "절대 기간 표현을 원문 그대로(예: '2019년 3월', '2019년 2분기', '2019년 상반기', "
        "'2019-03-05', '2019년'). 상대 기간(최근 N일)은 windowDays 를 쓴다."
    ),
}


def targeting_expression_json_schema(
    entity_set_config: dict[str, Any],
    canonicals: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """LLM tool 스키마. 어휘(canonical/관계/엔터티/지표)는 레지스트리에서 그대로 열거한다."""
    relations = sorted(str(name) for name in (entity_set_config.get("relations") or {}))
    entities = sorted(str(name) for name in (entity_set_config.get("entities") or {}))
    measures = sorted(str(name) for name in (entity_set_config.get("measures") or {}))
    filter_dimensions = sorted(str(name) for name in (entity_set_config.get("filters") or {}))
    entity_set = {
        "type": "object",
        "description": "순위로 정의되는 엔터티 집합(예: 2019년 판매수량 상위 10개 상품).",
        "properties": {
            "entity": {"type": "string", "enum": entities},
            "measure": {"type": "string", "enum": measures},
            "direction": {"type": "string", "enum": ["top", "bottom"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "rankRelation": {"type": "string", "enum": relations, "description": "순위를 계산할 관계(기본: 구매)."},
            "windowDays": {"type": ["integer", "null"], "description": "상대 기간(일). 절대 기간은 period/year 로 준다."},
            "year": {"type": ["integer", "null"], "description": "절대 연도(예: 2019). month 와 함께 주면 그 달."},
            "month": {"type": ["integer", "null"], "minimum": 1, "maximum": 12, "description": "절대 월(1~12). year 필요."},
            "period": _PERIOD_FIELD,
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"const": "dimension_filter"},
                        "dimension": {"type": "string", "enum": filter_dimensions},
                        "operator": {"type": "string", "enum": ["equals", "contains"]},
                        "value": {"type": "string", "minLength": 1},
                    },
                    "required": ["type", "dimension", "operator", "value"],
                },
            },
        },
        "required": ["entity", "measure", "direction", "limit"],
    }
    node: dict[str, Any] = {
        "type": "object",
        "description": "회원 집합 표현식. 반드시 아래 키 중 정확히 하나만 사용한다.",
        "properties": {
            "all": {"type": "boolean", "description": "조건 없는 전체 회원."},
            "member_filter": {
                "type": "string",
                "enum": sorted(canonicals),
                "description": "회원 속성 조건. 이 목록 밖의 값은 사용할 수 없다.",
            },
            "age": {
                "type": "object",
                "properties": {"min": {"type": ["integer", "null"]}, "max": {"type": ["integer", "null"]}},
            },
            "relation": {
                "type": "object",
                "description": "행동 관계의 존재/부재. entitySet 을 주면 그 집합에 한정한다.",
                "properties": {
                    "name": {"type": "string", "enum": relations},
                    "exists": {"type": "boolean"},
                    "windowDays": {"type": ["integer", "null"]},
                    "year": {"type": ["integer", "null"]},
                    "month": {"type": ["integer", "null"], "minimum": 1, "maximum": 12},
                    "period": _PERIOD_FIELD,
                    "entitySet": entity_set,
                },
                "required": ["name", "exists"],
            },
            "and": {"type": "array", "items": {"$ref": "#/$defs/node"}},
            "or": {"type": "array", "items": {"$ref": "#/$defs/node"}},
            "not": {"$ref": "#/$defs/node"},
        },
    }
    return {
        "type": "object",
        "$defs": {"node": node},
        "properties": {
            "expression": {"$ref": "#/$defs/node"},
            "unsupported": {
                "type": ["string", "null"],
                "description": "이 문법으로 표현할 수 없으면 사유를 적고 expression 은 생략한다.",
            },
        },
        "required": ["expression"],
    }


def _window(payload: dict[str, Any]) -> dict[str, Any] | None:
    """LLM 이 준 기간 슬롯을 창으로 해석한다. 달력 규칙은 calendar_window 가 단일 소유한다.

    period(자유 표현) → year/month(구조화) → windowDays(상대) 순. 예전에는 year 정수 하나뿐이라
    '2019년 3월'을 LLM 이 표현할 수단 자체가 없어 연 단위로 뭉개졌다."""
    period = payload.get("period")
    if isinstance(period, str) and period.strip():
        window = parse_calendar_window(period)
        if window is not None:
            return window
    absolute = calendar_window_from_parts(payload.get("year"), payload.get("month"))
    if absolute is not None:
        return absolute
    days = payload.get("windowDays")
    if isinstance(days, int) and days > 0:
        return {"days": days, "label": f"최근 {days}일"}
    return None


def validate_targeting_expression(
    node: Any,
    entity_set_config: dict[str, Any],
    canonicals: dict[str, dict[str, Any]],
    *,
    depth: int = 0,
    counter: list[int] | None = None,
) -> None:
    """문법·어휘·물리 매핑을 사전 대조한다. 위반은 예외로 즉시 드러낸다(조용한 축소 금지)."""
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if depth > MAX_DEPTH or counter[0] > MAX_NODES:
        raise TargetingExpressionError("표현식이 너무 깊거나 큽니다.")
    if not isinstance(node, dict):
        raise TargetingExpressionError("표현식 노드는 객체여야 합니다.")
    keys = [key for key in (*_LEAVES, *_COMBINATORS) if key in node]
    if len(keys) != 1:
        raise TargetingExpressionError(f"노드에는 정확히 하나의 키가 필요합니다: {sorted(node)}")
    key = keys[0]

    if key == "all":
        return
    if key == "member_filter":
        if str(node["member_filter"]) not in canonicals:
            raise TargetingExpressionError(f"등록되지 않은 회원 조건입니다: {node['member_filter']}")
        return
    if key == "age":
        age = node["age"]
        if not isinstance(age, dict) or not any(isinstance(age.get(bound), int) for bound in ("min", "max")):
            raise TargetingExpressionError("age 에는 min 또는 max 가 필요합니다.")
        return
    if key == "relation":
        relation = node["relation"]
        if not isinstance(relation, dict):
            raise TargetingExpressionError("relation 은 객체여야 합니다.")
        spec = (entity_set_config.get("relations") or {}).get(str(relation.get("name")))
        if not isinstance(spec, dict):
            raise TargetingExpressionError(f"등록되지 않은 관계입니다: {relation.get('name')}")
        if _window(relation) and not spec.get("dateColumn"):
            raise TargetingExpressionError(f"{relation.get('name')} 관계에는 기간 기준 컬럼이 없습니다.")
        entity_set = relation.get("entitySet")
        if entity_set is not None:
            reason = entity_set_capability(
                _entity_set_node(entity_set, str(relation.get("name")), entity_set_config), entity_set_config
            )
            if reason:
                raise TargetingExpressionError(f"엔터티 집합을 컴파일할 수 없습니다: {reason}")
        return
    if key == "not":
        validate_targeting_expression(node["not"], entity_set_config, canonicals, depth=depth + 1, counter=counter)
        return
    operands = node[key]
    if not isinstance(operands, list) or len(operands) < 2:
        raise TargetingExpressionError(f"{key} 에는 2개 이상의 피연산자가 필요합니다.")
    for operand in operands:
        validate_targeting_expression(operand, entity_set_config, canonicals, depth=depth + 1, counter=counter)


def _entity_set_node(
    payload: dict[str, Any],
    member_relation: str,
    config: dict[str, Any],
    *,
    negated: bool = False,
) -> dict[str, Any]:
    """LLM 이 준 엔터티 집합 조각을 1단계 노드 형태로 정규화한다(같은 컴파일러를 그대로 쓴다)."""
    if not isinstance(payload, dict):
        return {}
    node = {
        "relation": member_relation,
        "rankRelation": str(payload.get("rankRelation") or config.get("defaultRankRelation") or member_relation),
        "entity": str(payload.get("entity") or ""),
        "measure": str(payload.get("measure") or config.get("defaultMeasure") or ""),
        "direction": "bottom" if str(payload.get("direction")) == "bottom" else "top",
        "limit": int(payload.get("limit") or 10),
        "window": _window(payload),
        "negated": negated,
    }
    node["derived_set_ast"] = build_derived_set_ast(
        member_relation=node["relation"],
        rank_relation=node["rankRelation"],
        entity=node["entity"],
        measure=node["measure"],
        direction=node["direction"],
        limit=node["limit"],
        window=node["window"],
        filters=payload.get("filters") if isinstance(payload.get("filters"), list) else None,
        negated=negated,
    )
    return node


def compile_targeting_expression(
    node: dict[str, Any],
    entity_set_config: dict[str, Any],
    *,
    member_predicate: Callable[[str], str | None],
    member_alias: str = "B",
    member_key: str = "MEMBER_NO",
    age_column: str = "AGE",
    relative_date: Callable[[int], str] = _relative_date,
) -> str:
    """IR 을 회원(B) 기준 단일 술어로 컴파일한다.

    회원 투영은 호출자가 소유하므로 여기서는 술어만 만든다 — 어떤 표현식도 '회원이 아닌 것'을
    결과로 낼 수 없다.
    """
    keys = [key for key in (*_LEAVES, *_COMBINATORS) if key in node]
    key = keys[0]
    if key == "all":
        return "1 = 1"
    if key == "member_filter":
        predicate = member_predicate(str(node["member_filter"]))
        if not predicate:
            raise TargetingExpressionError(f"회원 조건을 컴파일할 수 없습니다: {node['member_filter']}")
        return predicate
    if key == "age":
        age = node["age"]
        bounds = []
        if isinstance(age.get("min"), int):
            bounds.append(f"{member_alias}.{age_column} >= {int(age['min'])}")
        if isinstance(age.get("max"), int):
            bounds.append(f"{member_alias}.{age_column} <= {int(age['max'])}")
        return " AND ".join(bounds) if len(bounds) == 1 else "(" + " AND ".join(bounds) + ")"
    if key == "not":
        return "NOT (" + compile_targeting_expression(
            node["not"], entity_set_config, member_predicate=member_predicate,
            member_alias=member_alias, member_key=member_key, age_column=age_column, relative_date=relative_date,
        ) + ")"
    if key in {"and", "or"}:
        joiner = " AND " if key == "and" else " OR "
        parts = [
            compile_targeting_expression(
                operand, entity_set_config, member_predicate=member_predicate,
                member_alias=member_alias, member_key=member_key, age_column=age_column, relative_date=relative_date,
            )
            for operand in node[key]
        ]
        return "(" + joiner.join(parts) + ")"
    return _compile_relation(
        node["relation"], entity_set_config,
        member_alias=member_alias, member_key=member_key, relative_date=relative_date,
    )


def _compile_relation(
    relation: dict[str, Any],
    config: dict[str, Any],
    *,
    member_alias: str,
    member_key: str,
    relative_date: Callable[[int], str],
) -> str:
    name = str(relation.get("name"))
    spec = config["relations"][name]
    exists = bool(relation.get("exists", True))
    entity_set = relation.get("entitySet")
    if isinstance(entity_set, dict) and entity_set:
        node = _entity_set_node(entity_set, name, config, negated=not exists)
        predicate = compile_entity_set_predicate(node, config, member_alias=member_alias, member_key=member_key)
        if predicate is None:
            raise TargetingExpressionError("엔터티 집합 술어를 컴파일하지 못했습니다.")
        return predicate

    alias = str(spec["outerAlias"])
    where = [str(spec["memberJoin"]).format(alias=alias, member=f"{member_alias}.{member_key}")]
    where.extend(str(condition).format(alias=alias) for condition in spec.get("conditions", []) or [])
    window = _window(relation)
    date_column = str(spec.get("dateColumn", "")).format(alias=alias)
    if window and date_column:
        if window.get("from"):
            where.append(f"{date_column} BETWEEN '{window['from']}' AND '{window['to']}'")
        else:
            where.append(f"{date_column} >= {relative_date(int(window['days']))}")
    keyword = "EXISTS" if exists else "NOT EXISTS"
    body = "\n      AND ".join(where)
    return f"{keyword} (\n    SELECT 1\n    FROM {spec['table']} {alias}\n    WHERE {body}\n  )"


def describe_targeting_expression(node: dict[str, Any], negated: bool = False) -> list[str]:
    """표현식이 담은 조건들의 canonical 요약(커버리지·트레이스 표시용).

    부정 라벨은 슬롯 경로의 표기(``non_<canonical>``)를 그대로 따른다 — 조건 커버리지 검증이
    두 경로를 같은 어휘로 대조해야 IR 후보만 '조건 미반영'으로 탈락하는 일이 없다.
    """
    labels: list[str] = []
    prefix = "non_" if negated else ""
    key = next((key for key in (*_LEAVES, *_COMBINATORS) if key in node), None)
    if key == "member_filter":
        labels.append(prefix + str(node["member_filter"]))
    elif key == "age":
        age = node["age"]
        labels.append(f"{prefix}age_{age.get('min') or ''}_{age.get('max') or ''}")
    elif key == "relation":
        relation = node["relation"]
        suffix = "" if relation.get("exists", True) and not negated else "_absent"
        labels.append(f"{relation.get('name')}{suffix}")
        if isinstance(relation.get("entitySet"), dict):
            entity_set = relation["entitySet"]
            labels.append(f"{entity_set.get('direction')}_{entity_set.get('limit')}_{entity_set.get('entity')}")
    elif key == "not":
        labels.extend(describe_targeting_expression(node["not"], not negated))
    elif key in {"and", "or"}:
        for operand in node[key]:
            labels.extend(describe_targeting_expression(operand, negated))
    return labels
