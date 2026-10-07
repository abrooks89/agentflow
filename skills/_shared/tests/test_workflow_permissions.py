"""Every workflow sets a top-level token scope and pins its third-party actions (gh#1172).

Without a top-level `permissions:` block a job runs with the repository's default token scope,
which can be read-write. An action referenced by tag runs whatever that tag points at when the
job starts, so a moved tag changes what CI executes. Both are pinned here structurally: every
workflow under .github/workflows/ declares `permissions:` at the top level, and every `uses:`
that names another repository carries a full 40-hex commit SHA plus the tag in a comment so a
reader can still see which release it is. A local reusable workflow (`./...`) has no SHA to pin
and is exempt.

ci.yml's block is exactly `contents: read`. pr-provenance-gate keeps the job-level grants it
reads the PR with; other jobs inherit the top-level scope.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agentflow.tracked_paths import only_tracked

REPO_ROOT = Path(__file__).resolve().parents[3]
# Tracked workflows only: an untracked .yml in one checkout is not part of the commit CI runs.
WORKFLOWS = only_tracked(REPO_ROOT, (REPO_ROOT / ".github" / "workflows").glob("*.yml"))

_SHA_REF = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
_USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<rest>.*)$")


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} did not parse to a mapping"
    return data


def _uses(data: dict) -> list[str]:
    """Every `uses:` value, at job level (reusable workflows) and step level."""
    found = []
    for job in (data.get("jobs") or {}).values():
        if "uses" in job:
            found.append(str(job["uses"]))
        for step in job.get("steps") or []:
            if "uses" in step:
                found.append(str(step["uses"]))
    return found


def test_there_are_workflows_to_check():
    names = {p.name for p in WORKFLOWS}
    assert {"ci.yml", "nightly.yml"} <= names, names


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_declares_a_top_level_token_scope(path):
    data = _load(path)
    assert "permissions" in data, f"{path.name} has no top-level permissions: block"
    perms = data["permissions"]
    assert isinstance(perms, dict), f"{path.name} permissions: is {perms!r}, not a mapping"
    writes = {k: v for k, v in perms.items() if v == "write"}
    assert not writes, f"{path.name} grants write at the top level: {writes}"


def test_ci_top_level_scope_is_contents_read_only():
    assert _load(REPO_ROOT / ".github" / "workflows" / "ci.yml")["permissions"] == {
        "contents": "read"
    }


def test_pr_provenance_gate_keeps_its_job_level_grants():
    job = _load(REPO_ROOT / ".github" / "workflows" / "ci.yml")["jobs"]["pr-provenance-gate"]
    assert job["permissions"] == {"issues": "read", "pull-requests": "read"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_third_party_action_is_pinned_to_a_commit_sha(path):
    refs = [r for r in _uses(_load(path)) if not r.startswith("./")]
    unpinned = [r for r in refs if not _SHA_REF.match(r)]
    assert not unpinned, f"{path.name} references actions by tag or branch: {unpinned}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_sha_pin_names_its_tag_in_a_comment(path):
    missing = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = _USES_LINE.match(line)
        if not m or m["ref"].strip("'\"").startswith("./"):
            continue
        if not re.match(r"^\s+#\s*v\d", m["rest"]):
            missing.append(f"{n}: {line.strip()}")
    assert not missing, f"{path.name} SHA pins without a `# vN` tag comment: {missing}"


def test_the_pin_checks_reject_a_tag_ref():
    """Control: the two checks above fail on the shapes they exist to catch."""
    assert not _SHA_REF.match("actions/checkout@v4")
    assert not _SHA_REF.match("actions/checkout@main")
    assert _SHA_REF.match("actions/checkout@" + "a" * 40)
    m = _USES_LINE.match("      - uses: actions/checkout@" + "a" * 40)
    assert m and not re.match(r"^\s+#\s*v\d", m["rest"])
