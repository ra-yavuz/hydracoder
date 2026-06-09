"""hydracoder entry point: serve the web UI, or run a goal headless from the CLI."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


DISCLAIMER = (
    "hydracoder is provided AS IS, WITHOUT WARRANTY OF ANY KIND. It runs local "
    "models that read, write, and delete files in a workspace and run shell "
    "commands. You alone are responsible for any damage to your data, hardware, "
    "or system. By running it you accept all risk."
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hydracoder",
                                 description="local-AI coding orchestrator with a web UI.",
                                 epilog=DISCLAIMER)
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="start the web UI + orchestrator")
    s.add_argument("--workspace", default="./hydracoder-workspace")
    s.add_argument("--run-dir", default="./hydracoder-run")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--http-port", type=int, default=8765)
    s.add_argument("--ws-port", type=int, default=8766)

    r = sub.add_parser("run", help="run a goal headless (no UI) and print the result")
    r.add_argument("goal")
    r.add_argument("--workspace", default="./hydracoder-workspace")
    r.add_argument("--run-dir", default="./hydracoder-run")

    args = ap.parse_args(argv)

    if args.version:
        print(f"hydracoder {__version__}")
        return 0

    if args.cmd == "serve":
        from .server import HydracoderServer
        srv = HydracoderServer(Path(args.run_dir), Path(args.workspace),
                               host=args.host, http_port=args.http_port,
                               ws_port=args.ws_port)
        srv.serve_forever()
        return 0

    if args.cmd == "run":
        from .orchestrator import Orchestrator
        orch = Orchestrator(Path(args.run_dir), Path(args.workspace),
                            log=lambda m: print("[hydracoder]", m, flush=True))
        print(DISCLAIMER)
        result = orch.run(args.goal)
        print("result:", result)
        return 0 if result.get("ok") else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
