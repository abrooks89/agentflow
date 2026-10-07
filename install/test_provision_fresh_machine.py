#!/usr/bin/env python3
"""Tests for the AF-B1 fresh-machine fixes in install/provision.py.

Four defects, all live-reproduced on a fresh directory 2026-08-05:

1. `_run_cmd` passed `cmd[0]` straight to `subprocess.run`, which does not apply
   PATHEXT on Windows — `npm` (really `npm.cmd`) raised FileNotFoundError. Fix:
   resolve `cmd[0]` via `shutil.which` and fail loudly, naming the original
   command, when unresolvable.
2. The SessionStart hook was registered with bare `python` while the `agentflow`
   package is pip-installed only into `<suite_root>/.venv` — ModuleNotFoundError
   at every session open on any machine but the author's. Fix: register the suite
   venv interpreter (`_venv_python`), with both paths quoted so spaces survive.
3. The resolver hard-fails when `data_root` does not exist, but the scaffold step
   that would create it runs AFTER the resolver step. Fix: provision pre-creates
   `data_root` (parents ok) in the config-presence step, before the resolver runs.
   The resolver stays strict.
4. The secrets step was a literal no-op `pass` while the printed plan claimed it
   "confirms named vars resolve". Fix: it now checks the env-var NAMES declared in
   `environment.md` (`notify.url_env` / `notify.topic_env`) against `os.environ`
   and fails loudly listing the missing NAMES — never values.

Run: `python -m pytest install/test_provision_fresh_machine.py -v`
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402


def _make_opts(suite_root: Path, config_home: Path = None, settings_file: Path = None) -> provision.Options:
    return provision.Options(
        dry_run=False,
        suite_root=suite_root,
        config_home=config_home if config_home is not None else suite_root / "config-home",
        settings_file=settings_file if settings_file is not None else suite_root / "settings.json",
        autodoc=False,
        autodoc_reason="forced by --no-autodoc",
    )


def _write_env_md(config_home: Path, *, data_root: str = None,
                  worktree_root: str = None, notify_yaml: str = None) -> Path:
    """Write a minimal environment.md with a fenced yaml block."""
    config_home.mkdir(parents=True, exist_ok=True)
    lines = ["schema_version: 1"]
    if data_root is not None:
        lines.append(f'data_root: "{data_root}"')
    if worktree_root is not None:
        lines.append(f'worktree_root: "{worktree_root}"')
    if notify_yaml is not None:
        lines.append(notify_yaml)
    body = "\n".join(lines)
    env_md = config_home / "environment.md"
    env_md.write_text(
        f"# environment\n\nprose above the block\n\n```yaml\n{body}\n```\n\nprose after\n",
        encoding="utf-8",
    )
    return env_md


# --------------------------------------------------------------------------- #
# Fix 1 — _run_cmd resolves cmd[0] via shutil.which
# --------------------------------------------------------------------------- #
def test_run_cmd_resolves_executable_via_which():
    captured = {}

    def fake_run(cmd, env=None, cwd=None, text=None, capture_output=None):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with mock.patch.object(provision.shutil, "which", return_value=r"C:\nodejs\npm.cmd") as fake_which, \
         mock.patch.object(provision.subprocess, "run", side_effect=fake_run):
        provision._run_cmd(["npm", "ci"])

    fake_which.assert_called_once_with("npm")
    assert captured["cmd"][0] == r"C:\nodejs\npm.cmd"
    assert captured["cmd"][1:] == ["ci"]


def test_run_cmd_unresolvable_executable_raises_naming_original_command():
    with mock.patch.object(provision.shutil, "which", return_value=None), \
         mock.patch.object(provision.subprocess, "run") as fake_run:
        with pytest.raises(RuntimeError) as excinfo:
            provision._run_cmd(["npm", "ci"])

    assert "npm" in str(excinfo.value)
    fake_run.assert_not_called()


# --------------------------------------------------------------------------- #
# Fix 2 + 5 — SessionStart hook uses the suite venv python, paths quoted
# --------------------------------------------------------------------------- #
def _hook_commands(settings_file: Path) -> list[str]:
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    return [
        h["command"]
        for block in settings.get("hooks", {}).get("SessionStart", [])
        for h in block.get("hooks", [])
    ]


def test_sessionstart_hook_registers_venv_python_with_quoted_paths(tmp_path):
    suite_root = tmp_path / "suite root with spaces"
    suite_root.mkdir()
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    opts = _make_opts(suite_root, settings_file=settings_file)

    provision._do_sessionstart_hook(opts)

    cmds = _hook_commands(settings_file)
    assert len(cmds) == 1
    expected_py = str(provision._venv_python(opts))
    expected_script = str(suite_root / provision.SESSIONSTART_HOOK_REL)
    assert cmds[0] == f'"{expected_py}" "{expected_script}"'
    # never the bare interpreter that lacks the agentflow package
    assert not cmds[0].startswith("python ")


def test_sessionstart_hook_registration_is_idempotent(tmp_path):
    suite_root = tmp_path / "suite"
    suite_root.mkdir()
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    opts = _make_opts(suite_root, settings_file=settings_file)

    provision._do_sessionstart_hook(opts)
    provision._do_sessionstart_hook(opts)

    assert len(_hook_commands(settings_file)) == 1


def test_sessionstart_hook_missing_settings_file_fails_actionably(tmp_path):
    opts = _make_opts(tmp_path, settings_file=tmp_path / "nope" / "settings.json")

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_sessionstart_hook(opts)

    msg = str(excinfo.value)
    assert str(opts.settings_file) in msg
    assert "echo '{}'" in msg and "--settings-file" in msg
    assert "Run Claude Code once" not in msg


# --------------------------------------------------------------------------- #
# Fix 3 — data_root pre-created before the resolver validates config
# --------------------------------------------------------------------------- #
def test_config_presence_precreates_missing_data_root(tmp_path):
    config_home = tmp_path / "config"
    data_root = tmp_path / "agentflow-data" / "nested"
    _write_env_md(config_home, data_root=str(data_root))
    opts = _make_opts(tmp_path, config_home=config_home)
    assert not data_root.exists()

    provision._ensure_required_dirs(opts)

    assert data_root.is_dir()


def test_config_presence_data_root_precreate_is_idempotent(tmp_path):
    config_home = tmp_path / "config"
    data_root = tmp_path / "agentflow-data"
    data_root.mkdir()
    (data_root / "keep.txt").write_text("kept", encoding="utf-8")
    _write_env_md(config_home, data_root=str(data_root))
    opts = _make_opts(tmp_path, config_home=config_home)

    provision._ensure_required_dirs(opts)

    assert (data_root / "keep.txt").read_text(encoding="utf-8") == "kept"


def test_config_presence_hard_fails_a_broken_environment_at_step_2(tmp_path):
    # gh#985: step 2 is "exists + resolves", so an environment.md the resolver rejects
    # (here: no data_root) fails here, before the venv is built, with the hard-fail
    # wording and the ConfigError's file and remediation - not later, and not raw.
    config_home = tmp_path / "config"
    _write_env_md(config_home, data_root=None)
    opts = _make_opts(tmp_path, config_home=config_home)

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_config_presence(opts)

    msg = str(excinfo.value)
    assert msg.startswith("config presence-check FAILED (hard-fail):")
    assert str(config_home / "environment.md") in msg
    assert "remediation:" in msg


# --------------------------------------------------------------------------- #
# gh#985 item 1 - a broken profile ends as the designed hard-fail, never a traceback
# --------------------------------------------------------------------------- #
_SUITE = Path(__file__).resolve().parent.parent


def _home_with_misnamed_profile(tmp_path: Path) -> Path:
    """The issue's repro: a valid environment.md and projects/toy.md declaring name 'toyy'."""
    home = tmp_path / "cfg"
    (home / "projects").mkdir(parents=True)
    dirs = {k: tmp_path / k for k in ("data", "worktrees", "lock")}
    for d in dirs.values():
        d.mkdir()
    (home / "environment.md").write_text(
        "```yaml\nschema_version: 1\nperson:\n  name: \"Test\"\n"
        f"data_root: \"{dirs['data'].as_posix()}\"\ngit_host:\n  kind: local\n"
        f"notify:\n  kind: none\nlock_dir: \"{dirs['lock'].as_posix()}\"\nshell: posix\n"
        f"suite_root: \"{tmp_path.as_posix()}\"\n"
        f"worktree_root: \"{dirs['worktrees'].as_posix()}\"\n```\n",
        encoding="utf-8",
    )
    (home / "projects" / "toy.md").write_text(
        "```yaml\nschema_version: 1\nname: toyy\n```\n", encoding="utf-8")
    return home


def _main_with_steps(tmp_path, monkeypatch, capsys, steps) -> tuple:
    home = _home_with_misnamed_profile(tmp_path)
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    with mock.patch.object(provision, "build_steps", lambda _o: steps):
        rc = provision.main(["--suite-root", str(_SUITE), "--config-home", str(home),
                             "--settings-file", str(tmp_path / "settings.json"),
                             "--claude-root", str(tmp_path / "claude")])
    out, err = capsys.readouterr()
    return rc, out, err


def _assert_hard_fail(rc: int, err: str) -> None:
    assert rc == 1
    assert "agentflow provision FAILED:\nconfig presence-check FAILED (hard-fail):" in err
    # The ConfigError's field, file and remediation, carried whole.
    assert "[name]" in err and "toyy" in err
    assert str(Path("projects") / "toy.md") in err
    assert "remediation:" in err
    assert "Traceback" not in err


def test_a_misnamed_profile_fails_at_step_2_before_the_venv(tmp_path, monkeypatch, capsys):
    steps = [provision.Step(2, "config presence", run=provision._do_config_presence),
             provision.Step(5, "autodoc re-check", run=provision._confirm_autodoc_against_config)]
    rc, out, err = _main_with_steps(tmp_path, monkeypatch, capsys, steps)
    _assert_hard_fail(rc, err)
    assert "2. done." not in out, "step 2 must catch it, before step 4 builds the venv"


def test_a_misnamed_profile_reaching_step_5_still_ends_as_the_hard_fail(
        tmp_path, monkeypatch, capsys):
    # Step 5 is where a fresh machine (no PyYAML before the venv) first resolves config,
    # and where the issue saw the raw ConfigError traceback.
    steps = [provision.Step(5, "autodoc re-check", run=provision._confirm_autodoc_against_config)]
    rc, _out, err = _main_with_steps(tmp_path, monkeypatch, capsys, steps)
    _assert_hard_fail(rc, err)


def test_config_presence_step_runs_before_resolver_step():
    opts = _make_opts(Path("."))
    steps = provision.build_steps(opts)
    presence_n = next(s.n for s in steps if s.run is provision._do_config_presence)
    resolver_n = next(s.n for s in steps if s.run is provision._do_presence_checks)
    assert presence_n < resolver_n


# --------------------------------------------------------------------------- #
# gh#322 - step 2 must not act irreversibly on a value nothing has validated
#
# These record mkdir instead of letting it run. The unguarded code creates a real
# directory at the real drive root, so a test that let it happen would litter the
# machine it is meant to protect - and the red-proof would be the bug.
# --------------------------------------------------------------------------- #
def _record_mkdirs(monkeypatch) -> list:
    """Capture every Path.mkdir target without touching the filesystem."""
    calls = []

    def fake_mkdir(self, *a, **kw):
        calls.append(self)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    return calls


def test_config_presence_REFUSES_to_create_at_the_drive_root(tmp_path, monkeypatch):
    # The observed gh#322 failure: a value that lost its prefix, leaving a path whose
    # only ancestor is the anchor. Creating it also PRE-SATISFIES the step-10
    # resolver's exists() check, so refusing here is what lets the resolver catch it.
    config_home = tmp_path / "config"
    stray = Path(tmp_path).anchor + "data-root"
    _write_env_md(config_home, data_root=stray)
    opts = _make_opts(tmp_path, config_home=config_home)
    calls = _record_mkdirs(monkeypatch)

    provision._ensure_required_dirs(opts)

    assert calls == [], f"step 2 created an unvalidated drive-root path: {calls}"


def test_config_presence_REFUSES_a_relative_scraped_path(tmp_path, monkeypatch):
    # A bare relative value is a mangled scrape, not a configuration: acting on it
    # would materialise a tree under whatever cwd provision happens to run from.
    config_home = tmp_path / "config"
    _write_env_md(config_home, data_root="data-root")
    opts = _make_opts(tmp_path, config_home=config_home)
    calls = _record_mkdirs(monkeypatch)

    provision._ensure_required_dirs(opts)

    assert calls == [], f"step 2 created a relative scraped path: {calls}"


def test_config_presence_STILL_creates_a_nested_tree_under_an_existing_dir(tmp_path, monkeypatch):
    # The guard must stay narrower than gh#322's own candidate shape 1. "Reject a path
    # whose parent does not already exist" would refuse exactly the tree provision was
    # written to create - this reds against a guard that over-refuses.
    config_home = tmp_path / "config"
    legit = tmp_path / "agentflow-data" / "nested"
    _write_env_md(config_home, data_root=str(legit))
    opts = _make_opts(tmp_path, config_home=config_home)
    calls = _record_mkdirs(monkeypatch)

    provision._ensure_required_dirs(opts)

    assert calls == [legit], f"step 2 refused a legitimate nested data_root: {calls}"


def test_config_presence_guards_worktree_root_the_same_way(tmp_path, monkeypatch):
    # worktree_root is the other half of _REQUIRED_DIR_KEYS and had no test at all;
    # gh#322 observed it materialising alongside data_root.
    config_home = tmp_path / "config"
    legit = tmp_path / "worktrees"
    _write_env_md(
        config_home,
        data_root=Path(tmp_path).anchor + "data-root",
        worktree_root=str(legit),
    )
    opts = _make_opts(tmp_path, config_home=config_home)
    calls = _record_mkdirs(monkeypatch)

    provision._ensure_required_dirs(opts)

    assert calls == [legit], f"expected only the valid worktree_root, got {calls}"


# gh#372 - the docs kept calling gh#322 open for weeks after the guard above shipped,
# including in the drill that writes the published install record. Keyed on the CODE
# fact rather than on the issue tracker, so it runs offline and cannot go stale itself:
# while provision carries the guard, no first-party doc may say the finding is open.
# The phrases are the ones gh#372 removed, plus the pattern its verification greps.
_GH322_STALE_OPEN_CLAIMS = re.compile(
    # Bounded, so "second finding" cannot pair with a "still open" paragraphs later.
    r"second\** finding[^.]{0,200}(still open|untouched|not fixed)"
    r"|two findings is fixed"
    r"|still open and (this drill|is \*\*not\*\* checked)"
    r"|is \*\*not\*\* fixed"
    r"|witness of\s+the open defect"
    # The old description of the defect itself, stated as current behaviour.
    r"|before\**\s+the\s+strict\s+resolver\s+validates\s+them"
    r"|materiali[sz]es\s+a\s+directory\s+(anywhere|wherever)",
    re.IGNORECASE,
)
_REPO_ROOT = Path(__file__).resolve().parent.parent
# Every file that carried a stale claim. Each must be in the scan, or narrowing a root or the
# extension filter would drop it silently and the test would stay green on a clean tree.
_GH372_SITE_FILES = (
    "docs/INSTALL.md",
    "docs/ROADMAP.md",
    "install/SKILL.md",
    "harness/README.md",
    "harness/fresh-clone-install-drill/README.md",
    "harness/fresh-clone-install-drill/fresh_clone_install_drill.py",
)


def _first_party_docs() -> dict:
    """Tracked text under the roots a new reader meets, keyed by repo-relative path.

    `git ls-files`, not a directory walk: gitignored drill records and scratch output
    under harness/ may legitimately quote the old wording, and they are not docs.
    Test modules are excluded because they name the phrases in order to forbid them.
    """
    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "ls-files", "--", "docs", "install", "harness",
         "README.md"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    docs = {}
    for rel in out:
        name = rel.rsplit("/", 1)[-1]
        if not rel.endswith((".md", ".py")) or name.startswith("test_"):
            continue
        path = _REPO_ROOT / rel
        if path.is_file():
            docs[rel] = path.read_text(encoding="utf-8")
    return docs


def _gh322_stale_open_claims(docs: dict) -> list:
    if not hasattr(provision, "_unsafe_to_precreate"):
        return []  # guard gone: an open-claim may be TRUE again, so there is nothing to flag
    return [
        f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}"
        for rel, text in docs.items()
        for m in _GH322_STALE_OPEN_CLAIMS.finditer(text)
    ]


def test_no_first_party_doc_calls_gh322_open_while_its_guard_exists():
    docs = _first_party_docs()
    missing = [rel for rel in _GH372_SITE_FILES if rel not in docs]
    assert missing == [], (
        f"the doc scan skipped files gh#372 had to fix, so it cannot see them regress: {missing}"
    )
    hits = _gh322_stale_open_claims(docs)
    assert hits == [], (
        "these docs still call gh#322 open, but provision._unsafe_to_precreate is the fix:\n"
        + "\n".join(hits)
    )


def test_the_gh322_doc_check_REDS_on_a_planted_stale_phrase_and_goes_QUIET_without_the_guard(
        monkeypatch):
    clean = {"docs/X.md": "The second finding, split out as gh#322, is fixed."}
    planted = {
        "docs/X.md": "The issue's second finding is still open: step 2 mkdirs.",
        # The old defect description, which carries no "open" word at all.
        "docs/Y.md": "Step 2 mkdirs `data_root` *before* the strict resolver\n"
                     "validates them.",
        "docs/Z.md": "So a malformed path materialises a directory wherever it can write.",
    }
    assert _gh322_stale_open_claims(clean) == []          # control: accurate prose passes
    flagged = {hit.split(":", 1)[0] for hit in _gh322_stale_open_claims(planted)}
    assert flagged == {"docs/X.md", "docs/Y.md", "docs/Z.md"},f"planted stale claims not all flagged: {flagged}"

    monkeypatch.delattr(provision, "_unsafe_to_precreate")
    assert _gh322_stale_open_claims(planted) == [], (
        "with the guard removed the claim may be true again; the check must not fire"
    )


# --------------------------------------------------------------------------- #
# Fix 4 — secrets step actually confirms the named vars resolve
# --------------------------------------------------------------------------- #
NOTIFY_NTFY = (
    "notify:\n"
    '  kind: ntfy\n'
    '  url_env: "AF_B1_TEST_NTFY_URL"\n'
    '  topic_env: "AF_B1_TEST_NTFY_TOPIC"\n'
)
NOTIFY_NONE = "notify:\n  kind: none\n"


def test_secrets_step_fails_listing_missing_var_names(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    _write_env_md(config_home, data_root=str(tmp_path / "d"), notify_yaml=NOTIFY_NTFY)
    monkeypatch.delenv("AF_B1_TEST_NTFY_URL", raising=False)
    monkeypatch.delenv("AF_B1_TEST_NTFY_TOPIC", raising=False)
    opts = _make_opts(tmp_path, config_home=config_home)

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_secrets(opts)

    msg = str(excinfo.value)
    assert "AF_B1_TEST_NTFY_URL" in msg
    assert "AF_B1_TEST_NTFY_TOPIC" in msg


def test_secrets_step_never_prints_values(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    _write_env_md(config_home, data_root=str(tmp_path / "d"), notify_yaml=NOTIFY_NTFY)
    monkeypatch.setenv("AF_B1_TEST_NTFY_URL", "https://secret.example/hook-xyzzy")
    monkeypatch.delenv("AF_B1_TEST_NTFY_TOPIC", raising=False)
    opts = _make_opts(tmp_path, config_home=config_home)

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_secrets(opts)

    msg = str(excinfo.value)
    assert "AF_B1_TEST_NTFY_TOPIC" in msg          # the missing NAME
    assert "AF_B1_TEST_NTFY_URL" not in msg or "xyzzy" not in msg
    assert "xyzzy" not in msg                       # never a value


def test_secrets_step_passes_when_named_vars_resolve(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    _write_env_md(config_home, data_root=str(tmp_path / "d"), notify_yaml=NOTIFY_NTFY)
    monkeypatch.setenv("AF_B1_TEST_NTFY_URL", "https://ntfy.example")
    monkeypatch.setenv("AF_B1_TEST_NTFY_TOPIC", "topic")
    opts = _make_opts(tmp_path, config_home=config_home)

    provision._do_secrets(opts)  # must not raise


def test_secrets_step_notify_none_requires_nothing(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    _write_env_md(config_home, data_root=str(tmp_path / "d"), notify_yaml=NOTIFY_NONE)
    monkeypatch.delenv("AF_B1_TEST_NTFY_URL", raising=False)
    monkeypatch.delenv("AF_B1_TEST_NTFY_TOPIC", raising=False)
    opts = _make_opts(tmp_path, config_home=config_home)

    provision._do_secrets(opts)  # must not raise


# --------------------------------------------------------------------------- #
# gh#985 review - step 2's resolve keeps step 3's messages and its own env clean
# --------------------------------------------------------------------------- #
def _full_env_home(tmp_path: Path, notify_yaml: str) -> Path:
    home = tmp_path / "cfg"
    home.mkdir()
    dirs = {k: tmp_path / k for k in ("data", "worktrees", "lock")}
    for d in dirs.values():
        d.mkdir()
    (home / "environment.md").write_text(
        "```yaml\nschema_version: 1\nperson:\n  name: \"Test\"\n"
        f"data_root: \"{dirs['data'].as_posix()}\"\ngit_host:\n  kind: local\n"
        f"{notify_yaml}lock_dir: \"{dirs['lock'].as_posix()}\"\nshell: posix\n"
        f"suite_root: \"{tmp_path.as_posix()}\"\n"
        f"worktree_root: \"{dirs['worktrees'].as_posix()}\"\n```\n",
        encoding="utf-8",
    )
    return home


def test_step_2_gives_the_secrets_message_not_the_resolvers(tmp_path, monkeypatch):
    # The resolver presence-checks the notify NAMES too, but its ConfigError does not say
    # where to persist them; step 3's message does, so it must win at step 2 as well.
    monkeypatch.delenv("AF_B1_TEST_NTFY_URL", raising=False)
    monkeypatch.delenv("AF_B1_TEST_NTFY_TOPIC", raising=False)
    home = _full_env_home(tmp_path, NOTIFY_NTFY)
    opts = _make_opts(_SUITE, config_home=home)

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_config_presence(opts)

    msg = str(excinfo.value)
    assert msg.startswith("secrets check FAILED"), msg
    assert "AF_B1_TEST_NTFY_URL" in msg and "settings.local.json" in msg
    assert "hard-fail" not in msg


def test_step_2_defers_resolution_when_pyyaml_is_not_importable(tmp_path, capsys):
    config_home = tmp_path / "config"
    _write_env_md(config_home, data_root=None)   # the resolver would reject this
    opts = _make_opts(tmp_path, config_home=config_home)

    with mock.patch.object(provision, "_parent_can_import_deps", lambda: False):
        provision._do_config_presence(opts)   # must not raise: step 5 resolves instead

    assert "resolution deferred to step 5" in capsys.readouterr().out


@pytest.mark.parametrize("prior", ["sentinel-config-home", None])
def test_load_config_or_fail_restores_agentflow_config_when_it_raises(
        tmp_path, monkeypatch, prior):
    if prior is None:
        monkeypatch.delenv("AGENTFLOW_CONFIG", raising=False)
    else:
        monkeypatch.setenv("AGENTFLOW_CONFIG", prior)
    config_home = tmp_path / "config"
    _write_env_md(config_home, data_root=None)
    opts = _make_opts(_SUITE, config_home=config_home)

    with pytest.raises(RuntimeError, match="hard-fail"):
        provision._load_config_or_fail(opts)

    assert os.environ.get("AGENTFLOW_CONFIG") == prior


# --------------------------------------------------------------------------- #
# gh#1047 - a delegated path to a skill provision deploys at step 7 resolves at steps
# 2 and 5 (before the deploy) from its suite source; step 10 stays strict.
# --------------------------------------------------------------------------- #
_SHIPPED = "polish-mode"


def _deploy_target_machine(tmp_path: Path, polishing: str) -> provision.Options:
    """A suite shipping one skill, a FRESH Claude dir, and a profile whose
    generation_skills.polishing is *polishing* (`{claude}` stands for the Claude dir)."""
    suite = tmp_path / "suite"
    (suite / "skills" / _SHIPPED).mkdir(parents=True)
    (suite / "skills" / _SHIPPED / "SKILL.md").write_text("# polish\n", encoding="utf-8")
    (suite / "skills" / "_shared").mkdir()   # the pip package: never deployed
    (suite / "skills" / "_shared" / "SKILL.md").write_text("x\n", encoding="utf-8")
    claude = tmp_path / "claude"             # deliberately absent: a fresh machine
    home = _full_env_home(tmp_path, "notify:\n  kind: none\n")
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (home / "projects").mkdir()
    (home / "projects" / "toy.md").write_text(
        "```yaml\nschema_version: 1\nname: toy\ncanonical_repo:\n  host_ref: owner/toy\n"
        f"  local_checkout: \"{checkout.as_posix()}\"\nverification:\n  strategy: test-suite\n"
        f"generation_skills:\n  polishing: \"{polishing.format(claude=claude.as_posix())}\"\n```\n",
        encoding="utf-8",
    )
    opts = _make_opts(suite, config_home=home)
    opts.claude_root = claude
    return opts


def _hard_fail_naming(opts, step, needle: str) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        step(opts)
    msg = str(excinfo.value)
    assert msg.startswith("config presence-check FAILED (hard-fail):"), msg
    assert "generation_skills.polishing" in msg and needle in msg, msg


def test_a_to_be_deployed_skill_passes_steps_2_and_5_then_strict_step_10(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    opts = _deploy_target_machine(tmp_path, "{claude}/skills/" + _SHIPPED + "/SKILL.md")
    target = opts.claude_root / "skills" / _SHIPPED / "SKILL.md"

    provision._do_config_presence(opts)               # step 2
    provision._confirm_autodoc_against_config(opts)   # step 5
    # Step 10 before the deploy is still the strict check: nothing was relaxed for it.
    _hard_fail_naming(opts, provision._do_presence_checks, "delegated path missing")

    provision._do_deploy_skills(opts)                 # step 7
    assert target.is_file()
    provision._do_presence_checks(opts)               # step 10, strict, now satisfied


def test_a_typod_skill_path_still_fails_at_step_2(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    opts = _deploy_target_machine(tmp_path, "{claude}/skills/polish-mod/SKILL.md")
    _hard_fail_naming(opts, provision._do_config_presence, "delegated path missing")


def test_a_file_the_shipped_skill_does_not_hold_still_fails_at_step_2(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    opts = _deploy_target_machine(tmp_path, "{claude}/skills/" + _SHIPPED + "/SKIL.md")
    _hard_fail_naming(opts, provision._do_config_presence, "delegated path missing")


def test_a_path_climbing_out_of_skills_is_not_accepted(tmp_path, monkeypatch):
    """`skills/polish-mode/../../polish-mode/SKILL.md` is <claude>/polish-mode/SKILL.md, which
    nothing deploys. A string-prefix check would accept it, and a naive join onto the suite
    lands on <suite>/polish-mode/SKILL.md - planted here so that mistake would pass."""
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    opts = _deploy_target_machine(
        tmp_path, "{claude}/skills/" + _SHIPPED + "/../../" + _SHIPPED + "/SKILL.md")
    (opts.suite_root / _SHIPPED).mkdir()
    (opts.suite_root / _SHIPPED / "SKILL.md").write_text("decoy\n", encoding="utf-8")
    _hard_fail_naming(opts, provision._do_config_presence, "delegated path missing")


def test_the_shared_package_is_not_a_deployed_skill(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    opts = _deploy_target_machine(tmp_path, "{claude}/skills/_shared/SKILL.md")
    _hard_fail_naming(opts, provision._do_config_presence, "delegated path missing")


def test_config_skill_check_is_strict_unless_a_pending_deploy_is_passed(tmp_path):
    from agentflow.config import ConfigError, PendingSkillDeploy, _check_skill_path_if_set

    suite, claude = tmp_path / "suite", tmp_path / "claude"
    (suite / "skills" / _SHIPPED).mkdir(parents=True)
    (suite / "skills" / _SHIPPED / "SKILL.md").write_text("# polish\n", encoding="utf-8")
    value = str(claude / "skills" / _SHIPPED / "SKILL.md")
    pending = PendingSkillDeploy(claude, suite, frozenset({_SHIPPED}))

    with pytest.raises(ConfigError, match="delegated path missing"):
        _check_skill_path_if_set(value, "generation_skills.polishing", tmp_path / "p.md")
    # The configured path comes back, not the suite source: it is where the seat will read.
    assert _check_skill_path_if_set(value, "generation_skills.polishing", tmp_path / "p.md",
                                    pending=pending) == value
    if os.name == "nt":   # Windows paths are case-insensitive, so the comparison is too
        upper = os.path.join(str(claude).upper(), "SKILLS", _SHIPPED, "SKILL.md")
        assert _check_skill_path_if_set(upper, "f", tmp_path / "p.md", pending=pending) == upper
    with pytest.raises(ConfigError, match="delegated path missing"):
        _check_skill_path_if_set(value, "f", tmp_path / "p.md",
                                 pending=PendingSkillDeploy(claude, suite, frozenset()))
