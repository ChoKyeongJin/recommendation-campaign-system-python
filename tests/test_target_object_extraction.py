"""상품 구매이력/판매 상품 추출 회귀 코퍼스.

목적: 재작성기(normalize_prompt)가 만들어내는 다양한 표현형에서 상품 조건이 조용히 사라지는
버그가 재발하지 않도록, 프롬프트 -> 기대 추출값 쌍을 고정한다. 사용자가 새 실패 케이스를 찾을
때마다 여기에 한 줄 추가하면 회귀가 방지된다.

여기서는 결정론적 경로(정규식 + 원문 존재 검증)만 테스트한다. LLM 폴백(_apply_llm_object_fallback)
은 네트워크에 의존하므로 이 코퍼스로 검증하지 않고, 대신 그 폴백이 채택 전 반드시 통과해야 하는
검증기(_validated_object)를 직접 테스트해 환각 차단을 보장한다.

실행(컨테이너): docker compose exec api pytest tests/test_target_object_extraction.py -q
"""

import pytest

import graph_rag as g


# (프롬프트, 기대 purchase_object) — effective_query(재작성본) 기준.
PURCHASE_CASES = [
    # 재작성기가 만드는 명사형(원래 버그): "…를 산 고객" -> "… 구매 고객"
    ("기저귀 구매 고객", "기저귀"),
    ("기저귀 구입 고객", "기저귀"),
    ("기저귀 구매 이력 고객", "기저귀"),
    ("유아복 구매 고객에게 쿠폰", "유아복"),
    # 동사형(원래도 동작)
    ("기저귀를 구매한 고객", "기저귀"),
    ("기저귀를 구입하신 회원", "기저귀"),
    ("분유를 구매했던 고객", "분유"),
    # 상품이 아닌 구매행동 수식어 / 목표 문구는 상품으로 오인하지 않는다
    ("첫 구매 고객", None),
    ("재구매 고객", None),
    ("구매 전환 캠페인", None),
    ("40대 여성 고객", None),
    # 수량/횟수·비교 수식어는 상품명이 아니다(원래 버그: '이상'/'2개'/'상품' 이 LIKE 로 샘)
    ("2019년 1월에 2개 이상 상품 구입한 사람", None),
    ("3회 이상 구매한 고객", None),
    ("상품 5개 구입한 회원", None),
    # 앞에 실제 상품명이 없는 일반명사만이면 상품 필터로 쓰지 않는다
    ("상품 구매한 고객", None),
    ("제품 구입 고객", None),
]

# 붙여 쓴 구매 합성어('다구매/총구매/무구매')는 상품명이 아니다. 목적어와 구매 동사 사이에 조사/공백이
# 전혀 없어도 매칭되던 정규식이 앞 음절('다'/'총'/'무')을 상품명으로 떼어내, 상품명이 없는 '전상품 대상'
# 조건에 CRM_CM_PRODUCT 조인과 상품 6컬럼 LIKE 가 붙던 버그의 회귀 코퍼스.
PURCHASE_COMPOUND_PREFIX_CASES = [
    "다구매 고객",
    "다구매 고객 캠페인",
    "총구매 고객",
    "총구매 횟수가 5회 이상인 고객",
    "무구매 회원",
    "무구매 고객 제외",
]

# 경계(조사/공백)를 요구해도 정상 상품 추출은 그대로여야 한다. '기저귀를구매한'처럼 조사만 있고 공백이
# 없는 형태도 조사가 경계 역할을 하므로 계속 잡힌다.
PURCHASE_OBJECT_PRESERVED_CASES = [
    ("기저귀 구매 고객", "기저귀"),
    ("기저귀 구매한 고객", "기저귀"),
    ("기저귀를 구매한 고객", "기저귀"),
    ("기저귀를구매한 고객", "기저귀"),
    ("생수 구입한 회원", "생수"),
]

# (프롬프트, 기대 sell_object)
SELL_CASES = [
    ("신상 컴퓨터를 팔고 싶어요", "신상 컴퓨터"),
    ("VIP 고객에게 신상 노트북을 판매하고 싶어요", "신상 노트북"),
    ("휴면 고객을 깨우고 싶어요", None),
]


@pytest.mark.parametrize("prompt,expected", PURCHASE_CASES)
def test_purchase_object_extraction(prompt, expected):
    target_user = {"purchase_object": None}
    g._apply_purchase_object_filter(prompt, target_user)
    assert target_user["purchase_object"] == expected


@pytest.mark.parametrize("prompt", PURCHASE_COMPOUND_PREFIX_CASES)
def test_purchase_compound_prefix_is_not_product(prompt):
    target_user = {"purchase_object": None}
    g._apply_purchase_object_filter(prompt, target_user)
    assert target_user["purchase_object"] is None


@pytest.mark.parametrize("prompt,expected", PURCHASE_OBJECT_PRESERVED_CASES)
def test_valid_purchase_object_is_preserved(prompt, expected):
    target_user = {"purchase_object": None}
    g._apply_purchase_object_filter(prompt, target_user)
    assert target_user["purchase_object"] == expected


def test_boundary_requirement_keeps_generic_noun_match():
    # 경계 강제가 정상 매칭 자체를 죽이지 않았는지 확인한다. '상품 구매한'은 정규식에는 계속 잡히고
    # (일반명사라) 상품 필터로 승격되지 않을 뿐이다 — 매칭이 사라지면 '알로루 브랜드 상품 구매한'의
    # 브랜드 재시도 경로까지 함께 끊긴다.
    assert g._PURCHASE_OBJECT_PATTERN.search("상품 구매한 고객").group("object") == "상품"
    assert g._PURCHASE_OBJECT_PATTERN.search("다구매 고객") is None


def test_validated_object_rejects_compound_prefix():
    # LLM 폴백은 '원문에 존재'만 보므로 '다구매'의 '다'는 존재 검증을 통과한다. sanitize 계층이
    # 정규식 우회 경로(LLM/브랜드/계사/chain)까지 같은 기준으로 막는지 고정한다.
    assert g._validated_object("다", "다구매 고객 캠페인") is None
    assert g._validated_object("총", "총구매 고객") is None
    assert g._validated_object("무", "무구매 회원") is None


@pytest.mark.parametrize("prompt,expected", SELL_CASES)
def test_sell_object_extraction(prompt, expected):
    plan = {"campaign_constraints": {"sell_object": None}}
    g._apply_sell_object(prompt, plan)
    assert plan["campaign_constraints"]["sell_object"] == expected


def test_validated_object_accepts_present_product():
    # LLM 이 원문에 있는 상품을 반환하면 채택
    assert g._validated_object("기저귀", "기저귀 구매 고객") == "기저귀"


def test_validated_object_rejects_hallucinated_product():
    # LLM 이 원문에 없는 상품을 지어내면 거부(환각 차단)
    assert g._validated_object("냉장고", "기저귀 구매 고객") is None
    assert g._validated_object(None, "기저귀 구매 고객") is None
    assert g._validated_object("", "기저귀 구매 고객") is None


def test_purchase_signal_gate():
    # 폴백은 구매 신호가 있을 때만 LLM 을 호출한다
    assert g._has_purchase_history_signal("기저귀 구매 고객") is True
    assert g._has_purchase_history_signal("40대 여성 고객") is False


def test_high_value_campaign_label_is_not_a_purchase_object():
    prompt = "최근 6개월 평균 주문금액이 10만 원 이상인 회원을 추출해서 고액구매 고객 캠페인을 만들어줘."
    target_user = {"purchase_object": None}
    g._apply_purchase_object_filter(prompt, target_user)
    assert target_user["purchase_object"] is None


def test_sell_signal_gate():
    assert g._has_sell_signal("신상 컴퓨터를 팔고 싶어요") is True
    assert g._has_sell_signal("기저귀 구매 고객") is False
