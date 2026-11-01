import argparse
import base64
import getpass
import secrets
from urllib.parse import quote
from cryptography.fernet import Fernet
from .security import cipher, password_hash, totp
from .store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["key", "create-user"])
    parser.add_argument("--username", default="admin")
    parser.add_argument("--role", choices=["admin", "viewer"], default="admin")
    args = parser.parse_args()
    if args.action == "key":
        print(Fernet.generate_key().decode())
        return
    f = cipher()
    store = Store()
    password = getpass.getpass("New password (minimum 14 characters): ")
    if len(password) < 14 or password != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords must match and contain at least 14 characters")
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    print("Add this secret to your authenticator:", secret)
    print("Provisioning URI:", f"otpauth://totp/SmallTimeTrader:{quote(args.username)}?secret={secret}&issuer=SmallTimeTrader")
    code = getpass.getpass("Enter authenticator code to confirm enrolment: ")
    import time
    step = int(time.time() // 30)
    matched = next((s for s in [step-1, step, step+1] if code.isascii() and secrets.compare_digest(code, totp(secret, s))),None)
    if matched is None:
        raise SystemExit("Invalid code; user was not created")
    with store.connect() as db:
        db.execute("INSERT INTO users(name,password,mfa,last_step,role) VALUES(?,?,?,?,?)", (args.username, password_hash(password), f.encrypt(secret.encode()).decode(), matched,args.role))
    print("User created. Wait for the next authenticator code before signing in.")


if __name__ == "__main__":
    main()
