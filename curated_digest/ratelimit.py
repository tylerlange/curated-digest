"""H8/H10/H11: telling a rate limit apart from a real failure, and working out
when the window reopens.

There is no distinct exit code for a rate limit, so detection is text matching
against an enumerated list. Everything here fails *safe*: an unrecognised
failure is a normal failure (retried, capped at 3), never an infinite wait.
"""
import json
import re
import time
from dataclasses import dataclass

from . import config

# The CLI emits a curly apostrophe; humans type a straight one.
_APOS = str.maketrans({"’": "'", "ʼ": "'"})


def _norm(text: str) -> str:
    return (text or "").translate(_APOS).lower()


@dataclass
class Verdict:
    is_rate_limit: bool
    kind: str | None = None          # five_hour | weekly | opus
    resume_after: int | None = None  # unix epoch
    source: str | None = None        # sidecar | text | fallback


def classify(text: str, cfg: dict | None = None, now: float | None = None) -> Verdict:
    cfg = cfg or config.load()
    rl = cfg["rate_limit"]
    now = now if now is not None else time.time()
    hay = _norm(text)

    # H8: API errors are a different class and win over any signature match.
    for marker in rl.get("not_rate_limit", []):
        if _norm(marker) in hay:
            return Verdict(False)

    for kind, sigs in rl["signatures"].items():
        for sig in sigs:
            if _norm(sig) in hay:
                at, src = _resume_at(hay, kind, rl, now)
                return Verdict(True, kind, at, src)
    return Verdict(False)


def _resume_at(hay: str, kind: str, rl: dict, now: float) -> tuple[int, str]:
    # Sidecar first: an exact epoch beats anything parsed out of prose.
    if kind == "five_hour":
        at = _from_sidecar(rl, now)
        if at:
            return at, "sidecar"
    at = _from_text(hay, now)
    if at:
        return at, "text"
    # H11: a weekly limit is not a short-window wait. Don't pretend an hour will do.
    if kind == "weekly":
        return int(now + 7 * 86400), "fallback"
    return int(now + rl["fallback_wait_seconds"]), "fallback"


def _from_sidecar(rl: dict, now: float) -> int | None:
    """`rate_limits.five_hour.resets_at` (unix epoch) as written by the
    statusline sidecar. Stale data is worse than none."""
    p = config.sidecar_path()
    try:
        raw = json.loads(p.read_text())
        if now - p.stat().st_mtime > rl["sidecar_max_age_seconds"]:
            return None
        at = raw["rate_limits"]["five_hour"]["resets_at"]
    except Exception:
        return None
    at = int(at)
    return at if at > now else None


_CLOCK = re.compile(r"reset(?:s)?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)


def _from_text(hay: str, now: float) -> int | None:
    """Parse `resets 3pm` / `reset at 10:30am` into the next such local time."""
    m = _CLOCK.search(hay)
    if not m:
        return None
    hour, minute, mer = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if mer:
        if hour == 12:
            hour = 0
        if mer.lower() == "pm":
            hour += 12
    if hour > 23 or minute > 59:
        return None
    lt = time.localtime(now)
    cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
    if cand <= now:
        cand += 86400
    # A parsed reset more than a window away means we misread it.
    return int(cand) if cand - now <= 6 * 3600 else None
