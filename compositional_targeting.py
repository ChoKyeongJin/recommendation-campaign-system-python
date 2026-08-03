"""Composable temporal-attribute targeting.

This module deliberately registers data primitives, not sentence-specific
capabilities.  Natural language is lowered to:

    attribute + window + aggregate + comparison

The compiler then binds that expression through the semantic attribute
catalog.  A new physical attribute is added in JSON; operators such as stable,
changed, and changed N times are shared by every compatible attribute.
"""

from __future__ import annotations

import event_ir
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
# 원문을 읽던 감지기(정규식 원자 + 소량 수사 + 스팬 재구성 헬퍼)는 2026-08-02 계층 분리에서
# 삭제됐다. 시간 한정어 감지는 도메인 계층(targeting_domain.temporal_lexicon)이 **범용 시간
# 연산자**로 사상하고, 이 모듈은 검증된 슬롯만 받아 SQL 로 낮춘다(원문을 읽지 않는다).
# 낱말형 → SQL 비교 기호. 표는 event_ir 이 단독 소유한다(기호 집합 바로 옆) — 여기서 다시
# 쓰면 그 순간 두 벌이 되고, 두 벌이 어긋난 상태가 정확히 R3 의 결함이었다.
_SQL_COMPARISONS = dict(event_ir.COMPARISON_OPERATOR_ALIASES)


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
    elif aggregate == "value_month_count":
        # 값 앵커가 있는 구간 판정(보유/부재/전구간 유지). 셋은 **같은 카운트의 다른 임계**다 —
        # 창 안에서 값이 V 인 월 수를 세고, ever = ≥1 / never = 0 / held_throughout = 전체 월수.
        # 관측 월 수를 전체로 요구하는 것은 fail-close 다: 안 보이는 달을 '아니었다'로 세면
        # 미관측을 부정으로 단정하게 된다(never 가 그 함정에 가장 취약하다).
        values = (operation.get("value_predicate") or {}).get("values") or []
        if not values:
            raise ValueError("value_month_count requires a value predicate")
        rendered = ", ".join(sql_dialect.quote_literal(str(value)) for value in values)
        ctes.extend([
            "ATTRIBUTE_SUMMARY AS (",
            "  SELECT MEMBER_NO",
            "  FROM ATTRIBUTE_MONTHS",
            "  GROUP BY MEMBER_NO",
            f"  HAVING COUNT(DISTINCT SNAPSHOT_TIME) = {observation_count}",
            f"     AND SUM(CASE WHEN ATTRIBUTE_VALUE IN ({rendered}) THEN 1 ELSE 0 END)"
            f" {sql_operator} {threshold}",
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
    if not all(isinstance(item, str) and item for item in values):
        raise ValueError("snapshot operation requires value predicate")
    is_transition = operation.get("aggregate") == "transition"
    if not values and not is_transition:
        # as_of 는 '어떤 값인가'가 조건의 전부다 — 값이 없으면 전체 회원이 된다.
        raise ValueError("snapshot operation requires value predicate")
    predicates = [_values_predicate(f"S.{value_column}", values)] if values else []
    if is_transition:
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
        if not values:
            # 도착값 미지정('직전 등급이 골드였던')도 같은 이유로 '값이 바뀌었다'를 요구한다 —
            # 없으면 '직전이 골드'가 '지금도 골드'를 포함해 조용히 넓어진다.
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
# 값 앵커 구간 판정 3종은 **같은 카운트의 다른 임계**다(창 안에서 값이 V 인 월 수).
# threshold=None 은 '창 전체 월수'라는 뜻으로, 창 길이에서 파생한다.
_VALUE_MONTH_COUNT_THRESHOLDS: dict[str, tuple[str, int | None]] = {
    "ever": ("gte", 1),
    "never": ("eq", 0),
    "held_throughout": ("eq", None),
}
# 다월 데이터가 있어도 아직 컴파일러가 없는 연산. 2026-08-02: 값 앵커 구간 판정 3종
# (held_throughout/ever/never)에 `value_month_count` 집계를 붙여 비었다.
UNIMPLEMENTED_OPERATORS: frozenset[str] = frozenset()


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


def attribute_resolver_specs(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """속성 **id** 해석용 스펙: {attribute_id: {label, synonyms}} (MetricResolver 입력 모양).

    `slot_vocab` 은 속성의 **값** 어휘를 돌려준다. 그것을 그대로 resolver 에 넣으면
    `{"attributes": {...}}` 가 통째로 스펙 하나로 읽혀 `attributes` 라는 이름의 지표 하나만
    생긴다 — 실측(2026-08-02): LLM 이 낸 `attribute='grade'` 가 어느 형태로도 해석되지 않아
    relation_predicate 노드가 타입 확정 단계에서 통째로 폐기됐다.

    동의어는 카탈로그가 단일 소유한다(`surface_terms`). 여기에 낱말을 나열하지 않는다.
    """
    specs: dict[str, Any] = {}
    for attribute_id, spec in (catalog.get("attributes") or {}).items():
        if not isinstance(spec, Mapping):
            continue
        specs[str(attribute_id)] = {
            "label": str(spec.get("label") or attribute_id),
            "synonyms": [
                str(term) for term in (spec.get("surface_terms") or []) if str(term).strip()
            ],
        }
    return specs


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


def _value_examples(attribute: Mapping[str, Any], limit: int = 2) -> str:
    """확인 요청 문구에 넣을 값 예시 — 낱말을 코드에 박지 않고 값 사전에서 뽑는다."""
    samples: list[str] = []
    for spec in (attribute.get("values") or {}).values():
        synonyms = spec.get("synonyms") or []
        if synonyms:
            samples.append(str(synonyms[0]))
        if len(samples) >= limit:
            break
    return f"(예: {'/'.join(samples)})" if samples else ""


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
    advisories: list[dict[str, Any]] = []
    if operator in MULTI_MONTH_OPERATORS:
        available = int(attribute.get("snapshot_months_available") or 0)
        window = int(months) if isinstance(months, int) and months > 0 else None
        if window is None:
            return _blocked(
                "needs_clarification",
                f"{label} 이력 조건의 기간(N개월)을 확정하지 못했습니다. '최근 3개월'처럼 기간을 명시해 주세요.",
            )
        if window > available:
            # 적재가 얕아도 **SQL 의 의미는 그대로다** — 관측 월 수를 세는 창 CTE 가
            # 조건을 충족하는 회원을 못 찾아 0건이 될 뿐이다. 0건은 정직한 답이므로 내보내고
            # 고지만 한다(semantic_capabilities.json 의 data_availability_policy 와 같은 기준).
            advisories.append({
                "code": "data_coverage_shallow",
                "message": (
                    f"{label} 월별 스냅샷이 현재 {available}개월치만 적재되어 있어 "
                    f"{window}개월 이력 조건의 결과가 0건일 수 있습니다. "
                    "월별 이력 적재가 확장되면 그대로 채워집니다."
                ),
                "required_months": window,
                "available_months": available,
            })
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
    if advisories:
        base["advisories"] = advisories
    if operator in {"as_of_latest", "as_of_month"}:
        comparison = str(slot.get("value_comparison") or "eq")
        values = _expand_value_predicate(attribute, str(slot.get("value") or ""), comparison)
        if not values:
            return _blocked(
                "needs_clarification",
                f"{label} 조건의 값{_value_examples(attribute)}을 확정하지 못했습니다.",
            )
        anchor: dict[str, Any] = {"type": "latest"}
        if operator == "as_of_month":
            month = str(slot.get("month") or "")
            if not _YYYYMM_RE.fullmatch(month):
                return _blocked("needs_clarification", "기준 월(YYYYMM)을 확정하지 못했습니다.")
            if int(attribute.get("snapshot_months_available") or 0) <= 1:
                # 지정 월 SQL 은 `YYYYMM = '지정월'` 로 의미가 정확하다 — 그 월이 적재되지
                # 않았으면 0건일 뿐이다. **조용한** 빈 오디언스가 문제였지 빈 결과 자체가
                # 문제가 아니므로, 막는 대신 이름을 대며 고지한다.
                base["advisories"] = [*base.get("advisories", []), {
                    "code": "data_coverage_shallow",
                    "message": (
                        f"{label}의 월별 스냅샷은 현재 최신 1개월만 적재되어 있어 "
                        f"{month} 기준 조회 결과가 0건일 수 있습니다."
                    ),
                    "requested_month": month,
                }]
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
        from_values = None
        if slot.get("from_value"):
            from_values = _expand_value_predicate(attribute, str(slot.get("from_value")), "eq")
            if not from_values:
                return _blocked(
                    "needs_clarification", f"{label} 전이의 출발 값{_value_examples(attribute)}을 확정하지 못했습니다."
                )
        # 전이는 **어느 한쪽 값만으로도 성립한다.** '직전 등급이 골드였던'은 출발값만 말하고
        # 도착값에는 아무 제약이 없다 — 도착값을 요구하면 이 문형이 통째로 막힌다(실측 #19).
        # 양쪽 다 없을 때만 되묻는다(그때는 전이라고 부를 것이 남지 않는다).
        if not to_values and not from_values:
            return _blocked(
                "needs_clarification",
                f"{label} 전이의 값{_value_examples(attribute)}을 확정하지 못했습니다.",
            )
        base.update({
            "aggregate": "transition",
            "anchor": {"type": "latest"},
            "value_predicate": {"values": to_values} if to_values else None,
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
    elif operator in _VALUE_MONTH_COUNT_THRESHOLDS:
        values = _expand_value_predicate(
            attribute, str(slot.get("value") or ""), str(slot.get("value_comparison") or "eq")
        )
        if not values:
            return _blocked(
                "needs_clarification",
                f"{label} 구간 조건의 값{_value_examples(attribute)}을 확정하지 못했습니다.",
            )
        comparison_operator, threshold = _VALUE_MONTH_COUNT_THRESHOLDS[operator]
        base.update({
            "aggregate": "value_month_count",
            "window": window,
            "value_predicate": {"values": values},
            "comparison": {
                "operator": comparison_operator,
                # 전구간 유지는 '창 안 전체 월수'가 임계다 — 창 길이에서 파생한다.
                "value": int(months) if threshold is None else threshold,
            },
        })
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
    aggregate = operation.get("aggregate")
    if aggregate == "count_distinct":
        summary = f"COUNT(DISTINCT ATTRIBUTE_VALUE) {operator} {comparison.get('value')}"
    elif aggregate == "value_month_count":
        # 값 앵커가 SQL 에 실제로 남았는지까지 확인한다 — 임계만 보면 '어떤 값을 세는지'가
        # 빠져도 통과한다(ever VIP 가 ever ANY 로 조용히 넓어지는 경로).
        rendered = ", ".join(
            sql_dialect.quote_literal(str(value))
            for value in (operation.get("value_predicate") or {}).get("values") or []
        )
        summary = (
            f"SUM(CASE WHEN ATTRIBUTE_VALUE IN ({rendered}) THEN 1 ELSE 0 END)"
            f" {operator} {comparison.get('value')}"
        )
    else:
        summary = "LAG(ATTRIBUTE_VALUE)"
    return [
        str(binding.get("table") or ""),
        str(binding.get("time_column") or ""),
        str(binding.get("value_column") or ""),
        f"TOP ({window.get('observation_count')})",
        f"COUNT(DISTINCT SNAPSHOT_TIME) = {window.get('observation_count')}",
        summary,
    ]
