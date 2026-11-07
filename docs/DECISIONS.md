# Decisions

Product decision records, newest at the bottom. Each says what was decided,
why, and what would make it worth revisiting. Ids are stable because code
comments cite them; an entry that was withdrawn or moved keeps a one-line
stub. Overrule any of these in your fork; that is what the file is for.

---

### D1. Product framing
**curated-digest is a scheduled pipeline that delivers curated content to the
right channel based on the need.** Not a notifier, not a chatbot:
`sources → normalize → dedup/store → curate (LLM) → render → deliver`.
*2026-08-24*

### D2. No bidirectional chat
Messaging the system does nothing. Interactive control belongs to a separate
remote-control tool.
**Why:** removes the untrusted-inbound threat model, permission relay, approval
fatigue and an always-on daemon, which were most of the complexity and nearly
all the bugs in a prior chat-driven prototype.
**Revisit if:** no adequate remote-control tool is available and interactive
control from a phone becomes painful.
*2026-08-24*

### D3. (Withdrawn from the public log: operator-specific notes on retiring a prior prototype.)

### D4. Telegram digest is the first renderer
Build the Telegram digest before a styled print/e-reader edition.
**Why:** same pipeline, far less design surface. Proves ingestion and curation
are right before investing in typography.
**Not a scope reduction:** infrastructure is scoped for the long run.
*2026-08-24*

### D5. RSS/Atom is the first source
Reddit, market data, arbitrary web pages and long-form news are deferred, not
dropped. The source layer must anticipate them.
**Why RSS first:** stable GUIDs make dedup nearly free, no auth, no rate limits.
It proves the pipeline without fighting the fetcher.
*2026-08-24*

### D6. Batched delivery, urgent breaks out
Default is a batched digest. Urgent bypasses batching and is earned by (a) the
host itself being broken, or (b) an explicit per-task flag.
**Why:** most findings are not time-sensitive, and one message reads better than
a trickle. Urgency is a property of the content, so it is declared, not inferred.
*2026-08-24*

### D7. Silence is the default outcome
Nothing qualifying means no message. No heartbeat, no filler: a message with
nothing in it is noise to open. Liveness is proven by an external watchdog on a
different failure domain, not by chatter.
*2026-08-24*

### D8. Build, don't adopt a workflow platform
The dominant self-hosted stack for this is a workflow engine plus a local LLM
plus a push service, and `changedetection.io` (Apache-2.0) covers ~70% of a
*notifier* off the shelf.
**Rejected n8n** on dependency grounds, not licensing purity: it has relicensed
twice (Apache → Commons Clause → Sustainable Use License), the `.ee` carve-out
is a widening open-core wedge, and under SUL the community cannot fork if the
company turns. Permissive licensing is the actual protection.
**The buy case weakened once scope was correct:** dedup and batching are solved
for arbitrary web watching, much less so for RSS with stable GUIDs. Curation
and rendering, the actual product, are custom either way.
**Revisit if:** arbitrary web-page watching is added; adopt `changedetection.io`
alongside rather than growing a diff engine.
*2026-08-24*

### D9. Strict task schema, not free-form English
Tasks are declarative data files against a validated schema.
**Why:** silent drift, a task that quietly stops matching, is the failure an
operator will not notice. A schema makes it detectable.
*2026-08-24*

### D10. (Moved to tools/PLAYBOOK.md, lesson L6: a build-process decision.)

### D11. (Moved to tools/PLAYBOOK.md, lesson L5: a build-process decision.)

### D12. Event-like content is a frequently polled source
New rows in a local database (a training log, an expense ledger) are not a
special case. The database is a source, a new row is an item, dedup is the row
id. The "event" is a source polled often. No event-bus concept.
**Why:** validates the source abstraction instead of straining it, and forces a
non-RSS source early.
*2026-08-24*

### D13. Three delivery modes, not two
`digest` (batched, default) · `prompt` (sent on its own, promptly, silent
notification) · `urgent` (interrupt).
**Why:** a notice that should arrive shortly after an event, on its own, without
interrupting (a summary of a new database row, say) is neither batched nor an
emergency. D6's two modes had nowhere to put it.
*2026-08-24*

### D14. Enrichment happens in the source layer
Curation stays tool-free (R20). Anything needing data access ("3rd entry this
week, up from 1 last week") is gathered by the deterministic source/enrichment
step and passed to curation as text.
**Why:** keeps the structural injection mitigation without crippling analysis.
*2026-08-24*

### D15. Calibre rejected
Its recipe engine is a set of per-site scrapers from a pre-LLM era; an LLM with
feed/API access replaces it. Its EPUB compilation is solid but irrelevant once
the target is PDF. No compelling hybrid: it would add a dependency and a reflow
format that hurts the design goal.
*2026-08-24*

### D16. Fixed-layout PDF for the print/e-reader edition, not EPUB or HTML
Planned renderer: LLM curates → structured JSON → hand-crafted HTML/CSS
template → **Vivliostyle CLI** (CSS Paged Media) → fixed-layout PDF → file sync
to the reading device.
**Why not EPUB:** reflow destroys art direction; multi-column, pull quotes and
image placement do not survive it.
**Why not plain HTML:** on-device reader apps still reflow it; only fixed layout
renders as designed.
**Runner-up:** Typst. Much faster, excellent typography, but an LLM writes its DSL
less fluently than HTML/CSS, and magazine-style layout is more native to CSS
Paged Media.
*2026-08-24*

### D17. Template library with LLM slot-filling, not generative layout
Build 3–5 strong templates once (hero-with-image, two-column, sidebar-brief,
pull-quote feature). The LLM picks a variant per story and fills it.
**Why:** no verified production system did genuine per-issue AI layout as of
Aug 2026. Every self-hosted "AI newsletter" project found is fixed template plus
filled slots, and one kit's own docs say code-based templates produce
significantly better output.
*2026-08-24*

### D18. Design for paper-like colour displays as newsprint
Colour e-paper renders colour at a fraction of its monochrome resolution, with a
muted palette and unreliable reds. Treat the target as **newsprint**: spot-colour
accents, not full-bleed photography; high-contrast text; line art over photos;
no subtle gradients. Vector text in a PDF stays crisp in colour mode.
*2026-08-24*

### D19. Curation runs on Gemini, not Claude ~~(superseded by D21)~~
Reuse an existing Gemini API setup.
**Why this mattered:** it decoupled runtime cost from the subscription plan's
rolling usage window, so rate limits would only affect *building* the project,
not running it. R21 (no unrelated context per invocation) becomes trivial: a
plain API call carries no instruction files or memory.
**Consequence:** no coding-agent CLI at runtime; an ordinary program calling an
API on a schedule. Model routing (R8) still stands over Gemini model ids: a
cheap model for relevance, a stronger one for synthesis.
*2026-08-24*

### D20. The pipeline gets its own scoped Gemini key
Not the key any other tool (an existing chat UI, say) uses.
**Why:** the pipeline feeds untrusted scraped content to a model. A compromise or
a runaway loop must not be able to exhaust or implicate another tool's key, and
per-key billing makes this pipeline's spend legible against a monthly cap.
*2026-08-24*

### D21. The model provider is an abstraction; start on local Claude
Supersedes D19. Tasks declare a model; the runtime resolves it through a
provider adapter. Gemini is one provider, not the provider.
**Start on local Claude** via `claude setup-token` (a long-lived token that
drives `claude -p` in automation on a subscription plan), accepting that the
pipeline competes with interactive work for the same usage window.
**Why:** the abstraction is what lasts; the starting provider is not. Using what
exists avoids a second credential and billing path before the pipeline proves itself.
**Accepted cost:** quota contention is real and was flagged as the most likely
six-month failure. Accepted knowingly.
*2026-08-24*

### D22. Rate limits are a runtime concern again
With D21, rate limits return to the product:
- A task blocked by a rate limit must not advance state or lose findings (R14).
- Being limited must be *visible*, never silent.
- **Urgent alerts must not depend on a model.** "Disk 95%, backup failed" is
  deterministic; the model only adds polish. With no provider available, the
  urgent path composes from a template and still delivers.
*2026-08-24*

### D23. Build the rate-limit harness before the pipeline
The harness (durable work ledger plus resumable runner) comes first.
**Why:** long work spans several usage windows, and D21 put a runtime form of
the same problem in scope. Building it first makes every later unit resumable
and exercises the design before the product depends on it.
**Gap flagged at the time:** the requirements had no harness section, so it got
its own spec (docs/HARNESS.md) before it was built.
*2026-08-24*

### D24. Conversational follow-up is out of scope
curated-digest owns **proactive** delivery only. Asking questions of the
underlying data belongs to whatever interactive interface the operator already
uses.
**Why:** keeps the system one-directional (D2) and avoids re-opening the
interactive surface D2 removed.
*2026-08-24*

### D25. The harness runs unattended; resume is never a human action
Agree a plan, walk away, and it resumes itself when the window reopens. A human
is involved only when work is genuinely `stuck`, never when it is waiting.
**Defaults:** 3 retries, 24h stuck timeout.
**Constraint:** keep it tiny. The critique that harness-first delays proof of
value is accepted as a size limit, not a veto: a state machine over one SQLite
table plus a CLI. If it outgrows a few hundred lines, the critique was right.
*2026-08-24*

### D26. Spend ceiling $10 per harness run
A subscription may have usage credits enabled; if so, an unattended loop could
consume them silently. $10 guards against that and is not an expected cost:
without credits the usage window is the real bound and the ceiling never engages.
*2026-08-24*

### D27. Parse RSS with the standard library, not feedparser
**Design goal:** stdlib-only, so it runs on a locked-down host with no package
manager, no virtualenv and no root, and an unattended build never blocks on a
manual install. RSS/Atom parsing uses `xml.etree` plus `email.utils`.
This is not only making the best of it. The RSS parser is the one component that
ingests hostile internet content in-process, and ~200 lines we control are a
smaller attack surface than 10k lines of third-party XML handling. R36 already
requires an unparseable feed to be reported as **broken**, not silently empty,
so the long tail of malformed feeds that feedparser rescues becomes a visible,
actionable report instead of a silent difference.
**Revisit if:** real feeds fail to parse because of our bug rather than theirs.
*2026-08-24*

### D28. Curation runs on Gemini; Claude keeps the trusted-input work
Split by *trust of input*, not by cost or quality.
The Claude adapter is locked down (no tools, no MCP, manual permissions), so an
injected instruction reaches a model that cannot invoke anything. But that
boundary is made of **flags**. A CLI upgrade that changes a default, a task that
adds `--add-dir`, or one wrong edit moves it, and the process underneath still
runs as the operator's user with their files and credentials.
A Gemini call is an HTTPS POST. There is no tool layer to misconfigure, so the
boundary is structural. Full control of the model's output yields text, and
text naming an item id we never sent is discarded.
So: anything touching raw fetched content goes to Gemini. Anything reading the
operator's own data may use either. Secondary benefit: the scoped key does not
share the subscription's usage window.
*Caveat at the time of writing:* the Gemini adapter was written and tested with
mocks only; the Claude path was verified live.
**Revisit if:** Gemini quality proves inadequate for relevance judgment; the
honest fix is then a scoped second Claude identity, not relaxing R20.
*2026-08-24*

### D29. Items are kept six months, and only once nothing needs them
R13 retains items on purpose, but nothing deleted them, and a busy
multi-subreddit task can ingest ~150 a day. The tick now sweeps items older than
**180 days**, and only those every task reading that source has settled.
**Age alone would have been wrong.** A task rate-limited or broken for a week
still owns an undelivered backlog; deleting it would break R14 from the cleanup
side, the one place the rule is easy to forget.
**`reported` rows are not swept.** They become tombstones: a feed that still
lists a six-month-old entry would otherwise be re-ingested as new and sent
again (R12). Three columns per item is the price; item bodies are what grow.
**Revisit if:** a renderer needs to look back further (the print edition is the
plausible one), or tombstones rather than bodies become the cost.
*2026-08-24*

### D30. A source stuck on 304 is judged against its own cadence, not the poll rate
R36 says broken must not look like empty. The loud half (404, parse error,
timeout) was covered. The quiet half was not: a retired feed whose server keeps
answering **304** reads as "nothing new" at every layer, and the pipeline stays
correctly silent (D7) forever. `last_change_at` now moves only on a real body.
**The threshold is not 3x the task's schedule.** A changelog that is polled every
6h and publishes weekly is quiet for three poll intervals on an ordinary day.
Alerting on that would fire five days in seven, suppress the watchdog ping for
those days (turning R39's liveness signal into a permanent false alarm), and
train the reader to mute the channel that also carries findings (R16). So the
baseline is the feed's **observed** change cadence, floored at the poll interval
and estimated from distinct `fetched_at` values, and 3x applies to that.
**Deliberately blind:** a feed with no ETag or Last-Modified never returns 304,
so it is out of scope; a source already counted broken is not reported twice.
**Revisit if:** healthy feeds get reported (distrust the estimator first), or a
cadence change makes old history dominate the estimate.
*2026-08-24*

### D31. (Moved to tools/PLAYBOOK.md, lesson L1: a build-process measurement.)

---

## Open

- ~~**O1** Repo name.~~ **Resolved: `curated-digest`.**
- ~~**O2** Calibre.~~ **Resolved: rejected (D15).**
- ~~**O3** Print/e-reader edition format.~~ **Resolved: fixed-layout PDF via Vivliostyle (D16).**
- ~~**O4** Digest cadence and time.~~ **Resolved: there is no global cadence.**
  R7 makes `schedule` a per-task field; a changelog watch and a database
  summary have no business sharing one clock.
- ~~**O5** Rate-limit harness sequencing.~~ **Resolved: harness first (D23).**
- ~~**O6** Conversational surface.~~ **Resolved: out of scope (D24).**
