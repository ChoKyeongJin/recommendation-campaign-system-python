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

# 순수 설정 모듈만 import 한다(경량 게이트 원칙). metric_registry 는 표준 라이브러리만 쓰고
# 스펙 디렉터리를 모듈 기준 절대경로로 잡으므로 cwd 와 무관하게 안전하다.
import metric_registry

# 결과 줄에 한글·기호(✅/❌)가 들어간다. Windows 레거시 콘솔(cp949)에서는 print 가
# UnicodeEncodeError 로 죽어 **ok 여부와 무관하게 종료코드가 1** 이 된다 — 게이트 도구가
# 통과를 실패로 보고하는 최악의 오작동이라 인코딩을 먼저 고정한다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REGISTRY_PATHS = [
    Path("docs/data/runtime/sql/member_target_filters.json"),
    Path("docs/data/runtime/sql/member_metrics.json"),
    # canonical 실행 경로(Event IR)의 물리 바인딩 소유자. legacy 빌더 설정만 검사하면
    # canonical 경로가 게이트 밖에 남아, DB 스왑 후 "레거시는 되는데 canonical 만 0명"이 된다.
    Path("docs/data/runtime/semantics/audience_catalog.json"),
    # 회원 속성 시점/이력(compositional_targeting)의 물리 바인딩.
    Path("docs/data/runtime/semantics/attribute_catalog.json"),
]
SCHEMA_CATALOG_PATH = Path("docs/data/generated/schema_catalog.json")

# 레지스트리에서 테이블명을 담는 키(정확 일치 또는 '*_table' 접미어).
_TABLE_KEYS = {"table", "member_table", "value_table"}
# alias.COLUMN 또는 bare COLUMN 형태(대문자+언더스코어). 스키마 컬럼 후보.
_COLUMN_RE = re.compile(r"^(?:([A-Za-z]\w*)\.)?([A-Z][A-Z0-9_]{1,})$")
# ``event_subject_key`` 는 사건 테이블의 회원 조인키다. 이 어휘에 없으면 조인키만 검사 밖에 남는데,
# 조인키가 틀리면 SQL 은 실행조차 못 하거나(없는 컬럼) 0명이 된다 — 실제로 cart 소스가
# event_subject_key='MEMBER_ID'(실제 컬럼은 CART_ID)로 선언된 채 게이트를 통과했다(2026-08-03).
_DIRECT_COLUMN_KEYS = {"column", "member_key", "login_id_key", "event_subject_key"}
# 같은 블록에 적히지만 소유자가 **주체(회원) 테이블**인 키. 사건 테이블 소유로 읽으면 멀쩡한 선언이
# 상시 빨강이 되고, 오탐이 나면 사람이 게이트를 끈다.
_SUBJECT_COLUMN_KEYS = {"subject_key"}


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


def _default_binding_overridden(node: dict[str, Any], key: str) -> bool:
    """``correlation_sql`` 이 있으면 기본 상관식은 쓰이지 않는다 — 그때 조인키는 이름표일 뿐인가.

    기본 상관식은 ``alias.event_subject_key = subject_alias.subject_key`` 다. 소스가
    ``correlation_sql`` 을 선언하면 그 식이 대신 쓰이므로, 두 키는 템플릿이 ``{키}`` 토큰으로
    **직접 부를 때만** 실컬럼이어야 한다(캠페인 소스들이 ``{subject_key}`` 를 그렇게 쓴다).
    ``*_expression`` 이 있으면 나란한 ``*_column`` 을 소유로 읽지 않는 규칙과 같은 이유다.
    """
    correlation = node.get("correlation_sql")
    if not isinstance(correlation, str) or not correlation.strip():
        return False
    return "{" + key + "}" not in correlation


def _configured_table_columns(registry: dict[str, Any]) -> set[tuple[str, str]]:
    """`table`이 선언된 레지스트리 블록의 직접 컬럼 참조를 (테이블, 컬럼)으로 모은다.

    팩트 테이블의 집계 컬럼, 조인키, 날짜 컬럼처럼 SQL 생성에 직접 쓰이는 선언형 필드만
    검증한다. join 조건이나 자유 형식 predicate를 억지로 파싱하지 않아 설정식의 표현 자유는
    유지한다.
    """
    result: set[tuple[str, str]] = set()
    subject = registry.get("subject") if isinstance(registry.get("subject"), dict) else {}
    subject_table = subject.get("table") if isinstance(subject.get("table"), str) else None

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
                if key in _SUBJECT_COLUMN_KEYS:
                    if not _default_binding_overridden(node, key):
                        add_columns(subject_table, value)
                elif (
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
                    # 형제 ``<접두어>_expression`` 이 있으면 그 표현식이 **실제로 쓰이는 것**이고
                    # 나란한 ``_column`` 은 이름표에 불과하다. 표현식은 from_sql 이 조인한 다른
                    # 테이블의 별칭을 가리킬 수 있으므로(예: campaign_* 사건의 time_expression =
                    # "ZC.CAMP_SDATE" → Z_CAMPAIGN), 소스 테이블에 귀속하면 오탐이 난다.
                    if key.endswith("_column") and isinstance(
                        node.get(f"{key[:-len('_column')]}_expression"), str
                    ):
                        continue
                    # 같은 이유로 ``correlation_sql`` 이 기본 상관식을 대신하면 조인키도 이름표다.
                    if key == "event_subject_key" and _default_binding_overridden(node, key):
                        continue
                    add_columns(owner_table if isinstance(owner_table, str) else table, value)
                walk(value, table)
        elif isinstance(node, list):
            for item in node:
                walk(item, inherited_table)

    walk(registry)
    # 주체 블록 자신의 회원키(``subject.key``)도 같은 실패 축이다 — 이 컬럼이 없으면 모든 사건의
    # 상관식이 한꺼번에 깨진다. 키 이름이 'key' 라 일반 어휘로는 잡히지 않아 여기서 명시한다.
    if subject_table and isinstance(subject.get("key"), str):
        add_columns(subject_table, subject["key"])
    return result



def _target_database(table: str) -> str:
    """이 테이블이 사는 커넥션 이름(schema_catalog 선언에서 파생)."""
    try:
        catalog = _load_json(SCHEMA_CATALOG_PATH)
    except Exception:  # pragma: no cover
        return "CRMDW"
    meta = (catalog.get("tables") or {}).get(table)
    return str((meta or {}).get("database") or "CRMDW")


def _declared_coverage_problems() -> list[str]:
    """카탈로그가 선언한 적재 구간을 실DB 로 대조한다.

    선언은 두 곳에서 쓰인다: 시간 창이 적재 밖이면 lowering 이 막고(fail-close), 그 문구가
    사용자에게 "적재되어 있지 않습니다"로 나간다. 선언이 틀리면 **막지 말아야 할 것을 막거나
    막아야 할 것을 통과**시키고, 어느 쪽이든 사용자는 이유를 알 수 없다. 정적 게이트는 손으로
    적은 JSON 둘을 서로 비교할 뿐이라 둘 다 같이 틀리면 초록이다 — 그래서 여기서 실DB 에 묻는다.
    """
    problems: list[str] = []
    try:
        import audience_runtime
        import db_connections

        raw = audience_runtime.load_audience_catalog_config()
        coverage = raw.get("data_coverage") or {}
        sources = raw.get("sources") or {}
        times = raw.get("times") or {}
        fields = raw.get("fields") or {}
    except Exception as exc:  # pragma: no cover - 설정 부재
        return [f"[coverage] 선언 로딩 실패: {exc}"]

    for time_id, declaration in times.items():
        if not isinstance(declaration, dict):
            continue
        coverage_id = str(declaration.get("coverage") or "")
        declared = coverage.get(coverage_id)
        if not isinstance(declared, dict) or not (declared.get("from") or declared.get("to")):
            continue
        field = fields.get(str(declaration.get("field") or ""))
        source_id = str(declaration.get("field") or "").partition(".")[0]
        source = sources.get(source_id)
        if not isinstance(source, dict):
            continue
        table = source.get("table")
        column = (field or {}).get("column") if isinstance(field, dict) else source.get("time_column")
        column = column or source.get("time_column")
        if not table or not column:
            continue
        database = str(source.get("database") or _target_database(table))
        try:
            rows = db_connections.run_read_query(
                database,
                f"SELECT MIN({column}) AS lo, MAX({column}) AS hi, "
                f"COUNT(DISTINCT {column}) AS buckets FROM {table}",
            )
        except Exception as exc:  # pragma: no cover - 접속 실패는 위에서 이미 보고된다
            problems.append(f"[coverage] {time_id}: 실DB 조회 실패: {exc}")
            continue
        if not rows:
            continue
        observed = rows[0]
        lo, hi = str(observed.get("lo") or ""), str(observed.get("hi") or "")
        want_lo = str(declared.get("from") or "")[: len(lo)]
        want_hi = str(declared.get("to") or "")[: len(hi)]
        if lo and want_lo and lo != want_lo:
            problems.append(
                f"[coverage] {time_id}: 선언 시작 {want_lo} != 실DB {lo}"
                f" ({table}.{column}) — 미지원 안내가 틀린 근거를 대게 된다"
            )
        if hi and want_hi and hi != want_hi:
            problems.append(
                f"[coverage] {time_id}: 선언 끝 {want_hi} != 실DB {hi} ({table}.{column})"
            )
    return problems


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
            problems.append(f"필수 레지스트리 파일 없음: {path}")

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

    # 3-b) 지표 스펙(docs/data/runtime/sql/metrics/*.json)의 물리 컬럼 ↔ 카탈로그.
    # 이 스펙들은 REGISTRY_PATHS 밖이라 지금까지 DB 스왑 게이트에 걸리지 않았다 — BUY_CYCLE·
    # LAST_LOGIN_DATE 같은 물리 바인딩이 실DB 에서 사라져도 preflight 는 통과했다.
    # metric_registry 는 이 대조를 lint_against_catalog 로 이미 갖고 있었고 부르는 곳만 없었다.
    # 스펙이 스키마를 위반하면 problems(레지스트리를 못 읽는 상태), 카탈로그와 어긋나면
    # warnings(카탈로그 주입식이라 강제하지 않는다는 lint 자체의 정책)로 나눈다.
    try:
        metric_specs = metric_registry.MetricRegistry.load()
    except metric_registry.MetricSpecError as exc:
        problems.append(f"[metric-spec] 지표 스펙이 스키마를 위반한다: {exc}")
    else:
        for warning in metric_specs.lint_against_catalog(columns_by_table):
            warnings.append(f"[metric-spec] {warning}")

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

        # 4) 선언된 적재 구간 ↔ 실DB. 이 숫자는 "다월 연산 미지원" 안내의 근거이므로
        #    틀리면 대량 거짓 안내가 된다. 지금까지의 검증은 손으로 적은 JSON 둘을 서로
        #    비교할 뿐이라 **둘 다 같이 틀리면 통과**했다.
        problems.extend(_declared_coverage_problems())

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
