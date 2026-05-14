#!/usr/bin/env python3
"""Anarchist worker: bypasses eBay search, queries Gemini for price estimate."""

import json
import logging
import os
import re
import socket

from google import genai

from services.ebay.scout_update import cast_vote, get_redis
from credentials import GEMINI_API_KEY

REPLICA = socket.gethostname()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger(f"anarchist.{REPLICA}")

PRICE_PROMPT = (
    "You are a Vinted resale pricing expert in the UK. "
    "For the item: '{query}', estimate the typical sold price in GBP "
    "on Vinted in good pre-owned condition. "
    "Respond with ONLY a number (no currency symbol, no text). "
    "Example: 24.50"
)


def estimate_price(query: str) -> float | None:
    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=PRICE_PROMPT.format(query=query),
    )
    raw = response.text.strip()
    match = re.search(r"\d+\.?\d*", raw)
    if not match:
        LOGGER.warning("Could not parse price from %r", raw)
        return None
    return float(match.group())


def handle_task(task: dict) -> None:
    img_hash = task.get("img_hash")
    base_query = task.get("base_query")
    if not img_hash or not base_query:
        LOGGER.warning("Malformed task: %s", task)
        return
    try:
        price = estimate_price(base_query)
    except Exception:
        LOGGER.exception("Gemini call failed for %r", base_query)
        return
    if price is None:
        return
    LOGGER.info("Estimated %.2f for %r", price, base_query)
    cast_vote(img_hash, REPLICA, price, f"LLM:{base_query}")


def run_anarchist() -> None:
    pubsub = get_redis().pubsub()
    pubsub.subscribe("scout_tasks")
    LOGGER.info("Anarchist %s subscribed to scout_tasks", REPLICA)
    for msg in pubsub.listen():
        if msg.get("type") != "message":
            continue
        try:
            task = json.loads(msg["data"])
        except json.JSONDecodeError:
            LOGGER.warning("Bad JSON on scout_tasks: %r", msg.get("data"))
            continue
        try:
            handle_task(task)
        except Exception:
            LOGGER.exception("handle_task failed for %s", task)


if __name__ == "__main__":
    run_anarchist()
