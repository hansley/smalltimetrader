import hashlib
import hmac
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from urllib.parse import urlencode
import httpx


def round_step(value, step, up=False):
    return (Decimal(str(value))/Decimal(step)).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)*Decimal(step)


class Binance:
    def __init__(self, mode, client=None):
        if mode not in ("live", "testnet"):
            raise ValueError("Real Binance client requires testnet or live mode")
        if mode == "live" and os.getenv("ENABLE_LIVE_TRADING") != "true":
            raise ValueError("Live trading is disabled by server policy")
        self.base = "https://api.binance.com" if mode == "live" else "https://testnet.binance.vision"
        prefix = "BINANCE_" if mode == "live" else "BINANCE_TESTNET_"
        self.key, self.secret = os.getenv(prefix+"API_KEY", ""), os.getenv(prefix+"API_SECRET", "")
        if not self.key or not self.secret:
            raise ValueError("Binance credentials are not configured")
        self.client = client or httpx.AsyncClient(timeout=10)

    async def signed(self, method, path, params=None):
        params = {**(params or {}), "timestamp": int(time.time()*1000), "recvWindow": 5000}
        query = urlencode(params)
        signature = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        r = await self.client.request(method, self.base+path+"?"+query+"&signature="+signature, headers={"X-MBX-APIKEY": self.key})
        r.raise_for_status()
        return r.json()

    async def public(self, path, params):
        r = await self.client.get(self.base+path, params=params)
        r.raise_for_status()
        return r.json()

    async def prepare(self, coin, side, config, held):
        info = await self.public("/api/v3/exchangeInfo", {"symbol": coin.binance})
        product = info["symbols"][0]
        if product["status"] != "TRADING" or not product.get("isSpotTradingAllowed", False):
            raise ValueError("Spot product unavailable")
        book = await self.public("/api/v3/ticker/bookTicker", {"symbol": coin.binance})
        bid, ask = float(book["bidPrice"]), float(book["askPrice"])
        if bid <= 0 or ask < bid or (ask-bid)/bid*100 > config.max_spread_pct:
            raise ValueError("Spread limit")
        filters = {f["filterType"]: f for f in product["filters"]}
        pf, lot = filters["PRICE_FILTER"], filters["LOT_SIZE"]
        limit = ask*(1+config.max_slippage_pct/100) if side == "BUY" else bid*(1-config.max_slippage_pct/100)
        price = round_step(limit, pf["tickSize"], up=(side == "SELL"))
        quantity = round_step(config.order_usdt/float(price) if side == "BUY" else min(held, config.order_usdt/float(price)), lot["stepSize"])
        if quantity < Decimal(lot["minQty"]) or quantity > Decimal(lot["maxQty"]):
            raise ValueError("Quantity filter")
        if not Decimal(pf["minPrice"]) <= price <= Decimal(pf["maxPrice"]):
            raise ValueError("Price filter")
        notional = quantity*price
        nf = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
        if notional < Decimal(nf.get("minNotional", "0")) or notional > Decimal(nf.get("maxNotional", "1e30")):
            raise ValueError("Notional filter")
        account = await self.signed("GET", "/api/v3/account")
        balances = {b["asset"]: Decimal(b["free"]) for b in account["balances"]}
        required = notional*Decimal("1.002") if side == "BUY" else quantity
        if balances.get("USDT" if side == "BUY" else coin.asset, Decimal(0)) < required:
            raise ValueError("Insufficient free balance")
        return {"symbol": coin.binance, "side": side, "type": "LIMIT", "timeInForce": "IOC", "quantity": format(quantity, "f"), "price": format(price, "f"), "newOrderRespType": "FULL"}

    async def submit(self, params, order_id):
        return await self.signed("POST", "/api/v3/order", {**params, "newClientOrderId": order_id})

    async def query(self, symbol, order_id):
        return await self.signed("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": order_id})


class WhatsApp:
    def __init__(self, client=None):
        self.client = client or httpx.AsyncClient(timeout=10)

    async def send(self, recipient, variables):
        import json
        sid, token, sender, template = [os.getenv(x, "") for x in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_CONTENT_SID")]
        if not all([sid, token, sender, template]):
            raise ValueError("WhatsApp credentials or approved template missing")
        r = await self.client.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", auth=(sid, token), data={"From": sender, "To": "whatsapp:"+recipient, "ContentSid": template, "ContentVariables": json.dumps(variables)})
        r.raise_for_status()
        return r.json()["sid"]
