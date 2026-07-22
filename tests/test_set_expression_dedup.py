"""집합식 피연산자 중복(부분겹침) 제거 회귀 테스트.

버그: "VIP 등급 고객"("vip등급고객")에서 VIP등급이 "vip등급"을 소비했는데도, member_grade 의 별칭
"등급 고객"("등급고객")이 "등급"에서 부분적으로 겹쳐 함께 매칭됐다. 예전 가드는 후보 compact 가 이미
선택된 표현의 substring 일 때만 걸러서(부분겹침은 통과), (VIP등급 * member_grade) 교집합이 생겼고
값 없는 member_grade 가 SQL 컴파일을 막았다. 이제 소비한 문자 구간(compact 좌표)을 추적해 구간이
겹치는 후보를 버린다(카탈로그가 긴 표현부터 정렬돼 greedy-longest 로 동작).

set_expression_engine 만 쓰므로 무거운 의존성 없이 로컬에서도 실행된다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_set_expression_dedup.py -q
"""

from pathlib import Path

import set_expression_engine as se

_CATALOG = se.load_set_term_catalog(Path("docs/data/normalization_rules.sample.json"))


def _canonicals(ast):
    if not isinstance(ast, dict):
        return []
    if ast.get("type") in ("operand", "age_range"):
        return [ast.get("canonical")]
    return _canonicals(ast.get("left")) + _canonicals(ast.get("right"))


def test_vip_grade_does_not_spawn_redundant_member_grade():
    ast = se._text_to_operand_ast("VIP 등급 고객", _CATALOG)
    assert ast is not None and ast.get("type") == "operand", ast
    assert ast["canonical"] == "VIP등급"
    # 부분겹침(등급)으로 헛매칭되던 member_grade 가 더는 나오지 않아야 한다.
    assert "member_grade" not in _canonicals(ast)


def test_single_grade_operand_stays_single():
    ast = se._text_to_operand_ast("VIP 고객", _CATALOG)
    assert ast.get("type") == "operand" and ast["canonical"] == "vip"


def test_distinct_grades_are_both_kept():
    # 진짜로 서로 다른 두 등급은 겹치지 않으므로 교집합으로 둘 다 살아 있어야 한다(과도한 dedup 방지).
    ast = se._text_to_operand_ast("골드 등급 실버 등급", _CATALOG)
    assert set(_canonicals(ast)) == {"gold_grade", "silver_grade"}


def test_age_and_gender_intersection_preserved():
    ast = se._text_to_operand_ast("20대 여성", _CATALOG)
    assert set(_canonicals(ast)) == {"age_20s", "female"}


def test_find_unclaimed_span_skips_overlap():
    # "vip등급고객"에서 [0,5)이 선점되면 "등급고객"(idx3~6)은 겹쳐서 None.
    hay = "vip등급고객"
    assert se._find_unclaimed_span(hay, "vip등급", []) == (0, 5)
    assert se._find_unclaimed_span(hay, "등급고객", [(0, 5)]) is None
    # 겹치지 않는 위치는 정상적으로 찾는다.
    assert se._find_unclaimed_span("골드실버", "실버", [(0, 2)]) == (2, 4)
