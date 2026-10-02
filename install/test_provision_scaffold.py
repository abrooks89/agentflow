#!/usr/bin/env python3
"""Tests for the AF-B4 data_root scaffold step in install/provision.py.

The registry consolidation (docs/REGISTRY.md + docs/DATA-LAYOUT.md):

- The per-project subtree is FOUR subdirs — `refine solution status use-cases`.
  `queue/` is retired: per-agent loop state lives at `status/<agent>-state.md`
  and shared coordination lives only in the registry.
- The scaffold seeds `status/registry.md` from `templates/registry.md`, with the
  `<project>` placeholder replaced by the project name.
- The seed is write-if-absent: a live registry is never clobbered on a re-run.
- The wiki pillar's two READ targets (`wiki/hot.md`, `wiki/digest.md`) are seeded too, with the
  empty bodies `agentflow.wiki` itself defines (E-0710). The directories alone left every reader
  unable to tell "nothing learned yet" from "the pillar is broken", and a body retyped here
  instead of imported is the drift that failure mode comes back through.

Run: `python -m pytest install/test_provision_scaffold.py -v`
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402

REGISTRY_TEMPLATE = """# Fleet Registry — <project>

<!-- lock: <lock_dir>/<project>-registry.lock -->

## Agents

| agent | status |
|---|---|
| lead-dev | idle |
"""


def _make_opts(suite_root: Path, config_home: Path) -> provision.Options:
    return provision.Options(
        dry_run=False,
        suite_root=suite_root,
        config_home=config_home,
        settings_file=suite_root / "settings.json",
        autodoc=False,
        autodoc_reason="forced by --no-autodoc",
    )


def _make_suite(tmp_path: Path, projects: list[str]) -> tuple[provision.Options, Path]:
    """Fake suite + config home with the given project profiles; returns (opts, data_root)."""
    suite_root = tmp_path / "suite"
    (suite_root / "templates").mkdir(parents=True)
    (suite_root / "templates" / "registry.md").write_text(
        REGISTRY_TEMPLATE, encoding="utf-8", newline="\n"
    )
    config_home = tmp_path / "config"
    (config_home / "projects").mkdir(parents=True)
    for name in projects:
        (config_home / "projects" / f"{name}.md").write_text(
            f"# {name}\n", encoding="utf-8"
        )
    data_root = tmp_path / "data"
    return _make_opts(suite_root, config_home), data_root


def _run_scaffold(opts: provision.Options, data_root: Path) -> None:
    fake_env = SimpleNamespace(data_root=str(data_root))
    with mock.patch("agentflow.config.load_environment", return_value=fake_env):
        provision._do_scaffold_data_root(opts)


# --------------------------------------------------------------------------- #
# Subtree: four subdirs, queue/ retired
# --------------------------------------------------------------------------- #
def test_subtree_constant_has_no_queue():
    assert provision.DATA_SUBTREE_DIRS == (
        "design/specs",
        "design/plans",
        "design/use-cases",
        "design/suggestions",
        "status",
        "wiki/pages",
        "wiki/pages/done",
        "wiki/pages/archived",
    )


def test_scaffold_creates_four_subdirs_and_never_queue(tmp_path: Path):
    opts, data_root = _make_suite(tmp_path, ["alpha"])
    _run_scaffold(opts, data_root)
    project_dir = data_root / "projects" / "alpha"
    # Kept in step with DATA_SUBTREE_DIRS deliberately: this is the BEHAVIOURAL half (the
    # scaffolder really made the directories) and the tuple assertion above is the CONSTANT
    # half. Both must move together or one silently stops covering the new buckets.
    for sub in ("design/specs", "design/plans", "design/use-cases",
                "design/suggestions", "status", "wiki/pages",
                "wiki/pages/done", "wiki/pages/archived"):
        assert (project_dir / sub).is_dir(), sub
    assert not (project_dir / "queue").exists()


# --------------------------------------------------------------------------- #
# Registry seed: from template, placeholder replaced, write-if-absent
# --------------------------------------------------------------------------- #
def test_scaffold_seeds_registry_with_project_name(tmp_path: Path):
    opts, data_root = _make_suite(tmp_path, ["alpha", "beta"])
    _run_scaffold(opts, data_root)
    for name in ("alpha", "beta"):
        reg = data_root / "projects" / name / "status" / "registry.md"
        assert reg.is_file(), name
        text = reg.read_text(encoding="utf-8")
        assert f"# Fleet Registry — {name}" in text
        assert f"{name}-registry.lock" in text
        assert "<project>" not in text  # every placeholder replaced


def test_scaffold_registry_seed_is_write_if_absent(tmp_path: Path):
    opts, data_root = _make_suite(tmp_path, ["alpha"])
    reg = data_root / "projects" / "alpha" / "status" / "registry.md"
    reg.parent.mkdir(parents=True)
    reg.write_text("# live registry with real rows\n", encoding="utf-8")
    _run_scaffold(opts, data_root)
    assert reg.read_text(encoding="utf-8") == "# live registry with real rows\n"


def test_scaffold_missing_registry_template_fails_loudly(tmp_path: Path):
    opts, data_root = _make_suite(tmp_path, ["alpha"])
    (opts.suite_root / "templates" / "registry.md").unlink()
    try:
        _run_scaffold(opts, data_root)
    except RuntimeError as e:
        assert "registry" in str(e).lower()
    else:
        raise AssertionError("expected a loud RuntimeError on a missing template")


# --------------------------------------------------------------------------- #
# Wiki pillar seed: the two READ targets, with the pillar's OWN empty bodies
# --------------------------------------------------------------------------- #
def test_scaffold_seeds_the_wiki_read_targets(tmp_path: Path):
    """`wiki/hot.md` and `wiki/digest.md` exist the moment the project does (E-0710).

    The directories alone shipped before this; the files did not. Both are READ before anything
    writes them - a lane opens `hot.md` at wake, dev-manager opens `digest.md` at session start -
    and an absent file is the one signal that cannot distinguish "nothing learned yet" from
    "the pillar is broken".
    """
    opts, data_root = _make_suite(tmp_path, ["alpha", "beta"])
    _run_scaffold(opts, data_root)
    for name in ("alpha", "beta"):
        wiki = data_root / "projects" / name / "wiki"
        assert (wiki / "hot.md").is_file(), name
        assert (wiki / "digest.md").is_file(), name


def test_scaffolded_bodies_come_from_the_wiki_module_not_from_here(tmp_path: Path):
    """Byte equality against `agentflow.wiki.initial_pillar_files`, per project.

    This is the assertion that keeps provisioning from growing a SECOND definition of "a valid
    empty pillar". Comparing against the function - not against a literal typed into this test -
    means a hand-written body in provision.py reddens here immediately instead of drifting
    silently until someone notices a scaffolded hot file contradicting every refreshed one.
    """
    from agentflow.wiki import initial_pillar_files

    opts, data_root = _make_suite(tmp_path, ["alpha"])
    _run_scaffold(opts, data_root)
    wiki = data_root / "projects" / "alpha" / "wiki"
    for filename, body in initial_pillar_files("alpha").items():
        assert (wiki / filename).read_text(encoding="utf-8") == body, filename


def test_scaffolded_digest_names_its_own_project(tmp_path: Path):
    """Two projects, two digests, each headed with its own name - not a shared template."""
    opts, data_root = _make_suite(tmp_path, ["alpha", "beta"])
    _run_scaffold(opts, data_root)
    for name in ("alpha", "beta"):
        digest = (data_root / "projects" / name / "wiki" / "digest.md").read_text(encoding="utf-8")
        assert digest.startswith(f"# {name} - fleet digest")


def test_wiki_seed_is_write_if_absent(tmp_path: Path):
    """A live hot file and a real steward digest survive a re-provision untouched.

    `digest.md` is the steward's only-writer file and `hot.md` is regenerated from `pages/`;
    a scaffold step that overwrote either on a re-run would destroy work no one asked it to touch.
    """
    opts, data_root = _make_suite(tmp_path, ["alpha"])
    wiki = data_root / "projects" / "alpha" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "hot.md").write_text("# Hot\n\nreal projection\n", encoding="utf-8", newline="\n")
    (wiki / "digest.md").write_text("# alpha\n\nthe steward wrote this\n",
                                    encoding="utf-8", newline="\n")

    _run_scaffold(opts, data_root)

    assert (wiki / "hot.md").read_text(encoding="utf-8") == "# Hot\n\nreal projection\n"
    assert (wiki / "digest.md").read_text(encoding="utf-8") == "# alpha\n\nthe steward wrote this\n"


def test_wiki_seed_is_idempotent_across_runs(tmp_path: Path):
    """Two scaffolds, identical bytes - the re-run neither rewrites nor appends."""
    opts, data_root = _make_suite(tmp_path, ["alpha"])
    _run_scaffold(opts, data_root)
    wiki = data_root / "projects" / "alpha" / "wiki"
    first = {p.name: p.read_bytes() for p in sorted(wiki.glob("*.md"))}

    _run_scaffold(opts, data_root)

    assert {p.name: p.read_bytes() for p in sorted(wiki.glob("*.md"))} == first


# --------------------------------------------------------------------------- #
# Residue baseline seed (gh#651): an empty, dated baseline so `wiki residue` measures from wake 1
# --------------------------------------------------------------------------- #
def test_scaffold_seeds_an_empty_residue_baseline_that_measures(tmp_path: Path):
    """Without a baseline, `python -m agentflow.wiki residue` exits 2 CANNOT MEASURE on every
    steward wake until a human records one. The seeded baseline is valid per `load_baseline`,
    empty, dated today, and the check run against the fresh pillar returns 0, not 2."""
    import argparse
    import datetime

    from agentflow import pillar_residue as pr

    opts, data_root = _make_suite(tmp_path, ["alpha"])
    _run_scaffold(opts, data_root)
    wiki = data_root / "projects" / "alpha" / "wiki"
    path = pr.baseline_path(wiki)
    assert path.name == "residue-baseline.json" and path.parent == wiki

    baseline = pr.load_baseline(path)
    assert baseline.pages == {}
    assert baseline.pages_scanned == 0
    assert baseline.recorded == datetime.date.today().isoformat()

    ns = argparse.Namespace(project=None, wiki_root=None, person_name="Tester", ci_yml=None,
                            update_baseline=None, accept_worsening=False)
    assert pr.run(ns, wiki) == 0


def test_residue_baseline_seed_never_overwrites_an_existing_one(tmp_path: Path):
    opts, data_root = _make_suite(tmp_path, ["alpha"])
    wiki = data_root / "projects" / "alpha" / "wiki"
    wiki.mkdir(parents=True)
    existing = '{"schema_version": 1, "recorded": "2026-01-01", "pages": {"old": {"person": 2}}}\n'
    (wiki / "residue-baseline.json").write_text(existing, encoding="utf-8", newline="\n")

    _run_scaffold(opts, data_root)
    _run_scaffold(opts, data_root)

    assert (wiki / "residue-baseline.json").read_text(encoding="utf-8") == existing


def test_no_baseline_is_seeded_into_a_pillar_that_already_has_pages(tmp_path: Path):
    """An empty baseline on a pillar with pages would flag every existing page as NEW PAGE WITH
    RESIDUE (exit 1) and make the human's first --update-baseline a refused "worsening". Such a
    pillar keeps no baseline, so `wiki residue` still exits 2 and asks a human to record one."""
    import argparse

    from agentflow import pillar_residue as pr

    wiki = tmp_path / "wiki"
    (wiki / "pages").mkdir(parents=True)
    (wiki / "pages" / "old.md").write_text("mentions Tester by name\n", encoding="utf-8")

    provision._seed_wiki_pillar(tmp_path, "alpha")

    assert not pr.baseline_path(wiki).exists()
    ns = argparse.Namespace(project=None, wiki_root=None, person_name="Tester", ci_yml=None,
                            update_baseline=None, accept_worsening=False)
    assert pr.run_cli(ns, wiki) == 2


def test_a_page_only_in_a_terminal_partition_still_gets_the_seed(tmp_path: Path):
    """Same page set as the check: done/ and archived/ are not live pages."""
    from agentflow import pillar_residue as pr

    wiki = tmp_path / "wiki"
    (wiki / "pages" / "done").mkdir(parents=True)
    (wiki / "pages" / "done" / "finished.md").write_text("x\n", encoding="utf-8")

    provision._seed_wiki_pillar(tmp_path, "alpha")

    assert pr.load_baseline(pr.baseline_path(wiki)).pages == {}
