"""Cosine-softmax semantic respawn for the consensus query-strategy pool.

Runtime dependency: numpy ONLY. Loads the frozen embedding generated offline by
scripts/evo_lab/build_E.py (suffix_embeddings.npz, same directory). No model
call, no sentence-transformers/torch in the container — the hot path is a dot
product over a precomputed matrix.

STAGED, NOT WIRED. The live engine still uses plain random respawn. To activate:

  1. add `numpy` to requirements.txt (forces a --build deploy), and
  2. in consensus_engine._Population._respawn_dead(), for a *literal* dead slot
     (NOT the structural $COND / $KW* tokens), replace the random vocab pick:

         from services.ebay.semantic_respawn import semantic_respawn
         pick = semantic_respawn(dead.suffix, live={s.suffix for s in self._pool})

The engine's literal respawn pool then becomes this artifact's vocab. Lab
verdict (scripts/evo_lab/README.md): the gain over random respawn is modest and
unproven — wire only as a deliberate experiment, not because the data demands it.
"""

import os

import numpy as np

_PATH = os.path.join(os.path.dirname(__file__), "suffix_embeddings.npz")
_E = None
_TOKENS = None
_INDEX = None


def _load():
    global _E, _TOKENS, _INDEX
    if _E is None:
        z = np.load(_PATH)
        _E = z["E"]
        _TOKENS = [str(t) for t in z["tokens"]]
        _INDEX = {t: i for i, t in enumerate(_TOKENS)}
    return _E, _TOKENS, _INDEX


def semantic_respawn(dead, live=None, T=0.08):
    """Return a vocab token semantically near `dead`, sampled ∝ exp(cos/T).

    Never returns `dead` or anything in `live` (suffixes already in the pool).
    Falls back to a uniform pick if `dead` isn't in the embedded vocab.
    """
    E, tokens, index = _load()
    if dead not in index:
        choices = [t for t in tokens
                   if t != dead and not (live and t in live)]
        return np.random.choice(choices) if choices else dead
    row = (E @ E[index[dead]]).copy()
    row[index[dead]] = -np.inf
    if live:
        for t in live:
            if t in index:
                row[index[t]] = -np.inf
    finite = row[np.isfinite(row)]
    w = np.exp((row - finite.max()) / T)
    w /= w.sum()
    return tokens[int(np.random.choice(len(tokens), p=w))]
