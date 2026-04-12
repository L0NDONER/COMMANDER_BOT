#!/usr/bin/env python3
"""
goals35_trader.py
─────────────────────────────────────────────────────────────────────────────
Automated Over 3.5 Goals lay trader using an existing BetfairClient.

Design choices:
- Focus on liquid football markets only.
- Enter near kickoff when the market price is most reliable.
- Use a two-rung ladder to reduce liability quickly.
- Use proper lay-to-back hedge maths for every back order.
- Exit fully by a hard time deadline instead of improvising.
- In practice mode, keep a simple ledger for review.

This script uses the user's existing BetfairClient directly.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("goals35_trader")


# ═══════════════════════════════════════════════════════════════════════════════
# PRACTICE MODE
# ═══════════════════════════════════════════════════════════════════════════════
PRACTICE_MODE: bool = True


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG — pulled from the existing config.py
# ═══════════════════════════════════════════════════════════════════════════════
_CONFIG_DIR = os.path.expanduser(
    os.environ.get(
        "BETFAIR_CONFIG_DIR",
        "/home/martin/commander/services/betfair_telegram",
    )
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

BETFAIR_USERNAME = _cfg.BETFAIR_USERNAME
BETFAIR_PASSWORD = _cfg.BETFAIR_PASSWORD
BETFAIR_APP_KEY = _cfg.BETFAIR_APP_KEY
BETFAIR_CERTS_DIR = _cfg.BETFAIR_CERTS
TELEGRAM_TOKEN = _cfg.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = _cfg.TELEGRAM_CHAT_ID


# ═══════════════════════════════════════════════════════════════════════════════
# IMPORT THE EXISTING BetfairClient
# ═══════════════════════════════════════════════════════════════════════════════
_BF_SERVICE_DIR = os.path.join(_CONFIG_DIR, "bt_services", "betfair")
if _BF_SERVICE_DIR not in sys.path:
    sys.path.insert(0, _BF_SERVICE_DIR)

from betfair_client import BetfairClient


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════
# Entry.
LAY_PRICE: float = 3.60
LAY_TOLERANCE: float = 0.15
LAY_STAKE: float = 30.00
MAX_HOLD_MINS: int = 20

# Two-rung ladder.
# Ladder 1 is front-loaded to cut exposure fast.
# Ladder 2 is a stronger cleanup hedge if the market keeps drifting.
LADDER_1_PRICE: float = 3.90
LADDER_1_FRACTION: float = 0.55
LADDER_2_PRICE: float = 4.80
LADDER_2_FRACTION: float = 1.00
TARGET_EXIT_PRICE: float = 5.00

# Risk controls.
# Full liability for a £30 lay @ 3.60 is £78.00.
MAX_LOSS: float = 78.00
STOP_AFTER_MINS: int = 20
EARLY_ADVERSE_LOSS: float = 10.00

# Market quality filters.
MIN_MATCHED_TOTAL: float = 10000.00
MAX_FAVOURITE_PRICE: float = 1.60
MIN_FAVOURITE_PRICE: float = 1.70

# Timing.
SCAN_INTERVAL_SECONDS: int = 5
CATALOGUE_REFRESH_MINS: int = 10
ENTRY_WINDOW_START_MINS: float = 0.00
ENTRY_WINDOW_END_MINS: float = 2.00

# Competition filter.
TIER_1: List[str] = [
    "39",     # Premier League
    "228",    # Serie A
    "2005",   # Bundesliga
    "81",     # La Liga
    "87",     # Ligue 1
]

TIER_2: List[str] = [
    "2",      # Championship
    "117",    # Segunda Division
    "30",     # 2. Bundesliga
    "55",     # Serie B
    "10932",  # League One
]

ALLOWED_COMPETITION_IDS: List[str] = TIER_1 + TIER_2

FOOTBALL_EVENT_TYPE_ID = "1"
MARKET_TYPE_GOALS_35 = "OVER_UNDER_35"
GOALS_35_RUNNER_NAME = "Over 3.5 Goals"
MATCH_ODDS_MARKET_TYPE = "MATCH_ODDS"


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
    except Exception as exc:  # pragma: no cover
        log.warning("Telegram send failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# PRACTICE LEDGER
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class SimulatedBet:
    bet_id: str
    event_name: str
    market_id: str
    runner_id: int
    entry_price: float
    lay_stake: float
    opened_at: float = field(default_factory=time.time)
    closed_at: Optional[float] = None
    close_price: Optional[float] = None
    pnl: Optional[float] = None
    exit_reason: str = ""


class PracticeLedger:
    """Simple in-memory practice ledger for quick review."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bets: List[SimulatedBet] = []

    def open_bet(
        self,
        event_name: str,
        market_id: str,
        runner_id: int,
        entry_price: float,
        lay_stake: float,
    ) -> SimulatedBet:
        bet = SimulatedBet(
            bet_id=f"SIM-{uuid.uuid4().hex[:8].upper()}",
            event_name=event_name,
            market_id=market_id,
            runner_id=runner_id,
            entry_price=entry_price,
            lay_stake=lay_stake,
        )
        with self._lock:
            self._bets.append(bet)
        log.info("[PRACTICE] Opened %s %s", bet.bet_id, event_name)
        return bet

    def close_bet(
        self,
        bet: SimulatedBet,
        close_price: float,
        pnl: float,
        reason: str,
    ) -> None:
        with self._lock:
            bet.closed_at = time.time()
            bet.close_price = close_price
            bet.pnl = pnl
            bet.exit_reason = reason
        log.info("[PRACTICE] Closed %s | %s | P&L=£%.2f", bet.bet_id, reason, pnl)

    def find_open(self, market_id: str, runner_id: int) -> Optional[SimulatedBet]:
        with self._lock:
            return next(
                (
                    bet
                    for bet in self._bets
                    if bet.market_id == market_id
                    and bet.runner_id == runner_id
                    and bet.pnl is None
                ),
                None,
            )

    def summary(self) -> str:
        with self._lock:
            closed = [bet for bet in self._bets if bet.pnl is not None]
            open_bets = [bet for bet in self._bets if bet.pnl is None]

        if not closed and not open_bets:
            return "📋 *Practice Session*\nNo bets recorded yet."

        total_pnl = sum(bet.pnl for bet in closed if bet.pnl is not None)
        winners = [bet for bet in closed if bet.pnl is not None and bet.pnl > 0]
        losers = [bet for bet in closed if bet.pnl is not None and bet.pnl <= 0]

        lines = ["📋 *Practice Session Summary*", ""]
        for bet in closed:
            assert bet.pnl is not None
            icon = "✅" if bet.pnl > 0 else "🔴"
            duration = ((bet.closed_at or time.time()) - bet.opened_at) / 60
            lines.append(
                f"{icon} {bet.event_name}\n"
                f"   {bet.entry_price} → {bet.close_price} | "
                f"P&L £{bet.pnl:+.2f} | {duration:.0f} min | {bet.exit_reason}"
            )

        if open_bets:
            lines += ["", f"⏳ Still open: {', '.join(b.event_name for b in open_bets)}"]

        lines += [
            "",
            f"Closed: {len(closed)} ({len(winners)}W / {len(losers)}L)",
            f"Total P&L: £{total_pnl:+.2f}",
        ]
        if closed:
            lines.append(f"Avg/trade: £{total_pnl / len(closed):+.2f}")
        return "\n".join(lines)


_LEDGER = PracticeLedger()


# ═══════════════════════════════════════════════════════════════════════════════
# BETFAIR SESSION
# ═══════════════════════════════════════════════════════════════════════════════
_BF_CLIENT: Optional[BetfairClient] = None
_BF_LOCK = threading.Lock()


def get_client() -> BetfairClient:
    """Return the authenticated BetfairClient, creating it once."""
    global _BF_CLIENT
    with _BF_LOCK:
        if _BF_CLIENT is None:
            _BF_CLIENT = BetfairClient(
                username=BETFAIR_USERNAME,
                password=BETFAIR_PASSWORD,
                app_key=BETFAIR_APP_KEY,
                certs=BETFAIR_CERTS_DIR,
            )
            _BF_CLIENT.login()
            log.info("BetfairClient session created")
        return _BF_CLIENT


# ═══════════════════════════════════════════════════════════════════════════════
# MARKET CATALOGUE
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class GoalsMarket:
    market_id: str
    event_id: str
    event_name: str
    competition: str
    runner_id: int
    market_start_time: Optional[datetime] = None


_CATALOGUE: List[GoalsMarket] = []
_MATCH_ODDS_CATALOGUE: Dict[str, str] = {}
_LAST_CATALOGUE_T: float = 0.0
_CATALOGUE_LOCK = threading.Lock()


def _parse_market_start_time(raw_time: Optional[str]) -> Optional[datetime]:
    if not raw_time:
        return None
    try:
        return datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        try:
            return datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None


def _extract_match_odds_lookup(raw: object) -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    if not isinstance(raw, list):
        return lookup

    for market in raw:
        event_id = str(market.get("event", {}).get("id", ""))
        market_id = market.get("marketId")
        if event_id and market_id:
            lookup[event_id] = market_id
    return lookup


def _refresh_catalogue() -> None:
    """Fetch live Over 3.5 and Match Odds markets for allowed competitions."""
    global _CATALOGUE, _MATCH_ODDS_CATALOGUE, _LAST_CATALOGUE_T

    bf = get_client()
    now = datetime.now(timezone.utc)
    base_filter = {
        "eventTypeIds": [FOOTBALL_EVENT_TYPE_ID],
        "competitionIds": ALLOWED_COMPETITION_IDS,
        "inPlayOnly": True,
        "marketStartTime": {
            "from": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }

    goals_params = {
        "filter": {
            **base_filter,
            "marketTypeCodes": [MARKET_TYPE_GOALS_35],
        },
        "marketProjection": [
            "COMPETITION",
            "EVENT",
            "RUNNER_DESCRIPTION",
            "MARKET_START_TIME",
        ],
        "maxResults": 100,
    }

    match_odds_params = {
        "filter": {
            **base_filter,
            "marketTypeCodes": [MATCH_ODDS_MARKET_TYPE],
        },
        "marketProjection": ["EVENT"],
        "maxResults": 100,
    }

    goals_raw = bf._rpc("listMarketCatalogue", goals_params)
    match_odds_raw = bf._rpc("listMarketCatalogue", match_odds_params)

    if not isinstance(goals_raw, list):
        log.warning("Unexpected goals catalogue response: %r", goals_raw)
        return

    new_catalogue: List[GoalsMarket] = []
    for market in goals_raw:
        market_id = market.get("marketId")
        event = market.get("event", {})
        event_id = str(event.get("id", ""))
        event_name = event.get("name", "Unknown")
        competition = market.get("competition", {}).get("name", "Unknown")
        runner_id = None

        for runner in market.get("runners", []):
            runner_name = runner.get("runnerName", "")
            if GOALS_35_RUNNER_NAME.lower() in runner_name.lower():
                runner_id = runner.get("selectionId")
                break

        if not market_id or not event_id or not runner_id:
            continue

        new_catalogue.append(
            GoalsMarket(
                market_id=market_id,
                event_id=event_id,
                event_name=event_name,
                competition=competition,
                runner_id=runner_id,
                market_start_time=_parse_market_start_time(
                    market.get("marketStartTime")
                ),
            )
        )

    with _CATALOGUE_LOCK:
        _CATALOGUE = new_catalogue
        _MATCH_ODDS_CATALOGUE = _extract_match_odds_lookup(match_odds_raw)
        _LAST_CATALOGUE_T = time.time()

    competitions = sorted({market.competition for market in new_catalogue})
    log.info("Catalogue: %d Over 3.5 markets | %s", len(new_catalogue), competitions)


def get_catalogue(force: bool = False) -> List[GoalsMarket]:
    """Return the refreshed market catalogue when needed."""
    age = time.time() - _LAST_CATALOGUE_T
    if force or age > (CATALOGUE_REFRESH_MINS * 60) or not _CATALOGUE:
        _refresh_catalogue()
    with _CATALOGUE_LOCK:
        return list(_CATALOGUE)


def get_match_odds_lookup() -> Dict[str, str]:
    """Return event_id -> match_odds_market_id lookup."""
    with _CATALOGUE_LOCK:
        return dict(_MATCH_ODDS_CATALOGUE)


# ═══════════════════════════════════════════════════════════════════════════════
# PRICE FETCHING
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class MarketState:
    best_back: Optional[float]
    best_lay: Optional[float]
    matched_total: float
    suspended: bool


@dataclass
class MatchOddsState:
    favourite_back: Optional[float]


def get_market_states(markets: List[GoalsMarket]) -> Dict[str, MarketState]:
    """Single bulk listMarketBook call for all Over 3.5 markets."""
    if not markets:
        return {}

    bf = get_client()
    params = {
        "marketIds": [market.market_id for market in markets],
        "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"],
            "exBestOffersOverrides": {
                "bestPricesDepth": 1,
                "rollupModel": "STAKE",
                "rollupLimit": 0,
            },
        },
    }

    raw = bf._rpc("listMarketBook", params)
    states: Dict[str, MarketState] = {}
    if not isinstance(raw, list):
        return states

    runner_lookup = {market.market_id: market.runner_id for market in markets}
    for book in raw:
        market_id = book.get("marketId")
        target_runner_id = runner_lookup.get(market_id)
        best_back = None
        best_lay = None

        for runner in book.get("runners", []):
            if runner.get("selectionId") != target_runner_id:
                continue
            ex = runner.get("ex", {})
            available_to_back = ex.get("availableToBack", [])
            available_to_lay = ex.get("availableToLay", [])
            if available_to_back:
                best_back = float(available_to_back[0]["price"])
            if available_to_lay:
                best_lay = float(available_to_lay[0]["price"])
            break

        states[market_id] = MarketState(
            best_back=best_back,
            best_lay=best_lay,
            matched_total=float(book.get("totalMatched", 0.0)),
            suspended=(book.get("status") != "OPEN"),
        )
    return states


def get_match_odds_states(market_ids: List[str]) -> Dict[str, MatchOddsState]:
    """Fetch best back prices for Match Odds markets and derive favourite price."""
    if not market_ids:
        return {}

    bf = get_client()
    params = {
        "marketIds": market_ids,
        "priceProjection": {
            "priceData": ["EX_BEST_OFFERS"],
            "exBestOffersOverrides": {
                "bestPricesDepth": 1,
                "rollupModel": "STAKE",
                "rollupLimit": 0,
            },
        },
    }

    raw = bf._rpc("listMarketBook", params)
    states: Dict[str, MatchOddsState] = {}
    if not isinstance(raw, list):
        return states

    for book in raw:
        market_id = book.get("marketId")
        best_prices: List[float] = []
        for runner in book.get("runners", []):
            ex = runner.get("ex", {})
            available_to_back = ex.get("availableToBack", [])
            if available_to_back:
                best_prices.append(float(available_to_back[0]["price"]))

        favourite_back = min(best_prices) if best_prices else None
        states[market_id] = MatchOddsState(favourite_back=favourite_back)
    return states


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION MODEL AND MATHS
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class HedgeFill:
    price: float
    stake: float
    reason: str
    placed_at: float = field(default_factory=time.time)


@dataclass
class Position:
    market: GoalsMarket
    entry_price: float
    lay_stake: float
    bet_id: str
    hedges: List[HedgeFill] = field(default_factory=list)
    ladder_1_done: bool = False
    ladder_2_done: bool = False
    fully_closed: bool = False

    @property
    def matched_back_stake(self) -> float:
        return round(sum(fill.stake for fill in self.hedges), 2)

    def liability(self) -> float:
        return round(self.lay_stake * (self.entry_price - 1), 2)

    def green_profit_at(self, back_price: float) -> float:
        total_back_stake = self.matched_back_stake + calc_full_green_back_stake(
            self.entry_price,
            self.remaining_lay_units(),
            back_price,
        )
        return round(self.lay_stake - total_back_stake, 2)

    def remaining_lay_units(self) -> float:
        """
        Return remaining lay stake units still requiring a hedge.

        Each back hedge covers lay stake units equal to:
            back_stake * back_price / entry_price
        """
        covered_lay_units = sum(
            (fill.stake * fill.price) / self.entry_price for fill in self.hedges
        )
        remaining = max(0.0, self.lay_stake - covered_lay_units)
        return round(remaining, 4)

    def realised_profit(self) -> float:
        return round(self.lay_stake - self.matched_back_stake, 2)

    def close_price(self) -> Optional[float]:
        if not self.hedges:
            return None
        return self.hedges[-1].price


def calc_full_green_back_stake(
    entry_price: float,
    remaining_lay_stake: float,
    current_back_price: float,
) -> float:
    """Return the back stake needed to fully hedge remaining lay exposure."""
    if current_back_price <= 0 or remaining_lay_stake <= 0:
        return 0.0
    return round((entry_price * remaining_lay_stake) / current_back_price, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER PLACEMENT
# ═══════════════════════════════════════════════════════════════════════════════
def _place_order(
    market_id: str,
    runner_id: int,
    side: str,
    price: float,
    stake: float,
) -> bool:
    bf = get_client()
    try:
        result = bf._rpc(
            "placeOrders",
            {
                "marketId": market_id,
                "instructions": [
                    {
                        "selectionId": runner_id,
                        "side": side,
                        "orderType": "LIMIT",
                        "limitOrder": {
                            "size": round(stake, 2),
                            "price": price,
                            "persistenceType": "PERSIST",
                        },
                    }
                ],
            },
        )
        if isinstance(result, dict) and result.get("status") == "SUCCESS":
            return True
        log.error("placeOrders failed: %s", result)
        return False
    except Exception as exc:  # pragma: no cover
        log.error("placeOrders exception: %s", exc)
        return False


def place_lay(market: GoalsMarket, price: float, stake: float) -> Optional[str]:
    """Place a lay. Returns a bet id string or None on failure."""
    liability = round(stake * (price - 1), 2)
    if PRACTICE_MODE:
        bet = _LEDGER.open_bet(
            market.event_name,
            market.market_id,
            market.runner_id,
            price,
            stake,
        )
        log.info(
            "[PRACTICE] LAY | %s | %s | price=%.2f stake=£%.2f liability=£%.2f",
            market.competition,
            market.event_name,
            price,
            stake,
            liability,
        )
        return bet.bet_id

    if _place_order(market.market_id, market.runner_id, "LAY", price, stake):
        bet_id = f"LIVE-{market.market_id[-6:]}"
        log.info(
            "LAY | %s | price=%.2f stake=£%.2f liability=£%.2f",
            market.event_name,
            price,
            stake,
            liability,
        )
        return bet_id
    return None


def place_back(position: Position, back_price: float, back_stake: float, reason: str) -> bool:
    """Back part or all of the position to reduce exposure."""
    if back_stake <= 0:
        return False

    if PRACTICE_MODE:
        position.hedges.append(
            HedgeFill(price=back_price, stake=back_stake, reason=reason)
        )
        log.info(
            "[PRACTICE] BACK | %s | %.2f | stake=£%.2f | %s",
            position.market.event_name,
            back_price,
            back_stake,
            reason,
        )
        return True

    ok = _place_order(
        position.market.market_id,
        position.market.runner_id,
        "BACK",
        back_price,
        back_stake,
    )
    if ok:
        position.hedges.append(
            HedgeFill(price=back_price, stake=back_stake, reason=reason)
        )
    return ok


def finalise_practice_bet(position: Position, reason: str) -> None:
    """Close the simulated ledger entry once a practice trade finishes."""
    if not PRACTICE_MODE:
        return
    sim_bet = _LEDGER.find_open(position.market.market_id, position.market.runner_id)
    if sim_bet is None:
        return
    _LEDGER.close_bet(
        sim_bet,
        close_price=position.close_price() or position.entry_price,
        pnl=position.realised_profit(),
        reason=reason,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# MONITOR
# ═══════════════════════════════════════════════════════════════════════════════
def _minutes_since_start(market: GoalsMarket) -> Optional[float]:
    if market.market_start_time is None:
        return None
    now = datetime.now(timezone.utc)
    return max(0.0, (now - market.market_start_time).total_seconds() / 60)


def _close_remaining(position: Position, back_price: float, reason: str) -> bool:
    """Fully hedge the remaining lay exposure at the current back price."""
    remaining_lay = position.remaining_lay_units()
    if remaining_lay <= 0:
        return False
    hedge_stake = calc_full_green_back_stake(
        position.entry_price,
        remaining_lay,
        back_price,
    )
    return place_back(position, back_price, hedge_stake, reason)


def _monitor(position: Position) -> None:
    """Monitor one open trade until it is closed or the deadline is hit."""
    start_time = time.time()
    deadline = start_time + (MAX_HOLD_MINS * 60)
    tag = "🧪" if PRACTICE_MODE else "⚡"

    _tg(
        f"{tag} *Over 3.5 Lay Active*\n"
        f"🏆 {position.market.competition}\n"
        f"⚽ {position.market.event_name}\n"
        f"ID: `{position.bet_id}`\n"
        f"Entry: {position.entry_price:.2f} | Lay stake: £{position.lay_stake:.2f} | "
        f"Liability: £{position.liability():.2f}\n"
        f"Ladder: {LADDER_1_PRICE:.2f} / {LADDER_2_PRICE:.2f} / {TARGET_EXIT_PRICE:.2f}\n"
        f"Deadline: {MAX_HOLD_MINS} min | Early adverse cut: -£{EARLY_ADVERSE_LOSS:.2f}"
    )

    final_reason = ""

    while time.time() < deadline:
        time.sleep(SCAN_INTERVAL_SECONDS)
        elapsed = (time.time() - start_time) / 60
        states = get_market_states([position.market])
        state = states.get(position.market.market_id)

        if not state or state.best_back is None:
            log.warning("No price for %s – skipping tick", position.market.event_name)
            continue

        if state.suspended:
            log.info("Suspended – %s", position.market.event_name)
            continue

        back = state.best_back
        lay = state.best_lay
        green_profit = position.green_profit_at(back)
        current_loss = min(0.0, green_profit)
        remaining_lay = position.remaining_lay_units()

        log.info(
            "TICK | %-32s | back=%.2f lay=%-5s | green=£%+.2f | "
            "remaining_units=%.2f | %.0f min",
            position.market.event_name,
            back,
            f"{lay:.2f}" if lay else "N/A",
            green_profit,
            remaining_lay,
            elapsed,
        )

        if (
            not position.ladder_1_done
            and back < position.entry_price
            and abs(current_loss) >= EARLY_ADVERSE_LOSS
        ):
            ok = _close_remaining(position, back, "EARLY_ADVERSE_MOVE")
            final_reason = "EARLY_ADVERSE_MOVE"
            _tg(
                f"🛑 *Early Adverse Move – {position.market.event_name}*\n"
                f"Back {back:.2f} moved against entry {position.entry_price:.2f}\n"
                f"Closed before ladder 1 | {'✅' if ok else '⚠️ ORDER FAILED'}"
            )
            position.fully_closed = ok
            break

        if abs(current_loss) >= MAX_LOSS:
            ok = _close_remaining(position, back, "HARD_STOP")
            final_reason = "HARD_STOP"
            _tg(
                f"🛑 *Hard Stop – {position.market.event_name}*\n"
                f"Loss £{abs(current_loss):.2f} hit max £{MAX_LOSS:.2f}\n"
                f"Closed @ {back:.2f} | {'✅' if ok else '⚠️ ORDER FAILED'}"
            )
            position.fully_closed = ok
            break

        if not position.ladder_1_done and back >= LADDER_1_PRICE and remaining_lay > 0:
            full_hedge = calc_full_green_back_stake(
                position.entry_price,
                remaining_lay,
                back,
            )
            hedge_stake = round(full_hedge * LADDER_1_FRACTION, 2)
            ok = place_back(position, back, hedge_stake, "LADDER_1")
            if ok:
                position.ladder_1_done = True
                _tg(
                    f"🪜 *Ladder 1 – {position.market.event_name}*\n"
                    f"Back £{hedge_stake:.2f} @ {back:.2f}\n"
                    f"Realised so far £{position.realised_profit():+.2f}"
                )
            continue

        remaining_lay = position.remaining_lay_units()
        if not position.ladder_2_done and back >= LADDER_2_PRICE and remaining_lay > 0:
            full_hedge = calc_full_green_back_stake(
                position.entry_price,
                remaining_lay,
                back,
            )
            hedge_stake = round(full_hedge * LADDER_2_FRACTION, 2)
            ok = place_back(position, back, hedge_stake, "LADDER_2")
            if ok:
                position.ladder_2_done = True
                _tg(
                    f"🪜 *Ladder 2 – {position.market.event_name}*\n"
                    f"Back £{hedge_stake:.2f} @ {back:.2f}\n"
                    f"Realised so far £{position.realised_profit():+.2f}"
                )
            continue

        remaining_lay = position.remaining_lay_units()
        if back >= TARGET_EXIT_PRICE and remaining_lay > 0:
            ok = _close_remaining(position, back, "TARGET_EXIT")
            final_reason = "TARGET_EXIT"
            _tg(
                f"✅ *Target Exit – {position.market.event_name}*\n"
                f"Fully hedged @ {back:.2f}\n"
                f"Locked profit £{position.realised_profit():.2f} | "
                f"{'✅' if ok else '⚠️ ORDER FAILED'}"
            )
            position.fully_closed = ok
            break

    if not position.fully_closed:
        states = get_market_states([position.market])
        state = states.get(position.market.market_id)
        if state and state.best_back is not None:
            ok = _close_remaining(position, state.best_back, "DEADLINE")
            final_reason = final_reason or "DEADLINE"
            _tg(
                f"⏰ *Deadline – {position.market.event_name}*\n"
                f"Closed @ {state.best_back:.2f}\n"
                f"Locked P&L £{position.realised_profit():+.2f} | "
                f"{'✅' if ok else '⚠️ ORDER FAILED'}"
            )
            position.fully_closed = ok
        else:
            _tg(f"⏰ *Deadline – {position.market.event_name}*\nNo price — check Betfair")
            final_reason = final_reason or "DEADLINE_NO_PRICE"

    if position.hedges:
        finalise_practice_bet(position, final_reason or "CLOSED")


# ═══════════════════════════════════════════════════════════════════════════════
# SCAN
# ═══════════════════════════════════════════════════════════════════════════════
def _eligible_for_entry(market: GoalsMarket) -> bool:
    """Only enter near kickoff, when the trade thesis is still intact."""
    in_play_mins = _minutes_since_start(market)
    if in_play_mins is None:
        return False
    return ENTRY_WINDOW_START_MINS <= in_play_mins <= ENTRY_WINDOW_END_MINS


def _passes_match_quality(
    market: GoalsMarket,
    market_state: MarketState,
    favourite_price: Optional[float],
) -> bool:
    """Filter out thin or obviously too explosive matches."""
    if market_state.matched_total < MIN_MATCHED_TOTAL:
        log.info(
            "Skip %s | matched total %.2f below minimum %.2f",
            market.event_name,
            market_state.matched_total,
            MIN_MATCHED_TOTAL,
        )
        return False

    if favourite_price is None:
        log.info("Skip %s | no favourite price available", market.event_name)
        return False

    if favourite_price < MAX_FAVOURITE_PRICE:
        log.info(
            "Skip %s | favourite %.2f too short",
            market.event_name,
            favourite_price,
        )
        return False

    if favourite_price > MIN_FAVOURITE_PRICE and favourite_price < 10.0:
        return True

    log.info(
        "Skip %s | favourite %.2f outside preferred range",
        market.event_name,
        favourite_price,
    )
    return False


def _scan_once() -> str:
    """Find eligible matches, place lays, and launch monitor threads."""
    markets = get_catalogue()
    tag = "🧪" if PRACTICE_MODE else "⚡"

    if not markets:
        return f"{tag} No live Over 3.5 markets in allowed competitions."

    states = get_market_states(markets)
    match_odds_lookup = get_match_odds_lookup()
    match_odds_ids = list({match_odds_lookup.get(market.event_id) for market in markets})
    match_odds_ids = [market_id for market_id in match_odds_ids if market_id]
    match_odds_states = get_match_odds_states(match_odds_ids)

    already_watching = {thread.name for thread in threading.enumerate()}
    placed = 0

    for market in markets:
        if f"monitor_{market.market_id}" in already_watching:
            continue

        if not _eligible_for_entry(market):
            continue

        state = states.get(market.market_id)
        if not state or state.suspended or state.best_lay is None:
            continue

        match_odds_market_id = match_odds_lookup.get(market.event_id)
        favourite_price = None
        if match_odds_market_id:
            match_odds_state = match_odds_states.get(match_odds_market_id)
            if match_odds_state:
                favourite_price = match_odds_state.favourite_back

        if not _passes_match_quality(market, state, favourite_price):
            continue

        price = state.best_lay
        if not (LAY_PRICE - LAY_TOLERANCE <= price <= LAY_PRICE + LAY_TOLERANCE):
            log.info(
                "Price %.2f out of window | %s [%s]",
                price,
                market.event_name,
                market.competition,
            )
            continue

        log.info(
            "ENTRY | %s | %s | lay=%.2f | matched=%.2f | favourite=%.2f",
            market.competition,
            market.event_name,
            price,
            state.matched_total,
            favourite_price or 0.0,
        )
        bet_id = place_lay(market, price, LAY_STAKE)
        if not bet_id:
            _tg(f"⚠️ Order failed for {market.event_name}")
            continue

        position = Position(
            market=market,
            entry_price=price,
            lay_stake=LAY_STAKE,
            bet_id=bet_id,
        )

        placed += 1
        threading.Thread(
            target=_monitor,
            args=(position,),
            daemon=True,
            name=f"monitor_{market.market_id}",
        ).start()

    if placed == 0:
        competitions = sorted({market.competition for market in markets})
        return (
            f"{tag} Scanned {len(markets)} markets "
            f"({len(competitions)} competitions) – none at {LAY_PRICE:.2f} ± "
            f"{LAY_TOLERANCE:.2f}"
        )
    return f"{tag} ✅ {placed} lay order(s) placed at ~{LAY_PRICE:.2f}"


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════
def run_trader_loop() -> None:
    """Persistent loop. Session stays alive via BetfairClient keepAlive."""
    tag = "🧪 PRACTICE" if PRACTICE_MODE else "⚡ LIVE"
    log.info("Trader loop started [%s] – scan every %ds", tag, SCAN_INTERVAL_SECONDS)
    _tg(
        f"{'🧪' if PRACTICE_MODE else '⚡'} *Over 3.5 Trader Started*\n"
        f"Mode: {tag}\n"
        f"Entry: {LAY_PRICE:.2f} ± {LAY_TOLERANCE:.2f} | Lay stake: £{LAY_STAKE:.2f}\n"
        f"Ladder exits: {LADDER_1_PRICE:.2f} / {LADDER_2_PRICE:.2f} / "
        f"{TARGET_EXIT_PRICE:.2f}\n"
        f"Deadline: {MAX_HOLD_MINS} min | Early adverse cut: -£{EARLY_ADVERSE_LOSS:.2f}\n"
        f"Min matched: £{MIN_MATCHED_TOTAL:.0f} | Scan every {SCAN_INTERVAL_SECONDS}s"
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
            _tg("🛑 *Over 3.5 Trader stopped*")
            if PRACTICE_MODE:
                _tg(_LEDGER.summary())
            break
        except Exception as exc:  # pragma: no cover
            log.error("Scan error: %s", exc, exc_info=True)
            _tg(f"⚠️ Scan error: {exc}\nRetrying in {SCAN_INTERVAL_SECONDS}s")
            time.sleep(SCAN_INTERVAL_SECONDS)


def get_session_summary() -> str:
    """Return the practice ledger summary."""
    if not PRACTICE_MODE:
        return "⚠️ Summary only available in PRACTICE_MODE."
    return _LEDGER.summary()


if __name__ == "__main__":
    try:
        run_trader_loop()
    except KeyboardInterrupt:
        pass
    if PRACTICE_MODE:
        print("\n" + get_session_summary())
