"""Offline one-time generator for the shipped suffix-embedding artifact.

Run locally — needs sentence-transformers/torch. Produces a tiny static file
(services/ebay/suffix_embeddings.npz: L2-normalized E + token list) that the
runtime loads with numpy ALONE. No ML dependency ever enters the container.

Re-run only when the respawn vocab changes; the saved token order is the
contract that semantic_respawn relies on.

    python build_E.py
"""

import os

import numpy as np

from embeddings import build_or_load_E
from vocab import RICH_VOCAB

SHIP = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..",
    "services", "ebay", "suffix_embeddings.npz"))


def main():
    tokens, E = build_or_load_E(RICH_VOCAB, name="rich")
    np.savez(SHIP, E=E.astype(np.float32), tokens=np.array(tokens))
    print(f"wrote {SHIP}  E={E.shape}  |V|={len(tokens)}")


if __name__ == "__main__":
    main()
