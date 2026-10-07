"""`Reading[T]`: a read of the outside world that says which of the five evidence states it is in.

The rule lives in `skills/_shared/EVIDENCE.md`. A read of `gh`, git, HTTP, ntfy or another seat's
files can come back five ways, and only two of them are complete:

* ``value``       - looked, complete, found something;
* ``empty``       - looked, complete, found nothing;
* ``unavailable`` - could not look (the command failed, the file is missing, the host is down);
* ``truncated``   - looked, but saw only part (a full page, a parse that stopped early);
* ``stale``       - looked, but the answer is older than what it is about to gate.

Collapsing the last three into ``empty`` is the failure this module exists to make hard: a full
page of 400 issues reads exactly like "400 issues", and a failed `gh` call that returns ``[]``
reads exactly like "no open PRs". A `Reading` carries the state with the value, so the caller
that gates on it must ask (``require_complete``) and the report that prints it must say what it
covered (``describe``).

WHY THE INCOMPLETE CONSTRUCTORS REFUSE A BLANK REASON. An ``unavailable`` with no reason, a
``truncated`` with no coverage note, or a ``stale`` with no ``as_of`` is a silent failure wearing
the right label. Each raises ``ValueError`` at construction, so the gap shows at the read site and
not three modules later in a report that cannot say why.

Frozen, so nothing downstream can edit a truncated read into a complete one. The value itself is
whatever the caller passed; a mutable list stays mutable, and that is the caller's business.

Stdlib only.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from collections.abc import Sized
from typing import Any, Generic, Optional, TypeVar

T = TypeVar("T")

__all__ = ["IncompleteEvidence", "Reading", "State"]


class State(enum.Enum):
    """The five evidence states. Order is the order `EVIDENCE.md` defines them in."""

    VALUE = "value"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    TRUNCATED = "truncated"
    STALE = "stale"


#: The states a gate may pass on.
COMPLETE = frozenset({State.VALUE, State.EMPTY})

_UNNAMED = "(unnamed source)"


class IncompleteEvidence(Exception):
    """A gate asked for complete evidence and the reading was unavailable, truncated or stale.

    Deliberately not a ``ValueError``: a caller catching parse errors must not swallow a refusal.
    """

    def __init__(self, reading: "Reading[Any]") -> None:
        self.reading = reading
        super().__init__(f"incomplete evidence: {reading.describe()}")


@dataclass(frozen=True)
class Reading(Generic[T]):
    """One read and the state it came back in. Build it with a constructor, not directly.

    ``coverage`` is a human-readable note of what was and was not read (for ``unavailable``, the
    reason). ``source`` names what was read (``"gh pr list"``, ``"registry Queue"``). ``as_of`` is
    the head SHA or ISO-8601 time the read describes, when the caller has one to bind it to.
    """

    state: State
    value: Optional[T]
    coverage: str
    source: str
    as_of: Optional[str]

    def __post_init__(self) -> None:
        try:
            state = State(self.state)
        except ValueError:
            raise ValueError(
                f"unknown evidence state {self.state!r}; expected one of {[s.value for s in State]}"
            ) from None
        object.__setattr__(self, "state", state)
        if state is State.UNAVAILABLE and not self.coverage.strip():
            raise ValueError("an unavailable reading must say why it could not look")
        if state is State.TRUNCATED and not self.coverage.strip():
            raise ValueError("a truncated reading must say what it covered")
        if state is State.STALE and not (self.as_of or "").strip():
            raise ValueError("a stale reading must say what it was true as of")
        # An EMPTY that carries rows lets a migration label a non-empty read "nothing found" and
        # still pass `require_complete`. A non-sized value (a count, an object) is not judged.
        if state is State.EMPTY and isinstance(self.value, Sized) and len(self.value):
            raise ValueError(
                f"an empty reading cannot carry {len(self.value)} item(s); use Reading.ok"
            )

    # -- constructors -------------------------------------------------------------------------

    @classmethod
    def ok(
        cls, value: T, *, source: str, coverage: str = "", as_of: Optional[str] = None
    ) -> "Reading[T]":
        """Looked, complete, found ``value``.

        An empty container is not "found something": pass it to :meth:`empty` instead. ``ok([])``
        is not refused, because a generic ``ok(rows)`` must not crash a read path at run time.
        """
        return cls(State.VALUE, value, coverage, source, as_of)

    @classmethod
    def empty(
        cls,
        value: Optional[T] = None,
        *,
        source: str,
        coverage: str = "",
        as_of: Optional[str] = None,
    ) -> "Reading[T]":
        """Looked, complete, found nothing. ``value`` is the empty container, if the caller wants one."""
        return cls(State.EMPTY, value, coverage, source, as_of)

    @classmethod
    def unavailable(cls, reason: str, *, source: str, as_of: Optional[str] = None) -> "Reading[T]":
        """Could not look. ``reason`` says why and becomes the coverage note."""
        return cls(State.UNAVAILABLE, None, reason, source, as_of)

    @classmethod
    def truncated(
        cls, value: T, coverage: str, *, source: str, as_of: Optional[str] = None
    ) -> "Reading[T]":
        """Looked, saw part. ``value`` is the part; ``coverage`` says how much and where it stopped."""
        return cls(State.TRUNCATED, value, coverage, source, as_of)

    @classmethod
    def stale(cls, value: T, as_of: str, *, source: str, coverage: str = "") -> "Reading[T]":
        """Looked, but ``value`` is as of ``as_of``, older than what it would gate."""
        return cls(State.STALE, value, coverage, source, as_of)

    # -- use ----------------------------------------------------------------------------------

    @property
    def is_complete(self) -> bool:
        """True for ``value`` and ``empty``, the only states a gate may pass on."""
        return self.state in COMPLETE

    def require_complete(self) -> Optional[T]:
        """The value, when the read was complete; otherwise raise :class:`IncompleteEvidence`.

        This checks the STATE only. Rule 2's other half, that the evidence is bound to what it
        gates, is the caller's: compare ``as_of`` with the head or time being gated. A reading
        with no ``as_of`` passes here.
        """
        if not self.is_complete:
            raise IncompleteEvidence(self)
        return self.value

    def describe(self) -> str:
        """One coverage line for a report: what was read, in what state, and as of when."""
        label = {
            State.VALUE: "read in full",
            State.EMPTY: "read in full, nothing found",
        }.get(self.state, self.state.value)
        line = f"{self.source or _UNNAMED}: {label}"
        if self.coverage:
            line += f" ({self.coverage})"
        if self.as_of:
            line += f", as of {self.as_of}"
        return " ".join(line.split())
