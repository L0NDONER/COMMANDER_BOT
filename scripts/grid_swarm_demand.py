#!/usr/bin/env python3
"""
Grid swarm + demand-side flexibility.

Layer 1 — Byzantine-fault-tolerant discharge consensus (from grid_swarm_discharge.py).
Layer 2 — HouseholdAgent: flexible loads shift to cheap / low-carbon windows.
           Tasks: WASH_DARKS, WASH_COLOURS, DISHWASHER, EV_CHARGE.
Layer 3 — RegionalDemand: N households per region → aggregate kW.
Layer 4 — demand → Δ feedback: excess demand nudges node local_delta.
Layer 5 — Grid batteries: Dinorwig (NorthWest) + Brechfa (SouthWest) + Faversham
           A+B (SouthEast) — charged overnight from wind, discharged at peak,
           blending effective CI down toward household green thresholds.

Heterogeneity axes:
  - per-region CI + tariff profiles (Scotland: clean/cheap; Yorkshire: dirty/expensive)
  - per-region free-wash utility partnership (4 of 8 regions)
  - per-household EV charge rate + session duration (randomised at enqueue)

Time model: SIM_HOUR_INTERVAL wall-clock seconds ≡ 1 simulated hour.
"""

import asyncio
import math
import random
import statistics
from dataclasses import dataclass, field
from enum import Enum, auto

# ── swarm parameters (from grid_swarm_discharge.py) ──────────────────────────
GOSSIP_INTERVAL          = 0.01
SAMPLE_INTERVAL          = 0.01
ACK_TIMEOUT              = 0.03
MAX_RETRIES              = 8
LOSS_PROB                = 0.1
DEFICIT_THRESHOLD        = -0.25
VOTE_THRESHOLD           = -0.10
PROPOSE_INTERVAL         = 0.05
VOTE_TIMEOUT             = 0.05
COOLDOWN_AFTER_EXECUTE   = 0.1
COOLDOWN_JITTER          = (0.7, 1.3)
MAX_BACKOFF_SHIFT        = 4
STARVATION_THRESHOLD     = 0.5
STARVATION_MIN_TIMEOUTS  = 2
WARMUP_HISTORY_LEN       = 5
CONVERGENCE_TOLERANCE    = 0.005
WARMUP_DEADLINE          = 2.0
PEER_STALE_THRESHOLD     = 0.3
SEED                     = 42

REGIONS = [
    "Scotland", "North", "NorthWest", "Yorkshire",
    "Midlands", "East", "SouthWest", "SouthEast",
]

# ── demand-side parameters ────────────────────────────────────────────────────
N_HOUSEHOLDS          = 20     # agents per region (represents ~50k real homes)
SIM_HOUR_INTERVAL     = 0.2    # wall-clock seconds per simulated hour
SIM_START_HOUR        = 0      # midnight — captures overnight EV window
N_SIM_HOURS           = 24     # full day
RUNTIME               = SIM_HOUR_INTERVAL * N_SIM_HOURS + 0.2
DEMAND_DELTA_COUPLING = 0.002  # kW excess above baseline → Δ nudge per step
BASELINE_KW_PER_HH    = 5.0   # always-on sacred load per household

SIM_MONTH       = 7   # July
SIM_DAY_OF_WEEK = 6   # Sunday


# ── per-region profiles ───────────────────────────────────────────────────────
@dataclass
class RegionalProfile:
    peak_ci:     float  # gCO2/kWh during 08:00–19:59
    shoulder_ci: float  # 06:00–07:59, 20:00–21:59
    off_ci:      float  # 00:00–05:59, 22:00–23:59
    peak_price:  float  # £/kWh during 07:00–08:59, 16:00–19:59
    off_price:   float  # £/kWh overnight (< 07:00, >= 22:00)
    free_wash:   bool   # utility partnership for free Sunday wash

    def price_at(self, hour: int) -> float:
        if hour < 7 or hour >= 22:
            return self.off_price
        if 7 <= hour < 9 or 16 <= hour < 20:
            return self.peak_price
        return (self.off_price + self.peak_price) / 2   # shoulder midpoint

    def ci_at(self, hour: int) -> float:
        if hour < 6 or hour >= 22:
            return self.off_ci
        if 8 <= hour < 20:
            return self.peak_ci
        return self.shoulder_ci


_P = RegionalProfile  # table shorthand

# ── scenario flags ────────────────────────────────────────────────────────────
SCOTLAND_SURPLUS        = True   # hydro+wind surplus: node starts positive, tariff near-zero
HORNSEA_ONLINE          = True   # Hornsea 1+2 at capacity: East/Yorkshire/North CI suppressed
WALES_BATTERIES_ENABLED = True   # Dinorwig + Brechfa: stored wind dispatched at peak
SHEPPEY_WIND_ONLINE     = True   # Isle of Sheppey offshore (London Array): SouthEast off/shoulder CI drops

BATTERY_DELTA_COUPLING  = 0.0003  # kW discharged → local_delta nudge per step
BATTERY_BELIEF_COUPLING = 0.0002  # kW discharged → direct belief nudge (soft feedback)

REGION_PROFILES: dict[str, RegionalProfile] = {
    #              peak_ci  shldr  off  peak   off   wash
    "Scotland":  _P(90,    70,    40,  0.28, 0.05, True),
    "North":     _P(240,   190,   120, 0.38, 0.07, False),
    "NorthWest": _P(210,   170,   110, 0.35, 0.07, True),
    "Yorkshire": _P(310,   240,   160, 0.42, 0.09, False),
    "Midlands":  _P(350,   270,   180, 0.40, 0.09, False),
    "East":      _P(170,   130,   80,  0.32, 0.06, True),
    "SouthWest": _P(180,   140,   90,  0.33, 0.06, True),
    "SouthEast": _P(290,   220,   140, 0.40, 0.08, False),
}

if SCOTLAND_SURPLUS:
    # Near-zero tariff: surplus spills into the grid all day
    REGION_PROFILES["Scotland"] = _P(90, 70, 40, 0.10, 0.00, True)

if HORNSEA_ONLINE:
    # Hornsea 1+2 offshore: dominates merit order for East, Yorkshire, North
    REGION_PROFILES["East"]      = _P(80,  60,  40, 0.22, 0.04, True)
    REGION_PROFILES["Yorkshire"] = _P(120, 90,  60, 0.28, 0.06, False)
    REGION_PROFILES["North"]     = _P(150, 100, 60, 0.30, 0.06, False)

if SHEPPEY_WIND_ONLINE:
    # Isle of Sheppey / London Array (630 MW): overnight + shoulder CI drops for SouthEast;
    # peak stays high — batteries needed to flatten the midday dirty peak.
    p = REGION_PROFILES["SouthEast"]
    REGION_PROFILES["SouthEast"] = _P(p.peak_ci, 130, 80, p.peak_price, p.off_price, False)

if SCOTLAND_SURPLUS and HORNSEA_ONLINE:
    # Scottish hydro + North Sea wind transmitted south overnight via interconnectors;
    # Midlands off-peak CI drops significantly (110 vs local 180) — needed so
    # Lincoln batteries can charge below the 130 gCO2 threshold.
    p = REGION_PROFILES["Midlands"]
    REGION_PROFILES["Midlands"] = _P(p.peak_ci, p.shoulder_ci, 110, p.peak_price, p.off_price, p.free_wash)


# ── grid-scale battery storage ────────────────────────────────────────────────
@dataclass
class GridBattery:
    name:           str
    region:         str
    capacity_kwh:   float
    charge_kw:      float
    discharge_kw:   float
    soc:            float = 0.5
    stored_ci:      float = 100.0
    kwh_charged:    float = 0.0
    kwh_discharged: float = 0.0
    discharge_start: int  = 8   # first hour allowed to dispatch

    def charge(self, ci: float, price: float) -> float:
        """Charge when cheap + green (overnight wind). Returns kW drawn from grid."""
        if price > 0.09 or ci > 130 or self.soc >= 0.98:
            return 0.0
        space = (1.0 - self.soc) * self.capacity_kwh
        actual = min(self.charge_kw, space)
        self.stored_ci = ci
        self.soc = min(1.0, self.soc + actual / self.capacity_kwh)
        self.kwh_charged += actual
        return actual

    def should_discharge(self, hour: int) -> bool:
        return self.soc > 0.05 and self.discharge_start <= hour < 21

    def discharge(self) -> tuple[float, float]:
        """Dispatch into peak. Returns (kW_released, stored_ci)."""
        available = self.soc * self.capacity_kwh
        actual = min(self.discharge_kw, available)
        self.soc = max(0.0, self.soc - actual / self.capacity_kwh)
        self.kwh_discharged += actual
        return actual, self.stored_ci


WALES_BATTERIES: dict[str, list[GridBattery]] = {}
if WALES_BATTERIES_ENABLED:
    WALES_BATTERIES = {
        "NorthWest": [GridBattery("Dinorwig",    "NorthWest", 900.0, 150.0, 200.0, soc=0.5)],
        "SouthWest": [GridBattery("Brechfa",     "SouthWest", 200.0,  60.0,  80.0, soc=0.5)],
        "SouthEast": [GridBattery("Faversham A", "SouthEast", 900.0, 150.0, 200.0, soc=0.5),
                      GridBattery("Faversham B", "SouthEast", 200.0,  60.0,  80.0, soc=0.5)],
        "Midlands":  [GridBattery("Lincoln A", "Midlands", 900.0, 150.0, 200.0, soc=0.5, discharge_start=16),
                      GridBattery("Lincoln B", "Midlands", 200.0,  60.0,  80.0, soc=0.5, discharge_start=16)],
    }


# ── demand model ──────────────────────────────────────────────────────────────
class TaskType(Enum):
    WASH_DARKS   = auto()
    WASH_COLOURS = auto()
    DISHWASHER   = auto()
    EV_CHARGE    = auto()


class DayOfWeek(Enum):
    MON = 0; TUE = 1; WED = 2; THU = 3; FRI = 4; SAT = 5; SUN = 6  # noqa: E702


@dataclass
class TimeContext:
    day_of_week:         DayOfWeek
    hour:                int
    month:               int
    price_per_kwh:       float
    ci:                  float
    corridor_congestion: float
    free_wash_broadcast: bool


@dataclass
class FlexibleTask:
    task_type:           TaskType
    kw:                  float          # power draw while running
    duration_hours:      float          # hours remaining
    must_finish_by_hour: int | None = None
    started:             bool = False
    finished:            bool = False


@dataclass
class HouseholdAgent:
    tou_evening_start: int   = 19
    max_ci_for_green:  float = 200.0
    max_congestion:    float = 0.7
    pending_tasks:     list[FlexibleTask] = field(default_factory=list)
    active_tasks:      list[FlexibleTask] = field(default_factory=list)
    done_types:        set[TaskType]      = field(default_factory=set)

    def _has_or_done(self, *types: TaskType) -> bool:
        return (any(t.task_type in types for t in self.pending_tasks + self.active_tasks)
                or any(tp in self.done_types for tp in types))

    def _enqueue_washes(self, ctx: TimeContext):
        if not ctx.free_wash_broadcast:
            return
        if ctx.day_of_week != DayOfWeek.SUN or ctx.month != 7:
            return
        if self._has_or_done(TaskType.WASH_DARKS, TaskType.WASH_COLOURS):
            return
        self.pending_tasks.append(FlexibleTask(TaskType.WASH_DARKS,   2.0, 1.0, 20))
        self.pending_tasks.append(FlexibleTask(TaskType.WASH_COLOURS, 2.0, 1.0, 22))

    def _enqueue_dishwasher(self):
        if self._has_or_done(TaskType.DISHWASHER):
            return
        self.pending_tasks.append(FlexibleTask(TaskType.DISHWASHER, 1.4, 1.5, 23))

    def _good_window(self, ctx: TimeContext) -> bool:
        return (ctx.price_per_kwh < 0.25
                and ctx.ci <= self.max_ci_for_green
                and ctx.corridor_congestion <= self.max_congestion)

    def _enqueue_ev(self):
        if self._has_or_done(TaskType.EV_CHARGE):
            return
        kw = random.uniform(3.0, 7.0)               # randomised per household
        duration = float(random.choice([4, 5, 6, 7, 8]))
        self.pending_tasks.append(FlexibleTask(TaskType.EV_CHARGE, kw, duration))

    def _can_start(self, task: FlexibleTask, ctx: TimeContext) -> bool:
        if task.started or task.finished:
            return False
        if (task.must_finish_by_hour is not None
                and task.must_finish_by_hour - ctx.hour <= task.duration_hours):
            return True  # deadline pressure overrides signals
        if task.task_type in (TaskType.WASH_DARKS, TaskType.WASH_COLOURS):
            if ctx.hour < 6:   # washes after 06:00 only — EV owns the night slot
                return False
        if task.task_type == TaskType.DISHWASHER and ctx.hour < self.tou_evening_start:
            return False
        if task.task_type == TaskType.EV_CHARGE:
            if not (ctx.hour >= 22 or ctx.hour < 6):   # overnight window only
                return False
        return self._good_window(ctx)

    def step(self, ctx: TimeContext) -> float:
        """Step one hour. Returns kW drawn by flexible tasks during this hour."""
        self._enqueue_washes(ctx)
        self._enqueue_dishwasher()
        self._enqueue_ev()
        to_start = [t for t in self.pending_tasks if self._can_start(t, ctx)]
        for t in to_start:
            t.started = True
            self.pending_tasks.remove(t)
            self.active_tasks.append(t)
        # Capture draw before advancing time — tasks that start and finish in one
        # step (duration=1.0) still drew power during this hour.
        power = sum(t.kw for t in self.active_tasks)
        for t in self.active_tasks:
            t.duration_hours -= 1.0
        done = [t for t in self.active_tasks if t.duration_hours <= 0.0]
        for t in done:
            t.finished = True
            self.done_types.add(t.task_type)
            self.active_tasks.remove(t)
        return power


class RegionalDemand:
    def __init__(self):
        self.households = [HouseholdAgent() for _ in range(N_HOUSEHOLDS)]
        self.history: list[tuple[int, float]] = []

    def step(self, hour: int, congestion: float, profile: RegionalProfile,
             ci_override: float | None = None) -> float:
        ci = ci_override if ci_override is not None else profile.ci_at(hour)
        ctx = TimeContext(
            day_of_week=DayOfWeek(SIM_DAY_OF_WEEK),
            hour=hour,
            month=SIM_MONTH,
            price_per_kwh=profile.price_at(hour),
            ci=ci,
            corridor_congestion=congestion,
            free_wash_broadcast=profile.free_wash,
        )
        total_kw = sum(BASELINE_KW_PER_HH + hh.step(ctx) for hh in self.households)
        self.history.append((hour, total_kw))
        return total_kw


# ── swarm layer ───────────────────────────────────────────────────────────────
class Node:
    t0 = 0.0

    def __init__(self, name, inbox, outboxes):
        self.name = name
        self.inbox = inbox
        self.outboxes = outboxes
        self.peers = [r for r in REGIONS if r != name]

        self.local_delta = random.uniform(-1.0, 0.0)
        self.belief = self.local_delta
        self.last_heard_from = {p: 0.0 for p in self.peers}

        self.seq = 0
        self.pending = {}
        self.seen = {}
        self.active_proposals = {}
        self.executed = []
        self.cooldown_until = 0.0
        self.consecutive_timeouts = 0
        self.proposals = 0
        self.consensus = 0
        self.timeouts = 0
        self.last_consensus_at = 0.0

        from collections import deque
        self.recent_deltas = deque(maxlen=WARMUP_HISTORY_LEN)
        self.warm = False
        self.warmup_finished_at = None
        self.warmup_failed = False

    def _set_cooldown(self, now, multiplier=1.0):
        jitter = random.uniform(*COOLDOWN_JITTER)
        self.cooldown_until = now + COOLDOWN_AFTER_EXECUTE * multiplier * jitter

    async def _send(self, target, msg):
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
                else:
                    delta = (msg["belief"] - self.belief) / 2
                    self.belief += delta
                    self.seen[key] = delta
                    self.recent_deltas.append(abs(delta))
                await asyncio.sleep(0.002)
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
                    await self._send(target, {"kind": "ack", "from": self.name, "seq": seq})
            elif kind == "ack":
                self.seen.pop((msg["from"], msg["seq"]), None)
            elif kind == "proposal":
                if not self.warm:
                    self.inbox.task_done()
                    continue
                vote = "YES" if self.belief < VOTE_THRESHOLD else "NO"
                if msg.get("priority"):
                    now = asyncio.get_event_loop().time()
                    self.cooldown_until = max(self.cooldown_until, now + VOTE_TIMEOUT)
                await self._send(msg["from"], {
                    "kind": "vote", "from": self.name,
                    "pid": msg["pid"], "vote": vote,
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
            self.pending[self.seq] = {"target": target, "sent_at": now, "retries": 0}
            await self._send(target, {
                "kind": "ask", "from": self.name,
                "seq": self.seq, "belief": self.belief,
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
                if now - self.last_heard_from.get(target, 0) >= PEER_STALE_THRESHOLD:
                    del self.pending[seq]
                    continue
                if info["retries"] >= MAX_RETRIES:
                    del self.pending[seq]
                    continue
                info["retries"] += 1
                info["sent_at"] = now
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
                if not self.warmup_failed:
                    if (len(self.recent_deltas) == WARMUP_HISTORY_LEN
                            and max(self.recent_deltas) < CONVERGENCE_TOLERANCE):
                        self.warm = True
                        self.warmup_finished_at = now
                        self.last_consensus_at = now
                    elif (now - Node.t0) > WARMUP_DEADLINE:
                        self.warmup_failed = True
                await asyncio.sleep(PROPOSE_INTERVAL)
                continue

            starved = (now - self.last_consensus_at > STARVATION_THRESHOLD
                       and self.consecutive_timeouts >= STARVATION_MIN_TIMEOUTS)
            if self.belief < DEFICIT_THRESHOLD and (now >= self.cooldown_until or starved):
                active_peers = [p for p in self.peers
                                if now - self.last_heard_from[p] < PEER_STALE_THRESHOLD]
                if not active_peers:
                    await asyncio.sleep(PROPOSE_INTERVAL)
                    continue
                prop_seq += 1
                self.proposals += 1
                pid = f"{self.name}:{prop_seq}"
                state = {
                    "yes": 0, "needed": len(active_peers),
                    "aborted": False, "event": asyncio.Event(),
                }
                self.active_proposals[pid] = state
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
                    self._set_cooldown(loop.time())
                elif state["yes"] >= state["needed"]:
                    self.consensus += 1
                    self.last_consensus_at = loop.time()
                    self.consecutive_timeouts = 0
                    self._set_cooldown(loop.time())
                    for p in active_peers:
                        await self._send(p, {
                            "kind": "execute", "from": self.name,
                            "pid": pid, "action": "DISCHARGE_50MW",
                        })
                else:
                    self.timeouts += 1
                    self.consecutive_timeouts = min(
                        self.consecutive_timeouts + 1, MAX_BACKOFF_SHIFT)
                    backoff = 2 ** self.consecutive_timeouts
                    self._set_cooldown(loop.time(), multiplier=backoff)
                del self.active_proposals[pid]
            await asyncio.sleep(PROPOSE_INTERVAL)


# ── demand coroutine ──────────────────────────────────────────────────────────
async def demand_loop(nodes, regional_demand, demand_samples, t0):
    for sim_hour in range(SIM_START_HOUR, SIM_START_HOUR + N_SIM_HOURS):
        await asyncio.sleep(SIM_HOUR_INTERVAL)
        all_beliefs = [nd.belief for nd in nodes.values()]
        consensus = statistics.median(all_beliefs)
        hour_kw = {}
        hour_ci = {}
        for region, rd in regional_demand.items():
            nd = nodes[region]
            profile = REGION_PROFILES[region]
            deviation = nd.belief - consensus
            congestion = max(0.0, min(1.0, 0.5 - deviation))

            # Layer 5: battery charge/discharge (multiple units per region)
            batteries = WALES_BATTERIES.get(region, [])
            battery_draw_kw = 0.0
            baseline_kw = BASELINE_KW_PER_HH * N_HOUSEHOLDS
            ci_num = profile.ci_at(sim_hour) * baseline_kw  # weighted numerator
            ci_den = baseline_kw                             # weighted denominator
            for battery in batteries:
                charge_kw = battery.charge(profile.ci_at(sim_hour), profile.price_at(sim_hour))
                if charge_kw > 0:
                    battery_draw_kw += charge_kw  # overnight charging loads the grid
                elif battery.should_discharge(sim_hour):
                    dkw, stored_ci = battery.discharge()
                    ci_num += stored_ci * dkw
                    ci_den += dkw
                    nd.local_delta += dkw * BATTERY_DELTA_COUPLING
                    nd.belief += dkw * BATTERY_BELIEF_COUPLING
            ci_override = ci_num / ci_den if ci_den > baseline_kw else None

            total_kw = rd.step(sim_hour, congestion, profile, ci_override) + battery_draw_kw
            hour_kw[region] = total_kw
            hour_ci[region] = ci_override if ci_override is not None else profile.ci_at(sim_hour)

            # Step 4: excess demand nudges local_delta and belief (closed loop)
            baseline = BASELINE_KW_PER_HH * N_HOUSEHOLDS
            excess = total_kw - baseline
            nd.local_delta += excess * DEMAND_DELTA_COUPLING
            nd.belief += excess * DEMAND_DELTA_COUPLING * 0.1

        t_ms = (asyncio.get_event_loop().time() - t0) * 1000
        demand_samples.append((t_ms, sim_hour, dict(hour_kw), dict(hour_ci)))
        avg_kw = statistics.mean(hour_kw.values())
        print(f"  {sim_hour:02d}:00  avg={avg_kw:.0f} kW  "
              + "  ".join(f"{r[:4]}={v:.0f}" for r, v in hour_kw.items()))


async def monitor(nodes, true_mean, samples, belief_samples):
    t0 = asyncio.get_event_loop().time()
    while True:
        beliefs = {n: nd.belief for n, nd in nodes.items()}
        mean_b = statistics.mean(beliefs.values())
        spread = max(beliefs.values()) - min(beliefs.values())
        rms = math.sqrt(statistics.mean((b - true_mean) ** 2 for b in beliefs.values()))
        t_ms = (asyncio.get_event_loop().time() - t0) * 1000
        samples.append((t_ms, spread, rms, mean_b))
        belief_samples.append((t_ms, dict(beliefs)))
        await asyncio.sleep(SAMPLE_INTERVAL)


# ── main ──────────────────────────────────────────────────────────────────────
async def main():
    random.seed(SEED)
    inboxes = {r: asyncio.Queue() for r in REGIONS}
    nodes = {r: Node(r, inboxes[r], inboxes) for r in REGIONS}
    regional_demand = {r: RegionalDemand() for r in REGIONS}

    if SCOTLAND_SURPLUS:
        nd = nodes["Scotland"]
        nd.local_delta = random.uniform(0.3, 0.7)   # exporting surplus
        nd.belief = nd.local_delta

    true_mean = statistics.mean(n.local_delta for n in nodes.values())
    t0 = asyncio.get_event_loop().time()
    Node.t0 = t0
    for n in nodes.values():
        for p in n.peers:
            n.last_heard_from[p] = t0

    print("=== grid swarm + demand ===")
    print(f"  regions: {len(REGIONS)}  households/region: {N_HOUSEHOLDS}")
    print(f"  sim: {SIM_START_HOUR:02d}:00–{SIM_START_HOUR + N_SIM_HOURS - 1:02d}:00  (Sunday July)")
    fw = [r for r, p in REGION_PROFILES.items() if p.free_wash]
    print(f"  free-wash regions ({len(fw)}): {', '.join(fw)}")
    if SCOTLAND_SURPLUS:
        print(f"  Scotland SURPLUS: Δ={nodes['Scotland'].local_delta:+.3f}  tariff 0–10p/kWh")
    if HORNSEA_ONLINE:
        print("  Hornsea 1+2 ONLINE: East/Yorkshire/North CI suppressed")
    if SHEPPEY_WIND_ONLINE:
        se = REGION_PROFILES["SouthEast"]
        print(f"  Isle of Sheppey wind ONLINE: SouthEast shoulder_ci={se.shoulder_ci}  off_ci={se.off_ci}")
    if WALES_BATTERIES_ENABLED:
        for batt_list in WALES_BATTERIES.values():
            for batt in batt_list:
                print(f"  Battery {batt.name} ({batt.region}): "
                      f"{batt.capacity_kwh:.0f} kWh  "
                      f"charge={batt.charge_kw:.0f} kW  "
                      f"discharge={batt.discharge_kw:.0f} kW  SOC₀={batt.soc:.0%}")
    print(f"  true global mean Δ: {true_mean:+.3f}")
    print()

    samples = []
    belief_samples = []
    demand_samples = []
    tasks = []
    for nd in nodes.values():
        tasks.append(asyncio.create_task(nd.consume()))
        tasks.append(asyncio.create_task(nd.gossip_loop()))
        tasks.append(asyncio.create_task(nd.retry_loop()))
        tasks.append(asyncio.create_task(nd.propose_loop()))
    tasks.append(asyncio.create_task(monitor(nodes, true_mean, samples, belief_samples)))
    tasks.append(asyncio.create_task(demand_loop(nodes, regional_demand, demand_samples, t0)))

    await asyncio.sleep(RUNTIME)
    for t in tasks:
        t.cancel()

    tot_p = sum(n.proposals for n in nodes.values())
    tot_c = sum(n.consensus for n in nodes.values())
    print()
    print("=== final swarm state ===")
    for r, nd in nodes.items():
        batts = WALES_BATTERIES.get(r, [])
        batt_str = ("  [B] SOC=" + "/".join(f"{b.soc:.0%}" for b in batts)) if batts else ""
        print(f"  {r:>9}: belief={nd.belief:+.3f}  Δ={nd.local_delta:+.3f}"
              f"  executed={len(nd.executed)}{batt_str}")
    print(f"  proposals: {tot_p}  consensus: {tot_c}")
    if WALES_BATTERIES_ENABLED:
        print()
        print("=== battery dispatch ===")
        for batt_list in WALES_BATTERIES.values():
            for batt in batt_list:
                print(f"  {batt.name} ({batt.region}): SOC={batt.soc:.0%}  "
                      f"charged={batt.kwh_charged:.0f} kWh  "
                      f"discharged={batt.kwh_discharged:.0f} kWh  "
                      f"stored_ci={batt.stored_ci:.0f} gCO₂/kWh")

    plot(belief_samples, demand_samples)


# ── plot ──────────────────────────────────────────────────────────────────────
def plot(belief_samples, demand_samples):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(
        3, 1, figsize=(14, 11), gridspec_kw={"height_ratios": [2, 2, 1]})
    ax1, ax2, ax3 = axes
    cmap = plt.get_cmap("tab10")

    # ── top: belief convergence ───────────────────────────────────────────────
    t_ms = [s[0] for s in belief_samples]
    for i, r in enumerate(REGIONS):
        ax1.plot(t_ms, [s[1][r] for s in belief_samples],
                 color=cmap(i), lw=1.2, label=r)
    ax1.axhline(DEFICIT_THRESHOLD, color="red", ls="--", lw=1.0, alpha=0.6,
                label=f"deficit {DEFICIT_THRESHOLD}")
    ax1.axhline(VOTE_THRESHOLD, color="orange", ls=":", lw=1.0, alpha=0.6,
                label=f"vote {VOTE_THRESHOLD}")
    ax1.set_ylabel("belief (grid Δ)")
    ax1.set_title("Layer 1 — Discharge consensus: gossip belief convergence")
    ax1.legend(loc="upper right", fontsize=7, ncol=2)
    ax1.grid(alpha=0.25)

    # ── middle: per-region demand lines ──────────────────────────────────────
    if demand_samples:
        hours = [s[1] for s in demand_samples]
        for i, r in enumerate(REGIONS):
            profile = REGION_PROFILES[r]
            kws = [s[2].get(r, 0) for s in demand_samples]
            ls = "-" if profile.free_wash else "--"
            batt_tag = " [B]" if r in WALES_BATTERIES else (" *" if profile.free_wash else "")
            ax2.step(hours, kws, where="post", color=cmap(i), lw=1.5,
                     ls=ls, label=f"{r}{batt_tag}")
        ax2.axhline(BASELINE_KW_PER_HH * N_HOUSEHOLDS, color="black",
                    ls=":", lw=0.8, alpha=0.5, label="baseline")
        ax2.set_xticks(range(0, 24, 2))
        ax2.set_xticklabels([f"{h:02d}:00" for h in range(0, 24, 2)], fontsize=8)
        ax2.set_ylabel(f"demand (kW)  [{N_HOUSEHOLDS} HH/region]")
        scenario = []
        if SCOTLAND_SURPLUS:
            scenario.append("Scotland surplus")
        if HORNSEA_ONLINE:
            scenario.append("Hornsea 1+2")
        if SHEPPEY_WIND_ONLINE:
            scenario.append("Sheppey wind")
        if WALES_BATTERIES_ENABLED:
            scenario.append("Wales+Faversham batteries")
        scene_str = "  |  ".join(scenario) if scenario else "baseline"
        title = (f"Layers 2–5 — Demand + storage  [{scene_str}]\n"
                 "EV overnight · washes · dishwasher  [B]=battery region  "
                 "solid=free-wash · dashed=standard")
        ax2.set_title(title, fontsize=9)
        ax2.legend(loc="upper right", fontsize=7, ncol=2)
        ax2.grid(alpha=0.25, axis="y")

    # ── bottom: per-region carbon intensity profiles ───────────────────────
    h_range = list(range(24))
    for i, r in enumerate(REGIONS):
        profile = REGION_PROFILES[r]
        ci_vals = [profile.ci_at(h) for h in h_range]
        ax3.step(h_range, ci_vals, where="post", color=cmap(i), lw=1.0,
                 alpha=0.7, label=r[:4])
    # Effective (battery-blended) CI for battery regions — dotted overlay
    if WALES_BATTERIES_ENABLED and demand_samples:
        batt_hours = [s[1] for s in demand_samples]
        for i, r in enumerate(REGIONS):
            if r not in WALES_BATTERIES:
                continue
            eff_ci = [s[3].get(r, REGION_PROFILES[r].ci_at(s[1])) for s in demand_samples]
            ax3.step(batt_hours, eff_ci, where="post", color=cmap(i), lw=2.0,
                     ls=":", alpha=1.0)  # dotted = effective CI with battery dispatch
    ax3.axhline(200, color="green", ls="--", lw=0.9, alpha=0.6,
                label="max_ci_for_green 200")
    ax3.set_xticks(range(0, 24, 2))
    ax3.set_xticklabels([f"{h:02d}:00" for h in range(0, 24, 2)], fontsize=8)
    ax3.set_ylabel("carbon intensity\n(gCO₂/kWh)")
    ci_note = "  dotted = battery-blended effective CI" if WALES_BATTERIES_ENABLED else ""
    ax3.set_title(f"Regional CI profiles — drives when green window opens{ci_note}")
    ax3.legend(loc="upper right", fontsize=7, ncol=4)
    ax3.grid(alpha=0.25, axis="y")

    fig.tight_layout(h_pad=1.5)
    out = "scripts/grid_swarm_demand.png"
    fig.savefig(out, dpi=120)
    print(f"plot saved → {out}")


if __name__ == "__main__":
    asyncio.run(main())
