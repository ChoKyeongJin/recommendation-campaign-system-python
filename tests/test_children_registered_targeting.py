"""자녀정보 등록 여부(CHILDREN_YN) 타겟 회귀.

배경: '자녀정보가 등록되어 있고 …'에서 '자녀'가 정규화 사전에서 parent(육아 페르소나, 회원 컬럼으로
표현 불가)로 매칭돼 조건이 조용히 탈락했다. 실컬럼 CHILDREN_YN('자녀정보 보유 여부' Y/N)이 있고
children_registered 매핑도 있었지만 boolean_filters 섹션에만 있어(코드는 eq_filters 만 파싱) 죽어 있었다.

고정 내용:
  - children_registered 를 eq_filters(category family, B.CHILDREN_YN, 'Y')로 승격 → 컴파일 가능.
  - '자녀(정보) 등록/보유/있음' → CHILDREN_YN = 'Y'(include), '없음/미등록' → <> 'Y'(exclude).
  - '등록/보유/있음' 문맥이 붙은 경우만 발동 — '자녀 선물' 같은 비속성 언급은 건드리지 않는다.
  - 컬럼/값 소유: member_target_filters.json eq_filters family 카테고리(children_registered).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_children_registered_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _child_preds(query: str) -> list[str]:
    comp = g.compile_member_target_conditions(_plan(query))
    return [p for p in comp["predicates"] if "CHILDREN_YN" in p]


def test_children_registered_eq_filter_exists():
    assert g.MEMBER_EQ_FILTERS.get("children_registered") == ("family", "B.CHILDREN_YN", "Y")


def test_children_registered_include():
    plan = _plan("자녀정보가 등록되어 있고 여성인 회원만 추출해줘.")
    assert "children_registered" in plan["target_user"]["lifecycle"]
    assert _child_preds("자녀정보가 등록되어 있고 여성인 회원만 추출해줘.") == ["B.CHILDREN_YN = 'Y'"]


def test_children_registered_full_sql_with_gender():
    # 실제 파이프라인의 재작성 타겟팅 프롬프트 기준(여성 + 자녀 등록 동시).
    plan = _plan("자녀 정보 등록 여성 회원")
    cand = g.build_member_targets_sql_candidate(plan)
    assert cand is not None
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in cand["sql"]
    assert "B.CHILDREN_YN = 'Y'" in cand["sql"]


def test_children_absent_is_exclude():
    assert _child_preds("자녀 정보 없는 회원") == ["B.CHILDREN_YN <> 'Y'"]
    assert _child_preds("자녀 등록 안 한 회원") == ["B.CHILDREN_YN <> 'Y'"]


def test_children_present_short_phrasing():
    assert _child_preds("자녀 있는 회원") == ["B.CHILDREN_YN = 'Y'"]


def test_non_attribute_child_mention_not_promoted():
    # '등록/보유/있음' 문맥이 없으면 자녀 속성 조건을 만들지 않는다.
    plan = _plan("자녀 선물 살 회원")
    assert "children_registered" not in plan["target_user"].get("lifecycle", [])
    assert "children_registered" not in plan["exclude"].get("lifecycle", [])
    assert _child_preds("자녀 선물 살 회원") == []
