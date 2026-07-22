"""Verify the concurrency + path-validation fixes against a COPY of the app.

Runs the real ui_server module but with all data paths pointed at a temp dir, so
the live board/brand data is never touched.
"""
import io, json, os, shutil, sys, tempfile, threading
from pathlib import Path

SRC = Path("/Users/nrmalisa/Applications/Ads Generator")
work = Path(tempfile.mkdtemp(prefix="adsgen-verify-"))

# minimal copy: the module + the dirs it expects
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

app = U.app
app.testing = True
BOARD = U.BOARD_DIR
SESS = U.SESSIONS_DIR
fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


print("\n=== 1. Concurrent board writes (the data-loss bug) ===")
# Simulate N parallel generate-one slots doing the locked claim->copy->append->save.
U.save_board([])
(SESS / "s1").mkdir(parents=True, exist_ok=True)
srcs = []
for i in range(12):
    p = SESS / "s1" / f"img_{i:02d}_final.png"
    p.write_bytes(b"IMAGE-" + str(i).encode())   # unique content per "generation"
    srcs.append(p)


def slot(src, i):
    with U._BOARD_LOCK:
        board = U.load_board()
        bid = max((b["id"] for b in board), default=0) + 1
        fn = f"board_{bid:05d}.png"
        shutil.copy2(str(src), str(BOARD / fn))
        board.insert(0, {"id": bid, "filename": fn, "session_id": "s1",
                         "created_at": f"2026-07-22T00:00:{i:02d}"})
        U.save_board(board)


ts = [threading.Thread(target=slot, args=(s, i)) for i, s in enumerate(srcs)]
[t.start() for t in ts]
[t.join() for t in ts]

board = U.load_board()
ids = [b["id"] for b in board]
files = [b["filename"] for b in board]
check("all 12 entries survive", len(board) == 12, f"got {len(board)}")
check("ids are unique", len(set(ids)) == 12, f"ids={sorted(ids)}")
check("filenames are unique", len(set(files)) == 12, f"files={sorted(files)}")
contents = {(BOARD / f).read_bytes() for f in files if (BOARD / f).exists()}
check("no image overwrote another", len(contents) == 12, f"{len(contents)} distinct images")

print("\n=== 2. Upload filename traversal ===")
c = app.test_client()
before = set(p.name for p in work.iterdir())
r = c.post("/api/brand-kit/upload-product",
           data={"photos": (io.BytesIO(b"x"), "../../../../evil.png")},
           content_type="multipart/form-data")
escaped = list(work.parent.glob("evil.png")) + list(work.glob("evil.png"))
check("traversal upload does not escape brand_kit", not escaped, f"escaped to {escaped}")
saved = [p.name for p in U.BRAND_DIR.glob("product_*")]
check("file still saved, name sanitised", any("evil.png" in n for n in saved), f"saved={saved}")
print("        stored as:", saved)

print("\n=== 3. /api/canva-queue path escape ===")
(work / ".env").write_text("GEMINI_API_KEY=SECRET123\n")
r = c.post("/api/canva-queue", json={"filename": "../.env", "session_id": ""})
body = r.get_json() or {}
check("cannot reach ../.env", r.status_code == 404, f"status={r.status_code} body={body}")
r = c.post("/api/canva-queue", json={"filename": "x.png", "session_id": "../.."})
check("cannot escape via session_id", (r.get_json() or {}).get("error", "").startswith("Image not found"),
      str(r.get_json()))

print("\n=== 4. /api/board/add arbitrary source ===")
outside = work.parent / "outside_secret.txt"
outside.write_text("SECRET")
r = c.post("/api/board/add", json={"src_path": str(outside)})
check("arbitrary absolute path rejected", r.status_code == 400, f"status={r.status_code}")
r = c.post("/api/board/add", json={"src_path": str(srcs[0])})
check("legitimate session path still accepted", r.status_code == 200, str(r.get_json()))

print("\n=== 5. Gemini key persists to .env ===")
r = c.post("/api/set-key", json={"key": "AIzaTESTKEY123"})
env_txt = (work / ".env").read_text()
check("key written to .env", "GEMINI_API_KEY=AIzaTESTKEY123" in env_txt, repr(env_txt))
check("env file has no duplicate key lines",
      env_txt.count("GEMINI_API_KEY=") == 1, repr(env_txt))
check("permissions are 0600", oct(os.stat(work / ".env").st_mode)[-3:] == "600",
      oct(os.stat(work / ".env").st_mode))
r = c.post("/api/canva-token", json={"token": "canva-pat-xyz"})
env_txt = (work / ".env").read_text()
check("canva token coexists with gemini key",
      "CANVA_API_TOKEN=canva-pat-xyz" in env_txt and "GEMINI_API_KEY=AIzaTESTKEY123" in env_txt,
      repr(env_txt))
c.delete("/api/canva-token")
env_txt = (work / ".env").read_text()
check("delete removes only the canva token",
      "CANVA_API_TOKEN" not in env_txt and "GEMINI_API_KEY=AIzaTESTKEY123" in env_txt, repr(env_txt))

print("\n=== 6. Board delete + repair still work ===")
b = U.load_board()
r = c.post("/api/board/delete", json={"ids": [b[0]["id"]]})
check("delete works", (r.get_json() or {}).get("deleted") == 1, str(r.get_json()))
r = c.post("/api/board/repair", json={"dry_run": True})
check("repair dry-run works", r.status_code == 200 and "would_recover" in (r.get_json() or {}),
      str(r.get_json()))
r = c.post("/api/board/repair", json={})
check("repair works", r.status_code == 200 and "recovered" in (r.get_json() or {}), str(r.get_json()))
bb = U.load_board()
check("repair produced unique ids", len(set(x["id"] for x in bb)) == len(bb))

print("\n=== 7. Rotation state is written atomically ===")
lib = [{"path": f"/t/tpl_{i}.jpg", "section": f"sec{i%3}"} for i in range(9)]
got = U.next_auto_templates(lib, "nonce-a", 4)
check("rotation returns distinct templates", len(set(got)) == 4, str(got))
check("rotation file is valid json", json.loads(U.ROTATION_JSON.read_text()).get("cursor") == 4)
check("same batch nonce reuses the offset", U.next_auto_templates(lib, "nonce-a", 4) == got)
check("no stray .tmp files left", not list(work.glob("*.tmp")), str(list(work.glob("*.tmp"))))

print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
print("workdir:", work)
sys.exit(1 if fails else 0)
