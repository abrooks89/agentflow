"""Plan-file YAML frontmatter parser and DAG validator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Literal

import yaml

Complexity = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class VerificationBlock:
    """Per-task verification contract evaluated by the in-wave verifier subagent."""

    acceptance_criteria: list[str] = field(default_factory=list)
    file_allowlist: list[str] = field(default_factory=list)
    regression_guard: list[str] = field(default_factory=list)
    spec_format: str = ""
    failure_modes: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlanVerificationGlobals:
    """Plan-level verifier defaults applied to every task in the plan."""

    global_failure_modes: list[str] = field(default_factory=list)
    escalation_threshold_pct: int = 15
    max_fix_iterations: int = 4
    wave_deadline_minutes: int | None = None


@dataclass(frozen=True)
class Task:
    id: int
    phase: str
    deps: list[int]
    complexity: Complexity = "medium"
    verification: VerificationBlock | None = None
    auto_mode: bool = True
    task_type: str = "standard"  # YAML key: "type". Values: "standard" | "tdd"
    wave_deadline_minutes: int | None = None
    best_of_n: int = 1           # >1 ⇒ best-of-N mode (candidates + judge); reviews run inside the workflow
    adversarial: bool = False    # True ⇒ refuter-panel verification; reviews run inside the workflow
    # New field (Phase 4 T4): opaque use-case IDs this task owns (e.g. ["UC-0147"]).
    # Additive-only, flat scalar list — solution threads existing IDs here; never required.
    use_cases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ContextFile:
    """Operational runbook injected into every implementer subagent's prompt at fire time."""

    path: str
    why: str


@dataclass(frozen=True)
class PlanFrontmatter:
    epic: str
    spec: str
    phases: list[str]
    tasks: dict[int, Task]
    test_commands: dict[str, str]
    extra_context_files: list[ContextFile] = field(default_factory=list)
    verification: PlanVerificationGlobals | None = None
    # New fields (Task 2): canonical join key, upstream epic deps, lifecycle override
    slug: str | None = None
    depends_on: list[str] = field(default_factory=list)
    current_state: str | None = None
    # New field (Task 18): worktree path for liveness-hardening worktree mandate
    worktree: str | None = None
    # New field (Task 2): project slug for wiki-mirror routing
    project: str | None = None
    # New field (Task 6): per-wave worktree support flag
    per_wave_worktrees: bool = False

    @property
    def is_document(self) -> bool:
        """True for a human-authored `solution/` document - no dispatchable work.

        Reading `not plan.tasks` works and says nothing; this says what the emptiness
        MEANS, so a consumer choosing to skip one is visibly choosing that.
        """
        return not self.tasks


@dataclass(frozen=True)
class SpecFrontmatter:
    """Parsed frontmatter from a spec document (no phases, no tasks)."""

    slug: str
    title: str
    depends_on: list[str] = field(default_factory=list)
    current_state: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class ExternalReferences:
    """All external references extracted from a plan — pure value, no I/O."""

    task_deps: list[tuple[int, int]] = field(default_factory=list)
    endpoints: list[tuple[int, str]] = field(default_factory=list)
    env_vars: list[tuple[int, str]] = field(default_factory=list)
    services: list[tuple[int, str]] = field(default_factory=list)
    # 5th dimension: file paths mentioned in verification blocks that should
    # exist on disk (file_allowlist + regression_guard) and plan-level
    # extra_context_files.  Annotations like "(no edits)" are stripped.
    plan_file_paths: list[tuple[int, str]] = field(default_factory=list)


class DAGError(ValueError):
    """Raised when the plan's task DAG is invalid."""


class SpecFormatError(ValueError):
    """Raised when a spec document has missing or malformed frontmatter."""


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Strips optional leading date (YYYY-MM-DD-) and optional trailing -design suffix.
_SLUG_FILENAME_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}-)?(.+?)(?:-design)?$")

# Regexes for extract_external_references
_ENDPOINT_RE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE) (/\S+)")
_ENV_DOLLAR_RE = re.compile(r"\$([A-Z_][A-Z0-9_]+)")
_ENV_OSENV_RE = re.compile(r'os\.environ(?:\.get\(|\[)["\']([A-Z_][A-Z0-9_]+)["\']')
_SERVICE_URL_VAR_RE = re.compile(r"(\$[A-Z_][A-Z0-9_]*URL)")
_SERVICE_PORT_RE = re.compile(r"(localhost:\d+|(?<!\w):\d+)")
# Strips trailing parenthetical annotations, e.g. "foo.py (no edits)" → "foo.py"
_FILE_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _slug_from_filename(path: Path) -> str:
    """Derive a slug from a filename stem by stripping date prefix and -design suffix."""
    stem = path.stem  # basename without extension
    m = _SLUG_FILENAME_RE.match(stem)
    return m.group(1) if m else stem


def extract_external_references(plan: PlanFrontmatter) -> ExternalReferences:
    """Extract all external references from a parsed plan.

    Pure function — operates only on the PlanFrontmatter struct, performs no I/O.

    The 5th dimension ``plan_file_paths`` collects file paths that should exist
    on disk: each task's ``verification.file_allowlist`` and
    ``verification.regression_guard``, plus plan-level ``extra_context_files``.
    Parenthetical annotations such as ``"(no edits)"`` or ``"(read-only)"`` are
    stripped so the auditor receives bare paths only.
    """
    task_deps: list[tuple[int, int]] = []
    endpoints: list[tuple[int, str]] = []
    env_vars: list[tuple[int, str]] = []
    services: list[tuple[int, str]] = []
    plan_file_paths: list[tuple[int, str]] = []

    for tid, task in sorted(plan.tasks.items()):
        for dep in task.deps:
            task_deps.append((tid, dep))

        if task.verification is None:
            continue

        for criterion in task.verification.acceptance_criteria:
            for m in _ENDPOINT_RE.finditer(criterion):
                endpoints.append((tid, f"{m.group(1)} {m.group(2)}"))
            for m in _ENV_DOLLAR_RE.finditer(criterion):
                env_vars.append((tid, m.group(1)))
            for m in _ENV_OSENV_RE.finditer(criterion):
                env_vars.append((tid, m.group(1)))
            for m in _SERVICE_URL_VAR_RE.finditer(criterion):
                services.append((tid, m.group(1)))
            for m in _SERVICE_PORT_RE.finditer(criterion):
                services.append((tid, m.group(1)))

        # 5th dimension: file paths from verification blocks
        for raw_path in task.verification.file_allowlist:
            cleaned = _FILE_ANNOTATION_RE.sub("", raw_path).strip()
            if cleaned:
                plan_file_paths.append((tid, cleaned))
        for raw_path in task.verification.regression_guard:
            cleaned = _FILE_ANNOTATION_RE.sub("", raw_path).strip()
            if cleaned:
                plan_file_paths.append((tid, cleaned))

    # Plan-level extra_context_files (tid=0 signals plan-level, not task-specific)
    for ctx_file in plan.extra_context_files:
        cleaned = _FILE_ANNOTATION_RE.sub("", ctx_file.path).strip()
        if cleaned:
            plan_file_paths.append((0, cleaned))

    return ExternalReferences(
        task_deps=task_deps,
        endpoints=endpoints,
        env_vars=env_vars,
        services=services,
        plan_file_paths=plan_file_paths,
    )


def parse_plan(path: Path) -> PlanFrontmatter:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise DAGError(f"No YAML frontmatter found in {path}")

    data = yaml.safe_load(match.group(1))

    # A DOCUMENT-SHAPED PLAN IS VALID AND SIMPLY CARRIES NO DISPATCHABLE WORK.
    #
    # `solution/` holds two kinds of file: orchestrator plans, and human-authored
    # operator documents - a waterfall programme, a beta-test runbook. The parser
    # required orchestrator frontmatter of both, so every human document failed
    # (`tasks`, then `type`, then `epic`, then `spec`) and `survey_epics` dropped it
    # with a warning no caller could see. **Measured: 50 files on this machine**, all
    # the same class - not the two the issue was filed on, which were merely the two
    # inside the project whose report printed the skip line.
    #
    # Admitting the shape beats moving the files: the alternative is relocating fifty
    # documents inside another project's data subtree, which no lane may write.
    # `tasks == {}` IS the marker - a plan with nothing to dispatch dispatches nothing,
    # and every consumer already iterates the task map.
    raw_tasks = data.get("tasks") or {}
    if not isinstance(raw_tasks, dict):
        raise DAGError(
            f"tasks must be a mapping of id -> task, got {type(raw_tasks).__name__} in {path}"
        )

    tasks: dict[int, Task] = {}
    for tid, tdata in raw_tasks.items():
        raw_type = tdata.get("type", "standard")
        if raw_type not in ("standard", "tdd"):
            raise DAGError(
                f"task {tid}.type must be 'standard' or 'tdd', got {raw_type!r}"
            )

        wave_deadline = tdata.get("wave_deadline_minutes")
        if wave_deadline is not None:
            if isinstance(wave_deadline, bool) or not isinstance(wave_deadline, int):
                raise DAGError(
                    f"task {tid}.wave_deadline_minutes must be an int or null, got {type(wave_deadline).__name__}"
                )
            if wave_deadline <= 0:
                raise DAGError(f"task {tid}.wave_deadline_minutes must be positive, got {wave_deadline}")

        best_of_n = tdata.get("best_of_n", 1)
        if isinstance(best_of_n, bool) or not isinstance(best_of_n, int) or best_of_n < 1:
            raise DAGError(
                f"task {tid}.best_of_n must be an int >= 1, got {best_of_n!r}"
            )
        adversarial = tdata.get("adversarial", False)
        if not isinstance(adversarial, bool):
            raise DAGError(
                f"task {tid}.adversarial must be a bool, got {type(adversarial).__name__}"
            )

        raw_deps = tdata.get("deps", [])
        if not isinstance(raw_deps, list):
            raise DAGError(
                f"task {tid}.deps must be a list, got {type(raw_deps).__name__}"
            )
        for dep in raw_deps:
            if isinstance(dep, bool) or not isinstance(dep, int):
                # Known emitter bug: YAML deps written as strings ("3") used to surface
                # downstream as a misleading "references unknown task" error. Fail loudly
                # at the type instead so the emitter gets fixed, not the symptom.
                raise DAGError(
                    f"task {tid}.deps entries must be ints, got {dep!r} "
                    f"({type(dep).__name__}) — plan YAML deps must be unquoted integers"
                )

        raw_use_cases = tdata.get("use_cases", [])
        if raw_use_cases is None:
            raw_use_cases = []
        if not isinstance(raw_use_cases, list):
            raise DAGError(
                f"task {tid}.use_cases must be a list, got {type(raw_use_cases).__name__}"
            )
        use_cases = [str(uc) for uc in raw_use_cases]

        tasks[int(tid)] = Task(
            id=int(tid),
            phase=tdata["phase"],
            deps=list(raw_deps),
            complexity=tdata.get("complexity", "medium"),
            verification=_parse_verification_block(
                tdata.get("verification"), f"task {tid}"
            ),
            auto_mode=bool(tdata.get("auto_mode", True)),
            task_type=raw_type,
            wave_deadline_minutes=wave_deadline,
            best_of_n=best_of_n,
            adversarial=adversarial,
            use_cases=use_cases,
        )

    extra_context_files: list[ContextFile] = []
    raw_ctx = data.get("extra_context_files") or []
    if not isinstance(raw_ctx, list):
        raise DAGError(
            f"extra_context_files must be a list, got {type(raw_ctx).__name__}"
        )
    for i, entry in enumerate(raw_ctx):
        if not isinstance(entry, dict):
            raise DAGError(
                f"extra_context_files[{i}] must be a mapping with 'path' and 'why'"
            )
        if "path" not in entry:
            raise DAGError(f"extra_context_files[{i}] missing required key 'path'")
        if "why" not in entry:
            raise DAGError(f"extra_context_files[{i}] missing required key 'why'")
        extra_context_files.append(
            ContextFile(path=str(entry["path"]), why=str(entry["why"]))
        )

    raw_slug = data.get("slug")
    slug = str(raw_slug) if raw_slug else _slug_from_filename(path)

    raw_depends_on = data.get("depends_on") or []
    if not isinstance(raw_depends_on, list):
        raise DAGError(
            f"depends_on must be a list, got {type(raw_depends_on).__name__}"
        )

    raw_worktree = data.get("worktree")
    worktree = str(raw_worktree) if raw_worktree else None

    plan = PlanFrontmatter(
        # `.get`, not `[...]`: a document plan has no epic, no spec and no phases, and a
        # blank `spec: ""` to satisfy the parser is a workaround rather than a fix - the
        # issue says so in as many words. An orchestrator plan still supplies all three;
        # nothing that reads them gained a new None to handle, because they default to
        # the same empty values the dataclass already allowed.
        epic=data.get("epic") or "",
        spec=data.get("spec") or "",
        phases=list(data.get("phases") or []),
        tasks=tasks,
        test_commands=dict(data.get("test_commands", {})),
        extra_context_files=extra_context_files,
        verification=_parse_verification_globals(data.get("verification")),
        slug=slug,
        depends_on=[str(s) for s in raw_depends_on],
        current_state=data.get("current_state"),
        worktree=worktree,
        project=data.get("project"),
        per_wave_worktrees=bool(data.get("per_wave_worktrees", False)),
    )
    _validate_dag(plan)
    return plan


def parse_spec_frontmatter(path: Path) -> SpecFrontmatter:
    """Parse YAML frontmatter from a spec document.

    Raises SpecFormatError when no frontmatter block is found.
    """
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SpecFormatError(f"No YAML frontmatter found in {path}")

    data = yaml.safe_load(match.group(1)) or {}

    raw_slug = data.get("slug")
    slug = str(raw_slug) if raw_slug else _slug_from_filename(path)

    raw_title = data.get("title")
    title = str(raw_title) if raw_title else slug

    raw_depends_on = data.get("depends_on") or []
    if not isinstance(raw_depends_on, list):
        raise SpecFormatError(
            f"depends_on must be a list, got {type(raw_depends_on).__name__}"
        )

    return SpecFrontmatter(
        slug=slug,
        title=title,
        depends_on=[str(s) for s in raw_depends_on],
        current_state=data.get("current_state"),
        last_modified=data.get("last_modified"),
    )


def _parse_verification_block(
    raw: object, context: str
) -> VerificationBlock | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DAGError(
            f"{context}.verification must be a mapping, got {type(raw).__name__}"
        )

    def _str_list(key: str) -> list[str]:
        val = raw.get(key, [])
        if val is None:
            return []
        if not isinstance(val, list):
            raise DAGError(
                f"{context}.verification.{key} must be a list, got {type(val).__name__}"
            )
        return [str(x) for x in val]

    spec_format = raw.get("spec_format", "")
    if spec_format is None:
        spec_format = ""
    if not isinstance(spec_format, str):
        raise DAGError(
            f"{context}.verification.spec_format must be a string, got {type(spec_format).__name__}"
        )

    return VerificationBlock(
        acceptance_criteria=_str_list("acceptance_criteria"),
        file_allowlist=_str_list("file_allowlist"),
        regression_guard=_str_list("regression_guard"),
        spec_format=spec_format,
        failure_modes=_str_list("failure_modes"),
        test_commands=_str_list("test_commands"),
    )


def _parse_verification_globals(raw: object) -> PlanVerificationGlobals | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DAGError(
            f"verification must be a mapping, got {type(raw).__name__}"
        )

    failure_modes = raw.get("global_failure_modes", [])
    if failure_modes is None:
        failure_modes = []
    if not isinstance(failure_modes, list):
        raise DAGError(
            f"verification.global_failure_modes must be a list, got {type(failure_modes).__name__}"
        )

    threshold = raw.get("escalation_threshold_pct", 15)
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        raise DAGError(
            f"verification.escalation_threshold_pct must be an int, got {type(threshold).__name__}"
        )

    max_fix = raw.get("max_fix_iterations", 4)
    if isinstance(max_fix, bool) or not isinstance(max_fix, int):
        raise DAGError(
            f"verification.max_fix_iterations must be an int, got {type(max_fix).__name__}"
        )

    wave_deadline = raw.get("wave_deadline_minutes")
    if wave_deadline is not None:
        if isinstance(wave_deadline, bool) or not isinstance(wave_deadline, int):
            raise DAGError(
                f"verification.wave_deadline_minutes must be an int or null, got {type(wave_deadline).__name__}"
            )
        if wave_deadline <= 0:
            raise DAGError(f"verification.wave_deadline_minutes must be positive, got {wave_deadline}")

    return PlanVerificationGlobals(
        global_failure_modes=[str(x) for x in failure_modes],
        escalation_threshold_pct=threshold,
        max_fix_iterations=max_fix,
        wave_deadline_minutes=wave_deadline,
    )


def compute_ready_set(
    plan: PlanFrontmatter, done: set[int], *, blocked: set[int]
) -> list[int]:
    """Task ids whose deps are all done, excluding done and blocked tasks.

    `blocked` is required: a blocked task belongs to a human or a handler until one resets it,
    and a ready-set that forgot it would re-dispatch escalated work. `in_flight` tasks are
    still returned; callers skip them.
    """
    ready: list[int] = []
    for tid, task in plan.tasks.items():
        if tid in done or tid in blocked:
            continue
        if all(dep in done for dep in task.deps):
            ready.append(tid)
    return sorted(ready)


def _validate_dag(plan: PlanFrontmatter) -> None:
    task_ids = set(plan.tasks)

    for tid, task in plan.tasks.items():
        for dep in task.deps:
            if dep not in task_ids:
                raise DAGError(f"Task {tid} references unknown task {dep}")
        if task.phase not in plan.phases:
            raise DAGError(
                f"Task {tid} in phase {task.phase!r} not declared in phases list"
            )

    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(plan.tasks, WHITE)

    def dfs(node: int) -> None:
        color[node] = GRAY
        for dep in plan.tasks[node].deps:
            if color[dep] == GRAY:
                raise DAGError(f"Dependency cycle involving task {node}")
            if color[dep] == WHITE:
                dfs(dep)
        color[node] = BLACK

    for tid in plan.tasks:
        if color[tid] == WHITE:
            dfs(tid)
# --------------------------------------------------------------------------- #
# Promotion precondition: a spec must name the files it will touch
# --------------------------------------------------------------------------- #
_FILES_HEADING_RE = re.compile(r"^#{2,}\s*Files\s*$", re.MULTILINE)
_NEXT_H2_RE = re.compile(r"^##\s", re.MULTILINE)
# A repo-relative path inside backticks: `skills/_shared/x.py`, `docs/REGISTRY.md`.
# Requires a separator or an extension, so `lead-dev` and `drafted` do not count.
_PATHLIKE_RE = re.compile(r"`([^`" + chr(10) + r"]*[/.][^`" + chr(10) + r"]*)`")


@dataclass(frozen=True)
class FilesSection:
    """What a spec's ``## Files`` section offers a seam check.

    ``present`` is mechanically decidable and is the promotion gate.
    ``paths`` feeds the SECOND half, which a machine cannot decide: whether the
    listing is specific enough to prove two lanes will not edit the same file.
    A section reading "the loop skills" is present and useless, so it yields
    ``present=True, paths=[]`` rather than passing silently.
    """

    present: bool
    paths: tuple[str, ...] = ()

    @property
    def seam_checkable(self) -> bool:
        return self.present and bool(self.paths)


def files_section(text: str) -> FilesSection:
    """Read a spec's ``## Files`` section without parsing the whole document.

    THE EXTRACTION RULE: one backticked, repo-relative path per entry. Only text inside
    backticks that carries a ``/`` or a ``.`` is extracted, so an un-backticked entry declares
    nothing: ``- src/widget.py (new)`` yields ``present=True, paths=()``, which is present and
    not ``seam_checkable``. The rule is not relaxed to guess bare paths out of prose, because a
    guessed path is a seam check run on words the author never marked as a path.

    WHY THIS EXISTS: `lead-dev` proves two lanes are disjoint from the ``domain``
    field and this listing. With neither usable it cannot prove disjointness, so it
    correctly refuses to publish - and the epic then sits `approved-for-autonomous`,
    looking like progress on every board, claimable in principle forever, with
    nothing anywhere reporting why it never moves. Measured on 2026-08-31: one epic
    sat that way for a full day and a second was one edit from joining it.
    """
    m = _FILES_HEADING_RE.search(text)
    if m is None:
        return FilesSection(present=False)
    body = text[m.end():]
    nxt = _NEXT_H2_RE.search(body)
    if nxt is not None:
        body = body[: nxt.start()]
    return FilesSection(present=True, paths=tuple(_PATHLIKE_RE.findall(body)))
