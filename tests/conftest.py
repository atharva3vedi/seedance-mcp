from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ElicitResult

from seedance_mcp.config import Settings
from seedance_mcp.pricing import video_input_tokens, video_output_tokens
from seedance_mcp.server import create_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeArk:
    """Just enough of ModelArk: tasks finish instantly and bill like the real thing."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.images: list[dict[str, Any]] = []
        self.elsewhere: list[dict[str, Any]] = []  # tasks made by other tools on the same key
        self.reject_next: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if request.url.host == "tos.test":
            return httpx.Response(200, content=b"fake-media")
        if self.reject_next and method == "POST":
            code, self.reject_next = self.reject_next, None
            return httpx.Response(400, json={"error": {"code": code, "message": "rejected"}})
        if path.endswith("/contents/generations/tasks") and method == "POST":
            body = json.loads(request.content)
            self.created.append(body)
            task_id = f"cgt-{len(self.created)}"
            refs = [b for b in body["content"] if b["type"] == "video_url"]
            tokens = video_output_tokens(body["resolution"], body["duration"]) + (
                video_input_tokens(body["resolution"], 4) if refs else 0
            )
            self.tasks[task_id] = {
                "id": task_id,
                "model": body["model"],
                "status": "succeeded",
                "duration": body["duration"],
                "resolution": body["resolution"],
                "ratio": body["ratio"],
                "created_at": int(time.time()),
                "content": {"video_url": f"https://tos.test/{task_id}.mp4?sig=abc"},
                "usage": {"completion_tokens": tokens, "total_tokens": tokens},
            }
            return httpx.Response(200, json={"id": task_id})
        if "/contents/generations/tasks/" in path and method == "GET":
            return httpx.Response(200, json=self.tasks[path.rsplit("/", 1)[-1]])
        if path.endswith("/contents/generations/tasks") and method == "GET":
            items = list(self.tasks.values()) + self.elsewhere
            return httpx.Response(200, json={"items": items, "total": len(items)})
        if "/contents/generations/tasks/" in path and method == "DELETE":
            return httpx.Response(200, json={})
        if path.endswith("/images/generations"):
            body = json.loads(request.content)
            self.images.append(body)
            return httpx.Response(
                200,
                json={
                    "data": [{"url": "https://tos.test/img.jpeg?sig=1"}],
                    "usage": {"generated_images": 1},
                },
            )
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"error": {"code": "NotFound", "message": path}})


@pytest.fixture
def fake_ark() -> FakeArk:
    return FakeArk()


@pytest.fixture
def make_settings(tmp_path):
    def make(**overrides: Any) -> Settings:
        base = dict(
            api_key="test-key",
            base_url="https://ark.test/api/v3",
            home=tmp_path / "home",
            poll_seconds=0.01,
        )
        return Settings(**{**base, **overrides})

    return make


class Dialog:
    """A stand-in human answering MCP elicitation dialogs."""

    def __init__(self, approve: bool | None = True) -> None:
        self.approve = approve
        self.messages: list[str] = []

    async def __call__(self, context: Any, params: Any) -> ElicitResult:
        self.messages.append(params.message)
        if self.approve is None:
            return ElicitResult(action="cancel")
        if not self.approve:
            return ElicitResult(action="decline")
        return ElicitResult(action="accept", content={"approve": True})


@pytest.fixture
def connect(fake_ark, make_settings):
    @asynccontextmanager
    async def _connect(dialog: Dialog | None = None, **settings: Any):
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake_ark.handler))
        server = create_server(make_settings(**settings), http_client=client)
        async with create_connected_server_and_client_session(
            server, elicitation_callback=dialog
        ) as session:

            async def call(tool: str, **args: Any) -> dict[str, Any]:
                result = await session.call_tool(tool, args)
                assert not result.isError, result.content
                return json.loads(result.content[0].text)

            call.server = server  # type: ignore[attr-defined]
            yield call

    return _connect
