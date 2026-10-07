#!/usr/bin/env python3
"""Tests for the verification-readiness verdict in install/provision.py.

The installer must not print an unqualified success banner over a done-gate that
cannot run as designed. This is a REPORT, never a failure path: a library with no
user-facing surface is correctly configured with no `staging.url`, and provision
still exits 0.

The gaps it names:
- `test-suite` / `smoke-cli` -> the browser done-gate never runs
- `none`                     -> nothing verifies work at all
- missing `staging.url`      -> a journey must start its own instance per firing
- url without deploy_recipe  -> no described path into that environment

Run: `python -m pytest install/test_provision_readiness.py -v`
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402


def _project(name, strategy, url=None, recipe=None):
    """Minimal stand-in for agentflow.config.Project (only fields the report reads)."""
    staging = None
    if url is not None or recipe is not None:
        staging = SimpleNamespace(url=url, deploy_recipe=recipe)
    return SimpleNamespace(
        name=name,
        verification=SimpleNamespace(strategy=strategy),
        staging=staging,
    )


def _run(tmp_path, projects):
    """Drive _verification_readiness over fake project profiles."""
    config_home = tmp_path / "config"
    (config_home / "projects").mkdir(parents=True)
    for project in projects:
        (config_home / "projects" / f"{project.name}.md").write_text("x", encoding="utf-8")

    opts = provision.Options(
        dry_run=False,
        suite_root=tmp_path / "suite",
        config_home=config_home,
        settings_file=tmp_path / "settings.json",
        autodoc=False,
        autodoc_reason="forced by --no-autodoc",
    )
    by_name = {p.name: p for p in projects}
    fake = SimpleNamespace(load_project=lambda stem, **_k: by_name[stem])
    with mock.patch.dict(sys.modules, {"agentflow.config": fake}):
        return provision._verification_readiness(opts)


def test_fully_wired_web_journey_reports_no_gaps(tmp_path):
    report = _run(tmp_path, [
        _project("shop", "web-journey", url="https://staging.example.com", recipe="merge deploys"),
    ])
    assert report == [("shop", "web-journey", [])]


def test_test_suite_strategy_is_named_as_a_gap(tmp_path):
    report = _run(tmp_path, [_project("game", "test-suite")])
    _, _, gaps = report[0]
    # the browser-gate gap AND the missing-url gap
    assert len(gaps) == 2
    assert "browser done-gate never runs" in gaps[0]
    assert "whether or not anything calls them" in gaps[0]


def test_strategy_none_is_named_outright(tmp_path):
    report = _run(tmp_path, [_project("thing", "none", url="http://x", recipe="r")])
    _, _, gaps = report[0]
    assert gaps == ["strategy is `none`: nothing verifies work before it is marked done."]


def test_url_without_deploy_recipe_is_a_gap(tmp_path):
    report = _run(tmp_path, [_project("app", "web-journey", url="http://x")])
    _, _, gaps = report[0]
    assert len(gaps) == 1
    assert "deploy_recipe is empty" in gaps[0]


def test_missing_url_is_a_gap_even_for_a_skill_path_strategy(tmp_path):
    report = _run(tmp_path, [_project("app", "~/skills/verify/SKILL.md")])
    _, _, gaps = report[0]
    assert len(gaps) == 1
    assert "no staging.url" in gaps[0]


def test_no_projects_dir_is_empty_not_an_error(tmp_path):
    opts = provision.Options(
        dry_run=False,
        suite_root=tmp_path / "suite",
        config_home=tmp_path / "empty-config",
        settings_file=tmp_path / "settings.json",
        autodoc=False,
        autodoc_reason="forced by --no-autodoc",
    )
    assert provision._verification_readiness(opts) == []


def test_clean_report_prints_the_plain_banner(capsys):
    provision._print_verification_readiness([("shop", "web-journey", [])])
    out = capsys.readouterr().out
    assert out.strip() == "agentflow provision complete."


def test_gapped_report_qualifies_the_banner_and_lists_every_gap(capsys):
    provision._print_verification_readiness([
        ("game", "test-suite", ["gap one", "gap two"]),
        ("lib", "none", ["gap three"]),
    ])
    out = capsys.readouterr().out
    assert "complete — with 3 verification gap(s)." in out
    for gap in ("gap one", "gap two", "gap three"):
        assert gap in out
    # a project with no gaps is not printed at all
    assert "[game]" in out and "[lib]" in out
    # the remediation names the both-modes escape hatch
    assert "SKILL.md path in verification.strategy" in out


def test_projects_without_gaps_are_omitted_from_the_listing(capsys):
    provision._print_verification_readiness([
        ("clean", "web-journey", []),
        ("dirty", "none", ["the one gap"]),
    ])
    out = capsys.readouterr().out
    assert "[dirty]" in out
    assert "[clean]" not in out
