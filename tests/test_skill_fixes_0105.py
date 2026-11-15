"""The bundled skills after the 0.105.0 skills review (batch 1): each fixed piece of advice stays fixed.

The pilot read the send, listen and lead skills (both assistants), the shared devkit holder and the
evidence reference against the real commands. What it found and what these tests keep from coming back:

* a message body with non-ASCII text must go through `--file <path>`; piping it into `--file -` is read in the
  Windows code page and stored garbled (the README says the same);
* the roster check is not proof of who sent a message; an unmarked `end` does not end the loop;
* the lead's "is the worktree clean?" check must see new files (`--untracked-files=all`) and quote the path (#93);
* reply examples name the sender; longer bodies go in a file; `na_reason` is a bus requirement.

The tests read the shipped files as text; they need no network and no store.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "src" / "agenttalk" / "skills"
README = ROOT / "README.md"

SEND = [SKILLS / "claude" / "agenttalk.send.md", SKILLS / "codex" / "agenttalk-send" / "SKILL.md"]
LISTEN = [SKILLS / "claude" / "agenttalk.listen.md", SKILLS / "codex" / "agenttalk-listen" / "SKILL.md"]
LEAD = [SKILLS / "claude" / "agenttalk.lead.md", SKILLS / "codex" / "agenttalk-lead" / "SKILL.md"]
EVIDENCE = SKILLS / "devkit" / "_shared" / "references" / "evidence.md"
HOLDER = SKILLS / "devkit" / "_shared" / "SKILL.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def flat(path: Path) -> str:
    """The text with markdown bold removed and all white space made single spaces."""
    return " ".join(read(path).replace("**", "").split())


def ids(paths: list[Path]) -> list[str]:
    return [f"{p.parent.name}/{p.name}" for p in paths]


# ------------------------------------------------------------------------------ the body goes in a file


@pytest.mark.parametrize("path", [*SEND, *LISTEN, *LEAD, README], ids=ids([*SEND, *LISTEN, *LEAD, README]))
def test_no_text_recommends_piping_a_body_into_file_dash(path: Path) -> None:
    """Every `--file -` is inside a paragraph that tells the reader NOT to pipe a body into it."""
    for paragraph in read(path).replace("**", "").split("\n\n"):
        if "--file -" in paragraph:
            assert re.search(r"\bnot\b[^.]{0,20}\bpipe", paragraph), paragraph


@pytest.mark.parametrize("path", SEND, ids=ids(SEND))
def test_the_send_skills_teach_a_utf8_file_and_say_why(path: Path) -> None:
    text = flat(path)
    assert "UTF-8 text file" in text
    assert re.search(r'--file "?\$BODYFILE"?', text)
    assert "Windows code page" in text and "garbled" in text


def test_the_readme_windows_note_teaches_a_file_not_a_pipe() -> None:
    text = flat(README)
    assert "Write the body to a UTF-8 file and pass `--file <path>`" in text
    assert "pipe a PowerShell here-string" not in text


# ------------------------------------------------------------------------------ what the roster check proves


@pytest.mark.parametrize("path", LISTEN, ids=ids(LISTEN))
def test_the_listen_skills_do_not_call_a_roster_check_authentication(path: Path) -> None:
    text = flat(path)
    assert "explicitly-authenticated" not in text
    assert "and forged sender" not in text
    assert "not proof of who" in text and "unless the store is signed" in text


@pytest.mark.parametrize("path", LISTEN, ids=ids(LISTEN))
def test_the_listen_table_rows_for_end_and_release_name_the_authority_rule(path: Path) -> None:
    rows = {
        line.split("|")[1].strip(): line
        for line in read(path).splitlines()
        if line.startswith("| `end`") or line.startswith("| `release`")
    }
    assert set(rows) == {"`end`", "`release`"}
    for row in rows.values():
        assert "full authority envelope" in row, row
    assert "unmarked `end`" in rows["`end`"] and "ignored" in rows["`end`"]


# ------------------------------------------------------------------------------ the lead's clean-worktree check


@pytest.mark.parametrize("path", LEAD, ids=ids(LEAD))
def test_the_clean_check_sees_new_files_and_quotes_the_worktree_path(path: Path) -> None:
    text = read(path)
    assert "git status --porcelain --untracked-files=all" in text
    assert "status.showUntrackedFiles=no" in text  # the reason, right where the command is
    assert not re.search(r"^\s*git -C .*status --porcelain(?! --untracked-files=all)", text, re.MULTILINE)
    assert "an empty `git status --porcelain --untracked-files=all`" in " ".join(text.split())
    assert "-C <worktree>" not in text
    assert text.count('git -C "<worktree>"') == 3  # status, log and the push alternative


# ------------------------------------------------------------------------------ smaller slips


DEVKIT_DIR = SKILLS / "devkit"
HANDOFF = [SKILLS / "claude" / "agenttalk.handoff.md", SKILLS / "codex" / "agenttalk-handoff" / "SKILL.md"]
PROPOSE = [SKILLS / "claude" / "agenttalk.propose.md", SKILLS / "codex" / "agenttalk-propose" / "SKILL.md"]
SENDER_NAMED = [*SEND, *LISTEN, *LEAD, *HANDOFF, *PROPOSE]


@pytest.mark.parametrize("path", SENDER_NAMED, ids=ids(SENDER_NAMED))
def test_every_reply_example_names_the_sender(path: Path) -> None:
    assert not re.findall(r"agenttalk reply (?!--from)--to-", read(path))


@pytest.mark.parametrize("path", [*LISTEN, *LEAD], ids=ids([*LISTEN, *LEAD]))
def test_longer_bodies_in_lead_and_listen_examples_use_a_file(path: Path) -> None:
    text = read(path)
    for inline in (
        '-m "<goal, scope',
        '-m "<result>"',
        '-m "<what each recipient should answer>"',
        '-m "<your rationale>"',
        '-m "<proposal body>"',
        '-m "<your critique>"',
    ):
        assert inline not in text, inline


def test_na_reason_is_described_as_a_bus_requirement_with_the_accepted_spellings() -> None:
    text = read(EVIDENCE)
    line = next(line for line in text.splitlines() if line.startswith("- `na_reason`"))
    assert "BUS-VALIDATED" in line
    flat_text = " ".join(text.split())
    assert "`na_reason_<field>`, `na_reason.<field>` or `na_reason`" in flat_text


BATCH2 = [
    SKILLS / "claude" / "agenttalk.challenge.md",
    SKILLS / "claude" / "agenttalk.consult.md",
    SKILLS / "claude" / "agenttalk.propose.md",
    SKILLS / "claude" / "agenttalk.md",
    SKILLS / "devkit" / "review-code" / "SKILL.md",
]
ROUND3 = [
    DEVKIT_DIR / rel
    for rel in (
        "definition-of-done/references/migration.md",
        "_shared/references/plain-language.md",
        "write-for-humans/SKILL.md",
        "write-docs/SKILL.md",
        "thinking-games/SKILL.md",
        "test-docs/SKILL.md",
        "qa-strategy/SKILL.md",
        "tester-qa/SKILL.md",
        "test-coverage/SKILL.md",
        "test-integration/SKILL.md",
        "test-performance/SKILL.md",
        "test-security/SKILL.md",
    )
]
STAMPED = [*SEND, *LISTEN, *LEAD, EVIDENCE, HOLDER, *BATCH2, *ROUND3]  # LEAD[0] after B4-1 fixed its two lines


@pytest.mark.parametrize("path", STAMPED, ids=ids(STAMPED))
def test_the_files_the_pilot_read_in_full_carry_the_0105_stamp(path: Path) -> None:
    assert re.search(r'^reviewed-against: "0\.105"$', read(path), re.MULTILINE)


@pytest.mark.parametrize("path", HANDOFF, ids=ids(HANDOFF))
def test_the_handoff_skills_do_not_end_the_session_on_a_bare_end(path: Path) -> None:
    text = flat(path)
    assert "the session is over" not in text
    assert "even one from the lead" in text and "carries no authority and is ignored" in text
    assert re.search(r'^reviewed-against: "0\.(43|55|75)"$', read(path), re.MULTILINE)  # not stamped: not re-read yet


def test_the_lead_and_listen_notes_do_not_call_every_inline_body_unsafe() -> None:
    for path in [*LEAD, *LISTEN]:
        text = flat(path)
        assert "has quotes in it" in text, path


# ------------------------------------------------------------------------------ round 2: batch 2 (handoff, propose)


@pytest.mark.parametrize("path", HANDOFF, ids=ids(HANDOFF))
def test_the_handoff_status_example_sees_untracked_files_and_says_why(path: Path) -> None:
    text = flat(path)
    assert "`git status --short --untracked-files=all`" in text
    assert "status.showUntrackedFiles" in text
    assert not re.search(r"<paste `git status --short` ", text)


@pytest.mark.parametrize("path", PROPOSE, ids=ids(PROPOSE))
def test_the_proposal_keeps_the_chosen_decision_maker(path: Path) -> None:
    """The send step must not set the target back to the default peer after step 2 chose one."""
    text = read(path)
    assert re.search(r'TARGET="\$\{TARGET:-|if \(-not \$TARGET\)', text)
    assert not re.search(r'^\s*\$?TARGET\s*=\s*(?:if \(\$env:AGENTTALK_PEER\)|"\$\{AGENTTALK_PEER)', text, re.M)


def managed_commands(path: Path) -> list[str]:
    """The managed-turn command of every fenced block that tests AGENTTALK_WRAPPER_GENERATION: the lines between that
    test and its `else`, comments dropped and continuation lines joined into one line."""
    found = []
    for block in re.findall(r"```\w*\n(.*?)```", read(path), re.S):
        if "AGENTTALK_WRAPPER_GENERATION" not in block:
            continue
        match = re.search(r"AGENTTALK_WRAPPER_GENERATION[^\n]*\n(.*?)\n\s*(?:\} )?else", block, re.S)
        assert match, block
        lines = [line for line in match.group(1).splitlines() if not line.strip().startswith("#")]
        found.append(" ".join(re.sub(r"\s*[\\`]$", "", line).strip() for line in lines))
    return found


@pytest.mark.parametrize("path", PROPOSE, ids=ids(PROPOSE))
def test_a_managed_wrapper_turn_sends_the_proposal_with_an_await_and_returns(path: Path) -> None:
    text = flat(path)
    assert "AGENTTALK_WRAPPER_GENERATION" in text
    assert "records no across-turn wait" in text
    assert "There is no `propose --await-reply`" in text
    assert "return immediately to the wrapper" in text
    assert "In a manual (unwrapped) session, wait for the response" in text


@pytest.mark.parametrize("path", PROPOSE, ids=ids(PROPOSE))
def test_the_managed_command_of_the_proposal_and_of_the_counter_carries_the_await_the_id_and_the_kind(
    path: Path,
) -> None:
    """Two managed blocks: the proposal (step 4) and the counter (the counter section). Each managed command is a
    `send --kind proposal` with a tracked request id and `--await-reply`; the counter also links the old proposal."""
    commands = managed_commands(path)
    assert len(commands) == 2, commands
    for command in commands:
        assert re.match(r"(?:python -m )?agenttalk send --from \S+ --to \S+ --kind proposal ", command), command
        assert re.search(r"--meta request_id=\S+", command), command
        assert re.search(r"--await-reply(?: |$)", command), command
        assert "propose" not in command.split("--subject")[0], command
    assert "in_reply_to=<old-request-id>" in commands[1], commands[1]
    assert "in_reply_to" not in commands[0]


@pytest.mark.parametrize("path", PROPOSE, ids=ids(PROPOSE))
def test_the_manual_branch_of_each_block_is_still_a_plain_propose(path: Path) -> None:
    blocks = [b for b in re.findall(r"```\w*\n(.*?)```", read(path), re.S) if "AGENTTALK_WRAPPER_GENERATION" in b]
    assert len(blocks) == 2
    for block in blocks:
        manual = re.split(r"\n\s*(?:\} )?else[^\n]*\n", block, maxsplit=1)[1]
        assert re.search(r"agenttalk propose --from \S+ --to \S+", manual) and "--await-reply" not in manual
    counter_manual = re.split(r"\n\s*(?:\} )?else[^\n]*\n", blocks[1], maxsplit=1)[1]
    assert "--in-reply-to <old-request-id>" in counter_manual  # the manual counter still links the old proposal


@pytest.mark.parametrize("path", PROPOSE, ids=ids(PROPOSE))
def test_the_counter_example_names_the_proposal_it_closes(path: Path) -> None:
    text = flat(path)
    assert re.search(r"reply --from \S+ --to-request <old-request-id> --kind proposal-response", text)
    assert "newest message you received" in text


# ------------------------------------------------------------------------------ round 2: batch 3 (devkit)

DEVKIT = SKILLS / "devkit"


def test_the_close_recipe_names_the_isolation_flags_and_the_hold_code() -> None:
    text = flat(DEVKIT / "system-review-protocol" / "SKILL.md")
    assert "`--lane-artifact <path>`" in text and "`--non-lane-isolation-not-asserted`" in text
    assert (
        text.count("worktree_isolation_unverified") == 2
    )  # at open (what happens without a flag) and in the HOLD list


def test_the_pin_check_compares_after_turning_crlf_into_lf() -> None:
    text = flat(DEVKIT / "definition-of-done" / "SKILL.md")
    assert "after turning any CRLF line endings into LF" in text
    assert "git hash-object --stdin" in text
    assert "On a seat, `git hash-object --no-filters <installed file>` for each file" not in text


def test_the_release_readiness_skill_says_where_secondary_risks_go() -> None:
    text = flat(DEVKIT / "review-release-readiness" / "SKILL.md")
    assert "`risk_class` takes exactly ONE value" in text and "`residual_risk`" in text


def test_routing_names_each_bus_skill_in_both_vendors_forms() -> None:
    text = flat(DEVKIT / "_shared" / "references" / "routing.md")
    assert "agenttalk-lead (Claude: /agenttalk.lead)" in text
    assert "agenttalk-listen (Claude: /agenttalk.listen)" in text
    assert "(Claude: /agenttalk.lead, /agenttalk.listen, /agenttalk.handoff, /agenttalk.send)" in text


def test_system_review_protocol_names_the_handoff_and_lead_skills_in_both_forms() -> None:
    text = flat(DEVKIT / "system-review-protocol" / "SKILL.md")
    assert "use agenttalk-handoff, or /agenttalk.handoff in Claude" in text
    assert "use agenttalk-lead, or /agenttalk.lead in Claude" in text


# ------------------------------------------------------------------------------ round 3: batches 4 and 5

ASSURANCE = DEVKIT / "assurance-scan"


def test_refactor_code_is_described_for_any_agent_not_for_codex_only() -> None:
    text = flat(DEVKIT / "refactor-code" / "SKILL.md")
    assert "Use when an agent is asked for behavior-preserving cleanup" in text
    assert "Use when Codex is asked" not in text


def test_assurance_scan_names_the_release_build_step_and_its_install_and_network_reach() -> None:
    text = flat(ASSURANCE / "SKILL.md")
    assert "`python-build` step runs `python -m build` with build isolation" in text
    assert "can install the project's build requirements, so it may use the network" in text
    assert "even when the manifest does not allow network" in text


def test_assurance_scan_says_deep_adds_nothing_by_default() -> None:
    text = flat(ASSURANCE / "SKILL.md")
    assert "With the built-in defaults it runs the same checks as `change`" in text
    assert "a broader evidence profile" not in text


def test_the_artifact_schema_says_a_finalization_failure_exits_1_with_the_artifact_left_in_place() -> None:
    text = flat(ASSURANCE / "references" / "artifact-schema.md")
    assert "the coverage finalization that follows succeeds" in text
    assert "the runner exits 1 but leaves the artifact in place" in text


def test_the_manifest_schema_says_paths_include_does_not_narrow_a_scan() -> None:
    text = flat(ASSURANCE / "references" / "manifest-schema.md")
    assert "`paths.include` is accepted but the built-in file discovery does not read it" in text
    assert "Use top-level `paths.include` and `paths.exclude`" not in text


def test_the_baseline_policy_says_which_fields_the_loader_defaults() -> None:
    text = flat(ASSURANCE / "references" / "baseline-policy.md")
    assert "The loader itself requires only `schema_version: 1`" in text
    assert "when `baseline_id` is missing it uses `baseline`" in text
    assert "when `findings` is missing it uses an empty list" in text
    assert not re.search(r"^Required fields:", read(ASSURANCE / "references" / "baseline-policy.md"), re.M)


NOT_STAMPED_YET = [
    DEVKIT_DIR / rel
    for rel in (
        "craft-code/SKILL.md",
        "refactor-code/SKILL.md",
        "fix-ci/SKILL.md",
        "assurance-scan/SKILL.md",
    )
] + [*HANDOFF, PROPOSE[1]]  # the Claude propose skill carried 0.105 from an earlier round


@pytest.mark.parametrize("path", NOT_STAMPED_YET, ids=ids(NOT_STAMPED_YET))
def test_files_with_an_open_finding_or_not_yet_read_again_keep_their_old_stamp(path: Path) -> None:
    assert not re.search(r'^reviewed-against: "0\.105"$', read(path), re.MULTILINE)


def test_the_security_reference_without_a_stamp_field_did_not_get_one() -> None:
    assert "reviewed-against" not in read(DEVKIT_DIR / "review-code" / "references" / "security.md")


# ------------------------------------------------------------------------------ round 4

CONDITIONAL_WARNING = "standard input uses the Windows code page"


@pytest.mark.parametrize("path", SEND, ids=ids(SEND))
def test_the_powershell_body_file_block_sets_the_path_before_it_writes(path: Path) -> None:
    blocks = [b for b in re.findall(r"```powershell\n(.*?)```", read(path), re.S) if "WriteAllText" in b]
    assert len(blocks) == 1, blocks
    block = blocks[0]
    assert re.search(r'^\s*\$BODYFILE\s*=\s*"', block, re.M), block
    assert block.index("$BODYFILE =") < block.index("WriteAllText")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is not installed")
@pytest.mark.parametrize("path", SEND, ids=ids(SEND))
def test_the_powershell_body_file_block_really_writes_a_utf8_file_without_a_bom(path: Path, tmp_path: Path) -> None:
    """The block as documented, run in a fresh `pwsh -NoProfile`: only the path and the body are filled in."""
    block = next(b for b in re.findall(r"```powershell\n(.*?)```", read(path), re.S) if "WriteAllText" in b)
    target = tmp_path / "body.txt"
    block = block.replace("<full path of the body file>", str(target))
    body = '$body = "caf" + [char]0xE9 + " " + [char]0x2014 + " " + [char]0x65E5 + [char]0x672C'
    done = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", body + "\n" + block],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert target.read_bytes() == "café — 日本".encode("utf-8")  # UTF-8, no BOM, no added newline


@pytest.mark.parametrize("path", [*SEND, README], ids=ids([*SEND, README]))
def test_the_windows_warning_says_when_it_happens_and_that_a_utf8_stdin_is_exact(path: Path) -> None:
    text = flat(path)
    assert CONDITIONAL_WARNING in text
    assert "A UTF-8 standard input is exact" in text
    assert "On Windows the CLI reads piped text" not in text


def test_the_changelog_warning_says_when_it_happens() -> None:
    assert CONDITIONAL_WARNING in " ".join(read(ROOT / "CHANGELOG.md").split())


def test_the_claude_lead_skill_says_the_bare_command_and_carries_the_0105_stamp() -> None:
    """B4-1 fixed the two lines that named a plain `python -m agenttalk`; the old command must never come back."""
    text = read(LEAD[0])
    assert re.search(r'^reviewed-against: "0\.105"$', text, re.MULTILINE)
    assert "`agenttalk lane workspace --id <id>`" in text  # the corrected, bare command
    assert "python -m agenttalk" not in text  # the Claude skills never name the plain module form
    changelog = " ".join(read(ROOT / "CHANGELOG.md").split())
    assert (
        "The Claude lead skill, whose two plain `python -m agenttalk` lines now say `agenttalk`, "
        "is stamped `reviewed-against 0.105` too."
    ) in changelog
    assert "also keeps its older stamp: two of its lines still" not in changelog
