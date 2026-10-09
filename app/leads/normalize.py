"""Normalizacion de datos de leads (telefono CO, nombre, sitio web, Instagram). Funciones puras."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import phonenumbers

GENERIC_DOMAINS = frozenset(
    {
        "instagram.com",
        "facebook.com",
        "fb.com",
        "linktr.ee",
        "wa.me",
        "whatsapp.com",
        "google.com",
        "goo.gl",
        "maps.app.goo.gl",
        "tiktok.com",
        "youtube.com",
        "linkedin.com",
        "twitter.com",
        "x.com",
        "wixsite.com",
        "business.site",
        "beacons.ai",
        "bit.ly",
    }
)
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "msclkid", "igshid", "mc_cid", "mc_eid", "ref"})
_LEGAL_SUFFIX = re.compile(
    r"\b(s\s?a\s?s|s\s?a|ltda|e\s?u|s\s?en\s?c|cia|y\s?cia|sociedad|limitada)\b"
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_IG_RE = re.compile(r"instagram\.com/(?:_u/)?([A-Za-z0-9._]{1,30})", re.IGNORECASE)
_IG_RESERVED = frozenset({"p", "reel", "reels", "explore", "stories", "accounts", "tv"})


def _phone_type(num: phonenumbers.PhoneNumber) -> str:
    kind = phonenumbers.number_type(num)
    if kind == phonenumbers.PhoneNumberType.MOBILE:
        return "mobile"
    if kind == phonenumbers.PhoneNumberType.FIXED_LINE:
        return "landline"
    return "unknown"


def _foreign_e164(digits: str) -> tuple[str | None, str]:
    """Numero internacional (no +57) valido: se conserva en E.164 con tipo ``unknown``."""
    try:
        num = phonenumbers.parse(f"+{digits}", None)
    except phonenumbers.NumberParseException:
        return None, "unknown"
    if not phonenumbers.is_valid_number(num):
        return None, "unknown"
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164), "unknown"


def to_e164_co(raw: str | None) -> tuple[str | None, str]:
    """Devuelve ``(e164, phone_type)`` con phone_type en mobile|landline|unknown.

    Acepta espacios, guiones, parentesis, ``+57``, ``57`` y ``0057``. Un fijo local de 7 digitos se
    asume Cali (prefijo ``+602``).
    """
    if not raw or not raw.strip():
        return None, "unknown"
    text = raw.strip()
    digits = re.sub(r"\D", "", text)
    if not digits or len(digits) > 15:
        return None, "unknown"
    if text.startswith("++"):
        return None, "unknown"
    international = text.startswith("+") or digits.startswith("00")
    if digits.startswith("00"):
        digits = digits[2:]
    if international and not digits.startswith("57"):
        return _foreign_e164(digits)
    if digits.startswith("57") and len(digits) in (10, 12):
        digits = digits[2:]
    elif digits.startswith("57") and international:
        return None, "unknown"
    if len(digits) == 7 and digits[0] in "2345678":
        digits = "602" + digits
    try:
        num = phonenumbers.parse(f"+57{digits}", None)
    except phonenumbers.NumberParseException:
        return None, "unknown"
    if not phonenumbers.is_valid_number(num):
        return None, "unknown"
    e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    return e164, _phone_type(num)


def phone_type(raw: str | None) -> str:
    """Solo el tipo de telefono (mobile|landline|unknown)."""
    return to_e164_co(raw)[1]


def _strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def name_key(name: str | None) -> str:
    """Clave comparable: minusculas, sin tildes, sin sufijos legales ni puntuacion."""
    if not name:
        return ""
    text = _strip_accents(name[:300]).lower().replace("&", " y ")
    text = re.sub(r"[.\-_/]", " ", text)
    text = _LEGAL_SUFFIX.sub(" ", text)
    return " ".join(_NON_ALNUM.sub(" ", text).split())


def normalize_website(raw: str | None) -> str | None:
    """URL https canonica sin parametros de tracking ni fragmento; None si no es valida."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if len(text) > 2000 or any(ord(c) < 32 for c in text):
        return None
    if "://" not in text:
        text = "https://" + text.lstrip("/")
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        return None
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        ]
    )
    host = parts.hostname.lower()
    path = parts.path.rstrip("/") if parts.path != "/" else ""
    return urlunsplit(("https", host, path, query, ""))


def website_domain(raw: str | None) -> str | None:
    """Dominio sin ``www.``; None si es vacio o un dominio generico (redes, linktree, wa.me)."""
    url = normalize_website(raw)
    if not url:
        return None
    host = urlsplit(url).hostname or ""
    host = host.removeprefix("www.")
    if host in GENERIC_DOMAINS or any(host.endswith("." + g) for g in GENERIC_DOMAINS):
        return None
    return host[:200] or None


def instagram_handle(raw: str | None) -> str | None:
    """Extrae el usuario de una URL de Instagram o de un ``@usuario``."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    m = _IG_RE.search(text)
    handle = (
        m.group(1)
        if m
        else text.lstrip("@")
        if re.fullmatch(r"@?[A-Za-z0-9._]{1,30}", text)
        else None
    )
    if not handle or handle.lower() in _IG_RESERVED:
        return None
    return handle.lower()
