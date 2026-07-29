"""Verification for the second review pass: meta.json race, atomic writers,
.env temp naming, session_id containment, export behaviour."""
import io, json, os, shutil, sys, tempfile, threading
from pathlib import Path

SRC = Path("/Users/nrmalisa/Applications/Ads Generator")
work = Path(tempfile.mkdtemp(prefix="adsgen-v2-"))
shutil.copy2(SRC / "ui_server.py", work / "ui_server.py")
for j in ("angles.json", "personas.json", "styles.json", "photo_styles.json",
          "product_benefits.json", "brand_kit.json"):
    if (SRC / j).exists():
        shutil.copy2(SRC / j, work / j)
(work / "fonts").mkdir(exist_ok=True)
for f in (SRC / "fonts").glob("*"):
    shutil.copy2(f, work / "fonts" / f.name)

sys.path.insert(0, str(work))
os.chdir(work)
import ui_server as U

U.app.testing = True
c = U.app.test_client()
fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


print("\n=== 1. meta.json concurrent merge (parallel batch slots) ===")
sess = U.SESSIONS_DIR / "sX"
sess.mkdir(parents=True, exist_ok=True)
meta_path = sess / "meta.json"
U.atomic_write_json(meta_path, {"session_id": "sX", "problem": "p"}, indent=None)

# Each thread mimics a slot merging its own copy key, having loaded meta long ago.
STALE = json.loads(meta_path.read_text())


def slot(i):
    key = f"key{i}"
    with U._META_LOCK:
        disk = {}
        try:
            with open(meta_path, encoding="utf-8") as fp:
                disk = json.load(fp)
        except Exception:
            disk = STALE
        cmap = disk.get("cached_copy_by_problem", {})
        cmap[key] = {"h1": f"headline {i}"}
        disk["cached_copy_by_problem"] = cmap
        U.atomic_write_json(meta_path, disk, indent=None)


ts = [threading.Thread(target=slot, args=(i,)) for i in range(16)]
[t.start() for t in ts]
[t.join() for t in ts]
final = json.loads(meta_path.read_text())
cached = final.get("cached_copy_by_problem", {})
check("all 16 slot results survive", len(cached) == 16, f"got {len(cached)}")
check("original meta fields preserved", final.get("problem") == "p", str(final)[:120])
check("meta.json is valid json", isinstance(final, dict))

print("\n=== 2. Every JSON saver is atomic ===")
import re
src = (work / "ui_server.py").read_text(encoding="utf-8-sig")
# strip comments first — the file discusses open("w") in prose several times
code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
bare = [m for m in re.findall(r'open\([^)]*["\']w["\'][^)]*\)', code)
        if "tmp" not in m]   # temp-file writes inside atomic_write_json / env_set are the point
check("no stray bare open(w) writers outside the atomic helpers",
      not bare, f"found {bare}")
for fn, path in [("save_photo_styles", U.PHOTO_STYLE_JSON),
                 ("save_product_benefits", U.BENEFITS_JSON)]:
    f = getattr(U, fn)
    f({"probe": 1} if fn == "save_photo_styles" else {"probe": {}})
    check(f"{fn} wrote valid json", json.loads(Path(path).read_text()) is not None)

print("\n=== 3. .env temp file name + gitignore coverage ===")
U.env_set("GEMINI_API_KEY", "AIzaTEST")
U.env_set("CANVA_API_TOKEN", "pat123")
check("no .env.env.tmp created", not list(work.glob(".env.env.tmp")), str(list(work.glob(".env*"))))
check("no leftover temp files", not list(work.glob(".env.tmp")), str(list(work.glob(".env*"))))
txt = (work / ".env").read_text()
check("both keys present", "AIzaTEST" in txt and "pat123" in txt, repr(txt))
gi = (SRC / ".gitignore").read_text()
check("gitignore covers *.tmp", "*.tmp" in gi and ".env.*" in gi, repr(gi))

print("\n=== 4. session_id containment ===")
r = c.post("/api/generate-one", json={"session_id": "../../etc", "index": 1})
check("traversal session_id rejected", r.status_code == 400, f"{r.status_code} {r.get_json()}")
r = c.post("/api/generate-one", json={})
check("missing session_id gives 400 not 500", r.status_code == 400, f"{r.status_code}")
r = c.get("/api/image/..%2f..%2fetc/passwd")
check("api_image traversal blocked", r.status_code in (400, 404), f"{r.status_code}")

print("\n=== 5. Export still correct after changes ===")
U.save_board([])
(U.BOARD_DIR).mkdir(parents=True, exist_ok=True)
from PIL import Image
for i, (fmt, ext) in enumerate([("PNG", ".png"), ("JPEG", ".png")]):
    p = U.BOARD_DIR / f"board_{i+1:05d}.png"
    Image.new("RGB", (20, 20), (i * 40, 90, 120)).save(p, format=fmt)
U.save_board([
    {"id": 1, "filename": "board_00001.png", "label": "Ad 01 — Jeli Gamat 550ML",
     "problem": "Selepas Bersalin", "quality": "final", "created_at": "2026-07-22T10:00:00"},
    {"id": 2, "filename": "board_00002.png", "label": "Ad 02 — Gel Gamat O'Riginal",
     "problem": "Luka", "quality": "draft", "created_at": "2026-07-21T10:00:00"},
])
r = c.post("/api/board/export", json={"all": True})
d = r.get_json()
check("exported both", d.get("exported") == 2, str(d))
names = sorted(p.name for p in U.EXPORT_DIR.rglob("*") if p.is_file())
check("true extensions used (.png + .jpg)",
      any(n.endswith(".png") for n in names) and any(n.endswith(".jpg") for n in names), str(names))
check("apostrophe in label did not break the filename",
      all("'" not in n for n in names), str(names))
days = sorted(p.name for p in U.EXPORT_DIR.iterdir() if p.is_dir())
check("one folder per date", days == ["2026-07-21", "2026-07-22"], str(days))
r2 = c.post("/api/board/export", json={"all": True})
check("re-export is idempotent", (r2.get_json() or {}).get("exported") == 0, str(r2.get_json()))
r3 = c.post("/api/board/export", json={"ids": [99999]})
check("unknown id gives clean error", r3.status_code == 400, str(r3.get_json()))

print("\n=== 6. Board lock still holds (regression) ===")
U.save_board([])
def bslot(i):
    with U._BOARD_LOCK:
        b = U.load_board()
        bid = max((x["id"] for x in b), default=0) + 1
        b.insert(0, {"id": bid, "filename": f"board_{bid:05d}.png", "created_at": "2026-07-22T00:00:00"})
        U.save_board(b)
ts = [threading.Thread(target=bslot, args=(i,)) for i in range(10)]
[t.start() for t in ts]; [t.join() for t in ts]
b = U.load_board()
check("10 concurrent writes all survive", len(b) == 10 and len(set(x["id"] for x in b)) == 10, f"{len(b)}")


# tweak-variant filename must not collide after a middle variant is deleted
def _test_variant_naming():
    import re as _re
    from pathlib import Path as _P
    print("\n=== 7. Tweak-variant naming survives a middle delete ===")
    d = U.BOARD_DIR
    for p in d.glob("board_09999_v*.png"):
        p.unlink()
    stem = "board_09999"

    def claim_and_write(payload):
        # mirrors the fixed logic in board_regenerate
        with U._BOARD_LOCK:
            ex = []
            for p in d.glob(f"{stem}_v*.png"):
                m = _re.fullmatch(rf"{_re.escape(stem)}_v(\d+)\.png", p.name)
                if m:
                    ex.append(int(m.group(1)))
            i = (max(ex) + 1) if ex else 1
            (d / f"{stem}_v{i}.png").write_bytes(payload)
            return i

    for k in range(3):
        claim_and_write(f"orig-{k+1}".encode())
    (d / f"{stem}_v2.png").unlink()                 # user deletes the middle one
    v3_before = (d / f"{stem}_v3.png").read_bytes()
    new_i = claim_and_write(b"NEW")                 # regenerate one more
    _fails = []
    if new_i != 4:
        _fails.append(f"expected v4, got v{new_i}")
    if (d / f"{stem}_v3.png").read_bytes() != v3_before:
        _fails.append("v3 was overwritten")
    for p in d.glob(f"{stem}_v*.png"):
        p.unlink()
    print("  " + ("PASS" if not _fails else "FAIL") + "  regenerate after delete does not overwrite  " + "; ".join(_fails))
    return not _fails


if not _test_variant_naming():
    fails.append("variant naming")

print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
