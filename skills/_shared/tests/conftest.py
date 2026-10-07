"""Shared fixtures. Only named fixtures live here: nothing is autouse, so no test module is
changed by this file unless it asks for a fixture by name.
"""
from __future__ import annotations

from pathlib import Path

import pytest

#: The configured person in every hermetic environment below. Obviously not anybody.
FAKE_PERSON = "Ada Example"


def write_environment(home: Path, root: Path, person: str = FAKE_PERSON) -> Path:
    """A minimal valid ``environment.md`` under *home*, every directory pointed at *root*."""
    home.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    d = root.as_posix()
    body = (
        "schema_version: 1\n"
        "person:\n"
        f"  name: {person}\n"
        f"data_root: {d}\n"
        "git_host:\n"
        "  kind: local\n"
        "notify:\n"
        "  kind: none\n"
        f"lock_dir: {d}\n"
        "shell: posix\n"
        f"suite_root: {d}\n"
        f"worktree_root: {d}\n"
    )
    path = home / "environment.md"
    path.write_text(f"# environment\n\n```yaml\n{body}```\n", encoding="utf-8")
    return path


@pytest.fixture
def residue_config(tmp_path_factory, monkeypatch) -> str:
    """A hermetic environment config, so ``write_page``'s residue guard resolves a known name.

    ``write_page`` resolves ``person.name`` from the environment config, as the residue CLI does.
    Without this, a suite run reads whatever config the machine has: on a developer box that is a
    real person's name, and on a CI runner there is none and every write is refused as
    unmeasurable. Returns the configured name.
    """
    base = tmp_path_factory.mktemp("residue-config")
    home = base / "config"
    write_environment(home, base / "root")
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    return FAKE_PERSON
