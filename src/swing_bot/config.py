from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AppConfig:
    raw: dict[str, Any]
    root: Path

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"Config section '{name}' must be a mapping")
        return value

    def path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root / path


def load_config(path: str | Path = "config/settings.yaml") -> AppConfig:
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("Top-level YAML configuration must be a mapping")
    return AppConfig(raw=raw, root=config_path.parent.parent)
