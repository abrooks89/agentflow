"""Optional LLM label-naming pass (epic 703 scope 3).

Renames community labels ONLY — it may never change membership. It runs as a
second, separately-committed pass over already-labelled rows: the
deterministic labels written by ``communities.run_communities`` are durable
BEFORE this starts, so a naming failure exits non-zero with every community
still labelled. No silent degradation, no unlabelled community, ever.

Portability contract (same as the embedder): the endpoint resolves
explicit argument > ``AUTODOC_OLLAMA_URL`` > ``http://localhost:11434``.
The model name has NO baked-in default — pass ``--llm-model`` or set
``AUTODOC_NAMING_MODEL``; a missing model is a loud, actionable error.

Storage-agnostic core: adapter interface only, injectable transport.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .adapter import GraphAdapter
from .communities import dedup_labels, sanitize_label
from .embedder import resolve_ollama_url

NAMING_MODEL_ENV = "AUTODOC_NAMING_MODEL"
MAX_LABEL_CHARS = 80


class NamingError(RuntimeError):
    """Loud naming-pass failure — labels are already durable when it fires."""


def resolve_naming_model(explicit: str | None) -> str:
    """Explicit argument > AUTODOC_NAMING_MODEL env var > loud error."""
    model = explicit or os.environ.get(NAMING_MODEL_ENV)
    if not model:
        raise NamingError(
            "no naming model configured: pass --llm-model or set "
            f"{NAMING_MODEL_ENV} (the naming pass has no baked-in model)"
        )
    return model


def _urllib_transport(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


class OllamaNamer:
    """One short /api/generate call per community; label text out."""

    def __init__(self, url: str | None = None, model: str | None = None,
                 transport=None) -> None:
        self.url = resolve_ollama_url(url)
        self.model = resolve_naming_model(model)
        self._transport = transport if transport is not None else _urllib_transport

    def name_community(self, label: str, descriptor: dict[str, Any] | None) -> str:
        d = descriptor or {}
        prompt = (
            "Name this code community in 2-4 plain words. Current "
            f"deterministic label: {label!r}. Members: {d.get('member_count', '?')}. "
            f"Key symbols: {', '.join(d.get('top_symbols', [])[:3])}. "
            f"Main directories: {', '.join(d.get('top_prefixes', [])[:3])}. "
            "Reply with the name only — no punctuation, no explanation."
        )
        try:
            resp = self._transport(
                f"{self.url}/api/generate",
                {"model": self.model, "prompt": prompt, "stream": False},
            )
        except urllib.error.HTTPError as e:
            raise NamingError(
                f"Ollama at {self.url} answered HTTP {e.code} for the naming "
                f"pass: {e.read().decode('utf-8', 'replace')[:300]}"
            ) from e
        except (urllib.error.URLError, OSError) as e:
            raise NamingError(
                f"naming endpoint unreachable at {self.url} — is Ollama "
                f"running? (set AUTODOC_OLLAMA_URL or pass --ollama-url): {e}"
            ) from e
        text = str(resp.get("response", "")).strip().splitlines()
        # Stored labels are charset-constrained (review finding 7): the model
        # output flows into downstream agent prompts, so anything outside
        # [A-Za-z0-9 _.-] is stripped; an empty result is a loud failure.
        name = sanitize_label(text[0]) if text else ""
        if not name:
            raise NamingError(
                f"naming model {self.model!r} returned an empty (or entirely "
                f"unsafe-charset) label for community {label!r} — refusing to "
                "blank a durable label"
            )
        return name[:MAX_LABEL_CHARS]


def _current_rows_by_level(adapter: GraphAdapter) -> list[dict[str, Any]]:
    """The CURRENT hierarchy rows (entity-pointed level 0 + parent chains).
    Rows dissolved in earlier runs are never renamed."""
    current0 = {
        e["community_id"]
        for e in adapter.find_entities({"tier": "symbol"})
        if e.get("community_id") is not None
    }
    rows: list[dict[str, Any]] = []
    level = 0
    wanted = current0
    while wanted:
        found = [r for r in adapter.get_communities(level) if r["id"] in wanted]
        if not found:
            break
        rows.extend(found)
        wanted = {r["parent_id"] for r in found if r["parent_id"] is not None}
        level += 1
    return rows


def rename_labels(adapter: GraphAdapter, namer: OllamaNamer) -> dict[str, int]:
    """Rewrite labels of the current hierarchy via the LLM. Membership,
    descriptors, parents, and 704's report: untouched by construction — the
    update rows carry ONLY id/level/label, and the adapter preserves every
    absent column. Duplicate model outputs are ordinal-suffix deduped
    batch-scoped per level (review finding 3) so fan-out display names never
    collapse two communities into one."""
    rows = _current_rows_by_level(adapter)
    if not rows:
        raise NamingError(
            "no current communities to rename — run `graph communities` first"
        )
    by_level: dict[int, list[dict]] = {}
    for row in rows:
        by_level.setdefault(row["level"], []).append(row)
    renamed = 0
    for level in sorted(by_level):
        level_rows = sorted(by_level[level], key=lambda r: r["id"])
        new_labels = [
            namer.name_community(r["label"] or "", r.get("descriptor"))
            for r in level_rows
        ]
        for row, label in zip(level_rows, dedup_labels(new_labels), strict=True):
            adapter.upsert_communities([{
                "id": row["id"], "level": row["level"], "label": label,
            }])
            renamed += 1
    return {"renamed": renamed}
