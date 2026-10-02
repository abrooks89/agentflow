# CLAUDE.md — repo instructions for agentflow

Binding on any Claude Code session working in this repository.

---

## Never write Claude Code session URLs into anything tracked or published

**Do not put `claude.ai/code` session links in commit messages, PR titles or bodies, PR or issue
comments, or any tracked file.** That includes the `Claude-Session:` commit trailer and the session
URL a harness may append to a PR body by default.

They are links to a private conversation. The maintainers' working repository stays private and
is never made public; it is shared only through a fresh single-commit cut of one chosen commit
(`docs/runbooks/publish.md`), and a public copy of this tree is such a cut. A session URL in the
tree would travel with it.

- **`Co-authored-by:` is not affected** and should stay.
- If your harness appends the trailer by default, **remove it by hand before committing.** Assume
  nothing suppresses it for you until you have seen a commit of your own land without it.
- If you need the session recorded, record it somewhere private — never in this repo.
- The convention this rule belongs to — attribution, prose length, and the session-URL prohibition
  — is `skills/_shared/PR-PROSE.md`. It ships with the product, so any fleet inherits it; this file
  is belt-and-braces for this checkout and is not the fix.

**Existing history is out of scope.** This rule is going-forward only. The private working
repository's history is never rewritten; do not "clean up" its old commits, PR bodies, or comments
unless explicitly asked. A shared copy is always a fresh single-commit cut
(`docs/runbooks/publish.md`), never a push of the working history, which is how that history stays
private.

---

## The sanitize gate will fail your PR for personal or product literals

CI greps the whole tree (`.github/workflows/ci.yml`, job *"Grep for personal/product literals"`) and
**fails the build** on machine-specific paths, plus whatever install-specific literals (names of
other products, personal hostnames, private IP literals) the repository's `SANITIZE_EXTRA_PATTERN`
Actions variable adds. Only the generic shapes are written in `ci.yml`, because it ships; the rest
is kept out of the tree on purpose (`docs/runbooks/publish.md`, preconditions 1 and 3). It excludes
`.git`, `node_modules`, `_vendored`, `records`, `__pycache__` and `toy-repo`.

**It also scans your pull request's title and body** — those are published beside the code and are
not tracked files, so a clean checkout is not a clean PR. If it fails there, the message says so
explicitly, because the natural response is to grep the tree for the string and find nothing. Edit
the PR description or title; there is nothing to change in the checkout.

Three things about it worth knowing before you trip over them:

- It is a **whole-tree grep**, so a stray literal in a docstring, a test fixture, or a comment fails
  the same as one in shipped code.
- **An absolute home path is a finding** — `C:\Users\<acct>\…`, `/c/Users/<acct>/…`, `/home/<acct>/…`,
  `/Users/<acct>/…`. This is easy to trip while doing the right thing: proving which tree your suite
  imported means printing `agentflow.__file__`, which is a home path by construction. Placeholder
  prose is fine and stays fine — an elided `C:/Users/...`, a one-letter fixture account, and the
  words the pattern lists (`you`, `user`, `name`, …) are all exempt, so documentation can write a
  home path it does not own.
- Its pattern can carry **exemptions** (lookarounds that keep a legitimate word alive while the
  bare term inside it is a finding). If you ever write a bulk replacement derived from this
  pattern, reproduce its exemptions and not just its matches — a plain `str.replace` ignores a
  lookaround and will corrupt legitimate words while CI stays green, because the gate cannot fire
  on a string it exempts.

Run it locally before pushing rather than discovering it in CI. The tree half runs anywhere; the
title and body halves only have anything to read on a `pull_request` event, and say so in the log
when they do not.

**It does not, and cannot, cover the `wiki/` pillar.** The gate greps the checkout; pillar pages
live under the data root, which is not in the checkout and is not a git repository. That is
structural, not a gap somebody left open — do not file it, and do not try to extend the gate to
walk the data root. The pillar has its own ratchet, run where the pillar is:
`python -m agentflow.wiki residue --project <project>`. It derives the sanitize half of its
pattern from `ci.yml` at run time rather than keeping a copy, so editing the pattern above moves
it too, and unions the same local extra pattern file the cut's check reads. Details, exit codes
and the reason it is a ratchet rather than a gate:
`docs/runbooks/publish.md`, precondition 4.

---

## Every PR says where the work came from

CI (`.github/workflows/ci.yml`, job *"pr-provenance-gate"*) reads the PR body and **fails the
build** unless it can tell why the change exists. `.github/PULL_REQUEST_TEMPLATE.md` gives you the
line; keep it and fill it in:

```
Source: gh#123
```

Anything that resolves counts — a `gh#N` / `#N` issue, an `E-NNNN` epic id, or `Epic:` plus the
epic slug. Two escapes are declared rather than implied, and both pass:

- `Source: direct instruction` — a human asked for exactly this, outside the tracker.
- `Source: trivial` — a typo, a comment, a formatting-only change.

An issue number that turns out to be a **pull request** does not satisfy the gate. `#N` is
ambiguous in a way `gh#N` is not, and citing the PR that shipped a change as the reason for the
next one loses the reason entirely — the gate resolves the number through the API specifically so
that this fails loudly.

The point is not paperwork. A repository this size accumulates changes nobody can date or explain
within a quarter; the cheapest moment to record why is while you still know.

**The same job also fails the build unless the PR body says where the review went** (gh#312):

```
Review: https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id>
```

or `Review: none` when no separate review record exists. Body only, exactly one line, a URL or the
word `none`; the template's unfilled placeholder does not pass, and a comment cannot stand in for
it. The rule and its reason: `skills/_shared/PR-PROSE.md`, *Review pointer*.

---

## Scope of this file

This file records repository rules that are **enforced or externally binding** — not style
preferences and not workflow conventions, which live in `docs/` and in the loop skills under
`skills/`. Add to it only what a contributor would otherwise get wrong in a way that CI, a reviewer,
or a published artifact would catch.
