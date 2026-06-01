"""Scorers layer — Δ-producing statistics for the null harness.

Each scorer is `seq -> float` (signed Δ; future-minus-past convention).
Reversal-symmetric by construction (forward and reverse pass the same
data through the same window definition).

Two scorers live here:
  mode       — the lag_game mode-match Δ at lag k (binary per-position)
  agreement  — the agreement-fraction Δ at lag k (continuous per-position)

Sign-flip between them on the same sequence is informative — see
[[courier-bubble-signature]] (2026-06-01 corroboration block).
"""
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


def make_scorer(name: str = "mode", k: int = 2):
    """Canonical scorer-layer entrypoint: name → callable `seq -> float`.
    Supported names: 'mode', 'agreement'. Both reversal-symmetric, both
    substrate-internal. Pick one before looking at the data; running
    both and picking the winner is statistical fishing."""
    if name == "mode":
        return lambda seq: _mode_scorer(seq, k)
    if name == "agreement":
        return lambda seq: _agreement_scorer(seq, k)
    raise ValueError(f"unknown scorer {name!r}; valid: mode, agreement")
