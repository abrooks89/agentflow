"""gh#103: keep PLR0917 LIVE in this subtree, and keep the two wide signatures keyword-only.

The root `ruff.toml` selects only `["E4", "E7", "E9", "F"]`, so this looks impossible from the
repo root — but ruff resolves config per file against the NEAREST config, and this subtree's
`pyproject.toml` declares a much wider set including blanket `PL`. So `ruff check .` run from the
repo root enforces the pylint family HERE and nowhere else in the tree.

`PLR0917` (too-many-positional-arguments) was preview-gated at the pinned ruff 0.15.10 and stable
by 0.16.5, so blanket `PL` started enforcing it on a version bump with no change to any config or
source: two errors, in two files the upgrader did not touch.

**The fix was at the source, not the config, and the reason is measured rather than stylistic.**
`PLR0913` (already ignored above it) pardons ARITY, which this adapter genuinely has — `query()`
takes 19 parameters. `PLR0917` measures POSITIONAL-ness, which is a discipline this subtree
already keeps: 10 of its 12 wide signatures were keyword-only before this change, including
`traverse()` in the same file as one of the two offenders. Ignoring it subtree-wide would have
retired the only rule enforcing a convention the code already follows.

Two things ruled out along the way, both by running the commands rather than reasoning:

- A subtree-wide `ignore = [... "PLR0917"]` works, but retires that rule for every future
  signature.
- A line-level `# noqa: PLR0917` on the one awkward site is WORSE THAN EITHER: on the pinned
  0.15.10 the rule is preview-gated, so the directive is unused and `RUF100` fires — the fix
  would have broken CI today to prevent a break tomorrow.

WHAT THIS MODULE DOES NOT DO: it does not run ruff. The graph CI job installs `.[test]` (pytest,
networkx, sqlite-vec) and has no ruff, so a behavioural lint check here would SKIP on every CI
run — an assertion that passes on absence. The signature test below is version-independent and
needs no linter, which is why it is the one that actually protects the fix.
"""

from __future__ import annotations

import importlib
import inspect
import tomllib
from pathlib import Path

import pytest

GRAPH_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = GRAPH_ROOT / "pyproject.toml"

# ruff's own default for PLR0917.
MAX_POSITIONAL = 5


@pytest.fixture(scope="module")
def lint_cfg() -> dict:
    assert PYPROJECT.is_file(), f"graph pyproject not found at {PYPROJECT}"
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["tool"]["ruff"]["lint"]


def test_the_pylint_family_is_still_selected_here(lint_cfg):
    """The precondition that makes everything below load-bearing.

    If `PL` is ever dropped from `select`, PLR0917 stops being enforced here at all and the rest
    of this module guards nothing. Fail here instead, so a narrowed select is a decision someone
    makes on purpose rather than one that quietly empties a gate.
    """
    select = lint_cfg["select"]
    assert "PL" in select, (
        f"blanket PL is no longer selected (select={select}); this module's premise needs "
        "revisiting rather than its assertions relaxing"
    )


def test_this_config_is_not_superseded_by_a_sibling_ruff_toml():
    """Reading a config that ruff no longer obeys would make every assertion here decorative.

    ruff prefers a sibling `.ruff.toml` over `[tool.ruff]` in `pyproject.toml`. If one appeared,
    the tests here would keep passing against the dead block while the live settings said
    something else entirely — green guard, red gate.
    """
    for name in (".ruff.toml", "ruff.toml"):
        assert not (GRAPH_ROOT / name).exists(), (
            f"{name} sits beside pyproject.toml and takes precedence over its [tool.ruff] block; "
            "the assertions in this module now read a config ruff ignores"
        )


def test_plr0917_is_deliberately_not_ignored_here(lint_cfg):
    """gh#103 was fixed at the source, so the rule must stay enforced.

    Adding it to `ignore` would make this subtree green again and silently retire the check on
    every future signature — which is the outcome this fix chose against.
    """
    assert "PLR0917" not in lint_cfg["ignore"], (
        "PLR0917 has been added to ignore. That re-greens the subtree by retiring the rule "
        "rather than fixing the signature, which gh#103 explicitly decided against: PLR0913 "
        "above pardons arity, PLR0917 measures positional-ness, and this code already keeps "
        "that discipline nearly everywhere."
    )


@pytest.mark.parametrize("dotted", ["autodoc_query.router:route", "autodoc_query.local:_Node.__init__"])
def test_the_two_wide_signatures_stay_keyword_only(dotted):
    """The actual protection, and it needs no linter to run.

    These are the two signatures gh#103 surfaced. Both are now keyword-only past the point that
    matters, so PLR0917 is clean by construction rather than by suppression. A revert to
    positional would pass the pinned ruff (preview-gated there) and only redden on the NEXT
    version bump — the exact latency that made gh#103 a trap in the first place. This assertion
    has no such gap: it fails immediately, on any ruff, and on none.
    """
    modpath, _, attr = dotted.partition(":")
    obj = importlib.import_module(modpath)
    for part in attr.split("."):
        obj = getattr(obj, part)

    params = list(inspect.signature(obj).parameters.values())
    positional = [
        p for p in params
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.name not in ("self", "cls")
    ]
    assert len(positional) <= MAX_POSITIONAL, (
        f"{dotted} takes {len(positional)} positional-capable parameters "
        f"({[p.name for p in positional]}), over PLR0917's limit of {MAX_POSITIONAL}. "
        "Keep the wide tail keyword-only; that is what makes this subtree pass on a newer ruff."
    )
