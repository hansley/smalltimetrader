import asyncio
import base64
import time
from types import SimpleNamespace
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from smalltimetrader.config import Settings
from smalltimetrader.store import Store
from smalltimetrader.market import Quote, MarketData, validate, signal
from smalltimetrader.agents import MasterAgent, TradingAgent
from smalltimetrader.execution import Binance, round_step
from smalltimetrader.security import cipher, password_hash, authenticate, totp


@pytest.fixture
def store(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setenv('COOKIE_SECURE','false')
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'default.sqlite'))
    monkeypatch.delenv('ENABLE_LIVE_TRADING',raising=False)
    return Store(str(tmp_path/'test.sqlite'))


def quotes(price=100,now=None):
    now = time.time() if now is None else now
    return [Quote(s,now,price,price*.9995,price*1.0005,price) for s in ['binance','coinbase','kraken']]


def add_user(store,name='admin',role='admin'):
    secret=base64.b32encode(b'12345678901234567890').decode()
    with store.connect() as db:
        db.execute('INSERT INTO users(name,password,mfa,role) VALUES(?,?,?,?)',(name,password_hash('a long test password'),cipher().encrypt(secret.encode()).decode(),role))
    return secret


def seed(store,config,price=100,now=None):
    now=time.time() if now is None else now
    with store.connect() as db:
        db.execute('UPDATE settings SET value=? WHERE id=1',(config.model_dump_json(),))
        for coin in config.coins:
            for q in quotes(price,now):
                db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?)',(coin.asset,q.source,now,q.price,q.bid,q.ask,q.raw))


def test_threshold_boundaries():
    assert signal([1,1,1],1,True)==1
    assert signal([-1,-1,-1],1,True)==-1
    assert signal([2,2,2],2)==0
    assert signal([2.01,2.02,-5],2)==1
    assert signal([-2.01,-2.02,5],2)==-1
    assert signal([0,0,10],2)==0
    assert signal([3,3],2)==0


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),0,-1])
def test_bad_quotes(bad):
    q=quotes();q[0].price=bad
    with pytest.raises(ValueError):validate(q,Settings(),time.time())


def test_staleness_and_disagreement():
    with pytest.raises(ValueError):validate(quotes(now=time.time()-61),Settings(),time.time())
    with pytest.raises(ValueError):validate(quotes(now=time.time()+5),Settings(),time.time())
    q=quotes();q[0].price=105
    with pytest.raises(ValueError):validate(q,Settings(),time.time())
    q=quotes();q[0].source='kraken'
    with pytest.raises(ValueError):validate(q,Settings(),time.time())


def test_settings_reject_invalid_inputs():
    for data in [{'alert_pct':2,'trade_pct':1},{'order_usdt':200},{'interval_seconds':300,'window_seconds':650},{'whatsapp_enabled':True},{'order_usdt':float('nan')},{'mode':'margin'},{'unexpected':1}]:
        with pytest.raises(ValueError):Settings(**data)
    assert Settings().paused and Settings().mode=='paper'


def test_mfa_password_replay_and_persistent_lockout(store):
    secret=add_user(store);now=time.time();code=totp(secret,int(now//30))
    assert authenticate(store,'admin','wrong',code,'a',now) is None
    assert authenticate(store,'admin','a long test password','000000','a',now) is None
    assert authenticate(store,'admin','a long test password',code,'a',now)
    assert authenticate(Store(store.path),'admin','a long test password',code,'a',now) is None
    for _ in range(5):authenticate(store,'admin','bad','000000','b',now+30)
    assert authenticate(Store(store.path),'admin','a long test password',totp(secret,int((now+30)//30)),'b',now+30) is None


def test_rfc6238_known_vector():
    secret=base64.b32encode(b'12345678901234567890').decode()
    assert totp(secret,1)=='287082'  # RFC 6238 time=59, SHA1, truncated to 6 digits.


def test_api_mfa_csrf_live_gate_and_roles(store):
    from smalltimetrader.app import create_app
    secret=add_user(store)
    with TestClient(create_app(store,start_worker=False)) as client:
        assert client.get('/api/state').status_code==401
        assert client.put('/api/settings',json={}).status_code==401
        assert client.get('/').status_code==200
        assert client.get('/static/app.js').status_code==200
        login=client.post('/api/login',json={'username':'admin','password':'a long test password','code':totp(secret,int(time.time()//30))})
        assert login.status_code==200
        assert 'HttpOnly' in login.headers['set-cookie']
        assert client.post('/api/pause').status_code==403
        state=client.get('/api/state').json();headers={'X-CSRF-Token':state['csrf']}
        settings=state['settings'];settings['mode']='live'
        assert client.put('/api/settings',json=settings,headers=headers).status_code==400
        settings['mode']='paper';settings['paused']=False
        assert client.put('/api/settings',json=settings,headers=headers).status_code==200
        assert client.post('/api/pause',headers=headers).status_code==200
        with store.connect() as db:db.execute("UPDATE users SET role='viewer'")
        assert client.post('/api/pause',headers=headers).status_code==403
        assert client.get('/api/state').status_code==200
        assert client.post('/api/logout',headers=headers).status_code==200
        assert client.get('/api/state').status_code==401


def test_process_lock_excludes_second_worker(store):
    from smalltimetrader.app import ProcessLock
    first=ProcessLock(store.path)
    try:
        with pytest.raises(RuntimeError):ProcessLock(store.path)
    finally:first.close()
    second=ProcessLock(store.path);second.close()


def test_paper_buy_sell_cooldown_and_restart(store):
    config=Settings(paused=False);seed(store,config)
    trader=TradingAgent(store);coin=config.coins[0]
    asyncio.run(trader.act(coin,quotes(),[3,3,3],config))
    assert store.rows('SELECT * FROM orders')[0]['status']=='FILLED'
    asyncio.run(TradingAgent(Store(store.path)).act(coin,quotes(),[3,3,3],config))
    assert len(store.rows('SELECT * FROM orders'))==1
    with store.connect() as db:db.execute("DELETE FROM cooldowns")
    asyncio.run(trader.act(coin,quotes(),[-3,-3,-3],config))
    assert len(store.rows('SELECT * FROM orders'))==2
    assert store.rows('SELECT quantity FROM positions')[0]['quantity']==pytest.approx(0)
    assert store.rows('SELECT cash FROM balances')[0]['cash']<1000


def test_spot_never_short_and_reversal(store):
    config=Settings(paused=False,strategy='reversal');seed(store,config)
    trader=TradingAgent(store);coin=config.coins[0]
    asyncio.run(trader.act(coin,quotes(),[3,3,3],config))
    assert not store.rows('SELECT * FROM orders')
    asyncio.run(trader.act(coin,quotes(),[-3,-3,-3],config))
    assert store.rows('SELECT side FROM orders')[0]['side']=='BUY'


def test_exposure_and_daily_loss_block_buys(store):
    config=Settings(paused=False,max_exposure_usdt=25);seed(store,config)
    with pytest.raises(ValueError,match='Exposure'):asyncio.run(TradingAgent(store).act(config.coins[0],quotes(),[3,3,3],config))
    config=Settings(paused=False);seed(store,config,now=time.time()+.01)
    with store.connect() as db:
        db.execute('UPDATE balances SET cash=900')
    with pytest.raises(ValueError,match='loss'):asyncio.run(TradingAgent(store).act(config.coins[0],quotes(),[3,3,3],config))


def test_unknown_order_blocks_other_coins(store):
    config=Settings(paused=False);seed(store,config)
    with store.connect() as db:db.execute("INSERT INTO orders(id,ts,asset,mode,side,status) VALUES('uncertain',?,'BTC','live','BUY','UNKNOWN')",(time.time(),))
    with pytest.raises(ValueError,match='Unreconciled'):asyncio.run(TradingAgent(store).act(config.coins[1],quotes(),[3,3,3],config))
    assert len(store.rows('SELECT * FROM orders'))==1


def test_settlement_idempotent_and_base_fees(store):
    config=Settings();seed(store,config)
    trader=TradingAgent(store);trader.marks(config,time.time())
    with store.connect() as db:db.execute("INSERT INTO orders(id,ts,asset,mode,side,status) VALUES('once',?,'BTC','paper','BUY','UNKNOWN')",(time.time(),))
    trader.settle('once',1,100,1,.001,'test')
    trader.settle('once',1,100,1,.001,'duplicate')
    assert store.rows('SELECT quantity FROM positions')[0]['quantity']==pytest.approx(.999)
    assert store.rows('SELECT cash FROM balances')[0]['cash']==899


def test_monitor_baseline_whatsapp_dedup_and_no_data_no_trade(store):
    config=Settings(paused=False,whatsapp_enabled=True,whatsapp_to='+23051234567')
    seed(store,config,now=time.time()-600)
    class Market:
        async def collect(self,coin,config):return quotes(103)
    class Notifier:
        def __init__(self):self.sent=[]
        async def send(self,to,variables):self.sent.append((to,variables));return 'SMtest'
    notifier=Notifier();master=MasterAgent(store,Market(),notifier)
    asyncio.run(master.cycle())
    assert len(notifier.sent)==2
    assert len(store.rows('SELECT * FROM orders'))==2
    asyncio.run(master.cycle())
    assert len(notifier.sent)==2
    assert len(store.rows('SELECT * FROM orders'))==2
    class Broken:
        async def collect(self,coin,config):raise ValueError('unavailable')
    master.monitor.market=Broken()
    asyncio.run(master.cycle())
    assert len(store.rows('SELECT * FROM orders'))==2


def test_binance_precision_and_live_gate(monkeypatch):
    monkeypatch.delenv('ENABLE_LIVE_TRADING',raising=False)
    with pytest.raises(ValueError,match='disabled'):Binance('live')
    assert str(round_step('1.23456','0.001'))=='1.234'
    assert str(round_step('1.23456','0.001',True))=='1.235'


def test_stop_loss_can_reduce_holdings_without_threshold_signal(store):
    config=Settings(paused=False);seed(store,config)
    trader=TradingAgent(store)
    asyncio.run(trader.act(config.coins[0],quotes(),[3,3,3],config))
    seed(store,config,90,now=time.time()+.001)
    asyncio.run(trader.act(config.coins[0],quotes(90),[],config))
    assert store.rows('SELECT * FROM orders ORDER BY ts DESC')[0]['side']=='SELL'


def test_adapters_convert_usdt_and_parse_timestamps():
    now=time.time()
    def handler(request):
        path=request.url.path
        if 'kraken' in request.url.host:
            if path.endswith('Ticker'):return httpx.Response(200,json={'error':[],'result':{'pair':{'b':['99.95'],'a':['100.05']}}})
            price='.99' if request.url.params['pair']=='USDTUSD' else '99'
            return httpx.Response(200,json={'error':[],'result':{'pair':[[price,'1',now,'b','m','']], 'last':'123'}})
        if 'coinbase' in request.url.host:
            from datetime import datetime,timezone
            return httpx.Response(200,json={'price':'99','bid':'98.95','ask':'99.05','time':datetime.fromtimestamp(now,timezone.utc).isoformat()})
        if path.endswith('trades'):return httpx.Response(200,json=[{'price':'100','time':int(now*1000)}])
        return httpx.Response(200,json={'bidPrice':'99.95','askPrice':'100.05'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            data=await MarketData(client).collect(Settings().coins[0],Settings())
            assert data[0].price==99 and data[0].raw==100
            assert data[1].source=='coinbase' and data[2].source=='kraken'
    asyncio.run(run())


def test_timeout_is_not_reposted_and_reconciles_once(store):
    config=Settings(paused=False,mode='testnet');seed(store,config)
    class FakeExchange:
        submitted=0
        def __init__(self,mode):
            self.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={})))
        async def prepare(self,*args):return {'symbol':'BTCUSDT'}
        async def submit(self,params,order_id):
            FakeExchange.submitted+=1
            raise httpx.ReadTimeout('Accepted remotely, response lost')
        async def query(self,symbol,order_id):
            return {'status':'FILLED','executedQty':'.25','cummulativeQuoteQty':'25','fills':[{'qty':'.25','price':'100','commission':'.025','commissionAsset':'USDT'}]}
    trader=TradingAgent(store,FakeExchange)
    asyncio.run(trader.act(config.coins[0],quotes(),[3,3,3],config))
    assert store.rows('SELECT status FROM orders')[0]['status']=='UNKNOWN'
    asyncio.run(trader.reconcile(config))
    asyncio.run(trader.reconcile(config))
    assert FakeExchange.submitted==1
    assert store.rows('SELECT status FROM orders')[0]['status']=='FILLED'
    assert store.rows('SELECT quantity FROM positions')[0]['quantity']==.25


def test_partial_fill_and_third_asset_fee_block(store):
    config=Settings();seed(store,config)
    trader=TradingAgent(store);trader.marks(config,time.time())
    for id in ['partial','bnb']:
        with store.connect() as db:db.execute("INSERT INTO orders(id,ts,asset,mode,side,status) VALUES(?,?,'BTC','paper','BUY','UNKNOWN')",(id,time.time()))
    result={'status':'EXPIRED','executedQty':'.1','cummulativeQuoteQty':'10','fills':[{'qty':'.1','price':'100','commission':'.01','commissionAsset':'USDT'}]}
    asyncio.run(trader.exchange_settle(None,'partial',config.coins[0],result))
    assert store.rows("SELECT quantity FROM orders WHERE id='partial'")[0]['quantity']==.1
    result['fills'][0]['commissionAsset']='BNB'
    asyncio.run(trader.exchange_settle(None,'bnb',config.coins[0],result))
    assert store.rows("SELECT status FROM orders WHERE id='bnb'")[0]['status']=='NEEDS_REVIEW'
    assert store.rows('SELECT quantity FROM positions')[0]['quantity']==.1


def test_whatsapp_failure_recorded_without_retries(store):
    config=Settings(whatsapp_enabled=True,whatsapp_to='+23051234567');seed(store,config)
    class Notifier:
        count=0
        async def send(self,*args):self.count+=1;raise httpx.ReadTimeout('uncertain')
    n=Notifier();master=MasterAgent(store,market=object(),notifier=n)
    asyncio.run(master.notify(config.coins[0],[1.2,1.2,1.2],config))
    asyncio.run(master.notify(config.coins[0],[1.2,1.2,1.2],config))
    assert n.count==1
    assert store.rows('SELECT status FROM notifications')[0]['status']=='UNKNOWN'


def test_order_preparation_rounds_and_checks_actual_balance(monkeypatch):
    monkeypatch.setenv('BINANCE_TESTNET_API_KEY','test')
    monkeypatch.setenv('BINANCE_TESTNET_API_SECRET','test')
    def handler(request):
        if request.url.path.endswith('exchangeInfo'):
            return httpx.Response(200,json={'symbols':[{'status':'TRADING','isSpotTradingAllowed':True,'filters':[{'filterType':'PRICE_FILTER','tickSize':'.01','minPrice':'.01','maxPrice':'10000000'},{'filterType':'LOT_SIZE','minQty':'.001','maxQty':'100','stepSize':'.001'},{'filterType':'MIN_NOTIONAL','minNotional':'5'}]}]})
        if request.url.path.endswith('bookTicker'):
            return httpx.Response(200,json={'bidPrice':'100','askPrice':'100.1'})
        assert request.headers['X-MBX-APIKEY']=='test'
        assert 'signature' in request.url.params
        return httpx.Response(200,json={'balances':[{'asset':'USDT','free':'1000'}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            params=await Binance('testnet',client).prepare(Settings().coins[0],'BUY',Settings(),0)
            assert params['type']=='LIMIT' and params['timeInForce']=='IOC'
            assert float(params['quantity'])*float(params['price'])<=25
            assert float(params['price'])<=100.1*1.002
    asyncio.run(run())
