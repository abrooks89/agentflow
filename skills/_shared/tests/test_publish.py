"""Tests for ``agentflow.publish`` - the share-cut readiness gate and the local-only cut.

Every fixture is a throwaway repo under ``tmp_path``. Every key-shaped string, session-URL-shaped
string and sanitize term is BUILT AT RUNTIME (or supplied through a fixture ``ci.yml`` whose terms
are made up), so this file never carries a literal the gate or the CI sanitize job would flag -
which ``test_the_gate_passes_over_a_cut_of_its_own_checkout`` proves by cutting this very file.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import string
import subprocess
import textwrap
from pathlib import Path

import pytest

from agentflow import publish

NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[3]
NOREPLY = "4242+fixture-bot@users.noreply.github.com"
HOST_REF = "fixture-owner/fixture-repo"
#: What build_cut stamps. A fixture checked directly as a cut carries it, or cut-shape fails.
CUT_IDENT = (publish.AUTHOR_NAME, NOREPLY)
SOURCE_IDENT = ("Fixture Source Person", "source-person@fixture.invalid")

# The fixture sanitize pattern, written the way ci.yml writes its own: split with '' joins.
FIXTURE_CI_YML = textwrap.dedent("""\
    jobs:
      other-job:
        steps:
          - run: pattern='not''this''one'
      sanitize-gate:
        steps:
          - run: |
              pattern='zorb''lax|quux''fred'
      after-job:
        steps: []
    """)


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout.decode("utf-8", "replace").strip()


def _base_files() -> dict[str, bytes]:
    return {
        ".github/workflows/ci.yml": FIXTURE_CI_YML.encode(),
        "docs/runbooks/publish.md": b"# Publish\n\nA human runs this.\n",
        "README.md": b"# Fixture\n\nSee gh#1 and gh#2.\n",
        "LICENSE": b"Fixture license.\n",
    }


@pytest.fixture(autouse=True)
def _cut_identity_global(tmp_path_factory, monkeypatch):
    """The cut identity, from a scratch GLOBAL config. A fixture checked directly as a cut
    must hold only allow-listed keys in its own config, so ``user.*`` cannot live there; a
    global one also keeps the operator's own config (a template dir, a hooks path) out."""
    cfg = tmp_path_factory.mktemp("gitglobal") / "gitconfig"
    cfg.write_text(f"[user]\n\tname = {CUT_IDENT[0]}\n\temail = {CUT_IDENT[1]}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    # A system config can name an init.templateDir whose template writes keys into every
    # fixture repo; the allow-list tests would then fail for reasons of the machine.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _repo(
    tmp_path: Path,
    files: dict[str, bytes],
    *,
    name: str = "src",
    executable: tuple[str, ...] = (),
    allow_empty: bool = False,
    ident: tuple[str, str] = CUT_IDENT,
) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=master")
    if ident != CUT_IDENT:  # a source repo, never checked as a cut
        _git(repo, "config", "user.name", ident[0])
        _git(repo, "config", "user.email", ident[1])
    _git(repo, "config", "core.autocrlf", "false")
    for rel, data in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    if files:
        _git(repo, "add", "-A")
        for rel in executable:
            _git(repo, "update-index", "--chmod=+x", rel)
    args = ["commit", "-q", "-m", "fixture"]
    if allow_empty:
        args.append("--allow-empty")
    _git(repo, *args)
    return repo


def _ledger(tmp_path: Path, dates: list[str], *, name: str = "ledger.md", tail: str = "") -> Path:
    rows = [
        f"| {i} | `FIXTURE_NAME_{i}` | somewhere | {d} | {'me' if d else ''} |"
        for i, d in enumerate(dates, 1)
    ]
    body = NL.join(
        [
            "# Key rotation ledger",
            "",
            "| # | name | where consumed | rotated_on | rotated_by |",
            "|---|---|---|---|---|",
            *rows,
            "",
            "Trailing prose.",
            "",
        ]
    )
    path = tmp_path / name
    path.write_text(body + tail, encoding="utf-8")
    return path


#: A complete waiver. The reason and evidence carry marker words a test asserts never print.
WAIVER_FIELDS = {
    "waived_by": "Owner",
    "waived_on": "2026-09-30",
    "scope": "publish",
    "reason": "FIXTURE_REASON: publishing without rotating.",
    "evidence": "FIXTURE_EVIDENCE: a value-match scan found 0 matches.",
}


def _waiver(heading: str = "## Waiver", **fields: str | None) -> str:
    """A waiver section. A field passed as ``None`` is left out; any other value replaces it."""
    merged = {**WAIVER_FIELDS, **fields}
    bullets = [f"- {k}: {v}".rstrip() for k, v in merged.items() if v is not None]
    return NL.join(["", heading, "", *bullets, ""])


def _check(
    repo: Path, ledger: Path, *extra: str, host_ref: str = HOST_REF, source: Path | None = None
) -> list[str]:
    return [
        "check",
        "--tree",
        str(repo),
        "--ledger",
        str(ledger),
        "--host-ref",
        host_ref,
        "--source",
        str(source or repo),
        *extra,
    ]


def _line(out: str, name: str) -> str:
    """The head line of check ``name``."""
    hits = [
        ln for ln in out.splitlines() if re.match(rf"^(PASS|FAIL|REPORT) {re.escape(name)}\b", ln)
    ]
    assert len(hits) == 1, out
    return hits[0]


def _session_url() -> str:
    return "https://" + "clau" + "de.ai/" + "code/" + "session" + "_01" + secrets.token_hex(8)


def _trailer() -> str:
    return "Claude" + "-Session" + ": " + _session_url()


def _alnum(n: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _upper_alnum(n: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


# name -> (prefix, random part) - built at runtime, never written out whole.
def _planted_keys() -> dict[str, tuple[str, str]]:
    return {
        "anthropic-key": ("sk" + "-ant-" + "api03-", _alnum(40)),
        "github-pat-classic": ("gh" + "p_", _alnum(36)),
        "github-oauth": ("gh" + "o_", _alnum(36)),
        "github-server-to-server": ("gh" + "s_", _alnum(36)),
        "github-user-to-server": ("gh" + "u_", _alnum(36)),
        "github-refresh": ("gh" + "r_", _alnum(36)),
        "github-pat-fine-grained": ("github" + "_pat_", _alnum(60)),
        "pem-private-key": ("-----" + "BEGIN RSA " + "PRIVATE " + "KEY" + "-----" + NL, _alnum(64)),
        "aws-access-key": ("AK" + "IA", _upper_alnum(16)),
        "slack-token": ("xo" + "xb-", _alnum(24)),
        "google-api-key": ("AI" + "za", _alnum(35)),
    }


# --------------------------------------------------------------------------- #
# ledger-dated
# --------------------------------------------------------------------------- #
def test_ledger_dated_fails_when_one_row_is_undated(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    code = publish.main(_check(repo, _ledger(tmp_path, ["2026-09-01", "", "2026-09-03"])))
    out, err = capsys.readouterr()
    assert code == 1
    assert _line(out, "ledger-dated") == "FAIL ledger-dated 2 of 3 rows dated"
    assert "FIXTURE_NAME" not in out + err  # counts only, never a ledger cell

    # control: every row dated -> PASS and exit 0
    code = publish.main(_check(repo, _ledger(tmp_path, ["2026-09-01"] * 3, name="ok.md")))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "ledger-dated") == "PASS ledger-dated 3 of 3 rows dated"


def test_ledger_dated_passes_on_a_complete_waiver_with_undated_rows(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    ledger = _ledger(tmp_path, ["2026-09-01", "", ""], tail=_waiver())
    code = publish.main(_check(repo, ledger))
    out, err = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "ledger-dated") == (
        "PASS ledger-dated waived by Owner on 2026-09-30: 2 of 3 rows undated"
    )
    # the waiver's name and date print; its reason, evidence and every other cell never do
    for never in ("FIXTURE_NAME", "FIXTURE_REASON", "FIXTURE_EVIDENCE"):
        assert never not in out + err, never


def test_ledger_dated_without_a_waiver_section_fails_as_before(tmp_path, capsys):
    """Waiver-shaped bullets under any other heading waive nothing: only a ``## Waiver``
    section does, so a ledger without one keeps today's FAIL, detail and exit code."""
    repo = _repo(tmp_path, _base_files())
    for i, tail in enumerate(("", _waiver(heading="## Notes"), _waiver(heading="### Waiver"))):
        ledger = _ledger(tmp_path, ["2026-09-01", "", "2026-09-03"], name=f"l{i}.md", tail=tail)
        code = publish.main(_check(repo, ledger))
        out, _ = capsys.readouterr()
        assert code == 1, (tail, out)
        assert _line(out, "ledger-dated") == "FAIL ledger-dated 2 of 3 rows dated", tail


@pytest.mark.parametrize("missing", sorted(WAIVER_FIELDS))
def test_ledger_waiver_missing_a_field_exits_2_naming_it(tmp_path, capsys, missing):
    repo = _repo(tmp_path, _base_files())
    # absent and present-but-empty are the same defect
    for i, value in enumerate((None, "")):
        tail = _waiver(**{missing: value})
        ledger = _ledger(tmp_path, ["", "2026-09-01"], name=f"l{i}.md", tail=tail)
        code = publish.main(_check(repo, ledger))
        out, _ = capsys.readouterr()
        assert code == 2, (value, out)
        line = _line(out, "ledger-dated")
        assert line.startswith("FAIL ledger-dated cannot measure"), line
        assert f"waiver is missing {missing}" in line, line


def test_ledger_waiver_with_a_bad_date_exits_2_naming_waived_on(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    for i, bad in enumerate(("2026-13-01", "2026-02-30", "30/09/2026", "someday")):
        ledger = _ledger(tmp_path, [""], name=f"l{i}.md", tail=_waiver(waived_on=bad))
        code = publish.main(_check(repo, ledger))
        out, _ = capsys.readouterr()
        assert code == 2, (bad, out)
        line = _line(out, "ledger-dated")
        assert "cannot measure" in line and "waived_on is not an ISO date" in line, line
        assert bad not in line  # the field is named, the cell is not printed


def test_ledger_waiver_with_a_wrong_scope_exits_2_naming_scope(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    for i, bad in enumerate(("release", "Publish", "publish, release")):
        ledger = _ledger(tmp_path, [""], name=f"l{i}.md", tail=_waiver(scope=bad))
        code = publish.main(_check(repo, ledger))
        out, _ = capsys.readouterr()
        assert code == 2, (bad, out)
        line = _line(out, "ledger-dated")
        assert "cannot measure" in line and "waiver scope is not publish" in line, line


def test_ledger_waiver_with_a_field_given_twice_exits_2(tmp_path, capsys):
    """Two values for one field is a waiver nobody can read one way, so it is not picked."""
    repo = _repo(tmp_path, _base_files())
    twice_in_one = (_waiver() + "- waived_on: 2026-10-01" + NL, "waived_on")
    # a second section repeats every field; the first one repeated is named
    twice_across = (_waiver() + _waiver(waived_by="Other"), "waived_by")
    for i, (tail, field) in enumerate((twice_in_one, twice_across)):
        ledger = _ledger(tmp_path, [""], name=f"l{i}.md", tail=tail)
        code = publish.main(_check(repo, ledger))
        out, _ = capsys.readouterr()
        assert code == 2, out
        line = _line(out, "ledger-dated")
        assert "cannot measure" in line and f"waiver gives {field} twice" in line, line


def _real_shape_ledger(tmp_path: Path) -> Path:
    """The live ledger's layout with neutral text: preamble sections, an eight-column table
    with a right-aligned separator and blank rotated_on cells, the waiver after the table with
    long colon-bearing reason and evidence bullets and a prose paragraph inside the section,
    then further sections with bold bullets and a code fence."""
    dash = "—"
    body = NL.join(
        [
            "# Key rotation ledger - fixture",
            "",
            "**Seeded by a fixture epic. NAMES ONLY.**",
            "",
            "This file is a checklist for a human to rotate off.",
            "",
            "## The hard rule this ledger is built around",
            "",
            "`rotated_on` and `rotated_by` are **deliberately blank on every row**.",
            "",
            "## The ledger",
            "",
            "| # | name | where consumed | owner / console | rotation order | rotated_on"
            " | rotated_by | notes |",
            "|---|---|---|---|---:|---|---|---|",
            "| 1 | `FIXTURE_NAME_1` | service env (host A) | Provider console | 1 | | |"
            " FIRST. Broadest exposure. |",
            "| 2 | `FIXTURE_NAME_2` | service env (host A) | self-issued | 2 | | |"
            " Internal only. |",
            "| 3 | fixture token | second env | Provider portal | 3 | | |"
            " Named by PROVIDER in the source, not by variable - see Fidelity below. |",
            "",
            "## Waiver",
            "",
            "- waived_by: Owner",
            "- waived_on: 2026-09-30",
            "- scope: publish",
            "- reason: Owner decided to publish without rotating these credentials. None of the"
            " 3 rows belongs to this product: each is consumed by another service, and none is"
            " in the repo. The cut publishes a fresh single commit, so no history ships.",
            "- evidence: a value-match scan of the full tree at the source rev (12 files, a"
            " superset of the cut) found 0 matches for the 3 values. Only names and counts were"
            " printed.",
            "",
            "The `rotated_on` and `rotated_by` cells stay blank: this waiver records a decision"
            " not to rotate, not a rotation. Transcribed on the owner's word.",
            "",
            "## Count",
            "",
            "**3 seed rows** - 2 from one env, 1 from another.",
            "",
            "## Rotation procedures",
            "",
            "### Read this before you start",
            "",
            f"- **Editing:** use an editor {dash} never a one-liner with the value in it.",
            "- **Restarting:** see below.",
            "",
            "```",
            "ssh host 'restart the service'",
            "```",
            "",
        ]
    )
    path = tmp_path / "real-shape-ledger.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_ledger_waiver_in_the_live_ledgers_shape_passes(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    code = publish.main(_check(repo, _real_shape_ledger(tmp_path)))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "ledger-dated") == (
        "PASS ledger-dated waived by Owner on 2026-09-30: 3 of 3 rows undated"
    )


def _fenced(body: str, opener: str, closer: str | None = None) -> str:
    """``body`` inside a fence. ``closer`` defaults to the opener's own run; ``""`` leaves the
    fence unclosed."""
    close = re.match(r"\s*([`~]+)", opener).group(1) if closer is None else closer
    return NL.join(["", opener, body.strip(NL), *([close] if close else []), ""])


@pytest.mark.parametrize("opener", ["```markdown", "~~~", "````", "  ~~~~ md"])
def test_ledger_fenced_example_waiver_is_not_a_waiver(tmp_path, capsys, opener):
    """gh#785: a complete, filled-in waiver pasted as an example inside a fence (either fence
    character, any length, indented up to three spaces) waives nothing: undated rows keep
    today's FAIL and exit 1, never the PASS the unfenced parser gave it."""
    repo = _repo(tmp_path, _base_files())
    ledger = _ledger(tmp_path, ["2026-09-01", "", ""], tail=_fenced(_waiver(), opener))
    code = publish.main(_check(repo, ledger))
    out, _ = capsys.readouterr()
    assert code == 1, out
    assert _line(out, "ledger-dated") == "FAIL ledger-dated 1 of 3 rows dated"


def test_ledger_unfenced_waiver_after_a_fenced_example_is_honoured(tmp_path, capsys):
    """The fence hides only its own lines. A real waiver after a closed example fence still
    passes, and a fenced block INSIDE the real section neither ends the section (a fenced
    heading) nor adds a second value (a fenced bullet)."""
    repo = _repo(tmp_path, _base_files())
    inner = NL.join(["# Not a heading", "- waived_by: Someone Else", "- scope: other"])
    real = NL.join(
        [
            "",
            "## Waiver",
            "",
            "- waived_by: Owner",
            _fenced(inner, "~~~"),
            *(f"- {k}: {v}" for k, v in WAIVER_FIELDS.items() if k != "waived_by"),
            "",
        ]
    )
    tail = _fenced(_waiver(waived_by="Example Person"), "```markdown") + real
    ledger = _ledger(tmp_path, ["2026-09-01", "", ""], tail=tail)
    code = publish.main(_check(repo, ledger))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "ledger-dated") == (
        "PASS ledger-dated waived by Owner on 2026-09-30: 2 of 3 rows undated"
    )


@pytest.mark.parametrize(
    "opener, inner_run",
    [
        ("````", "```"),  # a shorter run of the same character is content
        ("```", "~~~"),  # the other character is content
        ("~~~", "```"),
    ],
)
def test_ledger_fence_is_closed_only_by_its_own_kind(tmp_path, capsys, opener, inner_run):
    """A block that shows a fence of its own (nested) stays one block: the waiver after the
    inner run is still inside the outer fence, so it waives nothing."""
    repo = _repo(tmp_path, _base_files())
    body = NL.join([inner_run, "not a close", inner_run, _waiver()])
    ledger = _ledger(tmp_path, ["", "2026-09-01"], tail=_fenced(body, opener))
    code = publish.main(_check(repo, ledger))
    out, _ = capsys.readouterr()
    assert code == 1, out
    assert _line(out, "ledger-dated") == "FAIL ledger-dated 1 of 2 rows dated"


def test_ledger_waiver_after_an_unclosed_fence_is_not_read(tmp_path, capsys):
    """A fence never closed runs to the end of the file, so a pasted example that lost its
    closing line fails closed rather than counting as the human's word. A closing run with
    text after it is not a close either."""
    repo = _repo(tmp_path, _base_files())
    for i, closer in enumerate(("", "``` trailing text")):
        ledger = _ledger(
            tmp_path, ["", ""], name=f"l{i}.md", tail=_fenced(_waiver(), "```", closer=closer)
        )
        code = publish.main(_check(repo, ledger))
        out, _ = capsys.readouterr()
        assert code == 1, (closer, out)
        assert _line(out, "ledger-dated") == "FAIL ledger-dated 0 of 2 rows dated", closer


def test_ledger_near_miss_waiver_heading_is_named_by_line(tmp_path, capsys):
    """gh#785 (optional part): ``## waiver`` or ``## Waiver (publish)`` waives nothing, and the
    FAIL detail says which line looked like one instead of ignoring it silently. A near miss
    inside a fence is an example and is not named; the heading text itself never prints."""
    repo = _repo(tmp_path, _base_files())
    tail = (
        _waiver(heading="## waiver")
        + _fenced("## Waiver (draft)", "```")
        + _waiver(heading="## Waiver (publish)", waived_by="Other")
    )
    ledger = _ledger(tmp_path, ["", "2026-09-01"], tail=tail)
    lines = ledger.read_text(encoding="utf-8").splitlines()
    want = [str(n) for n, ln in enumerate(lines, 1) if ln in ("## waiver", "## Waiver (publish)")]
    assert len(want) == 2
    code = publish.main(_check(repo, ledger))
    out, _ = capsys.readouterr()
    assert code == 1, out
    assert _line(out, "ledger-dated") == (
        f"FAIL ledger-dated 1 of 2 rows dated; line(s) {', '.join(want)} look like a waiver "
        "heading but are not exactly '## Waiver', so they waive nothing"
    )
    assert "(publish)" not in out and "(draft)" not in out


def test_ledger_missing_or_rowless_exits_2_never_0(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(tmp_path / "no-config-here"))
    repo = _repo(tmp_path, _base_files())
    rowless = tmp_path / "rowless.md"
    rowless.write_text("| # | rotated_on |\n|---|---|\n\nno rows\n", encoding="utf-8")
    garbage = tmp_path / "garbage.md"
    garbage.write_text("this ledger has no table at all\n", encoding="utf-8")
    for ledger in (tmp_path / "missing.md", rowless, garbage):
        code = publish.main(_check(repo, ledger))
        out, _ = capsys.readouterr()
        assert code == 2, (ledger, out)
        assert "FAIL ledger-dated cannot measure" in out
    # no --ledger and no resolvable project is also unmeasurable, not clean
    code = publish.main(
        [
            "check",
            "--tree",
            str(repo),
            "--host-ref",
            HOST_REF,
            "--source",
            str(repo),
            "--project",
            "no-such-project-anywhere",
        ]
    )
    out, _ = capsys.readouterr()
    assert code == 2, out


# --------------------------------------------------------------------------- #
# session-urls
# --------------------------------------------------------------------------- #
def test_session_urls_flags_a_planted_url_and_a_trailer_line(tmp_path, capsys):
    ledger = _ledger(tmp_path, ["2026-09-01"])
    url_repo = _repo(
        tmp_path, {**_base_files(), "notes.md": f"see {_session_url()}\n".encode()}, name="url"
    )
    code = publish.main(_check(url_repo, ledger))
    out, _ = capsys.readouterr()
    assert code == 1
    assert _line(out, "session-urls").startswith("FAIL session-urls")
    assert "notes.md:1" in out

    trailer_repo = _repo(
        tmp_path,
        {**_base_files(), "msg.txt": f"Subject\n\n{_trailer()}\n".encode()},
        name="trailer",
    )
    code = publish.main(_check(trailer_repo, ledger))
    out, _ = capsys.readouterr()
    assert code == 1
    assert "msg.txt:3" in out

    # the cut commit's own message is scanned too
    msg_repo = _repo(tmp_path, _base_files(), name="msg")
    _git(msg_repo, "commit", "-q", "--amend", "-m", f"fixture{NL}{NL}{_trailer()}")
    code = publish.main(_check(msg_repo, ledger))
    out, _ = capsys.readouterr()
    assert code == 1
    assert "<cut commit message>" in out


def test_session_urls_ignores_the_two_rule_statements(tmp_path, capsys):
    files = _base_files()
    files["CLAUDE.md"] = (REPO_ROOT / "CLAUDE.md").read_bytes()
    files["skills/_shared/PR-PROSE.md"] = (REPO_ROOT / "skills/_shared/PR-PROSE.md").read_bytes()
    host = "clau" + "de.ai/code"
    assert (
        host.encode() in files["CLAUDE.md"] and host.encode() in files["skills/_shared/PR-PROSE.md"]
    )
    repo = _repo(tmp_path, files)
    publish.main(_check(repo, _ledger(tmp_path, ["2026-09-01"])))
    out, _ = capsys.readouterr()
    assert _line(out, "session-urls") == "PASS session-urls 0 lines"


# --------------------------------------------------------------------------- #
# sanitize-terms
# --------------------------------------------------------------------------- #
def test_sanitize_terms_scans_skills_vendored(tmp_path, capsys):
    upper = ("zorb" + "lax").upper()
    files = {
        **_base_files(),
        "skills/_vendored/upstream/SKILL.md": f"# Upstream\n\nmentions {upper} here\n".encode(),
        "skills/_vendored/upstream/notes.md": f"and {'quux' + 'fred'} too\n".encode(),
        "skills/_vendored/upstream/blob.bin": b"\0" + ("zorb" + "lax").encode(),  # binary: skipped
    }
    repo = _repo(tmp_path, files)
    code = publish.main(_check(repo, _ledger(tmp_path, ["2026-09-01"])))
    out, _ = capsys.readouterr()
    assert code == 1
    assert _line(out, "sanitize-terms").startswith("FAIL sanitize-terms 2 line(s)")
    assert "skills/_vendored/upstream/SKILL.md:3" in out
    assert "skills/_vendored/upstream/notes.md:1" in out
    assert "blob.bin" not in out
    assert upper.lower() not in out.lower()  # the term itself is never printed


def test_sanitize_terms_unparseable_ci_yml_exits_2(tmp_path, capsys):
    files = {**_base_files(), ".github/workflows/ci.yml": b"jobs: {}\n"}
    repo = _repo(tmp_path, files)
    code = publish.main(_check(repo, _ledger(tmp_path, ["2026-09-01"])))
    out, _ = capsys.readouterr()
    assert code == 2
    assert "FAIL sanitize-terms cannot measure" in out


# --------------------------------------------------------------------------- #
# sanitize-terms: the local-only extra pattern (gh#821)
# --------------------------------------------------------------------------- #
#: Neutral placeholder alternatives in the extra pattern file's own format. A real private
#: alternative never goes in a tracked file.
EXTRA_WORD = "acmeproduct"
EXTRA_HOST = "zephyrhost"
EXTRA_FILE_TEXT = f"# fixture extra pattern\n\n(?<!super){EXTRA_WORD}\n{EXTRA_HOST}\\b\n"


def _extra_file(tmp_path: Path, body: str = EXTRA_FILE_TEXT, name: str = "extra.txt") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_sanitize_terms_unions_the_extra_pattern_and_names_its_sources(tmp_path, capsys):
    """The cut's ci.yml pattern alone passes this cut; unioned with the extra pattern it fails,
    the line says which sources it applied, and neither a matched term nor the pattern is
    printed - not even through a path, which is masked like a sensitive-term path."""
    notes = ["# Notes", "", f"shipped for {EXTRA_WORD.upper()} once", f"super{EXTRA_WORD} is fine"]
    files = {
        **_base_files(),
        "notes.md": (NL.join(notes) + NL).encode(),
        f"docs/{EXTRA_HOST}-setup.md": f"see {EXTRA_HOST} first{NL}".encode(),
    }
    repo = _repo(tmp_path, files)
    ledger = _ledger(tmp_path, ["2026-09-01"])
    code = publish.main(_check(repo, ledger))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "sanitize-terms") == (
        f"PASS sanitize-terms 0 matches over {len(files)} text files (ci.yml pattern; extra "
        "pattern not configured: no --sanitize-extra-pattern and no --project)"
    )

    extra = _extra_file(tmp_path)
    code = publish.main(_check(repo, ledger, "--sanitize-extra-pattern", str(extra)))
    out, err = capsys.readouterr()
    assert code == 1, out
    assert _line(out, "sanitize-terms") == (
        "FAIL sanitize-terms 2 line(s) match the sanitize pattern "
        f"(ci.yml pattern + extra pattern from {extra})"
    )
    assert "  notes.md:3" in out.splitlines()
    assert re.search(r"^  <cut path #\d+>:1$", out, re.M), out
    _assert_no_term(out + err, EXTRA_WORD, EXTRA_HOST)


def test_sanitize_terms_extra_pattern_absent_is_not_configured_and_passes(tmp_path, capsys):
    repo = _repo(tmp_path, {**_base_files(), "notes.md": f"{EXTRA_WORD}\n".encode()})
    missing = tmp_path / "nowhere" / "sanitize-extra-pattern.txt"
    code = publish.main(
        _check(repo, _ledger(tmp_path, ["2026-09-01"]), "--sanitize-extra-pattern", str(missing))
    )
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "sanitize-terms").endswith(
        f"(ci.yml pattern; extra pattern not configured: {missing} does not exist)"
    )
    commented = _extra_file(tmp_path, "# only a comment\n\n   \n", name="empty.txt")
    code = publish.main(
        _check(repo, _ledger(tmp_path, ["2026-09-01"]), "--sanitize-extra-pattern", str(commented))
    )
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "sanitize-terms").endswith(
        f"(ci.yml pattern; extra pattern not configured: {commented} lists 0 alternatives)"
    )


def test_sanitize_terms_unreadable_extra_pattern_exits_2_never_0(tmp_path, capsys):
    """Mirrors sensitive-terms: a file that exists and cannot be used is cannot measure, never
    a quiet fall-back to the generic shapes, and the reason never quotes the file."""
    repo = _repo(tmp_path, _base_files())
    ledger = _ledger(tmp_path, ["2026-09-01"])
    a_dir = tmp_path / "extra-dir"
    a_dir.mkdir()
    not_utf8 = tmp_path / "latin1.txt"
    not_utf8.write_bytes(b"caf\xe9\n")
    bad_regex = _extra_file(tmp_path, f"{EXTRA_WORD}\n{EXTRA_HOST}(\n", name="bad.txt")
    for path in (a_dir, not_utf8, bad_regex):
        code = publish.main(_check(repo, ledger, "--sanitize-extra-pattern", str(path)))
        out, err = capsys.readouterr()
        assert code == 2, (path, out)
        assert _line(out, "sanitize-terms").startswith("FAIL sanitize-terms cannot measure - ")
        assert "caf" not in out
        _assert_no_term(out + err, EXTRA_WORD, EXTRA_HOST)
    assert f"{bad_regex} line 2 is not a valid regular expression" in out


def test_sanitize_extra_pattern_defaults_to_the_projects_data_root(tmp_path, capsys, monkeypatch):
    """With ``--project``, the file is read from
    ``<data_root>/projects/<project>/publish/sanitize-extra-pattern.txt`` - outside the repo."""
    from agentflow.sanitize_extra import EXTRA_PATTERN_RELPATH

    src = _repo(tmp_path, {**_base_files(), "notes.md": f"hi {EXTRA_HOST}\n".encode()})
    _scratch_home(tmp_path, src, monkeypatch, notify="kind: none")
    listed = tmp_path / "data" / "projects" / "demo" / EXTRA_PATTERN_RELPATH
    listed.parent.mkdir(parents=True)
    listed.write_text(EXTRA_FILE_TEXT, encoding="utf-8")
    code = publish.main(_check(src, _ledger(tmp_path, ["2026-09-01"]), "--project", "demo"))
    out, err = capsys.readouterr()
    assert code == 1, out
    assert _line(out, "sanitize-terms") == (
        "FAIL sanitize-terms 1 line(s) match the sanitize pattern "
        f"(ci.yml pattern + extra pattern from {listed})"
    )
    assert "  notes.md:1" in out.splitlines()
    _assert_no_term(out + err, EXTRA_HOST)


# --------------------------------------------------------------------------- #
# sensitive-terms: the local-only list (gh#791)
# --------------------------------------------------------------------------- #
#: Neutral, made-up fixture terms. A real sensitive term never goes in a tracked file.
TERM_WORD = "Zentrofax"
TERM_ADDR = "plumbix.wob@fixture.invalid"


def _terms(tmp_path: Path, body: str, name: str = "sensitive-terms.txt") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _assert_no_term(text: str, *terms: str) -> None:
    for term in terms:
        assert term.casefold() not in text.casefold(), "a sensitive term reached the output"


def test_sensitive_terms_hit_reports_location_never_the_term(tmp_path, capsys):
    """A hit names the file, the line and the term's line in the list; the term itself is in
    no output line, stdout or stderr. Matching ignores case and is whole-word, a cut path
    holding a term is shown by position, and the commit message is scanned too."""
    files = {
        **_base_files(),
        "notes.md": NL.join(["# Notes", "", f"built for {TERM_WORD.upper()} last year", ""]).encode(),
        f"docs/{TERM_WORD.lower()}-plan.md": f"mail {TERM_ADDR} for access\n".encode(),
        # not whole-word: a letter or digit continuing the word is not a hit
        "other.md": f"{TERM_WORD}ish and {TERM_WORD}2 are fine\n".encode(),
    }
    repo = _repo(tmp_path, files)
    _git(repo, "commit", "--amend", "-q", "-m", f"fixture for {TERM_WORD}")
    terms = _terms(tmp_path, NL.join(["# client names", "", TERM_WORD, "  ", TERM_ADDR, ""]))
    code = publish.main(_check(repo, _ledger(tmp_path, ["2026-09-01"]), "--sensitive-terms", str(terms)))
    out, err = capsys.readouterr()
    assert code == 1, out
    assert _line(out, "sensitive-terms") == (
        f"FAIL sensitive-terms 4 location(s) match a term on the local list {terms}"
    )
    assert "  notes.md:3 (list line 3)" in out.splitlines()
    assert "  <cut commit message> (list line 3)" in out.splitlines()
    path_hit = [ln for ln in out.splitlines() if ln.endswith("(the path itself, list line 3)")]
    content_hit = [ln for ln in out.splitlines() if ln.endswith(":1 (list line 5)")]
    assert len(path_hit) == 1 and len(content_hit) == 1, out
    assert re.fullmatch(r"  <cut path #\d+> \(the path itself, list line 3\)", path_hit[0])
    assert content_hit[0] == path_hit[0].split(" (")[0] + ":1 (list line 5)"
    assert "other.md" not in out
    _assert_no_term(out + err, TERM_WORD, TERM_ADDR)

    # the same, called directly: the Result and its lines carry no term either
    result = publish.check_sensitive_terms(publish._load_cut(repo), terms)
    _assert_no_term(repr(result) + NL.join(result.lines()), TERM_WORD, TERM_ADDR)


def test_sensitive_terms_match_inside_identifiers_and_punctuated_terms(tmp_path, capsys):
    """Only a letter or digit continues a word: an underscore-joined identifier and an address
    both hold the term. A term that starts with punctuation carries its own left boundary,
    so ``@<domain>`` is found inside ``jane@<domain>``. A longer word is still not a hit."""
    domain = f"@{TERM_WORD.lower()}.example"
    files = {
        **_base_files(),
        "cfg.py": f"{TERM_WORD.upper()}_API_KEY = 1\n".encode(),
        "mod.py": f"import os\nimport {TERM_WORD.lower()}_client\n".encode(),
        "mail.md": f"write to jane{domain} today\n".encode(),
        "other.md": f"{TERM_WORD.lower()}es, {TERM_WORD}2 and x{TERM_WORD}\n".encode(),
    }
    repo = _repo(tmp_path, files)
    terms = _terms(tmp_path, NL.join([TERM_WORD, domain, ""]))
    code = publish.main(
        _check(repo, _ledger(tmp_path, ["2026-09-01"]), "--sensitive-terms", str(terms))
    )
    out, err = capsys.readouterr()
    assert code == 1, out
    found = {ln.strip() for ln in out.splitlines() if "(list line" in ln}
    assert found == {
        "cfg.py:1 (list line 1)",
        "mod.py:2 (list line 1)",
        "mail.md:1 (list line 1)",
        "mail.md:1 (list line 2)",
    }, out
    _assert_no_term(out + err, TERM_WORD, domain)


def test_no_check_prints_a_path_that_holds_a_term(tmp_path, capsys):
    """Every check prints its paths through one display, so a path holding a term - even
    inside a longer word or an underscore join - never reaches the output of ANY check:
    session-urls, sanitize-terms, key-shapes, excluded-paths (in the cut and dropped by it),
    tracker-links and sha-citations each fire on such a path here, and the term appears
    nowhere in run_check's lines or in the CLI's stdout and stderr, in any case."""
    low = TERM_WORD.lower()
    prefix, rest = _planted_keys()["github-pat-classic"]
    owner = HOST_REF.split("/", 1)[0]
    files = {
        **_base_files(),
        f"src/{low}_client.py": f"# see {_issue_link(owner, 'r')}\n".encode(),
        f"{TERM_WORD}/.env.example": b"A=1\n",
        f"lib/{TERM_WORD.upper()}_keys.txt": f"token = {prefix}{rest}\n".encode(),
        f"notes/{low}_session.md": f"from {_session_url()}\n".encode(),
        f"vendor/{low}ish.md": f"mentions {'zorb' + 'lax'}\n".encode(),
    }
    repo = _repo(tmp_path, files)
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "docs" / f"{low}_history.md").write_text(f"fixed in {first}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "cite")
    terms = _terms(tmp_path, f"{TERM_WORD}{NL}")
    ledger = _ledger(tmp_path, ["2026-09-01"])

    code, lines = publish.run_check(
        repo,
        ledger=ledger,
        host_ref=HOST_REF,
        source=repo,
        dropped=[f"{TERM_WORD}_old/.env"],
        sensitive_terms=terms,
    )
    text = NL.join(lines)
    assert code == 1, text
    for name in (
        "session-urls",
        "sanitize-terms",
        "sensitive-terms",
        "key-shapes",
        "excluded-paths",
        "tracker-links",
    ):
        assert _line(text, name).startswith(f"FAIL {name}"), text
    assert _line(text, "sha-citations").startswith("REPORT sha-citations 1 in 1 file(s)"), text
    assert re.search(r"^  in cut: <cut path #\d+>$", text, re.M), text
    assert "  dropped by cut: <dropped path #1>" in lines, text
    _assert_no_term(text, TERM_WORD)

    publish.main(_check(repo, ledger, "--sensitive-terms", str(terms)))
    out, err = capsys.readouterr()
    assert "FAIL tracker-links" in out, out
    _assert_no_term(out + err, TERM_WORD)


def test_sensitive_terms_no_hit_passes(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    terms = _terms(tmp_path, NL.join([TERM_WORD, TERM_ADDR, ""]))
    code = publish.main(_check(repo, _ledger(tmp_path, ["2026-09-01"]), "--sensitive-terms", str(terms)))
    out, err = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "sensitive-terms") == (
        f"PASS sensitive-terms 0 matches for 2 term(s) from {terms} over "
        f"{len(_base_files())} files"
    )
    _assert_no_term(out + err, TERM_WORD, TERM_ADDR)


def test_sensitive_terms_absent_list_is_not_configured_and_passes(tmp_path, capsys):
    """No list is an extra guard switched off, not a failure: the line says ``not configured``
    and names where it looked, so the operator can see the guard did not run."""
    repo = _repo(tmp_path, {**_base_files(), "notes.md": f"{TERM_WORD}\n".encode()})
    ledger = _ledger(tmp_path, ["2026-09-01"])
    missing = tmp_path / "nowhere" / "sensitive-terms.txt"
    code = publish.main(_check(repo, ledger, "--sensitive-terms", str(missing)))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "sensitive-terms") == (
        f"PASS sensitive-terms not configured: {missing} does not exist"
    )
    # explicit routing flags with no project: there is no data root to look in
    code = publish.main(_check(repo, ledger))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "sensitive-terms") == (
        "PASS sensitive-terms not configured: no --sensitive-terms and no --project"
    )


def test_sensitive_terms_skip_blank_lines_and_whole_line_comments(tmp_path, capsys):
    """Blank and whitespace-only lines are skipped; a line whose first non-blank character is
    ``#`` is a comment even when it names a term; a ``#`` inside a term is part of it. A list
    of comments only is ``not configured``."""
    repo = _repo(
        tmp_path,
        {**_base_files(), "notes.md": f"{TERM_WORD} and {TERM_ADDR}\nuses qu#ark here\n".encode()},
    )
    ledger = _ledger(tmp_path, ["2026-09-01"])
    commented = _terms(tmp_path, NL.join([f"# {TERM_WORD}", "", "   ", f"  #{TERM_ADDR}", ""]))
    code = publish.main(_check(repo, ledger, "--sensitive-terms", str(commented)))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "sensitive-terms") == (
        f"PASS sensitive-terms not configured: {commented} lists 0 terms"
    )

    hashed = _terms(tmp_path, NL.join(["# a comment", "", "  QU#ARK  ", ""]), name="hashed.txt")
    code = publish.main(_check(repo, ledger, "--sensitive-terms", str(hashed)))
    out, _ = capsys.readouterr()
    assert code == 1, out
    assert "  notes.md:2 (list line 3)" in out.splitlines()
    _assert_no_term(out, "qu#ark")


def test_sensitive_terms_unreadable_list_exits_2_never_0(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    ledger = _ledger(tmp_path, ["2026-09-01"])
    a_dir = tmp_path / "terms-dir"
    a_dir.mkdir()
    not_utf8 = tmp_path / "latin1.txt"
    not_utf8.write_bytes(b"caf\xe9\n")
    for path in (a_dir, not_utf8):
        code = publish.main(_check(repo, ledger, "--sensitive-terms", str(path)))
        out, _ = capsys.readouterr()
        assert code == 2, (path, out)
        assert _line(out, "sensitive-terms").startswith("FAIL sensitive-terms cannot measure - ")
        assert "caf" not in out


def test_sensitive_terms_list_defaults_to_the_projects_data_root(tmp_path, capsys, monkeypatch):
    """With ``--project``, the list is read from
    ``<data_root>/projects/<project>/publish/sensitive-terms.txt`` - outside the repo."""
    src = _repo(tmp_path, {**_base_files(), "notes.md": f"hi {TERM_WORD}\n".encode()})
    _scratch_home(tmp_path, src, monkeypatch, notify="kind: none")
    project_dir = tmp_path / "data" / "projects" / "demo"
    listed = project_dir / publish.SENSITIVE_TERMS_RELPATH
    listed.parent.mkdir(parents=True)
    listed.write_text(f"{TERM_WORD}\n", encoding="utf-8")
    code = publish.main(_check(src, _ledger(tmp_path, ["2026-09-01"]), "--project", "demo"))
    out, err = capsys.readouterr()
    assert code == 1, out
    assert _line(out, "sensitive-terms") == (
        f"FAIL sensitive-terms 1 location(s) match a term on the local list {listed}"
    )
    assert "  notes.md:1 (list line 1)" in out.splitlines()
    _assert_no_term(out + err, TERM_WORD)


# --------------------------------------------------------------------------- #
# check over an empty cut / a non-repo
# --------------------------------------------------------------------------- #
def test_check_on_a_cut_with_no_files_exits_2(tmp_path, capsys):
    repo = _repo(tmp_path, {}, allow_empty=True)
    code = publish.main(_check(repo, _ledger(tmp_path, ["2026-09-01"])))
    _, err = capsys.readouterr()
    assert code == 2
    assert "zero files" in err

    not_repo = tmp_path / "plain"
    not_repo.mkdir()
    ceiling = subprocess.run(
        ["git", "-C", str(not_repo), "rev-parse", "--show-toplevel"], capture_output=True
    )
    if ceiling.returncode != 0:  # tmp_path is not inside some other repo
        assert publish.main(_check(not_repo, _ledger(tmp_path, ["2026-09-01"]))) == 2
    # a subdirectory of a repo is refused rather than read as its parent's cut
    assert publish.main(_check(repo / ".git", _ledger(tmp_path, ["2026-09-01"]))) == 2


# --------------------------------------------------------------------------- #
# cut-shape (gh#654)
# --------------------------------------------------------------------------- #
PERSON = "Some Person <person@example.com>"


def _shape_problems(repo: Path, ledger: Path, capsys) -> tuple[int, str]:
    code = publish.main(_check(repo, ledger))
    out, _ = capsys.readouterr()
    return code, out


def test_cut_shape_passes_a_genuine_cut_and_fails_a_second_commit(tmp_path, capsys):
    ledger = _ledger(tmp_path, ["2026-09-01"])
    src = _repo(tmp_path, _base_files(), ident=SOURCE_IDENT)
    out_dir = tmp_path / "cut"
    assert publish.main(_cut(src, out_dir, ledger)) == 0
    capsys.readouterr()
    code, out = _shape_problems(out_dir, ledger, capsys)
    assert code == 0, out
    assert _line(out, "cut-shape").startswith("PASS cut-shape")

    # committed to again by the cut identity itself: the content checks all still pass
    (out_dir / "later.md").write_text("later\n", encoding="utf-8")
    _git(out_dir, "add", "later.md")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": publish.AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": NOREPLY,
        "GIT_COMMITTER_NAME": publish.AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": NOREPLY,
    }
    _git(out_dir, "commit", "-q", "-m", "again", env=env)
    code, out = _shape_problems(out_dir, ledger, capsys)
    assert code == 1, out
    assert _line(out, "cut-shape").startswith("FAIL cut-shape 2 problem(s)")
    assert "2 commits reachable, want 1" in out
    assert "HEAD has 1 parent(s), want none" in out
    for name in ("session-urls", "key-shapes", "excluded-paths", "tracker-links"):
        assert _line(out, name).startswith(f"PASS {name}"), out


def test_cut_shape_fails_a_personal_author(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    _git(repo, "commit", "-q", "--amend", "--no-edit", f"--author={PERSON}")
    code, out = _shape_problems(repo, _ledger(tmp_path, ["2026-09-01"]), capsys)
    assert code == 1, out
    assert _line(out, "cut-shape") == "FAIL cut-shape 1 problem(s); rebuild the cut"
    assert f"author is not {publish.AUTHOR_NAME} with a noreply address" in out
    assert "person@example.com" not in out  # the identity found is never printed


def test_cut_shape_fails_a_personal_committer(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    _git(repo, "config", "user.name", "Some Person")
    _git(repo, "config", "user.email", "person@example.com")
    _git(repo, "commit", "-q", "--amend", "--no-edit")  # keeps the author, restamps the committer
    assert _git(repo, "log", "-1", "--format=%an|%cn") == f"{publish.AUTHOR_NAME}|Some Person"
    code, out = _shape_problems(repo, _ledger(tmp_path, ["2026-09-01"]), capsys)
    assert code == 1, out
    assert f"committer is not {publish.AUTHOR_NAME} with a noreply address" in out
    assert "  author is not" not in out

    # the right name with a non-noreply address is not the cut identity either
    _git(repo, "config", "user.name", publish.AUTHOR_NAME)
    _git(repo, "commit", "-q", "--amend", "--no-edit")
    code, out = _shape_problems(repo, _ledger(tmp_path, ["2026-09-01"], name="l2.md"), capsys)
    assert code == 1, out
    assert f"committer is not {publish.AUTHOR_NAME} with a noreply address" in out


def test_cut_shape_fails_a_remote(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    _git(repo, "remote", "add", "origin", "https://example.com/share.git")
    code, out = _shape_problems(repo, _ledger(tmp_path, ["2026-09-01"]), capsys)
    assert code == 1, out
    assert "1 remote(s) configured, want none" in out
    assert "example.com" not in out  # the remote's URL is never printed


@pytest.mark.parametrize("var", ["pushInsteadOf", "insteadOf"])
def test_cut_shape_fails_a_url_rewrite(tmp_path, capsys, var):
    # A rewrite can redirect step (7)'s push by URL without any remote. The section and variable
    # are written in mixed case: git keys are case-insensitive, so the check must be too.
    repo = _repo(tmp_path, _base_files())
    _git(repo, "config", f"URL.https://elsewhere.example.com/.{var}", "https://example.com/")
    code, out = _shape_problems(repo, _ledger(tmp_path, ["2026-09-01"]), capsys)
    assert code == 1, out
    assert _line(out, "cut-shape") == "FAIL cut-shape 1 problem(s); rebuild the cut"
    assert "1 URL rewrite(s) (insteadOf) configured, want none" in out
    assert "example.com" not in out  # counts only, never a URL

    # an unrelated url.* key is not a rewrite, and is still refused: it is not allow-listed
    other = _repo(tmp_path, _base_files(), name="other")
    _git(other, "config", "url.https://example.com/.fixtureKey", "x")
    code, out = _shape_problems(other, _ledger(tmp_path, ["2026-09-01"], name="l2.md"), capsys)
    assert code == 1, out
    assert "1 config key(s) outside the allow-list: local url.*.fixturekey" in out
    assert "URL rewrite" not in out and "example.com" not in out


def test_cut_shape_fails_an_extra_ref(tmp_path, capsys):
    ledger = _ledger(tmp_path, ["2026-09-01"])
    for name, args in (("branch", ("branch", "extra")), ("tag", ("tag", "v1"))):
        repo = _repo(tmp_path, _base_files(), name=name)
        _git(repo, *args)
        code, out = _shape_problems(repo, ledger, capsys)
        assert code == 1, (name, out)
        assert "1 ref(s) besides refs/heads/master, want none" in out, name
        # the same commit under a second name adds no commit: only the ref problem fires
        assert _line(out, "cut-shape") == "FAIL cut-shape 1 problem(s); rebuild the cut"


@pytest.mark.parametrize("kind", ["include", "includeIf"])
def test_cut_shape_fails_a_config_include(tmp_path, capsys, kind):
    # gh#728: `--local` does not follow an include, so a remote and a rewrite that live in the
    # included file read as "none" while git still pushes through them.
    repo = _repo(tmp_path, _base_files())
    leak = "file:///" + (tmp_path / "leak.git").as_posix().lstrip("/")
    extra = tmp_path / "extra.cfg"
    extra.write_text(
        f'[url "{leak}"]\n\tinsteadOf = https://example.com/share\n'
        '[remote "origin"]\n\turl = https://example.com/share\n',
        encoding="utf-8",
    )
    if kind == "include":
        key = "include.path"
    else:
        key = f"includeIf.gitdir/i:{repo.as_posix()}/.path"
    _git(repo, "config", "--local", key, extra.as_posix())
    # control: the bypass is real - git resolves the push URL through the included rewrite
    assert _git(repo, "ls-remote", "--get-url", "https://example.com/share") == leak
    code, out = _shape_problems(repo, _ledger(tmp_path, ["2026-09-01"]), capsys)
    assert code == 1, out
    # the include is named as one, and the keys it pulls in are refused as git sees them
    assert _line(out, "cut-shape") == "FAIL cut-shape 3 problem(s); rebuild the cut"
    assert "1 config include(s) (include.* / includeIf.*) configured, want none" in out
    assert "1 remote(s) configured, want none (local config)" in out
    assert "1 URL rewrite(s) (insteadOf) configured, want none (local config)" in out
    assert "example.com" not in out and "leak.git" not in out and "extra.cfg" not in out


def test_cut_shape_fails_worktree_config_git_obeys(tmp_path, capsys):
    # A key in `config.worktree` is outside `--local`, and git obeys it once
    # extensions.worktreeConfig is on.
    repo = _repo(tmp_path, _base_files())
    hooks = tmp_path / "elsewhere-hooks"
    hooks.mkdir()
    leak = "file:///" + (tmp_path / "leak.git").as_posix().lstrip("/")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    _git(repo, "config", "--worktree", f"url.{leak}.insteadOf", "https://example.com/share")
    _git(repo, "config", "--worktree", "core.hooksPath", hooks.as_posix())
    # control: git obeys both, and a plain --local read sees neither
    assert _git(repo, "ls-remote", "--get-url", "https://example.com/share") == leak
    assert _git(repo, "rev-parse", "--git-path", "hooks").replace("\\", "/") == hooks.as_posix()
    local = subprocess.run(
        ["git", "-C", str(repo), "config", "--local", "--get-regexp", r"^(url\.|core\.hookspath)"],
        capture_output=True,
    )
    assert local.returncode == 1, local.stdout
    code, out = _shape_problems(repo, _ledger(tmp_path, ["2026-09-01"]), capsys)
    assert code == 1, out
    assert _line(out, "cut-shape") == "FAIL cut-shape 3 problem(s); rebuild the cut"
    assert "1 URL rewrite(s) (insteadOf) configured, want none (worktree config)" in out
    assert "core.hooksPath is set, want unset (worktree config)" in out
    assert "1 config key(s) outside the allow-list: local extensions.worktreeconfig" in out
    assert "example.com" not in out and "leak.git" not in out


@pytest.mark.parametrize(
    "key, value, shown",
    [
        ("credential.helper", "!fixture-helper", "local credential.helper"),
        ("credential.https://example.com.helper", "store", "local credential.*.helper"),
        ("core.sshCommand", "fixture-ssh -o ProxyCommand=x", "local core.sshcommand"),
        ("http.proxy", "http://example.com:1", "local http.proxy"),
        ("core.fsmonitor", "fixture-monitor", "local core.fsmonitor"),
    ],
)
def test_cut_shape_refuses_every_key_outside_the_allow_list(tmp_path, capsys, key, value, shown):
    repo = _repo(tmp_path, _base_files())
    _git(repo, "config", key, value)
    assert _git(repo, "config", "--get", key) == value  # control: git holds it
    code, out = _shape_problems(repo, _ledger(tmp_path, ["2026-09-01"]), capsys)
    assert code == 1, out
    assert _line(out, "cut-shape") == "FAIL cut-shape 1 problem(s); rebuild the cut"
    assert f"1 config key(s) outside the allow-list: {shown}" in out
    assert value not in out and "example.com" not in out  # the key's name only, never a value


def test_cut_shape_passes_plain_git_init_and_build_cut_config(tmp_path, capsys):
    """Controls: neither what `git init` writes on this platform nor what build_cut adds is
    refused, so the allow-list is not failing genuine cuts."""
    ledger = _ledger(tmp_path, ["2026-09-01"])

    def local_keys(repo: Path) -> set[str]:
        out = _git(repo, "config", "--list", "--show-scope", "--name-only", "--includes")
        return {ln.split("\t", 1)[1] for ln in out.splitlines() if ln.startswith("local\t")}

    plain = _repo(tmp_path, _base_files(), name="plain")
    assert local_keys(plain) <= publish.ALLOWED_CONFIG_KEYS, local_keys(plain)
    code, out = _shape_problems(plain, ledger, capsys)
    assert code == 0, out

    src = _repo(tmp_path, _base_files(), ident=SOURCE_IDENT)
    out_dir = tmp_path / "cut"
    assert publish.main(_cut(src, out_dir, ledger)) == 0
    capsys.readouterr()
    assert {"core.autocrlf", "core.safecrlf", "core.logallrefupdates"} <= local_keys(out_dir)
    assert local_keys(out_dir) <= publish.ALLOWED_CONFIG_KEYS, local_keys(out_dir)
    code, out = _shape_problems(out_dir, ledger, capsys)
    assert code == 0, out
    assert _line(out, "cut-shape").startswith("PASS cut-shape")


def test_cut_shape_fails_core_hookspath(tmp_path, capsys):
    repo = _repo(tmp_path, _base_files())
    hooks = tmp_path / "elsewhere-hooks"
    hooks.mkdir()
    _git(repo, "config", "--local", "core.hooksPath", hooks.as_posix())
    code, out = _shape_problems(repo, _ledger(tmp_path, ["2026-09-01"]), capsys)
    assert code == 1, out
    assert _line(out, "cut-shape") == "FAIL cut-shape 1 problem(s); rebuild the cut"
    assert "core.hooksPath is set, want unset" in out


def test_cut_shape_fails_a_hook_file_and_allows_git_sample_hooks(tmp_path, capsys):
    ledger = _ledger(tmp_path, ["2026-09-01"])
    repo = _repo(tmp_path, _base_files())
    hooks = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "hooks"
    hooks.mkdir(exist_ok=True)
    # control: a sample is inert (git runs only `hooks/<hook name>`) and passes
    (hooks / "pre-push.sample").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    code, out = _shape_problems(repo, ledger, capsys)
    assert code == 0, out
    assert _line(out, "cut-shape").startswith("PASS cut-shape")

    # no executable bit: Git for Windows runs it anyway, so the mode is not what counts
    (hooks / "pre-push").write_text("#!/bin/sh\necho HOOK RAN\n", encoding="utf-8")
    code, out = _shape_problems(repo, ledger, capsys)
    assert code == 1, out
    assert _line(out, "cut-shape") == "FAIL cut-shape 1 problem(s); rebuild the cut"
    assert "1 hook file(s) besides git's inert *.sample files, want none" in out


def test_cut_shape_on_a_non_git_dir_exits_2(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "README.md").write_text("not a repo\n", encoding="utf-8")
    inside = subprocess.run(
        ["git", "-C", str(plain), "rev-parse", "--show-toplevel"], capture_output=True
    )
    if inside.returncode == 0:
        pytest.skip("tmp_path is inside another git repository")
    code = publish.main(_check(plain, _ledger(tmp_path, ["2026-09-01"])))
    _, err = capsys.readouterr()
    assert code == 2
    assert "CANNOT MEASURE" in err and "is not a git repository" in err


# --------------------------------------------------------------------------- #
# key-shapes
# --------------------------------------------------------------------------- #
def test_key_shapes_reports_counts_and_never_the_match(tmp_path, capsys):
    planted = _planted_keys()
    assert set(planted) == {name for name, _ in publish.KEY_SHAPES}, "a shape has no planted case"
    ledger = _ledger(tmp_path, ["2026-09-01"])
    for shape, (prefix, rand) in planted.items():
        value = prefix + rand
        rel = f"config/{shape}.txt"
        repo = _repo(tmp_path, {**_base_files(), rel: f"key = {value}\n".encode()}, name=shape)
        code = publish.main(_check(repo, ledger))
        out, err = capsys.readouterr()
        both = out + err
        assert code == 1, (shape, out)
        assert _line(out, "key-shapes").startswith("FAIL key-shapes 1 shape(s)")
        assert f"{shape}: 1 file(s): {rel}" in out
        assert value not in both
        for i in range(len(rand) - 7):
            assert rand[i : i + 8] not in both, (shape, i)


# --------------------------------------------------------------------------- #
# tracker-links
# --------------------------------------------------------------------------- #
def _issue_link(owner: str, repo: str, kind: str = "issues", n: int = 12) -> str:
    return "https://" + "github.com/" + f"{owner}/{repo}/{kind}/{n}"


def test_tracker_links_fails_on_a_planted_issue_link(tmp_path, capsys):
    files = {
        **_base_files(),
        "docs/a.md": f"line one\n[gh#12]({_issue_link('fixture-owner', 'other-repo')})\n".encode(),
    }
    repo = _repo(tmp_path, files)
    code = publish.main(_check(repo, _ledger(tmp_path, ["2026-09-01"])))
    out, _ = capsys.readouterr()
    assert code == 1
    assert _line(out, "tracker-links").startswith("FAIL tracker-links 1 line(s)")
    assert "docs/a.md:2" in out


def test_tracker_links_still_match_the_old_name_after_host_ref_is_renamed(tmp_path, capsys):
    ledger = _ledger(tmp_path, ["2026-09-01"])
    stale = {
        **_base_files(),
        "docs/a.md": f"[pr]({_issue_link('fixture-owner', 'fixture-repo', 'pull', 3)})\n".encode(),
    }
    repo = _repo(tmp_path, stale)
    code = publish.main(_check(repo, ledger, host_ref="fixture-owner/fixture-repo-private"))
    out, _ = capsys.readouterr()
    assert code == 1
    assert "docs/a.md:1" in out

    # control: another owner's link is not the canonical owner's and passes
    other = {
        **_base_files(),
        "docs/a.md": f"[x]({_issue_link('someone-else', 'fixture-repo')})\n".encode(),
    }
    repo2 = _repo(tmp_path, other, name="other")
    code = publish.main(_check(repo2, ledger, host_ref="fixture-owner/fixture-repo-private"))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "tracker-links").startswith("PASS tracker-links")


# --------------------------------------------------------------------------- #
# sha-citations (report only)
# --------------------------------------------------------------------------- #
def test_sha_citations_never_change_the_exit_code(tmp_path, capsys):
    history = _repo(tmp_path, {"a.txt": b"a\n"}, name="history")
    sha = _git(history, "rev-parse", "HEAD")
    files = {
        **_base_files(),
        "docs/record.md": (
            f"# Record\n\nlanded in {sha[:7]}, full {sha}\nsee {sha[:7]}, again {sha[:7]}\n"
        ).encode(),
    }
    repo = _repo(tmp_path, files)
    ledger = _ledger(tmp_path, ["2026-09-01"])
    code = publish.main(_check(repo, ledger, source=history))
    out, _ = capsys.readouterr()
    assert code == 0, out
    # line 4 names one sha twice: one citation, listed once
    assert _line(out, "sha-citations").startswith("REPORT sha-citations 3 in 1 file(s)")
    assert f"docs/record.md:3 {sha[:7]}" in out and f"docs/record.md:3 {sha}" in out
    assert out.count(f"docs/record.md:4 {sha[:7]}") == 1, out

    # and while a hard check fails, the report does not turn the 1 into anything else
    undated = _ledger(tmp_path, [""], name="undated.md")
    assert publish.main(_check(repo, undated, source=history)) == 1
    capsys.readouterr()


# --------------------------------------------------------------------------- #
# install-evidence (report only, gh#660)
# --------------------------------------------------------------------------- #
def _install_doc(tree_id: str) -> bytes:
    return (
        f"# Install\n\n**Last verified fresh-clone install:** tree `{tree_id}`,\n"
        "2026-09-26, a fixture container.\n"
    ).encode()


def test_install_evidence_counts_files_moved_since_the_verified_tree(tmp_path, capsys):
    verified_files = {
        **_base_files(),
        "docs/INSTALL.md": b"# Install\n\nnot verified yet\n",
        "a.md": b"v1\n",
    }
    history = _repo(tmp_path, verified_files, name="history")
    t1 = _git(history, "rev-parse", "HEAD^{tree}")
    ledger = _ledger(tmp_path, ["2026-09-01"])

    # only the Last-verified record moved: nothing else differs
    same = _repo(tmp_path, {**verified_files, "docs/INSTALL.md": _install_doc(t1)}, name="same")
    code = publish.main(_check(same, ledger, source=history))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "install-evidence") == (
        f"REPORT install-evidence 0 file(s) besides docs/INSTALL.md differ from verified tree "
        f"{t1} (never changes the exit code)"
    )

    # one file changed and one added since the drill ran: counted, and still exit 0
    moved = {**verified_files, "docs/INSTALL.md": _install_doc(t1), "a.md": b"v2\n", "b.md": b"b\n"}
    repo = _repo(tmp_path, moved, name="moved")
    code = publish.main(_check(repo, ledger, source=history))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _line(out, "install-evidence").startswith("REPORT install-evidence 2 file(s) besides")


def test_install_evidence_says_what_it_could_not_measure(tmp_path, capsys):
    ledger = _ledger(tmp_path, ["2026-09-01"])
    no_id = _repo(tmp_path, {**_base_files(), "docs/INSTALL.md": b"# Install\n"}, name="no-id")
    code = publish.main(_check(no_id, ledger))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert "cites no tree id; re-run the Linux drill" in _line(out, "install-evidence")

    absent = "0123456789abcdef" * 2 + "01234567"
    unknown = _repo(
        tmp_path, {**_base_files(), "docs/INSTALL.md": _install_doc(absent)}, name="unknown"
    )
    code = publish.main(_check(unknown, ledger))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert f"verified tree {absent} is not in the source checkout" in _line(out, "install-evidence")


def test_install_evidence_parses_the_real_last_verified_line():
    # The line's wording is the parser's contract; a reworded INSTALL.md must fail here, not
    # turn the report into a silent "cites no tree id".
    text = (REPO_ROOT / publish.INSTALL_DOC).read_text(encoding="utf-8")
    assert publish._LAST_VERIFIED_RE.search(text), "INSTALL.md's Last-verified line changed shape"


# --------------------------------------------------------------------------- #
# cut
# --------------------------------------------------------------------------- #
def _cut(source: Path, out: Path, ledger: Path, *extra: str, email: str = NOREPLY) -> list[str]:
    return [
        "cut",
        "--rev",
        "HEAD",
        "--source",
        str(source),
        "--out",
        str(out),
        "--author-email",
        email,
        "--ledger",
        str(ledger),
        "--host-ref",
        HOST_REF,
        *extra,
    ]


def _printed(out: str, key: str) -> str:
    hits = [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith(key + " ")]
    assert len(hits) == 1, out
    return hits[0]


def test_cut_keeps_skills_vendored_and_its_license(tmp_path, capsys):
    files = {
        **_base_files(),
        "skills/_vendored/upstream/SKILL.md": b"# Upstream skill\n",
        "skills/_vendored/upstream/LICENSE": b"Upstream license.\n",
        "skills/_vendored/other/refs/x.md": b"x\n",
    }
    src = _repo(tmp_path, files)
    out_dir = tmp_path / "cut"
    code = publish.main(_cut(src, out_dir, _ledger(tmp_path, ["2026-09-01"])))
    out, _ = capsys.readouterr()
    assert code == 0, out
    listed = set(_git(out_dir, "ls-files").splitlines())
    assert {
        "LICENSE",
        "skills/_vendored/upstream/SKILL.md",
        "skills/_vendored/upstream/LICENSE",
        "skills/_vendored/other/refs/x.md",
    } <= listed
    assert _printed(out, "files") == str(len(files))


def test_cut_drops_every_excluded_path_and_check_names_it(tmp_path, capsys):
    secret_body = "FIXTURE_CONTENT_" + _alnum(12)
    excluded = {
        ".env": secret_body.encode(),
        "svc/.env.local": secret_body.encode() + b"2",
        "certs/server.pem": secret_body.encode() + b"3",
        ".worktrees/lane/file.txt": secret_body.encode() + b"4",
    }
    kept_lookalikes = {"docs/env.md": b"env\n", "docs/.envrc-notes.md": b"n\n", "a.pem.md": b"m\n"}
    for path in excluded:
        assert publish.is_excluded(path), path
    for path in kept_lookalikes:
        assert not publish.is_excluded(path), path
    src = _repo(tmp_path, {**_base_files(), **excluded, **kept_lookalikes})
    out_dir = tmp_path / "cut"
    code = publish.main(_cut(src, out_dir, _ledger(tmp_path, ["2026-09-01"])))
    out, err = capsys.readouterr()
    assert code == 1
    assert _line(out, "excluded-paths").startswith("FAIL excluded-paths 0 in the cut, 4 dropped")
    for path in excluded:
        assert f"dropped by cut: {path}" in out
    assert secret_body not in out + err
    listed = set(_git(out_dir, "ls-files").splitlines())
    assert not listed & set(excluded)
    assert set(kept_lookalikes) <= listed
    # the dropped files' blobs never crossed into the cut's object store
    for path in excluded:
        oid = _git(src, "rev-parse", f"HEAD:{path}")
        probe = subprocess.run(
            ["git", "-C", str(out_dir), "cat-file", "-e", oid], capture_output=True
        )
        assert probe.returncode != 0, path
    # the tree is still built and its id printed when the gate fails
    assert re.fullmatch(r"[0-9a-f]{40}", _printed(out, "tree"))


#: gh#727: a directory named like an excluded file, and case variants of every pattern. None of
#: these may be on the path only through the last segment or an exact-case spelling.
CASE_AND_DIR_VARIANTS = (
    ".env/secrets",
    "cfg/.ENV",
    ".Worktrees/lane-a/file.txt",
    "cfg/server.PEM",
    "svc/.Env.Local",
    "deploy/.env.d/app.conf",
    "keys/bundle.pem/part1",
    "nested/.WORKTREES/b/c.md",
)


@pytest.mark.parametrize("path", CASE_AND_DIR_VARIANTS)
def test_is_excluded_matches_every_segment_ignoring_case(path):
    assert publish.is_excluded(path)


#: Names a Linux-built tree can carry that a Windows checkout reads as an excluded one.
WINDOWS_ALIASES = ("cfg\\.env", ".env ", "cfg/.env.", "certs/server.pem ", "a\\.Worktrees\\x.md")


@pytest.mark.parametrize("path", WINDOWS_ALIASES)
def test_is_excluded_reads_backslash_and_trailing_dots_as_windows_does(path):
    assert publish.is_excluded(path)


def _commit_with_extra_entries(repo: Path, names: tuple[str, ...]) -> None:
    """Commit HEAD's tree plus one blob per name, built with mktree so the name reaches the tree
    byte for byte: neither the index nor the filesystem gets a say in what it is."""
    def piped(*args: str, data: bytes) -> str:
        proc = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True)
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        return proc.stdout.decode().strip()

    oid = piped("hash-object", "-w", "--stdin", data=b"dummy\n")
    top = _git(repo, "ls-tree", "HEAD").splitlines()
    lines = top + [f"100644 blob {oid}\t{n}" for n in names]
    tree = piped("mktree", data=(NL.join(lines) + NL).encode("utf-8"))
    commit = _git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "extra")
    _git(repo, "update-ref", "refs/heads/master", commit)


def test_cut_drops_windows_alias_names_from_a_linux_built_tree(tmp_path, capsys):
    src = _repo(tmp_path, _base_files())
    names = ("cfg\\.env", ".env ", "certs\\server.PEM")
    _commit_with_extra_entries(src, names)
    out_dir = tmp_path / "cut"
    code = publish.main(_cut(src, out_dir, _ledger(tmp_path, ["2026-09-01"])))
    out, _ = capsys.readouterr()
    assert code == 1, out
    assert _line(out, "excluded-paths").startswith(
        f"FAIL excluded-paths 0 in the cut, {len(names)} dropped"
    ), out
    assert set(_git(out_dir, "ls-files").splitlines()) == set(_base_files())


def test_cut_fails_loudly_when_the_written_tree_is_short_of_the_kept_files(
    tmp_path, capsys, monkeypatch
):
    """Whatever drops an entry between the kept list and the tree, the cut says so and exits 2
    rather than printing a file count the tree does not hold. The drop is injected here so the
    check runs on every platform; the Windows-only test below produces it for real."""
    src = _repo(tmp_path, _base_files())
    real_run = publish._run

    def dropping_run(argv, **kw):
        if "--index-info" in argv:
            records = [r for r in kw["stdin"].split(b"\0") if r]
            kw["stdin"] = b"".join(r + b"\0" for r in records[:-1])
        return real_run(argv, **kw)

    monkeypatch.setattr(publish, "_run", dropping_run)
    code = publish.main(_cut(src, tmp_path / "cut", _ledger(tmp_path, ["2026-09-01"])))
    _, err = capsys.readouterr()
    assert code == 2, err
    n = len(_base_files())
    assert f"the cut's tree holds {n - 1} files, {n} were kept; not written: " in err, err


@pytest.mark.skipif(
    os.name != "nt", reason="only Git for Windows skips a name NTFS cannot hold, with exit 0"
)
def test_cut_fails_loudly_on_a_name_windows_git_silently_skips(tmp_path, capsys):
    src = _repo(tmp_path, _base_files())
    _commit_with_extra_entries(src, ("bad:name.txt",))
    code = publish.main(_cut(src, tmp_path / "cut", _ledger(tmp_path, ["2026-09-01"])))
    _, err = capsys.readouterr()
    assert code == 2, err
    assert "not written: bad:name.txt" in err, err


@pytest.mark.parametrize(
    "path",
    ("docs/env.md", ".envrc", "cfg/.ENVRC", "a.pem.md", "worktrees/x.md", "docs/.worktrees"),
)
def test_is_excluded_still_keeps_lookalikes(path):
    # `.worktrees/` is a directory pattern: a FILE of that name is not a worktree.
    assert not publish.is_excluded(path)


def test_cut_drops_directory_and_case_variants_and_counts_them(tmp_path, capsys):
    secret_body = "FIXTURE_CONTENT_" + _alnum(12)
    excluded = {p: (secret_body + str(i)).encode() for i, p in enumerate(CASE_AND_DIR_VARIANTS)}
    src = _repo(tmp_path, {**_base_files(), **excluded})
    out_dir = tmp_path / "cut two"
    code = publish.main(_cut(src, out_dir, _ledger(tmp_path, ["2026-09-01"])))
    out, err = capsys.readouterr()
    assert code == 1, out
    n = len(CASE_AND_DIR_VARIANTS)
    assert _line(out, "excluded-paths").startswith(
        f"FAIL excluded-paths 0 in the cut, {n} dropped"
    ), out
    for path in excluded:
        assert f"dropped by cut: {path}" in out
    assert secret_body not in out + err
    assert not set(_git(out_dir, "ls-files").splitlines()) & set(excluded)
    assert _printed(out, "files") == str(len(_base_files()))


def test_check_counts_excluded_paths_actually_present_in_the_tree(tmp_path, capsys):
    """The in-cut count is read from the tree's own HEAD, so a tree built by hand (or by an
    older cut) that carries these paths is never reported as 0."""
    repo = _repo(tmp_path, {**_base_files(), **{p: b"dummy\n" for p in CASE_AND_DIR_VARIANTS}})
    code = publish.main(_check(repo, _ledger(tmp_path, ["2026-09-01"])))
    out, _ = capsys.readouterr()
    assert code == 1, out
    n = len(CASE_AND_DIR_VARIANTS)
    assert _line(out, "excluded-paths").startswith(f"FAIL excluded-paths {n} in the cut, 0"), out
    listed = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("in cut: ")]
    assert sorted(listed) == sorted(f"in cut: {p}" for p in CASE_AND_DIR_VARIANTS)


def test_cut_is_one_parentless_commit_with_no_remote_and_no_source_object(
    tmp_path, capsys, monkeypatch
):
    global_cfg = tmp_path / "global.gitconfig"
    global_cfg.write_text(
        "[user]\n\tname = Ambient Global Person\n\temail = ambient-global@fixture.invalid\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    src = _repo(tmp_path, _base_files(), ident=SOURCE_IDENT)
    source_sha = _git(src, "rev-parse", "HEAD")
    out_dir = tmp_path / "cut"
    code = publish.main(_cut(src, out_dir, _ledger(tmp_path, ["2026-09-01"])))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert _printed(out, "source") == source_sha
    assert _git(out_dir, "rev-list", "--count", "HEAD") == "1"
    assert _git(out_dir, "rev-list", "--parents", "-n", "1", "HEAD").split() == [
        _git(out_dir, "rev-parse", "HEAD")
    ]
    assert _printed(out, "commit") == _git(out_dir, "rev-parse", "HEAD")
    assert _git(out_dir, "remote") == ""
    assert _git(out_dir, "for-each-ref", "--format=%(refname)") == "refs/heads/master"
    assert _git(out_dir, "symbolic-ref", "HEAD") == "refs/heads/master"
    probe = subprocess.run(
        ["git", "-C", str(out_dir), "cat-file", "-e", source_sha], capture_output=True
    )
    assert probe.returncode != 0, "the source commit object is in the cut"
    assert not (out_dir / ".git" / "objects" / "info" / "alternates").exists()
    ident = _git(out_dir, "log", "-1", "--format=%an|%ae|%cn|%ce")
    assert ident == f"agentflow|{NOREPLY}|agentflow|{NOREPLY}"
    assert _git(out_dir, "log", "-1", "--format=%B") == publish.COMMIT_MESSAGE.strip()
    assert _git(out_dir, "status", "--porcelain") == ""
    assert _git(out_dir, "config", "--local", "core.autocrlf") == "false"
    # a reflog would carry the operator's OS account and hostname, and hooks the operator's
    # template; the cut holds neither
    logs = out_dir / ".git" / "logs"
    assert not logs.exists() or not any(p.is_file() for p in logs.rglob("*")), "cut has a reflog"
    hooks = out_dir / ".git" / "hooks"
    assert not hooks.exists() or not any(hooks.iterdir()), "cut carries template hooks"
    # the source repo's refs are untouched
    assert _git(src, "for-each-ref", "--format=%(refname)") == "refs/heads/master"
    assert _git(src, "rev-parse", "HEAD") == source_sha


def test_cut_tree_id_equals_source_tree_when_nothing_is_excluded(tmp_path, capsys):
    files = {
        **_base_files(),
        "bin/run.sh": b"#!/bin/sh\necho hi\n",
        "docs/crlf.txt": b"line one\r\nline two\r\n",
        "docs/mixed.txt": b"lf\ncrlf\r\n",
    }
    src = _repo(tmp_path, files, executable=("bin/run.sh",))
    assert "100755" in _git(src, "ls-tree", "HEAD", "bin/run.sh")
    blob = subprocess.run(
        ["git", "-C", str(src), "cat-file", "-p", "HEAD:docs/crlf.txt"],
        capture_output=True,
        check=True,
    ).stdout
    assert blob == files["docs/crlf.txt"]  # the CRLF is in the source blob itself
    out_dir = tmp_path / "cut"
    code = publish.main(_cut(src, out_dir, _ledger(tmp_path, ["2026-09-01"])))
    out, _ = capsys.readouterr()
    assert code == 0, out
    source_tree = _git(src, "rev-parse", "HEAD^{tree}")
    assert _printed(out, "tree") == source_tree
    assert _git(out_dir, "rev-parse", "HEAD^{tree}") == source_tree
    assert (out_dir / "docs" / "crlf.txt").read_bytes() == files["docs/crlf.txt"]
    assert "100755" in _git(out_dir, "ls-tree", "HEAD", "bin/run.sh")


@pytest.mark.parametrize(
    "email",
    [
        "someone@example.com",
        "someone@users.noreply.github.com.evil.example",
        "x+someone@users.noreply.github.com",
        "",
        "@users.noreply.github.com",
    ],
)
def test_cut_refuses_a_non_noreply_author_email(tmp_path, capsys, email):
    src = _repo(tmp_path, _base_files())
    out_dir = tmp_path / "cut"
    code = publish.main(_cut(src, out_dir, _ledger(tmp_path, ["2026-09-01"]), email=email))
    _, err = capsys.readouterr()
    assert code == 2
    assert "REFUSED" in err
    assert not out_dir.exists()
    for good in (NOREPLY, "fixture-bot@users.noreply.github.com"):
        assert publish.NOREPLY_EMAIL_RE.match(good)


def test_cut_refuses_a_non_empty_out_dir(tmp_path, capsys):
    src = _repo(tmp_path, _base_files())
    out_dir = tmp_path / "cut"
    out_dir.mkdir()
    keep = out_dir / "keep.txt"
    keep.write_bytes(b"do not touch\n")
    code = publish.main(_cut(src, out_dir, _ledger(tmp_path, ["2026-09-01"])))
    _, err = capsys.readouterr()
    assert code == 2
    assert "REFUSED" in err
    assert keep.read_bytes() == b"do not touch\n"
    assert sorted(p.name for p in out_dir.iterdir()) == ["keep.txt"]

    a_file = tmp_path / "a-file"
    a_file.write_bytes(b"x")
    assert publish.main(_cut(src, a_file, _ledger(tmp_path, ["2026-09-01"], name="l2.md"))) == 2
    capsys.readouterr()


def _write_config(home: Path, name: str, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / name).write_text(
        f"# config{NL}{NL}```yaml{NL}{textwrap.dedent(body).strip()}{NL}```{NL}", encoding="utf-8"
    )


def test_cut_source_defaults_to_the_project_local_checkout(tmp_path, capsys, monkeypatch):
    src = _repo(tmp_path, _base_files())
    d = str(tmp_path).replace(chr(92), "/")
    data_root = tmp_path / "data"
    status = data_root / "projects" / "demo" / "status"
    status.mkdir(parents=True)
    shutil.copy(_ledger(tmp_path, ["2026-09-01", "2026-09-02"]), status / publish.LEDGER_FILENAME)
    home = tmp_path / "config"
    _write_config(
        home,
        "environment.md",
        f"""
        schema_version: 1
        person:
          name: Sample User
        data_root: {str(data_root).replace(chr(92), "/")}
        git_host:
          kind: local
        notify:
          kind: none
        lock_dir: {d}
        shell: posix
        suite_root: {d}
        worktree_root: {d}
    """,
    )
    _write_config(
        home / "projects",
        "demo.md",
        f"""
        schema_version: 1
        name: demo
        canonical_repo:
          host_ref: {HOST_REF}
          local_checkout: {str(src).replace(chr(92), "/")}
        verification:
          strategy: test-suite
    """,
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    out_dir = tmp_path / "cut"
    code = publish.main(
        [
            "cut",
            "--rev",
            "HEAD",
            "--out",
            str(out_dir),
            "--author-email",
            NOREPLY,
            "--project",
            "demo",
        ]
    )
    out, err = capsys.readouterr()
    assert code == 0, out + err
    assert _printed(out, "source") == _git(src, "rev-parse", "HEAD")
    assert _line(out, "ledger-dated") == "PASS ledger-dated 2 of 2 rows dated"


# --------------------------------------------------------------------------- #
# --project resolution is per field (gh#657)
# --------------------------------------------------------------------------- #
def _scratch_home(tmp_path: Path, src: Path, monkeypatch, *, notify: str) -> Path:
    """A scratch AGENTFLOW_CONFIG home: one environment file and one ``demo`` project."""
    d = str(tmp_path).replace(chr(92), "/")
    (tmp_path / "data").mkdir()
    home = tmp_path / "config"
    _write_config(
        home,
        "environment.md",
        f"""
        schema_version: 1
        person:
          name: Sample User
        data_root: {d}/data
        git_host:
          kind: local
        notify:
{textwrap.indent(textwrap.dedent(notify).strip(), " " * 10)}
        lock_dir: {d}
        shell: posix
        suite_root: {d}
        worktree_root: {d}
    """,
    )
    _write_config(
        home / "projects",
        "demo.md",
        f"""
        schema_version: 1
        name: demo
        canonical_repo:
          host_ref: {HOST_REF}
          local_checkout: {str(src).replace(chr(92), "/")}
        verification:
          strategy: test-suite
    """,
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(home))
    monkeypatch.delenv("AGENTFLOW_PROJECT", raising=False)
    return home


def test_an_unrelated_environment_error_blocks_only_the_data_root_paths(
    tmp_path, capsys, monkeypatch
):
    """The ledger, the sensitive-terms list and the extra sanitize pattern all live under the
    data root, so an environment error blanks those three and nothing else (gh#791 added the
    second, gh#821 the third)."""
    unset = "AF_FIXTURE_UNSET_" + secrets.token_hex(4).upper()
    monkeypatch.delenv(unset, raising=False)
    src = _repo(tmp_path, _base_files())
    _scratch_home(
        tmp_path, src, monkeypatch, notify=f"kind: ntfy\nurl_env: {unset}\ntopic_env: {unset}"
    )
    args = publish.build_parser().parse_args(["check", "--tree", str(src), "--project", "demo"])
    r = publish._resolve(args)
    assert r.host_ref == HOST_REF
    assert r.source == src
    assert r.ledger is None and r.sensitive_terms is None and r.sanitize_extra is None
    assert set(r.why) == {"ledger", "sensitive_terms", "sanitize_extra"}
    assert "notify.url_env" in r.why["ledger"] and unset in r.why["ledger"]
    assert r.why["sensitive_terms"] == r.why["ledger"] == r.why["sanitize_extra"]

    code = publish.main(["check", "--tree", str(src), "--project", "demo"])
    out, _ = capsys.readouterr()
    assert code == 2, out
    ledger_line = _line(out, "ledger-dated")
    assert ledger_line.startswith(
        "FAIL ledger-dated cannot measure - no ledger path: the environment config did not load"
    )
    assert unset in ledger_line and "pass --ledger" not in ledger_line
    assert _line(out, "sensitive-terms").startswith(
        "FAIL sensitive-terms cannot measure - no terms list path: the environment config"
    )
    assert _line(out, "sanitize-terms").startswith(
        "FAIL sanitize-terms cannot measure - no extra pattern path: the environment config"
    )
    assert _line(out, "tracker-links").startswith("PASS tracker-links")
    assert _line(out, "sha-citations").startswith("REPORT sha-citations 0 in 0 file(s)")


def test_a_missing_project_file_is_named_not_blamed_on_a_flag(tmp_path, capsys, monkeypatch):
    src = _repo(tmp_path, _base_files())
    _scratch_home(tmp_path, src, monkeypatch, notify="kind: none")
    code = publish.main(["check", "--tree", str(src), "--project", "ghost"])
    out, err = capsys.readouterr()
    assert code == 2, out
    for name in ("ledger-dated", "tracker-links"):
        line = _line(out, name)
        assert "project 'ghost' did not load" in line and "config file not found" in line, line
        assert "pass --" not in line, line
    assert "project 'ghost' did not load" in _line(out, "sha-citations")
    assert "config file not found" in err  # the stderr note names it too

    code = publish.main(
        ["cut", "--rev", "HEAD", "--out", str(tmp_path / "cut"), "--author-email", NOREPLY]
        + ["--ledger", str(_ledger(tmp_path, ["2026-09-01"])), "--project", "ghost"]
    )
    _, err = capsys.readouterr()
    assert code == 2
    assert "REFUSED - no source checkout: project 'ghost' did not load" in err
    assert "pass --source" not in err


def test_without_a_project_check_and_cut_refuse_even_with_a_session_default(
    tmp_path, capsys, monkeypatch
):
    """gh#729: the session default resolved a whole project, so an all-PASS run built from
    another project's ledger and checkout looked exactly like a correct one."""
    src = _repo(tmp_path, _base_files())
    _scratch_home(tmp_path, src, monkeypatch, notify="kind: none")
    monkeypatch.setenv("AGENTFLOW_PROJECT", "demo")  # resolvable, and still never used
    ledger = str(_ledger(tmp_path, ["2026-09-01"]))
    for argv, missing in (
        (["check", "--tree", str(src)], "--ledger, --source, --host-ref not given"),
        (["check", "--tree", str(src), "--ledger", ledger, "--host-ref", HOST_REF], "--source "),
    ):
        code = publish.main(argv)
        out, err = capsys.readouterr()
        assert code == 2, out
        assert out == ""  # refused before any check line, so nothing on screen reads as a PASS
        assert "publish check: REFUSED - no project named and " in err and missing in err, err
        assert "pass --project <name>, or all of --ledger, --source and --host-ref" in err

    out_dir = tmp_path / "cut"
    code = publish.main(["cut", "--rev", "HEAD", "--out", str(out_dir), "--author-email", NOREPLY])
    _, err = capsys.readouterr()
    assert code == 2
    assert "publish cut: REFUSED - no project named" in err, err
    assert not out_dir.exists()


def test_the_first_line_names_the_project_ledger_and_source(tmp_path, capsys, monkeypatch):
    src = _repo(tmp_path, _base_files())
    _scratch_home(tmp_path, src, monkeypatch, notify="kind: none")
    code = publish.main(["check", "--tree", str(src), "--project", "demo"])
    out, _ = capsys.readouterr()
    first = out.splitlines()[0]
    assert code == 2, out  # the scratch data root holds no ledger
    assert first.startswith("resolved project demo; ledger "), first
    ledger = Path("projects") / "demo" / "status" / publish.LEDGER_FILENAME
    assert str(ledger) in first, first
    assert first.endswith(f"; source {src}; host-ref {HOST_REF}"), first

    ledger = _ledger(tmp_path, ["2026-09-01"])
    code = publish.main(_cut(src, tmp_path / "cut", ledger))
    out, _ = capsys.readouterr()
    assert code == 0, out
    assert out.splitlines()[0] == (
        f"resolved project (none: explicit flags); ledger {ledger}; source {src}; "
        f"host-ref {HOST_REF}"
    )
    assert out.splitlines()[1].startswith("source ")  # the sha line follows it

    # a project named beside all five flags supplies nothing, and says so
    terms = tmp_path / "no-terms.txt"
    extra = tmp_path / "no-extra.txt"
    publish.main(
        _check(
            src, ledger, "--project", "demo", "--sensitive-terms", str(terms),
            "--sanitize-extra-pattern", str(extra),
        )
    )
    out, _ = capsys.readouterr()
    assert out.splitlines()[0].startswith(
        f"resolved project demo (unused: all flags explicit); ledger {ledger}; "
    ), out
    # beside four of them it still supplies the one left out, and so is not unused
    publish.main(_check(src, ledger, "--project", "demo", "--sensitive-terms", str(terms)))
    out, _ = capsys.readouterr()
    assert out.splitlines()[0].startswith(f"resolved project demo; ledger {ledger}; "), out
    want = Path("projects") / "demo" / "publish" / "sanitize-extra-pattern.txt"
    assert str(want) in _line(out, "sanitize-terms"), out
    # beside the three routing flags alone it still supplies the sensitive-terms path
    publish.main(_check(src, ledger, "--project", "demo"))
    out, _ = capsys.readouterr()
    assert out.splitlines()[0].startswith(f"resolved project demo; ledger {ledger}; "), out
    want = Path("projects") / "demo" / publish.SENSITIVE_TERMS_RELPATH
    assert str(want) in _line(out, "sensitive-terms"), out


def test_cut_never_invokes_push_remote_or_gh(tmp_path, capsys, monkeypatch):
    src = _repo(tmp_path, {**_base_files(), ".env": b"X=1\n"})
    out_dir = tmp_path / "cut"
    calls: list[list[str]] = []
    real_popen = subprocess.Popen

    class RecordingPopen(real_popen):
        def __init__(self, args, *a, **k):
            calls.append([str(x) for x in (args if isinstance(args, (list, tuple)) else [args])])
            super().__init__(args, *a, **k)

    def no_system(*_a, **_k):
        raise AssertionError("os.system is never used")

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    monkeypatch.setattr(publish.os, "system", no_system)
    code = publish.main(_cut(src, out_dir, _ledger(tmp_path, ["2026-09-01"])))
    capsys.readouterr()
    assert code == 1  # the planted .env is dropped and named
    assert calls, "nothing was recorded; the recorder is not wired"
    forbidden = {
        "push",
        "fetch",
        "clone",
        "remote",
        "ls-remote",
        "pull",
        "send-pack",
        "submodule",
        "gh",
    }
    allowed_dirs = {str(src), str(out_dir)}
    for argv in calls:
        assert Path(argv[0]).stem == "git", argv
        assert not forbidden & set(argv), argv
        assert argv[1] == "-C" and argv[2] in allowed_dirs, argv


# --------------------------------------------------------------------------- #
# The gate over its own checkout
# --------------------------------------------------------------------------- #
def _host_ref_from_origin(root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True
    )
    url = proc.stdout.decode("utf-8", "replace").strip()
    m = re.search(r"[:/]([^/:@\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
    # never print the URL: a clone URL can carry an inline token
    assert proc.returncode == 0 and m, "cannot parse origin into <owner>/<repo>"
    return f"{m.group(1)}/{m.group(2)}"


def test_the_gate_passes_over_a_cut_of_its_own_checkout(tmp_path, capsys):
    root = Path(_git(REPO_ROOT, "rev-parse", "--show-toplevel"))
    host_ref = _host_ref_from_origin(root)
    ledger = _ledger(tmp_path, ["2026-09-01"] * 3)
    out_dir = tmp_path / "cut"
    code = publish.main(
        [
            "cut",
            "--rev",
            "HEAD",
            "--source",
            str(root),
            "--out",
            str(out_dir),
            "--author-email",
            NOREPLY,
            "--ledger",
            str(ledger),
            "--host-ref",
            host_ref,
        ]
    )
    out, err = capsys.readouterr()
    hard = (
        "cut-shape",
        "ledger-dated",
        "session-urls",
        "sanitize-terms",
        "sensitive-terms",
        "key-shapes",
        "excluded-paths",
        "tracker-links",
        "runbook-present",
    )
    for name in hard:
        assert _line(out, name).startswith(f"PASS {name}"), out
    assert code == 0, out + err
    assert _printed(out, "tree") == _git(root, "rev-parse", "HEAD^{tree}")

    # control: one planted link under the same owner turns tracker-links red
    owner = host_ref.split("/", 1)[0]
    (out_dir / "planted.md").write_text(
        f"[x]({_issue_link(owner, 'x', 'issues', 1)})\n", encoding="utf-8"
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e.st",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e.st",
    }
    _git(out_dir, "add", "planted.md", env=env)
    _git(out_dir, "commit", "-q", "-m", "plant", env=env)
    code = publish.main(
        [
            "check",
            "--tree",
            str(out_dir),
            "--ledger",
            str(ledger),
            "--host-ref",
            host_ref,
            "--source",
            str(root),
        ]
    )
    out, _ = capsys.readouterr()
    assert code == 1
    assert _line(out, "tracker-links").startswith("FAIL tracker-links 1 line(s)")
    assert "planted.md:1" in out
    # and the second commit, by a non-cut identity, is what cut-shape exists to catch (gh#654)
    assert _line(out, "cut-shape").startswith("FAIL cut-shape")
