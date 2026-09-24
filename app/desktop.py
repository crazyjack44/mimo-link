"""MiMo Link desktop shell (pywebview).

Starts the local API/UI server in a background thread and opens a native
window. Used for source runs and PyInstaller release builds.

    python desktop.py [--port 8765]
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import socket
import sys
import threading
import traceback
from pathlib import Path

if getattr(sys, "frozen", False):
    _BASE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    APP_DIR = _BASE / "app" if (_BASE / "app" / "index.html").exists() else _BASE
    ROOT_DIR = _BASE
else:
    APP_DIR = Path(__file__).resolve().parent
    ROOT_DIR = APP_DIR.parent
for p in (str(ROOT_DIR), str(APP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from server import Handler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

DEFAULT_PORT = 8765
APP_TITLE = "MiMo Link"
APP_SIZE = (1280, 860)


def _log(msg: str) -> None:
    line = msg if msg.endswith("\n") else msg + "\n"
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        log_dir = base / "mimo-link"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "desktop.log").open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        return s.connect_ex((host, port)) == 0


def _serve_forever(port: int) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def start_server(port: int = DEFAULT_PORT) -> mp.Process | None:
    """Start the local HTTP UI/API in a child process (own GIL).

    A thread is not enough: WebView2 / pywebview can hold the GIL during
    window creation and starve the HTTP server.
    """
    if _port_open("127.0.0.1", port):
        _log(f"port {port} already open — reuse existing server")
        return None
    proc = mp.Process(target=_serve_forever, args=(port,), daemon=True)
    proc.start()
    for _ in range(40):
        if _port_open("127.0.0.1", port):
            break
        threading.Event().wait(0.05)
    _log(f"server process started pid={proc.pid} on 127.0.0.1:{port}")
    return proc


def main() -> int:
    # Frozen + Windows: allow a second process for the HTTP server.
    try:
        mp.freeze_support()
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="MiMo Link desktop app")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    port = int(args.port)
    _log(f"desktop start frozen={getattr(sys, 'frozen', False)} argv={sys.argv}")
    _log(f"APP_DIR={APP_DIR} exists={APP_DIR.exists()} index={(APP_DIR / 'index.html').exists()}")

    try:
        proc = start_server(port)
    except Exception:
        _log("start_server failed:\n" + traceback.format_exc())
        raise
    url = f"http://127.0.0.1:{port}"

    try:
        import webview
        _log("webview imported")
    except ImportError:
        _log("webview missing")
        print("pywebview is not installed. Run: pip install -r requirements-desktop.txt", file=sys.stderr)
        if proc is None:
            print(f"A server is already running at {url} — open it in a browser.", file=sys.stderr)
            return 1
        print(f"Server running at {url} (Ctrl+C to quit).", file=sys.stderr)
        try:
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            proc.terminate()
        return 0

    icon_path = APP_DIR / "assets" / "app.ico"
    window_kwargs = {
        "title": APP_TITLE,
        "url": url,
        "width": APP_SIZE[0],
        "height": APP_SIZE[1],
        "min_size": (960, 640),
        "background_color": "#F5F5F7",
        "easy_drag": False,
    }
    # Taskbar/window icon is embedded into MiMoLink.exe at build time (see mimo-link.spec).

    _log(f"create_window url={url}")
    try:
        webview.create_window(**window_kwargs)
    except Exception:
        _log("create_window failed:\n" + traceback.format_exc())
        if proc is not None:
            proc.terminate()
        raise
    _log("webview.start()")
    webview.start()
    _log("webview closed")

    if proc is not None and proc.is_alive():
        proc.terminate()
        proc.join(timeout=2)
        _log("server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
