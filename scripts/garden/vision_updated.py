# services/garden/vision.py
"""Estimate garden waste volume from a photo using Gemini Flash.

Usage (via Telegram):
  Send a photo with caption:  garden [material]
  e.g.  garden hedge
        garden heavy_green
        garden soil

A spade or fork must be visible in the photo as a scale reference.
"""

import logging
import math
import os
import re

from google import genai
from google.genai import types

from services.garden.materials import resolve_material, estimate_weight, DENSITY_MAP, RATE_MAP

LOGGER = logging.getLogger(__name__)

SPADE_HANDLE_CM = 150   # standard spade/fork handle length used as reference

# --- Pricing ---
VAN_PAYLOAD_KG    = 1000    # safe working load per run
LABOUR_BASE       = 60.00   # call-out / loading labour per job
VAN_COST          = 15.00   # fuel + wear per run
TIP_FEE           = 20.00   # council tip commercial charge per run
VOLUME_BUFFER     = 1.20    # 20% buffer on disposal costs only (AI estimation error)
PROFIT_MARGIN     = 1.15    # 15% profit on top of total costs
VOLUME_WARN_M3    = 8.0     # flag quotes above this for manual sanity check

PROMPT = """You are estimating the volume of a garden waste pile.

A spade or garden fork is visible in the image as a scale reference.
Assume the spade/fork handle is {handle_cm}cm long.

Using the tool as a scale reference:
1. Estimate the pile's approximate length, width, and height in metres.
2. Estimate the volume in cubic metres (treat the pile as a rough cone or mound).
3. Note the material type if visible.
4. Rate your confidence based on how clearly the spade is visible and how
   well-defined the pile edges are.

Reply in this exact format — numbers only, no ranges:
LENGTH_M: <number>
WIDTH_M: <number>
HEIGHT_M: <number>
VOLUME_M3: <number>
CONFIDENCE: <low|medium|high>
NOTES: <one short sentence>
"""


def _parse_response(text: str) -> dict:
    result = {}
    # Stricter numeric regex: one or more digits, optional single decimal point
    for key in ("LENGTH_M", "WIDTH_M", "HEIGHT_M", "VOLUME_M3"):
        m = re.search(rf"{key}:\s*(\d+\.?\d*)", text)
        if m:
            try:
                result[key] = float(m.group(1))
            except ValueError:
                LOGGER.warning("Failed to parse %s from: %s", key, m.group(1))
    m = re.search(r"CONFIDENCE:\s*(low|medium|high)", text, re.IGNORECASE)
    if m:
        result["CONFIDENCE"] = m.group(1).lower()
    m = re.search(r"NOTES:\s*(.+)", text)
    if m:
        result["NOTES"] = m.group(1).strip()
    return result


def _confidence_emoji(level: str) -> str:
    return {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(level, "⚪")


def analyse_photo(image_bytes: bytes, material_hint: str = "heavy_green") -> str:
    """Send image to Gemini Flash and return a formatted weight estimate."""
    try:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            try:
                import credentials
                api_key = credentials.GEMINI_API_KEY
            except (ImportError, AttributeError):
                return "❌ GEMINI_API_KEY not set"

        client = genai.Client(api_key=api_key)
        prompt = PROMPT.format(handle_cm=SPADE_HANDLE_CM)
        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[prompt, image_part],
        )
        parsed = _parse_response(response.text)

        if "VOLUME_M3" not in parsed:
            return f"❓ Couldn't parse volume from Gemini response:\n{response.text[:300]}"

        material = resolve_material(material_hint)
        volume = parsed["VOLUME_M3"]
        confidence = parsed.get("CONFIDENCE", "medium")

        # Sanity check on volume — flag absurd readings before quoting
        volume_warning = ""
        if volume > VOLUME_WARN_M3:
            volume_warning = (
                f"\n⚠️ Large volume estimate ({volume:.1f} m³) — "
                f"double-check on site before quoting.\n"
            )
        elif volume <= 0:
            return f"❌ Invalid volume estimate ({volume} m³) — retake photo with spade visible."

        # Weight and runs
        weight_kg = estimate_weight(volume, material)
        weight_tonnes = weight_kg / 1000
        runs = max(1, math.ceil(weight_kg / VAN_PAYLOAD_KG))
        van_status = "✅ 1 run" if runs == 1 else f"⚠️ {runs} runs needed"

        # Costs — buffer applied to disposal only (where volume error lives)
        rate             = RATE_MAP.get(material, 80)
        disposal_raw     = weight_tonnes * rate
        disposal_buffered = disposal_raw * VOLUME_BUFFER
        fixed_costs      = LABOUR_BASE + (runs * (VAN_COST + TIP_FEE))
        cost_total       = disposal_buffered + fixed_costs

        # Profit margin on top of total costs
        total_quote = round(cost_total * PROFIT_MARGIN, 2)
        profit      = round(total_quote - cost_total, 2)

        density_note = f"{DENSITY_MAP.get(material, 200)} kg/m³"
        conf_icon    = _confidence_emoji(confidence)

        return (
            f"🌿 Garden Waste Estimate {conf_icon}\n"
            f"{volume_warning}"
            f"\n📐 Dimensions\n"
            f"  {parsed.get('LENGTH_M', '?')}m × {parsed.get('WIDTH_M', '?')}m × {parsed.get('HEIGHT_M', '?')}m\n"
            f"  Volume: ~{volume:.2f} m³\n\n"
            f"⚖️ Weight ({material} @ {density_note})\n"
            f"  ~{weight_kg:.0f} kg ({weight_tonnes:.2f} tonnes)\n"
            f"  🚛 {van_status}\n\n"
            f"💷 Quote Breakdown\n"
            f"  Disposal:   £{disposal_raw:.2f} ({weight_tonnes:.2f}t × £{rate}/t)\n"
            f"  +20% buffer: £{disposal_buffered - disposal_raw:.2f}\n"
            f"  Labour:     £{LABOUR_BASE:.2f}\n"
            f"  Van/tip:    £{runs * (VAN_COST + TIP_FEE):.2f} "
            f"({runs} run{'s' if runs > 1 else ''} × £{VAN_COST + TIP_FEE:.0f})\n"
            f"  Profit 15%: £{profit:.2f}\n"
            f"  ──────────────\n"
            f"  TOTAL:      £{total_quote:.2f}\n\n"
            f"🎯 Confidence: {confidence}\n"
            f"📝 {parsed.get('NOTES', '')}"
        )

    except Exception as exc:
        LOGGER.exception("Garden vision error")
        return f"❌ Vision error: {exc}"
