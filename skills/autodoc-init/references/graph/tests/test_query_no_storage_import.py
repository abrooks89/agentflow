"""Success criterion 6: the query layer imports no storage adapter —
test-enforced the same two ways as autodoc_graph (epic 702): a
clean-interpreter import probe and a static source scan. The CLI is the one
composition root allowed to wire a backend."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.tracked import tracked_files

CORE_MODULES = (
    "autodoc_query.api",
    "autodoc_query.router",
    "autodoc_query.local",
    "autodoc_query.globalsearch",
    "autodoc_query.scorer",
    "autodoc_query.types",
    "autodoc_query.tuning",
)

PKG_DIR = Path(__file__).resolve().parent.parent / "autodoc_query"

CORE_SOURCES = ("api.py", "router.py", "local.py", "globalsearch.py",
                "scorer.py", "types.py", "tuning.py", "__init__.py")


def test_query_core_imports_pull_in_no_storage_backend():
    probe = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in CORE_MODULES)
        + "bad = [m for m in sys.modules if 'sqlite_backend' in m or m == 'sqlite3'"
          " or m == 'sqlite_vec']\n"
        + "assert not bad, f'query core imported storage modules: {bad}'\n"
        + "print('CLEAN')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, check=False,  # returncode asserted below
        cwd=str(PKG_DIR.parent),
    )
    assert res.returncode == 0, f"probe failed:\n{res.stdout}\n{res.stderr}"
    assert "CLEAN" in res.stdout


def test_query_core_sources_never_mention_sqlite():
    for name in CORE_SOURCES:
        text = (PKG_DIR / name).read_text(encoding="utf-8").lower()
        assert "sqlite" not in text, (
            f"autodoc_query/{name} mentions sqlite — the query layer must stay "
            "storage-agnostic (the fork thesis)"
        )


def test_cli_is_the_only_query_module_wiring_a_backend():
    for name in CORE_SOURCES:
        text = (PKG_DIR / name).read_text(encoding="utf-8")
        assert "sqlite_backend" not in text
    # eval: run_eval.py is that surface's composition root (it opens the
    # store to run live queries); the metric/embedder modules stay clean
    # Tracked modules only (gh#410): one untracked .py beside them must not redden this.
    eval_dir = PKG_DIR / "eval"
    if eval_dir.exists():
        for path in tracked_files(eval_dir, lambda rel: "/" not in rel and rel.endswith(".py")):
            if path.name == "run_eval.py":
                continue
            assert "sqlite_backend" not in path.read_text(encoding="utf-8"), (
                f"{path} wires a storage backend — only composition roots may"
            )
