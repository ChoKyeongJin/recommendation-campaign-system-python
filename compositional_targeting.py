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
from pathlib import Path
from typing import Any, Mapping



PLAN_IR_KEY = "relational_ir"
PLAN_OPERATIONS_KEY = "relational_operations"
SLOT_KEY = "relational_operation"  # target_user 구조화 슬롯 이름(LLM·결정론 감지기 공용)
IR_VERSION = "1.0"
DEFAULT_ATTRIBUTE_CATALOG_PATH = (
    Path(__file__).resolve().parent
    / "docs" / "data" / "runtime" / "semantics" / "attribute_catalog.json"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_YYYYMM_RE = re.compile(r"^\d{6}$")
_KOREAN_SMALL_COUNTS = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5}
_STABLE_RE = re.compile(
    r"(?:한번도)?(?:바뀌지않|변하지않|변경되지않|변동(?:이)?없|변화(?:가)?없|동일하게유지|그대로유지)"
)
_CHANGE_COUNT_RE = re.compile(
    r"(?P<count>\d+|한|두|세|네|다섯)(?:번|회)(?P<comparison>이상|초과|이하|미만|정확히|만큼)?"
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
    if not isinstance(binding, Mapping):
        raise ValueError("incomplete relational operation")
    table = str(binding.get("table") or "")
    entity_key = str(binding.get("entity_key") or "")
    time_column = str(binding.get("time_column") or "")
    value_column = str(binding.get("value_column") or "")
    identifiers = (table, entity_key, time_column, value_column, member_table, member_alias, member_key)
    if not all(_IDENTIFIER_RE.fullmatch(item) for item in identifiers):
        raise ValueError("unsafe relational binding")
    if operation.get("aggregate") in ("as_of", "transition"):
        return _compile_snapshot_sql(
            operation,
            member_table=member_table,
            member_alias=member_alias,
            member_key=member_key,
            member_select_columns=member_select_columns,
            member_predicates=member_predicates,
            segment_label=segment_label,
        )
    window = operation.get("window")
    comparison = operation.get("comparison")
    if not all(isinstance(item, Mapping) for item in (window, comparison)):
        raise ValueError("incomplete relational operation")
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


def _values_predicate(column_expr: str, values: list[str]) -> str:
    quoted = [sql_dialect.quote_literal(value) for value in sorted(dict.fromkeys(values))]
    if len(quoted) == 1:
        return f"{column_expr} = {quoted[0]}"
    return f"{column_expr} IN ({', '.join(quoted)})"


def _compile_snapshot_sql(
    operation: Mapping[str, Any],
    *,
    member_table: str,
    member_alias: str,
    member_key: str,
    member_select_columns: list[str],
    member_predicates: list[str],
    segment_label: str,
) -> str:
    """as-of(기준 스냅샷 값)·transition(직전→현재 전이)을 스냅샷 단일 조인 SQL 로 컴파일한다."""

    binding = operation["binding"]
    table = str(binding.get("table") or "")
    entity_key = str(binding.get("entity_key") or "")
    time_column = str(binding.get("time_column") or "")
    value_column = str(binding.get("value_column") or "")
    anchor = operation.get("anchor") if isinstance(operation.get("anchor"), Mapping) else {}
    if anchor.get("type") == "month":
        month = str(anchor.get("month") or "")
        if not _YYYYMM_RE.fullmatch(month):
            raise ValueError("invalid snapshot month anchor")
        anchor_expr = sql_dialect.quote_literal(month)
    else:
        anchor_expr = f"(SELECT MAX({time_column}) FROM {table})"
    value_predicate = operation.get("value_predicate")
    values = list((value_predicate or {}).get("values") or [])
    if not values or not all(isinstance(item, str) and item for item in values):
        raise ValueError("snapshot operation requires value predicate")
    predicates = [_values_predicate(f"S.{value_column}", values)]
    if operation.get("aggregate") == "transition":
        prev_column = str(binding.get("prev_value_column") or "")
        if not _IDENTIFIER_RE.fullmatch(prev_column):
            raise ValueError("transition requires a safe prev_value_column")
        prev_values = list((operation.get("prev_predicate") or {}).get("values") or [])
        if prev_values:
            predicates.append(_values_predicate(f"S.{prev_column}", prev_values))
        else:
            # 출발 값 미지정 전이('승급한')는 최소한 '직전과 값이 다름'을 강제한다 —
            # 이것마저 없으면 현재 값 필터로 조용히 축소되는 바로 그 오답이 된다.
            predicates.append(f"S.{prev_column} <> S.{value_column}")
    predicates.extend(dict.fromkeys(item for item in member_predicates if item))
    lines = [
        "SELECT DISTINCT " + ", ".join([
            *member_select_columns,
            sql_dialect.quote_literal(segment_label) + " AS segment_label",
        ]),
        f"FROM {member_table} {member_alias}",
        f"INNER JOIN {table} S ON S.{entity_key} = {member_alias}.{member_key}",
        f"  AND S.{time_column} = {anchor_expr}",
        "WHERE " + "\n  AND ".join(predicates),
    ]
    return "\n".join(lines)


# ── 속성 카탈로그: 물리 바인딩은 JSON, 값 사전은 eq_filters 참조 조인(이중 소유 금지) ──────────

# 값 사전(eq_filters synonyms)에 없는 결정론 감지 전용 표면형. 값 소유권은 여전히 eq_filters 이며,
# 여기는 "정상인/휴면이던"처럼 수식형으로 등장하는 낱말만 보탠다(파싱 규칙은 JSON 아닌 소스 소유).
_DETECTOR_EXTRA_VALUE_TOKENS: dict[str, list[str]] = {
    "normal_member": ["정상"],
    "dormant": ["휴면"],
}

# 지원 연산자. 다월(multi-month) 연산은 snapshot_months_available 이 창 크기 이상일 때만 열린다.
OPERATORS = frozenset({
    "as_of_latest", "as_of_month", "transition",
    "held_throughout", "stable", "changed_n_times",
    "ever", "never", "exists_every_month",
})
MULTI_MONTH_OPERATORS = frozenset({
    "held_throughout", "stable", "changed_n_times", "ever", "never", "exists_every_month",
})
# 다월 데이터가 있어도 아직 컴파일러가 없는 연산(값 앵커가 요약 CTE에 없음).
UNIMPLEMENTED_OPERATORS = frozenset({"held_throughout", "ever", "never"})


def load_attribute_catalog(
    eq_filter_entries: list[Mapping[str, Any]],
    path: Path | str = DEFAULT_ATTRIBUTE_CATALOG_PATH,
) -> dict[str, Any]:
    """attribute_catalog.json 과 eq_filters 값 사전을 조인한 런타임 카탈로그를 만든다.

    반환: {"attributes": {attribute_id: {label, binding, snapshot_months_available,
    history_unsupported_reason?, values: {canonical: {value, rank, synonyms}}, surface_terms}}}
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    attributes_raw = raw.get("attributes")
    if not isinstance(attributes_raw, Mapping) or not attributes_raw:
        raise ValueError("attribute catalog must declare a non-empty attributes mapping")
    by_category: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in eq_filter_entries or []:
        if not isinstance(entry, Mapping):
            continue
        category = str(entry.get("category") or "")
        canonical = str(entry.get("canonical") or "")
        value = str(entry.get("value") or "")
        if not category or not canonical or not value:
            continue
        by_category.setdefault(category, {})[canonical] = {
            "value": value,
            "rank": entry.get("rank"),
            "synonyms": [str(s) for s in entry.get("synonyms") or [] if str(s).strip()],
        }
    attributes: dict[str, Any] = {}
    for attribute_id, spec in attributes_raw.items():
        if not isinstance(spec, Mapping):
            continue
        binding = spec.get("binding") if isinstance(spec.get("binding"), Mapping) else None
        if binding is not None:
            for key in ("table", "entity_key", "time_column", "value_column"):
                if not _IDENTIFIER_RE.fullmatch(str(binding.get(key) or "")):
                    raise ValueError(
                        f"attribute {attribute_id!r} binding.{key} is not a safe identifier"
                    )
            prev_column = binding.get("prev_value_column")
            if prev_column is not None and not _IDENTIFIER_RE.fullmatch(str(prev_column)):
                raise ValueError(
                    f"attribute {attribute_id!r} binding.prev_value_column is not a safe identifier"
                )
        attributes[str(attribute_id)] = {
            "id": str(attribute_id),
            "label": str(spec.get("label") or attribute_id),
            "binding": dict(binding) if binding is not None else None,
            "snapshot_months_available": int(spec.get("snapshot_months_available") or 0),
            "history_unsupported_reason": spec.get("history_unsupported_reason"),
            "values": by_category.get(str(spec.get("value_category") or ""), {}),
            "surface_terms": [str(t) for t in spec.get("surface_terms") or []],
        }
    return {"attributes": attributes}


def slot_vocab(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """LLM 슬롯 coerce 용 닫힌 어휘: {"attributes": {id: {"value_tokens": {compact: canonical}}}}."""
    attributes: dict[str, Any] = {}
    for attribute_id, spec in (catalog.get("attributes") or {}).items():
        tokens: dict[str, str] = {}
        for canonical, value_spec in (spec.get("values") or {}).items():
            candidates = [canonical, str(value_spec.get("value") or "")]
            candidates.extend(value_spec.get("synonyms") or [])
            candidates.extend(_DETECTOR_EXTRA_VALUE_TOKENS.get(canonical, []))
            for token in candidates:
                compact = re.sub(r"\s+", "", token or "").casefold()
                if compact:
                    tokens.setdefault(compact, canonical)
        attributes[attribute_id] = {"value_tokens": tokens}
    return {"attributes": attributes}


def _value_token_index(catalog: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """감지·coerce 용 값 토큰 인덱스: compact 토큰 → (attribute_id, canonical)."""
    index: dict[str, tuple[str, str]] = {}
    for attribute_id, spec in (catalog.get("attributes") or {}).items():
        for canonical, value_spec in (spec.get("values") or {}).items():
            tokens = [canonical, str(value_spec.get("value") or "")]
            tokens.extend(value_spec.get("synonyms") or [])
            tokens.extend(_DETECTOR_EXTRA_VALUE_TOKENS.get(canonical, []))
            for token in tokens:
                compact = re.sub(r"\s+", "", token or "").casefold()
                if compact:
                    index.setdefault(compact, (attribute_id, canonical))
    return index


def _value_alternation(index: Mapping[str, tuple[str, str]]) -> str:
    return "|".join(
        re.escape(token) for token in sorted(index, key=lambda t: (-len(t), t))
    )


def _detect_months(compact: str) -> int | None:
    match = re.search(r"(?:최근|지난)(\d+)개월", compact)
    if match:
        return int(match.group(1))
    if "상반기" in compact or "하반기" in compact:
        return 6
    match = re.search(r"(\d+)개월(?:동안|내내|간)", compact)
    if match:
        return int(match.group(1))
    return None


# `detect_member_attribute_history` 는 2026-08-02 삭제됐다 — 원문을 연산자 패턴 10종으로 훑어
# relational_operation 슬롯을 만들던 결정론 감지기다. 그 의미는 SemanticPlanV2
# RelationPredicate 노드가 소유하고, 슬롯은 LegacyQueryPlanCompiler 만 쓴다.
# 이 모듈에 남는 것은 카탈로그 로더·리졸버(resolve_operation)·스냅샷 SQL 컴파일러다.




def _expand_value_predicate(
    attribute: Mapping[str, Any], canonical: str, comparison: str
) -> list[str] | None:
    """등급/상태 canonical 하나를 물리 값 목록으로 넓힌다(순수 변환 — 원문을 읽지 않는다).

    'VIP 이상'처럼 순서 비교가 오면 값 사전의 rank 로 확장하고, eq 면 그 값 하나다.
    canonical 이 사전에 없거나 rank 가 없으면 None — 조용한 추측 대신 확인 요청으로 귀결된다.
    """
    values = attribute.get("values") or {}
    spec = values.get(canonical)
    if not isinstance(spec, Mapping):
        return None
    if comparison == "eq":
        value = str(spec.get("value") or "")
        return [value] if value else None
    rank = spec.get("rank")
    if not isinstance(rank, int):
        return None
    selected = [
        str(other.get("value"))
        for other in values.values()
        if isinstance(other.get("rank"), int)
        and (other["rank"] >= rank if comparison == "gte" else other["rank"] <= rank)
    ]
    return sorted(selected) or None


def resolve_operation(
    slot: Mapping[str, Any], catalog: Mapping[str, Any]
) -> dict[str, Any]:
    """검증된 슬롯을 실행 가능(resolved) 또는 정직한 차단(unsupported/needs_clarification)으로 귀결한다."""

    def _blocked(status: str, message: str) -> dict[str, Any]:
        return {
            "status": status,
            "message": message,
            "operator": slot.get("operator"),
            "attribute_id": slot.get("attribute_id"),
            "source_slot": copy.deepcopy(dict(slot)),
        }

    attribute_id = str(slot.get("attribute_id") or "")
    attribute = (catalog.get("attributes") or {}).get(attribute_id)
    if not isinstance(attribute, Mapping):
        return _blocked("needs_clarification", f"'{attribute_id}' 속성은 이력 카탈로그에 없습니다.")
    operator = str(slot.get("operator") or "")
    if operator not in OPERATORS:
        return _blocked("needs_clarification", f"'{operator}' 연산은 지원 목록에 없습니다.")
    label = str(attribute.get("label") or attribute_id)
    binding = attribute.get("binding")
    if not isinstance(binding, Mapping):
        reason = str(
            attribute.get("history_unsupported_reason")
            or f"{label}의 시점·이력 데이터 소스가 없습니다."
        )
        return _blocked("unsupported", reason)

    months = slot.get("months")
    if operator in MULTI_MONTH_OPERATORS:
        available = int(attribute.get("snapshot_months_available") or 0)
        window = int(months) if isinstance(months, int) and months > 0 else None
        if window is None:
            return _blocked(
                "needs_clarification",
                f"{label} 이력 조건의 기간(N개월)을 확정하지 못했습니다. '최근 3개월'처럼 기간을 명시해 주세요.",
            )
        if window > available:
            return _blocked(
                "unsupported",
                f"{label} 월별 스냅샷이 현재 {available}개월치만 적재되어 있어 "
                f"{window}개월 이력 조건(유지/변경 횟수/월별 존재 등)을 지원하지 않습니다. "
                "월별 이력 적재가 확장되면 자동으로 열립니다.",
            )
        if operator in UNIMPLEMENTED_OPERATORS:
            return _blocked(
                "unsupported",
                f"'{operator}' 이력 연산은 아직 지원되지 않습니다"
                "(기간 내 특정 값 보유/부재 판정 컴파일러 미구현).",
            )

    base: dict[str, Any] = {
        "status": "resolved",
        "attribute": {"id": attribute_id, "label": label},
        "binding": dict(binding),
        "semantic_operator": operator,
        "source_slot": copy.deepcopy(dict(slot)),
    }
    if operator in {"as_of_latest", "as_of_month"}:
        comparison = str(slot.get("value_comparison") or "eq")
        values = _expand_value_predicate(attribute, str(slot.get("value") or ""), comparison)
        if not values:
            return _blocked(
                "needs_clarification",
                f"{label} 조건의 값(예: VIP/골드)을 확정하지 못했습니다.",
            )
        anchor: dict[str, Any] = {"type": "latest"}
        if operator == "as_of_month":
            month = str(slot.get("month") or "")
            if not _YYYYMM_RE.fullmatch(month):
                return _blocked("needs_clarification", "기준 월(YYYYMM)을 확정하지 못했습니다.")
            if int(attribute.get("snapshot_months_available") or 0) <= 1:
                # 단일 스냅샷 적재에서 임의 월 리터럴은 조용한 빈 오디언스가 된다(리뷰 실증) —
                # 적재가 다월로 확장되면 카탈로그 숫자만 올리면 열린다.
                return _blocked(
                    "unsupported",
                    f"{label}의 월 지정 스냅샷 조회는 현재 최신 1개월 스냅샷만 적재되어 있어 "
                    "지원되지 않습니다. '최신 기준월' 기준으로 요청해 주세요.",
                )
            anchor = {"type": "month", "month": month}
        base.update({"aggregate": "as_of", "anchor": anchor, "value_predicate": {"values": values}})
        return base
    if operator == "transition":
        if isinstance(months, int) and months > 1:
            return _blocked(
                "unsupported",
                f"기간을 지정한 전이 조건({months}개월 동안 승급/변경)은 월별 이력 적재 후 지원됩니다. "
                "현재는 직전 스냅샷 대비 전이만 가능합니다 — '직전 등급 대비 승급'으로 다시 요청해 주세요.",
            )
        prev_column = binding.get("prev_value_column")
        if not prev_column:
            return _blocked("unsupported", f"{label}의 직전 값 컬럼이 없어 전이 조건을 지원하지 않습니다.")
        to_values = _expand_value_predicate(attribute, str(slot.get("to_value") or ""), "eq")
        if not to_values:
            return _blocked("needs_clarification", f"{label} 전이의 도착 값(예: VIP)을 확정하지 못했습니다.")
        from_values = None
        if slot.get("from_value"):
            from_values = _expand_value_predicate(attribute, str(slot.get("from_value")), "eq")
            if not from_values:
                return _blocked(
                    "needs_clarification", f"{label} 전이의 출발 값(예: 골드)을 확정하지 못했습니다."
                )
        base.update({
            "aggregate": "transition",
            "anchor": {"type": "latest"},
            "value_predicate": {"values": to_values},
            "prev_predicate": {"values": from_values} if from_values else None,
        })
        return {key: value for key, value in base.items() if value is not None}
    # 다월 연산(가용 범위 내): 기존 창 CTE 컴파일러로 낮춘다.
    window = {"observation_count": int(months)}
    if operator == "stable":
        base.update({"aggregate": "count_distinct", "window": window,
                     "comparison": {"operator": "eq", "value": 1}})
    elif operator == "exists_every_month":
        base.update({"aggregate": "count_distinct", "window": window,
                     "comparison": {"operator": "gte", "value": 1}})
    elif operator == "changed_n_times":
        count = slot.get("change_count")
        if not isinstance(count, int) or count < 0:
            return _blocked("needs_clarification", "변경 횟수 임계값을 확정하지 못했습니다.")
        base.update({"aggregate": "change_count", "window": window,
                     "comparison": {"operator": str(slot.get("change_count_operator") or "gte"),
                                    "value": count}})
    else:  # pragma: no cover — OPERATORS 닫힌 집합에서 남는 분기 없음
        return _blocked("needs_clarification", f"'{operator}' 연산을 해석하지 못했습니다.")
    return base


def resolve_slot_to_operations(query_plan: dict[str, Any], catalog: Mapping[str, Any]) -> str | None:
    """컴파일러가 쓴 relational_operation 슬롯을 실행 IR 또는 정직한 차단으로 귀결한다.

    원문을 읽지 않는다 — 슬롯이 없으면 아무것도 하지 않는다(예전 백필은 여기서 원문을
    정규식으로 다시 읽어 슬롯을 만들었고, 그것이 이중 해석의 한 축이었다).
    반환: "resolved" | "blocked" | None(이력 조건 없음)."""
    if query_plan.get(PLAN_OPERATIONS_KEY) or isinstance(query_plan.get(PLAN_IR_KEY), Mapping):
        return None
    target_user = query_plan.get('target_user')
    slot = target_user.get(SLOT_KEY) if isinstance(target_user, dict) else None
    if not isinstance(slot, Mapping):
        return None
    operation = resolve_operation(slot, catalog)
    if operation.get('status') == 'resolved':
        query_plan[PLAN_OPERATIONS_KEY] = [operation]
        return 'resolved'
    query_plan[PLAN_IR_KEY] = {
        'status': operation.get('status'),
        'message': operation.get('message'),
        'missing_fields': [],
        'operator': operation.get('operator'),
        'attribute_id': operation.get('attribute_id'),
        'source_slot': operation.get('source_slot'),
    }
    return 'blocked'

def validation_terms(operation: Mapping[str, Any]) -> list[str]:
    binding = operation.get("binding") if isinstance(operation.get("binding"), Mapping) else {}
    if operation.get("aggregate") in ("as_of", "transition"):
        values = list((operation.get("value_predicate") or {}).get("values") or [])
        terms = [
            str(binding.get("table") or ""),
            str(binding.get("time_column") or ""),
            str(binding.get("value_column") or ""),
            *[sql_dialect.quote_literal(value) for value in values],
        ]
        if operation.get("aggregate") == "transition":
            terms.append(str(binding.get("prev_value_column") or ""))
        return terms
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
