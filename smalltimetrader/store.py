import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from .config import Settings


class Store:
    def __init__(self, path=None):
        self.path = path or os.getenv("DATABASE_PATH", "data/trader.sqlite")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, password TEXT NOT NULL, mfa TEXT NOT NULL, last_step INTEGER DEFAULT -1, role TEXT NOT NULL DEFAULT 'admin');
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, name TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(key TEXT PRIMARY KEY, count INTEGER, until REAL);
            CREATE TABLE IF NOT EXISTS quotes(asset TEXT, source TEXT, ts REAL, price REAL, bid REAL, ask REAL, raw REAL, PRIMARY KEY(asset,source,ts));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, kind TEXT, asset TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, ts REAL, asset TEXT, mode TEXT, side TEXT, status TEXT, quantity REAL DEFAULT 0, price REAL DEFAULT 0, fee REAL DEFAULT 0, detail TEXT);
            CREATE TABLE IF NOT EXISTS positions(asset TEXT, mode TEXT, quantity REAL, cost REAL, PRIMARY KEY(asset,mode));
            CREATE TABLE IF NOT EXISTS balances(mode TEXT PRIMARY KEY, cash REAL);
            CREATE TABLE IF NOT EXISTS equity(day TEXT, mode TEXT, opening REAL, PRIMARY KEY(day,mode));
            CREATE TABLE IF NOT EXISTS cooldowns(key TEXT PRIMARY KEY, ts REAL);
            CREATE TABLE IF NOT EXISTS worker(id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT, heartbeat REAL);
            CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, ts REAL, asset TEXT, status TEXT, detail TEXT);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (Settings().model_dump_json(),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self):
        with self.connect() as db:
            return Settings.model_validate_json(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])

    def event(self, kind, detail, asset=""):
        with self.connect() as db:
            db.execute("INSERT INTO events(ts,kind,asset,detail) VALUES(?,?,?,?)", (time.time(), kind, asset, json.dumps(detail)))

    def rows(self, query, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(query, args).fetchall()]
