"""Remove all Canva-related routes and functions from ui_server.py."""
import re

path = r'C:\Users\haliza.LUXOR\.claude\skills\static-remix\ui_server.py'
with open(path, encoding='utf-8') as f:
    content = f.read()

# Sections to remove — identified by start pattern and end pattern
removals = [
    # upload-public route (the placeholder we just added)
    (r"@app\.route\(\"/api/set-key\", methods=\[\"POST\"\]\)  # placeholder.*?\n", ""),

    # All the Canva routes + helper functions block
    # From upload-public to before set-key
    (
        r"@app\.route\(\"/api/board/upload-public.*?"  # start
        r"(?=@app\.route\(\"/api/set-key\")|"           # end at set-key
        r"(?=@app\.route\(\"/api/canva-token-status\"))", # or at canva-token-status
        ""
    ),
]

# More targeted removal using line-by-line approach
lines = content.split('\n')
output = []
skip_until = None
i = 0
while i < len(lines):
    line = lines[i]

    # Detect start of blocks to remove
    remove_starts = [
        '@app.route("/api/board/upload-public/',
        '@app.route("/api/board/save-canva-url"',
        '@app.route("/api/board/request-canva/',
        '@app.route("/api/board/canva-status/',
        '@app.route("/api/canva/pending"',
        '@app.route("/api/canva/create-design"',
        'def _canva_upload_asset(',
        'def _canva_create_instagram_post(',
        '@app.route("/api/test-product-image"',
        '@app.route("/api/set-canva-token"',
        '@app.route("/api/canva-token-status"',
        # The placeholder we created
        '@app.route("/api/set-key", methods=["POST"])  # placeholder',
    ]

    if any(line.strip().startswith(s) for s in remove_starts):
        # Skip until next @app.route or end of function (next def/class at top level)
        i += 1
        while i < len(lines):
            l = lines[i]
            # Stop when we hit a new top-level definition or decorator
            if (l.startswith('@app.route') or
                (l.startswith('def ') and not l.startswith('    ')) or
                (l.startswith('class ') and not l.startswith('    ')) or
                l.startswith('if __name__')):
                break
            i += 1
        continue

    output.append(line)
    i += 1

new_content = '\n'.join(output)

with open(path, 'w', encoding='utf-8') as f:
    f.write(new_content)

print("Done. Removed Canva routes.")
# Verify
removed = ['upload-public', 'save-canva-url', 'request-canva',
           'canva-status', 'canva/pending', 'canva/create-design',
           '_canva_upload_asset', '_canva_create_instagram_post',
           'test-product-image', 'set-canva-token', 'canva-token-status']
for r in removed:
    if r in new_content:
        print(f"WARNING: still found '{r}'")
    else:
        print(f"OK: removed '{r}'")
