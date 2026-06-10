"""Remove all Canva-related code from ui.html."""
import re

path = r'C:\Users\haliza.LUXOR\.claude\skills\static-remix\ui.html'
with open(path, encoding='utf-8') as f:
    content = f.read()

original_len = len(content)

# 1. Remove Canva CSS blocks
canva_css_patterns = [
    r'/\* Canva button on board cards \*/.*?(?=/\*|\n\n)',
    r'/\* Canva modal \*/.*?(?=/\*|\n\n)',
    r'\.btn-canva\{.*?\}',
    r'\.btn-canva:hover\{.*?\}',
    r'\.btn-canva\.has-url\{.*?\}',
    r'\.btn-canva\.loading\{.*?\}',
    r'@keyframes canvaPulse\{.*?\}',
    r'\.canva-modal-overlay\{.*?\}',
    r'\.canva-modal-overlay\.show\{.*?\}',
    r'\.canva-modal\{.*?\}',
    r'\.canva-modal-header\{.*?\}',
    r'\.canva-modal-header h2\{.*?\}',
    r'\.canva-modal-header p\{.*?\}',
    r'\.canva-modal-body\{.*?\}',
    r'\.canva-step\{.*?\}',
    r'\.canva-step-num\{.*?\}',
    r'\.canva-step-text\{.*?\}',
    r'\.canva-step-text strong\{.*?\}',
    r'\.canva-copy-preview\{.*?\}',
    r'\.canva-modal-footer\{.*?\}',
    r'\.btn-canva-copy\{.*?\}',
    r'\.btn-canva-copy:hover\{.*?\}',
    r'\.btn-canva-open\{.*?\}',
    r'\.btn-canva-open:hover\{.*?\}',
    r'\.btn-canva-open:disabled\{.*?\}',
    r'\.canva-url-ready\{.*?\}',
    r'\.canva-url-ready\.show\{.*?\}',
    r'\.canva-url-ready a\{.*?\}',
    r'\.canva-url-ready a:hover\{.*?\}',
    r'\.copy-counter\.warn\{.*?\}',
    r'\.copy-counter\.full\{.*?\}',
]

for pat in canva_css_patterns:
    content = re.sub(pat, '', content, flags=re.DOTALL)

# 2. Remove Canva modal HTML block
content = re.sub(
    r'<!-- ── Canva Export Modal ── -->.*?</div><!-- /canva.*?-->',
    '', content, flags=re.DOTALL
)
content = re.sub(
    r'<!-- ── Canva Export Modal ── -->.*?</div>\s*\n\s*<!-- ──',
    '<!-- ──', content, flags=re.DOTALL
)

# 3. Remove Canva button from board cards (the whole div containing it)
content = re.sub(
    r"\$\{b\.canva_url\s*\?.*?`\}\s*\n",
    '', content, flags=re.DOTALL
)
content = re.sub(
    r"<div id=\"canva-btn-wrap-.*?</div>",
    '', content, flags=re.DOTALL
)

# 4. Remove Canva JS function blocks
canva_js_patterns = [
    r'// ── Canva integration.*?(?=// ──)',
    r'let _canvaPollers.*?(?=// ──)',
    r'async function requestCanvaDesign.*?(?=\nfunction |\nasync function |\n// )',
    r'function startCanvaPolling.*?(?=\nfunction |\nasync function |\n// )',
    r'function updateCanvaBtn.*?(?=\nfunction |\nasync function |\n// )',
    r'async function saveCanvaUrl.*?(?=\nfunction |\nasync function |\n// )',
    r'async function resumeCanvaPolling.*?(?=\nfunction |\nasync function |\n// )',
    r'async function loadStyleReading.*?(?=\nfunction |\nasync function |\n// )',
    r'async function saveCanvaToken.*?(?=\nfunction |\nasync function |\n// )',
    r'async function checkCanvaTokenStatus.*?(?=\nfunction |\nasync function |\n// )',
]
for pat in canva_js_patterns:
    content = re.sub(pat, '', content, flags=re.DOTALL)

# 5. Remove Canva calls in other functions
content = re.sub(r'\s*resumeCanvaPolling\(\);\s*\n', '\n', content)
content = re.sub(r'\s*await checkCanvaTokenStatus\(\);\s*\n', '\n', content)
content = re.sub(r'\s*loadStyleReading\(firstId\);\s*\n', '\n', content)
content = re.sub(r'\s*const firstId = \[\.\.\.selectedTplIds\]\[0\];\s*\n', '\n', content)

# 6. Remove style-reading-card HTML
content = re.sub(
    r"<!-- STEP 3: Style Reading card.*?</div>\s*\n",
    '', content, flags=re.DOTALL
)
content = re.sub(
    r'<div id="style-reading-card".*?</div>\s*\n',
    '', content, flags=re.DOTALL
)

# 7. Clean up empty lines left over
content = re.sub(r'\n{4,}', '\n\n\n', content)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"Done. Removed {original_len - len(content)} chars of Canva UI code.")

# Check key terms are gone
checks = ['canva-modal', 'btn-canva', 'requestCanvaDesign', 'Open in Canva',
          'resumeCanvaPolling', 'loadStyleReading', 'style-reading-card']
for c in checks:
    if c in content:
        print(f"WARNING: still found '{c}'")
    else:
        print(f"OK: removed '{c}'")
