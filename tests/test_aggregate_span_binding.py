"""단위 토큰·비교 스팬·값↔단위 인접성 — '어디서 온 숫자인가'를 코드가 답할 수 있는지.

여기서 검증하는 것은 표면 결과가 아니라 **스팬**이다. 값이 맞더라도 스팬이 틀리면 다음 단계의
인접성·소유권 판정이 전부 틀린다(그게 '3개월'의 '개'가 수량 단위가 된 경위다).
"""

from __future__ import annotations

import pytest

import aggregate_parser_config as config
import aggregate_spans as spans
import graph_rag


@pytest.fixture(scope="module")
def rules() -> config.AggregateParserRules:
    return config.rules()


# ── 1. 단위 토큰(longest-match + priority) ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "surface", "kind", "span"),
    [
        ("3개", "개", "quantity", (1, 2)),
        ("3건", "건", "count", (1, 2)),
        ("5,000개", "개", "quantity", (5, 6)),
        ("3개월", "개월", "duration", (1, 3)),
        ("3개년", "개년", "duration", (1, 3)),
        ("10만원", "원", "currency", (3, 4)),
        ("10만 원", "원", "currency", (4, 5)),
        ("100평", "평", "area", (3, 4)),
    ],
)
def test_unit_token_surface_kind_and_span(rules, text, surface, kind, span) -> None:
    tokens = spans.find_unit_tokens(text, rules)
    assert [(t.surface, t.kind, (t.span.start, t.span.end)) for t in tokens] == [(surface, kind, span)]


def test_duration_token_does_not_emit_an_inner_quantity_token(rules) -> None:
    """'3개월' 안의 '개' 가 별도 수량 토큰으로 생기지 않는다 — 이것이 임시 regex 가드의 대체물이다."""
    for text in ("3개월 동안", "3개년 누적"):
        assert all(token.kind == "duration" for token in spans.find_unit_tokens(text, rules))


# ── 2. 비교 스팬 ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "operator", "value", "value_text", "comparison_text"),
    [
        ("50만원 이상", ">=", 500000.0, "50만", "50만원 이상"),
        ("5,000개 이상", ">=", 5000.0, "5,000", "5,000개 이상"),
        ("나이 30 이상", ">=", 30.0, "30", "30 이상"),
        ("10만 원 이상", ">=", 100000.0, "10만", "10만 원 이상"),
    ],
)
def test_comparison_span_separates_value_from_operator(
    text, operator, value, value_text, comparison_text,
) -> None:
    candidates = graph_rag._parse_amount_comparison_candidates(text, graph_rag._AGG_UNIT)
    assert candidates is not None and len(candidates) == 1
    candidate = candidates[0]
    assert candidate.operator == operator
    assert candidate.normalized_value == value
    assert candidate.value_span.text == value_text
    assert candidate.comparison_span.text == comparison_text
    # end 는 슬라이스와 같은 exclusive 인덱스다.
    assert text[candidate.value_span.start:candidate.value_span.end] == value_text


def test_dual_bound_keeps_a_span_per_bound() -> None:
    candidates = graph_rag._parse_amount_comparison_candidates("나이 30 이상 40 미만", graph_rag._AGG_UNIT)
    assert [(c.operator, c.normalized_value, c.value_span.text) for c in candidates] == [
        (">=", 30.0, "30"), ("<", 40.0, "40"),
    ]


def test_compat_wrapper_returns_the_legacy_tuples() -> None:
    assert graph_rag._parse_amount_comparison("50만원 이상", graph_rag._AGG_UNIT) == [(">=", 500000.0)]
    assert graph_rag._parse_amount_comparison("없는 문장", graph_rag._AGG_UNIT) is None


# ── 3. 값 ↔ 단위 인접성 ────────────────────────────────────────────────────────────────
def _bind(text: str, rules):
    candidates = graph_rag._parse_amount_comparison_candidates(text, graph_rag._AGG_UNIT) or []
    spans.bind_units(text, candidates, spans.find_unit_tokens(text, rules), rules)
    return candidates


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("50만원 이상", "원"),
        ("10만 원 이상", "원"),
        ("3개 이상", "개"),
        ("5,000개 이상", "개"),
        ("나이 30 이상", None),
        ("구매금액 100000 이상", None),
    ],
)
def test_units_bind_only_when_adjacent(rules, text, expected) -> None:
    candidates = _bind(text, rules)
    assert candidates, text
    bound = candidates[0].unit_ref
    assert (bound.surface if bound else None) == expected


def test_distant_unit_is_not_bound(rules) -> None:
    """'50만 이상인 도시중에 3개월' 의 '개월' 은 50만의 단위가 아니다(사이에 다른 문장이 있다)."""
    text = "인구가 50만 이상인 도시중에 3개월 동안 구매내역 없는 사람"
    candidates = _bind(text, rules)
    assert candidates and candidates[0].value_span.text == "50만"
    assert candidates[0].unit_ref is None


def test_one_unit_token_has_one_owner(rules) -> None:
    text = "구매금액 10만원 이상 상품 3개 이상"
    candidates = _bind(text, rules)
    bound = [(c.value_span.text, c.unit_ref.surface if c.unit_ref else None) for c in candidates]
    assert ("10만", "원") in bound


def test_zero_whitespace_policy_rejects_a_gap(tmp_path, rules) -> None:
    """허용 공백을 0 으로 낮추면 '10만 원' 이 결합되지 않는다 — 정책이 코드가 아니라 설정에 있다."""
    import json

    payload = json.loads(
        (config.DEFAULT_RULES_PATH).read_text(encoding="utf-8")
    )
    payload["span_binding"]["max_unit_whitespace"] = 0
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    strict = config.load_rules(path=path)
    candidates = graph_rag._parse_amount_comparison_candidates("10만 원 이상", graph_rag._AGG_UNIT) or []
    spans.bind_units("10만 원 이상", candidates, spans.find_unit_tokens("10만 원 이상", strict), strict)
    assert candidates[0].unit_ref is None
