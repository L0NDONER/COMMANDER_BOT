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
import os
import re

from google import genai
from google.genai import types

from services.garden.materials import resolve_material, estimate_weight, DENSITY_MAP, RATE_MAP

LOGGER = logging.getLogger(__name__)

SPADE_HANDLE_CM = 150   # standard spade/fork handle length used as reference

# --- Pricing ---
VAN_PAYLOAD_KG = 1000   # safe working load per run
LABOUR_BASE    = 60.00  # call-out / loading labour per job
VAN_COST       = 15.00  # fuel + wear per run
TIP_FEE        = 20.00  # council tip commercial charge per run
MARGIN         = 1.20   # 20% buffer for AI volume estimation error

PROMPT = """You are estimating the volume of a garden waste pile.

A spade or garden fork is visible in the image as a scale reference.
Assume the spade/fork handle is {handle_cm}cm long.

Using the tool as a scale reference:
1. Estimate the pile's approximate length, width, and height in metres.
2. Estimate the volume in cubic metres (treat the pile as a rough cone or mound).
3. Note the material type if visible.

Reply in this exact format — numbers only, no ranges:
LENGTH_M: <number>
WIDTH_M: <number>
HEIGHT_M: <number>
VOLUME_M3: <number>
NOTES: <one short sentence>
"""


def _parse_response(text: str) -> dict:
    result = {}
    for key in ("LENGTH_M", "WIDTH_M", "HEIGHT_M", "VOLUME_M3"):
        m = re.search(rf"{key}:\s*([\d.]+)", text)
        if m:
            result[key] = float(m.group(1))
    m = re.search(r"NOTES:\s*(.+)", text)
    if m:
        result["NOTES"] = m.group(1).strip()
    return result


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
            model="gemini-3.1-flash",
            contents=[prompt, image_part],
        )
        parsed = _parse_response(response.text)

        if "VOLUME_M3" not in parsed:
            return f"❓ Couldn't parse volume from Gemini response:\n{response.text[:300]}"

        material = resolve_material(material_hint)
        volume = parsed["VOLUME_M3"]
        weight_kg = estimate_weight(volume, material)
        weight_tonnes = weight_kg / 1000

        # Runs needed based on safe payload
        runs = max(1, -(-weight_kg // VAN_PAYLOAD_KG))  # ceiling division
        van_status = "✅ 1 run" if runs == 1 else f"⚠️ {int(runs)} runs needed"

        # Costs
        rate          = RATE_MAP.get(material, 80)
        disposal_fee  = weight_tonnes * rate
        run_costs     = runs * (VAN_COST + TIP_FEE)
        subtotal      = disposal_fee + LABOUR_BASE + run_costs
        total_quote   = round(subtotal * MARGIN, 2)
        profit        = round(total_quote - subtotal, 2)

        density_note = f"{DENSITY_MAP.get(material, 200)} kg/m³"

        return (
            f"🌿 Garden Waste Estimate\n\n"
            f"📐 Dimensions\n"
            f"  {parsed.get('LENGTH_M', '?')}m × {parsed.get('WIDTH_M', '?')}m × {parsed.get('HEIGHT_M', '?')}m\n"
            f"  Volume: ~{volume:.2f} m³\n\n"
            f"⚖️ Weight ({material} @ {density_note})\n"
            f"  ~{weight_kg:.0f} kg ({weight_tonnes:.2f} tonnes)\n"
            f"  🚛 {van_status}\n\n"
            f"💷 Quote Breakdown\n"
            f"  Disposal:  £{disposal_fee:.2f} ({weight_tonnes:.2f}t × £{rate}/t)\n"
            f"  Labour:    £{LABOUR_BASE:.2f}\n"
            f"  Van/tip:   £{run_costs:.2f} ({int(runs)} run × £{VAN_COST + TIP_FEE:.0f})\n"
            f"  Margin:    £{profit:.2f} (20%)\n"
            f"  ──────────────\n"
            f"  TOTAL:     £{total_quote:.2f}\n\n"
            f"📝 {parsed.get('NOTES', '')}"
        )

    except Exception as exc:
        LOGGER.exception("Garden vision error")
        return f"❌ Vision error: {exc}"
