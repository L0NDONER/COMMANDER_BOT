#!/usr/bin/env python3
"""Consensus worker: subscribes to scout_tasks, casts one vote per task."""

import json
import logging
import os
import socket

from services.ebay.scout_update import (
    ANARCHY_MODE,
    cast_vote,
    diversify_query,
    get_redis,
    get_stats,
)

REPLICA = socket.gethostname()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
LOGGER = logging.getLogger(f"worker.{REPLICA}")


def handle_task(task: dict) -> None:
    img_hash = task.get("img_hash")
    base_query = task.get("base_query")
    condition = task.get("condition", "used")
    if not img_hash or not base_query:
        LOGGER.warning("Malformed task, skipping: %s", task)
        return
    query = diversify_query(base_query, REPLICA, condition) if ANARCHY_MODE else base_query
    LOGGER.info("Search %r (cond=%s) for img %s", query, condition, img_hash[:8])
    stats = get_stats(query, REPLICA, condition)
    if "median" in stats:
        cast_vote(img_hash, REPLICA, stats["median"], query)
        LOGGER.info("Voted median=%.2f", stats["median"])
    else:
        LOGGER.info("No data for %r, skipping vote", query)


def run_worker() -> None:
    pubsub = get_redis().pubsub()
    pubsub.subscribe("scout_tasks")
    LOGGER.info("Worker %s subscribed to scout_tasks", REPLICA)
    for msg in pubsub.listen():
        if msg.get("type") != "message":
            continue
        try:
            task = json.loads(msg["data"])
        except json.JSONDecodeError:
            LOGGER.warning("Bad JSON on scout_tasks: %r", msg.get("data"))
            continue
        try:
            handle_task(task)
        except Exception:
            LOGGER.exception("handle_task failed for %s", task)


if __name__ == "__main__":
    run_worker()
