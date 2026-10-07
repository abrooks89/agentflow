"""Tests for agentflow.config — the hard-fail-loud typed-YAML resolver (P1-T2).

Covers each trigger from the acceptance criteria: missing required key, wrong type,
bad enum, unmet R*, missing required path, named env-var absent, delegated presence
fail, name!=filename — plus optional-default resolution, first-yaml-block extraction,
and secret-name-only reads.
"""

import ast
import json
import re
import textwrap
from pathlib import Path

import pytest

from agentflow import config
from agentflow.config import ConfigError, load_environment, load_project


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _write_env(home: Path, body: str, *, extra: str = "") -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "environment.md").write_text(
        f"# environment\n\n```yaml\n{textwrap.dedent(body).strip()}\n```\n{extra}",
        encoding="utf-8",
    )


def _write_project(home: Path, name: str, body: str) -> None:
    pdir = home / "projects"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{name}.md").write_text(
        f"# project {name}\n\n```yaml\n{textwrap.dedent(body).strip()}\n```\n",
        encoding="utf-8",
    )


def _valid_env_body(d: str) -> str:
    return f"""
    schema_version: 1
    person:
      name: Sample User
    data_root: {d}
    git_host:
      kind: local
    notify:
      kind: none
    lock_dir: {d}
    shell: posix
    suite_root: {d}
    worktree_root: {d}
    """


def _valid_project_body(d: str) -> str:
    return f"""
    schema_version: 1
    name: foo
    canonical_repo:
      host_ref: owner/repo
      local_checkout: {d}
    verification:
      strategy: test-suite
    """


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "config"
    h.mkdir()
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(h))
    return h


@pytest.fixture
def d(tmp_path):
    # an existing directory usable for every required path field
    return str(tmp_path).replace("\\", "/")


# --------------------------------------------------------------------------- #
# Environment — happy path + defaults
# --------------------------------------------------------------------------- #
def test_valid_environment_and_defaults(home, d):
    _write_env(home, _valid_env_body(d))
    env = load_environment()
    assert env.schema_version == 1
    assert env.person.name == "Sample User"
    assert env.git_host.cli == "none"          # default for kind != github
    assert env.notify.kind == "none"
    assert env.notify.ascii_only is True
    assert env.mtime_cutoff_iso == config.DEFAULT_MTIME_CUTOFF
    assert env.question_style == config.DEFAULT_QUESTION_STYLE  # two turns is the default
    assert env.question_style == "mobile-two-turn"
    assert env.editor_cmd == "${EDITOR:-code}"
    assert env.status_board_url_template is None
    assert env.vault is None
    assert env.parallel_builders.enabled is False
    assert env.parallel_builders.count == 1


def test_question_style_inline_is_accepted_as_the_opt_out(home, d):
    # Asserts on `inline`, not the default: under the two-turn default a test that set
    # `mobile-two-turn` would pass without reading the key at all.
    body = _valid_env_body(d) + "\n    question_style: inline"
    _write_env(home, body)
    assert load_environment().question_style == "inline"


def test_retired_person_mobile_default_is_rejected_by_name(home, d):
    # Nothing read it, so a leftover line must fail loudly rather than pretend to work.
    body = _valid_env_body(d).replace(
        "  name: Sample User\n", "  name: Sample User\n      mobile_default: true\n"
    )
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "person.mobile_default"


def test_question_style_bad_enum_is_loud(home, d):
    body = _valid_env_body(d) + "\n    question_style: sms"
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "question_style"


def test_github_defaults_cli_to_gh(home, d):
    body = _valid_env_body(d).replace("kind: local", "kind: github")
    _write_env(home, body)
    assert load_environment().git_host.cli == "gh"


def test_first_yaml_block_wins(home, d):
    # a broken SECOND yaml block must be ignored; only the first is parsed
    _write_env(home, _valid_env_body(d), extra="\n```yaml\n: : : not valid\n```\n")
    assert load_environment().person.name == "Sample User"


# --------------------------------------------------------------------------- #
# Environment — hard-fail triggers
# --------------------------------------------------------------------------- #
def test_missing_required_key(home, d):
    body = _valid_env_body(d).replace(f"    data_root: {d}\n", "")
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "data_root"


def test_wrong_type(home, d):
    body = _valid_env_body(d).replace("schema_version: 1", "schema_version: one")
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "schema_version"


def test_bad_enum(home, d):
    body = _valid_env_body(d).replace("shell: posix", "shell: bash")
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "shell"


def test_missing_required_path(home, d):
    body = _valid_env_body(d).replace(f"data_root: {d}", "data_root: /no/such/dir/agentflow")
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "data_root"


def test_config_file_not_found(home):
    with pytest.raises(ConfigError):
        load_environment()  # no environment.md written


def test_no_yaml_block(home, d):
    home.mkdir(parents=True, exist_ok=True)
    (home / "environment.md").write_text("# just prose, no fence\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_environment()


# --------------------------------------------------------------------------- #
# Environment — notify R* + env-var NAME reads (never values)
# --------------------------------------------------------------------------- #
def test_notify_ntfy_requires_url_env(home, d):
    body = _valid_env_body(d).replace("kind: none", "kind: ntfy")  # first `kind: none` is notify's
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "notify.url_env"


def test_notify_env_name_absent_from_environ(home, d):
    body = _valid_env_body(d).replace(
        "notify:\n      kind: none",
        "notify:\n      kind: ntfy\n      url_env: AF_ABSENT_URL_ENV\n      topic_env: AF_ABSENT_TOPIC_ENV",
    )
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "notify.url_env"


def test_notify_reads_name_not_value(home, d, monkeypatch):
    monkeypatch.setenv("AF_PRESENT_URL_ENV", "http://secret-should-not-be-stored")
    monkeypatch.setenv("AF_PRESENT_TOPIC_ENV", "secret-topic")
    body = _valid_env_body(d).replace(
        "notify:\n      kind: none",
        "notify:\n      kind: ntfy\n      url_env: AF_PRESENT_URL_ENV\n      topic_env: AF_PRESENT_TOPIC_ENV",
    )
    _write_env(home, body)
    env = load_environment()
    assert env.notify.url_env == "AF_PRESENT_URL_ENV"       # NAME kept
    assert env.notify.topic_env == "AF_PRESENT_TOPIC_ENV"
    # the secret VALUE is never carried on the resolved config
    assert "secret-should-not-be-stored" not in str(env.notify.__dict__)


# --------------------------------------------------------------------------- #
# Environment — vault + parallel_builders rules
# --------------------------------------------------------------------------- #
def test_vault_block_requires_root(home, d):
    body = _valid_env_body(d) + "\n    vault:\n      save_skill: null"
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "vault.root"


def test_vault_delegated_skill_missing_is_loud(home, d):
    body = _valid_env_body(d) + f"\n    vault:\n      root: {d}\n      save_skill: /no/such/SKILL.md"
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "vault.save_skill"


def test_parallel_builders_count_gt1_requires_enabled(home, d):
    body = _valid_env_body(d) + "\n    parallel_builders:\n      enabled: false\n      count: 2"
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "parallel_builders.count"


def test_parallel_builders_bad_count(home, d):
    body = _valid_env_body(d) + "\n    parallel_builders:\n      enabled: true\n      count: 4"
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "parallel_builders.count"


# --------------------------------------------------------------------------- #
# Project
# --------------------------------------------------------------------------- #
def test_valid_project(home, d):
    _write_project(home, "foo", _valid_project_body(d))
    proj = load_project("foo")
    assert proj.name == "foo"
    assert proj.canonical_repo.host_ref == "owner/repo"
    assert proj.canonical_repo.base_branch == "main"
    assert proj.canonical_repo.python is None
    assert proj.verification.strategy == "test-suite"
    assert proj.work_source.kind == "filesystem"
    assert proj.staging is None


def test_project_name_must_equal_filename(home, d):
    _write_project(home, "foo", _valid_project_body(d).replace("name: foo", "name: bar"))
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "name"


def test_project_bad_host_ref(home, d):
    _write_project(home, "foo", _valid_project_body(d).replace("host_ref: owner/repo", "host_ref: noslash"))
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "canonical_repo.host_ref"


def test_web_journey_requires_staging_url(home, d):
    _write_project(home, "foo", _valid_project_body(d).replace("strategy: test-suite", "strategy: web-journey"))
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "staging.url"


def test_web_journey_binds_verification_url(home, d):
    body = _valid_project_body(d).replace(
        "verification:\n      strategy: test-suite",
        "staging:\n      url: https://staging.example\n    verification:\n      strategy: web-journey",
    )
    _write_project(home, "foo", body)
    proj = load_project("foo")
    assert proj.verification.url == "https://staging.example"


def test_retired_readiness_gate_block_is_rejected_by_name(home, d):
    # Nothing read it, so a leftover block must fail loudly rather than pretend to work.
    body = _valid_project_body(d) + "\n    readiness_gate:\n      description: x"
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "readiness_gate"


def test_delegated_skill_present_passes(home, d, tmp_path):
    skill = tmp_path / "gen" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("# gen\n", encoding="utf-8")
    body = _valid_project_body(d) + f"\n    generation_skills:\n      provision: {str(skill).replace(chr(92), '/')}"
    _write_project(home, "foo", body)
    proj = load_project("foo")
    assert Path(proj.generation_skills.provision) == skill


def test_verification_strategy_as_missing_skillpath(home, d):
    _write_project(home, "foo", _valid_project_body(d).replace("strategy: test-suite", "strategy: /no/such/SKILL.md"))
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "verification.strategy"


@pytest.mark.parametrize("blank", ['""', '"   "'], ids=["empty", "whitespace"])
def test_a_blank_verification_strategy_is_rejected(home, d, blank):
    # Required delegated path: blank must hard-fail, not read as unset like the optional ones.
    _write_project(home, "foo", _valid_project_body(d).replace("strategy: test-suite", f"strategy: {blank}"))
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "verification.strategy"


@pytest.mark.parametrize("blank", ['""', '"   "'], ids=["empty", "whitespace"])
@pytest.mark.parametrize("field", ["vault.mirror_helper", "generation_skills.polishing",
                                   "generation_skills.provision"])
def test_a_blank_optional_delegated_path_reads_as_unset(home, d, field, blank):
    block, key = field.split(".")
    root = f"\n      root: {d}" if block == "vault" else ""
    _write_project(home, "foo", _valid_project_body(d) + f"\n    {block}:{root}\n      {key}: {blank}")
    proj = load_project("foo")
    assert getattr(getattr(proj, block), key) is None


def test_audit_defaults_when_block_absent(home, d):
    _write_project(home, "foo", _valid_project_body(d))
    proj = load_project("foo")
    assert proj.audit.window == 7
    assert proj.audit.roles == []
    assert proj.audit.idle_wake_minutes == 60
    assert proj.audit.notify is True


def test_audit_block_overrides_are_read(home, d):
    body = _valid_project_body(d) + (
        "\n    audit:"
        "\n      window: 10"
        "\n      roles:"
        "\n        - reviewer"
        "\n      idle_wake_minutes: 30"
        "\n      notify: false"
    )
    _write_project(home, "foo", body)
    proj = load_project("foo")
    assert proj.audit.window == 10
    assert proj.audit.roles == ["reviewer"]
    assert proj.audit.idle_wake_minutes == 30
    assert proj.audit.notify is False


def test_audit_bare_block_resolves_to_defaults(home, d):
    # a bare `audit:` (null body) is legal and resolves entirely to defaults
    body = _valid_project_body(d) + "\n    audit:"
    _write_project(home, "foo", body)
    proj = load_project("foo")
    assert proj.audit.window == 7
    assert proj.audit.notify is True


# gh#722: 0 made every pass the designed-healthy empty pass; a negative slice dropped the
# oldest epics. Neither reddened anything, so the resolver has to be the one that refuses.
@pytest.mark.parametrize("window", [0, -3])
def test_an_audit_window_below_one_is_rejected(home, d, window):
    body = _valid_project_body(d) + f"\n    audit:\n      window: {window}"
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "audit.window"


def test_an_audit_window_of_one_is_the_accepted_boundary(home, d):
    body = _valid_project_body(d) + "\n    audit:\n      window: 1"
    _write_project(home, "foo", body)
    assert load_project("foo").audit.window == 1


def test_an_audit_block_without_window_still_defaults_it(home, d):
    # the check sits in the audit-block branch, so the default must survive it too
    body = _valid_project_body(d) + "\n    audit:\n      notify: false"
    _write_project(home, "foo", body)
    assert load_project("foo").audit.window == 7


def test_an_audit_window_of_true_is_not_read_as_one(home, d):
    # bool is an int subclass; `_opt_int`'s bool guard is what refuses it, pinned here so
    # `true` can never reach the `< 1` check as 1
    body = _valid_project_body(d) + "\n    audit:\n      window: true"
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "audit.window"


def _project_with_page(d: str, page: str) -> str:
    return _valid_project_body(d) + f"\n    work_source:\n      kind: github-issues\n      open_issue_page: {page}"


def test_open_issue_page_defaults_to_200_absent_or_with_a_block(home, d):
    # absent block and a block without the key both keep the old constant's value
    _write_project(home, "foo", _valid_project_body(d))
    assert load_project("foo").work_source.open_issue_page == 200
    _write_project(home, "foo", _valid_project_body(d) + "\n    work_source:\n      kind: github-issues")
    assert load_project("foo").work_source.open_issue_page == 200


@pytest.mark.parametrize("page", [1, 300])
def test_open_issue_page_parses(home, d, page):
    _write_project(home, "foo", _project_with_page(d, str(page)))
    assert load_project("foo").work_source.open_issue_page == page


# The intake refusal's way out is this key, so a bad value must stop the load rather than
# fall back to 200: a silent fallback would make the edit look applied while intake refused.
@pytest.mark.parametrize("page,reason", [
    ("0", "is below 1"), ("-5", "is below 1"),
    ("true", "expected int"), ("'300'", "expected int"), ("2.5", "expected int"),
])
def test_open_issue_page_rejects_non_positive_and_non_int_loudly(home, d, page, reason):
    _write_project(home, "foo", _project_with_page(d, page))
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    # the reason, not just the path: an unknown-key rejection carries the same path
    assert ei.value.field_path == "work_source.open_issue_page"
    assert reason in ei.value.reason


def test_config_error_carries_all_four_fields(home, d):
    body = _valid_env_body(d).replace(f"    data_root: {d}\n", "")
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    e = ei.value
    assert e.field_path and e.file and e.reason and e.remediation


# --------------------------------------------------------------------------- #
# communities block (epic 703 — the escape-hatch pin override)
# --------------------------------------------------------------------------- #
def test_communities_absent_resolves_to_none(home, d):
    _write_project(home, "foo", _valid_project_body(d))
    proj = load_project("foo")
    assert proj.communities is None


def test_communities_pins_are_read(home, d):
    body = _valid_project_body(d) + (
        "\n    communities:"
        "\n      pins:"
        '\n        "legacy/subsys": "Legacy Subsystem"'
        '\n        "tools/scripts": "Tooling"'
    )
    _write_project(home, "foo", body)
    proj = load_project("foo")
    assert proj.communities.pins == {
        "legacy/subsys": "Legacy Subsystem",
        "tools/scripts": "Tooling",
    }


def test_communities_bare_block_resolves_to_empty_pins(home, d):
    body = _valid_project_body(d) + "\n    communities:\n      pins: {}"
    _write_project(home, "foo", body)
    proj = load_project("foo")
    assert proj.communities.pins == {}


def test_communities_pins_wrong_type_is_loud(home, d):
    body = _valid_project_body(d) + (
        "\n    communities:"
        "\n      pins:"
        "\n        - not-a-mapping"
    )
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "communities.pins"


def test_communities_pin_empty_prefix_is_a_catch_all_and_loud(home, d):
    body = _valid_project_body(d) + (
        "\n    communities:"
        "\n      pins:"
        '\n        "": "Everything"'
    )
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "communities.pins"
    assert "catch-all" in ei.value.reason


def test_communities_pin_nonstring_value_is_loud(home, d):
    body = _valid_project_body(d) + (
        "\n    communities:"
        "\n      pins:"
        '\n        "a/b": 7'
    )
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "communities.pins"


def test_project_python_interpreter_optional_and_presence_checked(home, d, tmp_path):
    py = tmp_path / "fake-python"
    py.write_text("")
    body = _valid_project_body(d).replace("local_checkout: " + d, "local_checkout: " + d + "\n      python: " + str(py))
    _write_project(home, "foo", body)
    assert load_project("foo").canonical_repo.python == str(py)
    body = _valid_project_body(d).replace("local_checkout: " + d, "local_checkout: " + d + "\n      python: " + str(tmp_path / "missing"))
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError):
        load_project("foo")


# --------------------------------------------------------------------------- #
# E-0300: project selection by slash argument
#
# The epic exists because the active project was chosen BEFORE any skill ran:
# Claude Code's settings `env` overrides the shell, so `AGENTFLOW_PROJECT=x claude`
# is silently ignored. These gates pin the escape hatch - an explicit argument that
# beats the environment - and pin the CLI to the SAME resolver and renderer the
# SessionStart hook uses, so the two cannot drift apart again.
# --------------------------------------------------------------------------- #
def _named_project(home: Path, name: str, d: str) -> None:
    """`name:` must equal the filename stem (routing integrity, config.py:463)."""
    _write_project(home, name, _valid_project_body(d).replace("name: foo", f"name: {name}"))


def _two_projects(home: Path, d: str) -> None:
    """Two profiles, so the single-stem default can never mask a precedence bug."""
    _named_project(home, "alpha", d)
    _named_project(home, "beta", d)


def test_resolve_project_name_explicit_beats_env(home, d, monkeypatch):
    """THE POINT OF THE EPIC: the argument wins over $AGENTFLOW_PROJECT."""
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.setenv("AGENTFLOW_PROJECT", "alpha")
    assert config.resolve_project_name("beta") == "beta"


def test_resolve_project_name_without_explicit_is_unchanged(home, d, monkeypatch):
    """explicit=None must reproduce the pre-epic order exactly (hook semantics
    are out of scope for E-0300): env var, else single stem, else None."""
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.setenv("AGENTFLOW_PROJECT", "alpha")
    assert config.resolve_project_name(None) == "alpha"
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    # two candidates -> no default; this ambiguity IS the friction the epic names
    assert config.resolve_project_name(None) is None


def test_resolve_project_name_single_stem_default(home, d, monkeypatch):
    _write_env(home, _valid_env_body(d))
    _named_project(home, "solo", d)
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    assert config.resolve_project_name(None) == "solo"


def test_show_cli_explicit_beats_env(home, d, monkeypatch, capsys):
    """End-to-end through main(): argv wins over the environment."""
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.setenv("AGENTFLOW_PROJECT", "alpha")
    assert config.main(["show", "--project", "beta"]) == 0
    out = capsys.readouterr().out
    assert "[project] beta" in out
    assert "[project] alpha" not in out


def test_show_cli_block_matches_renderer_exactly(home, d, monkeypatch, capsys):
    """The CLI must print the renderer's output verbatim - not a re-derived
    lookalike. A test that rebuilt the format itself would gate nothing."""
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    assert config.main(["show", "--project", "alpha"]) == 0
    printed = capsys.readouterr().out
    expected = config.render_resolved_block(load_environment(), load_project("alpha"))
    assert printed == expected + "\n"


def test_show_cli_unknown_project_hard_fails(home, d, monkeypatch, capsys):
    """Hard-fail loud, never partial - same ConfigError shape as the hook."""
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    rc = config.main(["show", "--project", "nope-does-not-exist"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "remediation:" in captured.err


def test_render_resolved_block_shape_is_pinned(home, d, monkeypatch):
    """Golden-ish structural pin. The hook injects this block verbatim and every
    loop skill parses it, so silent reformatting is a breaking change."""
    _write_env(home, _valid_env_body(d))
    _named_project(home, "alpha", d)
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    block = config.render_resolved_block(load_environment(), load_project("alpha"))
    lines = block.split("\n")
    assert lines[0] == "=== agentflow resolved config ==="
    assert lines[-1] == "=" * len(lines[0])
    assert "  data_root    = " in block   # 2-space indent, key padded to 12
    assert "  worktree_root = " in block  # longer than the pad, so it sets its own column
    assert "[active builders] enabled: " in block
    assert any(ln.startswith("[question_style] ") for ln in lines)
    assert "[project] alpha (repo: " in block
    # gh#624: the skills fill these placeholders "off the resolved block", so each is
    # printed under [project] and spelled exactly as the placeholder it fills.
    at = lines.index(next(ln for ln in lines if ln.startswith("[project] alpha")))
    assert lines[at + 1] == f"  canonical_repo.local_checkout = {Path(d)}"
    assert lines[at + 2] == "  base-branch = main"
    assert lines[at + 3] == "  vault.mirror_helper = (unset)"
    assert "deprecated_mirror" not in block  # only when set


def _mirror_script(d: str, name: str) -> str:
    p = Path(d) / name
    p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(0o755)
    return str(p).replace("\\", "/")


def test_resolved_block_prints_base_branch_deprecated_mirror_and_mirror_helper(home, d):
    """gh#624. Set values render verbatim; the environment's mirror_helper is the fallback."""
    env_helper = _mirror_script(d, "env-mirror.sh")
    _write_env(
        home,
        _env_with_vault(d, root=d).rstrip() + f"{NL}      mirror_helper: {env_helper}{NL}",
    )
    body = (
        _valid_project_body(d).replace(f"local_checkout: {d}", f"local_checkout: {d}{NL}      base_branch: develop")
        + f"{NL}    deprecated_mirror: legacy/mirror{NL}"
    )
    _write_project(home, "foo", body)
    block = config.render_resolved_block(load_environment(), load_project("foo"))
    assert "  base-branch = develop" in block
    assert "  deprecated_mirror = legacy/mirror" in block
    assert f"  vault.mirror_helper = {Path(env_helper)}" in block


def test_resolved_block_mirror_helper_prefers_the_project_level_value(home, d):
    env_helper = _mirror_script(d, "env-mirror.sh")
    proj_helper = _mirror_script(d, "proj-mirror.sh")
    _write_env(
        home,
        _env_with_vault(d, root=d).rstrip() + f"{NL}      mirror_helper: {env_helper}{NL}",
    )
    _write_project(
        home, "foo",
        _valid_project_body(d)
        + f"{NL}    vault:{NL}      root: {d}{NL}      mirror_helper: {proj_helper}{NL}",
    )
    block = config.render_resolved_block(load_environment(), load_project("foo"))
    assert f"  vault.mirror_helper = {Path(proj_helper)}" in block
    assert str(Path(env_helper)) not in block


def test_resolved_block_redacts_credentials_in_deprecated_mirror(home, d):
    """The field is unvalidated and the block lands in every transcript, so a pasted clone
    URL with a token must print with its userinfo redacted."""
    _write_env(home, _valid_env_body(d))
    _write_project(
        home, "foo",
        _valid_project_body(d) + f"{NL}    deprecated_mirror: https://someone:s3cretTOKEN@git.example.test/o/r.git{NL}",
    )
    block = config.render_resolved_block(load_environment(), load_project("foo"))
    assert "s3cretTOKEN" not in block and "someone" not in block
    assert "  deprecated_mirror = https://<redacted>@git.example.test/o/r.git" in block


def test_render_resolved_block_without_project(home, d, monkeypatch):
    _write_env(home, _valid_env_body(d))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    block = config.render_resolved_block(load_environment(), None)
    assert "[project] (no project)" in block
    assert "[verification] (no project)" in block


def test_show_cli_no_subcommand_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        config.main([])
    assert exc.value.code == 2
# --------------------------------------------------------------------------- #
# Unknown-key rejection (E-0200 scope item 1) + the verification.url cross-check
# --------------------------------------------------------------------------- #
UNKNOWN_TOP = """
    verifcation_typo: yes
"""
UNKNOWN_NESTED = """
      rolez: [a]
"""
ALIASES = """
    aliases: ["a", "b"]
"""
PINS = """
    communities:
      pins:
        "legacy/subsys": "Legacy"
"""
EMPTY_STAGING = """
    staging:
"""
URL_NO_WEB_JOURNEY = """
      url: https://nope.example
"""


def _web_journey_project(d: str, *, declared_url: str, staging_url: str) -> str:
    return f"""
    schema_version: 1
    name: foo
    canonical_repo:
      host_ref: owner/repo
      local_checkout: {d}
    verification:
      strategy: web-journey
      url: {declared_url}
    staging:
      url: {staging_url}
    """


def test_an_unknown_project_key_raises_naming_key_and_file(home, d):
    """The headline. A key nothing reads is a file that disagrees with the resolver."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d) + UNKNOWN_TOP)
    with pytest.raises(ConfigError) as e:
        load_project("foo")
    assert "verifcation_typo" in str(e.value)
    assert "foo.md" in str(e.value)


def test_an_unknown_NESTED_key_raises_with_its_dotted_path(home, d):
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d) + UNKNOWN_NESTED)
    with pytest.raises(ConfigError) as e:
        load_project("foo")
    assert "verification.rolez" in str(e.value)


def test_an_unknown_environment_key_raises(home, d):
    _write_env(home, _valid_env_body(d) + "    data_rooot: /tmp" + chr(10))
    with pytest.raises(ConfigError) as e:
        load_environment()
    assert "data_rooot" in str(e.value)


def test_every_key_the_resolver_reads_is_accepted(home, d):
    """The negative control. Without it, a rejection that fires on EVERYTHING
    passes the tests above and breaks every real config."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d))
    assert load_environment().data_root
    assert load_project("foo").name == "foo"


def test_a_key_read_inside_the_CONSTRUCTOR_call_is_not_rejected(home, d):
    """`aliases` is read in the `Project(...)` argument list, so a check placed
    before the return sees a half-filled consumed-set and rejects it. Measured on
    the live config, which carries three aliases."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d) + ALIASES)
    assert load_project("foo").aliases == ["a", "b"]


def test_community_PINS_are_data_and_are_never_rejected(home, d):
    """Pin keys are user-chosen path prefixes. Rejecting one for not being a
    schema key would be exactly backwards."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d) + PINS)
    assert load_project("foo").communities.pins == {"legacy/subsys": "Legacy"}


def test_an_empty_optional_block_is_not_an_unknown_key(home, d):
    """`staging:` with no body is a gate read, not an unread key."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d) + EMPTY_STAGING)
    assert load_project("foo").staging is None


def test_verification_url_matching_staging_url_is_accepted(home, d):
    """Today's live shape: a project file declares both, with the same value."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _web_journey_project(d, declared_url="https://s.example", staging_url="https://s.example"))
    assert load_project("foo").verification.url == "https://s.example"


def test_a_DIVERGENT_verification_url_raises(home, d):
    """THE ASSERTION THAT PROVES THE CROSS-CHECK RUNS BEFORE THE BINDING.

    The resolver assigns `verification.url = staging.url`. A check placed after
    that assignment compares st_url to itself, can never fire, and is green on
    every run - indistinguishable from a working guard. Setting the two keys to
    DIFFERENT values is the only thing that tells them apart.
    """
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _web_journey_project(d, declared_url="https://declared.example", staging_url="https://actual.example"))
    with pytest.raises(ConfigError) as e:
        load_project("foo")
    msg = str(e.value)
    assert "verification.url" in msg
    assert "declared.example" in msg and "actual.example" in msg


def test_verification_url_without_web_journey_raises(home, d):
    """Nothing binds it, so it would be dead the moment it was written."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d) + URL_NO_WEB_JOURNEY)
    with pytest.raises(ConfigError) as e:
        load_project("foo")
    assert "verification.url" in str(e.value)
    assert "web-journey" in str(e.value)
# --------------------------------------------------------------------------- #
# resolve_autodoc_root (E-0200 items 2+3) - precedence, and the LOUD third state
# --------------------------------------------------------------------------- #
from agentflow.config import AutodocRootError, resolve_autodoc_root  # noqa: E402
from agentflow.tracked_paths import tracked_files, tracked_paths  # noqa: E402

NL = chr(10)
Q = chr(34)
BS = chr(92)


def _corpus(root: Path, *, manifest: bool) -> Path:
    """A directory that looks like a real code-wiki to the resolver."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "flows").mkdir(exist_ok=True)
    (root / "flows" / "a-flow.md").write_text("# flow", encoding="utf-8")
    if manifest:
        (root / ".manifest.json").write_text("{}", encoding="utf-8")
    return root


class _Env:
    def __init__(self, autodoc_root=None, data_root=None):
        self.autodoc_root = autodoc_root
        self.data_root = data_root


class _Proj:
    def __init__(self, name="foo", autodoc_root=None):
        self.name = name
        self.autodoc_root = autodoc_root


def test_the_project_override_beats_the_environment_default(tmp_path):
    """The whole point: one machine-level root shared by two projects means one
    project's autodoc pass writes into the other's tree."""
    mine = _corpus(tmp_path / "mine", manifest=True)
    theirs = _corpus(tmp_path / "theirs", manifest=True)
    got = resolve_autodoc_root(_Env(autodoc_root=str(theirs)), _Proj(autodoc_root=str(mine)))
    assert got == str(mine)


def test_the_environment_default_is_used_when_the_project_has_no_override(tmp_path):
    theirs = _corpus(tmp_path / "theirs", manifest=True)
    assert resolve_autodoc_root(_Env(autodoc_root=str(theirs)), _Proj()) == str(theirs)


def test_a_project_NAME_is_refused_rather_than_silently_resolving_to_another_corpus(tmp_path):
    """gh#397. `getattr("foo", "autodoc_root", None)` is None, so a string skipped the override
    and fell through to the environment root -- another project's real, populated corpus, which
    passes every existence check downstream and never trips AutodocRootError.

    The fixture gives the project an override that DIFFERS from the env root deliberately: with
    both pointing at one path the two call forms agree and the test could not fail.
    """
    mine = _corpus(tmp_path / "mine", manifest=True)
    theirs = _corpus(tmp_path / "theirs", manifest=True)
    env = _Env(autodoc_root=str(theirs))

    assert resolve_autodoc_root(env, _Proj(autodoc_root=str(mine))) == str(mine)

    with pytest.raises(TypeError) as exc:
        resolve_autodoc_root(env, "foo")
    msg = str(exc.value)
    assert "load_project" in msg, "the refusal does not say what to pass instead: " + msg
    assert "another project" in msg, "the refusal does not say what goes wrong: " + msg


def test_the_refusal_names_the_type_it_got_so_the_call_site_is_findable(tmp_path):
    with pytest.raises(TypeError) as exc:
        resolve_autodoc_root(_Env(autodoc_root=str(_corpus(tmp_path / "t", manifest=True))), 42)
    assert "int" in str(exc.value)


def test_None_is_still_accepted_because_it_means_no_project_scope(tmp_path):
    """The guard must not catch the legitimate unscoped call -- provision.py makes it."""
    theirs = _corpus(tmp_path / "theirs", manifest=True)
    assert resolve_autodoc_root(_Env(autodoc_root=str(theirs)), None) == str(theirs)


def test_the_derived_per_project_root_is_used_when_neither_is_set(tmp_path):
    derived = _corpus(tmp_path / "projects" / "foo" / "autodoc", manifest=True)
    got = resolve_autodoc_root(_Env(data_root=str(tmp_path)), _Proj())
    assert Path(got) == derived


def test_unconfigured_returns_None_and_callers_degrade(tmp_path):
    """State 1. Not every install has a code-wiki, and that is not an error."""
    assert resolve_autodoc_root(_Env(data_root=str(tmp_path)), _Proj()) is None


def test_a_manifestless_root_RAISES_while_the_legacy_root_holds_the_corpus(tmp_path):
    """STATE 3, AND THE REASON THIS FUNCTION RAISES AT ALL.

    The migration hazard: an empty scaffolded root passes every exists/is_dir gate
    while the real corpus sits orphaned where config used to point. Returning None
    would be worse than useless - `find_touchpoints` never raises, so the tool would
    print a confident `## flow (0)`, and a zero is read downstream as evidence that
    the code is missing, manufacturing false gap epics.
    """
    legacy = _corpus(tmp_path / "legacy", manifest=True)
    scaffolded = tmp_path / "new"
    scaffolded.mkdir()
    with pytest.raises(AutodocRootError) as e:
        resolve_autodoc_root(_Env(autodoc_root=str(legacy)), _Proj(autodoc_root=str(scaffolded)))
    # Assert the STRUCTURED attributes, not the formatted message: `{path!r}` on
    # Windows escapes every separator, so a substring check on the text is a test
    # of repr() rather than of the guard.
    assert Path(e.value.legacy) == legacy
    assert Path(e.value.resolved) == scaffolded
    assert "remediation" in str(e.value)


def test_a_manifestless_root_is_FINE_when_the_legacy_root_is_empty(tmp_path):
    """The over-fire control. A guard that raises whenever a manifest is absent
    would break every install that simply has not run autodoc-init yet."""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    scaffolded = tmp_path / "new"
    scaffolded.mkdir()
    got = resolve_autodoc_root(_Env(autodoc_root=str(legacy)), _Proj(autodoc_root=str(scaffolded)))
    assert got == str(scaffolded)


def test_a_root_WITH_a_manifest_never_raises_however_full_the_legacy_root_is(tmp_path):
    """State 2. The manifest is what says 'this root is the real one'."""
    legacy = _corpus(tmp_path / "legacy", manifest=True)
    live = _corpus(tmp_path / "new", manifest=True)
    got = resolve_autodoc_root(_Env(autodoc_root=str(legacy)), _Proj(autodoc_root=str(live)))
    assert got == str(live)


# gh#759 item 1: with no project override, an environment root whose manifest names ANOTHER
# project is that project's corpus, not this one's. It used to win for every project on the box.
def _named(root: Path, project: str) -> Path:
    _corpus(root, manifest=False)
    (root / ".manifest.json").write_text(json.dumps({"project": project}), encoding="utf-8")
    return root


def test_a_FOREIGN_env_root_is_skipped_for_the_derived_per_project_root(tmp_path):
    theirs = _named(tmp_path / "theirs", "bar")
    derived = _corpus(tmp_path / "projects" / "foo" / "autodoc", manifest=True)
    got = resolve_autodoc_root(_Env(autodoc_root=str(theirs), data_root=str(tmp_path)), _Proj())
    assert got is not None and Path(got) == derived


def test_a_FOREIGN_env_root_with_no_derived_root_resolves_to_None_and_never_raises(tmp_path):
    """The skipped corpus is full, and nothing resolves in its place: that is unconfigured,
    not the migration hazard, so AutodocRootError must not fire against it."""
    theirs = _named(tmp_path / "theirs", "bar")
    assert resolve_autodoc_root(_Env(autodoc_root=str(theirs), data_root=str(tmp_path)), _Proj()) is None


def test_a_FOREIGN_env_root_skipped_onto_a_manifestless_derived_root_never_raises(tmp_path):
    theirs = _named(tmp_path / "theirs", "bar")
    derived = tmp_path / "projects" / "foo" / "autodoc"
    derived.mkdir(parents=True)
    got = resolve_autodoc_root(_Env(autodoc_root=str(theirs), data_root=str(tmp_path)), _Proj())
    assert got is not None and Path(got) == derived


def test_an_env_root_naming_THIS_project_still_wins_over_the_derived_root(tmp_path):
    mine = _named(tmp_path / "mine", "foo")
    _corpus(tmp_path / "projects" / "foo" / "autodoc", manifest=True)
    got = resolve_autodoc_root(_Env(autodoc_root=str(mine), data_root=str(tmp_path)), _Proj())
    assert got == str(mine)


def test_an_env_root_whose_manifest_has_NO_project_field_resolves_as_before(tmp_path):
    anon = _corpus(tmp_path / "anon", manifest=True)
    _corpus(tmp_path / "projects" / "foo" / "autodoc", manifest=True)
    got = resolve_autodoc_root(_Env(autodoc_root=str(anon), data_root=str(tmp_path)), _Proj())
    assert got == str(anon)


def test_an_explicit_override_with_a_foreign_manifest_is_never_checked(tmp_path):
    theirs = _named(tmp_path / "theirs", "bar")
    _corpus(tmp_path / "projects" / "foo" / "autodoc", manifest=True)
    got = resolve_autodoc_root(_Env(data_root=str(tmp_path)), _Proj(autodoc_root=str(theirs)))
    assert got == str(theirs)


def test_an_unscoped_call_still_returns_a_foreign_env_root(tmp_path):
    """No project means no name to compare against: provision.py's unscoped call is unchanged."""
    theirs = _named(tmp_path / "theirs", "bar")
    assert resolve_autodoc_root(_Env(autodoc_root=str(theirs)), None) == str(theirs)


def _yaml_path(p) -> str:
    """A Windows path is not YAML-safe unquoted: the drive colon starts a mapping."""
    return Q + str(p).replace(BS, "/") + Q


def test_the_resolved_block_prints_the_PROJECTS_root_not_the_machine_default(home, d, tmp_path):
    """Every loop binds {autodoc_root} out of this block, so printing the machine
    default here is exactly how a two-project fleet crosses its wires."""
    mine = _corpus(tmp_path / "mine", manifest=True)
    theirs = _corpus(tmp_path / "theirs", manifest=True)
    _write_env(home, _valid_env_body(d).rstrip() + NL + "    autodoc_root: " + _yaml_path(theirs) + NL)
    _write_project(home, "foo", _valid_project_body(d).rstrip() + NL + "    autodoc_root: " + _yaml_path(mine) + NL)
    block = config.render_resolved_block(load_environment(), load_project("foo"))
    line = [x for x in block.splitlines() if "autodoc_root" in x][0]
    assert str(mine) in line
    assert str(theirs) not in line


def test_the_resolved_block_REPORTS_an_unusable_root_instead_of_exploding(home, d, tmp_path):
    """A session-open banner that raises takes every loop down with it. It says
    UNUSABLE and keeps rendering; the tools that would answer WRONG are the ones
    that raise."""
    legacy = _corpus(tmp_path / "legacy", manifest=True)
    scaffolded = tmp_path / "new"
    scaffolded.mkdir()
    _write_env(home, _valid_env_body(d).rstrip() + NL + "    autodoc_root: " + _yaml_path(legacy) + NL)
    _write_project(home, "foo", _valid_project_body(d).rstrip() + NL + "    autodoc_root: " + _yaml_path(scaffolded) + NL)
    block = config.render_resolved_block(load_environment(), load_project("foo"))
    assert "UNUSABLE" in block


def test_a_profile_autodoc_root_SURVIVES_THE_PARSE_and_beats_the_env_default(home, d, tmp_path):
    """The same precedence as the fake-fed test above, asserted through the REAL
    profile parse -- because that is where it was once broken.

    The live agentflow override (gh#18, E-0735) is a profile key, and the sibling
    spec still says on disk that such a key is "silently swallowed". That was true
    once and is not now, and nothing pinned the difference: a `_Proj` fake carries
    the attribute whether or not `_build_project` would have kept it, so a parse-side
    regression leaves the precedence test green and repoints a project's whole corpus
    at another project's tree.

    The removal control is the assertion, not a comment: with the key gone from the
    same fixture the resolver must fall back to the environment default. Without it
    this test passes just as well against a resolver that ignores the environment.
    """
    mine = _corpus(tmp_path / "mine", manifest=True)
    theirs = _corpus(tmp_path / "theirs", manifest=True)
    _write_env(home, _valid_env_body(d).rstrip() + NL + "    autodoc_root: " + _yaml_path(theirs) + NL)

    _write_project(home, "foo", _valid_project_body(d).rstrip() + NL + "    autodoc_root: " + _yaml_path(mine) + NL)
    assert load_project("foo").autodoc_root is not None, "the profile key did not survive the parse"
    assert Path(resolve_autodoc_root(load_environment(), load_project("foo"))) == mine

    _write_project(home, "foo", _valid_project_body(d))
    assert Path(resolve_autodoc_root(load_environment(), load_project("foo"))) == theirs


def test_a_profile_autodoc_root_naming_a_MISSING_directory_is_refused_at_load(home, d, tmp_path):
    """Why the relocation writes the override LAST, and why that order is not a taste.

    `_check_path_if_set(..., must_be_dir=True)` means the moment this key names a
    path that is not there, `load_project` raises -- and every loop calls it at wake
    before it can read the registry or write a heartbeat, so the whole fleet goes
    down at once and reads as a broken machine rather than a bad config value.

    Both halves are pinned because they fail differently and the message is what a
    reader recovers from: absent path, and a path that exists but is a file.
    """
    absent = tmp_path / "not-moved-yet"
    _write_project(home, "foo", _valid_project_body(d).rstrip() + NL + "    autodoc_root: " + _yaml_path(absent) + NL)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "autodoc_root"
    assert "does not exist" in str(ei.value)

    a_file = tmp_path / "corpus-as-a-file"
    a_file.write_text("not a directory", encoding="utf-8")
    _write_project(home, "foo", _valid_project_body(d).rstrip() + NL + "    autodoc_root: " + _yaml_path(a_file) + NL)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "autodoc_root", (
        "the reason string is field-independent: _check_path_if_set and _require_dir both emit "
        "it verbatim for local_checkout, deprecated_mirror_checkout and vault.root, three of "
        "which are validated BEFORE this one and would satisfy a wording-only assertion"
    )
    assert "not a directory" in str(ei.value)


def test_no_module_reads_env_autodoc_root_bare(tmp_path):
    """`delete every bare env.autodoc_root read` is a scope item, so pin it: a new
    bare read is how the resolver gets bypassed six months from now."""
    pkg = Path(config.__file__).parent
    # The package that was IMPORTED, which under an editable install is not necessarily inside
    # this checkout - so the index is read at `pkg` rather than at `_REPO_ROOT`, and `ls-files`
    # answers relative to it (`config.py`, not `skills/_shared/agentflow/config.py`).
    modules = tracked_files(pkg, lambda rel: "/" not in rel and rel.endswith(".py"))
    assert len(modules) > 20 and any(m.name == "config.py" for m in modules), (
        f"the module corpus collapsed to {len(modules)} files - this gate had no floor guard at "
        "all before gh#175, so an empty corpus passed it by scanning nothing"
    )
    offenders = []
    for f in modules:
        assert f.is_file(), f"tracked but missing from the working tree: {f}"
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "env.autodoc_root" in line and "resolve_autodoc_root" not in line:
                offenders.append(f"{f.name}:{n}")
    assert not offenders, f"bare env.autodoc_root reads: {offenders}"


# The skills are prose programs, and the readers of `autodoc_root` that matter most are prose:
# the fenced blocks a skill runs, and the sentences that tell an agent what to assign.
_SKILLS_ROOT = Path(__file__).resolve().parents[2]
# The names the fences give the environment object. `_bare_reads` adds every other name a file binds
# to `load_environment()`. The per-project override (`proj.autodoc_root`) is not this class; the
# resolver reads that one.
_ENV_NAMES = frozenset({"e", "env", "environment"})
_ENV_BIND = re.compile(r"\b([A-Za-z_]\w*)\s*(?::\s*[\w.\[\]]+\s*)?=\s*load_environment\(\)")
# Read off the call itself, with no name in between: never legitimate, so matched string or not.
_CALL_ATTR = re.compile(r"load_environment\(\)\s*\.\s*autodoc_root\b")
# A prose instruction that binds $AUTODOC to the machine-level field, e.g. "`AUTODOC` =
# `environment.autodoc_root`". Stated in prose it is a read an agent performs. The `=` stands
# alone: the lookarounds keep `!=`, `==`, `<=`, `>=` and `=~` from reading as an assignment.
_PROSE_ASSIGN = re.compile(
    r"\bAUTODOC\b\W{0,4}(?<![!=<>~])=(?![=~])\W{0,4}(?:e|env|environment)\.autodoc_root\b"
)
# A binding stated inside a prohibition ("never set AUTODOC = ...") is the rule, not a read.
_NEGATION = re.compile(r"\b(?:never|not|don't|cannot|nor)\b", re.I)
_SENTENCE_END = re.compile(r"[.!?:]\s|\n\s*\n")
_SHELLS = frozenset({"bash", "sh", "shell", "zsh", "console"})
# `<<'PY'`, `<<-EOF`, `<< "X"`, but not a `<<<` here-string.
_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")


def _fences(text: str) -> list[tuple[int, str, str]]:
    """(line number, line, language) for every line inside a fenced block, of any language. A
    shell fence's heredoc body is not shell: it gets the language `heredoc`."""
    out, lang, tag = [], None, None
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if tag is None and s.startswith("```"):
            lang = None if lang is not None else (s[3:].strip().lower() or "text")
            continue
        if lang is None:
            continue
        if tag is not None:
            if s == tag:
                tag = None
                out.append((n, line, lang))
            else:
                out.append((n, line, "heredoc"))
            continue
        out.append((n, line, lang))
        if lang in _SHELLS:
            m = _HEREDOC.search(_strip_comment(line))
            if m:
                tag = m.group(2)
    return out


def _scan(line: str, *, keep_strings: bool) -> str:
    """The line up to a trailing `#` or `//` comment, with string contents kept or emptied."""
    out, q = [], None
    for i, ch in enumerate(line):
        if q:
            if ch == q:
                q = None
                out.append(ch)
            elif keep_strings:
                out.append(ch)
        elif ch in "'\"":
            q = ch
            out.append(ch)
        elif (ch == "#" or line.startswith("//", i)) and (i == 0 or line[i - 1].isspace()):
            break
        else:
            out.append(ch)
    return "".join(out)


def _strip_comment(line: str) -> str:
    """The line as written, minus a trailing comment. In a shell line a quoted string is code:
    `python -c '...'` carries a whole program in one."""
    return _scan(line, keep_strings=True)


def _code_only(line: str) -> str:
    """The line as code: a trailing comment dropped and string literals emptied, so a sentence
    ABOUT a read is not a read. The fences explain this very defect in comments, and a refusal
    message may name the field it refuses to trust."""
    return _scan(line, keep_strings=False)


def test_no_skill_prose_reads_env_autodoc_root_bare():
    """The same class as the module gate above, over the `.md` under skills/: every fenced block
    (what an agent runs) and every prose line (what an agent is told to assign). No allow-list:
    a rule that names the bare field in order to forbid it sits in prose, not in a fence, and does
    not bind AUTODOC to it."""
    docs = tracked_files(
        _SKILLS_ROOT, lambda rel: rel.endswith(".md") and not rel.startswith("_vendored/")
    )
    assert len(docs) > 40 and any(d.as_posix().endswith("autodoc-init/SKILL.md") for d in docs), (
        f"the skills prose corpus collapsed to {len(docs)} files - a gate that scans nothing passes"
    )
    offenders = []
    for f in docs:
        assert f.is_file(), f"tracked but missing from the working tree: {f}"
        text = f.read_text(encoding="utf-8")
        rel = f.relative_to(_SKILLS_ROOT).as_posix()
        for n, line in _bare_reads(text):
            offenders.append(f"{rel}:{n} (fenced) {line.strip()}")
        for n, span in _prose_binds(text):
            offenders.append(f"{rel}:{n} (prose) {span}")
    assert not offenders, "bare autodoc_root readers in skill prose:\n" + "\n".join(offenders)


def _bare_reads(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every fenced line that reads the machine-level field bare: an
    attribute read off any name the file binds to `load_environment()`, a read off the call
    itself, or a `getattr` of either. Shell lines are matched with their strings kept, since a
    `python -c` body is code; every other line with its strings emptied."""
    lines = _fences(text)
    names = set(_ENV_NAMES)
    for _, line, _ in lines:
        names.update(m.group(1) for m in _ENV_BIND.finditer(_strip_comment(line)))
    alt = "|".join(re.escape(x) for x in sorted(names))
    attr = re.compile(r"\b(?:" + alt + r")\s*\.\s*autodoc_root\b")
    via_getattr = re.compile(
        r"\bgetattr\(\s*(?:load_environment\(\)|(?:" + alt + r")\b)\s*,\s*['\"]autodoc_root['\"]"
    )
    hits = []
    for n, line, lang in lines:
        raw = _strip_comment(line)
        if "resolve_autodoc_root" in raw:
            continue
        code = raw if lang in _SHELLS else _code_only(line)
        if attr.search(code) or _CALL_ATTR.search(raw) or via_getattr.search(raw):
            hits.append((n, line))
    return hits


def _prose_binds(text: str) -> list[tuple[int, str]]:
    """(line number, span) for every prose binding of $AUTODOC to the machine-level field. Over
    the whole text, not per line: prose wraps, and the binding has been written with its `=` at
    the end of one line and the field at the start of the next. A binding inside a prohibition
    ("never set ...") is skipped: the clause it sits in says not to do it."""
    hits = []
    for m in _PROSE_ASSIGN.finditer(text):
        ends = [e.end() for e in _SENTENCE_END.finditer(text, 0, m.start())]
        clause = text[ends[-1] if ends else 0:m.start()]
        if _NEGATION.search(clause):
            continue
        hits.append((text.count("\n", 0, m.start()) + 1, " ".join(m.group(0).split())))
    return hits


def _fence(lang: str, *lines: str) -> str:
    return "```" + lang + "\n" + "\n".join(lines) + "\n```\n"


# Each shape is a way to hand an agent the machine-level value while bypassing the resolver.
_MUST_CATCH_READS = {
    "python attr": _fence("python", 'emit("AUTODOC", e.autodoc_root)'),
    "python -c one-liner": _fence(
        "bash", "AUTODOC=$(python -c 'from agentflow.config import load_environment; "
        "print(load_environment().autodoc_root)')"),
    "python -c with a bound name": _fence(
        "bash", "AUTODOC=$(python -c 'from agentflow.config import load_environment as L; "
        "e = L(); print(e.autodoc_root)')"),
    "any name bound to load_environment()": _fence(
        "python", "cfg = load_environment()", "root = cfg.autodoc_root"),
    "getattr": _fence("python", 'root = getattr(load_environment(), "autodoc_root")'),
    "getattr on a bound name": _fence(
        "python", "cfg = load_environment()", "root = getattr(cfg, 'autodoc_root')"),
}
# A sentence ABOUT the field is not a read of it: the fences explain this defect in comments, and
# a refusal message may name the field it refuses to trust.
_MUST_NOT_CATCH_READS = {
    "bash comment": _fence("bash", "# never e.autodoc_root here"),
    "python string": _fence("python", 'raise SystemExit("environment.autodoc_root is null")'),
    "python string in a bash heredoc": _fence(
        "bash", "CFG=\"$(python - <<'PY'", 'raise SystemExit("set environment.autodoc_root: null")',
        "PY", ')" || exit 1'),
    "comment naming the field after a call": _fence(
        "python", "env = load_environment()  # data_root, autodoc_root, suite_root"),
    "the per-project override": _fence("python", "proj = load_project(name)", "x = proj.autodoc_root"),
    "the resolver": _fence("python", "root = resolve_autodoc_root(e, proj)  # not e.autodoc_root"),
}
_MUST_CATCH_PROSE = {
    "inline": "`AUTODOC` = `environment.autodoc_root`; `SUITE_ROOT` = ...",
    "wrapped": "config first. `AUTODOC` =\n`environment.autodoc_root`; and",
}
_MUST_NOT_CATCH_PROSE = {
    "a rule forbidding it": "**Never the bare `env.autodoc_root`**",
    "inequality": "check that `AUTODOC` != `env.autodoc_root` before writing",
    "regex match": 'when `AUTODOC` =~ `env.autodoc_root`, stop',
    "a prohibition": "never set AUTODOC = environment.autodoc_root on a shared box",
    "a wrapped prohibition": "Do not\nset `AUTODOC` = `environment.autodoc_root`.",
}


@pytest.mark.parametrize("shape", sorted(_MUST_CATCH_READS))
def test_the_skill_prose_gate_catches_every_bare_read_shape(shape):
    assert _bare_reads(_MUST_CATCH_READS[shape]), f"missed a bare read: {shape}"


@pytest.mark.parametrize("shape", sorted(_MUST_NOT_CATCH_READS))
def test_the_skill_prose_gate_ignores_what_is_not_a_read(shape):
    assert not _bare_reads(_MUST_NOT_CATCH_READS[shape]), f"flagged a non-read: {shape}"


@pytest.mark.parametrize("shape", sorted(_MUST_CATCH_PROSE))
def test_the_skill_prose_gate_catches_a_prose_binding(shape):
    assert _prose_binds(_MUST_CATCH_PROSE[shape]), f"missed a prose binding: {shape}"


@pytest.mark.parametrize("shape", sorted(_MUST_NOT_CATCH_PROSE))
def test_the_skill_prose_gate_ignores_prose_that_does_not_bind(shape):
    assert not _prose_binds(_MUST_NOT_CATCH_PROSE[shape]), f"flagged a non-binding: {shape}"


# --------------------------------------------------------------------------- #
# idle_wake_minutes vs the ScheduleWakeup clamp (gh#82)
# --------------------------------------------------------------------------- #
def test_an_idle_wake_above_the_ceiling_resolves_to_what_the_harness_will_grant(home, d, caplog):
    """THE LIVE CASE, 2026-09-01: the auditor asked for 120 minutes, `ScheduleWakeup`
    granted 60, and every surface still said 120. Nothing in the runtime reports a clamp,
    so the resolved config has to carry the number that will really happen or the whole
    fleet reasons about an interval that never occurs.
    """
    import logging

    from agentflow.config import WAKE_DELAY_MAX_S

    body = _valid_project_body(d) + "\n    audit:\n      idle_wake_minutes: 120"
    _write_project(home, "foo", body)
    with caplog.at_level(logging.WARNING):
        proj = load_project("foo")
    assert proj.audit.idle_wake_minutes == WAKE_DELAY_MAX_S // 60 == 60
    assert "120" in caplog.text and "60" in caplog.text, (
        "the warning must name BOTH numbers - a clamp that says only the new value "
        "reads as a setting nobody chose"
    )


def test_it_warns_rather_than_hard_failing_the_whole_project(home, d):
    """Deliberate, and the asymmetry is the point. The value is a user dial with a
    harmless intent, and `load_environment`/`load_project` run at step 0 of every loop -
    a ConfigError here takes the entire fleet down over a preference. Compare
    `verification.url`, where the same reasoning produced a cross-check rather than a
    rejection."""
    body = _valid_project_body(d) + "\n    audit:\n      idle_wake_minutes: 999"
    _write_project(home, "foo", body)
    proj = load_project("foo")  # must not raise
    assert proj.audit.idle_wake_minutes == 60


def test_a_below_floor_idle_wake_is_raised_to_the_floor(home, d):
    """The clamp runs both ways. `delaySeconds` below 60 is raised, not honoured, so a
    project asking for a 0-minute backoff would otherwise record a busy-loop it never ran."""
    body = _valid_project_body(d) + "\n    audit:\n      idle_wake_minutes: 0"
    _write_project(home, "foo", body)
    assert load_project("foo").audit.idle_wake_minutes == 1


def test_a_value_INSIDE_the_range_is_left_exactly_alone(home, d):
    """The narrowness control. A clamp that also rewrote legal values would silently
    homogenise every project's cadence to 60 and look identical in every test above."""
    body = _valid_project_body(d) + "\n    audit:\n      idle_wake_minutes: 45"
    _write_project(home, "foo", body)
    assert load_project("foo").audit.idle_wake_minutes == 45


def test_the_default_is_inside_the_range_it_documents(home, d):
    """A default outside its own clamp would be the bug shipped as a shipped default."""
    from agentflow.config import WAKE_DELAY_MAX_S, WAKE_DELAY_MIN_S

    _write_project(home, "foo", _valid_project_body(d))
    seconds = load_project("foo").audit.idle_wake_minutes * 60
    assert WAKE_DELAY_MIN_S <= seconds <= WAKE_DELAY_MAX_S


# --------------------------------------------------------------------------- #
# vault.root stops being decorative (E-0200 items 3 + 6)
# --------------------------------------------------------------------------- #
def _env_with_vault(d, root=None, subdir=None) -> str:
    body = _valid_env_body(d)
    if root is None:
        return body
    extra = f"    vault:{NL}      root: {root}{NL}"
    if subdir:
        extra += f"      subdir: {subdir}{NL}"
    return body.rstrip() + NL + extra


def test_the_project_VAULT_override_beats_the_environment_default(home, d):
    """Same precedence as autodoc_root, deliberately: one shape for both per-project paths
    means a reader who has learned one has learned the other."""
    from agentflow.config import load_environment, resolve_vault_root

    (Path(d) / "vault-a").mkdir(exist_ok=True)
    (Path(d) / "vault-b").mkdir(exist_ok=True)
    _write_env(home, _env_with_vault(d, root=f"{d}/vault-a"))
    body = _valid_project_body(d) + f"{NL}    vault:{NL}      root: {d}/vault-b"
    _write_project(home, "foo", body)
    env, proj = load_environment(), load_project("foo")
    assert resolve_vault_root(env, proj).endswith("vault-b")
    assert resolve_vault_root(env, None).endswith("vault-a")


def test_no_vault_anywhere_resolves_to_None_rather_than_a_guess(home, d):
    """Absent means the feature is OFF and every mirror site no-ops. Returning a default
    path here is exactly the bug being fixed - one level up instead of in the shell."""
    from agentflow.config import load_environment, resolve_vault_root

    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d))
    assert resolve_vault_root(load_environment(), load_project("foo")) is None


def test_resolve_mirror_helper_project_then_environment_then_None(home, d):
    from agentflow.config import load_environment, resolve_mirror_helper

    env_helper = _mirror_script(d, "env-mirror.sh")
    proj_helper = _mirror_script(d, "proj-mirror.sh")
    _write_env(home, _env_with_vault(d, root=d).rstrip() + f"{NL}      mirror_helper: {env_helper}{NL}")
    _write_project(home, "foo", _valid_project_body(d) + f"{NL}    vault:{NL}      root: {d}{NL}      mirror_helper: {proj_helper}{NL}")
    env = load_environment()
    assert Path(resolve_mirror_helper(env, load_project("foo"))) == Path(proj_helper)
    assert Path(resolve_mirror_helper(env, None)) == Path(env_helper)
    _write_project(home, "foo", _valid_project_body(d))
    assert Path(resolve_mirror_helper(env, load_project("foo"))) == Path(env_helper)
    _write_env(home, _valid_env_body(d))
    assert resolve_mirror_helper(load_environment(), load_project("foo")) is None


def test_subdir_defaults_to_what_the_helper_already_hardcodes(home, d):
    """An existing install that never sets it must keep writing exactly where it writes now.
    A new default here would silently relocate every future mirror."""
    from agentflow.config import load_environment, resolve_vault_subdir

    _write_env(home, _env_with_vault(d, root=d))
    _write_project(home, "foo", _valid_project_body(d))
    assert resolve_vault_subdir(load_environment(), load_project("foo")) == "wiki/epics"


def test_subdir_is_overridable(home, d):
    from agentflow.config import load_environment, resolve_vault_subdir

    _write_env(home, _env_with_vault(d, root=d, subdir='"docs/epics"'))
    _write_project(home, "foo", _valid_project_body(d))
    assert resolve_vault_subdir(load_environment(), load_project("foo")) == "docs/epics"


def test_the_resolved_block_PRINTS_the_vault_root(home, d):
    """THE POINT OF THE WHOLE CHANGE. The helper takes its root from WIKI_VAULT_PATH and
    falls back to a hardcoded path, so a loop that cannot SEE the resolved root cannot pass
    it - and the fallback quietly decides where documents land."""
    from agentflow.config import load_environment, render_resolved_block

    _write_env(home, _env_with_vault(d, root=d))
    _write_project(home, "foo", _valid_project_body(d))
    block = render_resolved_block(load_environment(), load_project("foo"))
    # Separators are normalised before comparing: the fixture builds posix-style paths and
    # the block prints whatever the platform resolved to, so a raw substring test fails on
    # Windows for a reason that has nothing to do with the feature.
    assert "vault_root" in block
    assert str(d).replace(chr(92), "/") in block.replace(chr(92), "/")
    assert "vault_subdir" in block


def test_the_block_says_NO_VAULT_rather_than_printing_nothing(home, d):
    """A missing line reads as a rendering bug; "no vault" reads as a decision. "No" is a
    first-class answer here, not a degraded mode."""
    from agentflow.config import load_environment, render_resolved_block

    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d))
    block = render_resolved_block(load_environment(), load_project("foo"))
    assert "vault_root" in block and "no vault" in block
    assert "vault_subdir" not in block, "a subdir without a root is noise"


# --------------------------------------------------------------------------- #
# The project list the mirror routes by (E-0200 item 8)
# --------------------------------------------------------------------------- #
def test_configured_project_names_reads_the_config_dir(home, d):
    """A name with a config file IS a project - that is what `load_project` keys on, so the
    mirror's allowlist should key on the same thing rather than a literal in a bash file."""
    from agentflow.config import configured_project_names

    _write_env(home, _valid_env_body(d))
    _write_project(home, "beta", _valid_project_body(d))
    _write_project(home, "alpha", _valid_project_body(d))
    assert configured_project_names(home) == ["alpha", "beta"]


def test_a_missing_config_home_is_an_empty_list_not_a_raise(tmp_path):
    """A caller building an env var out of this must never be the thing that fails an
    install."""
    from agentflow.config import configured_project_names

    assert configured_project_names(tmp_path / "nope") == []


def test_an_UNREADABLE_config_home_is_also_an_empty_list(tmp_path, monkeypatch):
    """The case the OSError guard actually exists for, and the first version of this test
    missed it: `Path.glob` on a MISSING directory yields nothing rather than raising, so the
    missing-home test above passes with the guard deleted. Only an unreadable one reaches it.
    """
    from pathlib import Path

    from agentflow.config import configured_project_names

    (tmp_path / "projects").mkdir()

    def boom(self, pattern):
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "glob", boom)
    assert configured_project_names(tmp_path) == []


def test_the_resolved_block_carries_the_project_list(home, d):
    """THE POINT: the mirror routes by project NAME against an allowlist, and a loop that
    cannot see the list cannot pass it - at which point the script's own literal decides
    which projects exist, and the next one a user configures routes to _unsorted/ silently."""
    from agentflow.config import load_environment, render_resolved_block

    (Path(d) / "v").mkdir(exist_ok=True)
    _write_env(home, _env_with_vault(d, root=f"{d}/v"))
    _write_project(home, "foo", _valid_project_body(d))
    block = render_resolved_block(load_environment(), load_project("foo"))
    assert "vault_projects" in block and "foo" in block


def test_the_project_list_is_omitted_when_there_is_no_vault(home, d):
    """It exists to be handed to the mirror. With no vault there is no mirror, and a line
    nobody can use is a line that trains the reader to skim."""
    from agentflow.config import load_environment, render_resolved_block

    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d))
    assert "vault_projects" not in render_resolved_block(load_environment(), load_project("foo"))


# --------------------------------------------------------------------------- #
# The third answer: asked, and the answer was no (E-0200 item 7 correction)
# --------------------------------------------------------------------------- #
def test_an_EXPLICIT_null_vault_is_recorded_as_a_DECISION(home, d):
    """The install interview's third outcome. It resolves to no vault exactly like an
    absent key - what it adds is that a reader can tell a decision from an unfinished
    interview, which is the whole reason the convention was documented."""
    from agentflow.config import load_environment, resolve_vault_root, vault_declined

    _write_env(home, _valid_env_body(d) + f"{NL}    vault: null{NL}")
    env = load_environment()
    assert env.vault is None and resolve_vault_root(env) is None
    assert vault_declined(env) is True


def test_an_ABSENT_vault_key_is_NOT_a_decision(home, d):
    """Nobody has been asked yet. Reading this as a decline would silence the one prompt
    that finishes the install."""
    from agentflow.config import load_environment, vault_declined

    _write_env(home, _valid_env_body(d))
    assert vault_declined(load_environment()) is False


def test_a_vault_key_with_an_EMPTY_BODY_is_a_decision_too(home, d):
    """`vault:` and `vault: null` are the same YAML value, and a user who wrote the shorter
    one meant the same thing. Keying on the VALUE rather than the spelling is what makes
    that true without a second rule."""
    from agentflow.config import load_environment, vault_declined

    _write_env(home, _valid_env_body(d) + f"{NL}    vault:{NL}")
    assert vault_declined(load_environment()) is True


def test_a_CONFIGURED_vault_is_not_a_decline(home, d):
    """Sanity: the flag must not read "the key is present" - a configured vault is the
    opposite answer."""
    from agentflow.config import load_environment, vault_declined

    (Path(d) / "v").mkdir(exist_ok=True)
    _write_env(home, _env_with_vault(d, root=f"{d}/v"))
    assert vault_declined(load_environment()) is False


def test_a_PROJECT_can_record_the_decline_as_well(home, d):
    """Both files carry the same key, so both can carry the same answer."""
    from agentflow.config import load_environment, load_project, vault_declined

    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d) + f"{NL}    vault: null{NL}")
    assert vault_declined(load_environment(), load_project("foo")) is True


def test_a_project_decline_does_NOT_turn_off_the_environments_vault(home, d):
    """Recording an answer is not an opt-out, and only the first was asked for. A project
    that declines while the environment configures a vault still resolves that root - so
    this flag can never silently stop a project mirroring."""
    from agentflow.config import load_environment, load_project, resolve_vault_root

    (Path(d) / "v").mkdir(exist_ok=True)
    _write_env(home, _env_with_vault(d, root=f"{d}/v"))
    _write_project(home, "foo", _valid_project_body(d) + f"{NL}    vault: null{NL}")
    assert resolve_vault_root(load_environment(), load_project("foo")).endswith("v")


def test_the_resolved_block_SAYS_declined_when_the_answer_was_recorded(home, d):
    """Same reason the vault_root line exists at all: a loop reads this block, and "no
    vault" and "asked, answered no" are different states to anybody debugging a mirror."""
    from agentflow.config import load_environment, load_project, render_resolved_block

    _write_env(home, _valid_env_body(d) + f"{NL}    vault: null{NL}")
    _write_project(home, "foo", _valid_project_body(d))
    block = render_resolved_block(load_environment(), load_project("foo"))
    assert "declined" in block and "no vault" in block


def test_the_resolved_block_does_NOT_say_declined_when_nobody_was_asked(home, d):
    """The counterpart. An unfinished install must keep reading as unfinished."""
    from agentflow.config import load_environment, load_project, render_resolved_block

    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d))
    assert "declined" not in render_resolved_block(load_environment(), load_project("foo"))


# --------------------------------------------------------------------------- #
# publish_repo — the PUBLIC publish target (epic p202)
#
# The class of bug these guard is the silent swallow: before unknown-key rejection
# landed, a key nothing read resolved to nothing and said nothing. So every test
# below asserts on a value that reached the far end (the dataclass, the rendered
# block, the doctor finding), never merely that loading did not raise.
# --------------------------------------------------------------------------- #
_PUBLISH_BLOCK = f"{NL}    publish_repo:{NL}      host_ref: pubowner/pubrepo{NL}"


def test_a_publish_repo_resolves_onto_the_project(home, d):
    _write_project(home, "foo", _valid_project_body(d) + _PUBLISH_BLOCK)
    proj = load_project("foo")
    assert proj.publish_repo is not None
    assert proj.publish_repo.host_ref == "pubowner/pubrepo"
    assert proj.publish_repo.cadence == config.DEFAULT_PUBLISH_CADENCE


def test_the_DEFAULT_CADENCE_LITERAL_is_pinned():
    """Both tests above compare against `config.DEFAULT_PUBLISH_CADENCE`, so neither
    says anything about its value — change the constant and they stay green while
    SCHEMA.md, the template and the runbook go on saying something else. The literal is
    load-bearing prose: the runbook's whole argument is that the cadence describes a
    human act and nothing may read it as a schedule."""
    assert config.DEFAULT_PUBLISH_CADENCE == "manual, en masse"


def test_the_cadence_is_overridable_and_read(home, d):
    body = _valid_project_body(d) + _PUBLISH_BLOCK + '      cadence: "twice a year, by hand"' + NL
    _write_project(home, "foo", body)
    assert load_project("foo").publish_repo.cadence == "twice a year, by hand"


def test_a_publish_repo_APPEARS_in_the_rendered_block(home, d):
    """Verification 1. The block is what a loop reads at session open; a publish target
    the resolver holds and the block never prints is swallowed as far as any reader is
    concerned."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d) + _PUBLISH_BLOCK)
    block = config.render_resolved_block(load_environment(), load_project("foo"))
    assert "[publish] pubowner/pubrepo" in block
    assert config.DEFAULT_PUBLISH_CADENCE in block
    # The rule travels with the line, because the line is what gets read.
    assert "READ-ONLY" in block


def test_an_ABSENT_publish_repo_resolves_and_renders_NOTHING(home, d):
    """The other half of verification 1, and the more important half: absent is the
    normal case. An unconditional "[publish] (none)" line would put a public repo in
    front of every loop on every project that has none."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d))
    proj = load_project("foo")
    assert proj.publish_repo is None          # never synthesised
    block = config.render_resolved_block(load_environment(), proj)
    assert "[publish]" not in block
    # Not `"publish_repo" not in block`: the fixture's tmp paths are named after the
    # test, so that assertion would fail on its own directory name and prove nothing.
    assert "READ-ONLY" not in block


# --------------------------------------------------------------------------- #
# [question_style] and vault_save_skill in the resolved block (p144)
#
# Every seat is told to honour question_style and the done-gate is told to file its note
# only when a save skill is configured, so a value the block does not print is a value
# no seat can act on.
# --------------------------------------------------------------------------- #
def _block_lines(home, d, env_extra: str = "", proj_extra: str = "") -> list:
    _write_env(home, _valid_env_body(d) + env_extra)
    _write_project(home, "foo", _valid_project_body(d) + proj_extra)
    return config.render_resolved_block(load_environment(), load_project("foo")).split("\n")


def _save_skill(d: str, name: str) -> str:
    p = Path(d) / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# save\n", encoding="utf-8")
    return str(p).replace("\\", "/")


def _save_lines(lines: list) -> list:
    # Anchored on the line start: the tmp dirs carry the test name, so a substring
    # check would match a path and prove nothing.
    return [ln for ln in lines if ln.strip().startswith("vault_save_skill =")]


def test_question_style_default_is_PRINTED_after_active_builders(home, d):
    lines = _block_lines(home, d)
    at = lines.index(next(ln for ln in lines if ln.startswith("[active builders] ")))
    assert lines[at + 1] == "[question_style] mobile-two-turn"


def test_question_style_inline_is_PRINTED(home, d):
    lines = _block_lines(home, d, env_extra="\n    question_style: inline")
    assert [ln for ln in lines if ln.startswith("[question_style]")] == ["[question_style] inline"]


def test_environment_save_skill_is_PRINTED_under_the_vault(home, d):
    skill = _save_skill(d, "envsave")
    lines = _block_lines(home, d, env_extra=f"\n    vault:\n      root: {d}\n      save_skill: {skill}")
    got = _save_lines(lines)
    assert len(got) == 1
    assert got[0].startswith("  vault_save_skill = ")
    assert Path(got[0].split(" = ", 1)[1]) == Path(skill)
    # Inside the vault section, after vault_projects.
    at = lines.index(next(ln for ln in lines if ln.startswith("  vault_projects = ")))
    assert lines[at + 1] == got[0]


def test_project_save_skill_wins_over_the_environments(home, d):
    env_skill = _save_skill(d, "envsave")
    proj_skill = _save_skill(d, "projsave")
    lines = _block_lines(
        home, d,
        env_extra=f"\n    vault:\n      root: {d}\n      save_skill: {env_skill}",
        proj_extra=f"\n    vault:\n      root: {d}\n      save_skill: {proj_skill}",
    )
    got = _save_lines(lines)
    assert len(got) == 1
    assert Path(got[0].split(" = ", 1)[1]) == Path(proj_skill)


def test_no_save_skill_line_when_the_vault_sets_none(home, d):
    lines = _block_lines(home, d, env_extra=f"\n    vault:\n      root: {d}")
    assert any(ln.startswith("  vault_root   = ") for ln in lines)
    assert _save_lines(lines) == []


def test_no_save_skill_line_when_there_is_no_vault(home, d):
    assert _save_lines(_block_lines(home, d)) == []


def test_resolve_vault_save_skill_precedence():
    from types import SimpleNamespace as NS
    env = NS(vault=NS(save_skill="/env"))
    assert config.resolve_vault_save_skill(env, None) == "/env"
    assert config.resolve_vault_save_skill(env, NS(vault=NS(save_skill="/proj"))) == "/proj"
    assert config.resolve_vault_save_skill(env, NS(vault=NS(save_skill=None))) == "/env"
    assert config.resolve_vault_save_skill(NS(vault=None), NS(vault=None)) is None


# vault.autodoc_update_skill in the resolved block: lead-dev and the orchestrator follow
# `<vault.autodoc_update_skill>` after a merge, so the block prints it under that spelling,
# project over environment, beside vault_save_skill.
def _autodoc_lines(lines: list) -> list:
    return [ln for ln in lines if ln.strip().startswith("vault.autodoc_update_skill =")]


def _vault_extra(d: str, **fields) -> str:
    body = f"\n    vault:\n      root: {d}"
    for key, val in fields.items():
        body += f"\n      {key}: {val}"
    return body


def test_project_autodoc_update_skill_is_PRINTED(home, d):
    skill = _save_skill(d, "projautodoc")
    lines = _block_lines(home, d, proj_extra=_vault_extra(d, autodoc_update_skill=skill))
    got = _autodoc_lines(lines)
    assert len(got) == 1
    assert got[0].startswith("  vault.autodoc_update_skill = ")
    assert Path(got[0].split(" = ", 1)[1]) == Path(skill)


def test_environment_autodoc_update_skill_is_PRINTED_beside_the_save_skill(home, d):
    save = _save_skill(d, "envsave")
    skill = _save_skill(d, "envautodoc")
    lines = _block_lines(
        home, d, env_extra=_vault_extra(d, save_skill=save, autodoc_update_skill=skill)
    )
    got = _autodoc_lines(lines)
    assert len(got) == 1
    assert Path(got[0].split(" = ", 1)[1]) == Path(skill)
    at = lines.index(_save_lines(lines)[0])
    assert lines[at + 1] == got[0]


def test_project_autodoc_update_skill_wins_over_the_environments(home, d):
    env_skill = _save_skill(d, "envautodoc")
    proj_skill = _save_skill(d, "projautodoc")
    lines = _block_lines(
        home, d,
        env_extra=_vault_extra(d, autodoc_update_skill=env_skill),
        proj_extra=_vault_extra(d, autodoc_update_skill=proj_skill),
    )
    got = _autodoc_lines(lines)
    assert len(got) == 1
    assert Path(got[0].split(" = ", 1)[1]) == Path(proj_skill)


def test_no_autodoc_update_skill_line_when_unset(home, d):
    lines = _block_lines(home, d, env_extra=_vault_extra(d))
    assert any(ln.startswith("  vault_root   = ") for ln in lines)
    assert _autodoc_lines(lines) == []


def test_no_autodoc_update_skill_line_when_there_is_no_vault(home, d):
    assert _autodoc_lines(_block_lines(home, d)) == []


def test_resolve_vault_autodoc_update_skill_precedence():
    from types import SimpleNamespace as NS
    resolve = config.resolve_vault_autodoc_update_skill
    env = NS(vault=NS(autodoc_update_skill="/env"))
    assert resolve(env, None) == "/env"
    assert resolve(env, NS(vault=NS(autodoc_update_skill="/proj"))) == "/proj"
    assert resolve(env, NS(vault=NS(autodoc_update_skill=None))) == "/env"
    assert resolve(NS(vault=None), NS(vault=None)) is None


# A blank skill path is unset. Path("") is Path("."), which exists, so before the blank check
# an empty value cleared the presence check and the block printed `= .` - a directory a seat
# would then try to follow as a skill.
_BLANK_SKILL_FIELDS = (("save_skill", _save_lines), ("autodoc_update_skill", _autodoc_lines))


@pytest.mark.parametrize("blank", ['""', '"   "'], ids=["empty", "whitespace"])
@pytest.mark.parametrize("level", ["env", "project"])
@pytest.mark.parametrize(("field", "pick"), _BLANK_SKILL_FIELDS,
                         ids=[f for f, _ in _BLANK_SKILL_FIELDS])
def test_a_blank_skill_path_is_absent_from_the_block(home, d, field, pick, level, blank):
    extra = _vault_extra(d, **{field: blank})
    lines = _block_lines(home, d, **{"env_extra" if level == "env" else "proj_extra": extra})
    assert any(ln.startswith("  vault_root   = ") for ln in lines)
    assert pick(lines) == []


@pytest.mark.parametrize(("field", "pick"), _BLANK_SKILL_FIELDS,
                         ids=[f for f, _ in _BLANK_SKILL_FIELDS])
def test_a_blank_project_skill_path_falls_back_to_the_environments(home, d, field, pick):
    env_skill = _save_skill(d, "env" + field)
    lines = _block_lines(
        home, d,
        env_extra=_vault_extra(d, **{field: env_skill}),
        proj_extra=_vault_extra(d, **{field: '""'}),
    )
    got = pick(lines)
    assert len(got) == 1
    assert Path(got[0].split(" = ", 1)[1]) == Path(env_skill)


def test_a_bad_publish_host_ref_is_rejected_as_loudly_as_the_canonical_one(home, d):
    body = _valid_project_body(d) + f"{NL}    publish_repo:{NL}      host_ref: noslash{NL}"
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "publish_repo.host_ref"


def test_a_publish_block_without_a_host_ref_is_loud(home, d):
    body = _valid_project_body(d) + f"{NL}    publish_repo:{NL}      cadence: whenever{NL}"
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "publish_repo.host_ref"


def test_an_EMPTY_publish_block_is_loud_rather_than_silently_absent(home, d):
    """`publish_repo:` with no body is the one shape that would still swallow silently:
    `_optional_block` notes the key (so unknown-key rejection is satisfied) and returns
    None (so nothing resolves, nothing renders, and doctor says "no publish target
    configured" about a profile that visibly declares one)."""
    body = _valid_project_body(d) + f"{NL}    publish_repo:{NL}"
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "publish_repo"


def test_an_EMPTY_cadence_is_loud_rather_than_a_blank_descriptor(home, d):
    """`_opt_str` falls back to the default only for absent/None, so `cadence: ""`
    survives and prints `(cadence: )` — dropping the one sentence in the rendered
    block that says publishing is a human act."""
    body = _valid_project_body(d) + _PUBLISH_BLOCK + '      cadence: ""' + NL
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "publish_repo.cadence"


def test_an_unknown_key_INSIDE_publish_repo_is_still_rejected(home, d):
    """The new block must not become a hole in the unknown-key net."""
    body = _valid_project_body(d) + _PUBLISH_BLOCK + "      branch: main" + NL
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "publish_repo.branch"


def test_the_resolver_never_hands_publish_repo_back_as_a_GIT_REMOTE(home, d):
    """Verification 2's code-side counterpart (scope item 2).

    Every field a consumer takes a git remote or a working tree from is enumerated here
    and asserted NOT to carry the publish target. `PublishRepo` also carries no
    `local_checkout` and no `base_branch` at all, so there is nothing on it for a builder
    to clone, branch from, or merge into — asserted, not assumed, because adding either
    field later is exactly how this rule would erode.
    """
    _write_project(home, "foo", _valid_project_body(d) + _PUBLISH_BLOCK)
    proj = load_project("foo")
    publish = proj.publish_repo.host_ref
    assert proj.canonical_repo.host_ref != publish
    assert proj.canonical_repo.local_checkout != publish
    assert proj.deprecated_mirror != publish
    assert proj.deprecated_mirror_checkout != publish
    assert not hasattr(proj.publish_repo, "local_checkout")
    assert not hasattr(proj.publish_repo, "base_branch")


def test_the_blocks_repo_line_still_names_the_WORKING_repo(home, d):
    """`[project] <name> (repo: ...)` is the line every loop skill reads to decide which
    repo to work in. A publish target must never displace it."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d) + _PUBLISH_BLOCK)
    block = config.render_resolved_block(load_environment(), load_project("foo"))
    assert "[project] foo (repo: owner/repo)" in block


# --------------------------------------------------------------------------- #
# doctor reports the working/publish split (verification 3)
#
# These live here rather than in test_doctor.py because this epic's write scope is
# the config surface plus doctor.py itself; the check under test is a pure function
# of a resolved project, so it needs no doctor fixture.
# --------------------------------------------------------------------------- #
def test_doctor_reports_NO_PUBLISH_TARGET_as_INFO_not_a_failure(home, d):
    from agentflow import doctor

    _write_project(home, "foo", _valid_project_body(d))
    f = doctor._check_publish(load_project("foo"))
    assert f.status == doctor.INFO
    assert not f.failed
    assert "no publish target configured" in f.detail
    assert "owner/repo" in f.detail          # the working repo is still named


def test_doctor_prints_the_split_with_its_direction(home, d):
    from agentflow import doctor

    _write_project(home, "foo", _valid_project_body(d) + _PUBLISH_BLOCK)
    f = doctor._check_publish(load_project("foo"))
    assert f.status == doctor.OK
    assert "working owner/repo -> publish pubowner/pubrepo" in f.detail


def test_doctor_FAILS_when_the_publish_target_IS_the_working_repo(home, d):
    """The misconfiguration the check exists for: two lines of YAML far apart that
    happen to name the same repo, so the "split" publishes the private history to
    itself."""
    from agentflow import doctor

    body = _valid_project_body(d) + f"{NL}    publish_repo:{NL}      host_ref: owner/repo{NL}"
    _write_project(home, "foo", body)
    f = doctor._check_publish(load_project("foo"))
    assert f.status == doctor.FAIL
    assert f.remediation


@pytest.mark.parametrize("same_repo_written_differently", ["Owner/Repo", "owner/repo.git", "OWNER/REPO.git"])
def test_doctor_FAILS_when_the_split_is_only_a_DIFFERENCE_OF_SPELLING(home, d, same_repo_written_differently):
    """The FAIL tier defeated by letter case is worse than no FAIL tier: it prints
    `working owner/repo -> publish Owner/Repo`, which reads to a human as two repos,
    and the runbook's precondition tells the publisher to trust that line before an
    unrevertible push. Owner and repo names are case-insensitive on the hosts this
    suite targets, and `.git` is a clone-URL suffix; both forms clear the resolver's
    `owner/repo` shape check, so both reach the comparison."""
    from agentflow import doctor

    body = (_valid_project_body(d)
            + f"{NL}    publish_repo:{NL}      host_ref: {same_repo_written_differently}{NL}")
    _write_project(home, "foo", body)
    f = doctor._check_publish(load_project("foo"))
    assert f.status == doctor.FAIL, f.detail
    assert f.remediation


def test_a_GENUINELY_different_repo_still_reads_OK(home, d):
    """Control for the test above: normalising must not collapse two real repos."""
    from agentflow import doctor

    body = _valid_project_body(d) + f"{NL}    publish_repo:{NL}      host_ref: owner/repo-public{NL}"
    _write_project(home, "foo", body)
    assert doctor._check_publish(load_project("foo")).status == doctor.OK


def test_the_publish_check_is_actually_WIRED_into_diagnose(home, d, monkeypatch):
    """A check nothing calls is a check that cannot fire. Other findings in this run
    fail (there is no checkout, registry or spec tree under the fixture) — irrelevant
    here; what is asserted is that `publish` is among them at all."""
    from agentflow import doctor

    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d) + _PUBLISH_BLOCK)
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    checks = {f.check: f for f in doctor.diagnose("foo")}
    assert "publish" in checks
    assert "pubowner/pubrepo" in checks["publish"].detail


# --------------------------------------------------------------------------- #
# Verification 2 — the grep gate: no builder instruction may name `publish_repo`
# in a push, PR, or grounding action.
#
# WHY THIS IS NOT "assert publish_repo is absent from every SKILL.md": that gate
# would go red the day somebody adds the legitimate read-only echo line the spec
# defers, so it would be deleted rather than obeyed. What is forbidden is naming
# the key in an ACTION — a push, a pull request, a grounding, a remote — not
# naming it at all.
#
# SCOPE IS RECURSIVE, and that is the whole of the first widening. This gate's
# first draft globbed `skills/*/SKILL.md`, which is 20 files; the first-party
# instruction surface under `skills/` alone is 50-odd markdown files, and the ONE
# first-party file that already carries `git push` / `gh pr create` prose —
# `skills/orchestrator/prompts/implementer-with-verifier-loop.md` — sat outside it.
# A gate that reports clean over the files it cannot see is the failure mode a gate
# exists to prevent. `.github/workflows/ci.yml`'s worktree-tool gate records this
# same repo learning the same lesson twice: "Each widening answered the instance;
# recursion answers the class." So: recurse, over every first-party `.md` and `.js`
# in the directories that hold builder-facing instructions, `_vendored` excluded.
#
# `docs/` IS DELIBERATELY OUT, and it is the only deliberate hole. `docs/runbooks/
# publish.md` is the sanctioned human procedure for a single-commit cut; it names the
# key beside the word "push" because that is its subject, and it is addressed to a
# person, not a builder. Scanning it would make the runbook unwritable.
#
# WHAT IT CANNOT DO, stated so nobody reads more into a green run than it earns:
# the unit of judgement is a SENTENCE (markdown item / table row / code fence
# boundaries also split), and a prohibition counts only when it NEGATES AN ACTION
# ("never push", "no loop pushes", "not a push target") or the sentence says
# "read-only". That is a large step up from the first draft, which cleared a whole
# blank-line block on any negation anywhere in it — measured against the real skill
# tree with every `canonical_repo.host_ref` rewritten to `publish_repo.host_ref`,
# the block rule caught 1 of 17 action-bearing blocks and this rule catches 12,
# with the five residuals being prose that names the key outside any push, PR or
# grounding sentence. It is still a lint, not a proof: a negation that happens to
# sit next to an unrelated action word in the same sentence will still clear it.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Directories that hold builder-facing instruction prose. `docs/` is excluded on
# purpose (see above); everything else that a loop or a dispatched subagent can be
# told to read is in scope, at any depth.
_GATE_DIRS = ("skills", "commands", "install", "templates", "harness", "hooks", "scripts")
_GATE_SUFFIXES = (".md", ".js")
_GATE_SKIP_PARTS = frozenset({"_vendored", "__pycache__", "node_modules"})

_PUBLISH_ACTIONS = (
    r"push(?:e[sd]|es|ing)?|pull\s+requests?|PRs?|merge[sd]?|merging|"
    r"ground(?:ed|ing|s)?|remotes?|clone[sd]?|checkout|fetch(?:e[sd]|ing)?|"
    r"worktrees?|base\s+branch|sync(?:e?[sd]|ing)?"
)
_PUBLISH_ACTION_RE = re.compile(rf"\b({_PUBLISH_ACTIONS})\b", re.I)

# A prohibition only counts when it negates an ACTION. Bare `not` does not: it is
# one of the commonest words in this repo's prose, and treating it as a blanket
# clearance is exactly how the first draft let 16 of 17 mutated violations through.
_NEGATED_ACTION_RE = re.compile(
    rf"\b(?:never|not|n't|cannot|can\s*not|forbidden|refuse[sd]?|avoid)\b"
    rf"(?:\s+\w+){{0,3}}\s+(?:{_PUBLISH_ACTIONS})\b",
    re.I,
)
_NO_ACTOR_RE = re.compile(rf"\bno\s+(?:\w+\s+){{0,2}}(?:{_PUBLISH_ACTIONS})\b", re.I)
_READ_ONLY_RE = re.compile(r"read[-\s]?only", re.I)

# A markdown line that starts a new statement: list item, heading, blockquote,
# table row, fence. Joining across these would merge an instruction with a
# neighbouring caveat that was never about it.
_MD_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|>\s|\||```)")


def _publish_sentences(text: str):
    """Yield sentence-sized units of markdown prose.

    Lines are joined inside a paragraph (markdown wraps sentences across lines, and
    splitting on newlines would hide a violation as reliably as it would hide its
    negation), then split on sentence terminators.
    """
    chunks: list[list[str]] = [[]]
    for line in text.splitlines():
        if _MD_ITEM_RE.match(line) or not line.strip():
            chunks.append([])
        chunks[-1].append(line.strip())
    for chunk in chunks:
        joined = " ".join(x for x in chunk if x)
        for sentence in re.split(r"(?<=[.!?;])\s+", joined):
            if sentence.strip():
                yield sentence.strip()


def _publish_repo_violations(text: str, where: str = "<text>") -> list:
    """Sentences naming `publish_repo` in an action with no prohibition attached."""
    out = []
    for sentence in _publish_sentences(text):
        if "publish_repo" not in sentence:
            continue
        if not _PUBLISH_ACTION_RE.search(sentence):
            continue
        if (_NEGATED_ACTION_RE.search(sentence)
                or _NO_ACTOR_RE.search(sentence)
                or _READ_ONLY_RE.search(sentence)):
            continue
        out.append(f"{where}: {sentence[:200]}")
    return out


def _gate_corpus() -> list:
    """Every first-party instruction file the gate scans, recursively - FROM THE GIT INDEX.

    Not a tree walk (gh#175). `harness/records/`, `harness/toy-repo*/`, `harness/BASELINE.md` and
    the `_scratch/` trees are gitignored RUN ARTEFACTS sitting inside a gate dir, so a walk judged
    generated output as first-party builder prose and could red this suite with `git status`
    clean - which CI, checking out tracked files only, can never reproduce.

    THE INDEX SAYS WHICH FILES EXIST, NOT WHICH ARE FIRST-PARTY, so both exclusions stay:
    `_vendored` is TRACKED and dropped on purpose, and `docs/` is excluded by omission from
    `_GATE_DIRS` for the reason the header comment gives. Conflating the two would pull thirty
    vendored skill docs into a gate whose failure message is addressed to this repo's authors.
    """
    files = set()
    for rel in tracked_paths(_REPO_ROOT):
        parts = rel.split("/")
        if parts[0] not in _GATE_DIRS and rel != "CLAUDE.md":
            continue
        if Path(rel).suffix.lower() not in _GATE_SUFFIXES:
            continue
        if _GATE_SKIP_PARTS & set(parts):
            continue
        path = _REPO_ROOT / rel
        assert path.is_file(), f"tracked but missing from the working tree: {rel}"
        files.add(path)
    return sorted(files)


def test_the_gate_catches_a_PUSH_instruction():
    """Red-proof, permanent: the exact prose the gate exists to stop."""
    bad = "Push the merged branch to `publish_repo` once the run is green."
    assert _publish_repo_violations(bad)


def test_the_gate_catches_a_PR_instruction():
    assert _publish_repo_violations("Open a pull request against `publish_repo`.")


def test_the_gate_catches_a_GROUNDING_instruction():
    assert _publish_repo_violations("Ground against `publish_repo` before selecting work.")


def test_the_gate_catches_a_REMOTE_instruction():
    assert _publish_repo_violations("Add `publish_repo` as a git remote and fetch it.")


def test_the_gate_catches_a_SYNC_instruction():
    """`sync` is an action verb too — a mirror by another name is still a push."""
    assert _publish_repo_violations("Sync `publish_repo` from master at the end of every epic.")


def test_the_gate_catches_a_PUSH_paired_with_an_UNRELATED_negation():
    """The finding that made the block-scoped first draft worthless: a real push
    instruction sitting beside an ordinary caveat. `not` is not a prohibition unless
    it negates the action."""
    assert _publish_repo_violations(
        "Push the merged branch to `publish_repo` if the release is not a draft."
    )
    assert _publish_repo_violations(
        "Push to `publish_repo` after the run; nothing else is required."
    )


def test_the_gate_catches_a_violation_in_a_NEIGHBOURING_sentence_of_a_legal_one():
    """Prohibition scope is the sentence, not the paragraph: a sanctioned read-only
    sentence must not launder a violating one written next to it."""
    text = (
        "`publish_repo` is READ-ONLY: never push to it. "
        "At release time, push the branch to `publish_repo` yourself."
    )
    assert _publish_repo_violations(text)


def test_the_gate_ALLOWS_the_read_only_echo_line():
    """The deferred echo line must stay legal, or the gate gets deleted instead of
    obeyed the first time somebody writes the sanctioned sentence."""
    ok = ("`publish_repo` is READ-ONLY: never push to it, open a PR against it, or "
          "ground against it. Publishing is a human action.")
    assert _publish_repo_violations(ok) == []


def test_the_gate_ALLOWS_the_no_loop_phrasing():
    assert _publish_repo_violations(
        "No loop pushes to `publish_repo`, opens a PR against it, or grounds against it."
    ) == []


def test_the_gate_ALLOWS_naming_the_key_with_no_action_at_all():
    assert _publish_repo_violations("Echo the `[publish]` line when `publish_repo` is set.") == []


def test_the_gate_is_SILENT_on_prose_that_never_names_the_key():
    assert _publish_repo_violations("Push the branch and open a PR against the base branch.") == []


def test_the_gate_corpus_reaches_BELOW_the_skill_roots():
    """Coverage assertion, not a prose assertion. The first draft globbed
    `skills/*/SKILL.md` and so could never see the orchestrator's dispatched briefs —
    the one place in this tree that already carries push and PR instructions, and
    therefore the likeliest place a publish_repo push would be written next."""
    corpus = _gate_corpus()
    brief = _REPO_ROOT / "skills" / "orchestrator" / "prompts" / "implementer-with-verifier-loop.md"
    assert brief.is_file(), f"expected first-party brief missing: {brief}"
    assert brief in corpus, "the gate cannot see the orchestrator's dispatched briefs"
    assert (_REPO_ROOT / "commands" / "lead-dev.md") in corpus
    assert (_REPO_ROOT / "install" / "SKILL.md") in corpus
    assert len(corpus) > 60, f"corpus collapsed to {len(corpus)} files — the gate is scanning a stub"


def test_NO_FIRST_PARTY_INSTRUCTION_names_publish_repo_in_a_push_PR_or_grounding_action():
    """Verification 2 over the real tree, recursively."""
    corpus = _gate_corpus()
    assert corpus, f"no instruction files found under {_REPO_ROOT} — the gate is scanning nothing"
    violations = []
    for path in corpus:
        violations += _publish_repo_violations(
            path.read_text(encoding="utf-8", errors="replace"),
            where=path.relative_to(_REPO_ROOT).as_posix(),
        )
    assert violations == [], (
        "publish_repo is READ-ONLY to every builder: no loop pushes to it, opens a PR "
        "against it, or grounds against it (SCHEMA.md §2). Offending sentence(s):"
        + NL + NL.join(violations)
    )


# --------------------------------------------------------------------------- #
# work_source.kind vs git_host.kind: the pairing spans two files, so it is checked on both
# --------------------------------------------------------------------------- #
def _env_on_host(d: str, kind: str, cli: str) -> str:
    return _valid_env_body(d).replace(
        "git_host:\n      kind: local", f"git_host:\n      kind: {kind}\n      cli: {cli}"
    )


def _project_with_work_source(d: str, kind: str) -> str:
    return _valid_project_body(d) + f"""
    work_source:
      kind: {kind}
    """


@pytest.mark.parametrize("host_kind,cli", [("github", "gh"), ("gitea", "gh")])
def test_gitea_work_source_with_a_tracker_cli_is_flagged(home, d, host_kind, cli):
    """Intake refuses gitea on any host once the cli reaches a tracker, so the flag follows
    the cli, not the host kind."""
    _write_env(home, _env_on_host(d, host_kind, cli))
    _write_project(home, "foo", _project_with_work_source(d, "gitea"))
    env, proj = load_environment(), load_project("foo")
    assert (env.git_host.kind, env.git_host.cli) == (host_kind, cli)
    reason = config.work_source_host_conflict(env, proj)
    assert reason is not None and "'gitea'" in reason and f"'{cli}'" in reason
    flagged = [ln for ln in config.render_resolved_block(env, proj).split("\n")
               if ln.startswith("[work_source] CONFLICT")]
    assert flagged == [f"[work_source] CONFLICT: {reason}"]


@pytest.mark.parametrize(
    "host_kind,ws_kind,cli",
    [("gitea", "gitea", "none"), ("github", "github-issues", "gh"),
     ("github", "filesystem", "gh"), ("github", "gitea", "none")],
)
def test_pairings_intake_accepts_are_not_flagged(home, d, host_kind, ws_kind, cli):
    """The control: a verified tracker, no tracker, or gitea with cli none (the documented
    no-op) is not a conflict, and the block prints no CONFLICT line."""
    _write_env(home, _env_on_host(d, host_kind, cli))
    _write_project(home, "foo", _project_with_work_source(d, ws_kind))
    env, proj = load_environment(), load_project("foo")
    assert config.work_source_host_conflict(env, proj) is None
    assert "[work_source] CONFLICT" not in config.render_resolved_block(env, proj)
    assert config.work_source_host_conflict(env, None) is None


def _work_source_lines(env, proj) -> list:
    return [ln for ln in config.render_resolved_block(env, proj).split("\n")
            if ln.startswith("[work_source] ") and "CONFLICT" not in ln]


def test_work_source_defaults_are_PRINTED_when_the_block_is_absent(home, d):
    """gh#1031: a project with no work_source block still shows the three keys intake reads,
    as their defaults, on one line in the project section after [verification]."""
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d))
    env, proj = load_environment(), load_project("foo")
    assert _work_source_lines(env, proj) == [
        "[work_source] kind: filesystem, bug_triage: False, open_issue_page: 200"
    ]
    lines = config.render_resolved_block(env, proj).split("\n")
    at = lines.index(_work_source_lines(env, proj)[0])
    assert lines[at - 1].startswith("[verification] ")


def test_work_source_explicit_values_are_PRINTED(home, d):
    _write_env(home, _env_on_host(d, "github", "gh"))
    _write_project(home, "foo", _valid_project_body(d) + """
    work_source:
      kind: github-issues
      bug_triage: true
      open_issue_page: 50
    """)
    env, proj = load_environment(), load_project("foo")
    assert _work_source_lines(env, proj) == [
        "[work_source] kind: github-issues, bug_triage: True, open_issue_page: 50"
    ]


def test_work_source_line_sits_beside_a_CONFLICT_line(home, d):
    """The settings line does not replace the conflict flag: both print, settings first."""
    _write_env(home, _env_on_host(d, "github", "gh"))
    _write_project(home, "foo", _project_with_work_source(d, "gitea"))
    env, proj = load_environment(), load_project("foo")
    ws = [ln for ln in config.render_resolved_block(env, proj).split("\n")
          if ln.startswith("[work_source]")]
    assert ws[0] == "[work_source] kind: gitea, bug_triage: False, open_issue_page: 200"
    assert len(ws) == 2 and ws[1].startswith("[work_source] CONFLICT: ")


# --------------------------------------------------------------------------- #
# gh#881 - an unknown or path-shaped project name says what went wrong, on line one
# --------------------------------------------------------------------------- #
# Seats ntfy the first line of a config failure to a phone, so the name, where it came
# from, the configured names and the closest match all have to be on it - and the
# "run the installer" advice only belongs to a machine with nothing configured.
def _first_line(exc: ConfigError) -> str:
    return str(exc).splitlines()[0]


def test_an_unknown_project_from_the_argument_names_source_list_and_guess(home, d, monkeypatch):
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.setenv("AGENTFLOW_PROJECT", "alpha")
    name = config.resolve_project_name("alpah")
    with pytest.raises(ConfigError) as ei:
        load_project(name)
    line = _first_line(ei.value)
    assert "unknown project 'alpah' (from --project)" in line, line
    assert "configured: alpha, beta" in line, line
    assert "did you mean 'alpha'?" in line, line
    assert "installer" not in str(ei.value)
    assert "--project" in ei.value.remediation
    # Callers report a missing project file by this field path without echoing the text.
    assert ei.value.field_path == "<file>"


def test_an_unknown_project_from_the_env_var_says_so(home, d, monkeypatch):
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.setenv("AGENTFLOW_PROJECT", "zzz")
    with pytest.raises(ConfigError) as ei:
        load_project(config.resolve_project_name(None))
    line = _first_line(ei.value)
    assert "unknown project 'zzz' (from $AGENTFLOW_PROJECT)" in line, line
    assert "did you mean" not in line, "no near match, so no guess"
    assert "$AGENTFLOW_PROJECT" in ei.value.remediation


def test_an_explicit_source_beats_the_remembered_resolution(home, d):
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    with pytest.raises(ConfigError) as ei:
        load_project("gamma", source="from a test")
    assert "unknown project 'gamma' (from a test)" in _first_line(ei.value)


def test_show_cli_unknown_project_puts_the_facts_on_the_errors_first_line(home, d, monkeypatch, capsys):
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    assert config.main(["show", "--project", "betta"]) == 1  # exit code unchanged
    err = capsys.readouterr().err.splitlines()
    # Line 1 is what a seat's ntfy carries, so the facts sit there and the headline after.
    assert err[0].startswith("[<file>] "), err
    assert "unknown project 'betta' (from --project); configured: alpha, beta; did you mean 'beta'?" in err[0]
    assert err[1] == "agentflow config resolution FAILED", err
    assert any("remediation:" in ln for ln in err[2:]), err


_HOOK = Path(__file__).resolve().parents[3] / "hooks" / "sessionstart-inject-config.py"


def _run_hook_main():
    """Load the SessionStart hook from THIS tree and run its main() in-process."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_sessionstart_hook_under_test", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main()


def test_hook_unknown_project_puts_the_facts_on_stderr_line_one(home, d, monkeypatch, capsys):
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.setenv("AGENTFLOW_PROJECT", "betta")
    assert _run_hook_main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    err = captured.err.splitlines()
    assert "unknown project 'betta' (from $AGENTFLOW_PROJECT)" in err[0], err
    assert "configured: alpha, beta" in err[0], err
    assert err[1] == "agentflow SessionStart config injection FAILED", err
    assert any("remediation:" in ln for ln in err[2:]), err


def test_hook_and_show_lead_with_the_same_error_line(home, d, monkeypatch, capsys):
    """The two emitters share one formatter: same line 1, only the headline differs."""
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.setenv("AGENTFLOW_PROJECT", "zzz")
    assert _run_hook_main() == 1
    hook_err = capsys.readouterr().err.splitlines()
    assert config.main(["show"]) == 1
    show_err = capsys.readouterr().err.splitlines()
    assert hook_err[0] == show_err[0]
    assert "unknown project 'zzz'" in show_err[0], show_err
    assert hook_err[2:] == show_err[2:]


def test_failure_report_puts_a_multiline_reasons_first_line_first(tmp_path):
    exc = ConfigError("a.b", tmp_path / "x.md", "first fact\nsecond fact", "fix it")
    lines = config.failure_report(exc, "HEADLINE").splitlines()
    assert lines[0] == "[a.b] first fact"
    assert lines[1] == "HEADLINE"
    assert lines[2:] == str(exc).splitlines()


def test_failure_report_skips_blank_leading_lines_of_the_reason(tmp_path):
    exc = ConfigError("a.b", tmp_path / "x.md", "\n  \nthe fact", "fix it")
    assert config.failure_report(exc, "HEADLINE").splitlines()[0] == "[a.b] the fact"


def test_with_no_project_configured_the_installer_remediation_stays(home, d):
    _write_env(home, _valid_env_body(d))
    with pytest.raises(ConfigError) as ei:
        load_project("anything")
    assert "config file not found" in ei.value.reason
    assert "run the agentflow installer" in ei.value.remediation


@pytest.mark.parametrize("bad", ["../environment", "sub/alpha", r"sub\alpha", "C:alpha", "..", "."])
def test_a_path_shaped_project_name_is_rejected_before_any_file_is_read(home, d, bad):
    # `../environment` exists, so without the check it loaded environment.md as a project
    # and failed on a missing `name:` - an error pointing nowhere near the typo.
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    (home / "projects" / "sub").mkdir()
    _named_project(home / "projects" / "sub", "alpha", d)
    with pytest.raises(ConfigError) as ei:
        load_project(bad, source="from --project")
    line = _first_line(ei.value)
    assert f"invalid project name '{bad}' (from --project)" in line, line
    assert ei.value.field_path == "<project>"


def test_a_correct_project_name_still_loads_unchanged(home, d, monkeypatch):
    """Control: the new checks sit in front of the load and must not change a good one."""
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    monkeypatch.setenv("AGENTFLOW_PROJECT", "beta")
    proj = load_project(config.resolve_project_name(None))
    assert proj.name == "beta"
    assert load_project("alpha").name == "alpha"


# --------------------------------------------------------------------------- #
# gh#882 - a misspelled REQUIRED key is named in the same run that reports it missing
# --------------------------------------------------------------------------- #
def test_a_misspelled_required_environment_key_names_the_stray(home, d):
    _write_env(home, _valid_env_body(d).replace("lock_dir:", "lockdir:"))
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "lock_dir"
    assert ei.value.reason == "required key 'lock_dir' is missing; found 'lockdir' - a misspelling?"
    assert "rename 'lockdir:' to 'lock_dir:'" in ei.value.remediation


def test_a_misspelled_nested_required_key_names_the_stray(home, d):
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", _valid_project_body(d).replace("strategy:", "stratgy:"))
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.field_path == "verification.strategy"
    assert "found 'stratgy' - a misspelling?" in ei.value.reason


def test_a_misspelled_required_int_and_block_name_the_stray(home, d):
    _write_env(home, _valid_env_body(d).replace("schema_version:", "schema_verison:"))
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert "found 'schema_verison'" in ei.value.reason
    _write_env(home, _valid_env_body(d).replace("git_host:", "git_hots:"))
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.reason == "required block 'git_host' is missing; found 'git_hots' - a misspelling?"


def test_a_misspelled_worktree_root_names_the_stray(home, d):
    _write_env(home, _valid_env_body(d).replace("worktree_root:", "worktreeroot:"))
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert "found 'worktreeroot' - a misspelling?" in ei.value.reason
    assert "worktree_root: ~/worktrees" in ei.value.remediation  # the field's own advice kept


def test_a_missing_required_key_with_no_near_match_is_unchanged(home, d):
    """Control: no stray spelling, so the message and remediation are exactly the old ones."""
    _write_env(home, _valid_env_body(d).replace(f"    lock_dir: {d}\n", "    unrelated_thing: 1\n"))
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.reason == "required key 'lock_dir' is missing"
    assert ei.value.remediation == "add 'lock_dir:' (string)"


def test_a_stem_with_inner_dots_is_legal_and_fails_only_as_unknown(home, d):
    _write_env(home, _valid_env_body(d))
    _two_projects(home, d)
    with pytest.raises(ConfigError) as ei:
        load_project("a..b", source="from --project")
    assert ei.value.field_path == "<file>"
    assert "unknown project 'a..b'" in _first_line(ei.value)


def test_a_missing_key_never_suggests_renaming_a_legitimate_later_key(home, d):
    # `autodoc_root` is read AFTER `data_root`, so it is unread when data_root is found
    # missing - and is still a schema key, never a misspelling.
    body = _valid_env_body(d).replace(f"    data_root: {d}\n", f"    autodoc_root: {d}\n")
    _write_env(home, body)
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.reason == "required key 'data_root' is missing"


def test_a_missing_required_key_never_suggests_another_required_key(home, d):
    _write_env(home, _valid_env_body(d).replace(f"    suite_root: {d}\n", ""))
    with pytest.raises(ConfigError) as ei:
        load_environment()
    assert ei.value.field_path == "suite_root"
    assert ei.value.reason == "required key 'suite_root' is missing"


def test_a_project_schema_key_is_never_a_near_miss_candidate():
    # No REQUIRED project key sits near another project key today, so the pairs the review
    # reproduced are exercised on the helper directly: each sibling is a legitimate key.
    assert config._near_miss({"deprecated_mirror_checkout": "x"}, "deprecated_mirror", "") is None
    assert config._near_miss({"provision": "x"}, "polishing", "generation_skills") is None
    assert config._near_miss({"provision": "x"}, "provisio", "generation_skills") is None


def test_a_missing_project_block_still_names_an_unknown_near_spelling(home, d):
    """Control for the exclusion: a truly unknown key in a project file is still suggested."""
    body = _valid_project_body(d).replace(
        "    verification:\n      strategy: test-suite\n", "    verificaton: 1\n"
    )
    _write_env(home, _valid_env_body(d))
    _write_project(home, "foo", body)
    with pytest.raises(ConfigError) as ei:
        load_project("foo")
    assert ei.value.reason == "required block 'verification' is missing; found 'verificaton' - a misspelling?"


def test_a_missing_nested_key_names_an_unknown_sibling():
    assert config._near_miss({"provison": "x"}, "provision", "generation_skills") == "provison"


def _schema_reads_from_source() -> dict:
    """Per-block keys the two builders read, derived from config.py's own AST.

    Maps each block variable to its path through `x = _require_block(data, "k", ...)`, then
    collects the key literal of every reader call on it - the same calls that feed
    `_reject_unknown_keys`, so a reader added without updating `_SCHEMA_KEYS` fails here.
    """
    readers = {
        "_note", "_optional_block", "_require_block", "_require_int", "_require_str",
        "_require_enum", "_require_owner_repo", "_opt_bool", "_opt_int", "_opt_str", "_opt_list",
    }
    tree = ast.parse(Path(config.__file__).read_text(encoding="utf-8"))
    found: dict = {}
    for fn in tree.body:
        if not (isinstance(fn, ast.FunctionDef) and fn.name in ("_build_environment", "_build_project")):
            continue
        var = {"data": ""}
        for n in ast.walk(fn):
            if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                    and getattr(n.value.func, "id", None) == "_require_block"):
                var[n.targets[0].id] = n.value.args[1].value
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) in readers and len(n.args) >= 2:
                block, key = n.args[0], n.args[1]
                if isinstance(block, ast.Name) and isinstance(key, ast.Constant):
                    found.setdefault(var[block.id], set()).add(key.value)
    return {k: frozenset(v) for k, v in found.items()}


def test_schema_keys_match_what_the_builders_read():
    assert config._SCHEMA_KEYS == _schema_reads_from_source()


# --------------------------------------------------------------------------- #
# An explicit config home, per call, instead of a pinned $AGENTFLOW_CONFIG
# --------------------------------------------------------------------------- #
def _two_homes(tmp_path: Path, d: str) -> tuple:
    """Home A (in the variable) and home B (passed explicitly), each with its own profile."""
    a, b = tmp_path / "home-a", tmp_path / "home-b"
    _write_env(a, _valid_env_body(d).replace("Sample User", "Person A"))
    _write_env(b, _valid_env_body(d).replace("Sample User", "Person B"))
    _named_project(a, "alpha", d)
    _named_project(b, "beta", d)
    return a, b


def test_an_explicit_config_home_beats_the_variable_for_every_reader(tmp_path, d, monkeypatch):
    a, b = _two_homes(tmp_path, d)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(a))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)

    assert config.config_home(b) == b
    assert config.environment_path(b) == b / "environment.md"
    assert load_environment(config_home=b).person.name == "Person B"
    assert load_project("beta", config_home=b).name == "beta"
    assert config.resolve_project_name(None, config_home=b) == "beta"
    # The variable is read, never written, and the default path is unchanged.
    assert config.config_home() == a
    assert load_environment().person.name == "Person A"
    assert config.resolve_project_name(None) == "alpha"
    assert config.os.environ["AGENTFLOW_CONFIG"] == str(a)


def test_an_unknown_project_under_an_explicit_home_lists_THAT_homes_profiles(tmp_path, d,
                                                                             monkeypatch):
    """The failure path enumerates profiles too: under the variable's home it would name
    `alpha` while the caller asked about home B."""
    a, b = _two_homes(tmp_path, d)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(a))
    with pytest.raises(ConfigError) as exc:
        load_project("betx", config_home=b)
    assert "configured: beta" in str(exc.value)
    assert "alpha" not in str(exc.value)
