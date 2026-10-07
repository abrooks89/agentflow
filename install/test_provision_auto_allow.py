#!/usr/bin/env python3
"""The PreToolUse auto-allow hooks: honest text, and an opt-out that really opts out.

Two halves, one per defect.

HONEST. `skills/orchestrator/bootstrap.py` is the file README sends a careful reader to, and it
used to tell that reader `git push` and `git reset --hard` "still prompt" while `Bash(git *)`,
three screens down, matched both. The sentence scanner here keeps any future "still prompts" /
"not auto-allowed" claim from naming a command the list actually allows. It uses
`fnmatch.fnmatchcase` against each pattern's inner glob, which only APPROXIMATES Claude Code's
`if` matcher: good enough to catch a prefix glob swallowing a named command, not a model of
the runtime.

OPTIONAL. `provision.py --no-auto-allow` skips step 8 (the bootstrap run) and nothing else.
Step 9's SessionStart hook still lands, and the flag has to survive the step-4 venv handover or
the child process would install the hooks the operator declined.
"""

from __future__ import annotations

import ast
import fnmatch
import importlib.util
import io
import json
import os
import re
import sys
import tokenize
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO / "skills" / "orchestrator" / "bootstrap.py"

# FIXED before the rewrite, not chosen after it: every phrase that claims a command is left
# to prompt. A rewrite that dodges these words while making the same claim is a review
# problem, but one that keeps them has to keep them true.
NOT_ALLOWED_PHRASES = ("not auto-allow", "not in this list", "still prompt")


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("_af_bootstrap_under_test", BOOTSTRAP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _inner_globs(patterns):
    out = []
    for pat in patterns:
        assert pat.startswith("Bash(") and pat.endswith(")"), pat
        out.append(pat[len("Bash("):-1])
    return out


def _prose_blocks(source: str) -> list[str]:
    """Every docstring, and every run of consecutive `#` comment lines, as one text each.

    A comment run is joined with the leading `#` stripped; a bare `#` line is a paragraph
    break, kept as a blank line so the sentence splitter ends a sentence there.
    """
    blocks: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=True)
            if doc:
                blocks.append(doc)
    run: list[str] = []
    last_line = None
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type != tokenize.COMMENT:
            continue
        line = tok.start[0]
        if last_line is not None and line != last_line + 1 and run:
            blocks.append("\n".join(run))
            run = []
        run.append(tok.string.lstrip("#").strip())
        last_line = line
    if run:
        blocks.append("\n".join(run))
    return blocks


_ABBREV_TAIL = re.compile(r"(?:\be\.g|\bi\.e|\betc|\bvs)$", re.IGNORECASE)


def _sentences(block: str) -> list[str]:
    """Split prose into sentences without ending one inside backticks or after `e.g.`.

    Lines are joined with spaces first, so a sentence that wraps across comment lines stays
    whole; a blank line is a hard break. A naive split on '. ' loses `git push --force` from the
    old `(e.g. `rm`, `git push --force`, ...)` comment, which is exactly the claim to catch.
    """
    out: list[str] = []
    for para in re.split(r"\n\s*\n", block):
        text = " ".join(line.strip() for line in para.splitlines()).strip()
        buf = ""
        in_tick = False
        for i, ch in enumerate(text):
            buf += ch
            if ch == "`":
                in_tick = not in_tick
                continue
            if in_tick or ch not in ".!?":
                continue
            nxt = text[i + 1] if i + 1 < len(text) else " "
            if not nxt.isspace():
                continue
            if _ABBREV_TAIL.search(buf[:-1]):
                continue
            out.append(buf.strip())
            buf = ""
        if buf.strip():
            out.append(buf.strip())
    return out


def _false_not_allowed_claims(source: str, globs: list[str]) -> list[tuple[str, str]]:
    bad = []
    for block in _prose_blocks(source):
        for sentence in _sentences(block):
            # Hyphens and whitespace runs fold to one space on both sides, so "not auto allowed"
            # and a claim wrapped across lines as "auto- allowed" are caught like
            # "not auto-allowed".
            low = re.sub(r"[\s\-]+", " ", sentence.lower())
            if not any(p.replace("-", " ") in low for p in NOT_ALLOWED_PHRASES):
                continue
            for cmd in re.findall(r"`([^`]+)`", sentence):
                if any(fnmatch.fnmatchcase(cmd, g) for g in globs):
                    bad.append((cmd, sentence))
    return bad


# --------------------------------------------------------------------------- #
# Honest text
# --------------------------------------------------------------------------- #
def test_no_still_prompts_claim_names_an_auto_allowed_command():
    """A sentence that says a command is left to prompt may not name one the list allows."""
    bootstrap = _load_bootstrap()
    globs = _inner_globs(bootstrap.BASH_AUTO_ALLOW_PATTERNS)
    bad = _false_not_allowed_claims(BOOTSTRAP.read_text(encoding="utf-8"), globs)
    assert not bad, "bootstrap.py claims these auto-allowed commands still prompt:\n" + "\n".join(
        f"  `{cmd}` in: {sentence}" for cmd, sentence in bad
    )


def test_the_git_glob_matches_destructive_git():
    """Pins what the corrected text admits. Whoever narrows the list later changes that text
    with it, because this test goes red the moment `Bash(git *)` stops covering these."""
    globs = _inner_globs(_load_bootstrap().BASH_AUTO_ALLOW_PATTERNS)
    assert "git *" in globs
    for cmd in ("git push --force origin main", "git reset --hard HEAD"):
        assert any(fnmatch.fnmatchcase(cmd, g) for g in globs), cmd


def test_the_docstring_admits_what_the_list_allows():
    """The honesty scanner only forbids false claims; this requires the true ones. One
    docstring sentence must name `Bash(git *)` with both destructive commands it covers, and
    one must name the arbitrary-code launchers."""
    doc = ast.get_docstring(ast.parse(BOOTSTRAP.read_text(encoding="utf-8")), clean=True)
    sentences = _sentences(doc)
    git = ("`Bash(git *)`", "`git push --force`", "`git reset --hard`")
    assert any(all(t in s for t in git) for s in sentences),         "the module docstring no longer admits that `Bash(git *)` matches destructive git"
    launchers = ("`python`", "`node`", "`xargs`", "`env`", "`find`")
    assert any(all(t in s for t in launchers) for s in sentences),         "the module docstring no longer names the entries that launch arbitrary code"


def test_the_sentence_splitter_keeps_backticks_and_e_g_whole():
    """The scanner is only as good as its splitter: the base's comment put `git push --force`
    after an `e.g.`, and a split on '. ' would have dropped it from the scanned sentence."""
    text = "Patterns NOT in this list (e.g. `rm`, `a. b`, `git push --force`)\nstill prompt. Next."
    assert _sentences(text) == [
        "Patterns NOT in this list (e.g. `rm`, `a. b`, `git push --force`) still prompt.",
        "Next.",
    ]


# --------------------------------------------------------------------------- #
# The opt-out
# --------------------------------------------------------------------------- #
def _opts(tmp_path, **kw):
    base = dict(
        dry_run=False,
        suite_root=REPO,
        config_home=tmp_path / "cfg",
        settings_file=tmp_path / "s.json",
        autodoc=False,
        autodoc_reason="test",
        claude_root=tmp_path / "claude",
    )
    base.update(kw)
    return provision.Options(**base)


def _step(steps, n):
    return next(s for s in steps if s.n == n)


def test_the_default_install_still_runs_step_8(tmp_path):
    opts = _opts(tmp_path)
    assert opts.auto_allow is True, "the default must stay ON"
    s8 = _step(provision.build_steps(opts), 8)
    assert s8.is_applicable(opts)
    assert s8.title == "bootstrap.py (once): install PreToolUse auto-allow hooks"
    assert s8.detail == [
        f"python {REPO / provision.BOOTSTRAP_REL}",
        f"honors CLAUDE_SETTINGS_FILE ({tmp_path / 's.json'})",
    ]
    assert s8.run is provision._do_bootstrap


def test_no_auto_allow_skips_step_8_and_says_it_removes_nothing(tmp_path):
    opts = _opts(tmp_path, auto_allow=False)
    s8 = _step(provision.build_steps(opts), 8)
    assert not s8.is_applicable(opts)
    note = s8.skip_reason(opts)
    assert "--no-auto-allow" in note
    assert "removes nothing" in note
    assert "docs/INSTALL.md section 13" in note


def _bootstrap_runner(tmp_settings: Path):
    """Stand-in for `_run_cmd`: the tmp suite has no venv, so run bootstrap with the test's
    own interpreter. Same script, same env (CLAUDE_SETTINGS_FILE -> the tmp file).

    The guard is here, not in provision: if `_do_bootstrap` ever stops setting
    CLAUDE_SETTINGS_FILE, bootstrap falls back to the runner's real settings file, and this
    test must fail rather than write 47 hooks into it. The message names only that one
    variable; the env also carries the runner's tokens."""
    import subprocess

    def run(cmd, *, env=None, cwd=None):
        got = (env or {}).get("CLAUDE_SETTINGS_FILE")
        assert got == str(tmp_settings), f"CLAUDE_SETTINGS_FILE={got!r}, not the tmp file"
        subprocess.run([sys.executable, *cmd[1:]], env=env, cwd=cwd, check=True,
                       stdout=subprocess.DEVNULL)
    return run


@pytest.mark.parametrize("auto_allow", [True, False])
def test_an_opted_out_run_writes_no_pretooluse_but_still_the_sessionstart_hook(
        tmp_path, monkeypatch, auto_allow):
    """Steps 8 and 9 through `execute` over `{}`. The True leg is the control: it proves the
    same harness DOES write PreToolUse when step 8 runs, so the False leg's absence means
    something."""
    monkeypatch.delenv("AGENTFLOW_EXTRA_AUTO_ALLOW", raising=False)
    # Belt and braces: any fallback to Path.home() lands in tmp, never the runner's home.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    settings = tmp_path / "s.json"
    settings.write_text("{}", encoding="utf-8")
    opts = _opts(tmp_path, auto_allow=auto_allow)
    with mock.patch.object(provision, "_run_cmd", _bootstrap_runner(settings)):
        steps = [s for s in provision.build_steps(opts) if s.n in (8, 9)]
        out = io.StringIO()
        with redirect_stdout(out):
            provision.execute(opts, steps)
    hooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    assert hooks.get("SessionStart"), "step 9 must run on both paths"
    if auto_allow:
        assert hooks.get("PreToolUse"), "control: step 8 did not write, so the test proves nothing"
    else:
        assert "PreToolUse" not in hooks
        assert "8. bootstrap.py (once): install PreToolUse auto-allow hooks - SKIPPED (" \
               "--no-auto-allow" in out.getvalue()


def _dry_run(tmp_path, *extra):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = provision.main(["--dry-run", *extra,
                             "--config-home", str(tmp_path / "cfg"),
                             "--settings-file", str(tmp_path / "s.json"),
                             "--claude-root", str(tmp_path / "claude")])
    assert rc == 0
    return out.getvalue()


def _step_8_block(text):
    lines = text.splitlines()
    i = next(k for k, ln in enumerate(lines) if ln.startswith("  8. "))
    j = next(k for k, ln in enumerate(lines) if ln.startswith("  9. "))
    return lines[i:j]


def test_the_dry_run_plan_marks_step_8_skipped(tmp_path):
    off = _dry_run(tmp_path, "--no-auto-allow")
    block = _step_8_block(off)
    assert block[0].endswith("[SKIPPED]"), block
    assert "--no-auto-allow" in block[1] and "section 13" in block[1], block
    assert "auto-allow  : disabled (--no-auto-allow)" in off

    on = _dry_run(tmp_path)
    block = _step_8_block(on)
    assert not block[0].endswith("[SKIPPED]"), block
    assert any("bootstrap.py" in ln for ln in block[1:]), block
    assert "auto-allow  : enabled" in on


def test_the_flag_survives_the_venv_handover(tmp_path, monkeypatch):
    """Modelled on test_provision_venv_handover's argv test. `Options.invoked_argv` already
    carries every flag to the child; this pins it for `--no-auto-allow`, whose loss would install the
    very hooks the operator declined, in the child, after the parent's plan said it would not."""
    monkeypatch.setattr(sys, "argv", ["provision.py"])
    monkeypatch.delenv(provision._REEXEC_MARKER, raising=False)
    venv_py = tmp_path / ".venv" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("", encoding="utf-8")

    calls = []

    def step(n):
        def _run(_opts):
            if n == 4:
                provision._reexec_into_venv(_opts, resume_from=5)
        return _run

    steps = [provision.Step(n=n, title=f"step {n}", run=step(n)) for n in range(1, 9)]
    passed = ["--no-auto-allow", "--suite-root", str(tmp_path),
              "--config-home", str(tmp_path / "cfg"),
              "--settings-file", str(tmp_path / "s.json"), "--claude-root", str(tmp_path / "cl")]
    with mock.patch.object(provision, "build_steps", lambda _o: steps), \
         mock.patch.object(provision, "_venv_python", lambda _o: venv_py), \
         mock.patch.object(provision.subprocess, "call",
                           lambda argv, **k: calls.append(argv) or 0):
        rc = provision.main(list(passed))

    assert rc == 0
    child = calls[0]
    assert "--no-auto-allow" in child, "the child would install the hooks the operator declined"

    # And the child, parsing that argv, really does skip step 8.
    child_args = child[2:]
    seen = {}
    real_build = provision.build_steps

    def spy(opts):
        seen["auto_allow"] = opts.auto_allow
        return real_build(opts)

    with mock.patch.object(provision, "build_steps", spy), redirect_stdout(io.StringIO()):
        assert provision.main([*child_args, "--dry-run"]) == 0
    assert seen["auto_allow"] is False


# --------------------------------------------------------------------------- #
# The settings write
# --------------------------------------------------------------------------- #
def _run_bootstrap_main(monkeypatch, settings: Path) -> int:
    """bootstrap.main() in-process against *settings*, with the env patterns cleared."""
    monkeypatch.delenv("AGENTFLOW_EXTRA_AUTO_ALLOW", raising=False)
    monkeypatch.setenv("CLAUDE_SETTINGS_FILE", str(settings))
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    with redirect_stdout(io.StringIO()):
        return _load_bootstrap().main()


def _shared_atomic_io():
    """The atomic_io module bootstrap imports: the installed agentflow package, imported the
    same way, so a patch here is the one bootstrap's lazy import picks up."""
    from agentflow import atomic_io
    return atomic_io


def test_bootstrap_without_the_package_refuses_loudly_and_writes_nothing(tmp_path, monkeypatch):
    # A None entry in sys.modules makes the import raise ModuleNotFoundError for that name,
    # which is what a bare interpreter without the package raises.
    monkeypatch.setitem(sys.modules, "agentflow.atomic_io", None)
    settings = tmp_path / "s.json"
    settings.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        _run_bootstrap_main(monkeypatch, settings)
    msg = str(caught.value.code)
    assert "agentflow package is not importable" in msg and "Nothing was written" in msg, msg
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)
    assert settings.read_text(encoding="utf-8") == "{}"


def test_bootstrap_with_a_package_too_old_for_it_refuses_loudly_and_writes_nothing(tmp_path,
                                                                                   monkeypatch):
    """An installed agentflow that predates the shared settings writer: the module imports
    but the name does not, which raises a plain ImportError rather than ModuleNotFoundError.
    It must still end in the remediation, not a traceback, with the file untouched."""
    monkeypatch.delattr(_shared_atomic_io(), "write_settings_json")
    settings = tmp_path / "s.json"
    settings.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        _run_bootstrap_main(monkeypatch, settings)
    msg = str(caught.value.code)
    assert "older than this script" in msg and "Nothing was written" in msg, msg
    cause = caught.value.__cause__
    assert isinstance(cause, ImportError) and not isinstance(cause, ModuleNotFoundError)
    assert settings.read_text(encoding="utf-8") == "{}"


def test_bootstrap_keeps_non_ascii_settings_unescaped(tmp_path, monkeypatch):
    settings = tmp_path / "s.json"
    settings.write_text(json.dumps({"env": {"NOTE": "Zo\u00eb \u2713"}}, ensure_ascii=False),
                        encoding="utf-8")
    assert _run_bootstrap_main(monkeypatch, settings) == 0
    text = settings.read_bytes().decode("utf-8")
    assert "Zo\u00eb \u2713" in text, "the operator's non-ASCII value must survive as UTF-8"
    assert "\\u00eb" not in text and "\\u2713" not in text, "rewritten as escapes"
    after = json.loads(text)
    assert after["env"]["NOTE"] == "Zo\u00eb \u2713"
    assert after["hooks"]["PreToolUse"], "control: the hooks were actually written"
    assert b"\r\n" not in settings.read_bytes(), "provision writes LF; CRLF here flip-flops"


@pytest.mark.parametrize("raw_json", [
    '{"env": {"NOTE": "Zoë ✓"}}',
    # A lone surrogate parses as JSON but cannot be written as UTF-8.
    '{"env": {"NOTE": "\\ud800"}}',
], ids=["non-ascii", "lone-surrogate"])
def test_bootstrap_writes_what_provision_writes(tmp_path, monkeypatch, raw_json):
    """Byte-for-byte what provision's own settings write produces for the same settings, so
    the two writers of this one file never flip-flop it. Provision's write is driven for real
    (its step 9 over a settings file), never re-spelled here."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    by_provision = tmp_path / "provision.json"
    by_provision.write_bytes(raw_json.encode("utf-8"))
    with redirect_stdout(io.StringIO()):
        provision._do_sessionstart_hook(_opts(tmp_path, settings_file=by_provision))
    written = by_provision.read_bytes()
    assert written != raw_json.encode("utf-8"), "control: provision did not rewrite the file"

    by_bootstrap = tmp_path / "bootstrap.json"
    _load_bootstrap().write_settings(by_bootstrap, json.loads(written.decode("utf-8")))
    assert by_bootstrap.read_bytes() == written


def test_bootstrap_writes_a_lone_surrogate_losslessly(tmp_path):
    settings = tmp_path / "s.json"
    _load_bootstrap().write_settings(settings, {"env": {"NOTE": "\ud800"}})
    raw = settings.read_bytes()
    assert b"\\ud800" in raw, "the escaped form is the only lossless spelling"
    assert json.loads(raw.decode("utf-8"))["env"]["NOTE"] == "\ud800"


def test_bootstrap_writes_through_a_symlink_to_its_target(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"this platform cannot create a symlink here: {e}")
    _load_bootstrap().write_settings(link, {"env": {"NOTE": "Zoë"}})
    assert link.is_symlink(), "the link was replaced by a regular file"
    assert json.loads(target.read_text(encoding="utf-8")) == {"env": {"NOTE": "Zoë"}}
    assert sorted(x.name for x in tmp_path.iterdir()) == ["link.json", "real.json"]


def test_bootstrap_leaves_an_up_to_date_file_untouched(tmp_path, monkeypatch):
    settings = tmp_path / "s.json"
    settings.write_text("{}", encoding="utf-8")
    assert _run_bootstrap_main(monkeypatch, settings) == 0
    # Spelled differently from bootstrap's own output, so a needless rewrite changes bytes.
    settings.write_text(json.dumps(json.loads(settings.read_text(encoding="utf-8")), indent=4),
                        encoding="utf-8")
    before = settings.read_bytes()
    past = 1_000_000_000
    os.utime(settings, (past, past))
    assert _run_bootstrap_main(monkeypatch, settings) == 0
    assert settings.read_bytes() == before, "nothing to add, yet the file was rewritten"
    assert os.stat(settings).st_mtime == past, "nothing to add, yet the file was touched"


def test_bootstrap_writes_through_the_shared_atomic_helper(tmp_path, monkeypatch):
    atomic_io = _shared_atomic_io()
    calls = []
    real = atomic_io.atomic_write

    def spy(path, text, *a, **kw):
        calls.append((Path(path), text))
        return real(path, text, *a, **kw)

    monkeypatch.setattr(atomic_io, "atomic_write", spy)
    settings = tmp_path / "s.json"
    settings.write_text("{}", encoding="utf-8")
    assert _run_bootstrap_main(monkeypatch, settings) == 0
    assert len(calls) == 1, f"expected one atomic write, got {len(calls)}"
    assert calls[0][0] == settings.resolve()
    assert settings.read_bytes() == calls[0][1].encode("utf-8")


def test_a_failed_bootstrap_write_leaves_the_original_and_no_temp(tmp_path, monkeypatch):
    atomic_io = _shared_atomic_io()

    def refuse(a, b):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(atomic_io.os, "replace", refuse)
    settings = tmp_path / "s.json"
    original = json.dumps({"env": {"KEEP": "me"}}).encode("utf-8")
    settings.write_bytes(original)
    with pytest.raises(OSError):
        _run_bootstrap_main(monkeypatch, settings)
    assert settings.read_bytes() == original
    assert sorted(x.name for x in tmp_path.iterdir()) == ["s.json"], "a temp was left behind"
