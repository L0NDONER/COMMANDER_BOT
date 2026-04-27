#!/usr/bin/env python3
"""Telegram command handlers for Vinted-aware scout responses."""

import html
import logging
import random
import re
from typing import Tuple

TIPS = [
    "💡 Lighting is 50% of the sale. Take it outside for true colour accuracy.",
    "💡 Flat lay on a wood floor beats a hanger every time. Buyers see the fit better.",
    "💡 Depill before you shoot. A fabric shaver adds £2–£5 to any knitwear.",
    "💡 Measure pit-to-pit and length. Serious buyers pay extra for peace of mind.",
    "💡 Post within 24 hours of a sale. 5-star reviews build your ranking fast.",
    "💡 Natural daylight makes colours pop. Avoid yellow kitchen light.",
    "💡 Bundle offers attract buyers. List similar items and mention bundles in your bio.",
    "💡 First photo is your thumbnail. Make it count — clean background, good light.",
    "💡 Check the label for fabric content. 100% cotton and wool outsell polyester blends.",
    "💡 Vintage pieces sell better with era in the title — '90s', 'Y2K', 'retro' add clicks.",
]

from services.ebay.brands import handle_brands, get_brand_tip
from services.ebay.scout import get_stats, verdict


LOGGER = logging.getLogger(__name__)


def parse_scout(message: str) -> Tuple[str, float]:
    """Parse a scout message into query and buy price.

    Supported examples:
      scout barbour jacket XXL £8
      scout levi 501 W32
      scout "ralph lauren polo XL" 4
    """
    text = re.sub(r"^scout\s+", "", message.strip(), flags=re.IGNORECASE)
    price_match = re.search(r"£?(\d+(?:\.\d{1,2})?)\s*$", text)

    if price_match:
        buy_price = float(price_match.group(1))
        query = text[:price_match.start()].strip()
    else:
        buy_price = 5.0
        query = text.strip()

    query = query.strip('"\' ')
    return query, buy_price


def handle_scout_command(message: str, keywords: list = None) -> str:
    """Handle the full incoming scout message."""
    query, buy_price = parse_scout(message)

    if not query:
        return (
            "🔍 Vinted Scout\n"
            "Usage: scout [item] [optional £price]\n\n"
            "Examples:\n"
            "  scout levi 501 jeans W32\n"
            "  scout barbour jacket XXL £8\n"
            "  scout \"fred perry polo medium\" 4"
        )

    try:
        stats = get_stats(query)
        result = verdict(buy_price, stats, query, keywords=keywords)
        return _format(query, buy_price, stats, result)
    except Exception as exc:
        LOGGER.exception("Scout failed for query=%s", query)
        return f"❌ Scout error: {exc}"


def handle_scout_command_logged(message: str, keywords: list = None) -> tuple:
    """Returns (reply, verdict_str, median_price) for logging purposes."""
    query, buy_price = parse_scout(message)
    if not query:
        return handle_scout_command(message, keywords), "UNKNOWN", None
    try:
        stats = get_stats(query)
        result = verdict(buy_price, stats, query, keywords=keywords)
        reply = _format(query, buy_price, stats, result)
        median = float(stats["median"]) if "median" in stats else None
        return reply, result.get("verdict", "UNKNOWN"), median
    except Exception as exc:
        LOGGER.exception("Scout failed for query=%s", query)
        return f"❌ Scout error: {exc}", "ERROR", None



def _format(query: str, buy_price: float, stats: dict, result: dict) -> str:
    """Format a Telegram-safe plain text response."""
    safe_query = html.escape(query)
    if "reason" in result:
        return f"{result['verdict']} <b>{safe_query}</b>\n\n{html.escape(result['reason'])}"

    mock_tag = " (MOCK)" if stats.get("mock") else ""
    alert = result.get("high_value_alert", "")
    alert_block = f"{html.escape(alert)}\n\n" if alert else ""

    return (
        f"{alert_block}"
        f"🔍 {safe_query}\n\n"
        f"📊 eBay UK reference - {stats['count']} used listings{mock_tag}\n"
        f"Low: £{stats['low']:.2f}\n"
        f"Median: £{stats['median']:.2f}\n"
        f"High: £{stats['high']:.2f}\n\n"
        f"🧠 Vinted-adjusted using {int(result['discount'] * 100)}% of eBay median\n\n"
        f"💰 {result['verdict']}\n"
        f"Buy for: £{buy_price:.2f}\n"
        f"eBay ref: {result['ebay_sell_for']}\n"
        f"Vinted list: {result['sell_for']}\n"
        f"Fast sale: {result['fast_sale']}\n"
        f"Postage: {result['postage']}\n"
        f"Fees: {result['fees']}\n"
        f"Profit: {result['profit']}\n"
        f"ROI: {result['roi']}\n\n"
        f"TITLE\n"
        f"<code>{html.escape(result['title'])}</code>\n\n"
        f"DESCRIPTION\n"
        f"<code>{html.escape(result['description'])}</code>\n\n"
        f"{get_brand_tip(query) or f'<i>{random.choice(TIPS)}</i>'}"
    )
