"""trim_lab — does trimming the consensus fan-out cost availability?

The flat-landscape probe (scout_diag) proved fan-out width doesn't move PRICE.
This asks the other axis: does it move HIT-RATE? For each source's fan-out it
measures how often the base anchor (i=0) alone carries a vote, and how often a
non-base suffix (i>=1) is the *marginal* vote — i.e. the source would have had
zero votes without it. Suffixes that are never marginal are dead weight on a
flat landscape and safe to trim.

    python3 scripts/trim_lab.py < /tmp/diag.log
"""
import json
import sys
from collections import defaultdict


def load(stream):
    recs = []
    for line in stream:
        k = line.find("SCOUT_DIAG ")
        if k == -1:
            continue
        try:
            recs.append(json.loads(line[k + len("SCOUT_DIAG "):]))
        except ValueError:
            pass
    return recs


def main():
    recs = load(sys.stdin)
    # group by (event, src) == one source's fan-out for one photo
    fanouts = defaultdict(list)
    for r in recs:
        fanouts[(r.get("event"), r.get("src"))].append(r)

    # per variant index: fetches, hits (non-null median)
    idx_fetch = defaultdict(int)
    idx_hit = defaultdict(int)
    base_hit = base_total = 0          # does i=0 carry a vote?
    marginal = defaultdict(int)        # suffix i>=1 is the ONLY vote in its fan-out
    empty_fanouts = 0

    for (_, _src), rs in fanouts.items():
        hits = [r for r in rs if r.get("median") is not None]
        for r in rs:
            i = r.get("i")
            idx_fetch[i] += 1
            if r.get("median") is not None:
                idx_hit[i] += 1
        b = [r for r in rs if r.get("i") == 0]
        if b:
            base_total += 1
            if b[0].get("median") is not None:
                base_hit += 1
        if not hits:
            empty_fanouts += 1
        elif len(hits) == 1 and hits[0].get("i", 0) >= 1:
            marginal[hits[0]["i"]] += 1   # this suffix was the sole survivor

    print(f"fan-outs={len(fanouts)}  empty (zero votes)={empty_fanouts}")
    print(f"base anchor (i=0) carried a vote in {base_hit}/{base_total} fan-outs "
          f"({base_hit / base_total:.0%})" if base_total else "no base data")
    print("\nper-variant-index coverage (hits / fetches):")
    for i in sorted(idx_fetch):
        f, h = idx_fetch[i], idx_hit[i]
        m = marginal.get(i, 0)
        tag = "  <- base anchor" if i == 0 else (f"  marginal sole-vote x{m}" if m else "")
        print(f"  i={i}: {h:>3}/{f:<3} ({h / f:.0%}){tag}")

    sole = sum(marginal.values())
    print(f"\nsuffix variants (i>=1) were the SOLE vote in {sole} fan-out(s).")
    if sole == 0:
        print("VERDICT: base anchor always co-occurs with another vote — suffixes "
              "never rescue a fan-out. Trimming fan-out width risks no availability.")
    else:
        print("VERDICT: suffixes DO rescue some fan-outs — trimming would drop those "
              "to zero votes. Keep width or trim only proven-dead indices.")


if __name__ == "__main__":
    main()
