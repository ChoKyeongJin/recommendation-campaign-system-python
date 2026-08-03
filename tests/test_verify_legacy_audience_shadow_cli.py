"""shadow 검증 실행기의 계약 — **아무것도 바꾸지 않고, 검증하지 않은 것을 검증했다고 말하지 않는다.**"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_shadow as shadow  # noqa: E402
from tools import verify_legacy_audience_shadow as cli  # noqa: E402

ASSETS = REPO_ROOT / "tests" / "fixtures" / "legacy_audience_assets.json"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "audience_shadow_fixture.json"
AS_OF = "2026-08-03T00:00:00"


def _run(tmp_path: Path, *extra: str) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "shadow.json"
    assert cli.main([
        "--assets", str(ASSETS), "--fixture", str(FIXTURE), "--output", str(output),
        "--as-of", AS_OF, "--timezone", "Asia/Seoul", *extra,
    ]) == 0
    return json.loads(output.read_text(encoding="utf-8"))


def test_no_asset_is_cutover_ready_while_required_stages_are_unwired(tmp_path: Path) -> None:
    """스냅샷·성능 단계가 배선되지 않은 동안에는 **어떤 자산도** 통과할 수 없다."""
    report = _run(tmp_path)

    assert report["summary"]["cutover_allowed"] == 0
    for asset in report["assets"]:
        assert asset["cutover_allowed"] is False
        assert any("미실행" in reason for reason in asset["blocking_reasons"])


def test_unwired_stages_are_reported_with_a_reason(tmp_path: Path) -> None:
    report = _run(tmp_path)
    asset = next(item for item in report["assets"] if item["asset_id"] == "aud-convertible-absence")
    stages = {item["stage"]: item for item in asset["stages"]}

    for name in (shadow.STAGE_SNAPSHOT_MEMBERS, shadow.STAGE_PERFORMANCE):
        assert stages[name]["status"] == shadow.NOT_RUN
        assert stages[name]["detail"].strip(), "사유 없는 미실행은 허용하지 않는다"


def test_comparable_assets_actually_reach_the_fixture_stage(tmp_path: Path) -> None:
    """대조 가능한 자산이 하나도 실행되지 않으면 이 보고서는 공허하다."""
    report = _run(tmp_path)
    passed = [
        item["asset_id"] for item in report["assets"]
        for stage in item["stages"]
        if stage["stage"] == shadow.STAGE_ADVERSARIAL_FIXTURE and stage["status"] == shadow.PASS
    ]

    assert set(passed) >= {
        "aud-convertible-absence",
        "aud-convertible-lifetime-absence",
        "aud-convertible-aggregate-only",
    }


def test_partially_renderable_assets_are_not_run_instead_of_divergent(tmp_path: Path) -> None:
    """legacy 쪽을 온전히 렌더할 수 없으면 '다르다'가 아니라 '대조하지 않았다'로 남는다."""
    report = _run(tmp_path)
    asset = next(item for item in report["assets"] if item["asset_id"] == "aud-convertible-window")
    stages = {item["stage"]: item for item in asset["stages"]}

    assert stages[shadow.STAGE_ADVERSARIAL_FIXTURE]["status"] == shadow.NOT_RUN
    assert "자기확인" in stages[shadow.STAGE_ADVERSARIAL_FIXTURE]["detail"]
    assert not stages[shadow.STAGE_ADVERSARIAL_FIXTURE]["divergences"]


def test_blocked_assets_stop_at_path_accounting(tmp_path: Path) -> None:
    report = _run(tmp_path)
    asset = next(item for item in report["assets"] if item["asset_id"] == "aud-blocked-login-family")
    stages = {item["stage"]: item for item in asset["stages"]}

    assert stages[shadow.STAGE_PATH_ACCOUNTING]["status"] == shadow.FAIL
    assert stages[shadow.STAGE_SEMANTIC_FINGERPRINT]["status"] == shadow.NOT_RUN
    assert asset["blocking_reasons"]


def test_divergences_default_to_unexplained(tmp_path: Path) -> None:
    report = _run(tmp_path)
    kinds = set(report["summary"]["divergence_kinds"])

    assert kinds <= set(shadow.DIVERGENCE_KINDS)
    assert kinds == {shadow.UNEXPLAINED_DIVERGENCE}


def test_incomplete_approval_file_is_rejected_at_load(tmp_path: Path) -> None:
    approvals = tmp_path / "approvals.json"
    approvals.parent.mkdir(parents=True, exist_ok=True)
    approvals.write_text(
        json.dumps([{"key": "adversarial_fixture:차이", "before_meaning": "a"}]), encoding="utf-8"
    )

    code = cli.main([
        "--assets", str(ASSETS), "--fixture", str(FIXTURE), "--approvals", str(approvals),
        "--as-of", AS_OF,
    ])

    assert code == 2


def test_fixture_anchor_is_the_engine_clock_not_the_declared_as_of(tmp_path: Path) -> None:
    """선언 as_of(2026-08-03)로 경계 행을 만들면 롤링 컷오프와 어긋난다."""
    report = _run(tmp_path)

    assert report["context"]["as_of"].startswith("2026-08-03")
    assert report["fixture_anchor_date"] == shadow.engine_anchor_date().isoformat()
