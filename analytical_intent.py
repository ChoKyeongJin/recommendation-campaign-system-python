"""Natural-language analytical intent parsing and deterministic SQL compilation.

The targeting pipeline historically treated words such as ``VIP`` or ``여성`` as
proof that a query wanted a member list, even when the requested output was an
aggregate.  This module keeps output shape, metric, dimensions, scopes, and
filters in a separate registry-backed contract so audience filters cannot
replace a measure.

Physical capability is *derived*, never enumerated by hand.  A dimension or
filter declares one logical field; whether a given aggregate source can serve it
follows from that source's own table and declared joins.  A behavioural
population scope (``구매한 회원``, ``캠페인에 반응한 회원``) declares one
predicate and is either provided inherently by the source table or compiled as
an EXISTS/NOT EXISTS against the source's member key.  Adding a source therefore
adds every dimension it can reach, and adding a dimension serves every source
that can reach it — the combination matrix is computed instead of maintained.

When a request cannot be served, the failure names the offending element
(dimension, scope, filter, function, period) instead of collapsing into one
opaque "unsupported contract" code.
"""

from __future__ import annotations

import sql_dialect

import functools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

import lexicon_llm
import lexicon_patterns
from common_utils import compact_index_map, raw_span
from sql_ast import SelectAst
from member_policy import (
    active_member_filter,
    member_activity_filter,
    member_eq_filter,
    resolve_member_scope,
)


DEFAULT_ANALYTICS_REGISTRY_PATH = Path("docs/data/analytics_registry.json")

# ── 집계 표면어의 동결 백스톱 ──────────────────────────────────────────────────────────────
# 집계 함수어("총액"→SUM)와 미지원 한정어("미래 예상")는 위치를 쓰지 않는 순수 불리언/최장일치
# 판정이라 표면어 소유권을 LLM 으로 옮겼다(surface_concepts.json 의 aggregate_* / unsupported_metric_qualifier).
# 아래는 이관 시점 낱말을 그대로 옮긴 백스톱이며, 키가 없는 환경의 재현이 유일한 역할이다 —
# 손으로 늘리지 않는다. 레지스트리 JSON 에 같은 키가 남아 있으면 그쪽이 이긴다(구버전 호환).
_AGGREGATE_FUNCTION_BACKSTOP: dict[str, tuple[str, ...]] = {
    "SUM": ("합계", "총액", "총합", "전체 금액", "전체 구매 금액", "총 구매금액", "합산", "얼마"),
    "COUNT": ("건수", "개수", "몇 건"),
    "AVG": ("평균",),
    "MAX": ("최대", "최고", "가장 최근", "최근"),
    "MIN": ("최소", "최저", "가장 오래된", "오래된"),
}
_UNSUPPORTED_QUALIFIER_BACKSTOP: tuple[str, ...] = (
    "미래 예상", "미래예상", "예측 구매", "예상 구매",
)

_OUTPUT_ACTION_RE = re.compile(r"알려|보여|조회|계산|구해|집계")
_TARGETING_COMPARISON_RE = re.compile(
    # 수치 지표뿐 아니라 구매주기·경과일 같은 기간형 회원 속성도 같은 선택 술어다.
    # 단위/비교어를 여기에 열어두면 ``회원별 평균 <속성>이 30일 이내``의 ``평균``을
    # 결과 집계 AVG로 오인하지 않고 회원 행 선택 경로에 양보한다.
    r"(?:\d[\d,]*(?:\.\d+)?\s*(?:원|건|회|개|명|일|주|주일|개월|달|년)?\s*"
    r"(?:이상|이하|초과|미만|이내|이전|이후|같|넘)|"
    r"상위|하위|높은|낮은|많은|적은)"
)
_RECENT_DAYS_RE = re.compile(r"최근\s*(\d+)\s*일(?:간|동안|이내)?")
_RECENT_WINDOW_RE = re.compile(r"최근\s*\d+\s*(?:일|주|개월|달|년)(?:간|동안|이내)?")
_MEMBER_TARGET_RE = lexicon_patterns.pattern("member_noun_core")
_RANKING_HIGH_RE = re.compile(r"가장\s*(?:많이|많은)|최다|최고")
_RANKING_LOW_RE = re.compile(r"가장\s*(?:적게|적은)|최소|최저")
# A value followed by a member noun is an audience predicate (``0원인 회원``),
# not a request for a scalar aggregate merely because the phrase also says
# ``합계``/``건수``.  The legacy targeting compiler owns these predicates.
_MEMBER_VALUE_PREDICATE_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:원|건|회|번|개|명)?\s*"
    r"(?:인|인\s*회원|인\s*고객|이거나|거나|없거나)"
)
_MEMBER_METRIC_COMPARISON_RE = re.compile(r"보다|초과한|미만인|이상인|이하인|같은")
_MEMBER_NUMERIC_PREDICATE_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:원|건|회|번|개|명)?[^.!?]{0,30}"
    r"(?:이상|이하|초과|미만|사이|범위|인\s*(?:회원|고객|사용자))"
)
_EXPLICIT_MEMBER_LIMIT_RE = re.compile(r"\b(\d[\d,]*)\s*명")

SUPPORTED_QUERY_TYPES = frozenset({"aggregate", "grouped_aggregate", "ranking", "member_selection"})
SUPPORTED_RESULT_SHAPES = frozenset({"scalar", "grouped_rows", "single_member", "member_rows"})

# 미지원 사유별 사용자 안내. 사유 코드는 실패한 요소(차원/스코프/필터/함수/기간)를 가리키며,
# 응답·트레이스·테스트가 모두 같은 코드를 본다.
UNSUPPORTED_CLARIFICATIONS: dict[str, str] = {
    "unresolved_aggregate_metric": "계산할 지표를 스키마에 있는 항목으로 지정해 주세요.",
    "unsupported_aggregate_function": "이 지표에서 사용할 수 있는 집계 방식(합계/평균/최대/최소/건수)으로 바꿔 주세요.",
    "unsupported_group_dimension": "그룹 기준을 이 지표가 지원하는 축으로 바꾸거나, 그룹 없이 전체 값을 요청해 주세요.",
    "unsupported_metric_scope": "요청하신 회원 행동 조건을 이 지표로는 집계할 수 없습니다. 조건을 빼거나 대상 지표를 바꿔 주세요.",
    "unsupported_metric_filter": "요청하신 회원 조건을 이 지표로는 필터링할 수 없습니다.",
    "unsupported_relative_period": "이 지표에는 기간 기준 컬럼이 없습니다. 기간 조건 없이 요청해 주세요.",
    "unsupported_aggregate_metric_qualifier": "요청한 예측/미래 지표는 현재 스키마에서 안전하게 계산할 수 없습니다.",
    "unsupported_ranking_metric": "요청한 지표로 회원 순위를 계산할 스키마 매핑이 없습니다.",
    "analytical_signal_dropped": "질문에 있는 조건을 집계 SQL로 옮기지 못했습니다. 조건을 나눠서 다시 요청해 주세요.",
}

# 어느 요소가 문제인지 하나만 보고할 때의 우선순위. 그룹 축이 없으면 나머지는 부차적이다.
_REJECTION_PRIORITY = (
    "unsupported_group_dimension",
    "unsupported_metric_scope",
    "unsupported_metric_filter",
    "unsupported_relative_period",
    "unsupported_aggregate_function",
)


_COMPACT_DROP_RE = re.compile(r"[\s.,!?·_\-/]")

# 이 해석을 유발한 표면 근거(원문 좌표). 호출자는 이 구간으로 "같은 어구를 이미 다른 조건이
# 소유했는가"를 판정한다 — 근거 없이 종류만 보고 막으면 문장의 다른 절이 요구한 집계까지 죽는다.
EVIDENCE_KEY = "evidence"


def _compact(value: str) -> str:
    return re.sub(r"[\s.,!?·_\-/]+", "", value).casefold()


def _compact_map(value: str) -> tuple[str, list[int]]:
    """``_compact`` 와 같은 문자열 + compact 좌표 → 원문 좌표 대응표."""
    return compact_index_map(value, _COMPACT_DROP_RE)


def _contains(text: str, term: str) -> bool:
    return _compact(term) in text


@functools.lru_cache(maxsize=4)
def load_analytics_registry(path_text: str = str(DEFAULT_ANALYTICS_REGISTRY_PATH)) -> dict[str, Any]:
    path = Path(path_text)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# 물리 매핑 해석 — 논리 필드 1개 선언을 소스별 표현식으로 계산한다.
# ---------------------------------------------------------------------------


def _source_table(source: dict[str, Any]) -> str:
    return str(source.get("table") or "")


def _source_joins(source: dict[str, Any]) -> list[dict[str, Any]]:
    return [join for join in source.get("joins", []) or [] if isinstance(join, dict)]


def _resolve_field_for_source(field_spec: Any, source: dict[str, Any]) -> dict[str, Any] | None:
    """Bind a logical field declaration to one source's alias, or return ``None``.

    A field lives on the source table itself or on a table the source already
    declares a join to.  Nothing else is guessed: an unreachable table means the
    source genuinely cannot serve that dimension/filter.
    """
    if not isinstance(field_spec, dict) or not field_spec.get("column"):
        return None
    table = str(field_spec.get("table") or "")
    resolved = dict(field_spec)
    if table.casefold() == _source_table(source).casefold():
        resolved["expression"] = f"{source.get('alias')}.{field_spec['column']}"
        resolved["dependencies"] = []
        return resolved
    for join in _source_joins(source):
        if str(join.get("table") or "").casefold() == table.casefold():
            resolved["expression"] = f"{join.get('alias')}.{field_spec['column']}"
            resolved["dependencies"] = [str(join.get("id"))]
            return resolved
    return None


def _dimension_spec(registry: dict[str, Any], dimension_id: str) -> dict[str, Any]:
    spec = (registry.get("dimensions") or {}).get(dimension_id)
    return spec if isinstance(spec, dict) else {}


def _dimension_mapping(registry: dict[str, Any], source: dict[str, Any], dimension_id: str) -> dict[str, Any] | None:
    """Physical mapping of a group axis for one source (explicit override wins)."""
    spec = _dimension_spec(registry, dimension_id)
    if not spec:
        return None
    explicit = (spec.get("mappings") or {}).get(str(source.get("id")))
    if isinstance(explicit, dict):
        return dict(explicit)
    if spec.get("useSourceMember"):
        member = source.get("memberField")
        if not isinstance(member, dict):
            return None
        mapping = dict(member)
        mapping.setdefault("outputAlias", member.get("column"))
        mapping.setdefault("dependencies", [])
        return mapping
    mapping = _resolve_field_for_source(spec.get("field"), source)
    if mapping is None:
        return None
    mapping.setdefault("outputAlias", mapping.get("column"))
    if spec.get("semanticType"):
        mapping.setdefault("semanticType", spec["semanticType"])
    return mapping


def resolve_dimension_mapping(
    registry: dict[str, Any], source: dict[str, Any] | None, dimension_id: str
) -> dict[str, Any] | None:
    """Public accessor for the physical mapping of a group axis (result profiling, tracing)."""
    if not isinstance(source, dict):
        return None
    return _dimension_mapping(registry, source, dimension_id)


def _filter_spec(registry: dict[str, Any], filter_id: str) -> dict[str, Any]:
    spec = (registry.get("filters") or {}).get(filter_id)
    return spec if isinstance(spec, dict) else {}


def _filter_field(spec: dict[str, Any]) -> tuple[dict[str, Any] | None, str, Any]:
    """Return (field, operator, value) for a filter, resolving member-policy references.

    ``memberFilter`` points at a canonical entry of ``member_target_filters.json``
    so the physical column and stored value stay in one registry for targeting and
    analytics alike.
    """
    field_spec = spec.get("field") if isinstance(spec.get("field"), dict) else None
    operator = str(spec.get("operator") or "eq")
    value = spec.get("value")
    canonical = spec.get("memberFilter")
    if isinstance(canonical, str) and canonical:
        shared = member_eq_filter(canonical)
        if shared is None:
            return None, operator, value
        field_spec = {
            "entity": (field_spec or {}).get("entity", "member"),
            "field": (field_spec or {}).get("field", canonical),
            "table": shared["table"],
            "column": shared["column"],
        }
        value = shared["value"] if value is None else value
    return field_spec, operator, value


def _filter_mapping(
    registry: dict[str, Any], source: dict[str, Any], filter_id: str, inline: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    spec = inline if isinstance(inline, dict) else _filter_spec(registry, filter_id)
    if not spec:
        return None
    explicit = (spec.get("mappings") or {}).get(str(source.get("id")))
    if isinstance(explicit, dict):
        mapping = dict(explicit)
        mapping.setdefault("operator", "eq")
        return mapping
    field_spec, operator, value = _filter_field(spec)
    mapping = _resolve_field_for_source(field_spec, source)
    if mapping is None:
        return None
    mapping["operator"] = operator
    mapping["value"] = value
    if spec.get("includeNull"):
        mapping["includeNull"] = True
    return mapping


def member_condition_filter(canonical: str) -> dict[str, Any] | None:
    """Build an analytics filter spec from the shared member condition registries.

    Audience canonicals (``vip``/``employee``/``inactive_90d``/``recent_login``…)
    already carry a physical definition in ``member_target_filters.json``.  An
    aggregate over the same population must reuse it rather than fail closed or,
    worse, count a different population.
    """
    equality = member_eq_filter(canonical)
    if equality is not None:
        return {
            "label": canonical,
            "memberFilter": canonical,
            "category": equality["category"],
            "field": {"entity": "member", "field": canonical},
        }
    activity = member_activity_filter(canonical)
    if activity is None or activity.get("days") is None:
        return None
    operator = {"<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}.get(str(activity["operator"]), "lt")
    return {
        "label": canonical,
        "category": "activity",
        "field": {
            "entity": "member", "field": canonical,
            "table": activity["table"], "column": activity["column"],
        },
        "operator": operator,
        "value": _relative_period(int(activity["days"])),
        "includeNull": bool(activity["include_null"]),
    }


def _filter_specs(filters: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any] | None]]:
    """(filter id, inline spec) pairs. ``recent_days`` is a period, handled separately."""
    return [
        (str(item["id"]), item.get("spec") if isinstance(item.get("spec"), dict) else None)
        for item in filters
        if isinstance(item, dict) and item.get("id") and item.get("id") != "recent_days"
    ]


def _scopes(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return [scope for scope in registry.get("memberScopes", []) or [] if isinstance(scope, dict)]


def _scope_spec(registry: dict[str, Any], scope_id: str) -> dict[str, Any]:
    return next((scope for scope in _scopes(registry) if str(scope.get("id")) == scope_id), {})


def _scope_binding(scope: dict[str, Any], source: dict[str, Any], negated: bool) -> dict[str, Any] | None:
    """Decide how one source can carry a population scope.

    ``inherent`` — the source rows already *are* the scoped population (counting
    members on the order table is counting purchasers).  ``exists``/``column`` —
    the scope is compiled as a predicate against the source's member key.  A
    negated scope can never be inherent: absence cannot be read off the rows that
    represent presence.
    """
    scope_id = str(scope.get("id"))
    if not negated and scope_id in {str(item) for item in source.get("providesScopes", []) or []}:
        return {"mode": "inherent", "scope": scope, "negated": False}
    host_tables = {str(table).casefold() for table in scope.get("hostTables", []) or []}
    if host_tables and _source_table(source).casefold() not in host_tables:
        return None
    if str(scope.get("type") or "exists") == "column":
        return {"mode": "column", "scope": scope, "negated": negated}
    if not isinstance(source.get("memberField"), dict):
        return None
    return {"mode": "exists", "scope": scope, "negated": negated}


# ---------------------------------------------------------------------------
# 자연어 감지
# ---------------------------------------------------------------------------


def _matches_required_terms(compact: str, spec: dict[str, Any]) -> bool:
    required = spec.get("requiredTerms") or []
    return all(any(_contains(compact, term) for term in group) for group in required if isinstance(group, list))


def _match_metric_evidence(compact: str, registry: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """(지표, 그 판정의 근거 표면형). 근거는 compact 좌표계 문자열이다."""
    metrics = [item for item in registry.get("metrics", []) if isinstance(item, dict)]
    metrics.sort(key=lambda item: int(item.get("priority", 0)), reverse=True)
    for metric in metrics:
        terms = [str(term) for term in metric.get("terms") or []]
        matched = [term for term in terms if _contains(compact, term)]
        if matched and _matches_required_terms(compact, metric):
            return metric, max((_compact(term) for term in matched), key=len)
    return None, None


def _match_metric(compact: str, registry: dict[str, Any]) -> dict[str, Any] | None:
    return _match_metric_evidence(compact, registry)[0]


def _match_aggregate_function_evidence(compact: str, registry: dict[str, Any]) -> tuple[str | None, str | None]:
    """(집계 함수, 그 판정의 근거 표면형) — 동결 백스톱 최장일치가 먼저, 침묵하면 LLM 이 읽은 함수.

    최장일치를 유지하는 이유: '총 구매금액'이 '합계'보다 구체적이듯 긴 표현이 짧은 표현을 이겨야
    한다. LLM 은 백스톱이 아무것도 못 읽었을 때만 개입하므로 그 우선순위를 흔들지 않는다.

    근거를 함께 돌려주는 이유: '가장 많이'처럼 **다른 조건이 이미 소유한 어구**가 집계 표지로도
    읽히는 일이 있어서다. 그때 근거 표현이 같은지를 보고 중복 해석만 골라 막을 수 있어야 한다."""
    terms_by_function = registry.get("aggregateFunctions") or _AGGREGATE_FUNCTION_BACKSTOP
    matches: list[tuple[int, str, str]] = []
    for function, terms in terms_by_function.items():
        for term in terms or []:
            normalized = _compact(str(term))
            if normalized and normalized in compact:
                matches.append((len(normalized), str(function).upper(), normalized))
    if matches:
        _length, function, surface = max(matches)
        return function, surface
    for function in _AGGREGATE_FUNCTION_BACKSTOP:
        surface = lexicon_llm.signal_evidence(f"aggregate_{function.casefold()}", compact)
        if surface is not None:
            return function, surface
    return None, None


def _match_aggregate_function(compact: str, registry: dict[str, Any]) -> str | None:
    return _match_aggregate_function_evidence(compact, registry)[0]


def _match_dimensions_evidence(compact: str, registry: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """(선택된 그룹 축, 축별 근거 표면형)."""
    matches: list[tuple[int, int, str, dict[str, Any]]] = []
    surfaces: dict[str, str] = {}
    for dimension_id, spec in (registry.get("dimensions") or {}).items():
        if not isinstance(spec, dict):
            continue
        matched_terms = [str(term) for term in spec.get("terms", []) if _contains(compact, str(term))]
        if matched_terms:
            matches.append((max(len(_compact(term)) for term in matched_terms), int(spec.get("priority", 0)), str(dimension_id), spec))
            surfaces[str(dimension_id)] = max((_compact(term) for term in matched_terms), key=len)

    # Closely related dimensions (first/last/generic login channel, signup
    # channel/shop) declare an exclusive group.  Longest and then highest
    # priority semantic term wins, so substring overlap cannot produce two
    # contradictory GROUP BY columns.
    selected: list[str] = []
    claimed_groups: set[str] = set()
    for _length, _priority, dimension_id, spec in sorted(matches, reverse=True):
        group = str(spec.get("exclusiveGroup") or "")
        if group and group in claimed_groups:
            continue
        selected.append(dimension_id)
        if group:
            claimed_groups.add(group)
    return selected, surfaces


def _match_dimensions(compact: str, registry: dict[str, Any]) -> list[str]:
    return _match_dimensions_evidence(compact, registry)[0]


def _match_filters(compact: str, registry: dict[str, Any], query: str) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    for filter_id, spec in (registry.get("filters") or {}).items():
        if not isinstance(spec, dict):
            continue
        if any(_contains(compact, term) for term in spec.get("terms", [])) and _matches_required_terms(compact, spec):
            filters.append({"id": str(filter_id), "label": spec.get("label", filter_id)})
    recent = _RECENT_DAYS_RE.search(query)
    if recent:
        filters.append({"id": "recent_days", "label": f"최근 {int(recent.group(1))}일", "days": int(recent.group(1))})
    return filters


def _match_scopes(compact: str, registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect behavioural population scopes (purchase / login / campaign / cart).

    Negation is checked first: ``구매하지 않은`` contains the positive surface
    ``구매`` and would otherwise invert the requested population.
    """
    scopes: list[dict[str, Any]] = []
    for scope in _scopes(registry):
        negated = any(_contains(compact, term) for term in scope.get("negationTerms", []) or [])
        if not negated and not any(_contains(compact, term) for term in scope.get("terms", []) or []):
            continue
        scopes.append({"id": str(scope.get("id")), "label": scope.get("label", scope.get("id")), "negated": negated})
    return scopes


def _ranking_direction(query: str) -> str | None:
    """Return the requested member ranking direction, if this is an arg-extreme request."""
    if not _MEMBER_TARGET_RE.search(query):
        return None
    if _RANKING_LOW_RE.search(query):
        return "ASC"
    if _RANKING_HIGH_RE.search(query):
        return "DESC"
    return None


# ── 디멘션 행 랭킹('회원 수가 많은 시군구 상위 5개') ────────────────────────────────────
# 회원 행 랭킹(_ranking_direction)과 **출력 단위가 다르다**: 결과 1행이 회원이 아니라 그룹 축
# 하나다. 따라서 별도 실행 경로가 아니라 grouped_aggregate 위의 ORDER BY 집계 + 상위 N 이다.
#
# 어떤 축·지표가 존재하는지는 레지스트리(dimensions/metrics 의 terms·rankingTerms)가 안다 —
# 여기 있는 것은 순위 문법뿐이고, 새 축(등급별·채널별 …)은 레지스트리 한 줄로 열린다.
# 표면 낱말은 사전(docs/data/parser_lexicon.json)이 소유하고, 여기 코드에는 **구조**만 둔다.
_DIMENSION_RANK_HIGH_RE = lexicon_patterns.pattern("dimension_rank_high")
_DIMENSION_RANK_LOW_RE = lexicon_patterns.pattern("dimension_rank_low")
# 정렬 지시가 **직접 데리고 있는** 수. 이것이 행 수 제한의 1순위 근거다.
_SORT_DIRECTIVE_COUNT_RE = re.compile(
    "(?:" + lexicon_patterns.alternation(
        "sort_directive_high", "sort_directive_low", "sort_directive_alias"
    ) + r")(\d+)",
    re.IGNORECASE,
)
# 맨 카운터('시군구 5곳'). '3개월'·'2개국' 같은 기간·단위 명사는 행 수가 아니므로 배제한다 —
# 문장 아무 데나 있는 수를 행 수 제한으로 읽으면 **요청하지 않은 TOP N** 이 조용히 나간다.
_DIMENSION_RANK_COUNTER_RE = re.compile(
    r"(\d+)(?:(?:" + lexicon_patterns.alternation("dimension_row_counter_ambiguous")
    + ")(?!" + lexicon_patterns.alternation("dimension_row_counter_exclusion_tail") + ")|"
    + lexicon_patterns.alternation("dimension_row_counter") + ")"
)
# 축 뒤 어디든 회원 명사가 있으면 결과 행은 축이 아니라 회원이다('…지역의 회원을 추출').
# '회원 수'는 지표어이지 출력 단위가 아니므로 (?!수) 로 면제한다.
_MEMBER_ROW_TAIL_RE = re.compile(
    "(?:" + lexicon_patterns.alternation("member_noun", "member_noun_registrant") + ")(?!수)"
)
# 축 낱말 바로 뒤에 오면 그 낱말이 명사가 아니라 동사 어간인 경우('시도한/시도하는').
_AXIS_VERB_SUFFIX_RE = re.compile("(?:" + lexicon_patterns.alternation("verb_ending_hada") + ")")
# 그 자리에 정렬 지시가 오면 동사가 아니다('시군구 **하**위 3개').
_SORT_DIRECTIVE_HEAD_RE = re.compile(
    "(?:" + lexicon_patterns.alternation(
        "sort_directive_high", "sort_directive_low", "sort_directive_alias"
    ) + ")"
)
# 축 바로 앞의 거주 표지('많이 **사는** 시군구'). 이 축은 집계 대상이 아니라 거주지이고,
# 요청은 '그곳에 사는 회원'이다 — 밀집 지역 트랙(회원 행)이 소유한다.
_RESIDENCE_VERB_RE = re.compile("(?:" + lexicon_patterns.alternation("residence_verb") + ")")
# 축 앞에서 거주 표지를 찾는 창(compact).
_RESIDENCE_LOOKBEHIND = 6
# 순위 표지와 축 표면어 사이 허용 간격(compact). '많은 시군구'는 0, 조사 한둘까지만.
_DIMENSION_RANK_ADJACENCY = 2
# 정렬 지시('상위 3개')와 축 사이 허용 간격. 문장 반대편의 축을 끌어오지 못하게 한다.
_SORT_DIRECTIVE_REACH = 12
# 맨 카운터가 축에 붙어 행 수를 세는 것으로 인정하는 간격('시군구 10곳').
_COUNTER_REACH = 2


def _term_positions(
    specs: Any, compact: str, *, ranking_terms: bool
) -> list[tuple[str, int, int]]:
    """(항목 id, 시작, 끝) — 레지스트리 표면어가 이 문장에서 나타난 **모든** compact 좌표.

    ``rankingTerms`` 는 순위 문법 안에서만 축/지표로 읽는 맨 낱말이다(``시군구``·``회원``).
    그 자체로는 축도 지표도 아니므로(``결제를 시도한``), 인정 여부는 호출부가 문법으로 판정한다.

    첫 등장만 보면 앞 절의 같은 낱말이 진짜 축을 가린다('**지역별** 매출을 보고 회원 수가 많은
    **시군구** 상위 5개' → 축이 지역으로 잡힌다). 그래서 등장마다 좌표를 낸다.
    """
    items = specs.items() if isinstance(specs, dict) else (
        (str(spec.get("id")), spec) for spec in specs or [] if isinstance(spec, dict)
    )
    positions: list[tuple[str, int, int]] = []
    for item_id, spec in items:
        if not isinstance(spec, dict):
            continue
        terms = [*(spec.get("terms") or [])]
        if ranking_terms:
            terms.extend(spec.get("rankingTerms") or [])
        for term in terms:
            needle = _compact(str(term))
            if not needle:
                continue
            start = compact.find(needle)
            while start >= 0:
                end = start + len(needle)
                if not _is_verb_stem_use(compact, end):
                    positions.append((str(item_id), start, end))
                start = compact.find(needle, start + 1)
    return positions


def _is_verb_stem_use(compact: str, end: int) -> bool:
    """낱말 바로 뒤가 '하다' 활용이면 그 낱말은 명사가 아니라 동사다('시도한').

    다만 그 자리에 오는 것이 **정렬 지시**면 동사가 아니다 — '시군구 하위 3개'의 '하'는
    '하위'의 첫 글자다. 한 글자만 보고 자르면 '하위 N' 표현 전체가 축을 잃는다.
    """
    tail = compact[end:]
    if not _AXIS_VERB_SUFFIX_RE.match(tail[:1]):
        return False
    return _SORT_DIRECTIVE_HEAD_RE.match(tail) is None


def _ranking_cues(compact: str) -> list[tuple[int, int, str]]:
    """(시작, 끝, 방향) — 문장에 나타난 순위 표지를 **등장 순서대로**.

    방향을 '문장에 낮은 표지가 하나라도 있으면 ASC' 로 정하면, 무관한 절의 ``하위 등급``이
    정렬을 통째로 뒤집는다. 순위에 실제로 참여한 표지 하나만 방향을 정해야 한다.
    """
    cues = [
        (match.start(), match.end(), direction)
        for pattern, direction in ((_DIMENSION_RANK_LOW_RE, "ASC"), (_DIMENSION_RANK_HIGH_RE, "DESC"))
        for match in pattern.finditer(compact)
    ]
    return sorted(cues)


def _dimension_ranking(query: str, compact: str, registry: dict[str, Any]) -> dict[str, Any] | None:
    """그룹 축 행을 지표로 줄세우는 요청이면 {dimension, direction, limit} 를 돌려준다.

    두 가지 표면형만 인정한다 — 둘 다 '줄세우는 대상이 축'이라는 것이 문장에 드러난 경우다:
      (a) 순위 표지 바로 뒤에 축이 온다: ``회원 수가 **많은 시군구**``
      (b) 정렬 지시가 개수를 데리고 축 가까이 있다: ``시도별 회원 수 **상위 3개**``

    셋 중 하나라도 걸리면 물러난다 — 요청된 행이 축이 아니기 때문이다:
      ``N명``(그룹별 회원 Top-N 트랙), 축 뒤의 회원 명사(``…지역**의 회원**`` 밀집 거주자 트랙),
      순위 표지 앞에 지표어가 없음(``**실적은** 시군구별…`` — 무엇이 많은지가 앞에 없으면 순위가 아니다).
    """
    if _EXPLICIT_MEMBER_LIMIT_RE.search(query):
        return None
    axes = _term_positions(registry.get("dimensions"), compact, ranking_terms=True)
    if not axes:
        return None
    metrics = _term_positions(registry.get("metrics"), compact, ranking_terms=True)
    directive = _SORT_DIRECTIVE_COUNT_RE.search(compact)
    counter = _DIMENSION_RANK_COUNTER_RE.search(compact)
    for cue_start, cue_end, direction in _ranking_cues(compact):
        # 무엇을 기준으로 줄세우는가(지표)가 표지보다 앞에 있어야 순위 표현이다.
        if not any(metric_end <= cue_start for _id, _start, metric_end in metrics):
            continue
        carries_count = directive is not None and directive.start() == cue_start
        for dimension_id, start, end in axes:
            adjacent = 0 <= start - cue_end <= _DIMENSION_RANK_ADJACENCY
            near_directive = carries_count and abs(cue_start - end) <= _SORT_DIRECTIVE_REACH
            if not adjacent and not near_directive:
                continue
            if _MEMBER_ROW_TAIL_RE.search(compact[end:]):
                continue  # 축 뒤에 회원 명사 — 결과 행은 축이 아니라 회원이다
            if _RESIDENCE_VERB_RE.search(compact[max(0, start - _RESIDENCE_LOOKBEHIND): start]):
                continue  # '많이 사는 시군구' — 밀집 지역(거주 회원 추출) 트랙 소유
            return {
                "dimension": dimension_id,
                "direction": direction,
                "limit": _ranking_limit(directive, counter, end),
                "surface": compact[cue_start:cue_end],
            }
    return None


def _promote_ranked_axis(axis: str, dimensions: list[str], registry: dict[str, Any]) -> list[str]:
    """순위 절이 지목한 축을 결과 축으로 올린다.

    결과 행의 단위를 정하는 것은 순위 절이다 — '**지역별** 매출을 보고 회원 수가 많은 **시군구**
    상위 5개'에서 그룹 축은 시군구다. 같은 배타 그룹(residence_region)의 다른 축이 문장 앞쪽에서
    잡혀 있으면 그것을 대체하고, 다른 축(성별 등)은 그대로 둔다.
    """
    if axis in dimensions:
        return dimensions
    group = _dimension_spec(registry, axis).get("exclusiveGroup")
    kept = [
        item for item in dimensions
        if not group or _dimension_spec(registry, item).get("exclusiveGroup") != group
    ]
    return [axis, *kept]


def _match_ranking_metric(compact: str, registry: dict[str, Any]) -> dict[str, Any] | None:
    """순위 문법 안에서만 지표로 읽는 맨 낱말로 지표를 찾는다('회원이 가장 많은' → member_count).

    ``회원``·``고객``을 일반 지표어로 열면 회원 조건이 있는 모든 문장이 집계로 오독되므로,
    이 조회는 축 랭킹이 이미 성립한 뒤에만 부른다.
    """
    catalog = {
        str(metric.get("id")): metric
        for metric in registry.get("metrics", []) or []
        if isinstance(metric, dict) and metric.get("id")
    }
    positions = _term_positions(registry.get("metrics"), compact, ranking_terms=True)
    if not positions:
        return None
    best = max(
        positions,
        key=lambda item: (int(catalog.get(item[0], {}).get("priority", 0)), item[2] - item[1]),
    )
    return catalog.get(best[0])


def _ranking_limit(
    directive: "re.Match[str] | None", counter: "re.Match[str] | None", axis_end: int
) -> int | None:
    """행 수 제한. 정렬 지시가 데려온 수가 1순위, 축에 붙은 맨 카운터가 2순위, 없으면 None.

    방향을 정한 표지와 개수를 데려온 표지는 다를 수 있다('… **많은** 시군구 **상위 10곳**').
    그래서 제한은 방향과 따로, **축에서 가까운가**로만 판정한다. 문장 아무 데나 있는 수
    ('장바구니에 3개 이상')는 축에서 멀어 탈락한다 — 없는 제한을 지어내면 요청하지 않은 TOP N 이다.

    None 은 '제한 없음'(정렬만)이다.
    """
    if directive is not None and abs(directive.start() - axis_end) <= _SORT_DIRECTIVE_REACH:
        return int(directive.group(1))
    if counter is not None and 0 <= counter.start() - axis_end <= _COUNTER_REACH:
        return int(counter.group(1))
    return None


# ---------------------------------------------------------------------------
# 소스 선택 — 요청된 축·스코프·필터·기간을 모두 담을 수 있는 소스를 찾는다.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rejection:
    reason: str
    items: tuple[str, ...] = ()


@dataclass
class _Bindings:
    dimensions: dict[str, dict[str, Any]] = field(default_factory=dict)
    filters: dict[str, dict[str, Any]] = field(default_factory=dict)
    scopes: list[dict[str, Any]] = field(default_factory=list)
    window_target: str | None = None  # "scope" | "source" | None
    window_scope_id: str | None = None


def _evaluate_source(
    registry: dict[str, Any],
    source: dict[str, Any],
    *,
    dimensions: list[str],
    filters: list[tuple[str, dict[str, Any] | None]],
    scopes: list[dict[str, Any]],
    function: str | None,
    window_days: int | None,
) -> tuple[_Bindings | None, _Rejection | None]:
    allowed = {str(item).upper() for item in source.get("whenFunctions", []) or []}
    if allowed and str(function or "").upper() not in allowed:
        return None, _Rejection("unsupported_aggregate_function", (str(function or ""),))

    bindings = _Bindings()
    missing_dimensions = []
    for dimension_id in dimensions:
        mapping = _dimension_mapping(registry, source, dimension_id)
        if mapping is None:
            missing_dimensions.append(dimension_id)
        else:
            bindings.dimensions[dimension_id] = mapping
    if missing_dimensions:
        return None, _Rejection("unsupported_group_dimension", tuple(missing_dimensions))

    missing_scopes = []
    for scope_request in scopes:
        scope = _scope_spec(registry, str(scope_request.get("id")))
        binding = _scope_binding(scope, source, bool(scope_request.get("negated"))) if scope else None
        if binding is None:
            missing_scopes.append(str(scope_request.get("id")))
        else:
            bindings.scopes.append({**binding, "id": str(scope_request.get("id"))})
    if missing_scopes:
        return None, _Rejection("unsupported_metric_scope", tuple(missing_scopes))

    missing_filters = []
    for filter_id, inline in filters:
        mapping = _filter_mapping(registry, source, filter_id, inline)
        if mapping is None:
            missing_filters.append(filter_id)
        else:
            bindings.filters[filter_id] = mapping
    if missing_filters:
        return None, _Rejection("unsupported_metric_filter", tuple(missing_filters))

    if window_days:
        dated_scope = next(
            (
                binding
                for binding in bindings.scopes
                if binding["mode"] in {"exists", "column"} and binding["scope"].get("dateColumn")
            ),
            None,
        )
        if dated_scope is not None:
            bindings.window_target = "scope"
            bindings.window_scope_id = dated_scope["id"]
        elif isinstance(source.get("dateField"), dict):
            bindings.window_target = "source"
        else:
            return None, _Rejection("unsupported_relative_period", (f"최근 {window_days}일",))
    return bindings, None


def _source_rank(source: dict[str, Any], dimensions: set[str], index: int) -> tuple[int, int]:
    triggers = {str(item) for item in source.get("whenAnyDimensions", []) or []}
    if triggers & dimensions:
        return (0, index)
    # A source that exists to serve specific axes is a last resort when none of
    # those axes was requested — otherwise ``구매한 회원 수`` would silently move
    # off the member table onto the order table.
    return (2 if triggers else 1, index)


def _select_source(
    registry: dict[str, Any],
    metric: dict[str, Any],
    *,
    dimensions: list[str],
    filters: list[tuple[str, dict[str, Any] | None]],
    scopes: list[dict[str, Any]],
    function: str | None,
    window_days: int | None,
) -> tuple[dict[str, Any] | None, _Bindings | None, _Rejection | None]:
    sources = [item for item in metric.get("sources", []) or [] if isinstance(item, dict)]
    requested = set(dimensions)
    ordered = sorted(
        enumerate(sources), key=lambda pair: _source_rank(pair[1], requested, pair[0])
    )
    rejections: list[_Rejection] = []
    for _index, source in ordered:
        bindings, rejection = _evaluate_source(
            registry, source, dimensions=dimensions, filters=filters,
            scopes=scopes, function=function, window_days=window_days,
        )
        if bindings is not None:
            return source, bindings, None
        if rejection is not None:
            rejections.append(rejection)
    return None, None, _primary_rejection(rejections)


def _primary_rejection(rejections: list[_Rejection]) -> _Rejection:
    for reason in _REJECTION_PRIORITY:
        match = next((item for item in rejections if item.reason == reason), None)
        if match is not None:
            return match
    return rejections[0] if rejections else _Rejection("unsupported_group_dimension")


def _unsupported(reason: str, items: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    detail = ", ".join(item for item in items if item)
    messages = {
        "unsupported_aggregate_function": f"이 지표는 요청한 집계 함수({detail})를 지원하지 않습니다.",
        "unsupported_group_dimension": f"요청한 그룹 기준({detail})을 이 지표의 스키마에서 찾을 수 없습니다.",
        "unsupported_metric_scope": f"요청한 회원 행동 조건({detail})을 이 지표로 집계할 수 없습니다.",
        "unsupported_metric_filter": f"요청한 회원 조건({detail})을 이 지표에 적용할 수 없습니다.",
        "unsupported_relative_period": f"이 지표에는 기간 기준 컬럼이 없어 {detail} 조건을 적용할 수 없습니다.",
    }
    return {
        **extra,
        "unsupported_reason": reason,
        "unsupported_message": messages.get(reason, "요청한 집계를 안전하게 생성할 수 없습니다."),
        "unsupported_detail": {"kind": reason, "items": list(items)},
        "clarification": UNSUPPORTED_CLARIFICATIONS.get(reason),
    }


# ---------------------------------------------------------------------------
# 의도 분석
# ---------------------------------------------------------------------------


def _surface_evidence(compact: str, index_map: list[int], query: str, surface: str | None) -> dict[str, Any] | None:
    """정규형 표면형을 원문 구간으로 되돌린다. 원문에서 못 찾으면 None(= 근거 미상)."""
    needle = _compact(surface or "")
    if not needle:
        return None
    position = compact.find(needle)
    if position < 0:
        return None
    span = raw_span(index_map, position, position + len(needle))
    if span is None:
        return None
    return {"surface": query[span[0]: span[1]], "span": span}


def intent_evidence(query: str, intent: dict[str, Any], registry: dict[str, Any]) -> list[dict[str, Any]]:
    """이 해석을 성립시킨 표면 근거들(원문 좌표) — 역할별로 하나씩.

    담는 것은 **이 문장을 집계/랭킹 질문으로 만든 요소**(집계 함수어·지표어·그룹 축·출력 동사·
    극값 표지)뿐이다. 필터·스코프는 그 해석의 한정자일 뿐 성립 근거가 아니므로 넣지 않는다 —
    넣으면 "근거가 전부 남의 것"이라는 판정이 한정자 때문에 흐려진다.
    """
    compact, index_map = _compact_map(query)
    items: list[dict[str, Any]] = []

    def add(role: str, surface: str | None) -> None:
        found = _surface_evidence(compact, index_map, query, surface)
        if found is not None:
            items.append({"role": role, **found})

    if intent.get("aggregate_function"):
        add("aggregate_function", _match_aggregate_function_evidence(compact, registry)[1])
    if intent.get("metric"):
        add("metric", _match_metric_evidence(compact, registry)[1])
    dimension_surfaces = _match_dimensions_evidence(compact, registry)[1]
    for dimension in intent.get("dimensions") or []:
        add("dimension", dimension_surfaces.get(str(dimension)))
    action = _OUTPUT_ACTION_RE.search(query)
    if action is not None:
        items.append({"role": "output_action", "surface": action.group(0), "span": [action.start(), action.end()]})
    if intent.get("query_type") == "ranking":
        ranking = _RANKING_LOW_RE.search(query) or _RANKING_HIGH_RE.search(query)
        if ranking is not None:
            items.append({"role": "ranking", "surface": ranking.group(0), "span": [ranking.start(), ranking.end()]})
    elif isinstance(intent.get("ranking"), dict):
        # 축 랭킹의 순위 표지도 이 해석을 성립시킨 근거다 — 빠뜨리면 '근거가 전부 남의 것'으로
        # 판정돼(중복 해석 방지 게이트) 정상 축 랭킹이 조용히 승격되지 않는다.
        add("ranking", intent["ranking"].get("surface"))
    return items


def analyze_analytical_intent(
    query: str,
    registry_path: Path = DEFAULT_ANALYTICS_REGISTRY_PATH,
) -> dict[str, Any] | None:
    """Return a structured aggregate intent, or ``None`` for a non-aggregate query.

    The detector intentionally requires a registered metric.  An aggregate-like
    request with an unknown metric is returned as unsupported instead of being
    silently converted to a member list.

    The returned intent carries the surface evidence that produced it
    (:data:`EVIDENCE_KEY`) so a caller can tell an independent aggregate request
    from a re-reading of words another condition already owns.
    """
    intent = _analyze_analytical_intent(query, registry_path)
    if isinstance(intent, dict):
        intent[EVIDENCE_KEY] = intent_evidence(
            query, intent, load_analytics_registry(str(registry_path))
        )
    return intent


def _analyze_analytical_intent(
    query: str,
    registry_path: Path = DEFAULT_ANALYTICS_REGISTRY_PATH,
) -> dict[str, Any] | None:
    registry = load_analytics_registry(str(registry_path))
    if not registry or not isinstance(query, str) or not query.strip():
        return None
    compact = _compact(query)
    metric = _match_metric(compact, registry)
    function = _match_aggregate_function(compact, registry)
    dimensions = _match_dimensions(compact, registry)
    ranking_direction = _ranking_direction(query)
    dimension_ranking = _dimension_ranking(query, compact, registry)
    if dimension_ranking is not None:
        # 맨 낱말('시군구'·'회원')은 순위 문법 안에서만 축·지표다 — 그 판정이 섰으니 이제 축이고 지표다.
        # ('회원이 가장 많은 시군구' 처럼 '수' 없이 세는 표현이 여기서 지표를 얻는다)
        dimensions = _promote_ranked_axis(dimension_ranking["dimension"], dimensions, registry)
        if metric is None:
            metric = _match_ranking_metric(compact, registry)
    # 결과 행이 그룹 축인 요청은 '회원을 고르는 문장'이 아니다. 아래 회원 선택 방어들은 임계·비교
    # 표현이 있는 문장을 집계로 오인하지 않기 위한 것인데, 축 랭킹에서는 그 표현이 출력 단위를
    # 바꾸지 않고 모집단만 한정한다 — 그대로 두면 축 랭킹이 통째로 회원 목록으로 되돌아간다.
    member_selection_guard = dimension_ranking is None

    # Explicit top-N member requests belong to the existing configurable
    # audience-ranking path.  The analytical path below owns only the default
    # arg-extreme contract (one row).  This preserves "가장 많은 회원 100명"
    # while preventing an unrequested TOP 100 for a singular extreme.
    explicit_member_limit = _EXPLICIT_MEMBER_LIMIT_RE.search(query)
    if ranking_direction and explicit_member_limit and int(explicit_member_limit.group(1).replace(",", "")) > 1:
        return None

    # ``최근 30일 구매한 회원`` uses 최근 as a date-window adjective.  It
    # must not become MAX when no explicit metric is present.  Longer extreme
    # phrases such as ``가장 최근 구매일`` remain MAX through the registry.
    if _RECENT_WINDOW_RE.search(query) and function == "MAX":
        function = None
    if function == "MAX" and dimensions and "최근" in query and "가장 최근" not in query:
        function = None
    if function == "MAX" and metric is None and "최근" in query and "가장 최근" not in query:
        function = None

    # Member-valued thresholds are selection predicates.  Ranking is checked
    # first because ``가장 많은 회원`` is genuinely analytical.
    if member_selection_guard and ranking_direction is None and _MEMBER_TARGET_RE.search(query) and _MEMBER_VALUE_PREDICATE_RE.search(query):
        return None
    if member_selection_guard and ranking_direction is None and _MEMBER_TARGET_RE.search(query) and _TARGETING_COMPARISON_RE.search(query):
        return None
    if member_selection_guard and ranking_direction is None and _MEMBER_TARGET_RE.search(query) and _MEMBER_METRIC_COMPARISON_RE.search(query):
        return None
    if member_selection_guard and ranking_direction is None and _MEMBER_TARGET_RE.search(query) and _MEMBER_NUMERIC_PREDICATE_RE.search(query):
        return None

    aggregate_signal = bool(function or dimensions or _OUTPUT_ACTION_RE.search(query))
    if metric is None:
        if function:
            return {
                "query_type": "aggregate",
                "aggregate_function": function,
                "metric": None,
                "dimensions": dimensions,
                "filters": [],
                "scopes": [],
                "unsupported_reason": "unresolved_aggregate_metric",
                "unsupported_message": "요청한 집계 지표를 스키마의 수치 지표와 연결할 수 없습니다.",
                "clarification": UNSUPPORTED_CLARIFICATIONS["unresolved_aggregate_metric"],
            }
        return None
    # 회원 극값(TOP 1 회원) 계약. 같은 문장이 축 랭킹으로도 읽히면 축 랭킹이 이긴다 —
    # '가장 많은 시군구'의 결과 행은 회원이 아니라 시군구다.
    if ranking_direction and dimension_ranking is None:
        return _ranking_intent(metric, ranking_direction)
    # Amount thresholds and ranking phrases are audience conditions, not scalar/grouped analytics.
    if _TARGETING_COMPARISON_RE.search(query) and not function and not dimensions:
        return None
    if not aggregate_signal:
        return None
    if any(
        _contains(compact, term)
        for term in registry.get("unsupportedMetricQualifiers", _UNSUPPORTED_QUALIFIER_BACKSTOP)
    ) or lexicon_llm.signal_hit("unsupported_metric_qualifier", compact):
        return {
            "query_type": "aggregate",
            "aggregate_function": function or metric.get("defaultFunction"),
            "metric": metric.get("id"),
            "dimensions": dimensions,
            "filters": [],
            "scopes": [],
            "unsupported_reason": "unsupported_aggregate_metric_qualifier",
            "unsupported_message": "요청한 예측/미래 지표는 현재 스키마에서 안전하게 계산할 수 없습니다.",
            "clarification": UNSUPPORTED_CLARIFICATIONS["unsupported_aggregate_metric_qualifier"],
        }

    function = function or str(metric.get("defaultFunction") or "").upper() or None
    allowed = {str(value).upper() for value in metric.get("allowedFunctions", [])}
    filters = _match_filters(compact, registry, query)
    scopes = _match_scopes(compact, registry)
    window_days = next(
        (int(item["days"]) for item in filters if item.get("id") == "recent_days" and item.get("days")), None
    )
    filter_specs = _filter_specs(filters)
    base = {
        "query_type": "grouped_aggregate" if dimensions else "aggregate",
        "aggregate_function": function,
        "metric": metric.get("id"),
        "dimensions": dimensions,
        "filters": filters,
        "scopes": scopes,
        "window_days": window_days,
        "comparison": None,
        # 축 행의 정렬·상위 N. 없으면 None(그룹 집계 전체 행).
        "ranking": dimension_ranking,
        "result_shape": "grouped_rows" if dimensions else "scalar",
        "target_entity": None,
        "member_policy": resolve_member_scope(query),
    }
    if function not in allowed:
        return {**base, **_unsupported("unsupported_aggregate_function", (str(function or ""),))}

    source, _bindings, rejection = _select_source(
        registry, metric, dimensions=dimensions, filters=filter_specs,
        scopes=scopes, function=function, window_days=window_days,
    )
    if source is None:
        rejection = rejection or _Rejection("unsupported_group_dimension", tuple(dimensions))
        return {**base, **_unsupported(rejection.reason, rejection.items)}
    return {**base, "source_id": source.get("id")}


def _ranking_intent(metric: dict[str, Any], ranking_direction: str) -> dict[str, Any]:
    ranking_source = metric.get("rankingSource")
    common = {
        "query_type": "ranking",
        "metric": metric.get("id"),
        "dimensions": ["member"],
        "filters": [],
        "scopes": [],
        "comparison": {
            "operator": "argmin" if ranking_direction == "ASC" else "argmax",
            "direction": ranking_direction, "limit": 1,
        },
        "result_shape": "single_member",
        "target_entity": "member",
    }
    if not isinstance(ranking_source, dict):
        return {
            **common,
            "aggregate_function": None,
            "unsupported_reason": "unsupported_ranking_metric",
            "unsupported_message": "요청한 지표로 회원 순위를 안전하게 계산할 스키마 매핑이 없습니다.",
            "clarification": UNSUPPORTED_CLARIFICATIONS["unsupported_ranking_metric"],
        }
    return {
        **common,
        "aggregate_function": str(ranking_source.get("aggregationFunction") or "MAX").upper(),
        "source_id": ranking_source.get("id"),
        "ranking_direction": ranking_direction,
    }


# ---------------------------------------------------------------------------
# 계약 확정 — 의도 + 레지스트리를 물리 바인딩으로 굳힌다(IR/SQL 공용).
# ---------------------------------------------------------------------------


@dataclass
class AnalyticalContract:
    metric: dict[str, Any]
    source: dict[str, Any]
    bindings: _Bindings
    dimensions: list[str]
    policy_filter: dict[str, Any] | None
    window_days: int | None


def _source_for_intent(intent: dict[str, Any], registry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    metric = next(item for item in registry.get("metrics", []) if item.get("id") == intent.get("metric"))
    if intent.get("query_type") == "ranking":
        source = metric.get("rankingSource")
        if not isinstance(source, dict) or source.get("id") != intent.get("source_id"):
            raise KeyError("registered ranking source was not found")
        return metric, source
    source = next(item for item in metric.get("sources", []) if item.get("id") == intent.get("source_id"))
    return metric, source


def resolve_analytical_contract(
    intent: dict[str, Any],
    registry: dict[str, Any],
) -> AnalyticalContract:
    """Re-derive every physical binding of an accepted intent (single source of truth)."""
    metric, source = _source_for_intent(intent, registry)
    dimensions = [str(item) for item in intent.get("dimensions", []) or []]
    filter_specs = _filter_specs(intent.get("filters", []) or [])
    scopes = [item for item in intent.get("scopes", []) or [] if isinstance(item, dict)]
    window_days = intent.get("window_days")
    if window_days is None:
        window_days = next(
            (int(item["days"]) for item in intent.get("filters", []) or []
             if isinstance(item, dict) and item.get("id") == "recent_days" and item.get("days")),
            None,
        )
    bindings, rejection = _evaluate_source(
        registry, source, dimensions=dimensions, filters=filter_specs, scopes=scopes,
        function=intent.get("aggregate_function"), window_days=window_days,
    )
    if bindings is None:
        raise ValueError(f"{rejection.reason}: {', '.join(rejection.items) if rejection else ''}")
    policy = _policy_filter(intent, source)
    if policy is not None and any(
        str(mapping.get("column", "")).casefold() == str(policy["column"]).casefold()
        for mapping in bindings.filters.values()
    ):
        # 질문이 회원 상태를 명시했다면(휴면/탈퇴 회원 수) 그것이 모집단이다. 기본 정상회원 정책을
        # 함께 걸면 두 상태가 AND 로 만나 항상 0건이 된다.
        policy = None
    return AnalyticalContract(
        metric=metric, source=source, bindings=bindings, dimensions=dimensions,
        policy_filter=policy, window_days=window_days,
    )


def _policy_filter(intent: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the shared active-member policy against this source (own table or member join)."""
    if "active_member" not in (source.get("policyFilters") or []):
        return None
    policy = active_member_filter(str(intent.get("original_query") or ""))
    if policy is None:
        return None
    scope = intent.get("member_policy") if isinstance(intent.get("member_policy"), dict) else None
    if scope and scope.get("mode") == "all":
        return None
    mapping = _resolve_field_for_source(
        {"entity": policy["entity"], "field": policy["field"], "table": policy["table"], "column": policy["column"]},
        source,
    )
    if mapping is None:
        return None
    resolved = {**policy, **mapping, "id": policy["id"]}
    if scope and isinstance(scope.get("states"), list) and scope["states"]:
        states = scope["states"]
        resolved["operator"] = "eq" if len(states) == 1 else "in"
        resolved["value"] = states[0] if len(states) == 1 else states
        resolved["policyMode"] = scope.get("mode", "default")
    return resolved


def _field_ref(mapping: dict[str, Any]) -> dict[str, Any]:
    result = {
        "entity": mapping["entity"], "field": mapping["field"],
        "table": mapping["table"], "column": mapping["column"],
        "alias": mapping.get("alias"),
    }
    if mapping.get("semanticType"):
        result["semanticType"] = mapping["semanticType"]
    return result


def _relative_period(days: int) -> str:
    return f"P{int(days)}D"


def build_aggregation_request(
    intent: dict[str, Any],
    registry_path: Path = DEFAULT_ANALYTICS_REGISTRY_PATH,
) -> dict[str, Any]:
    registry = load_analytics_registry(str(registry_path))
    if intent.get("query_type") == "ranking":
        return _build_ranking_request(intent, registry)

    contract = resolve_analytical_contract(intent, registry)
    source, bindings = contract.source, contract.bindings
    dimensions = [_field_ref(bindings.dimensions[dimension_id]) for dimension_id in contract.dimensions]

    filters: list[dict[str, Any]] = []
    for raw in source.get("fixedFilters", []):
        filters.append({**_field_ref(raw), "id": raw["id"], "operator": raw["operator"], "value": raw.get("value")})
    for filter_id, mapping in bindings.filters.items():
        filters.append({
            **_field_ref(mapping), "id": filter_id,
            "operator": mapping.get("operator", "eq"), "value": mapping.get("value"),
        })
    if contract.window_days and bindings.window_target == "source":
        date_field = source["dateField"]
        filters.append({
            **_field_ref(date_field), "id": "recent_days", "operator": "gte",
            "value": _relative_period(contract.window_days),
        })

    applied_policy_filters: list[dict[str, Any]] = []
    if contract.policy_filter is not None:
        policy = contract.policy_filter
        filters.append({key: value for key, value in policy.items() if key not in {"expression", "dependencies"}})
        applied_policy_filters.append({
            "id": policy["id"], "column": policy["column"],
            "operator": policy["operator"], "value": policy["value"],
            "mode": policy.get("policyMode", "default"),
        })

    # 스코프는 단일 컬럼 비교가 아니라 회원 존재/부재 관계다. 관계 조건으로 실어야 SQL 검증기가
    # EXISTS/NOT EXISTS 증거를 독립적으로 확인할 수 있다(조용한 스코프 누락 차단).
    relations: list[dict[str, Any]] = []
    for binding in bindings.scopes:
        scope = binding["scope"]
        template = scope.get("absencePredicate") if binding["negated"] else scope.get("existsPredicate")
        relations.append({
            "id": f"scope_{binding['id']}",
            "sourceEntity": str(source.get("entity") or source.get("table") or "member"),
            "relation": str(binding["id"]),
            "targetEntity": str(scope.get("entity") or scope.get("table") or binding["id"]),
            "mode": "none" if binding["negated"] else "any",
            # 내재(inherent) 스코프의 증거는 스코프 테이블이 아니라 소스 테이블 자체다
            # (주문 상세에서 회원을 세는 것이 곧 구매자 모집단이다).
            "targetTable": source.get("table") if binding["mode"] == "inherent" else scope.get("table"),
            "binding": binding["mode"],
            "predicateColumns": (
                _template_columns([str(template or ""), f"{{host}}.{scope.get('dateColumn', '')}"])
                if binding["mode"] == "column" else []
            ),
        })

    measure = source["measure"]
    metric_id = str(intent["metric"])
    target_entity = metric_id if not dimensions else metric_id + "_by_" + "_".join(contract.dimensions)
    # 회원 grain 소스는 안쪽 SELECT(회원별 집계)가 물리 요구사항이다. 바깥 함수(평균 등)는
    # 의도 계약 검증이 확인한다 — 파생 별칭을 물리 컬럼처럼 요구하면 정상 SQL 이 탈락한다.
    member_grain = source.get("grain") == "member"
    inner = source.get("inner") if isinstance(source.get("inner"), dict) else {}
    aggregation = {
        "id": metric_id,
        "function": str(inner["function"] if member_grain else intent["aggregate_function"]).casefold(),
        "entity": (inner if member_grain else measure)["entity"],
        "field": (inner if member_grain else measure)["field"],
        "table": (inner if member_grain else measure)["table"],
        "column": (inner if member_grain else measure)["column"],
        "alias": (inner if member_grain else measure).get("alias", "TOTAL_PURCHASE_AMOUNT"),
        "distinct": bool((inner if member_grain else measure).get("distinct", False)),
    }
    if member_grain:
        member_ref = _field_ref(source["memberField"])
        groupings = [member_ref, *dimensions]
    else:
        groupings = list(dimensions)
    # 축 행 랭킹은 IR 이 이미 가진 sorting/ranking 계약으로 표현한다(정렬 기준은 컬럼이 아니라 지표 id).
    axis_ranking = intent.get("ranking") if isinstance(intent.get("ranking"), dict) else None
    sorting = (
        [{"metricId": metric_id, "direction": str(axis_ranking["direction"]).casefold()}]
        if axis_ranking else []
    )
    # ranking 계약은 '상위 N' 요청 자체다 — 제한 개수가 없으면 켜지 않는다(계약 검증이 limit>=1 을
    # 요구하므로, 켜고 비워 두면 '회원 수가 많은 시군구'(제한 없는 정렬)가 plan 통째로 무효가 된다).
    axis_limit = axis_ranking.get("limit") if axis_ranking else None
    ranking_contract: dict[str, Any] = {"enabled": False, "partitionBy": []}
    if axis_ranking and isinstance(axis_limit, int) and axis_limit > 0:
        ranking_contract = {
            "enabled": True,
            "type": "bottom" if str(axis_ranking["direction"]).upper() == "ASC" else "top",
            "limit": int(axis_limit),
            "partitionBy": [], "orderByMetricId": metric_id, "tiePolicy": "first",
        }
    return {
        "targetEntity": target_entity,
        "outputColumns": dimensions,
        "filters": filters,
        "groupings": groupings,
        "aggregations": [aggregation],
        "derivedMetrics": [], "sorting": sorting,
        "ranking": ranking_contract,
        "postAggregationFilters": [], "relationConditions": relations,
        "dateGrain": None, "comparison": None,
        "businessRules": {
            "sourceId": source["id"],
            "contractSource": "analytics_registry",
            "appliedPolicyFilters": applied_policy_filters,
            "memberScope": (intent.get("member_policy") or {}).get("mode"),
            "memberGrainAggregation": (
                {"function": str(intent["aggregate_function"]).upper(), "alias": measure.get("alias")}
                if member_grain else None
            ),
            "scopes": [{"id": item["id"], "mode": item["mode"], "negated": item["negated"]} for item in bindings.scopes],
            "intentContract": {
                key: intent.get(key) for key in (
                    "query_type", "aggregate_function", "metric", "dimensions", "filters",
                    "scopes", "comparison", "ranking", "result_shape", "target_entity",
                )
            },
        },
        "assumptions": _assumptions(source, bindings),
        "unresolvedFields": [],
    }


def _assumptions(source: dict[str, Any], bindings: _Bindings) -> list[str]:
    assumptions = [str(item) for item in source.get("assumptions", []) or []]
    for binding in bindings.scopes:
        note = binding["scope"].get("assumption")
        if isinstance(note, str) and note and note not in assumptions:
            assumptions.append(note)
    return assumptions


def _build_ranking_request(intent: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    _metric, source = _source_for_intent(intent, registry)
    member = source["member"]
    measure = source["measure"]
    metric_id = str(intent["metric"])
    member_ref = _field_ref(member)
    filters = [
        {**_field_ref(raw), "id": raw["id"], "operator": raw["operator"], "value": raw.get("value")}
        for raw in source.get("fixedFilters", [])
    ]
    ranking_type = "bottom" if intent.get("ranking_direction") == "ASC" else "top"
    return {
        "targetEntity": "member",
        "outputColumns": [member_ref],
        "filters": filters,
        "groupings": [member_ref],
        "aggregations": [{
            "id": metric_id,
            "function": str(intent["aggregate_function"]).casefold(),
            "entity": measure["entity"], "field": measure["field"],
            "table": measure["table"], "column": measure["column"],
            "alias": measure.get("alias", "METRIC_VALUE"),
            "distinct": bool(measure.get("distinct", False)),
        }],
        "derivedMetrics": [],
        "sorting": [{"metricId": metric_id, "direction": intent["ranking_direction"].casefold()}],
        "ranking": {
            "enabled": True, "type": ranking_type, "limit": 1,
            "partitionBy": [], "orderByMetricId": metric_id, "tiePolicy": "first",
        },
        "postAggregationFilters": [], "relationConditions": [],
        "dateGrain": None,
        "comparison": intent.get("comparison"),
        "businessRules": {
            "sourceId": source["id"],
            "contractSource": "analytics_registry",
            "intentContract": {
                key: intent.get(key) for key in (
                    "query_type", "aggregate_function", "metric", "dimensions", "filters",
                    "comparison", "result_shape", "target_entity",
                )
            },
        },
        "assumptions": source.get("assumptions", []),
        "unresolvedFields": [],
    }


# ---------------------------------------------------------------------------
# SQL 컴파일
# ---------------------------------------------------------------------------


def _sql_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return sql_dialect.quote_literal(value)


def _relative_date_expression(days: int) -> str:
    return f"CONVERT(CHAR(8), DATEADD(DAY, -{int(days)}, GETDATE()), 112)"


def _filter_sql(item: dict[str, Any], expression: str) -> str:
    operator = item.get("operator")
    value = item.get("value")
    if operator in {"gt", "gte", "lt", "lte"} and isinstance(value, str) and re.fullmatch(r"P\d+D", value, re.IGNORECASE):
        symbol = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[str(operator)]
        predicate = f"{expression} {symbol} {_relative_date_expression(int(value[1:-1]))}"
        # 미접속 조건은 '한 번도 로그인하지 않은 회원'을 포함한다(설정의 include_null).
        return f"({predicate} OR {expression} IS NULL)" if item.get("includeNull") else predicate
    if operator in {"in", "not_in"} and isinstance(value, (list, tuple)):
        keyword = "IN" if operator == "in" else "NOT IN"
        return f"{expression} {keyword} (" + ", ".join(_sql_literal(item) for item in value) + ")"
    if operator in {"is_null", "is_not_null"}:
        return f"{expression} {'IS NULL' if operator == 'is_null' else 'IS NOT NULL'}"
    sql_operator = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}.get(str(operator))
    if sql_operator is None:
        raise ValueError(f"unsupported analytical filter operator: {operator}")
    return f"{expression} {sql_operator} {_sql_literal(value)}"


def _render_scope_template(template: str, *, host_alias: str, member_expression: str, scope_alias: str) -> str:
    return (
        str(template)
        .replace("{alias}", scope_alias)
        .replace("{host}", host_alias)
        .replace("{member}", member_expression)
    )


def _scope_sql(binding: dict[str, Any], source: dict[str, Any], window_days: int | None) -> str | None:
    """Compile one population scope against the selected source."""
    scope = binding["scope"]
    host_alias = str(source.get("alias") or "")
    member = source.get("memberField") if isinstance(source.get("memberField"), dict) else {}
    member_expression = str(member.get("expression") or f"{host_alias}.MEMBER_NO")
    scope_alias = str(scope.get("alias") or "S")
    conditions = [
        _render_scope_template(condition, host_alias=host_alias, member_expression=member_expression, scope_alias=scope_alias)
        for condition in scope.get("conditions", []) or []
    ]

    if binding["mode"] == "inherent":
        # 소스 자체가 스코프 모집단이다. 그래도 스코프가 요구하는 술어(예: 대상군/반응 여부)는
        # 반드시 남아야 하므로 같은 정의를 그대로 WHERE 에 싣는다.
        return " AND ".join(conditions) if conditions else None

    if binding["mode"] == "column":
        template = scope.get("absencePredicate") if binding["negated"] else scope.get("existsPredicate")
        predicate = _render_scope_template(
            str(template or ""), host_alias=host_alias, member_expression=member_expression, scope_alias=scope_alias,
        )
        if window_days and scope.get("dateColumn") and not binding["negated"]:
            predicate = f"{host_alias}.{scope['dateColumn']} >= {_relative_date_expression(window_days)}"
        return predicate or None

    where = [
        _render_scope_template(
            str(scope.get("join") or ""), host_alias=host_alias,
            member_expression=member_expression, scope_alias=scope_alias,
        ),
        *conditions,
    ]
    if window_days and scope.get("dateColumn"):
        where.append(f"{scope_alias}.{scope['dateColumn']} >= {_relative_date_expression(window_days)}")
    keyword = "NOT EXISTS" if binding["negated"] else "EXISTS"
    return f"{keyword} (SELECT 1 FROM {scope['table']} {scope_alias} WHERE " + " AND ".join(part for part in where if part) + ")"


def _aggregate_expression(function: str, measure: dict[str, Any], expression: str | None = None) -> str:
    distinct = "DISTINCT " if measure.get("distinct") else ""
    return f"{function.upper()}({distinct}{expression or measure['expression']})"


def compile_aggregation_ast(
    intent: dict[str, Any],
    request: dict[str, Any],
    registry_path: Path = DEFAULT_ANALYTICS_REGISTRY_PATH,
) -> SelectAst:
    """Compile a registered aggregation contract to the project SelectAst."""
    registry = load_analytics_registry(str(registry_path))
    if intent.get("query_type") == "ranking":
        return _compile_ranking_ast(intent, request, registry)

    contract = resolve_analytical_contract(intent, registry)
    source, bindings = contract.source, contract.bindings
    function = str(intent["aggregate_function"]).upper()
    if source.get("grain") == "member":
        return _compile_per_member_ast(contract, function)

    dependencies: set[str] = set()
    columns: list[str] = []
    groups: list[str] = []
    for dimension_id in contract.dimensions:
        mapping = bindings.dimensions[dimension_id]
        columns.append(f"{mapping['expression']} AS {mapping.get('outputAlias', mapping['column'])}")
        groups.append(mapping["expression"])
        dependencies.update(mapping.get("dependencies", []))

    measure = source["measure"]
    columns.append(f"{_aggregate_expression(function, measure)} AS {measure.get('alias', 'AGGREGATE_VALUE')}")

    where = _compile_where(contract, dependencies)
    # 축 행 랭킹: 집계식으로 정렬하고 상위 N 행만 남긴다. 값이 없는 그룹(NULL/공백)은 '어느
    # 시군구인가'에 대한 답이 아니므로 순위 모집단에서 제외한다 — 빈 축 값이 1위를 차지하는
    # 그럴듯한 오답을 막는다. 공백이 결측인지는 축 선언(blankIsMissing)이 안다.
    ranking = intent.get("ranking") if isinstance(intent.get("ranking"), dict) else None
    order_by: list[str] = []
    limit: int | None = None
    if ranking:
        # 값이 없는 그룹은 '어느 시군구인가'에 대한 답이 아니므로 순위 모집단에서 뺀다. 다만
        # **줄세우는 축에만** 적용한다 — 다른 축까지 걸면 요청하지 않은 모집단 축소가 된다.
        ranked_axis = str(ranking.get("dimension") or (contract.dimensions[0] if contract.dimensions else ""))
        mapping = bindings.dimensions.get(ranked_axis)
        if mapping is not None:
            where.append(f"{mapping['expression']} IS NOT NULL")
            if _dimension_spec(registry, ranked_axis).get("blankIsMissing"):
                where.append(f"{mapping['expression']} <> ''")
        order_by = [f"{_aggregate_expression(function, measure)} {str(ranking['direction']).upper()}"]
        limit = ranking["limit"] if isinstance(ranking.get("limit"), int) and ranking["limit"] > 0 else None

    joins = [
        f"     INNER JOIN {join['table']} {join['alias']} ON {join['on']}"
        for join in _source_joins(source) if join.get("id") in dependencies
    ]
    return SelectAst(
        columns=columns,
        from_lines=[f"FROM {source['table']} {source['alias']}", *joins],
        where=where,
        group_by=groups,
        order_by=order_by,
        limit=limit,
    )


def _compile_where(contract: AnalyticalContract, dependencies: set[str]) -> list[str]:
    source, bindings = contract.source, contract.bindings
    where: list[str] = []
    for raw in source.get("fixedFilters", []) or []:
        where.append(_filter_sql(raw, raw["expression"]))
    for _filter_id, mapping in bindings.filters.items():
        dependencies.update(mapping.get("dependencies", []))
        where.append(_filter_sql(mapping, mapping["expression"]))
    if contract.window_days and bindings.window_target == "source":
        date_field = source["dateField"]
        dependencies.update(date_field.get("dependencies", []))
        where.append(
            _filter_sql({"operator": "gte", "value": _relative_period(contract.window_days)}, date_field["expression"])
        )
    if contract.policy_filter is not None:
        dependencies.update(contract.policy_filter.get("dependencies", []))
        where.append(_filter_sql(contract.policy_filter, contract.policy_filter["expression"]))
    for binding in bindings.scopes:
        window = contract.window_days if bindings.window_scope_id == binding["id"] else None
        predicate = _scope_sql(binding, source, window)
        if predicate:
            where.append(predicate)
    return where


def _compile_per_member_ast(contract: AnalyticalContract, function: str) -> SelectAst:
    """Compile a per-member two-level aggregate (``회원당 평균 구매횟수``).

    The inner query reduces rows to one row per member, the outer query applies
    the requested function to that member-level value.  A flat aggregate would
    silently answer a different question (per-order instead of per-member).
    """
    source, bindings = contract.source, contract.bindings
    inner = source["inner"]
    derived_alias = str(source.get("derivedAlias") or "M")
    dependencies: set[str] = set()
    member = source["memberField"]

    inner_columns = [f"{member['expression']} AS {member.get('outputAlias', member['column'])}"]
    inner_groups = [member["expression"]]
    outer_columns: list[str] = []
    outer_groups: list[str] = []
    for dimension_id in contract.dimensions:
        mapping = bindings.dimensions[dimension_id]
        output_alias = mapping.get("outputAlias", mapping["column"])
        inner_columns.append(f"{mapping['expression']} AS {output_alias}")
        inner_groups.append(mapping["expression"])
        outer_columns.append(f"{derived_alias}.{output_alias} AS {output_alias}")
        outer_groups.append(f"{derived_alias}.{output_alias}")
        dependencies.update(mapping.get("dependencies", []))

    inner_distinct = "DISTINCT " if inner.get("distinct") else ""
    inner_alias = str(inner.get("alias") or "METRIC_VALUE")
    inner_columns.append(f"{str(inner['function']).upper()}({inner_distinct}{inner['expression']}) AS {inner_alias}")

    where = _compile_where(contract, dependencies)
    joins = [
        f"     INNER JOIN {join['table']} {join['alias']} ON {join['on']}"
        for join in _source_joins(source) if join.get("id") in dependencies
    ]
    inner_ast = SelectAst(
        columns=inner_columns,
        from_lines=[f"FROM {source['table']} {source['alias']}", *joins],
        where=where,
        group_by=inner_groups,
    )
    from sql_ast import render_select_ast  # local import keeps the module import graph flat

    inner_sql = render_select_ast(inner_ast).replace("\n", "\n        ")
    measure = source["measure"]
    outer_columns.append(f"{function}({measure['expression']}) AS {measure.get('alias', 'AGGREGATE_VALUE')}")
    return SelectAst(
        columns=outer_columns,
        from_lines=[f"FROM ({inner_sql}) {derived_alias}"],
        group_by=outer_groups,
    )


def _compile_ranking_ast(intent: dict[str, Any], request: dict[str, Any], registry: dict[str, Any]) -> SelectAst:
    _metric, source = _source_for_intent(intent, registry)
    member = source["member"]
    measure = source["measure"]
    member_expression = member["expression"]
    function = str(intent["aggregate_function"]).upper()
    metric_expression = _aggregate_expression(function, measure)
    where = [
        _filter_sql(item, next(
            raw["expression"] for raw in source.get("fixedFilters", []) if raw.get("id") == item.get("id")
        ))
        for item in request.get("filters", [])
    ]
    joins = [f"     INNER JOIN {join['table']} {join['alias']} ON {join['on']}" for join in source.get("joins", [])]
    return SelectAst(
        columns=[
            f"TOP 1 {member_expression} AS {member.get('outputAlias', 'CUST_ID')}",
            f"{metric_expression} AS {measure.get('alias', 'METRIC_VALUE')}",
        ],
        from_lines=[f"FROM {source['table']} {source['alias']}", *joins],
        where=where,
        group_by=[member_expression],
        order_by=[f"{metric_expression} {intent['ranking_direction']}", f"{member_expression} ASC"],
    )


# ---------------------------------------------------------------------------
# 생성과 독립적인 SQL↔의도 계약 검증
# ---------------------------------------------------------------------------


def _outer_select(root: exp.Expression) -> exp.Select | None:
    """The statement's own SELECT — never a derived-table or EXISTS subquery.

    Result shape must be read from the query the caller executes.  Walking into
    subqueries made a scalar ``SELECT AVG(...) FROM (SELECT ... GROUP BY ...)``
    look like a grouped report and failed a correct contract.
    """
    if isinstance(root, exp.Select):
        return root
    node = root.this if isinstance(root, exp.Subquery) else None
    if isinstance(node, exp.Select):
        return node
    return root.find(exp.Select)


def _outer_nodes(select: exp.Select | None, kind: type[exp.Expression]) -> list[exp.Expression]:
    if select is None:
        return []
    return [node for node in select.find_all(kind) if node.find_ancestor(exp.Select) is select]


def validate_intent_sql_contract(
    intent: dict[str, Any],
    sql: str,
    registry_path: Path = DEFAULT_ANALYTICS_REGISTRY_PATH,
    dialect: str = "tsql",
) -> dict[str, Any]:
    """Check output shape and core metric/scope/dimension semantics independently of generation."""
    issues: list[dict[str, str]] = []
    try:
        statements = [node for node in sqlglot.parse(sql, read=dialect) if node is not None]
    except Exception as exc:
        return {
            "ran": True, "valid": False, "expected_shape": intent.get("result_shape"),
            "actual_shape": "invalid_sql",
            "issues": [{"code": "sql_parse_error", "message": str(exc)}],
        }
    if len(statements) != 1:
        return {
            "ran": True, "valid": False, "expected_shape": intent.get("result_shape"),
            "actual_shape": "invalid_sql",
            "issues": [{"code": "multiple_statements", "message": "Exactly one SQL statement is required."}],
        }
    root = statements[0]
    select = _outer_select(root)
    aggregates = list(root.find_all(exp.AggFunc))
    outer_aggregates = _outer_nodes(select, exp.AggFunc)
    groups = _outer_nodes(select, exp.Group)
    orders = list(root.find_all(exp.Ordered))
    limits = [node for node in root.find_all(exp.Limit) if isinstance(node.expression, exp.Literal)]
    limit_one = any(str(node.expression.name) == "1" for node in limits)
    if limit_one and orders:
        actual_shape = "single_member"
    elif outer_aggregates and groups:
        actual_shape = "grouped_rows"
    elif outer_aggregates and not groups and select is not None and len(select.expressions) == 1:
        actual_shape = "scalar"
    else:
        actual_shape = "member_rows"

    expected_shape = intent.get("result_shape")
    if actual_shape != expected_shape:
        issues.append({
            "code": "result_shape_mismatch",
            "message": f"Expected {expected_shape}, but SQL produces {actual_shape}.",
        })
    expected_function = str(intent.get("aggregate_function") or "").casefold()
    if expected_function and not any(node.key.casefold() == expected_function for node in aggregates):
        issues.append({
            "code": "aggregate_function_mismatch",
            "message": f"Expected aggregate function {expected_function.upper()} is absent.",
        })

    registry = load_analytics_registry(str(registry_path))
    try:
        _metric, source = _source_for_intent(intent, registry)
        aggregate_columns = {
            column.name.casefold()
            for aggregate in aggregates
            for column in aggregate.find_all(exp.Column)
        }
        _check_measure(intent, source, aggregate_columns, sql, issues)
        grouped_columns = {column.name.casefold() for group in groups for column in group.find_all(exp.Column)}
        selected_columns = {
            column.name.casefold()
            for projection in (select.expressions if select is not None else [])
            for column in projection.find_all(exp.Column)
        }
        if intent.get("query_type") == "ranking":
            _check_ranking(intent, source, selected_columns, orders, limit_one, issues)
        else:
            _check_dimensions(intent, registry, source, grouped_columns, selected_columns, issues)
            _check_scopes(resolve_analytical_contract(intent, registry), root, issues)
    except (KeyError, StopIteration, TypeError, ValueError):
        issues.append({"code": "intent_registry_mapping_missing", "message": "Intent has no registered physical mapping."})

    return {
        "ran": True,
        "valid": not issues,
        "expected_shape": expected_shape,
        "actual_shape": actual_shape,
        "issues": issues,
    }


def _check_measure(
    intent: dict[str, Any], source: dict[str, Any], aggregate_columns: set[str], sql: str, issues: list[dict[str, str]]
) -> None:
    expected_measure = str(source["measure"]["column"]).casefold()
    if expected_measure in aggregate_columns:
        return
    # A per-member source aggregates the derived member-level alias, so the
    # physical column appears in the inner query instead.
    inner = source.get("inner") if isinstance(source.get("inner"), dict) else None
    if inner and str(inner.get("column", "")).casefold() in aggregate_columns:
        return
    issues.append({
        "code": "metric_column_mismatch",
        "message": f"Registered metric column {expected_measure.upper()} is not aggregated.",
    })


def _check_dimensions(
    intent: dict[str, Any], registry: dict[str, Any], source: dict[str, Any],
    grouped_columns: set[str], selected_columns: set[str], issues: list[dict[str, str]],
) -> None:
    for dimension_id in intent.get("dimensions", []) or []:
        mapping = _dimension_mapping(registry, source, str(dimension_id))
        if mapping is None:
            issues.append({
                "code": "MISSING_GROUP_BY_DIMENSION",
                "message": f"Dimension {dimension_id} has no physical mapping for source {source.get('id')}.",
            })
            continue
        expected_column = str(mapping["column"]).casefold()
        output_alias = str(mapping.get("outputAlias") or mapping["column"]).casefold()
        if expected_column not in grouped_columns and output_alias not in grouped_columns:
            actual = sorted(grouped_columns)
            code = "MISSING_GROUP_BY_DIMENSION" if not actual else "SEMANTIC_COLUMN_MISMATCH"
            issues.append({
                "code": code,
                "message": f"Dimension {dimension_id} requires {expected_column.upper()} in GROUP BY; actual={actual}.",
            })
        elif expected_column not in selected_columns and output_alias not in selected_columns:
            issues.append({
                "code": "MISSING_SELECTED_DIMENSION",
                "message": f"Dimension {dimension_id} requires {expected_column.upper()} in SELECT.",
            })


def _check_scopes(contract: AnalyticalContract, root: exp.Expression, issues: list[dict[str, str]]) -> None:
    """Verify every requested population scope left physical evidence in the SQL.

    This is the guard against the most dangerous analytical failure: a plausible
    number computed over the wrong population because a behavioural condition was
    dropped between parsing and generation.  Evidence depends on how the scope was
    bound — an inherently scoped source proves it by its own table and predicates,
    a predicate binding proves it by EXISTS/NOT EXISTS or the host column.
    """
    sql_text = root.sql().casefold()
    for binding in contract.bindings.scopes:
        scope = binding["scope"]
        scope_id = binding["id"]
        if binding["mode"] == "inherent":
            table = str(contract.source.get("table") or "").casefold()
            missing = [
                column for column in _template_columns(scope.get("conditions", []) or [])
                if column not in sql_text
            ]
            if (table and table not in sql_text) or missing:
                issues.append({
                    "code": "MISSING_SCOPE_PREDICATE",
                    "message": f"Population scope {scope_id} lost its defining predicate ({', '.join(missing) or table}).",
                })
            continue
        if binding["mode"] == "column":
            template = scope.get("absencePredicate") if binding["negated"] else scope.get("existsPredicate")
            columns = _template_columns([str(template or ""), f"{{host}}.{scope.get('dateColumn', '')}"])
            if not any(column in sql_text for column in columns):
                issues.append({
                    "code": "MISSING_SCOPE_PREDICATE",
                    "message": f"Population scope {scope_id} is not applied in the SQL.",
                })
            continue
        table = str(scope.get("table") or "").casefold()
        if table and table not in sql_text:
            issues.append({
                "code": "MISSING_SCOPE_PREDICATE",
                "message": f"Population scope {scope_id} requires {table.upper()} evidence in the SQL.",
            })
            continue
        if binding["negated"] and "not exists" not in sql_text and "not in" not in sql_text:
            issues.append({
                "code": "SCOPE_POLARITY_MISMATCH",
                "message": f"Population scope {scope_id} is negated but the SQL asserts presence.",
            })


def _template_columns(templates: list[str]) -> list[str]:
    """Column names referenced by scope predicate templates, for evidence checks."""
    columns: list[str] = []
    for template in templates:
        for column in re.findall(r"[{}\w]+\.([A-Za-z_][A-Za-z0-9_]*)", str(template)):
            folded = column.casefold()
            if folded and folded not in columns:
                columns.append(folded)
    return columns


def _check_ranking(
    intent: dict[str, Any], source: dict[str, Any], selected_columns: set[str],
    orders: list[exp.Expression], limit_one: bool, issues: list[dict[str, str]],
) -> None:
    member_column = str(source["member"]["column"]).casefold()
    if member_column not in selected_columns:
        issues.append({"code": "ranking_member_missing", "message": "Ranking SQL does not return a member id."})
    expected_desc = intent.get("ranking_direction") == "DESC"
    if not orders or not any(bool(order.args.get("desc")) == expected_desc for order in orders):
        issues.append({"code": "ranking_direction_mismatch", "message": "Ranking direction differs from intent."})
    if not limit_one:
        issues.append({"code": "ranking_limit_mismatch", "message": "Single-member ranking requires TOP/LIMIT 1."})
