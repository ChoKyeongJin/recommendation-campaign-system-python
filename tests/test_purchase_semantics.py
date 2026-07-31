"""구매 의미 계층의 계약 — 활용형·명사형·복수 상품이 한 규칙으로 읽힌다.

이 스위트가 지키는 것은 넷이다.

  1. **활용형**  '샀다/산/사다/구입했다/구매했다'가 같은 구매 신호다(표현형마다 규칙을 늘리지 않는다).
  2. **동음이의** '산'은 山일 수도 있다 — 문맥 조건 없이 낱말만 보고 구매로 읽지 않는다.
  3. **명사형**  재작성이 동사를 지운 '구매 이력'도 구매 존재다. 단, '구매 이력이 없는'은 부재다.
  4. **복수 상품** 상품 목록·접지 결과가 상품 개수와 무관하게 같은 배열 구조로 흐른다.

모든 단언은 결정론 경로만 쓴다(LLM·실DB 없이 그린이어야 한다).
"""

from __future__ import annotations

import pytest

import graph_rag
import purchase_lexicon


# ── 1. 구매 동사 활용형 ────────────────────────────────────────────────────────────────────
PURCHASE_EXPRESSIONS = [
    "노트북을 산 사용자",
    "노트북을 샀다",
    "노트북을 사다",
    "노트북을 사서 쓰는 고객",
    "노트북을 구입했다",
    "노트북을 구매했다",
    "노트북을 주문한 고객",
    "2019년에 기저귀 산 사람들 찾아줘",
]


@pytest.mark.parametrize("text", PURCHASE_EXPRESSIONS, ids=PURCHASE_EXPRESSIONS)
def test_purchase_inflections_are_one_signal(text: str) -> None:
    assert purchase_lexicon.EXISTS in purchase_lexicon.membership_signals(text)
    assert graph_rag._has_purchase_history_signal(text)


# ── 2. 동음이의('산' = 山)는 구매가 아니다 ─────────────────────────────────────────────────
NON_PURCHASE_EXPRESSIONS = [
    "한라산에 갔다",
    "높은 산을 등반했다",
    "산 정상까지 올라간 고객",
    "부산 고객에게 발송",
    "등산을 좋아하는 회원",
    "교통사고 이력이 있는 고객",
]


@pytest.mark.parametrize("text", NON_PURCHASE_EXPRESSIONS, ids=NON_PURCHASE_EXPRESSIONS)
def test_homonyms_are_not_purchase_signals(text: str) -> None:
    assert purchase_lexicon.EXISTS not in purchase_lexicon.membership_signals(text)


def test_ambiguous_form_needs_purchase_context_not_just_the_word() -> None:
    """낱말이 같아도 문맥이 다르면 다른 뜻이다 — substring 검색으로는 절대 구분되지 않는 쌍."""
    assert purchase_lexicon.EXISTS in purchase_lexicon.membership_signals("기저귀 산 고객")
    assert purchase_lexicon.EXISTS not in purchase_lexicon.membership_signals("설악산 고객")


# ── 3. 명사형 구매 표현(재작성이 만드는 형태) ─────────────────────────────────────────────
NOMINAL_PURCHASE_EXPRESSIONS = [
    "구매 이력",
    "구매 내역",
    "구매 기록",
    "구매 경험",
    "상품 구매",
    "구입 이력",
    "구입 내역",
    "구입 경험",
    "7년전 기저귀 구매 이력",
]


@pytest.mark.parametrize("text", NOMINAL_PURCHASE_EXPRESSIONS, ids=NOMINAL_PURCHASE_EXPRESSIONS)
def test_nominal_purchase_forms_keep_the_signal(text: str) -> None:
    assert purchase_lexicon.EXISTS in purchase_lexicon.membership_signals(text)


NEGATED_PURCHASE_EXPRESSIONS = [
    "구매 이력이 없는 고객",
    "구매 내역이 없는 회원",
    "최근 30일 구매하지 않은 고객",
    "미구매 고객",
]


@pytest.mark.parametrize("text", NEGATED_PURCHASE_EXPRESSIONS, ids=NEGATED_PURCHASE_EXPRESSIONS)
def test_negated_nominal_forms_are_absence_not_existence(text: str) -> None:
    """명사형 단독 승격이 부정문을 긍정으로 뒤집으면 '미구매 고객'이 구매 고객이 된다."""
    signals = purchase_lexicon.membership_signals(text)
    assert purchase_lexicon.ABSENT in signals
    assert purchase_lexicon.EXISTS not in signals


def test_rewrite_that_nominalises_the_verb_preserves_the_condition() -> None:
    """원문 '…를 구매한' → 재작성 '… 구매 이력': 표현만 바뀌고 뜻은 그대로다(폐기 대상이 아니다)."""
    original = "7년전 기저귀를 구매한 여자 고객 찾아줘"
    rewritten = "여자 고객, 7년전 기저귀 구매 이력"

    assert purchase_lexicon.membership_signals(original) == purchase_lexicon.membership_signals(rewritten)
    assert graph_rag._rewrite_dropped_signals(original, rewritten) == []
    # 그리고 재작성본만 파싱해도 구매 존재 조건이 실제로 만들어진다(뜻이 실행 슬롯까지 도달한다).
    plan: dict = {"target_user": {}}
    graph_rag._apply_core_membership_semantics(rewritten, plan)
    assert plan["target_user"]["purchase_membership"] == {"domain": "purchase", "operator": "exists"}


def test_rewrite_that_deletes_the_purchase_relation_is_reported_as_a_loss() -> None:
    """뜻 자체가 사라지면(상품어만 남고 구매 관계가 없어지면) 소실로 잡아 원문으로 되돌린다."""
    dropped = graph_rag._rewrite_dropped_signals("기저귀를 구매한 고객", "기저귀에 관심 있는 고객")
    assert any("구매 조건" in item for item in dropped), dropped


def test_scope_split_must_keep_the_purchase_relation() -> None:
    """절 분리 폐기 판정도 같은 신호를 쓴다 — 게이트마다 다른 구매 규칙을 갖지 않는다."""
    original = "기저귀를 구매한 고객에게 쿠폰 발송"
    assert not (
        graph_rag._audience_polarity_signals(original)
        - graph_rag._audience_polarity_signals("기저귀 구매 이력 고객")
    )
    assert (
        graph_rag._audience_polarity_signals(original)
        - graph_rag._audience_polarity_signals("기저귀에 관심 있는 고객")
    )


def test_colloquial_spans_use_compact_coordinates() -> None:
    """중의 활용형은 원문에서 읽고 compact 좌표로 돌려준다(파서의 span 좌표계와 어긋나면 창 귀속이 깨진다)."""
    query = "기저귀 산 고객"
    compact = query.replace(" ", "")
    spans = purchase_lexicon.colloquial_spans(query)

    assert len(spans) == 1
    start, end = spans[0].span()
    assert compact[start:end] == "산"


# ── 4. 복수 상품 ───────────────────────────────────────────────────────────────────────────
PRODUCT_LIST_CASES = [
    ("노트북을 샀다", ["노트북"]),
    ("노트북과 모니터를 샀다", ["노트북", "모니터"]),
    ("노트북, 모니터를 샀다", ["노트북", "모니터"]),
    ("노트북, 모니터, 키보드를 구입했다", ["노트북", "모니터", "키보드"]),
    ("노트북과 모니터 그리고 키보드를 샀다", ["노트북", "모니터", "키보드"]),
    ("노트북 및 모니터를 구매했다", ["노트북", "모니터"]),
]


@pytest.mark.parametrize("query,expected", PRODUCT_LIST_CASES, ids=[case[0] for case in PRODUCT_LIST_CASES])
def test_product_phrases_are_always_a_list(query: str, expected: list[str]) -> None:
    """단일 상품도 나열형도 같은 배열 구조다(개수에 따라 필드가 갈라지지 않는다)."""
    assert graph_rag._ambiguous_purchase_scope_phrases(query) == expected


def test_product_list_keeps_order_and_drops_duplicates_and_blanks() -> None:
    assert graph_rag._ambiguous_purchase_scope_phrases("노트북과 노트북을 샀다") == ["노트북"]
    assert graph_rag._ambiguous_purchase_scope_phrases("을 를 샀다") == []
    assert graph_rag._ambiguous_purchase_scope_phrases("") == []


def test_non_purchase_enumeration_is_not_a_product_list() -> None:
    """목적격 조사만으로 나열을 상품으로 읽으면 '서울과 부산을 대상으로'가 상품이 된다."""
    assert graph_rag._ambiguous_purchase_scope_phrases("서울과 부산을 대상으로 발송") == []


@pytest.fixture
def resolved_product_master(monkeypatch: pytest.MonkeyPatch) -> None:
    """상품 마스터 조회를 상품별 확정 결과로 대체한다(실DB 없이 접지 경로를 검증)."""
    monkeypatch.setattr(
        graph_rag.product_master_resolver,
        "resolve_product_phrase",
        lambda phrase: {
            "input": phrase,
            "status": "resolved",
            "grounded": True,
            "source": "product_master_lookup",
            "confidence": 0.99,
            "filters": [{"kind": "product", "value": phrase, "columns": ["PRODUCT_NAME"]}],
            "alternatives": [],
        },
    )


def test_every_product_is_grounded_not_just_the_first(resolved_product_master: None) -> None:
    """두 절에 나뉜 상품('A 구매한 사람 중에 B 구매한 사람')도 각각 접지된다."""
    plan = {
        "target_user": {
            "purchase_objects": [{"value": "보행기", "kind": None}, {"value": "이불 세트", "kind": None}]
        }
    }

    graph_rag._apply_product_master_resolution("보행기 구매한 사람 중에 이불 세트 구매한 사람 찾아줘", plan)
    target_user = plan["target_user"]

    assert [item["input"] for item in target_user["purchase_object_resolutions"]] == ["보행기", "이불 세트"]
    scopes = graph_rag._target_purchase_objects(target_user)
    assert [scope["value"] for scope in scopes] == ["보행기", "이불 세트"]
    assert all(scope["kind"] == "resolved" for scope in scopes), "상품마다 자기 접지 결과를 가져야 한다"


def test_second_product_survives_into_sql(resolved_product_master: None) -> None:
    """접지가 상품 하나로 접히면 SQL 에서 두 번째 상품 조건이 조용히 사라진다(원래 결함)."""
    plan = {
        "intent": "find_user_segment",
        "target_user": {
            "purchase_objects": [{"value": "보행기", "kind": None}, {"value": "이불 세트", "kind": None}]
        },
    }
    graph_rag._apply_product_master_resolution("보행기 구매한 사람 중에 이불 세트 구매한 사람 찾아줘", plan)

    candidate = graph_rag.build_purchase_history_targets_sql_candidate(plan)

    assert candidate is not None
    assert "P.PRODUCT_NAME LIKE N'%보행기%'" in candidate["sql"]
    assert "P.PRODUCT_NAME LIKE N'%이불 세트%'" in candidate["sql"]


def test_singular_slot_stays_as_a_compatibility_projection(resolved_product_master: None) -> None:
    """복수 배열이 내부 표준이고 단수 슬롯은 그 투영이다 — 외부 소비자가 계속 읽을 수 있어야 한다."""
    plan = {"target_user": {}}
    graph_rag._apply_product_master_resolution("노트북과 모니터를 샀다", plan)
    target_user = plan["target_user"]

    assert target_user["purchase_objects"] == [
        {"value": "노트북", "kind": None},
        {"value": "모니터", "kind": None},
    ]
    assert target_user["purchase_object"] == "노트북"
    assert target_user["purchase_object_resolution"] is target_user["purchase_object_resolutions"][0]


def test_plans_built_without_the_plural_slot_still_compile(resolved_product_master: None) -> None:
    """구 계약(단수 슬롯만 채운 플랜)도 같은 결과를 낸다 — 경계에서만 변환한다."""
    target_user = {
        "purchase_object": "노트북",
        "purchase_object_resolution": {
            "input": "노트북",
            "status": "resolved",
            "source": "product_master_lookup",
            "confidence": 0.99,
            "filters": [{"kind": "product", "value": "노트북", "columns": ["PRODUCT_NAME"]}],
        },
    }

    scopes = graph_rag._target_purchase_objects(target_user)

    assert [scope["value"] for scope in scopes] == ["노트북"]
    assert scopes[0]["kind"] == "resolved"
