"""렉시콘 패턴 계약 — 이관이 낱말 집합을 바꾸지 않았음을 못 박는다.

이관(정규식 → 데이터)에서 가장 위험한 실수는 "옮기는 김에 어휘도 합치는 것"이다. 그러면 골든이
깨졌을 때 그것이 이관 버그인지 의도한 확장인지 구분할 수 없다. 그래서 이 테스트는 **이관 직전의
정규식 소스를 리터럴로 박아 두고**, 사전에서 만들어진 낱말 집합이 그것과 정확히 같은지 본다.

어휘를 넓히는 변경은 이 목록을 함께 고치는 별도 커밋으로 한다 — 그 diff 가 곧 "동작을 바꿨다"는 선언이다.
"""

from __future__ import annotations

import re

import pytest

import lexicon_patterns

# 이관 직전의 원본 정규식 소스(2026-07-29 기준). 낱말 집합 비교에만 쓴다 — 교대 **순서**는
# 사전 쪽이 '긴 낱말 우선'으로 정렬하므로 일부러 비교하지 않는다(순서 함정 제거가 이관의 목적).
MIGRATED_ORIGINALS: dict[str, str] = {
    "or_operand_boundary": r"이면서|면서|이고|이며|그리고|동시에|반면|지만|중|또는|혹은|이거나|거나|아니면|,",
    "campaign_clause_boundary": r"지만|면서|이며|이고|이거나|거나|또는|혹은|아니면|그리고|반면|다만|,",
    "logic_and": r"그리고|이면서|동시에|이며|이고|면서",
    "logic_or": r"또는|혹은|이거나|거나|아니면",
    "or_connective": r"또는|혹은|이거나|거나|아니면",
    "member_noun_core": r"회원|고객|사용자",
    "member_noun_basic": r"회원|고객|사용자|유저",
    "purchase_rank_target": r"고객님|고객|회원|유저|사람|구매자|소비자",
    "direction_high": r"높은|많은|큰|상위",
    "direction_low": r"낮은|적은|작은|하위",
    "period_compare_marker": r"보다|대비|증가|감소|늘|줄",
    "intra_temporal_compare": r"보다|대비|큰|작은|많은|적은|높은|낮은|증가|감소|커진|늘|줄",
    "prior_period": r"이전|직전",
    "exact_equals_marker": r"정확히|정확하게|딱",
    "agg_domain_context": r"구매|구입|주문|샀|상품|제품|품목|결제|할인|수량|종류|객단가|매출|구매액|금액|건수|종수",
    "campaign_concept_anchor": r"구매|구입|쿠폰|오퍼|혜택|제안|발송|전송|접촉|도달",
    "calendar_enum_connective": r"및|와|과|그리고|또는|이나|랑|하고",
    "condition_language": (
        r"구매|구입|주문|재구매|장바구니|카트|캠페인|반응|로그인|접속|방문|쿠폰|찜|"
        r"거주|지역|등급|성별|남성|여성|나이|연령|휴면|탈퇴|정상|활동|가입|수신|블랙리스트"
    ),
}


@pytest.mark.parametrize("name", sorted(MIGRATED_ORIGINALS), ids=sorted(MIGRATED_ORIGINALS))
def test_migrated_pattern_keeps_the_original_term_set(name: str) -> None:
    original = set(MIGRATED_ORIGINALS[name].split("|"))
    current = set(lexicon_patterns.terms(name))
    assert current == original, (
        f"[{name}] 이관이 낱말 집합을 바꿨다.\n"
        f"  추가됨: {sorted(current - original)}\n  사라짐: {sorted(original - current)}\n"
        f"어휘 확장은 이 테스트의 기대값을 함께 고치는 별도 변경으로 한다."
    )


@pytest.mark.parametrize("name", sorted(MIGRATED_ORIGINALS), ids=sorted(MIGRATED_ORIGINALS))
def test_longer_terms_come_first(name: str) -> None:
    """접두어가 겹치는 낱말은 긴 쪽이 먼저 와야 한다('이면서' 앞에 '면서'가 오면 영영 안 잡힌다)."""
    words = lexicon_patterns.terms(name)
    lengths = [len(word) for word in words]
    assert lengths == sorted(lengths, reverse=True), f"[{name}] 교대 순서가 긴 낱말 우선이 아니다: {words}"


@pytest.mark.parametrize("name", sorted(MIGRATED_ORIGINALS), ids=sorted(MIGRATED_ORIGINALS))
def test_pattern_compiles_and_matches_every_term(name: str) -> None:
    compiled = lexicon_patterns.pattern(name).compiled
    for word in lexicon_patterns.terms(name):
        assert compiled.search(word), f"[{name}] 자기 낱말 '{word}' 를 못 잡는다"


def test_terms_are_escaped_so_data_cannot_inject_regex() -> None:
    """사전은 사전이지 코드가 아니다 — 낱말에 정규식 메타문자가 있어도 리터럴로 다뤄야 한다."""
    assert lexicon_patterns.alternation("clause_separator") == re.escape(",")
    threshold = lexicon_patterns.pattern("logic_or")
    assert threshold.search("또는") and not threshold.search("또")


def test_proxy_exposes_the_regex_api_used_by_call_sites() -> None:
    """모듈 상수를 그대로 대체하므로 호출부가 쓰는 메서드가 다 있어야 한다."""
    proxy = lexicon_patterns.pattern("logic_and")
    assert proxy.search("A 그리고 B")
    assert [m.group(0) for m in proxy.finditer("A 그리고 B 이면서 C")] == ["그리고", "이면서"]
    assert proxy.match("동시에 구매", 0) is not None
    assert isinstance(proxy.pattern, str)


def test_shared_vocabulary_is_really_shared() -> None:
    """같은 어휘를 쓰는 패턴은 낱말 추가를 함께 얻는다 — 이관의 목적 자체."""
    and_terms = set(lexicon_patterns.vocabulary("and_connective"))
    assert and_terms <= set(lexicon_patterns.terms("logic_and"))
    assert "동시에" in and_terms
    # or_operand_boundary 는 같은 어휘를 쓰므로 '동시에'를 자동으로 얻는다(예전에는 따로 적혀 있었다).
    assert "동시에" in set(lexicon_patterns.terms("or_operand_boundary"))


def test_all_supported_or_connectives_come_from_one_vocabulary() -> None:
    expected = {"또는", "혹은", "이거나", "거나", "아니면"}
    assert set(lexicon_patterns.vocabulary("or_connective")) == expected
    assert expected <= set(lexicon_patterns.terms("logic_or"))
    assert expected <= set(lexicon_patterns.terms("or_operand_boundary"))


def test_exclusions_are_documented() -> None:
    """exclude 는 '이관 전 상태 보존'이라는 뜻이므로 반드시 사유가 붙어야 한다.

    사유 없는 exclude 는 그냥 숨겨진 동작 차이다. 이 목록이 곧 후속 검토 대상이다.
    """
    for name, excluded in lexicon_patterns.exclusions().items():
        assert lexicon_patterns.note(name), f"[{name}] exclude={list(excluded)} 인데 사유(note)가 없다"


def test_every_declared_pattern_is_buildable() -> None:
    for name in lexicon_patterns.pattern_names():
        assert lexicon_patterns.terms(name), f"[{name}] 낱말이 하나도 없다"
        assert lexicon_patterns.pattern(name).compiled is not None


def test_data_file_and_code_fallback_agree() -> None:
    """파일이 폴백보다 좁아지면(키 누락) 조용히 옛 어휘로 돌아간다 — 두 소스의 패턴 목록을 맞춰 둔다."""
    file_patterns = set(lexicon_patterns._section("patterns"))
    code_patterns = set(lexicon_patterns._CODE_FALLBACK["patterns"])
    assert code_patterns <= file_patterns, f"파일에 없는 패턴: {sorted(code_patterns - file_patterns)}"
