"""장바구니 보관 기간(cart_retention) 타겟 회귀.

배경: "장바구니 상품을 일주일 이상 유지하고 있는 회원을 찾아줘"가 기간 조건 없이
`KEEP_YN='Y'` 만 걸린 SQL 로 나왔다("언제 담았든 아직 안 산 회원" = 조건이 통째로 사라짐).
원인 두 가지:
  (1) 담은 시점과 기준일을 비교하는 조건 자체가 파서·빌더에 없었다.
  (2) 재작성 LLM 이 '일주일 이상 유지'를 '장바구니 이탈 고객'으로 뭉뚱그렸는데, 재작성 가드는
      숫자 서명만 봐서 숫자 없는 단어형('일주일') 소실을 잡지 못했다.

고정 내용:
  - _apply_cart_retention_filter: 장바구니 어휘 + 보관 표현 + 기간(+방향어)이면 cart_retention 확정.
  - 카트 빌더가 <담은 시점> <= DATEADD(DAY, -N, GETDATE()) 술어를 건다(이내면 >=).
  - 재작성 가드 서명에 durations(일수 정규화) 추가 — '일주일'과 '7일'은 같은 7 이라 표기 변환은 통과.

2차 수정(실DB 검증 후): 술어는 나왔지만 여전히 필터가 안 됐다. 처음 쓴 ODS_MALL_OMS_CART.INS_DT 는
'담은 시점'이 아니라 ETL 적재 시각으로, 38,133행 전체가 단일 값(2020-02-03 14:23:14.850, distinct 1)
이었다. 그래서 7일 기준이든 10년 기준이든 전건 통과/전건 탈락만 갈리는 계단 함수라 실제로는 조건이
없는 것과 같았다(실측: KEEP_YN='Y' 만 걸어도 72명, INS_DT 7일 조건을 더해도 똑같이 72명).
행마다 실제 시점이 다른 컬럼은 UPD_DT(distinct 33,446) 뿐이라 그쪽으로 옮기고, 컬럼 자체는
member_target_filters.json 의 cart_targets.registered_date_column 이 소유하게 했다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_cart_retention_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def test_week_retention_parsed_and_compiled():
    plan = _plan("장바구니 상품을 일주일 이상 유지하고 있는 회원을 찾아줘.")
    assert plan["target_user"]["cart_retention"] == {"min_days": 7, "label": "장바구니 보관 7일 이상"}
    # 보관 = 미결제 상태이므로 카트 이탈 행동으로도 승격돼 intent 가 unknown 에 머물지 않는다.
    assert "cart_abandoner" in plan["target_user"]["behaviors"]
    assert plan["intent"] in ("find_user_segment", "recommend_campaign")
    sql = g.build_sql_template_candidate(plan)["sql"]
    assert "A.UPD_DT <= DATEADD(DAY, -7, GETDATE())" in sql
    assert "A.KEEP_YN = 'Y'" in sql


def test_rewritten_phrasing_still_detected():
    # 재작성 LLM 이 표현형을 바꿔도('유지하고 있는' → '담고 있는') 같은 조건으로 잡혀야 한다.
    # effective_query(재작성본)가 파싱 기준이라 표현형 하나에 의존하면 조건이 통째로 사라진다.
    plan = _plan("장바구니에 상품을 일주일 이상 담고 있는 회원을 찾아줘.")
    assert plan["target_user"]["cart_retention"]["min_days"] == 7
    assert "A.UPD_DT <= DATEADD(DAY, -7, GETDATE())" in g.build_sql_template_candidate(plan)["sql"]


def test_benefit_period_is_not_retention():
    # 혜택 기간('7일 이상 유효한 쿠폰')은 오디언스 조건이 아니라 발송물 속성이다.
    # (이 문장 자체는 장바구니 이탈 신호가 없어 SQL 후보가 안 나온다 — 여기선 기간 오탐만 본다.)
    plan = _plan("장바구니에 담은 고객에게 7일 이상 유효한 쿠폰 보내줘")
    assert plan["target_user"].get("cart_retention") is None
    plan = _plan("장바구니 이탈 고객에게 7일 이상 유효한 쿠폰 보내줘")
    assert plan["target_user"].get("cart_retention") is None
    assert "UPD_DT" not in g.build_sql_template_candidate(plan)["sql"]


def test_numeric_and_month_durations():
    assert _plan("장바구니에 담아둔 지 30일 이상 지난 회원")["target_user"]["cart_retention"]["min_days"] == 30
    assert _plan("장바구니에 3개월 넘게 방치된 상품이 있는 회원")["target_user"]["cart_retention"]["min_days"] == 90
    assert _plan("장바구니에 보름 이상 담아둔 회원")["target_user"]["cart_retention"]["min_days"] == 15


def test_within_window_uses_opposite_operator():
    plan = _plan("장바구니에 담은 지 3일 이내인 회원")
    assert plan["target_user"]["cart_retention"] == {"max_days": 3, "label": "장바구니 보관 3일 이내"}
    assert "A.UPD_DT >= DATEADD(DAY, -3, GETDATE())" in g.build_sql_template_candidate(plan)["sql"]


def test_member_attribute_combines_with_retention():
    plan = _plan("장바구니에 담아둔 지 30일 이상 지난 여성 회원")
    sql = g.build_sql_template_candidate(plan)["sql"]
    assert "A.UPD_DT <= DATEADD(DAY, -30, GETDATE())" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql


def test_count_threshold_and_retention_both_compile():
    # 개수 임계값이 함께 오면 집계 빌더가 개수·기간을 모두 컴파일한다(어느 쪽도 조용히 사라지지 않게).
    plan = _plan("장바구니에 3개 이상 담고 일주일 넘게 유지 중인 회원")
    candidate = g.build_sql_template_candidate(plan)
    sql = candidate["sql"]
    assert "COUNT(DISTINCT CART_PRODUCT_NO) >= 3" in sql
    assert "UPD_DT <= DATEADD(DAY, -7, GETDATE())" in sql
    assert candidate["dropped_condition_labels"] == []


def test_plain_cart_abandoner_has_no_retention_predicate():
    # 기간 표현이 없으면 예전 동작 그대로(회귀 방지) — 없는 조건을 만들어내지 않는다.
    plan = _plan("장바구니 이탈 고객")
    assert plan["target_user"].get("cart_retention") is None
    assert "UPD_DT" not in g.build_sql_template_candidate(plan)["sql"]


def test_non_cart_period_not_captured():
    # 장바구니 어휘가 없는 기간 표현('최근 7일 미구매')은 카트 보관 기간으로 오인하지 않는다.
    assert _plan("최근 7일 동안 구매하지 않은 고객")["target_user"].get("cart_retention") is None
    # 방향어 없는 기간은 의미가 모호해 잡지 않는다.
    assert _plan("장바구니 7일 이벤트 대상자")["target_user"].get("cart_retention") is None


def test_retention_column_is_registry_owned():
    # 어느 컬럼이 '담은 시점'인지는 데이터 사실이라 코드가 아니라 레지스트리가 갖는다.
    # (INS_DT 로 하드코딩됐던 탓에 적재 시각 단일 값으로 필터가 무력화됐다 — 상단 docstring 참고.)
    column = g._MEMBER_TARGET_FILTERS["cart_targets"]["registered_date_column"]
    assert column.split(".")[-1] == g._cart_retention_column()
    plan = _plan("장바구니에 일주일 이상 담아둔 회원")
    assert f"A.{g._cart_retention_column()} <= DATEADD(DAY, -7, GETDATE())" in g.build_sql_template_candidate(plan)["sql"]


# ── 최신성('최근 생성된') ──────────────────────────────────────────────────────
# 배경: "최근 생성된 장바구니가 있지만 주문으로 이어지지 않은 회원"에서 '최근 생성된'이 통째로 사라져
# SQL 에 날짜 조건이 하나도 없었다(재작성 가드도 숫자/기간어만 봐서 '최근'을 못 잡는다).
def test_recent_cart_without_number_uses_registry_default():
    plan = _plan("최근 생성된 장바구니가 있지만 주문으로 이어지지 않은 회원을 추출해줘.")
    retention = plan["target_user"]["cart_retention"]
    assert retention["max_days"] == g._cart_recent_default_days()
    # 어떤 창이 적용됐는지 라벨로 드러나야 한다(숫자를 조용히 지어내면 안 된다).
    assert str(retention["max_days"]) in retention["label"]
    assert f"<= DATEADD(DAY, -{retention['max_days']}" not in g.build_sql_template_candidate(plan)["sql"]
    assert f">= DATEADD(DAY, -{retention['max_days']}, GETDATE())" in g.build_sql_template_candidate(plan)["sql"]


def test_recent_default_is_registry_owned():
    assert g._MEMBER_TARGET_FILTERS["cart_targets"]["recent_default_days"] == g._cart_recent_default_days()


def test_recent_with_number_is_upper_bound():
    # '최근 N일'은 상한이다 — 방향어가 '동안'이어도 '최근'이 붙으면 이내로 본다.
    for phrase in ("최근 7일 이내에 담은 장바구니가 있는 회원", "최근 7일 동안 담은 장바구니가 있는 회원"):
        assert _plan(phrase)["target_user"]["cart_retention"] == {"max_days": 7, "label": "장바구니 보관 7일 이내"}


def test_recent_does_not_override_explicit_lower_bound():
    # '최근 3개월 이상 방치된'은 하한이다('이상'이 최신성보다 우선).
    assert _plan("장바구니에 최근 3개월 이상 방치된 상품이 있는 회원")["target_user"]["cart_retention"]["min_days"] == 90


def test_recent_requires_cart_event():
    # 장바구니와 무관한 '최근'이나, 담김 사건이 없는 문장은 잡지 않는다.
    assert _plan("최근 생성된 캠페인 목록")["target_user"].get("cart_retention") is None
    assert _plan("장바구니 이탈 고객")["target_user"].get("cart_retention") is None


# ── 재작성 가드: 기간 조건 소실 ────────────────────────────────────────────────
def test_rewrite_guard_catches_dropped_word_duration():
    dropped = g._rewrite_dropped_signals(
        "장바구니 상품을 일주일 이상 유지하고 있는 회원을 찾아줘.", "장바구니 이탈 고객"
    )
    assert dropped == ["기간 조건 '7일'"]


def test_rewrite_guard_allows_duration_notation_change():
    # '일주일' → '7일' 은 같은 7 로 정규화되므로 소실이 아니다(표기 정리는 재작성의 정상 동작).
    assert g._rewrite_dropped_signals(
        "장바구니에 일주일 이상 담아둔 회원", "장바구니에 7일 이상 담아둔 회원"
    ) == []
