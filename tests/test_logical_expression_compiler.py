"""논리식(OR-of-conjunctions) 컴파일 계층 테스트.

임계값과 서로 다른 지표가 섞인 AND/OR 를 괄호·우선순위를 보존한 하나의 SQL 로 컴파일하는 새 계층
(logical_expression + graph_rag._apply_logical_expression). feature flag(LOGICAL_OR_COMPILER) 뒤에 있고,
기본은 기존 fail-close 게이트다. 파싱/컴파일/검증 실패는 미지원(fail-close) — AND-only 폴백 금지.

실행: docker compose exec -w /app -e PYTHONPATH=/app python pytest tests/test_logical_expression_compiler.py -q
"""

import re

import pytest

import graph_rag as g
import logical_expression as le

_NR = g.DEFAULT_NORMALIZATION_PATH
OR = g._LOGIC_OR_RE
AND = g._LOGIC_AND_RE


# ══════════════════════════════════════════════════════════════════════════════
# 1. 순수 모듈: 파서(괄호/AND>OR 우선순위)
# ══════════════════════════════════════════════════════════════════════════════
def _sig(text):
    return le.structure_signature(le.parse(text, OR, AND))


def test_parse_a_or_b():
    assert _sig("가나 또는 다라") == ("OR", (("LEAF",), ("LEAF",)))


def test_parse_a_and_b():
    assert _sig("가나 그리고 다라") == ("AND", (("LEAF",), ("LEAF",)))


def test_parse_and_binds_tighter_than_or():
    # A 또는 B 그리고 C  ==  A OR (B AND C)
    assert _sig("가 또는 나 그리고 다") == ("OR", (("LEAF",), ("AND", (("LEAF",), ("LEAF",)))))


def test_parse_left_and_group_or():
    # A 그리고 B 또는 C  ==  (A AND B) OR C
    assert _sig("가 그리고 나 또는 다") == ("OR", (("AND", (("LEAF",), ("LEAF",))), ("LEAF",)))


def test_parse_two_and_groups_or():
    assert _sig("가 그리고 나 또는 다 그리고 라") == (
        "OR", (("AND", (("LEAF",), ("LEAF",))), ("AND", (("LEAF",), ("LEAF",)))))


def test_parse_explicit_parens_override_precedence():
    # (A 또는 B) 그리고 C  ==  AND(OR(A,B), C) — 괄호가 우선순위를 뒤집는다.
    assert _sig("(가 또는 나) 그리고 다") == ("AND", (("OR", (("LEAF",), ("LEAF",))), ("LEAF",)))


def test_parse_no_paren_equals_paren_for_representative():
    # '무괄호 A 이거나 B 이고 C' 와 '(A) 또는 (B 이고 C)' 는 같은 AST(AND>OR 정책 고정).
    a = _sig("로그인 100회 이상이거나 구매 10회 이상이고 마케팅 동의")
    b = _sig("(로그인 100회 이상) 또는 (구매 10회 이상이고 마케팅 동의)")
    assert a == b == ("OR", (("LEAF",), ("AND", (("LEAF",), ("LEAF",)))))


@pytest.mark.parametrize("word", ["또는", "혹은", "이거나", "거나"])
def test_parse_korean_or_variants(word):
    assert le.has_or(le.parse(f"로그인 100회 이상{word} 구매 10회 이상", OR, AND))


def test_parse_paren_imbalance_raises():
    with pytest.raises(le.ParseError):
        le.parse("(로그인 100회 이상 또는 구매 10회 이상", OR, AND)


def test_parse_dangling_connective_raises():
    with pytest.raises(le.ParseError):
        le.parse("로그인 100회 이상 또는", OR, AND)


# ══════════════════════════════════════════════════════════════════════════════
# 2. 순수 모듈: 조립 + 검증 (leaf 컴파일 stub)
# ══════════════════════════════════════════════════════════════════════════════
def _stub_leaf(text, prefix):
    text = text.strip()
    if "로그인" in text:
        return le.LeafCompile(f"(B.LOGIN >= @{prefix}0)", {f"{prefix}0": 100},
                              [{"domain": "m", "metric": "login", "operator": ">=", "value": 100}])
    if "구매" in text:
        return le.LeafCompile(f"(B.ORDER >= @{prefix}0)", {f"{prefix}0": 10},
                              [{"domain": "a", "metric": "order", "operator": ">=", "value": 10}])
    if "마케팅" in text:
        return le.LeafCompile("(B.AGREE_YN = 'Y')", {}, [{"domain": "attr", "metric": "mk", "operator": None, "value": None}])
    raise le.LeafUnsupported(text, "unknown")


def _fmt(v):
    return str(int(v))


def test_assemble_preserves_parens_and_params():
    ast = le.parse("로그인 100회 이상이거나 구매 10회 이상이고 마케팅 동의", OR, AND)
    a = le.assemble(ast, _stub_leaf)
    assert a.fragment == "((B.LOGIN >= @L0_0) OR ((B.ORDER >= @L1_0) AND (B.AGREE_YN = 'Y')))"
    assert a.params == {"L0_0": 100, "L1_0": 10}
    assert le.render_inline(a.fragment, a.params, _fmt) == \
        "((B.LOGIN >= 100) OR ((B.ORDER >= 10) AND (B.AGREE_YN = 'Y')))"
    assert le.verify(a, _fmt) == []


def test_assemble_unique_params_across_branches():
    # 같은 지표 서로 다른 임계값 — 파라미터 이름 충돌 없이 각자 값 유지.
    def leaf(text, prefix):
        val = 100 if "100" in text else 10
        return le.LeafCompile(f"(B.ORDER >= @{prefix}0)", {f"{prefix}0": val},
                              [{"domain": "a", "metric": "order", "operator": ">=", "value": val}])
    a = le.assemble(le.parse("구매 100회 이상 또는 구매 10회 이상", OR, AND), leaf)
    assert set(a.params.values()) == {100, 10}
    assert le.render_inline(a.fragment, a.params, _fmt) == "((B.ORDER >= 100) OR (B.ORDER >= 10))"


def test_assemble_fail_close_on_unsupported_leaf():
    with pytest.raises(le.LeafUnsupported):
        le.assemble(le.parse("로그인 100회 이상 또는 알수없는조건", OR, AND), _stub_leaf)


# ══════════════════════════════════════════════════════════════════════════════
# 3. 통합: graph_rag 컴파일러(flag on) — 정상 컴파일
# ══════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("LOGICAL_OR_COMPILER", "1")


def _compile(query):
    plan = g.build_query_plan(query)
    g._apply_logical_expression(query, plan, _NR)
    g._promote_unknown_intent_for_target_signal(plan)
    candidate = g.build_sql_template_candidate(plan)
    return plan, (candidate["sql"] if candidate else None)


def test_representative_case_compiles(flag_on):
    plan, sql = _compile("로그인 횟수가 100회 이상이거나 구매 횟수가 10회 이상이면서 마케팅에 동의한 회원을 보여줘.")
    assert plan.get("unsupported") is None and sql is not None
    where = _where(sql)
    assert where == (
        "((B.TOTAL_LOGIN_CNT >= 100) OR ((B.MEMBER_NO IN (SELECT MEMBER_NO FROM CRM_SL_ORDERHEADERMALL "
        "WHERE MEMBER_NO IS NOT NULL GROUP BY MEMBER_NO HAVING COUNT(DISTINCT ORDER_ID) >= 10)) "
        "AND (B.AGREE_YN = 'Y')))"
    )
    # 바인드 파라미터 추적: 임계값이 지표별로 유지.
    params = plan["logical_expression"]["params"]
    assert sorted(params.values()) == [10.0, 100.0]


def test_channel_suffix_with_parens_does_not_break_parse(flag_on):
    # BFF 가 붙이는 "발송 채널: RCS (리치 메시지, …)" 접미어의 괄호가 논리식 파서에 들어가면 괄호 불균형으로
    # logical_expression_parse_failed 가 났다. 채널 절을 먼저 떼어 정상 컴파일돼야 한다.
    q = ("로그인 횟수가 100회 이상이거나 구매 횟수가 10회 이상이면서 마케팅에 동의한 회원을 보여줘.\n"
         "발송 채널: RCS (리치 메시지, 버튼 및 이미지 지원)")
    plan, sql = _compile(q)
    assert plan.get("unsupported") is None, plan.get("unsupported")
    assert sql is not None
    w = _where(sql)
    assert "B.TOTAL_LOGIN_CNT >= 100" in w and "COUNT(DISTINCT ORDER_ID) >= 10" in w and "B.AGREE_YN = 'Y'" in w
    # 채널 접미어(RCS/리치 메시지)가 SQL 에 새어들지 않는다.
    assert "RCS" not in sql and "리치" not in sql


def test_parenthesized_equals_unparenthesized(flag_on):
    _, sql_a = _compile("로그인 횟수가 100회 이상이거나 구매 횟수가 10회 이상이고 마케팅에 동의한 회원을 보여줘.")
    _, sql_b = _compile("(로그인 횟수가 100회 이상) 또는 (구매 횟수가 10회 이상이고 마케팅에 동의한) 회원")
    assert _where(sql_a) == _where(sql_b)


def test_and_group_or_predicate(flag_on):
    # (나이>=30 AND 구매금액>=100k) OR (장바구니 수량>=5) — 서로 다른 도메인 조합.
    plan, sql = _compile("나이가 30세 이상이고 구매금액이 100,000원 이상이거나 장바구니 수량이 5개 이상인 고객을 찾아줘.")
    assert plan.get("unsupported") is None
    w = _where(sql)
    assert "(B.AGE >= 30)" in w and "SUM(PAYMENT_AMT) >= 100000" in w and "SUM(QTY) >= 5" in w
    assert " OR " in w and " AND " in w
    # OR 가 최상위: 카트 분기가 AND 그룹 밖에 있다.
    assert w.count(" OR ") == 1


def test_same_metric_different_thresholds(flag_on):
    plan, sql = _compile("구매금액이 1,000,000원 이상이거나 구매금액이 500,000원 이상이면서 마케팅에 동의한 회원")
    assert plan.get("unsupported") is None
    w = _where(sql)
    assert "SUM(PAYMENT_AMT) >= 1000000" in w and "SUM(PAYMENT_AMT) >= 500000" in w


# ══════════════════════════════════════════════════════════════════════════════
# 4. 통합: fail-close (부분 컴파일·미지원·괄호불균형)
# ══════════════════════════════════════════════════════════════════════════════
def _reason(query):
    plan = g.build_query_plan(query)
    g._apply_logical_expression(query, plan, _NR)
    g._promote_unknown_intent_for_target_signal(plan)
    return (plan.get("unsupported") or {}).get("reason"), g.build_sql_template_candidate(plan)


def test_unsupported_predicate_in_branch_fails_close(flag_on):
    reason, cand = _reason("쿠폰을 3개 이상 사용하거나 구매 횟수가 10회 이상인 회원")
    assert reason == "logical_expression_unsupported_predicate" and cand is None


def test_message_received_branch_fails_close(flag_on):
    reason, cand = _reason("캠페인 메시지를 3회 이상 받았거나 구매 횟수가 10회 이상인 회원")
    assert reason == "logical_expression_unsupported_predicate" and cand is None


def test_paren_imbalance_fails_close(flag_on):
    reason, cand = _reason("(로그인 횟수가 100회 이상이거나 구매 횟수가 10회 이상인 회원")
    assert reason == "logical_expression_parse_failed" and cand is None


def test_no_and_only_fallback_when_flag_off():
    # flag off: 임계 낀 OR 은 mixed_and_or 게이트로 fail-close(AND-only 폴백 금지).
    plan = g.build_query_plan("로그인 횟수가 100회 이상이거나 구매 횟수가 10회 이상이면서 마케팅에 동의한 회원")
    g._apply_logical_expression("로그인 횟수가 100회 이상이거나 구매 횟수가 10회 이상이면서 마케팅에 동의한 회원", plan, _NR)
    g._promote_unknown_intent_for_target_signal(plan)
    assert (plan.get("unsupported") or {}).get("reason") == "mixed_and_or_precedence_unsupported"
    assert g.build_sql_template_candidate(plan) is None


# ══════════════════════════════════════════════════════════════════════════════
# 5. 하위 호환: 회원 속성 OR·순수 AND 은 새 계층을 통하지 않고 동일 결과
# ══════════════════════════════════════════════════════════════════════════════
def test_member_attr_or_not_intercepted(flag_on):
    for query, needle in [
        ("골드 또는 VIP 회원 중 적립금이 10,000원 이상인 고객", "EMART_GRADE_CD IN ('MEM_GRADE_CD.GOLD', 'MEM_GRADE_CD.VIP')"),
        ("서울 또는 경기 거주 회원 중 구매 횟수가 5회 이상인 고객", "B.SIDO IN ('경기', '서울')"),
    ]:
        plan, sql = _compile(query)
        assert plan.get("unsupported") is None
        assert plan.get("logical_expression") is None  # 새 계층 미개입
        assert needle in sql


def test_pure_and_not_intercepted(flag_on):
    plan, sql = _compile("30대 여성 중 구매 횟수가 5회 이상이고 구매금액이 500,000원 이상인 회원")
    assert plan.get("unsupported") is None and plan.get("logical_expression") is None


def _where(sql: str) -> str:
    """생성 SQL 의 논리식 WHERE 본문(첫 줄, 상태 조건 앞)만 추출."""
    for line in sql.splitlines():
        line = line.strip()
        if line.startswith("WHERE "):
            return re.sub(r"\s+", " ", line[len("WHERE "):]).strip()
    return ""
