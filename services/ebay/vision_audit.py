"""Independent-read audit for the vision step — the one place consensus can't
help, because every juror's query descends from the same Gemini read.

`run_shadow` fires a second, independent vision read (Groq, injected) in the
background and logs one VISION_AUDIT line comparing it to the Gemini read. It
never touches the verdict — pure measurement. Scheduled fire-and-forget from
scout_async on the vision CACHE-MISS path only, so repeats don't re-charge Groq
or pollute the sample.

Why a different model and not Gemini twice: a model that confidently mis-IDs an
item will mis-ID it again — same weights, correlated error. Only an independent
read can dissent on the confident-wrong case. Disagreement needs no ground
truth to fire; that disagreement rate is the whole measurement.

Analyse a log capture:
    docker compose logs commander-leader | python3 -m services.ebay.vision_audit
"""
import asyncio
import json
import logging
import re
import sys
from typing import Callable, List

LOGGER = logging.getLogger(__name__)

GroqReader = Callable[[str], str]   # image_path -> raw model string

# Sizes are noise for "is this the same product"; brand + type carry the signal.
_SIZE_TOKENS = {"xs", "s", "m", "l", "xl", "xxl", "xxxl", "os"}


def _tokens(s: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (s or "").lower())
            if t not in _SIZE_TOKENS]


def same_product(a: str, b: str) -> bool:
    """Agreement test on two free-form reads: agree iff the brand (first token)
    matches AND they share at least one non-brand content token.

    Whole-string token overlap is the wrong metric here — the two models append
    different tails (Gemini dumps multi-region size codes, Groq adds style
    keywords), so a real match like "Rab shirt" vs "Rab shirt outdoor casual"
    scores low overlap despite agreeing. Brand + shared item-type isolates the
    decision-relevant head and ignores the noisy tail. Brand mismatch is always
    a split — that's the dangerous confident-wrong case we're hunting."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if ta[0] != tb[0]:                              # brand disagreement
        return False
    shared_non_brand = (set(ta) & set(tb)) - {ta[0]}
    return len(shared_non_brand) >= 1


async def run_shadow(image_path: str, gemini_query: str, groq_reader: GroqReader) -> None:
    """Background: independent Groq read, compared to the Gemini query, logged.
    Swallows everything — a diagnostic must never disturb the request."""
    try:
        groq_raw = await asyncio.to_thread(groq_reader, image_path)
        LOGGER.info("VISION_AUDIT %s", json.dumps({
            "gemini": gemini_query,
            "groq": groq_raw,
            "agree": same_product(gemini_query, groq_raw),
        }))
    except asyncio.CancelledError:
        return                                  # loop shutting down; nothing to log
    except Exception as exc:
        LOGGER.warning("VISION_AUDIT shadow read failed: %r", exc)


# ── analyser ──────────────────────────────────────────────────────────────────
def _parse(stream) -> List[dict]:
    recs = []
    for line in stream:
        k = line.find("VISION_AUDIT ")
        if k == -1:
            continue
        try:
            recs.append(json.loads(line[k + len("VISION_AUDIT "):]))
        except ValueError:
            pass
    return recs


def analyse(recs: List[dict]) -> None:
    n = len(recs)
    if not n:
        print("no VISION_AUDIT records found.")
        return
    # Re-judge from the logged raw reads with the CURRENT comparator, so tuning
    # same_product applies retroactively to logs already collected (the baked-in
    # "agree" field reflects whatever comparator was live when the line was written).
    splits = [r for r in recs
              if not same_product(r.get("gemini", ""), r.get("groq", ""))]
    rate = len(splits) / n
    print(f"reads={n}  agree={n - len(splits)} ({1 - rate:.0%})  "
          f"split={len(splits)} ({rate:.0%})")

    if splits:
        print("\nsplits — your eye calls who's right:")
        for r in splits:
            print(f"  gemini={r.get('gemini')!r}\n  groq  ={r.get('groq')!r}\n")

    if n < 20:
        print(f"only {n} reads — let more photos flow (want >= ~20 to decide).")
        return
    if rate < 0.10:
        print("VERDICT: models agree >=90%. Gemini reads are stable; an independent "
              "live cross-check buys little for the latency. Don't wire it — keep "
              "eyeballing.")
    else:
        print(f"VERDICT: {rate:.0%} split. Review the splits above: wire a live tag "
              "ONLY if Gemini is the wrong one often enough to matter. If Groq is "
              "usually the wrong one, the audit is just noise.")


if __name__ == "__main__":
    analyse(_parse(sys.stdin))
