from __future__ import annotations

import pytest

from app.saleskit.markdown import inline, render_markdown


def test_escapes_raw_html() -> None:
    out = str(render_markdown("<script>alert(1)</script>\n\n> <img src=x onerror=alert(1)>"))
    assert "<script>" not in out
    assert "<img" not in out
    assert "&lt;script&gt;" in out


def test_inline_formats_after_escaping() -> None:
    assert inline("**a** *b* `c`") == "<strong>a</strong> <em>b</em> <code>c</code>"
    assert "&lt;b&gt;" in inline("<b>x</b>")


def test_quote_attribute_injection_is_escaped() -> None:
    out = str(render_markdown('> hola "x" onclick="y"'))
    assert 'onclick="y"' not in out


def test_headings_shift_and_hr() -> None:
    out = str(render_markdown("# T\n## S\n---"))
    assert "<h2>T</h2>" in out and "<h3>S</h3>" in out and "<hr>" in out


def test_lists_and_checklist() -> None:
    out = str(render_markdown("- a\n- b\n\n1. x\n2. y\n\n- [ ] todo\n- [x] hecho"))
    assert "<ul><li>a</li><li>b</li></ul>" in out
    assert "<ol><li>x</li><li>y</li></ol>" in out
    assert "kit-check" in out and "&#9745;" in out and "&#9744;" in out


def test_table() -> None:
    out = str(render_markdown("| A | B |\n|---|---|\n| 1 | <i>2</i> |"))
    assert "<th>A</th>" in out and "<td>1</td>" in out and "&lt;i&gt;" in out


def test_code_block_escaped() -> None:
    out = str(render_markdown("```\n<b>x</b>\n```"))
    assert "<pre><code>&lt;b&gt;x&lt;/b&gt;</code></pre>" in out


def test_quote_has_copy_button_and_paragraphs() -> None:
    out = str(render_markdown("> uno\n>\n> dos"))
    assert out.count("data-kit-copy") == 1
    assert "<p>uno</p><p>dos</p>" in out


def test_paragraph_joins_lines() -> None:
    assert "<p>a b</p>" in str(render_markdown("a\nb"))


@pytest.mark.parametrize("text", ["", "\n\n"])
def test_empty(text: str) -> None:
    assert str(render_markdown(text)) == ""
