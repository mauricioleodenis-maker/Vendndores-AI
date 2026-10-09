"""Extraccion de texto limpio y pistas de contacto desde HTML NO CONFIABLE.

Todo lo que sale de aqui es dato, nunca instruccion: se recorta, se normaliza y se envuelve con
``wrap_untrusted`` antes de pasarlo a un LLM.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import escape
from typing import Any

from bs4 import BeautifulSoup, Comment, Tag

MAX_TEXT_CHARS = 20_000
MAX_TITLE_CHARS = 300
_DROP_TAGS = (
    "script", "style", "noscript", "template", "iframe", "svg", "canvas", "object", "embed",
    "nav", "footer", "form", "head",
)  # fmt: skip
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])",
    re.I,
)
_WS = re.compile(r"[ \t\r\f\v ]+")
_BLANKS = re.compile(r"\n\s*\n+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩﻿]")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+\b")
# Telefonos colombianos: moviles 3XX XXX XXXX (con +57 opcional) y fijos (60X) XXX XXXX / 7 digitos.
_PHONE = re.compile(
    r"(?<![\d])(?:\+?57[\s.-]?)?(?:\(?60\d\)?[\s.-]?\d{3}[\s.-]?\d{4}|3\d{2}[\s.-]?\d{3}[\s.-]?\d{4})"
    r"(?![\d])"
)
_PRICE = re.compile(
    r"(?:\$|COP\s?)\s?\d{1,3}(?:[.,]\d{3})+(?!\d)|(?:\$|COP\s?)\s?\d{4,9}(?!\d)", re.I
)
_HOURS = re.compile(
    r"(?:lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|lun|mar|mi[eé]|jue|vie|s[aá]b|dom)"
    r"[^\n]{0,60}?\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.|h)?\s?(?:-|a|–|hasta)\s?"
    r"\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.|h)?",
    re.I,
)
_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above)[^\n.]*|ignora (todas )?(las )?instrucciones[^\n.]*"
    r"|olvida (todas )?(tus|las) instrucciones[^\n.]*|system\s*:|</?\s*(untrusted_source|system|"
    r"assistant|user_message)[^>]*>)",
    re.I,
)


@dataclass(frozen=True, slots=True)
class ContactHints:
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    hours: list[str] = field(default_factory=list)
    prices: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "phones": self.phones,
            "emails": self.emails,
            "hours": self.hours,
            "prices": self.prices,
        }


def _is_hidden(tag: Tag) -> bool:
    attrs = tag.attrs or {}
    style = attrs.get("style")
    return bool(
        attrs.get("hidden") is not None
        or str(attrs.get("aria-hidden", "")).lower() == "true"
        or (isinstance(style, str) and _HIDDEN_STYLE.search(style))
    )


def sanitize_text(text: str) -> str:
    """Quita caracteres de control/bidi y neutraliza patrones tipicos de inyeccion de prompt."""
    text = _CTRL.sub("", text)
    return _INJECTION.sub("[contenido filtrado]", text)


def _clean(text: str) -> str:
    lines = (_WS.sub(" ", ln).strip() for ln in text.splitlines())
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def extract_title(html: str) -> str:
    soup = _soup(html)
    node = soup.find("title")
    title = node.get_text(" ", strip=True) if node else ""
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
    return sanitize_text(_WS.sub(" ", title))[:MAX_TITLE_CHARS]


def html_to_text(html: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """HTML -> texto limpio: sin scripts/estilos/nav, comentarios ni contenido oculto."""
    soup = _soup(html)
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for tag in [t for t in soup.find_all(True) if _is_hidden(t)]:
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for blk in soup.find_all(
        ["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "ul"]
    ):
        blk.insert_before("\n")
        blk.insert_after("\n")
    return sanitize_text(_clean(soup.get_text()))[:max_chars]


def extract_jsonld(html: str) -> list[dict[str, Any]]:
    """Bloques JSON-LD validos (listas y @graph aplanados). Ignora JSON invalido."""
    out: list[dict[str, Any]] = []
    for node in _soup(html).find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(node.string or node.get_text() or "")
        except (ValueError, RecursionError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                graph = item.get("@graph")
                if isinstance(graph, list):
                    out.extend(g for g in graph if isinstance(g, dict))
                else:
                    out.append(item)
    return out[:20]


def _dedupe(values: list[str], limit: int) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))[:limit]


def _norm_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("57") and len(digits) > 10:
        digits = digits[2:]
    return digits


def extract_hints(text: str) -> ContactHints:
    """Telefonos (solo digitos), correos, horarios y precios (COP) como pistas, no como verdad."""
    return ContactHints(
        phones=_dedupe([_norm_phone(m.group()) for m in _PHONE.finditer(text)], 10),
        emails=_dedupe([m.group().lower() for m in _EMAIL.finditer(text)], 10),
        hours=_dedupe([_WS.sub(" ", m.group()).strip() for m in _HOURS.finditer(text)], 10),
        prices=_dedupe([_WS.sub(" ", m.group()).strip() for m in _PRICE.finditer(text)], 30),
    )


def extract_links(html: str) -> list[tuple[str, str]]:
    """(href, texto_ancla) de los <a href> visibles."""
    links: list[tuple[str, str]] = []
    for a in _soup(html).find_all("a", href=True):
        href = str(a["href"]).strip()
        if href and not href.lower().startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
            links.append((href, a.get_text(" ", strip=True)[:100]))
    return links


def extract_meta(html: str) -> dict[str, str]:
    """Metadatos publicos (og:*, description) saneados y acotados."""
    meta: dict[str, str] = {}
    for m in _soup(html).find_all("meta"):
        key = str(m.get("property") or m.get("name") or "").lower()
        content = m.get("content")
        if key in {"og:title", "og:description", "description", "og:site_name"} and content:
            meta[key] = sanitize_text(_WS.sub(" ", str(content)).strip())[:500]
    return meta


def wrap_untrusted(url: str, text: str) -> str:
    """Delimita contenido scrapeado para pasarlo a un LLM como dato."""
    safe = text.replace("</untrusted_source", "&lt;/untrusted_source")
    return f'<untrusted_source url="{escape(url, quote=True)}">\n{safe}\n</untrusted_source>'
