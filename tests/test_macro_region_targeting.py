"""광역 권역어(수도권 등) 타겟 회귀.

배경: "수도권 거주 회원…"에서 '수도권'이 단일 SIDO 저장값이 아니라 서울/경기/인천을 묶은 관용어라
member_value_index 에 없어 지역 조건이 통째로 빠졌다("수도권이라고 하니깐 조건에 안 들어감").

고정 내용:
  - macro_regions(member_target_filters.json) 매핑으로 권역어를 구성 시도명으로 확장한다.
  - _apply_macro_region_filter 가 SIDO dimension_filter(값 인덱스와 같은 형태)를 만들어
    _member_region_predicates 가 SIDO IN 으로 컴파일한다.
  - 값 인덱스 뒤에 실행해, 사용자가 구체 시도('부산')도 같이 말했으면 합집합으로 병합한다.
  - 재작성 프롬프트는 권역어를 라벨·effective_query 에서 구성 시도로 풀어 보여준다(prompt_rewrite_system.txt).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_macro_region_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sido_codes(query: str) -> set[str]:
    for f in _plan(query).get("dimension_filters", []):
        if (f.get("column") or "").split(".")[-1].upper() == "SIDO":
            return set(f.get("codes", []))
    return set()


def test_macro_regions_config_loaded():
    groups = g._MEMBER_TARGET_FILTERS["macro_regions"]["groups"]
    assert groups["수도권"] == ["서울", "경기", "인천"]


def test_sudogwon_expands_to_three_sido():
    assert _sido_codes("수도권 거주 회원 중 이메일 수신동의만 되어 있는 사람을 보여줘.") == {"서울", "경기", "인천"}


def test_sudogwon_compiles_to_sido_in_predicate():
    compiled = g.compile_member_target_conditions(_plan("수도권 거주 이메일 수신동의 회원"))
    joined = " AND ".join(compiled["predicates"])
    assert "B.SIDO IN (" in joined
    for sido in ("'서울'", "'경기'", "'인천'"):
        assert sido in joined
    assert "B.EMAIL_YN = 'Y'" in joined


def test_youngnam_expands_to_five_sido():
    assert _sido_codes("영남권에 사는 VIP 회원") == {"부산", "대구", "울산", "경북", "경남"}


def test_macro_region_merges_with_explicit_sido():
    # '수도권과 부산' -> 명시 시도(부산)와 매크로 확장을 합집합으로 병합한다(부산이 덮이지 않음).
    assert _sido_codes("수도권과 부산 거주 회원") == {"서울", "경기", "인천", "부산"}


def test_non_macro_region_unaffected():
    # 권역어가 없으면 매크로 확장을 하지 않는다(구체 시도는 값 인덱스가 그대로 처리).
    assert _sido_codes("서울 거주 회원") == {"서울"}
