# evo_lab

A sandbox for one question: **is embedding-space genotype evolution a real
improvement over the live engine's random-respawn, or just fancier dice?**

Nothing here touches the live consensus engine. `scripts/` is not deployed and
not linted by CI. We do not write a line of production code until the gates
below pass.

## The idea under test

The live engine (`services/ebay/consensus_engine.py`, EC2-only) evolves a tiny
population of query-suffix strategies. Today a dead slot respawns by picking a
random token from a fixed vocab. The proposal: give each organism a continuous
**genotype** vector; its **phenotype** is the nearest vocab token in a frozen
embedding `E`.

    genotype:  v ∈ R^d
    phenotype: express(v) = argmax_{t∈V} ⟨v, E_t⟩
    mutation:  v' = v + N(0, σ²I)
    fossil:    md5(v.tobytes())   # lineage id of a stored vector, not a string

Because `E` is frozen and precomputed, the hot path stays `lookup → dot →
argmax → suffix`. No model calls at request time. All evolution happens
between photos, where the energy bookkeeping already lives.

## Why it might be nothing

Freezing `E` turns the genotype space into Voronoi cells, one per token:

- **Selection lives on cells (tokens); drift lives inside cells (vecs).**
  Many vecs express the same token, so fitness cannot separate them — inside a
  cell it's a neutral random walk. Nothing behavioural happens until a mutation
  crosses a cell boundary.
- Too small `σ` → almost no crossings → pure neutral drift, no behaviour change.
- Too large `σ` → every jump is a ~random cell → this *is* uniform respawn, with
  extra linear algebra bolted on.

So the entire value proposition reduces to one empirical question: **does a `σ`
regime exist where most steps stay local but some cross into a token's
*semantic* neighbours** (`vintage → retro → 90s`), rather than scattering at
random? If not, the embedding buys nothing and we keep random respawn.

## Gates (what "proven" means)

Run the offline sweep (`sweep.py`). The concept advances only if:

- **G1 — a usable regime exists.** Some `σ` gives a boundary-crossing rate
  `p = P(express(v+ε) ≠ express(v))` in a workable band (target ~5–15%). If `p`
  jumps from ~0 to ~majority between adjacent `σ` with nothing between, the cell
  structure is too coarse to steer.
- **G2 — crossings are semantic.** At that `σ`, a clear majority (target ≥60%)
  of crossings land in the token's k-nearest neighbours. This is the whole game:
  fail G2 and embedding-drift is indistinguishable from random respawn → **stop.**
- **G3 — one σ is enough (or isn't).** Per-token crossing-rate spread is small
  enough that a single global `σ` doesn't leave whole regions frozen while
  others thrash. Failing G3 isn't fatal — it just means per-cell `σ` (scaled by
  k-NN distance), i.e. more machinery to justify.

Thresholds are starting guesses, to be argued with data, not treated as law.

## Roadmap (each phase gated by the previous)

- **Phase 0 — scaffold.** This commit.
- **Phase 1 — σ / Voronoi sweep.** `sweep.py`. Decide G1/G2/G3. Pure numpy +
  a frozen `E`. No engine, no eBay, no EC2.
- **Phase 2 — offline population sim** *(only if G1+G2 pass)*. Replay a fitness
  signal, run the energy/mutation/lineage loop, check evolved suffixes actually
  beat the fixed vocab on a held-out measure. Still offline.
- **Phase 3 — shadow mode** *(only if Phase 2 shows lift)*. Compute evolved
  variants alongside the live ones, log, act on nothing. Only after that do we
  discuss touching the real engine.

## Layout

    vocab.py        candidate suffix tokens (the action space)
    embeddings.py   build/cache the frozen E; express() = nearest token
    organism.py     Organism genotype, mutation, md5 fossil
    sweep.py        Phase 1 — the σ/Voronoi sweep and gate checks
    data/           cached embedding matrices (gitignored content)
    results/        sweep outputs

## Findings so far

Backend `all-MiniLM-L6-v2`, 30-token vocab, k=3 (chance floor ≈ 0.10).

- **Phase 1 global σ — G1 PASS, G2 REAL BUT PARTIAL.** Usable crossing band at
  σ≈0.22 (p≈0.07); in-band locality ≈ **0.39–0.41 ≈ 4× chance**. Clearly beats
  random respawn, but below the 0.60 "strongly steerable" bar. `p` and locality
  trade off monotonically — you can't have many crossings *and* high locality.
- **Per-cell σ (the G3 lever) — REFUTED.** At matched p, locality *drops* to
  0.26 (worse than global's 0.39) with no uniformity gain. Scaling σ by cell
  size gives a bigger budget to large-cell tokens, which are the semantically
  *isolated* ones — so crossings shift toward tokens with no near neighbours,
  lowering locality. The ~4× global ceiling looks **intrinsic** to
  frozen-Voronoi over this embedding, not a tuning miss.
- **Richer vocab (lever #2) — REFUTED.** A 3× denser, cluster-rich vocab (85
  tokens) left drift quality unchanged on the scale-invariant metric (mean
  cosine of where crossings land = **0.42, identical to base**). The apparent
  shifts — top-3 locality 0.39→0.31, lift 3.8×→8.6× — were pure artifacts of
  `|V|` changing the k=3 slice and the chance floor. Density changed nothing real.
- **Is 0.42 even good?** Baseline mean pairwise cosine (a random crossing) is
  ~0.28. So drift lands ~+0.14 cosine above random — a real but **modest**
  semantic pull. Confirms the effect exists; bounds how much.
- **Multi-step prev-anchored walk (`walk.py`) — REFUTED.** A K-step accepted
  walk (each hop ≥τ cosine to the previous token) does reach beyond knn(top-15)
  (~40% of endpoints) while staying semantic — but its endpoint distribution
  (meanCos 0.52, fat tail) is reproduced by a one-line **cosine-softmax
  respawn** (`token ∝ exp(cos/T)`) off the precomputed cosine row. The only
  residual difference is manifold-connectivity (τ-chains can't jump disconnected
  clusters), which has no product value and costs a ~42% stall rate.

## Verdict

**The meta-finding settles it:** every operator that "helps" (single-jump
mutation, per-cell σ, richer vocab, multi-step walk) is reproduced by a one-line
closed-form lookup/sampler off the precomputed cosine row — knn respawn or
cosine-softmax respawn. The genotype vectors, ε-perturbation, snap-to-token,
accept/reject, lineage/fossil machinery never add anything a cosine row can't do
directly.

**Recommendation: not worth shipping.** For a shelf-side triage bot in churn
mode, even the closed-form "semantic respawn" is only a modest, unproven gain
over plain random respawn — and the full ALife apparatus buys nothing over that
one-liner. The live engine's plain random respawn is the right call. Settled
negative result. If "semantic respawn" is ever wanted, it's a cosine-softmax
over the neighbour matrix — not this machinery. Revisit only if the product goal
changes from triage velocity to query-quality optimization.

## Run

    pip install -r requirements.txt
    python sweep.py                 # uses a local sentence-transformer for E
    python sweep.py --backend random   # dynamics-only smoke test, zero ML deps
