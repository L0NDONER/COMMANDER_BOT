#!/usr/bin/env python3
"""
Bookie client wrapping The Odds API.

Fetches the best available back odds across all configured bookmakers
for football, returning a dict keyed by (norm_home, norm_away) event tuple,
with each value being a dict of { norm_runner_name: BookieOffer }.

This two-level structure prevents cross-event name collisions — e.g.
"Barcelona" appearing in both La Liga and Champions League will only
match the correct Betfair event.

Design decisions:
- Sports are configured via a list — easy to extend
- Best back odds are selected per outcome across all bookmakers per event
- get_best_back_offers() returns Dict[Tuple[str,str], Dict[str, BookieOffer]]
- get_best_back_offers_flat() returns the old flat Dict[str, BookieOffer]
  as a fallback — kept for any legacy call sites
- Raises on auth failure; returns empty dict on parse errors so the
  watcher loop degrades gracefully rather than crashing
- Betfair is explicitly excluded — backing and laying same exchange is
  not matched betting

Horse racing note:
  The Odds API does not carry horse racing markets. Cheltenham bookie prices
  must be checked manually. The bot handles the Betfair lay side automatically.

Verified Odds API sport keys (as of 2026):
  soccer_epl                - Premier League       ✅
  soccer_efl_champ          - Championship         ✅
  soccer_fa_cup             - FA Cup               ✅
  soccer_league_cup         - EFL Cup              ❌ does not exist on Odds API
  soccer_uefa_champs_league - Champions League     ✅
  soccer_uefa_europa_league - Europa League        ✅
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Odds API config
# ---------------------------------------------------------------------------

ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"

# Verified sport keys — check https://the-odds-api.com/sports-odds-data/sports-apis.html
# before adding new ones. Invalid keys return 404 silently in logs.
FOOTBALL_SPORTS = [
    "soccer_epl",                   # Premier League
    "soccer_efl_champ",             # Championship
    "soccer_fa_cup",                # FA Cup
    "soccer_uefa_champs_league",    # Champions League
    "soccer_uefa_europa_league",    # Europa League
]

# Horse racing is NOT available on The Odds API.
HORSE_RACING_SPORTS: list[str] = []

UK_REGIONS = "uk"

# Betfair intentionally excluded — not a valid back bookie for matched betting
UK_BOOKMAKERS = ",".join([
    "bet365",
    "williamhill",
    "paddypower",
    "skybet",
    "ladbrokes",
    "coral",
    "unibet",
    "betvictor",
    "betway",
    "virginbet",
    "talksportbet",
    "spreadex",
    "10bet",
])

REQUEST_TIMEOUT = 20

# Type alias for the event-keyed cache
EventKey   = Tuple[str, str]                           # (norm_home, norm_away)
EventCache = Dict[EventKey, Dict[str, "BookieOffer"]]  # { event_key: { norm_runner: offer } }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", name).lower()


# ---------------------------------------------------------------------------
# Offer dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BookieOffer:
    """
    Best available back price for a single runner/team in a specific event.
    Field names mirror betfair_service._BookieOffer for getattr() compatibility.
    """
    back_odds:     float
    bookie_name:   str
    home_team:     Optional[str] = None
    away_team:     Optional[str] = None
    commence_time: Optional[str] = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class BookieClient:
    """
    Fetches best back odds from The Odds API across configured football sports.

    Primary method: get_best_back_offers() → EventCache
    Keyed by (norm_home, norm_away) to prevent cross-event name collisions.
    """

    def __init__(
        self,
        api_key:              str,
        include_horse_racing: bool = False,   # No-op — horse racing not on Odds API
        include_football:     bool = True,
        extra_sports:         Optional[list[str]] = None,
    ) -> None:
        if not api_key:
            raise ValueError("Odds API key is required.")
        self._api_key = api_key
        self._session = requests.Session()

        self._sports: list[str] = []
        if include_football:
            self._sports.extend(FOOTBALL_SPORTS)
        if extra_sports:
            self._sports.extend(extra_sports)

    # ------------------------------------------------------------------ #
    # Public                                                               #
    # ------------------------------------------------------------------ #

    def get_best_back_offers(self) -> EventCache:
        """
        Returns best back prices keyed by event:
            { (norm_home, norm_away): { norm_runner: BookieOffer } }

        This prevents "Barcelona" in La Liga matching "Barcelona" in
        Champions League. betfair_service.py parses the Betfair event name
        to extract home/away, looks up the event key, then finds the runner.
        """
        raw: Dict[EventKey, Dict[str, tuple]] = {}

        for sport in self._sports:
            try:
                events = self._fetch_sport(sport)
                self._parse_events(events, raw)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 401:
                    log.error("Odds API: Invalid API key (401). Check ODDS_API_KEY.")
                    raise
                log.warning("Odds API: HTTP error for sport %s: %s", sport, exc)
            except requests.RequestException as exc:
                log.warning("Odds API: Network error for sport %s: %s", sport, exc)
            except Exception:
                log.exception("Odds API: Unexpected error for sport %s", sport)

        results: EventCache = {}
        for event_key, runners in raw.items():
            results[event_key] = {
                norm: BookieOffer(
                    back_odds     = odds,
                    bookie_name   = title,
                    home_team     = home,
                    away_team     = away,
                    commence_time = commence,
                )
                for norm, (odds, title, home, away, commence) in runners.items()
            }

        total_runners = sum(len(v) for v in results.values())
        log.info(
            "BookieClient: %d events / %d runners fetched across %d sport(s).",
            len(results), total_runners, len(self._sports),
        )
        return results

    def get_best_back_offers_flat(self) -> Dict[str, BookieOffer]:
        """
        Legacy flat interface: { norm_runner_name: BookieOffer }
        May produce cross-event collisions — prefer get_best_back_offers().
        """
        flat: Dict[str, BookieOffer] = {}
        for runners in self.get_best_back_offers().values():
            for norm, offer in runners.items():
                if norm not in flat or offer.back_odds > flat[norm].back_odds:
                    flat[norm] = offer
        return flat

    # Alias for legacy call sites
    get_best_back_odds = get_best_back_offers_flat

    def remaining_requests(self) -> Optional[int]:
        """Returns API quota remaining — useful for status monitoring."""
        try:
            resp = self._session.get(
                f"{ODDS_API_BASE}/soccer_epl/odds",
                params={
                    "apiKey":     self._api_key,
                    "regions":    UK_REGIONS,
                    "bookmakers": UK_BOOKMAKERS,
                    "markets":    "h2h",
                    "oddsFormat": "decimal",
                },
                timeout=REQUEST_TIMEOUT,
            )
            return int(resp.headers.get("x-requests-remaining", -1))
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _fetch_sport(self, sport: str) -> list:
        resp = self._session.get(
            f"{ODDS_API_BASE}/{sport}/odds",
            params={
                "apiKey":     self._api_key,
                "regions":    UK_REGIONS,
                "bookmakers": UK_BOOKMAKERS,
                "markets":    "h2h",
                "oddsFormat": "decimal",
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining", "?")
        used      = resp.headers.get("x-requests-used", "?")
        log.debug("Odds API [%s]: quota used=%s remaining=%s", sport, used, remaining)
        return resp.json()

    def _parse_events(
        self,
        events: list,
        raw:    Dict[EventKey, Dict[str, tuple]],
    ) -> None:
        """
        Builds a two-level dict:
            raw[(norm_home, norm_away)][norm_runner] = (best_odds, title, home, away, commence)

        Each event gets its own namespace so runner names only collide
        within the same fixture, not across different games.
        """
        if not isinstance(events, list):
            log.warning("Odds API: Expected list of events, got %r", type(events))
            return

        for event in events:
            home_team     = event.get("home_team")
            away_team     = event.get("away_team")
            commence_time = event.get("commence_time")
            bookmakers    = event.get("bookmakers", [])

            if not home_team or not away_team or not bookmakers:
                continue

            event_key: EventKey = (_normalize(home_team), _normalize(away_team))

            if event_key not in raw:
                raw[event_key] = {}

            for bookmaker in bookmakers:
                title   = bookmaker.get("title", bookmaker.get("key", "Unknown"))
                markets = bookmaker.get("markets", [])

                if not markets:
                    continue

                for outcome in markets[0].get("outcomes", []):
                    name:  str   = outcome.get("name", "")
                    price: float = outcome.get("price", 0.0)

                    if not name or not price:
                        continue

                    norm     = _normalize(name)
                    existing = raw[event_key].get(norm)

                    if existing is None or price > existing[0]:
                        raw[event_key][norm] = (price, title, home_team, away_team, commence_time)
