# Comprehension for Python codebases (design)

Status: proposed, design only. Part C of the Python practice week (work item `py-c-comprehension`). Its challenge (`challenge-python-learning-20261014`) returned "reshape", and the reshape was accepted: a reviewed design and a spike, with nothing merged or released this week. Kill signal: any builder change merged this week, or the spike taking more than two days. Nothing in this document is built. Where it describes how agenttalk works today, it matches master `233b496a`; the measurements in it were taken on 2026-10-14.

**In plain words:** `agenttalk comprehension` makes an offline inventory of a repository: its parts, how they depend on each other, where work enters it, and how ready each part is, so a team can understand old code before a migration. Today it reads only Java: on a Python repository it lists every file but understands none of them, and it can still call the run "complete". This design proposes how it could read Python too: first a shared, language-neutral form for what a language reader reports, so the four artifact builders stop depending on Java types; then a Python reader that records modules, imports, classes, functions, Django routes and views, models, management commands, settings and entry points, and names every dynamic import it cannot follow. Old Python 2 code is read without lib2to3: the standard parser first, then a short, fixed list of repairs that keep every line in place, then a token-level outline for whatever still fails. The design decides the interface, the Python 2 approach, the builder changes and what a two-day spike must show; the build size and a few vocabulary choices are left to the review (section 12).

Audience: the lead and the reviewers, who decide; the builder of the spike. Mode: explanation (a design).

## Contents

1. [What happens today](#1-what-happens-today)
2. [The plan at a glance](#2-the-plan-at-a-glance)
3. [A language-neutral claim interface](#3-a-language-neutral-claim-interface)
4. [What the Python reader records](#4-what-the-python-reader-records)
5. [Reading Python 2 without lib2to3](#5-reading-python-2-without-lib2to3)
6. [Changes to discovery, the worker, the scan pipeline and the builders](#6-changes-to-discovery-the-worker-the-scan-pipeline-and-the-builders)
7. [What stays Java-only](#7-what-stays-java-only)
8. [What the spike will prove](#8-what-the-spike-will-prove)
9. [What it costs](#9-what-it-costs)
10. [What it commits us to](#10-what-it-commits-us-to)
11. [What we learn even if it is never built](#11-what-we-learn-even-if-it-is-never-built)
12. [Open questions](#12-open-questions)
13. [Technical notes](#13-technical-notes)

## 1. What happens today

`agenttalk comprehension scan` walks a repository, sends every file to a separate worker process, and publishes five artifacts: modules, dependencies, features (with entry points), readiness, and problems. The full contract is `docs/DESIGN-55-comprehension-plane.md`; its known gaps are in `docs/COMPREHENSION-LIMITATIONS.md`.

Only one language reader exists, for Java, plus readers for Maven's `pom.xml` and the servlet descriptor `web.xml`. Every other file that is not on a short list of harmless types (documentation, plain text, lock files, images) becomes a bare file unit with an `unsupported_language` problem. Which of those problems make the run "degraded" depends on a fixed list of file types; `.py` is deliberately not on it, because in a Java repository a Python file is usually a helper script.

**Measured on 2026-10-14**, with today's scanner run offline on read-only copies of the two practice codebases:

| Codebase | Python files | Run status | What it understood |
|---|---|---|---|
| Satchmo 0.9.2 (Python 2, Django 1.x) | 492 | **complete** | 1,139 file units, every one of language `unknown`; no dependencies, no entry points; 1,034 warnings, none degrading |
| Shuup (Python 3, Django 1.11-2.2) | 1,638 | degraded | 3,004 file units, all `unknown`; no dependencies, no entry points. The one degrading problem is a single `.sql` file; the 1,638 Python files are warnings that do not count |

So a Python repository can scan "complete" while nothing in it was understood. Section 11 proposes a small fix for that, independent of this design.

**Why Python is not a drop-in addition** (the challenge's finding, confirmed in the code):

- The four artifact builders (modules, dependencies, features, readiness: about 4,900 lines) take the Java reader's own result type. Three of them import it directly, and readiness depends on Java reason codes.
- The worker calls the Maven reader by name, and its only language registry maps `.java` to the Java reader.
- Discovery's exemptions for source folders are Maven-shaped (`src/main/java` and similar).
- The Java reader alone is 8,335 lines. The pull request that built it together with the scanner (#132) went through about fifty review rounds.

## 2. The plan at a glance

Three layers, built in this order, each one only after the previous one is proven:

1. **A shared claim interface.** One neutral description of what a language reader found in a file, which all four builders take instead of Java's types. Java is moved behind it first, with no Python at all. The proof: every Java artifact keeps exactly the same content (the same canonical content digest), and the existing comprehension tests pass unchanged.
2. **A Python reader.** It reads one `.py` file at a time and reports in the shared form, including Python 2 files.
3. **Python rules in the pipeline.** Python-aware discovery and test classification, a way to resolve Python imports across files, and Django URL composition across files.

**Before any of it: a spike** of at most two days (section 8), on a branch that is never merged. It checks the two riskiest points with real code, on Satchmo and Shuup: that Java fits the shared form without loss, and that Python 2 and Python 3 files can be read at useful rates.

## 3. A language-neutral claim interface

### What it is

A "claim" is one fact a language reader asserts about one file: "this file declares class X", "this file imports module Y", "this function serves URL pattern Z". Today these are Java types, and the builders read Java-only fields from them. The proposal is one shared set of claim types plus, for each language, a set of rules that the builders call wherever a decision really depends on the language.

### The claim types (proposed)

A new module, `comprehension/claims.py`, holds:

- **`FileClaims`:** everything one reader found in one file.
  - `adapter`: the reader's name, version and rule version.
  - `language`: for example `java` or `python`.
  - `relative_path`.
  - `namespace`: the Java package, or the Python dotted module name; empty when there is none.
  - `reader`: how the file was read, from a closed list per language. For Python: `python-ast`, `python-ast-repaired` or `python-tokens` (section 5).
  - `units`, `edges`, `entry_points`, `problems`: lists of the four claim types below.
  - `extras`: facts only that language's own rules read, which the builders pass through untouched. Java's are its Maven module paths, its descriptor name conflicts and its servlet and filter names. Python's are its URL-module facts and its function names.
- **`UnitClaim`:** `relative_path`, `qualified_name`, `simple_name`, `line`, `classification`, and `traits`, a set of words from a closed list per language. Java's five yes/no facts (interface, abstract, enum, inner, local) become traits; the Java-only step that checks whether a servlet class can be created reads them.
- **`EdgeClaim`:** `from_qualified_name`, `relation` (from the existing closed list: `import`, `include`, `inherit`, `invoke`, `route`, `data`, `configuration`, `build`, `test`), `target` (raw and unresolved), `target_kind` (a hint from a closed list per language, read only by that language's resolver), `evidence_class`, `line`, `phase` and `optional`.
- **`EntryPointClaim`:** `qualified_name` (the owner), `kind`, `name` (bounded: a route pattern, a command name, or `main`), `line`, `evidence_class`, and `traits`. Java's "`main` written with a bare `String`" becomes a trait.
- **`ProblemClaim`:** `reason_code`, `detail`, `qualified_name`.

The worker sends one `FileClaims` per file to the scan pipeline as JSON, with `language` telling them apart. That is a new worker wire format, so `WORKER_SCHEMA_VERSION` goes from 1 to 2.

### The language rules (proposed)

Each language registers one rules object next to its reader. The builders call it only where the answer depends on the language:

| Rule | Java (moved, unchanged) | Python (new) |
|---|---|---|
| Which files it reads | `.java`, plus `pom.xml` and `web.xml` by name | `.py`; `setup.py`, `setup.cfg`, `pyproject.toml` and `requirements*.txt` by name |
| Language label of a file | today's `.java` mapping | `python` |
| Test classification | today's Maven test roots, name suffixes and test-framework imports | `tests.py`, `test_*.py`, `*_tests.py`, `conftest.py`, and `tests/` folders at any depth |
| Resolving an edge's target | today's ladder: static imports, wildcard imports, Java scope rules, `java.lang` names, binary `$` names, Maven coordinates | section 4: package-relative and absolute imports, Python 2 implicit relative imports, import roots |
| Steps that need several files | the Maven reactor rule, the second `web.xml` pass, the servlet check | Django URL composition; choosing which settings module is the root |
| Capability tables for `scan.json` | today's unsupported relations, unsupported entry-point shapes, entry-point kinds and caveats | the Python ones (section 4) |
| Readiness checks for its reason codes | today's table | a table for the Python codes |
| Label for a candidate feature | today's split on `#` or the last dot | the view's module, or the command's name |

The readiness tables are merged when the module is loaded, and a reason code without a check is refused at that point. Today such a gap would only show up as a runtime `KeyError`.

### How Java moves behind it without risk

`adapters/java.py` is not edited. A thin converter turns its result into `FileClaims` and back. The builders change to take `dict[str, FileClaims]` plus the rules registry, and the Java-only parts of the builders move into the Java rules object as they are. Two checks must hold before any Python code is added:

- converting Java results to the shared form and back gives exactly what the reader produced, for every Java source the comprehension tests use;
- on every existing Java fixture, and on one real Java repository, the published artifacts have the same canonical content digest as on master, and the existing comprehension tests pass unchanged: 1,518 tests in 22 files at master `233b496a`, counted with `python -m pytest --collect-only -q tests/*comprehension*.py`.

This interface is also what `docs/COMPREHENSION-LIMITATIONS.md` calls "the adapter interface": its plan to replace the regex Java reader with tree-sitter needs the same thing.

## 4. What the Python reader records

The reader works on one file at a time, inside the existing worker. It never imports or runs the code it reads. It stores only pointers and identifiers (paths, dotted names, line numbers, route patterns), never source text. That is the existing rule (DESIGN-55, core invariant 3), and the Java reader already stores route patterns the same way.

| What | How it is found | Where it lands |
|---|---|---|
| **Modules and packages** | from the file's path and the import roots (below) | the file unit, with its dotted module name as `namespace` |
| **Imports** | `import a.b`, `from a.b import c`, `from . import x`, `from ..y import z` | `import` edges, resolved across files by the Python rules |
| **Classes** | every class defined at module level, and classes nested in them | a `component` unit per class (`package.module.Class`), as Java does per type |
| **Functions** | every function defined at module level | recorded in `extras`, so routes and imports can point at them; not a unit of their own (a symbol inventory is a later slice, S2, in DESIGN-55) |
| **Inheritance** | class bases, resolved through the file's own imports | `inherit` edges |
| **Django URL routes and views** | `patterns('prefix', ...)`, `url(...)`, `path(...)`, `re_path(...)` and tuple entries in `urlpatterns`; views given as callables, as `View.as_view()`, or as dotted strings | `route` edges and `http_route` entry points, owned by the view's class unit or its module's file unit |
| **URL includes** | `include('dotted.urls')` and `include(module)` | `include` edges; composed into full patterns across files (below) |
| **Models** | classes whose bases lead to `django.db.models.Model`, directly or through other classes in the scan | no new field: the `inherit` edges already say it, and a report can follow them |
| **Management commands** | `<app>/management/commands/<name>.py` defining `Command` | `cli_command` entry points (a new kind) named after the command |
| **Other entry points** | `console_scripts` in `setup.py`, `setup.cfg` or `pyproject.toml`; modules with an `if __name__ == "__main__":` block | `cli_command`, and `cli_main` for a module that starts a program |
| **Settings** | settings modules (named `settings*.py`, inside a `settings` package, or named by `DJANGO_SETTINGS_MODULE` in `manage.py` or `wsgi.py`) | `configuration` edges, only for the settings slots below; `http_filter` entry points for middleware |
| **Declared dependencies** | `install_requires` and similar in the packaging files; `requirements*.txt` | `build` edges to external distribution names (names only, no versions, as for Maven today) |
| **Dynamic imports it cannot resolve** | `importlib.import_module(...)`, `__import__(...)`, Django's `import_string(...)` and `get_callable(...)` | with a literal dotted name: an `import` edge with evidence class `inferred`; otherwise a `dynamic_import_unresolved` problem at that line |

**Settings: code references only.** Settings files often hold passwords, hosts and database names. The reader reads only these slots, and only when their value is a literal list, tuple or string, or literal pieces joined with `+`: `INSTALLED_APPS`, `MIDDLEWARE`, `MIDDLEWARE_CLASSES`, `ROOT_URLCONF`, `WSGI_APPLICATION`, `AUTHENTICATION_BACKENDS`, the template context processors, and the database and cache `ENGINE` / `BACKEND` values. Each value must be a dotted Python name. A slot built at run time gives a `settings_value_not_static` problem. No other setting's value is ever read into a claim. DESIGN-55 already requires a canary test for secrets and literals, and it would cover this.

**Import roots.** A dotted module name depends on which folder Python starts from. The reader works out the candidates in this order:
1. the package folders that `setup.py`, `setup.cfg` or `pyproject.toml` declare;
2. the parent of the top-most folder in each unbroken chain of `__init__.py` files. A chain stops at the scan root: an `__init__.py` in the scanned folder itself never counts, so an import root is never above the scanned folder.

For Satchmo, both give `satchmo/apps`: its `setup.py` declares that folder, and `satchmo/apps` itself has no `__init__.py`. Satchmo's source package also has a stray `__init__.py` at its top; without the stop, the chain from `satchmo/__init__.py` would run past the scanned folder. An import that still matches nothing is tried as a suffix of an in-scan path. If that matches, the edge is marked `ambiguous`; it is never presented as resolved.

**External targets.** A top-level name in `sys.stdlib_module_names` (Python 3.10 and later; 297 names on 3.14) is the standard library. Python 2's own standard-library names (`urllib2`, `cPickle`, `StringIO` and others) are not on that list, so the reader carries a short fixed list of them. Anything else outside the scan is external, under its top-level package name. Mapping a package name to the distribution that provides it is a named limit.

**Django URL composition across files.** Each URL module reports its local patterns and its includes. A cross-file step then starts from each `ROOT_URLCONF` it found and joins the prefixes along every include chain whose links are all static. A pattern reached only through a dynamic link stays local, and the run publishes a composition caveat, as Java does for container prefixes. Examples of dynamic links: Satchmo's `collect_urls` signal, which lets apps add patterns while the site runs; Shuup's URL lists loaded through its `provides` registry; an include of a computed name. When there are several settings modules and so several roots, the patterns are composed from each root and marked `ambiguous`. agenttalk does not guess which settings module runs in production.

**Shapes it recognises but does not model**, each published as an `unsupported_entry_point_shape` problem on its owner, counted rather than silently missed, as Java does for its 17 shapes:
- signal receivers (`@receiver`, `.connect(...)`);
- task queues (Celery tasks and schedules);
- `admin.site.register`;
- template tags and filters;
- project-specific route helpers (Shuup's `admin_url(...)` in admin modules, 152 uses);
- `AppConfig.ready()` hooks;
- WSGI and ASGI application objects.

**String references by name.** Shuup wires most of its plugins through strings of the form `"package.module:Name"` inside a `provides` dictionary (35 files), and Satchmo names many views as dotted strings. Strings in the recognised slots above become edges. For any other string that is exactly a dotted name or `module:Name` and that resolves to a module in the scan, the proposal is an `import` edge with evidence class `inferred`. A logger name such as `"shuup.core"` would also match, so the spike measures how often such edges are wrong before this rule is accepted (question 1 in section 12).

**New entries in closed vocabularies:** language `python`; entry-point kind `cli_command` (a named command: a Django management command or a `console_scripts` entry); and the relations `include` and `configuration`, which Java lists as unsupported and Python would produce. Python's unsupported relations are `invoke` (calls) and `data` (reads and writes of stored data). `ENTRY_POINT_KINDS` needs meanings that are not Java-specific: `cli_main` today says "a main method".

## 5. Reading Python 2 without lib2to3

### The facts that shape it

- **The standard parser only reads Python 3.** agenttalk runs on Python 3.10 to 3.14, and its `ast` module rejects Python 2-only syntax.
- **lib2to3 is gone.** It was removed in Python 3.13.
- **No new runtime dependency.** agenttalk has none today (`dependencies = []`), and the comprehension worker bans network-capable imports. Third-party parsers (parso, tree-sitter) are therefore out for this design.
- **Measured on Satchmo's 492 Python files:**

  | Python | Parse with `ast` | Fail | Split into tokens without error |
  |---|---|---|---|
  | 3.10.11 | 445 | 47 | 492 |
  | 3.14.6 | 462 | 30 | 492 |

  Python 3's own tokenizer (`tokenize`) splits every one of the 492 files.
- **A count of Python 2-only syntax in the token stream** finds it in 49 files (10%):

  | Construct | Uses | Files |
  |---|---|---|
  | print statement | 102 | 20 |
  | `except X, e:` | 37 | 24 |
  | `raise X, message` | 20 | 7 |
  | `<>` | 6 | 5 |
  | the `L` suffix on numbers | 2 | 1 |
  | a tuple parameter in a lambda (no repair: step 4) | 1 | 1 |

  The same count finds none of them in Shuup's 1,638 files.
- **The files that fail matter.** On Python 3.14 they include a `models.py`, three view modules and seven of Satchmo's ten management commands.
- **"It parses" is not a test for Python 3.** Python 3.14 accepts `except X, e:` (PEP 758), but reads it as "catch X or e" with no variable. 17 Satchmo files fail on 3.10 and parse on 3.14, 15 of them for this reason alone. The other two also contain the Python 2 statement `print >>sys.stderr, '...'`, which every Python 3 version parses without complaint as a different expression (a shift inside a tuple). So the same file can read differently depending on the Python that runs the scan, and a file can parse on every Python 3 and still be Python 2.

### The approach

1. **Decide the dialect from evidence, not from whether the parse succeeds.** Two kinds of marker count as evidence:
   - **Python 2 only**, found in the token stream: print statements (including `print >>f, x`), `<>`, backticks, `exec` statements, `raise X, message`, the `L` suffix, old octal numbers such as `0777`, and `ur''` strings. No Python 3 accepts any of them with that meaning.
   - **Python 3 only**, found in the parse tree: f-strings, annotations, keyword-only and positional-only parameters, `async` and `await`, `nonlocal`, `yield from`, `:=` and `match`. No Python 2 accepts any of them.

   `except X, e:` is in neither list: Python 2 reads it as "catch X, call it e", Python 3.14 as "catch X or e". It always takes the project's dialect, which is decided by the first of these rules that applies:
   1. **Packaging:** `python_requires`, or Python-version classifiers, in `setup.py`, `setup.cfg` or `pyproject.toml`.
   2. **One kind of marker:** if the files carry markers of only one kind, that is the dialect.
   3. **Both kinds:** the project is mixed. A file with markers follows its own; any other file follows the kind found in more files; the run records a `dialect_mixed` problem.
   4. **No markers at all:** `except X, e:` is read as Python 2, with a `dialect_assumed` problem on each such file. Code written for the Python 3.14 form almost always carries Python 3 markers too, so a codebase with none is far more likely old Python 2. Imports in such a project are resolved both ways, and one that resolves only as a Python 2 sibling import is marked `ambiguous`.

   A Django version pin is not used: Django 1.11 runs on both Python 2 and 3. `from __future__` lines do not choose the dialect either; `absolute_import` only changes how imports resolve (below).

   **Measured:** Satchmo has no packaging evidence: its classifiers name no Python version, it has no `python_requires`, and no file has a `from __future__` line. Rule 2 decides it: 33 files carry Python 2 markers and none carries a Python 3 marker, so it is Python 2, and its 15 files whose only sign of Python 2 is `except X, e:` are read as Python 2. Shuup is decided by rule 1 (`python_requires=">=3.6"`), and its files agree: 96 carry Python 3 markers and none carries a Python 2 marker.
2. **Read Python 3 files with `ast`.** The reader is `python-ast`.
3. **Repair, then parse, for Python 2 files.** A fixed list of token-level rewrites runs first, each keeping its text on its original lines, so every line number in a claim still points at the right line:
   - print statements become calls;
   - `except X, e:` becomes `except X as e:`;
   - `raise X, message` becomes a call;
   - `<>` becomes `!=`;
   - the `L` suffix is dropped;
   - old octal numbers gain `0o`;
   - `ur''` becomes `r''`;
   - backticks become `repr(...)`;
   - `exec code in names` becomes a call.

   There is no repair for a tuple parameter (`lambda (k, v): ...`, or `def f(a, (b, c)):`). Undoing one needs new unpacking lines inside the body, which would move code to other lines, so such a file goes to step 4.

   Then `ast` parses the result. The reader is `python-ast-repaired`, and the file gets a problem record naming each kind of repair applied, so the change is visible. A Python 2 file with nothing to repair is read as `python-ast`; its dialect still decides how its imports resolve.
4. **Outline from tokens when a file still fails.** One Satchmo file uses a lambda with a tuple parameter (`satchmo/apps/product/tests.py`, line 199); newer syntax than the running Python also lands here. The reader takes what tokens alone can show: imports, class and function names with their nesting, and line numbers. The reader is `python-tokens`, and readiness reads the file as partly understood (section 6).
5. **Unreadable.** If even tokenising fails, the file gets an `unreadable` problem and degrades the run. On both practice codebases this does not happen.

**Source encodings.** The reader decodes a file the way Python does: a UTF-8 byte-order mark, or a coding declaration in the first two lines (PEP 263), and otherwise UTF-8. The standard library's `tokenize.detect_encoding` applies exactly this rule, and `ast.parse` follows it when given bytes. A Python 2 file without a declaration had to be ASCII, which UTF-8 reads unchanged. A file that names an unknown encoding, or whose bytes do not decode in the encoding found, is not read at all. It gets the reason code the Java reader already uses for this, `encoding_undecodable`, which degrades the run, and readiness treats it as a whole-file gap, as for Java. **Measured:** Satchmo has 34 files that declare UTF-8 and 458 that rely on the default; Shuup has 1,201 and 437. Neither has a byte-order mark, a file that fails to decode, or a file with non-ASCII bytes and no declaration.

**Python 2 import meaning.** In a Python 2 file inside a package, `import models` first means the sibling module `models`, unless the file has `from __future__ import absolute_import`. Satchmo has 97 such import lines and no `absolute_import`. The Python resolver therefore tries the sibling first in a Python 2 file, and records which rule resolved each edge.

**Which Python ran the scan becomes part of the record.** Even after the dialect rule, `ast` is the grammar of the Python that runs the worker: Python 3.10 cannot read syntax added later. DESIGN-55 already lets a producer record a "grammar/indexer version", so the Python reader records the interpreter's minor version there. Scans on 3.10 and 3.14 may then differ, and the record says why. The spike checks that on these two codebases they differ only in that field.

**Rejected:**
- a vendored copy of lib2to3, which we would maintain, and for code it does not need;
- parso or tree-sitter, which would be the first runtime dependency and a new review of the network-free worker;
- running a Python 2 interpreter, which is not installed on most machines and is a second program to trust.

## 6. Changes to discovery, the worker, the scan pipeline and the builders

**Discovery** (`discovery.py`):
- **More cache folders excluded:** `.eggs`, `*.egg-info`, `.nox`, `site-packages`, `.ruff_cache` and `htmlcov`. Today `__pycache__`, `.venv`, `venv`, `.tox`, `.mypy_cache` and `.pytest_cache` are already excluded.
- **Python counts as code when it is hidden.** A folder named `build`, `dist`, `vendor`, `out` or `target` is excluded today wherever it sits, unless it is under a Maven source root, and a quick look inside for code files counts `.java` and several other application languages, but not `.py`. With the Python reader on, `.py` joins that list, so a Python package hidden under such a folder is flagged like a Java one instead of disappearing silently.
- **The hand-kept copies of the worker's extension lists** become reads from the language rules registry.

**The worker** (`worker.py`):
- **A real registry.** The `.java`-only entry becomes the rules registry: `.py` goes to the Python reader, and Python's packaging files are recognised by name.
- **Encodings:** files are decoded as Python decodes them, and an undecodable file gets `encoding_undecodable` (section 5).
- **`requirements*.txt` gets read.** Today it counts as harmless text, so nothing is recorded about it at all; the Python rules read it.
- **The new wire format** (`FileClaims`, schema version 2).
- **Warnings stay quiet.** The worker's standard output carries its JSON result, so parser warnings must never reach it. Parsing Shuup on 3.14 prints `SyntaxWarning`s for invalid escape sequences; the reader turns warnings off around each parse.
- **Limits.** A `RecursionError` from a deeply nested file falls back to the token outline. The existing 8 MiB per-file cap and the 300-second worker limit stay. Parsing all 1,638 Shuup files took 1.4 seconds on 3.14.
- **`pyproject.toml` on Python 3.10.** It is read with `tomllib`, which exists only from Python 3.11, so on 3.10 that file gets a problem saying it was not read (question 4 in section 12).

**The scan pipeline** (`scan_pipeline.py`):
- **One entry per reader in `scan.json`.** `adapters` lists every reader that handled at least one file, instead of one hard-coded Java entry. The Python capability tables go in a new map keyed by reader, and the existing top-level fields keep their Java meaning, so no current reader of `scan.json` breaks (question 3).
- **Cross-file steps.** These run per language: Java's reactor rule as today, and Python's URL composition and choice of settings root.

**The builders:**
- **modules:** the language label, the test classification and the nesting of qualified names come from the rules.
- **dependencies:** the shared part (records, IDs, evidence, phases, duplicates, conflicts) stays. Target resolution moves behind each language's resolver. `include` and `configuration` edges are accepted from readers that declare them.
- **features:** the label of a candidate feature comes from the rules, and the reader identity from the claim instead of a hard-wired Java name.
- **readiness:** two changes.
  - The reason-code table is merged from all languages.
  - `source_understood` changes meaning. Today it is "satisfied" for any file whose language is not `unknown`. It must instead be satisfied only for files the reader read fully (`python-ast` or `python-ast-repaired`); a `python-tokens` file reads `unknown`, with the reason `partial_read`.

## 7. What stays Java-only

These move behind the Java rules object without any change in behaviour:
- the Java reader itself (`adapters/java.py`);
- the readers for `pom.xml` and `web.xml`, and the Maven reactor rule;
- the check whether a servlet or filter class can be created, which reads the type traits;
- Java's resolution ladder: static and wildcard imports, Java scope rules, `java.lang` names, binary `$` names, the reserved `java.`, `javax.` and `jakarta.` prefixes;
- the retraction of a `main` that uses a shadowed `String`;
- the Maven source-root exemptions in discovery;
- Java's tables of unsupported shapes and its route caveats.

Django templates (`.html` and Jinja files) are not read by either language. A `{% url %}` in a template is not an edge; that is a named gap.

## 8. What the spike will prove

**Shape.**
- **Its own branch,** `spike/comprehension-python`, never merged; it stops after two working days, whatever its state.
- **Read-only on the practice code.** It reads the two codebases from scratch copies and runs nothing of theirs, and never touches the live agenttalk store, gateway or ports.
- **No builder refactor.** It builds only:
  1. the claim types and the Java converter;
  2. a prototype Python reader;
  3. a throwaway script that resolves imports and composes routes across the files and writes a report.

**Questions and proposed pass marks** (the review may change the marks):

| # | Question | Pass mark |
|---|---|---|
| S1 | Can every file be read? | Satchmo: at least 97% of `.py` files read fully (`python-ast` or `python-ast-repaired`) on both 3.10 and 3.14, and none unreadable. Shuup: 100%. The claims for each codebase are identical on 3.10 and 3.14, except the field that records the interpreter's version |
| S2 | Do imports resolve? | Which imports point inside the scan is decided before the spike runs, without the resolver: an import whose first name is one of the project's own top-level packages (Satchmo: `satchmo` and the packages under `satchmo/apps` that its `setup.py` declares; Shuup: `shuup`), a relative import, or a Python 2 import of a name that exists as a sibling file or package folder. At least 90% of those resolve to a unit in the scan. A hand check of 20 random edges per codebase finds no wrong resolution. Satchmo's implicit relative imports resolve to the sibling. Every import the resolver places inside the scan that this list puts outside it is listed with the reason |
| S3 | Are routes found? | For five URL modules per codebase, the local patterns found equal a hand count. The report gives composed versus local-only routes, with the reason for each local-only chain |
| S4 | Does Java fit without loss? | Converting to `FileClaims` and back gives exactly the original, for every Java source the comprehension tests use |
| S5 | What does it cost? | Worker time and artifact sizes for both codebases, on 3.10 and 3.14 |
| S6 | How noisy are by-name string references? | Count, with a hand check of 20 per codebase; the result decides question 1 in section 12 |

**What gets attached:** a short report on the PR with the counts, the samples (as file and line pointers, not source text) and the timings. The scanned source itself is never committed.

**What stops the work:** if S1 or S4 fails, the base of this design is wrong. The spike reports that and stops, and nothing is built.

**If time runs out:** the questions are dropped in this order: S6, then S5, then S3, then S2. S1 and S4 are never dropped, because they decide whether to stop. A dropped question is reported as not answered, never as passed.

## 9. What it costs

**To run.** Parsing is cheap at these sizes: 1.4 seconds for Shuup's 1,638 files (6.3 MiB) on Python 3.14. Today's whole scan of Shuup, which reads but does not parse the Python files, took about 22 seconds. The spike measures the full cost with resolution and composition (S5).

**To build, after the spike** (estimates, to be replaced with the spike's evidence):

| Piece | Estimate |
|---|---|
| Claim interface and builder refactor, with Java artifacts unchanged | about 6 days |
| Python reader: `ast`, repairs, token outline, dialect rules, import resolution | about 5 days |
| Django and packaging recognition, URL composition | about 5 days |
| Discovery, worker, pipeline and readiness changes; capability tables | about 3 days |
| **Total build** | **about 19 days** |
| Review rounds | not estimated: #132, which built the scanner and the Java reader, went through about fifty, so plan for review time of the same order as the build |

## 10. What it commits us to

- **One interface for every future reader.** The claim interface is the contract every reader must meet from then on, including the tree-sitter Java replacement already planned.
- **Twice the review surface.** With two languages, every builder change has to be checked against both.
- **New entries in closed vocabularies.** The language `python`, the entry-point kind `cli_command`, the relations `include` and `configuration` now produced, new problem reason codes and new capability tables. Readers of the artifacts (reports and the planned migration UI, #208) must accept them. For each artifact the build decides whether that means schema version 2.
- **A repair list to maintain.** The Python 2 repair list is a fixed, tested table. Django's URL syntax has changed between versions 1.x and 4.x and will change again.
- **Results depend on the interpreter.** The Python version that runs the scan becomes part of the producer identity, so two scans of the same code on different Pythons can legitimately differ.
- **Reading settings files stays dangerous.** These files often hold secrets. The rule "read only code-reference slots" must hold for good, with its canary test.

## 11. What we learn even if it is never built

1. **Today's scanner misleads on Python.** A Python repository can scan "complete" with nothing understood (Satchmo), or "degraded" only because of an unrelated file (Shuup). Anyone who runs `comprehension` on Python code this week will see a result that looks finished. A small fix, independent of this design, is proposed for after this week as its own issue: a run where code files exist but no reader handled any of them must not publish "complete", and the README's "for supported inputs" should say "Java only".
2. **A map of the Java coupling.** It lists exactly where the builders depend on Java types. That is the first step of the planned tree-sitter Java replacement too.
3. **Python 2 facts for the practice week.** In Satchmo, 10% of files use Python 2-only syntax, and 97 import lines rely on implicit relative imports. Python 3.14 accepts `except X, e:` with a different meaning, so any tool of ours that uses "does it parse" to detect Python 2 is wrong on 3.14.
4. **Where a static inventory of these codebases is blind.** Satchmo adds URL patterns while it runs (`collect_urls`). Shuup wires its plugins by string (35 `provides` dictionaries) and builds its admin routes with its own helper (152 `admin_url` uses). The Python introduction can use these points whether or not this is built.

## 12. Open questions

1. **String references by name:** use every exact dotted string that resolves inside the scan (this catches Shuup's plugins), or only the recognised slots? *Decide after the spike's S6 count.*
2. **Django apps as units:** should a Django app become a container unit, which would be the first producer of `container_unit_id`, or should reports group files by app package? *Recommend grouping in reports first; containment is a separate change.*
3. **`scan.json`:** add a per-reader capability map and keep today's Java fields as they are, or move to schema version 2? *Recommend the additive map.*
4. **`pyproject.toml` on Python 3.10:** report it as not read, or write a minimal reader for the few keys needed? *Recommend reporting it as not read.*
5. **Cython (`.pyx`) and Django templates:** should they degrade a Python repository's run when no reader handles them, the way unsupported application code does in a Java repository?
6. **The spike's pass marks** in section 8.

## 13. Technical notes

Code read at master `233b496a`. Measurements were taken on 2026-10-14 with read-only scripts that never import or run the scanned code:
- **Satchmo:** the PyPI source package `Satchmo-0.9.2.tar.gz`.
- **Shuup:** a shallow clone of `github.com/shuup/shuup` at `25f78cfe`.
- **Interpreters:** Python 3.10.11 and 3.14.6. Python 3.13 was not available on that machine.
- **Dialect markers** were counted per file on Python 3.14: Python 2 markers from the token stream, Python 3 markers from the parse tree of the files that parse. Satchmo: 33 files with Python 2 markers (print statement 20, `raise X, message` 7, `<>` 5, `L` suffix 1), none with Python 3 markers. Shuup: none with Python 2 markers, 96 with Python 3 markers (annotations 87, f-strings 10, `yield from` 3, keyword-only parameters 1).
- **Encodings** were read with `tokenize.detect_encoding`.

**Where the Java coupling is:**
- **The result types.** `adapters/java.py` defines `JavaFileResult` (units, edges, entry points, problems, plus `declared_module_paths`, `descriptor_name_conflicts`, `web_servlet_declared_names`, `web_filter_declared_names` and `package`), `JavaUnitClaim`, `JavaEdgeClaim`, `JavaEntryPointClaim` and `JavaAdapterProblem`. Its worker wire format is `file_result_to_json` / `file_result_from_json`.
- **The builders' signatures.** `build_modules`, `build_dependencies` and `build_features` take `dict[str, java_adapter.JavaFileResult]`. `readiness_artifact.py` imports no Java type, but `_READINESS_CHECKS_BY_REASON_CODE` lists Java, Maven and `web.xml` codes. `source_understood` is satisfied whenever `unit.language != "unknown"`.
- **The worker.** `worker.py`: `_ADAPTER_EXTENSIONS = {".java": java_adapter}` and `_ADAPTER_HANDLED_XML_BASENAMES`; the dispatch calls `parse_java_source` and `parse_maven_pom` by name; `_DEGRADING_CODE_EXTENSIONS` leaves out `.py` on purpose (fix round 17b); `WORKER_SCHEMA_VERSION = 1`; the result field is `WorkerResult.java_results`.
- **The scan pipeline.** `scan_pipeline.py` converts with `java_adapter.file_result_from_json`, and writes `scan.json`'s `adapters` and capability fields from the Java reader's constants.
- **Discovery.** `discovery.py`: `_RECOGNIZED_SOURCE_ROOT_SEGMENT_RE` (Maven roots), `_GENERATED_VENDOR_DIR_NAMES`, `_DEGRADABLE_EXCLUDED_EXTENSIONS` and `_ADAPTER_HANDLED_EXTENSIONS_FOR_SECRET_CALIBRATION`.
- **The test-root pattern** is copied three times: `worker.py`, `modules_artifact.py` and `adapters/java.py`.
- **Unit kinds.** `modules_artifact.py` builds only `file` and `component` units; its docstring says no container unit is built yet.

**The baseline runs:** `python -m agenttalk --root <copy> comprehension scan --json`, with the worktree's `src` on `PYTHONPATH`, the seat's store variables unset, and `.agenttalk/` added to each copy's local git exclude file so the privacy check passes.
- Satchmo: `complete`, 1,139 units, 1,034 problems.
- Shuup: `degraded`, 3,004 units, 2,678 problems; the only degrading problem is on `shoop-to-shuup.sql`.

**Facts used for Python:** `sys.stdlib_module_names` exists from Python 3.10. `tomllib` exists from Python 3.11. lib2to3 was removed in Python 3.13. PEP 758 (Python 3.14) allows exception lists without parentheses.

**Pattern counts** (regular-expression counts over the files, a first estimate rather than parsed facts):

| Pattern | Satchmo | Shuup |
|---|---|---|
| URL modules | 34 | 15 |
| `patterns('prefix', ...)` | 36 | — |
| `url(...)` | 46 | 98 |
| `path(...)` | — | 4 |
| `include(...)` | 17 | 11 |
| `.as_view()` | — | 454 |
| management commands | 10 | 11 |
| direct `models.Model` subclasses | 92 | 46 |
| settings modules | 2 | 24 |
| `apps.py` files | — | 13 |
| `provides` dictionaries | — | 35 |
| `admin_url(...)` | — | 152 |
| `import_module` / `__import__` | 1 / 7 | 4 / 0 |
| other load-by-name helpers | 17 | 51 |
| signal `.connect(...)` | 49 | 65 |
