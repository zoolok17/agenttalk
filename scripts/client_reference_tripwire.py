"""Client-reference tripwire (task #216): detect a denylisted string being
introduced by a commit or PR text, without ever committing the denylisted
strings themselves.

Mechanism (see the round-1 proposal on task #215's thread): the committed
denylist file holds SALTED HMAC digests of every substring ("n-gram") of
each denylisted string, from a floor length up to the string's own length -
never the strings. The salt is the one secret that never enters the repo
(a GitHub Actions secret for CI; a gitignored local file for a developer's
own pre-commit hook). Checking candidate text re-derives the same n-grams
from that text with the same salt and looks for a hash match - so the
denylist is useless to anyone who does not already separately have the
salt, while still being fully committed, diffable, and versioned.

This module is the ONE shared code path for both callers (a local
pre-commit hook and the CI workflow step) - see `scripts/pre-commit` and
`.github/workflows/security.yml`. The two callers differ only in how they
interpret this module's exit codes (see EXIT_* below and the module
docstring in each caller), not in how the check itself runs.

Deliberately stdlib-only: no new dependency for a repo-hygiene tool.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess  # nosec B404 - fixed argv, no shell, used for `git diff` only
import sys
from pathlib import Path

DEFAULT_DENYLIST_PATH = Path(__file__).with_name("client-reference-denylist.json")
SALT_ENV_VAR = "AGENTTALK_TRIPWIRE_SALT"
DEFAULT_LOCAL_SALT_FILE = Path(".agenttalk-tripwire-salt")  # gitignored; never committed

# Exit codes are the contract between this module and its two callers.
EXIT_CLEAN = 0
EXIT_HIT = 1          # a denylisted string was detected - a real finding
EXIT_SALT_UNAVAILABLE = 2  # the check could not run at all - never conflate with EXIT_CLEAN


class TripwireConfig:
    """One denylist file's parsed contents (PURE data, no I/O)."""

    def __init__(self, algorithm: str, min_ngram: int, max_ngram: int, hashes: frozenset[str]):
        self.algorithm = algorithm
        self.min_ngram = min_ngram
        self.max_ngram = max_ngram
        self.hashes = hashes

    @classmethod
    def from_json(cls, raw: dict) -> "TripwireConfig":
        algorithm = raw["algorithm"]
        if algorithm != "hmac-sha256":
            raise ValueError(f"unsupported tripwire denylist algorithm: {algorithm!r}")
        min_ngram = int(raw["min_ngram"])
        max_ngram = int(raw["max_ngram"])
        if min_ngram < 1 or max_ngram < min_ngram:
            raise ValueError(
                f"invalid tripwire denylist n-gram range: [{min_ngram}, {max_ngram}]")
        hashes = frozenset(raw["hashes"])
        return cls(algorithm=algorithm, min_ngram=min_ngram, max_ngram=max_ngram, hashes=hashes)


def load_denylist(path: Path = DEFAULT_DENYLIST_PATH) -> TripwireConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return TripwireConfig.from_json(raw)


def _hash_token(token: str, salt: bytes) -> str:
    return hmac.new(salt, token.lower().encode("utf-8"), hashlib.sha256).hexdigest()


def _ngrams_of(token: str, min_len: int, max_len: int):
    """Every substring of `token` with length in [min_len, min(max_len, len(token))].

    A token shorter than `min_len` yields itself (its own full length is the
    only meaningful "substring") so a short denylisted string is never
    silently unrepresented.
    """
    n = len(token)
    hi = min(max_len, n)
    lo = min(min_len, n)
    for length in range(lo, hi + 1):
        for start in range(0, n - length + 1):
            yield token[start:start + length]


def build_denylist(strings: list[str], salt: bytes, min_ngram: int) -> TripwireConfig:
    """Build a `TripwireConfig` from RAW strings (operator-local use only -
    this function's input must never be committed; only its hashed output,
    via `save_denylist`, may be)."""
    max_ngram = max((len(s) for s in strings), default=min_ngram)
    max_ngram = max(max_ngram, min_ngram)
    hashes: set[str] = set()
    for s in strings:
        for ngram in _ngrams_of(s, min_ngram, max_ngram):
            hashes.add(_hash_token(ngram, salt))
    return TripwireConfig(algorithm="hmac-sha256", min_ngram=min_ngram,
                          max_ngram=max_ngram, hashes=frozenset(hashes))


def save_denylist(config: TripwireConfig, path: Path = DEFAULT_DENYLIST_PATH) -> None:
    payload = {
        "algorithm": config.algorithm,
        "min_ngram": config.min_ngram,
        "max_ngram": config.max_ngram,
        "hashes": sorted(config.hashes),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class Hit:
    """One matched location. Deliberately carries NO matched text - only
    where it is - so a report (including a CI log, which may be public)
    never echoes the denylisted content it exists to keep out of view."""

    __slots__ = ("line", "column", "length", "offset")

    def __init__(self, line: int, column: int, length: int, offset: int):
        self.line = line
        self.column = column
        self.length = length
        self.offset = offset

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"Hit(line={self.line}, column={self.column}, length={self.length})"


def _is_word_char(ch: str) -> bool:
    return ch.isalnum()


def find_hits(text: str, config: TripwireConfig, salt: bytes) -> list[Hit]:
    """Scan `text` for any substring whose salted hash is in the denylist.

    A candidate match only counts if it is WORD-BOUNDARY ALIGNED - flanked
    on both sides by a non-alphanumeric character or the start/end of the
    text. Without this, a short internal n-gram of a longer denylisted
    string (e.g. a 4-char floor fragment of an 8-char name) matches as a
    substring of an entirely unrelated, innocent word (found empirically:
    this project's own word "criteria" contains a denylisted fragment as a
    pure coincidence of spelling). Boundary alignment still catches the
    denylisted string appearing as its own token OR as part of a
    hyphenated/punctuated compound (`name-legacy` tokenizes at the hyphen,
    so `name` alone still matches) - it only rejects a match buried inside
    a longer, different word.

    Line/column are 1-based, computed on the ORIGINAL (not lowercased) text
    so a report can point a human at the exact spot without this function
    ever needing to hand back the matched substring itself.
    """
    hits: list[Hit] = []
    lower = text.lower()
    n = len(lower)
    # Precompute line-start offsets once for O(log n) offset->(line,col).
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)

    def _line_col(offset: int) -> tuple[int, int]:
        import bisect
        line_idx = bisect.bisect_right(line_starts, offset) - 1
        return line_idx + 1, offset - line_starts[line_idx] + 1

    def _boundary_aligned(start: int, length: int) -> bool:
        if start > 0 and _is_word_char(text[start - 1]):
            return False
        end = start + length
        if end < n and _is_word_char(text[end]):
            return False
        return True

    for length in range(config.min_ngram, config.max_ngram + 1):
        if length > n:
            continue
        for start in range(0, n - length + 1):
            if not _boundary_aligned(start, length):
                continue
            token = lower[start:start + length]
            if _hash_token(token, salt) in config.hashes:
                line, col = _line_col(start)
                hits.append(Hit(line=line, column=col, length=length, offset=start))
    return hits


def dedupe_overlapping(hits: list[Hit]) -> list[Hit]:
    """Merge hits whose [offset, offset+length) spans OVERLAP into one
    representative hit per cluster (the earliest start, widest span found),
    since one real occurrence of an 8-char denylisted string legitimately
    matches at every n-gram length from the floor up to 8 - each producing
    its own Hit at a slightly different offset - and a report listing every
    one of those is noise, not additional evidence of a second occurrence."""
    ordered = sorted(hits, key=lambda h: h.offset)
    merged: list[Hit] = []
    for h in ordered:
        if merged and h.offset < merged[-1].offset + merged[-1].length:
            prior = merged[-1]
            end = max(prior.offset + prior.length, h.offset + h.length)
            merged[-1] = Hit(line=prior.line, column=prior.column,
                             length=end - prior.offset, offset=prior.offset)
        else:
            merged.append(h)
    return merged


# --------------------------------------------------------------- salt I/O

def resolve_salt(local_salt_file: Path = DEFAULT_LOCAL_SALT_FILE) -> bytes | None:
    """Best-effort salt resolution: a CI secret (env var) first, else a
    gitignored local file, else None (the caller decides fail-open vs
    fail-closed for None - this function never decides that itself)."""
    env_value = os.environ.get(SALT_ENV_VAR)
    if env_value:
        return env_value.encode("utf-8")
    if local_salt_file.exists():
        try:
            content = local_salt_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if content:
            return content.encode("utf-8")
    return None


# ------------------------------------------------------------------- CLI

def _cmd_check(args: argparse.Namespace) -> int:
    salt = resolve_salt(Path(args.local_salt_file))
    if salt is None:
        sys.stderr.write(
            "client-reference-tripwire: SALT UNAVAILABLE - this check did not run.\n"
            f"  (looked for ${SALT_ENV_VAR} and {args.local_salt_file})\n"
            "  This is NOT a pass: see the caller (pre-commit hook or CI step) for\n"
            "  how an unavailable salt is handled in that context.\n"
        )
        return EXIT_SALT_UNAVAILABLE

    try:
        config = load_denylist(Path(args.denylist))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"client-reference-tripwire: cannot load denylist: {exc}\n")
        return EXIT_SALT_UNAVAILABLE

    any_hits = False
    for file_arg in args.files:
        if file_arg == "-":
            text = sys.stdin.read()
            label = "<stdin>"
        else:
            path = Path(file_arg)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                sys.stderr.write(f"client-reference-tripwire: cannot read {file_arg}: {exc}\n")
                continue
            label = file_arg
        hits = dedupe_overlapping(find_hits(text, config, salt))
        if hits:
            any_hits = True
            for h in hits:
                print(f"{label}:{h.line}:{h.column}: denylisted content detected "
                     f"({h.length} chars) - see the local diff to identify it; "
                     "never paste the match into a report.")
    return EXIT_HIT if any_hits else EXIT_CLEAN


def _cmd_build(args: argparse.Namespace) -> int:
    """Operator-only: regenerate the committed hash file from raw strings.
    Never invoked by CI or the pre-commit hook - run by hand, locally, by
    whoever is adding a new denylisted string, with the salt already
    provisioned. Raw strings come from argv/stdin, never from a file this
    tool would encourage committing."""
    salt = resolve_salt(Path(args.local_salt_file))
    if salt is None:
        sys.stderr.write("client-reference-tripwire build: salt unavailable - "
                         "provision it before regenerating the denylist.\n")
        return EXIT_SALT_UNAVAILABLE
    strings = list(args.strings)
    if args.strings_stdin:
        strings.extend(line.strip() for line in sys.stdin if line.strip())
    if not strings:
        sys.stderr.write("client-reference-tripwire build: no strings supplied.\n")
        return 2
    config = build_denylist(strings, salt, min_ngram=args.min_ngram)
    save_denylist(config, Path(args.denylist))
    print(f"client-reference-tripwire: wrote {len(config.hashes)} hashes "
         f"(n-gram range [{config.min_ngram}, {config.max_ngram}]) to {args.denylist}")
    return EXIT_CLEAN


def _git_added_lines(base: str, head: str, repo: Path) -> str:
    """The ADDED-line text of a diff, base..head, in `repo` - never the
    removed lines (which, for this exact tool's own history, would always
    contain the string being cleaned up and would make the check
    permanently self-triggering on its own remediation diffs).

    `repo` is REQUIRED, never implied by the process's own cwd: this
    command is meant to be invoked from a pre-commit hook or a CI step,
    neither of which is guaranteed to already be sitting in the target
    repo's root, and silently diffing "whatever repo the cwd happens to be
    in" is exactly the kind of quiet wrong-target bug the rest of this
    project's own methodology treats as a wrong-data finding, not a detail.
    """
    # Fixed argv, never shell, never operator input as a program. "git" is
    # resolved from PATH on purpose (cross-platform; no fixed install path),
    # so partial-path (B607) is intentional here - same convention as
    # cli.py's own git helper.
    out = subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(repo), "diff", "--unified=0", f"{base}...{head}"],
        capture_output=True, text=True, check=True,
    )
    added = []
    for line in out.stdout.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
    return "\n".join(added)


def _cmd_check_diff(args: argparse.Namespace) -> int:
    """Check only the ADDED lines between two refs - the shape both the
    pre-commit hook (staged vs HEAD) and the CI step (PR base vs head) use."""
    salt = resolve_salt(Path(args.local_salt_file))
    if salt is None:
        sys.stderr.write(
            "client-reference-tripwire: SALT UNAVAILABLE - this check did not run.\n")
        return EXIT_SALT_UNAVAILABLE
    try:
        config = load_denylist(Path(args.denylist))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"client-reference-tripwire: cannot load denylist: {exc}\n")
        return EXIT_SALT_UNAVAILABLE
    text = _git_added_lines(args.base, args.head, Path(args.repo))
    hits = dedupe_overlapping(find_hits(text, config, salt))
    if hits:
        print(f"client-reference-tripwire: {len(hits)} denylisted match(es) in "
             f"added lines between {args.base} and {args.head}.")
        return EXIT_HIT
    return EXIT_CLEAN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="client_reference_tripwire")
    parser.add_argument("--denylist", default=str(DEFAULT_DENYLIST_PATH))
    parser.add_argument("--local-salt-file", default=str(DEFAULT_LOCAL_SALT_FILE))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Check given file(s) (or - for stdin) for hits.")
    p_check.add_argument("files", nargs="+")
    p_check.set_defaults(func=_cmd_check)

    p_diff = sub.add_parser("check-diff", help="Check only added lines between two git refs.")
    p_diff.add_argument("base")
    p_diff.add_argument("head")
    p_diff.add_argument("--repo", default=".",
                        help="Repo root to diff in (never implied by cwd).")
    p_diff.set_defaults(func=_cmd_check_diff)

    p_build = sub.add_parser("build", help="Operator-only: rebuild the denylist from raw strings.")
    p_build.add_argument("strings", nargs="*")
    p_build.add_argument("--strings-stdin", action="store_true")
    p_build.add_argument("--min-ngram", type=int, default=4)
    p_build.set_defaults(func=_cmd_build)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
