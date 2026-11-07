# Harness requirements

Built first (D23). Small by design: a state machine
over one table plus a CLI. If this grows past a few hundred lines, something has
gone wrong.

## Purpose

Long work (building the project, and digest tasks on a subscription provider) spans
several of a subscription plan's rolling usage windows. The harness makes work
**resumable**, so hitting a limit costs time but never progress.

**The point is going headless.** Agree a plan, walk away. It builds until the
limit stops it, waits for the window to reopen, and picks itself back up with no
human involved. Nobody sets a reminder or types "resume." The only
time it asks for a human is when it is genuinely stuck (H14) — never merely
waiting.

## Unit of work

- **H1.** A unit is one `(job_id, run_id)` pair — one invocation of one job.
  Never subdivided; there is no partial credit inside a run.
- **H2.** Every unit exists as a ledger row *before* execution:
  `job_id, run_id, status, created_at, started_at, completed_at, attempts, last_error`.
  `status ∈ pending | running | done | failed | rate_limited | stuck`.

## Completion

- **H3.** A run is `done` only when its terminal side effect is **observed to
  succeed** — not when the process exits 0. The ledger write and the side effect
  are ordered so a crash between them leaves the run `running`, never a false `done`.
- **H4.** On startup, a row left `running` by a dead process is treated as
  **unknown**, not failed: re-verify the side effect, and requeue as `pending` if
  it can't be confirmed.
- **H5.** Git is the atomicity boundary for build work: a unit counts as complete
  only when committed. Every run starts by discarding uncommitted changes.

## Failure

- **H6.** A run that raises, times out, or is killed becomes `failed` with
  `last_error` recorded. Retries cap at **3**; after that it stays failed and is
  surfaced, never silently dropped.
- **H7.** One failing unit must not corrupt or block the ledger for any other.

## Rate limits

- **H8.** A rate limit is distinguished from a real failure by an enumerated
  signature — the exact strings live in config, because "detect a rate limit" is
  unfalsifiable without them. Known forms: `You've hit your session limit · resets
  <time>`, `You've hit your weekly limit`, `You've hit your Opus limit`, and the
  legacy `Claude usage limit reached. Your limit will reset at <time>`. API errors
  (`API Error:`, `429`, `529`) are a *different* class and do not count.
- **H9.** On rate limit the unit becomes `rate_limited` — **not** `failed` — and
  does **not** consume a retry.
- **H10.** Waiting is not a loop inside one process. Record when the window
  reopens and exit; the next scheduled tick re-checks the ledger. Reset time comes
  from the statusline sidecar (`rate_limits.five_hour.resets_at`, Unix epoch
  seconds, written by an optional sidecar in tools/build-loop/) when fresh, else
  parsed from the error text, else a conservative fixed interval.
- **H11.** Weekly limits are handled distinctly from short-window limits — a week-long
  wait must notify, not silently stall.

## Resume

- **H12.** On start, resume `pending` and `rate_limited` units before accepting new
  work, oldest first.
- **H13.** **Resume is automatic.** A scheduled tick reclaims waiting work with no
  human action. An explicit command exists for convenience, never as a requirement.
  A tick with nothing to do makes no API call and costs nothing.
- **H13a.** Waiting is never surfaced as something to act on. The operator is notified on
  completion or on `stuck` — not when the harness is simply between windows.

## Stopping

- **H14.** A unit at its retry ceiling, or `rate_limited` for more than **24h**,
  becomes `stuck` and is excluded from auto-resume until a human requeues it.
- **H15.** The runner never busy-loops. Each check-then-skip is one bounded action.

## Yielding the quota back

- **H16.** A single command pauses the runner without editing code or units. It
  checks a pause flag before claiming any work. *(This is where R42c is actually
  implemented.)*
- **H17.** Pause stops new claims; it does not kill work in flight.
- **H18.** A status command shows counts by state, so it's clear what's queued
  before reclaiming the window.
- **H19.** A hard spend ceiling of **$10 per run** applies.
  *Note:* on a subscription with no usage credits enabled, the usage window,
  not dollars, is the real bound. The ceiling exists because credits may be on
  (promotional or otherwise) and could otherwise be consumed silently by an
  unattended loop.

## Proving it works

- **H20.** One command proves the harness by **behaviour**, not configuration:
  kill a unit mid-flight → restart → it resumes and does **not** double-execute
  its side effect; inject a synthetic rate-limit response → the unit yields
  without burning a retry; set the pause flag → no work is claimed.

## Implementation choices (not requirements)

SQLite for the ledger (durable, queryable, one file). One row per unit. CLI verbs
`run`, `status`, `pause`, `resume`, `requeue`. These are implementation choices;
they don't need review.

Confirmed defaults: 3 retries, 24h stuck timeout, $10 per run.
