"""The live harness scenarios name a toy project, never the operator's real one.

`harness/run-scenario.md` launched Scenarios A, B and D with no `--project`, and the loop
skills resolve their project from `--project` or the session default. Followed literally,
the runbook fired a live builder on whatever real fleet project the machine defaulted to,
and seeded the Scenario D registry into a `<toy>` path nothing defined. These pins keep
every live launch on `--project harness-toy`, keep the shipped profile for that project
valid under the real resolver, and keep the three places that spell its name in step.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentflow import doctor
from agentflow.config import ConfigError, _load_first_yaml_block, load_project
from agentflow.plan_parser import parse_plan

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS = REPO_ROOT / "harness"
RUNBOOK = HARNESS / "run-scenario.md"
TEMPLATE = HARNESS / "toy-profile" / "harness-toy.md"
EXAMPLE_CONFIG = HARNESS / "harness.config.example.json"
SEED_REGISTRY = HARNESS / "fixtures" / "scenarioD-registry-before.md"
FIXTURES = HARNESS / "fixtures"

TOY = "harness-toy"
PLACEHOLDER = "<agentflow_clone>"


def _runbook() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under `## <heading>...` up to the next `## ` heading (### stays inside)."""
    m = re.search(rf"^## {re.escape(heading)}.*?$(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
    assert m, f"run-scenario.md has no '## {heading}' section"
    return m.group("body")


def _launch_commands() -> list[str]:
    """Slash-command names the live scenarios launch, read from the shipped config."""
    cfg = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    return [
        "/" + cfg["solution_skill"].lstrip("/"),
        "/" + cfg["orchestrator_skill"].lstrip("/"),
        "/" + cfg["builder_entry"].lstrip("/"),
    ]


def _code_texts(body: str) -> list[str]:
    """Every fenced-block line and every inline code span in *body*.

    A launch line can be written either way, so both are read; prose outside code is a
    mention of a command, not an instruction to type one.
    """
    out: list[str] = []
    fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
            continue
        if fence:
            out.append(line.strip())
        else:
            out.extend(re.findall(r"`([^`\n]+)`", line))
    return out


def _launch_lines(body: str) -> list[str]:
    """Code text that invokes a launch command WITH arguments.

    A bare `/lead-dev` (as in "config: `/lead-dev`") names the command and launches
    nothing, so it is not a launch line; anything after the command word makes it one.
    """
    cmds = _launch_commands()
    lines = []
    for code in _code_texts(body):
        for cmd in cmds:
            if re.match(rf"{re.escape(cmd)}\s+\S", code):
                lines.append(code)
    return lines


@pytest.mark.parametrize(
    ("heading", "minimum"),
    [("Scenario A", 1), ("Scenario B", 1), ("Scenario D", 2)],
    ids=["A", "B", "D-and-D4"],
)
def test_every_live_launch_line_passes_project_harness_toy(heading: str, minimum: int) -> None:
    lines = _launch_lines(_section(_runbook(), heading))
    assert len(lines) >= minimum, (
        f"{heading}: expected at least {minimum} launch line(s), found {lines!r}"
    )
    for line in lines:
        args = line.split()[1:]
        assert args[:2] == ["--project", TOY], (
            f"{heading}: launch line does not open with `--project {TOY}`: {line!r}"
        )


def test_no_launch_line_anywhere_omits_the_project() -> None:
    """Outside A/B/D too: a launch line added to any section must still name the toy."""
    for line in _launch_lines(_runbook()):
        assert f"--project {TOY}" in line, f"launch line without --project {TOY}: {line!r}"


def test_no_unresolved_toy_or_project_placeholder() -> None:
    text = _runbook()
    assert "<toy>" not in text
    assert not re.search(r"--project\s+<", text), "a --project placeholder is left in the runbook"
    assert f"<data_root>/projects/{TOY}/status/registry.md" in text


def test_the_stop_rule_precedes_the_scenarios() -> None:
    """Tokens, not the sentence: a re-wording keeps the rule as long as these survive."""
    text = _runbook()
    head = text[: text.index("## Scenario A")]
    stop = re.search(r"\*\*STOP\b[^\n]*", head)
    assert stop, "no **STOP rule before the scenarios"
    assert f"`--project {TOY}`" in stop.group(0), "the STOP rule must name `--project harness-toy`"


def test_step4_says_stop_when_the_injected_block_names_another_project() -> None:
    setup = " ".join(_section(_runbook(), "Setup").split())
    assert "names any other project" in setup and "stop" in setup
    assert "--settings" in setup, "step 4 must warn off overriding the project with --settings"


def test_setup_names_the_shipped_template_and_the_resolve_check() -> None:
    setup = " ".join(_section(_runbook(), "Setup").split())
    assert "harness/toy-profile/harness-toy.md" in setup and TEMPLATE.is_file()
    assert f"<config_home>/projects/{TOY}.md" in setup
    assert f"python -m agentflow.config show --project {TOY}" in setup
    assert f"AGENTFLOW_PROJECT={TOY}" in setup  # A and B take the session default
    assert "Removing it" in setup


def _live_plan_launches() -> list[tuple[str, str]]:
    """(launch line, plan fixture filename) for every live launch that hands over a plan."""
    out = []
    for line in _launch_lines(_runbook()):
        out.extend((line, name) for name in re.findall(r"fixtures/(plan-[\w.-]+\.md)", line))
    return out


def test_every_live_plan_fixture_names_its_target_tree() -> None:
    """A plan a live scenario launches must give the orchestrator a tree to build in.

    With neither `worktree:` nor `project:` the orchestrator cannot resolve its target tree
    and escalates instead of running the scenario, and its board and mirror are unanchored.
    `project:` is the field a tracked fixture can carry (`worktree:` would be a machine
    path), and it must name the project the same launch line passes. The fixture set is
    read from the launch lines, so a parse-only negative fixture is never held to this.
    """
    launches = _live_plan_launches()
    assert launches, "no live launch line hands over a fixtures/plan-*.md - the pin examines nothing"
    for line, name in launches:
        fixture = FIXTURES / name
        assert fixture.is_file(), f"{line!r} launches {name}, which is not in harness/fixtures/"
        plan = parse_plan(fixture)
        m = re.search(r"--project\s+(\S+)", line)
        assert m, f"{line!r} launches {name} without naming a project"
        launched = m.group(1)
        assert plan.project == launched, (
            f"{name} declares project {plan.project!r}, but {line!r} launches it as {launched!r}:"
            " with no `project:` the orchestrator has no target tree to resolve"
        )
        assert not plan.worktree, f"{name} carries `worktree: {plan.worktree}` - a machine path"


def test_config_key_template_name_and_seed_agree() -> None:
    cfg = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    assert cfg["toy_project"] == TOY
    assert re.search(rf'^name: "{TOY}"$', TEMPLATE.read_text(encoding="utf-8"), re.M)
    first = SEED_REGISTRY.read_text(encoding="utf-8").splitlines()[0]
    assert TOY in first and "toy-project" not in first


def _config_home(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A throwaway config home whose environment the resolver accepts, plus a fake clone."""
    root = tmp_path.as_posix()
    home = tmp_path / "config"
    (home / "projects").mkdir(parents=True)
    (home / "environment.md").write_text(
        "# environment\n\n```yaml\n"
        "schema_version: 1\n"
        "person:\n  name: Sample User\n"
        f"data_root: {root}\n"
        "git_host:\n  kind: local\n"
        "notify:\n  kind: none\n"
        f"lock_dir: {root}\n"
        "shell: posix\n"
        f"suite_root: {root}\n"
        f"worktree_root: {root}\n"
        "```\n",
        encoding="utf-8",
    )
    clone = tmp_path / "clone"
    (clone / "harness" / "toy-repo").mkdir(parents=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    return home, clone


def test_installed_template_validates_under_the_real_resolver(tmp_path, monkeypatch) -> None:
    home, clone = _config_home(tmp_path, monkeypatch)
    text = TEMPLATE.read_text(encoding="utf-8")
    assert PLACEHOLDER in text
    (home / "projects" / f"{TOY}.md").write_text(
        text.replace(PLACEHOLDER, clone.as_posix()), encoding="utf-8"
    )
    proj = load_project(TOY)
    assert proj.name == TOY
    assert Path(proj.canonical_repo.local_checkout) == clone / "harness" / "toy-repo"
    assert proj.canonical_repo.base_branch == "main"
    assert proj.verification.strategy == "test-suite"
    assert proj.work_source.kind == "none"


def test_an_uninstalled_placeholder_fails_loudly(tmp_path, monkeypatch) -> None:
    home, _ = _config_home(tmp_path, monkeypatch)
    (home / "projects" / f"{TOY}.md").write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_project(TOY)
    assert "canonical_repo.local_checkout" in str(exc.value)


# --- gh#1105: the seed gets Scenario B past the orchestrator's Step 1.1 ---------------------
#
# Step 1.1 pre-flights `<worktree_root>/<project>/<slug>` and checks that its origin resolves
# to `canonical_repo.host_ref`. The seed used to make neither, so every Scenario B run ended in
# `ESCALATION: Worktree mandate`. Every case seeds into tmp_path: the real toy repo, origin and
# worktree root are never touched.

PLAN_FIXTURE = FIXTURES / "plan-single-task.md"


def _load_script(name: str, path: Path):
    """Import a harness script by path; it is a script, not a package module."""
    assert path.is_file(), f"{path} not found - moved, or this path is stale"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves its module out of sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seeder():
    return _load_script("agentflow_harness_seed", HARNESS / "seed_toy_repo.py")


@pytest.fixture(scope="module")
def check():
    return _load_script("agentflow_harness_check_toy", HARNESS / "check.py")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _host_ref() -> str:
    return _load_first_yaml_block(TEMPLATE)["canonical_repo"]["host_ref"]


@pytest.fixture()
def seeded(seeder, tmp_path):
    toy = tmp_path / "toy-repo"
    sha, wt = seeder.seed(
        toy_dir=toy, origin_root=tmp_path / "toy-origin", worktree_root=tmp_path / "wt", force=False
    )
    return toy, sha, wt, tmp_path


def test_seeded_origin_is_host_ref_under_doctors_rule(seeded) -> None:
    toy, _, _, _ = seeded
    url = _git(toy, "remote", "get-url", "origin").stdout.strip()
    assert url.startswith("file://"), f"origin must be a local file:// URL, never a network one: {url}"
    host_ref = _host_ref()
    assert doctor._same_repo(doctor._origin_ref(url), host_ref), (
        f"origin {url} resolves to {doctor._origin_ref(url)!r}, not host_ref {host_ref!r}"
    )
    finding = doctor._check_origin(toy, host_ref)
    assert finding.status == doctor.OK, f"doctor's origin check: {finding}"


def test_the_seed_sha_is_unmoved_by_the_origin_and_worktree(seeder, seeded) -> None:
    _, sha, _, _ = seeded
    assert sha == seeder.EXPECTED_SEED_SHA


def test_seed_creates_the_worktree_where_step_1_1_looks(seeded) -> None:
    toy, sha, wt, tmp_path = seeded
    plan = parse_plan(PLAN_FIXTURE)
    expected = tmp_path / "wt" / plan.project / plan.slug
    assert wt == expected and wt.is_dir(), f"worktree at {wt}, Step 1.1 looks at {expected}"
    top = _git(wt, "rev-parse", "--show-toplevel")
    assert top.returncode == 0 and Path(top.stdout.strip()).samefile(wt), top.stderr
    # Step 1.1 reads the origin from the worktree, not from the shared checkout.
    url = _git(wt, "remote", "get-url", "origin").stdout.strip()
    assert doctor._same_repo(doctor._origin_ref(url), _host_ref())
    assert _git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == plan.slug
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == sha
    # --no-track: Step 7's `push -u origin <epic-branch>` sets the upstream, not the seed.
    assert _git(wt, "rev-parse", "--abbrev-ref", f"{plan.slug}@{{upstream}}").returncode != 0


def test_reseed_refuses_without_force_and_rebuilds_the_worktree_with_it(seeder, seeded) -> None:
    toy, sha, wt, tmp_path = seeded
    kwargs = dict(toy_dir=toy, origin_root=tmp_path / "toy-origin", worktree_root=tmp_path / "wt")
    with pytest.raises(FileExistsError):
        seeder.seed(force=False, **kwargs)
    (wt / "stray.txt").write_text("recorded state\n", encoding="utf-8")
    sha2, wt2 = seeder.seed(force=True, **kwargs)
    assert (sha2, wt2) == (sha, wt)
    assert not (wt / "stray.txt").exists(), "--force kept the old worktree's contents"
    listed = _git(toy, "worktree", "list", "--porcelain").stdout
    assert listed.count("worktree ") == 2, f"expected the checkout plus one epic tree:\n{listed}"


def test_force_refuses_a_directory_that_is_not_the_toy_repos_worktree(seeder, tmp_path) -> None:
    plan = parse_plan(PLAN_FIXTURE)
    foreign = tmp_path / "wt" / plan.project / plan.slug
    foreign.mkdir(parents=True)
    (foreign / "keep.txt").write_text("not the seed's\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        seeder.seed(
            toy_dir=tmp_path / "toy-repo", origin_root=tmp_path / "toy-origin",
            worktree_root=tmp_path / "wt", force=True,
        )
    assert (foreign / "keep.txt").is_file(), "--force deleted a directory it did not create"


def test_a_relative_worktree_root_lands_outside_the_toy_repo(seeder, tmp_path, monkeypatch) -> None:
    """git runs with cwd=<toy repo>; a relative root must still mean the caller's directory."""
    monkeypatch.chdir(tmp_path)
    toy = tmp_path / "toy-repo"
    _, wt = seeder.seed(
        toy_dir=toy, origin_root=tmp_path / "toy-origin", worktree_root=Path("relwt"), force=False
    )
    plan = parse_plan(PLAN_FIXTURE)
    expected = (tmp_path / "relwt" / plan.project / plan.slug).resolve()
    assert wt == expected and wt.is_dir(), f"worktree at {wt}, expected {expected}"
    assert not (toy / "relwt").exists(), "a relative root put the worktree inside the toy repo"


def test_a_leftover_origin_alone_does_not_need_force(seeder, tmp_path) -> None:
    """The runbook lets harness/toy-origin stay; deleting only the toy repo must reseed plainly."""
    toy = tmp_path / "toy-repo"
    kwargs = dict(toy_dir=toy, origin_root=tmp_path / "toy-origin", worktree_root=None)
    seeder.seed(force=False, **kwargs)
    shutil.rmtree(toy, onerror=seeder._chmod_retry)
    sha, _ = seeder.seed(force=False, **kwargs)
    assert sha == seeder.EXPECTED_SEED_SHA
    assert doctor._check_origin(toy, _host_ref()).status == doctor.OK


def test_no_worktree_root_still_seeds_repo_and_origin(seeder, tmp_path) -> None:
    """CI's bare `python harness/seed_toy_repo.py` has no environment.md: repo + origin only."""
    toy = tmp_path / "toy-repo"
    sha, wt = seeder.seed(toy_dir=toy, origin_root=tmp_path / "toy-origin", worktree_root=None, force=False)
    assert wt is None and sha == seeder.EXPECTED_SEED_SHA
    assert doctor._check_origin(toy, _host_ref()).status == doctor.OK


def test_the_local_origin_is_gitignored(seeder) -> None:
    origin = seeder.origin_dir_for(_host_ref())
    probe = origin / "HEAD"
    out = _git(REPO_ROOT, "check-ignore", "-q", str(probe.relative_to(REPO_ROOT).as_posix()))
    assert out.returncode == 0, f"{origin} is not gitignored - `git add -A` would commit a bare repo"


# --- B2 refuses a recording that never got past Step 1.1 ---------------------------------------

_WT = "/srv/records/toy-wt"


def _b2(check, tmp_path: Path, *, tasks=None, escalations=None, preflight=None):
    from agentflow import status_writer as sw

    recs = tmp_path / "records" / "scenarioB"
    recs.mkdir(parents=True, exist_ok=True)
    sw.write_status(
        recs / "status.md",
        sw.StatusState(
            epic="toy", plan="plan-single-task.md", started="2026-01-01T00:00:00+00:00",
            status="running", current_phase="phase-1", tasks=tasks or {},
            escalations=escalations or [], preflight_audit_status=preflight,
        ),
    )
    (recs / "wave-evidence.json").write_text(json.dumps({"worktree_path": _WT}), encoding="utf-8")
    original = check.RECORDS_DIR
    check.RECORDS_DIR = tmp_path / "records"
    try:
        return check.b2_worktree(object())
    finally:
        check.RECORDS_DIR = original


def _task(state: str):
    from agentflow import status_writer as sw

    return {1: sw.TaskState(state=state, phase="phase-1")}


def _mandate(answer=None, heading="ESCALATION: Worktree mandate"):
    from agentflow import status_writer as sw

    question = (
        f"{heading}.\nThis epic must run in <worktree>, and that path is not "
        "a usable worktree of example/harness-toy."
    )
    return [sw.Escalation(id="e1", task=0, asked="2026-01-01T00:00:00+00:00", question=question, answer=answer)]


def test_b2_passes_a_run_that_reached_step_2(check, tmp_path) -> None:
    result = _b2(check, tmp_path, tasks=_task("done"), preflight="clean")
    assert result.status == check.PASS, result.detail
    assert "Step 2" in result.detail


def test_b2_fails_a_run_stopped_at_the_worktree_mandate(check, tmp_path) -> None:
    result = _b2(check, tmp_path, tasks=_task("pending"), escalations=_mandate())
    assert result.status == check.FAIL, f"B2 passed a Step 1.1 stop: {result.detail}"
    assert "Step 1.1" in result.detail and "Worktree mandate" in result.detail


def test_b2_fails_a_run_stopped_at_an_unresolvable_target_tree(check, tmp_path) -> None:
    escalations = _mandate(heading="ESCALATION: Target tree unresolvable")
    result = _b2(check, tmp_path, tasks=_task("pending"), escalations=escalations)
    assert result.status == check.FAIL, f"B2 passed a Step 1.1 stop: {result.detail}"
    assert "Step 1.1" in result.detail and "Target tree unresolvable" in result.detail


def test_b2_fails_a_run_with_no_step_2_write(check, tmp_path) -> None:
    result = _b2(check, tmp_path, tasks=_task("pending"))
    assert result.status == check.FAIL, f"B2 passed a run with no Step 2 evidence: {result.detail}"
    assert "Step 2" in result.detail


def test_b2_accepts_an_answered_mandate_once_the_run_moved_on(check, tmp_path) -> None:
    """Control: an operator fixed the tree, answered, and the run went on to build."""
    result = _b2(check, tmp_path, tasks=_task("done"), escalations=_mandate("recreated"))
    assert result.status == check.PASS, result.detail
