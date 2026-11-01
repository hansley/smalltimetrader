"""Three deterministic agents. No language model is allowed to place orders."""
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone
from uuid import uuid4
from .market import MarketData, validate, signal
from .execution import Binance, WhatsApp


class MonitoringAgent:
    def __init__(self, store, market):
        self.store, self.market = store, market

    async def inspect(self, coin, config):
        quotes = await self.market.collect(coin, config)
        now = time.time()
        validate(quotes, config, now)
        changes = []
        with self.store.connect() as db:
            for q in quotes:
                old = db.execute("SELECT price FROM quotes WHERE asset=? AND source=? AND ts BETWEEN ? AND ? ORDER BY ABS(ts-?) LIMIT 1", (coin.asset, q.source, now-config.window_seconds-config.baseline_tolerance_seconds, now-config.window_seconds+config.baseline_tolerance_seconds, now-config.window_seconds)).fetchone()
                if old:
                    changes.append((q.price/old["price"]-1)*100)
                db.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?,?)", (coin.asset, q.source, now, q.price, q.bid, q.ask, q.raw))
            db.execute("DELETE FROM quotes WHERE ts<?", (now-172800,))
        self.store.event("prices", {"prices_usd": {q.source: q.price for q in quotes}, "changes_pct": changes, "baseline_ready": len(changes)==3}, coin.asset)
        return quotes, changes


class TradingAgent:
    def __init__(self, store, executor_factory=Binance):
        self.store, self.executor_factory = store, executor_factory

    def marks(self, config, now):
        marks = {}
        with self.store.connect() as db:
            for coin in config.coins:
                row = db.execute("SELECT raw,ts FROM quotes WHERE asset=? AND source='binance' ORDER BY ts DESC LIMIT 1", (coin.asset,)).fetchone()
                if row and now-row["ts"] <= config.max_age_seconds:
                    marks[coin.asset] = row["raw"]
            positions = db.execute("SELECT * FROM positions WHERE mode=? AND quantity>0", (config.mode,)).fetchall()
            if any(p["asset"] not in marks for p in positions):
                raise ValueError("Missing fresh valuation for an open position")
            exposure = sum(p["quantity"]*marks[p["asset"]] for p in positions)
            db.execute("INSERT OR IGNORE INTO balances VALUES(?,?)", (config.mode, config.paper_start_usdt if config.mode=='paper' else 0))
            cash = db.execute("SELECT cash FROM balances WHERE mode=?", (config.mode,)).fetchone()[0]
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            db.execute("INSERT OR IGNORE INTO equity VALUES(?,?,?)", (day, config.mode, cash+exposure))
            opening = db.execute("SELECT opening FROM equity WHERE day=? AND mode=?", (day, config.mode)).fetchone()[0]
        return exposure, cash, opening-(cash+exposure)

    async def act(self, coin, quotes, changes, config):
        direction = signal(changes, config.trade_pct)
        if config.paused:
            return
        side = "BUY" if direction*(1 if config.strategy=="momentum" else -1)>0 else "SELL"
        now = time.time()
        key = f"trade:{config.mode}:{coin.asset}"
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM orders WHERE status IN ('SUBMITTING','UNKNOWN','NEEDS_REVIEW')").fetchone():
                raise ValueError("Unreconciled order blocks trading")
            last = db.execute("SELECT ts FROM cooldowns WHERE key=?", (key,)).fetchone()
            position = db.execute("SELECT quantity,cost FROM positions WHERE asset=? AND mode=?", (coin.asset, config.mode)).fetchone()
            held = position[0] if position else 0
        mark = next(q.raw for q in quotes if q.source=='binance')
        pnl = (held*mark/position['cost']-1)*100 if held and position['cost'] else 0
        risk_exit = held > 0 and (pnl <= -config.stop_loss_pct or pnl >= config.take_profit_pct)
        if risk_exit:
            side = 'SELL'
        elif not direction or (last and now-last[0] < config.cooldown_seconds):
            return
        if side == "SELL" and held <= 0:
            return  # Spot only; never short or sell unrelated account holdings.
        exposure, cash, loss = self.marks(config, now)
        if side=='BUY' and loss >= config.daily_loss_usdt:
            raise ValueError("Daily managed-portfolio loss limit reached")
        if side == "BUY" and exposure+config.order_usdt*1.02 > config.max_exposure_usdt:
            raise ValueError("Exposure limit")
        day_start = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        turnover = self.store.rows("SELECT COALESCE(SUM(quantity*price),0) value FROM orders WHERE ts>=? AND mode=? AND status='FILLED'", (day_start, config.mode))[0]["value"]
        if turnover+config.order_usdt*1.02 > config.max_daily_turnover_usdt:
            raise ValueError("Daily turnover limit")
        order_id = "stt" + uuid4().hex
        if config.mode == "paper":
            q = next(q for q in quotes if q.source=="binance")
            price = (q.ask if side=="BUY" else q.bid)*q.raw/q.price
            if (q.ask-q.bid)/q.bid*100 > config.max_spread_pct:
                raise ValueError("Spread limit")
            quantity = config.order_usdt/price if side=="BUY" else min(held, config.order_usdt/price)
            fee = quantity*price*config.paper_fee_pct/100
            if side=="BUY" and quantity*price+fee > cash:
                raise ValueError("Insufficient paper cash")
            with self.store.connect() as db:
                db.execute("INSERT INTO orders(id,ts,asset,mode,side,status,detail) VALUES(?,?,?,?,?,'SUBMITTING','Paper simulation')", (order_id, now, coin.asset, config.mode, side))
                db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?)", (key, now))
            self.settle(order_id, quantity, price, fee, 0, "Paper fill: assumes full liquidity at observed bid/ask")
        else:
            executor = self.executor_factory(config.mode)
            try:
                params = await executor.prepare(coin, side, config, held)
                # Recheck freshness after exchange/account API round trips.
                validate(quotes, config, time.time())
                with self.store.connect() as db:
                    db.execute("INSERT INTO orders(id,ts,asset,mode,side,status,detail) VALUES(?,?,?,?,?,'SUBMITTING',?)", (order_id, now, coin.asset, config.mode, side, json.dumps({"symbol":coin.binance})))
                    db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?)", (key, now))
                try:
                    result = await executor.submit(params, order_id)
                    await self.exchange_settle(executor, order_id, coin, result)
                except Exception:
                    # Never blindly retry POST: an HTTP timeout can hide an accepted order.
                    with self.store.connect() as db:
                        db.execute("UPDATE orders SET status='UNKNOWN' WHERE id=? AND status='SUBMITTING'", (order_id,))
                    self.store.event("order_unknown", {"id":order_id}, coin.asset)
            finally:
                await executor.client.aclose()
        with self.store.connect() as db:
            db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?)", (key, now))

    def settle(self, order_id, quantity, price, fee, base_fee, detail):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if order["status"] not in ("SUBMITTING", "UNKNOWN"):
                return
            mode, asset, side = order["mode"], order["asset"], order["side"]
            p = db.execute("SELECT quantity,cost FROM positions WHERE asset=? AND mode=?", (asset,mode)).fetchone()
            old_qty, old_cost = (p[0],p[1]) if p else (0,0)
            total = quantity*price
            new_qty = old_qty+quantity-base_fee if side=="BUY" else old_qty-quantity-base_fee
            if new_qty < -1e-10:
                db.execute("UPDATE orders SET status='NEEDS_REVIEW',detail='Fill exceeds managed holdings' WHERE id=?", (order_id,))
                return
            cost = old_cost+total+fee if side=="BUY" else old_cost*max(new_qty,0)/old_qty if old_qty else 0
            db.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?,?)", (asset,mode,max(0,new_qty),cost))
            db.execute("UPDATE balances SET cash=cash+? WHERE mode=?", ((-total if side=="BUY" else total)-fee,mode))
            db.execute("UPDATE orders SET status=?,quantity=?,price=?,fee=?,detail=? WHERE id=?", ("FILLED" if quantity else "NO_FILL",quantity,price,fee,detail,order_id))
        self.store.event("order_settled", {"id":order_id,"mode":mode,"side":side,"quantity":quantity,"price":price,"fee_usdt":fee,"base_fee":base_fee}, asset)

    async def exchange_settle(self, executor, order_id, coin, result):
        if result["status"] not in ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"):
            raise ValueError("Order not terminal")
        qty = float(result.get("executedQty",0))
        total = float(result.get("cummulativeQuoteQty",0))
        fills = result.get("fills")
        if qty and fills is None:
            fills = await executor.signed("GET", "/api/v3/myTrades", {"symbol":coin.binance,"orderId":result["orderId"],"limit":1000})
            if abs(sum(float(f["qty"]) for f in fills)-qty)>1e-9:
                raise ValueError("Incomplete fill reconciliation")
        fee = base_fee = 0
        for fill in fills or []:
            amount = float(fill["commission"])
            if amount == 0:
                continue
            if fill["commissionAsset"] == "USDT":
                fee += amount
            elif fill["commissionAsset"] == coin.asset:
                base_fee += amount
            else:
                with self.store.connect() as db:
                    db.execute("UPDATE orders SET status='NEEDS_REVIEW',detail='Third-asset commission requires operator accounting; disable BNB fee payment' WHERE id=?", (order_id,))
                return
        self.settle(order_id,qty,total/qty if qty else 0,fee,base_fee,"Binance terminal fill reconciled")

    async def reconcile(self, config):
        for order in self.store.rows("SELECT * FROM orders WHERE status IN ('UNKNOWN','SUBMITTING')"):
            if order["mode"] == "paper":
                # Paper cannot have external side effects. Crash before settlement is safe to cancel.
                with self.store.connect() as db:
                    db.execute("UPDATE orders SET status='NO_FILL',detail='Recovered uncommitted paper order' WHERE id=?", (order["id"],))
                continue
            executor = None
            try:
                executor = self.executor_factory(order["mode"])
                coin = next(c for c in config.coins if c.asset==order["asset"])
                result = await executor.query(coin.binance,order["id"])
                await self.exchange_settle(executor,order["id"],coin,result)
            except Exception:
                self.store.event("reconciliation_pending", {"id":order["id"]},order["asset"])
            finally:
                if executor:
                    await executor.client.aclose()


class MasterAgent:
    def __init__(self, store, market=None, notifier=None, trader=None):
        self.store = store
        self.market = market or MarketData()
        self.notifier = notifier or WhatsApp()
        self.monitor = MonitoringAgent(store,self.market)
        self.trader = trader or TradingAgent(store)
        self.lock = asyncio.Lock()
        self.health = {"state":"starting", "last_cycle":None}

    async def notify(self, coin, changes, config):
        direction = signal(changes, config.alert_pct, inclusive=True)
        if not direction:
            return
        now = time.time()
        key = f"alert:{coin.asset}:{direction}"
        with self.store.connect() as db:
            row = db.execute("SELECT ts FROM cooldowns WHERE key=?", (key,)).fetchone()
            if row and now-row[0] < config.cooldown_seconds:
                return
            db.execute("INSERT OR REPLACE INTO cooldowns VALUES(?,?)",(key,now))
        variables = {"1":coin.asset,"2":f"{statistics.median(changes):+.2f}%","3":str(config.window_seconds//60),"4":datetime.now(timezone.utc).isoformat()}
        self.store.event("alert",variables,coin.asset)
        if not config.whatsapp_enabled:
            return
        notification_id = uuid4().hex
        with self.store.connect() as db:
            db.execute("INSERT INTO notifications VALUES(?,?,?,'SENDING','')",(notification_id,now,coin.asset))
        try:
            sid = await self.notifier.send(config.whatsapp_to,variables)
            status, detail = "ACCEPTED",sid  # Acceptance is not proof of delivery.
        except Exception:
            status, detail = "UNKNOWN", "Provider submission failed or outcome uncertain; no automatic resend"
        with self.store.connect() as db:
            db.execute("UPDATE notifications SET status=?,detail=? WHERE id=?",(status,detail,notification_id))

    async def cycle(self):
        async with self.lock:
            config = self.store.settings()
            self.health = {"state":"running","last_cycle":time.time()}
            await self.trader.reconcile(config)
            observations = []
            for coin in config.coins:
                try:
                    quotes,changes = await self.monitor.inspect(coin,config)
                    observations.append((coin,quotes,changes))
                    await self.notify(coin,changes,config)
                except Exception as error:
                    self.store.event("monitor_blocked",{"error":type(error).__name__},coin.asset)
            # First collect the entire portfolio, then value risk and submit trades.
            for coin,quotes,changes in observations:
                try:
                    validate(quotes,config,time.time())
                    await self.trader.act(coin,quotes,changes,config)
                except Exception as error:
                    self.store.event("trade_blocked",{"error":type(error).__name__,"reason":str(error) if isinstance(error,ValueError) else "External service failure"},coin.asset)
            self.health["state"] = "idle"
            self.store.event("cycle_complete",{"coins":len(observations),"mode":config.mode,"paused":config.paused})

    async def run(self):
        while True:
            started = time.monotonic()
            try:
                await self.cycle()
            except Exception as error:
                self.health["state"] = "error"
                self.store.event("cycle_error",{"error":type(error).__name__})
            await asyncio.sleep(max(1,self.store.settings().interval_seconds-(time.monotonic()-started)))
