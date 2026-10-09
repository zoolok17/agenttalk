"""The lessons-lookup step (docs/ops/lessons-lookup.md): the wrapped-turn prompt and the listen and lead skills
carry one short, bounded, read-only search before substantive work, with three distinct outcomes, and never an
inbox command."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.install_skills import SKILLS_ROOT
from agenttalk.store import Store
from agenttalk.wrapper.prompt import (
    _LESSON_LOOKUP_RULES,
    assemble_cadence_prompt,
    assemble_turn_prompt,
)

INBOX = re.compile(r"agenttalk\s+(sync|threads|drain|recv|wait|ack)\b")
HEADING = "## Lessons lookup before substantive work"
RECORD = {"kind": "task", "from": "lead", "to": "dev", "body": "do it", "meta": {}, "request_id": "tk-1"}
OUTCOMES = ("found nothing", "failed", "read but not useful")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _section(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(HEADING)
    end = text.index("\n## ", start + 1)
    return text[start:end]


SKILL_FILES = [
    ("claude", SKILLS_ROOT / "claude" / "agenttalk.listen.md",
     "agenttalk knowledge search <term> --type lesson --limit 5"),
    ("codex", SKILLS_ROOT / "codex" / "agenttalk-listen" / "SKILL.md",
     "python -m agenttalk knowledge search <term> --type lesson --limit 5"),
]


# ------------------------------------------------------------------ the wrapped-turn prompt


@pytest.mark.parametrize("reply_shell,command", [
    ("powershell", '& "$env:AGENTTALK_PY" -m agenttalk knowledge search <term> --type lesson --limit 5'),
    ("bash", '"$AGENTTALK_PY" -m agenttalk knowledge search <term> --type lesson --limit 5'),
])
def test_the_turn_prompt_carries_the_one_bounded_lookup(reply_shell: str, command: str) -> None:
    prompt = assemble_turn_prompt(RECORD, reply_shell=reply_shell)
    assert prompt.count("LESSONS LOOKUP") == 1
    text = _norm(prompt)
    assert command in text
    for phrase in ("ONE bounded, read-only lookup", "literal substring matching", "never paste a task sentence",
                   "One reformulation is allowed", "do not search again later", "skip the lookup for an ack",
                   "A failed lookup never blocks the work", "cite only a lesson that changed a decision or a check"):
        assert phrase in text, phrase
    if reply_shell == "bash":
        block = prompt[prompt.index("LESSONS LOOKUP"):prompt.index("BUS-COMMAND CONTRACT")]
        assert "$env:" not in block and "& " not in block


def test_the_prompt_words_the_three_outcomes_apart() -> None:
    text = _norm(_LESSON_LOOKUP_RULES)
    assert "found nothing; the lookup failed (say so, never report it as found nothing); read but not useful" in text


def test_the_lookup_text_is_short_and_has_no_inbox_command() -> None:
    assert len(_LESSON_LOOKUP_RULES) < 1500
    assert not INBOX.search(_norm(_LESSON_LOOKUP_RULES))
    prompt = _norm(assemble_turn_prompt(RECORD))
    assert "NEVER run agenttalk sync / threads / drain / recv / wait / ack" in prompt, "the inbox ban is unchanged"


def test_the_lookup_text_names_the_command_field_and_the_draft_file_limit() -> None:
    text = _norm(_LESSON_LOOKUP_RULES)
    assert "--meta lessons_used=<lesson ids or none>" in text
    assert "a draft-file reply cannot carry meta" in text


def test_the_cadence_sweep_prompt_has_no_lookup() -> None:
    assert "LESSONS LOOKUP" not in assemble_cadence_prompt({}, [])


# ------------------------------------------------------------------ the skills


@pytest.mark.parametrize("label,path,command", SKILL_FILES, ids=[s[0] for s in SKILL_FILES])
def test_each_listen_skill_has_the_bounded_lookup_section(label: str, path: Path, command: str) -> None:
    section = _section(path)
    text = _norm(section)
    assert command in text
    for phrase in ("ONE bounded lookup", "literal substring matching", "One reformulation is allowed",
                   "Do not repeat onboarding every turn", "Skip it for trivial turns", "It is read-only",
                   "is not an inbox command", "Results are advisory", "Cite only a lesson that changed",
                   "**nothing found**", "**lookup failed**", "never report a failure as \"found nothing\"",
                   "**read but not useful**", "A failed lookup never blocks the task and adds no new gate"):
        assert phrase in text, f"{label}: {phrase}"
    assert len(section.splitlines()) < 40


@pytest.mark.parametrize("label,path,command", SKILL_FILES, ids=[s[0] for s in SKILL_FILES])
def test_the_lookup_section_teaches_no_inbox_command_and_stays_project_agnostic(
        label: str, path: Path, command: str) -> None:
    section = _section(path)
    assert INBOX.search(_norm(section)) is None, "no inbox command anywhere in the section"
    fenced = re.findall(r"```[a-z]*\n(.*?)```", section, re.DOTALL)
    assert len(fenced) == 1 and INBOX.search(fenced[0]) is None
    for word in ("estate", "clodex", "amperian"):
        assert word not in section.lower()


@pytest.mark.parametrize("path,command", [
    (SKILLS_ROOT / "claude" / "agenttalk.lead.md", "agenttalk knowledge search <term> --type lesson --limit 5"),
    (SKILLS_ROOT / "codex" / "agenttalk-lead" / "SKILL.md",
     "python -m agenttalk knowledge search <term> --type lesson --limit 5"),
], ids=["claude", "codex"])
def test_the_lead_skill_asks_the_brief_for_one_search_term(path: Path, command: str) -> None:
    text = _norm(path.read_text(encoding="utf-8"))
    assert "name one concrete subsystem or failure term the worker can use for its one bounded lessons lookup" in text
    assert command in text
    assert "do not paste lessons into the brief" in text


# ------------------------------------------------------------------ the command the text teaches really exists


def test_the_taught_command_runs_against_a_throwaway_store(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    store = Store(tmp_path)
    store.init(["dev"])
    root = ["--root", str(tmp_path)]
    assert cli.main([*root, "knowledge", "publish", "--from", "dev", "--type", "lesson", "--key", "junction-trap",
                     "--scope", "ops", "--trigger", "when a junction is resolved", "-m", "A junction hides its target.",
                     "--applies-to", "paths", "--evidence-ref", "t-1",
                     "--review-after", "2099-01-01", "--expires-at", "2099-06-01"]) == 0
    capsys.readouterr()
    assert cli.main([*root, "knowledge", "search", "junction", "--type", "lesson", "--limit", "5",
                     "--include-uncurated"]) == 0
    assert "junction-trap" in capsys.readouterr().out
    assert cli.main([*root, "knowledge", "search", "no-such-word", "--type", "lesson", "--limit", "5"]) == 0
    assert "junction-trap" not in capsys.readouterr().out
