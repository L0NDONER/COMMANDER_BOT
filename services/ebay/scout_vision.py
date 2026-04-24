#!/usr/bin/env python3
"""Vision-enhanced scout: identify items from photos then price via eBay.

Usage:
    result = evaluate_from_image("photo.jpg", buy_price=5.00)

Requires:
    pip install google-genai pillow
    GEMINI_API_KEY in credentials
"""

import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
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
    "Otherwise reply with ONLY a comma-separated list: brand, item type, size (if visible on label), then 3 style keywords. "
    "Example: 'Gant, Gingham Shirt, L, Preppy, Casual, Heritage'. "
    "Omit size if not clearly visible. No extra text."
)


def identify_item(image_path: str) -> tuple:
    """Return (search_query, keywords) from a photo. search_query is brand + type + size only."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    image = PIL.Image.open(image_path)

    def _call():
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[image, IDENTIFY_PROMPT],
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_call)
        try:
            response = future.result(timeout=GEMINI_TIMEOUT)
        except FuturesTimeoutError:
            raise TimeoutError(f"Gemini vision timed out after {GEMINI_TIMEOUT}s")

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


def evaluate_from_image(image_path: str, buy_price: float) -> Dict[str, object]:
    """Take a photo and buy price, return a full Vinted verdict."""
    query, keywords = identify_item(image_path)
    stats = get_stats(query)
    result = verdict(buy_price, stats, query, keywords=keywords)
    result["query"] = query
    return result
