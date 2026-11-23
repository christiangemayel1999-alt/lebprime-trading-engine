#!/usr/bin/env python3
"""Login gate for the public dashboard: username + password + TOTP (authenticator app).

Caddy sends every dashboard request through `forward_auth` to /auth/verify; only a valid,
signed session cookie gets through. Standard library only.

    lebprime-auth init --username christian   create credentials (random password + TOTP secret)
    lebprime-auth show                         print the initial password and the 2FA QR code (SSH only)
    lebprime-auth forget-initial               delete the one-time copy of the initial password
    lebprime-auth set-password                 choose a new password (prompted, never echoed)
    lebprime-auth reset-2fa                    new TOTP secret + QR (old authenticator entry stops working)
    lebprime-auth serve                        run the gate on 127.0.0.1:8601 (systemd: lebprime-auth.service)

Protection: scrypt password hash, TOTP (RFC 6238, 30 s, +/-1 step, each code usable once),
per-IP lockout after 5 failures in 15 minutes, account-wide slow-down after 10, constant-time
compares, HttpOnly + Secure + SameSite=Strict cookies valid 12 hours, failures logged for fail2ban.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import secrets
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

CONFIG = Path(os.environ.get("LEBPRIME_AUTH_CONFIG", "/etc/lebprime/auth.json"))
INITIAL = Path(os.environ.get("LEBPRIME_AUTH_INITIAL", "/etc/lebprime/auth-initial.txt"))
BIND = os.environ.get("LEBPRIME_AUTH_BIND", "127.0.0.1")
PORT = int(os.environ.get("LEBPRIME_AUTH_PORT", "8601"))
COOKIE = "lp_session"
SESSION_SECONDS = int(os.environ.get("LEBPRIME_AUTH_SESSION_SECONDS", str(12 * 3600)))
IP_MAX_FAILURES, IP_WINDOW, IP_LOCK = 5, 900, 900
USER_MAX_FAILURES, USER_WINDOW, USER_LOCK = 10, 900, 300
ISSUER = "LebPrime"


def log(event: str, **fields: object) -> None:
    print(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}), flush=True)


# --------------------------------------------------------------------------- crypto helpers

def hash_password(password: str) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return {"salt": salt.hex(), "n": 2**15, "r": 8, "p": 1, "hash": digest.hex()}


def check_password(password: str, stored: dict) -> bool:
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(stored["salt"]), n=stored["n"], r=stored["r"],
                            p=stored["p"], maxmem=64 * 1024 * 1024, dklen=32)
    return hmac.compare_digest(digest.hex(), stored["hash"])


def totp_at(secret_b32: str, step: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_match(secret_b32: str, code: str, now: float | None = None) -> int | None:
    """Return the matching time step (to block reuse) or None."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != 6:
        return None
    step = int((now or time.time()) // 30)
    for candidate in (step - 1, step, step + 1):
        if hmac.compare_digest(totp_at(secret_b32, candidate), code):
            return candidate
    return None


def otpauth_uri(username: str, secret: str) -> str:
    label = quote(f"{ISSUER}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={ISSUER}&algorithm=SHA1&digits=6&period=30"


def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o640)
    os.replace(tmp, CONFIG)


# --------------------------------------------------------------------------- sessions and throttling

def make_session(cfg: dict, username: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"u": username, "exp": int(time.time()) + SESSION_SECONDS,
                                                   "v": cfg.get("session_version", 1)}).encode()).decode().rstrip("=")
    sig = hmac.new(bytes.fromhex(cfg["session_secret"]), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def session_ok(cfg: dict, token: str) -> bool:
    try:
        payload, sig = token.split(".", 1)
        expected = hmac.new(bytes.fromhex(cfg["session_secret"]), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return data.get("u") == cfg["username"] and int(data["exp"]) > time.time() and data.get("v", 1) == cfg.get("session_version", 1)
    except (ValueError, KeyError, json.JSONDecodeError):
        return False


class Throttle:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ip_failures: dict[str, list[float]] = {}
        self.ip_locked_until: dict[str, float] = {}
        self.user_failures: list[float] = []
        self.user_locked_until = 0.0
        self.used_steps: set[int] = set()

    def locked(self, ip: str) -> float:
        now = time.time()
        with self.lock:
            return max(self.ip_locked_until.get(ip, 0.0), self.user_locked_until) - now

    def fail(self, ip: str) -> None:
        now = time.time()
        with self.lock:
            recent = [t for t in self.ip_failures.get(ip, []) if now - t < IP_WINDOW] + [now]
            self.ip_failures[ip] = recent
            if len(recent) >= IP_MAX_FAILURES:
                self.ip_locked_until[ip] = now + IP_LOCK
                self.ip_failures[ip] = []
            self.user_failures = [t for t in self.user_failures if now - t < USER_WINDOW] + [now]
            if len(self.user_failures) >= USER_MAX_FAILURES:
                self.user_locked_until = now + USER_LOCK
                self.user_failures = []

    def success(self, ip: str, step: int) -> bool:
        with self.lock:
            if step in self.used_steps:
                return False
            self.used_steps = {s for s in self.used_steps if s >= step - 3} | {step}
            self.ip_failures.pop(ip, None)
            return True


THROTTLE = Throttle()

LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LebPrime login</title>
<style>
:root{{color-scheme:light dark;--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--muted:#5d6675;--accent:#2563eb;--err:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1218;--card:#181c24;--fg:#e7e9ee;--muted:#9aa3b2;--accent:#5b8cff;--err:#ff8a7a}}}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif}}
form{{background:var(--card);padding:28px;border-radius:12px;width:min(340px,calc(100vw - 32px));box-shadow:0 4px 24px #0002}}
h1{{font-size:20px;margin:0 0 4px}}p{{margin:0 0 18px;color:var(--muted);font-size:14px}}
label{{display:block;font-size:13px;color:var(--muted);margin:12px 0 4px}}
input{{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #8884;background:transparent;color:inherit;font-size:16px}}
button{{margin-top:20px;width:100%;padding:11px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-size:16px;cursor:pointer}}
.err{{color:var(--err);font-size:14px;margin-top:12px}}
</style></head><body><form method="post" action="/auth/login">
<h1>LebPrime dashboard</h1><p>Sign in with your password and the 6-digit code from your authenticator app.</p>
<input type="hidden" name="next" value="{next}">
<label for="u">Username</label><input id="u" name="username" autocomplete="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<label for="c">Authenticator code</label><input id="c" name="code" inputmode="numeric" pattern="[0-9 ]{{6,7}}" autocomplete="one-time-code" required>
<button type="submit">Sign in</button>{error}</form></body></html>"""


def safe_next(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") and not value.startswith("/auth/") else "/"


class Handler(BaseHTTPRequestHandler):
    server_version = "lebprime-auth"
    sys_version = ""

    def log_message(self, *_args: object) -> None:  # quiet default access log
        return

    def client_ip(self) -> str:
        # Only Caddy on localhost talks to this service, and it sets X-Forwarded-For.
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def cookie_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def send(self, status: int, body: str = "", headers: dict | None = None, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def login_page(self, status: int = 200, error: str = "", next_url: str = "/") -> None:
        err = f'<div class="err">{html.escape(error)}</div>' if error else ""
        self.send(status, LOGIN_PAGE.format(next=html.escape(safe_next(next_url)), error=err))

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/auth/verify":
            if session_ok(load_config(), self.cookie_token()):
                self.send(200, "ok", content_type="text/plain")
                return
            original = self.headers.get("X-Forwarded-Uri") or "/"
            wants_html = "text/html" in (self.headers.get("Accept") or "")
            if wants_html and (self.headers.get("X-Forwarded-Method") or "GET") == "GET":
                self.send(302, "", {"Location": "/auth/login?next=" + quote(safe_next(original))})
            else:
                self.send(401, '{"detail":"login required"}', content_type="application/json")
            return
        if url.path == "/auth/login":
            self.login_page(next_url=(parse_qs(url.query).get("next") or ["/"])[0])
            return
        self.send(404, "not found", content_type="text/plain")

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/auth/logout":
            self.send(303, "", {"Location": "/auth/login", "Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Strict"})
            return
        if url.path != "/auth/login":
            self.send(404, "not found", content_type="text/plain")
            return
        ip = self.client_ip()
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(length).decode(errors="replace"))
        field = lambda name: (form.get(name) or [""])[0]  # noqa: E731
        next_url = field("next")
        wait = THROTTLE.locked(ip)
        if wait > 0:
            log("auth_locked", ip=ip, seconds=int(wait))
            self.login_page(429, f"Too many failed attempts. Try again in {int(wait // 60) + 1} minutes.", next_url)
            return
        cfg = load_config()
        user_ok = hmac.compare_digest(field("username").strip(), cfg["username"])
        password_ok = check_password(field("password"), cfg["password"])  # always computed: same timing
        step = totp_match(cfg["totp_secret"], field("code"))
        if user_ok and password_ok and step is not None and THROTTLE.success(ip, step):
            log("auth_success", ip=ip, user=cfg["username"])
            cookie = f"{COOKIE}={make_session(cfg, cfg['username'])}; Max-Age={SESSION_SECONDS}; Path=/; Secure; HttpOnly; SameSite=Strict"
            self.send(303, "", {"Location": safe_next(next_url), "Set-Cookie": cookie})
            return
        THROTTLE.fail(ip)
        time.sleep(1.0)
        log("auth_failure", ip=ip, user_ok=user_ok, password_ok=password_ok, code_ok=step is not None)
        self.login_page(401, "Wrong username, password or code.", next_url)


# --------------------------------------------------------------------------- CLI

def print_qr(uri: str) -> None:
    try:
        subprocess.run(["qrencode", "-t", "ANSIUTF8", uri], check=True)
    except (OSError, subprocess.CalledProcessError):
        print("(install qrencode to see a QR code; or type the secret into the app by hand)")


def cmd_init(args: argparse.Namespace) -> int:
    if CONFIG.exists() and not args.force:
        print(f"{CONFIG} exists; use --force to replace it (this logs everyone out and resets 2FA)")
        return 1
    password = secrets.token_urlsafe(18)
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    save_config({"username": args.username, "password": hash_password(password), "totp_secret": secret,
                 "session_secret": secrets.token_hex(32), "session_version": 1})
    old = os.umask(0o077)
    try:
        INITIAL.write_text(f"username: {args.username}\npassword: {password}\n", encoding="utf-8")
    finally:
        os.umask(old)
    print(f"credentials created for {args.username}; run `sudo lebprime-auth show` over SSH to see them")
    return 0


def cmd_show(_args: argparse.Namespace) -> int:
    cfg = load_config()
    if INITIAL.exists():
        print(INITIAL.read_text(encoding="utf-8"))
    else:
        print(f"username: {cfg['username']}\npassword: (initial copy deleted; use set-password to change it)")
    uri = otpauth_uri(cfg["username"], cfg["totp_secret"])
    print("Scan with Google Authenticator, Microsoft Authenticator, 1Password, Authy...:\n")
    print_qr(uri)
    print(f"\nor enter this key by hand: {cfg['totp_secret']}  (time-based, 6 digits)")
    print("\nWhen you have saved the password and the dashboard login works: sudo lebprime-auth forget-initial")
    return 0


def cmd_set_password(_args: argparse.Namespace) -> int:
    first = getpass.getpass("new password (16+ characters): ")
    if len(first) < 16 or first != getpass.getpass("again: "):
        print("passwords differ or shorter than 16 characters")
        return 1
    cfg = load_config()
    cfg["password"] = hash_password(first)
    cfg["session_version"] = int(cfg.get("session_version", 1)) + 1
    save_config(cfg)
    INITIAL.unlink(missing_ok=True)
    print("password changed; existing sessions are logged out")
    return 0


def cmd_reset_2fa(_args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg["totp_secret"] = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    cfg["session_version"] = int(cfg.get("session_version", 1)) + 1
    save_config(cfg)
    print_qr(otpauth_uri(cfg["username"], cfg["totp_secret"]))
    print(f"new key: {cfg['totp_secret']}")
    return 0


def cmd_serve(_args: argparse.Namespace) -> int:
    load_config()  # fail fast if missing
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    log("auth_listening", bind=BIND, port=PORT)
    server.serve_forever()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init")
    init.add_argument("--username", required=True)
    init.add_argument("--force", action="store_true")
    sub.add_parser("show")
    sub.add_parser("forget-initial")
    sub.add_parser("set-password")
    sub.add_parser("reset-2fa")
    sub.add_parser("serve")
    args = parser.parse_args()
    if args.cmd == "forget-initial":
        INITIAL.unlink(missing_ok=True)
        print("initial password copy deleted")
        return 0
    return {"init": cmd_init, "show": cmd_show, "set-password": cmd_set_password, "reset-2fa": cmd_reset_2fa, "serve": cmd_serve}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
