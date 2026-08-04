import traceback, json
import graph_rag, audience_authority

bad = {
    "intent": "recommend_campaign",
    "audience_authority": "evnet_ir",
    "audience_requirement": {"issues": [], "expression": {}},
    "output_contract": {"expected_grain": "member"},
}

# gate itself
r = graph_rag._audience_authority_blocking_sql_result(bad)
print("gate ->", None if r is None else r.get("failure_reason"), None if r is None else r.get("interpretation_status"))

# structuring-stage helper: does the typo still raise there?
try:
    graph_rag._grounded_canonical_event_ir_repair(
        bad, query="x", context=None, graph=None, collection="c", url="u",
        api_key=None, embedding_model_name="m", vector_top_k=1, keyword_top_k=1,
        graph_top_k=1, hops=1, llm_model="gpt", query_structurer=None)
except Exception as e:
    print("repair raised:", type(e).__name__, e)
else:
    print("repair returned ok")

# plan_validation
import plan_validation
try:
    v = plan_validation.validate_executable_plan(bad)
    print("plan_validation ->", v.status)
except Exception as e:
    print("plan_validation raised:", type(e).__name__, e)

# does a structurer keep a model-supplied bogus authority?
from query_structurer import validate_campaign_query_plan_v4
cand = {
    "intent": "recommend_campaign",
    "audience_authority": "evnet_ir",
    "raw_query": "테스트",
}
try:
    out = validate_campaign_query_plan_v4(cand, query="테스트", raw_query="테스트")
    print("validated keeps authority?:", out.get("audience_authority"))
except Exception as e:
    print("validate_campaign_query_plan_v4 raised:", type(e).__name__, e)
