# Build queue

Build tooling (see ../README.md). One item per harness unit.
`tools/build-loop/curated-digest-build` takes the first unchecked box,
does it, tests it, commits it, and ticks it. Add work here rather than to a
conversation that will end.

- [x] Add a `--model PROVIDER:MODEL` override to `curated-digest task run` so a task can
      be tested against a different model without editing its YAML. It must not
      persist anything and must be rejected unless it parses as `provider:model`
      (R8). Cover it with a test asserting the override actually reaches the
      provider, not just that the flag parses.
- [x] Split a bundled feed item into separate findings before curation. Some
      changelog feeds publish one entry per week containing four or five
      unrelated features, so the digest renders them as a single cryptic
      150-char bullet ("Fork mode is default; auto-continue limit retry"). That
      is the exact flat-and-unreadable failure the editorial spec exists to
      prevent. Curation should be able to emit more than one judgment per
      source item, each with its own tier and headline.
- [x] Add a retention sweep for `items`. R13 keeps items deliberately so a
      later renderer can reuse them, but nothing ever deletes them and the
      a busy multi-subreddit task can ingest ~150/day. Not urgent, but
      "grows forever" should be a decision rather than an oversight. Drop
      items older than six months that no task still has unreported.
- [x] Report a source that returns HTTP 304 for longer than 3x its schedule.
      A feed that is always "unchanged" is indistinguishable from one that has
      quietly stopped publishing, and R36's whole point is that broken and
      empty must not look alike.
