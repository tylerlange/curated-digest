# curated-digest

## What it is

**Problem.** Following many feeds means skimming noise for the few items that matter.

**Solution.** Describe what matters in plain language. On a schedule, an AI model judges new items and sends a short Telegram digest, or nothing.

**Features.**
- Watches RSS/Atom feeds and subreddits.
- One YAML task per watch, each with its own criteria and schedule.
- One-screen digest, each line linked and marked act or FYI.
- Never repeats an item.

## Architecture

```
sources -> dedup -> prefilter -> curate (model) -> render -> deliver
```

- **Prefilter.** Rules first. Only new items reach the model.
- **Curate.** The model has no tools, files or network. Its output is parsed strictly.
- **Deliver.** Items are marked sent only after Telegram confirms.
- **Failures.** Broken sources raise errors. A watchdog alerts if runs stop.
- **Harness.** A work ledger survives restarts and rate limits.

Details: [REQUIREMENTS](docs/REQUIREMENTS.md), [DECISIONS](docs/DECISIONS.md), [HARNESS](docs/HARNESS.md), [REFERENCE](docs/REFERENCE.md).

## Install

Python 3.10+ and PyYAML.

```bash
# "Use this template" on GitHub, then:
git clone https://github.com/<you>/<your-copy>.git && cd <your-copy>
bin/curated-digest demo                     # offline, no keys

bin/curated-digest init --task changelog-watch
$EDITOR ~/.config/curated-digest/telegram.env ~/.config/curated-digest/gemini.env
$EDITOR ~/.config/curated-digest/tasks/changelog-watch.yaml   # replace REWRITE: lines
bin/curated-digest check
bin/curated-digest task run changelog-watch --dry-run         # nothing sent
```

To schedule: copy `systemd/` to `~/.config/systemd/user/` and enable `curated-digest-tick.timer`. Needs a Gemini key or the `claude` CLI, plus a Telegram bot. Gemini is only mock-tested, so dry-run first.

Agent-assisted setup: [AGENT-SETUP](docs/AGENT-SETUP.md).

## About

Built by Tyler Lange directing Claude Code ([build loop](tools/PLAYBOOK.md)), gated by tests on every commit. MIT license.
