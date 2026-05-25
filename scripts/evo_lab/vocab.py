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


# Lever #2: a denser, cluster-rich vocab. Same domain (modifiers appended to a
# clothing query), but each concept gets several near-synonyms so most tokens
# have genuine close neighbours — the condition the per-cell test showed was
# missing. Tests whether neighbourhood density lifts in-band locality.
RICH_VOCAB = [
    # used / pre-owned cluster
    "used", "preloved", "preowned", "second hand", "worn", "gently worn",
    # new / unworn cluster
    "new", "brand new", "bnwt", "bnip", "unworn", "sealed", "mint", "pristine",
    # vintage / old cluster
    "vintage", "retro", "antique", "classic", "old school", "throwback",
    # decade cluster
    "70s", "80s", "90s", "00s", "y2k", "noughties",
    # fit / cut cluster
    "mens", "womens", "unisex", "oversized", "baggy", "slim", "fitted",
    "regular", "cropped", "longline",
    # size cluster
    "small", "medium", "large", "extra large", "plus size", "petite",
    # rarity cluster
    "rare", "scarce", "limited edition", "exclusive", "hard to find", "one off",
    # desirability / brand cluster
    "designer", "luxury", "premium", "high end", "branded", "authentic",
    "genuine", "original",
    # lot / quantity cluster
    "bundle", "job lot", "wholesale", "multipack", "set of", "single",
    "individual",
    # material cluster
    "leather", "suede", "denim", "wool", "cotton", "silk", "cashmere", "linen",
    # pattern cluster
    "striped", "plain", "floral", "checked", "plaid", "polka dot", "paisley",
    # style cluster
    "casual", "formal", "smart", "sporty", "workwear", "streetwear",
    "loungewear",
]


VOCABS = {"base": SUFFIX_VOCAB, "rich": RICH_VOCAB}
