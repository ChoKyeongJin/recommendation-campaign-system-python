"""브랜드 언급 타겟팅(구매 상품어 추출 확장) 회귀 테스트.

배경: "브랜드가 알로루인 곳에 10% 할인 쿠폰을 뿌리고 싶어"가 no_sql_candidates 로 실패했다.
원인 3가지를 각각 고정한다.
  A. 구매 동사 없는 계사형 브랜드 언급("브랜드가 X인 곳")이 타겟 조건으로 안 잡힘 → 구매 이력 타겟 승격
  B. "X 브랜드 상품 구매한"에서 단일 토큰 캡처가 일반명사('상품')를 상품명으로 오인 → 앞의 실제 이름 재시도
  C. 특수문자 생략 표기('알로루')가 DB 표기('알로&루')와 달라 LIKE 0건 → 브랜드 스냅샷 정규화 일치 보정

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_brand_targeting.py -q
"""

import pytest

import graph_rag as g


@pytest.fixture
def brand_snapshot(monkeypatch):
    monkeypatch.setattr(g, "_purchase_brand_names", lambda: ("알로&루", "NIKE", "포멜카멜리"))


# --- C. 브랜드명 표기 보정 ---

def test_canonicalize_special_char_brand(brand_snapshot):
    assert g._canonicalize_product_term("알로루") == "알로&루"


def test_canonicalize_restores_ascii_casing(brand_snapshot):
    assert g._canonicalize_product_term("nike") == "NIKE"


def test_canonicalize_multi_token_per_token(brand_snapshot):
    assert g._canonicalize_product_term("알로루 티셔츠") == "알로&루 티셔츠"


def test_canonicalize_no_match_keeps_original(brand_snapshot):
    assert g._canonicalize_product_term("기저귀") == "기저귀"


def test_canonicalize_without_snapshot_is_noop(monkeypatch):
    monkeypatch.setattr(g, "_purchase_brand_names", lambda: ())
    assert g._canonicalize_product_term("알로루") == "알로루"


# --- A. 계사형 브랜드 언급 → 구매 이력 타겟 ---

def test_brand_copula_promotes_purchase_object(brand_snapshot):
    plan = g.build_query_plan("브랜드가 알로루인 곳에 10% 할인 쿠폰을 뿌리고 싶어")
    assert plan["target_user"]["purchase_object"] == "알로&루"
    assert plan["target_user"]["purchase_object_kind"] == "brand"


def test_brand_copula_builds_brand_only_sql(brand_snapshot):
    # 브랜드로 확정된 타겟은 광역 6컬럼 LIKE 가 아니라 BRAND_NAME 만 매칭한다(정밀도).
    plan = g.build_query_plan("브랜드가 알로루인 곳에 10% 할인 쿠폰을 뿌리고 싶어")
    candidate = g.build_purchase_history_targets_sql_candidate(plan)
    assert candidate is not None
    assert "P.BRAND_NAME LIKE N'%알로&루%'" in candidate["sql"]
    assert "CATEGORY" not in candidate["sql"]
    assert "PRODUCT_NAME" not in candidate["sql"]
    assert "CRM_SL_ORDERDETAILMALL" in candidate["sql"]


def test_non_brand_object_keeps_wide_columns(brand_snapshot):
    # 브랜드 확정이 아닌 상품어("기저귀")는 기존 광역 매칭 유지 — 카테고리/브랜드/상품명 어디든 잡혀야 한다.
    plan = g.build_query_plan("기저귀를 구매한 고객")
    assert "purchase_object_kind" not in plan["target_user"]
    candidate = g.build_purchase_history_targets_sql_candidate(plan)
    assert candidate is not None
    assert "P.CATEGORY LIKE N'%기저귀%'" in candidate["sql"]
    assert "P.BRAND_NAME LIKE N'%기저귀%'" in candidate["sql"]


def test_known_brand_value_marks_brand_kind(brand_snapshot):
    # '브랜드'라는 단어 없이도 값 자체가 실DB 브랜드명이면 브랜드로 확정한다.
    plan = g.build_query_plan("포멜카멜리 구매한 고객")
    assert plan["target_user"]["purchase_object"] == "포멜카멜리"
    assert plan["target_user"]["purchase_object_kind"] == "brand"


# --- 연결형 계사("~면서") + 지시어("이곳에서") 조응 ---

def test_brand_connective_copula_with_anaphora(brand_snapshot):
    # "브랜드가 알로루면서 … 이곳에서 구매한 여성": '이곳에서'는 지시어(상품 아님)로 걸러지고,
    # 연결형 계사에서 브랜드가 추출돼 성별·구매년도와 AND 결합된다.
    plan = g.build_query_plan("브랜드가 알로루면서 2019년도에 이곳에서 구매한 여성에게 10% 할인 쿠폰을 뿌리고 싶어")
    tu = plan["target_user"]
    assert tu["purchase_object"] == "알로&루"
    assert tu["purchase_object_kind"] == "brand"
    assert tu["gender"] == "female"
    assert tu["purchase_date"]["from"] == "20190101" and tu["purchase_date"]["to"] == "20191231"


def test_brand_connective_variants(brand_snapshot):
    for phrase in ["브랜드가 알로루이면서 구매한 고객", "브랜드가 알로루이고 휴면인 고객", "브랜드가 알로루인 곳"]:
        assert "알로루" in g._brand_object_signals(phrase), phrase


def test_anaphora_alone_is_not_product():
    target_user: dict = {}
    g._apply_purchase_object_filter("이곳에서 구매한 고객", target_user)
    assert "purchase_object" not in target_user


def test_rewrite_guard_catches_brand_name_mutation(brand_snapshot):
    # LLM 재작성이 브랜드명을 오타로 변형(알로루→알로르)한 실사례 — 연결형 계사도 게이트가 보호한다.
    dropped = g._rewrite_dropped_signals(
        "브랜드가 알로루면서 2019년도에 이곳에서 구매한 여성에게 10% 할인 쿠폰을 뿌리고 싶어",
        "브랜드가 알로르인 2019년도에 이곳에서 구매한 여성에게 10% 할인 쿠폰을 제공하고자 합니다.",
    )
    assert any("브랜드 조건" in item and "알로루" in item for item in dropped)


def test_generic_noun_alone_is_not_brand_copula():
    # "브랜드가 상품인 곳" 같은 무의미 표현은 일반명사 제외로 타겟이 되지 않는다.
    target_user: dict = {}
    g._apply_purchase_object_filter("브랜드가 상품인 곳에 쿠폰", target_user)
    assert "purchase_object" not in target_user


# --- B. 일반명사 재시도 캡처 ---

def test_brand_before_generic_noun_is_captured(brand_snapshot):
    plan = g.build_query_plan("알로루 브랜드 상품 구매한 고객에게 10% 할인 쿠폰을 뿌리고 싶어")
    assert plan["target_user"]["purchase_object"] == "알로&루"


def test_generic_only_purchase_is_not_product_filter(brand_snapshot):
    # 앞에 실제 상품/브랜드명이 없이 일반명사('상품')만 있으면 상품 필터로 쓰지 않는다.
    # LIKE '%상품%' 는 사실상 모든 상품을 뜻해 무의미하고, 계사형('브랜드가 상품인')이 이미
    # 일반명사를 제외하는 것과도 일관된다. ('2개 이상 상품 구입' 처럼 실상품 없는 개수 조건 보호)
    target_user: dict = {}
    g._apply_purchase_object_filter("상품 구매한 고객", target_user)
    assert target_user.get("purchase_object") is None


def test_plain_product_extraction_unchanged(brand_snapshot):
    target_user: dict = {}
    g._apply_purchase_object_filter("40대 여성 중 기저귀를 구매한 고객", target_user)
    assert target_user["purchase_object"] == "기저귀"


# --- 타겟팅/채널 절 분리: '곳에' 표지 ---

def test_scope_split_on_brand_place_marker():
    # "…인 곳에"는 '에게'가 아니라 '에'만 붙는 장소형 오디언스 표현 — 규칙 표지 '곳에'로 결정론 분리된다.
    scopes = g.split_prompt_scopes("브랜드가 알로루인 곳에 10% 할인 쿠폰을 뿌리고 싶어")
    assert scopes["mode"] == "rules"
    assert "알로루" in scopes["targeting"] and "쿠폰" not in scopes["targeting"]
    assert "쿠폰" in scopes["channel"]


def test_scope_split_ignores_locative_seo():
    # '곳에서'(장소 부사격)는 대상 지향 표지가 아니다 — 구매 조건이 채널 절로 새면 안 된다.
    scopes = g.split_prompt_scopes("브랜드가 알로루인 곳에서 구매한 고객")
    assert "구매한 고객" in scopes["targeting"]
    assert scopes["channel"] == ""


def test_scope_split_egeseo_not_marker():
    scopes = g.split_prompt_scopes("VIP 고객에게서 추천받은 사람")
    assert "추천받은 사람" in scopes["targeting"]


def test_scope_split_existing_marker_unchanged():
    scopes = g.split_prompt_scopes("20대 여성에게 쿠폰을 발송")
    assert scopes["mode"] == "rules"
    assert scopes["targeting"].endswith("에게")
    assert "쿠폰" in scopes["channel"]


def test_reattached_scopes_keep_channel_clause(brand_snapshot):
    # 타겟팅 스코프 파이프라인 재현: 타겟팅 절만으로 빌드된 plan 은 내부 재분리에서 채널 절이 비므로,
    # 파이프라인 분리 결과를 다시 붙여 응답 prompt_scopes 에 채널 절이 살아있게 한다(BFF 표시 조건).
    full = "브랜드가 알로루인 곳에 10% 할인 쿠폰을 뿌리고 싶어"
    scopes = g.split_prompt_scopes(full)
    plan = g.build_query_plan(scopes["targeting"])
    assert plan["retrieval"].get("channel_query") == ""  # 재분리 한계(전제 확인)
    g._attach_retrieval_scopes(plan, scopes)
    assert plan["retrieval"]["targeting_query"] == "브랜드가 알로루인 곳에"
    assert "쿠폰" in plan["retrieval"]["channel_query"]


# --- 재작성 검증 게이트: 브랜드 조건은 '문자열 존재'가 아니라 '의미'로 보존 판정 ---

def test_rewrite_guard_catches_brand_to_residence_hallucination():
    # LLM 재작성이 브랜드를 거주지로 변질시킨 실사례: '알로루' 문자열은 남지만 브랜드 조건이 사라졌다.
    dropped = g._rewrite_dropped_signals(
        "브랜드가 알로루인 곳에 10% 할인 쿠폰을 뿌리고 싶어",
        "브랜드가 알로루에 거주하는 고객에게 10% 할인 쿠폰을 제공하고자 합니다.",
    )
    assert any("브랜드 조건" in item and "알로루" in item for item in dropped)


def test_rewrite_guard_accepts_brand_phrasing_change():
    # 표현형만 바뀌고 브랜드 언급이 유지되면 통과("알로루 브랜드 구매 고객").
    dropped = g._rewrite_dropped_signals(
        "브랜드가 알로루인 곳에 10% 할인 쿠폰을 뿌리고 싶어",
        "알로루 브랜드 구매 고객에게 10% 할인 쿠폰을 제공하고자 합니다.",
    )
    assert not any("브랜드" in item for item in dropped)


def test_rewrite_guard_accepts_purchase_phrasing():
    # 브랜드 단어가 빠져도 같은 이름의 '구매 이력' 조건으로 남으면 의미 보존으로 본다.
    dropped = g._rewrite_dropped_signals(
        "브랜드가 알로루인 곳에 10% 할인 쿠폰을 뿌리고 싶어",
        "알로루 구매 고객에게 10% 할인 쿠폰을 제공하고자 합니다.",
    )
    assert not any("브랜드" in item for item in dropped)


def test_rewrite_guard_no_brand_no_false_positive():
    # 브랜드 언급이 없는 문장은 브랜드 게이트가 개입하지 않는다(기존 동작 유지).
    dropped = g._rewrite_dropped_signals(
        "40대 여성 중 기저귀를 구매한 고객",
        "40대 여성 기저귀 구매 고객",
    )
    assert dropped == []
