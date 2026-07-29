"""A failed vision classification must NOT be cached — otherwise one transient
blip permanently mislabels a pour/lifestyle photo as a plain bottle, which is
what broke auto photo-role assignment."""
import json, os, shutil, sys, tempfile
from pathlib import Path

SRC = Path("/Users/nrmalisa/Applications/Ads Generator")
work = Path(tempfile.mkdtemp(prefix="adsgen-classify-"))
shutil.copy2(SRC / "ui_server.py", work / "ui_server.py")
(work / "fonts").mkdir(exist_ok=True)
for f in (SRC / "fonts").glob("*"):
    shutil.copy2(f, work / "fonts" / f.name)
sys.path.insert(0, str(work))
os.chdir(work)
import ui_server as U

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)

# a tiny real image so os.path.exists + img_to_b64 are happy
from PIL import Image
img = work / "p.png"
Image.new("RGB", (10, 10), (200, 180, 90)).save(img)

print("\n=== failed vision call is not cached ===")
U._vision_call = lambda *a, **k: ""          # simulate rate-limit / keyless failure
r1 = U.classify_product_photo(str(img))
check("returns neutral default on failure", r1 == "bottle_alone", r1)
cache = U.load_photo_styles() if U.PHOTO_STYLE_JSON.exists() else {}
check("failure NOT written to cache", str(img) not in cache, str(cache))

print("\n=== a real answer IS cached and buckets correctly ===")
U._vision_call = lambda *a, **k: "poured into a glass"
r2 = U.classify_product_photo(str(img))
check("real answer bucketed", r2 == "poured_glass", r2)
cache = U.load_photo_styles()
check("real answer cached", cache.get(str(img)) == "poured_glass", str(cache))

print("\n=== once cached, a later failure does not override the good value ===")
U._vision_call = lambda *a, **k: ""
r3 = U.classify_product_photo(str(img))
check("cached good value survives a later blip", r3 == "poured_glass", r3)

print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
