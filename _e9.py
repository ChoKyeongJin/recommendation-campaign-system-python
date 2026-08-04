import graph_rag, plan_validation
UNP={"type":"nope"}
p={"intent":"find_user_segment","target_user":{},"exclude":{},"campaign_constraints":{},
   "event_expression":{"expression":dict(UNP),"source":"audience_requirement"}}
graph_rag.build_event_expression_sql_candidate(p)
v=plan_validation.validate_executable_plan(p)
r=graph_rag._plan_validation_blocking_sql_result(v,p)
print("issue codes:", [i.code for i in v.issues])
print("clarifications:", r["clarification_questions"])
print("missing paths:", [m["path"] for m in r["missing_input_conditions"]])
