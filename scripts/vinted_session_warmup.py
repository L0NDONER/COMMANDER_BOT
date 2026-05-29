"""Vinted session warmup — prime cookies via the homepage before fanning out
to the catalog API.

Issues a single GET to vinted.co.uk so aiohttp's cookie jar picks up the
session cookies the catalog endpoint expects on subsequent calls, then
runs N parallel catalog searches reusing that session with a Referer back
to the homepage.

Prints captured cookie names and per-request status/latency so the session
behaviour can be inspected.
"""

import asyncio
import random
import time

import aiohttp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HOME = "https://www.vinted.co.uk/"
SEARCH_TERMS = ["nike", "zara", "uniqlo", "carhartt", "north face"]
CONCURRENCY = 3
JITTER = (1.0, 3.0)


async def warm_scout(session):
    t0 = time.perf_counter()
    async with session.get(HOME) as r:
        await r.read()
        dt = time.perf_counter() - t0
        print(f"[scout] {r.status} in {dt:.2f}s")
    cookies = sorted(c.key for c in session.cookie_jar)
    print(f"[scout] baked cookies: {cookies or '(none)'}")
    return bool(cookies)


async def fetch_search(session, sem, term):
    url = f"https://www.vinted.co.uk/catalog?search_text={term}"
    async with sem:
        await asyncio.sleep(random.uniform(*JITTER))
        t0 = time.perf_counter()
        try:
            async with session.get(url, headers={"Referer": HOME}, timeout=15) as r:
                body = await r.read()
                dt = time.perf_counter() - t0
                return term, r.status, len(body), dt, None
        except asyncio.TimeoutError:
            dt = time.perf_counter() - t0
            return term, None, 0, dt, "timeout"
        except aiohttp.ClientError as e:
            dt = time.perf_counter() - t0
            return term, None, 0, dt, f"client: {e!r}"


async def main():
    sem = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
        print("--- warming scout ---")
        if not await warm_scout(session):
            print("[!] no cookies baked — Vinted may be blocking from this IP")
        await asyncio.sleep(2)

        print(f"\n--- fan-out x{len(SEARCH_TERMS)} (concurrency={CONCURRENCY}) ---")
        results = await asyncio.gather(
            *(fetch_search(session, sem, t) for t in SEARCH_TERMS)
        )

    print("\n--- results ---")
    ok = 0
    for term, status, size, dt, err in results:
        if err:
            print(f"  {term:<12} FAIL {dt:.2f}s  {err}")
        else:
            tag = "OK " if status == 200 else "BAD"
            print(f"  {term:<12} {tag} {status} {size/1024:>6.1f}KB {dt:.2f}s")
            if status == 200:
                ok += 1
    print(f"\n{ok}/{len(results)} succeeded")


if __name__ == "__main__":
    asyncio.run(main())
