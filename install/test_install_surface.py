#!/usr/bin/env python3
"""The install surface a stranger meets first: gh#543, gh#418 item 1, gh#419 (plan-lint half).

Three shipped texts were wrong in a way that only misleads someone who follows the docs
literally, which is exactly who reads them first:

1. gh#543 - the documented Windows `lock_dir` could not resolve. Nothing expands `%VAR%`
   (the resolver and provision both use `Path(v).expanduser()` and nothing more), so a
   `%TEMP%/scratch` value is a relative path that provision refuses to pre-create.
2. gh#418 item 1 - the documented preflight example had no `--wave`, and `Workflow` is only
   required under `--wave`, so the example could not fail on the tool it claimed to check.
3. gh#419 - shipped text said a settings file wires `plan-lint.sh` as a PostToolUse hook.
   Nothing registers one. The docs pin and the fact it rests on are tested together here, so
   whoever later decides to register the hook is told the docs must change with it.

Each test goes red when its defect is restored. Every test is platform-neutral.

Run: `python -m pytest install/test_install_surface.py -v`
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402
from agentflow import preflight  # noqa: E402
from agentflow.tracked_paths import tracked_files  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TEMPLATE_ENV = REPO / "templates" / "environment.md"
SCHEMA_MD = REPO / "skills" / "_shared" / "SCHEMA.md"
INSTALL_MD = REPO / "docs" / "INSTALL.md"
MANIFEST_MD = REPO / "install" / "manifest.md"
BOOTSTRAP_PY = REPO / "skills" / "orchestrator" / "bootstrap.py"

# A Windows environment-variable token such as %TEMP%. Nothing in agentflow expands one.
PERCENT_VAR = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%")


def _template_lock_dir_block() -> list[str]:
    """The template's `lock_dir:` line plus its `#`-only continuation lines."""
    lines = TEMPLATE_ENV.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith("lock_dir:")]
    assert len(starts) == 1, f"expected one `lock_dir:` line in {TEMPLATE_ENV}, found {len(starts)}"
    block = [lines[starts[0]]]
    for ln in lines[starts[0] + 1:]:
        if not ln.lstrip().startswith("#") or not ln.startswith(" "):
            break
        block.append(ln)
    return block


# --------------------------------------------------------------------------- #
# 1. gh#543 - every documented lock_dir value resolves
# --------------------------------------------------------------------------- #
def test_template_lock_dir_documents_no_unexpanded_var():
    schema_lines = [
        ln for ln in SCHEMA_MD.read_text(encoding="utf-8").splitlines()
        if ln.startswith("lock_dir:") or ln.startswith("| `lock_dir` |")
    ]
    assert len(schema_lines) == 2, (
        f"expected the two lock_dir lines in SCHEMA.md (the YAML shape and the table row), "
        f"found {schema_lines}"
    )
    for ln in _template_lock_dir_block() + schema_lines:
        assert not PERCENT_VAR.search(ln), (
            f"a documented lock_dir value names an unexpanded %VAR%, which neither provision "
            f"nor the resolver expands - it is read as a relative path (gh#543): {ln!r}"
        )


def test_template_windows_lock_dir_example_is_precreatable(tmp_path, monkeypatch):
    comments = " ".join(ln.split("#", 1)[1] for ln in _template_lock_dir_block() if "#" in ln)
    examples = re.findall(r'"([^"]+)"', comments)
    assert len(examples) == 1, (
        f"expected exactly one quoted Windows example in the template's lock_dir comment, "
        f"found {examples}"
    )
    value = examples[0]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    target = Path(value).expanduser()

    assert target.is_absolute(), (
        f"the template's Windows lock_dir example {value!r} is not absolute after "
        f"expanduser ({target}), so provision will not pre-create it (gh#543)"
    )
    assert provision._unsafe_to_precreate(target) is None, (
        f"provision step 2 refuses the template's Windows lock_dir example {value!r}: "
        f"{provision._unsafe_to_precreate(target)}"
    )


# --------------------------------------------------------------------------- #
# 2. gh#418 item 1 - the documented preflight example checks Workflow
# --------------------------------------------------------------------------- #
def _preflight_lines(text: str, where: str) -> list[str]:
    found = [ln.strip() for ln in text.splitlines()
             if ln.strip().startswith("python -m agentflow.preflight ")]
    assert found, f"no `python -m agentflow.preflight` line in {where}"
    return found


def _install_preflight_section() -> str:
    text = INSTALL_MD.read_text(encoding="utf-8")
    head = "## Before you launch a loop: what is not installed by anything above"
    assert head in text, f"docs/INSTALL.md lost its section {head!r}"
    section = text.split(head, 1)[1]
    return re.split(r"^## ", section, maxsplit=1, flags=re.M)[0]


def test_documented_preflight_examples_require_workflow(capsys):
    docs = {
        "docs/INSTALL.md": _install_preflight_section(),
        "install/manifest.md": MANIFEST_MD.read_text(encoding="utf-8"),
    }
    for where, text in docs.items():
        for line in _preflight_lines(text, where):
            argv = shlex.split(line)
            assert argv[:3] == ["python", "-m", "agentflow.preflight"], argv
            args = argv[3:]
            assert "--tools" in args, f"{where}: the preflight example passes no --tools: {argv}"
            args[args.index("--tools") + 1] = "ScheduleWakeup,CronCreate"

            rc = preflight.main(args)
            out = capsys.readouterr().out

            assert rc == 1 and "MISSING  Workflow" in out, (
                f"{where}: the documented preflight command passes a session that lacks "
                f"Workflow (rc={rc}), so it cannot catch the absence it is there to catch "
                f"(gh#418). args={args} output={out!r}"
            )


# --------------------------------------------------------------------------- #
# 3. gh#419 - no shipped text claims a settings file wires plan-lint.sh
# --------------------------------------------------------------------------- #
PLAN_LINT_CLAIM_FILES = [
    "README.md",
    "docs/INSTALL.md",
    "install/SKILL.md",
    "install/manifest.md",
    "install/provision.py",
    "skills/_shared/tests/test_plan_lint_deploy.py",
]
# Matched over whitespace-flattened text, never per sentence: `.` sits inside both
# `plan-lint.sh` and `settings.json`. It also matches negated phrasings, so replacement
# prose must avoid these phrases rather than negate them. Wider than the spec's
# Verification 5 grep: its verb list alone let "settings.json registers ... as a
# PostToolUse hook" through.
PLAN_LINT_CLAIM = re.compile(
    r"settings(\.json)?`? (already )?"
    r"(wire|wires|wired|names|invokes|registers|registered|runs|calls)"
    r"|(wired|registered) (by|in) (the )?settings"
    r"|settings (file|hook) (wires|names|registers|runs)|path the settings",
    re.IGNORECASE,
)


def test_no_shipped_text_claims_settings_wire_plan_lint():
    hits = []
    for rel in PLAN_LINT_CLAIM_FILES:
        flat = re.sub(r"\s+", " ", (REPO / rel).read_text(encoding="utf-8"))
        for m in PLAN_LINT_CLAIM.finditer(flat):
            hits.append(f"{rel}: ...{flat[max(0, m.start() - 60):m.end() + 60]}...")
    assert not hits, (
        "shipped text claims a settings file wires plan-lint.sh. Nothing registers that hook "
        "(provision writes SessionStart, bootstrap writes PreToolUse), so the claim is false "
        "(gh#419):\n" + "\n".join(hits)
    )


def test_provision_registers_no_posttooluse_hook(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}", encoding="utf-8")
    opts = provision.Options(
        dry_run=False,
        suite_root=REPO,
        config_home=tmp_path / "config",
        settings_file=settings_file,
        autodoc=False,
        autodoc_reason="not under test",
        claude_root=tmp_path / "claude",
    )

    provision._do_sessionstart_hook(opts)
    # The bootstrap hook install, run the way provision step 8 runs it: the script, pointed
    # at the settings file by CLAUDE_SETTINGS_FILE. Extra patterns are cleared so the
    # caller's environment cannot change what is written.
    env = {k: v for k, v in os.environ.items() if k != "AGENTFLOW_EXTRA_AUTO_ALLOW"}
    env["CLAUDE_SETTINGS_FILE"] = str(settings_file)
    result = subprocess.run([sys.executable, str(BOOTSTRAP_PY)],
                            capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, f"bootstrap failed: {result.stdout!r} {result.stderr!r}"

    hooks = json.loads(settings_file.read_text(encoding="utf-8")).get("hooks", {})
    # Deliberately not an exact key set: bootstrap's hook set may grow, and an unrelated
    # new event must not turn this red. Only the plan-lint wiring is under test.
    assert "PostToolUse" not in hooks, (
        "provision now registers a PostToolUse hook. If that is the plan-lint hook, the docs "
        "pin in test_no_shipped_text_claims_settings_wire_plan_lint must change with it."
    )
    # Only the hook commands, with the suite root blanked: the SessionStart command embeds
    # the checkout path, and a worktree named for plan-lint must not turn this red.
    cmds = [h.get("command", "") for blocks in hooks.values() for b in blocks
            for h in b.get("hooks", [])]
    cmds = [c.replace(str(REPO), "<suite>") for c in cmds]
    assert not any("plan-lint" in c for c in cmds), (
        f"a provisioned hook command runs plan-lint: {cmds}"
    )


# --------------------------------------------------------------------------- #
# 4. gh#744 - the manifest's `_shared` citation names something that exists
# --------------------------------------------------------------------------- #
# The "why `_shared` does not deploy" paragraph used to cite a `$AF/skills/_shared` snippet in
# commands/uc-touchpoints.md as its example. Nothing sets `AF`, so the example was the defect,
# and a citation to a removed snippet would keep describing a mechanism that no longer exists.
_SHARED_WHY = "**Why the `_shared` DIRECTORY does not deploy:**"
_CITED_COMMAND = re.compile(r"`(commands/[a-z0-9-]+\.md)` runs\s+`(python -m agentflow\.[a-z_]+)`")
_CITED_SUITE_PATH = re.compile(r"`<suite_root>/(skills/_shared/[^`]+)`")


def _shared_why_paragraph() -> str:
    text = MANIFEST_MD.read_text(encoding="utf-8")
    assert _SHARED_WHY in text, f"install/manifest.md lost its paragraph {_SHARED_WHY!r}"
    paragraph = re.split(r"\r?\n[ \t]*\r?\n", text.split(_SHARED_WHY, 1)[1], maxsplit=1)[0]
    return re.sub(r"\s+", " ", paragraph)


def test_manifest_shared_citation_resolves():
    para = _shared_why_paragraph()
    assert "$AF" not in para, "install/manifest.md still cites the unset `$AF` snippet (gh#744)"

    suite_paths = _CITED_SUITE_PATH.findall(para)
    assert suite_paths, f"the paragraph cites no `<suite_root>/skills/_shared/...` path: {para!r}"
    for rel in suite_paths:
        assert (REPO / rel).is_file(), f"install/manifest.md cites `{rel}`, which does not exist"
        # From the git index (gh#410): an untracked SKILL.md must not satisfy this citation.
        skill_mds = tracked_files(
            REPO, lambda r: r.startswith("skills/") and r.count("/") == 2 and r.endswith("/SKILL.md")
        )
        citers = [p for p in skill_mds if f"<suite_root>/{rel}" in p.read_text(encoding="utf-8")]
        assert citers, f"install/manifest.md says SKILL.md files cite `<suite_root>/{rel}`; none do"

    commands = _CITED_COMMAND.findall(para)
    assert commands, f"the paragraph names no command and the module it runs: {para!r}"
    for rel, invocation in commands:
        path = REPO / rel
        assert path.is_file(), f"install/manifest.md cites `{rel}`, which does not exist"
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        assert any(ln.startswith(invocation + " ") for ln in lines), (
            f"install/manifest.md says `{rel}` runs `{invocation}`; no line of it does"
        )
