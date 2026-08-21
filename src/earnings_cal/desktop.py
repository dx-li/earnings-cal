"""Standalone desktop wrapper: runs Flask in a background thread, shows a pywebview window."""
import socket
import threading
import time

import webview

from earnings_cal.app import app, start_brief_backfill


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int) -> None:
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)


def _wait_for_server(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)


def main() -> None:
    port = _find_free_port()
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    _wait_for_server(port)
    start_brief_backfill()
    webview.create_window(
        "Earnings Calendar",
        f"http://127.0.0.1:{port}/",
        width=1280,
        height=840,
        min_size=(900, 600),
    )
    webview.start()


if __name__ == "__main__":
    main()
