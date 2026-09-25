"""Authentication and authorization tests for the dashboard adapter.

Run from vault-v2:  python3 -m unittest discover -s dashboard/tests
"""

import http.client
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "dashboard")]

from harness import Cluster                           # noqa: E402  (read-only use)
from vault.api import make_server as make_gateway     # noqa: E402  (frozen, used as-is)
from vault import Policy                              # noqa: E402
import server as dash                                 # noqa: E402
from auth import Auth, UserStore, hash_password, verify_password  # noqa: E402

ADMIN_PW = secrets.token_urlsafe(16)
VIEWER_PW = secrets.token_urlsafe(16)



def _temp_path() -> str:
    """A fresh path for a users file (the file itself is created by UserStore)."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    return path


class Client:
    """Minimal browser stand-in: keeps the session cookie and CSRF token."""

    def __init__(self, port):
        self.port, self.cookie, self.csrf = port, None, None

    def req(self, method, path, body=None, headers=None, csrf=True, cookie=True):
        h = dict(headers or {})
        if cookie and self.cookie:
            h["Cookie"] = self.cookie
        if csrf and self.csrf and method not in ("GET", "HEAD"):
            h.setdefault("X-CSRF-Token", self.csrf)
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            h.setdefault("Content-Type", "application/json")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def login(self, username, password):
        status, h, body = self.req("POST", "/api/login", {"username": username, "password": password})
        if status == 200:
            self.cookie = h["Set-Cookie"].split(";")[0]
            self.csrf = json.loads(body)["csrf"]
        return status, h, body


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.c = Cluster(6, 3)
        self.c.vault.create_bucket("files", Policy(n=3, chunk_size=64 * 1024))
        self.data = os.urandom(40_000)
        self.c.vault.put_object("files", "doc.bin", self.data)
        self.gw = make_gateway(self.c.vault, self.c.maint, port=0)
        threading.Thread(target=self.gw.serve_forever, daemon=True).start()
        self.users_path = _temp_path()
        os.unlink(self.users_path)
        self.users = UserStore(self.users_path)
        self.users.set_user("root", ADMIN_PW, "admin")
        self.users.set_user("alice", VIEWER_PW, "viewer")
        self.srv, self.adapter = dash.make_server(f"http://127.0.0.1:{self.gw.server_address[1]}", port=0,
                                                  users=self.users, frame_ancestors=["http://127.0.0.1:8095"])
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.adapter.close()
        self.gw.shutdown(); self.gw.server_close()
        self.c.close()
        os.unlink(self.users_path)

    def client(self, as_user=None):
        cl = Client(self.port)
        if as_user == "admin":
            self.assertEqual(cl.login("root", ADMIN_PW)[0], 200)
        elif as_user == "viewer":
            self.assertEqual(cl.login("alice", VIEWER_PW)[0], 200)
        return cl

    # ------------------------------------------------------------------ unauthenticated
    def test_unauthenticated_access_is_rejected(self):
        anon = self.client()
        status, h, _ = anon.req("GET", "/")
        self.assertEqual((status, h.get("Location")), (302, "/login"))
        # page loads of the app redirect to the login page
        self.assertEqual(anon.req("GET", "/static/index.html")[1].get("Location"), "/login")
        for method, path in [("GET", "/static/app.js"), ("GET", "/api/session"),
                             ("GET", "/api/snapshot"), ("GET", "/api/gw/buckets"),
                             ("GET", "/api/gw/buckets/files/doc.bin"), ("POST", "/api/verify/files/doc.bin"),
                             ("POST", "/api/nodes/n1/faults"), ("POST", "/api/gw/cluster/maintenance"),
                             ("PUT", "/api/gw/buckets/files/x"), ("POST", "/api/logout")]:
            self.assertEqual(anon.req(method, path, b"{}")[0], 401, f"{method} {path}")
        # the login page and its assets are public
        for path in ("/login", "/static/login.js", "/static/app.css"):
            self.assertEqual(anon.req("GET", path)[0], 200, path)

    def test_forged_or_stale_cookies_rejected(self):
        anon = self.client()
        anon.cookie = "vault_session=" + secrets.token_urlsafe(32)
        self.assertEqual(anon.req("GET", "/api/snapshot")[0], 401)
        anon.cookie = "vault_session="
        self.assertEqual(anon.req("GET", "/api/snapshot")[0], 401)

    # ------------------------------------------------------------------ login
    def test_viewer_login_and_read_access(self):
        v = self.client()
        status, h, body = v.login("alice", VIEWER_PW)
        self.assertEqual(status, 200)
        cookie = h["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)
        self.assertNotIn(VIEWER_PW, cookie + body.decode())
        self.assertEqual(json.loads(v.req("GET", "/api/session")[2])["role"], "viewer")
        self.assertEqual(v.req("GET", "/")[0], 200)
        status, _, body = v.req("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        snap = json.loads(body)
        self.assertEqual(len(snap["cluster"]["nodes"]), 6)
        self.assertEqual(snap["cluster"]["metadata"]["objects"], 1)
        self.assertEqual([o["key"] for o in json.loads(v.req("GET", "/api/gw/buckets/files")[2])], ["doc.bin"])
        self.assertEqual(v.req("GET", "/api/gw/buckets")[0], 200)
        status, _, got = v.req("GET", "/api/gw/buckets/files/doc.bin?dl=1")
        self.assertEqual((status, got), (200, self.data))
        status, _, body = v.req("POST", "/api/verify/files/doc.bin")
        self.assertTrue(json.loads(body)["ok"])

    def test_viewer_snapshot_omits_detailed_records(self):
        admin = self.client("admin")
        admin.req("POST", "/api/nodes/n2/faults", {"down": False})      # creates an attributed record
        v = self.client("viewer")
        snap = json.loads(v.req("GET", "/api/snapshot")[2])
        dump = json.dumps(snap)
        self.assertNotIn("127.0.0.1", dump)                             # no node or gateway addresses
        self.assertNotIn('"addr"', dump)
        self.assertNotIn('"user"', dump)                                # who did what
        self.assertTrue(all(set(v) <= {"reachable", "latency_ms", "health"} for v in snap["nodes_live"].values()))
        self.assertTrue(all("addr" not in e for e in snap["events"]))
        full = json.loads(admin.req("GET", "/api/snapshot")[2])
        self.assertIn("addr", full["cluster"]["nodes"][0])
        self.assertIn("faults", full["nodes_live"]["n1"])
        self.assertEqual(full["activity"][0]["user"], "root")

    def test_viewer_cannot_use_admin_endpoints(self):
        v = self.client("viewer")
        attempts = [
            ("POST", "/api/nodes/n1/faults", {"down": True}),
            ("POST", "/api/nodes/n1/corrupt-random", None),
            ("POST", "/api/gw/cluster/maintenance", None),
            ("POST", "/api/gw/cluster/nodes/n1/drain", None),
            ("PUT", "/api/gw/buckets/files/new.bin", b"data"),
            ("DELETE", "/api/gw/buckets/files/doc.bin", None),
            ("PUT", "/api/gw/buckets/newbucket", {"scheme": "replicate", "n": 3}),
        ]
        for method, path, body in attempts:
            status, _, resp = v.req(method, path, body)
            self.assertEqual(status, 403, f"{method} {path}")
            self.assertEqual(json.loads(resp)["error"], "admin access required")
        # nothing reached the backend
        self.assertFalse(self.c.nodes["n1"].faults.down)
        self.assertEqual(self.c.vault.membership.get("n1").admin, "active")
        self.assertEqual(self.c.vault.get_object("files", "doc.bin"), self.data)
        self.assertEqual([b["name"] for b in self.c.vault.list_buckets()], ["files"])
        self.assertEqual([o["key"] for o in self.c.vault.list_objects("files")], ["doc.bin"])

    def test_admin_can_use_admin_features(self):
        a = self.client("admin")
        self.assertEqual(json.loads(a.req("GET", "/api/session")[2])["role"], "admin")
        self.assertEqual(a.req("PUT", "/api/gw/buckets/files/new.bin", b"hello world")[0], 200)
        self.assertEqual(self.c.vault.get_object("files", "new.bin"), b"hello world")
        self.assertEqual(a.req("DELETE", "/api/gw/buckets/files/new.bin")[0], 200)
        self.assertEqual(a.req("PUT", "/api/gw/buckets/more", {"scheme": "replicate", "n": 3})[0], 201)
        self.assertEqual(a.req("POST", "/api/nodes/n1/faults", {"down": True})[0], 200)
        self.assertTrue(self.c.nodes["n1"].faults.down)
        self.assertEqual(a.req("POST", "/api/nodes/n1/faults", {"down": False})[0], 200)
        self.assertEqual(a.req("POST", "/api/gw/cluster/maintenance")[0], 200)
        self.assertEqual(a.req("POST", "/api/gw/cluster/nodes/n6/drain")[0], 200)
        self.assertEqual(self.c.vault.membership.get("n6").admin, "draining")

    def test_operations_the_dashboard_never_offers_are_denied_for_everyone(self):
        a = self.client("admin")
        for method, path in [("POST", "/api/gw/cluster/nodes/n1/remove"), ("POST", "/api/gw/cluster/nodes"),
                             ("DELETE", "/api/gw/buckets/files"), ("GET", "/api/gw/cluster"),
                             ("GET", "/api/gw/cluster/events"), ("GET", "/api/gw/buckets/../cluster"),
                             ("DELETE", "/api/gw/buckets/%2e%2e/x"), ("POST", "/api/nodes/n1/inventory"),
                             ("PUT", "/api/snapshot"), ("GET", "/api/login")]:
            status, _, body = a.req(method, path, b"{}")
            self.assertEqual(status, 403, f"{method} {path}")
        self.assertEqual(self.c.vault.membership.get("n1").admin, "active")
        self.assertEqual(a.req("GET", "/api/unknown")[0], 404)

    def test_csrf_and_origin_required_for_changes(self):
        a = self.client("admin")
        self.assertEqual(a.req("POST", "/api/nodes/n1/faults", {"down": True}, csrf=False)[0], 403)
        self.assertEqual(a.req("POST", "/api/nodes/n1/faults", {"down": True},
                               headers={"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(a.req("POST", "/api/nodes/n1/faults", {"down": True},
                               headers={"Origin": "https://evil.example"})[0], 403)
        self.assertFalse(self.c.nodes["n1"].faults.down)
        # reads don't need the token
        self.assertEqual(a.req("GET", "/api/snapshot", csrf=False)[0], 200)
        # cross-origin login attempts are refused
        anon = self.client()
        self.assertEqual(anon.req("POST", "/api/login", {"username": "root", "password": ADMIN_PW},
                                  headers={"Origin": "https://evil.example"})[0], 403)

    def test_rejected_body_does_not_desync_keep_alive(self):
        """A request body left unread by an early rejection must not become the next request."""
        v = self.client("viewer")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            h = {"Cookie": v.cookie, "X-CSRF-Token": v.csrf, "Content-Type": "application/json"}
            for method, path, body in [("POST", "/api/nodes/n1/faults", b'{"down": true}'),     # 403, unread
                                       ("POST", "/api/snapshot", b'{"x": 1}'),                    # 403 deny
                                       ("GET", "/api/snapshot", None)]:
                conn.request(method, path, body=body, headers=h)
                r = conn.getresponse()
                r.read()
                self.assertNotEqual(r.status, 501, f"{method} {path} was mis-parsed")
            self.assertEqual(r.status, 200)
            anon = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            anon.request("POST", "/api/nodes/n1/faults", body=b'{"down": true}', headers={"Content-Type": "application/json"})
            r = anon.getresponse(); r.read()
            self.assertEqual((r.status, r.getheader("Connection")), (401, "close"))
            anon.close()
        finally:
            conn.close()

    # ------------------------------------------------------------------ logout
    def test_logout_invalidates_session(self):
        a = self.client("admin")
        stolen = a.cookie
        status, h, _ = a.req("POST", "/api/logout")
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", h["Set-Cookie"])
        # the old token no longer works, even if replayed
        a.cookie = stolen
        self.assertEqual(a.req("GET", "/api/snapshot")[0], 401)
        self.assertEqual(a.req("POST", "/api/nodes/n1/faults", {"down": True})[0], 401)
        self.assertFalse(self.c.nodes["n1"].faults.down)

    # ------------------------------------------------------------------ bad credentials
    def test_invalid_credentials_rejected(self):
        anon = self.client()
        cases = [("root", "wrong-password"), ("alice", ADMIN_PW), ("nobody", "whatever-123"),
                 ("root", ""), ("", ADMIN_PW)]
        for user, pw in cases:
            status, h, body = anon.login(user, pw)
            self.assertEqual(status, 401, (user, pw))
            self.assertNotIn("Set-Cookie", h)
            self.assertEqual(json.loads(body)["error"], "Invalid username or password.")
        self.assertEqual(anon.req("POST", "/api/login", b"username=root", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(anon.req("POST", "/api/login", {"username": ["root"], "password": 1})[0], 400)
        self.assertEqual(anon.req("GET", "/api/snapshot")[0], 401)

    def test_repeated_failures_are_throttled(self):
        anon = self.client()
        for _ in range(5):
            self.assertEqual(anon.login("alice", "bad-password-x")[0], 401)
        # further attempts for this user are refused, even with the right password
        self.assertEqual(anon.login("alice", VIEWER_PW)[0], 429)
        self.assertEqual(anon.login("root", ADMIN_PW)[0], 200)   # other users unaffected

    # ------------------------------------------------------------------ user store
    def test_passwords_are_hashed_not_stored(self):
        raw = Path(self.users_path).read_text()
        self.assertNotIn(ADMIN_PW, raw)
        self.assertNotIn(VIEWER_PW, raw)
        data = json.loads(raw)["users"]
        self.assertTrue(all(u["hash"].startswith("scrypt$") for u in data.values()))
        self.assertEqual(stat.S_IMODE(os.stat(self.users_path).st_mode), 0o600)
        h1, h2 = hash_password("same-password-1"), hash_password("same-password-1")
        self.assertNotEqual(h1, h2)                               # salted
        self.assertTrue(verify_password("same-password-1", h1))
        self.assertFalse(verify_password("same-password-2", h1))
        with self.assertRaises(ValueError):
            self.users.set_user("short", "123", "viewer")         # minimum length enforced
        with self.assertRaises(ValueError):
            self.users.set_user("bad name!", ADMIN_PW, "viewer")

    def test_role_changes_and_removal_take_effect_immediately(self):
        a = self.client("admin")
        self.users.set_user("root", ADMIN_PW, "viewer")           # demote
        self.assertEqual(a.req("POST", "/api/gw/cluster/maintenance")[0], 403)
        self.users.remove("root")                                  # remove
        self.assertEqual(a.req("GET", "/api/snapshot")[0], 401)

    def test_session_timeouts(self):
        auth = Auth(self.users, idle_timeout=0.2, max_age=60)
        s = auth.login("alice", VIEWER_PW, "127.0.0.1")
        self.assertIsNotNone(auth.resolve(s.token))
        time.sleep(0.3)
        self.assertIsNone(auth.resolve(s.token))
        auth = Auth(self.users, idle_timeout=60, max_age=0.2)
        s = auth.login("alice", VIEWER_PW, "127.0.0.1")
        time.sleep(0.3)
        self.assertIsNone(auth.resolve(s.token))

    def test_server_refuses_to_start_without_users(self):
        empty = _temp_path()
        os.unlink(empty)
        with self.assertRaises(ValueError):
            dash.make_server("http://127.0.0.1:1", port=0, users=empty)

    # ------------------------------------------------------------------ headers & client code
    def test_security_headers(self):
        a = self.client("admin")
        for path in ("/", "/api/snapshot", "/api/gw/buckets", "/login"):
            _, h, _ = a.req("GET", path)
            csp = h["Content-Security-Policy"]
            self.assertIn("frame-ancestors 'self' http://127.0.0.1:8095", csp, path)
            self.assertIn("script-src 'self'", csp)
            self.assertEqual(h["X-Content-Type-Options"], "nosniff")

    def test_no_credentials_in_frontend(self):
        static = ROOT / "dashboard" / "static"
        for f in static.iterdir():
            text = f.read_text()
            for secret in (ADMIN_PW, VIEWER_PW):
                self.assertNotIn(secret, text)
            self.assertIsNone(re.search(r"password\s*[:=]\s*[\"'][^\"']+[\"']", text, re.I), f.name)
            # browser storage may hold UI preferences, never auth material
            for call in re.findall(r"(?:localStorage|sessionStorage)\.setItem\(([^)]*)\)", text):
                self.assertIsNone(re.search(r"(?i)csrf|session|token|password|cookie|auth", call), (f.name, call))


if __name__ == "__main__":
    unittest.main()
