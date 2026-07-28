"""골든 프롬프트 → 조건 IR 스냅샷.

이 스위트가 지키는 것은 SQL 문자열이 아니라 **해석 결과**다(:mod:`ir_snapshot`). 컴파일러·방언·
조인 순서를 바꿔도 같은 조건을 읽어냈으면 그린이다. 그래서 정규식을 렉시콘/LLM 으로 옮기는
이행 작업에서 안전망 역할을 할 수 있다.

세 층으로 단언한다.

  1. ``expect_slots`` — **명세**. 이 프롬프트가 반드시 만들어야 하는 조건. 현행 동작과 무관하게
     "올바른 해석"을 적는다.
  2. ``forbid_slots`` — 오탐 고정(날짜가 상품명으로 새는 류).
  3. 스냅샷 동등 — 회귀 감지. 값이 바뀌면 의도한 변경인지 사람이 확인하고 재생성한다.

``known_gap`` 이 달린 케이스는 현행 파서가 명세를 못 채우는 **알려진 결함**이다. 결함이 고쳐지면
테스트가 "마커를 지우라"고 실패한다 — 결함을 스냅샷으로 축복해버리는 사고를 막는다.
"""

from __future__ import annotations

import json

import pytest

import ir_snapshot
from golden_support import build_plan, load_cases, load_corpus, load_snapshot

CASES = load_cases()
PARSER = load_corpus().get("parser", "rules")
IDS = [case["id"] for case in CASES]


@pytest.fixture(scope="module")
def plans() -> dict[str, dict]:
    """코퍼스 전체를 한 번만 파싱한다(파싱이 이 스위트에서 가장 비싼 부분)."""
    return {case["id"]: build_plan(case["prompt"], parser=PARSER) for case in CASES}


def test_case_ids_are_unique() -> None:
    assert len(IDS) == len(set(IDS)), "케이스 id 가 중복되면 스냅샷 파일이 서로 덮어쓴다"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_expected_condition_slots_are_produced(case: dict, plans: dict[str, dict]) -> None:
    """명세: 이 프롬프트가 만들어야 하는 조건 슬롯이 실제로 만들어졌는가."""
    produced = ir_snapshot.condition_slots(plans[case["id"]])
    expected = set(case.get("expect_slots", []))
    gap = case.get("known_gap") or {}
    known_missing = set(gap.get("missing_slots", []))

    missing = expected - produced
    unexpectedly_fixed = known_missing - missing
    if unexpectedly_fixed:
        pytest.fail(
            f"[{case['id']}] known_gap 에 적힌 슬롯이 이제 생성된다: {sorted(unexpectedly_fixed)}.\n"
            f"cases.json 의 known_gap 을 지워 명세로 승격하라(결함이 고쳐진 것을 테스트가 계속 '결함'으로 두면 재발을 못 잡는다)."
        )

    still_missing = missing - known_missing
    assert not still_missing, (
        f"[{case['id']}] '{case['prompt']}' 에서 조건이 소실됐다: {sorted(still_missing)}\n"
        f"실제 생성된 슬롯: {sorted(produced)}\n"
        f"의도한 결함이면 cases.json 에 known_gap 으로 사유와 함께 적는다."
    )


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_forbidden_slots_are_not_produced(case: dict, plans: dict[str, dict]) -> None:
    """오탐 고정: 만들면 안 되는 슬롯이 새지 않았는가."""
    forbidden = set(case.get("forbid_slots", []))
    if not forbidden:
        pytest.skip("forbid_slots 없음")
    leaked = forbidden & ir_snapshot.condition_slots(plans[case["id"]])
    assert not leaked, f"[{case['id']}] '{case['prompt']}' 에서 조건이 잘못된 슬롯으로 샜다: {sorted(leaked)}"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_snapshot_matches_baseline(case: dict, plans: dict[str, dict]) -> None:
    """회귀 감지: 조건 IR 이 기준선과 같은가."""
    baseline = load_snapshot(case["id"])
    if baseline is None:
        pytest.fail(
            f"[{case['id']}] 기준 스냅샷이 없다. `python tools/regen_ir_goldens.py` 로 생성하고 내용을 검토한 뒤 커밋하라."
        )
    current = ir_snapshot.snapshot(plans[case["id"]])
    assert current == baseline, (
        f"[{case['id']}] '{case['prompt']}' 의 조건 IR 이 기준선과 다르다.\n"
        f"기대: {json.dumps(baseline, ensure_ascii=False, sort_keys=True)}\n"
        f"실제: {json.dumps(current, ensure_ascii=False, sort_keys=True)}\n"
        f"의도한 개선이면 `python tools/regen_ir_goldens.py` 로 갱신한다."
    )


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_snapshot_is_deterministic(case: dict) -> None:
    """같은 입력을 두 번 파싱하면 같은 IR 이어야 한다(비결정 요소가 섞이면 골든이 무의미해진다)."""
    first = ir_snapshot.snapshot(build_plan(case["prompt"], parser=PARSER))
    second = ir_snapshot.snapshot(build_plan(case["prompt"], parser=PARSER))
    assert first == second, f"[{case['id']}] 같은 프롬프트가 두 번 다른 IR 을 냈다"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_resolved_cases_do_not_enter_the_unresolved_queue(case: dict, plans: dict[str, dict]) -> None:
    """미해석 탐지기가 정상 프롬프트를 잡으면 주간 큐가 잡음으로 덮여 루프가 죽는다.

    코퍼스는 전부 '해석돼야 하는' 프롬프트이므로, 여기서 한 건이라도 잡히면 탐지기가 너무 넓다.
    """
    import unresolved_triage

    reason = unresolved_triage.detect_no_condition(plans[case["id"]], case["prompt"])
    assert reason is None, f"[{case['id']}] '{case['prompt']}' 가 미해석으로 오탐됐다: {reason}"


def test_known_gaps_carry_a_reason() -> None:
    """결함 마커는 사유를 적어야 한다 — 사유 없는 마커는 그냥 숨긴 실패다."""
    for case in CASES:
        gap = case.get("known_gap")
        if gap is None:
            continue
        assert gap.get("missing_slots"), f"[{case['id']}] known_gap 에 missing_slots 가 없다"
        assert (gap.get("reason") or "").strip(), f"[{case['id']}] known_gap 에 사유가 없다"
