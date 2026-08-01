"""일반 집계 요청 IR과 SQL AST 검증기.

자연어/LLM 계층과 물리 SQL 사이의 계약을 한 곳에 둔다.  이 모듈은 SQL을
생성하지 않는다. 제공된 스키마에 연결된 구조화 요구사항을 검증하고,
``sqlglot`` AST에서 실제 구현 증거를 독립적으로 확인한다.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

import sqlglot
from sqlglot import exp


AGGREGATION_FUNCTIONS = frozenset({
    "sum", "avg", "count", "count_distinct", "max", "min", "median",
    "percentile", "standard_deviation", "variance",
})
DATE_GRAINS = frozenset({"day", "week", "month", "quarter", "year"})
FILTER_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "is_null", "is_not_null"})
RANKING_TYPES = frozenset({"top", "bottom", "rank"})
RELATION_MODES = frozenset({"any", "all", "none", "at_least", "exactly"})
DERIVED_METRIC_TYPES = frozenset({"division", "growth_rate", "share", "conversion_rate", "difference", "cumulative"})
NUMERIC_TYPES = frozenset({
    "tinyint", "smallint", "int", "integer", "bigint", "decimal", "numeric",
    "float", "real", "double", "money", "smallmoney", "number",
})
ZERO_DIVISION_POLICIES = frozenset({"null", "zero"})
SORT_DIRECTIONS = frozenset({"asc", "desc"})
ROW_COUNT_FIELD = "*"  # COUNT(*) — 필드 참조가 없는 행 수 집계 표기


@dataclass(frozen=True)
class ValidationError:
    code: str
    message: str
    requirement_id: str | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class FieldRef:
    entity: str
    field: str
    table: str | None = None
    column: str | None = None
    alias: str | None = None

    @property
    def physical_name(self) -> str | None:
        if self.table and self.column:
            return f"{self.table}.{self.column}".casefold()
        return self.column.casefold() if self.column else None


@dataclass(frozen=True)
class FilterRequirement(FieldRef):
    id: str = "filter"
    operator: str = "eq"
    value: Any = None


@dataclass(frozen=True)
class AggregationRequirement(FieldRef):
    id: str = "aggregation"
    function: str = "count"
    distinct: bool = False
    condition: FilterRequirement | None = None

    @property
    def counts_rows(self) -> bool:
        """COUNT(*) 처럼 물리 컬럼 없이 행 수만 세는 집계인지."""
        return self.function == "count" and not self.column and self.field == ROW_COUNT_FIELD


@dataclass(frozen=True)
class DerivedMetricRequirement:
    id: str
    type: str
    alias: str
    numerator_metric_id: str | None = None
    denominator_metric_id: str | None = None
    current_metric_id: str | None = None
    previous_metric_id: str | None = None
    multiply_by: float | None = None
    zero_division_policy: str | None = None


@dataclass(frozen=True)
class SortRequirement:
    metric_id: str
    direction: str
    nulls: str | None = None


@dataclass(frozen=True)
class RankingRequirement:
    enabled: bool = False
    type: str | None = None
    limit: int | None = None
    partition_by: tuple[FieldRef, ...] = ()
    order_by_metric_id: str | None = None
    tie_policy: str | None = None


@dataclass(frozen=True)
class PostAggregationFilter:
    id: str
    metric_id: str
    operator: str
    value: Any


@dataclass(frozen=True)
class RelationCondition:
    id: str
    source_entity: str
    relation: str
    target_entity: str
    mode: str
    minimum_count: int | None = None
    # 관계가 어떤 물리 증거로 구현돼야 하는지. 모집단 스코프(구매/로그인/캠페인 반응)는
    # 소스 자체(inherent), EXISTS 서브쿼리(exists), 회원 행의 컬럼 술어(column) 중 하나로 실현된다.
    target_table: str | None = None
    binding: str | None = None
    predicate_columns: tuple[str, ...] = ()


@dataclass
class AggregationRequest:
    target_entity: str
    output_columns: list[FieldRef] = field(default_factory=list)
    filters: list[FilterRequirement] = field(default_factory=list)
    groupings: list[FieldRef] = field(default_factory=list)
    aggregations: list[AggregationRequirement] = field(default_factory=list)
    derived_metrics: list[DerivedMetricRequirement] = field(default_factory=list)
    sorting: list[SortRequirement] = field(default_factory=list)
    ranking: RankingRequirement = field(default_factory=RankingRequirement)
    post_aggregation_filters: list[PostAggregationFilter] = field(default_factory=list)
    relation_conditions: list[RelationCondition] = field(default_factory=list)
    date_grain: str | None = None
    comparison: dict[str, Any] | None = None
    business_rules: dict[str, Any] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    unresolved_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["targetEntity"] = data.pop("target_entity")
        for old, new in (
            ("output_columns", "outputColumns"),
            ("derived_metrics", "derivedMetrics"),
            ("post_aggregation_filters", "postAggregationFilters"),
            ("relation_conditions", "relationConditions"),
            ("date_grain", "dateGrain"),
            ("business_rules", "businessRules"),
            ("unresolved_fields", "unresolvedFields"),
        ):
            data[new] = data.pop(old)
        return _camelize_nested(data)


@dataclass
class SchemaMetadata:
    tables: dict[str, dict[str, Any]]

    def __post_init__(self) -> None:
        # 별칭 색인은 IR 해석 전용이다. table()/column() 의 엄격한 정확 일치는 그대로 둔다.
        self._table_aliases = _alias_index(
            (alias, key)
            for key, meta in self.tables.items()
            for alias in (key, meta.get("physical_name"), meta.get("logical_name"))
        )
        self._column_aliases = {
            key: _alias_index(
                (alias, str(column.get("name", "")))
                for column in meta.get("columns", []) or []
                if isinstance(column, dict) and column.get("name")
                for alias in (column.get("name"), column.get("logical_name"))
            )
            for key, meta in self.tables.items()
        }

    @classmethod
    def load(cls, schema_path: Path) -> "SchemaMetadata":
        """Load and merge the aggregation schema with broader catalog sources.

        The planner may receive a compact aggregation-only catalog.  A missing
        column there is not proof that the physical database lacks it, so the
        full project catalog (plus optional environment-configured snapshots)
        is merged before existence checks.  Tables and columns are de-duplicated
        case-insensitively and the explicitly supplied schema wins on metadata
        conflicts.
        """
        project_catalog = Path("docs/data/generated/schema_catalog.json")
        configured = [
            Path(value)
            for value in os.getenv("AGGREGATION_SCHEMA_FALLBACK_PATHS", "").split(os.pathsep)
            if value.strip()
        ]
        paths = [project_catalog, *configured, Path(schema_path)]
        tables: dict[str, dict[str, Any]] = {}
        seen_paths: set[str] = set()
        for path in paths:
            try:
                resolved = str(path.resolve())
            except OSError:
                resolved = str(path)
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for name, raw_meta in (payload.get("tables", {}) or {}).items():
                if not isinstance(raw_meta, dict):
                    continue
                key = str(name).casefold()
                previous = tables.get(key, {})
                merged = {**previous, **raw_meta}
                by_column = {
                    str(column.get("name", "")).casefold(): column
                    for column in previous.get("columns", []) or []
                    if isinstance(column, dict) and column.get("name")
                }
                for column in raw_meta.get("columns", []) or []:
                    if not isinstance(column, dict) or not column.get("name"):
                        continue
                    column_key = str(column["name"]).casefold()
                    by_column[column_key] = {**by_column.get(column_key, {}), **column}
                merged["columns"] = list(by_column.values())
                tables[key] = merged
        return cls(tables)

    def table(self, name: str | None) -> dict[str, Any] | None:
        return self.tables.get((name or "").casefold())

    def resolve_table_key(self, name: str | None) -> str | None:
        """물리명/논리명/표기 변형을 카탈로그의 정규 테이블 키로 되돌린다."""
        return _alias_lookup(self._table_aliases, name)

    def resolve_column_name(self, table: str | None, name: str | None) -> str | None:
        """해당 테이블 안에서 논리명/표기 변형을 실제 컬럼명으로 되돌린다."""
        key = self.resolve_table_key(table)
        return _alias_lookup(self._column_aliases.get(key or "", {}), name)

    def column(self, table: str | None, column: str | None) -> dict[str, Any] | None:
        meta = self.table(table)
        if not meta or not column:
            return None
        wanted = column.casefold()
        return next((c for c in meta.get("columns", []) if str(c.get("name", "")).casefold() == wanted), None)

    def is_unique(self, table: str, columns: Iterable[str]) -> bool:
        wanted = {c.casefold() for c in columns}
        meta = self.table(table) or {}
        pk = {str(c).casefold() for c in meta.get("primary_key", [])}
        if pk and pk <= wanted:
            return True
        for index in meta.get("indexes", []) or []:
            cols = {str(c).casefold() for c in index.get("columns", [])}
            if index.get("unique") and cols and cols <= wanted:
                return True
        return False

    def prompt_context(self, table_names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        wanted = {t.casefold() for t in table_names or []}
        result: list[dict[str, Any]] = []
        for key, meta in self.tables.items():
            if wanted and key not in wanted:
                continue
            result.append({
                "table": next((n for n in [meta.get("physical_name")] if n), key),
                "logicalName": meta.get("logical_name"),
                "description": meta.get("description_llm"),
                "rowGrain": meta.get("row_grain"),
                "primaryKey": meta.get("primary_key", []),
                "foreignKeys": meta.get("foreign_keys", []),
                "joinHints": meta.get("join_hints", []),
                "expectedCardinality": meta.get("expected_cardinality"),
                "duplicationRisk": meta.get("duplication_risk"),
                "historyTable": meta.get("history_table"),
                "includesDeleted": meta.get("includes_deleted"),
                "columns": [
                    {
                        "column": c.get("name"), "logicalName": c.get("logical_name"),
                        "type": c.get("type"), "nullable": c.get("nullable"),
                        "important": c.get("important"),
                        "meaning": c.get("human_note"), "unit": c.get("unit"),
                        "codeColumn": c.get("code_column"), "examples": c.get("value_examples", []),
                        "aggregatable": c.get("aggregatable"),
                        "recommendedAggregations": c.get("recommended_aggregations", []),
                        "semanticRoles": c.get("semantic_roles", []),
                        "unique": bool(c.get("primary_key") or c.get("unique")),
                    }
                    for c in meta.get("columns", []) if isinstance(c, dict)
                ],
            })
        return result


def aggregation_request_json_schema() -> dict[str, Any]:
    field_ref_properties = {
        "entity": {"type": "string", "description": "논리 엔터티명(예: order, product, customer)"},
        "field": {"type": "string", "description": "논리 필드명(예: salesAmount, productId)"},
        "table": {"type": "string", "description": "Aggregation Schema Metadata의 실제 테이블명. 필수."},
        "column": {"type": "string", "description": "해당 테이블의 실제 컬럼명. 필수."},
        "alias": {"type": "string"},
    }
    field_ref = {
        "type": "object",
        "description": "논리 필드 참조. entity/field 와 함께 물리 table/column 을 반드시 채운다.",
        "properties": field_ref_properties,
        "required": ["entity", "field", "table", "column"],
    }
    filter_ref = {
        "type": "object",
        "description": "집계 전 WHERE 조건.",
        "properties": {
            **field_ref_properties,
            "id": {"type": "string"},
            "operator": {"type": "string", "enum": sorted(FILTER_OPERATORS)},
            "value": {"description": "비교값. in/not_in 은 배열, is_null/is_not_null 은 생략."},
        },
        "required": ["id", "entity", "field", "table", "column", "operator"],
    }
    aggregation = {
        "type": "object",
        "description": "집계 지표 1개. COUNT(*) 행 수 집계는 field를 \"*\"로 두고 table/column 을 생략하며, "
                       "그 외 함수는 물리 table/column 이 필수다.",
        "properties": {
            **field_ref_properties,
            "id": {"type": "string", "description": "지표 id. sorting/ranking/postAggregationFilters 가 이 id를 참조한다."},
            "function": {"type": "string", "enum": sorted(AGGREGATION_FUNCTIONS)},
            "distinct": {"type": "boolean"},
            "condition": {"type": "object", "description": "조건부 집계 필터. filters 항목과 같은 구조."},
        },
        "required": ["id", "function"],
    }
    derived_metric = {
        "type": "object",
        "description": "집계 지표를 조합한 파생 지표.",
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string", "enum": sorted(DERIVED_METRIC_TYPES)},
            "alias": {"type": "string"},
            "numeratorMetricId": {"type": "string"}, "denominatorMetricId": {"type": "string"},
            "currentMetricId": {"type": "string"}, "previousMetricId": {"type": "string"},
            "multiplyBy": {"type": "number", "description": "백분율이면 100"},
            "zeroDivisionPolicy": {"type": "string", "enum": sorted(ZERO_DIVISION_POLICIES)},
        },
        "required": ["id", "type", "alias"],
    }
    sorting = {
        "type": "object",
        "description": "정렬 기준. 컬럼이 아니라 aggregations/derivedMetrics 의 id 를 참조한다.",
        "properties": {
            "metricId": {"type": "string", "description": "정렬 기준 지표 id"},
            "direction": {"type": "string", "enum": sorted(SORT_DIRECTIONS)},
            "nulls": {"type": "string"},
        },
        "required": ["metricId", "direction"],
    }
    ranking = {
        "type": "object",
        "description": "상위/하위 N 요청.",
        "properties": {
            "enabled": {"type": "boolean"},
            "type": {"type": "string", "enum": sorted(RANKING_TYPES)},
            "limit": {"type": "integer", "minimum": 1},
            "partitionBy": {"type": "array", "items": field_ref, "description": "그룹별 순위 기준"},
            "orderByMetricId": {"type": "string", "description": "순위 기준 지표 id"},
            "tiePolicy": {"type": "string"},
        },
        "required": ["enabled", "partitionBy"],
    }
    post_filter = {
        "type": "object",
        "description": "집계 후 HAVING 조건.",
        "properties": {
            "id": {"type": "string"},
            "metricId": {"type": "string", "description": "대상 지표 id"},
            "operator": {"type": "string", "enum": sorted(FILTER_OPERATORS)},
            "value": {},
        },
        "required": ["id", "metricId", "operator"],
    }
    relation = {
        "type": "object",
        "description": "집계 결과와 최종 대상의 관계 조건.",
        "properties": {
            "id": {"type": "string"},
            "sourceEntity": {"type": "string"}, "relation": {"type": "string"}, "targetEntity": {"type": "string"},
            "mode": {"type": "string", "enum": sorted(RELATION_MODES)},
            "minimumCount": {"type": "integer"},
        },
        "required": ["id", "sourceEntity", "relation", "targetEntity", "mode"],
    }
    return {
        "type": "object",
        "description": "SQL 생성 전 확정하는 일반 집계 요구사항 IR. 물리 매핑 불가 항목은 unresolvedFields에 기록한다.",
        "properties": {
            "targetEntity": {"type": "string"},
            "outputColumns": {"type": "array", "items": field_ref},
            "filters": {"type": "array", "items": filter_ref},
            "groupings": {"type": "array", "items": field_ref},
            "aggregations": {"type": "array", "items": aggregation},
            "derivedMetrics": {"type": "array", "items": derived_metric},
            "sorting": {"type": "array", "items": sorting},
            "ranking": ranking,
            "postAggregationFilters": {"type": "array", "items": post_filter},
            "relationConditions": {"type": "array", "items": relation},
            "dateGrain": {"type": ["string", "null"], "enum": [*sorted(DATE_GRAINS), None]},
            "comparison": {"type": ["object", "null"]},
            "businessRules": {"type": "object"},
            "assumptions": {"type": "array", "items": {"type": "string"}},
            "unresolvedFields": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["targetEntity", "outputColumns", "filters", "groupings", "aggregations", "derivedMetrics",
                     "sorting", "ranking", "postAggregationFilters", "relationConditions", "businessRules",
                     "assumptions", "unresolvedFields"],
    }


def parse_aggregation_request(
    payload: Any,
    schema_path: Path,
    *,
    dialect: str = "tsql",
) -> tuple[AggregationRequest | None, list[ValidationError]]:
    if not isinstance(payload, dict):
        return None, [ValidationError("INVALID_AGGREGATION_REQUEST", "집계 요구사항이 JSON 객체가 아닙니다.")]
    errors: list[ValidationError] = []
    target = _required_string(payload, "targetEntity", errors)
    output = [_field_ref(v, f"outputColumns[{i}]", errors) for i, v in enumerate(_list(payload, "outputColumns", errors))]
    filters = [_filter(v, f"filters[{i}]", errors) for i, v in enumerate(_list(payload, "filters", errors))]
    groupings = [_field_ref(v, f"groupings[{i}]", errors) for i, v in enumerate(_list(payload, "groupings", errors))]
    aggregations = [_aggregation(v, f"aggregations[{i}]", errors) for i, v in enumerate(_list(payload, "aggregations", errors))]
    derived = [_derived(v, f"derivedMetrics[{i}]", errors) for i, v in enumerate(_list(payload, "derivedMetrics", errors))]
    sorting = [_sorting(v, f"sorting[{i}]", errors) for i, v in enumerate(_list(payload, "sorting", errors))]
    post = [_post_filter(v, f"postAggregationFilters[{i}]", errors) for i, v in enumerate(_list(payload, "postAggregationFilters", errors))]
    relations = [_relation(v, f"relationConditions[{i}]", errors) for i, v in enumerate(_list(payload, "relationConditions", errors))]
    ranking = _ranking(payload.get("ranking"), errors)
    grain = payload.get("dateGrain")
    if grain is not None and grain not in DATE_GRAINS:
        errors.append(ValidationError("INVALID_DATE_GRAIN", f"지원하지 않는 기간 단위입니다: {grain}", path="dateGrain"))
    request = AggregationRequest(
        target_entity=target or "",
        output_columns=[x for x in output if x], filters=[x for x in filters if x],
        groupings=[x for x in groupings if x], aggregations=[x for x in aggregations if x],
        derived_metrics=[x for x in derived if x], sorting=[x for x in sorting if x],
        ranking=ranking, post_aggregation_filters=[x for x in post if x],
        relation_conditions=[x for x in relations if x], date_grain=grain,
        comparison=payload.get("comparison") if isinstance(payload.get("comparison"), dict) else None,
        business_rules=payload.get("businessRules") if isinstance(payload.get("businessRules"), dict) else {},
        assumptions=[str(x) for x in payload.get("assumptions", []) if isinstance(x, str) and x.strip()],
        unresolved_fields=[str(x) for x in payload.get("unresolvedFields", []) if isinstance(x, str) and x.strip()],
    )
    catalog = SchemaMetadata.load(schema_path)
    _resolve_field_refs(request, catalog)
    _normalize_aggregation_request(request, catalog)
    _validate_request(request, catalog, dialect, errors)
    return request, errors


def validate_aggregation_sql(
    request: AggregationRequest,
    sql: str,
    schema_path: Path,
    *,
    dialect: str | None = None,
) -> dict[str, Any]:
    errors: list[ValidationError] = []
    mappings: list[dict[str, Any]] = []
    catalog = SchemaMetadata.load(schema_path)
    if request.unresolved_fields:
        errors.append(ValidationError(
            "UNRESOLVED_BUSINESS_RULE", "확인되지 않은 집계 필드 또는 업무 규칙이 있습니다: " + ", ".join(request.unresolved_fields)
        ))
    try:
        statements = sqlglot.parse(sql, read=_dialect(dialect))
    except Exception as exc:  # noqa: BLE001 - parser error is part of validation output
        statements = []
        errors.append(ValidationError("SQL_PARSE_ERROR", f"SQL 파싱에 실패했습니다: {type(exc).__name__}"))
    if len(statements) != 1:
        errors.append(ValidationError("MULTIPLE_SQL_STATEMENTS", "SQL은 단일 문장이어야 합니다."))
    root = statements[0] if len(statements) == 1 else None
    if root is None or not isinstance(root, (exp.Select, exp.Union)):
        errors.append(ValidationError("UNSAFE_SQL", "SELECT 또는 WITH SELECT 문만 허용됩니다."))
        return _validation_result(errors, mappings, [], [], 0.0)
    if any(isinstance(n, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Alter, exp.Create)) for n in root.walk()):
        errors.append(ValidationError("UNSAFE_SQL", "변경 SQL은 실행할 수 없습니다."))

    aliases, ctes = _aliases(root)
    derived = _derived_sources(root)
    used_tables = sorted({t.name for t in root.find_all(exp.Table) if t.name.casefold() not in ctes})
    for table in used_tables:
        if catalog.table(table) is None:
            errors.append(ValidationError("UNKNOWN_TABLE", f"스키마에 존재하지 않는 테이블입니다: {table}"))
    used_columns: list[dict[str, str]] = []
    for column in root.find_all(exp.Column):
        if column.name == "*" or _column_is_output_alias(column, root):
            continue
        # 파생 테이블(서브쿼리) 출력 컬럼은 카탈로그 컬럼이 아니라 안쪽 SELECT 의 결과다.
        # 물리 존재 검사는 안쪽 컬럼 참조에서 이미 수행된다.
        if column.table and column.name.casefold() in derived.get(column.table.casefold(), set()):
            continue
        table = aliases.get(column.table.casefold()) if column.table else _resolve_unqualified_table(column.name, used_tables, catalog)
        if column.table and table is not None and table.casefold() in ctes:
            continue
        if table is None or catalog.column(table, column.name) is None:
            errors.append(ValidationError("UNKNOWN_COLUMN", f"스키마에서 확인되지 않는 컬럼입니다: {column.sql()}"))
        else:
            item = {"table": table, "column": column.name}
            if item not in used_columns:
                used_columns.append(item)

    aggregate_nodes = [n for n in root.walk() if isinstance(n, exp.AggFunc)]
    for req in request.aggregations:
        matched = next((n for n in aggregate_nodes if _aggregate_matches(req, n, aliases, catalog, used_tables)), None)
        if matched is None:
            code = "MISSING_DISTINCT" if req.distinct else "MISSING_AGGREGATION"
            errors.append(ValidationError(code, f"요청된 집계가 SQL에 없습니다: {req.function}({req.physical_name or req.field})", req.id))
            mappings.append(_mapping(req.id, f"{req.function} {req.field}", False))
        else:
            mappings.append(_mapping(req.id, f"{req.function} {req.field}", True, "SELECT/HAVING", matched.sql()))
            if req.condition and not _conditional_aggregate_has_filter(matched, req.condition):
                errors.append(ValidationError("MISSING_CONDITIONAL_AGGREGATION", "조건부 집계 조건이 반영되지 않았습니다.", req.id))

    group_columns = _group_columns(root, aliases, catalog, used_tables)
    for index, req in enumerate(request.groupings, 1):
        rid = f"grouping-{index}"
        if not _field_matches_set(req, group_columns):
            errors.append(ValidationError("MISSING_GROUP_BY_DIMENSION", f"요청된 그룹 기준이 GROUP BY에 없습니다: {req.field}", rid))
            mappings.append(_mapping(rid, req.field, False))
        else:
            mappings.append(_mapping(rid, req.field, True, "GROUP BY", req.physical_name or req.field))

    _validate_select_grouping(root, errors)
    _validate_filters(request, root, aliases, catalog, used_tables, errors, mappings)
    _validate_ranking(request, root, aliases, catalog, used_tables, errors, mappings)
    _validate_derived_metrics(request, root, errors, mappings)
    _validate_relations(request, root, errors, mappings)
    _validate_join_duplication(root, request, aliases, catalog, errors)

    confidence = max(0.0, 1.0 - (len(errors) * 0.12))
    return _validation_result(errors, mappings, used_tables, used_columns, confidence)


def aggregation_retry_count() -> int:
    try:
        return max(0, min(5, int(os.getenv("AGGREGATION_SQL_MAX_RETRIES", "2"))))
    except ValueError:
        return 2


def _resolve_field_refs(request: AggregationRequest, catalog: SchemaMetadata) -> None:
    """entity/field 논리명을 카탈로그의 물리 table/column 으로 보강한다.

    LLM 이 물리명을 알고도 table/column 키에 옮겨 적지 않으면 요구사항 확정 단계가 통째로 막힌다.
    카탈로그로 확인 가능한 매핑은 여기서 채우고, 확인 불가한 것만 검증 오류로 남긴다.  1차는 각 참조
    자체로, 2차는 1차에서 확보한 테이블 힌트로 컬럼만 있는 참조까지 해석한다."""
    hints: list[str] = [t for t in (r.table for r in _request_refs(request)) if t]

    def resolve(ref: FieldRef) -> FieldRef:
        out = _resolve_reference(ref, catalog, hints)
        if out.table and out.table not in hints:
            hints.append(out.table)
        return out

    def resolve_aggregation(agg: AggregationRequirement) -> AggregationRequirement:
        if agg.counts_rows:
            return agg if agg.condition is None else replace(agg, condition=resolve(agg.condition))
        out = resolve(agg)
        return out if out.condition is None else replace(out, condition=resolve(out.condition))

    for _ in range(2):
        request.output_columns = [resolve(r) for r in request.output_columns]
        request.filters = [resolve(r) for r in request.filters]
        request.groupings = [resolve(r) for r in request.groupings]
        request.aggregations = [resolve_aggregation(r) for r in request.aggregations]
        request.ranking = replace(
            request.ranking, partition_by=tuple(resolve(r) for r in request.ranking.partition_by)
        )


def _normalize_aggregation_request(request: AggregationRequest, catalog: SchemaMetadata) -> None:
    """Normalize redundant LLM output without weakening physical-schema validation.

    ``aggregations`` owns aggregate result columns.  ``outputColumns`` therefore contains only
    physical grouping columns when an aggregate is present; models often repeat the aggregate
    source or a virtual ``*_count`` alias there.  Keeping that repetition makes the SQL prompt
    select an ungrouped physical column or asks the catalog to resolve a virtual result column.

    ``unresolvedFields`` is also advisory LLM text.  Remove only notes that structured IR and the
    catalog can prove resolved: a dropped virtual output, a catalog-confirmed existence check, an
    ISO-8601 relative-period filter already represented by a physical filter reference, or an
    explicitly omitted optional filter (which means no restriction).  Real business ambiguities
    remain fail-closed.
    """
    dropped_outputs: list[FieldRef] = []
    if request.aggregations:
        grouping_keys = {_field_identity(ref) for ref in request.groupings}
        kept_outputs: list[FieldRef] = []
        for ref in request.output_columns:
            if _field_identity(ref) in grouping_keys:
                kept_outputs.append(ref)
            else:
                dropped_outputs.append(ref)
        request.output_columns = kept_outputs

    request.unresolved_fields = [
        note
        for note in request.unresolved_fields
        if not _unresolved_note_is_structurally_resolved(note, request, catalog, dropped_outputs)
    ]


def _field_identity(ref: FieldRef) -> tuple[str, str]:
    if ref.table and ref.column:
        return ref.table.casefold(), ref.column.casefold()
    return ref.entity.casefold(), ref.field.casefold()


_PHYSICAL_FIELD_IN_NOTE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
_RELATIVE_PERIOD_RE = re.compile(r"^P(?=\d)(?:\d+[YMWD])+$", re.IGNORECASE)
_SCHEMA_EXISTENCE_NOTE_RE = re.compile(
    r"(?:exist(?:s|ence)?|present|available|schema|column|field|존재|스키마|컬럼|필드)",
    re.IGNORECASE,
)
_OPTIONAL_FILTER_ABSENCE_NOTE_RE = re.compile(
    r"(?=.*(?:\bfilter\b|\blimit\b|\brestrict\w*\b|필터|제한|범위))"
    r"(?=.*(?:\bnot\s+(?:specified|provided|requested)\b|\bunspecified\b|명시되지|지정되지|제공되지|요청되지))",
    re.IGNORECASE,
)
_MISSING_OUTPUT_NOTE_RE = re.compile(
    r"(?:outputcolumns?|output_columns?).*(?:missing|unspecified|not\s+(?:specified|provided)|미지정|누락|없음)",
    re.IGNORECASE,
)
_RELATIVE_FILTER_NOTE_RE = re.compile(
    r"(?=.*(?:filters?(?:\[\d+\])?\.value|relative|상대|from\s*/\s*to|system\s+date|past\s+\d+\s+days|requires\s+concrete|today|current\s+date|현재\s*날짜|실행\s*시점))"
    r"(?=.*(?:date|from|to|today|날짜|기간|변환|실행))",
    re.IGNORECASE,
)
_RELATIVE_WINDOW_PLACEHOLDER_RE = re.compile(
    r"\bwindow_(?:start|end)_\d+(?:d|w|m|y)\b",
    re.IGNORECASE,
)


def _unresolved_note_is_structurally_resolved(
    note: str,
    request: AggregationRequest,
    catalog: SchemaMetadata,
    dropped_outputs: list[FieldRef],
) -> bool:
    folded = note.casefold()

    # An omitted filter is not missing information: it means the result is unrestricted on that
    # dimension.  Keep true business-definition gaps (metric choice, cancellation/return policy,
    # join semantics, etc.) fail-closed because they do not have filter/limit absence wording.
    if _OPTIONAL_FILTER_ABSENCE_NOTE_RE.search(note):
        return True

    # Aggregate result aliases live in aggregations, so an empty physical outputColumns list is
    # complete for a scalar aggregate.  It is not a missing member/customer output column.
    if request.aggregations and not request.output_columns and _MISSING_OUTPUT_NOTE_RE.search(note):
        return True

    # Aggregate aliases are virtual outputs, not physical catalog fields.
    for ref in dropped_outputs:
        logical_name = f"{ref.entity}.{ref.field}".casefold()
        physical_name = ref.physical_name
        if logical_name in folded or (physical_name and physical_name in folded):
            return True

    # Relative periods are executable filter values, not missing schema.  SQL generation renders
    # them with the selected DB dialect and the physical column's date representation.
    for ref in request.filters:
        value = str(ref.value).strip() if ref.value is not None else ""
        if (
            _RELATIVE_PERIOD_RE.fullmatch(value)
            and value.casefold() in folded
            and ref.physical_name
            and ref.physical_name in folded
        ):
            return True
        if (
            _RELATIVE_PERIOD_RE.fullmatch(value)
            and ref.physical_name
            and (_RELATIVE_FILTER_NOTE_RE.search(note) or _RELATIVE_WINDOW_PLACEHOLDER_RE.search(note))
        ):
            return True

    # A model may hedge about a field that is actually present in the supplied catalog.  Only
    # existence/schema notes are discharged here; relationship or business-definition notes stay.
    mentioned = _PHYSICAL_FIELD_IN_NOTE_RE.findall(note)
    if mentioned and _SCHEMA_EXISTENCE_NOTE_RE.search(note):
        return all(
            (table_key := catalog.resolve_table_key(table)) is not None
            and catalog.resolve_column_name(table_key, column) is not None
            for table, column in mentioned
        )
    return False


def _resolve_reference(ref: FieldRef, catalog: SchemaMetadata, hint_tables: Iterable[str] = ()) -> FieldRef:
    if ref.field == ROW_COUNT_FIELD:
        return ref
    table = catalog.resolve_table_key(ref.table) or catalog.resolve_table_key(ref.entity)
    name = None
    if table:
        name = catalog.resolve_column_name(table, ref.column) or catalog.resolve_column_name(table, ref.field)
    if name is None:
        # 테이블을 못 정했거나 그 테이블에 없는 컬럼이면, 같은 요청 안에서 이미 확정된 테이블로만 후보를 찾는다.
        wanted = ref.column or ref.field
        candidates: dict[str, str] = {}
        for hint in hint_tables:
            key = catalog.resolve_table_key(hint)
            found = catalog.resolve_column_name(key, wanted) if key else None
            if key and found:
                candidates[key] = found
        if len(candidates) != 1:
            return ref
        table, name = next(iter(candidates.items()))
    return replace(ref, table=table, column=name)


def _request_refs(request: AggregationRequest) -> list[FieldRef]:
    return [
        *request.output_columns, *request.filters, *request.groupings,
        *request.aggregations, *request.ranking.partition_by,
    ]


def _validate_request(request: AggregationRequest, catalog: SchemaMetadata, dialect: str, errors: list[ValidationError]) -> None:
    refs: list[tuple[str, FieldRef]] = []
    refs += [(f"outputColumns[{i}]", r) for i, r in enumerate(request.output_columns)]
    refs += [(f"filters[{i}]", r) for i, r in enumerate(request.filters)]
    refs += [(f"groupings[{i}]", r) for i, r in enumerate(request.groupings)]
    refs += [(f"aggregations[{i}]", r) for i, r in enumerate(request.aggregations) if not r.counts_rows]
    refs += [(f"ranking.partitionBy[{i}]", r) for i, r in enumerate(request.ranking.partition_by)]
    for path, ref in refs:
        if not ref.table or not ref.column:
            errors.append(ValidationError("UNRESOLVED_FIELD", f"물리 테이블/컬럼 매핑이 필요합니다: {ref.entity}.{ref.field}", path=path))
        elif catalog.table(ref.table) is None:
            errors.append(ValidationError("UNKNOWN_TABLE", f"스키마에 존재하지 않는 테이블입니다: {ref.table}", path=path))
        elif catalog.column(ref.table, ref.column) is None:
            errors.append(ValidationError("UNKNOWN_COLUMN", f"스키마에 존재하지 않는 컬럼입니다: {ref.table}.{ref.column}", path=path))
    metric_ids = {a.id for a in request.aggregations} | {d.id for d in request.derived_metrics}
    for agg in request.aggregations:
        col = catalog.column(agg.table, agg.column)
        if agg.function not in AGGREGATION_FUNCTIONS:
            errors.append(ValidationError("UNSUPPORTED_AGGREGATION", f"지원하지 않는 집계 함수입니다: {agg.function}", agg.id))
        if agg.function in {"sum", "avg", "median", "percentile", "standard_deviation", "variance"} and col:
            base_type = str(col.get("type", "")).split("(")[0].split()[0].casefold()
            if base_type not in NUMERIC_TYPES:
                errors.append(ValidationError("INVALID_AGGREGATION_FIELD", f"숫자 집계에 숫자 컬럼이 필요합니다: {agg.table}.{agg.column}", agg.id))
        if not _dialect_supports(agg.function, dialect):
            errors.append(ValidationError("UNSUPPORTED_AGGREGATION", f"{dialect}에서 안전한 구현이 확인되지 않은 함수입니다: {agg.function}", agg.id))
    for metric in request.derived_metrics:
        refs_for_metric = [metric.numerator_metric_id, metric.denominator_metric_id, metric.current_metric_id, metric.previous_metric_id]
        missing = [m for m in refs_for_metric if m and m not in metric_ids]
        if missing:
            errors.append(ValidationError("UNKNOWN_METRIC_REFERENCE", "파생 지표가 정의되지 않은 지표를 참조합니다: " + ", ".join(missing), metric.id))
        if metric.type in {"division", "growth_rate", "share", "conversion_rate"} and metric.zero_division_policy not in {"null", "zero"}:
            errors.append(ValidationError("ZERO_DIVISION_POLICY_REQUIRED", "나눗셈 파생 지표에는 0 나눗셈 정책이 필요합니다.", metric.id))
    if request.ranking.enabled:
        if request.ranking.type not in RANKING_TYPES or not request.ranking.limit or request.ranking.limit < 1:
            errors.append(ValidationError("INVALID_RANKING", "순위 유형과 1 이상의 제한 개수가 필요합니다.", "ranking-1"))
        if request.ranking.order_by_metric_id not in metric_ids:
            errors.append(ValidationError("UNKNOWN_RANKING_METRIC", "순위 기준 집계 지표가 정의되지 않았습니다.", "ranking-1"))
    for pf in request.post_aggregation_filters:
        if pf.metric_id not in metric_ids:
            errors.append(ValidationError("UNKNOWN_METRIC_REFERENCE", "HAVING 대상 지표가 정의되지 않았습니다.", pf.id))


def _dialect_supports(function: str, dialect: str) -> bool:
    d = (dialect or "").casefold()
    if function == "median":
        return d in {"postgres", "postgresql", "mysql", "mariadb"}
    if function == "percentile":
        return d in {"postgres", "postgresql", "tsql", "mssql", "sqlserver"}
    if function == "standard_deviation":
        return d not in {"tsql", "mssql", "sqlserver"}  # 프로젝트 T-SQL 방언 정책에서 함수명 미확정
    return True


def _aliases(root: exp.Expression) -> tuple[dict[str, str], set[str]]:
    ctes = {c.alias_or_name.casefold() for c in root.find_all(exp.CTE) if c.alias_or_name}
    aliases: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        if table.name.casefold() in ctes:
            aliases[table.name.casefold()] = table.name
            if table.alias:
                aliases[table.alias.casefold()] = table.name
            continue
        aliases[table.name.casefold()] = table.name
        if table.alias:
            aliases[table.alias.casefold()] = table.name
    return aliases, ctes


def _derived_sources(root: exp.Expression) -> dict[str, set[str]]:
    """파생 테이블(FROM (SELECT ...) X) 별칭이 노출하는 출력 컬럼 이름.

    2단계 집계(회원별로 묶은 뒤 평균 등)는 바깥 SELECT 가 안쪽 결과 별칭을 참조한다. 이 별칭을
    물리 컬럼으로 착각하면 정상 SQL 이 '스키마에 없는 컬럼'으로 탈락한다.
    """
    exposed: dict[str, set[str]] = {}
    for subquery in root.find_all(exp.Subquery):
        alias = subquery.alias_or_name
        inner = subquery.this
        if not alias or not isinstance(inner, exp.Select):
            continue
        exposed.setdefault(alias.casefold(), set()).update(
            projection.alias_or_name.casefold() for projection in inner.expressions if projection.alias_or_name
        )
    return exposed


def _aggregate_matches(req: AggregationRequirement, node: exp.AggFunc, aliases: dict[str, str], catalog: SchemaMetadata, used_tables: list[str]) -> bool:
    name = node.key.casefold()
    distinct = isinstance(node.this, exp.Distinct)
    expected = req.function
    if expected == "count_distinct":
        if name != "count" or not distinct:
            return False
    elif name != {"standard_deviation": "stddev"}.get(expected, expected):
        if expected == "percentile" and name.startswith("percentile"):
            pass
        elif expected == "variance" and name in {"variance", "var_pop", "var_samp"}:
            pass
        else:
            return False
    if req.distinct and not distinct:
        return False
    arg = node.this.expressions[0] if distinct and node.this.expressions else node.this
    if req.column is None:
        return isinstance(arg, exp.Star) or expected == "count"
    columns = list(arg.find_all(exp.Column)) if isinstance(arg, exp.Expression) else []
    if isinstance(arg, exp.Column):
        columns.insert(0, arg)
    for col in columns:
        table = aliases.get(col.table.casefold()) if col.table else _resolve_unqualified_table(col.name, used_tables, catalog)
        if table and table.casefold() == (req.table or "").casefold() and col.name.casefold() == req.column.casefold():
            return True
    return False


def _group_columns(root: exp.Expression, aliases: dict[str, str], catalog: SchemaMetadata, used_tables: list[str]) -> set[str]:
    result: set[str] = set()
    for group in root.find_all(exp.Group):
        for item in group.expressions:
            for col in item.find_all(exp.Column):
                table = aliases.get(col.table.casefold()) if col.table else _resolve_unqualified_table(col.name, used_tables, catalog)
                result.add(f"{table}.{col.name}".casefold() if table else col.name.casefold())
            if isinstance(item, exp.Column):
                table = aliases.get(item.table.casefold()) if item.table else _resolve_unqualified_table(item.name, used_tables, catalog)
                result.add(f"{table}.{item.name}".casefold() if table else item.name.casefold())
    return result


def _field_matches_set(ref: FieldRef, values: set[str]) -> bool:
    return bool(ref.physical_name and (ref.physical_name in values or (ref.column or "").casefold() in values))


def _validate_select_grouping(root: exp.Expression, errors: list[ValidationError]) -> None:
    for select in root.find_all(exp.Select):
        if not any(
            isinstance(n, exp.AggFunc)
            and n.find_ancestor(exp.Select) is select
            and n.find_ancestor(exp.Window) is None
            for n in select.walk()
        ):
            continue
        group_sql = {e.sql().replace(" ", "").casefold() for g in [select.args.get("group")] if g for e in g.expressions}
        for projection in select.expressions:
            target = projection.this if isinstance(projection, exp.Alias) else projection
            if isinstance(target, exp.Star) or target.find(exp.Window) or target.find(exp.AggFunc):
                continue
            if target.sql().replace(" ", "").casefold() not in group_sql:
                errors.append(ValidationError("INVALID_GROUP_BY", f"비집계 SELECT 컬럼이 GROUP BY에 없습니다: {target.sql()}"))


def _validate_filters(request: AggregationRequest, root: exp.Expression, aliases: dict[str, str], catalog: SchemaMetadata,
                      used_tables: list[str], errors: list[ValidationError], mappings: list[dict[str, Any]]) -> None:
    where_columns = _clause_columns(root, exp.Where, aliases, catalog, used_tables)
    having_sql = " ".join(h.sql().casefold() for h in root.find_all(exp.Having))
    for req in request.filters:
        if req.physical_name not in where_columns and (req.column or "").casefold() not in where_columns:
            errors.append(ValidationError("INVALID_PRE_AGGREGATION_FILTER", f"집계 전 필터가 WHERE에 없습니다: {req.field}", req.id))
            mappings.append(_mapping(req.id, req.field, False))
        else:
            mappings.append(_mapping(req.id, req.field, True, "WHERE", req.physical_name or req.field))
    for req in request.post_aggregation_filters:
        if req.metric_id.casefold() not in having_sql and not any(_operator_sql(req.operator) in h.sql() for h in root.find_all(exp.Having)):
            errors.append(ValidationError("MISSING_HAVING", f"집계 후 조건이 HAVING에 없습니다: {req.metric_id}", req.id))
            mappings.append(_mapping(req.id, req.metric_id, False))
        else:
            mappings.append(_mapping(req.id, req.metric_id, True, "HAVING", having_sql))


def _validate_ranking(request: AggregationRequest, root: exp.Expression, aliases: dict[str, str], catalog: SchemaMetadata,
                      used_tables: list[str], errors: list[ValidationError], mappings: list[dict[str, Any]]) -> None:
    ranking = request.ranking
    if not ranking.enabled:
        return
    expected_desc = ranking.type != "bottom"
    orders = list(root.find_all(exp.Ordered))
    direction_ok = any(bool(order.args.get("desc")) == expected_desc for order in orders)
    windows = list(root.find_all(exp.Window))
    rank_windows = [w for w in windows if isinstance(w.this, (exp.RowNumber, exp.Rank, exp.DenseRank))]
    limits = [n for n in root.find_all(exp.Limit) if isinstance(n.expression, exp.Literal)]
    limit_values = {int(n.expression.name) for n in limits if str(n.expression.name).isdigit()}
    predicate_text = " ".join(w.sql() for w in root.find_all(exp.Where)).casefold()
    has_rank_limit = bool(ranking.limit and (ranking.limit in limit_values or str(ranking.limit) in predicate_text))
    if not orders:
        errors.append(ValidationError("MISSING_RANKING", "순위 정렬 기준이 SQL에 없습니다.", "ranking-1"))
    elif not direction_ok:
        errors.append(ValidationError("INVALID_RANKING_DIRECTION", "순위 정렬 방향이 요청과 다릅니다.", "ranking-1"))
    if not has_rank_limit:
        errors.append(ValidationError("MISSING_TOP_N_LIMIT", "요청된 상위/하위 N개 제한이 없습니다.", "ranking-1"))
    if ranking.partition_by:
        partition_columns = {
            f"{aliases.get(c.table.casefold(), c.table)}.{c.name}".casefold() if c.table else c.name.casefold()
            for w in rank_windows for c in (w.args.get("partition_by") or []) if isinstance(c, exp.Column)
        }
        for ref in ranking.partition_by:
            if not _field_matches_set(ref, partition_columns):
                errors.append(ValidationError("INVALID_RANKING_PARTITION", f"그룹별 순위 PARTITION BY가 누락됐습니다: {ref.field}", "ranking-1"))
        if not rank_windows:
            errors.append(ValidationError("MISSING_RANKING_WINDOW", "그룹별 순위에는 ROW_NUMBER/RANK 계열 윈도 함수가 필요합니다.", "ranking-1"))
    mappings.append(_mapping("ranking-1", f"{ranking.type} {ranking.limit}", not any(e.requirement_id == "ranking-1" for e in errors),
                             "ORDER BY / LIMIT / WINDOW", "; ".join([o.sql() for o in orders] + [w.sql() for w in rank_windows] + [n.sql() for n in limits])))


def _validate_derived_metrics(request: AggregationRequest, root: exp.Expression, errors: list[ValidationError], mappings: list[dict[str, Any]]) -> None:
    sql_text = root.sql().casefold()
    divisions = list(root.find_all(exp.Div))
    for req in request.derived_metrics:
        implemented = True
        if req.type in {"division", "growth_rate", "share", "conversion_rate"}:
            if not divisions:
                errors.append(ValidationError("MISSING_DERIVED_METRIC", "비율/증감률 나눗셈 식이 없습니다.", req.id))
                implemented = False
            elif req.zero_division_policy in {"null", "zero"} and "nullif" not in sql_text:
                errors.append(ValidationError("MISSING_ZERO_DIVISION_GUARD", "0 나눗셈 처리가 없습니다.", req.id))
                implemented = False
            if req.multiply_by is not None and str(int(req.multiply_by) if req.multiply_by.is_integer() else req.multiply_by) not in sql_text:
                errors.append(ValidationError("INVALID_PERCENT_MULTIPLIER", "백분율 배수가 SQL에 반영되지 않았습니다.", req.id))
                implemented = False
        if req.type == "cumulative" and not any(isinstance(w.this, exp.Sum) for w in root.find_all(exp.Window)):
            errors.append(ValidationError("MISSING_CUMULATIVE_WINDOW", "누적값 SUM() OVER 윈도 함수가 없습니다.", req.id))
            implemented = False
        mappings.append(_mapping(req.id, req.type, implemented, "SELECT / WINDOW" if implemented else None, req.alias if implemented else None))


def _validate_relations(request: AggregationRequest, root: exp.Expression, errors: list[ValidationError], mappings: list[dict[str, Any]]) -> None:
    """관계 조건이 실제로 어떤 물리 증거로 구현됐는지 확인한다.

    존재 관계를 "조인이나 EXISTS 가 하나라도 있으면 통과"로 보면, 다른 목적의 조인이 우연히
    있다는 이유로 누락된 모집단 조건이 통과한다. 관계가 대상 테이블/구현 방식을 선언하면 그
    증거를 직접 확인하고, 선언이 없는 기존 관계만 종전처럼 완화해 판정한다.
    """
    sql_text = root.sql().casefold()
    has_relation = bool(list(root.find_all(exp.Join)) or list(root.find_all(exp.Exists)) or list(root.find_all(exp.In)))
    for req in request.relation_conditions:
        clause = "JOIN / EXISTS / IN"
        if req.binding == "column" or req.predicate_columns:
            implemented = any(str(column).casefold() in sql_text for column in req.predicate_columns)
            clause = "WHERE"
        elif req.target_table:
            implemented = str(req.target_table).casefold() in sql_text
            if implemented and req.binding == "exists":
                implemented = "exists" in sql_text
            if implemented and req.mode == "none":
                implemented = "not exists" in sql_text or "not in" in sql_text
            clause = "FROM / EXISTS"
        else:
            implemented = has_relation
        if req.mode in {"at_least", "exactly"} and req.minimum_count is not None:
            implemented = implemented and any(str(req.minimum_count) in h.sql() for h in root.find_all(exp.Having))
        if not implemented:
            errors.append(ValidationError(
                "MISSING_RELATION_JOIN",
                f"집계 결과와 최종 대상의 관계 조건이 없습니다: {req.relation or req.id}",
                req.id,
            ))
        mappings.append(_mapping(req.id, req.relation, implemented, clause if implemented else None))


def _validate_join_duplication(root: exp.Expression, request: AggregationRequest, aliases: dict[str, str],
                               catalog: SchemaMetadata, errors: list[ValidationError]) -> None:
    aggregate_sources = {(a.table or "").casefold() for a in request.aggregations if a.table}
    cte_names = {cte.alias_or_name.casefold() for cte in root.find_all(exp.CTE) if cte.alias_or_name}
    if not aggregate_sources:
        return
    for select in root.find_all(exp.Select):
        if not any(
            isinstance(node, exp.AggFunc) and node.find_ancestor(exp.Select) is select
            for node in select.walk()
        ):
            continue
        for join in select.args.get("joins", []) or []:
            table_node = join.this
            if not isinstance(table_node, exp.Table):
                continue
            joined_table = table_node.name
            if joined_table.casefold() in cte_names:
                continue  # CTE 내부 grain은 해당 SELECT의 GROUP BY/윈도 검증에서 별도로 확인한다.
            on = join.args.get("on")
            if on is None:
                errors.append(ValidationError("AGGREGATION_DUPLICATION_RISK", f"집계 SELECT에 조인 조건이 없습니다: {joined_table}"))
                continue
            join_columns: list[str] = []
            for eq in on.find_all(exp.EQ):
                for side in (eq.this, eq.expression):
                    if isinstance(side, exp.Column) and aliases.get(side.table.casefold(), "").casefold() == joined_table.casefold():
                        join_columns.append(side.name)
            # 집계 원본과 다른 비고유 테이블을 집계 전에 붙이면 원본 행이 증가할 수 있다.
            if joined_table.casefold() not in aggregate_sources and join_columns and not catalog.is_unique(joined_table, join_columns):
                errors.append(ValidationError(
                    "AGGREGATION_DUPLICATION_RISK",
                    f"비고유 조인으로 집계값이 증폭될 수 있습니다: {joined_table}({', '.join(join_columns)})",
                ))


def _conditional_aggregate_has_filter(node: exp.AggFunc, condition: FilterRequirement) -> bool:
    text = node.sql().casefold()
    return (condition.column or condition.field).casefold() in text and _operator_sql(condition.operator).casefold() in text


def _clause_columns(root: exp.Expression, kind: type[exp.Expression], aliases: dict[str, str],
                    catalog: SchemaMetadata, used_tables: list[str]) -> set[str]:
    result: set[str] = set()
    for clause in root.find_all(kind):
        for col in clause.find_all(exp.Column):
            table = aliases.get(col.table.casefold()) if col.table else _resolve_unqualified_table(col.name, used_tables, catalog)
            result.add(f"{table}.{col.name}".casefold() if table else col.name.casefold())
            result.add(col.name.casefold())
    return result


def _resolve_unqualified_table(column: str, used_tables: list[str], catalog: SchemaMetadata) -> str | None:
    matches = [table for table in used_tables if catalog.column(table, column) is not None]
    return matches[0] if len(matches) == 1 else None


def _column_is_output_alias(column: exp.Column, root: exp.Expression) -> bool:
    if column.table:
        return False
    aliases = {p.alias.casefold() for s in root.find_all(exp.Select) for p in s.expressions if p.alias}
    return column.name.casefold() in aliases


def _mapping(requirement_id: str, requirement: str, implemented: bool, clause: str | None = None,
             fragment: str | None = None) -> dict[str, Any]:
    return {
        "requirementId": requirement_id, "requirement": requirement, "implemented": implemented,
        "sqlClause": clause, "sqlFragment": fragment,
    }


def _validation_result(errors: list[ValidationError], mappings: list[dict[str, Any]], used_tables: list[str],
                       used_columns: list[dict[str, str]], confidence: float) -> dict[str, Any]:
    unique = _unique_errors(errors)
    result = {
        "valid": not errors,
        "errors": [e.to_dict() for e in unique],
        "requirementMappings": mappings,
        "usedTables": [{"table": t} for t in used_tables],
        "usedColumns": used_columns,
        "confidence": round(confidence, 4),
    }
    # Keep the detailed errors array for compatibility while exposing the
    # primary machine-readable failure directly to API clients.
    if unique:
        result["error_code"] = unique[0].code
        result["message"] = unique[0].message
    return result


def _unique_errors(errors: list[ValidationError]) -> list[ValidationError]:
    seen, result = set(), []
    for error in errors:
        key = (error.code, error.message, error.requirement_id, error.path)
        if key not in seen:
            seen.add(key)
            result.append(error)
    return result


def _field_ref(value: Any, path: str, errors: list[ValidationError]) -> FieldRef | None:
    if not isinstance(value, dict):
        errors.append(ValidationError("INVALID_FIELD_REFERENCE", "필드 참조는 객체여야 합니다.", path=path))
        return None
    table = _string(value.get("table"))
    column = _string(value.get("column"))
    # Physical table/column is stronger evidence than a repeated logical label.  Tool-calling
    # models sometimes omit entity/field while still returning a fully valid catalog reference;
    # derive the labels instead of discarding the whole aggregation/filter/grouping item.
    entity = _string(value.get("entity")) or table
    name = _string(value.get("field")) or column
    if entity is None or name is None:
        errors.append(ValidationError("INVALID_FIELD_REFERENCE", "entity와 field가 필요합니다.", path=path))
        return None
    return FieldRef(entity, name, table, column, _string(value.get("alias")))


def _filter(value: Any, path: str, errors: list[ValidationError]) -> FilterRequirement | None:
    ref = _field_ref(value, path, errors)
    if ref is None:
        return None
    op = value.get("operator")
    if op not in FILTER_OPERATORS:
        errors.append(ValidationError("INVALID_FILTER_OPERATOR", f"지원하지 않는 필터 연산자입니다: {op}", path=path))
    return FilterRequirement(**asdict(ref), id=_string(value.get("id")) or path, operator=op or "eq", value=value.get("value"))


def _aggregation(value: Any, path: str, errors: list[ValidationError]) -> AggregationRequirement | None:
    if not isinstance(value, dict):
        errors.append(ValidationError("INVALID_FIELD_REFERENCE", "필드 참조는 객체여야 합니다.", path=path))
        return None
    function = str(value.get("function", "")).casefold()
    if _is_row_count(value, function):
        # COUNT(*) 는 물리 컬럼 참조가 없다. entity 는 있으면 문맥으로 보존한다.
        ref = FieldRef(_string(value.get("entity")) or ROW_COUNT_FIELD, ROW_COUNT_FIELD)
    else:
        ref = _field_ref(value, path, errors)
        if ref is None:
            return None
    condition = _filter(value.get("condition"), path + ".condition", errors) if value.get("condition") is not None else None
    return AggregationRequirement(**asdict(ref), id=_string(value.get("id")) or path, function=function,
                                  distinct=bool(value.get("distinct")), condition=condition)


def _is_row_count(value: dict[str, Any], function: str) -> bool:
    """COUNT(*) 표기인지. 필드가 없거나 '*' 인 count 만 행 수 집계로 본다(DISTINCT 는 컬럼이 필요하다)."""
    if function != "count" or bool(value.get("distinct")):
        return False
    return all(_string(value.get(key)) in (None, ROW_COUNT_FIELD) for key in ("field", "column"))


def _derived(value: Any, path: str, errors: list[ValidationError]) -> DerivedMetricRequirement | None:
    if not isinstance(value, dict):
        errors.append(ValidationError("INVALID_DERIVED_METRIC", "파생 지표는 객체여야 합니다.", path=path)); return None
    kind = str(value.get("type", "")).casefold()
    if kind not in DERIVED_METRIC_TYPES:
        errors.append(ValidationError("INVALID_DERIVED_METRIC", f"지원하지 않는 파생 지표입니다: {kind}", path=path))
    multiply = value.get("multiplyBy")
    return DerivedMetricRequirement(
        _string(value.get("id")) or path, kind, _string(value.get("alias")) or (_string(value.get("id")) or path),
        _string(value.get("numeratorMetricId")), _string(value.get("denominatorMetricId")),
        _string(value.get("currentMetricId")), _string(value.get("previousMetricId")),
        float(multiply) if isinstance(multiply, (int, float)) else None, _string(value.get("zeroDivisionPolicy")),
    )


def _sorting(value: Any, path: str, errors: list[ValidationError]) -> SortRequirement | None:
    metric_id = _metric_id(value)
    if metric_id is None:
        errors.append(ValidationError("INVALID_SORTING", "정렬 기준 metricId가 필요합니다.", path=path)); return None
    direction = str(value.get("direction", "")).casefold()
    if direction not in SORT_DIRECTIONS:
        errors.append(ValidationError("INVALID_SORTING", "정렬 방향은 asc 또는 desc여야 합니다.", path=path))
    return SortRequirement(metric_id, direction, _string(value.get("nulls")))


def _metric_id(value: Any) -> str | None:
    """지표 참조 키. LLM 이 metricId 대신 쓰는 표기 변형까지 받아준다."""
    if not isinstance(value, dict):
        return None
    return next((v for v in (_string(value.get(k)) for k in ("metricId", "metric_id", "metric", "aggregationId")) if v), None)


def _ranking(value: Any, errors: list[ValidationError]) -> RankingRequirement:
    if not isinstance(value, dict):
        return RankingRequirement()
    parts = [_field_ref(v, f"ranking.partitionBy[{i}]", errors) for i, v in enumerate(value.get("partitionBy", []) or [])]
    limit = value.get("limit")
    return RankingRequirement(bool(value.get("enabled")), _string(value.get("type")), limit if isinstance(limit, int) else None,
                              tuple(x for x in parts if x), _string(value.get("orderByMetricId")), _string(value.get("tiePolicy")))


def _post_filter(value: Any, path: str, errors: list[ValidationError]) -> PostAggregationFilter | None:
    metric_id = _metric_id(value)
    if metric_id is None:
        errors.append(ValidationError("INVALID_POST_AGGREGATION_FILTER", "집계 후 필터에는 metricId가 필요합니다.", path=path)); return None
    return PostAggregationFilter(_string(value.get("id")) or path, metric_id, str(value.get("operator", "")), value.get("value"))


def _relation(value: Any, path: str, errors: list[ValidationError]) -> RelationCondition | None:
    if not isinstance(value, dict):
        errors.append(ValidationError("INVALID_RELATION", "관계 조건은 객체여야 합니다.", path=path)); return None
    mode = str(value.get("mode", "")).casefold()
    if mode not in RELATION_MODES:
        errors.append(ValidationError("INVALID_RELATION", f"지원하지 않는 관계 모드입니다: {mode}", path=path))
    minimum = value.get("minimumCount")
    columns = value.get("predicateColumns")
    return RelationCondition(
        _string(value.get("id")) or path, str(value.get("sourceEntity", "")), str(value.get("relation", "")),
        str(value.get("targetEntity", "")), mode, minimum if isinstance(minimum, int) else None,
        _string(value.get("targetTable")), _string(value.get("binding")),
        tuple(str(item) for item in columns if isinstance(item, str)) if isinstance(columns, list) else (),
    )


def _required_string(payload: dict[str, Any], key: str, errors: list[ValidationError]) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(ValidationError("INVALID_AGGREGATION_REQUEST", f"필수 문자열 필드가 없습니다: {key}", path=key)); return ""
    return value.strip()


def _list(payload: dict[str, Any], key: str, errors: list[ValidationError]) -> list[Any]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        errors.append(ValidationError("INVALID_AGGREGATION_REQUEST", f"배열 필드가 필요합니다: {key}", path=key)); return []
    return value


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _name_forms(value: Any, *, exact: bool) -> list[str]:
    """이름 비교 형태. exact=True 는 casefold 만, False 는 구분자/대소문자를 지운 형태다."""
    if not isinstance(value, str) or not value.strip():
        return []
    folded = value.strip().casefold()
    if exact:
        return [folded]
    squashed = re.sub(r"[^0-9a-z가-힣]+", "", folded)
    return [squashed] if squashed and squashed != folded else []


def _alias_index(pairs: Iterable[tuple[Any, str]]) -> dict[str, str]:
    """(별칭, 정규명) 쌍을 색인한다. 정확 일치 별칭이 항상 축약형 별칭을 이긴다."""
    items = [(alias, value) for alias, value in pairs if value]
    index: dict[str, str] = {}
    for exact in (True, False):
        for alias, value in items:
            for form in _name_forms(alias, exact=exact):
                index.setdefault(form, value)
    return index


def _alias_lookup(index: dict[str, str], name: str | None) -> str | None:
    for exact in (True, False):
        for form in _name_forms(name, exact=exact):
            hit = index.get(form)
            if hit:
                return hit
    return None


def _operator_sql(operator: str) -> str:
    return {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
            "in": " IN ", "not_in": " NOT IN ", "is_null": " IS NULL", "is_not_null": " IS NOT NULL"}.get(operator, operator)


def _dialect(dialect: str | None) -> str | None:
    return {"mssql": "tsql", "sqlserver": "tsql", "mariadb": "mysql", "postgresql": "postgres"}.get((dialect or "").casefold(), dialect)


def _camelize_nested(value: Any) -> Any:
    key_map = {
        "physical_name": "physicalName", "numerator_metric_id": "numeratorMetricId",
        "denominator_metric_id": "denominatorMetricId", "current_metric_id": "currentMetricId",
        "previous_metric_id": "previousMetricId", "multiply_by": "multiplyBy",
        "zero_division_policy": "zeroDivisionPolicy", "metric_id": "metricId",
        "partition_by": "partitionBy", "order_by_metric_id": "orderByMetricId",
        "tie_policy": "tiePolicy", "source_entity": "sourceEntity", "target_entity": "targetEntity",
        "minimum_count": "minimumCount", "target_table": "targetTable", "predicate_columns": "predicateColumns",
    }
    if isinstance(value, dict):
        return {key_map.get(k, k): _camelize_nested(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_camelize_nested(v) for v in value]
    if isinstance(value, tuple):
        return [_camelize_nested(v) for v in value]
    return value
