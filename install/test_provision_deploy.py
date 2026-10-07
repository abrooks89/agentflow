#!/usr/bin/env python3
"""Tests for the AF-B2 skill-deployment step in install/provision.py.

The step deploys the suite's skills + command wrappers into the user's Claude
config dir so the fleet is launchable post-install (`/lead-dev`, `/dev-manager`,
`/auditor`, `/jr-dev-1`, `/jr-dev-2` all resolve). Policy under test:

- `skills/<name>/` dirs -> `<claude_root>/skills/<name>/`; `commands/*.md` ->
  `<claude_root>/commands/`. `skills/_shared` is the pip package (editable
  install, step 5) and is NEVER deployed; `skills/_vendored` IS deployed because
  first-party skills reference it by RELATIVE sibling path (`../_vendored/...`),
  which post-deploy resolves under `<claude_root>/skills/_vendored`.
- Unix symlinks; Windows JUNCTIONS for directories (gh#296 - a deployed copy went
  stale the moment a skill-touching merge landed, and nothing anywhere re-deploys,
  so every seat ran the previous version of its own instructions until someone
  re-provisioned by hand). Junctions need no admin/Developer Mode, which is why
  the Windows answer is "junction" and not "symlink". Under JUNCTION mode the FILE
  entries (`commands/*.md` and the hook script) stay copies and stay stale; under
  SYMLINK mode they are links like everything else. That residue is named by
  `_stale_copy_entries` / `_stale_copy_blurb`, not hidden - and it is mode-dependent,
  which is the thing an unconditional assertion here got wrong once already.
- MODE PINNING - READ BEFORE ADDING A TEST HERE. The autouse `_copy_mode` fixture
  pins copy mode for every test that does NOT request the `real_deploy_mode`
  fixture. That blanket pin is exactly why gh#296 shipped green on both CI legs:
  no test in this file had ever exercised the mode `_deploy_mode()` actually
  returns. Tests whose premise is "edit the deployed copy, then observe drift"
  are copy-mode MECHANICS - under a link the deployed thing IS the source and the
  premise cannot exist - and each says so in its own docstring. They are not
  evidence about the shipped Windows behaviour.
- Collision policy: a pre-existing entry NOT owned by agentflow (no manifest
  record / no marker) is a loud refusal — never a skip, never clobbered, not
  even with --force. `--force` refreshes agentflow-owned entries whose deployed
  copy was locally modified.
- A deployment manifest (`<claude_root>/.agentflow-deploy.json`) records
  source -> target + content hash per entry (the seam for the future updater).
- Idempotent: re-run with no changes is a clean no-op, exit 0.
- `--dry-run` prints the step and mutates nothing.

Every test targets a tmp claude_root; the real `~/.claude` is never touched.

Run: `python -m pytest install/test_provision_deploy.py -v`
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402

REPO_ROOT = Path(provision.__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# Helpers — every opts points claude_root at a tmp dir, never ~/.claude
# --------------------------------------------------------------------------- #
def _make_opts(suite_root: Path, claude_root: Path, *, force: bool = False) -> provision.Options:
    return provision.Options(
        dry_run=False,
        suite_root=suite_root,
        config_home=suite_root / "config-home",
        settings_file=suite_root / "settings.json",
        autodoc=False,
        autodoc_reason="forced by --no-autodoc",
        claude_root=claude_root,
        force=force,
    )


def _make_suite(root: Path) -> Path:
    """Minimal fake suite: two skills, _shared, _vendored, one command."""
    (root / "skills" / "alpha").mkdir(parents=True)
    (root / "skills" / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\n---\n\nalpha body\n", encoding="utf-8"
    )
    (root / "skills" / "alpha" / "prompts").mkdir()
    (root / "skills" / "alpha" / "prompts" / "p.md").write_text("prompt\n", encoding="utf-8")
    (root / "skills" / "beta").mkdir()
    (root / "skills" / "beta" / "SKILL.md").write_text("beta body\n", encoding="utf-8")
    (root / "skills" / "_shared").mkdir()
    (root / "skills" / "_shared" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (root / "skills" / "_vendored" / "brainstorming").mkdir(parents=True)
    (root / "skills" / "_vendored" / "brainstorming" / "SKILL.md").write_text(
        "vendored\n", encoding="utf-8"
    )
    (root / "skills" / "NOTES.md").write_text("repo-side doc\n", encoding="utf-8")
    (root / "commands").mkdir()
    (root / "commands" / "alpha.md").write_text("Invoke the alpha skill.\n", encoding="utf-8")
    return root


@pytest.fixture()
def suite(tmp_path):
    return _make_suite(tmp_path / "suite")


@pytest.fixture()
def claude_root(tmp_path):
    return tmp_path / "claude-root"


@pytest.fixture()
def real_deploy_mode():
    """OPT OUT of the copy-mode pin: this test runs whatever the platform selects.

    Requesting this fixture is the whole opt-out mechanism - `_copy_mode` below checks
    for it by name and stands down. It exists because the unconditional pin it replaces
    made every test in this file blind to the mode the product actually ships: gh#296
    (Windows deployed a stale COPY of every skill) was green on the ubuntu leg AND on
    the windows leg of CI the entire time it was broken.

    Returns the mode so a test can assert against it instead of re-deriving it.
    """
    return provision._deploy_mode()


@pytest.fixture(autouse=True)
def _copy_mode(request, monkeypatch):
    """Pin copy mode - UNLESS the test requests `real_deploy_mode`.

    Copy mode still ships (file entries under junction mode, and the whole tree on a
    downgrade or a volume that cannot hold a junction), so the tests below that exercise
    copy MECHANICS are real coverage of a
    real path. They are just not coverage of the platform default, which is what the
    unconditional version of this fixture quietly implied for two years.
    """
    if "real_deploy_mode" in request.fixturenames:
        return
    monkeypatch.setattr(provision, "_deploy_mode", lambda: "copy")


requires_windows = pytest.mark.skipif(
    os.name != "nt",
    reason=(
        "junctions are a Windows-only filesystem object (_winapi.CreateJunction); there "
        "is no junction on this platform to exercise, so an honest skip is the correct "
        "outcome rather than a simulated one. The Unix half of the same contract - a "
        "deployed directory is a LIVE LINK, not a copy - is covered on every platform by "
        "test_platform_selected_mode_deploys_live_links_end_to_end, which runs symlink "
        "mode here and junction mode on Windows."
    ),
)


def _make_link(source: Path, target: Path) -> None:
    """Create the platform's own directory link: junction on Windows, symlink on Unix.

    Deliberately NOT `provision._deploy_entry` - the migration and ownership tests need a
    link that exists independently of the code under test.
    """
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(source), str(target))
    else:
        target.symlink_to(source, target_is_directory=True)


# --------------------------------------------------------------------------- #
# What deploys: skills + commands land under claude_root; _shared never does
# --------------------------------------------------------------------------- #
def test_deploys_skills_and_commands_to_claude_root(suite, claude_root):
    opts = _make_opts(suite, claude_root)

    provision._do_deploy_skills(opts)

    assert (claude_root / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8") \
        == "---\nname: alpha\n---\n\nalpha body\n"
    assert (claude_root / "skills" / "alpha" / "prompts" / "p.md").is_file()
    assert (claude_root / "skills" / "beta" / "SKILL.md").is_file()
    assert (claude_root / "commands" / "alpha.md").read_text(encoding="utf-8") \
        == "Invoke the alpha skill.\n"


def test_shared_is_never_deployed_vendored_is(suite, claude_root):
    opts = _make_opts(suite, claude_root)

    provision._do_deploy_skills(opts)

    # _shared is the pip package (step 5 editable install) — not a Claude skill.
    assert not (claude_root / "skills" / "_shared").exists()
    # _vendored IS deployed: skills reference it by relative sibling path
    # (../_vendored/...), which must resolve under claude_root post-deploy.
    assert (claude_root / "skills" / "_vendored" / "brainstorming" / "SKILL.md").is_file()
    # loose files at skills/ top level (NOTES.md) are not skills
    assert not (claude_root / "skills" / "NOTES.md").exists()


def test_real_repo_sources_exclude_shared_include_vendored_and_all_five_loops():
    keys = {k for k, _ in provision._deploy_sources(REPO_ROOT)}

    assert "skills/_shared" not in keys
    # every skills/ key must be a directory — loose top-level files never deploy
    for key, src in provision._deploy_sources(REPO_ROOT):
        if key.startswith("skills/"):
            assert src.is_dir(), f"non-directory deploy source under skills/: {key}"
    assert "skills/_vendored" in keys
    for loop in ("lead-dev", "dev-manager", "auditor", "jr-dev-1", "jr-dev-2"):
        assert f"skills/{loop}" in keys, f"skill missing from deploy set: {loop}"
        assert f"commands/{loop}.md" in keys, f"command wrapper missing: /{loop}"


def test_dev_manager_wrapper_exists_in_repo_and_deploys(tmp_path):
    src = REPO_ROOT / "commands" / "dev-manager.md"
    assert src.is_file(), "commands/dev-manager.md missing — /dev-manager cannot resolve"
    text = src.read_text(encoding="utf-8")
    assert text.startswith("---\n") and "description:" in text
    assert "dev-manager" in text

    claude_root = tmp_path / "cc"
    provision._do_deploy_skills(_make_opts(REPO_ROOT, claude_root))
    assert (claude_root / "commands" / "dev-manager.md").is_file()
    assert (claude_root / "skills" / "dev-manager" / "SKILL.md").is_file()


# --------------------------------------------------------------------------- #
# Collision policy — foreign entries are a loud refusal, never a clobber
# --------------------------------------------------------------------------- #
def test_foreign_skill_collision_refused_loudly(suite, claude_root):
    foreign = claude_root / "skills" / "alpha"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("someone else's alpha\n", encoding="utf-8")
    opts = _make_opts(suite, claude_root)

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_deploy_skills(opts)

    msg = str(excinfo.value)
    assert "alpha" in msg
    assert "not deployed by agentflow" in msg
    # refusal, not a skip: foreign content untouched
    assert (foreign / "SKILL.md").read_text(encoding="utf-8") == "someone else's alpha\n"


def test_force_does_not_clobber_foreign_entries(suite, claude_root):
    foreign = claude_root / "skills" / "alpha"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("someone else's alpha\n", encoding="utf-8")
    opts = _make_opts(suite, claude_root, force=True)

    with pytest.raises(RuntimeError):
        provision._do_deploy_skills(opts)

    assert (foreign / "SKILL.md").read_text(encoding="utf-8") == "someone else's alpha\n"


def test_foreign_command_collision_refused(suite, claude_root):
    (claude_root / "commands").mkdir(parents=True)
    (claude_root / "commands" / "alpha.md").write_text("foreign wrapper\n", encoding="utf-8")
    opts = _make_opts(suite, claude_root)

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_deploy_skills(opts)

    assert "alpha.md" in str(excinfo.value)
    assert (claude_root / "commands" / "alpha.md").read_text(encoding="utf-8") == "foreign wrapper\n"


# --------------------------------------------------------------------------- #
# --force refreshes agentflow-owned entries; without it, local edits refuse
# --------------------------------------------------------------------------- #
def test_locally_modified_owned_entry_refuses_without_force(suite, claude_root):
    """COPY-MODE MECHANICS ONLY - not evidence about the Windows default.

    The premise (hand-edit the deployed copy, observe drift from the source) cannot
    exist under a junction or a symlink, where the deployed thing IS the source and
    "drift" is not a state the filesystem can be in. Under the shipped Windows mode
    this scenario is unreachable for directory entries; it stays reachable for the 14
    FILE entries under junction mode, and on a downgrade back to copy.
    """
    opts = _make_opts(suite, claude_root)
    provision._do_deploy_skills(opts)
    deployed = claude_root / "skills" / "alpha" / "SKILL.md"
    deployed.write_text("hand-edited deployed copy\n", encoding="utf-8")

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_deploy_skills(opts)

    assert "--force" in str(excinfo.value)
    assert deployed.read_text(encoding="utf-8") == "hand-edited deployed copy\n"


def test_force_refreshes_locally_modified_owned_entry(suite, claude_root):
    """COPY-MODE MECHANICS ONLY - not evidence about the Windows default.

    Same reason as the test above: a hand-edited deployed copy is a state only copy
    mode can reach for a directory entry. Kept because copy mode still ships (all 14
    file entries, and every entry on a downgrade), not because it covers Windows.
    """
    provision._do_deploy_skills(_make_opts(suite, claude_root))
    deployed = claude_root / "skills" / "alpha" / "SKILL.md"
    deployed.write_text("hand-edited deployed copy\n", encoding="utf-8")

    provision._do_deploy_skills(_make_opts(suite, claude_root, force=True))

    assert deployed.read_text(encoding="utf-8") == "---\nname: alpha\n---\n\nalpha body\n"


def test_source_update_refreshes_owned_entry_on_plain_rerun(suite, claude_root):
    """COPY-MODE MECHANICS ONLY - and note what it proves is NOT what gh#296 needed.

    It proves a re-RUN of provision picks up a source change. gh#296 was that nobody
    ever re-runs provision after a merge, so this green test coexisted for two years
    with seven seats executing pre-merge instructions. Under the shipped Windows mode
    the source change is visible with no re-run at all - that is
    test_platform_selected_mode_deploys_live_links_end_to_end, not this.
    """
    opts = _make_opts(suite, claude_root)
    provision._do_deploy_skills(opts)
    (suite / "skills" / "alpha" / "SKILL.md").write_text("v2 body\n", encoding="utf-8")

    provision._do_deploy_skills(opts)  # owned + target unmodified -> refresh, no force needed

    assert (claude_root / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "v2 body\n"


# --------------------------------------------------------------------------- #
# Idempotency + manifest
# --------------------------------------------------------------------------- #
def test_rerun_with_no_changes_is_clean_noop(suite, claude_root, monkeypatch):
    opts = _make_opts(suite, claude_root)
    provision._do_deploy_skills(opts)

    # a true no-op: the second run must neither remove nor re-deploy anything
    # (mtime probes are vacuous — copy2 preserves source mtimes)
    calls = []
    real_remove, real_deploy = provision._remove_target, provision._deploy_entry
    monkeypatch.setattr(provision, "_remove_target",
                        lambda *a, **k: (calls.append("remove"), real_remove(*a, **k)))
    monkeypatch.setattr(provision, "_deploy_entry",
                        lambda *a, **k: (calls.append("deploy"), real_deploy(*a, **k)))

    provision._do_deploy_skills(opts)  # must not raise

    assert calls == []
    deployed = claude_root / "skills" / "alpha" / "SKILL.md"
    assert deployed.read_text(encoding="utf-8") == "---\nname: alpha\n---\n\nalpha body\n"


def test_manifest_written_with_source_target_and_hashes(suite, claude_root):
    provision._do_deploy_skills(_make_opts(suite, claude_root))

    manifest_path = claude_root / provision.DEPLOY_MANIFEST_NAME
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["entries"]
    for key in ("skills/alpha", "skills/beta", "skills/_vendored", "commands/alpha.md"):
        assert key in entries, f"manifest missing entry: {key}"
        e = entries[key]
        assert Path(e["source"]).exists()
        assert Path(e["target"]).exists()
        assert e["sha256"] == provision._content_hash(Path(e["source"]))
    assert "skills/_shared" not in entries


# --------------------------------------------------------------------------- #
# Platform mode selection + step wiring + dry-run
# --------------------------------------------------------------------------- #
def test_deploy_mode_is_junction_on_windows_and_symlink_on_unix():
    """gh#296: the Windows answer is a JUNCTION, never a copy.

    A copy is stale the instant a skill-touching merge lands, and nothing re-deploys -
    not the loop skills, not CI, not the steward. Junction rather than symlink because a
    Windows symlink needs admin or Developer Mode, which is a property of the machine and
    so cannot be a default; a junction needs neither.
    """
    assert provision._deploy_mode_for("nt") == "junction"
    assert provision._deploy_mode_for("posix") == "symlink"


def test_platform_selected_mode_deploys_live_links_end_to_end(suite, claude_root,
                                                              real_deploy_mode):
    """THE un-blinded test: runs whatever `_deploy_mode()` returns on this platform.

    Every other deploy test here is pinned to copy mode, which is how gh#296 stayed green
    on both CI legs while shipping broken. This one asserts the property that actually
    mattered - a deployed skill DIRECTORY tracks the checkout live, with no re-provision -
    on Windows via a junction and on Unix via a symlink.
    """
    assert real_deploy_mode == ("junction" if os.name == "nt" else "symlink")

    provision._do_deploy_skills(_make_opts(suite, claude_root))

    deployed = claude_root / "skills" / "alpha"
    assert provision._is_link(deployed), f"{real_deploy_mode} mode left a plain directory"
    # LIVE: the merge lands, nobody re-provisions, the seat still reads the new text
    (suite / "skills" / "alpha" / "SKILL.md").write_text("post-merge body\n", encoding="utf-8")
    (suite / "skills" / "alpha" / "prompts" / "added.md").write_text("new\n", encoding="utf-8")
    assert (deployed / "SKILL.md").read_text(encoding="utf-8") == "post-merge body\n"
    assert (deployed / "prompts" / "added.md").is_file()  # nested + newly created too
    # THE RESIDUE IS MODE-DEPENDENT, and asserting otherwise is how this file already got
    # one claim wrong. Junctions are directory-only, so under junction mode a file entry is
    # a copy and stays stale after a merge - that is gh#296's remainder. Under SYMLINK mode
    # `_deploy_entry` links files too, so the same path is a link and nothing is left over.
    # Only the linux leg can see the difference, which is why it was green locally.
    wrapper_is_link = provision._is_link(claude_root / "commands" / "alpha.md")
    assert wrapper_is_link == (real_deploy_mode == "symlink"), (
        f"{real_deploy_mode} mode: a commands/*.md wrapper should be "
        f"{'a link' if real_deploy_mode == 'symlink' else 'a copy'}"
    )
    assert provision._stale_copy_entries(suite, real_deploy_mode) == (
        [] if real_deploy_mode == "symlink" else ["commands/alpha.md"]
    )
    manifest = json.loads(
        (claude_root / provision.DEPLOY_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["mode"] == real_deploy_mode


def test_mode_blurb_is_derived_from_the_mode_not_from_the_platform():
    """It used to be `'Windows copies' if os.name == 'nt' else 'Unix symlinks'`.

    That is the line an operator reads to confirm a deploy change landed, so a value
    computed from the platform instead of from the mode is the worst kind of wrong: it
    would have gone on printing "Windows copies" after this fix shipped.
    """
    assert "junction" in provision._mode_blurb("junction")
    assert "live" in provision._mode_blurb("junction")
    assert "live" in provision._mode_blurb("symlink")
    assert "STALE" in provision._mode_blurb("copy")
    assert provision._mode_blurb("copy") != provision._mode_blurb("symlink")
    assert provision._mode_blurb("copy") != provision._mode_blurb("junction")


def test_deploy_step_ordered_after_editable_install_before_hooks():
    opts = _make_opts(Path("."), Path("./cc"))
    steps = provision.build_steps(opts)
    n = {s.run: s.n for s in steps if s.run is not None}
    assert n[provision._do_deploy_skills] > n[provision._do_editable_install]
    assert n[provision._do_deploy_skills] < n[provision._do_sessionstart_hook]
    # step numbers stay contiguous 1..N
    assert [s.n for s in steps] == list(range(1, len(steps) + 1))


def test_dry_run_prints_deploy_step_and_mutates_nothing(suite, tmp_path, capsys):
    claude_root = tmp_path / "cc-dry"

    rc = provision.main([
        "--dry-run", "--no-autodoc",
        "--suite-root", str(suite),
        "--claude-root", str(claude_root),
        "--config-home", str(tmp_path / "cfg"),
        "--settings-file", str(tmp_path / "settings.json"),
    ])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Deploy skills + commands" in out
    assert not claude_root.exists()  # nothing created, not even the root


def test_deploy_step_detail_names_the_real_mode_and_the_residue():
    """The step body an operator reads must carry BOTH halves of a partial fix.

    gh#296 is closed for the 22 directories and open for the 14 file entries. A step
    that printed only the new mode would read as total coverage.
    """
    opts = _make_opts(REPO_ROOT, Path("./cc"))
    step = next(s for s in provision.build_steps(opts) if s.run is provision._do_deploy_skills)
    body = "\n".join(step.detail)
    mode = provision._deploy_mode()

    assert f"mode: {mode}" in body
    assert provision._mode_blurb(mode) in body
    assert provision._stale_copy_blurb(REPO_ROOT, mode) in body


# --------------------------------------------------------------------------- #
# `_is_link` - the 3.11 trap the whole module's ownership logic sits on
# --------------------------------------------------------------------------- #
def test_is_link_false_for_plain_dirs_files_and_absent_paths(tmp_path):
    plain_dir = tmp_path / "d"
    plain_dir.mkdir()
    plain_file = tmp_path / "f.md"
    plain_file.write_text("x\n", encoding="utf-8")

    assert provision._is_link(plain_dir) is False
    assert provision._is_link(plain_file) is False
    assert provision._is_link(tmp_path / "does-not-exist") is False


@requires_windows
def test_is_link_true_for_a_junction_including_a_dangling_one(tmp_path):
    """`Path.is_symlink()` is False for a junction on 3.11 and `is_junction()` needs 3.12.

    That single fact is the shape of every bug this change fixes: an ownership check, a
    removal, and a re-point all branched on `is_symlink()` and so took the branch written
    for a real directory. `os.readlink` succeeds on a junction AND on a dangling one,
    which is exactly the case the re-point path has to be able to see.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "SKILL.md").write_text("body\n", encoding="utf-8")
    target = tmp_path / "tgt"
    _make_link(source, target)

    assert target.is_symlink() is False  # the trap, asserted so it cannot regress silently
    assert provision._is_link(target) is True

    os.rename(source, tmp_path / "src-moved")  # suite checkout moves: junction dangles

    assert target.exists() is False
    assert target.is_symlink() is False
    assert os.path.lexists(target) is True
    assert provision._is_link(target) is True


# --------------------------------------------------------------------------- #
# Windows junctions - the shipped Windows deploy path (gh#296)
# --------------------------------------------------------------------------- #
@requires_windows
def test_deployed_directory_is_a_junction_and_not_a_copied_dir(suite, claude_root,
                                                               real_deploy_mode):
    provision._do_deploy_skills(_make_opts(suite, claude_root))

    deployed = claude_root / "skills" / "alpha"
    assert real_deploy_mode == "junction"
    assert provision._is_link(deployed) is True
    assert deployed.is_symlink() is False  # a junction specifically, not a symlink
    assert Path(os.readlink(deployed)).name == "alpha"
    assert deployed.resolve() == (suite / "skills" / "alpha").resolve()
    # NOT a copied dir: copytree's ownership marker is what a copy leaves behind
    assert not (deployed / provision.DEPLOY_MARKER_NAME).exists()


@requires_windows
def test_junction_tracks_the_source_live_after_a_merge(suite, claude_root, real_deploy_mode):
    """The actual gh#296 scenario: a merge changes a SKILL.md and nobody re-provisions."""
    provision._do_deploy_skills(_make_opts(suite, claude_root))
    deployed = claude_root / "skills" / "alpha"
    assert (deployed / "SKILL.md").read_text(encoding="utf-8").endswith("alpha body\n")

    (suite / "skills" / "alpha" / "SKILL.md").write_text("merged body\n", encoding="utf-8")
    (suite / "skills" / "alpha" / "prompts" / "brand-new.md").write_text("new\n", encoding="utf-8")

    assert (deployed / "SKILL.md").read_text(encoding="utf-8") == "merged body\n"
    assert (deployed / "prompts" / "brand-new.md").read_text(encoding="utf-8") == "new\n"


@requires_windows
def test_remove_target_unlinks_a_junction_and_the_source_survives(tmp_path):
    """THE DATA-LOSS GUARD. `_remove_target` used to reach `shutil.rmtree` for a junction.

    3.11's rmtree refuses a reparse point outright ("Cannot call rmtree on a symbolic
    link"), which is it protecting the source from being recursed into and deleted - and
    is also a hard crash on every --force and every re-point. The canary asserts the
    source is untouched, not merely that no exception escaped.
    """
    source = tmp_path / "source-skill"
    source.mkdir()
    (source / "CANARY.md").write_text("the suite checkout must survive\n", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "deep.md").write_text("also survives\n", encoding="utf-8")
    target = tmp_path / "deployed-skill"
    _make_link(source, target)

    provision._remove_target(target)

    assert os.path.lexists(target) is False
    assert source.is_dir()
    assert (source / "CANARY.md").read_text(encoding="utf-8") == "the suite checkout must survive\n"
    assert (source / "nested" / "deep.md").is_file()


@requires_windows
def test_is_agentflow_owned_true_for_a_junction_with_an_empty_manifest(tmp_path):
    """Junction mode writes NO ownership marker, so the marker check judged it foreign.

    With an empty or reset manifest that refused every deployed skill as someone else's -
    a total provision failure, not a degraded one. Ownership for a link is proven by where
    it points, which the negative half below keeps honest.
    """
    suite_root = _make_suite(tmp_path / "suite")
    claude_root = tmp_path / "claude-root"
    (claude_root / "skills").mkdir(parents=True)

    source = suite_root / "skills" / "alpha"
    target = claude_root / "skills" / "alpha"
    _make_link(source, target)

    assert not (target / provision.DEPLOY_MARKER_NAME).exists()  # no marker to find
    assert provision._is_agentflow_owned(target, "skills/alpha", {}, suite_root, source) is True

    # negative half: a junction pointing OUTSIDE the suite is still foreign
    outsider = tmp_path / "someone-elses" / "alpha"
    outsider.mkdir(parents=True)
    (outsider / "SKILL.md").write_text("not ours\n", encoding="utf-8")
    foreign_target = claude_root / "skills" / "gamma"
    _make_link(outsider, foreign_target)

    assert provision._is_agentflow_owned(
        foreign_target, "skills/gamma", {}, suite_root, outsider
    ) is False


@requires_windows
def test_dangling_junction_is_repointed_not_a_fileexists_crash(tmp_path, claude_root,
                                                               real_deploy_mode):
    """Suite checkout moves -> every junction dangles. The old read was "nothing there".

    `not target.exists() and not target.is_symlink()` is True for a dangling junction, so
    the deploy path treated the slot as empty and then created over it: FileExistsError,
    WinError 183. `os.path.lexists` is the read that sees it.
    """
    suite_a = _make_suite(tmp_path / "suite-a")
    provision._do_deploy_skills(_make_opts(suite_a, claude_root))
    deployed = claude_root / "skills" / "alpha"
    assert provision._is_link(deployed)

    suite_b = tmp_path / "suite-b"
    os.rename(suite_a, suite_b)
    assert os.path.lexists(deployed) is True and deployed.exists() is False

    provision._do_deploy_skills(_make_opts(suite_b, claude_root))  # must not raise

    assert deployed.exists() is True
    assert deployed.resolve() == (suite_b / "skills" / "alpha").resolve()
    assert (deployed / "SKILL.md").read_text(encoding="utf-8").endswith("alpha body\n")


@requires_windows
def test_junction_mode_writes_no_ownership_marker_into_the_source(suite, claude_root,
                                                                 real_deploy_mode):
    """The copytree branch writes `.agentflow-deployed` INTO the target.

    Under a junction the target IS the suite checkout, so a marker written into the target
    lands in the repo - 22 spurious untracked files in someone's working tree, and worse,
    a file the ownership check would then find on the SOURCE side. This is why the junction
    branch sits ABOVE copytree with its own condition instead of being folded into the
    directory branch with a shared marker write, which is the obvious refactor and the one
    that reintroduces the bug.

    RED-PROOF NOTE. The source half of this is only falsifiable by that refactor (M14 in
    the mutation run), not by deleting the junction branch outright: with no junction
    created there is nothing to write through, so copytree marks a plain copy and the
    checkout stays clean by accident. The target half is killed by the branch deletion.
    """
    provision._do_deploy_skills(_make_opts(suite, claude_root))

    # target half: a junction is not a copy, so there is nothing to mark
    assert not (claude_root / "skills" / "alpha" / provision.DEPLOY_MARKER_NAME).exists()
    # source half: nothing was written back through the link into the checkout
    for name in ("alpha", "beta", "_vendored"):
        assert not (suite / "skills" / name / provision.DEPLOY_MARKER_NAME).exists()
    assert list(suite.rglob(provision.DEPLOY_MARKER_NAME)) == []


# --------------------------------------------------------------------------- #
# Mode migration - bytes cannot detect a copy that should now be a link
# --------------------------------------------------------------------------- #
def test_mode_mismatch_flags_an_unchanged_copy_when_the_mode_is_now_a_link(tmp_path):
    """The migration hole: an unchanged copy hashes EQUAL to its source.

    So no content-drift branch fires, the entry stays a copy, and the manifest records
    the new mode anyway - a record that disagrees with the disk it describes. The check
    has to be on the target's KIND, because bytes are what a link makes indistinguishable.
    """
    source = tmp_path / "src"
    source.mkdir()
    (source / "SKILL.md").write_text("body\n", encoding="utf-8")
    target = tmp_path / "tgt"
    provision._deploy_entry(source, target, "copy")

    assert provision._content_hash(target) == provision._content_hash(source)  # bytes agree
    assert provision._mode_mismatch(target, source, "junction") is True
    assert provision._mode_mismatch(target, source, "symlink") is True
    assert provision._mode_mismatch(target, source, "copy") is False


def test_mode_mismatch_flags_a_link_when_the_mode_is_now_copy(tmp_path):
    """The reverse: a downgrade back to copy mode must convert, not silently keep a link."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "SKILL.md").write_text("body\n", encoding="utf-8")
    target = tmp_path / "tgt"
    _make_link(source, target)

    assert provision._mode_mismatch(target, source, "copy") is True
    assert provision._mode_mismatch(target, source, "junction") is False
    assert provision._mode_mismatch(target, source, "symlink") is False


def test_mode_mismatch_reads_a_FILE_entry_PER_MODE_because_symlink_mode_links_files_too(tmp_path):
    """A copied FILE is correct under copy and junction mode and WRONG under symlink mode.

    This previously asserted False for all three, under the docstring 'a file entry is a copy
    under every mode'. That is false: `_deploy_entry`'s first branch is
    `if mode == "symlink": target.symlink_to(...)`, which covers files. Pinning the wrong
    claim would have kept a Unix box's adopted `commands/*.md` as plain copies forever while
    the manifest recorded mode symlink - a record disagreeing with its own disk, which is the
    failure the migration branch exists to prevent.
    """
    source = tmp_path / 'alpha.md'
    source.write_text('wrapper', encoding='utf-8')
    target = tmp_path / 'deployed.md'
    provision._deploy_entry(source, target, 'copy')

    assert provision._mode_mismatch(target, source, 'copy') is False
    assert provision._mode_mismatch(target, source, 'junction') is False, (
        'junctions are directory-only, so a file entry is correctly a copy'
    )
    assert provision._mode_mismatch(target, source, 'symlink') is True, (
        'symlink mode links files too, so a plain copy IS the wrong kind here'
    )


def test_existing_copy_is_converted_to_a_link_when_the_mode_changes(suite, claude_root,
                                                                    monkeypatch,
                                                                    real_deploy_mode):
    """End-to-end migration on an UNCHANGED source - the case bytes cannot see."""
    monkeypatch.setattr(provision, "_deploy_mode", lambda: "copy")
    provision._do_deploy_skills(_make_opts(suite, claude_root))
    target = claude_root / "skills" / "alpha"
    assert provision._is_link(target) is False
    assert (target / provision.DEPLOY_MARKER_NAME).is_file()

    monkeypatch.setattr(provision, "_deploy_mode", lambda: real_deploy_mode)
    provision._do_deploy_skills(_make_opts(suite, claude_root))  # source UNTOUCHED

    assert provision._is_link(target) is True
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "---\nname: alpha\n---\n\nalpha body\n"
    # the marker went with the copy and was NOT written through into the checkout
    assert list(suite.rglob(provision.DEPLOY_MARKER_NAME)) == []
    manifest = json.loads(
        (claude_root / provision.DEPLOY_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["mode"] == real_deploy_mode


def test_existing_link_is_converted_back_to_a_copy_on_downgrade(suite, claude_root,
                                                                monkeypatch,
                                                                real_deploy_mode):
    """Downgrade to copy mode, with a canary proving the source is not consumed."""
    provision._do_deploy_skills(_make_opts(suite, claude_root))
    target = claude_root / "skills" / "alpha"
    assert provision._is_link(target) is True
    canary = suite / "skills" / "alpha" / "CANARY.md"
    canary.write_text("source must survive the downgrade\n", encoding="utf-8")

    monkeypatch.setattr(provision, "_deploy_mode", lambda: "copy")
    provision._do_deploy_skills(_make_opts(suite, claude_root))

    assert provision._is_link(target) is False
    assert (target / provision.DEPLOY_MARKER_NAME).is_file()
    assert canary.read_text(encoding="utf-8") == "source must survive the downgrade\n"
    assert not (suite / "skills" / "alpha" / provision.DEPLOY_MARKER_NAME).exists()


# --------------------------------------------------------------------------- #
# Residue - what junction mode still cannot keep live, named rather than hidden
# --------------------------------------------------------------------------- #
def test_stale_copy_entries_are_exactly_the_file_sources_under_junction_mode(suite):
    by_key = dict(provision._deploy_sources(suite))
    stale = provision._stale_copy_entries(suite, "junction")

    assert stale, "junctions are directory-only; every file entry stays a copy"
    assert set(stale) == {k for k, src in by_key.items() if not src.is_dir()}
    assert all(not by_key[key].is_dir() for key in stale)
    assert stale == sorted(stale)


def test_stale_copy_entries_empty_under_symlink_mode(suite):
    """Unix symlinks cover files too, so there is no residue to report."""
    assert provision._stale_copy_entries(suite, "symlink") == []


def test_stale_copy_blurb_names_the_count_and_the_entries(suite):
    blurb = provision._stale_copy_blurb(suite, "junction")
    stale = provision._stale_copy_entries(suite, "junction")

    assert "STILL COPIED" in blurb
    assert str(len(stale)) in blurb
    assert "1 file entry" in blurb  # singular branch: the fake suite has one command
    assert stale[0] in blurb
    assert "gh#296" in blurb


def test_stale_copy_blurb_is_clean_under_symlink_mode(suite):
    assert "nothing goes stale" in provision._stale_copy_blurb(suite, "symlink")
    assert "STILL COPIED" not in provision._stale_copy_blurb(suite, "symlink")


def test_real_repo_stale_copy_entries_are_the_file_wrappers_only():
    """Property is the load-bearing assertion; the literal 14 is a docstring canary.

    The property (exactly the non-directory sources, nothing under skills/) is what the
    blurb claims and it survives someone adding a command wrapper. The literal is asserted
    anyway because `_deploy_mode_for`'s docstring tells an operator "22 are directories and
    14 are files" in prose - if the count moves and that sentence does not, the shipped
    explanation of a partially-closed bug becomes wrong.
    """
    by_key = dict(provision._deploy_sources(REPO_ROOT))
    stale = provision._stale_copy_entries(REPO_ROOT, "junction")

    assert set(stale) == {k for k, src in by_key.items() if not src.is_dir()}
    assert not any(key.startswith("skills/") for key in stale)
    assert "scripts/plan-lint.sh" in stale
    assert "commands/lead-dev.md" in stale
    assert len(stale) == 14, (
        "file-entry count moved; update the '22 are directories and 14 are files' "
        "sentence in _deploy_mode_for's docstring in the same change"
    )
    plural = provision._stale_copy_blurb(REPO_ROOT, "junction")
    assert "14 file entries" in plural  # plural branch


# --------------------------------------------------------------------------- #
# gh#328 - the 14 FILE entries still deploy as copies, so they can still wedge the
# whole step, and the refusal could not say which of two very different shapes it
# was looking at. 22703cd fixed the 22 directories; these were left behind.
# --------------------------------------------------------------------------- #
def _tree_snapshot(root):
    """Every path under `root` with its bytes.

    A refused deploy must leave this IDENTICAL - the gh#110 pre-check judges all 36
    entries before writing any - so a per-entry assertion would miss a half-write.
    """
    if not root.exists():
        return {}
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        out[rel] = p.read_bytes() if p.is_file() and not provision._is_link(p) else None
    return out


def _git(repo, *args):
    """Run git in `repo` with an identity, so these tests need no global config."""
    return subprocess.run(
        ["git", "-C", str(repo),
         "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        capture_output=True, text=True, check=True,
    )


@pytest.fixture()
def git_suite(suite):
    """The fake suite, as a real one-commit checkout."""
    if shutil.which("git") is None:  # pragma: no cover - git is present on CI
        pytest.skip("git not on PATH")
    _git(suite, "init", "-q")
    _git(suite, "add", "-A")
    _git(suite, "commit", "-q", "-m", "v1")
    return suite


def test_matches_a_repo_version_recognises_a_SUPERSEDED_version(git_suite, tmp_path):
    """The gh#328 shape exactly: the deployed copy holds what the repo used to hold.

    This is also the test that rejects a stdlib blob-id reimplementation. Under
    `core.autocrlf=true` the working file is CRLF and the stored blob is LF, so hashing
    the bytes in Python answers "no match" for every real case on Windows - green on the
    linux leg, useless on the platform that has the bug.
    """
    source = git_suite / "commands" / "alpha.md"
    was = source.read_bytes()
    source.write_bytes(b"Invoke the alpha skill, revised.\n")
    _git(git_suite, "commit", "-q", "-am", "v2")
    deployed = tmp_path / "deployed-alpha.md"
    deployed.write_bytes(was)

    assert provision._matches_a_repo_version(git_suite, source, deployed)


def test_matches_a_repo_version_hashes_AS_THE_SOURCE_PATH_not_the_deployed_one(
    git_suite, tmp_path
):
    """Pins `--path`, which a mutation run showed nothing else did.

    Dropping `--path` reads perfectly and survived every other test here, because plain
    `core.autocrlf` is CONFIG-wide: both forms convert identically and agree. Git's filters
    are also PATH-dependent though, and that is the half that bites - `.gitattributes`
    marking this glob `-text` stores CRLF verbatim, while a path-less hash still applies
    autocrlf and produces a different id for the same bytes. Hashing the deployed copy
    under its own temporary path would quietly answer for the wrong ruleset.
    """
    (git_suite / ".gitattributes").write_bytes(b"commands/*.md -text\n")
    source = git_suite / "commands" / "alpha.md"
    source.write_bytes(b"crlf body\r\n")
    _git(git_suite, "add", "-A")
    _git(git_suite, "commit", "-q", "-m", "attrs")
    was = source.read_bytes()
    source.write_bytes(b"moved on\r\n")
    _git(git_suite, "commit", "-q", "-am", "v3")
    deployed = tmp_path / "deployed-alpha.md"
    deployed.write_bytes(was)

    assert provision._matches_a_repo_version(git_suite, source, deployed)


def test_matches_a_repo_version_is_FALSE_for_bytes_never_committed(git_suite, tmp_path):
    source = git_suite / "commands" / "alpha.md"
    deployed = tmp_path / "deployed-alpha.md"
    deployed.write_bytes(b"nobody ever committed this\n")

    assert not provision._matches_a_repo_version(git_suite, source, deployed)


def test_matches_a_repo_version_degrades_to_False_outside_a_checkout(suite, tmp_path):
    # Must never raise into the refusal: no checkout means "cannot tell", which keeps the
    # conservative wording rather than failing the deploy.
    deployed = tmp_path / "deployed-alpha.md"
    deployed.write_bytes(b"anything\n")

    assert not provision._matches_a_repo_version(suite, suite / "commands" / "alpha.md", deployed)


def test_a_drifted_FILE_entry_aborts_the_WHOLE_deploy(suite, claude_root):
    """One hand-touched command wrapper blocks every skill and command (gh#110 + gh#328).

    COVERAGE GAP, not a regression guard for this change: the refusal already behaved this
    way and nothing pinned it. The 22 skill dirs stopped being able to reach this state in
    22703cd; the 14 file entries never did.
    """
    opts = _make_opts(suite, claude_root)
    provision._do_deploy_skills(opts)
    (claude_root / "commands" / "alpha.md").write_text("hand-touched\n", encoding="utf-8")
    before = _tree_snapshot(claude_root)

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_deploy_skills(opts)

    assert "ENTIRE deploy" in str(excinfo.value), "the refusal hides its own blast radius"
    assert _tree_snapshot(claude_root) == before, "a refused deploy wrote something"


def test_refusal_NAMES_an_earlier_repo_version_rather_than_local_work(
    suite, claude_root, monkeypatch
):
    # The reader of this message decides whether to --force. Told "local edits will be
    # LOST" about a file holding nothing of theirs, an honest reader stops - which is
    # what happened, for hours, in the incident gh#328 was filed about.
    opts = _make_opts(suite, claude_root)
    provision._do_deploy_skills(opts)
    (claude_root / "commands" / "alpha.md").write_text("an older repo version\n", encoding="utf-8")
    monkeypatch.setattr(provision, "_matches_a_repo_version", lambda root, src, tgt: True)

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_deploy_skills(opts)

    msg = str(excinfo.value)
    assert "EARLIER REPO VERSION" in msg, msg
    assert "local edits will be LOST" not in msg, "still threatening work that is not there"


def test_refusal_says_locally_modified_when_the_bytes_match_no_commit(
    suite, claude_root, monkeypatch
):
    # The other direction, and the one that must stay conservative: an unrecognised body
    # is somebody's work until proven otherwise.
    opts = _make_opts(suite, claude_root)
    provision._do_deploy_skills(opts)
    (claude_root / "commands" / "alpha.md").write_text("genuinely mine\n", encoding="utf-8")
    monkeypatch.setattr(provision, "_matches_a_repo_version", lambda root, src, tgt: False)

    with pytest.raises(RuntimeError) as excinfo:
        provision._do_deploy_skills(opts)

    msg = str(excinfo.value)
    assert "locally modified" in msg, msg
    assert "local edits will be LOST" in msg
    assert "EARLIER REPO VERSION" not in msg


def test_a_DIRECTORY_entry_is_never_asked_the_repo_version_question(
    suite, claude_root, monkeypatch
):
    # A directory has no blob to compare, so the discriminator must not be consulted for
    # one - and under junction mode it cannot drift at all. Guards against a fix that
    # reads plausibly and calls git once per skill on every refusal.
    asked = []
    monkeypatch.setattr(
        provision, "_matches_a_repo_version",
        lambda root, src, tgt: asked.append(tgt) or True,
    )
    opts = _make_opts(suite, claude_root)
    provision._do_deploy_skills(opts)
    (claude_root / "skills" / "alpha" / "SKILL.md").write_text("edited\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        provision._do_deploy_skills(opts)

    assert asked == [], f"the discriminator was asked about a directory: {asked}"


def test_file_entries_are_copies_and_can_still_drift_under_the_REAL_mode(
    suite, claude_root, real_deploy_mode
):
    """Not copy-mode mechanics: this runs whatever the platform ships.

    Under symlink mode a file entry IS the source and cannot drift, so there is nothing to
    assert; under junction mode it is a copy and the gh#328 shape is live. Skipping rather
    than asserting both ways keeps the windows leg honest instead of averaging two
    platforms into a claim true on neither.
    """
    if real_deploy_mode == "symlink":
        pytest.skip("file entries are links under symlink mode; drift is unreachable")
    opts = _make_opts(suite, claude_root)
    provision._do_deploy_skills(opts)
    deployed = claude_root / "commands" / "alpha.md"
    assert not provision._is_link(deployed), "a file entry was deployed as a link"

    deployed.write_text("drifted under the shipped mode\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        provision._do_deploy_skills(opts)


# --------------------------------------------------------------------------- #
# gh#985 item 2 - re-provisioning from a second checkout re-points, never doubles
# --------------------------------------------------------------------------- #
def _sessionstart_commands(settings_file: Path) -> list:
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    return [h["command"] for block in settings["hooks"]["SessionStart"]
            for h in block.get("hooks", [])]


def test_reprovision_from_a_second_checkout_repoints_links_and_replaces_the_hook(
    tmp_path, claude_root, real_deploy_mode, monkeypatch
):
    """The issue's repro: provision from repo/, then from an identical clone repo2/.

    An identical clone hashes EQUAL, so the old hash-only rule kept every link pointing at
    repo/ while the manifest recorded repo2/, and the exact-string hook dedupe appended a
    second SessionStart entry. Deleting repo/ then dangled every seat and left a hook
    exiting 127. A foreign SessionStart entry is the control: it must survive untouched.
    """
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    repo = _make_suite(tmp_path / "repo")
    repo2 = _make_suite(tmp_path / "repo2")
    settings_file = tmp_path / "settings.json"
    foreign = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo mine"}]}
    settings_file.write_text(json.dumps({"hooks": {"SessionStart": [foreign]}}),
                             encoding="utf-8")

    for checkout in (repo, repo2):
        opts = _make_opts(checkout, claude_root)
        opts.settings_file = settings_file
        opts.config_home = tmp_path / "cfg"   # one config home, two checkouts
        provision._do_deploy_skills(opts)
        provision._do_sessionstart_hook(opts)

    links = [claude_root / key for key, _src in provision._deploy_sources(repo2)
             if provision._is_link(claude_root / key)]
    assert links, f"{real_deploy_mode} mode deployed no link to check"
    for link in links:
        assert link.resolve().is_relative_to(repo2.resolve()), (
            f"{link} still resolves to {link.resolve()}, not into {repo2}")
    manifest = json.loads((claude_root / provision.DEPLOY_MANIFEST_NAME).read_text("utf-8"))
    assert manifest["suite_root"] == str(repo2)

    cmds = _sessionstart_commands(settings_file)
    ours = [c for c in cmds if provision._is_agentflow_sessionstart(c)]
    assert ours == [f'"{provision._venv_python(_make_opts(repo2, claude_root))}" '
                    f'"{repo2 / provision.SESSIONSTART_HOOK_REL}"'], cmds
    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["hooks"]["SessionStart"][0] == foreign, "a foreign entry was touched"


def test_a_link_the_manifest_does_not_account_for_is_not_repointed(
    tmp_path, claude_root, real_deploy_mode
):
    """Ownership is the manifest key PLUS a link landing on this entry's slot in a checkout.

    An operator re-points a deployed skill at their own tree (same content, so no hash
    drift). The key is still in the manifest, but `theirs/alpha` neither equals the
    recorded source nor ends in `skills/alpha`, so re-provision from repo2/ leaves their
    link exactly where they put it.
    """
    repo = _make_suite(tmp_path / "repo")
    repo2 = _make_suite(tmp_path / "repo2")
    provision._do_deploy_skills(_make_opts(repo, claude_root))
    target = claude_root / "skills" / "alpha"
    if not provision._is_link(target):
        pytest.skip(f"{real_deploy_mode} mode deploys no directory link")
    theirs = tmp_path / "theirs" / "alpha"
    shutil.copytree(repo / "skills" / "alpha", theirs)
    provision._remove_target(target)
    _make_link(theirs, target)

    provision._do_deploy_skills(_make_opts(repo2, claude_root))

    assert target.resolve() == theirs.resolve()


def _rewrite_manifest_to(claude_root: Path, old: Path, new: Path) -> None:
    """Leave the manifest the way the pre-gh#985 code did: every recorded source already
    names the NEW checkout while the links on disk still point into the OLD one."""
    path = claude_root / provision.DEPLOY_MANIFEST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data["suite_root"] = str(new)
    for entry in data["entries"].values():
        entry["source"] = entry["source"].replace(str(old), str(new))
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _deployed_links(claude_root: Path, suite_root: Path) -> list:
    return [claude_root / key for key, _src in provision._deploy_sources(suite_root)
            if provision._is_link(claude_root / key)]


def _add_plan_lint(root: Path) -> Path:
    """The one deploy entry whose key (scripts/plan-lint.sh) is NOT its suite-relative
    path; `_deploy_sources` ships it only when this exact file exists."""
    script = root / "skills" / "_shared" / "scripts" / "plan-lint.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return script


def _assert_plan_lint_survives(claude_root: Path, repo2: Path) -> None:
    """Run AFTER repo/ is gone: a link must point into repo2/, a copy must still exist."""
    deployed = claude_root / "scripts" / "plan-lint.sh"
    if provision._is_link(deployed):
        assert deployed.exists() and deployed.resolve().is_relative_to(repo2.resolve()), (
            f"{deployed} resolves to {deployed.resolve()}, not into {repo2}")
    else:
        assert deployed.is_file(), f"{deployed} vanished with the old checkout"


def test_reprovision_repoints_owned_links_after_the_manifest_was_already_rewritten(
    tmp_path, claude_root, real_deploy_mode, capsys
):
    """gh#985 auditor repro: the manifest already records repo2/, the links still point at
    repo/. The recorded source no longer equals the link target, so an exact-match rule
    never re-points them - and deleting repo/ then dangles skills/alpha, beta, _vendored.
    The re-point is also announced on stderr, since the record did not name the old target."""
    repo = _make_suite(tmp_path / "repo")
    repo2 = _make_suite(tmp_path / "repo2")
    _add_plan_lint(repo)
    _add_plan_lint(repo2)
    provision._do_deploy_skills(_make_opts(repo, claude_root))
    links = _deployed_links(claude_root, repo)
    assert links, f"{real_deploy_mode} mode deployed no link to check"
    assert all(link.resolve().is_relative_to(repo.resolve()) for link in links)
    _rewrite_manifest_to(claude_root, repo, repo2)
    capsys.readouterr()

    provision._do_deploy_skills(_make_opts(repo2, claude_root))

    err = capsys.readouterr().err
    for link in links:
        assert link.resolve().is_relative_to(repo2.resolve()), (
            f"{link} still resolves to {link.resolve()}, not into {repo2}")
        assert f"provision: re-pointed {link} from " in err, err
    shutil.rmtree(repo)
    assert all(link.exists() for link in links), "a seat dangled once repo/ was deleted"
    _assert_plan_lint_survives(claude_root, repo2)


def test_reprovision_repoints_owned_links_the_deleted_checkout_left_dangling(
    tmp_path, claude_root, real_deploy_mode
):
    """Same state, but the operator deleted repo/ BEFORE re-provisioning: every owned
    link now dangles, and re-provision from repo2/ must re-point it, not crash or skip."""
    repo = _make_suite(tmp_path / "repo")
    repo2 = _make_suite(tmp_path / "repo2")
    _add_plan_lint(repo)
    _add_plan_lint(repo2)
    provision._do_deploy_skills(_make_opts(repo, claude_root))
    links = _deployed_links(claude_root, repo)
    assert links, f"{real_deploy_mode} mode deployed no link to check"
    _rewrite_manifest_to(claude_root, repo, repo2)
    shutil.rmtree(repo)
    assert not any(link.exists() for link in links), "setup: links should dangle"

    provision._do_deploy_skills(_make_opts(repo2, claude_root))

    for link in links:
        assert link.exists() and link.resolve().is_relative_to(repo2.resolve()), link
    _assert_plan_lint_survives(claude_root, repo2)


def test_reprovision_leaves_foreign_links_and_user_directories_alone(
    tmp_path, claude_root, real_deploy_mode
):
    """Owned means the manifest key AND a link landing on this entry's slot in a checkout.

    - Key in the manifest, but the operator re-pointed it at their own tree: left alone,
      even with the manifest already rewritten (the state this epic re-points from).
    - A link shaped exactly like a checkout's slot under a key the manifest never
      recorded: foreign, so the deploy refuses and touches nothing.
    - A user's real directory under such a key: the same refusal, the same untouched tree.

    The gamma half guards the OUTER `_is_agentflow_owned` gate in `_refuse_undeployable`;
    the key check inside `_owned_link_elsewhere` is defensive only and never decides it.
    """
    repo = _make_suite(tmp_path / "repo")
    repo2 = _make_suite(tmp_path / "repo2")
    provision._do_deploy_skills(_make_opts(repo, claude_root))
    alpha = claude_root / "skills" / "alpha"
    if not provision._is_link(alpha):
        pytest.skip(f"{real_deploy_mode} mode deploys no directory link")
    theirs = tmp_path / "theirs" / "alpha"
    shutil.copytree(repo / "skills" / "alpha", theirs)
    provision._remove_target(alpha)
    _make_link(theirs, alpha)
    _rewrite_manifest_to(claude_root, repo, repo2)

    provision._do_deploy_skills(_make_opts(repo2, claude_root))

    assert alpha.resolve() == theirs.resolve(), "an operator's own link was hijacked"

    # A slot the manifest never recorded, holding a checkout-shaped foreign link.
    (repo2 / "skills" / "gamma").mkdir()
    (repo2 / "skills" / "gamma" / "SKILL.md").write_text("gamma\n", encoding="utf-8")
    other = _make_suite(tmp_path / "other-clone")
    (other / "skills" / "gamma").mkdir()
    (other / "skills" / "gamma" / "SKILL.md").write_text("gamma\n", encoding="utf-8")
    gamma = claude_root / "skills" / "gamma"
    _make_link(other / "skills" / "gamma", gamma)
    with pytest.raises(RuntimeError, match="not deployed by agentflow"):
        provision._do_deploy_skills(_make_opts(repo2, claude_root))
    assert gamma.resolve() == (other / "skills" / "gamma").resolve()

    # The same slot as a user's real directory.
    provision._remove_target(gamma)
    gamma.mkdir()
    (gamma / "mine.md").write_text("user content\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not deployed by agentflow"):
        provision._do_deploy_skills(_make_opts(repo2, claude_root))
    assert not provision._is_link(gamma)
    assert sorted(p.name for p in gamma.iterdir()) == ["mine.md"]


def test_reprovision_never_touches_a_link_already_inside_the_suite_root(
    tmp_path, claude_root, real_deploy_mode, monkeypatch
):
    """A link into the CURRENT checkout is left exactly as it is - even when the manifest
    records another checkout's source for it - so a re-run removes nothing."""
    repo = _make_suite(tmp_path / "repo")
    repo2 = _make_suite(tmp_path / "repo2")
    provision._do_deploy_skills(_make_opts(repo2, claude_root))
    links = _deployed_links(claude_root, repo2)
    assert links, f"{real_deploy_mode} mode deployed no link to check"
    _rewrite_manifest_to(claude_root, repo2, repo)   # record names the OTHER checkout
    removed = []
    real_remove = provision._remove_target
    monkeypatch.setattr(provision, "_remove_target",
                        lambda t: (removed.append(Path(t)), real_remove(t)))

    provision._do_deploy_skills(_make_opts(repo2, claude_root))

    assert not set(removed) & set(links), f"in-suite links were removed: {removed}"
    for link in links:
        assert link.resolve().is_relative_to(repo2.resolve()), link


def test_a_wrapper_running_our_hook_script_is_not_claimed(tmp_path, claude_root, monkeypatch):
    """Only the command step 9 writes is agentflow's: the suite venv python running the
    hook script from the SAME checkout. An operator's wrapper around our script is theirs."""
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    repo = _make_suite(tmp_path / "repo")
    wrapper = (f'"{tmp_path / "tools" / "wrap.exe"}" '
               f'"{tmp_path / "x" / provision.SESSIONSTART_HOOK_REL}"')
    other_venv = (f'"{provision._venv_python(_make_opts(tmp_path / "other", claude_root))}" '
                  f'"{repo / provision.SESSIONSTART_HOOK_REL}"')
    entries = [{"hooks": [{"type": "command", "command": c}]} for c in (wrapper, other_venv)]
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"hooks": {"SessionStart": entries}}), encoding="utf-8")
    opts = _make_opts(repo, claude_root)
    opts.settings_file = settings_file
    opts.config_home = tmp_path / "cfg"

    provision._do_sessionstart_hook(opts)

    saved = json.loads(settings_file.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    assert saved[:2] == entries, "a non-agentflow entry was rewritten or dropped"
    assert len(saved) == 3 and provision._is_agentflow_sessionstart(
        saved[2]["hooks"][0]["command"])


# --------------------------------------------------------------------------- #
# --deploy-only: step 7 alone, so a lane's skill deploy writes no project's data
# --------------------------------------------------------------------------- #
def _two_project_config(tmp_path: Path) -> tuple[Path, Path]:
    """A config home with two configured projects and a data_root holding one partial subtree.

    `otherproj`'s subtree is deliberately incomplete (directories, no seeded files), so a
    run that reached step 11 would visibly add to it. Returns (config_home, data_root).
    """
    data_root = tmp_path / "data"
    config_home = tmp_path / "cfg"
    (config_home / "projects").mkdir(parents=True)
    (config_home / "environment.md").write_text(
        f"```yaml\ndata_root: {data_root.as_posix()}\n```\n", encoding="utf-8")
    for name in ("homeproj", "otherproj"):
        (config_home / "projects" / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    other = data_root / "projects" / "otherproj"
    (other / "wiki" / "pages").mkdir(parents=True)
    (other / "status").mkdir(parents=True)
    (other / "status" / "notes.md").write_text("left alone\n", encoding="utf-8")
    return config_home, data_root


def _tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


def _deploy_only_argv(suite: Path, claude_root: Path, config_home: Path, tmp_path: Path,
                      *extra: str) -> list[str]:
    return ["--deploy-only", *extra,
            "--suite-root", str(suite),
            "--claude-root", str(claude_root),
            "--config-home", str(config_home),
            "--settings-file", str(tmp_path / "settings.json")]


def test_deploy_only_deploys_and_never_touches_another_projects_subtree(
        suite, claude_root, tmp_path, capsys):
    config_home, data_root = _two_project_config(tmp_path)
    before = _tree(data_root)

    rc = provision.main(_deploy_only_argv(suite, claude_root, config_home, tmp_path))

    out = capsys.readouterr().out
    assert rc == 0
    assert (claude_root / "skills" / "alpha" / "SKILL.md").is_file()
    assert (claude_root / "commands" / "alpha.md").is_file()
    assert (claude_root / provision.DEPLOY_MANIFEST_NAME).is_file()
    assert _tree(data_root) == before, "deploy-only wrote into a project's data subtree"
    assert not (data_root / "projects" / "homeproj").exists()
    # Step 7 is the only step that ran; the settings file steps 8 + 9 write never appeared.
    assert re.findall(r"^  (\d+)\. ", out, re.MULTILINE) == ["7", "7"]  # start + done
    # The apply header names only what step 7 reads and writes.
    assert f"  claude_root : {claude_root}" in out
    for absent in ("config_home :", "settings    :", "autodoc     :", "auto-allow  :"):
        assert absent not in out, absent
    assert not (tmp_path / "settings.json").exists()


def test_deploy_only_dry_run_lists_only_the_deploy_step(suite, claude_root, tmp_path, capsys):
    config_home, _ = _two_project_config(tmp_path)

    rc = provision.main(_deploy_only_argv(suite, claude_root, config_home, tmp_path, "--dry-run"))

    out = capsys.readouterr().out
    assert rc == 0
    assert provision.DEPLOY_ONLY_NOTE in out
    assert re.findall(r"^  (\d+)\. ", out, re.MULTILINE) == ["7"]
    assert "Scaffold data_root" not in out
    assert not claude_root.exists()


def test_full_dry_run_still_lists_the_scaffold_step(suite, claude_root, tmp_path, capsys):
    """The other half of the split: without the flag nothing changes, step 11 included."""
    config_home, _ = _two_project_config(tmp_path)
    argv = _deploy_only_argv(suite, claude_root, config_home, tmp_path, "--dry-run")
    argv.remove("--deploy-only")

    assert provision.main(argv) == 0
    full = capsys.readouterr().out
    assert provision.main([*argv, "--deploy-only"]) == 0
    reduced = capsys.readouterr().out

    assert "11. Scaffold data_root" in full and "11. Scaffold data_root" not in reduced
    assert provision.DEPLOY_ONLY_NOTE not in full


@pytest.mark.parametrize("extra, named", [
    (("--resume-from", "5"), "--resume-from"),
    (("--autodoc",), "--autodoc"),
    (("--wake-watch",), "--wake-watch"),
])
def test_deploy_only_refuses_flags_that_ask_for_another_step(
        suite, claude_root, tmp_path, capsys, extra, named):
    config_home, data_root = _two_project_config(tmp_path)
    before = _tree(data_root)

    with pytest.raises(SystemExit) as exc:
        provision.main(_deploy_only_argv(suite, claude_root, config_home, tmp_path, *extra))

    assert exc.value.code == 2
    err = capsys.readouterr().err
    # The error line itself, not the usage block, which names every flag anyway.
    error_line = err.strip().splitlines()[-1]
    assert "--deploy-only" in error_line and f"cannot be combined with {named}" in error_line
    assert not claude_root.exists()
    assert _tree(data_root) == before


@pytest.mark.parametrize("extra", [("--no-autodoc",), ("--no-auto-allow",), ("--no-wake-watch",),
                                   ("--force",)])
def test_deploy_only_accepts_flags_that_agree_with_it(
        suite, claude_root, tmp_path, capsys, extra):
    """Skip flags say what deploy-only already does, and --force tunes step 7 itself."""
    config_home, _ = _two_project_config(tmp_path)

    rc = provision.main(_deploy_only_argv(suite, claude_root, config_home, tmp_path,
                                          "--dry-run", *extra))

    assert rc == 0
    assert provision.DEPLOY_ONLY_NOTE in capsys.readouterr().out


# What `git worktree add` writes as a linked worktree's `.git`: a FILE, not a directory.
LINKED_WORKTREE_DOT_GIT = "gitdir: /elsewhere/.git/worktrees/epic-branch\n"


@pytest.mark.parametrize("dry_run", [False, True])
def test_deploy_only_refuses_a_linked_worktree_suite_root(
        suite, claude_root, tmp_path, capsys, dry_run):
    """Live entries point into suite_root; a per-epic worktree is deleted after merge."""
    (suite / ".git").write_text(LINKED_WORKTREE_DOT_GIT, encoding="utf-8")
    config_home, data_root = _two_project_config(tmp_path)
    before = _tree(data_root)
    extra = ("--dry-run",) if dry_run else ()

    with pytest.raises(SystemExit) as exc:
        provision.main(_deploy_only_argv(suite, claude_root, config_home, tmp_path, *extra))

    assert exc.value.code == 2
    error_line = capsys.readouterr().err.strip().splitlines()[-1]
    assert "linked git worktree" in error_line and "primary checkout" in error_line
    assert not claude_root.exists()
    assert _tree(data_root) == before


def test_deploy_only_accepts_a_primary_checkout_suite_root(suite, claude_root, tmp_path, capsys):
    (suite / ".git").mkdir()
    config_home, _ = _two_project_config(tmp_path)

    rc = provision.main(_deploy_only_argv(suite, claude_root, config_home, tmp_path))

    assert rc == 0
    assert (claude_root / "skills" / "alpha" / "SKILL.md").is_file()


def test_the_worktree_guard_is_scoped_to_deploy_only(suite, claude_root, tmp_path, capsys):
    """A full install from a worktree is unchanged: its dry run still plans every step."""
    (suite / ".git").write_text(LINKED_WORKTREE_DOT_GIT, encoding="utf-8")
    config_home, _ = _two_project_config(tmp_path)
    argv = _deploy_only_argv(suite, claude_root, config_home, tmp_path, "--dry-run")
    argv.remove("--deploy-only")

    assert provision.main(argv) == 0
    assert "11. Scaffold data_root" in capsys.readouterr().out
