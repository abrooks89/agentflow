# report-format.md — per-pass audit report format (FORMAT)

Every audit pass writes **one report** capturing the full reasoning behind that pass's
verdicts — the evidence the terse ledger row only tallies. It lives at:

```
<data_root>/projects/<name>/status/audits/YYYY-MM-DD-<pass>.md
```

(`<data_root>` + `<name>` config tokens; `audits/` is a subdir of `status/`. `<pass>` is a
per-day discriminator — a monotonic index (`1`, `2`, ...) or a short label — so two passes on
the same date never collide.)

The ledger is the index; **this file is the detail.** Each epic's ledger `report` column links
into this file at the epic's section anchor. The report is written once per pass and not edited
afterward (like the ledger, it is a log).

---

## Purpose

- **Show the work** behind each verdict: what intent was reconstructed, how it was verified,
  and the literal evidence (screenshot path, probe output, grep hit) that grounds the call.
- **Back the provenance chain.** A drafted gap epic's `provenance.evidence` frontmatter points
  at a section of this file, so the human approving the draft can trace *why* it was filed.
- **Be the audit trail.** Read-only after the pass; a durable answer to "why did the auditor
  say this UC was only partial?" months later.

## File shape

A pass header (window, epic count, roll-up) followed by one `##` section per audited epic,
each with a per-UC / per-acceptance-criterion verdict table and an evidence list.

```markdown
# Audit Pass — <name> — YYYY-MM-DD (<pass>)

- **Window:** last N *unaudited* done epics (config `audit.window`; the ledger filter runs first)
- **Epics audited:** <count>
- **Roll-up:** <X> fully satisfied, <Y> with gaps, <Z> gap epics filed

<!-- One section per epic. The anchor (## <slug>) is the ledger `report` link target. -->

## <slug>

- **Intent source:** refine `<slug>-design.md` (UC blocks) + solution `## Use Cases` +
  flow-page UC status (`grep UC-#### <autodoc_root>`).
- **Verification strategy:** `web-journey` | `test-suite` | `smoke-cli` | `none`
  (from `verification.strategy`).
- **Role(s) driven:** `<persona>` (web-journey only; omitted otherwise).

| item | verdict | evidence |
|------|---------|----------|
| UC-0000 — <short title> | satisfied | reached via UI as `<role>`; `screenshots/<slug>-uc0000.png` |
| UC-0001 — <short title> | partial | flow renders but save silently no-ops; `screenshots/<slug>-uc0001.png` |
| AC: <acceptance criterion text> | missed | probe `<cmd>` exit 1; output below |
| UC-0002 — <short title> | regressed | previously satisfied; now 500s; `screenshots/<slug>-uc0002.png` |

**Evidence detail**

- `UC-0001` — <what was observed vs. what the intent required; one or two sentences>.
- `AC:<criterion>` — probe output (verbatim, fenced):

  ```
  <literal probe stdout/stderr — never paraphrased, never fabricated>
  ```

**Gap epics filed from this section:** `<gap-slug-a>` (UC-0001), `<gap-slug-b>` (UC-0002, `regression`).
```

## The verdict table

- One row per judged item: either a **UC** (`UC-NNNN — <title>`) or an **acceptance
  criterion** (`AC: <text>`, drawn from the solution task's `verification` block).
- `verdict` is exactly one of `satisfied | partial | missed | regressed` — the same four the
  subagents return and the ledger tallies.
- `evidence` is a **pointer to literal proof**, not a claim: a screenshot path under
  `audits/screenshots/`, a probe command + result, or a `grep UC-####` hit on a flow page.
  Hallucination discipline is inherited from the orchestrator — never write a green verdict
  without literal evidence, never fabricate output.

## Anchors & linkage

- The epic section heading is `## <slug>`, giving the anchor `#<slug>` that the ledger's
  `report` column links to (`audits/YYYY-MM-DD-<pass>.md#<slug>`).
- A drafted gap epic's `provenance.evidence` points at this file, ideally at the epic anchor
  (e.g. `../../status/audits/2026-01-01-1.md#<slug>`), so provenance resolves to the exact section
  that justified it.
- "Gap epics filed from this section" mirrors the ledger's `gap_epics` column at the level of
  which specific UC each draft addresses — the ledger says *which* drafts, the report says
  *which UC each one closes*.

## Evidence assets

Screenshots and other binary evidence live under `status/audits/screenshots/` (or a
`<pass>`-scoped subfolder). The report references them by relative path; it never inlines
binary. Text probe output is fenced inline (verbatim) so the report stands alone.

## Invariants

- **One report per pass**, written once, not edited after — corrections come from a re-audit.
- **Every audited epic gets a section**, even fully-satisfied ones (their table is all
  `satisfied`, "gap epics filed" reads `none`). This keeps the report a complete account of the
  pass and gives every ledger `report` link a target.
- **The report is detail, the ledger is index** — counts live in the ledger; reasoning and
  evidence live here. Keep them consistent: the section's verdict tally must equal that epic's
  ledger row counts.
