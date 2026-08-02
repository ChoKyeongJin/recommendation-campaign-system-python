"""월별 스냅샷 적재 깊이를 **두 곳이 선언**한다 — 어긋나면 사용자에게 거짓을 말한다.

  · `attribute_catalog.json`  … `snapshot_months_available` (이력 연산의 고지 문구 입력)
  · `semantic_capabilities.json` … `grains.*.coverage` (요청 구간 판정의 입력)

2026-08-02 이전에는 이 둘이 **차단 게이트**의 입력이라 틀리면 "안 되는데?"로 드러났다.
이제는 비차단 고지의 입력이므로 틀려도 SQL 은 그대로 나가고, 대신 "0건일 수 있습니다"라는
**틀린 안내**만 남는다 — 조용해진 만큼 드리프트를 기계가 잡아야 한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

RUNTIME = REPO_ROOT / "docs" / "data" / "runtime" / "semantics"
ATTRIBUTE_CATALOG = RUNTIME / "attribute_catalog.json"
CAPABILITIES = RUNTIME / "semantic_capabilities.json"
GRAIN = "monthly_attribute_snapshot"


def _months_between(start: str, end: str) -> int:
    """YYYYMM 두 개 사이의 포함 개월 수."""
    start_year, start_month = int(start[:4]), int(start[4:6])
    end_year, end_month = int(end[:4]), int(end[4:6])
    return (end_year - start_year) * 12 + (end_month - start_month) + 1


def test_declared_snapshot_depth_matches_declared_coverage() -> None:
    attributes = json.loads(ATTRIBUTE_CATALOG.read_text(encoding="utf-8"))["attributes"]
    coverage = json.loads(CAPABILITIES.read_text(encoding="utf-8"))["grains"][GRAIN]["coverage"]

    start, end = coverage.get("from"), coverage.get("to")
    assert start and end, f"{GRAIN} 적재 구간이 선언되지 않았다 — 고지 문구를 만들 수 없다."
    expected = _months_between(str(start), str(end))

    for attribute_id, spec in attributes.items():
        if not isinstance(spec.get("binding"), dict):
            continue  # 소스 부재 선언은 적재 깊이를 갖지 않는다.
        declared = spec.get("snapshot_months_available")
        assert declared == expected, (
            f"{attribute_id}.snapshot_months_available={declared} 인데 "
            f"{GRAIN} 적재 구간({start}~{end})은 {expected}개월이다. "
            "적재를 늘렸다면 두 선언을 같이 올려라 — 한쪽만 올리면 안내 문구가 거짓이 된다."
        )


def test_availability_policy_is_declared_and_closed() -> None:
    """정책은 선언에서만 온다 — 코드에 기본값을 숨기면 되돌리기가 코드 변경이 된다."""
    payload = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
    assert payload["data_availability_policy"] in {"advise", "block"}
