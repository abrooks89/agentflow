"""Every fenced shell block in first-party Markdown must PARSE (E-0743).

THE DEFECT THIS EXISTS FOR. `skills/autodoc-update/SKILL.md` Step 1 wrapped a Python program in
`eval "$(python -c ' ... ')"`. Five explanatory comments inside that Python contain an apostrophe,
and inside a single-quoted `-c` string every apostrophe CLOSES the quote: the block held 11 single
quotes, an odd number, so bash could not parse it at all -- `syntax error near unexpected token
('`. Nothing in that block ran. Every later step of the runbook reads `$AUTODOC` / `$SUITE_ROOT` /
`$PROJECT` out of it, so the ENTIRE documented runbook was unrunnable, and had been for its whole
life. No reader ever reported it, which is the point: a runbook is prose until something parses
it, and nothing did.

THE PREDICATE. A fenced shell block in first-party Markdown must exit 0 under `bash -n` (parse
only; nothing is executed) AND SAY NOTHING ON STDERR, UNLESS neutralising its angle-bracket
placeholders makes it do both -- in which case the placeholder was the whole cause and the block is
a template, not a defect.

WHY STDERR IS PART OF THE PREDICATE, and it is the same hole in a second place. bash scores a
heredoc that runs off the end of input as a WARNING and still exits 0. That is the failure mode of
`<<'PY'`, the construct the fix above adopts, so a gate keyed on the exit code alone is blind
exactly where this epic now lives. MEASURED: two live blocks in `skills/project/SKILL.md` were
`python - <<'PYEOF'` indented inside a Markdown list item, terminator therefore not at column 0
(`<<-` strips tabs, not spaces). Executed as written the first exits 1 on an IndentationError and
the step branching on its output gets neither answer -- the E-0743 defect class, in the corpus,
under a green gate. Both are fixed in the same commit that closed this hole.

WHAT COUNTS AS A FENCE. Any run of 3+ backticks or 3+ tildes whose infostring's FIRST WORD names a
shell. Matching only three backticks with an exact infostring was an evasion, not a simplification:
restoring the verbatim pre-fix defect and changing ONLY its fence to ````bash -- not one byte of
the broken shell -- took this suite from red to green, and MIN_BLOCKS could not see it because the
count merely fell 62 -> 61. A closer must match the opener's character and length and carry no
infostring, or a block that WRITES a fenced file ends at the fence it is writing and its tail is
never scanned.

WHY THE EXEMPTION IS CAUSAL AND NOT "CONTAINS A PLACEHOLDER", which is the trap this gate nearly
shipped with. Bash reads `<name>` as a redirection, so a documentation template like
`git -C <canonical_repo.local_checkout> fetch publish` cannot parse and never will; eight such
blocks are live in this repo and none is a bug. But the broken autodoc block ALSO carried
`<data_root>` and `<name>` -- inside Python comments, incidental to its failure. A "block contains
a placeholder, skip it" rule therefore exempts the one real defect in the corpus and reports green
on the commit that introduced it: a 100%-false-green gate that still looks like it works. Measured
both ways while writing this. So the exemption substitutes the placeholders out and RE-PARSES: if
the block still fails, the placeholder was not the cause and the block is a defect.

MEASURED ON THIS COMMIT: 62 blocks scanned, 8 placeholder-caused (exempt), 0 defects. Against the
pre-fix `skills/autodoc-update/SKILL.md`: the same 62 and the same 8, plus that file RED. Against
the pre-fix `skills/project/SKILL.md`: the same counts plus its two heredoc blocks RED. These three
numbers are re-derived by the gate on every run, so a stale one here cannot make the suite pass --
but it can mislead a reader, which is why they are dated to the commit rather than left bare.

THE HARNESS TRAP, WHICH IS THE REAL RISK HERE. On Windows `shutil.which("bash")` resolves to
`C:\\Windows\\system32\\bash.exe` -- the WSL launcher -- ahead of Git Bash, because
`system32` is on PATH before the Git install. With no distribution installed it returns exit 1
with a UTF-16 message on **stdout** and an **empty stderr**. Every block then looks like a syntax
error with no error text, and the gate becomes an alarm that fires on all 62 blocks while
appearing to function. Measured from a PowerShell session on this machine. So this module resolves
bash by deriving it from the `git` executable first, never trusts a bare PATH lookup, and every
candidate must pass BOTH controls below before a single real block is judged. If none does, the
gate fails as HARNESS BROKEN -- it never skips (silent green) and never blanket-fails (noise that
buries the one real finding).
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
from pathlib import Path

from agentflow.tracked_paths import tracked_paths

# tests/ -> _shared/ -> skills/ -> suite root. Derived, never hard-coded: this suite is checked
# out under a different path in every worktree.
SKILLS_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = SKILLS_ROOT.parent

#: Trees whose prose is not this repo's to fix. `_vendored` is tracked (30 Markdown files) so it
#: must be excluded by name; `.venv` / `node_modules` / `.git` cannot reach a corpus built from
#: the index at all, and are listed anyway so the intent survives a future switch of source.
NOT_FIRST_PARTY = re.compile(r"(^|/)(\.venv|node_modules|_vendored|\.git)/")

#: Fence infostrings a reader would RUN. `shell` has no instances today, so including it does not
#: move the measured count; it is here so the next author cannot evade the gate by spelling it.
#:
#: `console` and `sh-session` are deliberately ABSENT and asserted absent instead (see
#: `test_no_first_party_block_hides_behind_an_unscanned_shell_fence`): their bodies carry `$`
#: prompts and interleaved output, so they are transcripts rather than runnable blocks and would
#: go red for reasons that are not defects.
SHELL_FENCES = frozenset({"bash", "sh", "shell"})

#: Fence spellings a reader would RUN are not limited to three backticks. CommonMark accepts any
#: run of 3+ backticks OR 3+ tildes, and a 4-backtick fence is the ordinary way to nest a block
#: whose body contains ``` -- which a repo documenting Markdown reaches for routinely.
#: MEASURED: matching only ```` ``` ```` let the verbatim pre-fix defect pass by changing its
#: fence to ```` ````bash ```` and nothing else. `MIN_BLOCKS` could not see it, because the count
#: fell 62 -> 61 against a floor of 40.
FENCE_RUN = re.compile(r"(`{3,}|~{3,})")

#: The infostring's FIRST WORD decides, so an attribute (```bash title="x") cannot smuggle a
#: block past the scanner. Split on whitespace and comma -- the two separators CommonMark
#: renderers and this corpus actually use.
_INFO_WORD = re.compile(r"[\s,]+")


def _is_shell_infostring(info: str) -> bool:
    """True when a reader would RUN this fence's contents."""
    first = _INFO_WORD.split(info.strip(), 1)[0]
    return first.lower() in SHELL_FENCES

#: `<name>`, `<suite_root>`, `<canonical_repo.local_checkout>`, `<public repo url>`. Spaces are
#: allowed because a real template uses them and the block that does (docs/runbooks/publish.md)
#: stays red without it. `2>&1`, `<(cmd)` and `<<'PY'` are all excluded by the first-character
#: and closing-`>` requirements -- checked against the corpus, not assumed.
PLACEHOLDER = re.compile(r"<[A-Za-z_][A-Za-z0-9_.\- ]*>")

#: What a placeholder is replaced with: an ordinary word, so a redirection becomes an argument.
NEUTRAL = "PLACEHOLDER"

#: Floor on the corpus. A gate that silently stops finding blocks reports the same green as one
#: with nothing to find. 62 today. 40 was the first value here and it was too slack to do the job:
#: it permitted a 29% collapse, which is more than enough room for an entire runbook to leave the
#: corpus -- or for a fence-spelling change to hide one -- with the gate still green. 55 keeps
#: headroom for ordinary documentation churn and still fails on a real enumeration collapse.
MIN_BLOCKS = 55

#: Must PARSE. Exercises compound syntax so a shell that only reads the first line is rejected.
CONTROL_VALID = 'set -eu\nfor f in a b; do\n  if [ -n "$f" ]; then echo "$f"; fi\ndone\n'

#: Must NOT parse, and it is the REAL defect shape rather than a toy: an odd number of single
#: quotes from an apostrophe inside `python -c '...'`. A harness that cannot see this one cannot
#: see the bug this module was written for, so that is what the control asserts.
CONTROL_BROKEN = "eval \"$(python -c '\n# the agent's own notes\nprint(1)\n')\"\n"


def _decode(raw: bytes) -> str:
    """Text from a child's stream, readable even when it is the WSL launcher's UTF-16."""
    if b"\x00" in raw:
        return raw.decode("utf-16-le", "replace").strip()
    return raw.decode("utf-8", "replace").strip()


def _parse(exe: str, script: str) -> tuple[int, str, str]:
    """Run `bash -n` over `script` on STDIN. Returns (exit, stderr, stdout).

    `-n` is noexec: bash reads the commands and does not run them, so a block that would
    `rm -rf` something is safe to hand it. `--noprofile --norc` keeps a developer's dotfiles
    from deciding the verdict. STDIN rather than a temp file so nothing is written anywhere.
    """
    try:
        proc = subprocess.run(
            [exe, "--noprofile", "--norc", "-n"],
            input=script.encode("utf-8"),
            capture_output=True,
            check=False,
            # `-n` cannot run a command, so nothing here should ever block -- but "should never
            # hang" is a claim about bash, and an unbounded wait in CI turns a wrong claim into a
            # job that dies on the runner's timeout with no finding attached. Bounded by
            # construction, not by belief.
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{exe} did not finish parsing within 30s", ""
    return proc.returncode, _decode(proc.stderr), _decode(proc.stdout)


def _reject_reason(exe: str) -> str | None:
    """None if `exe` is a real `bash -n`; otherwise why it was rejected, in the caller's words."""
    code, err, out = _parse(exe, CONTROL_VALID)
    if code != 0:
        detail = f"exit {code}, stderr={err[:160]!r}, stdout={out[:160]!r}"
        if out and not err:
            return (
                "refused a VALID control and wrote its complaint to STDOUT with an EMPTY stderr "
                f"-- the WSL-launcher signature, not a shell ({detail})"
            )
        return f"refused a VALID control ({detail})"
    if out:
        return f"printed to stdout while parsing a valid script, so it is not `bash -n`: {out!r}"
    code, err, _ = _parse(exe, CONTROL_BROKEN)
    if code == 0:
        return (
            "ACCEPTED a script with an unbalanced single quote, so it is parsing nothing and "
            "every block would look fine"
        )
    if not err:
        return "rejected the broken control but reported no reason on stderr, so failures are mute"
    return None


def _candidates() -> list[str]:
    """Bash executables to try, BEST FIRST.

    Derived from `git` before any bare PATH lookup: every machine that can run this suite has
    git (the corpus comes from the index), and on Windows a Git install ships the only bash that
    is actually a shell. A bare `which("bash")` is consulted last because on this platform it
    finds the WSL launcher first.
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(path: str | Path | None) -> None:
        if path is None:
            return
        text = str(path)
        if text not in seen and Path(text).exists():
            seen.add(text)
            out.append(text)

    git = shutil.which("git")
    if git:
        # `C:\Program Files\Git\cmd\git.exe` -> `...\Git\bin\bash.exe` / `...\Git\usr\bin\bash.exe`
        for base in list(Path(git).resolve().parents)[:3]:
            add(base / "bin" / "bash.exe")
            add(base / "usr" / "bin" / "bash.exe")
    add(shutil.which("bash"))
    return out


@functools.lru_cache(maxsize=1)
def _bash() -> str:
    """The first candidate that passes both controls, or a loud failure naming every rejection.

    Deliberately NOT a skip. A skip here is a green suite on a machine where the runbooks were
    never checked, which is the state this gate exists to end.
    """
    tried = []
    for exe in _candidates():
        reason = _reject_reason(exe)
        if reason is None:
            return exe
        tried.append(f"  {exe}\n      {reason}")
    listing = "\n".join(tried) if tried else "  (no bash executable found on this machine)"
    raise AssertionError(
        "HARNESS BROKEN: this gate parses documented shell with `bash -n` and could not find a "
        "working bash, so it can make no claim about the runbooks either way. Candidates tried, "
        f"best first:\n{listing}\n"
        "On Windows, install Git for Windows (it ships `bash.exe`); `C:\\Windows\\system32\\"
        "bash.exe` is the WSL launcher and is not a shell unless a distribution is installed."
    )


def shell_blocks(text: str) -> list[tuple[int, str]]:
    """(line of the opening fence, body) for every shell-fenced block in `text`."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    inside = False
    start = 0
    opener = ""
    for lineno, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        run = FENCE_RUN.match(stripped)
        if not inside:
            if run and _is_shell_infostring(stripped[run.end() :]):
                inside, buf, start, opener = True, [], lineno, run.group(0)
            continue
        # A closer must be the SAME character and at least as long as the opener, and carry no
        # infostring. Without that, a block writing a fenced file (`cat > out.md <<'EOF'` then a
        # ```bash line) ends at the INNER fence: the tail -- which may hold a real defect -- is
        # never scanned, and the surviving one-line fragment parses clean.
        if run and run.group(0)[0] == opener[0] and len(run.group(0)) >= len(opener) and not stripped[run.end() :].strip():
            out.append((start, "\n".join(buf)))
            inside = False
            opener = ""
            continue
        buf.append(line)
    return out


def verdict(body: str, exe: str | None = None) -> str | None:
    """None if the block is fine, "exempt" if a placeholder caused it, else bash's complaint.

    The re-parse is what makes "exempt" a statement about the CAUSE. See the module docstring:
    the one real defect in this corpus also contained placeholders.
    """
    exe = exe or _bash()
    code, err, _ = _parse(exe, body)
    # EXIT 0 IS NOT ENOUGH, and the gap sits exactly where this epic's own fix landed. bash scores
    # a heredoc that runs off the end of input as a WARNING on stderr and still exits 0, so an
    # unterminated `<<'PY'` -- the precise failure mode of the construct the autodoc runbook now
    # uses -- reads as clean. MEASURED on this corpus: two live blocks, both `python - <<'PYEOF'`
    # indented inside a Markdown list item so the terminator is not at column 0 (`<<-` strips tabs,
    # not spaces). Executed as written, the first exits 1 on IndentationError and the step that
    # branches on its output gets neither answer. Exactly 2 of 62 blocks emit stderr on exit 0 and
    # both are real, so requiring a silent exit costs no legitimate block.
    if code == 0 and not err:
        return None
    neutral = PLACEHOLDER.sub(NEUTRAL, body)
    n_code, n_err, _ = _parse(exe, neutral)
    if neutral != body and n_code == 0 and not n_err:
        return "exempt"
    return err or f"exit {code} with no message"


def _first_party_markdown() -> list[str]:
    """Suite-relative POSIX paths of the indexed first-party Markdown.

    From the GIT INDEX via the shared helper, never a tree walk -- see
    `tests/test_corpus_from_index.py` (gh#175). Membership from the index, CONTENT from the
    working tree, so a documentation fix moves this verdict as soon as it is written.
    """
    return sorted(
        rel
        for rel in tracked_paths(SUITE_ROOT)
        if rel.lower().endswith(".md") and not NOT_FIRST_PARTY.search(rel)
    )


# --------------------------------------------------------------------------------------------
# The control. This runs first and names itself, so a broken harness is never mistaken for a
# repository full of broken runbooks.
# --------------------------------------------------------------------------------------------
def test_the_bash_harness_actually_parses() -> None:
    exe = _bash()  # raises HARNESS BROKEN, naming every candidate and why it was rejected

    code, err, out = _parse(exe, CONTROL_VALID)
    assert code == 0, f"HARNESS BROKEN: {exe} rejected a valid script (exit {code}): {err or out!r}"
    assert not out, f"HARNESS BROKEN: {exe} wrote to stdout while parsing a valid script: {out!r}"

    code, err, _ = _parse(exe, CONTROL_BROKEN)
    assert code != 0, (
        f"HARNESS BROKEN: {exe} accepted a script with an unbalanced single quote. It is not "
        "parsing anything, so every block below would pass for free -- the exact false green "
        "this gate exists to prevent."
    )
    assert err, (
        f"HARNESS BROKEN: {exe} rejected the broken control but said nothing on stderr, so a "
        "real finding would arrive with no way to diagnose it."
    )


def test_a_placeholder_never_excuses_an_unrelated_syntax_error() -> None:
    """The false green this gate nearly shipped with, pinned so it cannot come back.

    The historical autodoc-update block carried `<data_root>` and `<name>` in Python comments AND
    an odd number of single quotes. Exempting on "contains a placeholder" would have hidden the
    only real defect in the corpus.
    """
    exe = _bash()

    template = "git -C <canonical_repo.local_checkout> push publish <branch>:<branch>\n"
    assert verdict(template, exe) == "exempt", (
        "a pure documentation template must be exempt, or every runbook in docs/ goes red"
    )

    both = "# derived <data_root>/projects/<name>/autodoc\n" + CONTROL_BROKEN
    result = verdict(both, exe)
    assert result not in (None, "exempt"), (
        "a block whose failure survives placeholder neutralisation is a DEFECT, not a template. "
        "Reporting it as exempt is how this gate would have shown green on the commit that "
        f"broke the autodoc-update runbook. Got: {result!r}"
    )


# --------------------------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------------------------
def test_every_documented_shell_block_parses() -> None:
    exe = _bash()
    scanned = 0
    exempted: list[str] = []
    offenders: list[str] = []

    for rel in _first_party_markdown():
        text = (SUITE_ROOT / rel).read_text(encoding="utf-8")
        for lineno, body in shell_blocks(text):
            scanned += 1
            result = verdict(body, exe)
            if result is None:
                continue
            if result == "exempt":
                exempted.append(f"{rel}:{lineno}")
                continue
            offenders.append(f"{rel}:{lineno}\n      {result.splitlines()[0]}")

    assert scanned >= MIN_BLOCKS, (
        f"only {scanned} shell blocks were found (floor {MIN_BLOCKS}, 62 when this was written). "
        "A gate whose enumeration has collapsed reports the same green as one with nothing to "
        "find -- check the fence infostrings and the first-party exclusions."
    )
    assert exempted != [] or offenders, (
        "no block was placeholder-exempt. Eight documentation templates cannot parse and never "
        "will, so an empty exemption set means the placeholder pattern stopped matching and the "
        "next real defect will arrive buried in eight false alarms."
    )
    assert not offenders, (
        f"{len(offenders)} documented shell block(s) do not parse under `bash -n`, so nobody "
        "following that runbook can run them -- and the failure is silent, because prose does "
        "not get executed until a reader tries it:\n  "
        + "\n  ".join(offenders)
        + f"\n\n({scanned} blocks scanned, {len(exempted)} exempt as placeholder templates.)"
    )


def test_a_fence_spelling_cannot_smuggle_a_defect_past_the_scanner() -> None:
    """The evasion that made this gate green over the verbatim defect (review finding, HIGH).

    Restoring the pre-fix block and changing ONLY its fence from ```bash to ````bash -- not one
    byte of the broken shell -- took the suite from red to green. `MIN_BLOCKS` could not see it:
    the count fell 62 -> 61 against a floor of 40.
    """
    exe = _bash()
    for opener, closer in (
        ("```bash", "```"),
        ("````bash", "````"),
        ("~~~bash", "~~~"),
        ('```bash title="x"', "```"),
        ("```BASH", "```"),
        ("```sh,ignore", "```"),
    ):
        doc = f"{opener}\n{CONTROL_BROKEN}{closer}\n"
        blocks = shell_blocks(doc)
        assert len(blocks) == 1, f"{opener!r} was not recognised as a shell block: {blocks!r}"
        assert verdict(blocks[0][1], exe) not in (None, "exempt"), (
            f"the defect passed when fenced as {opener!r}"
        )


def test_an_inner_fence_cannot_truncate_a_block_and_hide_the_tail() -> None:
    """A runbook that WRITES a fenced file must not end at the fence it is writing.

    Closing on the first ``` regardless of infostring ended the block at the inner fence; the
    tail -- which may hold a real defect -- became prose, and the surviving one-line fragment
    `cat > out.md <<'EOF'` passed, because bash -n exits 0 on a bare unterminated heredoc.
    """
    exe = _bash()
    doc = "```bash\ncat > out.md <<'EOF'\n```bash\n" + CONTROL_BROKEN + "```\nEOF\n```\n"
    blocks = shell_blocks(doc)
    assert len(blocks) == 1, f"expected one block, got {len(blocks)}: {blocks!r}"
    body = blocks[0][1]
    assert "python -c" in body, "the block was truncated at the inner fence; the tail was lost"
    assert verdict(body, exe) not in (None, "exempt"), "the hidden defect was reported clean"


def test_an_unterminated_heredoc_is_a_defect_not_a_clean_block() -> None:
    """exit 0 is not enough, and the gap sits where this epic's own fix landed.

    bash scores a heredoc running off the end of input as a WARNING and still exits 0. Two live
    corpus blocks were unrunnable and reported clean by that rule.
    """
    exe = _bash()
    code, err, _ = _parse(exe, "cat <<'EOF'\nnever closed\n")
    assert code == 0 and err, (
        "bash no longer warns-and-exits-0 on an unterminated heredoc; if it now fails outright "
        f"this test's premise is gone (exit {code}, stderr {err!r})"
    )
    assert verdict("cat <<'EOF'\nnever closed\n", exe) not in (None, "exempt"), (
        "an unterminated heredoc must be a defect: it is the failure mode of the construct the "
        "autodoc-update runbook now uses"
    )
    # And the shipped shape is genuinely safe -- not merely untested.
    assert verdict('eval "$(python - <<\'PY\'\nprint(1)\nPY\n)"\n', exe) is None


def test_no_first_party_block_hides_behind_an_unscanned_shell_fence() -> None:
    """`console` / `sh-session` are excluded by decision, so their absence is asserted.

    They are transcripts -- `$` prompts and interleaved output -- so scanning them would go red
    for reasons that are not defects. An excluded spelling nobody checks is how the next runnable
    block leaves the corpus silently.
    """
    found = []
    for rel in _first_party_markdown():
        text = (SUITE_ROOT / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.split("\n"), 1):
            stripped = line.strip()
            run = FENCE_RUN.match(stripped)
            if run and _INFO_WORD.split(stripped[run.end() :].strip(), 1)[0].lower() in {
                "console",
                "sh-session",
                "shell-session",
                "zsh",
            }:
                found.append(f"{rel}:{lineno}")
    assert not found, (
        "first-party Markdown now uses a shell fence spelling this gate does not scan. Either "
        "add it to SHELL_FENCES (and expect transcript blocks to go red) or rewrite the block:\n  "
        + "\n  ".join(found)
    )
