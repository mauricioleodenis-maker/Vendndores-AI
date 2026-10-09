"""Importacion de CSV exportados por scrapers de Google Maps (es/en): preview (dry-run) y commit."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, BinaryIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import get_crypto, make_aad, pack_blob, phone_hash
from app.core.errors import AppError
from app.db.models.leads import Lead, LeadEvent, LeadSource, ReviewSignal
from app.leads.dedupe import MATCH_NAME, NameIndex, find_duplicate
from app.leads.normalize import (
    instagram_handle,
    name_key,
    normalize_website,
    to_e164_co,
    website_domain,
)
from app.leads.scoring import review_signal_scan, score_lead

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5_000
MAX_FIELD_CHARS = 20_000
MAX_ERRORS_REPORTED = 50
PREVIEW_ROWS = 20


class CsvImportError(AppError):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(code, message, status)


def _fold(header: str) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", header) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "title", "titulo", "business_name", "nombre", "nombre_del_negocio"),
    "phone": (
        "phone",
        "phone_number",
        "phone_1",
        "phone1",
        "telephone",
        "telefono",
        "celular",
        "whatsapp",
        "numero_de_telefono",
    ),
    "address": ("address", "full_address", "street", "direccion"),
    "website": ("website", "site", "website_url", "sitio_web", "pagina_web", "web", "domain"),
    "rating": ("rating", "stars", "average_rating", "calificacion", "puntuacion", "estrellas"),
    "reviews_count": (
        "reviews",
        "reviews_count",
        "user_ratings_total",
        "review_count",
        "resenas",
        "numero_de_resenas",
        "cantidad_de_resenas",
        "total_resenas",
    ),
    "reviews_text": (
        "reviews_text",
        "review_text",
        "reviews_snippet",
        "texto_resenas",
        "texto_de_resenas",
        "comentarios",
        "review",
    ),
    "maps_url": ("google_maps_url", "maps_url", "link", "url", "enlace", "enlace_de_google_maps"),
    "place_id": ("place_id", "placeid", "google_id", "id_de_lugar"),
    "category": ("category", "type", "categories", "main_category", "categoria", "tipo"),
    "city": ("city", "ciudad"),
    "status": ("business_status", "status", "estado", "estado_del_negocio"),
    "instagram": ("instagram", "instagram_url"),
}


@dataclass(slots=True)
class ColumnMap:
    name: str
    phone: str | None = None
    address: str | None = None
    website: str | None = None
    rating: str | None = None
    reviews_count: str | None = None
    reviews_text: str | None = None
    maps_url: str | None = None
    place_id: str | None = None
    category: str | None = None
    city: str | None = None
    status: str | None = None
    instagram: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {f: getattr(self, f) for f in self.__slots__}


def detect_columns(headers: list[str]) -> ColumnMap:
    """Mapea encabezados (es/en) a campos internos. Exige columna de nombre."""
    folded = {_fold(h): h for h in headers if h and h.strip()}
    found: dict[str, str | None] = {}
    for field_name, aliases in _ALIASES.items():
        found[field_name] = next((folded[a] for a in aliases if a in folded), None)
    if not found["name"]:
        raise CsvImportError(
            "csv_sin_nombre", "No se encontro una columna de nombre (titulo, name, nombre)."
        )
    return ColumnMap(**found)  # type: ignore[arg-type]


def decode_bytes(data: bytes) -> str:
    """UTF-8 (con o sin BOM) con respaldo latin-1."""
    if len(data) > MAX_BYTES:
        raise CsvImportError("csv_muy_grande", "El archivo supera 5 MB.", 413)
    if b"\x00" in data[:4096]:
        raise CsvImportError("csv_invalido", "El archivo no parece un CSV de texto.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reader(text: str) -> csv.DictReader[str]:
    sample = text[:4096]
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        first = sample.splitlines()[0] if sample else ""
        delimiter = ";" if first.count(";") > first.count(",") else ","
    return csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)


def read_headers(data: bytes) -> list[str]:
    reader = _reader(decode_bytes(data))
    return [h for h in (reader.fieldnames or []) if h]


@dataclass(slots=True)
class LeadCandidate:
    row: int
    name: str
    phone_raw: str = ""
    phone_e164: str | None = None
    phone_type: str = "unknown"
    address: str = ""
    city: str = ""
    niche: str = "otro"
    website: str | None = None
    website_domain: str | None = None
    instagram: str | None = None
    instagram_handle: str | None = None
    rating: Decimal | None = None
    review_count: int | None = None
    maps_url: str | None = None
    place_id: str | None = None
    category: str = ""
    business_status: str | None = None
    review_text: str = ""

    @property
    def name_key(self) -> str:
        return name_key(self.name)


@dataclass(frozen=True, slots=True)
class RowError:
    row: int
    reason: str


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value: str) -> str:
    """Sin caracteres de control (log/CSV injection) y espacios colapsados."""
    return " ".join(_CONTROL.sub(" ", value).split())


def _http_url(value: str, limit: int = 500) -> str | None:
    """Solo URLs http(s); descarta ``javascript:``, ``data:`` y similares."""
    value = value.strip()
    return value[:limit] if value.lower().startswith(("http://", "https://")) else None


def _parse_rating(raw: str) -> Decimal | None:
    raw = raw.strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.1")) if Decimal(0) <= value <= Decimal(5) else None


def _parse_int(raw: str) -> int | None:
    digits = re.sub(
        r"[^\d]", "", raw.split(".")[0] if re.fullmatch(r"\d+\.0+", raw.strip()) else raw
    )
    return int(digits) if digits and len(digits) < 9 else None


_CLOSED = {
    "closed",
    "closed_permanently",
    "permanently_closed",
    "cerrado",
    "cerrado_permanentemente",
    "closed_temporarily",
    "true",
    "1",
    "si",
}


def _status(raw: str, column: str | None) -> str | None:
    value = _fold(raw)
    if not value:
        return None
    if column and _fold(column) == "permanently_closed":
        return "CLOSED_PERMANENTLY" if value in _CLOSED else None
    if value in _CLOSED or "cerrad" in value or "closed" in value:
        return "CLOSED_PERMANENTLY"
    return "OPERATIONAL"


def parse_csv(
    stream: BinaryIO, colmap: ColumnMap, *, niche: str, city: str
) -> Iterator[LeadCandidate | RowError]:
    """Genera candidatos y errores por fila (no falla el lote); respeta limites."""
    data = stream.read(MAX_BYTES + 1)
    reader = _reader(decode_bytes(data))
    old_limit = csv.field_size_limit(MAX_FIELD_CHARS)
    try:
        for i, row in enumerate(reader, start=2):
            if i - 1 > MAX_ROWS:
                raise CsvImportError(
                    "csv_muchas_filas", f"El archivo supera {MAX_ROWS} filas.", 413
                )

            def get(col: str | None, _row: dict[str, Any] = row) -> str:
                return (_row.get(col) or "").strip() if col else ""

            name = _clean(get(colmap.name))[:300]
            if not name:
                yield RowError(i, "Fila sin nombre")
                continue
            raw_phone = get(colmap.phone)
            e164, ptype = to_e164_co(raw_phone.split(",")[0].split(";")[0] if raw_phone else None)
            site = normalize_website(get(colmap.website))
            ig_raw = get(colmap.instagram)
            ig = instagram_handle(ig_raw) or instagram_handle(get(colmap.website))
            yield LeadCandidate(
                row=i,
                name=name,
                phone_raw=raw_phone[:40],
                phone_e164=e164,
                phone_type=ptype,
                address=_clean(get(colmap.address))[:300],
                city=_clean(get(colmap.city) or city)[:100],
                niche=niche,
                website=(site or None) if ig is None or website_domain(site) else None,
                website_domain=website_domain(site),
                instagram=f"https://instagram.com/{ig}" if ig else None,
                instagram_handle=ig,
                rating=_parse_rating(get(colmap.rating)),
                review_count=_parse_int(get(colmap.reviews_count)),
                maps_url=_http_url(get(colmap.maps_url)),
                place_id=_clean(get(colmap.place_id))[:200] or None,
                category=_clean(get(colmap.category))[:100],
                business_status=_status(get(colmap.status), colmap.status),
                review_text=get(colmap.reviews_text),
            )
    except csv.Error as exc:
        yield RowError(0, f"CSV malformado: {exc}")
    finally:
        csv.field_size_limit(old_limit)


@dataclass(slots=True)
class CsvImportReport:
    total: int = 0
    created: int = 0
    merged: int = 0
    skipped_no_phone: int = 0
    skipped_closed: int = 0
    possible_duplicates: int = 0
    invalid: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    preview: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = True

    def add_error(self, row: int, reason: str) -> None:
        self.invalid += 1
        if len(self.errors) < MAX_ERRORS_REPORTED:
            self.errors.append({"row": row, "reason": reason})

    def summary(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "created": self.created,
            "merged": self.merged,
            "skipped_no_phone": self.skipped_no_phone,
            "skipped_closed": self.skipped_closed,
            "possible_duplicates": self.possible_duplicates,
            "invalid": self.invalid,
        }


async def find_reupload(session: AsyncSession, sha256: str) -> LeadSource | None:
    """Fuente CSV previa con el mismo hash de archivo (para avisar re-subidas)."""
    rows = (
        await session.execute(
            select(LeadSource)
            .where(LeadSource.kind == "csv_import")
            .order_by(LeadSource.created_at.desc())
            .limit(500)
        )
    ).scalars()
    return next((s for s in rows if (s.params or {}).get("sha256") == sha256), None)


def _fill_empty(lead: Lead, cand: LeadCandidate) -> None:
    for attr in (
        "address",
        "city",
        "website",
        "website_domain",
        "instagram",
        "instagram_handle",
        "maps_url",
        "rating",
        "review_count",
        "phone_e164",
        "phone_type",
    ):
        if getattr(lead, attr) in (None, "") and getattr(cand, attr) not in (None, ""):
            setattr(lead, attr, getattr(cand, attr))
    if not lead.place_id and cand.place_id:
        lead.place_id = cand.place_id


async def import_csv(
    session: AsyncSession,
    source: LeadSource,
    candidates: Iterator[LeadCandidate | RowError] | list[LeadCandidate | RowError],
    *,
    dry_run: bool,
) -> CsvImportReport:
    """Aplica candidatos: crea/fusiona leads. Con ``dry_run`` no escribe nada (solo reporta)."""
    report = CsvImportReport(dry_run=dry_run)
    seen: dict[str, str] = {}  # clave de dedupe intra-archivo -> nombre
    crypto = get_crypto() if not dry_run else None
    name_index = NameIndex()

    for cand in candidates:
        report.total += 1
        if isinstance(cand, RowError):
            report.add_error(cand.row, cand.reason)
            continue
        if cand.business_status and cand.business_status != "OPERATIONAL":
            report.skipped_closed += 1
            continue
        if not cand.phone_e164 and not cand.website and not cand.instagram_handle:
            report.skipped_no_phone += 1
            continue

        p_hash = phone_hash(cand.phone_e164) if cand.phone_e164 else None
        keys = [
            k
            for k in (
                f"p:{cand.place_id}" if cand.place_id else None,
                f"t:{p_hash}" if p_hash else None,
                f"d:{cand.website_domain}" if cand.website_domain else None,
            )
            if k
        ]
        if any(k in seen for k in keys):
            report.merged += 1
            continue
        for k in keys:
            seen[k] = cand.name

        match = await find_duplicate(
            session,
            place_id=cand.place_id,
            phone_hash=p_hash,
            website_domain=cand.website_domain,
            name_key=cand.name_key,
            city=cand.city,
            name_index=name_index,
        )
        if match and match.kind != MATCH_NAME:
            report.merged += 1
            if not dry_run:
                _fill_empty(match.lead, cand)
            continue
        if match:
            report.possible_duplicates += 1
        report.created += 1
        if len(report.preview) < PREVIEW_ROWS:
            report.preview.append(
                {
                    "row": cand.row,
                    "name": cand.name,
                    "phone": cand.phone_e164,
                    "phone_type": cand.phone_type,
                    "website": cand.website_domain,
                    "rating": str(cand.rating) if cand.rating is not None else None,
                    "reviews": cand.review_count,
                    "posible_duplicado": bool(match),
                }
            )
        if dry_run:
            continue
        assert crypto is not None

        lead = Lead(
            source_id=source.id,
            place_id=cand.place_id,
            maps_url=cand.maps_url,
            name=cand.name,
            name_key=cand.name_key,
            niche=cand.niche,
            city=cand.city,
            address=cand.address,
            phone_e164=cand.phone_e164,
            phone_hash=p_hash,
            phone_type=cand.phone_type if cand.phone_e164 else None,
            website=cand.website,
            website_domain=cand.website_domain,
            instagram=cand.instagram,
            instagram_handle=cand.instagram_handle,
            rating=cand.rating,
            review_count=cand.review_count,
            business_status="OPERATIONAL",
            tags=(["posible_duplicado"] if match else [])
            + ([] if cand.phone_e164 else ["sin_telefono"]),
            signals={"duplicate_of": str(match.lead.id)} if match else {},
        )
        if cand.phone_e164:
            lead.phone_enc = pack_blob(
                crypto.encrypt_str(cand.phone_e164, aad=make_aad("leads", None, "phone"))
            )
        session.add(lead)
        await session.flush()
        name_index.add(cand.city, lead.id, cand.name_key)
        matches = review_signal_scan([cand.review_text]) if cand.review_text else []
        for m in matches:
            session.add(
                ReviewSignal(
                    lead_id=lead.id,
                    source="csv",
                    review_ref=hashlib.sha1(m.excerpt.encode()).hexdigest()[:40],  # noqa: S324
                    text_excerpt=m.excerpt,
                    matched_patterns=list(m.patterns),
                )
            )
        res = score_lead(lead, matches, None)
        lead.score, lead.score_breakdown, lead.score_version = res.score, res.breakdown, 1
        if res.disposition:
            lead.disposition = res.disposition
        session.add(
            LeadEvent(
                lead_id=lead.id,
                kind="imported",
                data={"source_id": str(source.id), "row": cand.row},
            )
        )

    if not dry_run:
        source.stats = report.summary()
        await session.flush()
    return report


__all__ = [
    "ColumnMap",
    "CsvImportError",
    "CsvImportReport",
    "LeadCandidate",
    "RowError",
    "decode_bytes",
    "detect_columns",
    "file_sha256",
    "find_reupload",
    "import_csv",
    "parse_csv",
    "read_headers",
]
