# ledger-format.md — audit ledger row format (FORMAT)

The **audit ledger** is the auditor's durable record of which epics it has judged and how
they scored. It lives at:

```
<data_root>/projects/<name>/status/audit-ledger.md
```

(config tokens, never a literal path — `<data_root>` from `environment.*`, `<name>` the
project routing key; see `<suite_root>/docs/DATA-LAYOUT.md`).

It has **two jobs at once**:

1. **The record** — one row per audited epic: what was judged, when, the verdict tally, and
   which gap epics the pass filed as a result.
2. **The already-audited filter** — the audit pass reads this file first and skips any epic
   whose slug already appears here. This is the sole dedupe mechanism; there is no separate
   "seen" state. An epic is re-judged only under an explicit `--re-audit <slug>` override.

Because it is the filter, the ledger must be **append-only during a pass** and every audited
epic **must** get exactly one row before the pass reports done — a missing row means the epic
is re-audited next wake (wasted work), a duplicate row is harmless but noise.

---

## File shape

A stable header block plus one Markdown table. Newest rows appended at the bottom. The table
is the machine-and-human contract; nothing else in the file is parsed.

```markdown
# Audit Ledger — <name>

<!-- Append-only. One row per audited epic. This table is ALSO the already-audited filter:
     the audit pass skips any slug that appears in the `epic` column. Re-audit only via
     `--re-audit <slug>`. Do not edit or reorder existing rows. -->

| epic | audited | satisfied | partial | missed | regressed | gap_epics | report |
|------|---------|-----------|---------|--------|-----------|----------|--------|
| <slug> | YYYY-MM-DD | 4 | 1 | 0 | 0 | - | [report](audits/YYYY-MM-DD-<pass>.md#<slug>) |
| <slug> | YYYY-MM-DD | 2 | 0 | 1 | 1 | `<gap-slug-a>`, `<gap-slug-b>` | [report](audits/YYYY-MM-DD-<pass>.md#<slug>) |
```

## Columns

| Column | Meaning |
|---|---|
| `epic` | The audited epic's slug (identity key; filename stem with any `YYYY-MM-DD-` prefix and `-design` suffix stripped). **This column is the dedupe key.** |
| `audited` | ISO date `YYYY-MM-DD` of the pass that produced this row. |
| `satisfied` | Count of UCs / acceptance criteria judged `satisfied`. |
| `partial` | Count judged `partial`. |
| `missed` | Count judged `missed`. |
| `regressed` | Count judged `regressed` (a previously-`satisfied` UC that no longer holds). |
| `gap_epics` | Comma-separated backtick-quoted slugs of the drafted gap epics this row's shortfalls produced, or `-` if none (a fully-satisfied epic files no gap and shows `-`). |
| `report` | Relative link into the per-pass report section for this epic (see `report-format.md`). |

The four verdict counts are the same four verdict values a subagent returns per UC
(`satisfied | partial | missed | regressed`); the row tallies them across the epic's UCs and
acceptance criteria. Their sum equals the number of judged items for that epic.

## Invariants

- **Slug uniqueness is the filter.** Before judging, load this file and collect the `epic`
  column into a set; any surveyed done-epic already in the set is dropped from the pass. This
  is step 1 of the audit pass ("filter out already-audited").
- **One row, written once, per audited epic.** Rows are never edited or reordered after the
  fact — the ledger is a log, not a state table. Corrections happen by re-auditing.
- **`gap_epics` points forward, `report` points sideways.** `gap_epics` names specs that now
  live in `design/specs/` (dev-manager sees them there natively). `report` links the evidence in the
  same pass's report file.
- **A satisfied epic still gets a row** (with `-` in `gap_epics`) — the ledger records that it
  was judged, which is what keeps it out of the next pass.
- **Regressed rows are the priority signal.** A non-zero `regressed` count means at least one
  gap epic in `gap_epics` carries the `regression` tag (see `<suite_root>/templates/gap-epic.md`); those are
  the highest-priority drafts in the dev-manager queue.

## Concurrency

The ledger is written by the single auditor loop, so it is not multi-writer contended the way
the fleet board is. Still, wrap the append under the project `lock_file` mkdir-lock if a
`--once` invocation could overlap a scheduled wake, and append whole rows (never rewrite the
table) so a torn write can only ever lose a not-yet-committed row, not corrupt prior history.
