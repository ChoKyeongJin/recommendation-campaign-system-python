"""커버리지 패리티 래칫 — canonical 이 legacy 에 더 뒤처지면 red (P4).

R1 이 지목한 결함은 케이스가 아니라 **비대칭**이다: 실행 권한은 canonical Event IR 로
옮겨졌는데 그 경로의 어휘는 요청의 일부만 덮고, 그 사이 구간이 전부 "미지원"으로 떨어진다.

    `plans_generic_execution_layer` Phase 4 의 결정은 "canonical 이 덮는 범위를 넓혀
    legacy 로 내려가지 않게 한다"였다. 실제로 일어난 순서는 반대다 — 권한을 먼저 옮기고
    어휘를 나중에.

케이스를 세면 끝이 없고 진척도 보이지 않는다. 그래서 단위를 **차집합**으로 잡고, 그 수를
하향 전용으로 못박는다. 줄이는 방향은 언제나 통과한다(기준선을 낮춰 적으면 된다).

`tests/test_physical_binding_ratchet.py` 와 같은 규약이다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import canonical_coverage_inventory as inventory  # noqa: E402

BASELINE_PATH = REPO_ROOT / "docs" / "data" / "test_baselines" / "canonical_coverage_baseline.json"


def _baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_the_scan_is_not_vacuous() -> None:
    """공허한 통과 방지 — 양쪽을 못 세면 차집합 0 이 '완료'처럼 보인다."""
    result = inventory.scan()
    assert result["canonical_columns"] > 0, "canonical 물리 컬럼을 하나도 못 셌다."
    assert result["legacy_columns"] > 0, "legacy 물리 컬럼을 하나도 못 셌다."


def test_every_counted_column_exists_in_the_schema_catalog() -> None:
    """세는 이름이 실재해야 수치가 의미를 갖는다(오탐이 나면 사람이 게이트를 끈다)."""
    known = inventory._known_columns()
    result = inventory.scan()
    unknown = sorted(set(result["columns"]) - known)
    assert not unknown, f"schema_catalog 에 없는 이름을 셌다: {unknown}"


def test_differential_does_not_grow() -> None:
    """canonical 이 legacy 에 더 뒤처지면 red. 이것이 P4 의 진척 지표다."""
    problems = inventory.compare(inventory.scan(), _baseline())
    assert not problems, "\n".join([
        "커버리지 차집합이 커졌다 — legacy 가 하는 축을 canonical 이 못 하게 됐다.",
        *problems,
        "",
        "고치는 법: audience_catalog.json 에 fields 항목을 선언하거나(코드 변경 없이 열린다),",
        "정말 못 하는 축이면 coverage_exemptions 에 expressible:false 로 **명시 선언**하라.",
        "차집합을 줄였다면 기준선을 다시 기록하라: python tools/canonical_coverage_inventory.py --json",
    ])


def test_consent_axis_stayed_closed() -> None:
    """선언 하나로 닫은 축이 다시 열리면 red.

    수신동의 4축은 R1 이 지목한 최대 군집(#47·#50·#56)의 미지원 사유였고,
    `audience_catalog.json` 의 fields 선언 4줄 + value_domain 1개로 닫혔다(코드 변경 0).
    이 항목은 그 사실이 회귀하지 않는지 본다.
    """
    result = inventory.scan()
    consent_columns = {"APP_PUSH_YN", "SMS_YN", "EMAIL_YN", "AGREE_YN"}
    still_missing = consent_columns & set(result["columns"])
    assert not still_missing, (
        f"수신동의 축이 canonical 밖으로 되돌아갔다: {sorted(still_missing)}"
    )


def test_exemptions_are_declared_not_assumed() -> None:
    """'빠뜨린 것'과 '못 하는 것'을 가드가 구분할 수 있어야 한다.

    면제는 카탈로그 선언으로만 성립한다 — 도구가 스스로 어떤 축을 봐주지 않는다.
    """
    declared = inventory.exemptions()
    assert isinstance(declared, dict)
    for column, note in declared.items():
        assert note.strip(), f"면제 {column} 에 사유가 없다 — 사유 없는 면제는 은폐다."
