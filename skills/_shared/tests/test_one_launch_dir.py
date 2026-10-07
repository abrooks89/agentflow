"""Tests for the one-launch-directory contract — worktree_root and repo-rule injection.

A lane is launched from the machine's single launch directory, so it sits in no repo:
cwd traversal finds neither a worktree to build in nor a repo ``CLAUDE.md`` to obey. Both
of those therefore have to arrive as CONFIG. These tests gate the two halves:

- ``worktree_root`` is a REQUIRED environment field, never defaulted. A silently
  defaulted worktree path is how a lane ends up building somewhere nobody chose.
- ``render_project_claude_md`` emits the resolved project's repo instructions, and the
  SessionStart hook and ``python -m agentflow.config show`` emit the SAME bytes, so a
  session and an operator inspecting that session can never disagree about what rules a
  lane is bound by.

Everything here runs against a TEMP config home. Nothing reads the live one — a test that
asserted against the machine's real config would pass or fail on that machine's state
rather than on this code.
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentflow import config
from agentflow.config import ConfigError, load_environment, load_project

# tests/ -> _shared/ -> skills/ -> suite root. Derived, never hard-coded: the suite is
# checked out under a different path on every machine and in every worktree.
_SHARED_ROOT = Path(__file__).resolve().parents[1]
_SUITE_ROOT = _SHARED_ROOT.parents[1]
_HOOK = _SUITE_ROOT / "hooks" / "sessionstart-inject-config.py"

# A body distinctive enough that finding it in a stream proves the FILE was emitted, not
# a lookalike header the renderer happened to print.
#
# The arrow is load-bearing, not decoration. A repo CLAUDE.md is arbitrary prose, and
# U+2192 has no cp1252 mapping — the encoding a Windows console hands a child process on
# a piped stdout. So an emitter that does not force UTF-8 dies with UnicodeEncodeError
# mid-block instead of printing this body, and the subprocess tests below catch it on the
# returncode. An ASCII-only fixture would let that regression through: every character
# would encode fine under cp1252 and the tests would stay green while the real feature
# crashed every session on Windows. (On a UTF-8 platform this costs nothing and proves
# nothing; the Windows CI leg is where it bites.)
_REPO_RULES = "PROJECT RULE: branch → commit → PR. marker-9f3a-repo-claude-md\n"


# --------------------------------------------------------------------------- #
# Helpers — same shape as test_config.py's, so the two files stay readable together
# --------------------------------------------------------------------------- #
def _write_env(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "environment.md").write_text(
        f"# environment\n\n```yaml\n{textwrap.dedent(body).strip()}\n```\n",
        encoding="utf-8",
    )


def _write_project(home: Path, name: str, body: str) -> None:
    pdir = home / "projects"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{name}.md").write_text(
        f"# project {name}\n\n```yaml\n{textwrap.dedent(body).strip()}\n```\n",
        encoding="utf-8",
    )


_OMIT = object()  # distinguishes "leave the field out" from "set it to None"


def _env_body(d, worktree_root=_OMIT):
    """A minimal valid environment body.

    ``worktree_root`` defaults to ``d``; pass ``None`` to OMIT the field entirely (the
    missing-required-key case) or a path string to point it somewhere specific.
    """
    if worktree_root is _OMIT:
        worktree_root = d
    wt = "" if worktree_root is None else f"\n    worktree_root: {worktree_root}"
    return f"""
    schema_version: 1
    person:
      name: Sample User
    data_root: {d}
    git_host:
      kind: local
    notify:
      kind: none
    lock_dir: {d}
    shell: posix
    suite_root: {d}{wt}
    """


def _project_body(name: str, checkout: str) -> str:
    return f"""
    schema_version: 1
    name: {name}
    canonical_repo:
      host_ref: owner/repo
      local_checkout: {checkout}
    verification:
      strategy: test-suite
    """


def _mkdir(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    return str(p).replace("\\", "/")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "config"
    h.mkdir()
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(h))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    return h


@pytest.fixture
def d(tmp_path):
    # an existing directory usable for every required path field
    return str(tmp_path).replace("\\", "/")


# --------------------------------------------------------------------------- #
# A1 — worktree_root is required environment config, not an implicit cwd
# --------------------------------------------------------------------------- #
def test_environment_without_worktree_root_hard_fails(home, d):
    """Omitting the field must be loud. If this ever defaults, a lane silently builds in
    whatever directory it was launched from — the exact failure the epic removes."""
    _write_env(home, _env_body(d, worktree_root=None))
    with pytest.raises(ConfigError) as ei:
        load_environment()
    e = ei.value
    assert e.field_path == "worktree_root"
    assert "worktree_root" in e.remediation
    assert "remediation:" in str(e)


def test_environment_resolves_worktree_root(home, d):
    _write_env(home, _env_body(d))
    env = load_environment()
    # The resolver returns the expanded, platform-normalised path, not the raw literal.
    assert env.worktree_root == str(Path(d).expanduser())


def test_worktree_root_must_exist(home, d, tmp_path):
    """Path existence is checked at resolve time, like every other required dir. A
    worktree root that is only a plausible string fails at first `git worktree add`,
    hours later and far from the cause."""
    missing = str(tmp_path / "no-such-worktree-root").replace("\\", "/")
    _write_env(home, _env_body(d, worktree_root=missing))
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "worktree_root"
    assert "does not exist" in ei.value.reason


def test_worktree_root_must_be_a_directory(home, d, tmp_path):
    f = tmp_path / "worktree-root-is-a-file"
    f.write_text("not a directory\n", encoding="utf-8")
    _write_env(home, _env_body(d, worktree_root=str(f).replace("\\", "/")))
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "worktree_root"
    assert "not a directory" in ei.value.reason


def test_worktree_root_is_rendered_in_the_resolved_block(home, d):
    """Downstream skills read the injected block rather than re-resolving config, so a
    field that never reaches the block is a field they cannot use."""
    _write_env(home, _env_body(d))
    block = config.render_resolved_block(load_environment(), None)
    assert f"worktree_root = {Path(d).expanduser()}" in block


# --------------------------------------------------------------------------- #
# A2 — the resolved project's repo CLAUDE.md becomes project config
# --------------------------------------------------------------------------- #
def test_project_claude_md_body_is_emitted(home, d, tmp_path):
    checkout = _mkdir(tmp_path, "checkout-alpha")
    (Path(checkout) / "CLAUDE.md").write_text(_REPO_RULES, encoding="utf-8")
    _write_env(home, _env_body(d))
    _write_project(home, "alpha", _project_body("alpha", checkout))

    out = config.render_project_claude_md(load_project("alpha"))
    assert _REPO_RULES.strip() in out
    lines = out.split("\n")
    assert lines[0].startswith("=== alpha repo CLAUDE.md (")
    assert lines[-1] == "=" * len(lines[0])  # rule-off matches the header width


def test_project_without_claude_md_emits_nothing(home, d, tmp_path):
    """A project need not carry repo instructions. That is a normal case, not a fault —
    emit nothing so the session block stays clean."""
    checkout = _mkdir(tmp_path, "checkout-bare")
    _write_env(home, _env_body(d))
    _write_project(home, "bare", _project_body("bare", checkout))
    assert config.render_project_claude_md(load_project("bare")) == ""


def test_no_project_emits_nothing():
    """Environment-only config is valid; there is no project whose rules to load."""
    assert config.render_project_claude_md(None) == ""


def test_unreadable_project_claude_md_hard_fails(home, d, tmp_path, monkeypatch):
    """The load-bearing case: the file EXISTS, so rules were promised, but cannot be
    read. Swallowing that runs a lane unbound by rules it was supposed to load, which is
    worse than not starting it — so it must raise, not return "".

    Neither chmod nor an ACL denies the owner a read portably across Windows and Linux,
    so the read itself is forced to raise instead. The assertion is about the handler,
    not about the operating system's permission model.
    """
    checkout = _mkdir(tmp_path, "checkout-locked")
    (Path(checkout) / "CLAUDE.md").write_text(_REPO_RULES, encoding="utf-8")
    _write_env(home, _env_body(d))
    _write_project(home, "locked", _project_body("locked", checkout))

    proj = load_project("locked")  # resolve BEFORE breaking reads, or this tests nothing

    real_read_text = Path.read_text

    def _boom(self, *args, **kwargs):
        if self.name == "CLAUDE.md":
            raise OSError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom)

    with pytest.raises(ConfigError) as ei:
        config.render_project_claude_md(proj)
    e = ei.value
    assert e.field_path == "canonical_repo.local_checkout"
    assert "could not be read" in e.reason
    assert e.remediation  # a bare raise with no fix instruction is not a loud failure
    assert e.file.endswith("CLAUDE.md")  # names the offending file, not just the project


# --------------------------------------------------------------------------- #
# A3 — drift guard: the hook and `config show` emit the same bytes
# --------------------------------------------------------------------------- #
def _subprocess_env(home: Path, project: str) -> dict:
    """Point a child at the TEMP config home and at THIS worktree's package.

    The suite is pip-installed editable from a different checkout, so without PYTHONPATH
    a child imports that other tree and every assertion below goes green over code this
    file never touched.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SHARED_ROOT)
    env["AGENTFLOW_CONFIG"] = str(home)
    env["AGENTFLOW_PROJECT"] = project
    return env


def _run(argv, env, cwd):
    # Decode as UTF-8, not as the parent's locale. Both emitters force UTF-8 on their
    # stdout precisely because the injected block carries arbitrary repo prose; decoding
    # with the ambient cp1252 here would mangle it back and compare mojibake to mojibake.
    return subprocess.run(
        argv, env=env, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8"
    )


def test_hook_and_show_emit_identical_bytes(home, d, tmp_path):
    """The hook is what a session sees; `show` is what an operator inspecting that
    session sees. If they can differ, the operator is debugging a block the lane never
    got. Compared as bytes, not by eye.

    Both children run from a directory that is NOT a repo — the launch-directory premise.
    """
    assert _HOOK.is_file(), f"SessionStart hook not found at {_HOOK}"

    checkout = _mkdir(tmp_path, "checkout-drift")
    (Path(checkout) / "CLAUDE.md").write_text(_REPO_RULES, encoding="utf-8")
    _write_env(home, _env_body(d))
    _write_project(home, "drift", _project_body("drift", checkout))

    env = _subprocess_env(home, "drift")
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()

    # Guard the guard: prove the children import THIS tree before trusting their output.
    probe = _run(
        [sys.executable, "-c", "import agentflow.config as c; print(c.__file__)"],
        env,
        launch_dir,
    )
    assert probe.returncode == 0, probe.stderr
    assert Path(probe.stdout.strip()).resolve().is_relative_to(_SHARED_ROOT), (
        f"child imported {probe.stdout.strip()}, not the worktree under test"
    )

    hook = _run([sys.executable, str(_HOOK)], env, launch_dir)
    show = _run(
        [sys.executable, "-m", "agentflow.config", "show", "--project", "drift"],
        env,
        launch_dir,
    )
    assert hook.returncode == 0, hook.stderr
    assert show.returncode == 0, show.stderr
    assert hook.stdout == show.stdout

    # And the bytes they agree on actually carry the two things this epic added.
    assert _REPO_RULES.strip() in hook.stdout
    assert f"worktree_root = {Path(d).expanduser()}" in hook.stdout


def test_hook_and_show_agree_when_project_has_no_repo_rules(home, d, tmp_path):
    """The absent-file branch has to stay in step too — a renderer that emits a stray
    blank line or a bare header on one side and nothing on the other is drift."""
    assert _HOOK.is_file(), f"SessionStart hook not found at {_HOOK}"

    checkout = _mkdir(tmp_path, "checkout-plain")
    _write_env(home, _env_body(d))
    _write_project(home, "plain", _project_body("plain", checkout))

    env = _subprocess_env(home, "plain")
    launch_dir = tmp_path / "launch"
    launch_dir.mkdir()

    hook = _run([sys.executable, str(_HOOK)], env, launch_dir)
    show = _run(
        [sys.executable, "-m", "agentflow.config", "show", "--project", "plain"],
        env,
        launch_dir,
    )
    assert hook.returncode == 0, hook.stderr
    assert show.returncode == 0, show.stderr
    assert hook.stdout == show.stdout
    assert "repo CLAUDE.md" not in hook.stdout


# --------------------------------------------------------------------------- #
# A4 — SCHEMA.md documents the remediation the resolver actually raises
# --------------------------------------------------------------------------- #
# The two texts are written independently: one is prose in SCHEMA.md, one is a string
# literal in config.py, and neither imports the other. That is the whole value of the
# check — a test where both sides read the same constant compares a string to itself and
# pins nothing. It is worth having because this drift already happened: SCHEMA.md
# documented `add 'worktree_root:' (string)`, a remediation the resolver has never
# raised, while the real one names an example path. A reader who followed the doc got a
# field name and no value.
_SCHEMA_MD = _SHARED_ROOT / "SCHEMA.md"
_REMEDIATION_BEGIN = "<!-- remediation:worktree_root:begin -->"
_REMEDIATION_END = "<!-- remediation:worktree_root:end -->"


def _documented_worktree_root_remediation() -> str:
    """Read SCHEMA.md's verbatim remediation block for ``worktree_root``.

    Markdown indentation is presentational — the block sits inside a list item, so its
    fence and its body carry leading spaces that say nothing about the string. Each line
    is therefore stripped, and fence lines dropped. Everything else is compared exactly.

    Raises ``AssertionError`` rather than returning a sentinel: a missing or malformed
    block means the documentation this test exists to pin is gone, which is a failure,
    not an empty result to compare against.
    """
    text = _SCHEMA_MD.read_text(encoding="utf-8")
    start = text.find(_REMEDIATION_BEGIN)
    end = text.find(_REMEDIATION_END)
    if start < 0 or end < 0 or end < start:
        raise AssertionError(
            f"{_SCHEMA_MD} is missing a well-formed "
            f"{_REMEDIATION_BEGIN}...{_REMEDIATION_END} block. The remediation the "
            "resolver raises is documented there verbatim; without it nothing holds the "
            "doc and the error message equal."
        )
    body = text[start + len(_REMEDIATION_BEGIN) : end]
    lines = [ln.strip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("```")]
    if len(lines) != 1:
        raise AssertionError(
            f"expected exactly one text line inside SCHEMA.md's worktree_root "
            f"remediation block, found {len(lines)}: {lines!r}. The block quotes a "
            "single-line error string; wrapping it would make the comparison depend on "
            "how the markdown was reflowed."
        )
    return lines[0]


def test_schema_md_quotes_the_remediation_the_resolver_raises(home, d):
    """The documented remediation and the raised one must be the same bytes.

    Editing either side alone turns this red, which is the point: the doc is the only
    place a person reads this text before they hit the error, so a doc that describes a
    different fix is worse than no doc at all.
    """
    _write_env(home, _env_body(d, worktree_root=None))
    with pytest.raises(ConfigError) as ei:
        load_environment()
    raised = ei.value.remediation
    documented = _documented_worktree_root_remediation()
    assert documented == raised, (
        "SCHEMA.md's documented worktree_root remediation has drifted from the string "
        "agentflow.config raises.\n"
        f"  SCHEMA.md: {documented!r}\n"
        f"  raised   : {raised!r}\n"
        "Fix whichever is wrong and keep both in step — they are written independently "
        "on purpose."
    )


def test_documented_remediation_is_actionable_not_just_present():
    """A guard on the guard.

    The equality above would still pass if both sides degraded to the same useless text
    (an empty string, or a bare field name). This pins the property that made the
    field-specific remediation worth writing: it names an example value, so the reader
    is not left guessing a path.
    """
    documented = _documented_worktree_root_remediation()
    assert documented.startswith("add 'worktree_root: "), documented
    assert "EXISTING" in documented, documented
    assert "<worktree_root>/<project>/<epic-branch>" in documented, documented


# --------------------------------------------------------------------------- #
# A5 — the launch-directory drill record stays in place and keeps its claims
# --------------------------------------------------------------------------- #
# The record at harness/launch-dir-drill/ discharges the source epic's assertion 5, which
# is deliberately NOT re-runnable in CI: it is an observation made from inside a live
# fleet session, and a job re-deriving it from a checkout would be asserting the premise
# it is meant to test.
#
# What IS holdable is that the citable artifact still exists and still says what it was
# cited for. Without this, the directory can be deleted or the record gutted and every
# gate stays green — for a record whose entire purpose is to be quoted later. This pins
# text, not behaviour, and the docstrings below say so rather than letting a reader
# mistake it for a check on the launch mechanism.
_DRILL_DIR = _SUITE_ROOT / "harness" / "launch-dir-drill"
_DRILL_RECORD = _DRILL_DIR / "launch-dir-drill.md"
_DRILL_README = _DRILL_DIR / "README.md"


def test_launch_dir_drill_record_is_present():
    """The record and its README exist where the epic's Decisions section put them."""
    assert _DRILL_RECORD.is_file(), (
        f"{_DRILL_RECORD} is missing. It is the citable evidence for the "
        "one-launch-dir-per-machine epic's assertion 5; deleting it silently un-evidences "
        "an assertion the source epic called 'the real proof'."
    )
    assert _DRILL_README.is_file(), f"{_DRILL_README} is missing."


def test_launch_dir_drill_record_still_carries_its_observations():
    """Each observation heading, and the run identifiers a citation would quote.

    Byte counts are deliberately NOT pinned here — they are properties of one machine at
    one moment, and a test asserting them would fail on anyone else's checkout while
    proving nothing. What is pinned is the shape a later reader relies on: the numbered
    observations, and the epic/PR/commit triple that ties the record to a real merged row.
    """
    text = _DRILL_RECORD.read_text(encoding="utf-8")
    for n in range(1, 8):
        heading = f"## Observation {n} —"
        assert heading in text, (
            f"{_DRILL_RECORD} no longer carries {heading!r}. The record's observations are "
            "what it is cited for; dropping one leaves the citation pointing at nothing."
        )
    for token in ("private-working-repo-and-public-publish-target", "#208", "e3b194a"):
        assert token in text, (
            f"{_DRILL_RECORD} no longer names {token!r}. The queue row the lane actually "
            "claimed and built is what makes this a drill rather than a fixture."
        )


def test_launch_dir_drill_record_carries_no_machine_literal():
    """The record's placeholders are its own gate, checked here rather than only in CI.

    The sanitize job greps the whole tree, but it runs on a push and reports against a
    pattern list. This is the local, specific version: the record is the one file in the
    repository whose subject matter IS a machine-specific path, so it is the one most
    likely to acquire one during an edit.
    """
    text = _DRILL_RECORD.read_text(encoding="utf-8")
    assert "<launch-dir>" in text, (
        "the record no longer uses the <launch-dir> placeholder — check that an edit did "
        "not substitute a real path for it."
    )
    # Assembled rather than written out, so this file does not itself carry the literal
    # a whole-tree grep is looking for.
    home_path = re.compile(
        r"[A-Za-z]:[\\/]+" + "Users" + r"[\\/]|/" + "home" + r"/[A-Za-z0-9._-]+"
    )
    hit = home_path.search(text)
    if hit is not None:
        raise AssertionError(
            f"{_DRILL_RECORD} contains the machine-specific path fragment "
            f"{hit.group(0)!r}. Use <launch-dir> and the substitution rule in the "
            "record's Notation section."
        )
