import json
path = r'C:\Users\haliza.LUXOR\.claude\skills\static-remix\brand_kit\brand_kit.json'
with open(path, encoding='utf-8') as f:
    brand = json.load(f)

print("All products:")
for p in brand.get('products', []):
    photos = len(p.get('photos', []))
    kb = p.get('knowledge', {})
    has_kb = any((kb.get(k) or '').strip() for k in ['product','benefits','competitors'])
    print(f"  id={p['id']}  name={p['name']}  photos={photos}  knowledge={'YES' if has_kb else 'EMPTY'}")

# Fix: point to the product with most photos
products = brand.get('products', [])
best = max(products, key=lambda p: len(p.get('photos', [])), default=None)
if best and len(best.get('photos', [])) > 0:
    brand['active_product_id'] = best['id']
    print(f"\nFixed active_product_id -> {best['id']} ({best['name']})")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(brand, f, indent=2, ensure_ascii=False)
    print("Saved.")
else:
    print("\nWARNING: No product has photos uploaded yet.")
