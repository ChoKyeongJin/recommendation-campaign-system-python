"""소스에 하드코딩된 물리 테이블·컬럼 바인딩을 센다.

    python tools/physical_binding_inventory.py            # 목록 출력
    python tools/physical_binding_inventory.py --json     # 기계 판독용

배경: 타겟팅 대상 실DB 는 계속 바뀔 수 있고, 그래서 스키마 지식은 설정(member_target_filters.json /
schema_catalog.json)이 소유하는 것이 목표다. 그런데 실제로는 테이블·컬럼 이름이 소스 곳곳에
문자열 리터럴로 남아 있다 — 이 도구는 그 부채를 **세는 장치**다.

세지 않으면 줄지 않는다. 이관을 하더라도 다른 곳에서 다시 늘어나는 것을 막을 수 없기 때문에,
tests/test_physical_binding_ratchet.py 가 이 수치를 하향 전용 래칫으로 묶는다.

판정 방식: dict/call/선언 기본값의 명시적 ``table``·``*_table``·``column``·``*_column``·
``columns`` 위치와 등록 모델 생성자, SQL ``FROM``/``JOIN`` 위치만 본다. 이 문맥의 대문자
식별자를 카탈로그에 있는 hit와 카탈로그에 없는 violation으로 나눠, 상태코드·enum 같은 일반
대문자 문자열은 세지 않으면서 오타·유령 테이블은 숨기지 않는다.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 설정/문서/도구는 스키마 지식의 정당한 소유자이거나 소비자가 아니다.
#
# 판단 기준(런타임 소비자인가, 카탈로그 생산자인가): 이 래칫이 재는 부채는 "DB 를 갈아끼울 때
# 소스를 뒤져야 하는가"다. **카탈로그를 만드는 쪽**(schema_extract, build_*)은 스키마 이름을
# 다루는 것이 직무이고, 새 DB 를 향해 다시 실행하면 되는 대상이지 고쳐야 할 부채가 아니다.
# 세면 신호가 흐려진다 — 예를 들어 build_table_relationships.py 한 파일이 전체의 20% 를 차지해
# 런타임 이관 진척을 가렸다.
SKIP_FILES = {
    "db_swap_preflight.py",       # 카탈로그 대조 도구 자신
    "schema_extract.py",          # 스키마를 읽어 카탈로그를 만드는 도구
    "build_member_value_index.py",
    "physical_binding_inventory.py",
    # 아래 셋은 같은 근거의 카탈로그 생산자다(2026-08-02 정의 수정 — 이관 진척이 아니다).
    "build_table_relationships.py",   # FK 큐레이션을 schema_catalog 에 주입하는 생산자
    "build_dimension_catalog.py",     # 디멘션 카탈로그 생산자
    "build_rag_knowledge.py",         # 카탈로그를 읽어 RAG 지식베이스를 만드는 생산자
}
SKIP_DIR_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "docs",
    "node_modules",
    "site-packages",
    "tests",
    "venv",
}

PHYSICAL_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
QUALIFIED_NAME_RE = re.compile(
    r"^(?:\[?[A-Za-z_][A-Za-z0-9_]*\]?\.)*\[?([A-Z][A-Z0-9_]{2,})\]?$"
)
SQL_TABLE_RE = re.compile(
    r"\b(FROM|JOIN)\s+(?!\()"
    r"((?:\[?[A-Za-z_][A-Za-z0-9_]*\]?\.)*\[?[A-Z][A-Z0-9_]{2,}\]?)",
    re.IGNORECASE,
)
SQL_CTE_DECLARATION_RE = re.compile(
    r"(?:\bWITH\s+)?"
    r"((?:\[?[A-Za-z_][A-Za-z0-9_]*\]?\.)*\[?[A-Z][A-Z0-9_]{2,}\]?)"
    r"\s+AS\s*\(",
    re.IGNORECASE,
)
SQL_NON_TABLE_IDENTIFIERS = frozenset({"STDIN", "STDOUT"})

# 일반 키 이름만으로는 물리 바인딩임을 알 수 없는 생성자 필드다. table/*_table,
# column/*_column/columns 는 아래 공통 규칙으로 잡고, 이 표는 ``key``·``left``처럼 해당 모델
# 문맥에서만 컬럼인 이름을 좁게 연다.
CONSTRUCTOR_BINDING_FIELDS: dict[str, dict[str, str]] = {
    "SubjectSpec": {"table": "table", "key": "column"},
    "EventSpec": {
        "table": "table",
        "subject_key": "column",
        "event_subject_key": "column",
        "time_column": "column",
    },
    "FieldSpec": {"column": "column"},
    "JoinPath": {"source_table": "table", "target_table": "table"},
    "JoinCondition": {"left": "column", "right": "column"},
}
CONSTRUCTOR_POSITIONAL_BINDINGS: dict[str, dict[int, str]] = {
    "SubjectSpec": {0: "table", 2: "column"},
    "EventSpec": {0: "table", 2: "column", 3: "column", 4: "column"},
    "FieldSpec": {1: "column"},
    "JoinPath": {1: "table", 3: "table"},
    "JoinCondition": {0: "column", 1: "column"},
}


def _catalog_names() -> tuple[set[str], set[str]]:
    catalog = json.loads(
        (ROOT / "docs" / "data" / "generated" / "schema_catalog.json").read_text(encoding="utf-8")
    )
    tables = catalog.get("tables") or {}
    table_names: set[str] = set()
    column_names: set[str] = set()
    entries = tables.items() if isinstance(tables, dict) else ((t.get("name"), t) for t in tables)
    for name, table in entries:
        if isinstance(name, str):
            table_names.add(name)
        columns = (table or {}).get("columns") or []
        for column in columns:
            column_name = column.get("name") if isinstance(column, dict) else column
            if isinstance(column_name, str):
                column_names.add(column_name)
    return table_names, column_names


def _source_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if SKIP_DIR_PARTS & set(path.parts) or path.name in SKIP_FILES:
            continue
        files.append(path)
    return sorted(files)


def _binding_kind(name: str | None) -> str | None:
    if not name:
        return None
    normalized = name.casefold()
    if normalized == "table" or normalized.endswith("_table"):
        return "table"
    if normalized in {"column", "columns"} or normalized.endswith(("_column", "_columns")):
        return "column"
    return None


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _target_names(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (node.attr,)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(name for item in node.elts for name in _target_names(item))
    return ()


def _literal_strings(node: ast.AST) -> list[ast.Constant]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [literal for item in node.elts for literal in _literal_strings(item)]
    if isinstance(node, ast.Dict):
        literals: list[ast.Constant] = []
        for key, value in zip(node.keys, node.values):
            if key is not None:
                literals.extend(_literal_strings(key))
            literals.extend(_literal_strings(value))
        return literals
    return []


def _physical_identifier(value: str) -> str | None:
    match = QUALIFIED_NAME_RE.fullmatch(value.strip())
    if match is None:
        return None
    name = match.group(1)
    return name if PHYSICAL_NAME_RE.fullmatch(name) else None


class _BindingVisitor(ast.NodeVisitor):
    def __init__(self, cte_names: set[str], docstring_nodes: set[int]) -> None:
        self.candidates: list[dict[str, object]] = []
        self._class_names: list[str] = []
        self._cte_names = cte_names
        self._docstring_nodes = docstring_nodes

    def _record(self, node: ast.AST, name: str, kind: str, source: str) -> None:
        self.candidates.append(
            {
                "line": getattr(node, "lineno", 0),
                "name": name,
                "kind": kind,
                "source": source,
            }
        )

    def _record_value(self, node: ast.AST, kind: str, source: str) -> None:
        for literal in _literal_strings(node):
            name = _physical_identifier(str(literal.value))
            if name is not None:
                self._record(literal, name, kind, source)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast visitor API
        self._class_names.append(node.name)
        self.generic_visit(node)
        self._class_names.pop()

    def visit_Dict(self, node: ast.Dict) -> None:  # noqa: N802 - ast visitor API
        for key, value in zip(node.keys, node.values):
            if not (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
            ):
                continue
            kind = _binding_kind(key.value)
            if kind is not None:
                self._record_value(value, kind, f"dict_key:{key.value}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast visitor API
        constructor = _call_name(node.func)
        model_fields = CONSTRUCTOR_BINDING_FIELDS.get(constructor or "", {})
        for keyword in node.keywords:
            if keyword.arg is None:
                continue
            kind = model_fields.get(keyword.arg) or _binding_kind(keyword.arg)
            if kind is not None:
                self._record_value(
                    keyword.value,
                    kind,
                    f"call_keyword:{constructor or '<call>'}.{keyword.arg}",
                )
        positional = CONSTRUCTOR_POSITIONAL_BINDINGS.get(constructor or "", {})
        for index, kind in positional.items():
            if index < len(node.args):
                self._record_value(
                    node.args[index],
                    kind,
                    f"call_positional:{constructor}[{index}]",
                )
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802 - ast visitor API
        if node.value is not None:
            for name in _target_names(node.target):
                model_fields = (
                    CONSTRUCTOR_BINDING_FIELDS.get(self._class_names[-1], {})
                    if self._class_names
                    else {}
                )
                kind = model_fields.get(name) or _binding_kind(name)
                if kind is not None:
                    self._record_value(node.value, kind, f"annotated_assignment:{name}")
        self.generic_visit(node)

    def _visit_function_defaults(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        positional_args = [*node.args.posonlyargs, *node.args.args]
        default_args = positional_args[-len(node.args.defaults):] if node.args.defaults else []
        for argument, default in zip(default_args, node.args.defaults):
            kind = _binding_kind(argument.arg)
            if kind is not None:
                self._record_value(default, kind, f"function_default:{argument.arg}")
        for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if default is None:
                continue
            kind = _binding_kind(argument.arg)
            if kind is not None:
                self._record_value(default, kind, f"function_default:{argument.arg}")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - ast visitor API
        self._visit_function_defaults(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(  # noqa: N802 - ast visitor API
        self,
        node: ast.AsyncFunctionDef,
    ) -> None:
        self._visit_function_defaults(node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802 - ast visitor API
        if id(node) in self._docstring_nodes or not isinstance(node.value, str):
            return
        for match in SQL_TABLE_RE.finditer(node.value):
            name = _physical_identifier(match.group(2))
            if (
                name is not None
                and name not in self._cte_names
                and name not in SQL_NON_TABLE_IDENTIFIERS
            ):
                self._record(
                    node,
                    name,
                    "table",
                    f"sql_fragment:{match.group(1).upper()}",
                )


def _docstring_nodes(tree: ast.AST) -> set[int]:
    nodes: set[int] = set()
    for owner in ast.walk(tree):
        if not isinstance(
            owner,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ) or not owner.body:
            continue
        first = owner.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            nodes.add(id(first.value))
    return nodes


def _sql_cte_names(tree: ast.AST, docstring_nodes: set[int]) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            id(node) in docstring_nodes
            or not isinstance(node, ast.Constant)
            or not isinstance(node.value, str)
        ):
            continue
        for match in SQL_CTE_DECLARATION_RE.finditer(node.value):
            name = _physical_identifier(match.group(1))
            if name is not None:
                names.add(name)
    return names


def _binding_candidates(tree: ast.AST) -> list[dict[str, object]]:
    docstring_nodes = _docstring_nodes(tree)
    visitor = _BindingVisitor(
        _sql_cte_names(tree, docstring_nodes),
        docstring_nodes,
    )
    visitor.visit(tree)
    # 한 줄에서 같은 심볼을 양쪽 조인키처럼 두 번 쓰는 경우는 파일:줄:심볼 인벤토리 한 건이다.
    # 이 중복 제거는 서로 다른 kind(table/column)나 서로 다른 줄의 실제 반복은 보존한다.
    unique: list[dict[str, object]] = []
    seen: set[tuple[object, object, object]] = set()
    for candidate in visitor.candidates:
        identity = (candidate["line"], candidate["name"], candidate["kind"])
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(candidate)
    return unique


def scan() -> dict:
    tables, columns = _catalog_names()
    hits: list[dict] = []
    violations: list[dict] = []
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        relative_path = path.relative_to(ROOT).as_posix()
        for candidate in _binding_candidates(tree):
            pool = tables if candidate["kind"] == "table" else columns
            item = {"file": relative_path, **candidate}
            if candidate["name"] in pool:
                hits.append(item)
            else:
                violations.append({**item, "reason": "not_in_schema_catalog"})

    per_file: dict[str, int] = {}
    for hit in hits:
        per_file[hit["file"]] = per_file.get(hit["file"], 0) + 1
    unknown_per_file: dict[str, int] = {}
    for violation in violations:
        unknown_per_file[violation["file"]] = unknown_per_file.get(violation["file"], 0) + 1
    return {
        "total": len(hits),
        "tables": sum(1 for hit in hits if hit["kind"] == "table"),
        "columns": sum(1 for hit in hits if hit["kind"] == "column"),
        "per_file": dict(sorted(per_file.items(), key=lambda item: -item[1])),
        "unknown_total": len(violations),
        "unknown_per_file": dict(
            sorted(unknown_per_file.items(), key=lambda item: -item[1])
        ),
        "hits": hits,
        "violations": violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="기계 판독용 JSON 출력")
    args = parser.parse_args()

    result = scan()
    if args.json:
        # 기존 baseline 생성/소비자의 JSON schema는 유지한다. 상세 hit/violation은 scan() API와
        # 사람이 읽는 출력에 남기고, JSON CLI는 기존 네 필드만 직렬화한다.
        summary = {
            key: result[key]
            for key in ("total", "tables", "columns", "per_file")
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if result["violations"] else 0

    print(f"소스 하드코딩 물리 바인딩: {result['total']}건 (테이블 {result['tables']} / 컬럼 {result['columns']})")
    print()
    for file, count in result["per_file"].items():
        print(f"  {count:4d}  {file}")
    if result["violations"]:
        print()
        print(f"카탈로그 미등록 물리 바인딩: {result['unknown_total']}건")
        for violation in result["violations"]:
            print(
                f"  {violation['file']}:{violation['line']} "
                f"{violation['kind']} {violation['name']} ({violation['source']})"
            )
    return 1 if result["violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
