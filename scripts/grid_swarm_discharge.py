#!/usr/bin/env python3
"""
Grid swarm + battery discharge consensus.

Builds on grid_swarm.py's gossip-converged belief about global Δ.
When a node's belief drops below DEFICIT_THRESHOLD, it proposes a
DISCHARGE action. Peers vote YES iff their own belief also confirms
deficit; one NO vetoes. Unanimous YES → broadcast EXECUTE.
"""

import asyncio
import collections
import math
import random
import statistics

TICK = 0.002
GOSSIP_INTERVAL = 0.01
SAMPLE_INTERVAL = 0.01
RUNTIME = 10.0
LOSS_PROB = 0.1
ACK_TIMEOUT = 0.03
MAX_RETRIES = 8

# Consensus layer
DEFICIT_THRESHOLD = -0.25   # propose DISCHARGE when belief drops below this
VOTE_THRESHOLD    = -0.10   # vote YES iff own belief also below this
PROPOSE_INTERVAL  = 0.05    # how often each node re-checks its belief
VOTE_TIMEOUT      = 0.05    # give up on this proposal after this long
COOLDOWN_AFTER_EXECUTE = 0.1  # refractory period after an executed discharge
COOLDOWN_JITTER = (0.7, 1.3)  # per-node multiplier so wakeups stagger
MAX_BACKOFF_SHIFT = 4         # cap exponential backoff at 2**4 = 16× cooldown

# Priority Token (fairness for starved nodes).
STARVATION_THRESHOLD = 0.5    # seconds without own consensus → eligible
STARVATION_MIN_TIMEOUTS = 2   # also requires this many consecutive timeouts

# Warmup gate (gossip convergence before consensus comes online).
WARMUP_HISTORY_LEN = 5        # samples of |belief delta| tracked per node
CONVERGENCE_TOLERANCE = 0.005 # max |delta| in window → node declares warm
WARMUP_DEADLINE = 2.0         # if not warm by this, node refuses to operate

SEED = 42                     # repeatable runs

# Partition test: nodes in this set have both inbound and outbound dropped.
PARTITIONED = set()
PEER_STALE_THRESHOLD = 0.3    # peer excluded from quorum after this long silent

VERBOSE = True                # set False during sweeps to silence event logs


def vprint(*args, **kwargs):
    if VERBOSE:
        print(*args, **kwargs)


REGIONS = [
    "Scotland", "North", "NorthWest", "Yorkshire",
    "Midlands", "East", "SouthWest", "SouthEast",
]


class Node:
    t0 = 0.0  # set by main() before tasks start

    def __init__(self, name, inbox, outboxes):
        self.name = name
        self.inbox = inbox
        self.outboxes = outboxes
        self.peers = [r for r in REGIONS if r != name]

        self.local_delta = random.uniform(-1.0, 0.0)
        self.belief = self.local_delta

        # Liveness tracking: timestamp of last message received per peer.
        self.last_heard_from = {p: 0.0 for p in self.peers}

        # Gossip state.
        self.seq = 0
        self.pending = {}
        self.seen = {}

        # Consensus state: pid -> {yes, needed, aborted, event}.
        self.active_proposals = {}
        self.executed = []
        self.cooldown_until = 0.0
        self.consecutive_timeouts = 0

        # Efficiency counters.
        self.proposals = 0
        self.consensus = 0
        self.timeouts = 0
        self.vetoes = 0
        self.peak_backoff = 0
        self.priority_used = 0
        self.last_consensus_at = 0.0

        # Warmup state.
        self.recent_deltas = collections.deque(maxlen=WARMUP_HISTORY_LEN)
        self.warm = False
        self.warmup_finished_at = None
        self.warmup_failed = False

        self.retries_sent = 0
        self.dups_replayed = 0

    def _set_cooldown(self, now, multiplier=1.0):
        jitter = random.uniform(*COOLDOWN_JITTER)
        self.cooldown_until = now + COOLDOWN_AFTER_EXECUTE * multiplier * jitter

    async def _send(self, target, msg):
        """Outbound wire: respects partition (both directions) + lossy."""
        if self.name in PARTITIONED or target in PARTITIONED:
            return
        if random.random() < LOSS_PROB:
            return
        await self.outboxes[target].put(msg)

    async def consume(self):
        while True:
            msg = await self.inbox.get()
            kind = msg["kind"]

            if "from" in msg:
                self.last_heard_from[msg["from"]] = asyncio.get_event_loop().time()

            if kind == "ask":
                key = (msg["from"], msg["seq"])
                if key in self.seen:
                    delta = self.seen[key]
                    self.dups_replayed += 1
                else:
                    delta = (msg["belief"] - self.belief) / 2
                    self.belief += delta
                    self.seen[key] = delta
                    self.recent_deltas.append(abs(delta))
                await asyncio.sleep(TICK)
                await self._send(msg["from"], {
                    "kind": "reply", "from": self.name,
                    "seq": msg["seq"], "delta": delta,
                })

            elif kind == "reply":
                seq = msg["seq"]
                if seq in self.pending:
                    self.belief -= msg["delta"]
                    self.recent_deltas.append(abs(msg["delta"]))
                    target = self.pending[seq]["target"]
                    del self.pending[seq]
                    await self._send(target, {
                        "kind": "ack", "from": self.name, "seq": seq,
                    })

            elif kind == "ack":
                self.seen.pop((msg["from"], msg["seq"]), None)

            elif kind == "proposal":
                if not self.warm:
                    # Fail closed: don't vote on consensus actions until warm.
                    # Non-response → proposer hits TIMEOUT, not VETOED.
                    self.inbox.task_done()
                    continue
                vote = "YES" if self.belief < VOTE_THRESHOLD else "NO"
                if msg.get("priority"):
                    # Yield airtime: don't fire a competing proposal while
                    # the starved node's votes are in flight.
                    now = asyncio.get_event_loop().time()
                    self.cooldown_until = max(self.cooldown_until, now + VOTE_TIMEOUT)
                await self._send(msg["from"], {
                    "kind": "vote", "from": self.name,
                    "pid": msg["pid"], "vote": vote,
                    "belief": self.belief,
                })

            elif kind == "vote":
                state = self.active_proposals.get(msg["pid"])
                if state is not None:
                    if msg["vote"] == "NO":
                        state["aborted"] = True
                        state["event"].set()
                    else:
                        state["yes"] += 1
                        if state["yes"] >= state["needed"]:
                            state["event"].set()

            elif kind == "execute":
                self.executed.append(msg["pid"])
                self._set_cooldown(asyncio.get_event_loop().time())
                self.consecutive_timeouts = 0
                vprint(f"[{self.name}] EXECUTE {msg['action']} "
                       f"(pid={msg['pid']}, belief={self.belief:+.3f})")

            self.inbox.task_done()

    async def gossip_loop(self):
        loop = asyncio.get_event_loop()
        while True:
            now = loop.time()
            active = [p for p in self.peers
                      if now - self.last_heard_from[p] < PEER_STALE_THRESHOLD]
            if not active:
                await asyncio.sleep(GOSSIP_INTERVAL)
                continue
            target = random.choice(active)
            self.seq += 1
            my_seq = self.seq
            self.pending[my_seq] = {
                "target": target, "sent_at": now, "retries": 0,
            }
            await self._send(target, {
                "kind": "ask", "from": self.name,
                "seq": my_seq, "belief": self.belief,
            })
            await asyncio.sleep(GOSSIP_INTERVAL)

    async def retry_loop(self):
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(ACK_TIMEOUT)
            now = loop.time()
            for seq, info in list(self.pending.items()):
                if now - info["sent_at"] < ACK_TIMEOUT:
                    continue
                target = info["target"]
                # Don't keep retrying into the void: abandon pending entries
                # for peers we've stopped hearing from entirely.
                if now - self.last_heard_from[target] >= PEER_STALE_THRESHOLD:
                    del self.pending[seq]
                    continue
                if info["retries"] >= MAX_RETRIES:
                    del self.pending[seq]
                    continue
                info["retries"] += 1
                info["sent_at"] = now
                self.retries_sent += 1
                await self._send(target, {
                    "kind": "ask", "from": self.name,
                    "seq": seq, "belief": self.belief,
                })

    async def propose_loop(self):
        prop_seq = 0
        loop = asyncio.get_event_loop()
        while True:
            now = loop.time()

            if not self.warm:
                if self.warmup_failed:
                    await asyncio.sleep(PROPOSE_INTERVAL)
                    continue
                if (len(self.recent_deltas) == WARMUP_HISTORY_LEN
                        and max(self.recent_deltas) < CONVERGENCE_TOLERANCE):
                    self.warm = True
                    self.warmup_finished_at = now
                    self.last_consensus_at = now  # reset starvation clock
                    vprint(f"[{self.name}] WARM @ t={(now - Node.t0)*1000:.0f}ms "
                           f"(belief={self.belief:+.3f})")
                elif (now - Node.t0) > WARMUP_DEADLINE:
                    self.warmup_failed = True
                    worst = max(self.recent_deltas) if self.recent_deltas else None
                    vprint(f"[{self.name}] WARMUP FAILED @ "
                           f"t={(now - Node.t0)*1000:.0f}ms "
                           f"(belief={self.belief:+.3f}, "
                           f"worst_delta={worst})")
                    await asyncio.sleep(PROPOSE_INTERVAL)
                    continue
                else:
                    await asyncio.sleep(PROPOSE_INTERVAL)
                    continue

            starved = (
                now - self.last_consensus_at > STARVATION_THRESHOLD
                and self.consecutive_timeouts >= STARVATION_MIN_TIMEOUTS
            )
            can_propose = now >= self.cooldown_until or starved

            if self.belief < DEFICIT_THRESHOLD and can_propose:
                # Dynamic quorum: exclude peers we haven't heard from recently.
                active_peers = [
                    p for p in self.peers
                    if now - self.last_heard_from[p] < PEER_STALE_THRESHOLD
                ]
                if not active_peers:
                    await asyncio.sleep(PROPOSE_INTERVAL)
                    continue

                prop_seq += 1
                self.proposals += 1
                if starved:
                    self.priority_used += 1
                pid = f"{self.name}:{prop_seq}"
                state = {
                    "yes": 0, "needed": len(active_peers),
                    "aborted": False, "event": asyncio.Event(),
                }
                self.active_proposals[pid] = state

                tag = " [PRIORITY]" if starved else ""
                excl = len(self.peers) - len(active_peers)
                excl_tag = f" excl={excl}" if excl else ""
                vprint(f"[{self.name}] PROPOSE{tag} DISCHARGE "
                       f"(pid={pid}, belief={self.belief:+.3f}, "
                       f"quorum={len(active_peers)}{excl_tag})")
                for p in active_peers:
                    await self._send(p, {
                        "kind": "proposal", "from": self.name,
                        "pid": pid, "action": "DISCHARGE_50MW",
                        "priority": starved,
                    })

                try:
                    await asyncio.wait_for(state["event"].wait(), VOTE_TIMEOUT)
                except asyncio.TimeoutError:
                    pass

                if state["aborted"]:
                    self.vetoes += 1
                    self._set_cooldown(loop.time())
                    vprint(f"[{self.name}] VETOED "
                           f"(pid={pid}, yes={state['yes']}/{state['needed']})")
                elif state["yes"] >= state["needed"]:
                    self.consensus += 1
                    vprint(f"[{self.name}] CONSENSUS → broadcast EXECUTE (pid={pid})")
                    self.executed.append(pid)
                    self._set_cooldown(loop.time())
                    self.consecutive_timeouts = 0
                    self.last_consensus_at = loop.time()
                    for p in active_peers:
                        await self._send(p, {
                            "kind": "execute", "from": self.name,
                            "pid": pid, "action": "DISCHARGE_50MW",
                        })
                else:
                    self.timeouts += 1
                    self.consecutive_timeouts = min(
                        self.consecutive_timeouts + 1, MAX_BACKOFF_SHIFT
                    )
                    backoff = 2 ** self.consecutive_timeouts
                    self.peak_backoff = max(self.peak_backoff, backoff)
                    self._set_cooldown(loop.time(), multiplier=backoff)
                    vprint(f"[{self.name}] TIMEOUT "
                           f"(pid={pid}, yes={state['yes']}/{state['needed']}, "
                           f"backoff={backoff}×)")

                del self.active_proposals[pid]

            await asyncio.sleep(PROPOSE_INTERVAL)


async def monitor(nodes, true_mean, samples):
    t0 = asyncio.get_event_loop().time()
    while True:
        beliefs = [n.belief for n in nodes.values()]
        spread = max(beliefs) - min(beliefs)
        rms = math.sqrt(statistics.mean((b - true_mean) ** 2 for b in beliefs))
        mean_b = statistics.mean(beliefs)
        t_ms = (asyncio.get_event_loop().time() - t0) * 1000
        samples.append((t_ms, spread, rms, mean_b))
        await asyncio.sleep(SAMPLE_INTERVAL)


async def main(loss_prob=None, verbose=True, runtime=None, partitioned=None):
    global LOSS_PROB, VERBOSE, PARTITIONED
    if loss_prob is not None:
        LOSS_PROB = loss_prob
    VERBOSE = verbose
    PARTITIONED = set(partitioned) if partitioned else set()
    rt = runtime if runtime is not None else RUNTIME

    random.seed(SEED)
    inboxes = {r: asyncio.Queue() for r in REGIONS}
    nodes = {r: Node(r, inboxes[r], inboxes) for r in REGIONS}

    if PARTITIONED:
        vprint(f"=== partitioned nodes: {sorted(PARTITIONED)} ===")

    true_mean = statistics.mean(n.local_delta for n in nodes.values())
    vprint(f"=== local Δs ===")
    for r, n in nodes.items():
        vprint(f"  {r:>9}: {n.local_delta:+.3f}")
    vprint(f"  true global mean: {true_mean:+.3f}")
    vprint(f"  propose threshold: {DEFICIT_THRESHOLD}, "
           f"vote threshold: {VOTE_THRESHOLD}")
    vprint(f"=== gossip + consensus starts ===")

    Node.t0 = asyncio.get_event_loop().time()
    # Optimistic liveness init: assume every peer is alive at startup so
    # gossip can find them. PEER_STALE_THRESHOLD then reaps the silent ones.
    for n in nodes.values():
        for p in n.peers:
            n.last_heard_from[p] = Node.t0
    samples = []
    tasks = []
    for node in nodes.values():
        tasks.append(asyncio.create_task(node.consume()))
        tasks.append(asyncio.create_task(node.gossip_loop()))
        tasks.append(asyncio.create_task(node.retry_loop()))
        tasks.append(asyncio.create_task(node.propose_loop()))
    tasks.append(asyncio.create_task(monitor(nodes, true_mean, samples)))

    await asyncio.sleep(rt)

    for t in tasks:
        t.cancel()

    tot_p = sum(n.proposals for n in nodes.values())
    tot_c = sum(n.consensus for n in nodes.values())
    tot_v = sum(n.vetoes for n in nodes.values())
    tot_t = sum(n.timeouts for n in nodes.values())
    tot_pr = sum(n.priority_used for n in nodes.values())
    max_peak_bo = max(n.peak_backoff for n in nodes.values())

    warmup_rel = [n.warmup_finished_at - Node.t0
                  for n in nodes.values() if n.warmup_finished_at is not None]
    failed = [r for r, n in nodes.items() if n.warmup_failed]
    all_warm = len(warmup_rel) == len(nodes)
    last_warm = max(warmup_rel) if warmup_rel else None
    warm_window = (rt - last_warm) if (all_warm and last_warm is not None) else 0.0
    exec_per_s = (tot_c / warm_window) if warm_window > 0 else 0.0

    if verbose:
        print(f"=== final state ===")
        for r, n in nodes.items():
            tag = ""
            if r in PARTITIONED:
                tag = "  [PARTITIONED]"
            elif n.warmup_failed:
                tag = "  [WARMUP FAILED]"
            print(f"  {r:>9}: belief={n.belief:+.3f}  "
                  f"executed={len(n.executed)}{tag}")
        print(f"  true global mean: {true_mean:+.3f}")
        total_retries = sum(n.retries_sent for n in nodes.values())
        total_dups = sum(n.dups_replayed for n in nodes.values())
        print(f"  retries: {total_retries}, duplicate asks replayed: {total_dups}")
        print(f"=== consensus efficiency ===")
        print(f"  {'node':>9}  {'props':>6}  {'cons':>5}  {'veto':>5}  "
              f"{'tmout':>6}  {'peak_bo':>8}  {'prio':>5}")
        for r, n in nodes.items():
            print(f"  {r:>9}  {n.proposals:6d}  {n.consensus:5d}  {n.vetoes:5d}  "
                  f"{n.timeouts:6d}  {n.peak_backoff:7d}×  {n.priority_used:5d}")
        print(f"  {'TOTAL':>9}  {tot_p:6d}  {tot_c:5d}  {tot_v:5d}  {tot_t:6d}  "
              f"{'':>8}  {tot_pr:5d}")
        if tot_c:
            print(f"  timeouts_per_execute: {tot_t / tot_c:.2f}")
            print(f"  vetoes_per_execute:   {tot_v / tot_c:.2f}")
        print(f"  executes/sec (whole run): {tot_c / rt:.1f}")
        if all_warm:
            first = min(warmup_rel)
            print(f"  warmup: first={first*1000:.0f}ms, "
                  f"last={last_warm*1000:.0f}ms, "
                  f"warm window={warm_window:.2f}s")
            if warm_window > 0:
                print(f"  executes/sec (warm only): {exec_per_s:.1f}")
        else:
            failed_names = [r for r, n in nodes.items() if n.warmup_failed]
            stuck = [r for r, n in nodes.items()
                     if n.warmup_finished_at is None and not n.warmup_failed]
            if failed_names:
                print(f"  warmup deadline tripped: {failed_names}")
            if stuck:
                print(f"  WARNING: nodes still trying to warm: {stuck}")

    return {
        "loss": LOSS_PROB,
        "props": tot_p, "cons": tot_c, "veto": tot_v, "tmout": tot_t,
        "prio": tot_pr, "peak_bo": max_peak_bo,
        "warm_last_ms": last_warm * 1000 if last_warm is not None else None,
        "all_warm": all_warm,
        "failed_nodes": len(failed),
        "exec_per_s": exec_per_s,
        "tmout_per_exec": (tot_t / tot_c) if tot_c else float("inf"),
    }


async def sweep():
    losses = [0.05, 0.10, 0.20, 0.30, 0.50, 0.70, 0.85, 0.90, 0.95, 0.98]
    rows = []
    for lp in losses:
        r = await main(loss_prob=lp, verbose=False, runtime=5.0)
        rows.append(r)
    print()
    print(f"=== sweep summary ===")
    print(f"  {'loss':>5}  {'exec/s':>7}  {'tm/ex':>6}  {'cons':>5}  "
          f"{'veto':>4}  {'prio':>4}  {'pk_bo':>5}  {'warm_ms':>7}  "
          f"{'failed':>6}")
    for r in rows:
        warm = f"{r['warm_last_ms']:.0f}" if r['warm_last_ms'] is not None else "—"
        print(f"  {r['loss']:5.2f}  {r['exec_per_s']:7.2f}  "
              f"{r['tmout_per_exec']:6.2f}  {r['cons']:5d}  {r['veto']:4d}  "
              f"{r['prio']:4d}  {r['peak_bo']:4d}×  {warm:>7}  "
              f"{r['failed_nodes']:6d}")


if __name__ == "__main__":
    import sys
    if "--sweep" in sys.argv:
        asyncio.run(sweep())
    elif "--partition" in sys.argv:
        idx = sys.argv.index("--partition")
        names = sys.argv[idx + 1].split(",") if idx + 1 < len(sys.argv) else ["North"]
        asyncio.run(main(partitioned=names, runtime=5.0))
    else:
        asyncio.run(main())
