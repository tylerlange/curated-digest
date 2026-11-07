"""H3/H4/H5/H15/H16: one tick of work.

A tick claims at most one unit, runs it, and records the outcome. Waiting is
never a loop inside a process (H10) — a rate-limited unit records when its
window reopens and the tick exits. The next scheduled tick picks it up with no
human involved (H13).
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import config, ledger, ratelimit

# Passed to the job so it can report structured outcomes back. Falling back to
# exit code + text is always possible, but this is the honest channel.
RESULT_ENV = "CURATED_DIGEST_RESULT"


def paused() -> bool:
    return config.pause_flag().exists()


def _run(cmd, cwd=None, timeout=3600, env=None):
    return subprocess.run(cmd, shell=True, cwd=cwd, timeout=timeout, env=env,
                          capture_output=True, text=True)


def _git(args, cwd):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


def witness(job, cwd) -> str | None:
    """The side-effect witness: a value that changes iff the unit did its work.

    For build jobs that is the git HEAD, because a unit counts as complete only
    when committed (H5). For everything else it is whatever `verify_cmd`
    prints, which the runner compares before and after.
    """
    if job["kind"] == "build":
        rc, head = _git(["rev-parse", "HEAD"], cwd)
        return head if rc == 0 else None
    if job["verify_cmd"]:
        p = _run(job["verify_cmd"], cwd=cwd, timeout=120)
        return f"{p.returncode}:{p.stdout.strip()}"
    return None


def side_effect_confirmed(job, cwd, baseline) -> bool:
    """H3: 'exited 0' is not completion. The side effect must be observed."""
    if job["kind"] == "build":
        rc, head = _git(["rev-parse", "HEAD"], cwd)
        if rc != 0 or head == baseline:
            return False
        rc, dirty = _git(["status", "--porcelain"], cwd)
        return rc == 0 and dirty == ""       # H5: committed, not merely edited
    if job["verify_cmd"]:
        return witness(job, cwd) != baseline
    # No way to observe it: trust the exit code, and say so in the ledger.
    return True


def _scrubbed_env(job, cwd) -> dict:
    """R32: nothing handling untrusted input carries secrets. The harness runs
    trusted build work today, but the same runner will later run fetch jobs, so
    the default is a scrubbed environment and jobs opt in to what they need.

    R34: also the one place that sets CLAUDE_CONFINE_ROOTS. The hook at
    The PreToolUse confinement hook (see CLAUDE_CONFINE_HOOK in .env.example) is a no-op unless this is set, so without
    this line the confinement layer is inert for everything curated-digest runs. Default
    is the job's own working directory plus curated-digest's state.
    """
    keep = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TZ"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["CURATED_DIGEST_STATE_DIR"] = str(config.state_dir())
    env["CLAUDE_CONFINE_ROOTS"] = job["confine"] or f"{cwd}:{config.state_dir()}"
    return env


def execute(conn, unit, cfg) -> str:
    job = conn.execute("SELECT * FROM jobs WHERE job_id=?",
                       (unit["job_id"],)).fetchone()
    cwd = job["cwd"] or str(config.REPO)
    jid, rid = unit["job_id"], unit["run_id"]

    # H5: every run starts from a clean tree, so a half-finished previous
    # attempt cannot be mistaken for progress. Opt-in per job.
    if job["reset_repo"]:
        _git(["reset", "--hard", "HEAD"], cwd)
        _git(["clean", "-fd"], cwd)

    base = witness(job, cwd)
    ledger.set_baseline(conn, jid, rid, base)

    env = _scrubbed_env(job, cwd)
    ceiling = float(cfg["spend"]["ceiling_usd_per_run"])
    env["CURATED_DIGEST_SPEND_CEILING_USD"] = str(ceiling)     # H19
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as fh:
        result_path = Path(fh.name)
    env[RESULT_ENV] = str(result_path)

    try:
        p = _run(job["cmd"], cwd=cwd, timeout=job["timeout_s"], env=env)
        out = f"{p.stdout}\n{p.stderr}"
        rc = p.returncode
    except subprocess.TimeoutExpired:
        result_path.unlink(missing_ok=True)
        return ledger.mark_attempt_failed(
            conn, jid, rid, f"timeout after {job['timeout_s']}s",
            cfg["retry"]["max_attempts"])

    result = {}
    try:
        result = json.loads(result_path.read_text())
    except Exception:
        pass
    finally:
        result_path.unlink(missing_ok=True)

    cost = float(result.get("cost_usd") or 0.0)

    # Structured rate-limit report wins; text matching is the fallback (H8).
    verdict = None
    if result.get("outcome") == "rate_limited":
        verdict = ratelimit.Verdict(
            True, result.get("limit_kind", "five_hour"),
            int(result.get("resume_after")
                or time.time() + cfg["rate_limit"]["fallback_wait_seconds"]),
            "job")
    elif rc != 0:
        v = ratelimit.classify(out, cfg)
        verdict = v if v.is_rate_limit else None

    if verdict:
        ledger.mark_rate_limited(conn, jid, rid, verdict.kind,
                                 verdict.resume_after, out.strip())
        if verdict.kind == "weekly":
            ledger.log(conn, "notify",
                       "weekly limit hit — a week-long wait, not a normal pause",
                       jid, rid)          # H11
        return "rate_limited"

    if cost > ceiling:
        # H19 / R44: stop this unit, and make the reason visible rather than
        # letting an unattended loop quietly consume credits.
        conn.execute("UPDATE units SET cost_usd=cost_usd+? WHERE job_id=? AND run_id=?",
                     (cost, jid, rid))
        ledger.mark_stuck(conn, jid, rid,
                          f"spend ceiling exceeded: ${cost:.2f} > ${ceiling:.2f}")
        return "stuck"

    if rc != 0:
        return ledger.mark_attempt_failed(
            conn, jid, rid, f"exit {rc}: {out.strip()[-1500:]}",
            cfg["retry"]["max_attempts"], cost)

    if not side_effect_confirmed(job, cwd, base):
        return ledger.mark_attempt_failed(
            conn, jid, rid, "exited 0 but side effect not observed",
            cfg["retry"]["max_attempts"], cost)

    ledger.mark_done(conn, jid, rid, cost)
    return "done"


def reconcile(conn, cfg) -> int:
    """H4: a row left `running` by a dead process is *unknown*, not failed.
    Re-verify the side effect; requeue only if it cannot be confirmed."""
    rows = conn.execute("SELECT * FROM units WHERE status='running'").fetchall()
    for u in rows:
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?",
                           (u["job_id"],)).fetchone()
        cwd = (job["cwd"] if job else None) or str(config.REPO)
        if job and side_effect_confirmed(job, cwd, u["baseline"]):
            ledger.mark_done(conn, u["job_id"], u["run_id"])
            ledger.log(conn, "reconciled_done",
                       "side effect confirmed after crash", u["job_id"], u["run_id"])
        else:
            ledger.mark_attempt_failed(
                conn, u["job_id"], u["run_id"],
                "process died mid-flight; side effect not confirmed",
                cfg["retry"]["max_attempts"])
    return len(rows)


def tick(conn=None, cfg=None) -> dict:
    """One bounded action. A tick with nothing to do makes no API call and
    costs nothing (H13)."""
    cfg = cfg or config.load()
    close = conn is None
    ctx = ledger.connect() if close else None
    conn = ctx.__enter__() if close else conn
    try:
        if paused():                                  # H16/H17
            return {"action": "paused"}
        reconciled = reconcile(conn, cfg)             # H4
        swept = ledger.sweep_stuck(conn, cfg)         # H14
        unit = ledger.claim(conn)                     # H12
        if unit is None:
            return {"action": "idle", "reconciled": reconciled, "swept": swept}
        outcome = execute(conn, unit, cfg)
        return {"action": "ran", "job_id": unit["job_id"],
                "run_id": unit["run_id"], "outcome": outcome,
                "reconciled": reconciled, "swept": swept}
    finally:
        if close:
            ctx.__exit__(None, None, None)
