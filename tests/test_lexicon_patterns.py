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
    "member_noun_core": r"회원|고객|사용자",
    "purchase_rank_target": r"고객님|고객|회원|유저|사람|구매자|소비자",
    "exact_equals_marker": r"정확히|정확하게|딱",
    # 한정사·지시어 이관(2026-08-04). 네 곳의 인라인 집합이 원본이며 **서로 달랐다** — 그 차이가
    # 이관 후에도 보존되는지가 여기서 걸린다. 넷을 하나로 합치는 것은 동작 변경이므로 별도 작업이다.
    "purchase_scope_nonspecific_determiner": (
        r"특정|어떤|일부|각|그|이|저|무슨|어느|임의|여러|다양|다양한|각기|각각|서로|전체|전부|모든|모두"
    ),
    "purchase_object_nonspecific_determiner": (
        r"특정|어떤|일부|각|그|이|저|무슨|어느|임의|여러|다양|다양한|각기|각각|서로|해당|전체|전부|모든|모두"
    ),
    "scope_placeholder_value": r"특정|어떤|모든|해당|일부|각|그|이|저|무슨|어느|임의",
    "scope_distinct_modifier": r"다른|여러|다양|다양한|각기|각각|가지각색|서로",
    "generic_scope_reference": r"다른|해당|특정|외|외의",
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
    """사전은 사전이지 코드가 아니다 — 낱말에 정규식 메타문자가 있어도 리터럴로 다뤄야 한다.

    대상 교체(2026-08-01): 예전에는 clause_separator(',')와 logic_or 로 이 계약을 쟀는데 둘 다
    소비자가 사라져 삭제됐다. 계약 자체는 지킬 값어치가 있으므로 메타문자를 가진 살아 있는
    어휘(targeting_and_alias 의 '&&', '&')로 옮긴다.
    """
    alternation = lexicon_patterns.alternation("targeting_and_alias")
    assert re.escape("&&") in alternation and "&&|" not in alternation.replace(re.escape("&&"), "")
    # 이스케이프됐으므로 '&' 는 정규식 연산자가 아니라 글자로 매치된다.
    assert re.search(alternation, "a && b")
    marker = lexicon_patterns.pattern("member_noun_core")
    assert marker.search("회원") and not marker.search("회")


def test_proxy_exposes_the_regex_api_used_by_call_sites() -> None:
    """모듈 상수를 그대로 대체하므로 호출부가 쓰는 메서드가 다 있어야 한다."""
    proxy = lexicon_patterns.pattern("member_noun_core")
    assert proxy.search("우리 회원 목록")
    assert [m.group(0) for m in proxy.finditer("회원 과 고객")] == ["회원", "고객"]
    assert proxy.match("고객 조회", 0) is not None
    assert isinstance(proxy.pattern, str)


def test_shared_vocabulary_is_really_shared() -> None:
    """같은 어휘를 쓰는 패턴은 낱말 추가를 함께 얻는다 — 이관의 목적 자체.

    member_noun 을 member_noun_core 와 purchase_rank_target 이 공유한다. 후자는 '사용자'를
    exclude 하므로 부분집합(<=)이 아니라 공유 낱말의 멤버십으로 단언한다.
    """
    member_terms = set(lexicon_patterns.vocabulary("member_noun"))
    assert member_terms <= set(lexicon_patterns.terms("member_noun_core"))
    assert {"회원", "고객"} <= member_terms
    assert {"회원", "고객"} <= set(lexicon_patterns.terms("purchase_rank_target"))
    assert "사용자" not in set(lexicon_patterns.terms("purchase_rank_target"))


def test_all_supported_or_connectives_come_from_one_vocabulary() -> None:
    """OR 접속 어휘는 한 곳이 소유한다(소비자는 targeting_expression·event_parser 쪽 alternation)."""
    expected = {"또는", "혹은", "이거나", "거나", "아니면"}
    assert set(lexicon_patterns.vocabulary("or_connective")) == expected


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


def test_determiner_consumers_read_from_the_lexicon_not_their_own_literal() -> None:
    """한정사 어휘의 소비자는 사전에서 읽는다 — 인라인 복제가 돌아오면 여기서 걸린다.

    이관 전에는 같은 낱말 묶음이 graph_rag 세 곳과 open_text_scope_claims 한 곳에 각자 적혀
    있었고 서로 달랐다('해당'이 한 곳에만 있는 식). 집합 동일성만 재면 누군가 다시 리터럴로
    적어도 통과하므로, 사전이 바뀌면 소비자도 따라 바뀐다는 것까지 본다.
    """
    import graph_rag
    import open_text_scope_claims

    assert graph_rag._SCOPE_PLACEHOLDER_VALUES == set(lexicon_patterns.terms("scope_placeholder_value"))
    assert graph_rag._SCOPE_DISTINCT_MODIFIERS == set(lexicon_patterns.terms("scope_distinct_modifier"))
    assert graph_rag._PURCHASE_OBJECT_NONSPECIFIC_DETERMINERS == set(
        lexicon_patterns.terms("purchase_object_nonspecific_determiner")
    )
    assert set(lexicon_patterns.terms("purchase_scope_nonspecific_determiner")) <= (
        graph_rag._PURCHASE_SCOPE_NON_ENTITY_TERMS
    )
    assert set(lexicon_patterns.terms("generic_scope_reference")) <= (
        open_text_scope_claims._generic_product_terms()
    )


def test_data_file_and_code_fallback_agree() -> None:
    """파일이 폴백보다 좁아지면(키 누락) 조용히 옛 어휘로 돌아간다 — 두 소스의 패턴 목록을 맞춰 둔다."""
    file_patterns = set(lexicon_patterns._section("patterns"))
    code_patterns = set(lexicon_patterns._CODE_FALLBACK["patterns"])
    assert code_patterns <= file_patterns, f"파일에 없는 패턴: {sorted(code_patterns - file_patterns)}"
