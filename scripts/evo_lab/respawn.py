"""Cosine-softmax respawn — the distilled result of this whole lab.

Every richer operator we tried (single-jump embedding mutation, per-cell σ,
denser vocab, multi-step prev-anchored walk) turned out to be reproduced by THIS
one closed-form sampler over the precomputed cosine row. The genotype vectors,
ε-perturbation, snap-to-token, accept/reject, and lineage/fossil machinery never
bought anything a cosine lookup couldn't. See README.md for the full writeup.

So if "semantic respawn" is ever wanted in the live engine, this is the entire
thing — no ALife apparatus. `cos_matrix = E @ E.T` for an L2-normalized,
precomputed embedding `E` (frozen; no model call at runtime).

NOT currently shipped. The lab's verdict is that even this is only a modest,
unproven gain over plain random respawn for a shelf-side triage bot — kept here
as the reference design, to be reached for only if the product goal shifts from
triage velocity to query-quality optimization.
"""

import numpy as np


def respawn(dead, cos_matrix, live_mask=None, T=0.08):
    """Pick a replacement token for a dead one, weighted toward its semantic
    neighbours. Lower T ⇒ stay closer to the dead token; higher T ⇒ broader.

    dead       : index of the token being replaced
    cos_matrix : (V, V) cosine matrix, E @ E.T for L2-normalized E
    live_mask  : optional bool array, True for tokens already in the pool
    """
    cos_row = cos_matrix[dead].copy()
    cos_row[dead] = -np.inf              # never respawn to yourself
    if live_mask is not None:
        cos_row[live_mask] = -np.inf     # nor to a suffix already in the pool
    finite = cos_row[np.isfinite(cos_row)]
    w = np.exp((cos_row - np.nanmax(finite)) / T)
    w /= w.sum()
    return int(np.random.choice(len(cos_row), p=w))
