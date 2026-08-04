import json
import graph_rag, plan_validation

UNPARSEABLE = {"type": "definitely_not_a_node"}
def p_():
    return {"intent": "find_user_segment", "target_user": {}, "exclude": {},
            "campaign_constraints": {},
            "event_expression": {"expression": dict(UNPARSEABLE), "source": "audience_requirement"}}
q = "최근 30일 안에 구매한 회원"

p = p_()
graph_rag.build_event_expression_sql_candidate(p)
print("after builder:", [i.get("source") for i in p["unresolved_source_conditions"]])

# which gate fires first?
print("semantic_ir_block:", graph_rag._semantic_ir_blocking_sql_result(p) is not None)
v = plan_validation.validate_executable_plan(p)
print("validate:", v.status, [i.code for i in v.issues])

res = graph_rag.build_sql_result(graph_rag.nx.Graph(), q, p, [], graph_rag.DEFAULT_SCHEMA_PATH,
                                 default_limit=100, original_query=q)
print("failure_reason:", res.get("failure_reason"))
print("keys sample:", sorted(res.keys()))
print("event_ir items:", [i for i in (res.get("unresolved_source_conditions") or []) if i.get("source")=="event_ir"])

# does a refresh wipe the coordinate?
p2 = p_()
graph_rag.build_event_expression_sql_candidate(p2)
before = len(p2["unresolved_source_conditions"])
merged = graph_rag._refresh_unresolved_source_conditions(q, p2)
print("refresh: before", before, "after", [i.get("source") for i in merged])
