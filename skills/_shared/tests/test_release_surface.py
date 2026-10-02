"""The files a stranger reads first must agree with each other and with the tree.

The community-surface epic (AF-R6) fixed four things that drift silently because nothing
reads them at runtime: the version string, the LICENSE body, the third-party credit, and
the labels an issue template applies. Each test below pins one of them. None imports the
package it checks: an import proves which tree the interpreter found, not what the file
on disk declares, so every declaration is parsed as text (`tomllib`, `json`, a regex).

WHAT IT CAN PROVE
* Every first-party version declaration equals `VERSION`, and a disagreement names its file.
* `LICENSE` is the bare MIT text, which is what GitHub's license matcher needs to report MIT.
* The vendored-code attribution moved out of `LICENSE` survives in `ACKNOWLEDGEMENTS.md`.
* Every runtime dependency the manifests declare is named in `ACKNOWLEDGEMENTS.md`. The list
  comes from the manifests, so a new dependency without a credit fails here.
* No issue template applies a label that routes work to the fleet.

WHAT IT CANNOT PROVE
* That GitHub actually detects MIT. That is a post-merge check against the host.
* That a license stated in `ACKNOWLEDGEMENTS.md` is correct; it was read from package
  metadata when written, and only its presence is checked here.
* Transitive dependencies. Only the ones the manifests declare are checked.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

from agentflow.issue_intake import (
    EPIC_LABEL,
    IN_PROGRESS_LABEL,
    NEEDS_DECISION_LABEL,
    TRIAGED_LABEL,
)
from agentflow.tracked_paths import tracked_files

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSION_FILE = REPO_ROOT / "VERSION"
LICENSE = REPO_ROOT / "LICENSE"
ACKNOWLEDGEMENTS = REPO_ROOT / "ACKNOWLEDGEMENTS.md"
ISSUE_TEMPLATE_DIR = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"

SHARED_PYPROJECT = REPO_ROOT / "skills" / "_shared" / "pyproject.toml"
GRAPH_DIR = REPO_ROOT / "skills" / "autodoc-init" / "references" / "graph"
GRAPH_PYPROJECT = GRAPH_DIR / "pyproject.toml"
GRAPH_INIT = GRAPH_DIR / "autodoc_graph" / "__init__.py"
INVENTORY_DIR = REPO_ROOT / "skills" / "autodoc-init" / "references" / "inventory"
INVENTORY_PACKAGE = INVENTORY_DIR / "package.json"
INVENTORY_LOCK = INVENTORY_DIR / "package-lock.json"

FLEET_ROUTING_LABELS = {TRIAGED_LABEL, EPIC_LABEL, IN_PROGRESS_LABEL, NEEDS_DECISION_LABEL}

# PEP 508: the distribution name is the leading run before any extra, specifier or marker.
_PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _declared_versions() -> list[tuple[str, str]]:
    """(where, version) for every first-party declaration, parsed without importing."""
    init_match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        GRAPH_INIT.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert init_match, f"{_rel(GRAPH_INIT)} declares no __version__"
    lock = _json(INVENTORY_LOCK)
    return [
        (_rel(SHARED_PYPROJECT), _toml(SHARED_PYPROJECT)["project"]["version"]),
        (_rel(GRAPH_PYPROJECT), _toml(GRAPH_PYPROJECT)["project"]["version"]),
        (_rel(GRAPH_INIT), init_match.group(1)),
        (_rel(INVENTORY_PACKAGE), _json(INVENTORY_PACKAGE)["version"]),
        (f"{_rel(INVENTORY_LOCK)} (top-level version)", lock["version"]),
        (f'{_rel(INVENTORY_LOCK)} (packages[""].version)', lock["packages"][""]["version"]),
    ]


def test_every_first_party_version_matches_VERSION():
    """`VERSION` is the source; every sub-package follows it.

    Before AF-R6 the suite shipped as 0.2.0 while its own packages said 0.1.0 and 1.0.0.
    Nothing reads these at runtime, so only a test notices when a bump misses one.
    """
    expected = VERSION_FILE.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", expected), f"VERSION is not X.Y.Z: {expected!r}"
    disagreeing = [f"{where} says {got!r}" for where, got in _declared_versions() if got != expected]
    assert not disagreeing, f"VERSION is {expected!r} but: " + "; ".join(disagreeing)


def test_license_is_the_bare_mit_text():
    """GitHub's matcher compares the whole file to the MIT template.

    Extra lines after the disclaimer (a `---` rule and a third-party paragraph) made the
    host report NOASSERTION. Third-party credit lives in ACKNOWLEDGEMENTS.md instead.
    """
    lines = LICENSE.read_text(encoding="utf-8").splitlines()
    assert lines[0].strip() == "MIT License", "LICENSE does not start with the MIT title"
    non_blank = [line for line in lines if line.strip()]
    assert non_blank[-1].strip() == "SOFTWARE.", (
        f"LICENSE must end at the MIT disclaimer's 'SOFTWARE.', ends with {non_blank[-1]!r}"
    )
    assert not any(line.strip() == "---" for line in lines), "LICENSE carries a '---' rule"
    assert "_vendored" not in "\n".join(lines), "LICENSE references skills/_vendored/"


def test_vendored_attribution_survives_in_acknowledgements():
    """Moving the paragraph out of LICENSE must not lose the upstream author or its license path."""
    text = ACKNOWLEDGEMENTS.read_text(encoding="utf-8")
    assert "Jesse Vincent" in text, "ACKNOWLEDGEMENTS.md lost the superpowers author"
    assert "skills/_vendored/LICENSE" in text, (
        "ACKNOWLEDGEMENTS.md lost the literal path skills/_vendored/LICENSE"
    )


def _runtime_dependencies() -> list[tuple[str, str]]:
    """(manifest, distribution name) for each declared runtime dependency; test extras excluded."""
    deps: list[tuple[str, str]] = []
    for pyproject in (SHARED_PYPROJECT, GRAPH_PYPROJECT):
        for requirement in _toml(pyproject)["project"].get("dependencies", []):
            match = _PEP508_NAME.match(requirement)
            assert match, f"{_rel(pyproject)}: unparseable requirement {requirement!r}"
            deps.append((_rel(pyproject), match.group(1)))
    for name in _json(INVENTORY_PACKAGE).get("dependencies", {}):
        deps.append((_rel(INVENTORY_PACKAGE), name))
    return deps


def test_acknowledgements_names_every_runtime_dependency():
    """Every dependency a manifest declares is credited, read from the manifests, not a list here.

    A hardcoded list would drift exactly the way ACKNOWLEDGEMENTS did (one of four named).
    Matching is case-insensitive (`pyyaml` in the manifest, `PyYAML` in prose) and whole-name,
    so `tree-sitter` inside `web-tree-sitter` does not count as a credit.
    """
    text = ACKNOWLEDGEMENTS.read_text(encoding="utf-8")
    deps = _runtime_dependencies()
    assert deps, "no runtime dependencies parsed; the manifest parsing is broken"
    missing = [
        f"{name} (from {manifest})"
        for manifest, name in deps
        if not re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text, re.IGNORECASE)
    ]
    assert not missing, "ACKNOWLEDGEMENTS.md does not name: " + ", ".join(missing)


def _labels(form: dict) -> list[str]:
    raw = form.get("labels") or []
    return [raw] if isinstance(raw, str) else list(raw)


def test_issue_templates_never_apply_a_fleet_routing_label():
    """An issue form that auto-applied `triaged` would put any stranger's issue in front of a builder.

    The routing labels are imported from issue_intake, the module the loops select by, so a
    renamed label is followed here rather than silently escaping the check.
    """
    # Tracked files only (gh#175): a stray untracked .yml in one checkout must not change the verdict.
    templates = tracked_files(
        REPO_ROOT, lambda rel: rel.startswith(".github/ISSUE_TEMPLATE/") and rel.endswith(".yml")
    )
    assert templates, f"no issue templates under {_rel(ISSUE_TEMPLATE_DIR)}; the check would be vacuous"
    offending = []
    for template in templates:
        form = yaml.safe_load(template.read_text(encoding="utf-8"))
        assert isinstance(form, dict), f"{_rel(template)} does not parse to a mapping"
        offending += [f"{_rel(template)}: {label}" for label in _labels(form) if label in FLEET_ROUTING_LABELS]
    assert not offending, "issue templates apply fleet routing labels: " + ", ".join(offending)
