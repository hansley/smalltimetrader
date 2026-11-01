import asyncio
import hashlib
import os
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .agents import MasterAgent
from .config import Settings
from .security import authenticate, session, cipher
from .store import Store


class ProcessLock:
    """OS releases lock on crash. Exactly one app worker per database volume."""
    def __init__(self,path):
        self.file = open(path+".lock","a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another SmallTimeTrader worker already owns this database")

    def close(self):
        self.file.close()


def create_app(store=None, start_worker=True):
    store = store or Store()
    master = MasterAgent(store)

    @asynccontextmanager
    async def lifespan(app):
        cipher()  # Fail closed if persistent encryption key is missing/invalid.
        lock = ProcessLock(store.path)
        task = asyncio.create_task(master.run()) if start_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await master.market.client.aclose()
            await master.notifier.client.aclose()
            lock.close()

    app = FastAPI(title="SmallTimeTrader",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=os.getenv("ALLOWED_HOSTS","localhost,127.0.0.1,testserver").split(","))
    static = Path(__file__).parent/"static"

    @app.middleware("http")
    async def headers(request,call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control":"no-store","X-Content-Type-Options":"nosniff","X-Frame-Options":"DENY","Referrer-Policy":"no-referrer","Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    def user(request:Request):
        identity = session(store,request.cookies.get("stt_session"))
        if not identity:
            raise HTTPException(401,"Sign in with password and MFA")
        if request.method not in ("GET","HEAD") and request.headers.get("X-CSRF-Token")!=identity["csrf"]:
            raise HTTPException(403,"Invalid CSRF token")
        return identity

    def admin(identity=Depends(user)):
        if identity['role']!='admin':
            raise HTTPException(403,'Administrator access required')
        return identity

    class Login(BaseModel):
        username: str = Field(min_length=1,max_length=100)
        password: str = Field(min_length=1,max_length=200)
        code: str = Field(pattern=r'^[0-9]{6}$')

    @app.post("/api/login")
    async def login(body:Login,request:Request):
        # Scrypt work runs off the event loop, so logins cannot stall market checks.
        result = await asyncio.to_thread(authenticate,store,body.username,body.password,body.code,request.client.host)
        if not result:
            raise HTTPException(401,"Invalid credentials, reused code or rate limit; try a fresh code later")
        from fastapi.responses import JSONResponse
        response = JSONResponse({"csrf":result[1]})
        response.set_cookie("stt_session",result[0],httponly=True,secure=os.getenv("COOKIE_SECURE","true")=="true",samesite="strict",max_age=3600)
        store.event("login",{"user":body.username})
        return response

    @app.post("/api/logout")
    async def logout(request:Request,identity=Depends(user)):
        with store.connect() as db:
            db.execute("DELETE FROM sessions WHERE token=?",(hashlib.sha256(request.cookies["stt_session"].encode()).hexdigest(),))
        from fastapi.responses import JSONResponse
        response = JSONResponse({"ok":True})
        response.delete_cookie("stt_session")
        return response

    @app.get("/api/state")
    async def state(identity=Depends(user)):
        return {"user":identity["name"],"csrf":identity["csrf"],"settings":store.settings().model_dump(),"schema":Settings.model_json_schema(),"health":master.health,"live_enabled":os.getenv("ENABLE_LIVE_TRADING")=="true","positions":store.rows("SELECT * FROM positions"),"balances":store.rows("SELECT * FROM balances"),"orders":store.rows("SELECT * FROM orders ORDER BY ts DESC LIMIT 100"),"events":store.rows("SELECT * FROM events ORDER BY id DESC LIMIT 100"),"notifications":store.rows("SELECT * FROM notifications ORDER BY ts DESC LIMIT 30"),"prices":store.rows("SELECT q.* FROM quotes q JOIN (SELECT asset,source,MAX(ts) ts FROM quotes GROUP BY asset,source) l ON q.asset=l.asset AND q.source=l.source AND q.ts=l.ts")}

    @app.put("/api/settings")
    async def settings(body:Settings,identity=Depends(admin)):
        async with master.lock:
            previous = store.settings()
            if body.mode=="live" and os.getenv("ENABLE_LIVE_TRADING")!="true":
                raise HTTPException(400,"Live trading is disabled by server policy")
            held = store.rows("SELECT DISTINCT asset FROM positions WHERE quantity>0")
            needed = {r["asset"] for r in held}
            needed |= {r["asset"] for r in store.rows("SELECT asset FROM orders WHERE status IN ('UNKNOWN','SUBMITTING','NEEDS_REVIEW')")}
            if not needed.issubset({c.asset for c in body.coins}):
                raise HTTPException(400,"Cannot remove coins with open positions or unresolved orders")
            if previous.mode != body.mode and not body.paused:
                raise HTTPException(400,"Pause before switching execution mode")
            with store.connect() as db:
                db.execute("UPDATE settings SET value=? WHERE id=1",(body.model_dump_json(),))
                # Changing the comparison definition invalidates existing baselines.
                if previous.window_seconds!=body.window_seconds or previous.coins!=body.coins:
                    db.execute("DELETE FROM quotes")
            store.event("settings_changed",{"user":identity["name"],"mode":body.mode,"paused":body.paused})
        return {"ok":True}

    @app.post("/api/pause")
    async def pause(identity=Depends(admin)):
        async with master.lock:
            config = store.settings()
            config.paused = True
            with store.connect() as db:
                db.execute("UPDATE settings SET value=? WHERE id=1",(config.model_dump_json(),))
            store.event("paused",{"user":identity["name"]})
        return {"ok":True}

    @app.get("/health")
    async def health():
        return {"ok":True}  # No private holdings/configuration leaked.

    @app.get("/")
    async def index():
        return FileResponse(static/"index.html")

    app.mount("/static",StaticFiles(directory=static),name="static")
    return app


app = create_app()
