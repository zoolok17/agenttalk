"""v0.75.0 wrapped-agent runtime ergonomics: per-agent model/effort config,
argv injection, restart-safe runtime fingerprinting, and the fail-safe dashboard
projection. Each test maps to a §5 acceptance criterion of the design spec.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk import supervisor as sup
from agenttalk import web
from agenttalk.store import Store
from agenttalk.wrapper import run as wrapper_run
from agenttalk.wrapper import session as S

REPO_ROOT = Path(__file__).resolve().parents[1]
CONSOLE_JS = REPO_ROOT / "src" / "agenttalk" / "web_static" / "console.js"
RENDER_SMOKE = Path(__file__).resolve().parent / "console_runtime_smoke.mjs"


# ============================================================ config / resolver

def test_crit1_resolvers_per_agent_none_and_nondict_safe():
    """Crit 1: resolvers return per-agent -> None (no global fallback); a non-dict
    per-agent entry does NOT raise (D-13 isinstance coercion)."""
    # absent config -> None, no warning
    assert sup.resolve_model({}) == (None, None)
    assert sup.resolve_reasoning_effort({}, "codex") == (None, None)
    # present + valid
    assert sup.resolve_model({"model": "gpt-5-codex"}) == ("gpt-5-codex", None)
    assert sup.resolve_reasoning_effort({"reasoning_effort": "high"}, "codex") == ("high", None)
    # D-13: a corrupt/non-dict per-agent entry must not raise anywhere it is read
    for garbage in ("a-string", ["list"], 42, None, True):
        assert sup.resolve_model(garbage) == (None, None)
        assert sup.resolve_reasoning_effort(garbage, "claude") == (None, None)


def test_crit1_nondict_agent_entry_resolvers_none(tmp_path: Path):
    """Crit 1 (D-13): a hand-edited supervisor.json whose per-agent entry is a
    non-dict loads fine (valid JSON) and the resolvers coerce it to unset without
    raising — launch proceeds."""
    s = Store(tmp_path)
    s.init(["ag"])
    (s.dir / "supervisor.json").write_text(
        json.dumps({"agents": {"ag": "oops-a-string"}}), encoding="utf-8")
    cfg = cli._load_supervisor_config(s)  # valid JSON -> loads
    raw_entry = cfg["agents"]["ag"]
    assert not isinstance(raw_entry, dict)
    # D-13: the resolvers accept the raw non-dict entry and never raise
    assert sup.resolve_model(raw_entry) == (None, None)
    assert sup.resolve_reasoning_effort(raw_entry, "codex") == (None, None)


@pytest.mark.parametrize("cli_name,value", [
    ("codex", "max"),        # claude-ish value, NOT in codex's confirmed set -> drop
    ("claude", "minimal"),   # codex-only value on claude -> drop
    ("codex", "bogus"),      # typo -> drop
    ("claude", "XHIGH-typo"),
])
def test_crit2_invalid_effort_warn_and_drop(cli_name, value):
    """Crit 2: an unknown / wrong-CLI reasoning_effort is warned + dropped."""
    got, warn = sup.resolve_reasoning_effort({"reasoning_effort": value}, cli_name)
    assert got is None
    assert warn and "reasoning_effort" in warn


def test_crit2_effort_casefolded():
    """Case-folded compare: 'HIGH' resolves to 'high'."""
    assert sup.resolve_reasoning_effort({"reasoning_effort": "HIGH"}, "codex") == ("high", None)


@pytest.mark.parametrize("bad", ["", "   ", "-foo", "--model", 123, ["x"], None])
def test_crit2_invalid_model_warn_and_unset(bad):
    """Crit 2: empty / non-str / leading-dash model -> warn + unset (None absent)."""
    got, warn = sup.resolve_model({"model": bad})
    assert got is None
    if bad is None:
        assert warn is None  # absent, not invalid
    else:
        assert warn is not None


# ============================================================ injection / scan

def test_crit3_inject_bare_tokens_exact():
    """Crit 3: exact BARE tokens per CLI in the correct position."""
    argv, warns = cli.inject_model_flags(["codex", "-a", "never"], "codex", "gpt-5-codex", "high")
    assert warns == []
    assert argv == ["codex", "-a", "never", "-m", "gpt-5-codex",
                    "-c", "model_reasoning_effort=high"]
    # the codex effort token is bare (NO quotes)
    assert "model_reasoning_effort=high" in argv
    assert 'model_reasoning_effort="high"' not in argv

    argv, warns = cli.inject_model_flags(["claude"], "claude", "opus", "high")
    assert warns == []
    assert argv == ["claude", "--model", "opus", "--effort", "high"]


def test_crit3_inject_only_set_halves():
    """Only the halves that are set are injected."""
    argv, _ = cli.inject_model_flags(["codex"], "codex", "gpt5", None)
    assert argv == ["codex", "-m", "gpt5"]
    argv, _ = cli.inject_model_flags(["codex"], "codex", None, "low")
    assert argv == ["codex", "-c", "model_reasoning_effort=low"]


def test_crit3_inject_idempotent():
    """Pure + idempotent: a second call with the value present no-ops (no double add)."""
    once, _ = cli.inject_model_flags(["codex"], "codex", "gpt5", "high")
    twice, warns = cli.inject_model_flags(once, "codex", "gpt5", "high")
    assert twice == once  # no duplicate tokens
    assert any("already set" in w for w in warns)


@pytest.mark.parametrize("cli_name,tail,which", [
    ("codex", ["codex", "-m", "X"], "model"),
    ("codex", ["codex", "--model", "X"], "model"),
    ("codex", ["codex", "--model=X"], "model"),
    ("codex", ["codex", "-c", "model=X"], "model"),
    ("codex", ["codex", "-c", 'model="X"'], "model"),
    ("codex", ["codex", "--config", "model=X"], "model"),
    ("codex", ["codex", "-c", "model_reasoning_effort=low"], "effort"),
    ("claude", ["claude", "--model", "X"], "model"),
    ("claude", ["claude", "--model=X"], "model"),
    ("claude", ["claude", "--effort", "low"], "effort"),
    ("claude", ["claude", "--effort=low"], "effort"),
])
def test_crit4_operator_tail_conflict_all_forms(cli_name, tail, which):
    """Crit 4: an operator-tail flag (EVERY form) -> no-op + warn; the tail value
    wins (the scanned effective value is the tail's, never the injected one)."""
    argv, warns = cli.inject_model_flags(tail, cli_name, "INJECTED-M", "high")
    assert any("already set" in w for w in warns), warns
    scanned = cli.scan_model_effort(argv, cli_name)
    if which == "model":
        assert scanned["model"] == "X"           # the tail's model wins
        assert scanned["model"] != "INJECTED-M"
    else:
        assert scanned["effort"] == "low"        # the tail's effort wins
        assert scanned["effort"] != "high"


def test_crit5_precedence_tail_over_flag_over_config_over_none():
    """Crit 5: raw tail (no-op+warn) > wrap --model/--effort > per-agent config > none."""
    # none: no config, no flag, no tail
    m, e, warns = cli._resolve_runtime_model_effort({}, "codex", None, None)
    assert (m, e) == (None, None)
    argv, _ = cli.inject_model_flags(["codex"], "codex", m, e)
    assert cli.scan_model_effort(argv, "codex") == {"model": None, "effort": None}

    # config only
    cfg = {"model": "config-model", "reasoning_effort": "low"}
    m, e, _ = cli._resolve_runtime_model_effort(cfg, "codex", None, None)
    assert (m, e) == ("config-model", "low")

    # flag OVERRIDES config
    m, e, _ = cli._resolve_runtime_model_effort(cfg, "codex", "flag-model", "high")
    assert (m, e) == ("flag-model", "high")

    # raw tail OVERRIDES flag+config (effective scan wins)
    argv, warns = cli.inject_model_flags(["codex", "-m", "tail-model"], "codex", "flag-model", "high")
    assert cli.scan_model_effort(argv, "codex")["model"] == "tail-model"
    assert any("already set" in w for w in warns)


@pytest.mark.parametrize("model,effort,bad_token", [
    ("gpt5", 'hi"gh', 'model_reasoning_effort=hi"gh'),  # malformed effort dropped
    ("--foo", "high", "--foo"),                          # leading-dash model dropped
    ("-a", "high", "-a"),                                # dash-shaped model dropped
    ("", "high", ""),                                    # empty model dropped
])
def test_crit6_d7_self_validation_drops_bad_tokens(model, effort, bad_token):
    """Crit 6 (D7): inject_model_flags self-validates and never emits a malformed /
    dash-shaped token, even when a caller skipped validation."""
    argv, warns = cli.inject_model_flags(["codex"], "codex", model, effort)
    assert warns, "a dropped value must warn"
    assert bad_token not in argv
    # the bad half is dropped, never emitted as a malformed / dash-shaped token
    scanned = cli.scan_model_effort(argv, "codex")
    if bad_token.startswith("model_reasoning_effort="):
        assert scanned["effort"] is None        # malformed effort dropped
    else:
        assert scanned["model"] is None         # dash-shaped / empty model dropped


def test_scan_recognizes_all_codex_and_claude_forms():
    assert cli.scan_model_effort(["c", "-m", "o3", "exec"], "codex")["model"] == "o3"
    assert cli.scan_model_effort(["c", "--model=o3"], "codex")["model"] == "o3"
    assert cli.scan_model_effort(["c", "-c", 'model="o3"'], "codex")["model"] == "o3"
    assert cli.scan_model_effort(["c", "--config", "model=o3"], "codex")["model"] == "o3"
    got = cli.scan_model_effort(["c", "-c", "model_reasoning_effort=high"], "codex")
    assert got["effort"] == "high"
    assert cli.scan_model_effort(["c", "--effort=low"], "claude")["effort"] == "low"


# ============================================================ integration argv

def _codex_state(**kw):
    return S.SessionState(cli="codex", **kw)


def test_crit7_end_to_end_codex_argv_fresh_and_resume():
    """Crit 7: per-turn child argv = base(injected) + build_turn args, with -m/-c
    strictly BEFORE exec, for both fresh and resume."""
    base = ["codex", "-a", "never", "-s", "workspace-write"]
    base, _ = cli.inject_model_flags(base, "codex", "gpt-5-codex", "high")

    fresh = base + S.build_turn(_codex_state(), "hi").args
    assert fresh == ["codex", "-a", "never", "-s", "workspace-write",
                     "-m", "gpt-5-codex", "-c", "model_reasoning_effort=high",
                     "exec", "--json"]
    assert fresh.index("-m") < fresh.index("exec")
    assert fresh.index("-c") < fresh.index("exec")

    resumed = base + S.build_turn(
        _codex_state(codex_thread_id="TID", resume_available=True, turns=2), "hi").args
    assert resumed == ["codex", "-a", "never", "-s", "workspace-write",
                       "-m", "gpt-5-codex", "-c", "model_reasoning_effort=high",
                       "exec", "resume", "--json", "TID"]
    assert resumed.index("-c") < resumed.index("exec")


def test_crit7_end_to_end_claude_argv_fresh_and_resume():
    base = ["claude"]
    base, _ = cli.inject_model_flags(base, "claude", "opus", "high")

    fresh = base + S.build_turn(
        S.SessionState(cli="claude", claude_session_id="SID", turns=0), "hi").args
    assert fresh == ["claude", "--model", "opus", "--effort", "high",
                     "-p", "--output-format", "stream-json", "--verbose",
                     "--include-partial-messages", "--session-id", "SID"]

    resumed = base + S.build_turn(
        S.SessionState(cli="claude", claude_session_id="SID", turns=3), "hi").args
    assert resumed[:5] == ["claude", "--model", "opus", "--effort", "high"]
    assert resumed[-2:] == ["--resume", "SID"]
    assert resumed.index("--model") < resumed.index("-p")


# ============================================================ fingerprint

def test_crit8_first_launch_stamps_no_reason(tmp_path: Path):
    """Crit 8: first-ever launch (no state file) -> adopt+stamp, no continuity loss;
    an unchanged relaunch then resumes."""
    st = S.SessionState(cli="claude", claude_session_id="SID")
    action = S.reconcile_runtime_fingerprint(st, "opus|high")
    assert action == "adopt"
    assert st.runtime_fingerprint == "opus|high"
    assert st.continuity_lost_reason == ""
    assert st.resume_available is True
    # relaunch unchanged -> resume
    assert S.reconcile_runtime_fingerprint(st, "opus|high") == "unchanged"
    assert st.claude_session_id == "SID"


def test_crit9_upgrade_adopts_preserves_session(tmp_path: Path):
    """Crit 9 (D5): a pre-v0.75.0 file (turns>0, valid id, NO fingerprint) is
    adopted-and-stamped: session PRESERVED, no reset, no reason."""
    s = Store(tmp_path)
    s.init(["ag"])
    (s.state_dir / "ag.wrapper-session.json").write_text(json.dumps({
        "cli": "claude", "claude_session_id": "OLD-SID", "resume_available": True,
        "turns": 7}), encoding="utf-8")
    st = S.load_session(s, "ag", "claude")
    assert st.runtime_fingerprint is None  # old file lacks it
    action = S.reconcile_runtime_fingerprint(st, "opus|high")
    assert action == "adopt"
    assert st.claude_session_id == "OLD-SID"    # preserved
    assert st.resume_available is True
    assert st.continuity_lost_reason == ""


def test_crit10_unchanged_relaunch_resumes(tmp_path: Path):
    st = S.SessionState(cli="codex", codex_thread_id="TID", resume_available=True,
                        turns=4, runtime_fingerprint="gpt5|high")
    assert S.reconcile_runtime_fingerprint(st, "gpt5|high") == "unchanged"
    assert st.codex_thread_id == "TID"
    assert st.resume_available is True
    assert st.continuity_lost_reason == ""


def test_crit11_change_model_or_effort_forces_fresh():
    # claude: new uuid + reason
    st = S.SessionState(cli="claude", claude_session_id="OLD", resume_available=True,
                        turns=3, runtime_fingerprint="opus|high")
    assert S.reconcile_runtime_fingerprint(st, "sonnet|high") == "reset"
    assert st.claude_session_id != "OLD"
    assert st.continuity_lost_reason == "runtime_config_changed"
    assert st.resume_available is False
    assert st.runtime_fingerprint == "sonnet|high"

    # codex: cleared thread_id + reason (effort change)
    st = S.SessionState(cli="codex", codex_thread_id="TID", resume_available=True,
                        turns=3, runtime_fingerprint="gpt5|high")
    assert S.reconcile_runtime_fingerprint(st, "gpt5|low") == "reset"
    assert st.codex_thread_id is None
    assert st.resume_available is False
    assert st.continuity_lost_reason == "runtime_config_changed"


def test_crit12_effective_model_drift_via_tail():
    """Crit 12: fingerprint tracks the EFFECTIVE (scanned) model, so changing the
    launch-tail model (config unchanged) forces fresh."""
    # config model=opus, tail --model sonnet -> effective sonnet
    argv, _ = cli.inject_model_flags(["claude", "--model", "sonnet"], "claude", "opus", "high")
    eff = cli.scan_model_effort(argv, "claude")
    fp1 = S.compute_runtime_fingerprint(eff["model"], eff["effort"])
    assert eff["model"] == "sonnet"
    st = S.SessionState(cli="claude", claude_session_id="SID", resume_available=True,
                        turns=2, runtime_fingerprint=fp1)
    # now tail changes to --model haiku (config still opus)
    argv2, _ = cli.inject_model_flags(["claude", "--model", "haiku"], "claude", "opus", "high")
    eff2 = cli.scan_model_effort(argv2, "claude")
    fp2 = S.compute_runtime_fingerprint(eff2["model"], eff2["effort"])
    assert eff2["model"] == "haiku"
    assert S.reconcile_runtime_fingerprint(st, fp2) == "reset"
    assert st.continuity_lost_reason == "runtime_config_changed"


def test_crit13_benign_edit_no_reset():
    """Crit 13: an edit that does NOT change the effective model/effort -> no reset."""
    st = S.SessionState(cli="codex", codex_thread_id="TID", resume_available=True,
                        turns=5, runtime_fingerprint="gpt5|high")
    # same effective fingerprint (e.g. an unrelated tail flag added)
    assert S.reconcile_runtime_fingerprint(st, "gpt5|high") == "unchanged"
    assert st.codex_thread_id == "TID"


def test_crit13_stamp_durability_reset_persists_before_make_drive(tmp_path, monkeypatch):
    """Crit 13: the reconciled fingerprint is persisted BEFORE make_drive, so a
    make_drive ValueError (early-return) resets AT MOST ONCE across relaunches."""
    s = Store(tmp_path)
    s.init(["ag"])
    # seed an OLD fingerprint with a live claude session
    (s.state_dir / "ag.wrapper-session.json").write_text(json.dumps({
        "cli": "claude", "claude_session_id": "OLD-SID", "resume_available": True,
        "turns": 4, "runtime_fingerprint": "opus|high"}), encoding="utf-8")

    def _boom(*a, **k):
        raise ValueError("make_drive blew up")

    monkeypatch.setattr(wrapper_run, "make_drive", _boom)

    # relaunch #1 with a NEW fingerprint -> reconcile resets, persists, then make_drive raises
    rc = cli._wrap_loop_mode(s, "ag", cli="claude", base_argv=["claude"], sender="ag",
                             min_interval=0.0, render=False, runtime_model="sonnet",
                             runtime_effort="high", runtime_fingerprint="sonnet|high")
    assert rc == 2
    after1 = json.loads((s.state_dir / "ag.wrapper-session.json").read_text(encoding="utf-8"))
    assert after1["runtime_fingerprint"] == "sonnet|high"  # stamp survived the raise
    assert after1["continuity_lost_reason"] == "runtime_config_changed"
    new_sid = after1["claude_session_id"]
    assert new_sid != "OLD-SID"  # reset minted a fresh id

    # relaunch #2 with the SAME NEW fingerprint -> unchanged, NO second reset
    rc = cli._wrap_loop_mode(s, "ag", cli="claude", base_argv=["claude"], sender="ag",
                             min_interval=0.0, render=False, runtime_model="sonnet",
                             runtime_effort="high", runtime_fingerprint="sonnet|high")
    assert rc == 2
    after2 = json.loads((s.state_dir / "ag.wrapper-session.json").read_text(encoding="utf-8"))
    assert after2["claude_session_id"] == new_sid  # NOT reset again


def test_session_state_roundtrips_new_fields(tmp_path):
    s = Store(tmp_path)
    s.init(["ag"])
    st = S.SessionState(cli="codex", model="gpt5", reasoning_effort="high",
                        runtime_fingerprint="gpt5|high")
    S.save_session(s, "ag", st)
    back = S.load_session(s, "ag", "codex")
    assert (back.model, back.reasoning_effort, back.runtime_fingerprint) == (
        "gpt5", "high", "gpt5|high")


# ============================================================ dashboard / redaction

def _build_root(store):
    return web.build_state([web.RootDescriptor(store=store, label="root")])["roots"][0]


def test_crit14_negative_redaction_ids_never_on_wire(tmp_path):
    """Crit 14 (hardened): the session ids AND the runtime_fingerprint value NEVER
    appear anywhere in the FULL /api/state result (not just roots[0]) — neither the
    seeded values nor their key names."""
    s = Store(tmp_path)
    s.init(["codexer"])
    (s.state_dir / "codexer.wrapper-session.json").write_text(json.dumps({
        "cli": "codex", "codex_thread_id": "THREAD-SECRET-123",
        "claude_session_id": "CLAUDE-SECRET-XYZ", "resume_available": True,
        "turns": 3, "model": "gpt5", "reasoning_effort": "high",
        "runtime_fingerprint": "gpt5|high"}), encoding="utf-8")
    # serialize the ENTIRE build_state result, not just one root
    blob = json.dumps(web.build_state([web.RootDescriptor(store=s, label="root")]))
    for forbidden in ("THREAD-SECRET-123", "CLAUDE-SECRET-XYZ",
                      "codex_thread_id", "claude_session_id",
                      "runtime_fingerprint", "gpt5|high"):
        assert forbidden not in blob, f"{forbidden!r} leaked into /api/state"
    # the allow-listed model/effort DO still surface (sanity: redaction didn't drop everything)
    assert "gpt5" in blob and "high" in blob


def test_crit15_agent_entries_expose_when_known(tmp_path):
    s = Store(tmp_path)
    s.init(["ag"])
    (s.state_dir / "ag.wrapper-session.json").write_text(json.dumps({
        "cli": "codex", "codex_thread_id": "TID", "resume_available": True,
        "turns": 6, "model": "gpt-5-codex", "reasoning_effort": "high"}),
        encoding="utf-8")
    by_name = {a["name"]: a for a in _build_root(s)["agents"]}
    e = by_name["ag"]
    assert e["model"] == "gpt-5-codex"
    assert e["reasoning_effort"] == "high"
    assert e["runtime"] == {"state": "resumed"}


def test_crit15_corrupt_session_fields_absent_no_crash(tmp_path):
    s = Store(tmp_path)
    s.init(["ag"])
    (s.state_dir / "ag.wrapper-session.json").write_text("{ broken", encoding="utf-8")
    by_name = {a["name"]: a for a in _build_root(s)["agents"]}
    e = by_name["ag"]
    assert "model" not in e and "reasoning_effort" not in e and "runtime" not in e


def test_crit15_no_session_file_fields_absent(tmp_path):
    s = Store(tmp_path)
    s.init(["ag"])
    by_name = {a["name"]: a for a in _build_root(s)["agents"]}
    assert "runtime" not in by_name["ag"]


def test_crit16_reset_reason_maps_to_closed_set(tmp_path):
    """Crit 16: multi-word continuity reason maps to a closed token; free-text is
    omitted; runtime_config_changed survives."""
    s = Store(tmp_path)
    s.init(["ag"])
    path = s.state_dir / "ag.wrapper-session.json"

    def _reason(raw):
        path.write_text(json.dumps({"cli": "claude", "continuity_lost_reason": raw}),
                        encoding="utf-8")
        rt = s.read_wrapper_runtime("ag")
        return rt.get("reset_reason")

    assert _reason("resume_unavailable after 2 session failures") == "resume_unavailable"
    assert _reason("runtime_config_changed") == "runtime_config_changed"
    assert _reason("stream disconnected") is None            # free-text -> omitted
    assert _reason("totally made up reason") is None
    assert _reason("") is None


def test_crit16_reset_reason_surfaces_in_web(tmp_path):
    s = Store(tmp_path)
    s.init(["ag"])
    (s.state_dir / "ag.wrapper-session.json").write_text(json.dumps({
        "cli": "claude", "claude_session_id": "SID", "turns": 0, "resume_available": True,
        "continuity_lost_reason": "runtime_config_changed",
        "model": "opus", "reasoning_effort": "high"}), encoding="utf-8")
    e = {a["name"]: a for a in _build_root(s)["agents"]}["ag"]
    assert e["runtime"] == {"state": "fresh", "reset_reason": "runtime_config_changed"}


# ============================================================ crit 17 render / JS

@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_crit17_node_check_console_js():
    """Crit 17/20: console.js passes node --check (syntax)."""
    r = subprocess.run(["node", "--check", str(CONSOLE_JS)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_crit17_render_smoke_xss_safe_and_rows():
    """Crit 17: executable render smoke — Model/Effort/Runtime rows show values and
    '—' when unset, and an XSS payload renders inert (textContent, never innerHTML)."""
    r = subprocess.run(["node", str(RENDER_SMOKE)], capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)
    assert "PASS" in r.stdout


def test_crit17_render_pins_textcontent_primitive():
    """Crit 17: pin the primitive — el() renders via textContent (never a raw-HTML
    sink), and the Supervisor card routes runtime values through supRuntimeRows."""
    src = CONSOLE_JS.read_text(encoding="utf-8")
    assert "n.textContent = String(text)" in src
    assert ".innerHTML" not in src  # the console builds every node via textContent
    assert "function supRuntimeRows(a)" in src
    assert "supRuntimeRows(a)" in src  # wired into the Supervisor card


# ============================================================ fold: seam + cadence

def _codex_turn_lines():
    return [json.dumps(o) for o in [
        {"type": "thread.started", "thread_id": "T"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
        {"type": "turn.completed"},
    ]]


def _claude_turn_lines():
    return [json.dumps(o) for o in [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "stream_event",
         "event": {"type": "message_start", "message": {"role": "assistant"}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "OK"},
    ]]


def test_fold_cmd_wrap_seam_fingerprint_from_post_injection_argv(tmp_path, monkeypatch):
    """FOLD item 1 (P2): drive the REAL cmd_wrap resolve->inject->scan->fingerprint
    ->_wrap_loop_mode seam. With per-agent config model + a `wrap --effort` flag + an
    operator tail `--model`, assert the model/effort/fingerprint cmd_wrap hands
    downstream are derived from the POST-injection argv: tail model WINS over config,
    flag effort WINS over config (tail > flag > config)."""
    s = Store(tmp_path)
    s.init(["ag"])
    (s.dir / "supervisor.json").write_text(json.dumps({"agents": {"ag": {
        "cli": "claude", "model": "config-model", "reasoning_effort": "low"}}}),
        encoding="utf-8")

    captured: dict = {}

    def fake_loop(store, agent, **kw):
        captured.update(kw)
        captured["agent"] = agent
        return 0

    monkeypatch.setattr(cli, "_wrap_loop_mode", fake_loop)
    monkeypatch.setattr(cli, "_get_store", lambda args, **k: s)
    # pass-through preflight (no real binary resolution / launcher-nonce in the unit test)
    monkeypatch.setattr(
        wrapper_run, "preflight_launch_runtime",
        lambda argv, cli_, cwd, env: types.SimpleNamespace(blocked=None, argv=list(argv)))

    args = argparse.Namespace(
        agent="ag", cli="claude", sender=None, lane_id=None, min_interval=5.0,
        no_render=True, model=None, effort="high", loop=True, lead_loop=False,
        one_shot=False, to_request=None, dead_letter_max_attempts=None,
        dead_letter_escalate_after=None,
        cmd=["--", "claude", "--model", "tail-model"])

    rc = cli.cmd_wrap(args)
    assert rc == 0
    # tail model wins over config-model; flag effort wins over config low
    assert captured["runtime_model"] == "tail-model"
    assert captured["runtime_effort"] == "high"
    assert captured["runtime_fingerprint"] == S.compute_runtime_fingerprint(
        "tail-model", "high")
    # and the fingerprint reflects the FINAL child argv actually passed downstream
    base = captured["base_argv"]
    assert cli.scan_model_effort(base, "claude") == {"model": "tail-model", "effort": "high"}


@pytest.mark.parametrize("cli_name", ["codex", "claude"])
def test_fold_crit7_injected_tokens_reach_spawned_argv_both_paths(tmp_path, cli_name):
    """FOLD item 1 (P2) / spec crit7 cadence: the injected model/effort tokens reach
    the ACTUAL spawned argv on BOTH the message path (make_drive, run.py:1664) and the
    cadence path (make_cadence_drive, run.py:2016), in the correct position."""
    s = Store(tmp_path)
    s.init(["ag"])
    base = [cli_name]
    base, _ = cli.inject_model_flags(base, cli_name, "the-model", "high")

    def _drive_once(factory):
        spawned: list[list[str]] = []

        def fake_spawn(argv, _second):
            spawned.append(list(argv))
            return _codex_turn_lines() if cli_name == "codex" else _claude_turn_lines()

        st = S.SessionState(cli=cli_name)
        if cli_name == "claude":
            st.claude_session_id = "SID"
        drv = factory(s, "ag", cli_name, st, base, spawn=fake_spawn,
                      clock=lambda: 0.0, render=False)
        if factory is wrapper_run.make_cadence_drive:
            drv({"agent": "ag"}, [{"type": "dead_letter"}])
        else:
            drv({"id": "m1", "from": "x", "to": "ag", "kind": "question",
                 "subject": "s", "body": "b", "correlation_id": "q", "request_id": "q",
                 "broadcast_id": None, "meta": {"request_id": "q"}})
        assert spawned, "spawn was invoked"
        return spawned[0]

    for factory in (wrapper_run.make_drive, wrapper_run.make_cadence_drive):
        argv = _drive_once(factory)
        if cli_name == "codex":
            assert "-m" in argv and "the-model" in argv
            assert "model_reasoning_effort=high" in argv
            assert argv.index("-m") < argv.index("exec")
            assert argv.index("-c") < argv.index("exec")
        else:
            assert "--model" in argv and "the-model" in argv
            assert "--effort" in argv and "high" in argv
            assert argv.index("--model") < argv.index("-p")
            assert argv.index("--effort") < argv.index("-p")


def test_fold_crit1_resolvers_never_raise_on_none_or_nondict():
    """FOLD item 3 (P3): defense-in-depth for the load-failure path — the resolvers
    return unset WITHOUT raising when handed None or any non-dict config."""
    for bad in (None, "a-string", ["l"], 42, True, ("t",)):
        assert sup.resolve_model(bad) == (None, None)
        assert sup.resolve_reasoning_effort(bad, "codex") == (None, None)
        assert sup.resolve_reasoning_effort(bad, "claude") == (None, None)


def test_fold_crit4_warns_when_model_effort_without_loop(tmp_path, monkeypatch, capsys):
    """FOLD item 4 (P3): `wrap --model/--effort` WITHOUT `--loop` emits a one-line
    stderr warning (not silently dropped) and does not enter the loop path."""
    s = Store(tmp_path)
    s.init(["ag"])
    monkeypatch.setattr(cli, "_get_store", lambda args, **k: s)
    # stub the non-loop runner so we exercise only the warning + branch
    monkeypatch.setattr(wrapper_run, "run_wrapper", lambda **k: 0)
    monkeypatch.setattr(
        wrapper_run, "preflight_launch_runtime",
        lambda argv, cli_, cwd, env: types.SimpleNamespace(blocked=None, argv=list(argv)))

    args = argparse.Namespace(
        agent="ag", cli="codex", sender=None, lane_id=None, min_interval=5.0,
        no_render=True, model="gpt5", effort="high", loop=False, lead_loop=False,
        one_shot=False, to_request=None, dead_letter_max_attempts=None,
        dead_letter_escalate_after=None, cmd=["--", "codex"])

    rc = cli.cmd_wrap(args)
    assert rc == 0
    err = capsys.readouterr().err
    assert "--model/--effort only apply with --loop; ignoring" in err
