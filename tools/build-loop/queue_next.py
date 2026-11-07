#!/usr/bin/env python3
"""Keep the build loop moving without a human: queue the next NEXT.md item.

    tools/build-loop/queue_next.py start|stop|status   switch the loop on/off
    tools/build-loop/queue_next.py tick                 submit one build unit if due

Run `tick` from the same timer as the harness, before `curated-digest run`
(see README.md here). It lives outside the core CLI because it is how this
repo was built, not something a digest operator runs.

The gap it closes: when a usage window ran out mid-build, scheduled digest
tasks resumed on their own and the build did not, because nothing queued the
next item. One unit at a time: the harness runs a single unit per tick, so
queuing ahead would pile up work that cannot start and could double-execute
an item.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from curated_digest import config, ledger  # noqa: E402

JOB = "build"


def queue_file() -> Path:
    return Path(os.environ.get("CURATED_DIGEST_QUEUE", HERE / "NEXT.md"))


def building_flag() -> Path:
    """Opt-in switch. Explicit on purpose: an agent that queues its own next
    unit forever should need a deliberate start, and must be stoppable
    without editing code or units."""
    return config.state_dir() / "BUILDING"


def unchecked(queue: Path) -> list:
    if not queue.exists():
        return []
    return [ln for ln in queue.read_text().splitlines() if ln.startswith("- [ ]")]


def submit_if_queued(c) -> list:
    if not building_flag().exists() or not unchecked(queue_file()):
        return []
    busy = c.execute(
        "SELECT 1 FROM units WHERE job_id=? AND status IN "
        "('pending','running','rate_limited')", (JOB,)).fetchone()
    if busy:
        return []
    if not c.execute("SELECT 1 FROM jobs WHERE job_id=?", (JOB,)).fetchone():
        return []
    return [{"task": JOB, "run_id": ledger.submit(c, JOB)}]


def main(argv) -> int:
    action = argv[1] if len(argv) > 1 else "status"
    if action == "start":
        building_flag().write_text("started\n")
        print("build loop on: the next tick queues the first unchecked item")
    elif action == "stop":
        building_flag().unlink(missing_ok=True)
        print("build loop off: work in flight finishes, nothing new is queued")
    elif action == "tick":
        with ledger.connect() as c:
            print(submit_if_queued(c))
    elif action == "status":
        todo = unchecked(queue_file())
        print(f"{'on' if building_flag().exists() else 'off'} | {len(todo)} item(s) queued")
        for line in todo:
            print("  " + line[6:][:100])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
