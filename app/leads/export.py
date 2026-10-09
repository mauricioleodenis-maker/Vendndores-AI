"""Exportacion CSV de leads con neutralizacion de inyeccion de formulas."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence
from typing import Any

EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("name", "Nombre"),
    ("niche", "Nicho"),
    ("city", "Ciudad"),
    ("address", "Direccion"),
    ("phone_e164", "Telefono"),
    ("phone_type", "Tipo telefono"),
    ("website", "Sitio web"),
    ("instagram", "Instagram"),
    ("rating", "Calificacion"),
    ("review_count", "Resenas"),
    ("score", "Score"),
    ("stage", "Etapa"),
    ("disposition", "Estado"),
    ("maps_url", "Google Maps"),
    ("tags", "Etiquetas"),
)
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r", "\n")
_SAFE_PHONE = re.compile(r"^\+\d{6,15}$")
_SAFE_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def neutralize_cell(value: Any) -> str:
    """Convierte a texto y antepone ``'`` si podria interpretarse como formula en Excel/Sheets."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "si" if value else "no"
    if isinstance(value, (list, tuple, set)):
        value = ", ".join(str(v) for v in value)
    text = str(value)
    if text.startswith(_DANGEROUS) and not _SAFE_PHONE.match(text) and not _SAFE_NUMBER.match(text):
        return "'" + text
    return text


def export_leads_csv(
    leads: Iterable[Any], columns: Sequence[tuple[str, str]] = EXPORT_COLUMNS
) -> str:
    """CSV (UTF-8 con BOM para Excel) con encabezados en espanol, sin notas."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow([label for _, label in columns])
    for lead in leads:
        writer.writerow([neutralize_cell(getattr(lead, attr, None)) for attr, _ in columns])
    return "﻿" + buf.getvalue()
