# ruff: noqa: S314, S405
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from app.voice import speech


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "cero"),
        (15, "quince"),
        (21, "veintiuno"),
        (31, "treinta y uno"),
        (100, "cien"),
        (101, "ciento uno"),
        (1000, "mil"),
        (21000, "veintiún mil"),
        (120000, "ciento veinte mil"),
        (1500000, "un millón quinientos mil"),
        (2026, "dos mil veintiséis"),
    ],
)
def test_number_to_words(n, expected):
    assert speech.number_to_words(n) == expected


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("Cuesta $120.000", "Cuesta ciento veinte mil pesos"),
        ("$120,000 COP", "ciento veinte mil pesos"),
        ("COP 35000", "treinta y cinco mil pesos"),
        ("$1.500.000", "un millón quinientos mil pesos"),
        ("$2.000.000", "dos millones de pesos"),
        ("$1", "un peso"),
        ("$50 mil", "cincuenta mil pesos"),
    ],
)
def test_prices(src, expected):
    assert speech.normalize_for_tts(src) == expected


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("3:30 pm", "tres y treinta de la tarde"),
        ("1:00 pm", "una de la tarde"),
        ("9 am", "nueve de la mañana"),
        ("8:15 p. m.", "ocho y quince de la noche"),
        ("12 pm", "doce del mediodía"),
        ("15:30", "tres y treinta de la tarde"),
    ],
)
def test_times(src, expected):
    assert speech.normalize_for_tts(src) == expected


def test_dates():
    assert speech.normalize_for_tts("2026-10-15") == "quince de octubre de dos mil veintiséis"
    assert speech.normalize_for_tts("1/11/2026") == "primero de noviembre de dos mil veintiséis"
    assert "octubre" not in speech.normalize_for_tts("99/99/2026")


def test_phone_digits_and_markdown():
    out = speech.normalize_for_tts("**Llama** 3001234567 https://x.co/a")
    assert "tres cero cero" in out and "http" not in out and "*" not in out


def test_say_escapes_and_attrs():
    x = speech.say('Corte "a" & tinte', voice='Po"lly')
    assert "&amp;" not in x and "&lt;" not in x
    ET.fromstring(f"<R>{x}</R>")


def test_gather_valid_xml_hints_and_fallback():
    x = speech.gather(
        "/webhooks/twilio/voice/gather?a=1&b=2", "Hola", hints=["Corte", "corte", "Tinte, rojo", ""]
    )
    root = ET.fromstring(x)
    g = root.find("Gather")
    assert g.get("input") == "speech" and g.get("language") == "es-CO"
    assert g.get("hints") == "Corte,Tinte rojo"
    assert g.get("action").endswith("a=1&b=2")
    assert g.find("Say").get("language") == "es-MX"
    assert root.find("Hangup") is not None
    assert len(root.findall("Say")) == 2  # reprompt + despedida


def test_hints_bounded():
    h = speech.build_hints([f"servicio {i}" for i in range(200)])
    assert len(h.split(",")) <= speech.MAX_HINTS


def test_goodbye_and_reply_modes():
    assert ET.fromstring(speech.goodbye()).find("Hangup") is not None
    r = ET.fromstring(speech.reply("Listo", action="/g", end_call=True))
    assert r.find("Gather") is None and r.find("Hangup") is not None
    r = ET.fromstring(speech.reply("Listo", action="/g"))
    assert r.find("Gather") is not None
    r = ET.fromstring(speech.reply("Pasando", action="/g", transfer_to="+573001112233"))
    assert r.find("Dial/Number").text == "+573001112233"


def test_transfer_rejects_bad_number():
    r = ET.fromstring(speech.transfer("123; <x>"))
    assert r.find("Dial") is None and r.find("Hangup") is not None
