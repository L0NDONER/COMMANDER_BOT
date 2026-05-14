#!/usr/bin/env python3
"""
Minimal Telegram client using getUpdates (long polling).

Why:
- No extra dependencies
- Works behind NAT
- Easy to run under systemd

Supports both plain text messages and inline keyboard callback_query updates.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests


@dataclass
class TelegramUpdate:
    update_id: int
    chat_id: int
    text: str
    # Callback query fields — populated when user presses an inline button
    is_callback: bool = False
    callback_query_id: str = ""
    callback_data: str = ""
    message_id: int = 0          # message the buttons were attached to


class TelegramClient:
    def __init__(self, token: str, timeout: int = 30) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._token = token
        self._base = f"https://api.telegram.org/bot{token}"
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: Optional[Dict] = None,
    ) -> Optional[int]:
        """
        Send a plain text message. Pass reply_markup for inline keyboards.
        Returns the sent message_id on success, None on failure.
        """
        url = f"{self._base}/sendMessage"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            resp = requests.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
            return data.get("result", {}).get("message_id")
        except Exception as exc:
            self._log.exception("Failed to send message: %s", exc)
            return None

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict] = None,
    ) -> None:
        """
        Edit an existing message in place — used after a button press
        to replace the keyboard with a result.
        """
        url = f"{self._base}/editMessageText"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            resp = requests.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except Exception as exc:
            self._log.exception("Failed to edit message: %s", exc)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        """
        Must be called after receiving a callback_query — clears the
        loading spinner on the button in the Telegram UI.
        """
        url = f"{self._base}/answerCallbackQuery"
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            resp = requests.post(url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except Exception as exc:
            self._log.exception("Failed to answer callback query: %s", exc)

    # ------------------------------------------------------------------
    # Inline keyboard builder helper
    # ------------------------------------------------------------------

    @staticmethod
    def inline_keyboard(buttons: List[List[Dict[str, str]]]) -> Dict:
        """
        Build a reply_markup dict for an inline keyboard.

        Usage:
            markup = TelegramClient.inline_keyboard([[
                {"text": "✅ Yes", "callback_data": "lay_yes|..."},
                {"text": "❌ No",  "callback_data": "lay_no"},
            ]])
            telegram.send_message(chat_id, "Place bet?", reply_markup=markup)
        """
        return {
            "inline_keyboard": buttons
        }

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def get_updates(
        self,
        offset: Optional[int],
        timeout_seconds: int,
    ) -> List[TelegramUpdate]:
        url = f"{self._base}/getUpdates"
        payload: Dict[str, Any] = {
            "timeout": timeout_seconds,
            # Request both plain messages and button presses
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset

        resp = requests.post(url, json=payload, timeout=timeout_seconds + 10)
        resp.raise_for_status()

        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getUpdates not ok: {data}")

        updates: List[TelegramUpdate] = []

        for item in data.get("result", []):
            update_id = int(item["update_id"])

            # ── Inline button press ──────────────────────────────────
            if "callback_query" in item:
                cq = item["callback_query"]
                msg = cq.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = int(chat.get("id", 0))
                message_id = int(msg.get("message_id", 0))

                updates.append(TelegramUpdate(
                    update_id=update_id,
                    chat_id=chat_id,
                    text="",
                    is_callback=True,
                    callback_query_id=str(cq.get("id", "")),
                    callback_data=str(cq.get("data", "")),
                    message_id=message_id,
                ))
                continue

            # ── Plain text message ───────────────────────────────────
            message = item.get("message") or {}
            chat = message.get("chat") or {}
            text = message.get("text") or ""

            if not text:
                continue

            updates.append(TelegramUpdate(
                update_id=update_id,
                chat_id=int(chat.get("id", 0)),
                text=str(text),
            ))

        return updates

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)
