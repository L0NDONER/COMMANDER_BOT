#!/usr/bin/env python3
"""
Betfair API client for listing football and horse racing markets with best lay prices.

Design decisions:
- Session is kept open by default (no auto-logout on context manager exit)
- Optional keepAlive support to extend session before expiry
- Automatic re-login if session expires/invalidates
- Defensive parsing throughout
- Uses Betfair's non-interactive (bot) login endpoint with cert auth

Notes:
- Betfair session lifetimes vary by jurisdiction; keepAlive should be called
  within the session expiry window to prevent expiry.
- Horse racing markets are structured differently to football:
  no competition IDs, filtered by event type + market type instead.
- ⚠️  All event type IDs and competition IDs are BETFAIR-specific.
      Do NOT mix with The Odds API IDs — they are completely different namespaces.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Betfair API endpoints
# ---------------------------------------------------------------------------

LOGIN_URL    = "https://identitysso-cert.betfair.com/api/certlogin"
LOGOUT_URL   = "https://identitysso.betfair.com/api/logout"
KEEPALIVE_URL = "https://identitysso.betfair.com/api/keepAlive"
API_URL      = "https://api.betfair.com/exchange/betting/json-rpc/v1"

REQUEST_TIMEOUT = 15

# ---------------------------------------------------------------------------
# Betfair event type IDs (SportsAPING)
# ⚠️  These are BETFAIR IDs — not The Odds API IDs
# ---------------------------------------------------------------------------

FOOTBALL_EVENT_TYPE_ID    = "1"
HORSE_RACING_EVENT_TYPE_ID = "7"

# ---------------------------------------------------------------------------
# KeepAlive strategy
# ---------------------------------------------------------------------------

# Refresh every 30 minutes (safe for short-lived jurisdictions)
DEFAULT_KEEPALIVE_EVERY_SECONDS = 30 * 60

# Retry once on auth failures before raising
RPC_RETRY_ON_AUTH_FAILURES = 1

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MarketInfo:
    market_id: str
    competition_name: str
    event_name: str
    market_name: str
    runners: List[dict]   # raw runner catalogue entries
    start_time: Optional[str] = None


@dataclass
class RunnerLay:
    runner_name: str
    selection_id: int
    best_lay: Optional[float]   # None if no lay available


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class BetfairClient:
    def __init__(
        self,
        username: str,
        password: str,
        app_key: str,
        certs: str,
        keepalive_every_seconds: int = DEFAULT_KEEPALIVE_EVERY_SECONDS,
        keep_session_open: bool = True,
    ) -> None:
        self._username   = username
        self._password   = password
        self._app_key    = app_key
        self._certs      = certs   # path to directory containing Betfair.crt / Betfair.key

        self._keepalive_every_seconds = int(keepalive_every_seconds)
        self._keep_session_open       = bool(keep_session_open)

        self._session_token: Optional[str]  = None
        self._last_auth_refresh_monotonic: Optional[float] = None

        self._session = requests.Session()

    # ------------------------------------------------------------------ #
    # Context manager                                                      #
    # ------------------------------------------------------------------ #

    def __enter__(self) -> "BetfairClient":
        self.login()
        return self

    def __exit__(self, *_) -> None:
        """
        Does NOT logout by default — keeps the token alive for reuse.
        Call logout() explicitly if you want to terminate the session.
        """
        if not self._keep_session_open:
            self.logout()
        try:
            self._session.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Auth (public)                                                        #
    # ------------------------------------------------------------------ #

    def login(self) -> None:
        self._login()

    def logout(self) -> None:
        self._logout()

    def keep_alive(self) -> bool:
        return self._keep_alive()

    # ------------------------------------------------------------------ #
    # Auth (internal)                                                      #
    # ------------------------------------------------------------------ #

    def _login(self) -> None:
        cert = (
            f"{self._certs}/Betfair.crt",
            f"{self._certs}/Betfair.key",
        )
        resp = self._session.post(
            LOGIN_URL,
            data={"username": self._username, "password": self._password},
            headers={"X-Application": self._app_key, "Accept": "application/json"},
            cert=cert,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("loginStatus") != "SUCCESS":
            raise RuntimeError(f"Betfair login failed: {data.get('loginStatus')}")

        self._session_token = data["sessionToken"]
        self._last_auth_refresh_monotonic = time.monotonic()
        log.info("Betfair login successful.")

    def _logout(self) -> None:
        if not self._session_token:
            return
        try:
            self._session.post(
                LOGOUT_URL,
                headers=self._auth_headers(),
                timeout=REQUEST_TIMEOUT,
            )
        except Exception:
            pass
        self._session_token = None
        self._last_auth_refresh_monotonic = None
        log.info("Betfair logout complete.")

    def _keep_alive(self) -> bool:
        if not self._session_token:
            return False
        try:
            resp = self._session.post(
                KEEPALIVE_URL,
                headers={
                    "X-Application":   self._app_key,
                    "X-Authentication": self._session_token,
                    "Accept":          "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("keepAlive request failed: %s", exc)
            return False

        status = data.get("status")
        if status == "SUCCESS":
            self._last_auth_refresh_monotonic = time.monotonic()
            log.debug("Betfair keepAlive SUCCESS.")
            return True

        log.info(
            "Betfair keepAlive not successful: status=%r error=%r",
            status,
            data.get("error"),
        )
        return False

    def _auth_headers(self) -> dict:
        if not self._session_token:
            raise RuntimeError("No session token set. You must login first.")
        return {
            "X-Application":   self._app_key,
            "X-Authentication": self._session_token,
            "Content-Type":    "application/json",
            "Accept":          "application/json",
        }

    def _should_refresh_session(self) -> bool:
        if not self._session_token or self._last_auth_refresh_monotonic is None:
            return True
        age = time.monotonic() - self._last_auth_refresh_monotonic
        return age >= self._keepalive_every_seconds

    def _ensure_session(self) -> None:
        """
        Ensures we have a usable session:
        - No token → login
        - Token is old → keepAlive; if that fails → login
        """
        if not self._session_token:
            self._login()
            return
        if not self._should_refresh_session():
            return
        if self._keep_alive():
            return
        log.info("Refreshing Betfair session via login() after keepAlive failure.")
        self._login()

    # ------------------------------------------------------------------ #
    # JSON-RPC helper                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _looks_like_auth_error(result_obj: dict) -> bool:
        if not isinstance(result_obj, dict):
            return False
        err = result_obj.get("error")
        if not isinstance(err, dict):
            return False
        msg  = str(err.get("message", "")).upper()
        data = err.get("data")
        needles = ("INVALID_SESSION", "NO_SESSION", "AUTH", "LOGIN", "UNAUTHORIZED", "FORBIDDEN")
        if any(n in msg for n in needles):
            return True
        if isinstance(data, str) and any(n in data.upper() for n in needles):
            return True
        return False

    def _rpc(self, method: str, params: dict) -> dict:
        """
        Makes a JSON-RPC call. Auto-refreshes session and retries once on
        auth/session failures.
        """
        attempts = 0
        while True:
            self._ensure_session()

            payload = [{
                "jsonrpc": "2.0",
                "method":  f"SportsAPING/v1.0/{method}",
                "params":  params,
                "id":      1,
            }]

            resp = self._session.post(
                API_URL,
                json=payload,
                headers=self._auth_headers(),
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()
            if isinstance(result, list):
                result = result[0]

            if "error" in result:
                if self._looks_like_auth_error(result) and attempts < RPC_RETRY_ON_AUTH_FAILURES:
                    attempts += 1
                    log.info("RPC auth-like error detected; re-logging in and retrying once.")
                    self._login()
                    continue
                raise RuntimeError(f"Betfair RPC error: {result['error']}")

            return result.get("result", {})

    # ------------------------------------------------------------------ #
    # Diagnostic helpers                                                   #
    # ------------------------------------------------------------------ #

    def list_competitions(self, event_type_id: str = FOOTBALL_EVENT_TYPE_ID) -> list:
        """
        Returns all competitions for the given event type.
        Useful for discovering correct BETFAIR competition IDs.
        ⚠️  These IDs are Betfair-specific — do not confuse with Odds API IDs.
        """
        params = {"filter": {"eventTypeIds": [event_type_id]}}
        result = self._rpc("listCompetitions", params)
        if not isinstance(result, list):
            return []
        competitions = []
        for c in result:
            comp = c.get("competition", {})
            competitions.append({
                "id":     comp.get("id"),
                "name":   comp.get("name"),
                "region": c.get("competitionRegion"),
                "count":  c.get("marketCount"),
            })
        log.info("Found %d competitions for event type %s.", len(competitions), event_type_id)
        return competitions

    def list_venues(self) -> list:
        """
        Returns all horse racing venues currently available on Betfair.
        Useful for filtering Cheltenham, Ascot, etc.
        """
        params = {"filter": {"eventTypeIds": [HORSE_RACING_EVENT_TYPE_ID]}}
        result = self._rpc("listEvents", params)
        if not isinstance(result, list):
            return []
        venues = []
        for e in result:
            event = e.get("event", {})
            venues.append({
                "id":        event.get("id"),
                "name":      event.get("name"),
                "venue":     event.get("venue"),
                "open_date": event.get("openDate"),
                "count":     e.get("marketCount"),
            })
        return venues

    # ------------------------------------------------------------------ #
    # Football markets                                                     #
    # ------------------------------------------------------------------ #

    def list_football_match_odds_markets(
        self,
        competition_ids: List[str],
        lookahead_hours: int = 48,
    ) -> List[MarketInfo]:
        """
        Returns MarketInfo for all MATCH_ODDS markets in the given
        competitions within the lookahead window.

        ⚠️  competition_ids must be BETFAIR competition IDs, e.g.:
              "39"   = English Premier League
              "30"   = FA Cup
              "228"  = UEFA Champions League
              "2005" = UEFA Europa League
            Do NOT use Odds API IDs here.
        """
        now   = datetime.now(timezone.utc)
        until = now + timedelta(hours=lookahead_hours)

        log.info(
            "Catalogue refresh: competition_ids=%s lookahead_hours=%s",
            competition_ids,
            lookahead_hours,
        )

        params = {
            "filter": {
                "eventTypeIds":   [FOOTBALL_EVENT_TYPE_ID],
                "competitionIds": competition_ids,
                "marketTypeCodes": ["MATCH_ODDS"],
                "marketStartTime": {
                    "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to":   until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                "inPlayOnly": False,
            },
            "marketProjection": ["COMPETITION", "EVENT", "RUNNER_DESCRIPTION", "MARKET_START_TIME"],
            "maxResults": 200,
        }

        raw = self._rpc("listMarketCatalogue", params)
        markets: List[MarketInfo] = []

        if not isinstance(raw, list):
            log.warning("Unexpected listMarketCatalogue response: %r", raw)
            return markets

        for m in raw:
            market_id = m.get("marketId")
            if not market_id:
                continue
            markets.append(MarketInfo(
                market_id        = market_id,
                competition_name = m.get("competition", {}).get("name", "Unknown Competition"),
                event_name       = m.get("event", {}).get("name", "Unknown Event"),
                market_name      = m.get("marketName", "MATCH_ODDS"),
                runners          = m.get("runners", []),
                start_time       = m.get("marketStartTime"),
            ))

        log.info("Found %d football MATCH_ODDS markets.", len(markets))
        return markets

    # ------------------------------------------------------------------ #
    # Horse racing markets                                                 #
    # ------------------------------------------------------------------ #

    def list_horse_racing_markets(
        self,
        lookahead_hours: int = 48,
        market_types: Optional[List[str]] = None,
        venues: Optional[List[str]] = None,
    ) -> List[MarketInfo]:
        """
        Returns MarketInfo for horse racing WIN markets within the lookahead window.

        Args:
            lookahead_hours: How far ahead to look for races (default 48h).
            market_types: Betfair market type codes to include.
                          Defaults to ["WIN"]. Pass ["WIN", "EACH_WAY"] if needed.
            venues: Optional list of venue names to filter by, e.g. ["Cheltenham"].
                    If None, all venues are returned.

        Horse racing has no competition IDs — it's filtered by event type + market type.
        Races are identified by event (meeting) and market (individual race).

        Cheltenham Festival tip: pass venues=["Cheltenham"] to isolate festival markets.
        """
        if market_types is None:
            market_types = ["WIN"]

        now   = datetime.now(timezone.utc)
        until = now + timedelta(hours=lookahead_hours)

        market_filter: dict = {
            "eventTypeIds":   [HORSE_RACING_EVENT_TYPE_ID],
            "marketTypeCodes": market_types,
            "marketStartTime": {
                "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to":   until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "inPlayOnly": False,
        }

        if venues:
            market_filter["venues"] = venues

        log.info(
            "Horse racing catalogue refresh: market_types=%s venues=%s lookahead_hours=%s",
            market_types,
            venues,
            lookahead_hours,
        )

        params = {
            "filter": market_filter,
            "marketProjection": [
                "EVENT",
                "RUNNER_DESCRIPTION",
                "MARKET_START_TIME",
            ],
            "sort":       "FIRST_TO_START",
            "maxResults": 200,
        }

        raw = self._rpc("listMarketCatalogue", params)
        markets: List[MarketInfo] = []

        if not isinstance(raw, list):
            log.warning("Unexpected horse racing listMarketCatalogue response: %r", raw)
            return markets

        for m in raw:
            market_id = m.get("marketId")
            if not market_id:
                continue

            event      = m.get("event", {})
            venue      = event.get("venue", "Unknown Venue")
            event_name = event.get("name", "Unknown Meeting")

            markets.append(MarketInfo(
                market_id        = market_id,
                competition_name = venue,
                event_name       = event_name,
                market_name      = m.get("marketName", ""),   # e.g. "2m4f Hdle" race name
                runners          = m.get("runners", []),
                start_time       = m.get("marketStartTime"),
            ))

        log.info("Found %d horse racing %s markets.", len(markets), market_types)
        return markets

    def list_cheltenham_markets(
        self,
        lookahead_hours: int = 120,
        market_types: Optional[List[str]] = None,
    ) -> List[MarketInfo]:
        """
        Convenience wrapper for Cheltenham Festival markets.
        Defaults to 120h lookahead to cover the full 5-day festival.
        """
        return self.list_horse_racing_markets(
            lookahead_hours=lookahead_hours,
            market_types=market_types or ["WIN"],
            venues=["Cheltenham"],
        )

    # ------------------------------------------------------------------ #
    # Market book (shared — works for any sport)                          #
    # ------------------------------------------------------------------ #

    def best_lays_for_market(
        self,
        market_id: str,
        runners: List[dict],
    ) -> List[RunnerLay]:
        """
        Returns the best available lay price for each runner in the market.
        Works for both football and horse racing markets.
        """
        params = {
            "marketIds": [market_id],
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS"],
                "exBestOffersOverrides": {
                    "bestPricesDepth": 1,
                    "rollupModel":     "STAKE",
                    "rollupLimit":     0,
                },
            },
        }

        raw = self._rpc("listMarketBook", params)

        id_to_name = {
            r["selectionId"]: r.get("runnerName", "Unknown")
            for r in runners
            if "selectionId" in r
        }

        results: List[RunnerLay] = []

        if not isinstance(raw, list) or not raw:
            log.warning("No market book data for %s", market_id)
            return results

        for runner_book in raw[0].get("runners", []):
            selection_id = runner_book.get("selectionId")
            if not selection_id:
                continue

            runner_name = id_to_name.get(selection_id, f"Runner {selection_id}")
            lay_prices  = runner_book.get("ex", {}).get("availableToLay", [])
            best_lay    = lay_prices[0].get("price") if lay_prices else None

            results.append(RunnerLay(
                runner_name  = runner_name,
                selection_id = selection_id,
                best_lay     = float(best_lay) if best_lay is not None else None,
            ))

        return results

    def list_market_books(self, market_ids: List[str]) -> Dict[str, Dict[int, Optional[float]]]:
        """
        Bulk market book fetch — works for any sport.

        Returns:
          {
            "<market_id>": {
              <selection_id>: <best_lay_price_or_None>,
              ...
            },
            ...
          }
        """
        if not market_ids:
            return {}

        params = {
            "marketIds": market_ids,
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS"],
                "exBestOffersOverrides": {
                    "bestPricesDepth": 1,
                    "rollupModel":     "STAKE",
                    "rollupLimit":     0,
                },
            },
        }

        raw = self._rpc("listMarketBook", params)
        out: Dict[str, Dict[int, Optional[float]]] = {}

        if not isinstance(raw, list):
            log.warning("Unexpected listMarketBook response: %r", raw)
            return out

        for book in raw:
            market_id = book.get("marketId")
            if not market_id:
                continue

            runners_out: Dict[int, Optional[float]] = {}
            for runner_book in book.get("runners", []):
                selection_id = runner_book.get("selectionId")
                if not isinstance(selection_id, int):
                    continue
                lay_prices = runner_book.get("ex", {}).get("availableToLay", [])
                best_lay   = lay_prices[0].get("price") if lay_prices else None
                runners_out[selection_id] = float(best_lay) if best_lay is not None else None

            out[market_id] = runners_out

        return out
