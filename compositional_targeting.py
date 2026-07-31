"""Composable temporal-attribute targeting.

This module deliberately registers data primitives, not sentence-specific
capabilities.  Natural language is lowered to:

    attribute + window + aggregate + comparison

The compiler then binds that expression through the semantic attribute
catalog.  A new physical attribute is added in JSON; operators such as stable,
changed, and changed N times are shared by every compatible attribute.
"""

from __future__ import annotations

import sql_dialect

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from calendar_window import SOURCE_SPAN_KEY, parse_duration_window


PLAN_IR_KEY = "relational_ir"
PLAN_OPERATIONS_KEY = "relational_operations"
IR_VERSION = "1.0"
CATALOG_PATH = Path(__file__).parent / "docs" / "data" / "semantic_attribute_catalog.json"
SCHEMA_PATH = Path(__file__).parent / "docs" / "data" / "schema_catalog.json"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STABLE_RE = re.compile(
    r"(?:한번도)?(?:바뀌지않|변하지않|변경되지않|변동(?:이)?없|변화(?:가)?없|동일하게유지|그대로유지)"
)
_CHANGE_COUNT_RE = re.compile(
    r"(?P<count>\d+)(?:번|회)(?P<comparison>이상|초과|이하|미만|정확히|만큼)?"
    r"(?:바뀌|변하|변경|변동)"
)
_CHANGED_RE = re.compile(r"(?:바뀐|바뀌었|변한|변했|변경된|변경됐|변동한|변동됐)")
_COMPARISONS = {
    "이상": "gte",
    "초과": "gt",
    "이하": "lte",
    "미만": "lt",
    "정확히": "eq",
    "만큼": "eq",
}
_SQL_COMPARISONS = {"eq": "=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


@dataclass(frozen=True)
class AttributeMatch:
    attribute: dict[str, Any]
    synonym: str
    compact_span: tuple[int, int]


def _compact_with_offsets(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        chars.append(char.casefold())
        offsets.append(index)
    return "".join(chars), offsets


def _source_span(
    offsets: list[int], compact_start: int, compact_end: int, text_length: int
) -> dict[str, int]:
    if not offsets or compact_start >= len(offsets):
        return {"start": 0, "end": text_length}
    start = offsets[max(0, compact_start)]
    end_index = min(len(offsets) - 1, max(compact_start, compact_end - 1))
    return {"start": start, "end": offsets[end_index] + 1}


def _combined_span(
    text: str, offsets: list[int], spans: list[tuple[int, int]]
) -> dict[str, int]:
    present = [span for span in spans if span[1] > span[0]]
    if not present:
        return {"start": 0, "end": len(text)}
    return _source_span(
        offsets,
        min(start for start, _ in present),
        max(end for _, end in present),
        len(text),
    )


def _schema_columns(schema_path: Path) -> dict[str, set[str]]:
    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    tables = payload.get("tables") if isinstance(payload, dict) else {}
    result: dict[str, set[str]] = {}
    for table_name, table in (tables or {}).items():
        columns = table.get("columns") if isinstance(table, dict) else []
        result[str(table_name)] = {
            str(column.get("name"))
            for column in columns
            if isinstance(column, dict) and isinstance(column.get("name"), str)
        }
    return result


def load_catalog(
    catalog_path: Path = CATALOG_PATH, schema_path: Path = SCHEMA_PATH
) -> dict[str, Any]:
    """Load and verify all physical bindings against the approved schema."""

    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    attributes = payload.get("attributes") if isinstance(payload, dict) else None
    if not isinstance(attributes, list):
        raise ValueError("semantic attribute catalog requires attributes[]")
    physical = _schema_columns(schema_path)
    seen_ids: set[str] = set()
    seen_synonyms: dict[str, str] = {}
    for attribute in attributes:
        if not isinstance(attribute, dict):
            raise ValueError("catalog attribute must be an object")
        attribute_id = attribute.get("id")
        if not isinstance(attribute_id, str) or not attribute_id or attribute_id in seen_ids:
            raise ValueError(f"invalid or duplicate attribute id: {attribute_id!r}")
        seen_ids.add(attribute_id)
        synonyms = attribute.get("synonyms")
        if not isinstance(synonyms, list) or not synonyms:
            raise ValueError(f"{attribute_id} requires synonyms")
        for synonym in synonyms:
            normalized = "".join(str(synonym).split()).casefold()
            owner = seen_synonyms.get(normalized)
            if owner is not None and owner != attribute_id:
                raise ValueError(f"ambiguous synonym {synonym!r}: {owner}, {attribute_id}")
            seen_synonyms[normalized] = attribute_id
        for binding_name in ("current", "history"):
            binding = attribute.get(binding_name)
            if binding is None:
                continue
            if not isinstance(binding, dict):
                raise ValueError(f"{attribute_id}.{binding_name} must be an object or null")
            table = binding.get("table")
            if table not in physical:
                raise ValueError(f"{attribute_id}: unknown table {table!r}")
            required = {"entity_key", "value_column"}
            if binding_name == "history":
                required.add("time_column")
            for key in required:
                column = binding.get(key)
                if (
                    not isinstance(column, str)
                    or not _IDENTIFIER_RE.fullmatch(column)
                    or column not in physical[table]
                ):
                    raise ValueError(
                        f"{attribute_id}.{binding_name}.{key}: unknown column {column!r}"
                    )
            for column in binding.get("scope_columns") or []:
                if not isinstance(column, str) or column not in physical[table]:
                    raise ValueError(
                        f"{attribute_id}.{binding_name}.scope_columns: unknown {column!r}"
                    )
    return copy.deepcopy(payload)


def _find_attribute(compact: str, catalog: Mapping[str, Any]) -> AttributeMatch | None:
    matches: list[AttributeMatch] = []
    for attribute in catalog.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        for raw_synonym in attribute.get("synonyms") or []:
            synonym = "".join(str(raw_synonym).split()).casefold()
            if not synonym:
                continue
            for match in re.finditer(re.escape(synonym), compact):
                matches.append(AttributeMatch(
                    attribute=attribute,
                    synonym=str(raw_synonym),
                    compact_span=match.span(),
                ))
    if not matches:
        return None
    # The longest semantic name wins ("가치 등급" before the generic "등급").
    return sorted(
        matches,
        key=lambda item: (
            -(item.compact_span[1] - item.compact_span[0]),
            item.compact_span[0],
        ),
    )[0]


def _find_operation(compact: str) -> tuple[dict[str, Any], tuple[int, int]] | None:
    stable = _STABLE_RE.search(compact)
    if stable is not None:
        return (
            {
                "aggregate": "count_distinct",
                "comparison": {"operator": "eq", "value": 1},
                "semantic_operator": "stable",
            },
            stable.span(),
        )
    counted = _CHANGE_COUNT_RE.search(compact)
    if counted is not None:
        return (
            {
                "aggregate": "change_count",
                "comparison": {
                    "operator": _COMPARISONS.get(counted.group("comparison") or "이상", "gte"),
                    "value": int(counted.group("count")),
                },
                "semantic_operator": "changed_n_times",
            },
            counted.span(),
        )
    changed = _CHANGED_RE.search(compact)
    if changed is not None:
        return (
            {
                "aggregate": "count_distinct",
                "comparison": {"operator": "gt", "value": 1},
                "semantic_operator": "changed",
            },
            changed.span(),
        )
    return None


def _history_alternatives(
    attribute: Mapping[str, Any], catalog: Mapping[str, Any]
) -> list[dict[str, str]]:
    family = attribute.get("family")
    return [
        {"id": str(item.get("id")), "label": str(item.get("label"))}
        for item in catalog.get("attributes") or []
        if isinstance(item, dict)
        and item.get("family") == family
        and isinstance(item.get("history"), dict)
    ]


def interpret(
    query: str,
    *,
    catalog_path: Path = CATALOG_PATH,
    schema_path: Path = SCHEMA_PATH,
) -> dict[str, Any] | None:
    """Interpret a generic temporal attribute predicate, or return ``None``.

    ``None`` means this is not this IR's domain.  A returned non-resolved IR
    means the semantics were recognized but cannot safely be bound.
    """

    compact, offsets = _compact_with_offsets(query)
    catalog = load_catalog(catalog_path, schema_path)
    attribute_match = _find_attribute(compact, catalog)
    operation_match = _find_operation(compact)
    window = parse_duration_window(query, exclude_past=True)
    if attribute_match is None or operation_match is None or window is None:
        return None

    operation, operation_span = operation_match
    compact_window_span = window.get(SOURCE_SPAN_KEY)
    if not (
        isinstance(compact_window_span, tuple)
        and len(compact_window_span) == 2
        and all(isinstance(value, int) for value in compact_window_span)
    ):
        return None
    # ``parse_duration_window`` deliberately returns only the numeric duration
    # token.  In a semantic claim the adjacent rolling anchor is part of that
    # same condition; leaving "최근" unowned makes the downstream source audit
    # misclassify it as a separate unresolved condition.
    window_start, window_end = compact_window_span
    anchor = re.search(r"(?:최근|지난|직전)$", compact[:window_start])
    if anchor is not None:
        compact_window_span = (anchor.start(), window_end)
    span = _combined_span(
        query,
        offsets,
        [
            attribute_match.compact_span,
            operation_span,
            compact_window_span,
        ],
    )
    source_text = query[span["start"]:span["end"]]
    attribute = attribute_match.attribute
    history = attribute.get("history")
    base = {
        "version": IR_VERSION,
        "kind": "attribute_aggregate_filter",
        "status": "resolved",
        "target_entity": "member",
        "source_text": source_text,
        "source_span": span,
        "attribute": {
            "id": attribute.get("id"),
            "label": attribute.get("label"),
            "family": attribute.get("family"),
            "matched_synonym": attribute_match.synonym,
        },
        "window": {
            "kind": "rolling",
            "value": window.get("value"),
            "unit": window.get("unit"),
            "anchor": "latest_available_snapshot",
        },
        **operation,
        "missing_fields": [],
        "candidates": [],
    }
    if not isinstance(history, dict):
        candidates = _history_alternatives(attribute, catalog)
        labels = ", ".join(item["label"] for item in candidates)
        base.update({
            "status": "needs_clarification",
            "binding": None,
            "missing_fields": ["attribute.history_binding"],
            "candidates": candidates,
            "message": (
                f"‘{attribute_match.synonym}’은 {attribute.get('label')} 컬럼으로 연결되지만 "
                "월별 변경 이력이 없습니다. "
                f"비교할 월별 등급을 지정해 주세요: {labels}."
            ),
        })
        return base

    unit = window.get("unit")
    value = window.get("value")
    if not isinstance(value, int) or value <= 0:
        return None
    if history.get("time_grain") != "month" or unit not in {"months", "years"}:
        base.update({
            "status": "unsupported",
            "binding": copy.deepcopy(history),
            "missing_fields": [],
            "message": (
                f"{attribute.get('label')} 이력은 월 단위입니다. "
                "기간을 개월 또는 년 단위로 지정해 주세요."
            ),
        })
        return base
    observation_count = value if unit == "months" else value * 12
    base["window"]["observation_count"] = observation_count
    base["binding"] = copy.deepcopy(history)
    return base


def apply_to_plan(
    query: str,
    plan: dict[str, Any],
    *,
    catalog_path: Path = CATALOG_PATH,
    schema_path: Path = SCHEMA_PATH,
) -> None:
    ir = interpret(query, catalog_path=catalog_path, schema_path=schema_path)
    if ir is None:
        plan.pop(PLAN_IR_KEY, None)
        plan.pop(PLAN_OPERATIONS_KEY, None)
        return
    plan[PLAN_IR_KEY] = ir
    if ir.get("status") == "resolved":
        plan[PLAN_OPERATIONS_KEY] = [copy.deepcopy(ir)]
    else:
        plan.pop(PLAN_OPERATIONS_KEY, None)


def compile_sql(
    operation: Mapping[str, Any],
    *,
    member_table: str,
    member_alias: str,
    member_key: str,
    member_select_columns: list[str],
    member_predicates: list[str],
    segment_label: str,
) -> str:
    """Compile a verified relational operation to deterministic T-SQL."""

    if operation.get("status") != "resolved":
        raise ValueError("only resolved relational operations are executable")
    binding = operation.get("binding")
    window = operation.get("window")
    comparison = operation.get("comparison")
    if not all(isinstance(item, Mapping) for item in (binding, window, comparison)):
        raise ValueError("incomplete relational operation")
    table = str(binding.get("table") or "")
    entity_key = str(binding.get("entity_key") or "")
    time_column = str(binding.get("time_column") or "")
    value_column = str(binding.get("value_column") or "")
    identifiers = (table, entity_key, time_column, value_column, member_table, member_alias, member_key)
    if not all(_IDENTIFIER_RE.fullmatch(item) for item in identifiers):
        raise ValueError("unsafe relational binding")
    observation_count = window.get("observation_count")
    if not isinstance(observation_count, int) or observation_count <= 0:
        raise ValueError("invalid observation count")
    operator = comparison.get("operator")
    threshold = comparison.get("value")
    if operator not in _SQL_COMPARISONS or not isinstance(threshold, int):
        raise ValueError("invalid aggregate comparison")
    sql_operator = _SQL_COMPARISONS[str(operator)]

    ctes = [
        "WINDOW_MONTHS AS (",
        f"  SELECT DISTINCT TOP ({observation_count}) S.{time_column}",
        f"  FROM {table} S",
        f"  WHERE S.{time_column} IS NOT NULL",
        f"  ORDER BY S.{time_column} DESC",
        "),",
        "ATTRIBUTE_MONTHS AS (",
        f"  SELECT S.{entity_key} AS MEMBER_NO, S.{time_column} AS SNAPSHOT_TIME,",
        f"         MIN(S.{value_column}) AS ATTRIBUTE_VALUE",
        f"  FROM {table} S",
        f"  WHERE S.{time_column} IN (SELECT {time_column} FROM WINDOW_MONTHS)",
        f"    AND S.{value_column} IS NOT NULL",
        f"  GROUP BY S.{entity_key}, S.{time_column}",
        f"  HAVING COUNT(DISTINCT S.{value_column}) = 1",
        "),",
    ]
    aggregate = operation.get("aggregate")
    if aggregate == "change_count":
        ctes.extend([
            "ATTRIBUTE_SEQUENCE AS (",
            "  SELECT MEMBER_NO, SNAPSHOT_TIME, ATTRIBUTE_VALUE,",
            "         LAG(ATTRIBUTE_VALUE) OVER (PARTITION BY MEMBER_NO ORDER BY SNAPSHOT_TIME) AS PREVIOUS_VALUE",
            "  FROM ATTRIBUTE_MONTHS",
            "),",
            "ATTRIBUTE_SUMMARY AS (",
            "  SELECT MEMBER_NO",
            "  FROM ATTRIBUTE_SEQUENCE",
            "  GROUP BY MEMBER_NO",
            f"  HAVING COUNT(DISTINCT SNAPSHOT_TIME) = {observation_count}",
            "     AND SUM(CASE WHEN PREVIOUS_VALUE IS NOT NULL",
            f"                  AND PREVIOUS_VALUE <> ATTRIBUTE_VALUE THEN 1 ELSE 0 END) {sql_operator} {threshold}",
            ")",
        ])
    elif aggregate == "count_distinct":
        ctes.extend([
            "ATTRIBUTE_SUMMARY AS (",
            "  SELECT MEMBER_NO",
            "  FROM ATTRIBUTE_MONTHS",
            "  GROUP BY MEMBER_NO",
            f"  HAVING COUNT(DISTINCT SNAPSHOT_TIME) = {observation_count}",
            f"     AND COUNT(DISTINCT ATTRIBUTE_VALUE) {sql_operator} {threshold}",
            ")",
        ])
    else:
        raise ValueError(f"unsupported aggregate: {aggregate!r}")

    predicates = list(dict.fromkeys(item for item in member_predicates if item))
    lines = [
        "WITH " + "\n".join(ctes),
        "SELECT DISTINCT " + ", ".join([
            *member_select_columns,
            sql_dialect.quote_literal(segment_label) + " AS segment_label",
        ]),
        f"FROM {member_table} {member_alias}",
        f"INNER JOIN ATTRIBUTE_SUMMARY R ON R.MEMBER_NO = {member_alias}.{member_key}",
    ]
    if predicates:
        lines.append("WHERE " + "\n  AND ".join(predicates))
    return "\n".join(lines)


def validation_terms(operation: Mapping[str, Any]) -> list[str]:
    binding = operation.get("binding") if isinstance(operation.get("binding"), Mapping) else {}
    window = operation.get("window") if isinstance(operation.get("window"), Mapping) else {}
    comparison = (
        operation.get("comparison")
        if isinstance(operation.get("comparison"), Mapping)
        else {}
    )
    operator = _SQL_COMPARISONS.get(str(comparison.get("operator")), "")
    return [
        str(binding.get("table") or ""),
        str(binding.get("time_column") or ""),
        str(binding.get("value_column") or ""),
        f"TOP ({window.get('observation_count')})",
        f"COUNT(DISTINCT SNAPSHOT_TIME) = {window.get('observation_count')}",
        (
            f"COUNT(DISTINCT ATTRIBUTE_VALUE) {operator} {comparison.get('value')}"
            if operation.get("aggregate") == "count_distinct"
            else "LAG(ATTRIBUTE_VALUE)"
        ),
    ]
