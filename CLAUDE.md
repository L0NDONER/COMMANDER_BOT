# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Deployment

- Runs in Docker on EC2 (SSH alias `aws`, user `ubuntu`, path `~/commander`).
- Local repo is source of truth. EC2 is a clean git clone of `origin/main`.
- **Auto-deploy:** push to `main` → GitHub Actions (`.github/workflows/deploy.yml`) SSHes to EC2, pulls, and runs `docker compose up -d --build`. ~1m30s per deploy.
- Manual fallback: `ssh aws "cd commander && git pull && docker compose up -d --build"`.
- If only `services/ebay/*` changed and you want zero-downtime: `docker compose restart commander-leader` is enough (volume-mounted), but auto-deploy always does a full rebuild.
- Do **not** edit files directly on EC2 — that workflow was retired 2026-05-14.

## Gitignored, EC2-only files

- `credentials.py` — API keys (Telegram, Groq, eBay, Gemini).
- `services/ebay/brands.py` — proprietary brand lists: `STRONG_BRANDS`, `SLOW_KEYWORDS`, `is_low_value`, `handle_brands`, `get_brand_tip`.

Never `git add` either. Never invent placeholder content — imports must keep resolving against the real EC2 copies.

## Build / run

```bash
docker compose up -d --build               # full rebuild
docker compose restart commander-leader    # pick up edits to services/ebay/*
docker compose logs -f commander-leader    # tail bot output
```

Volume-mounted (restart only): `services/ebay/`.
Requires `--build`: `telegram_app.py`, `requirements.txt`, `Dockerfile`, anything else at repo root.

Local non-Docker run (legacy text scout, not photo pipeline):
```bash
pip3 install -r requirements.txt
python3 telegram_app.py
```

## Containers

| Container | Role | Entrypoint |
|---|---|---|
| `commander-leader` | Telegram bot, photo intake, consensus orchestrator | `telegram_app.py` |
| `commander-worker-1`, `commander-worker-2` | Pricing voters | `python3 -m services.ebay.worker` |
| `redis` | Pub/sub + cache | `redis:alpine` |

## Architecture — photo pricing pipeline

Photo+price → Vinted resale verdict. Spans three containers via Redis:

1. **Telegram** (`telegram_app.py:handle_photo`) — downloads photo to `/tmp`, calls `evaluate_with_consensus(image_path, caption)`.
2. **Vision** (`services/ebay/scout_vision.py:identify_item`) — `_scan_barcode` first (pyzbar → Open Library for ISBN, Open Food Facts for UPC). On miss, Gemini (`gemini-3-flash-preview`) with `IDENTIFY_PROMPT`. Returns `(query, keywords)` or raises `ValueError("NOT_FOUND")`.
3. **Fan-out** (`services/ebay/scout_update.py:evaluate_with_consensus`) — leader md5-hashes image, caches vision result in Redis (`vision:{md5}`), publishes task to `scout_tasks` pubsub with `img_hash` and `base_query`, casts own vote.
4. **Worker vote** (`services/ebay/worker.py`) — subscribes to `scout_tasks`. With `ANARCHY_MODE=true`, mutates query via `diversify_query` per `WORKER_INDEX`. Fetches eBay median via `get_stats`, writes vote into `votes:{img_hash}` hash.
5. **Consensus** — leader polls `votes:{img_hash}` for up to `CONSENSUS_TIMEOUT_SECONDS` until `CONSENSUS_REQUIRED` votes. Median-of-medians wins; closest worker is "winner."
6. **Public feed** — on BUY verdict, `web_feed.update_web_feed` writes `/var/www/html/feed/feed.json` (Nginx-served). **Only item name + profit. No user IDs, chat IDs, or identifiers.**

eBay API: `api.ebay.com/buy/browse/v1/item_summary/search`, marketplace `EBAY_GB`, condition filter `3000|4000|5000` (used). Token cached in Redis under `ebay_token`, stats under `stats:{query.lower()}`.

## Architecture — legacy text scout

`services/ebay/handler.py:handle_scout_command` is the older `scout <item> [£price]` text interface. Calls `get_stats` + `verdict` directly — no consensus, no workers, no vision. Kept for README-documented commands. Do not confuse with `evaluate_with_consensus`.

## Other services (independent of eBay pipeline)

- `services/local_scout/` — standalone daemon polling eBay watchlist. Not in Docker.
- `services/garden/` — vision-based clearance volume estimator. Different prompt, different return shape.
- `services/betfair_telegram/` — Betfair lay-trader bots, standalone.
- `services/vision/blink_bridge.py` — Blink camera integration.

When working on the eBay pipeline, skip `grep`/`find` across `local_scout/`, `garden/`, `betfair_telegram/`, `vision/` — they're independent.

## Conventions

- Vision model: Gemini `gemini-3-flash-preview`. Chat fallback: Groq Llama 3.3 70B.
- Currency: GBP throughout.
- Vinted discount (eBay→Vinted price ratio): `DEFAULT_VINTED_DISCOUNT = 0.72`. Some code uses `0.50` — check the file being edited.
- Redis keys: `vision:{md5}`, `votes:{img_hash}`, `stats:{query}`, `ebay_token`, `wallet:{replica}`.
- `ANARCHY_MODE` env var (default true) enables per-worker query diversification.
