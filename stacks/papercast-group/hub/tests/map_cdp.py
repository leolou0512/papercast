"""A very small Chrome DevTools Protocol client for the browser tests.

Needs a headless Chrome and the `websocket-client` package. Neither ships in the image: these
tests run on stibnite, not on the NAS. Chrome is found from $PAPERCAST_TEST_CHROME, else the
Playwright cache (~/.cache/ms-playwright/chromium_headless_shell-*), else PATH.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def find_chrome() -> str | None:
    c = os.environ.get("PAPERCAST_TEST_CHROME")
    if c and os.access(c, os.X_OK):
        return c
    for g in sorted(glob.glob(os.path.expanduser(
            "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell")), reverse=True):
        if os.access(g, os.X_OK):
            return g
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome-headless-shell"):
        p = shutil.which(name)
        if p:
            return p
    return None


class Browser:
    def __init__(self):
        import websocket  # websocket-client
        self._ws_mod = websocket
        exe = find_chrome()
        if not exe:
            raise RuntimeError("no headless Chrome found")
        self.dir = tempfile.mkdtemp(prefix="papercast-chrome-")
        self.proc = subprocess.Popen(
            [exe, "--headless", "--remote-debugging-port=0", f"--user-data-dir={self.dir}", "--no-first-run",
             "--no-default-browser-check", "--disable-gpu", "--autoplay-policy=no-user-gesture-required",
             "--disable-background-networking", "--disable-extensions", "--mute-audio", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(self.dir) / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists() and port_file.read_text().strip():
                break
            time.sleep(0.1)
        port = int(port_file.read_text().split()[0])
        self.http = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                targets = json.loads(urllib.request.urlopen(f"{self.http}/json/list", timeout=2).read())
                page = next(t for t in targets if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.1)
        self.page_id = page["id"]
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
        self.n = 0
        self.events: list = []
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Network.enable")

    def call(self, method: str, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)

    def pump(self, seconds: float = 0.0):
        self.ws.settimeout(0.05)
        end = time.time() + seconds
        while True:
            try:
                self.events.append(json.loads(self.ws.recv()))
            except self._ws_mod.WebSocketTimeoutException:
                if time.time() >= end:
                    break
        self.ws.settimeout(30)

    def js(self, expr: str, context: int | None = None, await_promise: bool = True):
        params = {"expression": expr, "returnByValue": True, "awaitPromise": await_promise}
        if context is not None:
            params["contextId"] = context
        r = self.call("Runtime.evaluate", **params)
        if "exceptionDetails" in r:
            raise RuntimeError(f"js failed: {r['exceptionDetails'].get('exception', {}).get('description') or r['exceptionDetails']}")
        return r.get("result", {}).get("value")

    def wait_js(self, expr: str, timeout: float = 10, what: str | None = None, context: int | None = None):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                last = self.js(expr, context)
                if last:
                    return last
            except RuntimeError as e:
                last = e
            time.sleep(0.1)
        raise AssertionError(f"timed out: {what or expr}; last={last!r}")

    def goto(self, url: str):
        self.call("Page.navigate", url=url)
        self.wait_js("document.readyState === 'complete'", 15, f"load {url}")

    def frame_context(self, url_part: str, timeout: float = 10) -> int:
        """An isolated world in the child frame whose URL contains url_part: shares its DOM."""
        end = time.time() + timeout
        while time.time() < end:
            tree = self.call("Page.getFrameTree")["frameTree"]
            stack = [tree]
            while stack:
                f = stack.pop()
                if url_part in f["frame"].get("url", "") and f is not tree:
                    return self.call("Page.createIsolatedWorld", frameId=f["frame"]["id"], worldName="probe")["executionContextId"]
                stack.extend(f.get("childFrames", []))
            time.sleep(0.1)
        raise AssertionError(f"no frame with {url_part}")

    def viewport(self, w: int, h: int, mobile: bool = False):
        self.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1 if not mobile else 2, mobile=mobile)

    def screenshot(self, path: str | Path):
        data = self.call("Page.captureScreenshot", format="png")["data"]
        Path(path).write_bytes(base64.b64decode(data))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)
