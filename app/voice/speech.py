"""Constructores TwiML y normalizacion de texto para TTS en espanol (es-CO/es-MX).

Funciones puras, sin I/O. Todo texto dinamico se escapa para XML.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from xml.sax.saxutils import escape, quoteattr

GATHER_LANGUAGE = "es-CO"
SAY_LANGUAGE = "es-MX"
DEFAULT_VOICE = "Polly.Mia"
MAX_HINTS = 40
MAX_HINTS_CHARS = 3800

_UNITS = (
    "cero",
    "uno",
    "dos",
    "tres",
    "cuatro",
    "cinco",
    "seis",
    "siete",
    "ocho",
    "nueve",
    "diez",
    "once",
    "doce",
    "trece",
    "catorce",
    "quince",
    "dieciséis",
    "diecisiete",
    "dieciocho",
    "diecinueve",
    "veinte",
    "veintiuno",
    "veintidós",
    "veintitrés",
    "veinticuatro",
    "veinticinco",
    "veintiséis",
    "veintisiete",
    "veintiocho",
    "veintinueve",
)
_TENS = {
    3: "treinta",
    4: "cuarenta",
    5: "cincuenta",
    6: "sesenta",
    7: "setenta",
    8: "ochenta",
    9: "noventa",
}
_HUNDREDS = {
    1: "ciento",
    2: "doscientos",
    3: "trescientos",
    4: "cuatrocientos",
    5: "quinientos",
    6: "seiscientos",
    7: "setecientos",
    8: "ochocientos",
    9: "novecientos",
}
_MONTHS = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]


def _below_1000(n: int, *, apocope: bool = False) -> str:
    if n < 30:
        w = _UNITS[n]
        if apocope and n in (1, 21):
            w = "un" if n == 1 else "veintiún"
        return w
    if n < 100:
        t, u = divmod(n, 10)
        base = _TENS[t // 1 if t >= 3 else t]
        if u == 0:
            return base
        return f"{base} y {_below_1000(u, apocope=apocope)}"
    if n == 100:
        return "cien"
    h, r = divmod(n, 100)
    return _HUNDREDS[h] if r == 0 else f"{_HUNDREDS[h]} {_below_1000(r, apocope=apocope)}"


def number_to_words(n: int, *, apocope: bool = False) -> str:
    """Entero a palabras en espanol. apocope=True da 'un' en vez de 'uno' (antes de sustantivo)."""
    if n < 0:
        return "menos " + number_to_words(-n, apocope=apocope)
    if n < 1000:
        return _below_1000(n, apocope=apocope)
    if n < 1_000_000:
        th, r = divmod(n, 1000)
        head = "mil" if th == 1 else f"{_below_1000(th, apocope=True)} mil"
        return head if r == 0 else f"{head} {_below_1000(r, apocope=apocope)}"
    if n < 10**12:
        m, r = divmod(n, 1_000_000)
        head = "un millón" if m == 1 else f"{number_to_words(m, apocope=True)} millones"
        if r == 0:
            return head
        return f"{head} {number_to_words(r, apocope=apocope)}"
    return " ".join(number_to_words(int(c)) for c in str(n))


def _digits(s: str) -> int:
    return int(re.sub(r"\D", "", s) or 0)


def _money_words(raw: str) -> str:
    # separadores de miles (. o ,) ; descarta decimales ",00"/".50" de 1-2 digitos
    cleaned = re.sub(r"[.,]\d{1,2}$", "", raw.strip())
    n = _digits(cleaned)
    word = "peso" if n == 1 else "pesos"
    if n and n % 1_000_000 == 0 and n >= 1_000_000:
        return f"{number_to_words(n, apocope=True)} de {word}"
    return f"{number_to_words(n, apocope=True)} {word}"


_NUM = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_PRICE_RE = re.compile(
    rf"(?:(?:COP|cop)\s*\$?\s*|\$\s*)({_NUM})(?:\s*(?:COP|cop|pesos(?:\s+colombianos)?|mil\b)?)?"
    rf"|({_NUM})\s*(?:COP|cop)\b"
)


def _price_sub(m: re.Match[str]) -> str:
    raw = m.group(1) or m.group(2)
    text = m.group(0)
    if re.search(r"\bmil\b", text, re.I) and not re.search(r"\d{3}", raw):
        return f"{number_to_words(_digits(raw), apocope=True)} mil pesos"
    return _money_words(raw)


def _period(hour24: int, ampm: str | None) -> str:
    if hour24 == 12 and ampm:
        return "del mediodía" if ampm == "p" else "de la noche"
    if hour24 == 0:
        return "de la noche"
    if hour24 < 6:
        return "de la madrugada"
    if hour24 < 12:
        return "de la mañana"
    if hour24 < 19:
        return "de la tarde"
    return "de la noche"


def time_to_words(hour: int, minute: int, ampm: str | None = None) -> str:
    """'3:30 pm' -> 'tres y treinta de la tarde'."""
    a = ampm[0].lower() if ampm else None
    h24 = hour
    if a == "p" and hour < 12:
        h24 = hour + 12
    elif a == "a" and hour == 12:
        h24 = 0
    h12 = h24 % 12 or 12
    hw = "una" if h12 == 1 else number_to_words(h12)
    out = hw if minute == 0 else f"{hw} y {number_to_words(minute)}"
    return f"{out} {_period(h24, a)}"


_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::|\.(?=\d{2}\s*[ap]\.?\s?m))(\d{2})\s*(?:([ap])\.?\s?m\.?)?(?!\d)"
    r"|\b(\d{1,2})\s*([ap])\.?\s?m\.?(?![a-z])",
    re.I,
)


def _time_sub(m: re.Match[str]) -> str:
    if m.group(1) is not None:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    else:
        h, mi, ap = int(m.group(4)), 0, m.group(5)
    if h > 23 or mi > 59 or (ap and not 1 <= h <= 12):
        return m.group(0)
    return time_to_words(h, mi, ap)


def date_to_words(day: int, month: int, year: int | None = None) -> str:
    d = "primero" if day == 1 else number_to_words(day)
    out = f"{d} de {_MONTHS[month - 1]}"
    if year is not None:
        out += f" de {number_to_words(year)}"
    return out


_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b")


def _iso_sub(m: re.Match[str]) -> str:
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0)
    return date_to_words(d, mo, y)


def _dmy_sub(m: re.Match[str]) -> str:
    d, mo = int(m.group(1)), int(m.group(2))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0)
    y = m.group(3)
    year = None if y is None else (int(y) + 2000 if len(y) == 2 else int(y))
    return date_to_words(d, mo, year)


_PHONE_RE = re.compile(r"\+?\d[\d\s-]{6,}\d")
_BARE_NUM_RE = re.compile(r"\b\d{1,3}(?:\.\d{3})+\b|\b\d+\b")
_MD_RE = re.compile(r"[*_`#>~]+")
_URL_RE = re.compile(r"https?://\S+")


def _phone_sub(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) < 7:
        return m.group(0)
    return ", ".join(
        " ".join(number_to_words(int(c)) for c in digits[i : i + 3])
        for i in range(0, len(digits), 3)
    )


def _bare_sub(m: re.Match[str]) -> str:
    return number_to_words(_digits(m.group(0)))


def normalize_for_tts(text: str) -> str:
    """Prepara texto para voz: precios COP, horas, fechas, telefonos y numeros a palabras."""
    if not text:
        return ""
    t = _URL_RE.sub("", text)
    t = _MD_RE.sub("", t)
    t = _PRICE_RE.sub(_price_sub, t)
    t = _ISO_DATE_RE.sub(_iso_sub, t)
    t = _TIME_RE.sub(_time_sub, t)
    t = _DMY_DATE_RE.sub(_dmy_sub, t)
    t = _PHONE_RE.sub(_phone_sub, t)
    t = _BARE_NUM_RE.sub(_bare_sub, t)
    t = t.replace("&", " y ").replace("%", " por ciento")
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- TwiML


def _xml(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def say(
    text: str,
    *,
    voice: str | None = DEFAULT_VOICE,
    language: str = SAY_LANGUAGE,
    normalize: bool = True,
) -> str:
    spoken = normalize_for_tts(text) if normalize else text
    attrs = f" language={quoteattr(language)}"
    if voice:
        attrs = f" voice={quoteattr(voice)}" + attrs
    return f"<Say{attrs}>{escape(spoken)}</Say>"


def build_hints(names: Iterable[str]) -> str:
    """Pistas de reconocimiento (servicios): unicas, acotadas, sin comas internas."""
    seen: set[str] = set()
    out: list[str] = []
    total = 0
    for raw in names:
        n = re.sub(r"[,\s]+", " ", (raw or "")).strip()[:60]
        if not n or n.lower() in seen:
            continue
        if len(out) >= MAX_HINTS or total + len(n) + 2 > MAX_HINTS_CHARS:
            break
        seen.add(n.lower())
        out.append(n)
        total += len(n) + 2
    return ",".join(out)


def gather(
    action: str,
    prompt: str | None = None,
    *,
    hints: Iterable[str] = (),
    voice: str | None = DEFAULT_VOICE,
    timeout: int = 6,
    language: str = GATHER_LANGUAGE,
    reprompt: str | None = "No te escuché bien. ¿Puedes repetirlo, por favor?",
    fallback_goodbye: str | None = "Parece que hay un problema con la llamada. Hasta luego.",
) -> str:
    """<Gather speech> con prompt dentro (permite barge-in), pistas y reprompt si no hay entrada."""
    attrs = (
        f'input="speech" language={quoteattr(language)} action={quoteattr(action)} '
        f'method="POST" speechTimeout="auto" timeout="{int(timeout)}" actionOnEmptyResult="false"'
    )
    h = build_hints(hints)
    if h:
        attrs += f" hints={quoteattr(h)}"
    inner = say(prompt, voice=voice) if prompt else ""
    body = f"<Gather {attrs}>{inner}</Gather>"
    if reprompt:
        body += say(reprompt, voice=voice)
        body += f"<Gather {attrs}></Gather>"
    if fallback_goodbye:
        body += say(fallback_goodbye, voice=voice) + "<Hangup/>"
    return _xml(body)


def goodbye(
    text: str = "Gracias por llamar. ¡Hasta luego!", *, voice: str | None = DEFAULT_VOICE
) -> str:
    return _xml(say(text, voice=voice) + "<Hangup/>")


def transfer(
    to: str,
    text: str | None = "Te comunico con una persona del equipo, un momento por favor.",
    *,
    voice: str | None = DEFAULT_VOICE,
    timeout: int = 25,
    caller_id: str | None = None,
) -> str:
    """<Dial> a un numero E.164; si no contesta, despedida."""
    num = re.sub(r"[^\d+]", "", to or "")
    if not re.fullmatch(r"\+\d{7,15}", num):
        return goodbye(
            "En este momento no puedo transferir la llamada. Intenta de nuevo más tarde.",
            voice=voice,
        )
    attrs = f'timeout="{int(timeout)}"'
    if caller_id:
        attrs += f" callerId={quoteattr(caller_id)}"
    body = say(text, voice=voice) if text else ""
    body += f"<Dial {attrs}><Number>{escape(num)}</Number></Dial>"
    body += say(
        "No fue posible comunicarte. Por favor intenta más tarde. Hasta luego.", voice=voice
    )
    return _xml(body + "<Hangup/>")


def reply(
    text: str,
    *,
    action: str,
    hints: Iterable[str] = (),
    voice: str | None = DEFAULT_VOICE,
    end_call: bool = False,
    transfer_to: str | None = None,
) -> str:
    """TwiML segun un VoiceReply: despedida, transferencia o siguiente turno."""
    if transfer_to:
        return transfer(transfer_to, text, voice=voice)
    if end_call:
        return goodbye(text, voice=voice)
    return gather(action, text, hints=hints, voice=voice)
