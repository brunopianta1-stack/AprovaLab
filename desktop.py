import os
import sys
import threading
import time
import webview

def resource_path(relative):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)

# Make templates/static discoverable when packaged.
os.chdir(resource_path("."))

from app import app, init_db

HOST = "127.0.0.1"
PORT = 5050

def run_server():
    init_db()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    server = threading.Thread(target=run_server, daemon=True)
    server.start()
    time.sleep(1.0)

    webview.create_window(
        "AprovaLab",
        f"http://{HOST}:{PORT}",
        width=1536,
        height=960,
        min_size=(980, 650),
        resizable=True,
        text_select=True
    )
    webview.start()
