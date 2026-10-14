"""Command line: run the MCP server (default) or print what you've spent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .config import APPROVAL_MODES, PERIODS, Settings
from .guard import FREE_STATUSES, PERIOD_LABEL, Ledger, SpendGuard, record_cost
from .pricing import PriceBook, money


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seedance-mcp",
        description="Seedance + Seedream MCP server with a spend guard.",
    )
    parser.add_argument("--version", action="version", version=f"seedance-mcp {__version__}")
    parser.add_argument("--budget", type=float, metavar="USD", help="budget per period (20)")
    parser.add_argument("--period", choices=PERIODS, help="budget period (month)")
    parser.add_argument(
        "--ask-above",
        type=float,
        metavar="USD",
        help="ask before any single spend above this (0.50)",
    )
    parser.add_argument("--hard-cap", type=float, metavar="USD", help="never spend past this")
    parser.add_argument("--approval", choices=APPROVAL_MODES, help="how to ask (auto)")
    parser.add_argument("--output-dir", metavar="DIR", help="save every result here")
    parser.add_argument("command", nargs="?", choices=["serve", "spend"], default="serve")
    return parser


def _print_spend(settings: Settings) -> None:
    from .ark import Ark

    prices = PriceBook.load(settings.prices_file)
    guard = SpendGuard(settings, prices, Ledger(settings.ledger_path))
    if settings.api_key:
        asyncio.run(guard.sync(Ark(settings.api_key, settings.base_url), force=True))
    spent = guard.spent()
    budget = settings.budget_usd
    share = spent / budget if budget else 1.0
    filled = min(20, round(share * 20))
    period = PERIOD_LABEL[settings.budget_period]
    print(f"seedance-mcp: {period}")
    print(f"  {money(spent)} of {money(budget)}  [{'#' * filled}{'.' * (20 - filled)}] {share:.0%}")
    cap = money(settings.hard_cap_usd) if settings.hard_cap_usd is not None else "none"
    print(f"  ask first above {money(settings.ask_above_usd)}, hard cap {cap}")
    rows: dict[str, list[float]] = {}
    records = [r for r in guard.period_records() if r.get("status") not in FREE_STATUSES]
    for record in records:
        model = record.get("model") or "?"
        is_image = record.get("kind") == "image"
        name = prices.image_name(model) if is_image else prices.video_name(model)
        row = rows.setdefault(name, [0, 0.0])
        row[0] += record.get("images", 1) if is_image else 1
        row[1] += record_cost(record)
    for name, (count, usd) in sorted(rows.items(), key=lambda kv: -kv[1][1]):
        print(f"    {name:<20} {int(count):>4}  {money(usd):>9}")
    recent = sorted(records, key=lambda r: r.get("created") or 0, reverse=True)[:5]
    if recent:
        print("  latest:")
    for record in recent:
        when = time.strftime("%m-%d %H:%M", time.localtime(record.get("created") or 0))
        print(f"    {when}  {money(record_cost(record)):>8}  {record.get('label', '')}")
    if guard.last_sync_error:
        print(f"  (could not sync with BytePlus: {guard.last_sync_error})")


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    overrides = {
        "budget_usd": args.budget,
        "budget_period": args.period,
        "ask_above_usd": args.ask_above,
        "hard_cap_usd": args.hard_cap,
        "approval": args.approval,
        "output_dir": Path(args.output_dir).expanduser() if args.output_dir else None,
    }
    try:
        settings = Settings.load(overrides)
    except ValueError as exc:
        sys.exit(f"seedance-mcp: {exc}")
    if args.command == "spend":
        _print_spend(settings)
        return
    from .server import create_server

    logging.getLogger("httpx").setLevel(logging.WARNING)
    create_server(settings).run(transport="stdio")


if __name__ == "__main__":
    main()
