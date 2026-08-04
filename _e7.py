from query_structurer import validate_campaign_query_plan_v4
import graph_rag, json
cand = {
    "schema_version": "4.0",
    "intent": "find_user_segment",
    "audience_authority": "evnet_ir",
    "raw_query": "테스트",
    "original_query": "테스트",
}
try:
    out = validate_campaign_query_plan_v4(cand, query="테스트", raw_query="테스트")
    print("kept?", out.get("audience_authority"), "keys:", "audience_authority" in out)
except Exception as e:
    print("raised:", type(e).__name__, e)

# what about the coercion used for generic LLM json
try:
    p = graph_rag._coerce_llm_query_plan_candidate(cand, {"intent":"find_user_segment"})
    print("coerce keeps:", p.get("audience_authority"))
except Exception as e:
    print("coerce raised:", type(e).__name__, e)
