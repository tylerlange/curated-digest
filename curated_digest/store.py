"""R11-R17: the store. Ingested items, and the record of what has already been
reported.

This is the area where being wrong is most visible. Re-reporting an item the reader
has already seen is the failure mode that makes the whole system feel broken,
so the rules here are deliberately strict:

  - identity is the feed's, not ours, wherever the feed provides one
  - an item is retained after it is used, because a later renderer may want it
  - the "already reported" record advances ONLY after delivery is confirmed
  - a store that has been lost cannot flood the channel on the way back
"""
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  source_id    TEXT NOT NULL,
  item_id      TEXT NOT NULL,      -- feed GUID, else sha256(link + title)
  url          TEXT,
  title        TEXT,
  body         TEXT,
  published_at INTEGER,            -- may be NULL: feeds lie about dates
  fetched_at   INTEGER NOT NULL,
  PRIMARY KEY (source_id, item_id)
);
CREATE INDEX IF NOT EXISTS items_seen ON items(source_id, fetched_at);

-- R12/R14. A row here means "the reader has seen this", and it is written only
-- after a delivery has been confirmed. Separate from `items` because the same
-- item can legitimately reach more than one task.
CREATE TABLE IF NOT EXISTS reported (
  task_id      TEXT NOT NULL,
  source_id    TEXT NOT NULL,
  item_id      TEXT NOT NULL,
  reported_at  INTEGER NOT NULL,
  reason       TEXT NOT NULL DEFAULT 'delivered',   -- delivered | watermark
  PRIMARY KEY (task_id, source_id, item_id)
);

-- R36: a source that has started failing must be distinguishable from a source
-- that is genuinely empty. Silence is only correct when it is *earned*.
CREATE TABLE IF NOT EXISTS source_state (
  source_id            TEXT PRIMARY KEY,
  etag                 TEXT,
  last_modified        TEXT,
  last_ok_at           INTEGER,
  -- Distinct from last_ok_at on purpose. A 304 is a successful fetch, so
  -- last_ok_at advances on one — which means a feed that quietly stopped
  -- publishing looks permanently healthy by that column alone. This one moves
  -- only when the server actually handed us a body.
  last_change_at       INTEGER,
  last_attempt_at      INTEGER,
  last_error           TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0
);

-- R16: a condition that persists must escalate, not repeat identically and
-- not vanish. Without this the tick would re-send the same alert every 10
-- minutes until the reader mutes the channel that also carries real findings.
CREATE TABLE IF NOT EXISTS alerts (
  key        TEXT PRIMARY KEY,
  first_seen INTEGER NOT NULL,
  last_sent  INTEGER,
  sends      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_state (
  task_id           TEXT PRIMARY KEY,
  initialized_at    INTEGER NOT NULL,   -- R15: the first-run watermark
  last_delivered_at INTEGER,
  -- Distinct from last_delivered_at on purpose: a run that correctly delivered
  -- nothing still ran, and scheduling must key off that or a quiet task fires
  -- on every single tick forever.
  last_run_at       INTEGER
);
"""

FIRST_RUN_CAP = 10          # R15

# R13 keeps items on purpose, but nothing ever deleted one and the Reddit task
# alone ingests ~150 a day. Six months is chosen so "forever" is a decision
# rather than an oversight: it is far longer than any renderer we have plans
# for looks back, and short enough that the ledger stays a file you can copy.
RETENTION_DAYS = 180


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    _migrate(conn)


def _migrate(conn):
    """CREATE TABLE IF NOT EXISTS silently does nothing to a table that already
    exists, so a new column never lands on a live database. Add them here."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(task_state)")}
    if "last_run_at" not in have:
        conn.execute("ALTER TABLE task_state ADD COLUMN last_run_at INTEGER")

    have = {r["name"] for r in conn.execute("PRAGMA table_info(source_state)")}
    if "last_change_at" not in have:
        conn.execute("ALTER TABLE source_state ADD COLUMN last_change_at INTEGER")
        # Backfill, or every source that predates this column is invisible to
        # the 304 check forever: last_change_at would stay NULL while last_ok_at
        # kept advancing on each 304, so the fallback baseline would never age.
        # Seeding it with the last known-good fetch gives every existing source
        # a clock that starts now and can actually run out.
        conn.execute("UPDATE source_state SET last_change_at=last_ok_at")


def record_run(conn, task_id):
    """Scheduling keys off this, never off delivery."""
    conn.execute(
        """INSERT INTO task_state(task_id, initialized_at, last_run_at)
           VALUES (?,?,?)
           ON CONFLICT(task_id) DO UPDATE SET last_run_at=excluded.last_run_at""",
        (task_id, now(), now()))


def last_run(conn, task_id) -> int | None:
    r = conn.execute("SELECT last_run_at FROM task_state WHERE task_id=?",
                     (task_id,)).fetchone()
    return r["last_run_at"] if r else None


def now() -> int:
    return int(time.time())


def ingest(conn, items) -> int:
    """R13: items are retained, not consumed. Re-ingesting an item we already
    have is a no-op — the first `fetched_at` we saw is the true one, and
    overwriting it would let an edited feed resurrect old items."""
    new = 0
    for it in items:
        cur = conn.execute(
            """INSERT INTO items(source_id, item_id, url, title, body,
                                 published_at, fetched_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(source_id, item_id) DO NOTHING""",
            (it.source_id, it.id, it.url, it.title, it.body,
             it.published_at, it.fetched_at))
        new += cur.rowcount
    return new


def candidates(conn, task_id, source_ids, limit=None) -> list[sqlite3.Row]:
    """Items this task has not reported yet, newest first.

    R15/R17 both land here. On a task's very first run — which is also what a
    restored-from-backup or freshly-rebuilt store looks like — everything in the
    store is technically unreported. Returning all of it would flood the
    channel, so the first run takes the newest few and writes off the rest as
    `watermark`. That is what makes losing the store survivable rather than
    catastrophic.
    """
    if not source_ids:
        return []
    marks = ",".join("?" * len(source_ids))
    first_run = conn.execute(
        "SELECT 1 FROM task_state WHERE task_id=?", (task_id,)).fetchone() is None

    rows = conn.execute(
        f"""SELECT i.* FROM items i
            WHERE i.source_id IN ({marks})
              AND NOT EXISTS (SELECT 1 FROM reported r
                              WHERE r.task_id=? AND r.source_id=i.source_id
                                AND r.item_id=i.item_id)
            ORDER BY COALESCE(i.published_at, i.fetched_at) DESC, i.rowid DESC""",
        (*source_ids, task_id)).fetchall()

    if first_run:
        conn.execute(
            "INSERT INTO task_state(task_id, initialized_at) VALUES (?,?)",
            (task_id, now()))
        keep, writeoff = rows[:FIRST_RUN_CAP], rows[FIRST_RUN_CAP:]
        if writeoff:
            _record(conn, task_id, writeoff, "watermark")
        rows = keep

    return rows[:limit] if limit else rows


def mark_reported(conn, task_id, rows) -> int:
    """R14: call this ONLY after delivery has been confirmed.

    Calling it before a send succeeds is the bug that loses a finding
    permanently — the item is marked seen, the send fails, and it never comes
    back. The harness has the same rule for its own units (H3).
    """
    n = _record(conn, task_id, rows, "delivered")
    conn.execute(
        """INSERT INTO task_state(task_id, initialized_at, last_delivered_at)
           VALUES (?,?,?)
           ON CONFLICT(task_id) DO UPDATE SET last_delivered_at=excluded.last_delivered_at""",
        (task_id, now(), now()))
    return n


def mark_judged(conn, task_id, rows) -> int:
    """Items a model looked at and rejected.

    R14 governs *delivery*, and there is no delivery here — nothing can be lost
    by recording a rejection. Without this, every run re-judges the same
    rejected items forever and cost stops scaling with new candidates (R22).
    Kept items still wait for a confirmed send.
    """
    return _record(conn, task_id, rows, "judged")


def _record(conn, task_id, rows, reason) -> int:
    n = 0
    for r in rows:
        cur = conn.execute(
            """INSERT INTO reported(task_id, source_id, item_id, reported_at, reason)
               VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (task_id, r["source_id"], r["item_id"], now(), reason))
        n += cur.rowcount
    return n


def sweep_items(conn, readers: dict, *, older_than_days=RETENTION_DAYS,
                at=None) -> int:
    """Drop items older than the retention window that no task still needs.

    `readers` maps source_id -> the task ids that read that source. Age alone
    is not enough to delete on: a task that has been rate-limited or broken for
    a week still has unreported items, and deleting those would lose findings
    that R14 exists to protect — R14 broken by the cleaner instead of by the
    pipeline. So an item goes only when every task reading its source has a
    `reported` row for it (delivered, judged or watermark alike).

    A source no task reads any more — a feed deleted from `tasks/` — has no
    readers at all, and the age test is then the only one left. Nothing will
    ever ask for those items again.

    Deliberately does NOT delete the matching `reported` rows. They are
    tombstones: a feed that still lists a six-month-old entry would otherwise
    be re-ingested as brand new and reported a second time, which is the exact
    R12 failure this module exists to prevent. Three short columns per item is
    what we pay to keep that guarantee; the bodies are what actually grow.

    Keyed on `fetched_at`, not `published_at`: published dates are supplied by
    the feed and are often absent or wrong, and retention must be measured in
    time we observed, not time a publisher claims.
    """
    cutoff = (at if at is not None else now()) - older_than_days * 86400
    swept = 0
    stale_sources = [r["source_id"] for r in conn.execute(
        "SELECT DISTINCT source_id FROM items WHERE fetched_at < ?", (cutoff,))]
    for source_id in stale_sources:
        task_ids = sorted(set(readers.get(source_id) or ()))
        if task_ids:
            marks = ",".join("?" * len(task_ids))
            cur = conn.execute(
                f"""DELETE FROM items
                     WHERE source_id=? AND fetched_at < ?
                       AND (SELECT COUNT(DISTINCT r.task_id) FROM reported r
                             WHERE r.source_id=items.source_id
                               AND r.item_id=items.item_id
                               AND r.task_id IN ({marks})) = ?""",
                (source_id, cutoff, *task_ids, len(task_ids)))
        else:
            cur = conn.execute(
                "DELETE FROM items WHERE source_id=? AND fetched_at < ?",
                (source_id, cutoff))
        swept += cur.rowcount
    return swept


def conditional_headers(conn, source_id) -> dict:
    """R22: cost scales with new candidates, not feed size. Cheapest possible
    fetch is the one the server answers with 304."""
    r = conn.execute("SELECT etag, last_modified FROM source_state WHERE source_id=?",
                     (source_id,)).fetchone()
    if not r:
        return {}
    return {k: v for k, v in
            (("etag", r["etag"]), ("modified", r["last_modified"])) if v}


def record_success(conn, source_id, etag=None, last_modified=None, changed=True):
    """`changed=False` is the 304 case: the fetch succeeded, but the server told
    us nothing has been published. Both are successes and both clear the failure
    count; only a real body moves `last_change_at`, which is what
    `unchanged_sources` measures a dead feed against."""
    at = now()
    conn.execute(
        """INSERT INTO source_state(source_id, etag, last_modified, last_ok_at,
                                    last_change_at, last_attempt_at, last_error,
                                    consecutive_failures)
           VALUES (?,?,?,?,?,?,NULL,0)
           ON CONFLICT(source_id) DO UPDATE SET
             etag=COALESCE(excluded.etag, source_state.etag),
             last_modified=COALESCE(excluded.last_modified, source_state.last_modified),
             last_ok_at=excluded.last_ok_at,
             last_change_at=COALESCE(excluded.last_change_at, source_state.last_change_at),
             last_attempt_at=excluded.last_attempt_at,
             last_error=NULL, consecutive_failures=0""",
        (source_id, etag, last_modified, at, at if changed else None, at))


def record_failure(conn, source_id, error) -> int:
    """R36/R38: returns the consecutive failure count so the caller can decide
    when a flaky fetch has become a broken source worth reporting."""
    conn.execute(
        """INSERT INTO source_state(source_id, last_attempt_at, last_error,
                                    consecutive_failures)
           VALUES (?,?,?,1)
           ON CONFLICT(source_id) DO UPDATE SET
             last_attempt_at=excluded.last_attempt_at,
             last_error=excluded.last_error,
             consecutive_failures=source_state.consecutive_failures+1""",
        (source_id, now(), str(error)[-1000:]))
    return conn.execute(
        "SELECT consecutive_failures c FROM source_state WHERE source_id=?",
        (source_id,)).fetchone()["c"]


def _change_interval(conn, source_id, poll_interval) -> int:
    """How often this feed is observed to actually change, never less often
    than we poll it.

    The poll interval alone is the wrong yardstick and would make the alarm
    below useless. A weekly changelog task polls every 6h and publishes roughly
    weekly, so three poll intervals of "unchanged" is its normal Tuesday:
    alerting on that would fire five days out of seven, suppress the watchdog
    ping for the same five days, and train the reader to ignore the channel that
    also carries real findings (R16). A feed is only suspicious when it has
    gone quiet by its own standards.

    Distinct `fetched_at` values, not item counts: the first fetch of a feed
    ingests its whole backlog at one instant, and counting that as ten changes
    would drag the estimate towards zero and put the noise right back.
    """
    r = conn.execute(
        """SELECT COUNT(DISTINCT fetched_at) n, MIN(fetched_at) lo,
                  MAX(fetched_at) hi FROM items WHERE source_id=?""",
        (source_id,)).fetchone()
    # Two observations describe one gap, which is not a cadence. Below that,
    # trust the poll interval rather than a number made from a single sample.
    if not r or (r["n"] or 0) < 3:
        return poll_interval
    return max(poll_interval, (r["hi"] - r["lo"]) // (r["n"] - 1))


def unchanged_sources(conn, interval_by_source, *, factor=3, at=None) -> list[dict]:
    """R36's quiet half: a feed answering 304 forever is indistinguishable from
    one that has stopped publishing, and broken must not look like empty.

    `record_failure` covers the loud half — 404, a parse error, a timeout. This
    covers the source that keeps saying yes and never says anything, which is
    the case nothing else in the pipeline can see: every layer downstream reads
    "no new items" and correctly stays silent (D7).

    Deliberately skips a source that is already failing. It would otherwise be
    reported twice under two different names for one underlying problem, and a
    source that cannot be fetched has not been observed to change either.
    """
    at = at or now()
    out = []
    for source_id, poll in interval_by_source.items():
        r = conn.execute("SELECT * FROM source_state WHERE source_id=?",
                         (source_id,)).fetchone()
        # No row, no successful fetch yet, or already reported broken: nothing
        # here is evidence about publishing, only about fetching.
        if not r or r["last_change_at"] is None or r["consecutive_failures"]:
            continue
        quiet = at - r["last_change_at"]
        if quiet > factor * _change_interval(conn, source_id, poll):
            out.append({"source_id": source_id, "unchanged_for": quiet})
    return out


def broken_sources(conn, threshold=3) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_state WHERE consecutive_failures >= ? "
        "ORDER BY consecutive_failures DESC", (threshold,)).fetchall()


def stale_tasks(conn, interval_by_task: dict, at=None) -> list[str]:
    """R37: a task that has not RUN for 3x its own schedule interval.

    This originally measured last *delivery*, which put R37 in direct conflict
    with R26 — silence is the default and correct outcome. A task doing exactly
    its job for a quiet day got reported as possibly broken, and the alert then
    repeated on schedule. Observed: a weekly changelog task ran every 6h without
    issue, found nothing worth sending for 24h, and was flagged stale.

    Running is the thing that can actually stop. A task that runs and delivers
    nothing is healthy; a task that has stopped running is not, and that is
    what the timer, the schedule parser or a permanent yield would break.
    """
    at = at or now()
    out = []
    for task_id, interval in interval_by_task.items():
        r = conn.execute("SELECT * FROM task_state WHERE task_id=?",
                         (task_id,)).fetchone()
        if not r:
            continue
        last = r["last_run_at"] or r["initialized_at"]
        if at - last > 3 * interval:
            out.append(task_id)
    return out


def should_alert(conn, key, min_interval=21600, at=None) -> tuple[bool, int]:
    """Returns (send_it, hours_broken).

    First occurrence always sends. After that, at most once per min_interval,
    and the caller is given how long the condition has been going so the repeat
    carries new information rather than being the same message again (R16).
    """
    at = at or now()
    r = conn.execute("SELECT * FROM alerts WHERE key=?", (key,)).fetchone()
    if r is None:
        conn.execute("INSERT INTO alerts(key, first_seen, last_sent, sends) "
                     "VALUES (?,?,?,1)", (key, at, at))
        return True, 0
    hours = (at - r["first_seen"]) // 3600
    if r["last_sent"] and at - r["last_sent"] < min_interval:
        return False, hours
    conn.execute("UPDATE alerts SET last_sent=?, sends=sends+1 WHERE key=?",
                 (at, key))
    return True, hours


def clear_alert(conn, key):
    """A resolved condition forgets its history, so if it recurs later that is
    reported as new rather than silently folded into the old escalation."""
    conn.execute("DELETE FROM alerts WHERE key=?", (key,))


def integrity(conn) -> str:
    """R17: corruption must be *detectable*. SQLite will happily serve a
    partially-readable file until the moment it will not."""
    return conn.execute("PRAGMA integrity_check").fetchone()[0]
