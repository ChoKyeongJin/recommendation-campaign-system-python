from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


DEFAULT_SCHEMA_PATH = Path("docs/data/schema_catalog.json")
DEFAULT_LIMIT = 100
FORBIDDEN_KEYWORDS = {
    "alter",
    "create",
    "delete",
    "drop",
    "insert",
    "merge",
    "truncate",
    "update",
}
SENSITIVE_COLUMN_PATTERNS = (
    "email",
    "phone",
    "mobile",
    "address",
    "birth",
    "ssn",
    "password",
    "token",
    "name",
)


def load_allowed_tables(schema_path: Path = DEFAULT_SCHEMA_PATH) -> set[str]:
    if not schema_path.exists():
        return set()

    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    tables = payload.get("tables", {})
    if not isinstance(tables, dict):
        return set()
    return set(tables)


def _database_dialect(description: str) -> str:
    """DB 설명 문자열에서 SQL 방언을 판별한다. SQL Server 계열이면 tsql, 그 외는 mysql."""
    lowered = description.casefold()
    if "sql server" in lowered or "mssql" in lowered or "sqlserver" in lowered:
        return "tsql"
    return "mysql"


def load_table_dialects(schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, str]:
    """스키마 카탈로그에서 테이블명 -> SQL 방언('tsql'|'mysql') 매핑을 만든다.

    각 테이블의 `database` 필드를 최상위 `databases` 설명에 매핑해 방언을 결정한다.
    (예: CRMDW/CRMAN=SQL Server→tsql, quadmax_sdz=MariaDB→mysql)
    """
    if not schema_path.exists():
        return {}
    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    databases = payload.get("databases", {})
    db_dialect = (
        {name: _database_dialect(str(desc)) for name, desc in databases.items()}
        if isinstance(databases, dict)
        else {}
    )
    tables = payload.get("tables", {})
    if not isinstance(tables, dict):
        return {}
    result: dict[str, str] = {}
    for name, meta in tables.items():
        if isinstance(meta, dict):
            dialect = db_dialect.get(meta.get("database"))
            if dialect:
                result[name] = dialect
    return result


def infer_sql_dialect(tables: list[str], table_dialects: dict[str, str], default: str = "mysql") -> str:
    """참조 테이블 중 하나라도 tsql 이면 tsql. (교차 DB 조인은 실행 불가하므로 단일 방언 가정)"""
    for table in tables:
        dialect = table_dialects.get(table) or table_dialects.get(table.split(".")[-1])
        if dialect == "tsql":
            return "tsql"
    return default


def load_table_databases(schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, str]:
    """스키마 카탈로그에서 테이블명 -> 소속 DB/커넥션명(CRMDW|CRMAN|quadmax_sdz) 매핑."""
    if not schema_path.exists():
        return {}
    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    tables = payload.get("tables", {})
    if not isinstance(tables, dict):
        return {}
    return {
        name: meta["database"]
        for name, meta in tables.items()
        if isinstance(meta, dict) and meta.get("database")
    }


def infer_target_connection(tables: list[str], table_databases: dict[str, str]) -> str | None:
    """참조 테이블 중 카탈로그에 등록된 외부 DB가 있으면 그 커넥션명을 반환한다(없으면 None=로컬).

    카탈로그에는 외부 실DB(CRMDW/CRMAN/quadmax_sdz) 테이블만 등록돼 있고, 로컬 postgres
    데모 테이블(users/campaigns 등)은 없다. 따라서 매칭되는 테이블의 database 가 곧 실행 대상.
    """
    for table in tables:
        connection = table_databases.get(table) or table_databases.get(table.split(".")[-1])
        if connection:
            return connection
    return None


def validate_sql(
    sql: str,
    allowed_tables: set[str] | None = None,
    default_limit: int | None = DEFAULT_LIMIT,
    dialect: str | None = None,
    table_dialects: dict[str, str] | None = None,
) -> dict[str, Any]:
    cleaned_sql = _strip_sql_comments(sql).strip()
    statements = _sql_statements(cleaned_sql)
    issues: list[dict[str, str]] = []

    if not statements:
        issues.append(_issue("empty_sql", "error", "SQL is empty."))
        return _result(False, cleaned_sql, cleaned_sql, [], [], issues)

    if len(statements) > 1:
        issues.append(_issue("multiple_statements", "error", "Only one SELECT statement is allowed."))

    statement = statements[0]
    lowered_statement = statement.casefold()
    is_select = _is_select_statement(statement)
    if not is_select:
        issues.append(_issue("non_select_statement", "error", "Only SELECT statements are allowed."))

    forbidden = sorted(keyword for keyword in FORBIDDEN_KEYWORDS if re.search(fr"\b{keyword}\b", lowered_statement))
    for keyword in forbidden:
        issues.append(_issue("forbidden_keyword", "error", f"Forbidden SQL keyword: {keyword.upper()}"))

    tables = _extract_tables(statement)
    allowed = allowed_tables or set()
    if allowed:
        for table in tables:
            if table not in allowed:
                issues.append(_issue("table_not_allowed", "error", f"Table is not allowed: {table}"))

    sensitive_columns = _selected_sensitive_columns(statement)
    if sensitive_columns:
        issues.append(
            _issue(
                "sensitive_columns",
                "warning",
                "Sensitive columns should be masked or excluded: " + ", ".join(sensitive_columns),
            )
        )

    resolved_dialect = dialect or infer_sql_dialect(tables, table_dialects or {})

    # default_limit is None → 행수 제한을 붙이지 않는다(전체 결과 반환).
    if is_select and default_limit is not None:
        safe_sql = _apply_row_limit(statement, default_limit, resolved_dialect)
    else:
        safe_sql = statement
    if is_select and safe_sql != statement:
        clause = "TOP" if resolved_dialect == "tsql" else "LIMIT"
        issues.append(_issue("limit_added", "warning", f"Default {clause} {default_limit} was added."))

    if (
        is_select
        and default_limit is not None
        and not re.search(r"\bwhere\b", lowered_statement)
        and not re.search(r"\b(?:limit|top)\b", lowered_statement)
    ):
        issues.append(_issue("unbounded_scan", "warning", "Query has no WHERE clause; default row limit is required."))

    masked_sql = _mask_sensitive_select_columns(safe_sql)
    is_valid = not any(issue["severity"] == "error" for issue in issues)
    return _result(
        is_valid, statement, safe_sql, tables, sensitive_columns, issues, masked_sql=masked_sql, dialect=resolved_dialect
    )


def _strip_sql_comments(sql: str) -> str:
    without_line_comments = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", without_line_comments, flags=re.DOTALL)


def _sql_statements(sql: str) -> list[str]:
    statements = [part.strip() for part in sql.split(";") if part.strip()]
    return statements


def _extract_tables(sql: str) -> list[str]:
    cte_names = _extract_cte_names(sql)
    tables = []
    for match in re.finditer(r"\b(?:from|join)\s+([a-zA-Z_][\w.]*)", sql, re.IGNORECASE):
        table_name = match.group(1).split(".")[-1]
        if table_name in cte_names:
            continue
        if table_name not in tables:
            tables.append(table_name)
    return tables


def _is_select_statement(sql: str) -> bool:
    return bool(re.match(r"^\s*(?:select\b|with\b[\s\S]+\bselect\b)", sql, re.IGNORECASE))


def _extract_cte_names(sql: str) -> set[str]:
    if not re.match(r"^\s*with\b", sql, re.IGNORECASE):
        return set()
    cte_names = set()
    depth = 0
    token_start = 0
    for index, char in enumerate(sql):
        if char == "(":
            if depth == 0:
                prefix = sql[token_start:index]
                match = re.search(r"([a-zA-Z_][\w]*)\s+AS\s*$", prefix, re.IGNORECASE)
                if match:
                    cte_names.add(match.group(1))
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
            if depth == 0:
                token_start = index + 1
    return cte_names


def _selected_sensitive_columns(sql: str) -> list[str]:
    select_match = re.search(r"\bselect\b(?P<select>.*?)\bfrom\b", sql, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return []

    select_text = select_match.group("select")
    if _has_select_star(select_text):
        return ["*"]

    sensitive_columns = []
    for column in _split_select_items(select_text):
        column_name = _column_name(column)
        if any(pattern in column_name.casefold() for pattern in SENSITIVE_COLUMN_PATTERNS):
            sensitive_columns.append(column_name)
    return sensitive_columns


def _mask_sensitive_select_columns(sql: str) -> str:
    select_match = re.search(r"\bselect\b(?P<select>.*?)\bfrom\b", sql, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return sql

    select_text = select_match.group("select")
    if _has_select_star(select_text):
        return sql

    masked_items = []
    changed = False
    for item in _split_select_items(select_text):
        column_name = _column_name(item)
        if any(pattern in column_name.casefold() for pattern in SENSITIVE_COLUMN_PATTERNS):
            masked_items.append(f"NULL AS {column_name}_masked")
            changed = True
        else:
            masked_items.append(item.strip())

    if not changed:
        return sql

    return sql[: select_match.start("select")] + " " + ", ".join(masked_items) + " " + sql[select_match.end("select") :]


def _split_select_items(select_text: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    depth = 0
    for char in select_text:
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        elif char == "," and depth == 0:
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


def _has_select_star(select_text: str) -> bool:
    return any(re.fullmatch(r"(?:[a-zA-Z_][\w]*\.)?\*", item.strip()) for item in _split_select_items(select_text))


def _column_name(select_item: str) -> str:
    alias_match = re.search(r"\bas\s+([a-zA-Z_][\w]*)\s*$", select_item, re.IGNORECASE)
    if alias_match:
        return alias_match.group(1)
    token = select_item.strip().split()[-1]
    return token.split(".")[-1].strip('"')


def _apply_row_limit(sql: str, default_limit: int, dialect: str) -> str:
    if dialect == "tsql":
        return _ensure_top(sql, default_limit)
    return _ensure_limit(sql, default_limit)


def _ensure_limit(sql: str, default_limit: int) -> str:
    if re.search(r"\blimit\s+\d+\b", sql, re.IGNORECASE):
        return sql
    return sql.rstrip().rstrip(";") + f" LIMIT {default_limit}"


def _ensure_top(sql: str, default_limit: int) -> str:
    """SQL Server(T-SQL)용 행수 제한: 외곽 SELECT 뒤에 TOP n 을 주입한다.

    이미 TOP 또는 OFFSET/FETCH 로 제한돼 있으면 그대로 둔다. LIMIT 은 T-SQL 에서 무효라 붙이지 않는다.
    """
    if re.search(r"\btop\s*\(?\s*\d+", sql, re.IGNORECASE):
        return sql
    if re.search(r"\boffset\b\s+\d+\s+rows?\b", sql, re.IGNORECASE):
        return sql
    span = _outer_select_span(sql)
    if span is None:
        return sql
    end = span[1]
    return sql[:end] + f" TOP {default_limit}" + sql[end:]


def _outer_select_span(sql: str) -> tuple[int, int] | None:
    """최외곽(파렌 깊이 0) 쿼리의 'SELECT [DISTINCT]' 구간 (start, end) 을 반환한다.

    서브쿼리/CTE 내부 SELECT(깊이>0)는 건너뛴다. WITH ... SELECT 는 CTE 정의가 괄호 안이므로
    본 SELECT 가 마지막 깊이 0 SELECT 로 잡힌다.
    """
    depth = 0
    span: tuple[int, int] | None = None
    for match in re.finditer(r"[()]|\bselect\b(?:\s+distinct\b)?", sql, re.IGNORECASE):
        token = match.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            if depth > 0:
                depth -= 1
        elif depth == 0:
            span = (match.start(), match.end())
    return span


def _issue(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _result(
    is_valid: bool,
    sql: str,
    safe_sql: str,
    tables: list[str],
    sensitive_columns: list[str],
    issues: list[dict[str, str]],
    masked_sql: str | None = None,
    dialect: str = "mysql",
) -> dict[str, Any]:
    return {
        "is_valid": is_valid,
        "sql": sql,
        "safe_sql": safe_sql,
        "masked_sql": masked_sql or safe_sql,
        "tables": tables,
        "sensitive_columns": sensitive_columns,
        "dialect": dialect,
        "issues": issues,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate generated SQL for NL2SQL safety rules.")
    parser.add_argument("sql", help="SQL statement to validate.")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH, help="Schema catalog JSON for allowed tables.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Default row limit to add when missing.")
    parser.add_argument(
        "--dialect",
        choices=["mysql", "tsql"],
        default=None,
        help="Force SQL dialect for row limiting (default: inferred from schema catalog per table).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = validate_sql(
        args.sql,
        allowed_tables=load_allowed_tables(args.schema),
        default_limit=args.limit,
        dialect=args.dialect,
        table_dialects=load_table_dialects(args.schema),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()