#!/usr/bin/env python3

"""
Safety belt for Commander/Minty.

Design goals:
- Additive only: does not modify command logic, only wraps execution.
- Quiet by default: only emits a "FATAL" style message after repeated failures.
- Rate-limited: once we alert, we stay quiet for a cooldown window.
- Restart-safe: persists state so a crash loop doesn't spam WhatsApp.

Tradeoffs:
- Uses a small JSON state file (simple + reliable).
- "Fatal" is defined as repeated unhandled exceptions in the request path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class SafetyConfig:
    state_path: str = "/var/lib/commander/safety_state.json"
    consecutive_fail_threshold: int = 3
    alert_cooldown_seconds: int = 30 * 60  # 30 minutes


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        return {}
    except Exception:
        logging.exception("Failed to load safety state file")
    return {}


def _save_state(path: str, state: Dict[str, Any]) -> None:
    try:
        _ensure_parent_dir(path)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except Exception:
        logging.exception("Failed to save safety state file")


def safe_execute(
    func: Callable[[], str],
    *,
    cfg: SafetyConfig,
    context: Optional[Dict[str, str]] = None,
) -> str:
    """
    Execute request handler safely.

    Returns:
    - normal reply on success
    - a quiet "temporary issue" message on single failure
    - a "FATAL" message (rate-limited) on repeated failures
    """
    context = context or {}
    now = int(time.time())

    state = _load_state(cfg.state_path)
    consecutive_failures = int(state.get("consecutive_failures", 0))
    last_alert_ts = int(state.get("last_alert_ts", 0))

    try:
        reply = func()
        # Success resets failure count
        if consecutive_failures != 0:
            state["consecutive_failures"] = 0
            _save_state(cfg.state_path, state)
        return reply

    except Exception as exc:
        logging.exception("Unhandled error in request path", extra={"context": context})

        consecutive_failures += 1
        state["consecutive_failures"] = consecutive_failures

        cooldown_ok = (now - last_alert_ts) >= cfg.alert_cooldown_seconds
        fatal = consecutive_failures >= cfg.consecutive_fail_threshold and cooldown_ok

        if fatal:
            state["last_alert_ts"] = now
            _save_state(cfg.state_path, state)
            return (
                "Minty: 🚨 FATAL: I hit repeated errors and may be degraded.\n"
                "Try: status\n"
                "If still broken: check service logs / restart."
            )

        _save_state(cfg.state_path, state)
        # Quiet failure response (no details leaked)
        return "Minty: Brain fog... 🧠 (temporary issue)"

