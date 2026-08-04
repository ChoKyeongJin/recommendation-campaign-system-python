from query_structurer import validate_campaign_query_plan_v4
cand = {
    "schema_version": "4.0",
    "intent": "find_user_segment",
    "audience_authority": "evnet_ir",
    "raw_query": "테스트", "original_query": "테스트",
    "planning_query": "테스트", "normalized_query": "테스트",
}
try:
    out = validate_campaign_query_plan_v4(cand, query="테스트", raw_query="테스트")
    print("kept?", repr(out.get("audience_authority")), "present:", "audience_authority" in out)
except Exception as e:
    print("raised:", type(e).__name__, e)
