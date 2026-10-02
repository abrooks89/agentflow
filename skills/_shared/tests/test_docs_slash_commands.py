"""Every slash command written in `docs/` resolves to a file in `commands/`.

gh#205 found `/project` written three times in the binding schema doc as an invocable entry point,
with no `commands/project.md` behind it — the second measured instance of one class: `docs/` and
`commands/` are two surfaces that drift with nothing comparing them.

WHY THE OBVIOUS GATE WOULD BE WRONG. "Every skill has a command file" is the rule that suggests
itself, and it is false: measured at `529105b`, NINE skills have no command file
(asking-questions, backlog, choose-next, orchestrator, polish-mode, project, refine, solution,
tdd-loop). Most skills are invoked as skills. That gate would demand nine files nobody wants, and
would have "fixed" gh#205 by inventing a surface instead of correcting a sentence.

The invariant runs one way only: **a doc that tells a reader to type `/x` promises `/x` exists.**

HOW A COMMAND IS TOLD FROM A PATH, and why it is not a punctuation rule. An earlier version of this
gate tried to exclude paths with lookaround on the slash, and reported eleven false MISSes on its
first run — `~/agentflow`, `<data_root>/projects`, `wiki/pages{,/archived}` and friends. The
discriminator that actually holds is how these docs write things: **a command is a code span whose
ENTIRE content is `/name`.** A path always carries more inside the span. Measured against the whole
of `docs/`, that pattern yields fourteen references and no path.
"""
import re
from pathlib import Path

import pytest
from agentflow.tracked_paths import only_tracked

_ROOT = Path(__file__).resolve().parents[3]
_DOCS = _ROOT / "docs"
_COMMANDS = _ROOT / "commands"

#: Real commands this repository does not own. The reason is part of the entry: an allowlist whose
#: members carry no justification is where failures go to be forgotten.
_EXTERNAL = {
    "exit": "a Claude Code built-in the reader types at their own terminal (docs/INSTALL.md)",
}

#: References the docs make ONLY to say the command does not exist. Excluding these is not a
#: loophole — a sentence reading "no `/install` command — the file is read by path" is the gate's
#: own conclusion written in prose, and firing on it would punish the doc for being correct.
_NAMED_AS_ABSENT = {
    "install": "docs/INSTALL.md states there is no /install command; the file is read by path",
}

#: A code span whose entire content is `/name`.
_SLASH = re.compile(r"`(/[a-z][a-z0-9-]{2,})`")


def _docs_md():
    """Every markdown file under docs/ THAT THE INDEX KNOWS ABOUT (gh#175).

    A tree walk here would let an untracked scratch note under `docs/` invent a command
    reference, and would do it with `git status` clean and CI green - CI checks out tracked
    files only.
    """
    return only_tracked(_ROOT, _DOCS.rglob("*.md"))


def _referenced():
    """{command: [doc files that write it]} across every markdown file under docs/."""
    found = {}
    for md in _docs_md():
        for hit in set(_SLASH.findall(md.read_text(encoding="utf-8"))):
            found.setdefault(hit.lstrip("/"), []).append(md.relative_to(_ROOT).as_posix())
    return found


def test_the_corpus_this_gate_reads_is_not_empty():
    """A guard on the guard. If the glob stops matching, every assertion below passes over an
    empty set and the gate reports success while checking nothing."""
    assert _DOCS.is_dir(), "docs/ not found from %s" % _ROOT
    assert _docs_md(), "no tracked markdown under docs/ — this gate would be vacuous"


def test_the_pattern_still_finds_commands_that_really_resolve():
    """The same guard on the pattern rather than the glob: `/lead-dev` is written in docs and does
    resolve, so if the regex stops matching it, every MISS below vanishes too and the gate goes
    quiet rather than red."""
    found = _referenced()
    assert "lead-dev" in found, (
        "the pattern matched no `/lead-dev` anywhere in docs/, so it has stopped finding real "
        "references and this gate is no longer checking anything"
    )
    assert (_COMMANDS / "lead-dev.md").is_file(), "the control command file is gone"


def test_every_slash_command_written_in_docs_resolves_to_a_command_file():
    """gh#205's durable half."""
    exempt = set(_EXTERNAL) | set(_NAMED_AS_ABSENT)
    missing = [
        "/%s  written in %s" % (name, ", ".join(files))
        for name, files in sorted(_referenced().items())
        if name not in exempt and not (_COMMANDS / (name + ".md")).is_file()
    ]
    assert not missing, (
        "docs promise slash commands that do not resolve to commands/<name>.md:\n  "
        + "\n  ".join(missing)
        + "\n\nEither add the command file, or stop writing it as `/name`. Most skills are invoked "
          "as skills and correctly have no command file, so a missing file is not automatically "
          "the defect — gh#205 was fixed by correcting the prose."
    )


def test_no_exemption_has_gone_stale():
    """An allowlisted name that starts resolving is an entry that should be deleted. Without this
    the two sets only grow, and each stale member silently un-checks one real command."""
    stale = sorted(n for n in (set(_EXTERNAL) | set(_NAMED_AS_ABSENT))
                   if (_COMMANDS / (n + ".md")).is_file())
    assert not stale, (
        "exempted as absent or external, but a command file now exists — delete the exemption: "
        + ", ".join(stale)
    )


def test_every_exemption_is_still_referenced_somewhere_in_docs():
    """The other direction. An exemption for a name no doc mentions any more is dead weight that
    reads as coverage."""
    referenced = set(_referenced())
    orphans = sorted(n for n in (set(_EXTERNAL) | set(_NAMED_AS_ABSENT)) if n not in referenced)
    assert not orphans, (
        "exempted but no longer written anywhere in docs/ — delete the exemption: "
        + ", ".join(orphans)
    )


@pytest.mark.parametrize("span", [
    "`~/agentflow`",
    "`<data_root>/projects/<name>/autodoc`",
    "`~/.agentflow/config/projects/<name>.md`",
    "`<slug>/status.md`",
    "`wiki/pages{,/done,/archived}`",
    "`skills/_shared/tests`",
    "`/tmp/autodoc-changed.txt`",
])
def test_a_path_inside_a_code_span_is_not_read_as_a_command(span):
    """The direction that produces false failures. Every one of these appears in docs/ today, and
    the lookaround version of this gate reported each as a missing command."""
    assert not _SLASH.findall(span), "%s was read as a slash command" % span


def test_a_real_command_reference_IS_matched():
    """The direction that produces false passes: a pattern matching nothing satisfies the gate
    above perfectly."""
    assert _SLASH.findall("run `/dev-manager` to open the pump") == ["/dev-manager"]
    assert _SLASH.findall("`/autodoc-update` then `/report`") == ["/autodoc-update", "/report"]
