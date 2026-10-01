"""Lead-run integration facts for the work board's Done and Planned lanes.

`agenttalk board verify-merges` proves, outside the web server, that an item's
reviewed candidate is an ancestor of an approved target ref in an operator-approved
local checkout, and records each result in one bounded, schema-versioned store
file. The snapshot worker only reads that file: the server never runs Git.

`agenttalk board import-plan <file>` reads a lead-written plan file's work-items
table and records its rows in the SAME file, in their own "planned" section,
keyed by plan id. A plan row with no matching dispatch becomes a Planned card;
a dispatched item always keeps its derived column instead (see work_board_feed).
"""
import contextlib
import json
import os
import re
import subprocess  # nosec B404
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenttalk import work_tags
from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk.store import LockContention

SCHEMA_VERSION = 1
PLANNED_SCHEMA_VERSION = 1
FACTS_FILE = "work-board-facts.json"
LOCK_FILE = "work-board-facts.lock"
RUN_LOCK_FILE = "work-board-verify.lock"
MAX_FILE_BYTES = 512 * 1024
MAX_FACTS = 400
PROBE_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_AGE_SECONDS = 24 * 3600
GIT = ("git",)

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_REF = re.compile(r"refs/(?:heads|remotes)/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_FLAGS = {"--verify", "--quiet", "--end-of-options", "--is-ancestor", "--is-shallow-repository",
          "--show-toplevel", "--symbolic-full-name", "-e"}
_FACT_KEYS = {"project", "repo_alias", "repo_path", "work_item", "candidate", "target_ref", "target_oid",
              "checked_at", "result"}
_TIMED_OUT = "git unavailable or timed out"
_SCHEMA_UNSUPPORTED = "integration facts schema unsupported"
_OVERSIZE = "integration facts exceed their size bound"

# ----------------------------------------------------- plan import (wb-planned-lane)
_PLANNED_ROW_KEYS = {"work_item", "phase", "owner", "starts_when"}
_PLAN_TITLE = re.compile(r"^#\s*Plan:\s*(.+?)\s*$", re.MULTILINE)
_PLAN_ID_LINE = re.compile(r"^Plan id:(.*)$", re.MULTILINE)
_PLAN_REV = re.compile(r"^Plan revision:\s*(r\d+)\b", re.MULTILINE)
_WORK_ITEMS_HEADING = re.compile(r"^##\s*5\.\s*Work items\s*$", re.MULTILINE)
_NEXT_HEADING = re.compile(r"^##\s", re.MULTILINE)
_SEP_CELL = re.compile(r":?-{3,}:?")


class PlanRefused(ValueError):
    """import-plan published nothing, for an operator-actionable reason."""


class VerifyRefused(ValueError):
    """verify-merges published nothing, for an operator-actionable reason."""


def _valid_ref(ref):
    return bool(isinstance(ref, str) and _REF.fullmatch(ref) and ".." not in ref and "//" not in ref
                and "/." not in ref and "/-" not in ref and not ref.endswith(("/", ".", ".lock")))


def _norm(path):
    return os.path.normcase(os.path.normpath(path))


def _time(text):
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def repo_aliases(cfg):
    """Return (aliases, default alias, problems) from config ``work_repos``: an access
    allowlist of canonical local checkouts and approved full target refs, not an item
    registry. Invalid or ambiguous entries are dropped, never repaired."""
    raw = cfg.get("work_repos")
    if raw is None:
        return {}, None, []
    if not isinstance(raw, dict):
        return {}, None, ["work_repos must be an object"]
    aliases, defaults, bound, problems = {}, [], {}, []
    for alias, entry in raw.items():
        entry = entry if isinstance(entry, dict) else {}
        path, targets = entry.get("path"), entry.get("targets")
        if not isinstance(alias, str) or not _SLUG.fullmatch(alias):
            problems.append(f"work_repos alias {alias!r} is not a lowercase slug")
        elif not isinstance(path, str) or "\0" in path or not os.path.isabs(path) or ".." in Path(path).parts:
            problems.append(f"work_repos.{alias}: path must be absolute, without '..'")
        elif (not isinstance(targets, list) or not targets or len(set(map(str, targets))) != len(targets)
              or not all(_valid_ref(t) for t in targets)):
            problems.append(f"work_repos.{alias}: targets must be distinct full refs (refs/heads/ or refs/remotes/)")
        elif not isinstance(entry.get("default", False), bool):
            problems.append(f"work_repos.{alias}: default must be true or false")
        else:
            aliases[alias] = {"path": _norm(path), "targets": list(targets)}
            bound.setdefault(_norm(path), []).append(alias)
            if entry.get("default") is True:
                defaults.append(alias)
    for names in bound.values():
        if len(names) > 1:
            problems.append(f"work_repos aliases {', '.join(sorted(names))} bind the same checkout")
            for name in names:
                aliases.pop(name)
    if len(defaults) > 1:
        problems.append("more than one work_repos alias is marked default")
    default = defaults[0] if len(defaults) == 1 and defaults[0] in aliases else None
    return aliases, default, problems


def resolve_binding(binding, aliases, default):
    """(alias, approved target refs, None) for an item's CURRENT binding, or (None, [], reason).

    The binding is the item's declared work_repo/work_target, else the current default."""
    if binding == "ambiguous":
        return None, [], "conflicting repository declarations"
    binding = binding if isinstance(binding, dict) else {}
    if any(isinstance(binding.get(k), str) and not binding[k].strip() for k in ("repo", "target")):
        return None, [], "empty repository/target declaration"
    alias = binding.get("repo") or default
    if alias not in aliases:
        return None, [], (f"repository {alias!r} is not an approved work_repos alias" if alias
                          else "no default work_repos alias is configured")
    declared = binding.get("target")
    targets = [ref for ref in aliases[alias]["targets"] if declared in (None, ref, ref.removeprefix("refs/heads/"))]
    if not targets:
        return None, [], f"target {declared!r} is not approved for {alias}"
    return alias, targets, None


def max_age_seconds(cfg):
    value = cfg.get("integration_facts_max_age_seconds")
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 60 <= value <= 30 * 86400
    return float(value) if ok else float(DEFAULT_MAX_AGE_SECONDS)


def _git(repo, subcommand, *args):
    """One hardened probe (design section 4): rev-parse, merge-base --is-ancestor or
    cat-file; validated arguments, no shell, hooks, fetch, filters or prompts. None on
    timeout or OS failure."""
    if subcommand not in ("rev-parse", "merge-base", "cat-file") or (
            (subcommand == "merge-base") != ("--is-ancestor" in args)):
        raise ValueError(f"git probe not allowed: {subcommand}")
    for arg in args:
        base = arg[:-len("^{commit}")] if arg.endswith("^{commit}") else arg
        if arg not in _FLAGS and not (_OID.fullmatch(base) or _valid_ref(base)):
            raise ValueError(f"unsafe git argument: {arg!r}")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    try:
        return subprocess.run(  # noqa: S603  # nosec B603
            [*GIT, "--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(repo), subcommand, *args],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=PROBE_TIMEOUT_SECONDS, env=env, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _out(result):
    return result.stdout.decode("utf-8", "replace").strip()


def _open_repo(entry):
    """((checkout, {target_ref: target_oid}), problems) or (None, reason). Any doubt is Unknown."""
    try:
        real = Path(entry["path"]).resolve(strict=True)
    except OSError:
        return None, "checkout path does not exist"
    if _norm(str(real)) != entry["path"] or not real.is_dir():
        return None, "checkout path is not canonical (a symlink, junction or escape)"
    top = _git(real, "rev-parse", "--show-toplevel")
    shallow = top and _git(real, "rev-parse", "--is-shallow-repository")
    if not shallow:
        return None, _TIMED_OUT
    if top.returncode or _norm(_out(top)) != entry["path"]:
        return None, "checkout path is not the top of a Git work tree"
    if shallow.returncode or _out(shallow) != "false":
        return None, "shallow or incomplete history"
    targets, problems = {}, []
    for ref in entry["targets"]:
        name = _git(real, "rev-parse", "--verify", "--symbolic-full-name", "--end-of-options", ref)
        oid = name and _git(real, "rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}")
        if not oid:
            return None, _TIMED_OUT
        if name.returncode or _out(name) != ref:  # ambiguity prints nothing; a decoy prints another ref
            problems.append(f"target {ref} is missing or ambiguous")
        elif oid.returncode or not _OID.fullmatch(_out(oid)):
            problems.append(f"target {ref} does not name a commit")
        else:
            targets[ref] = _out(oid)
    return ((real, targets), problems) if targets else (None, "; ".join(problems))


def _probe(real, candidate, target_oid):
    """"integrated", "not_integrated", an "unknown: ..." reason, or None on timeout."""
    exists = _git(real, "cat-file", "-e", candidate + "^{commit}")
    if exists is None:
        return None
    if exists.returncode:
        return "unknown: candidate commit is not available locally"
    ancestry = _git(real, "merge-base", "--is-ancestor", candidate, target_oid)
    if ancestry is None:
        return None
    return {0: "integrated", 1: "not_integrated"}.get(ancestry.returncode, "unknown: ancestry check failed")


def _candidates(store, cfg):
    """Reduce the board read-only, across active and compacted envelopes."""
    from agenttalk import work_board
    from agenttalk.envelope_snapshot import validated_active
    rows, _ = validated_active(store, cfg)
    archive = store.compacted_dir
    paths = sorted(p for p in archive.iterdir() if ".json" in p.name) if archive.is_dir() else []
    cold = validated_active(store, cfg, paths=paths, compacted=True)[0] if paths else []
    messages = {m.id: m for m, _ in (*cold, *rows)}
    board = work_board.reduce(list(messages.values()), lead=store.sole_lead())
    return sorted((i for i in board["items"] if isinstance(i.get("candidate"), str)), key=lambda i: i["work_item"])


def verify_merges(store, *, now=None, dry_run=False):
    """Check every current (work_item, candidate) pair against its approved targets and
    record the verified results. Unknown outcomes are reported, never recorded.

    A publishing run holds a dedicated run lock from its first read to its publish,
    so two runs never interleave and a second one is refused at once. The lock sits
    outside state/, so a reset during the run cannot remove it. A dry run publishes
    nothing and takes no lock."""
    if dry_run:
        return _verify(store, now, dry_run=True)
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(store._exclusive_lock(store.dir / RUN_LOCK_FILE, timeout=0,
                                                      what="verify-merges run lock"))
        except LockContention:
            raise VerifyRefused("another verify-merges run is in progress") from None
        return _verify(store, now, dry_run=False)


def _verify(store, now, *, dry_run):
    now = now or datetime.now(timezone.utc)
    cfg = store.load_config()
    aliases, default, problems = repo_aliases(cfg)
    if not aliases and not dry_run:  # publishing now would silently erase the last proof
        raise VerifyRefused("no usable work_repos alias is configured (see docs/WORK-BOARD-FEED.md); "
                            "nothing was published" + "".join(f"; {p}" for p in problems[:5]))
    project, opened, facts, unknown = store.project_id(), {}, [], []
    for item in _candidates(store, cfg):
        slug, candidate = item["work_item"], item["candidate"]
        alias, targets, reason = resolve_binding(item.get("repo_binding"), aliases, default)
        if alias is None:
            unknown.append((slug, reason))
            continue
        if alias not in opened:
            opened[alias] = _open_repo(aliases[alias])
            if opened[alias][0] and opened[alias][1]:
                problems += [f"work_repos.{alias}: {p}" for p in opened[alias][1]]
        repo, reason = opened[alias]
        if repo is None:
            unknown.append((slug, reason))
            continue
        chosen = {ref: oid for ref, oid in repo[1].items() if ref in targets}
        if not chosen:
            unknown.append((slug, "no approved target of this item resolved"))
        for ref, target_oid in sorted(chosen.items()):
            result = _probe(repo[0], candidate, target_oid)
            if result is None:  # one timeout stops this checkout: never a probe storm
                opened[alias] = (None, _TIMED_OUT)
                unknown.append((slug, _TIMED_OUT))
                break
            if result.startswith("unknown: "):
                unknown.append((slug, result.removeprefix("unknown: ")))
            elif len(facts) >= MAX_FACTS:
                unknown.append((slug, "fact limit reached"))
            else:
                facts.append({"project": project, "repo_alias": alias, "repo_path": aliases[alias]["path"],
                              "work_item": slug, "candidate": candidate, "target_ref": ref,
                              "target_oid": target_oid, "checked_at": now.isoformat(), "result": result})
    if not dry_run:
        _write_section(store, "integration", {"session_id": cfg.get("session_id"),
                                              "written_at": now.isoformat(), "facts": facts})
    return {"facts": facts, "unknown": unknown, "problems": problems, "written": not dry_run}


def _read(path):
    """(document, None) or (None, warning): bounded, schema-checked, never raises."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None, "integration facts missing: the lead runs `agenttalk board verify-merges`"
    except OSError:
        return None, "integration facts unreadable"
    if len(raw) > MAX_FILE_BYTES:
        return None, _OVERSIZE
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None, "integration facts malformed"
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        return None, _SCHEMA_UNSUPPORTED
    return doc, None


def _write_section(store, name, section=None, *, build=None, refuse_cls=VerifyRefused):
    """Replace one section atomically under the ONE facts lock every writer shares
    (verify-merges, import-plan, retire-plan), keeping every other section untouched.

    With ``build``, the new value is computed from the section's CURRENT value (None if
    absent) INSIDE the lock, so two writers of the SAME section merge correctly instead of
    racing (import-plan, retire-plan: only their own plan id's entry must change). Without
    ``build``, ``section`` replaces the section outright - safe only when the caller is
    already the sole writer of this section by another means (verify-merges' own run lock).

    Refuses to overwrite a file of an unsupported schema, exceed the size bound, or (for a
    session-bound section) publish after a store reset in between."""
    path = store.state_dir / FACTS_FILE
    with store._exclusive_lock(store.state_dir / LOCK_FILE, what="work board facts"):
        doc, warning = _read(path)
        if warning == _SCHEMA_UNSUPPORTED:
            raise refuse_cls(f"{path} has an unsupported schema_version; it was left untouched "
                             "and nothing was published")
        if warning == _OVERSIZE:
            raise refuse_cls(f"{path} exceeds the {MAX_FILE_BYTES // 1024} KiB read bound; it was left "
                             "untouched and nothing was published")
        doc = doc or {"schema_version": SCHEMA_VERSION}
        new_section = build(doc.get(name)) if build is not None else section
        if (isinstance(new_section, dict) and "session_id" in new_section
                and new_section["session_id"] != store.load_config().get("session_id")):
            raise refuse_cls("the store session changed during this run (a reset); nothing was published")
        doc[name] = new_section
        text = json.dumps(doc, indent=2, ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("work board facts would exceed their size bound")
        _atomic_write_text(path, text)


def _valid_fact(fact):
    return (isinstance(fact, dict) and set(fact) == _FACT_KEYS and all(isinstance(v, str) for v in fact.values())
            and _SLUG.fullmatch(fact["repo_alias"]) and _SLUG.fullmatch(fact["work_item"])
            and _OID.fullmatch(fact["candidate"]) and _OID.fullmatch(fact["target_oid"])
            and _valid_ref(fact["target_ref"]) and fact["result"] in ("integrated", "not_integrated")
            and _time(fact["checked_at"]) is not None)


def load_integration(store, cfg, *, now):
    """Server side, no Git: the evidence integration_for() selects from. Never raises,
    because optional evidence must never take the board down; any fault is a warning."""
    try:
        return _load_integration(store, cfg, now)
    except Exception as exc:  # noqa: BLE001 - the board projects without this evidence
        return {"facts": [], "config": cfg, "warnings": [f"integration facts unusable ({type(exc).__name__})"]}


def _load_integration(store, cfg, now):
    aliases = repo_aliases(cfg)[0]
    doc, warning = _read(store.state_dir / FACTS_FILE)
    if doc is None:
        unconfigured = warning.startswith("integration facts missing") and "work_repos" not in cfg
        return {"facts": [], "config": cfg, "warnings": [] if unconfigured else [warning]}
    section = doc.get("integration")
    if section is None:  # the file exists (e.g. import-plan created it) but verify-merges never ran
        unconfigured = "work_repos" not in cfg
        return {"facts": [], "config": cfg, "warnings": [] if unconfigured else ["integration evidence missing"]}
    facts = section.get("facts") if isinstance(section, dict) else None
    if not isinstance(facts, list) or len(facts) > MAX_FACTS or not all(_valid_fact(f) for f in facts):
        return {"facts": [], "config": cfg, "warnings": ["integration facts malformed"]}
    if section.get("session_id") != cfg.get("session_id"):
        return {"facts": [], "config": cfg, "warnings": ["integration facts are from another store session"]}
    oldest, project = now - timedelta(seconds=max_age_seconds(cfg)), store.project_id()
    kept, ignored = [], 0
    for fact in facts:
        entry = aliases.get(fact["repo_alias"])
        if (fact["project"] != project or entry is None or entry["path"] != fact["repo_path"]
                or fact["target_ref"] not in entry["targets"]):
            ignored += 1
            continue
        checked = _time(fact["checked_at"])
        kept.append(dict(fact, checked=checked, fresh=oldest <= checked <= now))  # future-dated is stale
    warnings = [f"{ignored} integration fact(s) ignored: repository mapping changed"] if ignored else []
    return {"facts": kept, "config": cfg, "warnings": warnings}


def integration_for(items, evidence):
    """(integrated, notes) for work_board.reduce: a fact counts only when its alias and
    target ref match the item's CURRENT binding. notes maps (work_item, candidate) to
    (card reason suffix, stale as-of or None); unmatched or stale evidence never makes Done."""
    aliases, default, _ = repo_aliases(evidence["config"])
    found = {}
    for fact in evidence["facts"]:
        found.setdefault((fact["work_item"], fact["candidate"]), []).append(fact)
    integrated, notes = {}, {}
    for item in items:
        key = (item["work_item"], item.get("candidate"))
        if key not in found:
            continue
        alias, targets, reason = resolve_binding(item.get("repo_binding"), aliases, default)
        bound = [f for f in found[key] if f["repo_alias"] == alias and f["target_ref"] in targets]
        fresh = [f for f in bound if f["fresh"]]
        merged = next((f for f in fresh if f["result"] == "integrated"), None)
        if merged:
            integrated[key] = {k: merged[k] for k in ("repo_alias", "target_ref", "target_oid", "checked_at")}
        elif fresh and {f["target_ref"] for f in fresh} >= set(targets):
            integrated[key] = False
        elif fresh:  # a partial negative is not a negative
            missing = ", ".join(sorted(set(targets) - {f["target_ref"] for f in fresh}))
            notes[key] = (f"integration evidence incomplete (no fresh result for {missing})", None)
        elif bound:
            as_of = max(f["checked"] for f in bound).isoformat()
            notes[key] = (f"integration evidence stale (as of {as_of})", as_of)
        else:
            notes[key] = ("integration evidence ignored: "
                          + (reason or "recorded for another repository or target"), None)
    return integrated, notes


def plan_id_of(plan_name):
    """The portable plan id: the title's text, lowercased and slugified (docs/WORK-BOARD-FEED.md).
    None if the title yields nothing a plan id can be made of."""
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", plan_name.lower())).strip("-")[:64]
    return slug if _SLUG.fullmatch(slug) else None


def _split_row(line):
    """Markdown table cells, splitting on unescaped '|' only - '\\|' is literal pipe content,
    never a cell delimiter. (A reviewer-found bug: a plain str.split("|") silently misaligned
    every cell after one containing an escaped pipe, e.g. an owner cell of "dev \\| backup".)
    The table's own leading/trailing delimiter produces one empty cell at each end; dropped,
    matching the stripped "|...|".split("|") shape ordinary rows already had."""
    cells, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            cur.append("|")
            i += 2
        elif ch == "|":
            cells.append("".join(cur).strip())
            cur = []
            i += 1
        else:
            cur.append(ch)
            i += 1
    cells.append("".join(cur).strip())
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def _plan_table(text):
    """(header-cells, [data-row-cells]) of the "## 5. Work items" markdown table, or ([], [])
    if the heading is missing, there is no table under it, or the SECOND line is not a
    separator row of the SAME width as the header (reviewer-found: a header line with no
    separator at all used to parse as "zero data rows", i.e. a valid empty table, rather than
    a malformed one). A table with a valid header, a matching separator, and ZERO further data
    rows is a valid, empty table - callers decide whether that is a problem, not this parser.
    A stray extra separator-shaped row further down is still dropped, as before."""
    heading = _WORK_ITEMS_HEADING.search(text)
    if not heading:
        return [], []
    rest = text[heading.end():]
    next_heading = _NEXT_HEADING.search(rest)
    section = rest[:next_heading.start()] if next_heading else rest
    lines = [ln.strip() for ln in section.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return [], []
    rows = [_split_row(ln) for ln in lines]
    header, sep, data = rows[0], rows[1], rows[2:]
    if len(sep) != len(header) or not all(_SEP_CELL.fullmatch(c) for c in sep):
        return [], []
    data = [r for r in data if not (len(r) == len(header) and all(_SEP_CELL.fullmatch(c) for c in r))]
    return header, data


def _parse_plan(text):
    """(plan_id, explicit_id, plan_name, plan_rev, rows, skipped, problems).

    ``problems`` is FATAL: any entry refuses the whole import, and ``rows``/``skipped`` are
    both []. ``skipped`` is per-row ([(row text, reason)]) for a row that cannot be used but
    does not invalidate the rest of an otherwise-valid table (docs/WORK-BOARD-FEED.md): the
    table still imports around it. A structurally valid table with zero surviving rows - a
    genuinely empty table, or one where every row was skipped - is a valid plan of zero rows,
    never a problem: this is what lets a plan be cleared by re-importing an empty table."""
    problems = []
    title = _PLAN_TITLE.search(text)
    plan_name = title.group(1) if title else None
    if not plan_name:
        problems.append("missing a '# Plan: <name>' title line")
    # The field's PRESENCE is detected separately from its validity: a "Plan id:" line that is
    # blank or has more than one token must REFUSE, never silently fall back to the title slug
    # (reviewer-found: the old regex required the whole line to be one token, so a malformed
    # line simply failed to match at all, and this code could not tell "no line" from "bad line").
    id_line = _PLAN_ID_LINE.search(text)
    explicit_id = None
    if id_line is not None:
        tokens = id_line.group(1).split()
        if len(tokens) != 1 or not _SLUG.fullmatch(tokens[0]):
            problems.append(f"'Plan id:{id_line.group(1)}' is not a single lowercase slug "
                            "of at most 64 characters")
        else:
            explicit_id = tokens[0]
    plan_id = explicit_id if explicit_id is not None else (plan_id_of(plan_name) if plan_name else None)
    if explicit_id is None and plan_name and plan_id is None:
        problems.append(f"plan title {plan_name!r} does not yield a usable plan id; add a 'Plan id:' line")
    rev = _PLAN_REV.search(text)
    plan_rev = rev.group(1) if rev else None
    if not plan_rev:
        problems.append("missing a 'Plan revision: rN' line")
    header, data = _plan_table(text)
    if not header:
        problems.append("missing a '## 5. Work items' section with a table")
        return plan_id, explicit_id, plan_name, plan_rev, [], [], problems
    norm = [re.sub(r"\s+", " ", c).strip().lower() for c in header]

    def col(*names):
        return next((norm.index(n) for n in names if n in norm), None)

    cols = {"work_item": col("work_item"), "phase": col("phase"),
            "owner": col("owner (vendor)", "owner"), "starts_when": col("starts when")}
    missing = [k for k, i in cols.items() if i is None]
    if missing:
        problems.append("section 5 table is missing column(s): " + ", ".join(sorted(missing)))
        return plan_id, explicit_id, plan_name, plan_rev, [], [], problems
    if problems:  # a title/id/revision problem already refuses the import; skip row work
        return plan_id, explicit_id, plan_name, plan_rev, [], [], problems
    # The expected width is the FULL header width (the real plan table has columns this
    # import never reads, e.g. Reviewer/Estimate) - only the four required fields are
    # extracted from it. Using max(required-index)+1 here under-counted a wider header and
    # rejected every real row as a cell-count mismatch (reviewer-found P1).
    width, seen, rows, skipped = len(header), set(), [], []
    for cells in data:
        row_text = " | ".join(cells)
        if len(cells) != width:
            skipped.append((row_text, f"wrong number of cells ({len(cells)}, expected {width})"))
            continue
        try:
            work_item = work_tags.value("work_item", cells[cols["work_item"]])
        except (TypeError, ValueError):
            problems.append(f"invalid work_item in table row: {cells[cols['work_item']]!r}")
            continue
        if work_item in seen:
            problems.append(f"duplicate work_item in this plan's table: {work_item!r}")
            continue
        # Identity is recorded as soon as work_item itself is known valid - BEFORE the
        # skippable-field check below - so a later duplicate is caught regardless of
        # whether THIS row went on to be skipped or kept (reviewer-found: order-dependent).
        seen.add(work_item)
        phase, owner, starts_when = (cells[cols[k]] for k in ("phase", "owner", "starts_when"))
        if not (phase and owner and starts_when):
            skipped.append((row_text, f"work item {work_item!r} is missing phase, owner or starts-when"))
            continue
        rows.append({"work_item": work_item, "phase": phase, "owner": owner, "starts_when": starts_when})
    if problems:
        return plan_id, explicit_id, plan_name, plan_rev, [], [], problems
    if not rows and data:
        # Every row was skipped: a non-empty table that imports nothing is never a silent
        # "clear this plan" - only a STRUCTURALLY empty table (no data rows at all) is.
        problems.append("every row in section 5 was skipped, not a valid empty table: "
                        + "; ".join(f"{why} ({row!r})" for row, why in skipped))
        return plan_id, explicit_id, plan_name, plan_rev, [], [], problems
    return plan_id, explicit_id, plan_name, plan_rev, rows, skipped, []


def _read_plan_text(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        raise PlanRefused(f"{path} does not exist; nothing was published") from None
    except OSError as exc:
        raise PlanRefused(f"{path} is unreadable ({exc}); nothing was published") from None
    if len(raw) > MAX_FILE_BYTES:
        raise PlanRefused(f"{path} exceeds the {MAX_FILE_BYTES // 1024} KiB read bound; nothing was published")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PlanRefused(f"{path} is not valid UTF-8 text; nothing was published") from None


def _planned_build(plan_id, raise_cls=PlanRefused):
    """A ``_write_section`` ``build`` callback factory shared by import-plan and retire-plan:
    checks the planned section's own schema, unwraps its "plans" map, and leaves every OTHER
    plan id's entry untouched. The caller supplies what happens to THIS plan id's own entry."""
    def check(current):
        if current is not None and (not isinstance(current, dict)
                                    or current.get("schema_version") != PLANNED_SCHEMA_VERSION):
            raise raise_cls("the planned section has an unsupported schema_version; it was left "
                            "untouched and nothing was published")
        return dict(current["plans"]) if current and isinstance(current.get("plans"), dict) else {}
    return check


def import_plan(store, path, *, now=None):
    """Parse a plan file's "## 5. Work items" table and atomically replace THIS plan's rows in
    the same facts file wb-done-facts created, in their own "planned" section. Row identity is
    (plan id, work_item): a re-import replaces every row of this plan_id, so a row dropped from
    the file disappears (including down to zero rows, for a structurally valid empty table) and
    a superseded plan_rev is replaced. Other plans' rows and the integration section are
    untouched. Refuses, publishing nothing, on a malformed plan file - a per-row issue the
    documented contract allows (an empty cell, a cell-count mismatch) is skipped and reported
    instead, never refused.

    Plan identity: an explicit "Plan id: <id>" line, when present, IS the identity and survives
    a later title change. Without one, the id falls back to the title's own slug (legacy) - and
    if that slug already names a DIFFERENT plan's stored rows (a title collision), the import is
    refused with a message suggesting an explicit "Plan id:" line, rather than silently
    overwriting a different plan's rows."""
    text = _read_plan_text(path)
    plan_id, explicit_id, plan_name, plan_rev, rows, skipped, problems = _parse_plan(text)
    if problems:
        raise PlanRefused("plan file malformed: " + "; ".join(problems))
    now = now or datetime.now(timezone.utc)
    check = _planned_build(plan_id)

    def build(current):
        plans = check(current)
        existing = plans.get(plan_id)
        if explicit_id is None and existing is not None and existing.get("plan_name") != plan_name:
            raise PlanRefused(
                f"plan id {plan_id!r} (derived from the title) is already recorded for a "
                f"different plan ({existing.get('plan_name')!r}); add an explicit "
                "'Plan id: <id>' line to this plan file to give it its own stable identity")
        # project and session are read fresh HERE, under the lock, never captured earlier -
        # a reset before this point is simply the new, current store; a reset were it somehow
        # possible between this read and _write_section's own matching re-read is refused by
        # the session_id check below, exactly like the integration section's writer.
        cfg = store.load_config()
        entry = {"project": store.project_id(), "plan_rev": plan_rev, "plan_name": plan_name,
                 "written_at": now.isoformat(), "rows": rows}
        plans = dict(plans, **{plan_id: entry})
        return {"schema_version": PLANNED_SCHEMA_VERSION, "session_id": cfg.get("session_id"), "plans": plans}

    _write_section(store, "planned", build=build, refuse_cls=PlanRefused)
    return {"plan_id": plan_id, "plan_name": plan_name, "plan_rev": plan_rev, "rows": rows, "skipped": skipped}


def retire_plan(store, plan_id):
    """Atomically remove one plan id's rows from the planned section - clears an old or renamed
    plan the board should stop showing. Refuses, publishing nothing, if this plan id has no
    recorded rows; every other plan id's rows and the integration section are untouched."""
    if not _SLUG.fullmatch(plan_id):
        raise PlanRefused(f"{plan_id!r} is not a lowercase slug of at most 64 characters")
    check = _planned_build(plan_id)

    def build(current):
        plans = dict(check(current))
        if plan_id not in plans:
            raise PlanRefused(f"no planned rows are recorded for plan id {plan_id!r}; nothing changed")
        del plans[plan_id]
        session_id = store.load_config().get("session_id")  # read fresh, under the lock
        return {"schema_version": PLANNED_SCHEMA_VERSION, "session_id": session_id, "plans": plans}

    _write_section(store, "planned", build=build, refuse_cls=PlanRefused)
    return {"plan_id": plan_id}


def _valid_planned_row(row):
    # work_item is type-checked BEFORE the regex: a non-string value (JSON allows any type)
    # must fail validation, never raise TypeError out of this function and past its caller's
    # per-plan isolation (reviewer-found: this used to take down every OTHER plan too).
    return (isinstance(row, dict) and set(row) == _PLANNED_ROW_KEYS
            and isinstance(row.get("work_item"), str) and _SLUG.fullmatch(row["work_item"])
            and all(isinstance(row.get(k), str) and row[k] for k in ("phase", "owner", "starts_when")))


def load_planned(store, cfg, *, now=None):
    """Server side, no Git: the plan rows the Planned column reads. Never raises, because
    optional evidence must never take the board down; any fault is a warning and no rows."""
    try:
        return _load_planned(store, cfg)
    except Exception as exc:  # noqa: BLE001 - the board projects without this evidence
        return {"plans": {}, "warnings": [f"planned facts unusable ({type(exc).__name__})"]}


def _load_planned(store, cfg):
    doc, warning = _read(store.state_dir / FACTS_FILE)
    if doc is None:
        unconfigured = warning.startswith("integration facts missing")
        return {"plans": {}, "warnings": [] if unconfigured else [warning]}
    section = doc.get("planned")
    if section is None:
        return {"plans": {}, "warnings": []}
    if not isinstance(section, dict) or section.get("schema_version") != PLANNED_SCHEMA_VERSION:
        return {"plans": {}, "warnings": ["planned facts schema unsupported"]}
    plans = section.get("plans")
    if not isinstance(plans, dict):
        return {"plans": {}, "warnings": ["planned facts malformed"]}
    project, kept, warnings = store.project_id(), {}, []
    for plan_id, entry in plans.items():
        ok = (isinstance(plan_id, str) and _SLUG.fullmatch(plan_id) and isinstance(entry, dict)
              and isinstance(entry.get("plan_rev"), str) and entry["plan_rev"]
              and isinstance(entry.get("plan_name"), str) and entry["plan_name"]
              and isinstance(entry.get("rows"), list) and all(_valid_planned_row(r) for r in entry["rows"]))
        if not ok:
            warnings.append(f"planned facts: plan {plan_id!r} malformed; ignored")
            continue
        if entry.get("project") != project:
            continue  # another store's plan rows (no mapping change to report; just not ours)
        kept[plan_id] = {"plan_rev": entry["plan_rev"], "plan_name": entry["plan_name"], "rows": entry["rows"]}
    return {"plans": kept, "warnings": warnings}


def planned_cards(dispatched, planned):
    """[(work_item, column, reason, planned-dict-or-None)], sorted by work_item, for every plan
    row whose work_item has NO dispatch (``dispatched``: every work_item the reducer placed,
    dispatched or not - see work_board_feed.build). A dispatched item is skipped here and keeps
    its own derived column. A work_item planned by more than one plan_id is "unknown", never a
    silent pick of either."""
    by_item = {}
    for plan_id, entry in planned["plans"].items():
        for row in entry["rows"]:
            by_item.setdefault(row["work_item"], []).append((plan_id, entry, row))
    out = []
    for work_item in sorted(by_item):
        if work_item in dispatched:
            continue
        hits = by_item[work_item]
        plan_ids = sorted({plan_id for plan_id, _, _ in hits})
        if len(plan_ids) > 1:
            out.append((work_item, "unknown",
                       "planned in more than one plan: " + ", ".join(plan_ids), None))
        else:
            plan_id, entry, row = hits[0]
            out.append((work_item, "planned", f"planned in {entry['plan_name']} ({entry['plan_rev']})",
                       {"phase": row["phase"], "owner": row["owner"], "starts_when": row["starts_when"],
                        "plan_id": plan_id, "plan_name": entry["plan_name"], "plan_rev": entry["plan_rev"]}))
    return out
