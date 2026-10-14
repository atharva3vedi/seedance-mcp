"""MCP tools: generate, track, and pay for Seedance video and Seedream images."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field, create_model

from . import __version__
from .ark import Ark, ArkError, require_url, to_image_url
from .config import Settings
from .guard import (
    FREE_STATUSES,
    PERIOD_LABEL,
    Approval,
    GuardError,
    Ledger,
    SpendGuard,
    record_cost,
)
from .pricing import PRICES_AS_OF, Estimate, PriceBook, PricingError, money

INSTRUCTIONS = """\
Seedance (video) and Seedream (images) on the user's own BytePlus ModelArk key. Every \
generation costs real money, so a spend guard sits in front of it.

Spend rules:
- Talk about cost in dollars, never tokens.
- Before more than one generation, call estimate_cost and tell the user the dollar total, then \
call preapprove_spend once and pass its approval_id to every generate call in the batch.
- If a tool returns status "needs_your_ok", show the user its ask_user text and wait for an \
explicit yes. Only then call the same tool again with that approval_id. Never decide for them.
- Drafts and tests: model "mini" at 480p. Finals: model "2.5". Reuse clips instead of re-rendering.

Video is async: generate_video returns a task_id. Pass wait=true to block until it finishes, or \
call video_task later. Result URLs expire after 24 hours, so pass save_to to keep a copy.
Seedance 2.x rejects realistic human faces as image input. To keep a person consistent, \
generate them text-to-video first, then pass that clip in reference_videos.
"""

Ratio = Literal["adaptive", "16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
Resolution = Literal["480p", "720p", "1080p", "4k"]
TERMINAL = {"succeeded"} | FREE_STATUSES


class PlanItem(BaseModel):
    """One line of a planned batch."""

    kind: Literal["video", "image"] = "video"
    count: int = Field(1, ge=1, le=500, description="How many of this item")
    model: str | None = Field(None, description="Model id or alias: 2.5, 2.0, fast, mini, pro")
    duration: int = Field(5, ge=-1, le=15, description="Video seconds (-1 = smart length)")
    resolution: Resolution = "720p"
    reference_videos: int = Field(0, ge=0, le=3, description="Reference videos per clip")
    reference_video_seconds: float | None = Field(
        None, description="Length of each reference video, if known"
    )
    size: str = Field("2K", description="Image size: 1K, 2K, 4K or WIDTHxHEIGHT")
    reference_images: int = Field(0, ge=0, le=10, description="Input images per image request")


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]


def _can_elicit(ctx: Context) -> bool:
    try:
        params = ctx.session.client_params
    except Exception:  # noqa: BLE001
        return False
    caps = getattr(params, "capabilities", None)
    return getattr(caps, "elicitation", None) is not None


def _resolve_dest(save_to: str, default_name: str) -> Path:
    path = Path(save_to).expanduser()
    if path.is_dir() or not path.suffix:
        return path / default_name
    return path


def _task_view(task: dict[str, Any]) -> dict[str, Any]:
    content = task.get("content") if isinstance(task.get("content"), dict) else {}
    view = {
        "task_id": task.get("id"),
        "status": task.get("status"),
        "model": task.get("model"),
        "video_url": content.get("video_url"),
        "last_frame_url": content.get("last_frame_url"),
        "duration": task.get("duration"),
        "resolution": task.get("resolution"),
        "ratio": task.get("ratio"),
        "error": task.get("error"),
    }
    return {k: v for k, v in view.items() if v is not None}


def create_server(settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> FastMCP:
    prices = PriceBook.load(settings.prices_file)
    guard = SpendGuard(settings, prices, Ledger(settings.ledger_path))
    ark = Ark(settings.api_key, settings.base_url, client=http_client)
    mcp = FastMCP("seedance", instructions=INSTRUCTIONS, log_level="WARNING")

    # Guard plumbing

    async def ask_in_dialog(ctx: Context, message: str, usd_max: float | None) -> bool | None:
        """True/False from the human, or None if the client can't show a dialog."""
        label = f"Yes, spend up to {money(usd_max)}" if usd_max is not None else "Yes, spend it"
        schema = create_model(
            "SpendApproval",
            approve=(bool, Field(default=False, title=label, description="Tick, then Accept")),
        )
        try:
            result = await ctx.elicit(message=message, schema=schema)
        except Exception:  # noqa: BLE001 - the client refused or can't elicit: fall back to chat
            return None
        return result.action == "accept" and bool(getattr(result.data, "approve", False))

    async def authorize(
        ctx: Context, est: Estimate, fingerprint: str, approval_id: str | None
    ) -> tuple[Approval | str | None, dict[str, Any] | None]:
        """Returns (how it was approved, None) to go ahead, or (None, response) to stop."""
        await guard.sync(ark)
        decision = guard.evaluate(est.usd_max)
        if decision.blocked:
            return None, {
                "ok": False,
                "status": "blocked_by_hard_cap",
                "message": decision.blocked,
                "spend": guard.snapshot(),
            }
        if approval_id:
            try:
                return guard.redeem(approval_id, est.usd_max, fingerprint), None
            except GuardError as exc:
                return None, {"ok": False, "status": "approval_invalid", "message": str(exc)}
        if not decision.needs_approval:
            return ("off" if decision.reasons else "auto"), None

        message = guard.ask_text(est.label, est.usd, est.usd_max, decision)
        if settings.approval in ("auto", "dialog") and _can_elicit(ctx):
            answer = await ask_in_dialog(ctx, message, est.usd_max)
            if answer is True:
                return "dialog", None
            if answer is False:
                return None, {
                    "ok": False,
                    "status": "declined",
                    "message": "The user declined in the approval dialog. Nothing was spent.",
                }
        if settings.approval == "dialog":
            return None, {
                "ok": False,
                "status": "dialog_unavailable",
                "message": "This client can't show approval dialogs and SEEDANCE_APPROVAL=dialog "
                "forbids chat approvals. Nothing was spent.",
            }
        ticket = guard.open_single(est.usd_max, fingerprint, est.label)
        return None, {
            "ok": False,
            "status": "needs_your_ok",
            "cost": est.as_dict(),
            "ask_user": message,
            "approval_id": ticket.id,
            "next": "Show ask_user to the user and wait for an explicit yes. Only then call "
            "this tool again with the same arguments plus approval_id. If they say no, stop.",
        }

    def approved_label(approved: Approval | str) -> str:
        return f"{approved.kind}:{approved.id}" if isinstance(approved, Approval) else approved

    async def settle(task: dict[str, Any], save_to: str | None) -> dict[str, Any]:
        record = guard.record_task(task)
        view = _task_view(task)
        if record.get("usd") is not None and task.get("status") == "succeeded":
            view["cost_usd"] = round(record["usd"], 3)
            if record.get("tokens"):
                view["tokens"] = record["tokens"]
        target = save_to or (str(settings.output_dir) if settings.output_dir else None)
        if not save_to and record.get("file") and Path(record["file"]).exists():
            view["saved_to"] = record["file"]
        elif view.get("video_url") and target:
            try:
                dest = _resolve_dest(target, f"{task.get('id')}.mp4")
                view["saved_to"] = str(await ark.download(view["video_url"], dest))
                guard.ledger.upsert({"id": record["id"], "file": view["saved_to"]})
            except (ArkError, OSError) as exc:
                view["save_error"] = str(exc)
        return view

    async def wait_for(ctx: Context, task_id: str, timeout_s: float) -> dict[str, Any]:
        started = time.monotonic()
        while True:
            task = await ark.get_task(task_id)
            status = task.get("status")
            elapsed = time.monotonic() - started
            if status in TERMINAL or elapsed >= timeout_s:
                return task
            await ctx.report_progress(elapsed, timeout_s, f"{status}, {int(elapsed)}s")
            await asyncio.sleep(settings.poll_seconds)

    # Generation

    @mcp.tool()
    async def generate_video(
        ctx: Context,
        prompt: Annotated[str, Field(description="What happens in the clip")],
        model: Annotated[
            str | None, Field(description="2.5 (best), 2.0, fast, mini (cheap drafts) or a full id")
        ] = None,
        duration: Annotated[int, Field(ge=-1, le=15, description="Seconds, 2-15")] = 5,
        resolution: Resolution = "720p",
        ratio: Ratio = "adaptive",
        generate_audio: bool = True,
        watermark: bool = False,
        first_frame: Annotated[str | None, Field(description="Image URL or local path")] = None,
        last_frame: Annotated[str | None, Field(description="Image URL or local path")] = None,
        reference_images: Annotated[
            list[str] | None, Field(description="Image URLs or local paths")
        ] = None,
        reference_videos: Annotated[
            list[str] | None, Field(description="Public video URLs, e.g. an earlier video_url")
        ] = None,
        reference_audio: Annotated[list[str] | None, Field(description="Audio URLs")] = None,
        seed: int | None = None,
        camera_fixed: bool | None = None,
        return_last_frame: bool = False,
        wait: Annotated[bool, Field(description="Block until the video is done")] = False,
        timeout_s: Annotated[int, Field(ge=10, le=1800)] = 300,
        save_to: Annotated[str | None, Field(description="File or folder to save the mp4")] = None,
        approval_id: Annotated[
            str | None, Field(description="Only after the user said yes, or from a batch")
        ] = None,
    ) -> dict[str, Any]:
        """Make a Seedance video (text-to-video, image-to-video, or with reference media).
        Priced and checked by the spend guard before anything is sent."""
        model_id = prices.resolve_video(model or settings.video_model)
        try:
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for role, value in (("first_frame", first_frame), ("last_frame", last_frame)):
                if value:
                    content.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": to_image_url(value)},
                            "role": role,
                        }
                    )
            for value in reference_images or []:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": to_image_url(value)},
                        "role": "reference_image",
                    }
                )
            videos = [require_url(v, "reference_videos") for v in reference_videos or []]
            for value in videos:
                content.append(
                    {"type": "video_url", "video_url": {"url": value}, "role": "reference_video"}
                )
            for value in reference_audio or []:
                content.append(
                    {
                        "type": "audio_url",
                        "audio_url": {"url": require_url(value, "reference_audio")},
                        "role": "reference_audio",
                    }
                )
            est = prices.estimate_video(
                model_id,
                resolution=resolution,
                duration=duration,
                ratio=ratio,
                audio=generate_audio,
                input_videos=[guard.known_video_seconds(v) for v in videos],
            )
        except ArkError as exc:
            return exc.as_dict()
        except PricingError as exc:
            return {"ok": False, "status": "invalid_request", "message": str(exc)}

        payload: dict[str, Any] = {
            "model": model_id,
            "content": content,
            "resolution": resolution,
            "ratio": ratio,
            "duration": duration,
            "generate_audio": generate_audio,
            "watermark": watermark,
        }
        if seed is not None:
            payload["seed"] = seed
        if camera_fixed is not None:
            payload["camera_fixed"] = camera_fixed
        if return_last_frame:
            payload["return_last_frame"] = True

        approved, stop = await authorize(ctx, est, _fingerprint(payload), approval_id)
        if stop:
            return stop
        try:
            created = await ark.create_video_task(payload)
        except ArkError as exc:
            if isinstance(approved, Approval):
                guard.refund(approved, est.usd_max)
            return exc.as_dict()

        task_id = created.get("id")
        guard.record_task(
            created,
            model=model_id,
            resolution=resolution,
            duration=duration if duration > 0 else None,
            label=est.label,
            est_usd=est.usd,
            video_input=bool(videos),
            approved_by=approved_label(approved),
            source="mcp",
        )
        out: dict[str, Any] = {
            "ok": True,
            "task_id": task_id,
            "status": "queued",
            "estimate": est.as_dict(),
            "approved_by": approved_label(approved),
        }
        if wait:
            try:
                task = await wait_for(ctx, task_id, timeout_s)
                out.update(await settle(task, save_to))
            except ArkError as exc:
                out["wait_error"] = exc.as_dict()
        if out.get("status") not in TERMINAL:
            out["next"] = "Call video_task with this task_id (wait=true to block until done)."
        out["spend"] = guard.snapshot()
        return out

    @mcp.tool()
    async def generate_image(
        ctx: Context,
        prompt: str,
        model: Annotated[
            str | None, Field(description="pro (Seedream 5.0 Pro), flash, lite, 4.5, 4.0 or an id")
        ] = None,
        size: Annotated[str, Field(description="1K, 2K, 4K or WIDTHxHEIGHT")] = "2K",
        reference_images: Annotated[
            list[str] | None, Field(description="Up to 10 image URLs or local paths")
        ] = None,
        max_images: Annotated[int, Field(ge=1, le=15, description="Up to N related images")] = 1,
        output_format: Literal["jpeg", "png"] = "jpeg",
        watermark: bool = False,
        save_to: Annotated[str | None, Field(description="File or folder to save images")] = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        """Make an image with Seedream. Priced and checked by the spend guard first."""
        model_id = prices.resolve_image(model or settings.image_model)
        try:
            refs = [to_image_url(v) for v in reference_images or []]
        except ArkError as exc:
            return exc.as_dict()
        est = prices.estimate_image(model_id, size=size, count=max_images, input_images=len(refs))
        payload: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "size": size,
            "output_format": output_format,
            "response_format": "url",
            "watermark": watermark,
        }
        if refs:
            payload["image"] = refs if len(refs) > 1 else refs[0]
        if max_images > 1:
            payload["sequential_image_generation"] = "auto"
            payload["sequential_image_generation_options"] = {"max_images": max_images}

        approved, stop = await authorize(ctx, est, _fingerprint(payload), approval_id)
        if stop:
            return stop
        try:
            result = await ark.generate_image(payload)
        except ArkError as exc:
            if isinstance(approved, Approval):
                guard.refund(approved, est.usd_max)
            return exc.as_dict()

        urls = [item["url"] for item in result.get("data") or [] if item.get("url")]
        made = (result.get("usage") or {}).get("generated_images") or len(urls)
        usd = prices.image_cost(model_id, size, made, len(refs))
        record_id = "img_" + secrets.token_hex(6)
        guard.ledger.upsert(
            {
                "id": record_id,
                "kind": "image",
                "model": model_id,
                "status": "succeeded" if urls else "failed",
                "created": guard.clock(),
                "label": est.label,
                "est_usd": est.usd,
                "usd": usd if urls else 0.0,
                "images": made,
                "approved_by": approved_label(approved),
                "source": "mcp",
            }
        )
        out: dict[str, Any] = {
            "ok": bool(urls),
            "image_urls": urls,
            "cost_usd": round(usd, 3) if usd is not None else None,
            "approved_by": approved_label(approved),
        }
        target = save_to or (str(settings.output_dir) if settings.output_dir else None)
        if urls and target:
            ext = "png" if output_format == "png" else "jpg"
            saved = []
            for i, url in enumerate(urls, 1):
                name = f"{record_id}_{i}.{ext}" if len(urls) > 1 else f"{record_id}.{ext}"
                dest = _resolve_dest(target, name)
                if len(urls) > 1 and dest.name != name:  # the caller named a single file
                    dest = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
                try:
                    saved.append(str(await ark.download(url, dest)))
                except (ArkError, OSError) as exc:
                    out["save_error"] = str(exc)
            out["saved_to"] = saved
        if result.get("error"):
            out["error"] = result["error"]
        out["spend"] = guard.snapshot()
        return out

    # Tasks

    @mcp.tool()
    async def video_task(
        ctx: Context,
        task_id: str,
        wait: Annotated[bool, Field(description="Block until the task finishes")] = False,
        timeout_s: Annotated[int, Field(ge=10, le=1800)] = 300,
        save_to: Annotated[str | None, Field(description="File or folder to save the mp4")] = None,
    ) -> dict[str, Any]:
        """Status of a video task. When it has succeeded: video_url, real cost, saved file."""
        try:
            task = await wait_for(ctx, task_id, timeout_s) if wait else await ark.get_task(task_id)
            return {"ok": True, **(await settle(task, save_to))}
        except ArkError as exc:
            return exc.as_dict()

    @mcp.tool()
    async def list_video_tasks(
        status: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = None,
        model: str | None = None,
        page: Annotated[int, Field(ge=1)] = 1,
        page_size: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Recent video tasks on this key, newest first, with what each one cost."""
        try:
            data = await ark.list_tasks(
                page_num=page,
                page_size=page_size,
                status=status,
                model=prices.resolve_video(model) if model else None,
            )
        except ArkError as exc:
            return exc.as_dict()
        rows = []
        for task in data.get("items") or []:
            record = guard.record_task(task)
            row = _task_view(task)
            row.pop("video_url", None)
            row.pop("last_frame_url", None)
            row["model"] = prices.video_name(task.get("model", ""))
            if record.get("usd") is not None:
                row["cost_usd"] = round(record["usd"], 3)
            if task.get("created_at"):
                row["created"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(task["created_at"]))
            rows.append(row)
        return {"total": data.get("total"), "tasks": rows}

    @mcp.tool()
    async def cancel_video_task(task_id: str) -> dict[str, Any]:
        """Cancel a queued video task. Running tasks can't be stopped."""
        try:
            await ark.cancel_task(task_id)
        except ArkError as exc:
            return exc.as_dict()
        if guard.ledger.get(task_id):
            guard.ledger.upsert({"id": task_id, "status": "cancelled", "usd": 0.0})
        return {"ok": True, "task_id": task_id, "status": "cancelled"}

    # Money

    def price_plan(items: list[PlanItem]) -> tuple[list[dict[str, Any]], float, float, list[str]]:
        lines, total, total_max, unknown = [], 0.0, 0.0, []
        for item in items:
            if item.kind == "video":
                model_id = prices.resolve_video(item.model or settings.video_model)
                est = prices.estimate_video(
                    model_id,
                    resolution=item.resolution,
                    duration=item.duration,
                    input_videos=[item.reference_video_seconds] * item.reference_videos,
                )
            else:
                model_id = prices.resolve_image(item.model or settings.image_model)
                est = prices.estimate_image(
                    model_id, size=item.size, input_images=item.reference_images
                )
            line = {"count": item.count, **est.as_dict()}
            if est.usd is None:
                unknown.append(est.label)
            else:
                line["subtotal_usd"] = round(est.usd * item.count, 3)
                total += est.usd * item.count
                total_max += (est.usd_max or est.usd) * item.count
            lines.append(line)
        return lines, total, total_max, unknown

    @mcp.tool()
    async def estimate_cost(items: list[PlanItem]) -> dict[str, Any]:
        """Price a plan in dollars without spending anything."""
        try:
            lines, total, total_max, unknown = price_plan(items)
        except PricingError as exc:
            return {"ok": False, "status": "invalid_request", "message": str(exc)}
        await guard.sync(ark)
        decision = guard.evaluate(total_max)
        out: dict[str, Any] = {
            "ok": True,
            "items": lines,
            "total_usd": round(total, 2),
            "spend": guard.snapshot(),
            "after_usd": round(decision.after, 2),
            "say": f"About {money(total)} in total"
            + (f" (up to {money(total_max)})" if total_max > total + 0.005 else "")
            + f". {PERIOD_LABEL[settings.budget_period].capitalize()} you'd be at "
            f"{money(decision.after)} of your {money(settings.budget_usd)} budget.",
        }
        if total_max > total + 0.005:
            out["total_usd_max"] = round(total_max, 2)
        if unknown:
            out["unpriced"] = unknown
        if decision.blocked:
            out["blocked_by_hard_cap"] = decision.blocked
        return out

    @mcp.tool()
    async def preapprove_spend(
        ctx: Context,
        items: list[PlanItem] | None = None,
        note: Annotated[str | None, Field(description="What the batch is for")] = None,
        approval_id: Annotated[
            str | None, Field(description="Confirm a pending batch, only after the user said yes")
        ] = None,
    ) -> dict[str, Any]:
        """Get one human approval for a whole batch. Returns an approval_id to pass to each
        generate call; it covers up to the approved dollars and number of calls for 3 hours."""
        if approval_id:
            try:
                approval = guard.confirm_batch(approval_id)
            except GuardError as exc:
                return {"ok": False, "status": "approval_invalid", "message": str(exc)}
            return {"ok": True, "status": "approved", **approval.view(guard.clock())}
        if not items:
            return {"ok": False, "status": "invalid_request", "message": "Pass the planned items."}
        try:
            lines, total, total_max, unknown = price_plan(items)
        except PricingError as exc:
            return {"ok": False, "status": "invalid_request", "message": str(exc)}
        if unknown:
            return {
                "ok": False,
                "status": "invalid_request",
                "message": "Some items have no price on file and need their own approval: "
                + "; ".join(unknown),
            }
        await guard.sync(ark)
        decision = guard.evaluate(total_max)
        if decision.blocked:
            return {"ok": False, "status": "blocked_by_hard_cap", "message": decision.blocked}
        calls = sum(item.count for item in items)
        what = note or f"{calls} generation{'s' if calls != 1 else ''}"
        summary = "; ".join(f"{line['count']}x {line['what']}" for line in lines)
        decision.reasons = decision.reasons or ["it is a batch"]
        message = guard.ask_text(f"{what} ({summary})", total, total_max, decision)

        if settings.approval == "off":
            approval = guard.open_batch(total_max, calls, summary, confirmed=True)
            return {
                "ok": True,
                "status": "approved",
                "approved_by": "off",
                **approval.view(guard.clock()),
            }
        if settings.approval in ("auto", "dialog") and _can_elicit(ctx):
            answer = await ask_in_dialog(ctx, message, total_max)
            if answer is True:
                approval = guard.open_batch(total_max, calls, summary, confirmed=True)
                return {
                    "ok": True,
                    "status": "approved",
                    "approved_by": "dialog",
                    "next": "Pass approval_id to each generate call in this batch.",
                    **approval.view(guard.clock()),
                }
            if answer is False:
                return {"ok": False, "status": "declined", "message": "The user declined."}
        if settings.approval == "dialog":
            return {
                "ok": False,
                "status": "dialog_unavailable",
                "message": "This client can't show approval dialogs.",
            }
        approval = guard.open_batch(total_max, calls, summary, confirmed=False)
        return {
            "ok": False,
            "status": "needs_your_ok",
            "ask_user": message,
            "approval_id": approval.id,
            "next": "Show ask_user to the user and wait for an explicit yes. Only then call "
            "preapprove_spend(approval_id=...) to activate it.",
        }

    @mcp.tool()
    async def spend_status() -> dict[str, Any]:
        """What you've spent this period, by model, against your budget and caps."""
        await guard.sync(ark, force=True)
        records = guard.period_records()
        by_model: dict[str, dict[str, Any]] = {}
        for record in records:
            if record.get("status") in FREE_STATUSES:
                continue
            kind = record.get("kind", "video")
            model = record.get("model") or "?"
            name = prices.video_name(model) if kind == "video" else prices.image_name(model)
            row = by_model.setdefault(name, {"model": name, "generations": 0, "usd": 0.0})
            row["generations"] += record.get("images", 1) if kind == "image" else 1
            row["usd"] += record_cost(record)
        recent = sorted(records, key=lambda r: r.get("created") or 0, reverse=True)[:8]
        out: dict[str, Any] = {
            **guard.snapshot(),
            "ask_above_usd": settings.ask_above_usd,
            "approval_mode": settings.approval,
            "by_model": [
                {**row, "usd": round(row["usd"], 2)}
                for row in sorted(by_model.values(), key=lambda r: -r["usd"])
            ],
            "recent": [
                {
                    "what": r.get("label") or r.get("model"),
                    "status": r.get("status"),
                    "usd": round(r["usd"], 3) if r.get("usd") is not None else None,
                    "estimate_usd": round(r["est_usd"], 3) if r.get("est_usd") else None,
                    "when": time.strftime("%m-%d %H:%M", time.localtime(r.get("created") or 0)),
                }
                for r in recent
            ],
            "open_approvals": guard.open_approvals(),
            "prices_as_of": PRICES_AS_OF,
            "note": "BytePlus list prices. Prepaid token packs and discounts are not reflected.",
        }
        if guard.last_sync_error:
            out["sync_error"] = guard.last_sync_error
        return out

    @mcp.tool()
    async def check_setup(ctx: Context) -> dict[str, Any]:
        """Check the API key, the connection, and how spend approvals will reach the user."""
        ping = await ark.ping()
        dialog = _can_elicit(ctx)
        how = {
            "off": "never asks (tracking and hard cap still apply)",
            "dialog": "approval dialog only" if dialog else "dialog only, but this client has none",
            "chat": "asks in chat with a one-time approval_id",
            "auto": "approval dialog" if dialog else "in chat (this client has no dialogs)",
        }[settings.approval]
        return {
            "version": __version__,
            "api_key_set": bool(settings.api_key),
            **ping,
            "base_url": settings.base_url,
            "default_video_model": prices.video_name(prices.resolve_video(settings.video_model)),
            "default_image_model": prices.image_name(prices.resolve_image(settings.image_model)),
            "approvals": how,
            "ask_above_usd": settings.ask_above_usd,
            "budget": f"{money(settings.budget_usd)} per {settings.budget_period}",
            "hard_cap_usd": settings.hard_cap_usd,
            "ledger": str(settings.ledger_path),
            "output_dir": str(settings.output_dir) if settings.output_dir else None,
            "prices_as_of": PRICES_AS_OF,
        }

    mcp._seedance_guard = guard  # type: ignore[attr-defined]  # handy for tests and the CLI
    return mcp
