"""쿠폰 도메인 세그먼트 파서 회귀 코퍼스 — JSON 스펙 기반 지표/연산자/capability 게이트.

배경(수정 전 결함):
  1. 쿠폰 사용 건수 미지원 게이트가 어순에 취약 — '쿠폰 사용 횟수가 5회를 초과'·'1회에서 3회 사이'가
     게이트를 통과해 임계값이 조용히 USE_CPN_CNT>0(쿠폰 쓴 사람 전부)으로 축소됐다.
  2. '쿠폰 한 개당 구매금액'의 분모/비율 의미가 사라지고 SUM(PAYMENT_AMT)>=N 으로 축소됐다.
  3. '쿠폰 수보다 구매건수가 많은'(지표 비교)이 TOP N 순위로 오해됐다.
  4. '쿠폰을 한 번도 사용하지 않은'(부정)이 후보 없음(빈 결과)으로 끝났다.
  5. 순위/복합 요청의 미지원 안내가 실제 원인(쿠폰 지표)과 무관했다.

수정: 쿠폰 의미(지표·연산자·값·범위·부정·비교대상·파생식)를 docs/data/segment_metrics.json +
segment_operators.json 스펙으로 분리하고, segment_semantics 가 어순 독립적으로 의미 노드(IR)를 완성한 뒤
capability 게이트로 지원/미지원을 판정한다. 같은 의미는 어순과 무관하게 같은 IR·같은 판정을 낸다.

실행: python -m pytest tests/test_coupon_semantics.py -q
"""

import json
from pathlib import Path

import networkx as nx

import graph_rag as g
import segment_semantics as ss


_REG = ss.SegmentSemanticsRegistry.load()


def _interpret(query):
    it = ss.interpret(query, _REG)
    assert it is not None, f"{query!r}: 쿠폰 의미 미인식"
    return it


def _plan(query):
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _reason(query):
    return (_plan(query).get("unsupported") or {}).get("reason")


def _sql_or_none(query):
    return g.build_sql_template_candidate(_plan(query))


def _sql(query):
    cand = _sql_or_none(query)
    assert cand is not None, f"{query!r}: SQL 후보 없음"
    return cand["sql"]


# ── 테스트 1: 쿠폰 건수 임계값 어순 변형 — 같은 IR, 같은 미지원 ────────────────────────────────
COUPON_GT5_VARIANTS = [
    "쿠폰을 5회 초과 사용한 회원",
    "쿠폰 사용 횟수 5회 초과 회원",
    "쿠폰 사용 횟수가 5회를 초과한 회원",
    "5회보다 많이 쿠폰을 사용한 회원",
    "사용한 쿠폰 수가 5건을 넘는 회원",
]


def test_coupon_count_threshold_order_invariant_ir():
    for q in COUPON_GT5_VARIANTS:
        cond = _interpret(q).condition
        assert cond.type == "metric_filter", q
        assert cond.metric == "coupon_usage_count", q
        assert cond.operator == "gt", q
        assert cond.value == 5.0, q


def test_coupon_count_threshold_order_invariant_compiles():
    # 어순이 달라도 같은 회원별 SUM(USE_CPN_CNT) HAVING 집계로 컴파일된다.
    for q in COUPON_GT5_VARIANTS:
        assert "SUM(COALESCE(R.USE_CPN_CNT, 0)) > 5" in _sql(q), q


def test_coupon_count_threshold_not_reduced_to_existence():
    # 핵심 회귀: 임계값이 조용히 USE_CPN_CNT>0(사용 여부 EXISTS)으로 축소되면 안 된다 — SUM 집계로 보존.
    for q in COUPON_GT5_VARIANTS:
        sql = _sql(q)
        assert "SUM(COALESCE(R.USE_CPN_CNT, 0)) > 5" in sql, q
        assert "EXISTS" not in sql, q  # 존재(EXISTS)로 강등되지 않음


def test_at_least_one_collapses_to_existence():
    # '1개 이상 / 1회 이상'(≥1)은 사실상 '사용한'(존재)과 동치 → 집계 없이 EXISTS.
    for q in ["쿠폰을 1개 이상 사용한 회원", "쿠폰을 1회 이상 사용한 회원"]:
        it = _interpret(q)
        assert it.condition.type == "existence_filter" and it.condition.exists is True, q
        sql = _sql(q)
        assert "R.USE_CPN_CNT > 0" in sql and "SUM(" not in sql, q


def test_gte_variants_compile():
    # '5회 이상'은 회원별 SUM>=5 집계로 컴파일(≥2 이상 임계는 존재로 강등하지 않는다).
    assert "SUM(COALESCE(R.USE_CPN_CNT, 0)) >= 5" in _sql("쿠폰 사용 횟수가 5회 이상인 회원")
    assert "SUM(COALESCE(R.USE_CPN_CNT, 0)) >= 3" in _sql("캠페인 쿠폰을 3개 이상 사용한 고객을 추출해줘.")


# ── 테스트 2: 쿠폰 건수 범위 ──────────────────────────────────────────────────────────────
def test_coupon_count_range_between():
    q = "쿠폰 사용 횟수가 1회에서 3회 사이인 회원을 추출해줘."
    cond = _interpret(q).condition
    assert cond.type == "metric_filter" and cond.operator == "between"
    assert cond.min_value == 1.0 and cond.max_value == 3.0
    sql = _sql(q)
    assert "SUM(COALESCE(R.USE_CPN_CNT, 0)) >= 1" in sql
    assert "SUM(COALESCE(R.USE_CPN_CNT, 0)) <= 3" in sql


def test_coupon_count_range_explicit_bounds():
    q = "쿠폰을 1회 이상 3회 이하 사용한 회원"
    cond = _interpret(q).condition
    assert cond.operator == "between" and cond.min_value == 1.0 and cond.max_value == 3.0
    assert "SUM(COALESCE(R.USE_CPN_CNT, 0)) <= 3" in _sql(q)


# ── 테스트 3: 쿠폰 사용 여부는 기존 동작 유지 ──────────────────────────────────────────────
def test_coupon_existence_positive_supported():
    q = "쿠폰을 사용한 회원"
    it = _interpret(q)
    assert it.condition.type == "existence_filter" and it.condition.exists is True
    assert it.capability.supported
    assert "R.USE_CPN_CNT > 0" in _sql(q)


# ── 테스트 4: 쿠폰 미사용 ────────────────────────────────────────────────────────────────
COUPON_NEGATIVE_VARIANTS = [
    "쿠폰을 한 번도 사용하지 않은 회원",
    "쿠폰 미사용 회원",
    "쿠폰 사용 이력이 없는 회원",
    "쿠폰을 사용하지 않은 회원",
]


def test_coupon_non_use_is_existence_false():
    for q in COUPON_NEGATIVE_VARIANTS:
        cond = _interpret(q).condition
        assert cond.type == "existence_filter" and cond.exists is False, q


def test_coupon_non_use_compiles_not_exists_not_empty():
    for q in COUPON_NEGATIVE_VARIANTS:
        sql = _sql(q)  # 후보 없음(빈 결과)이면 assert 실패
        assert "NOT EXISTS" in sql and "R.USE_CPN_CNT > 0" in sql, q
        responses = (_plan(q)["target_user"].get("campaign_responses") or [])
        assert any(r.get("canonical") == "no_coupon_used" and r.get("negated") for r in responses), q


# ── 테스트 5: 파생 지표 보존 ──────────────────────────────────────────────────────────────
def test_derived_per_coupon_amount_preserved():
    q = "쿠폰 한 개당 구매금액이 50,000원 이상인 회원을 보여줘."
    cond = _interpret(q).condition
    assert cond.type == "derived_metric_filter"
    assert cond.metric == "purchase_amount_per_coupon"
    assert cond.formula == {"type": "ratio", "numerator": "purchase_amount", "denominator": "coupon_usage_count"}


def test_derived_per_coupon_amount_gated_not_reduced():
    q = "쿠폰 한 개당 구매금액이 50,000원 이상인 회원을 보여줘."
    assert _reason(q) == "derived_metric_filter_unsupported"
    # 단순 누적 구매금액 SQL 로 축소되면 실패 — 미지원이라 SQL 자체가 생성되지 않아야 한다.
    assert _sql_or_none(q) is None


def test_derived_per_coupon_order_variants():
    for q in ["쿠폰당 구매금액이 5만 원 이상인 회원", "쿠폰 사용 건당 구매금액 50000원 이상"]:
        assert _interpret(q).condition.metric == "purchase_amount_per_coupon", q
        assert _reason(q) == "derived_metric_filter_unsupported", q


# ── 테스트 6: 지표 간 비교(순위가 아님) ────────────────────────────────────────────────────
def test_metric_comparison_not_ranking():
    q = "사용한 쿠폰 수보다 구매건수가 많은 고객을 찾아줘."
    cond = _interpret(q).condition
    assert cond.type == "metric_comparison"
    assert cond.left_metric == "purchase_count" and cond.operator == "gt"
    assert cond.right_metric == "coupon_usage_count"
    assert _reason(q) == "coupon_usage_count_metric_comparison_unsupported"


def test_metric_comparison_not_compiled_to_ranking():
    q = "사용한 쿠폰 수보다 구매건수가 많은 고객을 찾아줘."
    assert _sql_or_none(q) is None
    plan = _plan(q)
    assert plan.get("member_metric_ranking") is None
    assert plan.get("purchase_count_ranking") is None


def test_metric_comparison_reversed_phrasing():
    q = "구매건수가 쿠폰 사용 수를 초과한 회원"
    cond = _interpret(q).condition
    assert cond.type == "metric_comparison"
    assert cond.left_metric == "purchase_count" and cond.right_metric == "coupon_usage_count"
    assert _reason(q) == "coupon_usage_count_metric_comparison_unsupported"


# ── 테스트 7: 쿠폰 건수 순위 안내 ──────────────────────────────────────────────────────────
def test_coupon_ranking_unsupported_message_is_specific():
    q = "쿠폰 사용 횟수 기준 상위 100명의 고객을 보여줘."
    cond = _interpret(q).condition
    assert cond.type == "ranking" and cond.metric == "coupon_usage_count"
    plan = _plan(q)
    unsup = plan.get("unsupported") or {}
    assert unsup.get("reason") == "coupon_usage_count_ranking_unsupported"
    # 무관한 '어떤 지표로 순위를?'·'상품 개수/총수량' 안내가 나오면 안 된다.
    assert "순위" in unsup.get("message", "")
    assert "어떤 지표" not in unsup.get("message", "")
    assert "상품 개수" not in (unsup.get("clarification", "") + unsup.get("message", ""))


def test_coupon_ranking_variants():
    for q in ["쿠폰 사용 횟수가 많은 순으로 회원을 보여줘", "쿠폰 사용 건수 상위 회원"]:
        assert _reason(q) == "coupon_usage_count_ranking_unsupported", q


# ── 테스트 8: 기존 쿠폰 사용 여부 회귀(긍정/부정 각각 정확히 컴파일) ─────────────────────────
def test_existence_positive_and_negative_both_compile():
    assert "EXISTS (SELECT 1 FROM MCS_CAMP_MBR_RSPN_FT R" in _sql("쿠폰을 사용한 회원")
    assert "NOT EXISTS" in _sql("쿠폰을 사용하지 않은 회원")


def test_possession_not_treated_as_usage():
    # '쿠폰 3개 이상 보유'(소지)는 사용 의미가 아니다 — 쿠폰 사용 건수로 게이트하지 않는다.
    assert ss.interpret("쿠폰을 3개 이상 보유한 회원", _REG) is None


# ── 테스트 9: registry 기반 동작 검증(JSON 변경만으로 게이트 결과 변경) ────────────────────
def _registry_with(tmp_path, mutate):
    metrics = json.loads(Path("docs/data/segment_metrics.json").read_text(encoding="utf-8"))
    mutate(metrics)
    mp = tmp_path / "segment_metrics.json"
    mp.write_text(json.dumps(metrics, ensure_ascii=False), encoding="utf-8")
    return ss.SegmentSemanticsRegistry.load(mp, Path("docs/data/segment_operators.json"))


def test_ranking_capability_driven_by_json(tmp_path):
    q = "쿠폰 사용 횟수 기준 상위 100명의 고객을 보여줘."
    # 기본: 미지원
    assert not ss.interpret(q, _REG).capability.supported

    def enable_ranking(metrics):
        metrics["metrics"]["coupon_usage_count"]["capabilities"]["ranking"] = {"supported": True}
    reg2 = _registry_with(tmp_path, enable_ranking)
    # Python 분기 코드 변경 없이 JSON 만으로 게이트 결과가 바뀐다.
    assert ss.interpret(q, reg2).capability.supported


# ── 테스트 10: alias 추가 검증(JSON 별칭만 추가하면 정규식 수정 없이 인식) ──────────────────
def test_new_alias_recognized_without_code_change(tmp_path):
    q = "쿠폰 소진 횟수가 5회 이상인 회원"
    assert ss.interpret(q, _REG) is None  # 기본 스펙엔 없는 별칭

    def add_alias(metrics):
        metrics["metrics"]["coupon_usage_count"]["aliases"].append("쿠폰 소진 횟수")
    reg2 = _registry_with(tmp_path, add_alias)
    cond = ss.interpret(q, reg2).condition
    assert cond.metric == "coupon_usage_count" and cond.operator == "gte" and cond.value == 5.0


# ── 로더 스키마 검증(JSON 오류가 런타임에서 조용히 무시되지 않게) ──────────────────────────
def _write(tmp_path, metrics):
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(metrics, ensure_ascii=False), encoding="utf-8")
    return mp


def _base_metrics():
    return json.loads(Path("docs/data/segment_metrics.json").read_text(encoding="utf-8"))


def _load(tmp_path, metrics):
    return ss.SegmentSemanticsRegistry.load(_write(tmp_path, metrics), Path("docs/data/segment_operators.json"))


def test_loader_rejects_unsupported_op_without_message(tmp_path):
    m = _base_metrics()
    # ranking 은 미지원(supported=false)인데 안내 메시지를 없애면 로더가 거부해야 한다.
    m["metrics"]["coupon_usage_count"]["unsupported_messages"].pop("ranking")
    try:
        _load(tmp_path, m)
        assert False, "미지원 ranking 인데 메시지 없음 → 에러 기대"
    except ss.SegmentSemanticsError as exc:
        assert "unsupported_messages" in str(exc)


def test_loader_rejects_alias_conflict(tmp_path):
    m = _base_metrics()
    m["metrics"]["purchase_count"]["aliases"].append("쿠폰 사용 횟수")  # 다른 지표와 충돌
    try:
        _load(tmp_path, m)
        assert False, "별칭 충돌 → 에러 기대"
    except ss.SegmentSemanticsError as exc:
        assert "충돌" in str(exc)


def test_loader_rejects_bad_formula_operand(tmp_path):
    m = _base_metrics()
    m["metrics"]["purchase_amount_per_coupon"]["formula"]["denominator"] = "no_such_metric"
    try:
        _load(tmp_path, m)
        assert False, "formula operand 미등록 → 에러 기대"
    except ss.SegmentSemanticsError as exc:
        assert "formula" in str(exc)


def test_loader_rejects_bad_capability_operation(tmp_path):
    m = _base_metrics()
    m["metrics"]["coupon_usage_count"]["capabilities"]["teleport"] = {"supported": False}
    try:
        _load(tmp_path, m)
        assert False, "미지원 capability 연산 → 에러 기대"
    except ss.SegmentSemanticsError as exc:
        assert "capability" in str(exc)


def test_real_registry_loads_clean():
    # 실제 스펙 파일이 스키마를 통과하는지(드리프트 가드).
    reg = ss.SegmentSemanticsRegistry.load()
    assert "coupon_usage_count" in reg.metrics
    assert "purchase_amount_per_coupon" in reg.metrics


# ── 미지원 질의는 LLM 폴백 SQL 을 시도하지 않는다(그럴듯한 오답·의미검증 잡음 방지) ───────────
def test_unsupported_coupon_does_not_trigger_llm_fallback():
    # 미지원(unsupported)로 후보가 없어졌을 때 LLM 폴백이 돌면, 그럴듯하지만 틀린 SQL(쿠폰 건수 임계를
    # USE_CPN_CNT>0 존재로 축소)이 생성돼 의미검증에서 inverted/불일치로 떨어지는 '혼합/실패'가 된다.
    # llm_model 을 줘도 폴백이 시도되지 않고(호출 없음) 깔끔한 미지원 응답으로 끝나야 한다.
    q = "쿠폰 사용 횟수 기준 상위 100명의 고객을 보여줘."  # 순위는 여전히 미지원
    plan = _plan(q)
    res = g.build_sql_result(nx.Graph(), q, plan, [], g.DEFAULT_SCHEMA_PATH, default_limit=0, llm_model="gpt-4o-mini")
    assert res["sql"] is None
    assert res["llm_fallback_used"] is False
    assert res["is_success"] is False
    assert res["failure_reason"] == "coupon_usage_count_ranking_unsupported"
    # 의미검증(semantic_verification)이 아예 돌지 않아야 한다(후보 SQL 자체가 없으므로).
    assert not (res.get("semantic_verification") or {}).get("ran")


def test_coupon_unsupported_api_response_is_clear():
    # UI 표시 회귀: 미지원 쿠폰 조건(순위)은 (1) needs_clarification 상태, (2) 실DB 조건 매핑 단계로 표시,
    # (3) 무관한 '혜택 유형 조건'이 아니라 쿠폰 순위 안내 문구가 메시지로 나와야 한다.
    q = "쿠폰 사용 횟수 기준 상위 100명의 고객을 보여줘."
    plan = _plan(q)
    res = g.build_sql_result(nx.Graph(), q, plan, [], g.DEFAULT_SCHEMA_PATH, default_limit=0)
    api = g.build_recommendation_api_response(q, plan, res, {"content": None, "mode": None})
    assert api["status"] == "needs_clarification"
    assert api["failure_reason"] == "coupon_usage_count_ranking_unsupported"
    assert (api.get("failure_stage") or {}).get("label") == "실DB 조건 매핑"
    assert "순위" in api["message"]
    assert "혜택 유형" not in api["message"]


def test_skipped_execution_renders_as_skipped_not_fail():
    # 실행할 SQL 이 없어 생략된 실행은 트레이스 10단계에서 '실패'가 아니라 'skipped'로 표시돼야 한다.
    res = {
        "query": "쿠폰을 1개 이상 사용한 회원을 찾아줘.",
        "sql_result": {"sql": None, "is_success": False, "failure_reason": "coupon_usage_count_filter_unsupported", "candidates": []},
        "api_response": {"status": "needs_clarification", "sql": None, "message": "..."},
    }
    trace = g.build_retrieve_trace(res)
    g.apply_execution_to_trace(trace, {"is_success": False, "mode": "skipped", "failure_reason": "sql_result_missing"})
    stage10 = trace["stages"][9]
    assert stage10["status"] == "skipped"
    assert "실패" not in stage10["summary"]
