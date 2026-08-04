"""단위 토큰·비교 스팬·값↔단위 인접성 — '어디서 온 숫자인가'를 코드가 답할 수 있는지.

여기서 검증하는 것은 표면 결과가 아니라 **스팬**이다. 값이 맞더라도 스팬이 틀리면 다음 단계의
인접성·소유권 판정이 전부 틀린다(그게 '3개월'의 '개'가 수량 단위가 된 경위다).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import aggregate_parser_config as config
import aggregate_spans as spans
import aggregate_parser_config
import graph_rag


# 단위 어휘의 단일 소스는 docs/data/runtime/language/aggregate_parser_rules.json 이다
# (옛 graph_rag._AGG_UNIT 대체).
_UNIT_ALTERNATION = aggregate_parser_config.unit_alternation(aggregate_parser_config.rules())


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
    candidates = graph_rag._parse_amount_comparison_candidates(text, _UNIT_ALTERNATION)
    assert candidates is not None and len(candidates) == 1
    candidate = candidates[0]
    assert candidate.operator == operator
    assert candidate.normalized_value == value
    assert candidate.value_span.text == value_text
    assert candidate.comparison_span.text == comparison_text
    # end 는 슬라이스와 같은 exclusive 인덱스다.
    assert text[candidate.value_span.start:candidate.value_span.end] == value_text


@pytest.mark.parametrize(
    ("text", "operator", "value", "value_text"),
    [
        ("최소 3회 구매", ">=", 3.0, "3"),
        ("적어도 3회 구매", ">=", 3.0, "3"),
        ("적어도 10만원 구매", ">=", 100000.0, "10만"),
        ("최대 5건", "<=", 5.0, "5"),
    ],
)
def test_prefix_comparators_bind_the_number_that_follows(
    rules, text, operator, value, value_text,
) -> None:
    """숫자 **앞**의 비교어는 aggregate_parser_rules.json 의 prefix_operators 가 소유한다.

    '적어도'는 공통 정규화 사전(normalization_lexicon)에만 있고 이 표에는 없어서 같은 말이
    경로마다 다르게 읽혔다(2026-08-04 이관). 이 계층(scan_comparisons)이 그 표의 유일한
    소비자다 — graph_rag 의 실행 문법은 접미 비교어만 보므로 여기서 재는 것이 정확하다.
    """
    candidates = spans.scan_comparisons(
        text, rules,
        operator_alternation=graph_rag._COMPARISON_OP_ALT,
        operator_normalizer=graph_rag._comparison_operator,
    )
    assert len(candidates) == 1
    assert (candidates[0].operator, candidates[0].normalized_value, candidates[0].value_span.text) == (
        operator, value, value_text,
    )


def test_native_korean_numeral_after_a_prefix_comparator_is_not_a_threshold(rules) -> None:
    """'적어도 한 달'의 '한'은 아직 값이 아니다 — 고유어 수사는 숫자 스캐너가 읽지 못한다.

    말없이 1 로 읽거나 '적어도'만 살려 반쪽 조건을 만들지 않는다(무추측). 고유어 수사가
    임계값이 되는 것은 별도 작업이며, 그때 이 기대값이 바뀌는 것이 그 선언이다.
    """
    assert not spans.scan_comparisons(
        "적어도 한 달", rules,
        operator_alternation=graph_rag._COMPARISON_OP_ALT,
        operator_normalizer=graph_rag._comparison_operator,
    )


# ── 3. 고유어 수관형사 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "operator", "value"),
    [
        ("정확히 세 번 주문", "=", 3.0),
        ("정확히 두 개 구매", "=", 2.0),
        ("정확히 네 종류 담은", "=", 4.0),
        ("한 번 이상 구매", ">=", 1.0),
        ("다섯 개 이상", ">=", 5.0),
    ],
)
def test_native_numeral_bound_to_a_counter_is_a_value(text, operator, value) -> None:
    """'세 번'의 '세'는 3이다. 표는 aggregate_parser_rules.json 의 number_words 가 소유한다.

    이관 전에는 값 문법이 아라비아 숫자만 알아서 '정확히 세 번 주문한 회원'의 임계값이 통째로
    사라졌다(고유어 수사 표는 엔티티 개수 전용으로 semantic_requirements 안에만 있었다).
    """
    assert graph_rag._parse_amount_comparison(text, _UNIT_ALTERNATION) == [(operator, value)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # '세'가 낱말 내부 음절 — 값이 아니고, 옆의 금액 비교는 그대로 살아 있어야 한다.
        ("세금 10만원 이상", [(">=", 100000.0)]),
        ("적어도 한 달", None),        # 기간 단위는 계수 단위가 아니다(창은 calendar_window 소유)
        ("두세 개 이상 구매", None),   # 어림수(2~3) — 확정할 수 없으므로 잡지 않는다
        ("서너 개 이상", None),        # 선언되지 않은 수사 — 비슷하다고 추측하지 않는다
        ("열 개 이상", None),          # 선언되지 않은 수사
    ],
)
def test_unbound_or_undeclared_numerals_are_not_values(text, expected) -> None:
    """수관형사는 계수 단위에 결속될 때만 값이다 — 아니면 조용히 1로 읽지 않고 값이 아니다."""
    assert graph_rag._parse_amount_comparison(text, _UNIT_ALTERNATION) == expected


def test_native_numeral_grammar_is_shared_by_both_scanners(rules) -> None:
    """숫자 표기 문법의 소유자는 설정 하나다 — 스캐너마다 다른 '숫자'를 알면 소유권이 어긋난다."""
    number = aggregate_parser_config.number_pattern(rules)
    assert aggregate_parser_config.ARABIC_NUMBER in number
    assert "세" in number and "다섯" in number
    candidates = spans.scan_comparisons(
        "세 번 이상", rules,
        operator_alternation=graph_rag._COMPARISON_OP_ALT,
        operator_normalizer=graph_rag._comparison_operator,
    )
    assert [(c.operator, c.normalized_value) for c in candidates] == [(">=", 3.0)]


def test_decimal_threshold_keeps_source_precision(rules) -> None:
    candidates = spans.scan_comparisons(
        "0.1234567890123456789원 이상",
        rules,
        operator_alternation=graph_rag._COMPARISON_OP_ALT,
        operator_normalizer=graph_rag._comparison_operator,
    )

    assert candidates[0].normalized_value == Decimal("0.1234567890123456789")
    assert isinstance(candidates[0].normalized_value, Decimal)


def test_dual_bound_keeps_a_span_per_bound() -> None:
    candidates = graph_rag._parse_amount_comparison_candidates("나이 30 이상 40 미만", _UNIT_ALTERNATION)
    assert [(c.operator, c.normalized_value, c.value_span.text) for c in candidates] == [
        (">=", 30.0, "30"), ("<", 40.0, "40"),
    ]


def test_compat_wrapper_returns_the_legacy_tuples() -> None:
    assert graph_rag._parse_amount_comparison("50만원 이상", _UNIT_ALTERNATION) == [(">=", 500000.0)]
    assert graph_rag._parse_amount_comparison("없는 문장", _UNIT_ALTERNATION) is None


# ── 3. 값 ↔ 단위 인접성 ────────────────────────────────────────────────────────────────
def _bind(text: str, rules):
    candidates = graph_rag._parse_amount_comparison_candidates(text, _UNIT_ALTERNATION) or []
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
    candidates = graph_rag._parse_amount_comparison_candidates("10만 원 이상", _UNIT_ALTERNATION) or []
    spans.bind_units("10만 원 이상", candidates, spans.find_unit_tokens("10만 원 이상", strict), strict)
    assert candidates[0].unit_ref is None
