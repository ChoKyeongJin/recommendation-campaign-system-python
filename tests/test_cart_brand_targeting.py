"""장바구니 × 브랜드 타겟팅 회귀 — 하이브리드(BRAND_ID 코드 우선, BRAND_NAME LIKE 폴백).

배경: "장바구니 유지 고객중 브랜드가 CJ제일제당을 담은 고객"이 capability 정책상 unsupported 로 조용히
막혔다(장바구니 브랜드 경로가 코드엔 있었지만 정책과 불일치). 이제 cart_retention×brand 를 하이브리드로
지원한다: dimension 코드가 해석되면 C.BRAND_ID IN(코드)(기존 경로 보존), 아니면 CART→CRM_CM_PRODUCT
조인 + BRAND_NAME LIKE(구매 경로와 동일 표현). 검증은 구조화 evidence 우선이라 코드 치환에도 안 깨진다.

실행: python -m pytest tests/test_cart_brand_targeting.py -q
"""

import graph_rag as g


def _cart_brand_candidate(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan, g.build_sql_template_candidate(plan)


# ── BRAND_NAME 폴백 경로(코드 미해석 시) ────────────────────────────────────────────────────
def test_cart_brand_name_path_joins_product_and_filters_brand_name():
    plan, cand = _cart_brand_candidate("장바구니 이탈 고객중 브랜드가 알로루인 상품을 담은 고객")
    sql = cand["sql"]
    # CART→CRM_CM_PRODUCT 조인은 라인 PK 가 아니라 PRODUCT_ID 로 건다.
    assert "INNER JOIN CRM_CM_PRODUCT C ON A.PRODUCT_ID = C.PRODUCT_ID" in sql
    assert "(C.BRAND_NAME LIKE N'%알로루%')" in sql
    # 회원 단위 DISTINCT 로 one-to-many 조인 증폭을 흡수한다.
    assert sql.startswith("SELECT DISTINCT")
    # 장바구니 라인 PK 를 상품 조인 키로 오용하지 않는다.
    assert "CART_PRODUCT_NO = C.PRODUCT_ID" not in sql
    assert "C.PRODUCT_ID = A.CART_PRODUCT_NO" not in sql


def test_cart_brand_emits_structured_evidence():
    plan, cand = _cart_brand_candidate("장바구니 이탈 고객중 브랜드가 알로루인 상품을 담은 고객")
    ev = cand.get("applied_requirements")
    assert ev and ev[0]["qualifier"] == "brand"
    assert ev[0]["join_path"] == "cart_to_product"
    assert ev[0]["filter_field"] == "BRAND_NAME"
    assert ev[0]["resolved_via"] == "name"


def test_cart_brand_no_longer_blocked_by_gate():
    plan, cand = _cart_brand_candidate("장바구니 이탈 고객중 브랜드가 CJ제일제당인 상품을 담은 고객에게 쿠폰")
    sql = cand["sql"]
    base, _ = g._infer_requirement_base(plan, sql)
    assert base == "cart_retention"
    acc = g._account_source_requirements("장바구니 이탈 고객중 브랜드가 CJ제일제당인 상품을 담은 고객에게 쿠폰", plan, sql, cand)
    assert acc is not None
    statuses = {r.qualifiers[0].domain: r.status for r in acc.requirements}
    assert statuses.get("brand") == "compiled"
    assert acc.blocking() == []  # 더 이상 unsupported 로 막지 않는다


# ── 비브랜드 장바구니 쿼리는 예전 그대로(회귀 방지) ─────────────────────────────────────────
def test_non_brand_cart_query_has_no_product_join():
    plan, cand = _cart_brand_candidate("장바구니에 담아둔 지 30일 이상 지난 여성 회원")
    sql = cand["sql"]
    assert "CRM_CM_PRODUCT" not in sql  # 브랜드가 없으면 상품 조인을 붙이지 않는다
    assert cand.get("applied_requirements") is None
    assert "A.UPD_DT <= DATEADD(DAY, -30, GETDATE())" in sql


def test_plain_cart_abandoner_unchanged():
    plan, cand = _cart_brand_candidate("장바구니 이탈 고객")
    assert "CRM_CM_PRODUCT" not in cand["sql"]
    assert cand.get("applied_requirements") is None
