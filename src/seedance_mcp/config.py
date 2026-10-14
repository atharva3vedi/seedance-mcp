"""Runtime settings, read from flags, the environment, and ~/.seedance-mcp/.env."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

PERIODS = ("day", "week", "month")
APPROVAL_MODES = ("auto", "dialog", "chat", "off")


@dataclass(frozen=True)
class Settings:
    api_key: str | None = None
    base_url: str = "https://ark.ap-southeast.bytepluses.com/api/v3"
    video_model: str = "dreamina-seedance-2-0-mini-260615"
    image_model: str = "dola-seedream-5-0-pro-260628"
    # Spend guard
    ask_above_usd: float = 0.50
    budget_usd: float = 20.0
    budget_period: str = "month"
    hard_cap_usd: float | None = None
    approval: str = "auto"
    # Files
    home: Path = Path.home() / ".seedance-mcp"
    output_dir: Path | None = None
    prices_file: Path | None = None
    poll_seconds: float = 5.0

    @property
    def ledger_path(self) -> Path:
        return self.home / "ledger.jsonl"

    @classmethod
    def load(
        cls,
        overrides: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Settings:
        """Flags beat environment variables, which beat ~/.seedance-mcp/.env."""
        env = dict(os.environ if environ is None else environ)
        home = Path(env.get("SEEDANCE_HOME") or Path.home() / ".seedance-mcp").expanduser()
        file_values = {k: v for k, v in dotenv_values(home / ".env").items() if v is not None}
        merged = {**file_values, **env}

        def get(name: str) -> str | None:
            value = (merged.get(name) or "").strip()
            return value or None

        def number(name: str, default: float | None) -> float | None:
            raw = get(name)
            if raw is None:
                return default
            try:
                return float(raw.lstrip("$"))
            except ValueError as exc:
                raise ValueError(f"{name} must be a number, got {raw!r}") from exc

        settings = cls(
            api_key=get("ARK_API_KEY"),
            base_url=(get("ARK_BASE_URL") or cls.base_url).rstrip("/"),
            video_model=get("SEEDANCE_VIDEO_MODEL") or cls.video_model,
            image_model=get("SEEDANCE_IMAGE_MODEL") or cls.image_model,
            ask_above_usd=number("SEEDANCE_ASK_ABOVE_USD", cls.ask_above_usd),
            budget_usd=number("SEEDANCE_BUDGET_USD", cls.budget_usd),
            budget_period=(get("SEEDANCE_BUDGET_PERIOD") or cls.budget_period).lower(),
            hard_cap_usd=number("SEEDANCE_HARD_CAP_USD", None),
            approval=(get("SEEDANCE_APPROVAL") or cls.approval).lower(),
            home=home,
            output_dir=Path(p).expanduser() if (p := get("SEEDANCE_OUTPUT_DIR")) else None,
            prices_file=Path(p).expanduser() if (p := get("SEEDANCE_PRICES_FILE")) else None,
        )
        if overrides:
            settings = replace(settings, **{k: v for k, v in overrides.items() if v is not None})
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.budget_period not in PERIODS:
            raise ValueError(f"budget period must be one of {', '.join(PERIODS)}")
        if self.approval not in APPROVAL_MODES:
            raise ValueError(f"approval mode must be one of {', '.join(APPROVAL_MODES)}")
        for name in ("ask_above_usd", "budget_usd"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} can't be negative")
        if self.hard_cap_usd is not None and self.hard_cap_usd < 0:
            raise ValueError("hard cap can't be negative")
