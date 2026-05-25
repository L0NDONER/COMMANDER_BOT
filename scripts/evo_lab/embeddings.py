"""Build and cache the frozen embedding matrix E, and the express() lookup.

E is computed once and cached to data/. At "runtime" express() is a single
argmax over dot products — the property that keeps the real hot path clean.

Two backends:
  sentence-transformers : real semantics (default). Needs the pip extra.
  random                : deterministic random unit vectors. Zero ML deps, for
                          smoke-testing the sweep *mechanics* only. Random E has
                          no semantic neighbours, so it is expected to FAIL G2 —
                          that is the point of having it (a negative control).
"""

from __future__ import annotations

import os

import numpy as np

_DATA = os.path.join(os.path.dirname(__file__), "data")


def _normalize(M: np.ndarray) -> np.ndarray:
    return M / np.linalg.norm(M, axis=1, keepdims=True)


def _embed_sentence_transformers(vocab: list[str]) -> np.ndarray:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:  # pragma: no cover - environment hint
        raise SystemExit(
            "sentence-transformers not installed. Either:\n"
            "  pip install -r requirements.txt\n"
            "or run the negative-control backend:\n"
            "  python sweep.py --backend random"
        ) from e
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return np.asarray(model.encode(vocab, normalize_embeddings=False), dtype=np.float32)


def _embed_random(vocab: list[str], d: int = 384, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal((len(vocab), d)).astype(np.float32)


def build_or_load_E(vocab: list[str], backend: str = "sentence-transformers",
                    name: str = "base"):
    """Return (tokens, E) with E L2-normalized (so ⟨v, E_t⟩ is cosine).

    Cached per (backend, name) so it is genuinely frozen between runs and
    different vocabs don't clobber each other's matrix.
    """
    os.makedirs(_DATA, exist_ok=True)
    cache = os.path.join(_DATA, f"E_{backend}_{name}.npz")
    if os.path.exists(cache):
        z = np.load(cache, allow_pickle=True)
        if list(z["tokens"]) == vocab:
            return list(z["tokens"]), z["E"]

    if backend == "random":
        raw = _embed_random(vocab)
    elif backend == "sentence-transformers":
        raw = _embed_sentence_transformers(vocab)
    else:
        raise ValueError(f"unknown backend: {backend}")

    E = _normalize(raw)
    np.savez(cache, tokens=np.array(vocab, dtype=object), E=E)
    return vocab, E


def express(v: np.ndarray, E: np.ndarray) -> int:
    """Phenotype: index of the nearest token. argmax of dot products."""
    return int(np.argmax(E @ v))


def neighbours(E: np.ndarray) -> np.ndarray:
    """For each token, indices of all other tokens ranked by cosine, nearest
    first. Shape (|V|, |V|-1). Used to score semantic locality of crossings."""
    sim = E @ E.T
    np.fill_diagonal(sim, -np.inf)
    return np.argsort(-sim, axis=1)
