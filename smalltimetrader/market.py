import asyncio
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
import httpx


@dataclass
class Quote:
    source: str
    timestamp: float
    price: float  # USD, explicitly normalised
    bid: float
    ask: float
    raw: float  # original quote currency


class MarketData:
    def __init__(self, client=None):
        self.client = client or httpx.AsyncClient(timeout=10)

    async def get(self, url, params=None):
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def kraken_result(self, endpoint, pair):
        data = await self.get("https://api.kraken.com/0/public/" + endpoint, {"pair": pair})
        if data.get("error"):
            raise ValueError("Kraken error")
        return data["result"]

    async def kraken(self, pair, source="kraken"):
        ticker, trades = await asyncio.gather(self.kraken_result("Ticker", pair), self.kraken_result("Trades", pair))
        tick = next(iter(ticker.values()))
        rows = next(v for k, v in trades.items() if k != "last")
        last = rows[-1]
        return Quote(source, float(last[2]), float(last[0]), float(tick["b"][0]), float(tick["a"][0]), float(last[0]))

    async def binance(self, symbol, fx):
        trades, book = await asyncio.gather(self.get("https://api.binance.com/api/v3/trades", {"symbol": symbol, "limit": 1}), self.get("https://api.binance.com/api/v3/ticker/bookTicker", {"symbol": symbol}))
        t = trades[-1]
        return Quote("binance", t["time"]/1000, float(t["price"])*fx, float(book["bidPrice"])*fx, float(book["askPrice"])*fx, float(t["price"]))

    async def coinbase(self, symbol):
        t = await self.get("https://api.exchange.coinbase.com/products/" + symbol + "/ticker")
        return Quote("coinbase", datetime.fromisoformat(t["time"].replace("Z", "+00:00")).timestamp(), float(t["price"]), float(t["bid"]), float(t["ask"]), float(t["price"]))

    async def collect(self, coin, config):
        # USDT/USD is measured, not assumed to be 1:1.
        fx = await self.kraken("USDTUSD", "fx")
        if abs(time.time()-fx.timestamp) > config.max_age_seconds or not 0.8 < fx.price < 1.2:
            raise ValueError("USDT/USD conversion unavailable or stale")
        result = await asyncio.gather(self.binance(coin.binance, fx.price), self.coinbase(coin.coinbase), self.kraken(coin.kraken), return_exceptions=True)
        if any(isinstance(q, BaseException) for q in result):
            raise ValueError("A required exchange is unavailable")
        return result


def validate(quotes, config, now):
    if len(quotes) != 3 or {q.source for q in quotes} != {"binance", "coinbase", "kraken"}:
        raise ValueError("Three distinct sources required")
    import math
    for q in quotes:
        if not all(math.isfinite(v) and v > 0 for v in [q.price, q.raw, q.bid, q.ask, q.timestamp]):
            raise ValueError("Invalid quote")
        if not 0 <= now-q.timestamp <= config.max_age_seconds or q.bid > q.ask:
            raise ValueError("Stale, future or crossed quote")
    prices = [q.price for q in quotes]
    if (max(prices)-min(prices))/statistics.median(prices)*100 > config.max_disagreement_pct:
        raise ValueError("Exchanges disagree")


def signal(changes, threshold, inclusive=False):
    """Median change must cross threshold AND at least two sources must cross it."""
    if len(changes) != 3:
        return 0
    middle = statistics.median(changes)
    crossed = abs(middle) >= threshold if inclusive else abs(middle) > threshold
    if not crossed:
        return 0
    direction = 1 if middle > 0 else -1
    agreeing = sum((x*direction >= threshold if inclusive else x*direction > threshold) for x in changes)
    return direction if agreeing >= 2 else 0
