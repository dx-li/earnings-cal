import socket
import threading
import time
import webview
from earnings_cal.research_app import app

def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); return sock.getsockname()[1]

def main():
    port = _port()
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True), daemon=True).start()
    deadline=time.time()+10
    while time.time()<deadline:
        try:
            with socket.create_connection(("127.0.0.1",port),timeout=.4): break
        except OSError: time.sleep(.1)
    webview.create_window("Earnings Research Lab", f"http://127.0.0.1:{port}/", width=1440, height=900, min_size=(1050,650))
    webview.start()

if __name__ == "__main__": main()
