from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk import cli, wrapper_logs
from agenttalk import supervisor as sup
from agenttalk import wrapper_runtime as runtime
from agenttalk.store import Store


NOW = 1_800_000_000.0


def _log_glob(root: Path, pattern: str) -> list[Path]:
    """Same as sorted(root.glob(pattern)), excluding the round-29 tail-ring
    cursor sibling (base_path.name + '.cursor') - a bare 'stdout.log*' or
    'stderr.log.*' glob sweeps it in too since it lives right alongside
    the numbered tail segments, but it holds a bare index, not log
    content, and is not a segment any of these tests mean to count."""
    return sorted(p for p in root.glob(pattern) if not p.name.endswith(".cursor"))


def _direct_wrap_environment(tmp_path: Path, blocked_state: Path) -> dict[str, str]:
    home = tmp_path / "home"
    temp = tmp_path / "temp"
    home.mkdir()
    temp.mkdir()
    env = os.environ.copy()
    for name in (
        wrapper_logs.ENV_STDOUT_PATH,
        wrapper_logs.ENV_STDERR_PATH,
        wrapper_logs.ENV_MAX_BYTES,
        wrapper_logs.ENV_SEGMENT_COUNT,
        wrapper_logs.ENV_LAUNCH_NONCE,
    ):
        env.pop(name, None)
    env.update(
        {
            "LOCALAPPDATA": str(blocked_state),
            "XDG_STATE_HOME": str(blocked_state),
            "USERPROFILE": str(home),
            "HOME": str(home),
            "TEMP": str(temp),
            "TMP": str(temp),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }
    )
    return env


def _run_direct_wrapper(project: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**env, "AGENTTALK_STUB_SCENARIO": "compute_no_reply"}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agenttalk",
            "--root",
            str(project),
            "wrap",
            "--for",
            "worker",
            "--cli",
            "claude",
            "--",
            sys.executable,
            str(Path(__file__).resolve().parent / "support" / "stub_cli.py"),
        ],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def _captured_stderr_logs(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.rglob("stderr.log"))


def _committed_generation_pool(
    root: Path,
    *,
    newest_sequence: str | None,
) -> tuple[str, list[Path], Path]:
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    generations: list[Path] = []
    for sequence in range(1, wrapper_logs.WRAPPER_LOG_GENERATIONS + 2):
        generation = (
            root
            / agent_leaf
            / f"20260804T12000{sequence}000Z-{sequence:032x}"
        )
        generation.mkdir(parents=True)
        if sequence <= wrapper_logs.WRAPPER_LOG_GENERATIONS:
            (generation / ".sequence").write_text(str(sequence), encoding="utf-8")
        elif newest_sequence is not None:
            (generation / ".sequence").write_text(
                newest_sequence,
                encoding="utf-8",
            )
        (generation / ".committed").write_bytes(b"")
        (generation / "stdout.log").write_bytes(b"")
        (generation / "stderr.log").write_bytes(b"")
        generations.append(generation)
    return agent_leaf, generations, generations[-1]


def test_direct_wrap_launch_owns_bounded_log_capture(tmp_path: Path) -> None:
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    blocked_state = tmp_path / "blocked-state"
    blocked_state.write_text("not a directory", encoding="utf-8")
    env = _direct_wrap_environment(tmp_path, blocked_state)

    result = _run_direct_wrapper(project, env)

    assert result.returncode == 0, result.stderr
    assert "Plan: I would compute" in result.stdout
    # Read the wrapper's own recorded fact rather than guessing where the
    # fallback landed: a rejected preferred root can be superseded by a
    # fallback outside tmp_path (e.g. the OS temp directory), which a glob
    # confined to tmp_path would never find even though capture worked
    # correctly. See #113 - four rounds were spent tracing exactly this.
    location = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")
    assert location["status"] == "observed", (
        f"expected an observed wrapper log location, got {location!r}; "
        f"wrapper stderr was: {result.stderr!r}"
    )
    generation = Path(str(location["generation_dir"]))
    assert (generation / ".committed").exists()
    captured = "".join(
        path.read_text(encoding="utf-8")
        for path in _log_glob(generation, "stderr.log*")
    )
    assert '"event":"wrapper_exited"' in captured.replace(" ", "")
    captured_stdout = "".join(
        path.read_text(encoding="utf-8")
        for path in _log_glob(generation, "stdout.log*")
    )
    assert "Plan: I would compute" in captured_stdout


@pytest.mark.parametrize(
    ("failure_type", "mapped_exit"),
    [(OSError, 2), (RuntimeError, None)],
    ids=("mapped-os-error", "unexpected-runtime-error"),
)
def test_direct_wrap_captures_implicit_root_discovery_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
    mapped_exit: int | None,
) -> None:
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    state = tmp_path / "state"
    env = _direct_wrap_environment(tmp_path, state)
    env.pop("AGENTTALK_ROOT", None)
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    def fail_root_discovery() -> Path:
        raise failure_type("ROOT-DISCOVERY-SENTINEL")

    monkeypatch.setattr(cli, "find_root", fail_root_discovery)

    argv = [
        "wrap",
        "--for",
        "worker",
        "--cli",
        "claude",
        "--",
        sys.executable,
        "-c",
        "pass",
    ]
    if mapped_exit is None:
        with pytest.raises(failure_type, match="ROOT-DISCOVERY-SENTINEL"):
            cli.main(argv)
    else:
        assert cli.main(argv) == mapped_exit

    [stderr_log] = _captured_stderr_logs(tmp_path)
    generation = stderr_log.parent
    assert (generation / ".committed").is_file()
    captured = "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in _log_glob(generation, "stderr.log*")
    )
    assert "ROOT-DISCOVERY-SENTINEL" in captured
    location = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")
    assert location["status"] == "observed"
    assert Path(str(location["generation_dir"])) == generation.resolve()
    assert Path(str(location["stderr"])) == stderr_log.resolve()


def test_report_names_the_root_that_accepted_the_wrapper_generation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    blocked_state = tmp_path / "blocked-state"
    blocked_state.write_text("not a directory", encoding="utf-8")
    env = _direct_wrap_environment(tmp_path, blocked_state)
    preferred = wrapper_logs.default_wrapper_log_root(
        project,
        platform=os.name,
        environ=env,
    )

    result = _run_direct_wrapper(project, env)
    assert result.returncode == 0, result.stderr
    # Read the wrapper's own recorded fact rather than guessing where the
    # fallback landed via a glob confined to tmp_path: the preferred root is
    # deliberately blocked above, and the fallback that supersedes it is not
    # guaranteed to resolve under tmp_path (e.g. the OS temp directory can
    # sit alongside it, not inside it). The wrapper already names its own
    # accepted location as a fact - asserting against that fact instead of
    # an inferred directory is the property this test is actually named for.
    location = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")
    assert location["status"] == "observed", (
        f"expected an observed wrapper log location, got {location!r}; "
        f"wrapper stderr was: {result.stderr!r}"
    )
    actual_root = Path(str(location["root"]))
    actual_generation = Path(str(location["generation_dir"]))
    actual_stderr = Path(str(location["stderr"]))
    assert actual_generation.is_dir()
    assert (actual_generation / ".committed").exists()
    assert actual_root != preferred.resolve()

    # Make the preferred location viable before asking. A report that recomputes
    # candidates instead of reading the wrapper's recorded fact now lies.
    blocked_state.unlink()
    blocked_state.mkdir()
    report_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agenttalk",
            "--root",
            str(project),
            "supervise",
            "--report",
            "--for",
            "worker",
        ],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert report_result.returncode == 0, report_result.stderr
    report = json.loads(report_result.stdout)
    assert report["selected_agent"] == "worker"
    location = report["agents"]["worker"]["wrapper_log"]
    assert location["status"] == "observed"
    assert Path(location["root"]) == actual_root
    assert Path(location["generation_dir"]) == actual_generation
    assert Path(location["stderr"]) == actual_stderr


def test_record_location_still_attempts_write_when_config_check_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113 review, round 5 sweep, site #11: _record_wrapper_log_location
    used to skip writing the location record whenever
    `config.json.is_file()` read False - which it also reports for a
    config.json it could not confirm. Only a CONFIRMED absence of
    config.json means "not a real store"; an unusable read must still
    attempt the write (an actual write failure is already handled
    safely by the surrounding except clause)."""
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    config_path = project / ".agenttalk" / "config.json"
    assert config_path.is_file()

    _poison_lstat(monkeypatch, config_path)

    root = tmp_path / "logs"
    generation = root / wrapper_logs._wrapper_log_agent_leaf("worker") / f"20260804T120000000Z-{1:032x}"
    generation.mkdir(parents=True)
    stdout = generation / "stdout.log"
    stderr = generation / "stderr.log"
    stdout.write_text("out", encoding="utf-8")
    stderr.write_text("err", encoding="utf-8")

    wrapper_logs._record_wrapper_log_location(
        project,
        "worker",
        wrapper_logs.WrapperLogInstallation(
            True,
            confirmed=True,
            root=root,
            generation_dir=generation,
            stdout_path=stdout,
            stderr_path=stderr,
        ),
    )

    location_path = wrapper_logs._wrapper_log_location_path(store.state_dir, "worker")
    assert location_path.is_file(), (
        "the write was skipped because config.json's readability could not "
        "be confirmed, not because it was confirmed absent"
    )


def test_wrapper_log_location_uses_hashed_windows_safe_agent_files(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    observed: dict[str, Path] = {}

    for agent in ("worker", "worker.", "NUL"):
        root = tmp_path / f"logs-{len(observed)}"
        generation = (
            root
            / wrapper_logs._wrapper_log_agent_leaf(agent)
            / f"20260804T120000000Z-{len(observed):032x}"
        )
        generation.mkdir(parents=True)
        stdout = generation / "stdout.log"
        stderr = generation / "stderr.log"
        stdout.write_text("out", encoding="utf-8")
        stderr.write_text("err", encoding="utf-8")
        wrapper_logs._record_wrapper_log_location(
            project,
            agent,
            wrapper_logs.WrapperLogInstallation(
                True,
                confirmed=True,
                root=root,
                generation_dir=generation,
                stdout_path=stdout,
                stderr_path=stderr,
            ),
        )
        path = wrapper_logs._wrapper_log_location_path(store.state_dir, agent)
        observed[agent] = path
        assert path.name == f"{wrapper_logs._wrapper_log_agent_leaf(agent)}.json"
        assert wrapper_logs.read_wrapper_log_location(store.state_dir, agent)[
            "generation_dir"
        ] == str(generation.resolve())

    assert len(set(observed.values())) == 3
    assert not any(path.name in {"worker.json", "worker..json", "NUL.json"} for path in observed.values())


def test_unreadable_location_record_reports_unusable_not_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113 review, round 3: read_wrapper_log_location's top-level
    `if not path.exists(): return absent` flattened an unreadable record
    into a confident absence before its own downstream error handling
    ever got a chance to run. A caller (e.g. cli.py's retired-agent
    fallback) that treats "absent" as "nothing to report" would
    incorrectly refuse a lookup a readable filesystem would have
    answered."""
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    root = tmp_path / "logs"
    generation = root / wrapper_logs._wrapper_log_agent_leaf("worker") / f"20260804T120000000Z-{1:032x}"
    generation.mkdir(parents=True)
    stdout = generation / "stdout.log"
    stderr = generation / "stderr.log"
    stdout.write_text("out", encoding="utf-8")
    stderr.write_text("err", encoding="utf-8")
    wrapper_logs._record_wrapper_log_location(
        project,
        "worker",
        wrapper_logs.WrapperLogInstallation(
            True,
            confirmed=True,
            root=root,
            generation_dir=generation,
            stdout_path=stdout,
            stderr_path=stderr,
        ),
    )
    location_path = wrapper_logs._wrapper_log_location_path(store.state_dir, "worker")
    assert location_path.is_file()

    _poison_lstat(monkeypatch, location_path)

    result = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")

    assert result["status"] == "unusable"
    assert result["generation_dir"] is None


def test_read_denial_past_the_first_probe_reports_unusable_not_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113 review, round 5, finding #13: only the FIRST probe
    (read_wrapper_log_location's top-level presence check) classified an
    OSError as 'unusable'. A read denial or path-resolution failure
    occurring AFTER that - e.g. read_text() racing a deletion or
    permission change between the first probe and the actual read - fell
    through to the broad except clause and reported 'invalid', even
    though nothing about the record's CONTENT was malformed. The public
    contract this file's own docs promise ('unusable' means could not
    read, 'invalid' means malformed content) must hold at every OSError
    site in this function, not just the first."""
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    root = tmp_path / "logs"
    generation = root / wrapper_logs._wrapper_log_agent_leaf("worker") / f"20260804T120000000Z-{1:032x}"
    generation.mkdir(parents=True)
    stdout = generation / "stdout.log"
    stderr = generation / "stderr.log"
    stdout.write_text("out", encoding="utf-8")
    stderr.write_text("err", encoding="utf-8")
    wrapper_logs._record_wrapper_log_location(
        project,
        "worker",
        wrapper_logs.WrapperLogInstallation(
            True,
            confirmed=True,
            root=root,
            generation_dir=generation,
            stdout_path=stdout,
            stderr_path=stderr,
        ),
    )
    location_path = wrapper_logs._wrapper_log_location_path(store.state_dir, "worker")
    assert location_path.is_file()

    # Deliberately does NOT poison lstat/stat: the top-level presence
    # probe must succeed (this is the SECOND site, past that probe).
    real_read_text = Path.read_text

    def denied_read_text(self: Path, *args: object, **kwargs: object):
        if self == location_path:
            raise OSError("simulated read denial past the first probe")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied_read_text)

    result = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")

    assert result["status"] == "unusable"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction guard")
def test_junctioned_state_ancestor_reports_unusable_not_observed(
    tmp_path: Path,
) -> None:
    """#113 review, round 5, findings #1/#2/#6 (the ancestry half): a
    generation-relative marker correctly needs no ancestry validation
    (it lives inside a generation directory the RETENTION scan already
    validates via _scan_path) - but this location record does not live
    inside a pre-validated generation directory, it lives under
    state_dir, which nothing else in the read path validates. A
    junctioned ancestor anywhere under state_dir must not let this
    function confidently report content read through that redirect."""
    real_project = tmp_path / "project"
    real_project.mkdir()
    store = Store(real_project)
    store.init(["worker"])
    root = tmp_path / "logs"
    generation = root / wrapper_logs._wrapper_log_agent_leaf("worker") / f"20260804T120000000Z-{1:032x}"
    generation.mkdir(parents=True)
    stdout = generation / "stdout.log"
    stderr = generation / "stderr.log"
    stdout.write_text("out", encoding="utf-8")
    stderr.write_text("err", encoding="utf-8")
    wrapper_logs._record_wrapper_log_location(
        real_project,
        "worker",
        wrapper_logs.WrapperLogInstallation(
            True,
            confirmed=True,
            root=root,
            generation_dir=generation,
            stdout_path=stdout,
            stderr_path=stderr,
        ),
    )
    real_state_dir = real_project / ".agenttalk" / "state"
    assert (real_state_dir / "wrapper-log-locations").is_dir()

    junctioned_state_dir = tmp_path / "junctioned-state"
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None, "Windows PowerShell is required for junction coverage"
    junction_script = tmp_path / "create-junction.ps1"
    junction_script.write_text(
        "param([string]$Link, [string]$Target)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null\n",
        encoding="utf-8-sig",
    )
    creation = subprocess.run(
        [
            powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-File", str(junction_script),
            str(junctioned_state_dir), str(real_state_dir),
        ],
        text=True, capture_output=True, timeout=120, check=False,
    )
    assert creation.returncode == 0, creation.stderr
    assert (
        junctioned_state_dir.lstat().st_file_attributes
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
    # The junction genuinely resolves to real, valid content - proving a
    # rejection is about the redirect, not about the record being
    # unreadable through it.
    assert (junctioned_state_dir / "wrapper-log-locations").is_dir()

    result = wrapper_logs.read_wrapper_log_location(junctioned_state_dir, "worker")

    assert result["status"] == "unusable"


def test_location_status_rejects_a_directory_where_a_log_file_is_expected(
    tmp_path: Path,
) -> None:
    """#113 review, round 4: the observed/stale determination expects
    THREE different object kinds - generation must be a directory,
    stdout/stderr must be plain files - so it must classify each through
    the type-aware classifier for its own kind, not a presence-only
    marker probe. A directory sitting where stdout.log belongs must not
    read as "observed" - that status is what the manual and tutorial
    promise means the captured output is really there."""
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    root = tmp_path / "logs"
    generation = root / wrapper_logs._wrapper_log_agent_leaf("worker") / f"20260804T120000000Z-{1:032x}"
    generation.mkdir(parents=True)
    stdout = generation / "stdout.log"
    stderr = generation / "stderr.log"
    stdout.mkdir()  # a directory, not a log file
    stderr.write_text("err", encoding="utf-8")
    wrapper_logs._record_wrapper_log_location(
        project,
        "worker",
        wrapper_logs.WrapperLogInstallation(
            True,
            confirmed=True,
            root=root,
            generation_dir=generation,
            stdout_path=stdout,
            stderr_path=stderr,
        ),
    )

    result = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")

    # Round 5 correction: != "observed" would let a collapse to "stale"
    # pass, which is exactly the weak-assertion shape this whole review
    # keeps catching. Assert the exact status.
    assert result["status"] == "unusable"


_WRAPPER_LOG_ALLOWLIST_MARKER = "wrapper-log-raw-primitive:"
_WRAPPER_LOG_ALLOWLIST_TAG_RE = re.compile(r"^\s*\[(?P<category>[\w-]+):(?P<area>[\w-]+)\]")

# ---------------------------------------------------------------------------
# #113 review, round 9: text/regex discovery has the exact defect it was
# built to catch - "no hits" meant "none of the spellings/shapes I
# enumerated," not "none exist." reviewer-1 executed four PowerShell-side
# evasions against round 7's check unchanged: a generic helper without the
# "WrapperLog" substring, a param-block function declaration, lowercase
# spelling, and an alternate raw API; and the Python AST walk banned exactly
# three method names, so lstat, os.stat, and os.path.isdir were all
# invisible. Discovery is now REAL PARSING - PowerShell's own AST (a real
# pwsh/powershell subprocess; ParseInput never executes anything) walks the
# WHOLE template - and scope is a CALL GRAPH reachable from the subsystem's
# actual external entry points, not a name pattern: a helper is in scope
# because something in scope calls it, never because of what it is named.
# Every evasion named above, plus round 7's own three, is pinned below as a
# permanent regression - the durable part is that the NEXT evasion has to be
# one nobody has thought of yet, not one already found and forgotten.
# ---------------------------------------------------------------------------

_POWERSHELL_ENTRY_POINTS = frozenset(
    {"new-wrapperlogtargets", "discard-pendingwrapperlogtargets"}
)
_POWERSHELL_PRIMITIVE_CMDLETS = frozenset({
    "test-path", "get-item", "get-itemproperty",
    "resolve-path", "get-itempropertyvalue",
})
_POWERSHELL_PRIMITIVE_MEMBERS = frozenset({"psiscontainer", "attributes", "linktype", "target"})
_POWERSHELL_PRIMITIVE_STATIC_TYPES = frozenset({"io.file", "io.directory"})
_POWERSHELL_PRIMITIVE_STATIC_MEMBERS = frozenset({"exists", "getattributes", "resolvelinktarget"})

# #113 review, round 9: the ratchet is the SET of debt (function, area)
# pairs, not a token count ("token counts are an audit detail, not the
# metric"). Three semantic debt areas: the agent-resolver's whole-function
# allowlist, #175's active-lock check (tracked and fixed separately), and
# pending-discard's honestly-reclassified low-severity debt.
_POWERSHELL_DEBT_KEYS = frozenset({
    ("get-safewrapperlogagentdir", "agent-resolver"),
    ("invoke-wrapperlogretentionprune", "175-active-lock"),
    ("discard-pendingwrapperlogtargets", "pending-discard"),
})

# Every (function, category, area) key this scan is allowed to discover,
# lower-cased so a case-swapped spelling of a KNOWN site cannot silently
# open a second, unmatched entry - editing this set on purpose is the only
# way to add, remove, or reclassify a site.
_POWERSHELL_MANIFEST: frozenset[tuple[str, str, str]] = frozenset({
    ("get-safewrapperlogagentdir", "debt", "agent-resolver"),
    ("test-wrapperlogmarkerpresence", "classifier-internal", "self"),
    ("test-wrapperlogdirectorypresence", "classifier-internal", "self"),
    ("read-wrapperlogsequencerecord", "reference-correct", "sequence-file"),
    ("invoke-wrapperlogretentionprune", "reference-correct", "already-retrieved"),
    ("invoke-wrapperlogretentionprune", "debt", "175-active-lock"),
    ("discard-pendingwrapperlogtargets", "debt", "pending-discard"),
})
assert {(f, a) for f, cat, a in _POWERSHELL_MANIFEST if cat == "debt"} == _POWERSHELL_DEBT_KEYS


# ParseInput parses source text without ever executing it - this is safe to
# run against arbitrary/mutated PowerShell text, including the evasion
# injections below.
_POWERSHELL_AST_SCAN_SCRIPT = r"""
param([string]$SourcePath, [string]$OutPath)
$text = [System.IO.File]::ReadAllText($SourcePath)
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)

$functions = @()
foreach ($fn in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
  $functions += [ordered]@{ name = $fn.Name; start = $fn.Extent.StartOffset; end = $fn.Extent.EndOffset }
}

$commands = @()
$dynamicInvocations = @()
$stringConstantType = [System.Management.Automation.Language.StringConstantExpressionAst]
foreach ($cmd in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)) {
  $firstElement = $cmd.CommandElements[0]
  if (-not ($firstElement -is $stringConstantType)) {
    # #113 review, round 11: the command name is not a static literal -
    # "& $name ..." or dot-sourcing a variable/expression. GetCommandName()
    # returns $null for exactly this shape, which is why it was silently
    # invisible before: it never reached the "if ($name)" filter at all.
    # This construct cannot be resolved statically in the general case -
    # that is a property of the construct, not a gap to close by looking
    # harder - so it is collected separately and REFUSED, not chased.
    $dynamicInvocations += [ordered]@{
      kind = "dynamic-command-invocation"
      start = $cmd.Extent.StartOffset
      line = $cmd.Extent.StartLineNumber
      rawLine = $cmd.Extent.StartScriptPosition.Line
    }
    continue
  }
  $name = $cmd.GetCommandName()
  if ($name) {
    $resolved = $name
    $stripped = $name -replace '^.*\\', ''
    if ($stripped -ieq "Invoke-Expression" -or $stripped -ieq "iex") {
      # #113 review, round 11: the string this evaluates is opaque to a
      # static scan regardless of what it looks like at the call site -
      # refused unconditionally, not just when its argument is itself
      # non-literal.
      $dynamicInvocations += [ordered]@{
        kind = "invoke-expression"
        start = $cmd.Extent.StartOffset
        line = $cmd.Extent.StartLineNumber
        rawLine = $cmd.Extent.StartScriptPosition.Line
      }
      continue
    }
    $cmdInfo = Get-Command -Name $stripped -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmdInfo -and $cmdInfo.CommandType -eq [System.Management.Automation.CommandTypes]::Alias) {
      # #113 review, round 11: resolve the alias to its canonical command
      # via the live session's own alias table (never by guessing a
      # hardcoded list) - "gi" and "Get-Item" invoke the identical cmdlet,
      # so a discovery mechanism that treats them as different strings is
      # not describing PowerShell.
      $resolved = $cmdInfo.ResolvedCommandName
    }
    $commands += [ordered]@{
      name = $name
      resolved = $resolved
      start = $cmd.Extent.StartOffset
      line = $cmd.Extent.StartLineNumber
      rawLine = $cmd.Extent.StartScriptPosition.Line
    }
  }
}

$members = @()
foreach ($m in $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.MemberExpressionAst] }, $true)) {
  if (-not ($m.Member -is $stringConstantType)) {
    # #113 review, round 11: the same unanalysable shape one level up -
    # $obj.($nameExpr) computes the property/method NAME at runtime, so
    # the primitive-member scan's "$m.Member.Value" would be $null here
    # too, the identical silent-miss shape as the dynamic-invocation case.
    $dynamicInvocations += [ordered]@{
      kind = "dynamic-member-access"
      start = $m.Extent.StartOffset
      line = $m.Extent.StartLineNumber
      rawLine = $m.Extent.StartScriptPosition.Line
    }
    continue
  }
  $memberName = $m.Member.Value
  if ($memberName) {
    $members += [ordered]@{
      name = $memberName
      start = $m.Extent.StartOffset
      line = $m.Extent.StartLineNumber
      rawLine = $m.Extent.StartScriptPosition.Line
    }
  }
}

$staticCalls = @()
$invokeMemberType = [System.Management.Automation.Language.InvokeMemberExpressionAst]
foreach ($m in $ast.FindAll({ $args[0] -is $invokeMemberType }, $true)) {
  if ($m.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) {
    $staticCalls += [ordered]@{
      type = $m.Expression.TypeName.FullName
      name = $m.Member.Value
      start = $m.Extent.StartOffset
      line = $m.Extent.StartLineNumber
      rawLine = $m.Extent.StartScriptPosition.Line
    }
  }
}

$result = [ordered]@{
  functions = @($functions)
  commands = @($commands)
  members = @($members)
  staticCalls = @($staticCalls)
  dynamicInvocations = @($dynamicInvocations)
  parseErrors = @($errors | ForEach-Object { $_.Message })
}
$result | ConvertTo-Json -Depth 8 -Compress | Set-Content -LiteralPath $OutPath -Encoding utf8
"""


def _pick_powershell_for_ast_parsing() -> str | None:
    """Prefer pwsh (cross-platform, ships on GitHub-hosted runners of every
    OS); fall back to Windows PowerShell. ParseInput never executes the
    source, so either host is safe to use purely for parsing."""
    return shutil.which("pwsh") or shutil.which("powershell")


def _parse_powershell_wrapper_log_ast(ps_text: str, tmp_path: Path) -> dict:
    shell = _pick_powershell_for_ast_parsing()
    if not shell:
        pytest.skip("no PowerShell host available to parse the template")
    source_path = tmp_path / "wrapper-log-ast-source.txt"
    out_path = tmp_path / "wrapper-log-ast-result.json"
    script_path = tmp_path / "wrapper-log-ast-scan.ps1"
    source_path.write_text(ps_text, encoding="utf-8")
    script_path.write_text(_POWERSHELL_AST_SCAN_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [
            shell, "-NoProfile", "-File", str(script_path),
            "-SourcePath", str(source_path), "-OutPath", str(out_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert not data["parseErrors"], (
        f"template does not parse as PowerShell: {data['parseErrors']}"
    )
    return data


def _strip_module_qualifier(name: str) -> str:
    return name.rsplit("\\", 1)[-1]


def _innermost_powershell_function(functions: list[dict], offset: int) -> dict | None:
    containing = [f for f in functions if f["start"] <= offset < f["end"]]
    if not containing:
        return None
    return min(containing, key=lambda f: f["end"] - f["start"])


def _reachable_powershell_functions(functions: list[dict], commands: list[dict]) -> set[str]:
    """BFS over real call edges (a CommandAst whose name matches another
    function's name) from the wrapper-log subsystem's actual external
    entry points (New-WrapperLogTargets, Discard-PendingWrapperLogTargets -
    verified by grep to be the only two called from outside this
    subsystem). A helper is in scope because something in scope calls it -
    never because of what it happens to be named. Closes the "generic
    helper without the WrapperLog substring" evasion, which any
    name-pattern filter is structurally unable to close."""
    by_lower = {f["name"].lower(): f for f in functions}
    edges: dict[str, set[str]] = {name: set() for name in by_lower}
    for cmd in commands:
        callee = _strip_module_qualifier(cmd["name"]).lower()
        if callee not in by_lower:
            continue
        caller_fn = _innermost_powershell_function(functions, cmd["start"])
        caller = caller_fn["name"].lower() if caller_fn else None
        if caller and caller != callee:
            edges[caller].add(callee)
    reachable: set[str] = set()
    queue = [name for name in _POWERSHELL_ENTRY_POINTS if name in by_lower]
    while queue:
        current = queue.pop()
        if current in reachable:
            continue
        reachable.add(current)
        queue.extend(edges.get(current, ()))
    return reachable


_WRAPPER_LOG_DYNAMIC_ALLOWLIST_MARKER = "wrapper-log-dynamic-invocation:"

# A dynamic construct that is genuinely unrelated to filesystem-presence
# probing (invoking an already-resolved EXTERNAL BINARY's path, never a
# PowerShell function or cmdlet, so it cannot hide a Test-Path/Get-Item
# call) may be allowlisted the same way a raw primitive can - visibly,
# with a reason, under its own marker so a dynamic-invocation exception
# reads differently from a primitive one. Protect-WrapperLogPaths'
# `& $chmod.Source ...` calls are the one pre-existing, reviewed case.
_POWERSHELL_DYNAMIC_MANIFEST: frozenset[tuple[str, str, str]] = frozenset({
    ("protect-wrapperlogpaths", "justified-exception", "posix-chmod"),
})


def _classify_powershell_dynamic_constructs(
    functions: list[dict], reachable: set[str], dynamic_invocations: list[dict]
) -> tuple[set[tuple[str, str, str]], list[str], list[str]]:
    """#113 review, round 11: a dynamic command invocation (`& $name ...`,
    dot-sourcing a variable/expression), an `Invoke-Expression`/`iex` call,
    or a dynamic member-name access (`$obj.($nameExpr)`) all make the call
    graph or the primitive set UNKNOWABLE rather than merely wider - "&
    $name" cannot be resolved statically in the general case; that is a
    property of the construct, not a gap in this scanner. Applying this
    project's own boundary-outcome discipline to the analyser itself:
    when a construct cannot be analysed, REFUSE it rather than silently
    passing it - absent, present, and cannot-tell must render as three
    different answers, never as "fine." An allowlisted exception is still
    visible and deliberate, never a silent pass. Scoped the same way
    primitive hits are: only constructs inside a function reachable from
    a real wrapper-log entry point are refused; the same construct in
    unrelated supervisor code is not this scan's concern."""
    discovered: set[tuple[str, str, str]] = set()
    refused: list[str] = []
    malformed: list[str] = []
    for item in dynamic_invocations:
        enclosing = _innermost_powershell_function(functions, item["start"])
        if enclosing is None or enclosing["name"].lower() not in reachable:
            continue
        func_name = enclosing["name"].lower()
        raw_line = item["rawLine"]
        location = f"{func_name}:{item['line']}: [{item['kind']}] {raw_line.strip()}"
        marker_idx = raw_line.find(_WRAPPER_LOG_DYNAMIC_ALLOWLIST_MARKER)
        if marker_idx == -1:
            refused.append(location)
            continue
        reason = raw_line[marker_idx + len(_WRAPPER_LOG_DYNAMIC_ALLOWLIST_MARKER):].strip()
        tag = _WRAPPER_LOG_ALLOWLIST_TAG_RE.match(reason)
        if not tag:
            malformed.append(
                f"{location} - allowlist comment has no '[category:area]' tag: {reason!r}"
            )
            continue
        discovered.add((func_name, tag["category"], tag["area"]))
    return discovered, refused, malformed


def _discover_powershell_wrapper_log_primitives(
    ast_data: dict,
) -> tuple[set[tuple[str, str, str]], list[str], list[str], list[str]]:
    """The discovery core, shared by the main manifest test and every
    pinned-evasion regression test below - a real function, not an
    inline block, so an evasion test can assert exactly what it needs:
    that a specific injected shape still surfaces as unallowlisted,
    malformed, refused, or an unexpected manifest key."""
    functions = ast_data["functions"]
    reachable = _reachable_powershell_functions(functions, ast_data["commands"])
    dynamic_discovered, refused, dynamic_malformed = _classify_powershell_dynamic_constructs(
        functions, reachable, ast_data.get("dynamicInvocations", [])
    )

    hits: list[dict] = []
    for cmd in ast_data["commands"]:
        resolved_name = cmd.get("resolved", cmd["name"])
        if _strip_module_qualifier(resolved_name).lower() in _POWERSHELL_PRIMITIVE_CMDLETS:
            hits.append(cmd)
    for member in ast_data["members"]:
        if member["name"].lower() in _POWERSHELL_PRIMITIVE_MEMBERS:
            hits.append(member)
    for call in ast_data["staticCalls"]:
        type_name = call["type"].lower().removeprefix("system.")
        if (
            type_name in _POWERSHELL_PRIMITIVE_STATIC_TYPES
            and call["name"].lower() in _POWERSHELL_PRIMITIVE_STATIC_MEMBERS
        ):
            hits.append(call)

    discovered: set[tuple[str, str, str]] = set()
    unallowlisted: list[str] = []
    malformed: list[str] = []
    for hit in hits:
        enclosing = _innermost_powershell_function(functions, hit["start"])
        if enclosing is None or enclosing["name"].lower() not in reachable:
            # Not reachable from a real wrapper-log entry point - out of
            # this scan's scope the same way an unrelated supervisor
            # function always was, just determined by a call edge now
            # instead of a name pattern.
            continue
        func_name = enclosing["name"].lower()
        raw_line = hit["rawLine"]
        marker_idx = raw_line.find(_WRAPPER_LOG_ALLOWLIST_MARKER)
        location = f"{func_name}:{hit['line']}: {raw_line.strip()}"
        if marker_idx == -1:
            unallowlisted.append(location)
            continue
        reason = raw_line[marker_idx + len(_WRAPPER_LOG_ALLOWLIST_MARKER):].strip()
        tag = _WRAPPER_LOG_ALLOWLIST_TAG_RE.match(reason)
        if not tag:
            malformed.append(
                f"{location} - allowlist comment has no '[category:area]' tag: {reason!r}"
            )
            continue
        discovered.add((func_name, tag["category"], tag["area"]))
    discovered |= dynamic_discovered
    malformed += dynamic_malformed
    return discovered, unallowlisted, malformed, refused


_PYTHON_PRIMITIVE_METHOD_NAMES = frozenset({
    "exists", "is_dir", "is_file", "is_symlink", "lstat", "stat",
})
_PYTHON_PRIMITIVE_OS_FUNCTIONS = frozenset({"stat", "lstat"})
_PYTHON_PRIMITIVE_OS_PATH_FUNCTIONS = frozenset({"exists", "isdir", "isfile", "islink"})
_PYTHON_STREAMING_CLASSES_EXCLUDED = frozenset({"BoundedStreamTee"})

# #113 review, round 9: every (function, primitive) pair this scan is
# allowed to discover on the Python side, keyed exactly like the
# PowerShell manifest - explicit classifier and reference exceptions, not
# a bare list of banned method names with no attribution (which is what
# let lstat, os.stat, and os.path.isdir go unseen in round 7's version).
_PYTHON_MANIFEST: frozenset[tuple[str, str, str, str]] = frozenset({
    ("_is_reparse_or_symlink", "lstat", "classifier-internal", "self"),
    ("_scan_path", "lstat", "classifier-internal", "self"),
    ("_scan_marker", "lstat", "classifier-internal", "self"),
    ("_validate_open_lock_path", "lstat", "reference-correct", "open-lock-identity"),
    ("_read_wrapper_log_sequence", "lstat", "reference-correct", "sequence-file"),
})
_PYTHON_MANIFEST_KEYS = frozenset((func, name) for func, name, _cat, _area in _PYTHON_MANIFEST)


def _python_wrapper_log_primitive_hits(module_text: str) -> dict[tuple[str, str], int]:
    """Whole-module AST scan for every raw presence/type primitive SHAPE:
    Path.exists()/.is_dir()/.is_file()/.is_symlink()/.lstat()/.stat(),
    os.stat()/os.lstat(), and os.path.exists()/isdir()/isfile()/islink() -
    not the three method names round 7's version happened to enumerate
    (#113 review, round 9: lstat, os.stat, and os.path.isdir were all
    invisible to it). Keyed by (function, primitive), never a bare count.
    BoundedStreamTee excluded by class name - streaming byte-accounting,
    a different concern this scan was never meant to police.
    """
    tree = ast.parse(module_text)
    hits: dict[tuple[str, str], int] = {}

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.func_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if node.name in _PYTHON_STREAMING_CLASSES_EXCLUDED:
                return
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.func_stack.append(node.name)
            self.generic_visit(node)
            self.func_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def _record(self, primitive: str) -> None:
            func_name = self.func_stack[-1] if self.func_stack else "<module>"
            key = (func_name, primitive)
            hits[key] = hits.get(key, 0) + 1

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute):
                if (
                    func.attr in _PYTHON_PRIMITIVE_OS_FUNCTIONS
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    self._record(func.attr)
                elif (
                    func.attr in _PYTHON_PRIMITIVE_OS_PATH_FUNCTIONS
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "path"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "os"
                ):
                    self._record(func.attr)
                elif func.attr in _PYTHON_PRIMITIVE_METHOD_NAMES:
                    self._record(func.attr)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return hits


def test_wrapper_log_python_side_uses_only_shared_classifiers() -> None:
    """#113 review, round 9: the round-7 AST walk banned exactly three
    method names - exists, is_dir, is_file - so `lstat`, `os.stat`, and
    `os.path.isdir` were all invisible to it: "no hits" meant "none of
    the three spellings I enumerated," not "none exist." Every
    primitive-shaped call anywhere in the module is now inventoried and
    keyed to an explicit (function, primitive) manifest exception - the
    shared classifiers' own bodies plus two already-correct reference
    sites - rather than a bare banned-name list with no attribution.
    Anything else is new, unexplained debt."""
    hits = _python_wrapper_log_primitive_hits(
        Path(wrapper_logs.__file__).read_text(encoding="utf-8")
    )
    discovered_keys = frozenset(hits)
    unexplained = discovered_keys - _PYTHON_MANIFEST_KEYS
    missing = _PYTHON_MANIFEST_KEYS - discovered_keys
    assert not unexplained, (
        "raw presence/type primitive(s) found with no manifest exception "
        f"(function, primitive): {sorted(unexplained)}"
    )
    assert not missing, (
        "a manifest exception no longer corresponds to any real call - "
        f"edit _PYTHON_MANIFEST deliberately if this site was removed: {sorted(missing)}"
    )


def test_wrapper_log_powershell_side_matches_the_manifest_exactly(tmp_path: Path) -> None:
    """#113 review, round 9: rebuilt on REAL PARSING after reviewer-1
    executed four evasions against round 7's text/regex check unchanged -
    a generically-named helper, a param-block function declaration,
    lowercase spelling, and an alternate raw API. Every pinned-evasion
    test below proves each of those, plus round 7's own three, still
    fails this rebuilt check. Scope is a CALL GRAPH reachable from the
    subsystem's two real external entry points, not a name pattern."""
    ast_data = _parse_powershell_wrapper_log_ast(sup.PS_TEMPLATE, tmp_path)
    discovered, unallowlisted, malformed, refused = _discover_powershell_wrapper_log_primitives(ast_data)

    assert not refused, (
        "an unanalysable construct (dynamic command invocation, "
        "Invoke-Expression, or dynamic member-name access) was found in a "
        "function reachable from the wrapper-log subsystem's entry points "
        "(#113 review, round 11) - this defeats static scope discovery and "
        "must not be used here:\n" + "\n".join(refused)
    )
    assert not unallowlisted, (
        "raw presence/type primitive(s) found in a function reachable from "
        "the wrapper-log subsystem's entry points, without a "
        "'# wrapper-log-raw-primitive: [category:area] <reason>' allowlist "
        "comment - route through the shared classifier or allowlist "
        "explicitly:\n" + "\n".join(unallowlisted)
    )
    assert not malformed, (
        "allowlist comment(s) missing the required [category:area] tag "
        "(bare reasons are not enough):\n" + "\n".join(malformed)
    )

    debt_keys = {(func, area) for func, category, area in discovered if category == "debt"}
    assert debt_keys == _POWERSHELL_DEBT_KEYS, (
        "the three debt keys have changed - the ratchet is the SET of debt "
        "(function, area) pairs, not a count (#113 review, round 9):\n"
        f"  expected: {sorted(_POWERSHELL_DEBT_KEYS)}\n"
        f"  found:    {sorted(debt_keys)}"
    )

    full_manifest = _POWERSHELL_MANIFEST | _POWERSHELL_DYNAMIC_MANIFEST
    missing = full_manifest - discovered
    unexpected = discovered - full_manifest
    assert not missing and not unexpected, (
        "the discovered PowerShell raw-primitive/dynamic-invocation "
        "(function, category, area) set no longer equals "
        "_POWERSHELL_MANIFEST | _POWERSHELL_DYNAMIC_MANIFEST - edit the "
        "manifest deliberately if this is an intended change:\n"
        f"  manifest entries no longer found (fixed or removed?): {sorted(missing)}\n"
        f"  new entries not yet in the manifest: {sorted(unexpected)}"
    )


# ---------------------------------------------------------------------------
# #113 review, round 9: "COMMIT reviewer-1's escape injections as permanent
# controls... That is the durable part: it means the next evasion has to be
# one nobody has thought of yet, rather than one we already found and
# forgotten." Each test below injects exactly one named evasion shape into a
# COPY of the real template (never the real one - these must keep failing,
# not pass) and asserts the rebuilt discovery mechanism still catches it.
# ---------------------------------------------------------------------------

_WRAPPER_LOG_MUTATION_ANCHOR = "  $observedCount = 0\n"


def _mutate_ps_template(injected_functions: str, injected_call: str) -> str:
    """Append new function definition(s) anywhere in the template (position
    must not matter - that is round 7's "after-marker" escape, closed by
    call-graph reachability rather than physical position) and splice a
    call to the injected code directly into Invoke-WrapperLogRetentionPrune
    - a real external entry point's transitive callee - so it is reachable
    exactly the way a genuine evasion would need to be to matter."""
    ps = sup.PS_TEMPLATE
    assert ps.count(_WRAPPER_LOG_MUTATION_ANCHOR) == 1
    mutated = ps.replace(
        _WRAPPER_LOG_MUTATION_ANCHOR,
        _WRAPPER_LOG_MUTATION_ANCHOR + "  " + injected_call + "\n",
        1,
    )
    return mutated + "\n" + injected_functions + "\n"


def test_evasion_after_marker_function_is_still_caught(tmp_path: Path) -> None:
    """#113 review, round 7's own first escape, pinned permanently: a new
    WrapperLog-named function, reachable via a real call edge, defined
    physically after the (no longer scope-relevant) old end marker."""
    mutated = _mutate_ps_template(
        injected_functions=(
            "function Get-WrapperLogEvasionAfterMarker([string]$path) {\n"
            "  if (Test-Path -LiteralPath $path) { return $true }\n"
            "  return $false\n"
            "}"
        ),
        injected_call="$null = Get-WrapperLogEvasionAfterMarker $candidateAgentDir",
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, unallowlisted, _malformed, _refused = _discover_powershell_wrapper_log_primitives(ast_data)
    assert any("get-wrapperlogevasionaftermarker" in line for line in unallowlisted)


def test_evasion_fully_qualified_provider_form_is_still_caught(tmp_path: Path) -> None:
    """#113 review, round 7's second escape, pinned permanently: the
    fully-qualified provider form used to dodge test-time cmdlet shadows
    is not exempt from discovery just because it is qualified."""
    mutated = _mutate_ps_template(
        injected_functions="",
        injected_call=(
            "$null = Microsoft.PowerShell.Management\\Get-Item "
            "-LiteralPath $candidateAgentDir -Force -ErrorAction SilentlyContinue"
        ),
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, unallowlisted, _malformed, _refused = _discover_powershell_wrapper_log_primitives(ast_data)
    assert any("invoke-wrapperlogretentionprune" in line for line in unallowlisted)


def test_evasion_untagged_allowlist_reason_is_still_caught(tmp_path: Path) -> None:
    """#113 review, round 7's third escape, pinned permanently: a
    non-empty allowlist reason with no '[category:area]' tag is not a
    ratchet - it is a printed length with extra words."""
    mutated = _mutate_ps_template(
        injected_functions="",
        injected_call=(
            "if (Test-Path -LiteralPath $candidateAgentDir) { "
            "$null = 1 } # wrapper-log-raw-primitive: allowlisted, trust me"
        ),
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, _unallowlisted, malformed, _refused = _discover_powershell_wrapper_log_primitives(ast_data)
    assert any("invoke-wrapperlogretentionprune" in line for line in malformed)


def test_evasion_generic_helper_name_without_wrapperlog_substring_is_still_caught(
    tmp_path: Path,
) -> None:
    """#113 review, round 9's first new escape: a helper whose name has
    nothing to do with "WrapperLog" is in scope because
    Invoke-WrapperLogRetentionPrune (a real transitive callee of a real
    external entry point) calls it - never because of what it is named.
    Any name-pattern filter is structurally unable to close this; a call
    graph closes it by construction."""
    mutated = _mutate_ps_template(
        injected_functions=(
            "function Get-QuotaSnapshot([string]$path) {\n"
            "  if (Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue) {\n"
            "    return $true\n"
            "  }\n"
            "  return $false\n"
            "}"
        ),
        injected_call="$null = Get-QuotaSnapshot $candidateAgentDir",
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, unallowlisted, _malformed, _refused = _discover_powershell_wrapper_log_primitives(ast_data)
    assert any("get-quotasnapshot" in line for line in unallowlisted)


def test_evasion_param_block_function_declaration_is_still_caught(tmp_path: Path) -> None:
    """#113 review, round 9's second new escape: `function Name { param
    (...); ... }` is just as valid PowerShell as `function Name(...) {
    ... }`, and a real parser does not care which form was used to
    reach the same AST shape - only a text regex anchored on a specific
    declaration syntax could miss this."""
    mutated = _mutate_ps_template(
        injected_functions=(
            "function Get-WrapperLogEvasionParamBlock {\n"
            "  param([string]$path)\n"
            "  if (Test-Path -LiteralPath $path) { return $true }\n"
            "  return $false\n"
            "}"
        ),
        injected_call="$null = Get-WrapperLogEvasionParamBlock $candidateAgentDir",
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, unallowlisted, _malformed, _refused = _discover_powershell_wrapper_log_primitives(ast_data)
    assert any("get-wrapperlogevasionparamblock" in line for line in unallowlisted)


def test_evasion_lowercase_cmdlet_spelling_is_still_caught(tmp_path: Path) -> None:
    """#113 review, round 9's third new escape: PowerShell cmdlet names
    are case-insensitive at the language level - `test-path` and
    `Test-Path` invoke the identical cmdlet - so a discovery mechanism
    that treats them as different strings is not describing PowerShell,
    it is describing one convention someone happened to type."""
    mutated = _mutate_ps_template(
        injected_functions="",
        injected_call="if (test-path -LiteralPath $candidateAgentDir) { $null = 1 }",
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, unallowlisted, _malformed, _refused = _discover_powershell_wrapper_log_primitives(ast_data)
    assert any("invoke-wrapperlogretentionprune" in line for line in unallowlisted)


def test_evasion_alternate_raw_api_is_still_caught(tmp_path: Path) -> None:
    """#113 review, round 9's fourth new escape: `[System.IO.File]`'s
    static methods answer the identical existence/attribute questions
    Test-Path/Get-Item do, via a completely different AST shape (a
    static type-member invocation, not a command call) - a cmdlet-only
    scan is blind to it no matter how it finds cmdlets."""
    mutated = _mutate_ps_template(
        injected_functions="",
        injected_call=(
            "if ([System.IO.File]::Exists($candidateAgentDir)) { $null = 1 }"
        ),
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, unallowlisted, _malformed, _refused = _discover_powershell_wrapper_log_primitives(ast_data)
    assert any("invoke-wrapperlogretentionprune" in line for line in unallowlisted)


def test_evasion_python_os_lstat_is_still_caught() -> None:
    """#113 review, round 9: `os.lstat(path)` answers the identical
    question `path.lstat()` does but was invisible to round 7's
    three-method-name AST walk."""
    mutated = Path(wrapper_logs.__file__).read_text(encoding="utf-8").replace(
        "def _prune_wrapper_log_generations(",
        "def _wrapper_log_evasion_os_lstat(path):\n"
        "    return os.lstat(path)\n\n\n"
        "def _prune_wrapper_log_generations(",
        1,
    )
    hits = _python_wrapper_log_primitive_hits(mutated)
    assert ("_wrapper_log_evasion_os_lstat", "lstat") in hits


def test_evasion_python_os_stat_is_still_caught() -> None:
    """#113 review, round 9: `os.stat(path)` - the follow-symlinks sibling
    of `os.lstat` - was equally invisible to round 7's three-method-name
    AST walk."""
    mutated = Path(wrapper_logs.__file__).read_text(encoding="utf-8").replace(
        "def _prune_wrapper_log_generations(",
        "def _wrapper_log_evasion_os_stat(path):\n"
        "    return os.stat(path)\n\n\n"
        "def _prune_wrapper_log_generations(",
        1,
    )
    hits = _python_wrapper_log_primitive_hits(mutated)
    assert ("_wrapper_log_evasion_os_stat", "stat") in hits


def test_evasion_python_os_path_isdir_is_still_caught() -> None:
    """#113 review, round 9: `os.path.isdir(path)` answers the identical
    question `path.is_dir()` does, in the one Python idiom round 7's
    named-method ban list did not enumerate at all."""
    mutated = Path(wrapper_logs.__file__).read_text(encoding="utf-8").replace(
        "def _prune_wrapper_log_generations(",
        "def _wrapper_log_evasion_os_path_isdir(path):\n"
        "    return os.path.isdir(path)\n\n\n"
        "def _prune_wrapper_log_generations(",
        1,
    )
    hits = _python_wrapper_log_primitive_hits(mutated)
    assert ("_wrapper_log_evasion_os_path_isdir", "isdir") in hits


def test_evasion_cmdlet_alias_is_still_caught(tmp_path: Path) -> None:
    """#113 review, round 11's first new escape: `gi` is the standard
    built-in alias for `Get-Item` - it invokes the identical cmdlet, so a
    discovery mechanism that treats the two as different strings is not
    describing PowerShell, it is describing one spelling convention.
    Resolved via the live PowerShell session's own alias table (Get-Command
    on `gi`), never a hardcoded guess list - reviewer-1's exact repro:
    `gi -LiteralPath ...` inside an already-reachable retention function."""
    mutated = _mutate_ps_template(
        injected_functions="",
        injected_call="$null = gi -LiteralPath $candidateAgentDir -Force -ErrorAction SilentlyContinue",
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, unallowlisted, _malformed, _refused = _discover_powershell_wrapper_log_primitives(
        ast_data
    )
    assert any("invoke-wrapperlogretentionprune" in line for line in unallowlisted)


def test_evasion_dynamic_command_invocation_is_refused_not_silently_missed(
    tmp_path: Path,
) -> None:
    """#113 review, round 11's second new escape, and the one this project's
    own rule applies to: `& $name ...` cannot be resolved to a command name
    statically in the general case - GetCommandName() returns null for it,
    which is exactly why it used to vanish before ever reaching the
    "if ($name)" filter. This is not a coverage gap to close by looking
    harder; it is refused outright. reviewer-1's exact repro: a helper
    containing a raw presence check, invoked from the reachable prune
    function via a variable holding its name."""
    mutated = _mutate_ps_template(
        injected_functions=(
            "function Get-DynamicRetentionProbe([string]$path) {\n"
            "  if (Test-Path -LiteralPath $path) { return $true }\n"
            "  return $false\n"
            "}"
        ),
        injected_call="$dynamicProbeName = 'Get-DynamicRetentionProbe'; "
        "$null = & $dynamicProbeName $candidateAgentDir",
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, _unallowlisted, _malformed, refused = _discover_powershell_wrapper_log_primitives(
        ast_data
    )
    assert any(
        "invoke-wrapperlogretentionprune" in line and "dynamic-command-invocation" in line
        for line in refused
    )


def test_evasion_invoke_expression_is_refused_not_silently_missed(tmp_path: Path) -> None:
    """#113 review, round 11: the same unanalysable-construct class as
    dynamic command invocation - Invoke-Expression evaluates a STRING as
    code, whose content is opaque to a static AST scan regardless of what
    the call site itself looks like. Refused unconditionally."""
    mutated = _mutate_ps_template(
        injected_functions="",
        injected_call="$null = Invoke-Expression \"Test-Path -LiteralPath '$candidateAgentDir'\"",
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, _unallowlisted, _malformed, refused = _discover_powershell_wrapper_log_primitives(
        ast_data
    )
    assert any(
        "invoke-wrapperlogretentionprune" in line and "invoke-expression" in line
        for line in refused
    )


def test_evasion_dynamic_member_access_is_refused_not_silently_missed(tmp_path: Path) -> None:
    """#113 review, round 11: the same unanalysable-construct class one
    level up from a command call - `$item.($propName)` computes the
    property NAME at runtime, so `.Member.Value` would be null here too,
    the identical silent-miss shape a dynamic command invocation has."""
    mutated = _mutate_ps_template(
        injected_functions="",
        injected_call=(
            "$dynamicPropName = 'Attributes'; "
            "$fakeItem = Microsoft.PowerShell.Management\\Get-Item -LiteralPath "
            "$candidateAgentDir -Force -ErrorAction SilentlyContinue; "
            "if ($fakeItem) { $null = $fakeItem.($dynamicPropName) }"
        ),
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, _unallowlisted, _malformed, refused = _discover_powershell_wrapper_log_primitives(
        ast_data
    )
    assert any(
        "invoke-wrapperlogretentionprune" in line and "dynamic-member-access" in line
        for line in refused
    )


def test_dynamic_invocation_allowlist_requires_a_tagged_reason(tmp_path: Path) -> None:
    """A dynamic-invocation exception must be visible and deliberate, the
    same discipline as a raw-primitive exception - a bare, untagged reason
    on the allowlist marker must still fail, not silently pass."""
    mutated = _mutate_ps_template(
        injected_functions=(
            "function Get-DynamicRetentionProbe([string]$path) {\n"
            "  if (Test-Path -LiteralPath $path) { return $true }\n"
            "  return $false\n"
            "}"
        ),
        injected_call=(
            "$dynamicProbeName = 'Get-DynamicRetentionProbe'; "
            "$null = & $dynamicProbeName $candidateAgentDir "
            "# wrapper-log-dynamic-invocation: trust me, this is fine"
        ),
    )
    ast_data = _parse_powershell_wrapper_log_ast(mutated, tmp_path)
    _discovered, _unallowlisted, malformed, refused = _discover_powershell_wrapper_log_primitives(
        ast_data
    )
    assert not refused, "a present-but-untagged reason must be malformed, not silently refused"
    assert any("invoke-wrapperlogretentionprune" in line for line in malformed)


def test_supervise_plan_never_probes_wrapper_log_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Store(tmp_path).init(["worker"])
    assert cli.main(["--root", str(tmp_path), "supervise", "--init"]) == 0
    capsys.readouterr()

    def _fail_probe(*_args, **_kwargs):
        raise AssertionError("supervisor plan probed wrapper log diagnostics")

    monkeypatch.setattr(wrapper_logs, "read_wrapper_log_location", _fail_probe)
    rc = cli.main(
        [
            "--root",
            str(tmp_path),
            "supervise",
            "--plan",
            "--now",
            str(NOW),
        ]
    )

    assert rc == 0
    assert "agents" in json.loads(capsys.readouterr().out)


def test_default_wrapper_log_root_is_project_scoped_and_outside_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = tmp_path / "local-state"

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": str(local)},
    )

    assert resolved.parent == local / "agenttalk" / "wrapper-logs"
    assert len(resolved.name) == 64
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_rejects_relative_ambient_state_path(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    home = tmp_path / "home"

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": ".agenttalk-logs", "HOME": str(home)},
    )

    assert resolved.parent == home / ".local" / "state" / "agenttalk" / "wrapper-logs"
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_rejects_absolute_state_path_inside_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    home = tmp_path / "home"

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={
            "XDG_STATE_HOME": str(checkout / "logs"),
            "HOME": str(home),
        },
    )

    assert resolved.parent == home / ".local" / "state" / "agenttalk" / "wrapper-logs"
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_uses_independent_fallback_when_home_is_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={
            "XDG_STATE_HOME": str(checkout / "state"),
            "HOME": str(checkout / "home"),
        },
    )

    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_tolerates_unresolvable_home_when_configured_path_is_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = tmp_path / "local-state"

    def _raise_no_home() -> Path:
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(wrapper_logs.Path, "home", staticmethod(_raise_no_home))

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": str(local)},
    )

    assert resolved.parent == local / "agenttalk" / "wrapper-logs"
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_falls_back_to_tempdir_when_home_is_unresolvable_and_no_state_path_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def _raise_no_home() -> Path:
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(wrapper_logs.Path, "home", staticmethod(_raise_no_home))

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={},
    )

    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_falls_back_when_project_is_a_filesystem_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project rooted at a filesystem anchor has no same-volume "outside":
    every absolute candidate resolves as relative to the project. This must
    degrade to a fixed fallback, not raise - restoring a guarantee an earlier
    revision had (`if not roots: roots.append(<tempdir fallback>)`) and a
    later one silently dropped when it added the raise this test guards
    against (#113 review)."""
    anchor = Path(tmp_path.anchor)
    monkeypatch.setenv("TEMP", str(anchor))
    monkeypatch.setenv("TMP", str(anchor))
    monkeypatch.setattr(wrapper_logs.tempfile, "tempdir", None)
    env = {
        "LOCALAPPDATA": str(anchor),
        "USERPROFILE": str(anchor),
        "TEMP": str(anchor),
        "TMP": str(anchor),
    }

    resolved = wrapper_logs.default_wrapper_log_root(anchor, platform="nt", environ=env)
    assert resolved is not None

    roots = wrapper_logs.wrapper_log_root_candidates(anchor, platform="nt", environ=env)
    assert roots, "candidates must never be empty for this edge case"

    targets = wrapper_logs._allocate_wrapper_log_targets(anchor, "worker", env)
    assert targets is not None
    # _allocate_wrapper_log_targets' own contract stops at ".pending" -
    # ".committed" is written later, by the confirm step inside
    # installed_standard_streams_from_environment, which this test does not
    # exercise. Assert what this function actually promises.
    assert (targets.generation_dir / ".pending").exists()
    assert targets.stdout_path.exists()
    assert targets.stderr_path.exists()


def test_wrapper_log_candidates_do_not_consult_later_home_after_valid_configured_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = tmp_path / "local-state"

    def _unexpected_home(*_args, **_kwargs):
        raise AssertionError("later HOME candidate was consulted")

    monkeypatch.setattr(wrapper_logs, "_home_state_root", _unexpected_home)
    roots = wrapper_logs.wrapper_log_root_candidates(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": str(local)},
    )

    assert roots[0].parent == local / "agenttalk" / "wrapper-logs"


def test_wrapper_log_candidates_keep_preferred_when_later_fallback_resolution_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = tmp_path / "local-state"

    def _broken_temp(_project):
        raise OSError("injected later fallback failure")

    monkeypatch.setattr(wrapper_logs, "temporary_wrapper_log_root", _broken_temp)
    roots = wrapper_logs.wrapper_log_root_candidates(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": str(local)},
    )

    assert roots[0].parent == local / "agenttalk" / "wrapper-logs"


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific reparse detection")
def test_reparse_detection_uses_raw_windows_attributes_when_stat_disagrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113 review, round 5, findings #1/#2/#6: a real OneDrive
    placeholder file reports FILE_ATTRIBUTE_REPARSE_POINT to
    GetFileAttributesW (the same call PowerShell's Get-Item/.Attributes
    uses) but Python's os.lstat() does NOT reflect it on the host where
    this was measured - a genuine platform discovery, not a line-level
    bug: the two languages' classifiers disagreed about the identical
    input this scan exists to unify.

    This cannot be reproduced with a real OneDrive placeholder in this
    environment (no OneDrive sync client configured here), so it is
    verified by mocking the raw-attribute primitive
    (_windows_raw_file_attributes) directly to return exactly the
    discrepancy measured: a file lstat() reports as perfectly ordinary,
    but the raw Win32 attribute call flags as a reparse point. Said
    plainly rather than claimed as an executed real-condition
    reproduction it is not."""
    target = tmp_path / "onedrive-placeholder.txt"
    target.write_text("looks like a plain file to os.lstat()", encoding="utf-8")
    assert not stat.S_ISLNK(target.lstat().st_mode)

    monkeypatch.setattr(
        wrapper_logs,
        "_windows_raw_file_attributes",
        lambda path: (
            wrapper_logs._WIN32_FILE_ATTRIBUTE_REPARSE_POINT
            if path == target
            else None
        ),
    )

    assert wrapper_logs._is_reparse_or_symlink(target) is True


@pytest.mark.skipif(os.name != "nt", reason="exercises the real Win32 boundary")
def test_windows_raw_file_attributes_native_boundary_raises_on_real_failure() -> None:
    """#113 review, round 9, finding 3: the native call itself, not a
    mock of it. GetFileAttributesW is documented to return an unsigned
    DWORD, with INVALID_FILE_ATTRIBUTES (0xFFFFFFFF) on failure - but an
    undeclared ctypes foreign-function call defaults to a SIGNED
    restype, so a real failure came back as -1, which the sentinel
    comparison (against 4294967295) never matched. Every existing
    caller still ended up fail-closed, but only because `-1 & <any bit>`
    is truthy by accident of two's-complement representation, not
    because the contract actually fired. This calls the REAL WinAPI
    function against a genuinely nonexistent path (no OneDrive
    placeholder needed for a plain lookup failure), with no mock of
    _windows_raw_file_attributes itself, and asserts the documented
    exception - not merely that some caller's bit test still happens to
    come out truthy."""
    nonexistent = Path(r"\\?\C:\agenttalk-113-round9-finding3-does-not-exist")

    with pytest.raises(wrapper_logs._RawAttributeLookupFailed):
        wrapper_logs._windows_raw_file_attributes(nonexistent)


def test_reparse_attribute_check_is_exercised_without_symlink_privilege(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state" / "agenttalk" / "wrapper-logs" / "project"
    blocked_ancestor = tmp_path / "state"
    real_check = wrapper_logs._is_reparse_or_symlink

    monkeypatch.setattr(
        wrapper_logs,
        "_is_reparse_or_symlink",
        lambda path: path == blocked_ancestor or real_check(path),
    )

    with pytest.raises(OSError, match="unsafe wrapper log root ancestry"):
        wrapper_logs._prepare_agent_log_dir(root, "agent-0123456789abcdef")


def test_wrapper_log_candidate_rechecks_original_ancestry_after_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    configured_state = tmp_path / "configured-state"
    home = tmp_path / "home"
    home.mkdir()
    real_check = wrapper_logs._is_reparse_or_symlink
    configured_visits = 0

    def swap_after_first_check(path: Path) -> bool:
        nonlocal configured_visits
        if path == configured_state:
            configured_visits += 1
            return configured_visits > 1
        return real_check(path)

    monkeypatch.setattr(
        wrapper_logs,
        "_is_reparse_or_symlink",
        swap_after_first_check,
    )

    roots = wrapper_logs.wrapper_log_root_candidates(
        checkout,
        platform="posix",
        environ={
            "XDG_STATE_HOME": str(configured_state),
            "HOME": str(home),
        },
    )

    assert configured_visits >= 2
    assert roots[0].is_relative_to(home.resolve())


def test_read_wrapper_log_sequence_honors_persisted_uncertainty_marker(
    tmp_path: Path,
) -> None:
    """.sequence-uncertain is written when the allocator that created a
    generation could not fully scan every root, so its OWN sequence number
    may already be lower than the true prior maximum. A later launch
    reading a perfectly well-formed .sequence file back must not conclude
    that uncertainty has gone away - the marker's fact is about the
    generation's history, not about whether .sequence itself parses
    (#113 review comment #5: a marker nothing reads back is not a check,
    it's a costume)."""
    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / ".sequence").write_text("5", encoding="utf-8")
    (generation / ".sequence-uncertain").write_bytes(b"")

    sequence, uncertain = wrapper_logs._read_wrapper_log_sequence(
        generation, committed=True,
    )

    assert sequence == 5
    assert uncertain is True


def test_read_wrapper_log_sequence_marker_is_inert_when_not_committed(
    tmp_path: Path,
) -> None:
    """Matches the existing `committed and ...` gating used everywhere else
    in this function - an uncommitted generation's uncertainty is not this
    function's concern regardless of the marker."""
    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / ".sequence").write_text("5", encoding="utf-8")
    (generation / ".sequence-uncertain").write_bytes(b"")

    sequence, uncertain = wrapper_logs._read_wrapper_log_sequence(
        generation, committed=False,
    )

    assert sequence == 5
    assert uncertain is False


def test_read_wrapper_log_sequence_stays_certain_without_the_marker(
    tmp_path: Path,
) -> None:
    """Negative control: a valid record with no marker at all must not be
    flagged uncertain just because the check now also looks for one."""
    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / ".sequence").write_text("5", encoding="utf-8")

    sequence, uncertain = wrapper_logs._read_wrapper_log_sequence(
        generation, committed=True,
    )

    assert sequence == 5
    assert uncertain is False


def test_owned_committed_generations_propagates_persisted_sequence_uncertainty(
    tmp_path: Path,
) -> None:
    """Integration-level proof the marker actually changes a retention
    decision, not just the direct unit's return value: a generation with a
    syntactically perfect .sequence but a .sequence-uncertain marker must
    still make the aggregate `uncertain` True."""
    root = tmp_path / "logs"
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    generation = root / agent_leaf / f"20260804T120001000Z-{'1' * 32}"
    generation.mkdir(parents=True)
    (generation / ".sequence").write_text("1", encoding="utf-8")
    (generation / ".sequence-uncertain").write_bytes(b"")
    (generation / ".committed").write_bytes(b"")
    (generation / "stdout.log").write_bytes(b"")
    (generation / "stderr.log").write_bytes(b"")

    _owned, _observed_count, _max_sequence, uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )

    assert uncertain is True


def test_corrupt_committed_sequence_marks_retention_uncertain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "logs"
    agent_leaf, _generations, _newest = _committed_generation_pool(
        root,
        newest_sequence="not-a-sequence",
    )

    _owned, _observed_count, _max_sequence, uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )

    assert uncertain is True


def _poison_lstat(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Make stat()/lstat() raise a real-shaped 'could not tell' OSError for
    exactly `target`, leaving every other path's real stat/lstat behavior
    untouched.

    Path.exists()/.is_dir() are boolean wrappers that swallow SOME
    OSErrors into False rather than raising - on 3.10's pathlib, only a
    curated list (ENOENT/ENOTDIR/EBADF/ELOOP, plus WinError 21 "not ready"
    on Windows), not an arbitrary PermissionError (confirmed directly
    against 3.10's pathlib.py source: a plain PermissionError is NOT in
    that list and propagates normally, so it would not actually exercise
    the bug this classifier exists for). WinError 21 is exactly that
    shape, and it's the realistic one anyway (a disconnected network
    share or removed media).

    Tried and abandoned a real ACL-based repro first: Windows'
    GetFileAttributes-class calls (what stat()/lstat() use) are
    permissive enough against ordinary NTFS ACL edits that even an
    explicit full-control deny left os.stat() succeeding - not something
    this environment could reproduce for real, only construct.
    """
    real_stat = Path.stat
    real_lstat = Path.lstat

    def _not_ready() -> OSError:
        err = OSError("drive not ready")
        err.winerror = 21
        return err

    def denied_stat(self: Path, *args: object, **kwargs: object):
        if self == target:
            raise _not_ready()
        return real_stat(self, *args, **kwargs)

    def denied_lstat(self: Path, *args: object, **kwargs: object):
        if self == target:
            raise _not_ready()
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    monkeypatch.setattr(Path, "lstat", denied_lstat)


@pytest.mark.parametrize("depth", ["agent_dir", "generation"])
def test_scan_marks_retention_uncertain_not_absent_at_every_depth(
    depth: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer-level control for the shared classifier (#113 review,
    reviewer-1's sharpened class fix): an UNUSABLE outcome must surface as
    ``uncertain=True`` from ``_owned_committed_generations`` at EVERY scan
    depth - parametrized so a depth added later without a matching entry
    here is conspicuous, not silently uncovered.

    This asserts the CONSUMER's behavior, not just that ``_scan_path``
    returns the right enum member in isolation: a future call site that
    mishandles the classifier's outcome and collapses UNUSABLE back into
    behaving like ABSENT fails HERE, at the same boundary retention
    actually reads, not only in a classifier-local unit test a careless
    refactor could leave green while the real bug ships (the exact gap
    that let the same mistake recur once already in this PR).
    """
    root = tmp_path / "logs"
    root.mkdir()
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    agent_dir = root / agent_leaf
    agent_dir.mkdir()
    if depth == "agent_dir":
        target = agent_dir
    else:
        target = agent_dir / f"20260804T120001000Z-{1:032x}"
        target.mkdir()

    _poison_lstat(monkeypatch, target)

    owned, _observed_count, max_sequence, uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )

    assert owned == []
    assert max_sequence == 0
    assert uncertain is True


def test_unreadable_committed_marker_is_not_deletion_eligible_but_still_flagged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113 review, round 3 then corrected in round 4: a `.committed`
    marker that cannot be read must not collapse to a confident "not
    committed" (round 3's finding - reviewer-1 measured that exact
    collapse producing owned_count 0 and uncertain False for a generation
    that was genuinely committed) - round 3's OWN fix over-corrected by
    resolving the ambiguity AS committed, which round 4 found makes an
    unconfirmed generation deletion-eligible once the safety bound is
    crossed. The correct resolution: still flag the scan uncertain (so
    retention knows its view is incomplete) and still track its sequence
    number (so max_sequence is not lost), but do NOT count it as owned -
    deletion needs positive proof of commitment, never a permissive
    resolution of an unknown."""
    root = tmp_path / "logs"
    root.mkdir()
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    agent_dir = root / agent_leaf
    agent_dir.mkdir()
    generation = agent_dir / f"20260804T120001000Z-{1:032x}"
    generation.mkdir()
    (generation / ".sequence").write_text("9", encoding="utf-8")
    committed_marker = generation / ".committed"
    committed_marker.write_bytes(b"")

    _poison_lstat(monkeypatch, committed_marker)

    owned, _observed_count, max_sequence, uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )

    assert owned == []
    assert max_sequence == 9
    assert uncertain is True


def test_marker_that_is_a_directory_is_not_present(tmp_path: Path) -> None:
    """#113 review, round 4: a closed outcome set with an under-specified
    member is not closed - PRESENT must mean a valid marker leaf, not
    merely that some filesystem object occupies the name. A directory
    placed where `.committed` is expected must not read as PRESENT, or
    an unconfirmed generation is counted committed and becomes prunable
    on nothing more than "something exists at that path"."""
    root = tmp_path / "logs"
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    generation = root / agent_leaf / f"20260804T120001000Z-{1:032x}"
    generation.mkdir(parents=True)
    (generation / ".sequence").write_text("9", encoding="utf-8")
    (generation / ".committed").mkdir()

    owned, _observed_count, _max_sequence, uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )

    assert owned == []
    assert uncertain is True


def test_genuinely_absent_agent_dir_is_not_flagged_uncertain(tmp_path: Path) -> None:
    """The other side of the same check: a root this agent has truly never
    used (no agent_dir at all) must still read as a plain, confident
    absence - the fix must not turn every unused root into a false
    'uncertain', only a genuinely unreadable one."""
    root = tmp_path / "logs"
    root.mkdir()
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    assert not (root / agent_leaf).exists()

    owned, _observed_count, max_sequence, uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )

    assert owned == []
    assert max_sequence == 0
    assert uncertain is False


def test_allocator_continues_to_next_candidate_when_cleanup_probe_itself_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113 review: the cleanup-time ancestry probe inside the allocator's
    exception handler can itself raise OSError (the candidate became
    inaccessible or disconnected after its generation directory was
    created but before allocation finished). Unsuppressed, that second
    error escaped the candidate loop entirely and disabled capture, rather
    than treating one uncheckable partial directory as unsafe to delete
    and continuing to the next candidate root."""
    configured_base = tmp_path / "configured"
    home_base = tmp_path / "home"
    configured_base.mkdir()
    home_base.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    env = {
        "LOCALAPPDATA": str(configured_base),
        "USERPROFILE": str(home_base),
        "HOME": str(home_base),
    }
    roots = wrapper_logs.wrapper_log_root_candidates(project, environ=env)
    assert len(roots) >= 2
    poisoned_root = roots[0].resolve()

    real_reparse_check = wrapper_logs._has_reparse_or_symlink_component

    def failing_reparse_check(path: Path):
        try:
            rel = Path(path).resolve().relative_to(poisoned_root)
        except ValueError:
            return real_reparse_check(path)
        # 0 parts = poisoned_root itself, 1 part = its agent_leaf dir - both
        # legitimate _prepare_agent_log_dir ancestry checks that must keep
        # working so the candidate can be created in the first place. 2+
        # parts is the generation directory itself: the level this probe
        # cannot tell about, both when validating the freshly created
        # directory and again from the cleanup handler.
        if len(rel.parts) >= 2:
            raise OSError("cannot probe ancestry: disconnected")
        return real_reparse_check(path)

    monkeypatch.setattr(
        wrapper_logs, "_has_reparse_or_symlink_component", failing_reparse_check
    )

    targets = wrapper_logs._allocate_wrapper_log_targets(project, "worker", env)

    assert targets.root != poisoned_root
    assert targets.stdout_path.is_file()
    assert targets.stderr_path.is_file()
    poisoned_agent_dir = poisoned_root / wrapper_logs._wrapper_log_agent_leaf("worker")
    assert len(list(poisoned_agent_dir.iterdir())) == 1


def test_collapsed_marker_cannot_prune_data_it_could_not_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal-behaviour control the round-3 review demanded:
    asserting `uncertain is True` at the scan consumer is not enough,
    because the flag is not what protects data - the prune's own
    early-return on `uncertain` is. This drives the exact shape
    reviewer-1 reproduced: WRAPPER_LOG_GENERATIONS + 1 committed
    generations (5, with a keep count of 4), the OLDEST one genuinely
    marked `.sequence-uncertain`, and ONLY that marker's readability
    poisoned. If an unreadable marker still collapses to "no marker" here,
    the prune runs unprotected and deletes exactly the generation
    uncertainty was raised to protect - reviewer-1 measured this as five
    generations becoming four. Assert the generation still EXISTS after
    the collapse mutation, not merely that a flag was set.
    """
    root = tmp_path / "logs"
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    generations: list[Path] = []
    for sequence in range(1, wrapper_logs.WRAPPER_LOG_GENERATIONS + 2):
        generation = root / agent_leaf / f"20260804T12000{sequence}000Z-{sequence:032x}"
        generation.mkdir(parents=True)
        (generation / ".sequence").write_text(str(sequence), encoding="utf-8")
        (generation / ".committed").write_bytes(b"")
        (generation / "stdout.log").write_bytes(b"")
        (generation / "stderr.log").write_bytes(b"")
        generations.append(generation)
    oldest = generations[0]
    uncertainty_marker = oldest / ".sequence-uncertain"
    uncertainty_marker.write_bytes(b"")

    _poison_lstat(monkeypatch, uncertainty_marker)

    wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)

    assert oldest.is_dir(), (
        "prune deleted the generation its own uncertainty marker existed to protect"
    )


def test_unusable_committed_probe_cannot_make_a_pending_generation_deletion_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113 review, round 4: round 3's fix covered only one member of the
    uncertainty union - a physically PRESENT `.committed` marker that
    could not be read. It missed the other member: a genuinely PENDING
    generation (no real `.committed` at all) whose PROBE for `.committed`
    itself raises. Counting that ambiguity toward the safety bound is
    conservative and fine; resolving it AS committed is not - that makes
    an unconfirmed, still-pending generation deletion-eligible the moment
    the bound is crossed. Reproduces reviewer-1's exact numbers:
    WRAPPER_LOG_GENERATIONS*3 + 1 (13) generations, the oldest genuinely
    pending with captured stdout, only its `.committed` probe poisoned.
    """
    root = tmp_path / "logs"
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    total = wrapper_logs.WRAPPER_LOG_GENERATIONS * 3 + 1
    generations: list[Path] = []
    for sequence in range(1, total + 1):
        generation = root / agent_leaf / f"20260804T12{sequence:04d}000Z-{sequence:032x}"
        generation.mkdir(parents=True)
        (generation / "stdout.log").write_bytes(b"captured output")
        (generation / "stderr.log").write_bytes(b"")
        generations.append(generation)
    oldest = generations[0]
    # oldest is genuinely PENDING - no .committed at all. Every later
    # generation is properly committed with a real sequence.
    for index, generation in enumerate(generations[1:], start=2):
        (generation / ".sequence").write_text(str(index), encoding="utf-8")
        (generation / ".committed").write_bytes(b"")

    _poison_lstat(monkeypatch, oldest / ".committed")

    wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)

    assert oldest.is_dir(), (
        "prune deleted a pending generation whose .committed probe was merely unreadable"
    )
    assert (oldest / "stdout.log").read_bytes() == b"captured output"
    # #113 review, round 5, finding #7: the other half of this same
    # scenario. 13 physical generations against a bound of 12 must
    # actually cross that bound and prune the 12 real candidates down to
    # quota - using len(candidates)==12<=12 for the bound check instead
    # of the true observed_count==13 made this cycle look safely under
    # quota when it was not, so pruning never ran and all 13 generations
    # accumulated on disk indefinitely (confirmed by execution before
    # this fix). The ambiguous generation is never itself a candidate
    # either way (already covered by the assertions above) - this
    # checks that its mere presence does not ALSO block real candidates
    # from being pruned once they should be.
    committed_survivors = [g for g in generations[1:] if g.is_dir()]
    assert len(committed_survivors) == wrapper_logs.WRAPPER_LOG_GENERATIONS, (
        "the 12 real candidates were not pruned toward quota - the bound "
        "check used the undercounted candidate total instead of the true "
        "observed generation count"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific reparse detection")
def test_raw_attribute_reparse_signal_actually_reaches_the_destructive_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113 review, round 7, finding 2 (BLOCKING): the reframe, applied to
    this PR's own #1/#2/#6 fix. `_windows_raw_file_attributes` works
    correctly in isolation (the round-5 test above proves that), and
    `_scan_marker` now consults it (wired in this round) - but neither
    of those facts alone proves the CORRECTED VALUE ever reaches the
    place a deletion decision is made. This is that proof, one level
    up the call chain from the existing test: the oldest of
    WRAPPER_LOG_GENERATIONS + 1 committed generations has a `.committed`
    marker that `os.lstat()` reports as a perfectly ordinary file (so an
    unwired classifier would read PRESENT and this generation would be
    a normal, oldest-first deletion candidate) but the raw Win32 signal
    flags as a reparse point - the same measured OneDrive-placeholder
    discrepancy, at the one marker whose misclassification is
    destructive rather than merely observed. If the classifier's
    corrected value is collapsed anywhere between `_scan_marker` and
    `_prune_wrapper_log_generations`, this generation gets pruned away
    on schedule instead of preserved as unconfirmed."""
    root = tmp_path / "logs"
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    generations: list[Path] = []
    for sequence in range(1, wrapper_logs.WRAPPER_LOG_GENERATIONS + 2):
        generation = root / agent_leaf / f"20260804T12000{sequence}000Z-{sequence:032x}"
        generation.mkdir(parents=True)
        (generation / ".sequence").write_text(str(sequence), encoding="utf-8")
        (generation / ".committed").write_bytes(b"")
        generations.append(generation)
    oldest = generations[0]
    disputed_marker = oldest / ".committed"
    assert not stat.S_ISLNK(disputed_marker.lstat().st_mode)

    monkeypatch.setattr(
        wrapper_logs,
        "_windows_raw_file_attributes",
        lambda path: (
            wrapper_logs._WIN32_FILE_ATTRIBUTE_REPARSE_POINT
            if path == disputed_marker
            else None
        ),
    )

    wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)

    assert oldest.is_dir(), (
        "the oldest generation's .committed marker disagreed between "
        "os.lstat() and the raw Win32 attribute signal, and the "
        "classifier's UNUSABLE verdict never reached the prune decision "
        "- it was pruned as an ordinary committed candidate instead"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific reparse detection")
def test_failed_raw_attribute_lookup_reaches_the_destructive_prune_as_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113 review, round 7, finding 4 (low-confidence, reported and
    fixed rather than dropped): `_windows_raw_file_attributes` used to
    conflate "not on Windows, nothing to attempt" and "this IS Windows,
    and the call itself just failed" into the same silent `None`,
    contradicting this PR's own documented contract that an
    inconclusive raw call must mean "no additional signal," never
    "confirmed clear." A caller that could not tell the two apart would
    fall through to whatever `os.lstat()` alone found - exactly the
    failure mode finding 2 exists to close, just reached from a
    genuinely failing lookup instead of a successful, disagreeing one.
    Same consumer-level shape as the finding-2 test immediately above:
    the oldest of WRAPPER_LOG_GENERATIONS + 1 committed generations has
    an ordinary-looking `.committed` marker whose raw attribute lookup
    raises `_RawAttributeLookupFailed` rather than returning a value -
    if that failure fell through silently, this generation is an
    ordinary committed candidate and gets pruned on schedule; if it
    propagates as UNUSABLE the way an unreadable marker already does,
    the generation is preserved."""
    root = tmp_path / "logs"
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    generations: list[Path] = []
    for sequence in range(1, wrapper_logs.WRAPPER_LOG_GENERATIONS + 2):
        generation = root / agent_leaf / f"20260804T12000{sequence}000Z-{sequence:032x}"
        generation.mkdir(parents=True)
        (generation / ".sequence").write_text(str(sequence), encoding="utf-8")
        (generation / ".committed").write_bytes(b"")
        generations.append(generation)
    oldest = generations[0]
    disputed_marker = oldest / ".committed"
    assert not stat.S_ISLNK(disputed_marker.lstat().st_mode)

    def _failing_raw_attributes(path: Path) -> int | None:
        if path == disputed_marker:
            raise wrapper_logs._RawAttributeLookupFailed(str(path))
        return None

    monkeypatch.setattr(
        wrapper_logs, "_windows_raw_file_attributes", _failing_raw_attributes
    )

    wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)

    assert oldest.is_dir(), (
        "the raw attribute lookup for the oldest generation's .committed "
        "marker FAILED (not merely 'not on Windows') and that failure "
        "fell through to os.lstat()'s confident PRESENT instead of "
        "propagating as UNUSABLE - it was pruned as an ordinary "
        "committed candidate instead"
    )


def test_prune_preserves_newest_generation_when_its_sequence_is_corrupt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "logs"
    agent_leaf, generations, newest = _committed_generation_pool(
        root,
        newest_sequence="not-a-sequence",
    )

    wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)

    assert newest.is_dir()
    assert len([generation for generation in generations if generation.is_dir()]) == 5


def test_bare_legacy_generation_without_sequence_remains_prunable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "logs"
    agent_leaf, generations, newest = _committed_generation_pool(
        root,
        newest_sequence=None,
    )
    _owned, _observed_count, _max_sequence, uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )

    wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)

    assert uncertain is False
    assert not newest.exists()
    assert len([generation for generation in generations if generation.is_dir()]) == 4


def test_prune_preserves_live_generation_and_reclaims_stale_active_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "logs"
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    generations: list[Path] = []
    for sequence in range(1, wrapper_logs.WRAPPER_LOG_GENERATIONS + 2):
        generation = (
            root
            / agent_leaf
            / f"20260804T12000{sequence}000Z-{sequence:032x}"
        )
        generation.mkdir(parents=True)
        # Simultaneous direct launches can legitimately observe the same prior
        # maximum. The nonce-bearing generation name must total-order the tie.
        (generation / ".sequence").write_text("1", encoding="utf-8")
        (generation / ".committed").write_bytes(b"")
        (generation / "stdout.log").write_bytes(b"")
        (generation / "stderr.log").write_bytes(b"")
        generations.append(generation)

    owned, _observed_count, _max_sequence, _uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )
    assert len({sort_key for sort_key, _generation in owned}) == len(generations)

    active = generations[0]
    script = (
        "import os,sys\n"
        "from pathlib import Path\n"
        "from agenttalk import wrapper_logs\n"
        "lock=wrapper_logs._acquire_active_generation_lock(Path(sys.argv[1]))\n"
        "assert lock is not None\n"
        "print('ready', flush=True)\n"
        "sys.stdin.readline()\n"
        "os._exit(0)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(active)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"

        wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)
        assert active.is_dir()
        assert len([path for path in generations if path.is_dir()]) == 5

        assert process.stdin is not None
        process.stdin.write("exit\n")
        process.stdin.flush()
        assert process.wait(timeout=5) == 0

        wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)
        assert not active.exists()
        assert not wrapper_logs._active_generation_lock_path(active).exists()
        assert len([path for path in generations if path.is_dir()]) == 4
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_guard_prune_does_not_reclaim_lock_when_generation_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#113 review, round 5 sweep, site #10: the trailing cleanup in
    _guard_wrapper_log_prune used to remove the now-orphaned `.active`
    lock file whenever `generation_dir.exists()` read False - which
    `.exists()` also reports for a generation it simply could not
    confirm. Only a CONFIRMED absence may reclaim the lock; an unusable
    read must leave it in place rather than prematurely freeing
    protection for a generation that might still genuinely be there."""
    generation = (
        tmp_path
        / wrapper_logs._wrapper_log_agent_leaf("worker")
        / "20260804T120000000Z-0123456789abcdef0123456789abcdef"
    )
    generation.mkdir(parents=True)
    lock = wrapper_logs._acquire_active_generation_lock(generation)
    assert lock is not None
    # Simulate a crash, not a clean release: _release_active_generation_lock
    # itself unconditionally unlinks the marker, which would defeat this
    # test's setup - closing the fd directly drops the OS-level byte lock
    # (so _guard_wrapper_log_prune can re-acquire it) while leaving the
    # marker FILE in place, exactly like a wrapper that died without its
    # own cleanup running.
    os.close(lock.fd)
    marker = wrapper_logs._active_generation_lock_path(generation)
    assert marker.is_file()

    _poison_lstat(monkeypatch, generation)

    with wrapper_logs._guard_wrapper_log_prune(generation) as prunable:
        assert prunable is True

    assert marker.is_file(), (
        "the active lock was reclaimed for a generation whose readability "
        "could not be confirmed, not one confirmed absent"
    )


def test_second_active_lock_attempt_cannot_unlink_live_holders_marker(
    tmp_path: Path,
) -> None:
    generation = (
        tmp_path
        / wrapper_logs._wrapper_log_agent_leaf("worker")
        / "20260804T120000000Z-0123456789abcdef0123456789abcdef"
    )
    generation.mkdir(parents=True)
    first = wrapper_logs._acquire_active_generation_lock(generation)
    assert first is not None
    try:
        assert wrapper_logs._acquire_active_generation_lock(generation) is None
        assert first.path.is_file()
        with wrapper_logs._guard_wrapper_log_prune(generation) as prunable:
            # On POSIX record locks are process-scoped, so the same-process
            # probe can acquire. The pathname invariant is the direction
            # control here; cross-process exclusion is proved above.
            if os.name == "nt":
                assert prunable is False
    finally:
        wrapper_logs._release_active_generation_lock(first)


@pytest.mark.skipif(os.name != "nt", reason="generated supervisor is Windows-only")
def test_python_and_powershell_share_active_generation_byte_lock(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell Core is unavailable")
    generation = (
        tmp_path
        / wrapper_logs._wrapper_log_agent_leaf("worker")
        / "20260804T120000000Z-0123456789abcdef0123456789abcdef"
    )
    generation.mkdir(parents=True)
    marker = wrapper_logs._active_generation_lock_path(generation)
    script = tmp_path / "active-lock.ps1"
    script.write_text(
        """param([string]$Path, [string]$Mode)
$share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
$fileMode = if ($Mode -eq 'hold') { [IO.FileMode]::CreateNew } else { [IO.FileMode]::Open }
$stream = New-Object IO.FileStream($Path, $fileMode, [IO.FileAccess]::ReadWrite, $share)
try {
  if ($stream.Length -lt 1) { $stream.SetLength(1) }
  try {
    $stream.Lock(0, 1)
    [Console]::Out.WriteLine('acquired')
    [Console]::Out.Flush()
    if ($Mode -eq 'hold') { $null = [Console]::In.ReadLine() }
    $stream.Unlock(0, 1)
  } catch {
    [Console]::Out.WriteLine('busy')
    [Console]::Out.Flush()
  }
} finally {
  $stream.Dispose()
}
""",
        encoding="utf-8",
    )

    python_lock = wrapper_logs._acquire_active_generation_lock(generation)
    assert python_lock is not None
    try:
        probe = subprocess.run(
            [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script), str(marker), "probe"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == "busy"
    finally:
        wrapper_logs._release_active_generation_lock(python_lock)

    holder = subprocess.Popen(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script), str(marker), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "acquired"
        with wrapper_logs._guard_wrapper_log_prune(generation) as prunable:
            assert prunable is False
        assert holder.stdin is not None
        holder.stdin.write("exit\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
        with wrapper_logs._guard_wrapper_log_prune(generation) as prunable:
            assert prunable is True
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
        marker.unlink(missing_ok=True)


def test_direct_allocator_refuses_a_symlinked_root_ancestor(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    linked_state = tmp_path / "linked-state"
    try:
        linked_state.symlink_to(redirected, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "LOCALAPPDATA": str(linked_state),
        "XDG_STATE_HOME": str(linked_state),
        "USERPROFILE": str(home),
        "HOME": str(home),
    }
    preferred = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform=os.name,
        environ=env,
    )

    targets = wrapper_logs._allocate_wrapper_log_targets(
        checkout,
        "worker",
        env,
    )

    assert targets is not None
    assert targets.root != preferred.resolve()
    assert not any(redirected.rglob("stdout.log"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction guard")
def test_direct_allocator_refuses_a_configured_state_junction(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    linked_state = tmp_path / "linked-state"
    home = tmp_path / "home"
    home.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None, "Windows PowerShell is required for junction coverage"
    junction_script = tmp_path / "create-junction.ps1"
    junction_script.write_text(
        "param([string]$Link, [string]$Target)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null\n",
        encoding="utf-8-sig",
    )
    creation = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(junction_script),
            str(linked_state),
            str(redirected),
        ],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert creation.returncode == 0, creation.stderr
    assert linked_state.is_dir()
    assert (
        linked_state.lstat().st_file_attributes
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )

    env = {
        "LOCALAPPDATA": str(linked_state),
        "USERPROFILE": str(home),
        "TEMP": str(temp),
        "TMP": str(temp),
    }
    try:
        targets = wrapper_logs._allocate_wrapper_log_targets(
            checkout,
            "worker",
            env,
        )

        assert targets is not None
        assert targets.root.is_relative_to(home.resolve())
        assert targets.stdout_path.is_file()
        assert targets.stderr_path.is_file()
        assert not any(redirected.rglob("stdout.log"))
        assert not any(redirected.rglob("stderr.log"))
    finally:
        if linked_state.exists():
            linked_state.rmdir()


def test_restrictive_file_opener_bakes_0o600_into_os_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 22: verifies the ARGUMENT _open_tail's opener passes to
    os.open, independent of platform - the actual POSIX permission
    enforcement (mode bits meaning something real on disk) is checked
    separately, skipped on Windows, in
    test_bounded_stream_tee_creates_tail_path_restrictively_not_via_later_chmod."""
    recorded = {}
    real_open = os.open

    def spy(path: str, flags: int, mode: int) -> int:
        # No permissive default: _restrictive_file_opener always passes an
        # explicit mode, and a stray default here (e.g. 0o777) is itself
        # the shape CodeQL flags as an overly permissive mask on open(),
        # whether or not this test's own call path ever reaches it.
        recorded["mode"] = mode
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", spy)
    path = tmp_path / "probe-file"
    fd = wrapper_logs._restrictive_file_opener(str(path), os.O_WRONLY | os.O_CREAT)
    os.close(fd)

    assert recorded["mode"] == 0o600


def test_bounded_stream_tee_keeps_each_file_and_generation_within_cap(
    tmp_path: Path,
) -> None:
    original = io.StringIO()
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("old-" * 3000)
    tee.write("TERMINAL-SENTINEL\n")
    tee.flush()
    tee.close()

    files = _log_glob(tmp_path, "stdout.log*")
    assert files
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 3072
    assert len(original.getvalue().encode("utf-8")) <= 1024
    assert "TERMINAL-SENTINEL" in "".join(
        path.read_text(encoding="utf-8") for path in files
    )


def test_bounded_stream_tee_direct_mode_keeps_the_console_unbounded(
    tmp_path: Path,
) -> None:
    base = tmp_path / "stdout.log"
    file_stream = base.open("a", encoding="utf-8", buffering=1)
    console = io.StringIO()
    tee = wrapper_logs.BoundedStreamTee(
        file_stream,
        base,
        max_bytes=4096,
        segment_count=4,
        mirror=console,
    )
    output = "console-output-" * 1000

    tee.write(output)
    tee.close()
    file_stream.close()

    assert console.getvalue() == output
    files = _log_glob(tmp_path, "stdout.log*")
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096


def test_bounded_stream_tee_newline_heavy_stream_stays_within_cap_on_disk(
    tmp_path: Path,
) -> None:
    """Finding B (PR 98 connector re-review, head 4323e20): the budget must be
    measured against what actually lands on disk, not the pre-translation
    UTF-8 length. A REAL text-mode file (unlike io.StringIO, used elsewhere in
    this file) applies the platform's newline translation on write - on
    Windows each accounted "\\n" becomes two bytes ("\\r\\n") on disk, so a
    newline-heavy stream could blow the per-file cap by nearly 2x while the
    accounting still believed it was exactly at the cap."""
    original_path = tmp_path / "original-stdout.txt"
    original = original_path.open("w", encoding="utf-8", newline=None)
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("x\n" * 3000)
    tee.flush()
    tee.close()
    original.close()

    assert original_path.stat().st_size <= tee.segment_bytes


def test_bounded_stream_tee_line_buffered_original_flushes_on_newline(
    tmp_path: Path,
) -> None:
    """I4 (PR 98 cold review): writing straight to self._original.buffer
    (added to fix the CRLF cap overrun above) bypasses TextIOWrapper's own
    line-buffering entirely - stderr is line-buffered by default, so a
    diagnostic line written just before an uncatchable SIGKILL would sit
    unflushed in the underlying BufferedWriter's own (larger, not
    newline-triggered) buffer and never reach disk, defeating the entire
    reason this module exists. Read back through a SEPARATE file handle,
    with no explicit flush() call anywhere in this test, to prove the bytes
    actually reached the OS level rather than merely Python's own buffer."""
    original_path = tmp_path / "original-stderr.txt"
    original = original_path.open(
        "w", encoding="utf-8", newline=None, buffering=1
    )
    assert original.line_buffering
    base = tmp_path / "stderr.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("final diagnostic before SIGKILL\n")
    # No tee.flush() / original.flush() here - simulating the kill landing
    # immediately after this write, before anything explicit could flush.
    on_disk = original_path.read_text(encoding="utf-8")

    tee.close()
    original.close()

    assert "final diagnostic before SIGKILL" in on_disk


def test_bounded_stream_tee_tail_flushes_after_each_write(tmp_path: Path) -> None:
    """I4 remnant (PR 98 cold review, round 3): the sibling test above fixed
    the ORIGINAL stream's flush - the tail ring's own files are raw binary
    BufferedWriters with no per-write flush of their own. Once the base
    segment's forwarding budget is spent, every further diagnostic write
    lands ONLY in the tail ring, so a small write can sit in that
    BufferedWriter's own internal buffer and never reach disk until an
    explicit flush()/close() or the buffer filling completely - the exact
    same durability gap, just one layer over. Read back through a SEPARATE
    file handle with zero explicit flush() calls anywhere in this test."""
    original = io.StringIO()
    base = tmp_path / "stderr.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("tail diagnostic\n")
    tail_path = tmp_path / "stderr.log.1"
    on_disk = tail_path.read_text(encoding="utf-8")

    tee.close()

    assert "tail diagnostic" in on_disk


def test_bounded_stream_tee_failure_discards_excess_without_breaking_wrapper(
    tmp_path: Path,
) -> None:
    original = io.StringIO()
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    tee = wrapper_logs.BoundedStreamTee(
        original,
        blocked_parent / "stdout.log",
        max_bytes=4096,
        segment_count=4,
    )

    assert tee.write("x" * 10_000) == 10_000
    tee.flush()
    tee.close()

    assert len(original.getvalue().encode("utf-8")) == 1024


def test_bounded_stream_tee_tail_accounts_before_writing_not_after(
    tmp_path: Path,
) -> None:
    """Finding C (PR 98 connector re-review, head 6495534): a terminating
    signal's handler runs between bytecode instructions, so it can only land
    in the gap between the tail write and the size accounting that follows
    it - never inside the write call itself. Accounting AFTER the write
    leaves self._tail_size understated once the real bytes are already on
    disk if a signal lands in that gap, so the next write believes it has
    more room than it does and can push a segment past segment_bytes by up
    to another chunk. This interacts with the prior round's SIGTERM fix -
    making a terminating signal actually unwind (and log) promptly, instead
    of hanging, means a diagnostic write reaching this exact gap is now
    something normal termination can hit, not an exotic timing accident.

    Simulated directly rather than via a real OS signal: a tail whose
    write() raises must still have updated the accounting BEFORE that
    raise, proving accounting is ordered ahead of the write - the same
    ordering a signal's handler firing right after write() returns would
    otherwise be able to slip between."""
    original = io.StringIO()
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )
    tee._open_tail()
    real_tail = tee._tail

    class RaisingTail:
        def write(self, data: object) -> None:
            raise OSError("simulated interruption during the tail write")

    tee._tail = RaisingTail()
    size_before = tee._tail_size
    with pytest.raises(OSError):
        tee._write_tail(b"x" * 100)
    assert tee._tail_size == size_before + 100, (
        "the tail's size accounting was not updated before the write that "
        "raised - a signal landing in that gap would understate it instead"
    )

    tee._tail = real_tail
    tee.close()


def test_bounded_stream_tee_resume_appends_to_newest_segment_instead_of_truncating(
    tmp_path: Path,
) -> None:
    """Round 21 (connector finding; withdraws the trade-off accepted in
    round 20): print_bounded_uncaught_exception's fresh, second tee used to
    always reset to segment .1 and open it with "wb" - truncating whatever
    was there regardless of whether it was the OLDEST content (as normal
    rotation would discard anyway) or the NEWEST (a live wrapper's own
    lifecycle output, written moments before the crash). resume=True must
    find the segment the FIRST instance was actually writing to and APPEND
    to it instead.

    Round 29 correction: this only ever exercised the single-candidate
    case (one short write never rotates past .1), so it never actually
    proved which SELECTION mechanism resume used - see
    test_bounded_stream_tee_resume_follows_cursor_not_misleading_mtime for
    the adversarial case that does."""
    base = tmp_path / "stderr.log"
    original = io.StringIO()
    first = wrapper_logs.BoundedStreamTee(
        original, base, max_bytes=4096, segment_count=4,
    )
    first.write("FIRST-INSTANCE-NEWEST-LIFECYCLE-OUTPUT\n")
    first.close()

    tail_files = _log_glob(tmp_path, "stderr.log.*")
    assert len(tail_files) == 1
    newest = tail_files[0]
    newest_content_before = newest.read_bytes()
    assert b"FIRST-INSTANCE-NEWEST-LIFECYCLE-OUTPUT" in newest_content_before

    second = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4, resume=True,
    )
    second.write("SECOND-INSTANCE-CRASH-SENTINEL\n")
    second.close()

    combined = newest.read_bytes()
    assert combined.startswith(newest_content_before), (
        "resume must APPEND to the newest segment, not truncate it"
    )
    assert b"SECOND-INSTANCE-CRASH-SENTINEL" in combined


def test_bounded_stream_tee_resume_follows_cursor_not_misleading_mtime(
    tmp_path: Path,
) -> None:
    """Round 29 connector finding (P2, against the round-21 fix above):
    choosing the current segment by st_mtime is ambiguous on filesystems
    with coarse timestamp resolution and wrong after a backward clock
    adjustment - on a tie the highest index wins even when a DIFFERENT
    suffix holds the newest output, and if that wrongly-chosen suffix is
    already near full, the next write truncates the segment that actually
    held the newest crash evidence. Proves the fix by constructing exactly
    that adversarial mtime shape and showing resume follows the persisted
    cursor instead: segment .1 is made to look newer BY MTIME than the
    segment the first instance actually finished writing to (.2); resume
    must still append to .2, the one recorded in the cursor file, not .1."""
    base = tmp_path / "stderr.log"
    first = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4,
    )
    # segment_bytes = 4096 // 4 = 1024. Fill .1 then rotate into .2 - the
    # cursor now records index 1 (".2"), while .1 sits untouched since.
    first.write("A" * 1024)
    first.write("FIRST-INSTANCE-NEWEST-LIFECYCLE-OUTPUT\n")
    first.close()

    tail_files = {p.name: p for p in _log_glob(tmp_path, "stderr.log.*")}
    assert set(tail_files) == {"stderr.log.1", "stderr.log.2"}
    cursor_before = (tmp_path / "stderr.log.cursor").read_text(encoding="utf-8")
    assert cursor_before == "1"          # 0-based index for .2, the current one
    newest_content_before = tail_files["stderr.log.2"].read_bytes()
    assert b"FIRST-INSTANCE-NEWEST-LIFECYCLE-OUTPUT" in newest_content_before

    # Make the OLDER, already-superseded .1 look newer than .2 by mtime -
    # exactly the ambiguous shape a coarse filesystem clock or a backward
    # wall-clock adjustment can produce for real, without needing either.
    newer_than_now = os.path.getmtime(tail_files["stderr.log.2"]) + 5
    os.utime(tail_files["stderr.log.1"], (newer_than_now, newer_than_now))
    assert (
        tail_files["stderr.log.1"].stat().st_mtime
        > tail_files["stderr.log.2"].stat().st_mtime
    )

    second = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4, resume=True,
    )
    second.write("SECOND-INSTANCE-CRASH-SENTINEL\n")
    second.close()

    # .2 (the cursor-recorded segment) got the append - the mtime-newer .1
    # was never touched, still holds only its original "A"*1024.
    combined = tail_files["stderr.log.2"].read_bytes()
    assert combined.startswith(newest_content_before), (
        "resume must follow the recorded cursor, not the misleading mtime"
    )
    assert b"SECOND-INSTANCE-CRASH-SENTINEL" in combined
    assert tail_files["stderr.log.1"].read_bytes() == b"A" * 1024


def test_bounded_stream_tee_resume_appends_not_truncates_when_no_cursor_exists(
    tmp_path: Path,
) -> None:
    """Round 29 rereview: the missing-cursor fallback - true of every
    generation already on disk the moment cursor-recording ships - must
    never truncate. My first draft treated "no cursor" as "resume was
    never requested at all", defaulting straight back to the ORIGINAL
    truncate-on-first-write behavior - which is strictly WORSE than the
    mtime scan it replaced on precisely the generations that predate this
    fix (every one on disk at upgrade time), and worse specifically in the
    crash path this module exists for: the mtime scan was USUALLY right,
    "no cursor -> wb" is wrong every time. Simulates a pre-fix generation
    directly - write .1's content by hand, with NO cursor file, exactly
    what an old-code generation looks like - then resumes into it and
    confirms the existing content survives, appended to rather than
    overwritten."""
    base = tmp_path / "stderr.log"
    pre_fix_tail = base.with_name("stderr.log.1")
    pre_fix_tail.write_bytes(b"PRE-FIX-GENERATION-CONTENT-NO-CURSOR-EXISTS\n")
    assert not (tmp_path / "stderr.log.cursor").exists()

    second = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4, resume=True,
    )
    second.write("SECOND-INSTANCE-CRASH-SENTINEL\n")
    second.close()

    combined = pre_fix_tail.read_bytes()
    assert combined.startswith(b"PRE-FIX-GENERATION-CONTENT-NO-CURSOR-EXISTS\n"), (
        "a missing cursor must never truncate pre-existing content"
    )
    assert b"SECOND-INSTANCE-CRASH-SENTINEL" in combined


def test_bounded_stream_tee_resume_survives_a_stale_cursor_after_rewrite_failure(
    tmp_path: Path,
) -> None:
    """PR 98 round 29 connector finding, against 7a4ee43: if the
    best-effort cursor rewrite in _write_tail_cursor fails right after
    _advance_tail has already opened a new suffix, the STALE cursor
    (still pointing at the segment just rotated OUT of, which is already
    full) survives on disk. A later resume=True instance trusts it,
    immediately advances past the reported-full segment on its first
    write, and - before this fix - opened the NEXT suffix with "wb",
    destroying whatever the crashed instance had already written there
    before its own cursor-rewrite failure ever happened.

    Simulates exactly that: .1 hand-written full (segment_bytes), .2
    hand-written with real, never-recorded content (the segment the
    crashed instance HAD rotated into but never got to confirm), and a
    cursor file left stale at "0" (still naming .1). A resumed instance
    must find its OWN first write immediately cascades past the
    reported-full .1 into .2 - and append there, not truncate, because
    THIS instance has never itself visited .2 before, regardless of what
    the stale cursor said about .1."""
    base = tmp_path / "stderr.log"
    segment_bytes = 4096 // 4
    (tmp_path / "stderr.log.1").write_bytes(b"A" * segment_bytes)
    (tmp_path / "stderr.log.2").write_bytes(b"INSTANCE-A-REAL-UNRECORDED-CONTENT\n")
    (tmp_path / "stderr.log.cursor").write_text("0", encoding="utf-8")  # stale

    second = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4, resume=True,
    )
    second.write("SECOND-INSTANCE-CRASH-SENTINEL\n")
    second.close()

    seg2 = (tmp_path / "stderr.log.2").read_bytes()
    assert seg2.startswith(b"INSTANCE-A-REAL-UNRECORDED-CONTENT\n"), (
        "a stale cursor's cascading rotation must never truncate a segment "
        "this instance has not itself visited before"
    )
    assert b"SECOND-INSTANCE-CRASH-SENTINEL" in seg2
    # .1 (where the stale cursor pointed) is untouched-then-appended-to on
    # this instance's own first visit, same guarantee as the no-cursor case.
    assert (tmp_path / "stderr.log.1").read_bytes().startswith(b"A" * segment_bytes)


def test_bounded_stream_tee_resume_still_rotates_and_truncates_the_next_segment(
    tmp_path: Path,
) -> None:
    """A resumed instance must still behave like a normal one once the
    resumed segment fills up - truncating the NEXT segment in ring order is
    the correct, expected ring-buffer behavior, only the FIRST open (the
    resumed one) is special."""
    base = tmp_path / "stderr.log"
    first = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4,
    )
    # Long enough to cycle through all 3 tail segments more than once, so
    # every one of them ends up holding some of this text - avoids having
    # to manufacture a stale file with a manually-set mtime.
    first.write("old-content-that-must-be-evicted-by-rotation " * 200)
    first.close()

    tail_files = _log_glob(tmp_path, "stderr.log.*")
    assert len(tail_files) == 3
    before = {p.name: p.read_bytes() for p in tail_files}
    assert all(b"old-content" in content for content in before.values())

    second = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4, resume=True,
    )
    # Enough to force a full extra rotation cycle past the resumed segment.
    second.write("x" * 3500)
    second.close()

    after = {p.name: p.read_bytes() for p in tail_files}
    evicted = [
        name for name, content in after.items() if b"old-content" not in content
    ]
    assert evicted, "at least one segment must have been freshly rotated into"
    # Every freshly-rotated segment must be a clean truncate - no stale
    # old-content leaking in alongside the new "x"s.
    for name in evicted:
        assert after[name].strip(b"x") == b""


def test_bounded_stream_tee_tail_rotates_before_splitting_a_utf8_code_point(
    tmp_path: Path,
) -> None:
    """Round 11 connector finding, the serious one: _write_tail sliced the
    encoded byte buffer at `available` with no regard for UTF-8 code point
    boundaries. When a multi-byte character's leading byte is the LAST byte
    that fits in the current segment, the old code split its encoded bytes
    across two files - the first segment ends with a dangling lead byte,
    the next begins with an orphaned continuation byte, and a strict UTF-8
    reader then fails to open EITHER file - not merely mis-render one
    character, the whole diagnostic becomes unopenable.

    Constructed at the byte level, not hoped into: 127 ASCII bytes exactly
    fill the segment to available=1, so e-acute (2-byte UTF-8) lands with
    its lead byte as the very last byte that would fit. Asserted on the
    raw bytes read back from disk, each segment decoded standalone - a
    string round-trip through Python would paper over exactly this."""
    original = io.StringIO()
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=32,  # segment_bytes = 4096 // 32 = 128
    )
    assert tee.segment_bytes == 128

    payload = ("A" * 127) + "é" + "BB"  # 127 + 2 + 2 = 131 encoded bytes
    tee.write(payload)
    tee.close()

    seg1 = (tmp_path / "stdout.log.1").read_bytes()
    seg2 = (tmp_path / "stdout.log.2").read_bytes()

    seg1.decode("utf-8")
    seg2.decode("utf-8")

    assert seg1 == b"A" * 127
    assert seg2 == "éBB".encode("utf-8")


def test_signal_diagnostic_is_deferred_until_stream_write_unwinds(
    tmp_path: Path,
) -> None:
    original = io.StringIO()
    tee = wrapper_logs.BoundedStreamTee(
        original,
        tmp_path / "stderr.log",
        max_bytes=4096,
        segment_count=4,
    )
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=tee,
        clock=lambda: NOW,
    )

    lifecycle.defer_signal(signal.SIGTERM, terminating=True)

    tee._lock.acquire()
    try:
        assert original.getvalue() == ""
    finally:
        tee._lock.release()
    lifecycle.flush_deferred_signal()
    try:
        assert '"event":"wrapper_signal_received"' in original.getvalue()
    finally:
        tee.close()


def test_runtime_transitions_emit_only_factual_lifecycle_lines(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )

    writer.idle()
    writer.starting(message_id="msg-1", turn_id="turn-1")
    writer.active(456, "start-456")
    writer.progress()
    lifecycle.child_exited(456, "start-456", 7)
    writer.terminal(runtime.OUTCOME_FAILED)
    writer.idle()
    lifecycle.wrapper_exited(0, reason="loop_returned")

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["event"] for row in rows] == [
        "waiting_for_mail",
        "turn_started",
        "child_spawned",
        "child_exited",
        "turn_ended",
        "waiting_for_mail",
        "wrapper_exited",
    ]
    child_exit = rows[3]
    assert child_exit["agent"] == "worker"
    assert child_exit["wrapper_pid"] == 123
    assert child_exit["turn_generation"] == 1
    assert child_exit["turn_id"] == "turn-1"
    assert child_exit["cli_launcher_pid"] == 456
    assert child_exit["progress_sequence"] == 1
    assert child_exit["last_progress_at"] is not None
    assert child_exit["child_pid"] == 456
    assert child_exit["return_code"] == 7
    assert rows[4]["last_outcome"] == runtime.OUTCOME_FAILED
    assert all(
        forbidden not in stream.getvalue().casefold()
        for forbidden in (
            '"healthy"',
            '"ok"',
            '"alive"',
            '"progressing"',
            "working normally",
        )
    )


def test_lifecycle_sink_failure_never_breaks_runtime_transition(
    tmp_path: Path,
) -> None:
    class BrokenStream:
        def write(self, _value: str) -> int:
            raise OSError("simulated full disk")

        def flush(self) -> None:
            raise OSError("simulated full disk")

    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=BrokenStream(),
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )

    record = writer.idle()

    assert record["phase"] == runtime.PHASE_IDLE
    assert runtime.read_runtime(
        tmp_path,
        "worker",
        now_epoch=NOW,
    )["status"] == runtime.STATUS_VALID


def test_mid_turn_exception_trail_keeps_turn_and_child_without_fabricated_end(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )
    writer.starting(message_id="msg-1", turn_id="turn-41")
    writer.active(456, "start-456")

    lifecycle.wrapper_exception(RuntimeError("simulated abrupt wrapper failure"))

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["event"] for row in rows] == [
        "turn_started",
        "child_spawned",
        "wrapper_exception",
    ]
    assert rows[-1]["turn_id"] == "turn-41"
    assert rows[-1]["cli_launcher_pid"] == 456
    assert rows[-1]["exception_type"] == "RuntimeError"
    assert "turn_ended" not in {row["event"] for row in rows}


def test_dead_letter_disposition_is_not_reported_as_a_driven_turn(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )

    writer.idle()
    writer.dead_letter(message_id="msg-dead")

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["event"] for row in rows] == [
        "waiting_for_mail",
        "message_dead_lettered",
    ]
    assert rows[-1]["message_id"] == "msg-dead"
    assert rows[-1]["last_outcome"] == runtime.OUTCOME_DEAD_LETTER


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM unavailable")
def test_signal_logging_is_deferred_and_existing_handler_is_restored() -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
        wrapper_pid=123,
    )
    calls: list[tuple[int, int]] = []

    def prior(signum: int, frame: object) -> None:
        calls.append((signum, len(stream.getvalue().splitlines())))

    old = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, prior)
    try:
        with wrapper_logs.capture_termination_signals(lifecycle):
            installed = signal.getsignal(signal.SIGTERM)
            assert callable(installed)
            installed(signal.SIGTERM, None)
        assert signal.getsignal(signal.SIGTERM) is prior
    finally:
        signal.signal(signal.SIGTERM, old)

    assert calls == [(signal.SIGTERM, 0)]
    row = json.loads(stream.getvalue())
    assert row["event"] == "wrapper_signal_received"
    assert row["signal"] == int(signal.SIGTERM)
    assert row["terminating"] is False
    assert lifecycle.terminal_emitted is False


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM unavailable")
def test_ignored_signal_is_not_logged_as_wrapper_termination() -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
        wrapper_pid=123,
    )
    old = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        with wrapper_logs.capture_termination_signals(lifecycle):
            installed = signal.getsignal(signal.SIGTERM)
            assert callable(installed)
            installed(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, old)

    assert stream.getvalue() == ""
    assert lifecycle.terminal_emitted is False


def test_stream_environment_context_restores_process_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)
    before_out = io.StringIO()
    before_err = io.StringIO()
    monkeypatch.setattr("sys.stdout", before_out)
    monkeypatch.setattr("sys.stderr", before_err)

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="a" * 32,
    ):
        import sys

        assert sys.stdout is not before_out
        assert sys.stderr is not before_err
        sys.stdout.write("child-output\n")
        sys.stderr.write("child-error\n")

    import sys

    assert sys.stdout is before_out
    assert sys.stderr is before_err
    assert "child-output" in "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stdout.log*")
    )
    assert "child-error" in "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stderr.log*")
    )
    assert os.environ[wrapper_logs.ENV_STDOUT_PATH] == str(out_path)


def test_stream_environment_confirms_the_generation_from_inside_the_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 23: replaces the round-19/20/21 predictive probe entirely - the
    supervisor no longer forecasts whether a launch will reach cmd_wrap, it
    waits for THIS wrapper process to say so by evidence. Simulates the
    supervisor's own .pending marker (New-WrapperLogPendingMarker) to prove
    the wrapper transitions it to .committed itself, at the point
    authentication has already succeeded and both tees are live - not
    merely that the marker exists at some point after the fact."""
    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / ".pending").write_bytes(b"")
    out_path = generation / "stdout.log"
    err_path = generation / "stderr.log"
    nonce = "a" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce=nonce,
    ):
        # Confirmed WHILE still inside the context - proving this happens
        # at stream-install time, not merely "eventually, somehow".
        assert (generation / ".committed").exists()
        assert not (generation / ".pending").exists()

    assert (generation / ".committed").exists()


def test_authenticated_supervisor_targets_record_their_actual_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    root = tmp_path / "preallocated-root"
    generation = (
        root
        / wrapper_logs._wrapper_log_agent_leaf("worker")
        / "20260804T120000000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    generation.mkdir(parents=True)
    (generation / ".pending").write_bytes(b"")
    stdout_path = generation / "stdout.log"
    stderr_path = generation / "stderr.log"
    stdout_path.write_bytes(b"")
    stderr_path.write_bytes(b"")
    nonce = "a" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(stdout_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(stderr_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce=nonce,
        project_root=project,
        agent="worker",
    ):
        sys.stderr.write("supervised target accepted\n")

    location = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")
    assert location["status"] == "observed"
    assert Path(str(location["root"])) == root.resolve()
    assert Path(str(location["generation_dir"])) == generation.resolve()


def test_invalid_location_path_resolution_cannot_crash_report_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path / "project")
    store.init(["worker"])
    location_path = wrapper_logs._wrapper_log_location_path(
        store.state_dir,
        "worker",
    )
    location_path.parent.mkdir(parents=True)
    location_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agent": "worker",
                "root": str(tmp_path / "resolution-loop"),
                "generation_dir": str(tmp_path / "generation"),
                "stdout": str(tmp_path / "stdout.log"),
                "stderr": str(tmp_path / "stderr.log"),
            }
        ),
        encoding="utf-8",
    )
    real_resolve = Path.resolve

    def fail_one_path(path: Path, *args, **kwargs) -> Path:
        if path.name == "resolution-loop":
            raise RuntimeError("symlink loop")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_one_path)

    location = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")

    assert location["status"] == "invalid"
    assert location["root"] is None


def test_stream_environment_without_a_matching_nonce_never_touches_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch that never authenticates never installs streams at all -
    it must not confirm a generation it was never actually authorized to
    write into either."""
    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / ".pending").write_bytes(b"")
    out_path = generation / "stdout.log"
    err_path = generation / "stderr.log"
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="b" * 32,
    ):
        pass

    assert not (generation / ".committed").exists()
    assert (generation / ".pending").exists()


def test_cmd_wrap_records_setup_exception_before_loop_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "a" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def fail_setup(_args: argparse.Namespace) -> int:
        raise RuntimeError("setup failed before run_loop")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", fail_setup)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    # New area (cold review): a bare re-raise here lets the original
    # RuntimeError reach main()'s uncaught-exception path, which prints its
    # OWN traceback AFTER streams are restored - to the unbounded raw file.
    # That is not a regression: the bounded diagnostic capture below (the
    # traceback that ends up IN the wrapper log tail) already happened,
    # via traceback.print_exc(file=sys.stderr), while sys.stderr was still
    # the bounded tee - BEFORE this raise. A second, redundant traceback on
    # the raw console afterward is exactly what an uncaught exception
    # should show an operator; #117's bounded capture does not depend on
    # suppressing it. Round 18: cmd_wrap propagates the ORIGINAL exception
    # here (not a converted SystemExit(1)) so an embedder or test runner
    # calling cli.main([...]) can catch RuntimeError specifically, inspect
    # it, and retry - the same contract main() had before this PR ever
    # touched cmd_wrap.
    with pytest.raises(RuntimeError, match="setup failed before run_loop"):
        cli.cmd_wrap(args)

    tail = "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exception"]
    assert rows[0]["exception_type"] == "RuntimeError"
    # The traceback itself must also survive - it is captured through the
    # bounded stream before cmd_wrap's context manager restores raw streams.
    assert "setup failed before run_loop" in tail
    assert "Traceback (most recent call last)" in tail


def test_cmd_wrap_routine_system_exit_emits_exited_fact_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cli._get_store's "not initialized" path writes its own actionable
    # diagnostic and calls sys.exit(2) directly - a routine, already-
    # explained exit, not a crash. Two regressions, in sequence:
    # (1) cmd_wrap's except block used to record wrapper_exception and
    # print a full Python traceback on top of it regardless of exception
    # type, turning a one-line diagnostic into crash-report noise; fixing
    # that by bare-raising on any SystemExit (2) overcorrected into
    # emitting NO lifecycle fact at all for this shape - no deferred signal
    # exists to have recorded one, unlike the signal-driven SystemExit
    # case - so the trail ended with no termination fact whatsoever,
    # indistinguishable from an OOM or a hard kill when reading the JSON
    # lines. Every termination path must emit exactly one termination
    # fact: here, a normalized wrapper_exited - not wrapper_exception,
    # since this was never an unexplained exception - and still no
    # traceback.
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "e" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def not_initialized(_args: argparse.Namespace) -> int:

        sys.stderr.write(
            "agenttalk: not initialized at X\n"
            "Run `agenttalk init --here` from the project root.\n"
        )
        raise SystemExit(2)

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", not_initialized)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_wrap(args)
    assert exc_info.value.code == 2

    tail = "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exited"]
    assert rows[0]["exit_code"] == 2
    assert rows[0]["reason"] == "system_exit"
    assert "not initialized" in tail
    assert "Traceback (most recent call last)" not in tail


@pytest.mark.parametrize(
    "raise_exc,expected_code,expected_reason,expected_text",
    [
        (lambda: KeyboardInterrupt(), 130, "keyboard_interrupt", "interrupted"),
        (lambda: ValueError("bad value"), 2, "mapped_cli_exception", "bad value"),
        (lambda: FileNotFoundError("missing.toml"), 2, "mapped_cli_exception",
         "missing.toml"),
        (lambda: OSError("disk full"), 2, "mapped_cli_exception", "disk full"),
    ],
    ids=["KeyboardInterrupt", "ValueError", "FileNotFoundError", "OSError"],
)
def test_cmd_wrap_routine_exception_types_skip_crash_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_exc,
    expected_code: int,
    expected_reason: str,
    expected_text: str,
) -> None:
    # I2's shape, one layer up: main() already has a concise, actionable
    # diagnostic for KeyboardInterrupt and (ValueError, FileNotFoundError,
    # OSError) - cmd_wrap's except block converts them to the SAME exit
    # codes at the bottom, but the crash-reporting block above that
    # conversion ran unconditionally for anything that wasn't SystemExit,
    # so an OSError got wrapper_exception + a full Python traceback BEFORE
    # being converted to the CLI's normal one-line error. The property:
    # the crash path runs ONLY for exceptions not in this known,
    # concise-diagnostic set - enumerated as a set (this parametrize), not
    # patched type by type. A future exception type that gains a concise
    # diagnostic elsewhere and is not added here must fail this test, not
    # silently fall through to the crash path.
    #
    # Round 17 connector finding: this used to assert cmd_wrap RAISES
    # SystemExit(expected_code) for all four - which was itself the bug.
    # main() previously RETURNED an int for exactly these two classes
    # (KeyboardInterrupt, and ValueError/FileNotFoundError/OSError); a
    # raised SystemExit is not an Exception subclass, so it bypasses
    # main()'s own except clauses entirely and can escape a caller that
    # invokes cli.main([...]) programmatically expecting an int back
    # (an embedder, or a test runner). cmd_wrap now RETURNS the code
    # instead, restoring that contract regardless of whether cmd_wrap's
    # own exception handling sits in front of main()'s.
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "f" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def raiser(_args: argparse.Namespace) -> int:
        raise raise_exc()

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raiser)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    assert cli.cmd_wrap(args) == expected_code

    tail = "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exited"]
    assert rows[0]["exit_code"] == expected_code
    assert rows[0]["reason"] == expected_reason
    assert expected_text in tail
    assert "Traceback (most recent call last)" not in tail


@pytest.mark.parametrize(
    "raise_exc,expected_return,expected_raise",
    [
        (lambda: SystemExit(2), None, SystemExit),
        (lambda: KeyboardInterrupt(), 130, None),
        (lambda: ValueError("bad value"), 2, None),
        (lambda: FileNotFoundError("missing.toml"), 2, None),
        (lambda: OSError("disk full"), 2, None),
        (lambda: RuntimeError("truly unexpected"), None, RuntimeError),
    ],
    ids=["SystemExit", "KeyboardInterrupt", "ValueError", "FileNotFoundError",
         "OSError", "RuntimeError"],
)
def test_cmd_wrap_and_main_exception_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_exc,
    expected_return: int | None,
    expected_raise: type[BaseException] | None,
) -> None:
    """Round 17/18 connector findings: the stated contract, tested end to
    end through cli.main() - not just cmd_wrap in isolation - because the
    property that actually matters is what an embedder or a test runner
    calling cli.main([...]) programmatically observes, and that can only
    be proven by calling main() itself.

    THE CONTRACT (exception classes that can reach cmd_wrap's handler):

    | class                        | cmd_wrap/main()   | lifecycle fact       | tb  |
    |-------------------------------|-------------------|-----------------------|-----|
    | SystemExit (deliberate)       | raises (same)     | wrapper_exited(code,"system_exit") | no |
    | KeyboardInterrupt             | returns 130       | wrapper_exited(130,"keyboard_interrupt") | no |
    | ValueError/FileNotFoundError/ | returns 2         | wrapper_exited(2,"mapped_cli_exception") | no |
    | OSError                       |                   |                       |     |
    | anything else (unexpected)    | raises (original) | wrapper_exception(exc) | yes |

    Every row now preserves main()'s PRE-#117 contract exactly - no
    behavior change anywhere except that rows 2 and 3 are now actually
    correct (they used to raise SystemExit, breaking main()'s contract
    for those two classes specifically):

    - SystemExit: main() has never caught bare SystemExit (no except
      clause for it, ever) - letting it propagate matches what happens
      with no exception handling here at all.
    - KeyboardInterrupt, ValueError/FileNotFoundError/OSError: main()
      RETURNED an int for these before this PR touched cmd_wrap.
      Raising SystemExit(code) for them bypassed main()'s own except
      clauses (SystemExit is not an Exception subclass) and let it
      escape a caller expecting an int back - round 17's fix.
    - Anything else (unexpected): main() had no RETURN contract for this
      class either - it let the ORIGINAL exception type propagate
      uncaught. Converting it to SystemExit(1) (round 17's own crash-
      design choice) destroyed the type information an embedder needs
      and substituted a class an ordinary `except Exception` would miss
      - round 18's fix: propagate the original after recording the
      crash fact, restoring the SAME pre-#117 status quo, not a new
      contract. The "one known exit code for any crash" goal was a
      CONSOLE concern the console gets for free (Python exits 1 on any
      uncaught exception regardless), so nothing is lost there either."""
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "3" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def raiser(_args: argparse.Namespace) -> int:
        raise raise_exc()

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raiser)

    argv = ["--supervisor-launch-nonce", nonce, "wrap", "--for", "worker"]
    if expected_raise is SystemExit:
        with pytest.raises(SystemExit) as exc_info:
            cli.main(argv)
        assert exc_info.value.code == 2
    elif expected_raise is not None:
        with pytest.raises(expected_raise):
            cli.main(argv)
    else:
        assert cli.main(argv) == expected_return


def test_cmd_wrap_unclassified_exception_still_gets_crash_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half of the property: an exception type NOT in the known,
    # concise-diagnostic set must still fall through to the crash path -
    # this is the visible-omission guard the enumeration exists for.
    #
    # Round 18: this used to assert cmd_wrap converts the exception to
    # SystemExit(1) - the same contract violation as rows 2/3 (KeyboardInterrupt,
    # ValueError/FileNotFoundError/OSError), one row down. main() never had
    # a RETURN contract for an unexpected type - it would have let the
    # ORIGINAL type propagate uncaught - so cmd_wrap must propagate the
    # original RuntimeError here too, after recording the crash fact, not
    # substitute a SystemExit an embedder's `except Exception` would miss.
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "9" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def raiser(_args: argparse.Namespace) -> int:
        raise RuntimeError("truly unexpected")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raiser)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    with pytest.raises(RuntimeError, match="truly unexpected"):
        cli.cmd_wrap(args)

    tail = "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exception"]
    assert rows[0]["exception_type"] == "RuntimeError"
    assert "Traceback (most recent call last)" in tail


def test_cmd_wrap_entry_bounds_python_level_wrapper_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    out_path.write_text("B" * 1024, encoding="utf-8")
    err_path.write_text("", encoding="utf-8")
    nonce = "c" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def emit_output(_args: argparse.Namespace) -> int:

        sys.stdout.write("x" * 20_000)
        sys.stdout.write("FINAL-SENTINEL\n")
        return 0

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", emit_output)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    assert cli.cmd_wrap(args) == 0

    files = _log_glob(tmp_path, "stdout.log*")
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096
    assert "FINAL-SENTINEL" in "".join(
        path.read_text(encoding="utf-8") for path in files
    )


def test_cmd_wrap_uncaught_exception_traceback_is_bounded_and_in_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    out_path.write_text("", encoding="utf-8")
    err_path.write_text("E" * 1024, encoding="utf-8")
    nonce = "d" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def blow_up(_args: argparse.Namespace) -> int:

        # The initial segment is already full; more chatter before the crash
        # forces several rotations, so the eventual traceback lands in a
        # tail segment rather than the (already-evicted) first one.
        sys.stderr.write("x" * 20_000)
        raise RuntimeError("TRACEBACK-SENTINEL-BOOM")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", blow_up)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    # Round 18: cmd_wrap now propagates the ORIGINAL RuntimeError rather
    # than converting it to SystemExit(1) - the bounded capture below is
    # unaffected either way, since it happens before this raise.
    with pytest.raises(RuntimeError, match="TRACEBACK-SENTINEL-BOOM"):
        cli.cmd_wrap(args)

    files = _log_glob(tmp_path, "stderr.log*")
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096
    tail = "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in files
    )
    assert "TRACEBACK-SENTINEL-BOOM" in tail
    assert "Traceback (most recent call last)" in tail


def test_print_bounded_uncaught_exception_writes_into_tail_ring_when_config_is_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    err_path.write_text("E" * 1024, encoding="utf-8")
    nonce = "f" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    monkeypatch.setattr(wrapper_logs, "_LAST_STDERR_LOG_CONFIG", None)

    # Simulate cmd_wrap's own tee lifecycle: installed, writes some real
    # lifecycle output (round 21: this is the content a naive fresh tee
    # would have truncated), then torn down normally by the unconditional
    # finally in installed_standard_streams_from_environment - exactly the
    # state the tee is in by the time agenttalk/__main__.py's top-level
    # fallback would ever run.
    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce=nonce,
    ):
        sys.stderr.write("LIVE-WRAPPER-LIFECYCLE-OUTPUT-BEFORE-THE-CRASH\n")

    try:
        raise RuntimeError("TOP-LEVEL-SENTINEL-BOOM")
    except RuntimeError:
        wrapper_logs.print_bounded_uncaught_exception()

    files = _log_glob(tmp_path, "stderr.log*")
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096
    tail = "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in files
    )
    assert "TOP-LEVEL-SENTINEL-BOOM" in tail
    assert "Traceback (most recent call last)" in tail
    # Round 21: the crash traceback's own bounded write must not have
    # truncated the lifecycle output the FIRST tee had already written -
    # only a genuinely full ring may evict it via normal rotation.
    assert "LIVE-WRAPPER-LIFECYCLE-OUTPUT-BEFORE-THE-CRASH" in tail
    # The raw file the supervisor redirects to must not receive this second
    # copy directly - only the bounded tail ring does.
    assert "TOP-LEVEL-SENTINEL-BOOM" not in err_path.read_text(encoding="utf-8")


def test_print_bounded_uncaught_exception_prints_normally_when_no_wrapper_log_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wrapper_logs, "_LAST_STDERR_LOG_CONFIG", None)
    captured = io.StringIO()
    monkeypatch.setattr("sys.stderr", captured)

    try:
        raise RuntimeError("MANUAL-RUN-SENTINEL")
    except RuntimeError:
        wrapper_logs.print_bounded_uncaught_exception()

    assert "MANUAL-RUN-SENTINEL" in captured.getvalue()
    assert "Traceback (most recent call last)" in captured.getvalue()


def _install_boundary_test_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nonce: str,
) -> tuple[Path, Path]:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    err_path.write_text("E" * 1024, encoding="utf-8")
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    monkeypatch.setattr(wrapper_logs, "_LAST_STDERR_LOG_CONFIG", None)

    def blow_up(_args: argparse.Namespace) -> int:

        sys.stderr.write("x" * 20_000)
        raise RuntimeError("BOUNDARY-SENTINEL-BOOM " + "z" * 20_000)

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", blow_up)
    return out_path, err_path


def test_console_main_bounds_the_top_level_traceback_and_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 20: agenttalk/__main__.py, the installed console script, and
    cli.py's own __main__ guard are three real top-level entry points that
    all need this SAME property - drives console_main() itself (the one
    shared function all three now call), not a hand-replicated copy of
    what any one of them does."""
    nonce = "a" * 32
    _out_path, err_path = _install_boundary_test_env(tmp_path, monkeypatch, nonce)

    result = cli.console_main(
        ["--supervisor-launch-nonce", nonce, "wrap", "--for", "worker"]
    )

    assert result == 1
    files = _log_glob(tmp_path, "stderr.log*")
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096


def test_main_still_propagates_original_exception_type_for_embedders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The embedder-safety property console_main was built to preserve:
    a program that imports cli and calls main([...]) directly never goes
    through console_main at all, so it must still see the ORIGINAL
    exception type, uncaught - exactly as if console_main did not exist."""
    nonce = "b" * 32
    _install_boundary_test_env(tmp_path, monkeypatch, nonce)

    try:
        cli.main(["--supervisor-launch-nonce", nonce, "wrap", "--for", "worker"])
    except SystemExit as exc:
        raise AssertionError(
            "an unexpected exception must not be converted to SystemExit"
        ) from exc
    except RuntimeError as exc:
        assert "BOUNDARY-SENTINEL-BOOM" in str(exc)
    else:
        raise AssertionError("cli.main should have raised")
    # Deliberately no sys.excepthook assertion here: console_main never
    # installs one on any path (the design that would have needed one was
    # rejected in round 18), so there is nothing of THIS code's making to
    # check for - and the ambient hook is not this test's to assert on.
    # merely `import pytest` on Python <3.11 replaces sys.excepthook with
    # the `exceptiongroup` backport's handler (confirmed by reproducing:
    # `python -c "import sys; import pytest; print(sys.excepthook)"` shows
    # exceptiongroup_excepthook, before any of this test's code ever runs),
    # which is exactly what made an earlier, now-removed version of this
    # assertion fail on the 3.10 CI legs - a fact about the test
    # environment, not about main()/console_main.


def test_console_main_passes_systemexit_through_unchanged() -> None:
    """SystemExit (argparse's own error exit here) is never main()'s to
    return-ify, and console_main must not touch it either - row 1 of the
    contract table, now checked at the shared function too."""
    with pytest.raises(SystemExit) as excinfo:
        cli.console_main(["not-a-real-subcommand"])
    assert excinfo.value.code == 2


def test_console_main_returns_main_result_unchanged_for_routine_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt/ValueError/OSError already return ints from inside
    main() itself (round 17) - console_main must pass those straight
    through, not just the exception-propagation cases."""

    def raise_keyboard_interrupt(_args: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raise_keyboard_interrupt)
    assert cli.console_main(["wrap", "--for", "worker"]) == 130


@pytest.mark.parametrize("patch_target", ["build_parser", "parse_args"])
def test_keyboard_interrupt_before_dispatch_still_returns_130(
    monkeypatch: pytest.MonkeyPatch, patch_target: str,
) -> None:
    """Round 24 connector finding: a KeyboardInterrupt raised while
    build_parser() or parser.parse_args() is running happens BEFORE
    main()'s own try ever started, so it used to propagate straight past
    main() (as if main() had no exception handling at all) instead of
    returning 130 like the contract table's KeyboardInterrupt row already
    promises for every OTHER point in main(). Once console_main's broad
    `except BaseException` was added (round 20), that same propagating
    KeyboardInterrupt fell into ITS crash-reporting branch instead,
    misreporting a Ctrl-C as an unexpected crash (return 1) rather than the
    conventional cancellation status. The fix widens main()'s own try to
    cover the whole function body - the SAME contract-table row now covers
    this window too, rather than adding a second, special-cased catch in
    console_main beside it."""

    def raise_it(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    if patch_target == "build_parser":
        monkeypatch.setattr(cli, "build_parser", raise_it)
    else:
        monkeypatch.setattr(argparse.ArgumentParser, "parse_args", raise_it)

    assert cli.main([]) == 130
    assert cli.console_main([]) == 130


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["--root", "R", "wrap", "--for", "worker", "--loop",
             "--", "codex", "exec", "--json"],
            True,
        ),
        (["--root", "R", "wrap", "--help"], False),
        (["--root", "R", "wrap", "-h"], False),
        # --min-interval takes a value; a dangling flag with none is a
        # parse error (argparse exits 2) before cmd_wrap is ever reached.
        (["--root", "R", "wrap", "--min-interval"], False),
        (["--root", "R", "wrap", "--nonexistent-flag"], False),
        (["--root", "R", "status"], False),
        # A --help intended for the WRAPPED cli (after --) is captured by
        # wrap's own REMAINDER positional and never reaches agenttalk's
        # own -h/--help recognition.
        (
            ["--root", "R", "wrap", "--for", "worker", "--", "codex", "--help"],
            True,
        ),
    ],
)
def test_resolves_to_cmd_wrap_matches_whether_argparse_actually_dispatches(
    argv: list[str],
    expected: bool,
) -> None:
    assert cli._resolves_to_cmd_wrap(argv) is expected


def test_internal_check_wrap_dispatch_exit_code_matches_resolution_and_is_silent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main([
        "_internal-check-wrap-dispatch", "--",
        "--root", "R", "wrap", "--for", "worker", "--loop", "--", "codex",
    ]) == 0
    assert cli.main([
        "_internal-check-wrap-dispatch", "--", "--root", "R", "wrap", "--help",
    ]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cmd_wrap_records_terminating_signal_without_exception_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "b" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def signal_during_setup(_args: argparse.Namespace) -> int:
        installed = signal.getsignal(signal.SIGTERM)
        assert callable(installed)
        installed(signal.SIGTERM, None)
        raise AssertionError("terminating signal handler returned")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", signal_during_setup)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    with pytest.raises(SystemExit):
        cli.cmd_wrap(args)

    rows = [
        json.loads(line)
        for path in _log_glob(tmp_path, "stderr.log*")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event"] for row in rows] == ["wrapper_signal_received"]
    assert rows[0]["signal"] == int(signal.SIGTERM)
    assert rows[0]["terminating"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_stream_environment_hardens_sensitive_log_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = tmp_path / "project" / "worker" / "generation"
    generation.mkdir(parents=True, mode=0o755)
    out_path = generation / "stdout.log"
    err_path = generation / "stderr.log"
    out_path.write_text("", encoding="utf-8")
    err_path.write_text("", encoding="utf-8")
    out_path.chmod(0o644)
    err_path.chmod(0o644)
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="a" * 32,
    ):
        pass

    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(err_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(generation.stat().st_mode) == 0o700
    assert stat.S_IMODE(generation.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_bounded_stream_tee_creates_tail_path_restrictively_not_via_later_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 22 (connector finding, security): chmod-AFTER cannot close the
    window between creating a path and tightening it - another local user
    on a shared POSIX account could open a handle inside that window and
    keep it past the chmod. Checking the FINAL mode after everything runs
    would pass even with the old create-then-chmod code (the chmod still
    ran and still corrected it eventually) - that is exactly the test
    shape the connector warned against. This neuters os.chmod entirely
    (monkeypatched to raise, caught by the existing contextlib.suppress)
    AND sets a fully permissive ambient umask, so the ONLY way the mode
    ends up correct is if the creation call itself (the opener passed to
    Path.open, and mkdir's own mode= argument) already requested it -
    proving the fix is atomic-at-creation, not merely fast-to-correct."""
    old_umask = os.umask(0o000)
    monkeypatch.setattr(
        os, "chmod",
        lambda *a, **k: (_ for _ in ()).throw(OSError("chmod disabled for this test")),
    )
    try:
        base = tmp_path / "project" / "worker" / "generation" / "stderr.log"
        original = io.StringIO()
        tee = wrapper_logs.BoundedStreamTee(
            original, base, max_bytes=4096, segment_count=4,
        )
        tee.write("SENTINEL\n")
        tee.close()
    finally:
        os.umask(old_umask)

    tail_files = _log_glob(base.parent, "stderr.log.*")
    assert tail_files
    for path in tail_files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE(base.parent.stat().st_mode) == 0o700


def test_ambient_log_paths_without_matching_launch_nonce_are_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "must-not-open.stdout"
    err_path = tmp_path / "must-not-open.stderr"
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)
    before_out = io.StringIO()
    before_err = io.StringIO()
    monkeypatch.setattr("sys.stdout", before_out)
    monkeypatch.setattr("sys.stderr", before_err)

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="b" * 32,
    ):
        import sys

        assert sys.stdout is before_out
        assert sys.stderr is before_err

    assert not out_path.exists()
    assert not err_path.exists()
    lifecycle = wrapper_logs.WrapperLifecycleLog.from_environment(
        "worker",
        expected_nonce="b" * 32,
    )
    assert lifecycle.enabled is False
