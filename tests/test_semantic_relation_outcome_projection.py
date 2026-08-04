from __future__ import annotations

import ast
import copy
import inspect
import textwrap
from typing import Any

import semantic_relation_ownership
from query_structurer import semantic_ir, semantic_outcome


_MESSAGE = "요청한 이력 조건이 현재 월별 스냅샷 적재 범위를 벗어납니다."


def _has_direct_semantic_ir_assignment(function: object) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            raw_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            targets.extend(raw_targets)
        for target in targets:
            if not isinstance(target, ast.Subscript):
                continue
            if not isinstance(target.value, ast.Name) or target.value.id != "payload":
                continue
            if isinstance(target.slice, ast.Constant) and target.slice.value == "semantic_ir":
                return True
    return False


def test_coverage_gap_uses_typed_outcome_and_central_writer(monkeypatch: Any) -> None:
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

    outcome_calls: list[dict[str, Any]] = []
    writer_calls: list[dict[str, Any]] = []
    real_outcome_factory = semantic_outcome.SemanticOutcome.unsupported
    real_writer = semantic_ir.write_semantic_ir

    class TrackingSemanticOutcome:
        @classmethod
        def unsupported(cls, **kwargs: Any) -> semantic_outcome.SemanticOutcome:
            outcome_calls.append(copy.deepcopy(kwargs))
            return real_outcome_factory(**kwargs)

    def tracking_writer(payload: dict[str, Any], projection: dict[str, Any]) -> None:
        writer_calls.append(copy.deepcopy(projection))
        real_writer(payload, projection)

    monkeypatch.setattr(semantic_outcome, "SemanticOutcome", TrackingSemanticOutcome)
    monkeypatch.setattr(semantic_ir, "write_semantic_ir", tracking_writer)

    payload: dict[str, Any] = {"semantic_ir": {"status": "resolved"}}
    returned = semantic_relation_ownership.project_relation_data_coverage(
        payload, "지난달 말 기준 VIP", {}
    )

    assert returned == [gap]
    assert outcome_calls == [
        {
            "operations": [
                {
                    "kind": "data_coverage_gap",
                    "reason": "requested history predates complete coverage",
                    "evidence": "지난달 말 기준",
                }
            ],
            "message": _MESSAGE,
        }
    ]
    assert writer_calls == [payload["semantic_ir"]]
    assert payload["semantic_ir"]["status"] == "unsupported"
    assert payload["semantic_ir"]["failure_kind"] == "unsupported"
    assert not _has_direct_semantic_ir_assignment(
        semantic_relation_ownership.project_relation_data_coverage
    )


def test_no_coverage_gap_does_not_replace_the_existing_outcome(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        semantic_relation_ownership,
        "relation_data_coverage_gaps",
        lambda *_args: [],
    )
    monkeypatch.setattr(
        semantic_ir,
        "write_semantic_ir",
        lambda *_args: (_ for _ in ()).throw(AssertionError("writer must not be called")),
    )
    existing = {"status": "resolved"}
    payload: dict[str, Any] = {"semantic_ir": existing}

    assert semantic_relation_ownership.project_relation_data_coverage(
        payload, "현재 VIP", {}
    ) == []
    assert payload["semantic_ir"] is existing
