"""정규화 계사(서술격 조사) '-인' 처리 회귀.

배경: '여성인 회원'의 '여성인'(='여성'+계사 '인')이 정규화 조사 목록(KOREAN_PARTICLES)에 '인'이 없어
매칭되지 않았다. 재작성이 '여성인 회원'을 '여성 회원'으로 다듬어 주는 경로에서는 가려졌지만, 원문
그대로의 rules 경로(재작성 off/불가)에서는 성별(gender) 조건이 통째로 누락됐다.

고정 내용:
  - 계사 '-인'을 2글자 이상 한글 term 뒤 선택 조사로 허용('여성인'→female, '남성인'→male).
  - 1글자 term 에는 붙이지 않는다('여'+'인'='여인', '남'+'인' 오분해 방지).
  - 기존 격조사 매칭('여성 회원'/'여성은'/'여성이')과 다른 term('외국인' 등)은 영향 없음.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_normalization_copula_particle.py -q
"""

from ingest import NormalizationIngester

RULES = "docs/data/normalization_rules.sample.json"


def _matches(query: str) -> list[tuple[str, str]]:
    n = NormalizationIngester.from_file(RULES)
    return [(m["matched_text"], m["normalized"]) for m in n.normalize_text(query)["matches"]]


def test_female_copula_matches():
    assert ("여성인", "female") in _matches("여성인 회원")
    assert ("여성인", "female") in _matches("자녀정보가 등록되어 있고 여성인 회원만 추출해줘.")


def test_male_copula_matches():
    assert ("남성인", "male") in _matches("남성인 고객")


def test_plain_and_josa_still_match():
    # 기존 매칭이 깨지지 않는다.
    assert ("여성", "female") in _matches("여성 회원")
    assert any(canonical == "female" for _text, canonical in _matches("여성은 제외"))
    assert any(canonical == "female" for _text, canonical in _matches("여성이 아닌"))


def test_copula_not_applied_to_single_char_terms():
    # '여인'(여성 아님)·'남인방'이 female/male 로 오분해되지 않는다(1글자 term + 인 금지).
    assert not any(c in ("female", "male") for _t, c in _matches("여인이 나오는 드라마"))
    assert not any(c in ("female", "male") for _t, c in _matches("남인방 회원"))


def test_copula_does_not_break_unrelated_words():
    # '성인/개인/미성년인'은 female/male 등으로 오분해되지 않는다.
    for query in ("성인 회원", "개인 고객", "미성년인 회원"):
        assert not any(c in ("female", "male") for _t, c in _matches(query)), query


def test_english_term_accepts_korean_particles():
    # 영문 종결 term(2글자 이상)도 뒤 한글 조사를 허용한다('VIP인'·'VIP를'·'RCS로').
    assert ("VIP인", "vip") in _matches("VIP인 회원만 추출")
    assert ("VIP를", "vip") in _matches("VIP를 대상으로")
    assert ("VIP는", "vip") in _matches("VIP는 제외")
    assert any(c == "rcs" for _t, c in _matches("RCS로 발송"))


def test_single_letter_english_term_no_particle():
    # 단일 문자 영문 term('f'→female, 'A'~'E'→등급)은 조사에 오탐하지 않는다.
    assert not any(c == "female" for _t, c in _matches("f가 무엇인지"))
    assert _matches("A는 무엇") == []


def test_english_term_boundary_no_partial_match():
    # 부분 매칭 금지: 'VIPS'/'VIP2' 는 vip 로 매칭되지 않는다.
    assert not any(c == "vip" for _t, c in _matches("VIPS 회원"))
    assert not any(c == "vip" for _t, c in _matches("VIP2 등급"))
