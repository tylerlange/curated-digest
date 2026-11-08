# curated-digest

## What it is

**Problem.** Keeping up with many feeds (release notes, subreddits, blogs) means skimming a lot of noise for the few items that matter to you.

**How this solves it.** You write what matters in plain language. On a schedule, it fetches new items, has an AI model judge each one against your criteria, and sends a short Telegram digest. If nothing qualifies, it sends nothing.

**What it does.**
- Watches RSS/Atom feeds and subreddits.
- Runs each watch as a YAML task with its own criteria and schedule (`every 6h`, `daily at 07:00`).
- Sends a one-screen digest, each line linked to its source and marked act or FYI.
- Never reports the same item twice.

## Architecture

```
sources -> normalize -> dedup/store -> prefilter -> curate (model) -> render -> deliver
```

- **Prefilter.** Rules run first (age, keywords). Only new candidates reach the model.
- **Curate.** One model call per run. The model has no tools, files or network access, fetched text is wrapped as untrusted data, and its reply is parsed strictly.
- **Deliver.** An item is marked reported only after Telegram confirms delivery.
- **Failures.** A broken source is reported as an error, not as an empty feed. An external watchdog alerts if runs stop.
- **Harness.** Runs are tracked in a work ledger that survives restarts and rate limits ([docs/HARNESS.md](docs/HARNESS.md)).

More: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md), [docs/DECISIONS.md](docs/DECISIONS.md), and [docs/REFERENCE.md](docs/REFERENCE.md) for configuration and commands.

## Install

Python 3.10+ and PyYAML. Nothing else.

```bash
# Use GitHub's "Use this template" button (not Fork), then:
git clone https://github.com/<you>/<your-copy>.git && cd <your-copy>

# Demo: one offline run with a fictional feed. No keys.
bin/curated-digest demo

# Your instance, outside the repo (~/.config/curated-digest).
bin/curated-digest init --task changelog-watch
$EDITOR ~/.config/curated-digest/telegram.env ~/.config/curated-digest/gemini.env
$EDITOR ~/.config/curated-digest/tasks/changelog-watch.yaml   # replace every REWRITE: line
bin/curated-digest check

# First real run: real fetch and model, nothing sent, no state written.
bin/curated-digest task run changelog-watch --dry-run
```

To schedule it, copy `systemd/` to `~/.config/systemd/user/`, adjust the paths, and run `systemctl --user enable --now curated-digest-tick.timer`. A real run needs a Gemini key or a signed-in `claude` CLI, plus a Telegram bot. The Claude provider is tested live. Gemini is tested with mocks, so do one `--dry-run` with your key first. [docs/AGENT-SETUP.md](docs/AGENT-SETUP.md) lets a coding agent do the setup for you.

## About

Built by Tyler Lange directing Claude Code through an unattended build loop ([tools/PLAYBOOK.md](tools/PLAYBOOK.md)), with the test suite and `verify.sh` as the gate on every commit. MIT license.
