import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?P<table>[\w.]+)\s*\((?P<body>.*?)\);",
    re.IGNORECASE | re.DOTALL,
)
CREATE_INDEX_RE = re.compile(
    r"CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+(?P<name>\w+)\s+ON\s+(?P<table>[\w.]+)\s*\((?P<columns>[^)]+)\);",
    re.IGNORECASE,
)
CREATE_VIEW_RE = re.compile(
    r"CREATE\s+VIEW\s+(?P<view>[\w.]+)\s+AS\s+(?P<body>.*?);",
    re.IGNORECASE | re.DOTALL,
)
COLUMN_RE = re.compile(r"^(?P<name>\w+)\s+(?P<type>[A-Z][A-Z0-9_]*(?:\([^)]*\))?)(?P<constraints>.*)$", re.IGNORECASE)
REFERENCES_RE = re.compile(r"REFERENCES\s+(?P<table>[\w.]+)\s*\((?P<column>\w+)\)", re.IGNORECASE)
FOREIGN_KEY_RE = re.compile(
    r"FOREIGN\s+KEY\s*\((?P<columns>[^)]+)\)\s*REFERENCES\s+(?P<table>[\w.]+)\s*\((?P<ref_columns>[^)]+)\)",
    re.IGNORECASE,
)

TABLE_CONSTRAINT_PREFIXES = (
    "PRIMARY KEY",
    "FOREIGN KEY",
    "UNIQUE",
    "CHECK",
    "CONSTRAINT",
)

IMPORTANT_COLUMN_NAMES = {
    "campaign_id",
    "user_id",
    "name",
    "objective",
    "category",
    "channel",
    "target_segment",
    "keyword",
    "age",
    "gender",
    "region",
    "lifecycle",
    "interest",
    "preferred_channel",
    "behavior",
    "price_sensitivity",
    "predicted_ltv_segment",
    "campaign_id",
    "reason",
    "label",
    "message_id",
    "send_type",
    "send_status",
    "experiment_id",
    "experiment_name",
    "status",
    "primary_metric",
    "variant_id",
    "variant_code",
    "message_name",
    "message_body",
    "delivery_id",
    "assignment_source",
    "predicted_click_probability",
    "final_status",
    "event_id",
    "event_type",
    "event_at",
    "conversion_value_krw",
    "ctr_pct",
    "cvr_pct",
    "revenue_krw",
}

DEFAULT_OBJECT_DESCRIPTIONS = {
    "campaigns": "캠페인의 기본 속성, 목적, 카테고리, 예산, 기간, 예상 성과와 임베딩용 텍스트를 저장하는 중심 테이블이다.",
    "campaign_channels": "캠페인별 사용 가능한 발송/노출 채널을 관리하는 매핑 테이블이다.",
    "campaign_target_segments": "캠페인이 겨냥하는 타겟 세그먼트를 캠페인별로 관리하는 매핑 테이블이다.",
    "campaign_keywords": "캠페인 검색과 의미 매칭에 쓰는 키워드를 캠페인별로 저장한다.",
    "campaign_message_examples": "새 LMS/RCS 문안 생성 시 참고할 과거 메시지, 강조 유형, 브랜드 톤을 저장한다.",
    "users": "캠페인 타겟팅 대상 고객의 인구통계, 생애주기, 구매 성향, LTV 세그먼트를 저장한다.",
    "user_interests": "사용자별 관심 카테고리나 주제를 관리하는 매핑 테이블이다.",
    "user_preferred_channels": "사용자별 선호 채널을 관리하는 매핑 테이블이다.",
    "user_recent_behaviors": "사용자별 최근 행동 신호를 관리하는 매핑 테이블이다.",
    "recommendation_edges": "사용자와 추천 캠페인 사이의 매칭 결과, 추천 사유, 강도 라벨을 저장한다.",
    "campaign_channel_messages": "캠페인 채널별 실제 발송 메시지 이력과 외부 발송 provider id를 저장한다.",
    "campaign_experiments": "캠페인 안에서 수행되는 A/B/C 메시지 실험의 실행 단위와 상태, 주요 지표를 저장한다.",
    "campaign_message_variants": "실험에 포함되는 메시지 A/B/C 버전, 랜딩 URL, 배정 가중치, AI 생성 특성을 저장한다.",
    "campaign_message_deliveries": "사용자에게 특정 메시지 variant를 배정하고 발송한 사실을 이벤트 로그와 분리해 저장한다.",
    "campaign_message_events": "발송 요청, 도달, 노출, 클릭, 전환 등 append-only 메시지 이벤트 로그를 저장한다.",
    "v_campaign_variant_metrics": "메시지 variant별 발송, 도달, 노출, 클릭, 전환, CTR/CVR, 매출 지표를 집계하는 분석 view다.",
    "v_campaign_segment_metrics": "성별, 연령대, 지역, 라이프사이클별 메시지 성과와 CTR/CVR을 집계하는 분석 view다.",
    "v_campaign_daily_metrics": "KST 날짜별 메시지 이벤트 퍼널과 매출 추이를 집계하는 분석 view다.",
}


def merge_existing_annotations(schema: dict[str, Any], existing_schema: dict[str, Any] | None) -> dict[str, Any]:
    if not existing_schema:
        return schema

    existing_tables = existing_schema.get("tables", {})
    for table_name, table in schema["tables"].items():
        existing_table = existing_tables.get(table_name, {})
        table["description_llm"] = existing_table.get("description_llm", table["description_llm"])
        for key in (
            "logical_name", "row_grain", "expected_cardinality", "duplication_risk",
            "history_table", "includes_deleted", "business_rules", "join_hints",
        ):
            if key in existing_table:
                table[key] = existing_table[key]

        existing_columns = {
            column.get("name"): column
            for column in existing_table.get("columns", [])
            if column.get("name")
        }
        for column in table["columns"]:
            existing_column = existing_columns.get(column["name"], {})
            column["human_note"] = existing_column.get("human_note", column["human_note"])
            for key in (
                "logical_name", "unit", "code_column", "value_examples", "aggregatable",
                "recommended_aggregations", "semantic_roles", "unique",
            ):
                if key in existing_column:
                    column[key] = existing_column[key]

    return schema


# ---------------------------------------------------------------------------
# 라이브 DB 인트로스펙션 (source of truth = PostgreSQL)
#
# 손으로 유지하는 local_bootstrap.sql 미러 대신 실제 DB의 information_schema/pg_catalog를 읽어
# extract_schema()와 동일한 구조의 catalog를 만든다. description_llm/human_note 같은
# 사람이 쓴 주석은 merge_existing_annotations()로 기존 catalog에서 그대로 보존한다.
# ---------------------------------------------------------------------------

TYPE_BASE_MAP = {
    "character varying": "VARCHAR",
    "character": "CHAR",
    "timestamp without time zone": "TIMESTAMP",
    "timestamp with time zone": "TIMESTAMPTZ",
    "time without time zone": "TIME",
    "time with time zone": "TIMETZ",
    "double precision": "DOUBLE PRECISION",
    "boolean": "BOOLEAN",
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "smallint": "SMALLINT",
    "numeric": "NUMERIC",
    "text": "TEXT",
    "date": "DATE",
    "json": "JSON",
    "jsonb": "JSONB",
    "uuid": "UUID",
    "real": "REAL",
}

_INTROSPECT_COLUMNS_SQL = """
SELECT c.relname AS table_name,
       (c.relkind IN ('v', 'm')) AS is_view,
       a.attname AS column_name,
       a.attnum AS ordinal,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull AS not_null,
       pg_get_expr(ad.adbin, ad.adrelid) AS default_expr
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE n.nspname = %(schema)s
  AND c.relkind IN ('r', 'p', 'v', 'm')
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

SERIAL_TYPE_MAP = {"INTEGER": "SERIAL", "BIGINT": "BIGSERIAL", "SMALLINT": "SMALLSERIAL"}

_INTROSPECT_PK_SQL = """
SELECT c.relname AS table_name, a.attname AS column_name,
       array_position(con.conkey, a.attnum) AS pos
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY (con.conkey)
WHERE con.contype = 'p' AND n.nspname = %(schema)s
ORDER BY c.relname, pos
"""

_INTROSPECT_FK_SQL = """
SELECT con.conname, c.relname AS table_name, rc.relname AS ref_table,
       a.attname AS column_name, fa.attname AS ref_column, k.ord
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_class rc ON rc.oid = con.confrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS k(attnum, fattnum, ord) ON TRUE
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
JOIN pg_attribute fa ON fa.attrelid = con.confrelid AND fa.attnum = k.fattnum
WHERE con.contype = 'f' AND n.nspname = %(schema)s
ORDER BY c.relname, con.conname, k.ord
"""

_INTROSPECT_CHECK_SQL = """
SELECT c.relname AS table_name, pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE con.contype = 'c' AND n.nspname = %(schema)s
ORDER BY c.relname, con.conname
"""

_INTROSPECT_INDEX_SQL = """
SELECT c.relname AS table_name, ic.relname AS index_name,
       idx.indisunique AS is_unique, a.attname AS column_name, k.ord
FROM pg_index idx
JOIN pg_class c ON c.oid = idx.indrelid
JOIN pg_class ic ON ic.oid = idx.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN LATERAL unnest(idx.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
JOIN pg_attribute a ON a.attrelid = idx.indrelid AND a.attnum = k.attnum
WHERE n.nspname = %(schema)s
  AND NOT idx.indisprimary
  AND NOT EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = idx.indexrelid)
ORDER BY c.relname, ic.relname, k.ord
"""


def postgres_conninfo() -> str:
    return " ".join(
        [
            f"host={os.getenv('POSTGRES_HOST', 'postgres')}",
            f"port={os.getenv('POSTGRES_PORT', '5432')}",
            f"dbname={os.getenv('POSTGRES_DB', 'campaign_db')}",
            f"user={os.getenv('POSTGRES_USER', 'postgres')}",
            f"password={os.getenv('POSTGRES_PASSWORD', '1234')}",
        ]
    )


def normalize_db_type(formatted: str) -> str:
    text = formatted.strip()
    match = re.match(r"^([a-z ]+?)(\([^)]*\))?$", text, re.IGNORECASE)
    if not match:
        return text.upper()
    base = match.group(1).strip().lower()
    params = match.group(2) or ""
    return TYPE_BASE_MAP.get(base, base.upper()) + params.upper()


def introspect_schema(conninfo: str | None = None, schema_name: str = "public") -> dict[str, Any]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - depends on runtime env
        raise RuntimeError("psycopg가 필요합니다. requirements.txt의 psycopg[binary]를 설치하세요.") from exc

    params = {"schema": schema_name}
    with psycopg.connect(conninfo or postgres_conninfo(), row_factory=dict_row, connect_timeout=5) as conn:
        with conn.cursor() as cursor:
            cursor.execute(_INTROSPECT_COLUMNS_SQL, params)
            column_rows = cursor.fetchall()
            cursor.execute(_INTROSPECT_PK_SQL, params)
            pk_rows = cursor.fetchall()
            cursor.execute(_INTROSPECT_FK_SQL, params)
            fk_rows = cursor.fetchall()
            cursor.execute(_INTROSPECT_CHECK_SQL, params)
            check_rows = cursor.fetchall()
            cursor.execute(_INTROSPECT_INDEX_SQL, params)
            index_rows = cursor.fetchall()

    return _build_catalog_from_rows(schema_name, column_rows, pk_rows, fk_rows, check_rows, index_rows)


def _build_catalog_from_rows(
    schema_name: str,
    column_rows: list[dict[str, Any]],
    pk_rows: list[dict[str, Any]],
    fk_rows: list[dict[str, Any]],
    check_rows: list[dict[str, Any]],
    index_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    primary_keys: dict[str, list[str]] = {}
    for row in pk_rows:
        primary_keys.setdefault(row["table_name"], []).append(row["column_name"])

    foreign_keys: dict[str, dict[str, dict[str, Any]]] = {}
    for row in fk_rows:
        table_fks = foreign_keys.setdefault(row["table_name"], {})
        entry = table_fks.setdefault(
            row["conname"],
            {"columns": [], "references": {"table": row["ref_table"], "columns": []}},
        )
        entry["columns"].append(row["column_name"])
        entry["references"]["columns"].append(row["ref_column"])

    checks: dict[str, list[str]] = {}
    for row in check_rows:
        checks.setdefault(row["table_name"], []).append(row["definition"])

    indexes: dict[str, dict[str, dict[str, Any]]] = {}
    for row in index_rows:
        table_indexes = indexes.setdefault(row["table_name"], {})
        entry = table_indexes.setdefault(
            row["index_name"],
            {"name": row["index_name"], "unique": bool(row["is_unique"]), "columns": []},
        )
        entry["columns"].append(row["column_name"])

    tables: dict[str, Any] = {}
    for row in column_rows:
        table_name = row["table_name"]
        table = tables.get(table_name)
        if table is None:
            is_view = bool(row["is_view"])
            table = tables[table_name] = {
                "object_type": "view" if is_view else "table",
                "description_llm": DEFAULT_OBJECT_DESCRIPTIONS.get(table_name, ""),
                "description_source": "introspected_from_db",
                "logical_name": None,
                "row_grain": None,
                "expected_cardinality": None,
                "duplication_risk": None,
                "history_table": None,
                "includes_deleted": None,
                "business_rules": {},
                "columns": [],
                "primary_key": primary_keys.get(table_name, []),
                "checks": checks.get(table_name, []),
                "foreign_keys": list(foreign_keys.get(table_name, {}).values()),
                "indexes": list(indexes.get(table_name, {}).values()),
            }

        column_name = row["column_name"]
        single_column_ref = _single_column_reference(column_name, foreign_keys.get(table_name, {}))
        column_type = normalize_db_type(row["data_type"])
        default_expr = (row.get("default_expr") or "").lower()
        if "nextval(" in default_expr:
            column_type = SERIAL_TYPE_MAP.get(column_type, column_type)
        column_metadata = _aggregation_column_metadata(column_name, column_type, column_name in primary_keys.get(table_name, []))
        table["columns"].append(
            {
                "name": column_name,
                "type": column_type,
                "nullable": not row["not_null"],
                "primary_key": column_name in primary_keys.get(table_name, []),
                "references": single_column_ref,
                "important": column_name in IMPORTANT_COLUMN_NAMES,
                "human_note": "",
                **column_metadata,
            }
        )

    return {
        "source": f"postgresql://{schema_name} (information_schema/pg_catalog)",
        "policy": {
            "schema": "introspected_from_live_db",
            "llm": "table_descriptions_only",
            "human_edit": "important_columns_only",
            "sql_examples_limit": "10_to_30",
            "ops_feedback": "add_missing_dictionary_terms_and_examples_from_logs",
        },
        "tables": tables,
    }


def _aggregation_column_metadata(column_name: str, column_type: str, primary_key: bool) -> dict[str, Any]:
    """DB 구조에서 확정 가능한 최소 집계 메타데이터만 만든다.

    업무 정의(매출 공식, 정상 주문 코드 등)는 추론하지 않는다. 이름 기반 semantic_roles는
    후보 탐색용이며 ``metadata_source``로 명시해 검증된 업무 규칙과 구분한다.
    """
    name = column_name.casefold()
    base_type = re.split(r"[\s(]", column_type.casefold(), maxsplit=1)[0]
    numeric = base_type in {
        "tinyint", "smallint", "integer", "int", "bigint", "decimal", "numeric",
        "real", "float", "double", "money", "smallmoney",
    }
    roles: list[str] = []
    if primary_key or name.endswith(("_id", "_no", "_key")):
        roles.append("identifier")
    if any(token in name for token in ("date", "_dt", "_at", "year", "month", "day")):
        roles.append("date")
    if any(token in name for token in ("amount", "_amt", "price", "revenue", "sales")):
        roles.append("amount")
    if any(token in name for token in ("quantity", "_qty")):
        roles.append("quantity")
    if any(token in name for token in ("status", "state")):
        roles.append("status")
    if any(token in name for token in ("_cd", "_code")):
        roles.append("code")
    aggregatable = numeric and "identifier" not in roles and "code" not in roles
    if aggregatable:
        recommended = ["sum", "avg", "min", "max"]
    elif "identifier" in roles:
        recommended = ["count_distinct"]
    elif "date" in roles:
        recommended = ["min", "max"]
    else:
        recommended = []
    return {
        "logical_name": None,
        "unit": None,
        "code_column": "code" in roles or "status" in roles,
        "value_examples": [],
        "aggregatable": aggregatable,
        "recommended_aggregations": recommended,
        "semantic_roles": roles,
        "unique": primary_key,
        "aggregation_metadata_source": "inferred_from_physical_schema",
    }


def _single_column_reference(column_name: str, table_fks: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for foreign_key in table_fks.values():
        if len(foreign_key["columns"]) == 1 and foreign_key["columns"][0] == column_name:
            return {
                "table": foreign_key["references"]["table"],
                "column": foreign_key["references"]["columns"][0],
            }
    return None


# ---------------------------------------------------------------------------
# 외부 실DB(MSSQL/MariaDB) 카탈로그 리프레시 — DB 이식성 D단계
# (docs/operations/db_portability_audit.md §4-D)
#
# 실DB 카탈로그는 '승인 테이블만 큐레이션'된 파일이라 전체 덤프가 아니라 **리프레시**가 맞다:
# 기존 schema_catalog.json 의 테이블 집합을 각 테이블의 `database`(커넥션) 실DB에서
# INFORMATION_SCHEMA 로 재인트로스펙션해 구조(컬럼/타입/널러블/PK/객체유형)만 갱신하고,
# 사람이 쓴 지식(description_llm/join_hints/human_note/important/references/FK)은 보존한다.
# 신규/삭제 컬럼과 실DB에 없는 테이블은 요약으로 보고한다. DB 스왑 시에는 테이블 집합과
# `database` 필드를 새 DB 기준으로 고친 뒤 이 모드를 돌리면 구조가 채워진다.
# ---------------------------------------------------------------------------


def _external_type_label(row: dict[str, Any], dialect: str) -> str:
    """카탈로그 표기 타입(예: 'nvarchar(100)', 'bigint'). MySQL 은 COLUMN_TYPE 이 완성형이라 그대로 쓴다."""
    if dialect == "mysql" and row.get("full_type"):
        return str(row["full_type"]).lower()
    data_type = str(row.get("data_type") or "").lower()
    char_len = row.get("char_len")
    if data_type in ("varchar", "nvarchar", "char", "nchar", "binary", "varbinary") and char_len is not None:
        return f"{data_type}(max)" if int(char_len) == -1 else f"{data_type}({int(char_len)})"
    num_prec, num_scale = row.get("num_prec"), row.get("num_scale")
    if data_type in ("decimal", "numeric") and num_prec is not None:
        return f"{data_type}({int(num_prec)},{int(num_scale or 0)})"
    return data_type


def _in_placeholders(count: int) -> str:
    return ", ".join(["%s"] * count)


def _introspect_external_tables(connection: str, table_names: list[str], dialect: str) -> dict[str, dict[str, Any]]:
    """커넥션 실DB에서 요청 테이블들의 컬럼/PK/객체유형을 읽는다(INFORMATION_SCHEMA — MSSQL/MySQL 공통).

    반환: {table_name: {"columns": [...], "primary_key": [...], "object_type": "table"|"view"}}
    MySQL 은 같은 서버에 스키마가 여럿이라 TABLE_SCHEMA = DATABASE() 로 현재 DB 로 한정한다.
    """
    import db_connections as db

    placeholders = _in_placeholders(len(table_names))
    schema_filter = " AND TABLE_SCHEMA = DATABASE()" if dialect == "mysql" else ""
    extra_type_column = ", COLUMN_TYPE AS full_type" if dialect == "mysql" else ""
    params = tuple(table_names)

    column_rows = db.run_read_query(
        connection,
        "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, ORDINAL_POSITION AS ordinal, "
        "DATA_TYPE AS data_type, CHARACTER_MAXIMUM_LENGTH AS char_len, "
        "NUMERIC_PRECISION AS num_prec, NUMERIC_SCALE AS num_scale, IS_NULLABLE AS is_nullable"
        f"{extra_type_column} "
        f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME IN ({placeholders}){schema_filter} "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION",
        params,
    )
    pk_rows = db.run_read_query(
        connection,
        "SELECT ku.TABLE_NAME AS table_name, ku.COLUMN_NAME AS column_name, ku.ORDINAL_POSITION AS ordinal "
        "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc "
        "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku "
        "  ON ku.CONSTRAINT_NAME = tc.CONSTRAINT_NAME AND ku.TABLE_SCHEMA = tc.TABLE_SCHEMA AND ku.TABLE_NAME = tc.TABLE_NAME "
        f"WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND ku.TABLE_NAME IN ({placeholders})"
        f"{schema_filter.replace('TABLE_SCHEMA', 'ku.TABLE_SCHEMA')} "
        "ORDER BY ku.TABLE_NAME, ku.ORDINAL_POSITION",
        params,
    )
    type_rows = db.run_read_query(
        connection,
        "SELECT TABLE_NAME AS table_name, TABLE_TYPE AS table_type "
        f"FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME IN ({placeholders}){schema_filter}",
        params,
    )

    result: dict[str, dict[str, Any]] = {}
    for row in type_rows:
        table_type = str(row.get("table_type") or "").upper()
        result[row["table_name"]] = {
            "columns": [],
            "primary_key": [],
            "object_type": "view" if "VIEW" in table_type else "table",
        }
    for row in pk_rows:
        if row["table_name"] in result:
            result[row["table_name"]]["primary_key"].append(row["column_name"])
    for row in column_rows:
        if row["table_name"] not in result:
            continue
        result[row["table_name"]]["columns"].append(
            {
                "name": row["column_name"],
                "type": _external_type_label(row, dialect),
                "nullable": str(row.get("is_nullable") or "").upper() == "YES",
            }
        )
    return result


def _merge_live_structure(meta: dict[str, Any], live: dict[str, Any]) -> dict[str, list[str]]:
    """실DB 구조를 카탈로그 테이블 항목에 반영한다. 사람 지식(human_note/important/references)은
    컬럼명 기준으로 보존하고, 신규 컬럼은 빈 주석으로 추가·사라진 컬럼은 제거해 보고한다."""
    old_by_name = {column.get("name"): column for column in meta.get("columns", []) if isinstance(column, dict)}
    live_pk = live.get("primary_key") or []
    new_columns: list[dict[str, Any]] = []
    for column in live["columns"]:
        old = old_by_name.get(column["name"], {})
        new_columns.append(
            {
                "name": column["name"],
                "type": column["type"],
                "nullable": column["nullable"],
                "primary_key": (column["name"] in live_pk) if live_pk else bool(old.get("primary_key")),
                "references": old.get("references"),
                "important": bool(old.get("important", False)),
                "human_note": old.get("human_note", ""),
            }
        )
    live_names = {column["name"] for column in live["columns"]}
    added = [name for name in live_names if name not in old_by_name]
    removed = [name for name in old_by_name if name not in live_names]
    meta["columns"] = new_columns
    if live_pk:
        meta["primary_key"] = live_pk
    if live.get("object_type"):
        meta["object_type"] = live["object_type"]
    meta["description_source"] = "live_db_information_schema"
    return {"added": sorted(added), "removed": sorted(removed)}


def refresh_external_catalog(
    existing_schema: dict[str, Any],
    connection_filter: str | None = None,
    table_filter: set[str] | None = None,
) -> dict[str, Any]:
    """카탈로그의 외부 실DB 테이블들을 재인트로스펙션해 구조를 갱신하고 변경 요약을 반환한다."""
    from sql_dialect import CONNECTION_DIALECTS

    tables = existing_schema.get("tables", {})
    by_connection: dict[str, list[str]] = {}
    for name, meta in tables.items():
        if not isinstance(meta, dict):
            continue
        database = meta.get("database")
        if not database:
            continue  # 로컬(postgres) 테이블은 --from-db 경로 소관
        if connection_filter and database != connection_filter:
            continue
        if table_filter and name not in table_filter:
            continue
        by_connection.setdefault(database, []).append(name)

    summary: dict[str, Any] = {"connections": {}, "changed_tables": {}, "missing_in_db": []}
    for connection, names in sorted(by_connection.items()):
        dialect = CONNECTION_DIALECTS.get(connection)
        if dialect not in ("tsql", "mysql"):
            summary["connections"][connection] = f"skipped(unknown dialect: {dialect})"
            continue
        live_by_table = _introspect_external_tables(connection, names, dialect)
        refreshed = 0
        for name in names:
            live = live_by_table.get(name)
            if not live or not live["columns"]:
                summary["missing_in_db"].append(f"{connection}:{name}")
                continue  # 실DB에 없거나 컬럼을 못 읽은 테이블은 기존 항목을 보존한다
            diff = _merge_live_structure(tables[name], live)
            refreshed += 1
            if diff["added"] or diff["removed"]:
                summary["changed_tables"][name] = diff
        summary["connections"][connection] = f"refreshed {refreshed}/{len(names)}"
    return summary


def reorder_tables_like(schema: dict[str, Any], existing_schema: dict[str, Any] | None) -> dict[str, Any]:
    if not existing_schema:
        return schema

    existing_order = list(existing_schema.get("tables", {}).keys())
    current_tables = schema["tables"]
    ordered: dict[str, Any] = {}
    for table_name in existing_order:
        if table_name in current_tables:
            ordered[table_name] = current_tables[table_name]
    for table_name in current_tables:
        if table_name not in ordered:
            ordered[table_name] = current_tables[table_name]
    schema["tables"] = ordered
    return schema


def split_sql_items(body: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = False

    for char in body:
        if char == "'":
            in_quote = not in_quote
        elif not in_quote and char == "(":
            depth += 1
        elif not in_quote and char == ")":
            depth -= 1

        if char == "," and depth == 0 and not in_quote:
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
            continue

        current.append(char)

    item = "".join(current).strip()
    if item:
        items.append(item)
    return items


def normalize_identifier(identifier: str) -> str:
    return identifier.strip().strip('"').split(".")[-1]


def parse_column(item: str) -> dict[str, Any] | None:
    normalized = " ".join(item.split())
    if normalized.upper().startswith(TABLE_CONSTRAINT_PREFIXES):
        return None

    match = COLUMN_RE.match(normalized)
    if not match:
        return None

    constraints = match.group("constraints").strip()
    reference = REFERENCES_RE.search(constraints)
    return {
        "name": normalize_identifier(match.group("name")),
        "type": match.group("type").upper(),
        "nullable": "NOT NULL" not in constraints.upper() and "PRIMARY KEY" not in constraints.upper(),
        "primary_key": "PRIMARY KEY" in constraints.upper(),
        "references": {
            "table": normalize_identifier(reference.group("table")),
            "column": normalize_identifier(reference.group("column")),
        }
        if reference
        else None,
        "important": normalize_identifier(match.group("name")) in IMPORTANT_COLUMN_NAMES,
        "human_note": "",
    }


def parse_table_constraints(items: list[str]) -> dict[str, Any]:
    primary_key: list[str] = []
    checks: list[str] = []
    foreign_keys: list[dict[str, Any]] = []

    for item in items:
        normalized = " ".join(item.split())
        upper = normalized.upper()
        if upper.startswith("PRIMARY KEY"):
            inside = normalized[normalized.find("(") + 1 : normalized.rfind(")")]
            primary_key = [normalize_identifier(column) for column in inside.split(",")]
        elif upper.startswith("CHECK"):
            checks.append(normalized)
        if foreign_key := FOREIGN_KEY_RE.search(normalized):
            foreign_keys.append(
                {
                    "columns": [normalize_identifier(column) for column in foreign_key.group("columns").split(",")],
                    "references": {
                        "table": normalize_identifier(foreign_key.group("table")),
                        "columns": [normalize_identifier(column) for column in foreign_key.group("ref_columns").split(",")],
                    },
                }
            )

    return {"primary_key": primary_key, "checks": checks, "foreign_keys": foreign_keys}


def extract_schema(sql: str) -> dict[str, Any]:
    tables: dict[str, Any] = {}

    for match in CREATE_TABLE_RE.finditer(sql):
        table_name = normalize_identifier(match.group("table"))
        items = split_sql_items(match.group("body"))
        table_constraints = parse_table_constraints(items)
        columns = [column for item in items if (column := parse_column(item))]

        for column in columns:
            if column["name"] in table_constraints["primary_key"]:
                column["primary_key"] = True
                column["nullable"] = False

        tables[table_name] = {
            "object_type": "table",
            "description_llm": DEFAULT_OBJECT_DESCRIPTIONS.get(table_name, ""),
            "description_source": "llm_table_only",
            "columns": columns,
            "primary_key": table_constraints["primary_key"] or [column["name"] for column in columns if column["primary_key"]],
            "checks": table_constraints["checks"],
            "foreign_keys": table_constraints["foreign_keys"],
            "indexes": [],
        }

    for match in CREATE_VIEW_RE.finditer(sql):
        view_name = normalize_identifier(match.group("view"))
        tables[view_name] = {
            "object_type": "view",
            "description_llm": DEFAULT_OBJECT_DESCRIPTIONS.get(view_name, ""),
            "description_source": "ddl_view",
            "columns": _view_columns(match.group("body")),
            "primary_key": [],
            "checks": [],
            "foreign_keys": [],
            "indexes": [],
        }

    for match in CREATE_INDEX_RE.finditer(sql):
        table_name = normalize_identifier(match.group("table"))
        if table_name not in tables:
            continue
        tables[table_name]["indexes"].append(
            {
                "name": match.group("name"),
                "unique": bool(match.group("unique")),
                "columns": [normalize_identifier(column) for column in match.group("columns").split(",")],
            }
        )

    return {
        "source": "docs/data/local_bootstrap.sql",
        "policy": {
            "schema": "auto_extracted_from_ddl",
            "llm": "table_descriptions_only",
            "human_edit": "important_columns_only",
            "sql_examples_limit": "10_to_30",
            "ops_feedback": "add_missing_dictionary_terms_and_examples_from_logs",
        },
        "tables": tables,
    }


def _view_columns(view_body: str) -> list[dict[str, Any]]:
    select_list = _outer_select_list(view_body)
    return [_view_column(item) for item in split_sql_items(select_list) if _view_column(item)]


def _outer_select_list(sql: str) -> str:
    select_positions = [match.end() for match in re.finditer(r"\bSELECT\b", sql, re.IGNORECASE)]
    if not select_positions:
        return ""
    select_start = select_positions[-1]
    from_start = _first_top_level_from(sql, select_start)
    return sql[select_start:from_start].strip() if from_start != -1 else ""


def _first_top_level_from(sql: str, start: int) -> int:
    depth = 0
    in_quote = False
    index = start
    while index < len(sql):
        char = sql[index]
        if char == "'":
            in_quote = not in_quote
        elif not in_quote and char == "(":
            depth += 1
        elif not in_quote and char == ")":
            depth -= 1
        elif depth == 0 and not in_quote and re.match(r"\bFROM\b", sql[index:], re.IGNORECASE):
            return index
        index += 1
    return -1


def _view_column(item: str) -> dict[str, Any] | None:
    normalized = " ".join(item.split())
    alias_match = re.search(r"\bAS\s+([a-zA-Z_][a-zA-Z0-9_]*)$", normalized, re.IGNORECASE)
    if alias_match:
        name = normalize_identifier(alias_match.group(1))
    else:
        name_match = re.search(r"(?:^|\.)([a-zA-Z_][a-zA-Z0-9_]*)$", normalized)
        if not name_match:
            return None
        name = normalize_identifier(name_match.group(1))
    return {
        "name": name,
        "type": "VIEW_COLUMN",
        "nullable": True,
        "primary_key": False,
        "references": None,
        "important": name in IMPORTANT_COLUMN_NAMES,
        "human_note": "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a schema catalog from a live PostgreSQL database (default) or a DDL file."
    )
    parser.add_argument("ddl", type=Path, nargs="?", help="DDL 파일 경로. --from-db 없이 쓸 때 필요.")
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="라이브 DB(information_schema/pg_catalog)에서 스키마를 읽는다. DDL 파싱 대신 사용.",
    )
    parser.add_argument(
        "--refresh-external",
        action="store_true",
        help="기존 카탈로그의 외부 실DB(MSSQL/MariaDB) 테이블 구조를 INFORMATION_SCHEMA 로 재인트로스펙션한다"
        "(승인 테이블 집합·사람 주석은 보존 — DB 스왑/스키마 변경 반영용).",
    )
    parser.add_argument("--connection", default=None, help="--refresh-external 대상 커넥션 필터(CRMDW 등). 기본 전체.")
    parser.add_argument("--tables", default=None, help="--refresh-external 대상 테이블 필터(쉼표 구분). 기본 전체.")
    parser.add_argument("--dry-run", action="store_true", help="--refresh-external 결과를 쓰지 않고 변경 요약만 출력한다.")
    parser.add_argument("--conninfo", default=None, help="psycopg conninfo 문자열. 미지정 시 POSTGRES_* 환경변수 사용.")
    parser.add_argument("--schema-name", default="public", help="대상 스키마 이름. 기본 public.")
    parser.add_argument(
        "--output", "-o", type=Path,
        default=Path("docs/data/generated/schema_catalog.json"),
    )
    args = parser.parse_args()

    existing_schema = None
    if args.output.exists():
        existing_schema = json.loads(args.output.read_text(encoding="utf-8"))

    if args.refresh_external:
        if not existing_schema:
            parser.error(f"--refresh-external 은 기존 카탈로그({args.output})가 있어야 합니다(승인 테이블 집합 기준).")
        table_filter = {name.strip() for name in args.tables.split(",") if name.strip()} if args.tables else None
        summary = refresh_external_catalog(existing_schema, connection_filter=args.connection, table_filter=table_filter)
        if not args.dry_run:
            args.output.write_text(json.dumps(existing_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            json.dumps(
                {"mode": "refresh_external", "dry_run": args.dry_run, "output": str(args.output), **summary},
                ensure_ascii=False,
            )
        )
        return

    if args.from_db:
        schema = introspect_schema(conninfo=args.conninfo, schema_name=args.schema_name)
        schema = reorder_tables_like(schema, existing_schema)
    elif args.ddl is not None:
        schema = extract_schema(args.ddl.read_text(encoding="utf-8"))
    else:
        parser.error("DDL 파일 경로를 주거나 --from-db / --refresh-external 을 지정하세요.")

    schema = merge_existing_annotations(schema, existing_schema)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "mode": "from_db" if args.from_db else "from_ddl",
                "output": str(args.output),
                "table_count": len(schema["tables"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
