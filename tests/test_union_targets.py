"""합집합(OR) 타겟 컴파일 회귀 테스트.

배경: "A 이거나 B 또는 C" 는 OR(합집합)인데 (1) 재작성이 OR 을 콤마로 뭉개고 (2) 회원속성·집계 조건이
서로 다른 메커니즘이라 기본 빌더가 전부 AND 로만 결합해 교집합으로 잘못 타겟됐다. 이제 원본에서 top-level
합집합을 감지(_apply_union_condition)해 union_condition(set_ast)으로 붙이고, 실CRM 술어로 재귀 컴파일해
하나의 CRM_MB_BASEINFO 쿼리에서 OR 로 묶는다(집계 조건은 회원키 IN 서브쿼리).

LLM 없이 결정론 경로(rules)만 검증한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_union_targets.py -q
"""

from pathlib import Path

import graph_rag as g

_NR = Path("docs/data/normalization_rules.sample.json")
_ORIGINAL = (
    "서울 지역에 거주하는 고객이거나 VIP 등급 이상인 고객 또는 최근 90일 동안 누적 구매 금액이 "
    "100만 원 이상인 고객을 대상으로 오프라인 행사 초대 캠페인을 만들어줘"
)
# 재작성본(OR 이 콤마로 뭉개진 형태) — 값·임계값은 여기서 결정론 필터가 뽑는다.
_REWRITE = "서울 거주 고객, VIP 등급 이상 고객, 최근 90일 누적 구매 금액 100만 원 이상 고객"


def _union_plan():
    plan = g.build_query_plan(_REWRITE)
    g._apply_union_condition(_ORIGINAL, plan, _NR)
    return plan


def test_or_connectives_recognized_as_union():
    import set_expression_engine as se
    ast = se.parse_set_expressions_from_query(
        "서울 지역 거주 고객이거나 VIP 등급 이상 고객", normalization_path=_NR
    )[0]["set_ast"]
    assert ast["op"] == "+"  # 이거나 → 합집합


def test_union_condition_attached_from_original():
    plan = _union_plan()
    assert plan.get("combine_mode") == "or"
    assert isinstance(plan.get("union_condition"), dict)
    assert plan["union_condition"]["op"] == "+"


def test_union_sql_ors_region_grade_and_aggregate():
    plan = _union_plan()
    candidate = g.build_union_targets_sql_candidate(plan)
    assert candidate is not None
    sql = candidate["sql"]
    assert "B.SIDO IN ('서울')" in sql
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'" in sql
    assert "B.MEMBER_NO IN (SELECT MEMBER_NO FROM CRM_SL_ORDERHEADERMALL" in sql
    assert "SUM(PAYMENT_AMT) >= 1000000" in sql
    # 핵심: 세 조건이 AND 가 아니라 OR 로 묶여야 한다.
    assert " OR " in sql
    assert "SIDO IN ('서울')\n  AND B.EMART_GRADE_CD" not in sql  # 교집합(AND)로 묶이면 안 됨
    # 정상회원 게이트만 AND 로 남는다.
    assert "AND B.MEMBER_STATE_CD" in sql


def test_and_only_prompt_does_not_trigger_union():
    for q in ["20대 여성 고객", "서울 거주 20대 여성 고객 대상 캠페인", "VIP 등급 고객 찾아줘"]:
        plan = g.build_query_plan(q)
        g._apply_union_condition(q, plan, _NR)
        assert plan.get("combine_mode") != "or", q
        assert g.build_union_targets_sql_candidate(plan) is None, q


def test_crm_set_ast_falls_back_when_operand_uncompilable():
    # 지원 안 되는 operand(정규화 못한 값)면 전체 컴파일 실패 → None(폴백).
    ast = {"type": "set_op", "op": "+",
           "left": {"type": "operand", "canonical": "vip"},
           "right": {"type": "unknown_operand", "text": "모르는말"}}
    assert g._compile_crm_set_ast(ast, {"target_user": {}}) is None


def test_crm_set_ast_compiles_difference_and_intersection():
    plan = {"target_user": {}, "dimension_filters": []}
    # vip AND NOT female
    ast = {"type": "set_op", "op": "-",
           "left": {"type": "operand", "canonical": "vip"},
           "right": {"type": "operand", "canonical": "female"}}
    pred = g._compile_crm_set_ast(ast, plan)
    assert pred == "(B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP' AND NOT B.GENDER_CD = 'GENDER_CD.FEMALE')"
