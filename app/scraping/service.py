"""Persistencia del scraping como ``kb_documents`` (dedupe por ``content_hash`` y tenant)."""

from __future__ import annotations

import hashlib
import uuid
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http import FetchError, safe_fetch
from app.db.models.catalog import KbDocument
from app.scraping.crawler import (
    ScrapedPage,
    crawl_business_site,
    normalize_start_url,
    registrable_domain,
)
from app.scraping.extractor import extract_meta, sanitize_text

_INSTAGRAM_HOSTS = frozenset({"instagram.com"})


def content_hash(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def fetch_instagram_profile(url: str) -> ScrapedPage | None:
    """Metadata publica (og:title/og:description) del perfil. Best-effort: ``None`` si falla."""
    url = normalize_start_url(url)
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return None
    if registrable_domain(host) not in _INSTAGRAM_HOSTS:
        return None
    try:
        res = await safe_fetch(url, max_bytes=500_000)
    except FetchError:
        return None
    if res.status >= 400 or registrable_domain(urlsplit(res.url).hostname or "") not in (
        _INSTAGRAM_HOSTS
    ):
        return None
    meta = extract_meta(res.text)
    title = meta.get("og:title", "")
    parts = [meta.get("og:description") or meta.get("description", "")]
    text = sanitize_text("\n".join(p for p in parts if p))
    if not title and not text:
        return None
    return ScrapedPage(url=res.url, title=title[:300], text=text or title)


async def store_pages(
    session: AsyncSession, tenant_id: uuid.UUID, pages: list[ScrapedPage]
) -> list[KbDocument]:
    """Guarda paginas nuevas; omite las que ya existen (mismo tenant y hash). No hace commit."""
    created: list[KbDocument] = []
    unique: dict[str, ScrapedPage] = {}
    for page in pages:
        unique.setdefault(content_hash(page.text), page)
    if not unique:
        return created
    existing = set(
        await session.scalars(
            select(KbDocument.content_hash).where(
                KbDocument.tenant_id == tenant_id, KbDocument.content_hash.in_(list(unique))
            )
        )
    )
    for digest, page in unique.items():
        if digest in existing:
            continue
        doc = KbDocument(
            tenant_id=tenant_id,
            source_url=page.url[:1000],
            title=page.title[:300],
            content=page.text,
            content_hash=digest,
        )
        session.add(doc)
        created.append(doc)
    await session.flush()
    return created


async def scrape_and_store(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    website_url: str | None,
    *,
    instagram_url: str | None = None,
    max_pages: int = 10,
) -> list[KbDocument]:
    """Rastrea web (+ Instagram opcional) y guarda ``kb_documents``. Devuelve los nuevos."""
    pages: list[ScrapedPage] = []
    if website_url:
        pages.extend(await crawl_business_site(website_url, max_pages=max_pages))
    if instagram_url:
        profile = await fetch_instagram_profile(instagram_url)
        if profile:
            pages.append(profile)
    return await store_pages(session, tenant_id, pages)
