"""DB 스왑 프리플라이트 — 새 DB로 갈아끼운 뒤 "소스 무수정"이 성립하는지 검증한다.

배경(docs/operations/db_swap_runbook.md, db_portability_audit.md): 타겟팅 대상 실DB는 계속
바뀔 수 있고, 스키마 지식은 member_target_filters.json / member_metrics.json 레지스트리 +
schema_catalog.json 이 소유한다. DB 스왑의 1순위 실패모드는 **레지스트리가 참조하는
테이블/컬럼이 새 카탈로그(또는 실DB)에 없는데 조용히 통과되는 것** — 런타임에 SQL 이 깨지거나
0명이 나온다. 이 도구는 그 불일치를 배포 전에 드러낸다.

검사 항목:
    1. registry↔catalog: 레지스트리가 참조하는 테이블이 schema_catalog 에 있는가
    2. registry 컬럼: 회원 기준 테이블과 table 이 명시된 팩트/지표 설정의 컬럼이 카탈로그에
         실재하는가
  3. catalog↔live DB(선택 --check-db): 카탈로그 테이블이 각 커넥션 실DB에 실재하는가
     (schema_extract.refresh_external_catalog --dry-run 재사용)
  4. 커넥션 방언 등록: 카탈로그의 각 database 가 sql_dialect 에 방언이 등록돼 있는가

사용:
  python db_swap_preflight.py                # 정적 검사(1,2,4). DB 접속 불필요
  python db_swap_preflight.py --check-db     # + 실DB 대조(3). 커넥션 필요
  python db_swap_preflight.py --json         # 기계 판독용 JSON 출력
  종료코드 0=통과, 1=문제 발견(CI/런북 게이트용)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# 결과 줄에 한글·기호(✅/❌)가 들어간다. Windows 레거시 콘솔(cp949)에서는 print 가
# UnicodeEncodeError 로 죽어 **ok 여부와 무관하게 종료코드가 1** 이 된다 — 게이트 도구가
# 통과를 실패로 보고하는 최악의 오작동이라 인코딩을 먼저 고정한다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REGISTRY_PATHS = [
    Path("docs/data/runtime/sql/member_target_filters.json"),
    Path("docs/data/runtime/sql/member_metrics.json"),
]
SCHEMA_CATALOG_PATH = Path("docs/data/generated/schema_catalog.json")

# 레지스트리에서 테이블명을 담는 키(정확 일치 또는 '*_table' 접미어).
_TABLE_KEYS = {"table", "member_table", "value_table"}
# alias.COLUMN 또는 bare COLUMN 형태(대문자+언더스코어). 스키마 컬럼 후보.
_COLUMN_RE = re.compile(r"^(?:([A-Za-z]\w*)\.)?([A-Z][A-Z0-9_]{1,})$")
_DIRECT_COLUMN_KEYS = {"column", "member_key", "login_id_key"}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _catalog_index(catalog: dict[str, Any]) -> tuple[dict[str, set[str]], dict[str, str]]:
    """schema_catalog → ({테이블: {대문자 컬럼집합}}, {테이블: database})."""
    columns_by_table: dict[str, set[str]] = {}
    database_by_table: dict[str, str] = {}
    for name, meta in (catalog.get("tables") or {}).items():
        if not isinstance(meta, dict):
            continue
        columns_by_table[name] = {
            str(column.get("name")).upper()
            for column in meta.get("columns", [])
            if isinstance(column, dict) and column.get("name")
        }
        if meta.get("database"):
            database_by_table[name] = str(meta["database"])
    return columns_by_table, database_by_table


def _walk_table_refs(node: Any, out: set[str]) -> None:
    """레지스트리를 재귀 순회하며 테이블명 참조를 모은다(키가 table/member_table/value_table/*_table)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and value and (key in _TABLE_KEYS or key.endswith("_table")):
                out.add(value.split(".")[-1])  # 스키마 접두어(quadmax_sdz.t_x) 제거
            _walk_table_refs(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_table_refs(item, out)


def _base_entity_columns(registry: dict[str, Any]) -> tuple[str | None, str, set[str]]:
    """(base_table, base_alias, 참조된 base-테이블 컬럼집합)을 레지스트리에서 뽑는다.

    base_alias 접두어가 붙은 column 값과, 별칭 없는 회원-테이블 전용 섹션
    (active_state/birthday_target/signup_target/recent_login_target)의 컬럼을 base 테이블 소속으로 본다.
    """
    base = registry.get("base_entity") if isinstance(registry.get("base_entity"), dict) else {}
    base_table = base.get("table")
    base_alias = str(base.get("alias") or "B")
    columns: set[str] = set()
    for key in ("member_key", "login_id_key"):
        value = base.get(key)
        if isinstance(value, str) and value:
            columns.add(value.split(".")[-1].upper())

    # 별칭 없이 회원 테이블 컬럼을 지정하는 섹션들.
    for section in ("active_state", "birthday_target", "signup_target", "recent_login_target"):
        cfg = registry.get(section)
        if isinstance(cfg, dict) and isinstance(cfg.get("column"), str):
            columns.add(cfg["column"].split(".")[-1].upper())

    # base_alias 접두어가 붙은 모든 column 값(eq_filters/numeric_filters/region_target 등).
    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "column" and isinstance(value, str):
                    match = _COLUMN_RE.match(value.strip())
                    if match and match.group(1) == base_alias:
                        columns.add(match.group(2).upper())
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(registry)
    # region_target.columns 는 {sido: "B.SIDO"} 형태라 위 walk 가 key=="column" 이 아니라 놓친다 → 별도 처리.
    region = registry.get("region_target")
    if isinstance(region, dict) and isinstance(region.get("columns"), dict):
        for value in region["columns"].values():
            match = _COLUMN_RE.match(str(value).strip())
            if match and (match.group(1) in (base_alias, None)):
                columns.add(match.group(2).upper())
    return (base_table, base_alias, columns)


def _configured_table_columns(registry: dict[str, Any]) -> set[tuple[str, str]]:
    """`table`이 선언된 레지스트리 블록의 직접 컬럼 참조를 (테이블, 컬럼)으로 모은다.

    팩트 테이블의 집계 컬럼, 조인키, 날짜 컬럼처럼 SQL 생성에 직접 쓰이는 선언형 필드만
    검증한다. join 조건이나 자유 형식 predicate를 억지로 파싱하지 않아 설정식의 표현 자유는
    유지한다.
    """
    result: set[tuple[str, str]] = set()

    def add_columns(table: str | None, value: Any) -> None:
        if not table:
            return
        values = value.values() if isinstance(value, dict) else value if isinstance(value, list) else (value,)
        for candidate in values:
            if not isinstance(candidate, str):
                continue
            match = _COLUMN_RE.match(candidate.strip())
            if match:
                result.add((table.split(".")[-1], match.group(2).upper()))

    def walk(node: Any, inherited_table: str | None = None) -> None:
        if isinstance(node, dict):
            table_value = next(
                (
                    value
                    for key, value in node.items()
                    if key in _TABLE_KEYS and isinstance(value, str) and value
                ),
                inherited_table,
            )
            table = table_value.split(".")[-1] if isinstance(table_value, str) else None
            for key, value in node.items():
                if (
                    key in _DIRECT_COLUMN_KEYS
                    or key.endswith("_column")
                    or key.endswith("_columns")
                    or key in {"columns", "match_columns", "cell_keys"}
                ):
                    # 소유 테이블 선언은 두 가지 형태로 나타난다: 형제 키 ``<키>_table``(예:
                    # column_table 이 column 의 소유자)과 접두어 규칙 ``<접두어>_table``(예:
                    # campaign_date_column ↔ campaign_date_table). 형제 키를 먼저 보지 않으면
                    # column_table 처럼 이름이 겹치는 선언을 놓쳐, 실제로는 상품 마스터가 소유한
                    # 컬럼을 주문 테이블 소유로 오귀속해 오탐이 난다.
                    owner_table = node.get(f"{key}_table")
                    if owner_table is None and key.endswith("_column"):
                        owner_table = node.get(f"{key[:-len('_column')]}_table")
                    add_columns(owner_table if isinstance(owner_table, str) else table, value)
                walk(value, table)
        elif isinstance(node, list):
            for item in node:
                walk(item, inherited_table)

    walk(registry)
    return result


def run_preflight(check_db: bool = False) -> dict[str, Any]:
    problems: list[str] = []
    warnings: list[str] = []

    if not SCHEMA_CATALOG_PATH.exists():
        return {"ok": False, "problems": [f"스키마 카탈로그 없음: {SCHEMA_CATALOG_PATH}"], "warnings": []}
    catalog = _load_json(SCHEMA_CATALOG_PATH)
    columns_by_table, database_by_table = _catalog_index(catalog)
    catalog_tables = set(columns_by_table)

    registries: dict[str, dict[str, Any]] = {}
    for path in REGISTRY_PATHS:
        if path.exists():
            registries[path.name] = _load_json(path)
        else:
            warnings.append(f"레지스트리 파일 없음(건너뜀): {path}")

    # 논리 심볼 → 물리 테이블. 설정은 물리 테이블명 대신 심볼('product' 등)을 쓸 수 있고, 그 매핑은
    # 레지스트리의 table_symbols 가 소유한다. 심볼을 해석하지 못하면 멀쩡한 설정이 '카탈로그에 없는
    # 테이블'로 보고돼 게이트가 상시 빨강이 된다 — 반대로 매핑이 선언되지 않은 심볼은 여전히 실패해야
    # 하므로(그게 이식성 결함이다) 원문 심볼명을 메시지에 그대로 남긴다.
    table_symbols: dict[str, str] = {}
    for registry in registries.values():
        declared = registry.get("table_symbols")
        if isinstance(declared, dict):
            table_symbols.update(
                {str(k): str(v) for k, v in declared.items() if isinstance(v, str)}
            )

    def resolve(table: str) -> str:
        return table_symbols.get(table, table)

    # 1) registry 테이블 참조 ↔ catalog
    referenced_tables: set[str] = set()
    for name, registry in registries.items():
        refs: set[str] = set()
        _walk_table_refs(registry, refs)
        referenced_tables |= refs
        for table in sorted(refs):
            if resolve(table) not in catalog_tables:
                problems.append(f"[{name}] 참조 테이블 '{table}' 이 schema_catalog 에 없음")

    # 2) base_entity 및 table 소유 선언형 컬럼 ↔ catalog
    mtf = registries.get("member_target_filters.json")
    configured_columns: set[tuple[str, str]] = set()
    if mtf:
        base_table, base_alias, base_columns = _base_entity_columns(mtf)
        if not base_table:
            problems.append("[member_target_filters.json] base_entity.table 미지정")
        elif base_table not in catalog_tables:
            problems.append(f"[base_entity] 회원 기준 테이블 '{base_table}' 이 schema_catalog 에 없음")
        else:
            configured_columns |= {(base_table, column) for column in base_columns}

    for registry in registries.values():
        configured_columns |= _configured_table_columns(registry)

    for table, column in sorted(configured_columns):
        present = columns_by_table.get(resolve(table))
        if present is None:
            continue  # 테이블 참조 검사가 위에서 더 정확한 원인을 이미 보고한다.
        if column not in present:
            problems.append(f"[registry] 컬럼 '{table}.{column}' 이 카탈로그 테이블에 없음")

    # 4) 커넥션 방언 등록
    try:
        from sql_dialect import CONNECTION_DIALECTS

        for table, database in sorted(database_by_table.items()):
            if database not in CONNECTION_DIALECTS:
                warnings.append(f"커넥션 '{database}'(테이블 {table})의 방언이 sql_dialect.CONNECTION_DIALECTS 에 미등록")
    except Exception as exc:  # pragma: no cover
        warnings.append(f"방언 등록 확인 실패: {exc}")

    # 5) capability 선언 ↔ 실행 자산 경량 대조(플랜 A-3). 빌더 소유권 축은 graph_rag import 가
    # 필요해 무거우므로 여기서는 제외하고 tests/test_capability_contract.py 가 담당한다 —
    # 이 분담은 의도된 설계다(preflight 는 설정·순수 모듈만 읽는 경량 게이트).
    try:
        import capability_validation

        for issue in capability_validation.validate_capabilities():
            problems.append(f"[capability] {issue}")
    except Exception as exc:  # pragma: no cover
        problems.append(f"[capability] 정적 검증 실행 실패: {exc}")

    # 3) catalog ↔ live DB (선택)
    db_summary: dict[str, Any] | None = None
    if check_db:
        try:
            import schema_extract as se

            db_summary = se.refresh_external_catalog(_load_json(SCHEMA_CATALOG_PATH))
            for missing in db_summary.get("missing_in_db", []):
                problems.append(f"[live-db] 카탈로그 테이블이 실DB에 없음: {missing}")
        except Exception as exc:
            problems.append(f"[live-db] 실DB 대조 실패(접속/권한 확인): {exc}")

    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "stats": {
            "catalog_tables": len(catalog_tables),
            "referenced_tables": len(referenced_tables),
            "checked_columns": len(configured_columns),
            "checked_db": check_db,
        },
        "db_summary": db_summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="DB 스왑 프리플라이트 — 레지스트리↔카탈로그↔실DB 정합성 검증.")
    parser.add_argument("--check-db", action="store_true", help="카탈로그 테이블을 각 커넥션 실DB와 대조(접속 필요).")
    parser.add_argument("--json", action="store_true", help="JSON 으로 출력.")
    args = parser.parse_args()

    result = run_preflight(check_db=args.check_db)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "PASS ✅" if result["ok"] else "FAIL ❌"
        print(f"DB 스왑 프리플라이트: {status}")
        stats = result.get("stats", {})
        print(f"  카탈로그 테이블 {stats.get('catalog_tables')}개 / 레지스트리 참조 {stats.get('referenced_tables')}개"
              + (" / 실DB 대조함" if stats.get("checked_db") else " / 정적 검사만"))
        for problem in result["problems"]:
            print(f"  ❌ {problem}")
        for warning in result["warnings"]:
            print(f"  ⚠️  {warning}")
        if result["ok"] and not result["warnings"]:
            print("  참조된 모든 테이블/컬럼이 카탈로그와 일치합니다.")

    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
