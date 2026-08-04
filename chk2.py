import json
sch=json.load(open('docs/data/generated/schema_catalog.json',encoding='utf-8'))
print(type(sch), list(sch)[:6] if isinstance(sch,dict) else len(sch))
t=sch['tables']
print(type(t), (list(t)[:3] if isinstance(t,dict) else t[:2]))
