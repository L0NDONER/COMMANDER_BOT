import os


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Environment variable {name!r} is not set. "
            f"Set it before running the Betfair trader."
        )
    return value


BETFAIR_USERNAME = _require("BETFAIR_USERNAME")
BETFAIR_PASSWORD = _require("BETFAIR_PASSWORD")
BETFAIR_APP_KEY = _require("BETFAIR_APP_KEY")
ODDS_API_KEY = _require("ODDS_API_KEY")
BETFAIR_CERTS = os.environ.get(
    "BETFAIR_CERTS",
    "/home/martin/commander/scripts/betfair_telegram/certs",
)

TELEGRAM_BOT_TOKEN = _require("BETFAIR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require("BETFAIR_TELEGRAM_CHAT_ID")

FOOTBALL_COMPETITION_IDS = [
    "39",        # English Premier League
    "228",       # UEFA Champions League
    "2005",      # UEFA Europa League
    "117",       # English Championship
    "30",        # FA Cup
]

WATCH_POLL_SECONDS = 45
WATCH_LOOKAHEAD_HOURS = 48

CHELTENHAM_MODE = True
WATCH_HORSE_RACING = True

FOOTBALL_MARKET_TYPES = ["MATCH_ODDS"]

LAY_THRESHOLD = 2.0

TELEGRAM_POLL_TIMEOUT_SECONDS = 50
TELEGRAM_POLL_SLEEP_SECONDS = 1.0

EVENT_TYPES = {
    "football": "1",
    "tennis": "2",
    "golf": "3",
    "cricket": "4",
    "rugby union": "5",
    "boxing": "6",
    "horse racing": "7",
    "motor sport": "8",
    "special bets": "10",
    "cycling": "11",
    "rowing": "12",
    "darts": "3503",
    "snooker": "6422",
    "basketball": "7522",
    "ice hockey": "7524",
    "baseball": "7511",
    "american football": "6423",
}
