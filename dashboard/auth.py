"""Authentication for the Vault dashboard (standard library only).

Users live in a JSON file outside the source tree, with scrypt password hashes:

  python3 dashboard/auth.py --users ~/.vault-dashboard/users.json add alice --role viewer
  python3 dashboard/auth.py --users ~/.vault-dashboard/users.json add root --role admin
  python3 dashboard/auth.py --users ... list | remove NAME | passwd NAME

Roles
  viewer  read-only: cluster health, nodes, objects (list/download/verify), events
  admin   additionally: uploads/deletes/buckets, maintenance, drain, demo fault controls,
          and detailed records (node addresses, fault state, who did what)

Sessions are random server-side tokens (HttpOnly, SameSite=Strict cookie) with idle
and absolute timeouts; logout deletes the session. State-changing requests must
carry the session's CSRF token. Failed logins are throttled per client and per user.
Nothing here is hard-coded: there are no default users or passwords.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

ROLES = ("viewer", "admin")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MIN_PASSWORD = 10
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
COOKIE = "vault_session"


# ---------------------------------------------------------------- password hashing

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT["n"], r=SCRYPT["r"], p=SCRYPT["p"],
                        dklen=SCRYPT["dklen"], maxmem=64 * 1024 * 1024)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                            dklen=len(base64.b64decode(want)), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(dk, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


# A fixed hash to verify against when the username doesn't exist, so a login for an
# unknown user costs the same time as one for a real user (no username probing).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------- user store

class UserStore:
    """Users file, re-read automatically when it changes on disk."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._mtime = None
        self._users: dict[str, dict] = {}

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._users, self._mtime = {}, None
            return
        if mtime != self._mtime:
            data = json.loads(self.path.read_text() or "{}")
            users = data.get("users", {}) if isinstance(data, dict) else {}
            self._users = {u: v for u, v in users.items()
                           if isinstance(v, dict) and v.get("role") in ROLES and isinstance(v.get("hash"), str)}
            self._mtime = mtime

    def get(self, username: str) -> dict | None:
        with self._lock:
            self._load()
            return self._users.get(username)

    def count(self) -> int:
        with self._lock:
            self._load()
            return len(self._users)

    def names(self) -> list[tuple[str, str]]:
        with self._lock:
            self._load()
            return sorted((u, v["role"]) for u, v in self._users.items())

    def _write(self, users: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"users": users}, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def set_user(self, username: str, password: str, role: str) -> None:
        if not USERNAME_RE.match(username):
            raise ValueError("username may contain letters, digits, '.', '_' and '-' (max 64)")
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
        with self._lock:
            self._load()
            users = dict(self._users)
            users[username] = {"role": role, "hash": hash_password(password)}
            self._write(users)
            self._mtime = None

    def remove(self, username: str) -> bool:
        with self._lock:
            self._load()
            if username not in self._users:
                return False
            users = {u: v for u, v in self._users.items() if u != username}
            self._write(users)
            self._mtime = None
            return True


# ---------------------------------------------------------------- sessions

@dataclass
class Session:
    token: str
    username: str
    csrf: str
    created: float
    last_seen: float


class Auth:
    def __init__(self, users: UserStore, idle_timeout: float = 30 * 60, max_age: float = 12 * 3600,
                 secure_cookie: bool = False):
        self.users = users
        self.idle_timeout = idle_timeout
        self.max_age = max_age
        self.secure_cookie = secure_cookie
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._fails_ip: dict[str, deque] = defaultdict(deque)
        self._fails_user: dict[str, deque] = defaultdict(deque)

    # -- login throttling ----------------------------------------------------------
    @staticmethod
    def _recent(q: deque, window: float) -> int:
        now = time.monotonic()
        while q and now - q[0] > window:
            q.popleft()
        return len(q)

    def throttled(self, ip: str, username: str) -> bool:
        with self._lock:
            return (self._recent(self._fails_ip[ip], 300) >= 20
                    or self._recent(self._fails_user[username.lower()], 300) >= 5)

    def _fail(self, ip: str, username: str) -> None:
        with self._lock:
            self._fails_ip[ip].append(time.monotonic())
            self._fails_user[username.lower()].append(time.monotonic())
            if len(self._fails_ip) > 10_000:
                self._fails_ip.clear()
            if len(self._fails_user) > 10_000:
                self._fails_user.clear()

    # -- login / logout ----------------------------------------------------------
    def login(self, username: str, password: str, ip: str) -> Session | None:
        user = self.users.get(username) if isinstance(username, str) else None
        ok = verify_password(password if isinstance(password, str) else "", user["hash"] if user else _DUMMY_HASH)
        if not (user and ok):
            self._fail(ip, username if isinstance(username, str) else "")
            return None
        now = time.time()
        s = Session(secrets.token_urlsafe(32), username, secrets.token_urlsafe(24), now, now)
        with self._lock:
            self._sessions[s.token] = s
            self._prune(now)
        return s

    def logout(self, token: str | None) -> bool:
        with self._lock:
            return self._sessions.pop(token, None) is not None if token else False

    def _prune(self, now: float) -> None:
        dead = [t for t, s in self._sessions.items()
                if now - s.last_seen > self.idle_timeout or now - s.created > self.max_age]
        for t in dead:
            del self._sessions[t]

    def resolve(self, token: str | None) -> tuple[Session, str] | None:
        """Session and *current* role for a token, or None. Removed users lose access at once."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            s = self._sessions.get(token)
            if s is None:
                return None
            if now - s.last_seen > self.idle_timeout or now - s.created > self.max_age:
                del self._sessions[token]
                return None
            s.last_seen = now
        user = self.users.get(s.username)
        if user is None:
            self.logout(token)
            return None
        return s, user["role"]

    # -- cookies -----------------------------------------------------------------
    def cookie(self, token: str) -> str:
        return (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
                + ("; Secure" if self.secure_cookie else ""))

    def clear_cookie(self) -> str:
        return (f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                + ("; Secure" if self.secure_cookie else ""))

    @staticmethod
    def token_from(cookie_header: str | None) -> str | None:
        for part in (cookie_header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and value:
                return value
        return None


# ---------------------------------------------------------------- CLI

def _prompt_password() -> str:
    while True:
        p1 = getpass.getpass("Password: ")
        if len(p1) < MIN_PASSWORD:
            print(f"Use at least {MIN_PASSWORD} characters.", file=sys.stderr)
            continue
        if p1 != getpass.getpass("Repeat password: "):
            print("Passwords don't match.", file=sys.stderr)
            continue
        return p1


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Manage Vault dashboard users")
    ap.add_argument("--users", required=True, help="path to the users file (created if missing)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add a user, or change an existing user's role/password")
    a.add_argument("username")
    a.add_argument("--role", choices=ROLES, required=True)
    sub.add_parser("list")
    r = sub.add_parser("remove")
    r.add_argument("username")
    pw = sub.add_parser("passwd")
    pw.add_argument("username")
    args = ap.parse_args(argv)
    store = UserStore(args.users)
    if args.cmd == "add":
        store.set_user(args.username, _prompt_password(), args.role)
        print(f"Saved {args.username} ({args.role}) to {store.path}")
    elif args.cmd == "list":
        for name, role in store.names():
            print(f"{name}\t{role}")
    elif args.cmd == "remove":
        print("removed" if store.remove(args.username) else "no such user")
    elif args.cmd == "passwd":
        user = store.get(args.username)
        if not user:
            sys.exit("no such user")
        store.set_user(args.username, _prompt_password(), user["role"])
        print("password changed")


if __name__ == "__main__":
    main()
