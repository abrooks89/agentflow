#!/usr/bin/env python3
"""Tests for the install-interview project probe.

The probe exists so "I don't know" is a usable answer to the verification
question: it reads the checkout and proposes a strategy WITH its reasons, so the
interviewer never silently picks one.

Its contract:
- evidence-based — an unreadable or unknown tree yields `none` and says why
- a UI project is never recommended a suite-only gate
- a project with BOTH a suite and browser tooling is pointed at a SKILL.md path,
  because the enum values are single-mode
- every recommendation carries at least one reason

Run: `python -m pytest skills/_shared/tests/test_project_probe.py -v`
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow.project_probe import Evidence, format_report, main, probe, recommend


def _write(root, rel, content=""):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    return path


def _pkg(scripts=None, deps=None):
    return json.dumps({"scripts": scripts or {}, "dependencies": deps or {}})


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def test_missing_checkout_is_reported_not_raised(tmp_path):
    ev = probe(tmp_path / "nope")
    assert ev.exists is False
    rec = recommend(ev)
    assert rec.strategy == "none"
    assert "no checkout found" in rec.reasons[0]


def test_detects_node_ui_browser_tooling_and_commands(tmp_path):
    _write(tmp_path, "package.json", _pkg(
        scripts={"test": "vitest run", "e2e": "playwright test", "dev": "next dev"},
        deps={"react": "^19", "@playwright/test": "^1"},
    ))
    _write(tmp_path, "next.config.ts")
    _write(tmp_path, "e2e/full-flow.spec.ts")

    ev = probe(tmp_path)
    assert "javascript/typescript" in ev.languages
    assert "next.js" in ev.ui_frameworks and "react" in ev.ui_frameworks
    assert "playwright" in ev.browser_tools
    assert "e2e" in ev.e2e_dirs
    assert "npm run test" in ev.test_commands and "npm run e2e" in ev.test_commands
    assert "npm run dev" in ev.dev_commands
    assert ev.has_ui and ev.has_suite and ev.has_browser_tooling


def test_malformed_package_json_degrades_instead_of_crashing(tmp_path):
    _write(tmp_path, "package.json", "{ this is not json")
    ev = probe(tmp_path)
    assert ev.exists is True
    assert "javascript/typescript" in ev.languages  # the manifest still counts
    assert ev.test_commands == []                   # but yielded no commands


def test_python_library_detection(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path, "tests/test_x.py")
    ev = probe(tmp_path)
    assert ev.languages == ["python"]
    assert ev.test_commands == ["pytest"]
    assert not ev.has_ui


def test_containerization_is_noticed(tmp_path):
    _write(tmp_path, "Dockerfile", "FROM scratch\n")
    assert probe(tmp_path).containerized is True


# --------------------------------------------------------------------------- #
# recommend
# --------------------------------------------------------------------------- #
def test_ui_plus_suite_plus_browser_is_pointed_at_a_skill_path(tmp_path):
    _write(tmp_path, "package.json", _pkg(scripts={"test": "vitest run"}, deps={"react": "^19"}))
    _write(tmp_path, "playwright.config.ts")
    rec = recommend(probe(tmp_path))
    assert rec.strategy == "<SKILL.md path>"
    assert any("single-mode" in f for f in rec.follow_ups)
    assert any("staging.url" in f for f in rec.follow_ups)


def test_ui_without_browser_tooling_still_never_recommends_a_suite_gate(tmp_path):
    _write(tmp_path, "package.json", _pkg(scripts={"test": "vitest run"}, deps={"vue": "^3"}))
    rec = recommend(probe(tmp_path))
    assert rec.strategy == "web-journey"
    assert any("no browser tooling found" in f for f in rec.follow_ups)
    # the suite is demoted, not discarded
    assert any("regression floor" in f for f in rec.follow_ups)


def test_library_with_tests_gets_test_suite_plus_a_caveat(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path, "tests/test_x.py")
    rec = recommend(probe(tmp_path))
    assert rec.strategy == "test-suite"
    assert any("UI the probe missed" in f for f in rec.follow_ups)


def test_code_with_no_tooling_recommends_none_and_says_why(tmp_path):
    _write(tmp_path, "go.mod", "module x\n")
    ev = probe(tmp_path)
    ev.test_commands = []  # a go.mod with no suite convention applied
    rec = recommend(ev)
    assert rec.strategy == "none"
    assert any("no test tooling" in r for r in rec.reasons)
    assert any("smoke-cli" in f for f in rec.follow_ups)


def test_empty_directory_yields_no_evidence_and_admits_it(tmp_path):
    rec = recommend(probe(tmp_path))
    assert rec.strategy == "none"
    assert "no evidence" in rec.reasons[0] or "probe has no evidence" in rec.reasons[0]


def test_every_recommendation_carries_at_least_one_reason(tmp_path):
    cases = [
        Evidence(checkout="x", exists=True, ui_frameworks=["react"]),
        Evidence(checkout="x", exists=True, languages=["python"], test_commands=["pytest"]),
        Evidence(checkout="x", exists=True, languages=["go"]),
        Evidence(checkout="x", exists=False),
    ]
    for ev in cases:
        assert recommend(ev).reasons, f"no reason given for {ev}"


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def test_report_states_it_is_a_proposal_not_a_decision(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    ev = probe(tmp_path)
    text = format_report(ev, recommend(ev))
    assert "RECOMMENDED verification.strategy: test-suite" in text
    assert "because:" in text
    assert "The user decides." in text


# --- the SCRIPT form the install interview runs before any venv exists -------------------

_PROBE_FILE = Path(__file__).resolve().parents[1] / "agentflow" / "project_probe.py"


def _bare_python(*args: str) -> subprocess.CompletedProcess:
    """Run this interpreter with no site-packages and no script-dir path games (-I -S)."""
    return subprocess.run(
        [sys.executable, "-I", "-S", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def test_the_bare_interpreter_really_cannot_import_the_package():
    """Control: without it the script-form test below could pass on an installed package."""
    r = _bare_python("-c", "import agentflow")
    assert r.returncode != 0 and "ModuleNotFoundError" in r.stderr, r.stderr


def test_the_FILE_runs_as_a_script_with_no_agentflow_package_importable(tmp_path):
    """install/SKILL.md runs `python skills/_shared/agentflow/project_probe.py <checkout>`
    in Step 1, before any venv exists, so the file must not need the `agentflow` package."""
    _write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n')
    _write(tmp_path, "tests/test_x.py", "def test_x():\n    pass\n")

    r = _bare_python(str(_PROBE_FILE), str(tmp_path))

    assert r.returncode == 0, r.stderr
    assert "ModuleNotFoundError" not in r.stderr
    assert "RECOMMENDED verification.strategy:" in r.stdout


# --- below the root: a monorepo's frontend is not "no user-facing surface" (gh#879) ------

def test_monorepo_frontend_subdir_is_seen_and_named(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path, "tests/test_x.py")
    _write(tmp_path, "frontend/package.json", _pkg(
        scripts={"test": "vitest run"},
        deps={"react": "^19", "@playwright/test": "^1", "vite": "^5"},
    ))
    _write(tmp_path, "frontend/vite.config.ts")

    ev = probe(tmp_path)
    assert "react in frontend/package.json" in ev.ui_frameworks
    assert "vite in frontend/vite.config.ts" in ev.ui_frameworks
    assert "playwright in frontend/package.json" in ev.browser_tools
    # suite + browser tooling -> the two-mode SKILL.md answer, never a suite-only gate
    assert recommend(ev).strategy == "<SKILL.md path>"


def test_monorepo_frontend_without_browser_tooling_is_web_journey(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path, "tests/test_x.py")
    _write(tmp_path, "frontend/package.json", _pkg(deps={"react": "^19"}))
    _write(tmp_path, "frontend/vite.config.ts")

    rec = recommend(probe(tmp_path))
    assert rec.strategy == "web-journey"
    assert any("react in frontend/package.json" in r for r in rec.reasons)


def test_depth_two_is_seen_and_depth_three_is_not(tmp_path):
    _write(tmp_path, "apps/web/next.config.ts")
    _write(tmp_path, "a/b/c/angular.json")
    ev = probe(tmp_path)
    assert ev.ui_frameworks == ["next.js in apps/web/next.config.ts"]


@pytest.mark.parametrize("skipped", [
    "node_modules/x", ".venv/x", "venv/x", "env/x", "_vendored/x", "vendor/x",
    "third_party/x", "fixtures/x", "testdata/x", "build/x", "dist/x", "target/x",
    "harness/fixtures", ".git/x",
])
def test_dependency_vendored_build_and_fixture_trees_are_not_evidence(tmp_path, skipped):
    _write(tmp_path, f"{skipped}/package.json", _pkg(deps={"react": "^19"}))
    _write(tmp_path, f"{skipped}/vite.config.ts")
    ev = probe(tmp_path)
    assert ev.ui_frameworks == [] and ev.languages == []
    assert recommend(ev).strategy == "none"


@pytest.mark.parametrize("tree", ["docs", "doc", "examples", "example", "samples", "sample"])
def test_a_docs_or_example_site_is_not_a_product_ui(tmp_path, tree):
    """A CLI library with a Docusaurus/VitePress docs site is still a CLI library."""
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n[tool.pytest.ini_options]\n")
    _write(tmp_path, f"{tree}/package.json", _pkg(
        scripts={"dev": "docusaurus start"}, deps={"react": "^19"},
    ))
    _write(tmp_path, f"{tree}/vite.config.ts")

    ev = probe(tmp_path)
    assert ev.ui_frameworks == [] and ev.dev_commands == []
    rec = recommend(ev)
    assert rec.strategy == "test-suite"
    assert not any(f"{tree}/" in r for r in rec.reasons)


def test_a_real_frontend_beside_a_docs_site_still_counts(tmp_path):
    """Control: skipping docs/ must not blind the probe to a genuine nested UI."""
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path, "docs/package.json", _pkg(scripts={"dev": "x"}, deps={"react": "^19"}))
    _write(tmp_path, "frontend/package.json", _pkg(deps={"vue": "^3"}))

    ev = probe(tmp_path)
    assert ev.ui_frameworks == ["vue in frontend/package.json"]
    assert recommend(ev).strategy == "web-journey"


def test_a_symlinked_subdir_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    _write(outside, "web/vite.config.ts")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    try:
        os.symlink(outside / "web", checkout / "web", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a directory symlink here: {exc}")
    assert probe(checkout).ui_frameworks == []


def test_a_dockerfile_below_the_root_names_its_directory(tmp_path):
    _write(tmp_path, "svc/Dockerfile", "FROM scratch\n")
    ev = probe(tmp_path)
    assert ev.containerized is True
    assert ev.container_files == ["svc/Dockerfile"]
    assert "containerized   : yes (svc/Dockerfile)" in format_report(ev, recommend(ev))


# --- an unreadable path is reported, never silently dropped ------------------------------

def test_an_unreadable_subdir_is_reported_and_flagged(tmp_path, monkeypatch):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path, "frontend/package.json", _pkg(deps={"react": "^19"}))
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self.name == "frontend":
            raise PermissionError(13, "denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    ev = probe(tmp_path)
    # the manifest is still read (no listing needed); only the walk below it is lost
    assert ev.unreadable == ["frontend/ (PermissionError)"]
    rec = recommend(ev)
    assert any("missing evidence" in f and "frontend/" in f for f in rec.follow_ups)
    assert "unreadable      : frontend/ (PermissionError)" in format_report(ev, rec)


def test_an_unreadable_or_malformed_package_json_is_reported(tmp_path, monkeypatch):
    _write(tmp_path, "frontend/package.json", _pkg(deps={"react": "^19"}))
    _write(tmp_path, "admin/package.json", "{ this is not json")
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.parent.name == "frontend" and self.name == "package.json":
            raise PermissionError(13, "denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    ev = probe(tmp_path)
    assert sorted(ev.unreadable) == [
        "admin/package.json (JSONDecodeError)",
        "frontend/package.json (PermissionError)",
    ]
    assert ev.ui_frameworks == []
    assert any("missing evidence" in f for f in recommend(ev).follow_ups)


def test_an_unreadable_workflows_dir_is_reported_not_raised(tmp_path, monkeypatch):
    _write(tmp_path, ".github/workflows/ci.yml", "jobs:\n  t:\n    steps:\n      - run: pytest\n")
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self.name == "workflows":
            raise PermissionError(13, "denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    ev = probe(tmp_path)
    assert ev.unreadable == [".github/workflows/ (PermissionError)"]
    assert ev.test_commands == []


def test_an_unreadable_workflow_file_is_reported(tmp_path, monkeypatch):
    _write(tmp_path, ".github/workflows/ci.yml", "jobs:\n  t:\n    steps:\n      - run: pytest\n")
    _write(tmp_path, ".github/workflows/ok.yml", "jobs:\n  t:\n    steps:\n      - run: go test ./...\n")
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name == "ci.yml":
            raise PermissionError(13, "denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    ev = probe(tmp_path)
    assert ev.unreadable == [".github/workflows/ci.yml (PermissionError)"]
    assert ev.test_commands == ["go test in .github/workflows/ok.yml"]


def test_ci_workflow_counts_only_when_it_runs_a_known_runner(tmp_path):
    _write(tmp_path, ".github/workflows/ci.yml", (
        "jobs:\n"
        "  t:\n"
        "    steps:\n"
        "      - run: pip install pytest\n"
        "      - name: suite\n"
        "        run: |\n"
        "          cd lib && python -m pytest -q\n"
    ))
    _write(tmp_path, ".github/workflows/lint.yaml", "jobs:\n  l:\n    steps:\n      - run: echo hi\n")
    ev = probe(tmp_path)
    assert ev.test_commands == ["python -m pytest in .github/workflows/ci.yml"]
    rec = recommend(ev)
    assert rec.strategy == "test-suite"
    assert any(".github/workflows/ci.yml" in r for r in rec.reasons)


def test_ci_workflow_without_a_runner_is_not_a_suite(tmp_path):
    _write(tmp_path, ".github/workflows/ci.yml", "jobs:\n  e:\n    steps:\n      - run: echo hi\n")
    ev = probe(tmp_path)
    assert ev.test_commands == []
    assert recommend(ev).strategy == "none"


# --- CLI: a bad argument is a usage error, not a "no checkout found" answer ---------------

def test_main_exits_2_on_a_missing_path(tmp_path, capsys):
    assert main([str(tmp_path / "typo")]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "usage:" in out.err


def test_main_exits_2_on_help(capsys):
    assert main(["--help"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "usage:" in out.err


def test_main_exits_2_on_a_file_not_a_directory(tmp_path, capsys):
    target = _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    assert main([str(target)]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "not a directory:" in out.err and "usage:" in out.err


def test_main_exits_0_on_a_valid_path(tmp_path, capsys):
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\n")
    assert main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "RECOMMENDED verification.strategy: test-suite" in out
    # root evidence keeps its bare label; only findings below the root are prefixed
    assert "languages       : python\n" in out


# --- a skipped docs/examples tree's UI is reported as seen, not counted ------------------

def _deny_stat(monkeypatch, refused):
    """Make stat() and lstat() raise PermissionError for every path `refused` accepts, which
    is what an unenterable directory does to its children. The probe's is_* helpers stat
    directly, so this drives the same code on Python 3.11 and on 3.14, where Path.is_*
    would swallow the error and a patch on them would test nothing the probe calls."""
    real_stat, real_lstat = Path.stat, Path.lstat

    def stat(self, *a, **k):
        if refused(self):
            raise PermissionError(13, "denied", str(self))
        return real_stat(self, *a, **k)

    def lstat(self, *a, **k):
        if refused(self):
            raise PermissionError(13, "denied", str(self))
        return real_lstat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "lstat", lstat)


def _python_cli(root):
    _write(root, "pyproject.toml", "[project]\nname='x'\n")
    _write(root, "tests/test_x.py")


def test_docs_ui_is_reported_not_counted_and_the_verdict_is_unchanged(tmp_path):
    bare, with_docs = tmp_path / "bare", tmp_path / "with_docs"
    _python_cli(bare)
    _python_cli(with_docs)
    _write(with_docs, "docs/package.json", _pkg(deps={"react": "^19"}))

    ev_bare, ev = probe(bare), probe(with_docs)
    rec_bare, rec = recommend(ev_bare), recommend(ev)
    assert ev.ui_frameworks == []
    assert ev.not_counted == ["react in docs/package.json"]
    assert rec.strategy == rec_bare.strategy == "test-suite"
    assert rec.reasons == rec_bare.reasons
    assert "  not counted     : react in docs/package.json" in format_report(ev, rec)
    assert "not counted" not in format_report(ev_bare, rec_bare)
    assert any("not counted (react in docs/package.json)" in f for f in rec.follow_ups)


def test_a_nested_examples_vite_config_is_reported_not_counted(tmp_path):
    _python_cli(tmp_path)
    _write(tmp_path, "examples/app/vite.config.ts")
    ev = probe(tmp_path)
    assert ev.ui_frameworks == []
    assert ev.not_counted == ["vite in examples/app/vite.config.ts"]
    rec = recommend(ev)
    assert rec.strategy == "test-suite"
    assert "  not counted     : vite in examples/app/vite.config.ts" in format_report(ev, rec)


def test_a_not_counted_tree_keeps_the_depth_budget(tmp_path):
    _write(tmp_path, "pkg/docs/package.json", _pkg(deps={"react": "^19"}))  # depth 2: seen
    _write(tmp_path, "pkg/docs/site/vite.config.ts")                       # depth 3: not
    assert probe(tmp_path).not_counted == ["react in pkg/docs/package.json"]


@pytest.mark.parametrize("tree", ["node_modules", "vendor", "dist", "fixtures"])
def test_dependency_and_build_trees_are_never_reported_as_not_counted(tmp_path, tree):
    _python_cli(tmp_path)
    _write(tmp_path, f"{tree}/package.json", _pkg(deps={"react": "^19"}))
    _write(tmp_path, f"{tree}/vite.config.ts")
    _write(tmp_path, f"docs/{tree}/package.json", _pkg(deps={"react": "^19"}))
    ev = probe(tmp_path)
    assert ev.not_counted == [] and ev.ui_frameworks == []
    assert "not counted" not in format_report(ev, recommend(ev))


def test_root_ui_plus_docs_ui_is_web_journey_with_docs_still_not_counted(tmp_path):
    _write(tmp_path, "package.json", _pkg(deps={"vue": "^3"}))
    _write(tmp_path, "docs/package.json", _pkg(deps={"react": "^19"}))
    ev = probe(tmp_path)
    assert ev.ui_frameworks == ["vue"]
    assert ev.not_counted == ["react in docs/package.json"]
    rec = recommend(ev)
    assert rec.strategy == "web-journey"
    # the verdict already has a UI, so the "say so" note would be noise
    assert not any("not counted" in f for f in rec.follow_ups)
    assert "  not counted     : react in docs/package.json" in format_report(ev, rec)


def test_an_unreadable_dir_inside_a_docs_tree_is_not_reported_unreadable(tmp_path, monkeypatch):
    _python_cli(tmp_path)
    _write(tmp_path, "docs/package.json", _pkg(deps={"react": "^19"}))
    _write(tmp_path, "docs/site/vite.config.ts")
    real_iterdir = Path.iterdir

    def iterdir(self):
        if self.name == "docs":
            raise PermissionError(13, "denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    ev = probe(tmp_path)
    assert ev.unreadable == []
    assert ev.not_counted == ["react in docs/package.json"]


def test_an_unenterable_dir_inside_a_docs_tree_cannot_break_the_probe(tmp_path, monkeypatch):
    """A listable but unenterable directory refuses to stat its children, so the probe's
    is_* checks raise; inside a tree that never feeds the verdict it is dropped, and the
    verdict stands."""
    _python_cli(tmp_path)
    _write(tmp_path, "docs/package.json", _pkg(deps={"react": "^19"}))
    before = recommend(probe(tmp_path))
    _deny_stat(monkeypatch, lambda path: "docs" in path.parent.parts)
    ev = probe(tmp_path)
    assert ev.unreadable == [] and ev.not_counted == []
    rec = recommend(ev)
    assert (rec.strategy, rec.reasons) == (before.strategy, before.reasons)


def test_a_malformed_docs_package_json_is_not_reported_unreadable(tmp_path):
    _python_cli(tmp_path)
    _write(tmp_path, "docs/package.json", "{not json")
    ev = probe(tmp_path)
    assert ev.unreadable == [] and ev.not_counted == []


# --- an unenterable directory in the main scan is reported, never a crash -----------------

def test_an_unenterable_subdir_in_the_main_scan_is_reported_not_raised(tmp_path, monkeypatch):
    """Outside a docs tree an unenterable directory may hold the product, so it is reported
    unreadable, and the rest of the tree still yields the verdict it would have without it."""
    _python_cli(tmp_path)
    _write(tmp_path, "frontend/package.json", _pkg(deps={"react": "^19"}))
    blocked = tmp_path / "frontend"
    _deny_stat(monkeypatch, lambda path: path.parent == blocked)
    ev = probe(tmp_path)
    assert ev.unreadable == ["frontend/ (PermissionError)"]
    assert ev.ui_frameworks == []
    rec = recommend(ev)
    assert rec.strategy == "test-suite"
    assert any("missing evidence" in f and "frontend/" in f for f in rec.follow_ups)
    assert "unreadable      : frontend/ (PermissionError)" in format_report(ev, rec)


def test_an_unstatable_child_in_the_main_walk_is_reported_not_raised(tmp_path, monkeypatch):
    """A directory that can be listed but not searched lets iterdir() name its children and
    then refuses to stat them, so the walk's own symlink/directory check raises."""
    _python_cli(tmp_path)
    _write(tmp_path, "apps/web/vite.config.ts")
    _write(tmp_path, "frontend/package.json", _pkg(deps={"vue": "^3"}))
    _deny_stat(monkeypatch, lambda path: path.parent.name == "apps")
    ev = probe(tmp_path)
    assert ev.unreadable == ["apps/ (PermissionError)"]
    # the readable sibling still counts
    assert ev.ui_frameworks == ["vue in frontend/package.json"]
    assert recommend(ev).strategy == "web-journey"


def test_an_unenterable_depth_two_leaf_is_reported_by_the_probe_itself(tmp_path, monkeypatch):
    """The walk never lists a directory at the depth limit, so only probe()'s own catch
    around reading that directory can report it - this is the test that keeps it honest."""
    _python_cli(tmp_path)
    _write(tmp_path, "apps/web/package.json", _pkg(deps={"react": "^19"}))
    blocked = tmp_path / "apps" / "web"
    _deny_stat(monkeypatch, lambda path: path.parent == blocked)
    ev = probe(tmp_path)
    assert ev.unreadable == ["apps/web/ (PermissionError)"]
    assert ev.ui_frameworks == []
    assert recommend(ev).strategy == "test-suite"


def test_an_unenterable_github_dir_is_reported_not_raised(tmp_path, monkeypatch):
    _python_cli(tmp_path)
    _write(tmp_path, ".github/workflows/ci.yml", "jobs:\n  t:\n    steps:\n      - run: pytest\n")
    _deny_stat(monkeypatch, lambda path: path.parent.name == ".github")
    ev = probe(tmp_path)
    assert ev.unreadable == [".github/workflows/ (PermissionError)"]
    assert ev.test_commands == ["pytest"]
