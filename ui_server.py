"""
static-remix UI server
Run: python ui_server.py
Opens at: http://localhost:7373
"""
import base64, json, os, uuid, urllib.request, urllib.error, shutil, re, textwrap
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory

# ── Font paths ────────────────────────────────────────────────────────────────
FONTS_DIR       = Path(__file__).parent / "fonts"
FONT_H1         = str(FONTS_DIR / "Montserrat-Bold.ttf")        # Bold for H1 headlines
FONT_BODY       = str(FONTS_DIR / "Poppins-SemiBold.ttf")       # for bullets / subheads
FONT_REGULAR    = str(FONTS_DIR / "Poppins-Regular.ttf")
FONT_BOLD       = str(FONTS_DIR / "Poppins-Bold.ttf")
FONT_FALLBACK   = "C:/Windows/Fonts/arialbd.ttf"                # always available on Windows

PORT         = 7373
MYR_RATE     = 4.07
COSTS        = {"draft": 0.01, "final": 0.04}
DRAFT_MODEL  = "gemini-2.5-flash-image"           # cheap draft (~$0.01)
DRAFT_FALL   = "gemini-3.1-flash-image-preview"   # fallback draft
FINAL_MODEL  = "nano-banana-pro-preview"          # Nano Banana Pro — highest quality, accepts ref images
VISION_MODEL = "gemini-2.5-flash"                 # vision analysis (text only, cheap)

SKILLS_DIR    = Path(__file__).parent
SESSIONS_DIR  = SKILLS_DIR / "sessions"
BRAND_DIR     = SKILLS_DIR / "brand_kit"
BRAND_JSON    = BRAND_DIR  / "brand_kit.json"
TEMPLATES_DIR = SKILLS_DIR / "templates"
TEMPLATES_JSON= TEMPLATES_DIR / "templates.json"
for d in (SESSIONS_DIR, BRAND_DIR, TEMPLATES_DIR):
    d.mkdir(exist_ok=True)

PROBLEM_CONTEXT = {
    "Post-partum":   "new mothers recovering after childbirth — wound healing, uterus recovery, energy restoration",
    "Joint Pain":    "people with joint/knee pain or arthritis needing natural anti-inflammatory relief",
    "Accident":      "people recovering from accidents or fractures needing faster natural healing",
    "Gastric":       "people with gastric problems, stomach ulcers, or acid reflux needing gut lining repair",
    "Post-Op":       "patients after surgery needing faster wound healing and immune support",
    "Gout":          "people with gout or high uric acid needing natural inflammation reduction",
}

AD_FRAMEWORKS = [
    ("Us Vs Them",      "Two-column split. Left = generic competitor with red ✗ problems. Right = this brand with green ✓ wins. Header: 'US VS THEM'."),
    ("Bold Claim",      "One huge bold statement occupies the top 60% in large typography. Product below. Minimal. Confident."),
    ("Before & After",  "Split screen: left = person in pain (before), right = person recovered and happy (after). Product in centre."),
    ("Testimonial",     "Realistic customer review card (5 stars, short quote) overlaid on a soft lifestyle background. Product in corner."),
    ("Stat Surround",   "Large hero number (e.g. '10 JUTA+ BOTOL') surrounded by smaller proof points — certs, years, reviews."),
    ("Problem Hook",    "Relatable problem in bold text at top. Product below as the solution. Three short benefit bullets."),
    ("Ingredient Hero", "Close-up of the key ingredient (golden sea cucumber) alongside the product. Short benefit callout."),
    ("Social Proof",    "Grid of real-looking user review screenshots or star ratings with the product. Trust-builder layout."),
]

app = Flask(__name__, static_folder=None)

# ── Brand kit helpers ──────────────────────────────────────────────────────────
DEFAULT_BRAND = {
    "brand_name":      "",
    "tagline":         "",
    "brand_voice":     "",
    "primary_color":   "#F5A800",
    "secondary_color": "#D94F00",
    "accent_color":    "#FFF8E7",
    "price":           "",
    "product_url":     "",
    "key_claims":      [],
    "certifications":  [],
    "logo_path":       "",
    "product_photos":  [],
    "extra_notes":     "",
    "knowledge":       {},
    "products":        [],          # list of {id, name, sku, price, product_url, knowledge:{}}
    "active_product_id": None,
}

def load_brand() -> dict:
    if BRAND_JSON.exists():
        with open(BRAND_JSON, encoding="utf-8") as f:
            data = json.load(f)
        # Merge with defaults so new fields always present
        return {**DEFAULT_BRAND, **data}
    return dict(DEFAULT_BRAND)

def save_brand(data: dict):
    with open(BRAND_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def brand_to_prompt_block(brand: dict) -> str:
    """Convert brand kit + knowledge base into a full prompt context block."""
    lines = ["═══ BRAND & PRODUCT KNOWLEDGE ═══"]

    # ── Identity
    if brand.get("brand_name"):
        lines.append(f"\nBRAND: {brand['brand_name']}")
    if brand.get("_active_product_name"):
        lines.append(f"PRODUCT: {brand['_active_product_name']}")
    if brand.get("tagline"):
        lines.append(f"TAGLINE: {brand['tagline']}")
    # Price intentionally excluded — never shown in generated ads
    if brand.get("certifications"):
        lines.append(f"CERTIFICATIONS (show prominently): {', '.join(brand['certifications'])}")
    if brand.get("key_claims"):
        lines.append("KEY CLAIMS:")
        for c in brand["key_claims"]:
            lines.append(f"  • {c}")
    if brand.get("brand_voice"):
        lines.append(f"BRAND VOICE: {brand['brand_voice']}")

    # ── Colors
    colors = []
    if brand.get("primary_color"):   colors.append(f"Primary {brand['primary_color']}")
    if brand.get("secondary_color"): colors.append(f"Secondary {brand['secondary_color']}")
    if brand.get("accent_color"):    colors.append(f"Accent/BG {brand['accent_color']}")
    if colors: lines.append(f"BRAND COLORS: {', '.join(colors)}")

    # ── Knowledge base (the meaty part)
    kb = brand.get("knowledge", {})
    sections = [
        ("PRODUCT & INGREDIENTS", "product"),
        ("TARGET AUDIENCE",       "audience"),
        ("BENEFITS BY PROBLEM",   "benefits"),
        ("PROOF & TESTIMONIALS",  "proof"),
        ("OBJECTIONS & ANSWERS",  "objections"),
        ("VS COMPETITORS",        "competitors"),
    ]
    for title, key in sections:
        val = (kb.get(key) or "").strip()
        if val:
            lines.append(f"\n{title}:\n{val}")

    # ── Creative rules
    if brand.get("extra_notes"):
        lines.append(f"\nCREATIVE RULES (must follow):\n{brand['extra_notes']}")

    lines.append("\n═══════════════════════════════")
    return "\n".join(lines)

# ── Image helpers ──────────────────────────────────────────────────────────────
def img_to_b64(path: str):
    ext = Path(path).suffix.lower().strip(".")
    mime = {"png":"image/png","jpg":"image/jpeg","jpeg":"image/jpeg",
            "webp":"image/webp","gif":"image/gif"}.get(ext,"image/png")
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode(), mime

def call_gemini_image(prompt, ref_paths, model,
                      winning_ad_path=None, product_image_path=None):
    """
    Single-turn API call with clearly labelled images.
    Image generation models require everything in ONE user turn.
    Order: text label → image → text label → image → main prompt.
    """
    api_key = os.environ.get("GEMINI_API_KEY","")
    if not api_key:
        return None, "GEMINI_API_KEY not set"

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={api_key}")

    winning_ad  = winning_ad_path
    product_img = product_image_path

    valid_refs = [r for r in (ref_paths or []) if r and os.path.exists(str(r))]
    if not winning_ad and len(valid_refs) >= 1:
        winning_ad = valid_refs[0]
    if not product_img and len(valid_refs) >= 2:
        product_img = valid_refs[-1]

    print(f"[gemini] model={model}")
    print(f"[gemini] winning_ad={Path(winning_ad).name if winning_ad else 'NONE'}")
    print(f"[gemini] product_img={Path(product_img).name if product_img else 'NONE'}")

    # Single user turn — all parts in one message
    # Label each image WITH TEXT before the image so Gemini reads the label first
    parts = []

    if winning_ad and os.path.exists(winning_ad):
        b64, mime = img_to_b64(winning_ad)
        parts.append({"text": "=== IMAGE 1: WINNING AD — CREATIVE DIRECTION REFERENCE ==="})
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        parts.append({"text":
            "This is the WINNING AD. Treat it as a TEMPLATE to clone almost exactly — this is your primary creative reference.\n\n"
            "REPLICATE EXACTLY from IMAGE 1, pixel-for-pixel as much as possible:\n"
            "- Overall composition and layout structure (where everything sits, how much negative space, balance)\n"
            "- Background — exact type, treatment (solid, gradient, textured, lifestyle scene), and color family\n"
            "- Color palette — dominant and accent tones, exact same hues\n"
            "- Lighting, shadows, and how the product is treated (floating, glowing, grounded, on a surface, etc.)\n"
            "- Every graphic element: shapes, panels, dividers, badges, arrows, pointers, icons, decorative elements — "
            "same shapes, same positions, same sizes, same styling\n"
            "- Overall mood, energy, and visual confidence (busy vs minimal, centered vs asymmetric)\n\n"
            "ONLY change these two things, nothing else:\n"
            "1. The product → remove IMAGE 1's product completely and put IMAGE 2's product in the EXACT same "
            "position, scale, angle, and role (e.g. if IMAGE 1's product is held by a hand, IMAGE 2's product is "
            "held by that same hand in that same way; if it's standing on a surface, place IMAGE 2's product "
            "standing on that same surface)\n"
            "2. All text, words, and logos → replace with the new headline/subheadline/CTA copy provided "
            "in the prompt below, in the SAME positions, sizes, fonts, and styles as IMAGE 1's text\n\n"
            "Do NOT redesign, simplify, reinterpret, or 'inspire yourself' from IMAGE 1 — copy it as a template "
            "and swap only the product and text. The result should look like the exact same ad, with only the "
            "product and copy swapped."})

    if product_img and os.path.exists(product_img):
        b64, mime = img_to_b64(product_img)
        parts.append({"text": "=== IMAGE 2: PRODUCT — USE THIS EXACT PRODUCT ==="})
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        parts.append({"text":
            "This is the ONLY product allowed in the output.\n\n"
            "COPY exactly:\n"
            "- Product shape, silhouette, and proportions\n"
            "- Label design, colors, and packaging details\n"
            "- Surface material and finish (matte, glossy, transparent, etc.)\n\n"
            "DO NOT substitute, hallucinate, or blend with any other product."})

    parts.append({"text": "\n\n" + prompt})

    payload = json.dumps({
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": "4:5"}
        }
    }).encode()

    req = urllib.request.Request(url, data=payload,
          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:400]
        print(f"[gemini] HTTP {e.code}: {err}")
        return None, f"HTTP {e.code}: {err}"
    for part in body.get("candidates",[{}])[0].get("content",{}).get("parts",[]):
        for key in ("inlineData","inline_data"):
            if key in part:
                return base64.b64decode(part[key]["data"]), None
    return None, "No image in response"

def call_gemini_final(prompt, ref_paths):
    """Final quality — uses Nano Banana Pro which accepts reference images."""
    return call_gemini_image(prompt, ref_paths, FINAL_MODEL)

def extract_winning_ad_style(image_path: str) -> dict:
    """
    STEP 2: Run a dedicated Gemini vision call on the winning ad image.
    Returns structured JSON describing the ad's visual design style.
    If extraction fails, returns None — app continues to work without it.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not os.path.exists(image_path):
        print("[StyleExtract] Skipped — no key or image not found")
        return None

    prompt = """You are a professional ad creative analyst. Study this winning advertisement image carefully.

Return ONLY a raw JSON object — no explanation, no markdown, no code blocks. Just the JSON.

{
  "layout": {
    "structure": "describe the overall layout e.g. split left-right, single column, centered product",
    "product_placement": "exactly where the product appears on canvas",
    "headline_placement": "exactly where the headline text sits",
    "cta_placement": "where the CTA is, or 'none' if absent",
    "negative_space": "how much empty breathing room exists e.g. minimal, moderate, generous 40%+"
  },
  "colors": {
    "background": "exact hex if visible, or description",
    "background_description": "describe the background type: flat solid, gradient, sunburst, textured, photographic scene",
    "dominant_color": "hex or description of the most used color",
    "dominant_description": "what element uses this color",
    "accent_color": "hex or description of the secondary color",
    "accent_description": "what element uses this accent color",
    "text_color_headline": "hex or description of headline text color",
    "text_color_subheadline": "hex or description of subheadline text color",
    "cta_color": "hex or description of CTA element, or 'none'"
  },
  "typography": {
    "headline_weight": "bold, medium, or light",
    "headline_case": "uppercase, title case, or lowercase",
    "headline_size": "describe relative size: small, medium, large, very large",
    "font_style": "sans-serif, serif, or script",
    "headline_line_breaks": "describe how text wraps e.g. one long line, stacked short lines",
    "subheadline_weight": "bold, medium, or regular",
    "subheadline_size": "describe relative size compared to headline"
  },
  "mood": {
    "tone": "choose: clinical, warm, urgent, premium, playful, calm, trustworthy",
    "energy_level": "choose: high energy urgent, medium, low energy quiet confident",
    "lighting": "flat natural, bright studio, dark moody, gradient soft, dramatic",
    "overall_feel": "write one sentence describing the brand feel"
  },
  "composition": {
    "style": "minimalist, balanced, busy, or asymmetric",
    "has_human": true or false,
    "has_background_scene": true or false,
    "background_type": "flat solid color, gradient, real photograph, illustrated scene",
    "overlay_style": "none, gradient overlay, solid color block",
    "props": "list any supporting props e.g. glass of water, leaves, none",
    "product_style": "how the product appears: floating, standing on surface, held by hand"
  },
  "graphic_elements": [
    "list every non-text/non-product graphic element visible, with its position",
    "e.g. 'arrow pointing from headline to product, top-left to center'",
    "e.g. 'circular badge/sticker top-right corner with discount text'",
    "e.g. 'pointer/callout line connecting label to product feature'",
    "e.g. 'icon row of checkmarks/bullets along the bottom'",
    "if none, return an empty array"
  ],
  "visual_hierarchy": {
    "first_attention": "what catches the eye first",
    "second_attention": "what the eye moves to second",
    "third_attention": "third focal point",
    "fourth_attention": "fourth focal point or 'none'"
  },
  "forbidden_elements": [
    "list specific visual elements that are NOT present in this ad and should NOT appear in any recreation",
    "e.g. NO speech bubbles, NO sunburst background, NO certification badges",
    "e.g. NO gradient on background, NO warm orange tones, NO cluttered layout"
  ],
  "size": "detected or assumed canvas size e.g. 1080x1350 portrait or 1080x1080 square"
}"""

    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{VISION_MODEL}:generateContent?key={api_key}")
        b64, mime = img_to_b64(image_path)
        payload = json.dumps({"contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": b64}},
            {"text": prompt}
        ]}]}).encode()
        req = urllib.request.Request(url, data=payload,
              headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw   = next((p["text"] for p in parts if "text" in p), "")
        raw   = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw   = re.sub(r"\s*```$", "",          raw.strip())
        style = json.loads(raw)
        print(f"[StyleExtract] OK — tone={style.get('mood',{}).get('tone')} "
              f"structure={style.get('layout',{}).get('structure','')[:40]}")
        return style
    except Exception as e:
        print(f"[StyleExtract Error] {e}")
        return None


def score_generated_ad(style_json: dict, generated_image_path: str) -> dict:
    """
    STEP 5: Compare the generated ad against the extracted style brief.
    Returns {match_score, biggest_mismatch, verdict} or None on failure.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not style_json or not os.path.exists(generated_image_path):
        return None
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{VISION_MODEL}:generateContent?key={api_key}")
        b64, mime = img_to_b64(generated_image_path)
        prompt = (f"Compare these two things:\n"
                  f"1. This style brief (JSON): {json.dumps(style_json)}\n"
                  f"2. The generated ad image I am sending you\n\n"
                  f"Rate how well the generated ad matches the style brief.\n"
                  f"Return ONLY raw JSON, no explanation:\n"
                  f'{{"match_score": (number 1-10), '
                  f'"biggest_mismatch": "one sentence", '
                  f'"verdict": "good" or "regenerate"}}')
        payload = json.dumps({"contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": b64}},
            {"text": prompt}
        ]}]}).encode()
        req = urllib.request.Request(url, data=payload,
              headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw   = next((p["text"] for p in parts if "text" in p), "")
        raw   = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw   = re.sub(r"\s*```$", "",          raw.strip())
        result = json.loads(raw)
        print(f"[MatchScore] {result.get('match_score')}/10 — {result.get('verdict')}")
        return result
    except Exception as e:
        print(f"[MatchScore Error] {e}")
        return None


def _vision_call(prompt_text: str, image_path: str, timeout: int = 30) -> str:
    """Shared helper: send one image + prompt to the vision model, return text."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not os.path.exists(image_path):
        return ""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{VISION_MODEL}:generateContent?key={api_key}")
    b64, mime = img_to_b64(image_path)
    payload = json.dumps({"contents": [{"parts": [
        {"inline_data": {"mime_type": mime, "data": b64}},
        {"text": prompt_text}
    ]}]}).encode()
    req = urllib.request.Request(url, data=payload,
          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates",[{}])[0].get("content",{}).get("parts",[])
        return next((p["text"] for p in parts if "text" in p), "")
    except Exception:
        return ""

def analyze_ad_layout(image_path: str) -> str:
    """
    Extract ONLY color palette and visual mood from winning ad.
    This description goes into the Gemini prompt as TEXT — the image itself
    is NOT sent to Gemini, to prevent Gemini from copying text from the reference.
    """
    return _vision_call(
        "Describe ONLY the visual style of this ad — colors, mood, background. "
        "Do NOT describe any text, words, or typography in the image. "
        "Cover these 4 things only:\n"
        "1. BACKGROUND: exact color(s), gradient direction, texture (e.g. 'light blue gradient top-to-bottom, soft')\n"
        "2. COLOR PALETTE: list every distinct background/accent color as hex values (e.g. #E8F4FD, #2B5F8E)\n"
        "3. MOOD: one sentence describing the visual feel (e.g. 'clean, professional, medical, warm')\n"
        "4. PRODUCT PLACEMENT: where the product bottle sits (e.g. 'centered, large, white background behind it')\n"
        "Output these 4 points only. No mention of text, headlines, or copy.",
        image_path
    )

def analyze_product_visual(image_path: str) -> str:
    """Vision pass: describe the product bottle/packaging so the AI can render it exactly."""
    return _vision_call(
        "You are a product photographer's assistant. Describe this product image precisely "
        "so an AI image generator can render it identically in an ad. Cover:\n"
        "1. Container: shape, size, material (glass/plastic/HDPE), transparency\n"
        "2. Label: exact colors, font styles, all text visible on the label\n"
        "3. Cap/lid: shape, color, material\n"
        "4. Overall color mood: dominant colors in order\n"
        "5. Any icons, badges, or graphic elements on the packaging\n"
        "6. One-line product description (e.g. 'Squat translucent HDPE bottle, "
        "bright golden-yellow label, white ribbed screw cap')\n"
        "Be specific and literal — describe only what you see.",
        image_path
    )

def fetch_product_facts(product_url: str) -> str:
    """
    Fetch live product facts from the product URL.
    Tries Shopify JSON first (richest data), falls back to HTML scraping.
    Returns a formatted string of facts to ground copy generation.
    """
    if not product_url or not product_url.startswith("http"):
        return ""

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    facts   = []

    # ── Try 1: Shopify product JSON endpoint ──────────────────────────────────
    try:
        json_url = product_url.rstrip("/") + ".json"
        req = urllib.request.Request(json_url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read())
        prod = data.get("product", {})
        if prod.get("title"):
            facts.append(f"PRODUCT NAME: {prod['title']}")
        if prod.get("body_html"):
            clean = re.sub(r"<[^>]+>", " ", prod["body_html"])
            clean = re.sub(r"\s+", " ", clean).strip()
            facts.append(f"DESCRIPTION: {clean[:800]}")
        tags = prod.get("tags", "")
        if tags:
            facts.append(f"TAGS/KEYWORDS: {tags[:200]}")
        for v in prod.get("variants", [])[:2]:
            if v.get("title") and v["title"] != "Default Title":
                facts.append(f"VARIANT: {v['title']}")
        print(f"[web-fetch] Shopify JSON OK — {len(facts)} facts from {json_url}")
    except Exception as e:
        print(f"[web-fetch] Shopify JSON failed ({e}) — trying HTML")

    # ── Try 2: Raw HTML scraping if Shopify JSON failed or had little data ────
    if len(facts) < 2:
        try:
            req = urllib.request.Request(product_url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                html = resp.read().decode("utf-8", errors="ignore")

            # Page title
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            if m: facts.append(f"PAGE TITLE: {m.group(1).strip()[:150]}")

            # Meta description
            m = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{10,})',
                html, re.I)
            if m: facts.append(f"META DESC: {m.group(1).strip()[:300]}")

            # Open Graph description
            m = re.search(
                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']{10,})',
                html, re.I)
            if m: facts.append(f"OG DESC: {m.group(1).strip()[:300]}")

            # Look for JSON-LD product schema
            ld = re.search(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                           html, re.S | re.I)
            if ld:
                try:
                    schema = json.loads(ld.group(1))
                    if isinstance(schema, dict):
                        if schema.get("description"):
                            facts.append(f"SCHEMA DESC: {schema['description'][:400]}")
                        if schema.get("name"):
                            facts.append(f"SCHEMA NAME: {schema['name']}")
                except Exception:
                    pass

            print(f"[web-fetch] HTML scrape OK — {len(facts)} facts from {product_url}")
        except Exception as e:
            print(f"[web-fetch] HTML scrape failed: {e}")

    if not facts:
        print(f"[web-fetch] No facts retrieved from {product_url}")
        return ""

    result = "\n".join(facts)
    print(f"[web-fetch] Total: {len(result)} chars of live product data")
    return result


def _text_call(prompt_text: str, timeout: int = 30) -> str:
    """Call the text model and return the response text."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return ""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{VISION_MODEL}:generateContent?key={api_key}")
    payload = json.dumps({"contents": [{"parts": [{"text": prompt_text}]}]}).encode()
    req = urllib.request.Request(url, data=payload,
          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates",[{}])[0].get("content",{}).get("parts",[])
        return next((p["text"] for p in parts if "text" in p), "")
    except Exception as e:
        print(f"[text-call] Error: {e}")
        return ""


def verify_and_clean_copy(copy_data: dict, product_facts: str) -> dict:
    """
    Typo & fact verification pass.
    Runs the generated copy through the text model to:
    1. Fix all Bahasa Malaysia spelling errors
    2. Ensure all claims match real product facts
    3. Remove any hallucinated words or phrases
    Returns a corrected copy_data dict.
    """
    if not copy_data:
        return copy_data

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return copy_data

    # Serialize current copy for verification
    copy_json = json.dumps(copy_data, ensure_ascii=False, indent=2)

    facts_section = f"""
FAKTA PRODUK SEBENAR (dari laman web):
{product_facts}
""" if product_facts else ""

    prompt = f"""Kamu adalah editor iklan Malaysia yang pakar dalam Bahasa Malaysia dan Bahasa Inggeris.

TUGASAN: Semak dan betulkan copy iklan di bawah.

{facts_section}

COPY UNTUK DISEMAK:
{copy_json}

ARAHAN SEMAKAN:
1. EJAAN — betulkan SEMUA ejaan Bahasa Malaysia yang salah.
   Contoh kesalahan biasa: "Ingredielas"→"Ingredien", "Nidiak"→"Tidak", "Sembuataa"→"Semulajadi"
2. PERKATAAN REKAAN — buang mana-mana perkataan yang tidak wujud dalam Bahasa Malaysia
3. FAKTA — pastikan semua tuntutan sepadan dengan fakta produk di atas
4. AYAT — pastikan setiap ayat adalah Bahasa Malaysia yang betul dan boleh difahami
5. PENDEK — setiap bullet point maksimum 5 patah perkataan
6. JANGAN ubah struktur JSON atau nama kunci (keys)
7. JANGAN ubah teks yang sudah betul tanpa sebab

Kembalikan JSON yang telah dibetulkan sahaja (tiada teks lain, tiada ``` fences):"""

    raw = _text_call(prompt, timeout=30)
    if not raw:
        return copy_data

    # Strip code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$",          "", raw.strip())

    try:
        verified = json.loads(raw)
        print(f"[verify-copy] Verified — h1='{verified.get('h1','')}' | left_bullets={verified.get('left_bullets',[])[:1]}")
        return verified
    except Exception as e:
        print(f"[verify-copy] JSON parse error: {e} — using original copy")
        return copy_data


def _get_logo_path() -> str:
    """Read logo path from Brand Setup. Returns path string or ''."""
    try:
        brand = load_brand()
        path  = brand.get("logo_path", "")
        if path and Path(path).exists():
            return path
    except Exception:
        pass
    return ""


def _get_zone_text_color(img, x1, y1, x2, y2):
    """
    Sample background brightness in a zone and return a contrasting dark color.
    Light background → near-black. Dark background → also dark (with white backing).
    Returns (text_color, needs_white_backing).
    """
    try:
        from PIL import Image as PILImage
        region = img.crop((x1, y1, x2, y2)).convert("L")
        pixels = list(region.getdata())
        avg    = sum(pixels) / max(len(pixels), 1)
        # Always use dark text — just vary the backing opacity
        if avg < 100:        # very dark bg → white backing, dark text
            return (15, 15, 15, 255), True
        elif avg < 160:      # medium bg → semi-white backing, dark text
            return (20, 20, 20, 255), True
        else:                # light bg → no backing needed, dark text
            return (25, 25, 25, 255), False
    except Exception:
        return (20, 20, 20, 255), True


def _zone_flatness(img, x1, y1, x2, y2):
    """
    Standard deviation of luminance in a region — LOW value means a flat/simple
    area (good for placing text), HIGH value means busy/detailed (product, etc.).
    """
    try:
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(img.width, int(x2)), min(img.height, int(y2))
        if x2 <= x1 or y2 <= y1:
            return 999
        region = img.crop((x1, y1, x2, y2)).convert("L")
        pixels = list(region.getdata())
        n = len(pixels)
        if n == 0:
            return 999
        avg = sum(pixels) / n
        var = sum((p - avg) ** 2 for p in pixels) / n
        return var ** 0.5
    except Exception:
        return 999


def apply_text_overlay(img_path: str, copy_data: dict, out_path: str,
                       img_idx: int = 0, brand_colors: dict = None,
                       layout_template: int = None, extracted_style: dict = None) -> bool:
    """
    PIL text overlay — single pass, zero duplicates.

    Text placement is chosen FREELY/DYNAMICALLY per image: PIL scans candidate
    regions of the generated image and picks whichever is flattest/cleanest
    (least busy) for the headline block, then picks the cleanest bottom corner
    (or full bottom strip) for the CTA — so placement adapts to wherever Gemini
    actually left empty space, instead of a fixed template.

    Headline candidates:
      0 — top_center:       centered headline top, CTA full-width bottom strip
      1 — left_block:       left-aligned headline block, CTA pill bottom-left/right
      2 — top_left_compact: compact top-left headline, CTA pill bottom-left/right

    Fonts: Montserrat-Bold for H1, Poppins for everything else.
    Colors: dark/black, auto-contrasted against Gemini background.
    Logo: loaded from Brand Setup, pasted as-is with transparency preserved.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[pil] Pillow not available"); return False

    if not copy_data:
        return False

    # ── Only H1, H2, CTA — no bullets, no comparison columns ─────────────────
    h1  = (copy_data.get("h1")  or "").strip()
    h2  = (copy_data.get("h2")  or "").strip()
    cta = (copy_data.get("cta") or "").strip()

    if not any([h1, h2, cta]):
        return False

    # ── Pull text direction/style from the winning ad's extracted style ────────
    headline_placement = ""
    headline_case      = ""
    headline_size_word = ""
    h1_color_override  = None
    if extracted_style:
        layout_b = extracted_style.get("layout", {}) or {}
        typo_b   = extracted_style.get("typography", {}) or {}
        colors_b = extracted_style.get("colors", {}) or {}
        headline_placement = (layout_b.get("headline_placement") or "").lower()
        headline_case      = (typo_b.get("headline_case") or "").lower()
        headline_size_word = (typo_b.get("headline_size") or "").lower()

        hex_candidate = (colors_b.get("text_color_headline") or "").strip()
        if re.fullmatch(r"#?[0-9a-fA-F]{6}", hex_candidate):
            h1_color_override = hex_candidate if hex_candidate.startswith("#") else f"#{hex_candidate}"

    if headline_case == "uppercase":
        h1 = h1.upper()
    elif headline_case == "lowercase":
        h1 = h1.lower()

    # Scale headline size to match the winning ad's relative headline size
    SIZE_SCALE = {"small": 0.78, "medium": 0.9, "large": 1.0, "very large": 1.15}
    h1_scale = next((v for k, v in SIZE_SCALE.items() if k in headline_size_word), 1.0)

    # ── Font loader — Montserrat-Bold for H1, Poppins for everything else ─────
    def fnt(path, size):
        for p in [path, FONT_FALLBACK]:
            if p and os.path.exists(p):
                try: return ImageFont.truetype(p, int(size))
                except Exception: pass
        return ImageFont.load_default()

    # ── Text measurement using textbbox — no hardcoded sizes ──────────────────
    def measure(font, text):
        """Returns (width, height) of text using textbbox for accuracy."""
        try:
            bb = font.getbbox(text)
            return bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            sz = getattr(font, 'size', 14)
            return len(text) * sz // 2, sz

    def text_height(font, text="Ag"):
        """Accurate height of a line of text."""
        try: return font.getbbox(text)[3]
        except Exception: return getattr(font, 'size', 14)

    def text_width(font, text):
        try: return int(font.getlength(text))
        except Exception: return measure(font, text)[0]

    def wrap(text, font, max_w):
        words, lines, cur = text.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if text_width(font, t) <= max_w: cur = t
            else:
                if cur: lines.append(cur)
                cur = w
        if cur: lines.append(cur)
        return lines or [text]

    def put_text_backed(d, x, y, text, font, color, bpad=8, radius=8):
        """Draw text with white semi-transparent backing for readability."""
        w, h = measure(font, text)
        d.rounded_rectangle(
            [x - bpad, y - bpad // 2, x + w + bpad, y + h + bpad // 2],
            radius=radius, fill=(255, 255, 255, 185)
        )
        d.text((x, y), text, font=font, fill=color)

    # ── Load Gemini image ──────────────────────────────────────────────────────
    img  = Image.open(img_path).convert("RGBA")
    W, H = img.size
    cv   = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d    = ImageDraw.Draw(cv)
    PAD  = int(W * 0.055)
    DARK = (18, 18, 18, 255)

    # ── Brand colors for H1 — alternate Primary / Secondary per image ────────
    bc = brand_colors or {}

    def hex_to_rgba(hex_str, alpha=255):
        """Convert #RRGGBB to (R,G,B,A)."""
        h = hex_str.lstrip("#")
        if len(h) == 6:
            return (int(h[0:2],16), int(h[2:4],16), int(h[4:6],16), alpha)
        return (18, 18, 18, alpha)

    primary_hex   = bc.get("primary_color",   "#F5A800")
    secondary_hex = bc.get("secondary_color", "#D94F00")

    # Even images → Primary (#F5A800 gold), Odd images → Secondary (#D94F00 red-orange)
    # If the winning ad's headline color was extracted as a real hex, prefer that.
    if h1_color_override:
        h1_color = hex_to_rgba(h1_color_override)
    else:
        h1_color = hex_to_rgba(primary_hex) if img_idx % 2 == 0 else hex_to_rgba(secondary_hex)

    # ── Derive headline & CTA regions directly from the winning ad's layout
    # description — no fixed top-center/left-block/top-left templates. ────────
    cta_placement = ((extracted_style or {}).get("layout", {}) or {}).get("cta_placement", "").lower()

    def region_from(desc, default_y_frac):
        desc = desc or ""
        if "bottom" in desc:
            y_frac = 0.60
        elif "middle" in desc or "centre" in desc or ("center" in desc and "top" not in desc and "bottom" not in desc):
            y_frac = 0.38
        else:
            y_frac = default_y_frac
        if "right" in desc:
            align = "right"
        elif "center" in desc or "centre" in desc:
            align = "center"
        else:
            align = "left"
        return y_frac, align

    h_y_frac, h_align = region_from(headline_placement, default_y_frac=0.05)
    TXT_W = (W - PAD * 2) if h_align == "center" else int(W * 0.56) - PAD

    print(f"[pil] Image #{img_idx+1} H1 color: "
          f"{'override ' + h1_color_override if h1_color_override else ('Primary ' + primary_hex if img_idx % 2 == 0 else 'Secondary ' + secondary_hex)} "
          f"| headline_placement='{headline_placement}' -> y_frac={h_y_frac} align={h_align}")

    def draw_pill(text_str, font, anchor_x, anchor_y, align="left",
                   bg=(245, 168, 0, 248), fg=(18, 18, 18, 255), pad_h=22, pad_v=12):
        """Draw a rounded-rect CTA pill. anchor_x/anchor_y = top-left if align='left',
        or top-right corner (anchor_x = right edge) if align='right'."""
        tw = text_width(font, text_str)
        th = text_height(font, text_str)
        bw = tw + pad_h * 2
        bh = th + pad_v * 2
        if align == "right":
            x0 = anchor_x - bw
        else:
            x0 = anchor_x
        y0 = anchor_y
        d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=bh // 2, fill=bg)
        d.text((x0 + pad_h, y0 + pad_v), text_str, font=font, fill=fg)
        return bw, bh

    # ── Headline (H1) ──────────────────────────────────────────────────────────
    cur_y = int(H * h_y_frac) + (4 if h_y_frac > 0.05 else 20)
    h1_bottom = cur_y
    if h1:
        sz_h1 = max(30, int(H * 0.060 * h1_scale))
        f_h1  = fnt(FONT_H1, sz_h1)
        for line in wrap(h1, f_h1, TXT_W):
            lw = text_width(f_h1, line)
            lh = text_height(f_h1, line)
            if h_align == "center":
                x = (W - lw) // 2
            elif h_align == "right":
                x = W - PAD - lw
            else:
                x = PAD
            shadow_offset = max(2, int(lh * 0.06))
            d.text((x + shadow_offset, cur_y + shadow_offset), line, font=f_h1, fill=(0, 0, 0, 160))
            d.text((x, cur_y), line, font=f_h1, fill=h1_color)
            cur_y += lh + 6
        h1_bottom = cur_y

    # ── Subheadline (H2) ─────────────────────────────────────────────────────
    c2, _ = _get_zone_text_color(img, PAD, int(H * h_y_frac), W - PAD, min(H, h1_bottom + int(H * 0.18)))
    cur_y = h1_bottom + 14
    if h2:
        sz_h2 = max(18, int(H * 0.032))
        f_h2  = fnt(FONT_BODY, sz_h2)
        for line in wrap(h2, f_h2, TXT_W):
            lw = text_width(f_h2, line)
            lh = text_height(f_h2, line)
            if h_align == "center":
                x = (W - lw) // 2
            elif h_align == "right":
                x = W - PAD - lw
            else:
                x = PAD
            d.text((x + 2, cur_y + 2), line, font=f_h2, fill=(0, 0, 0, 120))
            d.text((x, cur_y), line, font=f_h2, fill=c2)
            cur_y += lh + 4

    # ── CTA — placement follows the winning ad's cta_placement ────────────────
    CTA_H, CTA_TOP = int(H * 0.115), 0
    if cta:
        sz_cta = max(22, int(H * 0.034))
        f_cta  = fnt(FONT_BOLD, sz_cta)
        if "right" in cta_placement:
            draw_pill(cta, f_cta, W - PAD, H - int(H*0.10) - PAD, align="right")
        elif any(k in cta_placement for k in ("center", "centre", "full", "bar", "strip", "bottom")) or not cta_placement:
            CTA_H   = int(H * 0.115)
            CTA_TOP = H - CTA_H
            d.rectangle([0, CTA_TOP, W, H], fill=(245, 168, 0, 248))
            cw_cta = text_width(f_cta, cta)
            ch_cta = text_height(f_cta, cta)
            d.text(((W - cw_cta) // 2, CTA_TOP + (CTA_H - ch_cta) // 2), cta, font=f_cta, fill=(18, 18, 18, 255))
        else:
            draw_pill(cta, f_cta, PAD, H - int(H*0.10) - PAD, align="left")

    # ── Logo — opposite corner from the headline ───────────────────────────────
    logo_path = _get_logo_path()
    if logo_path:
        try:
            logo = Image.open(logo_path).convert("RGBA")
            target_w = 110
            ratio  = target_w / logo.width
            logo_w, logo_h = target_w, int(logo.height * ratio)
            logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
            logo_x = PAD if h_align == "right" else W - logo_w - 16
            logo_y = 16
            if CTA_TOP and (logo_y + logo_h) > CTA_TOP - 4:
                logo_y = CTA_TOP - logo_h - 10
            cv.paste(logo, (logo_x, logo_y), logo)
        except Exception as e:
            print(f"[pil] Logo skipped: {e}")

    # ── Composite — called exactly ONCE ───────────────────────────────────────
    result = Image.alpha_composite(img, cv).convert("RGB")
    result.save(out_path, "PNG", optimize=True)
    print(f"[pil] Done — H1={bool(h1)} H2={bool(h2)} CTA={bool(cta)} layout={layout_template}")
    return True


def extract_benefits_from_knowledge(benefits_text: str, problem: str) -> list:
    """
    Extract product benefit bullets DIRECTLY from the user's Brand Setup text.
    No AI involved — copies exactly what the user wrote.
    Looks for the problem section first, then falls back to all bullets.
    Returns at least 3 benefits.
    """
    if not benefits_text:
        return []

    lines = benefits_text.split("\n")
    bullets = []
    in_section = False
    problem_upper = problem.upper().replace("-", " ").replace("_", " ")

    # ── Pass 1: Find bullets under the matching problem section ─────────────
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Detect section headers (all-caps lines or lines ending with :)
        if stripped.upper() == stripped and len(stripped) > 3 and not stripped.startswith("-"):
            # Check if this header matches the current problem
            in_section = any(kw in stripped.upper() for kw in problem_upper.split())
            continue
        if stripped.endswith(":") and len(stripped) < 40:
            in_section = any(kw in stripped.upper() for kw in problem_upper.split())
            continue
        # Extract bullet points
        if stripped.startswith("-") or stripped.startswith("•") or stripped.startswith("*"):
            bullet = stripped.lstrip("-•*").strip()
            if bullet and len(bullet) > 3:
                if in_section:
                    bullets.append(bullet)

    # ── Pass 2: If fewer than 3 found, take bullets from anywhere in text ───
    if len(bullets) < 3:
        all_bullets = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("-") or stripped.startswith("•") or stripped.startswith("*"):
                bullet = stripped.lstrip("-•*").strip()
                if bullet and len(bullet) > 3 and bullet not in bullets:
                    all_bullets.append(bullet)
        # Fill up to 5 total
        for b in all_bullets:
            if len(bullets) >= 5:
                break
            bullets.append(b)

    return bullets[:5]   # max 5 benefits


def generate_copy_from_knowledge(framework_hint, problem, problem_ctx, brand,
                                  copy_h1="", copy_h2="", copy_cta="",
                                  product_facts="") -> dict:
    """
    Step 1 of 2: Use the text model to generate typo-free, accurate ad copy
    from the brand knowledge base BEFORE image generation.
    Returns a structured dict with h1, h2, cta, left_bullets, right_bullets etc.
    Any fields the user already filled in manually are kept as-is.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"h1": copy_h1, "h2": copy_h2, "cta": copy_cta}

    product_name = brand.get("_active_product_name") or brand.get("brand_name") or "produk ini"
    brand_name   = brand.get("brand_name") or product_name
    kb           = brand.get("knowledge", {})
    price        = brand.get("price", "")
    certs        = ", ".join(brand.get("certifications", [])) or ""
    claims       = "\n".join(f"- {c}" for c in brand.get("key_claims", []))
    benefits_full = (kb.get("benefits")    or "").strip()
    product_txt   = (kb.get("product")     or "").strip()[:500]
    comp_txt      = (kb.get("competitors") or "").strip()[:400]
    proof_txt     = (kb.get("proof")       or "").strip()[:300]
    audience_txt  = (kb.get("audience")    or "").strip()[:300]

    # ── Extract benefits DIRECTLY from Brand Setup — exact copy, no AI ──────
    direct_benefits = extract_benefits_from_knowledge(benefits_full, problem)
    print(f"[copy-gen] Direct benefits from Brand Setup: {direct_benefits}")

    # Format benefits for the prompt (first 600 chars of full text)
    benefits_txt = benefits_full[:600]

    # ── Build JSON template based on framework ───────────────────────────────
    is_vs_them = framework_hint == "US_VS_THEM"

    if is_vs_them:
        json_template = f"""{{
  "h1": "Short powerful headline max 7 words",
  "h2": "Supporting subheadline max 10 words",
  "cta": "Call to action max 5 words (no price)",
  "left_header": "JENAMA LAIN",
  "left_bullets": ["competitor weakness 1 (max 5 words)", "weakness 2", "weakness 3", "weakness 4"],
  "right_header": "{brand_name}",
  "right_bullets": ["REAL benefit 1 from Brand Setup", "benefit 2", "benefit 3", "benefit 4"],
  "benefit_bullets": []
}}"""
    else:
        # All other frameworks: single column benefits, no comparison table
        json_template = f"""{{
  "h1": "Short powerful headline max 7 words",
  "h2": "Supporting subheadline max 10 words",
  "cta": "Call to action max 5 words (no price)",
  "left_header": "",
  "left_bullets": [],
  "right_header": "",
  "right_bullets": [],
  "benefit_bullets": [
    "REAL benefit 1 from Brand Setup (max 6 words)",
    "REAL benefit 2 from Brand Setup",
    "REAL benefit 3 from Brand Setup",
    "REAL benefit 4 from Brand Setup"
  ]
}}"""

    framework_task = {
        "US_VS_THEM":    "US VS THEM comparison — two columns: competitor weaknesses vs brand benefits",
        "TESTIMONIAL":   "Testimonial style — focus on a customer result/story and key benefits",
        "PROBLEM_HOOK":  "Problem Hook — headline states the problem, subheadline is the solution, bullets are benefits",
        "BENEFIT":       "Benefit showcase — headline is bold claim, bullets list real product benefits",
    }.get(framework_hint, "Benefit showcase")

    prompt = f"""Write ad copy for {product_name} by {brand_name}.

PRODUCT KNOWLEDGE (use ONLY these facts — never invent):
DESCRIPTION: {product_txt or '(not provided)'}
BENEFITS for {problem}: {benefits_txt or '(not provided)'}
COMPETITORS: {comp_txt or '(not provided)'}
CERTIFICATIONS: {certs or 'Halal JAKIM, KKM Approved, GMP Certified'}
{f"LIVE PRODUCT FACTS: {product_facts}" if product_facts else ""}

TASK: {framework_task}
TARGET: {problem}

RULES:
1. Use ONLY facts above — never invent
2. Correct Bahasa Malaysia only — no invented words
3. Max 5 words per bullet
4. No price, no RM amounts
{"5. benefit_bullets must contain REAL benefits from the Benefits section above" if not is_vs_them else "5. right_bullets must be REAL benefits from the Benefits section above"}

Return JSON only:
{json_template}"""

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{VISION_MODEL}:generateContent?key={api_key}")
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(url, data=payload,
          headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates",[{}])[0].get("content",{}).get("parts",[])
        raw   = next((p["text"] for p in parts if "text" in p), "")
        # Strip markdown code fences if present
        raw   = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        raw   = re.sub(r"\s*```$", "",        raw.strip())
        copy_data = json.loads(raw)

        # ── User-provided fields always win (exact text, no changes) ──────────
        if copy_h1:  copy_data["h1"]  = copy_h1
        if copy_h2:  copy_data["h2"]  = copy_h2
        if copy_cta: copy_data["cta"] = copy_cta

        # ── Enforce minimum 3 benefits from Brand Setup ───────────────────────
        # Direct benefits (extracted from Brand Setup text) take priority over AI-generated ones.
        # This guarantees the ad always shows real product benefits the user wrote themselves.
        if direct_benefits:
            ai_bullets    = copy_data.get("right_bullets", [])
            # Start with direct benefits (user's exact words), pad with AI bullets if needed
            merged = list(direct_benefits)   # user's exact words from Brand Setup
            for b in ai_bullets:
                if len(merged) >= 5: break
                if b not in merged: merged.append(b)
            copy_data["right_bullets"] = merged[:5]
            print(f"[copy-gen] Benefits enforced from Brand Setup: {merged[:3]}")
        else:
            print("[copy-gen] WARNING: no benefits found in Brand Setup — using AI-generated bullets")

        # Remove price from CTA if it slipped in
        cta_val = copy_data.get("cta", "")
        copy_data["cta"] = re.sub(r'RM\s*\d+', '', cta_val).strip().rstrip("—- ")

        print(f"[copy-gen] Final — h1='{copy_data.get('h1','')}' | benefits={copy_data.get('right_bullets',[])[:2]}")
        return copy_data
    except Exception as e:
        print(f"[copy-gen] Error: {e}")
        # Even on error, return direct benefits if we have them
        return {
            "h1": copy_h1, "h2": copy_h2, "cta": copy_cta,
            "right_bullets": direct_benefits,
            "right_header": brand_name,
        }


VIBE_MODIFIERS = [
    "warm and energetic — golden hour lighting, rich warm tones, vibrant and inviting",
    "clean and professional — bright white light, crisp sharp edges, minimal and trustworthy",
    "bold and dramatic — deep contrast, strong shadows, high-impact and powerful",
    "soft and calming — pastel tones, gentle gradients, soothing and natural",
    "fresh and modern — cool blue-white tones, sleek, contemporary health brand feel",
    "rich and premium — deep luxurious tones, elegant, premium product presentation",
]


def build_prompt(layout_analysis, product_visual, problem, problem_ctx,
                 has_style_ref, has_product, brand,
                 copy_h1="", copy_h2="", copy_cta="", copy_data=None,
                 vibe_idx=0, extracted_style=None, skip_overlay=False, tweak_instructions=""):
    """
    Replication brief: keep exact winning-ad layout, swap in the correct product + copy.
    product_visual = vision analysis of the product photo (describes bottle precisely).
    """
    brand_block  = brand_to_prompt_block(brand)
    product_name = brand.get("_active_product_name") or brand.get("brand_name") or "this product"

    # ── Product identity section (most important — AI must render this exactly) ──
    if product_visual:
        product_section = f"""━━━ PRODUCT IDENTITY — MUST MATCH EXACTLY ━━━
The FIRST reference image attached is the actual product photo.
Render the product bottle IDENTICALLY to that photo. Do not invent or substitute.

Visual description of the product (from photo analysis):
{product_visual}

Product name: {product_name}
CRITICAL RULES:
- The product bottle in the ad MUST look exactly like the reference photo
- Keep every label detail: colors, text, fonts, icons as seen in the photo
- Same cap shape, bottle shape, transparency as the photo
- Place the bottle as the visual hero of the ad
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""
    else:
        product_section = f"""PRODUCT: {product_name}
The product photo is the first reference image — render it exactly as shown.
It is the hero of the ad. Reproduce label, cap, bottle shape accurately."""

    # ── Layout section ──
    if layout_analysis and has_style_ref:
        layout_section = f"""━━━ AD LAYOUT — REPLICATE THIS STRUCTURE ━━━
The SECOND reference image is a winning competitor ad. Use it for layout only.
{layout_analysis}

LAYOUT RULES:
- Keep the SAME background, zone splits, text hierarchy, composition structure
- Keep the SAME positions for headline, subheadline, CTA, badges, overlays
- REPLACE the competitor product → use the product from the first reference photo
- REPLACE all text → write Serigama copy for the target problem (in Bahasa Malaysia)
- REPLACE competitor brand colors → use brand colors listed below
- The layout skeleton stays. Product identity and copy change.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""
    elif has_style_ref:
        layout_section = ("Use the second reference image for layout structure. "
                          "Replace its product and text with the product from the first photo.")
    else:
        layout_section = "Create a clean, bold 4:5 static ad. Product is the hero."

    # ── User copy — these override everything. User fills H1/H2/CTA → used verbatim.
    # If blank → use AI-generated copy_data. Either way, Gemini gets exact strings.
    brand_name = brand.get("brand_name", "Serigama")

    # ── Simple per-slot variation phrase — just enough for batch variety ──────
    VARIATION_AXES = [
        "straight-on, eye-level angle",
        "slightly from the left, eye-level angle",
        "slightly from the right, eye-level angle",
        "slightly from above",
        "slightly closer / zoomed in",
        "slightly further / zoomed out",
    ]
    variation_note = VARIATION_AXES[vibe_idx % len(VARIATION_AXES)]

    LAYOUT_VARIATIONS = [
        "product placed center, balanced negative space around it",
        "product placed slightly left, more breathing room on the right",
        "product placed slightly right, more breathing room on the left",
        "product placed lower in frame, open space above",
        "product placed upper-frame, grounded shadow below",
        "product placed off-center on a diagonal, dynamic negative space",
    ]
    layout_note = LAYOUT_VARIATIONS[vibe_idx % len(LAYOUT_VARIATIONS)]

    # ── Copy block — Gemini renders this text directly onto the ad ───────────
    h1  = copy_h1  or (copy_data or {}).get("h1", "")
    h2  = copy_h2  or (copy_data or {}).get("h2", "")
    cta = copy_cta or (copy_data or {}).get("cta", "")
    copy_lines = []
    if h1:
        copy_lines.append(f'- HEADLINE (large, bold, most prominent): "{h1}"')
    if h2:
        copy_lines.append(f'- SUBHEADLINE (smaller, below headline): "{h2}"')
    if cta:
        copy_lines.append(f'- CTA (button or pill, high contrast): "{cta}"')
    copy_block = ""
    if copy_lines:
        copy_block = (
            "\n\nTEXT TO RENDER ON THE AD (in Bahasa Malaysia, exactly as written, no spelling changes):\n"
            + "\n".join(copy_lines) +
            "\n\nTEXT STYLING:\n"
            "- Use clean, legible, modern sans-serif typography\n"
            "- Place the headline, subheadline, and CTA following IMAGE 1's text placement and hierarchy "
            "(same regions, alignment, and relative sizing as the winning ad)\n"
            "- Ensure strong contrast between text and background (add a subtle shadow, scrim, or solid panel behind text if needed for readability)\n"
            "- The CTA should look like a tappable button/pill with a solid background color from the brand palette"
        )

    color_block  = ""
    layout_block = ""
    human_block  = ""
    if extracted_style:
        colors = extracted_style.get("colors", {}) or {}
        mood   = extracted_style.get("mood", {}) or {}
        comp   = extracted_style.get("composition", {}) or {}
        lay    = extracted_style.get("layout", {}) or {}
        color_bits = []
        if colors.get("background_description"):
            color_bits.append(f"background feel: {colors['background_description']}")
        if colors.get("dominant_color"):
            color_bits.append(f"dominant tone: {colors['dominant_color']}")
        if colors.get("accent_color"):
            color_bits.append(f"accent tone: {colors['accent_color']}")
        if mood.get("lighting"):
            color_bits.append(f"lighting: {mood['lighting']}")
        if mood.get("tone"):
            color_bits.append(f"overall tone: {mood['tone']}")
        if color_bits:
            color_block = "\n\nCOLOR & LIGHTING (match IMAGE 1 closely):\n- " + "\n- ".join(color_bits)

        layout_bits = []
        if lay.get("structure"):
            layout_bits.append(f"overall structure: {lay['structure']}")
        if lay.get("product_placement"):
            layout_bits.append(f"product placement: {lay['product_placement']}")
        if lay.get("negative_space"):
            layout_bits.append(f"negative space: {lay['negative_space']}")
        if comp.get("style"):
            layout_bits.append(f"composition style: {comp['style']}")
        if comp.get("background_type"):
            layout_bits.append(f"background type: {comp['background_type']}")
        if comp.get("product_style"):
            layout_bits.append(f"product treatment: {comp['product_style']}")
        graphics = extracted_style.get("graphic_elements") or []
        if graphics:
            layout_bits.append("graphic elements to recreate: " + "; ".join(graphics))

        if layout_bits:
            layout_block = "\n\nLAYOUT — REPLICATE FROM IMAGE 1:\n- " + "\n- ".join(layout_bits)
            if graphics:
                layout_block += ("\n\nIMPORTANT: IMAGE 1 contains graphic elements (arrows, pointers, badges, "
                                  "callouts, icons) listed above — recreate these same elements in the same "
                                  "positions, adapted to point at / highlight the new product and copy.")

        if comp.get("has_human"):
            human_block = ("\n\nHUMAN ELEMENT: IMAGE 1 features a person interacting with the product. "
                           "You MAY include a person in a similar role (e.g. holding/using the product), "
                           "but do not copy their identity, face, or outfit — generate a new person that "
                           "fits the same vibe.")

    problem_block = ""
    if problem_ctx:
        problem_block = f"\n\nTARGET PROBLEM / ANGLE FOR THIS AD: {problem_ctx}\nUse this to inform mood, imagery cues, and the copy's emotional angle — the ad should feel relevant to someone facing this problem."

    tweak_block = ""
    if tweak_instructions:
        tweak_block = f"""

━━━ USER REQUESTED EDIT — HIGHEST PRIORITY ━━━
The attached IMAGE 1 is the ad to fix/edit. Keep everything about it the same EXCEPT for the
following requested change. This instruction overrides any conflicting guidance above:
{tweak_instructions}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    if has_style_ref and has_product:
        return f"""Generate a 4:5 advertisement image.

{brand_block}{problem_block}

CREATIVE DIRECTION: Clone IMAGE 1 as a template — same composition, layout, background, color palette, lighting,
and every graphic element (panels, badges, arrows, icons, dividers) in the same positions and styling.
This must look like the EXACT same ad as IMAGE 1, with ONLY the product and text swapped.{color_block}{layout_block}

PRODUCT: Completely remove IMAGE 1's product and replace it with IMAGE 2's product, in the exact same position,
scale, angle, and role that IMAGE 1's product occupied (same surface, same hand, same framing). Render IMAGE 2's
product faithfully with correct shape, label, and packaging.

LAYOUT VARIATION FOR THIS SLOT (apply on top of the above, keep it subtle): {layout_note}

CAMERA ANGLE: {variation_note}{human_block}{copy_block}

OUTPUT RULES:
- No watermarks or signatures
- No invented text other than the headline, subheadline, and CTA specified above
- Clean, professional ad quality
- 4:5 aspect ratio{tweak_block}"""

    elif has_product:
        return f"""Create a 4:5 advertisement image.

{brand_block}{problem_block}

PRODUCT: Feature only the product from the PRODUCT image. Do not change or replace it.
BACKGROUND: Clean soft gradient, professional health product style.
PRODUCT PLACEMENT: centered, large, clear hero shot.

Variation for this slot: {variation_note}{copy_block}

Rules:
- Product centered and clearly visible
- Clean, professional ad quality{tweak_block}"""

    else:
        return f"""Create a clean 4:5 advertisement background for {brand_name}.

{brand_block}{problem_block}

Professional health product style, soft gradient.{copy_block}{tweak_block}"""

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(SKILLS_DIR, "ui.html")

# ── Brand kit endpoints ───────────────────────────────────────────────────────
@app.route("/api/brand-kit", methods=["GET"])
def get_brand_kit():
    return jsonify(load_brand())

@app.route("/api/brand-kit", methods=["POST"])
def save_brand_kit():
    """Save text fields. Files handled separately."""
    data = request.json or {}
    brand = load_brand()
    for key in ["brand_name","tagline","brand_voice","primary_color","secondary_color",
                "accent_color","price","product_url","key_claims","certifications",
                "extra_notes","knowledge","products","active_product_id"]:
        if key in data:
            brand[key] = data[key]
    save_brand(brand)
    return jsonify({"ok": True, "brand": brand})

@app.route("/api/brand-kit/upload-logo", methods=["POST"])
def upload_logo():
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify({"error": "No file"}), 400
    ext  = Path(f.filename).suffix
    dest = BRAND_DIR / f"logo{ext}"
    f.save(str(dest))
    brand = load_brand()
    brand["logo_path"] = str(dest)
    save_brand(brand)
    return jsonify({"ok": True, "logo_url": f"/api/brand-kit/asset/logo{ext}"})

@app.route("/api/brand-kit/upload-product", methods=["POST"])
def upload_brand_product():
    """Upload to global product_photos pool (legacy / fallback)."""
    saved = []
    for f in request.files.getlist("photos"):
        if not f.filename: continue
        name = f"product_{len(list(BRAND_DIR.glob('product_*')))}_{f.filename}"
        dest = BRAND_DIR / name
        f.save(str(dest))
        saved.append({"url": f"/api/brand-kit/asset/{name}", "path": str(dest)})
    brand = load_brand()
    brand["product_photos"] = brand.get("product_photos",[]) + [s["path"] for s in saved]
    save_brand(brand)
    return jsonify({"ok": True, "photos": saved})

@app.route("/api/brand-kit/upload-product-photo/<int:product_id>", methods=["POST"])
def upload_product_photo(product_id):
    """Upload photos scoped to a specific product in the products array."""
    brand    = load_brand()
    products = brand.get("products", [])
    prod     = next((p for p in products if p.get("id") == product_id), None)
    if prod is None:
        return jsonify({"error": "Product not found"}), 404
    saved = []
    for f in request.files.getlist("photos"):
        if not f.filename: continue
        name = f"prod{product_id}_{len(list(BRAND_DIR.glob(f'prod{product_id}_*')))}_{f.filename}"
        dest = BRAND_DIR / name
        f.save(str(dest))
        saved.append({"url": f"/api/brand-kit/asset/{name}", "path": str(dest)})
    prod.setdefault("photos", [])
    prod["photos"] += [s["path"] for s in saved]
    save_brand(brand)
    return jsonify({"ok": True, "photos": saved})

@app.route("/api/brand-kit/delete-product-photo", methods=["POST"])
def delete_product_photo():
    """Remove a photo from a specific product's photos list."""
    data       = request.json or {}
    product_id = int(data.get("product_id", 0))
    filepath   = data.get("path", "")
    brand      = load_brand()
    products   = brand.get("products", [])
    prod       = next((p for p in products if p.get("id") == product_id), None)
    if prod and isinstance(prod.get("photos"), list):
        prod["photos"] = [p for p in prod["photos"] if p != filepath]
    save_brand(brand)
    try:
        if filepath and Path(filepath).exists():
            Path(filepath).unlink()
    except Exception:
        pass
    return jsonify({"ok": True})

@app.route("/api/brand-kit/upload-creative", methods=["POST"])
def upload_brand_creative():
    """Upload brand creatives / existing ads as style references."""
    saved = []
    for f in request.files.getlist("creatives"):
        if not f.filename: continue
        name = f"creative_{len(list(BRAND_DIR.glob('creative_*')))}_{f.filename}"
        dest = BRAND_DIR / name
        f.save(str(dest))
        saved.append({"url": f"/api/brand-kit/asset/{name}", "path": str(dest)})
    brand = load_brand()
    brand["creative_refs"] = brand.get("creative_refs",[]) + [s["path"] for s in saved]
    save_brand(brand)
    return jsonify({"ok": True, "creatives": saved})

@app.route("/api/brand-kit/delete-asset", methods=["POST"])
def delete_brand_asset():
    data     = request.json or {}
    filepath = data.get("path","")
    brand    = load_brand()
    # Remove from any list it appears in
    for key in ("product_photos","creative_refs"):
        if isinstance(brand.get(key), list):
            brand[key] = [p for p in brand[key] if p != filepath]
    if brand.get("logo_path") == filepath:
        brand["logo_path"] = ""
    save_brand(brand)
    try:
        if filepath and Path(filepath).exists():
            Path(filepath).unlink()
    except Exception:
        pass
    return jsonify({"ok": True})

@app.route("/api/brand-kit/asset/<filename>")
def brand_asset(filename):
    return send_from_directory(BRAND_DIR, filename)

# ── Campaign upload + generate ────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def api_upload():
    session_id  = str(uuid.uuid4())[:8]
    session_dir = SESSIONS_DIR / session_id
    session_dir.mkdir(exist_ok=True)
    ad_paths = []
    for f in request.files.getlist("ads"):
        if f.filename:
            dest = session_dir / f"ad_{len(ad_paths):02d}_{f.filename}"
            f.save(str(dest))
            ad_paths.append(str(dest))
    product_path = ""
    pf = request.files.get("product")
    if pf and pf.filename:
        dest = session_dir / f"product_{pf.filename}"
        pf.save(str(dest))
        product_path = str(dest)
    meta = {"session_id": session_id, "ad_paths": ad_paths,
            "product_path": product_path,
            "problem": request.form.get("problem","Post-partum"),
            "count": int(request.form.get("count",6))}
    with open(session_dir / "meta.json","w") as fp:
        json.dump(meta, fp)
    return jsonify({"session_id": session_id, "ads_count": len(ad_paths),
                    "has_product": bool(product_path)})

@app.route("/api/image/<session_id>/<filename>")
def api_image(session_id, filename):
    return send_from_directory(SESSIONS_DIR / session_id, filename)

# ── Template library ──────────────────────────────────────────────────────────
def load_templates() -> list:
    if TEMPLATES_JSON.exists():
        with open(TEMPLATES_JSON, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_templates(tpls: list):
    with open(TEMPLATES_JSON, "w", encoding="utf-8") as f:
        json.dump(tpls, f, indent=2, ensure_ascii=False)

def next_tpl_id(tpls: list) -> int:
    return max((t["id"] for t in tpls), default=0) + 1

@app.route("/api/templates", methods=["GET"])
def get_templates():
    tpls = load_templates()
    # Attach URL for each
    for t in tpls:
        t["url"] = f"/api/templates/image/{Path(t['path']).name}"
    return jsonify(tpls)

@app.route("/api/templates/image/<filename>")
def template_image(filename):
    return send_from_directory(TEMPLATES_DIR, filename)

@app.route("/api/templates/upload", methods=["POST"])
def upload_templates():
    """Accept a PDF or image files, extract/save as numbered templates."""
    tpls = load_templates()
    added = []

    # ── PDF upload ──
    pdf_file = request.files.get("pdf")
    if pdf_file and pdf_file.filename:
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return jsonify({"error": "PyMuPDF not installed. Run: pip install pymupdf"}), 500

        pdf_bytes = pdf_file.read()
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        # Detect heading font (same logic as extract.py)
        from collections import defaultdict
        font_page_sets = defaultdict(set)
        font_sizes     = defaultdict(list)
        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = span["text"].strip()
                        if 0 < len(txt) < 60:
                            font_page_sets[span["font"]].add(page.number)
                            font_sizes[span["font"]].append(span["size"])

        heading_font = None
        if font_page_sets:
            candidates = {f: p for f, p in font_page_sets.items() if len(p) >= 2}
            pool = candidates if candidates else font_page_sets
            heading_font = max(pool, key=lambda f: (
                len(font_page_sets[f]),
                sum(font_sizes[f]) / len(font_sizes[f])
            ))

        avg_size = (sum(font_sizes[heading_font]) / len(font_sizes[heading_font])
                    if heading_font else 0)

        # Build page → section heading map
        page_section = {}
        current_section = "Winning Ad"
        for page_num, page in enumerate(doc):
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = span["text"].strip()
                        is_heading = (
                            (heading_font and span["font"] == heading_font) or
                            span["size"] >= avg_size * 0.85
                        ) and 2 < len(txt) < 60
                        if is_heading:
                            current_section = txt
            page_section[page_num] = current_section

        # Extract images
        seen_xrefs = set()
        for page_num, page in enumerate(doc):
            section = page_section.get(page_num, "Winning Ad")
            for img_idx, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    base_img = doc.extract_image(xref)
                except Exception:
                    continue
                if len(base_img["image"]) < 8000:   # skip tiny icons
                    continue
                tid      = next_tpl_id(tpls + added)
                filename = f"tpl_{tid:04d}.png"
                dest     = TEMPLATES_DIR / filename
                with open(dest, "wb") as fp:
                    fp.write(base_img["image"])
                entry = {
                    "id":      tid,
                    "path":    str(dest),
                    "label":   f"Winning Ad #{tid}",
                    "section": section,
                    "source":  pdf_file.filename,
                    "page":    page_num,
                }
                added.append(entry)

        doc.close()

    # ── Image files upload ──
    for img_file in request.files.getlist("images"):
        if not img_file.filename:
            continue
        tid      = next_tpl_id(tpls + added)
        ext      = Path(img_file.filename).suffix or ".png"
        filename = f"tpl_{tid:04d}{ext}"
        dest     = TEMPLATES_DIR / filename
        img_file.save(str(dest))
        added.append({
            "id":      tid,
            "path":    str(dest),
            "label":   f"Winning Ad #{tid}",
            "section": "Uploaded",
            "source":  img_file.filename,
            "page":    0,
        })

    tpls.extend(added)
    save_templates(tpls)

    # ── STEP 2: Extract visual style from each newly added template ────────────
    # Run in background-friendly way — failures are non-fatal
    for t in added:
        if not t.get("extracted_style"):
            style = extract_winning_ad_style(t["path"])
            if style:
                t["extracted_style"] = style
                # Persist style into templates.json
                for saved in tpls:
                    if saved["id"] == t["id"]:
                        saved["extracted_style"] = style
                        break
    save_templates(tpls)

    for t in added:
        t["url"] = f"/api/templates/image/{Path(t['path']).name}"
    return jsonify({"ok": True, "added": len(added), "templates": added})


@app.route("/api/templates/style/<int:tpl_id>", methods=["GET"])
def get_template_style(tpl_id):
    """Return the extracted style JSON for a specific template."""
    tpls  = load_templates()
    tpl   = next((t for t in tpls if t["id"] == tpl_id), None)
    if not tpl:
        return jsonify({"error": "Template not found"}), 404
    style = tpl.get("extracted_style")
    if not style:
        # Try extracting now if not done yet
        style = extract_winning_ad_style(tpl["path"])
        if style:
            tpl["extracted_style"] = style
            save_templates(tpls)
    return jsonify({"style": style, "tpl_id": tpl_id})

@app.route("/api/templates/rename", methods=["POST"])
def rename_template():
    data  = request.json or {}
    tid   = int(data.get("id", 0))
    label = data.get("label","").strip()
    tpls  = load_templates()
    for t in tpls:
        if t["id"] == tid:
            t["label"] = label
            break
    save_templates(tpls)
    return jsonify({"ok": True})

@app.route("/api/templates/delete", methods=["POST"])
def delete_template():
    data = request.json or {}
    tid  = int(data.get("id", 0))
    tpls = load_templates()
    tpl  = next((t for t in tpls if t["id"] == tid), None)
    if tpl:
        try:
            Path(tpl["path"]).unlink(missing_ok=True)
        except Exception:
            pass
        tpls = [t for t in tpls if t["id"] != tid]
        save_templates(tpls)
    return jsonify({"ok": True})

@app.route("/api/templates/delete-all", methods=["POST"])
def delete_all_templates():
    tpls = load_templates()
    for t in tpls:
        try: Path(t["path"]).unlink(missing_ok=True)
        except Exception: pass
    save_templates([])
    return jsonify({"ok": True})

# ── Updated generate-one: accept template_ids ─────────────────────────────────
@app.route("/api/generate-one", methods=["POST"])
def api_generate_one():
    data         = request.json
    session_id   = data["session_id"]
    idx          = int(data["index"])
    quality      = data.get("quality", "draft")
    problem      = data.get("problem", "Post-partum")
    total        = int(data.get("count", 6))
    template_ids = data.get("template_ids", [])   # list of int template IDs
    campaign_id  = data.get("campaign_id", None)
    campaign_name= data.get("campaign_name", "")
    # active_product_id comes as a JS integer — normalise to int for comparison
    _apid = data.get("active_product_id", None)
    active_product_id = int(_apid) if _apid is not None else None
    copy_h1      = (data.get("copy_h1")  or "").strip()
    copy_h2      = (data.get("copy_h2")  or "").strip()
    copy_cta     = (data.get("copy_cta") or "").strip()
    skip_overlay = bool(data.get("skip_overlay", False))

    session_dir = SESSIONS_DIR / session_id
    with open(session_dir / "meta.json") as fp:
        meta = json.load(fp)

    brand        = load_brand()

    # ── Merge active product knowledge — this is what feeds Gemini ──────────────
    # If no active_product_id sent, fall back to the saved active_product_id
    if active_product_id is None:
        active_product_id = brand.get("active_product_id")
        if active_product_id:
            active_product_id = int(active_product_id)

    if active_product_id is not None:
        products = brand.get("products", [])
        active_prod = next((p for p in products if p.get("id") == active_product_id), None)
        if active_prod:
            if active_prod.get("product_url"): brand["product_url"] = active_prod["product_url"]
            if active_prod.get("knowledge"):   brand["knowledge"]   = active_prod["knowledge"]
            brand["_active_product_name"] = active_prod.get("name", "")
            kb = active_prod.get("knowledge", {})
            filled = [k for k in ("product","audience","benefits","proof","objections","competitors")
                      if (kb.get(k) or "").strip()]
            print(f"[generate] Product: '{active_prod.get('name')}' | "
                  f"Knowledge sections: {filled} | idx={idx}")
        else:
            print(f"[generate] WARNING: active_product_id={active_product_id} not found in products list")
    else:
        print(f"[generate] WARNING: no active_product_id — no product knowledge will be used!")
    ad_paths     = meta.get("ad_paths", [])
    product_path = meta.get("product_path", "")

    # ── Resolve reference images ──
    # Priority: selected templates > session-uploaded ads > brand creative refs
    tpl_map  = {t["id"]: t for t in load_templates()}
    tpl_paths = [tpl_map[tid]["path"] for tid in template_ids
                 if tid in tpl_map and os.path.exists(tpl_map[tid]["path"])]

    brand_creatives     = [p for p in brand.get("creative_refs",[])  if os.path.exists(p)]
    brand_product_photos= [p for p in brand.get("product_photos",[]  ) if os.path.exists(p)]

    # Per-product photos take priority over global product_photos pool
    all_products = brand.get("products", [])
    if active_product_id is not None:
        active_prod = next((p for p in all_products if p.get("id") == active_product_id), None)
        if active_prod and active_prod.get("photos"):
            candidate = [p for p in active_prod["photos"] if os.path.exists(p)]
            if candidate:
                brand_product_photos = candidate
            else:
                # Active product photos don't exist on disk — fall through to any product with photos
                active_prod = None

    # If still no photos, search ALL products for one that has photos
    if not brand_product_photos:
        for prod in all_products:
            candidate = [p for p in prod.get("photos", []) if os.path.exists(p)]
            if candidate:
                brand_product_photos = candidate
                print(f"[generate] Using photos from product '{prod.get('name')}' as fallback")
                break

    # Style references: selected templates first, then session uploads, then brand creatives
    style_refs = tpl_paths or ad_paths or brand_creatives
    # Cycle so each slot uses a different winning ad reference
    style_ref  = [style_refs[(idx-1) % len(style_refs)]] if style_refs else []

    # Product ref: session upload > active product photos (pick largest = best quality)
    if brand_product_photos:
        best_photo = max(brand_product_photos, key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0)
    else:
        best_photo = ""
    product_ref = product_path or best_photo
    print(f"[generate] Using product photo: {Path(product_ref).name if product_ref else 'NONE'}")

    # ── CRITICAL: product photo goes FIRST so the model weights it highest ──
    # Order: [product_photo, winning_ad_style_ref]
    # The model treats the first image as the primary subject
    print(f"[generate idx={idx}] product_ref={product_ref}  style_refs={style_ref}")
    if not product_ref:
        print("[generate] WARNING: no product photo found — check Brand Setup → Assets")
    # Send BOTH product photo AND winning ad to Gemini
    # Order: [winning ad first = primary layout reference, product photo second]
    # Winning ad tells Gemini the visual design. Product photo tells it what to show.
    ref_images = []
    for sr in style_ref:
        if sr and os.path.exists(sr):
            ref_images.append(sr)        # winning ad first
    if product_ref and os.path.exists(product_ref):
        ref_images.append(product_ref)  # product photo second

    problem_ctx = PROBLEM_CONTEXT.get(problem, problem)

    # ── Detect framework from selected template's section label ───────────────────
    # This controls what copy structure and PIL layout to use
    detected_framework = "BENEFIT"  # safe default — single column, no VS
    if style_ref:
        tpls = load_templates()
        selected_tpl = next(
            (t for t in tpls if t.get("path") == style_ref[0]), None
        )
        if selected_tpl:
            section = (selected_tpl.get("section") or "").lower()
            if any(k in section for k in ["us vs them", "versus", "compare", "perbandingan"]):
                detected_framework = "US_VS_THEM"
            elif any(k in section for k in ["testimonial", "testimoni", "review"]):
                detected_framework = "TESTIMONIAL"
            elif any(k in section for k in ["rage", "problem", "hook", "masalah"]):
                detected_framework = "PROBLEM_HOOK"
            elif any(k in section for k in ["benefit", "faedah", "stat", "product detail"]):
                detected_framework = "BENEFIT"
            else:
                detected_framework = "BENEFIT"
            print(f"[framework] Template section='{section}' → framework={detected_framework}")

    # ── SPEED: copy generated ONCE per session, cached for all images ────────────
    # Copy data is stored in session meta after first image — all others reuse it.
    # This saves ~10-20s per image when generating 6 at once.
    session_meta_path = session_dir / "meta.json"
    cached_copy = meta.get("cached_copy_data")

    if cached_copy:
        # Reuse copy from previous image in this batch
        copy_data = cached_copy
        # Still apply manual overrides per-image if user provided them
        if copy_h1:  copy_data["h1"]  = copy_h1
        if copy_h2:  copy_data["h2"]  = copy_h2
        if copy_cta: copy_data["cta"] = copy_cta
        print(f"[copy] Reusing cached copy for idx={idx} — skipping text model call")
    else:
        # First image in batch — fetch web facts + generate copy, then cache

        # ── ALWAYS fetch live product facts from web ──────────────────────────
        # Even when H1/H2/CTA are manually filled, web facts enrich the bullets
        product_url = brand.get("product_url", "") or ""
        if not product_url and active_product_id is not None:
            ap = next((p for p in brand.get("products", []) if p.get("id") == active_product_id), None)
            if ap: product_url = ap.get("product_url", "") or ""

        product_facts = ""
        if product_url:
            print(f"[web-fetch] Checking product page: {product_url}")
            product_facts = fetch_product_facts(product_url)
            if product_facts:
                print(f"[web-fetch] Got {len(product_facts)} chars of live product data")
            else:
                print("[web-fetch] Nothing fetched — using Brand Setup knowledge only")
        else:
            print("[web-fetch] No product URL set — add one in Brand Setup → Products")

        all_copy_manual = bool(copy_h1 and copy_h2 and copy_cta)

        if all_copy_manual:
            # H1/H2/CTA provided — use them exactly, but still generate bullets
            # from Brand Setup + web facts so benefits are always present
            print("[copy] Manual H1/H2/CTA — generating bullets from knowledge + web facts")
            framework_hint = detected_framework
            copy_data = generate_copy_from_knowledge(
                framework_hint, problem, problem_ctx, brand,
                copy_h1=copy_h1, copy_h2=copy_h2, copy_cta=copy_cta,
                product_facts=product_facts
            )
        else:
            framework_hint = detected_framework
            print(f"[copy-gen] Generating copy from Brand Setup + web facts...")
            copy_data = generate_copy_from_knowledge(
                framework_hint, problem, problem_ctx, brand,
                copy_h1=copy_h1, copy_h2=copy_h2, copy_cta=copy_cta,
                product_facts=product_facts
            )
            if quality == "final":
                copy_data = verify_and_clean_copy(copy_data, product_facts)

        # Tag copy_data with framework so build_prompt can use it
        copy_data["_framework"] = detected_framework

        # ── Enforce correct copy structure based on detected framework ──────────
        # If NOT US_VS_THEM: clear left/right columns, move benefits to benefit_bullets
        if detected_framework != "US_VS_THEM":
            benefits = (copy_data.get("benefit_bullets") or
                        copy_data.get("right_bullets")   or [])
            copy_data["left_bullets"]   = []    # no comparison left column
            copy_data["right_bullets"]  = []    # no comparison right column
            copy_data["left_header"]    = ""
            copy_data["right_header"]   = ""
            copy_data["benefit_bullets"]= benefits
            print(f"[framework] {detected_framework} — using single-column benefits: {benefits[:2]}")

        # Cache in session meta so all subsequent images in this batch skip copy gen
        meta["cached_copy_data"] = copy_data
        with open(session_meta_path, "w") as fp:
            json.dump(meta, fp)

    # ── Step 2: Analyse winning ad COLORS/STYLE — described in text, not sent as image ──
    # The winning ad image is NOT sent to Gemini (it copies text from it).
    # Instead, vision model reads it and produces a text description of colors/layout
    # which goes into the prompt — Gemini uses the description, not the image itself.
    product_visual  = ""
    layout_analysis = ""

    # Vision analysis removed — prompt is now short and clear, no extra description needed

    # ── Step 3: Get extracted style from selected template ────────────────────────
    extracted_style = None
    if style_ref:
        tpls = load_templates()
        selected_tpl = next((t for t in tpls if t.get("path") == style_ref[0]), None)
        if selected_tpl:
            extracted_style = selected_tpl.get("extracted_style")
            if not extracted_style:
                # Extract now if not done at upload time
                print(f"[StyleExtract] Running on-demand for template {selected_tpl.get('id')}")
                extracted_style = extract_winning_ad_style(style_ref[0])
                if extracted_style:
                    selected_tpl["extracted_style"] = extracted_style
                    save_templates(tpls)
        else:
            # Session-uploaded winning ad (not in Template Library) — extract style
            # directly so the vibe (colors/mood/layout) still gets locked in.
            print(f"[StyleExtract] Running on-demand for session-uploaded winning ad")
            extracted_style = extract_winning_ad_style(style_ref[0])

    # ── Step 4: Build prompt with extracted style ────────────────────────────────
    prompt = build_prompt(layout_analysis, product_visual, problem, problem_ctx,
                          bool(style_refs), bool(product_ref), brand,
                          copy_h1=copy_h1, copy_h2=copy_h2, copy_cta=copy_cta,
                          copy_data=copy_data,
                          vibe_idx=idx - 1,
                          extracted_style=extracted_style,
                          skip_overlay=skip_overlay)

    # Pass winning_ad and product separately so Gemini knows exactly what each image is
    winning_ad_img  = style_ref[0] if style_ref else None
    product_img_ref = product_ref if product_ref else None

    # NOTE: gemini-2.5-flash-image (DRAFT_MODEL) frequently ignores the winning-ad
    # reference image entirely for style transfer. nano-banana-pro-preview handles
    # multi-image composition / style replication far more reliably, so use it
    # for ALL generations (not just "final") whenever a winning ad is provided.
    if quality == "final" or winning_ad_img:
        img_bytes, err = call_gemini_image(prompt, ref_images, FINAL_MODEL,
                                           winning_ad_path=winning_ad_img,
                                           product_image_path=product_img_ref)
        if err:
            img_bytes, err = call_gemini_image(prompt, ref_images, DRAFT_MODEL,
                                               winning_ad_path=winning_ad_img,
                                               product_image_path=product_img_ref)
    else:
        img_bytes, err = call_gemini_image(prompt, ref_images, DRAFT_MODEL,
                                           winning_ad_path=winning_ad_img,
                                           product_image_path=product_img_ref)
        if err:
            img_bytes, err = call_gemini_image(prompt, ref_images, DRAFT_FALL,
                                               winning_ad_path=winning_ad_img,
                                               product_image_path=product_img_ref)

    if err:
        return jsonify({"error": err}), 500

    out_name = f"img_{idx:02d}_{quality}.png"
    out_path = session_dir / out_name
    with open(out_path, "wb") as fp:
        fp.write(img_bytes)

    # ── Save clean Gemini visual (no text) as background reference ───────────────
    bg_name = out_name.replace(".png", "_bg.png")
    bg_path = session_dir / bg_name
    shutil.copy2(str(out_path), str(bg_path))

    # ── PIL text overlay — text content from product knowledge (copy_data),
    # placement/size/color/case biased toward the winning ad's extracted_style ─
    # Gemini renders headline/subheadline/CTA directly into the image now.
    has_copy = False
    if has_copy:
        text_path = str(out_path).replace(".png", "_t.png")
        ok = apply_text_overlay(str(out_path), copy_data, text_path,
                                img_idx=idx - 1,
                                brand_colors={
                                    "primary_color":   brand.get("primary_color",   "#F5A800"),
                                    "secondary_color": brand.get("secondary_color", "#D94F00"),
                                },
                                layout_template=(idx - 1) % 3,
                                extracted_style=extracted_style)
        if ok and os.path.exists(text_path):
            shutil.move(text_path, str(out_path))
            print(f"[pil] Text rendered onto image — zero duplicates guaranteed")
        else:
            print("[pil] Overlay skipped (no copy data or PIL error)")

    # Auto-save to Board
    board     = load_board()
    bid       = max((b["id"] for b in board), default=0) + 1
    b_filename= f"board_{bid:05d}.png"
    b_dest    = BOARD_DIR / b_filename
    shutil.copy2(str(out_path), str(b_dest))
    prod_name = brand.get("_active_product_name", "")
    board.insert(0, {
        "id":           bid,
        "filename":     b_filename,
        "label":        f"Ad {idx}" + (f" — {prod_name}" if prod_name else ""),
        "problem":      problem,
        "quality":      quality,
        "session_id":   session_id,
        "campaign_id":  campaign_id,
        "campaign_name":campaign_name,
        "created_at":   __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "copy_data":    copy_data if isinstance(copy_data, dict) else {},
    })
    # Update campaign image count
    if campaign_id:
        camps = load_campaigns()
        for c in camps:
            if c["id"] == campaign_id:
                c["image_count"] = c.get("image_count", 0) + 1
                break
        save_campaigns(camps)
    save_board(board)

    # ── STEP 5: Match score — non-fatal, skip silently if it fails ────────────────
    match_result = None
    if extracted_style and quality == "draft":   # only score drafts to save cost
        match_result = score_generated_ad(extracted_style, str(out_path))

    return jsonify({
        "image_url":    f"/api/image/{session_id}/{out_name}",
        "board_url":    f"/api/board/image/{b_filename}",
        "board_id":     bid,
        "filename":     out_name,
        "label":        f"Ad {idx}",
        "cost_usd":     COSTS[quality],
        "cost_myr":     round(COSTS[quality] * MYR_RATE, 2),
        "match_score":  match_result.get("match_score")      if match_result else None,
        "mismatch":     match_result.get("biggest_mismatch") if match_result else None,
        "verdict":      match_result.get("verdict")          if match_result else None,
    })

# ── Campaigns ────────────────────────────────────────────────────────────────
CAMPAIGNS_JSON = SKILLS_DIR / "campaigns.json"

def load_campaigns() -> list:
    if CAMPAIGNS_JSON.exists():
        with open(CAMPAIGNS_JSON, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_campaigns(items: list):
    with open(CAMPAIGNS_JSON, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)

@app.route("/api/campaigns", methods=["GET"])
def get_campaigns():
    return jsonify(load_campaigns())

@app.route("/api/campaigns/create", methods=["POST"])
def create_campaign():
    data  = request.json or {}
    name  = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    camps = load_campaigns()
    cid   = max((c["id"] for c in camps), default=0) + 1
    entry = {
        "id":         cid,
        "name":       name,
        "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "image_count": 0,
    }
    camps.insert(0, entry)
    save_campaigns(camps)
    return jsonify({"ok": True, "campaign": entry})

@app.route("/api/campaigns/rename", methods=["POST"])
def rename_campaign():
    data  = request.json or {}
    cid   = int(data.get("id", 0))
    name  = data.get("name", "").strip()
    camps = load_campaigns()
    for c in camps:
        if c["id"] == cid:
            c["name"] = name
            break
    save_campaigns(camps)
    return jsonify({"ok": True})

@app.route("/api/campaigns/delete", methods=["POST"])
def delete_campaign():
    data  = request.json or {}
    cid   = int(data.get("id", 0))
    camps = load_campaigns()
    camps = [c for c in camps if c["id"] != cid]
    save_campaigns(camps)
    return jsonify({"ok": True})

# ── Board — persistent creative storage ──────────────────────────────────────
BOARD_DIR  = SKILLS_DIR / "board"
BOARD_JSON = BOARD_DIR  / "board.json"
BOARD_DIR.mkdir(exist_ok=True)

def load_board() -> list:
    if BOARD_JSON.exists():
        with open(BOARD_JSON, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_board(items: list):
    with open(BOARD_JSON, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)

@app.route("/api/board", methods=["GET"])
def get_board():
    board = load_board()
    cid   = request.args.get("campaign_id")
    if cid:
        board = [b for b in board if str(b.get("campaign_id","")) == cid]
    return jsonify(board)

@app.route("/api/board/add", methods=["POST"])
def board_add():
    """Copy a generated image into the board folder and register it."""
    data      = request.json or {}
    src_path  = data.get("src_path", "")   # absolute path to session image
    label     = data.get("label", "Ad")
    problem   = data.get("problem", "")
    quality   = data.get("quality", "draft")
    session_id= data.get("session_id", "")

    if not src_path or not Path(src_path).exists():
        return jsonify({"error": "Source file not found"}), 400

    board    = load_board()
    bid      = max((b["id"] for b in board), default=0) + 1
    filename = f"board_{bid:05d}.png"
    dest     = BOARD_DIR / filename
    shutil.copy2(src_path, str(dest))

    entry = {
        "id":         bid,
        "filename":   filename,
        "label":      label,
        "problem":    problem,
        "quality":    quality,
        "session_id": session_id,
        "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    board.insert(0, entry)
    save_board(board)
    return jsonify({"ok": True, "entry": entry,
                    "url": f"/api/board/image/{filename}"})

@app.route("/api/board/delete", methods=["POST"])
def board_delete():
    data = request.json or {}
    ids  = set(int(i) for i in data.get("ids", []))
    board = load_board()
    to_del = [b for b in board if b["id"] in ids]
    for b in to_del:
        try: (BOARD_DIR / b["filename"]).unlink(missing_ok=True)
        except Exception: pass
    board = [b for b in board if b["id"] not in ids]
    save_board(board)
    return jsonify({"ok": True, "deleted": len(to_del)})

@app.route("/api/board/image/<filename>")
def board_image(filename):
    return send_from_directory(BOARD_DIR, filename)

@app.route("/api/board/variants/<int:board_id>")
def board_variants(board_id):
    """List previously generated tweak-variant files for a board item."""
    board = load_board()
    item  = next((b for b in board if b["id"] == board_id), None)
    if not item:
        return jsonify({"items": []})
    stem = Path(item["filename"]).stem
    files = sorted(BOARD_DIR.glob(f"{stem}_v*.png"))
    items = [{"filename": f.name, "board_url": f"/api/board/image/{f.name}"} for f in files]
    return jsonify({"items": items})

@app.route("/api/board/regenerate", methods=["POST"])
def board_regenerate():
    """Take an existing board image + a user 'tweak' instruction and ask Gemini
    to produce new variation(s) that fix/adjust it accordingly."""
    data     = request.json or {}
    board_id = int(data.get("id", 0))
    tweak    = (data.get("tweak") or "").strip()
    count    = max(1, min(int(data.get("count", 1)), 4))
    quality  = data.get("quality", "draft")

    if not tweak:
        return jsonify({"error": "Please describe what to change"}), 400

    board = load_board()
    item  = next((b for b in board if b["id"] == board_id), None)
    if not item:
        return jsonify({"error": "Board item not found"}), 404

    src_path = BOARD_DIR / item["filename"]
    if not src_path.exists():
        return jsonify({"error": "Source image not found"}), 404

    brand = load_brand()
    active_product_id = brand.get("active_product_id")
    if active_product_id is not None:
        active_product_id = int(active_product_id)
    all_products = brand.get("products", [])
    if active_product_id is not None:
        active_prod = next((p for p in all_products if p.get("id") == active_product_id), None)
        if active_prod:
            if active_prod.get("product_url"): brand["product_url"] = active_prod["product_url"]
            if active_prod.get("knowledge"):   brand["knowledge"]   = active_prod["knowledge"]
            brand["_active_product_name"] = active_prod.get("name", "")

    # Resolve product photo (per-product first, then any product, then global pool)
    brand_product_photos = [p for p in brand.get("product_photos", []) if os.path.exists(p)]
    if active_product_id is not None:
        active_prod = next((p for p in all_products if p.get("id") == active_product_id), None)
        if active_prod and active_prod.get("photos"):
            cand = [p for p in active_prod["photos"] if os.path.exists(p)]
            if cand:
                brand_product_photos = cand
    if not brand_product_photos:
        for prod in all_products:
            cand = [p for p in prod.get("photos", []) if os.path.exists(p)]
            if cand:
                brand_product_photos = cand
                break
    product_ref = (max(brand_product_photos, key=lambda p: os.path.getsize(p))
                   if brand_product_photos else "")

    problem     = item.get("problem", "Post-partum")
    problem_ctx = PROBLEM_CONTEXT.get(problem, problem)
    copy_data   = item.get("copy_data") or {}

    ref_images = [str(src_path)]
    if product_ref and os.path.exists(product_ref):
        ref_images.append(product_ref)

    brand_block = brand_to_prompt_block(brand)
    product_clause = ""
    if product_ref and os.path.exists(product_ref):
        product_clause = (
            "\n\nPRODUCT REFERENCE (IMAGE 2): If the product bottle/packaging is visible in the ad, "
            "it MUST match IMAGE 2 exactly — same label, colors, text, cap, and bottle shape. "
            "Do not invent or substitute a different product."
        )

    prompt = f"""The attached IMAGE 1 is a contact sheet showing several ad concepts arranged in a grid.
It is a REFERENCE ONLY for style, vibe, background, colors, product placement, and tone.

{brand_block}

━━━ YOUR TASK ━━━
Create ONE brand-new advertisement image (a single visual, NOT a grid, NOT a collage, NOT multiple
panels) that matches the same overall style/vibe/colors/background/product placement as the concepts
in IMAGE 1, but is a fresh variation per this instruction:
{tweak}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{product_clause}

OUTPUT RULES:
- Output is exactly ONE advertisement visual — a single scene, single headline, single CTA
- Do NOT reproduce IMAGE 1's grid/panel structure — that is reference material only, not the layout to copy
- 4:5 aspect ratio
- No watermarks or signatures
- Write any on-image text in Bahasa Malaysia, clean and legible"""

    results = []
    for _ in range(count):
        if quality == "final":
            img_bytes, err = call_gemini_image(prompt, ref_images, FINAL_MODEL,
                                                winning_ad_path=str(src_path),
                                                product_image_path=product_ref or None)
            if err:
                img_bytes, err = call_gemini_image(prompt, ref_images, DRAFT_MODEL,
                                                    winning_ad_path=str(src_path),
                                                    product_image_path=product_ref or None)
        else:
            img_bytes, err = call_gemini_image(prompt, ref_images, DRAFT_MODEL,
                                                winning_ad_path=str(src_path),
                                                product_image_path=product_ref or None)
            if err:
                img_bytes, err = call_gemini_image(prompt, ref_images, DRAFT_FALL,
                                                    winning_ad_path=str(src_path),
                                                    product_image_path=product_ref or None)
        if err or not img_bytes:
            continue

        # Save as a variant file alongside the original — NOT added to the
        # main board grid, so the original board item stays unchanged.
        variant_idx = len(list(BOARD_DIR.glob(f"{src_path.stem}_v*.png"))) + 1
        v_filename  = f"{src_path.stem}_v{variant_idx}.png"
        with open(BOARD_DIR / v_filename, "wb") as fp:
            fp.write(img_bytes)

        results.append({"filename": v_filename,
                         "board_url": f"/api/board/image/{v_filename}",
                         "label": (item.get("label", "Ad") + " (edit)")})

    if not results:
        return jsonify({"error": "Generation failed — please try again"}), 500
    cost_usd = round(COSTS.get(quality, COSTS["draft"]) * len(results), 2)
    return jsonify({"ok": True, "items": results,
                     "cost_usd": cost_usd, "cost_myr": round(cost_usd * MYR_RATE, 2)})

@app.route("/api/board/delete-variant", methods=["POST"])
def board_delete_variant():
    """Delete a generated tweak-variant image file (not a real board entry)."""
    data     = request.json or {}
    filename = (data.get("filename") or "").strip()
    if not filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    path = BOARD_DIR / filename
    if path.exists():
        path.unlink()
    return jsonify({"ok": True})

# ── Product management ────────────────────────────────────────────────────────
@app.route("/api/brand-kit/products", methods=["GET"])
def get_products():
    brand = load_brand()
    return jsonify(brand.get("products", []))

@app.route("/api/brand-kit/products/save", methods=["POST"])
def save_products():
    """Save full products list."""
    data = request.json or {}
    brand = load_brand()
    brand["products"] = data.get("products", [])
    brand["active_product_id"] = data.get("active_product_id", None)
    save_brand(brand)
    return jsonify({"ok": True})

def test_product_image():  # kept as debug utility (no route, called manually)
    """
    FIX 4: Test that the product image is being sent to Gemini correctly.
    Sends the product image and asks Gemini to describe what it sees.
    Returns Gemini's description — should say 'Jeli Gamat' if image is correct.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 400

    data        = request.json or {}
    product_id  = data.get("active_product_id")
    brand       = load_brand()

    # Find product photo
    product_photo = ""
    if product_id:
        products = brand.get("products", [])
        ap = next((p for p in products if p.get("id") == product_id), None)
        if ap:
            photos = [p for p in ap.get("photos", []) if os.path.exists(p)]
            if photos:
                product_photo = photos[0]

    if not product_photo:
        # Fall back to global product photos
        global_photos = [p for p in brand.get("product_photos", []) if os.path.exists(p)]
        if global_photos:
            product_photo = global_photos[0]

    if not product_photo:
        return jsonify({"error": "No product photo found. Upload one in Brand Setup → Assets."}), 400

    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{VISION_MODEL}:generateContent?key={api_key}")
        b64, mime = img_to_b64(product_photo)
        payload = json.dumps({"contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": b64}},
            {"text": "What product is shown in this image? "
                     "Describe the label text, brand name, and product name exactly as you see them. "
                     "Be specific and literal."}
        ]}]}).encode()
        req = urllib.request.Request(url, data=payload,
              headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        parts = body.get("candidates",[{}])[0].get("content",{}).get("parts",[])
        description = next((p["text"] for p in parts if "text" in p), "No description")
        print(f"[ProductTest] Image: {Path(product_photo).name}")
        print(f"[ProductTest] Gemini says: {description[:200]}")
        return jsonify({
            "ok": True,
            "image_path": product_photo,
            "image_name": Path(product_photo).name,
            "gemini_description": description,
            "pass": any(k in description.lower() for k in ["jeli", "gamat", "serigama", "luxor"])
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/set-key", methods=["POST"])
def set_api_key():
    key = (request.json or {}).get("key", "").strip()
    if not key:
        return jsonify({"error": "Empty key"}), 400
    os.environ["GEMINI_API_KEY"] = key
    return jsonify({"ok": True, "hint": key[:8] + "…"})

@app.route("/api/key-status")
def key_status():
    key = os.environ.get("GEMINI_API_KEY", "")
    return jsonify({"set": bool(key), "hint": (key[:8] + "…") if key else ""})

# ── Canva Connect API (Personal Access Token) ───────────────────────────────────
CANVA_API = "https://api.canva.com/rest/v1"

def _get_canva_token() -> str:
    """Return stored Canva PAT — checks env var first, then .env file."""
    token = os.environ.get("CANVA_API_TOKEN", "").strip()
    if not token:
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("CANVA_API_TOKEN="):
                        token = line.split("=", 1)[1].strip()
                        os.environ["CANVA_API_TOKEN"] = token
                        break
    return token

@app.route("/api/canva-token", methods=["POST"])
def api_set_canva_token():
    """Save a Canva Connect API Personal Access Token to .env and env var."""
    token = (request.json or {}).get("token", "").strip()
    if not token:
        return jsonify({"error": "Token is empty"}), 400
    env_path = Path(__file__).parent / ".env"
    lines = []
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            lines = [l for l in f.readlines() if not l.startswith("CANVA_API_TOKEN")]
    lines.append(f"CANVA_API_TOKEN={token}\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.environ["CANVA_API_TOKEN"] = token
    return jsonify({"ok": True, "hint": token[:10] + "…"})

@app.route("/api/canva-token", methods=["DELETE"])
def api_delete_canva_token():
    """Remove the stored Canva PAT (disconnect)."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            lines = [l for l in f.readlines() if not l.startswith("CANVA_API_TOKEN")]
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    os.environ.pop("CANVA_API_TOKEN", None)
    return jsonify({"ok": True})

@app.route("/api/canva-token-status")
def api_canva_token_status():
    """Check if a Canva PAT is stored and currently valid."""
    token = _get_canva_token()
    if not token:
        return jsonify({"connected": False})
    try:
        req = urllib.request.Request(
            f"{CANVA_API}/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        display = (data.get("team_user", {}).get("user_id")
                   or data.get("display_name")
                   or token[:8] + "…")
        return jsonify({"connected": True, "display": display, "hint": token[:10] + "…"})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return jsonify({"connected": False, "error": "Token expired or invalid"})
        return jsonify({"connected": True, "hint": token[:10] + "…"})
    except Exception:
        return jsonify({"connected": True, "hint": token[:10] + "…"})

@app.route("/api/canva-queue", methods=["POST"])
def api_canva_queue():
    """Upload a generated image to the connected Canva account's asset library."""
    import base64 as _b64, time as _time

    data       = request.json or {}
    session_id = data.get("session_id", "").strip()
    filename   = data.get("filename",   "").strip()
    label      = (data.get("label", filename) or "Serigama Ad").strip()[:50]

    if not filename:
        return jsonify({"error": "Missing filename"}), 400

    img_path = SESSIONS_DIR / session_id / filename if session_id else None
    if not img_path or not img_path.exists():
        img_path = BOARD_DIR / filename
        if not img_path.exists():
            return jsonify({"error": f"Image not found: {filename}"}), 404

    token = _get_canva_token()
    if not token:
        return jsonify({
            "error": "Canva not connected. Go to Brand Setup → Brand tab → paste your Canva Connect API token."
        }), 400

    with open(img_path, "rb") as f:
        img_bytes = f.read()

    name_b64 = _b64.b64encode(label.encode()).decode()
    metadata = json.dumps({"name_base64": name_b64})
    req = urllib.request.Request(
        f"{CANVA_API}/asset-uploads",
        data=img_bytes,
        method="POST",
        headers={
            "Authorization":         f"Bearer {token}",
            "Content-Type":          "application/octet-stream",
            "Asset-Upload-Metadata": metadata,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            job_data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 401:
            return jsonify({"error": "Canva token expired or invalid. Reconnect in Brand Setup."}), 401
        return jsonify({"error": f"Canva upload failed ({e.code}): {body[:300]}"}), 500
    except Exception as exc:
        return jsonify({"error": f"Upload failed: {exc}"}), 500

    job    = job_data.get("job", job_data)
    job_id = job.get("id", "")
    status = job.get("status", "")

    # Poll briefly if still in progress
    deadline = _time.time() + 10
    while status not in ("success", "failed") and job_id and _time.time() < deadline:
        try:
            r = urllib.request.Request(f"{CANVA_API}/asset-uploads/{job_id}",
                                        headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(r, timeout=8) as resp:
                job = json.loads(resp.read()).get("job", job)
            status = job.get("status", status)
            if status in ("success", "failed"):
                break
        except Exception:
            break
        _time.sleep(1.5)

    if status == "failed":
        return jsonify({"error": f"Canva upload failed: {job.get('error', {})}"}), 500

    return jsonify({
        "ok": True, "job_id": job_id, "label": label,
        "message": f"✅ '{label}' is now in your Canva library!",
    })

if __name__ == "__main__":
    import webbrowser, threading

    # ── Load API key from .env file on startup (survives server restarts) ──────
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        with open(env_file, encoding="utf-8") as ef:
            for line in ef:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if not os.environ.get(k.strip()):  # don't override if already set
                        os.environ[k.strip()] = v.strip()
        print("[startup] Loaded API keys from .env")

    if not os.environ.get("GEMINI_API_KEY"):
        print("WARNING: GEMINI_API_KEY not set")

    print(f"\n  static-remix UI  ->  http://localhost:{PORT}\n")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    app.run(host="0.0.0.0", port=PORT, debug=False)
