import csv
import io
from types import SimpleNamespace as NS

from app.leads.export import export_leads_csv, neutralize_cell


def test_neutralize():
    assert neutralize_cell('=HYPERLINK("x")') == '\'=HYPERLINK("x")'
    assert neutralize_cell("@SUM(A1)") == "'@SUM(A1)"
    assert neutralize_cell("-cmd") == "'-cmd"
    assert neutralize_cell("+cmd|x") == "'+cmd|x"
    assert neutralize_cell("\tx") == "'\tx"
    assert neutralize_cell("+573001234567") == "+573001234567"
    assert neutralize_cell(-3) == "-3"
    assert neutralize_cell(None) == ""
    assert neutralize_cell(True) == "si"
    assert neutralize_cell(["a", "=b"]) == "a, =b"


def test_export_csv_roundtrip():
    lead = NS(
        name="=1+1",
        niche="dentista",
        city="Cali",
        phone_e164="+573001234567",
        rating=4.5,
        tags=["x", "y"],
        score=80,
    )
    out = export_leads_csv([lead])
    assert out.startswith("﻿")
    rows = list(csv.reader(io.StringIO(out.lstrip("﻿"))))
    assert rows[0][0] == "Nombre"
    assert rows[1][0] == "'=1+1"
    assert rows[1][4] == "+573001234567"
    assert rows[1][14] == "x, y"
