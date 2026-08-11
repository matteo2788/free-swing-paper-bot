from __future__ import annotations

import json
import logging
import sys

from swing_bot.config import load_config
from swing_bot.responsive_runtime import run_responsive_auto


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        config = load_config("config/settings.yaml")
        result = run_responsive_auto(config)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        logging.getLogger(__name__).exception("Responsive bot run failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
