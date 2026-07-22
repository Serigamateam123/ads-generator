"""Verify the robustness work: JSON error handler, upload cap, log trimming
(including that an already-open append fd keeps working), startup self-check."""
import io, json, os, shutil, sys, tempfile
from pathlib import Path

SRC = Path("/Users/nrmalisa/Applications/Ads Generator")
work = Path(tempfile.mkdtemp(prefix="adsgen-v3-"))
shutil.copy2(SRC / "ui_server.py", work / "ui_server.py")
for j in ("angles.json", "personas.json", "styles.json", "brand_kit.json"):
    if (SRC / j).exists():
        shutil.copy2(SRC / j, work / j)
(work / "fonts").mkdir(exist_ok=True)
for f in (SRC / "fonts").glob("*"):
    shutil.copy2(f, work / "fonts" / f.name)

sys.path.insert(0, str(work))
os.chdir(work)
import ui_server as U

U.app.testing = False          # exercise the real error handler, not test propagation
c = U.app.test_client()
fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


print("\n=== 1. API errors come back as JSON, never HTML ===")
@U.app.route("/api/_boom")
def _boom():
    raise RuntimeError("simulated failure")

r = c.get("/api/_boom")
ct = r.headers.get("Content-Type", "")
check("500 is application/json", "application/json" in ct, ct)
try:
    body = r.get_json()
except Exception as e:
    body = None
check("body parses as JSON", isinstance(body, dict), str(body))
check("error message names the cause",
      "RuntimeError" in (body or {}).get("error", ""), str(body))

r = c.get("/api/does-not-exist")
check("404 on /api is JSON too", "application/json" in r.headers.get("Content-Type", ""),
      r.headers.get("Content-Type", ""))
check("404 keeps its status", r.status_code == 404, str(r.status_code))

r = c.get("/definitely-not-here")
check("non-API 404 unaffected (still HTML)",
      "application/json" not in r.headers.get("Content-Type", ""),
      r.headers.get("Content-Type", ""))

print("\n=== 2. Upload size cap ===")
check("MAX_CONTENT_LENGTH set to 256MB",
      U.app.config.get("MAX_CONTENT_LENGTH") == 256 * 1024 * 1024,
      str(U.app.config.get("MAX_CONTENT_LENGTH")))
r = c.post("/api/upload", data={"ads": (io.BytesIO(b"x" * 1024), "small.png")},
           content_type="multipart/form-data")
check("normal upload still works", r.status_code == 200, f"{r.status_code} {r.get_data()[:120]}")

print("\n=== 3. Log trimming with a live append fd (the launchd case) ===")
log = U.LOG_PATH
U.LOG_MAX_BYTES = 200_000
U.LOG_KEEP_TAIL = 50_000
with open(log, "w") as f:
    for i in range(20_000):
        f.write(f"line {i:06d} some log content here\n")
size_before = log.stat().st_size
check("log starts oversized", size_before > U.LOG_MAX_BYTES, str(size_before))

# simulate launchd: a process holding the file open in append mode across the trim
holder = open(log, "a")
holder.write("BEFORE-TRIM-MARKER\n")
holder.flush()

trimmed = U.trim_log_if_large()
check("trim reported success", trimmed is True)
size_after = log.stat().st_size
check("log shrank", size_after < size_before, f"{size_before} -> {size_after}")
check("log kept roughly the tail size", size_after <= U.LOG_KEEP_TAIL + 4096, str(size_after))

# the crucial bit: the pre-existing fd must keep working after truncation
holder.write("AFTER-TRIM-MARKER\n")
holder.flush()
holder.close()
text = log.read_text(errors="replace")
check("writer with open fd still works after trim", "AFTER-TRIM-MARKER" in text)
check("trim banner present", "log trimmed" in text)
check("recent lines survived", "line 019999" in text)
check("oldest lines dropped", "line 000001" not in text)
check("no fragment on first content line",
      all(l.startswith(("line ", "[log trimmed", "BEFORE-TRIM", "AFTER-TRIM"))
          for l in text.splitlines()[:3] if l),
      repr(text.splitlines()[:3]))

# below threshold -> no-op
check("no trim when under the cap", U.trim_log_if_large() is False)

print("\n=== 4. Startup self-check logic ===")
import subprocess
r = subprocess.run([str(SRC / "venv/bin/python"), "-c", f"""
import os, sys
os.chdir({str(work)!r}); sys.path.insert(0, {str(work)!r})
os.environ.pop('GEMINI_API_KEY', None)
os.environ['PORT'] = '7999'
import ui_server
"""], capture_output=True, text=True, timeout=60)
check("module imports cleanly without a key", r.returncode == 0, r.stderr[-400:])

print("\n=== 5. requirements.txt is pinned and installable-looking ===")
req = (SRC / "requirements.txt").read_text()
pins = [l for l in req.splitlines() if l.strip() and not l.strip().startswith("#")]
check("every dep pinned", all("==" in p for p in pins), str(pins))
check("google-generativeai dropped", not any("generativeai" in p for p in pins), str(pins))
check("werkzeug pinned explicitly", any("werkzeug" in p.lower() for p in pins), str(pins))

print("\n" + ("ALL CHECKS PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
