"""H1/H2: the ledger. One SQLite file, one row per unit of work.

A unit is one (job_id, run_id) pair. It exists as a row *before* it executes,
and the row is the only thing that decides what happens next. There is no
in-memory queue and no partial credit inside a run.
"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import config

STATES = ("pending", "running", "done", "failed", "rate_limited", "stuck")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id      TEXT PRIMARY KEY,
  cmd         TEXT NOT NULL,
  verify_cmd  TEXT,
  cwd         TEXT,
  kind        TEXT NOT NULL DEFAULT 'generic',   -- generic | build
  reset_repo  INTEGER NOT NULL DEFAULT 0,        -- H5: discard uncommitted first
  confine     TEXT,                                -- R34: CLAUDE_CONFINE_ROOTS
  timeout_s   INTEGER NOT NULL DEFAULT 3600,
  created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
  job_id             TEXT NOT NULL,
  run_id             TEXT NOT NULL,
  status             TEXT NOT NULL,
  created_at         INTEGER NOT NULL,
  next_at            INTEGER NOT NULL,   -- H7: retry goes to the back, not the front
  started_at         INTEGER,
  completed_at       INTEGER,
  attempts           INTEGER NOT NULL DEFAULT 0,
  last_error         TEXT,
  resume_after       INTEGER,
  limit_kind         TEXT,
  rate_limited_since INTEGER,
  baseline           TEXT,          -- side-effect witness captured at claim
  cost_usd           REAL NOT NULL DEFAULT 0.0,
  PRIMARY KEY (job_id, run_id)
);

CREATE INDEX IF NOT EXISTS units_status ON units(status, next_at);

CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  at      INTEGER NOT NULL,
  job_id  TEXT, run_id TEXT, kind TEXT NOT NULL, detail TEXT
);
"""


@contextmanager
def connect(path=None):
    conn = sqlite3.connect(path or config.db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")   # H3: the row must survive a crash
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.executescript(SCHEMA)
        yield conn
    finally:
        conn.close()


def now() -> int:
    return int(time.time())


def log(conn, kind, detail=None, job_id=None, run_id=None):
    conn.execute(
        "INSERT INTO events(at, job_id, run_id, kind, detail) VALUES (?,?,?,?,?)",
        (now(), job_id, run_id, kind,
         detail if isinstance(detail, str) or detail is None else json.dumps(detail)),
    )


def define_job(conn, job_id, cmd, verify_cmd=None, cwd=None, kind="generic",
               reset_repo=False, timeout_s=3600, confine=None):
    prev = conn.execute(
        "SELECT cmd, verify_cmd, cwd, kind, reset_repo, confine, timeout_s "
        "FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    conn.execute(
        """INSERT INTO jobs(job_id, cmd, verify_cmd, cwd, kind, reset_repo,
                            confine, timeout_s, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET
             cmd=excluded.cmd, verify_cmd=excluded.verify_cmd, cwd=excluded.cwd,
             kind=excluded.kind, reset_repo=excluded.reset_repo,
             confine=excluded.confine, timeout_s=excluded.timeout_s""",
        (job_id, cmd, verify_cmd, cwd, kind, int(reset_repo), confine,
         timeout_s, now()),
    )
    # Only log a real change. The dispatcher redefines every job on every tick
    # so a task file edit takes effect without a restart, and logging that
    # unconditionally buried the events that matter under one no-op per task
    # per tick.
    now_row = (cmd, verify_cmd, cwd, kind, int(reset_repo), confine, timeout_s)
    if prev is None or tuple(prev) != now_row:
        log(conn, "job_defined", job_id=job_id)


def submit(conn, job_id, run_id=None) -> str:
    """H2: the row exists before anything runs."""
    if not conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone():
        raise KeyError(f"no such job: {job_id}")
    run_id = run_id or uuid.uuid4().hex[:12]
    ts = now()
    conn.execute(
        "INSERT INTO units(job_id, run_id, status, created_at, next_at) "
        "VALUES (?,?,'pending',?,?)", (job_id, run_id, ts, ts),
    )
    log(conn, "submitted", job_id=job_id, run_id=run_id)
    return run_id


def claim(conn, at=None) -> sqlite3.Row | None:
    """H12: oldest eligible first — pending, or rate_limited whose window has
    reopened. Exactly one unit per call; the runner never loops (H15)."""
    at = at or now()
    row = conn.execute(
        """UPDATE units SET status='running', started_at=?
           WHERE (job_id, run_id) = (
             SELECT job_id, run_id FROM units
             WHERE status='pending'
                OR (status='rate_limited' AND resume_after IS NOT NULL
                    AND resume_after <= ?)
             ORDER BY next_at, attempts, rowid LIMIT 1)
           RETURNING *""",
        (at, at),
    ).fetchone()
    if row:
        log(conn, "claimed", job_id=row["job_id"], run_id=row["run_id"])
    return row


def set_baseline(conn, job_id, run_id, baseline):
    conn.execute("UPDATE units SET baseline=? WHERE job_id=? AND run_id=?",
                 (baseline, job_id, run_id))


def mark_done(conn, job_id, run_id, cost_usd=0.0):
    """H3: only ever called after the side effect has been *observed*."""
    conn.execute(
        """UPDATE units SET status='done', completed_at=?, last_error=NULL,
                            cost_usd=cost_usd+? WHERE job_id=? AND run_id=?""",
        (now(), cost_usd, job_id, run_id))
    log(conn, "done", job_id=job_id, run_id=run_id)


def mark_rate_limited(conn, job_id, run_id, kind, resume_after, detail=""):
    """H9: not a failure. Does not consume a retry."""
    conn.execute(
        """UPDATE units SET status='rate_limited', resume_after=?, limit_kind=?,
                 rate_limited_since=COALESCE(rate_limited_since, ?),
                 last_error=?, started_at=NULL
           WHERE job_id=? AND run_id=?""",
        (int(resume_after), kind, now(), detail[-2000:], job_id, run_id))
    log(conn, "rate_limited", {"kind": kind, "resume_after": int(resume_after)},
        job_id, run_id)


def mark_attempt_failed(conn, job_id, run_id, error, max_attempts, cost_usd=0.0):
    """H6: retries cap at max_attempts, then the unit is surfaced as stuck —
    never silently dropped (H14)."""
    conn.execute(
        "UPDATE units SET attempts=attempts+1, last_error=?, cost_usd=cost_usd+? "
        "WHERE job_id=? AND run_id=?",
        (str(error)[-2000:], cost_usd, job_id, run_id))
    attempts = conn.execute(
        "SELECT attempts FROM units WHERE job_id=? AND run_id=?",
        (job_id, run_id)).fetchone()["attempts"]
    status = "stuck" if attempts >= max_attempts else "pending"
    conn.execute(
        "UPDATE units SET status=?, started_at=NULL, completed_at=?, next_at=? "
        "WHERE job_id=? AND run_id=?",
        (status, now() if status == "stuck" else None, now(), job_id, run_id))
    log(conn, status, {"attempts": attempts, "error": str(error)[-500:]}, job_id, run_id)
    return status


def mark_stuck(conn, job_id, run_id, reason):
    conn.execute(
        "UPDATE units SET status='stuck', completed_at=?, last_error=? "
        "WHERE job_id=? AND run_id=?", (now(), reason, job_id, run_id))
    log(conn, "stuck", reason, job_id, run_id)


def requeue(conn, job_id=None, run_id=None) -> int:
    """H14: the human's way back in. Clears the rate-limit clock too."""
    where, args = "status IN ('stuck','failed','rate_limited')", []
    if job_id:
        where += " AND job_id=?"
        args.append(job_id)
    if run_id:
        where += " AND run_id=?"
        args.append(run_id)
    ts = now()
    cur = conn.execute(
        f"""UPDATE units SET status='pending', attempts=0, last_error=NULL,
              resume_after=NULL, limit_kind=NULL, rate_limited_since=NULL,
              completed_at=NULL, started_at=NULL, next_at={ts} WHERE {where}""", args)
    log(conn, "requeued", {"job_id": job_id, "run_id": run_id, "n": cur.rowcount})
    return cur.rowcount


def sweep_stuck(conn, cfg, at=None) -> int:
    """H14: rate_limited for more than 24h stops being 'waiting' and becomes a
    thing a human needs to look at."""
    at = at or now()
    cutoff = at - cfg["retry"]["stuck_after_rate_limited_seconds"]
    rows = conn.execute(
        "SELECT job_id, run_id, limit_kind FROM units "
        "WHERE status='rate_limited' AND rate_limited_since <= ?", (cutoff,)).fetchall()
    for r in rows:
        mark_stuck(conn, r["job_id"], r["run_id"],
                   f"rate_limited ({r['limit_kind']}) for over 24h")
    return len(rows)


def counts(conn) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) n FROM units GROUP BY status").fetchall()
    out = {s: 0 for s in STATES}
    out.update({r["status"]: r["n"] for r in rows})
    return out
