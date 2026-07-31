"""소스에 하드코딩된 물리 테이블·컬럼 바인딩을 센다.

    python tools/physical_binding_inventory.py            # 목록 출력
    python tools/physical_binding_inventory.py --json     # 기계 판독용

배경: 타겟팅 대상 실DB 는 계속 바뀔 수 있고, 그래서 스키마 지식은 설정(member_target_filters.json /
schema_catalog.json)이 소유하는 것이 목표다. 그런데 실제로는 테이블·컬럼 이름이 소스 곳곳에
문자열 리터럴로 남아 있다 — 이 도구는 그 부채를 **세는 장치**다.

세지 않으면 줄지 않는다. 이관을 하더라도 다른 곳에서 다시 늘어나는 것을 막을 수 없기 때문에,
tests/test_physical_binding_ratchet.py 가 이 수치를 하향 전용 래칫으로 묶는다.

판정 방식: 카탈로그에 실재하는 테이블/컬럼 이름과 **정확히 일치하는 문자열 리터럴**만 센다.
이름이 우연히 겹치는 일반 단어를 세지 않으려고 대문자+언더스코어 형태로 한정한다.
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
SKIP_FILES = {
    "db_swap_preflight.py",       # 카탈로그 대조 도구 자신
    "schema_extract.py",          # 스키마를 읽어 카탈로그를 만드는 도구
    "build_member_value_index.py",
    "physical_binding_inventory.py",
}
SKIP_DIR_PARTS = {".git", "__pycache__", ".pytest_cache", "tests", "docs", "node_modules"}

PHYSICAL_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


def _catalog_names() -> tuple[set[str], set[str]]:
    catalog = json.loads((ROOT / "docs" / "data" / "schema_catalog.json").read_text(encoding="utf-8"))
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
    return files


def scan() -> dict:
    tables, columns = _catalog_names()
    hits: list[dict] = []
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value.strip()
            if not PHYSICAL_NAME_RE.match(value):
                continue
            kind = "table" if value in tables else ("column" if value in columns else None)
            if kind is None:
                continue
            hits.append(
                {
                    "file": path.relative_to(ROOT).as_posix(),
                    "line": node.lineno,
                    "name": value,
                    "kind": kind,
                }
            )

    per_file: dict[str, int] = {}
    for hit in hits:
        per_file[hit["file"]] = per_file.get(hit["file"], 0) + 1
    return {
        "total": len(hits),
        "tables": sum(1 for hit in hits if hit["kind"] == "table"),
        "columns": sum(1 for hit in hits if hit["kind"] == "column"),
        "per_file": dict(sorted(per_file.items(), key=lambda item: -item[1])),
        "hits": hits,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="기계 판독용 JSON 출력")
    args = parser.parse_args()

    result = scan()
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "hits"}, ensure_ascii=False, indent=2))
        return 0

    print(f"소스 하드코딩 물리 바인딩: {result['total']}건 (테이블 {result['tables']} / 컬럼 {result['columns']})")
    print()
    for file, count in result["per_file"].items():
        print(f"  {count:4d}  {file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
