import sys, json, os
sys.path.insert(0, 'C:/Users/haliza.LUXOR/.claude/skills/static-remix')
from ui_server import load_brand, load_templates

brand = load_brand()
active_id = brand.get('active_product_id')
print("=== ACTIVE PRODUCT ===")
print("active_product_id:", active_id)
print()

for p in brand.get('products', []):
    photos = [x for x in p.get('photos', []) if os.path.exists(x)]
    name = p.get('name','?')
    pid  = p.get('id','?')
    marker = " <-- ACTIVE" if str(pid) == str(active_id) else ""
    print(f"Product: {name}  id={pid}{marker}")
    print(f"  Photos found: {len(photos)}")
    for ph in photos:
        size = os.path.getsize(ph)
        print(f"  {os.path.basename(ph)}  {size} bytes")
    if not photos:
        print("  *** NO PHOTOS ***")
    print()

print("=== TEMPLATES ===")
tpls = load_templates()
for t in tpls:
    has_style = bool(t.get('extracted_style'))
    path = t.get('path','')
    exists = os.path.exists(path)
    print(f"  ID={t['id']}  {t.get('label','')}  style={has_style}  file_exists={exists}")
