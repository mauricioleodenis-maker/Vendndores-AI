"""Ayudas de agendamiento por voz: ofertas habladas, confirmaciones y fechas dichas en espanol.

Funciones puras (sin I/O). Los textos ya salen en palabras (aptos para TTS); el llamador puede
pasarlos igual por ``speech.normalize_for_tts``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from app.core.clock import to_bogota
from app.voice.speech import number_to_words

MAX_SPOKEN_SLOTS = 3

Confirmation = Literal["yes", "no", "repeat"]

_WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS = (
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
)


def _norm(text: str) -> str:
    """Minusculas, sin tildes ni puntuacion, espacios colapsados."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9:/ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(text: str) -> list[str]:
    return _norm(text).split()


# ------------------------------------------------------------------ numeros hablados
_NUM_WORDS: dict[str, int] = {}
_NUM_LIST = (
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
    "dieciseis",
    "diecisiete",
    "dieciocho",
    "diecinueve",
    "veinte",
    "veintiuno",
    "veintidos",
    "veintitres",
    "veinticuatro",
    "veinticinco",
    "veintiseis",
    "veintisiete",
    "veintiocho",
    "veintinueve",
)
for _i, _w in enumerate(_NUM_LIST):
    _NUM_WORDS[_w] = _i
_NUM_WORDS.update({"un": 1, "una": 1, "primero": 1, "primer": 1})
_TENS_WORDS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50}


def _number_at(tokens: list[str], i: int) -> tuple[int, int] | None:
    """Numero (digitos o palabras) en tokens[i]. Devuelve (valor, tokens consumidos)."""
    tok = tokens[i]
    if tok.isdigit():
        return int(tok), 1
    if tok in _TENS_WORDS:
        base = _TENS_WORDS[tok]
        if i + 2 < len(tokens) + 0 and tokens[i + 1] == "y" and tokens[i + 2] in _NUM_WORDS:
            unit = _NUM_WORDS[tokens[i + 2]]
            if 1 <= unit <= 9:
                return base + unit, 3
        return base, 1
    if tok in _NUM_WORDS and tok not in ("primero", "primer"):
        # "veinte y cinco" (separado)
        if tok == "veinte" and i + 2 < len(tokens) and tokens[i + 1] == "y":
            unit = _NUM_WORDS.get(tokens[i + 2], 0)
            if 1 <= unit <= 9:
                return 20 + unit, 3
        return _NUM_WORDS[tok], 1
    return None


# ------------------------------------------------------------------ confirmaciones
_YES = {
    "si",
    "sii",
    "claro",
    "dale",
    "listo",
    "perfecto",
    "correcto",
    "confirmo",
    "confirmado",
    "ok",
    "okay",
    "vale",
    "afirmativo",
    "exacto",
    "adelante",
    "bueno",
    "bien",
    "sip",
    "aja",
    "agendalo",
    "reservalo",
    "obvio",
}
_YES_PHRASES = (
    "de una",
    "por supuesto",
    "esta bien",
    "esta perfecto",
    "me parece bien",
    "con gusto",
)
_NO = {"no", "nop", "nope", "negativo", "cancela", "cancelalo", "cancelar", "nada"}
_NO_PHRASES = ("mejor no", "otra hora", "otro dia", "otro horario", "cambiar", "cambialo")
_REPEAT = (
    "repite",
    "repita",
    "repetir",
    "repiteme",
    "como",
    "que dijiste",
    "no entendi",
    "otra vez",
)


def parse_confirmation(text: str) -> Confirmation | None:
    """'si/claro/dale' -> yes; 'no/mejor no' -> no; 'repite' -> repeat; otro -> None."""
    n = _norm(text)
    if not n:
        return None
    if any(p in n for p in _REPEAT) and not set(n.split()) & _YES:
        return "repeat"
    if "no hay problema" in n or "no importa" in n:
        return "yes"
    if any(p in n for p in _NO_PHRASES):
        return "no"
    toks = set(n.split())
    if toks & _NO and not ("si" in toks and n.index("si") < n.index("no")):
        return "no"
    if toks & _YES or any(p in n for p in _YES_PHRASES):
        return "yes"
    return None


# ------------------------------------------------------------------ horas habladas
@dataclass(frozen=True, slots=True)
class SpokenTime:
    hour: int  # 0-23 si ``explicit24`` o hay periodo; si no, 1-12 ambiguo
    minute: int
    period: Literal["am", "pm"] | None = None

    def matches(self, dt: datetime) -> bool:
        local = to_bogota(dt)
        if local.minute != self.minute:
            return False
        if self.hour >= 13 or self.hour == 0:
            return local.hour == self.hour
        if local.hour % 12 != self.hour % 12:
            return False
        if self.period == "am":
            return local.hour < 12
        if self.period == "pm":
            return local.hour >= 12
        return True

    def resolve(self) -> time:
        """Hora mas probable en horario de atencion (1-6 -> tarde, 7-11 -> manana)."""
        h = self.hour
        if h >= 13 or h == 0:
            return time(h, self.minute)
        if self.period == "pm":
            h = h % 12 + 12
        elif self.period == "am":
            h = h % 12
        elif h <= 6:
            h += 12
        return time(h, self.minute)


_PERIOD_PM = ("tarde", "noche")


def _period_in(n: str) -> Literal["am", "pm"] | None:
    if re.search(r"\b(de la )?(tarde|noche)\b", n) or re.search(r"\bp ?m\b", n):
        return "pm"
    if re.search(r"\b(de la )?(manana|madrugada)\b", n) or re.search(r"\ba ?m\b", n):
        return "am"
    return None


def parse_spoken_time(text: str) -> SpokenTime | None:
    """'las tres de la tarde', '3 y media', '10:30', 'tres menos cuarto', 'mediodia'."""
    n = _norm(text)
    if re.search(r"\bmediodia\b", n):
        return SpokenTime(12, 0, "pm")
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", n)
    period = _period_in(n)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        return SpokenTime(h, mi, period)
    toks = n.split()
    for i, tok in enumerate(toks):
        if tok not in ("las", "la", "a", "de", "para") and not (
            tok.isdigit() or tok in _NUM_WORDS or tok in _TENS_WORDS
        ):
            continue
        # hora ancla: despues de "las"/"la" o numero suelto con periodo
        j = i
        if tok in ("las", "la", "a", "de", "para"):
            j = i + 1
            if tok == "a" and j < len(toks) and toks[j] in ("las", "la"):
                j += 1
        if j >= len(toks):
            continue
        got = _number_at(toks, j)
        if got is None:
            continue
        val, used = got
        has_article = tok in ("las", "la") or (tok in ("a", "de", "para") and j > i)
        if not has_article and period is None and tok != "a":
            # numero suelto sin "las"/periodo: solo vale si es hora plausible y hay "y/menos"
            rest = toks[j + used : j + used + 1]
            if not rest or rest[0] not in ("y", "menos", "en"):
                continue
        if not 1 <= val <= 23:
            continue
        k = j + used
        minute = 0
        hour = val
        rest = toks[k:]
        if rest[:2] == ["y", "media"]:
            minute = 30
        elif rest[:2] == ["y", "cuarto"]:
            minute = 15
        elif rest[:2] == ["menos", "cuarto"]:
            minute, hour = 45, val - 1
        elif rest[:1] == ["y"] and len(rest) > 1:
            g2 = _number_at(rest, 1)
            if g2 and 0 <= g2[0] <= 59:
                minute = g2[0]
        elif rest[:1] == ["menos"] and len(rest) > 1:
            g2 = _number_at(rest, 1)
            if g2 and 1 <= g2[0] <= 59:
                minute, hour = 60 - g2[0], val - 1
        if hour <= 0:
            hour += 12
        return SpokenTime(hour, minute, period)
    return None


# ------------------------------------------------------------------ fechas habladas
_WD_NORM = {_norm(w): i for i, w in enumerate(_WEEKDAYS)}
_MONTH_NORM = {_norm(w): i + 1 for i, w in enumerate(_MONTHS)}
_ORDINAL_DAY = {"primero": 1, "primer": 1}


def _safe_date(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_spoken_date(text: str, today: date) -> date | None:
    """'mañana', 'el lunes', 'el 15 de noviembre', 'quince de noviembre', 'pasado mañana'.

    Devuelve una fecha >= hoy (o None). Un dia de semana igual a hoy se entiende como la
    proxima semana; un dia/mes ya pasado se entiende del ano siguiente.
    """
    n = _norm(text)
    # "de la manana" es periodo del dia, no la fecha de manana
    n = re.sub(r"\b(de|en|por) la manana\b", " ", n)
    if not n:
        return None
    if re.search(r"\bpasado manana\b", n):
        return today + timedelta(days=2)
    if re.search(r"\bmanana\b", n):
        return today + timedelta(days=1)
    if re.search(r"\bhoy\b", n):
        return today
    m = re.search(r"\ben (\w+) dias?\b", n)
    if m:
        v = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS.get(m.group(1))
        if v and 1 <= v <= 60:
            return today + timedelta(days=v)
    if re.search(r"\ben (una|1) semana\b|\bproxima semana\b|\bsemana que viene\b", n) and not any(
        w in n.split() for w in _WD_NORM
    ):
        return today + timedelta(days=7)
    toks = n.split()
    # dia de la semana
    for tok in toks:
        if tok in _WD_NORM:
            delta = (_WD_NORM[tok] - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)
    # dd/mm[/aaaa]
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", n)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = m.group(3)
        year = today.year if y is None else (int(y) + 2000 if len(y) == 2 else int(y))
        found = _safe_date(year, mo, d)
        if found and found < today and y is None:
            found = _safe_date(year + 1, mo, d)
        return found if found and found >= today else None
    # "<dia> de <mes>" o "<mes> <dia>"
    for i, tok in enumerate(toks):
        if tok in _MONTH_NORM:
            mo = _MONTH_NORM[tok]
            day = None
            if i >= 1:
                j = i - 1
                if toks[j] == "de" and j >= 1:
                    j -= 1
                # numero compuesto como "treinta y uno" termina en j
                for start in range(max(0, j - 2), j + 1):
                    got = _number_at(toks, start)
                    if got and start + got[1] - 1 == j:
                        day = got[0]
                        break
                if day is None and toks[j] in _ORDINAL_DAY:
                    day = _ORDINAL_DAY[toks[j]]
            if day is None and i + 1 < len(toks):
                got = _number_at(toks, i + 1)
                if got:
                    day = got[0]
            if day is None:
                return None
            year = today.year
            found = _safe_date(year, mo, day)
            if found and found < today:
                found = _safe_date(year + 1, mo, day)
            return found
    # solo el dia del mes: "el 15", "el quince"
    for i, tok in enumerate(toks):
        if tok == "el" and i + 1 < len(toks):
            got = _number_at(toks, i + 1)
            if got and 1 <= got[0] <= 31 and "las" not in toks[max(0, i - 1) : i + 1]:
                if i + 1 + got[1] < len(toks) and toks[i + 1 + got[1]] in ("y", "menos"):
                    continue  # "el de las ..." / hora
                for add in (0, 1):
                    mo = (today.month - 1 + add) % 12 + 1
                    yr = today.year + (today.month - 1 + add) // 12
                    found = _safe_date(yr, mo, got[0])
                    if found and found >= today:
                        return found
    return None


# ------------------------------------------------------------------ eleccion de opcion
_ORDINALS: dict[str, int] = {
    "primero": 0,
    "primera": 0,
    "primer": 0,
    "segundo": 1,
    "segunda": 1,
    "tercero": 2,
    "tercera": 2,
    "tercer": 2,
}


def parse_slot_choice(text: str, slots: Sequence[datetime]) -> int | None:
    """Indice de la opcion elegida: 'el primero', 'la segunda', 'el ultimo', 'el de las tres'.

    Devuelve None si no se entiende o si es ambiguo (varias opciones calzan).
    """
    n = _norm(text)
    if not n or not slots:
        return None
    toks = n.split()
    time_ctx = "las" in toks or "la" in toks and any(t.isdigit() for t in toks)
    # "el de las tres" -> hora; tiene prioridad sobre numeros sueltos
    if re.search(r"\bde las?\b", n) or re.search(r"\b(a )?las?\b", n) and time_ctx:
        spoken = parse_spoken_time(text)
        if spoken is not None:
            hits = [i for i, s in enumerate(slots) if spoken.matches(s)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                return None
    for tok in toks:
        if tok in _ORDINALS and _ORDINALS[tok] < len(slots):
            return _ORDINALS[tok]
    if "ultimo" in toks or "ultima" in toks:
        return len(slots) - 1
    m = re.search(r"\b(?:opcion|numero|la|el) (\w+)\b", n)
    if m:
        v = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS.get(m.group(1))
        if v is not None and 1 <= v <= len(slots) and m.group(0).split()[0] in ("opcion", "numero"):
            return v - 1
    # hora sin "de las" ("a las tres", "tres de la tarde", "las 10")
    spoken = parse_spoken_time(text)
    if spoken is not None:
        hits = [i for i, s in enumerate(slots) if spoken.matches(s)]
        if len(hits) == 1:
            return hits[0]
        return None
    # por franja: "el de la tarde"
    period = _period_in(n)
    if period is not None:
        hits = [i for i, s in enumerate(slots) if (to_bogota(s).hour >= 12) == (period == "pm")]
        if len(hits) == 1:
            return hits[0]
    # solo un numero: "uno", "dos"
    if len(toks) == 1:
        v = int(toks[0]) if toks[0].isdigit() else _NUM_WORDS.get(toks[0])
        if v is not None and 1 <= v <= len(slots):
            return v - 1
    return None


# ------------------------------------------------------------------ salida hablada
def spoken_time(dt: datetime, *, with_period: bool = True) -> str:
    """'a las tres y media de la tarde' / 'a la una de la tarde' / 'a las diez menos cuarto'."""
    local = to_bogota(dt)
    h24, minute = local.hour, local.minute
    shown_h = h24
    if minute == 45:
        shown_h = (h24 + 1) % 24
    h12 = shown_h % 12 or 12
    hw = "una" if h12 == 1 else number_to_words(h12)
    art = "a la" if h12 == 1 else "a las"
    if h24 == 12 and minute == 0:
        return "al mediodía"
    if minute == 0:
        core = hw
    elif minute == 15:
        core = f"{hw} y cuarto"
    elif minute == 30:
        core = f"{hw} y media"
    elif minute == 45:
        core = f"{hw} menos cuarto"
    else:
        core = f"{hw} y {number_to_words(minute)}"
    if with_period:
        ref = shown_h
        if ref < 6:
            per = "de la madrugada"
        elif ref < 12:
            per = "de la mañana"
        elif ref < 19:
            per = "de la tarde"
        else:
            per = "de la noche"
        core += f" {per}"
    return f"{art} {core}"


def spoken_date(dt: datetime | date, *, today: date | None = None) -> str:
    """'hoy', 'mañana' o 'el lunes quince de noviembre'."""
    d = to_bogota(dt).date() if isinstance(dt, datetime) else dt
    if today is not None:
        if d == today:
            return "hoy"
        if d == today + timedelta(days=1):
            return "mañana"
    day = "primero" if d.day == 1 else number_to_words(d.day)
    return f"el {_WEEKDAYS[d.weekday()]} {day} de {_MONTHS[d.month - 1]}"


def spoken_slot(dt: datetime, *, today: date | None = None) -> str:
    return f"{spoken_date(dt, today=today)} {spoken_time(dt)}"


def select_slots_to_offer(
    slots: Sequence[datetime], limit: int = MAX_SPOKEN_SLOTS
) -> list[datetime]:
    """Hasta ``limit`` horarios ordenados y variados (distinto dia/franja antes que repetir)."""
    uniq = sorted({s for s in slots})
    limit = max(1, min(limit, MAX_SPOKEN_SLOTS))
    if len(uniq) <= limit:
        return uniq
    chosen: list[datetime] = []
    seen: set[tuple[date, bool]] = set()
    for s in uniq:
        local = to_bogota(s)
        key = (local.date(), local.hour >= 12)
        if key not in seen:
            seen.add(key)
            chosen.append(s)
        if len(chosen) == limit:
            break
    for s in uniq:
        if len(chosen) == limit:
            break
        if s not in chosen:
            chosen.append(s)
    return sorted(chosen)


def offer_slots_text(
    slots: Sequence[datetime], *, today: date | None = None, limit: int = MAX_SPOKEN_SLOTS
) -> str:
    """Oferta hablada natural, maximo 3 opciones y una pregunta final."""
    picked = select_slots_to_offer(slots, limit)
    if not picked:
        return "No tengo horarios libres en esas fechas. ¿Quieres probar con otro día?"
    days = {to_bogota(s).date() for s in picked}
    if len(picked) == 1:
        return f"Tengo {spoken_slot(picked[0], today=today)}. ¿Te sirve?"
    if len(days) == 1:
        head = spoken_date(picked[0], today=today)
        times = [spoken_time(s) for s in picked]
        return f"{head.capitalize()} tengo {_join(times)}. ¿Cuál prefieres?"
    return f"Tengo {_join([spoken_slot(s, today=today) for s in picked])}. ¿Cuál prefieres?"


def confirm_text(service_name: str, dt: datetime, *, today: date | None = None) -> str:
    """Repite servicio, fecha y hora para confirmar antes de reservar."""
    return f"Te agendo {service_name} {spoken_slot(dt, today=today)}. ¿Lo confirmo?"


def _join(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " o " + items[-1]
