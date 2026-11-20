"""Command line: `python -m jrwi serve` (default), `check` or `reparse`."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import VERSION


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jrwi", description="JRWI members archive service")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "check", "reparse"],
                    help="serve: web app and scheduled checks (default); check: one check and exit; "
                         "reparse: re-read saved pages and exit")
    ap.add_argument("--full", action="store_true", help="with check: read the whole feed")
    ap.add_argument("--host", default=os.environ.get("JRWI_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("JRWI_PORT", "8090")))
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("JRWI_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "serve":
        import uvicorn

        from .web.app import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning", proxy_headers=True,
                    forwarded_allow_ips="*")
        return 0

    from .config import Paths
    from .crawler import Archiver
    from .db import Db
    paths = Paths.from_env()
    archiver = Archiver(Db(paths.data / "jrwi.db"), paths)
    if args.command == "check":
        result = archiver.run_check(full=args.full)
        print(result)
        return 0 if result["result"] == "ok" else 1
    print(archiver.run_reparse())
    return 0


if __name__ == "__main__":
    sys.exit(main())
