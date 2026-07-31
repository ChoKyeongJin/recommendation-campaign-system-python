"""소스에 하드코딩된 물리 테이블·컬럼 바인딩이 늘지 않게 한다.

타겟팅 대상 실DB 는 계속 바뀔 수 있으므로 스키마 지식은 설정(member_target_filters.json /
schema_catalog.json)이 소유해야 한다. 그런데 실제로는 테이블·컬럼 이름이 소스 곳곳에 리터럴로
남아 있다 — DB 를 바꿀 때 설정만 고치면 되는 게 아니라 소스도 뒤져야 하고, 빠뜨리면 SQL 이
'성공하는데 0명'이 되는 형태로 조용히 틀린다.

세지 않으면 줄지 않는다. 여기서는 두 가지만 강제한다:
  (1) 총량이 기준선을 넘지 않는다(하향 전용 래칫)
  (2) 소스가 참조하는 이름은 카탈로그에 실재한다(오타·유령 컬럼 차단)

이관 자체는 플랜 W5-3 의 일이고, 이 파일은 그 진행을 측정 가능하게 만든다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import physical_binding_inventory as inventory  # noqa: E402

BASELINE_PATH = REPO_ROOT / "docs" / "data" / "physical_binding_baseline.json"


def _baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_baseline_exists_and_is_non_trivial() -> None:
    baseline = _baseline()
    assert baseline["total"] > 0, "기준선이 0 이면 래칫이 아무것도 지키지 않는다."


def test_total_does_not_regress() -> None:
    """새 하드코딩이 들어오면 즉시 드러난다."""

    current = inventory.scan()
    baseline = _baseline()
    assert current["total"] <= baseline["total"], (
        f"소스 하드코딩 물리 바인딩이 {baseline['total']} → {current['total']} 로 늘었다.\n"
        "설정(member_target_filters.json)으로 빼거나, 정말 필요하면 "
        "`python tools/physical_binding_inventory.py --json > docs/data/physical_binding_baseline.json` 로 "
        "기준선을 올리고 커밋 메시지에 사유를 남겨라."
    )


def test_no_file_regresses_individually() -> None:
    """총량이 같아도 한 파일이 늘고 다른 파일이 줄면 이관이 아니라 이동이다."""

    current = inventory.scan()["per_file"]
    baseline = _baseline()["per_file"]
    grown = {
        file: (baseline.get(file, 0), count)
        for file, count in current.items()
        if count > baseline.get(file, 0)
    }
    assert not grown, f"파일별 하드코딩이 늘었다(기준선, 현재): {grown}"


def test_referenced_names_exist_in_the_catalog() -> None:
    """스캐너는 카탈로그에 있는 이름만 세므로, 이 테스트는 스캐너가 실제로 대조하는지를 본다."""

    tables, columns = inventory._catalog_names()
    assert len(tables) > 10 and len(columns) > 100, (
        f"카탈로그 인덱스가 비정상적으로 작다(테이블 {len(tables)}, 컬럼 {len(columns)}) — "
        "대조가 공허해진다."
    )
    current = inventory.scan()
    for hit in current["hits"][:50]:
        pool = tables if hit["kind"] == "table" else columns
        assert hit["name"] in pool, f"카탈로그에 없는 이름을 셌다: {hit}"


def test_scanner_skips_its_own_tooling() -> None:
    """도구·스키마 추출기는 스키마 이름을 다루는 것이 정당하다 — 부채로 세면 신호가 흐려진다."""

    files = {path.name for path in inventory._source_files()}
    assert "db_swap_preflight.py" not in files
    assert "physical_binding_inventory.py" not in files
