"""Console v2 (``/v2``) - milestone M1: route, shell, assets, security lint, tokens.

The v2 console is a new document route beside the classic ``/dashboard``. These
tests pin what must not change (the CSP, the read-only posture, the classic
console) and what the new files must never contain (markup injection, inline
style, links built from data, write requests). The JS behaviour is tested under
node by ``console2_model.test.mjs`` / ``console2_render.test.mjs`` and run from
here, skipped when node is absent (same convention as the classic console tests).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import pytest

from agenttalk import web
from agenttalk.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC = REPO_ROOT / "src" / "agenttalk" / "web_static"
V2_ASSETS = {
    "console2.css": "text/css",
    "console2-model.js": "application/javascript",
    "console2-board-model.js": "application/javascript",
    "console2.js": "application/javascript",
}
V2_JS = ("console2-model.js", "console2-board-model.js", "console2.js")


# --------------------------------------------------------------- helpers

def _serve(tmp_path: Path, *, enable_actions: bool = False):
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return web.serve_in_thread(s, host="127.0.0.1", port=0, enable_actions=enable_actions)


def _get(url: str):
    return urllib.request.urlopen(url, timeout=5)  # noqa: S310  # nosec B310  # nosemgrep


def _status_of(url: str, *, method: str = "GET", data: bytes | None = None) -> int:
    req = urllib.request.Request(url, method=method, data=data)  # noqa: S310  # nosec B310  # nosemgrep
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310  # nosec B310  # nosemgrep
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)(^|\s)//.*$", r"\1", src)


def _strip_css_comments(src: str) -> str:
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


class _ScriptTagCollector(HTMLParser):
    """Every ``<script>`` tag, however it is cased or spaced - the HTML5 tokenizer (and this
    stdlib parser) treats tag and attribute names case-insensitively, so ``<script>``, ``<SCRIPT>``
    and ``<ScRiPt>`` are all found the same way. This is the fix for py/bad-tag-filter: a regex
    anchored on a literal lowercase ``<script`` (as the old check was) can be fooled by upper or
    mixed case; a real parser cannot."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[str | None, bool]] = []  # (src attribute, has inline text)
        self._depth = 0
        self._src: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._depth += 1
            self._src = dict(attrs).get("src")
            self._text = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.scripts.append((dict(attrs).get("src"), False))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._depth:
            self._depth -= 1
            self.scripts.append((self._src, "".join(self._text).strip() != ""))

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._text.append(data)


def _find_scripts(page: str) -> list[tuple[str | None, bool]]:
    """Every ``<script>`` tag in `page`, paired with whether it carries inline (non-whitespace)
    text content - case-insensitively, unlike a hand-rolled regex."""
    parser = _ScriptTagCollector()
    parser.feed(page)
    parser.close()
    return parser.scripts


# ------------------------------------------------------------ route & shell

def test_v2_shell_uses_the_dashboard_csp_byte_for_byte(tmp_path: Path) -> None:
    srv, _t, base = _serve(tmp_path)
    try:
        with _get(f"{base}/v2") as v2, _get(f"{base}/dashboard") as classic:
            assert v2.status == 200
            assert v2.headers["Content-Type"].startswith("text/html")
            assert v2.headers["Content-Security-Policy"] == web._DASHBOARD_CSP
            assert v2.headers["Content-Security-Policy"] == classic.headers["Content-Security-Policy"]
            for header in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Cache-Control"):
                assert v2.headers[header] == classic.headers[header], header
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_console_csp_itself_is_unchanged() -> None:
    assert web._DASHBOARD_CSP == (
        "default-src 'none'; script-src 'self'; connect-src 'self'; "
        "style-src 'self'; img-src 'self'; frame-ancestors 'none'"
    )
    assert "unsafe" not in web._DASHBOARD_CSP


def test_v2_shell_has_no_inline_anything_and_no_external_url(tmp_path: Path) -> None:
    srv, _t, base = _serve(tmp_path)
    try:
        with _get(f"{base}/v2") as resp:
            page = resp.read().decode("utf-8")
    finally:
        srv.shutdown()
        srv.server_close()
    assert "<style" not in page
    assert "style=" not in page
    assert not re.search(r"\son[a-z]+\s*=", page)
    assert not re.search(r"(?i)javascript:", page)
    assert "http://" not in page and "https://" not in page
    scripts = _find_scripts(page)
    assert scripts, "expected at least the two served <script src> tags"
    assert not any(has_inline for _src, has_inline in scripts), "a <script> tag carries inline content"
    assert all(src is not None and src.startswith("/static/console2") for src, _has_inline in scripts)
    assert "<link rel=\"stylesheet\" href=\"/static/console2.css\">" in page
    # every link is a fixed server-authored path: /dashboard plus the two B8 route anchors
    assert sorted(set(re.findall(r"<a\b[^>]*href=\"([^\"]*)\"", page))) == ["#board", "#conversation", "/dashboard"]
    for region in ("c2-header", "c2-stream", "c2-rail", "c2-board", "c2-board-detail", "c2-footer", "c2-hints"):
        assert f"id=\"{region}\"" in page
    assert "spec-kitty" not in page.lower()
    # nothing bus-derived is rendered server-side
    assert "alpha" not in page and "beta" not in page


def test_script_tag_check_is_not_fooled_by_case_or_spacing() -> None:
    """Negative control for py/bad-tag-filter: the inline-script check above must not be fooled by
    an upper-case or mixed-case <script> tag, or by extra whitespace before the closing angle
    bracket - it must FIND the tag and correctly report it as carrying inline content."""
    upper = '<script src="/static/console2.js"></script><SCRIPT>x</SCRIPT>'
    mixed = '<script src="/static/console2.js"></script><ScRiPt >x</ScRiPt >'
    for page in (upper, mixed):
        scripts = _find_scripts(page)
        assert len(scripts) == 2, (page, scripts)
        assert any(has_inline for _src, has_inline in scripts), (page, scripts)
        # confirms the check is genuinely catching it, not merely finding an unrelated tag:
        # the legitimate served script must still be recognised as inline-free
        assert any(src == "/static/console2.js" and not has_inline for src, has_inline in scripts)


def test_classic_console_is_untouched_by_the_new_route(tmp_path: Path) -> None:
    srv, _t, base = _serve(tmp_path)
    try:
        for path in ("/dashboard", "/"):
            with _get(f"{base}{path}") as resp:
                page = resp.read().decode("utf-8")
            assert "/static/console.css" in page and "/static/console.js" in page
            assert "console2" not in page
        with _get(f"{base}/dashboard") as resp:
            assert resp.read() == web.render_dashboard([])   # the shell does not depend on the roots
    finally:
        srv.shutdown()
        srv.server_close()


def test_classic_console_links_to_v2_with_a_fixed_path() -> None:
    src = _read("console.js")
    assert "v2Link.setAttribute('href', '/v2')" in src


def test_v2_route_is_read_only_and_adds_no_write_path(tmp_path: Path) -> None:
    srv, _t, base = _serve(tmp_path)
    try:
        assert _status_of(f"{base}/v2", method="POST", data=b"x") == 405
        assert _status_of(f"{base}/v2", method="PUT", data=b"x") == 405
        assert _status_of(f"{base}/api/session") == 404       # no session without --enable-actions
        assert _status_of(f"{base}/v2?root=whatever") == 200   # the query is ignored server-side
        for asset in V2_ASSETS:
            assert _status_of(f"{base}/static/{asset}", method="POST", data=b"x") == 405, asset
    finally:
        srv.shutdown()
        srv.server_close()


# ------------------------------------------------------------------ assets

def test_v2_assets_are_served_with_the_right_type_and_default_csp(tmp_path: Path) -> None:
    srv, _t, base = _serve(tmp_path)
    try:
        for name, ctype in V2_ASSETS.items():
            assert name in web._STATIC_ASSETS, name
            with _get(f"{base}/static/{name}") as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"].startswith(ctype), name
                assert resp.headers["Content-Security-Policy"] == web._DEFAULT_CSP, name
                assert resp.read() == (STATIC / name).read_bytes(), name
    finally:
        srv.shutdown()
        srv.server_close()


def test_allowlist_stays_exact_for_the_new_names(tmp_path: Path) -> None:
    srv, _t, base = _serve(tmp_path)
    try:
        for bad in (
            "console2.css.bak", "console2.js/", "console2.JS", "Console2.js", "console2-model.js%00",
            "..%2Fconsole2.js", "avatars/..%2Fconsole2.js", "console3.js", "console2-model.mjs",
        ):
            assert _status_of(f"{base}/static/{bad}") == 404, bad
    finally:
        srv.shutdown()
        srv.server_close()


def test_only_the_expected_v2_asset_keys_were_added() -> None:
    keys = {k for k in web._STATIC_ASSETS if "console" in k}
    assert keys == {"console.css", "console.js", *V2_ASSETS}


# -------------------------------------------------- source lint (security)

JS_BANNED = {
    "markup injection": (
        r"\binnerHTML\b|\bouterHTML\b|insertAdjacentHTML|document\.write|createContextualFragment|DOMParser"
    ),
    "dynamic code": r"\beval\s*\(|new\s+Function\b|\bFunction\s*\(|set(?:Timeout|Interval)\s*\(\s*['\"`]",
    "inline style": r"setAttribute\(\s*['\"]style|\.cssText\b|\.style\s*=[^=]|['\"]style\s*=",
    "link or source from data": (
        r"\.href\s*=(?!=)|\.src\s*=(?!=)|\.action\s*=(?!=)|setAttribute\(\s*['\"](?:href|src|srcdoc|action|formaction)|"
        r"javascript:|data:text"
    ),
    "event handler attribute": r"\.on[a-z]+\s*=[^=]|setAttribute\(\s*['\"]on",
    "external URL": r"https?://|//cdn|\bwss?://",
    "other channels": r"XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts|postMessage|document\.cookie",
    "write request (read-only slice)": r"\bmethod\s*:|\bbody\s*:|X-CSRF",
    "removed surface": r"(?i)spec-kitty|\bmission\b",
}


@pytest.mark.parametrize("name", V2_JS)
@pytest.mark.parametrize("what", sorted(JS_BANNED))
def test_v2_js_source_lint(name: str, what: str) -> None:
    if name == "console2-model.js" and what == "write request (read-only slice)":
        pytest.skip("the model never makes requests (see the no-requests test); its view objects use a body key")
    if name == "console2.js" and what == "link or source from data":
        pytest.skip("one sanctioned src assignment exists (M4 avatars); see test_v2_js_avatar_src_is_narrowly_gated")
    code = _strip_js_comments(_read(name))
    hit = re.search(JS_BANNED[what], code)
    assert hit is None, f"{name}: {what}: {hit.group(0)!r}"


def test_v2_js_avatar_src_is_narrowly_gated_and_nothing_else_sets_a_link_or_source() -> None:
    """M4 relaxation of "link or source from data": exactly one place ever assigns `src`, only for
    an already-served avatar image, only after checking the model's own closed allowlist, and href/
    action/srcdoc/formaction stay completely banned everywhere."""
    code = _strip_js_comments(_read("console2.js"))
    src_sites = re.findall(r"setAttribute\(\s*['\"]src['\"]|\.src\s*=(?!=)", code)
    assert len(src_sites) == 1, src_sites

    fn = re.search(r"function avatarNode\([^)]*\)\s*\{(.*?)\n  \}", code, re.S)
    assert fn is not None, "the src assignment must live in a single, narrowly-named function"
    body = fn.group(1)
    assert re.search(r"setAttribute\(\s*['\"]src['\"]|\.src\s*=(?!=)", body), "not inside avatarNode"
    assert re.search(r"hasOwn\(\s*AVATAR_FILE_SET\s*,", body), "no allowlist membership check before use"
    assert "'/static/avatars/' +" in body, "the path prefix must be a fixed literal, matching the server route"

    other = re.search(
        r"\.href\s*=(?!=)|\.action\s*=(?!=)|setAttribute\(\s*['\"](?:href|srcdoc|action|formaction)['\"]", code,
    )
    assert other is None, other


def test_v2_avatar_allowlist_matches_the_servers_static_assets() -> None:
    """Every filename the model can ever produce is one the server actually serves under
    /static/avatars/<file> (agenttalk.avatars.AVATAR_ASSETS, exposed via web._STATIC_ASSETS) -
    client and server are never out of step on what an avatar file name may be."""
    model_src = _read("console2-model.js")
    assert "var AVATAR_FILES = HEX_MOTIFS.map(function (m) { return 'hexagon-' + m + '.png'; });" in model_src
    motifs_block = re.search(r"var HEX_MOTIFS = \[(.*?)\];", model_src, re.S)
    assert motifs_block is not None
    motifs = re.findall(r"'([a-z]+)'", motifs_block.group(1))
    assert len(motifs) == 10
    files = [f"hexagon-{m}.png" for m in motifs]
    for f in files:
        assert f"avatars/{f}" in web._STATIC_ASSETS, f


ALLOWED_API_PATHS = {"/api/state", "/api/attention", "/api/lead-chat", "/api/work-board"}


@pytest.mark.parametrize("name", ("console2-model.js", "console2-board-model.js"))
def test_v2_model_makes_no_requests_and_touches_no_dom(name: str) -> None:
    code = _strip_js_comments(_read(name))
    for pattern in (r"\bfetch\b", r"\bdocument\b", r"\blocalStorage\b", r"\bsetTimeout\b", r"\bsetInterval\b",
                    r"\bXMLHttpRequest\b", r"\bnavigator\b", r"Date\.now\("):
        assert not re.search(pattern, code), pattern


def test_v2_js_reads_only_the_three_feeds_through_one_fetch() -> None:
    code = _strip_js_comments(_read("console2.js"))
    assert len(re.findall(r"\bfetch\(", code)) == 1, "one request helper (getJson), nothing else"
    assert "{ cache: 'no-store' }" in code
    literals = set(re.findall(r"'(/api/[^']*)'", code))
    assert literals == ALLOWED_API_PATHS, literals
    assert "/api/intent" not in code and "/api/session" not in code


def test_v2_js_bounds_every_request_with_a_timeout_and_an_abort_signal() -> None:
    code = _strip_js_comments(_read("console2.js"))
    assert "AbortController" in code and "REQUEST_TIMEOUT_MS" in code
    assert "signal" in code and "clearTimeout" in code
    assert re.search(r"var REQUEST_TIMEOUT_MS = \d{4};", code)


def test_v2_js_uses_textcontent_and_creates_no_markup() -> None:
    code = _strip_js_comments(_read("console2.js"))
    assert "textContent" in code
    assert "createElement" in code
    assert "createTextNode" in code   # text nodes only


def test_v2_css_has_no_remote_or_font_loading() -> None:
    css = _strip_css_comments(_read("console2.css"))
    banned = (r"@font-face", r"@import", r"url\(", r"https?://", r"expression\(", r"behavior\s*:", r"-moz-binding")
    for pattern in banned:
        assert not re.search(pattern, css, flags=re.I), pattern
    assert not re.search(r"(?i)spec-kitty|\bmission\b", css)


def test_v2_files_do_not_leak_machine_names() -> None:
    blob = "\n".join(_read(n) for n in (*V2_ASSETS,)).lower()
    assert "win-ws01" not in blob and "linux-02" not in blob
    assert "gethostname" not in blob and "hostname" not in blob


# ------------------------------------------------------------------ tokens

THEME_KEYS = ("bg", "panel", "panel2", "border", "fg", "dim", "accent", "accent-ink",
              "ok", "warn", "bad", "info", "serif")

# Values of design/console-v2/tokens/themes.json (handoff v2). Paper's dim and accent are the two
# contrast fixes recommended by 06-RULES ("Action needed - Paper"): themes.json has #857F74 / #E4572E.
EXPECTED = {
    "midnight": dict(zip(THEME_KEYS, ("#0A0C11", "#11141B", "#161A23", "#232834", "#E7EAF0", "#7D8595",
                                      "#7C8CFF", "#0A0C11", "#3DD68C", "#F5B83D", "#FF6B6B", "#5CC8FF",
                                      "#F2F3F7"), strict=True)),
    "paper": dict(zip(THEME_KEYS, ("#F6F3EC", "#FFFFFF", "#FBF9F4", "#E5E0D4", "#1D1B17", "#6B655B",
                                   "#C9481F", "#FFFFFF", "#2E9F6B", "#B7780A", "#D2402F", "#2F6FC9",
                                   "#1D1B17"), strict=True)),
    "synthwave": dict(zip(THEME_KEYS, ("#120822", "#1B0F33", "#231342", "#3B2266", "#FBEFFF", "#A88CCB",
                                       "#FF4FD8", "#120822", "#00F5D4", "#FFD23F", "#FF5C8A", "#00E5FF",
                                       "#FFE3FA"), strict=True)),
    "terminal": dict(zip(THEME_KEYS, ("#040906", "#07100A", "#0B170F", "#1B3A24", "#7CFF9B", "#52A06B",
                                      "#DFFFE7", "#040906", "#7CFF9B", "#FFD166", "#FF5C5C", "#7FD8FF",
                                      "#7CFF9B"), strict=True)),
}
PAPER_FIXED = {"dim", "accent"}
RUNTIME = {"dark": ("#D08A5E", "#4FB3A0", "#9E82E0"), "light": ("#B0532C", "#2C7A6B", "#6D4AC0")}


def _theme_blocks() -> dict[str, dict[str, str]]:
    css = _strip_css_comments(_read("console2.css"))
    blocks: dict[str, dict[str, str]] = {}
    for m in re.finditer(r":root(?:\[data-theme=\"([a-z]+)\"\])?\s*\{([^}]*)\}", css):
        name = m.group(1) or "midnight"
        decls = dict(re.findall(r"--([a-z0-9-]+)\s*:\s*([^;]+);", m.group(2)))
        blocks[name] = {k: v.strip() for k, v in decls.items()}
    return blocks


def test_four_theme_blocks_with_every_key() -> None:
    blocks = _theme_blocks()
    assert set(blocks) == {"midnight", "paper", "synthwave", "terminal"}
    for name, decls in blocks.items():
        for key in THEME_KEYS:
            assert key in decls, (name, key)


def test_theme_values_match_the_handoff_tokens() -> None:
    blocks = _theme_blocks()
    for name, expected in EXPECTED.items():
        for key, want in expected.items():
            got = blocks[name][key]
            if name == "terminal" and key == "serif":
                assert got in (want, "var(--fg)")
            else:
                assert got.upper() == want, (name, key, got)


def test_theme_values_match_the_design_file_when_present() -> None:
    path = REPO_ROOT / "design" / "console-v2" / "tokens" / "themes.json"
    if not path.is_file():
        pytest.skip("design/console-v2 is not in this tree (PR #202 not merged)")
    design = {t["name"]: t for t in json.loads(path.read_text(encoding="utf-8"))["themes"]}
    blocks = _theme_blocks()
    for name, theme in design.items():
        for key, value in theme.items():
            css_key = "accent-ink" if key == "accentInk" else key
            if key == "name" or (name == "paper" and key in PAPER_FIXED):
                continue
            assert blocks[name][css_key].upper() == value.upper(), (name, key)


def test_runtime_badge_colours_dark_and_paper_light() -> None:
    blocks = _theme_blocks()
    rt = tuple(f"rt-{r}" for r in ("claude", "codex", "qwen"))
    assert tuple(blocks["midnight"][k].upper() for k in rt) == RUNTIME["dark"]
    assert tuple(blocks["paper"][k].upper() for k in rt) == RUNTIME["light"]
    for name in ("synthwave", "terminal"):   # inherit the dark set from :root
        assert not set(rt) & set(blocks[name]), name


# WCAG 2 relative luminance / contrast ratio (https://www.w3.org/TR/WCAG21/#dfn-relative-luminance).
def _channel(c: int) -> float:
    s = c / 255
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# Only Midnight is styled and reviewed in this slice (see the CSS header). Pairs below are every
# text-token-on-background-token combination the CSS actually uses to paint readable text (fg,
# dim, serif, warn, bad, ok, info as `color`, over bg/panel/panel2 as `background`), plus
# accent-ink on accent (the one token pair whose own name says "ink for this background").
MIDNIGHT_TEXT_ON_BG = [
    ("fg", "bg"), ("fg", "panel"), ("fg", "panel2"),
    ("dim", "bg"), ("dim", "panel"), ("dim", "panel2"),
    ("serif", "bg"), ("serif", "panel"),
    ("warn", "panel"), ("warn", "panel2"),
    ("bad", "panel"), ("bad", "panel2"),
    ("ok", "panel"), ("ok", "panel2"),
    ("info", "panel"), ("info", "panel2"),
    ("accent-ink", "accent"),
]


def test_midnight_text_tokens_meet_wcag_aa_contrast_on_their_backgrounds() -> None:
    midnight = _theme_blocks()["midnight"]
    for text_key, bg_key in MIDNIGHT_TEXT_ON_BG:
        ratio = _contrast(midnight[text_key], midnight[bg_key])
        assert ratio >= 4.5, (text_key, bg_key, midnight[text_key], midnight[bg_key], round(ratio, 2))


def _composite(fg_hex: str, bg_hex: str, alpha: float) -> str:
    """The colour a browser actually paints when `fg_hex` is shown at `alpha` opacity over `bg_hex` -
    i.e. what a token-pair check alone cannot see: an *opacity* rule washes text out toward the
    background, and a flat pair check on the tokens themselves would never notice."""
    fg, bg = fg_hex.lstrip("#"), bg_hex.lstrip("#")
    out = []
    for i in (0, 2, 4):
        f, b = int(fg[i:i + 2], 16), int(bg[i:i + 2], 16)
        out.append(round(f * alpha + b * (1 - alpha)))
    return "#" + "".join(f"{c:02x}" for c in out)


# N3 (fix round 2): the design's blanket "50% greying" for stale (offline/silent) data, applied to
# whole regions including readable text, measured well under 4.5:1 once actually composited over its
# background - a wash-out, not a legibility cue. The lead's call: last-known TEXT stays fully
# readable; only decorative elements (avatars, usage meters) still dim. Recorded as an accessibility
# deviation from the design's literal "50% greying" in the step doc (section 22).
def test_the_designs_blanket_greying_would_have_failed_contrast_composited() -> None:
    midnight = _theme_blocks()["midnight"]
    # Documents the N3 finding precisely: --dim text at the design's 50% opacity, composited over
    # --panel2 (the rail's own background), reads at roughly 2:1 - nowhere near 4.5:1.
    composited = _composite(midnight["dim"], midnight["panel2"], 0.5)
    ratio = _contrast(composited, midnight["panel2"])
    assert ratio < 3.0, (composited, round(ratio, 2))


# N3 (fix round 3, narrowed): the app-wide stale rule dimmed the whole `.c2-avatar` box, including
# the informational runtime-badge letter (C/X/Q) nested inside it - real Edge measured 2.60/2.82/2.47:1
# for the three runtimes once the badge's own glyph-on-fill colours were ALSO composited at the
# parent's 0.5 opacity. A selector-substring blocklist cannot see this: `.c2-avatar` never mentions
# "badge" in its text, yet dims it anyway by simple CSS inheritance/compositing of the whole subtree.
# So this is an ALLOWLIST of exact selectors instead: every `is-stale` rule that still declares an
# `opacity` must target one of exactly these leaf, non-text elements - nothing broader.
ALLOWED_STALE_OPACITY_SELECTORS = {
    ".is-stale .c2-avatar-img",
    ".is-stale .c2-meter",
    ".c2-usage-row.is-stale .c2-meter",
}


def test_stale_greying_dims_only_an_allowlisted_set_of_decorative_selectors() -> None:
    css = _strip_css_comments(_read("console2.css"))
    stale_rules = re.findall(r"([^{}]*\bis-stale\b[^{}]*)\{([^}]*)\}", css)
    assert stale_rules, "expected at least the app-wide and per-window stale rules"
    found = set()
    for selector, decls in stale_rules:
        if "opacity" not in decls:
            continue
        for part in selector.split(","):
            part = part.strip()
            assert part in ALLOWED_STALE_OPACITY_SELECTORS, (part, decls)
            found.add(part)
    assert found == ALLOWED_STALE_OPACITY_SELECTORS, found


# The avatar's runtime-badge letter (bg-as-text on a runtime-coloured fill) at full opacity - it is
# never dimmed by the stale state (checked above), so this is the contrast that actually applies.
BADGE_TEXT_ON_FILL = [("bg", "dim"), ("bg", "rt-claude"), ("bg", "rt-codex"), ("bg", "rt-qwen")]


def test_avatar_badge_text_meets_wcag_aa_contrast_undimmed_by_the_stale_state() -> None:
    midnight = _theme_blocks()["midnight"]
    for text_key, bg_key in BADGE_TEXT_ON_FILL:
        ratio = _contrast(midnight[text_key], midnight[bg_key])
        assert ratio >= 4.5, (text_key, bg_key, midnight[text_key], midnight[bg_key], round(ratio, 2))


def test_the_badge_at_the_old_whole_avatar_opacity_would_have_failed_contrast() -> None:
    # Documents the N3-narrowed finding: had the badge been dimmed along with the image (the old
    # `.c2-avatar` rule), both its fill AND its own text colour would have composited toward the
    # background at 0.5, reading well under 4.5:1 for every runtime - real Edge measured 2.60/2.82/2.47:1.
    midnight = _theme_blocks()["midnight"]
    for _text_key, bg_key in BADGE_TEXT_ON_FILL:
        composited_bg = _composite(midnight[bg_key], midnight["panel"], 0.5)
        composited_text = _composite(midnight["bg"], midnight["panel"], 0.5)
        ratio = _contrast(composited_text, composited_bg)
        assert ratio < 3.5, (bg_key, composited_text, composited_bg, round(ratio, 2))


def test_terminal_changes_only_fonts_and_radii() -> None:
    blocks = _theme_blocks()
    layout_keys = {"font-ui", "font-serif", "r-card", "r-btn", "r-pill", "r-logo"}
    terminal_layout = {k for k in blocks["terminal"] if k in layout_keys}
    assert terminal_layout == layout_keys
    assert all(blocks["terminal"][k] == "0" for k in ("r-card", "r-btn", "r-pill", "r-logo"))
    assert blocks["terminal"]["font-ui"] == "var(--font-mono)"
    assert blocks["terminal"]["font-serif"] == "var(--font-mono)"
    # the other themes never override fonts or radii: layout and content stay identical
    for name in ("paper", "synthwave"):
        assert not layout_keys & set(blocks[name]), name


def test_font_stacks_are_system_fonts_only() -> None:
    css = _strip_css_comments(_read("console2.css"))
    stacks = re.findall(r"--font-(?:ui|mono|serif):\s*([^;]+);", css)
    assert len(stacks) >= 3
    banned = ("geist", "instrument", "jetbrains", "space grotesk", "sora", "archivo", "space mono")
    for stack in stacks:
        assert not any(b in stack.lower() for b in banned), stack


def test_stream_styling_uses_theme_variables_not_colour_literals() -> None:
    css = _strip_css_comments(_read("console2.css"))
    outside = re.sub(r":root(?:\[data-theme=\"[a-z]+\"\])?\s*\{[^}]*\}", "", css)
    literals = set(re.findall(r"#[0-9A-Fa-f]{3,8}\b", outside))
    assert literals <= {"#111"}, literals            # #111 is the badge ink on the warn colour
    assert not re.search(r"\brgba?\(", outside), "colours come from the theme variables"


def test_composer_is_pinned_to_the_bottom_of_the_stream() -> None:
    css = _strip_css_comments(_read("console2.css"))
    block = re.search(r"\.c2-composer\s*\{([^}]*)\}", css).group(1)
    assert "position: sticky" in block and "bottom:" in block


def test_the_live_dot_pulses_and_prefers_reduced_motion_turns_it_off() -> None:
    """08-INTERACTIONS: "Live dot pulse 2s ease-in-out (opacity 1->.35); nothing else animates.
    prefers-reduced-motion: no pulse." """
    css = _strip_css_comments(_read("console2.css"))
    assert re.search(r"\.c2-dot\.is-live\s*\{[^}]*animation:\s*c2-pulse\s+2s\s+ease-in-out\s+infinite", css)
    keyframes = re.search(r"@keyframes\s+c2-pulse\s*\{([^}]*\{[^}]*\}[^}]*)\}", css)
    assert keyframes is not None
    assert "opacity: 1" in keyframes.group(1) and "opacity: .35" in keyframes.group(1)
    reduced = re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([^}]*\{[^}]*\}[^}]*)\}", css)
    assert reduced is not None
    assert "animation: none" in reduced.group(1)
    # nothing else in the stylesheet declares an animation (only the dot pulses)
    outside_media = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
    others = re.findall(r"([a-z0-9.:\[\]\"=_-]+)\s*\{[^}]*animation:", outside_media)
    assert others == [".c2-dot.is-live"], others


def test_retry_button_is_the_only_control_in_the_offline_banner() -> None:
    css = _strip_css_comments(_read("console2.css"))
    assert re.search(r"\.c2-retry\s*\{", css)
    assert "cursor: pointer" in re.search(r"\.c2-retry\s*\{([^}]*)\}", css).group(1)


def test_avatar_css_only_hides_the_image_in_terminal_never_via_a_style_attribute() -> None:
    css = _strip_css_comments(_read("console2.css"))
    assert 'data-theme="terminal"] .c2-avatar-img { display: none; }' in css
    assert "style=" not in css and "!important" not in css


def test_layout_matches_the_spec_grid() -> None:
    css = _strip_css_comments(_read("console2.css"))
    assert "grid-template-columns: minmax(0, 1fr) 340px" in css
    assert "grid-template-rows: 58px minmax(0, 1fr) 32px" in css
    assert "min-width: 1024px" in css


# --------------------------------------------------------------- node tests

@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize(
    "script",
    [
        "console2_model.test.mjs", "console2_view.test.mjs", "console2_render.test.mjs",
        "console2_data.test.mjs", "console2_stream.test.mjs", "console2_needs_you.test.mjs",
        "console2_board_model.test.mjs", "console2_board_app.test.mjs",
    ],
)
def test_console2_node_tests(script: str) -> None:
    r = subprocess.run(
        ["node", str(REPO_ROOT / "tests" / script)],
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=REPO_ROOT, env={**os.environ, "TZ": "UTC"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    last = r.stdout.strip().splitlines()[-1]
    passed, total = re.search(r"(\d+)/(\d+) passed", last).groups()
    assert passed == total and int(total) > 0, last


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("name", V2_JS)
def test_console2_js_passes_node_check(name: str) -> None:
    r = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
