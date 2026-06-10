import json
with open(r'C:\Users\haliza.LUXOR\.claude\skills\static-remix\templates\templates.json', encoding='utf-8') as f:
    tpls = json.load(f)
for t in tpls:
    tid     = t.get('id','?')
    section = t.get('section','?')
    label   = t.get('label','?')
    print(f"ID={tid}  section={section!r:30s}  label={label}")
