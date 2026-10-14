"""The spend guard: a local ledger, budgets, and human approvals.

Every generation is priced before it is sent. Cheap requests inside the budget go straight
through. Anything over the ask-first limit, past the budget, or without a known price needs a
human yes: through an MCP elicitation dialog when the client supports one, otherwise through a
one-time approval_id that the agent may only use after the user agrees in chat. Nothing ever
goes past the hard cap.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Settings
from .pricing import RESOLUTION_PIXELS, PriceBook, money, video_output_tokens

FREE_STATUSES = {"failed", "cancelled", "expired"}
PERIOD_LABEL = {"day": "today", "week": "this week", "month": "this month"}
SINGLE_TTL = 15 * 60
BATCH_TTL = 3 * 60 * 60
SYNC_EVERY = 30.0


class GuardError(Exception):
    pass


def period_start(period: str, now: float) -> float:
    start = datetime.fromtimestamp(now).astimezone()
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start -= timedelta(days=start.weekday())
    elif period == "month":
        start = start.replace(day=1)
    return start.timestamp()


def url_key(url: str | None) -> str | None:
    """Signed URLs change their query string; the path identifies the file."""
    if not url:
        return None
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def record_cost(record: dict[str, Any]) -> float:
    if record.get("status") in FREE_STATUSES:
        return 0.0
    if record.get("usd") is not None:
        return float(record["usd"])
    return float(record.get("est_usd") or 0.0)


class Ledger:
    """Append-only JSON lines file. The last line for an id wins, so several server
    processes (one per open Claude session) can share it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: dict[str, dict[str, Any]] = {}
        self._stamp: tuple[int, int] | None = None

    def _load(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._records, self._stamp = {}, None
            return
        stamp = (stat.st_size, stat.st_mtime_ns)
        if stamp == self._stamp:
            return
        records: dict[str, dict[str, Any]] = {}
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("id"):
                    records[record["id"]] = record
        self._records, self._stamp = records, stamp

    def all(self) -> list[dict[str, Any]]:
        self._load()
        return list(self._records.values())

    def get(self, record_id: str) -> dict[str, Any] | None:
        self._load()
        return self._records.get(record_id)

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        self._load()
        current = self._records.get(record["id"], {})
        merged = {**current, **{k: v for k, v in record.items() if v is not None}}
        if merged == current:
            return current
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(merged, separators=(",", ":")) + "\n")
        self._records[record["id"]] = merged
        stat = self.path.stat()
        self._stamp = (stat.st_size, stat.st_mtime_ns)
        return merged


@dataclass
class Decision:
    spent: float
    after: float
    reasons: list[str] = field(default_factory=list)
    needs_approval: bool = False
    blocked: str | None = None


@dataclass
class Approval:
    id: str
    kind: str  # "single" (one exact request) or "batch" (a dollar envelope)
    max_usd: float
    remaining_usd: float
    calls_left: int
    expires_at: float
    confirmed: bool
    summary: str
    fingerprint: str | None = None

    def view(self, now: float) -> dict[str, Any]:
        return {
            "approval_id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "confirmed": self.confirmed,
            "max_usd": round(self.max_usd, 3),
            "remaining_usd": round(self.remaining_usd, 3),
            "calls_left": self.calls_left,
            "expires_in_min": max(0, round((self.expires_at - now) / 60)),
        }


class SpendGuard:
    def __init__(
        self,
        settings: Settings,
        prices: PriceBook,
        ledger: Ledger,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.prices = prices
        self.ledger = ledger
        self.clock = clock
        self._approvals: dict[str, Approval] = {}
        self._last_sync = float("-inf")
        self.last_sync_error: str | None = None

    # Accounting

    def period_records(self) -> list[dict[str, Any]]:
        start = period_start(self.settings.budget_period, self.clock())
        return [r for r in self.ledger.all() if float(r.get("created") or 0) >= start]

    def spent(self) -> float:
        return round(sum(record_cost(r) for r in self.period_records()), 4)

    def snapshot(self) -> dict[str, Any]:
        s = self.settings
        spent = self.spent()
        out: dict[str, Any] = {
            "period": s.budget_period,
            "spent_usd": round(spent, 2),
            "budget_usd": s.budget_usd,
            "left_usd": round(max(0.0, s.budget_usd - spent), 2),
        }
        if s.hard_cap_usd is not None:
            out["hard_cap_usd"] = s.hard_cap_usd
        return out

    def known_video_seconds(self, url: str) -> float | None:
        key = url_key(url)
        for record in self.ledger.all():
            if record.get("video_key") == key and record.get("duration"):
                return float(record["duration"])
        return None

    def record_task(self, task: dict[str, Any], **extra: Any) -> dict[str, Any]:
        """Fold a ModelArk task (from create, get or list) into the ledger with its cost."""
        task_id = str(task.get("id") or extra.get("id"))
        existing = self.ledger.get(task_id) or {}
        model = task.get("model") or existing.get("model") or extra.get("model")
        resolution = task.get("resolution") or existing.get("resolution")
        duration = task.get("duration") or existing.get("duration")
        status = task.get("status") or existing.get("status") or "queued"
        content = task.get("content") if isinstance(task.get("content"), dict) else {}
        record: dict[str, Any] = {
            "id": task_id,
            "kind": "video",
            "model": model,
            "status": status,
            "resolution": resolution,
            "duration": duration,
            "created": existing.get("created") or task.get("created_at") or self.clock(),
            "video_key": url_key(content.get("video_url")),
            **extra,
        }
        if not existing and "label" not in extra:
            name = self.prices.video_name(model) if model else "Seedance"
            record["label"] = f"{name}, {duration}s {resolution}"
            record["source"] = extra.get("source", "account")
            if resolution in RESOLUTION_PIXELS and duration and duration > 0:
                record["est_usd"] = self.prices.video_cost(
                    model, video_output_tokens(resolution, duration), resolution, False
                )
        tokens = (task.get("usage") or {}).get("completion_tokens")
        if tokens is not None:
            video_input = existing.get("video_input")
            if video_input is None:
                video_input = self.prices.infer_video_input(resolution, duration, tokens)
            record["tokens"] = tokens
            record["video_input"] = video_input
            record["usd"] = self.prices.video_cost(model, tokens, resolution, video_input)
        if status in FREE_STATUSES:
            record["usd"] = 0.0
        return self.ledger.upsert(record)

    async def sync(self, ark: Any, *, force: bool = False) -> None:
        """Pull the account's recent video tasks so spending from other tools, scripts and
        sessions on the same key counts too. Best effort, at most every 30 s."""
        now = self.clock()
        if not ark.api_key or (not force and now - self._last_sync < SYNC_EVERY):
            return
        self._last_sync = now
        start = period_start(self.settings.budget_period, now)
        try:
            for page in range(1, 6):
                data = await ark.list_tasks(page_num=page, page_size=100)
                items = data.get("items") or []
                for task in items:
                    self.record_task(task)
                oldest = min((float(t.get("created_at") or now) for t in items), default=0)
                if len(items) < 100 or oldest < start:
                    break
            self.last_sync_error = None
        except Exception as exc:  # noqa: BLE001 - never block a spend check on sync
            self.last_sync_error = str(exc)[:200]

    # Decisions

    def evaluate(self, usd_max: float | None) -> Decision:
        s = self.settings
        spent = self.spent()
        after = spent + (usd_max or 0.0)
        period = PERIOD_LABEL[s.budget_period]
        if s.hard_cap_usd is not None and after > s.hard_cap_usd + 1e-9:
            return Decision(
                spent,
                after,
                blocked=(
                    f"Blocked by your hard cap: this would bring {period} to {money(after)}, past "
                    f"the {money(s.hard_cap_usd)} cap ({money(spent)} spent so far). Raise "
                    "SEEDANCE_HARD_CAP_USD to go further. An approval can't override it."
                ),
            )
        reasons = []
        if usd_max is None:
            reasons.append("there is no price on file for this model, so the cost is unknown")
        elif usd_max > s.ask_above_usd + 1e-9:
            reasons.append(f"it is over your {money(s.ask_above_usd)} ask-first limit")
        if after > s.budget_usd + 1e-9:
            reasons.append(f"it takes you past your {money(s.budget_usd)} budget for {period}")
        return Decision(spent, after, reasons, needs_approval=bool(reasons) and s.approval != "off")

    def ask_text(self, what: str, usd: float | None, usd_max: float | None, d: Decision) -> str:
        s = self.settings
        cost = money(usd)
        if usd is not None and usd_max is not None and usd_max > usd + 0.005:
            cost = f"{money(usd)} (up to {money(usd_max)})"
        period = PERIOD_LABEL[s.budget_period]
        lines = [
            f"Spend about {cost} on {what}?",
            f"{period.capitalize()}: {money(d.spent)} spent of your {money(s.budget_usd)} "
            f"budget. After this: {money(d.after)}.",
        ]
        if d.reasons:
            lines.append("Asking because " + " and ".join(d.reasons) + ".")
        return "\n".join(lines)

    # Approvals

    def _new_id(self) -> str:
        return "apv_" + secrets.token_urlsafe(9)

    def _prune(self) -> None:
        now = self.clock()
        for key in [k for k, a in self._approvals.items() if a.expires_at < now]:
            del self._approvals[key]

    def open_single(self, usd_max: float | None, fingerprint: str, summary: str) -> Approval:
        self._prune()
        approval = Approval(
            id=self._new_id(),
            kind="single",
            max_usd=usd_max or 0.0,
            remaining_usd=usd_max or 0.0,
            calls_left=1,
            expires_at=self.clock() + SINGLE_TTL,
            confirmed=False,
            summary=summary,
            fingerprint=fingerprint,
        )
        self._approvals[approval.id] = approval
        return approval

    def open_batch(self, usd_max: float, calls: int, summary: str, confirmed: bool) -> Approval:
        self._prune()
        approval = Approval(
            id=self._new_id(),
            kind="batch",
            max_usd=usd_max,
            remaining_usd=usd_max,
            calls_left=calls,
            expires_at=self.clock() + BATCH_TTL,
            confirmed=confirmed,
            summary=summary,
        )
        self._approvals[approval.id] = approval
        return approval

    def get_approval(self, approval_id: str) -> Approval:
        self._prune()
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise GuardError(
                "That approval_id is unknown or expired. Ask the user again; never reuse old ids."
            )
        return approval

    def confirm_batch(self, approval_id: str) -> Approval:
        approval = self.get_approval(approval_id)
        if approval.kind != "batch":
            raise GuardError("That approval_id is for a single request, not a batch.")
        approval.confirmed = True
        return approval

    def redeem(self, approval_id: str, usd_max: float | None, fingerprint: str) -> Approval:
        approval = self.get_approval(approval_id)
        if approval.kind == "single":
            if approval.fingerprint != fingerprint:
                raise GuardError(
                    "This approval was for a request with different settings. The user approved "
                    "that exact request; ask them again for this one."
                )
            approval.confirmed = True
        elif not approval.confirmed:
            raise GuardError(
                "This batch approval was never confirmed. Show the user the total, and after "
                "they agree call preapprove_spend again with this approval_id."
            )
        if approval.calls_left <= 0:
            raise GuardError("This approval is used up. Ask the user before spending more.")
        if usd_max is None and approval.kind == "batch":
            raise GuardError("This model has no known price, so it needs its own approval.")
        cost = usd_max or 0.0
        if approval.kind == "batch" and cost > approval.remaining_usd + 0.005:
            raise GuardError(
                f"This request (up to {money(cost)}) is more than the "
                f"{money(approval.remaining_usd)} left on the approved batch. Ask the user first."
            )
        approval.calls_left -= 1
        approval.remaining_usd = max(0.0, approval.remaining_usd - cost)
        if approval.kind == "single":
            del self._approvals[approval.id]
        return approval

    def refund(self, approval: Approval, usd_max: float | None) -> None:
        """Give a batch its money back when the provider rejected the request."""
        if approval.kind == "batch" and approval.id in self._approvals:
            approval.calls_left += 1
            approval.remaining_usd = min(approval.max_usd, approval.remaining_usd + (usd_max or 0))

    def open_approvals(self) -> list[dict[str, Any]]:
        self._prune()
        now = self.clock()
        return [a.view(now) for a in self._approvals.values()]
