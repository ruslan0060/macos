import multiprocessing
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from fastapi.responses import HTMLResponse
import uvicorn

import license_guard
import parse_api

# --noconsole build: stdout/stderr are None, which breaks uvicorn logging
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

APP_NAME = "ExamViewer"
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _data_root() -> Path:
    # Windows: %LOCALAPPDATA%   macOS: ~/Library/Application Support
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("LOCALAPPDATA", Path.home()))


DATA_DIR = _data_root() / APP_NAME
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR = DATA_DIR / "parsed_output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Redirect all data to a user-writable folder (Program Files is read-only)
parse_api.APP_ROOT = DATA_DIR
parse_api.JOB_STORAGE_ROOT = DATA_DIR / "api_jobs"
parse_api.UPLOAD_ROOT = parse_api.JOB_STORAGE_ROOT / "uploads"

# Lisenziya: açar təsdiqlənməyibsə API bağlıdır (bax license_guard.py)
LICENSE = license_guard.install(parse_api.app, DATA_DIR)


@parse_api.app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (BUNDLE_DIR / "viewer.html").read_text(encoding="utf-8")
    html = html.replace("D:/projectss/python/onlineexam/parsed_output", OUTPUT_DIR.as_posix())
    response = HTMLResponse(html)
    # Sessiya kukisi: yalnız bu pəncərədən gələn yazma sorğuları qəbul edilsin (bax license_guard.py)
    response.set_cookie(license_guard.SESSION_COOKIE, LICENSE.session_token,
                        httponly=True, samesite="strict", path="/")
    return response


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    port = free_port()
    config = uvicorn.Config(parse_api.app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    url = f"http://127.0.0.1:{port}/"
    try:
        import webview
        webview.create_window("Quiz Viewer", url, width=1280, height=820, min_size=(900, 600), maximized=True)
        webview.start()
    except Exception:
        webbrowser.open(url)
        while True:
            time.sleep(3600)
    server.should_exit = True


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
