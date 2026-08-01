"""계산식(formula) AST 검증·컴파일 엔진.

소유 범위: `formula_ast`(number/column/binary_op 세 노드)를 받아 (1) 숫자형 컬럼과 허용 연산자만
쓰는지 검증하고 (2) 안전한 SQL expression 으로 컴파일한다. AST 를 만드는 쪽은 LLM 파서다
(graph_rag._coerce_llm_computed_metric → source="llm_formula_ast").

2026-07-29: 자연어 문장에서 계산식을 뽑던 규칙 파서(metric_lexicon.sample.json 별칭 사전 +
기호/어구 스캐너)를 제거했다. 이유는 두 가지다. (1) 사전이 이미 비어 있었다(metrics: []).
(2) 컬럼 해석의 관문인 TABLE_ALIASES 가 데모 스키마(users/campaigns) 고정인데 연결된 실 CRM
schema_catalog 에는 그 테이블이 없어 규칙 파서가 항상 빈 결과를 냈다. AST 를 받는 LLM 경로만
남기고 사전·스캐너는 걷어낸다. 실DB에서 계산식을 살리려면 TABLE_ALIASES 를 실 테이블로 넓히면
되고, 자연어 → AST 변환은 LLM 파서가 이미 담당한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_SCHEMA_PATH = Path("docs/data/generated/schema_catalog.json")

# 계산식이 참조할 수 있는 테이블과 SQL 별칭. 여기 없는 테이블의 컬럼은 산술에 쓸 수 없다(검증 실패).
TABLE_ALIASES = {
    "users": "u",
    "campaigns": "c",
}
NUMERIC_TYPE_MARKERS = ("INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL")
ALLOWED_BINARY_OPERATORS = {"+", "-", "*", "/"}


def load_numeric_columns(schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, dict[str, str]]:
    if not schema_path.exists():
        return {}
    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    tables = payload.get("tables", {})
    numeric_columns: dict[str, dict[str, str]] = {}
    if not isinstance(tables, dict):
        return numeric_columns

    for table_name, table_payload in tables.items():
        if table_name not in TABLE_ALIASES:
            continue
        columns = table_payload.get("columns", []) if isinstance(table_payload, dict) else []
        for column in columns:
            if not isinstance(column, dict):
                continue
            column_name = column.get("name")
            column_type = str(column.get("type", "")).upper()
            if isinstance(column_name, str) and any(marker in column_type for marker in NUMERIC_TYPE_MARKERS):
                numeric_columns[f"{table_name}.{column_name}"] = {
                    "table": table_name,
                    "column": column_name,
                    "type": column_type,
                    "alias": TABLE_ALIASES[table_name],
                }
    return numeric_columns


def validate_formula_ast(ast: Any, schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    issues: list[str] = []
    referenced_columns: list[str] = []
    numeric_columns = load_numeric_columns(schema_path)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            issues.append("formula node must be an object")
            return
        node_type = node.get("type")
        if node_type == "number":
            value = node.get("value")
            if not isinstance(value, int | float):
                issues.append("number node requires numeric value")
            return
        if node_type == "column":
            table = node.get("table")
            column = node.get("column")
            column_key = f"{table}.{column}"
            if column_key not in numeric_columns:
                issues.append(f"column is not allowed for arithmetic: {column_key}")
            else:
                referenced_columns.append(column_key)
            return
        if node_type == "binary_op":
            op = node.get("op")
            if op not in ALLOWED_BINARY_OPERATORS:
                issues.append(f"operator is not allowed: {op}")
            visit(node.get("left"))
            visit(node.get("right"))
            return
        issues.append(f"node type is not allowed: {node_type}")

    visit(ast)
    return {
        "is_valid": not issues,
        "issues": issues,
        "referenced_columns": _unique(referenced_columns),
    }


def compile_formula_ast(ast: dict[str, Any], schema_path: Path = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    validation = validate_formula_ast(ast, schema_path=schema_path)
    if not validation["is_valid"]:
        return {
            "is_valid": False,
            "expression_sql": None,
            "referenced_columns": validation["referenced_columns"],
            "referenced_tables": [],
            "issues": validation["issues"],
        }

    numeric_columns = load_numeric_columns(schema_path)

    def compile_node(node: dict[str, Any]) -> str:
        node_type = node["type"]
        if node_type == "number":
            return str(node["value"])
        if node_type == "column":
            column_info = numeric_columns[f"{node['table']}.{node['column']}"]
            return f"{column_info['alias']}.{column_info['column']}"
        left = compile_node(node["left"])
        right = compile_node(node["right"])
        if node["op"] == "/":
            right = f"NULLIF({right}, 0)"
        return f"({left} {node['op']} {right})"

    referenced_tables = _unique([column.split(".", 1)[0] for column in validation["referenced_columns"]])
    return {
        "is_valid": True,
        "expression_sql": compile_node(ast),
        "referenced_columns": validation["referenced_columns"],
        "referenced_tables": referenced_tables,
        "issues": [],
    }


def _unique(values: list[str]) -> list[str]:
    unique_values: list[str] = []
    for value in values:
        if value not in unique_values:
            unique_values.append(value)
    return unique_values
