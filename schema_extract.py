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

        existing_columns = {
            column.get("name"): column
            for column in existing_table.get("columns", [])
            if column.get("name")
        }
        for column in table["columns"]:
            existing_column = existing_columns.get(column["name"], {})
            column["human_note"] = existing_column.get("human_note", column["human_note"])

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
        table["columns"].append(
            {
                "name": column_name,
                "type": column_type,
                "nullable": not row["not_null"],
                "primary_key": column_name in primary_keys.get(table_name, []),
                "references": single_column_ref,
                "important": column_name in IMPORTANT_COLUMN_NAMES,
                "human_note": "",
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


def _single_column_reference(column_name: str, table_fks: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for foreign_key in table_fks.values():
        if len(foreign_key["columns"]) == 1 and foreign_key["columns"][0] == column_name:
            return {
                "table": foreign_key["references"]["table"],
                "column": foreign_key["references"]["columns"][0],
            }
    return None


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
    parser.add_argument("--conninfo", default=None, help="psycopg conninfo 문자열. 미지정 시 POSTGRES_* 환경변수 사용.")
    parser.add_argument("--schema-name", default="public", help="대상 스키마 이름. 기본 public.")
    parser.add_argument("--output", "-o", type=Path, default=Path("docs/data/schema_catalog.json"))
    args = parser.parse_args()

    existing_schema = None
    if args.output.exists():
        existing_schema = json.loads(args.output.read_text(encoding="utf-8"))

    if args.from_db:
        schema = introspect_schema(conninfo=args.conninfo, schema_name=args.schema_name)
        schema = reorder_tables_like(schema, existing_schema)
    elif args.ddl is not None:
        schema = extract_schema(args.ddl.read_text(encoding="utf-8"))
    else:
        parser.error("DDL 파일 경로를 주거나 --from-db 를 지정하세요.")

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
