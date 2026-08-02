"""재방출 계약 — 미해결 요구사항만, 패치로, 정책 횟수만큼.

강제하는 것:
  - 패치 요청에는 **미해결** 요구사항만 담긴다(미지원·내부 사고는 재방출로 고쳐지지 않는다).
  - 이미 통과한 요구사항의 노드는 교체·삭제되지 않는다(보호 집합).
  - 요청 구간 밖 노드는 병합되지 않는다(조용한 전체 재해석 금지).
  - 라운드 수는 정책값이고, 환경변수로도 조정된다.
  - 한 라운드가 회귀를 만들면 그 라운드를 통째로 되돌린다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import requirement_ledger as ledger_mod  # noqa: E402
import semantic_plan  # noqa: E402
import semantic_reemission as reemit  # noqa: E402
from requirement_ledger import Requirement, RequirementLedger  # noqa: E402


def _requirement(requirement_id: str, outcome: str, span: str = "", **validation) -> Requirement:
    return Requirement(
        requirement_id=requirement_id,
        label="조건",
        source_span=span or requirement_id,
        validation={"outcome": outcome, **validation},
    )


def _plan(query: str, nodes: list[dict]):
    return semantic_plan.plan_from_dict({"nodes": nodes}, source_query=query)


def _node(node_id: str, span: str, **values) -> dict:
    return {
        "id": node_id, "type": "aggregate_predicate", "source_span": span,
        "scope": "cart", "metric": "cart_line_count", "operator": ">=", "value": 2,
        **values,
    }


# ── 패치 요청의 대상 ─────────────────────────────────────────────────────────────
def test_patch_request_targets_only_unresolved_requirements() -> None:
    ledger = RequirementLedger([
        _requirement("done", ledger_mod.COMPILED, "이미 된 조건"),
        _requirement("pending", ledger_mod.PENDING, "덜 된 조건"),
        _requirement("unsupported", ledger_mod.UNSUPPORTED, "안 되는 조건"),
        _requirement("failed", ledger_mod.FAILED, "터진 조건"),
        _requirement("uncovered", ledger_mod.UNCOVERED, "안 읽힌 조건"),
    ])
    request = reemit.build_patch_request(ledger, policy=reemit.ReemissionPolicy())
    assert set(request.spans) == {"덜 된 조건", "안 읽힌 조건"}


def test_patch_request_respects_the_span_cap() -> None:
    ledger = RequirementLedger([
        _requirement(f"r{index}", ledger_mod.PENDING, f"조건{index}") for index in range(10)
    ])
    request = reemit.build_patch_request(
        ledger, policy=reemit.ReemissionPolicy(max_spans_per_round=3)
    )
    assert len(request.spans) == 3


def test_patch_request_carries_the_reason() -> None:
    ledger = RequirementLedger([
        _requirement("r1", ledger_mod.PENDING, "덜 된 조건", reason="임계값이 없다",
                     missing_fields=["value"]),
    ])
    request = reemit.build_patch_request(ledger, policy=reemit.ReemissionPolicy())
    assert request.targets[0]["reason"] == "임계값이 없다"
    assert request.targets[0]["missing_fields"] == ["value"]
    assert "임계값이 없다" in " ".join(reemit.patch_prompt_targets(request))


# ── 병합 규칙 ────────────────────────────────────────────────────────────────────
def test_patch_outside_the_requested_span_is_rejected() -> None:
    plan = _plan("A절, B절", [])
    patch = _plan("A절, B절", [_node("new", "B절")])
    accepted, rejected = reemit.apply_patch(
        plan, patch, request=reemit.PatchRequest(spans=("A절",)), protected_ids=frozenset()
    )
    assert accepted == [] and rejected == ["new"]
    assert plan.nodes == []


def test_protected_requirement_nodes_are_never_replaced() -> None:
    plan = _plan("A절", [_node("req-1", "A절")])
    patch = _plan("A절", [_node("req-1", "A절", value=99)])
    accepted, rejected = reemit.apply_patch(
        plan, patch, request=reemit.PatchRequest(spans=("A절",)),
        protected_ids=frozenset({"req-1"}),
    )
    assert accepted == [] and rejected == ["req-1"]
    assert plan.nodes[0].values["value"] == 2, "보호된 노드가 패치로 덮였다"


def test_unprotected_node_with_the_same_id_is_replaced_in_place() -> None:
    plan = _plan("A절", [_node("req-1", "A절")])
    patch = _plan("A절", [_node("req-1", "A절", value=99)])
    accepted, _ = reemit.apply_patch(
        plan, patch, request=reemit.PatchRequest(spans=("A절",)), protected_ids=frozenset()
    )
    assert accepted == ["req-1"]
    assert len(plan.nodes) == 1 and plan.nodes[0].values["value"] == 99
    assert plan.nodes[0].producer.endswith(":patch"), "패치 계보가 기록되지 않았다"


# ── 정책 ─────────────────────────────────────────────────────────────────────────
def test_zero_rounds_disables_reemission() -> None:
    calls: list[int] = []
    outcome = reemit.run(
        _plan("A절", []),
        RequirementLedger([_requirement("r1", ledger_mod.PENDING, "A절")]),
        request_patch=lambda _request: calls.append(1),
        rebuild=lambda _plan: RequirementLedger(),
        policy=reemit.ReemissionPolicy(max_rounds=0),
    )
    assert calls == [] and outcome.rounds == []


def test_policy_reads_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(reemit.MAX_ROUNDS_ENV, "3")
    assert reemit.ReemissionPolicy.from_env().max_rounds == 3
    monkeypatch.setenv(reemit.MAX_ROUNDS_ENV, "not-a-number")
    assert reemit.ReemissionPolicy.from_env().max_rounds == reemit.DEFAULT_MAX_ROUNDS


def test_rounds_stop_as_soon_as_the_ledger_is_complete() -> None:
    calls: list[int] = []

    def request_patch(_request):
        calls.append(1)
        return _plan("A절", [_node("r1", "A절")])

    complete = RequirementLedger([_requirement("r1", ledger_mod.COMPILED, "A절")])
    outcome = reemit.run(
        _plan("A절", []),
        RequirementLedger([_requirement("r1", ledger_mod.PENDING, "A절")]),
        request_patch=request_patch,
        rebuild=lambda _plan: complete,
        policy=reemit.ReemissionPolicy(max_rounds=5),
    )
    assert len(calls) == 1, "귀결된 뒤에도 재방출이 계속됐다"
    assert outcome.ledger.is_complete()


def test_a_round_that_regresses_is_reverted() -> None:
    """고치려다 망가뜨리면 그 라운드를 되돌린다 — 미해결 보고가 훼손보다 낫다."""
    plan = _plan("A절, B절", [_node("r1", "A절")])
    before = RequirementLedger([
        _requirement("r1", ledger_mod.COMPILED, "A절"),
        _requirement("r2", ledger_mod.PENDING, "B절"),
    ])
    after = RequirementLedger([
        _requirement("r1", ledger_mod.PENDING, "A절"),   # ← 회귀
        _requirement("r2", ledger_mod.COMPILED, "B절"),
    ])
    outcome = reemit.run(
        plan,
        before,
        request_patch=lambda _request: _plan("A절, B절", [_node("r2", "B절")]),
        rebuild=lambda _plan: after,
        policy=reemit.ReemissionPolicy(max_rounds=2),
    )
    assert outcome.rounds[-1].reverted
    assert "r1" in (outcome.rounds[-1].revert_reason or "")
    # 되돌렸으므로 원장은 라운드 전 상태다.
    assert outcome.ledger.by_id("r1").is_resolved


def test_patch_failure_falls_back_to_the_honest_deficit_report() -> None:
    def explode(_request):
        raise RuntimeError("boom")

    ledger = RequirementLedger([_requirement("r1", ledger_mod.PENDING, "A절")])
    outcome = reemit.run(
        _plan("A절", []), ledger, request_patch=explode,
        rebuild=lambda _plan: RequirementLedger(),
        policy=reemit.ReemissionPolicy(max_rounds=2),
    )
    assert outcome.ledger is ledger
    assert outcome.rounds[-1].revert_reason and "boom" in outcome.rounds[-1].revert_reason
