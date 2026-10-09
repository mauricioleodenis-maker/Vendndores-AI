"""Guardrails de entrada y salida del recepcionista (puros, sin I/O, deterministas).

Entrada: longitud, inyeccion de prompt, urgencia medica, consejo medico, peticion de humano,
datos de terceros, identidad, temas ajenos. Salida: fuga del prompt (canary), diagnosticos y
medicamentos, precios fuera de catalogo, URLs no permitidas, datos de otras personas, promesas.
El codigo bloquea aunque el LLM "obedezca": el prompt solo es la primera barrera.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

MAX_INPUT_CHARS = 1500
MAX_OUTPUT_CHARS = 1500
INJECTION_HANDOFF_AT = 3
OFFTOPIC_HANDOFF_AT = 3

DEFAULT_REPLIES: dict[str, str] = {
    "out_of_scope_reply": (
        "Eso no lo puedo orientar por aquí, pero con gusto te ayudo con nuestros servicios, "
        "precios, horarios o con agendar una cita. ¿Qué necesitas?"
    ),
    "injection_reply": (
        "No puedo ayudarte con eso. Soy el asistente virtual del negocio y solo puedo "
        "informarte sobre servicios, precios, horarios y citas. ¿Te ayudo con alguno?"
    ),
    "urgent_reply": (
        "Lamento que estés pasando por esto. No puedo recomendarte medicamentos ni dosis. "
        "Si es una urgencia, acude a urgencias o llama al 123. Ya avisé a una persona del "
        "equipo para que te escriba y te ayude a conseguir una cita prioritaria."
    ),
    "crisis_reply": (
        "Lamento mucho que te sientas así; no estás solo/a y tu vida importa. Por favor "
        "comunícate ahora con la línea de salud mental: en Bogotá llama al 106 y en el resto "
        "de Colombia a la línea 192 opción 4 (gratuitas, 24 horas), o a urgencias al 123. "
        "Ya avisé a una persona del equipo para que te escriba."
    ),
    "medical_reply": (
        "No puedo recomendarte medicamentos ni dosis ni dar diagnósticos; eso lo valora el "
        "profesional en consulta. Si es urgente, acude a urgencias o llama al 123. "
        "Si quieres, te ayudo a agendar una valoración."
    ),
    "handoff_reply": (
        "Claro, ya avisé a una persona del equipo para que te escriba por aquí lo antes posible."
    ),
    "privacy_reply": (
        "Por protección de datos personales no puedo compartir información de otras personas. "
        "Puedo ayudarte con tu propia cita o con información de nuestros servicios."
    ),
    "price_unknown_reply": (
        "Para darte el valor exacto, el equipo te lo confirma. ¿Quieres que te escriba "
        "una persona del equipo o prefieres que te ayude a agendar una valoración?"
    ),
    "fallback_reply": "Un asesor te responderá pronto. Gracias por tu paciencia.",
    "safe_reply": (
        "Prefiero que una persona del equipo te confirme ese punto. "
        "¿Te ayudo con algo más sobre nuestros servicios o citas?"
    ),
    "too_long_reply": ("Tu mensaje es muy largo para mí. ¿Puedes resumirlo en unas pocas frases?"),
    "empty_reply": "No alcancé a entenderte. ¿Puedes escribirlo de nuevo, por favor?",
    "limit_reply": (
        "En este momento no puedo atender más conversaciones automáticas. "
        "Una persona del equipo te responderá pronto."
    ),
}


def fold(text: str) -> str:
    """Minusculas sin acentos ni signos raros, espacios colapsados (para heuristicas)."""
    stripped = unicodedata.normalize("NFKD", text.lower())
    ascii_only = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", ascii_only).strip()


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


# --------------------------------------------------------------------------- patrones de entrada
_INJECTION = _rx(
    r"ignora\w*\s+(?:\w+\s+){0,3}(?:instrucciones|reglas|indicaciones|anterior\w*)",
    r"olvida\w*\s+(?:\w+\s+){0,3}(?:instrucciones|reglas|todo)",
    r"ignore\s+(?:\w+\s+){0,3}(?:instructions|rules|prompt)",
    r"disregard\s+(?:\w+\s+){0,3}(?:instructions|rules)",
    r"system\s*prompt|prompt\s+(?:completo|inicial|del sistema|de sistema)|tu\s+prompt",
    r"(?:muestra|dime|revela|pega|repite|imprime|escribe)\w*\s+(?:\w+\s+){0,3}"
    r"(?:configuracion|instrucciones|reglas internas|prompt|api\s*key|clave)",
    r"(?:reveal|show|print|repeat)\s+(?:\w+\s+){0,3}(?:prompt|instructions|config)",
    r"modo\s+(?:debug|desarrollador|dios|admin\w*|mantenimiento)|developer\s+mode|jailbreak",
    r"sin\s+reglas|sin\s+restricciones|without\s+(?:rules|restrictions)",
    r"actua\s+como|actuar\s+como|\bact\s+as\b|finge\s+(?:ser|que)|pretend\s+to\s+be",
    r"eres\s+ahora|a\s+partir\s+de\s+ahora\s+(?:eres|seras|responde)|you\s+are\s+now|from\s+now\s+on",
    r"\[\s*system\s*\]|<\s*/?\s*system\s*>|nuevo\s+mandato|mandato\s+del\s+operador",
    r"soy\s+(?:el|la)\s+(?:programador|desarrollador|administrador|creador|dueno de la agencia)",
    r"traduce\w*\s+(?:\w+\s+){0,4}(?:prompt|instrucciones)",
    r"</?\s*(?:user_message|untrusted_source|business_knowledge|system|instructions)\b",
    r"api\s*-?key|clave\s+(?:de\s+)?api|secret\s*key|\btoken\s+de\s+acceso",
    r"[A-Za-z0-9+/]{80,}={0,2}",
)
_DAN = re.compile(r"\bDAN\b")
_URGENT = _rx(
    r"no\s+puedo\s+respirar|dificultad\s+para\s+respirar|me\s+ahogo",
    r"sangrado\s+(?:abundante|fuerte|que\s+no\s+para)|hemorragia|no\s+para\s+de\s+sangrar",
    r"desmay|convulsi|intoxic|sobredosis|infarto|me\s+quiero\s+morir",
    r"dolor\s+(?:\w+\s+){0,4}(?:fuerte|intenso|insoportable|terrible)",
    r"(?:fuerte|intenso|insoportable)\s+dolor|me\s+duele\s+(?:muchisimo|demasiado)",
    r"cara\s+(?:muy\s+)?inflamada|hinchazon\s+en\s+(?:la\s+)?cara",
)
_CRISIS = _rx(
    r"suicid|quitarme\s+la\s+vida|quitarle\s+la\s+vida\s+a\s+mi|acabar\s+con\s+mi\s+vida|"
    r"terminar\s+con\s+mi\s+vida|no\s+quiero\s+(?:seguir\s+)?vivir|ya\s+no\s+quiero\s+vivir|"
    r"quiero\s+morirme|hacerme\s+dano|hacerme\s+mal\s+a\s+mi\s+mismo|matarme|"
    r"mejor\s+(?:estar|estaria)\s+muert",
)
_MEDICAL_ADVICE = _rx(
    r"(?:que|cual)\s+(?:\w+\s+){0,2}(?:pastilla|medicamento|pildora|antibiotico|analgesico|"
    r"calmante|jarabe|pomada|crema)\s+(?:\w+\s+){0,3}(?:tomo|tomar|uso|usar|aplico|recomiendas|me\s+das)",
    r"\bdosis\b|cuantos\s+(?:mg|miligramos|pastillas)|puedo\s+tomar\s+(?:\w+\s+){0,2}"
    r"(?:ibuprofeno|acetaminofen|paracetamol|aspirina|antibiotico|amoxicilina)",
    r"me\s+(?:puedes\s+)?(?:recetar|formular)|receta\s+medica|diagnostic\w*\s+(?:\w+\s+){0,3}(?:tengo|mi)",
    r"que\s+(?:enfermedad|infeccion)\s+(?:tengo|es)",
)
_HUMAN = _rx(
    r"(?:hablar|comunicar\w*|pasa\w*|conectar\w*|contactar)\s+(?:\w+\s+){0,3}"
    r"(?:humano|persona|asesor\w*|agente|alguien|ser\s+humano|encargad\w+|operador\w*)",
    r"(?:humano|persona\s+real|asesor)\s+(?:ya|por\s+favor|ahora)",
    r"que\s+me\s+atienda\s+(?:\w+\s+){0,2}(?:persona|humano|alguien)",
    r"talk\s+to\s+(?:a\s+)?(?:human|person|agent)",
)
_THIRD_PARTY = _rx(
    r"(?:telefono|numero|celular|correo|email|direccion|cita|citas|datos|historia|historial)\s+"
    r"(?:\w+\s+){0,2}de\s+(?:la|el|los|las)\s+(?:senor\w*|sr\w*|paciente\w*|cliente\w*|vecin\w+|"
    r"usuari\w+|doctor\w*|doctora)",
    r"(?:todos|todas)\s+(?:tus|los|las)\s+(?:clientes|pacientes|contactos|numeros|telefonos)",
    r"lista\s+de\s+(?:clientes|pacientes|contactos|telefonos)",
    r"(?:dame|pasame|comparte|envia\w*|exporta\w*|descargar)\s+(?:\w+\s+){0,3}base\s+de\s+datos",
    r"(?:quien|quienes)\s+(?:mas\s+)?(?:tiene|tienen)\s+cita",
)
_IDENTITY = _rx(
    r"eres\s+(?:\w+\s+){0,2}(?:chat\s*gpt|gpt|openai|claude|anthropic|gemini|copilot|llama|"
    r"un\s+robot|un\s+bot|una\s+ia|humano|humana|una\s+persona|real)",
    r"(?:que|cual)\s+(?:modelo|ia|inteligencia)\s+(?:eres|usas|usan)",
    r"(?:quien|que\s+empresa)\s+te\s+(?:creo|hizo|entreno|desarrollo|programo)",
    r"eres\s+(?:de\s+)?(?:openai|google|anthropic)",
)
_OFFTOPIC = _rx(
    r"\bpoema\b|poesia|\bcancion\b|\bchiste\b|\bhoroscopo\b",
    r"\btarea\b|matematic|\d+\s*[x*×]\s*\d+|ecuacion|\bensayo\b",
    r"bitcoin|criptomoned|\bacciones\b|invertir|inversion|bolsa de valores|forex",
    r"president\w*|elecciones|\bpetro\b|\buribe\b|religion|partido\s+politico|\bpoliticos?\b",
    r"\bnovi[oa]\b|romantic|te\s+amo|\bcoquet|sexo|sexual|desnud",
    r"hackear|\bhack\b|\bvirus\b|\bmalware\b|\bphishing\b",
    r"(?:hacer|fabricar|armar)\s+(?:una\s+)?bomba|bomba\s+casera|explosiv|arma\s+casera|"
    r"\bdrogas?\b|como\s+matar",
    r"programa\w*\s+en\s+(?:python|java|js|c\+\+)|escribe\s+(?:un\s+)?(?:codigo|script|programa)|"
    r"\bcodigo\s+en\b|\bpython\b|\bjavascript\b|\bsql\b",
    r"traduce\s+(?:esto|el\s+texto)|dime\s+un\s+secreto|cuentame\s+algo|"
    r"cuantos\s+habitantes|capital\s+de\s+\w+|quien\s+gano",
)
_COMPLAINT = _rx(
    r"\bqueja\b|\breclamo\b|\bdemanda\b|\bdenuncia\b|\bestafa\b|\bfraude\b|"
    r"superintendencia|reembolso|devolucion\s+del\s+dinero|quiero\s+mi\s+dinero",
)

_TAG = re.compile(
    r"</?\s*(?:user_message|untrusted_source|business_knowledge|business_notes|system|assistant|"
    r"instructions|datos_no_confiables)[^>]*>",
    re.IGNORECASE,
)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(slots=True)
class InputVerdict:
    """Resultado de ``check_input``. Si ``reply`` esta definido NO se llama al LLM."""

    flags: list[str] = field(default_factory=list)
    reply: str | None = None
    handoff_reason: str | None = None
    text: str = ""


def sanitize_user_text(text: str) -> str:
    """Neutraliza etiquetas propias y caracteres de control antes de envolver el texto."""
    return _strip_tags(_CTRL.sub("", text)).strip()


def _strip_tags(text: str) -> str:
    """Quita etiquetas propias hasta punto fijo: ``<sys<system>tem>`` no puede reensamblarse."""
    for _ in range(8):
        cleaned = _TAG.sub("", text)
        if cleaned == text:
            return cleaned
        text = cleaned
    return _TAG.sub("", text).replace("<", "(").replace(">", ")")


def wrap_user_text(text: str) -> str:
    return f"<user_message>{sanitize_user_text(text)}</user_message>"


def sanitize_config_text(text: str, *, limit: int = 3000) -> str:
    """Sanea texto que viene de la config generada/scrapeada antes de ir al system prompt."""
    lines: list[str] = []
    for raw in _strip_tags(_CTRL.sub("", text)).splitlines():
        if _INJECTION.search(fold(raw)):
            continue
        lines.append(raw.strip())
    return "\n".join(line for line in lines if line)[:limit]


def reply_for(templates: dict[str, Any] | None, key: str) -> str:
    custom = (templates or {}).get(key)
    if isinstance(custom, str) and custom.strip():
        return custom.strip()[:600]
    return DEFAULT_REPLIES[key]


def check_input(
    text: str,
    *,
    templates: dict[str, Any] | None = None,
    prior_injections: int = 0,
    prior_offtopic_streak: int = 0,
    business_name: str = "",
) -> InputVerdict:
    """Clasifica el mensaje entrante. Orden de prioridad: urgencia, inyeccion, humano, medico,
    terceros, identidad, tema ajeno, queja."""
    clean = sanitize_user_text(text)
    verdict = InputVerdict(text=clean)
    if not clean:
        verdict.flags.append("empty")
        verdict.reply = reply_for(templates, "empty_reply")
        return verdict
    if len(clean) > MAX_INPUT_CHARS:
        verdict.flags.append("too_long")
        verdict.reply = reply_for(templates, "too_long_reply")
        return verdict

    folded = fold(clean)
    hits = {
        "crisis": bool(_CRISIS.search(folded)),
        "urgent": bool(_URGENT.search(folded)),
        "injection": bool(_INJECTION.search(folded))
        or bool(_INJECTION.search(text))
        or bool(_DAN.search(text)),
        "human_request": bool(_HUMAN.search(folded)),
        "medical_advice": bool(_MEDICAL_ADVICE.search(folded)),
        "third_party": bool(_THIRD_PARTY.search(folded)),
        "identity": bool(_IDENTITY.search(folded)),
        "off_topic": bool(_OFFTOPIC.search(folded)),
        "complaint": bool(_COMPLAINT.search(folded)),
    }
    verdict.flags = [name for name, hit in hits.items() if hit]
    if hits["crisis"]:
        verdict.reply = reply_for(templates, "crisis_reply")
        verdict.handoff_reason = "medical_urgent"
    elif hits["urgent"]:
        verdict.reply = reply_for(templates, "urgent_reply")
        verdict.handoff_reason = "medical_urgent"
    elif hits["injection"]:
        verdict.reply = reply_for(templates, "injection_reply")
        if prior_injections + 1 >= INJECTION_HANDOFF_AT:
            verdict.handoff_reason = "injection_suspected"
    elif hits["human_request"]:
        verdict.reply = reply_for(templates, "handoff_reply")
        verdict.handoff_reason = "user_request"
    elif hits["medical_advice"]:
        verdict.reply = reply_for(templates, "medical_reply")
    elif hits["third_party"]:
        verdict.reply = reply_for(templates, "privacy_reply")
    elif hits["identity"]:
        name = business_name or "este negocio"
        verdict.reply = (
            f"Soy el asistente virtual de {name}, un asistente automatizado. "
            "¿Te ayudo con información de servicios, precios u horarios, o con una cita?"
        )
    elif hits["off_topic"]:
        verdict.reply = reply_for(templates, "out_of_scope_reply")
        if prior_offtopic_streak + 1 >= OFFTOPIC_HANDOFF_AT:
            verdict.handoff_reason = "out_of_scope_repeated"
            verdict.reply = reply_for(templates, "handoff_reply")
    elif hits["complaint"]:
        verdict.reply = reply_for(templates, "handoff_reply")
        verdict.handoff_reason = "complaint"
    return verdict


# --------------------------------------------------------------------------- confirmacion
_AFFIRM = {
    "si", "sii", "siii", "claro", "dale", "listo", "ok", "okay", "vale", "confirmo",
    "confirmado", "perfecto", "correcto", "exacto", "agendalo", "reservalo",
    "hagale", "acuerdo", "afirmativo", "seguro", "bueno", "yes",
}  # fmt: skip
_NEGATE = {
    "no",
    "nunca",
    "jamas",
    "tampoco",
    "cancela",
    "cancelar",
    "cancelala",
    "cancelalo",
    "mejor",
    "pero",
    "aunque",
}


def is_affirmation(text: str, *, allow_cancel: bool = False) -> bool:
    """True si el mensaje es una confirmacion explicita (no una pregunta ni una negacion).

    ``allow_cancel`` acepta "si, cancelala" como confirmacion de una cancelacion."""
    folded = fold(text)
    if "?" in folded or not folded:
        return False
    tokens = set(re.findall(r"[a-z]+", folded))
    negations = (
        _NEGATE - {"cancela", "cancelar", "cancelala", "cancelalo"} if allow_cancel else _NEGATE
    )
    if tokens & negations:
        return False
    return bool(tokens & _AFFIRM) or "de una" in folded or "de acuerdo" in folded


# --------------------------------------------------------------------------- salida
_PROVIDER = re.compile(r"\b(?:openai|chat\s*gpt|anthropic|claude|gemini|gpt-?\d\w*)\b", re.I)
_DRUGS = _rx(
    r"\b(?:ibuprofeno|acetaminofen|paracetamol|amoxicilina|naproxeno|diclofenaco|dolex|aspirina|"
    r"antibiotic\w*|analgesic\w*|antiinflamator\w*|cetirizina|loratadina|clindamicina|"
    r"metronidazol|tramadol|dexametasona|prednisona|omeprazol)\b",
    r"\b\d+\s*(?:mg|miligramos|ml)\b|\bdosis\b",
    r"toma\w*\s+(?:un|una|dos|tres|\d+)\s+(?:pastilla|tableta|capsula|comprimido|sobre)",
    r"te\s+recomiendo\s+(?:tomar|tomarte|aplicar|usar)",
)
_DIAGNOSIS = _rx(
    r"\b(?:tienes|tiene|padeces|sufres\s+de|presentas)\s+(?:una\s+|un\s+)?"
    r"(?:caries|infeccion|gingivitis|periodontitis|absceso|diabetes|cancer|hipertension|"
    r"alergia|fractura|enfermedad|inflamacion)\b",
    r"\b(?:probablemente|seguramente|parece\s+que|podria\s+ser|puede\s+ser)\s+"
    r"(?:\w+\s+){0,2}(?:caries|infeccion|gingivitis|absceso|alergia|fractura|enfermedad)\b",
    r"(?:tu|su|el)\s+diagnostico\s+(?:es|seria)|diagnostico\s+de",
)
_PROMISES = ("garantiz", "gratis", "sin costo", "cura definitiva", "100%", "sin ningun costo")
_URL = re.compile(r"(?:https?://|www\.)[^\s)>\]]+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(
    r"(?<![\w$.])(?:\+?57[\s-]?)?3\d{2}[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
    r"|(?<![\w$.])\(?60\d\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"
    r"|(?<![\w$.])\+\d[\d\s().-]{8,}\d"
)
_PRICE = re.compile(
    r"\$\s*(?P<a>\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)(?:\s*(?P<ma>mil|k|millones?)\b)?"
    r"|(?P<b>\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*(?:(?P<mb>mil|millones?)\b|cop\b|pesos\b)",
    re.I,
)


def parse_prices(text: str) -> set[int]:
    """Montos (COP) que aparecen en ``text``: ``$150.000``, ``150 mil``, ``80000 pesos``."""
    found: set[int] = set()
    for m in _PRICE.finditer(text):
        raw = m.group("a") or m.group("b")
        mult = (m.group("ma") or m.group("mb") or "").lower()
        if not raw:
            continue
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
            value = float(re.sub(r"[.,]", "", raw))
        else:
            value = float(raw.replace(",", "."))
        if mult in {"mil", "k"}:
            value *= 1000
        elif mult.startswith("millon"):
            value *= 1_000_000
        found.add(int(value))
    return found


def digits_only(value: str) -> str:
    return re.sub(r"\D", "", value)


def canary_for(secret: str, tenant_key: str = "") -> str:
    digest = hashlib.sha256(f"canary:{secret}:{tenant_key}".encode()).hexdigest()
    return f"VAI-{digest[:12].upper()}"


def _shingles(text: str, n: int = 8) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9]+", fold(text))
    return {tuple(words[i : i + n]) for i in range(max(0, len(words) - n + 1))}


@dataclass(slots=True)
class OutputContext:
    """Datos permitidos para validar la respuesta del LLM."""

    canary: str
    rules_text: str = ""
    allowed_prices: set[int] = field(default_factory=set)
    allowed_hosts: set[str] = field(default_factory=set)
    allowed_phones: set[str] = field(default_factory=set)  # solo digitos
    allowed_emails: set[str] = field(default_factory=set)
    knowledge_folded: str = ""
    templates: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutputVerdict:
    text: str
    flags: list[str] = field(default_factory=list)
    blocked: bool = False
    alert: bool = False


def _phone_allowed(raw: str, allowed: set[str]) -> bool:
    digits = digits_only(raw)
    tail = digits[-10:]
    for a in allowed:
        if not a:
            continue
        if len(a) < 10:  # numeros cortos (123): solo coincidencia exacta, nunca por sufijo
            if digits == a:
                return True
        elif a.endswith(tail) or tail.endswith(a[-10:]):
            return True
    return False


def _host(url: str) -> str:
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", url.lower()).split("/")[0].split(":")[0]


def check_output(text: str, ctx: OutputContext) -> OutputVerdict:
    """Valida/repara la respuesta. ``blocked`` => se reemplazo por una respuesta segura."""
    verdict = OutputVerdict(text=_strip_tags(text).strip())
    out = verdict.text
    folded = fold(out)

    def block(flag: str, reply_key: str, *, alert: bool = False) -> OutputVerdict:
        verdict.flags.append(flag)
        verdict.blocked = True
        verdict.alert = verdict.alert or alert
        verdict.text = reply_for(ctx.templates, reply_key)
        return verdict

    if not out:
        return block("empty_output", "safe_reply")
    if ctx.canary and ctx.canary.lower() in out.lower():
        return block("prompt_leak", "injection_reply", alert=True)
    if ctx.rules_text and _shingles(out) & _shingles(ctx.rules_text):
        return block("prompt_leak", "injection_reply", alert=True)
    if "system prompt" in folded or "mis instrucciones internas" in folded:
        return block("prompt_leak", "injection_reply", alert=True)
    if _PROVIDER.search(out):
        verdict.flags.append("provider_disclosure")
        verdict.blocked = True
        verdict.text = (
            "Soy el asistente virtual del negocio, un asistente automatizado. "
            "¿Te ayudo con servicios, precios, horarios o una cita?"
        )
        return verdict
    if _DRUGS.search(folded) or _DIAGNOSIS.search(folded):
        return block("medical_content", "medical_reply")
    for term in _PROMISES:
        if term in folded and term not in ctx.knowledge_folded:
            return block("unapproved_promise", "safe_reply")
    for url in _URL.findall(out):
        if _host(url) not in ctx.allowed_hosts:
            return block("url_not_allowed", "safe_reply")
    bad_prices = parse_prices(out) - ctx.allowed_prices
    if bad_prices:
        return block("unknown_price", "price_unknown_reply")

    for m in list(_EMAIL.finditer(out)):
        if m.group(0).lower() not in ctx.allowed_emails:
            out = out.replace(m.group(0), "[dato omitido]")
            verdict.flags.append("third_party_data")
    for m in list(_PHONE.finditer(out)):
        if not _phone_allowed(m.group(0), ctx.allowed_phones):
            out = out.replace(m.group(0), "[dato omitido]")
            verdict.flags.append("third_party_data")
    verdict.text = out[:MAX_OUTPUT_CHARS]
    return verdict
