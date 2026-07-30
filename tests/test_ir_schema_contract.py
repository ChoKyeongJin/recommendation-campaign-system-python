"""조건 IR 스키마 계약 — 새 조건 슬롯이 분류 없이 조용히 들어오는 것을 막는다.

리팩터가 위험한 이유는 슬롯이 사라져서만이 아니다. **분류되지 않은 슬롯이 늘어나는 것**도 같은
문제다 — 그 슬롯은 confidence 에도, 빌더 소유권 검사에도, 감사 로그 해석에도 걸리지 않은 채
플랜 안을 떠돈다. 여기서 강제하는 계약:

  1. plan 최상위 키는 조건/파생/비조건 중 하나로 **선언**돼 있어야 한다(:mod:`ir_snapshot`).
  2. 스냅샷은 빈 슬롯을 담지 않는다(선초기화가 조건으로 보이면 안 된다).
  3. 스냅샷은 JSON 직렬화 가능하고 키 순서에 무관하게 동등해야 한다(골든 비교의 전제).
"""

from __future__ import annotations

import json

import pytest
from golden_support import build_plan, load_cases, load_corpus

import ir_snapshot
import plan_decisions

CASES = load_cases()
PARSER = load_corpus().get("parser", "rules")
IDS = [case["id"] for case in CASES]


@pytest.fixture(scope="module")
def plans() -> dict[str, dict]:
    return {case["id"]: build_plan(case["prompt"], parser=PARSER) for case in CASES}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_every_plan_key_is_classified(case: dict, plans: dict[str, dict]) -> None:
    """미분류 plan 최상위 키가 없어야 한다."""
    unclassified = ir_snapshot.unclassified_plan_keys(plans[case["id"]])
    assert not unclassified, (
        f"[{case['id']}] 분류되지 않은 plan 키가 조건 IR 로 흘러들었다: {unclassified}\n"
        f"ir_snapshot.KNOWN_CONDITION_PLAN_KEYS(조건) 또는 DERIVED_PLAN_KEYS(파생)에 등록하라."
    )


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_snapshot_has_no_empty_slots(case: dict, plans: dict[str, dict]) -> None:
    """빈 슬롯은 스냅샷에 들어가지 않는다(선초기화 잡음 제거)."""
    snapshot = ir_snapshot.snapshot(plans[case["id"]])
    for container, slots in snapshot.items():
        if container == "schema_version":
            continue
        for slot, value in slots.items():
            assert value not in (None, [], {}, "", False), f"[{case['id']}] 빈 슬롯이 스냅샷에 들어갔다: {container}.{slot}"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_snapshot_is_json_serializable(case: dict, plans: dict[str, dict]) -> None:
    """골든 파일로 왕복 가능해야 한다 — 직렬화 불가 값이 IR 에 있으면 스냅샷을 못 고정한다."""
    snapshot = ir_snapshot.snapshot(plans[case["id"]])
    restored = json.loads(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    assert restored == snapshot, f"[{case['id']}] 조건 IR 이 JSON 왕복에서 달라졌다"


def test_snapshot_carries_schema_version() -> None:
    assert ir_snapshot.snapshot({})["schema_version"] == ir_snapshot.SCHEMA_VERSION


def test_containers_track_the_audit_source() -> None:
    """컨테이너 목록이 감사 로그와 갈라지면 '감사에는 잡히는데 IR 에는 없는' 슬롯이 생긴다."""
    assert ir_snapshot.CONTAINERS == plan_decisions.AUDITED_CONTAINERS


def test_derived_and_condition_key_sets_are_disjoint() -> None:
    """같은 키를 조건이자 파생으로 선언하면 분류가 무의미해진다."""
    overlap = ir_snapshot.DERIVED_PLAN_KEYS & ir_snapshot.KNOWN_CONDITION_PLAN_KEYS
    assert not overlap, f"조건과 파생에 동시에 선언된 키: {sorted(overlap)}"


def test_canonical_expression_is_a_condition_but_execution_receipts_are_derived() -> None:
    assert "canonical_targeting_expression" in ir_snapshot.KNOWN_CONDITION_PLAN_KEYS
    receipts = {
        "canonical_projection",
        "canonical_targeting_validation",
        "canonical_targeting_version",
        "condition_claims",
        "event_compiler_capability",
        "event_semantic_validation",
        "ownership_reconciliation_complete",
    }
    assert receipts <= ir_snapshot.DERIVED_PLAN_KEYS
    assert not (receipts & ir_snapshot.KNOWN_CONDITION_PLAN_KEYS)


def test_empty_plan_snapshots_to_version_only() -> None:
    """조건이 하나도 없으면 스냅샷은 버전만 남는다(빈 컨테이너 키가 생기지 않는다)."""
    assert ir_snapshot.snapshot({"target_user": {}, "exclude": {}}) == {"schema_version": ir_snapshot.SCHEMA_VERSION}
