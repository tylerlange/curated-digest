# curated-digest

A scheduled pipeline that watches feeds, has a model judge what is worth your attention against criteria you write, and sends a short Telegram digest, or nothing when nothing qualifies.
It treats every fetched item and every model reply as hostile, and it cannot fail quietly.

## Quickstart

About ten minutes. Python 3.10+ and PyYAML; nothing else, nothing to install.

```bash
# 1. Get a copy. Use GitHub's "Use this template" button rather than Fork
#    (see "Starting your own copy" below), then:
git clone https://github.com/<you>/<your-copy>.git && cd <your-copy>
python3 -c "import yaml" || echo "install PyYAML (e.g. apt install python3-yaml)"

# 2. Demo: the whole pipeline once, offline. Fictional feed, stub judge, no keys.
bin/curated-digest demo

# 3. Your instance, outside the repo (~/.config/curated-digest by default).
bin/curated-digest init --task changelog-watch
$EDITOR ~/.config/curated-digest/telegram.env ~/.config/curated-digest/gemini.env
$EDITOR ~/.config/curated-digest/tasks/changelog-watch.yaml   # rewrite every REWRITE: line
bin/curated-digest check                                      # says how to fix anything wrong

# 4. First real run: real fetch, real model, nothing delivered, no state written.
bin/curated-digest task run changelog-watch --dry-run
```

An empty first run is often correct. The summary says why: `after_prefilter: 0`
means every unreported item was older than the task's `max_age_days`.

Then schedule it: copy `systemd/` to `~/.config/systemd/user/`, adjust the
paths, `systemctl --user enable --now curated-digest-tick.timer`. The demo
needs no key; a real run needs a model (a Gemini key, or the `claude` CLI
signed in) and, to deliver, a Telegram bot. `docs/AGENT-SETUP.md` walks a
coding agent through setting up an instance for you.

## Concepts

```
sources -> normalize -> dedup/store -> curate (LLM) -> render -> deliver
```

| Stage | What it does |
|---|---|
| Source | RSS/Atom (stdlib parser) and Reddit (OAuth). Emits one normalized item shape. |
| Normalize, dedup, store | SQLite. Stable id (feed GUID, else content hash). An item reported once is never reported again. Items kept 180 days. |
| Prefilter | Deterministic rules first (age, keywords). Only new candidates reach a model. |
| Curate | One model call judges candidates against the task's criteria, assigns `act` or `fyi`, writes the one-line bullet. A bundled item can yield several findings. |
| Render | Telegram HTML, one phone screen, each bullet linked to its item. |
| Deliver | Telegram send with confirmed success. |

**Tasks** are YAML files in your config home. Adding one needs no code change
and no restart. Each declares sources, criteria, schedule (`every 6h`,
`daily at 07:00`), a mandatory `provider:model`, renderer, channel and urgency
(`digest`, `prompt`, `urgent`). Three templates ship in `templates/tasks/`:
`changelog-watch`, `multi-subreddit` and `rss-generic`. Their criteria are
placeholders; the criteria are the product, so rewrite them.

**Silence is the normal outcome.** No heartbeat, no filler. Liveness comes from
an external dead-man's switch that a clean tick pings and an unhealthy one
deliberately does not.

**The harness** is a resumable work ledger. Every task run is a unit that is
written before it executes, counts as done only when its side effect is
observed, yields on a rate limit without burning a retry, and resumes on the
next 10-minute tick. `curated-digest pause` stops new claims without killing
work in flight. Spec: [docs/HARNESS.md](docs/HARNESS.md).

**Safety design.** Fetched content is the only untrusted input, and it flows
into a model, so the design assumes prompt-level defences fail:

- The curation model has no tools, filesystem or network (R20). Gemini is a plain
  HTTPS call; the Claude adapter runs with an empty allow-list, a named deny
  list and no MCP. An injected instruction reaches something that cannot act.
- Every item is wrapped in `<untrusted_item>` tags it cannot close; an attempted
  injection is reported to the reader as a finding, not followed (R29, R30).
- Model output is untrusted: parsed strictly, and an item id it was never sent
  is discarded (R31).
- State advances only after confirmed delivery (R14). Only items the renderer
  actually showed are marked reported.
- A broken source never looks quiet: 404s, parse failures, timeouts and a feed
  stuck on HTTP 304 against its own cadence are reported (R36, D30).
- The runner scrubs its environment, credentials live in 0600 files outside the
  repo (R32, R33), and a path-confinement hook can be armed via
  `CLAUDE_CONFINE_ROOTS` (the hook itself is yours to supply).

Numbered requirements: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md). Reasoning:
[docs/DECISIONS.md](docs/DECISIONS.md).

```bash
bin/curated-digest task list                  # schedules, models, what is due
bin/curated-digest task run <id> --dry-run    # real output, no state written
bin/curated-digest task run <id> --model gemini:gemini-3.7-flash   # try another model once
bin/curated-digest status                     # harness counts; what needs you
bin/curated-digest health                     # checks itself; pings the watchdog if clean
```

## Configuration reference

Everything an operator supplies lives in the **config home**:
`$CURATED_DIGEST_HOME`, else `$XDG_CONFIG_HOME/curated-digest`, else
`~/.config/curated-digest`. `bin/curated-digest init` creates it (0700) and
every file below; it never overwrites.

| File | Holds | Format |
|---|---|---|
| `tasks/*.yaml` | Your tasks | YAML: `id`, `source` (one `{type, url}` or a list of `{type, url, id}`), `criteria`, `schedule`, `model` (`provider:model`), `renderer`, `channel`, `urgency`, `heading`; optional `link`, `prefilter` (`max_age_days`, `include`, `exclude`) |
| `telegram.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `KEY=value`, mode 0600 |
| `gemini.env` | `GEMINI_API_KEY` (for `gemini:` models) | `KEY=value`, mode 0600 |
| `reddit.env` | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | `KEY=value`, mode 0600 |
| `curated-digest.env` | `CURATED_DIGEST_HC_PING_URL` (watchdog) | `KEY=value`, mode 0600 |
| `voice.md` | The editorial voice the curator writes in | Plain text; defaults to `prompts/voice.md` |

State (ledger, item store, pause flag) lives in `$CURATED_DIGEST_STATE_DIR`,
else `$XDG_STATE_HOME/curated-digest`, else `~/.local/state/curated-digest`.
Back it up with `bin/backup-state.sh` (uses SQLite's backup API; never tar a
live WAL database). Keep it on a volume with headroom.

Every path override, with defaults, is in [.env.example](.env.example). Rate-limit
signatures, retry caps and the $10 per-run spend ceiling are in
`config/harness.yaml`. `.identifiers.local` (repo root, git-ignored): one term
per line that must never be published; `make check` and the pre-push hook
(`make hooks`) fail on any match.

`bin/curated-digest check` validates all of the above (missing or world-readable
credential files, placeholder values, broken tasks, leftover `REWRITE:` lines,
no watchdog) and says how to fix each. Task verbs refuse to run without a
tasks directory.

## Extending

- **A source:** a module in `curated_digest/sources/` that returns normalized
  `Item`s and raises `SourceError` on any failure, never an empty list (R36).
  Register its `type` in `dispatch.SOURCE_TYPES` and in the allowed types in
  `task.py`. Enrichment a judgment needs is computed
  here, not by the model (R19a).
- **A provider:** one adapter in `curated_digest/providers/` with
  `complete(prompt, model) -> Completion`, registered by name. It must have no
  tool layer, or a locked-down one (R20, D28).
- **A channel or renderer** (registered in `dispatch.CHANNELS` / `RENDERERS`): `channels/` sends an artifact and returns
  confirmed success only (R14); `renderers/` turns judgments into an artifact
  and reports which item ids it showed. Formatting lives only in renderers (R28).

Add a fixture test for every new parser and an injection case for every new
path model text can take (R47, R48).

## Status

Reference implementation. Fork it and make it yours. Issues and PRs may not get a response.

## How this was built

Tyler Lange owned the requirements, the decisions, the evaluations and the
safety constraints: the pipeline shape, the rule that curation gets no tools,
the rule that state moves only after confirmed delivery, the spend ceiling, and
testing behaviour instead of configuration. Claude Code wrote most of the code
and most of the tests, through the unattended build loop in `tools/` with the
test suite and `verify.sh` as the gate on every commit. To judge it, read `tests/`, `verify.sh`, the ADRs, and
`tools/PLAYBOOK.md` for how the loop worked and what it got wrong.

Provider status: the Claude provider is verified live; the Gemini provider is
covered by mocked tests, so run one `--dry-run` with your own key before
scheduling it.

## Starting your own copy

Use GitHub's **Use this template** button rather than Fork. A template copy
starts with one clean commit and no link back here; a fork carries this repo's
history and is meant for sending changes upstream.

## License

MIT. See [LICENSE](LICENSE).
