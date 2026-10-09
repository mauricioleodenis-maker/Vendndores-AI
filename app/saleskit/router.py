"""Kit de ventas: lectura de los documentos de ``docs/ventas`` en ``/admin/kit-ventas``."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.core.deps import current_user
from app.core.errors import AppError
from app.saleskit import service
from app.web.templating import render

router = APIRouter(
    prefix="/admin/kit-ventas", tags=["kit-ventas"], dependencies=[Depends(current_user)]
)


@router.get("", response_class=HTMLResponse)
async def kit_index(request: Request) -> HTMLResponse:
    return render(request, "saleskit/index.html", {"docs": service.list_docs()})


@router.get("/{slug}", response_class=HTMLResponse)
async def kit_doc(request: Request, slug: str) -> HTMLResponse:
    found = service.get_doc(slug)
    if found is None:
        raise AppError("not_found", "Documento no encontrado.", 404)
    doc, body = found
    return render(
        request, "saleskit/doc.html", {"docs": service.list_docs(), "doc": doc, "body": body}
    )
