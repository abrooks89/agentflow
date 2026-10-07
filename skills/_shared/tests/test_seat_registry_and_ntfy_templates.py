"""Seat-skill command and notification templates must be ones the CLI and the notify path accept.

Three defects of one shape: a seat skill prescribed a literal a lane copies and runs, and the code
on the other end refuses it.

* gh#939 - the mid-pass phase-boundary refresh (`status --actor <seat> ... --phase ... --eta ...`)
  carried no `--state`. `_status_argv_checks` refuses every non-`--heartbeat-only` status write
  without one (`agent_state_enum`, exit 2), so the documented boundary refresh never landed and a
  lane following its own skill went quiet at every phase boundary. The live tests below extract
  each seat's boundary template from its SKILL.md and RUN it against a scratch board.
* gh#940 - the prescribed ntfy lines used a middle dot and an em dash. `agentflow.ntfy` rejects a
  non-ASCII payload under `notify.ascii_only` (`assert_ascii`), and the seats' own prose says ntfy
  400s on multibyte bodies. Every prescribed ntfy template in the seat skills is extracted and fed
  to the real `assert_ascii`.
* gh#941 - every seat read any `validate` exit 2 as corruption, but an ABSENT board also exits 2
  (`refused [file_missing]`), so the `init` remedy each skill documents was unreachable from the
  gate that meets the absent board first. The gate must branch on the refusal key, and the
  config/argument keys (`project_unconfigured`, `registry_board_conflict`, `bad_project`) are
  neither absent nor corrupt: fixed by hand, never `init`, never routed to the steward.

The corpus is a fixed list of files, still taken through the git index (`only_tracked`) so an
untracked stray can never be what a gate reads.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentflow.ntfy import NtfyAsciiError, assert_ascii
from agentflow.registry import AGENTS, BUILDERS, RULES, main, parse_registry
from agentflow.tracked_paths import only_tracked

SKILLS_ROOT = Path(__file__).resolve().parents[2]
SUITE_ROOT = SKILLS_ROOT.parent
REGISTRY_DOC = SUITE_ROOT / "docs" / "REGISTRY.md"

#: Every fleet seat, from the product's own roster rather than a hand list.
SEATS = AGENTS
#: The seats whose firing contract carries a mid-pass phase-boundary refresh. dev-manager has no
#: such boundary: its status write is the section-named `pumping|triaging|...` refresh.
BOUNDARY_SEATS = tuple(s for s in SEATS if s != "dev-manager")
#: The seats that run the harness preflight (dev-manager does not self-wake).
PREFLIGHT_SEATS = BOUNDARY_SEATS


def _tracked(path: Path) -> Path:
    kept = only_tracked(SUITE_ROOT, [path])
    assert kept, f"{path} is not in the git index - the gate would be reading an untracked file"
    return kept[0]


def _seat_path(seat: str) -> Path:
    return _tracked(SKILLS_ROOT / seat / "SKILL.md")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _inline_spans(text: str) -> list[tuple[int, str]]:
    """(offset, content) of every inline code span, fenced blocks blanked out first."""
    blanked = re.sub(r"```.*?```", lambda m: " " * len(m.group(0)), text, flags=re.S)
    return [(m.start(), m.group(1)) for m in re.finditer(r"`([^`]+)`", blanked)]


def _fenced_blocks(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(1)) for m in re.finditer(r"```[^\n]*\n(.*?)```", text, flags=re.S)]


# --------------------------------------------------------------------------- #
# gh#939 - every status template carries --state (or is --heartbeat-only)
# --------------------------------------------------------------------------- #
_STATUS_CALL = re.compile(r"(?:^|[\s.])status (?:--actor\b|\.\.\.)")


def status_templates(text: str) -> list[tuple[int, str]]:
    """Every documented `status` write: a code span or fenced command that runs `status --actor`.

    A span that only NAMES the flag (`status --eta`, "send one full `status --eta` write") is a
    reference, not a template, and is not collected. `status ...` (an elided actor) is a template.
    A fenced block may hold several commands; each `python -m agentflow...` starts a new one.
    """
    out = []
    pieces = list(_inline_spans(text))
    for at, block in _fenced_blocks(text):
        flat = _flat(block.replace("\\\n", " "))
        pieces.extend((at, part) for part in re.split(r"(?=python -m agentflow)", flat))
    for at, span in pieces:
        flat = _flat(span)
        if _STATUS_CALL.search(flat):
            out.append((text[:at].count("\n") + 1, flat))
    return out


def lacks_state(template: str) -> bool:
    return "--state" not in template and "--heartbeat-only" not in template


@pytest.mark.parametrize("seat", SEATS)
def test_every_status_template_in_a_seat_skill_carries_state(seat: str) -> None:
    text = _read(_seat_path(seat))
    found = status_templates(text)
    assert found, f"{seat}/SKILL.md: no status template found - the extractor is reading nothing"
    bad = [(line, t) for line, t in found if lacks_state(t)]
    assert not bad, (
        f"{seat}/SKILL.md prescribes status writes the CLI refuses (`agent_state_enum`, exit 2): "
        f"a full status write needs --state, a liveness-only one --heartbeat-only: {bad}"
    )


def test_every_status_template_in_the_registry_doc_carries_state() -> None:
    text = _read(_tracked(REGISTRY_DOC))
    found = status_templates(text)
    assert len(found) >= 3, f"docs/REGISTRY.md: only {len(found)} status templates found"
    bad = [(line, t) for line, t in found if lacks_state(t)]
    assert not bad, f"docs/REGISTRY.md prescribes status writes the CLI refuses: {bad}"


def test_the_template_extractor_sees_the_defect_shape() -> None:
    """Red-proof on synthetic input: the pre-fix boundary spelling is caught, references are not."""
    old = "2. **THEN refresh** (`status --actor jr-dev-1 --project <name> --phase\n   <x> --eta <ISO>`)"
    assert [t for _, t in status_templates(old) if lacks_state(t)], "the pre-fix form must be flagged"
    assert not status_templates("send one full `status --eta` write"), "a flag reference is not a template"
    assert not [t for _, t in status_templates("`status --actor a --heartbeat-only`") if lacks_state(t)]


_BOUNDARY = re.compile(r"\*\*THEN refresh\*\* your `## Agents` row \(`(status --actor[^`]+)`\)")


def boundary_template(text: str, where: str) -> str:
    hits = _BOUNDARY.findall(text)
    assert len(hits) == 1, f"{where}: expected one phase-boundary refresh template, found {len(hits)}"
    return _flat(hits[0])


def _boundary_cases() -> list:
    cases = [pytest.param(seat, _seat_path(seat), id=seat) for seat in BOUNDARY_SEATS]
    # The generic firing contract, run as the primary builder.
    cases.append(pytest.param("lead-dev", _tracked(REGISTRY_DOC), id="REGISTRY.md"))
    return cases


@pytest.fixture
def board(tmp_path: Path):
    reg = tmp_path / "projects" / "demo" / "status" / "registry.md"
    lock = tmp_path / "locks"
    lock.mkdir()
    assert main(["init", "--file", str(reg), "--lock-dir", str(lock), "--project", "demo"]) == 0
    return reg, lock


def _eta() -> str:
    return (datetime.now(UTC) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%MZ")


def render(template: str, actor: str) -> list[str]:
    """Fill a documented template's placeholders the way a lane would, then split it as a shell does."""
    filled = template
    for placeholder, value in (
        ("E-NNNN-<name>", "E-0042-demo"),
        ("<ISO>", _eta()),
        ("<you>", actor),
        ("<name>", "demo"),
        ("<what is running>", "seat-template-probe"),
        (" <marker>", ""),
    ):
        filled = filled.replace(placeholder, value)
    filled = re.sub(r"\[(--work [^\]]+)\]", r"\1", filled)
    assert "<" not in filled and "[" not in filled, f"unfilled placeholder left in {filled!r}"
    argv = shlex.split(filled)
    if argv[:3] == ["python", "-m", "agentflow.registry"]:
        argv = argv[3:]
    return argv


@pytest.mark.parametrize(("actor", "path"), _boundary_cases())
def test_each_seats_boundary_refresh_lands_on_a_scratch_board(actor, path, board, capsys) -> None:
    """The documented boundary refresh, copied out of the skill and run - exit 0, phase recorded."""
    reg, lock = board
    template = boundary_template(_read(path), f"{path.parent.name}/{path.name}")
    argv = render(template, actor)
    assert argv[0] == "status" and argv[argv.index("--actor") + 1] == actor, argv
    rc = main([*argv, "--file", str(reg), "--lock-dir", str(lock), "--timeout", "5"])
    err = capsys.readouterr().err
    assert rc == 0, f"{path}: the documented boundary refresh {template!r} was refused: {err}"
    row = parse_registry(reg.read_text(encoding="utf-8")).agents[actor]
    assert row.status == "building", row
    assert row.phase.strip() == "seat-template-probe", row
    if actor in BUILDERS:
        # A builder's boundary refresh mid-build must re-send its claim: without `--work` the
        # full write blanks the work cell to `-`, and the row stops saying what is being built.
        assert "--work" in argv, f"{path}: builder {actor}'s boundary refresh omits --work: {template!r}"
        assert row.work.strip() == "E-0042-demo", f"the boundary refresh blanked the work cell: {row}"


def test_a_boundary_refresh_without_state_is_refused(board, capsys) -> None:
    """Pins the refusal the skills used to walk into, so the static gate above guards a real rule."""
    reg, lock = board
    rc = main(["status", "--actor", "auditor", "--project", "demo", "--phase", "auditing",
               "--eta", _eta(), "--file", str(reg), "--lock-dir", str(lock), "--timeout", "5"])
    err = capsys.readouterr().err
    assert rc == 2 and "refused [agent_state_enum]" in err, (rc, err)


# --------------------------------------------------------------------------- #
# gh#940 - every prescribed ntfy template is ASCII
# --------------------------------------------------------------------------- #
_NTFY_WORD = re.compile(r"\bntfy\b|notif|banner", re.I)
#: A notification line leads with a seat or project prefix (each skill's own "Ntfy prefix" rule).
#: No `\b` before the space: `<name>` ends in `>`, and `>` + space is not a word boundary.
_NTFY_PREFIX = re.compile(r"^(?:<name>|<project>|agentflow|" + "|".join(map(re.escape, SEATS)) + r") \S")
#: A paragraph is a blank-line block or one list item / numbered step.
_PARA_SPLIT = re.compile(r"\n\s*\n|\n(?=\s*(?:[-*]|\d+\.) )")


def ntfy_templates(text: str) -> list[str]:
    """Every inline code span that is a prescribed ntfy line.

    Prescribed = the span leads with a seat/project prefix AND sits in a paragraph (a blank-line
    block, or one list item) that talks about ntfy / notifications / the banner. Both halves are
    needed: the prefix alone also matches registry Notes (`<lane> -> steward: ...`), and the
    paragraph alone matches every command span in a notify section.
    """
    blanked = re.sub(r"```.*?```", lambda m: " " * len(m.group(0)), text, flags=re.S)
    out = []
    for para in _PARA_SPLIT.split(blanked):
        if not _NTFY_WORD.search(para):
            continue
        for span in re.findall(r"`([^`]+)`", para):
            flat = _flat(span)
            if _NTFY_PREFIX.match(flat) and " --actor" not in flat:
                out.append(flat)
    return out


@pytest.mark.parametrize("seat", SEATS)
def test_every_prescribed_ntfy_template_is_ascii(seat: str) -> None:
    templates = ntfy_templates(_read(_seat_path(seat)))
    assert templates, f"{seat}/SKILL.md: no ntfy template extracted - the extractor reads nothing"
    refused = []
    for t in templates:
        try:
            assert_ascii(t, field="detail", ascii_only=True)
        except NtfyAsciiError as exc:
            refused.append((t, str(exc)))
    assert not refused, f"{seat}/SKILL.md prescribes ntfy lines the notify path refuses: {refused}"


@pytest.mark.parametrize("seat", PREFLIGHT_SEATS)
def test_the_harness_preflight_ntfy_line_is_extracted_and_ascii(seat: str) -> None:
    """The one line every self-waking seat sends when it cannot run at all - pinned by name."""
    templates = ntfy_templates(_read(_seat_path(seat)))
    want = f"{seat} - harness preflight FAILED - <missing tool(s)>"
    assert want in templates, f"{seat}/SKILL.md: {want!r} not among its ntfy templates {templates}"


def test_the_ntfy_extractor_sees_the_defect_shape() -> None:
    """Red-proof on synthetic input: the pre-fix spelling is extracted and refused."""
    old = "- **Exit 1.** Ntfy the remedy (`steward · harness preflight FAILED — <x>`)."
    got = ntfy_templates(old)
    assert got == ["steward · harness preflight FAILED — <x>"], got
    with pytest.raises(NtfyAsciiError):
        assert_ascii(got[0], field="detail", ascii_only=True)
    assert not ntfy_templates("A Note: `steward -> fleet: x`, unrelated to anything sent.")


# --------------------------------------------------------------------------- #
# gh#941 - the validate gate branches on the refusal key; file_missing -> init
# --------------------------------------------------------------------------- #
_VALIDATE = "agentflow.registry validate --project"
#: `validate` exits 2 with these too, and none of them is corruption: a missing project config,
#: `$AGENTFLOW_REGISTRY` naming another board than `--project`, a malformed `--project` slug.
_CONFIG_KEYS = ("project_unconfigured", "registry_board_conflict", "bad_project")
#: Non-steward seats must not route these as corruption; the steward must not `repair` them.
_NOT_STEWARD = re.compile(r"never `init` or (?:route them to the steward|`repair`)")


def validate_gate(text: str) -> str:
    flat = _flat(text)
    at = flat.find(_VALIDATE)
    assert at != -1, "no validate gate"
    return flat[at:at + 900]


def _gate_cases() -> list:
    cases = [pytest.param(_seat_path(seat), id=seat) for seat in SEATS]
    cases.append(pytest.param(_tracked(REGISTRY_DOC), id="REGISTRY.md"))
    return cases


@pytest.mark.parametrize("path", _gate_cases())
def test_the_validate_gate_sends_file_missing_to_init_and_the_rest_to_repair(path: Path) -> None:
    gate = validate_gate(_read(path))
    missing = gate.find("`refused [file_missing]`")
    assert missing != -1, f"{path}: the validate gate never names `refused [file_missing]`: {gate[:500]}"
    init = gate.find("init --project <name>", missing)
    assert init != -1, f"{path}: `file_missing` is not tied to `init --project <name>`: {gate[:500]}"
    other = re.search(r"Any other exit 2.{0,200}corrupt", gate[init:])
    assert other, f"{path}: every other exit-2 key must still be the corruption path: {gate[:600]}"
    assert "`file_unreadable`" in other.group(0) and "`file_damaged`" in other.group(0), (
        f"{path}: the two non-absent file keys must land on the corruption branch: {other.group(0)}"
    )
    # The config/argument branch sits between `init` and the corruption branch: these keys are
    # neither an absent board (init would mint a board nothing reads) nor damage (repair cannot
    # fix a bad slug or an env var naming another board).
    config = gate[init:init + other.start()]
    for key in _CONFIG_KEYS:
        assert f"`{key}`" in config, f"{path}: `{key}` is not named on the config-error branch: {config}"
        assert key not in other.group(0), f"{path}: `{key}` landed on the corruption branch"
    assert "never `init`" in config, f"{path}: the config-error branch does not forbid `init`: {config}"
    assert _NOT_STEWARD.search(config), (
        f"{path}: the config-error branch does not keep these keys away from the steward: {config}"
    )
    assert "$AGENTFLOW_REGISTRY" in config and "`--file`" in config, (
        f"{path}: `registry_board_conflict` is named without its remedy: {config}"
    )


def test_the_config_keys_the_gates_name_are_real_refusals() -> None:
    """A gate naming a key the CLI never prints routes nothing; bind the list to `RULES`."""
    missing = [key for key in _CONFIG_KEYS if key not in RULES]
    assert not missing, f"keys named on the config-error branch that the CLI does not define: {missing}"


def test_a_malformed_project_slug_is_bad_project_not_file_missing(tmp_path, capsys) -> None:
    lock = tmp_path / "locks"
    lock.mkdir()
    rc = main(["validate", "--file", str(tmp_path / "nope.md"), "--lock-dir", str(lock),
               "--project", "../demo"])
    err = capsys.readouterr().err
    assert rc == 2 and "refused [bad_project]" in err and "file_missing" not in err, (rc, err)


def test_validate_on_an_absent_board_prints_the_key_the_gates_branch_on(tmp_path, capsys) -> None:
    lock = tmp_path / "locks"
    lock.mkdir()
    rc = main(["validate", "--file", str(tmp_path / "nope.md"), "--lock-dir", str(lock),
               "--project", "demo"])
    err = capsys.readouterr().err
    assert rc == 2, (rc, err)
    assert "refused [file_missing]" in err, err


def test_validate_on_a_damaged_board_is_not_file_missing(board, capsys) -> None:
    """The other branch: a board that exists but lost a section must NOT read as absent (no init)."""
    reg, lock = board
    text = reg.read_text(encoding="utf-8")
    assert "## Questions" in text, "fixture precondition"
    reg.write_text(text.replace("## Questions", "## Questionz"), encoding="utf-8", newline="")
    rc = main(["validate", "--file", str(reg), "--lock-dir", str(lock), "--project", "demo"])
    err = capsys.readouterr().err
    assert rc == 2, (rc, err)
    assert "refused [file_damaged]" in err and "file_missing" not in err, err
