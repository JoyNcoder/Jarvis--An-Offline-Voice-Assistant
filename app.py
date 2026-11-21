"""Jarvis desktop launcher: starts the server and opens it in its own window."""
import env_setup
import os, sys, socket, threading, time, json, urllib.request
from pathlib import Path

window = None

BASE = Path(__file__).resolve().parent
os.chdir(BASE)                      # model files (kokoro, silero) are loaded by relative path
sys.path.insert(0, str(BASE))

# pythonw has no console: send print() output to a log file instead of crashing
_log = open(BASE / "jarvis_log.txt", "a", encoding="utf-8", buffering=1)
sys.stdout = _log
sys.stderr = _log

import uvicorn
import webview

HOST, PORT = "127.0.0.1", 8765
URL = f"http://{HOST}:{PORT}/"
server = None          # imported later, in the background

SPLASH = """
<html><body style="margin:0;height:100vh;display:flex;flex-direction:column;
align-items:center;justify-content:center;background:#04080d;color:#22d3ee;
font-family:'Segoe UI',sans-serif;overflow:hidden">
<div style="width:90px;height:90px;border-radius:50%;border:3px solid rgba(34,211,238,.2);
border-top-color:#22d3ee;animation:s 1.1s linear infinite"></div>
<div style="margin-top:28px;letter-spacing:.4em;font-size:15px">JARVIS</div>
<div id="s" style="margin-top:10px;font-size:12px;color:#8fb3c4">Starting up...</div>
<style>@keyframes s{to{transform:rotate(360deg)}}</style>
</body></html>
"""

def run_server():
    global server
    import server as _server
    server = _server
    uvicorn.run(server.app, host=HOST, port=PORT, log_level="warning")

def wait_for_port(timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False

def wait_until_ready(timeout=180):
    """Wait until Jarvis has finished loading and is listening."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/ready", timeout=2) as r:
                if json.loads(r.read()).get("ready"):
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False

quitting = False

def on_closing():
    global quitting
    if server is None:          # still loading: nothing to save, just close
        os._exit(0)
    if not quitting:
        quitting = True
        server.handle_command("quit", {})
    return False

class Api:
    def set_mode(self, mode):
        size = (300, 370) if mode == "compact" else (1040, 740)
        clear_region()                      # no stale clip while resizing
        window.resize(*size)
        on_top = False
        if mode == "compact":
            import settings_store
            on_top = bool(settings_store.get_setting("compact_always_on_top", True))
        window.on_top = on_top
        round_corners(size=size)            # apply the right shape immediately
        threading.Timer(0.3, round_corners, kwargs={"size": size}).start()  # safety re-apply

    def minimize(self):
        window.minimize()

def clear_region():
    try:
        import ctypes
        hwnd = window.native.Handle.ToInt64()
        ctypes.windll.user32.SetWindowRgn(hwnd, None, True)
    except Exception as error:
        print("clear_region failed:", error)

def round_corners(radius=28, size=None):
    """Clip the window to rounded corners (Windows only)."""
    try:
        import ctypes, ctypes.wintypes
        hwnd = window.native.Handle.ToInt64()
        user32 = ctypes.windll.user32
        try:
            scale = user32.GetDpiForWindow(hwnd) / 96
        except Exception:
            scale = 1
        if size:
            w, h = int(size[0] * scale), int(size[1] * scale)
        else:
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
        d = int(radius * scale * 2)
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, d, d)
        user32.SetWindowRgn(hwnd, rgn, True)
    except Exception as error:
        print("round_corners failed:", error)

def start_backend():
    threading.Thread(target=run_server, daemon=True).start()
    if wait_for_port(120):
        try:
            window.evaluate_js("document.getElementById('s').textContent='Loading models...'")
        except Exception:
            pass
        wait_until_ready()          # keep the splash until Jarvis is listening
        window.load_url(URL)
    else:
        print("Server did not start in time.")

if __name__ == "__main__":
    window = webview.create_window("Jarvis", html=SPLASH, width=1180, height=820,
                                   min_size=(300, 350), background_color="#04080d",
                                   frameless=True, easy_drag=False, js_api=Api())
    window.events.closing += on_closing
    window.events.shown += lambda: round_corners()
    webview.start(start_backend)