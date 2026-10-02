"""A deterministic, NETWORK-FREE stand-in embedder for smokes and evals.

NOT semantic: vectors are L2-normalized hashed bags of identifier tokens, so
cosine similarity measures lexical overlap only. It exists so a store can be
built and queried end-to-end when no Ollama endpoint is reachable — every
run that uses it must SAY SO (run_eval prints a banner; the smoke reports
label the vectors). ``model`` mirrors the store's fixed constant because the
schema pins the column; the honesty lives in the reporting, not the column.
"""

from __future__ import annotations

import hashlib
import math
import re

from autodoc_graph.vocab import EMBEDDING_DIM, EMBEDDING_MODEL

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_SALTS = (b"a", b"b", b"c")  # 3 positions per token to soften collisions


def _subtokens(token: str) -> list[str]:
    """`write_records` -> [write_records, write, records]; CamelCase splits
    too (before lowercasing), so questions and code identifiers overlap
    lexically."""
    low = token.lower()
    parts = [low]
    parts.extend(p for p in low.split("_") if p and p != low)
    camel = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", token)
    parts.extend(p.lower() for p in camel if p.lower() != low)
    return parts


class DeterministicEmbedder:
    """Duck-type of ``OllamaEmbedder``: ``.model`` + ``.embed_texts``."""

    model = EMBEDDING_MODEL

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    @staticmethod
    def _vec(text: str) -> list[float]:
        vec = [0.0] * EMBEDDING_DIM
        for raw in _TOKEN_RE.findall(text):
            for tok in _subtokens(raw):
                for salt in _SALTS:
                    digest = hashlib.md5(salt + tok.encode("utf-8")).digest()
                    idx = int.from_bytes(digest[:4], "little") % EMBEDDING_DIM
                    vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            vec[0] = 1.0  # degenerate all-symbols text still embeds validly
            return vec
        return [x / norm for x in vec]
