"""Skills run agenttalk through the seat's pinned runtime when there is one (B4-1).

The rule, stated once per skill family: run agenttalk commands with `"$AGENTTALK_PY" -m agenttalk` when
`AGENTTALK_PY` is set (the seat's pinned runtime); otherwise use the form the project's agent manual gives
(`python -m agenttalk` under Codex, `agenttalk` elsewhere).

What this does and does not promise: the skills SAY to use the pinned runtime when there is one. A command form
cannot make an agenttalk any newer, so nothing here claims the same version or protects against an older install.

The lint (`skill_currency`) keeps it that way: a plain `python -m agenttalk` is flagged in a Claude skill, and in a
Codex or devkit skill unless that skill states the rule (it names `AGENTTALK_PY`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agenttalk import __version__
from agenttalk import install_skills as iskl
from agenttalk import skill_currency as sc

ROOT = Path(__file__).resolve().parents[1]
SKILLS = iskl.SKILLS_ROOT
TOKEN = "python -m agenttalk"
DEVKIT_RUNNERS = ["craft-code", "refactor-code", "fix-ci", "assurance-scan"]
FRONT = '---\nname: x\ndescription: d\nreviewed-against: "0.105"\n---\n'
RULE = (
    'When `AGENTTALK_PY` is set (the seat\'s pinned runtime), use `"$AGENTTALK_PY" -m agenttalk`; '
    "otherwise use the form your project's agent manual gives.\n"
)
CODEX_RULE = (
    "If `AGENTTALK_PY` is set, invoke the bus with the pinned interpreter:\n\n"
    '```powershell\n& "$env:AGENTTALK_PY" -m agenttalk <subcommand> ...\n```\n\n'
    "If `AGENTTALK_PY` is not set, fall back to the runnable module form.\n"
)


def pinned_findings(tmp_path: Path, kind: str, body: str) -> list[sc.Finding]:
    """The pinned-runtime findings the lint reports for one skill file of the given kind."""
    path = tmp_path / ("x.md" if kind == "claude" else "x/SKILL.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FRONT + body, encoding="utf-8")
    found = sc.check_skill_file(
        path,
        kind=kind,
        inventory=sc.build_command_inventory(),
        current=sc.current_major_minor(__version__),
        require_stamp=False,
    )
    return [f for f in found if f.token == TOKEN]


# ------------------------------------------------------------------------------ the lint, on invented skills


@pytest.mark.parametrize(
    "body",
    [
        "Run `python -m agenttalk lane workspace --id x` first.\n",
        "```bash\npython -m agenttalk lane workspace --id x\n```\n",
        "Run `python -m agenttalk.assurance --root .` first.\n",
    ],
    ids=["inline", "fenced", "assurance-module"],
)
def test_a_plain_python_form_is_flagged_in_a_claude_skill_even_if_it_names_the_variable(tmp_path, body) -> None:
    found = pinned_findings(tmp_path, "claude", body + "Use AGENTTALK_PY when set.\n")
    assert len(found) == 1 and found[0].level == "error" and found[0].line > 0, found


def test_bare_agenttalk_and_the_pinned_form_are_clean_in_a_claude_skill(tmp_path) -> None:
    body = 'Run `agenttalk lane workspace --id x`, or `"$AGENTTALK_PY" -m agenttalk lane workspace --id x`.\n'
    assert pinned_findings(tmp_path, "claude", body) == []


@pytest.mark.parametrize("kind", ["codex", "devkit"])
def test_a_plain_python_form_is_flagged_in_a_codex_or_devkit_skill_that_does_not_state_the_rule(tmp_path, kind) -> None:
    found = pinned_findings(tmp_path, kind, "Run `python -m agenttalk lane workspace --id x` first.\n")
    assert len(found) == 1 and found[0].level == "error" and "AGENTTALK_PY" in found[0].reason, found


@pytest.mark.parametrize("kind", ["codex", "devkit"])
def test_the_same_form_is_clean_when_the_skill_states_the_rule(tmp_path, kind) -> None:
    assert pinned_findings(tmp_path, kind, RULE + "Run `python -m agenttalk lane workspace --id x` first.\n") == []


def test_the_assurance_module_form_needs_the_rule_in_a_devkit_skill_too(tmp_path) -> None:
    body = "Run `python -m agenttalk.assurance --root .` first.\n"
    assert len(pinned_findings(tmp_path, "devkit", body)) == 1
    assert (
        len(pinned_findings(tmp_path, "devkit", body + "Set AGENTTALK_PY first.\n")) == 1
    )  # a mention is not the rule
    assert pinned_findings(tmp_path, "devkit", body + RULE) == []


def test_the_ignore_comment_silences_one_line(tmp_path) -> None:
    body = "<!-- agenttalk-skill-lint: ignore-line --> `python -m agenttalk x`\n"
    assert pinned_findings(tmp_path, "claude", body) == []
    assert len(pinned_findings(tmp_path, "claude", body + "And `python -m agenttalk y`.\n")) == 1


def test_every_occurrence_in_a_claude_skill_is_reported_with_its_own_line(tmp_path) -> None:
    found = pinned_findings(tmp_path, "claude", "a `python -m agenttalk x`\nb\nc `python -m agenttalk y`\n")
    assert [f.line for f in found] == [len(FRONT.splitlines()) + 1, len(FRONT.splitlines()) + 3]


# ------------------------------------------------------------------------------ the real skills


def test_the_bundled_skills_have_no_pinned_runtime_finding() -> None:
    found = [f for f in sc.check_bundled_skills(SKILLS, __version__) if f.token == TOKEN]
    assert found == [], "; ".join(f"{f.file}:{f.line} {f.reason}" for f in found)


def test_the_claude_lead_lines_use_the_files_own_bare_form() -> None:
    text = (SKILLS / "claude" / "agenttalk.lead.md").read_text(encoding="utf-8")
    assert TOKEN not in text
    assert "`agenttalk lane assign ...`" in text and "`agenttalk lane workspace --id <id>`" in text


@pytest.mark.parametrize("skill", DEVKIT_RUNNERS)
def test_each_devkit_skill_that_runs_agenttalk_states_the_rule_once(skill) -> None:
    text = " ".join((SKILLS / "devkit" / skill / "SKILL.md").read_text(encoding="utf-8").split())
    assert text.count('"$AGENTTALK_PY" -m agenttalk') == 1, text
    assert "when `agenttalk_py` is set" in text.lower() and "otherwise" in text
    assert text.count('& "$env:AGENTTALK_PY" -m agenttalk') == 1, text
    if skill != "assurance-scan":
        assert "`python -m agenttalk` under Codex, `agenttalk` elsewhere" in text


# ------------------------------------------------------------------------------ install-skills sync


def test_installed_copies_carry_the_rule_and_match_the_bundled_files(tmp_path) -> None:
    cl_bus, cx_bus, cl_dev, cx_dev = (tmp_path / n for n in ("clb", "cxb", "cld", "cxd"))
    result = iskl.install(
        claude_dir=cl_bus, codex_dir=cx_bus, devkit=True, claude_skills_dir=cl_dev, codex_skills_dir=cx_dev
    )
    assert not result.counts().get("skipped")
    for skill in DEVKIT_RUNNERS:
        bundled = (SKILLS / "devkit" / skill / "SKILL.md").read_bytes()
        for installed in (cl_dev / skill / "SKILL.md", cx_dev / skill / "SKILL.md"):
            assert installed.read_bytes() == bundled
            assert b"AGENTTALK_PY" in bundled
    assert (cl_bus / "agenttalk.lead.md").read_bytes() == (SKILLS / "claude" / "agenttalk.lead.md").read_bytes()
    for copy in [
        *cl_bus.glob("*.md"),
        *cl_dev.glob("*/SKILL.md"),
        *cx_dev.glob("*/SKILL.md"),
        *cx_bus.glob("*/SKILL.md"),
    ]:
        text = copy.read_text(encoding="utf-8")
        if copy.parent == cl_bus:
            assert TOKEN not in text, copy
        elif TOKEN in text:
            assert "AGENTTALK_PY" in text, copy
    again = iskl.install(
        claude_dir=cl_bus, codex_dir=cx_bus, devkit=True, claude_skills_dir=cl_dev, codex_skills_dir=cx_dev
    )
    assert again.counts().get("unchanged") and not again.counts().get("copied")


# ------------------------------------------------------------------------------ honest wording


def test_the_changelog_entry_says_only_what_the_change_does() -> None:
    changelog = " ".join((ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split())
    unreleased = changelog[changelog.index("## [Unreleased]") : changelog.index("## [0.105.0]")]
    match = re.search(r"pinned runtime", unreleased)
    assert match and "when there is one" in unreleased[max(0, match.start() - 200) : match.end() + 200]
    for promise in ("same version", "never an older install", "prevents older client", "never an older client"):
        assert promise not in unreleased.lower(), promise


# ------------------------------------------------------- equivalent spellings (codex-dev's probes)

BACKSLASH = chr(92)
SPELLINGS = [
    "python -m agenttalk lane workspace --id x",
    "python  -m agenttalk lane workspace --id x",
    "python\t-m\tagenttalk lane workspace --id x",
    "python3 -m agenttalk lane workspace --id x",
    "py -m agenttalk lane workspace --id x",
    "python -m " + BACKSLASH + "\n  agenttalk lane workspace --id x",
    "python3  -m " + BACKSLASH + "\n\tagenttalk lane workspace --id x",
]


@pytest.mark.parametrize("kind", ["claude", "codex", "devkit"])
@pytest.mark.parametrize("command", SPELLINGS, ids=[f"s{i}" for i in range(len(SPELLINGS))])
def test_every_spelling_of_the_plain_module_form_is_found_in_a_fenced_block(tmp_path, kind, command) -> None:
    assert pinned_findings(tmp_path, kind, f"```bash\n{command}\n```\n")


@pytest.mark.parametrize("kind", ["claude", "codex", "devkit"])
@pytest.mark.parametrize(
    "command", ["python3 -m agenttalk", "py -m agenttalk", "python  -m agenttalk", "python -m agenttalk"]
)
def test_every_spelling_of_the_plain_module_form_is_found_inline(tmp_path, kind, command) -> None:
    assert pinned_findings(tmp_path, kind, f"Run `{command} lane workspace --id x`.\n")


@pytest.mark.parametrize("kind", ["claude", "codex", "devkit"])
def test_the_plain_form_is_found_in_prose_too(tmp_path, kind) -> None:
    assert pinned_findings(tmp_path, kind, "Then run python -m agenttalk lane workspace --id x yourself.\n")


@pytest.mark.parametrize("command", SPELLINGS, ids=[f"s{i}" for i in range(len(SPELLINGS))])
def test_every_spelling_is_clean_in_a_codex_or_devkit_skill_that_states_the_rule(tmp_path, command) -> None:
    for kind in ("codex", "devkit"):
        assert pinned_findings(tmp_path / kind, kind, RULE + f"```bash\n{command}\n```\n") == []
        assert pinned_findings(tmp_path / (kind + "2"), kind, CODEX_RULE + f"```bash\n{command}\n```\n") == []


@pytest.mark.parametrize(
    "not_a_command", ["agenttalk-send", "/agenttalk.lead", "copy -m agenttalk", "python -m agenttalkish"]
)
def test_names_that_only_look_like_the_form_are_not_flagged(tmp_path, not_a_command) -> None:
    assert pinned_findings(tmp_path, "claude", f"Use `{not_a_command}` here.\n") == []


# ------------------------------------------------------------------------------ the ignore comments follow the scanner


@pytest.mark.parametrize("gap", ["", "\n", "```bash\n"], ids=["next-line", "blank-line", "opening-fence"])
def test_ignore_next_holds_over_a_blank_line_and_an_opening_fence(tmp_path, gap) -> None:
    body = "<!-- agenttalk-skill-lint: ignore-next -->\n" + gap + "`python -m agenttalk lane workspace --id x`\n"
    if gap.startswith("```"):
        body += "```\n"
    spans, dangling = sc._command_spans(body)  # the scanner the CLI-token lint uses: the same boundaries
    assert spans == [] and dangling == []
    assert pinned_findings(tmp_path, "claude", body) == []


def test_ignore_next_covers_only_the_one_command_after_it(tmp_path) -> None:
    body = "<!-- agenttalk-skill-lint: ignore-next -->\n\n`python -m agenttalk x`\n`python -m agenttalk y`\n"
    assert [f.line for f in pinned_findings(tmp_path, "claude", body)] == [len(FRONT.splitlines()) + 4]


# ------------------------------------------------------------------------------ what counts as stating the rule


@pytest.mark.parametrize("kind", ["codex", "devkit"])
@pytest.mark.parametrize(
    "mention",
    [
        "<!-- AGENTTALK_PY -->",
        "<!-- when `AGENTTALK_PY` is set use it; otherwise not -->",
        "The variable NOT_AGENTTALK_PY is unrelated.",
        "AGENTTALK_PY_OTHER is unrelated; otherwise nothing.",
        "Set AGENTTALK_PY first.",
        "Use `AGENTTALK_PY` when you can.",
        "When `AGENTTALK_PY` is set, use it.",
        "if you like: `AGENTTALK_PY` (is set elsewhere), otherwise whatever",
        "When `NOT_AGENTTALK_PY` is set, use it; otherwise stop.",
        "When `AGENTTALK_PY_OTHER` is set, use it; otherwise stop.",
        "The variable AGENTTALK_PY is set by the wrapper; otherwise nothing.",
    ],
    ids=[
        "comment",
        "comment-with-the-rule",
        "other-name",
        "longer-name",
        "imperative",
        "mention",
        "no-fallback",
        "loose",
        "prefixed-name-in-a-rule",
        "suffixed-name-in-a-rule",
        "a-statement-not-a-condition",
    ],
)
def test_a_mention_a_comment_or_half_a_rule_does_not_state_the_rule(tmp_path, kind, mention) -> None:
    body = mention + "\nRun `python -m agenttalk lane workspace --id x`.\n"
    assert pinned_findings(tmp_path, kind, body)


@pytest.mark.parametrize("kind", ["codex", "devkit"])
def test_the_rule_must_sit_near_its_fallback(tmp_path, kind) -> None:
    far = "When `AGENTTALK_PY` is set, use it.\n" + "filler line.\n" * 80 + "Otherwise something unrelated.\n"
    assert pinned_findings(tmp_path, kind, far + "Run `python -m agenttalk x`.\n")


def test_the_rule_in_either_bundled_wording_is_recognised() -> None:
    assert sc._states_pinned_rule(RULE) and sc._states_pinned_rule(CODEX_RULE)
    assert not sc._states_pinned_rule("<!-- " + RULE + " -->")
    assert not sc._states_pinned_rule("")


# ------------------------------------------- more spellings of the interpreter (zteam reviewer-1, P3)

MORE_SPELLINGS = [
    "python.exe -m agenttalk lane workspace --id x",
    "py -3.14 -m agenttalk lane workspace --id x",
    "py -3 -u -m agenttalk lane workspace --id x",
    "python -I -m agenttalk lane workspace --id x",
    "python3.14 -m agenttalk lane workspace --id x",
    '"python" -m agenttalk lane workspace --id x',
    "'python' -m agenttalk lane workspace --id x",
    "/usr/bin/python3 -m agenttalk lane workspace --id x",
    "C:" + BACKSLASH + "Python314" + BACKSLASH + "python.exe -m agenttalk lane workspace --id x",
]


@pytest.mark.parametrize("kind", ["claude", "codex", "devkit"])
@pytest.mark.parametrize("command", MORE_SPELLINGS, ids=[f"m{i}" for i in range(len(MORE_SPELLINGS))])
def test_the_extra_interpreter_spellings_are_found_in_a_fenced_block_and_inline(tmp_path, kind, command) -> None:
    assert pinned_findings(tmp_path / "fence", kind, f"```bash\n{command}\n```\n")
    assert pinned_findings(tmp_path / "inline", kind, f"Run `{command}` first.\n")


@pytest.mark.parametrize("command", MORE_SPELLINGS, ids=[f"m{i}" for i in range(len(MORE_SPELLINGS))])
def test_the_extra_interpreter_spellings_are_clean_in_a_codex_or_devkit_skill_that_states_the_rule(
    tmp_path, command
) -> None:
    for kind in ("codex", "devkit"):
        assert pinned_findings(tmp_path / kind, kind, RULE + f"```bash\n{command}\n```\n") == []


@pytest.mark.parametrize("kind", ["claude", "codex", "devkit"])
@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest tests/test_x.py",
        "python -m build",
        "py -3 -m venv .venv",
        "pytest -m agenttalk",
        "pypy3 -m pytest",
        "python -m agenttalk_other",
        '"$AGENTTALK_PY" -m agenttalk lane workspace --id x',
        "agenttalk lane workspace --id x",
    ],
)
def test_nothing_else_is_taken_for_the_plain_form(tmp_path, kind, command) -> None:
    assert pinned_findings(tmp_path, kind, f"```bash\n{command}\n```\n") == []


# --------------------------------------------- the rule, wrapped at a line end (zteam reviewer-1, P2)

WRAPPED_RULES = {
    "when-then-newline": (
        'Run agenttalk with the pinned runtime. When\nAGENTTALK_PY is set, use `"$AGENTTALK_PY" -m agenttalk`;\n'
        "otherwise use the form your project's agent manual gives.\n"
    ),
    "if-then-newline-and-backticks": (
        'If\n`AGENTTALK_PY` is set, use `"$AGENTTALK_PY" -m agenttalk`; otherwise use the form your manual gives.\n'
    ),
    "wrapped-inside-the-condition": (
        'When\n  `AGENTTALK_PY`\nis\nset, use `"$AGENTTALK_PY" -m agenttalk` '
        "(the seat's pinned runtime); otherwise use the form your manual gives.\n"
    ),
    "wrapped-before-otherwise": (
        'When `AGENTTALK_PY` is set, use `"$AGENTTALK_PY" -m agenttalk`,\nand when it is not set use the\n'
        "form your manual gives.\n"
    ),
}


@pytest.mark.parametrize("kind", ["codex", "devkit"])
@pytest.mark.parametrize("name", sorted(WRAPPED_RULES))
def test_a_rule_wrapped_at_a_line_end_still_counts_as_the_rule(tmp_path, kind, name) -> None:
    body = WRAPPED_RULES[name] + "\n```bash\npython -m agenttalk lane workspace --id x\n```\n"
    assert pinned_findings(tmp_path, kind, body) == []
    assert sc._states_pinned_rule(body)


@pytest.mark.parametrize("kind", ["codex", "devkit"])
def test_a_wrapped_mention_that_is_not_the_rule_is_still_flagged(tmp_path, kind) -> None:
    body = (
        "The variable\nAGENTTALK_PY is set by the wrapper.\n\n```bash\npython -m agenttalk lane workspace --id x\n```\n"
    )
    assert pinned_findings(tmp_path, kind, body)
    assert not sc._states_pinned_rule(body)
    far = "When the seat starts" + "\n" * 3 + "x" * 80 + " AGENTTALK_PY is set, otherwise ...\n"
    assert not sc._states_pinned_rule(far)  # the condition must be close to the variable


# ------------------------------------------- the merge with the skill-fix branch (zteam reviewer-1, P2)


def test_assurance_scan_keeps_the_release_build_exception_before_the_pinned_sentence() -> None:
    """The skill-fix branch's release-build / network exception comes first, then the pinned-module sentence, then the
    module examples, which keep the module form."""
    text = " ".join((SKILLS / "devkit" / "assurance-scan" / "SKILL.md").read_text(encoding="utf-8").split())
    exception = text.index("One exception to know about: the `release` profile's built-in `python-build` step")
    pinned = text.index("Run the package module. When `AGENTTALK_PY` is set (the seat's pinned runtime)")
    example = text.index("python -m agenttalk.assurance --root . --profile change")
    assert exception < pinned < example
    assert text.count("Run the package module.") == 1  # the two branches' sentences were joined, not both kept


@pytest.mark.parametrize("kind", ["codex", "devkit"])
@pytest.mark.parametrize(
    "rule",
    [
        "When AGENTTALK_PY is set, do not use it; otherwise use python -m agenttalk",
        "When AGENTTALK_PY is set, use the runtime; otherwise use python -m agenttalk",
        'When AGENTTALK_PY is set, use "$OTHER_PY" -m agenttalk; otherwise use python -m agenttalk',
        'When AGENTTALK_PY is set, otherwise use "$AGENTTALK_PY" -m agenttalk',
    ],
)
def test_rule_requires_its_pinned_invocation_before_fallback(tmp_path, kind, rule):
    assert not sc._states_pinned_rule(rule)
    assert pinned_findings(tmp_path, kind, rule + "\nRun `python -m agenttalk x`.\n")


@pytest.mark.parametrize("command", ['"$AGENTTALK_PY" -m agenttalk', '& "$env:AGENTTALK_PY" -m agenttalk'])
def test_either_shell_pinned_invocation_states_the_rule(tmp_path, command):
    rule = f"When AGENTTALK_PY is set, use `{command}`; otherwise use your manual.\n"
    assert sc._states_pinned_rule(rule)
    assert pinned_findings(tmp_path, "codex", rule + "Run `python -m agenttalk x`.\n") == []
    assert pinned_findings(tmp_path, "claude", f"Run `{command} doctor`.\n") == []


def test_every_pinned_runtime_skill_gives_both_labelled_shell_forms():
    for path in SKILLS.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        if "AGENTTALK_PY" not in text:
            continue
        assert '"$AGENTTALK_PY" -m agenttalk' in text, path
        assert '& "$env:AGENTTALK_PY" -m agenttalk' in text, path
        assert "Bash" in text and "PowerShell" in text, path


@pytest.mark.parametrize("kind", ["codex", "devkit"])
@pytest.mark.parametrize(
    "negative",
    [
        "do not run",
        "never use",
        "don't use",
        "avoid running",
        "refrain from using",
        "do NOT run",
        "mustn't run",
        "don\u2019t use",
        "without using",
    ],
)
@pytest.mark.parametrize("command", ['"$AGENTTALK_PY" -m agenttalk', '& "$env:AGENTTALK_PY" -m agenttalk'])
def test_a_negated_pinned_invocation_does_not_state_the_rule(tmp_path, kind, negative, command):
    rule = f"When AGENTTALK_PY is set, {negative} `{command}`; otherwise use python -m agenttalk.\n"
    assert not sc._states_pinned_rule(rule)
    assert pinned_findings(tmp_path, kind, rule + "Run `python -m agenttalk x`.\n")


def test_unquoted_powershell_pinned_invocation_states_the_rule(tmp_path):
    command = "& $env:AGENTTALK_PY -m agenttalk"
    rule = f"When AGENTTALK_PY is set, use `{command}`; otherwise use your manual.\n"
    assert sc._states_pinned_rule(rule)
    assert pinned_findings(tmp_path, "codex", rule + "Run `python -m agenttalk x`.\n") == []


# --------------------- a "do not" after the command, and a command split over two lines (#801, after #799)

PINNED_FORMS = {
    "bash": '"$AGENTTALK_PY" -m agenttalk',
    "powershell": '& "$env:AGENTTALK_PY" -m agenttalk',
}


@pytest.mark.parametrize("kind", ["codex", "devkit"])
@pytest.mark.parametrize("shell", sorted(PINNED_FORMS))
@pytest.mark.parametrize("negation", ["must not be used", "is never permitted"])
def test_a_do_not_after_the_pinned_command_does_not_state_the_rule(tmp_path, kind, shell, negation) -> None:
    """#801 gap 1: the guide says the opposite of the rule, so the plain command that follows is a finding."""
    rule = f"When AGENTTALK_PY is set, `{PINNED_FORMS[shell]}` {negation}; otherwise use python -m agenttalk.\n"
    assert not sc._states_pinned_rule(rule)
    assert pinned_findings(tmp_path, kind, rule + "Run `python -m agenttalk x`.\n")


SPLIT_FORMS = {
    "bash": ('"$AGENTTALK_PY" \\\n  -m agenttalk <subcommand> ...', "bash"),
    "powershell": ('& "$env:AGENTTALK_PY" `\n  -m agenttalk <subcommand> ...', "powershell"),
}


@pytest.mark.parametrize("kind", ["codex", "devkit"])
@pytest.mark.parametrize("shell", sorted(SPLIT_FORMS))
def test_a_pinned_command_split_over_two_lines_still_states_the_rule(tmp_path, kind, shell) -> None:
    """#801 gap 2: the rule is the same guide whether the pinned command is on one line or continued onto the next."""
    command, fence = SPLIT_FORMS[shell]
    body = (
        "When `AGENTTALK_PY` is set, run the pinned interpreter:\n\n"
        f"```{fence}\n{command}\n```\n\n"
        "Otherwise use the form your project's agent manual gives, for example:\n\n"
        "```bash\npython -m agenttalk lane workspace --id x\n```\n"
    )
    assert sc._states_pinned_rule(body)
    assert pinned_findings(tmp_path, kind, body) == []


@pytest.mark.parametrize("shell", sorted(SPLIT_FORMS))
def test_a_split_pinned_command_with_a_do_not_after_it_still_does_not_state_the_rule(tmp_path, shell) -> None:
    command, fence = SPLIT_FORMS[shell]
    body = (
        f"When `AGENTTALK_PY` is set, this must not be used:\n\n```{fence}\n{command}\n```\n\n"
        "Otherwise run:\n\n```bash\npython -m agenttalk lane workspace --id x\n```\n"
    )
    assert not sc._states_pinned_rule(body)
    assert pinned_findings(tmp_path, "codex", body)


def test_a_backslash_or_backtick_outside_a_fence_joins_nothing() -> None:
    """Only a fenced command is continued (as the plain-command scan does it): prose that ends in a backtick is not."""
    prose = 'When AGENTTALK_PY is set, run `"$AGENTTALK_PY"`\n-m agenttalk; otherwise use your manual.\n'
    assert not sc._states_pinned_rule(prose)


# A negation after the pinned command counts only when it is about THAT command (#801, codex-rev's read of the fix).

VALID_AFTER = {
    "about-the-unpinned-form": "{cmd}; do not use the unpinned form while it is set; otherwise use your manual.",
    "not-optional": "{cmd}; the pinned interpreter is not optional; otherwise use your manual.",
    "never-skip-it": "{cmd} (never skip it); otherwise use your manual.",
    "never-the-bare-form": "{cmd}; never use the bare form while it is set; otherwise use your manual.",
    "no-other-runtime": "{cmd}, and do not replace it with another runtime; otherwise use your manual.",
    "not-to-be-omitted": "{cmd}, which is not to be omitted; otherwise use your manual.",
}


@pytest.mark.parametrize("kind", ["codex", "devkit"])
@pytest.mark.parametrize("shell", sorted(PINNED_FORMS))
@pytest.mark.parametrize("name", sorted(VALID_AFTER))
def test_a_negation_about_something_else_or_in_support_of_the_rule_still_states_the_rule(
    tmp_path, kind, shell, name
) -> None:
    rule = "When AGENTTALK_PY is set, run " + VALID_AFTER[name].format(cmd=f"`{PINNED_FORMS[shell]}`") + "\n"
    assert sc._states_pinned_rule(rule), rule
    assert pinned_findings(tmp_path, kind, rule + "Run `python -m agenttalk x`.\n") == []


INVALID_AFTER = {
    "do-not-use-it": "{cmd}; do not use it; otherwise use python -m agenttalk.",
    "it-must-not-be-used": "{cmd}; it must not be used; otherwise use python -m agenttalk.",
    "do-not-use-the-pinned-command": "{cmd}; do not use the pinned command; otherwise use python -m agenttalk.",
    "same-clause": "{cmd} must not be used; otherwise use python -m agenttalk.",
    "never-permitted": "{cmd} is never permitted; otherwise use python -m agenttalk.",
    "never-run-this": "{cmd}. Never run this; otherwise use python -m agenttalk.",
}


@pytest.mark.parametrize("kind", ["codex", "devkit"])
@pytest.mark.parametrize("shell", sorted(PINNED_FORMS))
@pytest.mark.parametrize("name", sorted(INVALID_AFTER))
def test_a_negation_that_forbids_the_pinned_command_still_does_not_state_the_rule(tmp_path, kind, shell, name) -> None:
    rule = "When AGENTTALK_PY is set, run " + INVALID_AFTER[name].format(cmd=f"`{PINNED_FORMS[shell]}`") + "\n"
    assert not sc._states_pinned_rule(rule), rule
    assert pinned_findings(tmp_path, kind, rule + "Run `python -m agenttalk x`.\n")


def test_the_example_from_the_review_is_accepted_and_its_opposite_is_not(tmp_path) -> None:
    valid = (
        'When AGENTTALK_PY is set, run "$AGENTTALK_PY" -m agenttalk; do not use the unpinned form while it is set; '
        "otherwise use your manual. Run `python -m agenttalk send`.\n"
    )
    assert pinned_findings(tmp_path, "codex", valid) == []
    opposite = valid.replace("do not use the unpinned form", "do not use the pinned form")
    assert pinned_findings(tmp_path, "codex", opposite)


# ----------------------------------------- punctuation inside the command's own words (#861, after #801)

IN_THE_SPAN_FORBIDDEN = {
    "dotted-module-must-not": "When AGENTTALK_PY is set, `{cmd}.assurance` must not be used; "
    "otherwise run `python -m agenttalk send`.",
    "dotted-module-do-not": "When AGENTTALK_PY is set, `{cmd}.assurance` do not use while it is set; "
    "otherwise run `python -m agenttalk send`.",
    "dotted-argument-must-not": "When AGENTTALK_PY is set, `{cmd} send --file reply.md` must not be used; "
    "otherwise run `python -m agenttalk send`.",
    "dotted-argument-do-not": "When AGENTTALK_PY is set, `{cmd} send --file reply.md` do not use while it is set; "
    "otherwise run `python -m agenttalk send`.",
    "colon-in-span-must-not": "When AGENTTALK_PY is set, `{cmd} send --meta status:done` must not be used; "
    "otherwise run `python -m agenttalk send`.",
    "sentence-in-span-must-not": "When AGENTTALK_PY is set, `{cmd} send --note 'Done. Next'` must not be used; "
    "otherwise run `python -m agenttalk send`.",
}
IN_THE_SPAN_VALID = {
    "dotted-module": "When AGENTTALK_PY is set, run `{cmd}.assurance scan`; otherwise use your manual.",
    "dotted-argument": "When AGENTTALK_PY is set, run `{cmd} send --file reply.md`; do not use the unpinned form "
    "while it is set; otherwise use your manual.",
    "a-real-period-still-ends-the-clause": "When AGENTTALK_PY is set, run `{cmd} send`. Do not use the unpinned form; "
    "otherwise use your manual.",
}


@pytest.mark.parametrize("kind", ["codex", "devkit"])
@pytest.mark.parametrize("shell", sorted(PINNED_FORMS))
@pytest.mark.parametrize("name", sorted(IN_THE_SPAN_FORBIDDEN))
def test_a_prohibition_after_punctuation_inside_the_command_span_is_seen(tmp_path, kind, shell, name) -> None:
    """#861: a period or colon INSIDE the command's code span does not end the clause, so the "must not" that follows
    is still about the command."""
    rule = IN_THE_SPAN_FORBIDDEN[name].format(cmd=PINNED_FORMS[shell]) + "\n"
    assert not sc._states_pinned_rule(rule), rule
    assert pinned_findings(tmp_path, kind, rule + "Run `python -m agenttalk x`.\n")


@pytest.mark.parametrize("kind", ["codex", "devkit"])
@pytest.mark.parametrize("shell", sorted(PINNED_FORMS))
@pytest.mark.parametrize("name", sorted(IN_THE_SPAN_VALID))
def test_a_valid_rule_with_punctuation_inside_the_command_span_still_states_the_rule(
    tmp_path, kind, shell, name
) -> None:
    rule = IN_THE_SPAN_VALID[name].format(cmd=PINNED_FORMS[shell]) + "\n"
    assert sc._states_pinned_rule(rule), rule
    assert pinned_findings(tmp_path, kind, rule + "Run `python -m agenttalk x`.\n") == []


UNQUOTED_FORBIDDEN = {
    "dotted-module": "When AGENTTALK_PY is set, {cmd}.assurance must not be used; otherwise run python -m agenttalk x.",
    "dotted-argument": "When AGENTTALK_PY is set, {cmd} send --file reply.md must not be used; "
    "otherwise run python -m agenttalk x.",
}


@pytest.mark.parametrize("shell", sorted(PINNED_FORMS))
@pytest.mark.parametrize("name", sorted(UNQUOTED_FORBIDDEN))
def test_a_period_inside_a_word_of_a_command_written_without_backticks_does_not_end_the_clause(shell, name) -> None:
    """The same split without a code span: a period ends a clause only before whitespace or the end of the text."""
    rule = UNQUOTED_FORBIDDEN[name].format(cmd=PINNED_FORMS[shell]) + "\n"
    assert not sc._states_pinned_rule(rule), rule


def test_the_rule_pattern_still_reads_the_text_as_written_not_the_masked_copy() -> None:
    """Only the cutting of the words after the command reads the masked copy: a period inside a code span between
    "When" and the variable still keeps the condition from counting (the pattern allows no period there)."""
    rule = 'When `a.b` says so, AGENTTALK_PY is set, run `"$AGENTTALK_PY" -m agenttalk`; otherwise use your manual.\n'
    assert not sc._states_pinned_rule(rule)
