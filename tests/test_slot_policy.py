"""슬롯 소유권·백스톱 계약.

이행에서 지켜야 할 두 가지를 못 박는다.

  1. **승격은 근거로만.** 슬롯을 LLM 에 넘기는 것은 누적 관찰이 위험 등급별 문턱을 넘고 위험 판정이
     0 건일 때만 가능하다. 감으로 넘기면 조용히 0명이 되는 슬롯이 생긴다.
  2. **조용한 소실 금지.** LLM 이 소유한 슬롯은 백스톱이 있거나 fail-close 여야 한다. 둘 다 없으면
     결함이며, 최소한 세어져야 한다(개수 래칫).
"""

from __future__ import annotations

import ir_snapshot
import parser_shadow
import pytest
import slot_policy

# 조용한 소실이 남아 있는 슬롯의 상한. 7단계의 종료 조건은 이 값이 0 이 되는 것이다.
SILENT_LOSS_CEILING = 2


def test_unregistered_slot_defaults_to_the_safe_side() -> None:
    """등록을 잊은 슬롯이 조용히 LLM 으로 넘어가면 안 된다."""
    policy = slot_policy.policy_for("target_user.mystery_slot_that_does_not_exist")
    assert policy["owner"] == slot_policy.RULE
    assert policy["risk"] == slot_policy.HIGH
    assert policy["backstop"] == slot_policy.BACKSTOP_RULE


def test_policy_vocabularies_are_closed() -> None:
    for slot in slot_policy.registered_slots():
        policy = slot_policy.policy_for(slot)
        assert policy["owner"] in slot_policy.OWNERS
        assert policy["risk"] in slot_policy.RISKS
        assert policy["backstop"] in slot_policy.BACKSTOPS


def test_risk_gates_get_stricter_with_risk() -> None:
    low, medium, high = (slot_policy.PROMOTION_GATES[r] for r in (slot_policy.LOW, slot_policy.MEDIUM, slot_policy.HIGH))
    assert low.min_observations < medium.min_observations < high.min_observations
    assert low.min_agreement < medium.min_agreement < high.min_agreement


def test_high_risk_slot_is_not_promoted_on_thin_evidence() -> None:
    """관찰 20건 100% 일치라도 high 위험 슬롯은 승격되지 않는다(문턱 200건)."""
    agreement = {"target_user.purchase_date": {"observed": 20, "agree": 20, "rate": 1.0, "risky": 0}}
    row = next(r for r in slot_policy.promotion_report(agreement) if r["slot"] == "target_user.purchase_date")
    assert row["eligible"] is False
    assert any("observations" in reason for reason in row["blocked_by"])


def test_any_risky_verdict_blocks_promotion() -> None:
    """일치율이 문턱을 넘어도 조건 소실(only_baseline)이나 값 불일치가 있으면 승격 금지."""
    agreement = {"plan.result_limit": {"observed": 500, "agree": 499, "rate": 0.998, "risky": 1}}
    row = next(r for r in slot_policy.promotion_report(agreement) if r["slot"] == "plan.result_limit")
    assert row["eligible"] is False
    assert any("risky_verdicts" in reason for reason in row["blocked_by"])


def test_low_risk_slot_promotes_on_sufficient_clean_evidence() -> None:
    agreement = {"plan.result_limit": {"observed": 40, "agree": 40, "rate": 1.0, "risky": 0}}
    row = next(r for r in slot_policy.promotion_report(agreement) if r["slot"] == "plan.result_limit")
    assert row["eligible"] is True
    assert row["blocked_by"] == []


def test_enforce_never_erases_a_condition(monkeypatch) -> None:
    """후보가 값을 못 만든 칸(only_baseline)은 채택 대상이 아니다 — 소실이 가장 위험한 실패다."""
    monkeypatch.setenv("PARSER_SHADOW_MODE", parser_shadow.MODE_ENFORCE)
    comparison = {"slots": {
        "target_user.purchase_object": {"verdict": parser_shadow.ONLY_BASELINE, "baseline": "기저귀"},
    }}
    assert slot_policy.resolve_enforced_slots(comparison) == {}


def test_enforce_adopts_only_llm_owned_slots(monkeypatch) -> None:
    monkeypatch.setenv("PARSER_SHADOW_MODE", parser_shadow.MODE_ENFORCE)
    comparison = {"slots": {
        "target_user.purchase_object": {"verdict": parser_shadow.ONLY_CANDIDATE, "candidate": "기저귀"},
        "target_user.purchase_date": {"verdict": parser_shadow.ONLY_CANDIDATE, "candidate": {"from": "20190301"}},
    }}
    adopted = slot_policy.resolve_enforced_slots(comparison)
    assert adopted == {"target_user.purchase_object": "기저귀"}  # purchase_date 는 규칙 소유라 제외


def test_shadow_mode_adopts_nothing(monkeypatch) -> None:
    """shadow 는 관찰만 한다 — 이 약속이 깨지면 관찰이 곧 사고다."""
    monkeypatch.setenv("PARSER_SHADOW_MODE", parser_shadow.MODE_SHADOW)
    comparison = {"slots": {
        "target_user.purchase_object": {"verdict": parser_shadow.ONLY_CANDIDATE, "candidate": "기저귀"},
    }}
    assert slot_policy.resolve_enforced_slots(comparison) == {}


def test_silent_loss_slots_are_documented_and_capped() -> None:
    """백스톱도 fail-close 도 없는 슬롯은 반드시 사유가 있고, 수가 늘지 않는다."""
    losses = slot_policy.silent_loss_slots()
    for slot, gap in losses.items():
        assert gap.strip(), f"[{slot}] 조용한 소실인데 사유(gap)가 없다 — 세어지지 않으면 고쳐지지 않는다"
    assert len(losses) <= SILENT_LOSS_CEILING, (
        f"조용한 소실 슬롯이 늘었다: {sorted(losses)} ({len(losses)} > {SILENT_LOSS_CEILING}).\n"
        f"LLM 소유 슬롯은 백스톱(rule) 또는 fail_close 를 선언해야 한다."
    )


def test_llm_owned_slots_are_known_condition_slots() -> None:
    """정책이 존재하지 않는 슬롯을 가리키면(오타·이름 변경) 승격 판정이 영원히 안 돈다."""
    known = set(ir_snapshot.KNOWN_CONDITION_PLAN_KEYS)
    for slot in slot_policy.registered_slots():
        container, _, name = slot.partition(".")
        assert container in {"plan", *ir_snapshot.CONTAINERS}, f"[{slot}] 알 수 없는 컨테이너"
        if container == "plan":
            assert name in known, f"[{slot}] plan 조건 키 목록에 없다(ir_snapshot.KNOWN_CONDITION_PLAN_KEYS)"


@pytest.mark.parametrize("risk", list(slot_policy.RISKS))
def test_every_risk_tier_has_a_gate(risk: str) -> None:
    assert risk in slot_policy.PROMOTION_GATES
