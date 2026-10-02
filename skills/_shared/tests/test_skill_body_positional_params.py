"""No bare dollar-digit in any first-party skill or command body (gh#502).

THE DEFECT. `skills/autodoc-update/SKILL.md` defined its run carrier's writer as
`env_put() { printf '%s=%q\\n' "$1" "$2" ...; }`. Loaded through the Skill tool with arguments,
the harness rewrote that `$1` to the invocation's argument before bash ever saw it, so every
`env_put` wrote `agentflow=<value>` instead of `NAME=<value>`. Each later block then sourced a
carrier that bound nothing it needed, and nothing reported it: an empty expansion is not an error.

WHAT THE HARNESS DOES, MEASURED rather than read from documentation (2026-09-23, a throwaway
probe skill invoked with two arguments). Documentation consulted for this fix said plain code
fences are exempt; the probe showed otherwise, so the probe is what this file encodes:

- a bare dollar-digit is rewritten to that argument, and it is 0-BASED - the zeroth digit is the
  FIRST argument;
- it is rewritten INSIDE fences too - backtick, indented and tilde fences alike;
- an index with no matching argument is left literal, so a block can look fine for one call
  shape and break for the next;
- the braced form `${1}` is left untouched, and bash reads it exactly as it reads the bare one,
  so it is correct whether the block is run from the rendered skill or from the file on disk;
- `\\$1` renders as a BARE dollar-digit: the harness strips the backslash. So a genuine bash
  escape - `awk "{print \\$1}"`, meant as a literal for awk - reaches bash rendered as a live
  positional that expands to nothing, and awk silently prints whole lines. The escaped form is
  flagged like the bare one; the braced form is the only spelling that reads the same whether the
  block is run from the rendered skill or from the file on disk.

THE CORPUS is every tracked first-party `SKILL.md` and `commands/*.md` - the files loaded as a
skill or command body - read from the git index (gh#175). Membership, not fences: the rewrite
applies to the whole body, so a prose `$1` is as live as a fenced one.

NOT COVERED: `skills/_vendored/` is excluded like every other first-party gate here. It deploys,
but nested under `_vendored/`, and first-party skills read it by relative path; it is never loaded
as a skill body, so the harness never renders it. Its one dollar-digit (`requesting-code-review`,
an awk field) is therefore not live. If a vendored skill is ever promoted to a top-level skill,
it enters this corpus by being a first-party `SKILL.md`.
"""

from __future__ import annotations

import re
from pathlib import Path

from agentflow.tracked_paths import tracked_paths

SUITE_ROOT = Path(__file__).resolve().parents[3]

NOT_FIRST_PARTY = re.compile(r"(^|/)(_vendored|\.venv|node_modules|\.git)(/|$)")

#: A dollar-digit the harness would rewrite. The braced form is the one spelling measured to pass
#: through untouched, and this pattern cannot match it. Everything else is flagged: the escaped
#: form renders bare (docstring), and a digit after `$$` or a word character was not measured.
BARE_POSITIONAL = re.compile(r"\$[0-9]")

#: The corpus is 35 files today (22 SKILL.md + 13 command wrappers). The floor sits just under
#: it, so a glob that has gone quiet - or silently dropped a handful of skills - cannot read as a
#: clean corpus. Lower it deliberately when skills are removed.
MIN_BODIES = 33


def _is_body(rel: str) -> bool:
    if NOT_FIRST_PARTY.search(rel):
        return False
    name = rel.rsplit("/", 1)[-1]
    return name == "SKILL.md" or (rel.endswith(".md") and "/commands/" in f"/{rel}")


def _bodies() -> list[str]:
    return sorted(rel for rel in tracked_paths(SUITE_ROOT) if _is_body(rel))


def _hits(text: str) -> list[tuple[int, str]]:
    return [
        (n, line.strip())
        for n, line in enumerate(text.splitlines(), 1)
        if BARE_POSITIONAL.search(line)
    ]


def test_the_predicate_fires_on_the_defect_and_spares_the_safe_forms() -> None:
    """The control. Runs first so a pattern that matches nothing is never read as a clean tree."""
    assert _hits("""env_put() { printf '%s=%q\\n' "$1" "$2"; }""")
    assert _hits('python - "$1" <<\'PY\'')
    assert _hits("zero=$0")
    assert _hits("cost$1") and _hits("pid=$$1"), "an unmeasured form was exempted"
    assert _hits('awk "{print \\$1}"'), "the escaped form renders bare and must be flagged"
    for safe in ('"${1}" "${2}"', "pid=$$.sh", "$ARGUMENTS", '"$@"'):
        assert not _hits(safe), safe
    # The scan, not only the pattern: a hit past a body's first line is found, at its line.
    body = "---\nname: x\n---\n\n```bash\nok \"${1}\"\nenv_put() { printf '%s' \"$1\"; }\n```\n"
    assert [n for n, _ in _hits(body)] == [7]


def test_the_corpus_is_the_whole_set_of_skill_and_command_bodies() -> None:
    bodies = _bodies()
    assert len(bodies) >= MIN_BODIES, f"only {len(bodies)} skill/command bodies found: {bodies}"
    assert "skills/autodoc-update/SKILL.md" in bodies
    assert any("/commands/" in f"/{b}" for b in bodies), "no command wrappers in the corpus"
    assert not any(NOT_FIRST_PARTY.search(b) for b in bodies)


def test_no_skill_or_command_body_carries_a_bare_positional_parameter() -> None:
    offenders = []
    for rel in _bodies():
        text = (SUITE_ROOT / rel).read_text(encoding="utf-8")
        offenders += [f"{rel}:{n}: {line}" for n, line in _hits(text)]
    assert not offenders, (
        "a bare dollar-digit in a skill or command body is rewritten to the invocation's argument "
        "(0-based, fences included) before bash sees it - write it braced, ${1}, which the harness "
        "leaves alone and bash reads identically (gh#502):\n" + "\n".join(offenders)
    )
