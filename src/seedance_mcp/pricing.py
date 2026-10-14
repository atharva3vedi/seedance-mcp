"""BytePlus ModelArk list prices and the token math behind every estimate.

Rates are USD list prices from https://docs.byteplus.com/en/docs/ModelArk/model-pricing
(page last updated 2026-10-08). Prepaid token packs and discounts are not reflected.
Add or override models with a JSON file named by SEEDANCE_PRICES_FILE.

Seedance bills tokens = (input video seconds + output seconds) x width x height x fps / 1024.
Real tasks bill one extra output frame (fps x seconds + 1), which this module matches exactly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRICES_AS_OF = "2026-10-08"
FPS = 24
# Output pixel area per resolution. Seedance 2.x keeps the area constant across aspect ratios
# (720p is 1280x720 at 16:9 and 720x1280 at 9:16).
RESOLUTION_PIXELS = {"480p": 854 * 480, "720p": 1280 * 720, "1080p": 1920 * 1080, "4k": 3840 * 2160}
RESOLUTION_TIER = {"480p": "sd", "720p": "sd", "1080p": "1080p", "4k": "4k"}
TIER_RESOLUTIONS = {"sd": ["480p", "720p"], "1080p": ["1080p"], "4k": ["4k"]}
# Tasks with video input bill at least about 4 s of input video.
MIN_INPUT_VIDEO_SECONDS = 4.0
# Length assumed for a reference video this server has never seen.
ASSUMED_INPUT_VIDEO_SECONDS = 5.0
SMART_DURATION_SECONDS = 15

# rates: tier -> [USD per 1M tokens without video input, with video input]
VIDEO_MODELS: dict[str, dict[str, Any]] = {
    "dreamina-seedance-2-5-260628": {
        "name": "Seedance 2.5",
        "max_input_video_seconds": 30,
        "smart_duration": True,
        "rates": {"sd": [10.70, 6.40], "1080p": [11.70, 7.00]},
    },
    "dreamina-seedance-2-0-260128": {
        "name": "Seedance 2.0",
        "max_input_video_seconds": 15,
        "smart_duration": True,
        "rates": {"sd": [7.00, 4.30], "1080p": [7.70, 4.70], "4k": [4.00, 2.40]},
    },
    "dreamina-seedance-2-0-fast-260128": {
        "name": "Seedance 2.0 Fast",
        "max_input_video_seconds": 15,
        "smart_duration": False,
        "rates": {"sd": [5.60, 3.30]},
    },
    "dreamina-seedance-2-0-mini-260615": {
        "name": "Seedance 2.0 Mini",
        "max_input_video_seconds": 15,
        "smart_duration": False,
        "rates": {"sd": [3.50, 2.10]},
    },
}

IMAGE_MODELS: dict[str, dict[str, Any]] = {
    "dola-seedream-5-0-pro-260628": {
        "name": "Seedream 5.0 Pro",
        "per_image": 0.045,
        "per_large_image": 0.09,
        "large_above_pixels": 2_610_000,
        "per_extra_input_image": 0.003,
    },
    "dola-seedream-5-0-flash-260915": {"name": "Seedream 5.0 Flash", "per_image": 0.018},
    "seedream-5-0-lite-260128": {"name": "Seedream 5.0 Lite", "per_image": 0.035},
    "seedream-4-5-251128": {"name": "Seedream 4.5", "per_image": 0.04},
    "seedream-4-0-250828": {"name": "Seedream 4.0", "per_image": 0.03},
}

VIDEO_ALIASES = {
    "2.5": "dreamina-seedance-2-5-260628",
    "seedance-2.5": "dreamina-seedance-2-5-260628",
    "2.0": "dreamina-seedance-2-0-260128",
    "seedance-2.0": "dreamina-seedance-2-0-260128",
    "fast": "dreamina-seedance-2-0-fast-260128",
    "2.0-fast": "dreamina-seedance-2-0-fast-260128",
    "mini": "dreamina-seedance-2-0-mini-260615",
    "2.0-mini": "dreamina-seedance-2-0-mini-260615",
}

IMAGE_ALIASES = {
    "pro": "dola-seedream-5-0-pro-260628",
    "5.0-pro": "dola-seedream-5-0-pro-260628",
    "flash": "dola-seedream-5-0-flash-260915",
    "5.0-flash": "dola-seedream-5-0-flash-260915",
    "lite": "seedream-5-0-lite-260128",
    "5.0-lite": "seedream-5-0-lite-260128",
    "4.5": "seedream-4-5-251128",
    "4.0": "seedream-4-0-250828",
}

IMAGE_SIZE_PIXELS = {"1K": 1024 * 1024, "2K": 2048 * 2048, "3K": 3072 * 3072, "4K": 4096 * 4096}


class PricingError(ValueError):
    """A request the model can't do, caught before anything is spent."""


@dataclass
class Estimate:
    kind: str
    model: str
    label: str
    usd: float | None
    usd_max: float | None
    tokens: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def priced(self) -> bool:
        return self.usd is not None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"what": self.label, "usd": _r(self.usd)}
        if self.usd_max is not None and self.usd is not None and self.usd_max > self.usd + 0.005:
            out["usd_max"] = _r(self.usd_max)
        if self.tokens is not None:
            out["tokens"] = self.tokens
        if self.notes:
            out["notes"] = self.notes
        return out


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def money(value: float | None) -> str:
    if value is None:
        return "an unknown amount"
    if value == 0 or value >= 0.1:
        return f"${value:,.2f}"
    text = f"${value:.3f}"
    return text[:-1] if text.endswith("0") else text


def video_output_tokens(resolution: str, seconds: float) -> int:
    return int(RESOLUTION_PIXELS[resolution] * (FPS * seconds + 1) // 1024)


def video_input_tokens(resolution: str, seconds: float) -> int:
    return int(RESOLUTION_PIXELS[resolution] * FPS * seconds // 1024)


class PriceBook:
    def __init__(
        self,
        video: dict[str, dict[str, Any]] | None = None,
        image: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.video = {**VIDEO_MODELS, **(video or {})}
        self.image = {**IMAGE_MODELS, **(image or {})}

    @classmethod
    def load(cls, path: Path | None) -> PriceBook:
        if path is None:
            return cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(video=data.get("video"), image=data.get("image"))

    # Model names

    def resolve_video(self, model: str) -> str:
        return VIDEO_ALIASES.get(model.strip().lower(), model.strip())

    def resolve_image(self, model: str) -> str:
        return IMAGE_ALIASES.get(model.strip().lower(), model.strip())

    def video_name(self, model: str) -> str:
        return self.video.get(model, {}).get("name", model)

    def image_name(self, model: str) -> str:
        return self.image.get(model, {}).get("name", model)

    # Video

    def check_video(self, model: str, resolution: str, duration: int) -> None:
        info = self.video.get(model)
        if info is None:
            return
        tiers = info["rates"]
        if RESOLUTION_TIER.get(resolution) not in tiers:
            supported = [r for tier in tiers for r in TIER_RESOLUTIONS.get(tier, [])]
            raise PricingError(
                f"{info['name']} can't render {resolution}. It supports {', '.join(supported)}."
            )
        if duration == -1 and not info.get("smart_duration", True):
            raise PricingError(f"{info['name']} doesn't support smart duration (-1).")

    def estimate_video(
        self,
        model: str,
        *,
        resolution: str,
        duration: int,
        ratio: str | None = None,
        audio: bool = True,
        input_videos: list[float | None] | None = None,
    ) -> Estimate:
        if resolution not in RESOLUTION_PIXELS:
            raise PricingError(f"Unknown resolution {resolution!r}.")
        self.check_video(model, resolution, duration)
        info = self.video.get(model)
        notes: list[str] = []
        seconds: float = duration
        if duration == -1:
            seconds = SMART_DURATION_SECONDS
            notes.append(f"smart duration, priced at the {SMART_DURATION_SECONDS}s maximum")

        out_tokens = video_output_tokens(resolution, seconds)
        in_tokens = in_tokens_max = 0
        videos = input_videos or []
        if videos:
            cap = float(info["max_input_video_seconds"]) if info else 30.0
            known = sum(s for s in videos if s)
            unknown = sum(1 for s in videos if not s)
            likely = max(known + unknown * ASSUMED_INPUT_VIDEO_SECONDS, MIN_INPUT_VIDEO_SECONDS)
            worst = max(cap if unknown else known, likely)
            if unknown:
                notes.append(
                    f"{unknown} reference video(s) of unknown length, assumed "
                    f"{ASSUMED_INPUT_VIDEO_SECONDS:g}s each (worst case {cap:g}s of input)"
                )
            in_tokens = video_input_tokens(resolution, likely)
            in_tokens_max = video_input_tokens(resolution, worst)

        name = info["name"] if info else model
        parts = [name, f"{seconds:g}s" if duration != -1 else "smart length", resolution]
        if ratio and ratio != "adaptive":
            parts.append(ratio)
        if audio:
            parts.append("with audio")
        if videos:
            parts.append(f"{len(videos)} reference video(s)")
        label = ", ".join(parts)

        tokens = out_tokens + in_tokens
        if info is None:
            notes.append(f"no price on file for {model}; add it with SEEDANCE_PRICES_FILE")
            return Estimate("video", model, label, None, None, tokens, notes)
        rate = info["rates"][RESOLUTION_TIER[resolution]][1 if videos else 0]
        usd = tokens * rate / 1e6
        usd_max = (out_tokens + in_tokens_max) * rate / 1e6
        return Estimate("video", model, label, usd, usd_max, tokens, notes)

    def video_cost(
        self, model: str, tokens: int, resolution: str | None, video_input: bool
    ) -> float | None:
        info = self.video.get(model)
        if info is None:
            return None
        rates = info["rates"].get(RESOLUTION_TIER.get(resolution or "720p", "sd"))
        if rates is None:
            rates = next(iter(info["rates"].values()))
        return tokens * rates[1 if video_input else 0] / 1e6

    @staticmethod
    def infer_video_input(resolution: str | None, duration: int | None, tokens: int) -> bool:
        """Billed tokens above the output-only count mean the task had a reference video."""
        if resolution not in RESOLUTION_PIXELS or not duration or duration < 0:
            return False
        return tokens > video_output_tokens(resolution, duration) * 1.01

    # Images

    @staticmethod
    def image_pixels(size: str) -> int | None:
        if size.upper() in IMAGE_SIZE_PIXELS:
            return IMAGE_SIZE_PIXELS[size.upper()]
        match = re.fullmatch(r"(\d{2,5})\s*[xX*]\s*(\d{2,5})", size.strip())
        return int(match.group(1)) * int(match.group(2)) if match else None

    def image_cost(self, model: str, size: str, images: int, input_images: int = 0) -> float | None:
        info = self.image.get(model)
        if info is None:
            return None
        per = info["per_image"]
        pixels = self.image_pixels(size)
        if "per_large_image" in info and (pixels is None or pixels > info["large_above_pixels"]):
            per = info["per_large_image"]
        extra = info.get("per_extra_input_image", 0.0) * max(0, input_images - 1)
        return per * images + extra

    def estimate_image(
        self, model: str, *, size: str = "2K", count: int = 1, input_images: int = 0
    ) -> Estimate:
        name = self.image_name(model)
        label = f"{name}, {count} image{'s' if count != 1 else ''} at {size}"
        if input_images:
            label += f", {input_images} reference image(s)"
        usd = self.image_cost(model, size, count, input_images)
        notes = [] if usd is not None else [f"no price on file for {model}"]
        return Estimate("image", model, label, usd, usd, None, notes)
