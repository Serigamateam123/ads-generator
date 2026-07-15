"""Generate images via Gemini 3 Pro Image Preview."""
import os, sys, json, base64, urllib.request, urllib.error

API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not API_KEY:
    print("ERROR: GEMINI_API_KEY not set"); sys.exit(1)

RUN_DIR = r"C:\Users\haliza.LUXOR\.claude\skills\static-remix\runs\20260529-1006"
PRODUCT_PHOTO = os.path.join(RUN_DIR, "product_photo.png")
PRODUCTION_DIR = os.path.join(RUN_DIR, "production")
os.makedirs(PRODUCTION_DIR, exist_ok=True)

API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image-preview:generateContent?key={API_KEY}"

def load_image_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

def generate_image(prompt: str, aspect_ratio: str, output_path: str, ref_image_path: str = None):
    parts = [{"text": prompt}]
    if ref_image_path and os.path.exists(ref_image_path):
        b64 = load_image_b64(ref_image_path)
        ext = os.path.splitext(ref_image_path)[1].lower().strip(".")
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        print(f"  Reference image attached: {os.path.basename(ref_image_path)}")

    payload = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": aspect_ratio}
        }
    }).encode("utf-8")

    req = urllib.request.Request(API_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"  HTTP {e.code}: {err[:500]}")
        return False

    # Extract image from response
    for part in body.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            img_data = base64.b64decode(part["inlineData"]["data"])
            with open(output_path, "wb") as f:
                f.write(img_data)
            print(f"  Saved: {output_path}")
            return True
        if "inline_data" in part:
            img_data = base64.b64decode(part["inline_data"]["data"])
            with open(output_path, "wb") as f:
                f.write(img_data)
            print(f"  Saved: {output_path}")
            return True

    print(f"  No image in response. Keys: {list(body.keys())}")
    # Print text parts for debugging
    for part in body.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "text" in part:
            print(f"  Model said: {part['text'][:300]}")
    return False

CONCEPTS = [
    {
        "id": "concept_01_var_01",
        "aspect": "9:16",
        "prompt": (
            "Create a 9:16 portrait static ad image for Serigama Luxor Jeli Gamat 550ml, a Malaysian sea cucumber "
            "wellness supplement. Framework: US VS THEM comparison.\n\n"
            "BACKGROUND: Warm beige/cream gradient (#D4B896), clean and minimal.\n\n"
            "LAYOUT: Two-column split. Left column has a plain white unlabelled supplement bottle (slightly grey/blurred). "
            "Right column has the Serigama Luxor bottle — squat HDPE bottle, translucent body, white ribbed cap, "
            "bright golden-yellow label with cream leaf motif, 'Serigama' in dark cursive at top, 'LUXOR' in red caps, "
            "'Jeli Gamat' in large warm orange script. Right column background has a soft warm gold glow.\n\n"
            "TOP CENTER LABEL (small caps, dark): 'US VS THEM'\n\n"
            "LEFT COLUMN HEADER (bold, dark grey): 'SUPPLEMENT BIASA'\n"
            "RIGHT COLUMN HEADER (bold, brand orange #C0392B): 'SERIGAMA LUXOR'\n\n"
            "COMPARISON BULLETS — left side red ✗, right side green ✓:\n"
            "✗ Tiada sijil HALAL JAKIM | ✓ Halal JAKIM Certified\n"
            "✗ Kandungan tersembunyi | ✓ 3.48g gamat per 10ml\n"
            "✗ Tiada kelulusan KKM | ✓ KKM Approved\n"
            "✗ Kilang tidak berlesen GMP | ✓ GMP-Certified Facility\n"
            "✗ Botol kecil 350ml | ✓ 550ml — 50% Lebih Banyak\n\n"
            "BOTTOM BLACK BAR spanning full width:\n"
            "Line 1 (white bold): '20 Tahun Dipercayai. Lebih 10 Juta Botol Terjual.'\n"
            "Line 2 (warm amber): 'RM88 untuk 550ml — Lebih Gamat, Lebih Berbaloi.'\n\n"
            "Typography: clean modern sans-serif (Montserrat style). "
            "Serigama bottle is the visual hero — sharp, well-lit, golden label prominent. "
            "Overall feel: credibility, transparency, premium but warm."
        )
    },
    {
        "id": "concept_01_var_02",
        "aspect": "9:16",
        "prompt": (
            "Create a 9:16 portrait static ad image for Serigama Luxor Jeli Gamat 550ml, a Malaysian sea cucumber "
            "wellness supplement. Framework: REVERSE PSYCHOLOGY / DON'T BUY THIS.\n\n"
            "BACKGROUND: Deep charcoal/navy (#1A1A2E) — high contrast, scroll-stopping.\n\n"
            "TOP 50% OF IMAGE: Massive bold white all-caps stacked text, left-aligned, taking up almost the full width:\n"
            "   JANGAN\n   BELI\n   INI\n"
            "Font: ExtraBold, heavy weight (Montserrat Black style). Almost like a billboard.\n\n"
            "IMMEDIATELY BELOW the big text, smaller italic warm amber text:\n"
            "'...melainkan kau nak sembuh dalam 30 hari.'\n\n"
            "BOTTOM 40% OF IMAGE: The Serigama Luxor 550ml bottle, centered, clean product shot on dark background. "
            "The golden-yellow label glows warmly. Bottle is sharp and well-lit.\n\n"
            "Below the bottle, three tight proof badges in small green text:\n"
            "✓ Halal JAKIM   ✓ KKM Approved   ✓ GMP Certified\n\n"
            "Star rating line below: ★★★★★ 4.8 dari 3,000+ ulasan\n\n"
            "BOTTOM STRIP: Full-width golden-yellow bar (#F5A800) with dark text:\n"
            "'RM88 / 550ml — Free Shipping atas RM70'\n\n"
            "Overall feel: bold, challenger brand energy. The 'JANGAN BELI INI' headline is the dominant visual element. "
            "The bottle reveal at the bottom is the payoff. "
            "Serigama bottle: translucent HDPE, white ribbed cap, bright golden-yellow label, 'Jeli Gamat' in orange script."
        )
    }
]

failures = []
for c in CONCEPTS:
    print(f"\nGenerating {c['id']} ...")
    out = os.path.join(PRODUCTION_DIR, f"{c['id']}.png")
    ok = generate_image(c["prompt"], c["aspect"], out, PRODUCT_PHOTO)
    if not ok:
        failures.append(c["id"])

if failures:
    print(f"\nRETRYING failures: {failures}")
    for cid in failures[:]:
        c = next(x for x in CONCEPTS if x["id"] == cid)
        out = os.path.join(PRODUCTION_DIR, f"{c['id']}.png")
        ok = generate_image(c["prompt"], c["aspect"], out, PRODUCT_PHOTO)
        if ok:
            failures.remove(cid)

print(f"\nDone. Failures: {failures if failures else 'none'}")
