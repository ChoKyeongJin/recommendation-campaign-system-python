"""장바구니 이탈(cart_abandoner) 타겟 회귀.

배경: "장바구니에 담고 아직 구매 안 한 회원을 찾아줘"가 SQL 을 못 만들었다. 원인 두 가지:
  (1) _has_member_target_signal 이 cart_abandoner 를 타겟 신호로 인정 안 해 intent=unknown 에 머묾
      → 빌더가 아예 호출되지 않음(no_sql_candidates).
  (2) 장바구니 템플릿(_build_cart_targets_candidate)이 recommend_campaign 분기에만 있고
      find_user_segment(세그먼트 조회) 분기엔 없었다.

고정 내용:
  - cart_abandoner 를 _has_member_target_signal 에 추가 → intent 승격(find_user_segment).
  - 장바구니 빌더를 헬퍼로 추출해 recommend_campaign·find_user_segment 양쪽에서 호출.
  - 실추출: ODS_MALL_OMS_CART A JOIN CRM_MB_BASEINFO B, KEEP_YN='Y'(미결제 보관 = 카트 이탈).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_cart_abandoner_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def test_cart_abandoner_detected_and_promotes_intent():
    plan = _plan("장바구니에 상품을 담아두고 아직 구매하지 않은 회원을 찾아줘.")
    assert "cart_abandoner" in plan["target_user"]["behaviors"]
    # 실추출 신호로 인정돼 intent 가 unknown 에서 승격돼야 한다.
    assert plan["intent"] in ("find_user_segment", "recommend_campaign")
    assert g._has_member_target_signal(plan) is True


def test_cart_abandoner_segment_lookup_builds_sql():
    plan = _plan("장바구니에 상품을 담아두고 아직 구매하지 않은 회원을 찾아줘.")
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None
    sql = cand["sql"]
    assert "FROM ODS_MALL_OMS_CART A" in sql
    assert "CRM_MB_BASEINFO B" in sql
    assert "A.KEEP_YN = 'Y'" in sql
    assert "'cart_abandoner' AS target_segment" in sql


def test_cart_abandoner_short_phrasing():
    cand = g.build_sql_template_candidate(_plan("장바구니 이탈 고객"))
    assert cand is not None
    assert "FROM ODS_MALL_OMS_CART A" in cand["sql"]


def test_cart_abandoner_campaign_still_builds_sql():
    # 캠페인 발송(recommend_campaign) 경로도 그대로 동작(회귀 방지).
    plan = _plan("장바구니 이탈 고객에게 재구매 유도 쿠폰 보내줘")
    assert plan["intent"] == "recommend_campaign"
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None
    assert "FROM ODS_MALL_OMS_CART A" in cand["sql"]


def test_cart_abandoner_with_member_attribute_combines():
    # 회원 속성(성별)이 함께 오면 같은 SQL 에 B 술어로 AND 결합한다.
    plan = _plan("장바구니에 담고 안 산 여성 회원 찾아줘")
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None
    assert "FROM ODS_MALL_OMS_CART A" in cand["sql"]
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in cand["sql"]


# ── '장바구니 보유' 표현 + '최근 N일 미구매'가 카트 SQL 에 함께 결합된다 ────────────────────
def test_cart_hold_phrasing_detected():
    # '장바구니를 보유하고'(조사 '를' + '보유' 동사)도 카트 존재로 잡힌다 — '장바구니에 담긴'만 알던 버그.
    assert "cart_abandoner" in _plan("장바구니를 보유한 회원")["target_user"]["behaviors"]


def test_recent_inactivity_particle_between_purchase_and_negation():
    # '구매가 없는'(조사 '가' 삽입)도 최근 N일 미구매로 잡힌다 — '구매없'만 알던 버그.
    tu = _plan("최근 90일 이내 구매가 없는 회원")["target_user"]
    assert isinstance(tu.get("purchase_inactivity"), dict) and tu["purchase_inactivity"]["min_days"] == 90


def test_cart_hold_and_recent_inactivity_combine_in_one_sql():
    # 이번 사례: 'GOLD 이상 + 장바구니 보유 + 최근 90일 미구매'가 한 SQL 에 전부 결합돼야 한다.
    # 카트 빌더가 이기더라도 미구매 NOT EXISTS 가 조용히 누락되면 안 된다.
    plan = _plan("GOLD 이상 회원 중 장바구니를 보유하고 최근 90일 이내 구매가 없는 회원을 추출해줘.")
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None
    sql = cand["sql"]
    assert "FROM ODS_MALL_OMS_CART A" in sql and "A.KEEP_YN = 'Y'" in sql
    assert "B.EMART_GRADE_CD IN ('MEM_GRADE_CD.GOLD', 'MEM_GRADE_CD.VIP')" in sql
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O" in sql and "DATEADD(DAY, -90, GETDATE())" in sql
    assert not cand.get("dropped_condition_labels")


# ── 장바구니 개수/수량 임계값("N개 이상 담은") ──────────────────────────────────────────
def test_cart_line_count_threshold_builds_sql():
    plan = _plan("장바구니에 3개 이상 상품을 담은 회원만 조회해줘.")
    assert plan["target_user"]["cart_aggregate"] == {"metric": "cart_line_count", "operator": ">=", "threshold": 3}
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None
    assert "COUNT(DISTINCT CART_PRODUCT_NO) >= 3" in cand["sql"]
    assert "FROM ODS_MALL_OMS_CART" in cand["sql"]


def test_cart_quantity_threshold_uses_sum_and_combines_member_attr():
    plan = _plan("장바구니에 담은 상품 수량이 5개 이상인 여성 회원")
    assert plan["target_user"]["cart_aggregate"]["metric"] == "cart_quantity"
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None
    # 수량은 QTY('담은 수량')다. SET_QTY 는 '세트 수량'이라 담은 개수와 무관하다(schema_catalog human_note).
    assert "SUM(QTY) >= 5" in cand["sql"]
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in cand["sql"]


def test_cart_count_operator_variants():
    for phrase, op in [("이하", "<="), ("초과", ">"), ("미만", "<")]:
        plan = _plan(f"장바구니에 2개 {phrase} 담은 회원")
        assert plan["target_user"]["cart_aggregate"]["operator"] == op, phrase


def test_count_threshold_without_cart_context_not_cart_aggregate():
    # 장바구니 어휘가 없으면 일반 개수 표현('3개 이상 구매')을 카트 집계로 오인하지 않는다.
    plan = _plan("3개 이상 구매한 고객")
    assert "cart_aggregate" not in plan["target_user"]


def test_behavior_terms_removed_from_lifecycle():
    # LLM(auto)이 cart_abandoner/repeat_buyer 를 lifecycle 로도 분류하면(lifecycle_extra_terms 겸용 어휘),
    # compile 이 lifecycle 항목을 '미지원 제외 조건'으로 처리해 신뢰도 리포트에 '생애주기: cart_abandoner'
    # (저점수)·'레지스트리/스키마 미확인' 경고가 뜬다. behavior 용어는 lifecycle 에서 걷어내야 한다(신호는
    # behaviors/objective 가 소유). _dedupe_lifecycle_against_behaviors 가 이를 결정론으로 보정한다.
    plan = {"target_user": {"lifecycle": ["repeat_buyer", "cart_abandoner", "vip"], "behaviors": ["cart_abandoner"]}}
    g._dedupe_lifecycle_against_behaviors(plan)
    assert plan["target_user"]["lifecycle"] == ["vip"]  # behavior 용어만 제거, 실제 lifecycle(vip)은 보존
    assert plan["target_user"]["behaviors"] == ["cart_abandoner"]  # 신호 보존


def test_lifecycle_without_behavior_terms_untouched():
    plan = {"target_user": {"lifecycle": ["dormant", "vip"], "behaviors": []}}
    g._dedupe_lifecycle_against_behaviors(plan)
    assert plan["target_user"]["lifecycle"] == ["dormant", "vip"]
