#!/usr/bin/env python3
"""Vision-enhanced scout: identify items from photos then price via eBay.

Usage:
    result = evaluate_from_image("photo.jpg", buy_price=5.00)

Requires:
    pip install google-genai pillow
    GEMINI_API_KEY in credentials
"""

import asyncio
import sys
from typing import Dict

import PIL.Image
from google import genai

GEMINI_TIMEOUT = 20  # seconds

sys.path.insert(0, "/home/martin/commander")
from credentials import GEMINI_API_KEY

from services.ebay.scout import get_stats, verdict


IDENTIFY_PROMPT = (
    "Identify this item for a secondhand resale search. "
    "If you cannot identify a saleable secondhand item (e.g. barcode, food, blurry photo), reply with only: NOT_FOUND. "
    "Otherwise reply with ONLY a comma-separated list: brand, item type, size, then 3 style keywords. "
    "Example: 'Gant, Gingham Shirt, L, Preppy, Casual, Heritage'. "
    "For size: only report what is physically printed on a visible label in the photo. "
    "Do NOT guess size from the item's shape or proportions. If no label is legible, omit size entirely. "
    "No extra text."
)


def _call_gemini(image_path: str):
    """Blocking Gemini call — run via asyncio.to_thread in async contexts."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    image = PIL.Image.open(image_path)
    return client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[image, IDENTIFY_PROMPT],
    )


def identify_item(image_path: str) -> tuple:
    """Synchronous wrapper — use identify_item_async in async handlers."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_call_gemini, image_path)
        try:
            response = future.result(timeout=GEMINI_TIMEOUT)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"Gemini vision timed out after {GEMINI_TIMEOUT}s")

    return _parse_response(response)


def _parse_response(response) -> tuple:
    raw = response.text.strip()
    if raw.upper() == "NOT_FOUND":
        raise ValueError("NOT_FOUND")
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) >= 2:
        query = " ".join(parts[:3]) if len(parts) >= 3 else " ".join(parts[:2])
        keywords = parts[3:] if len(parts) > 3 else parts[2:]
    else:
        query = parts[0]
        keywords = []
    return query, keywords


async def identify_item_async(image_path: str) -> tuple:
    """Non-blocking version — use this in async Telegram handlers."""
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_call_gemini, image_path),
            timeout=GEMINI_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TimeoutError(f"Gemini vision timed out after {GEMINI_TIMEOUT}s")
    return _parse_response(response)


def evaluate_from_image(image_path: str, buy_price: float) -> Dict[str, object]:
    """Take a photo and buy price, return a full Vinted verdict."""
    query, keywords = identify_item(image_path)
    stats = get_stats(query)
    result = verdict(buy_price, stats, query, keywords=keywords)
    result["query"] = query
    return result
