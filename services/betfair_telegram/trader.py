#!/usr/bin/env python3
"""
over25_trader.py
─────────────────────────────────────────────────────────────────────────────
Over 2.5 Goals lay trader — built on your existing BetfairClient.

Strategy:
  • Watches OVER_UNDER_25 markets for Tier 1/2 competitions only
  • Lays "Over 2.5 Goals" when the price is within ±0.09 of LAY_PRICE (2.0)
  • GREEN UP : price drifts up (0-0 game) → back to close, profit £3–£5
  • HARD STOP: loss hits MAX_LIABILITY (£20) → exit immediately
  • SOFT STOP: price collapses (goal locked in) after STOP_AFTER_MINS → exit

P&L direction:
  LAY "Over 2.5" at 2.0.  You WIN if goals DON'T happen.
  • 0-0 after 30 min → price drifts 2.0 → 2.6  → GREEN UP, back to lock profit
  • Goal scored      → price drops  2.0 → 1.3  → assess stop loss

No betfairlightweight dependency — uses your BetfairClient directly.
One bulk API call per scan tick for ALL monitored markets.

Integration (bot.py):
    from over25_trader import run_trader_loop, get_session_summary
    threading.Thread(target=run_trader_loop, daemon=True).start()
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import sys
import os
import time
import uuid
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("over25_trader")


# ═══════════════════════════════════════════════════════════════════════════════
# PRACTICE MODE
# ═══════════════════════════════════════════════════════════════════════════════
#
#   True  → NO real orders. Real prices fetched. Full logic runs. Risk nothing.
#   False → Live trading. Real money. Only flip when you're confident.
#
PRACTICE_MODE: bool = True   # ← change to False to go live


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — pulled from your existing config.py
# ═══════════════════════════════════════════════════════════════════════════════

_CONFIG_DIR = os.path.expanduser(
    os.environ.get("BETFAIR_CONFIG_DIR",
                   "/home/martin/commander/services/betfair_telegram")
)
if _CONFIG_DIR not in sys.path:
    sys.path.insert(0, _CONFIG_DIR)

try:
    import config as _cfg
except ModuleNotFoundError as exc:
    raise RuntimeError(
        f"Cannot find config.py in {_CONFIG_DIR}. "
        "Set BETFAIR_CONFIG_DIR env var if your path differs."
    ) from exc

BETFAIR_USERNAME  = _cfg.BETFAIR_USERNAME
BETFAIR_PASSWORD  = _cfg.BETFAIR_PASSWORD
BETFAIR_APP_KEY   = _cfg.BETFAIR_APP_KEY
BETFAIR_CERTS_DIR = _cfg.BETFAIR_CERTS
TELEGRAM_TOKEN    = _cfg.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID  = _cfg.TELEGRAM_CHAT_ID


# ═══════════════════════════════════════════════════════════════════════════════
# IMPORT YOUR EXISTING BetfairClient
# ═══════════════════════════════════════════════════════════════════════════════

_BF_SERVICE_DIR = os.path.join(_CONFIG_DIR, "bt_services", "betfair")
if _BF_SERVICE_DIR not in sys.path:
    sys.path.insert(0, _BF_SERVICE_DIR)

from betfair_client import BetfairClient   # your production client, no betfairlightweight


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════

LAY_PRICE:     float = 2.0   # Target lay price
LAY_TOLERANCE: float = 0.09  # Accept entry within ±this  (catches 1.95 / 2.05)
LAY_STAKE:     float = 10.0  # Backer's stake (your max win). Liability = stake * (price-1)
MIN_PROFIT:    float = 3.0   # Green up: exit when unrealised P&L >= this
MAX_PROFIT:    float = 5.0   # Green up: exit when unrealised P&L >= this (upper band)
MAX_HOLD_MINS: int   = 90    # Force-exit after this long regardless

# ── Stop loss ────────────────────────────────────────────────────────────────
MAX_LIABILITY:         float = 20.0  # HARD stop: exit if loss hits this
STOP_PRICE_MULTIPLIER: float = 1.5   # SOFT stop threshold = entry / 1.5 (e.g. 2.0/1.5 = 1.33)
STOP_AFTER_MINS:       int   = 20    # SOFT stop only fires after this many minutes

# ── Timing ───────────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS:  int = 10    # How often to re-check prices of open + candidate markets
CATALOGUE_REFRESH_MINS: int = 20    # How often to re-fetch the market catalogue

# ── Competition tier filter ──────────────────────────────────────────────────
# Betfair competition IDs.  These match your FOOTBALL_COMPETITION_IDS in config.py
# plus Serie A and La Liga which are ideal for laying Over 2.5 (defensive football).
#
# Set ALLOWED_COMPETITION_IDS = [] to allow ALL competitions (not recommended —
# will pick up midnight South American football with illiquid prices).

TIER_1: List[str] = [
    "39",    # English Premier League      — deepest liquidity
    "228",   # UEFA Champions League       — excellent liquidity
    "2005",  # UEFA Europa League          — good liquidity
    "81",    # Serie A                     — low scoring, ideal for laying Over 2.5
    "87",    # La Liga                     — solid liquidity
]

TIER_2: List[str] = [
    "2",     # Championship                — decent but thinner
    "117",   # English Championship (alt)  — Betfair uses either ID
    "30",    # FA Cup
    "55",    # Ligue 1
    "10932", # UEFA Europa Conference League
]

ALLOWED_COMPETITION_IDS: List[str] = TIER_1 + TIER_2

# Betfair constants
FOOTBALL_EVENT_TYPE_ID = "1"
MARKET_TYPE_OVER25     = "OVER_UNDER_25"
OVER25_RUNNER_NAME     = "Over 2.5 Goals"


# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM HELPER
# ═══════════════════════════════════════════════════════════════════════════════

def _tg(msg: str) -> None:
    """Fire-and-forget Telegram message. Never raises."""
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=8,
        )
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# PRACTICE LEDGER
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SimulatedBet:
    bet_id:      str
    event_name:  str
    market_id:   str
    runner_id:   int
    entry_price: float
    stake:       float
    opened_at:   float = field(default_factory=time.time)
    closed_at:   Optional[float] = None
    close_price: Optional[float] = None
    pnl:         Optional[float] = None
    exit_reason: str = ""


class PracticeLedger:
    def __init__(self):
        self._lock = threading.Lock()
        self._bets: List[SimulatedBet] = []

    def open_bet(self, event_name: str, market_id: str, runner_id: int,
                 entry_price: float, stake: float) -> SimulatedBet:
        bet = SimulatedBet(
            bet_id      = f"SIM-{uuid.uuid4().hex[:8].upper()}",
            event_name  = event_name,
            market_id   = market_id,
            runner_id   = runner_id,
            entry_price = entry_price,
            stake       = stake,
        )
        with self._lock:
            self._bets.append(bet)
        log.info("[PRACTICE] Opened – %s  %s", bet.bet_id, event_name)
        return bet

    def close_bet(self, bet: SimulatedBet, close_price: float,
                  pnl: float, reason: str) -> None:
        with self._lock:
            bet.closed_at   = time.time()
            bet.close_price = close_price
            bet.pnl         = pnl
            bet.exit_reason = reason
        log.info("[PRACTICE] Closed %s | %s | P&L=£%.2f", bet.bet_id, reason, pnl)

    def find_open(self, market_id: str, runner_id: int) -> Optional[SimulatedBet]:
        with self._lock:
            return next(
                (b for b in self._bets
                 if b.market_id == market_id
                 and b.runner_id == runner_id
                 and b.pnl is None),
                None,
            )

    def summary(self) -> str:
        with self._lock:
            closed = [b for b in self._bets if b.pnl is not None]
            open_  = [b for b in self._bets if b.pnl is None]

        if not closed and not open_:
            return "📋 *Practice Session*\nNo bets recorded yet."

        total_pnl = sum(b.pnl for b in closed)
        winners   = [b for b in closed if b.pnl > 0]
        losers    = [b for b in closed if b.pnl <= 0]

        lines = ["📋 *Practice Session Summary*", ""]
        for b in closed:
            icon = "✅" if b.pnl > 0 else "🔴"
            dur  = (b.closed_at - b.opened_at) / 60
            lines.append(
                f"{icon} {b.event_name}\n"
                f"   {b.entry_price} → {b.close_price} | "
                f"P&L £{b.pnl:+.2f} | {dur:.0f} min | {b.exit_reason}"
            )
        if open_:
            lines += ["", f"⏳ Still open: {', '.join(b.event_name for b in open_)}"]
        lines += [
            "",
            f"Closed: {len(closed)}  ({len(winners)}W / {len(losers)}L)",
            f"Total P&L: £{total_pnl:+.2f}",
        ]
        if closed:
            lines.append(f"Avg/trade: £{total_pnl / len(closed):+.2f}")
        return "\n".join(lines)


_LEDGER = PracticeLedger()


# ═══════════════════════════════════════════════════════════════════════════════
# BETFAIR SESSION  (module-level singleton — one login, kept alive by client)
# ═══════════════════════════════════════════════════════════════════════════════

_BF_CLIENT: Optional[BetfairClient] = None
_BF_LOCK   = threading.Lock()


def get_client() -> BetfairClient:
    """Return the authenticated BetfairClient, creating it once."""
    global _BF_CLIENT
    with _BF_LOCK:
        if _BF_CLIENT is None:
            _BF_CLIENT = BetfairClient(
                username = BETFAIR_USERNAME,
                password = BETFAIR_PASSWORD,
                app_key  = BETFAIR_APP_KEY,
                certs    = BETFAIR_CERTS_DIR,
            )
            _BF_CLIENT.login()
            log.info("BetfairClient session created")
        return _BF_CLIENT


# ═══════════════════════════════════════════════════════════════════════════════
# MARKET CATALOGUE  (refreshed every CATALOGUE_REFRESH_MINS)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Over25Market:
    market_id:   str
    event_name:  str
    competition: str
    runner_id:   int     # selection ID for "Over 2.5 Goals"


_CATALOGUE:         List[Over25Market] = []
_LAST_CATALOGUE_T:  float = 0.0
_CATALOGUE_LOCK     = threading.Lock()


def _refresh_catalogue() -> None:
    """
    Fetch all live OVER_UNDER_25 markets for Tier 1/2 competitions.
    Calls _rpc() directly since BetfairClient has no dedicated Over25 method.
    """
    global _CATALOGUE, _LAST_CATALOGUE_T

    bf  = get_client()
    now = datetime.now(timezone.utc)

    params = {
        "filter": {
            "eventTypeIds":    [FOOTBALL_EVENT_TYPE_ID],
            "competitionIds":  ALLOWED_COMPETITION_IDS,
            "marketTypeCodes": [MARKET_TYPE_OVER25],
            "inPlayOnly":      True,
            "marketStartTime": {
                "from": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to":   (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        },
        "marketProjection": ["COMPETITION", "EVENT", "RUNNER_DESCRIPTION"],
        "maxResults": 100,
    }

    raw = bf._rpc("listMarketCatalogue", params)
    if not isinstance(raw, list):
        log.warning("Unexpected catalogue response: %r", raw)
        return

    new_catalogue: List[Over25Market] = []
    for m in raw:
        market_id   = m.get("marketId")
        event_name  = m.get("event", {}).get("name", "Unknown")
        competition = m.get("competition", {}).get("name", "Unknown")

        runner_id = None
        for r in m.get("runners", []):
            if OVER25_RUNNER_NAME.lower() in r.get("runnerName", "").lower():
                runner_id = r.get("selectionId")
                break

        if not market_id or not runner_id:
            continue

        new_catalogue.append(Over25Market(
            market_id   = market_id,
            event_name  = event_name,
            competition = competition,
            runner_id   = runner_id,
        ))

    with _CATALOGUE_LOCK:
        _CATALOGUE        = new_catalogue
        _LAST_CATALOGUE_T = time.time()

    comp_names = sorted({m.competition for m in new_catalogue})
    log.info("Catalogue: %d Over 2.5 markets | %s", len(new_catalogue), comp_names)


def get_catalogue(force: bool = False) -> List[Over25Market]:
    age = time.time() - _LAST_CATALOGUE_T
    if force or age > (CATALOGUE_REFRESH_MINS * 60) or not _CATALOGUE:
        _refresh_catalogue()
    with _CATALOGUE_LOCK:
        return list(_CATALOGUE)


# ═══════════════════════════════════════════════════════════════════════════════
# PRICE FETCHING  (one bulk API call for ALL markets per tick)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class MarketState:
    best_back: Optional[float]
    best_lay:  Optional[float]
    suspended: bool   # True when back price <= 1.02 (market suspended/closed)


def get_market_states(markets: List[Over25Market]) -> Dict[str, MarketState]:
    """
    Single bulk listMarketBook call for all markets.
    Returns {market_id: MarketState}.
    One API call regardless of how many markets are being watched.
    """
    if not markets:
        return {}

    bf = get_client()
    params = {
        "marketIds": [m.market_id for m in markets],
        "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"],
            "exBestOffersOverrides": {
                "bestPricesDepth": 1,
                "rollupModel":     "STAKE",
                "rollupLimit":     0,
            },
        },
    }

    raw = bf._rpc("listMarketBook", params)
    states: Dict[str, MarketState] = {}

    if not isinstance(raw, list):
        return states

    runner_lookup = {m.market_id: m.runner_id for m in markets}

    for book in raw:
        market_id        = book.get("marketId")
        target_runner_id = runner_lookup.get(market_id)
        best_back = best_lay = None

        for runner in book.get("runners", []):
            if runner.get("selectionId") != target_runner_id:
                continue
            ex  = runner.get("ex", {})
            atb = ex.get("availableToBack", [])
            atl = ex.get("availableToLay",  [])
            if atb:
                best_back = float(atb[0]["price"])
            if atl:
                best_lay = float(atl[0]["price"])
            break

        states[market_id] = MarketState(
            best_back = best_back,
            best_lay  = best_lay,
            suspended = (best_back is not None and best_back <= 1.02),
        )

    return states


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER PLACEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def _place_order(market_id: str, runner_id: int, side: str,
                 price: float, stake: float) -> bool:
    bf = get_client()
    try:
        result = bf._rpc("placeOrders", {
            "marketId": market_id,
            "instructions": [{
                "selectionId": runner_id,
                "side":        side,
                "orderType":   "LIMIT",
                "limitOrder": {
                    "size":            round(stake, 2),
                    "price":           price,
                    "persistenceType": "LAPSE",
                },
            }],
        })
        if isinstance(result, dict) and result.get("status") == "SUCCESS":
            return True
        log.error("placeOrders failed: %s", result)
        return False
    except Exception as exc:
        log.error("placeOrders exception: %s", exc)
        return False


def place_lay(market: Over25Market, price: float, stake: float) -> Optional[str]:
    """Place a lay. Returns bet ID string or None on failure."""
    liability = round(stake * (price - 1), 2)

    if PRACTICE_MODE:
        bet = _LEDGER.open_bet(market.event_name, market.market_id,
                               market.runner_id, price, stake)
        log.info("[PRACTICE] LAY | %s | %s | price=%.2f stake=£%.2f liability=£%.2f",
                 market.competition, market.event_name, price, stake, liability)
        return bet.bet_id

    if _place_order(market.market_id, market.runner_id, "LAY", price, stake):
        bet_id = f"LIVE-{market.market_id[-6:]}"
        log.info("LAY | %s | price=%.2f stake=£%.2f liability=£%.2f",
                 market.event_name, price, stake, liability)
        return bet_id
    return None


def close_position(market: Over25Market, back_price: float, stake: float,
                   pnl: float, reason: str) -> bool:
    """Back to close the lay. Returns True on success."""
    if PRACTICE_MODE:
        sim = _LEDGER.find_open(market.market_id, market.runner_id)
        if sim:
            _LEDGER.close_bet(sim, close_price=back_price, pnl=pnl, reason=reason)
        log.info("[PRACTICE] CLOSE | %s | %.2f | P&L=£%.2f | %s",
                 market.event_name, back_price, pnl, reason)
        return True

    return _place_order(market.market_id, market.runner_id, "BACK", back_price, stake)


# ═══════════════════════════════════════════════════════════════════════════════
# P&L
# ═══════════════════════════════════════════════════════════════════════════════

def calc_pnl(entry_price: float, current_back: float, stake: float) -> float:
    """
    Unrealised P&L for open LAY.
    Positive = price drifted up (no goals) = profit.
    Negative = price dropped (goal scored) = loss.
    """
    return round(stake * (current_back - entry_price), 2)


# ═══════════════════════════════════════════════════════════════════════════════
# MONITOR  (one thread per open position)
# ═══════════════════════════════════════════════════════════════════════════════

def _monitor(market: Over25Market, entry_price: float,
             stake: float, bet_id: str) -> None:
    start_time           = time.time()
    deadline             = start_time + (MAX_HOLD_MINS * 60)
    stop_price_threshold = round(entry_price / STOP_PRICE_MULTIPLIER, 2)
    liability            = round(stake * (entry_price - 1), 2)
    tag                  = "🧪" if PRACTICE_MODE else "⚡"

    _tg(
        f"{tag} *Over 2.5 Lay Active*\n"
        f"🏆 {market.competition}\n"
        f"⚽ {market.event_name}\n"
        f"ID: `{bet_id}`\n"
        f"Entry: {entry_price} | Stake: £{stake} | Liability: £{liability}\n"
        f"🟢 Green up @ £{MIN_PROFIT}–£{MAX_PROFIT}\n"
        f"🛑 Hard stop @ loss £{MAX_LIABILITY}\n"
        f"🔴 Soft stop @ price ≤ {stop_price_threshold} after {STOP_AFTER_MINS} min"
    )

    while time.time() < deadline:
        time.sleep(SCAN_INTERVAL_SECONDS)
        elapsed = (time.time() - start_time) / 60

        states = get_market_states([market])
        state  = states.get(market.market_id)

        if not state or state.best_back is None:
            log.warning("No price for %s – skipping tick", market.event_name)
            continue

        if state.suspended:
            log.info("Suspended (back=%.2f) – %s", state.best_back, market.event_name)
            continue

        back = state.best_back
        lay  = state.best_lay
        pnl  = calc_pnl(entry_price, back, stake)

        log.info("TICK | %-32s | back=%.2f lay=%-5s | P&L=£%+.2f | %.0f min",
                 market.event_name, back,
                 f"{lay:.2f}" if lay else "N/A", pnl, elapsed)

        # 1. HARD STOP
        if pnl <= -MAX_LIABILITY:
            ok = close_position(market, back, stake, pnl, "HARD_STOP")
            _tg(f"🛑 *HARD STOP – {market.event_name}*\n"
                f"Loss £{abs(pnl):.2f} hit max £{MAX_LIABILITY}\n"
                f"@ {back} | {'✅' if ok else '⚠️ ORDER FAILED'}")
            return

        # 2. SOFT STOP
        if lay is not None and lay <= stop_price_threshold:
            if elapsed >= STOP_AFTER_MINS:
                ok = close_position(market, back, stake, pnl, "SOFT_STOP")
                _tg(f"🔴 *SOFT STOP – {market.event_name}*\n"
                    f"Price {lay} ≤ {stop_price_threshold} | {elapsed:.0f} min\n"
                    f"Loss £{abs(pnl):.2f} | @ {back} | {'✅' if ok else '⚠️ ORDER FAILED'}")
                return
            else:
                _tg(f"⚠️ *Price Warning – {market.event_name}*\n"
                    f"Lay {lay} ≤ {stop_price_threshold} but only {elapsed:.0f} min\n"
                    f"Holding until min {STOP_AFTER_MINS} | loss so far £{abs(pnl):.2f}")

        # 3. GREEN UP
        elif MIN_PROFIT <= pnl <= MAX_PROFIT:
            ok = close_position(market, back, stake, pnl, "GREEN_UP")
            _tg(f"✅ *GREEN UP – {market.event_name}*\n"
                f"Profit *£{pnl:.2f}* | {entry_price} → {back}\n"
                f"{'✅ Order placed' if ok else '⚠️ ORDER FAILED'}")
            return

        # 4. OVER-RUN
        elif pnl > MAX_PROFIT:
            _tg(f"📈 *Over-run – {market.event_name}*\n"
                f"P&L £{pnl:.2f} above band – still open\n"
                f"Consider greening up manually on Betfair")

    # DEADLINE
    states = get_market_states([market])
    state  = states.get(market.market_id)
    if state and state.best_back:
        pnl = calc_pnl(entry_price, state.best_back, stake)
        ok  = close_position(market, state.best_back, stake, pnl, "DEADLINE")
        _tg(f"⏰ *Deadline – {market.event_name}*\n"
            f"{MAX_HOLD_MINS} min | P&L £{pnl:.2f} | @ {state.best_back}\n"
            f"{'✅' if ok else '⚠️ ORDER FAILED – check Betfair'}")
    else:
        _tg(f"⏰ *Deadline – {market.event_name}*\nNo price — check Betfair\n`{market.market_id}`")


# ═══════════════════════════════════════════════════════════════════════════════
# SCAN  (called every SCAN_INTERVAL_SECONDS by the main loop)
# ═══════════════════════════════════════════════════════════════════════════════

def _scan_once() -> str:
    """
    1. Get catalogue of live Over 2.5 markets (Tier 1/2 only, cached)
    2. Bulk-fetch all prices in ONE API call
    3. For any market in the price window not already being watched → open position
    """
    markets  = get_catalogue()
    tag      = "🧪" if PRACTICE_MODE else "⚡"

    if not markets:
        return f"{tag} No live Over 2.5 markets in Tier 1/2 competitions."

    states           = get_market_states(markets)
    already_watching = {t.name for t in threading.enumerate()}
    placed           = 0

    for market in markets:
        if f"monitor_{market.market_id}" in already_watching:
            continue

        state = states.get(market.market_id)
        if not state or state.suspended or state.best_lay is None:
            continue

        price = state.best_lay
        if not (LAY_PRICE - LAY_TOLERANCE <= price <= LAY_PRICE + LAY_TOLERANCE):
            log.info("Price %.2f out of window | %s [%s]",
                     price, market.event_name, market.competition)
            continue

        log.info("ENTRY | %s | %s | lay=%.2f", market.competition, market.event_name, price)

        bet_id = place_lay(market, price, LAY_STAKE)
        if not bet_id:
            _tg(f"⚠️ Order failed for {market.event_name}")
            continue

        placed += 1
        threading.Thread(
            target = _monitor,
            args   = (market, price, LAY_STAKE, bet_id),
            daemon = True,
            name   = f"monitor_{market.market_id}",
        ).start()

    if placed == 0:
        comps = sorted({m.competition for m in markets})
        return (f"{tag} Scanned {len(markets)} markets "
                f"({len(comps)} competitions) – none at {LAY_PRICE} ± {LAY_TOLERANCE}")
    return f"{tag} ✅ {placed} lay order(s) placed at ~{LAY_PRICE}"


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def run_trader_loop() -> None:
    """
    Persistent loop. Session stays alive via BetfairClient's built-in keepAlive.
    Stop with Ctrl+C or /betfair stop.
    """
    tag  = "🧪 PRACTICE" if PRACTICE_MODE else "⚡ LIVE"
    log.info("Trader loop started [%s] – scan every %ds", tag, SCAN_INTERVAL_SECONDS)
    _tg(
        f"{'🧪' if PRACTICE_MODE else '⚡'} *Over 2.5 Trader Started*\n"
        f"Mode: {tag}\n"
        f"Target: {LAY_PRICE} ± {LAY_TOLERANCE} | Stake: £{LAY_STAKE}\n"
        f"Competitions: Tier 1 (PL, UCL, UEL, Serie A, La Liga) + Tier 2 (Championship, etc)\n"
        f"Scan every {SCAN_INTERVAL_SECONDS}s | Catalogue refresh every {CATALOGUE_REFRESH_MINS} min\n"
        f"🛑 Hard stop £{MAX_LIABILITY} | 🔴 Soft stop after {STOP_AFTER_MINS} min"
    )

    scan_count = 0
    while True:
        try:
            scan_count += 1
            result = _scan_once()
            log.info("Scan #%d: %s", scan_count, result)
            if scan_count == 1 or "placed" in result:
                _tg(result)
            time.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            log.info("Trader loop stopped by user")
            _tg("🛑 *Over 2.5 Trader stopped*")
            if PRACTICE_MODE:
                _tg(_LEDGER.summary())
            break
        except Exception as exc:
            log.error("Scan error: %s", exc, exc_info=True)
            _tg(f"⚠️ Scan error: {exc}\nRetrying in {SCAN_INTERVAL_SECONDS}s")
            time.sleep(SCAN_INTERVAL_SECONDS)


def get_session_summary() -> str:
    if not PRACTICE_MODE:
        return "⚠️ Summary only available in PRACTICE_MODE."
    return _LEDGER.summary()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI  —  python3 over25_trader.py
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        run_trader_loop()
    except KeyboardInterrupt:
        pass
    if PRACTICE_MODE:
        print("\n" + get_session_summary())
