#!/usr/bin/env python3
"""Identify items from photos: barcode (Open Library / Open Food Facts) → Gemini fallback.

Usage:
    query, keywords = identify_item("photo.jpg")
"""

import concurrent.futures
import logging

import PIL.Image
import requests
from google import genai
from pyzbar.pyzbar import decode as decode_barcode

from credentials import GEMINI_API_KEY

LOGGER = logging.getLogger(__name__)

GEMINI_TIMEOUT = 20  # seconds

IDENTIFY_PROMPT = (
    "Identify this item for a secondhand resale search. "
    "If you cannot identify a saleable secondhand item (e.g. barcode, food, blurry photo), reply with only: NOT_FOUND. "
    "Otherwise reply with ONLY a comma-separated list: brand, item type, size, then 3 style keywords. "
    "Example: 'Gant, Gingham Shirt, L, Preppy, Casual, Heritage'. "
    "For size: only report what is physically printed on a visible label in the photo. "
    "Do NOT guess size from the item's shape or proportions. If no label is legible, omit size entirely. "
    "No extra text."
)


def _scan_barcode(image_path: str) -> tuple | None:
    """Try to decode a barcode and look up the product. Returns (query, keywords) or None."""
    image = PIL.Image.open(image_path)
    barcodes = decode_barcode(image)
    if not barcodes:
        LOGGER.info("No barcode detected — falling back to Gemini")
        return None

    code = barcodes[0].data.decode("utf-8").strip()
    LOGGER.info("Barcode decoded: %s", code)

    try:
        # Open Library for ISBN (books)
        if len(code) in (10, 13) and code.startswith(("97", "0", "1")):
            r = requests.get(f"https://openlibrary.org/api/books?bibkeys=ISBN:{code}&format=json&jscmd=data", timeout=5)
            data = r.json()
            if data:
                book = list(data.values())[0]
                title = book.get("title", "")
                authors = ", ".join(a["name"] for a in book.get("authors", []))
                query = f"{authors} {title}".strip()
                LOGGER.info("ISBN hit: %s", query)
                return query, ["Book", "Paperback", "Collectible"]
            LOGGER.info("ISBN %s not in Open Library", code)

        # Open Food Facts / UPC lookup fallback
        r = requests.get(f"https://world.openfoodfacts.org/api/v0/product/{code}.json", timeout=5)
        data = r.json()
        if data.get("status") == 1:
            product = data["product"]
            name = product.get("product_name", "")
            brand = product.get("brands", "")
            if name:
                LOGGER.info("UPC hit: %s %s", brand, name)
                return f"{brand} {name}".strip(), []
            LOGGER.info("UPC %s found but no product name", code)
        else:
            LOGGER.info("UPC %s not in Open Food Facts", code)

    except Exception as exc:
        LOGGER.warning("Barcode lookup failed for %s: %s", code, exc)

    return None


def _call_gemini(image_path: str):
    client = genai.Client(api_key=GEMINI_API_KEY)
    image = PIL.Image.open(image_path)
    # Phone photos are 4–8MB; shrinking to a Gemini tile boundary cuts upload
    # latency over mobile by seconds. Barcode scanning keeps the full-res image.
    image.thumbnail((1568, 1568))
    return client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=[image, IDENTIFY_PROMPT],
    )


def identify_item(image_path: str) -> tuple:
    barcode_result = _scan_barcode(image_path)
    if barcode_result:
        return barcode_result
    # ThreadPoolExecutor gives the call site a hard wall-clock timeout —
    # the Gemini SDK has no caller-controllable timeout option.
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
