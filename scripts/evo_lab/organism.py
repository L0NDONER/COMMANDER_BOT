"""The organism: a continuous genotype that expresses into a discrete token.

This is the unit that would, in Phase 2+, carry energy and lineage. Phase 1
only needs the genotype + mutation to measure Voronoi crossing behaviour.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np


@dataclass
class Organism:
    vec: np.ndarray              # genotype in R^d
    energy: int = 100
    parent: str | None = None    # md5 of the genotype it descended from

    @property
    def fossil(self) -> str:
        """Lineage id: md5 of the genotype's raw bytes. The 'genetic fossil' a
        dead organism leaves — a hash of a *vector*, which (unlike a hash of a
        bare string) actually has something to mutate from."""
        return hashlib.md5(self.vec.astype(np.float32).tobytes()).hexdigest()

    def mutate(self, sigma: float, rng: np.random.Generator) -> "Organism":
        child = self.vec + rng.normal(0.0, sigma, size=self.vec.shape)
        return Organism(vec=child.astype(np.float32), parent=self.fossil)
