"""골든 코퍼스 슬롯 계약 — expect_slots 가 실제 생산 가능한 슬롯 계층에 실재함을 오프라인으로 강제.

배경: cases.json 의 expect_slots/forbid_slots 는 '올바른 해석'의 선언이었지만 어떤 테스트도 읽지
않는 주석이었다. 그 사이 실사고가 났다 — TOP N PERCENT 컴파일러는 존재하는데 V4 strict 스키마에
member_metric_ranking 슬롯이 노출되지 않아 LLM 이 도달할 수 없었다('상위 10%' 2026-08-01 실패).
이 파일은 그 드리프트(컴파일러는 있는데 스키마 미노출/슬롯 오타/사장된 슬롯)를 LLM 없이 잡는다.

여기서 강제하는 계약:
  1. 코퍼스 크기 하한(표본이 줄면 게이트가 공허해진다).
  2. 모든 expect_slot 은 소유 계층 정확히 하나에 속한다 —
     LLM 노출(strict tool 스키마) / 앱 소유(결정론 백필) / 컴파일러 파생(V4 밖, plan_schema 조건).
  3. target_user.* 기대 슬롯은 전부 LLM strict 스키마에 노출돼 있다(미노출=도달 불가 사고 재발).
  4. 컴파일러 파생 목록은 하향 전용 래칫이다 — 늘리려면 V4 에 노출하거나 여기 사유를 적어라.
  5. forbid_slots 중 SLOT_SHAPES 에 실재하는 것은 V4 에도 노출돼 있다(죽은 슬롯을 금지 목록이
     가리키면 오탐 고정이 무의미해진다).

여기서 실패하면 프로덕션 프롬프트가 아니라 CI 가 먼저 알려 준다.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import plan_schema  # noqa: E402
import targeting_ir  # noqa: E402
from query_structurer.campaign_plan_v4 import (  # noqa: E402
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA,
    _APPLICATION_OWNED_PLAN_FIELDS,
)

from golden_support import load_cases  # noqa: E402

# LLM 이 채울 수 있는 슬롯(strict tool 스키마 노출).
_EXPOSED_TARGET_USER = frozenset(
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"]["target_user"]["properties"]
)
_EXPOSED_PLAN_ROOT = frozenset(CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA["properties"])

# 앱 소유: LLM 스키마에서 제거되고 결정론 생산자(백필 등)가 채우는 plan 키.
_APP_OWNED_PLAN = frozenset(_APPLICATION_OWNED_PLAN_FIELDS)

# 컴파일러 파생: V4 스키마 밖에서 파서/컴파일러가 실제로 생산하는 plan 조건 키. 하향 전용 래칫.
#   dimension_filters: 값 인덱스/차원 해석기가 생산(지역·브랜드 코드 변환).
_COMPILER_OWNED_PLAN = frozenset({
    "dimension_filters",
})

# 지향 슬롯: 골든 케이스가 '올바른 해석'으로 선언했지만 **현재 생산자가 배선돼 있지 않은** plan 키.
# 규칙 계층 철거로 생산자가 사라졌다(event_expression 빌더는 등록만 존재, canonical_targeting_expression
# 은 대입 코드 자체가 없다). 생산자가 생기면 아래 가드 테스트가 빨개지고, 그때 _COMPILER_OWNED_PLAN
# 으로 승격하라 — 그 전까지 이 기대는 어떤 계층의 테스트도 검증하지 않는 지향 선언임을 명시한다.
_ASPIRATIONAL_PLAN = frozenset({
    "event_expression",
    "canonical_targeting_expression",
})


def _split_slot(slot: str) -> tuple[str, str]:
    container, _, name = slot.partition(".")
    return container, name


def _expect_slots() -> set[str]:
    return {slot for case in load_cases() for slot in case.get("expect_slots", [])}


def _forbid_slots() -> set[str]:
    return {slot for case in load_cases() for slot in case.get("forbid_slots", [])}


def test_corpus_keeps_minimum_size() -> None:
    """표본이 줄면 이 계약 전체가 공허해진다 — 케이스 삭제는 의도적 결정이어야 한다."""
    assert len(load_cases()) >= 19, "골든 코퍼스가 19케이스 밑으로 줄었다. 삭제가 의도라면 하한도 함께 조정하라."


def test_every_expect_slot_is_owned_by_exactly_one_tier() -> None:
    """expect_slot 오타·사장된 슬롯 탐지: 어느 계층에도 없거나 두 계층에 걸치면 실패."""
    problems: list[str] = []
    for slot in sorted(_expect_slots()):
        container, name = _split_slot(slot)
        if container == "target_user":
            tiers = {"llm"} if name in _EXPOSED_TARGET_USER else set()
        elif container == "plan":
            tiers = set()
            if name in _EXPOSED_PLAN_ROOT:
                tiers.add("llm")
            if name in _APP_OWNED_PLAN:
                tiers.add("app")
            if name in _COMPILER_OWNED_PLAN:
                tiers.add("compiler")
            if name in _ASPIRATIONAL_PLAN:
                tiers.add("aspirational")
        else:
            problems.append(f"{slot}: 알 수 없는 컨테이너 '{container}'")
            continue
        if len(tiers) != 1:
            problems.append(
                f"{slot}: 소유 계층 {sorted(tiers) or '없음'} — 오타이거나 사장된 슬롯이다. "
                "V4 스키마 노출/앱 소유 목록/_COMPILER_OWNED_PLAN 중 정확히 한 곳에 있어야 한다."
            )
    assert not problems, "\n".join(problems)


def test_target_user_expect_slots_are_llm_exposed() -> None:
    """'컴파일러는 있는데 strict 스키마 미노출'(도달 불가) 드리프트의 직접 가드."""
    missing = {
        slot for slot in _expect_slots()
        if _split_slot(slot)[0] == "target_user"
        and _split_slot(slot)[1] not in _EXPOSED_TARGET_USER
    }
    assert not missing, (
        f"골든 코퍼스가 기대하는 target_user 슬롯이 V4 LLM 스키마에 노출돼 있지 않다: {sorted(missing)}. "
        "targeting_ir.SLOT_SHAPES 등록 + campaign_plan_v4 _TARGET_USER_SCHEMA 배선을 확인하라."
    )


def test_compiler_owned_tier_is_a_ratchet() -> None:
    """컴파일러 파생 계층은 늘어나면 안 된다 — 새 슬롯은 V4 노출이 기본이고, 예외는 사유와 함께."""
    unknown = (_COMPILER_OWNED_PLAN | _ASPIRATIONAL_PLAN) - plan_schema.names(plan_schema.CONDITION)
    assert not unknown, (
        f"컴파일러/지향 계층에 plan_schema 조건 선언이 없는 이름이 있다: {sorted(unknown)}. "
        "고아 슬롯이다 — plan_schema 에 선언하거나 목록에서 빼라."
    )


def test_aspirational_slots_still_have_no_producer() -> None:
    """지향 슬롯의 '생산자 없음' 전제를 고정한다 — 생산 대입 코드가 생기면 이 테스트가 빨개지고,
    그때 해당 슬롯을 _COMPILER_OWNED_PLAN 으로 승격해 골든 기대가 실제로 검증되게 하라."""
    import re

    sources = list(REPO_ROOT.glob("*.py")) + list((REPO_ROOT / "query_structurer").glob("*.py"))
    produced: dict[str, list[str]] = {}
    for slot in sorted(_ASPIRATIONAL_PLAN):
        pattern = re.compile(r"\[(?P<q>[\"'])" + re.escape(slot) + r"(?P=q)\]\s*=[^=]")
        hits = [
            source.name for source in sources
            if pattern.search(source.read_text(encoding="utf-8"))
        ]
        if hits:
            produced[slot] = hits
    assert not produced, (
        f"지향 슬롯에 생산자가 생겼다: {produced}. _COMPILER_OWNED_PLAN 으로 승격하고 "
        "_ASPIRATIONAL_PLAN 에서 제거해 골든 기대가 실제 검증되게 하라."
    )


def test_forbid_slots_that_exist_are_exposed() -> None:
    """금지 목록이 죽은 슬롯을 가리키면 오탐 고정이 무의미하다 — 실재 슬롯만 노출 검증한다."""
    shaped = {shape.name for shape in targeting_ir.structured_slot_shapes() if shape.container == "target_user"}
    stale = {
        slot for slot in _forbid_slots()
        if _split_slot(slot)[0] == "target_user"
        and _split_slot(slot)[1] in shaped
        and _split_slot(slot)[1] not in _EXPOSED_TARGET_USER
    }
    assert not stale, (
        f"forbid_slots 가 SLOT_SHAPES 에는 있는데 V4 에 노출되지 않은 슬롯을 가리킨다: {sorted(stale)}. "
        "노출이 끊겼다면 금지 자체가 공허하다 — 노출을 복구하거나 케이스를 갱신하라."
    )
