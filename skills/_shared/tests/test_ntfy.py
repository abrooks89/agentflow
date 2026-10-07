"""Tests for the shared ntfy helper.

**No test in this file may touch the network.** Every send goes through an
injected ``sender`` (the house pattern ``issue_intake`` uses for its ``runner``);
the one test that exercises the default wiring monkeypatches the module's
urlopen sender and asserts it was selected, never that it ran.

Covers: the halted-question trigger and its priority/tag entries, the
map-completeness invariant (a TriggerType missing from either map would be a
send-time failure, not an import-time one), sender injection, the documented
env-unset skip, the ASCII rule, and backward compatibility with the pre-existing
``notify()`` signature.
"""

import types

import pytest

from agentflow import ntfy as ntfy_mod
from agentflow.ntfy import (
    _DEFAULT_TOPIC_ENV,
    _DEFAULT_URL_ENV,
    _PRIORITY,
    _TAGS,
    NtfyAsciiError,
    NtfyError,
    TriggerType,
    _ResolvedNotify,
    assert_ascii,
    notify,
    notify_once,
    to_ascii,
)

URL = "https://notify.invalid"
TOPIC = "fleet-test-topic"


class FakeSender:
    """Records every (url, headers, body) it is handed. Never opens a socket."""

    def __init__(self, exc=None):
        self.calls: list = []
        self.exc = exc

    def __call__(self, url, headers, body):
        self.calls.append((url, dict(headers), body))
        if self.exc is not None:
            raise self.exc


@pytest.fixture(autouse=True)
def hermetic_config(tmp_path, monkeypatch):
    """Point every test in this module at an EMPTY config home.

    notify() resolves notify.kind / url_env / topic_env / ascii_only from the agentflow
    config, so without this the suite reads whatever config the machine running it
    happens to have. That is not hypothetical: a contributor whose environment.md said
    `notify.kind: none` would watch every send test in this file fail, and a green run
    here would mean "this developer's config" rather than "this code".

    An empty home does NOT resolve, and the distinction matters to anyone reading a
    failure here: load_environment() raises ConfigError ("config file not found"), so
    the values arrive through _resolved_notify's CONFIG-ERROR fallback - kind ntfy, the
    historical default names, ascii_only strict - not through a parsed config. Same
    values, different mechanism, and one visible consequence: every send under this
    fixture also prints the substitution warning notify() emits when it falls back.
    CI has the same shape.

    Tests that need a REAL resolved config re-point AGENTFLOW_CONFIG themselves.
    """
    empty = tmp_path / "no-config"
    empty.mkdir()
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(empty))


@pytest.fixture
def ntfy_env(monkeypatch):
    monkeypatch.setenv("NTFY_URL", URL)
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)


# --------------------------------------------------------------------------- #
# (a) the halted-question trigger + map completeness
# --------------------------------------------------------------------------- #
def test_halted_question_trigger_exists():
    assert TriggerType.HALTED_QUESTION.value == "halted_question"


def test_halted_question_is_high_priority_with_a_tag():
    assert _PRIORITY[TriggerType.HALTED_QUESTION] == "high"
    assert _TAGS[TriggerType.HALTED_QUESTION]


@pytest.mark.parametrize("trigger", list(TriggerType), ids=lambda t: t.value)
def test_every_trigger_has_a_priority_and_a_tag(trigger):
    """A member missing from either map KeyErrors at SEND time, i.e. in prod."""
    assert trigger in _PRIORITY, f"{trigger} missing from _PRIORITY"
    assert trigger in _TAGS, f"{trigger} missing from _TAGS"
    assert _PRIORITY[trigger] and _TAGS[trigger]


def test_the_five_original_triggers_are_unchanged():
    """Backward compatibility: the pre-existing members keep their names+values."""
    for name, value in [
        ("ESCALATION", "escalation"),
        ("PHASE_COMPLETE", "phase_complete"),
        ("EPIC_DONE", "epic_done"),
        ("HARD_FAILURE", "hard_failure"),
        ("SPEC_QUALITY_REVIEW", "spec_quality_review"),
    ]:
        assert TriggerType[name].value == value


def test_unmapped_trigger_fails_loudly_not_with_a_bare_keyerror(ntfy_env, monkeypatch):
    monkeypatch.delitem(_TAGS, TriggerType.HALTED_QUESTION)
    sender = FakeSender()
    with pytest.raises(NtfyError, match="HALTED_QUESTION"):
        notify(
            TriggerType.HALTED_QUESTION,
            epic="af-fleet",
            detail="halted on Q-0007 - open dev-manager",
            sender=sender,
        )
    assert sender.calls == []


# --------------------------------------------------------------------------- #
# (b) injectable sender
# --------------------------------------------------------------------------- #
def test_sender_called_once_with_expected_title_and_body(ntfy_env):
    sender = FakeSender()
    notify(
        TriggerType.HALTED_QUESTION,
        epic="af-fleet",
        detail="jr-dev-1 halted on Q-0007 - open dev-manager",
        sender=sender,
    )
    assert len(sender.calls) == 1
    url, headers, body = sender.calls[0]
    assert url == f"{URL}/{TOPIC}"
    assert headers["Title"] == "af-fleet - halted_question"
    assert headers["Priority"] == "high"
    assert headers["Tags"] == _TAGS[TriggerType.HALTED_QUESTION]
    assert body == b"jr-dev-1 halted on Q-0007 - open dev-manager"


def test_trailing_slash_on_base_url_is_not_doubled(monkeypatch):
    monkeypatch.setenv("NTFY_URL", URL + "/")
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)
    assert sender.calls[0][0] == f"{URL}/{TOPIC}"


def test_default_sender_is_the_urlopen_path(ntfy_env, monkeypatch):
    """The default must be the real POST path -- asserted by substitution, never by
    letting a socket open."""
    seen: list = []
    monkeypatch.setattr(
        ntfy_mod, "_urlopen_sender", lambda url, headers, body: seen.append(url)
    )
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done")
    assert seen == [f"{URL}/{TOPIC}"]


def test_sender_exception_propagates(ntfy_env):
    """notify() does not swallow send failures -- the CALLER decides whether a
    failed ping may fail its own write."""
    sender = FakeSender(exc=RuntimeError("connection refused"))
    with pytest.raises(RuntimeError, match="connection refused"):
        notify(TriggerType.HALTED_QUESTION, epic="af-fleet", detail="halted", sender=sender)


def test_notify_returns_true_when_the_sender_returns(ntfy_env):
    """True means the sender returned without raising - with the default sender, that
    ntfy accepted the POST. It is not a claim that a human read it."""
    sender = FakeSender()
    assert notify(
        TriggerType.HALTED_QUESTION, epic="af-fleet", detail="halted", sender=sender
    ) is True
    assert len(sender.calls) == 1


def test_default_sender_http_error_propagates(ntfy_env, monkeypatch):
    """An HTTP error from ntfy is a raise, not a False: urlopen raises on a 4xx/5xx, and
    a raising sender still raises out of notify()."""
    from urllib.error import HTTPError

    def _refuse(req, timeout):
        raise HTTPError(req.full_url, 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(ntfy_mod, "urlopen", _refuse)
    with pytest.raises(HTTPError):
        notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done")


@pytest.fixture
def real_notify(monkeypatch):
    """Put the real ``notify`` back where ``notify_once`` looks it up.

    ``notify_once`` calls ``notify`` through this module's globals, and in a full-suite run
    those globals are not ours: test_missed_wake_alarm.py loads test_registry as a pytest
    plugin, which makes its autouse ``pings`` fixture (a stub that returns None) apply to
    EVERY test in the session, this file's included. Under that stub every send reads as
    "nothing sent", so a record-on-True test fails and a record-nothing test passes for the
    wrong reason. ``notify`` here is the name bound at import, before any fixture ran. A
    requested fixture is set up after every autouse one, so this patch is the one in force.
    """
    monkeypatch.setattr(ntfy_mod, "notify", notify)


def _once(state, sender):
    return notify_once(
        state,
        "HALTED_QUESTION:af-fleet",
        TriggerType.HALTED_QUESTION,
        epic="af-fleet",
        detail="halted",
        sender=sender,
    )


def test_notify_once_does_not_record_a_send_that_went_nowhere(monkeypatch, real_notify):
    """With no destination in the environment notify() returns False, so the key is
    NOT recorded and the next call is attempted again rather than suppressed for the
    whole dedup window."""
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    state = types.SimpleNamespace(notifications_sent={})
    sender = FakeSender()
    assert _once(state, sender) is True
    assert state.notifications_sent == {}
    assert _once(state, sender) is True, "an unrecorded key must not suppress the retry"
    assert sender.calls == []


def test_notify_once_passes_the_sender_through_and_dedups_a_real_send(ntfy_env, real_notify):
    state = types.SimpleNamespace(notifications_sent={})
    sender = FakeSender()
    assert _once(state, sender) is True
    assert "HALTED_QUESTION:af-fleet" in state.notifications_sent
    assert _once(state, sender) is False
    assert len(sender.calls) == 1, "the second call is suppressed, so the sender ran once"


def test_notify_once_does_not_record_when_kind_is_none(monkeypatch, ntfy_env, real_notify):
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver(kind="none"))
    state = types.SimpleNamespace(notifications_sent={})
    assert _once(state, FakeSender()) is True
    assert state.notifications_sent == {}


def test_notify_once_does_not_record_when_the_sender_raises(ntfy_env, real_notify):
    state = types.SimpleNamespace(notifications_sent={})
    with pytest.raises(RuntimeError):
        _once(state, FakeSender(exc=RuntimeError("connection refused")))
    assert state.notifications_sent == {}


# --------------------------------------------------------------------------- #
# env-unset skip (today's documented behavior)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "present",
    [{}, {"NTFY_URL": URL}, {"NTFY_TOPIC": TOPIC}],
    ids=["neither", "url-only", "topic-only"],
)
def test_env_unset_skips_and_sender_is_not_called(monkeypatch, present):
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    for key, value in present.items():
        monkeypatch.setenv(key, value)
    sender = FakeSender()
    assert notify(
        TriggerType.HALTED_QUESTION, epic="af-fleet", detail="halted", sender=sender
    ) is False
    assert sender.calls == []


def test_empty_env_values_count_as_unset(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "")
    monkeypatch.setenv("NTFY_TOPIC", "")
    sender = FakeSender()
    notify(TriggerType.HALTED_QUESTION, epic="af-fleet", detail="halted", sender=sender)
    assert sender.calls == []


# --------------------------------------------------------------------------- #
# (c) ASCII-only: REJECT LOUDLY, with to_ascii() as the opt-in fold
# --------------------------------------------------------------------------- #
EM_DASH = "—"
CURLY = "’"


def test_non_ascii_detail_is_rejected_loudly(ntfy_env):
    sender = FakeSender()
    with pytest.raises(NtfyAsciiError, match="detail"):
        notify(
            TriggerType.HALTED_QUESTION,
            epic="af-fleet",
            detail=f"halted {EM_DASH} open dev-manager",
            sender=sender,
        )
    assert sender.calls == []


def test_non_ascii_epic_is_rejected_loudly(ntfy_env):
    """The title rides in an HTTP header, where a multibyte glyph is a 400 at best."""
    sender = FakeSender()
    with pytest.raises(NtfyAsciiError, match="epic"):
        notify(
            TriggerType.HALTED_QUESTION,
            epic=f"af{CURLY}fleet",
            detail="halted",
            sender=sender,
        )
    assert sender.calls == []


def test_ascii_rejection_precedes_the_env_check(monkeypatch):
    """A non-ASCII body must be loud on a dev box with no ntfy env, not only in prod."""
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    with pytest.raises(NtfyAsciiError):
        notify(TriggerType.EPIC_DONE, epic="af-fleet", detail=f"done {EM_DASH} ok")


def test_to_ascii_folds_typography_and_is_accepted_by_notify(ntfy_env):
    folded = to_ascii(f"halted {EM_DASH} don{CURLY}t wait…")
    assert folded == "halted - don't wait..."
    assert folded.isascii()
    sender = FakeSender()
    notify(TriggerType.HALTED_QUESTION, epic="af-fleet", detail=folded, sender=sender)
    assert sender.calls[0][2] == folded.encode("ascii")


def test_to_ascii_drops_what_it_cannot_fold():
    assert to_ascii("café 你好").isascii()
    assert "cafe" in to_ascii("café")


def test_assert_ascii_names_the_field_and_the_offending_character():
    with pytest.raises(NtfyAsciiError) as exc:
        assert_ascii(f"a{EM_DASH}b", field="detail")
    message = str(exc.value)
    assert "detail" in message
    assert "U+2014" in message


def test_assert_ascii_passes_plain_text_through():
    assert assert_ascii("plain ascii", field="detail") == "plain ascii"


# --------------------------------------------------------------------------- #
# backward compatibility: the pre-existing signature + title shapes
# --------------------------------------------------------------------------- #
def test_old_signature_still_works_with_task_progress(ntfy_env):
    sender = FakeSender()
    notify(
        TriggerType.PHASE_COMPLETE,
        epic="af-fleet",
        detail="phase build complete",
        task=2,
        task_total=5,
        sender=sender,
    )
    _, headers, body = sender.calls[0]
    assert headers["Title"] == "Task 2 of 5 - af-fleet"
    assert headers["Priority"] == _PRIORITY[TriggerType.PHASE_COMPLETE]
    assert body == b"phase build complete"


def test_old_signature_still_works_with_task_only(ntfy_env):
    sender = FakeSender()
    notify(
        TriggerType.ESCALATION, epic="af-fleet", detail="stuck", task=3, sender=sender
    )
    assert sender.calls[0][1]["Title"] == "Task 3 - af-fleet"


def test_positional_trigger_and_keyword_only_rest(ntfy_env):
    """The old call shape: trigger positional, everything else keyword-only."""
    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="shipped", sender=sender)
    assert len(sender.calls) == 1
    with pytest.raises(TypeError):
        notify(TriggerType.EPIC_DONE, "af-fleet", "shipped")


# --------------------------------------------------------------------------- #
# gh#133 — notify.url_env / notify.topic_env / notify.kind are HONOURED
#
# Before this, all three were decorative: the schema required the two names, stored
# them and printed them, while notify() read the literals NTFY_URL/NTFY_TOPIC and
# consulted `kind` nowhere. An operator who renamed the variables got silence.
#
# Most cells below drive the resolver directly, because the behaviour under test is
# "what notify() does with a resolved config", not "how config.py parses YAML" (which
# test_config.py already owns). Three tests at the end go through the REAL loader and
# a real environment.md, so the wiring between the two modules is proved end to end
# and not just against a stub of itself.
# --------------------------------------------------------------------------- #
ALT_URL_ENV = "AF_ALT_BASE"
ALT_TOPIC_ENV = "AF_ALT_CHANNEL"
ALT_URL = "https://alt-host.invalid"
ALT_TOPIC = "alt-channel-name"


def _resolver(
    kind="ntfy",
    url_env=ALT_URL_ENV,
    topic_env=ALT_TOPIC_ENV,
    ascii_only=True,
    config_error=None,
):
    """Build a stand-in for ntfy._resolved_notify returning a fixed resolution."""
    resolved = _ResolvedNotify(kind, url_env, topic_env, ascii_only, config_error)
    return lambda: resolved


@pytest.fixture
def alt_env(monkeypatch):
    """Only the ALTERNATIVE names are exported; the historical ones are removed."""
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.setenv(ALT_URL_ENV, ALT_URL)
    monkeypatch.setenv(ALT_TOPIC_ENV, ALT_TOPIC)


def test_the_default_name_constants_are_the_historical_literals():
    """One definition each. Three copies of a default is why f10226f's first mutation
    survived, so the constants are pinned rather than re-spelled at each reader."""
    assert _DEFAULT_URL_ENV == "NTFY_URL"
    assert _DEFAULT_TOPIC_ENV == "NTFY_TOPIC"


def test_the_configured_url_env_and_topic_env_are_the_ones_consulted(
    monkeypatch, alt_env
):
    """THE FILED BUG. Config names two variables; the send must read those two."""
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver())
    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)
    assert len(sender.calls) == 1, "a configured, fully-exported channel must send"
    assert sender.calls[0][0] == f"{ALT_URL}/{ALT_TOPIC}"


def test_the_hardcoded_names_are_not_consulted_when_config_names_others(
    monkeypatch, alt_env
):
    """Kills the 'read both, prefer either' mutation: with BOTH pairs exported and
    pointing somewhere different, only the configured pair may be used."""
    monkeypatch.setenv("NTFY_URL", URL)
    monkeypatch.setenv("NTFY_TOPIC", TOPIC)
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver())
    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)
    assert sender.calls[0][0] == f"{ALT_URL}/{ALT_TOPIC}"
    assert URL not in sender.calls[0][0] and TOPIC not in sender.calls[0][0]


def test_kind_none_sends_nothing_even_with_the_env_set(monkeypatch, ntfy_env):
    """The third decorative field, and it points the OPPOSITE way to the other two:
    an unwanted send rather than a dropped one."""
    monkeypatch.setattr(
        ntfy_mod,
        "_resolved_notify",
        _resolver(kind="none", url_env="NTFY_URL", topic_env="NTFY_TOPIC"),
    )
    sender = FakeSender()
    assert notify(
        TriggerType.HALTED_QUESTION, epic="af-fleet", detail="halted", sender=sender
    ) is False
    assert sender.calls == []


def test_kind_none_is_silent(monkeypatch, ntfy_env, capsys):
    """No warning: the operator declared the channel off, and nagging about a state
    somebody chose is how a human learns to stop reading stderr."""
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver(kind="none"))
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=FakeSender())
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


@pytest.mark.parametrize("field", ["epic", "detail"])
def test_ascii_rejection_precedes_the_kind_check(monkeypatch, ntfy_env, field):
    """Sibling of test_ascii_rejection_precedes_the_env_check, and unpinned until now:
    hoisting the `kind: none` return above the two assert_ascii calls left the whole
    suite green while notify() with a non-ASCII body under a switched-off channel
    returned silently instead of raising. A bad body must be loud on a box with
    notifications OFF - that is the cheapest place to find it, and the reason the
    shipped ordering puts both gates above the kind check.

    Both fields, because a return hoisted BETWEEN the two calls is a mutant of its own.
    """
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver(kind="none"))
    fields = {"epic": "af-fleet", "detail": "done"}
    fields[field] = f"caf{CURLY}{fields[field]}"
    sender = FakeSender()
    with pytest.raises(NtfyAsciiError, match=field):
        notify(TriggerType.EPIC_DONE, sender=sender, **fields)
    assert sender.calls == []


def test_a_missing_configured_name_is_loud_and_names_it(monkeypatch, capsys):
    """POST-RESOLUTION CONTRACT ONLY - it stubs the resolver, so it pins what notify()
    does with an already-resolved config and CANNOT pin what a config on disk resolves
    to. kind: ntfy means the operator believes the channel works, so a dropped send must
    say so, naming the CONFIGURED variable rather than the built-in default.

    The disk-to-stderr half is
    test_a_real_environment_md_naming_an_unexported_var_says_it_substituted, which
    drives the real loader; this test passing is not evidence that one does."""
    monkeypatch.delenv(ALT_URL_ENV, raising=False)
    monkeypatch.setenv(ALT_TOPIC_ENV, ALT_TOPIC)
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver())
    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)
    assert sender.calls == []
    err = capsys.readouterr().err
    assert ALT_URL_ENV in err
    assert "notify.url_env" in err
    assert "unset" in err
    assert ALT_TOPIC_ENV not in err, "the variable that IS set is not the problem"
    assert "NTFY_URL" not in err, "naming the default sends the operator to the wrong var"


def test_the_warning_distinguishes_set_but_empty_from_unset(monkeypatch, capsys):
    """config.py's _require_env_name is a MEMBERSHIP test, so an exported-but-empty
    variable passes config validation clean and then drops here. It is the one state
    the operator cannot diagnose from a clean `config show`, so it gets its own words."""
    monkeypatch.setenv(ALT_URL_ENV, "")
    monkeypatch.setenv(ALT_TOPIC_ENV, ALT_TOPIC)
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver())
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=FakeSender())
    assert "set but empty" in capsys.readouterr().err


def test_the_warning_never_prints_a_secret_value(monkeypatch, capsys):
    """Names only. config.py reads env vars by name for exactly this reason."""
    secret = "https://s3cr3t-host.invalid/hook/PRIVATE-TOKEN"
    monkeypatch.setenv(ALT_URL_ENV, secret)
    monkeypatch.delenv(ALT_TOPIC_ENV, raising=False)
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver())
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=FakeSender())
    err = capsys.readouterr().err
    assert secret not in err
    assert "PRIVATE-TOKEN" not in err
    assert ALT_TOPIC_ENV in err


def test_unresolvable_config_falls_back_to_the_historical_names(monkeypatch, ntfy_env):
    """A config that will not parse must not silence the fleet's only halt channel -
    an unreadable config is the incident that makes the ping MORE valuable. Falls back
    to the literals notify() always used, so no working install changes behaviour."""

    def _boom():
        raise RuntimeError("environment.md is not parseable")

    monkeypatch.setattr("agentflow.config.load_environment", _boom)
    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)
    assert sender.calls[0][0] == f"{URL}/{TOPIC}"


def test_a_config_failure_never_escapes_notify(monkeypatch, capsys):
    """notify() runs on the side-effect path of callers that have ALREADY written a
    durable row (registry.py's halted ping). A config-shaped exception escaping here
    would turn a notification bug into a write-path bug. OSError, not ConfigError:
    load_environment touches Path.home() and read_text, so a narrow catch would be
    the new escape hatch."""

    def _boom():
        raise OSError("config home is unreadable")

    monkeypatch.setattr("agentflow.config.load_environment", _boom)
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert notify(
        TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=FakeSender()
    ) is False
    err = capsys.readouterr().err
    assert "OSError" in err, "a swallowed failure is reported where it cost a send"
    assert "NTFY_URL" in err


def test_the_config_is_resolved_once_per_send(monkeypatch, alt_env):
    """load_environment() is uncached and re-reads the file from disk, and the old
    shape reached it three times per send through assert_ascii. Pinned so a later
    refactor cannot silently restore per-field reads."""
    calls = []

    def counting():
        calls.append(1)
        return _ResolvedNotify("ntfy", ALT_URL_ENV, ALT_TOPIC_ENV, True, None)

    monkeypatch.setattr(ntfy_mod, "_resolved_notify", counting)
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=FakeSender())
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# End to end through the REAL config loader -- a real environment.md on disk.
# Everything above stubs the resolver; these prove the two modules are actually wired
# to each other, which is the thing gh#133 says they were not.
# --------------------------------------------------------------------------- #
def _write_config(home, *, kind: str, names: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    d = str(home).replace("\\", "/")
    env_names = (
        f"  url_env: {ALT_URL_ENV}\n  topic_env: {ALT_TOPIC_ENV}\n" if names else ""
    )
    fence = "```"
    (home / "environment.md").write_text(
        f"# environment\n\n{fence}yaml\n"
        "schema_version: 1\n"
        "person:\n  name: Sample User\n"
        f"data_root: {d}\n"
        "git_host:\n  kind: local\n"
        f"notify:\n  kind: {kind}\n{env_names}"
        f"lock_dir: {d}\n"
        "shell: posix\n"
        f"suite_root: {d}\n"
        f"worktree_root: {d}\n"
        f"{fence}\n",
        encoding="utf-8",
    )


def test_a_real_environment_md_url_env_reaches_the_send(tmp_path, monkeypatch, alt_env):
    """No stubbed resolver anywhere: a real environment.md naming two non-default
    variables, parsed by the real config loader, must decide where the POST goes."""
    home = tmp_path / "cfg-honoured"
    _write_config(home, kind="ntfy", names=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)
    assert sender.calls[0][0] == f"{ALT_URL}/{ALT_TOPIC}"


def test_a_real_environment_md_kind_none_suppresses_the_send(
    tmp_path, monkeypatch, ntfy_env
):
    """Same loader, same exported NTFY_URL/NTFY_TOPIC as the passing send tests above.
    Only `kind` differs, and it alone decides."""
    home = tmp_path / "cfg-off"
    _write_config(home, kind="none", names=False)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)
    assert sender.calls == []


def test_a_real_environment_md_naming_an_unexported_var_says_it_substituted(
    tmp_path, monkeypatch, ntfy_env, capsys
):
    """THE PATH A REAL CONFIG ACTUALLY TAKES, and the one the stubbed tests above
    cannot reach.

    config.py's _require_env_name (config.py:468-474) RAISES when a configured name is
    absent from os.environ - it is not a reporting check. So "operator renamed the
    variables and has not exported them yet" does not arrive as a resolved config with
    those names; it arrives as a ConfigError, and _resolved_notify substitutes
    NTFY_URL/NTFY_TOPIC. With the old names still exported from before the rename, the
    ping is DELIVERED - to the previous destination - and before this fix nothing was
    printed at all. Silence there is worse than the silence gh#133 filed: it sends
    somewhere wrong rather than nowhere, and looks like a working channel.

    The send is deliberately kept (see _resolved_notify: a YAML error must not silence a
    fleet's only halt channel). Only the silence is the defect, so this asserts BOTH -
    it still sends, and it says where and why.
    """
    home = tmp_path / "cfg-named-but-unexported"
    _write_config(home, kind="ntfy", names=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.delenv(ALT_URL_ENV, raising=False)
    monkeypatch.delenv(ALT_TOPIC_ENV, raising=False)

    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)

    assert sender.calls[0][0] == f"{URL}/{TOPIC}", "the permissive fallback still sends"
    err = capsys.readouterr().err
    assert err.strip(), "a send through names the operator did not choose cannot be mute"
    assert _DEFAULT_URL_ENV in err and _DEFAULT_TOPIC_ENV in err
    assert "ConfigError" in err, "which fallback fired decides which fix is needed"
    assert URL not in err and TOPIC not in err, "names only, never values"


def test_a_real_resolved_config_prints_nothing_on_a_send(
    tmp_path, monkeypatch, alt_env, capsys
):
    """The other side of the line, and the reason the warning above sits on the send
    path rather than in _resolved_notify: a correctly configured box has config_error
    None, so a healthy fleet must not accumulate one stderr line per ping."""
    home = tmp_path / "cfg-healthy"
    _write_config(home, kind="ntfy", names=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)
    assert sender.calls[0][0] == f"{ALT_URL}/{ALT_TOPIC}"
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out == ""


def test_the_substitution_disclosure_survives_a_sender_that_raises(
    tmp_path, monkeypatch, ntfy_env, capsys
):
    """THE ORDERING THE COMMENT PROMISES, now enforced rather than described (gh#227).

    `notify()` emits the substitution warning BEFORE calling the sender, with a comment
    saying a sender that raises must not take the disclosure with it. Nothing tested
    that. Moving the warn below the send left the whole module green — 48 passed — so
    the comment was a claim, not a guarantee, which is the exact shape gh#133 closed for
    the kind/ASCII ordering.

    The combination is what makes it a gate: the two disclosure tests above use a
    non-raising sender, and the raising-sender test above uses a healthy config. Only a
    substituted-name config AND a raising sender can tell the two orderings apart.

    Why it matters beyond tidiness: the operator here is being told the ping went
    somewhere they did not choose. If the send then fails, that is when they most need
    the line — the traceback names the substituted destination and nothing else explains
    why the fleet is pinging it.
    """
    home = tmp_path / "cfg-named-but-unexported"
    _write_config(home, kind="ntfy", names=True)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.delenv(ALT_URL_ENV, raising=False)
    monkeypatch.delenv(ALT_TOPIC_ENV, raising=False)

    sender = FakeSender(exc=RuntimeError("host unreachable"))
    with pytest.raises(RuntimeError):
        notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)

    err = capsys.readouterr().err
    assert err.strip(), (
        "the sender raised and took the disclosure with it - the warning is now "
        "below the send, and an operator learns nothing about the substitution"
    )
    assert _DEFAULT_URL_ENV in err and _DEFAULT_TOPIC_ENV in err
    assert URL not in err and TOPIC not in err, "names only, never values"


def test_a_whitespace_only_value_is_set_but_empty_not_a_url(monkeypatch, capsys):
    """"   " is truthy. Before _env_value it built "   /topic", sailed past the env
    check and reached the sender, where the real one raises ValueError out of notify() -
    an escape route around the only code that can explain the problem. It belongs in the
    set-but-empty branch with "", which is where a stray line continuation in a .env
    actually leaves you."""
    monkeypatch.setenv(ALT_URL_ENV, "   ")
    monkeypatch.setenv(ALT_TOPIC_ENV, ALT_TOPIC)
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver())
    sender = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sender)
    assert sender.calls == [], "a URL made of spaces is not a destination"
    err = capsys.readouterr().err
    assert "set but empty" in err and ALT_URL_ENV in err


def test_control_the_fix_neither_always_sends_nor_always_refuses(
    tmp_path, monkeypatch, alt_env
):
    """CONTROL. A fix that hardcoded `return` would pass every drop test above; one
    that ignored config entirely would pass every send test. All three directions in
    one body, so neither degenerate implementation survives.
    """
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver())
    sends = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=sends)
    assert len(sends.calls) == 1, "configured + exported must SEND"

    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver(kind="none"))
    off = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=off)
    assert off.calls == [], "kind: none must NOT send"

    monkeypatch.delenv(ALT_URL_ENV, raising=False)
    monkeypatch.setattr(ntfy_mod, "_resolved_notify", _resolver())
    unset = FakeSender()
    notify(TriggerType.EPIC_DONE, epic="af-fleet", detail="done", sender=unset)
    assert unset.calls == [], "a named-but-unset variable must NOT send"
