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
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agenttalk import web
from agenttalk.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC = REPO_ROOT / "src" / "agenttalk" / "web_static"
V2_ASSETS = {
    "console2.css": "text/css",
    "console2-model.js": "application/javascript",
    "console2.js": "application/javascript",
}
V2_JS = ("console2-model.js", "console2.js")


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
    scripts = re.findall(r"<script\b[^>]*>", page)
    assert scripts and all(" src=\"/static/console2" in tag for tag in scripts)
    assert "<link rel=\"stylesheet\" href=\"/static/console2.css\">" in page
    # the two links are fixed server-authored paths
    assert sorted(set(re.findall(r"<a\b[^>]*href=\"([^\"]*)\"", page))) == ["/dashboard"]
    for region in ("c2-header", "c2-stream", "c2-rail", "c2-footer", "c2-hints"):
        assert f"id=\"{region}\"" in page
    assert "spec-kitty" not in page.lower()
    # nothing bus-derived is rendered server-side
    assert "alpha" not in page and "beta" not in page


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
        r"\.href\s*=|\.src\s*=|\.action\s*=|setAttribute\(\s*['\"](?:href|src|srcdoc|action|formaction)|"
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
    code = _strip_js_comments(_read(name))
    hit = re.search(JS_BANNED[what], code)
    assert hit is None, f"{name}: {what}: {hit.group(0)!r}"


def test_v2_js_only_fetches_fixed_api_paths() -> None:
    calls = re.findall(r"\bfetch\(\s*([^,)]*)", _strip_js_comments(_read("console2.js")))
    assert calls, "expected at least the /api/state fetch"
    for first_arg in calls:
        assert re.fullmatch(r"'/api/[a-z-]+'", first_arg.strip()), first_arg


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


def test_layout_matches_the_spec_grid() -> None:
    css = _strip_css_comments(_read("console2.css"))
    assert "grid-template-columns: minmax(0, 1fr) 340px" in css
    assert "grid-template-rows: 58px minmax(0, 1fr) 32px" in css
    assert "min-width: 1024px" in css


# --------------------------------------------------------------- node tests

@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("script", ["console2_model.test.mjs", "console2_render.test.mjs"])
def test_console2_node_tests(script: str) -> None:
    r = subprocess.run(
        ["node", str(REPO_ROOT / "tests" / script)],
        capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
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
