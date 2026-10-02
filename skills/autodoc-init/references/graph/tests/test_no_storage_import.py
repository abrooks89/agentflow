"""Success criterion 4: the core (ingest, embedder, adapter, vocab) has NO
import path to any storage adapter — the whole fork thesis. Extraction is a
separate Node runtime and trivially cannot import Python storage code.

Enforced two ways: a clean-interpreter import probe (sys.modules must not
contain the sqlite backend, nor sqlite3 itself) and a static source scan.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CORE_MODULES = ("autodoc_graph.ingest", "autodoc_graph.embedder",
                "autodoc_graph.adapter", "autodoc_graph.vocab",
                "autodoc_graph.communities", "autodoc_graph.naming")

PKG_DIR = Path(__file__).resolve().parent.parent / "autodoc_graph"


def test_core_imports_pull_in_no_storage_backend():
    probe = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in CORE_MODULES)
        + "bad = [m for m in sys.modules if 'sqlite_backend' in m or m == 'sqlite3'"
          " or m == 'sqlite_vec']\n"
        + "assert not bad, f'core imported storage modules: {bad}'\n"
        + "print('CLEAN')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, check=False,  # returncode asserted below
        cwd=str(PKG_DIR.parent),
    )
    assert res.returncode == 0, f"probe failed:\n{res.stdout}\n{res.stderr}"
    assert "CLEAN" in res.stdout


def test_core_sources_never_mention_sqlite():
    for name in ("ingest.py", "embedder.py", "adapter.py", "vocab.py",
                 "communities.py", "naming.py"):
        text = (PKG_DIR / name).read_text(encoding="utf-8").lower()
        assert "sqlite" not in text, (
            f"{name} mentions sqlite — core modules must stay storage-agnostic"
        )


def test_cli_is_the_only_module_wiring_core_to_backend():
    # the CLI is the composition root; nothing else may import the backend
    for name in ("ingest.py", "embedder.py", "adapter.py", "vocab.py",
                 "communities.py", "naming.py", "__init__.py"):
        text = (PKG_DIR / name).read_text(encoding="utf-8")
        assert "sqlite_backend" not in text
