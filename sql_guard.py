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


def validate_sql(
    sql: str,
    allowed_tables: set[str] | None = None,
    default_limit: int = DEFAULT_LIMIT,
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

    safe_sql = _ensure_limit(statement, default_limit) if is_select else statement
    if is_select and safe_sql != statement:
        issues.append(_issue("limit_added", "warning", f"Default LIMIT {default_limit} was added."))

    if is_select and not re.search(r"\bwhere\b", lowered_statement) and not re.search(r"\blimit\b", lowered_statement):
        issues.append(_issue("unbounded_scan", "warning", "Query has no WHERE clause; default LIMIT is required."))

    masked_sql = _mask_sensitive_select_columns(safe_sql)
    is_valid = not any(issue["severity"] == "error" for issue in issues)
    return _result(is_valid, statement, safe_sql, tables, sensitive_columns, issues, masked_sql=masked_sql)


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


def _ensure_limit(sql: str, default_limit: int) -> str:
    if re.search(r"\blimit\s+\d+\b", sql, re.IGNORECASE):
        return sql
    return sql.rstrip().rstrip(";") + f" LIMIT {default_limit}"


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
) -> dict[str, Any]:
    return {
        "is_valid": is_valid,
        "sql": sql,
        "safe_sql": safe_sql,
        "masked_sql": masked_sql or safe_sql,
        "tables": tables,
        "sensitive_columns": sensitive_columns,
        "issues": issues,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate generated SQL for NL2SQL safety rules.")
    parser.add_argument("sql", help="SQL statement to validate.")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH, help="Schema catalog JSON for allowed tables.")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Default LIMIT to add when missing.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = validate_sql(args.sql, allowed_tables=load_allowed_tables(args.schema), default_limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()