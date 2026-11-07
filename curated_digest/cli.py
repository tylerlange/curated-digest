"""H16/H18: the CLI. Verbs are run, status, pause, resume, requeue, plus job
definition and submission, task commands, and instance setup (init, check)."""
import argparse
import json
import sys
import time
from dataclasses import replace

from . import config, ledger, runner


def _age(ts):
    if not ts:
        return "-"
    d = int(time.time()) - int(ts)
    for n, u in ((86400, "d"), (3600, "h"), (60, "m")):
        if abs(d) >= n:
            return f"{d // n}{u}"
    return f"{d}s"


def cmd_define(a):
    with ledger.connect() as c:
        ledger.define_job(c, a.job_id, a.cmd, a.verify, a.cwd, a.kind,
                          a.reset_repo, a.timeout)
    print(f"job {a.job_id} defined")


def cmd_submit(a):
    with ledger.connect() as c:
        print(ledger.submit(c, a.job_id, a.run_id))


def cmd_run(a):
    res = runner.tick()
    print(json.dumps(res))
    return 0


def cmd_status(a):
    cfg = config.load()
    with ledger.connect() as c:
        counts = ledger.counts(c)
        print("paused" if runner.paused() else "active", "| " + "  ".join(
            f"{k}={v}" for k, v in counts.items() if v or k in ("pending", "running")))
        # H13a: waiting is shown as information, never as something to act on.
        for r in c.execute(
                "SELECT * FROM units WHERE status='rate_limited' "
                "ORDER BY resume_after").fetchall():
            when = time.strftime("%H:%M", time.localtime(r["resume_after"]))
            print(f"  waiting  {r['job_id']}/{r['run_id']}  "
                  f"{r['limit_kind']} until {when} (auto-resumes)")
        for r in c.execute(
                "SELECT * FROM units WHERE status IN ('stuck','failed') "
                "ORDER BY completed_at DESC LIMIT 20").fetchall():
            print(f"  NEEDS YOU  {r['job_id']}/{r['run_id']}  "
                  f"attempts={r['attempts']}  {(r['last_error'] or '')[:120]}")
        spent = c.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM units").fetchone()["s"]
        if spent:
            print(f"  spend: ${spent:.2f} total "
                  f"(ceiling ${cfg['spend']['ceiling_usd_per_run']:.2f}/run)")


def _tasks_dir():
    from . import setup
    return str(setup.require_tasks_dir())


def cmd_task_list(a):
    from . import store, task
    tasks, errors = task.load_all(_tasks_dir())
    with ledger.connect() as c:
        store.ensure_schema(c)
        for t in tasks:
            lr = store.last_run(c, t.id)
            due = task.is_due(t.schedule, lr, time.time())
            print(f"  {t.id:<24} {t.schedule:<16} {t.model:<28} "
                  f"{'DUE' if due else 'waiting'}")
    # R9: broken tasks are surfaced, never silently skipped.
    for e in errors:
        print(f"  BROKEN  {e}")


def cmd_task_run(a):
    from . import dispatch, store, task
    tasks, errors = task.load_all(_tasks_dir())
    found = [t for t in tasks if t.id == a.task_id]
    if not found:
        print(json.dumps({"error": f"no such task: {a.task_id}",
                          "broken": [str(e) for e in errors]}))
        return 1

    t = found[0]
    if a.model:
        # R8 applies to an override exactly as it applies to the file: a bare
        # model name is refused rather than resolved against some default.
        if not task.valid_model(a.model):
            print(json.dumps(
                {"error": f"--model {a.model!r} must be 'provider:model'"}))
            return 2
        # In-memory only, for this one invocation. Nothing writes it back to
        # the YAML or to the store, so trying a model here cannot silently
        # become the model every scheduled run afterwards uses.
        t = replace(t, model=a.model)

    from .providers import ProviderError, RateLimited
    try:
        with ledger.connect() as c:
            res = dispatch.run_task(c, t, dry_run=a.dry_run)
            if not a.dry_run:
                store.record_run(c, t.id)
    except RateLimited:
        raise  # the harness classifies this from the exit; do not mask it
    except ProviderError as e:
        # A missing or placeholder credential is a setup problem: say what to
        # fix instead of printing a traceback. Exit non-zero so the harness
        # still counts it as a failed run.
        print(json.dumps({"task": t.id, "outcome": "provider_error",
                          "error": str(e)[:500],
                          "fix": "run `bin/curated-digest check`"}))
        return 1
    if a.dry_run and res.get("artifact"):
        print(res["artifact"])
        print("---")
    print(json.dumps({k: v for k, v in res.items() if k != "artifact"}))
    return 0


def cmd_dispatch(a):
    """Submit a harness unit for every task that is due.

    Deliberately does no work itself: the harness owns execution, so a task
    inherits crash-resume, rate-limit yielding and the pause flag for free.
    That is the whole reason the harness was built first.
    """
    from . import store, task
    tasks, errors = task.load_all(_tasks_dir())
    submitted = []
    with ledger.connect() as c:
        store.ensure_schema(c)
        swept = _sweep(c, tasks, errors)
        for t in tasks:
            job_id = f"task:{t.id}"
            ledger.define_job(
                c, job_id, f"{config.REPO}/bin/curated-digest task run {t.id}",
                cwd=str(config.REPO), timeout_s=1800)
            if not task.is_due(t.schedule, store.last_run(c, t.id), time.time()):
                continue
            # Never queue a second unit for a task already waiting to run —
            # a rate-limited task would otherwise pile up one unit per tick.
            busy = c.execute(
                "SELECT 1 FROM units WHERE job_id=? AND status IN "
                "('pending','running','rate_limited')", (job_id,)).fetchone()
            if busy:
                continue
            submitted.append({"task": t.id, "run_id": ledger.submit(c, job_id)})
    print(json.dumps({"submitted": submitted, "swept": swept,
                      "broken": [str(e) for e in errors]}))
    return 0


def _sweep(c, tasks, errors) -> int:
    """Retention runs here because this is the one verb that both runs every
    tick and already knows which task reads which source — the store cannot
    work that out on its own without importing the task layer.

    A task file that failed to load contributes no sources, so its feeds would
    look unread and its undelivered items would be swept while the only thing
    wrong is a typo someone has not fixed yet. Skipping the sweep entirely
    until every task loads costs nothing but disk; `curated-digest health` already
    reports the broken file, so this cannot hide indefinitely.
    """
    from . import store
    if errors:
        return 0
    readers = {}
    for t in tasks:
        for src in t.sources:
            readers.setdefault(src.id, []).append(t.id)
    return store.sweep_items(c, readers)


def cmd_init(a):
    from . import setup
    return setup.init(a.task, a.id)


def cmd_demo(a):
    from . import demo
    return demo.run()


def cmd_check(a):
    from . import setup
    r = setup.check()
    print(f"config home: {config.config_home()}\ntasks:       {config.tasks_dir()}\n"
          f"state:       {config.state_dir()}")
    for e in r.errors:
        print(f"ERROR  {e}")
    for w in r.warnings:
        print(f"WARN   {w}")
    print("OK" if r.ok else "FAILED")
    return 0 if r.ok else 1


def cmd_health(a):
    """R39: the tick calls this. A clean report pings the external watchdog;
    a dirty one alerts and deliberately does NOT ping, so the watchdog also
    fires if curated-digest is too broken to alert."""
    from . import health, store, task
    tasks, errors = task.load_all(_tasks_dir())
    with ledger.connect() as c:
        report = health.check(c, tasks)
    report["broken_tasks"] = [str(e) for e in errors]

    with ledger.connect() as c:
        store.ensure_schema(c)
        keys = {k for k, _ in health.conditions(report)}
        # A resolved condition forgets its history, so a recurrence later is
        # reported as new rather than folded into the old escalation.
        for row in c.execute("SELECT key FROM alerts").fetchall():
            if row["key"] not in keys:
                store.clear_alert(c, row["key"])
        throttle = ((lambda k: store.should_alert(c, k)) if a.notify else None)
        alert = health.render_alert(report, throttle)

    if alert and a.notify:
        from .channels import telegram as ch
        from .renderers import Artifact
        try:
            ch.send(Artifact(alert))
        except Exception as e:
            report["alert_error"] = str(e)[:200]

    # The ping is driven by the report, NEVER by whether an alert was sent.
    # Those came apart when a condition was throttled: `alert` is None between
    # repeats, which the old `elif not alert` read as healthy and pinged — so
    # during a real outage curated-digest told the dead-man's switch everything was fine,
    # which is the one thing the watchdog exists to prevent.
    if health.is_healthy(report):
        report["pinged"] = health.ping()

    print(json.dumps(report, default=str))
    return 0


def cmd_pause(a):
    config.pause_flag().write_text(f"{a.reason or 'manual'}\n")
    print("paused — no new work will be claimed; work in flight continues")


def cmd_resume(a):
    config.pause_flag().unlink(missing_ok=True)
    print("resumed")


def cmd_requeue(a):
    with ledger.connect() as c:
        print(f"requeued {ledger.requeue(c, a.job_id, a.run_id)}")


def cmd_events(a):
    with ledger.connect() as c:
        for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?",
                           (a.limit,)).fetchall():
            print(f"{_age(r['at']):>5} ago  {r['kind']:<16} "
                  f"{r['job_id'] or '-'}/{r['run_id'] or '-'}  {r['detail'] or ''}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="curated-digest", description="curated-digest harness")
    s = p.add_subparsers(dest="cmd", required=True)

    d = s.add_parser("define", help="define or update a job")
    d.add_argument("job_id")
    d.add_argument("cmd")
    d.add_argument("--verify", help="command whose output witnesses the side effect")
    d.add_argument("--cwd")
    d.add_argument("--kind", choices=["generic", "build"], default="generic")
    d.add_argument("--reset-repo", action="store_true",
                   help="discard uncommitted changes before each run (H5)")
    d.add_argument("--timeout", type=int, default=3600)
    d.set_defaults(fn=cmd_define)

    x = s.add_parser("submit", help="queue one run of a job")
    x.add_argument("job_id")
    x.add_argument("--run-id")
    x.set_defaults(fn=cmd_submit)

    s.add_parser("run", help="one tick: reconcile, sweep, claim, execute"
                 ).set_defaults(fn=cmd_run)
    s.add_parser("status", help="counts by state").set_defaults(fn=cmd_status)

    pa = s.add_parser("pause", help="stop claiming new work")
    pa.add_argument("--reason")
    pa.set_defaults(fn=cmd_pause)

    s.add_parser("resume", help="clear the pause flag").set_defaults(fn=cmd_resume)

    rq = s.add_parser("requeue", help="return stuck/failed units to pending")
    rq.add_argument("--job-id")
    rq.add_argument("--run-id")
    rq.set_defaults(fn=cmd_requeue)

    t = s.add_parser("task", help="task files")
    ts = t.add_subparsers(dest="taskcmd", required=True)
    ts.add_parser("list", help="tasks, schedules, and which are due"
                  ).set_defaults(fn=cmd_task_list)
    tr = ts.add_parser("run", help="run one task end to end")
    tr.add_argument("task_id")
    tr.add_argument("--dry-run", action="store_true",
                    help="produce output, write no state (R46)")
    tr.add_argument("--model", metavar="PROVIDER:MODEL",
                    help="run against this model instead of the task's, for "
                         "this invocation only; never written back")
    tr.set_defaults(fn=cmd_task_run)

    s.add_parser("dispatch", help="queue a unit for every due task"
                 ).set_defaults(fn=cmd_dispatch)

    it = s.add_parser("init", help="create the config home, 0600 credential files, a task")
    it.add_argument("--task", metavar="TEMPLATE",
                    help="scaffold a task from templates/tasks/TEMPLATE.yaml")
    it.add_argument("--id", help="id (and file name) for the scaffolded task")
    it.set_defaults(fn=cmd_init)

    s.add_parser("check", help="validate config, tasks and credential files"
                 ).set_defaults(fn=cmd_check)
    s.add_parser("demo", help="run the pipeline once offline: fictional feed, stub "
                 "judge, no keys, nothing delivered").set_defaults(fn=cmd_demo)

    hl = s.add_parser("health", help="machine facts; pings the watchdog if clean")
    hl.add_argument("--notify", action="store_true",
                    help="send an alert to the channel when unhealthy")
    hl.set_defaults(fn=cmd_health)

    ev = s.add_parser("events", help="recent ledger events")
    ev.add_argument("--limit", type=int, default=30)
    ev.set_defaults(fn=cmd_events)

    a = p.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
