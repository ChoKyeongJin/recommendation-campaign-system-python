"""결정론 구제가 fail-close 게이트보다 **앞에서** 도는지 고정한다.

배경(2026-08-02 실측): `behavior_demotion.normalize_lapsed_purchase_pattern` 은 정확히
"최근 N개월 주문은 있었지만 최근 M일 구매가 없는" 문형을 위해 존재했고, 정규식도 그 문장에
매치됐고, 만들어 내는 합성도 정확했다. 그런데 호출 위치가 `_semantic_ir_blocking_sql_result`
게이트보다 **40줄 뒤**였다. 게이트가 먼저 return 하므로 구제는 한 번도 실행되지 않았다.

이런 결함은 동작이 아니라 **순서**에 있어서, 어떤 단위 테스트도 잡지 못한다. 그래서 순서 자체를
불변식으로 고정한다:

  1. 원문만 읽어 슬롯을 채우는 결정론 정규화기는 게이트보다 먼저 호출된다.
  2. 그 정규화기는 자기가 읽은 원문 구간을 남긴다(누가 이 어구를 읽었는지 묻는 감사 입력).

2026-08-05 삭제된 순서 불변식 2개가 있었다: '구제가 의미 파이프라인보다 먼저'와 '집계 강등은
파이프라인 뒤'. 둘 다 SemanticPlanV2 파이프라인의 존재를 전제했고, 그 파이프라인과 그것이
쓰던 슬롯(aggregate_conditions 등)의 생산자가 폐기되면서 고정할 순서 자체가 사라졌다.
"""

from __future__ import annotations

import inspect
import re
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import behavior_demotion  # noqa: E402
import graph_rag  # noqa: E402
import slot_ownership  # noqa: E402

_LAPSED_PROMPT = "최근 3개월 주문은 있었지만 최근 30일간 구매가 없는 회원을 추출해서 이탈방지 캠페인을 만들어줘."
_REFERENCE_DATE = date(2026, 3, 31)


def _build_sql_result_source() -> str:
    return inspect.getsource(graph_rag.build_sql_result)


def _line_of(pattern: str, source: str) -> int:
    match = re.search(pattern, source)
    assert match is not None, f"build_sql_result 에서 {pattern!r} 를 찾지 못했다"
    return source[: match.start()].count("\n")


def test_source_only_rescue_runs_before_the_fail_close_gate() -> None:
    """원문만 읽는 결정론 구제는 semantic_ir 게이트보다 먼저 돌아야 한다."""
    source = _build_sql_result_source()
    rescue = _line_of(r"normalize_lapsed_purchase_pattern\(", source)
    gate = _line_of(r"_semantic_ir_blocking_sql_result\(", source)
    assert rescue < gate, (
        "결정론 구제가 fail-close 게이트 뒤에 있다 — 게이트가 먼저 return 하므로 구제는 "
        f"영영 실행되지 않는다(구제 {rescue}행, 게이트 {gate}행)."
    )


def test_rescue_records_the_source_span_it_read() -> None:
    """구제는 자기가 읽은 구간을 남긴다 — 소유 판정이 '누가 이 어구를 읽었나'를 물을 곳."""
    plan: dict = {"target_user": {"behaviors": ["no_purchase", "cart_abandoner"]}}
    changed = behavior_demotion.normalize_lapsed_purchase_pattern(
        plan,
        source_text=_LAPSED_PROMPT,
        reference_date=_REFERENCE_DATE,
    )
    assert changed, "이탈 문형이 매치되지 않았다"
    assert plan["target_user"]["purchase_membership"] == {
        "operator": "exists",
        "window": {
            "type": "relative",
            "value": 3,
            "unit": "months",
            "from": "20260101",
            "to": "20260331",
        },
    }
    assert plan["target_user"]["purchase_inactivity"] == {
        "window": {
            "type": "relative",
            "value": 30,
            "unit": "days",
            "from": "20260302",
            "to": "20260331",
        }
    }
    assert plan["target_user"]["behaviors"] == ["cart_abandoner"], "평생 무구매는 창 조건과 모순이다"

    # 청구는 **절 단위**다 — 매치 전체를 청구하면 두 절 사이에 낀 무관한 조건까지 삼킨다.
    expected = {
        "purchase_membership": ("주문", "있었지만"),
        "purchase_inactivity": ("구매", "없"),
    }
    for slot, markers in expected.items():
        span = slot_ownership.slot_span(plan, slot)
        assert span is not None, f"{slot} 의 출처 구간이 기록되지 않았다"
        assert span["source"] == _LAPSED_PROMPT
        assert all(marker in span["text"] for marker in markers), (slot, span["text"])
    assert slot_ownership.slot_span(plan, "purchase_membership")["text"] != (
        slot_ownership.slot_span(plan, "purchase_inactivity")["text"]
    ), "두 슬롯이 같은 구간을 청구하면 절 단위 청구가 아니다"


def test_rescue_does_not_overwrite_values_the_structurer_already_produced() -> None:
    """fill-if-empty — 구조화기가 확정한 창을 결정론 창으로 덮지 않는다."""
    plan: dict = {
        "target_user": {
            "purchase_inactivity": {"value": 45, "unit": "days", "min_days": 45},
        }
    }
    behavior_demotion.normalize_lapsed_purchase_pattern(
        plan,
        source_text=_LAPSED_PROMPT,
        reference_date=_REFERENCE_DATE,
    )
    assert plan["target_user"]["purchase_inactivity"]["min_days"] == 45
    assert plan["target_user"]["purchase_membership"]["window"] == {
        "type": "relative",
        "value": 3,
        "unit": "months",
        "from": "20260101",
        "to": "20260331",
    }


def test_calendar_rescue_fails_closed_without_an_injected_reference_date() -> None:
    plan: dict = {"target_user": {"behaviors": ["no_purchase"]}}

    changed = behavior_demotion.normalize_lapsed_purchase_pattern(
        plan,
        source_text=_LAPSED_PROMPT,
    )

    assert changed == []
    assert plan == {"target_user": {"behaviors": ["no_purchase"]}}


def test_six_calendar_months_are_not_flattened_to_180_days() -> None:
    query = "최근 6개월 주문은 있었지만 최근 30일간 구매가 없는 회원"
    plan: dict = {}

    behavior_demotion.normalize_lapsed_purchase_pattern(
        plan,
        source_text=query,
        reference_date=_REFERENCE_DATE,
    )

    window = plan["target_user"]["purchase_membership"]["window"]
    assert window == {
        "type": "relative",
        "value": 6,
        "unit": "months",
        "from": "20251001",
        "to": "20260331",
    }


def test_calendar_year_is_not_flattened_to_365_days_across_a_leap_day() -> None:
    query = "최근 1년 주문은 있었지만 최근 30일간 구매가 없는 회원"
    plan: dict = {}

    behavior_demotion.normalize_lapsed_purchase_pattern(
        plan,
        source_text=query,
        reference_date=date(2024, 2, 29),
    )

    window = plan["target_user"]["purchase_membership"]["window"]
    assert window == {
        "type": "relative",
        "value": 1,
        "unit": "years",
        "from": "20230301",
        "to": "20240229",
    }
