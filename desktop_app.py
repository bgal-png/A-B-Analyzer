"""Desktop launcher — run the A/B Sales Analyzer in its own native window.

Starts the Streamlit server *headless* in the background (no browser tab) and
shows it inside a pywebview desktop window. On launch it tries a fast-forward
`git pull` so you always run the latest pushed version. Uses this computer's
resources (not Streamlit Cloud), so large exports load without the memory cap.

Run it via "Run A-B Analyzer.bat" (double-click) or:  python desktop_app.py
Close the window to quit — the background server is stopped automatically.
"""
from __future__ import annotations

import atexit
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "app.py"
TITLE = "A/B Sales Analyzer"


def _free_port() -> int:
    """Pick an OS-assigned free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _git_pull() -> None:
    """Best-effort fast-forward to the latest pushed version. Never clobbers:
    if offline / not a repo / can't fast-forward, we just run what's on disk."""
    try:
        subprocess.run(["git", "pull", "--ff-only"], cwd=str(HERE), timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _start_streamlit(port: int) -> subprocess.Popen:
    """Launch `streamlit run app.py` headless, bound to localhost only."""
    env = dict(os.environ)
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    return subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(APP),
         "--server.headless=true",
         "--server.port", str(port),
         "--server.address", "127.0.0.1"],
        cwd=str(HERE), env=env,
    )


def _wait_until_up(url: str, proc: subprocess.Popen, timeout: float = 90) -> bool:
    """Poll the server until it answers 200, the process dies, or we time out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # server exited before coming up
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main() -> int:
    if not APP.exists():
        print(f"app.py not found next to launcher: {APP}", file=sys.stderr)
        return 1

    _git_pull()
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    proc = _start_streamlit(port)
    atexit.register(_stop, proc)

    if not _wait_until_up(url, proc):
        _stop(proc)
        print("Streamlit failed to start within the timeout.", file=sys.stderr)
        return 1

    import webview  # imported late so server-only checks don't require a display
    webview.create_window(TITLE, url, width=1500, height=950)
    webview.start()  # blocks until the window is closed
    _stop(proc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
