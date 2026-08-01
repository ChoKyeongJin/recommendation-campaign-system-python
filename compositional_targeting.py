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
