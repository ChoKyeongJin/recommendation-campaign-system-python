"""extractor 단독 테스트.

각 extractor 는 자기 축만 본다는 계약이 있으므로, 테스트도 축 단위로 격리한다. 여기서
파이프라인 전체 결과를 검증하면 어느 계층이 깨졌는지 알 수 없게 된다.

입력은 **정규화가 끝난 문자열**이다(파이프라인에서 그 순서로 도달한다).
"""

from __future__ import annotations

from datetime import date

import pytest

from nl_event_ir.aliases import AliasRegistry, load_alias_registry
from nl_event_ir.enums import (
    ComparisonOperator,
    EntityType,
    EventType,
    LogicOperator,
    MetricType,
    Polarity,
    TimeUnit,
)
from nl_event_ir.extractors import (
    ComparisonExtractor,
    CountExtractor,
    EventExtractor,
    LogicExtractor,
    PolarityExtractor,
    ScopeExtractor,
    TargetExtractor,
    TemporalExtractor,
)
from nl_event_ir.models import AbsoluteDateRange, MetricCondition, RelativeTimeWindow
from nl_event_ir.tokenizer import SemanticToken, TokenKind

FIXED_TODAY = date(2026, 8, 5)


@pytest.fixture(scope="module")
def registry() -> AliasRegistry:
    return load_alias_registry()


def _values(tokens: list[SemanticToken], kind: TokenKind) -> list[object]:
    return [token.value for token in tokens if token.kind is kind]


# ── EventExtractor ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("상품을 구매한 고객", EventType.PURCHASE),
        ("상품을 구입한 고객", EventType.PURCHASE),
        ("상품을 주문한 고객", EventType.PURCHASE),
        ("상품을 결제한 고객", EventType.PURCHASE),
        ("상품을 사다 고객", EventType.PURCHASE),
        ("로그인한 회원", EventType.LOGIN),
        ("접속한 회원", EventType.LOGIN),
        ("방문한 회원", EventType.LOGIN),
        ("장바구니에 담은 회원", EventType.CART),
        ("가입한 회원", EventType.SIGNUP),
        ("회원가입한 사용자", EventType.SIGNUP),
    ],
)
def test_event_surface_variants_map_to_one_canonical_event(
    registry: AliasRegistry, text: str, expected: EventType
) -> None:
    tokens = EventExtractor(registry).extract(text)
    assert expected in _values(tokens, TokenKind.EVENT)


def test_longer_event_alias_wins_over_contained_one(registry: AliasRegistry) -> None:
    """``회원가입`` 이 ``가입`` 을 포함한다. 같은 자리를 두 번 읽으면 조건이 중복된다."""

    tokens = EventExtractor(registry).extract("회원가입한 사용자")
    assert len(tokens) == 1
    assert tokens[0].raw_text == "회원가입"


# ── TemporalExtractor ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("최근 7일", RelativeTimeWindow(7, TimeUnit.DAY)),
        ("최근 3개월", RelativeTimeWindow(3, TimeUnit.MONTH)),
        ("지난 2주", RelativeTimeWindow(2, TimeUnit.WEEK)),
        ("최근 1년", RelativeTimeWindow(1, TimeUnit.YEAR)),
        ("최근 3개월 동안", RelativeTimeWindow(3, TimeUnit.MONTH)),
        ("30일 이내", RelativeTimeWindow(30, TimeUnit.DAY)),
    ],
)
def test_relative_time_windows(
    registry: AliasRegistry, text: str, expected: RelativeTimeWindow
) -> None:
    tokens = TemporalExtractor(registry).extract(text)
    assert _values(tokens, TokenKind.TIME_WINDOW) == [expected]


def test_relative_window_consumes_its_tail(registry: AliasRegistry) -> None:
    """``동안`` 을 남기면 잔여 구간이 엔티티 후보로 오인된다."""

    tokens = TemporalExtractor(registry).extract("최근 3개월 동안")
    assert tokens[0].raw_text == "최근 3개월 동안"


def test_bare_duration_without_tail_is_not_a_window(registry: AliasRegistry) -> None:
    """``3개월 할부`` 의 3개월은 조회 기간이 아니다."""

    tokens = TemporalExtractor(registry).extract("3개월 할부로 결제")
    assert _values(tokens, TokenKind.TIME_WINDOW) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("오늘", AbsoluteDateRange(date(2026, 8, 5), date(2026, 8, 5))),
        ("어제", AbsoluteDateRange(date(2026, 8, 4), date(2026, 8, 4))),
        ("이번 달", AbsoluteDateRange(date(2026, 8, 1), date(2026, 8, 31))),
        ("지난 달", AbsoluteDateRange(date(2026, 7, 1), date(2026, 7, 31))),
    ],
)
def test_deictic_dates_use_injected_reference(
    registry: AliasRegistry, text: str, expected: AbsoluteDateRange
) -> None:
    extractor = TemporalExtractor(registry, reference_date=FIXED_TODAY)
    assert _values(extractor.extract(text), TokenKind.DATE_RANGE) == [expected]


def test_deictic_month_crosses_year_boundary(registry: AliasRegistry) -> None:
    extractor = TemporalExtractor(registry, reference_date=date(2026, 1, 15))
    assert _values(extractor.extract("지난 달"), TokenKind.DATE_RANGE) == [
        AbsoluteDateRange(date(2025, 12, 1), date(2025, 12, 31))
    ]


def test_deictic_needs_reference_date(registry: AliasRegistry) -> None:
    """기준 날짜가 없으면 ``오늘`` 의 뜻이 없다. 지어내지 않는다."""

    assert TemporalExtractor(registry).extract("오늘") == []


def test_iso_date_range(registry: AliasRegistry) -> None:
    tokens = TemporalExtractor(registry).extract("2026-07-01부터 2026-07-31까지")
    assert _values(tokens, TokenKind.DATE_RANGE) == [
        AbsoluteDateRange(date(2026, 7, 1), date(2026, 7, 31))
    ]


def test_impossible_date_is_not_invented(registry: AliasRegistry) -> None:
    """달력에 없는 날짜로 구간을 지어내지 않는다."""

    tokens = TemporalExtractor(registry).extract("2026-13-45부터 2026-14-01까지")
    assert _values(tokens, TokenKind.DATE_RANGE) == []


def test_impossible_date_is_preserved_as_unparsed(registry: AliasRegistry) -> None:
    """조용히 버리면 기간 조건이 사라져 조회가 전체 기간으로 넓어진다."""

    tokens = TemporalExtractor(registry).extract("2026-02-30부터 2026-03-01까지")
    unparsed = [token for token in tokens if token.kind is TokenKind.UNPARSED]
    assert len(unparsed) == 1
    assert unparsed[0].raw_text == "2026-02-30부터 2026-03-01까지"


# ── CountExtractor ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2회 이상", MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.GTE, 2)),
        ("3건 이하", MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.LTE, 3)),
        ("정확히 1번", MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.EQ, 1)),
        ("5회 초과", MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.GT, 5)),
        ("4건 미만", MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.LT, 4)),
        ("한 번도", MetricCondition(MetricType.EVENT_COUNT, ComparisonOperator.EQ, 0)),
    ],
)
def test_count_conditions(
    registry: AliasRegistry, text: str, expected: MetricCondition
) -> None:
    tokens = CountExtractor(registry).extract(text)
    assert _values(tokens, TokenKind.COUNT_CONDITION) == [expected]


def test_bare_count_without_comparison_is_not_a_condition(registry: AliasRegistry) -> None:
    """``상품 3개를 담은`` 의 3개는 조건이 아니라 수량이다. 연산자를 지어내지 않는다."""

    assert CountExtractor(registry).extract("상품 3개를 담은") == []


# ── ComparisonExtractor ──────────────────────────────────────────────────────────


def test_amount_condition_uses_decimal(registry: AliasRegistry) -> None:
    from decimal import Decimal

    tokens = ComparisonExtractor(registry).extract("금액이 100000원 이상")
    aggregate = _values(tokens, TokenKind.AGGREGATE)
    assert aggregate == [
        MetricCondition(MetricType.SUM, ComparisonOperator.GTE, Decimal("100000"))
    ]
    assert isinstance(aggregate[0].value, Decimal)


def test_amount_multiplier(registry: AliasRegistry) -> None:
    from decimal import Decimal

    tokens = ComparisonExtractor(registry).extract("10만원 이상")
    assert _values(tokens, TokenKind.AGGREGATE) == [
        MetricCondition(MetricType.SUM, ComparisonOperator.GTE, Decimal("100000"))
    ]


def test_assumed_aggregation_is_marked_with_low_confidence(registry: AliasRegistry) -> None:
    """집계 표지가 없으면 가정이 들어간다. 그 사실이 신뢰도로 남아야 한다."""

    tokens = ComparisonExtractor(registry).extract("금액이 100000원 이상")
    assert tokens[0].confidence < 1.0


def test_explicit_aggregation_marker_is_certain(registry: AliasRegistry) -> None:
    tokens = ComparisonExtractor(registry).extract("평균 50000원 이상")
    aggregate = [token for token in tokens if token.kind is TokenKind.AGGREGATE]
    assert aggregate[0].confidence == 1.0
    assert aggregate[0].value.metric_type is MetricType.AVERAGE


# ── Logic / Polarity ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("아디다스 또는 나이키", LogicOperator.OR),
        ("아디다스 혹은 나이키", LogicOperator.OR),
        ("구매 그리고 로그인", LogicOperator.AND),
        ("구매 및 로그인", LogicOperator.AND),
    ],
)
def test_logic_connectives(
    registry: AliasRegistry, text: str, expected: LogicOperator
) -> None:
    tokens = LogicExtractor(registry).extract(text)
    assert expected in _values(tokens, TokenKind.LOGIC)


@pytest.mark.parametrize(
    "text",
    ["구매하지 않은", "구매 이력이 없는", "미구매", "구매한 적 없음"],
)
def test_negation_is_detected(registry: AliasRegistry, text: str) -> None:
    tokens = PolarityExtractor(registry).extract(text)
    assert Polarity.NEGATIVE in _values(tokens, TokenKind.POLARITY)


def test_negation_prefix_binds_to_event_only(registry: AliasRegistry) -> None:
    """``미`` 는 사건 앞에 있을 때만 부정이다. ``미국``·``미세`` 를 부정으로 읽으면 안 된다."""

    assert PolarityExtractor(registry).extract("미국 상품") == []


def test_negation_adverb_requires_standalone_word(registry: AliasRegistry) -> None:
    """``안`` 이 ``안양`` 의 첫 음절일 때 부정으로 읽히면 지역 조건이 뒤집힌다."""

    assert PolarityExtractor(registry).extract("안양시 고객") == []


# ── Target / Scope ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text", ["회원", "고객", "사용자", "유저", "고객님", "사람", "가입자"]
)
def test_target_nouns(registry: AliasRegistry, text: str) -> None:
    tokens = TargetExtractor(registry).extract(text)
    assert _values(tokens, TokenKind.TARGET) == [EntityType.MEMBER]


def test_scope_extractor_reads_entity_types_not_members(registry: AliasRegistry) -> None:
    tokens = ScopeExtractor(registry).extract("브랜드 상품 카테고리 고객")
    assert _values(tokens, TokenKind.ENTITY_TYPE) == [
        EntityType.BRAND,
        EntityType.PRODUCT,
        EntityType.CATEGORY,
    ]


def test_residual_entity_candidate_needs_no_dictionary(registry: AliasRegistry) -> None:
    """브랜드명은 사전에 없다. 아무도 읽지 않은 자리가 후보가 된다."""

    text = "나이키를 구매한 고객"
    claimed = EventExtractor(registry).extract(text) + TargetExtractor(registry).extract(text)
    candidates = ScopeExtractor(registry).extract_residual(text, claimed)
    assert [token.raw_text for token in candidates] == ["나이키"]


def test_residual_candidate_strips_particles(registry: AliasRegistry) -> None:
    text = "아디다스와 뉴발란스를 구매"
    claimed = EventExtractor(registry).extract(text)
    candidates = ScopeExtractor(registry).extract_residual(text, claimed)
    assert [token.raw_text for token in candidates] == ["아디다스", "뉴발란스"]


@pytest.mark.parametrize("brand", ["진로", "새로", "제로", "미로", "이도"])
def test_particle_stripping_spares_short_brands(
    registry: AliasRegistry, brand: str
) -> None:
    """조사와 같은 음절로 끝나는 두 글자 브랜드를 잘라 없애면 안 된다.

    ``진로 구매한 고객`` 에서 끝 음절 ``로`` 를 조사로 보면 ``진`` 만 남고, 한 글자는 후보
    자격을 잃어 브랜드가 통째로 사라진다. 조사가 실제로 붙은 ``진로를`` 은 절삭되어야 한다.
    """

    bare = f"{brand} 구매한 고객"
    with_particle = f"{brand}를 구매한 고객"
    for text in (bare, with_particle):
        claimed = EventExtractor(registry).extract(text) + TargetExtractor(registry).extract(
            text
        )
        candidates = ScopeExtractor(registry).extract_residual(text, claimed)
        assert brand in [token.raw_text for token in candidates], text


def test_residual_skips_word_internal_remainders(registry: AliasRegistry) -> None:
    """``로그인하지`` 의 ``하지`` 는 값이 아니라 어미다."""

    text = "로그인하지 않은 회원"
    claimed = EventExtractor(registry).extract(text) + TargetExtractor(registry).extract(text)
    candidates = ScopeExtractor(registry).extract_residual(text, claimed)
    assert "하지" not in [token.raw_text for token in candidates]


def test_residual_candidates_are_not_yet_confident(registry: AliasRegistry) -> None:
    text = "나이키를 구매"
    candidates = ScopeExtractor(registry).extract_residual(
        text, EventExtractor(registry).extract(text)
    )
    assert all(token.confidence < 1.0 for token in candidates)


# ── 전역 상태 없음 ───────────────────────────────────────────────────────────────


def test_extractors_share_no_state(registry: AliasRegistry) -> None:
    """같은 입력을 두 번 넣으면 같은 결과가 나와야 한다(누적 상태 금지)."""

    extractor = CountExtractor(registry)
    assert extractor.extract("2회 이상") == extractor.extract("2회 이상")
