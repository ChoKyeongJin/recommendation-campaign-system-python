import json, copy
import graph_rag, plan_validation, audience_authority, event_ir

def mkplan():
    return {
        "intent": "recommend_campaign",
        "audience_authority": "event_ir",
        "event_expression": {"source": "audience_requirement", "expression": {"garbage": 1}},
        "output_contract": {"expected_grain": "member"},
    }

p = mkplan()
print("requires_event_ir:", audience_authority.requires_event_ir(p))
print("_plan_event_expression:", graph_rag._plan_event_expression(p))
v = plan_validation.validate_executable_plan(p)
print("validate status:", v.status, [i.code for i in v.issues])

# path A: production compile facade
pa = mkplan()
out = graph_rag.compile_executable_plan(pa)
print("A compile_executable_plan ->", out)
print("A unresolved:", json.dumps(pa.get("unresolved_source_conditions"), ensure_ascii=False))

# path B: direct public builder call
pb = mkplan()
out = graph_rag.build_event_expression_sql_candidate(pb)
print("B direct builder ->", out)
print("B unresolved:", json.dumps(pb.get("unresolved_source_conditions"), ensure_ascii=False))
vb = plan_validation.validate_executable_plan(pb)
print("B validate after:", vb.status, [i.code for i in vb.issues])

# path C: second call after B mutated the plan -> other builders
out2 = graph_rag.build_purchase_history_targets_sql_candidate(pb)
print("C other builder after mutation ->", out2)
out3 = graph_rag.build_event_expression_sql_candidate(pb)
print("C event builder 2nd ->", out3)
print("C unresolved len:", len(pb.get("unresolved_source_conditions") or []))
