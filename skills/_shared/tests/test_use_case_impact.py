"""Tests for the advisory use-case impact / touchpoint tool (Phase 4 T10).

Covers: registry-row lookup, plain-Python grep over refine/solution/flow,
stage grouping, id validation, graceful degradation when roots are absent,
and the load-bearing contract — the CLI ALWAYS exits 0 and never edits.
"""

import json
from pathlib import Path

import pytest

from agentflow import use_case_impact as uci
from agentflow.use_case_impact import (
    STAGES,
    find_touchpoints,
    is_valid_uc_id,
    main,
    read_registry_row,
    render,
)


def _seed_project(tmp_path: Path, uc_id: str = "UC-0147") -> Path:
    """Build a minimal data_root/projects/demo tree touching the id."""
    proj = tmp_path / "data" / "projects" / "demo"
    uc_dir = proj / "design" / "use-cases"
    uc_dir.mkdir(parents=True)
    (uc_dir / "registry.jsonl").write_text(
        json.dumps(
            {
                "id": uc_id,
                "minted": "2026-07-18",
                "epic": "epic-login",
                "title": "Login flow",
                "origin": "refine",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (proj / "design" / "specs").mkdir()
    (proj / "design" / "specs" / "login-design.md").write_text(
        f"### {uc_id} — Login flow\nFunctional story here.\nDomain: auth/login\n",
        encoding="utf-8",
    )
    (proj / "design" / "plans").mkdir()
    (proj / "design" / "plans" / "login.md").write_text(
        f"## Use Cases\n| {uc_id} | auth/login | backlog | Login summary |\n",
        encoding="utf-8",
    )
    return tmp_path / "data"


def _seed_autodoc(tmp_path: Path, uc_id: str = "UC-0147") -> Path:
    flows = tmp_path / "autodoc" / "flows"
    flows.mkdir(parents=True)
    (flows / "Login.md").write_text(
        f"---\nuse_cases: [{uc_id}]\n---\n## Use Cases\n| {uc_id} | auth/login | completed | Login |\n",
        encoding="utf-8",
    )
    return tmp_path / "autodoc"


def test_is_valid_uc_id() -> None:
    assert is_valid_uc_id("UC-0147")
    assert is_valid_uc_id("UC-12345")  # width is a floor
    assert not is_valid_uc_id("UC-1")
    assert not is_valid_uc_id("uc-0147")
    assert not is_valid_uc_id("UC-0147x")
    assert not is_valid_uc_id("")


def test_read_registry_row_found_and_missing(tmp_path: Path) -> None:
    data_root = _seed_project(tmp_path)
    reg = data_root / "projects" / "demo" / "design" / "use-cases" / "registry.jsonl"
    row = read_registry_row(reg, "UC-0147")
    assert row is not None
    assert row["title"] == "Login flow"
    assert row["epic"] == "epic-login"
    assert read_registry_row(reg, "UC-9999") is None
    # Missing file degrades to None, never raises.
    assert read_registry_row(tmp_path / "nope.jsonl", "UC-0147") is None


def test_find_touchpoints_groups_refine_solution_flow(tmp_path: Path) -> None:
    data_root = _seed_project(tmp_path)
    autodoc_root = _seed_autodoc(tmp_path)

    report = find_touchpoints(
        "UC-0147",
        data_root=data_root,
        project_name="demo",
        code_repo=None,  # not a git repo in this test → commits/code empty
        autodoc_root=autodoc_root,
    )

    assert report.registry_row["title"] == "Login flow"
    assert len(report.touchpoints["refine"]) == 1
    assert report.touchpoints["refine"][0].location == "login-design.md"
    assert report.touchpoints["refine"][0].line == 1
    assert len(report.touchpoints["solution"]) == 1
    assert len(report.touchpoints["flow"]) == 2  # frontmatter + table row
    # No git repo → commit/code stages are empty, not errored.
    assert report.touchpoints["commits"] == []
    assert report.touchpoints["code"] == []
    assert report.total() == 4


def test_all_stages_present_even_when_empty(tmp_path: Path) -> None:
    report = find_touchpoints("UC-0147")  # everything unresolved
    assert set(report.touchpoints) == set(STAGES)
    assert report.total() == 0
    assert report.registry_row is None


def test_invalid_id_yields_empty_report() -> None:
    report = find_touchpoints("not-an-id", data_root=".", project_name="demo")
    assert report.total() == 0
    assert report.registry_row is None


def test_render_includes_header_and_stages(tmp_path: Path) -> None:
    data_root = _seed_project(tmp_path)
    autodoc_root = _seed_autodoc(tmp_path)
    report = find_touchpoints(
        "UC-0147",
        data_root=data_root,
        project_name="demo",
        autodoc_root=autodoc_root,
    )
    out = render(report)
    assert "UC-0147 — Login flow" in out
    assert "epic: epic-login" in out
    for stage in STAGES:
        assert f"## {stage}" in out


def test_main_always_exits_zero(monkeypatch, tmp_path: Path, capsys) -> None:
    """The advisory contract: main() returns 0 no matter what."""
    # No config home → environment load fails → still exits 0.
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(tmp_path / "no-config"))
    assert main(["UC-0147"]) == 0
    # Malformed id → still 0.
    assert main(["garbage"]) == 0
    # No args → usage, still 0.
    assert main([]) == 0


def test_main_resolves_config_and_prints(monkeypatch, tmp_path: Path, capsys) -> None:
    """End-to-end through main() with a stubbed environment config."""
    data_root = _seed_project(tmp_path)
    autodoc_root = _seed_autodoc(tmp_path)

    class _Env:
        pass

    env = _Env()
    env.data_root = str(data_root)
    env.autodoc_root = str(autodoc_root)

    from agentflow import config as _config

    monkeypatch.setattr(_config, "load_environment", lambda: env)
    monkeypatch.setattr(uci, "_resolve_project_name", lambda *a, **k: "demo")

    def _boom(name):
        raise RuntimeError("no project file in test")

    monkeypatch.setattr(_config, "load_project", _boom)

    rc = main(["UC-0147"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "UC-0147 — Login flow" in out
    assert "refine" in out
def test_main_RAISES_on_an_unusable_autodoc_root_instead_of_degrading(monkeypatch, tmp_path: Path) -> None:
    """THE POINT OF MAKING resolve_autodoc_root RAISE AT ALL.

    Every other config problem in main() degrades to a partial report and exits 0,
    which is the documented advisory contract and is right. This one must not: an
    empty-but-existing root yields a confident `## flow (0)`, and downstream the
    auditor reads a use-case that greps to nothing as positive evidence the code is
    missing - so a silently wrong root manufactures false gap epics. Exiting 0 here
    is worse than crashing.
    """
    from agentflow import config as _config

    data_root = _seed_project(tmp_path)
    legacy = _seed_autodoc(tmp_path)              # the real corpus, where config used to point
    (Path(legacy) / ".manifest.json").write_text("{}", encoding="utf-8")
    scaffolded = tmp_path / "fresh-empty-root"    # passes exists+is_dir, holds nothing
    scaffolded.mkdir()

    class _Env:
        pass

    env = _Env()
    env.data_root = str(data_root)
    env.autodoc_root = str(legacy)

    class _Proj:
        name = "demo"
        autodoc_root = str(scaffolded)

    monkeypatch.setattr(_config, "load_environment", lambda: env)
    monkeypatch.setattr(_config, "load_project", lambda name: _Proj())
    monkeypatch.setattr(uci, "_resolve_project_name", lambda *a, **k: "demo")

    with pytest.raises(_config.AutodocRootError):
        main(["UC-0147"])


def test_main_still_exits_zero_when_the_autodoc_root_is_merely_absent(monkeypatch, tmp_path: Path) -> None:
    """The over-fire control for the test above: an unconfigured root is not an
    error, and the advisory contract still holds for it."""
    from agentflow import config as _config

    data_root = _seed_project(tmp_path)

    class _Env:
        pass

    env = _Env()
    env.data_root = str(data_root)
    env.autodoc_root = None

    monkeypatch.setattr(_config, "load_environment", lambda: env)
    monkeypatch.setattr(uci, "_resolve_project_name", lambda *a, **k: None)
    assert main(["UC-0147"]) == 0
