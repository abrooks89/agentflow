# PR and issue prose

The convention every agentflow loop follows when it writes to the git host: pull request bodies,
PR review comments, issue bodies, and issue comments.

It is stated here once. The authoring sites cite this file rather than restating it, because
copies of a rule drift, and a hazard fixed in only one copy stays live in the others.

## Attribution

The first line of every host-published body or comment is exactly:

```
Agentflow [<lane>]
```

`<lane>` is the seat that authored the text. Seven of the eight permitted values are registry
actors - the set `skills/_shared/agentflow/registry.py` enforces as `AGENTS`: `lead-dev`,
`jr-dev-1`, `jr-dev-2`, `jr-dev-3`, `dev-manager`, `auditor`, `steward`. **`orchestrator` is the
eighth and is deliberately not one of them**: it holds no `## Agents` row and posts on behalf of
the seat that dispatched it.

Do not derive the list from `LANES` in that same module. `LANES` is a different four-element set
(`lead`, `jr-dev-1`, `jr-dev-2`, `jr-dev-3`) naming Queue lanes rather than authors, and it
contains neither `lead-dev` nor any of the three non-building seats.

It goes first because GitHub's collapsed preview and the e-mail notification both truncate. A
reader has to be able to tell a loop wrote it without opening the page.

## Prose rules

- Headings name a change, never a verdict or a feeling. `## Registry schema` is fine;
  `## APPLIED WITH DEVIATION` is not.
- No justification sections. A decision recorded elsewhere is cited, not re-argued: write
  `deviation: registry Q-0008` and stop. The registry is the record of why; the PR is the record
  of what.
- No first-person narrative. Not `I chose`, `I decided`, `what I found was`.
- No sentence appears twice.

## Length

Free-text prose in a body or comment is capped at **1500 characters**. The cap counts prose.

Exempt from the count:

- **Generated structure** - task tables, verifier verdicts, commit SHAs, file lists. A large epic
  legitimately has a large table, and a byte cap that punished one would push builders to drop the
  table instead of the waffle.
- **The provenance line** (`Source:` and `Epic:` for a loop; `Closes #N` only for a PR from a
  fork, an outside contributor's, since a loop never closes an issue by keyword - *Citing an
  issue*, below). The `pr-provenance-gate` job in `.github/workflows/ci.yml` hard-fails a body
  carrying none, so counting it against a budget eventually costs a terse builder the one line CI
  cannot tolerate losing.
- **The review pointer** (`Review:`, below) - same gate, same reason.
- **The attribution line** above.

## Citing an issue

A loop's PR body cites the issue it works on as:

```
Source: gh#N
```

Neither the PR title, the body, nor any commit message on the branch puts a GitHub closing keyword
before an issue reference. The keyword is `close`, `closes`, `closed`, `fix`, `fixes`, `fixed`,
`resolve`, `resolves` or `resolved`, in any case, with or without a colon; the reference is `#N`,
`<owner>/<repo>#N` or an issue URL. (`gh#N` is not a reference GitHub closes on, which is why the
provenance line uses it.)

Why all three places: GitHub closes the issue on merge wherever the keyword sits in the body, even
in "does not close #N"; the repo's squash merge copies the branch's commit messages onto the base
branch, where a keyword closes the issue too; and the PR title becomes a multi-commit squash's
subject and a merge commit's message. Any of the three closes the issue without the auditor's
ask-by-ask check, and the seat that built a fix never closes its issue.

The `pr-provenance-gate` job enforces this for every PR from a branch in this repository, a loop's
or not, and for any PR whose body starts with the attribution line. A PR from a fork, an outside
contributor's, keeps `Closes #N` as its provenance: merging it is the maintainer's decision. A
keyword in the title or body is fixed with one `gh pr edit`; one in a commit message needs the
commit reworded and the branch pushed with `--force-with-lease`.

## Fixed-issue verdict comment

The one shape a closing seat posts on an issue a merged change claims to fix. The closing seats
are the auditor (its fixed-issue pass) and, on the human's instruction, dev-manager; the seat that
built the fix is never one. In order:

1. The attribution line.
2. `Checked against: <base_branch> <sha7>; claimed fixed by #<pr> (<sha7>)`, naming the claim's
   newest claimant. Use `spec <slug>` for a done spec, or `sha <sha7>` for a candidate that came by
   Note.
3. `Verdict: fixed`, `Verdict: remainder` or `Verdict: routed`.
4. A per-ask table, `| # | ask (from) | verdict | evidence |`. It is generated structure, exempt
   from the prose cap.
5. A `Remainder:` list, when the verdict is remainder.
6. A `Routed:` line, when the verdict is routed: `Routed: <reason> -> <recipient>`. The reason is
   `moot`, `duplicate`, `won't-fix`, `close_failed`, `evidence_shape` or `fixer_is_actor`, and the
   recipient is the seat the Note went to.

The verdicts:

- `fixed` - every ask met, and the issue is being closed (through
  `python -m agentflow.issue_intake close`, which posts this comment and then closes).
- `remainder` - at least one ask not met, and the issue stays open.
- `routed` - the checking seat will not close it, so the issue stays open and the judgement went
  to the named seat.

Tools read `Checked against:`, `Verdict:` and `Routed:`, so their spelling is fixed. A `remainder`
or `routed` verdict marks the claim checked until a newer claimant merges, and each `fixed` verdict
on an issue that is still open counts as one failed close.

## Review pointer

Every PR body carries exactly one line naming where its review went:

```
Review: https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id>
```

or, when no separate review record exists for the PR:

```
Review: none
```

The `pr-provenance-gate` job hard-fails a body carrying neither. The
line goes in the body and not in a comment, because a comment fires no `pull_request` event: a gate
counting comments would evaluate before the comment exists and never again. The `opened` run reads
the body line; `edited` is for a body fixed after a gate failure. `none` is legal so the gate never
blocks a PR that genuinely had no review record. What the URL points at is not checked.

**The pointer is final when the PR is created.** In this order:

1. Finish the review.
2. Post the review comment on the issue the PR will cite for provenance (the first one when it
   cites several): `gh issue comment <n> --repo <owner>/<repo> --body-file <file>`. Give the
   comment a first line no other comment has, naming the PR branch or epic and the review round.
   The command prints the new comment's URL. If it prints nothing, find this comment by that line:
   `gh api --paginate repos/<owner>/<repo>/issues/<n>/comments --jq '.[] | select(.body | startswith("<first line>")) | .html_url'`
3. Run `gh pr create` with `Review: <that URL>` already in the body.

Except to fix a body the gate failed, a loop never adds or re-points the pointer with a
`gh pr edit` after create: every `gh pr edit` fires the `edited` trigger, which re-runs every CI
job, not only the gate, on a SHA that already ran them. A later review round is a further comment
on the same issue, and the pointer is not re-pointed. A `gh pr edit` whose title and body are both
unchanged is skipped.

A PR that cites no issue (its provenance is an `Epic:` line, an `E-NNNN` id or a declared escape)
carries the review verdicts in its own body and writes `Review: none`.

Why: a review that lives only in the lane's state file dies with the lane. A pointer in the body outlives the lane; so does `none`, which says on the PR that
nothing is missing rather than leaving the inheritor to guess.

The pointer is a POINTER: the review comment it names is still written per this file, and each
later round's review still goes on the same issue as that round completes. Neither is enforced by
the gate.

## No Claude Code session URLs on the host

No `claude.ai/code` link belongs in a commit message, a PR title or body, a PR or issue comment, or
any tracked file. That includes the `Claude-Session:` trailer and the session URL a harness appends
to a PR body by default. They are links to a private conversation.

`Co-authored-by:` is unaffected and stays.

If a session needs recording, record it somewhere private, such as a configured vault, and never on
the git host.

This is a product rule rather than one user's policy. No agentflow user should have their loops
publish a private conversation link, and none of them should have to know to ask - so the installed
loop skills carry the prohibition and a fleet inherits it with no configuration. A repo `CLAUDE.md`
is useful belt-and-braces for one checkout, and is not the fix.

## Coverage

Every seat that publishes to the git host is bound by this file, including seats whose skill does
not yet cite it.

Wired to cite it: `skills/orchestrator/SKILL.md`, `skills/lead-dev/SKILL.md`,
`skills/dev-manager/SKILL.md`, `skills/steward/SKILL.md`, `skills/auditor/SKILL.md`,
`skills/polish-mode/SKILL.md`. That is a starting point rather than an
inventory - the binding statement is the sentence above it, never the enumeration.
