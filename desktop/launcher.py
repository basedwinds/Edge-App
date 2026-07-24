import os
import sys
import threading

import uvicorn
import webview

BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND_DIR))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

FRONTEND_DEV_URL = "http://localhost:5173"
FRONTEND_DIST = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "dist", "index.html")
)


def run_server():
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


def main():
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    dev_mode = os.environ.get("NFL_EDGE_DEV") == "1"
    target = FRONTEND_DEV_URL if dev_mode else FRONTEND_DIST

    window = webview.create_window("NFL Edge Finder", target, width=1400, height=900, min_size=(1000, 700))
    webview.start()


if __name__ == "__main__":
    main()
