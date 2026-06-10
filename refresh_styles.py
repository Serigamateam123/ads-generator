"""Clear cached extracted_style so all templates get re-extracted with the new richer schema."""
import json
path = r'C:\Users\haliza.LUXOR\.claude\skills\static-remix\templates\templates.json'
with open(path, encoding='utf-8') as f:
    tpls = json.load(f)
cleared = 0
for t in tpls:
    if 'extracted_style' in t:
        del t['extracted_style']
        cleared += 1
with open(path, 'w', encoding='utf-8') as f:
    json.dump(tpls, f, indent=2, ensure_ascii=False)
print(f"Cleared {cleared} cached styles. Next template selection will trigger fresh extraction.")
