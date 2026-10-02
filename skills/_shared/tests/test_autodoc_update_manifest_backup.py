"""autodoc-update's manifest backup carries the canonical name (gh#184).

`write-manifest.js` used to copy the old manifest to a name it composed by hand, which
`agentflow.backups` does not recognise as canonical, so the reaper never bounded that family and
it became one of the deepest backup stacks on the live tree. Step 5 now CLAIMS the name from
`agentflow.backups` and passes it to the writer as `--backup-to`.

WHAT IT CAN PROVE
* The Step 5 manifest block (found by heading and markers, never a copy) claims the name through
  `python -m agentflow.backups new` BEFORE it calls `write-manifest.js`, and hands that claim over.
* The SKILL no longer composes a backup name anywhere.
* Executed for real against a temp corpus: a successful rewrite leaves exactly one backup, whose
  name `parse_backup_name` accepts as canonical (so the reaper can bound it), whose firing passes
  `FIRING_RE`, and whose bytes are the prior manifest's; a refused write leaves no backup at all,
  not even the empty claim.

WHAT IT CANNOT PROVE
* The Step 1 carrier: the block's first line (sourcing the run carrier) is replaced by the same
  variables set directly.
* That a real corpus's pages are valid; the fixture is one page.
* Skips (not passes) when node or a usable bash is absent. A skip is not a kill.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow import backups as bk

REPO_ROOT = Path(__file__).resolve().parents[3]
SHARED = REPO_ROOT / "skills" / "_shared"
SKILL = REPO_ROOT / "skills" / "autodoc-update" / "SKILL.md"
STEP5_HEADING = "## Step 5 — Update manifest, log, hot"
START = "# manifest-backup"
END = "# end manifest-backup"
CARRIER = '. "$(cat /tmp/autodoc-env.current)" || exit 1'

#: The backup marker as a composed filename token. `agentflow.backups` itself contains the
#: letters, so the module name is excluded; anything else is a name written by hand.
HAND_COMPOSED = re.compile(r"\.bak(?!ups)")


def _manifest_block() -> str:
    text = SKILL.read_text(encoding="utf-8")
    idx = text.find(STEP5_HEADING)
    assert idx >= 0, f"Step 5 heading not found in {SKILL}"
    for m in re.finditer(r"```bash\n(.*?)```", text[idx:], re.S):
        if START in m.group(1):
            return m.group(1)
    raise AssertionError("no ```bash block under Step 5 carries the manifest-backup marker")


def _code_lines(block: str) -> list[str]:
    return [ln for ln in block.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_the_block_claims_the_name_before_it_writes_the_manifest() -> None:
    lines = _code_lines(_manifest_block())

    def first(needle: str) -> int:
        for i, ln in enumerate(lines):
            if needle in ln:
                return i
        raise AssertionError(f"{needle!r} not found in the Step 5 manifest block")

    claim = first("python -m agentflow.backups new")
    write = first("write-manifest.js")
    assert claim < write, "the name must be claimed before write-manifest runs"
    assert '"$AUTODOC/.manifest.json"' in lines[claim], "the claim must be for the manifest itself"
    assert '--backup-to "$BAK"' in "\n".join(lines[write:]), "the claim must be handed to the writer"


def test_the_skill_composes_no_backup_name_anywhere() -> None:
    text = SKILL.read_text(encoding="utf-8")
    offenders = [ln for ln in text.splitlines() if HAND_COMPOSED.search(ln)]
    assert not offenders, f"autodoc-update composes a backup name by hand: {offenders}"


def _bash() -> str:
    """Git's own bash first; a bare PATH lookup on Windows finds the WSL launcher."""
    candidates: list[str] = []
    git = shutil.which("git")
    if git:
        for base in list(Path(git).resolve().parents)[:3]:
            for rel in ("bin/bash.exe", "usr/bin/bash.exe", "bin/bash"):
                c = base / rel
                if c.is_file() and str(c) not in candidates:
                    candidates.append(str(c))
    found = shutil.which("bash")
    if found and "system32" not in found.lower() and found not in candidates:
        candidates.append(found)
    if not candidates:
        pytest.skip("no usable bash found (Git for Windows ships one)")
    return candidates[0]


PAGE_OK = (
    "---\ntype: service\ntitle: Alpha\nsource_paths:\n  - a.py\nstatus: active\n---\n\n# Alpha\n"
)
PAGE_NO_TYPE = "---\ntitle: Broken\nsource_paths:\n  - b.py\nstatus: active\n---\n\n# Broken\n"
#: Bytes a text round-trip would alter, so "preserved" means byte-for-byte.
PRIOR = '{"source_repo":"old/repo",\r\n "note":"café"}'.encode("utf-8")


def _run_block(autodoc: Path) -> subprocess.CompletedProcess[str]:
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH; write-manifest.js cannot run")
    block = _manifest_block()
    assert block.splitlines()[0].startswith(CARRIER), "Step 5 block no longer opens with the carrier"
    script = "set -u\n" + "\n".join(block.splitlines()[1:]) + "\necho BLOCK-DONE\n"
    env = {
        **os.environ,
        # `python` in the block must be THIS interpreter, importing THIS tree's agentflow.
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")]),
        "PYTHONPATH": str(SHARED),
        "SUITE_ROOT": REPO_ROOT.as_posix(),
        "AUTODOC": autodoc.as_posix(),
        "TO": "0123456789abcdef0123456789abcdef01234567",
        "source_repo": "example-org/sample-repo",
        "source_branch": "main",
        "source_path": ".source/sample-repo",
        "project": "sample",
    }
    return subprocess.run(
        [_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=120
    )


def _siblings(autodoc: Path) -> list[str]:
    return sorted(p.name for p in autodoc.iterdir() if p.name != ".manifest.json" and p.suffix != ".md")


def test_a_rewrite_leaves_one_canonical_reapable_backup_of_the_prior_bytes(tmp_path: Path) -> None:
    autodoc = tmp_path / "autodoc"
    autodoc.mkdir()
    (autodoc / "alpha.md").write_text(PAGE_OK, encoding="utf-8")
    manifest = autodoc / ".manifest.json"
    manifest.write_bytes(PRIOR)

    r = _run_block(autodoc)
    assert r.returncode == 0 and "BLOCK-DONE" in r.stdout, r.stderr

    backups = _siblings(autodoc)
    assert len(backups) == 1, f"expected exactly one backup, found {backups}"
    parsed = bk.parse_backup_name(backups[0])
    assert parsed is not None, f"{backups[0]!r} is not a canonical backup name; the reaper cannot bound it"
    assert parsed.primary == ".manifest.json"
    assert bk.FIRING_RE.match(parsed.firing) and parsed.firing.startswith("autodoc-update-f"), parsed
    assert bk.backup_group(manifest) == [autodoc / backups[0]], "the backup must group under the manifest"
    assert (autodoc / backups[0]).read_bytes() == PRIOR, "the backup must be the prior manifest, byte-for-byte"
    assert manifest.read_bytes() != PRIOR, "the manifest itself was rewritten"


def test_a_refused_write_leaves_no_backup_not_even_the_empty_claim(tmp_path: Path) -> None:
    autodoc = tmp_path / "autodoc"
    autodoc.mkdir()
    (autodoc / "broken.md").write_text(PAGE_NO_TYPE, encoding="utf-8")
    manifest = autodoc / ".manifest.json"
    manifest.write_bytes(PRIOR)

    r = _run_block(autodoc)
    assert r.returncode != 0, "a refused manifest write must fail the block"
    assert "BLOCK-DONE" not in r.stdout
    assert manifest.read_bytes() == PRIOR, "the refused write left the manifest untouched"
    assert _siblings(autodoc) == [], "no backup of any name: the write never happened"
