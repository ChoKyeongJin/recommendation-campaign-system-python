"""세그먼트 구성(segment_composition) 회귀 — 특히 실CRM 축에 '등급'을 추가하고 질문 관련 축을 우선 노출.

배경: 실DB 세그먼트 구성이 성별·연령대·지역 3축만 계산해, 'GOLD 이상' 처럼 등급으로 타겟한 질문에도
등급 구성이 안 나오고 무관한 데모그래픽만 보였다. 등급(EMART_GRADE_CD) 축을 추가하고, 질문이 지정한
축(등급·지역)을 relevance 로 우선 노출한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_segment_composition.py -q
"""

import api
import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


# ── 등급 라벨/서열 ──────────────────────────────────────────────────────────────────────
def test_grade_label_maps_code_to_korean():
    assert api._grade_label("MEM_GRADE_CD.GOLD") == "골드"
    assert api._grade_label("MEM_GRADE_CD.VIP") == "VIP"
    assert api._grade_label(None) == "미상"
    assert api._grade_label("") == "미상"


def test_grade_sort_is_by_rank_low_to_high():
    segs = api._counts_to_segments({"VIP": 5, "실버": 100, "골드": 30}, sort_key=api._grade_sort_key)
    assert [s["value"] for s in segs] == ["실버", "골드", "VIP"]
    # 미지의 라벨은 뒤로.
    segs2 = api._counts_to_segments({"미상": 3, "골드": 1}, sort_key=api._grade_sort_key)
    assert [s["value"] for s in segs2] == ["골드", "미상"]


def test_grade_group_registered():
    assert api.SEGMENT_GROUP_TITLES.get("grade") == "등급"
    assert api._GRADE_LIFECYCLE_CANONICALS  # graph_rag 레지스트리에서 파생(비어있지 않음)
    assert {"gold_grade", "vip"} <= api._GRADE_LIFECYCLE_CANONICALS


# ── relevance: 질문이 지정한 축을 우선 노출 ─────────────────────────────────────────────
def test_grade_condition_promotes_grade_axis():
    rel = api._segment_relevance(_plan("GOLD 이상 회원을 추출해줘"))
    assert rel.get("grade", {}).get("priority") == api.SEGMENT_PRIORITY_PRIMARY


def test_region_condition_promotes_region_axis():
    rel = api._segment_relevance(_plan("서울에 거주하는 회원을 추출해줘"))
    assert rel.get("region", {}).get("priority") == api.SEGMENT_PRIORITY_PRIMARY


def test_no_grade_condition_does_not_promote_grade():
    rel = api._segment_relevance(_plan("30대 여성 회원"))
    assert "grade" not in rel


# ── presentation: 관련 축만 노출하고 무관 축은 접는다(hidden) ─────────────────────────────
_COMPOSITION = {
    "gender": [{"value": "여성", "count": 10}],
    "age_band": [{"value": "30대", "count": 10}],
    "region": [{"value": "서울", "count": 10}],
    "grade": [{"value": "골드", "count": 8}, {"value": "VIP", "count": 2}],
}


def test_grade_query_shows_only_grade_and_hides_demographics():
    # 'GOLD 이상'만 지정 → 등급만 노출, 성별·연령·지역은 접힌다(hidden).
    plan = _plan("GOLD 이상 회원 중 장바구니를 보유하고 최근 90일 이내 구매가 없는 회원")
    pres = api._external_segment_presentation(_COMPOSITION, plan)
    assert [g["title"] for g in pres["relevant_groups"]] == ["등급"]
    assert pres["relevant_groups"][0]["priority"] == api.SEGMENT_PRIORITY_PRIMARY
    assert [s["label"] for s in pres["relevant_groups"][0]["segments"]] == ["골드", "VIP"]
    hidden = {g["key"] for g in pres["hidden_group_keys"]}
    assert hidden == {"gender", "age_band", "region"}


def test_gender_region_query_shows_those_hides_rest():
    # 성별·지역을 지정 → 그 둘만 노출, 연령·등급은 접힌다.
    plan = _plan("서울에 거주하는 여성 회원")
    pres = api._external_segment_presentation(_COMPOSITION, plan)
    shown = {g["key"] for g in pres["relevant_groups"]}
    hidden = {g["key"] for g in pres["hidden_group_keys"]}
    assert shown == {"gender", "region"}
    assert hidden == {"age_band", "grade"}


def test_empty_composition_is_safe():
    pres = api._external_segment_presentation({}, _plan("GOLD 이상 회원"))
    assert pres["relevant_groups"] == []
    assert pres["hidden_group_keys"] == []
