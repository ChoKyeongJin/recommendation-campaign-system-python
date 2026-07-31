"""파서 계약 · 파이프라인 · 회귀 — 의미(극성/AND·OR/조건 존재)가 SQL 까지 보존되는지 본다.

레벨 구성
  1. 계약   : rules 파서와 LLM 픽스처가 **같은 의미 AST** 로 수렴하는지(네트워크 없이 어댑터 이후 전 구간).
  2. 파이프 : plan → 의미 AST → SQL → SQL 역해석 → 의미 대조.
  3. 회귀   : 부정 표현·나열 제외·OR 보존·포함/제외 충돌의 실제 프롬프트 케이스.

LLM 경로는 실제 호출 없이 ``query_plan_v2`` 로 구조화 후보를 주입해 검증한다 — 어댑터
(``_coerce_llm_query_plan_candidate``) 이후 경로는 rules 와 완전히 같은 코드다.
"""

from __future__ import annotations

import networkx as nx
import pytest

import graph_rag
import plan_semantic_ast
from semantic_ast import Not, Or, Predicate, describe, signed_predicates


@pytest.fixture(autouse=True)
def _offline_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 보완 계층을 끈다 — 이 테스트는 결정론 경로의 의미 보존을 고정한다."""
    monkeypatch.setattr(graph_rag, "_apply_llm_condition_slot_fallback", lambda *_a, **_kw: None)
    monkeypatch.setenv("TARGET_OBJECT_LLM_FALLBACK", "false")
    monkeypatch.setenv("SURFACE_LEXICON_LLM", "off")
    monkeypatch.setenv("CONDITION_SLOT_LLM_FALLBACK", "off")


def _plan(query: str, llm_candidate: dict | None = None) -> dict:
    return graph_rag.build_query_plan(query, parser="rules", query_plan_v2=llm_candidate)


def _expr(plan: dict):
    return graph_rag._plan_semantic_expr(plan)


def _sql_result(query: str, plan: dict) -> dict:
    return graph_rag.build_sql_result(
        nx.Graph(), query, plan, [], graph_rag.DEFAULT_SCHEMA_PATH, 1000, original_query=query
    )


def _dimension_predicates(plan: dict, column: str) -> list:
    return [
        signed
        for signed in signed_predicates(_expr(plan))
        if signed.predicate.dimension.upper() == column
    ]


# ── 1. 부정 극성: 구어체 제외 표현은 모두 같은 NOT 로 정규화된다 ────────────────


@pytest.mark.parametrize(
    "query",
    [
        "서울 빼줘",
        "서울은 빼주고",
        "서울 제외해줘",
        "서울 말고",
        "서울 빼고 고객 추출해줘",
        "서울 제외한 고객 목록 뽑아줘",
        "서울은 빼주고 여성 고객 뽑아줘",
    ],
)
def test_exclusion_surface_forms_normalize_to_negative_predicate(query: str) -> None:
    signed = _dimension_predicates(_plan(query), "SIDO")
    assert [item.polarity for item in signed] == ["negative"]
    assert signed[0].predicate.values == ("서울",)


@pytest.mark.parametrize(
    "query",
    ["서울 빼고 고객 추출해줘", "서울 제외한 고객 목록 뽑아줘", "서울은 빼주고 여성 고객 뽑아줘"],
)
def test_exclusion_compiles_to_not_in(query: str) -> None:
    result = _sql_result(query, _plan(query))
    assert result["is_success"] is True
    assert "SIDO NOT IN ('서울')" in result["sql"]


@pytest.mark.parametrize("query", ["서울 빼줘", "서울은 빼주고", "서울 제외해줘", "서울 말고"])
def test_bare_exclusion_fragment_compiles_to_not_in(query: str) -> None:
    """조회 동사 없이 제외만 말한 조각도 NOT IN 으로 컴파일된다.

    intent 승격(``_promote_unknown_intent_for_target_signal``)은 API 파이프라인 단계가 소유하므로
    여기서도 같은 순서로 부른다 — build_query_plan 만으로는 intent 가 unknown 이라 빌더가 돌지 않는다."""
    plan = _plan(query)
    graph_rag._promote_unknown_intent_for_target_signal(plan)
    result = _sql_result(query, plan)
    assert "SIDO NOT IN ('서울')" in (result["sql"] or "")


def test_listed_values_are_not_lost_before_the_exclusion_cue() -> None:
    """'서울하고 부산 빼고' 의 첫 값이 조용히 사라지지 않는다(값 경계 검사가 접속 조사를 인정)."""
    result = _sql_result("서울하고 부산 빼고 고객 뽑아줘", _plan("서울하고 부산 빼고 고객 뽑아줘"))
    assert "SIDO NOT IN ('서울', '부산')" in result["sql"]


def test_same_column_or_stays_a_single_in_list() -> None:
    query = "서울 또는 부산 고객 뽑아줘"
    result = _sql_result(query, _plan(query))
    assert "SIDO IN ('서울', '부산')" in result["sql"]


# ── 2. 파서 계약: rules 와 LLM 픽스처가 같은 의미 AST 로 수렴한다 ──────────────


LLM_FIXTURES: list[tuple[str, dict]] = [
    (
        "서울은 빼주고 VIP 고객만 뽑아줘",
        {
            "intent": "find_user_segment",
            "target_user": {"lifecycle": ["vip"]},
            "exclude": {},
            "campaign_constraints": {},
            "retrieval": {"query": "서울은 빼주고 VIP 고객만 뽑아줘", "terms": ["vip"]},
        },
    ),
    (
        "서울 빼고 여성 고객 뽑아줘",
        {
            "intent": "find_user_segment",
            "target_user": {"gender": "female"},
            "exclude": {},
            "campaign_constraints": {},
            "retrieval": {"query": "서울 빼고 여성 고객 뽑아줘", "terms": ["female"]},
        },
    ),
]


@pytest.mark.parametrize(("query", "fixture"), LLM_FIXTURES, ids=["vip", "female"])
def test_rules_and_llm_fixture_reach_the_same_semantic_ast(query: str, fixture: dict) -> None:
    assert describe(_expr(_plan(query))) == describe(_expr(_plan(query, fixture)))


def test_rule_owned_llm_exclusion_is_replaced_by_source_grounded_negative_predicate() -> None:
    """Broad LLM 제외값은 버리고 원문 파서가 확인한 값만 부정 노드가 된다."""
    query = "남성 빼고"
    rules = graph_rag._build_rule_query_plan(query)
    candidate = {
        "intent": "find_user_segment",
        "target_user": {},
        "exclude": {"gender": ["male"]},
        "campaign_constraints": {},
        "retrieval": {"query": query, "terms": []},
    }
    llm = graph_rag._coerce_llm_query_plan_candidate(
        candidate,
        rules,
        source_query=query,
    )
    plan = graph_rag.plan_resolver.resolve_plan_candidates([
        graph_rag.plan_resolver.PlanCandidate("rules", rules, priority=300),
        graph_rag.plan_resolver.PlanCandidate(
            "llm_query_structurer", llm, priority=100
        ),
    ])
    signed = [item for item in signed_predicates(_expr(plan)) if item.predicate.dimension == "gender"]
    assert [(item.polarity, item.predicate.values) for item in signed] == [("negative", ("male",))]


def test_llm_fixture_schema_is_the_shared_plan_schema() -> None:
    """어댑터 출력은 rules 와 같은 슬롯 스키마다 — 이후 경로가 파서별로 갈라지지 않는다."""
    query = "남성 빼고"
    rules = graph_rag._build_rule_query_plan(query)
    candidate = {
        "intent": "find_user_segment",
        "target_user": {"lifecycle": ["vip"]},
        "exclude": {"gender": ["male"]},
        "campaign_constraints": {},
        "retrieval": {"query": query, "terms": []},
    }
    llm = graph_rag._coerce_llm_query_plan_candidate(
        candidate,
        rules,
        source_query=query,
    )
    plan = graph_rag.plan_resolver.resolve_plan_candidates([
        graph_rag.plan_resolver.PlanCandidate("rules", rules, priority=300),
        graph_rag.plan_resolver.PlanCandidate(
            "llm_query_structurer", llm, priority=100
        ),
    ])
    assert set(plan["target_user"]) <= set(_plan("VIP 고객 뽑아줘")["target_user"])
    assert plan["exclude"]["gender"] == ["male"]


# ── 3. 파이프라인: SQL 역해석 대조 ────────────────────────────────────────────


def _compiled(query: str) -> tuple[dict, str]:
    plan = _plan(query)
    result = _sql_result(query, plan)
    assert result["sql"], result.get("failure_reason")
    return plan, result["sql"]


def test_compiled_sql_passes_semantic_reverse_check() -> None:
    plan, sql = _compiled("서울은 빼주고 여성 고객 뽑아줘")
    assert graph_rag._verify_compiled_sql_semantics(plan, sql, None) == []


def test_polarity_flip_in_sql_is_detected_and_blocks_output() -> None:
    plan, sql = _compiled("서울은 빼주고 여성 고객 뽑아줘")
    flipped = sql.replace("NOT IN ('서울')", "IN ('서울')")
    issues = graph_rag._verify_compiled_sql_semantics(plan, flipped, None)
    assert [issue["code"] for issue in issues] == ["POLARITY_MISMATCH"]
    invariants = graph_rag._verify_sql_semantic_invariants("서울은 빼주고 여성 고객 뽑아줘", plan, flipped, [])
    assert invariants["ok"] is False


def test_equivalent_not_form_is_accepted() -> None:
    """``NOT (SIDO IN (...))`` 은 ``SIDO NOT IN (...)`` 과 같은 의미다(문자열 비교가 아니다)."""
    plan, sql = _compiled("서울은 빼주고 여성 고객 뽑아줘")
    rewritten = sql.replace("B.SIDO NOT IN ('서울')", "NOT (B.SIDO IN ('서울'))")
    assert graph_rag._verify_compiled_sql_semantics(plan, rewritten, None) == []


def test_dropped_condition_in_sql_is_detected() -> None:
    plan, sql = _compiled("서울은 빼주고 여성 고객 뽑아줘")
    without_region = sql.replace("\n  AND B.SIDO NOT IN ('서울')", "")
    issues = graph_rag._verify_compiled_sql_semantics(plan, without_region, None)
    assert [issue["code"] for issue in issues] == ["MISSING_CONDITION"]


def test_reverse_check_accepts_reordered_conjuncts() -> None:
    """A AND B 와 B AND A 는 같은 의미 — 순서 차이로 실패하지 않는다."""
    plan, sql = _compiled("서울은 빼주고 여성 고객 뽑아줘")
    reordered = (
        sql.replace("WHERE B.GENDER_CD = 'GENDER_CD.FEMALE'\n  AND B.SIDO NOT IN ('서울')",
                    "WHERE B.SIDO NOT IN ('서울')\n  AND B.GENDER_CD = 'GENDER_CD.FEMALE'")
    )
    assert reordered != sql
    assert graph_rag._verify_compiled_sql_semantics(plan, reordered, None) == []


def test_or_to_and_reduction_is_detected_by_reverse_check() -> None:
    """서로 다른 컬럼의 OR 이 AND 로 컴파일되면 구조 불일치로 잡힌다."""
    expr = Or((
        Predicate("member", "SIDO", "in", ("서울",)),
        Predicate("member", "EMART_GRADE_CD", "in", ("MEM_GRADE_CD.VIP",)),
    ))
    sql = (
        "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B "
        "WHERE B.SIDO IN ('서울') AND B.EMART_GRADE_CD IN ('MEM_GRADE_CD.VIP')"
    )
    result = plan_semantic_ast.verify_compiled_sql(expr, sql, columns={"SIDO", "EMART_GRADE_CD"})
    assert result.status == "unsafe"
    assert "LOGICAL_OPERATOR_MISMATCH" in [issue.code for issue in result.issues]


# ── 4. OR 보존: 소유 슬롯이 다른 합집합은 축소하지 않는다 ─────────────────────


def test_cross_slot_or_is_not_silently_reduced_to_and() -> None:
    query = "VIP 또는 서울 거주 고객 뽑아줘"
    plan = _plan(query)
    expressions = plan.get("set_expressions") or []
    assert expressions, "합집합 구조가 통째로 사라지면 OR 이 AND 로 조용히 바뀐다"
    assert expressions[0]["set_ast"]["op"] == "+"

    result = _sql_result(query, plan)
    assert result["sql"] is None  # 부분 조건만 실행하지 않는다
    assert result["is_success"] is False
    assert result["clarification_questions"]


def test_or_branch_condition_is_not_dropped_from_the_expression() -> None:
    plan = _plan("VIP 또는 서울 거주 고객 뽑아줘")
    ast = plan["set_expressions"][0]["set_ast"]
    assert ast["left"]["canonical"] == "vip"
    assert ast["right"]["type"] in ("operand", "unknown_operand")


# ── 5. 포함/제외 충돌 ─────────────────────────────────────────────────────────


def test_full_conflict_blocks_sql_and_names_the_overlap() -> None:
    query = "남성 고객은 포함하고 남성 고객은 제외해줘 리스트 뽑아줘"
    plan = _plan(query)
    issues = graph_rag._verify_plan_semantic_conflicts(plan)
    assert [issue["code"] for issue in issues] == ["FULL_CONFLICT"]
    assert issues[0]["metadata"]["overlap"] == ["male"]
    assert _sql_result(query, plan)["sql"] is None


def test_partial_conflict_reports_only_the_overlapping_value() -> None:
    query = "서울하고 부산은 포함하고 서울은 제외해서 고객 뽑아줘"
    plan = _plan(query)
    result = _sql_result(query, plan)
    assert result["failure_reason"] == "semantic_condition_conflict"
    error = result["validation_errors"][0]
    assert error["code"] == "PARTIAL_CONFLICT"
    assert error["metadata"]["overlap"] == ["서울"]
    assert sorted(error["metadata"]["included"]) == ["부산", "서울"]


def test_conditions_on_different_owners_do_not_conflict() -> None:
    """같은 값·반대 극성이라도 owner 가 다르면 충돌이 아니다."""
    plan = {
        "intent": "find_user_segment",
        "target_user": {"gender": "male"},
        "exclude": {"owner": "account_manager", "gender": ["male"]},
    }
    assert graph_rag._verify_plan_semantic_conflicts(plan) == []


def test_non_overlapping_include_and_exclude_is_allowed() -> None:
    query = "서울 빼고 부산 고객 뽑아줘"
    plan = _plan(query)
    assert graph_rag._verify_plan_semantic_conflicts(plan) == []


# ── 6. 투영 규칙(불변식) ──────────────────────────────────────────────────────


def test_not_in_filter_projects_to_a_negated_predicate_node() -> None:
    plan = {
        "dimension_filters": [
            {
                "table": "CRM_MB_BASEINFO", "column": "CRM_MB_BASEINFO.SIDO",
                "operator": "NOT_IN", "codes": ["서울"],
            }
        ]
    }
    expr = plan_semantic_ast.plan_to_semantic_expr(plan)
    assert isinstance(expr, Predicate) and expr.operator == "not_in"


def test_unknown_set_operand_is_preserved_as_unknown_node() -> None:
    plan = {
        "set_expressions": [
            {
                "set_ast": {
                    "type": "set_op", "op": "+",
                    "left": {"type": "operand", "canonical": "vip"},
                    "right": {"type": "unknown_operand", "text": "서울 거주 고객"},
                }
            }
        ]
    }
    result = plan_semantic_ast.verify_plan_semantics(plan)
    assert result.status == "needs_clarification"
    assert isinstance(result.expr, Or) and len(result.expr.children) == 2


def test_set_difference_projects_to_and_not() -> None:
    plan = {
        "set_expressions": [
            {
                "set_ast": {
                    "type": "set_op", "op": "-",
                    "left": {"type": "operand", "canonical": "vip"},
                    "right": {"type": "operand", "canonical": "male"},
                }
            }
        ]
    }
    expr = plan_semantic_ast.plan_to_semantic_expr(plan, value_dimensions={"vip": "lifecycle", "male": "gender"})
    assert describe(expr) == "(member.lifecycle eq(vip) AND member.gender neq(male))"
    assert not any(isinstance(node, Not) for node in expr.children)  # 부정은 연산자로 흡수된다


def test_debug_payload_carries_source_plan_and_normalized_expr() -> None:
    query = "서울은 빼주고 여성 고객 뽑아줘"
    plan = _plan(query)
    info = plan_semantic_ast.semantic_debug_info(
        input_text=query,
        parser="rules",
        plan=plan,
        expr=_expr(plan),
        result=plan_semantic_ast.verify_plan_semantics(plan),
        compiled_sql="SELECT 1",
    )
    assert info["parser"] == "rules"
    assert info["raw_plan"]["dimension_filters"]
    assert "SIDO not_in" in info["normalized_summary"]
    assert info["status"] == "valid"
