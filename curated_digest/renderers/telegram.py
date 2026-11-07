"""R27/R28: the Telegram digest.

The editorial rule is layering by actionability, not brevity for its own sake.
The first version of this failed by being flat — forty lines with no signal
about what to act on. A five-bullet version failed the opposite way, throwing
away the interesting-but-not-urgent tier. Structure fixes both.

All formatting rules live here. The pipeline knows nothing about phones.
"""
import html
import re

from . import Artifact

MAX_ACT = 4
MAX_FYI = 5
LINE_CHARS = 150
BULLET = "• "

# Only these get turned into an anchor. Item urls come from feed and API data,
# which is untrusted input like everything else a source hands us.
SAFE_URL = re.compile(r"^https?://", re.I)


def _text(item, judgment) -> str:
    """The curator writes the sentence; this only assembles it.

    Falling back to "title — why" is what produced lines like "Fork mode is
    default" — a feature name with no consequence attached, which is useless to
    a reader who does not already know the term. The fallback stays because a
    missing line must not lose the item, but the curator supplying `line` is
    the intended path.
    """
    if getattr(judgment, "line", ""):
        return judgment.line.strip()
    why = (judgment.why or "").strip().rstrip(".")
    line = item.title.strip()
    if why:
        line += f" — {why}"
    # LINE_CHARS is the whole visible line, "• " included.
    budget = LINE_CHARS - len(BULLET)
    if len(line) > budget:
        line = line[:budget - 1].rstrip() + "…"
    return line


def _bullet(item, judgment) -> str:
    """One bullet, linked to the item it came from.

    A digest spanning several sources cannot be footnoted with a single link:
    doing that shipped Pixel tips under a link to r/steammachine for a week.
    The link belongs to the finding, so it is attached to the finding. Truncate
    before escaping — LINE_CHARS is a budget for what the reader reads, not for
    `&amp;`.
    """
    body = html.escape(_text(item, judgment))
    url = getattr(item, "url", "") or ""
    if SAFE_URL.match(url):
        return f'{BULLET}<a href="{html.escape(url, quote=True)}">{body}</a>'
    return f"{BULLET}{body}"


def _select(kept, items_by_id):
    """Which findings this digest can carry, and the items it fully presents.

    A judgment is a finding, so a bundled item that yields three changes gets
    three bullets, each in whichever tier it earned on its own. The caps are
    what make it one phone screen (R27).

    Selection is per item, not per finding, because "already reported" is
    recorded against an item: a digest that shows three of an item's four
    findings and lets the caller mark it reported loses the fourth for good.
    So an item goes in whole or waits for the next run — the same rule R14
    applies at the delivery seam, applied at the render seam.

    Presence is checked before the cap: a judgment naming an item we no longer
    have renders nothing, so letting it consume a slot costs a real finding
    its place.
    """
    groups, order = {}, []
    for j in kept:
        if j.item_id not in items_by_id:
            continue
        if j.item_id not in groups:
            order.append(j.item_id)
            groups[j.item_id] = []
        groups[j.item_id].append(j)

    act, fyi = [], []
    for iid in order:
        want_act = [j for j in groups[iid] if j.tier == "act"]
        want_fyi = [j for j in groups[iid] if j.tier == "fyi"]
        if (len(act) + len(want_act) <= MAX_ACT
                and len(fyi) + len(want_fyi) <= MAX_FYI):
            act += want_act
            fyi += want_fyi
        elif not act and not fyi:
            # One item larger than the whole digest. Holding it back starves it
            # — it will not fit next run either — so it is shown truncated.
            # This is the only case where the cap costs a finding, and it costs
            # it visibly, with the rest of the item alongside it.
            act, fyi = want_act[:MAX_ACT], want_fyi[:MAX_FYI]

    # dict.fromkeys, not a set: several findings can name one item, and the
    # caller reports it once, in the order the digest presented it.
    return act, fyi, tuple(dict.fromkeys(j.item_id for j in act + fyi))


def render(judgments, items_by_id, *, heading, link=None,
           broken_sources=()) -> Artifact | None:
    """Returns None when there is nothing worth sending.

    R26: silence is the default and correct outcome. Returning an empty digest
    would train the reader to ignore the channel, so the renderer refuses to produce
    one and delivery never happens.
    """
    kept = [j for j in judgments if j.keep]
    # Items, not judgments. One bundled item that yields three findings is still
    # one item that contained text addressed to the filter, and that count is the
    # entire information content of the warning line.
    injected_items = {j.item_id for j in judgments if j.injection}

    act, fyi, rendered = _select(kept, items_by_id)

    if not act and not fyi and not injected_items and not broken_sources:
        return None

    parts = [html.escape(heading)]

    if act:
        parts.append("\nAct")
        parts += [_bullet(items_by_id[j.item_id], j) for j in act]
    if fyi:
        parts.append("\nWorth knowing")
        parts += [_bullet(items_by_id[j.item_id], j) for j in fyi]

    # R36/R38: a source that has stopped working is reported. Silence from a
    # broken feed is indistinguishable from a quiet one, and that is the worst
    # failure this system has.
    if broken_sources:
        parts.append("\nNot working")
        for s in broken_sources:
            parts.append(f"• {html.escape(str(s['source_id']))} — "
                         f"{s['consecutive_failures']} failed fetches")

    # R30: an injection attempt is a finding, reported rather than swallowed.
    if injected_items:
        parts.append(f"\n⚠ {len(injected_items)} item(s) contained text addressed "
                     f"to the filter. Judged on subject matter, not obeyed.")

    # Still meaningful for a single-source task ("go read the changelog");
    # task.py refuses it on a multi-source one, which is the actual bug guard.
    if link:
        parts.append(f"\n→ {html.escape(link)}")

    return Artifact("\n".join(parts), kind="html", title=heading,
                    rendered_ids=rendered)
