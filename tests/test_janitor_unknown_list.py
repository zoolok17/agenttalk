"""#901: a list of registered checkouts that Git could not give is UNKNOWN, never zero.

The janitor's report, its sweep (`--apply`) and doctor's scratch-hygiene check read one strict list of the registered
checkouts (`git worktree list --porcelain -z`). When it cannot be had or read reliably, the report says "unknown (why)"
and what to do, the sweep removes, unlinks and prunes nothing (links and an empty sweep included) and fails, the count
after a prune says unknown, and doctor warns with a JSON `null` for its outside-checkout count.

Every test runs in a throw-away repository inside `tmp_path`; Git's list is made to fail with a stand-in for the
function that runs Git. Tests that already passed before #901 (what #858 had done) are marked GUARD.
"""

# ruff: noqa: F811  (the imported `estate` fixture is named as an argument by each test)
import dataclasses
import json
import os
import subprocess

import pytest

from agenttalk import doctor, janitor
from agenttalk.cli import main

from test_janitor_active_lanes import checkout, sections
from test_worktree_release import directory_link, estate, git  # noqa: F401  (estate is a fixture)

LIST = ("worktree", "list", "--porcelain")


def stand_in(monkeypatch, answer):
    """Answer Git's list of checkouts through `answer(call_number, args, state)` (None: ask the real Git). Returns the
    calls made (their args) and a state dict that says whether `worktree prune` has run."""
    real, calls, state = janitor._run_git_checked, [], {"pruned": False}

    def run_git(repo, *args, **kwargs):
        if args[:2] == ("worktree", "prune"):
            state["pruned"] = True
        if args[:3] == LIST:
            calls.append(args)
            reply = answer(len(calls), args, state)
            if reply is not None:
                if not kwargs.get("raw"):  # the text reader asks for text
                    reply = (reply[0], reply[1].decode("utf-8", "replace"), reply[2].decode("utf-8", "replace"))
                return reply
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(janitor, "_run_git_checked", run_git)
    return calls, state


def shapes(repo):
    main_checkout = b"worktree " + str(repo).encode("utf-8")
    return {
        "git-missing": ((None, b"", b"git is not resolvable"), "git is not resolvable"),
        "nonzero-with-partial-output": (
            (1, main_checkout + b"\0HEAD abc\0\0", b"fatal: detected dubious ownership in repository at 'x'\n"),
            "dubious ownership",
        ),
        "truncated": ((0, main_checkout + b"\0HEAD abc\0", b""), "not complete"),
        "malformed": ((0, b"garbage\0\0", b""), "could not be read reliably"),
        "invalid-utf8": ((0, b"worktree /x/\xff\xfe\0\0", b""), "not valid UTF-8"),
        "empty-success": ((0, b"", b""), "not complete"),  # success with no output at all is not a list either
        "names-no-checkout": ((0, b"\0\0", b""), "names no checkout"),
    }


SHAPE_NAMES = list(shapes("x"))


def a_plain_candidate(repo, name=".probe-plain"):
    folder = repo / name
    folder.mkdir()
    (folder / "note.txt").write_text("kept")
    return folder


def fails_first(reply=(1, b"", b"fatal: the list failed (test)")):
    return lambda n, args, state: reply if n == 1 else None


# ---------------------------------------------------------------------------------- the report says unknown, not zero


@pytest.mark.parametrize("shape", SHAPE_NAMES)
def test_the_report_says_unknown_with_the_reason_and_no_count_when_the_list_fails(estate, monkeypatch, shape):
    repo, wt, cfg = estate
    candidate = a_plain_candidate(repo)
    reply, why = shapes(repo)[shape]
    calls, _ = stand_in(monkeypatch, lambda n, args, state: reply)

    report = janitor.build_report(cfg)
    text = janitor.format_report(report, cfg, apply=False)

    assert report.registered_worktrees is None, text
    (unknown,) = [line for line in text.splitlines() if "registered worktrees: unknown (" in line]
    assert why in unknown, unknown
    assert "dirty registered worktrees: unknown" in text, text
    assert "registered worktrees: 0" not in text and "worktrees: 0" not in text, text
    assert "may be wrong" not in text, text  # the old best-effort count is gone
    assert any(c.path == candidate for c in report.candidates) and str(candidate) in text, text  # still shown
    assert calls and all(args[-1] == "-z" for args in calls), calls  # no fallback to the text reader
    assert len(calls) == 1, calls  # and no second reading: a failed list is not retried


def test_git_that_cannot_be_run_is_an_unknown_list_too(estate, monkeypatch):
    repo, wt, cfg = estate

    def cannot_run(n, args, state):
        raise PermissionError("access is denied (test)")

    stand_in(monkeypatch, cannot_run)

    report = janitor.build_report(cfg)
    text = janitor.format_report(report, cfg, apply=False)

    assert report.registered_worktrees is None, text
    assert "registered worktrees: unknown (git could not be run" in text and "access is denied" in text, text


def test_the_report_tells_what_to_do_and_names_the_strict_form(estate, monkeypatch):
    repo, wt, cfg = estate
    reply, _ = shapes(repo)["nonzero-with-partial-output"]
    stand_in(monkeypatch, lambda n, args, state: reply)

    text = janitor.format_report(janitor.build_report(cfg), cfg, apply=False)

    assert "git worktree list --porcelain -z" in text, text
    assert "safe.directory" in text and "Git 2.36" in text, text
    assert "before the delete" not in text, text  # a report deletes nothing


def test_an_old_git_that_rejects_the_nul_form_gets_upgrade_advice(estate, monkeypatch):
    repo, wt, cfg = estate
    old = (129, b"", b"error: unknown switch `z'\nusage: git worktree list [-v | --porcelain] [--expire <expire>]\n")
    stand_in(monkeypatch, lambda n, args, state: old)

    text = janitor.format_report(janitor.build_report(cfg), cfg, apply=False)

    assert "upgrade Git" in text and "2.36" in text, text


@pytest.mark.parametrize("shape", ["git-missing", "nonzero-with-partial-output", "truncated"])
def test_GUARD_an_unrelated_failure_is_not_blamed_on_the_git_version(estate, monkeypatch, shape):
    repo, wt, cfg = estate
    reply, _ = shapes(repo)[shape]
    stand_in(monkeypatch, lambda n, args, state: reply)

    text = janitor.format_report(janitor.build_report(cfg), cfg, apply=False)

    assert "upgrade Git" not in text, text


NASTY_STDERR = (
    b"fatal: boom\n\x1b[31mred\x1b[0m\r\nFAILED (1) - forged heading:\n  C:\\forged: rm -rf it\xe2\x80\xa8more\x00\x07 "
    + b"x" * 400
)


@pytest.mark.parametrize("apply", [False, True], ids=["report", "apply"])
def test_the_reason_is_one_sanitized_line_whatever_git_wrote(estate, monkeypatch, apply):
    """A control character or a line break in Git's message must not start a line of its own or hide a character."""
    repo, wt, cfg = estate
    a_plain_candidate(repo)
    stand_in(monkeypatch, lambda n, args, state: (1, b"", NASTY_STDERR))

    report = janitor.build_report(cfg)
    text = janitor.apply(cfg, report) if apply else janitor.format_report(report, cfg, apply=False)

    assert "fatal: boom" in text, text
    for bad in ("\x1b", "\r", "\u2028", "\x00", "\x07"):
        assert bad not in text, (bad, text)
    assert not any(line.startswith(("FAILED (1) - forged", "  C:\\forged")) for line in text.splitlines()), text
    assert "x" * 250 not in text, text  # Git's message is cut, not shown in full (400 characters here)


def test_the_early_refusal_before_discovery_says_not_checked_not_zero(estate, monkeypatch):
    repo, wt, cfg = estate
    calls, _ = stand_in(monkeypatch, lambda n, args, state: None)
    monkeypatch.setattr(janitor, "_ancestor_ids", lambda _cfg: ({}, [(repo, "the folder is a link (test)")]))

    report = janitor.build_report(cfg)
    text = janitor.format_report(report, cfg, apply=False)

    assert report.registered_worktrees is None, text
    assert "registered worktrees: not checked" in text and "dirty registered worktrees: not checked" in text, text
    assert "registered worktrees: 0" not in text and not calls, (text, calls)


def test_GUARD_a_known_list_prints_its_counts(estate):
    repo, wt, cfg = estate
    checkout(repo, "one")

    report = janitor.build_report(cfg)
    text = janitor.format_report(report, cfg, apply=False)

    assert report.registered_worktrees is not None and "registered worktrees: 3" in text, text
    assert "dirty registered worktrees: 0" in text and "unknown" not in text, text


# --------------------------------------------------------------------- the sweep removes nothing on an unknown list


def a_stale_link(repo, tmp_path):
    target = tmp_path / "linked-target"
    target.mkdir()
    (target / "precious.txt").write_text("only copy")
    link = repo / ".probe-link"
    directory_link(link, target)
    return link, target


def test_GUARD_a_link_is_unlinked_when_the_list_is_known(estate, tmp_path):
    repo, wt, cfg = estate
    link, target = a_stale_link(repo, tmp_path)

    report = janitor.build_report(cfg)
    text = janitor.apply(cfg, report)

    assert not os.path.lexists(link) and (target / "precious.txt").read_text() == "only copy", text
    assert not report.apply_failed, text


@pytest.mark.parametrize("shape", ["git-missing", "nonzero-with-partial-output", "truncated"])
def test_a_link_is_kept_untouched_when_the_list_is_unknown(estate, tmp_path, monkeypatch, shape):
    repo, wt, cfg = estate
    link, target = a_stale_link(repo, tmp_path)
    reply, _ = shapes(repo)[shape]
    stand_in(monkeypatch, fails_first(reply))

    report = janitor.build_report(cfg)
    assert any(c.path == link and c.link for c in report.candidates)  # it was found: discovery does not read the list
    text = janitor.apply(cfg, report)

    assert os.path.lexists(link) and (target / "precious.txt").read_text() == "only copy", text
    assert report.apply_failed and "removed: 1" not in text, text
    assert "before the delete" not in text, text  # nothing was being deleted: the first list was what failed
    found = sections(text)
    (heading,) = [h for h in found if "do not remove them by hand" in h]
    assert any(".probe-link" in line and "run the sweep again" in line for line in found[heading]), text


def test_a_sweep_with_no_candidates_fails_and_does_not_prune_when_the_list_is_unknown(estate, monkeypatch):
    repo, wt, cfg = estate
    stand_in(monkeypatch, fails_first())
    pruned = []
    real = janitor._run_git
    monkeypatch.setattr(janitor, "_run_git", lambda r, *a: pruned.append(a) or real(r, *a))

    report = janitor.build_report(cfg)
    assert report.registered_worktrees is None and not report.candidates
    text = janitor.apply(cfg, report)

    assert report.apply_failed, text
    assert ("worktree", "prune") not in [a[:2] for a in pruned], pruned
    assert "registered worktrees after prune" not in text and "worktree prune skipped" in text, text
    assert "git worktree list --porcelain -z" in text, text


def test_the_command_exits_with_failure_for_an_empty_sweep_on_an_unknown_list(estate, monkeypatch, capsys):
    repo, wt, cfg = estate
    monkeypatch.delenv(
        "AGENTTALK_STORE_FENCE"
    )  # the test run's own fence would refuse first: this is the unfenced path
    calls, state = stand_in(monkeypatch, lambda n, args, st: (1, b"", b"fatal: the list failed (test)"))

    code = main(["--root", str(repo), "janitor", "--apply"])

    out = capsys.readouterr().out
    assert code == 1 and "unknown" in out and not state["pruned"], out
    assert len(calls) == 1, calls  # one reading for the whole command: no retry, no second listing


def test_GUARD_a_sweep_with_no_candidates_and_a_known_list_still_prunes_and_succeeds(estate, monkeypatch, capsys):
    repo, wt, cfg = estate
    monkeypatch.delenv("AGENTTALK_STORE_FENCE")
    calls, state = stand_in(monkeypatch, lambda n, args, st: None)

    code = main(["--root", str(repo), "janitor", "--apply"])

    out = capsys.readouterr().out
    assert code == 0 and state["pruned"] and "registered worktrees after prune: 2" in out, out


# ------------------------------------------------------------------------ the count after the prune is read strictly


def test_a_failed_listing_after_the_prune_says_unknown_and_fails_the_sweep(estate, monkeypatch):
    repo, wt, cfg = estate
    gone = a_plain_candidate(repo, ".probe-gone")
    calls, state = stand_in(
        monkeypatch, lambda n, args, st: (1, b"", b"fatal: gone wrong (test)") if st["pruned"] else None
    )

    report = janitor.build_report(cfg)
    text = janitor.apply(cfg, report)

    assert not gone.exists() and state["pruned"], text  # the sweep itself ran: only the last reading failed
    assert "registered worktrees after prune: unknown (" in text and "gone wrong" in text, text
    assert "after prune: 0" not in text and report.apply_failed, text


def test_GUARD_a_failed_final_listing_keeps_the_candidate(estate, monkeypatch):
    """#858: the re-listing made at the delete is separate from the first one; when it fails the folder is kept."""
    repo, wt, cfg = estate
    kept = a_plain_candidate(repo, ".probe-kept")
    stand_in(monkeypatch, lambda n, args, st: (1, b"", b"fatal: second reading failed (test)") if n == 2 else None)

    report = janitor.build_report(cfg)
    text = janitor.apply(cfg, report)

    assert (kept / "note.txt").read_text() == "kept" and report.apply_failed, text


# --------------------------------------------------------------------------------------------------------- doctor


def hygiene(repo):
    return doctor._check_scratch_hygiene(repo)


def test_doctor_warns_when_the_list_is_unknown_even_with_nothing_else_to_say(estate, monkeypatch):
    repo, wt, cfg = estate
    assert hygiene(repo) is None  # the control: a known list and nothing outside the scratch root: no check at all
    reply, _ = shapes(repo)["nonzero-with-partial-output"]
    stand_in(monkeypatch, lambda n, args, state: reply)

    check = hygiene(repo)

    assert check is not None and check.name == "scratch_hygiene" and check.status == "warn"
    assert "could not be checked" in check.details and "dubious ownership" in check.details, check
    assert "git worktree list --porcelain -z" in check.fix and "safe.directory" in check.fix, check


def test_doctor_reports_a_null_count_and_keeps_its_other_keys(estate, monkeypatch):
    repo, wt, cfg = estate
    checkout(repo, "outside")  # registered under the repository, not under the scratch root
    known = hygiene(repo)
    assert known.data["outside_worktrees"] == 1  # GUARD: the known count is a number
    reply, _ = shapes(repo)["truncated"]
    stand_in(monkeypatch, lambda n, args, state: reply)

    unknown = hygiene(repo)

    assert unknown.data["outside_worktrees"] is None
    assert set(unknown.data) == set(known.data) == {"outside_worktrees", "outside_candidates", "scratch_root"}
    assert '"outside_worktrees": null' in json.dumps(dataclasses.asdict(unknown))


def test_doctor_keeps_its_other_warnings_beside_an_unknown_list(estate, monkeypatch):
    repo, wt, cfg = estate
    a_plain_candidate(repo)
    reply, _ = shapes(repo)["git-missing"]
    stand_in(monkeypatch, lambda n, args, state: reply)

    check = hygiene(repo)

    assert "could not be checked" in check.details, check
    assert "scratch-family candidate(s) outside the scratch root" in check.details, check
    assert check.data["outside_candidates"] == 1


def test_doctor_reads_the_checkouts_once_and_with_the_strict_reader(estate, monkeypatch):
    repo, wt, cfg = estate
    calls, _ = stand_in(monkeypatch, lambda n, args, state: None)

    hygiene(repo)

    assert len(calls) == 1 and calls[0][-1] == "-z", calls  # one list for discovery and the outside-checkout count


def test_GUARD_doctor_still_counts_a_known_outside_checkout_and_candidates(estate):
    repo, wt, cfg = estate
    checkout(repo, "outside")
    a_plain_candidate(repo)

    check = hygiene(repo)

    assert check.status == "warn" and "1 registered worktree(s) outside the scratch root" in check.details
    assert "scratch-family candidate(s) outside the scratch root" in check.details
    assert check.data["outside_worktrees"] == 1 and check.data["outside_candidates"] >= 1


def test_doctor_on_a_folder_that_is_not_a_repository_says_the_checkouts_could_not_be_checked(tmp_path):
    """Not a repository: Git has no list to give, so the list is unknown, and that is warned about (it used to
    read as 'no checkouts' and say nothing)."""
    proj = tmp_path / "plain-project"
    (proj / ".agenttalk").mkdir(parents=True)
    (proj / ".agenttalk" / "config.json").write_text(json.dumps({"scratch": {"tmp_root": str(tmp_path / "t")}}))
    (tmp_path / "t").mkdir()

    check = hygiene(proj)

    assert check is not None and check.status == "warn" and "could not be checked" in check.details, check
    assert check.data["outside_worktrees"] is None


BAD_CHARACTERS = ("\x1b", "\r", "\n", "\u2028", "\x00", "\x07")


def test_the_reason_itself_is_one_line_whoever_prints_it(estate, monkeypatch):
    """The report escapes every line it prints, but the reason is also handed to other readers (doctor, the apply
    summary): it is one short line with control characters written out before it goes anywhere."""
    repo, wt, cfg = estate
    stand_in(monkeypatch, lambda n, args, state: (1, b"", NASTY_STDERR))

    paths, why = janitor._strict_listing(repo)

    assert paths is None and "fatal: boom" in why
    assert not any(bad in why for bad in BAD_CHARACTERS), why
    assert "x" * 250 not in why and len(why) < 400, why


def test_doctor_shows_gits_message_on_one_sanitized_line(estate, monkeypatch):
    repo, wt, cfg = estate
    stand_in(monkeypatch, lambda n, args, state: (1, b"", NASTY_STDERR))

    check = hygiene(repo)

    for text in (check.details, check.fix):
        assert not any(bad in text for bad in BAD_CHARACTERS), text
    assert "fatal: boom" in check.details


def test_doctor_under_a_fence_says_unknown_when_the_fence_refuses_a_registered_checkout(
    tmp_path, monkeypatch, capsys
):
    """Under a store fence, Git's registration files are fenced before Git lists the checkouts: a registration of a
    folder outside the fence is refused (the fence's own error). Doctor's scratch check used to let that error out of
    the check; now the list is UNKNOWN with the fence's reason, like any list that could not be had, and the rest of
    doctor runs as before. The whole doctor run is checked too: the check is a warning, which doctor counts as exit 0
    unless another check is an error (exit 2)."""
    monkeypatch.delenv("AGENTTALK_ROOT", raising=False)
    fence = (tmp_path / "fence").resolve()
    repo = fence / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-b", "main")
    git(repo, "config", "maintenance.auto", "false")  # Git's upkeep after a commit would prune the registration below
    git(repo, "config", "user.name", "Test Author")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    for folder in ("scratch", "temp"):
        (fence / folder).mkdir()
    (repo / ".agenttalk").mkdir()
    (repo / ".agenttalk" / "config.json").write_text(json.dumps({
        "scratch": {"root": str(fence / "scratch"), "tmp_root": str(fence / "temp")}}))
    outside = (tmp_path / "zz-outside-registered").resolve()  # beside the fence, inside this test's own folder
    outside.mkdir()
    admin = repo / ".git" / "worktrees" / "late-registration"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(f"{outside / '.git'}\n", encoding="utf-8")
    (admin / "HEAD").write_text("ref: refs/heads/late-registration\n", encoding="utf-8")
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    # A commit that waits for its upkeep: on macOS it runs in the background and can land at any moment (CI, #901).
    git(repo, "-c", "maintenance.autoDetach=false", "-c", "gc.autoDetach=false", "commit", "--allow-empty", "-m", "x")
    assert (admin / "gitdir").is_file(), "Git's automatic upkeep removed the registration"
    monkeypatch.setenv("AGENTTALK_STORE_FENCE", str(fence))
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(tmp_path / "fence-report.txt"))

    check = hygiene(repo)

    assert check is not None and check.status == "warn", check
    assert "could not be checked" in check.details and "store fence refused" in check.details, check.details
    assert str(outside) in check.details, check.details
    assert check.data["outside_worktrees"] is None
    assert "registration" in check.fix and "safe.directory" not in check.fix, check.fix
    assert list(outside.iterdir()) == []
    record = []  # which way doctor's own scratch check went, shown only when the check is missing (macOS CI)

    def spy(owner, name, real):
        def wrapper(*args, **kwargs):
            try:
                out = real(*args, **kwargs)
            except BaseException as exc:
                record.append([name, "raised", f"{type(exc).__name__}: {exc}"])
                raise
            record.append([name, "returned", repr(out)])
            return out

        monkeypatch.setattr(owner, name, wrapper)

    for owner, name in ((janitor, "_strict_listing"), (janitor, "find_candidates"), (doctor, "_check_scratch_hygiene")):
        spy(owner, name, getattr(owner, name))

    rc = main(["--root", str(repo), "doctor", "--json"])

    printed = capsys.readouterr()
    report = json.loads(printed.out) if printed.out.strip() else {"no report; stderr": printed.err}
    scratch = [c for c in report.get("checks", []) if c["name"] == "scratch_hygiene"]
    if len(scratch) != 1:  # pytest.fail: outside CI pytest cuts an assertion message to 8 lines
        pytest.fail(json.dumps({"record": record, "registration still there": admin.is_dir(), "report": report},
                               sort_keys=True, indent=2))
    (scratch,) = scratch
    assert scratch["status"] == "warn" and scratch["data"]["outside_worktrees"] is None, scratch
    assert "store fence refused" in scratch["details"], scratch  # the fence's reason, not some other failure
    assert rc == (2 if any(c["status"] == "error" for c in report["checks"]) else 0), report


@pytest.mark.parametrize("step", ["config", "listing", "discovery"])
def test_doctor_reports_a_scratch_check_it_could_not_finish_instead_of_leaving_it_out(estate, monkeypatch, step):
    """A failure to read a folder (a disk that times out, a folder refused) anywhere in the scratch check used to
    leave the check out of doctor without a word, so a list in doubt read as nothing to report. Now it is a warning
    that names the failure, with the counts unknown."""
    repo, _wt, _cfg = estate
    owner, name = {"config": (janitor.JanitorConfig, "load"), "listing": (janitor, "_strict_listing"),
                   "discovery": (janitor, "find_candidates")}[step]

    def fail(*_args, **_kwargs):
        raise TimeoutError("the disk did not answer (test)")

    monkeypatch.setattr(owner, name, fail)

    check = hygiene(repo)

    assert check is not None and check.name == "scratch_hygiene" and check.status == "warn", check
    assert "could not be checked" in check.details and "the disk did not answer (test)" in check.details, check
    assert check.data["outside_worktrees"] is None and check.data["outside_candidates"] is None, check.data
    assert "named above" in check.fix, check.fix
    assert [c.status for c in doctor.run(repo).checks if c.name == "scratch_hygiene"] == ["warn"]


# --- round 2: a Git timeout, or any other failure to run Git that is not an OSError, is an unknown list ----------

NOT_AN_OSERROR = {
    "timeout": (lambda: subprocess.TimeoutExpired(["git", "worktree", "list"], 30), "timed out"),
    "subprocess-error": (
        lambda: subprocess.SubprocessError("git went wrong (test)\nsecond line"), "git went wrong (test)"
    ),
    "subprocess-error-without-words": (lambda: subprocess.SubprocessError(), "SubprocessError"),
}


def raises(kind, *, after_prune=False):
    """An answer for `stand_in`: Git's list raises this exception (only once `worktree prune` has run, if asked)."""
    make, _ = NOT_AN_OSERROR[kind]

    def answer(n, args, state):
        if after_prune and not state["pruned"]:
            return None
        raise make()

    return answer


@pytest.mark.parametrize("kind", list(NOT_AN_OSERROR))
def test_git_that_times_out_or_cannot_be_run_in_any_way_is_an_unknown_list_in_the_report(estate, monkeypatch, kind):
    repo, wt, cfg = estate
    stand_in(monkeypatch, raises(kind))

    report = janitor.build_report(cfg)
    text = janitor.format_report(report, cfg, apply=False)

    assert report.registered_worktrees is None, text  # no AttributeError out of the handler: the answer is "unknown"
    assert "registered worktrees: unknown (git could not be run" in text and NOT_AN_OSERROR[kind][1] in text, text
    line = next(row for row in text.splitlines() if "registered worktrees: unknown" in row)
    assert NOT_AN_OSERROR[kind][1] in line  # the whole reason is on that one line, a line break written out
    assert "registered worktrees: 0" not in text and "git worktree list --porcelain -z" in text, text


@pytest.mark.parametrize("kind", list(NOT_AN_OSERROR))
def test_doctor_warns_unknown_when_git_times_out_or_cannot_be_run_in_any_way(estate, monkeypatch, kind):
    repo, wt, cfg = estate
    stand_in(monkeypatch, raises(kind))

    check = hygiene(repo)

    assert check is not None and check.status == "warn" and "could not be checked" in check.details, check
    assert NOT_AN_OSERROR[kind][1] in check.details, check.details
    assert check.data["outside_worktrees"] is None
    for text in (check.details, check.fix):
        assert not any(bad in text for bad in BAD_CHARACTERS), text


@pytest.mark.parametrize("kind", list(NOT_AN_OSERROR))
def test_the_count_after_the_prune_says_unknown_when_git_times_out_or_cannot_be_run_in_any_way(
    estate, monkeypatch, kind
):
    repo, wt, cfg = estate
    gone = a_plain_candidate(repo, ".probe-gone")
    calls, state = stand_in(monkeypatch, raises(kind, after_prune=True))

    report = janitor.build_report(cfg)
    text = janitor.apply(cfg, report)

    assert not gone.exists() and state["pruned"], text  # the sweep itself ran: only the last reading failed
    assert "registered worktrees after prune: unknown (git could not be run" in text, text
    assert NOT_AN_OSERROR[kind][1] in text and "after prune: 0" not in text and report.apply_failed, text


def test_a_timeout_at_the_final_listing_keeps_the_candidate_and_says_why(estate, monkeypatch):
    repo, wt, cfg = estate
    kept = a_plain_candidate(repo, ".probe-kept")
    make, _ = NOT_AN_OSERROR["timeout"]

    def second_reading_times_out(n, args, state):
        if n == 2:
            raise make()

    stand_in(monkeypatch, second_reading_times_out)

    report = janitor.build_report(cfg)
    text = janitor.apply(cfg, report)

    assert kept.exists(), text
    assert "timed out" in text and report.apply_failed, text


def test_the_text_of_a_caught_failure_comes_from_the_exception_that_has_it():
    """`_os_error_text` reads `strerror`, which only an OSError has: any other failure is worded by its own text, or by
    its type's name when it has none, and an OSError keeps the system's words without its file names."""
    assert janitor._exception_text(PermissionError(13, "access is denied (test)", "C:/some/file")) == (
        "access is denied (test)"
    )
    assert "timed out after 30" in janitor._exception_text(subprocess.TimeoutExpired(["git"], 30))
    assert janitor._exception_text(subprocess.SubprocessError()) == "SubprocessError"
    assert janitor._exception_text(ValueError("odd (test)")) == "odd (test)"


# --- round 2: the exit codes of the report, as the docs state them -------------------------------------------


def test_the_report_exits_0_for_a_useful_partial_report(estate, monkeypatch, capsys):
    """Report mode: an unknown list of checkouts is said plainly, and the command still exits 0 when the report is
    useful (the candidates it can safely find are listed) and no scan root could not be read."""
    repo, wt, cfg = estate
    monkeypatch.delenv("AGENTTALK_STORE_FENCE")  # the test run's own fence would refuse first: the unfenced path
    candidate = a_plain_candidate(repo, ".probe-listed")
    calls, state = stand_in(monkeypatch, lambda n, args, st: (1, b"", b"fatal: the list failed (test)"))

    code = main(["--root", str(repo), "janitor"])

    out = capsys.readouterr().out
    assert code == 0 and "registered worktrees: unknown" in out and str(candidate) in out, out
    assert not state["pruned"] and candidate.exists()  # a report removes nothing


def test_the_report_exits_1_when_a_scan_root_cannot_be_read(estate, monkeypatch, capsys):
    repo, wt, cfg = estate
    monkeypatch.delenv("AGENTTALK_STORE_FENCE")
    monkeypatch.setattr(
        janitor, "_ancestor_ids", lambda c: ({}, [(c.tmp_root, "could not check ancestor: denied (test)")])
    )

    code = main(["--root", str(repo), "janitor"])

    out = capsys.readouterr().out
    assert code == 1 and "denied (test)" in out, out


def test_the_report_exits_2_when_the_fence_refuses_the_unknown_list(estate, tmp_path, monkeypatch, capsys):
    """Under AGENTTALK_STORE_FENCE nothing is listed, read or asked of Git outside the fence, and a list that cannot be
    had cannot be fenced: the command refuses (exit 2) instead of printing a partial report."""
    repo, wt, cfg = estate
    monkeypatch.setenv("AGENTTALK_STORE_FENCE_REPORT", str(tmp_path / "own-report.txt"))  # this test probes on purpose
    stand_in(monkeypatch, lambda n, args, st: (1, b"", b"fatal: the list failed (test)"))

    code = main(["--root", str(repo), "janitor"])

    out = capsys.readouterr()
    assert code == 2 and "registered worktrees: unknown" not in out.out, out
