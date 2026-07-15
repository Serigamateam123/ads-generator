import fitz
import json, os, re
from collections import Counter, defaultdict

pdf_path = r"C:\Users\haliza.LUXOR\Downloads\Winning Ads.pdf"
run_dir = r"C:\Users\haliza.LUXOR\.claude\skills\static-remix\runs\20260529-1006"
extracted_dir = os.path.join(run_dir, "extracted")
os.makedirs(extracted_dir, exist_ok=True)

doc = fitz.open(pdf_path)
print(f"PDF pages: {len(doc)}")

# --- Auto-detect heading font ---
# Count how many distinct pages each font appears on (in short spans < 60 chars)
font_page_sets = defaultdict(set)
font_sizes = defaultdict(list)

for page in doc:
    blocks = page.get_text("dict")["blocks"]
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span["text"].strip()
                if 0 < len(text) < 60:
                    font = span["font"]
                    size = span["size"]
                    font_page_sets[font].add(page.number)
                    font_sizes[font].append(size)

# Heading font = most pages with short spans AND largest average font size (break ties by size)
# First find fonts on 3+ pages, then pick largest average size
candidates = {f: pages for f, pages in font_page_sets.items() if len(pages) >= 3}
if candidates:
    heading_font = max(candidates, key=lambda f: (len(font_page_sets[f]), sum(font_sizes[f])/len(font_sizes[f])))
else:
    heading_font = max(font_page_sets, key=lambda f: sum(font_sizes[f])/len(font_sizes[f]))

avg_heading_size = sum(font_sizes[heading_font]) / len(font_sizes[heading_font])
print(f"Heading font: {heading_font} (avg size: {avg_heading_size:.1f}pt, on {len(font_page_sets[heading_font])} pages)")

# --- Also collect all headings for framework detection ---
# A heading is: heading_font OR large font size (>= avg_heading_size * 0.85), short text
SIZE_THRESHOLD = avg_heading_size * 0.85

all_headings = []
for page_num, page in enumerate(doc):
    blocks = page.get_text("dict")["blocks"]
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span["text"].strip()
                is_heading_font = span["font"] == heading_font
                is_large = span["size"] >= SIZE_THRESHOLD
                if (is_heading_font or is_large) and 2 < len(text) < 60:
                    all_headings.append((page_num, text, span["size"]))

# Show unique headings
unique_headings = sorted(set(h[1] for h in all_headings))
print(f"\nDetected headings ({len(unique_headings)} unique):")
for h in unique_headings:
    safe = h.encode('ascii', errors='replace').decode('ascii')
    print(f"  {safe}")

# --- Extract images with framework labels ---
manifest = []
current_heading = "UNKNOWN"
current_heading_page = -1

# Build a page->headings map
page_headings = defaultdict(list)
for page_num, text, size in all_headings:
    page_headings[page_num].append((size, text))

# Sort headings per page by size desc (largest = most likely the section heading)
for pn in page_headings:
    page_headings[pn].sort(reverse=True)

def safe_framework(text):
    clean = re.sub(r'[^\w\s\-&]', '', text).strip().upper()
    clean = re.sub(r'\s+', '_', clean)
    return clean[:40] if clean else "UNKNOWN"

extracted_count = 0
for page_num, page in enumerate(doc):
    # Update heading if this page has one
    if page_num in page_headings:
        # Use the largest-font text on this page as section heading
        _, heading_text = page_headings[page_num][0]
        current_heading = safe_framework(heading_text)
        current_heading_page = page_num

    img_list = page.get_images(full=True)
    for img_idx, img in enumerate(img_list):
        xref = img[0]
        try:
            base_image = doc.extract_image(xref)
        except Exception as e:
            print(f"  Skip xref {xref} on page {page_num}: {e}")
            continue

        img_bytes = base_image["image"]
        if len(img_bytes) < 5000:  # skip tiny icons/decorations
            continue

        filename = f"{current_heading}_p{page_num:03d}_i{img_idx:02d}.png"
        filepath = os.path.join(extracted_dir, filename)

        with open(filepath, "wb") as f:
            f.write(img_bytes)

        manifest.append({
            "path": filepath,
            "filename": filename,
            "framework": current_heading,
            "page": page_num,
            "size_bytes": len(img_bytes)
        })
        extracted_count += 1

manifest_path = os.path.join(extracted_dir, "manifest.json")
with open(manifest_path, "w") as f:
    json.dump({
        "heading_font": heading_font,
        "avg_heading_size": round(avg_heading_size, 1),
        "images": manifest
    }, f, indent=2)

frameworks = sorted(set(m["framework"] for m in manifest))
print(f"\nExtracted {extracted_count} images.")
print(f"Frameworks found ({len(frameworks)}):")
for fw in frameworks:
    count = sum(1 for m in manifest if m["framework"] == fw)
    print(f"  {fw}: {count} image(s)")
print(f"\nManifest saved to: {manifest_path}")
