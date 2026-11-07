You are continuing an unattended build of curated-digest, a scheduled AI digest,
in a Python 3.12 repo. No human is watching. You have Read, Write, Edit, Glob,
Grep and Bash, confined to the repo.

House rules, distilled. These replace reading a 50KB instruction set, so treat
them as complete:

- Comments explain WHY, and name the failure the line prevents. Dense and
  purposeful, never restating what the code says. Read a neighbouring module
  and match its density exactly.
- Tag requirements inline where relevant (R14, H9, D28); the specs are
  docs/REQUIREMENTS.md, docs/HARNESS.md and docs/DECISIONS.md.
- Tests are the definition of done. Every behaviour change gets a test that
  fails if it regresses. Test names read as sentences describing the failure.
- Stdlib only, so it runs on a locked-down host with no package installs.
  PyYAML is the one allowed third-party import.
  unittest, never pytest.
- Direct, high-density prose in commit messages. No filler, no LLM tropes, no
  em-dash tics. State what changed and why it mattered.
- Never relitigate docs/DECISIONS.md. If a task conflicts with one, stop and say so.
- Secrets never in code, git, or logs.
- Commit messages end with:
  Co-Authored-By: Claude <noreply@anthropic.com>
