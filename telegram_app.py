#!/usr/bin/env python3
"""
Telegram handler for commander bot.
Final Update: Explicit Vinted Price and Net Profit display.
"""

import json
import logging
import os
import sys
import re
from typing import Dict

# --- PATH FIX ---
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
EXTRA_PATH = os.path.join(CURRENT_DIR, "services", "ebay")
if EXTRA_PATH not in sys.path:
    sys.path.insert(0, EXTRA_PATH)
# ----------------

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from scout_update import evaluate_with_consensus

# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------

from credentials import TELEGRAM_BOT_TOKEN as TELEGRAM_TOKEN
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL)
LOGGER = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------------------

def format_result(result: Dict, raw_buy_input: str) -> str:
    """Convert result dict into Telegram message with real-world profit math."""

    if result.get("status") != "success":
        status = result.get("status", "error")
        return f"⚠️ Error: {result.get('message', status)}"

    # 1. Extract raw numbers
    median = result.get("median", 0)
    roi = result.get("roi", 0)
    confidence = result.get("confidence", "LOW")
    verdict = result.get("verdict", "❌ PASS")
    
    # 2. Financial Logic (0.72 is the Vinted Fee/Speed Discount)
    vinted_target = result.get("sell_price_num", round(median * 0.50, 2))
    
    # Extract numeric buy price from the user's caption (e.g., "4.50")
    try:
        numeric_buy = float(re.sub(r'[^\d.]', '', raw_buy_input))
    except:
        numeric_buy = 0.0
        
    net_profit = round(vinted_target - numeric_buy, 2)

    # 3. Listing Content
    title = result.get("title", "No Title")
    desc = result.get("description", "No Description")
    tags = result.get("tags", "")

    # 4. Construct Final Message
    msg = (
        f"Verdict: {verdict}\n\n"
        f"📊 **Market Data**\n"
        f"eBay Median: {result['median_pretty']}\n"
        f"🛡️ Confidence: {confidence}\n\n"
        f"💰 **Arbitrage Math**\n"
        f"Vinted List Price: **{result['sell_for']}**\n"
        f"Fast Sale: **{result['fast_sale']}**\n"
        f"Est. Net Profit: **£{net_profit:.2f}**\n"
        f"📈 ROI: {int(roi)}%\n"
        f"--------------------------\n\n"
        f"📝 **PROPOSED LISTING**\n\n"
        f"**Title:**\n`{title}`\n\n"
        f"**Description:**\n`{desc}`\n\n"
        f"**Tags:**\n`{tags}`"
    )

    return msg

# ------------------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Send an image + price (e.g. 5.00) to evaluate.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message.photo:
        return

    caption = update.message.caption
    if not caption:
        await update.message.reply_text("Please provide the buy price in the caption!")
        return

    # Download image
    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_path = f"/tmp/{file.file_id}.jpg"
    await file.download_to_drive(image_path)

    await update.message.reply_text("🤖 Dispatching agents to the jury...")

    try:
        # Run core logic from scout_update
        result = evaluate_with_consensus(image_path, caption)
        
        # Pass both result and the user's original caption for profit math
        message = format_result(result, caption)
        
        # Markdown enabled for copy-paste on tap
        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as exc:
        LOGGER.error("Processing failed: %s", exc)
        await update.message.reply_text("❌ The agent jury encountered an error.")

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN not set")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    LOGGER.info("Bot started and listening...")
    app.run_polling()

if __name__ == "__main__":
    main()
