---
name: wiki-save
description: Write ONE durable page into the project's `wiki/` pillar at the end of a firing that produced something durable — a lesson learned or a decision made. Fires at four phase boundaries: end-of-epic, park, halt, block. A lane duty for five seats — `lead-dev`, `jr-dev-1`, `jr-dev-2`, `jr-dev-3` and `dev-manager` — and a human command in any interactive seat (`/wiki-save [--project <name>] [what to save]`). Writes through `agentflow.wiki` — `write_page`, then `refresh_projection` — never by hand-editing a hot file. A firing with nothing durable writes nothing.
---

# save — the `wiki/` pillar's writer

## Overview

A project's data root has four pillars and the axis that separates them is AUTHORSHIP: `design/`
is what we INTEND and a human writes it, `autodoc/` is what the code IS and is generated from the
code, `status/` is what the fleet is DOING and the loops write it, and `wiki/` is what we LEARNED —
**written by the sessions.** This skill is that writer. Nothing else in the suite creates a page.

The storage half of the pillar already exists and is not this skill's business: `pages/` is
create-only, `hot.md` and `hot-<lane>.md` are projections regenerated from it, and `digest.md`
belongs to the steward. What this skill adds is the thing that decides **when a page is owed and
what goes in it.** A store with no writer accumulates nothing; a backfilled corpus with no ongoing
writer starts going stale the day it lands.

## When NOT to use this

- **A vault `/save` skill, if you have one, is a different skill.** This one is named
  `wiki-save` so it never collides with a `/save` a user already has. A vault `/save` files a
  conversation into a human's Obsidian vault. This one writes one agentflow page into one
  project's `wiki/pages/`. This skill does not replace, wrap, or port it, and a seat that means
  the vault should follow the vault's own SKILL.md by path rather than by command name.
- **Nothing durable happened.** See "A firing with nothing durable writes nothing" below. This is
  the common case and it is not a failure.
- **The fact belongs on the board, not in the wiki.** A state change (an epic parked, a lane
  claimed) is a registry write. A page records what was LEARNED, which the registry does not hold.

## When it fires — four phase boundaries, each one on its own

A page is written at a **phase boundary**, never mid-work: the boundary is the only moment the
seat knows how the reasoning came out. All four rows below are boundaries. Three of them are the
ones a narrower completed-epics-only rule throws away.

| Trigger | The boundary | Why this one specifically |
|---|---|---|
| **end-of-epic** | the epic's done-gate passed and its PR merged | the arc is complete, so the lesson is stated against a known outcome rather than a guess |
| **park** | the item is released back with `--state parked` | **the reasoning is freshest and most perishable here.** A park is a seat stopping *mid-thought*; the next seat to touch the item inherits the board row and none of the thinking. A parking note is often the most useful thing a stopped seat leaves behind, and an end-of-epic-only rule never writes one |
| **halt** | the lane declares `halted` against a live question id | a halt says *this cannot proceed without an answer*. Why it cannot is the durable part, and it is the part that evaporates when the question is finally answered and everyone moves on |
| **block** | a `## Blockers` row is raised or an owned blocker is found unclearable | a blocker that is later cleared leaves no record of what it cost or how it was diagnosed unless someone writes one at the moment it bites |

**Each row is its own trigger and none of them implies the others.** A disjunction ("fires at
end-of-epic or when work stops") reads as complete and lets three of the four be deleted silently,
because any one surviving alternative keeps the sentence true. They are gated per-trigger for
exactly that reason.

The last three are required, not extras: a completed-epics-only rule loses the reasoning at
exactly the three moments work stops. Do not narrow them back.

### dev-manager fires on DIFFERENT moments, and the reason is structural

The four rows above are an **epic build's** boundaries. `dev-manager` has none of them: it does
not self-wake, it builds no epic, and its firing is a human conversation that ends when the human
closes the seat. Forcing *end-of-epic / park / halt / block* onto it would produce a duty that
never fires, or one that fires on a boundary the seat does not have — which is worse, because it
reads as coverage.

Its durable moments are its own, and they are the four things that leave the fleet different than
they found it:

| dev-manager moment | `trigger=` to record it under |
|---|---|
| a question answered under delegated authority | `halt` — the question-shaped boundary. A lane halts against a live `Q-` id; this is that boundary's other end |
| a Directive transcribed from the human | `park` — the perishable-reasoning boundary. A Directive is human reasoning that arrived in one seat and must reach seats that were never in the room |
| an epic promoted, split or carved | `end-of-epic` — the refinement arc completing, which is this seat's equivalent of a done-gate |
| a blocker disposed (`owner: dev-manager`) | `block` |

**The right-hand column is a mapping, not a renaming.** `compose_and_save` REFUSES a trigger
outside `save_summariser.TRIGGERS` and returns a `failed` outcome, so a seat that invents a fifth
value writes nothing and only an audit line says why. The page body carries the moment in its own
words; the `trigger=` value exists so the audit record groups.

## What a page IS — the unit

**One page per durable lesson or decision, written at the end of the firing that produced it.**

- A firing that learned nothing durable **writes nothing.** That is the expected outcome of most
  firings and it is not a miss.
- A firing that learned two separable things writes **two pages**, not one page with two headings.
  The projection ranks pages, so a bundle ranks as one thing and half of it is invisible.
- `pages/` therefore grows **per OUTCOME, never per wake.** A per-wake unit would rank the hot
  tier by how often a lane happened to wake rather than by what was learned.

**Durable** means: it will still be true, and still be worth reading, on a firing that has none of
this firing's context. A rerun command, a transient CI colour, or a restatement of what the board
already says is not durable. A measured incident, a decision and the alternative it beat, a
mechanism that behaves differently from how it reads, or a rule with the failure that produced it —
those are.

Every page carries at least one `[[wikilink]]`. This is not advice: `write_page` **refuses** a body
without one. Retrofitting links onto a corpus later is a migration; writing them as you go is free.

## What may be read

- **The seat's own context** — what it did this firing, and what it concluded.
- **The artefacts it produced this firing** — its PR, its state file, notes it wrote, the diff it
  landed.

And nothing else. In particular:

- **NEVER another project's data root.** Two fleets share one `data_root`; a page composed from
  another project's material lands in this project's create-only store and cannot be taken back.
- **NEVER a transcript file on disk.** It is the obvious input and the least bounded one: an
  unbounded read is the opposite of the cheap-for-a-tired-seat rule below, and a transcript
  carries every secret, path and dead end the firing touched.

## The write path

Two calls, in this order, both through `agentflow.wiki`. There is no third way to put a page in
the pillar, and hand-editing is not a fallback:

```python
from pathlib import Path
from agentflow import config
from agentflow.wiki import write_page, refresh_projection

# <data_root>/projects/<name>/wiki, built in code. Never paste data_root into a "..." literal:
# a Windows path's backslashes become escapes and the fence fails to compile.
wiki_root = Path(config.load_environment().data_root) / "projects" / "<name>" / "wiki"
# Raw and triple-quoted, so backslashes and either quote stay literal. The one text it cannot
# hold is three double quotes in a row; the newline before the closing quotes keeps a trailing
# backslash in the text from escaping them.
body = r"""
<the page, with at least one [[wikilink]]>
"""
page = write_page(
    wiki_root,
    slug="<kebab-slug>",       # a bare filename component; no separators
    title="<one line>",
    body=body.strip(),
    created="<YYYY-MM-DD>",    # COMPUTE it (`date -u +%F`), never type it — see below
    lane="<lane>",             # the seat writing it; drives hot-<lane>.md
)
refresh_projection(wiki_root)  # regenerates hot.md + every hot-<lane>.md; writes only the changed ones
```

- `write_page` is **create-only** and refuses an existing slug. A correction is a NEW page that
  links to the old one — never an overwrite. Overwriting would silently rewrite the record of what
  was learned, and since the projection is regenerated from pages, nothing else would show the
  loss.
- **`refresh_projection` is not optional and is not a hand edit.** A `hot*.md` written by hand is
  erased by the next refresh, and anything built on it becomes unreproducible. From a shell the
  equivalent is `python -m agentflow.wiki refresh --project <name>`, which prints a path per file it
  REWROTE -- none, on a run where nothing changed -- then a summary naming what it left
  alone, and exits non-zero **naming the file** when a page will not parse.
- **A refusal is loud and is not swallowed.** If `write_page` raises, the page is not written and
  the firing says so — a silently empty page is a memory store lying about what it holds.

## Lane duty, human command

Both, and they are the same skill:

- **A lane duty at the phase boundary** for **five seats, named rather than described**:
  `lead-dev`, `jr-dev-1`, `jr-dev-2`, `jr-dev-3` and `dev-manager`. Each trigger is a point in
  that seat's own procedure, so the page is written by the session that has the context, before
  the context is gone.
- **A human command in an interactive seat**: `/wiki-save [--project <name>] [what to save]`. The
  wrapper forwards `$ARGUMENTS` whole; the project selector is resolved first and the remainder is
  the subject.

**The five are a product decision — only the builders and the dev-manager — not a
predicate.** They are not "the self-waking seats" — that set includes `auditor` and
`steward` and excludes `dev-manager`, and it is the wrong set. The `auditor` is read-only against
the product by construction and the `steward` already owns `digest.md`; neither gained this duty,
and a later author pasting the block into either is a widening nobody signed off. The suite's
skill call-site tests hold that list once, as `SAVING_SEATS`, and DERIVE the exclusion from it —
so an eighth lane joining `AGENTS` lands outside the duty and its exclusion is asserted from the
moment it exists.

N writers into a create-only store is safe by construction: pages are create-only and
lane-namespaced, and the only shared surface — the projection — is a pure function of the page set,
so two seats refreshing at the same moment compute the same bytes.

## Cheap for a tired seat

This runs at the end of a firing, which is the moment the seat has least left. The standing
Directive is *exhaust will degrade your senses* — and this is where a tired seat meets it. So:

- **One page. One job. Links out.** A page either **shares a
  lesson** or **records an audit of a critical action**, and carries only what that job needs.
  **Cite rather than restate** — a page that links a PR, a spec or another page does not re-tell
  it. The length each job calls for is `LESSON_TARGET_WORDS` and `AUDIT_TARGET_WORDS` in
  `agentflow.save_summariser`, which is where the summariser's prompt reads them from and where
  `SaveOutcome.word_count` is measured against them. **Read them there rather than repeating a
  number here:** two copies of a provisional figure drift, and the copy in the file a tired seat
  actually reads is the one that wins.
- **Never a re-read of the whole context** to compose it. If composing the page needs a fresh read
  of everything the firing did, the page is too big — write the one thing that will not survive
  otherwise, link the rest, and stop.
- **Nothing durable? Write nothing and move on.** The empty case must be cheaper than the written
  one, or a tired seat learns to skip the step entirely.
- **Never let this step block the firing's real end-state writes.** The registry row, the ntfy and
  the state file come first; a page is owed after them, not instead of them.

## The summariser seam — an inline bounded one-shot

The page body is composed by a **model summarising the firing**, and the seat starts that model
**inline**. The summariser is a **bounded one-shot** the seat launches at
its own phase boundary, waits on, and writes the page from. No reconciler, no desired-state
record, no scheduler dependency.

**The seam is one boundary:** something hands `save` a `(title, body)` for a firing, and `save`
writes it through the path above. Everything upstream of that pair — triggers, unit, read
boundary, write path — does not depend on what composes the pair. Inline is the seam's **default**
implementation, not its only possible one; a reconciled headless session in its place stays a
one-file change.

**The implementation is the module `agentflow.save_summariser`
(`<suite_root>/skills/_shared/agentflow/save_summariser.py`), and it is the only place in
the suite where an agent session starts.** A seat CALLS it. A seat does not assemble a command
line of its own, which is why the guard below still bans a process start written into a fence
here: a fence is what a tired seat copies, and a copy is a second spawn path that inherits none of
the guardrails.

```python
from pathlib import Path
from agentflow import config
from agentflow.save_summariser import compose_and_save

# <data_root>/projects/<name>/wiki, built in code. Never paste data_root into a "..." literal:
# a Windows path's backslashes become escapes and the fence fails to compile before the save
# can return `failed`. <name> is the seat's resolved project, a bare slug.
wiki_root = Path(config.load_environment().data_root) / "projects" / "<name>" / "wiki"
# Raw and triple-quoted, so backslashes and either quote stay literal. The one text it cannot
# hold is three double quotes in a row; the newline before the closing quotes keeps a trailing
# backslash in the text from escaping them.
context = r"""
<what this firing did and concluded — bounded, never the whole transcript>
"""
outcome = compose_and_save(
    wiki_root,
    trigger="park",            # one of the four boundaries above
    context=context.strip(),
    lane="<lane>",
)
print(outcome.render())        # the audit line: trigger, duration, exit, wrote-or-not, slug
```

Note what is NOT in that call: `created`. It is not a parameter at all — see the requirement
below that `created` is COMPUTED, never typed.

### The rule against spawning seats is SCOPED by this, not repealed

Written here because this is where the next reader meets it. Without it, "seats may spawn" reads
as general licence, which is precisely what that rule refuses:

- **The rule governs DURABLE SEATS** — an identity, a wake cadence, a claim, and the ability to run
  away. A reconciler is what makes starting one auditable and idempotent. **Still forbidden to a
  loop.**
- **This exception governs a BOUNDED ONE-SHOT** — no identity, no cadence, no claim, it never
  resumes, and it exits.

They are different objects. Only the second one is licensed here, and only through the module
named above.

### The five guardrails

1. **NON-RECURSIVE, and it is a hard assertion rather than prose.** The spawned session hits phase
   boundaries of its own, so a save that could trigger a save would spawn a save that spawns a
   save. The child runs with saving DISABLED via `AGENTFLOW_SAVE_DISABLED`, and both halves are
   real: the spawn SETS it on the child's environment block, and the save entry point REFUSES when
   it finds itself running with it set. If the marker cannot be put on the child, **no process
   starts at all** — the failure mode of a lost marker is *no save*, never *an unguarded child*.
   A test proves each half alone and a third proves the composition.
2. **BOUNDED AND SINGLE.** One call per boundary, a default 180-second timeout, no retry, and an
   in-flight guard so a seat with a bounded one-shot already running does not start a second. The
   guard is a process-local lock released under `try/finally`, so it cannot go stale into a
   permanent refusal — a seat that dies takes it with it.
3. **NON-FATAL.** A failed, timed-out, empty or unparseable summariser writes nothing, says why,
   and never raises into the firing. The reply decides, not the exit code: a non-zero exit whose
   stdout still parses as a complete page is written, and its reason keeps the exit code and both
   streams so the anomaly stays in the record. It never blocks the epic, the merge or the wake. A memory
   function that can fail a build is worse than none — but a silent one is worse still, so the
   reason always reaches the caller.
4. **AUDITABLE.** Every attempt returns a record: the trigger, the UTC start, the duration, the
   exit code, whether it wrote, the slug if it did, and the word count it wrote. `--json` prints
   the same fields. That record is how *every spawn is
   auditable* holds without a reconciler. It is a return value and one
   rendered line, not a new store: a log file under `wiki/` would be a new file kind in a pillar
   built of pages plus pure projections, and a registry row would put per-save churn on a
   lock-protected coordination file — which rule 3 above forbids this step from touching.
5. **BEHIND THE SEAM ALREADY BUILT.** Inline is the default, not the only implementation. Moving
   to a reconciler later is a one-file change and no caller moves with it.

**The child never writes the store.** It composes text; the module validates it and calls
`write_page`. So the one-shot needs no store access and no permission grant, and a model that
misbehaves produces a rejected draft rather than a bad page.

**Two requirements the summariser honours:**

- **A summariser that returns nothing or errors fails LOUDLY.** The failure to prevent is a page
  written empty rather than a run that stops. `write_page` cannot catch it: an empty-bodied page is
  refused only because it carries no `[[wikilink]]`, which is an accident of the body being empty
  and not a check on the summariser. So the seam owes the check, not the writer — and
  `save_summariser.parse_summary` is where it is paid.
- **`created` is COMPUTED, never typed.** `_validated_created` checks SHAPE only — `2026-13-45`
  passes it — and `select()` ranks `created` lexicographically descending over a create-only
  store. So one mistyped or stale-session date pins a page to the top of `hot.md` and its
  `hot-<lane>.md` permanently, correctable only by a newer page that the bad one still outranks.
  The field most likely to be wrong is the one a tired seat types from memory at a phase boundary
  — so it is not a parameter. The module reads the clock itself and no caller can supply it.

**Nothing else in this skill starts a process.** No `subprocess`, no `Popen`, no `claude -p` typed
into a fence, no background task, no scheduler entry. The bounded one-shot lives in the module
named above; a seat that needs one calls `compose_and_save` and inherits every guardrail with it.
A command line copied out of a fence here would inherit none of them, which is why the ban
stands. It is gated by a test rather than left to prose.

## Definition of done for a change to the spawn path

**If you change how the one-shot is spawned, the live smoke gate is the check you owe, and it
will not run itself.** It is one real spawn that checks the child read the whole prompt and
returned a page in the expected shape, that the PARENT (not the child) wrote the page, and that the
audit record agrees with what landed on disk, with nothing filed outside the wiki root.
It stays a LOCAL OPT-IN: CI never runs it, on purpose, so that a leg requiring an authenticated
agent CLI cannot fail closed for every user who has not got one. That is also why this paragraph
exists — an opt-in nobody is told about is an opt-in nobody
uses, and the only place anyone learns this gate exists is here.

Run it from `<suite_root>/skills/_shared`. The prefix is not decoration: an
unrooted repo-relative path resolves from nowhere the reader of the DEPLOYED copy stands,
and a gate in this suite refuses one:

```
AGENTFLOW_LIVE_SAVE=1 python -m pytest tests/test_save_live_spawn.py -ra
```

**Keep the `-ra`, and know why it is `-ra` and not `-rs`.** The package's own `pyproject.toml`,
in that same directory, carries `addopts = "-ra"`. pytest finds that file from the test
paths it is given, not from where you stand, so a plain run of this test file picks it up and shows
the skip reason on its own. The flag in the fence is belt and braces for the runs that do not:
an explicit `-c` or `-o addopts=`, or a deployed copy of the test with no package `pyproject.toml`
above it. Without the setting, a `-q` run of that file prints one `s` among its dots and a
`passed, 1 skipped` summary — the skip REASON is not shown. And it must be `-ra`: a command-line
`-r` REPLACES the configured one rather than adding to it, so `-rs` here would show the skip and
drop the failed/error summary lines. The reason string is good
(`"no-opt-in: AGENTFLOW_LIVE_SAVE is not set ... set AGENTFLOW_LIVE_SAVE=1 to run one real
spawn"`) and it is what makes the skip loud, but you only see it with `-rs` or
`-ra`. A run without it cannot tell you the gate exists — which is the vacuous-green shape
this gate was written about, one layer out.

What the opt-in run costs and does not prove: model tokens, one spawn, one page, its own timeout.
It is a SMOKE test — it proves the path is connected end to end, not that a page is good and not
that a different agent CLI behaves the same. The pure checker helpers beside it are what run in
CI for free, so every defect the live case would catch is mimicked with an injected runner and
proved to red without spending anything.

## The precondition on writing — discharged

Unattended writing is enabled. Before any seat wrote a page with no human present, a human read
the **first generated pages as actual page text** — the bodies, not a count and not a summary of
them — and accepted the capability.

The review matters because it is the reason the pillar is trustworthy: a memory store nobody
has audited is worse than no memory store, because every later seat reads it as ground truth and
cannot tell a good page from a plausible one. The review judged that pages composed from
**bounded context** by a lane mid-epic come out specific and accurate. **If the pages come out
visibly thinner than the reviewed ones, that is evidence, and it is raised as a new question** —
it is not a licence to widen the read boundary above, which is untouched.

## Failure modes

| Failure | Action |
|---|---|
| `write_page` raises `PageExistsError` | the slug is taken. Write a NEW page that links to the existing one. Never overwrite, never delete-then-write. `compose_and_save` applies this itself: it retries once as `<slug>-2` with a line linking `[[<slug>]]` and reports `wrote`. Only when `<slug>-2` is taken too does it return `failed`, naming both slugs. |
| `write_page` raises `MissingWikilinkError` | the body has no `[[wikilink]]`. Add a real target — not `[[ ]]`, which the extractor correctly rejects as an edge to nothing. |
| `write_page` raises `WikiFormatError` | a field is malformed (`created` is not `YYYY-MM-DD`, the slug or lane is not a bare component, is over 100 bytes, or makes a path longer than the platform opens, the title carries a control character). Fix the field; do not route around the writer. |
| `write_page` raises `PageResidueError` (a `WikiFormatError`; `compose_and_save` returns `failed` with it) saying the page "carries pillar residue" | the slug, title, lane or body holds a term the pillar residue ratchet counts: the configured person's name, or a term the sanitize pattern covers. The message names the class and where (`person in title`, `sanitize in body line 3`), never the term. Nothing was written. Reword with the person named by role and the flagged term dropped, then write again. If it says the page "cannot be measured", the environment config or the extra pattern file will not load: fix that; never write around the guard. |
| `write_page` raises `NotAPillarError` (or `refresh` exits 2 saying "not a provisioned wiki pillar") | `wiki_root` is the wrong path or an unprovisioned project: it lacks `digest.md`, `pages/` or a GENERATED `hot.md`. Check the path; if it is right, re-provision the project (when only `hot.md` is named, `refresh` regenerates it). Never create the markers by hand. |
| `write_page` raises `WikiFormatError` saying the lane and a live lane "differ only by case" | the lane is a case-twin of a lane a live page already carries, e.g. `LEAD-DEV` against `lead-dev`. Nothing was written. Save again with the existing spelling. |
| `compose_and_save` returns `failed` with "lane refused before spawning" | nothing was written and no summariser ran; the rest of the reason names the cause. "differ only by case": the lane is a case-twin of a live lane, so save again with the existing spelling. Anything else: the lane itself is malformed (empty, or carrying `/`, `\`, `:`, `\|` or another character a filename cannot hold), so fix the lane. |
| `refresh` exits 2 naming a page | some page in the store will not parse. The projection was NOT rewritten. Say so; do not hand-edit the hot file to paper over it. |
| `refresh` exits 2 saying two lanes "differ only by case" | live pages carry two spellings of one lane; the error lists every page of each spelling. The projection was NOT rewritten. Pages are create-only, so retire one spelling: move its pages into `pages/archived/`, which refresh does not read, then refresh again. |
| `refresh` exits 3 | the filesystem refused (hot file locked or read-only, volume full). Nothing in the store is wrong; retry or report. |
| Nothing durable this firing | write nothing, say nothing. This is the expected outcome, not a skipped duty. |
| Two durable lessons and low context left | write the more perishable one and note the second as owed. Half a page is worse than one good one. |

## Reference

- The pillar's layout, the four-pillar authorship split, and the `pages/` unit:
  `<suite_root>/skills/_shared/SCHEMA.md` (the `data_root` subtree) and
  `<suite_root>/docs/DATA-LAYOUT.md`.
- The writer, the projection and every refusal above: `agentflow.wiki`.
- The steward owns `digest.md` and refreshes the projection on its sweep; it never writes
  `pages/`.
