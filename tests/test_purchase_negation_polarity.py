"""구매 존재/부재 극성의 표면 판정 고정.

이 영역에는 지금까지 테스트가 **한 건도** 없었다. 그래서 어휘를 바꾸든 정규식을 바꾸든
스위트가 어느 방향으로도 반응하지 않았고, '구매 이력이 전혀 없고'가 존재로 뒤집혀 있는 것을
2468개 테스트 중 어느 것도 잡지 못했다. 여기 있는 것은 새 기능의 테스트가 아니라
**극성이 조용히 뒤집히는 것을 막는 안전망**이다.

극성 판정에서 가장 나쁜 실패는 0건이 아니라 **정반대 집합**이다 — "구매 이력이 없는 회원"을
물었는데 구매한 회원이 나오는 것. 그래서 여기서는 '무엇이 켜지는가'보다
**'무엇이 켜지면 안 되는가'**를 더 많이 고정한다.
"""

from __future__ import annotations

import json

import lexicon_patterns
import purchase_lexicon


def _signals(text: str) -> frozenset[str]:
    return purchase_lexicon.membership_signals(text)


# ── 어휘 동등성 ────────────────────────────────────────────────────────────────────────


def test_json_lexicon_and_code_fallback_agree_on_vocabulary_values() -> None:
    """JSON 과 코드 폴백이 **값**까지 같아야 한다.

    기존 `test_data_file_and_code_fallback_agree` 는 패턴 **이름**만 단방향으로 본다. 값이
    어긋나면 아무 테스트도 걸리지 않고, 파일이 없거나 깨졌을 때(_load 가 {} 반환) 폴백이
    조용히 다른 극성으로 동작한다 — 부정 어휘에서 이건 곧 모집단 반전이다.
    """
    payload = json.loads(lexicon_patterns.DEFAULT_LEXICON_PATH.read_text(encoding="utf-8"))
    json_vocabularies = payload["vocabularies"]
    fallback_vocabularies = lexicon_patterns._CODE_FALLBACK["vocabularies"]

    mismatched = {
        name: (fallback_values, json_vocabularies.get(name))
        for name, fallback_values in fallback_vocabularies.items()
        if json_vocabularies.get(name) != fallback_values
    }
    assert mismatched == {}, f"JSON 과 _CODE_FALLBACK 의 어휘 값이 어긋난다: {sorted(mismatched)}"


# ── 부재가 부재로 읽혀야 하는 것 ──────────────────────────────────────────────────────


def test_total_absence_phrase_is_absent_not_exists() -> None:
    """예시 3 '구매 이력이 전혀 없고' — 부정어가 기록 명사 뒤에 오는 자리."""
    signals = _signals("현재까지 구매 이력이 전혀 없고 장바구니에만 상품을 담아둔 회원")
    assert purchase_lexicon.ABSENT in signals
    assert purchase_lexicon.EXISTS not in signals


def test_zero_count_absence_phrase_is_absent_not_exists() -> None:
    """예시 36 '주문이 한 건도 없는' — 수사 '한'이 완료 어미 '한'과 표면이 같아 긍정이 오발화하던 자리.

    부정이 잡히는 것만으로는 부족하다. 긍정 갈래가 함께 켜지면 하류가 존재를 먼저 읽을 수 있어
    증상이 그대로 남는다 — 그래서 EXISTS 가 **꺼져 있는 것**까지 고정한다.
    """
    signals = _signals("온라인몰 주문이 한 건도 없는 회원")
    assert purchase_lexicon.ABSENT in signals
    assert purchase_lexicon.EXISTS not in signals


# ── 부재로 읽히면 안 되는 것 ──────────────────────────────────────────────────────────


def test_double_negation_does_not_become_absent() -> None:
    """이중부정 '전혀 없지 않다' 는 사람 판단으로 존재다.

    부정 어휘만 늘리면 이 문장이 존재→부재로 **반전**한다. 반전보다는 무주장(아무 신호도 없음)이
    낫다 — 그래서 여기서는 EXISTS 를 요구하지 않고 ABSENT 가 아님만 고정한다.
    """
    assert purchase_lexicon.ABSENT not in _signals("구매 이력이 전혀 없지 않다")


def test_campaign_response_absence_is_not_a_purchase_absence() -> None:
    """'구매반응이 없는' 은 캠페인 반응의 부재이지 구매의 부재가 아니다.

    부정어와 구매 동사 사이를 글자수 다리(gap)로 잇는 방식은 이 문장을 purchase:absent 로
    오분류하고, 그러면 IR 이 소유하지 못하는 부정 근거가 생겨 SQL 이 아예 안 나간다.
    이 테스트가 그 방식으로 되돌아가는 것을 막는 유일한 실효 가드다.
    """
    text = "최근 캠페인 발송 성공 횟수가 3회 이상이고 구매반응이 없는 회원"
    assert purchase_lexicon.ABSENT not in _signals(text)


def test_absence_of_something_else_after_a_purchase_is_not_a_purchase_absence() -> None:
    """구매는 있고 **다른 것**이 없는 문장들. 부정어가 멀리 있는데 다리로 이어지면 전부 오분류된다."""
    for text in (
        "구매 후 리뷰가 없는 회원",
        "주문 시 주소가 없는 회원",
        "구매한 상품에 재고가 없는 회원",
        "구매한 뒤 반품이 없는 회원",
        "구매 이력이 있는 회원 중 쿠폰이 없는 회원",
    ):
        assert purchase_lexicon.ABSENT not in _signals(text), text


def test_unrelated_negation_before_the_purchase_verb_stays_positive() -> None:
    """'전혀 다른 카테고리를 구매한' — '전혀'가 구매 동사 **앞**에 있어 부정 문맥이 아니다."""
    signals = _signals("최근 1년 동안 전혀 다른 카테고리를 구매한 회원")
    assert purchase_lexicon.ABSENT not in signals
    assert purchase_lexicon.EXISTS in signals


# ── 알려진 미해결(고정해 두고 나중에 뗀다) ────────────────────────────────────────────


def test_other_zero_count_phrasings_remain_undetected_but_never_invert() -> None:
    """'두 건도 없는' 류는 아직 부재로 읽지 못한다 — 수량 부정('N 건도 없')의 어휘화가 필요하다.

    미검출은 별도 안건으로 남기되, **거짓 존재로 뒤집히지는 않아야 한다**. 검출 실패는 나중에
    고칠 수 있지만 극성 반전은 잘못된 모집단을 만든다.
    """
    assert purchase_lexicon.EXISTS not in _signals("온라인몰 주문이 두 건도 없는 회원")
