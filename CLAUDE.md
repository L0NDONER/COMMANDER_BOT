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

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

- `tests/conftest.py` stubs EC2-only modules (`credentials`, `services.ebay.brands`, `scout_vision`) so the suite runs without those deps installed.
- CI gates deploys: the `test` job in `.github/workflows/deploy.yml` runs first; the `deploy` job has `needs: test`. Broken code cannot reach EC2.
- Pull requests to `main` run tests but do not deploy.

Volume-mounted (restart only): `services/ebay/`.
Requires `--build`: `telegram_app.py`, `requirements.txt`, `Dockerfile`, anything else at repo root.

Local non-Docker run:
```bash
pip3 install -r requirements.txt
python3 telegram_app.py
```

## Containers

Single container — `commander-leader` (`telegram_app.py`). The old Redis fan-out and `commander-worker-*` containers were stripped in commits b7515a3 / f72b523.

## Architecture — photo pricing pipeline

Photo+price → Vinted resale verdict. Single process, all in-memory:

1. **Telegram** (`telegram_app.py:handle_photo`) — downloads photo to `/tmp`, calls `evaluate_with_consensus_saas(image_path, caption)`.
2. **Vision** (`services/ebay/scout_vision.py:identify_item`) — `_scan_barcode` first (pyzbar → Open Library for ISBN, Open Food Facts for UPC). On miss, Gemini (`gemini-3-flash-preview`) with `IDENTIFY_PROMPT`. Returns `(query, keywords)` or raises `ValueError("NOT_FOUND")`.
3. **Consensus** (`services/ebay/scout_async.py:evaluate_with_consensus_saas`) — md5-hashes image, caches vision result via `database.set_cached_value("vision:{md5}", ...)`, builds 5 query variants (base / used|new / mens / womens / vintage), fans out to `asyncio.gather` with `CONSENSUS_TIMEOUT_SECONDS` timeout. Needs `MIN_VOTES_FOR_CONSENSUS` (2) successful medians. Median-of-medians wins; the variant closest to that median is logged as `winner=#N`.
4. **Public feed** — on BUY verdict, `web_feed.update_web_feed` writes `/var/www/html/feed/feed.json` (Nginx-served). **Only item name + profit. No user IDs, chat IDs, or identifiers.**

eBay API: `api.ebay.com/buy/browse/v1/item_summary/search`, marketplace `EBAY_GB`, condition filter `3000|4000|5000` (used). Token + stats cached in the SQLite-backed `database` module under `ebay_token` and `stats:{condition}:{query.lower()}`.

## Standalone scripts (not deployed)

Everything under `scripts/` is checked-in but not part of the Docker deploy. The Dockerfile copies them in but `telegram_app.py` doesn't import them at runtime.

- `scripts/local_scout/` — standalone daemon polling eBay watchlist.
- `scripts/garden/` — vision-based clearance volume estimator. Different prompt, different return shape.
- `scripts/betfair_telegram/` — Betfair lay-trader bots.
- `scripts/vision/blink_bridge.py` — Blink camera integration.

When working on the eBay pipeline, skip `grep`/`find` across `scripts/` — independent.

## Conventions

- Vision model: Gemini `gemini-3-flash-preview`. Chat fallback: Groq Llama 3.3 70B.
- Currency: GBP throughout.
- Vinted discount (eBay→Vinted price ratio): `DEFAULT_VINTED_DISCOUNT = 0.72`.
- Cache keys (SQLite via `database` module): `vision:{md5}`, `stats:{condition}:{query}`, `ebay_token`.
