import json, graph_rag
plan = {"intent": "find_user_segment", "target_user": {}, "exclude": {},
        "campaign_constraints": {}, "audience_authority": "evnet_ir"}
res = graph_rag._audience_authority_blocking_sql_result(plan)
try:
    api = graph_rag.build_recommendation_api_response("q", plan, res, {"content": None})
    print("status:", api.get("status"))
    print("message:", api.get("message"))
    print("failure_reason:", api.get("failure_reason"))
    print("interpretation_status:", api.get("interpretation_status"))
except Exception as e:
    import traceback; traceback.print_exc()
