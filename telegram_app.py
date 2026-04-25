#!/usr/bin/env python3
"""Commander Scout — Telegram Stars gated version.

Each scout query costs STARS_PER_SCOUT stars.
Users top up via /buy which sends a Telegram Stars invoice.
"""

import sys
import logging
import re
import tempfile
from pathlib import Path

APP_DIR = str(Path(__file__).resolve().parent)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

from services.ebay.scout_vision import identify_item_async
from services.ebay.handler import handle_scout_command, handle_scout_command_logged
import telegram_config as config
from stars_db import (
    init_db, get_balance, add_stars, deduct_stars,
    get_region, set_region, STARS_PER_SCOUT, BOUNTY_TIERS,
    submit_video_for_review, get_pending_rewards, approve_reward,
    has_claimed_bounty, log_scout, get_trends, get_expert_users,
)

ADMIN_CHAT_ID = str(getattr(config, "ADMIN_CHAT_ID", ""))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Stars bundle options: (label, star_amount)
BUNDLES = [
    ("Starter — 50 Stars (10 scouts)", 50),
    ("Value — 150 Stars (30 scouts)", 150),
    ("Pro — 500 Stars (100 scouts)", 500),
]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    balance = get_balance(chat_id)
    keyboard = [
        [InlineKeyboardButton("🇬🇧 UK Market", callback_data="set_region_uk")],
        [InlineKeyboardButton("🇺🇸 US Market", callback_data="set_region_us")],
    ]
    await update.message.reply_text(
        f"👋 Welcome to Scout Bot!\n\n"
        f"📸 Send a photo with caption: scout £5\n"
        f"💫 Each scout costs {STARS_PER_SCOUT} Stars\n"
        f"⭐ Your balance: {balance} Stars\n\n"
        f"Use /buy to top up.\n\n"
        f"Which market should I scout?\n\n"
        f"<i>🔒 Privacy: Commander stores your Star balance only. "
        f"Search queries are anonymised. Nothing else is kept.</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_region_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = str(query.from_user.id)
    region = "us" if query.data == "set_region_us" else "uk"
    set_region(chat_id, region)
    flag = "🇺🇸" if region == "us" else "🇬🇧"
    await query.edit_message_text(
        f"{flag} Market set to {'US' if region == 'us' else 'UK'}. "
        f"You're good to go — send a photo with scout £5 to start."
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    balance = get_balance(chat_id)
    scouts_left = balance // STARS_PER_SCOUT
    await update.message.reply_text(
        f"⭐ Balance: {balance} Stars ({scouts_left} scouts remaining)\n\n"
        f"Use /buy to top up."
    )


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a Stars invoice for the starter bundle."""
    chat_id = str(update.effective_chat.id)
    label, star_amount = BUNDLES[0]  # default to starter

    args = context.args
    if args and args[0].isdigit():
        amount = int(args[0])
        found = next((b for b in BUNDLES if b[1] == amount), None)
        if found:
            label, star_amount = found[0], found[1]
        else:
            await update.message.reply_text("Invalid bundle! Use /buy 50, 150, or 500.")
            return

    await context.bot.send_invoice(
        chat_id=chat_id,
        title="Scout Credits",
        description=label,
        payload=f"stars_{star_amount}",
        currency="XTR",          # Telegram Stars currency code
        prices=[LabeledPrice(label=label, amount=star_amount)],
    )


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Always confirm — Telegram requires this within 10 seconds."""
    await update.pre_checkout_query.answer(ok=True)


async def on_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Credit the user's balance after Stars payment confirmed."""
    chat_id = str(update.effective_chat.id)
    payload = update.message.successful_payment.invoice_payload
    star_amount = int(payload.split("_")[1])
    add_stars(chat_id, star_amount)
    balance = get_balance(chat_id)
    scouts_left = balance // STARS_PER_SCOUT
    await update.message.reply_text(
        f"✅ Payment received! +{star_amount} Stars added.\n"
        f"⭐ New balance: {balance} Stars ({scouts_left} scouts remaining)"
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    caption = (update.message.caption or "").strip()

    if not caption.lower().startswith("scout"):
        return

    is_admin = bool(ADMIN_CHAT_ID) and chat_id == ADMIN_CHAT_ID
    balance = get_balance(chat_id)
    if not is_admin and balance < STARS_PER_SCOUT:
        await update.message.reply_text(
            f"⭐ Not enough Stars (need {STARS_PER_SCOUT}, have {balance}).\n"
            f"Use /buy to top up."
        )
        return

    price_match = re.search(r"£?(\d+(?:\.\d{1,2})?)", caption)
    buy_price = float(price_match.group(1)) if price_match else 5.0

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name
        query, keywords = await identify_item_async(tmp_path)
        reply, verdict_str, median_price = handle_scout_command_logged(f"scout {query} £{buy_price:.2f}", keywords=keywords)
        if not is_admin:
            deduct_stars(chat_id, STARS_PER_SCOUT)
            balance_after = get_balance(chat_id)
            reply += f"\n\n⭐ {balance_after} Stars remaining"
        log_scout(query, verdict_str, median_price, chat_id=chat_id)
    except ValueError as exc:
        if "NOT_FOUND" in str(exc):
            reply = (
                "🤷 <b>Item not recognised.</b>\n\n"
                "No Stars charged.\n\n"
                "Try:\n"
                "• A clearer shot of the item\n"
                "• Include the label in the frame\n"
                "• Avoid barcodes, backgrounds, or blurry angles"
            )
        else:
            reply = f"❌ Scout error: {exc}"
    except TimeoutError:
        reply = "⏱ Vision took too long to respond. No Stars charged — please try again."
    except Exception as exc:
        log.exception("Vision scout failed")
        reply = "❌ Something went wrong. No Stars charged — please try again."
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

    await update.message.reply_text(reply, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 <b>Scout an item</b>\nSend a photo with caption: <code>scout £5</code>\n\n"
        "⭐ <b>Check balance</b>\n/balance\n\n"
        "💳 <b>Top up Stars</b>\n/buy — 50 Stars (10 scouts)\n/buy 150 — 150 Stars\n/buy 500 — 500 Stars\n\n"
        "🎬 <b>Earn free Stars</b>\n/promote — share the bot, get Stars back\n\n"
        "💡 Each scout costs 5 Stars. No charge if the item can't be identified.",
        parse_mode="HTML"
    )


async def cmd_promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User submits a video/post link for a bounty reward."""
    chat_id = str(update.effective_chat.id)

    if has_claimed_bounty(chat_id):
        await update.message.reply_text("✅ You've already claimed your bounty reward. Thanks for spreading the word!")
        return

    tier_list = "\n".join(f"  /promote {k} <link> — {label} (+{stars} Stars)" for k, (label, stars) in BOUNTY_TIERS.items())
    args = context.args

    if len(args) < 2 or args[0] not in BOUNTY_TIERS:
        await update.message.reply_text(
            f"🎬 Earn free Stars by sharing Scout Bot!\n\n"
            f"{tier_list}\n\n"
            f"Example: /promote video https://tiktok.com/..."
        )
        return

    tier, url = args[0], args[1]
    submitted = submit_video_for_review(chat_id, tier, url)
    if not submitted:
        await update.message.reply_text("You've already claimed a bounty reward.")
        return

    _, stars = BOUNTY_TIERS[tier]
    await update.message.reply_text(
        f"🙌 Submission received! You'll get {stars} Stars once approved (usually within 24h)."
    )

    if ADMIN_CHAT_ID:
        await update.get_bot().send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"📬 New bounty submission\nTier: {tier}\nUser: {chat_id}\nURL: {url}\n\nApprove with /approve <id>"
        )


def _is_admin(update) -> bool:
    return bool(ADMIN_CHAT_ID) and str(update.effective_chat.id) == ADMIN_CHAT_ID


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: list pending bounty submissions."""
    if not _is_admin(update):
        return
    rows = get_pending_rewards()
    if not rows:
        await update.message.reply_text("No pending submissions.")
        return
    msg = "\n\n".join(f"ID {r[0]} | {r[2]} | {r[1]}\n{r[3]}" for r in rows)
    await update.message.reply_text(f"📋 Pending:\n\n{msg}")


async def cmd_trends(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: show top scouted items this week for brands.py mining."""
    if not _is_admin(update):
        return
    rows = get_trends()
    if not rows:
        await update.message.reply_text("No scout data yet.")
        return
    lines = ["📊 <b>Top scouts this week:</b>\n"]
    for query, scouts, avg_price, buys in rows:
        pct = int((buys / scouts) * 100) if scouts else 0
        lines.append(f"• <b>{query}</b> — {scouts} scouts | avg £{avg_price:.2f} | {pct}% BUY")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_experts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: show pseudonymous expert users by buy rate."""
    if not _is_admin(update):
        return
    rows = get_expert_users()
    if not rows:
        await update.message.reply_text("No expert users identified yet.")
        return
    lines = ["🧠 <b>Expert Users (last 30 days):</b>\n"]
    for user_hash, total, avg_median, buy_rate in rows:
        lines.append(f"• <code>{user_hash}</code> — {total} scouts | avg £{avg_median} | {int(buy_rate*100)}% BUY")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: approve a bounty by ID."""
    if not _is_admin(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /approve <id>")
        return

    reward_id = int(context.args[0])
    chat_id, stars = approve_reward(reward_id)
    if not chat_id:
        await update.message.reply_text("ID not found or already approved.")
        return

    add_stars(chat_id, stars)
    await update.message.reply_text(f"✅ Approved. +{stars} Stars sent to {chat_id}.")
    await update.get_bot().send_message(
        chat_id=chat_id,
        text=f"🎉 Your bounty was approved! +{stars} Stars added to your balance.\nKeep spreading the word 🙌"
    )


if __name__ == "__main__":
    init_db()
    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("promote", cmd_promote))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("trends", cmd_trends))
    app.add_handler(CommandHandler("experts", cmd_experts))
    app.add_handler(CallbackQueryHandler(on_region_callback, pattern="^set_region_"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    log.info("Scout Stars bot running...")
    app.run_polling()
