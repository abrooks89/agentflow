"""Tests for `find_vault_candidates` and the doctor line that offers them (E-0200 item 7).

This runs during install, so the load-bearing property is that it CANNOT fail: an
unreadable directory yields fewer candidates, never an exception, because "no vault" is a
first-class answer and the question must never block the install.
"""

from __future__ import annotations

from agentflow import doctor
from agentflow.config import find_vault_candidates


def _vault(root, *parts):
    d = root.joinpath(*parts)
    (d / ".obsidian").mkdir(parents=True)
    return d


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def test_a_directory_with_dot_obsidian_is_a_vault(tmp_path):
    """`.obsidian/` is the marker Obsidian itself writes, so this finds real vaults rather
    than folders somebody named hopefully."""
    v = _vault(tmp_path, "CODE", "my-vault")
    assert find_vault_candidates(tmp_path) == [str(v.resolve())]


def test_a_directory_WITHOUT_the_marker_is_not_offered(tmp_path):
    (tmp_path / "CODE" / "notes").mkdir(parents=True)
    assert find_vault_candidates(tmp_path) == []


def test_the_home_root_itself_can_be_a_vault(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    assert str(tmp_path.resolve()) in find_vault_candidates(tmp_path)


def test_search_is_SHALLOW_by_design(tmp_path):
    """A deep walk costs seconds on every install and turns up other people's backups. A
    miss costs one typed path, which the interview already accepts."""
    deep = tmp_path / "CODE" / "a" / "b" / "c" / "deep-vault"
    (deep / ".obsidian").mkdir(parents=True)
    assert find_vault_candidates(tmp_path) == []
    assert str(deep.resolve()) in find_vault_candidates(tmp_path, depth=4)


def test_results_are_sorted_and_deduplicated(tmp_path):
    """The same vault reachable from two search roots must be offered once. A list with a
    duplicate reads as two different vaults with the same name."""
    _vault(tmp_path, "CODE", "b-vault")
    _vault(tmp_path, "CODE", "a-vault")
    got = find_vault_candidates(tmp_path)
    assert got == sorted(got)
    assert len(got) == len(set(got))


def test_hidden_directories_are_not_descended(tmp_path):
    """`.git`, `.venv` and friends hold thousands of directories and no vaults, and
    descending them is most of what a slow search spends its time on."""
    hidden = tmp_path / ".cache" / "vault"
    (hidden / ".obsidian").mkdir(parents=True)
    assert find_vault_candidates(tmp_path) == []


# --------------------------------------------------------------------------- #
# It cannot fail
# --------------------------------------------------------------------------- #
def test_a_missing_home_returns_an_empty_list_rather_than_raising(tmp_path):
    assert find_vault_candidates(tmp_path / "does-not-exist") == []


def test_an_unreadable_directory_costs_one_candidate_not_the_search(tmp_path, monkeypatch):
    """THE INSTALL-BLOCKING PROPERTY. One permission error must not take the whole search
    down, because the search failing is not a reason the install cannot proceed."""
    good = _vault(tmp_path, "CODE", "readable")
    (tmp_path / "Documents").mkdir()

    real_iterdir = type(tmp_path).iterdir

    def selective(self):
        if self.name == "Documents":
            raise PermissionError("nope")
        return real_iterdir(self)

    monkeypatch.setattr(type(tmp_path), "iterdir", selective)
    assert find_vault_candidates(tmp_path) == [str(good.resolve())]


# --------------------------------------------------------------------------- #
# The doctor line
# --------------------------------------------------------------------------- #
class _Cfg:
    vault = None


def test_doctor_OFFERS_what_it_found_when_no_vault_is_configured(monkeypatch):
    """The UNASKED case: nobody has answered, so naming real paths turns the line from a
    shrug into something actionable. An install that ANSWERED NO takes the branch below
    instead - it used to land here too, which is why the offer never stopped."""
    monkeypatch.setattr(
        "agentflow.config.find_vault_candidates", lambda *a, **k: ["/x/one", "/x/two"]
    )
    findings = doctor._check_vault(_Cfg(), _Cfg())
    assert len(findings) == 1
    assert findings[0].status == doctor.INFO
    assert "/x/one" in findings[0].detail and "2 found" in findings[0].detail


def test_doctor_names_the_null_convention_in_its_remediation(monkeypatch):
    """The remedy has to name BOTH answers. Suggesting only "set vault.root" reads as an
    instruction to acquire a vault, when declining is equally correct - and writing the
    null is what stops this line reappearing every run."""
    monkeypatch.setattr("agentflow.config.find_vault_candidates", lambda *a, **k: ["/x/one"])
    finding = doctor._check_vault(_Cfg(), _Cfg())[0]
    assert "vault: null" in (finding.remediation or "")
    assert "vault.root" in (finding.remediation or "")


def test_doctor_says_so_when_the_disk_has_none_either(monkeypatch):
    """Distinct from "found some": nothing to offer is a different situation from an
    unasked question, and a reader deciding what to do next needs to tell them apart."""
    monkeypatch.setattr("agentflow.config.find_vault_candidates", lambda *a, **k: [])
    finding = doctor._check_vault(_Cfg(), _Cfg())[0]
    assert finding.status == doctor.INFO
    assert "none found on disk" in finding.detail


def test_doctor_truncates_a_long_list_and_says_how_many_it_hid(monkeypatch):
    """A diagnostic that prints twenty paths is a diagnostic nobody reads to the end of."""
    monkeypatch.setattr(
        "agentflow.config.find_vault_candidates", lambda *a, **k: [f"/x/{i}" for i in range(7)]
    )
    detail = doctor._check_vault(_Cfg(), _Cfg())[0].detail
    assert "7 found" in detail and "+4 more" in detail
    assert "/x/5" not in detail


def test_a_CONFIGURED_vault_is_never_offered_candidates(tmp_path):
    """The offer belongs only to the unconfigured case. Listing alternatives to somebody
    who already chose is noise, and noise is how a diagnostic dies."""

    class HasVault:
        class vault:  # noqa: N801 - a stand-in for the resolved dataclass
            root = str(tmp_path)
            subdir = "wiki/epics"
            mirror_helper = None
            save_skill = None
            autodoc_update_skill = None
            hot_cache = None

    findings = doctor._check_vault(HasVault(), HasVault())
    assert not any("found on disk" in f.detail for f in findings)


# --------------------------------------------------------------------------- #
# The recorded decline (E-0200 item 7 correction)
# --------------------------------------------------------------------------- #
class _Declined:
    vault = None
    vault_declined = True


def test_doctor_STOPS_offering_once_the_answer_is_RECORDED(monkeypatch):
    """The convention was documented in three places as the thing that stops this line
    reappearing, and nothing read it: `vault: null` and an absent key both arrived as
    vault=None. A diagnostic that re-asks a settled question is one people learn to skim."""
    monkeypatch.setattr(
        "agentflow.config.find_vault_candidates", lambda *a, **k: ["/x/one", "/x/two"]
    )
    findings = doctor._check_vault(_Declined(), _Declined())
    assert len(findings) == 1
    assert findings[0].status == doctor.INFO
    assert "declined" in findings[0].detail
    assert "found on disk" not in findings[0].detail


def test_the_declined_branch_never_SEARCHES_the_disk(monkeypatch):
    """Not merely that the paths are hidden: the answer is settled, so the walk is work
    nobody asked for. Raising from the search proves the branch returns before it."""

    def _boom(*a, **k):
        raise AssertionError("searched the disk after the question was answered")

    monkeypatch.setattr("agentflow.config.find_vault_candidates", _boom)
    assert doctor._check_vault(_Declined(), _Declined())[0].status == doctor.INFO


def test_a_decline_recorded_on_EITHER_side_is_a_decline(monkeypatch):
    """The flag is only ever consulted when no vault resolved, so an environment decline
    and a project decline are the same answer arriving from different files - ranking them
    would let a project that was never asked re-open a machine-level decision."""
    monkeypatch.setattr("agentflow.config.find_vault_candidates", lambda *a, **k: ["/x/one"])
    assert "declined" in doctor._check_vault(_Declined(), _Cfg())[0].detail
    assert "declined" in doctor._check_vault(_Cfg(), _Declined())[0].detail


def test_an_UNASKED_install_still_gets_the_offer(monkeypatch):
    """The regression this fix could cause. Silencing the unfinished case would hide the
    one prompt that finishes the install."""
    monkeypatch.setattr("agentflow.config.find_vault_candidates", lambda *a, **k: ["/x/one"])
    assert "found on disk" in doctor._check_vault(_Cfg(), _Cfg())[0].detail
