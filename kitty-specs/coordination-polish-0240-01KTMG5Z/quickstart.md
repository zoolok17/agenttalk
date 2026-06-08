# Quickstart / Manual Validation: agenttalk 0.24.0 — Coordination Polish

Run from a scratch dir. `pip install -e .` first (tests/CLI run against the installed
copy, not src/).

```bash
agenttalk init
agenttalk roster add lead-agent
agenttalk roster add dev-agent
agenttalk roster add rev-agent
```

## 1. Escalation lead-fallback (FR-001..003)

```bash
# No liaison, no lead yet → escalate must exit 2 with remediation naming BOTH fixes
agenttalk escalate --from rev-agent -m "need a human ruling" ; echo "exit=$?"   # expect 2

# Designate a lead → escalate now routes to the lead with a fallback notice
agenttalk roster set-role lead-agent lead
agenttalk escalate --from rev-agent -m "need a human ruling"                     # routes to lead-agent
                                                                                 # prints request_id=esc-…
```

## 2. At-most-one-lead invariant (FR-004..008)

```bash
agenttalk roster                                  # lead-agent role=lead
agenttalk roster set-role dev-agent lead          # expect: "demoted lead-agent, promoted dev-agent"
agenttalk roster                                  # exactly one lead: dev-agent
agenttalk roster set-role dev-agent lead          # idempotent: no "demoted" line
agenttalk roster set-role dev-agent reviewer      # zero leads now — allowed
```

## 3. doctor no-target nudge (FR-009)

```bash
# With zero leads and no liaison on a multi-agent roster:
agenttalk doctor                                  # expect a WARNING: escalation has nowhere to go,
                                                  #   naming set-operator-facing AND set-role … lead
agenttalk roster set-operator-facing lead-agent
agenttalk doctor                                  # warning gone (liaison resolves)
```

## 4. wake correlation id (FR-010..011)

```bash
agenttalk send --from lead-agent --to dev-agent --kind wake -m "resume"  # prints request_id=wk-…
agenttalk threads --for dev-agent                                        # wake creates NO owed/open row
# explicit id is honored:
agenttalk send --from lead-agent --to dev-agent --kind wake \
  --meta request_id=mine-123 -m "resume"                                 # id stays mine-123
```

## 5. Owed-inbound pre-send warning (FR-012..014)

```bash
# dev-agent proposes to lead-agent; lead-agent then sends unrelated traffic back
agenttalk propose --from dev-agent --to lead-agent -m "option a vs b"    # mints pp-…
agenttalk send --from lead-agent --to dev-agent --kind note -m "ping"    # SOFT warning: you owe dev-agent
                                                                         #   an open proposal pp-… ; send still OK
# replying on the same id does NOT warn:
agenttalk reply --to-request pp-XXXX --from lead-agent --kind proposal-response \
  --meta status=accepted -m "go with a"                                  # no warning
```

## Acceptance

- All five sections behave as described.
- `python -m pytest -q` green; `ruff check src tests` clean.
- `python -m agenttalk --version` → `agenttalk 0.24.0` (after WP-D).
