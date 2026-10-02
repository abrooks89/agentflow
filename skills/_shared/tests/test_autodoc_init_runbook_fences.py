"""The autodoc runbook fences that were true in prose and false when run (gh#594, gh#595, gh#596).

* gh#596 -- `communities.pins`: both runbooks called `graph communities ... $COMM_PINS` with the
  array built later (init) or hard-set to "" (update), and the real expansion lived in prose.
  Run top to bottom, pins were silently dropped. Pinned here on what the fence RUNS: the array is
  built before the call, and the call itself expands `"${COMM_PINS[@]}"`.
* gh#594 -- init's no-agent-writes gate ran with no `--stamp`, so a retry after a red Step 5 gate
  died on its own previous render. Init now uses update's stamp flow with update's exact flags.
* gh#595 -- init Step 6 claimed /autodoc-update STOPs on a rendered tree, and the command page
  advertised a repo argument and a stale agent count. The README graph-query showcase half lives
  in the graph suite (tests/test_readme_query_showcase.py), where the CLI's dependencies exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INIT = REPO_ROOT / "skills" / "autodoc-init" / "SKILL.md"
UPDATE = REPO_ROOT / "skills" / "autodoc-update" / "SKILL.md"
COMMAND = REPO_ROOT / "commands" / "autodoc-init.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _bash_blocks(text: str) -> str:
    out, buf, inside = [], [], False
    for line in text.split("\n"):
        s = line.strip()
        if not inside and s.startswith("```bash"):
            inside, buf = True, []
        elif inside and s.startswith("```"):
            out.append("\n".join(buf))
            inside = False
        elif inside:
            buf.append(line)
    return "\n".join(out)


def _strip_comments(text: str) -> str:
    """What the fence RUNS: a comment may quote any command, so it must never satisfy a pin."""
    out = []
    for line in text.split("\n"):
        q, cut = None, None
        for i, ch in enumerate(line):
            if q:
                if ch == q:
                    q = None
            elif ch in "'\"":
                q = ch
            elif ch == "#" and (i == 0 or line[i - 1].isspace()):
                cut = i
                break
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def _shell(p: Path) -> str:
    return _strip_comments(_bash_blocks(_read(p)))


# ------------------------------------------------------------------ gh#596


@pytest.mark.parametrize("skill", [INIT, UPDATE], ids=["init", "update"])
def test_comm_pins_is_built_before_the_communities_call_and_expanded_as_an_array(skill: Path) -> None:
    shell = _shell(skill)
    calls = list(re.finditer(r"^\s*python -m autodoc_graph\.cli communities .*$", shell, re.M))
    assert len(calls) == 1, f"{skill.name}: expected one `cli communities` call, found {len(calls)}"
    call = calls[0]
    assert call.group(0).rstrip().endswith('"${COMM_PINS[@]}"'), (
        f"{skill.name}: the communities call does not expand the pins array quoted; a bare "
        f"$COMM_PINS is only its first element (`--pin`), which the parser rejects: {call.group(0)}"
    )
    init_arr = re.search(r"^\s*COMM_PINS=\(\)", shell, re.M)
    append = re.search(r"COMM_PINS\+=\(--pin \"\$pin\"\)", shell)
    assert init_arr and append, f"{skill.name}: COMM_PINS is not built as an array in a runnable block"
    assert init_arr.start() < append.start() < call.start(), (
        f"{skill.name}: COMM_PINS is built AFTER the call that consumes it, so the call sees nothing"
    )
    # Every expansion is the quoted array form -- no bare `$COMM_PINS` anywhere it runs.
    bare = [m.group(0) for m in re.finditer(r"\$\{?COMM_PINS\b(?!\[@\]\})", shell)]
    assert not bare, f"{skill.name}: bare COMM_PINS expansion(s) remain: {bare}"
    # The profile read stops the run on failure rather than yielding zero pins.
    assert re.search(r"^PINS_TXT=\"\$\(python -c '", shell, re.M), skill.name
    assert re.search(r"^' \"\$PROJECT\"\)\" \|\| exit 1$", shell, re.M), (
        f"{skill.name}: a failed profile load is swallowed and the partition runs unpinned"
    )


@pytest.mark.parametrize("skill", [INIT, UPDATE], ids=["init", "update"])
def test_no_prose_asks_the_reader_to_substitute_the_pins_by_hand(skill: Path) -> None:
    text = _read(skill)
    assert not re.search(r"[Ss]ubstitute `?\"\$\{COMM_PINS\[@\]\}\"`? for", text), skill.name
    assert "substitute it for `$COMM_PINS`" not in text, skill.name
    assert 'COMM_PINS=""' not in text, f"{skill.name}: the pins are hard-set to empty again"


# ------------------------------------------------------------------ gh#594


def _gate_calls(shell: str) -> list[re.Match[str]]:
    return list(re.finditer(r"^\s*node \S*assert-no-agent-writes\.js\"? (.*)$", shell, re.M))


def _flags(args: str) -> list[str]:
    return sorted(re.findall(r"--[a-z-]+", args))


def test_init_uses_the_update_paths_stamp_flow_with_the_same_flags() -> None:
    init, update = _shell(INIT), _shell(UPDATE)
    init_calls, update_calls = _gate_calls(init), _gate_calls(update)
    assert len(init_calls) == 2, [m.group(0) for m in init_calls]
    # Same two invocations, same flags -- init adopts update's flow, it does not invent one.
    assert sorted(_flags(m.group(1)) for m in init_calls) == sorted(
        _flags(m.group(1)) for m in update_calls
    )
    check, stamp = init_calls
    assert check.group(1).startswith('"$RENDER_TARGET" --stamp "$STAMP" --clear-prior-render'), (
        "init's pre-render gate runs without the stamp, so a retry after a red gate dies on its "
        "own previous render: " + check.group(0)
    )
    assert stamp.group(1).startswith('--write-stamp "$RENDER_TARGET" --stamp "$STAMP"'), stamp.group(0)

    render = init.index("autodoc_graph.cli render ")
    assert check.start() < render < stamp.start(), (
        "the stamp must be written immediately after the render, and the gate must run before it"
    )
    between = init[render:stamp.start()]
    assert "autodoc_graph.cli" not in between.split("\n", 1)[1], (
        "something runs between `cli render` and --write-stamp; the stamp would record it too"
    )
    assert re.search(r"cli render .*\|\| exit 1$", init, re.M), (
        "a failed render falls through to --write-stamp and stamps a partial tree"
    )
    assigned = re.search(r'^\s*STAMP="\$RENDER_TARGET\.render-stamp\.json"', init, re.M)
    assert assigned and assigned.start() < check.start(), "STAMP is used before it is assigned"


def test_init_cutover_removes_the_stamp_with_the_tree_it_describes() -> None:
    init = _shell(INIT)
    mv = init.index('mv "$RENDER_TARGET" "$AUTODOC"')
    rm = re.search(r'rm -f "\$STAMP"', init)
    assert rm and rm.start() > mv, "the stamp outlives the render target it describes"


# ------------------------------------------------------------------ gh#595


def test_init_step_6_routes_the_next_refresh_to_the_update_records_path() -> None:
    text = _read(INIT)
    assert "autodoc-update-record-conversion" not in text
    assert "pre-flights on it and STOPS" not in text
    step6 = text[text.index("## Step 6 "):text.index("## Step 6.5")]
    assert ".rendered-by-store" in step6 and "STORE_PRIMARY=1" in step6
    # ...and that is what the update skill actually does.
    assert re.search(
        r'if \[ -f "\$AUTODOC/\.rendered-by-store" \]; then\s*\n\s*STORE_PRIMARY=1', _read(UPDATE)
    )


def test_command_page_matches_what_the_skill_accepts() -> None:
    text = _read(COMMAND)
    assert "<owner/repo>" not in text, "the skill takes REPO from the profile; there is no repo arg"
    assert not re.search(r"\bareas\b", text), "areas were retired; communities replaced them"
    ceiling = re.search(r"^\s*maxAgents:\s*(\d+)", _read(INIT), re.M)
    assert ceiling, "init SKILL.md no longer states maxAgents"
    counts = re.findall(r"up to (\d+) agents", text)
    assert counts == [ceiling.group(1)], (
        f"commands/autodoc-init.md states {counts} agents; the skill's ceiling is {ceiling.group(1)}"
    )
