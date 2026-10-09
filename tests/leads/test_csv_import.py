import io

import pytest
from sqlalchemy import select

from app.db.models.leads import Lead, LeadSource, ReviewSignal
from app.leads import csv_import as ci

CSV_ES = (
    "Titulo,Telefono,Direccion,Sitio web,Calificacion,Resenas,Texto resenas,Estado\n"
    'Dental Sonrisas,300 123 4567,Cra 1,https://www.sonrisas.co/?utm_a=1,"4,2",120,'
    '"Nunca me contestaron el whatsapp",OPERATIONAL\n'
    "Sin Contacto,,Calle 2,,,,,\n"
    "Cerrado SAS,3001112233,x,,4.0,10,,CLOSED_PERMANENTLY\n"
    ",3009998877,x,,,,,\n"
    "Dental Sonrisas Dup,3001234567,x,,,,,\n"
)


def parse(text: str, **kw):
    data = text.encode() if isinstance(text, str) else text
    cm = ci.detect_columns(ci.read_headers(data))
    return list(ci.parse_csv(io.BytesIO(data), cm, niche="dentista", city="Cali")), cm


def test_detect_columns_es_en():
    cm = ci.detect_columns(["Título", "Teléfono", "Calificación", "Reseñas", "cid"])
    assert (cm.name, cm.phone, cm.rating, cm.reviews_count) == (
        "Título",
        "Teléfono",
        "Calificación",
        "Reseñas",
    )
    assert cm.place_id is None
    en = ci.detect_columns(["name", "phone", "stars", "user_ratings_total", "placeId"])
    assert en.place_id == "placeId" and en.reviews_count == "user_ratings_total"


def test_detect_requires_name():
    with pytest.raises(ci.CsvImportError):
        ci.detect_columns(["phone"])


def test_decode_variants():
    assert ci.decode_bytes("﻿a,b".encode()) == "a,b"
    assert ci.decode_bytes("Reseña".encode("latin-1")) == "Reseña"
    with pytest.raises(ci.CsvImportError):
        ci.decode_bytes(b"x" * (ci.MAX_BYTES + 1))
    with pytest.raises(ci.CsvImportError):
        ci.decode_bytes(b"\x00\x01binary")


def test_parse_rows_and_semicolon():
    cands, _ = parse(CSV_ES.replace(",", ";").replace('"4;2"', '"4,2"'))
    first = cands[0]
    assert first.name == "Dental Sonrisas"
    assert first.phone_e164 == "+573001234567"
    assert first.website_domain == "sonrisas.co"
    assert first.review_count == 120
    assert str(first.rating) == "4.2"


def test_parse_comma_and_errors():
    cands, _ = parse(CSV_ES)
    assert isinstance(cands[3], ci.RowError)
    assert cands[2].business_status == "CLOSED_PERMANENTLY"
    assert len(cands) == 5


def test_parse_row_limit(monkeypatch):
    monkeypatch.setattr(ci, "MAX_ROWS", 2)
    with pytest.raises(ci.CsvImportError):
        parse("name,phone\na,3001\nb,3002\nc,3003\n")


def test_parse_instagram_website():
    cands, _ = parse("name,phone,website\nX,3001234567,https://instagram.com/tienda\n")
    assert cands[0].website is None and cands[0].instagram_handle == "tienda"


def test_sha():
    assert len(ci.file_sha256(b"a")) == 64


async def make_source(session):
    src = LeadSource(kind="csv_import", label="t", params={"sha256": "abc"})
    session.add(src)
    await session.flush()
    return src


async def test_dry_run_writes_nothing(session):
    cands, _ = parse(CSV_ES)
    src = await make_source(session)
    rep = await ci.import_csv(session, src, cands, dry_run=True)
    assert rep.dry_run and rep.created == 1 and rep.merged == 1
    assert (rep.skipped_no_phone, rep.skipped_closed, rep.invalid) == (1, 1, 1)
    assert rep.preview[0]["name"] == "Dental Sonrisas"
    assert (await session.execute(select(Lead))).scalars().all() == []


async def test_commit_then_reimport_merges(session):
    cands, _ = parse(CSV_ES)
    src = await make_source(session)
    rep = await ci.import_csv(session, src, cands, dry_run=False)
    assert rep.created == 1
    lead = (await session.execute(select(Lead))).scalar_one()
    assert lead.phone_hash and lead.phone_enc and lead.score > 0
    assert lead.source_id == src.id
    assert (await session.execute(select(ReviewSignal))).scalars().all()[0].source == "csv"
    assert src.stats["created"] == 1
    assert (await ci.find_reupload(session, "abc")).id == src.id
    assert await ci.find_reupload(session, "zzz") is None

    cands2, _ = parse(CSV_ES)
    rep2 = await ci.import_csv(session, src, cands2, dry_run=False)
    assert rep2.created == 0 and rep2.merged == 2


async def test_similar_name_flagged_not_merged(session):
    src = await make_source(session)
    a, _ = parse("name,phone\nClinica Dental Sonrisas Cali,3001234567\n")
    await ci.import_csv(session, src, a, dry_run=False)
    b, _ = parse("name,phone\nClinica Dental Sonrisas Cali,3119876543\n")
    rep = await ci.import_csv(session, src, b, dry_run=False)
    assert rep.created == 1 and rep.possible_duplicates == 1
    leads = (await session.execute(select(Lead))).scalars().all()
    assert len(leads) == 2 and any("posible_duplicado" in x.tags for x in leads)


async def test_error_list_capped(session):
    src = await make_source(session)
    rows = [ci.RowError(i, "x") for i in range(80)]
    rep = await ci.import_csv(session, src, rows, dry_run=True)
    assert rep.invalid == 80 and len(rep.errors) == ci.MAX_ERRORS_REPORTED


def test_sanitizes_control_chars_and_non_http_maps_url():
    text = "name,phone,link\n" + '"Cafe\x07 X\nY",3001234567,javascript:alert(1)\n'
    rows, _ = parse(text)
    cand = rows[0]
    assert cand.name == "Cafe X Y"
    assert cand.maps_url is None


def test_keeps_https_maps_url():
    rows, _ = parse("name,phone,link\nA,3001234567,https://maps.google.com/?cid=1\n")
    assert rows[0].maps_url == "https://maps.google.com/?cid=1"
