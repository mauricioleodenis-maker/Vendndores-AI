from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.core.http import FetchError, FetchResult, UnsafeURLError
from app.scraping import crawler, robots, service


class FakeWeb:
    """Sitio falso: ``pages[url] = html | (status, ctype, body) | Exception``."""

    def __init__(self) -> None:
        self.pages: dict[str, Any] = {}
        self.calls: list[str] = []

    def add(self, url: str, body: Any, *, status: int = 200, ctype: str = "text/html") -> None:
        self.pages[url] = (status, ctype, body) if isinstance(body, str) else body

    async def fetch(self, url: str, **_: Any) -> FetchResult:
        self.calls.append(url)
        item = self.pages.get(url)
        if item is None:
            return FetchResult(url, 404, "text/html", "")
        if isinstance(item, Exception):
            raise item
        status, ctype, body = item
        if isinstance(body, tuple):  # redireccion simulada: (final_url, html)
            return FetchResult(body[0], status, ctype, body[1])
        return FetchResult(url, status, ctype, body)


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    fake = FakeWeb()
    for mod in (crawler, robots, service):
        monkeypatch.setattr(mod, "safe_fetch", fake.fetch)
    monkeypatch.setattr(crawler, "CRAWL_DELAY_SECONDS", 0)
    return fake


@pytest.fixture
def page_html() -> Callable[..., str]:
    def _build(title: str, body: str, links: tuple[str, ...] = ()) -> str:
        anchors = "".join(f'<a href="{h}">{t}</a>' for h, t in (x.split("|") for x in links))
        return f"<html><head><title>{title}</title></head><body><main><p>{body}</p>{anchors}</main></body></html>"

    return _build


__all__ = ["FakeWeb", "FetchError", "UnsafeURLError"]
