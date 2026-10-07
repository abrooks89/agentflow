"""Ntfy POST helper — minimal trigger types only.

Three rules govern every send:

**The config decides the destination, the environment holds the value.**
``notify.kind`` switches the channel off, and ``notify.url_env`` / ``notify.topic_env``
name the two environment variables the base URL and topic are read from — so an
operator renames the variables in one place. A send that cannot happen because those
variables are unset says so on stderr, naming the variables and never their values, and
a send that goes out through the built-in default names because the config would not
resolve says THAT on stderr too — a ping arriving at the old destination is otherwise
indistinguishable from a working channel. ``kind: none`` is silent, because it is the
answer the operator gave.

**The sender is injectable.** ``notify(..., sender=...)`` takes the same shape
``issue_intake`` uses for its ``runner``: the default is the real POST path and
tests pass a fake, so no test in this repo can reach the network. A sender is
``sender(url: str, headers: dict[str, str], body: bytes) -> None``.

**The payload is ASCII-only, and non-ASCII is REJECTED, not transliterated.**
The project profile sets ``notify.ascii_only`` and the fleet has been bitten by
multibyte ntfy bodies (the title rides in an HTTP header, where a non-ASCII
glyph is a 400 at best and a mojibake banner at worst). ``notify()`` therefore
raises :class:`NtfyAsciiError` naming the field and the offending codepoint
rather than quietly mangling what a human is about to read. ``epic`` and ``detail``
are asserted BEFORE the env check, so a bad body is loud on a dev box with no ntfy
configured instead of only in production; the TITLE's assertion runs after that gate,
so a non-ASCII title alone is not caught until ntfy is actually configured.

A caller that legitimately holds prose with typographic punctuation folds it
explicitly first::

    notify(trigger, epic=slug, detail=to_ascii(text))

``to_ascii`` is lossy on purpose; making the loss an explicit call at the call
site is the whole point.
"""

from __future__ import annotations

import enum
import os
import sys
import unicodedata
from typing import Any, Callable, NamedTuple, Optional
from urllib.request import Request, urlopen

_TIMEOUT = 5  # seconds; a wedged ntfy host must never hang a wake

#: The env-var names notify() consulted before notify.url_env/notify.topic_env were
#: honoured (gh#133), and the fallback whenever no config resolves. ONE definition
#: each: f10226f shipped the same default in three places and a mutation to any one
#: left the other two answering, so no test could see it.
_DEFAULT_URL_ENV = "NTFY_URL"
_DEFAULT_TOPIC_ENV = "NTFY_TOPIC"


class NtfyError(Exception):
    """Raised when a notification cannot be built — a misconfigured trigger, a
    payload that violates the ASCII rule. Transport failures are the sender's
    own exceptions and propagate unchanged."""


class NtfyAsciiError(NtfyError, ValueError):
    """Raised when a title or body field carries a non-ASCII character."""


class TriggerType(enum.Enum):
    ESCALATION = "escalation"
    PHASE_COMPLETE = "phase_complete"
    EPIC_DONE = "epic_done"
    HARD_FAILURE = "hard_failure"
    SPEC_QUALITY_REVIEW = "spec_quality_review"
    # A loop stopped on a question and needs a human to open dev-manager. Fired
    # once per halted TRANSITION by the registry CLI — never once per wake.
    HALTED_QUESTION = "halted_question"


# Every TriggerType MUST appear in both maps: a missing entry is a failure at
# SEND time (i.e. in production), not at import time. test_ntfy.py asserts
# completeness over `list(TriggerType)` so adding a member without its rows fails
# the suite instead of a live ping.
_PRIORITY = {
    TriggerType.ESCALATION: "high",
    TriggerType.HARD_FAILURE: "high",
    TriggerType.EPIC_DONE: "default",
    TriggerType.PHASE_COMPLETE: "default",
    TriggerType.SPEC_QUALITY_REVIEW: "high",
    TriggerType.HALTED_QUESTION: "high",
}


_TAGS = {
    TriggerType.ESCALATION: "warning",
    TriggerType.PHASE_COMPLETE: "white_check_mark",
    TriggerType.EPIC_DONE: "tada",
    TriggerType.HARD_FAILURE: "rotating_light",
    TriggerType.SPEC_QUALITY_REVIEW: "mag",
    TriggerType.HALTED_QUESTION: "octagonal_sign",
}


# sender(url, headers, body) -> None
Sender = Callable[[str, dict, bytes], None]


# --------------------------------------------------------------------------- #
# ASCII rule
# --------------------------------------------------------------------------- #
# Typographic characters that carry meaning worth keeping. Anything not listed
# falls to NFKD decomposition, and anything that survives that is dropped.
_ASCII_FOLD = {
    "‘": "'",   # left single quote
    "’": "'",   # right single quote / apostrophe
    "‚": "'",
    "“": '"',   # left double quote
    "”": '"',   # right double quote
    "„": '"',
    "–": "-",   # en dash
    "—": "-",   # em dash
    "−": "-",   # minus sign
    "·": "-",   # middle dot (the old title separator)
    "•": "*",   # bullet
    " ": " ",   # non-breaking space
    "→": "->",
    "⇒": "=>",
    "≤": "<=",
    "≥": ">=",
}


def to_ascii(text: str) -> str:
    """Fold ``text`` to plain ASCII. **Lossy on purpose.**

    Common typography is folded to its ASCII equivalent, accented letters are
    decomposed to their base letter, and anything still non-ASCII (CJK, emoji)
    is DROPPED. Call this deliberately at the call site when the source prose is
    not under your control; ``notify()`` never calls it for you, because silently
    rewriting a human-facing notification is worse than refusing to send it.
    """
    folded = "".join(_ASCII_FOLD.get(ch, ch) for ch in text)
    decomposed = unicodedata.normalize("NFKD", folded)
    return decomposed.encode("ascii", "ignore").decode("ascii")


class _ResolvedNotify(NamedTuple):
    """Everything ``notify()`` needs from config, read in one pass.

    ``config_error`` is the exception CLASS NAME when the config could not be
    resolved at all and the fallbacks below are in force, else None. It is carried
    rather than discarded because "the names you are looking at are not the ones you
    configured" is the difference between two completely different fixes.
    """

    kind: str
    url_env: str
    topic_env: str
    ascii_only: bool
    config_error: Optional[str]


def _resolved_notify() -> _ResolvedNotify:
    """Resolve the ``notify`` block: kind, the two env-var NAMES, and the ASCII rule.

    WHY THIS EXISTS (gh#133): ``url_env``, ``topic_env`` and ``kind`` were decorative.
    The schema required the two names whenever kind != none (config.py:551-553), stored
    them (config.py:556-557) and printed them in the resolved block (config.py:1177-1183)
    -- while this module read the literals NTFY_URL/NTFY_TOPIC and consulted ``kind``
    nowhere at all. An operator who named the variables the schema invites them to name
    got no notifications and no warning; one who set ``kind: none`` still got a POST
    whenever NTFY_URL happened to be exported.

    Resolved ONCE per send rather than once per field: ``load_environment()`` is uncached
    and re-reads the file from disk (config.py:886-890), and the previous shape reached it
    three times per notify() through ``assert_ascii``.

    The two fallback directions are deliberately OPPOSITE. ``ascii_only`` falls back
    STRICT, because refusing to send beats mangling a banner a human is about to read.
    ``kind`` falls back to "ntfy" -- PERMISSIVE -- because suppression must be an
    affirmatively-read ``kind: none`` and never inferred from a config that failed to
    parse: silencing a fleet's only halt notification on the strength of a YAML error is
    the worst outcome available, and an unreadable config is itself the kind of incident
    that makes the ping more valuable, not less.

    That permissiveness is NOT free, and ``config_error`` is what pays for it. The
    failure is routine, not exotic: ``_require_env_name`` (config.py:468-474) RAISES when
    a configured name is absent from os.environ, so an operator who renames the variables
    in config and has not yet exported them lands here with the DEFAULT names
    substituted -- and if the old variables are still exported, the ping is delivered to
    the old destination. ``notify()`` therefore says so on the send path
    (``_warn_default_names_substituted``); the substitution is allowed, the silence is
    not.
    """
    try:
        from agentflow.config import load_environment

        nt = load_environment().notify
    except Exception as exc:  # noqa: BLE001 - not narrower than Exception on purpose:
        # load_environment() fails with more than ConfigError (config_home() calls
        # Path.home(), read_text() raises OSError), and this runs on the side-effect
        # path of callers that have already written a durable row. The fallback IS the
        # handling, and the class name is reported if a send is then dropped.
        return _ResolvedNotify(
            "ntfy", _DEFAULT_URL_ENV, _DEFAULT_TOPIC_ENV, True, type(exc).__name__
        )
    return _ResolvedNotify(
        kind=getattr(nt, "kind", None) or "ntfy",
        url_env=getattr(nt, "url_env", None) or _DEFAULT_URL_ENV,
        topic_env=getattr(nt, "topic_env", None) or _DEFAULT_TOPIC_ENV,
        ascii_only=bool(getattr(nt, "ascii_only", True)),
        config_error=None,
    )


def _ascii_only_configured() -> bool:
    """Honour ``notify.ascii_only`` from the resolved project config.

    It is a real, parsed, defaulted field (`config.py`), so a project that sets
    ``ascii_only: false`` must NOT get a hard raise. Config resolution can fail in a
    bare test/dev environment; that falls back STRICT rather than silently loosening
    the rule, which is the one direction that cannot mangle a banner a human reads.

    There is no ``default`` parameter. It had one, and every caller took it -- so the
    error branch could only ever return True while the signature advertised a choice
    nobody could make. An unused knob that reads as configurable is worse than no knob.
    """
    resolved = _resolved_notify()
    return True if resolved.config_error is not None else resolved.ascii_only


def assert_ascii(text: str, *, field: str, ascii_only: Optional[bool] = None) -> str:
    """Return ``text`` unchanged, or raise :class:`NtfyAsciiError` naming the
    field and the first offending codepoint.

    Skipped entirely when the project sets ``notify.ascii_only: false``. ``notify()``
    passes the value it has already resolved; every other caller leaves it None and
    pays for its own resolve."""
    if ascii_only is None:
        ascii_only = _ascii_only_configured()
    if not ascii_only:
        return text
    for index, ch in enumerate(text):
        if ord(ch) > 127:
            raise NtfyAsciiError(
                f"ntfy {field} must be ASCII-only (notify.ascii_only): "
                f"U+{ord(ch):04X} at position {index} in {text!r}; "
                f"fold it at the call site with agentflow.ntfy.to_ascii()"
            )
    return text


# --------------------------------------------------------------------------- #
# Sender
# --------------------------------------------------------------------------- #
def _urlopen_sender(url: str, headers: dict, body: bytes) -> None:
    """Default sender: POST ``body`` to ``url``. The only code path here that
    touches the network."""
    req = Request(url, data=body, method="POST", headers=headers)
    with urlopen(req, timeout=_TIMEOUT) as resp:
        resp.read()


def _headers_for(trigger: TriggerType, title: str) -> dict:
    """Priority/tag lookup with an explicit failure — a bare KeyError from a map
    miss would surface as an unhelpful traceback at send time."""
    try:
        priority = _PRIORITY[trigger]
        tags = _TAGS[trigger]
    except KeyError as e:
        raise NtfyError(
            f"trigger {trigger!r} is missing a _PRIORITY/_TAGS entry in agentflow.ntfy"
        ) from e
    return {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
    }


def _env_value(name: str) -> Optional[str]:
    """Read env var ``name``: None when unset, otherwise the value STRIPPED.

    Stripping folds a whitespace-only value ("   ", a stray line continuation in a .env)
    into the existing set-but-empty branch. Without it that value is truthy, builds a
    URL out of spaces and reaches the sender, which raises ValueError out of notify() --
    an escape route past the one place that knows how to explain the problem. None is
    preserved as None so ``_warn_not_sent`` can still tell unset from set-but-empty.
    """
    raw = os.environ.get(name)
    return raw if raw is None else raw.strip()


def _warn_default_names_substituted(
    resolved: _ResolvedNotify, trigger: TriggerType, epic: str
) -> None:
    """One stderr line when a send GOES OUT through the substituted default names.

    The permissive fallback in ``_resolved_notify`` is deliberate and stays. But "send
    anyway" and "say nothing about it" are separable, and only the second is a defect:
    an operator who configured ``notify.url_env: MY_VAR`` without exporting MY_VAR makes
    load_environment() raise, gets NTFY_URL/NTFY_TOPIC substituted, and -- if those are
    still exported from before the rename -- watches the ping land at the OLD
    destination with nothing printed. That is worse than the silence gh#133 filed: it
    delivers somewhere wrong rather than nowhere.

    Names only, never values, for the same reason as ``_warn_not_sent``.

    Nothing prints on a box whose config resolves: ``config_error`` is None there, and
    this is its only reader on the send path.
    """
    print(
        f"warn: ntfy sent via the built-in default names ({resolved.url_env} and "
        f"{resolved.topic_env}) for {trigger.value} on {epic!r}: the agentflow config "
        f"did not resolve ({resolved.config_error}), so notify.url_env and "
        f"notify.topic_env could not be read - if you renamed those variables, this "
        f"ping did NOT go where you configured it",
        file=sys.stderr,
    )


def _warn_not_sent(
    resolved: _ResolvedNotify,
    trigger: TriggerType,
    epic: str,
    *,
    base: Optional[str],
    topic: Optional[str],
) -> None:
    """One stderr line when notifications are ON but the environment supplies no
    destination. This is the loud half of gh#133: the operator believes the channel
    works, so a silent return is exactly the bug.

    NAMES ONLY, never values. config.py reads env vars by name for this reason
    (config.py:468-474) and the install suite asserts that a set URL's value never
    reaches a message.

    "unset" and "set but empty" are reported differently because config validation
    structurally cannot tell them apart: ``_require_env_name`` (config.py:472) is a
    membership test, so an exported-but-empty var passes ``config show`` clean and then
    drops here. That is the one state the operator cannot diagnose from the config.
    A whitespace-only value is folded into "set but empty" by ``_env_value``.
    """
    problems = []
    for field, name, value in (
        ("notify.url_env", resolved.url_env, base),
        ("notify.topic_env", resolved.topic_env, topic),
    ):
        if value:
            continue
        problems.append(f"{name} ({field}) {'set but empty' if value is not None else 'unset'}")

    note = ""
    if resolved.config_error is not None:
        # The drop half of the same disclosure _warn_default_names_substituted makes on
        # the send path. Both are on notify()'s paths rather than in _resolved_notify,
        # so a resolve that no send follows stays quiet.
        note = (
            f"; the agentflow config did not resolve ({resolved.config_error}), "
            f"so the built-in default names were used"
        )

    print(
        f"warn: ntfy not sent ({trigger.value} for {epic!r}): {' and '.join(problems)}"
        f"{note}; export the named variable(s), or set notify.kind: none",
        file=sys.stderr,
    )


def notify(
    trigger: TriggerType,
    *,
    epic: str,
    detail: str,
    task: Optional[int] = None,
    task_total: Optional[int] = None,
    sender: Optional[Sender] = None,
) -> bool:
    """POST a minimal notification to ntfy, to the destination the config names.

    ``sender`` defaults to the real POST path; inject a fake in tests. Both
    ``epic`` and ``detail`` must be ASCII (see the module docstring) — a
    non-ASCII field raises :class:`NtfyAsciiError` before anything is sent, and
    before the env check.

    Three config fields decide what happens (all of them were decorative before
    gh#133): ``notify.kind: none`` means no send and no output; ``notify.url_env`` and
    ``notify.topic_env`` name the environment variables the destination is read from.
    Config supplies the NAMES, the environment supplies the values — nothing here ever
    reads a secret out of a file.

    Returns ``True`` when the sender returned without raising (with the default sender,
    ntfy accepted the POST) and ``False`` when nothing was sent: ``notify.kind: none``, or
    a missing or empty destination. A raising sender still raises, so ``False`` never
    means "the POST failed". ``True`` is not a claim that a human read the push.
    """
    resolved = _resolved_notify()

    assert_ascii(epic, field="epic", ascii_only=resolved.ascii_only)
    assert_ascii(detail, field="detail", ascii_only=resolved.ascii_only)

    # An affirmative `kind: none` is a first-class answer, not a degraded mode, so it is
    # SILENT — warning about a state the operator chose is a nag, and the resolved block
    # already prints "[notify] none" at session start (config.py:1177-1178). It sits
    # below the ASCII gate for the same reason the env check does: a bad body must be
    # loud on a box with notifications switched off, not only in production.
    if resolved.kind == "none":
        return False

    base = _env_value(resolved.url_env)
    topic = _env_value(resolved.topic_env)
    if not base or not topic:
        _warn_not_sent(resolved, trigger, epic, base=base, topic=topic)
        return False

    # A send is about to happen through names the operator did not choose. Said before
    # the send, not after, so a sender that raises does not take the disclosure with it.
    if resolved.config_error is not None:
        _warn_default_names_substituted(resolved, trigger, epic)

    # Lead the title with task progress + project name when available — the title is
    # what mobile ntfy shows in the banner (the body is truncated/hidden). Fall back to
    # "<project> - <trigger>" for non-task events (epic_done, escalation, etc.).
    # The separator is a plain hyphen: the old middle dot violated the ASCII rule.
    if task is not None and task_total is not None:
        title = f"Task {task} of {task_total} - {epic}"
    elif task is not None:
        title = f"Task {task} - {epic}"
    else:
        title = f"{epic} - {trigger.value}"

    headers = _headers_for(
        trigger, assert_ascii(title, field="title", ascii_only=resolved.ascii_only)
    )
    active = sender if sender is not None else _urlopen_sender
    active(f"{base.rstrip('/')}/{topic}", headers, detail.encode("ascii"))
    return True


def notify_once(
    state: Any,
    key: str,
    trigger_type: TriggerType,
    dedup_window_seconds: int = 3600,
    **kwargs: Any,
) -> bool:
    """Send a notification only if the same key has not been sent recently.

    Uses ``was_notification_sent_recently`` to gate on ``state.notifications_sent``,
    then calls ``notify(trigger_type, **kwargs)`` and records the key via
    ``record_notification_sent`` only when ``notify()`` returned ``True``. ``kwargs``
    passes straight through, so ``sender=`` is injectable here too.

    Returns:
        True  — not suppressed by dedup, so notify() was called.
        False — suppressed because the key was sent within ``dedup_window_seconds``.

    ``True`` is not a delivery receipt: it means only that dedup did not suppress the
    call. The KEY, though, is recorded only for a send ``notify()`` reports as made, so a
    call that found no destination, or ran under ``notify.kind: none``, leaves no record
    and the next call is attempted again instead of being suppressed for the whole
    window. A raising sender propagates before the record, so it leaves none either.

    Important:
        This function does NOT call ``write_status``. The caller is responsible for
        persisting ``state`` after calling ``notify_once`` so the dedup record
        survives across wakes.

    Key namespacing:
        The same ``key`` used for different ``trigger_type`` values would suppress
        unrelated events. Callers should namespace keys to include the event type,
        e.g. ``"PHASE_COMPLETE:phase_b"`` or ``"EPIC_DONE:my-epic-slug"``.
    """
    from agentflow.status_writer import record_notification_sent, was_notification_sent_recently

    if was_notification_sent_recently(state, key, dedup_window_seconds):
        return False

    if notify(trigger_type, **kwargs):
        record_notification_sent(state, key)
    return True
