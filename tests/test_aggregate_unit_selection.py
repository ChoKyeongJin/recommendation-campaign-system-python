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


# ── typed tokenizer 가 임시 정규식 가드를 대체했다 ──────────────────────────────────────
def test_the_temporary_regex_guard_is_gone() -> None:
    """보호가 사라진 게 아니라 구조로 옮겨졌다 — 소스에 임시 가드가 남아 있으면 안 된다."""
    import inspect

    source = inspect.getsource(graph_rag)
    assert "(?![월년])" not in source
    assert not hasattr(graph_rag, "_AGG_UNIT_TOKEN_RE")


def test_duration_tokens_are_single_tokens_without_an_inner_quantity() -> None:
    import aggregate_parser_config as config
    import aggregate_spans as spans

    rules = config.rules()
    for text in ("3개월", "3개년"):
        tokens = spans.find_unit_tokens(text, rules)
        assert [(t.surface, t.kind) for t in tokens] == [(text[1:], "duration")]
    for text, surface in (("3개", "개"), ("5,000개", "개")):
        assert [(t.surface, t.kind) for t in spans.find_unit_tokens(text, rules)] == [(surface, "quantity")]


# ── 한 절의 복수 숫자·복수 단위가 각각 붙는다 ───────────────────────────────────────────
def _groups(clause: str):
    return graph_rag._clause_threshold_groups(clause, graph_rag._bind_threshold_candidates(clause))


def test_two_units_in_one_clause_bind_to_their_own_values() -> None:
    assert _groups("구매금액 10만원 이상 상품 3개 이상") == [
        ("원", [(">=", 100000.0)]),
        ("개", [(">=", 3.0)]),
    ]


def test_a_single_unit_clause_keeps_the_orphan_bound_merge() -> None:
    assert _groups("10만원 이상 50만 이하") == [("원", [(">=", 100000.0), ("<=", 500000.0)])]


def test_unitless_dual_bound_stays_one_group() -> None:
    assert _groups("나이 30 이상 40 미만") == [(None, [(">=", 30.0), ("<", 40.0)])]


def test_a_distant_duration_does_not_become_a_quantity_threshold() -> None:
    """원문 재현 문장: '50만'과 '3개월'은 결합되지 않고, 50만은 미지원 속성이 이미 소유했다."""
    assert _groups("인구가 50만 이상인 도시중에 3개월 동안 구매내역 없는 사람") == []


def test_two_conditions_in_one_sentence_compile_to_two_metrics() -> None:
    plan = graph_rag.build_query_plan("구매금액 10만원 이상이고 상품 3개 이상 구매한 고객", parser="rules")
    conditions = plan["target_user"]["aggregate_conditions"]
    assert {(c["metric_id"], c["threshold"]) for c in conditions} == {
        ("purchase_amount", 100000.0), ("total_item_quantity", 3.0),
    }


def test_a_duration_window_and_a_quantity_threshold_coexist() -> None:
    plan = graph_rag.build_query_plan("최근 3개월 동안 3개 이상 구매한 고객", parser="rules")
    condition = plan["target_user"]["aggregate_conditions"][0]
    assert (condition["metric_id"], condition["threshold"], condition["window_days"]) == (
        "total_item_quantity", 3.0, 90,
    )
