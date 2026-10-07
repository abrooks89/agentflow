"""One way to say what a failed subprocess said: BOTH streams, labelled, budgeted each.

WHY THIS IS A MODULE AND NOT A LINE IN ITS CALLER (gh#478). `save_summariser` and
`issue_intake` each turned a non-zero exit into a human-readable reason with the SAME
expression -- `(stderr or stdout or "")` -- and that expression is wrong in the same way in
both. Fixing one site would have left the other holding the defect this epic exists to
remove, which is the shape of the bug one level up. So the choice lives here once, and each
caller spends one call on it.

WHAT WAS MEASURED, and it is the opposite of what that expression assumes. On the toolchain
this fleet runs, the agent CLI prints an ADVISORY on stderr that accompanies SUCCESSFUL runs
too -- a probe against a different model exited 0 and still printed it, so it is not a failure
signal at all -- while the line that actually TERMINATED the run arrived on stdout. `stderr or
stdout` therefore reports the advisory whenever stderr is non-empty, which is always, and the
200-character clamp over the winner meant a cause on stdout could not appear even as a tail.

THE COST, and none of it is hypothetical. Every lane that hit this was handed the advisory as
"the reason": gh#451 was titled after the wrong cause, B-0023 went onto the registry carrying
the same wrong cause, and Q-10080 put a false premise to a human whose stated default would
have approved a fix that cannot work. Five seats repeated it for roughly ten sweeps. The
advisory line has been present since 2026-05-10, through every successful save.

WHY NOT `stdout or stderr`. **That inverts the bug rather than fixing it, and the inversion is
not a smaller bug.** The callers' own default runners SYNTHESISE causes on stderr with an
empty stdout -- `save_summariser._subprocess_runner` returns `(-1, "", "timed out after ...")`
for a timeout and `(NEVER_STARTED, "", "agent CLI not found: ...")` for a missing binary. A stdout-first
precedence would hide those exactly as today's precedence hides a model limit. NO PRECEDENCE
IS CORRECT, because neither stream is reliably the cause and nothing in the triple says which
one spoke. The only shape that cannot be wrong is to print both, labelled, and let the reader
decide. If you are here to make this pick a stream again: that is the regression, not a
simplification.
"""

from __future__ import annotations

#: Per-stream character budget. 200 is the clamp the buggy line already applied -- kept rather
#: than retuned, so nothing that fitted in a reason before fits less well now -- but applied
#: PER STREAM instead of to whichever stream won. That is the second half of gh#478: one
#: budget over one winner let a chatty advisory consume the whole allowance and starve the
#: stream carrying the cause. The worst case is now two clamped streams plus their labels,
#: around 460 characters, which still reads at a glance in a registry Notes cell, an ntfy body
#: or a halt line.
STREAM_CHARS = 200

#: What a stream with nothing in it renders as. A bare `stdout:` with nothing after it reads
#: like a formatting bug and invites the reader to go looking for the missing text; `<empty>`
#: is a positive statement that the stream was read and was empty -- which, when BOTH are, is
#: the whole diagnosis: the process died without saying anything, so the exit code is all
#: there is.
EMPTY = "<empty>"

#: What separates the two labelled streams. **It must not be a pipe, and that is a hard
#: constraint rather than a taste.** These reasons are written into the fleet's registry --
#: `registry._INLINE_FIELDS` lists `reason` and `text`, and `_require_inline` REFUSES the whole
#: write on a `|`, because the registry is a markdown table and a pipe inside a cell opens a
#: new column for every loop. A separator chosen for looks would have made the one string
#: this epic exists to deliver the one string that cannot travel the path it was written for.
#: A semicolon also matches the joiner the call sites already use between the exit code and
#: this text, so the whole reason reads as one delimited list instead of two syntaxes.
SEPARATOR = "; "

#: A pipe found INSIDE a stream becomes this. The separator above is ours to choose; the
#: child's output is not, and a shell pipeline or an ASCII table echoed back in an error
#: message carries pipes we did not put there. Substituting one character keeps the reason
#: writable; refusing it would mean the diagnostic cannot be recorded in exactly the runs
#: where it matters. This is the ONLY character of the child's text this module alters, and
#: it is named here so a reader who sees `/` where the child printed `|` can find out why.
PIPE_SUB = "/"


def clip_stream(text: str | None, limit: int = STREAM_CHARS) -> str:
    """One stream, flattened to a single INLINE-SAFE line and clamped to ``limit`` characters.

    Interior whitespace is collapsed because the callers render one-line audit records: a
    multi-line advisory pasted in raw turns one record into several, and a reader scanning a
    log cannot tell the continuation lines from the next event. Collapsing first also makes
    the budget mean what it looks like -- ``limit`` characters a human will actually read,
    not ``limit`` bytes mostly spent on indentation.

    Pipes become ``PIPE_SUB`` for the reason given there: the destination is a markdown table
    cell in the registry, whose guard rejects the entire write on one ``|``. Collapsing the
    whitespace already removes every newline, the other character that guard refuses, so what
    this returns is writable into any registry free-text field by construction rather than by
    the caller remembering to check.

    Truncation is MARKED and COUNTED (``...(+N chars)``). A silently shortened cause is the
    failure mode this module exists to end; a reader who can see that 3,000 characters were
    dropped knows to go to the transcript, while a reader shown a clean-looking tail does not.
    """
    flat = " ".join((text or "").replace("|", PIPE_SUB).split())
    if not flat:
        return EMPTY
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit].rstrip()}...(+{len(flat) - limit} chars)"


def describe_streams(
    stdout: str | None, stderr: str | None, *, limit: int = STREAM_CHARS
) -> str:
    """Both streams of a failed process, labelled: ``stdout: ...; stderr: ...``.

    Pair it with the exit code at the call site -- ``f"... exited {code}; {describe_streams(
    out, err)}"`` -- because the three together are what let a reader who has never seen the
    code judge the failure: the code says the process ended badly, and the labels say which
    stream said what, so an advisory that shows up on the SUCCESSFUL runs too can be
    recognised for what it is instead of being read as the cause.

    stdout is printed first only because it is the stream the callers otherwise PARSE -- the
    child's answer channel, and, measured on gh#478, where the terminating cause arrived.
    **Position is a reading convenience and never a claim about which stream is the cause**;
    both are always present, including when one is empty.

    The result carries no newline and no ``|``, so it can be written into a registry
    free-text field -- the path the fleet's own failure reports travel -- without the caller
    sanitising it first. See ``SEPARATOR``.
    """
    return (
        f"stdout: {clip_stream(stdout, limit)}"
        f"{SEPARATOR}"
        f"stderr: {clip_stream(stderr, limit)}"
    )
