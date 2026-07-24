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


def test_sell_signal_gate():
    assert g._has_sell_signal("신상 컴퓨터를 팔고 싶어요") is True
    assert g._has_sell_signal("기저귀 구매 고객") is False
