"""Thin async client for the BytePlus ModelArk media endpoints."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

import httpx

TASKS_PATH = "/contents/generations/tasks"
KEY_HINT = (
    "Create a key in BytePlus ModelArk > API Key Management, then put ARK_API_KEY=... in "
    "~/.seedance-mcp/.env or pass it with -e when you add the server."
)

# Matched against the provider's error code and message, first hit wins.
_HINTS: tuple[tuple[str, str], ...] = (
    (
        "PrivacyInformation",
        "Seedance 2.x rejects realistic human faces as image input. To keep a person "
        "consistent, generate them text-to-video first, then pass that clip in reference_videos.",
    ),
    (
        "InputTextSensitiveContentDetected",
        "The prompt tripped BytePlus moderation. Reword anything that reads as danger or "
        "violence, e.g. 'a cliff-jumping spot with swimmers below' instead of "
        "'leaping off a cliff'.",
    ),
    (
        "SensitiveContentDetected",
        "BytePlus moderation blocked this. Failed tasks are not billed. "
        "Adjust the input and retry.",
    ),
    (
        "not activated",
        "Activate the model in the ModelArk console: Model activation > Media. Activation is "
        "free; you pay only for generations.",
    ),
    (
        "ModelNotOpen",
        "Activate the model in the ModelArk console: Model activation > Media.",
    ),
    (
        "InvalidEndpointOrModel",
        "Unknown model id. Use an alias like '2.5' or 'mini', or check the ModelArk model list.",
    ),
    ("ratio", "Check ratio. When you pass a first_frame image, ratio must be 'adaptive'."),
)


class ArkError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.hint = hint

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "status": "provider_error", "message": str(self)}
        if self.code:
            out["error_code"] = self.code
        if self.status:
            out["http_status"] = self.status
        if self.hint:
            out["hint"] = self.hint
        return out


def _to_error(response: httpx.Response) -> ArkError:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    err = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    code = err.get("code") if isinstance(err, dict) else None
    message = (err.get("message") if isinstance(err, dict) else None) or (
        response.text[:300] or f"HTTP {response.status_code}"
    )
    haystack = f"{code} {message}".lower()
    hint = next((h for key, h in _HINTS if key.lower() in haystack), None)
    if hint is None and response.status_code in (401, 403):
        hint = KEY_HINT
    if hint is None and response.status_code == 429:
        hint = "Rate limited by BytePlus. Wait a minute or lower concurrency."
    return ArkError(str(message), status=response.status_code, code=code, hint=hint)


def to_image_url(value: str) -> str:
    """Pass URLs through; turn a local image path into a base64 data URL."""
    if value.startswith(("http://", "https://", "data:")):
        return value
    path = Path(value).expanduser()
    if not path.is_file():
        raise ArkError(f"Image not found: {value}", code="file_not_found")
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def require_url(value: str, what: str) -> str:
    if not value.startswith(("http://", "https://")):
        raise ArkError(
            f"{what} must be a public http(s) URL, got {value[:80]!r}.",
            code="url_required",
            hint="Upload the file somewhere public, or reuse the video_url of an earlier task.",
        )
    return value


class Ark:
    def __init__(
        self, api_key: str | None, base_url: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15))

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ArkError("ARK_API_KEY is not set.", code="missing_api_key", hint=KEY_HINT)
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = self._headers()
        try:
            response = await self.client.request(
                method, self.base_url + path, headers=headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ArkError(
                f"Could not reach BytePlus ModelArk ({type(exc).__name__}).",
                code="network_error",
            ) from exc
        if response.is_success:
            return response.json() if response.content else {}
        raise _to_error(response)

    async def create_video_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", TASKS_PATH, json=payload)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"{TASKS_PATH}/{task_id}")

    async def list_tasks(
        self,
        *,
        page_num: int = 1,
        page_size: int = 20,
        status: str | None = None,
        model: str | None = None,
        task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        params: list[tuple[str, str | int]] = [("page_num", page_num), ("page_size", page_size)]
        if status:
            params.append(("filter.status", status))
        if model:
            params.append(("filter.model", model))
        for task_id in task_ids or []:
            params.append(("filter.task_ids", task_id))
        return await self._request("GET", TASKS_PATH, params=params)

    async def cancel_task(self, task_id: str) -> None:
        await self._request("DELETE", f"{TASKS_PATH}/{task_id}")

    async def generate_image(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/images/generations", json=payload)

    async def ping(self) -> dict[str, Any]:
        try:
            response = await self.client.get(f"{self.base_url}/models", headers=self._headers())
        except ArkError as exc:
            return {"reachable": None, "key_ok": False, "message": str(exc), "hint": exc.hint}
        except httpx.HTTPError as exc:
            return {"reachable": False, "key_ok": None, "message": type(exc).__name__}
        key_ok = response.status_code not in (401, 403)
        out: dict[str, Any] = {"reachable": True, "key_ok": key_ok}
        if not key_ok:
            out["hint"] = KEY_HINT
        return out

    async def download(self, url: str, dest: Path) -> Path:
        """Save a signed result URL. No auth header: these are pre-signed storage links."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        async with self.client.stream("GET", url) as response:
            if not response.is_success:
                raise ArkError(
                    f"Download failed with HTTP {response.status_code}.",
                    code="download_failed",
                    hint="Result URLs expire 24 hours after generation.",
                )
            with tmp.open("wb") as fh:
                async for chunk in response.aiter_bytes():
                    fh.write(chunk)
        tmp.replace(dest)
        return dest
