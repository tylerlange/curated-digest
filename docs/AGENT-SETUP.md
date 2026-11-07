# Agent setup guide

For a coding agent setting up a curated-digest instance for a new user.
Follow the steps in order. Do not read the source code unless a step fails
and its error message does not say how to fix it; if you had to, tell the user
which step so the guide can be fixed.

How to hand this file to your agent: in Claude Code or Codex, run it from the
repo root and say "Follow docs/AGENT-SETUP.md to set me up." For any other
agent, paste this file and give it shell access to the clone.

Ground rules for you, the agent:

- Instance files live outside the repo, in the config home
  (`~/.config/curated-digest` unless `CURATED_DIGEST_HOME` is set). Never write
  tasks or credentials inside the checkout, and never commit them.
- Never ask the user to paste a secret (bot token, API key, client secret) into
  chat, and never read the `*.env` files back. Tell them which file to edit;
  `init` already created it with mode 0600.
- Task criteria are the user's judgment, in their words. Do not invent
  interests or cut rules for them.

## 1. Check prerequisites

```bash
python3 --version              # needs 3.10+
python3 -c "import yaml"       # PyYAML; if missing, ask the user to install it
make check || scripts/check.sh # must pass before you change anything
```

`verify.sh` SKIPs three live-host checks on a fresh machine; that is expected.

## 2. Run the demo, then init

```bash
bin/curated-digest demo
```

Show the user the rendered digest: one `act`, two `fyi`, a chatter post cut,
and an injection attempt reported instead of obeyed. Then:

```bash
bin/curated-digest init --task TEMPLATE --id TASK_ID
```

Pick `TEMPLATE` from the interview below (`changelog-watch`,
`multi-subreddit` or `rss-generic`). Run `init --task` again for each extra
task; it never overwrites.

## 3. Interview the user and fill each task file

Ask one block at a time. Write the answers into
`~/.config/curated-digest/tasks/TASK_ID.yaml`, replacing every `REWRITE:` line.

**Sources**

1. What do you want watched? (A product changelog, a blog, a news section, some subreddits.)
2. For each: the feed URL. If they only know a web page, look for its RSS/Atom
   link and confirm it with them. Reddit uses `https://www.reddit.com/r/NAME/top/?t=week`.
3. How often does it publish, and how quickly do you need to hear? (Sets
   `schedule`: `every 6h`, `daily at 07:00`.)

**Criteria** (the most important part; push for specifics)

1. Who are you, as a reader of this source, in one or two sentences?
2. What would make you act this week? (Becomes the `act` tier.)
3. What is worth knowing but needs no action? (`fyi`.)
4. What should always be cut? Ask for examples of posts they are tired of seeing.

**Delivery**

1. Telegram is the shipped channel. Do they have a bot? If not, they create
   one with @BotFather (steps are in `telegram.env`).
2. Batched digest (default), `prompt` (sent on its own soon after), or
   `urgent` (interrupts)? Most tasks are `digest`.
3. Which model: `gemini:...` (needs a scoped Gemini key) or `claude:...`
   (needs the `claude` CLI signed in on this machine)? For raw internet
   content, docs/DECISIONS.md D28 recommends Gemini.

Optionally ask how digests should sound and edit
`~/.config/curated-digest/voice.md` (not a secret).

## 4. Validate the config

```bash
bin/curated-digest check
```

Expect `ERROR` lines for credentials the user has not filled in yet; that is
step 7. Fix every other `ERROR` (broken task file, bad schedule, missing
template line). Resolve `REWRITE:` warnings before step 6.

## 5. Re-run the demo

```bash
bin/curated-digest demo
```

Confirms the install still works after edits. It uses a temporary state dir
and touches nothing in the instance.

## 6. First real run as a dry run

This needs the model credential, so do it after the user has filled in step 7's
model key, or tell them it is the first thing to run afterwards.

```bash
bin/curated-digest task list
bin/curated-digest task run TASK_ID --dry-run
```

A dry run fetches and judges for real and writes no state. Show the user the
digest. If it is empty, that may be correct (silence is a valid outcome); ask
whether anything they expected is missing, and tighten or loosen the criteria.
If a source is reported broken, fix the URL.

## 7. Report what is left for the human

Tell the user, and stop:

- **Credentials**, each in its own 0600 file in the config home, filled in with
  an editor, never pasted into chat:
  - `telegram.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
  - `gemini.env`: `GEMINI_API_KEY` (only for `gemini:` models)
  - `reddit.env`: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` (only for Reddit sources)
  - `curated-digest.env`: `CURATED_DIGEST_HC_PING_URL`, a dead-man's-switch URL
    from any watchdog service, so a dead pipeline does not look like a quiet one
- Then `bin/curated-digest check` must print `OK`.
- **Scheduling**: copy `systemd/*` to `~/.config/systemd/user/`, fix the paths,
  `systemctl --user enable --now curated-digest-tick.timer`, and
  `loginctl enable-linger $USER`.
- **Identifier denylist** (if they will publish a fork): `.identifiers.local`
  in the repo root, one personal term per line, then `make hooks`.
- Anything you could not complete, with the exact error.
