"""값 자리 잔여 기능어와 프레임 잉여어 고정.

두 결함이 같은 뿌리를 갖는다 — **조건을 담지 않는 말이 조건 자리에 남는 것**이다.

1. 상품 자유텍스트 값 앞에 기간·수량 기능어가 붙어 ``'동안 노트북'`` 같은 값이 만들어진다.
   이 값은 어떤 상품명과도 일치하지 않으므로 조건이 조용히 무력해진다.
2. 문장 꼬리의 ``'…회원만 남겨줘'`` 가 잉여어로 인정되지 않아 "아직 처리 못 한 조건이 있다"로
   읽히고, 그러면 SQL 이 나가지 못한다.

이 프로젝트의 산출물은 SQL 자체이므로, 조건을 담지 않는 말 때문에 SQL 이 막히는 것은 결함이다.
"""

from __future__ import annotations

import audience_frame
import open_text_scope_claims


def _product_values(query: str) -> list[str]:
    return [claim.value for claim in open_text_scope_claims.extract_purchase_product_claims(query)]


# ── 값 자리 잔여 기능어 ────────────────────────────────────────────────────────────────


def test_duration_function_word_is_not_part_of_the_product_value() -> None:
    """``'최근 1년 동안 노트북을'`` 의 상품은 '노트북'이지 '동안 노트북'이 아니다."""
    assert _product_values("최근 1년 동안 노트북을 구매한 회원") == ["노트북"]


def test_quantity_function_word_is_not_part_of_the_product_value() -> None:
    """예29 ``'3개 이상의 중분류'`` 의 값은 '중분류'다."""
    assert _product_values("최근 6개월 동안 서로 다른 3개 이상의 중분류를 구매한 회원") == ["중분류"]


def test_duration_function_word_is_stripped_from_every_enumerated_value() -> None:
    """예25 — 열거된 첫 값에만 기능어가 붙는다. 둘 다 깨끗해야 한다."""
    values = _product_values("최근 6개월 동안 식품 대분류와 생활용품 대분류를 모두 구매했지만")
    assert values == ["식품 대분류", "생활용품 대분류"]


def test_known_exposure_cosmetic_term_loses_its_modifier() -> None:
    """알려진 노출을 숨기지 않고 고정한다.

    '동안 크림'(화장품)은 띄어 쓰면 '크림'으로 좁혀진다. 붙여 쓴 '동안크림'은 영향이 없고, 현재
    값 인덱스·RAG 자산에 standalone '동안' 토큰을 가진 값은 0건이라 실피해는 아직 없다.
    값 인덱스를 재적재하면 이 테스트가 그 사실을 다시 묻는 자리다.
    """
    assert _product_values("동안 크림을 구매한 회원") == ["크림"]


# ── 프레임 잉여어 ──────────────────────────────────────────────────────────────────────


def test_restrictive_particle_and_keep_directive_are_frame_not_condition() -> None:
    """``'…만'`` 과 ``'남겨줘'`` 는 조건이 아니라 문장을 굴리는 말이다.

    둘은 **함께** 있어야 효과가 난다 — 하나만 넣으면 나머지 하나가 조건으로 남아 잉여어 판정이
    여전히 실패한다. 그래서 한 테스트로 묶어 고정한다.
    """
    cases = (
        ("VIP 등급인 회원만 추출해줘", ((0, 6),)),
        ("서울 거주 회원만 남겨줘", ((0, 5),)),
        ("30대 여성 회원을 남겨줘", ((0, 7),)),
    )
    for query, owned_spans in cases:
        assert audience_frame.is_frame_only(query, owned_spans), query


def test_condition_words_never_count_as_frame() -> None:
    """잉여어 목록이 넓어져도 조건을 담은 말은 잉여어가 되면 안 된다(반대 방향 가드)."""
    query = "VIP 등급인 회원만 남겨줘"
    assert not audience_frame.is_frame_only(query, ())
