"""A routing mock transport, so the suite never depends on a venue being up."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

RouteHandler = Callable[[httpx.Request], httpx.Response]


def json_route(payload: Any, status_code: int = 200) -> RouteHandler:
    """Serve a fresh response per call, so repeats cannot share read state."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


def status_route(status_code: int, headers: dict[str, str] | None = None) -> RouteHandler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, headers=headers, json={"code": -1003})

    return handler


def raw_route(content: bytes, status_code: int = 200) -> RouteHandler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content)

    return handler


class RoutingHandler:
    """Dispatches by URL path and records what was requested."""

    def __init__(self, routes: dict[str, RouteHandler]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"code": -1121, "msg": "unknown path"})
        return handler(request)

    def count(self, path: str) -> int:
        return sum(1 for call in self.calls if call == path)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
