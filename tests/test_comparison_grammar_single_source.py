"""비교 연산자 어휘 단일 소스 + 공용 임계 정규식 팩토리 회귀.

배경: '이상|초과|이하|미만'(연산자 열거)과 op→부등호 매핑이 age/cart/count/amount/cell 도메인마다 인라인으로
재하드코딩돼 있어, 새 비교어를 지원하려면 여러 곳을 고쳐야 했다. 이제 어휘는 `_COMPARISON_OPERATORS`(단일
소스)에서 `_OP_ALT_BASIC`(정규식 열거)·`_AGG_OPERATOR_WORDS`(매핑)·`_COMPARISON_OP_ALT`(rich)로 파생하고,
공통 shape 는 `_threshold_pattern` 팩토리가 찍어낸다. age 는 값 의미론(정수 배타경계·연대·범위)이 도메인 고유라
예외로 자체 정규식을 유지한다. 아래 계약이 도메인 정규식이 공용 열거를 참조하는지(인라인 재하드코딩 금지) 강제한다.

실행: python -m pytest tests/test_comparison_grammar_single_source.py -q
"""

import graph_rag as g
import targeting_ir


def test_operator_vocabulary_is_single_source():
    # 매핑은 어휘 dict 의 별칭(사본 아님) — 새 연산자를 한 곳에 넣으면 매핑도 함께 얻는다.
    assert g._AGG_OPERATOR_WORDS is g._COMPARISON_OPERATORS
    assert g._OP_ALT_BASIC == "|".join(g._COMPARISON_OPERATORS)
    # rich 문법(부사·'보다 많은')도 기본 4어를 단일 소스에서 포함한다.
    assert g._OP_ALT_BASIC in g._COMPARISON_OP_ALT


def test_operator_map_single_source_across_module_boundary():
    # graph_rag 의 표면 파싱 표는 targeting_ir(순수 모듈)의 단일 소스를 그대로 재수출한다(사본 아님).
    assert g._COMPARISON_OPERATORS is targeting_ir.COMPARISON_WORD_OPERATORS
    # IR 의 연산자 별칭(단어+기호 항등)은 그 단일 소스의 단어 매핑을 빠짐없이 포함한다.
    for word, op in targeting_ir.COMPARISON_WORD_OPERATORS.items():
        assert targeting_ir._OPERATOR_ALIASES[word] == op
    # 기호 자기사상도 유지(LLM 이 이미 부등호를 낸 경우).
    for symbol in (">=", ">", "<=", "<"):
        assert targeting_ir._OPERATOR_ALIASES[symbol] == symbol


def test_domain_patterns_reference_shared_operator_alternation():
    # 도메인 임계 정규식들은 공용 열거(_OP_ALT_BASIC)를 참조해야 한다(인라인 '이상|초과|이하|미만' 금지).
    # (cart_count 는 이제 전용 정규식 없이 공용 _parse_amount_comparison 문법에 직접 위임 —
    #  test_cart_count_uses_shared_comparison_grammar 참조.)
    for name, pattern in (
        ("cart_amount", g._CART_AMOUNT_PATTERN),
        ("campaign_freq", g._CAMPAIGN_FREQ_COUNT_PATTERN),
        ("campaign_amount", g._CAMPAIGN_AMOUNT_THRESHOLD_PATTERN),
        ("campaign_buy_verb", g._CAMPAIGN_BUY_VERB_PATTERN),
    ):
        assert g._OP_ALT_BASIC in pattern.pattern, f"{name} 이 공용 연산자 열거를 참조하지 않음"
    assert g._OP_ALT_BASIC in g._CELL_RATE_EXPLICIT_SUFFIX


def test_threshold_factory_builds_from_shared_vocabulary():
    # 팩토리가 공용 열거로 op 그룹을 만들고, 숫자+단위+연산자를 추출한다.
    pat = g._threshold_pattern(r"\d+", r"개|건")
    assert g._OP_ALT_BASIC in pat.pattern
    m = pat.search("장바구니에 3개 이상 담은")
    assert m and m.group("num") == "3" and m.group("op") == "이상"
    # 배수어(mag) 옵션 + 새 연산어가 어휘에 있으면 모든 팩토리 생성 정규식이 함께 얻는다.
    money = g._threshold_pattern(r"[\d,]+(?:\.\d+)?", r"원", mag=True)
    m2 = money.search("10만원 미만")
    assert m2 and m2.group("num") == "10" and m2.group("mag") == "만" and m2.group("op") == "미만"


def test_cart_count_uses_shared_comparison_grammar():
    # cart_count 는 전용 임계 정규식(_CART_COUNT)을 버리고 공용 _parse_amount_comparison 에 개수 단위만
    # 넘겨 위임한다 — 그래서 주문/상품 집계 경로와 동일하게 이상/초과/미만/정확값/범위를 모두 처리한다.
    parse = lambda s: g._parse_amount_comparison(s, g._CART_COUNT_UNIT, bare_equals=False)
    assert parse("3개 이상") == [(">=", 3)]
    assert parse("10개를 초과") == [(">", 10)]          # 단위-연산자 사이 조사(를) + '초과'
    assert parse("상품 종류가 3종 이상") == [(">=", 3)]   # '종' 단위
    assert parse("정확히 3개") == [("=", 3)]             # 정확값
    assert parse("2개에서 5개 사이") == [(">=", 2), ("<=", 5)]  # 범위 → 두 술어
    assert parse("매칭 없음") is None


# ── 타입 스펙 기반 생성기(_ThresholdSpec → regex + 파서) ──────────────────────────────
def test_threshold_spec_generates_regex_and_parser_per_number_type():
    # 스펙의 number 타입이 정규식 조각 + 값 파서를 함께 결정한다(정수/한글금액/%/고유어수사).
    integer = g._compile_threshold(g._ThresholdSpec("integer", r"개"))
    m = integer.pattern.search("3개 이상")
    assert integer.parse(m) == (">=", 3)  # 값=정수

    money = g._compile_threshold(g._ThresholdSpec("korean_amount", r"원"))
    m = money.pattern.search("10만원 미만")
    assert money.parse(m) == ("<", 100000.0)  # 배수어 반영

    native = g._compile_threshold(g._ThresholdSpec("native_count", r"번|회"))
    m = native.pattern.search("세 번 초과")
    assert native.parse(m) == (">", 3)  # 고유어 수사→값

    percent = g._compile_threshold(g._ThresholdSpec("percent", r"%", unit_optional=True, sep=""))
    m = percent.pattern.search("90%이상")
    assert percent.parse(m) == (">=", 90.0)


def test_threshold_percent_parser_validates_range():
    # percent 타입 기본 파서는 0<v<=100 을 벗어나면 None(도메인이 막연어 폴백하게 함).
    percent = g._compile_threshold(g._ThresholdSpec("percent", r"%", unit_optional=True, sep=""))
    m = percent.pattern.search("150%이상")
    assert percent.parse(m) is None


def test_threshold_custom_parse_hook_overrides_default():
    # 특수 값 해석은 parse 커스텀 훅으로만 허용 — 있으면 기본 파서 대신 그걸 쓴다.
    hook = lambda m: (">=", 42.0)
    spec = g._ThresholdSpec("integer", r"개", parse=hook)
    matcher = g._compile_threshold(spec)
    m = matcher.pattern.search("3개 이상")
    assert matcher.parse(m) == (">=", 42.0)


def test_migrated_domains_use_threshold_matchers():
    # cart_amount/count/cell/campaign_buy 는 스펙 기반 matcher 로 이전됐다(전용 파서 없이 스펙이 regex+값해석
    # 소유). cart_count(개수)는 여기서 한 발 더 나아가 공용 _parse_amount_comparison 에 직접 위임한다.
    for name in ("_CART_AMOUNT", "_CAMPAIGN_FREQ", "_CELL_RATE", "_CAMPAIGN_BUY_AMOUNT"):
        matcher = getattr(g, name)
        assert isinstance(matcher, g._ThresholdMatcher), f"{name} 이 _ThresholdMatcher 가 아님"
        assert g._OP_ALT_BASIC in matcher.regex


def test_korean_amount_bare_kind_matches_fused_unit_forms():
    # korean_amount_bare 타입: 배수어가 단위(원) 안에 융합, 원 optional — '10만'·'10만원'·'10원' 모두 매칭.
    # (campaign_buy 는 공백 제거 compact 텍스트에서 search 하므로 여기서도 붙여쓴 형태로 검증한다.)
    matcher = g._compile_threshold(g._ThresholdSpec("korean_amount_bare", r"원", sep=""))
    for text, expected in (("10만이상", (">=", 100000)), ("10만원초과", (">", 100000)), ("5000원미만", ("<", 5000))):
        m = matcher.pattern.search(text)
        assert m is not None, f"{text!r} 미매칭"
        op, value = matcher.parse(m)
        assert (op, int(value)) == expected


def test_campaign_buy_patterns_generated_from_spec():
    # campaign_buy 의 두 패턴(단독 임계 + 동사형 임베드)이 스펙 matcher 에서 나온다(전용 regex 제거 확인).
    assert g._CAMPAIGN_AMOUNT_THRESHOLD_PATTERN is g._CAMPAIGN_BUY_AMOUNT.pattern
    assert g._CAMPAIGN_BUY_AMOUNT.regex in g._CAMPAIGN_BUY_VERB_PATTERN.pattern


def test_korean_amount_value_parser_is_shared():
    # korean_amount 와 korean_amount_bare 는 같은 값 파서(_korean_amount_value)를 공유한다(중복 제거).
    assert g._THRESHOLD_NUMBER_KINDS["korean_amount"]["value"] is g._korean_amount_value
    assert g._THRESHOLD_NUMBER_KINDS["korean_amount_bare"]["value"] is g._korean_amount_value
