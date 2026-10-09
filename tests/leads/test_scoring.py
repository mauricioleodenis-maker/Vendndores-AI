from types import SimpleNamespace as NS

from app.leads.scoring import DEFAULT, band_for, review_signal_scan, score_lead


def lead(**kw):
    base = dict(
        business_status="OPERATIONAL",
        rating=None,
        review_count=0,
        phone_e164="+573001234567",
        phone_type="mobile",
        website="https://x.co",
        instagram=None,
        instagram_handle=None,
        niche="taller",
        signals={"web_whatsapp": True},
    )
    base.update(kw)
    return NS(**base)


def test_scan_matches_spanish_patterns():
    res = review_signal_scan(
        [
            "Nunca me respondieron el WhatsApp",
            "Me dejaron en visto",
            "Excelente servicio",
            "NO CONTESTAN EL TELÉFONO",
            "",
            "Imposible comunicarse con ellos",
            "Tardaron mucho en responder",
        ]
    )
    assert [m.index for m in res] == [0, 1, 3, 5, 6]
    assert "nunca_responden" in res[0].patterns


def test_scan_truncates_excerpt():
    assert len(review_signal_scan(["no contestan " + "x" * 500])[0].excerpt) == 280


def test_rating_and_volume():
    r = score_lead(lead(rating=4.2, review_count=250), [], None)
    assert r.breakdown["rating"]["points"] == 25
    assert r.breakdown["volumen_resenas"]["points"] == 20
    assert r.score == 25 + 20 + 15  # + movil
    assert score_lead(lead(rating=3.5), [], None).breakdown["rating"]["points"] == 15
    assert score_lead(lead(rating=4.8), [], None).breakdown["rating"]["points"] == 0
    for n, pts in ((60, 15), (25, 8), (5, 0)):
        assert (
            score_lead(lead(review_count=n), [], None).breakdown["volumen_resenas"]["points"] == pts
        )


def test_signals_one_vs_many():
    s = [NS(matched_patterns=["a"])]
    assert score_lead(lead(), s, None).breakdown["resenas_no_contestan"]["points"] == 15
    s2 = s + [NS(matched_patterns=["b"]), NS(matched_patterns=[])]
    assert score_lead(lead(), s2, None).breakdown["resenas_no_contestan"]["points"] == 25


def test_no_phone_cap_and_landline():
    r = score_lead(lead(phone_e164=None, rating=4.2, review_count=300), [], None)
    assert r.score == 20
    assert "sin_telefono_tope" in r.breakdown
    assert (
        score_lead(lead(phone_type="landline"), [], None).breakdown["contactabilidad"]["points"]
        == 3
    )


def test_web_instagram_and_niche():
    assert score_lead(lead(signals={}), [], None).breakdown["web_sin_boton"]["points"] == 5
    r = score_lead(lead(website=None, instagram_handle="x", niche="dentista"), [], None)
    assert r.breakdown["solo_instagram"]["points"] == 5
    assert r.breakdown["fit_nicho"]["points"] == 5


def test_secret_shop():
    fail = score_lead(lead(), [], NS(outcome="sin_respuesta", response_seconds=None))
    assert fail.breakdown["prueba_secreta"]["points"] == 25
    slow = score_lead(lead(), [], NS(outcome="respuesta_lenta", response_seconds=1200))
    assert slow.breakdown["prueba_secreta"]["points"] == 25
    ok = score_lead(lead(rating=4.2), [], NS(outcome="respuesta_ok", response_seconds=60))
    assert ok.breakdown["prueba_secreta"]["points"] == -15
    mid = score_lead(lead(), [], NS(outcome="respuesta_ok", response_seconds=600))
    assert "prueba_secreta" not in mid.breakdown


def test_closed_business_discarded():
    r = score_lead(lead(business_status="CLOSED_PERMANENTLY", rating=4.2), [], None)
    assert (r.score, r.disposition) == (0, "perdido")


def test_clamped_and_bands():
    r = score_lead(
        lead(rating=4.2, review_count=500, niche="dentista", signals={}),
        [NS(matched_patterns=["a"])] * 3,
        NS(outcome="sin_respuesta", response_seconds=None),
    )
    assert r.score == 100 and r.band == "caliente"
    assert (
        band_for(40) == "tibio" and band_for(39) == "frio" and band_for(70, DEFAULT) == "caliente"
    )
    assert score_lead(lead(), [], NS(outcome="respuesta_ok", response_seconds=1)).score >= 0
