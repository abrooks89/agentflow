"""Tests for the opaque UC-ID allocator + provenance registry (Phase 4 T1).

Covers: opaque zero-padded format, monotonic allocation across calls, the
append-only registry birth-cert shape (no domain/status), input validation,
and — the load-bearing one — parallel-mint collision freedom under lock
contention.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

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
