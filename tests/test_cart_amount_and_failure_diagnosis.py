"""장바구니 금액 임계값 타겟 + 실패 원인 구분(인식했으나 미지원) 회귀.

배경: "장바구니에 10만원 이상 예상 금액이 있는 회원을 조회해줘"가 SQL 없이 실패하면서
"입력에서 타겟 조건을 찾지 못해 SQL을 만들지 못했습니다" 안내가 나왔다. 두 가지가 겹쳤다.
  (1) 장바구니 집계 지표에 금액이 없었다 — 개수(COUNT)/수량(SUM SET_QTY)만 있어서
      _CART_COUNT_PATTERN('N개')이 '10만원'을 못 잡고 cart_aggregate 가 안 세워졌다.
  (2) 그래서 어떤 빌더도 해당되지 않아 no_sql_candidates 로 떨어졌는데, 그 안내문이
      "조건을 찾지 못했다"고 말한다. 사실은 조건(장바구니+금액)을 인식했고 표현할 수단이 없었을
      뿐이라 오진이다. 게다가 지원 조건 목록에 '장바구니'와 '구매 금액'이 둘 다 있어서
      사용자는 이미 쓴 조건을 다시 쓰게 된다.

고정 내용:
  - cart_amount 지표 추가: SUM(TOTAL_SALE_PRICE) 로 컴파일(실컬럼, KEEP_YN='Y' 라인에 값 존재).
  - 금액은 장바구니 어휘 근처에 있을 때만 카트 금액으로 본다 — "구매 금액"은 기존 누적 집계 담당.
  - recognized_domains: 어휘로 인식한 도메인을 플랜에 남겨 실패를 (a)신호 없음 / (b)형태 미지원 으로 가른다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_cart_amount_and_failure_diagnosis.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


# ── (1) 장바구니 금액 임계값 ──────────────────────────────────────────────────
def test_cart_amount_threshold_parsed_and_compiled():
    plan = _plan("장바구니에 10만원 이상 예상 금액이 있는 회원을 조회해줘.")
    assert plan["target_user"]["cart_aggregate"] == {
        "metric": "cart_amount",
        "operator": ">=",
        "threshold": 100000.0,
    }
    sql = g.build_sql_template_candidate(plan)["sql"]
    assert "SUM(TOTAL_SALE_PRICE) >= 100000" in sql
    assert "KEEP_YN = 'Y'" in sql


def test_cart_amount_magnitudes_and_operators():
    assert _plan("장바구니 금액이 3만원 이하인 회원")["target_user"]["cart_aggregate"] == {
        "metric": "cart_amount", "operator": "<=", "threshold": 30000.0,
    }
    assert _plan("장바구니에 50000원 초과 담은 회원")["target_user"]["cart_aggregate"]["threshold"] == 50000.0


def test_purchase_amount_is_not_stolen_by_cart():
    # "구매 금액"은 누적 구매 집계(aggregate_conditions) 담당이다. 장바구니 어휘가 같은 문장에
    # 있다고 카트 금액으로 채가면 지표가 조용히 바뀐다.
    plan = _plan("장바구니에 담은 고객 중 구매 금액 10만원 이상인 회원")
    assert plan["target_user"].get("cart_aggregate") is None
    assert [c["metric_id"] for c in plan["target_user"]["aggregate_conditions"]] == ["purchase_amount"]


def test_cart_count_and_quantity_still_work():
    # 금액 분기 추가로 기존 개수/수량 경로가 깨지지 않아야 한다(회귀 방지).
    assert _plan("장바구니에 3개 이상 담은 회원")["target_user"]["cart_aggregate"]["metric"] == "cart_line_count"
    assert _plan("장바구니에 담은 총 수량이 5개 이상인 회원")["target_user"]["cart_aggregate"]["metric"] == "cart_quantity"
    sql = g.build_sql_template_candidate(_plan("장바구니에 3개 이상 담은 회원"))["sql"]
    assert "COUNT(DISTINCT CART_PRODUCT_NO) >= 3" in sql


def test_cart_amount_combines_with_member_attribute():
    plan = _plan("장바구니에 10만원 이상 담은 여성 회원")
    sql = g.build_sql_template_candidate(plan)["sql"]
    assert "SUM(TOTAL_SALE_PRICE) >= 100000" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql


# ── (2) 실패 원인 구분: 신호 없음 vs 형태 미지원 ────────────────────────────────
def test_recognized_domains_recorded_only_when_lexicon_present():
    assert _plan("장바구니에 담은 회원")["recognized_domains"] == ["cart"]
    assert _plan("서울에 거주하는 20대 여성")["recognized_domains"] == []


def test_failure_message_distinguishes_recognized_domain():
    # (b) 도메인은 인식했는데 그 형태가 미지원 → 지원 형태를 구체적으로 알려준다.
    recognized = _describe({"recognized_domains": ["cart"]}, "recognized_domain_unsupported")
    assert "장바구니" in recognized and "인식했지만" in recognized
    assert "타겟 조건을 찾지 못해" not in recognized

    # (a) 신호 자체가 없음 → 기존 안내가 맞다.
    generic = _describe({"recognized_domains": []}, "no_sql_candidates")
    assert "타겟 조건을 찾지 못해" in generic


def _describe(query_plan: dict, reason: str) -> str:
    return g._describe_sql_failure(query_plan, {"failure_reason": reason, "selected": None})


# ── (3) 동일 상품 복수 담기 + 수량 컬럼 ────────────────────────────────────────
# 배경: "장바구니에 동일 상품을 여러 개 담은 회원"이 조건 없이 KEEP_YN='Y' 만 걸린 SQL 로 나왔다
# (조용한 조건 소실). 또한 기존 cart_quantity 지표가 SET_QTY 를 쓰고 있었는데 schema_catalog 의
# human_note 상 SET_QTY 는 '세트 수량', QTY 가 '담은 수량'(important=true)이라 컬럼 자체가 틀렸다.
def test_same_product_multiple_uses_max_qty():
    plan = _plan("장바구니에 동일 상품을 여러 개 담은 회원을 찾아줘.")
    assert plan["target_user"]["cart_aggregate"] == {
        "metric": "cart_same_product_quantity", "operator": ">=", "threshold": 2,
    }
    # MAX(QTY) 여야 한다 — SUM 은 서로 다른 상품을 하나씩 담아도 커져서 '동일 상품'이 아니다.
    assert "MAX(QTY) >= 2" in g.build_sql_template_candidate(plan)["sql"]


def test_same_product_explicit_count():
    plan = _plan("장바구니에 같은 상품 3개 이상 담은 회원")
    assert plan["target_user"]["cart_aggregate"]["threshold"] == 3
    assert "MAX(QTY) >= 3" in g.build_sql_template_candidate(plan)["sql"]


def test_same_product_without_quantity_is_not_guessed():
    # 수량 표현이 없으면 임계값을 지어내지 않는다.
    assert _plan("장바구니에 동일 상품이 있는 회원")["target_user"].get("cart_aggregate") is None


def test_line_count_and_quantity_metrics_stay_separate():
    # '3개 이상 담은'(상품 종류 수)이 동일상품 지표로 새면 안 된다.
    assert _plan("장바구니에 3개 이상 담은 회원")["target_user"]["cart_aggregate"]["metric"] == "cart_line_count"
    # 총 수량은 SUM(QTY) — SET_QTY(세트 수량) 아님.
    sql = g.build_sql_template_candidate(_plan("장바구니에 담은 총 수량이 5개 이상인 회원"))["sql"]
    assert "SUM(QTY) >= 5" in sql and "SET_QTY" not in sql


# ── (4) cart_terms 게이트 오탐 ────────────────────────────────────────────────
# targeting_lexicon.json 의 cart_terms 는 장바구니 파서 3곳(집계·보관기간·도메인 인식)의 게이트다.
# 여기에 일반어를 넣으면 장바구니와 무관한 임계값이 전부 장바구니 집계로 빨려 들어간다. 실제로
# '보유'를 넣었을 때 "포인트를 보유한 회원 중 10만원 이상"이 SUM(TOTAL_SALE_PRICE)>=100000 으로,
# "쿠폰을 3개 이상 보유한"이 COUNT(DISTINCT CART_PRODUCT_NO)>=3 으로 컴파일됐다(경고도 없이).
def test_generic_possession_is_not_cart_context():
    plan = _plan("포인트를 보유한 회원 중 10만원 이상인 고객")
    assert plan["target_user"].get("cart_aggregate") is None
    assert plan["recognized_domains"] == []

    plan = _plan("쿠폰을 3개 이상 보유한 회원")
    assert plan["target_user"].get("cart_aggregate") is None


def test_cart_type_values_are_not_cart_context():
    # '정기배송'·'픽업'은 장바구니 문맥 표지가 아니라 cart_type 값이다(cart_types 가 소유).
    assert _plan("정기배송 신청한 회원")["recognized_domains"] == []
    assert _plan("픽업 이용 회원 조회")["recognized_domains"] == []


def test_cart_lexicon_has_no_overly_generic_terms():
    # 게이트 어휘에 일반어가 다시 들어오면 위 오탐이 통째로 재발한다 — 사전 쪽에서 못 박는다.
    assert not {"보유", "정기배송", "픽업", "픽업상품", "일반상품"} & set(g._lexicon_terms("cart_terms"))
    assert "장바구니" in g._lexicon_terms("cart_terms")
