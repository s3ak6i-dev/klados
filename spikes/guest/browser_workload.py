#!/usr/bin/env python3
"""Browser fidelity workload: launch headless Chrome, establish LIVE in-memory state (a session
token in the JS heap + localStorage + document.title), and continuously report the live state to
/run/klados/browser_state via CDP. If the browser survives snapshot/fork, a restored fork will
still return the same token AND keep incrementing the heartbeat (proving the renderer process +
JS heap are alive, not just the on-disk profile).

Minimal CDP-over-websocket client (stdlib only). Chrome flags are the microVM-friendly set.
"""
import base64
import json
import os
import secrets
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

ORIGIN_PORT = 8123  # a real local origin so localStorage works (about:blank is opaque)


class _Page(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><html><body>klados fidelity</body></html>")


def start_origin():
    srv = HTTPServer(("127.0.0.1", ORIGIN_PORT), _Page)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

PORT = 9222
PROFILE = "/run/klados/chrome"
STATE = "/run/klados/browser_state"
TOKEN = "/run/klados/browser_token"


def start_chrome():
    for d in (PROFILE, "/run/klados/home", "/run/klados/cache", "/run/klados/config"):
        os.makedirs(d, exist_ok=True)
    args = ["/usr/bin/google-chrome", "--headless=new", "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-gpu", "--disable-software-rasterizer", "--disable-dev-shm-usage",
            "--disable-breakpad", "--no-first-run", "--disable-extensions",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"]
    env = dict(os.environ, HOME="/run/klados/home", XDG_CACHE_HOME="/run/klados/cache",
               XDG_CONFIG_HOME="/run/klados/config")
    log = open("/run/klados/chrome.log", "wb")
    return subprocess.Popen(args, stdout=log, stderr=log, env=env)


def http_json(path):
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10))


class WS:
    def __init__(self, url):
        path = url.split(str(PORT), 1)[1]
        self.s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
                        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)

    def send(self, obj):
        data = json.dumps(obj).encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        d = b""
        while len(d) < n:
            c = self.s.recv(n - len(d))
            if not c:
                raise ConnectionError("ws closed")
            d += c
        return d

    def recv(self):
        b0 = self._read(1)[0]
        b1 = self._read(1)[0]
        ln = b1 & 0x7f
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        payload = self._read(ln)
        if (b0 & 0x0f) not in (0x1, 0x2):  # skip control frames (ping/pong/close)
            return None
        return json.loads(payload)


def main():
    os.makedirs("/run/klados", exist_ok=True)
    start_origin()
    start_chrome()
    open(STATE, "w").write("stage=chrome-launched\n")

    def page_ws(diag):
        try:
            for t in http_json("/json"):
                if t.get("type") in ("page", "tab") and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception as e:
            diag["json"] = repr(e)
        try:  # create a page target (Chrome 111+ requires PUT on /json/new)
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/json/new?about:blank", method="PUT")
            return json.load(urllib.request.urlopen(req, timeout=10)).get("webSocketDebuggerUrl")
        except Exception as e:
            diag["new"] = repr(e)
        return None

    ws = None
    for i in range(200):  # ~60s
        diag = {}
        url = page_ws(diag)
        if url:
            try:
                ws = WS(url)
                break
            except Exception as e:
                diag["ws"] = repr(e)
        if i % 8 == 0:
            try:
                open(STATE, "w").write(f"stage=connecting i={i} diag={diag}\n")
            except Exception:
                pass
        time.sleep(0.3)
    if not ws:
        try:
            lg = open("/run/klados/chrome.log").read()
            dbg = "HEAD:\n" + lg[:1500] + "\n...TAIL:\n" + lg[-2000:]
        except Exception:
            dbg = "(no chrome.log)"
        open(STATE, "w").write("error=no-cdp\n" + dbg)
        return

    open(STATE, "w").write("stage=ws-connected\n")
    ws.s.settimeout(20)  # so a hung evaluate fails visibly instead of blocking forever
    mid = [0]

    def ev(expr):
        mid[0] += 1
        ws.send({"id": mid[0], "method": "Runtime.evaluate",
                 "params": {"expression": expr, "returnByValue": True}})
        while True:
            m = ws.recv()
            if m and m.get("id") == mid[0]:
                return m.get("result", {}).get("result", {}).get("value")

    try:
        # navigate to a real origin (localStorage is unavailable on about:blank)
        mid[0] += 1
        ws.send({"id": mid[0], "method": "Page.navigate",
                 "params": {"url": f"http://127.0.0.1:{ORIGIN_PORT}/"}})
        while True:
            m = ws.recv()
            if m and m.get("id") == mid[0]:
                break
        for _ in range(50):
            if ev("document.readyState") == "complete":
                break
            time.sleep(0.2)
        token = secrets.token_hex(12)
        ev(f"window.__session={token!r}; localStorage.setItem('sess',{token!r}); "
           f"document.title={token!r}; window.__beat=0; 'ok'")
        open(TOKEN, "w").write(token)
        open(STATE, "w").write(f"stage=token-set token={token} beat=0 ls={token}\n")
    except Exception as e:
        open(STATE, "w").write(f"stage=eval-failed err={e!r}\n")
        return

    while True:
        try:
            beat = ev("window.__beat=(window.__beat||0)+1; window.__beat")
            sess = ev("window.__session")
            ls = ev("localStorage.getItem('sess')")
        except Exception as e:
            with open(STATE + ".tmp", "w") as f:
                f.write(f"stage=loop-failed err={e!r}\n")
            os.replace(STATE + ".tmp", STATE)
            time.sleep(0.5)
            continue
        with open(STATE + ".tmp", "w") as f:
            f.write(f"token={sess} beat={beat} ls={ls}\n")
        os.replace(STATE + ".tmp", STATE)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
