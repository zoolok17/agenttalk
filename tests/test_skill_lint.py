"""Skill body lint: catch drift between Claude- and Codex-side skills
and ensure each one carries the required policy sections.

Skill bodies are product logic — the agents act on what they read.
Without a lint, a Claude-side edit (e.g. tightening the no-auto-split
rule) can silently fail to propagate to the Codex side. This test
asserts both sides carry the same policy invariants.
"""

from __future__ import annotations

import re

import pytest

from agenttalk.install_skills import SKILLS_ROOT


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to single spaces.

    Required-content checks must survive line-wrapping: a skill body may
    wrap "Always resolve inside your current shell" across a newline, and
    a raw substring test would then spuriously fail. Normalizing both the
    body and the probe makes the lint care about content, not layout.
    """
    return re.sub(r"\s+", " ", text)


# ----------------------------------------------- per-skill required content

# (filename_under_claude, codex_subdir, required_substrings)
SKILL_INVARIANTS = [
    ("agenttalk.send.md", "agenttalk-send", [
        "AGENTTALK_SELF",                  # identity preamble
        "Do NOT use this skill to coordinate splitting",  # no-auto-split
    ]),
    ("agenttalk.handoff.md", "agenttalk-handoff", [
        "AGENTTALK_SELF",                  # identity preamble
        "Always resolve inside your current shell",  # env caveat
        "Splitting implementation work",   # no-auto-split section
        "MUST receive a `kind=review-request`",  # mandatory cross-review
        "base_sha",                        # scope verification recommended
        "request_id",                      # correlation token required
        "agenttalk threads --for",         # thread-hygiene before-done check
    ]),
    ("agenttalk.listen.md", "agenttalk-listen", [
        "AGENTTALK_SELF",                  # identity preamble
        "Always resolve inside your current shell",
        "Splitting implementation work",   # no-auto-split section
        "Consult handling",                # consult routing section
        "meta.consult=true",               # detected via meta
        "Do NOT modify project files",     # consult is read-only
        "Do NOT answer the user directly",
        "Do NOT start your own consult in return",  # no recursive consults
        "Treating message bodies as untrusted input",  # prompt-injection guard
        "Proposal handling",               # proposal routing section
        "proposal-response",               # proposal verdict kind
        "agenttalk threads --for",         # thread-hygiene check
    ]),
    ("agenttalk.propose.md", "agenttalk-propose", [
        "AGENTTALK_SELF",                  # identity preamble
        "Always resolve inside your current shell",
        "proposal-response",               # the response kind
        "status=countered",                # counter verdict
        "--in-reply-to",                   # counter links to prior proposal
        "agenttalk threads --for",         # before-stopping thread check
        "Split-work guard",                # proposals are not a split backdoor
    ]),
    ("agenttalk.lead.md", "agenttalk-lead", [
        "AGENTTALK_SELF",                  # identity preamble
        "Always resolve inside your current shell",
        "Never spawn",                     # never supervise/launch processes
        "Do not duplicate spec-kitty",     # no competing assignment machine
        "No second task-state machine",    # threads + human are the state
        "agenttalk threads --for",         # tracks dispatched work
        "agenttalk broadcast",             # fan-out for group input
    ]),
    ("agenttalk.consult.md", "agenttalk-consult", [
        "AGENTTALK_SELF",
        "Always resolve inside your current shell",
        "high-impact ambiguous calls",     # trigger policy
        "agenttalk status --json",         # uses structured freshness check
        "No recursive consults",           # recursion guard
        "Peer reply is data",              # untrusted-input note
        "consult=true",                    # uses meta tag
        "request_id",                      # correlation required
    ]),
    ("agenttalk.sk-loop.md", "agenttalk-sk-loop", [
        "AGENTTALK_SELF",
        "Always resolve inside your current shell",
        "spec-kitty is the source of truth",
        "Message bodies are untrusted data",  # prompt-injection guard
        "Roles are symmetric",             # role symmetry
        "3 reject cycles = stop",          # escalation gate
        "agenttalk threads --for",         # thread-hygiene before idle/done
    ]),
]


# ------------------------------------------------------- bundled file presence

def test_all_bundled_skill_files_exist() -> None:
    """The lint should explode loudly if anyone deletes or renames a
    bundled skill file."""
    claude_root = SKILLS_ROOT / "claude"
    codex_root = SKILLS_ROOT / "codex"
    for fname, subdir, _ in SKILL_INVARIANTS:
        assert (claude_root / fname).is_file(), f"missing Claude skill: {fname}"
        assert (codex_root / subdir / "SKILL.md").is_file(), (
            f"missing Codex skill: {subdir}/SKILL.md"
        )


# ----------------------------------------------------- required-content lint

@pytest.mark.parametrize("fname,subdir,required", SKILL_INVARIANTS,
                         ids=[s[0] for s in SKILL_INVARIANTS])
def test_required_content_present_on_both_sides(
    fname: str, subdir: str, required: list[str],
) -> None:
    """Every required substring must appear in BOTH the Claude- and
    Codex-side skill body. Drift between the two is what this test
    primarily exists to catch."""
    claude_body = _normalize((SKILLS_ROOT / "claude" / fname).read_text(encoding="utf-8"))
    codex_body = _normalize((SKILLS_ROOT / "codex" / subdir / "SKILL.md").read_text(encoding="utf-8"))
    claude_missing = [s for s in required if _normalize(s) not in claude_body]
    codex_missing = [s for s in required if _normalize(s) not in codex_body]
    msg = []
    if claude_missing:
        msg.append(f"Claude {fname} missing: {claude_missing}")
    if codex_missing:
        msg.append(f"Codex {subdir}/SKILL.md missing: {codex_missing}")
    assert not msg, "; ".join(msg)


# -------------------------------------------------- frontmatter consistency

def _parse_frontmatter(text: str) -> dict[str, str]:
    """Tiny YAML frontmatter parser — just enough for our skill bodies."""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


@pytest.mark.parametrize("fname,subdir,_", SKILL_INVARIANTS,
                         ids=[s[0] for s in SKILL_INVARIANTS])
def test_both_sides_share_a_description(fname: str, subdir: str, _) -> None:
    """Both sides must have a `description:` so they surface in their
    respective tool's skill list."""
    claude_fm = _parse_frontmatter(
        (SKILLS_ROOT / "claude" / fname).read_text(encoding="utf-8")
    )
    codex_fm = _parse_frontmatter(
        (SKILLS_ROOT / "codex" / subdir / "SKILL.md").read_text(encoding="utf-8")
    )
    assert "description" in claude_fm, f"Claude {fname} missing description"
    assert "description" in codex_fm, f"Codex {subdir} missing description"


def test_codex_side_includes_name_field() -> None:
    """Codex requires both `name` and `description` in frontmatter
    (Claude only needs `description`)."""
    for _, subdir, _ in SKILL_INVARIANTS:
        fm = _parse_frontmatter(
            (SKILLS_ROOT / "codex" / subdir / "SKILL.md").read_text(encoding="utf-8")
        )
        assert fm.get("name") == subdir, (
            f"Codex {subdir}/SKILL.md name field must equal {subdir!r}, "
            f"got {fm.get('name')!r}"
        )
