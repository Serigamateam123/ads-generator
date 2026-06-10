---
name: static-remix
description: >
  Turns a PDF of winning competitor static ads into on-brand image recreations for your product
  using Nano Banana Pro (Gemini 3 Pro Image Preview). Use this skill whenever the user wants to
  remix competitor ads, generate on-brand static creatives from a PDF, produce ad variations from
  competitor examples, or run a creative production pipeline from a PDF of reference ads.
  Trigger on: /static-remix, "remix these ads", "recreate competitor statics", "generate ad
  variations from PDF", "turn this PDF into ads for my product", "make creatives based on these
  examples". Also trigger when the user mentions a PDF of ads alongside a product URL or brand.
  The skill extracts ad frameworks (US VS THEM, BOLD CLAIM, TESTIMONIAL, etc.), asks four
  required questions, fetches the product photo for brand grounding, writes production briefs,
  generates images via the Gemini API, and outputs a testing report.
---

# static-remix

You are running the static-remix pipeline. Follow every step in order. Never skip steps or silently default any user inputs.

---

## Step 0 — Get the PDF path

If the user hasn't provided a PDF path yet, ask them:

> "What's the path to your PDF of competitor ads? (e.g. ~/Downloads/winning-statics.pdf)"

---

## Step 1 — Create the run folder

Create a dated run folder and required subfolders:

```
~/.claude/skills/static-remix/runs/<YYYYMMDD-HHMM>/
  extracted/
  production/
  teardowns/
  briefs/
```

Use Python or PowerShell to get the current timestamp and create all directories. Save the run folder path — you'll use it throughout.

---

## Step 2 — Extract images from PDF

Write and run a Python script (save it to the run folder as `extract.py`) that does the following:

```python
import fitz  # PyMuPDF
import json, os, re
from collections import Counter

pdf_path = "USER_PDF_PATH"
run_dir = "RUN_DIR"
extracted_dir = os.path.join(run_dir, "extracted")

doc = fitz.open(pdf_path)

# --- Auto-detect heading font ---
# Collect all text spans that are short (< 60 chars) and note their font on each page
font_page_sets = {}  # font_name -> set of page numbers
for page in doc:
    blocks = page.get_text("dict")["blocks"]
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span["text"].strip()
                if 0 < len(text) < 60:
                    font = span["font"]
                    if font not in font_page_sets:
                        font_page_sets[font] = set()
                    font_page_sets[font].add(page.number)

# Heading font = the font appearing on the most distinct pages
heading_font = max(font_page_sets, key=lambda f: len(font_page_sets[f]))

# --- Extract images with framework labels ---
manifest = []
current_heading = "UNKNOWN"

for page_num, page in enumerate(doc):
    blocks = page.get_text("dict")["blocks"]
    
    # Find headings on this page (by font match)
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span["text"].strip()
                if span["font"] == heading_font and 0 < len(text) < 60:
                    current_heading = re.sub(r'[^\w\s-]', '', text).strip().upper().replace(' ', '_')

    # Extract images on this page
    img_list = page.get_images(full=True)
    for img_idx, img in enumerate(img_list):
        xref = img[0]
        base_image = doc.extract_image(xref)
        img_bytes = base_image["image"]
        ext = base_image["ext"]
        
        filename = f"{current_heading}_p{page_num:03d}_i{img_idx:02d}.png"
        filepath = os.path.join(extracted_dir, filename)
        
        with open(filepath, "wb") as f:
            f.write(img_bytes)
        
        manifest.append({"path": filepath, "framework": current_heading, "page": page_num})

with open(os.path.join(extracted_dir, "manifest.json"), "w") as f:
    json.dump({"heading_font": heading_font, "images": manifest}, f, indent=2)

print(f"Extracted {len(manifest)} images. Heading font: {heading_font}")
frameworks = sorted(set(m["framework"] for m in manifest))
print("Frameworks found:", frameworks)
```

Run it with `python extract.py` (or `python3`). Read the output and the manifest to get the list of detected frameworks. If PyMuPDF isn't installed, run `pip install pymupdf` first.

---

## Step 3 — Ask the four required questions

**NEVER skip these. NEVER silently default any of them.** Use `AskUserQuestion` to ask all four at once (or in two batches of two if the tool limits questions per call).

Show the user the detected frameworks from Step 2 before asking question (d).

**The four questions:**

**(a)** Product URL — what's the URL of the product page? (Required, no default)

**(b)** Total images — how many images do you want to generate in total? (e.g. 10, 50, 100)

**(c)** Variations per concept — how many variations per concept? (Usually 2: same framework, one axis changes per variation — e.g. camera angle or overlay wording)

**(d)** Per-framework split — given the detected frameworks above, how many concepts from each? The user can say "even split" or give specific numbers like "20 US_VS_THEM, 10 BOLD_CLAIM, 10 BEFORE_AFTER, 10 TESTIMONIAL".

**After collecting answers:**

1. Compute `concepts = total_images / variations_per_concept`. Verify it's a whole number.
2. Verify that the per-framework concept counts sum to `concepts`. If not, show the mismatch:
   > "Your framework split sums to X concepts but you asked for Y total concepts (Z images ÷ V variations). Please adjust the split so it sums to Y."
   Ask the user to correct it before continuing.
3. Show estimated cost: `total_images × $0.25 = $X.XX`
4. If estimated cost > $10, ask for explicit confirmation before proceeding.

---

## Step 4 — Fetch product page and product photo

1. Use WebFetch (or Bash curl) to fetch the product URL.
2. **CRITICAL — download the actual product photo:**
   - For Shopify: try `<product_url>.json` — this returns a JSON with `product.images[].src` URLs. Download the first (or highest-resolution) image.
   - For other platforms: parse the HTML for `<img>` tags with `product`, `hero`, or `main` in the src, or look for `og:image` meta tags.
   - Save the photo to the run folder as `product_photo.<ext>`.
3. **View the product photo** using the Read tool (pass the image file path). This is non-negotiable — you must see the image.
4. Write a concrete visual brand description based on what you actually see:
   - Package shape and size
   - Bottle/container color
   - Cap/lid color
   - Label typography (font style, color, weight)
   - Capsule/softgel/pill color (if applicable)
   - Brand color palette (background, accent, text colors)
   - Any icons, badges, or graphic elements on the label
5. Save this description to `brand_description.txt` in the run folder.
6. Also save any pricing, offer copy, and product claims from the product page to `product_copy.txt` — you'll need exact numbers for briefs.

---

## Step 5 — Teardown source examples

For each framework the user selected, pick 1–2 representative images from `extracted/manifest.json` that belong to that framework.

For each image:
1. View it with the Read tool.
2. Write a teardown covering:
   - **Framework**: what ad framework this is
   - **Psychology**: why it works (urgency, social proof, contrast, curiosity gap, etc.)
   - **Keep**: visual or copy elements worth carrying over
   - **Swap**: what to replace with our brand's product, colors, copy, and claims

Save each teardown as `teardowns/<FRAMEWORK>_teardown.txt`.

---

## Step 6 — Write production briefs

Write one brief per concept. Recall: `concepts = total_images / variations_per_concept`.

Number them `concept_01`, `concept_02`, etc. within each framework group.

**Each brief must contain:**

```
CONCEPT: concept_NN
FRAMEWORK: <framework name>
VARIATION AXIS: <the one thing that changes between var_01 and var_02, e.g. "camera angle: straight-on vs 45° overhead" or "overlay wording: price vs benefit">

SCENE DESCRIPTION:
<Describe the visual scene in detail — background, lighting, product placement, props, mood>

TEXT OVERLAYS:
Headline: "<exact quoted copy>"
Subhead: "<exact quoted copy>"
Body: "<exact quoted copy>"
CTA: "<exact quoted copy>"

CAPTION (for social):
<Full caption text>

VAR_01 SPECIFICS:
<What's unique to variation 01>

VAR_02 SPECIFICS:
<What's unique to variation 02>
```

**Rules:**
- Pull ALL pricing and offer copy verbatim from `product_copy.txt`. Never invent prices, percentages, or claim numbers.
- Each variation changes exactly one axis (the variation axis). Everything else stays the same.
- The scene description should be specific enough that an image model can produce it consistently.

Save to `briefs/concept_NN_brief.txt`.

---

## Step 7 — Generate images via the UI

### 7a — Launch the 2-stage UI

After writing all briefs, launch the UI server instead of running `generate.py` directly:

```powershell
$env:GEMINI_API_KEY = "YOUR_KEY_HERE"
python "$HOME\.claude\skills\static-remix\ui_server.py" --run-dir "<RUN_DIR>"
```

The UI opens at **http://localhost:7373** and gives you:

| Stage | Model | Cost | When |
|-------|-------|------|------|
| **Draft Preview** | gemini-2.0-flash-preview-image-generation | ~$0.01/image | Click "🔍 Draft Preview" — judge composition cheaply |
| **Final Export** | imagen-3.0-generate-002 | ~$0.04/image | Click "✨ Generate Final" — only after approving the draft |

**Cost-saving rules the UI enforces:**
- Nothing auto-generates — every image requires a manual click
- A confirmation dialog shows the exact cost in USD + MYR before each Final generation
- A running session cost counter is always visible in the top-right corner
- The Download button only appears after a Final image is generated

**Output sizes available in the UI:**
- 1080×1350 — Instagram Vertical (default, 4:5)
- 1080×1920 — Story / Reels (9:16)
- 1080×1080 — Facebook Square (1:1)

### 7b — Ensure the bash helper exists (for manual CLI use)

Check if `~/.claude/skills/static-remix/scripts/gemini-image-ref.sh` exists. If not (or if it needs updating), write it now — the content is in the `scripts/` directory alongside this SKILL.md. Make it executable: `chmod +x gemini-image-ref.sh`.

The script accepts: `prompt` `aspect_ratio` `output_path` [optional: `reference_image_path`]

### 7b — Generate all images sequentially

For each concept × variation:

```bash
bash ~/.claude/skills/static-remix/scripts/gemini-image-ref.sh \
  "<full prompt from brief>" \
  "9:16" \
  "<run_dir>/production/concept_NN_var_MM.png" \
  "<run_dir>/product_photo.jpg"
```

Always pass the product photo as the reference image — this is what keeps brand visuals consistent across all generated images.

Build the prompt from the brief: combine the scene description, text overlays, framework, and variation-specific details into one rich prompt.

**On HTTP 500 errors:** note the failed concept+variation and continue. After all others are done, retry the failures once. Log any that still fail in `report.txt`.

**Aspect ratios to use:**
- Feed / square: `1:1`
- Story / Reel: `9:16`
- Landscape: `16:9`
Default to `9:16` unless the user specified otherwise.

### 7c — Verify outputs

After generation, list `production/` and confirm image files exist and are non-empty.

---

## Step 8 — Write report

Write `report.txt` in the run folder:

```
STATIC-REMIX RUN REPORT
=======================
Date: <YYYYMMDD-HHMM>
PDF: <source pdf path>
Product: <product URL>

IMAGES PRODUCED
---------------
Total generated: X / Y requested
Failed: <list any that failed>

TOP 3 CONCEPTS TO TEST FIRST
------------------------------
1. concept_NN (<framework>) — <one-line reason why this should win>
2. concept_NN (<framework>) — <one-line reason>
3. concept_NN (<framework>) — <one-line reason>

TESTING PLAYBOOK
----------------
Budget per creative: $5–10 for initial 48-hour test
Kill criteria: < 0.5% CTR or < 1 ROAS after $10 spend — pause and move to next
Winner criteria: > 2% CTR or > 2 ROAS — scale budget 2x every 48h
Test one variable at a time; don't change creative and audience simultaneously.

PER-CONCEPT DETAILS
-------------------
<For each concept: concept ID, framework, headline, caption, variation axis, file paths>
```

Base your top-3 picks on: psychological strength of the framework, specificity of the copy, how well the visual description exploits your brand's actual visual identity.

---

## Step 9 — Final summary to user

End with a short chat message (no report dump):

```
Run complete.

📁 Run folder: ~/.claude/skills/static-remix/runs/<YYYYMMDD-HHMM>/
🖼  Images: X generated (Y requested)

Top 3 to test first:
1. concept_NN — <one-line reason>
2. concept_NN — <one-line reason>
3. concept_NN — <one-line reason>

Full details in report.txt.
```

---

## Dependencies

- Python with PyMuPDF: `pip install pymupdf`
- `curl` available in terminal (Git Bash on Windows, or WSL)
- `perl` available (ships with Git for Windows)
- `GEMINI_API_KEY` environment variable set

If any dependency is missing, tell the user what to install before proceeding.
