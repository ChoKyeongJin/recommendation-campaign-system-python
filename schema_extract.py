import argparse
import json
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
        "source": "docs/data/ddl.sql",
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
    parser = argparse.ArgumentParser(description="Extract a schema catalog from PostgreSQL DDL.")
    parser.add_argument("ddl", type=Path)
    parser.add_argument("--output", "-o", type=Path, default=Path("docs/data/schema_catalog.json"))
    args = parser.parse_args()

    existing_schema = None
    if args.output.exists():
        existing_schema = json.loads(args.output.read_text(encoding="utf-8"))

    schema = extract_schema(args.ddl.read_text(encoding="utf-8"))
    schema = merge_existing_annotations(schema, existing_schema)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(schema['tables'])} tables)")


if __name__ == "__main__":
    main()
