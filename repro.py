import io, json, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import graph_rag

for q in ["2026년 3월에 같은 상품을 동시 구매한 고객수", "2026년 3월에 동일 상품을 동시 구매한 고객 수"]:
    print("=" * 60)
    print(q)
    print("detect:", graph_rag.detects_same_product_co_purchase(q), "count:", graph_rag.requests_member_count(q))
    plan = graph_rag.build_query_plan(q, parser="rules")
    print("intent:", plan.get("intent"), "route:", plan.get("selected_route"))
    print("has_evals:", bool(plan.get("condition_evaluations")))
    print("unresolved:", json.dumps(plan.get("unresolved_source_conditions"), ensure_ascii=False)[:400])
    cand = graph_rag.build_sql_template_candidate(plan)
    print("cand:", None if cand is None else cand.get("id"))
    if cand:
        print(cand["sql"])
