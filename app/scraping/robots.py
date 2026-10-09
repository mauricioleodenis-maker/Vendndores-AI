"""Interpretacion minima de robots.txt (por host, en memoria durante un crawl)."""

from __future__ import annotations

from urllib.robotparser import RobotFileParser

from app.core.http import USER_AGENT, FetchError, safe_fetch

_AGENT = USER_AGENT.split("/", 1)[0]


class RobotsPolicy:
    def __init__(self, text: str | None) -> None:
        self._parser: RobotFileParser | None = None
        if text is not None:
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            self._parser = parser

    def allowed(self, url: str) -> bool:
        return True if self._parser is None else self._parser.can_fetch(_AGENT, url)


async def load_robots(origin: str) -> RobotsPolicy:
    """Descarga ``/robots.txt``. 404/errores => se permite todo; 401/403 => se bloquea todo."""
    try:
        res = await safe_fetch(f"{origin}/robots.txt", max_bytes=200_000)
    except FetchError:
        return RobotsPolicy(None)
    if res.status in (401, 403):
        return RobotsPolicy("User-agent: *\nDisallow: /")
    if res.status >= 400:
        return RobotsPolicy(None)
    return RobotsPolicy(res.text)
