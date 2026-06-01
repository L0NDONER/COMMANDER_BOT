#!/usr/bin/env python3
"""Master command centre for the courier route stack.

Stitches together:
    topology.py     — street-level TYPE_THROUGH / CLOSE / HYBRID tags
    outside_in.py   — radial sort, the dispatcher's outside-in heuristic
                      (kept as the falsification control, not a router)
    greedy_angle.py — angle-aware greedy sequencer, the live router
    route_null.py   — null harness: radial vs random×N vs greedy

Subcommands:
    plan    run greedy_angle on stdin → sequenced route
    null    run route_null harness  → kill criteria PASS/REFUTED
    radial  run outside_in control   → distance-from-home ordering
    tag     classify each manifest line → audit the topology map

All subcommands read the manifest from stdin (any whitespace-
separated postcodes / address lines). Forward any extra flags to
the underlying script — e.g.

    dispatch.py plan --pin-tail --alpha 1 --beta 1.5 < manifest.txt
    dispatch.py null --pin-tail --n 1000 --kill-cost 0.3 < manifest.txt
    dispatch.py tag < manifest.txt
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SCRIPTS = {
    "plan":   "greedy_angle.py",
    "null":   "route_null.py",
    "radial": "outside_in.py",
}

USAGE = """\
usage: dispatch.py <command> [args...] < manifest.txt

commands:
  plan    angle-aware greedy router (the live sequencer)
  null    null harness — radial vs random×N vs greedy, kill verdict
  verify  same as null but quiet on PASS, verbose on REFUTED (CI gate)
  radial  outside-in control (radial sort, the falsifier baseline)
  tag     classify each manifest line by topology tag

run `dispatch.py <command> --help` to see flags for that subcommand.
"""


def do_verify(rest: list[str]) -> int:
    """Run route_null but stay quiet on PASS. On REFUTED, dump the
    full output so the failure is debuggable in CI logs. Exit code
    propagates from route_null (0 = PASS, 1 = REFUTED)."""
    stdin_data = sys.stdin.read()
    script = HERE / SCRIPTS["null"]
    result = subprocess.run(
        [sys.executable, str(script)] + rest,
        input=stdin_data,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("verdict:") or "Δ" in stripped:
                print(stripped)
    else:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
    return result.returncode


def do_tag(rest: list[str]) -> int:
    """Read the manifest, classify each line by topology tag,
    and print address | postcode | tag. Useful for spot-checking
    the ANCHOR_MAP against a new round."""
    import topology

    pc_re = re.compile(r"\b[A-Z]{1,2}\d{1,2}\s?\d?[A-Z]{0,2}\b")
    counts = {topology.TYPE_THROUGH: 0,
              topology.TYPE_CLOSE: 0,
              topology.TYPE_HYBRID: 0}
    rows = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        m = pc_re.findall(line.upper())
        pc = m[-1] if m else "?"
        tag = topology.classify([line])
        counts[tag] = counts.get(tag, 0) + 1
        rows.append((line, pc, tag))

    glyph = {topology.TYPE_THROUGH: " ",
             topology.TYPE_CLOSE:   "▲",
             topology.TYPE_HYBRID:  "◆"}
    print(f"  {'t':1}  {'postcode':<10}  address")
    for addr, pc, tag in rows:
        print(f"  {glyph.get(tag, '?')}  {pc:<10}  {addr}")
    print()
    print(f"summary: "
          f"{counts.get(topology.TYPE_THROUGH, 0)} through, "
          f"{counts.get(topology.TYPE_CLOSE, 0)} close ▲, "
          f"{counts.get(topology.TYPE_HYBRID, 0)} hybrid ◆")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(USAGE)
        return 0

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    if cmd == "tag":
        return do_tag(rest)
    if cmd == "verify":
        return do_verify(rest)

    if cmd not in SCRIPTS:
        print(f"unknown command: {cmd!r}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    script = HERE / SCRIPTS[cmd]
    # forward stdin so subcommands can read the manifest naturally
    return subprocess.run([sys.executable, str(script)] + rest).returncode


if __name__ == "__main__":
    sys.exit(main())
