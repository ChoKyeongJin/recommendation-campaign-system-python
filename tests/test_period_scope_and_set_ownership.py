"""기간 스코프 전파 + 집합식 소유권(dimension filter 중복) 회귀 코퍼스.

배경(구조적 결함 3종):
  1. dimension_filters 로 이미 소비된 지역 OR('서울 또는 경기')이 set-expression 엔진의 일반
     operator-scan 폴백('또는')에서도 처리돼 '서울'을 정규화 사전에서 못 찾았다는 clarification 으로
     전체 SQL 을 막았다. → operator-scan 집합식이 '결정론 dimension/속성 필터로 완전 소유'된 경우
     리던던트로 버린다(진짜 집합연산 교집합/합집합/포함·제외 구조는 유지).
  2. 한 조건의 기간값('최근 30일 로그인')이 전역 first-match 로 뒤쪽 구매 집계조건까지 흘러
     window_days 를 오염시켰다. → 집계 창은 절별로 집계-도메인 앵커 근처에서만 추출한다.
  3. '과거 누적 구매금액'이 옆 절의 롤링 창을 물려받아 최근 N일로 잘렸다. → '누적/평생/과거 누적'
     표지 절은 lifetime(window_days=None)으로 고정하고, 옆 조건의 창을 상속하지 않는다.
  4. '최근 180일 구매건수 0건'이 plan 에서 조용히 사라졌다. → 창이 있는 무주문(0건/미구매)은
     purchase_inactivity(윈도우 anti-join)로 컴파일하고, 컴파일 불가(달력구간 등)면 경고로 고지한다.

실행: python -m pytest tests/test_period_scope_and_set_ownership.py -q
"""

import networkx as nx

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sql(query: str):
    plan = _plan(query)
    res = g.build_sql_result(
        graph=nx.Graph(), query=query, query_plan=plan, context_nodes=[],
        schema_path=g.DEFAULT_SCHEMA_PATH, default_limit=None, original_query=query,
    )
    return plan, res


def _sido_values(plan: dict) -> set[str]:
    out: set[str] = set()
    for f in plan.get("dimension_filters", []):
        if isinstance(f, dict) and str(f.get("column", "")).endswith("SIDO"):
            out.update(f.get("names") or [])
            out.update(f.get("codes") or [])
    return out


def _aggs_by_metric(plan: dict) -> dict[str, dict]:
    return {c["metric_id"]: c for c in plan["target_user"].get("aggregate_conditions") or []}


# ── 테스트 1: 지역 OR 는 집합식 clarification 을 발생시키지 않음 ────────────────────────────
def test_region_or_does_not_trigger_set_clarification():
    q = "서울 또는 경기 회원 중 여성이고 골드 또는 VIP인 회원"
    plan = _plan(q)
    sido = _sido_values(plan)
    assert "서울" in sido and "경기" in sido, f"지역이 dimension_filters 에 없음: {sido}"
    # 미해결 집합식 operand(clarification)가 남지 않아야 한다.
    assert all(not e.get("requires_clarification") for e in plan.get("set_expressions", [])), plan.get("set_expressions")
    _plan_, res = _sql(q)
    assert res["sql"] is not None, res.get("failure_reason")
    assert res.get("failure_reason") != "query_plan_required_conditions_missing"


# ── 테스트 2: 지역이 이미 처리된 경우 set-expression 중복 질문 금지 ──────────────────────────
def test_region_already_handled_no_duplicate_set_question():
    q = "서울 또는 경기 회원 중 최근 30일 로그인한 회원"
    _plan_, res = _sql(q)
    cqs = res.get("clarification_questions") or []
    assert not any(("서울" in c or "경기" in c) for c in cqs), cqs
    assert res.get("failure_reason") != "query_plan_required_conditions_missing"
    assert res["sql"] is not None


# ── 테스트 3: 로그인 기간이 구매 집계조건으로 전파되지 않음 ─────────────────────────────────
def test_login_window_not_propagated_to_purchase_aggregates():
    q = "최근 30일 로그인한 회원 중 최근 90일 구매횟수 3회 이상이고 최근 90일 구매금액 30만 원 이상인 회원"
    plan = _plan(q)
    tu = plan["target_user"]
    assert tu["recent_login"]["min_days"] == 30
    aggs = _aggs_by_metric(plan)
    assert aggs["order_count"]["window_days"] == 90, aggs["order_count"]
    assert aggs["purchase_amount"]["window_days"] == 90, aggs["purchase_amount"]


# ── 테스트 4: 누적(과거 누적) 구매금액은 전 기간(lifetime), 롤링 창 주입 금지 ────────────────
def test_cumulative_purchase_amount_is_lifetime():
    q = "최근 90일 로그인하지 않았고 과거 누적 구매금액이 100만 원 이상인 회원"
    plan = _plan(q)
    tu = plan["target_user"]
    assert tu["inactivity_period"]["min_days"] == 90
    amt = _aggs_by_metric(plan)["purchase_amount"]
    assert amt["window_days"] is None, amt
    _plan_, res = _sql(q)
    sql = res["sql"]
    assert sql is not None
    # 누적 집계는 주문일 롤링 창(ORDER_DATE >= ...)을 갖지 않는다(로그인 미활동 창은 LAST_LOGIN_DATE).
    assert "ORDER_DATE >=" not in sql, sql


# ── 테스트 5: 최근 N일 구매건수 0건 → 윈도우 anti-join 컴파일(조용한 드롭 금지) ──────────────
def test_recent_zero_purchase_count_compiles_to_windowed_antijoin():
    q = "최근 180일 구매건수가 0건인 회원"
    plan = _plan(q)
    pi = plan["target_user"].get("purchase_inactivity")
    assert isinstance(pi, dict) and pi["min_days"] == 180, plan["target_user"]
    _plan_, res = _sql(q)
    sql = res["sql"]
    assert sql is not None
    assert "NOT EXISTS" in sql and "DATEADD(DAY, -180" in sql, sql


def test_zero_purchase_count_phrasing_variants():
    for q in (
        "최근 180일 주문이 0건인 회원",
        "최근 180일 동안 구매하지 않은 회원",
        "최근 180일간 미구매 회원",
    ):
        pi = _plan(q)["target_user"].get("purchase_inactivity")
        assert isinstance(pi, dict) and pi["min_days"] == 180, (q, pi)


# ── 테스트 6: 복합 요청 전체 의미 보존 ──────────────────────────────────────────────────────
def test_composite_login_cumulative_and_zero_count_all_preserved():
    q = ("최근 90일 로그인하지 않았고 과거 누적 구매금액이 100만 원 이상이며 "
         "최근 180일 구매건수가 0건인 회원")
    plan = _plan(q)
    tu = plan["target_user"]
    assert tu["inactivity_period"]["min_days"] == 90
    assert _aggs_by_metric(plan)["purchase_amount"]["window_days"] is None
    assert tu["purchase_inactivity"]["min_days"] == 180
    _plan_, res = _sql(q)
    sql = res["sql"]
    assert sql is not None and res["is_success"] is True, res.get("failure_reason")
    assert "LAST_LOGIN_DATE" in sql
    assert "HAVING SUM(PAYMENT_AMT) >= 1000000" in sql
    assert "NOT EXISTS" in sql and "DATEADD(DAY, -180" in sql
    # 결정론 의미 불변식 게이트는 SQL 생성 시 항상 실행된다(LLM 유무 무관).
    assert res["semantic_invariants"]["ran"] is True


# ── 테스트 7: silent drop 방지 — 컴파일 불가한 필수 조건은 경고로 고지 ───────────────────────
def test_uncompilable_zero_count_is_not_silently_dropped():
    # 달력 구간(올해)의 0건 무주문은 롤링 창 anti-join 으로 컴파일할 수 없다 — 조용히 사라지지 말고
    # 경고(dropped_signal_warnings)로 고지해야 한다. 서울 필터로 SQL 자체는 생성된다.
    q = "서울에 거주하는 회원 중 올해 구매건수가 0건인 회원"
    _plan_, res = _sql(q)
    assert res["sql"] is not None
    warned = any(("구매" in w or "주문" in w) for w in (res.get("dropped_signal_warnings") or []))
    silent = res["is_success"] and not warned
    assert not silent, "미지원 0건(달력) 조건이 경고 없이 사라졌다(silent drop)"


# ── 테스트 8: 실제 집합식 동작 유지(교집합/세그먼트 OR/포함/제외) ───────────────────────────
def test_genuine_intersection_set_expression_preserved():
    # 진짜 집합연산(교집합, postfix 구조)은 dimension-consumed drop 대상이 아니다.
    assert len(_plan("VIP 고객과 휴면 고객의 교집합").get("set_expressions", [])) == 1


def test_operator_scan_over_real_segments_preserved():
    # operator-scan('또는') 이라도 피연산자가 진짜 세그먼트(관심사)면 결정론 dimension 소유가 아니므로 유지.
    assert len(_plan("축구 관심사 또는 야구 관심사 회원").get("set_expressions", [])) == 1


def test_genuine_refinement_and_difference_still_set_expressions():
    # 기존 회귀(test_set_expression_scope)와 동일 불변식 — dimension-consumed drop 이 이를 깨지 않는다.
    assert len(_plan("20대를 대상으로 하되 여성만 포함").get("set_expressions", [])) == 1
    assert len(_plan("VIP 고객에서 휴면 고객을 제외").get("set_expressions", [])) == 1
