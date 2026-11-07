# Build playbook

How curated-digest was built: one coding agent, unattended, one checklist item
per turn, across many rate-limited sessions. A human set the requirements,
decisions and evals, and reviewed the results; the loop did the typing.

## The loop

> take the first unchecked item in `NEXT.md` -> implement it with a test ->
> `make check` -> commit with the box ticked -> stop.

The harness (docs/HARNESS.md) owns everything around that sentence. A systemd
timer ticks every 10 minutes; `queue_next.py tick` submits one `build` unit
when the loop is on and the queue has work; `curated-digest run` claims it and
runs `curated-digest-build`. A unit counts as done only when a new commit
exists and the tree is clean (H5). Rate limits yield and resume on a later
tick without burning a retry (H9, H13). Nobody types "continue".

Run one iteration by hand first and read the diff before letting it go unattended:
`bin/curated-digest submit build && bin/curated-digest run`.

## Guardrails (so it always halts)

1. **One item per turn.** The prompt forbids starting the next item; the
   harness gives another turn. Half-done work is worse than none, so a unit
   that cannot pass `make check` reverts and writes the blocker under the item.
2. **Commit is the only proof of progress.** `--reset-repo` discards uncommitted
   changes before each run, so a crashed turn cannot leave a half-edit the next
   turn builds on.
3. **Bounded failure.** Three attempts, or 24 hours rate-limited, then `stuck`
   and it waits for a human (H6, H14). An empty queue exits 0, so a finished
   build does not burn retries.
4. **Spend ceiling.** $10 per run (H19); `curated-digest-claude` reports an
   overrun instead of continuing silently.
5. **Confinement.** Build turns get tools (unlike curation), bounded by
   `CLAUDE_CONFINE_ROOTS` plus a PreToolUse hook, not by the model agreeing.
6. **Pausable.** `curated-digest pause` hands the usage window back to an
   interactive session without killing work in flight (H16, H17).
7. **Decisions are not relitigated.** An item that conflicts with
   docs/DECISIONS.md stays unchecked with a note.

## Who did what

| Work | Where it ran |
|---|---|
| Requirements, decisions, review synthesis | The human, with the strongest model as orchestrator. Never delegated. |
| A specced, bounded unit plus its tests | A mid-tier model, through this loop |
| Running tests, lint, behaviour checks | The cheapest model |
| Search and file discovery | The cheapest model |
| Research needing judgment | A mid-tier model, one agent unless lanes are truly independent |

Subagents spend from the same quota as the orchestrator. What they save is
context compounding, not tokens, so fan out narrowly.

Build order was harness first (so every later unit was resumable), then a
behaviour-asserting verify script, then RSS, curation, Telegram delivery,
reliability checks, and the injection regression fixtures.

## Lessons

Format: trigger, lesson, status (graduated to where, or accepted).

### L1. Full system prompt beats the slim one (was D31)
- **Trigger:** a measured A/B: three identical work items per arm from the same
  commit, same model, isolated worktrees, blind review. Full: $8.44, 0
  correctness bugs. Slim (house-style.md only): $7.35, 2 bugs, both silent
  failures in the areas this project treats as sacred (an injection warning
  that overcounted once one item could yield several findings, and a retention
  sweep that inferred readers from history instead of current task files).
  Neither was caught by the tests that arm wrote.
- **Lesson:** the measurement overturned both going-in assumptions. Slim was expected to be ~10x
  cheaper; it was 13% cheaper, because ~28k cache-creation tokens are small
  next to a build turn's real work. It was expected to fail on host
  knowledge; it failed on correctness reasoning instead. n=3 per arm is
  suggestive, not significant, but 13% is too small a prize for any extra
  risk of a silent data-loss bug.
- **Status:** graduated: full prompt is the default; `CURATED_DIGEST_BUILD_SLIM=1`
  stays so the measurement can be repeated rather than re-argued. Revisit if
  build turns dominate spend or context cost changes by an order of magnitude.

### L2. `--allowedTools` is variadic
- **Trigger:** a prompt passed positionally after the flag was swallowed as a tool name.
- **Lesson:** pass prompts on stdin, always.
- **Status:** graduated: both `curated-digest-claude` and the Claude provider use stdin.

### L3. Rate limits have no distinct exit code
- **Trigger:** a limit and a crash both exited non-zero.
- **Lesson:** classify by enumerated text signatures kept in config (H8); an
  unrecognized non-zero exit is retry-later, never a hard failure.
- **Status:** graduated: `config/harness.yaml`, `curated_digest/ratelimit.py`.

### L4. Checks that test configuration let failures ship
- **Trigger:** an earlier prototype's checks all asserted that files and
  settings existed; three failures shipped looking correct. Later, a file moved
  between repos silently disabled a scheduled job for a day.
- **Lesson:** every check provokes the behaviour and asserts the response;
  anything referencing a path across repos needs a behavioural check.
- **Status:** graduated: `verify.sh`, R45, R49.

### L5. Delegation saves context, not quota (was D11)
- **Trigger:** fanning out subagents did not stretch the usage window.
- **Lesson:** subagents draw on the same pool; use them to keep the
  orchestrator's context small, one agent unless lanes are independent.
- **Status:** accepted practice (table above).

### L6. Spec tooling deferred (was D10)
- **Trigger:** a spec framework fit well in principle (specs survive session
  boundaries; a spec is easier for a non-engineer to review than a diff).
- **Lesson:** skipped to avoid ceremony before the first thing shipped.
- **Status:** accepted. Revisit if plan drift across sessions becomes a problem.

### L7. Keep state off nearly-full disks
- **Trigger:** state on a volume near capacity.
- **Lesson:** point `CURATED_DIGEST_STATE_DIR` at a volume with headroom.
- **Status:** accepted; documented in the configuration reference.
