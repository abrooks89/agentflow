"""Pin commands/dev-manager.md to the one-mode seat (gh#645).

skills/dev-manager/SKILL.md section 10 says the seat schedules nothing and has no unattended
mode; the unattended pump is the steward's. The slash-command wrapper used to tell a session to
follow a self-wake continuation, so a session entering through /dev-manager was instructed to do
what the skill forbids. These tests pin the text of the wrapper, not the behaviour of a session.
"""

from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[3] / "commands" / "dev-manager.md"


def _text() -> str:
    return WRAPPER.read_text(encoding="utf-8")


def test_wrapper_names_no_self_wake_continuation():
    text = _text()
    assert "ScheduleWakeup" not in text
    assert "self-paced continuation" not in text
    assert "/dev-manager --project <name> --unattended" not in text


def test_wrapper_does_not_offer_unattended_as_a_mode():
    text = _text()
    assert "no unattended mode" in text
    # The flag may appear only where the wrapper says the skill ignores it; any other mention
    # (a mode, a continuation, a reworded comeback) is the defect this file guards against.
    paragraphs = [p for p in text.split("\n\n") if "--unattended" in p]
    assert len(paragraphs) <= 1, paragraphs
    for p in paragraphs:
        assert "ignores the flag" in p, p


def test_wrapper_still_forwards_arguments_verbatim():
    text = _text()
    assert 'Skill(dev-manager, args="$ARGUMENTS")' in text
    assert "--project <name>" in text
