"""집합식 피연산자의 디멘션 레벨 canonical(회원등급/지역) 컴파일 회귀 테스트.

배경: 정규화 사전은 값(vip/서울)이 아니라 디멘션(member_grade/VIP등급/지역)을 canonical 로 내주기도
한다. 이전에는 _compile_set_operand 가 값 레벨 canonical(vip/female 등)만 알아서, LLM/룰 파서가
디멘션 canonical 을 실은 operand 를 만들면 SQL 생성 시점에 "사용자 집합 조건으로 컴파일할 수 없는
피연산자입니다: 지역/VIP등급/member_grade" 로 막혔다(타겟 수·행 수가 '-'). 이제 등급/지역 디멘션
operand 는 operand 표면형(value/text/matched_text/label 또는 canonical 이름)에서 구체 값을 복원해
데모 users 스키마 조건(u.lifecycle / u.region)으로 컴파일하고, 값을 못 찾으면 하드 실패 대신
"무슨 값인지" 되묻는 clarification 이슈를 돌려준다.

LLM 없이 순수 함수만 검증한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_set_expression_dimension.py -q
"""

import graph_rag as g


# (라벨, operand, 기대 SQL) — 디멘션 operand 가 값 조건으로 컴파일되어야 하는 케이스.
COMPILE_CASES = [
    ("VIP등급 (값이 canonical 이름에)", {"type": "operand", "canonical": "VIP등급"}, "u.lifecycle = 'vip'"),
    ("member_grade + value 골드", {"type": "operand", "canonical": "member_grade", "value": "골드"}, "u.lifecycle = 'gold_grade'"),
    ("grade + matched_text 실버 회원", {"type": "operand", "canonical": "grade", "matched_text": "실버 회원"}, "u.lifecycle = 'silver_grade'"),
    ("지역 + value 서울 거주", {"type": "operand", "canonical": "지역", "value": "서울 거주"}, "u.region = '서울'"),
    ("지역 + matched_text 서울특별시", {"type": "operand", "canonical": "지역", "matched_text": "서울특별시"}, "u.region = '서울'"),
    ("시도 + value 부산", {"type": "operand", "canonical": "시도", "value": "부산"}, "u.region = '부산'"),
]

# 값 없는 디멘션 operand — 하드 실패가 아니라 clarification(어떤 값인지 되묻기)로 떨어져야 한다.
CLARIFY_CASES = [
    ("member_grade 값 없음", {"type": "operand", "canonical": "member_grade", "label": "회원 등급"}, "회원 등급"),
    ("지역 값 없음", {"type": "operand", "canonical": "지역"}, "지역"),
]

# 기존 값 레벨 canonical 은 그대로 컴파일되어야 한다(회귀 방지).
REGRESSION_CASES = [
    ({"type": "operand", "canonical": "vip"}, "u.lifecycle = 'vip'"),
    ({"type": "operand", "canonical": "female"}, "u.gender = 'female'"),
]


def test_dimension_operands_compile_to_predicates():
    for label, operand, expected_sql in COMPILE_CASES:
        result = g._compile_set_operand(operand)
        assert result["is_valid"], f"{label}: 컴파일 실패 {result['issues']}"
        assert result["expression_sql"] == expected_sql, f"{label}: {result['expression_sql']} != {expected_sql}"


def test_valueless_dimension_operands_ask_for_clarification():
    for label, operand, hint in CLARIFY_CASES:
        result = g._compile_set_operand(operand)
        assert not result["is_valid"], f"{label}: 값 없는 디멘션인데 컴파일됨"
        joined = "; ".join(result["issues"])
        assert "지정해 주세요" in joined and hint in joined, f"{label}: 부적절한 이슈 {result['issues']}"


def test_value_level_canonicals_still_compile():
    for operand, expected_sql in REGRESSION_CASES:
        result = g._compile_set_operand(operand)
        assert result["is_valid"] and result["expression_sql"] == expected_sql, f"{operand}: {result}"


def test_unknown_operand_still_errors():
    result = g._compile_set_operand({"type": "operand", "canonical": "blahblah"})
    assert not result["is_valid"]
    assert "컴파일할 수 없는 피연산자" in "; ".join(result["issues"])


def test_union_expression_of_region_and_grade():
    ast = {
        "type": "set_op",
        "op": "+",
        "left": {"type": "operand", "canonical": "지역", "value": "서울"},
        "right": {"type": "operand", "canonical": "VIP등급"},
    }
    result = g._compile_set_expression_ast(ast)
    assert result["is_valid"], result["issues"]
    assert result["expression_sql"] == "(u.region = '서울' OR u.lifecycle = 'vip')"


# --- upstream 값 보강(_enrich_set_expression_operand_values): 값 없는 디멘션 operand에 프롬프트에서 복원한 값 주입 ---

def test_enrichment_fills_region_value_from_prompt():
    # 재작성/정규화가 "서울 거주"를 값 없는 지역 operand로 뭉갠 케이스 — 프롬프트에서 서울을 복원해야 한다.
    plan = {"set_expressions": [{"set_ast": {
        "type": "set_op", "op": "+",
        "left": {"type": "operand", "canonical": "지역"},
        "right": {"type": "operand", "canonical": "VIP등급"},
    }}]}
    g._enrich_set_expression_operand_values(plan, "서울 거주 고객, VIP 등급 고객")
    ast = plan["set_expressions"][0]["set_ast"]
    assert ast["left"].get("value") == "서울"
    compiled = g._compile_set_expression_ast(ast)
    assert compiled["is_valid"] and compiled["expression_sql"] == "(u.region = '서울' OR u.lifecycle = 'vip')"


def test_enrichment_boundary_guard_rejects_substring_region():
    # '경기'가 '경기침체' 같은 무관 단어에 얻어걸려선 안 된다(_value_token_mentioned 경계검사).
    assert g._region_value_from_query("경기침체로 소비가 준 고객") is None
    plan = {"set_expressions": [{"set_ast": {"type": "operand", "canonical": "지역"}}]}
    g._enrich_set_expression_operand_values(plan, "경기침체 관련 캠페인")
    assert "value" not in plan["set_expressions"][0]["set_ast"]


def test_enrichment_is_idempotent_when_value_present():
    # 이미 값이 있으면 프롬프트에 다른 지역이 있어도 덮어쓰지 않는다.
    plan = {"set_expressions": [{"set_ast": {"type": "operand", "canonical": "지역", "value": "부산"}}]}
    g._enrich_set_expression_operand_values(plan, "서울 거주 고객")
    assert plan["set_expressions"][0]["set_ast"]["value"] == "부산"


def test_region_and_grade_query_recognizers():
    assert g._region_value_from_query("서울특별시 거주 회원") == "서울"
    assert g._grade_value_from_query("VIP 등급 고객만 골라줘") == "vip"
    assert g._grade_value_from_query("아무 조건 없음") is None


# --- LLM 이 만든 malformed 집합식 방어(_coerce_llm_set_expression): 알 수 없는 노드 타입은 버린다 ---

def test_structural_validation_rejects_unknown_node_type():
    # 정상 구조는 통과
    assert g._set_ast_is_structurally_valid({"type": "operand", "canonical": "vip"})
    assert g._set_ast_is_structurally_valid(
        {"type": "set_op", "op": "+", "left": {"type": "operand", "canonical": "vip"},
         "right": {"type": "age_range", "age_min": 20, "age_max": 29}}
    )
    # LLM 이 지어낸 노드 타입/구조는 거절
    assert not g._set_ast_is_structurally_valid({"type": "threshold", "column": "amount", "op": ">=", "value": 1000000})
    assert not g._set_ast_is_structurally_valid(
        {"type": "set_op", "op": "+", "left": {"type": "operand", "canonical": "vip"},
         "right": {"type": "metric", "name": "purchase_amount"}}
    )
    assert not g._set_ast_is_structurally_valid({"type": "set_op", "op": "&", "left": {}, "right": {}})


def test_coerce_drops_malformed_llm_set_expression():
    # 알 수 없는 노드가 든 LLM 집합식은 coercion 단계에서 버려져 SQL 을 막지 못한다.
    assert g._coerce_llm_set_expression({"set_ast": {"type": "threshold", "value": 1000000}}) is None
    # 정상 집합식은 유지된다.
    kept = g._coerce_llm_set_expression({"set_ast": {"type": "operand", "canonical": "vip"}})
    assert kept is not None and kept["set_ast"]["canonical"] == "vip"


def test_drop_uncompilable_set_expression_source_agnostic():
    # 지표 canonical '구매금액'을 operand 로 매칭(컴파일 불가) → source 무관하게 버려 SQL 을 막지 않는다.
    llm_bad = {"source": "llm_set_expression_ast", "set_ast": {"type": "operand", "canonical": "구매금액"}}
    rules_bad = {"source": "rules_set_expression", "set_ast": {"type": "operand", "canonical": "구매금액"}}
    # 컴파일 가능한 집합식(vip) → 유지.
    good = {"source": "llm_set_expression_ast", "set_ast": {"type": "operand", "canonical": "vip"}}
    # 정규화 못한 값(unknown_operand)은 진짜 clarification 이라 유지.
    unknown = {"source": "rules_set_expression", "set_ast": {"type": "unknown_operand", "text": "모르는말"}}
    plan = {"set_expressions": [llm_bad, rules_bad, good, unknown]}
    g._drop_uncompilable_set_expressions(plan)
    canonicals = [e.get("set_ast", {}).get("canonical") for e in plan["set_expressions"]]
    assert "구매금액" not in canonicals            # 인식된-비지원 canonical 은 source 무관 제거
    assert "vip" in canonicals                     # 컴파일 가능 유지
    assert unknown in plan["set_expressions"]      # unknown_operand clarification 유지


def test_drop_keeps_compilable_union_and_difference():
    # 진짜 집합연산(합집합/차집합)은 컴파일되므로 유지되어야 한다(과도한 drop 방지).
    union = {"source": "rules_set_expression", "set_ast": {"type": "set_op", "op": "+",
             "left": {"type": "operand", "canonical": "vip"}, "right": {"type": "operand", "canonical": "female"}}}
    plan = {"set_expressions": [union]}
    g._drop_uncompilable_set_expressions(plan)
    assert plan["set_expressions"] == [union]
