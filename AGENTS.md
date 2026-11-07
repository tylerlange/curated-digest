# AGENTS.md

Rules for an agent changing this code (humans: README.md).

## Command

- Run `make check` before you call any change done (`scripts/check.sh` where
  make is absent). It runs the unit suite, `verify.sh` behaviour checks, the
  build-tooling tests and the identifier scan. Network is mocked or fixtured.
- Tests use `unittest`, not pytest. Stdlib only plus PyYAML: do not add a
  dependency; the design goal is a host with no package installs (D27).
- `verify.sh` SKIPs live-host checks when their setup is absent. A SKIP is not
  a pass for a change to those areas; say what was skipped.

## Invariants (each exists for a reason; keep it)

- State advances only after confirmed delivery (R14). `mark_reported` runs only
  after `send()` returned success, and only for the ids the renderer showed.
  Anything else loses findings silently.
- Sending nothing is a valid outcome (D7, R26). Never add a heartbeat, a
  "nothing today" message, or filler to make a run look alive.
- Fetched content is untrusted (R29, R30): it reaches a model only inside
  `<untrusted_item>` tags it cannot close. An injection attempt is a finding.
- Model output is untrusted (R31): parse it strictly; drop any item id that was
  not sent. Never execute, fetch or route on model text.
- Curation has no tools, filesystem or network (R20). Do not give a provider a
  tool layer; anything a judgment needs is computed before curation (R19a).
- A broken source must never look empty (R36): raise `SourceError`, do not
  return `[]` on failure.
- Urgent alerts and `health` never depend on a model (R42b).
- The runner scrubs the job environment (R32). Do not pass secrets through.
- `model` is mandatory per task; there is no default provider (R8).
- Dry runs write no state (R46); keep them inside the rolled-back transaction.
- Code comments cite R/H/D ids. Keep ids stable; check docs/ before
  contradicting a decision, and record a new decision rather than editing one.

## Never

- Never commit instance data: tasks, `*.env`, state, ledgers, or
  `.identifiers.local`. Instance files live in `~/.config/curated-digest`.
- Never add a real person's details, accounts, hosts or schedules to code,
  tests, templates or docs. Fixtures and templates stay fictional.
- Never push without the scan passing: `python3 scripts/scan_identifiers.py`
  (also run by `make check` and the pre-push hook from `make hooks`).
- Never put a secret in code, a fixture, a log line or `.env.example`.
- REVIEW: Never change `tools/` to make product tests pass; it is build
  tooling, not product (tools/README.md).

Requirements: docs/REQUIREMENTS.md. Decisions: docs/DECISIONS.md. Harness: docs/HARNESS.md.
