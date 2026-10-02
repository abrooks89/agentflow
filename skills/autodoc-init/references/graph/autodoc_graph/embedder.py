"""Ollama embedding pass — nomic-embed-text, 768-dim, batched, incremental.

Storage-agnostic core: this module sees storage ONLY through the abstract
GraphAdapter interface (enforced by test — the fork thesis).

URL resolution (portability contract): explicit argument >
``AUTODOC_OLLAMA_URL`` env var > ``http://localhost:11434`` (Ollama's
universal default). Nothing here assumes any particular machine.

Failure modes are LOUD by design (epic 705 depends on this):
* unreachable Ollama  -> EmbedderError naming the URL
* model not installed -> EmbedderError telling the user to run
  ``ollama pull nomic-embed-text``
* response model/dimension/count drift -> EmbedderError

All HTTP goes through an injectable ``transport`` callable so tests never
touch the network; the live run happens later over an operator-provided URL.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .adapter import GraphAdapter
from .vocab import EMBEDDING_DIM, EMBEDDING_MODEL

DEFAULT_OLLAMA_URL = "http://localhost:11434"
ENV_VAR = "AUTODOC_OLLAMA_URL"
BATCH_SIZE = 50  # matches the embedding batch size of the retrieval store this feeds

Transport = Callable[..., dict[str, Any]]


class EmbedderError(RuntimeError):
    """Loud embedding failure — never silently skipped."""


def resolve_ollama_url(explicit: str | None) -> str:
    """Explicit argument > AUTODOC_OLLAMA_URL env var > localhost default."""
    url = explicit or os.environ.get(ENV_VAR) or DEFAULT_OLLAMA_URL
    return url.rstrip("/")


def _urllib_transport(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """POST ``payload`` as JSON (or GET when payload is None); parsed JSON out."""
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


class OllamaEmbedder:
    """Batched, incremental embedding against an Ollama ``/api/embed`` endpoint."""

    def __init__(
        self,
        url: str | None = None,
        model: str = EMBEDDING_MODEL,
        batch_size: int = BATCH_SIZE,
        transport: Transport | None = None,
    ) -> None:
        self.url = resolve_ollama_url(url)
        self.model = model
        self.batch_size = batch_size
        self._transport = transport if transport is not None else _urllib_transport
        self._model_checked = False

    # ---------------------------------------------------------------- HTTP
    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.url}{path}"
        try:
            return self._transport(url, payload)
        except urllib.error.HTTPError as e:
            raise EmbedderError(
                f"Ollama at {self.url} answered HTTP {e.code} for {path}: "
                f"{e.read().decode('utf-8', 'replace')[:500]}"
            ) from e
        except (urllib.error.URLError, OSError) as e:
            raise EmbedderError(
                f"Ollama unreachable at {self.url} — is it running? "
                f"(set {ENV_VAR} or pass --ollama-url to point elsewhere): {e}"
            ) from e

    def _check_model_available(self) -> None:
        """Preflight /api/tags: a missing model must fail actionably, not 404."""
        if self._model_checked:
            return
        tags = self._request("/api/tags")
        names = {str(m.get("name", "")).split(":")[0] for m in tags.get("models", [])}
        if self.model not in names:
            raise EmbedderError(
                f"embedding model {self.model!r} is not installed on the Ollama "
                f"server at {self.url} (available: {sorted(names)}). "
                f"Install it with: ollama pull {self.model}"
            )
        self._model_checked = True

    # ---------------------------------------------------------------- embedding
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` in batches; exactly one 768-dim vector per text."""
        self._check_model_available()
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            resp = self._request("/api/embed", {"model": self.model, "input": batch})
            resp_model = str(resp.get("model", self.model)).split(":")[0]
            if resp_model != self.model:
                raise EmbedderError(
                    f"embedding model mismatch: requested {self.model!r} but the "
                    f"server answered with {resp.get('model')!r} — refusing to mix "
                    "embedding spaces"
                )
            vecs = resp.get("embeddings")
            if not isinstance(vecs, list) or len(vecs) != len(batch):
                got = len(vecs) if isinstance(vecs, list) else type(vecs).__name__
                raise EmbedderError(
                    f"embedding count mismatch: sent {len(batch)} texts, "
                    f"got {got} vectors back"
                )
            for vec in vecs:
                if len(vec) != EMBEDDING_DIM:
                    raise EmbedderError(
                        f"embedding dimension drift: expected {EMBEDDING_DIM}, "
                        f"got {len(vec)} — the graph schema is fixed at "
                        f"{EMBEDDING_DIM} dims"
                    )
            out.extend([list(map(float, v)) for v in vecs])
        return out

    @staticmethod
    def text_for_entity(entity: dict[str, Any]) -> str:
        """Deterministic embedding text for an entity row."""
        parts = [
            f"{entity.get('kind', '')} {entity.get('name', '')}".strip(),
            entity.get("qualified_name") or "",
            entity.get("summary") or "",
            entity.get("body") or "",
        ]
        return "\n".join(p for p in parts if p)

    def embed_pending(self, adapter: GraphAdapter) -> dict[str, int]:
        """Incremental pass: embed only entities whose ``last_seen_sha`` advanced
        past their embedding's ``embedded_at_sha`` (or that have none)."""
        pending = adapter.find_entities({"needs_embedding": True})
        if not pending:
            return {"embedded": 0}
        vecs = self.embed_texts([self.text_for_entity(e) for e in pending])
        adapter.upsert_embeddings([
            {
                "entity_id": entity["id"],
                "vec": vec,
                "model": self.model,
                "embedded_at_sha": entity.get("last_seen_sha"),
                # content-version marker: embedding is fresh while the entity's
                # updated_at (bumped only on content change) still matches
                "entity_version": entity.get("updated_at"),
            }
            for entity, vec in zip(pending, vecs, strict=True)
        ])
        return {"embedded": len(pending)}
