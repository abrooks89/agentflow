"""Tests for the opaque UC-ID allocator + provenance registry (Phase 4 T1).

Covers: opaque zero-padded format, monotonic allocation across calls, the
append-only registry birth-cert shape (no domain/status), input validation,
and — the load-bearing one — parallel-mint collision freedom under lock
contention.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow import config
from agentflow.lock_file import LockHeldError
from agentflow.use_case_id import allocate


def _read_registry(project_root: Path) -> list[dict]:
    path = project_root / "design" / "use-cases" / "registry.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_format_is_opaque_zero_padded(tmp_path: Path) -> None:
    ids = allocate(tmp_path, 1, epic_slug="e", titles=["t"], date="2026-07-18")
    assert ids == ["UC-0001"]


def test_monotonic_across_calls(tmp_path: Path) -> None:
    first = allocate(tmp_path, 2, epic_slug="e", titles=["a", "b"], date="d")
    second = allocate(tmp_path, 1, epic_slug="e", titles=["c"], date="d")
    assert first == ["UC-0001", "UC-0002"]
    assert second == ["UC-0003"]


def test_counter_persists_exact_high_water_mark(tmp_path: Path) -> None:
    allocate(tmp_path, 3, epic_slug="e", titles=["a", "b", "c"], date="d")
    counter = (tmp_path / "design" / "use-cases" / ".counter").read_text(encoding="utf-8")
    assert counter == "3"


def test_registry_is_append_only_birth_cert(tmp_path: Path) -> None:
    allocate(
        tmp_path,
        1,
        epic_slug="epic-x",
        titles=["Login flow"],
        date="2026-07-18",
        origin="refine",
    )
    allocate(tmp_path, 1, epic_slug="epic-y", titles=["Logout"], date="2026-07-19")
    rows = _read_registry(tmp_path)
    assert len(rows) == 2
    assert rows[0] == {
        "id": "UC-0001",
        "minted": "2026-07-18",
        "epic": "epic-x",
        "title": "Login flow",
        "origin": "refine",
    }
    # Birth cert stores NO domain/status (those are mutable, live elsewhere).
    assert "domain" not in rows[0]
    assert "status" not in rows[0]


def test_rejects_non_positive_count(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="count must be >= 1"):
        allocate(tmp_path, 0, epic_slug="e", titles=[], date="d")


def test_rejects_title_count_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="titles length"):
        allocate(tmp_path, 2, epic_slug="e", titles=["only-one"], date="d")


def test_creates_use_cases_dir_when_absent(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    allocate(project, 1, epic_slug="e", titles=["t"], date="d")
    assert (project / "design" / "use-cases" / "registry.jsonl").is_file()
    assert (project / "design" / "use-cases" / ".counter").is_file()


def _mint_batch(project_root: Path, n: int) -> list[str]:
    titles = [f"story-{i}" for i in range(n)]
    return allocate(
        project_root, n, epic_slug="parallel", titles=titles, date="d"
    )


def test_parallel_mint_is_collision_free(tmp_path: Path) -> None:
    """Concurrent minters must never hand out the same id twice.

    Many workers each mint a batch simultaneously; the counter lock serializes
    them. The union of all minted ids must be duplicate-free and dense
    (1..total), and the registry must hold exactly one row per id.
    """
    workers = 12
    per_worker = 5
    total = workers * per_worker

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_mint_batch, tmp_path, per_worker)
            for _ in range(workers)
        ]
        minted: list[str] = []
        for fut in futures:
            minted.extend(fut.result())

    assert len(minted) == total
    assert len(set(minted)) == total, "duplicate ids handed out under contention"

    expected = {f"UC-{i:04d}" for i in range(1, total + 1)}
    assert set(minted) == expected, "ids not dense/monotonic across minters"

    rows = _read_registry(tmp_path)
    assert len(rows) == total
    assert len({r["id"] for r in rows}) == total


def test_acquire_times_out_when_lock_held(tmp_path: Path, monkeypatch) -> None:
    """A live-held lock that never releases surfaces as LockHeldError."""
    import agentflow.use_case_id as uc

    monkeypatch.setattr(uc, "_ACQUIRE_TIMEOUT", 0.05)
    monkeypatch.setattr(uc, "_ACQUIRE_POLL", 0.01)

    uc_dir = tmp_path / "design" / "use-cases"
    uc_dir.mkdir(parents=True)
    lock_path = uc_dir / ".counter.lock"
    # Simulate a live holder: PID of this very (alive) process.
    from agentflow import lock_file

    lock_file.acquire(lock_path)  # held, never released

    with pytest.raises(LockHeldError):
        allocate(tmp_path, 1, epic_slug="e", titles=["t"], date="d")


# --- The mint command documented in refine/SKILL.md -------------------------------------------
#
# THE DEFECT. The documented command pasted `<data_root>/projects/<name>` into an ordinary Python
# string inside a `python -c "..."` shell argument. A Windows data root carries `\U` (a unicode
# escape) and `\n`, so the command failed to compile or wrote to a mangled path; and an apostrophe
# in a title closed the single-quoted literal. WHAT THIS PINS: the command is a quoted python
# heredoc, and its body -- filled in the way a seat fills it -- compiles and, run against a
# backslash data root and hostile titles, allocates into the resolved project's registry with the
# titles verbatim. WHAT IT CANNOT PROVE: that a seat copies the command rather than retyping it.

_SKILL_NAME = "refine"  # a skill directory, not a data-root bucket
_REFINE_SKILL = Path(__file__).resolve().parents[2] / _SKILL_NAME / "SKILL.md"
_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```[ \t]*$", re.S | re.M)
_HEREDOC = re.compile(r"<<'PY'[^\n]*\n(.*?)^PY[ \t]*$", re.S | re.M)
_PLACEHOLDER = re.compile(r"<([^<>\n]+)>")

_NAME = "agentflow"
_EPIC = "some-epic"
_DATE = "2026-01-02"
#: Two titles: an apostrophe, double quotes, `\U`/`\n` escapes, and a trailing backslash.
_TITLES = ["It's the seat's C:\\Users\\new story", 'A "quoted" title ending in \\']


def _mint_command() -> str:
    """The one fenced block in refine/SKILL.md that calls the allocator."""
    text = _REFINE_SKILL.read_text(encoding="utf-8")
    blocks = [b for b in _FENCE.findall(text) if "use_case_id import allocate" in b]
    assert len(blocks) == 1, (
        f"expected exactly one fenced mint command in refine/SKILL.md, found {len(blocks)}"
    )
    return blocks[0]


def _python_source(command: str) -> str:
    """What python receives: a quoted heredoc's body is passed through byte for byte."""
    m = _HEREDOC.search(command)
    return m.group(1) if m else command


def _fill(src: str, data_root: str) -> str:
    def value(m: re.Match) -> str:
        ph = m.group(1)
        if ph == "name":
            return _NAME
        if ph == "epic-slug":
            return _EPIC
        if ph == "YYYY-MM-DD":
            return _DATE
        if "data_root" in ph:
            return data_root
        return "\n".join(_TITLES)

    return _PLACEHOLDER.sub(value, src)


def test_refine_mint_command_is_a_quoted_python_heredoc() -> None:
    """Quoted delimiter, so the shell expands nothing and an apostrophe closes no quote."""
    command = _mint_command()
    assert _HEREDOC.search(command), (
        "refine/SKILL.md's mint command is not a `python - <<'PY'` heredoc; a title or data root "
        f"pasted into a shell argument breaks on an apostrophe or a backslash:\n{command}"
    )


def test_refine_mint_command_compiles_with_a_windows_root_and_hostile_titles() -> None:
    src = _fill(_python_source(_mint_command()), r"C:\Users\you\newroot\agentflow")
    try:
        compile(src, "<refine mint command>", "exec")
    except SyntaxError as exc:
        pytest.fail(
            f"refine/SKILL.md's mint command does not compile once filled in ({exc.msg}). Build "
            f"the root in code from config and hold titles in a raw triple-quoted string.\n{src}"
        )


def test_refine_mint_command_allocates_into_the_resolved_projects_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # Backslash separators: real directories on Windows, one backslash-laden name on Linux.
    # Either way a non-raw literal would read `\U` and `\n` as escapes.
    data_root = f"{tmp_path}\\Users\\new\\agentflow"
    monkeypatch.setattr(config, "load_environment", lambda: SimpleNamespace(data_root=data_root))
    src = _fill(_python_source(_mint_command()), data_root)
    exec(compile(src, "<refine mint command>", "exec"), {"__name__": "mint"})

    assert capsys.readouterr().out.strip() == str(["UC-0001", "UC-0002"])
    project_root = Path(data_root) / "projects" / _NAME
    rows = _read_registry(project_root)
    assert [r["title"] for r in rows] == _TITLES, "titles were altered on the way in"
    assert {(r["epic"], r["minted"]) for r in rows} == {(_EPIC, _DATE)}
