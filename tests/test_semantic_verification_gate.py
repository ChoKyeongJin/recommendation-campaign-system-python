"""최종 SQL↔원문 의미 검증 게이트 회귀.

배경: 정규식 파서가 '캠페인 구매 이력이 없는'을 EXISTS 구매(정반대)로 뒤집는 등, plan 자체가 틀리면
SQL↔plan 대조(coverage/intent_scope)는 못 잡는다. 이 게이트만 원문 NL 과 최종 SQL 을 직접 대조해
불일치를 확신할 때 틀린 SQL 출고를 막고 clarification 으로 전환한다. LLM 비결정성 때문에 게이트 자체는
LLM 이 하지만(여기선 monkeypatch 로 판정을 주입), 통합/차단 로직은 결정론이라 테스트한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_semantic_verification_gate.py -q
"""

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


def test_gate_does_not_block_on_dropped_issue():
    # dropped(누락)는 비차단 자문이다 — 판정 모델이 도메인 인코딩(장바구니=KEEP_YN='Y', 미접속=
    # LAST_LOGIN_DATE<=과거 등)을 못 알아봐 정상 SQL 을 '누락'으로 오판하는 오탐이 잦기 때문.
    # 진짜 누락은 결정론 감지기·커버리지 검증이 소유한다. inverted 만 차단한다.
    verdict = {"ran": True, "faithful": False, "issues": [
        {"type": "dropped", "condition": "결제하지 않은", "detail": "SQL에 결제 조건 없음"}]}
    res = _result(SUPPORTED, verdict)
    assert res["is_success"] is True and res["sql"] is not None
    assert res["failure_reason"] is None
    # 판정은 응답에 자문으로 남는다(디버깅·튜닝용).
    assert res["semantic_verification"]["issues"]


def test_gate_does_not_block_on_value_level_issue():
    # 값 수준(wrong_value)만 있으면 차단하지 않는다 — 판정 모델이 등급 서열·권역 구성을 몰라 정상 확장
    # ('GOLD 이상'→GOLD,VIP)을 오판하는 오탐이라, 값 정확성은 결정론 컴파일러·커버리지가 소유한다.
    verdict = {"ran": True, "faithful": False, "issues": [
        {"type": "wrong_value", "condition": "GOLD 이상", "detail": "GOLD, VIP 로 확장됨"}]}
    res = _result(SUPPORTED, verdict)
    assert res["is_success"] is True and res["sql"] is not None
    assert res["failure_reason"] is None
    # 판정 결과 자체는 응답에 자문으로 남는다(디버깅·튜닝용).
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
