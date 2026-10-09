"""Textos Ley 1581 (support/12). BORRADOR PARA REVISION DE ABOGADO (no usar sin revisar)."""

from __future__ import annotations

from typing import Final

POLICY_VERSION: Final = "1.0"
LEGAL_DRAFT_NOTICE: Final = (
    "Borrador para revisión legal: este texto debe ser revisado por un abogado especializado "
    "en habeas data (Ley 1581 de 2012) antes de su publicación definitiva."
)
PURPOSES: Final = ("atencion", "recordatorios", "marketing")

NOTICE_ATENCION: Final = (
    "Hola, soy el asistente virtual de *{negocio}*. Para gestionar tus citas tratamos tu nombre y "
    "teléfono. Te pedimos *no* compartir datos de salud por este chat. Responsable: {negocio}; "
    "el servicio lo opera {agencia}. Política de privacidad: {url}. "
    "Responde *SI* para continuar, o *STOP* para no recibir recordatorios."
)
NOTICE_RECORDATORIOS: Final = (
    "¿Autorizas que te enviemos recordatorios de tu cita por WhatsApp? Responde *SI* o *NO*. "
    "Puedes cambiar de opinión escribiendo *STOP*."
)
NOTICE_MARKETING: Final = (
    "¿Quieres recibir promociones y novedades de *{negocio}* por WhatsApp? "
    "Responde *SI* para aceptar. *NO* no afecta tu cita."
)
REPLY_CONSENT_OK: Final = (
    "Gracias. Usaremos tus datos solo para gestionar tu atención. Para ejercer tus derechos "
    "escribe *DERECHOS* o visita {url}."
)
REPLY_OPTOUT: Final = (
    "Listo, no recibirás recordatorios ni promociones. Tus citas confirmadas siguen vigentes. "
    "Si quieres borrar tus datos escribe *BORRAR MIS DATOS*."
)
REPLY_OPTIN: Final = (
    "Listo, volverás a recibir nuestros mensajes. Puedes escribir *STOP* cuando quieras."
)
REPLY_ERASE: Final = (
    "Recibimos tu solicitud de supresión de datos. La atenderemos en los plazos de ley y te "
    "confirmaremos por este mismo canal."
)

_NOTICES: Final = {
    "atencion": NOTICE_ATENCION,
    "recordatorios": NOTICE_RECORDATORIOS,
    "marketing": NOTICE_MARKETING,
}


def privacy_notice(
    purpose: str = "atencion",
    *,
    negocio: str = "el negocio",
    agencia: str = "la Agencia",
    url: str = "",
) -> str:
    """Texto exacto del aviso para ``purpose`` (se guarda en ``consents.evidence``)."""
    template = _NOTICES.get(purpose)
    if template is None:
        raise ValueError(f"Finalidad desconocida: {purpose}")
    return template.format(
        negocio=negocio, agencia=agencia, url=url or "nuestra página de privacidad"
    )


# Secciones de la politica publica (support/12 seccion 1). Cada item: (titulo, parrafos).
POLICY_SECTIONS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "Responsable y encargado",
        (
            "[NOMBRE AGENCIA], NIT [NIT], domicilio en Cali, Valle del Cauca, Colombia. "
            "Correo para ejercer derechos: [CORREO_DPO]. Teléfono: [TELEFONO].",
        ),
    ),
    (
        "Alcance",
        (
            "Esta política aplica a los datos personales tratados en el sitio web y formularios, "
            "en la búsqueda y contacto de negocios potenciales, en los asistentes virtuales "
            "(WhatsApp y, en el futuro, voz) operados por cuenta de nuestros clientes y en la "
            "facturación y relación comercial.",
        ),
    ),
    (
        "Finalidades",
        (
            "Contactar a representantes de negocios para ofrecer el servicio de asistente virtual "
            "y respetar las solicitudes de supresión. No vendemos datos personales a terceros.",
            "Respecto de los pacientes y clientes finales de un negocio, la Agencia actúa como "
            "Encargada: trata los datos solo para gestionar citas, enviar recordatorios "
            "autorizados y responder consultas, siguiendo las instrucciones del negocio "
            "(Responsable).",
        ),
    ),
    (
        "Datos sensibles",
        (
            "No solicitamos, almacenamos ni inferimos datos de salud. Si escribes alguno, el "
            "sistema lo enmascara antes de cualquier registro de analítica y el asistente "
            "deriva el caso a una persona. Ningún dato sensible es obligatorio.",
        ),
    ),
    (
        "Tus derechos",
        (
            "Puedes conocer, actualizar, rectificar y suprimir tus datos, revocar la autorización, "
            "solicitar prueba de la autorización y presentar quejas ante la Superintendencia de "
            "Industria y Comercio (SIC). Consultas: [10] días hábiles; reclamos: [15] días "
            "hábiles.",
        ),
    ),
    (
        "Cómo ejercerlos",
        (
            "Escribe por WhatsApp la palabra *STOP* (dejar de recibir recordatorios y promociones) "
            "o *BORRAR MIS DATOS* (supresión). Verificamos tu identidad con el número desde el que "
            "escribes y respondemos por el mismo canal.",
        ),
    ),
    (
        "Transferencia internacional",
        (
            "Para operar el servicio, algunos datos se procesan fuera de Colombia por proveedores "
            "de inteligencia artificial (Anthropic), mensajería (Twilio) y calendario (Google), "
            "enviando solo el contexto mínimo necesario. [Confirmar base legal, art. 26 Ley 1581.]",
        ),
    ),
    (
        "Seguridad y retención",
        (
            "Los datos de contacto y los mensajes se cifran en reposo, el acceso se controla por "
            "rol y se registran auditorías. Los mensajes se conservan un máximo de [N] meses "
            "o hasta la supresión.",
        ),
    ),
    (
        "Vigencia",
        (f"Política v{POLICY_VERSION}. Cualquier cambio sustancial se comunicará con antelación.",),
    ),
)
