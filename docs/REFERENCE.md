# Reference

## Pipeline stages

| Stage | What it does |
|---|---|
| Source | RSS/Atom (stdlib parser) and Reddit (OAuth). Emits one normalized item shape. |
| Normalize, dedup, store | SQLite. Stable id (feed GUID, else content hash). An item reported once is never reported again. Items kept 180 days. |
| Prefilter | Deterministic rules (age, keywords). Only new candidates reach a model. |
| Curate | One model call judges candidates against the task's criteria, assigns `act` or `fyi`, and writes the one-line bullet. |
| Render | Telegram HTML, one phone screen, each bullet linked to its item. |
| Deliver | Telegram send with confirmed success. |

Tasks are YAML files in your config home. Adding one needs no code change and no restart. Each declares sources, criteria, schedule, a required `provider:model`, renderer, channel and urgency (`digest`, `prompt`, `urgent`). Three templates ship in `templates/tasks/`: `changelog-watch`, `multi-subreddit` and `rss-generic`. Their criteria are placeholders to rewrite.

There is no heartbeat message. Liveness comes from an external watchdog that a clean run pings and an unhealthy one does not. An empty run is normal: `after_prefilter: 0` means every unreported item was older than the task's `max_age_days`.

## Safety controls

- The curation model has no tools, filesystem or network (R20). Gemini is a plain HTTPS call. The Claude adapter runs with an empty allow-list, a deny list and no MCP.
- Every item is wrapped in `<untrusted_item>` tags it cannot close. An injection attempt is reported as a finding (R29, R30).
- Model output is parsed strictly, and any item id it was not sent is discarded (R31).
- State advances only after confirmed delivery, and only for items the renderer showed (R14).
- 404s, parse failures, timeouts and a feed stuck on HTTP 304 are reported (R36, D30).
- The runner scrubs its environment. Credentials live in 0600 files outside the repo (R32, R33). A path-confinement hook can be armed with `CLAUDE_CONFINE_ROOTS` (you supply the hook).

## Commands

```bash
bin/curated-digest task list                  # schedules, models, what is due
bin/curated-digest task run <id> --dry-run    # real output, no state written
bin/curated-digest task run <id> --model gemini:gemini-3.7-flash   # try another model once
bin/curated-digest status                     # harness counts and anything needing attention
bin/curated-digest health                     # self-check, pings the watchdog if clean
bin/curated-digest pause                      # stop new runs without killing work in flight
```

## Configuration

The config home is `$CURATED_DIGEST_HOME`, else `$XDG_CONFIG_HOME/curated-digest`, else `~/.config/curated-digest`. `bin/curated-digest init` creates it (0700) and every file below, and never overwrites.

| File | Holds | Format |
|---|---|---|
| `tasks/*.yaml` | Your tasks | YAML: `id`, `source` (one `{type, url}` or a list of `{type, url, id}`), `criteria`, `schedule`, `model` (`provider:model`), `renderer`, `channel`, `urgency`, `heading`. Optional `link`, `prefilter` (`max_age_days`, `include`, `exclude`) |
| `telegram.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `KEY=value`, mode 0600 |
| `gemini.env` | `GEMINI_API_KEY` (for `gemini:` models) | `KEY=value`, mode 0600 |
| `reddit.env` | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | `KEY=value`, mode 0600 |
| `curated-digest.env` | `CURATED_DIGEST_HC_PING_URL` (watchdog) | `KEY=value`, mode 0600 |
| `voice.md` | The voice the digest is written in | Plain text. Defaults to `prompts/voice.md` |

State (ledger, item store, pause flag) lives in `$CURATED_DIGEST_STATE_DIR`, else `$XDG_STATE_HOME/curated-digest`, else `~/.local/state/curated-digest`. Back it up with `bin/backup-state.sh`, which uses SQLite's backup API.

Path overrides and defaults are in [.env.example](../.env.example). Rate-limit signatures, retry caps and the $10 per-run spend ceiling are in `config/harness.yaml`. `.identifiers.local` (repo root, git-ignored) lists terms that must never be published. `make check` and the pre-push hook (`make hooks`) fail on any match. `bin/curated-digest check` validates the configuration and says how to fix each problem.

## Extending

- **A source:** a module in `curated_digest/sources/` that returns normalized `Item`s and raises `SourceError` on failure, never an empty list (R36). Register its `type` in `dispatch.SOURCE_TYPES` and in the allowed types in `task.py`. Anything a judgment needs is computed here, not by the model (R19a).
- **A provider:** one adapter in `curated_digest/providers/` with `complete(prompt, model) -> Completion`, registered by name. It must have no tool layer, or a locked-down one (R20, D28).
- **A channel or renderer** (`dispatch.CHANNELS` / `RENDERERS`): channels send and return confirmed success only (R14). Renderers turn judgments into an artifact and report which item ids they showed (R28).

Add a fixture test for every new parser and an injection case for every new path model text can take (R47, R48).
