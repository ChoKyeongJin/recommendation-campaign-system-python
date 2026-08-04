import json
c=json.load(open('docs/data/runtime/semantics/audience_catalog.json',encoding='utf-8'))
srcs=c['sources']
sch=json.load(open('docs/data/generated/schema_catalog.json',encoding='utf-8'))
tabs={}
for name,t in sch['tables'].items():
    cols=t.get('columns') or {}
    tabs[name]=set(cols) if isinstance(cols,dict) else {x.get('name') or x.get('column_name') for x in cols}
rows=[]
for name,f in c['fields'].items():
    src=f.get('source')
    has_expr = ('expression' in f) or ('search_expressions' in f)
    tbl = srcs.get(src,{}).get('table') if src in srcs else (c['subject'].get('table') if src=='subject' else None)
    col=f.get('column')
    ok = (tbl in tabs and col in tabs[tbl]) if (tbl and col) else None
    if ok is not True:
        rows.append((name,src,col,tbl,has_expr,ok))
print("NOT-OK count",len(rows))
for r in rows: print(r)
