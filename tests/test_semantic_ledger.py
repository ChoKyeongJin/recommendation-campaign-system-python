"""요구 원장의 계약: **terminal disposition 없이는 출고하지 않는다.**

일부 의미가 누락된 SQL 을 HTTP 성공으로 돌려주던 상태(2026-08-06 실측 #19)를 막는 규칙이
여기 있다. 개수 비교가 아니라 귀결의 유무가 판정 기준이다.
"""

from __future__ import annotations

import pytest

import semantic_ledger as ledger_mod


def _requirement(kind: str, value: object = None) -> ledger_mod.SemanticRequirement:
    spans = (ledger_mod.EvidenceSpan(0, 3, "최근"),)
    return ledger_mod.SemanticRequirement(
        requirement_id=ledger_mod.requirement_id(
            canonical_kind=kind, spans=spans, typed_value=value
        ),
        canonical_kind=kind,
        typed_value=value,
        source_spans=spans,
    )


def test_unknown_disposition_is_rejected_at_construction() -> None:
    with pytest.raises(ledger_mod.LedgerContractError):
        ledger_mod.SemanticRequirement(
            requirement_id="x", canonical_kind="k", disposition="probably_fine"
        )


def test_open_requirement_blocks_shipping() -> None:
    ledger = ledger_mod.RequirementLedger()
    ledger.add(_requirement("temporal_window", {"amount": 30}))
    verdict = ledger_mod.verdict(ledger)
    assert not verdict.is_shippable
    assert verdict.reason_code == ledger_mod.REASON_OPEN_REQUIREMENTS
    assert verdict.missing_canonical_kinds == ("temporal_window",)


def test_every_terminal_disposition_ships() -> None:
    for disposition in sorted(ledger_mod.TERMINAL_DISPOSITIONS):
        ledger = ledger_mod.RequirementLedger()
        requirement = ledger.add(_requirement("source_condition", disposition))
        ledger.settle(requirement.requirement_id, disposition)
        assert ledger_mod.verdict(ledger).is_shippable, disposition


def test_first_settlement_wins() -> None:
    """귀결은 한 번만 정해진다 — 나중 단계가 미지원을 성공으로 덮어쓸 수 없다."""

    ledger = ledger_mod.RequirementLedger()
    requirement = ledger.add(_requirement("source_condition"))
    ledger.settle(requirement.requirement_id, ledger_mod.DISPOSITION_UNSUPPORTED)
    ledger.settle(requirement.requirement_id, ledger_mod.DISPOSITION_COMPILED)
    assert ledger.get(requirement.requirement_id).disposition == (
        ledger_mod.DISPOSITION_UNSUPPORTED
    )


def test_traceability_is_checked_only_when_asked() -> None:
    """compiled 인데 artifact 까지 못 가는 요구는 추적 검사에서만 걸린다."""

    ledger = ledger_mod.RequirementLedger()
    requirement = ledger.add(_requirement("source_condition"))
    ledger.settle(requirement.requirement_id, ledger_mod.DISPOSITION_COMPILED)
    assert ledger_mod.verdict(ledger).is_shippable
    strict = ledger_mod.verdict(ledger, require_traceability=True)
    assert not strict.is_shippable
    assert strict.reason_code == ledger_mod.REASON_TRACEABILITY
    assert strict.traceability_gaps[0]["missing_links"] == [
        "ir_node",
        "compile_receipt",
        "sql_artifact",
    ]


def test_full_trace_satisfies_the_strict_verdict() -> None:
    ledger = ledger_mod.RequirementLedger()
    requirement = ledger.add(_requirement("source_condition"))
    ledger.settle(
        requirement.requirement_id,
        ledger_mod.DISPOSITION_COMPILED,
        ir_node_ids=("ir_0",),
        compile_receipt_ids=("cr_0",),
        sql_artifact_ids=("sa_0",),
    )
    assert ledger_mod.verdict(ledger, require_traceability=True).is_shippable


def test_settle_open_closes_everything_left() -> None:
    ledger = ledger_mod.RequirementLedger()
    ledger.add(_requirement("a", 1))
    ledger.add(_requirement("b", 2))
    closed = ledger.settle_open(ledger_mod.DISPOSITION_CLARIFICATION, reason_code="asked")
    assert len(closed) == 2
    assert ledger.counts()["open"] == 0


def test_requirement_id_is_content_addressed() -> None:
    """같은 의미는 실행이 달라도 같은 id — 지문과 회귀 대조가 이 성질에 기댄다."""

    first = _requirement("temporal_window", {"amount": 30, "unit": "days"})
    second = _requirement("temporal_window", {"unit": "days", "amount": 30})
    assert first.requirement_id == second.requirement_id


def test_plan_round_trip_preserves_dispositions() -> None:
    plan: dict[str, object] = {}
    ledger = ledger_mod.RequirementLedger()
    requirement = ledger.add(_requirement("source_condition"))
    ledger.settle(
        requirement.requirement_id,
        ledger_mod.DISPOSITION_COMPILED,
        sql_artifact_ids=("sa_1",),
    )
    ledger_mod.write_ledger(plan, ledger)
    restored = ledger_mod.read_ledger(plan)
    assert restored.counts() == ledger.counts()
    assert restored.get(requirement.requirement_id).sql_artifact_ids == ("sa_1",)
