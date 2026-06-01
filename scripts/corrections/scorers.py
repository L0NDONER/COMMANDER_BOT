"""Scorers layer — Δ-producing statistics for the null harness.

Each scorer is `seq -> float` (signed Δ; future-minus-past convention).
Reversal-symmetric by construction (forward and reverse pass the same
data through the same window definition).

Three scorers live here:
  mode       — lag_game mode-match Δ at lag k (binary per-position)
  agreement  — agreement-fraction Δ at lag k (continuous per-position)
  surprise   — local-distribution self-prediction Δ (full distribution
               per-position; Laplace add-1 smoothing)

Sign-flip between them on the same sequence is informative — see
[[courier-bubble-signature]] (2026-06-01 corroboration block).
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from courier_lens import lag_game  # noqa: E402


def _mode_scorer(seq, k):
    rows = lag_game(seq, k)
    r = next(row for row in rows if row["k"] == k)
    return r["future_hit"] - r["past_hit"]


def _agreement_scorer(seq, k):
    n = len(seq)
    past, future = [], []
    for t in range(n):
        if t - k >= 0:
            past.append(sum(1 for x in seq[t - k:t] if x == seq[t]) / k)
        if t + k < n:
            future.append(sum(1 for x in seq[t + 1:t + 1 + k]
                              if x == seq[t]) / k)
    p = sum(past) / len(past) if past else 0.0
    f = sum(future) / len(future) if future else 0.0
    return f - p


def _surprise_scorer(seq, k):
    """Local-distribution self-prediction scorer.

    For each position t, estimate the symbol distribution from the
    past-k window and the future-k window separately (Laplace add-1
    smoothing against the full sequence alphabet so unseen symbols
    don't blow up to -log(0) = inf). Surprise of seq[t] under each
    window is -log(P). Statistic = mean(future_surprise) - mean(past_surprise).

    Reversal-symmetric by construction: reversing the sequence swaps
    past and future windows exactly, so Δ_rev = -Δ_fwd and S = 0.
    Substrate-internal: uses only the sequence and its own alphabet.
    Strictly more sensitive than mode/agreement because it consumes
    the full per-window distribution, not just the mode or a binary
    match."""
    n = len(seq)
    if n < 2:
        return 0.0
    alphabet = sorted(set(seq))
    A = len(alphabet)
    past_s, future_s = [], []
    for t in range(n):
        if t - k >= 0:
            window = seq[t - k:t]
            count = sum(1 for x in window if x == seq[t])
            p = (count + 1) / (k + A)
            past_s.append(-math.log(p))
        if t + k < n:
            window = seq[t + 1:t + 1 + k]
            count = sum(1 for x in window if x == seq[t])
            p = (count + 1) / (k + A)
            future_s.append(-math.log(p))
    fut = sum(future_s) / len(future_s) if future_s else 0.0
    pas = sum(past_s) / len(past_s) if past_s else 0.0
    return fut - pas


def make_scorer(name: str = "mode", k: int = 2):
    """Canonical scorer-layer entrypoint: name → callable `seq -> float`.
    Supported names: 'mode', 'agreement', 'surprise'. All three are
    reversal-symmetric and substrate-internal. Pick one before looking
    at the data; running all three and picking the winner is statistical
    fishing — unless you're using the sign agreement / disagreement
    across all three as the falsification, in which case it's a feature."""
    if name == "mode":
        return lambda seq: _mode_scorer(seq, k)
    if name == "agreement":
        return lambda seq: _agreement_scorer(seq, k)
    if name == "surprise":
        return lambda seq: _surprise_scorer(seq, k)
    raise ValueError(
        f"unknown scorer {name!r}; valid: mode, agreement, surprise")
