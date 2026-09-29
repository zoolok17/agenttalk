"""Real-browser check of the console v2 stream and B8 work board (the one browser-driving test is
skipped when no Chromium-family browser or node is present; the launch-flags unit test below it
always runs).

The DOM stub in the node tests models detachment and focus, but only a real browser proves that
(F1) a thread scrolled while detached loses its position and (F2) a redraw that replaces a focused
control drops focus to <body>. The page under test is the REAL /v2 shell and scripts, served with
the console CSP by a tiny stdlib server that also answers the four feeds with fixed JSON (30 chat
messages, two needs cards and two board items, all of whose ages grow on every read). The browser
is driven over the DevTools protocol by tests/console2_browser_check.mjs, which also drives the
board: hash-navigating to #board, then proving its keyed cards keep focus and scroll across an
ordinary redraw exactly like the stream's needs-you cards and chat thread do, plus j/k/Escape.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agenttalk import web

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK = REPO_ROOT / "tests" / "console2_browser_check.mjs"
LEAD = "claude-agenttalk-lead"
PROJECT = "proj-browser"

# PR #230 delta review, F2: the subprocess timeout must exceed the check's OWN worst-case elapsed
# time - the SUM of every phase, each already independent/sequential, never nested (the startup
# phase is now itself ONE shared 90s budget, not two 90s budgets composed - see
# console2_browser_check.mjs's STARTUP_BUDGET_MS/makeDeadline):
#   startup (DevTools up AND a page target found - ONE shared budget) ......... 90.0s
#   post-navigate settle sleep .................................................. 3.5s
#   F1 stream redraw wait (waitForTextChange, its own independent 30s budget) . 30.0s
#   F2 stream redraw wait (waitForTextChange) .................................. 30.0s
#   board-cards-render wait loop (100 attempts x 150ms) ......................... 15.0s
#   board meta redraw wait (waitForTextChange) .................................. 30.0s
#   misc small fixed sleeps and CDP round-trips (keyboard/overlay steps, etc.) ...  5.0s
#                                                                              ----------
#   worst case, one full scenario ............................................ 203.5s
# 240s leaves ~36s of headroom above that computed worst case.
BROWSER_CHECK_TIMEOUT_S = 240


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


def _browser_launch_flags(system: str) -> list[str]:
    """The Chromium-family launch flags for `system` (a `platform.system()`-shaped string:
    "Linux", "Darwin" or "Windows" - never queried internally, so every branch is exercisable
    from any host, including this one, where CI's linux/macos legs cannot actually be run).

    Fix for PR #214: this check was written and only ever exercised against Edge on Windows;
    on a linux CI runner (typically containerized, often effectively root, with a small
    /dev/shm) the SAME flags the Windows leg used let the browser process fail to start at
    all, with no sandbox or shared-memory workaround. macOS needs neither: it is not
    containerized and Chrome's normal sandbox works there in CI the same as anywhere else.

    Fix for PR #221 (N4 follow-up): a headless target with no real OS window can still
    "occlude" itself and drift `document.hidden`/`visibilityState` back to backgrounded mid-run
    (observed even after an explicit `/json/activate` - console2_browser_check.mjs re-asserts it
    before the board section for exactly this reason), which stalls the board's OWN F6 hidden-tab
    pause indefinitely. These two flags are Chromium's own documented switches for headless/CI
    testing to disable that occlusion-based throttling at the source.
    """
    common = [
        "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
        "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
    ]
    if system == "Linux":
        return common + ["--no-sandbox", "--disable-dev-shm-usage"]
    return common


def test_browser_launch_flags_are_correct_per_platform() -> None:
    """Pure-function unit test: runs unconditionally (no browser or node required), and so is
    the one part of this fix actually verified locally - the real launch on linux/macos CI is
    not (see the module docstring)."""
    linux = _browser_launch_flags("Linux")
    assert "--no-sandbox" in linux and "--disable-dev-shm-usage" in linux
    assert "--headless=new" in linux and "--disable-gpu" in linux
    assert "--disable-backgrounding-occluded-windows" in linux and "--disable-renderer-backgrounding" in linux
    for other_system in ("Darwin", "Windows"):
        flags = _browser_launch_flags(other_system)
        assert "--no-sandbox" not in flags and "--disable-dev-shm-usage" not in flags
        assert "--headless=new" in flags and "--disable-gpu" in flags
        assert "--disable-backgrounding-occluded-windows" in flags and "--disable-renderer-backgrounding" in flags


BROWSER = _find_browser()


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


def _handler(monotonic=time.monotonic):
    # PR #230 delta review, F1: aging used to start at HANDLER-CONSTRUCTION time (before Chromium
    # even launches), so a longer (but healthy) browser startup - #229's own fix allows up to 90s -
    # directly ate into the fixture's own 3600s (1h) granularity budget: the reviewer reproduced
    # this exactly by backdating `started` by 65s and getting "waiting 1h" on the very first read.
    # Aging is decoupled from startup duration by starting the clock lazily, on the FIRST request
    # this server actually receives (the earliest a page could possibly be exercising it) rather
    # than at construction time - real startup time, however long, is no longer charged against it.
    # `monotonic` is injectable (round 2, N2) so the real-browser fixture uses real time.monotonic
    # while the F1 regression test below can drive it with fixed readings instead of a real sleep.
    clock = {"started": None}

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
            if clock["started"] is None:
                clock["started"] = monotonic()
            path = self.path.split("?", 1)[0]
            now = datetime.now(timezone.utc)
            grown = int((monotonic() - clock["started"]) * 60)  # cards age 60 s per second: a new label every read
            if path == "/v2":
                self._send(web.render_console2(), "text/html; charset=utf-8", web._DASHBOARD_CSP)
            elif path.startswith("/static/") and path[len("/static/"):] in web._STATIC_ASSETS:
                ctype, data = web._STATIC_ASSETS[path[len("/static/"):]]
                self._send(data, ctype, web._DEFAULT_CSP)
            elif path == "/api/state":
                self._send(json.dumps(_state(now, grown)).encode(), "application/json", web._DEFAULT_CSP)
            elif path == "/api/attention":
                # Both cards now start near zero (not ~1000s+, as before): a card's rendered age
                # label crosses from "Nm" to hour ("Nh") granularity at 3600 simulated seconds,
                # which this fixture's own 60x-accelerated `grown` reaches from a near-zero base
                # about a real MINUTE after `clock["started"]` is set (F1: the FIRST request this
                # server actually answers, not construction time - a slow browser startup, however
                # long, is never charged against this margin) - comfortably longer than this
                # check's own total run time on any realistic runner, however slow. From ~1000s+,
                # that margin was only ~40 real seconds, which is exactly what a real CI runner
                # exceeded (fix). card-2 is kept SLIGHTLY older (oldest-first queue order, "card-2
                # then card-1" - unchanged) so it is still the SURVIVING card once the M4b/R2/R3
                # key-path steps below defer the other one away; console2_browser_check.mjs
                # watches THIS card specifically, by its own stable data-c2-card key, never "the
                # first .c2-age" (which is what a real CI runner's queue/render order handed it,
                # non-deterministically, before this fix).
                items = [
                    {"id": "card-1", "source": "escalation", "source_label": "ESCALATION", "severity": "high",
                     "title": "Question 1", "agent": None, "detail": "why it matters",
                     "age_seconds": grown, "human_can_unblock_now": True},
                    {"id": "card-2", "source": "escalation", "source_label": "ESCALATION", "severity": "high",
                     "title": "Question 2", "agent": None, "detail": "why it matters",
                     "age_seconds": 50 + grown, "human_can_unblock_now": True},
                ]
                self._send(json.dumps({"target_root_project_id": PROJECT, "items": items}).encode(),
                           "application/json", web._DEFAULT_CSP)
            elif path == "/api/work-board":
                # B8: two real-envelope-shaped board items. board-a's last_work_event_at recedes by
                # the SAME accelerated `grown` counter as /api/attention above, so its rendered
                # "active ... ago" meta text changes on every ordinary redraw - the console2_browser_
                # check.mjs board section watches it the same way F1/F2 watch WATCHED_AGE_SELECTOR.
                items = [
                    {"work_item": "board-a", "title": None, "cycle": 1, "round": None, "legacy_cycle": False,
                     "candidate": None,
                     "obligations": [{"request_id": "rq-a1", "recipient": "claude-agenttalk-developer-2",
                                      "stage": "build", "state": "outstanding", "verdict": None}],
                     "verdicts": {}, "integration": {}, "workflow_column": "building", "reason": "build accepted",
                     "evidence": ["rq-a1"], "checks": None, "issues": [],
                     "first_dispatch_at": _iso(now - timedelta(seconds=1800)),
                     "last_work_event_at": _iso(now - timedelta(seconds=30 + grown))},
                    {"work_item": "board-b", "title": "Second item", "cycle": 2, "round": 1,
                     "legacy_cycle": False, "candidate": "a" * 40,
                     "obligations": [{"request_id": "rq-b1", "recipient": "codex-agenttalk-reviewer-1",
                                      "stage": "read", "state": "done", "verdict": "GO"}],
                     "verdicts": {"a" * 40: [{"reviewer": "codex-agenttalk-reviewer-1", "verdict": "GO",
                                              "reply": "rp-b1", "independent": True, "vendor": "openai"}]},
                     "integration": {}, "workflow_column": "ready",
                     "reason": "reviewed; independent GO; local checks not tracked",
                     "evidence": ["rq-b1"], "checks": "local checks not tracked", "issues": [],
                     "first_dispatch_at": _iso(now - timedelta(seconds=3600)),
                     "last_work_event_at": _iso(now - timedelta(seconds=10))},
                ]
                # Padding cards so the list genuinely overflows #c2-board's viewport - the scroll
                # check below is meaningless if two short cards never need a scrollbar at all.
                items += [
                    {"work_item": f"board-pad-{n}", "title": None, "cycle": 1, "round": None,
                     "legacy_cycle": False, "candidate": None,
                     "obligations": [{"request_id": f"rq-pad-{n}", "recipient": "claude-agenttalk-developer-2",
                                      "stage": "build", "state": "outstanding", "verdict": None}],
                     "verdicts": {}, "integration": {}, "workflow_column": "queued", "reason": "start unconfirmed",
                     "evidence": [f"rq-pad-{n}"], "checks": None, "issues": [],
                     "first_dispatch_at": _iso(now - timedelta(seconds=600)),
                     "last_work_event_at": _iso(now - timedelta(seconds=600))}
                    for n in range(12)
                ]
                payload = {
                    "schema_version": 1, "target_root_project_id": PROJECT, "generated_at": _iso(now),
                    "coverage": {"status": "complete"}, "items": items,
                    "legacy": {"open_request_count": 0, "known_lower_bound": 0}, "unassigned": {"count": 0},
                    "total_count": len(items), "truncated": False, "omitted_count": 0, "errors": [],
                    "window_days": 7,
                }
                self._send(json.dumps(payload).encode(), "application/json", web._DEFAULT_CSP)
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
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/v2", tmp_path / "profile"
    finally:
        srv.shutdown()
        srv.server_close()


def test_fixture_aging_starts_on_first_request_not_construction() -> None:
    """PR #230 delta review, F1 regression, made deterministic in round 2 (N2): a real (but short)
    gap between constructing the fixture and its FIRST request (standing in for however long a real
    browser takes to start) must never be charged against the fixture's own aging clock. Before the
    F1 fix, `_handler(started)` took `time.monotonic()` as a constructor argument - a slow start was
    indistinguishable from genuine card age.

    Round 1 exercised this with a real 2s sleep and a loose `first_grown < 10` bound - which was
    itself load-sensitive (N2): do_GET reads the injected clock TWICE on the first request (once to
    lazily set clock["started"], once to compute `grown`), and under real time.monotonic those two
    reads can legitimately drift apart by more than 167ms of scheduler jitter alone, at this
    fixture's 60x acceleration - a correctly-lazy first request descheduled for 200ms between those
    two reads reports grown=12 and fails outright, adding a load-sensitive flake to a flake fix.

    Injecting a controllable `monotonic` callable removes both the real sleep AND the wall-clock
    bound: the TEST advances the clock explicitly (standing in for however long a real gap is,
    without waiting for one), so the exact epoch semantics can be asserted precisely instead of
    merely bounded. A plain canned reading sequence cannot express this: since nothing calls
    monotonic() during the "gap" itself, a sequential fake would hand the same next value to
    "started" whether the code reads it eagerly at construction or lazily at the first request -
    the two are only distinguishable if the clock can advance independently of being read."""

    class FakeMonotonic:
        def __init__(self, value: float) -> None:
            self.value = value

        def __call__(self) -> float:
            return self.value

    fake = FakeMonotonic(100.0)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(monotonic=fake))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        # The exact 65s the reviewer used to reproduce the original bug ("backdating `started` by
        # 65s ... 'waiting 1h' on the very first read") - a healthy but slow browser startup gap,
        # advanced directly with no real sleep and no other code ever reading the clock meanwhile.
        fake.value += 65.0
        with urllib.request.urlopen(f"{base}/api/attention", timeout=5) as resp:  # noqa: S310  # nosec B310  # nosemgrep
            first = json.loads(resp.read())
        # card-2's age is "50 + grown" (see the /api/attention branch below). Lazy (correct): started
        # is set to 165.0 AT this first read, so grown=(165.0-165.0)*60=0 EXACTLY. Eager (the F1 bug):
        # started would have been captured as 100.0 at construction, so grown=(165.0-100.0)*60=3900 -
        # unmistakably not 0.
        assert first["items"][1]["age_seconds"] - 50 == 0
        fake.value += 1.0   # a whole second - exact in binary float, no int()-truncation surprises
        with urllib.request.urlopen(f"{base}/api/attention", timeout=5) as resp:  # noqa: S310  # nosec B310  # nosemgrep
            second = json.loads(resp.read())
        # grown=(166.0-165.0)*60=60 EXACTLY - ages forward from the FIRST request's start, not from
        # construction, and is not re-latched on every request.
        assert second["items"][1]["age_seconds"] - 50 == 60
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser on this runner")
@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this runner")
def test_thread_scroll_and_focus_survive_redraws_in_a_real_browser(page) -> None:
    url, profile = page
    flags = _browser_launch_flags(platform.system())
    result = subprocess.run(
        ["node", str(CHECK), BROWSER, url, str(profile), json.dumps(flags)],
        capture_output=True, text=True, encoding="utf-8", timeout=BROWSER_CHECK_TIMEOUT_S, cwd=REPO_ROOT,
    )
    # A browser binary that exists but fails to start is a hard FAILURE, never a skip -
    # result.stderr carries the launched process's own stderr on that path (see the .mjs).
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
    # M4b/R2: one key path (run first, focus still untouched from page load) - j selects the first
    # card (A), Enter focuses its first action (Later)
    assert out["firstCardSelectedAfterJ"] is True, out
    after_enter = out["focusAfterEnterOnSelected"]
    assert after_enter["tag"] == "BUTTON" and after_enter["sameAsFirstCardLater"] is True, after_enter
    # R3: a second j must move real focus WITH the selection, onto card B itself - never left behind
    # on card A's Later
    assert out["secondJMovedFocusToCardB"] is True, out
    # R2/R3: Enter opens B (the actually-highlighted card, an interactive control - its Later button);
    # a second native Enter must defer B specifically, never A, never diverted back to the selection
    assert out["focusAfterEnterOnB"]["tag"] == "BUTTON", out
    assert out["cardsAfterSecondEnter"] == 1, out
    assert out["remainingCardIsA"] is True, out
    # R2: a native Enter on a focused, unrelated interactive control (a theme button), with a card
    # still selected, must activate THAT control - never get diverted to the selection shortcut
    assert out["themeButtonKeptNativeEnter"] is True, out
    # R4: closing help restores focus to wherever it was invoked from - here, a focused Later button -
    # never a blanket default to the ? button
    assert out["r4FocusInOverlay"] is True, out
    assert out["r4FocusRestoredToInvoker"] is True, out
    assert out["r4InvokerStillConnected"] is True, out
    # M4b: the ? button opens the keyboard overlay, moves focus into it
    assert out["overlayOpenAfterClick"] is True, out
    assert out["focusInOverlayAfterClick"] is True, out
    # R1: native Tab and Shift+Tab are trapped on the overlay's one focusable control, never escaping
    # to the page beneath (the "Classic view" link); the background is genuinely `inert` while open
    assert out["focusAfterNativeTab"] is True, out
    assert out["focusAfterNativeShiftTab"] is True, out
    assert out["backgroundInertWhileOpen"] is True, out
    # R1: a native Enter on the (correctly still-focused) Close button closes the dialog and never
    # reaches the link outside it - the page never navigates away, and the background un-inerts
    assert out["overlayClosedAfterNativeEnterOnClose"] is True, out
    assert out["pathUnchangedAfterNativeEnter"] is True, out
    assert out["backgroundInertRemovedAfterClose"] is True, out
    # a native Escape still closes the overlay too, and returns focus to the button that opened it
    assert out["overlayClosedAfterNativeEscape"] is True, out
    assert out["focusBackOnKeysBtnAfterEscape"] is True, out
    # B8: the board - hash-navigating to #board swaps the stream/rail out for the board/detail pair
    assert out["boardShellSwapped"] is True, out
    # j selects the first card with REAL focus and the selected class together
    assert out["boardFirstSelectedWithFocus"] is True, out
    # a second j moves focus WITH the selection onto the next card, never left behind
    assert out["boardSecondJMovedFocus"] is True, out
    # the detail panel shows the selection (not the "select a card" placeholder) once one exists
    assert out["boardDetailShowsSelection"] is True, out
    # scrolling the board, then an ordinary redraw (the watched card's own age ticked) - the
    # container, the selected card and its focus all survive, exactly like the stream's thread/rail
    assert out["boardScrollKept"] is True, out
    assert out["boardContainerKept"] is True, out
    assert out["boardFocusKept"] is True, out
    # Escape clears the board selection
    assert out["boardSelectionClearedByEscape"] is True, out
    # F4: native Enter and Space (real CDP key events, not synthetic DOM ones) activate whichever
    # board card actually holds focus - a role="button" article gets none of this for free
    assert out["boardEnterActivatesFocusedCard"] is True, out
    assert out["boardSpaceActivatesFocusedCard"] is True, out
    # N2 (PR #221 delta round 2): the board's own Enter/Space handler must consume a key ONLY when
    # it actually handled a board card - a focused Conversation link or help button, both real
    # server-rendered/page-built controls unrelated to any card, keep their own native activation
    assert out["n2ConversationLinkNavigatesOnEnter"] is True, out
    assert out["n2HelpButtonStillOpensOnEnter"] is True, out
    # the page raised no exception and the console CSP blocked nothing
    assert out["problems"] == [], out["problems"]


@pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser on this runner")
@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this runner")
def test_check_catches_a_redraw_that_breaks_scroll_after_setup(page) -> None:
    """Negative control for dev-4 finding F1: console2_browser_check.mjs's own "scroll" sabotage
    mode installs a MutationObserver that snaps the thread back to its bottom on every age
    redraw. If the scroll-setup (capturing the thread, writing scrollTop=250) and the age
    baseline it waits against were ever split back into two separate CDP round-trips, a redraw
    landing BETWEEN them would satisfy waitForAgeChange on evidence of the WRONG redraw - one
    that happened BEFORE our own scrollTop=250 write - and this sabotage would go completely
    undetected (afterRedraw.top would still read 250, the value WE just wrote, not yet touched
    by the sabotage's own reaction to a LATER redraw). With setup and baseline atomic, the
    sabotage is reliably caught: this must fail (afterRedraw.top != 250), never pass.
    """
    url, profile = page
    flags = _browser_launch_flags(platform.system())
    result = subprocess.run(
        ["node", str(CHECK), BROWSER, url, str(profile), json.dumps(flags), "scroll"],
        capture_output=True, text=True, encoding="utf-8", timeout=BROWSER_CHECK_TIMEOUT_S, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["ageMoved"] is True, "the redraws really happened"
    assert out["afterRedraw"]["top"] != 250, (
        "the sabotage should have snapped scrollTop away from 250 on the redraw that followed "
        "setup - if this is 250, the atomic setup+baseline fix has regressed and a redraw "
        "landing before setup is being mistaken for one that happened after it"
    )
