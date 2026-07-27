import graph_rag as g

from query_structurer import QueryPlannerInput, build_fallback, call_query_planner


def test_adapter_passes_the_original_query_and_structured_query():
    structured_query = build_fallback("결제 시스템 담당 부서는 어디야?")
    calls = []

    def create_plan(query, *, structured_query=None):
        calls.append((query, structured_query))
        return {"query": query}

    result = call_query_planner(
        create_plan,
        QueryPlannerInput(query="결제 시스템 담당 부서는 어디야?", structured_query=structured_query),
    )

    assert result == {"query": "결제 시스템 담당 부서는 어디야?"}
    assert calls == [("결제 시스템 담당 부서는 어디야?", structured_query)]


def test_planner_prompt_and_plan_keep_the_structured_query():
    query = "결제 시스템 담당 부서는 어디야?"
    structured_query = build_fallback(query)

    prompt = g._query_plan_user_prompt(query, {}, structured_query=structured_query)
    plan = g.build_query_plan(query, parser="rules", structured_query=structured_query)

    assert "[Structured Query]" in prompt
    assert '"originalQuery": "결제 시스템 담당 부서는 어디야?"' in prompt
    assert "structuredQuery가 제공되면" in prompt
    assert plan["structured_query"] == structured_query.to_dict()


def test_structurer_failure_returns_fallback_without_blocking_the_planner():
    query = "결제 시스템 담당 부서는 어디야?"

    class FailingStructurer:
        def structure(self, input):
            raise RuntimeError("provider unavailable")

    structured_query = g._structure_query(
        query,
        g.StructuringContext(current_date="2026-07-27"),
        "gpt-4o-mini",
        FailingStructurer(),
    )
    plan = g.build_query_plan(query, parser="rules", structured_query=structured_query)

    assert structured_query.intent == "unknown"
    assert plan["structured_query"] == structured_query.to_dict()