"""출처(provenance) 계약 — 모든 조건은 '누가·무엇으로' 만들었는지 답할 수 있어야 한다.

이 계약이 없으면 이행(정규식 → 렉시콘 → LLM)을 안전하게 못 한다. shadow 비교에서 값이 갈렸을 때
어느 경로가 만든 값인지 귀속할 수 없고, 슬롯 단위로 LLM-first 를 켜는 전환도 근거 없이 하게 된다.

강제하는 것:

  1. 살아 있는 모든 조건 슬롯에 생산자 기록이 있다(출처 없는 조건 금지).
  2. 모든 생산자 이름이 방법(rule/lexicon/llm/structured/stage/policy)으로 분류된다.
     — 새 파서 경로를 열면서 이름 규약을 안 지키면 여기서 걸린다.
  3. 원문에서 읽은 조건은 방법이 파싱 계열이다(파생 스테이지가 조건을 만들어내지 않는다).
  4. **래칫**: 코퍼스 전체의 ``rule``(코드 정규식) 슬롯 수가 기준선을 넘지 않는다.
     이행의 방향(rule↓ / lexicon·llm↑)을 테스트가 지킨다.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

import provenance
from golden_support import BASELINE_PATH, build_plan, load_cases, load_corpus

CASES = load_cases()
PARSER = load_corpus().get("parser", "rules")
IDS = [case["id"] for case in CASES]


@pytest.fixture(scope="module")
def plans() -> dict[str, dict]:
    return {case["id"]: build_plan(case["prompt"], parser=PARSER) for case in CASES}


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_every_producer_is_classified(case: dict, plans: dict[str, dict]) -> None:
    unknown = provenance.unknown_producers(plans[case["id"]])
    assert not unknown, (
        f"[{case['id']}] 방법을 분류할 수 없는 생산자: {unknown}\n"
        f"provenance.PRODUCER_PREFIXES 의 이름 규약을 따르거나 PRODUCER_OVERRIDES 에 등록하라."
    )


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_expected_slots_have_provenance(case: dict, plans: dict[str, dict]) -> None:
    """명세가 요구한 슬롯은 출처 기록을 갖는다(생성됐지만 출처가 없는 조건 금지)."""
    records = provenance.slot_provenance(plans[case["id"]])
    gap = set((case.get("known_gap") or {}).get("missing_slots", []))
    for slot in case.get("expect_slots", []):
        if slot in gap:
            continue
        assert slot in records, f"[{case['id']}] 조건 슬롯 {slot} 에 출처 기록이 없다"
        record = records[slot]
        assert record["method"] in provenance.METHODS, f"[{case['id']}] {slot} 의 방법이 어휘 밖: {record['method']}"
        assert record["producer"], f"[{case['id']}] {slot} 의 생산자 이름이 비어 있다"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_at_least_one_condition_comes_from_parsing(case: dict, plans: dict[str, dict]) -> None:
    """모든 프롬프트는 원문 해석으로 만든 조건을 최소 하나 갖는다(전부 파생이면 아무것도 못 읽은 것)."""
    mix = provenance.method_mix(plans[case["id"]])
    assert sum(mix.values()) > 0, (
        f"[{case['id']}] '{case['prompt']}' 에서 원문 해석으로 만든 조건이 하나도 없다 — 파서가 프롬프트를 통째로 놓쳤다."
    )


def test_method_vocabulary_is_closed() -> None:
    for _, method in provenance.PRODUCER_PREFIXES:
        assert method in provenance.METHODS
    for name, method in provenance.PRODUCER_OVERRIDES.items():
        assert method in provenance.METHODS, f"{name} 의 방법이 어휘 밖: {method}"


def test_unnamespaced_producer_is_a_stage() -> None:
    """맨 함수 이름은 스테이지, 미지의 네임스페이스는 UNKNOWN(조용히 stage 로 접히면 지표가 틀린다)."""
    assert provenance.method_of("apply_something") == provenance.STAGE
    assert provenance.method_of("filter:age") == provenance.RULE
    assert provenance.method_of("normalization:vip") == provenance.LEXICON
    assert provenance.method_of("llm:purchase_object") == provenance.LLM
    assert provenance.method_of("mystery:thing") == provenance.UNKNOWN


def test_rule_share_does_not_regress(plans: dict[str, dict]) -> None:
    """래칫: 코퍼스 전체의 rule(코드 정규식) 조건 수가 기준선을 넘지 않는다.

    이행 작업(어휘형 정규식 → 렉시콘)이 진행되면 이 수치는 내려간다. 올라간다는 것은 새 표현을
    또 코드로 받았다는 뜻이므로, 기준선을 올리려면 사유를 남기고 명시적으로 갱신해야 한다.
    """
    if not BASELINE_PATH.exists():
        pytest.fail("method_mix 기준선이 없다. `python tools/regen_ir_goldens.py` 로 생성하고 커밋하라.")
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    total: Counter = Counter()
    for plan in plans.values():
        total.update(provenance.method_mix(plan))

    recorded = baseline.get("methods", {})
    assert total[provenance.RULE] <= recorded.get(provenance.RULE, 0), (
        f"코드 정규식으로 만든 조건이 늘었다: {total[provenance.RULE]} > 기준선 {recorded.get(provenance.RULE, 0)}.\n"
        f"새 표현은 렉시콘(데이터) 또는 LLM 슬롯으로 받는 것이 기본이다. 정규식이 불가피하면 사유와 함께 "
        f"`python tools/regen_ir_goldens.py` 로 기준선을 갱신하라.\n"
        f"현재 구성: {dict(total)}"
    )
