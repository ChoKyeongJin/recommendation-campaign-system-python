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


# 조인키 타입군. 서로 다른 군끼리 등호 조인하면 실행 자체가 안 되거나(암묵 변환 실패) 조용히 0건이 된다.
# 예: LLM 폴백이 만든 ODS_MALL_OMS_CART.CART_ID(nvarchar) = CRM_MB_BASEINFO.MEMBER_NO(bigint) 는
# "Error converting data type nvarchar to bigint" 로 실행 실패한다(올바른 짝은 MEMBER_ID).
_TYPE_FAMILIES = {
    "numeric": ("bigint", "int", "smallint", "tinyint", "decimal", "numeric", "float", "real", "money", "bit"),
    "string": ("nvarchar", "varchar", "nchar", "char", "ntext", "text"),
    "datetime": ("datetime2", "datetime", "smalldatetime", "date", "time", "timestamp"),
    "binary": ("varbinary", "binary", "image"),
}
# FROM/JOIN 뒤 별칭 자리에 올 수 있는 예약어(별칭 없는 테이블을 별칭으로 오인하지 않도록 제외).
_ALIAS_STOPWORDS = {
    "where", "on", "inner", "left", "right", "full", "cross", "outer", "join", "group",
    "order", "having", "union", "select", "as", "and", "or", "not", "exists", "with",
}
# 별칭 자리에 예약어가 오면 아예 매칭하지 않는다(부정 전방탐색). 그냥 잡아서 나중에 거르면
# "FROM T1 JOIN T2 ON ..." 에서 'JOIN' 을 T1 의 별칭으로 소비해 버려 T2 를 못 찾는다.
_TABLE_ALIAS_PATTERN = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)"
    r"(?:\s+(?:AS\s+)?(?!(?:" + "|".join(sorted(_ALIAS_STOPWORDS)) + r")\b)([A-Za-z_][\w]*))?",
    re.IGNORECASE,
)
_EQUI_JOIN_PATTERN = re.compile(r"\b([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\s*=\s*([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)")


def _type_family(raw_type: str) -> str | None:
    """'nvarchar(100)' -> 'string', 'bigint' -> 'numeric'. 모르는 타입은 None(판정 보류)."""
    base = re.split(r"[\s(]", (raw_type or "").strip().casefold(), maxsplit=1)[0]
    for family, types in _TYPE_FAMILIES.items():
        if base in types:
            return family
    return None


def load_column_types(schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, dict[str, str]]:
    """스키마 카탈로그에서 {테이블: {컬럼: 타입군}} 을 만든다(타입군 판별 실패 컬럼은 제외)."""
    if not schema_path.exists():
        return {}
    try:
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    tables = payload.get("tables", {})
    if not isinstance(tables, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for table, meta in tables.items():
        if not isinstance(meta, dict):
            continue
        columns = {}
        for column in meta.get("columns", []):
            if not isinstance(column, dict) or not column.get("name"):
                continue
            family = _type_family(str(column.get("type", "")))
            if family:
                columns[str(column["name"]).casefold()] = family
        result[str(table).casefold()] = columns
    return result


def _alias_map(sql: str, column_types: dict[str, dict[str, str]]) -> dict[str, str]:
    """SQL 의 별칭/테이블명 -> 실테이블명(소문자). 스키마에 있는 테이블만 담는다."""
    aliases: dict[str, str] = {}
    for match in _TABLE_ALIAS_PATTERN.finditer(sql):
        table = match.group(1).casefold()
        if table not in column_types:
            continue
        aliases[table] = table
        alias = match.group(2)
        if alias and alias.casefold() not in _ALIAS_STOPWORDS:
            aliases[alias.casefold()] = table
    return aliases


def load_join_key_registry(schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[tuple[str, str, str], set[str]]:
    """검증된(verified) 조인 관계를 {(출발테이블, 출발컬럼, 상대테이블): {허용 상대컬럼}} 으로 만든다.

    schema_catalog.foreign_keys 는 build_table_relationships.py 가 큐레이션한 관계다(CRMDW 는 선언 FK 가
    없어 사람이 확인한 것만 verified 로 들어간다). 양방향으로 등록해 조인을 어느 쪽으로 쓰든 잡는다.
    confidence=verified 만 강제한다 — inferred/human_hint 까지 강제하면 추정이 틀렸을 때 정상 SQL 을 막는다."""
    if not schema_path.exists():
        return {}
    try:
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    tables = payload.get("tables", {})
    if not isinstance(tables, dict):
        return {}
    registry: dict[tuple[str, str, str], set[str]] = {}

    def register(from_table: str, from_column: str, to_table: str, to_column: str) -> None:
        registry.setdefault((from_table, from_column, to_table), set()).add(to_column)

    for table, meta in tables.items():
        if not isinstance(meta, dict):
            continue
        for fk in meta.get("foreign_keys", []) or []:
            if not isinstance(fk, dict) or fk.get("confidence") != "verified":
                continue
            reference = fk.get("references") or {}
            target_table = str(reference.get("table", "")).casefold()
            columns, target_columns = fk.get("columns") or [], reference.get("columns") or []
            if not target_table or len(columns) != 1 or len(target_columns) != 1:
                continue  # 복합키는 컬럼 단위 등호 비교로 판정할 수 없으므로 보류한다.
            source_table = str(table).casefold()
            source_column, target_column = str(columns[0]).casefold(), str(target_columns[0]).casefold()
            register(source_table, source_column, target_table, target_column)
            register(target_table, target_column, source_table, source_column)
    return registry


def validate_join_keys(
    sql: str,
    column_types: dict[str, dict[str, str]],
    join_registry: dict[tuple[str, str, str], set[str]] | None = None,
) -> dict[str, Any]:
    """등호 조인이 (1) 타입군이 맞는지 (2) 검증된 조인키를 쓰는지 검사한다(위반은 error).

    기존 sql_guard 는 테이블 허용목록·SELECT 전용만 봐서 '문법은 맞고 의미가 틀린' 조인을 통과시켰다.
    실제로 LLM 폴백이 ODS_MALL_OMS_CART.CART_ID(nvarchar) = CRM_MB_BASEINFO.MEMBER_NO(bigint) 를 만들어
    실행 실패(nvarchar→bigint 변환 오류)하는 SQL 이 success 로 나갔다.

    (1) 타입군 검사는 nvarchar↔bigint 처럼 실행조차 안 되는 조인을 잡고, (2) 조인키 검사는 타입은 같지만
    엉뚱한 컬럼에 붙인 조인(CART_ID = 다른 문자열 컬럼)을 잡는다 — CART_ID 의 상대는 MEMBER_ID 뿐이다.
    스키마에 타입/관계 정보가 없는 컬럼은 둘 다 판정을 보류한다(오탐으로 정상 SQL 을 막지 않기 위해)."""
    issues: list[dict[str, str]] = []
    aliases = _alias_map(sql, column_types)
    registry = join_registry or {}
    for match in _EQUI_JOIN_PATTERN.finditer(sql):
        left_alias, left_column, right_alias, right_column = (part.casefold() for part in match.groups())
        left_table, right_table = aliases.get(left_alias), aliases.get(right_alias)
        if not left_table or not right_table:
            continue
        left_family = column_types.get(left_table, {}).get(left_column)
        right_family = column_types.get(right_table, {}).get(right_column)
        if left_family and right_family and left_family != right_family:
            issues.append(
                {
                    "code": "join_key_type_mismatch",
                    "severity": "error",
                    "message": (
                        f"조인키 타입 불일치: {left_table.upper()}.{left_column.upper()}({left_family}) = "
                        f"{right_table.upper()}.{right_column.upper()}({right_family})"
                    ),
                }
            )
            continue
        # 한쪽이라도 상대 테이블에 대한 검증된 조인키를 갖고 있으면 그 컬럼으로만 조인해야 한다.
        for table, column, other_table, other_column in (
            (left_table, left_column, right_table, right_column),
            (right_table, right_column, left_table, left_column),
        ):
            expected = registry.get((table, column, other_table))
            if expected and other_column not in expected:
                issues.append(
                    {
                        "code": "join_key_not_verified",
                        "severity": "error",
                        "message": (
                            f"검증되지 않은 조인키: {table.upper()}.{column.upper()} 는 "
                            f"{other_table.upper()}.{'/'.join(sorted(name.upper() for name in expected))} 와 조인해야 한다"
                            f"({other_table.upper()}.{other_column.upper()} 아님)"
                        ),
                    }
                )
                break
    return {"is_valid": not issues, "issues": issues}


# 집계 함수(윈도 함수 OVER 절 없이 쓰이면 grain 을 접는다). 괄호 내용을 blank 처리한 뒤에는
# 'COUNT()' 형태로 남으므로 이름 뒤 빈 괄호로 탐지한다.
_AGG_FUNCS = ("count", "sum", "avg", "min", "max")
_AGG_CALL_PATTERN = re.compile(r"\b(" + "|".join(_AGG_FUNCS) + r")\s*\(\)", re.IGNORECASE)
_BARE_COLUMN_PATTERN = re.compile(r"\b[A-Za-z_]\w*\.[A-Za-z_]\w*")


def _blank_parens(sql: str) -> str:
    """모든 최상위 괄호의 내용을 지워 빈 괄호만 남긴다(중첩 포함).

    서브쿼리(EXISTS (…)/IN (…))와 함수 인자(COUNT(…)/TRY_CAST(…))의 내부를 제거해, 바깥(outer)
    질의의 구조만 정규식으로 안전하게 분석하기 위한 전처리다. 예: 'COUNT(DISTINCT ORDER_ID)' -> 'COUNT()',
    'EXISTS (SELECT 1 …)' -> 'EXISTS ()'. 이렇게 하면 서브쿼리 안의 집계를 바깥 집계로 오인하지 않는다."""
    out: list[str] = []
    depth = 0
    for ch in sql:
        if ch == "(":
            if depth == 0:
                out.append("()")  # 최상위 괄호 쌍은 빈 괄호로 보존(함수/서브쿼리 존재 표시)
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def validate_analytics_shape(sql: str) -> dict[str, Any]:
    """바깥(outer) 질의의 집계·grain 구조가 흔한 정합성 오류를 범하지 않는지 정적으로 검사한다.

    허용목록·조인키 가드가 못 잡는 '문법은 그럴듯하나 집계/grain 이 틀린' SQL(특히 LLM 폴백)을 겨냥한다.
    괄호 내용을 지운 outer 스켈레톤만 보므로 서브쿼리(EXISTS/IN)와 함수 인자는 오탐 없이 제외된다.
    확실한 오류만 error(후보 탈락), 의심스러운 형태는 warning(고지만)으로 둔다 — 정적 분석의 한계상
    의미적 항목(기준집합 동일성·재집계·연산 grain)은 여기서 판정하지 않고 생성 프롬프트 지침으로 남긴다.

    검사 항목:
      - agg_in_where(error): WHERE 절에 집계 함수 — SQL 문법상 불가(집계 후 필터는 HAVING). [집계 전/후 필터 구분]
      - agg_without_group_by(error): SELECT 에 집계 컬럼과 비집계 컬럼이 GROUP BY 없이 혼용. [집계/비집계 혼용]
      - distinct_with_aggregate(warning): SELECT DISTINCT 와 집계 혼용 — DISTINCT 가 중복/그레인 문제를 가릴 수 있음. [DISTINCT 은폐]
      - join_without_grain_control(warning): outer 조인이 있는데 DISTINCT/GROUP BY 가 없음 — 1:N 조인이 결과 행을 부풀릴 수 있음. [1:N 중복/그레인]
    """
    issues: list[dict[str, str]] = []
    if not isinstance(sql, str) or not sql.strip():
        return {"is_valid": True, "issues": issues}
    outer = _blank_parens(sql)

    select_match = re.search(r"\bSELECT\b(.*?)\bFROM\b", outer, re.IGNORECASE | re.DOTALL)
    select_list = select_match.group(1) if select_match else ""
    has_distinct = re.search(r"\bSELECT\s+DISTINCT\b", outer, re.IGNORECASE) is not None
    has_group_by = re.search(r"\bGROUP\s+BY\b", outer, re.IGNORECASE) is not None
    has_join = re.search(r"\bJOIN\b", outer, re.IGNORECASE) is not None
    select_has_agg = _AGG_CALL_PATTERN.search(select_list) is not None
    # DISTINCT 키워드는 비집계 컬럼 판정에서 제외한다(SELECT DISTINCT 의 DISTINCT 는 컬럼이 아님).
    select_wo_agg = _AGG_CALL_PATTERN.sub("", select_list)
    select_has_bare_column = _BARE_COLUMN_PATTERN.search(select_wo_agg) is not None

    where_match = re.search(
        r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|$)", outer, re.IGNORECASE | re.DOTALL
    )
    if where_match and _AGG_CALL_PATTERN.search(where_match.group(1)):
        issues.append(
            {
                "code": "agg_in_where",
                "severity": "error",
                "message": "WHERE 절에 집계 함수가 있다 — 집계 후 조건은 HAVING 으로 분리해야 한다(집계 전/후 필터 구분).",
            }
        )
    if select_has_agg and select_has_bare_column and not has_group_by:
        issues.append(
            {
                "code": "agg_without_group_by",
                "severity": "error",
                "message": "SELECT 에 집계 컬럼과 비집계 컬럼이 GROUP BY 없이 혼용됐다 — 비집계 컬럼을 GROUP BY 에 넣거나 집계를 서브쿼리로 분리해야 한다.",
            }
        )
    if has_distinct and select_has_agg:
        issues.append(
            {
                "code": "distinct_with_aggregate",
                "severity": "warning",
                "message": "SELECT DISTINCT 와 집계 함수가 함께 쓰였다 — DISTINCT 가 잘못된 조인의 행 중복을 가리고 있는지 확인하라.",
            }
        )
    if has_join and not has_distinct and not has_group_by:
        issues.append(
            {
                "code": "join_without_grain_control",
                "severity": "warning",
                "message": "outer 조인이 있는데 DISTINCT/GROUP BY 로 grain 을 통제하지 않는다 — 1:N 조인이 결과 행(회원)을 중복시킬 수 있다.",
            }
        )
    return {"is_valid": not any(issue["severity"] == "error" for issue in issues), "issues": issues}


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