#!/usr/bin/env python3
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BETFAIR_COMMISSION  = 0.02
MIN_QUALIFYING_RET  = 1.0
MIN_FREE_BET_RET    = 0.70
LAY_THRESHOLD       = 5

DEFAULT_CATALOGUE_REFRESH_SECONDS = 20 * 60
DEFAULT_MARKETBOOK_BATCH_SIZE     = 40

BOOKIE_URLS: Dict[str, str] = {
    "bet365":       "https://www.bet365.com/",
    "williamhill":  "https://sports.williamhill.com/",
    "paddypower":   "https://www.paddypower.com/",
    "skybet":       "https://m.skybet.com/",
    "ladbrokes":    "https://sports.ladbrokes.com/",
    "coral":        "https://sports.coral.co.uk/",
    "betfair":      "https://www.betfair.com/",
    "unibet":       "https://www.unibet.co.uk/",
    "virginbet":    "https://www.virginbet.com/",
    "betvictor":    "https://www.betvictor.com/",
    "spreadex":     "https://www.spreadex.com/sports/",
    "betway":       "https://betway.com/",
    "talksportbet": "https://talksportbet.com/",
    "10bet":        "https://www.10bet.co.uk/",
}

# Separators Betfair uses in event names: "Team A v Team B" or "Team A vs Team B"
_BETFAIR_EVENT_SEP = re.compile(r"\s+v(?:s)?\s+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", name).lower()


def calculate_retention(back_odds: float, lay_odds: float, is_free_bet: bool = False) -> float:
    effective_lay = lay_odds - ((lay_odds - 1) * BETFAIR_COMMISSION)
    if is_free_bet:
        return (back_odds - 1) / effective_lay
    return back_odds / effective_lay


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _format_kickoff_local(value: Optional[str]) -> Optional[str]:
    dt_utc = _parse_iso_datetime(value)
    if not dt_utc:
        return None
    return dt_utc.astimezone().strftime("%a %d %b %H:%M")


def _bookie_link(bookie_name: str) -> Optional[str]:
    return BOOKIE_URLS.get(normalize(bookie_name))


def _parse_betfair_event_name(event_name: str) -> Optional[Tuple[str, str]]:
    """
    Parses a Betfair event name like "Athletic Bilbao v Barcelona" into
    a (norm_home, norm_away) tuple for matching against the bookie cache.

    Returns None if the event name cannot be split into two teams.
    """
    parts = _BETFAIR_EVENT_SEP.split(event_name, maxsplit=1)
    if len(parts) != 2:
        return None
    return (normalize(parts[0].strip()), normalize(parts[1].strip()))


def _lookup_bookie_offer(
    cached_bookie: Dict,
    event_name:    str,
    runner_name:   str,
) -> Optional[Any]:
    """
    Two-level lookup against the event-keyed bookie cache:
      1. Parse the Betfair event name into (norm_home, norm_away)
      2. Find that event in the cache
      3. Find the runner within that event

    Returns None if either the event or runner is not found.
    This prevents cross-event false positives (e.g. "Barcelona" in La Liga
    matching a Champions League price for a different fixture).
    """
    event_key = _parse_betfair_event_name(event_name)
    if not event_key:
        return None

    norm_home, norm_away = event_key

    # Try exact match first
    event_offers = cached_bookie.get((norm_home, norm_away))

    # Try reversed (Betfair and Odds API may disagree on home/away)
    if event_offers is None:
        event_offers = cached_bookie.get((norm_away, norm_home))

    if not event_offers:
        return None

    return event_offers.get(normalize(runner_name))


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _BookieOffer:
    """
    Internal offer representation. Field names match BookieOffer in
    bookie_client.py so getattr() works across both without conversion.
    """
    back_odds:     float
    bookie_name:   str
    home_team:     Optional[str] = None
    away_team:     Optional[str] = None
    commence_time: Optional[str] = None


@dataclass
class _MarketCacheEntry:
    market_id:        str
    competition_name: str
    event_name:       str
    market_name:      str
    runners:          Dict[int, str]
    sport:            str               # "football" or "horse_racing"
    start_time:       Optional[str] = None


@dataclass
class _WatcherState:
    running:                bool = False
    thread:                 Optional[threading.Thread] = None
    stop_event:             Optional[threading.Event]  = None
    alerted:                Dict[Tuple[str, int], float] = field(default_factory=dict)
    cached_bookie:          Dict                         = field(default_factory=dict)
    last_bookie_refresh:    float = 0.0
    market_cache:           Dict[str, _MarketCacheEntry] = field(default_factory=dict)
    last_catalogue_refresh: float = 0.0
    bf_client:              Optional[Any] = None


_WATCHER:     _WatcherState   = _WatcherState()
_PENDING_LAY: Dict[str, Dict] = {}


# ---------------------------------------------------------------------------
# Alerting & Bet Placement
# ---------------------------------------------------------------------------

def _send_alert(
    telegram:    Any,
    chat_id:     str,
    entry:       _MarketCacheEntry,
    runner_name: str,
    best_lay:    float,
    offer:       Any,
) -> None:
    if best_lay > LAY_THRESHOLD:
        return

    q_ret  = calculate_retention(offer.back_odds, best_lay, is_free_bet=False)
    fb_ret = calculate_retention(offer.back_odds, best_lay, is_free_bet=True)

    if q_ret < MIN_QUALIFYING_RET and fb_ret < MIN_FREE_BET_RET:
        return

    kickoff = _format_kickoff_local(entry.start_time) if entry.start_time else None
    link    = _bookie_link(offer.bookie_name)

    if entry.sport == "horse_racing":
        lines = [
            "🏇 **RACING MATCH FOUND**",
            f"📍 {entry.competition_name}",
            f"🏁 {entry.market_name}",
        ]
        if kickoff:
            lines.append(f"🕒 Off: **{kickoff}**")
        lines.append(f"🐴 Runner: **{runner_name}**")
    else:
        lines = [
            "🎯 **MATCH FOUND**",
            f"⚽ {entry.competition_name}",
            f"🏟️ {entry.event_name}",
        ]
        if kickoff:
            lines.append(f"🕒 Kickoff: **{kickoff}**")
        lines.append(f"🏇 Selection: **{runner_name}**")

    lines.extend([
        "---",
        f"🏦 Bookie: **{offer.bookie_name}**",
    ])
    if link:
        lines.append(f"🔗 {link}")
    lines.extend([
        f"📈 Back: {offer.back_odds:.2f} | Lay: {best_lay:.2f}",
        f"🔄 Qualy Ret: {q_ret * 100:.1f}%",
        f"🎁 Free Bet Ret: {fb_ret * 100:.1f}%",
        "---",
        f"🆔 Market: `{entry.market_id}`",
        "",
        "Reply with a stake amount to lay this, e.g. `10`",
    ])

    _PENDING_LAY[str(chat_id)] = {
        "market_id":      entry.market_id,
        "lay_odds":       best_lay,
        "runner_name":    runner_name,
        "event_name":     entry.event_name,
        "awaiting_stake": True,
    }
    telegram.send_message(chat_id, "\n".join(lines))


def _place_lay_bet(
    bf_client:    Any,
    market_id:    str,
    selection_id: int,
    lay_odds:     float,
    stake:        float,
) -> Any:
    from betfairlightweight.filters import place_instruction, limit_order
    instruction = place_instruction(
        selection_id=selection_id,
        side="LAY",
        order_type="LIMIT",
        limit_order=limit_order(
            size=round(stake, 2),
            price=lay_odds,
            persistence_type="LAPSE",
        ),
    )
    result = bf_client.betting.place_orders(market_id=market_id, instructions=[instruction])
    if result.status != "SUCCESS":
        raise RuntimeError(f"placeOrders status: {result.status} -- {result.error_code}")
    return result.place_instruction_reports[0]


# ---------------------------------------------------------------------------
# Caching Logic
# ---------------------------------------------------------------------------

def _refresh_bookie_cache_if_needed(*, config: Any, bookie: Any, now: float) -> None:
    """
    TTL-guarded bookie odds refresh.

    Stores the event-keyed cache from bookie_client.get_best_back_offers()
    directly into _WATCHER.cached_bookie as:
        { (norm_home, norm_away): { norm_runner: BookieOffer } }

    Matching in the watcher loop uses _lookup_bookie_offer() which does
    a two-level lookup — event first, then runner — to prevent cross-event
    name collisions.
    """
    refresh_interval = float(getattr(config, "BOOKIE_REFRESH_SECONDS", 300))

    if _WATCHER.cached_bookie and (now - _WATCHER.last_bookie_refresh < refresh_interval):
        return

    log.info("Refreshing Bookie odds (Token Cost: 1-3 Credits)...")

    try:
        fresh = bookie.get_best_back_offers()
    except Exception:
        log.exception("Bookie refresh failed — keeping stale cache.")
        return

    if not fresh:
        log.warning("Bookie refresh returned no data.")
        return

    _WATCHER.cached_bookie      = fresh
    _WATCHER.last_bookie_refresh = now

    total_runners = sum(len(v) for v in fresh.values())
    log.info("Bookie cache updated: %d events / %d runners.", len(fresh), total_runners)


def _refresh_catalogue_if_needed(*, config: Any, bf: Any, now: float) -> None:
    refresh_every = float(getattr(config, "CATALOGUE_REFRESH_SECONDS", DEFAULT_CATALOGUE_REFRESH_SECONDS))
    if now - _WATCHER.last_catalogue_refresh < refresh_every and _WATCHER.market_cache:
        return

    new_cache: Dict[str, _MarketCacheEntry] = {}

    # --- Football ---
    football_ids = getattr(config, "FOOTBALL_COMPETITION_IDS", [])
    if football_ids:
        football_markets = bf.list_football_match_odds_markets(
            competition_ids=football_ids,
            lookahead_hours=getattr(config, "WATCH_LOOKAHEAD_HOURS", 48),
        )
        for m in football_markets:
            runners_map = {
                r["selectionId"]: r["runnerName"]
                for r in m.runners
                if "selectionId" in r and "runnerName" in r
            }
            if runners_map:
                new_cache[m.market_id] = _MarketCacheEntry(
                    market_id        = m.market_id,
                    competition_name = m.competition_name,
                    event_name       = m.event_name,
                    market_name      = m.market_name,
                    runners          = runners_map,
                    sport            = "football",
                    start_time       = m.start_time,
                )
        log.info("Watcher: Refreshed football catalogue (%d markets).", len(football_markets))

    # --- Horse Racing ---
    cheltenham_mode = getattr(config, "CHELTENHAM_MODE", False)
    watch_racing    = getattr(config, "WATCH_HORSE_RACING", False)

    if cheltenham_mode:
        racing_markets = bf.list_cheltenham_markets(
            lookahead_hours=120,
            market_types=getattr(config, "RACING_MARKET_TYPES", ["WIN"]),
        )
        log.info("Watcher: Refreshed Cheltenham catalogue (%d markets).", len(racing_markets))
    elif watch_racing:
        racing_markets = bf.list_horse_racing_markets(
            lookahead_hours=getattr(config, "RACING_LOOKAHEAD_HOURS", 24),
            market_types=getattr(config, "RACING_MARKET_TYPES", ["WIN"]),
            venues=getattr(config, "RACING_VENUES", None),
        )
        log.info("Watcher: Refreshed horse racing catalogue (%d markets).", len(racing_markets))
    else:
        racing_markets = []

    for m in racing_markets:
        runners_map = {
            r["selectionId"]: r["runnerName"]
            for r in m.runners
            if "selectionId" in r and "runnerName" in r
        }
        if runners_map:
            new_cache[m.market_id] = _MarketCacheEntry(
                market_id        = m.market_id,
                competition_name = m.competition_name,
                event_name       = m.event_name,
                market_name      = m.market_name,
                runners          = runners_map,
                sport            = "horse_racing",
                start_time       = m.start_time,
            )

    if not new_cache:
        log.warning(
            "Watcher: No markets in catalogue cache (football_ids=%s racing=%s).",
            football_ids, len(racing_markets),
        )
    else:
        log.info("Watcher: Total catalogue cache: %d markets.", len(new_cache))

    _WATCHER.market_cache           = new_cache
    _WATCHER.last_catalogue_refresh = now


# ---------------------------------------------------------------------------
# Main Watcher Loop
# ---------------------------------------------------------------------------

def _watch_loop(
    config:     Any,
    telegram:   Any,
    chat_id:    str,
    stop_event: threading.Event,
) -> None:
    from .betfair_client import BetfairClient
    from ..bookie.bookie_client import BookieClient

    poll_seconds = float(getattr(config, "WATCH_POLL_SECONDS", 120))
    batch_size   = int(getattr(config, "MARKETBOOK_BATCH_SIZE", DEFAULT_MARKETBOOK_BATCH_SIZE))

    bookie = BookieClient(
        api_key              = getattr(config, "ODDS_API_KEY", ""),
        include_football     = True,
        include_horse_racing = False,   # Not available on Odds API
    )
    bf = BetfairClient(
        username = config.BETFAIR_USERNAME,
        password = config.BETFAIR_PASSWORD,
        app_key  = config.BETFAIR_APP_KEY,
        certs    = config.BETFAIR_CERTS,
    )

    bf.login()
    _WATCHER.bf_client = bf

    try:
        while not stop_event.is_set():
            start = time.time()
            try:
                now = time.time()

                # 1. Clean stale alerts (12h TTL)
                _WATCHER.alerted = {
                    k: v for k, v in _WATCHER.alerted.items()
                    if now - v < 43200
                }

                # 2. Refresh caches
                _refresh_bookie_cache_if_needed(config=config, bookie=bookie, now=now)
                _refresh_catalogue_if_needed(config=config, bf=bf, now=now)

                if not _WATCHER.market_cache:
                    log.warning("Watcher: No markets to poll, sleeping.")
                    time.sleep(poll_seconds)
                    continue

                # 3. Poll Betfair market books in batches
                market_ids = list(_WATCHER.market_cache.keys())
                batches    = [market_ids[i:i + batch_size] for i in range(0, len(market_ids), batch_size)]

                for batch in batches:
                    if stop_event.is_set():
                        break

                    books = bf.list_market_books(market_ids=batch)

                    for mid, runners in books.items():
                        entry = _WATCHER.market_cache.get(mid)
                        if not entry:
                            continue

                        for sid, best_lay in runners.items():
                            if best_lay is None:
                                continue
                            if (mid, sid) in _WATCHER.alerted:
                                continue

                            r_name = entry.runners.get(sid)
                            if not r_name:
                                continue

                            # Two-level lookup: event first, then runner
                            # Prevents cross-event name collisions
                            offer = _lookup_bookie_offer(
                                cached_bookie = _WATCHER.cached_bookie,
                                event_name    = entry.event_name,
                                runner_name   = r_name,
                            )
                            if not offer:
                                continue

                            _send_alert(
                                telegram    = telegram,
                                chat_id     = chat_id,
                                entry       = entry,
                                runner_name = r_name,
                                best_lay    = float(best_lay),
                                offer       = offer,
                            )
                            _WATCHER.alerted[(mid, sid)] = time.time()

            except Exception:
                log.exception("Watcher loop cycle failed")

            time.sleep(max(0.1, poll_seconds - (time.time() - start)))

    finally:
        try:
            bf.logout()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Interaction Handlers
# ---------------------------------------------------------------------------

def handle_lay_callback(telegram: Any, upd: Any) -> None:
    chat_id = str(upd.chat_id)
    telegram.answer_callback_query(upd.callback_query_id)

    if upd.callback_data == "lay_no":
        _PENDING_LAY.pop(chat_id, None)
        telegram.edit_message_text(chat_id, upd.message_id, "❌ Bet cancelled.")
        return

    parts = upd.callback_data.split("|")
    if len(parts) == 5:
        _, mid, sid, odds, stake = parts
        telegram.edit_message_text(chat_id, upd.message_id, "⏳ Placing lay bet...")
        try:
            _place_lay_bet(_WATCHER.bf_client, mid, int(sid), float(odds), float(stake))
            telegram.edit_message_text(chat_id, upd.message_id, "✅ *Lay placed!*")
        except Exception as e:
            telegram.edit_message_text(chat_id, upd.message_id, f"❌ Failed: {e}")
        _PENDING_LAY.pop(chat_id, None)


def handle_stake_reply(telegram: Any, chat_id: str, text: str) -> bool:
    pending = _PENDING_LAY.get(chat_id)
    if not pending or not pending.get("awaiting_stake"):
        return False

    try:
        stake = float(text.strip())
        if stake <= 0:
            raise ValueError
    except ValueError:
        telegram.send_message(chat_id, "❌ Enter a valid stake.")
        return True

    mid    = pending["market_id"]
    odds   = pending["lay_odds"]
    r_name = pending["runner_name"]

    market_entry = _WATCHER.market_cache.get(mid)
    if not market_entry:
        telegram.send_message(chat_id, "❌ Market no longer in cache.")
        return True

    sid = next(
        (s for s, n in market_entry.runners.items() if normalize(n) == normalize(r_name)),
        None,
    )
    if not sid:
        telegram.send_message(chat_id, "❌ Selection not found in cache.")
        return True

    pending.update({"awaiting_stake": False, "stake": stake, "selection_id": sid})

    confirm_text = (
        f"⚠️ *Confirm Lay*\n"
        f"{pending['event_name']}\n"
        f"Odds: {odds} | Stake: £{stake:.2f}\n"
        f"Liability: £{stake * (odds - 1):.2f}"
    )
    markup = telegram.inline_keyboard([[
        {"text": "✅ Place Bet", "callback_data": f"lay_yes|{mid}|{sid}|{odds}|{stake}"},
        {"text": "❌ Cancel",   "callback_data": "lay_no"},
    ]])
    telegram.send_message(chat_id, confirm_text, reply_markup=markup)
    return True


def handle(config: Any, telegram: Any, chat_id: str, text: str) -> None:
    args = text.split()
    if len(args) < 2:
        telegram.send_message(chat_id, "Usage: betfair <watch|status|stop>")
        return

    cmd = args[1].lower()

    if cmd == "watch":
        if _WATCHER.running:
            telegram.send_message(chat_id, "Already running.")
            return
        _WATCHER.stop_event = threading.Event()
        _WATCHER.thread = threading.Thread(
            target=_watch_loop,
            args=(config, telegram, chat_id, _WATCHER.stop_event),
            daemon=True,
        )
        _WATCHER.running = True
        _WATCHER.thread.start()

        mode_parts = [f"Threshold <= {LAY_THRESHOLD}"]
        if getattr(config, "CHELTENHAM_MODE", False):
            mode_parts.append("🏇 Cheltenham mode ON")
        elif getattr(config, "WATCH_HORSE_RACING", False):
            mode_parts.append("🏇 Horse racing ON")

        telegram.send_message(chat_id, f"🟢 Watcher started ({' | '.join(mode_parts)})")

    elif cmd == "stop":
        if _WATCHER.stop_event:
            _WATCHER.stop_event.set()
        _WATCHER.running = False
        telegram.send_message(chat_id, "🔴 Stopped.")

    elif cmd == "status":
        status       = "🟢 Running" if _WATCHER.running else "🔴 Stopped"
        football_cnt = sum(1 for e in _WATCHER.market_cache.values() if e.sport == "football")
        racing_cnt   = sum(1 for e in _WATCHER.market_cache.values() if e.sport == "horse_racing")
        cheltenham   = "✅" if getattr(config, "CHELTENHAM_MODE", False) else "❌"

        telegram.send_message(
            chat_id,
            f"Status: {status}\n"
            f"Alerts: {len(_WATCHER.alerted)}\n"
            f"Cached Odds: {len(_WATCHER.cached_bookie)}\n"
            f"Football Markets: {football_cnt}\n"
            f"Racing Markets: {racing_cnt}\n"
            f"Cheltenham Mode: {cheltenham}",
        )

    else:
        telegram.send_message(chat_id, "Usage: betfair <watch|status|stop>")
