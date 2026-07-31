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
from datetime import date
from typing import Any

import lexicon_patterns
from calendar_window import parse_time_window, parse_time_window_span
from common_utils import compact_index_map, raw_span


_MEMBER_NOUN_RE = lexicon_patterns.pattern("purchase_rank_target")
_COUNT_AFTER_RE = re.compile(r"^(\d{1,4})\s*(?:개|종|가지|건|위)")
# '상위 5개 카테고리'처럼 개수가 엔터티 앞에 오는 어순.
_COUNT_BEFORE_RE = re.compile(r"(\d{1,4})\s*(?:개|종|가지|건|위)\s*$")
_COUNT_SEARCH_RE = re.compile(r"(\d{1,4})\s*(?:개|종|가지|건|위)")
# 랭킹으로 만든 유한 집합과 회원 행동 집합의 교집합 크기.
# ``상위 3개 중에서 2개만 구매``처럼 모집단 크기와 회원별 일치 개수가 함께 있는 문법만 읽는다.
# 단독 ``2개 구매``는 상품 수량일 수 있으므로 이 파서가 소유하지 않는다.
_SET_SCOPE_ALT = lexicon_patterns.alternation("clause_scope_marker")
_SET_CARDINALITY_RE = re.compile(
    rf"(?:{_SET_SCOPE_ALT})"
    r"(\d{1,4})\s*(?:개|종|가지)"
    r"(만|이상|이하|초과|미만)?"
)
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
    filters: list[dict[str, Any]] | None = None,
    cardinality: dict[str, Any] | None = None,
    negated: bool = False,
) -> dict[str, Any]:
    """평면 파싱 결과를 ``집계 → 랭킹 → 회원 집합`` AST로 만든다.

    단계별 책임을 섞지 않는다. 특히 순위를 계산하는 관계와 그 결과 엔터티를 회원에게 연결하는
    관계가 다를 수 있으므로 두 관계는 각각 aggregation/member_set 노드가 소유한다.
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
    if isinstance(cardinality, dict):
        member_set["cardinality"] = {
            "operator": cardinality.get("operator"),
            "value": cardinality.get("value"),
        }
    return member_set


def derived_set_ast_error(ast: Any) -> str | None:
    """파생 집합 AST의 닫힌 문법을 검사하고 안정적인 오류 코드를 반환한다."""
    if not isinstance(ast, dict) or ast.get("type") != MEMBER_SET_NODE:
        return "invalid_derived_set_ast_root"
    if not isinstance(ast.get("relation"), str) or not ast["relation"]:
        return "invalid_derived_set_member_relation"
    if not isinstance(ast.get("exists"), bool):
        return "invalid_derived_set_membership"
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
    window = aggregation.get("window")
    if window is not None:
        if not isinstance(window, dict):
            return "invalid_derived_set_window"
        absolute = isinstance(window.get("from"), str) and isinstance(window.get("to"), str)
        days = window.get("days")
        relative = isinstance(days, int) and not isinstance(days, bool) and days > 0
        if not absolute and not relative:
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


def _compact_map(value: str) -> tuple[str, list[int]]:
    """``_compact`` 와 같은 문자열 + compact 인덱스 → 원문 인덱스 대응표.

    이 절이 원문의 **어느 구간**을 읽었는지 호출자에게 돌려주려면(소유권 회수가 '같은 종류'가
    아니라 '같은 구간'으로 판정되게 하려면) compact 좌표를 원문 좌표로 되돌릴 수 있어야 한다.
    되돌림 규약 자체는 common_utils 가 소유한다 — 같은 규약을 쓰는 파서가 둘 이상이다."""
    return compact_index_map(value, _COMPACT_DROP_RE)


def _raw_span(index_map: list[int], start: int | None, end: int | None) -> list[int] | None:
    """compact 좌표 [start, end) → 원문 좌표 [start, end). 범위 밖이면 None."""
    return raw_span(index_map, start, end)


def _find_term(compact: str, terms: Any, start: int = 0) -> tuple[int, int, str] | None:
    """terms 중 start 이후 가장 먼저 나오는 표면어의 (시작, 끝, 표면어). 같은 위치면 긴 것 우선."""
    best: tuple[int, int, str] | None = None
    for term in terms or []:
        needle = _compact(term)
        if not needle:
            continue
        position = compact.find(needle, start)
        if position < 0:
            continue
        candidate = (position, position + len(needle), str(term))
        if best is None or (candidate[0], -candidate[1]) < (best[0], -best[1]):
            best = candidate
    return best


def _match_direction(compact: str, config: dict[str, Any]) -> tuple[int, int, str] | None:
    """상위/하위 방향 표지. 하위 표지가 상위 표지의 부분문자열이 되는 경우가 없도록 둘 다 훑는다."""
    found: list[tuple[int, int, str]] = []
    for direction, terms in (config.get("directions") or {}).items():
        hit = _find_term(compact, terms)
        if hit is not None:
            found.append((hit[0], hit[1], str(direction)))
    if not found:
        return None
    # 가장 먼저 등장한 방향 표지가 이 절의 방향이다.
    return min(found, key=lambda item: (item[0], -item[1]))


def _match_entity_before(
    compact: str, config: dict[str, Any], end: int
) -> tuple[str, int, int] | None:
    """``상품 중 많이 팔린``처럼 방향 표지 앞에 놓인 엔터티를 찾는다.

    방향 표지에서 가장 가까운 엔터티만 고른다. ``카테고리가 X인 상품 중 ...``에서 앞의
    ``카테고리``가 아니라 실제 랭킹 단위인 ``상품``을 선택하기 위한 규칙이다. 엔터티와 방향 사이에는
    집합 범위를 나타내는 짧은 연결어만 허용해 앞 절의 우연한 명사를 가져오지 않는다.
    """
    found: list[tuple[int, int, str]] = []
    for entity_id, spec in (config.get("entities") or {}).items():
        if not isinstance(spec, dict):
            continue
        for term in spec.get("terms") or []:
            needle = _compact(term)
            if not needle:
                continue
            position = compact.rfind(needle, 0, end)
            if position < 0:
                continue
            entity_end = position + len(needle)
            bridge = compact[entity_end:end]
            if len(bridge) <= 8 and re.fullmatch(r"(?:중(?:에서|의)?|가운데|내에서|에서|중에)?", bridge):
                found.append((position, entity_end, str(entity_id)))
    if not found:
        return None
    position, entity_end, entity_id = max(found, key=lambda item: (item[1], item[0]))
    return entity_id, position, entity_end


def _match_window_span(compact: str) -> list[int] | None:
    """``_match_window`` 가 고른 창의 compact 좌표 구간. 절대 달력 창과 과거 시점만 위치를 안다.

    롤링 기간('최근 3개월')은 선택 규칙이 앵커 근접성까지 보므로 위치를 단정하지 않는다 —
    구간을 모르면 소유권 회수는 기존(종류 기준) 동작으로 남는다(fail-safe)."""
    span = parse_time_window_span(compact)
    return [span[0], span[1]] if span is not None else None


def _match_window(
    compact: str, rank_marker: str = "", *, today: date | None = None
) -> dict[str, Any] | None:
    """절 앞머리의 기간 표현. 달력 표현은 절대창, 롤링 기간은 상대창.

    문법도 창 종류 간 우선순위도 calendar_window 가 소유한다 — 여기서 별도 정규식을 갖고 있던 동안
    '2019년 3월'의 월이 통째로 사라져 그 달 순위가 연간 순위로 바뀌었고, 우선순위를 따로 갖고 있던
    동안 '7년전 상반기'가 순위 창에서 반기를 잃었다.

    롤링 기간은 방향 표지(rank_marker, 예: '가장많이') 근처의 것만 본다 — 앞 절에 자기 기간을 가진 다른
    조건이 있으면('2주 이내 가입한 회원 중 가장 많이 팔린 …') 그 창을 순위 창으로 훔쳐오게 된다."""
    return parse_time_window(
        compact,
        today=today,
        include_duration=True,
        duration_anchor_terms=(rank_marker,) if rank_marker else None,
    )


def parse_entity_set_condition(
    query: str,
    config: dict[str, Any] | None,
    *,
    today: date | None = None,
) -> dict[str, Any] | None:
    """엔터티 랭킹 결과를 구매/장바구니 회원 집합으로 잇는 표현을 읽는다.

    정방향(``가장 많이 팔린 상품 5개``)과 한국어에서 흔한 범위 선행형
    (``상품 중 많이 팔린 5개``)을 모두 받는다. 방향·엔터티·회원 관계 중 하나라도 없으면 ``None`` —
    불완전한 랭킹을 단순 리터럴 구매 조건으로 추측하지 않는다.
    """
    if not isinstance(config, dict) or not isinstance(query, str) or not query.strip():
        return None
    compact, index_map = _compact_map(query)
    if not _MEMBER_NOUN_RE.search(compact):
        return None

    direction = _match_direction(compact, config)
    if direction is None:
        return None
    entity = _match_entity(compact, config, direction[1])
    reverse_order = entity is None
    if reverse_order:
        entity = _match_entity_before(compact, config, direction[0])
        if entity is None:
            return None
    entity_id, entity_start, entity_end = entity
    entity_noun_end = entity_end

    limit = DEFAULT_LIMIT
    count_span: tuple[int, int] | None = None
    relation_search_start = entity_end
    if reverse_order:
        count = _COUNT_SEARCH_RE.search(compact, direction[1])
        if count is not None:
            limit = max(1, min(MAX_LIMIT, int(count.group(1))))
            count_span = (count.start(), count.end())
            relation_search_start = count.end()
    else:
        count = _COUNT_AFTER_RE.match(compact[entity_end:])
        if count is not None:
            limit = max(1, min(MAX_LIMIT, int(count.group(1))))
            count_span = (entity_end + count.start(), entity_end + count.end())
            relation_search_start = entity_end + count.end()
        else:
            count = _COUNT_BEFORE_RE.search(compact[direction[0]: entity_start])
            if count is not None:
                limit = max(1, min(MAX_LIMIT, int(count.group(1))))
                count_span = (direction[0] + count.start(), direction[0] + count.end())

    relation = _match_relation(compact, config, relation_search_start)
    if relation is None:
        return None
    relation_id, negated, relation_start, relation_end = relation
    cardinality: dict[str, Any] | None = None
    cardinality_span: tuple[int, int] | None = None
    cardinality_match = _SET_CARDINALITY_RE.search(
        compact,
        count_span[1] if count_span is not None else entity_noun_end,
        relation_start,
    )
    if cardinality_match is not None:
        cardinality = {
            "operator": _CARDINALITY_OPERATOR[cardinality_match.group(2)],
            "value": int(cardinality_match.group(1)),
        }
        cardinality_span = cardinality_match.span()
    # 순위를 계산하는 관계와 회원을 잇는 관계는 다를 수 있다("가장 많이 *장바구니에 담은* 상품을
    # *구매한* 고객"). 엔터티 앞의 관계 표현이 순위 관계, 뒤의 것이 회원 연결 관계다.
    # 순위 관계가 문장에 없으면('가장 많이 팔린 상품') 판매 실적 관계가 기본이다 — 회원 연결 관계를
    # 그대로 쓰면 '장바구니에 담은 회원'에서 순위까지 장바구니 기준으로 조용히 바뀐다.
    rank_relation_end = relation_start if reverse_order else entity_start
    ranking = _match_relation(compact, config, direction[0], end=rank_relation_end)
    rank_relation_id = ranking[0] if ranking is not None else str(config.get("defaultRankRelation") or relation_id)

    measure_end = relation_start if reverse_order else entity_start
    measure_id = _match_measure(compact, config, direction[0], measure_end)
    # 기간은 이 절(순위 계산)의 것이다 — 엔터티 앞에 있는 기간 표현만 가져간다.
    # '2019년 가장 많이 팔린 상품을 구매한 고객'에서 2019년은 판매 순위의 창이지 구매 시점이 아니다.
    window = _match_window(
        compact[: entity_start],
        compact[direction[0]: direction[1]],
        today=today,
    )
    window_span = _match_window_span(compact[: entity_start]) if window else None

    # 이 절이 원문의 어느 구간을 읽었는지(요소별). 소유권 회수가 '조건의 종류'가 아니라 '문장의
    # 같은 구간'으로 판정되게 하는 근거다 — 순위 절의 기간이 뒤쪽 절의 구매 기간까지 지우는 사고를
    # 막는다. 위치를 단정할 수 없는 요소(상대 기간 등)는 None 이고, 그때는 기존 동작이 유지된다.
    spans = {
        "clause": _raw_span(
            index_map,
            min(entity_start, direction[0], window_span[0] if window_span else direction[0]),
            relation_end,
        ),
        "window": _raw_span(index_map, window_span[0], window_span[1]) if window_span else None,
        "entity": _raw_span(index_map, entity_start, entity_noun_end),
        "count": _raw_span(index_map, count_span[0], count_span[1]) if count_span else None,
        "cardinality": (
            _raw_span(index_map, cardinality_span[0], cardinality_span[1])
            if cardinality_span else None
        ),
        "relation": _raw_span(index_map, relation_start, relation_end),
    }

    node: dict[str, Any] = {
        "relation": relation_id,
        "rankRelation": rank_relation_id,
        "entity": entity_id,
        "measure": measure_id,
        "direction": direction[2],
        "limit": limit,
        "window": window,
        "cardinality": cardinality,
        "negated": negated,
        "surface": query.strip(),
        "spans": spans,
    }
    node[DERIVED_SET_AST_FIELD] = build_derived_set_ast(
        member_relation=relation_id,
        rank_relation=rank_relation_id,
        entity=entity_id,
        measure=measure_id,
        direction=direction[2],
        limit=limit,
        window=window,
        cardinality=cardinality,
        negated=negated,
    )
    node["unsupported_reason"] = entity_set_capability(node, config)
    return node


def _match_entity(compact: str, config: dict[str, Any], start: int) -> tuple[str, int, int] | None:
    found: list[tuple[int, int, str]] = []
    for entity_id, spec in (config.get("entities") or {}).items():
        if not isinstance(spec, dict):
            continue
        hit = _find_term(compact, spec.get("terms"), start)
        if hit is not None:
            found.append((hit[0], hit[1], str(entity_id)))
    if not found:
        return None
    position, end, entity_id = min(found, key=lambda item: (item[0], -item[1]))
    return entity_id, position, end


def _match_relation(
    compact: str, config: dict[str, Any], start: int, end: int | None = None
) -> tuple[str, bool, int, int] | None:
    """[start, end) 구간의 관계 동사 → (관계, 부정여부, 시작, 끝). 부정형('구매하지 않은')을 먼저 본다.

    위치까지 돌려주는 이유: 이 절이 문장의 어디서 끝나는지가 소유권 구간의 경계이기 때문이다."""
    span = compact[start: end] if end is not None else compact[start:]
    found: list[tuple[int, int, str, bool]] = []
    for relation_id, spec in (config.get("relations") or {}).items():
        if not isinstance(spec, dict):
            continue
        negative = _find_term(span, spec.get("negationTerms"))
        if negative is not None:
            found.append((negative[0], negative[1], str(relation_id), True))
            continue
        positive = _find_term(span, spec.get("terms"))
        if positive is not None:
            found.append((positive[0], positive[1], str(relation_id), False))
    if not found:
        return None
    position, position_end, relation_id, negated = min(found, key=lambda item: item[0])
    return relation_id, negated, start + position, start + position_end


def _match_measure(compact: str, config: dict[str, Any], direction_start: int, entity_start: int) -> str:
    """순위 기준 지표. 방향 표지와 엔터티 사이의 표면어가 지표를 정한다(없으면 기본 지표)."""
    span = compact[max(0, direction_start - 12): entity_start]
    found: list[tuple[int, str]] = []
    for measure_id, spec in (config.get("measures") or {}).items():
        if not isinstance(spec, dict):
            continue
        hit = _find_term(span, spec.get("terms"))
        if hit is not None:
            found.append((hit[0], str(measure_id)))
    if found:
        return min(found, key=lambda item: item[0])[1]
    return str(config.get("defaultMeasure") or "sales_quantity")


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
    action = relation.get("label", node.get("relation"))
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


def _window_predicate(window: dict[str, Any] | None, date_column: str) -> str | None:
    if not isinstance(window, dict):
        return None
    start, end = window.get("from"), window.get("to")
    if isinstance(start, str) and isinstance(end, str):
        return f"{date_column} BETWEEN '{start}' AND '{end}'"
    days = window.get("days")
    if isinstance(days, int) and days > 0:
        return f"{date_column} >= CONVERT(CHAR(8), DATEADD(DAY, -{days}, GETDATE()), 112)"
    return None


def compile_entity_set_predicate(
    node: dict[str, Any],
    config: dict[str, Any],
    member_alias: str = "B",
    member_key: str = "MEMBER_NO",
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
    window_predicate = _window_predicate(
        node.get("window"), str(rank_relation.get("dateColumn", "")).format(alias=inner)
    )
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
