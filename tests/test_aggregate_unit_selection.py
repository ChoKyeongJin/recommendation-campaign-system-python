"""절의 임계값 단위 선택 — 기간 표현 안의 '개'는 수량 단위가 아니다.

'3개월'의 '개'가 수량 단위로 잡히면 그 절의 지표가 '상품 수량'으로 확정되고, 옆에 있던 남의 숫자
('인구 50만')가 그 임계값 자리에 들어간다. 여기서 고정하는 것은 그 결합의 **입구**다.

이 계약은 임시 정규식 가드로 먼저 세워졌고, typed unit tokenizer 가 그 자리를 대신한 뒤에도
그대로 성립해야 한다 — 그래서 테스트는 가드가 아니라 결과(단위 선택)를 본다.
"""

from __future__ import annotations

import pytest

import graph_rag


@pytest.mark.parametrize(
    ("clause", "unit"),
    [
        ("3개 이상 구매", "개"),
        ("상품 5개", "개"),
        ("3건 이상", "건"),
        ("5,000개 이상", "개"),
        ("10만원 이상", "원"),
        ("3종 이상", "종"),
    ],
)
def test_quantity_units_are_still_selected(clause: str, unit: str) -> None:
    assert graph_rag._clause_primary_unit(clause) == unit


@pytest.mark.parametrize("clause", ["2개월 이내", "3개월 동안", "3개년", "6개월 누적"])
def test_duration_tokens_do_not_yield_a_quantity_unit(clause: str) -> None:
    assert graph_rag._clause_primary_unit(clause) != "개"


def test_a_duration_and_a_quantity_in_one_clause_pick_the_quantity() -> None:
    """'최근 3개월 동안 3개 이상 구매' — 기간은 창이고, 단위는 수량이다."""
    assert graph_rag._clause_primary_unit("최근 3개월 동안 3개 이상 구매") == "개"


def test_duration_still_parses_as_a_time_window() -> None:
    """'차단'은 기간 해석을 막으라는 뜻이 아니다 — duration 은 여전히 기간으로 살아 있어야 한다."""
    plan = graph_rag.build_query_plan("최근 3개월 동안 구매하지 않은 고객", parser="rules")
    assert plan["target_user"]["purchase_inactivity"]["min_days"] == 90
