"""셀 단위 성공률/구매율 비율 타겟팅 회귀 (cell_rate_target).

배경: "발송 성공률은 높지만 구매율이 낮은 셀의 회원을 찾아줘"가 (1) 접촉성공 정규식('발송성공')이
'발송 성공률'의 부분문자열에 걸려 회원 단위 EXISTS 로 강등되고 (2) LLM 재작성이 '구매율 낮음'을
'미구매'(평생 무주문 anti-join)로 극단화해, 셀 단위 비율 의미가 통째로 사라졌다. 이제
cell_rate_target 이 Z_CAMP_MBR(셀별 발송 대상 명단 = 분모, 회원별 접촉성공)를 셀로 집계해
성공률·구매율 HAVING 으로 셀을 고르고 그 셀의 발송 대상 회원을 타겟한다. '높은/낮은' 막연어는
설정 기본 임계(높음 80% 이상, 낮음 10% 이하)로 컴파일하고 명시 % 는 그대로 쓴다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_cell_rate_targeting.py -q
"""

import pytest

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _sql(query: str) -> str:
    cand = g.build_sql_template_candidate(_plan(query))
    assert cand is not None, f"{query!r}: SQL 미생성"
    return cand["sql"]


ORIGINAL_PROMPT = "발송 성공률은 높지만 구매율이 낮은 셀의 회원을 찾아줘."


# ── 파싱: 막연어(높은/낮은)는 기본 임계, 명시 % 는 그대로 ──────────────────────────────
def test_vague_rates_parsed_with_defaults():
    rate = _plan(ORIGINAL_PROMPT)["target_user"].get("cell_rate_target")
    assert isinstance(rate, dict)
    assert rate["success_rate"] == {"operator": ">=", "value": 80.0, "inferred": True}
    assert rate["buy_rate"] == {"operator": "<=", "value": 10.0, "inferred": True}


def test_explicit_rates_parsed():
    rate = _plan("발송 성공률 90% 이상이고 구매율 5% 이하인 셀의 회원")["target_user"]["cell_rate_target"]
    assert rate["success_rate"] == {"operator": ">=", "value": 90.0, "inferred": False}
    assert rate["buy_rate"] == {"operator": "<=", "value": 5.0, "inferred": False}


def test_single_rate_parsed():
    rate = _plan("구매율이 낮은 셀의 회원을 찾아줘")["target_user"]["cell_rate_target"]
    assert rate["success_rate"] is None
    assert rate["buy_rate"] == {"operator": "<=", "value": 10.0, "inferred": True}


@pytest.mark.parametrize("query", [
    # 비율어 없는 회원 단위 접촉성공/미구매는 기존 트랙 소유다.
    "발송에 성공한 회원",
    "캠페인 발송 성공 후 미구매 회원",
    "구매 금액 20만원 이상 회원",
])
def test_not_stolen(query):
    assert _plan(query)["target_user"].get("cell_rate_target") is None, query


# ── 오배정 정리: 접촉성공 EXISTS 강등·no_purchase 극단화를 걷어낸다 ─────────────────────
def test_prunes_misassigned_contact_exists():
    tu = _plan(ORIGINAL_PROMPT)["target_user"]
    assert not [
        r for r in tu.get("campaign_responses", []) if r.get("canonical") == "campaign_contact"
    ]


def test_prunes_no_purchase_when_buy_rate_present():
    plan = {"target_user": {"behaviors": ["no_purchase"]}}
    g._apply_cell_rate_target_filter("발송 성공률은 높지만 구매율이 낮은 셀의 회원", plan)
    assert plan["target_user"]["behaviors"] == []
    assert plan["target_user"]["cell_rate_target"]["buy_rate"]["value"] == 10.0


def test_keeps_contact_exists_without_rate_context():
    tu = _plan("발송에 성공한 회원")["target_user"]
    assert [r for r in tu.get("campaign_responses", []) if r.get("canonical") == "campaign_contact"]


# ── SQL: Z_CAMP_MBR 셀 집계 HAVING → 셀 회원 조인으로 컴파일된다 ───────────────────────
def test_cell_rate_sql_shape():
    sql = _sql(ORIGINAL_PROMPT)
    # 회원 단위 EXISTS/평생 무주문이 아니라 셀 집계다.
    assert "Z_CAMP_MBR M" in sql
    assert "NOT EXISTS" not in sql
    assert "CRM_SL_ORDERHEADERMALL" not in sql
    # 셀 키 집계 + 성공률/구매율 HAVING(기본 임계 80/10).
    assert "GROUP BY M2.CAMP_ID, M2.CAMP_EXEC_NO, M2.CELL_NODE_ID" in sql
    assert "SUM(CASE WHEN M2.CONTAC_SUCC_YN = 'Y' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) >= 80" in sql
    assert "COUNT(DISTINCT R.MBR_NO) * 100.0 / COUNT(DISTINCT M2.MBR_NO) <= 10" in sql
    # 구매율 분자는 반응 팩트의 구매반응 행(LEFT JOIN — 무반응 셀도 분모 유지).
    assert "LEFT JOIN MCS_CAMP_MBR_RSPN_FT R" in sql
    assert "R.BUY_RSPN_YN = 'Y'" in sql
    # 셀 회원 조인 + 캐스트 회원키 조인.
    assert "CELL.CELL_NODE_ID = M.CELL_NODE_ID" in sql
    assert "TRY_CAST(M.MBR_NO AS BIGINT) = B.MEMBER_NO" in sql


def test_cell_rate_combines_with_member_attribute():
    sql = _sql("발송 성공률은 높지만 구매율이 낮은 셀의 VIP 여성 회원")
    assert "SUM(CASE WHEN M2.CONTAC_SUCC_YN = 'Y' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) >= 80" in sql
    assert "B.GENDER_CD = 'GENDER_CD.FEMALE'" in sql
    assert "B.EMART_GRADE_CD = 'MEM_GRADE_CD.VIP'" in sql


def test_explicit_threshold_in_sql():
    sql = _sql("발송 성공률 90% 이상이고 구매율 5% 이하인 셀의 회원")
    assert "* 100.0 / COUNT(*) >= 90" in sql
    assert "COUNT(DISTINCT M2.MBR_NO) <= 5" in sql


def test_label_marks_inferred_defaults():
    rate = _plan(ORIGINAL_PROMPT)["target_user"]["cell_rate_target"]
    assert "기본 임계" in rate["label"]
    sql = _sql(ORIGINAL_PROMPT)
    assert "발송 성공률 80% 이상(기본 임계)" in sql


# ── 소유권/양보 ─────────────────────────────────────────────────────────────────────
def test_builder_owns_cell_rate_target():
    for builder, owned in g._sql_target_builder_registry():
        if builder is g.build_cell_rate_targets_sql_candidate:
            assert "cell_rate_target" in owned
            break
    else:
        pytest.fail("셀 비율 빌더가 레지스트리에 없음")


def test_exists_builder_defers_to_cell_rate():
    plan = {
        "intent": "find_user_segment",
        "target_user": {
            "campaign_responses": [{"canonical": "offer_response", "predicate": "R.OFFR_RSPN_YN = 'Y'"}],
            "cell_rate_target": {
                "success_rate": {"operator": ">=", "value": 80.0, "inferred": True},
                "buy_rate": None,
                "label": "셀 발송 성공률 80% 이상(기본 임계)",
            },
        },
    }
    assert g.build_campaign_response_targets_sql_candidate(plan) is None


def test_config_defaults_loaded():
    cfg = g._MEMBER_TARGET_FILTERS.get("cell_rate_targets", {})
    assert cfg.get("member_table") == "Z_CAMP_MBR"
    assert cfg.get("vague_high_default") == 80
    assert cfg.get("vague_low_default") == 10
