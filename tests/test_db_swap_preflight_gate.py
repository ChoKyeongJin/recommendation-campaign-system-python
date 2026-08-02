"""DB 스왑 프리플라이트를 pytest 게이트로 승격한다.

db_swap_preflight.py 는 설정 레지스트리(docs/data/runtime/sql/*.json)가 참조하는 테이블·컬럼이
schema_catalog 에 실재하는지 대조하는 이식성 가드다. 그런데 CLI 로만 존재해서 아무도 돌리지
않았고, 그 사이 실제로 3건이 어긋난 채 문서에는 '완료 ✅' 로 적혀 있었다. 설정과 카탈로그가
어긋나면 SQL 은 '성공하는데 0명'으로 조용히 틀린다 — 가장 잡기 어려운 실패 형태다.

이 파일은 삭제된 tests/test_db_swap_preflight.py 의 복원이 아니다. 목적이 다르다:
옛 파일은 합성 픽스처로 '탐지력'만 봤고 '지금 이 저장소가 green 인가'는 보지 않아 이번 드리프트를
막지 못했다. 여기서는 현재 저장소 상태 자체를 단언한다.

run_preflight() 이 모듈 상수의 **상대경로**를 쓰므로 반드시 저장소 루트에서 실행해야 한다
(pytest.ini 가 rootdir 을 고정하지만, 이 fixture 로 cwd 까지 못 박는다).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import db_swap_preflight  # noqa: E402


@pytest.fixture
def at_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)


def test_preflight_is_green_on_current_repo(at_repo_root: None) -> None:
    """설정 ↔ 카탈로그가 어긋난 채 커밋되지 않게 한다."""

    result = db_swap_preflight.run_preflight()
    assert result["ok"], "프리플라이트 실패:\n  " + "\n  ".join(result["problems"])
    assert result["problems"] == []


def test_preflight_actually_checks_something(at_repo_root: None) -> None:
    """공허한 통과 방지 — 검사 대상이 0건이면 green 은 아무 의미가 없다.

    실제로 밟은 함정이다: 레지스트리를 파일 basename 으로 키잉하기 때문에 파일명이 다르면
    컬럼 검사 전체가 조용히 건너뛰어지고 green 이 된다.
    """

    stats = db_swap_preflight.run_preflight()["stats"]
    assert stats["catalog_tables"] > 0
    assert stats["referenced_tables"] > 0
    assert stats["checked_columns"] > 50, f"검사한 컬럼이 너무 적다: {stats}"


def test_unmapped_table_symbol_still_fails(
    at_repo_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """논리 심볼 해석이 '모르는 테이블을 전부 통과시키는' 우회로가 되지 않는지 확인한다.

    table_symbols 는 설정이 물리 테이블명 대신 쓰는 심볼('product')을 해석하기 위한 것이다.
    선언되지 않은 심볼은 여전히 실패해야 한다 — 그게 바로 이식성 결함이기 때문이다.
    """

    original = db_swap_preflight._load_json

    def fake(path: Path):
        data = original(path)
        if path.name == "member_target_filters.json" and isinstance(data, dict):
            data = dict(data)
            data["_test_unmapped"] = {"table": "NO_SUCH_TABLE_XYZ"}
        return data

    monkeypatch.setattr(db_swap_preflight, "_load_json", fake)
    result = db_swap_preflight.run_preflight()
    assert not result["ok"]
    assert any("NO_SUCH_TABLE_XYZ" in problem for problem in result["problems"])


def test_declared_symbol_resolves_to_a_catalog_table(at_repo_root: None) -> None:
    """table_symbols 의 매핑 대상은 카탈로그에 실재해야 한다(오타 방지)."""

    registry = db_swap_preflight._load_json(
        REPO_ROOT / "docs" / "data" / "runtime" / "sql" / "member_target_filters.json"
    )
    symbols = registry.get("table_symbols") or {}
    assert symbols, "table_symbols 선언이 사라졌다 — 논리 심볼 해석이 무력화된다."

    catalog = db_swap_preflight._load_json(db_swap_preflight.SCHEMA_CATALOG_PATH)
    columns_by_table, _ = db_swap_preflight._catalog_index(catalog)
    unknown = sorted(
        f"{symbol} -> {physical}"
        for symbol, physical in symbols.items()
        if physical not in columns_by_table
    )
    assert not unknown, f"카탈로그에 없는 물리 테이블로 매핑됨: {unknown}"


# ── 게이트 범위: canonical 실행 경로의 카탈로그도 검사 대상인가 ───────────────────────


def test_preflight_covers_the_canonical_execution_catalogs() -> None:
    """SQL 로 가는 길은 둘(legacy 빌더 설정 / canonical Event IR 카탈로그)이다.

    legacy 쪽 설정만 검사하면 canonical 경로가 게이트 밖에 남아, DB 스왑 후
    "레거시는 되는데 canonical 만 0명"이 된다 — 그리고 그 차이는 응답에 드러나지 않는다.
    """
    covered = {path.name for path in db_swap_preflight.REGISTRY_PATHS}
    for required in (
        "member_target_filters.json",
        "member_metrics.json",
        "audience_catalog.json",   # canonical Event IR 물리 바인딩
        "attribute_catalog.json",  # 회원 속성 시점/이력 물리 바인딩
    ):
        assert required in covered, (
            f"{required} 가 프리플라이트 범위 밖이다 — 이 파일의 스키마 드리프트는 "
            "배포 전에 아무도 보지 못한다."
        )


def test_expression_backed_columns_are_not_attributed_to_the_source_table(
    at_repo_root: None,
) -> None:
    """``*_expression`` 이 있으면 나란한 ``*_column`` 은 이름표일 뿐이다.

    canonical 캠페인 사건들은 time_column="CAMP_SDATE" 를 선언하지만 실제로 쓰는 것은
    time_expression="ZC.CAMP_SDATE" 이고, ZC 는 from_sql 이 조인한 Z_CAMPAIGN 이다.
    이름표를 소스 테이블 소유로 읽으면 멀쩡한 설정이 상시 빨강이 된다(오탐이 나면 사람이
    게이트를 끈다 — 정확도가 곧 게이트의 수명이다).
    """
    pairs = db_swap_preflight._configured_table_columns(
        {
            "sources": {
                "probe": {
                    "table": "MCS_CAMP_MBR_RSPN_FT",
                    "time_column": "CAMP_SDATE",
                    "time_expression": "ZC.CAMP_SDATE",
                }
            }
        }
    )
    assert ("MCS_CAMP_MBR_RSPN_FT", "CAMP_SDATE") not in pairs


def test_column_without_expression_is_still_attributed(at_repo_root: None) -> None:
    """반대 방향 — 표현식이 없으면 여전히 소스 테이블 소유로 검사해야 한다(가드 공허 방지)."""
    pairs = db_swap_preflight._configured_table_columns(
        {"sources": {"probe": {"table": "CRM_SL_ORDERHEADERMALL", "time_column": "ORDER_DATE"}}}
    )
    assert ("CRM_SL_ORDERHEADERMALL", "ORDER_DATE") in pairs
