from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools import capability_graphrag, capability_review

ROOT = Path(__file__).resolve().parents[1]


def test_verify_cli_reports_deterministic_offline_projection(capsys) -> None:
    status = capability_graphrag.main(
        ["verify", "--repo-root", str(ROOT), "--format", "json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["ok"] is True
    assert payload["full_rebuild_deterministic"] is True
    assert payload["runtime_changed"] is False
    assert payload["statistics"]["gaps"] > 0


def test_candidate_cli_writes_only_to_explicit_path_and_promotion_is_blocked(
    tmp_path: Path, capsys
) -> None:
    output = tmp_path / "candidate.json"
    status = capability_graphrag.main(
        [
            "candidate",
            "active_state",
            "--repo-root",
            str(ROOT),
            "--output",
            str(output),
            "--format",
            "json",
        ]
    )
    generated = json.loads(capsys.readouterr().out)

    assert status == 0
    assert Path(generated["output"]) == output.resolve()
    assert generated["mutation_performed"] is False
    before = hashlib.sha256(output.read_bytes()).hexdigest()

    assert capability_review.main(["validate", str(output), "--format", "json"]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated == {"issues": [], "ok": True}

    assert capability_review.main(["promote", str(output), "--format", "json"]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["mutation_performed"] is False
    assert hashlib.sha256(output.read_bytes()).hexdigest() == before
