# CLAUDE.md

## Deployment

- Runs in Docker on EC2 (SSH alias `aws`, user `ubuntu`, path `~/commander`).
- Local repo is source of truth. EC2 is a clean git clone of `origin/main`.
- **Auto-deploy:** push to `main` → GitHub Actions (`.github/workflows/deploy.yml`) SSHes to EC2, pulls, and runs `docker compose up -d --build`. ~1m30s per deploy.
- Manual fallback: `ssh aws "cd commander && git pull && docker compose up -d --build"`.
- If only `services/market/*` changed and you want zero-downtime: `docker compose restart commander-leader` is enough (volume-mounted), but auto-deploy always does a full rebuild.
- Do **not** edit files directly on EC2 — that workflow was retired 2026-05-14.

## Gitignored, EC2-only files

- `credentials.py` — API keys (Telegram, Groq, market, Gemini).
- `services/market/brands.py` — proprietary brand lists: `STRONG_BRANDS`, `SLOW_KEYWORDS`, `is_low_value`, `handle_brands`, `get_brand_tip`.
- `services/market/consensus_engine.py` — consensus orchestration: `MIN_VOTES_FOR_CONSENSUS`, `build_variants`, `gather_votes`. Stubbed in `tests/conftest.py` so CI passes without it.

Never `git add` any of these. Never invent placeholder content — imports must keep resolving against the real EC2 copies.

## Build / run

- Full rebuild: `docker compose up -d --build`
- Pick up `services/market/*` edits: `docker compose restart commander-leader`
- Tail logs: `docker compose logs -f commander-leader`
- Local non-Docker: `pip3 install -r requirements.txt && python3 telegram_app.py`

## Tests

- `pip install -r requirements-dev.txt && pytest tests/ -v`
- `tests/conftest.py` stubs EC2-only modules (`credentials`, `services.market.brands`, `scout_vision`).
- CI gates deploys: `test` job runs first; `deploy` job has `needs: test`.

Volume-mounted (restart only): `services/market/` and `services/site/`.
Requires `--build`: `telegram_app.py`, `requirements.txt`, `Dockerfile`, anything else at repo root.

## Containers

Single container — `commander-leader` (`telegram_app.py`).

## Architecture — photo pricing pipeline

Photo+price → site resale verdict. Single process, all in-memory:

1. **Telegram** (`telegram_app.py:handle_photo`) — downloads photo to `/tmp`, calls `evaluate_with_consensus_saas(image_path, caption)`.
2. **Vision** (`services/market/scout_vision.py:identify_item`) — `_scan_barcode` first (pyzbar → Open Library for ISBN, Open Food Facts for UPC). On miss, Gemini (`gemini-3-flash-preview`) with `IDENTIFY_PROMPT`. Returns `(query, keywords)` or raises `ValueError("NOT_FOUND")`.
3. **Consensus** (`services/market/scout_async.py:evaluate_with_consensus_saas`) — md5-hashes image, caches vision result via `database.set_cached_value("vision:{md5}", ...)`, builds ≤2 query variants via `build_variants` (base anchor + 1 suffix backup, e.g. used/new or a vision keyword), fans out 5 replicas per variant to `asyncio.gather` with `CONSENSUS_TIMEOUT_SECONDS` timeout. Needs `MIN_VOTES_FOR_CONSENSUS` (2) successful votes overall. `gather_votes` tags each vote with `variant_idx` (0 = base) by construction, not by matching an echoed query string. `_score` (`scout_update.py`) then applies a fallback-ordering ladder: if the base bucket (`variant_idx == 0`) has ≥ `MIN_BUCKET_VOTES` (3) votes, `verdict_median` is the median of the base bucket alone — the suffix bucket never influences price. Otherwise it pools base + suffix votes together. This keeps the suffix variant a pure availability fallback (per its own docstring), not a co-equal pricing input; the variant closest to `verdict_median` is logged as `winner=#N`.
4. **Public feed** — on BUY verdict, `web_feed.update_web_feed` writes `/var/www/html/feed/feed.json` (Nginx-served). **Only item name + profit. No user IDs, chat IDs, or identifiers.**

Market API: `api.ebay.com/buy/browse/v1/item_summary/search`, marketplace `EBAY_GB`, condition filter `3000|4000|5000` (used). Token + stats cached in the SQLite-backed `database` module under `market_token` and `stats:{condition}:{query.lower()}`.

## Consensus algorithm — confidence-weighted (proposed, not yet implemented)

Current: `consensus_engine.py` does median-of-medians over variant votes (see Architecture §3). No confidence weighting.

Proposed upgrade, gated on real per-variant confidence:

```python
def consensus(variants, ctx):
    """Run variants against ctx; return -1/0/+1 weighted by confidence."""
    votes = [(sign, conf) for sign, conf in
             (v(ctx) for _, v in variants) if sign is not None]
    if not votes:
        return 0
    score = sum(sign * conf for sign, conf in votes) / len(votes)
    return -1 if score <= -0.5 else 1 if score >= 0.5 else 0
```

- eBay variants (`services/market/`): can compute real confidence from measurable structure — sold-listing count, price variance, recency, outlier fraction, demand spikes, category volatility. Sketch:

```python
def ebay_confidence(sold_listings, price_variance, recency_days,
                     outlier_frac, demand_spike, category_volatility):
    n = min(sold_listings / 10, 1.0)
    spread = max(0.0, 1.0 - price_variance)
    fresh = max(0.0, 1.0 - recency_days / 90)
    clean = 1.0 - outlier_frac
    stable = 1.0 - demand_spike
    cat = 1.0 - category_volatility
    return sum([n, spread, fresh, clean, stable, cat]) / 6
```

- Vinted variants (`services/site/`): underlying data can't support a real confidence number. Must default to `conf = 1.0` (flat, equivalent to today's unweighted vote) — never a fabricated intermediate value. Fabricated confidence isn't noise that cancels out, it's structured bias toward whichever variants got invented numbers.
- Net effect: upgrade can only help. Variants with real structure get to speak louder when confident; variants without it sit at today's baseline.
- Before implementing: confirm against a case log that median vs. weighted-sum actually flips a real decision — don't ship on the strength of the argument alone.

## Web app (flaz.co.uk)

- `web_app.py` — FastAPI app serving the frontend and API endpoints.
- `web/index.html` — single-page PWA. All CSS/JS inline, no build step.
- `web/manifest.json` — PWA manifest (standalone display, icons, scope).
- `web/icons/` — PWA icons (192, 512) and OG image. Served via `/icons/{name}` route.
- Static files are served by explicit routes, not a catch-all static mount.
- `POST /api/evaluate` — photo + price → consensus verdict (same pipeline as Telegram).
- `POST /api/log-buy` — logs a buy decision to the database.
- Lighthouse scores: 100 across performance, accessibility, best practices, SEO.

## scripts/

Checked in, not deployed. When working on the market pipeline, skip `grep`/`find` across `scripts/` — independent.

## Conventions

- Vision model: Gemini `gemini-3-flash-preview`. Chat fallback: Groq Llama 3.3 70B.
- Currency: GBP throughout.
- site discount (market→site price ratio): `DEFAULT_SITE_DISCOUNT = 0.75`, `STRONG_BRAND_DISCOUNT = 0.80` for `STRONG_BRANDS`.
- Cache keys (SQLite via `database` module): `vision:{md5}`, `stats:{condition}:{query}`, `market_token`.
