"""Every agentflow CLI forces UTF-8 on stdout/stderr (gh#574).

On a legacy cp1252 stdout, `registry note` accepted an arrow, `validate` stayed green, and
`registry read` then died with UnicodeEncodeError - blinding every lane's wake read. gh#524
had fixed three modules one line at a time, which is exactly the class this file guards:
"one module was fixed". So the guard derives the entry-point set from the tree rather than
from a list, and the behavioural test runs the real CLI under a forced cp1252 stdout.
"""

from __future__ import annotations

import ast
import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow import _stdio
from agentflow.tracked_paths import only_tracked, tracked_files

_SHARED = Path(__file__).resolve().parents[1]
_PKG = _SHARED / "agentflow"
_REPO = _SHARED.parents[1]
_HELPER = "utf8_stdio"
_ARROW = "→"

# Text files a lane or an operator reads a `python -m agentflow.X` command out of. The
# vendored closure is third-party and never names this package.
_INVOCATION_SUFFIXES = {".md", ".py", ".sh", ".json", ".yml", ".yaml", ".toml"}
# Lower-case only: module names are, and prose writes the placeholder `agentflow.X`.
_M_INVOCATION = re.compile(r"-m\s+agentflow\.([a-z_][a-z0-9_]*)\b")


def _has_main_block(tree: ast.Module) -> bool:
    for node in tree.body:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and any(
                isinstance(c, ast.Constant) and c.value == "__main__" for c in node.test.comparators
            )
        ):
            return True
    return False


def _package_modules() -> list[Path]:
    # The index, not the working tree (gh#175): a stray untracked file must not move the verdict.
    return only_tracked(_REPO, _PKG.glob("*.py"))


def _main_block_modules() -> set[str]:
    found = set()
    for path in _package_modules():
        if _has_main_block(ast.parse(path.read_text(encoding="utf-8"))):
            found.add(path.stem)
    return found


def _is_invocation_source(rel: str) -> bool:
    parts = rel.split("/")
    return Path(rel).suffix in _INVOCATION_SUFFIXES and "_vendored" not in parts


def _invoked_modules() -> set[str]:
    found = set()
    for path in tracked_files(_REPO, _is_invocation_source):
        text = path.read_text(encoding="utf-8", errors="replace")
        found.update(_M_INVOCATION.findall(text))
    return found


def _entry_modules() -> set[str]:
    return _main_block_modules() | _invoked_modules()


def _calls_helper_first(module: str) -> tuple[bool, str]:
    """True when main() calls utf8_stdio() before any statement that could write output."""
    path = _PKG / f"{module}.py"
    if not path.is_file():
        return False, f"agentflow/{module}.py does not exist but is invoked with -m"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    if len(mains) != 1:
        return False, f"agentflow/{module}.py has {len(mains)} top-level main() definitions"
    for stmt in mains[0].body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Name)
            and stmt.value.func.id == _HELPER
        ):
            return True, ""
        return False, (
            f"agentflow/{module}.py main() runs `{ast.unparse(stmt).splitlines()[0]}` "
            f"(line {stmt.lineno}) before calling {_HELPER}()"
        )
    return False, f"agentflow/{module}.py main() never calls {_HELPER}()"


def test_the_derived_entry_point_set_is_not_vacuous():
    """A glob or regex that silently matched nothing would make the guard below pass empty."""
    mains = _main_block_modules()
    invoked = _invoked_modules()
    assert "registry" in mains and "registry" in invoked
    assert len(mains) >= 10, sorted(mains)
    assert len(invoked) >= 10, sorted(invoked)


def test_every_cli_entry_point_calls_the_helper_first():
    failures = []
    for module in sorted(_entry_modules()):
        ok, why = _calls_helper_first(module)
        if not ok:
            failures.append(why)
    assert not failures, "\n".join(failures)


def test_no_module_keeps_an_ad_hoc_reconfigure():
    """The gh#524 one-line fixes are folded into the helper; a new copy is the old class."""
    offenders = []
    for p in _package_modules():
        if p.name == "_stdio.py":
            continue
        text = p.read_text(encoding="utf-8")
        if ".reconfigure(" in text or '"reconfigure"' in text:
            offenders.append(p.name)
    assert not offenders, offenders


# --------------------------------------------------------------------------- #
# The helper itself
# --------------------------------------------------------------------------- #
def test_helper_turns_a_cp1252_stream_into_utf8(monkeypatch):
    raw_out, raw_err = io.BytesIO(), io.BytesIO()
    out = io.TextIOWrapper(raw_out, encoding="cp1252", write_through=True)
    err = io.TextIOWrapper(raw_err, encoding="cp1252", write_through=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    _stdio.utf8_stdio()
    out.write(f"a {_ARROW} b")
    err.write(_ARROW)
    assert (out.encoding, out.errors) == ("utf-8", "replace")
    assert (err.encoding, err.errors) == ("utf-8", "replace")
    assert raw_out.getvalue() == f"a {_ARROW} b".encode("utf-8")
    assert raw_err.getvalue() == _ARROW.encode("utf-8")


def test_helper_leaves_a_stream_without_reconfigure_alone(monkeypatch):
    capture = io.StringIO()
    monkeypatch.setattr(sys, "stdout", capture)
    monkeypatch.setattr(sys, "stderr", None)  # pythonw hands a process no streams at all
    _stdio.utf8_stdio()
    assert sys.stdout is capture
    assert sys.stderr is None


class _RefusingStream:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def reconfigure(self, **_kw) -> None:
        self.calls += 1
        raise self.exc


@pytest.mark.parametrize("exc", [ValueError("I/O operation on closed file"), OSError("detached")])
def test_helper_never_raises_when_reconfigure_refuses(monkeypatch, exc):
    out, err = _RefusingStream(exc), _RefusingStream(exc)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    _stdio.utf8_stdio()
    assert (out.calls, err.calls) == (1, 1)


# --------------------------------------------------------------------------- #
# Behaviour: the real CLI under a legacy stdout
# --------------------------------------------------------------------------- #
def _child_env(force_cp1252: bool) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONPATH"] = str(_SHARED) + os.pathsep + env.get("PYTHONPATH", "")
    if force_cp1252:
        # What a Windows pipe or console hands a process with PYTHONUTF8 unset. Forcing it
        # through the env reproduces the gh#574 crash on every CI platform, not only Windows.
        env["PYTHONIOENCODING"] = "cp1252"
    return env


def _registry(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agentflow.registry", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        cwd=str(_SHARED),
        env=env,
        timeout=120,
    )


@pytest.fixture
def arrow_board(tmp_path):
    board = tmp_path / "registry.md"
    env = _child_env(force_cp1252=True)
    common = ["--file", str(board), "--lock-dir", str(tmp_path / "locks")]
    init = _registry(["init", *common], env)
    assert init.returncode == 0, init.stderr.decode("utf-8", "replace")
    note = _registry(["note", *common, "--actor", "lead-dev", "--text", f"ship A {_ARROW} B"], env)
    assert note.returncode == 0, note.stderr.decode("utf-8", "replace")
    assert f"ship A {_ARROW} B" in board.read_text(encoding="utf-8")
    return board, common


@pytest.mark.parametrize("force_cp1252", [True, False], ids=["cp1252-stdout", "ambient-stdout"])
def test_registry_read_emits_a_noted_arrow_as_utf8(arrow_board, force_cp1252):
    board, common = arrow_board
    res = _registry(["read", *common], _child_env(force_cp1252))
    assert res.returncode == 0, res.stderr.decode("utf-8", "replace")
    assert b"UnicodeEncodeError" not in res.stderr
    assert f"ship A {_ARROW} B".encode("utf-8") in res.stdout


def test_a_board_validate_calls_valid_is_one_read_can_print(arrow_board):
    """validate and read must agree: a board validate passes is a board every lane can read."""
    board, common = arrow_board
    env = _child_env(force_cp1252=True)
    validate = _registry(["validate", *common], env)
    read = _registry(["read", *common], env)
    assert validate.returncode == 0, validate.stdout + validate.stderr
    assert read.returncode == 0, read.stderr.decode("utf-8", "replace")
    assert _ARROW.encode("utf-8") in read.stdout
