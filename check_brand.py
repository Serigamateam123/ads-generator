import json
with open(r'C:\Users\haliza.LUXOR\.claude\skills\static-remix\brand_kit\brand_kit.json', encoding='utf-8') as f:
    brand = json.load(f)

print("active_product_id:", brand.get('active_product_id'))
print()
for p in brand.get('products', []):
    kb = p.get('knowledge', {})
    name    = p.get('name', '?')
    sku     = p.get('sku', '')
    price   = p.get('price', '')
    photos  = len(p.get('photos', []))
    prod_kb = len((kb.get('product') or '').strip())
    ben_kb  = len((kb.get('benefits') or '').strip())
    comp_kb = len((kb.get('competitors') or '').strip())
    pid     = p.get('id', '?')
    print(f"  ID={pid}")
    print(f"  Name={name}  SKU={sku}  Price={price}")
    print(f"  Photos={photos}  product_kb={prod_kb}chars  benefits={ben_kb}chars  competitors={comp_kb}chars")
    print()
