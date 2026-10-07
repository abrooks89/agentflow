#!/usr/bin/env python3
"""gh#417 - two defects that stop or mislead a first install.

1. lock_dir is a third directory the step-10 resolver requires to already exist, and
   provision did not pre-create it. Harmless on POSIX (the template value is /tmp); on a
   fresh Windows box the install died after the venv and editable install had run.
   The test here runs the real step 2 and then the real resolver step against a config
   home under tmp_path - not a mocked resolver - because the defect was the GAP between
   the two, which a test of either alone cannot see.
2. docs/INSTALL.md and install/SKILL.md named the checkout (~/agentflow) as the launch
   directory. provision.py writes the settings under Path.home()/.claude, so the launch
   directory is the home directory; a lane launched from the checkout gets no
   SessionStart block and looks stale rather than broken.

Run: `python -m pytest install/test_provision_gh417.py -v`
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
INSTALL_MD = REPO / "docs" / "INSTALL.md"
SKILL_MD = REPO / "install" / "SKILL.md"


# --------------------------------------------------------------------------- #
# Item 2 - lock_dir is pre-created before the resolver requires it
# --------------------------------------------------------------------------- #
def _write_full_env_md(config_home: Path, *, data_root: Path, worktree_root: Path,
                       lock_dir: Path, suite_root: Path) -> None:
    """A complete environment.md the strict resolver accepts once the dirs exist."""
    config_home.mkdir(parents=True, exist_ok=True)
    # as_posix(): a Windows backslash inside a double-quoted YAML scalar is an escape.
    body = "\n".join([
        "schema_version: 1",
        "person:",
        '  name: "Test"',
        f'data_root: "{data_root.as_posix()}"',
        "git_host:",
        "  kind: local",
        "notify:",
        "  kind: none",
        f'lock_dir: "{lock_dir.as_posix()}"',
        "shell: posix",
        f'suite_root: "{suite_root.as_posix()}"',
        f'worktree_root: "{worktree_root.as_posix()}"',
    ])
    (config_home / "environment.md").write_text(
        f"# environment\n\n```yaml\n{body}\n```\n", encoding="utf-8"
    )


@pytest.fixture
def fresh_box(tmp_path, monkeypatch):
    """Config home, data root and claude root all under tmp_path; real home untouched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    config_home = tmp_path / "config"
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config_home))
    suite_root = tmp_path / "suite"
    suite_root.mkdir()
    lock_dir = tmp_path / "fresh" / "lock"
    _write_full_env_md(
        config_home,
        data_root=tmp_path / "data",
        worktree_root=tmp_path / "worktrees",
        lock_dir=lock_dir,
        suite_root=suite_root,
    )
    opts = provision.Options(
        dry_run=False,
        suite_root=suite_root,
        config_home=config_home,
        settings_file=home / ".claude" / "settings.local.json",
        autodoc=False,
        autodoc_reason="forced by --no-autodoc",
        claude_root=home / ".claude",
    )
    return opts, lock_dir


def test_fresh_provision_precreates_a_missing_lock_dir_before_the_resolver(fresh_box):
    opts, lock_dir = fresh_box
    assert not lock_dir.exists()

    provision._do_config_presence(opts)
    # The real resolver step. Before the fix this raised
    # "[lock_dir] required path does not exist: .../fresh/lock".
    provision._do_presence_checks(opts)

    assert lock_dir.is_dir()


def _set_lock_dir(opts, tmp_path, value: str) -> None:
    env_md = opts.config_home / "environment.md"
    old = f'lock_dir: "{(tmp_path / "fresh" / "lock").as_posix()}"'
    text = env_md.read_text(encoding="utf-8")
    assert old in text
    env_md.write_text(text.replace(old, f'lock_dir: "{value}"'), encoding="utf-8")


def test_tilde_lock_dir_is_created_where_the_resolver_expands_it(fresh_box, tmp_path):
    # "~/..." must expand exactly as config.py expands it (expanduser, under the
    # monkeypatched home), or the dir created is not the dir later required.
    opts, _ = fresh_box
    _set_lock_dir(opts, tmp_path, "~/scratchlock")
    expected = tmp_path / "home" / "scratchlock"
    assert not expected.exists()

    provision._do_config_presence(opts)
    provision._do_presence_checks(opts)

    assert expected.is_dir()


def test_unexpanded_percent_var_lock_dir_is_refused_with_an_actionable_hint(
        fresh_box, tmp_path, capsys):
    # Neither side expands %VAR%, so provision must not create it (it would not be the
    # dir the resolver checks) - but it must say what to write instead.
    opts, _ = fresh_box
    _set_lock_dir(opts, tmp_path, "%TEMP%/scratch_gh417")

    provision._ensure_required_dirs(opts)

    err = capsys.readouterr().err
    assert "not pre-creating lock_dir" in err
    assert "write the expanded absolute path" in err

    # Refused pre-creation leaves the relative path missing, so step 2's resolve now stops
    # the install on it, with the hard-fail wording.
    with pytest.raises(RuntimeError, match="hard-fail"):
        provision._do_config_presence(opts)


def test_skill_md_tells_the_interview_to_expand_lock_dir():
    # The in-seam half of the Windows case: an operator may still reach for a %TEMP% value,
    # and nothing expands it, so the interview must write the expanded absolute path.
    text = " ".join(SKILL_MD.read_text(encoding="utf-8").split())
    assert re.search(r"Write `lock_dir` as an expanded absolute path, never a literal "
                     r"`%TEMP%/scratch`", text)


def test_lock_dir_is_a_required_dir_key_and_suite_root_is_not():
    # suite_root is left to the resolver on purpose: it is the checkout, so a missing
    # one is a WRONG one, and pre-creating it would hide that.
    assert "lock_dir" in provision._REQUIRED_DIR_KEYS
    assert "suite_root" not in provision._REQUIRED_DIR_KEYS


# --------------------------------------------------------------------------- #
# Item 1 - the launch directory is the home directory, never the checkout
# --------------------------------------------------------------------------- #
# Narrow on purpose: "launch directory" followed, within the same sentence, by the
# checkout or ~/agentflow named as what it IS. Mentions that merely sit near the words
# (e.g. "the checkout, not the launch directory") are not what gh#417 found.
_WRONG_LAUNCH_DIR = re.compile(
    r"launch directory\*{0,2}\s*(?:—|-|,|:|\(|is)\s*"
    r"(?:(?:the|your|this|machine's|agentflow|suite)\s+|\*{1,2})*"
    r"(?:checkout\b|`?~/agentflow`?)",
    re.IGNORECASE,
)
# Subject-first: "<the/your agentflow> checkout is <the/this machine's> launch directory".
_WRONG_LAUNCH_DIR_SUBJECT_FIRST = re.compile(
    r"(?:checkout|`?~/agentflow`?)\*{0,2}\s+is\s+"
    r"(?:(?:the|your|this|machine's|also|always)\s+|\*{1,2})*launch directory",
    re.IGNORECASE,
)
# §7's old shape named no folder in the sentence itself - "That folder" pointed back at the
# `cd ~/agentflow` just above it - so it gets its own pattern anchored on that command.
_WRONG_SECTION7 = re.compile(
    r"cd ~/agentflow && claude\s*```\s*That folder[^.]*?it is this machine's "
    r"\*{0,2}launch directory",
    re.IGNORECASE,
)
_RIGHT_LAUNCH_DIR = re.compile(
    r"Which folder is the launch directory\?\*{0,2}\s+The one the installer wrote the "
    r"settings for\s*(?:—|-)\s*your home directory",
    re.IGNORECASE,
)


@pytest.mark.parametrize("doc", [INSTALL_MD, SKILL_MD], ids=lambda p: p.name)
def test_no_install_doc_names_the_checkout_as_the_launch_directory(doc):
    text = " ".join(doc.read_text(encoding="utf-8").split())
    hits = [m.group(0) for pat in (_WRONG_LAUNCH_DIR, _WRONG_LAUNCH_DIR_SUBJECT_FIRST, _WRONG_SECTION7)
            for m in pat.finditer(text)]
    assert hits == [], f"{doc.name} names the checkout as the launch directory: {hits}"


def test_wrong_launch_dir_pin_catches_the_gh417_wording():
    # Control: the pin must red on the exact sentences the issue quoted.
    old_skill = ("Every one of them launches from the machine's launch directory — "
                 "the checkout, never a project repo")
    old_install = ("it is this machine's **launch directory**, the single place every "
                   "session below is started from")
    assert _WRONG_LAUNCH_DIR.search(old_skill)
    assert not _WRONG_LAUNCH_DIR.search(old_install)  # this one names no folder itself...
    old_section7 = ("```\ncd ~/agentflow && claude\n```\n\nThat folder is not only where "
                    "the installer lives — " + old_install)
    assert _WRONG_SECTION7.search(" ".join(old_section7.split()))  # ...so this one catches it
    assert _WRONG_LAUNCH_DIR.search("the launch directory is ~/agentflow")


@pytest.mark.parametrize("wrong", [
    "your agentflow checkout is the launch directory",
    "the checkout is this machine's **launch directory**",
    "the launch directory is the agentflow checkout",
    "the launch directory is your checkout",
    "the launch directory (the checkout)",
])
def test_wrong_launch_dir_pin_catches_paraphrases(wrong):
    assert (_WRONG_LAUNCH_DIR.search(wrong)
            or _WRONG_LAUNCH_DIR_SUBJECT_FIRST.search(wrong)), wrong


@pytest.mark.parametrize("right", [
    "the launch directory — the operator's home directory, never the agentflow checkout",
    "in the checkout, not the launch directory",
    "It is **not** this machine's launch directory",
])
def test_wrong_launch_dir_pin_stays_narrow(right):
    assert not _WRONG_LAUNCH_DIR.search(right)
    assert not _WRONG_LAUNCH_DIR_SUBJECT_FIRST.search(right)


def test_install_md_never_says_read_install_skill_from_the_launch_directory():
    # `install/SKILL.md` is relative to the checkout; from home it does not resolve (§12).
    text = " ".join(INSTALL_MD.read_text(encoding="utf-8").split())
    assert "Read install/SKILL.md and add a project profile" in text  # control
    assert not re.search(r"From the launch directory[^>]{0,160}> Read install/SKILL\.md",
                         text)


def test_install_md_still_says_home_directory():
    # Control for the pin: the correct answer (§10) is still there, so a pin that went
    # vacuous by the whole passage being deleted does not pass silently.
    text = " ".join(INSTALL_MD.read_text(encoding="utf-8").split())
    assert _RIGHT_LAUNCH_DIR.search(text)
