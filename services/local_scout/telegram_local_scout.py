"""
eBay Deal Scout
---------------
Polls eBay local listings, cross-references against sold prices,
calculates real profit margin, and fires Telegram alerts.

Usage:
    python local_scout.py           # Run once manually
    python local_scout.py --daemon  # Run on poll interval from config
"""

import argparse
import logging
import sqlite3
import time
from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path

import requests
import yaml

from credentials import (
    EBAY_APP_ID,
    EBAY_SECRET,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)

CONFIG_PATH = Path(__file__).parent / "local_scout.watchlist"
DB_PATH     = Path(__file__).parent / "scout.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "scout.log"),
    ],
)
log = logging.getLogger("local_scout")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_listings (
            listing_id   TEXT PRIMARY KEY,
            title        TEXT,
            listed_price REAL,
            avg_sold_price REAL,
            margin_percent REAL,
            profit_gbp   REAL,
            distance_miles REAL,
            alerted_at   TEXT
        )
    """)
    conn.commit()
    return conn


def already_seen(conn, listing_id):
    return conn.execute(
        "SELECT 1 FROM seen_listings WHERE listing_id = ?", (listing_id,)
    ).fetchone() is not None


def mark_seen(conn, listing_id, title, listed_price, avg_sold, margin, profit, distance):
    conn.execute(
        "INSERT OR IGNORE INTO seen_listings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (listing_id, title, listed_price, avg_sold, margin, profit, distance,
         datetime.utcnow().isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# eBay API
# ---------------------------------------------------------------------------

def get_ebay_token(client_id, client_secret):
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        auth=(client_id, client_secret),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_local_listings(token, keywords, postcode, radius_miles, max_buy_price):
    resp = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3DGB%2Czip%3D{postcode.replace(' ', '')}",
        },
        params={
            "q": keywords,
            "filter": (
                f"buyingOptions:{{FIXED_PRICE}},"
                f"itemLocationCountry:GB,"
                f"price:[0..{max_buy_price}],"
                f"priceCurrency:GBP"
            ),
            "fieldgroups": "EXTENDED",
            "limit": 20,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("itemSummaries", [])


def get_avg_sold_price(token, keywords, num_results=15):
    resp = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        },
        params={
            "q": keywords,
            "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:GB,soldItems:true",
            "limit": num_results,
        },
        timeout=10,
    )
    resp.raise_for_status()
    items = resp.json().get("itemSummaries", [])
    prices = []
    for item in items:
        try:
            prices.append(float(item["price"]["value"]))
        except (KeyError, ValueError):
            continue
    return round(sum(prices) / len(prices), 2) if prices else None


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------

def postcode_to_coords(postcode):
    resp = requests.get(
        f"https://api.postcodes.io/postcodes/{postcode.replace(' ', '')}",
        timeout=5,
    )
    if resp.status_code == 200:
        result = resp.json().get("result", {})
        return result.get("latitude"), result.get("longitude")
    return None, None


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def get_listing_distance(item, home_lat, home_lon):
    try:
        postcode = item.get("itemLocation", {}).get("postalCode", "")
        if not postcode:
            return None
        lat, lon = postcode_to_coords(postcode)
        if lat and lon:
            return round(haversine_miles(home_lat, home_lon, lat, lon), 1)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Deal scoring
# ---------------------------------------------------------------------------

def calculate_margin(listed_price, avg_sold_price, fees_percent=13.5):
    net_sold = avg_sold_price * (1 - fees_percent / 100)
    profit   = net_sold - listed_price
    margin   = (profit / avg_sold_price) * 100
    return round(profit, 2), round(margin, 1)


def is_good_deal(profit, margin, item_config, settings):
    min_margin = item_config.get("min_margin", settings["default_min_margin"])
    min_profit = item_config.get("min_profit", settings["default_min_profit"])
    return profit >= min_profit and margin >= min_margin


def is_priced_below_market(listed_price, avg_sold_price, item_config):
    max_buy_ratio = item_config.get("max_buy_ratio", 0.65)
    return (listed_price / avg_sold_price) < max_buy_ratio


# ---------------------------------------------------------------------------
# Telegram alert
# ---------------------------------------------------------------------------

def send_telegram_alert(bot_token, chat_id, message):
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
        timeout=10,
    )
    resp.raise_for_status()
    log.info("Alert sent: %s", resp.json().get("result", {}).get("message_id"))


def format_alert(item, listed_price, avg_sold, profit, margin, distance):
    dist_str = f"📍 {distance} miles away\n" if distance else ""
    return (
        f"⚡ *{item['title']}*\n"
        f"💰 Listed: £{listed_price:.0f} | Avg sold: £{avg_sold:.0f}\n"
        f"📈 Margin: {margin}% | Est. profit: £{profit:.0f}\n"
        f"{dist_str}"
        f"🔗 {item.get('itemWebUrl', 'No link')}"
    )


# ---------------------------------------------------------------------------
# Main scout loop
# ---------------------------------------------------------------------------

def run_scout(config, ebay_token, conn, home_lat, home_lon):
    settings      = config["settings"]
    alerted_count = 0

    for watch in config["watchlist"]:
        keywords = watch["keywords"]
        max_buy  = watch["max_buy_price"]
        log.info("Scanning: %s", keywords)

        try:
            listings = search_local_listings(
                ebay_token, keywords,
                settings["postcode"], settings["radius_miles"], max_buy,
            )
        except Exception as e:
            log.error("Error fetching listings for '%s': %s", keywords, e)
            continue

        if not listings:
            log.info("  No listings found")
            continue

        log.info("  Found %d listings, fetching sold prices...", len(listings))

        try:
            avg_sold = get_avg_sold_price(ebay_token, keywords)
        except Exception as e:
            log.error("  Error fetching sold prices: %s", e)
            continue

        if not avg_sold:
            log.info("  No sold price data available, skipping")
            continue

        log.info("  Avg sold: £%s", avg_sold)

        for item in listings:
            listing_id = item.get("itemId")
            if not listing_id or already_seen(conn, listing_id):
                continue

            try:
                listed_price = float(item["price"]["value"])
            except (KeyError, ValueError):
                continue

            if not is_priced_below_market(listed_price, avg_sold, watch):
                log.info("  Skipping (priced at market): %s £%s vs avg £%s",
                         item.get("title"), listed_price, avg_sold)
                mark_seen(conn, listing_id, item.get("title", ""),
                          listed_price, avg_sold, 0, 0, None)
                continue

            profit, margin = calculate_margin(
                listed_price, avg_sold, settings["ebay_fees_percent"]
            )

            if not is_good_deal(profit, margin, watch, settings):
                mark_seen(conn, listing_id, item.get("title", ""),
                          listed_price, avg_sold, margin, profit, None)
                continue

            distance = get_listing_distance(item, home_lat, home_lon)

            if distance and distance > settings["radius_miles"]:
                mark_seen(conn, listing_id, item.get("title", ""),
                          listed_price, avg_sold, margin, profit, distance)
                continue

            message = format_alert(item, listed_price, avg_sold, profit, margin, distance)
            log.info("  🔥 Deal found: %s - £%s | %s%% margin",
                     item.get("title"), listed_price, margin)

            try:
                send_telegram_alert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, message)
                alerted_count += 1
            except Exception as e:
                log.error("  Failed to send alert: %s", e)

            mark_seen(conn, listing_id, item.get("title", ""),
                      listed_price, avg_sold, margin, profit, distance)

    log.info("Scan complete. %d alerts sent.", alerted_count)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true", help="Run continuously on poll interval")
    args = parser.parse_args()

    config   = load_config()
    settings = config["settings"]
    conn     = init_db()

    log.info("Geocoding home postcode...")
    home_lat, home_lon = postcode_to_coords(settings["postcode"])
    if not home_lat:
        log.error("Could not geocode home postcode. Check local_scout.watchlist")
        return

    log.info("Home coords: %s, %s", home_lat, home_lon)

    # Cache token — valid for 2 hours, refresh each run in daemon mode
    def one_run():
        log.info("Fetching eBay token...")
        token = get_ebay_token(EBAY_APP_ID, EBAY_SECRET)
        run_scout(config, token, conn, home_lat, home_lon)

    if args.daemon:
        interval = settings["poll_interval_minutes"] * 60
        log.info("Daemon mode — polling every %d mins", settings["poll_interval_minutes"])
        while True:
            one_run()
            log.info("Sleeping %d minutes...", settings["poll_interval_minutes"])
            time.sleep(interval)
    else:
        one_run()


if __name__ == "__main__":
    main()
