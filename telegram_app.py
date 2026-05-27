#!/usr/bin/env python3
"""
Telegram handler for commander bot.
Final Update: Explicit Vinted Price and Net Profit display.
"""

import asyncio
import logging
import os
import re
from typing import Dict

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import uvicorn

import database
from services.ebay.scout_async import evaluate_with_consensus_saas
from services.ebay.vinted_fetcher import warmup as vinted_warmup
from web_app import app as web_app

# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------

from credentials import TELEGRAM_BOT_TOKEN as TELEGRAM_TOKEN
from credentials import TELEGRAM_CHAT_ID as OWNER_CHAT_ID
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL)
LOGGER = logging.getLogger(__name__)

# httpx logs every request at INFO including the bot token in the URL.
# Mute below WARNING so docker logs don't expose secrets.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._utils.networkloop").setLevel(logging.ERROR)

# ------------------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------------------

def format_result(result: Dict, raw_buy_input: str) -> str:
    """Convert result dict into Telegram message with real-world profit math."""

    if result.get("status") != "success":
        status = result.get("status", "error")
        return f"⚠️ Error: {result.get('message', status)}"

    # 1. Extract raw numbers
    roi = result.get("roi", 0)
    confidence = result.get("confidence", "LOW")
    verdict = result.get("verdict", "❌ PASS")

    vinted_target = result["sell_price_num"]

    # Extract numeric buy price from the user's caption (e.g., "4.50")
    try:
        numeric_buy = float(re.sub(r'[^\d.]', '', raw_buy_input))
    except (ValueError, TypeError):
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
# /sold parsing
# ------------------------------------------------------------------------------

_SOLD_PRICE_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")


def parse_sold(text: str):
    """Pull £price out of a freeform /sold message, return (query, price).

    Returns (None, None) if no £-prefixed price is present.
    """
    match = _SOLD_PRICE_RE.search(text)
    if not match:
        return None, None
    price = float(match.group(1))
    query = (text[:match.start()] + text[match.end():]).strip(" -,")
    return query, price


# ------------------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Send an image + price (e.g. 5.00) to evaluate.")


async def handle_sold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if int(update.effective_chat.id) != int(OWNER_CHAT_ID):
        return  # Silently ignore — sales DB is owner-only

    raw = (update.message.text or "").removeprefix("/sold").strip()
    if not raw:
        await update.message.reply_text(
            "Usage: /sold <description> £<price>\n"
            "Example: /sold Jordan 1 Low uk 9 BNIB £55"
        )
        return

    query, price = parse_sold(raw)
    if price is None:
        await update.message.reply_text("❌ Couldn't find a £price in that message.")
        return

    sale_id = await database.log_sale(update.effective_chat.id, query, price, raw)
    await update.message.reply_text(f"✅ Logged #{sale_id}: {query} @ £{price:.2f}")

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
        result = await evaluate_with_consensus_saas(image_path, caption)

        message = format_result(result, caption)
        await update.message.reply_text(message, parse_mode='Markdown')

        # Auto-log this evaluation as a candidate buy for /pnl reconciliation.
        # Gated to owner so strangers' captions don't pollute the moat DB.
        if (int(update.effective_chat.id) == int(OWNER_CHAT_ID)
                and result.get("status") == "success"):
            try:
                buy_price = float(re.sub(r'[^\d.]', '', caption))
            except (ValueError, TypeError):
                buy_price = 0.0
            if buy_price > 0 and result.get("query"):
                await database.log_buy(
                    update.effective_chat.id,
                    result["query"],
                    buy_price,
                    result.get("median"),
                    result.get("sell_price_num"),
                    result.get("verdict"),
                    caption,
                )
    except Exception as exc:
        LOGGER.error("Processing failed: %s", exc)
        await update.message.reply_text("❌ The agent jury encountered an error.")
    finally:
        try:
            os.remove(image_path)
        except OSError:
            pass


async def handle_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if int(update.effective_chat.id) != int(OWNER_CHAT_ID):
        return  # Owner-only — pnl is private moat data

    rows = await database.pnl()
    if not rows:
        await update.message.reply_text("No buys or sales logged yet.")
        return

    matched, orphan_buys, orphan_sales = [], [], []
    total_buy = total_sale = 0.0
    for q, bought, sold, net, bn, sn in rows:
        total_buy += bought
        total_sale += sold
        if bn and sn:
            matched.append(f"`{q}`: £{bought:.2f} → £{sold:.2f} = £{net:+.2f}")
        elif bn:
            orphan_buys.append(f"`{q}`: £{bought:.2f}")
        else:
            orphan_sales.append(f"`{q}`: £{sold:.2f}")

    lines = ["📊 *P&L*"]
    if matched:
        lines += ["", "*Matched:*", *matched]
    if orphan_buys:
        lines += ["", "*Bought, not sold yet:*", *orphan_buys]
    if orphan_sales:
        lines += ["", "*Sold, no matching buy logged:*", *orphan_sales]
    lines += [
        "",
        f"*Totals:* spent £{total_buy:.2f}, earned £{total_sale:.2f}, "
        f"net £{total_sale - total_buy:+.2f}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------

async def main_async() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN not set")

    await database.init_db()
    asyncio.create_task(vinted_warmup())

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("sold", handle_sold))
    app.add_handler(CommandHandler("pnl", handle_pnl))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    config = uvicorn.Config(
        web_app,
        host=os.getenv("WEB_HOST", "0.0.0.0"),
        port=int(os.getenv("WEB_PORT", "8080")),
        log_level=LOG_LEVEL.lower(),
    )
    server = uvicorn.Server(config)

    LOGGER.info("Bot + web server started.")
    try:
        await server.serve()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await database.checkpoint()      # fold WAL into the .db before exit


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
