<!--
Every PR cites where it came from and where its review went. CI enforces both
(job: "PR provenance gate"). Full convention: skills/_shared/PR-PROSE.md

1. Source: keep ONE of the lines below, take it out of its comment, and delete the rest.
   The gate reads the PR body and title.
2. Review: keep ONE Review line below, take it out of its comment, and fill it in. The gate
   reads it from the body only. Nothing may follow the URL or `none` on that line, and an
   unfilled line fails.

Outside contributors (a PR from a fork): open an issue first and cite it with Closes #<n>, or
use `Source: trivial` for a typo or comment-only change. Then write `Review: none`.

A PR from a branch in this repository, a loop's or anyone's, cites an issue as Source: gh#<n>
and never puts Closes / Fixes / Resolves before an issue reference in its title, body or commit
messages: the gate fails it, because a fixed issue is closed after a check, not on merge.

Loops only, per PR-PROSE.md; people leave it out: an attribution line
"Agentflow [<lane>]" goes FIRST, above the Source line, with <lane> replaced by the
actor name. Collapsed previews and e-mail both truncate, so it stays at the top.
-->

**Source:** <!-- pick one, delete the others -->
<!-- Source: gh#<n>                      an issue in this repo, for a PR from a branch in this repo -->
<!-- Closes #<n>                         an issue, for outside contributors (a PR from a fork) only -->
<!-- Epic: <id-or-slug>                  maintainer-only: a registry queue id or a spec slug -->
<!-- Source: direct instruction         a maintainer asked for this directly, no ticket -->
<!-- Source: trivial                    typo / indent / comment-only, no ticket warranted -->

**Review:** <!-- required: replace this comment with the review comment's URL or with none, and
     nothing after it; or delete this line and uncomment ONE line below exactly as it stands.
     none means no separate review record exists for this PR. -->
<!-- Review: https://github.com/<owner>/<repo>/issues/<n>#issuecomment-<id> -->
<!-- Review: none -->

## What

<!-- One or two sentences. What changed and why it is the right change. -->

## Verification

<!-- What you ran and what it said. "Tests pass" is not a result; "1152 passed, 5 skipped" is. -->

---

<!--
Why the gate exists: a convention that lives only in people's heads stops being followed the
first time the context turns over. The rule and its reason: skills/_shared/PR-PROSE.md
-->
