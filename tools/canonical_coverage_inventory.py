"""canonical 이 못 하고 legacy 는 하는 축의 **차집합**을 기계로 산출한다 (P4).

R1(권한과 어휘의 비대칭)의 작업 큐다. 실행 권한은 canonical Event IR 경로로 옮겨졌는데
그 경로의 어휘는 요청의 일부만 덮는다 — 그 사이 구간이 전부 "미지원"으로 떨어졌다.
케이스를 세면 끝이 없고 진척도 보이지 않는다. **차집합**이 단위여야 한다:

    legacy 실행 자산이 물리 컬럼까지 선언한 축  ⊖  canonical audience_catalog 가 아는 축

두 쪽을 **같은 통화**(schema_catalog 에 실재하는 물리 컬럼 이름)로 환산해 뺀다. 그래야
"선언 방식이 달라서 못 세는" 축이 없다.

    python tools/canonical_coverage_inventory.py            # 사람이 읽는 요약
    python tools/canonical_coverage_inventory.py --json     # 기계 판독(기준선 형식)

선례는 `tools/physical_binding_inventory.py` 이고 기준선·래칫 규약도 같다:
차집합이 **커지면**(canonical 이 뒤처지면) `tests/test_canonical_coverage_ratchet.py` 가 red 다.
줄이는 방향은 언제나 통과한다 — 기준선을 낮춰 적으면 그만이다.

카탈로그가 `coverage_exemptions` 로 **명시 선언**한 축은 차집합에서 빠진다. "빠뜨린 것"과
"못 하는 것"을 가드가 구분할 수 있어야 하기 때문이다(NOTES ⑤ 의 signal_coverage 방식).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "docs" / "data" / "runtime"
SCHEMA_CATALOG = ROOT / "docs" / "data" / "generated" / "schema_catalog.json"
AUDIENCE_CATALOG = RUNTIME / "semantics" / "audience_catalog.json"
LEGACY_SOURCES = (
    RUNTIME / "sql" / "member_target_filters.json",
    RUNTIME / "sql" / "member_metrics.json",
)
LEGACY_METRIC_DIR = RUNTIME / "sql" / "metrics"
BASELINE = ROOT / "docs" / "data" / "test_baselines" / "canonical_coverage_baseline.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 물리 컬럼을 담는 키(선언 관례). db_swap_preflight 와 같은 규약을 쓴다 — 두 도구가 서로 다른
# 관례를 가지면 한쪽이 세지 못하는 축이 조용히 생긴다.
_COLUMN_KEYS = {"column", "member_key", "login_id_key", "columns", "match_columns", "cell_keys"}
_COLUMN_SUFFIXES = ("_column", "_columns", "_key")


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _known_columns() -> set[str]:
    """schema_catalog 에 실재하는 컬럼 이름 전부(대문자). 실재하지 않는 이름은 세지 않는다."""
    catalog = _load(SCHEMA_CATALOG)
    names: set[str] = set()
    for meta in (catalog.get("tables") or {}).values():
        if isinstance(meta, dict):
            for column in meta.get("columns") or []:
                name = str((column or {}).get("name") or "").strip().upper()
                if name:
                    names.add(name)
    return names


def _bare(value: Any) -> list[str]:
    """'B.SMS_YN' / '{alias}.PRODUCT_ID' / 'SMS_YN' → 'SMS_YN'.

    `columns` 는 배열로도, 이름→컬럼 매핑(region_target 처럼)으로도 선언된다. 매핑 형태를
    빠뜨리면 그 축이 통째로 차집합에서 사라져 '이미 닫힌 것'처럼 보인다.
    """
    if isinstance(value, dict):
        value = list(value.values())
    out: list[str] = []
    for item in value if isinstance(value, list) else [value]:
        text = str(item or "").strip()
        if not text:
            continue
        name = text.rpartition(".")[2].strip().upper()
        if name.isidentifier() or (name and all(ch.isalnum() or ch == "_" for ch in name)):
            out.append(name)
    return out


def _walk_columns(node: Any, known: set[str], axis: str, found: dict[str, set[str]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("_"):
                continue
            if key in _COLUMN_KEYS or any(key.endswith(suffix) for suffix in _COLUMN_SUFFIXES):
                for name in _bare(value):
                    if name in known:
                        found.setdefault(name, set()).add(axis)
            _walk_columns(value, known, axis, found)
    elif isinstance(node, list):
        for item in node:
            _walk_columns(item, known, axis, found)


def canonical_columns(known: set[str]) -> dict[str, set[str]]:
    """canonical audience_catalog 이 물리적으로 닿는 컬럼."""
    catalog = _load(AUDIENCE_CATALOG)
    found: dict[str, set[str]] = {}
    for section in ("sources", "fields", "subject", "joins"):
        _walk_columns(catalog.get(section), known, f"canonical.{section}", found)
    return found


def legacy_columns(known: set[str]) -> dict[str, set[str]]:
    """legacy 실행 자산이 물리적으로 닿는 컬럼(선언한 축 이름과 함께)."""
    found: dict[str, set[str]] = {}
    for path in LEGACY_SOURCES:
        if not path.exists():
            continue
        payload = _load(path)
        if isinstance(payload, dict):
            for axis, node in payload.items():
                if not str(axis).startswith("_"):
                    _walk_columns(node, known, str(axis), found)
        else:
            _walk_columns(payload, known, path.stem, found)
    for path in sorted(LEGACY_METRIC_DIR.glob("*.json")) if LEGACY_METRIC_DIR.exists() else []:
        _walk_columns(_load(path), known, f"metric:{path.stem}", found)
    return found


def exemptions() -> dict[str, str]:
    """카탈로그가 `expressible:false` 로 **명시 선언**한 축(컬럼 → 사유)."""
    catalog = _load(AUDIENCE_CATALOG)
    declared = catalog.get("coverage_exemptions")
    if not isinstance(declared, dict):
        return {}
    return {
        str(column).strip().upper(): str((spec or {}).get("note") or "")
        for column, spec in declared.items()
        if isinstance(spec, dict) and spec.get("expressible") is False
    }


def scan() -> dict[str, Any]:
    known = _known_columns()
    canonical = canonical_columns(known)
    legacy = legacy_columns(known)
    exempt = exemptions()

    missing = {
        column: sorted(axes)
        for column, axes in legacy.items()
        if column not in canonical and column not in exempt
    }
    axes: dict[str, list[str]] = {}
    for column, owners in missing.items():
        for axis in owners:
            axes.setdefault(axis, []).append(column)
    return {
        "canonical_columns": len(canonical),
        "legacy_columns": len(legacy),
        "missing_columns": len(missing),
        "missing_axes": len(axes),
        "exempt_columns": len(exempt),
        "by_axis": {axis: sorted(columns) for axis, columns in sorted(axes.items())},
        "columns": dict(sorted(missing.items())),
    }


def _baseline() -> dict[str, Any]:
    return _load(BASELINE) if BASELINE.exists() else {}


def compare(result: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """기준선 대비 **악화**만 보고한다(개선은 언제나 통과)."""
    problems: list[str] = []
    recorded = baseline.get("missing_columns")
    if isinstance(recorded, int) and result["missing_columns"] > recorded:
        problems.append(
            f"차집합이 커졌다: {recorded} → {result['missing_columns']} "
            "(canonical 이 legacy 에 더 뒤처졌다)"
        )
    for axis, columns in (baseline.get("by_axis") or {}).items():
        current = set(result["by_axis"].get(axis) or [])
        added = sorted(current - set(columns))
        if added:
            problems.append(f"축 {axis} 에 미해결 컬럼이 늘었다: {added}")
    for axis in sorted(set(result["by_axis"]) - set(baseline.get("by_axis") or {})):
        problems.append(f"기준선에 없던 축이 나타났다: {axis} → {result['by_axis'][axis]}")
    return problems


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="기계 판독 결과를 stdout 에 덤프")
    parser.add_argument("--check", action="store_true", help="기준선 대비 악화면 종료코드 1")
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = scan()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    print(f"canonical 물리 컬럼 {result['canonical_columns']} / legacy {result['legacy_columns']}")
    print(f"차집합 {result['missing_columns']} 컬럼 / {result['missing_axes']} 축"
          f" (명시 면제 {result['exempt_columns']})")
    print()
    for axis, columns in result["by_axis"].items():
        print(f"  {axis:34} {', '.join(columns)}")

    if args.check:
        problems = compare(result, _baseline())
        if problems:
            print()
            for problem in problems:
                print(f"FAIL {problem}")
            return 1
        print("\nOK — 기준선 대비 악화 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
