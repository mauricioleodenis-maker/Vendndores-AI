"""Renderizador Markdown minimo y seguro: todo el texto se escapa ANTES de dar formato.

Soporta titulos, parrafos, listas, tablas, citas (mensajes copiables), reglas, codigo y
negrita/cursiva/codigo en linea. No interpreta HTML crudo ni enlaces.
"""

from __future__ import annotations

import re
from html import escape

from markupsafe import Markup

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_CODE = re.compile(r"`([^`]+)`")
_ULIST = re.compile(r"^\s*[-*]\s+(.*)$")
_OLIST = re.compile(r"^\s*\d+\.\s+(.*)$")
_HEAD = re.compile(r"^(#{1,4})\s+(.*)$")
_CHECK = re.compile(r"^\[( |x|X)\]\s+(.*)$")
_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def inline(text: str) -> str:
    """Escapa y luego aplica formato en linea. El resultado es seguro para insertar como HTML."""
    out = escape(text, quote=True)
    out = _CODE.sub(r"<code>\1</code>", out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return _ITALIC.sub(r"<em>\1</em>", out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_table_start(lines: list[str], i: int) -> bool:
    return (
        "|" in lines[i]
        and i + 1 < len(lines)
        and bool(_SEP.match(lines[i + 1]))
        and "|" in lines[i + 1]
    )


def _list_item(text: str) -> str:
    m = _CHECK.match(text)
    if m:
        mark = "&#9745;" if m.group(1).lower() == "x" else "&#9744;"
        label = inline(m.group(2))
        return f'<li class="kit-check"><span aria-hidden="true">{mark}</span> {label}</li>'
    return f"<li>{inline(text)}</li>"


def render_markdown(source: str) -> Markup:
    lines = source.replace("\r\n", "\n").split("\n")
    html: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.strip().startswith("```"):
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            html.append(f"<pre><code>{escape(chr(10).join(buf))}</code></pre>")
            continue
        head = _HEAD.match(line)
        if head:
            level = min(len(head.group(1)) + 1, 5)  # el h1 de la pagina es el titulo del documento
            html.append(f"<h{level}>{inline(head.group(2))}</h{level}>")
            i += 1
            continue
        if line.strip() in {"---", "***"}:
            html.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            quote: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote.append(lines[i].lstrip()[1:].removeprefix(" "))
                i += 1
            paras = [p for p in "\n".join(quote).split("\n\n")]
            body = "".join(
                f"<p>{inline(p.strip()).replace(chr(10), '<br>')}</p>" for p in paras if p.strip()
            )
            html.append(
                '<div class="kit-msg"><div class="kit-msg-text">'
                f"{body}</div>"
                '<button type="button" class="btn btn-ghost kit-copy" '
                "data-kit-copy>Copiar</button></div>"
            )
            continue
        if _is_table_start(lines, i):
            header = _cells(line)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in header)
            tr = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            html.append(
                f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead>'
                f"<tbody>{tr}</tbody></table></div>"
            )
            continue
        ul, ol = _ULIST.match(line), _OLIST.match(line)
        if ul or ol:
            pat, tag = (_ULIST, "ul") if ul else (_OLIST, "ol")
            items: list[str] = []
            while i < len(lines) and (m := pat.match(lines[i])):
                items.append(_list_item(m.group(1)))
                i += 1
            html.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        para: list[str] = [line.strip()]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not _HEAD.match(lines[i])
            and not lines[i].lstrip().startswith((">", "```"))
            and not _ULIST.match(lines[i])
            and not _OLIST.match(lines[i])
        ):
            para.append(lines[i].strip())
            i += 1
        html.append(f"<p>{inline(' '.join(para))}</p>")
    return Markup("\n".join(html))  # noqa: S704 - cada fragmento fue escapado arriba
