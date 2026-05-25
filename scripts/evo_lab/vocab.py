"""Candidate suffix tokens — the discrete action space the genotype expresses
into. Mirrors the spirit of the live engine's vocab: item-agnostic modifiers
that get appended to a vision-derived base query.

Deliberately generic. The whole point of Phase 1 is to learn whether the
embedding geometry over *these* tokens supports steerable drift.
"""

SUFFIX_VOCAB = [
    # condition / state
    "used", "new", "boxed", "sealed", "bnwt", "worn", "vintage", "retro",
    # era
    "90s", "80s", "70s", "y2k",
    # cut / fit
    "mens", "womens", "oversized", "slim", "cropped",
    # quality / desirability
    "rare", "genuine", "deadstock", "limited", "designer",
    # lot shape
    "bundle", "job lot", "single",
    # material / style
    "leather", "denim", "wool", "striped", "plain",
]
