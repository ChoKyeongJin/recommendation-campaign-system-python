"""공통 semantic requirement 계층 회귀 — source requirement 기록 + base×qualifier capability 회계.

배경: '장바구니 조건 + 브랜드명 CJ제일제당'처럼 base 조건에 붙은 qualifier(브랜드/상품/카테고리)가 그 base
빌더에서 컴파일 못 돼 조용히 사라진 채 '성공'으로 나갔다. 브랜드 전용 감지기 대신, 모든 자연어 조건을
source requirement 로 먼저 기록하고 JSON capability(base→qualifier→supported/message/compiler_strategy)로
각 requirement 가 compiled/unsupported/clarification 중 하나로 귀결됐는지 회계한다. '특정 값 인식 성공'이
아니라 '모든 requirement 가 귀결됐는가'가 검증 기준이다.

실행: python -m pytest tests/test_semantic_requirements.py -q
"""

import json
from pathlib import Path

import graph_rag as g
import semantic_requirements as sr


_REG = sr.RequirementRegistry.load()


# ── 추출: 원문 → source requirement(base + qualifier + source_span) ─────────────────────────
def test_extract_entity_qualifier_records_requirement():
    reqs = sr.extract_entity_qualifier_requirements(
        "브랜드명이 CJ제일제당을 담은 고객", {"type": "behavior", "name": "cart_retention"})
    assert len(reqs) == 1
    r = reqs[0]
    assert r.type == "qualified_condition"
    assert r.base == {"type": "behavior", "name": "cart_retention"}
    assert r.qualifiers[0].type == "entity" and r.qualifiers[0].domain == "brand"
    assert r.qualifiers[0].raw_value == "CJ제일제당"  # 조사(을) 제거
    assert r.source_span["end"] > r.source_span["start"]  # source_span 기록
    assert r.status == "detected"  # 회계 전 초기 상태


def test_general_noun_phrase_not_captured():
    # 조사 표지가 없는 일반 명사구('상품 구매한')는 값으로 오포착하지 않는다.
    assert sr.extract_entity_qualifier_requirements("상품 구매한 고객", {"type": "behavior", "name": "purchase"}) == []


def test_product_and_category_markers():
    reqs = sr.extract_entity_qualifier_requirements(
        "상품명이 촉촉수분크림이고 카테고리가 생활용품인 회원", {"type": "behavior", "name": "purchase"})
    domains = {r.qualifiers[0].domain for r in reqs}
    assert domains == {"product", "category"}


# ── 회계: base×qualifier capability + SQL 반영 여부로 귀결 ──────────────────────────────────
def test_unsupported_combination_yields_unsupported_with_message():
    # 장바구니+상품은 여전히 미지원(브랜드는 하이브리드로 지원 전환됨). 미지원 조합은 unsupported+안내.
    acc = sr.account_requirements(
        "장바구니 유지 고객중 상품명이 촉촉수분크림을 담은 고객", "cart_retention", "behavior",
        "SELECT 1 FROM ODS_MALL_OMS_CART A WHERE A.KEEP_YN='Y'", _REG)
    r = acc.requirements[0]
    assert r.status == "unsupported"
    assert "상품" in r.message
    assert acc.blocking()  # 차단 대상
    assert acc.unresolved() == []  # detected 로 남지 않음(귀결됨)


def test_cart_brand_supported_and_compiled_via_evidence():
    # 하이브리드: 장바구니+브랜드는 이제 지원. dimension 코드 치환이면 원문 값이 SQL 문자열엔 없어도(코드 'A')
    # 빌더가 낸 구조화 evidence 로 반영이 확인돼 compiled(사일런트 clarification 오탐 없음).
    acc = sr.account_requirements(
        "장바구니 유지 고객중 브랜드명이 CJ제일제당을 담은 고객", "cart_retention", "behavior",
        "SELECT DISTINCT B.MEMBER_NO FROM ODS_MALL_OMS_CART A INNER JOIN CRM_CM_PRODUCT C "
        "ON A.PRODUCT_ID=C.PRODUCT_ID WHERE A.KEEP_YN='Y' AND C.BRAND_ID IN ('A')", _REG,
        applied_requirements=[{"base": "cart_retention", "qualifier": "brand", "values": ["A"], "resolved_via": "code"}])
    r = acc.requirements[0]
    assert r.status == "compiled", r.message
    assert acc.blocking() == []


def test_cart_brand_name_path_compiled_via_sql_literal():
    # 코드가 없어 BRAND_NAME LIKE 폴백으로 컴파일된 경우, 원문 값이 SQL 에 있어 evidence 없이도 compiled.
    acc = sr.account_requirements(
        "장바구니 유지 고객중 브랜드명이 알로루를 담은 고객", "cart_retention", "behavior",
        "SELECT 1 FROM ODS_MALL_OMS_CART A INNER JOIN CRM_CM_PRODUCT C ON A.PRODUCT_ID=C.PRODUCT_ID "
        "WHERE A.KEEP_YN='Y' AND (C.BRAND_NAME LIKE N'%알로루%')", _REG)
    assert acc.requirements[0].status == "compiled"
    assert acc.blocking() == []


def test_supported_and_compiled_when_value_in_sql():
    acc = sr.account_requirements(
        "브랜드명이 알로루인 상품을 구매한 회원", "purchase", "behavior",
        "SELECT 1 FROM P WHERE P.BRAND_NAME LIKE N'%알로루%'", _REG)
    r = acc.requirements[0]
    assert r.status == "compiled" and r.qualifiers[0].resolved_value == "알로루"
    assert acc.blocking() == []  # 정상 반영 → 차단 없음


def test_canonicalized_brand_counts_as_compiled():
    # 시스템이 '알로루'를 실DB 브랜드명 '알로&루'로 canonical 보정해 SQL 에 넣는다. 특수문자가 달라도
    # 정규화 비교로 '반영됨(compiled)'으로 봐야 한다(정상 컴파일을 누락으로 오탐하면 안 됨).
    acc = sr.account_requirements(
        "브랜드명이 알로루인 상품을 구매한 회원", "purchase", "behavior",
        "SELECT 1 FROM P WHERE P.BRAND_NAME LIKE N'%알로&루%'", _REG)
    r = acc.requirements[0]
    assert r.status == "compiled", r.message
    assert acc.blocking() == []


def test_supported_but_missing_yields_clarification():
    # 지원되는데(purchase+brand) 값이 SQL 에 없으면 사일런트 드롭 → clarification 으로 승격.
    acc = sr.account_requirements(
        "브랜드명이 알로루인 상품을 구매한 회원", "purchase", "behavior",
        "SELECT 1 FROM ORDERS WHERE 1=1", _REG)
    r = acc.requirements[0]
    assert r.status == "clarification"
    assert "알로루" in r.message
    assert acc.blocking()


def test_all_requirements_reach_terminal_status():
    # 검증기 계약: 회계 후 어떤 requirement 도 'detected'(미귀결)로 남지 않는다.
    for q, base, sql in [
        ("브랜드명이 CJ제일제당 담은", "cart_retention", "... KEEP_YN='Y'"),
        ("브랜드명이 알로루 구매", "purchase", "... LIKE '%알로루%'"),
        ("상품명이 촉촉수분크림 구매", "purchase", "SELECT 1"),
    ]:
        acc = sr.account_requirements(q, base, "behavior", sql, _REG)
        assert all(r.status in sr.TERMINAL_STATUSES for r in acc.requirements), (q, [r.status for r in acc.requirements])


# ── capability 는 JSON 이 제어(코드 무관) ─────────────────────────────────────────────────
def test_capability_lookup_falls_back_to_default():
    # 레지스트리에 없는 base 라도 _default 로 브랜드 미지원 판정.
    cap = _REG.qualifier_capability("some_unknown_base", "brand")
    assert cap is not None and cap["supported"] is False


def test_capability_driven_by_json(tmp_path):
    caps = json.loads(Path("docs/data/requirement_capabilities.json").read_text(encoding="utf-8"))
    caps["capabilities"]["cart_retention"]["qualifiers"]["brand"]["supported"] = True
    caps["capabilities"]["cart_retention"]["qualifiers"]["brand"]["compiler_strategy"] = "x"
    caps["capabilities"]["cart_retention"]["qualifiers"]["brand"].pop("message", None)
    p = tmp_path / "req.json"
    p.write_text(json.dumps(caps, ensure_ascii=False), encoding="utf-8")
    reg2 = sr.RequirementRegistry.load(p)
    # JSON 만 바꿔도 장바구니+브랜드가 이제 '지원'으로 판정됨(코드 변경 없이).
    cap = reg2.qualifier_capability("cart_retention", "brand")
    assert cap["supported"] is True


# ── 로더 검증(미지원인데 message 없으면 거부) ─────────────────────────────────────────────
def test_loader_rejects_unsupported_without_message(tmp_path):
    caps = json.loads(Path("docs/data/requirement_capabilities.json").read_text(encoding="utf-8"))
    caps["capabilities"]["cart_retention"]["qualifiers"]["product"].pop("message")
    p = tmp_path / "req.json"
    p.write_text(json.dumps(caps, ensure_ascii=False), encoding="utf-8")
    try:
        sr.RequirementRegistry.load(p)
        assert False, "미지원인데 message 없음 → 에러 기대"
    except sr.RequirementCapabilityError as exc:
        assert "message" in str(exc)


def test_real_registry_loads_clean():
    reg = sr.RequirementRegistry.load()
    assert "cart_retention" in reg.capabilities and "purchase" in reg.capabilities


# ── base 추론(graph_rag) ───────────────────────────────────────────────────────────────────
def test_base_inference_cart_from_sql():
    base, _ = g._infer_requirement_base({}, "SELECT 1 FROM ODS_MALL_OMS_CART A WHERE A.KEEP_YN='Y'")
    assert base == "cart_retention"


def test_base_inference_purchase_default():
    base, _ = g._infer_requirement_base({"target_user": {"purchase_object": {"brand": "알로루"}}}, "SELECT 1 FROM ORDERS")
    assert base == "purchase"
