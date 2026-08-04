import json
c=json.load(open('docs/data/runtime/semantics/audience_catalog.json',encoding='utf-8'))
srcs=c['sources']
sch=json.load(open('docs/data/generated/schema_catalog.json',encoding='utf-8'))
tabs={}
for t in (sch.get('tables') or []):
    name=t.get('table_name') or t.get('name')
    cols={ (col.get('column_name') or col.get('name')) for col in (t.get('columns') or [])}
    tabs[name]=cols
print("schema tables sample", list(tabs)[:3], "n=",len(tabs))
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
