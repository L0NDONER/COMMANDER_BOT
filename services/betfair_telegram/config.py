# Betfair credentials
BETFAIR_USERNAME = "oflaherty@gmail.com"
BETFAIR_PASSWORD = "bHjH^H9HS6Rp8%"
BETFAIR_APP_KEY =  "PzaVCAQbsMWCULBM"
ODDS_API_KEY = "caaaa3c9cc07ae2a867a22f879e149c7"
BETFAIR_CERTS = "/home/martin/commander/services/betfair_telegram/certs"

# Telegram credentials
TELEGRAM_BOT_TOKEN = "7833661803:AAH-JheEFlbgu0n7CzHAc4HQsHUCQKtUEEo"
TELEGRAM_CHAT_ID = "5811891036"

# Event type IDs - you provided these; keep as-is
FOOTBALL_COMPETITION_IDS = [
    "39",        # English Premier League
    "228",       # UEFA Champions League  
    "2005",      # UEFA Europa League
    "117",       # English Championship (bonus)
    "30",        # FA Cup
]

# Football watcher
WATCH_POLL_SECONDS = 45
WATCH_LOOKAHEAD_HOURS = 48

# Horse Racing  
CHELTENHAM_MODE    = True
WATCH_HORSE_RACING = True

# Market types to watch / query
FOOTBALL_MARKET_TYPES = ["MATCH_ODDS"]

# Alert when lay odds are AT OR BELOW this
LAY_THRESHOLD = 2.0

# Bot polling tuning
TELEGRAM_POLL_TIMEOUT_SECONDS = 50
TELEGRAM_POLL_SLEEP_SECONDS = 1.0

# Event type map for "betfair markets <sport>" command
# Betfair event type IDs (SportsAPING)
# Used in listMarketCatalogue filter → eventTypeIds
# Source: https://docs.developer.betfair.com/display/1smk3cen4v3lu3yomq5qye0ni/listEventTypes
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
