from __future__ import annotations

import json
import logging
import sys

from swing_bot.config import load_config
from swing_bot.entry_receipt import install_entry_receipt
from swing_bot.single_account_runtime import run_single_account_auto


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        install_entry_receipt()
        config = load_config("config/settings.yaml")
        result = run_single_account_auto(config)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        logging.getLogger(__name__).exception("Responsive bot run failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
