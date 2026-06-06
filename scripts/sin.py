#!/usr/bin/env python3
"""
SIN — Self-Integrated Nervous control plane.
Arms: cognitive_load (C), emotional_amplitude (E), somatic_noise (B)
Fusion: (C + E + B) / 3
States: PLATEAU <55 | RECOVERY 55-70 | PANIC >70
"""
import time

HYSTERESIS = 3  # samples required to confirm a state transition

# Minimum seconds between outbound frames per state (0 = no limit)
MPI = {
    "PLATEAU":  0,
    "RECOVERY": 10,
    "PANIC":    30,
}

ENVELOPES = {
    "PLATEAU":  "Full fidelity. No constraint.",
    "RECOVERY": "Short frames only. Defer new threads.",
    "PANIC":    "Safety frames only. Close outbound.",
}

def classify(fusion):
    if fusion < 55:
        return "PLATEAU"
    elif fusion < 70:
        return "RECOVERY"
    return "PANIC"

def run():
    history = []
    state = "PLATEAU"
    pending = None
    pending_count = 0
    last_emit = 0.0

    print("SIN loop — enter C E B (0-100 each), or q to quit.\n")

    while True:
        try:
            raw = input("C E B > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if raw.lower() == "q":
            break

        parts = raw.split()
        if len(parts) != 3:
            print("  need three values")
            continue

        try:
            c, e, b = [float(x) for x in parts]
        except ValueError:
            print("  numbers only")
            continue

        if not all(0 <= v <= 100 for v in (c, e, b)):
            print("  values must be 0-100")
            continue

        fusion = (c + e + b) / 3
        candidate = classify(fusion)

        # hysteresis: only transition after HYSTERESIS consecutive matching samples
        if candidate != state:
            if candidate == pending:
                pending_count += 1
            else:
                pending = candidate
                pending_count = 1

            if pending_count >= HYSTERESIS:
                state = candidate
                pending = None
                pending_count = 0
        else:
            pending = None
            pending_count = 0

        history.append(fusion)
        trend = ""
        if len(history) >= 3:
            delta = history[-1] - history[-3]
            trend = " ↑" if delta > 2 else " ↓" if delta < -2 else " →"

        now = time.time()
        mpi = MPI[state]
        elapsed = now - last_emit
        if mpi and elapsed < mpi:
            print(f"  fusion={fusion:.1f}{trend}  [{state}]  held — {mpi - elapsed:.0f}s remaining")
        else:
            print(f"  fusion={fusion:.1f}{trend}  [{state}]  {ENVELOPES[state]}")
            last_emit = now
        if pending:
            print(f"  (transitioning → {pending}, {pending_count}/{HYSTERESIS})")

if __name__ == "__main__":
    run()
