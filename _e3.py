import json
import graph_rag, plan_validation, audience_authority

UNPARSEABLE = {"type": "definitely_not_a_node"}

def plan_canonical():
    return {
        "intent": "find_user_segment",
        "target_user": {}, "exclude": {}, "campaign_constraints": {},
        "audience_requirement": {"expression": dict(UNPARSEABLE), "issues": []},
        "event_expression": {"expression": dict(UNPARSEABLE), "source": "audience_requirement"},
    }

q = "최근 30일 안에 구매한 회원"

# Production-shaped: nobody pre-calls the builder.
p = plan_canonical()
res = graph_rag.build_sql_result(graph_rag.nx.Graph(), q, p, [], graph_rag.DEFAULT_SCHEMA_PATH,
                                 default_limit=100, original_query=q)
print("failure_reason:", res.get("failure_reason"))
print("interpretation_status:", res.get("interpretation_status"))
print("unresolved in result:", json.dumps(res.get("unresolved_source_conditions"), ensure_ascii=False)[:400])
print("event_ir items:", [i for i in (res.get("unresolved_source_conditions") or []) if isinstance(i,dict) and i.get("source")=="event_ir"])
print("plan unresolved after:", json.dumps(p.get("unresolved_source_conditions"), ensure_ascii=False)[:300])
