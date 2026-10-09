"""Mensajes listos para copiar y pegar en WhatsApp (envio manual por el operador).

Secuencia por lead, con los campos ya rellenos:

1. Prueba secreta: pregunta de cliente potencial real (mide el tiempo de respuesta).
2. Apertura suave: primer contacto honesto que NO menciona la venta; solo abre conversacion.
3. Propuesta: se envia cuando el negocio responde la apertura (con evidencia si existe).
4. Demo, seguimientos (dia 1, 3, 7) y respuestas a objeciones.

Reglas (docs/ventas): nunca se finge ser una persona concreta, el operador se presenta con su
nombre real en la apertura, un solo mensaje por paso y se respeta el NO de inmediato.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import ensure_utc, utcnow
from app.core.errors import AppError
from app.db.models.leads import Lead, LeadEvent, SecretShopTest
from app.db.models.tenants import Tenant
from app.db.models.users import User
from app.leads import pipeline, secret_shop
from app.plans.catalog import PLAN_CATALOG

# claves de paso; el orden es el de la secuencia
STEP_KEYS = (
    "prueba_secreta",
    "apertura",
    "propuesta",
    "demo",
    "seguimiento_1",
    "seguimiento_3",
    "seguimiento_7",
)
FOLLOW_UP_DAYS = {"seguimiento_1": 1, "seguimiento_3": 3, "seguimiento_7": 7}
QUEUE_SCAN = 500

GENERIC = "otro"
_NICHE_NOUN = {
    "dentista": "consultorio",
    "clinica_estetica": "clínica",
    "taller": "taller",
    "restaurante": "restaurante",
    GENERIC: "negocio",
}
_NICHE_CLIENTS = {
    "dentista": "pacientes",
    "clinica_estetica": "pacientes",
    "taller": "clientes",
    "restaurante": "clientes",
    GENERIC: "clientes",
}
# prueba secreta: preguntas planas de cliente, una sola se envia
_SECRET_SCRIPTS = {
    "dentista": [
        ("Precio", "Buenas, ¿cuánto cuesta una limpieza dental? ¿Tienen cita esta semana?"),
        ("Cita", "Buenas, quisiera sacar una cita de valoración. ¿Qué días tienen disponibles?"),
        ("Horario", "Buenas, ¿cuál es el horario de atención? ¿Atienden los sábados?"),
    ],
    "clinica_estetica": [
        ("Precio", "Buenas, ¿cuánto vale la valoración? ¿Qué días tienen cupo esta semana?"),
        ("Cita", "Buenas, quisiera agendar una valoración. ¿Qué horarios manejan?"),
        ("Horario", "Buenas, ¿cuál es el horario de atención? ¿Atienden los sábados?"),
    ],
    "taller": [
        (
            "Precio",
            "Buenas, ¿cuánto cobran por una revisión general del carro? ¿Me lo reciben esta semana?",
        ),
        ("Urgencia", "Buenas, mi carro se está recalentando. ¿Me lo pueden recibir hoy?"),
        ("Horario", "Buenas, ¿hasta qué hora atienden y abren los sábados?"),
    ],
    "restaurante": [
        ("Reserva", "Buenas tardes, ¿tienen mesa para 4 personas el sábado a las 8 pm?"),
        ("Menú", "Buenas, ¿me pueden compartir el menú y si hacen domicilios?"),
        ("Horario", "Buenas, ¿hasta qué hora atienden hoy?"),
    ],
    GENERIC: [
        ("Precio", "Buenas, ¿me pueden dar información de precios? ¿Atienden esta semana?"),
        ("Horario", "Buenas, ¿cuál es el horario de atención? ¿Atienden los sábados?"),
    ],
}
# apertura: dos versiones por nicho; {me} = operador, {biz} = negocio, {city}
_OPENERS = {
    "dentista": [
        "Hola, buenas. Soy {me}, de {city}. Vi {biz} en Google y me quedó una duda: ¿el WhatsApp lo contestan ustedes mismos o tienen a alguien para eso?",
        "Buenas tardes, soy {me}, de {city}. Les escribo por {biz}: ¿quién les contesta el WhatsApp cuando están atendiendo a un paciente?",
    ],
    "clinica_estetica": [
        "Hola, buenas. Soy {me}, de {city}. Encontré {biz} en Google. Una pregunta rápida: ¿el WhatsApp de valoraciones lo manejan ustedes o hay alguien encargado?",
        "Buenas, soy {me}, de {city}. Vi las reseñas de {biz} y deben recibir muchos mensajes. ¿Quién se los atiende?",
    ],
    "taller": [
        "Hola, buenas. Soy {me}, de {city}. Vi {biz} en Google Maps. Una pregunta: cuando están metidos en un carro, ¿quién les contesta el WhatsApp de cotizaciones?",
        "Buenas, ¿cómo están? Soy {me}, de {city}. Les escribo por {biz}: ¿las cotizaciones por WhatsApp las responde usted o alguien del taller?",
    ],
    "restaurante": [
        "Hola, buenas. Soy {me}, de {city}. Vi {biz} en Google y quería preguntarles: en horas pico, ¿quién les contesta el WhatsApp de reservas?",
        "Buenas, soy {me}, de {city}. Les escribo por {biz}: ¿las reservas por WhatsApp las llevan ustedes mismos o hay alguien dedicado a eso?",
    ],
    GENERIC: [
        "Hola, buenas. Soy {me}, de {city}. Vi {biz} en Google y me quedó una duda: ¿quién les contesta el WhatsApp de los clientes cuando hay mucho movimiento?",
        "Buenas, soy {me}, de {city}. Les escribo por {biz}: ¿el WhatsApp lo manejan ustedes mismos o tienen a alguien encargado?",
    ],
}
# salida obligatoria en el primer contacto (Ley 1581, docs/ventas/09)
OPT_OUT = " Si prefieren que no les escriba, me dicen y listo."
_PITCH_BENEFIT = {
    "dentista": "agenda citas y responde precios a toda hora, sin que su equipo deje lo que está haciendo",
    "clinica_estetica": "agenda valoraciones y responde precios a toda hora, incluso fuera de horario",
    "taller": "responde cotizaciones y agenda revisiones solo, mientras ustedes trabajan",
    "restaurante": "toma reservas y responde el menú y los horarios mientras atienden mesas",
    GENERIC: "responde las preguntas frecuentes y agenda a toda hora, sin que su equipo deje lo que está haciendo",
}
SLOW_MINUTES = 15  # docs/ventas/02: mas de 15 min en horario = dolor alto


@dataclass(slots=True)
class MessageStep:
    key: str
    title: str
    when: str
    text: str
    wa_url: str | None
    tip: str = ""
    sent_at: Any = None
    due: bool = False
    alternatives: list[tuple[str, str]] = field(default_factory=list)


def _first_name(user: User | None) -> str:
    name = (user.full_name if user else "") or ""
    return name.split()[0] if name.strip() else "[TU_NOMBRE]"


def _business(lead: Lead) -> str:
    return (lead.name or "su negocio").strip()


def wa_url(phone_e164: str | None, text: str) -> str | None:
    """Enlace wa.me con el texto precargado (el operador solo pulsa Enviar)."""
    if not phone_e164:
        return None
    digits = "".join(ch for ch in phone_e164 if ch.isdigit())
    if not digits:
        return None
    return f"https://wa.me/{digits}?text={quote(text)}"


def _evidence_line(ev: dict[str, Any], business: str) -> str | None:
    """Frase con la prueba secreta; solo si hubo dolor (sin respuesta o mas de 15 min)."""
    if not ev.get("available"):
        return None
    if not ev.get("answered"):
        if ev.get("outcome") is None:  # aun esperando respuesta
            return None
        return (
            f"Hace unos días les escribí a {business} como un cliente más y no alcanzó a "
            "llegar respuesta."
        )
    minutes = ev.get("response_minutes")
    if minutes is None or minutes <= SLOW_MINUTES:
        return None
    when = " fuera de horario" if ev.get("after_hours") else ""
    return (
        f"Les escribí a {business} como un cliente más{when} y la respuesta tardó unos "
        f"{minutes} minutos. Le pasa a casi todo negocio con movimiento."
    )


def build_steps(
    lead: Lead,
    user: User | None,
    *,
    evidence: dict[str, Any] | None = None,
    demo_link: str | None = None,
    sent: dict[str, Any] | None = None,
    replied: bool = False,
) -> list[MessageStep]:
    """Arma los mensajes del lead con todos los campos rellenos (funcion pura).

    ``replied``: el negocio respondio despues del ultimo envio; no hay seguimientos pendientes.
    """
    sent = sent or {}
    ev = evidence or {"available": False}
    biz = _business(lead)
    me = _first_name(user)
    niche = lead.niche if lead.niche in _NICHE_NOUN else GENERIC
    clients = _NICHE_CLIENTS[niche]
    city = lead.city or "Cali"
    phone = lead.phone_e164

    # 1. prueba secreta
    scripts = _SECRET_SCRIPTS[niche]
    steps = [
        MessageStep(
            key="prueba_secreta",
            title="Prueba secreta (como cliente)",
            when="Primero, desde el número de la agencia",
            text=scripts[0][1],
            wa_url=wa_url(phone, scripts[0][1]),
            tip=(
                "Envía solo UNA versión, una sola vez por negocio. Regístrala en la ficha del "
                "lead (sección Ventas), revisa a los 15 y 60 min y no muestres capturas a nadie."
            ),
            alternatives=[(label, text) for label, text in scripts[1:]],
        )
    ]

    # 2. apertura: honesta (nombre real), no vende, con salida
    openers = [t.format(me=me, biz=biz, city=city) + OPT_OUT for t in _OPENERS[niche]]
    steps.append(
        MessageStep(
            key="apertura",
            title="Apertura (no menciona la venta)",
            when="Después de la prueba secreta",
            text=openers[0],
            wa_url=wa_url(phone, openers[0]),
            tip=(
                "Solo abre conversación; la propuesta va cuando respondan. Si el negocio nunca "
                "te ha escrito, WhatsApp Business puede exigir plantilla aprobada por Meta."
            ),
            alternatives=[("Variante", t) for t in openers[1:]],
        )
    )

    # 3. propuesta (cuando responden la apertura)
    ev_line = _evidence_line(ev, biz)
    pitch = (
        "¡Gracias por responder! Les cuento por qué preguntaba: trabajo con una recepcionista "
        f"con IA para WhatsApp que {_PITCH_BENEFIT[niche]}. "
        + (f"{ev_line} " if ev_line else "")
        + f"Se la puedo mostrar armada con los datos de {biz} en 2 minutos y, si no les sirve, "
        "no pasa nada. ¿Se la envío?"
    )
    steps.append(
        MessageStep(
            key="propuesta",
            title="Propuesta" + (" con evidencia" if ev_line else ""),
            when="Cuando respondan la apertura",
            text=pitch,
            wa_url=wa_url(phone, pitch),
            tip=""
            if ev_line
            else "Si la prueba secreta muestra demora (más de 15 min), aquí sale el tiempo medido.",
        )
    )

    # 4. demo
    link = demo_link or "[CREA LA DEMO EN LA FICHA DEL LEAD]"
    demo_msg = (
        f"Listo, aquí está la demo de {biz}: {link}\n\nEscríbanle como si fueran uno de sus "
        f"{clients} (pregunten un precio o pidan una cita) y miren cómo responde. Es solo una "
        "demo: no da consejos médicos ni diagnósticos. ¿Qué les parece?"
    )
    steps.append(
        MessageStep(
            key="demo",
            title="Enviar la demo",
            when="Cuando digan que sí",
            text=demo_msg,
            wa_url=wa_url(phone, demo_msg) if demo_link else None,
            tip=""
            if demo_link
            else "Aún no hay demo: créala en la ficha del lead (sección Ventas) y vuelve aquí.",
        )
    )

    # 5. seguimientos: si no han visto la propuesta, no se revela la venta hasta el dia 3
    pitched = "propuesta" in sent or "demo" in sent
    if pitched:
        demo_part = f" Aquí la pueden probar: {demo_link}." if demo_link else ""
        day1 = (
            f"Hola, soy {me}, les escribí sobre la recepcionista con IA para {biz}.{demo_part} "
            "¿Les sirve que lo veamos 10 minutos?"
        )
        day3 = (
            f"Hola, {me} por acá. Si me dicen cuántos mensajes reciben al día en {biz}, les "
            "muestro cómo se vería con sus números. ¿Se lo preparo?"
        )
    else:
        day1 = (
            f"Hola, soy {me} otra vez. Por si se les pasó mi mensaje sobre {biz}. Sin afán, "
            "era solo una pregunta."
        )
        day3 = (
            f"Hola, {me} por acá. Les cuento por qué preguntaba: ayudo a negocios de {city} a "
            "que el WhatsApp responda solo cuando no alcanzan a contestar. ¿Les muestro cómo se "
            f"vería con los datos de {biz}? Si no les interesa, me dicen y listo."
        )
    day7 = (
        f"Hola, esta es mi última nota, {me}. Si no es el momento para {biz}, no hay problema: "
        "con un NO no les escribo más. Y si quieren verlo, díganme un horario y lo agendamos "
        "esta semana."
    )
    follow = {
        "seguimiento_1": ("Seguimiento día 1", day1),
        "seguimiento_3": ("Seguimiento día 3", day3),
        "seguimiento_7": ("Cierre día 7", day7),
    }
    for key, (title, text) in follow.items():
        steps.append(
            MessageStep(
                key=key,
                title=title,
                when=f"{FOLLOW_UP_DAYS[key]} día(s) sin respuesta",
                text=text,
                wa_url=wa_url(phone, text),
                tip="Máximo 3 seguimientos en 7 días. Si responden, detén la secuencia.",
            )
        )

    opener_at = sent.get("apertura") or sent.get("propuesta")
    now = utcnow()
    for st in steps:
        st.sent_at = sent.get(st.key)
        if st.key in FOLLOW_UP_DAYS and opener_at and not st.sent_at and not replied:
            st.due = now - ensure_utc(opener_at) >= timedelta(days=FOLLOW_UP_DAYS[st.key])
    return steps


def _price_line() -> str:
    def cop(v: int) -> str:
        return f"${v:,}".replace(",", ".")

    plans = "; ".join(
        f"{p.name}: {cop(p.setup_fee_cop)} de instalación + {cop(p.monthly_fee_cop)}/mes"
        for p in PLAN_CATALOG
    )
    return (
        f"Los planes son {plans}. Con la oferta Fundador (primeros 5 clientes) la instalación "
        "no tiene costo a cambio de un testimonio en video. Primero les muestro la demo con sus "
        "datos y, si les gusta, vemos el plan que mejor encaje."
    )


def objection_replies(lead: Lead, user: User | None) -> list[tuple[str, str]]:
    """Respuestas de docs/ventas/04-objeciones.md con los datos del lead."""
    biz = _business(lead)
    me = _first_name(user)
    niche = lead.niche if lead.niche in _NICHE_NOUN else GENERIC
    clients = _NICHE_CLIENTS[niche]
    return [
        (
            "¿Quién eres? / ¿De dónde sacaste mi número?",
            f"Soy {me}, de Cali. Vi {biz} en Google Maps, donde el número es público, y creí "
            "que esto les podía servir. Es un solo mensaje: si prefieren que no les escriba "
            "más, respondan NO y los saco de mi lista de inmediato.",
        ),
        ("¿Cuánto cuesta?", _price_line()),
        (
            "Es muy caro",
            "Entiendo. ¿Cuánto les deja hoy una cita que se pierde por no contestar a tiempo? "
            "Si el bot recupera 2 o 3 al mes, el plan se paga solo. Les muestro el cálculo con "
            "sus números.",
        ),
        (
            "¿Y hay garantía?",
            "Sí: si en los primeros 30 días el bot no les funciona como acordamos, les "
            "devolvemos la instalación y la primera mensualidad. Y si funciona pero quieren "
            "cambiar el tono, los servicios o los horarios, hacemos hasta tres rondas de "
            "ajustes sin costo.",
        ),
        (
            "La IA responde mal o se inventa cosas",
            "El bot solo responde con la información que ustedes le dan: precios, horarios y "
            "servicios. Si no sabe, dice que lo consulta con el equipo y les avisa. Lo pueden "
            "probar ustedes mismos antes de activarlo.",
        ),
        (
            "¿Y los datos de mis clientes?",
            "Los datos quedan en su cuenta, no los usamos para otra cosa y solo guardamos lo "
            "necesario para agendar. Está diseñado para la Ley 1581 y podemos firmar un acuerdo "
            "de tratamiento de datos. Les envío el documento.",
        ),
        (
            "Ya tengo secretaria / alguien que responde",
            "El bot no reemplaza a esa persona: se encarga de los mensajes fuera de horario y "
            "de las consultas repetidas (precios, horarios, dirección), para que ella se dedique "
            f"a los {clients} que están ahí.",
        ),
        (
            "Mis clientes quieren hablar con una persona",
            "Claro, por eso el bot pasa a una persona cuando el cliente lo pide o el caso es "
            "urgente. Ustedes deciden qué maneja el bot y qué pasa al equipo.",
        ),
        (
            "Ya uso ManyChat u otro chatbot",
            "Qué bueno que ya automatizan. ¿Su chatbot agenda en el calendario y responde con "
            "IA, o funciona con botones? Nosotros lo configuramos con los datos del negocio y "
            "habla como su equipo.",
        ),
        (
            "Mándame info y lo reviso",
            "Les envío un resumen de una página con el plan y el enlace a la demo. ¿Les escribo "
            "el jueves para saber qué les pareció?",
        ),
        (
            "No tengo tiempo para configurar nada",
            "No tienen que configurar nada. Con sus datos básicos armamos el bot nosotros. "
            "Ustedes solo revisan y aprueban.",
        ),
        (
            "Mis clientes no escriben por WhatsApp",
            "Entonces lo probamos con una semana de piloto. Si el volumen de mensajes es bajo, "
            "no tiene sentido que paguen. Revisamos los datos juntos al final.",
        ),
        (
            "¿Y si se equivoca con una cita?",
            "El bot agenda en su calendario y pueden ver y cambiar cada cita. Si hay conflicto "
            "de horario, no confirma y les avisa.",
        ),
        (
            "Eso es para empresas grandes",
            "Lo diseñamos para negocios como el suyo en Cali. Empezamos con un plan pequeño y "
            "subimos solo si les sirve.",
        ),
        (
            "Déjame pensarlo / hablo con mi socio",
            "Claro. ¿Qué información le llevaría para decidir? Le preparo un resumen con el "
            "plan, la demo y las respuestas a lo que más se pregunta. ¿Lo vemos los dos en 15 "
            "minutos?",
        ),
    ]


# --------------------------------------------------------------------------- persistencia
async def sent_steps(session: AsyncSession, lead_id: uuid.UUID) -> dict[str, Any]:
    """Primer envio registrado por paso (eventos ``outreach_sent`` con ``data.step``)."""
    rows = (
        await session.execute(
            select(LeadEvent.data, LeadEvent.ts)
            .where(LeadEvent.lead_id == lead_id, LeadEvent.kind == "outreach_sent")
            .order_by(LeadEvent.ts)
        )
    ).all()
    out: dict[str, Any] = {}
    for data, ts in rows:
        step = (data or {}).get("step")
        if step in STEP_KEYS and step not in out:
            out[step] = ts
    return out


async def demo_link_for(session: AsyncSession, lead: Lead) -> str | None:
    if lead.demo_tenant_id is None:
        return None
    tenant = await session.get(Tenant, lead.demo_tenant_id)
    if tenant is None or tenant.deleted_at is not None:
        return None
    from app.leads.demo import demo_links

    return demo_links(tenant)[0]


async def last_reply_at(session: AsyncSession, lead_id: uuid.UUID) -> Any:
    return (
        await session.execute(
            select(LeadEvent.ts)
            .where(LeadEvent.lead_id == lead_id, LeadEvent.kind == "reply_received")
            .order_by(LeadEvent.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def steps_for(
    session: AsyncSession, lead: Lead, user: User | None
) -> tuple[list[MessageStep], Any]:
    """Pasos del lead y fecha de su ultima respuesta registrada (o ``None``)."""
    sent = await sent_steps(session, lead.id)
    reply_at = await last_reply_at(session, lead.id)
    latest_sent = max((ensure_utc(v) for v in sent.values()), default=None)
    replied = reply_at is not None and (latest_sent is None or ensure_utc(reply_at) >= latest_sent)
    steps = build_steps(
        lead,
        user,
        evidence=await secret_shop.pitch_evidence(session, lead.id),
        demo_link=await demo_link_for(session, lead),
        sent=sent,
        replied=replied,
    )
    return steps, reply_at


async def mark_sent(session: AsyncSession, lead: Lead, step: str, actor: User) -> None:
    """Registra el envio manual y avanza la etapa cuando corresponde."""
    if step not in STEP_KEYS:
        raise AppError("invalid_step", "Paso desconocido", 422)
    if lead.disposition != "activo":
        raise AppError("lead_inactive", "Este lead no se puede contactar", 409)
    await pipeline.add_event(
        session, lead, "outreach_sent", {"step": step, "channel": "whatsapp_manual"}, actor=actor
    )
    lead.last_contacted_at = utcnow()
    if step == "prueba_secreta" and lead.stage == "nuevo":
        await pipeline.transition_stage(session, lead, "prueba_secreta", actor=actor)
    elif step in ("apertura", "propuesta") and lead.stage in ("nuevo", "prueba_secreta"):
        await pipeline.transition_stage(session, lead, "contactado", actor=actor)
    await session.flush()


async def mark_replied(session: AsyncSession, lead: Lead, actor: User) -> None:
    """El negocio respondio por WhatsApp (registro manual): detiene seguimientos."""
    if lead.disposition != "activo":
        raise AppError("lead_inactive", "Este lead no se puede contactar", 409)
    await pipeline.add_event(
        session, lead, "reply_received", {"channel": "whatsapp_manual"}, actor=actor
    )
    if lead.stage in ("nuevo", "prueba_secreta"):
        await pipeline.transition_stage(session, lead, "contactado", actor=actor)
    await session.flush()


# --------------------------------------------------------------------------- cola del dia
SECRET_CHECKS_MIN = (15, 60)  # docs/ventas/02: revisar a los 15 y a los 60 minutos


@dataclass(slots=True)
class QueueItem:
    lead: Lead
    step: MessageStep | None = None
    note: str = ""


def _secret_note(minutes: int) -> str:
    check = "60" if minutes >= SECRET_CHECKS_MIN[1] else "15"
    return (
        f"Le escribiste hace {minutes} min (revisión de los {check} min): mira si ya "
        "respondieron y registra la hora en la sección Ventas. A las 24 h queda como sin respuesta."
    )


async def daily_queue(
    session: AsyncSession, user: User | None, *, limit: int = 30
) -> dict[str, list[QueueItem]]:
    """Leads activos con telefono agrupados por la siguiente accion pendiente.

    Tres consultas en total (leads, eventos y pruebas secretas en lote).
    """
    stmt = (
        select(Lead)
        .where(
            Lead.disposition == "activo",
            Lead.phone_e164.is_not(None),
            Lead.stage.in_(("nuevo", "prueba_secreta", "contactado", "demo")),
        )
        .order_by(Lead.score.desc(), Lead.created_at)
        .limit(QUEUE_SCAN)
    )
    leads = list((await session.execute(stmt)).scalars())
    groups: dict[str, list[QueueItem]] = {
        "revisar": [],
        "respondieron": [],
        "seguimiento": [],
        "apertura": [],
        "prueba": [],
    }
    if not leads:
        return groups
    ids = [lead.id for lead in leads]
    rows = (
        await session.execute(
            select(LeadEvent.lead_id, LeadEvent.kind, LeadEvent.data, LeadEvent.ts)
            .where(
                LeadEvent.lead_id.in_(ids),
                LeadEvent.kind.in_(("outreach_sent", "reply_received")),
            )
            .order_by(LeadEvent.ts)
        )
    ).all()
    sent_by_lead: dict[uuid.UUID, dict[str, Any]] = {}
    last_sent: dict[uuid.UUID, Any] = {}
    last_reply: dict[uuid.UUID, Any] = {}
    for lead_id, kind, data, ts in rows:
        if kind == "reply_received":
            last_reply[lead_id] = ts
            continue
        step = (data or {}).get("step")
        if step in STEP_KEYS:
            sent_by_lead.setdefault(lead_id, {}).setdefault(step, ts)
            last_sent[lead_id] = ts
    tests = (
        await session.execute(
            select(SecretShopTest)
            .where(SecretShopTest.lead_id.in_(ids))
            .order_by(SecretShopTest.sent_at)
        )
    ).scalars()
    latest_test = {t.lead_id: t for t in tests}
    now = utcnow()

    def add(key: str, item: QueueItem) -> None:
        if len(groups[key]) < limit:
            groups[key].append(item)

    for lead in leads:
        sent = sent_by_lead.get(lead.id, {})
        test = latest_test.get(lead.id)
        if test is not None and test.first_reply_at is None and test.outcome is None:
            minutes = int((now - ensure_utc(test.sent_at)).total_seconds() // 60)
            if minutes >= SECRET_CHECKS_MIN[0]:
                add("revisar", QueueItem(lead, note=_secret_note(minutes)))
            continue
        replied = lead.id in last_reply and (
            lead.id not in last_sent
            or ensure_utc(last_reply[lead.id]) >= ensure_utc(last_sent[lead.id])
        )
        steps = {st.key: st for st in build_steps(lead, user, sent=sent, replied=replied)}
        if replied:
            nxt = "propuesta" if "propuesta" not in sent else ("demo" if "demo" not in sent else "")
            if nxt:
                add("respondieron", QueueItem(lead, steps[nxt]))
            continue
        if lead.stage == "demo":
            continue
        if not sent and test is None:
            add("prueba", QueueItem(lead, steps["prueba_secreta"]))
        elif "apertura" not in sent and "propuesta" not in sent:
            add("apertura", QueueItem(lead, steps["apertura"]))
        else:
            due = [steps[k] for k in ("seguimiento_7", "seguimiento_3", "seguimiento_1")]
            pending = next((st for st in due if st.due), None)
            if pending is not None:
                add("seguimiento", QueueItem(lead, pending))
    return groups
