# gap-epic.md — drafted gap/enhancement epic (TEMPLATE)

The shape the **auditor** writes when a pass finds an epic's intent was not fully met. One
spec per coherent gap (cluster related shortfalls — never one spec per finding), written to:

```
<data_root>/projects/<name>/design/specs/YYYY-MM-DD-<slug>.md
```

It lands `current_state: drafted` and **never** more — the auditor is autonomous but never
approves its own work. Drafts accumulate in the dev-manager queue and the human batch-approves
them on their periodic pass. The `provenance` block is what makes a gap epic traceable back to
the audit that filed it.

The placeholders below are illustrative shapes, not literals to ship. Replace every `<...>`.

---

## Frontmatter + body

```markdown
---
title: "<short imperative title of the gap to close>"
date: YYYY-MM-DD
slug: <gap-slug>
project: <name>
status: drafted
current_state: drafted          # auditor writes ONLY this; never approved-for-autonomous
tags: []                        # add `regression` for a regressed-verdict gap (see below)
provenance:
  source_epic: <slug>           # the audited epic whose intent fell short
  audit_pass: YYYY-MM-DD        # the pass date; resolves to status/audits/YYYY-MM-DD-<pass>.md
  use_cases: ["UC-0147"]        # REUSED id when completing an existing intent (see UC continuity)
  evidence: <path>              # e.g. ../../status/audits/YYYY-MM-DD-<pass>.md#<source_epic>
---

# <title>

## Gap (one sentence)

<What intent is unmet: the use case / acceptance criterion that came back `partial`, `missed`,
or `regressed`, stated in user-facing terms.>

## Evidence

<What the audit observed vs. what the intent required. Point at the report section named in
`provenance.evidence` (screenshot path, probe output, grep hit). Do not restate a verdict
without its literal evidence.>

## Proposed scope

<The coherent unit of work that closes this gap. One gap epic per cluster of related
shortfalls, not per finding. Enough for refine to pick up; not a full solution plan.>

## Hand-offs

<OMIT this section entirely when the whole scope is the builder's own. Include it for any step
this epic's builder lane is not permitted to run -- see the convention below.>

- **owner:** <the actor the verb is granted to, from `registry read --capabilities`>
  **step:** <what to do, in the owner's vocabulary>
  **trigger:** <the observable event that makes it due -- not "as the last step of this PR">

## Use case continuity

<State ONE of:>
- Completing existing intent: reuse `UC-0147` (its status stays in-progress on the spine).
- New enhancement: mint a new UC in refine. The auditor never calls `allocate()`.

## Files

<REQUIRED -- a spec with no `## Files` section may not be promoted. One backticked
repo-relative path per line with a clause saying what changes there; mark new files `(new)`.>

- <path/to/file.py> -- <what changes there, in one clause>

<The placeholder line above is deliberately NOT backticked and must stay that way. Backticked
tokens are what `files_section` extracts as DECLARED PATHS, so a backticked placeholder left
unedited would report `seam_checkable=True` naming a file that does not exist -- and lead-dev
proves lane disjointness off exactly that list. Unbackticked, an unedited spec is honestly
present-but-pathless. Backtick your REAL entries.>

<When the placement decision IS this epic's scope, naming files would prejudge it. Say so in
one sentence and list the CANDIDATE surfaces instead, so the promoting seat can still judge
seam width. A true sentence and a candidate list beat an empty heading.>
```

---

## Provenance block (required)

Every field is what lets a human trust and trace the draft without re-running the audit:

| Field | Meaning |
|---|---|
| `source_epic` | Slug of the audited epic whose intent fell short. Ties the gap back to its origin. |
| `audit_pass` | ISO date of the pass. Resolves to `status/audits/<audit_pass>-<pass>.md` and the ledger row. |
| `use_cases` | The UC id(s) this gap concerns. **Reused** (not minted) when completing an existing intent — see below. A genuinely new enhancement leaves this to the new UC that refine will mint, and notes so in the body. |
| `evidence` | Relative path (ideally with `#<source_epic>` anchor) into the pass report section that justifies this draft. |

## UC continuity (the auditor NEVER allocates)

The traceability spine mints each `UC-NNNN` **once, in refine**. The auditor threads existing
ids; it never calls `allocate()`. Two cases:

- **Completing an existing intent** — the original use case is still only partially met, so its
  story is genuinely unfinished. **Reuse the original `UC-NNNN`** in `provenance.use_cases`. Its
  status stays `in-progress` on the spine (the flow page shows the story visibly incomplete);
  closing the gap finally moves it to `completed`. A second mint would fork identity for one
  story — never do it.
- **A genuinely new enhancement** — the audit surfaced desirable new behavior beyond the
  original intent. The auditor does **not** mint. It leaves `use_cases` empty (or omits it) and
  the body states "mint new UC in refine". Minting stays refine's job when the human approves
  the draft; only then does a new `UC-NNNN` come into being.

Rule of thumb: reuse when the gap **finishes** an existing story; defer-to-refine when the gap
**adds** a new one. When unsure, treat it as completing the existing intent (reuse) — over-
minting forks identity, under-minting is a cheap fix in refine.

## Hand-offs: a step the builder is not permitted to run

Some gaps close with a step only another seat may take — expiring a Directive (`dev-manager`),
culling a row (`steward`), publishing into a lane (`lead-dev`). **Never write that as an item of
`## Proposed scope`.** Scope is what the epic's verification is measured against, so an item its
own builder is forbidden to execute makes the criterion unmeetable by construction: the epic ships
`done` carrying a criterion it never could have satisfied.

This is the failure the convention prevents. An epic carries a scope item only `dev-manager` can
run, `lead-dev` builds it and routes the step correctly — and the step still does not happen,
because `lead-dev` believes it is dev-manager's (it routed) while dev-manager believes it is the
epic's (it deferred). Both are behaving correctly.

So put it under `## Hand-offs` instead, with all three fields:

- **owner** — the actor the verb is actually granted to. Read it, do not guess:
  `python -m agentflow.registry read --capabilities` prints every restricted verb with the actors
  that may run it, plus the prose spellings a spec is likely to use.
- **step** — what to do.
- **trigger** — the observable event that makes it due. This is the field that closes the deadlock,
  and the one most often left out. *"As the last step of the same PR"* is not a trigger another
  seat can act on; *"once `<slug>` has merged"* is.

The owner learns of it the normal way — `note --to <owner>`, or `raise-blocker --owner <owner>` if
the gap genuinely cannot close without it. Full convention: `docs/REGISTRY.md`, **Cross-actor
scope items**. `dev-manager` §7c reports any scope item that should have been a hand-off; it
reports and does not block, because a visible, assigned cross-actor step is fine.

## `regression` tag convention

A `regressed` verdict — a previously-`satisfied` UC that no longer holds — is the highest-
priority gap class. When any shortfall in the cluster is `regressed`:

- Add `regression` to `tags` (`tags: [regression]`).
- `provenance.use_cases` **reuses** the regressed UC's id (a regression is by definition an
  existing intent that broke — never a new UC).
- The evidence must show the before/after: the UC was satisfied at an earlier audit (ledger
  history) and now is not (this pass's report).
- The ledger row for `source_epic` carries a non-zero `regressed` count, and the notify line
  surfaces the regression count. These three signals (tag, ledger count, notify) let the human
  triage regressions first in the dev-manager queue.

## Invariants

- **`current_state: drafted` only.** The auditor writes drafted, autonomously and continuously.
  It never sets `approved-for-autonomous` and never blocks on approval. Approval is the human's
  job on the dev-manager pass.
- **One spec per coherent gap**, clustered — not one per finding.
- **A step the builder may not run is a `## Hand-offs` entry, never a scope item** — with an owner
  read off `read --capabilities` and a trigger another seat can act on.
- **A `## Files` section is mandatory** — a spec that does not name the files it will touch may
  not be promoted, so a draft written without one is unpromotable by construction and spends an
  attended promotion pass to discover it. When the placement decision is itself the scope, list
  the candidate surfaces and say why: candidate surfaces beat an empty heading.
- **Provenance is mandatory** — a gap epic with no `provenance` block is not auditor output and
  cannot be traced; every drafted gap carries `source_epic`, `audit_pass`, `use_cases` (reused
  or deferred), and `evidence`.
- **The auditor never allocates a UC.** Reuse an existing id or defer minting to refine.
