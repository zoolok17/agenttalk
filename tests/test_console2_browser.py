"""Real-browser check of the console v2 stream (skipped when no Chromium-family browser or node).

The DOM stub in the node tests models detachment and focus, but only a real browser proves that
(F1) a thread scrolled while detached loses its position and (F2) a redraw that replaces a focused
control drops focus to <body>. The page under test is the REAL /v2 shell and scripts, served with
the console CSP by a tiny stdlib server that also answers the three feeds with fixed JSON (30 chat
messages, two needs cards whose age grows on every read). The browser is driven over the DevTools
protocol by tests/console2_browser_check.mjs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agenttalk import web

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK = REPO_ROOT / "tests" / "console2_browser_check.mjs"
LEAD = "claude-agenttalk-lead"
PROJECT = "proj-browser"


def _find_browser() -> str | None:
    env = os.environ.get("AGENTTALK_TEST_BROWSER")
    if env and Path(env).is_file():
        return env
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    for path in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if Path(path).is_file():
            return path
    return None


BROWSER = _find_browser()
pytestmark = pytest.mark.skipif(
    BROWSER is None or shutil.which("node") is None, reason="needs node and a Chromium-family browser",
)


def _iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _state(now: datetime, grown: int = 0) -> dict:
    def agent(name: str, state: str, since_s: int) -> dict:
        return {
            "name": name, "cli": name.split("-")[0], "last_seen": _iso(now - timedelta(seconds=3)),
            "last_seen_age_seconds": 3,
            "health": {"state": state, "since": _iso(now - timedelta(seconds=since_s)), "updated_at": _iso(now),
                       "last_progress_at": None, "age_seconds": 1, "stale": False, "reason_code": "x", "warnings": []},
        }
    return {"schema_version": 1, "generated_at": _iso(now), "roots": [{
        "label": "browser", "path": "x", "project_id": PROJECT, "errors": [], "operator_facing": LEAD,
        # the second agent's age is inflated by "grown" (like /api/attention below) so the roster's
        # own rendered text really does change on every poll, exercising the rail render path -
        # not merely relying on it being skipped because nothing in the view changed at all.
        "agents": [agent(LEAD, "idle_waiting", 3000),
                   agent("claude-agenttalk-developer-2", "idle_waiting", 2000 + grown)],
        "recent": [{"id": "1", "ts": _iso(now - timedelta(seconds=5)), "from": LEAD, "to": "operator",
                    "kind": "message", "subject": "s"}],
    }]}


def _handler(started: float):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # noqa: D401 - silence the test server
            return

        def _send(self, body: bytes, ctype: str, csp: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            now = datetime.now(timezone.utc)
            grown = int((time.monotonic() - started) * 60)          # cards age 60 s per second: a new label every read
            if path == "/v2":
                self._send(web.render_console2(), "text/html; charset=utf-8", web._DASHBOARD_CSP)
            elif path.startswith("/static/") and path[len("/static/"):] in web._STATIC_ASSETS:
                ctype, data = web._STATIC_ASSETS[path[len("/static/"):]]
                self._send(data, ctype, web._DEFAULT_CSP)
            elif path == "/api/state":
                self._send(json.dumps(_state(now, grown)).encode(), "application/json", web._DEFAULT_CSP)
            elif path == "/api/attention":
                items = [{"id": f"card-{n}", "source": "escalation", "source_label": "ESCALATION", "severity": "high",
                          "title": f"Question {n}", "agent": None, "detail": "why it matters",
                          "age_seconds": 900 + 100 * n + grown, "human_can_unblock_now": True} for n in (1, 2)]
                self._send(json.dumps({"target_root_project_id": PROJECT, "items": items}).encode(),
                           "application/json", web._DEFAULT_CSP)
            elif path == "/api/lead-chat":
                msgs = [{"id": f"m{n:03d}", "from": "operator" if n % 3 == 0 else LEAD, "to": LEAD,
                         "body": f"Message number {n} " + "x" * 60, "ts": _iso(now - timedelta(minutes=60 - n))}
                        for n in range(30)]
                self._send(json.dumps({"target_root_project_id": PROJECT, "available": True, "operator": "operator",
                                       "lead": LEAD, "messages": msgs}).encode(), "application/json", web._DEFAULT_CSP)
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self.send_error(404)
    return Handler


@pytest.fixture
def page(tmp_path: Path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(time.monotonic()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/v2", tmp_path / "profile"
    finally:
        srv.shutdown()
        srv.server_close()


def test_thread_scroll_and_focus_survive_redraws_in_a_real_browser(page) -> None:
    url, profile = page
    result = subprocess.run(
        ["node", str(CHECK), BROWSER, url, str(profile)],
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    # F1: the thread is scrolled to its end on the first draw (it has 30 messages: it must overflow)
    initial = out["initial"]
    assert initial["connected"] and initial["height"] > initial["client"]
    assert abs(initial["top"] - (initial["height"] - initial["client"])) <= 1, initial
    # F1: scrolled up to 250, ordinary redraws (ages moved) leave it there, on the same element
    assert out["threadKept"] is True
    assert out["afterRedraw"]["top"] == 250, out["afterRedraw"]
    # F2: the focused Later button is still focused, and is the very same element, after redraws
    assert out["ageMoved"] is True, "the redraws really happened"
    assert out["focusAfter"]["tag"] == "BUTTON" and out["focusAfter"]["same"] is True, out["focusAfter"]
    assert out["focusAfter"]["key"].endswith("|later"), out["focusAfter"]
    # F2: deferring the focused card moves focus to another control in the stream, never to <body>
    after_later = out["focusAfterLater"]
    assert after_later["tag"] == "BUTTON" and after_later["inStream"] is True, after_later
    # F2: the rail is reconciled in place too - the avatar <img> survives an ordinary redraw
    assert out["railImageKept"] is True, out
    # M4b: the ? button opens the keyboard overlay, moves focus into it, and Escape closes it and
    # returns focus to the button that opened it
    assert out["overlayOpenAfterClick"] is True, out
    assert out["focusInOverlayAfterClick"] is True, out
    assert out["overlayClosedAfterEscape"] is True, out
    assert out["focusBackOnKeysBtnAfterEscape"] is True, out
    # M4b: one key path - j selects the first card, Enter focuses its first action
    assert out["firstCardSelectedAfterJ"] is True, out
    after_enter = out["focusAfterEnterOnSelected"]
    assert after_enter["tag"] == "BUTTON" and after_enter["sameAsFirstCardLater"] is True, after_enter
    # the page raised no exception and the console CSP blocked nothing
    assert out["problems"] == [], out["problems"]
