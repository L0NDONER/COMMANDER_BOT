"""Phase 2 (sandbox): does a leader/lineage/anarchist MIXTURE respawn policy
beat plain semantic respawn and random — and at what diversity cost?

Pure offline sim over the frozen embedding. No live engine, no eBay, no EC2.
Mirrors the live pool: 3 evolving literal slots, winner-takes-energy ticks
(reward the fittest live slot, decay the rest, respawn the dead). Only the
respawn policy changes.

CRUCIAL CAVEAT baked in: any semantic policy can only help if modifier *quality*
is smooth in embedding space (good modifiers cluster). We don't know that it is,
so every policy runs under BOTH a smooth fitness landscape and a random one. If
the gains only appear under 'smooth', the whole idea hinges on that unknown.

Policies:
  random   : dead slot -> uniform random token (today's pre-semantic baseline)
  semantic : dead slot -> drift from its own neighbourhood (what's live now)
  mix-*    : sample a mode ~ {p_leader, p_self, p_lineage, p_anarchist}
             leader  -> drift from the highest-energy live slot (champion)
             self    -> drift from the dead token
             lineage -> drift from a past champion (fossil)
             anarchist -> uniform random (the desync valve; p>0 ⇒ no full lock)

    python evolve.py
"""

import numpy as np

from embeddings import build_or_load_E
from vocab import VOCABS

N_SLOTS = 3
START, REWARD, DECAY = 100, 30, 8
T = 0.08
MODES = ["leader", "self", "lineage", "anarchist"]

# p = [leader, self, lineage, anarchist]
MIXES = {
    "mix-balanced": [0.25, 0.25, 0.25, 0.25],
    "mix-exploit": [0.40, 0.10, 0.40, 0.10],
    "mix-explore": [0.20, 0.20, 0.20, 0.40],
    "mix-leaderlow-anarch": [0.50, 0.20, 0.20, 0.10],
}


def _drift(anchor, E, live, rng):
    row = (E @ E[anchor]).copy()
    row[anchor] = -np.inf
    for t in live:
        row[t] = -np.inf
    finite = row[np.isfinite(row)]
    if finite.size == 0:
        return anchor
    w = np.exp((row - finite.max()) / T)
    w /= w.sum()
    return int(rng.choice(len(row), p=w))


def _uniform(n, live, rng):
    choices = [i for i in range(n) if i not in live]
    return int(rng.choice(choices)) if choices else 0


def _respawn(mode, dead, leader, lineage, E, live, rng):
    if mode == "anarchist":
        return _uniform(len(E), live, rng)
    if mode == "leader":
        return _drift(leader, E, live, rng)
    if mode == "lineage":
        return _drift(int(rng.choice(lineage)) if lineage else dead, E, live, rng)
    return _drift(dead, E, live, rng)  # self


def _make_fitness(E, regime, rng):
    n = len(E)
    if regime == "random":
        return rng.random(n)
    ideal = E[rng.integers(n)]            # a random "good" region
    f = E @ ideal
    return (f - f.min()) / (f.max() - f.min() + 1e-9)


def run(policy, p, E, fitness, ticks, rng, burn=0.5):
    n = len(E)
    slots = list(rng.choice(n, N_SLOTS, replace=False))
    energy = [START] * N_SLOTS
    lineage = []
    fit_ts, div_ts = [], []
    for tick in range(ticks):
        scores = [fitness[s] + rng.normal(0, 0.05) for s in slots]
        win = int(np.argmax(scores))
        for i in range(N_SLOTS):
            energy[i] += REWARD if i == win else -DECAY
        leader = slots[int(np.argmax(energy))]
        if not lineage or lineage[-1] != leader:
            lineage.append(leader)
            lineage[:] = lineage[-20:]
        for i in range(N_SLOTS):
            if energy[i] <= 0:
                if policy == "random":
                    mode = "anarchist"
                elif policy == "semantic":
                    mode = "self"
                else:
                    mode = MODES[rng.choice(4, p=p)]
                live = set(slots)
                live.discard(slots[i])
                slots[i] = _respawn(mode, slots[i], leader, lineage, E, live, rng)
                energy[i] = START
        if tick >= ticks * burn:
            fit_ts.append(float(np.mean([fitness[s] for s in slots])))
            sub = E[slots]
            cos = sub @ sub.T
            div_ts.append(float(cos[np.triu_indices(N_SLOTS, 1)].mean()))
    return np.mean(fit_ts), np.mean(div_ts)


def evaluate(E, regime, ticks, seeds):
    policies = [("random", None), ("semantic", None)]
    policies += [(name, p) for name, p in MIXES.items()]
    out = {}
    base_div = (E @ E.T)[~np.eye(len(E), dtype=bool)].mean()
    for name, p in policies:
        fits, divs = [], []
        for s in range(seeds):
            rng = np.random.default_rng(1000 * s + 7)
            fitness = _make_fitness(E, regime, rng)
            f, d = run(name, p, E, fitness, ticks, rng)
            # normalise fitness vs this landscape's mean (random-pick baseline)
            fits.append(f - fitness.mean())
            divs.append(d)
        out[name] = (float(np.mean(fits)), float(np.mean(divs)))
    return out, float(base_div)


def main():
    tokens, E = build_or_load_E(VOCABS["rich"], name="rich")
    ticks, seeds = 1200, 40
    print(f"|V|={len(tokens)} N_SLOTS={N_SLOTS} ticks={ticks} seeds={seeds}")
    print("fitness = mean pool fitness ABOVE the random-pick mean (higher=better)")
    print("diversity = mean pairwise cosine of live slots (LOWER = more diverse)")

    for regime in ("smooth", "random"):
        out, base_div = evaluate(E, regime, ticks, seeds)
        print(f"\n=== fitness landscape: {regime} "
              f"(random-pair cosine refmin diversity ≈ {base_div:.2f}) ===")
        print(f"{'policy':>22} {'fitness↑':>9} {'diversity↓':>11}")
        for name, (f, d) in out.items():
            print(f"{name:>22} {f:9.3f} {d:11.2f}")

        sem_f = out["semantic"][0]
        rnd_f = out["random"][0]
        best = max((n for n in out if n.startswith("mix")),
                   key=lambda n: out[n][0])
        bf, bd = out[best]
        print(f"  best mixture: {best}  fitness={bf:.3f} diversity={bd:.2f}")
        print(f"  vs semantic {sem_f:.3f} | random {rnd_f:.3f}")
        if regime == "smooth":
            if bf <= max(sem_f, rnd_f) + 0.005:
                print("  -> mixture gives NO fitness edge over the baselines.")
            elif bf > sem_f and bd >= out["semantic"][1] + 0.05:
                print("  -> mixture wins fitness but at WORSE diversity "
                      "(exploitation tax) — judge if the trade is worth it.")
            else:
                print("  -> mixture beats baselines on fitness without "
                      "collapsing diversity.")
        else:
            print("  -> under RANDOM fitness, any edge here is noise: semantics "
                  "carry no signal. Compare to the smooth block.")


if __name__ == "__main__":
    main()
