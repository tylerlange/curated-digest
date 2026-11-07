"""R37/R38/R39/R42b: is curated-digest actually working?

The hard part is the watchdog paradox. Telegram is the notification channel, so
if curated-digest dies, silence means either "nothing was worth sending" — the normal
outcome — or "the whole pipeline is dead". Those must not look the same.

The answer is an external dead-man's switch on a *different failure domain*:
a healthy tick pings an external dead-man's-switch service, which alerts by
email (or any route other than the delivery channel) if the ping stops.
Absence of the ping is the alarm, and email is deliberately the one thing the
Telegram consolidation does not absorb.

Nothing in this file calls a model. R42b: machine facts are deterministic, and
an alert about the pipeline being broken must still work when the pipeline is
broken.
"""
import os
import urllib.error
import urllib.request
from pathlib import Path

from . import config, store, task as taskmod

ENV_FILE = config.env_file("curated-digest.env", "CURATED_DIGEST_ENV")


def _ping_url() -> str | None:
    if os.environ.get("CURATED_DIGEST_HC_PING_URL"):
        return os.environ["CURATED_DIGEST_HC_PING_URL"]
    try:
        for line in ENV_FILE.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "CURATED_DIGEST_HC_PING_URL":
                return v.strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _interval_seconds(schedule: str) -> int:
    p = taskmod.parse_schedule(schedule)
    return p["seconds"] if p["kind"] == "every" else 86400


def check(conn, tasks) -> dict:
    """Machine facts only. No model, no judgment."""
    store.ensure_schema(conn)
    intervals, source_intervals = {}, {}
    for t in tasks:
        try:
            intervals[t.id] = _interval_seconds(t.schedule)
        except Exception:
            continue        # a broken schedule is a task error, not a health fact
        for s in t.sources:
            # The most forgiving reader wins. A source read by both a 30m task
            # and a 6h one is only suspicious once even the slow reader should
            # have seen something; taking the fast one instead would trade a
            # few hours of detection lag for false alarms, and a false alarm
            # here costs more than a late true one (R16).
            source_intervals[s.id] = max(source_intervals.get(s.id, 0),
                                         intervals[t.id])
    return {
        "broken_sources": [dict(r) for r in store.broken_sources(conn)],
        "unchanged_sources": store.unchanged_sources(conn, source_intervals),
        "stale_tasks": store.stale_tasks(conn, intervals),
        "integrity": store.integrity(conn),
    }


def is_healthy(report: dict) -> bool:
    return (not report["broken_sources"]
            and not report.get("unchanged_sources")
            and not report["stale_tasks"]
            and report["integrity"] == "ok")


def ping(url=None, timeout=10) -> bool:
    """Only ever called when the report is clean. Pinging on a run that found
    problems would tell the watchdog everything is fine while it is not."""
    url = url or _ping_url()
    if not url:
        return False
    try:
        urllib.request.urlopen(url, timeout=timeout).read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        # A failed ping is not a curated-digest failure. The watchdog notices a missing
        # ping on its own — that is the entire design.
        return False


def _span(seconds: int) -> str:
    """Days once there is a day to report, hours before that. "0d" reads like a
    rounding error in an alert that is claiming a feed has gone quiet."""
    return f"{seconds // 86400}d" if seconds >= 86400 else f"{seconds // 3600}h"


def conditions(report: dict) -> list[tuple[str, str]]:
    """(stable_key, human_line). The key must not contain anything that changes
    run to run, or every tick looks like a brand new condition and the
    throttle never engages."""
    out = []
    if report["integrity"] != "ok":
        out.append(("integrity", f"store integrity: {report['integrity']}"))
    for s in report["broken_sources"]:
        out.append((f"source:{s['source_id']}",
                    f"{s['source_id']}: {s['consecutive_failures']} failed "
                    f"fetches — {(s.get('last_error') or '')[:80]}"))
    for s in report.get("unchanged_sources", []):
        # The key names the source only. Putting the duration in it would mint
        # a brand new condition every tick and the throttle would never engage.
        out.append((f"unchanged:{s['source_id']}",
                    f"{s['source_id']}: unchanged for {_span(s['unchanged_for'])} "
                    f"— may have stopped publishing"))
    for t in report["stale_tasks"]:
        out.append((f"stale:{t}", f"{t}: no output for 3x its schedule"))
    for e in report.get("broken_tasks", []):
        out.append((f"task:{e[:40]}", f"unloadable task: {e[:120]}"))
    return out


def render_alert(report: dict, throttle=None) -> str | None:
    """A template, not a model (R42b). This has to work when things are broken,
    which is exactly when a model call is least likely to succeed.

    `throttle` is a callable (key) -> (send, hours_broken). Without one every
    condition is reported, which is what the tests want and what a one-off
    `curated-digest health` should do.
    """
    lines = []
    for key, text in conditions(report):
        if throttle is None:
            lines.append(f"• {text}")
            continue
        send, hours = throttle(key)
        if send:
            lines.append(f"• {text}" + (f" (ongoing {hours}h)" if hours else ""))
    if not lines:
        return None
    return "\n".join(["curated-digest needs attention"] + lines)
