from __future__ import annotations

from typing import Any

import semantic_relation_ownership


def test_coverage_gap_diagnostic_does_not_replace_semantic_outcome(monkeypatch: Any) -> None:
    gap = {
        "node_id": "history-1",
        "kind": "data_coverage_gap",
        "reason": "requested history predates complete coverage",
        "evidence": "지난달 말 기준",
    }
    monkeypatch.setattr(
        semantic_relation_ownership,
        "relation_data_coverage_gaps",
        lambda *_args: [gap],
    )

    existing = {"status": "resolved"}
    payload: dict[str, Any] = {"semantic_ir": existing}
    returned = semantic_relation_ownership.project_relation_data_coverage(
        payload, "지난달 말 기준 VIP", {}
    )

    assert returned == [gap]
    assert payload["semantic_ir"] is existing


def test_no_coverage_gap_does_not_replace_the_existing_outcome(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        semantic_relation_ownership,
        "relation_data_coverage_gaps",
        lambda *_args: [],
    )
    existing = {"status": "resolved"}
    payload: dict[str, Any] = {"semantic_ir": existing}

    assert semantic_relation_ownership.project_relation_data_coverage(
        payload, "현재 VIP", {}
    ) == []
    assert payload["semantic_ir"] is existing
