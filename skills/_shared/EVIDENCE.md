# Evidence states — empty, unavailable, truncated and stale are different answers

Each gate-bearing skill carries one pointer line to this file. It is one rule, kept in one place
so no seat drifts from it.

A missing connector, a partial page or a failed query is not an empty source. A call that asked
for 400 rows and got exactly 400 has not said there are 400; it has said there are *at least* 400.

## The five states

Every read of the outside world (`gh`, git, HTTP, ntfy, another seat's files) comes back in one
of five states:

| State | Meaning | Complete? |
|---|---|---|
| `value` | Looked, complete, found something. | yes |
| `empty` | Looked, complete, found nothing. | yes |
| `unavailable` | Could not look: the command failed, the file is missing or unreadable, the host did not answer. | no |
| `truncated` | Looked, but saw only part: a full page, a capped window, a parse that stopped early. | no |
| `stale` | Looked, but the answer is older than what it gates: a verdict for a previous head, a status read before the change it judges. | no |

## The three rules

1. **Never collapse unavailable, truncated or stale into empty.** A failed read that returns `[]`,
   `0`, `None` or "no rows" is a false `empty`. Carry the state with the value, and when the state
   cannot be determined, say so rather than reporting absence.
2. **A gate passes only on complete evidence bound to what it gates.** Complete means `value` or
   `empty`. Bound means the evidence names what it is about: a head SHA, a timestamp, or the
   revision it was read at. A green check for an earlier head is `stale`, not green.
3. **Every report states what it read and what it did not.** A source that could not be read is
   shown as unavailable, never as zero. A count drawn from a truncated read is shown as a floor
   ("at least 400"), with what was cut off.

## In code

`agentflow.evidence.Reading` carries a read and its state: `Reading.ok`, `Reading.empty`,
`Reading.unavailable(reason)`, `Reading.truncated(value, coverage)` and
`Reading.stale(value, as_of)`. `require_complete()` raises `IncompleteEvidence` unless the state
is `value` or `empty`; `describe()` renders the coverage line a report prints. An incomplete
reading must say why: the constructors refuse a blank reason, coverage note or `as_of`.
`require_complete()` checks the state only: binding the evidence to what it gates (rule 2) is the
caller's, by comparing `as_of` with the head or time being gated.

`skills/_shared/tests/test_evidence_adapter_inventory.py` lists every outside-world read in the
modules being moved onto `Reading`. Each must return `Reading[...]` unless it is on the
`UNMIGRATED` allowlist, and that allowlist may only shrink.

## Appendix — the lessons this rule consolidates

The project wiki holds at least 42 lessons on this one theme; these are the ones linked here. They
are the rule's evidence; the slug is the page's filename under the wiki's `pages/` directory.

- `a-parked-row-is-not-an-absent-row`
- `a-skipped-check-and-a-passing-check-are-indistinguishable-until-the-answer-changes`
- `a-report-truncated-by-position-drops-the-cause`
- `a-truncated-token-is-a-prefix-so-every-presence-check-still-passes`
- `absence-claims-fail-toward-unknown`
- `a-blocked-backlog-is-not-a-dry-one`
- `a-default-to-safe-fallback-must-require-every-source-to-be-empty`
- `a-distinction-your-parser-collapses-cannot-be-restored-by-documenting-it`
- `a-failure-that-means-two-things-is-not-actionable`
- `a-filter-on-a-nonexistent-label-returns-empty-only-the-write-side-errors`
- `a-floor-on-the-parse-is-not-a-floor-on-the-coverage`
- `a-frozen-remote-tracking-ref-reports-an-unreachable-host-as-current`
- `a-max-of-writers-mtime-is-as-stale-as-its-least-stale-writer`
- `a-missing-wake-tool-reads-as-a-loop-that-finished`
- `an-absent-check-means-ask-whether-the-trigger-fired`
- `an-alarm-that-skips-unparseable-rows-reports-zero-at-risk`
- `an-empty-check-list-is-not-a-pending-one`
- `an-empty-sweep-is-not-an-empty-queue`
- `an-unparseable-record-is-dropped-from-selection-with-no-report`
- `a-preflight-is-a-reading-not-a-list-you-can-reuse`
- `a-probe-that-never-reaches-the-code-path-proves-nothing`
- `a-protocol-whose-normal-output-is-silence-cannot-report-its-own-outage`
- `a-timestamp-read-once-measures-freshness-read-twice-it-measures-motion`
- `a-warning-that-vanishes-on-re-run-may-be-cached-not-absent`
- `a-window-that-truncates-before-it-filters-hides-the-item-it-was-sized-for`
- `a-zero-refutation-panel-is-a-smell-not-a-result`
- `check-three-outcomes-empty-input`
- `ci-job-steps-zero-check-annotations`
- `ci-outage-queued-pr-untested-gates`
- `empty-confirmed-findings-is-not-a-clean-review`
- `gate-parsers-must-fail-closed-and-mirror-renderer`
- `grep-or-true-fails-open`
- `incomplete-files-list-is-a-false-disjointness-proof`
- `intake-open-issue-page-refusal`
- `one-tool-traverses-the-junction-and-another-silently-does-not`
- `parked-pr-stale-green-rerun-master-gates`
- `pathlib-is-predicates-swallow-oserror-py314`
- `pillar-residue-report-counts-only-hit-pages`
- `stale-ci-verdict-after-force-push`
- `truncated-output-is-not-a-set`
- `widening-probe-reach-widens-silent-failure`
- `zero-hits-is-not-proof-of-redaction`
