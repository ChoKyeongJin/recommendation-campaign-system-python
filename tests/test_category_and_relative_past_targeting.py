"""카테고리 값 타겟 + 과거 시점 창('N년 전') 회귀.

배경: '7년전 카테고리가 "어린이건강"을 구매한 고객' 이 두 조건을 모두 잃었다.
  (1) 카테고리 **값**: 디멘션어('카테고리')는 일반명사로 걸러지는데 그 자리에서 사용자가 말한 값을
      되찾는 경로가 없었다. 재작성본("'어린이건강' 카테고리에서 구매한")에서는 더 나쁘게, 조사가 붙어
      일반명사 검사를 우회한 축 이름이 상품 LIKE(N'%카테고리에서%')로 새어 0명 SQL 이 됐다.
  (2) 과거 시점 창: 'N년 전'은 롤링 창 파서가 exclude_past 로 건너뛰기만 하고 아무도 읽지 않아
      기간이 통째로 사라졌다(전 기간 구매로 컴파일).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_category_and_relative_past_targeting.py -q
"""

from datetime import date

import pytest

import calendar_window as cw
import graph_rag as g


# 창 문법(calendar_window)은 기준일을 주입받으므로 고정 날짜로 검증한다. 계획/SQL 경로는 실행 시점의
# 오늘을 기준일로 쓰므로 date.today() 로 기대값을 계산한다(연도가 바뀌어도 테스트가 썩지 않게).
TODAY = date(2026, 7, 28)
SEVEN_YEARS_AGO = date.today().year - 7


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sql(query: str) -> str:
    candidate = g.build_sql_template_candidate(_plan(query))
    assert candidate is not None, f"{query!r}: SQL 미생성"
    return candidate["sql"]


# ── 과거 시점 창 문법(calendar_window 소유) ──────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("7년전 구매한 고객", ("20190101", "20191231")),   # 그 해 전체
    ("3개월 전에 구매한 고객", ("20260401", "20260430")),  # 그 달 전체
    ("2주 전 주문한 고객", ("20260713", "20260719")),   # 그 주(월~일)
    ("10일 전 구매한 고객", ("20260718", "20260718")),  # 그 날 하루
])
def test_relative_past_window_grain_follows_unit(text, expected):
    window = cw.parse_relative_past_window(text, today=TODAY)
    assert (window["from"], window["to"]) == expected


def test_relative_past_window_ignores_boundary_forms():
    """'3년 전부터'는 시점이 아니라 그 시점을 경계로 삼는 범위 — 잡지 않는다(fail-close)."""
    assert cw.parse_relative_past_window("3년 전부터 구매한 고객", today=TODAY) is None


def test_relative_past_window_span_marks_source_text():
    assert cw.parse_relative_past_window_span("7년전 구매한 고객") == (0, 3)


# ── 구매일 슬롯(도메인 게이트는 graph_rag 소유) ─────────────────────────────
def test_relative_past_becomes_purchase_date_window():
    slot = g._parse_purchase_date_period("7년전 구매한 고객")
    assert (slot["from"], slot["to"]) == (f"{SEVEN_YEARS_AGO}0101", f"{SEVEN_YEARS_AGO}1231")


def test_absolute_year_wins_over_relative_past():
    slot = g._parse_purchase_date_period("2019년 3월에 구매한 고객 (3년 전 대비)")
    assert (slot["from"], slot["to"]) == ("20190301", "20190331")


def test_relative_past_not_claimed_when_other_date_anchor_present():
    """'3개월 전 가입'의 시점을 구매일로 뒤바꾸지 않는다."""
    assert g._parse_purchase_date_period("3개월 전 가입한 회원에게 구매 유도") is None


def test_purchase_date_span_absent_when_slot_not_claimed():
    assert g._purchase_date_span("3개월 전 가입한 회원에게 구매 유도", {}) is None


# ── 스코프 분리 소실 복원 ───────────────────────────────────────────────────
# 타겟/채널 절 분리(LLM)가 기간 표현을 통째로 지우면 창이 계획 입력에 도달하지 못한다. 계획 문장 기준의
# 고아 창 귀속으로는 되찾을 수 없어, 원문 재파싱 복원이 유일한 안전망이다.
SPLIT_LOST_TARGETING = "어린이건강 카테고리에서 구매한 고객"  # 분리기가 '7년전'을 지운 계획 문장


def test_purchase_date_restored_from_source_when_scope_split_drops_it():
    plan = g.build_query_plan(SPLIT_LOST_TARGETING, parser="rules")
    assert plan["target_user"].get("purchase_date") is None
    g._restore_purchase_date_from_source('7년전 카테고리가 "어린이건강"을 구매한 고객 추출해줘', plan)
    slot = plan["target_user"]["purchase_date"]
    assert (slot["from"], slot["to"]) == (f"{SEVEN_YEARS_AGO}0101", f"{SEVEN_YEARS_AGO}1231")


def test_source_restore_declines_when_window_belongs_elsewhere():
    """원문의 시점이 가입 시점이면 주문일로 뒤바꾸지 않는다."""
    plan = g.build_query_plan(SPLIT_LOST_TARGETING, parser="rules")
    g._restore_purchase_date_from_source("3개월 전 가입한 회원 중 구매한 고객", plan)
    assert plan["target_user"].get("purchase_date") is None


def test_source_restore_declines_without_order_fact():
    """계획이 주문 팩트를 요구하지 않으면 주문일 창을 만들지 않는다."""
    plan = g.build_query_plan("30대 여성 회원", parser="rules")
    g._restore_purchase_date_from_source("7년전 구매한 고객", plan)
    assert plan["target_user"].get("purchase_date") is None


# ── 카테고리 값 추출 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("query", [
    '7년전 카테고리가 "어린이건강"을 구매한 고객 추출해줘',   # 원문(따옴표 + 주격 계사)
    "7년 전 '어린이건강' 카테고리에서 구매한 고객",             # 재작성본(인접형 + 조사)
    "어린이건강 카테고리 구매 고객",
    "카테고리가 어린이건강인 상품을 구매한 회원",
])
def test_category_value_and_kind(query):
    target_user = _plan(query)["target_user"]
    assert target_user.get("purchase_object") == "어린이건강"
    assert target_user.get("purchase_object_kind") == "category"


def test_category_kind_matches_only_category_columns():
    sql = _sql('7년전 카테고리가 "어린이건강"을 구매한 고객 추출해줘')
    assert "P.CATEGORY LIKE N'%어린이건강%'" in sql
    assert "P.CATEGORYM_NAME LIKE N'%어린이건강%'" in sql
    assert "PRODUCT_NAME LIKE N'%어린이건강%'" not in sql
    assert "BRAND_NAME LIKE N'%어린이건강%'" not in sql


def test_category_target_keeps_relative_past_window_in_sql():
    sql = _sql('7년전 카테고리가 "어린이건강"을 구매한 고객 추출해줘')
    assert f"BETWEEN '{SEVEN_YEARS_AGO}0101' AND '{SEVEN_YEARS_AGO}1231'" in sql


def test_axis_name_never_becomes_product_like():
    """축 이름이 조사를 달고 상품 LIKE 로 새면 안 된다(N'%카테고리에서%' → 0명)."""
    sql = _sql("7년 전 '어린이건강' 카테고리에서 구매한 고객")
    assert "카테고리에서" not in sql


def test_particle_stripped_only_when_stem_survives():
    assert g._sanitize_purchase_object("카테고리에서") == "카테고리"
    assert g._sanitize_purchase_object("제로") == "제로"  # 어간 1글자 축약은 조사로 보지 않는다


# ── 기존 트랙 보존 ──────────────────────────────────────────────────────────
def test_brand_mention_still_wins_its_kind():
    target_user = _plan("알로루 브랜드 상품 구매한 고객")["target_user"]
    assert target_user.get("purchase_object_kind") == "brand"


@pytest.mark.parametrize("query", [
    "서로 다른 카테고리 2개 이상 구매한 고객",  # 가짓수를 세는 축 — 값이 아니다
    "특정 카테고리 구매 고객",                   # 자리표시자
    "카테고리별 구매 고객 수",                   # 그룹 축
])
def test_dimension_axis_expressions_are_not_category_values(query):
    assert _plan(query)["target_user"].get("purchase_object") is None
