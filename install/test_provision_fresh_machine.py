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

    provision._do_config_presence(opts)

    assert data_root.is_dir()


def test_config_presence_data_root_precreate_is_idempotent(tmp_path):
    config_home = tmp_path / "config"
    data_root = tmp_path / "agentflow-data"
    data_root.mkdir()
    (data_root / "keep.txt").write_text("kept", encoding="utf-8")
    _write_env_md(config_home, data_root=str(data_root))
    opts = _make_opts(tmp_path, config_home=config_home)

    provision._do_config_presence(opts)

    assert (data_root / "keep.txt").read_text(encoding="utf-8") == "kept"


def test_config_presence_without_data_root_key_leaves_failure_to_resolver(tmp_path):
    # No data_root in the yaml block: provision must not crash here — the strict
    # resolver in step 9 owns the loud failure for a broken config.
    config_home = tmp_path / "config"
    _write_env_md(config_home, data_root=None)
    opts = _make_opts(tmp_path, config_home=config_home)

    provision._do_config_presence(opts)  # must not raise


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

    provision._do_config_presence(opts)

    assert calls == [], f"step 2 created an unvalidated drive-root path: {calls}"


def test_config_presence_REFUSES_a_relative_scraped_path(tmp_path, monkeypatch):
    # A bare relative value is a mangled scrape, not a configuration: acting on it
    # would materialise a tree under whatever cwd provision happens to run from.
    config_home = tmp_path / "config"
    _write_env_md(config_home, data_root="data-root")
    opts = _make_opts(tmp_path, config_home=config_home)
    calls = _record_mkdirs(monkeypatch)

    provision._do_config_presence(opts)

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

    provision._do_config_presence(opts)

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

    provision._do_config_presence(opts)

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
