# Requirements

Numbered so code, tests and reviews can cite them. This is the second draft;
the first was scoped as a notifier and baked phone-shaped delivery assumptions
into the core.

## 1. What this is

**curated-digest is a scheduled pipeline that delivers curated content to the
right channel based on the need.**

```
sources → normalize → dedup/store → curate (LLM) → render → deliver
```

Four independently pluggable seams. Adding a source must not touch renderers. Adding a
renderer must not touch sources.

| Layer | v1 | Anticipated |
|---|---|---|
| Source | RSS/Atom, Reddit | a local database, market data, long-form news, web pages |
| Curate | relevance + ranking | summarization, grouping, cross-source synthesis |
| Render | Telegram digest | styled HTML daily edition, email |
| Deliver | Telegram | file sync to a reading device, ntfy, email |
| Model provider | local Claude (`claude -p`) | Gemini, other APIs, local models |

A styled print/e-reader edition and the Telegram digest are **two renderers over the
same curated content**, not two projects. This is the load-bearing architectural claim.

## 2. Non-goals

- **N1.** No bidirectional chat (D2). Messaging the system does nothing.
- **N2.** Single user. No accounts, no per-user state.
- **N3.** Not a general agent. It runs the tasks it is given.
- **N4.** No recommendations on money. Financial sources surface facts and deltas only.

## 3. Pipeline seams

- **R1.** Sources emit a **normalized item**: stable id, timestamp, title, body, url,
  source ref. Everything downstream consumes only this shape.
- **R2.** Adding a source type must not require changes to curation, rendering, or
  delivery code.
- **R3.** Renderers consume curated items and produce an artifact. A renderer must not
  fetch, judge, or deliver.
- **R4.** Delivery channels accept an artifact plus a destination. A channel must not
  know what produced the artifact.
- **R5.** Routing is declared per task: which renderer, which channel(s). The same
  curated content may go to more than one.
- **R5a.** **The model provider is an abstraction** (D21). A task declares a model;
  the runtime resolves it through a provider adapter that takes a prompt and returns
  text. Adding a provider means adding one adapter — no change to tasks, curation,
  rendering, or delivery.
- **R5b.** No provider-specific concept leaks past the adapter. Credentials are
  per-provider and scoped.

## 4. Tasks

- **R6.** A task is a declarative data file, validated against a schema (D9). Adding a
  task means adding a file — no code change, no new unit, no restart.
- **R7.** Every task declares: id, source, criteria, schedule, model, renderer, channel,
  urgency.
- **R8.** `model` is per-task and mandatory, expressed as `provider:model`. No implicit
  default — the default is where cost silently accumulates. A cheap relevance filter and
  an expensive synthesis step may name different providers.
- **R9.** A malformed task fails loudly and in isolation; other tasks still run.
- **R10.** Tasks are version-controlled alongside the code.

## 5. Store, dedup, state

The single most important area. Re-reporting the same item is the most visible way
this system can be wrong.

- **R11.** Durable per-task state survives restart and reboot.
- **R12.** Items are deduplicated by stable identity (GUID; fallback content hash).
  An item reported once is never reported again.
- **R13.** Ingested items are **retained**, not discarded after one use — a later
  renderer (a print edition) may need items an earlier one already saw.
- **R14.** State advances **only after delivery is confirmed**. A send failure leaves
  state untouched so the finding survives to the next run.
- **R15.** First run of a new task establishes a watermark and reports at most 10 items.
- **R16.** A condition that persists escalates rather than repeating identically or
  vanishing silently.
- **R17.** Loss or corruption of the store must be detectable, and recovery must not
  flood the channel.

## 6. Curation

- **R18.** Deterministic work first: fetch, dedup, then a non-model prefilter (age,
  score, keyword). **Only new candidates reach a model.**
- **R19.** The model performs only judgment a rule cannot express.
- **R19a.** Context a judgment needs — history, trends, comparisons — is gathered by the
  deterministic source/enrichment step and passed to curation as text (D14). Curation
  never queries for its own context; that is what keeps R20 achievable.
- **R20.** The curation step runs with **no tools, no filesystem access, no network**.
  Text in, text out. Anything with side effects is a separate deterministic step.
- **R21.** Curation must not load unrelated context. A relevance check must not pay for
  full user memory and instruction set per invocation. *(Live again under D21: driving
  `claude -p` loads instruction files and memory unless the invocation replaces the
  system prompt and disables memory, as the Claude provider does.)*
- **R22.** Analysis cost scales with new candidates, not feed size.

## 7. Delivery

- **R23.** Three delivery modes (D13), declared per task:
  - `digest` — batched into the next window. Default.
  - `prompt` — delivered on its own, within one tick, silent notification. For content
    that is timely but not an emergency (a summary of a new database row).
  - `urgent` — interrupts. Earned by the host being broken or an explicit flag.
- **R24.** *(removed — was an urgent-volume cap. Urgency is a property of the content;
  capping it by count would suppress real signal to satisfy a quota.)*
- **R25.** Urgent is delivered within one dispatcher tick; the tick interval is **≤ 10
  minutes** and stated in config.
- **R26.** Silence is the default and correct outcome (D7).
- **R27.** Telegram digest renderer: leads with items needing action, then items of
  interest. One phone screen. No tables. Self-contained lines. Each bullet links
  to the item it came from; a task-level footer link is single-source only.
  Tiers are capped, so the renderer reports which items it actually showed and
  only those are marked reported — an item goes in whole or waits (R14).
- **R28.** Formatting rules live in the renderer, never in the pipeline.

## 8. Security

Fetched content is the only untrusted input, and it flows into a model.

- **R29.** All fetched content is untrusted: sanitised and wrapped in explicit delimiters
  before reaching any prompt.
- **R30.** Instructions embedded in fetched content are data to report, never to follow.
  An injection attempt is itself a finding.
- **R31.** R20's lack of tools/network is the structural mitigation; prompt-level defences
  are assumed fallible.
- **R32.** No process handling fetched content has secrets in its environment.
- **R33.** Credentials live outside the repo, `0600`, referenced by name.
- **R34.** Filesystem access is confined by an enforcement layer that does not depend on
  model cooperation.

## 9. Reliability

- **R35.** One failing task must not stop the run.
- **R36.** A source that starts failing (404, format change) must be reported as broken,
  **not silently treated as empty**. *(Gap found in draft 1 review.)*
- **R37.** A task producing no output for **3× its own schedule interval** is flagged as
  possibly broken.
- **R38.** Repeated failure surfaces; silent permanent failure is the worst outcome.
- **R39.** Liveness is proven by an external watchdog on a different failure domain.
- **R40.** Must survive reboot; a missed run must not consume unreported findings.
- **R41.** Must not start before its network is ready.
- **R42.** The dispatcher must not share a runtime, process, or lock with any interactive
  agent session on the same box. *(Gap found in draft 1 review — the dominant failure
  mode in this product category.)*
- **R42a.** A task blocked by a provider rate limit must not advance state, must not
  lose findings, and must surface the condition — never fail silently (D22).
- **R42b.** **The urgent path must not depend on a model.** Machine facts are
  deterministic; the model only improves wording. With no provider available, urgent
  alerts compose from a template and still deliver (D22).
- **R42c.** On a subscription provider, curated-digest shares a quota with the operator's
  interactive sessions (D21). It must be pausable without editing code or units, so the
  operator can reclaim the window.

## 10. Cost

- **R43.** Per-task model declaration (R8) is the primary control. Runtime spend on an
  API provider is metered against the pipeline's own scoped key (D20), separate from
  any subscription usage.
- **R44.** A hard spend ceiling is enforced. **On reaching it, the system degrades to
  delivery-only and reports the ceiling as urgent — it must not silently stop**, which
  would violate R38. *(Contradiction found in draft 1 review.)*

## 11. Verifiability

Draft 1's lesson, kept: every check in a prior prototype tested for presence of
configuration, none tested behaviour, and three failures shipped looking correct.

- **R45.** One command asserts the system works, by **behaviour**: schedules armed and due
  in the future, a dry run produces output, the watchdog is armed, confinement denies a
  probe it should deny, a source failure is reported as failure. Never "a file exists."
- **R46.** Every task supports a dry run that produces output and writes no state.
- **R47.** Curation and formatting are covered by fixture tests so criteria and prompts
  can change without silent regression.
- **R48.** Injection resistance is a permanent regression test.
- **R49.** Facts the system relies on about live systems carry a verification command and
  a last-verified date; stale facts are flagged. *(Gap found in draft 1 review — a stale
  fact caused a real wrong answer.)*

## 12. Deferred

Market data, long-form news, arbitrary web pages; the print/e-reader renderer;
file-sync delivery; additional channels; natural-language task definition; any
interactive surface (D2, D24).

## 13. Open

See `DECISIONS.md` → Open (all resolved).
