from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


DEFAULT_SCHEMA_PATH = Path("docs/data/schema_catalog.json")
DEFAULT_METRIC_LEXICON_PATH = Path("docs/data/metric_lexicon.sample.json")

TABLE_ALIASES = {
    "users": "u",
    "campaigns": "c",
}
NUMERIC_TYPE_MARKERS = ("INT", "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE", "REAL")
ALLOWED_BINARY_OPERATORS = {"+", "-", "*", "/"}
RANK_DESC_TERMS = ("가장높", "높은", "상위", "큰", "많은", "top", "highest", "largest")
RANK_ASC_TERMS = ("가장낮", "낮은", "하위", "작은", "적은", "lowest", "smallest")


def parse_computed_metrics_from_query(
    query: str,
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    metric_lexicon_path: Path = DEFAULT_METRIC_LEXICON_PATH,
) -> list[dict[str, Any]]:
    catalog = load_formula_catalog(schema_path, metric_lexicon_path)
    parsed = _parse_symbol_formula(query, catalog) or _parse_phrase_formula(query, catalog)
    if parsed is None:
        return []

    behavior, order_by = _infer_formula_behavior(query)
    metric = {
        "metric_id": "computed_formula_score",
        "ko_label": "계산 점수",
        "formula_text": parsed["formula_text"],
        "formula_ast": parsed["formula_ast"],
        "sql_behavior": behavior,
        "operator": None,
        "threshold": None,
        "order_by": order_by,
        "unit": None,
        "confidence": parsed["confidence"],
        "requires_clarification": False,
        "clarification_question": None,
        "source": parsed["source"],
    }
    validation = validate_formula_ast(metric["formula_ast"], schema_path=schema_path)
    if not validation["is_valid"]:
        metric["requires_clarification"] = True
        metric["clarification_question"] = "계산식에 사용할 수 없는 컬럼이나 연산자가 포함되어 있습니다: " + "; ".join(validation["issues"])
    return [metric]


def load_formula_catalog(
    schema_path: Path = DEFAULT_SCHEMA_PATH,
    metric_lexicon_path: Path = DEFAULT_METRIC_LEXICON_PATH,
) -> dict[str, Any]:
    numeric_columns = load_numeric_columns(schema_path)
    terms: list[dict[str, Any]] = []

    for entry in _load_metric_lexicon(metric_lexicon_path):
        table = entry.get("table")
        column = entry.get("column")
        column_key = f"{table}.{column}"
        if column_key not in numeric_columns:
            continue
        aliases = [entry.get("canonical"), entry.get("ko_label"), *entry.get("synonyms", [])]
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                terms.append(
                    {
                        "term": alias,
                        "compact_term": _compact(alias),
                        "column_key": column_key,
                        "table": table,
                        "column": column,
                    }
                )

    for column_key, column_info in numeric_columns.items():
        for alias in (column_info["column"], column_key):
            terms.append(
                {
                    "term": alias,
                    "compact_term": _compact(alias),
                    "column_key": column_key,
                    "table": column_info["table"],
                    "column": column_info["column"],
                }
            )

    terms.sort(key=lambda item: len(item["compact_term"]), reverse=True)
    return {"numeric_columns": numeric_columns, "terms": terms}


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


def _load_metric_lexicon(metric_lexicon_path: Path) -> list[dict[str, Any]]:
    if not metric_lexicon_path.exists():
        return []
    payload = json.loads(metric_lexicon_path.read_text(encoding="utf-8"))
    metrics = payload.get("metrics", [])
    return [metric for metric in metrics if isinstance(metric, dict)]


def _parse_symbol_formula(query: str, catalog: dict[str, Any]) -> dict[str, Any] | None:
    tokens = _scan_formula_tokens(query, catalog)
    if not any(token["kind"] == "op" for token in tokens):
        return None
    ast = _tokens_to_ast(tokens)
    if ast is None:
        return None
    return {"formula_text": query, "formula_ast": ast, "confidence": 0.72, "source": "rules_formula_symbols"}


def _parse_phrase_formula(query: str, catalog: dict[str, Any]) -> dict[str, Any] | None:
    compact_query = _compact(query)
    occurrences = []
    seen_column_keys = set()
    for term in catalog["terms"]:
        compact_term = term["compact_term"]
        if not compact_term:
            continue
        index = compact_query.find(compact_term)
        if index < 0 or term["column_key"] in seen_column_keys:
            continue
        occurrences.append((index, term))
        seen_column_keys.add(term["column_key"])
    occurrences.sort(key=lambda item: item[0])
    if len(occurrences) < 2:
        return None

    op = None
    if any(marker in compact_query for marker in ("곱", "곱한", "곱해서", "乘")):
        op = "*"
    elif any(marker in compact_query for marker in ("더", "더한", "합", "플러스")):
        op = "+"
    elif any(marker in compact_query for marker in ("빼", "뺀", "차이", "마이너스")):
        op = "-"
    elif any(marker in compact_query for marker in ("나누", "나눈", "분의")):
        op = "/"
    if op is None:
        return None

    left = _column_ast(occurrences[0][1])
    right = _column_ast(occurrences[1][1])
    return {
        "formula_text": query,
        "formula_ast": {"type": "binary_op", "op": op, "left": left, "right": right},
        "confidence": 0.62,
        "source": "rules_formula_phrases",
    }


def _scan_formula_tokens(query: str, catalog: dict[str, Any]) -> list[dict[str, Any]]:
    compact_query = _compact(query)
    tokens: list[dict[str, Any]] = []
    index = 0
    while index < len(compact_query):
        char = compact_query[index]
        if char in ALLOWED_BINARY_OPERATORS:
            tokens.append({"kind": "op", "value": char})
            index += 1
            continue
        if char in "()":
            tokens.append({"kind": "paren", "value": char})
            index += 1
            continue
        number_match = re.match(r"\d+(?:\.\d+)?", compact_query[index:])
        if number_match:
            number_text = number_match.group(0)
            value: int | float = float(number_text) if "." in number_text else int(number_text)
            tokens.append({"kind": "operand", "ast": {"type": "number", "value": value}})
            index += len(number_text)
            continue
        term_match = next((term for term in catalog["terms"] if compact_query.startswith(term["compact_term"], index)), None)
        if term_match is not None:
            tokens.append({"kind": "operand", "ast": _column_ast(term_match)})
            index += len(term_match["compact_term"])
            continue
        index += 1
    return _dedupe_adjacent_operands(tokens)


def _tokens_to_ast(tokens: list[dict[str, Any]]) -> dict[str, Any] | None:
    output: list[dict[str, Any]] = []
    operators: list[str] = []
    precedence = {"+": 1, "-": 1, "*": 2, "/": 2}

    for token in tokens:
        if token["kind"] == "operand":
            output.append(token["ast"])
        elif token["kind"] == "op":
            while operators and operators[-1] != "(" and precedence[operators[-1]] >= precedence[token["value"]]:
                _reduce_operator(output, operators.pop())
            operators.append(token["value"])
        elif token["kind"] == "paren" and token["value"] == "(":
            operators.append("(")
        elif token["kind"] == "paren" and token["value"] == ")":
            while operators and operators[-1] != "(":
                _reduce_operator(output, operators.pop())
            if operators and operators[-1] == "(":
                operators.pop()

    while operators:
        op = operators.pop()
        if op == "(":
            return None
        _reduce_operator(output, op)

    return output[0] if len(output) == 1 else None


def _reduce_operator(output: list[dict[str, Any]], op: str) -> None:
    if len(output) < 2:
        return
    right = output.pop()
    left = output.pop()
    output.append({"type": "binary_op", "op": op, "left": left, "right": right})


def _column_ast(term: dict[str, Any]) -> dict[str, str]:
    return {"type": "column", "table": term["table"], "column": term["column"]}


def _dedupe_adjacent_operands(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    for token in tokens:
        if deduped and token["kind"] == "operand" and deduped[-1]["kind"] == "operand" and token["ast"] == deduped[-1]["ast"]:
            continue
        deduped.append(token)
    return deduped


def _infer_formula_behavior(query: str) -> tuple[str, str | None]:
    compact_query = _compact(query)
    if any(term in compact_query for term in RANK_ASC_TERMS):
        return "rank", "asc"
    if any(term in compact_query for term in RANK_DESC_TERMS):
        return "rank", "desc"
    return "select", None


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value.casefold())


def _unique(values: list[str]) -> list[str]:
    unique_values: list[str] = []
    for value in values:
        if value not in unique_values:
            unique_values.append(value)
    return unique_values


def _self_test() -> dict[str, Any]:
    query = "(평균주문금액 + 구매횟수) * 구매횟수 - 최근활동일 점수가 높은 고객"
    metrics = parse_computed_metrics_from_query(query)
    if not metrics:
        raise AssertionError("expected computed metric")
    compiled = compile_formula_ast(metrics[0]["formula_ast"])
    if not compiled["is_valid"]:
        raise AssertionError(compiled["issues"])
    expected_terms = ["u.avg_order_value_krw", "u.purchase_count_90d", "u.last_active_days"]
    if not all(term in compiled["expression_sql"] for term in expected_terms):
        raise AssertionError(compiled["expression_sql"])
    return {"metrics": metrics, "compiled": compiled}


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse and compile safe arithmetic formula ASTs.")
    parser.add_argument("query", nargs="?", help="Natural language query that may contain a formula.")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA_PATH)
    parser.add_argument("--metric-lexicon", type=Path, default=DEFAULT_METRIC_LEXICON_PATH)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        print(json.dumps(_self_test(), ensure_ascii=False, indent=2))
        return
    if not args.query:
        parser.error("query is required unless --self-test is set")
    metrics = parse_computed_metrics_from_query(args.query, schema_path=args.schema, metric_lexicon_path=args.metric_lexicon)
    for metric in metrics:
        metric["compiled"] = compile_formula_ast(metric["formula_ast"], schema_path=args.schema)
    print(json.dumps({"computed_metrics": metrics}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()