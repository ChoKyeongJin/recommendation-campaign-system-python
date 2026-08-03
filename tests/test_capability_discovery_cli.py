from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

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


def test_search_cli_is_graph_first_and_non_executable_by_default(capsys) -> None:
    status = capability_graphrag.main(
        [
            "search",
            "grade",
            "--repo-root",
            str(ROOT),
            "--limit",
            "5",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["mode"] == "deterministic"
    assert payload["rerank_applied"] is False
    assert payload["candidate_generated"] is False
    assert payload["diagnostic_only"] is True
    assert payload["executable"] is False
    retrieved_ids = {item["id"] for item in payload["retrieved_nodes"]}
    result_ids = {
        item["candidate_id"]
        for key in ("approved_results", "discovery_results")
        for item in payload[key]
    }
    assert result_ids
    assert result_ids <= retrieved_ids


def test_search_cli_rejects_model_without_explicit_llm(capsys) -> None:
    status = capability_graphrag.main(
        [
            "search",
            "grade",
            "--repo-root",
            str(ROOT),
            "--model",
            "some-model",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 2
    assert payload["ok"] is False
    assert payload["runtime_changed"] is False
    assert payload["error"] == "--model requires --llm"


def test_search_cli_llm_switch_is_explicit_and_forwards_bounds(
    monkeypatch, capsys
) -> None:
    calls: list[dict[str, Any]] = []

    class _Result:
        @staticmethod
        def to_dict() -> dict[str, Any]:
            return {
                "mode": "llm_rerank",
                "rerank_applied": True,
                "candidate_generated": False,
                "diagnostic_only": True,
                "executable": False,
            }

    class _Search:
        @staticmethod
        def search(query: str, *, limit: int, approved_only: bool) -> _Result:
            calls.append(
                {
                    "query": query,
                    "limit": limit,
                    "approved_only": approved_only,
                }
            )
            return _Result()

    def _build(snapshot, repository_root, *, model, timeout):
        calls.append(
            {
                "snapshot": snapshot,
                "repository_root": repository_root,
                "model": model,
                "timeout": timeout,
            }
        )
        return _Search()

    monkeypatch.setattr(capability_graphrag, "build_openai_capability_search", _build)
    status = capability_graphrag.main(
        [
            "search",
            "grade",
            "--repo-root",
            str(ROOT),
            "--llm",
            "--model",
            "test-fast-model",
            "--timeout",
            "2.5",
            "--limit",
            "3",
            "--approved-only",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["mode"] == "llm_rerank"
    assert payload["executable"] is False
    assert calls[0]["model"] == "test-fast-model"
    assert calls[0]["timeout"] == 2.5
    assert calls[1] == {"query": "grade", "limit": 3, "approved_only": True}
