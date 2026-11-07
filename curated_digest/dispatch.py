"""One task, end to end: source -> store -> curate -> render -> deliver.

This is where the seams meet, so it is also where the ordering rules matter
most. Two in particular:

  - state advances only after a send is confirmed (R14), so a Telegram outage
    costs a delay rather than a permanently lost finding
  - a source that has broken is reported, never silently treated as empty (R36)
"""
import time

from . import store
from .channels import telegram as tg_channel
from .curate import curate
from .providers import RateLimited          # noqa: F401  (callers catch it)
from .renderers import telegram as tg_renderer
from .sources import reddit, rss
from .sources.base import SourceError

# Reddit returns 429 for requests a second apart. Cheap insurance: a digest
# spanning several feeds is not in a hurry.
SOURCE_GAP_SECONDS = 5

# R2: a source type is a registry entry. Adding one must not reach into
# curation, rendering, or delivery — they only ever see the normalized Item.
SOURCE_TYPES = {"rss": rss, "reddit": reddit}

RENDERERS = {"telegram": tg_renderer}
CHANNELS = {"telegram": tg_channel}


def run_task(conn, task, *, dry_run=False, now=None) -> dict:
    """Returns a summary dict. Raises RateLimited so the harness can yield the
    unit without consuming a retry — a quota wall is not a task failure."""
    now = now or time.time()
    store.ensure_schema(conn)

    if dry_run:
        # R46: a dry run produces real output and writes no state. Doing that
        # with a transaction rather than a flag threaded through every write
        # means there is no code path that can forget to honour it.
        conn.execute("BEGIN")
    try:
        result = _run(conn, task, dry_run, now)
    finally:
        if dry_run:
            conn.execute("ROLLBACK")
    return result


def _run(conn, task, dry_run, now) -> dict:
    out = {"task": task.id, "dry_run": dry_run, "new": 0, "judged": 0,
           "kept": 0, "reported": 0, "held_back": 0, "delivered": False,
           "outcome": "ok"}

    # --- sources ----------------------------------------------------------
    # One failing source must not cost the digest the others (R35). Each is
    # fetched, recorded and reported independently; only if every source fails
    # does the run have nothing to work with.
    out["sources"] = {}
    for i, src in enumerate(task.sources):
        if i:
            # Reddit 429s on back-to-back requests. Pacing here rather than in
            # the RSS module keeps politeness a property of running a task,
            # not of the parser.
            time.sleep(SOURCE_GAP_SECONDS)
        out["sources"][src.id] = _fetch_source(conn, src, out)

    if all(v == "failed" for v in out["sources"].values()):
        out["outcome"] = "source_failed"
        return _maybe_report_broken(conn, task, out, dry_run)

    # --- candidates + prefilter ------------------------------------------
    from .curate import prefilter
    rows = store.candidates(conn, task.id, [s.id for s in task.sources])
    candidates = prefilter(
        [_Row(r) for r in rows], max_age_days=task.max_age_days,
        include=list(task.include) or None, exclude=list(task.exclude) or None,
        now=now)

    # Reported so an empty run explains itself: "nothing new" and "everything
    # was older than max_age_days" look identical otherwise.
    out["unreported"] = len(rows)
    out["after_prefilter"] = len(candidates)
    if not candidates:
        return _maybe_report_broken(conn, task, out, dry_run)

    # R19a: enrichment is deterministic and happens here, not inside curation.
    # Curation stays text-in/text-out with no network, which is what makes R20
    # achievable at all.
    candidates = _enrich(task, candidates)

    # --- curate -----------------------------------------------------------
    kept, judged, cost = curate(candidates, task.criteria, task.model)
    out.update(judged=len(judged), kept=len(kept), cost_usd=cost)

    by_id = {c.id: c for c in candidates}
    rows_by_id = {r["item_id"]: r for r in rows}

    # Rejected items are settled now; nothing can be lost by recording them,
    # and not recording them means paying to re-judge them forever (R22).
    # Rejection is a property of the item, not of one judgment: a bundled
    # release note yields several findings and may well keep one and drop two.
    # Marking it judged on the first `keep: false` writes it off before the kept
    # finding has been delivered, bypassing R14 for exactly the item it protects.
    kept_ids = {j.item_id for j in kept}
    rejected = [rows_by_id[iid] for iid in dict.fromkeys(j.item_id for j in judged)
                if iid not in kept_ids and iid in rows_by_id]
    if rejected:
        store.mark_judged(conn, task.id, rejected)

    # --- render + deliver -------------------------------------------------
    renderer = RENDERERS[task.renderer]
    artifact = renderer.render(
        judged, by_id, heading=_heading(task, now), link=task.link,
        broken_sources=store.broken_sources(conn))

    if artifact is None:
        out["outcome"] = "silent"        # R26: the normal, correct outcome
        return out
    out["artifact"] = artifact.body

    if dry_run:
        out["outcome"] = "dry_run"
        return out

    delivered = CHANNELS[task.channel].send(artifact)
    out["delivered"] = bool(delivered)

    # R14. This is the load-bearing line in the file: only a confirmed send
    # advances state. If send() raised or returned False we never get here, and
    # the finding survives to the next run.
    if delivered:
        # What the renderer *presented*, not what the curator kept. The digest
        # caps its tiers, so a run that keeps seven findings may show five —
        # marking all seven reported writes the other two off unseen, which is
        # the R14 failure moved from the delivery seam to the render seam. The
        # held-back items stay unreported and compete again next run; the cost
        # is re-judging them, and the alternative is losing them.
        reported = [rows_by_id[iid] for iid in artifact.rendered_ids
                    if iid in rows_by_id]
        store.mark_reported(conn, task.id, reported)
        out["reported"] = len(reported)
        out["held_back"] = len({j.item_id for j in kept}) - len(reported)
    else:
        out["outcome"] = "delivery_failed"
    return out


def _enrich(task, candidates):
    """Ask each source type to fill in context its listing format leaves out.
    Optional by design — a source without `enrich` needs nothing."""
    by_source = {s.id: SOURCE_TYPES[s.type] for s in task.sources}
    grouped, order = {}, []
    for c in candidates:
        grouped.setdefault(c.source_id, []).append(c)
        order.append(c.id)
    enriched = {}
    for sid, group in grouped.items():
        impl = by_source.get(sid)
        fn = getattr(impl, "enrich", None)
        try:
            result = fn(group) if fn else group
        except Exception:
            result = group          # never let enrichment cost us the digest
        for item in result:
            enriched[item.id] = item
    return [enriched[i] for i in order if i in enriched]


def _fetch_source(conn, src, out) -> str:
    impl = SOURCE_TYPES[src.type]
    headers = store.conditional_headers(conn, src.id)
    try:
        raw, resp = impl.fetch(src.url, etag=headers.get("etag"),
                               modified=headers.get("modified"))
    except SourceError as e:
        store.record_failure(conn, src.id, e.reason)
        return "failed"

    if raw is None:
        # 304: unchanged. Cheapest possible outcome, unambiguous success — but
        # NOT evidence the feed is still alive, so it must not touch the clock
        # `unchanged_sources` reads. A feed that answers 304 forever is exactly
        # what a feed that quietly stopped publishing looks like (R36).
        store.record_success(conn, src.id, changed=False)
        return "unchanged"
    try:
        items = impl.parse(raw, src.id)
    except SourceError as e:
        store.record_failure(conn, src.id, e.reason)
        return "failed"
    store.record_success(conn, src.id, etag=resp.get("etag"),
                         last_modified=resp.get("last-modified"))
    out["new"] += store.ingest(conn, items)
    return "ok"


def _maybe_report_broken(conn, task, out, dry_run) -> dict:
    """R36/R38: a feed that has failed repeatedly is worth interrupting for,
    because from the outside a broken source and a quiet one look identical."""
    broken = store.broken_sources(conn)
    if not broken:
        out.setdefault("outcome", "ok")
        return out
    artifact = RENDERERS[task.renderer].render(
        [], {}, heading=_heading(task, time.time()), broken_sources=broken)
    if artifact is None or dry_run:
        return out
    try:
        out["delivered"] = CHANNELS[task.channel].send(artifact)
    except Exception as e:                      # never let the alarm be fatal
        out["report_error"] = str(e)[:200]
    return out


def _heading(task, now) -> str:
    return task.heading.replace("{date}", time.strftime("%b %-d", time.localtime(now)))


class _Row:
    """Adapts a stored row to the attribute shape curation expects, so the
    curator never learns that a store exists."""

    __slots__ = ("id", "source_id", "url", "title", "body", "published_at",
                 "fetched_at")

    def __init__(self, r):
        self.id = r["item_id"]
        self.source_id = r["source_id"]
        self.url = r["url"]
        self.title = r["title"] or ""
        self.body = r["body"] or ""
        self.published_at = r["published_at"]
        self.fetched_at = r["fetched_at"]
