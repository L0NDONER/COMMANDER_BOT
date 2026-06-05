#!/usr/bin/env python3
import asyncio
import json
import math
import statistics
import websockets
from datetime import datetime

BINANCE_WS = "wss://stream.binance.com:9443/stream"

STREAMS = [
    "btcusdt@aggTrade",
    "solusdt@aggTrade",
]

BUCKET_SEC = 1
WINDOW = 60
BASELINE_RATIO = 64.0 / 60810.0  # example from your earlier snapshot


class BucketState:
    def __init__(self):
        self.current_sec = None
        self.btc_prices = []
        self.sol_prices = []
        self.last_btc: float | None = None
        self.last_sol: float | None = None
        self.history = []  # list of (t, btc_mid, sol_mid, ratio)

    def add_trade(self, symbol, price, ts_ms):
        t_sec = ts_ms // 1000
        if self.current_sec is None:
            self.current_sec = t_sec

        if t_sec != self.current_sec:
            self.flush_bucket()
            self.current_sec = t_sec
            self.btc_prices = []
            self.sol_prices = []

        if symbol == "BTCUSDT":
            self.btc_prices.append(price)
        elif symbol == "SOLUSDT":
            self.sol_prices.append(price)

    def flush_bucket(self):
        btc_mid = statistics.mean(self.btc_prices) if self.btc_prices else self.last_btc
        sol_mid = statistics.mean(self.sol_prices) if self.sol_prices else self.last_sol
        if btc_mid is None or sol_mid is None:
            return
        self.last_btc = btc_mid
        self.last_sol = sol_mid
        ratio = sol_mid / btc_mid
        self.history.append((self.current_sec, btc_mid, sol_mid, ratio))

        if len(self.history) > WINDOW:
            self.history.pop(0)

        self.report()

    def report(self):
        if len(self.history) < 2:
            return

        ts    = datetime.fromtimestamp(self.history[-1][0]).strftime("%H:%M:%S")
        btc   = self.history[-1][1]
        sol   = self.history[-1][2]
        ratio = self.history[-1][3]

        gap = (ratio - BASELINE_RATIO) / BASELINE_RATIO * 100.0

        if len(self.history) >= 3:
            corr = pearson([h[1] for h in self.history],
                           [h[2] for h in self.history])
        else:
            corr = float("nan")

        if len(self.history) >= 3:
            r1, r2, r3 = (self.history[-3][3],
                          self.history[-2][3],
                          self.history[-1][3])
            curv = (r3 - r2) - (r2 - r1)
        else:
            curv = 0.0

        print(
            f"{ts}  BTC={btc:9.2f}  SOL={sol:7.3f}  "
            f"gap={gap:+7.4f}%  corr={corr:6.3f}  curv={curv:+.3e}"
        )


def pearson(xs, ys):
    if len(xs) != len(ys) or len(xs) < 2:
        return float("nan")
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    deny = math.sqrt(sum((y - my) ** 2 for y in ys))
    if denx == 0 or deny == 0:
        return float("nan")
    return num / (denx * deny)


async def main():
    url = f"{BINANCE_WS}?streams={'/'.join(STREAMS)}"
    state = BucketState()

    async for ws in websockets.connect(url, ping_interval=20, ping_timeout=20):
        try:
            async for msg in ws:
                data    = json.loads(msg)
                payload = data.get("data", {})

                if not payload or payload.get("e") != "aggTrade":
                    continue

                s = payload.get("s")
                p = float(payload.get("p"))
                t = int(payload.get("T"))

                if s not in ("BTCUSDT", "SOLUSDT"):
                    continue

                state.add_trade(s, p, t)

        except websockets.ConnectionClosed:
            print("WebSocket disconnected, reconnecting…")
            continue
        except Exception as e:
            print("Error:", e)
            continue


if __name__ == "__main__":
    asyncio.run(main())
