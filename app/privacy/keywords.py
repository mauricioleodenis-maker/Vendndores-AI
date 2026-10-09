"""Deteccion de palabras clave (insensible a tildes y mayusculas, mensaje completo)."""

from __future__ import annotations

import re
import unicodedata

_OPTOUT = frozenset(
    {
        "stop",
        "stop all",
        "parar",
        "baja",
        "dar de baja",
        "darme de baja",
        "no mas",
        "salir",
        "unsubscribe",
        "desuscribir",
        "desuscribirme",
        "cancelar suscripcion",
        "cancelar mi suscripcion",
        "no quiero mas mensajes",
        "no me escriban mas",
        "no me escribas mas",
        "dejar de recibir mensajes",
    }
)
_OPTIN = frozenset({"start", "iniciar", "activar", "suscribir", "suscribirme", "si quiero"})
_ERASE = frozenset({"borrar mis datos", "borrar datos", "eliminar mis datos", "suprimir mis datos"})
_RIGHTS = frozenset({"derechos", "mis derechos"})
_NON_ALPHA = re.compile(r"[^a-z ]")
_SPACES = re.compile(r"\s+")


def normalize_keyword(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text[:200].lower()).encode("ascii", "ignore").decode()
    return _SPACES.sub(" ", _NON_ALPHA.sub(" ", folded)).strip()


def is_optout_message(text: str) -> bool:
    """``cancelar`` solo NO es opt-out (puede ser cancelar una cita)."""
    return normalize_keyword(text) in _OPTOUT


def is_optin_message(text: str) -> bool:
    return normalize_keyword(text) in _OPTIN


def is_erase_request(text: str) -> bool:
    return normalize_keyword(text) in _ERASE


def is_rights_request(text: str) -> bool:
    return normalize_keyword(text) in _RIGHTS
