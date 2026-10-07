"""The single tracked home of agentflow's RETIRED vocabulary.

WHY A MODULE EXISTS FOR TWO STRINGS. A retired term does not disappear the moment the
code stops accepting it: a migration has to know what to search FOR, a test has to assert
the old value is gone, and a reader six months from now has to be able to find out what
the value used to be without a `git log -S` expedition. Those three needs each want the
retired spelling in the tree. Left to themselves they put it in three places, and the
third one is the one nobody updates.

So the rule is: the retired spelling is written ONCE, here, and everything that needs it
imports it.

WHAT CHILD 2 ACTUALLY SHIPPED, because this paragraph used to predict it wrongly. It said the
gate armed by `de-autoloop-the-loop-skill-prose-and-arm-the-vocabulary-gate` would BAN this
string everywhere but here and a named exemption list. It does not. gh#252 argued that a ban
is a denylist over history: it closes one instance and leaves the class open, catching neither
a typo that was never a value, nor a third spelling invented after the gate was written, nor a
real-but-non-dispatchable value taught as though it dispatched.
`skills/_shared/tests/test_vocabulary_gate.py` is an ALLOW-list instead - every `queue_class`
value taught in a tracked SKILL.md must be a member of the two frozensets in `epic_survey`,
which it imports rather than restates. The retired spelling is caught there for the right
reason, that it is not in the vocabulary, rather than because it is on a list of banned words.

WHAT THAT GATE DOES NOT CATCH. It checks MEMBERSHIP only, so it closes the first two of those
three cases and not the third: a real non-dispatchable value taught as though the fleet would
build it is in the vocabulary, so the gate passes it (gh#938). Where that case IS caught is
written once, in the docstring of `test_vocabulary_gate.py`.

That gate reads SKILL.md files only, so it never sees this module, the CHANGELOG or the harness
fixtures, and the three permanent exemptions named in `skills/_shared/SCHEMA.md` need no
exemption from it.

WHY THE STRING IS SPELLED OUT AND NOT ASSEMBLED. Splitting it (`"auto" + "loop-buildable"`)
would hide it from the very grep that has to find it. The point of this file is to BE the
one hit. It is spelled literally, once, on purpose.

WHY `epic_survey` DOES NOT IMPORT `CURRENT_QUEUE_CLASS`. The allow-list
`DISPATCHABLE_QUEUE_CLASSES` spells `fleet-buildable` literally, because the allow-list is
where every reader looks to answer "what value dispatches?" and an import turns that
one-line answer into a two-file lookup. The invariant that the migration's TO value is a
value the allow-list actually accepts is guarded by a TEST instead
(`test_migrate_queue_class.py::test_current_value_is_dispatchable`) - which catches drift
without costing the reader anything. The retired value is the opposite case: nobody should
be reading it, so indirection is free and duplication is the risk.
"""

from __future__ import annotations

#: The retired `queue_class` value. Removed from the dispatch allow-list on 2026-09-05 by
#: `retire-the-autoloop-queue-class-value` as a HARD cut (no dual-accept window - parent
#: decision 2B), so a spec still carrying it surveys as out-of-vocabulary, warns once, and
#: dispatches nothing until `migrate_queue_class` rewrites it.
#:
#: THIS IS THE ONLY TRACKED PLACE THIS STRING IS WRITTEN, apart from two permanent
#: exemptions - the CHANGELOG and one recorded harness fixture - which are named and
#: justified in `skills/_shared/SCHEMA.md`. Import it; never re-spell it.
RETIRED_QUEUE_CLASS: str = "autoloop-buildable"

#: What `RETIRED_QUEUE_CLASS` was renamed TO. Lives here beside it so the migration's FROM
#: and TO cannot be edited apart, and so the rename is legible as one fact rather than two
#: constants in two modules.
CURRENT_QUEUE_CLASS: str = "fleet-buildable"

#: The rename, as the migration reads it: old value -> new value. A mapping rather than a
#: pair because the next retirement should extend this dict and inherit the migration and
#: its safety tests, instead of growing a second script that re-derives which files the
#: survey parses - which is the part that is easy to get subtly wrong.
QUEUE_CLASS_REWRITES: dict[str, str] = {RETIRED_QUEUE_CLASS: CURRENT_QUEUE_CLASS}
