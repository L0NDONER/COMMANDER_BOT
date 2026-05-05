"""Atomic, lock-serialized writer for the public scan feed at /var/www/html/feed.json."""

import fcntl
import json
import logging
import os

FEED_PATH = "/var/www/html/feed/feed.json"
LOCK_PATH = f"{FEED_PATH}.lock"
MAX_ENTRIES = 5

LOGGER = logging.getLogger(__name__)


def update_web_feed(item_name: str, profit_amount) -> None:
    temp_path = f"{FEED_PATH}.tmp"
    with open(LOCK_PATH, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = []
            if os.path.exists(FEED_PATH):
                try:
                    with open(FEED_PATH, "r") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, IOError):
                    data = []

            data.insert(0, {"label": item_name, "profit": f"+£{profit_amount}"})
            data = data[:MAX_ENTRIES]

            with open(temp_path, "w") as tf:
                json.dump(data, tf)
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(temp_path, FEED_PATH)
        except Exception:
            LOGGER.exception("Feed update failed")
            if os.path.exists(temp_path):
                os.remove(temp_path)
