"""
Watchdog runner — keeps ui_server.py alive forever.
If it crashes, restarts it after 3 seconds.
Launched by Windows Task Scheduler at login.
"""
import subprocess, time, sys, os

PYTHON  = sys.executable
SERVER  = os.path.join(os.path.dirname(__file__), "ui_server.py")
WORKDIR = os.path.dirname(__file__)

print(f"Watchdog started. Running: {SERVER}")

while True:
    print(f"[watchdog] Starting server...")
    proc = subprocess.Popen(
        [PYTHON, SERVER],
        cwd=WORKDIR,
        creationflags=0,   # no new window flags — task scheduler handles visibility
    )
    proc.wait()
    print(f"[watchdog] Server exited (code {proc.returncode}). Restarting in 3s...")
    time.sleep(3)
