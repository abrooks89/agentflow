"""Tests for the advisory use-case impact / touchpoint tool (Phase 4 T10).

Covers: registry-row lookup, plain-Python grep over refine/solution/flow,
stage grouping, id validation, graceful degradation when roots are absent,
and the load-bearing contract — the CLI exits 0 and never edits, with one
exception: an unusable autodoc root refuses with exit 2 and a clean stderr message.
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
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
    """The advisory contract: main() returns 0 for every degraded input."""
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
def _unusable_autodoc_setup(tmp_path: Path):
    """An env + project whose resolved autodoc root is empty while the legacy root holds a corpus."""
    data_root = _seed_project(tmp_path)
    legacy = _seed_autodoc(tmp_path)              # the real corpus, where config used to point
    (Path(legacy) / ".manifest.json").write_text("{}", encoding="utf-8")
    scaffolded = tmp_path / "fresh-empty-root"    # passes exists+is_dir, holds nothing
    scaffolded.mkdir()
    return data_root, legacy, scaffolded


def test_main_REFUSES_an_unusable_autodoc_root_instead_of_degrading(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """THE POINT OF MAKING resolve_autodoc_root RAISE AT ALL.

    Every other config problem in main() degrades to a partial report and exits 0,
    which is the documented advisory contract and is right. This one must not: an
    empty-but-existing root yields a confident `## flow (0)`, and downstream the
    auditor reads a use-case that greps to nothing as positive evidence the code is
    missing - so a silently wrong root manufactures false gap epics. Exiting 0 here
    is worse than crashing, so main() refuses with exit 2, prints the error's own
    remediation text to stderr, and prints no report at all.
    """
    from agentflow import config as _config

    data_root, legacy, scaffolded = _unusable_autodoc_setup(tmp_path)

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

    assert main(["UC-0147"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "", "a refused run must not print a (zero) report"
    assert "autodoc root" in captured.err
    assert "remediation:" in captured.err
    assert scaffolded.name in captured.err  # the message names the root (repr'd, so no full-path match)


def test_cli_unusable_autodoc_root_exits_2_with_a_clean_message(tmp_path: Path) -> None:
    """The same refusal through the module's real `__main__` shim: a nonzero exit, the
    remediation on stderr, and no uncaught traceback for a seat to mistake for a tool bug."""
    data_root, legacy, scaffolded = _unusable_autodoc_setup(tmp_path)
    shared = Path(uci.__file__).resolve().parents[1]
    # The stubs stand in for a real config home; runpy then runs the module's own
    # `__main__` shim, so an exception main() lets escape prints a real traceback.
    bootstrap = textwrap.dedent(
        f"""
        import runpy, sys, types
        from agentflow import config as c
        env = types.SimpleNamespace(data_root={str(data_root)!r}, autodoc_root={str(legacy)!r})
        proj = types.SimpleNamespace(name="demo", autodoc_root={str(scaffolded)!r},
                                     canonical_repo=types.SimpleNamespace(local_checkout=None))
        c.load_environment = lambda: env
        c.load_project = lambda name: proj
        c._config_home = lambda: {str(tmp_path / "no-config")!r}
        sys.argv = ["uc-touchpoints", "UC-0147", "demo"]
        runpy.run_module("agentflow.use_case_impact", run_name="__main__")
        """
    )
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(shared)
    result = subprocess.run(
        [sys.executable, "-c", bootstrap], cwd=tmp_path, env=child_env,
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "Traceback" not in result.stderr, result.stderr
    assert "autodoc root" in result.stderr and "remediation:" in result.stderr, result.stderr
    assert result.stdout == "", result.stdout


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


# --------------------------------------------------------------------------- #
# git output is decoded as UTF-8 whatever the host locale
# --------------------------------------------------------------------------- #
# U+0141's UTF-8 encoding holds byte 0x81, which cp1252 leaves unmapped and ASCII rejects.
_NON_ASCII = "\u0141\u00f3d\u017a \u2014 caf\u00e9"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )


def _seed_non_ascii_repo(tmp_path: Path) -> Path:
    """A non-ASCII grep hit in a non-ASCII-named file, an ASCII hit, and a trailer commit
    with a non-ASCII subject."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / f"{_NON_ASCII}a.md").write_text(f"UC-0007 {_NON_ASCII}\n", encoding="utf-8")
    (repo / "b.md").write_text("plain UC-0007 line\n", encoding="utf-8")
    _git(repo, "add", "-A")
    msg = tmp_path / "msg.txt"
    # Through a file, never argv: a Windows argv passes the subject through the ANSI code page.
    msg.write_bytes(f"Add {_NON_ASCII}\n\nUse-Case: UC-0007\n".encode("utf-8"))
    _git(repo, "commit", "-q", "-F", str(msg))
    return repo


def _assert_git_stages(stages: dict) -> None:
    code = sorted((t["location"], t["text"]) for t in stages["code"])
    assert code == sorted([(f"{_NON_ASCII}a.md", f"UC-0007 {_NON_ASCII}"),
                           ("b.md", "plain UC-0007 line")]), code
    commits = [t["text"] for t in stages["commits"]]
    assert commits == [f"Add {_NON_ASCII}"], commits


def test_git_stages_survive_a_non_utf8_default_decoder(monkeypatch, tmp_path: Path) -> None:
    """Deterministic on every OS: emulate a cp1252 locale by making cp1252 the decoder a
    text-mode subprocess.run call gets when it names no encoding. A git read that leans on
    the locale default then raises or mojibakes; one that asks for UTF-8 is untouched."""
    repo = _seed_non_ascii_repo(tmp_path)
    real_run = subprocess.run

    def _cp1252_default_run(*args, **kwargs):
        if kwargs.get("text") or kwargs.get("universal_newlines"):
            kwargs.setdefault("encoding", "cp1252")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(uci.subprocess, "run", _cp1252_default_run)
    report = find_touchpoints("UC-0007", code_repo=repo)
    _assert_git_stages(
        {s: [{"location": t.location, "text": t.text} for t in report.touchpoints[s]]
         for s in ("code", "commits")}
    )


def test_git_stages_survive_a_real_non_utf8_locale(tmp_path: Path) -> None:
    """The real thing, in a child interpreter whose locale encoding is not UTF-8: UTF-8 mode
    off everywhere, and on POSIX the C locale with locale coercion off, so the preferred
    encoding is ASCII. On Windows the ANSI code page decides (cp1252 on a Western host)."""
    repo = _seed_non_ascii_repo(tmp_path)
    shared = Path(uci.__file__).resolve().parents[1]
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(shared)
    child_env["PYTHONUTF8"] = "0"
    child_env.pop("PYTHONIOENCODING", None)
    if os.name != "nt":
        child_env.update(LC_ALL="C", LANG="C", PYTHONCOERCECLOCALE="0")
    script = textwrap.dedent(
        f"""
        import json, locale
        from agentflow.use_case_impact import find_touchpoints
        r = find_touchpoints("UC-0007", code_repo={str(repo)!r})
        print(json.dumps({{
            "encoding": locale.getpreferredencoding(False),
            "code": [{{"location": t.location, "text": t.text}} for t in r.touchpoints["code"]],
            "commits": [{{"location": t.location, "text": t.text}} for t in r.touchpoints["commits"]],
        }}, ensure_ascii=True))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=child_env,
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    if payload["encoding"].lower().replace("-", "") in ("utf8", "cp65001"):
        pytest.skip(f"locale encoding here is {payload['encoding']}; the emulated test covers it")
    assert "UnicodeDecodeError" not in result.stderr, result.stderr
    _assert_git_stages(payload)


def test_command_doc_names_its_single_nonzero_exit() -> None:
    """A seat reads the command doc, not this module: it must learn there that exit 2 is the
    autodoc-root refusal to fix in config, not a tool bug to retry."""
    doc = Path(uci.__file__).resolve().parents[3] / "commands" / "uc-touchpoints.md"
    text = doc.read_text(encoding="utf-8")
    assert "AutodocRootError" in text
    assert "exits 2" in text
    assert "always exits 0" not in text


@pytest.mark.parametrize("zero", [0x0660, 0xFF10], ids=["arabic-indic", "fullwidth"])
def test_a_uc_id_spelled_in_NON_ASCII_digits_is_not_a_uc_id(zero: int) -> None:
    """gh#1123: `\\d` takes any Unicode decimal digit; a UC id takes ASCII digits only."""
    spelled = "".join(chr(zero + int(c)) for c in "0147")
    assert not is_valid_uc_id(f"UC-{spelled}")
    assert not is_valid_uc_id(f"UC-014{spelled[-1]}")
    assert is_valid_uc_id("UC-0147"), "control"
