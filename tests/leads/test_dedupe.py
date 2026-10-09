from app.core.crypto import phone_hash
from app.db.models.leads import Lead, LeadEvent
from app.leads import dedupe as d
from app.leads.normalize import name_key


def mk(name, **kw):
    return Lead(name=name, name_key=name_key(name), city=kw.pop("city", "Cali"), **kw)


def test_trigram_similarity():
    assert d.trigram_similarity("abc", "abc") == 1.0
    assert d.trigram_similarity("", "abc") == 0.0
    assert d.trigram_similarity("dental sonrisas", "taller pepe") < 0.2


async def test_priority_order(session):
    a = mk("Alfa", place_id="P1")
    b = mk("Beta", phone_hash=phone_hash("+573001234567"), phone_e164="+573001234567")
    c = mk("Gamma", website_domain="gamma.co")
    session.add_all([a, b, c])
    await session.flush()
    m = await d.find_duplicate(session, place_id="P1", phone_hash=b.phone_hash)
    assert (m.lead.id, m.kind, m.auto_merge) == (a.id, "place_id", True)
    m = await d.find_duplicate(session, phone_hash=b.phone_hash, website_domain="gamma.co")
    assert m.kind == "phone_hash"
    m = await d.find_duplicate(session, website_domain="gamma.co")
    assert m.kind == "website_domain"
    assert await d.find_duplicate(session, website_domain="instagram.com") is None
    assert await d.find_duplicate(session) is None
    assert await d.find_duplicate(session, website_domain="gamma.co", exclude_id=c.id) is None


async def test_name_similarity_same_city_only(session):
    session.add(mk("Clinica Dental Sonrisas"))
    await session.flush()
    key = name_key("Clínica Dental Sonrisas S.A.S")
    m = await d.find_duplicate(session, name_key=key, city="Cali")
    assert m.kind == "name_similar" and not m.auto_merge
    assert await d.find_duplicate(session, name_key=key, city="Bogota") is None
    assert await d.find_duplicate(session, name_key="otro negocio", city="Cali") is None


async def test_merge_leads(session):
    w = mk("W", tags=["a"], notes="n1")
    lo = mk(
        "L",
        tags=["b"],
        notes="n2",
        place_id="PL",
        website="https://l.co",
        phone_hash="h",
        phone_e164="+573001234567",
        review_count=50,
        rating=4.1,
        disposition="no_contactar",
    )
    session.add_all([w, lo])
    await session.flush()
    session.add(LeadEvent(lead_id=lo.id, kind="note"))
    await session.flush()
    out = await d.merge_leads(session, w, lo)
    assert out is w and w.place_id == "PL" and w.website == "https://l.co"
    assert w.tags == ["a", "b"] and w.disposition == "no_contactar"
    assert lo.disposition == "perdido" and "merged" in lo.tags
    assert w.review_count == 50 and "n2" in w.notes
    from sqlalchemy import select

    ev = (await session.execute(select(LeadEvent))).scalar_one()
    assert ev.lead_id == w.id
    assert await d.merge_leads(session, w, w) is w
