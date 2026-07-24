"""LLM 구조화 슬롯 coercion 회귀 (Stage A — 파서 역할 재분배).

배경: LLM tool 스키마의 target_user 가 불투명 {"type":"object"}라 LLM 이 어떤 구조화 슬롯(가입창/
로그인창/카트/캠페인/집계)이 있는지 몰랐고, coerce 도 그 슬롯을 받지 않아 결정론 정규식만이 유일한
소스였다. 이제 targeting_ir.SLOT_SHAPES 가 슬롯별 (i) LLM tool JSON-schema 조각과 (ii) 닫힌 어휘
coerce 를 단일 소스로 선언하고, graph_rag 가 이걸로 tool 스키마를 생성·LLM 출력을 검증한다.

신뢰 모델: 덧셈형 — 정규식이 채운 슬롯은 불가침, LLM 은 정규식이 못 잡은(표현 변형) 빈 슬롯만 채운다.
실 LLM 불필요 — 시뮬레이션 candidate dict 를 coerce 에 직접 먹인다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_llm_structured_coercion.py -q
"""

import pytest

import graph_rag as g
import targeting_ir as ir


# ── 슬롯 coercion: 정규식이 못 잡던 표현이 닫힌 어휘로 정규화된다 ─────────────────────────
def test_signup_year_window_coerced():
    # "1년 이내 가입" — 정규식 창 파서가 년 단위를 몰라 놓치던 케이스.
    out = g._coerce_llm_structured_conditions({"target_user": {"signup_target": {"value": 1, "unit": "years"}}})
    assert out["signup_target"] == {"days": 365}


def test_signup_days_and_null():
    assert g._coerce_llm_structured_conditions({"target_user": {"signup_target": {"days": 90}}})["signup_target"] == {"days": 90}
    assert g._coerce_llm_structured_conditions({"target_user": {"signup_target": {"days": None}}})["signup_target"] == {"days": None}


def test_recent_login_min_days_default():
    # 숫자 없는 "최근 로그인" → LLM 이 기본 창(min_days)로 채움.
    out = g._coerce_llm_structured_conditions({"target_user": {"recent_login": {"min_days": 30}}})
    assert out["recent_login"] == {"value": 30, "unit": "days", "min_days": 30, "sql_interval": "30 days"}


def test_recent_login_value_unit():
    out = g._coerce_llm_structured_conditions({"target_user": {"recent_login": {"value": 2, "unit": "weeks"}}})
    assert out["recent_login"] == {"value": 2, "unit": "weeks", "min_days": 14, "sql_interval": "2 weeks"}


def test_purchase_inactivity_no_sql_interval():
    # purchase_inactivity 는 sql_interval 을 갖지 않는다(빌더 계약).
    out = g._coerce_llm_structured_conditions({"target_user": {"purchase_inactivity": {"value": 6, "unit": "months"}}})
    assert out["purchase_inactivity"] == {"value": 6, "unit": "months", "min_days": 180}
    assert "sql_interval" not in out["purchase_inactivity"]


def test_cart_retention_min_and_max():
    lo = g._coerce_llm_structured_conditions({"target_user": {"cart_retention": {"min_days": 7}}})
    assert lo["cart_retention"]["min_days"] == 7
    hi = g._coerce_llm_structured_conditions({"target_user": {"cart_retention": {"value": 3, "unit": "days", "direction": "max"}}})
    assert hi["cart_retention"]["max_days"] == 3


def test_cart_aggregate_metric_gated():
    ok = g._coerce_llm_structured_conditions({"target_user": {"cart_aggregate": {"metric": "cart_line_count", "operator": "이상", "threshold": 2}}})
    assert ok["cart_aggregate"] == {"metric": "cart_line_count", "operator": ">=", "threshold": 2}
    bad = g._coerce_llm_structured_conditions({"target_user": {"cart_aggregate": {"metric": "made_up", "operator": ">=", "threshold": 2}}})
    assert "cart_aggregate" not in bad


def test_cart_type_canonical_mapped():
    out = g._coerce_llm_structured_conditions({"target_user": {"cart_type": {"value": "CART_TYPE_CD.GENERAL"}}})
    assert out["cart_type"]["value"] == "CART_TYPE_CD.GENERAL"
    assert out["cart_type"]["canonical"] and out["cart_type"]["unpaid_only"] is False


def test_campaign_responses_canonical_to_predicate():
    out = g._coerce_llm_structured_conditions({"target_user": {"campaign_responses": [
        {"canonical": "campaign_contact"}, {"canonical": "no_buy_response", "negated": True}]}})
    by = {r["canonical"]: r for r in out["campaign_responses"]}
    # 접촉 성공은 발송 명단(camp_member_list) 소스로 매핑된다.
    assert by["campaign_contact"]["source"] == "camp_member_list"
    assert by["campaign_contact"]["predicate"] == "M.CONTAC_SUCC_YN = 'Y'"
    assert by["no_buy_response"]["negated"] is True and by["no_buy_response"]["predicate"] == "R.BUY_RSPN_YN = 'Y'"


def test_campaign_frequency_and_buy_amount():
    freq = g._coerce_llm_structured_conditions({"target_user": {"campaign_response_frequency": {"operator": "이상", "count": 2}}})
    assert freq["campaign_response_frequency"]["operator"] == ">=" and freq["campaign_response_frequency"]["count"] == 2
    buy = g._coerce_llm_structured_conditions({"target_user": {"campaign_buy_amount": {"operator": ">=", "amount": 200000}}})
    assert buy["campaign_buy_amount"]["amount"] == 200000


def test_cell_rate_coerced():
    out = g._coerce_llm_structured_conditions({"target_user": {"cell_rate_target": {
        "success_rate": {"operator": ">=", "value": 80}, "buy_rate": {"operator": "<=", "value": 10}}}})
    cr = out["cell_rate_target"]
    assert cr["success_rate"] == {"operator": ">=", "value": 80.0, "inferred": False}
    assert cr["buy_rate"]["operator"] == "<="


def test_aggregate_conditions_list_gated():
    out = g._coerce_llm_structured_conditions({"target_user": {"aggregate_conditions": [
        {"metric_id": "purchase_amount", "operator": "이상", "threshold": 200000}]}})
    assert out["aggregate_conditions"][0]["metric_id"] == "purchase_amount"
    assert out["aggregate_conditions"][0]["operator"] == ">="


def test_purchase_date_requires_year():
    ok = g._coerce_llm_structured_conditions({"target_user": {"purchase_date": {"from": "20240101", "to": "20240131"}}})
    assert ok["purchase_date"]["from"] == "20240101"
    bad = g._coerce_llm_structured_conditions({"target_user": {"purchase_date": {"from": "0101", "to": "0131"}}})
    assert "purchase_date" not in bad


# ── 환각 drop: 어휘/형식 이탈은 슬롯 자체가 빠진다 ────────────────────────────────────
@pytest.mark.parametrize("tu", [
    {"signup_target": {"days": "작년"}},
    {"recent_login": {"unit": "decade"}},
    {"cell_rate_target": {"success_rate": {"operator": "~=", "value": 80}}},
    {"cart_type": {"value": "subscription"}},
    {"campaign_responses": [{"canonical": "made_up_response"}]},
    {"cart_aggregate": {"metric": "cart_line_count", "operator": ">=", "threshold": -1}},
    {"aggregate_conditions": [{"metric_id": "unknown_metric", "operator": ">=", "threshold": 100}]},
])
def test_hallucination_dropped(tu):
    out = g._coerce_llm_structured_conditions({"target_user": tu})
    assert out == {} or all(v for v in out.values())


# ── 덧셈형 병합: 정규식이 채운 슬롯은 LLM 이 덮지 않는다 ──────────────────────────────
def test_apply_slots_fill_if_empty():
    plan = {"target_user": {"signup_target": None, "recent_login": None},
            "_llm_structured_slots": {"signup_target": {"days": 365}, "recent_login": {"value": 1, "unit": "days", "min_days": 1, "sql_interval": "1 days"}}}
    g._apply_llm_structured_slots(plan)
    assert plan["target_user"]["signup_target"] == {"days": 365}
    assert "_llm_structured_slots" not in plan


def test_apply_slots_regex_wins_when_present():
    plan = {"target_user": {"signup_target": {"days": 90}},  # 정규식이 이미 채움
            "_llm_structured_slots": {"signup_target": {"days": 365}}}
    g._apply_llm_structured_slots(plan)
    assert plan["target_user"]["signup_target"] == {"days": 90}  # 정규식 값 유지


def test_plan_level_slot_container():
    plan = {"target_user": {}, "member_metric_ranking": None,
            "_llm_structured_slots": {"member_metric_ranking": {"metric_id": "x", "top_n": 10}}}
    g._apply_llm_structured_slots(plan)
    assert plan["member_metric_ranking"] == {"metric_id": "x", "top_n": 10}


# ── 스키마 자기정합성 / 레지스트리 단일 소스 ──────────────────────────────────────────
def test_tool_schema_covers_every_target_user_slot():
    props = g._QUERY_PLAN_TOOL["function"]["parameters"]["properties"]["target_user"]["properties"]
    for shape in ir.structured_slot_shapes():
        if shape.container == "target_user":
            assert shape.name in props, shape.name


def test_every_slot_name_is_a_condition_kind():
    kinds = {spec.kind for spec in ir.CONDITION_SPECS}
    assert set(ir.SLOT_SHAPES) <= kinds


def test_closed_vocab_units_and_operators():
    assert set(ir.UNIT_DAYS) == {"days", "weeks", "months", "years"}
    assert ir.OPERATORS == {">=", ">", "<=", "<"}


# ── coerce 후 슬롯이 대응 ConfidenceMeta.applies 를 통과(confidence 무변경 유지) ──────────
def test_coerced_slots_pass_confidence_applies():
    coerced = g._coerce_llm_structured_conditions({"target_user": {
        "recent_login": {"value": 6, "unit": "months"},
        "purchase_inactivity": {"min_days": 90},
        "campaign_response_frequency": {"operator": ">=", "count": 2},
    }})
    specs = {spec.kind: spec for spec in ir.CONDITION_SPECS}
    for name, value in coerced.items():
        meta = specs[name].confidence
        if meta is not None:
            assert meta.applies(value), name
