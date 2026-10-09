"""Scoring determinista y explicable de leads (version 1) + deteccion de senales en resenas."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

SCORE_VERSION = 1

# Patrones es-CO sobre texto en minusculas y SIN tildes.
NO_RESPONSE_PATTERNS: dict[str, str] = {
    "no_contestan": r"\b(no|nunca) (me )?contest\w*",
    "nunca_responden": r"\bnunca (me )?respond\w*",
    "no_responden": r"\bno (me )?respond\w*",
    "no_contestan_canal": r"\bno contestan (el |la )?(whatsapp|telefono|llamad\w*)",
    "dejaron_en_visto": r"\bdejaro?n? (me )?en visto\b|\ben visto\b",
    "nadie_responde": r"\bnadie (me |nos |le |lo )?(respond|contest|coge|cogio|atiend|devolv)\w*",
    "nunca_lo_respondieron": r"\bnunca (lo |la |los |las )(respond|contest)\w*",
    "ni_contestaron": r"\bni (me |nos )?(respond|contest)\w*",
    "sin_respuesta": r"\bsin respuesta\b",
    "demoran_responder": r"\b(tard|demor)\w*[^.!?]{0,40}\b(responder|contestar)\b",
    "lentos_responder": r"\blent\w* para (responder|contestar)\b",
    "respuesta_tardia": (
        r"\b(respond|contest)\w* (al (segundo|tercer|cuarto|quinto|otro) dia"
        r"|a las [^.!?]{0,25}(dia siguiente|muy tarde)|(dos|tres|cuatro) dias despues)"
    ),
    "hasta_que_alguien": r"\b(antes de que|hasta que|para que) alguien (respond|contest)\w*",
    "dejan_hablando_solo": r"\bdejan? hablando solo",
    "responder_mas_rapido": r"\b(respond|contest)\w* [^.!?]{0,30}\bmas rapido",
    "no_atienden": r"\bno (me )?atiend\w*",
    "no_devolvieron_llamada": (
        r"\b(no|nunca) (me |nos )?devol\w* (la |el |mi )?(llamada|mensaje|llamado)"
    ),
    "imposible_comunicar": r"\bimposible (comunicar\w*|contactar\w*|hablar)",
    "tardan_responder": r"\btarda\w* (mucho )?en (responder|contestar)",
}
_COMPILED = {k: re.compile(v) for k, v in NO_RESPONSE_PATTERNS.items()}
_EXCERPT_LEN = 280
# Negaciones ("nunca me dejaron en visto") y contexto que NO es dolor de atencion al cliente.
_NEGATED_VISTO = re.compile(r"\b(nunca|jamas|no)\b[^.!?]{0,12}$")
_RECOVERED = re.compile(
    r"\bpero (me )?(devolv|respond|contest)\w* [^.!?]{0,30}(minutos|rapido|enseguida|al rato)"
)
_OWNER_REPLY = re.compile(
    r"\brespond\w* (a )?(mi |la |una |esta )?(resena|opinion|comentario|critica)\b"
)


@dataclass(frozen=True, slots=True)
class SignalMatch:
    index: int
    patterns: tuple[str, ...]
    excerpt: str


def _fold(text: str) -> str:
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    return " ".join(stripped.lower().split())


def _matches(name: str, rx: re.Pattern[str], folded: str) -> bool:
    for m in rx.finditer(folded):
        if name == "dejaron_en_visto" and _NEGATED_VISTO.search(folded[: m.start()]):
            continue
        return True
    return False


def review_signal_scan(reviews: Iterable[str]) -> list[SignalMatch]:
    """Devuelve una coincidencia por resena que mencione falta de respuesta (regex es-CO)."""
    out: list[SignalMatch] = []
    for i, review in enumerate(reviews):
        if not review or not review.strip():
            continue
        folded = _OWNER_REPLY.sub(" ", _fold(review))
        if _RECOVERED.search(folded):
            continue
        hits = tuple(name for name, rx in _COMPILED.items() if _matches(name, rx, folded))
        if hits:
            out.append(SignalMatch(i, hits, " ".join(review.split())[:_EXCERPT_LEN]))
    return out


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    version: int = SCORE_VERSION
    rating_low: int = 15  # < 4.0
    rating_mid: int = 25  # 4.0 - 4.49
    rating_high: int = 0  # >= 4.5
    reviews_200: int = 20
    reviews_50: int = 15
    reviews_20: int = 8
    no_response_one: int = 15
    no_response_many: int = 25
    mobile: int = 15
    landline: int = 3
    no_phone_cap: int = 20
    no_booking_web: int = 5
    only_instagram: int = 5
    shop_fail: int = 25
    shop_slow_seconds: int = 15 * 60
    shop_ok: int = -15
    shop_ok_seconds: int = 5 * 60
    niche_fit: int = 5
    fit_niches: tuple[str, ...] = ("dentista", "clinica_estetica")
    hot: int = 70
    warm: int = 40


DEFAULT = ScoringConfig()


@dataclass(slots=True)
class ScoreResult:
    score: int = 0
    breakdown: dict[str, Any] = field(default_factory=dict)
    band: str = "frio"
    disposition: str | None = None  # "perdido" si el negocio no opera


def band_for(score: int, cfg: ScoringConfig = DEFAULT) -> str:
    if score >= cfg.hot:
        return "caliente"
    if score >= cfg.warm:
        return "tibio"
    return "frio"


def _signal_count(signals: Iterable[Any] | None) -> int:
    n = 0
    for s in signals or ():
        patterns = getattr(s, "matched_patterns", None)
        if patterns is None:
            patterns = getattr(s, "patterns", None)
        if patterns:
            n += 1
    return n


def score_lead(
    lead: Any, signals: Any, secret_test: Any, cfg: ScoringConfig = DEFAULT
) -> ScoreResult:
    """Puntua 0-100 con desglose por regla."""
    status = getattr(lead, "business_status", None)
    if status and status != "OPERATIONAL":
        return ScoreResult(
            0, {"negocio_no_opera": {"points": 0, "detail": str(status)}}, "frio", "perdido"
        )

    parts: dict[str, dict[str, Any]] = {}

    def add(rule: str, points: int, detail: str) -> None:
        parts[rule] = {"points": points, "detail": detail}

    rating = getattr(lead, "rating", None)
    if rating is not None:
        r = float(rating)
        pts = cfg.rating_low if r < 4.0 else cfg.rating_mid if r < 4.5 else cfg.rating_high
        add("rating", pts, f"{r:.1f}")

    reviews = getattr(lead, "review_count", None) or 0
    pts = (
        cfg.reviews_200
        if reviews >= 200
        else cfg.reviews_50
        if reviews >= 50
        else cfg.reviews_20
        if reviews >= 20
        else 0
    )
    add("volumen_resenas", pts, str(reviews))

    n_sig = _signal_count(signals)
    if n_sig:
        add(
            "resenas_no_contestan",
            cfg.no_response_many if n_sig >= 2 else cfg.no_response_one,
            f"{n_sig} resena(s)",
        )

    ptype = getattr(lead, "phone_type", None)
    has_phone = bool(getattr(lead, "phone_e164", None))
    if has_phone and ptype == "mobile":
        add("contactabilidad", cfg.mobile, "movil")
    elif has_phone:
        add("contactabilidad", cfg.landline, "fijo/otro")

    lead_signals = getattr(lead, "signals", None) or {}
    website = getattr(lead, "website", None)
    if website:
        if not (lead_signals.get("web_whatsapp") or lead_signals.get("web_booking")):
            add("web_sin_boton", cfg.no_booking_web, "sin WhatsApp/Calendly")
    elif getattr(lead, "instagram", None) or getattr(lead, "instagram_handle", None):
        add("solo_instagram", cfg.only_instagram, "sin web")

    if secret_test is not None:
        outcome = getattr(secret_test, "outcome", None)
        secs = getattr(secret_test, "response_seconds", None)
        if outcome == "sin_respuesta" or (secs is not None and secs > cfg.shop_slow_seconds):
            add("prueba_secreta", cfg.shop_fail, outcome or f"{secs}s")
        elif (
            outcome in ("respuesta_ok", "respuesta_con_cierre")
            and secs is not None
            and secs < cfg.shop_ok_seconds
        ):
            add("prueba_secreta", cfg.shop_ok, f"{secs}s")

    if getattr(lead, "niche", None) in cfg.fit_niches:
        add("fit_nicho", cfg.niche_fit, str(lead.niche))

    total = max(0, min(100, sum(p["points"] for p in parts.values())))
    if not has_phone and total > cfg.no_phone_cap:
        parts["sin_telefono_tope"] = {"points": cfg.no_phone_cap - total, "detail": "tope"}
        total = cfg.no_phone_cap
    return ScoreResult(total, parts, band_for(total, cfg))
