# tools/: how this repo was built

Nothing here is needed to run a digest. This directory is a build showcase:
the loop that let a coding agent build curated-digest unattended across many
rate-limited sessions, kept because it works and because the product's own
harness (docs/HARNESS.md) was designed and proven on it.

| File | What it does |
|---|---|
| `PLAYBOOK.md` | The loop, its guardrails, and the lessons it produced. Start here. |
| `build-loop/curated-digest-build` | One headless build turn: take the first unchecked item in `NEXT.md`, do it, run `make check`, commit, tick the box. |
| `build-loop/curated-digest-claude` | Runs a headless `claude -p` turn as a harness job and reports outcome, cost and rate limits as JSON. |
| `build-loop/queue_next.py` | `start` / `stop` / `status` the loop; `tick` queues the next item when the loop is on. |
| `build-loop/statusline-sidecar` | Claude Code statusline hook that saves the subscription's reset time where the harness reads it. Optional. |
| `build-loop/house-style.md` | Distilled rules for the slim-prompt variant (see PLAYBOOK, "full vs slim prompt"). |
| `build-loop/NEXT.md` | The queue: one checkbox per unit of work. |
| `build-loop/tests/` | Behaviour tests for the queue feeder; run by `make check`. |

## Running it on your fork

Needs the Claude Code CLI signed in (`claude setup-token` for unattended use)
and a PreToolUse path-confinement hook of your own; the runner sets
`CLAUDE_CONFINE_ROOTS` for it, and nothing here enforces confinement without it.

```bash
bin/curated-digest define build "tools/build-loop/curated-digest-build" \
  --kind build --cwd . --reset-repo
tools/build-loop/queue_next.py start
# Add to the tick unit, before `curated-digest run`:
#   ExecStart=%h/curated-digest/tools/build-loop/queue_next.py tick
bin/curated-digest status
bin/curated-digest pause        # give the usage window back to an interactive session
tools/build-loop/queue_next.py stop
```
