"""
eBay Deal Scout
---------------
Polls eBay local listings, cross-references against sold prices,
calculates real profit margin, and fires WhatsApp alerts via Twilio.

Usage:
    python local_scout.py           # Run once manually
    python local_scout.py --daemon  # Run on poll interval from config
"""

import sqlite3
import requests
import yaml
import logging
import argparse
import time
import sys
sys.path.insert(0, "/home/martin/ansible/commander")
from credentials import (
    EBAY_APP_ID,
    EBAY_SECRET,
    twilio_sid,
    tw_auth_k,
    twilio_from_number,
    my_mobile_number,
    GROQ_API_KEY,
    GROQ_MODEL,
)
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2
from pathlib import Path

# ---------------------------------------------------------------------------
# Config & logging
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "local_scout.watchlist"
DB_PATH = Path(__file__).parent / "scout.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "scout.log"),
    ],
)
log = logging.getLogger("local_scout")


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
            listing_id TEXT PRIMARY KEY,
            title TEXT,
            listed_price REAL,
            avg_sold_price REAL,
            margin_percent REAL,
            profit_gbp REAL,
            distance_miles REAL,
            alerted_at TEXT
        )
    """)
    conn.commit()
    return conn


def already_seen(conn, listing_id):
    row = conn.execute(
        "SELECT 1 FROM seen_listings WHERE listing_id = ?", (listing_id,)
    ).fetchone()
    return row is not None


def mark_seen(conn, listing_id, title, listed_price, avg_sold, margin, profit, distance):
    conn.execute("""
        INSERT OR IGNORE INTO seen_listings
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        listing_id, title, listed_price, avg_sold,
        margin, profit, distance,
        datetime.utcnow().isoformat()
    ))
    conn.commit()


# ---------------------------------------------------------------------------
# eBay API
# ---------------------------------------------------------------------------

def get_ebay_token(client_id, client_secret):
    """Get OAuth application token (client credentials flow)."""
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
    """Search active local eBay listings."""
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    params = {
        "q": keywords,
        "filter": (
            f"buyingOptions:{{FIXED_PRICE}},"
            f"itemLocationCountry:GB,"
            f"price:[0..{max_buy_price}],"
            f"priceCurrency:GBP"
        ),
        "fieldgroups": "EXTENDED",
        "limit": 20,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
        "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3DGB%2Czip%3D{postcode.replace(' ', '')}",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data.get("itemSummaries", [])


def get_avg_sold_price(token, keywords, num_results=15):
    """Get average sold price from completed listings."""
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    params = {
        "q": keywords,
        "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:GB,soldItems:true",
        "limit": num_results,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB",
    }
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("itemSummaries", [])
    if not items:
        return None
    prices = []
    for item in items:
        try:
            price = float(item["price"]["value"])
            prices.append(price)
        except (KeyError, ValueError):
            continue
    return round(sum(prices) / len(prices), 2) if prices else None


# ---------------------------------------------------------------------------
# Distance calculation
# ---------------------------------------------------------------------------

def postcode_to_coords(postcode):
    """Use postcodes.io to get lat/lng from a UK postcode."""
    resp = requests.get(
        f"https://api.postcodes.io/postcodes/{postcode.replace(' ', '')}",
        timeout=5
    )
    if resp.status_code == 200:
        result = resp.json().get("result", {})
        return result.get("latitude"), result.get("longitude")
    return None, None


def haversine_miles(lat1, lon1, lat2, lon2):
    """Straight-line distance between two lat/lng coords in miles."""
    R = 3958.8  # Earth radius in miles
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def get_listing_distance(item, home_lat, home_lon):
    """Extract listing location and return distance in miles."""
    try:
        loc = item.get("itemLocation", {})
        postcode = loc.get("postalCode", "")
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
    """
    Returns (profit_gbp, margin_percent) after eBay fees.
    profit  = what you net after selling minus what you paid
    margin  = profit as % of sold price
    """
    net_sold = avg_sold_price * (1 - fees_percent / 100)
    profit = net_sold - listed_price
    margin = (profit / avg_sold_price) * 100
    return round(profit, 2), round(margin, 1)


def is_good_deal(profit, margin, item_config, settings):
    min_margin = item_config.get("min_margin", settings["default_min_margin"])
    min_profit = item_config.get("min_profit", settings["default_min_profit"])
    return profit >= min_profit and margin >= min_margin


def is_priced_below_market(listed_price, avg_sold_price, item_config):
    """Filter out listings priced at or near market value — not worth flipping."""
    max_buy_ratio = item_config.get("max_buy_ratio", 0.65)
    return (listed_price / avg_sold_price) < max_buy_ratio


# ---------------------------------------------------------------------------
# WhatsApp alert
# ---------------------------------------------------------------------------

def send_whatsapp_alert(account_sid, auth_token, from_number, to_number, message):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    resp = requests.post(
        url,
        data={"From": from_number, "To": to_number, "Body": message},
        auth=(account_sid, auth_token),
        timeout=10,
    )
    resp.raise_for_status()
    log.info(f"Alert sent: {resp.json().get('sid')}")


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

def run_scout(config, ebay_token, conn, home_lat, home_lon, twilio_cfg):
    settings = config["settings"]
    alerted_count = 0

    for watch in config["watchlist"]:
        keywords = watch["keywords"]
        max_buy = watch["max_buy_price"]
        log.info(f"Scanning: {keywords}")

        try:
            listings = search_local_listings(
                ebay_token, keywords,
                settings["postcode"], settings["radius_miles"], max_buy
            )
        except Exception as e:
            log.error(f"Error fetching listings for '{keywords}': {e}")
            continue

        if not listings:
            log.info(f"  No listings found")
            continue

        log.info(f"  Found {len(listings)} listings, fetching sold prices...")

        try:
            avg_sold = get_avg_sold_price(ebay_token, keywords)
        except Exception as e:
            log.error(f"  Error fetching sold prices: {e}")
            continue

        if not avg_sold:
            log.info(f"  No sold price data available, skipping")
            continue

        log.info(f"  Avg sold: £{avg_sold}")

        for item in listings:
            listing_id = item.get("itemId")
            if not listing_id or already_seen(conn, listing_id):
                continue

            try:
                listed_price = float(item["price"]["value"])
            except (KeyError, ValueError):
                continue

            # Skip listings priced at or near market value
            if not is_priced_below_market(listed_price, avg_sold, watch):
                log.info(f"  Skipping (priced at market): {item.get('title')} £{listed_price} vs avg £{avg_sold}")
                mark_seen(conn, listing_id, item.get("title", ""), listed_price,
                          avg_sold, 0, 0, None)
                continue

            profit, margin = calculate_margin(
                listed_price, avg_sold, settings["ebay_fees_percent"]
            )

            if not is_good_deal(profit, margin, watch, settings):
                mark_seen(conn, listing_id, item.get("title", ""), listed_price,
                          avg_sold, margin, profit, None)
                continue

            distance = get_listing_distance(item, home_lat, home_lon)

            # Respect radius filter
            if distance and distance > settings["radius_miles"]:
                mark_seen(conn, listing_id, item.get("title", ""), listed_price,
                          avg_sold, margin, profit, distance)
                continue

            message = format_alert(item, listed_price, avg_sold, profit, margin, distance)
            log.info(f"  🔥 Deal found: {item.get('title')} - £{listed_price} | {margin}% margin")

            try:
                send_whatsapp_alert(
                    twilio_cfg["account_sid"],
                    twilio_cfg["auth_token"],
                    twilio_cfg["from_number"],
                    twilio_cfg["to_number"],
                    message,
                )
                alerted_count += 1
            except Exception as e:
                log.error(f"  Failed to send alert: {e}")

            mark_seen(conn, listing_id, item.get("title", ""), listed_price,
                      avg_sold, margin, profit, distance)

    log.info(f"Scan complete. {alerted_count} alerts sent.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true", help="Run continuously on poll interval")
    args = parser.parse_args()

    config = load_config()
    settings = config["settings"]

    ebay_client_id = EBAY_APP_ID
    ebay_client_secret = EBAY_SECRET
    twilio_cfg = {
        "account_sid": twilio_sid,
        "auth_token": tw_auth_k,
        "from_number": twilio_from_number,
        "to_number": my_mobile_number,
    }

    conn = init_db()

    log.info("Geocoding home postcode...")
    home_lat, home_lon = postcode_to_coords(settings["postcode"])
    if not home_lat:
        log.error("Could not geocode home postcode. Check local_scout.watchlist")
        return

    log.info(f"Home coords: {home_lat}, {home_lon}")

    def one_run():
        log.info("Fetching eBay token...")
        token = get_ebay_token(ebay_client_id, ebay_client_secret)
        run_scout(config, token, conn, home_lat, home_lon, twilio_cfg)

    if args.daemon:
        interval = settings["poll_interval_minutes"] * 60
        log.info(f"Running in daemon mode, polling every {settings['poll_interval_minutes']} mins")
        while True:
            one_run()
            log.info(f"Sleeping {settings['poll_interval_minutes']} minutes...")
            time.sleep(interval)
    else:
        one_run()


if __name__ == "__main__":
    main()
