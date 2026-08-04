import graph_rag, audience_authority
bad = {"intent": "find_user_segment", "target_user": {}, "exclude": {},
       "campaign_constraints": {}, "audience_authority": "EVENT-IR"}
for name in ("build_purchase_history_targets_sql_candidate",
             "build_event_expression_sql_candidate",
             "compile_executable_plan",
             "compile_sql_template_candidate"):
    fn = getattr(graph_rag, name, None)
    if fn is None:
        print(name, "-> missing"); continue
    try:
        fn(dict(bad))
        print(name, "-> ok")
    except Exception as e:
        print(name, "-> RAISES", type(e).__name__)

# whitespace/case: coerce_authority casefolds+strips
print("coerce ' Event_IR ':", audience_authority.coerce_authority(" Event_IR ") if True else None)
