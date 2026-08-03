"""dry-run 이행 배치 CLI 의 계약 — **읽기만 하고, 재실행이 같은 답을 낸다**.

이행 도구가 조용히 운영 상태를 바꾸는 것이 이 웨이브에서 막아야 할 일이라, 여기서 고정하는 것은
"무엇을 출력하는가"보다 "무엇을 하지 않는가"다: 권위 이관 없음 · 저장 없음 · 재실행 멱등.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import legacy_audience_migration as migration  # noqa: E402
from audience_authority import AudienceAuthority, MigrationStatus  # noqa: E402
from tools import migrate_legacy_audience as cli  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "legacy_audience_assets.json"
AS_OF = "2026-08-03T00:00:00"
# 코퍼스에 자산을 하나 더하는 일이 개수를 박아 둔 테스트 세 개를 깨뜨리면, 다음 사람은 경계 자산을
# 추가하는 대신 추가하지 않는 쪽을 고른다 — 개수는 코퍼스에서 파생한다.
ASSET_COUNT = len(json.loads(FIXTURE.read_text(encoding="utf-8"))["assets"])


def _run(tmp_path: Path, *extra: str) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "report.json"
    code = cli.main([
        "--assets", str(FIXTURE), "--output", str(output),
        "--as-of", AS_OF, "--timezone", "Asia/Seoul", *extra,
    ])
    assert code == 0
    return json.loads(output.read_text(encoding="utf-8"))


def test_dry_run_classifies_every_fixture_asset(tmp_path: Path) -> None:
    report = _run(tmp_path)
    rows = {row["asset_id"]: row for row in report["assets"]}

    assert report["mode"] == "dry-run"
    assert report["summary"]["assets"] == len(rows) == ASSET_COUNT
    assert rows["aud-convertible-window"]["migration_status"] == MigrationStatus.CONVERTED.value
    assert rows["aud-convertible-absence"]["is_executable"] is True
    assert rows["aud-blocked-ir-extension"]["migration_status"] == MigrationStatus.BLOCKED_IR_EXTENSION.value
    assert rows["aud-blocked-login-family"]["classification"] in {
        migration.NEEDS_CATALOG_BINDING, migration.NEEDS_DOMAIN_DECISION,
    }
    assert rows["aud-invalid-window-surface"]["migration_status"] == MigrationStatus.INVALID_LEGACY_ASSET.value
    assert "LEGACY_PATH_UNCLASSIFIED" in rows["aud-unknown-slot"]["reason_codes"]
    assert rows["aud-empty-audience"]["is_executable"] is False  # 조건이 없으면 표현도 없다


def test_no_asset_is_granted_event_ir_authority_by_the_batch(tmp_path: Path) -> None:
    """dry-run 은 권위를 옮기지 않는다 — 검증 전 IR 이 실행되는 유일한 경로를 여기서 닫는다."""

    report = _run(tmp_path)

    assert {row["audience_authority"] for row in report["assets"]} == {AudienceAuthority.LEGACY.value}
    assert not any(
        row["migration_status"] == MigrationStatus.EVENT_IR_PRIMARY.value for row in report["assets"]
    )


def test_converted_assets_actually_compile(tmp_path: Path) -> None:
    report = _run(tmp_path, "--compile-sql")

    compiled = [row for row in report["assets"] if row.get("event_ir_sql")]
    assert compiled, "실행 가능으로 분류된 자산이 하나도 컴파일되지 않았다 — 공허한 통과다"
    for row in compiled:
        assert not row["event_ir_compile_error"]
        assert "SELECT" in row["event_ir_sql"] or "EXISTS" in row["event_ir_sql"]


def test_offline_replay_matches_the_legacy_member_predicates(tmp_path: Path) -> None:
    """회원키 상관 술어 계열은 legacy 헬퍼 출력과 **문자열 대조**가 성립해야 한다."""

    report = _run(tmp_path, "--compile-sql")
    rows = {row["asset_id"]: row for row in report["assets"]}

    absence = rows["aud-convertible-absence"]["replay"]
    lifetime = rows["aud-convertible-lifetime-absence"]["replay"]

    assert absence["verdict"] == "predicate_identical"
    assert absence["unmatched_legacy_predicates"] == []
    assert len(absence["legacy_predicates"]) == 2  # 미구매 창 + 구매 이력 존재
    assert lifetime["verdict"] == "predicate_identical"
    # 대조할 legacy 헬퍼가 없는 계열은 '일치'라고 부르지 않는다 — 모르는 것을 안다고 하지 않는다.
    assert rows["aud-convertible-window"]["replay"]["verdict"] == "structural_only"
    assert rows["aud-convertible-window"]["replay"]["uncomparable_slots"] == [
        "target_user.aggregate_conditions", "target_user.purchase_date",
    ]
    assert report["summary"]["replay_verdicts"]["predicate_identical"] == 2


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    first = _run(tmp_path / "a")
    second = _run(tmp_path / "b")

    def fingerprints(report: dict) -> dict[str, tuple]:
        return {
            row["asset_id"]: (row["source_fingerprint"], row["semantic_fingerprint"], row["migration_status"])
            for row in report["assets"]
        }

    assert fingerprints(first) == fingerprints(second)


def test_checkpoint_resume_skips_completed_assets(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.txt"
    first = _run(tmp_path / "a", "--checkpoint", str(checkpoint), "--batch-size", "3")
    second = _run(tmp_path / "b", "--checkpoint", str(checkpoint))

    assert first["summary"]["assets"] == ASSET_COUNT
    assert second["summary"]["assets"] == 0
    assert second["skipped_by_checkpoint"] == ASSET_COUNT


def test_asset_and_slot_filters_narrow_the_batch(tmp_path: Path) -> None:
    by_id = _run(tmp_path / "a", "--asset-id", "aud-convertible-window")
    by_slot = _run(tmp_path / "b", "--slot-type", "metric_trend")

    assert [row["asset_id"] for row in by_id["assets"]] == ["aud-convertible-window"]
    assert [row["asset_id"] for row in by_slot["assets"]] == ["aud-blocked-ir-extension"]


def test_apply_is_refused_in_this_wave(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["--assets", str(FIXTURE), "--apply"])

    assert code == 2
    assert "compare-and-swap" in capsys.readouterr().err


def test_report_summary_counts_reason_codes(tmp_path: Path) -> None:
    report = _run(tmp_path)
    summary = report["summary"]

    assert summary["executable"] < summary["assets"], "막힌 자산이 하나도 없으면 fixture 가 무의미하다"
    assert summary["by_classification"][migration.CONVERTIBLE] == summary["executable"]
    assert set(summary["reason_codes"]) >= {
        "PERIOD_OVER_PERIOD_NOT_EXPRESSIBLE",
        "TIME_OF_DAY_NOT_EXPRESSIBLE",
        "AGGREGATE_ZERO_EVENT_MEMBER_SEMANTICS",
        "EXCLUDE_CONTAINER_NOT_IN_WAVE",
    }


def test_csv_manifest_has_one_row_per_asset(tmp_path: Path) -> None:
    csv_path = tmp_path / "manifest.csv"
    cli.main([
        "--assets", str(FIXTURE), "--csv-output", str(csv_path),
        "--as-of", AS_OF, "--timezone", "Asia/Seoul",
    ])
    lines = csv_path.read_text(encoding="utf-8").strip().splitlines()

    assert len(lines) == ASSET_COUNT + 1  # 헤더 + 자산
    assert lines[0].startswith("asset_id,asset_revision,classification,migration_status")
