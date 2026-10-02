#!/usr/bin/env python3
"""Tests for the autodoc graph-package provisioning in install/provision.py (epic 702).

The autodoc runtime step (step 6, autodoc ONLY) now provisions BOTH autodoc
runtimes: `npm ci` in the Node tree-sitter inventory package AND
`pip install -e` of the Python `autodoc_graph` package
(`skills/autodoc-init/references/graph`) into the suite venv. Mirrors the
inventory-package discipline: fixed path under suite_root (no config
round-trip), loud RuntimeError naming the expected path when missing, venv
python (never bare `python`).

Run: `python -m pytest install/test_provision_graph.py -v`
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision

AUTODOC_PKG_REL = "skills/autodoc-init/references/inventory"
GRAPH_PKG_REL = "skills/autodoc-init/references/graph"


def _make_opts(suite_root: Path, autodoc: bool = True) -> provision.Options:
    return provision.Options(
        dry_run=False,
        suite_root=suite_root,
        config_home=suite_root / "unused-config-home",
        settings_file=suite_root / "unused-settings.json",
        autodoc=autodoc,
        autodoc_reason="forced by --autodoc" if autodoc else "forced by --no-autodoc",
    )


def _scaffold(suite_root: Path, *, inventory: bool = True, graph: bool = True) -> None:
    if inventory:
        d = suite_root / AUTODOC_PKG_REL
        d.mkdir(parents=True)
        (d / "package.json").write_text("{}", encoding="utf-8")
    if graph:
        d = suite_root / GRAPH_PKG_REL
        d.mkdir(parents=True)
        (d / "pyproject.toml").write_text("[project]\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# _graph_pkg_dir
# --------------------------------------------------------------------------- #
def test_graph_pkg_dir_resolves_fixed_path_under_suite_root(tmp_path):
    _scaffold(tmp_path)
    assert provision._graph_pkg_dir(_make_opts(tmp_path)) == tmp_path / GRAPH_PKG_REL


def test_graph_pkg_dir_raises_naming_expected_path_when_missing(tmp_path):
    _scaffold(tmp_path, graph=False)
    expected = tmp_path / GRAPH_PKG_REL / "pyproject.toml"
    with pytest.raises(RuntimeError) as excinfo:
        provision._graph_pkg_dir(_make_opts(tmp_path))
    assert str(expected) in str(excinfo.value)


# --------------------------------------------------------------------------- #
# _do_autodoc_graph_pip
# --------------------------------------------------------------------------- #
def test_graph_pip_installs_editable_with_suite_venv_python(tmp_path):
    _scaffold(tmp_path)
    opts = _make_opts(tmp_path)
    with mock.patch.object(provision, "_run_cmd") as fake_run_cmd:
        provision._do_autodoc_graph_pip(opts)
    fake_run_cmd.assert_called_once()
    (cmd,), _ = fake_run_cmd.call_args
    assert cmd[0] == str(provision._venv_python(opts))  # venv python, never bare
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert "-e" in cmd
    assert cmd[-1] == str(tmp_path / GRAPH_PKG_REL)


def test_graph_pip_raises_before_running_when_package_missing(tmp_path):
    _scaffold(tmp_path, graph=False)
    with mock.patch.object(provision, "_run_cmd") as fake_run_cmd, \
         pytest.raises(RuntimeError):
        provision._do_autodoc_graph_pip(_make_opts(tmp_path))
    fake_run_cmd.assert_not_called()


# --------------------------------------------------------------------------- #
# step wiring
# --------------------------------------------------------------------------- #
def test_autodoc_step_runs_npm_then_graph_pip_in_order(tmp_path):
    _scaffold(tmp_path)
    opts = _make_opts(tmp_path)
    step = next(s for s in provision.build_steps(opts)
                if s.run is provision._do_autodoc_runtime)
    order: list[str] = []
    with mock.patch.object(provision, "_do_autodoc_npm",
                           side_effect=lambda o: order.append("npm")), \
         mock.patch.object(provision, "_do_autodoc_graph_pip",
                           side_effect=lambda o: order.append("pip")):
        step.run(opts)
    assert order == ["npm", "pip"]


def test_autodoc_step_skipped_when_autodoc_disabled(tmp_path):
    _scaffold(tmp_path)
    opts = _make_opts(tmp_path, autodoc=False)
    step = next(s for s in provision.build_steps(opts)
                if s.run is provision._do_autodoc_runtime)
    # `is_applicable(opts)` rather than the `applies` field: step 6's applicability is
    # LAZY now, because step 5 re-checks autodoc against the resolved config and a bool
    # captured when the list was built could not see that correction.
    assert step.is_applicable(opts) is False


def test_the_autodoc_step_reads_its_applicability_LATE(tmp_path):
    """The property that makes step 5's re-check reachable at all.

    Step 6 used to capture `ad_on = opts.autodoc` when the step list was built, so a
    correction applied during step 5 - after capture, before step 6 - changed nothing.
    The re-check would have run, printed, mutated opts, and been ignored.
    """
    _scaffold(tmp_path)
    opts = _make_opts(tmp_path, autodoc=False)
    step = next(s for s in provision.build_steps(opts)
                if s.run is provision._do_autodoc_runtime)
    assert step.is_applicable(opts) is False

    opts.autodoc = True          # exactly what _confirm_autodoc_against_config does
    opts.autodoc_reason = "resolved config says enabled"
    assert step.is_applicable(opts) is True, "step 6 is still reading a stale captured bool"
    assert "enabled" in step.skip_reason(opts) or step.is_applicable(opts)


def test_a_step_with_no_lazy_form_still_reads_its_plain_fields(tmp_path):
    """The narrowness control. Only steps 6 and 12 needed this; every other step keeps the
    eager `applies`/`skip_note`, and the accessors must fall through to them unchanged."""
    _scaffold(tmp_path)
    opts = _make_opts(tmp_path, autodoc=False)
    plain = [s for s in provision.build_steps(opts) if s.applies_fn is None]
    assert plain, "every step went lazy - that was not the change"
    for s in plain:
        assert s.is_applicable(opts) is s.applies
        assert s.skip_reason(opts) == s.skip_note


def test_autodoc_step_applies_and_mentions_graph_package_when_enabled(tmp_path):
    _scaffold(tmp_path)
    step = next(s for s in provision.build_steps(_make_opts(tmp_path, autodoc=True))
                if s.run is provision._do_autodoc_runtime)
    assert step.applies is True
    detail = "\n".join(step.detail)
    assert GRAPH_PKG_REL in detail
    assert "pip install -e" in detail


def test_step_count_is_unchanged(tmp_path):
    # graph pip folds into the existing autodoc step: numbering elsewhere holds.
    # 12 since E-0768 — the missed-wake watchdog registration is a step of its own,
    # appended AFTER scaffold (it needs step 11's registry to exist). This assertion is
    # the tripwire that catches a step inserted in the middle, which would renumber every
    # step a reader has already followed; it is not a claim that 11 is the right total.
    _scaffold(tmp_path)
    steps = provision.build_steps(_make_opts(tmp_path))
    assert [s.n for s in steps] == list(range(1, 13))
