"""The acceptance drill's env-strip must key on the RESOLVED notification names (gh#227).

`_run_registry_cli` hands the drill child a copy of the parent environment with the
notification variables removed, on the stated rule that a stray fleet ping must never
leave the box from an acceptance check. That strip named `NTFY_URL` / `NTFY_TOPIC` as
literals, and gh#133 (#226) made `notify()` honour `notify.url_env` / `notify.topic_env`
instead — so on an install that renamed the pair the guard passed every test, stripped
two variables nothing reads, and handed the child the live ones.

TWO THINGS THIS MODULE IS CAREFUL ABOUT, because they are what make it a real gate:

- **A test that uses the default names passes on the broken code.** The literal strip
  removes `NTFY_URL` / `NTFY_TOPIC` perfectly well; that is the whole reason the defect
  is invisible to everyone running a default install. So the resolved-name case below
  configures a RENAMED pair and asserts on those names — assert on the defaults and this
  file would be green against the code it exists to catch.
- **The fallback is asserted separately.** `_notify_env_names` degrades to the defaults
  when the config cannot be resolved, which is what `notify()` itself does. That
  degradation is a behaviour, not an accident, so it gets its own test rather than being
  left to the resolved case's coincidence.

SEVERITY, MEASURED RATHER THAN INHERITED FROM THE ISSUE: gh#227 says "a ping raised
inside the drill would be delivered for real". Not today — the registry CLI's only
`notify()` call sits behind `_send_halted_ping`, reached only when `command == "status"`
and the write is a transition into `halted`, and no drill runs `status`. This is a
defence-in-depth guard whose invariant is currently vacuous. It is worth repairing
precisely because it is vacuous: nothing would go red on the day a drill first exercises
a halt transition through renamed variables.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO_ROOT / "harness"
CLI_ROOT = REPO_ROOT / "skills" / "_shared"

ALT_URL_ENV = "AF_DRILL_ALT_BASE"
ALT_TOPIC_ENV = "AF_DRILL_ALT_CHANNEL"


def _load_check_module():
    """Import harness/check.py by path — it is a script, not a package module."""
    path = HARNESS_DIR / "check.py"
    # FAIL, never skip: if the harness moves, this gate must break loudly rather
    # than quietly stop covering the strip.
    assert path.is_file(), f"harness/check.py not found at {path} - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location("agentflow_harness_check_envstrip", path)
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: check.py defines dataclasses, and @dataclass resolves
    # its own module out of sys.modules at class-creation time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check():
    return _load_check_module()


def _write_config(home: Path, *, names: bool) -> None:
    """A real environment.md, loaded by the real resolver.

    Deliberately not a stubbed `_resolved_notify`: the claim under test is that the
    strip reads what an OPERATOR's config says, so a stub would assert the test's own
    idea of resolution rather than the shipped one.
    """
    home.mkdir(parents=True, exist_ok=True)
    d = str(home).replace("\\", "/")
    env_names = f"  url_env: {ALT_URL_ENV}\n  topic_env: {ALT_TOPIC_ENV}\n" if names else ""
    fence = "```"
    (home / "environment.md").write_text(
        f"# environment\n\n{fence}yaml\n"
        "schema_version: 1\n"
        "person:\n  name: Sample User\n"
        f"data_root: {d}\n"
        "git_host:\n  kind: local\n"
        f"notify:\n  kind: ntfy\n{env_names}"
        f"lock_dir: {d}\n"
        "shell: posix\n"
        f"suite_root: {d}\n"
        f"worktree_root: {d}\n"
        f"{fence}\n",
        encoding="utf-8",
    )


@pytest.fixture
def captured_env(check, monkeypatch):
    """Run `_run_registry_cli` with a fake `subprocess.run` and hand back the child env.

    The child is never launched. What is under test is the environment the drill BUILDS,
    and building it is the only part a real subprocess would not make more observable.
    """
    seen: dict = {}

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(_argv, **kwargs):
        seen["env"] = dict(kwargs["env"])
        return _Completed()

    monkeypatch.setattr(check.subprocess, "run", _fake_run)

    def _go() -> dict:
        rc, _out, _err, fail = check._run_registry_cli(CLI_ROOT, ["read", "--help"])
        assert fail == "", f"the helper reported a launch failure: {fail}"
        assert rc == 0
        return seen["env"]

    return _go


def test_the_strip_removes_the_names_the_operators_config_actually_uses(
    tmp_path, monkeypatch, captured_env
):
    """THE REGRESSION. Renamed pair exported, config naming it, and the child gets neither.

    This is the case the literal strip fails: `AF_DRILL_ALT_BASE` is what `notify()`
    would read on this config, and the pre-fix code never looked at it.
    """
    home = tmp_path / "cfg-renamed"
    _write_config(home, names=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.setenv(ALT_URL_ENV, "https://renamed-host.invalid")
    monkeypatch.setenv(ALT_TOPIC_ENV, "renamed-channel")

    env = captured_env()

    assert ALT_URL_ENV not in env, (
        "the drill child inherited the host notify() would actually read - "
        "the strip is matching literals rather than the resolved names"
    )
    assert ALT_TOPIC_ENV not in env


def test_the_defaults_are_stripped_even_when_the_config_names_others(
    tmp_path, monkeypatch, captured_env
):
    """Resolved names do not REPLACE the defaults, they are added to them.

    `notify()` falls back to the defaults whenever the config fails to resolve, and the
    child resolves config independently of the parent. Strip only the resolved pair and
    a child whose own resolution fails reads the defaults the parent left in place.
    """
    home = tmp_path / "cfg-renamed-both"
    _write_config(home, names=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.setenv(ALT_URL_ENV, "https://renamed-host.invalid")
    monkeypatch.setenv(ALT_TOPIC_ENV, "renamed-channel")
    monkeypatch.setenv("NTFY_URL", "https://default-host.invalid")
    monkeypatch.setenv("NTFY_TOPIC", "default-channel")

    env = captured_env()

    for var in (ALT_URL_ENV, ALT_TOPIC_ENV, "NTFY_URL", "NTFY_TOPIC"):
        assert var not in env, f"{var} reached the drill child"


def test_an_unresolvable_config_still_strips_the_defaults(tmp_path, monkeypatch, captured_env):
    """The degradation is a behaviour, so it is asserted rather than assumed.

    An empty config home makes `load_environment()` raise. The strip must fall back to
    the pair `notify()` itself falls back to — stripping fewer names than that would make
    a broken config quieter AND less safe at the same time.
    """
    empty = tmp_path / "cfg-absent"
    empty.mkdir()
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(empty))
    monkeypatch.setenv("NTFY_URL", "https://default-host.invalid")
    monkeypatch.setenv("NTFY_TOPIC", "default-channel")

    env = captured_env()

    assert "NTFY_URL" not in env and "NTFY_TOPIC" not in env


def test_a_live_registry_path_is_still_stripped(tmp_path, monkeypatch, captured_env):
    """The strip's other half, unchanged by gh#227 and easy to lose in a refactor:
    this drill WRITES, so it must never inherit a path to a live fleet registry."""
    home = tmp_path / "cfg-registry"
    _write_config(home, names=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.setenv("AGENTFLOW_REGISTRY", str(tmp_path / "live-registry.md"))

    env = captured_env()

    assert "AGENTFLOW_REGISTRY" not in env


def test_the_resolver_reports_the_names_rather_than_the_values(tmp_path, monkeypatch, check):
    """`_notify_env_names` returns NAMES. A helper that returned values would put a
    notification URL into anything that logged its result, and the strip needs keys."""
    home = tmp_path / "cfg-names-only"
    _write_config(home, names=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.setenv(ALT_URL_ENV, "https://renamed-host.invalid")
    monkeypatch.setenv(ALT_TOPIC_ENV, "renamed-channel")

    names = check._notify_env_names(CLI_ROOT)

    assert set(names) == {"NTFY_URL", "NTFY_TOPIC", ALT_URL_ENV, ALT_TOPIC_ENV}
    assert not any("://" in n for n in names), "these are variable names, never their values"


# --- gh#411: the auditor-scenario driver is a writing child too ---------------------------


@pytest.fixture
def scenario_env(check, monkeypatch):
    """Run `run_auditor_scenario` with a fake `subprocess.run` and hand back the child env.

    The driver is never launched, so the gate itself reports FAIL (no rows) - that verdict
    is not what is under test. What is under test is the environment it would have been
    handed.
    """
    seen: dict = {}

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(_argv, **kwargs):
        seen["env"] = dict(kwargs["env"])
        return _Completed()

    monkeypatch.setattr(check.subprocess, "run", _fake_run)

    def _go() -> dict:
        check.run_auditor_scenario()
        assert "env" in seen, "run_auditor_scenario never launched its driver"
        return seen["env"]

    return _go


def test_the_auditor_scenario_child_inherits_no_notify_host_and_no_registry(
    tmp_path, monkeypatch, scenario_env
):
    """THE gh#411 REGRESSION. The driver writes `registry status`, and a transition into
    `halted` pings for real - so it must get the drill's stripped environment, renamed
    names included, and not the operator's."""
    home = tmp_path / "cfg-scenario"
    _write_config(home, names=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.setenv(ALT_URL_ENV, "https://renamed-host.invalid")
    monkeypatch.setenv(ALT_TOPIC_ENV, "renamed-channel")
    monkeypatch.setenv("NTFY_URL", "https://default-host.invalid")
    monkeypatch.setenv("NTFY_TOPIC", "default-channel")
    monkeypatch.setenv("AGENTFLOW_REGISTRY", str(tmp_path / "live-registry.md"))

    env = scenario_env()

    for var in (ALT_URL_ENV, ALT_TOPIC_ENV, "NTFY_URL", "NTFY_TOPIC", "AGENTFLOW_REGISTRY"):
        assert var not in env, f"{var} reached the auditor-scenario driver"


def test_the_auditor_scenario_child_still_gets_its_own_tree_first(
    monkeypatch, scenario_env
):
    """The strip must not cost the driver the import path that makes it grade THIS
    checkout rather than an installed one (the reason it runs as a subprocess)."""
    monkeypatch.setenv("PYTHONPATH", "prior-entry")

    env = scenario_env()

    entries = env["PYTHONPATH"].split(os.pathsep)
    assert Path(entries[0]) == CLI_ROOT, entries
    assert entries[-1] == "prior-entry", "an existing PYTHONPATH was dropped, not extended"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


#: Every function in check.py allowed to copy the parent environment directly, and why.
#: A new entry here is a claim that the child it launches cannot write through the
#: registry CLI - make it in review, not by default.
_SPAWNS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

_DIRECT_ENV_COPIES = {
    "_isolated_child_env": "the shared strip itself",
    "c13_slash_argument_beats_injected_default": (
        "points AGENTFLOW_CONFIG at a temp home and runs only the read-only `config show`"
    ),
}


def test_every_parent_env_copy_in_check_py_is_accounted_for():
    """THE CENSUS. gh#411 existed because three call sites each built their own child
    environment and only one remembered to strip it. This walks the file for every
    `dict(os.environ)` / `os.environ.copy()` / `{**os.environ}` and fails on one outside
    the allow-list, and on any subprocess spawn with no `env=` at all, so a fourth site
    cannot quietly repeat the miss. Keyed by function, so it counts sites, not copies."""
    tree = ast.parse((HARNESS_DIR / "check.py").read_text(encoding="utf-8"))

    def _is_environ(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    class _Census(ast.NodeVisitor):
        """Attribute each copy to its INNERMOST enclosing function, module level as '<module>'."""

        def __init__(self) -> None:
            self.scope: list[str] = []
            self.found: dict[str, int] = {}
            self.inherits: list[int] = []

        def _enter(self, node) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = _enter
        visit_AsyncFunctionDef = _enter

        def visit_Call(self, node: ast.Call) -> None:
            f = node.func
            copies = (
                isinstance(f, ast.Name) and f.id == "dict"
                and len(node.args) == 1 and _is_environ(node.args[0])
            ) or (
                isinstance(f, ast.Attribute) and f.attr == "copy" and _is_environ(f.value)
            )
            if copies:
                self.found[self.scope[-1] if self.scope else "<module>"] = node.lineno
            # A spawn with no `env=` inherits the parent environment WHOLE - the
            # quietest way a fourth site could repeat gh#411, since it copies nothing.
            if (
                isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "subprocess"
                and f.attr in _SPAWNS
                and not any(kw.arg == "env" for kw in node.keywords)
            ):
                self.inherits.append(node.lineno)
            self.generic_visit(node)

        def visit_Dict(self, node: ast.Dict) -> None:
            # `{**os.environ}` is a copy too.
            if any(k is None and _is_environ(v) for k, v in zip(node.keys, node.values)):
                self.found[self.scope[-1] if self.scope else "<module>"] = node.lineno
            self.generic_visit(node)

    census = _Census()
    census.visit(tree)
    found = census.found

    assert not census.inherits, (
        f"check.py spawns a child with no env= at line(s) {census.inherits}: it inherits "
        "the parent environment whole. Pass env=_isolated_child_env(...) or an allow-listed copy."
    )

    unexpected = {name: line for name, line in found.items() if name not in _DIRECT_ENV_COPIES}
    assert not unexpected, (
        f"check.py copies the parent environment outside the shared strip: {unexpected}. "
        "Route the child through _isolated_child_env, or add it to _DIRECT_ENV_COPIES "
        "with the reason it cannot write."
    )
    missing = set(_DIRECT_ENV_COPIES) - set(found)
    assert not missing, (
        f"allow-listed sites no longer copy the environment: {sorted(missing)} - "
        "drop them from _DIRECT_ENV_COPIES so the list stays a census, not a history"
    )
