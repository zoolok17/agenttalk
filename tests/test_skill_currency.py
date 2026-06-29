"""Tests for the Tier-0 skill-currency lint (skill_currency.py + doctor wiring).

Covers: parser-inventory extraction (incl. nested subcommands), the conservative CLI-token
scanner on good/bad snippets, the wrapper ``--`` passthrough regression, the reviewed-against
ratchet, frontmatter well-formedness, and the SOURCE-TREE all-bundled-skills-pass gate (the
atomic CI guard - every bundled skill must pass after the Tier-0a migration + refresh).
"""
from __future__ import annotations

from pathlib import Path

from agenttalk import __version__
from agenttalk import doctor
from agenttalk import install_skills as iskl
from agenttalk import skill_currency as sc


# ----------------------------------------------------------- parser inventory

def test_inventory_extracts_nested_subcommands_and_globals() -> None:
    inv = sc.build_command_inventory()
    # top-level subcommands present
    assert {"send", "wait", "relay", "roster", "wrap", "close", "lane"} <= set(inv.subcommands)
    # TWO-LEVEL subcommands resolve (the case the validator must descend into)
    assert {"operator-answer", "operator-command"} <= set(inv.subcommands["relay"].subcommands)
    assert "add" in inv.subcommands["roster"].subcommands
    assert "open" in inv.subcommands["close"].subcommands
    # global flags live on the root node
    assert "--root" in inv.flags and "--version" in inv.flags


# ----------------------------------------------------------- CLI-token validation

def test_validate_good_commands_clean() -> None:
    inv = sc.build_command_inventory()
    good = [
        "send --to x -m y",
        "relay operator-answer --to-request q-1 -m z",
        "relay operator-command --to beta -m do-it",
        "roster add n --unique",
        "close open --id c1 --scope release --revision HEAD",
        "--root /tmp/x sync --for me",          # global flag before subcommand
    ]
    for cmd in good:
        assert sc._validate_command(cmd, inv) == [], f"false positive on: {cmd}"


def test_validate_bad_commands_flagged() -> None:
    inv = sc.build_command_inventory()
    assert sc._validate_command("frobnicate", inv)                 # unknown subcommand
    assert sc._validate_command("send --no-such-flag x", inv)      # unknown flag
    assert sc._validate_command("relay bogus-subcommand", inv)     # unknown NESTED subcommand


def test_wrapper_passthrough_after_dashdash_ignored() -> None:
    inv = sc.build_command_inventory()
    # everything after a literal `--` is the wrapped CLI's argv - must NOT be validated
    assert sc._validate_command("wrap --loop -- python -m weird --made-up-flag --another",
                                inv) == []


def test_placeholders_and_comments_do_not_false_positive() -> None:
    inv = sc.build_command_inventory()
    assert sc._validate_command("send --to <agent> -m TEXT", inv) == []
    assert sc._validate_command("roster   # see who is on the team", inv) == []


# ----------------------------------------------------------- command-span scoping

def test_command_spans_only_scan_code_not_prose_or_slash_commands() -> None:
    text = (
        "Plain prose that says agenttalk send should NOT be scanned.\n"
        "See the `/agenttalk.lead` skill (a slash command, not the CLI).\n"
        "Run `agenttalk roster` to inspect.\n"
        "```\nagenttalk send --to x -m y\n```\n"
    )
    spans, _ = sc._command_spans(text)
    tails = [t for _, t in spans]
    assert "roster" in tails                       # inline-backtick CLI command picked up
    assert any(t.startswith("send") for t in tails)  # fenced command picked up
    # prose mention + slash-command reference are NOT picked up
    assert not any("should NOT" in t for t in tails)
    assert all(".lead" not in t for t in tails)


def test_ignore_comment_suppresses_next_line() -> None:
    text = "<!-- agenttalk-skill-lint: ignore-next -->\n`agenttalk frobnicate --bogus`\n"
    assert sc._command_spans(text) == ([], [])


# ----------------------------------------------------------- reviewed-against ratchet

def test_reviewed_against_parsing_and_ratchet() -> None:
    assert sc.parse_reviewed_against('"0.42"') == (0, 42)
    assert sc.parse_reviewed_against("v0.40") == (0, 40)
    assert sc.parse_reviewed_against("0.42.1") == (0, 42)     # patch ignored
    assert sc.parse_reviewed_against("garbage") is None
    cur = sc.current_major_minor("0.42.0")
    assert (0, 40) < cur                                       # older minor -> stale
    assert not ((0, 42) < cur)                                # same minor -> not stale
    assert not (sc.parse_reviewed_against("0.42.1") < cur)     # patch-lag -> not stale


def test_frontmatter_checks_flag_missing_fields(tmp_path: Path) -> None:
    inv = sc.build_command_inventory()
    cur = sc.current_major_minor(__version__)
    # a devkit SKILL.md missing category / evidence-profile / reviewed-against
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: x\ndescription: d\n---\n# x\n", encoding="utf-8")
    findings = sc.check_skill_file(p, kind="devkit", inventory=inv, current=cur)
    tokens = {f.token for f in findings}
    assert {"reviewed-against", "category", "evidence-profile"} <= tokens
    # a stale stamp is flagged
    p.write_text('---\nname: x\ndescription: d\ncategory: production\n'
                 'evidence-profile:\n  - production-handoff\nreviewed-against: "0.1"\n---\n',
                 encoding="utf-8")
    findings = sc.check_skill_file(p, kind="devkit", inventory=inv, current=cur)
    assert any("lags package" in f.reason for f in findings)


# ----------------------------------------------------------- the atomic source-tree gate

def test_all_bundled_skills_pass_currency() -> None:
    # THE atomic gate: after the Tier-0a frontmatter migration + v0.42.0 refresh, every
    # bundled skill must pass the currency lint (valid frontmatter + stamps + CLI tokens).
    findings = sc.check_bundled_skills(iskl.SKILLS_ROOT, __version__)
    assert findings == [], "stale bundled skills: " + "; ".join(
        f"{f.file}:{f.line} {f.reason}" for f in findings)


def test_doctor_skill_currency_passes_on_refreshed_source() -> None:
    c = doctor._check_skill_currency()
    assert c.name == "skill_currency" and c.status == "ok"


def test_doctor_skill_currency_is_warn_only_never_errors(monkeypatch) -> None:
    # PIN the safety property (reviewer-1 P2): the check must degrade to WARN, never error
    # or crash, in BOTH the lint-raises and lint-returns-findings paths - so it can never
    # brick the bus. (The previous assertion was tautological after asserting ok.)
    def boom(*a, **k):
        raise RuntimeError("lint blew up")

    monkeypatch.setattr(sc, "check_bundled_skills", boom)
    assert doctor._check_skill_currency().status == "warn"            # raise -> warn

    monkeypatch.setattr(sc, "check_bundled_skills",
                        lambda *a, **k: [sc.Finding("x.md", 1, "tok", "stale token")])
    c = doctor._check_skill_currency()
    assert c.status == "warn" and c.data and c.data["findings"]       # findings -> warn


# ----------------------------------------------------------- fold regressions (3-reviewer)

def test_continuation_line_stale_token_is_caught() -> None:
    # P1 (corroborated): a stale flag/subcommand on a SHELL CONTINUATION line - in BOTH bash
    # backslash and PowerShell backtick forms - must be validated, not silently passed.
    inv = sc.build_command_inventory()
    cases = [
        "```\nagenttalk send \\\n  --bogus-stale-flag value\n```\n",   # bash, stale flag
        "```\nagenttalk relay \\\n  bogus-subcommand\n```\n",          # bash, stale subcommand
        "```\nagenttalk send `\n  --bogus-stale-flag value\n```\n",    # PowerShell, stale flag
        "```\nagenttalk relay `\n  bogus-subcommand\n```\n",           # PowerShell, stale sub
    ]
    for text in cases:
        spans, _ = sc._command_spans(text)
        assert spans and any(sc._validate_command(t, inv) for _, t in spans), text
    # the reported line is the FIRST physical line of the joined command
    spans0, _ = sc._command_spans(cases[0])
    assert spans0[0][0] == 2


def test_valid_multiline_continuation_is_clean() -> None:
    inv = sc.build_command_inventory()
    good = "```\nagenttalk send \\\n  --to x \\\n  -m y\n```\n"
    spans, dangling = sc._command_spans(good)
    assert not dangling                          # last line has no trailing continuation char
    assert all(not sc._validate_command(t, inv) for _, t in spans)


# -------------------------------------- fence-boundary fold (delta re-review, both reviewers)

def test_dangling_continuation_at_fence_boundary_does_not_hide_later_command() -> None:
    # A dangling continuation right before a closing fence must NOT consume the delimiter and
    # cascade past it - a LATER fenced command must still be scanned (the bug the continuation
    # fix introduced). Verified for both ``` and ~~~ closers.
    inv = sc.build_command_inventory()
    for fence in ("```", "~~~"):
        text = (f"{fence}\nagenttalk send \\\n{fence}\n\n"
                f"{fence}\nagenttalk frobnicate\n{fence}\n")
        spans, dangling = sc._command_spans(text)
        assert dangling, f"dangling not reported ({fence})"
        tails = [t for _, t in spans]
        assert any("frobnicate" in t for t in tails), f"later command hidden ({fence})"
        assert any(sc._validate_command(t, inv) for t in tails)  # the later stale cmd is caught


def test_dangling_continuation_at_eof_is_handled() -> None:
    spans, dangling = sc._command_spans("```\nagenttalk send \\\n")   # dangling at EOF, no crash
    assert dangling


def test_dangling_continuation_reported_as_finding(tmp_path: Path) -> None:
    inv = sc.build_command_inventory()
    cur = sc.current_major_minor(__version__)
    p = tmp_path / "SKILL.md"
    p.write_text('---\nname: x\ndescription: d\ncategory: production\n'
                 'evidence-profile:\n  - production-handoff\nreviewed-against: "0.42"\n---\n'
                 "# body\n\n```\nagenttalk send \\\n```\n", encoding="utf-8")
    findings = sc.check_skill_file(p, kind="devkit", inventory=inv, current=cur)
    assert any("dangling line-continuation" in f.reason for f in findings)


def test_unterminated_frontmatter_is_flagged(tmp_path: Path) -> None:
    # codex MAJOR: opening --- + all required keys but NO closing --- must be flagged.
    inv = sc.build_command_inventory()
    p = tmp_path / "SKILL.md"
    p.write_text('---\nname: x\ndescription: d\ncategory: production\n'
                 'evidence-profile:\n  - production-handoff\nreviewed-against: "0.42"\n'
                 "# body, frontmatter never closed\n", encoding="utf-8")
    findings = sc.check_skill_file(p, kind="devkit", inventory=inv, current=(0, 42))
    assert any("unterminated" in f.reason for f in findings)


def test_undecodable_file_degrades_to_finding(tmp_path: Path) -> None:
    # P2: a non-UTF-8 file raises UnicodeDecodeError (a ValueError) - it must degrade to a
    # Finding, not propagate through check_bundled_skills and red the CI test.
    inv = sc.build_command_inventory()
    p = tmp_path / "SKILL.md"
    p.write_bytes(b"\xff\xfe not valid utf-8 \x00\x80")
    findings = sc.check_skill_file(p, kind="devkit", inventory=inv, current=(0, 42))  # no raise
    assert any("unreadable" in f.reason for f in findings)


def test_stale_flag_after_value_flag_is_caught() -> None:
    # P3: a value-taking flag must not swallow a following stale flag as its value.
    inv = sc.build_command_inventory()
    assert sc._validate_command("send --to --no-such-flag x", inv)
