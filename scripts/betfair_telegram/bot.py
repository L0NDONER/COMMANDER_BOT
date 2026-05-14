#!/usr/bin/env python3
"""
Main Telegram bot loop with PREFIX_COMMANDS dispatch.

Hardlink behavior (jellylink style):
- If TELEGRAM_CHAT_ID is numeric:
    - only accept that chat
    - always reply to that chat
- If TELEGRAM_CHAT_ID is empty / 'foobar' / non-numeric:
    - accept any chat (dev mode)
    - reply to the sender's chat
"""

from __future__ import annotations

import importlib
import logging
import sys
from typing import Callable, Dict, Optional

from bt_services.telegram.telegram_client import TelegramClient

config = importlib.import_module("config")

Handler = Callable[[object, TelegramClient, str, str], None]

from bt_services.betfair.betfair_service import (  # noqa: E402
    handle as betfair_handle,
    handle_lay_callback,
    handle_stake_reply,
)

PREFIX_COMMANDS: Dict[str, Handler] = {
    "betfair": betfair_handle,
}


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _parse_hardlink_chat_id() -> Optional[int]:
    """
    Returns numeric TELEGRAM_CHAT_ID if configured, else None (dev mode).
    """
    raw = str(getattr(config, "TELEGRAM_CHAT_ID", "")).strip()
    if not raw or raw.lower() == "foobar":
        return None
    try:
        return int(raw)
    except Exception:
        return None


def _is_allowed_chat(sender_chat_id: int, hardlink_chat_id: Optional[int]) -> bool:
    if hardlink_chat_id is None:
        return True
    return sender_chat_id == hardlink_chat_id


def _reply_chat_id(sender_chat_id: int, hardlink_chat_id: Optional[int]) -> int:
    """
    If hardlinked, reply to the configured chat id.
    Otherwise reply to the sender.
    """
    return hardlink_chat_id if hardlink_chat_id is not None else sender_chat_id


def _handle_builtin(
    telegram: TelegramClient,
    reply_chat_id: int,
    text: str,
    hardlink_chat_id: Optional[int],
) -> bool:
    parts = text.strip().split()
    if not parts:
        return False

    cmd = parts[0].lower()

    if cmd == "whoami":
        msg = f"chat_id={reply_chat_id}"
        if hardlink_chat_id is not None:
            msg += f" (hardlink={hardlink_chat_id})"
        telegram.send_message(str(reply_chat_id), msg)
        return True

    if cmd == "help":
        prefixes = ", ".join(sorted(PREFIX_COMMANDS.keys()))
        telegram.send_message(
            str(reply_chat_id),
            "\n".join(
                [
                    "Commands:",
                    "  whoami                 - show chat id",
                    "  help                   - show this help",
                    f"  <prefix> ...           - service dispatch ({prefixes})",
                    "",
                    "Example:",
                    "  betfair help",
                ]
            ),
        )
        return True

    return False


def main() -> int:
    configure_logging()
    log = logging.getLogger("bot")

    token = str(getattr(config, "TELEGRAM_BOT_TOKEN", "")).strip()
    if not token or token == "foobar":
        log.error("TELEGRAM_BOT_TOKEN is not set")
        return 2

    hardlink_chat_id = _parse_hardlink_chat_id()
    if hardlink_chat_id is None:
        log.info("TELEGRAM_CHAT_ID not set (dev mode): accepting any chat")
    else:
        log.info("TELEGRAM_CHAT_ID hardlinked to: %s", hardlink_chat_id)

    telegram = TelegramClient(token=token, timeout=30)

    offset = None
    log.info("Starting polling loop...")

    while True:
        try:
            updates = telegram.get_updates(
                offset=offset,
                timeout_seconds=int(getattr(config, "TELEGRAM_POLL_TIMEOUT_SECONDS", 50)),
            )

            for upd in updates:
                offset = upd.update_id + 1

                # Defensive: enforce int chat_id
                try:
                    sender_chat_id = int(upd.chat_id)
                except Exception:
                    log.warning("Skipping update with non-numeric chat_id=%r", upd.chat_id)
                    continue

                log.info("update sender_chat_id=%s text=%r", sender_chat_id, upd.text)

                if not _is_allowed_chat(sender_chat_id, hardlink_chat_id):
                    log.info(
                        "sender_chat_id=%s blocked (hardlink=%s)",
                        sender_chat_id,
                        hardlink_chat_id,
                    )
                    continue

                text = (upd.text or "").strip()
                if not text:
                    continue

                reply_id = _reply_chat_id(sender_chat_id, hardlink_chat_id)

                # Inline button press (callback_query)
                if upd.is_callback:
                    if upd.callback_data.startswith("lay_"):
                        handle_lay_callback(telegram, upd)
                    continue

                # Stake reply — intercept before normal routing
                if handle_stake_reply(telegram, str(reply_id), text):
                    continue

                # Built-ins first
                if _handle_builtin(telegram, reply_id, text, hardlink_chat_id):
                    continue

                prefix = text.split()[0].lower()
                handler = PREFIX_COMMANDS.get(prefix)
                if not handler:
                    telegram.send_message(str(reply_id), "Unknown command. Try: help")
                    continue

                handler(config, telegram, str(reply_id), text)

        except Exception as exc:  # noqa: BLE001
            log.exception("Polling loop error: %s", exc)

        telegram.sleep(float(getattr(config, "TELEGRAM_POLL_SLEEP_SECONDS", 1.0)))


if __name__ == "__main__":
    sys.exit(main())
