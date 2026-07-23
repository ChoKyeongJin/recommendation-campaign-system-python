"""SQL 파이프라인(복잡도 판별 → Tool Calling → AST → Validation → SQL) 회귀.

파이프라인:
  사용자 질문 → 의도·복잡도 판별(classify_query_complexity) → [파서 auto: 단순이면 LLM 플랜 스킵,
  복잡이면 Query Plan(LLM 은 Tool Calling 구조화 출력)] → 빌더 → SelectAst(AST) →
  validate_select_ast(Validation, member_target_filters.json "validation") → render_select_ast → SQL

고정 내용:
  - 렌더 동등성: SelectAst 렌더가 기존 문자열 join 과 동일한 SQL 을 만든다(기존 259개 테스트가 보장,
    여기서는 AST 경로 사용 여부와 validation 통과를 명시 검증).
  - Validation 게이트: 허용 목록 밖 별칭·raw SQL 토큰이 있으면 후보가 거부된다(예전 죽은 설정 소생).
  - 복잡도 판별: 회원 속성만이면 simple, 조인/집계/합집합이 필요하면 complex.
  - Tool Calling: LLM 파서는 submit_query_plan 함수 스키마로 구조화 출력을 강제한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_sql_ast_pipeline.py -q
"""

import graph_rag as g
from sql_ast import SelectAst, collect_aliases, render_select_ast, validate_select_ast


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


# ── AST 렌더러 ─────────────────────────────────────────────────────────
def test_render_select_ast_shape():
    ast = SelectAst(
        distinct=True,
        columns=["B.MEMBER_NO AS CUST_ID"],
        from_lines=["FROM CRM_MB_BASEINFO B"],
        where=["B.GENDER_CD = 'GENDER_CD.FEMALE'", "B.AGE >= 20"],
    )
    assert render_select_ast(ast) == (
        "SELECT DISTINCT B.MEMBER_NO AS CUST_ID\n"
        "FROM CRM_MB_BASEINFO B\n"
        "WHERE B.GENDER_CD = 'GENDER_CD.FEMALE'\n"
        "  AND B.AGE >= 20"
    )


def test_member_builder_uses_ast_and_passes_validation():
    cand = g.build_sql_template_candidate(_plan("서울 거주 30대 여성"))
    assert cand is not None
    assert cand["validation"]["ast_used"] is True
    assert cand["validation"]["issues"] == []


def test_legacy_string_builder_still_validated():
    # AST 미전환 빌더(구매 이력)도 같은 검증 게이트를 통과한다(ast_used=False, issues 없음).
    cand = g.build_sql_template_candidate(_plan("생수를 구매한 고객"))
    assert cand is not None
    assert cand["validation"]["ast_used"] is False
    assert cand["validation"]["issues"] == []


# ── Validation 게이트 ──────────────────────────────────────────────────
def test_validate_rejects_unknown_alias():
    ast = SelectAst(columns=["X.A"], from_lines=["FROM EVIL_TABLE X"], where=["X.A = 1"])
    issues = validate_select_ast(ast, g._sql_validation_config())
    assert any("별칭" in issue and "X" in issue for issue in issues)


def test_validate_rejects_raw_sql_tokens():
    ast = SelectAst(columns=["B.MEMBER_NO"], from_lines=["FROM CRM_MB_BASEINFO B"], where=["B.AGE > 20; DROP TABLE x"])
    issues = validate_select_ast(ast, {"allow_raw_sql": False})
    assert any("raw SQL" in issue for issue in issues)


def test_validate_ignores_tokens_inside_string_literals():
    # 문자열 리터럴 안의 세미콜론/대시는 오탐하지 않는다.
    ast = SelectAst(columns=["B.MEMBER_NO"], from_lines=["FROM CRM_MB_BASEINFO B"], where=["B.NAME = 'a;b--c'"])
    assert validate_select_ast(ast, {"allow_raw_sql": False}) == []


def test_collect_aliases_includes_subquery():
    ast = SelectAst(
        columns=["B.MEMBER_NO"],
        from_lines=["FROM CRM_MB_BASEINFO B"],
        where=["EXISTS (SELECT 1 FROM MCS_CAMP_MBR_RSPN_FT R WHERE R.MBR_NO = B.MEMBER_NO)"],
    )
    assert collect_aliases(ast) == {"B", "R"}


def test_orchestrator_rejects_invalid_candidate(monkeypatch):
    # 검증 위반 후보는 오케스트레이터가 채택하지 않는다(다음 빌더로 폴스루 → 최종 None).
    bad = {"id": "x", "sql": "SELECT 1", "source": "sql_template", "validation": {"issues": ["허용되지 않은 테이블 별칭: X"]}}
    monkeypatch.setattr(g, "_sql_target_builders", lambda: (lambda plan: bad,))
    assert g.build_sql_template_candidate({"intent": "find_user_segment"}) is None


# ── 의도·복잡도 판별 ───────────────────────────────────────────────────
def test_simple_member_query_classified_simple():
    plan = _plan("서울 거주 30대 여성")
    assert plan["complexity"] == "simple"


def test_join_aggregate_queries_classified_complex():
    for query in ("생수를 구매한 고객", "장바구니 이탈 고객", "누적 구매 금액 10만원 이상 회원", "쿠폰을 사용한 회원"):
        assert _plan(query)["complexity"] == "complex", query


def test_auto_parser_skips_llm_for_simple_query():
    # 단순 질의는 파서 auto 여도 LLM Query Plan 을 건너뛰고 rules 플랜으로 직행한다(비용/조건소실 방지).
    plan = g.build_query_plan("서울 거주 30대 여성", parser="auto")
    assert plan["parser"]["type"] == "rules"
    assert plan["parser"].get("skip_reason") == "simple_query_direct"


# ── Tool Calling 구조화 출력 ───────────────────────────────────────────
def test_query_plan_tool_schema_shape():
    tool = g._QUERY_PLAN_TOOL
    assert tool["type"] == "function"
    fn = tool["function"]
    assert fn["name"] == "submit_query_plan"
    params = fn["parameters"]
    assert params["type"] == "object"
    assert "intent" in params["properties"]
    assert "target_user" in params["properties"]
    assert set(params["required"]) == {"intent", "target_user"}
