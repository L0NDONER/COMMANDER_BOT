# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Deployment model

The bot runs in Docker on an EC2 host. **The deployed code on EC2 routinely drifts from this repo** — the operator edits files directly over SSH and does not always commit back. Treat `git log` as a lower bound on what's live. Before making non-trivial changes, ask whether to apply them locally, on EC2, or both. The Python heredoc / `sed -i` pattern is the usual way to patch on EC2.

Two files are gitignored and only exist on EC2:
- `credentials.py` (root) — API keys (Telegram, Groq, eBay, Gemini)
- `services/ebay/brands.py` — proprietary brand lists (`STRONG_BRANDS`, `SLOW_KEYWORDS`, `is_low_value`, `handle_brands`, `get_brand_tip`)

Never `git add` either, and never invent placeholder content for them — code imports must keep working against the real EC2 copies.

## Run / build

```bash
docker compose up -d --build         # full rebuild
docker compose restart commander-leader   # pick up volume-mounted edits to services/ebay/* or scout_vision.py
docker compose logs -f commander-leader   # tail bot output
```

Container layout (`docker-compose.yml`):
- `commander-leader` — runs `telegram_app.py`, handles photo uploads from Telegram
- `commander-worker-1`, `commander-worker-2` — run `python3 -m services.ebay.worker`, cast pricing votes
- `redis` — message bus + cache

`services/ebay/` and `scout_vision.py` are volume-mounted, so edits to those files only need a restart, not a rebuild. Anything else (e.g. `telegram_app.py`, `requirements.txt`, Dockerfile) needs `--build`.

Local non-Docker run (legacy text-mode scout, not the photo bot):
```bash
pip3 install -r requirements.txt
python3 telegram_app.py
```

## Architecture — photo pricing flow

The flagship feature is the photo+price → Vinted resale verdict pipeline. It runs across three containers coordinating via Redis. Understanding this fan-out is the main thing that requires reading multiple files:

1. **Telegram entry** (`telegram_app.py`) — user sends photo with caption (buy price). `handle_photo` downloads to `/tmp` and calls `evaluate_with_consensus(image_path, caption)`.
2. **Vision identify** (`scout_vision.py` at repo root, mounted to `/app/scout_vision.py`) — `identify_item` tries `_scan_barcode` first (pyzbar → Open Library for ISBN, Open Food Facts for UPC). On miss, falls back to Gemini (`gemini-3-flash-preview`) with `IDENTIFY_PROMPT`. Returns `(query, keywords)` or raises `ValueError("NOT_FOUND")`.
3. **Task fan-out** (`services/ebay/scout_update.py:evaluate_with_consensus`) — leader hashes the image, caches the vision result in Redis (`vision:{md5}`), then publishes a task to the `scout_tasks` pubsub channel with `img_hash` and `base_query`. Leader also casts its own vote.
4. **Worker voting** (`services/ebay/worker.py`) — workers subscribe to `scout_tasks`. Each worker mutates the query via `diversify_query` (different suffixes per `WORKER_INDEX`) when `ANARCHY_MODE=true`, fetches eBay median via `get_stats`, and writes a vote into the `votes:{img_hash}` hash.
5. **Consensus** — leader polls `votes:{img_hash}` for up to `CONSENSUS_TIMEOUT_SECONDS` until `CONSENSUS_REQUIRED` votes land. Median of medians wins; worker closest to it is "winner."
6. **Publish to public feed** — if verdict contains BUY, `web_feed.update_web_feed` writes to `/var/www/html/feed/feed.json` (Nginx-served). **Only item name + profit go in this file — no user IDs, chat IDs, or other identifiers.**

`scout_vision.py` exists at two paths. The repo-root copy is what the container imports (mounted as `/app/scout_vision.py`); `services/ebay/scout_vision.py` exists but is not the one in use. Edit the root copy.

The eBay token is cached in Redis under `ebay_token`, statistics under `stats:{query.lower()}`. eBay calls go to `api.ebay.com/buy/browse/v1/item_summary/search` with marketplace `EBAY_GB` and condition filter `3000|4000|5000` (used items).

## Architecture — legacy text scout

`services/ebay/handler.py:handle_scout_command` is the older text-based `scout <item> [£price]` interface. It calls `get_stats` + `verdict` directly (no consensus, no workers, no vision). Kept for the README's documented commands. Don't confuse it with `evaluate_with_consensus`.

## Other services

- `services/local_scout/` — standalone daemon polling eBay watchlist (`local_scout.watchlist`) for deals; runs separately, not in Docker.
- `services/garden/` — vision-based clearance volume estimator (different prompt, different return shape).
- `services/betfair_telegram/` — Betfair lay-trader bots, standalone.
- `services/vision/blink_bridge.py` — Blink camera integration.

These are independent of the eBay pipeline; touching one rarely affects the others.

## Conventions

- All AI model defaults: Gemini for vision (`gemini-3-flash-preview`), Groq Llama 3.3 70B for chat fallback.
- Currency is GBP throughout. Vinted discount (eBay→Vinted price ratio) is `DEFAULT_VINTED_DISCOUNT = 0.72`; some code uses `0.50` — check the file you're editing.
- Redis keys: `vision:{md5}`, `votes:{img_hash}`, `stats:{query}`, `ebay_token`, `wallet:{replica}`.
- `ANARCHY_MODE` env var (default true) enables query diversification across workers.
