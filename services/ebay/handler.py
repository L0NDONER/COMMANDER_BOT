#!/usr/bin/env python3
"""Telegram command handlers for Vinted-aware scout responses."""

import logging
import re
from typing import Tuple

from services.ebay.brands import handle_brands
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


def handle_scout_command(message: str) -> str:
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
        result = verdict(buy_price, stats, query)
        return _format(query, buy_price, stats, result)
    except Exception as exc:
        LOGGER.exception("Scout failed for query=%s", query)
        return f"❌ Scout error: {exc}"



def _format(query: str, buy_price: float, stats: dict, result: dict) -> str:
    """Format a Telegram-safe plain text response."""
    if "reason" in result:
        return f"❓ {query}\n{result['reason']}"

    mock_tag = " (MOCK)" if stats.get("mock") else ""

    return (
        f"🔍 {query}\n\n"
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
        f"{result['title']}\n\n"
        f"DESCRIPTION\n"
        f"{result['description']}"
    )
