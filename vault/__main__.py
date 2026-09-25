"""Command line entry point.

  python -m vault node     --id n1 --dir data/n1 --port 9001 --zone a
  python -m vault gateway  --meta data/meta.db --port 8080 --node n1=127.0.0.1:9001@a ...
  python -m vault cluster  --nodes 6 --zones 3 --dir data     (everything in one process)
"""

from __future__ import annotations

import argparse
import signal
import threading
from pathlib import Path

from .core import Vault
from .maintenance import Maintenance
from .node import StorageNode
from .api import make_server


def _serve_gateway(vault: Vault, host: str, port: int, args) -> None:
    maint = Maintenance(vault, interval=args.maint_interval, gc_grace=args.gc_grace,
                        orphan_grace=args.orphan_grace)
    vault.membership.start()
    maint.start()
    server = make_server(vault, maint, host, port)
    print(f"gateway listening on http://{host}:{server.server_address[1]}", flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    server.server_close()
    maint.close()
    vault.close()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="vault")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("node", help="run one storage node")
    n.add_argument("--id", required=True)
    n.add_argument("--dir", required=True)
    n.add_argument("--host", default="127.0.0.1")
    n.add_argument("--port", type=int, default=9001)
    n.add_argument("--zone", default="zone-a")
    n.add_argument("--no-fsync", action="store_true")

    for name in ("gateway", "cluster"):
        g = sub.add_parser(name)
        g.add_argument("--host", default="127.0.0.1")
        g.add_argument("--port", type=int, default=8080)
        g.add_argument("--maint-interval", type=float, default=5.0)
        g.add_argument("--gc-grace", type=float, default=300.0)
        g.add_argument("--orphan-grace", type=float, default=3600.0)
        g.add_argument("--dead-after", type=float, default=10.0)
        if name == "gateway":
            g.add_argument("--meta", required=True)
            g.add_argument("--node", action="append", default=[],
                           help="id=host:port[@zone] (may repeat)")
        else:
            g.add_argument("--dir", default="vault-data")
            g.add_argument("--nodes", type=int, default=6)
            g.add_argument("--zones", type=int, default=3)
            g.add_argument("--base-port", type=int, default=9100)

    args = p.parse_args(argv)
    if args.cmd == "node":
        node = StorageNode(args.id, args.dir, args.host, args.port, args.zone,
                           fsync=not args.no_fsync)
        print(f"node {args.id} ({args.zone}) serving {args.dir} on {args.host}:{args.port}",
              flush=True)
        node.serve_forever()
    elif args.cmd == "gateway":
        vault = Vault(args.meta, dead_after=args.dead_after)
        for spec in args.node:
            node_id, rest = spec.split("=", 1)
            addr, _, zone = rest.partition("@")
            vault.add_node(node_id, addr, zone or "zone-a")
        _serve_gateway(vault, args.host, args.port, args)
    else:
        root = Path(args.dir)
        root.mkdir(parents=True, exist_ok=True)
        vault = Vault(str(root / "meta.db"), dead_after=args.dead_after)
        for i in range(args.nodes):
            zone = f"zone-{chr(ord('a') + i % args.zones)}"
            node = StorageNode(f"n{i + 1}", root / f"n{i + 1}", args.host,
                               args.base_port + i, zone).start()
            vault.add_node(node.node_id, node.addr, zone)
            print(f"  node {node.node_id} {zone} {node.addr}")
        _serve_gateway(vault, args.host, args.port, args)


if __name__ == "__main__":
    main()
