"""Natural-language analytical intent parsing and deterministic SQL compilation.

The targeting pipeline historically treated words such as ``VIP`` or ``여성`` as
proof that a query wanted a member list, even when the requested output was an
aggregate.  This module keeps output shape, metric, dimensions, and filters in a
separate registry-backed contract so audience filters cannot replace a measure.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

from sql_ast import SelectAst


DEFAULT_ANALYTICS_REGISTRY_PATH = Path("docs/data/analytics_registry.json")

_OUTPUT_ACTION_RE = re.compile(r"알려|보여|조회|계산|구해|집계")
_TARGETING_COMPARISON_RE = re.compile(
    r"(?:\d[\d,]*(?:\.\d+)?\s*(?:원|건|회|개|명)?\s*(?:이상|이하|초과|미만|같|넘)|"
    r"상위|하위|높은|낮은|많은|적은)"
)
_RECENT_DAYS_RE = re.compile(r"최근\s*(\d+)\s*일(?:간|동안|이내)?")


def _compact(value: str) -> str:
    return re.sub(r"[\s.,!?·_\-/]+", "", value).casefold()


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


def _matches_required_terms(compact: str, spec: dict[str, Any]) -> bool:
    required = spec.get("requiredTerms") or []
    return all(any(_contains(compact, term) for term in group) for group in required if isinstance(group, list))


def _match_metric(compact: str, registry: dict[str, Any]) -> dict[str, Any] | None:
    metrics = [item for item in registry.get("metrics", []) if isinstance(item, dict)]
    metrics.sort(key=lambda item: int(item.get("priority", 0)), reverse=True)
    for metric in metrics:
        terms = metric.get("terms") or []
        if terms and any(_contains(compact, term) for term in terms) and _matches_required_terms(compact, metric):
            return metric
    return None


def _match_aggregate_function(compact: str, registry: dict[str, Any]) -> str | None:
    matches: list[tuple[int, str]] = []
    for function, terms in (registry.get("aggregateFunctions") or {}).items():
        for term in terms or []:
            normalized = _compact(str(term))
            if normalized and normalized in compact:
                matches.append((len(normalized), str(function).upper()))
    return max(matches, default=(0, ""))[1] or None


def _match_dimensions(compact: str, registry: dict[str, Any]) -> list[str]:
    matches: list[str] = []
    for dimension_id, spec in (registry.get("dimensions") or {}).items():
        if not isinstance(spec, dict):
            continue
        if any(_contains(compact, term) for term in spec.get("terms", [])):
            matches.append(str(dimension_id))
    return matches


def _match_filters(compact: str, registry: dict[str, Any], query: str) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    for filter_id, spec in (registry.get("filters") or {}).items():
        if not isinstance(spec, dict):
            continue
        if any(_contains(compact, term) for term in spec.get("terms", [])):
            filters.append({"id": str(filter_id), "label": spec.get("label", filter_id)})
    recent = _RECENT_DAYS_RE.search(query)
    if recent:
        filters.append({"id": "recent_days", "label": f"최근 {int(recent.group(1))}일", "days": int(recent.group(1))})
    return filters


def _select_source(metric: dict[str, Any], dimensions: list[str]) -> dict[str, Any] | None:
    sources = [source for source in metric.get("sources", []) if isinstance(source, dict)]
    requested = set(dimensions)
    for source in sources:
        triggers = set(source.get("whenAnyDimensions") or [])
        supported = set(source.get("supportedDimensions") or [])
        if triggers and requested & triggers and requested <= supported:
            return source
    for source in sources:
        supported = set(source.get("supportedDimensions") or [])
        if not source.get("whenAnyDimensions") and requested <= supported:
            return source
    return None


def analyze_analytical_intent(
    query: str,
    registry_path: Path = DEFAULT_ANALYTICS_REGISTRY_PATH,
) -> dict[str, Any] | None:
    """Return a structured aggregate intent, or ``None`` for a non-aggregate query.

    The detector intentionally requires a registered metric.  An aggregate-like
    request with an unknown metric is returned as unsupported instead of being
    silently converted to a member list.
    """
    registry = load_analytics_registry(str(registry_path))
    if not registry or not isinstance(query, str) or not query.strip():
        return None
    compact = _compact(query)
    metric = _match_metric(compact, registry)
    function = _match_aggregate_function(compact, registry)
    dimensions = _match_dimensions(compact, registry)

    aggregate_signal = bool(function or dimensions or _OUTPUT_ACTION_RE.search(query))
    if metric is None:
        if function:
            return {
                "query_type": "aggregate",
                "aggregate_function": function,
                "metric": None,
                "dimensions": dimensions,
                "filters": [],
                "unsupported_reason": "unresolved_aggregate_metric",
                "unsupported_message": "요청한 집계 지표를 스키마의 수치 지표와 연결할 수 없습니다.",
            }
        return None
    # Amount thresholds and ranking phrases are audience conditions, not scalar/grouped analytics.
    if _TARGETING_COMPARISON_RE.search(query) and not function and not dimensions:
        return None
    if not aggregate_signal:
        return None
    if any(_contains(compact, term) for term in registry.get("unsupportedMetricQualifiers", [])):
        return {
            "query_type": "aggregate",
            "aggregate_function": function or metric.get("defaultFunction"),
            "metric": metric.get("id"),
            "dimensions": dimensions,
            "filters": [],
            "unsupported_reason": "unsupported_aggregate_metric_qualifier",
            "unsupported_message": "요청한 예측/미래 지표는 현재 스키마에서 안전하게 계산할 수 없습니다.",
        }

    function = function or str(metric.get("defaultFunction") or "").upper() or None
    allowed = {str(value).upper() for value in metric.get("allowedFunctions", [])}
    filters = _match_filters(compact, registry, query)
    source = _select_source(metric, dimensions)
    if function not in allowed or source is None:
        return {
            "query_type": "aggregate",
            "aggregate_function": function,
            "metric": metric.get("id"),
            "dimensions": dimensions,
            "filters": filters,
            "unsupported_reason": "unsupported_aggregate_contract",
            "unsupported_message": "요청한 지표·집계 함수·그룹 기준 조합을 안전하게 생성할 수 없습니다.",
        }
    return {
        "query_type": "aggregate",
        "aggregate_function": function,
        "metric": metric.get("id"),
        "dimensions": dimensions,
        "filters": filters,
        "source_id": source.get("id"),
    }


def _source_for_intent(intent: dict[str, Any], registry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    metric = next(item for item in registry.get("metrics", []) if item.get("id") == intent.get("metric"))
    source = next(item for item in metric.get("sources", []) if item.get("id") == intent.get("source_id"))
    return metric, source


def _field_ref(mapping: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity": mapping["entity"], "field": mapping["field"],
        "table": mapping["table"], "column": mapping["column"],
        "alias": mapping.get("alias"),
    }


def build_aggregation_request(
    intent: dict[str, Any],
    registry_path: Path = DEFAULT_ANALYTICS_REGISTRY_PATH,
) -> dict[str, Any]:
    registry = load_analytics_registry(str(registry_path))
    _metric, source = _source_for_intent(intent, registry)
    dimensions: list[dict[str, Any]] = []
    for dimension_id in intent.get("dimensions", []):
        mapping = registry["dimensions"][dimension_id]["mappings"][source["id"]]
        dimensions.append(_field_ref(mapping))

    filters: list[dict[str, Any]] = []
    for raw in source.get("fixedFilters", []):
        filters.append({**_field_ref(raw), "id": raw["id"], "operator": raw["operator"], "value": raw.get("value")})
    for item in intent.get("filters", []):
        if item.get("id") == "recent_days":
            date_field = source.get("dateField")
            if not isinstance(date_field, dict):
                raise ValueError("selected aggregate source does not support a relative date filter")
            filters.append({
                **_field_ref(date_field), "id": "recent_days", "operator": "gte",
                "value": f"P{int(item['days'])}D",
            })
            continue
        spec = registry["filters"][item["id"]]
        mapping = spec["mappings"][source["id"]]
        filters.append({
            **_field_ref(mapping), "id": item["id"],
            "operator": mapping.get("operator", "eq"), "value": mapping.get("value"),
        })

    measure = source["measure"]
    metric_id = str(intent["metric"])
    return {
        "targetEntity": metric_id if not dimensions else metric_id + "_by_" + "_".join(intent["dimensions"]),
        "outputColumns": dimensions,
        "filters": filters,
        "groupings": dimensions,
        "aggregations": [{
            "id": metric_id,
            "function": str(intent["aggregate_function"]).casefold(),
            "entity": measure["entity"], "field": measure["field"],
            "table": measure["table"], "column": measure["column"],
            "alias": measure.get("alias", "TOTAL_PURCHASE_AMOUNT"),
            "distinct": bool(measure.get("distinct", False)),
        }],
        "derivedMetrics": [], "sorting": [],
        "ranking": {"enabled": False, "partitionBy": []},
        "postAggregationFilters": [], "relationConditions": [],
        "dateGrain": None, "comparison": None,
        "businessRules": {
            "sourceId": source["id"],
            "intentContract": {
                key: intent.get(key) for key in ("query_type", "aggregate_function", "metric", "dimensions", "filters")
            },
        },
        "assumptions": source.get("assumptions", []),
        "unresolvedFields": [],
    }


def _sql_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _filter_sql(item: dict[str, Any], expression: str) -> str:
    operator = item.get("operator")
    value = item.get("value")
    if operator == "gte" and isinstance(value, str) and re.fullmatch(r"P\d+D", value, re.IGNORECASE):
        days = int(value[1:-1])
        return f"{expression} >= CONVERT(CHAR(8), DATEADD(DAY, -{days}, GETDATE()), 112)"
    sql_operator = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}.get(str(operator))
    if sql_operator is None:
        raise ValueError(f"unsupported analytical filter operator: {operator}")
    return f"{expression} {sql_operator} {_sql_literal(value)}"


def compile_aggregation_ast(
    intent: dict[str, Any],
    request: dict[str, Any],
    registry_path: Path = DEFAULT_ANALYTICS_REGISTRY_PATH,
) -> SelectAst:
    """Compile a registered aggregation contract to the project SelectAst."""
    registry = load_analytics_registry(str(registry_path))
    _metric, source = _source_for_intent(intent, registry)
    dependencies: set[str] = set()
    columns: list[str] = []
    groups: list[str] = []
    expression_by_field: dict[tuple[str, str], str] = {}

    for dimension_id in intent.get("dimensions", []):
        mapping = registry["dimensions"][dimension_id]["mappings"][source["id"]]
        expression = mapping["expression"]
        columns.append(f"{expression} AS {mapping.get('outputAlias', mapping['column'])}")
        groups.append(expression)
        dependencies.update(mapping.get("dependencies", []))
        expression_by_field[(mapping["table"].casefold(), mapping["column"].casefold())] = expression

    measure = source["measure"]
    measure_expression = measure["expression"]
    function = str(intent["aggregate_function"]).upper()
    distinct = "DISTINCT " if measure.get("distinct") else ""
    columns.append(f"{function}({distinct}{measure_expression}) AS {measure.get('alias', 'AGGREGATE_VALUE')}")

    where: list[str] = []
    for item in request.get("filters", []):
        key = (str(item.get("table", "")).casefold(), str(item.get("column", "")).casefold())
        expression = expression_by_field.get(key)
        if expression is None:
            if item.get("id") == "recent_days":
                mapping = source["dateField"]
            else:
                mapping = next(
                    (raw for raw in source.get("fixedFilters", []) if raw.get("id") == item.get("id")),
                    None,
                )
                if mapping is None:
                    mapping = registry["filters"][item["id"]]["mappings"][source["id"]]
            expression = mapping["expression"]
            dependencies.update(mapping.get("dependencies", []))
        where.append(_filter_sql(item, expression))

    joins = []
    for join in source.get("joins", []):
        if join.get("id") in dependencies:
            joins.append(f"     INNER JOIN {join['table']} {join['alias']} ON {join['on']}")
    return SelectAst(
        columns=columns,
        from_lines=[f"FROM {source['table']} {source['alias']}", *joins],
        where=where,
        group_by=groups,
    )

