"""The autodoc runbook fences that were true in prose and false when run (gh#594, gh#595, gh#596,
gh#592).

* gh#596 -- `communities.pins`: both runbooks called `graph communities ... $COMM_PINS` with the
  array built later (init) or hard-set to "" (update), and the real expansion lived in prose.
  Run top to bottom, pins were silently dropped. Pinned here on what the fence RUNS: the array is
  built before the call, and the call itself expands `"${COMM_PINS[@]}"`.
* gh#594 -- init's no-agent-writes gate ran with no `--stamp`, so a retry after a red Step 5 gate
  died on its own previous render. Init now uses update's stamp flow with update's exact flags.
* gh#595 -- init Step 6 claimed /autodoc-update STOPs on a rendered tree, and the command page
  advertised a repo argument and a stale agent count. The README graph-query showcase half lives
  in the graph suite (tests/test_readme_query_showcase.py), where the CLI's dependencies exist.
* gh#592 -- init Step 5c always ran the symmetric page-set budget, which a first build (empty
  baseline) can never meet. The fence now counts baseline content pages and picks the mode;
  the fence is executed against temp dirs, not only read.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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


# ------------------------------------------------------------------ gh#592
# A first /autodoc-init build compares its render against an EMPTY baseline, so every page is
# "only-rendered" and the symmetric budget fails it by construction. Step 5c picks the mode:
# zero baseline content pages (counted by the gate's own loader, nav excluded) -> lost-only;
# otherwise the symmetric budget is unchanged. The fence is RUN, not just read, with `/tmp/`
# rewritten into the test's scratch dir so a live run's pointer and config are never touched.

INVENTORY = REPO_ROOT / "skills" / "autodoc-init" / "references" / "inventory"


def _step_5c_fence() -> str:
    text = _read(INIT)
    section = text[text.index("### 5c "):text.index("### 5d ")]
    opens = [ln for ln in section.split("\n") if ln.strip().startswith("```bash")]
    assert len(opens) == 1, f"Step 5c holds {len(opens)} bash fences; expected exactly one"
    return _bash_blocks(section)


def test_step_5c_fence_counts_the_baseline_with_the_gates_loader_and_picks_the_budget() -> None:
    shell = _strip_comments(_step_5c_fence())
    assert re.search(r"listPages\([^)]*\{\s*excludeNav:\s*true\s*\}\s*\)", shell), (
        "Step 5c must count baseline content pages with listPages(..., {excludeNav: true}), the "
        "loader golden-gate.js compares, or 'empty' means something different to each of them"
    )
    assert '{ "pageSetLostBudget": 0 }' in shell, "the first-build config is not written"
    assert '{ "pageSetSymmetricDiffBudget": 12 }' in shell, "the re-init config is not written"
    assert re.search(
        r'^\s*node "\S*golden-gate\.js" "\$RENDER_TARGET" "\$AUTODOC" /tmp/golden-gate\.json ',
        shell, re.M,
    ), "the config Step 5c writes is not the one it hands to golden-gate.js"


def _yaml_path(p: Path) -> str:
    return str(p).replace("\\", "/")


def _bash() -> str:
    """Git's own bash first; a bare PATH lookup on Windows finds the WSL launcher."""
    candidates: list[str] = []
    git = shutil.which("git")
    if git:
        for base in list(Path(git).resolve().parents)[:3]:
            for rel in ("bin/bash.exe", "usr/bin/bash.exe", "bin/bash"):
                c = base / rel
                if c.is_file() and str(c) not in candidates:
                    candidates.append(str(c))
    found = shutil.which("bash")
    if found and "system32" not in found.lower() and found not in candidates:
        candidates.append(found)
    if not candidates:
        pytest.skip("no usable bash found (Git for Windows ships one)")
    return candidates[0]


def _service_page(title: str) -> str:
    return (
        f'---\ntype: "service"\ntitle: "{title}"\ncreated: "2026-08-01"\nupdated: "2026-08-07"\n'
        f'source_repo: "o/r"\nsource_paths:\n  - "app/x.py"\nstatus: "active"\n'
        f'service_kind: "api"\npath: "app/"\nlanguage: "python"\nentrypoint: "app/x.py"\n---\n\n'
        f"{title} summary.\n"
    )


def _run_5c(tmp_path: Path, baseline_pages: list[str]) -> tuple[subprocess.CompletedProcess[str], Path]:
    if not shutil.which("node"):
        pytest.skip("node is not on PATH; the static Step 5c pin above still runs")
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    rendered, baseline = tmp_path / "rendered", tmp_path / "baseline"
    for d in (rendered / "services", baseline / "services"):
        d.mkdir(parents=True)
    # Nav pages only: a scaffolded corpus that was never built still holds these.
    for nav in ("CLAUDE.md", "log.md", "index.md"):
        (baseline / nav).write_text("# nav\n", encoding="utf-8")
    for name in baseline_pages:
        (baseline / "services" / f"{name}.md").write_text(_service_page(name), encoding="utf-8")
    for name in [*baseline_pages, *(f"New{i}" for i in range(1, 14))]:
        (rendered / "services" / f"{name}.md").write_text(_service_page(name), encoding="utf-8")
    carrier = scratch / "carrier.env"
    carrier.write_text(
        "".join(
            f"{k}='{_yaml_path(v) if isinstance(v, Path) else v}'\n"
            for k, v in {"SUITE_ROOT": REPO_ROOT, "RENDER_TARGET": rendered, "AUTODOC": baseline,
                         "SRC": "-", "INV": "-"}.items()
        ),
        encoding="utf-8", newline="\n",
    )
    (scratch / "autodoc-env.current").write_text(_yaml_path(carrier) + "\n", encoding="utf-8", newline="\n")
    prefix = _yaml_path(scratch) + "/"
    body = _step_5c_fence().replace("/tmp/", prefix)
    assert not re.search(r"(?<![\w.-])/tmp/", body.replace(prefix, "")), "a /tmp/ path survived the rewrite"
    script = tmp_path / "step5c.sh"
    script.write_text(body + "\n", encoding="utf-8", newline="\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("SUITE_ROOT", "RENDER_TARGET", "AUTODOC", "SRC", "INV", "AUTODOC_ENV")}
    proc = subprocess.run(
        [_bash(), str(script)], env=env, capture_output=True, text=True, encoding="utf-8",
        timeout=120, stdin=subprocess.DEVNULL,
    )
    return proc, scratch / "golden-gate.json"


def test_step_5c_first_build_runs_lost_only_and_passes(tmp_path: Path) -> None:
    proc, config_path = _run_5c(tmp_path, baseline_pages=[])
    out = proc.stdout + proc.stderr
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"pageSetLostBudget": 0}, out
    assert re.search(r"lost-only.*0 content pages", proc.stdout), f"the mode and its reason are not printed:\n{out}"
    assert "PASS page-set: 0 lost <= budget 0; 13 created" in proc.stdout, out
    assert "golden-gate: PASS" in proc.stdout, out
    assert proc.returncode == 0, out


def test_step_5c_reinit_over_a_populated_tree_keeps_the_symmetric_budget(tmp_path: Path) -> None:
    proc, config_path = _run_5c(tmp_path, baseline_pages=["One", "Two", "Three"])
    out = proc.stdout + proc.stderr
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"pageSetSymmetricDiffBudget": 12}, out
    assert re.search(r"symmetric.*3 content pages", proc.stdout), f"the mode and its reason are not printed:\n{out}"
    assert "FAIL page-set: symmetric difference 13 > budget 12" in proc.stdout, out
    assert proc.returncode == 1, out


# ------------------------------------------------------------------ gh#621
# Two fences that read right and broke when run twice or off GitHub. Step 6 moved a re-init's live
# tree INSIDE the first cutover's backup, the `.source` move then failed and the block kept going,
# so the live tree lost its clone and got the first run's log back. Step 1 always ran `gh repo
# clone`, whatever host the profile names. Both fences are RUN here, with `/tmp/` rewritten into
# the test's scratch dir as above.


def _section_fence(start: str, end: str) -> str:
    text = _read(INIT)
    section = text[text.index(start):text.index(end)]
    opens = [ln for ln in section.split("\n") if ln.strip().startswith("```bash")]
    assert len(opens) == 1, f"{start!r} holds {len(opens)} bash fences; expected exactly one"
    return _bash_blocks(section)


def _run_fence(
    tmp_path: Path, body: str, values: dict[str, str | Path], *, path_prefix: Path | None = None,
    carrier_extra: str = "",
) -> subprocess.CompletedProcess[str]:
    scratch = tmp_path / "tmp"
    scratch.mkdir(exist_ok=True)
    carrier = scratch / "carrier.env"
    lines = [f"{k}='{_yaml_path(v) if isinstance(v, Path) else v}'\n" for k, v in values.items()]
    lines.append(f"AUTODOC_ENV='{_yaml_path(carrier)}'\n")
    lines.append("env_put() { printf '%s=%q\\n' \"${1}\" \"${2}\" >> \"$AUTODOC_ENV\"; }\n")
    lines.append(carrier_extra)
    carrier.write_text("".join(lines), encoding="utf-8", newline="\n")
    (scratch / "autodoc-env.current").write_text(_yaml_path(carrier) + "\n", encoding="utf-8", newline="\n")
    prefix = _yaml_path(scratch) + "/"
    shield = "\0SCRATCH\0"
    script_body = body.replace(_yaml_path(tmp_path), shield).replace("/tmp/", prefix)
    assert not re.search(r"(?<![\w.-])/tmp/", script_body.replace(prefix, "")), "a /tmp/ path survived the rewrite"
    script_body = script_body.replace(shield, _yaml_path(tmp_path))
    script = tmp_path / f"fence-{len(list(tmp_path.glob('fence-*.sh')))}.sh"
    script.write_text(script_body + "\n", encoding="utf-8", newline="\n")
    env = {k: v for k, v in os.environ.items()
           if k not in (*values, "AUTODOC_ENV") and not k.startswith("GIT_")}
    if path_prefix is not None:
        env["PATH"] = str(path_prefix) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        [_bash(), str(script)], env=env, capture_output=True, text=True, encoding="utf-8",
        timeout=120, stdin=subprocess.DEVNULL,
    )


def _carrier_value(tmp_path: Path, key: str) -> str | None:
    found = None
    for line in (tmp_path / "tmp" / "carrier.env").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            found = line.split("=", 1)[1].strip("'")
    return found


# --- Step 6 cutover


def _live_tree(root: Path, log: str, *, with_source: bool = True) -> None:
    root.mkdir(parents=True)
    for name, body in (("log.md", log), ("hot.md", "## Latest\n"), ("CLAUDE.md", "# c\n"),
                       (".gitignore", ".source/\n")):
        (root / name).write_text(body, encoding="utf-8")
    if with_source:
        (root / ".source" / "repo" / ".git").mkdir(parents=True)
        (root / ".source" / "graph.db").write_text("db\n", encoding="utf-8")


def _render(root: Path, tag: str) -> Path:
    (root / "services").mkdir(parents=True)
    (root / "services" / "Alpha.md").write_text(_service_page("Alpha"), encoding="utf-8")
    (root / ".rendered-by-store").write_text(tag, encoding="utf-8")
    stamp = root.parent / f"{root.name}.render-stamp.json"
    stamp.write_text("{}\n", encoding="utf-8")
    return stamp


def _cutover(
    tmp_path: Path, autodoc: Path, render: Path, stamp: Path, *, stubs: str = "",
) -> subprocess.CompletedProcess[str]:
    body = _section_fence("## Step 6 ", "## Step 6.5")
    return _run_fence(tmp_path, body, {"AUTODOC": autodoc, "RENDER_TARGET": render, "STAMP": stamp},
                      carrier_extra=stubs)


def _refusing(cmd: str, case_pattern: str) -> str:
    """A shell function, defined in the carrier the fence sources first, that makes `cmd` fail
    when its argument list matches `case_pattern` and otherwise runs the real command. A function
    rather than a PATH stub: Git for Windows' bash can put /usr/bin ahead of a PATH prefix."""
    return (f'{cmd}() {{ case "$*" in {case_pattern}) echo "stub {cmd} refused: $*" >&2; return 1;; esac; '
            f'command {cmd} "$@"; }}\n')


def _backups(parent: Path, autodoc: Path) -> list[Path]:
    return sorted(p for p in parent.iterdir()
                  if p.name.startswith(autodoc.name + ".pre-store") and p.name.endswith(".bak"))


def test_step_6_cutover_twice_keeps_the_clone_and_the_latest_log_live(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _live_tree(corpus, "scaffold-log\n")

    render1 = tmp_path / "render1"
    first = _cutover(tmp_path, corpus, render1, _render(render1, "render1"))
    out1 = first.stdout + first.stderr
    assert first.returncode == 0, out1
    assert (corpus / ".source" / "repo" / ".git").is_dir(), out1
    assert (corpus / "log.md").read_text(encoding="utf-8") == "scaffold-log\n", out1
    # Step 6.5 appends this run's entry to the live log before the next re-init.
    (corpus / "log.md").write_text("run1-log\n", encoding="utf-8")

    render2 = tmp_path / "render2"
    second = _cutover(tmp_path, corpus, render2, _render(render2, "render2"))
    out2 = second.stdout + second.stderr
    assert second.returncode == 0, out2
    assert (corpus / ".rendered-by-store").read_text(encoding="utf-8") == "render2", out2
    assert (corpus / ".source" / "repo" / ".git").is_dir(), f"the re-init lost its clone:\n{out2}"
    assert (corpus / ".source" / "graph.db").is_file(), out2
    assert (corpus / "log.md").read_text(encoding="utf-8") == "run1-log\n", (
        f"the re-init restored an older log.md, not the one that was live:\n{out2}"
    )

    backups = _backups(tmp_path, corpus)
    assert len(backups) == 2, f"expected two sibling backups, got {[b.name for b in backups]}:\n{out2}"
    first_bak = tmp_path / "corpus.pre-store.bak"
    assert first_bak in backups, [b.name for b in backups]
    assert not (first_bak / corpus.name).exists(), "the second backup was nested inside the first"
    assert (first_bak / "log.md").read_text(encoding="utf-8") == "scaffold-log\n"
    (second_bak,) = [b for b in backups if b != first_bak]
    assert (second_bak / "log.md").read_text(encoding="utf-8") == "run1-log\n"
    assert not render1.exists() and not render2.exists()


def test_step_6_failed_source_move_stops_before_the_copy_loop(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _live_tree(corpus, "baseline-log\n", with_source=False)
    render = tmp_path / "render"
    stamp = _render(render, "render")
    proc = _cutover(tmp_path, corpus, render, stamp)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"a failed .source move did not stop the cutover:\n{out}"
    assert ".source" in proc.stderr, out
    # The baseline is put back, so the corpus a reader resolves is never a tree with no clone.
    assert (corpus / "log.md").read_text(encoding="utf-8") == "baseline-log\n", out
    assert not (corpus / ".rendered-by-store").exists(), out
    assert (render / ".rendered-by-store").is_file(), out
    assert _backups(tmp_path, corpus) == [], out


def _assert_baseline_untouched(corpus: Path, out: str) -> None:
    assert (corpus / "log.md").read_text(encoding="utf-8") == "baseline-log\n", out
    assert (corpus / ".source" / "repo" / ".git").is_dir(), out
    assert (corpus / ".source" / "graph.db").is_file(), out
    assert not (corpus / ".rendered-by-store").exists(), out
    assert not (corpus / "services").exists(), out


def test_step_6_same_second_backup_collision_is_fatal(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _live_tree(corpus, "baseline-log\n")
    render = tmp_path / "render"
    stamp = _render(render, "render")
    frozen = "20260101T000000Z"
    (tmp_path / "corpus.pre-store.bak").mkdir()
    taken = tmp_path / f"corpus.pre-store.{frozen}.bak"
    taken.mkdir()
    (taken / "log.md").write_text("older-backup\n", encoding="utf-8")
    # The fence's only `date` call is the backup stamp; freezing it makes the collision certain.
    proc = _cutover(tmp_path, corpus, render, stamp, stubs=f"date() {{ echo {frozen}; }}\n")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "taken" in proc.stderr, out
    _assert_baseline_untouched(corpus, out)
    assert sorted(p.name for p in taken.iterdir()) == ["log.md"], f"a taken backup was written into:\n{out}"
    assert (taken / "log.md").read_text(encoding="utf-8") == "older-backup\n", out
    assert (render / ".rendered-by-store").is_file(), out


def test_step_6_failed_baseline_move_stops_before_anything_moves_in(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _live_tree(corpus, "baseline-log\n")
    render = tmp_path / "render"
    stamp = _render(render, "render")
    proc = _cutover(tmp_path, corpus, render, stamp, stubs=_refusing("mv", "*.pre-store.bak"))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "stub mv refused" in proc.stderr, out
    _assert_baseline_untouched(corpus, out)
    assert sorted(p.name for p in render.iterdir()) == [".rendered-by-store", "services"], (
        f"the render moved although the baseline did not:\n{out}"
    )
    assert stamp.is_file(), out
    assert _backups(tmp_path, corpus) == [], out


def test_step_6_failed_render_move_restores_the_baseline(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _live_tree(corpus, "baseline-log\n")
    render = tmp_path / "render"
    stamp = _render(render, "render")
    shutil.rmtree(render)
    proc = _cutover(tmp_path, corpus, render, stamp)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    _assert_baseline_untouched(corpus, out)
    assert _backups(tmp_path, corpus) == [], f"a backup was left behind:\n{out}"
    assert stamp.is_file(), out


def test_step_6_failed_copy_stops_the_cutover(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    _live_tree(corpus, "baseline-log\n")
    render = tmp_path / "render"
    stamp = _render(render, "render")
    proc = _cutover(tmp_path, corpus, render, stamp, stubs=_refusing("cp", '*/log.md" "*'))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "stub cp refused" in proc.stderr, out
    assert "cutover done" not in proc.stdout, out
    assert not (corpus / "hot.md").exists(), f"the copy loop kept going after a failed cp:\n{out}"


# --- Step 1 clone


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _origin_and_checkout(tmp_path: Path) -> tuple[Path, Path, str]:
    if not shutil.which("git"):
        pytest.skip("git is not on PATH")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", str(seed))
    (seed / "app.py").write_text("print(1)\n", encoding="utf-8")
    _git("add", "app.py", cwd=seed)
    _git("commit", "-q", "-m", "one", cwd=seed)
    origin = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(seed), str(origin))
    checkout = tmp_path / "checkout"
    _git("clone", "-q", origin.as_uri(), str(checkout))
    return origin, checkout, _git("rev-parse", "HEAD", cwd=seed)


def _gh_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A gh that records its argv and fails: proof of whether the fence reached for it."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    marker = tmp_path / "gh-called"
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\necho \"$*\" >> '{_yaml_path(marker)}'\nexit 1\n", encoding="utf-8", newline="\n")
    gh.chmod(0o755)
    return bin_dir, marker


def _clone(tmp_path: Path, kind: str, checkout: Path) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    autodoc = tmp_path / "corpus"
    autodoc.mkdir()
    bin_dir, marker = _gh_stub(tmp_path)
    body = _section_fence("## Step 1 ", "## Step 2 ")
    proc = _run_fence(
        tmp_path, body,
        {"AUTODOC": autodoc, "REPO": "owner/app", "BRANCH": "main", "REPO_STEM": "app",
         "GIT_HOST_KIND": kind, "LOCAL_CHECKOUT": checkout},
        path_prefix=bin_dir,
    )
    return proc, autodoc / ".source" / "app", marker


def test_step_1_clones_a_non_github_origin_without_calling_gh(tmp_path: Path) -> None:
    _, checkout, head = _origin_and_checkout(tmp_path)
    proc, src, marker = _clone(tmp_path, "gitea", checkout)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert not marker.exists(), f"gh was called on a gitea profile: {marker.read_text(encoding='utf-8')}"
    assert (src / ".git").exists(), out
    assert _git("-C", str(src), "rev-parse", "HEAD") == head
    assert _git("-C", str(src), "remote", "get-url", "origin") == _git(
        "-C", str(checkout), "remote", "get-url", "origin"), "the clone did not come from the checkout's origin"
    assert _git("-C", str(src), "rev-parse", "--is-shallow-repository") == "true", "the clone is not --depth 1"
    assert _carrier_value(tmp_path, "COMMIT") == head, out


@pytest.mark.parametrize("origin_state", ["none", "unreachable"])
def test_step_1_falls_back_to_the_local_checkout(tmp_path: Path, origin_state: str) -> None:
    _, checkout, head = _origin_and_checkout(tmp_path)
    if origin_state == "none":
        _git("-C", str(checkout), "remote", "remove", "origin")
    else:
        _git("-C", str(checkout), "remote", "set-url", "origin", (tmp_path / "gone.git").as_uri())
    proc, src, marker = _clone(tmp_path, "local", checkout)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert not marker.exists(), "gh was called on a local profile"
    assert (src / ".git").exists(), out
    assert _git("-C", str(src), "rev-parse", "HEAD") == head
    # A bare local path ignores --depth; only the file:// URL gives a shallow clone.
    assert _git("-C", str(src), "rev-parse", "--is-shallow-repository") == "true", (
        f"the fallback did not clone through file://:\n{out}"
    )
    assert _carrier_value(tmp_path, "COMMIT") == head, out


def test_step_1_still_tries_gh_for_github_and_falls_through_when_it_fails(tmp_path: Path) -> None:
    _, checkout, head = _origin_and_checkout(tmp_path)
    proc, src, marker = _clone(tmp_path, "github", checkout)
    out = proc.stdout + proc.stderr
    assert marker.exists(), f"a github profile with gh on PATH never tried gh:\n{out}"
    assert "repo clone owner/app" in marker.read_text(encoding="utf-8")
    assert proc.returncode == 0, out
    assert _git("-C", str(src), "rev-parse", "HEAD") == head


def test_step_1_existing_clone_whose_origin_is_gone_fails(tmp_path: Path) -> None:
    origin, checkout, _ = _origin_and_checkout(tmp_path)
    autodoc = tmp_path / "corpus"
    src = autodoc / ".source" / "app"
    src.parent.mkdir(parents=True)
    _git("clone", "-q", origin.as_uri(), str(src))
    _git("-C", str(src), "remote", "set-url", "origin", (tmp_path / "gone.git").as_uri())
    body = _section_fence("## Step 1 ", "## Step 2 ")
    proc = _run_fence(
        tmp_path, body,
        {"AUTODOC": autodoc, "REPO": "owner/app", "BRANCH": "main", "REPO_STEM": "app",
         "GIT_HOST_KIND": "local", "LOCAL_CHECKOUT": checkout},
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"a fetch that failed kept the stale clone and carried on:\n{out}"
    assert _carrier_value(tmp_path, "COMMIT") is None, out
