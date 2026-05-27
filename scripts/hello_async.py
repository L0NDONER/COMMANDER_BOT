import asyncio
import aiohttp
import xml.etree.ElementTree as ET

TOP_N = 5


async def fetch_rss(url, session, semaphore):
    """Fetch + parse one feed. Returns (url, [headlines]) or raises — the caller's
    gather(return_exceptions=True) isolates a bad feed from the good ones."""
    async with semaphore:
        print(f"...fetching {url}...")
        async with session.get(url) as response:
            response.raise_for_status()              # a 4xx/5xx body isn't XML
            content = await response.text()
        root = ET.fromstring(content)
        headlines = []
        for item in root.findall('.//item')[:TOP_N]:
            t = item.find('title')                   # some items have no <title>
            headlines.append(t.text if t is not None else "(no title)")
        return url, headlines


async def main():
    semaphore = asyncio.Semaphore(30)                # the throttle; slack at 1 URL
    urls = [
        "http://feeds.bbci.co.uk/news/business/rss.xml",
        "http://feeds.bbci.co.uk/news/technology/rss.xml",
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "http://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "http://feeds.bbci.co.uk/news/THIS-FEED-IS-DEAD/rss.xml",   # dud -> [FAIL], not a crash
    ]

    # One shared session across all fetches (pools connections) — not one per URL.
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(fetch_rss(u, session, semaphore) for u in urls),
            return_exceptions=True,                  # one dead feed won't sink the batch
        )

    # Print in main -> deterministic, input-order output regardless of who landed first.
    for url, result in zip(urls, results):
        if isinstance(result, Exception):
            detail = (f"{result.status} {result.message}"
                      if isinstance(result, aiohttp.ClientResponseError)
                      else f"{type(result).__name__}: {result}")
            print(f"\n[FAIL] {url}: {detail}")
            continue
        _, headlines = result
        print(f"\n--- Top {len(headlines)} from {url} ---")
        for title in headlines:
            print(f" > {title}")


if __name__ == "__main__":
    asyncio.run(main())
