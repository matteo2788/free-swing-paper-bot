from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_config
from .quant_runtime import build_quant_engine, run_quant_auto


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Quant paper-trading swing scanner")
    parser.add_argument("--config", default="config/settings.yaml", help="Path to YAML configuration")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts instead of posting to Discord")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("auto", help="Automatically choose scan, risk-off, after-close, or idle mode")
    subparsers.add_parser("scan", help="Run one macro-gated quant scan")
    subparsers.add_parser("refresh", help="Refresh the daily context and sector-relative-strength pool")
    subparsers.add_parser("test-discord", help="Send a Discord connection test")
    subparsers.add_parser("status", help="Print saved quant bot state summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "auto"
    try:
        config = load_config(args.config)
        if command == "auto":
            result = run_quant_auto(config, dry_run=args.dry_run)
            print(json.dumps(result, indent=2))
            return 0

        engine = build_quant_engine(config, dry_run=args.dry_run)
        if command == "scan":
            result = engine.scan()
            print(json.dumps(result, indent=2))
        elif command == "refresh":
            count = engine.refresh_daily_pool()
            print(json.dumps({"eligible": count}, indent=2))
        elif command == "test-discord":
            if not args.dry_run and not engine.alerter.configured:
                raise RuntimeError("DISCORD_WEBHOOK_URL is missing")
            success = engine.send_test()
            print(json.dumps({"success": success}, indent=2))
        elif command == "status":
            engine.print_status()
        return 0
    except Exception as exc:
        logging.getLogger(__name__).exception("Quant bot command failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
