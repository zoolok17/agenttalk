# Migration playbook: moving a codebase to a newer framework, language or platform

**In plain words:** This playbook is for a team that moves a codebase to a newer framework, language version or platform, and has to show that it still behaves the same. It collects what several practice migrations taught us: a Python 2 web shop moved to Python 3, a Django shop upgraded three major versions, a PHP invoicing app upgraded five major versions (Laravel 8 to 13), and a Java 8 application moved to Java 21 and a current Spring Boot. Each rule says when it applies, why it matters, and how to check it. The rules follow the order of the work: assess, baseline, one step at a time, prove, concurrency and data, packaging and runtime, review and hand-over. Start with the short "Start here" list; nobody needs every rule on day one.

**Written for:** the people, or AI assistants, who plan and carry out a migration or a large upgrade, and the person who reviews it.
**Covers:** the method that held up in practice: Python 2 to 3, Django (a Python web framework), PHP and Laravel (a PHP web framework), Java and Spring (a Java application framework), Docker and Kubernetes (containers and the system that runs them). The rules are about how to work and how to prove it, not about one framework's steps.
**Leaves out on purpose:** step-by-step recipes for a single framework, and four lessons that depend on how a payment provider answers. Those four are kept in the lesson pack, tagged `payments`.

## How to use this guide

- Each rule has the form: the rule in bold with a label for the stack it applies to ("any stack" if it applies to all), then **When** it applies, **Why** (one line) and **Check** (one line you can actually run or read). If your stack is not in the label, skip the rule.
- The same lessons are in `src/agenttalk/knowledge_packs/migration.jsonl`, one per line, with the case each came from. That file is the data for tools that load them into a team's *lesson store* (the place where agenttalk keeps short notes about what the team learned); this guide is the version for people.
- The rules use real examples from the practice migrations. The numbers are examples, not targets, and many rules rest on a single case.
- A *bundled skill* is a written method that ships with agenttalk for an AI assistant to follow. Where one covers a method, this guide names it instead of repeating it: `agenttalk.challenge` (should this be done at all), `test-coverage` (writing tests, failing-first), `test-performance` (measuring speed), `review-failure-injection` (failure paths), `review-code` (reading a change line by line), `review-contract-drift` (renamed settings and interfaces), `test-security` (abuse cases) and `review-release-readiness` (is it safe to release).

## Words used here

- **On the wire:** what the program actually sends and receives over the network, as opposed to what its code says.
- **Race:** two requests at the same moment that interfere, so that the result depends on timing.
- **Dump the defaults:** write out the settings, the database schema and the data that the framework chooses on its own, so that you can compare before and after.
- **Failing-first:** a new test is shown to fail on the old code or on the broken version before it is trusted.
- **Removal proof:** take a fix out, show that its test fails on its assertion, put the fix back.
- **Negative control:** a deliberate break that a check must catch; if the check stays green, the check is empty.
- **Differential probe:** the same inputs fed to the old and the new version, with the outputs compared.
- **Golden capture:** a saved real answer (a response, a file) that later runs are compared with.
- **Pin:** a test that fixes today's behaviour so that a silent change turns it red.
- **Gate:** a command that must pass before the next step; **ratchet:** a gate whose limit can only get stricter.
- **Harness:** the code that runs a comparison or a test campaign; **census:** a complete count of one kind of thing; **slice:** the part of the code in scope.

## Start here

If you only have time for a few rules, apply these twelve first, in this order. Each is explained in its stage below.

1. **1.14**: Send an independent reader a blind brief (the outcome wanted, what triggered it, the size; never the pitch) and ask whether the work should be done at all, in this shape, now.
2. **2.8**: Run the old suite first, from a fresh clone, record every failing test id with a written cause, and compare ids, not totals, on every later run.
3. **2.5**: Have someone who has not seen the upgrade write a black-box suite that drives the running app over HTTP, run it on the old version, freeze it outside both code bases and run the same file (same hash) on every step.
4. **3.2** [Django, PHP and Laravel, Python]: Move one framework major version per step with the same test selection, a tag and a recorded result per step, and finish with a language-version-only step (climb the language only as far as each framework step needs).
5. **3.15** [frameworks that run their own migrations]: Build a fresh database from the real migrations on the old and new commit, dump columns (type, nullable, default), indexes and foreign keys per table, and treat every difference beyond the intended ones as a finding; run makemigrations --check twice and do not wrap lists of allowed values in a function, which makes the generator report changes every time.
6. **4.15**: Accept a step as green only with the same test ids and the same outcomes, nothing skipped or deleted, built from one process that discovers every test module, and explain any change in assertion count per test.
7. **4.16**: Treat a test that fails after an upgrade as a finding: restore the original input, run it on old and new with values printed at each step, name the changed behaviour, and keep a declared list of every edited test with its reason and no new skips (every existing skip explained).
8. **4.8**: Break a module on purpose and prove the harness goes red; make it fail on import errors, empty case lists, repeated names, unused declarations, missing captures and any network attempt, and compare raw bytes, not decoded text.
9. **4.11**: Remove each fix in a throwaway copy and rerun its test; count it proved only if the intended test was collected, ran, and failed on its assertion without the fix and passes with it.
10. **5.1**: Run concurrency probes on the production-grade database with several web workers and judge saved effects (stock, orders, payments), not HTTP statuses; also try cold workers with overlapping first requests and share per-process caches across workers.
11. **7.7**: Write every claim at the size of its proof and name the boundary of each all-or-nothing or once claim; search the code before writing that something is not done; do not write 'all differences come from the framework' unless the probe fed inputs that would show an app-level difference.
12. **7.2**: Give the finished, committed work to an independent reader, ideally built by a different vendor or tool, who rebuilds it from the written recipe in a clean checkout and tries to break it.

An item with a stack label applies only to that stack. The order of steps differs by stack: for a Java move, rule 1.2 comes first.

## The stages at a glance

1. **Assess**: Before you change anything, find out what you really have, where it runs and what is in the way.
2. **Baseline**: A baseline is a measured picture of the old system: which tests pass, what it sends and receives over the network ("on the wire"), and what it silently relies on.
3. **One step at a time**: Move in small steps, each with the same checks and a point to go back to.
4. **Prove it**: A claim such as "same behaviour" or "fixed" is worth exactly the check behind it.
5. **Concurrency and data**: Races (two requests at the same moment that interfere with each other) and lost data are invisible on a developer machine with one user.
6. **Packaging and runtime**: Moving the code is half the work; the other half is how it is built, started, configured, stopped and rolled back.
7. **Review and hand-over**: A migration is finished when someone who did not do it can rebuild it from your recipe and get the same results.

## 1. Assess

Before you change anything, find out what you really have, where it runs and what is in the way. Write the assessment so that someone else can try to break it: one reader writes, a second reader attacks. To ask whether the work should be done at all, use the `agenttalk.challenge` skill first.

- **1.1 [any stack] Write a read-only assessment first (build, runtime, tests, interfaces, deployment, risk list), then have a second, independent reader try to break it before anyone plans from it.**
  - When: You are about to plan a migration of a codebase nobody on the team knows.
  - Why: even a careful first assessment contained errors and a wrong slice order.
  - Check: every citation in the assessment resolves to a real line, and the second reader's list of missed risks is merged before the plan is frozen.
- **1.2 [Java] Treat the JDK uplift, the framework upgrade and the javax-to-jakarta rename as one coupled step unless a scratch build proves otherwise; make the first slice only 'resolve and compile on the old JDK'.**
  - When: The plan has a separate 'just move to the new JDK' slice before the framework upgrade.
  - Why: old framework, mocking and coverage libraries do not run on the new JDK, so a JDK-only slice cannot go green.
  - Check: build the old code on the new JDK in a scratch copy and list what fails before accepting a split.
- **1.3 [any stack] Choose targets from the vendors' published end-of-support dates on the day you decide, and prefer the newest generation still in open-source support that your key dependencies already support.**
  - When: You are choosing the target framework and JDK versions.
  - Why: the version the team knew was already out of support and the next one was weeks from the same fate.
  - Check: the proposal names the source of each support date, and the dates are re-checked before the first dependency change.
- **1.4 [any stack] Test the new driver against every server version you run (deployed, middle, newest) in a throwaway project, and run the unchanged old build against the new server with seeded data.**
  - When: The framework upgrade brings a new database driver, or someone says 'upgrade the database first, it is safe'.
  - Why: driver docs rarely state the minimum server, and the answer sets the order of the whole programme and who must decide.
  - Check: a driver-by-server pass/fail matrix with the driver's own error text, and the old artifact's golden responses and smoke checks passing on the new server.
- **1.5 [any stack] Read, never run, the deployment scripts and inventories to learn the versions really deployed, and compare them with what code and tests assume.**
  - When: The repository ships deployment scripts or inventories that you must not run.
  - Why: the deployed container and database were years older than anything developers ran and set hard limits on the target.
  - Check: a table of component, deployed version, source line and the target's minimum, reviewed by the system owner.
- **1.6 [Spring] Check every XML schema location in Spring configs against the schema mappings inside the jars; any unmapped URL is fetched over the network at startup.**
  - When: Tests or startup fail only on some machines or networks.
  - Why: one host-name mismatch made startup depend on a live CDN and made most tests fail behind a filtering proxy.
  - Check: list each location with its mapping, start the context with a dead proxy set, and keep a control cell with the fix reverted that fails for the stated reason.
- **1.7 [any stack] Do not call a red test pre-existing until a run on the original tree shows the same test red; otherwise call it not yet explained.**
  - When: Someone labels a red test 'pre-existing'.
  - Why: errors labelled pre-existing were in fact introduced by the migration.
  - Check: the claim cites a run on the old stack with the same test and the same result.
- **1.8 [any stack] List files and line counts from the old tag, write one inclusion rule for code and templates, define what a stub is and set a cut limit before cutting; record each cut in a ledger the day it happens, and search kept code for imports of every cut part.**
  - When: You cannot port everything and must decide what is in and out of scope.
  - Why: scope becomes a lookup, not an argument, and a cut that worked is not forgotten.
  - Check: the ledger total is under the limit and no kept module still imports a cut one.
- **1.9 [any stack] Before counting tests, try to start the old code on the intended interpreter and list every install and import error: pinned libraries that cap the language version, native libraries, and download sources that no longer exist (look for copies on the main package index).**
  - When: You are about to count tests on old code that has never run on today's machine, or rebuild an old environment from its requirements file.
  - Why: a start-up blocker makes every later number meaningless.
  - Check: each failure has a recorded cause, and re-running with the environment fixed shows how many failures flip.
- **1.10 [Python 2 to 3] Decide the dialect from definitive markers: Python 2 only from tokens (print statement, <>, backticks, exec statement, L suffix, old octal), Python 3 only from the parse tree; treat except-comma as neutral and give the rule a default when no evidence exists.**
  - When: A tool or plan classifies legacy Python source as Python 2 or 3 by whether it parses.
  - Why: newer Python 3 accepts 'except X, e:' and tokenizes <> differently by version.
  - Check: parse counts and classification agree on the oldest and newest interpreter over the same corpus.
- **1.11 [PHP and Laravel] Resolve each direct dependency alone against the target framework and PHP, check pairwise conflicts among the rest, then confirm with one resolve without the confirmed blockers; read the locked packages' PHP bounds before choosing the old runtime, and climb PHP only as far as each framework step needs.**
  - When: You list which dependencies block a PHP framework upgrade from Composer's resolver output, or choose the old PHP to run a legacy app.
  - Why: the resolver names the first requirement, not the cause, and an old error handler turns new deprecations into fatals.
  - Check: the final resolve without the blockers succeeds.
- **1.12 [any stack] Count a fault only if it also exists in the original version, keep a failing test that runs against the old version too, and reproduce every code-read lead as counted, rejected or reproduced-but-design.**
  - When: You scope an 'improve it as well' round after a port, or review money code in an old product.
  - Why: it separates inherited faults from port regressions and keeps the claim honest.
  - Check: new tests fail on the old version on behaviour, and an independent read confirms the count.
- **1.13 [any stack] Probe the new version early: import every pinned package on it, install every pin with binary-only wheels and list the gaps, search your own code for each name in the version's removal and changed-default lists, and record new warning classes (invalid escapes, resource warnings, deprecations) as next-step work.**
  - When: You plan a move to a newer language version.
  - Why: one import-time line in a dependency can stop every test from starting.
  - Check: a per-project list of import failures, missing wheels, watch-list hits and warning classes.
- **1.14 [any stack] Send an independent reader a blind brief (the outcome wanted, what triggered it, the size; never the pitch) and ask whether the work should be done at all, in this shape, now.**
  - When: Before a migration, or a big step in one, is dispatched as if it were settled.
  - Why: the person who proposed it is too close to judge its size, and a wrong-sized migration costs weeks.
  - Check: a recorded verdict (proceed, reshape, probe, stop) and what you did with it; see the agenttalk.challenge skill.

## 2. Baseline

A baseline is a measured picture of the old system: which tests pass, what it sends and receives over the network ("on the wire"), and what it silently relies on. Everything later is compared with it, so a baseline that cannot fail makes every later claim empty. How to write the tests themselves is in the `test-coverage` skill.

- **2.1 [any stack] Record, for each module separately, the list of test ids and outcomes (run, failed, error, skipped) on the old stack, and compare the tests discovered with the test methods written in the source. Gate each module on its own id list with the same known failures.**
  - When: You are recording the 'before' result that the migration will be gated on, or a module reports zero or surprisingly few tests.
  - Why: a whole-suite total cannot gate one module, and tests the runner never found hide coverage.
  - Check: the discovered count equals the test methods in the source, and the gate names the module and its ids.
- **2.2 [any stack] Before trusting any green, stop the database or other required service and confirm the suite goes red; remove every catch block in test listeners that swallows connection failures.**
  - When: The test suite needs a database or other service and 'passes' while that service is down (a vacuous green).
  - Why: a suite that is green with its dependency down is a vacuous baseline that will pass any migration.
  - Check: the run with the service down fails with errors, not a smaller green run.
- **2.3 [any stack] Before changing anything, capture real responses for every kind of route (body, status, content type, charset) as fixtures with authentication, include one non-ASCII body, and prove the harness catches a renamed field and a pure reorder.**
  - When: The framework upgrade changes JSON handling, content negotiation, charset or error handling.
  - Why: no existing test pinned the wire format, so every serialisation or header change would be silent.
  - Check: renaming one field fails exactly one fixture, a reorder fails too, and recording is deterministic on a wiped database.
- **2.4 [any stack] Write one test per security default that could flip (CSRF, headers, session handling, exception translation, request cache, URL firewall), pin today's behaviour on the old stack, prove the real filter chain is installed in the test, and pin the ordered list of filter classes.**
  - When: The security configuration is hand-built and the target framework's DSL has new defaults.
  - Why: no test ran the chain, and the new DSL adds filters and defaults silently.
  - Check: without the chain half the pins fail; removing one filter fails exactly one test.
- **2.5 [any stack] Have someone who has not seen the upgrade write a black-box suite that drives the running app over HTTP, run it on the old version, freeze it outside both code bases and run the same file (same hash) on every step. Change it only through a declared, reviewed new version that states what the old version returns, and rerun that version on the old code and on every earlier step.**
  - When: Before the first line of a port or framework upgrade is changed, you need a black-box acceptance suite to judge it by.
  - Why: it settles arguments about done, but a frozen suite can itself be wrong.
  - Check: outcomes are compared per test id between the old version and each step, and every new version has a recorded reason.
- **2.6 [any stack] Give the frozen suite its own negative controls: match whole labelled values (the right currency or unit), use non-unit rates and distinct non-zero values wherever numbers are summed, read back the record you edited, and prove that the server answering on the port is yours.**
  - When: You write or fix an outside-in suite that will be frozen and used to judge an upgrade.
  - Why: the yardstick can be wrong where no application break reaches, so a broken app passes.
  - Check: near-miss values (127.50 for 27.50, a wrong currency), a port already taken and a skipped step each turn the suite red.
- **2.7 [any stack] Make the old version run first, pinned, from one script that builds it from nothing: use a prebuilt old runtime instead of compiling one, pin old installers for the before environment only, pin library releases to the old framework, and export old tags without large-file pointers you cannot fetch.**
  - When: You start a port where the old version is ancient or its environment is hard to rebuild.
  - Why: asking the running old system beats guessing from code.
  - Check: the script builds the old environment on a clean machine and the old tests run.
- **2.8 [any stack] Run the old suite first, from a fresh clone, record every failing test id with a written cause, and compare ids, not totals, on every later run.**
  - When: Old tests fail before you have changed anything, or you are about to freeze a baseline.
  - Why: otherwise inherited failures get blamed on the upgrade, or a test-data problem gets 'fixed' as a bug.
  - Check: a stored id-and-outcome list for the old version, with every red test carrying a cause.
- **2.9 [any stack] Suspect fixed historical dates against now-relative data, or a UTC date compared with a local date, before suspecting the code; anchor fixtures to the test's own day and freeze the clock.**
  - When: Tests fail at some times of day (for example around midnight or month end) or only in the full suite.
  - Why: the failure looks like time-of-day but is a drifted fixture, and it wastes review rounds.
  - Check: the test passes in isolation under three time zones, and the frozen suite passes at month end and between 00:00 and 02:00 local time.
- **2.10 [any stack] Set autocrlf off before creating worktrees, commit fixtures and shell scripts with LF (text eol=lf), count CR bytes in Python rather than grep, write files back in binary mode, and run recipes once from a fresh clone on the target platform.**
  - When: Development happens on Windows with git autocrlf and fixtures, hashes or shell scripts cross to Linux.
  - Why: line endings changed fixtures, hashes and scripts between Windows and Linux and buried real edits in whole-file diffs.
  - Check: zero CR bytes per commit except named files, git diff --stat shows only intended lines, and the recipe passes on Linux twice.
- **2.11 [any stack] Prove which code and which server produced the result: print the imported module's file path (isolated mode ignores PYTHONPATH and a worktree has no editable install), call the interpreter by full path, use a fresh port and confirm the listener is yours in its own log.**
  - When: A black-box suite or probe runs against a local server or a scratch checkout, and a result looks surprisingly good or bad.
  - Why: suites passed against another server on the same port or tested the installed copy.
  - Check: the printed path is inside the checkout and the server log shows every request.
- **2.12 [any stack] Before the first comparison, run the harness's own self-tests and one capture on the old and the new toolchain, make it read the summary format of each test-tool version, and treat any port-only red as a harness bug to fix with a failing test first.**
  - When: You hand over or integrate a test harness that must run on two toolchains (old and ported language, runner or client library).
  - Why: a harness checked only on the old side hides faults that appear only on the port.
  - Check: self-tests and a capture are green on both sides and captures hold no memory addresses or other per-run noise.

## 3. One step at a time

Move in small steps, each with the same checks and a point to go back to. The principle is to take the smallest step that can go green, and to prove a split with a scratch build before you rely on it; some stacks split into one framework version per step, others cannot be split (the stack labels below say which). The dangerous changes raise no error at all, so look for them on purpose: dump the defaults, which means writing out the settings, the database schema and the data that the framework chooses by itself, before and after each step, and compare them row by row.

- **3.1 [any stack] Make one commit per planned step, run the gate as one command from a clean tree and clean build output with no continue-on-failure switch, quote the outcome (first test classes run, counts), not the flag you passed, and write down where you are before any run longer than ten minutes.**
  - When: You execute a multi-step uplift, or a gate passes on one machine and fails on another.
  - Why: uncommitted work died with sessions, a warm build directory hid an incomplete port, and a hard-coded order made 'reverse order' gates alphabetical.
  - Check: each step has a commit id, a gate log from deleted output and a recorded count.
- **3.2 [Django, PHP and Laravel, Python] Move one framework major version per step with the same test selection, a tag and a recorded result per step, and finish with a language-version-only step (climb the language only as far as each framework step needs).**
  - When: You plan a multi-version framework upgrade.
  - Why: failures group by cause and trace to one change, and the dangerous changes raise no error.
  - Check: each step has a tag and a re-runnable table of tests, warnings and advisories.
- **3.3 [any stack] Group failures by error line (sort and count) before opening any test, fix the biggest group first in one place, rerun only failing files, then do one full run.**
  - When: A framework step turns a green run into hundreds of red tests.
  - Why: hundreds of failures usually come from a handful of causes.
  - Check: a table maps each group to one fix and the count of distinct error lines falls after each fix.
- **3.4 [Java and Maven] Read the resolved dependency tree: if the old major is still on the classpath transitively, the port can be half done and still compile; audit every version-management entry against the effective POM and drop old pins the BOM manages.**
  - When: The code compiles after moving a library to a new major version, or the old parent pom pins versions the new BOM also manages.
  - Why: compile success proved nothing, and old pins over the BOM broke the first real boot.
  - Check: the tree shows no old-major artifacts, a grep for old package names is zero, and the built archive's jar list is compared before and after.
- **3.5 [Java] Dump every feature default from old and new, list the flips and new enforcements, restore the old values in one shared factory for read as well as write, sweep for bare constructions, and compare field order per domain class against the old jar.**
  - When: A serialisation library changes major version, or JSON output differs after the upgrade though the fields are the same.
  - Why: goldens cover only the write side; read-side flips accept malformed input and a changed ordering rule reorders output.
  - Check: an effective-value probe on the built mapper and tests that fail first, before the fix.
- **3.6 [Java] Port matchers by their old semantics (Mockito 1's any(Class) matched null), treat each changed call site as a finding, and expect XML-configured mocks to pick new overloads.**
  - When: You upgrade from an old mocking library to a current major version.
  - Why: the old matcher quietly let production code pass null and the new one fails.
  - Check: a failing-first count (10 of 13 red) then all green after switching to nullable or any, each site named.
- **3.7 [Java and Spring] Look for JVM-static holders (application context, registries) that every context refreshes so the last writer wins; reset them in base classes and add a probe that fails with the reset disabled.**
  - When: Tests pass or fail depending on class order or machine.
  - Why: order-dependent suites look like random flakes and are not reproducible across machines.
  - Check: reverse-order and random-seed runs give identical counts, and the probe fails with the reset removed.
- **3.8 [any stack] Probe trailing slash, encoded slash and double slash on both stacks, including the old container deployed locally, and grep the frontend for emitters before accepting a flip.**
  - When: The new stack is stricter about paths (trailing slash, encoded slash, double slash).
  - Why: router, firewall and connector each now reject URLs the old stack served.
  - Check: a table of probe, old result, new result and layer; every accepted flip is pinned.
- **3.9 [any stack] Write a parse test for every profile and deployment template, assert the next fire times of schedule expressions, and do not fix an expression to what you think it meant: compare with the other profiles.**
  - When: A config profile, deployment template or schedule expression has never been parsed by the application.
  - Why: a malformed cron sat inert in one profile and a first fix changed the schedule.
  - Check: a test over tracked profiles with a drift guard on the profile list, and two next fire times asserted.
- **3.10 [Python 2 to 3] Decide per call site whether data is text or binary: archive and image buffers need BytesIO, hashes need an explicit encoding that reproduces old values, and decoding adds UnicodeDecodeError that a read-error-only handler misses.**
  - When: You replace Python 2 StringIO, cStringIO or md5-of-text code during a port.
  - Why: a blind swap imports cleanly and fails only when the archive is built or read.
  - Check: an export and re-import round trip with real bytes, the same hash-derived folder names on both versions, and invalid UTF-8 input.
- **3.11 [Python 2 to 3] Give every sort or listing whose key can tie an explicit key function with a defined tie-break, and decide in writing what was arbitrary in the old output.**
  - When: Code sorts or lists by a key that can tie, or old output depended on dict or set order.
  - Why: Python 2 compared anything and kept an arbitrary order; Python 3 raises TypeError or follows insertion order.
  - Check: feed tied inputs to every sorted listing and compare old against new.
- **3.12 [Django] Dump every installed app's configuration class (name, label, class path) before and after each step and compare; set default=True on the class and keep the apps module importable.**
  - When: You move a Django project with its own AppConfig classes to Django 4.1 or newer.
  - Why: Django 4.1 silently ignores the old default-app-config setting, so apps lose menus, providers and signals with no error.
  - Check: the dump is identical before and after and a fresh migrate works.
- **3.13 [Django] Grep forms, views and templates for filters on an unsaved instance and related-manager calls that can run on a new object, and guard them with a pk check. Keep primary keys in the session rather than model objects; if you keep a serializer that rebuilds objects, let it name only installed models and round-trip literal marker values as values.**
  - When: You move to Django 5 a project that keeps model objects in sessions or filters by new unsaved objects.
  - Why: Django 2.2 quietly returned nothing, 5 raises, and checkout kept unsaved objects in sessions.
  - Check: open every create page in a test client and round-trip an unsaved object and a decimal.
- **3.14 [Python 2 to 3] Load real fixtures and exercise save, str and admin of every model, visit each view, and sweep by class of removed API and Python 3 change (render with a dict not Context, add on unsaved objects, empty mail recipients).**
  - When: A port 'imports cleanly' and you consider the layer done.
  - Why: removed APIs called inside functions fail only when the line runs.
  - Check: fixture load and a save of every model succeed, and the last step of every flow (the mail, the first add to cart) is tested.
- **3.15 [frameworks that run their own migrations] Build a fresh database from the real migrations on the old and new commit, dump columns (type, nullable, default), indexes and foreign keys per table, and treat every difference beyond the intended ones as a finding; run makemigrations --check twice and do not wrap lists of allowed values in a function, which makes the generator report changes every time.**
  - When: After any library or framework upgrade that runs the app's own database migrations, even with no code change.
  - Why: libraries change schemas themselves, so new installs differ from existing ones.
  - Check: the diff holds only declared differences.
- **3.16 [Django 5] Round money, quantity and measurement decimals to the field's decimal places in your own code before saving, and check boundary ties with the original input on both versions, including exact and range lookups.**
  - When: You move a project that stores money, quantity or measurement decimals to Django 5.
  - Why: Django 5 no longer rounds a decimal to the field's places before handing it to the database, and a fix in a general preparation hook also rounds query operands.
  - Check: a boundary-tie test passes on old and new and the same lookups return the same rows.
- **3.17 [any stack] Feed old user-entered amount formats for each supported locale through the real form handler and declare any accepted-behaviour change.**
  - When: You upgrade a library that carries locale data (Babel, CLDR) and the app parses typed amounts.
  - Why: new data can change separator characters, so a typed amount that was accepted is refused.
  - Check: a table of old formatted amounts per locale shows accept or reject on the new stack.
- **3.18 [PHP and Laravel] Search unit calls whose argument is a variable and convert settings stored as text to whole numbers at each use the way the old library did ('7' is 7, '7.5' is 7, empty is 0), checking whether a real null can reach the call.**
  - When: A date library moves from lenient to strict typing (Carbon 2 to 3) in an app that stores settings as text.
  - Why: the new library throws TypeError for text the old one accepted, mostly in paths tests never reach.
  - Check: test text, empty, non-numeric and null values at each call site against the old version.
- **3.19 [PHP and Laravel] For each package that crosses a major version, read its upgrade notes, diff your published config against the package default two levels deep, name every key it reads with the old value, check every imported class still exists and run the real code path.**
  - When: A framework step moves any package across a major version.
  - Why: renames, new columns and nested keys surface only when the path runs, and defaults merge only one level deep.
  - Check: a written note per package and a test per fault that fails without the fix.
- **3.20 [any stack] Compare behaviour, not the setting value, with scripted input on the real transport: a server without STARTTLS must receive no credentials, a visitor-supplied forwarded address must not change the client address, and unscoped permission checks must still behave.**
  - When: A mail, proxy, TLS, cookie or permission library crosses a major version while you keep the old security configuration.
  - Why: a key can load fine while the new library ignores it or changes its meaning.
  - Check: each refusal is probed on old and new and diffed.
- **3.21 [PHP and Laravel] For every job class the app dispatches add a test that runs the job for real on throwaway inputs and checks its output, seed one real stored artifact for list and download paths, run both on the old commit and on every step, and grep for fake( to find unrun jobs.**
  - When: Your tests only check that a background job was queued, or an upgrade touches a package that stores or lists artifacts.
  - Why: a faked queue proves a job was queued, never that it can run, and empty-list tests pass after a method is removed.
  - Check: green on the old commit, red on a step that removes an API the job uses.
- **3.22 [any stack] Run the test and smoke suites behind an egress block or a recording proxy, and switch off every library's phone-home and cloud-metadata probing in production config.**
  - When: You upgrade an embedded infrastructure library, or run test suites on a machine that may reach the internet.
  - Why: default usage reporting sent hundreds of outbound requests per test run before anyone looked.
  - Check: a count of outbound attempts per run, and zero with the switch set.
- **3.23 [Java] Pull build-plugin, coverage and mock-library upgrades forward as the new runtime demands them, and give legacy libraries temporary open-up flags only with an end date.**
  - When: The first full build on the new JDK or language version goes red before any of your own code is touched.
  - Why: the old test-runner plugin crashed on the new runtime and old reflection libraries needed flags, so the build was red for reasons unrelated to your code.
  - Check: each temporary flag has a removal commit and the final build uses none.

## 4. Prove it

A claim such as "same behaviour" or "fixed" is worth exactly the check behind it. These rules are about checks that can fail, and about claims that are no bigger than their proof. One counting rule is used throughout: a test "went red" only when the intended test was collected, ran, and failed on its own assertion. An import error, a test that never ran or a smaller green run does not count. For how code behaves when things go wrong (bad input, failed writes, half-finished work), use the `review-failure-injection` skill.

- **4.1 [Java, WAR to embedded server] Deploy the actual built archive on the target container with a real database and drive it with a browser (render, failed login, successful login, authenticated read, a mutation check that fails when a field is renamed).**
  - When: Your gates all assemble contexts in-process, or you move from an external application server to an embedded one.
  - Why: every in-process gate was green while the shipped archive could not boot, route or log in.
  - Check: the smoke runs as a standing gate with the archive's jar count and a hash that changes when the build changes.
- **4.2 [any stack] Show each new test red against a deliberately broken version of what it protects (mutation) and red against the old code (failing-first), and state what it does not pin; run the clean suite first and count a mutant caught only when the intended test was collected, ran and failed on its assertion (an import error or a smaller green run is not a catch).**
  - When: You add a pin, guard, golden or any new check to a suite.
  - Why: many pins stayed green with the mechanism removed.
  - Check: two quoted outcomes per test; a mutation that stays green is a finding.
- **4.3 [any stack] Run a control: the same test with the fix reverted must show the failure, and an owned stand-in host outside the environment must be unreachable (never aim probes at a live service).**
  - When: You claim 'does not connect out', 'is offline' or 'was rejected'.
  - Why: an absence of failure may mean the path was never exercised.
  - Check: a 2x2 matrix (fix and proxy flags) in which the control cell fails for the stated reason, with a computed verdict of blocked, reached or inconclusive.
- **4.4 [any stack] Check the build exit code, hash the artifact and require its timestamp after the build start; bind every run to the code under test with a receipt (run id, exit status, test counts) and delete old output first.**
  - When: A recipe builds, deploys and tests, or a comparison reads an output file written by a separate test run.
  - Why: a failed build deployed a stale archive that reported green, and a failed run left the last good capture so the comparison passed.
  - Check: distinct commits give distinct hashes, and a planted import error with the same output path makes the comparison fail.
- **4.5 [any stack] Run the original and the migrated application side by side on the same restored original data and walk every screen and dynamic check, including the old container locally for behaviour the old stack tolerated; keep a tracked acceptance record with a stack table, per-screen results and defects with severities.**
  - When: You prepare the acceptance of the migration.
  - Why: identical only means something against a measured original.
  - Check: the record exists and every deviation is declared.
- **4.6 [any stack] Enforce no-egress with an OS or firewall rule bound to the exact program paths, or an internal network with no route out, and prove it with a probe that checks isolation first (outside blocked, internal control connects); do not rely on proxy variables.**
  - When: The migration or its test run must not contact external hosts.
  - Why: cooperative proxies have many escapes and some HTTP clients ignore proxy variables inside web servers.
  - Check: each toolchain binary fails to reach an owned outside canary host at the socket.
- **4.7 [any stack] Pin an independent toolset with versions and hashes (static analysis, bug patterns, coverage, mutation testing, dependency and removed-API scans, architecture rules, contract tests), record baselines per category and make them fail-closed ratchets.**
  - When: The team wants a final, tool-based acceptance of the migrated code.
  - Why: tool output is evidence that does not depend on the team's own tests.
  - Check: a preflight verifies the pins, each ratchet has a negative control, and a full run from a clean checkout is made at the final commit.
- **4.8 [any stack] Break a module on purpose and prove the harness goes red; make it fail on import errors, empty case lists, repeated names, unused declarations, missing captures and any network attempt, and compare raw bytes, not decoded text.**
  - When: You build a comparison or differential harness as your evidence.
  - Why: 'zero different' may mean both sides skipped the same thing.
  - Check: a broken import, an emptied case list and a wildcard declaration each turn the verdict red.
- **4.9 [any stack] Capture what is SENT below the HTTP library, compare raw bytes first, let the stand-in record every request field, and allow differences only as one declaration (a written note saying which difference is expected and why) per case and field, with exact values; no wildcards, and unused declarations fail.**
  - When: You port code that talks to an outside service (payments, banks) and compare old against new.
  - Why: internal balances can be right while the outgoing request is wrong.
  - Check: doubling an outgoing amount turns a test red and removing a real difference's declaration fails the run.
- **4.10 [any stack] Assert the saved effect (amount stored, row written, stock changed) with non-zero distinct values, call hooks through the real production entry point, and prove each new check with a deliberately broken build that the old suite still passes.**
  - When: An acceptance check says 'the success page appeared'.
  - Why: a page can say success while nothing was recorded, and hand-made arguments can bypass the production branch.
  - Check: skipping the payment step turns the new check red while the old suite stays green.
- **4.11 [any stack] Remove each fix in a throwaway copy and rerun its test; count it proved only if the intended test was collected, ran, and failed on its assertion without the fix and passes with it.**
  - When: You count 'N fixes, each with a test'.
  - Why: removing a fix can also remove a name the test imports, so a tool that accepts any non-zero exit counts a test that never ran as red.
  - Check: the log shows the test id FAILED with an assertion message; a misspelt name or import error reads NOT PROVED.
- **4.12 [any stack] Plant one-line faults one at a time in the money code (answer applied twice, wrong refund sign, payout at gross amount), count a catch only from a healthy run with a new failing test, include controls (a comment-only change is not caught, a broken file is invalid), and list unreached safeguards.**
  - When: You rely on statement coverage as confidence for money code.
  - Why: coverage counts lines run, not lines checked.
  - Check: a fault runner restores the file and reports which check caught each fault.
- **4.13 [any stack] Run scripted stories through the product's own code on old and new, compare the stored tables cell by cell with every difference declared, and show unchanged money code as a byte-for-byte file comparison.**
  - When: You port a money engine and must show old and new behave the same.
  - Why: it finds differences tests do not ask about without trusting the port's own tests.
  - Check: the diff exits clean except for declared cells, and the money module's hash or diff against the old tag is empty.
- **4.14 [any stack] Use three kinds of checks because each finds different faults: the builder's own extra checks, a pre-written outside-in suite, and an independent read of the code against upgrade notes and what each setting promises.**
  - When: You plan the verification of a framework upgrade.
  - Why: a suite sees what users see, a read sees what a setting promises, and neither alone is enough.
  - Check: each recorded fault is tagged with the check that found it.
- **4.15 [any stack] Accept a step as green only with the same test ids and the same outcomes, nothing skipped or deleted, built from one process that discovers every test module, and explain any change in assertion count per test.**
  - When: You claim a framework step is green, and the evidence is a pass count or totals.
  - Why: totals hide swapped, skipped or removed tests, and a hand-kept module list undercounts.
  - Check: diff the id-and-outcome lists before and after; the discovered module count equals the report.
- **4.16 [any stack] Treat a test that fails after an upgrade as a finding: restore the original input, run it on old and new with values printed at each step, name the changed behaviour, and keep a declared list of every edited test with its reason and no new skips (every existing skip explained).**
  - When: A test fails after an upgrade and someone edits its input or expectation to make it pass.
  - Why: nudging an input keeps ids and totals and deletes the case that caught the change.
  - Check: the original input is in the final test and passes on both versions.
- **4.17 [any stack] Submit missing and invalid input to every form and endpoint and compare status, body and content type with the old version.**
  - When: A port's happy-path journeys and checkout tests all pass.
  - Why: a removed error-construction API survives in validation branches that successful journeys never reach.
  - Check: one invalid-input test per form asserting a normal form error, not a server exception.
- **4.18 [any stack] Write the test to state what the OLD version returns for edge cases and run it on both versions; list every route to the behaviour (writes, lookups, nested data, both daylight-saving transitions) and avoid helpers whose error cases differ from the original call.**
  - When: You restore old behaviour after an upgrade (rounding, timezone attach, decoding) with a wrapper or replacement function.
  - Why: a fix for the failing case often changes a neighbouring contract.
  - Check: the same test passes old and new, with a neighbour probe per route.
- **4.19 [any stack] Choose inputs by asking what the old stack would have refused, rounded differently or ordered arbitrarily: exact halves (Python 3 round() is half-to-even), tied sort keys, '1_6' and '32.0' in integer fields, ids after deletes; report which inputs ran, not which endpoints.**
  - When: You write or grade a differential probe of old against new.
  - Why: a probe over every endpoint misses changes when no input sits where the versions differ.
  - Check: the report lists edge inputs per type and each ran on both stacks.
- **4.20 [any stack] Read the skip reasons (-rs) of every CI leg, run xfailed defect tests with --runxfail on old and new and diff the failing assertion lines, assert the intended handler was reached, and keep history-dependent tests from skipping.**
  - When: A suite is green but uses skips, expected failures or history-dependent tests, or you add an earlier admission check in front of failure-path tests.
  - Why: a skip or xfail absorbs any failure, so a new error on the same path or a missing git history hides lost coverage.
  - Check: identical assertion values old and new, and no unexplained SKIPPED.
- **4.21 [Django] For each changed dependency ask which object the normal runtime path supplies, and add a test through that path (the real middleware, the default timezone) rather than one that builds its own legacy object; confirm it fails without the fix and check what mechanical renames touched.**
  - When: An upgrade changes which object the framework hands to your code by default (for example the default timezone implementation).
  - Why: tests that activate the legacy object stay green while real requests get the new type.
  - Check: a request-level test fails if a legacy-only call is used on the new default.
- **4.22 [Django] Compare the new database to the old table by table and row by row from an empty start with the same fixed data, including data the old framework loaded implicitly (initial_data fixtures before Django 1.9), and recreate it in a migration.**
  - When: The old framework loaded data automatically that the new one does not, and you claim the new schema or database equals the old.
  - Why: structure can match while rows quietly go missing.
  - Check: row counts per table match and a saved per-table comparison is built at the final commit.
- **4.23 [any stack] Test each layer alone as well as removing it: take the other layers away and see whether the layer under test still stops the fault.**
  - When: One risk is covered by several layered protections (for example a lock, a unique index and a check in code).
  - Why: removing one of several layers may change nothing, so a removal proof can pass while that layer is untested.
  - Check: for each layer, a test that disables all the other layers and goes red when that layer is removed.
- **4.24 [npm builds] Audit the lockfile, not the installed folder, and fail when the lockfile has no package map or the audit scanned zero packages.**
  - When: You audit licences, install scripts or advisories of a JavaScript module that the migration builds.
  - Why: the installed tree holds only the packages for your own operating system, and an old lockfile format passed with zero packages scanned.
  - Check: a fixture lockfile in the old format makes the audit exit non-zero, and the scanned count equals the lockfile's.
- **4.25 [Maven and npm builds] Pin the Node and npm versions inside the project, fingerprint the built output as a tree hash, and prove it with two clean builds and one build on a different Node version; keep the old libraries unchanged if byte-identical output is the contract.**
  - When: The old front end builds with a toolchain from years ago and its built output is part of the contract.
  - Why: a global Node version rewrote the lockfile and an unmaintained headless browser hung the test phase.
  - Check: the same fingerprint on three machines, and a build without the pin fails loudly.

## 5. Concurrency and data

Races (two requests at the same moment that interfere with each other) and lost data are invisible on a developer machine with one user. They show up on the production database with several workers, and in rows that the old version wrote years ago.

- **5.1 [any stack] Run concurrency probes on the production-grade database with several web workers and judge saved effects (stock, orders, payments), not HTTP statuses; also try cold workers with overlapping first requests and share per-process caches across workers.**
  - When: A change touches stock, orders or any check-then-write, or you move from a single dev server to several workers.
  - Why: single-threaded or SQLite runs hide races, half-imported modules and caches that lose state between steps.
  - Check: the record names database, worker count and repeated runs, and the pass condition is read from the database.
- **5.2 [any stack] Lock the shared rows first in a fixed key order and check inside the lock, and run the old version through the same probe as a baseline.**
  - When: You make a multi-step write all-or-nothing in one transaction around a check-then-change on shared counts.
  - Why: a transaction hides writes until commit, so a gap of milliseconds between check and decrease becomes the length of the whole operation.
  - Check: a concurrent probe on PostgreSQL shows stock never negative and sold never above supply, with old and new results in one record.
- **5.3 [any stack] Decide from values read under the lock (locked order, balance, paid flag, pending row) and compute amounts inside it, never from values the caller worked out before the lock.**
  - When: You add a once-per-order guard (capture, authorise, notify) behind a row lock.
  - Why: a second request that read the balance before the first committed recorded a second authorisation or an overpayment.
  - Check: a deterministic test where request B reads the balance, A completes, then B continues, red on the old code.
- **5.4 [any stack] Use two real database connections (threads) on the production-grade database, hold the first inside the race window with an event, and prove the test by removing the fix and watching it fail on a count (for example the fake provider's call count).**
  - When: You write a test for an 'at the same moment' claim (no double spend, no double count).
  - Why: SQLite's write lock serialises races away and nested calls on one connection prove nothing.
  - Check: with the guard removed the test fails every time; with it, it passes every time.
- **5.5 [any stack] Call a clean concurrency run evidence, not proof: name the machine, database version, workers, run counts per scenario and the scenarios not probed, and run a zero-errors log search on a previous round's log that has known errors.**
  - When: You report a clean concurrency run or a 'zero database errors' result, which is easy to overclaim.
  - Why: a race that did not happen in one run says nothing about the next, and a search that never finds anything proves nothing.
  - Check: the report lists scenarios, run counts and untested ones.
- **5.6 [any stack] Send notifications after the commit and log failures, name which operations stay outside the transaction, and inject a failure just after commit to see what state remains.**
  - When: Mail, ship notices or captures run inside a database transaction, or you claim an operation is all or nothing.
  - Why: a failing handler inside the money transaction erases the payment, while work after the commit can fail and leave a paid order with an error.
  - Check: a failing mail handler leaves order and payment saved, and the state after a post-commit failure is documented.
- **5.7 [any stack] Write a small tool that scans real or restored data for typed variants of key fields (numbers stored as strings and the reverse) on the old and the new server, and fails on any incompatibility; prove the tool by breaking its input on purpose.**
  - When: A new driver or mapper handles typed values (numbers, strings, dates) differently.
  - Why: a new driver's typed calls throw on values that test fixtures never held.
  - Check: exact counts per type, and a deliberately broken query makes the tool fail instead of passing through a stand-in that ignores its input.
- **5.8 [Java and Spring] Do not keep state about one request in a shared object; carry it in a per-request details object, delete the field and setter, and test with barrier-forced interleavings over the real wired beans, including other paths that read the same flag.**
  - When: A singleton authentication provider or service holds per-request state in a field.
  - Why: interleaving logins can leak state from one session into another.
  - Check: race cells are red on the old code and green with the token carrying the flag, and a mutation turns them red again.
- **5.9 [any stack] Stop patching after three or more review rounds on one concurrent mechanism that each find a new failure. Write a table of every operation and every way it can fail, state the invariant that must hold at the end whatever fails, and decide in writing what to implement and what to leave as a known limit.**
  - When: After three or more review rounds on the same concurrent mechanism, each round finds a new failure cell.
  - Why: the recovery signal was smaller than the state it had to recover, so each fix exposed the next failure.
  - Check: a design note with the table, the root cause in one sentence and the decision.
- **5.10 [Spring Data] List the indexes on a fresh database created by the old and the new stack and compare definitions including unique flags; recreate them explicitly when automatic creation is off, and make seeds idempotent.**
  - When: You upgrade Spring Data or a similar layer for a document database.
  - Why: unique indexes silently stopped being created and duplicates were accepted.
  - Check: a duplicate insert throws on the new stack and the index lists match collection by collection.
- **5.11 [any stack] Run the app's own tests and the outside-in suite once on the production database engine, and use IMMEDIATE transactions and lock timeouts on SQLite because select_for_update does nothing there.**
  - When: Your upgrade was tested only on SQLite or on one database engine, or you make order completion atomic on SQLite.
  - Why: engines differ on rounding, unsigned columns, strict mode, unknown columns, id reuse after rollback, case sensitivity and locking.
  - Check: per-test outcomes are compared between engines and each difference is treated as a finding.
- **5.12 [any stack] Prove a rollback by running forward, reversing, then running the old release on the rows the new code wrote, including a resumed workflow; fence old binaries at every old write path, not one reader.**
  - When: A schema or data migration has a down step, or claims old binaries cannot use the upgraded store.
  - Why: restoring a column does not restore the old meaning of its data, and a version check in one reader leaves other writers open.
  - Check: the old page opens migrated rows or fails cleanly, and every old write entry refuses and leaves rows unchanged.
- **5.13 [any stack] After a migration drops or rewrites a column that held a secret, search the storage for the old values. On SQLite turn on secure delete first, then search the database file and its -wal and -journal files. On PostgreSQL run VACUUM FULL and treat WAL archives, backups and replicas as copies that still hold them (this half is advice; it was not tested).**
  - When: A migration removes or rewrites an encrypted or secret column.
  - Why: dropping or blanking a column leaves the old bytes in free pages and logs.
  - Check: a byte search of the database files, journals, backups and replicas finds none of the old values.
- **5.14 [any stack] Test the upgrade on a populated database written by each previous release (every writer, including optional fields they always filled), and say plainly which old records cannot be classified or what was not tested.**
  - When: Tests run only on fresh databases or on data written by the new version, while production holds data written by older releases.
  - Why: new-writer tests miss old shapes, and a fallback to the old algorithm can keep every old bug alive.
  - Check: a regression test loads old-format data and fails on the pre-fix code.
- **5.15 [any stack] Rerun a differing scenario alone on both sides before declaring a version difference, and TRUNCATE tables before each scenario or sort inside the picture.**
  - When: You compare database state between runs or versions and code reads or updates rows without ORDER BY.
  - Why: PostgreSQL returns unordered rows in physical order and DELETE leaves dead row versions, so order depends on which tests ran before.
  - Check: a scenario that differed in the full run gives the same result alone.
- **5.16 [any stack] Bind the signed cookie to a per-login id and require a matching stored record; never recreate a session from a cookie, and rotate the session id on login.**
  - When: Sessions or signed cookies must stop working after logout or a new login.
  - Why: a cookie saved before logout stayed valid until it expired, and a session id that stays the same across logins can be reused.
  - Check: probes written as test cells: reuse a saved cookie after logout, reuse the first cookie after a re-login, and two parallel logins.
- **5.17 [any stack] Find out what the success value means (queue accepted, worker ran, broker acknowledged) and record 'confirmed' only on the signal that proves the effect; allow for thread pools that reorder two quick calls.**
  - When: You record 'confirmed' after publishing to a queue or broker.
  - Why: a publish call returned an id even after it threw, and a pool of five threads reordered two back-to-back publishes.
  - Check: executed probes of the dispatch layer, plus a control with a pool of one that keeps order.
- **5.18 [any stack] Stop duplicates with a conditional write on the key, or a per-order record written before the side effect; do not rely on a framework's 'run once' or 'no overlap' job feature, which does not deduplicate a redelivered message.**
  - When: A message or job can be delivered more than once (redelivery, retry, double click) and has a side effect.
  - Why: those features guard a schedule, not a message, so the second delivery runs the side effect again.
  - Check: deliver the same message twice, and twice at once, and count the side effects, the saved records and the outgoing calls; with the guard removed the count is 2.
- **5.19 [any stack] Check how the SDK reads stored decimals: some return floating-point numbers, so large amounts lose precision. Read numbers as strings, or store integer minor units (cents) and convert at the edge.**
  - When: A key-value or document store holds decimals (money, quantities) that you read through a vendor SDK.
  - Why: a float cannot hold every large amount exactly, and the error shows only on big or fine values.
  - Check: round-trip a large amount and a fractional amount through save and load on the real SDK version and compare them exactly.
- **5.20 [any stack] Test a worker killed after the side effect but before it acknowledges the message: the message can be redelivered until it lands in a dead-letter queue, and the order can be lost silently. Probe the saved state and the contents of the dead-letter queue, not only a count of messages.**
  - When: A worker consumes queue messages and has a side effect before it acknowledges the message.
  - Why: a count can look right while the order exists nowhere.
  - Check: kill the worker at that exact point and show where the order is afterwards: saved, retried, or in the dead-letter queue with its reason.

## 6. Packaging and runtime

Moving the code is half the work; the other half is how it is built, started, configured, stopped and rolled back. Settings that did nothing on the old runtime can become live on the new one, and the other way round.

- **6.1 [Java, WAR to embedded server] Recover the descriptor contract from version history (servlets, mappings, filters, listeners, missing encoding filter) and reproduce it in code; inventory behaviour that lives outside the code (context parameters, JVM options in start scripts, connector settings) from redacted copies of the real host.**
  - When: You replace web.xml and an external servlet container with an executable jar and embedded container.
  - Why: auto-configuration adds or drops things silently and ignored settings go live.
  - Check: a request test asserting path and filter order, and a probe that goes red on a planted difference.
- **6.2 [Java] State plainly that the smoke ran on the target container, not the deployed one; change deployment descriptors by edit-and-diff with an owner checklist per descriptor (OS, init system, service name, bypass paths), and replace unchecked plain-HTTP downloads with HTTPS plus a checksum.**
  - When: The deployed servlet container cannot run the new archive.
  - Why: the deployed servlet container was too old to host a Jakarta WAR and descriptors carried stale names and OS assumptions.
  - Check: the owner signs off each checklist item.
- **6.3 [Java] Compare archives by listing contents (library list, file counts, tree fingerprint), not by archive hash, and keep test-scoped and provided jars out.**
  - When: You compare archives between builds.
  - Why: a hash only shows that a file changed (useful to show a new build really differs), not that two builds differ in content: four builds of one tree gave four digests while being identical entry by entry.
  - Check: the library count and names are as expected, test jars are absent and the dist fingerprint matches.
- **6.4 [Java and Maven] Never run a bare reactor build or install to test one module: exclude modules with docker, native or network steps, use -pl module -am with a per-person local repository, and count project artifacts in shared and home repositories before and after every gate.**
  - When: A module in the build reactor builds images or binds external steps, or you run install to test one module.
  - Why: a default-lifecycle docker build contacted the daemon, and installed artifacts shadow reactor builds.
  - Check: logs show no contact and zero project artifacts in shared caches.
- **6.5 [Maven and npm builds] Set the cache or temp location in the tool's own global configuration (Maven localRepository, npm cache) and pass explicit properties (-Dmaven.repo.local, -Djava.io.tmpdir); never forward a whole option string through an environment filter, and check the first tool version that reads each variable.**
  - When: You route a build tool's cache or temp directory into a project folder while some child processes drop environment variables.
  - Why: variables reach only children whose environment keeps them, and project or user settings can override a global default.
  - Check: build inside the strictest boundary and print where it wrote.
- **6.6 [any stack] A recipe is not done until it has run end to end from a clean checkout on the target platform, twice in a row, from an empty database and cache, with exit status propagated and a scenario with no recorded effect reported as inconclusive rather than failed.**
  - When: You write a recipe or script that others will run, often on a different platform from yours.
  - Why: your own environment hides holes: line endings, leftover data, shared caches, unpinned drivers, a test job that exited 0 after failed tests.
  - Check: two consecutive clean runs give the same result.
- **6.7 [Docker images] Compare the built image's file tree with a manifest of the commit, bind the running container to its immutable image id and config (User, Env, StopSignal, entrypoint), and anchor ignore patterns to the folder you mean (/vendor, not **/vendor).**
  - When: You build an app image with a shared dockerignore file, or claim the tested image is the built image.
  - Why: an ignore rule silently dropped the app's own nested vendor folders, and equal layers can carry a different user or stop signal.
  - Check: the tree equals the manifest, and a wrong image or a one-byte change fails the gate.
- **6.8 [containers] End the image with a numeric USER (such as 33 or 10001), copy config files in rather than bind-mounting into a sticky world-writable web root, and for a read-only root list every write location (temp, thumbnails, caches, control sockets, installer files) with its own writable volume.**
  - When: You build a base image for Kubernetes with a non-root user, a read-only root or mounted config files.
  - Why: the kubelet refuses a named user under runAsNonRoot, and fs.protected_regular blocks writes to bind-mounted files.
  - Check: start the container as it will run, with the installer and the first request.
- **6.9 [PHP] php-fpm clears the environment by default, so set clear_env to no for secrets and settings. Guzzle ignores the upper-case HTTP_PROXY variable outside the command line (use lower-case http_proxy), while HTTPS_PROXY is read everywhere; better still, enforce no-egress with a network that has no route out.**
  - When: You move a PHP app into php-fpm or behind an egress proxy and pass configuration or secrets through the environment.
  - Why: the container held the secret but the app answered 500, and a proxy meant to watch outgoing calls was bypassed for plain http.
  - Check: log in with the secret's database password and see a deliberate outside call reach the tripwire.
- **6.10 [Kubernetes] Run installs and migrations in a separate Job, make readiness depend on the complete schema (applied migrations equal the files in the image plus a marker written after seeding), label web and job pods differently so the Service selects web pods only, and keep job reruns idempotent.**
  - When: A Deployment with a migration Job, a readiness probe and more than one replica.
  - Why: a home-page or row-count probe passes on a half-migrated schema and the Job pod became a ready endpoint.
  - Check: pause the Job halfway and log EndpointSlices; no pod is ready.
- **6.11 [Kubernetes] Add a preStop delay (a few seconds) so the pod leaves the Service before it stops, set resource requests as well as limits so the surge pod can schedule, give tests a warm-up for fresh pods, and read the image's real StopSignal before explaining a failure; change one factor at a time.**
  - When: You roll a Deployment under traffic, or explain a restart or shutdown failure by 'SIGTERM'.
  - Why: requests fail during rolling restarts and the assumed SIGTERM story was wrong.
  - Check: a load test across a rolling restart with and without the hook.
- **6.12 [Kubernetes] Prove the cut-off from inside the pod with a control: the same probe from a policy-free pod must reach a stand-in host and the app pod must not; check the union of all egress rules that select the app pods and probe every serving pod; block the host and its ports too.**
  - When: You claim a Kubernetes NetworkPolicy or an internal network cuts the test environment off from the outside.
  - Why: policy can be silently unenforced and an internal network only keeps out the internet.
  - Check: the gate refuses an extra rule, a missing probe and a down control.
- **6.13 [containers] Run the same code on the current language version in the container first, and keep a two-by-two table (host or container, current or new version).**
  - When: You move to containers and a new language version together.
  - Why: in three Python projects whose earlier runs were on Windows, more results changed with the move to Linux than with the new language version.
  - Check: the table attributes each change to one factor.
- **6.14 [any stack] Rehearse on a copy of the data built by the old runtime, run the new version's pre-start checks one by one, hand the same data back to the old runtime to prove rollback, make rollback recover partial writes from a verified original without depending on the failed runtime, and start the next window from the fixed scripts of the last.**
  - When: You prepare an upgrade of a service whose ports and data are live, or reuse the previous upgrade's scripts.
  - Why: reverse string replacement does not recover a partial write.
  - Check: the log shows each check, one real transaction and an old-runtime read of new data.

## 7. Review and hand-over

A migration is finished when someone who did not do it can rebuild it from your recipe and get the same results. Review is part of the method, not a last check. For reading a diff, use the `review-code` skill.

- **7.1 [any stack] Have a reader who has not seen the plan or expectations read the diff against the baseline commit, with neutral wording and no hint of the conclusion; give the evidence pack to someone who did not follow the work.**
  - When: Before declaring a slice or an evidence pack done.
  - Why: cold readers find classes of defects that briefed reviewers miss.
  - Check: the brief states only the commit range and baseline, and findings come back severity-tagged.
- **7.2 [any stack] Give the finished, committed work to an independent reader, ideally built by a different vendor or tool, who rebuilds it from the written recipe in a clean checkout and tries to break it.**
  - When: A team is about to declare a port or upgrade done on its own evidence (self-certified).
  - Why: the team's tests and same-vendor reviewers share its blind spots.
  - Check: the reader reproduces every test id and outcome from a clean checkout and returns a verdict with severity-tagged findings.
- **7.3 [any stack] Besides per-commit verdicts, sweep the whole slice for each known class of porting fault (removed APIs on every path, ordering, bytes versus text, shared state, equal sort keys), and treat one confirmed finding as an instance of a class.**
  - When: You review a long series of small porting commits, or a review confirms one finding.
  - Why: per-commit review leans on the suite and never asks where else the same mistake lives.
  - Check: each class has a recorded search and result.
- **7.4 [any stack] Record the old contract, the new behaviour, the layer where it happens, the decision and the owner, and re-pin the fixture with a diff review, never a blind re-record.**
  - When: Old and new behaviour differ, you decide to accept the new one, and a golden file must be re-recorded.
  - Why: silent re-recording destroys the baseline.
  - Check: the golden diff is empty unless a declared re-record has a recorded review.
- **7.5 [any stack] Keep a decision document for the owner with release-blocking, scope and information items; write business-rule oddities and old bugs down as questions with the code or commit showing current intent, classify each as migration-caused, pre-existing or unknown, and do not change them silently.**
  - When: Findings need a decision from the system's owner, a defect turns out to be a business rule, or the migration surfaces bugs older than itself.
  - Why: maintainers may have meant it, and fixing one cause of a coupled symptom closes neither.
  - Check: each item has an owner, a question and what it blocks.
- **7.6 [any stack] Define the finish line as same behaviour on the new stack, proven, park UI rewrites, refactors and new features until after acceptance, and have the owner ratify an out-of-scope list; faults already in the old code go to the owner as decisions, not into the migration.**
  - When: Improvement ideas appear while you migrate.
  - Why: improvement slices consumed many review rounds and diluted the migration.
  - Check: a written finish line and an out-of-scope list the owner has signed.
- **7.7 [any stack] Write every claim at the size of its proof and name the boundary of each all-or-nothing or once claim; search the code before writing that something is not done; do not write 'all differences come from the framework' unless the probe fed inputs that would show an app-level difference.**
  - When: You write a result page, release note or summary for a migration.
  - Why: reviewers repeatedly found sentences stronger than the code.
  - Check: each claim pairs with a test, command or probe and states its boundary.
- **7.8 [any stack] Regenerate every number and file at the final commit, read counts out of the committed result files and compare them with the page, name the exact commit beside each artifact, and keep a clean run's worktree until its files are committed.**
  - When: A result page cites test counts, lint numbers or artifact files.
  - Why: a page's final count was backed by an older run that still held a withdrawn test.
  - Check: counts parsed from the tagged files equal the page, and a fresh run on the tag matches.
- **7.9 [any stack] End with a 'not run' list and a 'which runs count' note: name the platforms, databases and scenarios actually executed (read the CI matrix), label one run per variant as one run, and say what a single-node or laptop environment cannot show.**
  - When: You write the result of a migration or claim a change is safe on several platforms.
  - Why: it makes the size of the proof visible and honest.
  - Check: every claim matches an entry in proved or not run.
- **7.10 [any stack] Test the guide blind: questions and an answer key written from the code by someone who has not read the guide, a fresh reader answering from the guide alone, and a control from the project's own docs; write the business rules and what really happens on the default path, not only code layout.**
  - When: You hand over a newcomer's guide to a system you just ported.
  - Why: the author's own questions give a flattering score.
  - Check: graded scores per category and a list of material errors with file and line.
- **7.11 [any stack] Run every step exactly as written in a throwaway environment of the exact release, as a newcomer would, and record which commands were run and which were not, with prerequisites, clean-up between steps and the real output line.**
  - When: You write or review a setup guide, runbook or replay recipe that tells a reader to run commands.
  - Why: reading alone missed install sources, hidden items and a failing step that cost 13 minutes.
  - Check: someone who did not write it reproduces the stated output.
- **7.12 [any stack] Leave a guard test in the repository for each fixed trap, and keep the output of each removal proof, not just a claim. Also keep four records of a well-run migration: the frozen list of test ids from the old version, a dump of the silent defaults, removal proofs that show the restored code identical to the original, and repeat runs from clean copies.**
  - When: A migration run ends and its evidence lives in a side folder.
  - Why: evidence in a side folder guards nothing for the next maintainer.
  - Check: reverting each trap fix in a copy turns a repository test red.

## 8. General practice, not measured in our runs

The rules above come from migrations we ran and measured. The points below are standard practice that our runs did not test; they are here because a migration that skips them can pass every rule above and still hurt.

- **Measure speed before and after.** Record response time, throughput, memory, start-up time and the number of database queries for the main requests on the old version, under a fixed load, then repeat on the new version. A change that keeps behaviour but doubles response time, or that moves memory use through a new garbage collector, passes every rule above. The `test-performance` skill covers how to run and record such a measurement.
- **Plan the cut-over.** Decide how the new version takes over: a gradual rollout, old and new side by side, or a switch that turns the new code on and off. Time the data and schema migrations on a copy of production-size data, and note any table lock or downtime they cause (add new columns before you remove old ones, so that the old version keeps working). After go-live, compare errors and latency with the old version, and keep the old system ready to take over for the whole rollback window. The `review-release-readiness` skill covers the release check.
- **Know the silent changes of your language.** These raise no error:
  - Python 2 to 3: `/` on two integers returns a fraction (use `//` for the old behaviour); `map`, `filter`, `zip` and dictionary views return one-shot iterators or views, not lists; text and bytes are different types and meet at every database, file and network boundary; dictionaries keep insertion order (Python 2 order was arbitrary); data pickled or cached by Python 2 has to be read by Python 3.
  - Java 8 to 17 or 21: the default character set is UTF-8 from Java 18; the default locale data (CLDR) changed from Java 9, so dates and numbers can print differently; the Java EE modules were removed in Java 11; Hibernate 6 comes with Spring Boot 3 (Spring Boot 4 ships Hibernate 7); Spring Boot's properties migrator reports settings that were renamed.
- **Treat automated rewrite tools as a change to review.** Tools such as OpenRewrite (Java), pyupgrade and django-upgrade (Python) and Rector (PHP) are useful, but run each in its own commit, read what it changed, and rerun the baseline afterwards. One rename script in our runs turned a compatibility alias into a self-assignment.

## Where this comes from

Six sources, all practice rather than theory:

- Practice migrations of public open-source projects: Satchmo (Python 2 to 3, Django 1 to 5), Shuup (Django 2.2 to 5.2), Liberapay (payments, Python 3.13 and 3.14), Crater (PHP and Laravel, five major upgrades from Laravel 8 to 13) and django-avatar (a small app used for blind tests).
- A programme that moved a Java 8, Spring 4 application packaged as a WAR to Java 21, Spring Boot 4.1 and Tomcat 11, and a rerun of it with the first run's lessons applied.
- Container and Kubernetes studies of those images: what a pod spec must provide, and what the rolling restart and the readiness check really do.
- Blind tests of the method itself: independent runs that migrated a small app from a written method, graded against a key the runs never saw. The main finding was that a differential probe must use inputs where the old and new versions can differ (exact halves, tied sort keys, inputs the old version refused), not just endpoints.
- A practice port of an order-handling service onto a key-value store and a message queue, run against local stand-ins.
- The team's own working practice: reviews, hand-overs and operations around those migrations.
