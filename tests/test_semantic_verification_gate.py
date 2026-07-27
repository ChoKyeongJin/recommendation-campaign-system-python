"""최종 SQL↔원문 의미 검증 게이트 회귀.

배경: 정규식 파서가 '캠페인 구매 이력이 없는'을 EXISTS 구매(정반대)로 뒤집는 등, plan 자체가 틀리면
SQL↔plan 대조(coverage/intent_scope)는 못 잡는다. 이 게이트만 원문 NL 과 최종 SQL 을 직접 대조해
불일치를 확신할 때 틀린 SQL 출고를 막고 clarification 으로 전환한다. LLM 비결정성 때문에 게이트 자체는
LLM 이 하지만(여기선 monkeypatch 로 판정을 주입), 통합/차단 로직은 결정론이라 테스트한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_semantic_verification_gate.py -q
"""

from types import SimpleNamespace

import networkx as nx
import pytest

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _result(query: str, verdict: dict | None) -> dict:
    """build_sql_result 를 태우되, 게이트 판정만 monkeypatch 로 주입(OPENAI 불필요·결정론)."""
    plan = _plan(query)
    orig = g._verify_sql_semantics
    try:
        if verdict is not None:
            g._verify_sql_semantics = lambda *a, **k: verdict
        return g.build_sql_result(
            graph=nx.Graph(),
            query=query,
            query_plan=plan,
            context_nodes=[],
            schema_path=g.DEFAULT_SCHEMA_PATH,
            default_limit=None,
            llm_model="test-model",  # non-None 이라야 게이트가 돈다(rules 경로는 None 이라 skip)
            original_query=query,
            prompt_dir=g.DEFAULT_PROMPT_DIR,
        )
    finally:
        g._verify_sql_semantics = orig


SUPPORTED = "서울에 거주하는 30대 여성 회원"


# ── 차단: 게이트가 불일치를 확신하면 SQL 출고를 막고 clarification 으로 전환 ──────────────
def test_gate_blocks_on_mismatch():
    verdict = {"ran": True, "faithful": False, "issues": [
        {"type": "inverted", "condition": "캠페인 구매 이력이 없는", "detail": "SQL은 구매함(EXISTS)으로 반영"}]}
    res = _result(SUPPORTED, verdict)
    assert res["is_success"] is False
    assert res["sql"] is None
    assert res["failure_reason"] == "semantic_verification_failed"
    assert res["clarification_questions"], "불일치 시 clarification 이 있어야 한다"
    assert any("캠페인 구매 이력이 없는" in q for q in res["clarification_questions"])
    assert _api_status(res) == "needs_clarification"


def _api_status(res: dict) -> str:
    return g._api_status(res)


def test_blocked_sql_preserved_for_display():
    # 차단되면(inverted) 출고(sql)는 None 이지만, 무엇이 생성됐는지 표시용으로 blocked_sql 에 원본 SQL 을 보존한다.
    verdict = {"ran": True, "faithful": False, "issues": [
        {"type": "inverted", "condition": "미접속", "detail": "극성 뒤집힘"}]}
    res = _result(SUPPORTED, verdict)
    assert res["sql"] is None  # 출고/실행은 막힘
    assert res["blocked_sql"] and "SELECT" in res["blocked_sql"]  # 표시용 보존
    # api_response 로도 노출된다(프론트가 sql 없을 때 blocked_sql 로 폴백).
    api = g.build_recommendation_api_response(SUPPORTED, _plan(SUPPORTED), res, {"content": None, "mode": None, "failure_reason": None})
    assert api["sql"] is None
    assert api["blocked_sql"] == res["blocked_sql"]


def test_blocked_sql_none_on_success():
    # 정상 출고면 blocked_sql 은 None(막힌 게 없음).
    res = _result(SUPPORTED, {"ran": True, "faithful": True, "issues": []})
    assert res["sql"] is not None
    assert res["blocked_sql"] is None


def test_gate_blocks_on_dropped_issue_when_unfaithful():
    verdict = {"ran": True, "faithful": False, "issues": [
        {"type": "dropped", "condition": "결제하지 않은", "detail": "SQL에 결제 조건 없음"}]}
    res = _result(SUPPORTED, verdict)
    assert res["is_success"] is False and res["sql"] is None
    assert res["failure_reason"] == "semantic_verification_failed"
    assert res["semantic_verification"]["issues"]


def test_gate_blocks_on_value_level_issue_when_unfaithful():
    verdict = {"ran": True, "faithful": False, "issues": [
        {"type": "wrong_value", "condition": "GOLD 이상", "detail": "GOLD, VIP 로 확장됨"}]}
    res = _result(SUPPORTED, verdict)
    assert res["is_success"] is False and res["sql"] is None
    assert res["failure_reason"] == "semantic_verification_failed"
    assert res["semantic_verification"]["issues"]


def test_gate_blocks_on_inverted_even_with_value_issue():
    verdict = {"ran": True, "faithful": False, "issues": [
        {"type": "wrong_value", "condition": "GOLD 이상", "detail": "확장"},
        {"type": "inverted", "condition": "구매 없는", "detail": "EXISTS 로 뒤집힘"}]}
    res = _result(SUPPORTED, verdict)
    assert res["is_success"] is False and res["failure_reason"] == "semantic_verification_failed"
    # 차단 사유엔 inverted 만 들어가고 wrong_value 는 clarification 에서 빠진다.
    assert any("구매 없는" in q for q in res["clarification_questions"])
    assert not any("GOLD" in q for q in res["clarification_questions"])


# ── 연령대 결정론 변환은 LLM inverted 판정으로 차단하지 않는다(값 산술 오판 면제) ──────────
AGE_OR_QUERY = "20대 또는 30대이면서 구매 횟수가 5회 이상인 회원을 찾아줘."


def test_gate_blocks_any_unfaithful_age_range_verdict():
    verdict = {"ran": True, "faithful": False, "issues": [
        {"type": "inverted", "condition": "20대 또는 30대",
         "detail": "SQL에서 20세 이상 39세 이하로 잘못 반영됨"}]}
    res = _result(AGE_OR_QUERY, verdict)
    assert res["is_success"] is False
    assert res["sql"] is None and res["blocked_sql"]
    assert res["failure_reason"] == "semantic_verification_failed"
    assert res["semantic_verification"]["issues"]


def test_gate_blocks_any_unfaithful_positive_threshold_verdict():
    verdict = {"ran": True, "faithful": False, "issues": [
        {"type": "inverted", "condition": "구매 횟수가 5회 이상",
         "detail": "서브쿼리로 잘못 반영되어 부정형으로 해석됨"}]}
    res = _result(AGE_OR_QUERY, verdict)
    assert res["is_success"] is False
    assert res["failure_reason"] == "semantic_verification_failed"


def test_gate_still_blocks_real_polarity_inversion():
    # 회귀: 부정/제외 표지가 있는 진짜 극성 반전은 여전히 차단한다(면제는 '양의 조건' 한정).
    for cond in ["구매하지 않은", "구매 이력이 없는", "미접속", "블랙리스트가 아닌"]:
        verdict = {"ran": True, "faithful": False, "issues": [
            {"type": "inverted", "condition": cond, "detail": "극성 뒤집힘"}]}
        res = _result(AGE_OR_QUERY, verdict)
        assert res["is_success"] is False, cond
        assert res["failure_reason"] == "semantic_verification_failed", cond


# ── 공통 requirement 회계로 사일런트 드롭 승격(브랜드 전용 감지기 대체) ──────────────────────
def test_gate_escalates_when_requirement_unresolved(monkeypatch):
    # 공통 requirement 계층이 차단 requirement(unsupported/clarification)를 돌려주면 게이트가
    # needs_clarification 로 승격하는지 배선 검증(회계 로직 자체는 tests/test_semantic_requirements.py).
    import semantic_requirements as sr
    blocking = sr.SourceRequirement(
        id="req_1", type="qualified_condition", base={"type": "behavior", "name": "cart_retention"},
        qualifiers=[sr.Qualifier(type="entity", domain="brand", raw_value="CJ제일제당")],
        status="unsupported", message="현재 장바구니 조건에는 브랜드 필터를 함께 적용할 수 없습니다.")
    monkeypatch.setattr(g, "_account_source_requirements",
                        lambda *a, **k: sr.RequirementAccounting(requirements=[blocking]))
    res = _result(SUPPORTED, {"ran": True, "faithful": True, "issues": []})
    assert res["is_success"] is False
    assert res["sql"] is None and res["blocked_sql"]
    assert res["failure_reason"] == "semantic_verification_failed"
    assert any("브랜드" in q for q in res["clarification_questions"])
    assert g._api_status(res) == "needs_clarification"
    # 회계 결과는 응답에 노출된다(트레이스/디버깅).
    assert res["source_requirements"] and res["source_requirements"][0]["status"] == "unsupported"


def test_gate_success_when_all_requirements_resolved(monkeypatch):
    # 모든 requirement 가 compiled 로 귀결되면 정상 성공(차단 없음).
    import semantic_requirements as sr
    ok = sr.SourceRequirement(
        id="req_1", type="qualified_condition", base={"type": "behavior", "name": "purchase"},
        qualifiers=[sr.Qualifier(type="entity", domain="brand", raw_value="알로루")], status="compiled")
    monkeypatch.setattr(g, "_account_source_requirements",
                        lambda *a, **k: sr.RequirementAccounting(requirements=[ok]))
    res = _result(SUPPORTED, {"ran": True, "faithful": True, "issues": []})
    assert res["is_success"] is True and res["sql"] is not None


# ── 통과: faithful 이면 SQL 그대로 성공 ────────────────────────────────────────────────
def test_gate_passes_when_faithful():
    res = _result(SUPPORTED, {"ran": True, "faithful": True, "issues": []})
    assert res["is_success"] is True
    assert res["sql"] and "B.SIDO IN ('서울')" in res["sql"]
    assert res["failure_reason"] is None


# ── fail-open: 게이트 미실행(ran=False)이면 정상 SQL 을 막지 않는다 ─────────────────────
def test_gate_fail_open_when_not_run():
    res = _result(SUPPORTED, {"ran": False})
    assert res["is_success"] is True
    assert res["sql"] is not None


def test_gate_skipped_without_llm_model():
    # llm_model=None(rules 경로)이면 게이트 자체가 안 돌아 실제 _verify_sql_semantics 도 ran=False.
    plan = _plan(SUPPORTED)
    res = g.build_sql_result(
        graph=nx.Graph(), query=SUPPORTED, query_plan=plan, context_nodes=[],
        schema_path=g.DEFAULT_SCHEMA_PATH, default_limit=None,
        llm_model=None, original_query=SUPPORTED, prompt_dir=g.DEFAULT_PROMPT_DIR,
    )
    assert res["is_success"] is True
    assert res["semantic_verification"] == {"ran": False}


# ── 게이트 자체의 fail-open: OPENAI 없으면 ran=False(정상 통과) ──────────────────────────
def test_verify_returns_not_run_without_openai(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    verdict = g._verify_sql_semantics("서울 30대 여성", "SELECT 1", "test-model", g.DEFAULT_PROMPT_DIR)
    assert verdict == {"ran": False}


def test_verify_disabled_by_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("SQL_SEMANTIC_VERIFY", "off")
    assert g._sql_semantic_verify_enabled() is False
    assert g._verify_sql_semantics("q", "SELECT 1", "test-model", None) == {"ran": False}


def test_verify_receives_targeting_member_policy_context(monkeypatch):
    captured = {}

    def fake_chat_create(_client, **kwargs):
        captured.update(kwargs)
        content = '{"faithful":true,"issues":[]}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SQL_SEMANTIC_VERIFY", "on")
    monkeypatch.setattr(g, "_openai_chat_create", fake_chat_create)
    monkeypatch.setattr(g, "_write_rag_llm_log", lambda *_args, **_kwargs: None)

    query = "2019년에 이십만원 이상을 구매한 고객에서 남자는 제외해."
    plan = _plan(query)
    sql = g.build_aggregate_targets_sql_candidate(plan)["sql"]
    verdict = g._verify_sql_semantics(query, sql, "test-model", g.DEFAULT_PROMPT_DIR, plan)

    assert verdict["faithful"] is True
    user_content = captured["messages"][-1]["content"]
    assert "[적용된 서비스 정책]" in user_content
    assert '"id": "policy_active_member"' in user_content
    assert '"value": "MEMBER_STATE_CD.NORMAL"' in user_content


# ── clarification 문구 포맷 ────────────────────────────────────────────────────────────
def test_clarification_formatting():
    qs = g._semantic_verification_clarifications([
        {"type": "dropped", "condition": "최근 로그인", "detail": "SQL에 LAST_LOGIN 조건 없음"}])
    assert len(qs) == 1
    assert "최근 로그인" in qs[0] and "누락" in qs[0]


def test_describe_failure_uses_clarifications():
    res = {
        "failure_reason": "semantic_verification_failed",
        "clarification_questions": ["'X' 조건이 ... 확인해 주세요."],
        "semantic_verification": {"ran": True, "faithful": False, "issues": []},
    }
    msg = g._describe_sql_failure({}, res)
    assert "확인이 필요합니다" in msg and "X" in msg
