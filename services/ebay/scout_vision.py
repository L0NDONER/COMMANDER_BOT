#!/usr/bin/env python3
"""Vision-enhanced scout: identify items from photos then price via eBay.

Usage:
    result = evaluate_from_image("photo.jpg", buy_price=5.00)

Requires:
    pip install google-genai pillow
    GEMINI_API_KEY in credentials
"""

import sys
from typing import Dict

import PIL.Image
from google import genai

sys.path.insert(0, "/home/martin/commander")
from credentials import GEMINI_API_KEY

from services.ebay.scout import get_stats, verdict


IDENTIFY_PROMPT = (
    "Identify this clothing item for a secondhand resale search. "
    "Reply with only: brand, item type, and size if visible on a label (e.g. 'Barbour wax jacket L'). "
    "Omit size if not clearly visible. No extra text."
)


def identify_item(image_path: str) -> str:
    """Use Gemini Flash vision to identify an item from a photo."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    image = PIL.Image.open(image_path)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[image, IDENTIFY_PROMPT],
    )
    return response.text.strip()


def evaluate_from_image(image_path: str, buy_price: float) -> Dict[str, object]:
    """Take a photo and buy price, return a full Vinted verdict."""
    query = identify_item(image_path)
    stats = get_stats(query)
    result = verdict(buy_price, stats, query)
    result["query"] = query
    return result
