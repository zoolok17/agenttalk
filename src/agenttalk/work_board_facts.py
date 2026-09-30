"""Lead-run integration facts for the work board's Done lane.

`agenttalk board verify-merges` proves, outside the web server, that an item's
reviewed candidate is an ancestor of an approved target ref in an operator-approved
local checkout, and records each result in one bounded, schema-versioned store
file. The snapshot worker only reads that file: the server never runs Git.
"""
import json
import os
import re
import subprocess  # nosec B404
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agenttalk._atomic import write_text as _atomic_write_text

SCHEMA_VERSION = 1
FACTS_FILE = "work-board-facts.json"
LOCK_FILE = "work-board-facts.lock"
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


def _valid_ref(ref):
    return bool(isinstance(ref, str) and _REF.fullmatch(ref) and ".." not in ref and "//" not in ref
                and "/." not in ref and "/-" not in ref and not ref.endswith(("/", ".", ".lock")))


def _norm(path):
    return os.path.normcase(os.path.normpath(path))


def _time(text):
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else None


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
    record the verified results. Unknown outcomes are reported, never recorded."""
    now = now or datetime.now(timezone.utc)
    cfg = store.load_config()
    aliases, default, problems = repo_aliases(cfg)
    project, opened, facts, unknown = store.project_id(), {}, [], []
    for item in _candidates(store, cfg):
        slug, candidate, binding = item["work_item"], item["candidate"], item.get("repo_binding")
        if binding == "ambiguous":
            unknown.append((slug, "conflicting repository declarations"))
            continue
        alias = (binding or {}).get("repo") or default
        if alias not in aliases:
            unknown.append((slug, f"repository {alias!r} is not an approved work_repos alias" if alias
                            else "no default work_repos alias is configured"))
            continue
        if alias not in opened:
            opened[alias] = _open_repo(aliases[alias])
            if opened[alias][0] and opened[alias][1]:
                problems += [f"work_repos.{alias}: {p}" for p in opened[alias][1]]
        repo, reason = opened[alias]
        if repo is None:
            unknown.append((slug, reason))
            continue
        declared = (binding or {}).get("target")
        chosen = {ref: oid for ref, oid in repo[1].items()
                  if declared in (None, ref, ref.removeprefix("refs/heads/"))}
        if not chosen:
            unknown.append((slug, f"target {declared!r} is not approved for {alias}"))
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
        _write_section(store, "integration", {"written_at": now.isoformat(), "facts": facts})
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
        return None, "integration facts exceed their size bound"
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None, "integration facts malformed"
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        return None, "integration facts schema unsupported"
    return doc, None


def _write_section(store, name, section):
    """Replace one section atomically under the store lock, keeping the others."""
    path = store.state_dir / FACTS_FILE
    with store._exclusive_lock(store.state_dir / LOCK_FILE, what="work board facts"):
        doc = _read(path)[0] or {"schema_version": SCHEMA_VERSION}
        doc[name] = section
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
    """Server side, no Git: return (integrated, stale, warnings) for work_board_feed.build.

    integrated maps (work_item, candidate) to a fact summary (truthy: merged) or False.
    A stale fact, or one whose alias/target mapping no longer matches config, is never
    passed on; stale ones only label the card. Any file problem means no facts."""
    aliases = repo_aliases(cfg)[0]
    doc, warning = _read(store.state_dir / FACTS_FILE)
    if doc is None:
        unconfigured = warning.startswith("integration facts missing") and "work_repos" not in cfg
        return {}, {}, [] if unconfigured else [warning]
    section = doc.get("integration")
    facts = section.get("facts") if isinstance(section, dict) else None
    if not isinstance(facts, list) or len(facts) > MAX_FACTS or not all(_valid_fact(f) for f in facts):
        return {}, {}, ["integration facts malformed"]
    oldest, project = now - timedelta(seconds=max_age_seconds(cfg)), store.project_id()
    integrated, stale, ignored = {}, {}, 0
    for fact in facts:
        entry = aliases.get(fact["repo_alias"])
        if (fact["project"] != project or entry is None or entry["path"] != fact["repo_path"]
                or fact["target_ref"] not in entry["targets"]):
            ignored += 1
            continue
        key, checked = (fact["work_item"], fact["candidate"]), _time(fact["checked_at"])
        if not oldest <= checked <= now + timedelta(minutes=5):
            stale[key] = max(stale.get(key, checked), checked)
        elif fact["result"] == "integrated":
            integrated[key] = {k: fact[k] for k in ("repo_alias", "target_ref", "target_oid", "checked_at")}
        else:
            integrated.setdefault(key, False)
    stale = {key: when.isoformat() for key, when in stale.items() if key not in integrated}
    return integrated, stale, [f"{ignored} integration fact(s) ignored: repository mapping changed"] if ignored else []
