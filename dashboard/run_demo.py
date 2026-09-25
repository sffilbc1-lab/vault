"""One-command demo: start an unmodified Vault cluster plus the dashboard.

  python3 dashboard/run_demo.py            # from the vault-v2 directory

The cluster is the stock ``python -m vault cluster`` command, run as a separate
process with its data in a fresh temporary directory (deleted on exit). Timings
are shortened so failures and repairs are visible within seconds during a demo;
they are ordinary CLI flags of the frozen backend.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # vault-v2
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import make_server  # noqa: E402


def wait_for(url: str, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"vault cluster exited early (code {proc.returncode})")
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"timed out waiting for {url}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--nodes", type=int, default=6)
    p.add_argument("--zones", type=int, default=3)
    p.add_argument("--gateway-port", type=int, default=28080)
    p.add_argument("--node-base-port", type=int, default=29100)
    p.add_argument("--port", type=int, default=8090, help="dashboard port")
    p.add_argument("--data-dir", help="keep cluster data here instead of a temp dir")
    p.add_argument("--maint-interval", type=float, default=2.0)
    p.add_argument("--dead-after", type=float, default=5.0)
    args = p.parse_args()

    data = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="vault-demo-"))
    cmd = [sys.executable, "-m", "vault", "cluster", "--nodes", str(args.nodes),
           "--zones", str(args.zones), "--dir", str(data), "--port", str(args.gateway_port),
           "--base-port", str(args.node_base_port), "--maint-interval", str(args.maint_interval),
           "--dead-after", str(args.dead_after), "--gc-grace", "30"]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    cluster = subprocess.Popen(cmd, cwd=ROOT, env=env)
    server = adapter = None
    try:
        gateway = f"http://127.0.0.1:{args.gateway_port}"
        wait_for(f"{gateway}/cluster", cluster)
        server, adapter = make_server(gateway, "127.0.0.1", args.port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"\n  Vault cluster : {args.nodes} nodes, gateway {gateway}, data {data}")
        print(f"  Dashboard     : http://127.0.0.1:{server.server_address[1]}\n", flush=True)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.wait(0.5):
            if cluster.poll() is not None:
                print("vault cluster exited", file=sys.stderr)
                break
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.shutdown()
            server.server_close()
            adapter.close()
        if cluster.poll() is None:
            cluster.terminate()
            try:
                cluster.wait(10)
            except subprocess.TimeoutExpired:
                cluster.kill()
        if not args.data_dir:
            shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main()
