#!/usr/bin/env python3

import sys
from pathlib import Path

# Ensure project root (/app) is importable for services.*
APP_DIR = str(Path(__file__).resolve().parent)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import logging
import re
import tempfile

from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

from services.garden.vision import analyse_photo
from services.ebay.scout_vision import identify_item
from services.ebay.handler import handle_scout_command

import telegram_config as config
from safety_belt import SafetyConfig, safe_execute

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

groq_client = Groq(api_key=config.GROQ_API_KEY)
SAFETY_CFG = SafetyConfig()


def route_command(body_raw: str):
    body = body_raw.lower().strip()

    for prefix, handler in config.PREFIX_COMMANDS.items():
        token = f"{prefix} "
        if body.startswith(token):
            arg = body_raw.strip()[len(token):].strip()
            return handler(arg)

    for _, (handler, keywords) in config.KEYWORD_COMMANDS.items():
        if any(k in body for k in keywords):
            return handler()

    return None


def get_ai_fallback(body_raw: str) -> str:
    try:
        system_prompt = (
            "You are Minty, a helpful Telegram assistant for a network engineer.\n"
            "You have two modes:\n"
            "1. If the message is about infrastructure, networking, servers, or looks like a command request, "
            "suggest relevant commands like: ssh <host>, ping <host>, tailping <node>, tail, status.\n"
            "2. If the message is casual (jokes, questions, general chat), just respond naturally and helpfully.\n"
            "3. Even after infrastructure commands, do not assume future messages are commands unless clearly "
            "formatted as one.\n"
            "Be concise. Never wrap casual responses in command syntax."
        )

        completion = groq_client.chat.completions.create(
            model=getattr(config, "GROQ_MODEL", "llama-3.3-70b-versatile"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": body_raw},
            ],
            temperature=0.7,
            max_tokens=500,
        )

        return f"Minty: {completion.choices[0].message.content.strip()}"

    except Exception:
        log.exception("Groq error")
        return "Minty: Brain fog... 🧠"


def _is_handled_prefix(body_raw: str) -> bool:
    body = body_raw.lower().strip()
    return any(body.startswith(f"{prefix} ") for prefix in config.PREFIX_COMMANDS)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    body_raw = (update.message.text or "").strip()

    if not body_raw:
        return

    def _do_work() -> str:
        if config.ALLOWED_CHAT_IDS and chat_id not in config.ALLOWED_CHAT_IDS:
            log.warning("Blocked message from unauthorised chat_id: %s", chat_id)
            return "⛔ Not authorised"
        result = route_command(body_raw)
        if result is None and _is_handled_prefix(body_raw):
            return ""
        return result or get_ai_fallback(body_raw)

    reply = safe_execute(
        _do_work,
        cfg=SAFETY_CFG,
        context={"from": chat_id, "body": body_raw[:80]},
    )

    if reply:
        await update.message.reply_text(reply)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    if config.ALLOWED_CHAT_IDS and chat_id not in config.ALLOWED_CHAT_IDS:
        return

    caption = (update.message.caption or "").strip()
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())

    caption_lower = caption.lower()

    if caption_lower.startswith("scout"):
        price_match = re.search(r"£?(\d+(?:\.\d{1,2})?)", caption)
        buy_price = float(price_match.group(1)) if price_match else 5.0

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name

        try:
            query, keywords = identify_item(tmp_path)
            reply = handle_scout_command(f"scout {query} £{buy_price:.2f}", keywords=keywords)
        except Exception as exc:
            log.exception("Vision scout failed")
            reply = f"❌ Vision scout error: {exc}"
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    elif caption_lower.startswith("garden"):
        material = caption_lower.replace("garden", "").strip() or "heavy_green"
        reply = analyse_photo(image_bytes, material)

    else:
        return

    await update.message.reply_text(reply, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 <b>Scout</b>\nSend a photo with caption: <code>scout £5</code>\n\n"
        "🌿 <b>Garden</b>\nSend a photo with caption: <code>garden</code>\n\n"
        "🔍 <b>Text Scout</b>\nType: <code>scout barbour jacket XL £8</code>\n\n"
        "📋 <b>Brands guide</b>\nType: <code>brands</code>",
        parse_mode="HTML"
    )


if __name__ == "__main__":
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    log.info("Minty is running...")
    app.run_polling()
