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

BASELINE_PATH = REPO_ROOT / "docs" / "data" / "test_baselines" / "physical_binding_baseline.json"


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
        "`python tools/physical_binding_inventory.py --json > "
        "docs/data/test_baselines/physical_binding_baseline.json` 로 "
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


def test_all_binding_positions_resolve_to_the_catalog() -> None:
    """명시적 바인딩 위치의 미등록 테이블·컬럼은 배포 전에 실패한다."""

    tables, columns = inventory._catalog_names()
    assert len(tables) > 10 and len(columns) > 100, (
        f"카탈로그 인덱스가 비정상적으로 작다(테이블 {len(tables)}, 컬럼 {len(columns)}) — "
        "대조가 공허해진다."
    )
    current = inventory.scan()
    assert not current["violations"], (
        "카탈로그에 없는 물리 바인딩이 있다:\n  "
        + "\n  ".join(
            f"{item['file']}:{item['line']} {item['kind']}={item['name']} "
            f"({item['source']})"
            for item in current["violations"]
        )
    )


def test_scanner_skips_its_own_tooling() -> None:
    """도구·스키마 추출기는 스키마 이름을 다루는 것이 정당하다 — 부채로 세면 신호가 흐려진다."""

    files = {path.name for path in inventory._source_files()}
    assert "db_swap_preflight.py" not in files
    assert "physical_binding_inventory.py" not in files


def test_scan_is_independent_of_installed_and_built_packages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """로컬 설치물의 유무가 제품 코드 기준선을 바꾸면 안 된다."""

    catalog_path = tmp_path / "docs" / "data" / "generated" / "schema_catalog.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(
        json.dumps(
            {
                "tables": {
                    "CUSTOMERS": {
                        "columns": [{"name": "CUSTOMER_ID"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "product.py").write_text(
        'BINDING = {"column": "CUSTOMER_ID"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(inventory, "ROOT", tmp_path)

    product_only = inventory.scan()
    assert product_only["total"] == 1

    excluded_directories = (
        ".venv",
        "venv",
        "vendor/site-packages",
        "build",
        "dist",
    )
    for index, directory in enumerate(excluded_directories):
        package_file = tmp_path / directory / f"installed_{index}.py"
        package_file.parent.mkdir(parents=True, exist_ok=True)
        package_file.write_text(
            'BINDING = {"column": "CUSTOMER_ID"}\n',
            encoding="utf-8",
        )

    assert inventory.scan() == product_only


def test_unknown_table_and_column_bindings_are_reported(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """카탈로그 미등록 이름을 hit에서 버리지 않고 violation으로 보존한다."""

    catalog_path = tmp_path / "docs" / "data" / "generated" / "schema_catalog.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(
        json.dumps(
            {
                "tables": {
                    "CUSTOMERS": {
                        "columns": [{"name": "CUSTOMER_ID"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "product.py").write_text(
        "\n".join(
            (
                "BINDING = {",
                '    "table": "MISSING_CUSTOMERS",',
                '    "member_column": "MISSING_CUSTOMER_ID",',
                "}",
                'STATUS = "UNREGISTERED_STATUS"',
                'METADATA = {"status": "PENDING", "enum": "ACTIVE"}',
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(inventory, "ROOT", tmp_path)

    result = inventory.scan()

    assert result["total"] == 0
    assert {
        (item["kind"], item["name"])
        for item in result["violations"]
    } == {
        ("table", "MISSING_CUSTOMERS"),
        ("column", "MISSING_CUSTOMER_ID"),
    }
    assert result["unknown_total"] == 2


def test_constructor_keywords_and_sql_sources_are_checked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """모델 keyword와 SQL FROM/JOIN도 같은 known/unknown 분류를 사용한다."""

    catalog_path = tmp_path / "docs" / "data" / "generated" / "schema_catalog.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(
        json.dumps(
            {
                "tables": {
                    "CUSTOMERS": {
                        "columns": [{"name": "CUSTOMER_ID"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "product.py").write_text(
        "\n".join(
            (
                "EVENT = EventSpec(",
                '    table="CUSTOMERS",',
                '    subject_key="CUSTOMER_ID",',
                '    event_subject_key="MISSING_JOIN_ID",',
                '    time_column="CUSTOMER_ID",',
                ")",
                'SQL = "SELECT C.CUSTOMER_ID FROM MISSING_ORDERS O JOIN CUSTOMERS C ON 1 = 1"',
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(inventory, "ROOT", tmp_path)

    result = inventory.scan()

    assert {(item["kind"], item["name"]) for item in result["hits"]} == {
        ("table", "CUSTOMERS"),
        ("column", "CUSTOMER_ID"),
    }
    assert {(item["kind"], item["name"]) for item in result["violations"]} == {
        ("column", "MISSING_JOIN_ID"),
        ("table", "MISSING_ORDERS"),
    }


def test_sql_examples_inside_docstrings_are_not_bindings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """설명용 FROM/JOIN 예시는 실행 SQL이 아니므로 카탈로그 위반으로 세지 않는다."""

    catalog_path = tmp_path / "docs" / "data" / "generated" / "schema_catalog.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(json.dumps({"tables": {}}), encoding="utf-8")
    (tmp_path / "product.py").write_text(
        '''"""SELECT * FROM MODULE_DOC_TABLE"""

class Example:
    """SELECT * FROM CLASS_DOC_TABLE"""

    def render(self) -> str:
        """SELECT * FROM FUNCTION_DOC_TABLE JOIN SECOND_DOC_TABLE ON 1 = 1"""
        return "ok"
''',
        encoding="utf-8",
    )
    monkeypatch.setattr(inventory, "ROOT", tmp_path)

    result = inventory.scan()

    assert result["total"] == 0
    assert result["violations"] == []


def test_json_cli_keeps_the_existing_summary_shape(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    catalog_path = tmp_path / "docs" / "data" / "generated" / "schema_catalog.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(
        json.dumps(
            {
                "tables": {
                    "CUSTOMERS": {
                        "columns": [{"name": "CUSTOMER_ID"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "product.py").write_text(
        'BINDING = {"table": "CUSTOMERS", "column": "CUSTOMER_ID"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(inventory, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["physical_binding_inventory.py", "--json"])

    assert inventory.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"total", "tables", "columns", "per_file"}
