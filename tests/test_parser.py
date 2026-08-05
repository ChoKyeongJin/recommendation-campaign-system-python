"""파이프라인 통합 테스트 — 요구 사양의 문장들이 실제로 처리되는지 본다.

기준 시각은 고정한다(CLAUDE.md 44). ``오늘``·``이번 달`` 이 실제 현재 시각을 쓰면 테스트가
날짜에 따라 깨지거나, 더 나쁘게는 특정 날짜에만 통과한다.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from nl_event_ir import (
    ComparisonOperator,
    ConditionGroup,
    EntityCandidate,
    EntityType,
    EventCondition,
    EventType,
    InMemoryEntityRepository,
    MetricType,
    Polarity,
    RelativeTimeWindow,
    SortDirection,
    TimeUnit,
)
from nl_event_ir.aliases import load_alias_registry
from nl_event_ir.fallback import FallbackReason
from nl_event_ir.models import (
    AbsoluteDateRange,
    EventIR,
    IRDeserializationError,
    MetricCondition,
    ScopeFilter,
    event_ir_from_dict,
)
from nl_event_ir.normalizer import RuleBasedKoreanNormalizer
from nl_event_ir.parser import EventParser, build_default_parser
from nl_event_ir.tokenizer import TokenKind

FIXED_TODAY = date(2026, 8, 5)

CATALOG = [
    EntityCandidate(EntityType.BRAND, "brand:nike", "나이키", 1.0),
    EntityCandidate(EntityType.BRAND, "brand:adidas", "아디다스", 1.0),
    EntityCandidate(EntityType.BRAND, "brand:newbalance", "뉴발란스", 1.0),
    EntityCandidate(EntityType.BRAND, "brand:apple", "애플", 1.0),
    EntityCandidate(EntityType.PRODUCT, "product:apple", "애플", 1.0),
]


@pytest.fixture(scope="module")
def parser() -> EventParser:
    return build_default_parser(
        InMemoryEntityRepository(CATALOG), reference_date=FIXED_TODAY
    )


# ── 최소 동작 버전의 기준 문장 ────────────────────────────────────────────────────


def test_golden_sentence_produces_the_specified_ir(parser: EventParser) -> None:
    result = parser.parse("최근 3개월 동안 나이키를 두 번 이상 산 고객")

    assert result.normalized_text == "최근 3개월 동안 나이키를 2번 이상 사다 고객"
    assert result.fallback_required is False
    assert result.issues == ()
    assert result.ir == EventIR(
        target_entity=EntityType.MEMBER,
        condition=EventCondition(
            event_type=EventType.PURCHASE,
            polarity=Polarity.POSITIVE,
            time_window=RelativeTimeWindow(value=3, unit=TimeUnit.MONTH),
            scopes=(
                ScopeFilter(
                    entity_type=EntityType.BRAND,
                    operator=ComparisonOperator.EQ,
                    value="나이키",
                    resolved_id="brand:nike",
                    confidence=1.0,
                ),
            ),
            metric=MetricCondition(
                metric_type=MetricType.EVENT_COUNT,
                operator=ComparisonOperator.GTE,
                value=2,
            ),
        ),
    )


def test_golden_sentence_token_kinds(parser: EventParser) -> None:
    result = parser.parse("최근 3개월 동안 나이키를 두 번 이상 산 고객")
    assert [token.kind for token in result.tokens] == [
        TokenKind.TIME_WINDOW,
        TokenKind.ENTITY_VALUE,
        TokenKind.COUNT_CONDITION,
        TokenKind.EVENT,
        TokenKind.TARGET,
    ]


# ── 요구 사양의 정상 사례 ─────────────────────────────────────────────────────────


def test_negative_login_within_window(parser: EventParser) -> None:
    result = parser.parse("지난 7일 동안 로그인하지 않은 회원")
    condition = result.ir.condition

    assert isinstance(condition, EventCondition)
    assert condition.event_type is EventType.LOGIN
    assert condition.polarity is Polarity.NEGATIVE
    assert condition.time_window == RelativeTimeWindow(7, TimeUnit.DAY)


def test_amount_condition_in_current_month(parser: EventParser) -> None:
    result = parser.parse("이번 달 주문 금액이 100000원 이상인 고객")
    condition = result.ir.condition

    assert condition.time_window == AbsoluteDateRange(date(2026, 8, 1), date(2026, 8, 31))
    assert condition.metric == MetricCondition(
        MetricType.SUM, ComparisonOperator.GTE, Decimal("100000")
    )
    # 집계 표지가 없어 가정이 들어갔다는 사실이 결과에 남아 있어야 한다.
    assert {issue.code for issue in result.issues} == {"metric.aggregation_assumed"}


def test_disjunction_of_two_brands(parser: EventParser) -> None:
    result = parser.parse("아디다스 또는 나이키 상품을 구매한 사용자")
    condition = result.ir.condition

    assert isinstance(condition, ConditionGroup)
    assert [child.scopes[0].resolved_id for child in condition.children] == [
        "brand:adidas",
        "brand:nike",
    ]


def test_exact_count(parser: EventParser) -> None:
    result = parser.parse("정확히 1회 구매한 회원")
    assert result.ir.condition.metric == MetricCondition(
        MetricType.EVENT_COUNT, ComparisonOperator.EQ, 1
    )


def test_never_logged_in(parser: EventParser) -> None:
    result = parser.parse("한 번도 로그인하지 않은 고객")
    condition = result.ir.condition

    assert condition.event_type is EventType.LOGIN
    assert condition.polarity is Polarity.NEGATIVE
    # 횟수 0 과 부정을 둘 다 담지 않는다 — canonical 형태는 하나다.
    assert condition.metric is None


# ── 표현 변형이 하나의 canonical 이벤트로 모이는가 ────────────────────────────────


@pytest.mark.parametrize(
    "sentence",
    [
        "상품을 구매한 고객",
        "상품을 구입한 고객",
        "상품을 주문한 고객",
        "상품을 결제한 고객",
        "상품을 산 고객",
    ],
)
def test_purchase_surface_variants_collapse(parser: EventParser, sentence: str) -> None:
    result = parser.parse(sentence)
    assert result.ir.condition.event_type is EventType.PURCHASE


def test_all_purchase_variants_produce_identical_ir(parser: EventParser) -> None:
    """활용형이 달라도 IR 이 **완전히 같아야** 사전 없이 정규화한 보람이 있다."""

    sentences = [
        "상품을 구매한 고객",
        "상품을 구입한 고객",
        "상품을 주문한 고객",
        "상품을 결제한 고객",
        "상품을 산 고객",
    ]
    produced = {parser.parse(sentence).ir for sentence in sentences}
    assert len(produced) == 1


@pytest.mark.parametrize(
    "sentence",
    ["나이키를 샀다", "나이키를 샀던 고객", "나이키를 산 고객", "나이키를 구매한 고객"],
)
def test_conjugation_variants_resolve_the_same_brand(
    parser: EventParser, sentence: str
) -> None:
    result = parser.parse(sentence)
    scopes = result.ir.condition.scopes
    assert scopes[0].resolved_id == "brand:nike"


# ── 엔티티는 사전이 아니라 repository 가 해석한다 ─────────────────────────────────


def test_brand_comes_from_repository_not_from_the_alias_file() -> None:
    """카탈로그가 비면 브랜드를 알 수 없어야 한다 — 사전에 박혀 있지 않다는 증거.

    같은 문장이 카탈로그가 있을 때는 접지되고(다른 테스트) 없을 때는 접지되지 않는다는
    대비가 '지식이 어디서 왔는가'를 증명한다.
    """

    empty = build_default_parser(
        InMemoryEntityRepository([]), reference_date=FIXED_TODAY
    )
    result = empty.parse("나이키를 구매한 고객")

    assert "나이키" in result.diagnostics["unresolved_values"]
    # 접지 실패를 조건 없는 IR 로 넘기지 않는다 — 그러면 '나이키를 산 고객'이 조용히
    # '아무거나 산 고객'이 되어 모수가 통째로 넓어진다.
    assert result.ir is None
    assert "scope.value_not_found" in {issue.code for issue in result.issues}


def test_newly_registered_brand_needs_no_code_change() -> None:
    """새 브랜드는 카탈로그 한 줄이지 사전 수정이 아니다."""

    repository = InMemoryEntityRepository(
        [EntityCandidate(EntityType.BRAND, "brand:zzz", "지지지", 1.0)]
    )
    parser = build_default_parser(repository, reference_date=FIXED_TODAY)
    result = parser.parse("지지지를 구매한 고객")

    assert result.ir.condition.scopes[0].resolved_id == "brand:zzz"


def test_alias_file_contains_no_brand_names() -> None:
    """사전에 데이터 값이 들어가는 순간 사전은 카탈로그의 낡은 사본이 된다."""

    registry = load_alias_registry()
    every_surface = {
        surface for section in registry.sections.values() for surface in section
    }
    assert not every_surface & {"나이키", "아디다스", "뉴발란스", "애플"}


# ── 조용한 모수 확대 방지 ─────────────────────────────────────────────────────────
#
# 이 묶음은 전부 같은 실패 유형을 막는다: **조건 하나가 사라졌는데 IR 은 정상처럼 보이는 것**.
# 결과가 '조금 다른' 정도가 아니라 대상이 수백 배로 늘어나므로, 조용히 넘기면 안 된다.


def test_exclusion_is_not_dropped(parser: EventParser) -> None:
    """``제외한`` 을 버리면 결과가 정확히 반대가 된다."""

    result = parser.parse("나이키를 제외한 상품을 구매한 고객")
    scope = result.ir.condition.scopes[0]

    assert scope.operator is ComparisonOperator.NE
    assert scope.resolved_id == "brand:nike"


def test_negation_alias_variant_is_also_honoured(parser: EventParser) -> None:
    result = parser.parse("나이키가 아닌 상품을 구매한 고객")
    assert result.ir.condition.scopes[0].operator is ComparisonOperator.NE


def test_unattached_exclusion_fails_closed(parser: EventParser) -> None:
    """붙일 값이 없는 배제어를 무시하면 조건이 통째로 사라진다."""

    result = parser.parse("제외하고 구매한 고객")
    assert result.ir is None
    assert "logic.not_unattached" in {issue.code for issue in result.issues}


def test_rank_phrase_is_not_silently_discarded(parser: EventParser) -> None:
    """``상위 100명`` 이 사라지면 대상이 '구매한 전원'으로 넓어진다."""

    result = parser.parse("최근 3개월 구매 금액 상위 100명")

    assert result.ir.rank is not None
    assert result.ir.rank.limit == 100
    assert result.ir.rank.direction is SortDirection.DESC


def test_rank_direction_is_declared_not_assumed(parser: EventParser) -> None:
    result = parser.parse("구매 금액 하위 20명")
    assert result.ir.rank.direction is SortDirection.ASC


def test_rank_without_a_number_fails_closed(parser: EventParser) -> None:
    """개수 없는 ``상위 고객`` 에 기본값을 지어내지 않는다."""

    result = parser.parse("구매 금액 상위 고객")
    assert result.ir is None
    assert "expression.unparsed" in {issue.code for issue in result.issues}


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("일주일 동안 구매한 고객", RelativeTimeWindow(1, TimeUnit.WEEK)),
        ("한 달 동안 구매한 고객", RelativeTimeWindow(1, TimeUnit.MONTH)),
        ("두 달 동안 구매한 고객", RelativeTimeWindow(2, TimeUnit.MONTH)),
        ("3주일 동안 구매한 고객", RelativeTimeWindow(3, TimeUnit.WEEK)),
        ("한 달간 구매한 고객", RelativeTimeWindow(1, TimeUnit.MONTH)),
    ],
)
def test_native_period_expressions_keep_their_window(
    parser: EventParser, sentence: str, expected: RelativeTimeWindow
) -> None:
    """기간이 사라지면 조회가 조용히 전 기간으로 넓어진다."""

    assert parser.parse(sentence).ir.condition.time_window == expected


@pytest.mark.parametrize(
    "sentence",
    [
        "구매금액이 100000원 이상인 고객",
        "주문금액이 100000원 이상인 고객",
        "주문 금액이 100000원 이상인 고객",
    ],
)
def test_amount_phrase_does_not_swallow_the_event(
    parser: EventParser, sentence: str
) -> None:
    """금액 스팬이 사건 낱말을 삼키면 문장 전체가 해석 불능이 된다."""

    result = parser.parse(sentence)
    assert result.ir is not None, sentence
    assert result.ir.condition.event_type is EventType.PURCHASE


def test_entity_type_alias_inside_a_value_is_not_a_marker(parser: EventParser) -> None:
    """``언노운브랜드`` 의 ``브랜드`` 를 종류 표지로 읽으면 값이 잘려 나간다."""

    result = parser.parse("언노운브랜드를 구매한 고객")
    values = [
        token.raw_text for token in result.tokens if token.kind is TokenKind.ENTITY_VALUE
    ]
    assert "언노운브랜드" in values


def test_filler_residue_does_not_fail_the_parse(parser: EventParser) -> None:
    """목적격 조사가 없는 잔여물은 조건이 아니므로 오류가 되면 안 된다.

    이 테스트가 없으면 위의 ``scope.value_not_found`` 규칙이 과잉 적용되어 거의 모든
    문장이 실패한다.
    """

    result = parser.parse("최근 3개월 동안 나이키를 두 번 이상 산 고객")
    assert result.ir is not None
    assert result.issues == ()


# ── 애매한 사례 ──────────────────────────────────────────────────────────────────


def test_ambiguous_entity_does_not_produce_an_ir(parser: EventParser) -> None:
    result = parser.parse("애플을 구매한 고객")

    assert result.ir is None
    assert result.fallback_required is True
    assert result.fallback_reason == FallbackReason.AMBIGUOUS_ENTITY.value
    assert {issue.code for issue in result.issues} == {"entity.ambiguous"}


def test_ambiguity_message_names_the_candidates(parser: EventParser) -> None:
    result = parser.parse("애플을 구매한 고객")
    message = result.issues[0].message
    assert "brand:apple" in message and "product:apple" in message


# ── 실패 사례 ────────────────────────────────────────────────────────────────────


def test_unparseable_sentence_requests_fallback(parser: EventParser) -> None:
    result = parser.parse("최근에 뭔가 많이 한 사람")

    assert result.ir is None
    assert result.fallback_required is True
    assert result.fallback_reason == FallbackReason.EVENT_NOT_FOUND.value


def test_invalid_date_does_not_silently_widen_the_query(parser: EventParser) -> None:
    """가장 위험한 실패는 조건이 사라진 채 IR 이 정상처럼 보이는 것이다.

    ``2026-02-30`` 은 달력에 없다. 이 구간을 그냥 버리면 '기간 없는 전체 구매 조회'가
    되어 모수가 통째로 달라진다.
    """

    result = parser.parse("2026-02-30부터 2026-03-01까지 구매한 고객")

    assert result.ir is None
    assert result.fallback_required is True
    assert "expression.unparsed" in {issue.code for issue in result.issues}


def test_failure_preserves_the_original_text(parser: EventParser) -> None:
    """실패해도 원문을 잃지 않는다 — fallback 이 다시 읽어야 한다."""

    sentence = "최근에 뭔가 많이 한 사람"
    assert parser.parse(sentence).original_text == sentence


def test_fallback_can_recover_an_ir() -> None:
    """규칙이 실패한 문장을 fallback 이 처리하면 결과가 살아나야 한다."""

    class StubFallback:
        def parse(self, text: str, partial_tokens: object) -> EventIR:
            return EventIR(
                target_entity=EntityType.MEMBER,
                condition=EventCondition(event_type=EventType.PURCHASE),
            )

    parser = build_default_parser(
        InMemoryEntityRepository(CATALOG),
        reference_date=FIXED_TODAY,
        fallback=StubFallback(),
    )
    result = parser.parse("최근에 뭔가 많이 한 사람")

    assert result.ir is not None
    assert result.fallback_required is False
    assert result.diagnostics["ir_source"] == "fallback"


# ── 스팬 중재 ────────────────────────────────────────────────────────────────────


def test_longer_span_wins_over_contained_one(parser: EventParser) -> None:
    """``구매자`` 안의 ``구매`` 를 사건으로도 읽으면 같은 자리가 두 번 조건이 된다."""

    result = parser.parse("구매자 목록")
    kinds = [token.kind for token in result.tokens if token.start == 0]
    assert TokenKind.TARGET in kinds
    assert TokenKind.EVENT not in kinds


def test_comparison_inside_a_count_is_not_double_read(parser: EventParser) -> None:
    result = parser.parse("2회 이상 구매한 고객")
    assert [token.kind for token in result.tokens].count(TokenKind.COMPARISON) == 0


# ── 원문 위치 추적 ───────────────────────────────────────────────────────────────


def test_token_spans_map_back_to_the_original_text(parser: EventParser) -> None:
    """정규화가 길이를 바꿔도 '원문의 어디를 그렇게 읽었는지' 보여줄 수 있어야 한다."""

    sentence = "최근 3개월 동안 나이키를 두 번 이상 산 고객"
    result = parser.parse(sentence)

    by_kind = {token.kind: token for token in result.tokens}
    assert result.original_text_of(by_kind[TokenKind.EVENT]) == "산"
    assert result.original_text_of(by_kind[TokenKind.COUNT_CONDITION]) == "두 번 이상"
    assert result.original_text_of(by_kind[TokenKind.ENTITY_VALUE]) == "나이키"


# ── 조립 계약 ────────────────────────────────────────────────────────────────────


def test_parser_rejects_an_extractor_that_cannot_contribute() -> None:
    """오타 난 메서드명 하나로 축이 통째로 빠지는 것을 조립 시점에 막는다."""

    class Broken:
        def extrct(self, text: str) -> list:  # 오타
            return []

    with pytest.raises(TypeError, match="neither extract"):
        EventParser(normalizer=RuleBasedKoreanNormalizer(), extractors=[Broken()])


# ── 결정론 ───────────────────────────────────────────────────────────────────────


def test_same_input_yields_same_result(parser: EventParser) -> None:
    sentence = "최근 3개월 동안 나이키를 두 번 이상 산 고객"
    assert parser.parse(sentence).ir == parser.parse(sentence).ir


def test_reference_date_is_injected_not_read_from_the_clock() -> None:
    """기준일을 바꾸면 결과가 따라 바뀌어야 한다 — 내부에서 현재 시각을 읽지 않는다는 증거."""

    january = build_default_parser(
        InMemoryEntityRepository(CATALOG), reference_date=date(2026, 1, 20)
    )
    result = january.parse("이번 달 구매한 고객")
    assert result.ir.condition.time_window == AbsoluteDateRange(
        date(2026, 1, 1), date(2026, 1, 31)
    )


# ── 직렬화 ───────────────────────────────────────────────────────────────────────


def test_ir_serializes_to_the_documented_shape(parser: EventParser) -> None:
    result = parser.parse("최근 3개월 동안 나이키를 두 번 이상 산 고객")
    assert result.ir.to_dict() == {
        "target_entity": "member",
        "condition": {
            "event_type": "purchase",
            "polarity": "positive",
            "time_window": {"value": 3, "unit": "month"},
            "scopes": [
                {
                    "entity_type": "brand",
                    "operator": "eq",
                    "value": "나이키",
                    "resolved_id": "brand:nike",
                    "confidence": 1.0,
                }
            ],
            "metric": {"metric_type": "event_count", "operator": "gte", "value": 2},
        },
    }


def test_decimal_amount_serializes_without_precision_loss(parser: EventParser) -> None:
    result = parser.parse("이번 달 주문 금액이 100000원 이상인 고객")
    assert result.ir.to_dict()["condition"]["metric"]["value"] == "100000"


@pytest.mark.parametrize(
    "sentence",
    [
        "최근 3개월 동안 나이키를 두 번 이상 산 고객",
        "지난 7일 동안 로그인하지 않은 회원",
        "이번 달 주문 금액이 100000원 이상인 고객",
        "아디다스 또는 나이키 상품을 구매한 사용자",
        "나이키를 제외한 상품을 구매한 고객",
        "구매한 고객 상위 10명",
        "2026-07-01부터 2026-07-31까지 구매한 고객",
    ],
)
def test_ir_survives_a_full_json_round_trip(parser: EventParser, sentence: str) -> None:
    """``Expression → JSON → 저장 → 로드 → Expression`` 이 의미를 보존해야 한다.

    ``json.loads(json.dumps(x)) == x`` 를 확인하는 것은 json 모듈에 대한 동어반복이다.
    실제로 검사해야 하는 것은 **모델로 되돌렸을 때 같은 객체인가**이다.
    """

    import json

    original = parser.parse(sentence).ir
    assert original is not None

    restored = event_ir_from_dict(json.loads(json.dumps(original.to_dict(), ensure_ascii=False)))
    assert restored == original
    assert restored.to_dict() == original.to_dict()


def test_round_trip_preserves_decimal_precision(parser: EventParser) -> None:
    """금액이 float 을 거치면 라운드트립이 값을 바꾼다."""

    import json

    original = parser.parse("이번 달 주문 금액이 100000원 이상인 고객").ir
    restored = event_ir_from_dict(json.loads(json.dumps(original.to_dict())))

    assert isinstance(restored.condition.metric.value, Decimal)
    assert restored.condition.metric.value == Decimal("100000")


def test_round_trip_rejects_unknown_enum_values() -> None:
    """저장된 IR 에 모르는 값이 있으면 조용히 통과시키지 않는다."""

    with pytest.raises(IRDeserializationError, match="unknown event_type"):
        event_ir_from_dict(
            {"target_entity": "member", "condition": {"event_type": "teleport"}}
        )


# ── IR 에는 한국어 표면어가 없다 ──────────────────────────────────────────────────


def test_ir_contains_no_korean_surface_forms(parser: EventParser) -> None:
    """``value`` 의 브랜드명을 빼면 IR 어디에도 한국어가 없어야 한다.

    브랜드명은 표면 '표현'이 아니라 데이터 '값'이므로 예외다.
    """

    result = parser.parse("최근 3개월 동안 나이키를 두 번 이상 산 고객")
    payload = result.ir.to_dict()
    condition = payload["condition"]
    scalars = [
        payload["target_entity"],
        condition["event_type"],
        condition["polarity"],
        condition["time_window"]["unit"],
        condition["metric"]["metric_type"],
        condition["metric"]["operator"],
        condition["scopes"][0]["entity_type"],
        condition["scopes"][0]["operator"],
    ]
    assert all(value.isascii() for value in scalars)
