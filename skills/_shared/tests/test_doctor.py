"""Tests for ``agentflow.doctor`` — the runnable-when-broken diagnostic.

The property under test is unusual: this module's whole reason to exist is the
state where the resolver hard-fails, so most of these assert what happens when
things are BROKEN. A doctor that only works on a healthy machine is a status
line, not a diagnostic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from agentflow import doctor
from agentflow.doctor import FAIL, INFO, OK, WARN, Finding, diagnose, main, render

NL = chr(10)


def _write(home: Path, name: str, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / name).write_text(
        f"# config{NL}{NL}```yaml{NL}{textwrap.dedent(body).strip()}{NL}```{NL}",
        encoding="utf-8",
    )


def _healthy(tmp_path: Path, monkeypatch) -> Path:
    """A config home whose every surface exists, so a FAIL means the check found one."""
    home = tmp_path / "config"
    d = str(tmp_path).replace(chr(92), "/")
    data_root = tmp_path / "data"
    (data_root / "projects" / "demo" / "design" / "specs").mkdir(parents=True)
    (data_root / "projects" / "demo" / "design" / "plans").mkdir(parents=True)
    (data_root / "projects" / "demo" / "status").mkdir(parents=True)
    (data_root / "projects" / "demo" / "design" / "specs" / "a.md").write_text("x", encoding="utf-8")
    (data_root / "projects" / "demo" / "design" / "plans" / "a.md").write_text("x", encoding="utf-8")
    (data_root / "projects" / "demo" / "status" / "registry.md").write_text(
        NL.join(("## Agents", "## Queue", "## Questions", "## Blockers", "## Directives")),
        encoding="utf-8",
    )
    checkout = tmp_path / "repo"
    (checkout / ".git").mkdir(parents=True)

    _write(home, "environment.md", f"""
        schema_version: 1
        person:
          name: Sample User
        data_root: {str(data_root).replace(chr(92), "/")}
        git_host:
          kind: local
        notify:
          kind: none
        lock_dir: {d}
        shell: posix
        suite_root: {d}
        worktree_root: {d}
    """)
    (home / "projects").mkdir(exist_ok=True)
    _write(home / "projects", "demo.md", f"""
        schema_version: 1
        name: demo
        canonical_repo:
          host_ref: owner/repo
          local_checkout: {str(checkout).replace(chr(92), "/")}
        verification:
          strategy: test-suite
    """)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    return home


def _by_check(findings) -> dict:
    return {f.check: f for f in findings}


def _clamped(home: Path) -> None:
    """Re-write demo.md with a dial the resolver will CLAMP rather than reject.

    120 is over the 60-minute ScheduleWakeup ceiling, so `config.py` resolves the
    project successfully and warns that the number in force is not the number
    written. That is the exact case the WARN tier exists for: nothing is broken and
    nothing is absent, but the surface is not as configured.
    """
    checkout = home.parent / "repo"
    _write(home / "projects", "demo.md", f"""
        schema_version: 1
        name: demo
        canonical_repo:
          host_ref: owner/repo
          local_checkout: {str(checkout).replace(chr(92), "/")}
        verification:
          strategy: test-suite
        audit:
          idle_wake_minutes: 120
    """)


def test_a_healthy_surface_reports_no_failures(tmp_path, monkeypatch):
    """The negative control. Without it, a diagnostic that fails on everything
    passes every 'it reports the breakage' test below."""
    _healthy(tmp_path, monkeypatch)
    findings = diagnose("demo")
    assert not [f for f in findings if f.failed], render(findings)
    assert _by_check(findings)["environment"].status == OK
    assert main(["--project", "demo"]) == 0


def test_a_BROKEN_config_is_reported_not_raised(tmp_path, monkeypatch):
    """THE LOAD-BEARING PROPERTY. This tool exists for the state where the resolver
    hard-fails; if it propagates that failure it cannot see its only patient."""
    home = tmp_path / "config"
    home.mkdir()
    (home / "environment.md").write_text("this is not a yaml block", encoding="utf-8")
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))

    findings = diagnose("demo")  # must not raise
    env = _by_check(findings)["environment"]
    assert env.failed
    assert "ConfigError" in env.detail
    assert main(["--project", "demo"]) == 1


def test_a_MISSING_config_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(tmp_path / "nowhere"))
    findings = diagnose("demo")  # must not raise
    assert _by_check(findings)["environment"].failed
    assert main(["--project", "demo"]) == 1


def test_a_broken_environment_SKIPS_the_rest_instead_of_cascading(tmp_path, monkeypatch):
    """Every later check reads the environment, so running them would print a page
    of derived failures that all restate the first one — and a reader who has to
    find the real failure in that page stops reading the report."""
    home = tmp_path / "config"
    home.mkdir()
    (home / "environment.md").write_text("not yaml", encoding="utf-8")
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))

    findings = diagnose("demo")
    assert len(findings) == 2, render(findings)
    assert findings[1].status == INFO
    assert "skipped" in findings[1].detail


def test_a_check_that_raises_becomes_a_FAILING_finding(tmp_path, monkeypatch):
    """Guarding only the checks we imagined failing is guarding nothing. A check
    that raises for a reason nobody predicted must still land in the report."""
    _healthy(tmp_path, monkeypatch)

    def boom(env, name):
        raise RuntimeError("unpredicted")

    monkeypatch.setattr(doctor, "_check_registry", boom)
    findings = diagnose("demo")  # must not raise
    registry = _by_check(findings)["registry"]
    assert registry.failed
    assert "RuntimeError: unpredicted" in registry.detail


def test_a_raised_check_keeps_the_exceptions_own_remediation(tmp_path, monkeypatch):
    """A `ConfigError` carries its own `remediation:` line. Overwriting that with a
    generic sentence would replace the one useful thing in the report."""
    home = tmp_path / "config"
    home.mkdir()
    (home / "environment.md").write_text("not yaml", encoding="utf-8")
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))

    summary = render(diagnose("demo"))
    assert "remediation:" in summary


def test_an_absent_optional_surface_is_INFO_not_FAIL(tmp_path, monkeypatch):
    """No vault and no autodoc root are legitimate configurations. Reporting them
    as failures teaches the reader to skim, which is how a diagnostic dies."""
    _healthy(tmp_path, monkeypatch)
    findings = _by_check(diagnose("demo"))
    assert findings["vault"].status == INFO
    assert findings["autodoc"].status == INFO
    assert not findings["vault"].failed


def test_a_path_that_does_not_exist_FAILS(tmp_path, monkeypatch):
    home = _healthy(tmp_path, monkeypatch)
    text = (home / "environment.md").read_text(encoding="utf-8")
    missing = str(tmp_path / "gone").replace(chr(92), "/")
    (home / "environment.md").write_text(
        text.replace(f"worktree_root: {str(tmp_path).replace(chr(92), '/')}",
                     f"worktree_root: {missing}"),
        encoding="utf-8",
    )
    findings = diagnose("demo")
    # The resolver rejects a non-existent required dir before doctor sees the field,
    # so this surfaces as the environment check failing - which is the same signal,
    # reported at the layer that noticed. What must NOT happen is a crash.
    assert any(f.failed for f in findings)
    assert main(["--project", "demo"]) == 1


def test_a_registry_missing_a_section_FAILS(tmp_path, monkeypatch):
    """A file that exists is not a registry. The five sections ARE the schema."""
    _healthy(tmp_path, monkeypatch)
    reg = tmp_path / "data" / "projects" / "demo" / "status" / "registry.md"
    reg.write_text("## Agents" + NL + "## Queue", encoding="utf-8")
    findings = _by_check(diagnose("demo"))
    assert findings["registry"].failed
    assert "Questions" in findings["registry"].detail


def test_a_glob_matching_nothing_FAILS(tmp_path, monkeypatch):
    """The layout and the glob defaults disagreeing is exactly the failure the
    three-pillar migration can cause, and it is silent everywhere else: a survey
    that matches nothing returns zero records and looks like an empty backlog.

    The DIRECTORY goes, not just its file: a layout that moved leaves no
    ``design/specs/`` behind, while an existing empty one is a fresh install (gh#416,
    pinned by the test below)."""
    _healthy(tmp_path, monkeypatch)
    specs = tmp_path / "data" / "projects" / "demo" / "design" / "specs"
    (specs / "a.md").unlink()
    specs.rmdir()
    findings = _by_check(diagnose("demo"))
    assert findings["glob:spec"].failed
    assert "no files match" in findings["glob:spec"].detail
    assert main(["--project", "demo"]) == 1


def test_an_EMPTY_existing_bucket_is_INFO_not_FAIL(tmp_path, monkeypatch):
    """gh#416: the installer scaffolds ``design/specs/`` and ``design/plans/`` empty, so
    a correct install before its first spec used to end its own self-check on two FAILs
    and exit 1, sending the user after a layout migration that never happened."""
    _healthy(tmp_path, monkeypatch)
    for bucket in ("specs", "plans"):
        (tmp_path / "data" / "projects" / "demo" / "design" / bucket / "a.md").unlink()
    findings = _by_check(diagnose("demo"))
    for label in ("glob:spec", "glob:plan"):
        assert findings[label].status == INFO, findings[label]
        assert "holds nothing yet" in findings[label].detail
    assert main(["--project", "demo"]) == 0


# ── gh#416: the single-project default, and output that cannot raise ─────────


def test_with_ONE_project_file_and_no_env_doctor_checks_that_project(tmp_path, monkeypatch):
    """The state provision.py ships in: exactly one projects/<name>.md and no
    AGENTFLOW_PROJECT. Every other resolver picks that project; doctor used to say
    "no project selected" and call a surface it never examined healthy."""
    _healthy(tmp_path, monkeypatch)   # one project file, AGENTFLOW_PROJECT deleted
    checks = _by_check(diagnose())
    assert "project:demo" in checks, render(list(checks.values()))
    assert checks["project:demo"].status == OK
    assert "project" not in checks          # the "no project selected" INFO
    assert "glob:spec" in checks            # the project checks actually ran


def test_with_TWO_project_files_and_no_env_no_project_is_selected(tmp_path, monkeypatch):
    """Control: the default is 'exactly one', so two files resolve to nothing - the
    resolver's answer, not a guess by doctor."""
    home = _healthy(tmp_path, monkeypatch)
    (home / "projects" / "other.md").write_text(
        (home / "projects" / "demo.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    checks = _by_check(diagnose())
    assert checks["project"].status == INFO
    assert "no project selected" in checks["project"].detail
    assert not [c for c in checks if c.startswith("project:")]


def test_an_explicit_project_still_beats_the_single_file_default(tmp_path, monkeypatch):
    _healthy(tmp_path, monkeypatch)
    checks = _by_check(diagnose("nosuch"))
    assert checks["project:nosuch"].failed


def test_a_non_cp1252_character_in_the_report_does_not_raise(tmp_path, monkeypatch):
    """gh#416: the final write sits outside every per-check guard, and a config value,
    project name or user folder reaches it verbatim. On a legacy cp1252 console one
    character outside that code page raised UnicodeEncodeError out of main()."""
    import io
    import sys

    _healthy(tmp_path, monkeypatch)
    alien = "→ 文字"   # an arrow and two CJK characters; none are in cp1252
    real = doctor.diagnose
    monkeypatch.setattr(
        doctor, "diagnose",
        lambda project=None: real(project) + [Finding("unicode", INFO, alien)],
    )
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    rc = main(["--project", "demo"])   # must not raise
    stream.flush()
    assert rc == 0
    out = raw.getvalue().decode("utf-8")   # strict: the bytes ARE UTF-8
    assert alien in out
    assert "resolved surface healthy" in out


def test_render_restates_the_failures_at_the_end():
    """A twenty-line report with one FAIL in the middle is a report whose reader
    misses the FAIL."""
    out = render([
        Finding("a", OK, "fine"),
        Finding("b", FAIL, "broken", "fix b"),
        Finding("c", OK, "fine"),
    ])
    assert "1 FAILING:" in out
    assert out.strip().endswith("fix b")


def test_render_says_so_when_nothing_failed():
    assert "healthy" in render([Finding("a", OK, "fine")])


# ── The WARN tier: resolved, but not as the operator configured it ────────────


def test_a_clamped_dial_is_a_WARN_finding(tmp_path, monkeypatch):
    """The gap this epic closes: the value in force is not the value written down.

    Before the tier existed this case matched none of OK/FAIL/INFO, so it fell out of
    the report and the run still ended "resolved surface healthy".
    """
    home = _healthy(tmp_path, monkeypatch)
    _clamped(home)
    findings = diagnose("demo")
    warned = [f for f in findings if f.warned]
    assert len(warned) == 1, render(findings)
    assert warned[0].status == WARN
    assert "idle_wake_minutes" in warned[0].detail
    assert "60" in warned[0].detail


def test_a_clamped_dial_does_not_FAIL_anything(tmp_path, monkeypatch):
    """A warning is not a soft failure. Nothing is broken; a dial was clamped."""
    home = _healthy(tmp_path, monkeypatch)
    _clamped(home)
    findings = diagnose("demo")
    assert not [f for f in findings if f.failed], render(findings)


def test_a_WARN_does_not_fail_the_exit_code(tmp_path, monkeypatch, capsys):
    """Pinned deliberately, and cited in `main`: only FAIL exits 1.

    A health check that exits 1 over a clamped preference is one people stop running
    - the same argument the INFO docstring makes, one tier over.
    """
    home = _healthy(tmp_path, monkeypatch)
    _clamped(home)
    assert main(["--project", "demo"]) == 0


def test_a_FAIL_still_exits_1_when_a_warning_is_also_present(tmp_path, monkeypatch, capsys):
    """The other half of the exit-code rule: a warning must not MASK a failure."""
    home = _healthy(tmp_path, monkeypatch)
    _clamped(home)
    (tmp_path / "repo" / ".git").rmdir()   # checkout stops being a git repository
    assert main(["--project", "demo"]) == 1


def test_the_warning_reaches_STDOUT_where_a_redirect_keeps_it(tmp_path, monkeypatch, capsys):
    """`doctor > report.txt` keeps stdout and discards stderr.

    The clamp used to reach stderr alone, so the saved artifact - the thing somebody
    pastes into a bug report - kept the verdict and dropped the contradiction.
    """
    home = _healthy(tmp_path, monkeypatch)
    _clamped(home)
    main(["--project", "demo"])
    out = capsys.readouterr().out
    assert "idle_wake_minutes" in out
    assert "WARNING" in out


def test_the_verdict_stops_saying_healthy_unqualified(tmp_path, monkeypatch, capsys):
    """The verdict is the line people read, so the qualification has to live there."""
    home = _healthy(tmp_path, monkeypatch)
    _clamped(home)
    main(["--project", "demo"])
    last = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
    assert "NOT everywhere as configured" in last


def test_a_warning_is_reported_ONCE_not_per_resolution(tmp_path, monkeypatch):
    """The surface was resolved twice per run, so every warning printed twice.

    A warning printed twice is one a reader learns to skip. Asserted on the rendered
    report rather than on the finding list, because the duplicate a reader saw was in
    the output.
    """
    home = _healthy(tmp_path, monkeypatch)
    _clamped(home)
    out = render(diagnose("demo"))
    assert out.count("exceeds the 60-minute") == 2   # once in the check line, once restated
    assert len([ln for ln in out.splitlines() if ln.startswith(WARN)]) == 1


# --------------------------------------------------------------------------- #
# gh#254 - a vocabulary cut lands in the code and in the corpus at different moments
#
# The survey already warns once per unknown queue_class, and that line is identical whether
# one spec has a typo or every spec is mid-migration. doctor was blind to both: run against
# the corpus where 93 of 108 specs were out of vocabulary it still said "resolved surface
# healthy" (gh#252). These pin the distinction, not just the detection.
# --------------------------------------------------------------------------- #


def _spec(home_parent: Path, slug: str, queue_class: str) -> None:
    """One surveyable spec under demo/design/specs carrying a queue_class."""
    specs = home_parent / "data" / "projects" / "demo" / "design" / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    (specs / f"{slug}.md").write_text(
        NL.join(("---", f"queue_class: {queue_class}", "---", "", f"# {slug}", "")),
        encoding="utf-8",
    )


def test_a_corpus_wide_unknown_queue_class_FAILS_and_names_the_migration(tmp_path, monkeypatch):
    home = _healthy(tmp_path, monkeypatch)
    for i in range(4):
        _spec(tmp_path, f"old-{i}", "autoloop-buildable")

    f = _by_check(diagnose("demo"))["vocabulary"]

    assert f.status == FAIL
    assert "autoloop-buildable" in f.detail
    assert "4 of 4" in f.detail
    assert "migrate_queue_class" in f.remediation, "the remediation names the act, not the state"
    assert "MIGRATION, not a typo" in f.remediation
    assert home.exists()


def test_a_lone_bad_value_is_a_WARN_not_a_migration(tmp_path, monkeypatch):
    """The whole point of the ratio: one stray value must NOT read as a pending cut."""
    _healthy(tmp_path, monkeypatch)
    _spec(tmp_path, "typo", "fleet-buildabel")
    for i in range(5):
        _spec(tmp_path, f"fine-{i}", "fleet-buildable")

    f = _by_check(diagnose("demo"))["vocabulary"]

    assert f.status == WARN, "below the mass threshold"
    assert "fleet-buildabel" in f.detail
    assert "migrate_queue_class" not in f.remediation


def test_a_corpus_entirely_in_vocabulary_is_OK(tmp_path, monkeypatch):
    _healthy(tmp_path, monkeypatch)
    _spec(tmp_path, "a", "fleet-buildable")
    _spec(tmp_path, "b", "local-tooling")

    f = _by_check(diagnose("demo"))["vocabulary"]

    assert f.status == OK
    assert "all in vocabulary" in f.detail


def test_two_bad_specs_do_not_trip_the_floor(tmp_path, monkeypatch):
    """Half of two is not a migration - the floor exists so a tiny corpus cannot claim one."""
    _healthy(tmp_path, monkeypatch)
    _spec(tmp_path, "x", "autoloop-buildable")
    _spec(tmp_path, "y", "autoloop-buildable")

    f = _by_check(diagnose("demo"))["vocabulary"]

    assert f.status == WARN
    assert "migrate_queue_class" not in f.remediation


def test_render_restates_the_warnings_at_the_end():
    out = render([
        Finding("a", OK, "fine"),
        Finding("b", WARN, "clamped to 60"),
    ])
    assert "1 WARNING:" in out
    assert out.index("1 WARNING:") > out.index("fine")


def test_render_keeps_failures_and_warnings_in_separate_sections():
    out = render([
        Finding("a", FAIL, "broken", "fix a"),
        Finding("b", WARN, "clamped"),
    ])
    assert "1 FAILING:" in out and "1 WARNING:" in out
    assert out.index("1 FAILING:") < out.index("1 WARNING:")
    # A run with failures does not also claim health, warnings or not.
    assert "healthy" not in out


def test_the_collector_does_not_outlive_the_diagnosis(tmp_path, monkeypatch):
    """A handler left attached would collect into a dead object for the next importer.

    `diagnose` is a library function with two callers (this CLI and the steward), so a
    leak here would accumulate across a long-lived loop process.
    """
    import logging

    logger = logging.getLogger("agentflow.config")
    before = list(logger.handlers)
    home = _healthy(tmp_path, monkeypatch)
    _clamped(home)
    diagnose("demo")
    assert list(logger.handlers) == before


def test_a_warning_check_name_survives_a_message_with_no_dial():
    """The name is cosmetic; an unparseable message must still report, not crash."""
    assert doctor._warning_check("something odd happened") == "config"
    assert doctor._warning_check("a.b=1 went wrong") == "config:a.b"


def test_the_surface_is_resolved_ONCE_per_run(tmp_path, monkeypatch):
    """Asserts the RESOLUTION count, not the reported count.

    The collector deduplicates by message, so a report showing one warning is
    compatible with the surface still being resolved twice - the dedupe would hide
    exactly the defect it was written beside. This counts the log records the
    resolver actually emits, which is one per resolution.
    """
    import logging

    records: list = []

    class _Counter(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("agentflow.config")
    counter = _Counter(level=logging.WARNING)
    logger.addHandler(counter)
    try:
        home = _healthy(tmp_path, monkeypatch)
        _clamped(home)
        diagnose("demo")
    finally:
        logger.removeHandler(counter)

    assert len([m for m in records if "exceeds the 60-minute" in m]) == 1, records


def test_the_collector_deduplicates_identical_warnings():
    """Unit-tested directly, because `diagnose` no longer produces a duplicate.

    Found by mutation: deleting the dedupe left the whole suite green, since the
    single-resolution fix means nothing in an end-to-end run emits the same warning
    twice any more. The dedupe is the backstop for a FUTURE caller that resolves more
    than once, so the only honest way to test it is to hand it the duplicate itself.
    """
    import logging

    collector = doctor._ResolutionWarnings()
    for _ in range(2):
        collector.emit(logging.LogRecord(
            "agentflow.config", logging.WARNING, __file__, 0, "dial=9 clamped", None, None))
    collector.emit(logging.LogRecord(
        "agentflow.config", logging.WARNING, __file__, 0, "other=1 clamped", None, None))

    findings = collector.findings()
    assert [f.detail for f in findings] == ["dial=9 clamped", "other=1 clamped"]
    assert all(f.status == WARN for f in findings)


def test_each_surface_is_LOADED_exactly_once(tmp_path, monkeypatch):
    """Counts the loader calls themselves, for both surfaces.

    Also found by mutation: the warning-counting test above can only see the PROJECT
    file, because the dial that warns lives there. Re-resolving the ENVIRONMENT emits
    nothing, so a second `load_environment()` was invisible to every assertion in this
    file - the resolution cost and the re-validation were real and unmeasured.
    """
    from agentflow import config as config_mod

    calls = {"env": 0, "proj": 0}
    real_env = config_mod.load_environment
    real_proj = config_mod.load_project

    def counting_env(*a, **kw):
        calls["env"] += 1
        return real_env(*a, **kw)

    def counting_proj(*a, **kw):
        calls["proj"] += 1
        return real_proj(*a, **kw)

    monkeypatch.setattr(config_mod, "load_environment", counting_env)
    monkeypatch.setattr(config_mod, "load_project", counting_proj)

    _healthy(tmp_path, monkeypatch)
    diagnose("demo")
    assert calls == {"env": 1, "proj": 1}


# ── autodoc runtime: a configured root is not a runnable autodoc (gh#620) ─────
#
# Install step 6 (`npm ci` + `pip install -e` of autodoc_graph) runs only when autodoc is on
# at provision time, and the interview says to leave `autodoc_root` null. Setting it later
# made this line say "never initialised - run autodoc-init", and init then died after the
# clone on `python -m autodoc_graph.cli`. The fakes below stand in for the two probes so the
# verdict does not depend on what this interpreter happens to have installed.


def _autodoc_root(tmp_path: Path, monkeypatch, *, manifest: bool) -> Path:
    from agentflow import config as config_mod

    root = tmp_path / "autodoc"
    root.mkdir()
    if manifest:
        (root / ".manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_mod, "resolve_autodoc_root", lambda env, proj=None: str(root))
    return root


def _runtime(monkeypatch, *, graph, node: bool) -> None:
    # `graph` is True / False / None (the probe could not tell). raising=False: the names are
    # this fix's seam; a red run against an older module must fail on the VERDICT, not on a
    # missing attribute.
    monkeypatch.setattr(
        doctor, "_graph_importable", lambda module="autodoc_graph": graph, raising=False,
    )
    monkeypatch.setattr(
        doctor, "which",
        lambda name: "/usr/bin/node" if (node and name == "node") else None,
        raising=False,
    )


def test_a_root_without_the_graph_package_WARNS_and_names_reprovision_first(tmp_path, monkeypatch):
    _autodoc_root(tmp_path, monkeypatch, manifest=False)
    _runtime(monkeypatch, graph=False, node=True)
    f = doctor._check_autodoc(object(), object())
    assert f.status == WARN, f
    assert "autodoc_graph" in f.detail
    assert "--autodoc" in f.remediation
    # Re-provision BEFORE init: init is the step that dies without it.
    assert f.remediation.index("provision.py --autodoc") < f.remediation.index("autodoc-init")


def test_a_root_without_node_WARNS_and_says_install_node_first(tmp_path, monkeypatch):
    _autodoc_root(tmp_path, monkeypatch, manifest=False)
    _runtime(monkeypatch, graph=True, node=False)
    f = doctor._check_autodoc(object(), object())
    assert f.status == WARN, f
    assert "node" in f.detail
    low = f.remediation.lower()
    assert low.index("node") < low.index("provision.py --autodoc")


def test_both_runtimes_missing_are_named_in_one_finding(tmp_path, monkeypatch):
    _autodoc_root(tmp_path, monkeypatch, manifest=False)
    _runtime(monkeypatch, graph=False, node=False)
    f = doctor._check_autodoc(object(), object())
    assert f.status == WARN, f
    assert "autodoc_graph" in f.detail and "node" in f.detail


def test_an_initialised_corpus_with_no_runtime_still_WARNS(tmp_path, monkeypatch):
    """A manifest proves a corpus was built once, not that /autodoc-update can run here."""
    _autodoc_root(tmp_path, monkeypatch, manifest=True)
    _runtime(monkeypatch, graph=False, node=True)
    f = doctor._check_autodoc(object(), object())
    assert f.status == WARN, f
    assert "provision.py --autodoc" in f.remediation
    assert "autodoc-init" not in f.remediation, "the corpus exists; init is the wrong remedy"


def test_a_runnable_root_with_no_manifest_is_still_INFO_pointing_at_init(tmp_path, monkeypatch):
    _autodoc_root(tmp_path, monkeypatch, manifest=False)
    _runtime(monkeypatch, graph=True, node=True)
    f = doctor._check_autodoc(object(), object())
    assert f.status == INFO, f
    assert "autodoc-init" in f.remediation
    assert "provision.py" not in f.remediation


def test_a_runnable_initialised_root_is_OK(tmp_path, monkeypatch):
    """The happy path - the negative control for the WARN tests above."""
    root = _autodoc_root(tmp_path, monkeypatch, manifest=True)
    _runtime(monkeypatch, graph=True, node=True)
    f = doctor._check_autodoc(object(), object())
    assert f.status == OK, f
    assert f.detail == str(root)


def test_no_root_never_probes_the_runtime(tmp_path, monkeypatch):
    """Autodoc is optional: an install that never enabled it owes neither Node nor the package."""
    from agentflow import config as config_mod

    monkeypatch.setattr(config_mod, "resolve_autodoc_root", lambda env, proj=None: None)

    def probe(name="autodoc_graph"):
        raise AssertionError(f"probed {name!r} with no autodoc root")

    monkeypatch.setattr(doctor, "_graph_importable", probe, raising=False)
    monkeypatch.setattr(doctor, "which", probe, raising=False)
    f = doctor._check_autodoc(object(), object())
    assert f.status == INFO, f


def test_a_missing_runtime_does_not_fail_the_exit_code(tmp_path, monkeypatch, capsys):
    _healthy(tmp_path, monkeypatch)
    _autodoc_root(tmp_path, monkeypatch, manifest=False)
    _runtime(monkeypatch, graph=False, node=False)
    assert main(["--project", "demo"]) == 0
    out = capsys.readouterr().out
    assert "provision.py --autodoc" in out


def test_the_reprovision_remedy_keeps_declined_auto_allow_hooks_declined(tmp_path, monkeypatch):
    """A bare `provision.py --autodoc` re-runs step 8 and writes back auto-allow hooks the
    operator declined (docs/INSTALL.md section 13), so the remedy has to say so."""
    for manifest in (False, True):
        sub = tmp_path / ("m" if manifest else "n")
        sub.mkdir()
        _autodoc_root(sub, monkeypatch, manifest=manifest)
        _runtime(monkeypatch, graph=False, node=True)
        f = doctor._check_autodoc(object(), object())
        assert "--no-auto-allow" in f.remediation, f
        assert "declined" in f.remediation, f


def test_an_UNKNOWN_graph_probe_is_not_a_WARN(tmp_path, monkeypatch):
    """A probe that could not answer (timed out) must not manufacture a false WARN."""
    _autodoc_root(tmp_path, monkeypatch, manifest=True)
    _runtime(monkeypatch, graph=None, node=True)
    f = doctor._check_autodoc(object(), object())
    assert f.status == OK, f
    assert "not verified" in f.detail


# ── the graph probe itself runs in a FRESH interpreter ─────────────────────────
#
# provision.py prints its closing doctor report in-process, in the venv child whose sys.path
# was fixed before step 6's `pip install -e` wrote its editable .pth. An in-process find_spec
# there still says "not installed" about a package installed seconds earlier.


def test_the_graph_probe_sees_a_package_this_process_cannot(tmp_path, monkeypatch):
    import importlib.util
    import sys as _sys

    name = "gh620_fresh_probe_pkg"
    (tmp_path / name).mkdir()
    (tmp_path / name / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert str(tmp_path) not in _sys.path
    assert importlib.util.find_spec(name) is None, "control: this process cannot see it"
    assert doctor._graph_importable(name) is True


def test_the_graph_probe_reports_an_absent_package_as_False():
    assert doctor._graph_importable("gh620_no_such_package_anywhere") is False


def test_a_timed_out_probe_is_unknown_not_absent(monkeypatch):
    import subprocess as _sp

    def slow(*a, **kw):
        raise _sp.TimeoutExpired(cmd="python", timeout=1)

    monkeypatch.setattr(doctor.subprocess, "run", slow)
    assert doctor._graph_importable() is None


def test_a_probe_that_cannot_start_falls_back_to_find_spec(monkeypatch):
    def cannot(*a, **kw):
        raise OSError("no interpreter")

    monkeypatch.setattr(doctor.subprocess, "run", cannot)
    monkeypatch.setattr(doctor, "find_spec", lambda name: object())
    assert doctor._graph_importable() is True
    monkeypatch.setattr(doctor, "find_spec", lambda name: None)
    assert doctor._graph_importable() is False


# --- gh#692: local_checkout's origin must resolve to canonical_repo.host_ref ----------------


def _real_checkout(tmp_path: Path, origin: str | None, branch_ref: str = "refs/heads/main") -> Path:
    """Turn the `_healthy` fixture's fake `.git` into a real repository, with *origin* if given.

    *branch_ref* is pointed at one empty commit, so the profile's default base_branch
    resolves (gh#745): a bare `git init` has no branch at all, which is a real FAIL.
    """
    checkout = tmp_path / "repo"
    (checkout / ".git").rmdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    if origin is not None:
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", origin], check=True)
    if branch_ref:
        # commit-tree + update-ref rather than `git commit`: no identity, hooks or signing
        # config from the machine running the suite can reach a fixture repository.
        git = ["git", "-C", str(checkout), "-c", "commit.gpgsign=false"]
        ident = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        tree = subprocess.run([*git, "mktree"], input="", capture_output=True, text=True,
                              check=True).stdout.strip()
        sha = subprocess.run([*git, "commit-tree", tree, "-m", "init"], capture_output=True,
                             text=True, check=True, env=ident).stdout.strip()
        subprocess.run([*git, "update-ref", branch_ref, sha], check=True)
    return checkout


def test_a_checkout_with_no_origin_WARNs_rather_than_passing(tmp_path, monkeypatch):
    """Local-only repos are supported, so no origin is not a FAIL - but it is not `ok` either."""
    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, None)
    checkout = _by_check(diagnose("demo"))["checkout"]
    assert checkout.status == WARN, checkout
    assert "no origin" in checkout.detail
    assert "owner/repo" in checkout.detail
    assert main(["--project", "demo"]) == 0, "a WARN never fails the exit code"


def test_a_foreign_origin_FAILs_naming_the_expected_and_the_actual_repo(tmp_path, monkeypatch):
    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, "https://github.com/someone-else/unrelated.git")
    checkout = _by_check(diagnose("demo"))["checkout"]
    assert checkout.failed, checkout
    assert "someone-else/unrelated" in checkout.detail
    assert "owner/repo" in checkout.detail
    assert main(["--project", "demo"]) == 1


def test_a_foreign_origin_is_compared_on_BOTH_path_segments(tmp_path, monkeypatch):
    """Same repo name under another owner is another repository."""
    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, "git@github.com:someone-else/repo.git")
    assert _by_check(diagnose("demo"))["checkout"].failed


@pytest.mark.parametrize("origin", [
    "https://github.com/owner/repo.git",
    "https://github.com/owner/repo",
    "https://example.com/Owner/Repo/",
    "git@github.com:owner/repo.git",
    "ssh://git@example.com:2222/owner/repo.git",
    "https://x-access-token:SECRET123@github.com/owner/repo.git",
])
def test_a_matching_origin_passes_in_https_and_ssh_forms(tmp_path, monkeypatch, origin):
    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, origin)
    checkout = _by_check(diagnose("demo"))["checkout"]
    assert checkout.status == OK, checkout


@pytest.mark.parametrize("origin", [
    "https://someone:SECRET123@example.com/someone-else/unrelated.git",
    "https://SECRET123@example.com/someone-else/unrelated.git",
    "https://someone:SECRET123@example.com/",
    "https://someone:SECRET123@example.com/owner/repo.git",
])
def test_credentials_in_the_origin_url_are_never_echoed(tmp_path, monkeypatch, capsys, origin):
    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, origin)
    findings = diagnose("demo")
    assert "SECRET123" not in render(findings)
    assert all("SECRET123" not in f.remediation for f in findings)
    main(["--project", "demo"])
    assert "SECRET123" not in capsys.readouterr().out


def test_an_origin_naming_no_owner_repo_is_not_compared_and_not_echoed(tmp_path, monkeypatch):
    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, "https://someone:SECRET123@example.com/")
    checkout = _by_check(diagnose("demo"))["checkout"]
    assert checkout.status == WARN, checkout
    assert "SECRET123" not in checkout.detail


def test_git_failing_is_unmeasured_not_a_verdict(tmp_path, monkeypatch):
    """The `_healthy` fixture's `.git` is an empty directory, which git refuses. That is the
    doctor's cannot-measure case: the line stays `ok` and says the origin was not verified,
    the way the autodoc probe reports a timeout."""
    _healthy(tmp_path, monkeypatch)
    checkout = _by_check(diagnose("demo"))["checkout"]
    assert checkout.status == OK, checkout
    assert "origin not verified" in checkout.detail


def test_git_missing_or_hung_is_unmeasured_not_a_crash(tmp_path, monkeypatch):
    import subprocess as _sp

    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, "https://github.com/someone-else/unrelated.git")

    def slow(*a, **kw):
        raise _sp.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(doctor.subprocess, "run", slow)
    checkout = _by_check(diagnose("demo"))["checkout"]
    assert checkout.status == OK and "origin not verified" in checkout.detail, checkout

    def absent(*a, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr(doctor.subprocess, "run", absent)
    checkout = _by_check(diagnose("demo"))["checkout"]
    assert checkout.status == OK and "origin not verified" in checkout.detail, checkout


def test_a_query_token_in_the_origin_is_ignored_and_never_echoed(tmp_path, monkeypatch, capsys):
    """A query names no repository and can carry a token: the ref is read before it."""
    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, "https://github.com/owner/repo.git?private_token=SECRET5")
    findings = diagnose("demo")
    checkout = _by_check(findings)["checkout"]
    assert checkout.status == OK, checkout
    assert "SECRET5" not in render(findings)
    main(["--project", "demo"])
    assert "SECRET5" not in capsys.readouterr().out


def test_a_query_token_on_a_foreign_origin_is_not_echoed(tmp_path, monkeypatch):
    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, "https://example.com/someone-else/unrelated.git?private_token=SECRET5")
    checkout = _by_check(diagnose("demo"))["checkout"]
    assert checkout.failed, checkout
    assert "someone-else/unrelated" in checkout.detail
    assert "SECRET5" not in checkout.detail and "SECRET5" not in checkout.remediation


def test_a_fragment_in_the_origin_is_ignored(tmp_path, monkeypatch):
    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, "https://github.com/owner/repo.git#SECRET5")
    checkout = _by_check(diagnose("demo"))["checkout"]
    assert checkout.status == OK, checkout


def test_a_multi_at_userinfo_leaves_nothing_behind():
    stripped = doctor._strip_userinfo("fatal: could not read https://user:p@ss@example.com/o/r")
    assert stripped == "fatal: could not read https://example.com/o/r"
    assert doctor._origin_ref("https://user:p@ss@example.com/owner/repo.git") == "owner/repo"


# --- gh#745: three setups doctor used to misreport ------------------------------------------


def test_a_missing_project_subtree_says_scaffold_not_moved(tmp_path, monkeypatch):
    """No <data_root>/projects/<name>/ at all is provision never having run for the project.
    Three "one of them moved" FAILs sent the reader after a migration that never happened."""
    _healthy(tmp_path, monkeypatch)
    shutil.rmtree(tmp_path / "data" / "projects" / "demo")
    findings = diagnose("demo")
    checks = _by_check(findings)
    assert checks["glob"].failed, checks["glob"]
    assert "not scaffolded" in checks["glob"].remediation
    assert "install/provision.py" in checks["glob"].remediation
    assert not [c for c in checks if c.startswith("glob:")], "one finding, not one per glob"
    assert not any("moved" in f.remediation for f in findings), render(findings)


def test_a_missing_bucket_under_an_existing_project_still_says_moved(tmp_path, monkeypatch):
    """Control: the real layout-moved case keeps its wording."""
    _healthy(tmp_path, monkeypatch)
    shutil.rmtree(tmp_path / "data" / "projects" / "demo" / "design" / "specs")
    spec = _by_check(diagnose("demo"))["glob:spec"]
    assert spec.failed
    assert "moved" in spec.remediation


def _base_branch(home: Path, branch: str) -> None:
    checkout = home.parent / "repo"
    _write(home / "projects", "demo.md", f"""
        schema_version: 1
        name: demo
        canonical_repo:
          host_ref: owner/repo
          local_checkout: {str(checkout).replace(chr(92), "/")}
          base_branch: {branch}
        verification:
          strategy: test-suite
    """)


def test_an_existing_local_base_branch_is_OK(tmp_path, monkeypatch):
    _healthy(tmp_path, monkeypatch)
    _real_checkout(tmp_path, "https://github.com/owner/repo.git")
    base = _by_check(diagnose("demo"))["base_branch"]
    assert base.status == OK, base
    assert "refs/heads/main" in base.detail


def test_a_base_branch_that_exists_nowhere_FAILS(tmp_path, monkeypatch):
    home = _healthy(tmp_path, monkeypatch)
    _base_branch(home, "nope")
    _real_checkout(tmp_path, "https://github.com/owner/repo.git")
    base = _by_check(diagnose("demo"))["base_branch"]
    assert base.failed, base
    assert "'nope'" in base.detail
    assert base.remediation == "set canonical_repo.base_branch to an existing branch"
    assert main(["--project", "demo"]) == 1


def test_a_remote_tracking_base_branch_is_enough(tmp_path, monkeypatch):
    """A fresh clone has only the default branch locally; origin/<b> is still a real base."""
    home = _healthy(tmp_path, monkeypatch)
    _base_branch(home, "trunk")
    _real_checkout(tmp_path, "https://github.com/owner/repo.git",
                   branch_ref="refs/remotes/origin/trunk")
    base = _by_check(diagnose("demo"))["base_branch"]
    assert base.status == OK, base
    assert "refs/remotes/origin/trunk" in base.detail


def test_a_repository_git_cannot_read_is_unverified_not_FAIL(tmp_path, monkeypatch):
    """The `_healthy` fixture's `.git` is an empty directory git refuses - git failing to
    answer is not git answering no."""
    _healthy(tmp_path, monkeypatch)
    base = _by_check(diagnose("demo"))["base_branch"]
    assert base.status == OK, base
    assert "not verified" in base.detail


def test_no_checkout_skips_the_base_branch_check(tmp_path):
    """The checkout finding already FAILs for this; a second FAIL would restate it."""
    from types import SimpleNamespace

    proj = SimpleNamespace(canonical_repo=SimpleNamespace(
        local_checkout=str(tmp_path / "gone"), base_branch="main"))
    base = doctor._check_base_branch(proj)
    assert base.status == INFO, base
    assert "not checked" in base.detail


def test_the_CLI_with_no_project_selected_does_not_call_the_surface_healthy(
        tmp_path, monkeypatch, capsys):
    home = _healthy(tmp_path, monkeypatch)
    (home / "projects" / "other.md").write_text(
        (home / "projects" / "demo.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert main([]) == 0
    out = capsys.readouterr().out
    last = out.rstrip().splitlines()[-1]
    assert "resolved surface healthy" not in out, out
    assert last.startswith("environment and paths only:"), last
    assert "2 profile(s) configured" in last and "none selected" in last
    assert "pass --project" in last


def test_the_CLI_with_no_profiles_says_no_project_was_checked(tmp_path, monkeypatch, capsys):
    home = _healthy(tmp_path, monkeypatch)
    (home / "projects" / "demo.md").unlink()
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "resolved surface healthy" not in out, out
    assert "no project checked" in out


def test_the_CLI_with_a_project_keeps_the_healthy_verdict(tmp_path, monkeypatch, capsys):
    """Control: a run that examined a project says healthy exactly as before."""
    _healthy(tmp_path, monkeypatch)
    assert main(["--project", "demo"]) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-1] == "resolved surface healthy"


def test_the_unexamined_verdict_leaves_a_failing_report_alone(tmp_path):
    report = render([Finding("path:data_root", FAIL, "missing", "create it")])
    assert doctor.unexamined_verdict(report, tmp_path / "projects") == report


def test_the_unexamined_verdict_keeps_the_warning_qualifier(tmp_path):
    report = render([Finding("config:x", WARN, "clamped")])
    out = doctor.unexamined_verdict(report, tmp_path / "projects")
    assert "environment and paths only (see the warning(s) above):" in out


def _child_commit(checkout: Path) -> None:
    """Advance refs/heads/main by one commit, so `main~1` names a real commit."""
    git = ["git", "-C", str(checkout), "-c", "commit.gpgsign=false"]
    ident = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    tree = subprocess.run([*git, "mktree"], input="", capture_output=True, text=True,
                          check=True).stdout.strip()
    sha = subprocess.run([*git, "commit-tree", tree, "-p", "refs/heads/main", "-m", "two"],
                         capture_output=True, text=True, check=True, env=ident).stdout.strip()
    subprocess.run([*git, "update-ref", "refs/heads/main", sha], check=True)


def test_revision_syntax_is_not_a_branch(tmp_path, monkeypatch):
    """`main~1` resolves to a commit under rev-parse, but no branch has that name."""
    home = _healthy(tmp_path, monkeypatch)
    _base_branch(home, "main~1")
    checkout = _real_checkout(tmp_path, "https://github.com/owner/repo.git")
    _child_commit(checkout)
    assert subprocess.run(["git", "-C", str(checkout), "rev-parse", "--verify", "--quiet",
                           "refs/heads/main~1"], capture_output=True).returncode == 0, \
        "control: the revision must really resolve, or this test proves nothing"
    base = _by_check(diagnose("demo"))["base_branch"]
    assert base.failed, base


def test_a_base_branch_differing_only_in_case_FAILS(tmp_path, monkeypatch):
    """A case-insensitive filesystem opens the loose ref `main` for `MAIN`; git refs are
    case-sensitive, and every other platform the fleet runs on would not find it."""
    home = _healthy(tmp_path, monkeypatch)
    _base_branch(home, "MAIN")
    _real_checkout(tmp_path, "https://github.com/owner/repo.git")
    base = _by_check(diagnose("demo"))["base_branch"]
    assert base.failed, base


def test_an_unreadable_project_subtree_is_a_finding_not_a_raise(tmp_path, monkeypatch):
    _healthy(tmp_path, monkeypatch)
    project_dir = tmp_path / "data" / "projects" / "demo"
    real_is_dir = Path.is_dir

    def is_dir(self):
        if self == project_dir:
            raise PermissionError("denied")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    glob = _by_check(diagnose("demo"))["glob"]   # must not raise
    assert glob.failed
    assert "PermissionError" in glob.detail
