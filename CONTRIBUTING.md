# Contributing to agenttalk

**In plain words:** you are welcome to report problems, suggest changes and send pull requests. Work from your own copy (a fork) of the repository: you do not need write access. For anything bigger than a small fix, open an issue first so we can agree on the idea before you spend time on it. The maintainers' team of AI agents reviews every pull request, automated checks run on it, and only the maintainers merge it. We promise no response time. Never put secrets, credentials, or private or client data into an issue, a pull request or a commit.

## Start here

1. Install a release and try it. The [README](README.md) says what agenttalk is and how to install it. For a first setup, the [lead setup brief](https://github.com/zoolok17/agenttalk/blob/master/docs/LEAD-SETUP-BRIEF.md) is written to be handed to your AI assistant, and the [new-user manual](https://github.com/zoolok17/agenttalk/blob/master/docs/AGENTTALK-NEW-USER-MANUAL.md) explains the ideas first. We do not repeat them here.
2. Read what is already known. The README has an "Honest limits" section, and the [issue list](https://github.com/zoolok17/agenttalk/issues) shows what is open. Your problem may already be there.

## Proposing a change

- **A small fix** (a typo, a clear bug with an obvious fix, a wrong sentence in the docs) can go straight to a pull request.
- **Anything larger** (a new command, a changed behaviour, a new document, a rewrite) starts with an issue. Please include:
  - what you were trying to do, and what happened instead (or what is missing);
  - the agenttalk version (`agenttalk --version`), your operating system and Python version;
  - the exact commands you ran and the messages you saw, with any private details removed (see the next section);
  - for a suggestion, the problem you want solved before the solution you have in mind;
  - for a change to code or tests: the short security note, written before you write the code (where outside input reaches your change, how it could be misused, what the code must do instead). [The standard's short route](https://github.com/zoolok17/agenttalk/blob/master/docs/SECURE-CODING.md#if-you-are-sending-one-pull-request) says how; a change that touches no outside input needs one line.
- A pull request starts with a short template (four prompts, see the checklist below). There is no issue template, so the issue headings above are a request, not a form.

## What must never go into an issue, a pull request or a commit

- Passwords, keys, tokens, or any credential, even an expired one. Refer to a secret by where it lives, never by its value.
- Private data, and anything that belongs to a client or an employer: names, records, logs or code that you are not free to publish.
- Internal hostnames, paths, addresses, or account names. Replace them with placeholders such as `<host>` or `<path>`.

Everything you post here is public, and a commit stays in the history even after you delete it. If you posted something by mistake, change it at its source (rotate the credential) first, and tell us.

The repository also runs a check that rejects added text matching a private list of names. We cannot publish that list. If the check blocks your pull request, say so in the pull request and a maintainer will help.

**A security problem is different.** Do not open a public issue with exploit details. Follow [SECURITY.md](SECURITY.md): open a private security advisory on GitHub, or email the maintainer.

## What happens to a pull request

The list below is the story in plain words. [The pull-request checklist](#the-pull-request-checklist) after it is the same path as a table, step by step, with who acts and what proof each step leaves.

1. **You send it from a fork,** to the `master` branch. You do not need write access to this repository. In the description, say what changed and why in plain words, and how you checked it.
2. **Automated checks run.** On every pull request the `tests` workflow runs the project's development gate (the tests on Linux, macOS and Windows with Python 3.10 to 3.13, packaging checks and security scanners), and the `security` workflow runs CodeQL and the client-reference check. A third workflow, `comprehension-network-deny`, also runs on every pull request, documentation-only ones included: it tests agenttalk's code-analysis worker on Linux and Windows with external networking blocked, and it has nothing to do with your change unless you touch that worker. Some documentation-only changes qualify for a lighter set (documentation wording tests, a secret scan, workflow checks, CodeQL and the client-reference check). Not every documentation file does: the exclusions are in the technical details below, and the README and this file run the full matrix. [docs/DEV-GATE.md](https://github.com/zoolok17/agenttalk/blob/master/docs/DEV-GATE.md) explains both.
3. **The maintainers' agent team reviews it.** A reader who did not write the change reads it, from a different AI vendor than the author when one is available. An automated reviewer on the code host also comments, and the maintainers answer each of its serious comments. [docs/ROLES-SKILLS-PROCEDURES.md](https://github.com/zoolok17/agenttalk/blob/master/docs/ROLES-SKILLS-PROCEDURES.md) (sections 4.2 and 4.3) describes the cycle.
4. **Expect at least one round of fixes when the review finds something.** The reader lists what it found, the change is fixed, and it is read again. A change is not read once and passed: each fix is read too.
5. **Only the maintainers merge.** A change merges when it has a recorded approval for its exact last commit, the serious automated comments are answered, and the checks are green. If you push another commit after an approval, the approval no longer covers it (the one exception, a push that only brings master in, is defined in [the merge rule](#the-merge-rule)).

Two things to know about forks. First, GitHub does not give a fork's pull request the repository's secrets, so the client-reference check cannot run on it. Its log says HOLD and the job ends with a failure, so GitHub shows it as a red failed check. That red mark means "this check did not run", which is a hold: it is not a finding and it is not a pass. A maintainer runs the check on their side before merging, but that run cannot turn the red mark green. You do not need to do anything. Any other red check on a fork is a real failure to fix. Second, a first-time contributor's workflows may wait for a maintainer's approval before they start: that is GitHub's own rule for forks, not something this repository adds.

## The pull-request checklist

**In plain words:** this is the one list every pull request follows, in the same order, whoever sends it. Each row says who acts, what they do, what result is accepted, and what makes that result out of date. The notes under the table hold the details. The rows describe the team's rules; GitHub enforces only some of them (see the notes). "The lead" is the maintainers' lead agent, who does the merging. Nothing here adds an approval or asks you to use anything private: the maintainer rows are done by the maintainers.

| Step | Who acts | Action | Accepted result | Stale when |
|---|---|---|---|---|
| 1. Propose | Contributor | Opens an issue for anything bigger than a small fix, and claims it with a comment. | An issue with the agreed scope. | The scope changes. |
| 2. Send | Contributor, from a fork. Maintainers, and the second team if agreed, may use a branch in this repository (the second team's are named `neuro/<topic>`). | Opens one pull request on one subject, using the template. | A plain-words description (what, why, how checked, on which commit); a CHANGELOG line if a user will notice ([style](#changelog-entry-style)); nothing private. | A new push. Another pull request lands on the same lines. |
| 3. Checks | The checks, by themselves; a maintainer for the fork-only check. | Run on the pull request; the maintainers say which [path](#which-checks-a-change-gets) it gets. | `dev-gate aggregate` and `codeql (security-extended)` green on this head; the client-reference check green, or on a fork run by a maintainer ([fork route](#a-pull-request-from-a-fork)). | A new push. Master moving ([merge rule](#the-merge-rule)). |
| 4. Review | Our reviewer, or a read the second team supplies ([when it counts](#a-read-supplied-by-the-second-team)). | The lead posts "hold, review coming". A reader who is not the author, designer or builder, from another AI vendor when one is available, reads the exact head; each fix is read again. | A recorded GO (the reader's written approval) naming the full commit ID, posted on the pull request. | A new push, except one that only brings master in ([merge rule](#the-merge-rule)). |
| 5. Final read | The lead | Says in a comment that an existing read is the final one. It needs no extra approval and no extra read. | After it, only a P0 or P1 blocks ([late findings](#late-findings)). | A push that changes more than the read asked for. |
| 6. Automated reviewer's comments | The maintainers' side; the author need not chase them. | Answers each comment on the pull request, triaged by the commit it was first posted on. | Before the final read, every P0, P1 and P2 fixed, already closed or disproved, in writing ([late findings](#late-findings)). | A new push brings new comments. |
| 7. Merge | The lead only. | Merges as a merge commit, the only method GitHub allows here. | The GO names the current head (or is kept across a push that only brings master in, as [the merge rule](#the-merge-rule) defines), the checks are green for that head, the request is still current; for a fork, the [fork route](#a-pull-request-from-a-fork) is done. | A new push. Master moving. |
| 8. After a merge | The lead | Comments once on every other open pull request ([what it says](#the-comment-after-a-merge)), closes the ones the merge made redundant, releases the finished checkout. | The comment is posted. | The next merge. |
| 9. Waiting | The lead | Names the next owner and the next checkpoint on a pull request that waits. | A comment with both. No response time is promised beyond "What to expect from us". | The checkpoint passes. |

### Notes to the table

#### Late findings
One rule, for our reviewer and the automated reviewer alike. Before a read is named final, every P0, P1 and P2 finding is fixed, already closed or disproved, in writing. After a read is named final, only a P0 or P1 blocks: a P2 or P3 then gets a written disposition (fixed, disproved, or moved to a linked follow-up issue with a reason). A finding of any tag always blocks if it shows that a stated requirement is not met, or if it is a safety or privacy problem. After the final read the author pushes only for a P0 or P1, or because a maintainer asks.

#### The merge rule
If master has moved since the last full run on this head, run the checks again before merging. The one exception to that rerun: the lead writes on the pull request, before merging, why the change cannot interact with what moved (the files it touches, compared with the files that changed on master). A push that only brings master into the branch is a new head, so the checks run on it with no exception. The earlier GO is a separate question: it stands only if the lead records that the pull request's own changed lines are byte-identical before and after, for example by comparing `git patch-id --verbatim` of both diffs, each against its merge base, or by a byte comparison of the changed lines. Plain `git patch-id` ignores whitespace, and whitespace can change what code does (moving a line out of an indented block can change a check), so it is not proof. Any difference, or any doubt, means the change is read again. GitHub's own approval is renewed, as the ruleset requires. While another open pull request changes the same lines, hold your push; after it merges, bring master in once (a rebase or a merge of master, either) and push once. The lead names the pull request to wait for. GitHub does not enforce this rule; its "branch must be up to date" setting is off on purpose, because every merge would then force every other open pull request to run the full gate again.

#### A pull request from a fork
The client-reference check needs a secret that a fork's run never receives, so it prints HOLD and ends with a failure. GitHub shows a failure, not "hold", and no maintainer run can change that mark; Dependabot's pull requests end the same way. The route is:
1. The lead runs both private client-reference checks. The diff check runs on the contributor's change (given the base and head commit IDs). The full-tree check runs on the proposed merge of that change with the current master, using the checking script and the list of terms as they are in that merge. This is the same version the hosted job checks, and it matters because a change and a newer master can each be clean while the two together are not. The lead takes the merge from GitHub's own merge ref for the pull request (`refs/pull/<number>/merge`), or makes the merge locally from the head and the current master. The lead records on the pull request the base and head commit IDs, the commit ID of the merge that was scanned, and both results. Both must be clean: any finding, or a check that cannot run, stops the merge. If master moves before the merge, the full-tree check runs again on the new proposed merge, as the rerun rule above says.
2. The lead posts the GitHub approval on the contributor's last push.
3. Only then may the lead merge through the administrator bypass, and only because the hosted check cannot run on a fork. The bypass never replaces a row of the table.

#### A read supplied by the second team
It counts as our independent read only if it meets the same bar as our own cold read: its reader is not the change's author, designer or builder; it is from another AI vendor than the author when one is available; and it names the exact head. The lead records an accepted outside GO as typed team evidence (a recorded GO naming the outside reader and the commit) and says so on the pull request. The contributor needs no private tools and no extra review, and the lead adds a read of its own only when the supplied one does not meet the bar or a later push changes what it covered.

#### Which checks a change gets
A change that touches only pages under `docs/`, or the README, changelog or security page, and that no test reads, gets the lighter documentation path. `README.md`, `CHANGELOG.md` and `CONTRIBUTING.md`, and anything a test reads, get the full matrix, which includes Windows and can take several hours. The lists are in the technical details below. The maintainers' comment on the pull request names the path.

#### CHANGELOG entry style
Add lines only; never edit older entries. Under `Unreleased`, use `### Added`, `### Changed` or `### Fixed`. An entry starts with a bold plain sentence that ends with the pull request number, then says what you will notice, before and now. A tooling-only change, such as raising a development tool's minimum version, has no entry.

#### The comment after a merge
One comment on every other open pull request: master moved and what changed on it; which other open pull requests touch the same files (above all `Unreleased` in `CHANGELOG.md`, and tests that read a changed file); and the merge order as one line, for example "#559, then #560, #562, #563", posted again whenever the order changes. The later sender rebases (for the second team, through its lead).

#### What GitHub enforces, and what only the team does
- The ruleset "Merge to master" requires one approval, approval of the last push, dismisses an approval when a new commit is pushed, and requires `dev-gate aggregate` and `codeql (security-extended)`. Separately, the older branch protection requires `client-reference-tripwire`; administrators are exempt from it.
- Repository administrators can bypass the ruleset. That is how a maintainer's own change merges, because an author cannot approve their own pull request. A bypass only carries out a merge once the table is satisfied.
- GitHub allows only merge commits on this repository: the repository settings have squash and rebase merges off, and the ruleset's allowed merge method is merge only. So a squash or a rebase merge cannot be used, and the cleanup of a finished checkout relies on that, because it proves a merge by commit ancestry.
- GitHub does not check the rerun after master moved, the reader from another vendor, the answers to the automated comments, or the late-findings rule. It does not require review threads to be resolved: the written answer counts.

#### Who keeps this list true
The lead. A pull request that changes a rule this list summarises (sections 4.2 and 4.3 of the [procedures](https://github.com/zoolok17/agenttalk/blob/master/docs/ROLES-SKILLS-PROCEDURES.md), [docs/DEV-GATE.md](https://github.com/zoolok17/agenttalk/blob/master/docs/DEV-GATE.md), or a GitHub setting) changes this section in the same pull request. The commands to check the GitHub settings are in the technical details below.

## Two teams, one repository

**In plain words:** agenttalk is now built by two teams in parallel. The maintainers' team works on this desktop under its lead. A second team also changes the repository: agenttalk's collaborator Vladimir Bukinac (GitHub account [vladimir-bukinac](https://github.com/vladimir-bukinac)) and a team of agents on another machine, with its own lead. This section says how they stay out of each other's way.

- **GitHub is the record** for issues, pull requests and reviews. A finding may reach the second team through its lead first, because that is faster, but the lead posts the written verdict on the pull request, so whatever a reader needs to follow a change is there.
- **The second team works through its lead,** and the maintainers' lead works with that lead. The second team's lead tells the maintainers' lead the number of each pull request it opens. A question or a finding about the second team's pull request goes to the maintainers' lead, who passes it on; reviewers do not contact the second team directly.
- **Both teams change the same files.** When master moves, the maintainers' lead comments on every other open pull request (step 8 above). The later sender rebases and runs the checks again, unless the lead has recorded why the change cannot interact. Never assume an earlier green run still holds.
- **The rules are the same for both teams:** the checklist above, the same review, and merging by the maintainers' lead only.

## Before you send a pull request

- Install your copy for development: `python -m pip install -e ".[dev]"` from the top of your clone (this installs pytest and the build tool). agenttalk itself has no other runtime dependency and needs Python 3.10 or newer. Some tests, and the full gate, need more tools than that: install `dev-gate-requirements.txt` (`python -m pip install -r dev-gate-requirements.txt`; it lists `pytest-xdist`) before you run them, as the gate documentation describes.
- To try a change to agenttalk itself by hand, run `python -m agenttalk ...` with the Python you installed your clone with (for example its `.venv`), not with `AGENTTALK_PY`, which may run another copy of agenttalk. If your shell is set up for a team's agenttalk, do the trial in a separate shell, so that nothing carries over to your other work:
  1. Open a new shell just for the trial.
  2. Clear `PYTHONPATH`, because a team's setup may point it at the team's own copy of agenttalk, which Python then uses instead of your clone: `unset PYTHONPATH` in bash, or `Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue` in PowerShell.
  3. Run `python -m agenttalk scratch store --for <your agent name> --shell bash` (or `--shell powershell`), then run the one line it prints. It points agenttalk at a throwaway store, so your trial does not write to the team's live store.
  4. Run your trial commands.
  5. Close that shell before you run the tests or any normal team command. The settings from step 3 last until the shell closes, and they make agenttalk refuse every other store.
- Run the project's tests in a separate shell without the scratch-store line. From the top of your clone, clear `AGENTTALK_*` and `PYTHONPATH`, then use your clone's Python. Put `--basetemp` (pytest's temporary folder) in your scratch area **outside every agenttalk store**, including the trial store. Clearing the settings alone does not help if that folder is still inside a store: `init` refuses to create another store beneath it. pytest installs its own store fence around its temporary folder.

  These examples assume your clone's Python is in `.venv`. Replace the scratch path with an unused scratch folder you can use, outside any agenttalk store, before running them; pytest empties that folder. Replace the test file with the tests that cover your change.

  Bash (Linux or macOS; open Bash first, even if your usual shell is zsh):

  ```bash
  test_scratch=/absolute/path/to/scratch/pytest-agenttalk
  for setting in ${!AGENTTALK_@}; do unset "$setting"; done
  unset PYTHONPATH
  .venv/bin/python -m pytest tests/test_pytest_shell_recipe.py -p no:cacheprovider --basetemp "$test_scratch"
  ```

  PowerShell (Windows):

  ```powershell
  $testScratch = 'C:\path\to\scratch\pytest-agenttalk'
  Get-ChildItem Env:AGENTTALK_* | Remove-Item
  Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
  & .\.venv\Scripts\python.exe -m pytest tests/test_pytest_shell_recipe.py -p no:cacheprovider --basetemp $testScratch
  ```

  `tests/test_docs_plain_voice.py` reads only `README.md` and `docs/AGENTTALK-NEW-USER-MANUAL.md`, so it says nothing about other pages. For a different document, search `tests/` for its file name to find a test that reads it; if there is none, CI's documentation checks are the check.
- The full gate is `agenttalk dev-gate`. It needs CPython 3.10 and 3.14, `gitleaks` on your PATH and the packages in `dev-gate-requirements.txt`, so most contributors will let CI run it. [docs/DEV-GATE.md](https://github.com/zoolok17/agenttalk/blob/master/docs/DEV-GATE.md) lists the prerequisites if you want it locally.
- For a change to code or tests, put the security lines under "How you checked it" in your pull request description, one line for each rule your change brings into play, and run the tests those lines name ([the short route](https://github.com/zoolok17/agenttalk/blob/master/docs/SECURE-CODING.md#if-you-are-sending-one-pull-request) has two complete examples). You need no other tool.
- Keep a pull request to one subject. Add a line to the `Unreleased` section of `CHANGELOG.md` for a change that a user will notice, and only add lines: do not edit older entries.
- Write for a reader who does not program. The documents here use plain words first, and the technical detail last.

## The licence

agenttalk is released under the [MIT licence](LICENSE). By sending a contribution you agree that it may be used, copied and distributed under that licence. There is no separate contributor agreement to sign.

## What to expect from us

There is no promised response time. agenttalk is maintained by a small team that works through its agents, and it is young: expect a first answer in days rather than minutes, and sometimes longer. If an issue or a pull request has had no answer for a while, a short, polite reminder on it is fine.

## Technical details

- The `tests` workflow is `.github/workflows/tests.yml` and the `security` workflow is `.github/workflows/security.yml`. Both run on pull requests to `master`, and weekly. `comprehension-network-deny` is `.github/workflows/comprehension-network-deny.yml`; it runs on pull requests, pushes to `master` and by hand, and covers only the analysis worker and its adapter. The gate's plan is `dev-gate.json`.
- The lighter documentation path covers `README.md`, `CHANGELOG.md`, `SECURITY.md` and Markdown files under `docs/`, except documents that a test names (`scripts/ci_scope.py`). `README.md` and `CHANGELOG.md` are named by tests, so a change to either runs the full matrix, and `CONTRIBUTING.md` is not in that list, so a change to this file does too.
- To check the GitHub settings the checklist describes (read access is enough for the repository and the rulesets; the branch protection needs a maintainer): `gh api repos/zoolok17/agenttalk --jq '{allow_merge_commit,allow_squash_merge,allow_rebase_merge}'`, `gh api repos/zoolok17/agenttalk/rulesets` (then `.../rulesets/<id>` for the rules and the bypass list) and `gh api repos/zoolok17/agenttalk/branches/master/protection`. Last checked on 2026-10-26.
- On a fork the client-reference job exits with code 1 after printing HOLD when the salt secret is missing (`.github/workflows/security.yml`, `scripts/client_reference_tripwire.py`).
- To change the description of a pull request when `gh pr edit` fails for lack of the `read:org` scope, use `gh api -X PATCH repos/zoolok17/agenttalk/pulls/<number> -F body=@<file>`; it works without that scope.
- The client-reference check is `scripts/client_reference_tripwire.py`. A local pre-commit hook for it lives in `scripts/githooks/`; it needs a private salt that contributors do not have, so you can skip it.
- The review cycle in `docs/ROLES-SKILLS-PROCEDURES.md` is written for the maintainers' own team. An outside contributor is not part of it and does not need to follow its commands.
