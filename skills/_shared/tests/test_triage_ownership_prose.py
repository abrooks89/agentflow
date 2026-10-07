"""No unattended seat's SKILL.md tells it to label its own filing `triaged`: the TRIAGE OWNERSHIP pin.

The rule: no agent triages an issue it raised. The steward may label `triaged` + `bug` on a
bounded issue ANOTHER lane raised, with one evidence comment; what it files itself goes out
without `triaged` or `bug` for dev-manager's intake; a mixed issue it splits becomes children
filed the same way, each naming its parent, which lead-dev labels. The steward's prose once told
it to add `--label triaged` to its own `issue create`, and `triaged` landed on its filings a
second after creation, by the filer.

THE SEATS SCANNED are the fleet loops that run without the human in the room: `lead-dev`,
`steward`, `auditor` and every `jr-dev-*`. dev-manager is the human's seat and the triager of
record, so its `--add-label triaged,bug` on other lanes' issues is the rule, not a breach.

THE PREDICATE, two forms, both on command text rather than on words about it. Backslash line
continuations are joined first, so a command split across lines is read as one line.
* a line holding `issue create` that also holds a `--label` or `-l` whose comma-separated
  values, each stripped, include `triaged` (quoted, unquoted, or after a placeholder such as
  `<x>,triaged`). Creating an issue with the label is self-triage whatever the surrounding
  sentence says, so a negation on the same line trips it too: the rule is stated in its positive
  form ("file it without `triaged`") instead.
* an `--add-label` whose values include `triaged`, in the same block as an `issue create` or as
  an own-filing phrase: "you/it/this seat (just) filed/raised/created/opened", "your/its own
  issue", or "the <seat> filed ..." naming the seat being scanned. Labelling an issue another
  lane raised ("another lane raised", or "the steward filed" when lead-dev is scanned) names a
  different raiser and passes.

THE BLOCK BOUND. The create-then-label form is seen only inside one block: a paragraph between
blank lines, or one markdown table row. A create in one paragraph and its `--add-label` in the
next, or an own-filing phrase that only an earlier paragraph establishes, passes this checker.

POSITIVE CONTROLS. The pre-fix steward text is embedded below and must fail both the predicate
and the section pins; planted bad forms must fail; the legitimate forms must pass. The pins also
tie domain G's comment exception to the `triaged` paragraph, so the two cannot drift apart.

WHAT IT CANNOT PROVE. That the predicate sees every phrasing of self-triage: it catches the
command forms above, not prose that describes the act in other words. Nor that a wake follows
the prose, or that the host refuses a self-triage: the code-level refusal is a separate spec.
"""

from __future__ import annotations

import re
from pathlib import Path

from agentflow.tracked_paths import tracked_files

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "skills"

FIXED_SEATS = ("lead-dev", "steward", "auditor")

_VALUE = r"""(?:"([^"]*)"|'([^']*)'|([^\s`'"]+))"""
LABEL_RE = re.compile(r"(?:--label|(?<![\w-])-l)(?:=|\s+)" + _VALUE)
ADD_LABEL_RE = re.compile(r"--add-label(?:=|\s+)" + _VALUE)
_FILED = r"\s+(?:just\s+)?(?:filed|raised|created|opened)\b"
OWN_FILING = (
    r"\b(?:you|it|this seat)" + _FILED,
    r"\b(?:your|its)\s+own\s+(?:issue|filing)s?\b",
)
NO_TRIAGED_LABEL = re.compile(r"unlabell?ed|without `triaged`|never with `triaged`")

# The steward's text before the fix, verbatim: the positive controls.
PRE_FIX_ISSUE_ROW = (
    "| **Issue** | A **bounded defect with a known repair** — one component, no design left to do "
    "| `<git_host.cli> issue create --repo <canonical_repo.host_ref>` — add `--label triaged` ONLY "
    "when you verified it bounded yourself; otherwise file it unlabeled. Write the body per "
    "`<suite_root>/skills/_shared/PR-PROSE.md` |"
)
PRE_FIX_G = (
    "**G. Issue triage.** **Report the open count and its trend** — a backlog growing faster than it\n"
    "closes is itself a finding. **No verification, no issue comment, no closure:** you\n"
    "verify no claim, post no comment on an issue and close nothing. Fixed issues are the auditor's\n"
    "(its fixed-issue pass). Duplicates, won't-fix and premise-gone issues are dev-manager's and the\n"
    "human's (dev-manager §3.4).\n"
)
PRE_FIX_CLAIM_PARAGRAPH = (
    "**The `triaged` label is a claim, not a courtesy.** Builders select bugs only from issues\n"
    "labeled `triaged` (lead-dev §2 via `agentflow.issue_intake`), so an issue you label `triaged`\n"
    "skips dev-manager's §3.4 intake and goes straight to a bug-burn wake. Add it only when you\n"
    "yourself established the defect is bounded — one component, repair known, no decision left —\n"
    "and say so in the issue body. If you are not sure, file it unlabeled: dev-manager's intake\n"
    "triages it (bug / epic / brainstorm-pin / close) on its next pass, and nothing is lost but a\n"
    "wake. A steward-labeled `triaged` issue that turns out to need design is the same defect as a\n"
    "structural finding filed as an issue.\n"
)
PRE_FIX_DIVISION = (
    "## Division of labor (deltas)\n\n"
    "* **Issue triage is dev-manager's (§3.4), not yours.** You close no issue and report closure debt\n"
    "  (domain G); you file what you find; you label an issue `triaged` only when you verified it\n"
    "  bounded yourself. An unlabeled issue you file is the intake's to dispose, and the builders\n"
    "  cannot see it until it is.\n"
)


def _seat_files() -> dict[str, Path]:
    seats = {}
    corpus = tracked_files(
        REPO_ROOT,
        lambda rel: rel.startswith("skills/") and rel.count("/") == 2 and rel.endswith("/SKILL.md"),
    )
    for path in corpus:
        name = path.parent.name
        if name in FIXED_SEATS or name.startswith("jr-dev-"):
            seats[name] = path
    return seats


def _carries_triaged(regex: re.Pattern[str], text: str) -> bool:
    for groups in regex.findall(text):
        value = next(g for g in groups if g) if any(groups) else ""
        if "triaged" in (v.strip() for v in value.split(",")):
            return True
    return False


def _normalise(text: str) -> str:
    """LF endings, and every backslash continuation joined onto its line."""
    return re.sub(r"\\\n[ \t]*", " ", text.replace("\r\n", "\n"))


def _blocks(text: str) -> list[str]:
    """Paragraphs split on blank lines; each markdown table row is a block of its own."""
    blocks = []
    for para in re.split(r"\n\s*\n", text):
        lines = para.split("\n")
        blocks.extend(ln for ln in lines if ln.lstrip().startswith("|"))
        rest = [ln for ln in lines if not ln.lstrip().startswith("|")]
        if rest:
            blocks.append("\n".join(rest))
    return blocks


def self_triage_hits(text: str, seat: str) -> list[str]:
    """Every line or block that has `seat` label its own filing `triaged`."""
    text = _normalise(text)
    own = re.compile(
        "|".join((*OWN_FILING, r"\bthe\s+" + re.escape(seat) + _FILED)), re.IGNORECASE
    )
    hits = []
    for line in text.split("\n"):
        if "issue create" in line and _carries_triaged(LABEL_RE, line):
            at = line.find("issue create")
            hits.append(f"`issue create` carries the triaged label: ...{line[at:at + 160]}...")
    for block in _blocks(text):
        if not _carries_triaged(ADD_LABEL_RE, block):
            continue
        if "issue create" in block or own.search(block):
            hits.append(f"--add-label triaged on the seat's own filing: {block.strip()[:160]}")
    return hits


def _slice(text: str, start: str, end: str) -> str:
    """From `start` to the next `end` after it; empty when `start` is absent."""
    i = text.find(start)
    if i == -1:
        return ""
    j = text.find(end, i + len(start))
    return text[i:] if j == -1 else text[i:j]


def _flat(text: str) -> str:
    """Whitespace collapsed, since a needle may wrap across lines."""
    return " ".join(text.split())


def _issue_row(text: str) -> str:
    return next((ln for ln in text.splitlines() if ln.startswith("| **Issue** |")), "")


def steward_rule_failures(text: str) -> list[str]:
    """The steward's three places state the raiser rule, and domain G allows the one comment."""
    text = text.replace("\r\n", "\n")
    places = {
        "the Three outputs Issue row": _flat(_issue_row(text)),
        "the `triaged`-label paragraph": _flat(_slice(text, "**The `triaged` label is a claim", "\n\n")),
        "Division of labor": _flat(_slice(text, "## Division of labor", "\n## ")),
    }
    bad = []
    for where, body in places.items():
        if not body:
            bad.append(f"{where} not found")
            continue
        for needle in ("TRIAGE OWNERSHIP", "lead-dev label"):
            if needle not in body:
                bad.append(f"{where} lacks {needle!r}")
        if not NO_TRIAGED_LABEL.search(body):
            bad.append(f"{where} does not say the steward's own filing goes out without `triaged`")
    if "`Agentflow [steward]`" not in places["the Three outputs Issue row"]:
        bad.append("the Issue row does not name the `Agentflow [steward]` first line")
    for where in ("the `triaged`-label paragraph", "Division of labor"):
        if "another lane" not in places[where].lower():
            bad.append(f"{where} does not limit the steward's labelling to another lane's issue")
    g = _flat(_slice(text, "**G. Issue triage.**", "\n## "))
    if "post no comment on an issue" in g:
        bad.append("domain G still forbids every issue comment, against the `triaged` paragraph")
    for needle in ("`Agentflow [steward]`", "another lane", "`triaged` + `bug`", "comment"):
        if needle not in g:
            bad.append(f"domain G's comment exception lacks {needle!r}")
    if "comment G allows" not in places["the `triaged`-label paragraph"]:
        bad.append("the `triaged`-label paragraph does not tie its comment to domain G")
    return bad


# ── the real tree ────────────────────────────────────────────────────────────


def test_seat_set_is_not_vacuous():
    seats = _seat_files()
    for name in FIXED_SEATS:
        assert name in seats, f"skills/{name}/SKILL.md not found - moved, or this pin's path is stale"
    assert any(n.startswith("jr-dev-") for n in seats), "no jr-dev-* SKILL.md found"


def test_no_unattended_seat_labels_its_own_filing_triaged():
    failures = []
    for name, path in _seat_files().items():
        for hit in self_triage_hits(path.read_text(encoding="utf-8"), name):
            failures.append(f"skills/{name}/SKILL.md {hit}")
    assert not failures, "\n".join(failures)


def test_steward_states_the_raiser_rule_and_g_allows_the_one_comment():
    text = (SKILLS / "steward" / "SKILL.md").read_text(encoding="utf-8")
    bad = steward_rule_failures(text)
    assert not bad, "\n".join(bad)


# ── positive controls ────────────────────────────────────────────────────────


def test_pre_fix_issue_row_fails():
    assert self_triage_hits(PRE_FIX_ISSUE_ROW, "steward")


def test_pre_fix_steward_text_fails_the_rule_pins():
    pre = "\n\n".join(["## Three outputs", PRE_FIX_ISSUE_ROW, PRE_FIX_G, PRE_FIX_CLAIM_PARAGRAPH, PRE_FIX_DIVISION])
    bad = steward_rule_failures(pre)
    for where in ("Issue row", "`triaged`-label paragraph", "Division of labor"):
        assert any(where in b and "TRIAGE OWNERSHIP" in b for b in bad), where
    assert any("domain G still forbids" in b for b in bad)
    assert any("does not tie its comment to domain G" in b for b in bad)


def test_planted_lines_fail():
    planted = [
        "File it with `gh issue create --repo o/r --label bug,triaged`.",
        "Run `gh issue create --repo o/r --label=triaged --title x`.",
        "Run `gh issue create --repo o/r --title x`, then\n`gh issue edit N --add-label triaged,bug` on it.",
        "On the issue you just filed, run `gh issue edit N --add-label triaged`.",
        "Label your own issue with `--add-label triaged,bug` once it is bounded.",
        # a backslash continuation carries the label onto the next line
        "```\ngh issue create --repo o/r --title x \\\n  --label triaged\n```",
        "Run `gh issue create --repo o/r -l triaged`.",
        'Run `gh issue create --repo o/r --label "bug, triaged"`.',
        'On the issue you just filed, run `gh issue edit N --add-label "bug, triaged"`.',
        "Run `gh issue create --repo o/r --label <x>,triaged`.",
    ]
    for line in planted:
        assert self_triage_hits(line, "steward"), line


def test_the_seat_naming_itself_as_raiser_fails():
    line = "On an issue the steward filed, run `gh issue edit N --add-label triaged`."
    assert self_triage_hits(line, "steward")
    assert not self_triage_hits(line, "lead-dev")


def test_planted_line_in_a_real_seat_file_fails():
    text = (SKILLS / "steward" / "SKILL.md").read_text(encoding="utf-8")
    assert not self_triage_hits(text, "steward")
    assert self_triage_hits(text + "\n\nThen `gh issue create --repo o/r --label triaged`.\n", "steward")


def test_legitimate_labelling_passes():
    fine = [
        ("steward", "On a bounded issue another lane raised, run `gh issue edit N --add-label triaged,bug`."),
        ("lead-dev", "A split child the steward filed names its parent; lead-dev runs "
                     "`gh issue edit N --add-label triaged,bug` on it."),
        ("steward", "File it with `gh issue create --repo o/r --label needs-decision`, without `triaged`."),
        ("steward", "`gh issue create --repo o/r` - never with `triaged` or `bug`."),
        ("steward", "A `triaged-later` label on `gh issue create --label triaged-later` is a different label."),
        ("steward", "`gh issue create --repo o/r --label <x>` then `ls -l triaged/` elsewhere."),
    ]
    for seat, line in fine:
        assert not self_triage_hits(line, seat), (seat, line)
