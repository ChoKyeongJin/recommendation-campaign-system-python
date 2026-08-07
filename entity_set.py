"""파생 엔터티 집합(entity set) — 리터럴이 아니라 '계산으로 정의되는' 피연산자.

배경: 지금까지 타겟 조건의 피연산자 자리에는 리터럴만 올 수 있었다(``purchase_object: "기저귀"``).
그래서 ``2019년 가장 많이 팔린 상품 10개를 구매한 고객``처럼 피연산자 자체가 다른 질의의 결과인
요청은 표현할 자리가 없었고, 문장 형태마다 전용 빌더를 붙이는 수밖에 없었다. 문장 형태는 무한하지만
조합의 종류는 적다 — 이 모듈은 그 조합을 닫힌 파생 집합 AST로 표현한다.

    aggregation := aggregate(relation, group_by=entity, measure, window, filters)
    ranking     := rank(aggregation, direction, limit)
    member_set  := members(relation, ranking, exists)                 # AST 루트

즉 ``집계 → 랭킹 → 고객 집합``의 각 단계가 별도 노드다. 파서가 돌려주는 기존 평면 필드는 하위
호환용으로 남기되, ``derived_set_ast``가 있으면 검증과 컴파일은 AST를 단일 진실 공급원으로 사용한다.

따라서 엔터티(상품/브랜드/카테고리) · 지표(판매수량/매출/주문건수/구매자수) · 방향(상위/하위) ·
관계(구매/장바구니) · 기간 · 부정을 바꾼 요청은 코드 추가 없이 같은 컴파일러가 처리한다. 물리 매핑
(테이블·컬럼·조인·별칭)은 전부 ``member_target_filters.json`` 의 ``entity_set_targets`` 가 소유한다 —
이 모듈에는 스키마 지식이 없다(DB 이식성).

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 설정은 호출자가 주입한다.
"""

from __future__ import annotations

import sql_dialect

import re
from typing import Any

import lexicon_patterns
from reference_time import ReferenceDate, ReferenceTimeError, relative_day_char8


# 랭킹으로 만든 유한 집합과 회원 행동 집합의 교집합 크기.
# ``상위 3개 중에서 2개만 구매``처럼 모집단 크기와 회원별 일치 개수가 함께 있는 문법만 읽는다.
# 단독 ``2개 구매``는 상품 수량일 수 있으므로 이 파서가 소유하지 않는다.
_SET_SCOPE_ALT = lexicon_patterns.alternation("clause_scope_marker")
_CARDINALITY_OPERATOR = {
    None: "=",
    "만": "=",
    "이상": ">=",
    "이하": "<=",
    "초과": ">",
    "미만": "<",
}
CARDINALITY_OPERATORS = frozenset({"=", ">", ">=", "<", "<="})

DEFAULT_LIMIT = 10
MAX_LIMIT = 1000

DERIVED_SET_AST_FIELD = "derived_set_ast"
AGGREGATION_NODE = "aggregation"
RANKING_NODE = "ranking"
MEMBER_SET_NODE = "member_set"
DIMENSION_FILTER_NODE = "dimension_filter"
DIMENSION_FILTER_OPERATORS = frozenset({"equals", "contains"})


def build_derived_set_ast(
    *,
    member_relation: str,
    rank_relation: str,
    entity: str,
    measure: str,
    direction: str,
    limit: int,
    window: dict[str, Any] | None = None,
    member_window: dict[str, Any] | None = None,
    filters: list[dict[str, Any]] | None = None,
    cardinality: dict[str, Any] | None = None,
    negated: bool = False,
) -> dict[str, Any]:
    """평면 파싱 결과를 ``집계 → 랭킹 → 회원 집합`` AST로 만든다.

    단계별 책임을 섞지 않는다. 특히 순위를 계산하는 관계와 그 결과 엔터티를 회원에게 연결하는
    관계가 다를 수 있으므로 두 관계는 각각 aggregation/member_set 노드가 소유한다. 기간도
    마찬가지다 — ``window`` 는 랭킹 모집단의 기간이고 ``member_window`` 는 회원이 그 행동을
    한 기간이다.
    """
    aggregation: dict[str, Any] = {
        "type": AGGREGATION_NODE,
        "relation": str(rank_relation),
        "group_by": str(entity),
        "measure": str(measure),
    }
    if isinstance(window, dict):
        aggregation["window"] = dict(window)
    if filters:
        aggregation["filters"] = [dict(item) for item in filters if isinstance(item, dict)]
    member_set = {
        "type": MEMBER_SET_NODE,
        "relation": str(member_relation),
        "exists": not bool(negated),
        "source": {
            "type": RANKING_NODE,
            "direction": "bottom" if str(direction) == "bottom" else "top",
            "limit": int(limit),
            "source": aggregation,
        },
    }
    # 회원이 **언제** 그 행동을 했는가. 랭킹 창(aggregation.window)과 다른 절의 기간이므로 다른
    # 노드가 소유한다 — 한 슬롯에 뭉치면 '작년에 팔린 상품을 올해 산 회원' 같은 요청에서 둘 중
    # 하나가 조용히 사라진다. 없으면 키도 없다(기간을 지어내지 않는다).
    if isinstance(member_window, dict):
        member_set["window"] = dict(member_window)
    if isinstance(cardinality, dict):
        member_set["cardinality"] = {
            "operator": cardinality.get("operator"),
            "value": cardinality.get("value"),
        }
    return member_set


def _window_is_valid(window: Any) -> bool:
    """창 표기 하나의 닫힌 문법(절대 구간 또는 상대 일수). 두 창이 같은 규칙을 쓴다."""
    if not isinstance(window, dict):
        return False
    if isinstance(window.get("from"), str) and isinstance(window.get("to"), str):
        return True
    days = window.get("days")
    return isinstance(days, int) and not isinstance(days, bool) and days > 0


def derived_set_ast_error(ast: Any) -> str | None:
    """파생 집합 AST의 닫힌 문법을 검사하고 안정적인 오류 코드를 반환한다."""
    if not isinstance(ast, dict) or ast.get("type") != MEMBER_SET_NODE:
        return "invalid_derived_set_ast_root"
    if not isinstance(ast.get("relation"), str) or not ast["relation"]:
        return "invalid_derived_set_member_relation"
    if not isinstance(ast.get("exists"), bool):
        return "invalid_derived_set_membership"
    if ast.get("window") is not None and not _window_is_valid(ast.get("window")):
        return "invalid_derived_set_member_window"
    cardinality = ast.get("cardinality")
    if cardinality is not None:
        if not isinstance(cardinality, dict):
            return "invalid_derived_set_cardinality"
        if cardinality.get("operator") not in CARDINALITY_OPERATORS:
            return "invalid_derived_set_cardinality_operator"
        value = cardinality.get("value")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return "invalid_derived_set_cardinality_value"
        if ast.get("exists") is not True:
            return "invalid_derived_set_cardinality_membership"

    ranking = ast.get("source")
    if not isinstance(ranking, dict) or ranking.get("type") != RANKING_NODE:
        return "invalid_derived_set_ranking"
    if ranking.get("direction") not in {"top", "bottom"}:
        return "invalid_derived_set_ranking_direction"
    limit = ranking.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        return "invalid_derived_set_ranking_limit"
    if isinstance(cardinality, dict) and int(cardinality["value"]) > limit:
        return "invalid_derived_set_cardinality_exceeds_ranking_limit"

    aggregation = ranking.get("source")
    if not isinstance(aggregation, dict) or aggregation.get("type") != AGGREGATION_NODE:
        return "invalid_derived_set_aggregation"
    for field, error in (
        ("relation", "invalid_derived_set_rank_relation"),
        ("group_by", "invalid_derived_set_group_by"),
        ("measure", "invalid_derived_set_measure"),
    ):
        if not isinstance(aggregation.get(field), str) or not aggregation[field]:
            return error
    if aggregation.get("window") is not None and not _window_is_valid(aggregation.get("window")):
        return "invalid_derived_set_window"
    filters = aggregation.get("filters")
    if filters is not None:
        if not isinstance(filters, list):
            return "invalid_derived_set_filters"
        for item in filters:
            if not isinstance(item, dict) or item.get("type") != DIMENSION_FILTER_NODE:
                return "invalid_derived_set_filter"
            if not isinstance(item.get("dimension"), str) or not item["dimension"]:
                return "invalid_derived_set_filter_dimension"
            if item.get("operator") not in DIMENSION_FILTER_OPERATORS:
                return "invalid_derived_set_filter_operator"
            if not isinstance(item.get("value"), str) or not item["value"].strip():
                return "invalid_derived_set_filter_value"
    return None


def entity_set_node_from_ast(ast: Any) -> dict[str, Any] | None:
    """검증된 파생 집합 AST를 기존 컴파일러가 쓰는 평면 뷰로 투영한다."""
    if derived_set_ast_error(ast) is not None:
        return None
    ranking = ast["source"]
    aggregation = ranking["source"]
    return {
        "relation": ast["relation"],
        "rankRelation": aggregation["relation"],
        "entity": aggregation["group_by"],
        "measure": aggregation["measure"],
        "direction": ranking["direction"],
        "limit": ranking["limit"],
        "window": aggregation.get("window"),
        "memberWindow": ast.get("window"),
        "filters": [dict(item) for item in aggregation.get("filters", [])],
        "cardinality": dict(ast["cardinality"]) if isinstance(ast.get("cardinality"), dict) else None,
        "negated": not ast["exists"],
        DERIVED_SET_AST_FIELD: ast,
    }


def _normalized_entity_set_node(node: Any) -> dict[str, Any] | None:
    """AST가 있으면 그것을 우선하고, 없는 구버전 계획만 평면 포맷으로 읽는다."""
    if not isinstance(node, dict):
        return None
    if DERIVED_SET_AST_FIELD in node:
        normalized = entity_set_node_from_ast(node.get(DERIVED_SET_AST_FIELD))
        if normalized is None:
            return None
        # surface/spans/label 같은 비실행 메타데이터는 보존한다.
        for key in ("surface", "spans", "ko_label"):
            if key in node:
                normalized[key] = node[key]
        return normalized
    return node


_COMPACT_DROP_RE = re.compile(r"[\s.,!?·_\-/'\"()]")


def _compact(value: str) -> str:
    """공백·구두점을 지운 비교용 문자열(프로젝트 표면어 매칭 관례와 동일)."""
    return re.sub(r"[\s.,!?·_\-/'\"()]+", "", str(value)).casefold()


# `drop_entity_set_owned_missing_fields` 는 2026-08-02 삭제됐다 — 파생 집합이 소유한 기간/엔터티
# 결핍 보고를 semantic_ir 에서 사후에 걷어내던 sweep 이다. 결핍이 LLM 소유였기 때문에 필요했고,
# 이제는 EntitySetMembership 노드가 순위 창과 대상 엔터티를 스스로 소유하므로 그런 결핍이
# 생기지 않는다(semantic_plan.NODE_REQUIREMENTS 에 purchase_object/period 가 없다).


# `parse_entity_set_condition` 은 2026-08-02 삭제됐다 — '가장 많이 팔린 상품 N개를 구매한 회원'
# 을 원문 정규식으로 읽어 노드를 만들던 파서다. 그 의미는 SemanticPlanV2 EntitySetMembership
# 노드가 소유하고, 이 모듈에는 **컴파일러**(capability 판정·술어 생성·라벨)만 남는다.


def _rank_relation_id(node: dict[str, Any]) -> str:
    return str(node.get("rankRelation") or node.get("relation"))


def _scope_filter_specs(node: dict[str, Any], config: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """AST 범위 필터와 물리 매핑을 짝지어 반환한다. 검증 전 호출자는 빈 결과만 사용한다."""
    mappings = config.get("filters") if isinstance(config.get("filters"), dict) else {}
    result: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in node.get("filters") or []:
        if not isinstance(item, dict):
            continue
        spec = mappings.get(item.get("dimension"))
        if isinstance(spec, dict):
            result.append((item, spec))
    return result


def _sql_unicode(value: str) -> str:
    return "N" + sql_dialect.quote_literal(value)


def _scope_filter_predicate(
    item: dict[str, Any], spec: dict[str, Any], *, alias: str, product_alias: str
) -> str:
    column = str(spec["column"]).format(alias=alias, product_alias=product_alias)
    value = str(item["value"])
    if item["operator"] == "equals":
        return f"{column} = {_sql_unicode(value)}"
    return f"{column} LIKE {_sql_unicode('%' + value + '%')}"


def entity_set_capability(node: dict[str, Any], config: dict[str, Any]) -> str | None:
    """이 조합을 물리 매핑으로 낼 수 있는지. 불가하면 어느 요소가 문제인지 사유로 돌려준다.

    엔터티는 두 관계(순위를 계산하는 곳, 회원을 잇는 곳) 모두에서 참조 가능해야 한다 — 한쪽에만
    있으면 두 집합을 이을 키가 없다.
    """
    if isinstance(node, dict) and DERIVED_SET_AST_FIELD in node:
        ast_error = derived_set_ast_error(node.get(DERIVED_SET_AST_FIELD))
        if ast_error:
            return ast_error
    normalized = _normalized_entity_set_node(node)
    if normalized is None:
        return "invalid_derived_set_ast"
    node = normalized
    relations = config.get("relations") or {}
    member_relation = relations.get(node.get("relation"))
    rank_relation = relations.get(_rank_relation_id(node))
    if not isinstance(member_relation, dict) or not isinstance(rank_relation, dict):
        return "unsupported_entity_set_relation"
    entity = (config.get("entities") or {}).get(node.get("entity"))
    if not isinstance(entity, dict):
        return "unsupported_entity_set_entity"
    entity_id = str(node.get("entity"))
    for relation in (member_relation, rank_relation):
        if entity_id not in {str(item) for item in relation.get("entities", []) or []}:
            return "unsupported_entity_set_entity"
    if str(node.get("measure")) not in (rank_relation.get("measures") or {}):
        return "unsupported_entity_set_measure"
    if node.get("window") and not rank_relation.get("dateColumn"):
        return "unsupported_entity_set_period"
    if node.get("memberWindow") and not member_relation.get("dateColumn"):
        return "unsupported_entity_set_member_period"
    filter_mappings = config.get("filters") if isinstance(config.get("filters"), dict) else {}
    for item in node.get("filters") or []:
        spec = filter_mappings.get(item.get("dimension")) if isinstance(item, dict) else None
        if not isinstance(spec, dict) or not isinstance(spec.get("column"), str):
            return "unsupported_entity_set_filter_dimension"
        if item.get("operator") not in set(spec.get("operators") or DIMENSION_FILTER_OPERATORS):
            return "unsupported_entity_set_filter_operator"
        if spec.get("requiresProductJoin") and not isinstance(rank_relation.get("productJoin"), dict):
            return "unsupported_entity_set_filter_join"
    # 바깥(회원 연결)과 안쪽(순위) 스코프가 같은 별칭을 쓰면 안쪽이 바깥을 가려 조건이 조용히
    # 무의미해진다(SQL 은 유효하다). 설정 실수를 SQL 로 내보내지 않고 여기서 막는다.
    outer_aliases = {str(member_relation.get("outerAlias") or "")}
    inner_aliases = {str(rank_relation.get("innerAlias") or "")}
    if entity.get("requiresProductJoin"):
        outer_aliases.add(str((member_relation.get("productJoin") or {}).get("outerAlias") or ""))
    if entity.get("requiresProductJoin") or any(
        spec.get("requiresProductJoin") for _item, spec in _scope_filter_specs(node, config)
    ):
        inner_aliases.add(str((rank_relation.get("productJoin") or {}).get("innerAlias") or ""))
    if outer_aliases & inner_aliases:
        return "unsupported_entity_set_alias_conflict"
    return None


def entity_set_label(node: dict[str, Any], config: dict[str, Any]) -> str:
    """한글 라벨(confidence/트레이스/미지원 안내 공용)."""
    node = _normalized_entity_set_node(node) or node
    entity = (config.get("entities") or {}).get(node.get("entity")) or {}
    measure = (config.get("measures") or {}).get(node.get("measure")) or {}
    relations = config.get("relations") or {}
    relation = relations.get(node.get("relation")) or {}
    rank_relation = relations.get(_rank_relation_id(node)) or {}
    direction = "상위" if node.get("direction") == "top" else "하위"
    window = (node.get("window") or {}).get("label")
    ranking_scope = (
        f"{rank_relation.get('label', '')} " if _rank_relation_id(node) != node.get("relation") else ""
    )
    parts = [
        part for part in (
            window,
            " ".join(
                f"{item.get('value')} {(config.get('filters') or {}).get(item.get('dimension'), {}).get('label', item.get('dimension'))} 내"
                for item in node.get("filters") or []
                if isinstance(item, dict)
            ) or None,
            f"{ranking_scope}{measure.get('label', node.get('measure'))} {direction}",
            f"{node.get('limit')}개 {entity.get('label', node.get('entity'))}",
        ) if part
    ]
    member_window = (node.get("memberWindow") or {}).get("label")
    action = relation.get("label", node.get("relation"))
    if member_window:
        action = f"{member_window} {action}"
    cardinality = node.get("cardinality")
    if isinstance(cardinality, dict):
        operator_label = {
            "=": "정확히",
            ">": "초과",
            ">=": "이상",
            "<": "미만",
            "<=": "이하",
        }.get(str(cardinality.get("operator")), str(cardinality.get("operator")))
        suffix = (
            f"중 {operator_label} {cardinality.get('value')}개를 {action}한 회원"
        )
    else:
        suffix = f"{action} 안 한 회원" if node.get("negated") else f"{action}한 회원"
    return " ".join(parts) + " " + suffix


def _entity_column(entity: dict[str, Any], alias: str, product_alias: str | None) -> str:
    return str(entity["column"]).format(alias=alias, product_alias=product_alias or "")


def _window_predicate(
    window: dict[str, Any] | None,
    date_column: str,
    *,
    reference_date: ReferenceDate | None = None,
) -> str | None:
    if not isinstance(window, dict):
        return None
    start, end = window.get("from"), window.get("to")
    if isinstance(start, str) and isinstance(end, str):
        return f"{date_column} BETWEEN '{start}' AND '{end}'"
    days = window.get("days")
    if isinstance(days, int) and days > 0:
        cutoff = relative_day_char8(days, reference_date=reference_date)
        return f"{date_column} >= {sql_dialect.quote_literal(cutoff)}"
    return None


def compile_entity_set_predicate(
    node: dict[str, Any],
    config: dict[str, Any],
    member_alias: str,
    member_key: str,
    *,
    reference_date: ReferenceDate | None = None,
) -> str | None:
    """엔터티 집합 조건을 회원 기준 EXISTS/NOT EXISTS 술어로 컴파일한다.

    안쪽 서브쿼리가 순위 집합을 만들고, 바깥 EXISTS 가 그 집합과 회원을 잇는다. 회원 투영은 항상
    호출자(회원 빌더)가 소유하므로, '전제 조건만 계산하고 대상을 잊는' 형태가 구조적으로 나올 수 없다.
    """
    if entity_set_capability(node, config) is not None:
        return None
    normalized = _normalized_entity_set_node(node)
    if normalized is None:
        return None
    node = normalized
    relation = config["relations"][node["relation"]]
    rank_relation = config["relations"][_rank_relation_id(node)]
    entity = config["entities"][node["entity"]]
    outer = str(relation["outerAlias"])
    inner = str(rank_relation["innerAlias"])
    needs_outer_product = bool(entity.get("requiresProductJoin"))
    filter_specs = _scope_filter_specs(node, config)
    needs_inner_product = needs_outer_product or any(
        spec.get("requiresProductJoin") for _item, spec in filter_specs
    )
    outer_join = relation.get("productJoin") if isinstance(relation.get("productJoin"), dict) else None
    inner_join = rank_relation.get("productJoin") if isinstance(rank_relation.get("productJoin"), dict) else None
    outer_product = str((outer_join or {}).get("outerAlias") or "")
    inner_product = str((inner_join or {}).get("innerAlias") or "")

    inner_lines = [f"SELECT TOP {int(node['limit'])} {_entity_column(entity, inner, inner_product)}"]
    inner_lines.append(f"FROM {rank_relation['table']} {inner}")
    if needs_inner_product and inner_join:
        inner_lines.append(
            f"     INNER JOIN {inner_join['table']} {inner_product} "
            + f"ON {str(inner_join['on']).format(alias=inner, product_alias=inner_product)}"
        )
    inner_where = [
        str(condition).format(alias=inner, product_alias=inner_product)
        for condition in rank_relation.get("conditions", []) or []
    ]
    try:
        window_predicate = _window_predicate(
            node.get("window"),
            str(rank_relation.get("dateColumn", "")).format(alias=inner),
            reference_date=reference_date,
        )
    except ReferenceTimeError:
        # This compiler's public failure contract is ``None``.  Omitting the
        # unresolved relative predicate would silently widen the population.
        return None
    if window_predicate:
        inner_where.append(window_predicate)
    inner_where.extend(
        _scope_filter_predicate(item, spec, alias=inner, product_alias=inner_product)
        for item, spec in filter_specs
    )
    if inner_where:
        inner_lines.append("WHERE " + " AND ".join(inner_where))
    inner_column = _entity_column(entity, inner, inner_product)
    measure_expression = str(rank_relation["measures"][node["measure"]]).format(
        alias=inner, product_alias=inner_product
    )
    order = "DESC" if node.get("direction") != "bottom" else "ASC"
    inner_lines.append(f"GROUP BY {inner_column}")
    # 동점 시 결과가 흔들리지 않도록 엔터티 키로 결정론 정렬을 덧붙인다(집계 랭킹 계약과 동일).
    inner_lines.append(f"ORDER BY {measure_expression} {order}, {inner_column} ASC")

    cardinality = node.get("cardinality")
    outer_select = (
        f"SELECT COUNT(DISTINCT {_entity_column(entity, outer, outer_product)})"
        if isinstance(cardinality, dict)
        else "SELECT 1"
    )
    outer_lines = [outer_select, f"FROM {relation['table']} {outer}"]
    if needs_outer_product and outer_join:
        outer_lines.append(
            f"     INNER JOIN {outer_join['table']} {outer_product} "
            + f"ON {str(outer_join['on']).format(alias=outer, product_alias=outer_product)}"
        )
    outer_where = [str(relation["memberJoin"]).format(alias=outer, member=f"{member_alias}.{member_key}")]
    outer_where.extend(
        str(condition).format(alias=outer, product_alias=outer_product)
        for condition in relation.get("conditions", []) or []
    )
    # 회원 행동 기간은 **바깥** 스코프에 건다. 안쪽(랭킹) 창과 같은 자리에 두면 '작년에 팔린
    # 상품을 올해 산 회원'이 '작년 판매 상위 + 아무 때나 구매'로 넓어진다.
    try:
        member_window_predicate = _window_predicate(
            node.get("memberWindow"),
            str(relation.get("dateColumn", "")).format(alias=outer),
            reference_date=reference_date,
        )
    except ReferenceTimeError:
        return None
    if member_window_predicate:
        outer_where.append(member_window_predicate)
    indented_inner = "\n".join("          " + line for line in inner_lines).lstrip()
    outer_where.append(f"{_entity_column(entity, outer, outer_product)} IN (\n          {indented_inner}\n      )")
    outer_lines.append("WHERE " + "\n      AND ".join(outer_where))

    body = "\n".join("    " + line for line in outer_lines)
    if isinstance(cardinality, dict):
        operator = str(cardinality["operator"])
        value = int(cardinality["value"])
        return f"(\n{body}\n  ) {operator} {value}"
    keyword = "NOT EXISTS" if node.get("negated") else "EXISTS"
    return f"{keyword} (\n{body}\n  )"
