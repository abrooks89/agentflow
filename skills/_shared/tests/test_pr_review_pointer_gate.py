"""The `pr-provenance-gate` job runs the SAME shell CI runs, against synthetic PR bodies.

gh#312: a lane's review evidence died with the lane. The registry answer (Q-10063, option C)
is one line in the PR BODY - `Review: <url>` or `Review: none` - enforced by the job that already
hard-fails a body with no provenance. This file pins that the gate does what the answer says.

WHAT IT CAN PROVE
* The gate's own `run:` block, parsed out of `.github/workflows/ci.yml` rather than restated
  here, is executed under bash with PR_TITLE / PR_BODY / REPO set the way the workflow sets
  them. A copy of the script in this file would drift from the one CI runs; the parse is what
  keeps the two identical.
* Provenance and the review pointer are INDEPENDENT checks: each fails alone, both messages
  appear when both fail, and a body satisfying one does not satisfy the other.
* The pointer is body-only, accepts exactly a URL or `none` (bold-wrapped or not), and refuses
  the template's unfilled placeholder, an empty value, and any other word.
* The three files a contributor reads (template, PR-PROSE.md, repo CLAUDE.md) all carry the
  line, so the rule's home is on the reader's path and not only in the gate.

WHAT IT CANNOT PROVE
* That GitHub delivers the body on `edited`. That is the trigger gh#331 wired and pinned elsewhere; here
  the body is an env var, exactly as the workflow hands it to the step.
* Anything about what the URL points at. The answer declares that out of scope; so does the gate.
* The `gh api` issue/PR lookup against a live host. `gh` is a shim on PATH here: issue 12 is an
  issue, 99 is a pull request, anything else is missing.

BASH: the gate is a bash script, so the tests that RUN it need bash. Git for Windows ships one
and the ubuntu runner has one; the WSL stub at `System32\\bash.exe` is NOT one (it fails without
a distro), so bash is derived from where `git` lives before any bare PATH lookup - the same
derivation `test_documented_shell_parses.py` uses. With no usable bash each gate-running test
SKIPS, loudly (the file-content tests at the end still run); the ubuntu leg of CI still runs
them all, so a skip here is a gap on one leg and not a green.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PR_TEMPLATE = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
PR_PROSE = REPO_ROOT / "skills" / "_shared" / "PR-PROSE.md"
REPO_CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

JOB = "pr-provenance-gate"

def _bash() -> str:
    """A bash that can run the gate: derived from `git` first, bare PATH last.

    On Windows a bare `which("bash")` finds the WSL launcher before Git's shell, so the Git
    install's own `bin/bash.exe` / `usr/bin/bash.exe` are tried first.
    """
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
        pytest.skip("no usable bash found (Git for Windows ships one); the ubuntu CI leg runs this test")
    return candidates[0]


def _gate_script() -> str:
    """The gate's `run:` block, read out of the workflow CI executes - never a copy."""
    wf = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    job = wf["jobs"][JOB]
    steps = job["steps"]
    assert len(steps) == 1, f"{JOB} grew a second step; this file drives step 0 only"
    return steps[0]["run"]


def _write_gh_shim(directory: Path) -> None:
    """A `gh` that answers `gh api repos/<r>/issues/<n> --jq ...` the way the gate reads it.

    12 is an issue, 99 is a pull request, anything else is a 404 (exit 1, JSON on stdout -
    the shape the gate's comment says it must not fold into its message).

    `pulls/<n>/commits` answers with the lines the gate's `--jq` would print (`<sha7><TAB><line>`
    per commit-message line), read from the file `$GH_SHIM_COMMITS` names; none by default. With
    `GH_SHIM_COMMITS_FAIL=1` that read exits 1, as a failed API call does.
    """
    shim = directory / "gh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        '  *"/pulls/"*"/commits"*)\n'
        # The canned lines stand in for the gate's real filter, so the shim refuses any call
        # that does not carry that exact filter and --paginate: a typo there cannot pass.
        '    prev=""; jq=""; pag=0\n'
        '    for a in "$@"; do\n'
        '      if [ "$prev" = "--jq" ]; then jq="$a"; fi\n'
        '      if [ "$a" = "--paginate" ]; then pag=1; fi\n'
        '      prev="$a"\n'
        "    done\n"
        '    if [ "$pag" != 1 ] || [ "$jq" != "$GH_SHIM_EXPECT_JQ" ]; then\n'
        '      echo "gh shim: commits call without --paginate or with another --jq: $jq" >&2; exit 2\n'
        "    fi\n"
        '    if [ "${GH_SHIM_COMMITS_FAIL:-0}" = 1 ]; then echo \'{"message":"boom"}\'; exit 1; fi\n'
        '    if [ -n "${GH_SHIM_COMMITS:-}" ]; then cat "$GH_SHIM_COMMITS"; fi\n'
        "    exit 0 ;;\n"
        '  *"/issues/12 "*) echo issue; exit 0 ;;\n'
        '  *"/issues/99 "*) echo pr; exit 0 ;;\n'
        '  *) echo \'{"message":"Not Found"}\'; exit 1 ;;\n'
        "esac\n",
        encoding="utf-8",
        newline="\n",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


#: The gate's filter over `pulls/<n>/commits`: one `<sha7><TAB><line>` per commit-message line.
#: The shim demands exactly this string, and a test below demands it of the workflow.
COMMITS_JQ = '.[] | .sha[0:7] as $s | (.commit.message | split("\\n")[]) | $s + "\\t" + .'


#: The harness's `REPO`, and a fork of it: the default head repo, so every case that predates the
#: closing-keyword check (bodies with `Closes #12` and no attribution line) reads as an outside
#: contributor's PR, which that check leaves alone.
BASE_REPO = "someone/somerepo"
FORK_REPO = "contributor/somerepo"


def _run_gate(
    tmp_path: Path,
    *,
    title: str = "a title",
    body: str,
    head_repo: str = FORK_REPO,
    commits: tuple[tuple[str, str], ...] = (),
    commits_fail: bool = False,
) -> subprocess.CompletedProcess:
    script = tmp_path / "gate.sh"
    script.write_text(_gate_script(), encoding="utf-8", newline="\n")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(exist_ok=True)
    _write_gh_shim(shim_dir)
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    commits_file = tmp_path / "commits.txt"
    commits_file.write_text(
        "".join(f"{sha}\t{line}\n" for sha, message in commits for line in message.splitlines()),
        encoding="utf-8",
        newline="\n",
    )
    env.update(
        {
            "GH_TOKEN": "shim",
            "PR_TITLE": title,
            "PR_BODY": body,
            "PR_NUMBER": "7",
            "REPO": BASE_REPO,
            "HEAD_REPO": head_repo,
            "GH_SHIM_COMMITS": str(commits_file),
            "GH_SHIM_COMMITS_FAIL": "1" if commits_fail else "0",
            "GH_SHIM_EXPECT_JQ": COMMITS_JQ,
        }
    )
    return subprocess.run(
        [_bash(), str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


PROVENANCE_OK = "Source: trivial"
REVIEW_OK = "Review: none"
REVIEW_URL = "Review: https://github.com/someone/somerepo/pull/7#issuecomment-123456"


# ---------------------------------------------------------------------------
# Both checks pass
# ---------------------------------------------------------------------------


def test_escape_plus_none_passes(tmp_path):
    r = _run_gate(tmp_path, body=f"Agentflow [lead-dev]\n\n{PROVENANCE_OK}\n{REVIEW_OK}\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "provenance: declared escape present" in r.stdout
    assert "review: pointer present" in r.stdout


def test_issue_plus_url_passes(tmp_path):
    r = _run_gate(tmp_path, body=f"Closes #12\n\n{REVIEW_URL}\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "provenance: issue #12 cited" in r.stdout
    assert "review: pointer present" in r.stdout


def test_epic_id_plus_bold_review_passes(tmp_path):
    body = "**Source:** Epic: E-0763\n\n**Review:** none\n"
    r = _run_gate(tmp_path, body=body)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "provenance: epic id cited" in r.stdout


def test_review_value_is_case_insensitive_and_tolerates_crlf(tmp_path):
    r = _run_gate(tmp_path, body="Source: trivial\r\nReview: NONE\r\n")
    assert r.returncode == 0, r.stdout + r.stderr


# ---------------------------------------------------------------------------
# The two checks are independent: each fails alone, with its own message
# ---------------------------------------------------------------------------


def test_provenance_without_review_fails_and_names_the_review(tmp_path):
    r = _run_gate(tmp_path, body="Closes #12\n\nno pointer here\n")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "provenance: issue #12 cited" in r.stdout, "provenance still reported as satisfied"
    assert "no 'Review:' pointer" in r.stdout
    assert "cites no issue, no epic" not in r.stdout, "the provenance error fired on a body that has provenance"


def test_review_without_provenance_fails_and_names_the_provenance(tmp_path):
    r = _run_gate(tmp_path, body=f"{REVIEW_URL}\n")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "review: pointer present" in r.stdout, "the pointer was there and was not reported"
    assert "cites no issue, no epic, and no declared escape" in r.stdout
    assert "no 'Review:' pointer" not in r.stdout


def test_neither_reports_both_in_one_run(tmp_path):
    r = _run_gate(tmp_path, body="just prose\n")
    assert r.returncode == 1
    assert "cites no issue, no epic, and no declared escape" in r.stdout
    assert "no 'Review:' pointer" in r.stdout


def test_two_pointers_fail_as_two_not_as_none(tmp_path):
    """Exactly one: two pointers give an inheriting seat two answers, and the message says
    which failure it is rather than reporting a body with two lines as having none."""
    r = _run_gate(tmp_path, body=f"{PROVENANCE_OK}\n{REVIEW_OK}\n{REVIEW_URL}\n")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "carries 2 'Review:' pointers; exactly one" in r.stdout
    assert "no 'Review:' pointer" not in r.stdout


# ---------------------------------------------------------------------------
# The pointer's shape: URL or `none`, body only, nothing else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Review:",
        "Review: ",
        "Review: pending",
        "Review: see comments",
        "Review: #issuecomment-1",
        "Review: none <!-- filled in later -->",
        "**Review:** <!-- required, gh#312: the comment on THIS PR carrying its review verdicts, or `none` -->",
        "Reviewed: none",
        "Review none",
    ],
)
def test_wrong_shapes_fail(tmp_path, line):
    r = _run_gate(tmp_path, body=f"{PROVENANCE_OK}\n{line}\n")
    assert r.returncode == 1, f"{line!r} passed:\n{r.stdout}{r.stderr}"
    assert "no 'Review:' pointer" in r.stdout


def test_pointer_in_the_title_does_not_count(tmp_path):
    r = _run_gate(tmp_path, title="Review: none", body=f"{PROVENANCE_OK}\n")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "no 'Review:' pointer" in r.stdout


def test_pr_number_is_still_not_provenance_even_with_a_pointer(tmp_path):
    r = _run_gate(tmp_path, body=f"Closes #99\n{REVIEW_OK}\n")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "#99 is a pr, not an issue" in r.stdout
    assert "cites no issue, no epic, and no declared escape" in r.stdout


def test_provenance_failure_text_suggests_no_real_example_ids(tmp_path):
    """The failure message is what an outside contributor reads first (gh#553). It used to
    suggest `Closes #123` and `Epic: E-0212`: a real registry id means nothing to them, and a
    literal `#N` copied into a PR body becomes a live citation once that number exists on the
    host. The suggestions are placeholders, like the PR template's."""
    r = _run_gate(tmp_path, body=f"no citation here\n{REVIEW_OK}\n")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "cites no issue, no epic, and no declared escape" in r.stdout
    # The body carries REVIEW_OK, so "review: pointer present" ends the advice block; without it
    # the slice would run on into the review error, which cites its own issue number.
    advice = r.stdout.split("cites no issue", 1)[1].split("review:", 1)[0]
    assert not re.search(r"#[0-9]+|\bE-[0-9]{4}\b", advice), advice
    assert "Closes #<n>" in r.stdout


def test_unfilled_template_fails_on_the_review_half(tmp_path):
    """The shipped template, submitted untouched, must not pass - and it now fails on BOTH
    halves. It used to pass provenance through a commented example epic id, because the gate's
    epic-id and `#N` checks are unanchored and read HTML comments too. The examples became
    placeholders (`Epic: <id-or-slug>`, `Closes #<n>`), so an untouched template cites nothing
    and the contributor reads both messages from one run."""
    template_text = PR_TEMPLATE.read_text(encoding="utf-8")
    # The gate greps `#[0-9]+` and `E-[0-9]{4}` across the whole body, comments included, so a
    # leftover example number would pass provenance as soon as that number is an issue (or a
    # real epic id) on the host - silently, for every PR that leaves the template unfilled.
    assert not re.search(r"#[0-9]+|\bE-[0-9]{4}\b", template_text)
    r = _run_gate(tmp_path, body=template_text)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "cites no issue, no epic, and no declared escape" in r.stdout
    assert "provenance:" not in r.stdout, "the untouched template satisfied provenance"
    assert "no 'Review:' pointer" in r.stdout


@pytest.mark.parametrize("how", ["uncomment", "replace"])
def test_template_filled_the_documented_way_passes(tmp_path, how):
    """The template's own fill instructions must lead to a body the gate accepts. The Review
    regex is anchored at end of line, so an example line carrying trailing explanation, or a
    `**Review:** none` typed in front of the placeholder comment, fails with "no pointer" - the
    contributor did what the template said and CI told them they did nothing."""
    lines = PR_TEMPLATE.read_text(encoding="utf-8").splitlines()
    out, skip = [], False
    for ln in lines:
        if skip:
            skip = not ln.rstrip().endswith("-->")
            continue
        if ln.startswith("<!-- Source: trivial"):
            out.append(ln.removeprefix("<!-- ").removesuffix(" -->"))
        elif how == "uncomment" and ln.startswith("<!-- Review: none"):
            out.append(ln.removeprefix("<!-- ").removesuffix(" -->"))
        elif ln.startswith("**Review:**"):
            if how == "replace":
                out.append("**Review:** none")
            skip = not ln.rstrip().endswith("-->")
        else:
            out.append(ln)
    r = _run_gate(tmp_path, body="\n".join(out) + "\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "review: pointer present" in r.stdout


def test_template_review_placeholder_cites_no_issue_number():
    """The Review placeholder must not write `#312` into every PR body: the gate greps
    `#[0-9]+` across comments, and 312 is an open issue, so the unfilled placeholder would
    itself be a valid provenance citation."""
    text = PR_TEMPLATE.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("**Review:**"))
    assert not re.search(r"#[0-9]+", line), line


# ---------------------------------------------------------------------------
# The rule's home is on the reader's path, not only inside the gate
# ---------------------------------------------------------------------------


def test_template_carries_the_review_line():
    text = PR_TEMPLATE.read_text(encoding="utf-8")
    assert "**Review:**" in text
    assert "Review: none" in text
    assert "skills/_shared/PR-PROSE.md" in text


def test_pr_prose_states_the_rule_once():
    text = PR_PROSE.read_text(encoding="utf-8")
    assert "## Review pointer" in text
    assert "Review: none" in text
    assert "hard-fails a body carrying neither" in text


def test_repo_claude_md_names_the_enforced_rule():
    text = REPO_CLAUDE_MD.read_text(encoding="utf-8")
    assert "Review: none" in text
    assert "gh#312" in text


def test_gate_step_name_says_it_checks_the_review():
    wf = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    name = wf["jobs"][JOB]["steps"][0]["name"]
    assert "review" in name.lower(), name


# ---------------------------------------------------------------------------
# C. No closing keyword on a same-repo PR: title, body and commit messages
# ---------------------------------------------------------------------------

FLEET = "Agentflow [lead-dev]"
KEYWORD_ERR = "carries a closing keyword before an issue reference"


def _fleet_body(line: str) -> str:
    return f"{FLEET}\n\n{line}\n{REVIEW_OK}\n"


def test_same_repo_pr_without_attribution_line_fails_on_closes(tmp_path):
    """A fleet PR that left the attribution line out is still told apart: its head is in this
    repo. This is the case the attribution line alone would have let through."""
    r = _run_gate(tmp_path, body=f"Closes #12\n{REVIEW_OK}\n", head_repo=BASE_REPO)
    assert r.returncode == 1, r.stdout + r.stderr
    assert KEYWORD_ERR in r.stdout
    assert "the PR BODY" in r.stdout


@pytest.mark.parametrize(
    "line",
    [
        "**Source:** Closes #12",
        "Source: gh#12. It also fixes #12 on the way.",
        "Fixes: #12",
        "This PR does not close #12.",
    ],
)
def test_fleet_body_with_a_closing_keyword_fails(tmp_path, line):
    r = _run_gate(tmp_path, body=_fleet_body(line))
    assert r.returncode == 1, f"{line!r} passed:\n{r.stdout}{r.stderr}"
    assert KEYWORD_ERR in r.stdout
    assert "Source: gh#<n>" in r.stdout
    assert "gh pr edit" in r.stdout


@pytest.mark.parametrize("line", ["Source: gh#12", "Closes gh#12"])
def test_fleet_body_citing_a_gh_ref_passes(tmp_path, line):
    """GitHub does not close on `gh#N`, so a keyword before it closes nothing."""
    r = _run_gate(tmp_path, body=_fleet_body(line))
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_keyword_check_says_whether_it_ran(tmp_path):
    """A check that is silent when it passes cannot be told from one that never ran."""
    ran = _run_gate(tmp_path, body=_fleet_body("Source: gh#12"))
    assert "closing keywords: none in the title, body or commit messages" in ran.stdout
    skipped = _run_gate(tmp_path, body=f"Closes #12\n{REVIEW_OK}\n")
    assert "closing keywords: not checked" in skipped.stdout


def test_fleet_commit_message_with_a_closing_keyword_fails_and_names_the_reword(tmp_path):
    r = _run_gate(
        tmp_path,
        body=_fleet_body("Source: gh#12"),
        commits=(("abc1234", "Add the widget\n\nCloses #12"),),
    )
    assert r.returncode == 1, r.stdout + r.stderr
    assert "COMMIT MESSAGE" in r.stdout
    assert "abc1234" in r.stdout
    assert "reword" in r.stdout
    assert "--force-with-lease" in r.stdout


def test_clean_fleet_commit_messages_pass(tmp_path):
    r = _run_gate(
        tmp_path,
        body=_fleet_body("Source: gh#12"),
        commits=(("abc1234", "Add the widget\n\nSource: gh#12"),),
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_gate_reads_commits_with_the_filter_the_shim_demands():
    """The shim answers with pre-formatted lines; this keeps the workflow's real filter and the
    shim's expectation one string, so editing either alone fails."""
    script = _gate_script()
    assert f"--paginate --jq '{COMMITS_JQ}'" in script


def test_quoted_pr_text_cannot_issue_a_workflow_command(tmp_path):
    """The runner trims leading whitespace before it looks for `::`, so an indented echo of a
    hit line would still run it as a command. Every quoted hit line starts with `>`."""
    injected = "::warning::closes #12 injected"
    r = _run_gate(
        tmp_path,
        title="::stop-commands::x fixes #12",
        body=_fleet_body(injected),
        commits=(("abc1234", "x\n\n::error::Closes #12"),),
    )
    assert r.returncode == 1, r.stdout + r.stderr
    markers = ("::warning::", "::stop-commands::", "::error::Closes")
    quoted = [ln for ln in r.stdout.splitlines() if any(m in ln for m in markers)]
    assert len(quoted) == 3, r.stdout
    for ln in quoted:
        assert not ln.lstrip().startswith("::"), ln
        assert ln.startswith("  > "), ln


def test_failed_commit_read_fails_the_check(tmp_path):
    r = _run_gate(tmp_path, body=_fleet_body("Source: gh#12"), commits_fail=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "could not read the PR's commit messages" in r.stdout


def test_fleet_title_with_a_closing_keyword_fails(tmp_path):
    r = _run_gate(tmp_path, title="Widget fix (fixes #12)", body=_fleet_body("Source: gh#12"))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "the PR TITLE" in r.stdout


def test_fleet_title_citing_a_gh_ref_passes(tmp_path):
    r = _run_gate(tmp_path, title="Widget fix (gh#12)", body=_fleet_body("Source: gh#12"))
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("head_repo", [FORK_REPO, ""])
def test_fork_or_null_head_repo_keeps_closes(tmp_path, head_repo):
    """An outside contributor's PR comes from a fork (or from a deleted one, whose head repo
    renders empty) and keeps `Closes #N` as its provenance."""
    r = _run_gate(tmp_path, body=f"Closes #12\n{REVIEW_OK}\n", head_repo=head_repo)
    assert r.returncode == 0, r.stdout + r.stderr


def test_provenance_failure_text_offers_source_for_loops(tmp_path):
    r = _run_gate(tmp_path, body=f"no citation here\n{REVIEW_OK}\n")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Source: gh#<n>" in r.stdout
    assert "outside contributors" in r.stdout


def test_template_offers_a_source_line_and_marks_closes_for_forks():
    text = PR_TEMPLATE.read_text(encoding="utf-8")
    assert "<!-- Source: gh#<n>" in text
    closes = next(ln for ln in text.splitlines() if ln.startswith("<!-- Closes #<n>"))
    assert "outside contributors" in closes
