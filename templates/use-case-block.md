# use-case-block.md — UC block shapes (TEMPLATE)

Two copy-paste variants for authoring a use case along the traceability spine. Both key off
the same **opaque** `UC-NNNN` id (zero-padded monotonic, minted once in refine; digits carry
no domain meaning). The id is the only thing that must stay byte-identical across hops — a
single `grep UC-0147` walks refine → solution → commits → code → flow page.

- **Refine variant** — birth of a UC. One `### UC-NNNN` block per approved *functional*
  story in `.../design/specs/<topic>-design.md`. Carries the functional story, the human-gated
  `Domain:` path, and an initial `Status:`.
- **Solution variant** — doc of record. The `## Use Cases` section in
  `.../design/plans/<slug>.md`. Threads the *existing* id (never mints), carries the functional
  summary forward, adds the technical story, and names the owning tasks. This section is the
  single source of identity; the same ids are threaded flat into plan task frontmatter as
  `use_cases: ["UC-NNNN"]`.

The placeholder ids/paths/domains below are illustrative — replace them. Nothing here is a
literal to ship; it is a shape.

---

## Refine variant (functional story block)

Paste one block per minted UC into the refine design doc. `Domain:` is a path into
`design/use-cases/domains.md` (human-gated via asking-questions before write). `Status:` starts at
`backlog`.

```markdown
### UC-0000 — <short functional title>

**Functional story.** As a <role>, I want <capability> so that <outcome>. Written in
user-facing terms only — no components, no technical mechanism. This is the sentence a
non-engineer stakeholder would recognize.

- **Domain:** `<top>/<child>`   <!-- path in design/use-cases/domains.md; human-gated -->
- **Status:** backlog           <!-- backlog | in-progress (pre-ship, plan-derived) -->
```

---

## Solution variant (doc-of-record `## Use Cases` block)

Paste this section into the solution doc. One `### UC-NNNN` entry per UC the solution
realizes. Ids are threaded, never minted. Add the same ids to the relevant plan task
frontmatter as a flat scalar list (`use_cases: ["UC-0000"]`).

```markdown
## Use Cases

<!-- Doc of record: the single identity for each UC this solution realizes.
     Thread existing ids from refine — do NOT mint here. -->

### UC-0000 — <short functional title>

- **Functional summary.** <one-line restatement of the refine functional story, carried
  forward so this doc stands alone>.
- **Technical story.** <how it is realized: the components, data path, and mechanism —
  the engineering counterpart to the functional summary>.
- **Domain:** `<top>/<child>`   <!-- same path as refine; re-parenting edits the string only -->
- **Status:** in-progress       <!-- backlog | in-progress (pre-ship); completed | deprecated set post-ship by autodoc -->
- **Tasks:** `<task-id-a>`, `<task-id-b>`   <!-- owning plan tasks; mirror these ids into each task's use_cases frontmatter -->
```

---

## Notes (human-only)

- **Opaque ids.** `UC-NNNN` digits are allocator-assigned and meaningless — never encode a
  domain or ordering into them. Re-parenting a UC's domain edits the `Domain:` string and
  moves the node in `domains.md`; the id and every reference to it stay put.
- **Mint once.** Only refine calls the allocator. The solution and flow-page hops *thread*
  the id that already exists; a second mint would fork identity.
- **Status ownership is temporally disjoint.** Pre-ship (`backlog`/`in-progress`) is
  plan-derived from task-done state. Post-ship (`completed`/`deprecated`) is owned by
  autodoc-update reconciling against code — never hand-copied from the plan.
- **Flow-page hosting is separate.** The flow page carries `use_cases:` as an opaque scalar
  list (no `[[ ]]`) plus a `## Use Cases` table; that shape lives in the taxonomy, not here.
