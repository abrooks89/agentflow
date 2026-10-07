"""The auditor's declared-dependency check for a product with no interpreter set.

With `canonical_repo.python` unset the audit probe runs under the default `python`, which in a
fleet session is the agentflow suite venv. A probe that then fails on a missing third-party
import reads like a product regression. The auditor stays read-only (no scratch venv, no
install), so instead Step 2 checks once per pass whether the product's declared distributions
are present under the default `python`, and the audit contract turns every Python probe into
the "probe cannot run" blocker when any are missing. These tests pin the helper, its CLI and
the prose that carries its result to the subagents.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentflow import config
from agentflow.config import ConfigError, declared_deps_check, requirement_name

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = REPO_ROOT / "skills" / "auditor" / "references" / "audit-agent.md"
SKILL = REPO_ROOT / "skills" / "auditor" / "SKILL.md"

MISSING = "agentflow-nonexistent-dist-xyz"
PRESENT_EVIDENCE = "product interpreter unset; ran under default python (declared deps present)"
NONE_DECLARED = "(no dependencies declared in pyproject.toml [project] or requirements.txt)"
REMEDY = "set canonical_repo.python in projects/<name>.md"
NO_VERSIONS = "versions are not compared"


def _pyproject(repo: Path, deps: list[str] | None, *, project: bool = True) -> None:
    lines = ["[build-system]", 'requires = ["setuptools"]', ""]
    if project:
        lines += ["[project]", 'name = "scratch"', 'version = "0.0.1"']
        if deps is not None:
            lines.append("dependencies = [" + ", ".join(json.dumps(d) for d in deps) + "]")
    (repo / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


# --------------------------------------------------------------------------- #
# The three scratch-repo cases the epic names
# --------------------------------------------------------------------------- #
def test_a_missing_declared_distribution_is_reported_and_blocks(tmp_path):
    _pyproject(tmp_path, ["PyYAML>=6", MISSING + ">=1.0"])
    result = declared_deps_check(tmp_path)
    assert result.missing == (MISSING,)
    assert result.source == "pyproject.toml [project].dependencies"
    assert result.declared_count == 2
    assert result.blocks
    assert MISSING in result.blocked_on
    assert REMEDY in result.blocked_on
    assert "probe cannot run" in result.blocked_on


def test_only_pyyaml_declared_runs_with_nothing_missing(tmp_path):
    _pyproject(tmp_path, ["PyYAML"])
    result = declared_deps_check(tmp_path)
    assert result.missing == ()
    assert result.declared_count == 1
    assert not result.blocks
    assert result.blocked_on is None
    assert PRESENT_EVIDENCE in result.evidence
    assert NO_VERSIONS in result.evidence


def test_no_project_table_and_no_requirements_names_what_was_read(tmp_path):
    _pyproject(tmp_path, None, project=False)
    result = declared_deps_check(tmp_path)
    assert result.source is None
    assert result.declared_count == 0
    assert result.missing == ()
    assert not result.blocks
    assert NONE_DECLARED in result.evidence
    assert result.evidence.startswith("product interpreter unset; ran under default python")


def test_an_empty_repo_is_the_none_declared_case(tmp_path):
    result = declared_deps_check(tmp_path)
    assert result.source is None and not result.blocks
    assert NONE_DECLARED in result.evidence


# --------------------------------------------------------------------------- #
# Source selection
# --------------------------------------------------------------------------- #
def test_pyproject_dependencies_win_over_requirements(tmp_path):
    _pyproject(tmp_path, ["PyYAML"])
    (tmp_path / "requirements.txt").write_text(MISSING + "\n", encoding="utf-8")
    result = declared_deps_check(tmp_path)
    assert result.source == "pyproject.toml [project].dependencies"
    assert result.missing == ()


def test_requirements_are_read_when_project_declares_no_dependencies(tmp_path):
    _pyproject(tmp_path, None)  # [project] present, no dependencies key
    (tmp_path / "requirements.txt").write_text("pyyaml\n" + MISSING + "\n", encoding="utf-8")
    result = declared_deps_check(tmp_path)
    assert result.source == "requirements.txt"
    assert result.declared_count == 2
    assert result.missing == (MISSING,)


def test_names_compare_under_pep_503_normalization(tmp_path):
    _pyproject(tmp_path, ["py_YAML.x"])
    installed = {"py-yaml-x"}
    assert declared_deps_check(tmp_path, installed=installed).missing == ()
    assert declared_deps_check(tmp_path, installed={"pyyaml"}).missing == ("py_YAML.x",)


def test_duplicate_declarations_count_once(tmp_path):
    (tmp_path / "requirements.txt").write_text("PyYAML\npyyaml>=6\n", encoding="utf-8")
    assert declared_deps_check(tmp_path).declared_count == 1


def test_a_malformed_pyproject_fails_loudly(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project\nname=", encoding="utf-8")
    with pytest.raises(ConfigError):
        declared_deps_check(tmp_path)


def test_a_missing_repo_root_fails_loudly(tmp_path):
    with pytest.raises(ConfigError):
        declared_deps_check(tmp_path / "nope")


# --------------------------------------------------------------------------- #
# requirements.txt line parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "line, name",
    [
        ("requests", "requests"),
        ("requests[security,socks]>=2.0", "requests"),
        ("Django==4.2.1", "Django"),
        ("numpy~=1.26 ; python_version >= '3.10'", "numpy"),
        ('pywin32; sys_platform == "win32"', "pywin32"),
        ("flask >= 2, < 4", "flask"),
        ("pkg @ https://example.invalid/pkg-1.0.tar.gz", "pkg"),
        ("zope.interface  # pinned below", "zope.interface"),
        ("  six  ", "six"),
        ("a", "a"),
    ],
)
def test_requirement_name_extracts_the_distribution(line, name):
    assert requirement_name(line) == name


@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "# a comment",
        "-r other.txt",
        "--requirement other.txt",
        "-e .",
        "-e git+https://example.invalid/x.git#egg=x",
        "--index-url https://example.invalid/simple",
        "-c constraints.txt",
        "git+https://example.invalid/x.git#egg=x",
        "./local/path",
        "https://example.invalid/pkg.whl",
        "foo-1.0-py3-none-any.whl",
        "pkg.tar.gz",
        "Pkg-2.0.ZIP",
    ],
)
def test_requirement_name_skips_non_requirement_lines(line):
    assert requirement_name(line) is None


def test_requirements_file_with_mixed_lines(tmp_path):
    (tmp_path / "base.txt").write_text("# included, declares nothing\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(
        "\n".join([
            "# top comment",
            "-r base.txt",
            "-e .",
            "",
            "PyYAML[extra]>=6  # yaml",
            f"{MISSING}==1.0; python_version >= '3.8'",
        ]) + "\n",
        encoding="utf-8",
    )
    result = declared_deps_check(tmp_path)
    assert result.declared_count == 2
    assert result.missing == (MISSING,)


# --------------------------------------------------------------------------- #
# requirements.txt includes: -r is followed, -c is not
# --------------------------------------------------------------------------- #
def _write(path: Path, *lines: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    "include", ["-r base.txt", "-rbase.txt", "--requirement base.txt", "--requirement=base.txt"],
)
def test_a_delegating_requirements_file_checks_the_included_deps(tmp_path, include):
    _write(tmp_path / "requirements.txt", include)
    _write(tmp_path / "base.txt", "PyYAML", MISSING)
    result = declared_deps_check(tmp_path)
    assert result.source == "requirements.txt"
    assert result.declared_count == 2
    assert result.missing == (MISSING,)
    assert PRESENT_EVIDENCE in result.evidence


def test_a_nested_include_chain_resolves_against_each_including_file(tmp_path):
    _write(tmp_path / "requirements.txt", "-r reqs/prod.txt")
    _write(tmp_path / "reqs" / "prod.txt", "-r common/base.txt", "PyYAML")
    _write(tmp_path / "reqs" / "common" / "base.txt", MISSING)
    result = declared_deps_check(tmp_path)
    assert result.source == "requirements.txt"
    assert result.declared_count == 2
    assert result.missing == (MISSING,)


def test_an_include_cycle_terminates(tmp_path):
    _write(tmp_path / "requirements.txt", "-r a.txt")
    _write(tmp_path / "a.txt", "-r b.txt", "PyYAML")
    _write(tmp_path / "b.txt", "-r a.txt", "-r requirements.txt", MISSING)
    result = declared_deps_check(tmp_path)
    assert result.declared_count == 2
    assert result.missing == (MISSING,)


def test_a_missing_include_fails_loudly_naming_it(tmp_path, capsys):
    _write(tmp_path / "requirements.txt", "PyYAML", "-r nope.txt")
    with pytest.raises(ConfigError) as exc:
        declared_deps_check(tmp_path)
    assert "nope.txt" in str(exc.value)
    assert config.main(["declared-deps", "--repo", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "nope.txt" in captured.err


def test_a_remote_include_fails_loudly(tmp_path):
    _write(tmp_path / "requirements.txt", "-r https://example.invalid/base.txt")
    with pytest.raises(ConfigError) as exc:
        declared_deps_check(tmp_path)
    assert "remote" in str(exc.value)


def test_a_constraints_file_is_not_followed(tmp_path):
    _write(tmp_path / "requirements.txt", "-c constraints.txt", "--constraint=more.txt", "PyYAML")
    _write(tmp_path / "constraints.txt", MISSING + "==1.0")
    # more.txt does not exist: following it would raise.
    result = declared_deps_check(tmp_path)
    assert result.declared_count == 1
    assert result.missing == ()


# --------------------------------------------------------------------------- #
# Environment markers: a requirement false on this interpreter is not declared here
# --------------------------------------------------------------------------- #
ENV = {
    "sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
    "python_version": "3.12", "platform_machine": "x86_64", "implementation_name": "cpython",
}


@pytest.fixture
def linux_env(monkeypatch):
    monkeypatch.setattr(config, "_marker_environment", lambda: dict(ENV))


def test_a_false_marker_is_excluded_from_requirements(tmp_path, linux_env):
    _write(tmp_path / "requirements.txt", "PyYAML", f'{MISSING}; sys_platform == "win32"')
    result = declared_deps_check(tmp_path)
    assert result.declared == ("PyYAML",)
    assert result.declared_count == 1
    assert result.missing == ()
    assert not result.blocks


def test_a_true_marker_is_included(tmp_path, linux_env):
    _write(tmp_path / "requirements.txt", f"{MISSING}; sys_platform == 'linux'")
    result = declared_deps_check(tmp_path)
    assert result.missing == (MISSING,)


def test_markers_against_the_real_interpreter(tmp_path):
    _write(
        tmp_path / "requirements.txt",
        f'{MISSING}-a; sys_platform == "nonexistent"',
        f'{MISSING}-b; sys_platform != "nonexistent"',
    )
    assert declared_deps_check(tmp_path).missing == (MISSING + "-b",)


def test_a_marker_in_pyproject_dependencies(tmp_path, linux_env):
    _pyproject(tmp_path, ["PyYAML", f"{MISSING}; os_name == 'nt'", f"{MISSING}-b; os_name == 'posix'"])
    result = declared_deps_check(tmp_path)
    assert result.source == "pyproject.toml [project].dependencies"
    assert result.declared_count == 2
    assert result.missing == (MISSING + "-b",)


def test_a_direct_reference_keeps_its_marker(tmp_path, linux_env):
    _write(tmp_path / "requirements.txt",
           f"{MISSING} @ https://example.invalid/x.tar.gz ; sys_platform == 'win32'")
    assert declared_deps_check(tmp_path).declared_count == 0


@pytest.mark.parametrize(
    "marker, applies",
    [
        ('python_version >= "3.0"', True),
        ('python_version < "3.0"', False),
        ("python_version == '3.12.0'", True),
        ("python_version > '3.9'", True),
        ("'3.13' <= python_version", False),
        ("'win32' == sys_platform", False),
        ("platform_system != 'Windows'", True),
        ("platform_machine == 'x86_64' and implementation_name == 'cpython'", True),
        ("os_name == 'posix' and sys_platform == 'win32'", False),
        ("sys_platform == 'win32' or sys_platform == 'linux'", True),
        ("sys_platform == 'win32' or sys_platform == 'darwin'", False),
        # and binds tighter than or
        ("sys_platform == 'linux' or sys_platform == 'win32' and os_name == 'nt'", True),
        ("sys_platform == 'win32' and os_name == 'nt' or os_name == 'posix'", True),
        ("(sys_platform == 'linux' or sys_platform == 'win32') and os_name == 'nt'", False),
        ("(sys_platform == 'win32' or os_name == 'posix') and python_version >= '3.8'", True),
    ],
)
def test_marker_grammar(marker, applies):
    assert config.marker_applies(marker, ENV) is applies


@pytest.mark.parametrize(
    "marker",
    [
        "extra == 'test'",
        "sys_platform in 'win32 cygwin'",
        "sys_platform not in 'win32'",
        "platform_release == '5.0'",
        "sys_platform ~= 'win32'",
        "sys_platform === 'win32'",
        "sys_platform < 'win32'",
        "python_version >= '3.x'",
        "sys_platform == 'win32' and",
        "(sys_platform == 'win32'",
        "sys_platform == 'win32')",
        "sys_platform == win32",
        "sys_platform == 'win32' and extra == 'x'",
        "%%% garbage",
    ],
)
def test_an_unparseable_marker_counts_as_applicable(marker):
    assert config.marker_applies(marker, ENV) is True


def test_an_unparseable_marker_keeps_the_requirement(tmp_path, linux_env):
    _write(tmp_path / "requirements.txt", f"{MISSING}; extra == 'test' and sys_platform == 'win32'")
    assert declared_deps_check(tmp_path).missing == (MISSING,)


@pytest.mark.parametrize(
    "line, marker",
    [
        ("pkg", None),
        ("pkg ; ", None),
        ('pywin32; sys_platform == "win32"', 'sys_platform == "win32"'),
        ("pkg>=1; python_version >= '3.8'  # note", "python_version >= '3.8'"),
        ("pkg==1; os_name == 'nt' --hash=sha256:abc", "os_name == 'nt'"),
        ("pkg @ https://example.invalid/a;b.tar.gz", None),
        ("pkg @ https://example.invalid/x.tar.gz ; os_name == 'nt'", "os_name == 'nt'"),
    ],
)
def test_requirement_marker_extracts_the_marker(line, marker):
    assert config.requirement_marker(line) == marker


# --------------------------------------------------------------------------- #
# CLI: python -m agentflow.config declared-deps --repo <repo_root>
# --------------------------------------------------------------------------- #
def test_cli_missing_exits_1_with_blocker(tmp_path, capsys):
    _pyproject(tmp_path, [MISSING])
    assert config.main(["declared-deps", "--repo", str(tmp_path)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["missing"] == [MISSING]
    assert out["declared_count"] == 1
    assert out["source"] == "pyproject.toml [project].dependencies"
    assert REMEDY in out["blocked_on"]


def test_cli_present_exits_0_with_evidence(tmp_path, capsys):
    _pyproject(tmp_path, ["PyYAML"])
    assert config.main(["declared-deps", "--repo", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["missing"] == []
    assert out["blocked_on"] is None
    assert PRESENT_EVIDENCE in out["evidence"]
    assert NO_VERSIONS in out["evidence"]


def test_cli_none_declared_exits_0(tmp_path, capsys):
    assert config.main(["declared-deps", "--repo", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["source"] is None
    assert NONE_DECLARED in out["evidence"]


def test_cli_unreadable_declaration_exits_2(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    assert config.main(["declared-deps", "--repo", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "remediation:" in captured.err


@pytest.mark.parametrize("body", ["project = 1\n", 'project = "x"\n'])
def test_cli_non_table_project_exits_2_not_missing(tmp_path, capsys, body):
    # Exit 1 means "declared deps missing"; a malformed file must not masquerade as that.
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    assert config.main(["declared-deps", "--repo", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[project] is not a table" in captured.err


def test_cli_requires_repo(capsys):
    with pytest.raises(SystemExit) as exc:
        config.main(["declared-deps"])
    assert exc.value.code == 2


def test_this_repo_root_declares_nothing():
    """agentflow's own profile is unaffected: no declaration file at the repo root."""
    assert not (REPO_ROOT / "pyproject.toml").exists()
    assert not (REPO_ROOT / "requirements.txt").exists()
    assert declared_deps_check(REPO_ROOT).source is None


# --------------------------------------------------------------------------- #
# Prose pins: Step 2 runs the check and passes it; the contract reads it
# --------------------------------------------------------------------------- #
def _step2() -> str:
    return _section(SKILL.read_text(encoding="utf-8"), "## Step 2 ", "## Step 4 ")


def _probes() -> str:
    return _section(CONTRACT.read_text(encoding="utf-8"),
                    "### `test-suite` / `smoke-cli`", "### `none`")


def test_step_2_runs_the_check_once_and_passes_it():
    step2 = _step2()
    assert "- `{declared_deps}`" in step2
    assert "python -m agentflow.config declared-deps --repo" in step2
    assert "once per pass" in step2


def test_contract_declares_the_new_input():
    text = CONTRACT.read_text(encoding="utf-8")
    context = _section(text, "Caller-supplied context", "## Part 1")
    assert re.search(r"^- `\{declared_deps\}`", context, re.MULTILINE)


def test_contract_null_rule_reads_the_result():
    probes = _probes()
    assert "{declared_deps}" in probes
    assert PRESENT_EVIDENCE in probes
    assert NONE_DECLARED in probes
    assert REMEDY in probes
    assert NO_VERSIONS in probes
    assert "never `satisfied`" in probes


def test_contract_part_3_names_missing_declared_distributions():
    part3 = _section(CONTRACT.read_text(encoding="utf-8"), "## Part 3", "## Return JSON")
    assert "{declared_deps}" in part3
    assert REMEDY in part3


def test_step_2_carries_strings_and_remedy():
    step2 = _step2()
    assert REMEDY in step2
    assert NO_VERSIONS in step2
    assert PRESENT_EVIDENCE in step2
    assert NONE_DECLARED in step2


def test_deeply_nested_parentheses_count_as_applicable():
    """A marker too deep for the recursive evaluator is malformed input, not a crash."""
    deep = "(" * 2000 + "os_name == 'nonexistent'" + ")" * 2000
    assert config.marker_applies(deep) is True
