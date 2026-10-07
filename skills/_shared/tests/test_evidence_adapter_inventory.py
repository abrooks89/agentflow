"""The evidence adapter inventory: every outside-world read in the migrating modules returns `Reading`.

`skills/_shared/EVIDENCE.md` says empty, unavailable, truncated and stale are different answers.
`agentflow.evidence.Reading` is how a function says which one it got. This file lists, by name,
every function in the sixteen modules the evidence migration covers that reads the outside world
(`gh`, git, HTTP, ntfy, or files another seat or process writes), and asserts that each one is
annotated to return `Reading[...]` unless it is in `UNMIGRATED`.

WHAT IS AN ADAPTER
* A function that INTERPRETS a read: it decides what a failure, a short answer or a missing file
  means for its caller. That is where a failure becomes a false `empty`, so that is where
  `Reading` belongs. A runner that itself turns a failure into a default (``_run_git`` returning
  ``None``) is an interpreter and is listed.
* A TRANSPORT is not: it starts the process or sends the request and hands back the raw result,
  or raises. Those are named in `TRANSPORTS` so the exclusion is a decision rather than an
  omission; moving one onto `Reading` would put the state in the wrong layer.
* A read written inline in a larger function cannot be listed by name. It is named in
  `PENDING_EXTRACTION` until its epic extracts it into a function and lists that in `ADAPTERS`.

HOW THE RATCHET WORKS
* `ADAPTERS` is the inventory. A name that stops resolving (removed or renamed) fails, so the
  list cannot silently describe code that is gone. Its length is pinned to `ADAPTER_BASELINE`,
  so an adapter cannot leave the inventory to escape the check, and adding one moves the
  baseline in plain view.
* `UNMIGRATED` is the allowlist. Its length is pinned to `UNMIGRATED_BASELINE`, which may only
  fall: lower it in the same change that migrates a function. Pinned exactly rather than capped,
  so a migration that forgets to lower it fails instead of leaving room for a later addition.
* Removing a name from `UNMIGRATED` without migrating the function fails, because the function
  is still in `ADAPTERS` and still not annotated `Reading`. Leaving a migrated function in
  `UNMIGRATED` also fails, so the allowlist cannot outlive its reason.
* The migration is done when `UNMIGRATED` and `PENDING_EXTRACTION` are both empty.

WHAT IT DOES NOT PROVE
* That the inventory is complete. The list was built by reading the sixteen modules for
  subprocess, HTTP and file reads; a new adapter added later is caught only if someone lists it.
* That a function annotated `Reading` returns the right state. Each module's own tests own that.
* Anything about modules outside the sixteen. Other modules also call git or gh, so an empty
  allowlist means the sixteen are migrated, not that the rule is enforced everywhere.

It imports the modules and reads annotations; it never walks the filesystem.
"""

from __future__ import annotations

import importlib
import inspect
import re
import typing

import pytest

from agentflow.evidence import Reading

#: The sixteen modules the three migration epics move, keyed by the epic that owns each.
MIGRATION_MODULES = {
    "evidence-registry-reads": ("registry",),
    "evidence-gates-and-reports": (
        "issue_intake",
        "epic_survey",
        "refs",
        "use_case_impact",
        "fleet_report",
        "stream_report",
    ),
    "evidence-edge-modules": (
        "backoff",
        "backups",
        "config",
        "ntfy",
        "publish",
        "save_summariser",
        "wake_watch",
        "wiki",
        "migrate_queue_class",
    ),
}

#: Modules in the migration set with no outside-world read of their own, and why. A module here
#: that gains an adapter must move it into ADAPTERS.
NO_ADAPTER = {
    "stream_report": (
        "formats the stdout/stderr a caller already captured; it runs nothing and reads nothing. "
        "Its migration work is rendering coverage, which the gates-and-reports epic owns."
    ),
}

#: Every outside-world read in the migration modules, as `module:function`.
ADAPTERS = (
    # registry: the registry file and the edit-lock holder another seat wrote
    "registry:_read_registry_text",
    "registry:read_registry_checked",
    "registry:_lock_holder",
    "registry:_lock_age",
    # issue_intake: the tracker CLI, the spec corpus and the board
    "issue_intake:list_open_issues",
    "issue_intake:list_closed_issues",
    "issue_intake:list_merged_prs",
    "issue_intake:_view_json",
    "issue_intake:spec_linked_issue_numbers",
    "issue_intake:done_spec_links",
    "issue_intake:read_queue_rows",
    "issue_intake:_claimed_refs",
    "issue_intake:read_board",
    # epic_survey: specs, plans and status boards other seats write
    "epic_survey:survey_epics",
    "epic_survey:_rwalk_match",
    "epic_survey:_stat_status_boards",
    "epic_survey:is_orchestrator_status",
    "epic_survey:_opens_a_frontmatter_block",
    "epic_survey:warn_unresolved_done_spec_paths",
    "epic_survey:handoff_path",
    # refs: gh and git ref state
    "refs:merged_pr_heads",
    "refs:live_remote_heads",
    "refs:local_tracking_refs",
    "refs:fetch_prune_enabled",
    "refs:nonstandard_refspecs",
    "refs:local_branch_tips",
    "refs:worktree_branches",
    "refs:_branches_held_by_operations",
    "refs:checked_out_branches",
    # use_case_impact: the use-case registry, the tree and git history
    "use_case_impact:read_registry_row",
    "use_case_impact:_scan_tree",
    "use_case_impact:_run_git",
    "use_case_impact:_git_log_trailers",
    "use_case_impact:_git_grep",
    "use_case_impact:_resolve_project_name",
    # fleet_report: every source the fleet report prints
    "fleet_report:find_latest_archive",
    "fleet_report:load_prior_snapshot",
    "fleet_report:_branch_drift",
    "fleet_report:_worktree_drift",
    "fleet_report:_registered_worktrees",
    "fleet_report:_read_registry",
    "fleet_report:_read_cull_log",
    "fleet_report:_spec_index",
    "fleet_report:_read",
    "fleet_report:fingerprint_tree",
    "fleet_report:_merged_prs",
    "fleet_report:_open_prs",
    "fleet_report:_read_held_board",
    # backoff: the shared still-log and the lane state it gathers
    "backoff:_tail_rows",
    "backoff:still_run",
    "backoff:gather_marker",
    # backups: the backup store
    "backups:backup_group",
    "backups:iter_backups",
    "backups:plan_reap",
    "backups:_explicit_slug",
    "backups:_live_slug_index",
    # config: profiles and project files on disk (the loaders stay fail-closed; this is the read)
    "config:_load_first_yaml_block",
    "config:resolve_project_name",
    "config:configured_project_names",
    "config:find_vault_candidates",
    "config:_autodoc_foreign_project",
    "config:_autodoc_has_corpus",
    "config:_autodoc_has_manifest",
    "config:resolve_autodoc_root",
    "config:_declared_requirements",
    "config:_requirements_lines",
    # ntfy: HTTP, and the config that names where to send
    "ntfy:_resolved_notify",
    "ntfy:notify",
    # publish: git in the source and cut repos, and the local ledger and terms files
    "publish:_ls_tree",
    "publish:_read_blobs",
    "publish:check_ledger",
    "publish:_read_sensitive_terms",
    "publish:_load_sensitive_terms",
    "publish:_load_cut",
    "publish:check_cut_shape",
    # save_summariser: the one-shot's reply
    "save_summariser:compose_and_save",
    # wake_watch: the board, read for its timers
    "wake_watch:watch",
    # wiki: the pillar store
    "wiki:read_pages",
    "wiki:_live_page_files",
    "wiki:_read_one_page",
    "wiki:missing_pillar_markers",
    "wiki:_missing_hot_markers",
    # migrate_queue_class: the spec corpus
    "migrate_queue_class:survey_parsed_files",
    "migrate_queue_class:plan_migration",
)

#: Adapters not yet returning `Reading`. May only shrink; the program is done when it is empty.
UNMIGRATED = frozenset(
    {
        "registry:_read_registry_text",
        "registry:read_registry_checked",
        "registry:_lock_holder",
        "registry:_lock_age",
        "issue_intake:list_open_issues",
        "issue_intake:list_closed_issues",
        "issue_intake:list_merged_prs",
        "issue_intake:_view_json",
        "issue_intake:spec_linked_issue_numbers",
        "issue_intake:done_spec_links",
        "issue_intake:read_queue_rows",
        "issue_intake:_claimed_refs",
        "issue_intake:read_board",
        "epic_survey:survey_epics",
        "epic_survey:_rwalk_match",
        "epic_survey:_stat_status_boards",
        "epic_survey:is_orchestrator_status",
        "epic_survey:_opens_a_frontmatter_block",
        "epic_survey:warn_unresolved_done_spec_paths",
        "epic_survey:handoff_path",
        "refs:merged_pr_heads",
        "refs:live_remote_heads",
        "refs:local_tracking_refs",
        "refs:fetch_prune_enabled",
        "refs:nonstandard_refspecs",
        "refs:local_branch_tips",
        "refs:worktree_branches",
        "refs:_branches_held_by_operations",
        "refs:checked_out_branches",
        "use_case_impact:read_registry_row",
        "use_case_impact:_scan_tree",
        "use_case_impact:_run_git",
        "use_case_impact:_git_log_trailers",
        "use_case_impact:_git_grep",
        "use_case_impact:_resolve_project_name",
        "fleet_report:find_latest_archive",
        "fleet_report:load_prior_snapshot",
        "fleet_report:_branch_drift",
        "fleet_report:_worktree_drift",
        "fleet_report:_registered_worktrees",
        "fleet_report:_read_registry",
        "fleet_report:_read_cull_log",
        "fleet_report:_spec_index",
        "fleet_report:_read",
        "fleet_report:fingerprint_tree",
        "fleet_report:_merged_prs",
        "fleet_report:_open_prs",
        "fleet_report:_read_held_board",
        "backoff:_tail_rows",
        "backoff:still_run",
        "backoff:gather_marker",
        "backups:backup_group",
        "backups:iter_backups",
        "backups:plan_reap",
        "backups:_explicit_slug",
        "backups:_live_slug_index",
        "config:_load_first_yaml_block",
        "config:resolve_project_name",
        "config:configured_project_names",
        "config:find_vault_candidates",
        "config:_autodoc_foreign_project",
        "config:_autodoc_has_corpus",
        "config:_autodoc_has_manifest",
        "config:resolve_autodoc_root",
        "config:_declared_requirements",
        "config:_requirements_lines",
        "ntfy:_resolved_notify",
        "ntfy:notify",
        "publish:_ls_tree",
        "publish:_read_blobs",
        "publish:check_ledger",
        "publish:_read_sensitive_terms",
        "publish:_load_sensitive_terms",
        "publish:_load_cut",
        "publish:check_cut_shape",
        "save_summariser:compose_and_save",
        "wake_watch:watch",
        "wiki:read_pages",
        "wiki:_live_page_files",
        "wiki:_read_one_page",
        "wiki:missing_pillar_markers",
        "wiki:_missing_hot_markers",
        "migrate_queue_class:survey_parsed_files",
        "migrate_queue_class:plan_migration",
    }
)

#: Committed size of UNMIGRATED, pinned exactly. Lower it in the change that migrates an entry;
#: never raise it.
UNMIGRATED_BASELINE = 84

#: Committed size of ADAPTERS, pinned exactly. An adapter leaves the inventory only when its
#: function is deleted, and then this baseline drops in the same change, in plain view.
ADAPTER_BASELINE = 84

#: Transports in the migration modules: they run the process or send the request and return the
#: raw result or raise, so the function that interprets the result is the adapter, not these.
TRANSPORTS = (
    "issue_intake:_subprocess_runner",
    "fleet_report:_default_git_runner",
    "refs:_git",
    "publish:_run",
    "publish:_git",
    "save_summariser:_subprocess_runner",
    "ntfy:_urlopen_sender",
)

#: Reads written inline in a larger function, as `module:host_function` -> what the read is. Each
#: one's epic extracts it into a function, lists that in ADAPTERS and UNMIGRATED (or migrates it),
#: and removes it here.
PENDING_EXTRACTION = {
    "registry:_run_mutation": (
        "the registry-log read after a mutation: a log file that does not exist becomes the "
        "empty string"
    ),
}

#: Committed size of PENDING_EXTRACTION, pinned exactly like UNMIGRATED_BASELINE.
PENDING_EXTRACTION_BASELINE = 1

_READING_ANNOTATION = re.compile(r"^(?:[A-Za-z_][\w.]*\.)?Reading(?:\[.*\])?$", re.DOTALL)


def _resolve(name: str):
    module_name, _, attr = name.partition(":")
    module = importlib.import_module(f"agentflow.{module_name}")
    obj = module
    for part in attr.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def returns_reading(fn) -> bool:
    """True when ``fn``'s return annotation is `Reading` or `Reading[...]`, as a string or a type."""
    fn = inspect.unwrap(fn)
    annotation = getattr(fn, "__annotations__", {}).get("return", inspect.Signature.empty)
    if annotation is inspect.Signature.empty:
        return False
    if isinstance(annotation, str):
        return bool(_READING_ANNOTATION.match(annotation.strip().strip("'\"")))
    return annotation is Reading or typing.get_origin(annotation) is Reading


def _all_modules() -> list[str]:
    return [m for modules in MIGRATION_MODULES.values() for m in modules]


def size_drift(label: str, actual: int, baseline: int, *, shrinks_only: bool) -> str | None:
    """Why ``actual`` does not match its committed ``baseline``, or None when it does."""
    if actual == baseline:
        return None
    if actual > baseline:
        if shrinks_only:
            return (
                f"{label} has {actual} entries, over its committed baseline of {baseline}; "
                "it may only shrink"
            )
        return (
            f"{label} has {actual} entries, {baseline} committed; raise the baseline in this "
            "change, in plain view"
        )
    return (
        f"{label} has {actual} entries, under its committed baseline of {baseline}; lower the "
        "baseline in the same change, so the room left cannot be refilled later"
    )


# ---------------------------------------------------------------------------
# The inventory describes code that exists
# ---------------------------------------------------------------------------


def test_the_migration_set_is_the_sixteen_modules():
    modules = _all_modules()
    assert len(modules) == 16 and len(set(modules)) == 16, modules


def test_every_migration_module_has_adapters_or_a_reason_it_has_none():
    listed = {name.partition(":")[0] for name in ADAPTERS}
    for module in _all_modules():
        assert (module in listed) != (module in NO_ADAPTER), (
            f"{module}: must be either inventoried in ADAPTERS or explained in NO_ADAPTER, not both or neither"
        )
    assert set(NO_ADAPTER) <= set(_all_modules())
    assert listed <= set(_all_modules()), (
        f"ADAPTERS names modules outside the migration set: {listed - set(_all_modules())}"
    )


@pytest.mark.parametrize("module", sorted(NO_ADAPTER))
def test_a_no_adapter_module_still_starts_nothing(module):
    mod = importlib.import_module(f"agentflow.{module}")
    for handle in ("subprocess", "urlopen", "urllib", "Request"):
        assert not hasattr(mod, handle), f"{module} now imports {handle}; inventory its adapter"


def test_the_inventory_has_no_duplicates():
    assert len(ADAPTERS) == len(set(ADAPTERS))


@pytest.mark.parametrize("name", ADAPTERS)
def test_every_inventoried_adapter_exists(name):
    obj = _resolve(name)
    assert obj is not None, (
        f"{name} no longer exists; a removed or renamed adapter must be updated here"
    )
    assert callable(obj), f"{name} is not a function"


def test_the_inventory_size_matches_its_baseline():
    problem = size_drift("ADAPTERS", len(ADAPTERS), ADAPTER_BASELINE, shrinks_only=False)
    assert problem is None, (
        f"{problem}; an adapter leaves the inventory only when its function is deleted"
    )


@pytest.mark.parametrize("name", TRANSPORTS)
def test_every_named_transport_exists_and_is_not_inventoried(name):
    assert name.partition(":")[0] in _all_modules(), f"{name}: outside the migration set"
    obj = _resolve(name)
    assert obj is not None and callable(obj), f"{name} no longer exists; update TRANSPORTS"
    assert name not in ADAPTERS, (
        f"{name} is a transport; inventory the function that interprets its result instead"
    )


@pytest.mark.parametrize("host", sorted(PENDING_EXTRACTION))
def test_every_pending_extraction_names_a_live_host(host):
    assert host.partition(":")[0] in _all_modules(), f"{host}: outside the migration set"
    obj = _resolve(host)
    assert obj is not None and callable(obj), (
        f"{host} no longer exists; list the extracted read in ADAPTERS and remove it here"
    )
    assert PENDING_EXTRACTION[host].strip(), f"{host}: say which read is pending"


def test_pending_extractions_match_their_baseline():
    problem = size_drift(
        "PENDING_EXTRACTION",
        len(PENDING_EXTRACTION),
        PENDING_EXTRACTION_BASELINE,
        shrinks_only=True,
    )
    assert problem is None, problem


# ---------------------------------------------------------------------------
# The allowlist only shrinks, and only by migration
# ---------------------------------------------------------------------------


def test_the_allowlist_matches_its_baseline():
    problem = size_drift("UNMIGRATED", len(UNMIGRATED), UNMIGRATED_BASELINE, shrinks_only=True)
    assert problem is None, problem


@pytest.mark.parametrize(
    "actual, baseline, needle",
    [
        (82, 82, None),
        # Migrated one but left the baseline: that slack is what a later addition would refill.
        (81, 82, "lower the baseline"),
        (83, 82, "may only shrink"),
    ],
)
def test_the_allowlist_ratchet_is_exact(actual, baseline, needle):
    problem = size_drift("UNMIGRATED", actual, baseline, shrinks_only=True)
    if needle is None:
        assert problem is None
    else:
        assert problem is not None and needle in problem


def test_the_inventory_ratchet_moves_only_with_its_baseline():
    assert "raise the baseline" in size_drift("ADAPTERS", 85, 84, shrinks_only=False)
    assert "lower the baseline" in size_drift("ADAPTERS", 83, 84, shrinks_only=False)
    assert size_drift("ADAPTERS", 84, 84, shrinks_only=False) is None


def test_every_allowlisted_name_is_inventoried():
    assert UNMIGRATED <= set(ADAPTERS), (
        f"allowlisted but not inventoried: {sorted(UNMIGRATED - set(ADAPTERS))}"
    )


@pytest.mark.parametrize("name", ADAPTERS)
def test_every_adapter_returns_a_reading_unless_allowlisted(name):
    obj = _resolve(name)
    assert obj is not None, f"{name} no longer exists"
    migrated = returns_reading(obj)
    if name in UNMIGRATED:
        assert not migrated, (
            f"{name} returns Reading now; remove it from UNMIGRATED and lower the baseline"
        )
    else:
        assert migrated, (
            f"{name} is off the UNMIGRATED allowlist but is not annotated to return Reading[...]"
        )


# ---------------------------------------------------------------------------
# The annotation detector itself
# ---------------------------------------------------------------------------


def _string_annotated(ret: str):
    def f():
        return None

    f.__annotations__ = {"return": ret}
    return f


@pytest.mark.parametrize(
    "ret, expected",
    [
        ("Reading", True),
        ("Reading[list[str]]", True),
        ("evidence.Reading[dict[str, int]]", True),
        ("agentflow.evidence.Reading[tuple]", True),
        ("'Reading[int]'", True),
        ("Optional[Reading[int]]", False),
        ("list[Reading[int]]", False),
        ("ReadingList", False),
        ("tuple", False),
    ],
)
def test_detector_reads_string_annotations(ret, expected):
    assert returns_reading(_string_annotated(ret)) is expected


def test_detector_reads_evaluated_annotations_and_missing_ones():
    def typed() -> Reading[int]:  # evaluated lazily under __future__; set explicitly below
        raise NotImplementedError

    typed.__annotations__ = {"return": Reading[int]}
    assert returns_reading(typed)

    def bare():
        return None

    assert not returns_reading(bare)

    def plain() -> int:
        return 0

    plain.__annotations__ = {"return": int}
    assert not returns_reading(plain)
