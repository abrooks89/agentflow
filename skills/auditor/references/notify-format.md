# notify-format.md — pass summary notification format (FORMAT)

When `audit.notify` is true, the audit pass sends **one line** through the configured notify
endpoint at the end of a pass (step 4, "Record + notify"). This is a heads-up for the human's
dev-manager cadence — not the record. The record is the ledger and the report; the notification
just says a pass happened and whether it produced work to approve.

---

## The line

One sentence, present-tense, counts first. Shape:

```
audited <N>: <M> fully met, <K> gap epics filed for approval
```

Examples:

```
audited 7: 5 fully met, 2 gap epics filed for approval
audited 4: 4 fully met, 0 gap epics filed
audited 6: 3 fully met, 3 gap epics filed for approval (1 regression)
audited 0: nothing new to audit
audited 3: 3 fully met, 0 gap epics filed; issues: 2 closed, 1 remainder, 0 routed
audited 0: nothing new to audit; issues: 1 closed, 0 remainder, 1 routed
audited 2: 1 fully met, 1 gap epics filed for approval; issues: unread
audited 2: 1 fully met, 1 gap epics filed for approval; 1 blocked
audited 0: 2 blocked, nothing judgeable (epic-noverify, epic-staging-down)
audited 0: 1 blocked, nothing judgeable (epic-noverify); issues: 1 closed, 0 remainder, 0 routed
```

Rules for the body:

- **Counts first, then meaning.** `audited <N>` is the window actually judged this pass (after
  the already-audited filter), not the raw survey size.
- `<M> fully met` = epics with zero shortfalls (all `satisfied`); these filed no gap epic.
- `<K> gap epics filed` = total drafted gap specs this pass produced (may exceed the number of
  epics with gaps, since one epic can yield more than one gap epic).
- **Surface regressions.** If any gap carries the `regression` tag, append `(<R> regression)` /
  `(<R> regressions)` — regressions are the highest-priority approval class and the human should
  see them in the glance.
- **The fixed-issue pass (Step 4b).** When it acted, append
  `; issues: <c> closed, <r> remainder, <n> routed`: issues closed after every ask was met, issues
  left open with a remainder verdict, and issues routed to dev-manager. When its listing failed,
  append `; issues: unread` instead, never a zero. A wake whose epic window was empty but whose
  Step 4b acted reads `audited 0: nothing new to audit; issues: <c> closed, <r> remainder, <n> routed`.
- **Blocked count.** `<B> blocked` counts the epics in the window that came back blocked (Step 6),
  including one whose raise the Step 6 dedupe skipped because its blocker row is still open. When
  at least one other epic was judged, append `; <B> blocked` right after the counts and before any
  `; issues:` clause. A pass with no blocked epic carries no blocked clause.
- **Blocked-only pass:** `audited 0: <B> blocked, nothing judgeable (<slug>, ...)` when the window
  held epics and every one of them came back blocked. It names the blocked slugs so the human
  sees which epics are stuck, and it never borrows the empty-pass line: the window was not empty,
  nothing in it was judgeable. Append the `; issues:` clause when Step 4b acted. Keep the line
  under the length cap below by dropping trailing slugs and ending the list with `...`; the full
  list is in the auditor state file's `blocked:` key.
- **Empty pass:** `audited 0: nothing new to audit`, only when the epic window was empty AND Step
  4b had no candidate. The loop then idle-wakes and checks again —
  an empty pass never stops the loop, so there is no separate stop summary for it.
- Keep it to one line, no trailing period needed, under ~120 chars so it renders whole in a
  push notification.

## ASCII-only body (hard constraint)

The notify endpoint is ntfy, which **400s on multibyte UTF-8 in the message body** — a middot
(`·`), en/em dash (`–`/`—`), curly quote, arrow, or emoji makes ntfy reject the send as
"attachments not allowed". The summary body **must be pure 7-bit ASCII.**

- Use ASCII hyphen `-` only; never `–` or `—`.
- Use straight quotes `'` `"`; never curly.
- Separate clauses with `,` `:` `;` or `-`; never `·` or `|` box characters.
- No emoji, no arrows (`->` is fine, `→` is not), no non-breaking spaces.

If a slug or label could contain non-ASCII, transliterate or drop it from the body — the
notification is a glanceable heads-up, and the exact slugs live in the ledger and design/specs/
anyway. (Titles/tags on the notify request may tolerate more, but keep the **body** ASCII to be
safe.)

## What it is not

- **Not the record.** Never let a failed or skipped notify block the pass — the ledger and
  report are the source of truth; notify is best-effort. Log a send failure and continue.
- **Not per-epic.** One line per pass, not per audited epic. Per-epic detail is the report.
- **Not an approval prompt.** "filed for approval" describes where the drafts landed (the
  dev-manager queue); the auditor never blocks waiting on that approval.
