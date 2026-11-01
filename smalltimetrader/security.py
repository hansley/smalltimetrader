"""Password + RFC 6238 TOTP, persistent replay prevention and revocable sessions."""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from cryptography.fernet import Fernet


def cipher():
    key = os.environ.get("APP_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("APP_ENCRYPTION_KEY is required; run python -m smalltimetrader.cli key")
    return Fernet(key.encode())


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.b64encode(salt + digest).decode()


def password_valid(password, encoded):
    raw = base64.b64decode(encoded)
    return hmac.compare_digest(password_hash(password, raw[:16]), encoded)


def totp(secret, step):
    key = base64.b32decode(secret)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[offset:offset+4])[0] & 0x7fffffff) % 1000000:06d}"


def authenticate(store, name, password, code, address, now=None):
    now = time.time() if now is None else now
    # Both per-IP and per-account limits survive process restarts. Direct deployment
    # must not trust user-supplied forwarding headers for the client IP.
    keys = ["ip:" + address, "user:" + name]
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for key in keys:
            row = db.execute("SELECT * FROM attempts WHERE key=?", (key,)).fetchone()
            if row and row["until"] > now and row["count"] >= 5:
                return None
        user = db.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        matched = None
        valid_password = password_valid(password, user["password"]) if user else password_valid(password, password_hash("dummy"))
        if user and valid_password and len(code) == 6 and code.isascii() and code.isdigit():
            secret = cipher().decrypt(user["mfa"].encode()).decode()
            for step in range(int(now // 30)-1, int(now // 30)+2):
                if step > user["last_step"] and hmac.compare_digest(totp(secret, step), code):
                    matched = step
                    break
        if matched is None:
            for key in keys:
                db.execute("INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN until<? THEN 1 ELSE count+1 END, until=CASE WHEN until<? THEN excluded.until ELSE until END", (key, now+900, now, now))
            return None
        db.execute("UPDATE users SET last_step=? WHERE name=?", (matched, name))
        for key in keys:
            db.execute("DELETE FROM attempts WHERE key=?", (key,))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute("DELETE FROM sessions WHERE expires<?", (now,))
        db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), name, csrf, now+3600))
        return token, csrf


def session(store, token):
    if not token:
        return None
    rows = store.rows("SELECT s.name,s.csrf,s.expires,u.role FROM sessions s JOIN users u ON s.name=u.name WHERE s.token=? AND s.expires>?", (hashlib.sha256(token.encode()).hexdigest(), time.time()))
    return rows[0] if rows else None
