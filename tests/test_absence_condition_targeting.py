"""장바구니 '부재'(cart_absence) 조건 회귀 (Stage D — 부재형 조건 신규 지원).

배경: "장바구니 없는 / 장바구니 생성이나 구매 이력 없는 회원"에서 '장바구니 없는'을 표현할 술어가 없어
조건이 소실되거나, 오히려 cart_abandoner(장바구니에 상품이 '있는')로 뒤집혀 정반대 SQL 이 나왔다.
이제 cart_absence 가 회원키 NOT EXISTS(보관 카트 없음)로 컴파일된다(캠페인 반응 EXISTS 와 같은 인라인
술어라 어느 빌더에나 AND 결합, 전용 빌더 불필요). 구매 부재는 기존 no_purchase 트랙이 잡는다.
'장바구니 없음'은 '카트 있음' 조건(cart_abandoner/retention/type)과 모순이라 같은 절의 오파싱을 걷어낸다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_absence_condition_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


# ── 파싱: '장바구니 없는'이 cart_absence 로 승격되고 cart_abandoner 로 뒤집히지 않는다 ─────
def test_cart_absence_parsed():
    tu = _plan("장바구니 없는 회원")["target_user"]
    assert tu.get("cart_absence") is True
    assert "cart_abandoner" not in tu.get("behaviors", [])


def test_cart_presence_not_absence():
    # 존재 표현은 cart_absence 가 아니라 cart_abandoner(있음) 트랙 유지.
    tu = _plan("장바구니에 상품이 있는 회원")["target_user"]
    assert tu.get("cart_absence") is None
    assert "cart_abandoner" in tu.get("behaviors", [])


def test_cart_absence_clears_contradictory_cart_signals():
    # '장바구니 없음'은 카트 보관/유형/이탈과 모순 — 같은 절 오파싱을 걷어낸다.
    tu = _plan("최근 로그인 했지만 장바구니 생성이나 구매 이력 없는 회원")["target_user"]
    assert tu.get("cart_absence") is True
    assert tu.get("cart_retention") is None
    assert tu.get("cart_type") is None
    assert tu.get("purchase_object") is None
    # OR-스코프 부정: 구매 부재는 no_purchase 로 함께 잡힌다.
    assert "no_purchase" in tu.get("behaviors", [])


# ── 컴파일: 회원키 NOT EXISTS(보관 카트 없음) ──────────────────────────────────────────
def test_cart_absence_compiles_not_exists():
    compiled = g.compile_member_target_conditions(_plan("장바구니 없는 회원"))
    assert any(
        p.startswith("NOT EXISTS") and "ODS_MALL_OMS_CART" in p and "A.CART_ID = B.MEMBER_ID" in p
        for p in compiled["predicates"]
    )
    assert compiled["has_signal"] is True


# ── SQL: 부재 조합이 두 NOT EXISTS(카트+주문)로, 모순 없이 나온다 ────────────────────────
def test_cart_and_purchase_absence_sql():
    plan = _plan("장바구니 생성이나 구매 이력 없는 회원")
    cand = g.build_sql_template_candidate(plan)
    assert cand is not None
    sql = cand["sql"]
    assert "NOT EXISTS (SELECT 1 FROM ODS_MALL_OMS_CART A" in sql
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O" in sql
    # 카트 보관(KEEP_YN='Y') 빌더가 이겨 NOT EXISTS 와 자기모순 나면 안 된다(존재+부재 공존 금지).
    assert "FROM ODS_MALL_OMS_CART A\n" not in sql  # 카트 라인이 메인 FROM 이 아니다


def test_cart_absence_combines_with_member_attribute():
    sql = g.build_sql_template_candidate(_plan("장바구니 없는 VIP 여성"))["sql"]
    assert "NOT EXISTS (SELECT 1 FROM ODS_MALL_OMS_CART A" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'" in sql


# ── 원 실패 프롬프트: 6개 조건이 모두 실린다 ────────────────────────────────────────────
def test_full_failing_prompt_all_conditions():
    q = ("최근 1년 이내 가입 자녀 정보 등록 온라인 가입 회원 중 최근 로그인 했지만 "
         "장바구니 생성이나 구매 이력 없는 회원")
    plan = _plan(q)
    tu = plan["target_user"]
    assert tu.get("signup_target") == {"days": 365}
    assert tu.get("recent_login", {}).get("min_days") == 30
    assert "online_signup" in tu.get("lifecycle", [])
    assert "children_registered" in tu.get("lifecycle", [])
    assert tu.get("cart_absence") is True
    assert "no_purchase" in tu.get("behaviors", [])

    sql = g.build_sql_template_candidate(plan)["sql"]
    assert "B.REG_OFFSHOP_ID = 'O'" in sql
    assert "B.CHILDREN_YN = 'Y'" in sql
    assert "B.LAST_LOGIN_DATE >= CONVERT(CHAR(8), DATEADD(DAY, -30, GETDATE()), 112)" in sql
    assert "B.REG_DT >= CONVERT(CHAR(8), DATEADD(DAY, -365" in sql
    assert "NOT EXISTS (SELECT 1 FROM ODS_MALL_OMS_CART A" in sql
    assert "NOT EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O" in sql


# ── IR: cart_absence 는 fact_join 아님(인라인 술어, 전용 빌더 불필요) ────────────────────
def test_cart_absence_not_fact_join():
    import targeting_ir as ir
    spec = next(s for s in ir.CONDITION_SPECS if s.kind == "cart_absence")
    assert spec.fact_join is False and spec.signals_target is True
